# Board editing tools - live test plan

Five new MCP tools need confirming against a running Altium before anyone uses
them on a real design:

| Tool | Writes? |
|---|---|
| `get_board_outline` | no |
| `set_board_outline` | yes - replaces the board shape |
| `set_board_origin` | yes - moves the origin (nothing on the board moves) |
| `create_pcb_primitives` | yes - adds objects |
| `delete_pcb_primitives` | only with `dry_run: false` |

The Python side is unit tested offline (`server/tests/test_board_geometry.py`)
and was exercised against a fake bridge. What is not yet known is how this
Altium build answers a few API calls that nothing in this repo had used before.
Each of those runs right after a logged step, so a failure names itself:

- writing the outline: `Outline.SetState_PointCount`, `SetState_Segments`,
  `TPolySegment` records
- reading arc segments: `cx`, `cy`, `Radius`, `Angle1`, `Angle2`
- moving the origin: `Board.XOrigin :=`
- polygons: `SetState_PolyHatchStyle`, `Rebuild`
- deleting: `Prim.InPolygon`, `Prim.InDimension`

Whoever runs this: work through the tests in order, and for each one report
what the "Report" line asks for. Steps marked **Manual** need a person at
Altium; an agent should list them in its report as not done.

## Setup

1. Close Altium completely.
2. Check out branch `claude/eager-ptolemy-4w4c4m` and point the MCP server at
   that checkout (or build and install a .dxt from it). Restart the MCP client
   so the five new tools load. `get_server_status` should show `script_found:
   true`.
3. Start Altium. Create a new PCB (File > New > PCB) and save it as
   `mcp_scratch.PcbDoc` in a local folder. Never on the N: drive, and not inside
   a real project.
4. Close every other PCB document. If the scratch board is not the active
   document, the tools fall back to the first PCB in the focused project.
5. Click the scratch board's tab so it is the active document. Do this again
   whenever another document has taken focus.
6. Every write call below passes `"expected_board": "mcp_scratch"`. With it,
   a tool refuses to touch any board whose file name does not contain that text.

Report: the Altium version (Help > About).

## If a call times out

The error names the last step the script logged, for example
`the last step it logged was: 'Outline.SetState_PointCount(8)'`.

1. Copy the whole error text into the report, along with the contents of
   `C:\Users\Public\altium_mcp\steps.txt`.
2. Run `python dev/unwedge.py` from the repo. It stops the stalled script and
   checks that scripts run again. The error text also gives the raw command
   (`"...\X2.EXE" -REditScript:Stop`).
3. If the failure was in a write, close the scratch board without saving and
   reopen it. A half-finished write can leave it inconsistent.
4. If the stalled step matches one in the appendix, run that probe and include
   its output.
5. Skip the tests that depend on the failed one and carry on with the rest.

## Tests

All lengths are mm unless a test says otherwise. Coordinates are relative to
the board origin.

### T1 - read the starting outline (read-only)

```json
get_board_outline {"units": "mm", "check_components": true}
```

Expect `success: true`, `board_file: "mcp_scratch.PcbDoc"`, and an outline
summary for the default board shape.

Report: the full JSON.

### T2 - exact rectangle

```json
set_board_outline {"units": "mm", "width": 100, "height": 80, "expected_board": "mcp_scratch"}
```

Expect `verified: true`, `requested.width` 100, `requested.height` 80,
`outline.summary.segment_count` 4. Then take `get_screenshot {"view_type": "pcb"}`:
the board should now be a 100 x 80 mm rectangle with its lower-left corner at
the origin.

Report: the full JSON and the screenshot.
**Manual:** press Ctrl+Z once in Altium and note whether the whole change
undoes. Then press Ctrl+Y (or re-run T2) to get the rectangle back.

### T3 - refuses the wrong board

```json
set_board_outline {"units": "mm", "width": 50, "height": 50, "expected_board": "definitely_not_this_board"}
```

Expect `success: false`, an error naming `mcp_scratch.PcbDoc`, and the board
unchanged.

Report: the error text.

### T4 - rounded corners (first arcs)

```json
set_board_outline {"units": "mm", "width": 100, "height": 80, "corner_radius": 3, "expected_board": "mcp_scratch"}
```

Expect `verified: true`, `outline.summary.arc_count` 4, and
`outline.summary.area` about 7992.27 (100 x 80 minus (4 - pi) x 3^2).

Report: the full JSON. The `outline.segments` array matters most here: it shows
how this Altium build stores arc segments.

### T5 - D-shaped board (an arc through 0 degrees)

```json
set_board_outline {"units": "mm", "expected_board": "mcp_scratch", "vertices": [
  {"x": 0, "y": 0},
  {"x": 40, "y": 0, "arc": {"cx": 40, "cy": 20}},
  {"x": 40, "y": 40},
  {"x": 0, "y": 40}]}
```

The right side is a half circle from 270 degrees through 0 to 90. Expect
`verified: true`, width 60, height 40, area about 2228.32 (1600 + pi x 20^2 / 2).

Report: the full JSON and a screenshot.

