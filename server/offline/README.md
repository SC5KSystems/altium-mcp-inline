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
    python -m offline review-power board.PcbDoc --current VBAT=30   # joins, vias, necks, edge
    python -m offline notches board.PcbDoc -o merge.json      # same-net pours that nearly meet
    python -m offline stack base.PcbDoc top.PcbDoc --map J7:J1 # boards joined by headers
    python -m offline blank template.PcbDoc -o new.PcbDoc --rect 100 80 --hole 4 4 3.2
    python -m offline assembly board.PcbDoc -o assembly.html --bom bom.csv
    python -m offline fab-check board.PcbDoc gerbers/         # Gerber/drill read-back
    python -m offline models3d board.PcbDoc                   # embedded STEP sanity
    python -m offline membership-audit --board-path "C:\b\x.PcbDoc" -o audit.pas  # live script
    python -m offline example -o folder                       # the worked example below
    python -m offline holes board.PcbDoc --mounting           # holes and edge distances
    python -m offline fasteners board.PcbDoc --screw M3-pan   # screw heads vs edge and copper
    python -m offline edges board.PcbDoc                      # parts and silkscreen at the edge
    python -m offline rule-scopes board.PcbDoc                # rules naming missing classes
    python -m offline pad-ties board.PcbDoc --nets GND        # how pads really join their pours
    python -m offline neck board.PcbDoc --net VBAT --src J1 --dst Q1.D
    python -m offline sch-check project.PrjPcb                # schematic problems
    python -m offline sch-compare old.PrjPcb new.PrjPcb       # what a schematic edit changed
    python -m offline sch-footprints project.PrjPcb           # before Update PCB
    python -m offline sch-netlist|sch-part|sch-bom|sch-rules project.PrjPcb ...
    python -m offline ripple --r 1 --l 0.0005 --f 2000        # R-L load current under PWM
    python -m offline profet --amps 8 --pwm 200               # PROFET+2 channel, datasheet values

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
| `review.py` | power layout review: relief joins, vias per amp, necks, edge, test points, polarity |
| `notch.py` | same-net pours that nearly meet: seam, notches, merge plan |
| `stack.py` | two boards joined by mating headers: fit, pin 1, mirroring, holes, nets, heights |
| `blank.py` | a new empty board from a template (outline, mounting holes) |
| `assembly.py` | one-file interactive hand-assembly page from the board and a BOM |
| `gerber.py` | Gerber/Excellon read-back against the board, fab readiness |
| `models3d.py` | embedded STEP models against their footprints |
| `audit.py` | live script: primitives whose Net disagrees with their net's member group |
| `example.py` | the worked example: a synthetic board through ECO, copper, pour, DRC, render |
| `mech.py` | holes and edge distances, screw heads against edge and copper, parts and silkscreen at the edge |
| `padties.py` | how each pad joins its pour, measured from the poured copper, against the rule |
| `neck.py` | narrowest copper on the widest route between two pad groups |
| `schdoc.py`, `schnet.py`, `schtools.py` | schematic records, netlist with Altium's connectivity rules (checked live against Altium's compiled netlist), compare, BOM, part view, block rules |
| `fpcheck.py` | footprints and pins before Update PCB; the update previewed by unique ID |
| `calc.py` | PROFET+2 (datasheet values), MOSFET stage, freewheel diode, R-L PWM ripple, copper area for watts |
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
fresh pour where a polygon is unpoured or listed as stale. Dead-copper
removal keeps a piece only when it touches a same-net pad, via or free
copper; another polygon's copper does not keep it alive (as Altium's saved
pours show).

A board that embeds other boards (a panel) is reported as one: the embedded
boards' copper lives in their own files and is not analysed.

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
e.g. both sides of a removed part once its nets are merged. In place of the
project, a netlist `.json` works too (a schematic from another tool, or one
written by hand):

    {"parts": {"J1": {"footprint": "CONN2", "comment": "", "unique_id": "ABCDEFGH",
                      "pins": ["1", "2"]}},
     "nets": {"VBAT": ["J1.1", "Q1.D"], "GND": ["J1.2"]}}

A pin is `DES.PIN` or `[DES, PIN]`; pins named in nets join their part;
a pin on two nets or of an unlisted part is an error.

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

## Small tools

- `review-power` lists what a reviewer of a high-current board checks by
  eye: power pads joined to their pour by thermal reliefs (with the current
  density in the spokes), vias per amp against IPC-2221, the narrowest
  track per power net, parts closer than `--edge` to the outline, power
  nets with no test point, and polarized parts whose silkscreen is the same
  at both pins. Heuristics; each finding says what it measured.
- `notches` finds same-net pours on one layer within `--gap` of each other
  (what removing a part between them leaves), reports the seam and the
  notch tips, and with `-o` writes a plan that closes the seam and merges
  the pair (repour afterwards).
