# .PcbDoc format notes

What the offline reader and writer rely on. Each fact was checked on KiCad's
public Altium test boards (`dev/fetch_fixtures.py`); the counts in brackets are
how many fixture records agree. KiCad's `pcbnew/pcb_io/altium/` parser and
AltiumSharp were read for orientation only; no code was copied.

## Container

- An OLE2 compound file, version 3, 512-byte sectors, 64-byte mini sectors,
  4096-byte mini-stream cutoff. Directory entries form proper red-black trees;
  CLSIDs are zero; some entries carry timestamps.
- Empty streams start at ENDOFCHAIN; storages have start 0, size 0.
- Boards over ~7 MB need DIFAT sectors (one fixture does).
- The writer reuses the source directory entry for entry when no entry was
  added or removed, so names, colours, sibling links, timestamps and state
  bits are preserved. A storage whose children change gets a balanced tree in
  CFB name order (length first, then upper case), validly coloured.

## Storages

Most data lives in top-level storages holding `Header` (u32 record count) and
`Data`. `PrimitiveParameters/Header` is not its record count; that storage is
never edited. `FileHeader` and `FileHeaderSix` are root streams.

### Binary primitive streams

A record is `u8 type`, then sub-records, each `u32 length` + bytes:

| Storage | type | sub-records | main sub-record lengths seen |
|---|---|---|---|
| Arcs6 | 1 | 1 | 56, 60 |
| Pads6 | 2 | 6 | name; 1; 5; 1; 120/171/194/202 (main); 0/596/651/666 (size and shape) |
| Vias6 | 3 | 1 | 209, 300, 321 |
| Tracks6 | 4 | 1 | 45, 49 |
| Texts6 | 5 | 2 | 230, 240, 252; then the string |
| Fills6 | 6 | 1 | 46, 50 |
| Regions6, ShapeBasedRegions6, BoardRegions | 11 | 1 | variable |
| ComponentBodies6, ShapeBasedComponentBodies6 | 12 | 1 | variable |

Common head of the main sub-record (offsets in bytes):

| Offset | Field |
|---|---|
| 0 | V6 layer (u8) |
| 1 | flags1: bit 2 set = unlocked, bit 1 = polygon outline |
| 2 | flags2: 2 = keepout |
| 3 | net index (u16, 0xFFFF none) |
| 5 | polygon index (u16, 0xFFFF none, 0xFFFE plane-boundary marker) |
| 7 | component index (u16, 0xFFFF none) |

Per type (coordinates are i32 in units of 1e-7 inch; angles f64 degrees):

- Track: 13 x1, 17 y1, 21 x2, 25 y2, 29 width, 33 sub-polygon index (u16),
  36 union index (u8), 41 V7 layer (u32, when length >= 45).
- Arc: 13 cx, 17 cy, 21 radius, 25 start angle, 33 end angle, 41 width,
  45 sub-polygon index, 48 union index, 52 V7 layer (when >= 56).
- Via: 0 is 74 (multi-layer); 13 x, 17 y, 21 diameter, 25 hole, 29 low layer,
  30 high layer, 74 diameter mode (0 simple, 1 top-middle-bottom, 2 full
  stack), 75..202 diameter per layer (32 x i32), 203 V7 layer. The table sits
  at 75 on all 802 fixture vias; byte 74 is 0 on all of them.
- Pad: sub-record 0 is the name (u8 length + cp1252). Main (sub-record 4):
  13 x, 17 y, 21 top size, 29 mid size, 37 bottom size (x, y each), 45 hole,
  49/50/51 top/mid/bottom shape (1 round, 2 rect, 3 octagonal), 52 rotation,
  60 plated, 62 mode, 86 paste expansion, 90 mask expansion, 101/102 paste/mask
  expansion mode, 106 hole rotation, 114 V7 layer. 126..141 is a pad-template
  GUID shared by pads of one template and 142..157 the PadViaLibrary GUID (not
  per-pad identities, so cloning a record is safe). Sub-record 5 (596+ bytes
  when present): 29 inner sizes x then y, 29 inner shapes, 1 byte, hole shape
  (0 round, 1 square, 2 slot), slot size, slot rotation (f64), 32 hole offsets
  x then y, 1 byte, 32 alternate shapes (9 = round-rect), 32 corner radii (%,
  radius = pct/100 * min(sx, sy)/2).
