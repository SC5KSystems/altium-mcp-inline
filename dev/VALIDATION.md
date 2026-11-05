# Validation status (live checks from the phase-6 branch)

Carried over from the phase-6 branch (98b87ff), with the design it ran on left
unnamed: the repo is public and holds no real design data. Those runs used
that branch's own offline reader. This branch kept its own reader and ported
the harness to it (`dev/validate_offline_vs_altium.py`), so the comparison
must be run again here (dev/CLOUD_HANDOFF.md, UNVERIFIED item 25).

Last live run: 2026-10-20, Altium Designer 24.10.1, a real two-layer board
project open. The live MCP server was running the `main` checkout, not that
branch, so only the arithmetic and enum findings below were checked live,
through `run_altium_script`.

## 1. Baseline - done 2026-10-20

The bridge works. Nine read tools were called once each (`get_pcb_layer_stackup`,
`get_track_data`, `get_vias`, `get_net_geometry_summary`, `get_polygon_data`,
`get_pad_data`, `get_pcb_rules_parsed`, `get_all_nets`, `get_object_classes`) and
`python dev/validate_offline_vs_altium.py <board.PcbDoc>` gave 23 PASS and 2 FAIL out
of 25. Both failures are the stackup bug in section 2. Net, pad, track, via,
polygon, rule and class counts from live Altium matched the offline reader.

To repeat: save and leave the board open, call the nine tools, run the harness,
then `python dev/validation_ledger.py --log server/altium_mcp.log`. After that run
21 of 45 dispatcher commands had log evidence and 24 had none. The 24 include the
schematic reads and builds (`get_schematic_data`, `get_component_pins`,
`search_library_symbol`, `build_circuit`, `place_components`), symbol and footprint
creation, and output jobs.

## 2. Discrepancies found by the baseline

| item | what was seen | status |
|---|---|---|
| `get_pcb_layer_stackup` micron values | copper reported as 108.5 um for 2.7559 mil (true value 70 um): the divisor was 254, should be 10000/25.4 | Root cause confirmed live: the corrected arithmetic on the live stack gives 69.99986 um copper and 320.04 um dielectric. Fixed in `pcb_utils.pas`, **not yet deployed**; once the server runs this code the two stackup lines of the harness should pass. |
| `get_polygon_data` `pour_over` | the tool reported `false` for every polygon. In the saved file every polygon but one had `POUROVER=TRUE`, `POUROVERSTYLE=1`; the one (a power net's bottom-layer pour) had `POUROVER=FALSE`, `POUROVERSTYLE=2` | Resolved. `Poly.PourOver` is an integer enum equal to `POUROVERSTYLE`; live Altium returned 1 for the others and 2 for that one, and `AddJSONBoolean` printed false for both. The tool now emits `pour_over_style` and the harness compares it. **Deployed check pending.** |
| `get_net_geometry_summary` `pad_connections` | reports the rule Altium resolves per pad, not how the pour was built. Saved copper showed pads whose rule says relief but which merge directly | The live tool reported Relief for two power nets' pads, which agreed with the offline scope evaluator (the Direct-connect rule was scoped to a class with no members: `python -m offline rule-scopes` finds such rules). Still open: whether the live tool should also report measured ties, as `python -m offline pad-ties` does offline. |

## 2b. Schematic reader - validated 2026-10-20

`dev/exp_compiled_netlist.pas` (read-only; it compiles the project, which changes
no document) dumped Altium's compiled netlist of a multi-sheet project.
`offline_sch_compare` against the `.PrjPcb` read from the saved files: every net
identical, automatic names included, and every pin. The first comparison found
GND split in two, because a GND net label and a GND power port were not joined;
fixed and covered by a test. The same project against its `.PcbDoc`: identical on
every pin that has a pad with a net (the rest are unconnected pins).

The schematic modules (`schdoc.py`, `schnet.py`, `schtools.py`) came across
unchanged apart from reading boards with this branch's reader, so this result
still describes them. On KiCad's public eDP fixture the schematic and its board
also agree pin for pin (`test_offline_schematic.py`).

The sandbox declares `List1`/`List2` without creating them; the first run of the
dump stalled on `List1.Clear` and was recovered with `dev/unwedge.py`. The tool
docstring and dev/README say so.

## 3. Altium-side experiment bodies written but not run

In `dev/`, for `dev/sandbox_runner.py`. Both start read-only or in dry-run mode.

- `exp_board_outline_dump.pas` - reads the board outline through the API. Settled
  2026-10-20 by an equivalent read-only probe:
  `BoardOutline.Segments[i].Kind/vx/vy/cx/cy/Radius/Angle1/Angle2` all read on
  Altium 24.10.1 without stalling. `PointCount` had no closing repeat, and every
  vertex, arc centre, radius and angle matched the offline reader exactly.
- `exp_move_free_pad.pas` - moves one mounting hole (a pad that belongs to no
  component). Set `DRY_RUN := False` only on a scratch copy of the board.

## 4. Board outline and primitive editing

The phase-6 branch did not edit the outline or the pours. Editing is this
branch's work (WP3 onward); its live checks are in dev/CLOUD_HANDOFF.md under
UNVERIFIED.