- `stack A B --map J7:J1` fits B's mating pins onto A's (rotation and
  shift), says when only a mirrored fit or a turned connector lines up,
  checks mounting holes after the fit, maps pin-to-pin nets (ground or power
  against something else is a problem) and compares the connectors' body
  heights with the tallest parts between the boards. `--facing` when B is
  upside down.
- `blank` keeps a template's stack, rules, classes (emptied) and settings,
  removes every component, net, polygon and primitive, sets the outline in
  both places Altium keeps it (Board6 vertices and the board region) and
  adds mounting holes. Embedded fonts and models stay.
- `assembly` writes one HTML file with nothing fetched at run time: both
  sides of the board (the bottom mirrored as held), the BOM grouped by value
  and footprint, click to light a line's parts, ticks kept in the browser.
- `fab-check` reads a Gerber/drill folder (X2 file functions, else Altium's
  extensions), lists layers the board needs but the set lacks, compares hole
  sizes and counts, finds the files' origin and orientation from the holes
  and checks every hit lands on a board hole.
- `models3d` reads each embedded STEP (zlib in `Models/N`, or
  `Library/Models/N` in a PcbLib), resolves its length unit, and checks the
  placed model against its pads: standing on end (with the quarter turn and
  lift that lay it flat), pads it does not cover (from the body's own
  projected outline; a STEP assembly's raw points are in its parts' frames),
  and heights against the footprint's package class and the declared height.
- `membership-audit` writes a live script (it cannot be seen offline): every
  primitive in a net's member group whose Net is another, and every
  primitive whose Net is set but which is not in that group; `--fix`
  repairs the groups. `dev/probe_processes.py` tries candidate processes for
  a dialog-free DRC or Update PCB, one per run.

## Ported from the phase-6 branch