- Fill: 13 x1, 17 y1, 21 x2, 25 y2, 29 rotation (about the centre), 42 V7 layer.
- Text: 13 x, 17 y, 21 height, 25 font id (u16), 27 rotation, 35 mirrored,
  36 stroke width, 40 is comment, 41 is designator, 43 font type, 46 font name
  (64 bytes UTF-16), 115 WideStrings6 index (u32). Sub-record 1 is the string
  (u8 length + cp1252), truncated; the WideStrings6 entry is the real text.
- Region: 14 hole count (u16); 18 u32 length + `|KEY=VALUE` properties
  (V7_LAYER, KIND 0 copper / 1 polygon cutout / 2 dashed outline / 4 cavity,
  ISBOARDCUTOUT, ISSHAPEBASED, SUBPOLYINDEX, UNIONINDEX); then u32 vertex count
  and the outline; then per hole u32 count + vertices. Regions6 and
  BoardRegions store vertices as (f64 x, f64 y) in internal units.
  ShapeBasedRegions6 stores count + 1 extended vertices of 37 bytes (u8 round,
  i32 x, i32 y, i32 cx, i32 cy, i32 radius, f64 angle1, f64 angle2); its holes
  are still f64 pairs. A round vertex is an arc around (cx, cy) from angle1 to
  angle2; it starts from whichever arc end is nearer the vertex position.
- Bodies use the region layout; their properties carry MODELID, MODEL.2D.X/Y,
  MODEL.2D.ROTATION, MODEL.3D.ROTX/ROTY/ROTZ/DZ, STANDOFFHEIGHT, OVERALLHEIGHT.

### V7 layer ids

`0x01CCNNNN`: class 0 signal layers (N = layer 1..31, 0xFFFF bottom), class 1
internal planes (N = plane number), class 2 mechanical (N = 1..1024), class 3
others (6 top overlay, 7 bottom overlay, 8 top paste, 9 bottom paste, 10 top
solder, 11 bottom solder, 14 drill drawing, 15 multi-layer; 12 and 13 are
assumed drill guide and keep-out). Mechanical layers above 16 put 72 in the V6
byte and the real number in the V7 id; the offline API numbers them 1000 + n.

### Text-record streams

`[prefix] u32 length` (the high byte is a binary flag) then cp1252 text
`|KEY=VALUE|...` with a NUL that the length includes. Rules6 and Dimensions6
carry a 2-byte prefix. Keys compare case-insensitively. `UNICODE__KEY` (comma
separated code points) and `%UTF8%KEY` carry non-ASCII values; polygon NAME
can itself be comma-separated code points. Lengths are `1234.5678mil`; angles
` 2.70000000000000E+0002`. The reader decodes cp1252 with surrogate escapes so
every byte round-trips; an untouched record is written back byte for byte.

Parsed: Board6, Nets6, Components6, Polygons6, Classes6, Rules6, Dimensions6,
UniqueIDPrimitiveInformation, PrimitiveParameters, DifferentialPairs6,
SmartUnions, SignalClasses, ExtendedPrimitiveInformation, CustomMaskShapes,
SharedUnion and the cached-violation storages (`T...Viola...`).

### WideStrings6

`u32 index, u32 byte length, UTF-16 text + NUL`, indices sequential. An empty
string is written with length 2 and no bytes at all (all 4 empties in the
fixtures). Texts point at entries by index; removed texts leave their entry
behind (harmless), added texts get the next index.

## Cross-references the writer keeps consistent

By index (renumbered on every removal; additions append, so existing indices
never move):

