# Cloud session handoff: offline PCB tools

Written by an unattended cloud session with no Altium. Everything that needs
Altium is listed under **UNVERIFIED** with the exact local check. "Tests pass"
below means the offline unit tests in this repo pass on Linux; it never means
Altium accepted anything.

Branch: `claude/gracious-rubin-f1hbv2`.

## Setup facts

- `origin/phase-6-pdm-review-tools` does **not exist on the remote** (checked
  2026-11-04 with `git ls-remote`). Per the brief, work started from
  `origin/main` (`e29e0ac`) with `origin/claude/eager-ptolemy-4w4c4m` merged in
  (fast-forward to `09efd44`). The offline reader that branch was meant to
  carry (`server/offline/*`, `server/offline_tools.py`,
  `server/tests/test_offline.py`, `dev/VALIDATION.md`) was therefore rebuilt
  here from scratch. If the phase-6 branch exists locally, expect overlapping
  module names (`pcbdoc.py`, `cfb.py`, `raster.py`, ...) with different
  internals; merge by keeping one reader, not both.
- Fixtures: `python dev/fetch_fixtures.py` downloads KiCad's public Altium
  importer test files (GitLab `kicad/code/kicad`, pinned commit
  `bbe344f935a87778920165cb8e019224dbd476f2`) to a cache outside the repo
  (`%LOCALAPPDATA%\altium-mcp\fixtures`, `~/.cache/altium-mcp/fixtures`, or
  `$ALTIUM_FIXTURE_CACHE`). 9 PcbDocs, 3 PcbLibs, 3 SchDocs, 2 PrjPcbs.
- Network: GitLab, raw.githubusercontent.com and `git clone` from GitHub work;
  `api.github.com` returns 403 through the session proxy.

## Packages

| Package | State | Commit |
|---|---|---|
| WP1 script preflight + unwedge | done (offline tests); live UNVERIFIED | `bf2d5e8` |
| Reader foundation (rebuilt: phase-6 missing) | done | `83dcf11` |
| WP2 renderer | done | `00a2c0b` |
| WP3 PcbDoc writer, editor, EditPlan, live backend | done offline; Altium open + live scripts UNVERIFIED | `9189a8a` |
| WP4 pour engine | done offline; Altium reading stored pours UNVERIFIED | `f51f28c` |
| WP5 offline DRC | done offline; agreement with Altium's DRC UNVERIFIED | `f0b9814` |
| WP6 offline ECO | done offline; Altium's view of ECO'd boards UNVERIFIED | `cd73856` |
| WP7 power copper: planner, thermal, vias, fuse, FETs | done offline; Altium's view of planned copper UNVERIFIED; thermal accuracy is a screen | see git log ("power copper") |

## How to run