The phase-6 branch (98b87ff) had its own, simpler reader. Its tools were
moved onto this reader rather than kept beside it: `mech.py`, `padties.py`,
`neck.py`, `fpcheck.py` and the scope check in `rules.py` are rewrites on
shapely and this reader; the schematic modules and `calc.py` came across
as they were. Two schematic readers now live here: `sch.py` (used by the
ECO) and `schdoc`/`schnet` (the `sch-*` tools, checked live against
Altium's compiled netlist). Choosing one is left for later.

- `pad-ties` samples a ring half the rule's air gap outside each pad and
  classes the join as direct, direct_partial (merged, with part of the edge
  cleared round another net), relief (spoke count and widths) or none.
- `neck` reports widths to about one raster step (0.1 mm by default).
- `sch-footprints` checks a library footprint against the board's copy when
  the project's own PcbLibs lack it (a warning), and lists libraries it
  cannot read instead of skipping them.

## MCP tools

`server/offline_tools.py` makes these tools available to the MCP client
next to the Altium ones (`main.py` registers them; `ALTIUM_MCP_OFFLINE_TOOLS=0`
leaves them out). They run in a worker thread and answer JSON with `ok`;
a failure answers `ok: false` and the error, never a partial result. Every
docstring says when to use the tool.

| Tool | Use it to |
|---|---|
| `offline_pcb_info` | learn a board file's nets, parts, layers and size (start here) |
| `offline_pcb_rules` | list its rules and the scopes the offline DRC cannot evaluate |
| `offline_pcb_connectivity` | find islands, open connections, shorts, dead copper |
| `offline_pcb_net_gaps` | find where a net's copper is apart (after an ECO) |
| `offline_pcb_render` | see the board, a net, a zoom, markers, current density or a diff (PNG shown inline) |
| `offline_pcb_drc` | check the board against its own rules (plus fab presets) |
| `offline_pcb_pdn` | voltage drop and current density of one net |
| `offline_pcb_review_power` | first screen of a power board |
| `offline_pcb_thermal` | hot spots from current and part losses (a screen) |
| `offline_pcb_via_array` | size vias for a current (no board needed) |
| `offline_pcb_fuse_check` | copper after a fuse against the fuse |
| `offline_pcb_fet_screen` | current density into FET pads |
| `offline_pcb_notches` | same-net pours that nearly meet, with a merge plan |
| `offline_pcb_stack_check` | two boards joined by mating headers |
| `offline_pcb_fab_check` | Gerber and drill files against the board |
| `offline_pcb_models3d` | embedded STEP models against their footprints |
| `offline_pcb_apply_plan` | apply an EditPlan and write a new board (or dry run) |
| `offline_pcb_live_scripts` | the same plan as sandbox scripts for the open board |
| `offline_pcb_eco_preview`, `offline_pcb_eco_apply` | bring a board in line with its schematic |
| `offline_pcb_copper_plan` | plan power copper for a current |
| `offline_pcb_repour` | pour polygons, compare with the saved pours, write |
| `offline_pcb_autoroute` | route open signal connections with Freerouting |
| `offline_pcb_blank_board` | start a new board from a template |
| `offline_pcb_assembly_page` | write a hand-assembly page |
| `offline_pcb_membership_audit_script` | a live script for stale net membership |
| `offline_pcb_holes`, `offline_pcb_fasteners`, `offline_pcb_edges` | holes, screw heads, parts and silkscreen at the edge |
| `offline_pcb_rules_check` | rule scopes naming a missing or empty class, net, part or room |
| `offline_pcb_pad_ties` | how pads really join their pours, against the rule |
| `offline_pcb_neck` | the bottleneck of a power path, per layer |
| `offline_sch_netlist`, `offline_sch_check`, `offline_sch_part`, `offline_sch_bom` | read and check a schematic |
| `offline_sch_compare` | what changed between two netlists (schematic, board or Altium's dump) |
| `offline_sch_rules` | check repeated circuit blocks against rules |
| `offline_sch_footprints` | footprints and pins before Update PCB, with the update previewed |
| `calc_pwm_ripple`, `calc_profet`, `calc_profet_network`, `calc_mosfet_stage` | power-stage calculators |

Paths must be absolute (a relative one would resolve against the server's
own working folder). Writers take an output path and refuse to write onto
an input, onto the N: drive, or over an existing Altium design file unless
`overwrite` is true.
The offline requirements must be installed in the server's own Python
(`server\.venv\Scripts\python.exe -m pip install -r server\offline\requirements.txt`);
without them the tools load but each one answers with that install line.
If the module cannot load at all, a single `offline_pcb_unavailable` tool
says why.

## Worked example

`python -m offline example -o folder` builds a two-layer 2 oz board (made
up: J1 feeds VIN, R2 links VIN to VBAT, Q1 switches VBAT to VOUT at J2, U1
drives the gate, C1 decouples, GND is a bottom pour, and the VBAT track to
Q1 is 0.6 mm) and runs the session below through the same functions the MCP
tools are. The calls, as a model would make them (paths shortened):

1. Load: `offline_pcb_info(board="input.PcbDoc")`: 5 nets, 6 parts, 60 x 40 mm.
2. ECO: the schematic drops R2 and joins VIN into VBAT (`schematic_netlist.json`).
   `offline_pcb_eco_preview(schematic="schematic_netlist.json", board="input.PcbDoc")`
   says remove R2, merge VIN into VBAT;
   `offline_pcb_eco_apply(..., out="eco.PcbDoc", plan_out="eco_plan.json")`
   writes it (2 ops: `remove_component`, `merge_nets`).
3. Gap list: `offline_pcb_net_gaps(board="eco.PcbDoc", nets=["VBAT"])`: one
   gap, 4.85 mm, where R2 was.
4. Copper plan: `offline_pcb_copper_plan(board="eco.PcbDoc", plan={"net": "VBAT",
   "sources": ["J1.1"], "sinks": ["Q1.D"], "current": 30, "targets":
   {"density_a_per_mm2": 40, "drop_mv": 30}, "res": 0.25}, plan_out="copper_plan.json")`:
   targets met, 18 mV, 38 A/mm2 at the worst 1 mm window, 20 vias; then
   `offline_pcb_apply_plan(board="eco.PcbDoc", plan="copper_plan.json", out="planned.PcbDoc")`.
5. Pour: `offline_pcb_repour(board="planned.PcbDoc", out="final.PcbDoc")`: the
   GND pour flows around the new copper in one piece.
6. DRC: `offline_pcb_drc(board="final.PcbDoc", copper="saved")`: 0 violations;
   `offline_pcb_connectivity`: nothing open, no shorts.
7. Render: `offline_pcb_render` of the input, the result (VBAT highlighted)
   and `diff=` between them.
8. Write: `final.PcbDoc` is the result. In Altium: open it, Repour All, run
   the DRC (the offline result is not proof Altium agrees).

The run takes a few seconds; `server/tests/test_offline_mcp.py` runs it and
checks the story (the two ECO ops, the one 4.85 mm gap, targets met, a clean
DRC and connectivity, inputs never written).

## Acceptance on the Altium PC

`dev/live_validate_tools.py` runs, in one command, every check that needs
Altium, safest first, on a copy of a board you name:

    server\.venv\Scripts\python.exe dev\live_validate_tools.py C:\boards\x.PcbDoc
        [--work C:\scratch\offline_check] [--project x.PrjPcb] [--gerbers outputs]
        [--power VBAT:30:J1.1>Q1.D] [--wedge] [--list] [--only STEP] [--no-ask]

It never writes the named board (it checks its hash at the end) or the N:
drive. Steps: requirements and registration; read and time the copy; write
the test files (writer steps, pours stored three ways, blank board, live
scripts, audit body, and when possible a copper plan, an autorouted copy, an
ECO preview and a fab check); a script refused before Altium sees it; with
Altium closed, a refused script that does not start Altium; a sandbox body;
you open each written file and answer y/n; Altium's Repour All against the
offline pour (IoU); the membership audit on the open copy; offline DRC
against Altium's markers (90 % agreement); live scripts that edit and save
the copy, read back offline; and with `--wedge` a deliberate wedge and
recovery. It prints PASS / FAIL / SKIP / MANUAL per step and saves
`results.json`.