| Who | Field | Points at |
|---|---|---|
| every binary primitive | net @3 | Nets6 |
| every binary primitive | polygon @5 | Polygons6 (0xFFFE is a marker) |
| every binary primitive | component @7 | Components6 (vias too: footprint thermal vias carry it, 42 in one fixture) |
| Polygons6 | NET | Nets6 |
| Regions6[i] | position | ShapeBasedRegions6[i] (parallel, 298/298) |
| ComponentBodies6[i] | position | ShapeBasedComponentBodies6[i] |
| UniqueIDPrimitiveInformation | PRIMITIVEOBJECTID + PRIMITIVEINDEX | that stream (only Pad seen; one per pad) |
| ExtendedPrimitiveInformation | PRIMITIVEOBJECTID + PRIMITIVEINDEX | that stream |
| CustomMaskShapes | PRIMITIVEINDEX | Pads6 (assumed: no object id is stored) |
| SharedUnion | OBJECTID/PRIMITIVEINDEX, REFnOBJID/REFnINDEX | those streams |
| Dimensions6 | REFERENCEnOBJECTSTRING + REFERENCEnPRIM | that stream; a removed target becomes NoObject |
| PrimitiveGuids | 24-byte entries: u32 kind, u32 index, GUID | kind = section << 8 \| object id: 0x1004 Tracks6, 0x0D01 Arcs6, 0x0E02 Pads6, 0x0F03 Vias6, 0x1105 Texts6, 0x1206 Fills6, 0x1359 Regions6, 0x1459 ShapeBasedRegions6, 0x155A/0x165A bodies, 0x0208 Nets6, 0x0309 Components6, 0x040A Polygons6, 0x0116 Classes6, 0x050B Dimensions6, 0x09xx Rules6 (xx = rule kind). New objects get new entries. |
| PadViaCacheLibraryLinksSection | 10-byte entries: u8 0, u32 index, u8 kind (2 pad, 3 via), u32 library entry | Pads6 / Vias6 |
| T...Violation storages | PRIMnID/PRIMnINDEX, RULEINDEX | objects and rules; dropped whenever indices shift (cached DRC results) |
| Texts6 | WideStrings6 index @115 | WideStrings6 entry index |

By name (kept in step by rename/merge/remove):

| Who | Field |
|---|---|
| Classes6 | M0..Mn: net names (KIND 0), designators (1), DES-PAD (3), polygon names (7), pair names (6) |
| DifferentialPairs6 | POSITIVENETNAME, NEGATIVENETNAME (a pair losing a net is dropped) |
| Board6 | PLANEnNETNAME |
| Rules6 | SCOPE1EXPRESSION, SCOPE2EXPRESSION (renamed nets are rewritten; merged nets are reported) |

By identity string: Components6 UNIQUEID and SOURCEUNIQUEID (schematic link),
PrimitiveParameters PRIMITIVEID (= a UNIQUEID), ComponentBodies6 MODELID
(Models/Data ID GUID), pads' and vias' template and library GUIDs.

## Placement convention

A component at (cx, cy), rotation r: top side `world = c + Rot(r) . local`;
bottom side `world = c + Rot(r) . (lx, -ly)` (44/44 bottom-side pads agree to
18 nm). Pad and fill angles become `r + a` on top and `r - a` on the bottom;
an arc from a1 to a2 becomes `(r - a2, r - a1)` on the bottom.

## Planes

Internal planes are negative. Split planes are Polygons6 records of type
`Split Plane` on the plane layer, with a net and an outline and no poured
regions; a plane without them carries `PLANEnNETNAME` over the board. Tracks
on plane layers are split lines (polygon field 0xFFFE when they bound the
plane).

## Polygons and poured copper

