# Validation status

Last live run: 2026-10-20, Altium Designer Professional 24.10.1, project `PDM_2026-2027.PrjPcb` open,
board `PDM_2026_2027.PcbDoc`. The live MCP server was running the `main` checkout, not this branch, so
edits made on this branch have not been exercised by it yet. Only the arithmetic and enum findings below
were checked live, through `run_altium_script`.

## 1. Baseline - done 2026-10-20

The bridge works. Nine read tools were called once each (`get_pcb_layer_stackup`, `get_track_data`,
`get_vias`, `get_net_geometry_summary`, `get_polygon_data`, `get_pad_data`, `get_pcb_rules_parsed`,
`get_all_nets`, `get_object_classes`) and `python dev/validate_offline_vs_altium.py <board.PcbDoc>`
gave 23 PASS and 2 FAIL out of 25. Both failures are the stackup bug in section 2. Net, pad, track,
via, polygon, rule and class counts from live Altium match the offline reader.

To repeat: save and leave the board open, call the nine tools, run the harness, then
`python dev/validation_ledger.py --log server/altium_mcp.log`. After today 21 of 45 dispatcher commands
have log evidence and 24 have none. The 24 include the schematic reads and builds
(`get_schematic_data`, `get_component_pins`, `search_library_symbol`, `build_circuit`,
`place_components`), symbol and footprint creation, and output jobs.

## 2. Discrepancies found by the baseline

| item | what was seen | status |
|---|---|---|
| `get_pcb_layer_stackup` micron values | copper reported as 108.5 um for 2.7559 mil (true value 70 um): the divisor was 254, should be 10000/25.4 | Root cause confirmed live. The corrected arithmetic, applied to the live stack, gives 69.99986 um copper and 320.04 um dielectric. The fix is on this branch in `pcb_utils.pas` but **not yet deployed**; once the live server runs this branch the two stackup lines of the harness should PASS (25/25). |
| `get_polygon_data` `pour_over` | the tool reported `false` for every polygon. The saved file has 42 polygons with `POUROVER=TRUE`, `POUROVERSTYLE=1` and one (BATT+, bottom layer) with `POUROVER=FALSE`, `POUROVERSTYLE=2` | Resolved. `Poly.PourOver` is an integer enum equal to `POUROVERSTYLE`; live Altium returned 1 for the 42 and 2 for BATT+ bottom. `AddJSONBoolean` printed false for both. The branch now emits `pour_over_style` and the harness compares it. **Deployed check pending.** |
| `get_net_geometry_summary` `pad_connections` | reports the rule Altium resolves per pad, not how the pour was built. Saved copper showed pads whose rule says relief but which merge directly | The live tool reports Relief for the RECT+ and BATT+ pads, which agrees with the offline scope evaluator (the Direct-connect rule is scoped to a class with no members). Still open: whether the tool should also report measured ties, as offline `connect.py` does. |

## 2b. Schematic reader - validated 2026-10-20

`dev/exp_compiled_netlist.pas` (read-only; it compiles the project, which changes no document)
dumped Altium's compiled netlist of the three-sheet project. `offline_sch_compare` against
the `.PrjPcb` read from the saved files: 71 of 71 nets identical, 327 pins, every net name
including the automatic ones. The first comparison found GND split in two, because a GND net label
and a GND power port were not joined; fixed and covered by a test. The same project against its
`.PcbDoc`: identical on the 313 pins that have pads with nets (the other 14 are unconnected pins).

The sandbox declares `List1`/`List2` without creating them; the first run of the dump stalled on
`List1.Clear` and was recovered with `dev/unwedge.py`. The tool docstring and dev/README now say so.

## 3. Altium-side experiment bodies written but not run

In `dev/`, for `dev/sandbox_runner.py`. Both start read-only or in dry-run mode.

- `exp_board_outline_dump.pas` - reads the board outline through the API. Settled 2026-10-20 by an
  equivalent read-only probe: `BoardOutline.Segments[i].Kind/vx/vy/cx/cy/Radius/Angle1/Angle2` all read on
  Altium 24.10.1 without stalling. `PointCount` is 8 with no closing repeat, and every vertex, arc centre,
  radius and angle matched the offline reader exactly. This also answers the board-editing branch's
  "reading arc segments" unknown (section 4).
- `exp_move_free_pad.pas` - moves one mounting hole (a pad that belongs to no component).
  Set `DRY_RUN := False` only on a scratch copy of the board.

## 4. Board outline and primitive editing

This branch does not edit the outline or the pours. Another branch on the fork,
`origin/claude/eager-ptolemy-4w4c4m` (commit 09efd44, 2026-09-26), adds `get_board_outline`,
`set_board_outline`, `set_board_origin`, `create_pcb_primitives` and `delete_pcb_primitives`, with
`dev/BOARD_EDITING_TEST_PLAN.md` on that branch as the live test plan. Its offline tests exist; none of it
has run against Altium. The plan needs a scratch board (`mcp_scratch.PcbDoc`, never in a real project) and
a server started from that branch. This repo's history records that repouring could not be driven from
script; that branch tries polygon `Rebuild` in its test T10, so T10 is what settles it. Until then, repour in the editor.
