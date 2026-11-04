# Offline PCB tools

Read, check, edit and write Altium `.PcbDoc` files without Altium. Run from the
`server/` directory:

    python -m offline info board.PcbDoc
    python -m offline connect board.PcbDoc            # islands, gaps, shorts
    python -m offline pdn board.PcbDoc --net VBAT --source J1.1 --sink U5.3 --current 30
    python -m offline rules board.PcbDoc
    python -m offline render board.PcbDoc -o board.png --net VBAT
    python -m offline edit board.PcbDoc plan.json -o edited.PcbDoc
    python -m offline pour board.PcbDoc --compare           # repour offline, check against saved
    python -m offline drc board.PcbDoc [--preset jlcpcb-2oz] # the board's rules, without Altium
    python -m offline eco preview board.PrjPcb               # schematic vs board, like Update PCB
    python -m offline copper plan.json board.PcbDoc -o out.json  # plan power copper for a current
    python -m offline thermal board.PcbDoc --flow "VBAT:30:J1.1>Q1.D" --power Q1=2
    python -m offline vias 30 --hole 0.3                      # via array for a layer change
    python -m offline fuse board.PcbDoc F1.2 --rating 10 --i2t 500
    python -m offline fets board.PcbDoc --current VBAT=30     # FET pad entry density
    python -m offline autoroute board.PcbDoc --nets 'SIG*' -o plan.json --check  # Freerouting

All lengths are millimetres. Coordinates are absolute (database) coordinates,
Y up, as the file stores them; `PcbDoc.origin` gives the user origin.

Install: `pip install -r server/offline/requirements.txt` (numpy, shapely,
scipy, Pillow; olefile for tests).

## Modules

| Module | Does |
|---|---|
| `cfb.py`, `cfb_write.py` | compound-file container: read; write with FAT, mini FAT, DIFAT, directory |
| `pcbdoc.py` | the reader: every storage parsed losslessly, mm API |
| `layers.py` | layer numbers, names, copper stack order |
| `records.py`, `templates.py` | construct new tracks, arcs, vias, pads, fills, regions, texts |
| `pcbwrite.py` | save (never onto the input), add records, integrity check |
| `geom.py` | shapely shapes of primitives |
| `connect.py` | islands per net, unrouted gaps, shorts, dead copper |
| `scope.py`, `rules.py` | rule scope language and rule resolution |
| `raster.py` | exact pixel-centre rasterizing per net |
| `pdn.py` | DC voltage drop, current density, power, via currents |
| `render.py` | PNG/SVG of layers, nets, ratsnest, markers, heat maps, diffs |
| `edits.py`, `editplan.py`, `live.py` | in-memory edits, JSON EditPlans, the same plan as live scripts |
| `pour.py` | polygon pours: clearances, reliefs, necks, islands, dead copper, pour order |
| `drc.py` | design rule check from Rules6, fab presets for 2 oz copper, cached-result comparison |
| `sch.py` | SchDoc/PrjPcb reader: parts, pins, footprints, the netlist |
| `footprints.py` | PcbLib reader; footprints from a library or another board; place and swap |
| `eco.py` | preview, link, apply (as an EditPlan), swap, gaps |
| `copper.py` | power-copper planner: free room, corridor, layer joins, neck vias, PDN loop |
| `thermal.py` | steady-state temperature rise from PDN heat and component watts |
| `power.py` | IPC-2221 and Onderdonk formulas, via arrays, fuse vs copper, FET pad screen |
| `specctra.py` | Specctra DSN export and SES import (`dsn`, `ses` commands) |
| `autoroute.py` | route signal nets with Freerouting, back as an EditPlan |
| `synth.py` | small synthetic boards for tests |
| `fixtures.py` | where tests find real boards |

## Facts the tools rely on

The binary layout and every cross-reference between streams are written down
in [`PCBDOC_FORMAT.md`](PCBDOC_FORMAT.md). Tests:
`python -m unittest discover -s server/tests -p "test_offline*.py"`.

## Pours

`pour.PourEngine(doc)` computes what a repour gives from each polygon's own
settings and the board's rules (see the module docstring for what is and is
not modelled). `repour_all("clean")` pours in POURINDEX order from nothing;
`"sequential"` replays Altium's Repour All from the copper saved in the file,
so `dents()` shows where stale copper would leave a dent. `store(results,
"pour" | "solid" | "unpoured")` writes the result through an Editor; the
board in memory is then usable by `connect`, `pdn`, `render` and the DRC.
`effective_copper()` is what checks use: saved copper where it exists, a
fresh pour where a polygon is unpoured or listed as stale.

## DRC