Polygons6 text records hold the outline (`KINDn VXn VYn CXn CYn SAn EAn Rn`)
and the pour settings: `POURINDEX` (pour order), `HATCHSTYLE`, `GRIDSIZE`,
`TRACKWIDTH`, `POUROVERSTYLE` (0 don't pour over same-net objects, 1 pour
over all of them, 2 same-net polygons only; older files only `POUROVER`),
`REMOVEDEAD`, `REMOVEISLANDSBYAREA` with `AREATHRESHOLD` in internal units
squared (2.5e11 = 2500 sq mil), `REMOVENECKS` with `NECKWIDTHTHRESHOLD`,
`ARCRESOLUTION`, `OBEYPOLYGONCUTOUT`, `SHELVED`.

A solid pour is stored as Regions6/ShapeBasedRegions6 pairs with the polygon
index at @5, no net of their own, and props `KIND=0`, `SUBPOLYINDEX=0`
(free regions carry -1). Each piece is one region with holes; Altium's rings
may touch themselves, which shapely calls invalid. Thermal reliefs are part
of the region outline. Regions with `KIND=1` are polygon cutouts; on the
Keep-Out or Multi layer they apply to every copper layer.

Measured on the fixture pours (pour.py): the gap to other objects is exactly
the clearance rule's value for a poured region (Poly class) except on one
board, which keeps ~0.008 mm more; relief air gaps around rectangular pads,
and clearance gaps around rectangular pads, have square corners; a relief
spoke exists when pour copper covers its continuation for half a conductor
width past the air gap; neck removal acts like a morphological opening with
half the neck width.

## Keep-outs

A track, arc or fill is a keep-out when flags byte 2 is 2; the byte after
its V7 layer id is the restriction mask (via 1, track 2, copper 4, SMD pad 8,
TH pad 16); 0, a missing byte, or the Keep-Out layer mean all. Regions keep
the mask in `KEEPOUTRESTRICTIONS`. A pour avoids a keep-out only when the
copper bit is set.

## Cached DRC results

`T<Kind>Violation` storages (TClearanceViolation, TMatchedNetLengthsViolation,
...) are text records: `RULEINDEX` (index into Rules6), `PRIM1ID`/`PRIM1INDEX`
and `PRIM2ID`/`PRIM2INDEX` (object kind and its index in that kind's
storage), `LOCATION1.X/Y`, `LOCATION2.X/Y`, `DESCRIPTION`. They go stale (and
are dropped by the editor) once an edit renumbers anything.

## Preserved without being understood

Kept byte for byte; listed with what could go stale after an edit:

| Storage | Holds | Stale risk |
|---|---|---|
| PrimitiveParameters | parameters by unique id; header is not the record count | parameters of a removed component stay behind |
| Models, ModelsNoEmbed | embedded STEP (zlib), by GUID | models of removed parts stay embedded |
| SmartUnions, UnionNames, UnionFeatures | unions by union index | a union may name removed primitives (union index, not primitive index) |
| LettersGeometry | TrueType glyph geometry cache (6 entries, by font) | text added offline has no cached glyphs; Altium regenerates (UNVERIFIED) |
| PadViaLibrary, PadViaLibraryCache | pad/via templates | edited pad sizes no longer match their template |
| EmbeddedFonts6, Texts (root), FileVersionInfo, Textures | fonts and version info | none known |
| Connections6, Coordinates6, FromTos6, Embeddeds6, EmbeddedBoards6, WaivedViolations, PinPairsSection | empty in every fixture | unknown when populated |
| Advanced Placer / DRC / Pin Swap options, ConstraintManager, LayerKindMapping, SimbeorCacheSection, PadViaCacheLibraryLinks | settings | none known |
| per-GUID storages of a .CSPcbDoc | unknown | not edited |

## Not known

- Whether Altium accepts a record cloned from a template of another file
  version (the writer clones from the board itself when it can).
- Whether new pads need a PadViaCacheLibraryLinksSection entry (none is added).
- The meaning of track byte 36 / arc byte 48 beyond "union index", and of the
  trailing 4 bytes of 49-byte tracks and 60-byte arcs.
- V7 class 3 codes 12 and 13.