### T6 - put the previous shape back

Call `set_board_outline` with `units` "mm", `expected_board` "mcp_scratch", and
`vertices` copied exactly from T5's `previous_outline.vertices`.

Expect `verified: true` and the rounded 100 x 80 board from T4 again.

Report: the full JSON.

### T7 - move the origin to a point

```json
set_board_origin {"position": "point", "x": 10, "y": 5, "units": "mm", "expected_board": "mcp_scratch"}
```

Expect `verified: true` and `moved_by` {x: 10, y: 5}. Then
`get_board_outline {"units": "mm"}` should report the outline's bbox left at
-10 and bottom at -5.

Report: both JSON results.

### T8 - origin back to the board corner

```json
set_board_origin {"position": "lower_left", "expected_board": "mcp_scratch", "units": "mm"}
```

Expect `verified: true` and `moved_by` {x: -10, y: -5}. Then
`get_board_outline {"units": "mm"}` should report bbox left 0 and bottom 0.

Report: both JSON results.

### T9 - one of everything

```json
create_pcb_primitives {"units": "mm", "expected_board": "mcp_scratch", "primitives": [
  {"type": "track", "layer": "Mechanical 1", "x1": 5, "y1": 2, "x2": 95, "y2": 2, "width": 0.2},
  {"type": "track", "layer": "Keep Out Layer", "x1": 5, "y1": 75, "x2": 95, "y2": 75, "width": 0.2},
  {"type": "arc", "layer": "Top Overlay", "cx": 50, "cy": 40, "radius": 5, "start_angle": 0, "end_angle": 360, "width": 0.2},
  {"type": "via", "x": 20, "y": 20, "diameter": 0.6, "hole": 0.3},
  {"type": "fill", "layer": "Top Layer", "x1": 30, "y1": 10, "x2": 35, "y2": 15},
  {"type": "region", "layer": "Top Layer", "points": [{"x": 40, "y": 10}, {"x": 48, "y": 10}, {"x": 48, "y": 15}, {"x": 40, "y": 15}]},
  {"type": "region", "kind": "board_cutout", "points": [{"x": 60, "y": 30}, {"x": 70, "y": 30}, {"x": 70, "y": 40}, {"x": 60, "y": 40}]},
  {"type": "pad", "x": 4, "y": 4, "size": 3.2, "hole": 3.2, "plated": false, "name": "MH1"},
  {"type": "pad", "x": 10, "y": 70, "size": 1.0, "name": "FID1"},
  {"type": "text", "layer": "Top Overlay", "x": 10, "y": 60, "height": 2, "text": "MCP TEST"},
  {"type": "polygon", "layer": "Top Layer", "points": [{"x": 15, "y": 25}, {"x": 35, "y": 25}, {"x": 35, "y": 45}, {"x": 15, "y": 45}]}
]}
```

Expect `verified: true` and `total_created` 11: 2 tracks, 1 arc, 1 via,
1 fill, 2 regions, 2 pads, 1 text, 1 polygon. There should also be a
`next_steps` note to repour the unpoured polygon. Take a screenshot.

Report: the full JSON and the screenshot.
**Manual:**
- In the 3D view (press 3), the 10 x 10 mm cutout should be a hole through the
  board.
- MH1's properties should show a 3.2 mm hole with Plated unticked.
- The polygon appears as an outline until Tools > Polygon Pours > Repour All.
- Ctrl+Z once should remove all 11 objects together. Press Ctrl+Y to restore them.

### T10 - a polygon that pours

```json
create_pcb_primitives {"units": "mm", "expected_board": "mcp_scratch", "primitives": [
  {"type": "polygon", "layer": "Top Layer", "pour": true, "points": [{"x": 60, "y": 50}, {"x": 80, "y": 50}, {"x": 80, "y": 70}, {"x": 60, "y": 70}]}
]}
```

Expect `verified: true` and `polygons_poured` 1, with solid copper visible in a
screenshot.

Report: the full JSON and the screenshot.

### T11 - nothing is created from a bad batch

```json
create_pcb_primitives {"units": "mm", "expected_board": "mcp_scratch", "primitives": [
  {"type": "track", "layer": "Top Layer", "x1": 1, "y1": 1, "x2": 2, "y2": 1, "width": 0.2},
  {"type": "track", "layer": "No Such Layer", "x1": 1, "y1": 2, "x2": 2, "y2": 2, "width": 0.2},
  {"type": "via", "x": 3, "y": 3, "diameter": 0.6, "hole": 0.3, "net": "NO_SUCH_NET"}
]}
```

Expect `success: false`, an error starting "Nothing was created" that names
both the layer and the net, and no new track at (1, 1).

Report: the error text.

### T12 - cutouts and parts against the outline

```json
get_board_outline {"units": "mm", "check_components": true}
```

Expect `non_copper_regions` to include the T9 cutout, with bbox 60..70 x 30..40.

Report: the full JSON, especially the cutout's `kind` number. The tool assumes
3 means board cutout.
**Manual (optional):** draw a cutout by hand (Design > Board Shape > Define
Board Cutout), re-run this call, and report the kind number Altium gave it.

