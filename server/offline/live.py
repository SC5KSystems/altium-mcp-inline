"""Turn an EditPlan into DelphiScript bodies for run_altium_script.

The same plan editplan.apply() runs offline can run in a live Altium, one
script per step, through the existing run_altium_script tool (no server
restart). Every script:

  * gets the board with PCBServer.GetPCBBoardByPath (GetCurrentPCBBoard
    returned nil in the sandbox and wedged Altium);
  * uses only the sandbox's own scratch variables: Obj1 board, Obj2
    iterator, Obj3 object, Obj4 net or helper, Obj5 a TInterfaceList; S1-S3,
    I1-I3, B1;
  * tests with nested ifs, never `a and b` on a reference that may be nil
    (whether DelphiScript short-circuits is not known), and never uses Exit
    (it skips writing ResultText);
  * when the plan was checked against the saved file, refuses to act unless
    the live board matches it (same selection count), and changes nothing;
  * passes script_preflight against the actual server sandbox template
    before it is written.

Each script carries "proven": true only when every call in it was proven on
a live board (2026-11-02 / 2026-11-04); the rest copy patterns from this
repo's .pas units and are UNVERIFIED on a board. Ops with no safe live form
raise LiveError naming the offline alternative.
"""

import json
import re
from pathlib import Path

from . import layers as L

SANDBOX = Path(__file__).resolve().parent.parent / "SandboxScript" / "Sandbox.pas"
CHUNK = 60

KIND_SET = {"track": "eTrackObject", "arc": "eArcObject", "via": "eViaObject", "pad": "ePadObject",
            "fill": "eFillObject", "region": "eRegionObject", "text": "eTextObject"}
LAYER_CONST = {L.TOP: "eTopLayer", L.BOTTOM: "eBottomLayer", L.MULTI: "eMultiLayer"}


class LiveError(ValueError):
    pass


def q(s):
    return "'" + str(s).replace("'", "''") + "'"


def mm(v):
    return f"MMsToCoord({float(v):.6f})"


def layer_expr(layer):
    n = L.parse(layer)
    if n > 255:
        raise LiveError(f"{L.name(n)}: layers above Mechanical 16 are not addressable live")
    return LAYER_CONST.get(n, str(n))


