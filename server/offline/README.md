# offline - analysis of saved Altium files, no Altium or license needed

Reads a `.PcbDoc`, `.SchDoc` or `.PrjPcb` directly (the documents are OLE2 containers) and
answers design-review questions from the files. Stdlib only, except `pdn.py` which needs numpy.

```
cd server
python -m offline summary  board.PcbDoc
python -m offline holes    board.PcbDoc --mounting
python -m offline fasteners board.PcbDoc --screw M4-pan --washer
python -m offline edges    board.PcbDoc
python -m offline rules    board.PcbDoc                 # scopes that reference missing things
python -m offline connect  board.PcbDoc --nets "VBAT,VOUT"
python -m offline neck     board.PcbDoc --net "VBAT" --src J1 --dst U1.2        # numpy
python -m offline solve    board.PcbDoc --net "VBAT" --src J1 --dst U1.2 --amps 50  # numpy
python -m offline ripple   --r 0.8429 --l 0.0005511 --f 2500 --duty 0.5
python -m offline profet   --part BTS7008-1EPP --amps 10 --pwm 100

python -m offline sch-check   project.PrjPcb               # unconnected pins, floating labels ...
python -m offline sch-netlist project.PrjPcb --net FAN
python -m offline sch-part    project.PrjPcb U3            # pins, nets, neighbours
python -m offline sch-bom     project.PrjPcb               # with MPN search terms
python -m offline sch-rules   project.PrjPcb rules.json    # circuit-block checks
python -m offline sch-compare before.PrjPcb after.PrjPcb   # or a .PcbDoc, or a compiled netlist
```

The same analyses without numpy are MCP tools (`offline_pcb_*`, `offline_sch_*`, `calc_*`)
registered by `server/offline_tools.py` and `server/offline_sch_tools.py`.

## What each piece is

| file | does |
|---|---|
| `cfb.py` | OLE2 compound-file reader |
| `pcbdoc.py` | parses board, stackup, nets, classes, rules, components, pads, vias, tracks, arcs, fills, polygons, pour regions, 3D bodies |
| `scope.py` | evaluates rule scopes such as `(InNetClass('Power')) or IsVia` (a parser, not `eval`) |
| `review.py` | summary, holes, fasteners, edge items, outline clearance, dead rule scopes |
| `connect.py` | how each pad really joins its pour (direct or N spokes of W mm), measured from the copper |
| `raster.py`, `pdn.py` | per-net copper raster; narrowest neck between pads; DC resistance, drop, power and hot spots |
| `power.py` | PROFET datasheet values and thermal/PWM model; R-L PWM ripple; MOSFET loss; copper-area estimate |
| `render.py` | annotated SVG of the board |
| `schdoc.py` | parses a sheet: components, parameters, pins (electrical end), wires, junctions, net labels, power ports, ports, sheet symbols |
| `schnet.py` | connectivity within and across sheets, net naming, schematic findings, `.PrjPcb` sheet list and net scope |
| `schtools.py` | one netlist shape for every source (project, sheet, board, compiled dump); compare, BOM, part view, rule checks |

## How far to trust it

Validated against Altium's own API output for the same board (`dev/validate_offline_vs_altium.py`):
track, via, arc and polygon counts; track lengths to 0.01 mil; via positions; polygon vertex lists
and outline areas; pad positions, sizes and nets; net list; rule names and scopes; object-class
counts. It also reproduces all six Board Outline Clearance violations Altium reported.

Checked analytically (`tests/test_offline.py`): strip resistance equals rho*L/(W*t) within 2 %.

Schematic netlist: on a three-sheet project it matched Altium's compiled netlist exactly (71 nets,
327 pins, every net name including automatic `NetR1_2` names), dumped live with
`dev/exp_compiled_netlist.pas`. That comparison caught one bug, now fixed and tested: a net label
and a power port of the same name must join. `tests/test_schematic.py` pins down each connectivity
rule on synthetic sheets.

Found by testing, now fixed: polygon arcs are traversed in the direction the polygon is wound, not
always counter-clockwise (caught because a polygon's outline area disagreed with its poured copper).

Known limits:

- The file stores the pour Altium last computed, not what a repour would produce under today's rules.
- The pad-tie measurement samples a ring just outside the pad (about 0.02 mm step). Widths are good
  to that step, which is fine for 0.2 mm spokes.
- `pdn.solve_net` models sheet resistance, plated barrels and vias; it does not model solder, pad
  stack detail inside a component, or temperature rise feeding back into resistance.
- Neck width is the inscribed-circle diameter along the widest route, ignoring copper within
  `ignore_mm` of the terminals. IPC-2221 current from it is conservative for wide pours over a plane.
- `WithinRoom(...)` and numeric scope tests (`CompPinCount < 5`) are not evaluated; they are reported.
- Schematics: hierarchical designs (sheet symbols, sheet entries, ports), buses and harnesses are
  reported, not followed; the project tested is flat. A wire end landing inside another wire
  connects only through a junction, as in `dev/netlist.py`; each such spot without one is reported.
  Net Identifier Scope numbers other than 0 (Automatic) are unconfirmed.
- PROFET numbers come from two datasheets (see `power.PROFETS`); a part not in that table needs
  its own numbers. Junction-to-ambient defaults to the datasheet's 4-layer figure - pass your own.

## Open items that need a live Altium

See `dev/VALIDATION.md`.