### T13 - delete, dry run

```json
delete_pcb_primitives {"object_types": ["track", "arc", "via", "fill", "region", "pad", "text", "polygon"],
  "within": {"x1": -1, "y1": -1, "x2": 101, "y2": 81}, "units": "mm", "expected_board": "mcp_scratch"}
```

Expect `dry_run: true`, `deleted` 0, and `matched` 12: the 11 objects from T9
plus the T10 polygon. The copper that polygon poured must not be listed
separately. The board must be unchanged.

Report: the full JSON. If `matched` is not 12, list what is extra or missing.

### T14 - delete for real

Run the same call as T13 with `"dry_run": false` added.

Expect `deleted` equal to T13's `matched`. Afterwards a screenshot should show
a bare board, and `get_board_outline` should report no `non_copper_regions`.

Report: both JSON results and the screenshot.
**Manual:** Ctrl+Z once should bring every deleted object back.

### T15 - nets (optional, needs a net)

A new board has no nets.
**Manual:** add one with Design > Netlist > Edit Nets > Add, named `GND`.
Then:

```json
create_pcb_primitives {"units": "mm", "expected_board": "mcp_scratch", "primitives": [
  {"type": "via", "x": 50, "y": 20, "diameter": 0.6, "hole": 0.3, "net": "GND"},
  {"type": "track", "layer": "Top Layer", "x1": 50, "y1": 20, "x2": 60, "y2": 20, "width": 0.3, "net": "GND"}]}
```

```json
get_vias {"net_names": ["GND"]}
```

```json
delete_pcb_primitives {"object_types": ["via", "track"], "net_names": ["GND"], "expected_board": "mcp_scratch"}
```

Expect the via and track created, `get_vias` to list the via, and the
delete dry run to match 2.

Note: `get_vias` reports absolute coordinates, not origin-relative ones.

Report: all three results.

## Appendix - probes for a stalled step

Run these with `run_altium_script`, with the scratch board active, only when a
test stalls at the matching step. Each probe changes nothing.

**Stalled at `reading arc fields cx, cy, Radius, Angle1, Angle2`** (the board
needs an arced outline, for example after T4):

```pascal
Obj1 := PCBServer.GetCurrentPCBBoard;
Obj2 := Obj1.BoardOutline;
SandboxLog('point count ' + IntToStr(Obj2.PointCount));
I1 := 0;
while (I1 < Obj2.PointCount - 1) and (Obj2.Segments[I1].Kind <> 1) do I1 := I1 + 1;
SandboxLog('segment ' + IntToStr(I1) + ' kind ' + IntToStr(Obj2.Segments[I1].Kind));
SandboxLog('vx ' + FloatToStr(CoordToMils(Obj2.Segments[I1].vx)));
SandboxLog('cx ' + FloatToStr(CoordToMils(Obj2.Segments[I1].cx)));
SandboxLog('cy ' + FloatToStr(CoordToMils(Obj2.Segments[I1].cy)));
SandboxLog('Radius ' + FloatToStr(CoordToMils(Obj2.Segments[I1].Radius)));
SandboxLog('Angle1 ' + FloatToStr(Obj2.Segments[I1].Angle1));
SandboxLog('Angle2 ' + FloatToStr(Obj2.Segments[I1].Angle2));
ResultText := 'arc fields read';
```

**Stalled at `assigning Board.XOrigin and Board.YOrigin`**. Probe A writes the
origin back to its own value:

```pascal
Obj1 := PCBServer.GetCurrentPCBBoard;
SandboxLog('XOrigin ' + IntToStr(Obj1.XOrigin));
SandboxLog('trying SetState_XOrigin');
Obj1.SetState_XOrigin(Obj1.XOrigin);
SandboxLog('SetState_XOrigin worked');
ResultText := 'ok';
```

**Stalled at `checking Prim.InPolygon` or `checking Prim.InDimension`**. Draw
one track on the board first:

```pascal
Obj1 := PCBServer.GetCurrentPCBBoard;
Obj2 := Obj1.BoardIterator_Create;
Obj2.AddFilter_ObjectSet(MkSet(eTrackObject));
Obj2.AddFilter_LayerSet(AllLayers);
Obj2.AddFilter_Method(eProcessAll);
Obj3 := Obj2.FirstPCBObject;
SandboxLog('have track: ' + BoolToStr(Obj3 <> nil, True));
SandboxLog('InComponent ' + BoolToStr(Obj3.InComponent, True));
SandboxLog('InPolygon ' + BoolToStr(Obj3.InPolygon, True));
SandboxLog('InDimension ' + BoolToStr(Obj3.InDimension, True));
Obj1.BoardIterator_Destroy(Obj2);
ResultText := 'flags read';
```

## Report template

```
Altium version:
T1: <result JSON or error>
T2: <result JSON> | screenshot | manual Ctrl+Z: <whole change undone? yes/no/not done>
...
Timeouts: <test, full error text, steps.txt contents, probe output>
Anything that looked wrong in Altium even when verified was true:
```