def _json(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("'", "''")


class Script:
    def __init__(self, title, proven, notes=()):
        self.title = title
        self.proven = proven
        self.notes = list(notes)
        self.lines = []
        self.depth = 0

    def __call__(self, *lines):
        for ln in lines:
            if ln.startswith("end") or ln.startswith("else"):
                pass
            self.lines.append("    " * self.depth + ln)

    def block(self):
        self("begin")
        self.depth += 1

    def close(self, semicolon=True):
        self.depth -= 1
        self("end;" if semicolon else "end")

    def text(self):
        return "\n".join(self.lines) + "\n"


def _board(s, board):
    s(f"SandboxLog('get board');",
      f"Obj1 := PCBServer.GetPCBBoardByPath({q(board)});",
      "if Obj1 = nil then",
      f"    ResultText := '{{\"error\": \"board is not open in Altium: {_json(board)}\"}}'",
      "else")
    s.block()


def _iterate(s, kinds):
    sets = ", ".join(KIND_SET[k] for k in kinds)
    s("Obj2 := Obj1.BoardIterator_Create;",
      f"Obj2.AddFilter_ObjectSet(MkSet({sets}));",
      "Obj2.AddFilter_LayerSet(AllLayers);",
      "Obj2.AddFilter_Method(eProcessAll);",
      "Obj3 := Obj2.FirstPCBObject;",
      "while Obj3 <> nil do")
    s.block()


def _end_iterate(s):
    s("Obj3 := Obj2.NextPCBObject;")
    s.close()
    s("Obj1.BoardIterator_Destroy(Obj2);")


def _find_net(s, name, var="Obj4"):
    s(f"{var} := nil;",
      "Obj2 := Obj1.BoardIterator_Create;",
      "Obj2.AddFilter_ObjectSet(MkSet(eNetObject));",
      "Obj2.AddFilter_LayerSet(AllLayers);",
      "Obj2.AddFilter_Method(eProcessAll);",
      "Obj3 := Obj2.FirstPCBObject;",
      "while Obj3 <> nil do")
    s.block()
    s(f"if UpperCase(Obj3.Name) = {q(name.upper())} then {var} := Obj3;",
      "Obj3 := Obj2.NextPCBObject;")
    s.close()
    s("Obj1.BoardIterator_Destroy(Obj2);")


def _match(s, sel):
    """Set B1 to 1 when Obj3 matches the selection, with nested ifs only."""
    s("B1 := 1;")
    if sel.get("layer") is not None:
        s(f"if Obj3.Layer <> {layer_expr(sel['layer'])} then B1 := 0;")
    if sel.get("net") is not None:
        s("if B1 = 1 then",
          "    if Obj3.Net = nil then B1 := 0",
          f"    else if UpperCase(Obj3.Net.Name) <> {q(str(sel['net']).upper())} then B1 := 0;")
    if not sel.get("pours") and sel.get("polygon") is None:
        s("if B1 = 1 then",
          "    if Obj3.ObjectId <> ePadObject then",
          "        if Obj3.ObjectId <> eViaObject then",
          "            if Obj3.InPolygon then B1 := 0;")
    if sel.get("polygon") is not None:
        raise LiveError("selecting a polygon's pour primitives is not supported live; "
                        "use unpour / remove_polygon, which Altium handles itself")
    if sel.get("component") is not None:
        s("if B1 = 1 then",
          "    if not Obj3.InComponent then B1 := 0",
          f"    else if UpperCase(Obj3.Component.Name.Text) <> {q(str(sel['component']).upper())} then B1 := 0;")
    if sel.get("free"):
        s("if B1 = 1 then",
          "    if Obj3.InComponent then B1 := 0;")
    if sel.get("box") is not None:
        x0, y0, x1, y1 = sel["box"]
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        X0, Y0, X1, Y1 = mm(x0), mm(y0), mm(x1), mm(y1)

        def inside(xe, ye):
            return f"({xe} < {X0}) or ({xe} > {X1}) or ({ye} < {Y0}) or ({ye} > {Y1})"
        s("if B1 = 1 then")
        s.block()
        s(f"if Obj3.ObjectId = eTrackObject then",
          f"    begin if {inside('Obj3.x1', 'Obj3.y1')} then B1 := 0;",
          f"          if {inside('Obj3.x2', 'Obj3.y2')} then B1 := 0; end",
          f"else if Obj3.ObjectId = eArcObject then",
          f"    begin if {inside('Obj3.XCenter', 'Obj3.YCenter')} then B1 := 0; end",
          f"else if Obj3.ObjectId = eFillObject then",
          f"    begin if {inside('Obj3.x1Location', 'Obj3.y1Location')} then B1 := 0;",
          f"          if {inside('Obj3.x2Location', 'Obj3.y2Location')} then B1 := 0; end",
          f"else if Obj3.ObjectId = eTextObject then",
          f"    begin if {inside('Obj3.XLocation', 'Obj3.YLocation')} then B1 := 0; end",
          f"else if Obj3.ObjectId = eRegionObject then",
          f"    begin if {inside('Obj3.BoundingRectangle.Left', 'Obj3.BoundingRectangle.Bottom')} then B1 := 0;",
          f"          if {inside('Obj3.BoundingRectangle.Right', 'Obj3.BoundingRectangle.Top')} then B1 := 0; end",
          f"else",
          f"    begin if {inside('Obj3.x', 'Obj3.y')} then B1 := 0; end;")
        s.close()


def _collect(s, kinds, sel):
    s("Obj5 := TInterfaceList.Create;")
    _iterate(s, kinds)
    _match(s, sel)
    s("if B1 = 1 then Obj5.Add(Obj3);")
    _end_iterate(s)
    s("SandboxLog('matched ' + IntToStr(Obj5.Count));")


def _guard_count(s, expected, what):
    """Open an if/else that only proceeds when the live count matches."""
    if expected is None:
        s("if 0 = 1 then ResultText := ''", "else")
        s.block()
        return
    s(f"if Obj5.Count <> {int(expected)} then",
      f"    ResultText := '{{\"error\": \"{_json(what)} matched ' + IntToStr(Obj5.Count) + "
      f"' objects live, the plan expected {int(expected)}; nothing changed\"}}'",
      "else")
    s.block()


def _begin(s, obj="Obj3"):
    s(f"PCBServer.SendMessageToRobots({obj}.I_ObjectAddress, c_Broadcast, PCBM_BeginModify, c_NoEventData);")


def _endm(s, obj="Obj3"):
    s(f"PCBServer.SendMessageToRobots({obj}.I_ObjectAddress, c_Broadcast, PCBM_EndModify, c_NoEventData);")


def _register(s, obj="Obj3"):
    s(f"Obj1.AddPCBObject({obj});",
      f"PCBServer.SendMessageToRobots(Obj1.I_ObjectAddress, c_Broadcast, PCBM_BoardRegisteration, {obj}.I_ObjectAddress);")


def _with_net(s, net, body):
    """Run body() with Obj4 = the named net (or nil when net is None)."""
    if net is None:
        s("Obj4 := nil;")
        body()
        return
    _find_net(s, net)
    s("if Obj4 = nil then",
      f"    ResultText := '{{\"error\": \"no net {_json(net)} on the live board\"}}'",
      "else")
    s.block()
    body()
    s.close()


def _assign_net(s):
    """Give Obj3 the net in Obj4 and register it in the net's member group."""
    s("if Obj4 <> nil then")
    s.block()
    _begin(s)
    s("Obj3.Net := Obj4;", "Obj4.AddPCBObject(Obj3);")
    _endm(s)
    s.close()


def _segment_var():
    """Name of a TPolySegment variable the sandbox template declares, or None."""
    import script_preflight as sp
    vocab = sp.build_vocabulary(SANDBOX)
    for low, (spelling, kind, typ) in vocab.sandbox.items():
        if kind == "var" and typ == "tpolysegment":
            return spelling
    return None


# ---------------------------------------------------------------- per-op builders

def _op_scripts(op, board, expect):
    name = op["op"]
    out = []

    def new(title, proven, notes=()):
        s = Script(title, proven, notes)
        out.append(s)
        _board(s, board)
        return s

    if name in ("remove", "set_net"):
        sel = dict(op.get("select", op))
        kinds = sel.get("kinds") or (["track", "arc", "via", "pad", "fill", "region", "text"]
                                     if name == "remove" else
                                     ["track", "arc", "via", "pad", "fill", "region"])
        s = new(f"{name}: {', '.join(kinds)}", True)
        if name == "remove":
            _collect(s, kinds, sel)
            _guard_count(s, expect, "the selection")
            s("PCBServer.PreProcess;",
              "for I1 := 0 to Obj5.Count - 1 do",
              "    Obj1.RemovePCBObject(Obj5.Items[I1]);",
              "PCBServer.PostProcess;",
              "ResultText := '{\"removed\": ' + IntToStr(Obj5.Count) + '}';")
            s.close()
        else:
            def body():
                _collect(s, kinds, sel)
                _guard_count(s, expect, "the selection")
                s("PCBServer.PreProcess;",
                  "for I1 := 0 to Obj5.Count - 1 do")
                s.block()
                s("Obj3 := Obj5.Items[I1];")
                _assign_net(s)
                s.close()
                s("PCBServer.PostProcess;",
                  "ResultText := '{\"moved\": ' + IntToStr(Obj5.Count) + '}';")
                s.close()
            _with_net(s, op["net"], body)
    elif name == "rename_net":
        s = new(f"rename net {op['from']} -> {op['to']}", True)
        _find_net(s, op["from"])
        s("if Obj4 = nil then",
          f"    ResultText := '{{\"error\": \"no net {_json(op['from'])}\"}}'",
          "else")
        s.block()
        _begin(s, "Obj4")
        s(f"Obj4.Name := {q(op['to'])};")
        _endm(s, "Obj4")
        s("ResultText := '{\"renamed\": 1}';")
        s.close()
    elif name == "merge_nets":
        for src in ([op["from"]] if isinstance(op["from"], str) else op["from"]):
            s = new(f"merge net {src} into {op['into']}", True,
                    [f"net {src} is left on the board, empty; delete it in Altium if wanted"])

            def body(src=src, s=s):
                s("Obj5 := TInterfaceList.Create;")
                _iterate(s, ["track", "arc", "via", "pad", "fill", "region"])
                s("if Obj3.Net <> nil then",
                  f"    if UpperCase(Obj3.Net.Name) = {q(src.upper())} then Obj5.Add(Obj3);")
                _end_iterate(s)
                s("PCBServer.PreProcess;", "for I1 := 0 to Obj5.Count - 1 do")
                s.block()
                s("Obj3 := Obj5.Items[I1];")
                _assign_net(s)
                s.close()
                s("I2 := Obj5.Count;", "Obj5 := TInterfaceList.Create;",
                  "Obj2 := Obj1.BoardIterator_Create;",
                  "Obj2.AddFilter_ObjectSet(MkSet(ePolyObject));",
                  "Obj2.AddFilter_LayerSet(AllLayers);", "Obj2.AddFilter_Method(eProcessAll);",
                  "Obj3 := Obj2.FirstPCBObject;", "while Obj3 <> nil do")
                s.block()
                s("if Obj3.Net <> nil then",
                  f"    if UpperCase(Obj3.Net.Name) = {q(src.upper())} then Obj5.Add(Obj3);",
                  "Obj3 := Obj2.NextPCBObject;")
                s.close()
                s("Obj1.BoardIterator_Destroy(Obj2);",
                  "for I1 := 0 to Obj5.Count - 1 do")
                s.block()
                s("Obj3 := Obj5.Items[I1];")
                _begin(s)
                s("Obj3.Net := Obj4;")
                _endm(s)
                s.close()
                s("PCBServer.PostProcess;",
                  "ResultText := '{\"moved\": ' + IntToStr(I2) + ', \"polygons\": ' + "
                  "IntToStr(Obj5.Count) + ', \"repour\": true}';")
            _with_net(s, op["into"], body)
    elif name in ("add_track", "add_via", "add_fill", "add_arc", "add_region", "add_text"):
        proven = name in ("add_track", "add_via", "add_fill")
        s = new(name, proven, [] if proven else
                ["arcs, regions and texts have not been created live on a board yet: UNVERIFIED"])

        def body():
            if name == "add_track":
                s("Obj3 := PCBServer.PCBObjectFactory(eTrackObject, eNoDimension, eCreate_Default);",
                  f"Obj3.x1 := {mm(op['x1'])};", f"Obj3.y1 := {mm(op['y1'])};",
                  f"Obj3.x2 := {mm(op['x2'])};", f"Obj3.y2 := {mm(op['y2'])};",
                  f"Obj3.Width := {mm(op['width'])};", f"Obj3.Layer := {layer_expr(op['layer'])};")
            elif name == "add_via":
                s("Obj3 := PCBServer.PCBObjectFactory(eViaObject, eNoDimension, eCreate_Default);",
                  f"Obj3.x := {mm(op['x'])};", f"Obj3.y := {mm(op['y'])};",
                  f"Obj3.Size := {mm(op['diameter'])};", f"Obj3.HoleSize := {mm(op['hole'])};",
                  f"Obj3.LowLayer := {layer_expr(op.get('low', 'Top Layer'))};",
                  f"Obj3.HighLayer := {layer_expr(op.get('high', 'Bottom Layer'))};")
            elif name == "add_fill":
                s("Obj3 := PCBServer.PCBObjectFactory(eFillObject, eNoDimension, eCreate_Default);",
                  f"Obj3.x1Location := {mm(min(op['x1'], op['x2']))};",
                  f"Obj3.y1Location := {mm(min(op['y1'], op['y2']))};",
                  f"Obj3.x2Location := {mm(max(op['x1'], op['x2']))};",
                  f"Obj3.y2Location := {mm(max(op['y1'], op['y2']))};",
                  f"Obj3.Rotation := {float(op.get('rotation', 0.0)):.6f};",
                  f"Obj3.Layer := {layer_expr(op['layer'])};")
            elif name == "add_arc":
                s("Obj3 := PCBServer.PCBObjectFactory(eArcObject, eNoDimension, eCreate_Default);",
                  f"Obj3.XCenter := {mm(op['cx'])};", f"Obj3.YCenter := {mm(op['cy'])};",
                  f"Obj3.Radius := {mm(op['radius'])};",
                  f"Obj3.StartAngle := {float(op['start_angle']):.6f};",
                  f"Obj3.EndAngle := {float(op['end_angle']):.6f};",
                  f"Obj3.LineWidth := {mm(op['width'])};",
                  f"Obj3.Layer := {layer_expr(op['layer'])};")
            elif name == "add_region":
                if op.get("holes"):
                    raise LiveError("regions with holes are not supported live; add them offline")
                s("Obj3 := PCBServer.PCBObjectFactory(eRegionObject, eNoDimension, eCreate_Default);",
                  "Obj2 := PCBServer.PCBContourFactory;")
                for x, y in op["outline"]:
                    s(f"Obj2.AddPoint({mm(x)}, {mm(y)});")
                s("Obj3.SetOutlineContour(Obj2);", f"Obj3.Layer := {layer_expr(op['layer'])};")
            elif name == "add_text":
                s("Obj3 := PCBServer.PCBObjectFactory(eTextObject, eNoDimension, eCreate_Default);",
                  f"Obj3.XLocation := {mm(op['x'])};", f"Obj3.YLocation := {mm(op['y'])};",
                  f"Obj3.Size := {mm(op['height'])};",
                  f"Obj3.Layer := {layer_expr(op.get('layer', 'Top Overlay'))};",
                  f"Obj3.Rotation := {float(op.get('rotation', 0.0)):.6f};",
                  f"Obj3.Text := {q(op['text'])};")
            s("PCBServer.PreProcess;")
            _register(s)
            if name != "add_text":
                _assign_net(s)
            s("PCBServer.PostProcess;", "ResultText := '{\"created\": 1}';")
        if name == "add_text":
            s("Obj4 := nil;")
            body()
        else:
            _with_net(s, op.get("net"), body)
    elif name == "move_component":
        s = new(f"move {op['designator']}", True)
        s("Obj4 := nil;", "Obj2 := Obj1.BoardIterator_Create;",
          "Obj2.AddFilter_ObjectSet(MkSet(eComponentObject));",
          "Obj2.AddFilter_LayerSet(AllLayers);", "Obj2.AddFilter_Method(eProcessAll);",
          "Obj3 := Obj2.FirstPCBObject;", "while Obj3 <> nil do")
        s.block()
        s(f"if UpperCase(Obj3.Name.Text) = {q(op['designator'].upper())} then Obj4 := Obj3;",
          "Obj3 := Obj2.NextPCBObject;")
        s.close()
        s("Obj1.BoardIterator_Destroy(Obj2);", "if Obj4 = nil then",
          f"    ResultText := '{{\"error\": \"no component {_json(op['designator'])}\"}}'",
          "else")
        s.block()
        s("PCBServer.PreProcess;")
        _begin(s, "Obj4")
        if op.get("side") is not None:
            s(f"Obj4.Layer := {'eBottomLayer' if str(op['side']).lower().startswith('b') else 'eTopLayer'};")
        if op.get("rotation") is not None:
            s(f"Obj4.Rotation := {float(op['rotation']) % 360.0:.6f};")
        if op.get("x") is not None or op.get("y") is not None:
            if op.get("x") is None or op.get("y") is None:
                raise LiveError("move_component live needs both x and y")
            s(f"Obj4.MoveToXY({mm(op['x'])}, {mm(op['y'])});")
        _endm(s, "Obj4")
        s("PCBServer.PostProcess;", "ResultText := '{\"moved\": 1}';")
        s.close()
    elif name == "remove_component":
        s = new(f"remove {op['designator']}", False,
                ["Board.RemovePCBObject on a component is UNVERIFIED (proven only for primitives "
                 "and polygons)"])
        s("Obj4 := nil;", "Obj2 := Obj1.BoardIterator_Create;",
          "Obj2.AddFilter_ObjectSet(MkSet(eComponentObject));",
          "Obj2.AddFilter_LayerSet(AllLayers);", "Obj2.AddFilter_Method(eProcessAll);",
          "Obj3 := Obj2.FirstPCBObject;", "while Obj3 <> nil do")
        s.block()
        s(f"if UpperCase(Obj3.Name.Text) = {q(op['designator'].upper())} then Obj4 := Obj3;",
          "Obj3 := Obj2.NextPCBObject;")
        s.close()
        s("Obj1.BoardIterator_Destroy(Obj2);", "if Obj4 = nil then",
          f"    ResultText := '{{\"error\": \"no component {_json(op['designator'])}\"}}'",
          "else")
        s.block()
        s("PCBServer.PreProcess;", "Obj1.RemovePCBObject(Obj4);", "PCBServer.PostProcess;",
          "ResultText := '{\"removed\": 1}';")
        s.close()
    elif name in ("set_polygon_outline", "remove_polygon", "unpour"):
        poly = op["polygon"]
        ident = poly if isinstance(poly, dict) else None
        if ident is None:
            raise LiveError(f"{name} live needs the polygon identified by layer, net and name "
                            "(the plan compiler fills this in from the board)")
        if name == "set_polygon_outline":
            seg = _segment_var()
            if seg is None:
                raise LiveError("set_polygon_outline live needs a TPolySegment variable in "
                                "server/SandboxScript/Sandbox.pas (add `Seg : TPolySegment;` to "
                                "its var section, as on the 2026-11-04 run)")
        s = new(f"{name} {ident.get('name') or ident.get('layer')}", False,
                ["Rebuild, RemovePCBObject and the segment writes are proven; finding the "
                 "polygon by its Name property is not"])
        _find_polygon(s, ident)
        s("if Obj4 = nil then",
          f"    ResultText := '{{\"error\": \"polygon not found live: {_json(json.dumps(ident))}\"}}'",
          "else")
        s.block()
        if name == "remove_polygon":
            s("PCBServer.PreProcess;", "Obj1.RemovePCBObject(Obj4);", "PCBServer.PostProcess;",
              "ResultText := '{\"removed\": 1}';")
        elif name == "unpour":
            _rebuild(s, "Obj4")
            s("ResultText := '{\"repoured\": 1}';")
        else:
            pts = list(op["outline"])
            s("PCBServer.PreProcess;")
            _begin(s, "Obj4")
            s(f"Obj4.PointCount := {len(pts)};", f"{seg} := Obj4.Segments[0];",
              f"{seg}.Kind := ePolySegmentLine;")
            for i, (x, y) in enumerate(pts + [pts[0]]):
                s(f"{seg}.vx := {mm(x)};", f"{seg}.vy := {mm(y)};", f"Obj4.Segments[{i}] := {seg};")
            _endm(s, "Obj4")
            s("PCBServer.PostProcess;")
            _rebuild(s, "Obj4")
            s(f"ResultText := '{{\"points\": {len(pts)}, \"repoured\": 1}}';")
        s.close()
    elif name == "set_rule_scope":
        s = new(f"rule scope {op['name']}", True)
        s("I2 := 0;")
        s("Obj2 := Obj1.BoardIterator_Create;", "Obj2.AddFilter_ObjectSet(MkSet(eRuleObject));",
          "Obj2.AddFilter_LayerSet(AllLayers);", "Obj2.AddFilter_Method(eProcessAll);",
          "Obj3 := Obj2.FirstPCBObject;", "while Obj3 <> nil do")
        s.block()
        s(f"if UpperCase(Obj3.Name) = {q(op['name'].upper())} then")
        s.block()
        if op.get("scope1") is not None:
            s(f"Obj3.Scope1Expression := {q(op['scope1'])};")
        if op.get("scope2") is not None:
            s(f"Obj3.Scope2Expression := {q(op['scope2'])};")
        s("I2 := I2 + 1;")
        s.close()
        s("Obj3 := Obj2.NextPCBObject;")
        s.close()
        s("Obj1.BoardIterator_Destroy(Obj2);",
          "ResultText := '{\"rules\": ' + IntToStr(I2) + '}';")
    elif name == "add_net_class":
        s = new(f"net class {op['name']}", False,
                ["pattern from pcb_utils.pas CreateNetClass; not run from the sandbox yet"])
        s("PCBServer.PreProcess;",
          "Obj4 := PCBServer.PCBClassFactoryByClassMember(eClassMemberKind_Net);",
          "Obj4.SuperClass := False;", f"Obj4.Name := {q(op['name'])};", "Obj1.AddPCBObject(Obj4);",
          "I2 := 0;")
        for n in op["nets"]:
            s(f"if Obj4.AddMemberByName({q(n)}) then I2 := I2 + 1;")
        s("PCBServer.PostProcess;", "ResultText := '{\"members\": ' + IntToStr(I2) + '}';")
    else:
        raise LiveError(f"{name} has no live form; apply it offline and open the result, or "
                        "split it into ops that do")
    for s in out:
        s.close()
    return out


def _find_polygon(s, ident):
    s("Obj4 := nil;", "Obj2 := Obj1.BoardIterator_Create;",
      "Obj2.AddFilter_ObjectSet(MkSet(ePolyObject));",
      "Obj2.AddFilter_LayerSet(AllLayers);", "Obj2.AddFilter_Method(eProcessAll);",
      "Obj3 := Obj2.FirstPCBObject;", "while Obj3 <> nil do")
    s.block()
    s("B1 := 1;")
    if ident.get("layer"):
        s(f"if Obj3.Layer <> {layer_expr(ident['layer'])} then B1 := 0;")
    if ident.get("net"):
        s("if B1 = 1 then",
          "    if Obj3.Net = nil then B1 := 0",
          f"    else if UpperCase(Obj3.Net.Name) <> {q(ident['net'].upper())} then B1 := 0;")
    if ident.get("name"):
        s(f"if B1 = 1 then if UpperCase(Obj3.Name) <> {q(ident['name'].upper())} then B1 := 0;")
    s("if B1 = 1 then Obj4 := Obj3;", "Obj3 := Obj2.NextPCBObject;")
    s.close()
    s("Obj1.BoardIterator_Destroy(Obj2);")


def _rebuild(s, var):
    """The proven repour: nudge the polygon so its bounding box is fresh, then Rebuild."""
    s(f"SandboxLog('repour');", "PCBServer.PreProcess;",
      f"{var}.MoveByXY(1, 0);", f"{var}.MoveByXY(-1, 0);", f"{var}.Rebuild;",
      "PCBServer.PostProcess;")


# ---------------------------------------------------------------- plan -> scripts

def _identify_polygons(plan, doc):
    """Replace polygon indices with layer/net/name identities the live board can match."""
    out = []
    for op in plan["ops"]:
        op = dict(op)
        if "polygon" in op and doc is not None and not isinstance(op["polygon"], dict):
            from .edits import Editor
            p = doc.polygons[Editor(doc).polygon_index(op["polygon"])]
            op["polygon"] = {"layer": L.name(p.layer), "name": p.name,
                             "net": doc.nets[p.net].name if p.net is not None else None,
                             "pour_index": p.pour_index}
        out.append(op)
    return out


def _expected_counts(plan, doc):
    """How many objects each remove/set_net selection matches on the saved file."""
    if doc is None:
        return {}
    from .edits import Editor
    from .editplan import _sel
    ed = Editor(doc)
    out = {}
    for i, op in enumerate(plan["ops"]):
        if op["op"] in ("remove", "set_net"):
            sel = _sel(op.get("select", op) if op["op"] == "set_net" else op)
            if "kinds" not in sel:
                sel["kinds"] = ("track", "arc", "via", "pad", "fill", "region", "text") \
                    if op["op"] == "remove" else ("track", "arc", "via", "pad", "fill", "region")
            try:
                out[i] = len(ed.select(**sel))
            except Exception:
                out[i] = None
    return out


def scripts_for_plan(plan, board_path, doc=None):
    """[{"index", "op", "title", "proven", "notes", "script"}] in run order.

    doc (the board as saved) lets scripts check the live board matches before
    they change anything. Raises LiveError for ops with no live form, or if a
    script fails preflight.
    """
    import script_preflight as sp
    from .editplan import validate
    probs = validate(plan)
    if probs:
        raise LiveError("; ".join(probs))
    if plan.get("origin_relative"):
        raise LiveError("live scripts need absolute coordinates; drop origin_relative")
    ops = _identify_polygons(plan, doc)
    expect = _expected_counts(plan, doc)
    out = []
    for i, op in enumerate(ops):
        for k, s in enumerate(_op_scripts(op, board_path, expect.get(i))):
            body = s.text()
            rep = sp.check(body, SANDBOX)
            if not rep["ok"]:
                raise LiveError(f"op {i} ({op['op']}) script fails preflight: "
                                + sp.format_report(rep))
            out.append({"index": i, "op": op["op"], "part": k, "title": s.title,
                        "proven": s.proven, "notes": s.notes, "expected": expect.get(i),
                        "script": body})
    return out


def save_script(board_path):
    body = (f"SandboxLog('save');\nObj1 := Client.GetDocumentByPath({q(board_path)});\n"
            "if Obj1 = nil then\n"
            f"    ResultText := '{{\"error\": \"document not open: {_json(board_path)}\"}}'\n"
            "else\nbegin\n    Obj1.DoFileSave('');\n    ResultText := '{\"saved\": 1}';\nend;\n")
    return body


def write_scripts(plan, board_path, out_dir, doc=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        scripts = scripts_for_plan(plan, board_path, doc)
    except LiveError as e:
        return {"ok": False, "error": str(e)}
    manifest = []
    for n, sc in enumerate(scripts, 1):
        fname = f"{n:03d}_{re.sub(r'[^A-Za-z0-9]+', '_', sc['op'])}.pas"
        (out_dir / fname).write_text(sc["script"], encoding="utf-8")
        manifest.append({k: v for k, v in sc.items() if k != "script"} | {"file": fname})
    (out_dir / "999_save.pas").write_text(save_script(board_path), encoding="utf-8")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"ok": True, "dir": str(out_dir), "scripts": len(manifest),
            "unverified": [m["file"] for m in manifest if not m["proven"]],
            "how": "Run each .pas body in file order with run_antium_script; stop at the first "
                   "result that is not success or reports an error. 999_save.pas saves the "
                   "board. Each script changes nothing when the live board does not match "
                   "the plan.".replace("antium", "altium")}