`drc.DRC(doc).run()` checks Clearance, ShortCircuit, UnRoutedNet, Width,
HoleSize, MinimumAnnularRing, HoleToHoleClearance, BoardOutlineClearance,
SilkToSolderMaskClearance and ComponentClearance from the board's own rules.
It pours unpoured polygons first (`--copper`), honours keep-out restriction
masks, treats touching no-net copper as joined, and leaves text out unless
`--text` (then each text is its box). `--compare-cached` checks against the
DRC results Altium saved in the file; `dev/compare_drc.py` against a live
`get_drc_violations` export.

## ECO

`eco preview` compares the project's netlist with the board pin by pin and
names renames, merges, splits and single-pin moves as well as parts to add,
remove or re-footprint. `eco apply` turns that into an EditPlan (merges first
so tracks and pours follow) and, with `-o`, writes the board and lists the
gaps left to route. `eco gaps` finds pieces of a net that are not joined,
e.g. both sides of a removed part once its nets are merged.

## Power copper

`copper plan.json` shapes a net's copper for a current. Copper may go
wherever the net may have copper: inside the board and the plan's area,
outside keep-outs, clear of every other net by the clearance a region of
this net would get (the voids `pour.py` uses). The planner turns the free
room into pieces per layer and joins them: a piece stays when a terminal
touches it or it lies on a path from the sources to the sinks, so nothing
it adds floats. Where a path changes layer it places a via array (sized by
`power.size_vias` for the whole current), plus arrays at both terminals.
It then loops against `pdn.py`: solve, find the worst 1 mm neck, widen the
corridor there, and when a neck stops improving put a via cluster at it so
the current can change layers around the obstacle. It stops when the targets
are met or the room stops growing, and names the object that limits the
worst neck. Vias stay out of pads unless `"via": {"in_pad": true}`.
The output is an EditPlan (`add_region`, `add_via`) plus a report: drop,
watts, density, via currents against their IPC-2221 capacity, and a
thermal estimate. Exit status 1 when a target is missed.

`thermal` is a 2.5-D steady-state model: per copper layer a sheet of copper
and half the dielectric on each side, barrels between layers, natural
convection and radiation from both faces. PDN heat is the solver's own
per-cell dissipation; a powered component is one package node tied to its
pads through solder joints, so its figure is the case temperature. Checked
against published IPC-2152 figures (`thermal --ipc-check`): it reads cooler
than the charts, so treat it as a screen and keep margin; see
`dev/CLOUD_HANDOFF.md` for the numbers.

`fuse` compares a fuse's I²t with what the copper downstream survives
(Onderdonk, adiabatic) and its hold current with the IPC-2221 rating of the
narrowest neck. `fets` screens the current density where copper enters each
power FET pad. Both are screening checks, not certification.

Current density comes from the PDN grid: a cell is copper when its centre
is, so a neck reads within one cell of its width. Use a resolution of a
tenth of the narrowest neck you care about for about 10 %.

## Autorouting

`autoroute` exports a Specctra DSN, runs Freerouting headless and turns
its session (SES) into an EditPlan of tracks and vias. Without `--nets` it
takes every net with an open connection except power nets (a pour or plane,
a power-like name, a power net class, or tracks 0.4 mm wide and up): those
are the power-copper planner's. Existing copper stays put unless
`--keep-fixed none` lets the router redo the routed nets (the plan then
removes their free tracks and vias first). Other nets' poured copper is left
out of the design so routes may cross it; repour after applying the plan.
`--check` applies the plan in memory and runs the DRC before and after (with
pours repoured on both) and reports only new violations. Exit 1 when a
connection stays open or a violation is new.

The DSN gives each net to route a class with the width the Width rule
prefers and the largest clearance the Clearance rule asks of a track or via
against other copper. Copper of nets not routed goes in as one net per
connected island, each complete, in a class the router is told to ignore, so
it never touches them and sees nothing of theirs to connect. Freerouting
treats a track as joined to an SMD pad only where it ends at the pad's
centre, so short design-only stubs join off-centre track ends to their pads.

Freerouting (GPL) is not part of this repo: `autoroute --fetch` downloads the
pinned release from Maven Central and checks its SHA-1, or point
`--freerouting` / `$FREEROUTING_JAR` at a jar. Versions 2.4 and up need
Java 25 (`--java`, `$FREEROUTING_JAVA`, or found under `$JAVA_HOME`, PATH or
the usual install folders). It runs with analytics, its API and MCP servers
and fan-out off, settings and logs in the work folder, and Java's HTTP(S)
proxy pointed at a closed local port, so its network calls fail (the one
seen in its log is a version check). `--allow-network` lifts the block. Its automatic neck-down stays off: it narrows tracks to
0.75x and 0.6x of the class width at pins whatever the Width rule says;
`--neckdown` allows it and the DRC then shows where the rule breaks.
`--ses` imports a session routed elsewhere (e.g. in Freerouting's GUI).