- Preflight a body by hand: `python server/script_preflight.py body.pas [--sandbox dev/sandbox/Sandbox.pas] [--allow-unknown]`
- Recover a wedged executor: `python dev/unwedge.py [--project dev]`; state only: `python dev/unwedge.py --status`
- Fetch fixtures: `python dev/fetch_fixtures.py [--list]`
- Offline tools run from `server/`: `python -m offline <command> --help`
- Render: `python -m offline render board.PcbDoc -o out.png|svg [--layers ...] [--net VBAT] [--around U5 --margin 10 | --box x0 y0 x1 y1] [--ratsnest] [--markers drc.json] [--pdn VBAT --source J1.1 --sink U5.3 --current 30] [--diff other.PcbDoc]`
- PDN: `python -m offline pdn board.PcbDoc --net VBAT --source J1.1 --sink U5.3 --current 30 [--png heat.png]`
- Connectivity: `python -m offline connect board.PcbDoc`
- Apply an EditPlan offline: `python -m offline edit board.PcbDoc plan.json -o edited.PcbDoc [--dry-run]`
- Same plan as live scripts: `python -m offline edit board.PcbDoc plan.json --live scripts_dir --board-path "C:\...\board.PcbDoc"`
- Check what Altium accepts: `python dev/validate_pcbwrite.py board.PcbDoc --out folder`
- DRC: `python -m offline drc board.PcbDoc [--json] [--box x0 y0 x1 y1] [--check Clearance Width ...] [--preset jlcpcb-2oz|pcbway-2oz|oshpark-2oz] [--copper auto|saved|repour] [--markers drc.json] [--compare-cached] [--text]`
- Compare with Altium's DRC: `python dev/compare_drc.py offline_drc.json altium_drc.json`
- ECO: `python -m offline eco preview board.PrjPcb [board.PcbDoc] [--json]`; `eco link board.PrjPcb board.PcbDoc -o linked.PcbDoc`; `eco apply board.PrjPcb board.PcbDoc --lib parts.PcbLib [--plan plan.json] [-o out.PcbDoc]`; `eco swap board.PcbDoc U1 NEW_FOOTPRINT --lib parts.PcbLib -o out.PcbDoc`; `eco gaps board.PcbDoc [--net VBAT]`
- Repour offline: `python -m offline pour board.PcbDoc [--polygon N|name] [--mode clean|sequential|saved] [--compare] [--dents] [-o out.PcbDoc --store pour|solid|unpoured] [--png pours.png] [--json]`
- Plan power copper: `python -m offline copper plan.json board.PcbDoc -o plan_out.json [--png copper.png] [--json]` (plan format in `server/offline/copper.py`'s docstring; exit 1 when a target is missed), then `python -m offline edit board.PcbDoc plan_out.json -o planned.PcbDoc`
- Thermal: `python -m offline thermal board.PcbDoc --flow "VBAT:30:J1.1>Q1.5+Q1.6" [--power Q1=1.8] [--ambient 25] [--res 0.5] [--json]`; against IPC-2152: `python -m offline thermal --ipc-check`
- Via array: `python -m offline vias 30 [--hole 0.3] [--layers 0.07 0.07]`
- Fuse vs copper: `python -m offline fuse board.PcbDoc F1.2 --rating 10 [--i2t 500 | --fault AMPS SECONDS]`
- FET pads: `python -m offline fets board.PcbDoc --current VBAT=30 [--flow "VBAT:30:J1.1>Q1.D"]`
- Second reader (Linux, KiCad installed): `python3.12 dev/kicad_read.py a.PcbDoc b.PcbDoc`
- All offline tests: `python -m unittest discover -s server/tests -p "test_[!a]*.py"`.
  The pattern leaves out `test_altium_script.py`, the pre-existing live
  integration test, which errors at setUpClass on any machine without
  `C:/AltiumMCP` and a running Altium (it did before this session too).

## Reader foundation

Rebuilt from scratch under `server/offline/` (see `server/offline/README.md`).
Measured on the KiCad fixtures:

- The compound-file reader matches olefile stream for stream on all 15 files.
  The writer round-trips all 14 compound files (PcbDoc, PcbLib, SchDoc) with
  identical streams and entry metadata, checked by both readers.
- Every parsed storage of all 7 PcbDocs re-encodes to its original bytes; a
  no-op `pcbwrite.save` gives byte-identical stream contents.
- Connectivity on the 5 routed boards: islands equal nets, 0 unrouted, 0
  shorts, 0 dead copper. That only holds once vias, planes and pours are all
  decoded right, so it is the main evidence the geometry is correct.
- All 218 fixture rules compile in the scope evaluator.
- PDN: a 28 mm x 1 mm x 35 um track gives 137.93 mV at 10 A (0.05 mm grid)
  against 137.6 mV from rho*L/(w*t), and converges to it as the grid refines.
- Speed: parse 0.01-0.11 s; connectivity 0.1-0.7 s; a whole-board plane PDN
  solve 3.8 s at 0.1 mm.

Bugs of the earlier reader that the brief lists, handled here: free fills are
copper; poured regions take their polygon's net; each shape is rasterized on
its own (even-odd over its own rings), so holes never blank other copper.

## WP1 notes

- `run_altium_script` now runs a static preflight (`server/script_preflight.py`)
  before injecting anything. Allow-lists come from the target sandbox's own
  declarations, every `.pas` file in the repo (names used after a dot become
  members; bare names the repo uses without declaring become API globals; names
  the repo declares as locals are *not* allowed in a body), and a seed list of
  calls proven on a live board. New parameter `allow_unknown`.
- `server/altium_guard.py` checks X2.EXE before any launch (tasklist + the
  `IsHungAppWindow` state of X2.EXE's own windows) and keeps a wedge marker at
  `C:\Users\Public\altium_mcp\executor_wedged.json`. While it exists,
  `run_altium_script` and every bridge tool return at once without sending
  anything. A bridge timeout also writes the marker. Recovery (new MCP tool
  `recover_altium_script_engine`, or `dev/unwedge.py`) clears it only after a
  probe script runs; an Altium restart (new PID) clears it too.
- `dev/unwedge.py` fixes: probes `server/SandboxScript` by default (`--project
  dev` for the runner's sandbox); Ctrl+F3 is sent only after
  `WorkspaceManager:OpenObject` brings `Sandbox.pas` forward and the
  foreground frame's title names it; never dispatches to a dead or hung
  Altium; reads `server/config.json` (it used to read a file nothing writes).
- Dialog closing now only touches X2.EXE's own windows. The old code closed
  every `#32770` dialog on the desktop, from any program.
- Both sandbox templates had leftover experiment bodies naming a personal
  user path and a project; they now hold a placeholder. The old text is still
  in git history.

## WP3 notes (writer, editor, EditPlan, live backend)

- Format facts and every cross-reference the editor keeps consistent are in
  `server/offline/PCBDOC_FORMAT.md`.
- `cfb_write.py` rewrites the compound file. When the set of streams is
  unchanged it keeps the source's directory (names, CLSIDs, timestamps, tree)
  and only re-lays sectors. Writers refuse to write onto their input
  (`pcbwrite.save`, `cfb_write.write`, `Editor.save`, the `edit` CLI).
- `pcbwrite.save` re-parses what it wrote and runs `check_integrity` before
  and after; it raises (and leaves no file) when a count or reference is off.
- Edits (`edits.Editor`): select/remove primitives, add track/arc/via/fill/
  region/text/pad, add/rename/merge/remove nets, set_net, add/remove/move/
  rotate/flip components, add/remove/unpour/reshape polygons, merge two
  polygons, add net classes and rules, change rule scopes. EditPlan JSON
  (`editplan.py`) wraps the same ops; `--dry-run` applies in memory and
  reports. Ops and fields: `editplan.OPS`.
- Acceptance, measured here:
  - No-op save: every stream byte-identical on all 7 fixture PcbDocs.
  - Single edits change only the streams they must (test asserts the set).
  - KiCad 7's Altium importer (independent code) reads every no-op save
    with the same counts as the original, and sees each edit (component
    removed with its pads and owned vias, track and via added, net renamed).
    Test: `server/tests/test_offline_kicad.py` (skips without pcbnew).
  - `dev/validate_pcbwrite.py` on a copy of the Fastino fixture: all 16
    single-edit files and the cumulative file written; KiCad reads each with
    the expected change (track +1, via +1, fill +1 zone, arc +1, text +1,
    pad +1, region +1, component -1 with 2 pads, merged net -1, unpoured
    polygon -1 filled zone). Altium has not opened any of them.
- Live backend (`live.py`): one DelphiScript body per op for
  `run_altium_script`, written with a `manifest.json`. Every body passes
  the preflight against `server/SandboxScript/Sandbox.pas`. Given the saved
  board, `remove` and `set_net` bodies first count their selection and
  refuse (changing nothing) if the live board differs. `proven: false` in
  the manifest marks bodies using calls never run on a live board (arcs,
  regions, texts, component removal, polygon lookup by name, net classes).
  Ops without a safe live form (add_pad, add_component, add_net,
  remove_net, add_polygon, set_polygon, merge_polygons, add_rule) raise and
  say to apply them offline.
- `set_polygon_outline` live needs `Seg : TPolySegment;` in the var section
  of `server/SandboxScript/Sandbox.pas`. Not added: the template is shared by
  every sandbox body, and a change can't be compiled here. Add it by hand if
  you want that op live.
- Cached DRC results (`*Violation` storages) cite objects by index; when an
  edit renumbers anything they are dropped. Altium rebuilds them on the next
  DRC; the report says how many were dropped.
- Streams this code does not parse are carried through byte for byte (list:
  `PcbDoc.unparsed()`); an edit that would need to change one raises.

## WP4 notes (pour engine)

`server/offline/pour.py`. Each polygon is poured from its own settings and
the board's rules: outline, board outline at BoardOutlineClearance, polygon
cutouts, keep-outs, other-net objects at the clearance rule's gap for poured
copper, same-net objects by pour-over style, pads/vias by PolygonConnect
(including the newer per-object THPAD./SMDPAD./VIA. settings), relief spokes,
neck removal, islands under the area threshold, dead copper, pour order.

Accuracy: every fixture polygon poured against the rest of the board as
saved (`--mode saved --compare`), compared with the copper Altium saved.
Copper of the polygon's own net is left out on both sides (Altium's regions
skip over same-net pads and tracks, which are copper anyway).

| Fixture | Polygons | Saved mm2 | IoU | Worst polygon | Missing mm2 | Extra mm2 | Seconds |
|---|---|---|---|---|---|---|---|
| HiFive1.B01 | 21 | 4118.3 | 0.9959 | 0.9889 | 1.36 | 15.75 | 1.49 |
| eDP_adapter_dvt1 | 2 | 1719.4 | 0.9898 | 0.9860 | 2.60 | 15.18 | 2.03 |
| Fastino_Ground_Isolator | 15 | 6515.3 | 0.9917 | 0.9330 | 43.43 | 10.78 | 2.02 |
| issue24654 PCB1 | 2 | 4789.8 | 0.9956 | 0.9925 | 0.25 | 20.78 | 0.41 |
| issue24847 PCB1 | 1 | 256.8 | 0.9976 | 0.9976 | 0.02 | 0.59 | 0.01 |
| all | 41 | 17399.7 | 0.9937 | | 47.65 | 63.07 | |

- Fastino's saved pours connect every SMD pad directly although the board's
  only PolygonConnect rule says SMD pads relief (its TH pads do have
  reliefs). With SMD pads forced direct its IoU is 0.9977 (worst 0.974). The
  engine follows the rule; the saved pours look older than the rule.
- Most of the remaining extra copper is a few micrometres along curved
  edges: Altium's arc approximation sits outside the true clearance circle
  (vertices out, chords tangent), ours on it. issue24654 also keeps about
  0.008 mm more than its rule on every gap; nothing in the file says why.
- Rules found by measuring the fixture pours and built in: gap = clearance
  rule for a poured region (Poly class) exactly; relief air gaps and
  clearance gaps around rectangular pads have square corners; a spoke is
  placed when pour copper covers its continuation for half a conductor width
  past the air gap (1,200 spoke positions: 502 kept / 26 not at >= 95 %
  cover, 11 kept / 525 not below 25 %); neck removal is an opening with half
  the neck width and round joins (mitred joins regrow the cut necks).
- Keep-outs block a pour only when their restriction mask has the copper
  bit (Fastino's two keep-out regions are for vias and TH pads only).
  Every clearance void gets 2 um extra so polygonal arcs never cut into a
  gap (DRC-clean pours; it also matched Altium slightly better).
- Not modelled: hatched pours (poured solid, flagged in the report, stored
  unpoured); copper text is its box; unused inner-layer pad removal;
  `OPTIMALVOIDROTATION`; `NECKWIDTHFROMRULE` uses the Width rule's minimum
  (a guess, no fixture shows it). Split planes are not pours (connect.py
  models planes).
- Pour order: `--mode sequential` replays Repour All from the saved copper
  (each pour avoids whatever the others hold at that moment); `--mode clean`
  starts from nothing. `--dents` lists where they differ, and `--store
  unpoured -o out.PcbDoc` writes the board with those pours stripped so
  Altium's Repour All starts clean (the fill-and-rebuild dance in the brief
  is not needed then; UNVERIFIED).
- Speed: a 120 x 100 mm synthetic board with 3,612 tracks, 500 arcs, 480
  pads, 300 vias and 48 polygons (`synth.stress_board()`) repours in 2.4 s;
  the dent check (two full runs) in 5 s. Every fixture board repours in
  under 2.1 s.
- After a clean offline repour of every fixture board, stored as pours,
  connectivity is unchanged (all nets one island, 0 shorts, 0 dead copper)
  and KiCad reads each board with the same counts as the original.
- DRC and other checks get copper from `PourEngine.effective_copper()`:
  saved copper where it exists, a fresh pour for unpoured or edited
  polygons, and a note of which it used.

## WP5 notes (offline DRC)

`server/offline/drc.py`, `python -m offline drc`. Rules come from Rules6 and
resolve through scope.py / rules.py exactly as for pours (priority, both
scope orders, per-object clearance matrix, per-layer width overrides).

- Checks: Clearance (copper of different nets, keep-outs, drill holes not
  covered by their own copper, copper text with `--text`), ShortCircuit,
  UnRoutedNet (connect.py), Width, HoleSize, MinimumAnnularRing (geometric:
  hole edge to pad edge on every layer), HoleToHoleClearance,
  BoardOutlineClearance (outline and cutouts), SilkToSolderMaskClearance
  (silk against pad mask openings; vias only when a SolderMaskExpansion rule
  untents them) and ComponentClearance (3D body outlines and their
  standoff/overall heights; reported as approximate).
- Pours: `--copper auto` (default) keeps saved copper and repours unpoured
  polygons in memory first, and the report lists which; `saved` and
  `repour` force one or the other.
- Behaviour decided from fixture evidence: no-net copper that touches is
  one piece (issue24847: a no-net fill overlapping no-net pads is not in
  Altium's saved results); keep-outs are checked against every object their
  mask names, their own footprint's included (issue24847: Altium flagged
  keep-out arcs against the fiducial pads of the same footprint). The mask
  matters: on Fastino 22 track-in-keep-out hits went away once vias-and-TH-
  pads-only keep-outs were honoured.
- Against Altium: issue24847's file holds Altium's own DRC result (two
  clearance violations). The offline DRC finds exactly those two and nothing
  else of that kind (`--compare-cached`; also a unit test). No other fixture
  stores clearance-type results.
- Per fixture (default checks): HiFive 2 silk-to-mask + 14 component (all
  approximate), eDP 3 clearance, Fastino 10 clearance (no-net artwork
  regions closer than the gap) + 1 silk, issue24654 596 width (its only
  Width rule is 10/10 mil while the diff pairs are 15 mil) + 144 silk,
  issue24847 the 2 above. None verified against Altium beyond issue24847.
- Arc tracks and arc region edges are approximated to 0.2 um for these
  measurements; a violation needs to miss its limit by more than 0.5 um.
- DFM presets for 2 oz outer copper, from each fab's own page read
  2026-11-04 (the URL is in the report): `jlcpcb-2oz` (0.16/0.16 mm, drill
  0.15, PTH ring 0.254, via ring 0.05, hole-to-hole 0.2 via / 0.45 pad, hole
  to copper 0.2 via / 0.28 PTH, edge 0.2), `pcbway-2oz` (advanced table,
  70 um, normal process: 7/8 mil, drill 0.20, ring 7 mil via / 12 mil PTH,
  hole-to-hole 11 / 16 mil, edge 0.25), `oshpark-2oz` (2 Layer 2oz 0.8mm
  service: 6/6 mil, drill 10 mil, ring 5 mil, holes 5 mil apart, edge
  keepout 15 mil). Fabs change these; the preset says where to check.
- Speed: fixtures 0.01-1.7 s; the 3,612-track stress board 1.4 s of checks
  plus 3.0 s to pour its 48 unpoured polygons.
- Not checked: text shapes (no glyph outlines), SilkToSilk, net antennae,
  matched lengths and differential-pair rules, mask slivers, testpoints,
  plane clearance (planes are not modelled as copper shapes yet), relative
  (percent) hole-size limits, vertical clearances from real 3D models.

## WP6 notes (offline ECO)

`footprints.py` and `schtools.py` from the missing phase-6 branch did not
exist here, so this package adds `sch.py` (SchDoc/PrjPcb reader and
netlister), `footprints.py` (PcbLib reader, footprints from another board,
place and swap) and `eco.py`.

- Netlist check: the eDP fixture project (2 sheets) netlists to exactly the
  board's 112 parts and 78 nets; all 368 pins carry the same net *name* as
  their pad, auto-generated `NetXX_n` names included, and every footprint
  matches. `eco preview` on it reports nothing to do except one stale
  source link (C22P), which `eco link` fixes.
- Preview compares membership pin by pin: renames, merges (one schematic net
  over several board nets), splits, pin moves, parts to add/remove,
  footprint and comment changes, unmatched pins/pads, stale source links.
  Board parts with no schematic link (no SOURCEUNIQUEID) are listed and left
  alone unless `--remove-unlinked`.
- Apply order: remove parts, merge nets (tracks and pours follow), rename
  (through temporary names when names swap), move single pins, swap
  footprints, place new parts in a column right of the board outline with
  their nets. Comment changes are reported, not applied.
- New EditPlan ops: `set_pad_net`, `set_component`, `place_footprint`,
  `swap_footprint` (offline only; live.py refuses them).
- Footprints: placing clones the library records (full pad stacks kept),
  adds a designator and a comment text, and moves the part with the same
  code as move_component. 3D bodies are not copied (reported). A footprint
  not found in the given sources is reported and not placed.
- The removed-part case from the brief: removing a two-pin part and merging
  its nets yields one gap at the part's position with both islands' pads
  named (test on the fixture: 0.125 mm, R10C). KiCad reads the result.
- PcbLib reader checked on all four fixture libraries; a board with a
  library footprint placed bottom-side and rotated, a copied footprint and a
  swapped one passes the integrity check and reads in KiCad with the
  expected footprint, pad and via counts.
- Not supported (raises): hierarchical schematics (sheet symbols), binary
  schematic records (a newer pin format). Not handled: multi-channel
  designs, net classes from the schematic, parameters other than Comment.

## WP7 notes (power copper)

New: `copper.py` (planner), `thermal.py`, `power.py`. Changed: `pdn.py`.

- **PDN fixes found on the way.** Current density used `np.gradient`, which
  returns NaN beside a copper edge; those cells were set to 0, so a track
  one or two cells wide read **zero** density and the edge cells of every
  neck read low. Density now comes from the faces a cell shares with copper
  neighbours (both faces inside, the one face on an edge). The solver also
  returns a per-cell dissipation map (`maps[layer]["p"]`, each link's I²R
  split between its ends; via barrels included); it sums to the solver's
  power within 0.1 %, and the thermal model uses it.
- **Planner** (`copper plan.json`): free room = where a region of the net
  may go (board minus clearance voids, keep-outs, the plan's area and
  keep-outs; other nets' pours respected unless `respect_pours: false`).
  Pieces of free room inside a corridor form a graph: a piece touching a
  terminal, or overlapping a piece on another layer where a via fits. Kept:
  pieces on source-to-sink paths and pieces a terminal touches; dead ends
  are dropped (they would float). Vias: arrays at both terminals
  (`size_vias` for the layer share), a full-current array on each layer
  change of a breadth-first tree from the sources, and a cluster at any
  neck that stops improving (so current changes layer around a fence).
  Vias stay out of pads unless `"via": {"in_pad": true}`. Density is the
  worst 1 mm window average (`density_window_mm`), not the singular corner
  cell; both are reported. If the free room joins nothing, the corridor
  grows by doubling until the room stops growing; the error then gives the
  closest approach between the two sides and the object blocking it.
  Exit 1 when a target is missed; the report says which (density, drop,
  rise, via current over its IPC-2221 figure).
- **Synthetic results.** Test board (80 x 50 mm, 2 oz, 2 layers, a
  connector to a FET drain past a top fence and a bottom fence), 30 A,
  targets 40 A/mm² and 30 mV: met in 10 iterations, 3.1 s, 37.3 A/mm²,
  16.3 mV, 60 vias; the plan applies with no DRC violation (test). Without
  neck clusters the same board stalls at 49.5 A/mm² at a fence tip.
- **Large board.** `synth.stress_board()` (120 x 100 mm, 3,612 tracks, 500
  arcs, 480 pads, 300 vias, 48 polygons), VIN 30 A from one header pin to
  another: 32 s for 12 iterations plus the thermal estimate (was 136 s
  before profiling; candidate building and via placement were the cost).
  Not met, correctly: the sink pin is boxed in by its neighbours; the
  report names J2 pin 3, 0.25 mm from the worst neck. The applied plan adds
  no DRC violation (0 new; the random board has 7,455 of its own).
- **Thermal** (`thermal.py`): per copper layer one node per cell (copper
  sheet plus half of each adjacent dielectric), dielectric between layers,
  barrels, natural convection (horizontal plate correlations, both faces)
  plus linearised radiation, iterated on the rise. A powered component is a
  package node tied to its pad cells through 75 µm solder joints; its
  figure is the case (add P x Rth(j-c)). Before that change a part's watts
  were spread over all its pad cells and a small isolated gate pad heated
  unphysically. Against published IPC-2152 figures (`--ipc-check`, res
  0.5 mm):

  | Case | Model | Published | Ratio |
  |---|---|---|---|
  | Universal chart: 10 A, 520 sq mil, 3 oz, 0.070" polyimide | 19.1 °C | 20 °C | 0.96 |
  | Olson worked example: 10 A, 180 mil 1 oz external, planes below | 12.5 °C | 20 °C | 0.63 |
  | Conservative chart 4 A, 300 sq mil (upper bound) | 5.4 °C | ≤ 10 °C | ok |
  | Conservative chart 10 A, 680 sq mil (upper bound) | 10.0 °C | ≤ 20 °C | ok |

  So it can read up to ~40 % cool; use it to compare layouts and add margin
  for absolute numbers. Not modelled: heat leaving through connectors,
  leads and screws, enclosures, forced air, transients.
- **Via sizer.** 30 A, 0.3 mm hole, 25 µm plating, 1.6 mm board: 0.95 A per
  via at a 10 °C rise (barrel as an IPC-2221 internal conductor), so 20 vias
  for a half-current layer share or 40 for a full transfer, with a 1.25x
  margin.
- **Fuse check.** Onderdonk I²t of the narrowest copper downstream (from a
  PDN solve) against the fuse's I²t, plus the hold current against the
  IPC-2221 rating of that neck (10 mil 1 oz: 2.57 A for 1 s, a hand
  evaluation of Onderdonk's formula). **FET screen**: density in a 0.4 mm ring round each
  power-FET pad (FETs found by designator/pattern); warn 50, fail 100
  A/mm² are screening values, not a standard.
- **Accuracy limit.** A cell is copper when its centre is, so a neck reads
  within one cell of its width (a 0.5 mm track at 0.1 mm reads 25 % high
  when its edges sit on cell centres). Use res at a tenth of the narrowest
  neck for ~10 %. Fractional cell coverage would fix it; not done.

## UNVERIFIED (needs Altium)

Run these on the Altium PC, in order. Each says what passing looks like.

1. **Sandbox still compiles after the template change.** Restart the MCP
   server, then call `run_altium_script` with
   `SandboxLog('hello'); ResultText := '{"ok": true}';`.
   Pass: `success: true`, `result` is `{"ok": true}`. Fail: "script never
   started" means the template no longer compiles; `git diff
   server/SandboxScript/Sandbox.pas` shows what changed.
2. **Preflight refuses without launching.** `run_altium_script` with
   `Result := 'x';`. Pass: returns at once with `preflight rejected the
   script`, and Altium shows no activity.
3. **Process check.** Close Altium, call `run_altium_script` with the body
   from step 1. Pass: `blocked: not_running` and Altium does **not** start.
   Fail: Altium starts; then `tasklist /FI "IMAGENAME eq X2.EXE" /FO CSV
   /NH` output is not what `parse_tasklist` expects; paste it into the report.
4. **Wedge, refuse, recover.** Start Altium, open any board. Call
   `run_altium_script` with `SandboxLog('before'); Obj1 := nil;
   S1 := Obj1.Name; ResultText := 'unreachable';`. Pass: `executor_wedged:
   true`, `last_step_reached: before`. Then call it again with the step-1 body.
   Pass: returns in under a second with `blocked: wedged`. Then call
   `recover_altium_script_engine`. Pass: `recovered: true`, method
   `EditScript:Stop`; the step-1 body then succeeds. If method is `Ctrl+F3`,
   the Stop dispatch alone did not work; report that. If `recovered: false`,
   paste its `steps`.
5. **Ctrl+F3 fallback safety.** Only if step 4 needed it: confirm the keys
   went to a window titled with `Sandbox.pas`, never a PCB.
6. **Hung detection.** Optional: run a body with a long loop
   (`for I1 := 0 to 200000000 do I2 := I1;`), and while it runs call
   `python dev/unwedge.py --status`. Pass: `state: hung` while it runs.
7. Assumption to confirm: a parked `Altium_API` (bridge) script blocks the
   sandbox project as well, so a sandbox probe proves the whole engine runs.
   If a bridge tool times out, `recover_altium_script_engine` reports
   recovered, and the next bridge tool still times out, this is false.
8. **Altium opens what the writer writes.** Copy any board you can lose to a
   local folder (not N:), then
   `python dev\validate_pcbwrite.py C:\scratch\copy\board.PcbDoc --out C:\scratch\pcbwrite_check`.
   Open the files it lists, in order, with File > Open (not from a
   project); for each, note OPENED (no dialog), DIALOG (paste its text) or
   WRONG (opened but the printed check line is false). Close without
   saving. Pass: every file OPENED and every check holds, including
   `step99_all`. On failure: the first DIALOG/WRONG names one edit;
   `steps.json` has the targets. Send the step name, the dialog text and,
   if possible, the file; the suspect is that edit's storage in
   `pcbwrite.py`/`edits.py` (PCBDOC_FORMAT.md says what each touches).
9. **Altium agrees after a repour and DRC.** On `step99_all`: Tools >
   Polygon Pours > Repour All, then Tools > Design Rule Check. Pass: no
   crash, the pours fill, and DRC reports only violations explained by the
   test edits (the added track/via/fill/arc/pad/region on one net, the moved
   and flipped parts, the removed part's now-unconnected nets).
10. **Live scripts.** Open a scratch copy of a board in Altium. Write a plan
    with one `add_track` on a known net and one `rename_net`, then
    `python -m offline edit board.PcbDoc plan.json --live C:\scratch\live --board-path "<the path Altium shows>"`.
    Run each `.pas` body in `manifest.json` order through
    `run_altium_script`. Pass: each returns its manifest `expect` (e.g.
    `{"created": 1}`), the track appears, the net is renamed, and
    `999_save.pas` saves. Bodies with `"proven": false` are the ones to
    watch; if one fails, paste its result and the body.
11. **Altium reads pours written offline.** On a copy of a board:
    `python -m offline pour C:\scratch\copy\board.PcbDoc -o C:\scratch\poured.PcbDoc --store pour`.
    Open `poured.PcbDoc`. Pass: no dialog; the pours look like the
    original's; Tools > Polygon Pours > Repour All changes little (compare
    with `python -m offline render`). Fail: paste the dialog text.
12. **Solid store.** Same with `--store solid`. Pass: the PCB panel lists no
    polygons, the copper is there as regions on the right nets, DRC shows no
    new clearance errors.
13. **Dent fix.** On a board where `python -m offline pour board.PcbDoc
    --dents` lists dents: `--store unpoured -o C:\scratch\fixed.PcbDoc`,
    open, Repour All, save as `after.PcbDoc`, then `python -m offline pour
    after.PcbDoc --mode saved --compare`. Pass: no dents left and IoU close
    to the fixture figures above.
14. **Offline DRC agrees with Altium's.** Follow the steps at the top of
    `dev/compare_drc.py` on a board you can lose (its own DRC run, then
    `get_drc_violations` with max_violations=100000 saved as JSON, then
    `python -m offline drc ... --json --copper saved`). Pass: `compare_drc`
    shows at least 90 % agreement for Clearance, Width, HoleSize,
    MinimumAnnularRing, HoleToHoleClearance and BoardOutlineClearance, and
    the differences listed are explained (approximate kinds, text). Send the
    printed table and the "only offline" / "only Altium" lists.
15. **ECO results open and Update PCB agrees.** On a copy of a project:
    change the schematic (remove a part, rename a net), then
    `python -m offline eco apply X.PrjPcb X.PcbDoc --lib <its PcbLib> -o C:\scratch\eco.PcbDoc`
    and open the result in the project in place of the board. Pass: no
    dialog; Design > Update PCB Document shows no changes for the edits made
    (only comment changes, which apply leaves alone); placed footprints look
    right and carry their nets. Also run `eco link` on a board whose parts
    were re-symbolled and check Update PCB no longer offers to remove and
    re-add them.

16. **Planned copper opens and passes DRC.** On a copy of a board with a
    power path: write a plan.json for it (net, source and sink pads,
    current, targets), then
    `python -m offline copper plan.json C:\scratch\b.PcbDoc -o C:\scratch\plan_out.json --png C:\scratch\copper.png`
    and `python -m offline edit C:\scratch\b.PcbDoc C:\scratch\plan_out.json -o C:\scratch\planned.PcbDoc`.
    Open planned.PcbDoc in Altium, Tools > Polygon Pours > Repour All, then
    run DRC. Pass: no dialog on open; the new regions and vias are on the
    net (PCB panel); the copper matches copper.png; DRC shows no Clearance
    or Short-Circuit violation that the original board did not have. On
    failure: send the DRC report and the planner's printed report (not the
    board).
17. **Thermal estimate against a real board** (optional; needs hardware,
    not Altium). On a built board with a known steady load, measure the
    hottest copper (IR camera or thermocouple, 30 min at constant current,
    still air) and run `python -m offline thermal board.PcbDoc --flow ...
    --power ...` with the same current and part losses. Pass: within 30 %
    of the measured rise. If it reads low, keep that ratio as a margin.

## Decisions made without asking

- Work from `origin/main` + eager-ptolemy because phase-6 is missing (as the
  brief says).
- The bridge still starts Altium when it is not running (old behaviour, kept
  deliberately); only `run_altium_script` and the recovery path refuse a dead
  Altium.
- `GetCurrentPCBBoard` in a sandbox body is an error (overridable), because
  the brief records it wedging Altium in the sandbox.
- New primitives get a fresh PrimitiveGuids entry (random GUID) and, for
  pads, a new random 8-letter unique id, as Altium writes them.
- `remove_component` removes the vias a component owns (field @7) as well;
  `keep_copper` keeps its free-standing tracks, arcs, fills and vias as free
  copper.
- Offline `merge_nets` deletes the emptied source nets; the live script
  leaves them (deleting a net live is not proven).
- `merge_polygons` follows the brief: when either polygon overlaps another
  net's polygon the one with the higher pour index survives (keeping its
  settings), otherwise the first named. Its report names the neighbours
  whose pour order may change.
- The sandbox template was not changed to add a `TPolySegment` variable.
- The pour engine follows the board's PolygonConnect rule even where the
  saved pours disagree (Fastino's SMD pads); it reports the measured
  difference instead of copying the saved behaviour.
- DRC: touching no-net copper counts as joined; text is left out unless
  asked for; component clearance uses body outlines and is marked
  approximate; DFM presets are added on top of the board's own rules, never
  instead of them.
- ECO: parts match by designator; a multi-unit part is linked if the board
  names any unit's unique id; `eco link` writes the lowest unit's id.
- Placed and swapped parts do not get 3D bodies; their models live in the
  library, so Altium's own Update from Libraries is the way to add them.
- Power copper: density targets are judged on a 1 mm window average (a
  corner cell is singular and would never meet any target); the corner
  peak is reported beside it. The planner never puts vias in pads unless
  asked, drops copper that would float, and prefers more vias to a missed
  target (60 on the test board); prune by hand if drill count matters.
- Thermal: components are package nodes (case temperature), not watts
  smeared over their pads.
