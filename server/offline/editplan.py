"""EditPlan: a JSON list of edits that can be reviewed, then replayed offline or live.

    {"version": 1,
     "ops": [
       {"op": "remove", "kinds": ["track", "via"], "net": "SENSE"},
       {"op": "merge_nets", "from": ["SENSE"], "into": "VBAT"},
       {"op": "add_track", "layer": "Top Layer", "x1": 10, "y1": 10, "x2": 20, "y2": 10,
        "width": 2.0, "net": "VBAT"}
     ]}

Coordinates are absolute mm (database coordinates, as the file stores them).
Set "origin_relative": true at the top to give them relative to the board
origin instead. validate() checks every op before anything runs; apply() runs
them in order on a PcbDoc with edits.Editor. live.py turns the same plan into
sandbox scripts for run_altium_script.
"""

import json
from pathlib import Path

from .edits import EditError, Editor

# op -> (required keys, optional keys)
OPS = {
    "add_track": (("layer", "x1", "y1", "x2", "y2", "width"), ("net",)),
    "add_arc": (("layer", "cx", "cy", "radius", "start_angle", "end_angle", "width"), ("net",)),
    "add_via": (("x", "y", "diameter", "hole"), ("net", "low", "high")),
    "add_fill": (("layer", "x1", "y1", "x2", "y2"), ("rotation", "net")),
    "add_region": (("layer", "outline"), ("holes", "net")),
    "add_text": (("text", "x", "y", "height"), ("layer", "rotation")),
    "add_pad": (("component", "name", "x", "y", "sx", "sy"),
                ("layer", "shape", "hole", "rotation", "net", "corner_pct")),
    "remove": ((), ("kinds", "net", "layer", "box", "polygon", "component", "free", "pours")),
    "set_net": (("net",), ("select",)),
    "add_net": (("name",), ()),
    "rename_net": (("from", "to"), ()),
    "merge_nets": (("from", "into"), ()),
    "remove_net": (("name",), ()),
    "add_component": (("designator", "pattern", "x", "y"),
                      ("rotation", "side", "children", "template", "source_unique_id", "comment")),
    "remove_component": (("designator",), ("keep_copper",)),
    "move_component": (("designator",), ("x", "y", "rotation", "side")),
    "add_polygon": (("layer", "outline"), ("net", "name", "hatch", "pour_index", "settings")),
    "remove_polygon": (("polygon",), ()),
    "set_polygon_outline": (("polygon", "outline"), ()),
    "set_polygon": (("polygon",), ("net", "props")),
    "unpour": (("polygon",), ()),
    "merge_polygons": (("polygons",), ("bridge_fills",)),
    "add_net_class": (("name", "nets"), ()),
    "add_rule": (("kind", "name"), ("scope1", "scope2", "priority", "values", "enabled")),
    "set_rule_scope": (("name",), ("scope1", "scope2")),
    "set_pad_net": (("designator", "pad", "net"), ()),
    "set_component": (("designator", "props"), ()),
    "place_footprint": (("designator", "footprint", "x", "y"),
                        ("rotation", "side", "source", "nets", "comment", "source_unique_id",
                         "props")),
    "swap_footprint": (("designator", "footprint"), ("source",)),
}
XY_KEYS = {"x", "y", "x1", "y1", "x2", "y2", "cx", "cy"}


class PlanError(ValueError):
    pass


def load(path_or_obj):
    if isinstance(path_or_obj, (str, Path)) and Path(path_or_obj).is_file():
        return json.loads(Path(path_or_obj).read_text(encoding="utf-8"))
    if isinstance(path_or_obj, str):
        return json.loads(path_or_obj)
    return path_or_obj


def validate(plan):
    """Problems with a plan's shape; empty when every op is well formed."""
    probs = []
    if not isinstance(plan, dict) or not isinstance(plan.get("ops"), list):
        return ["a plan is an object with an 'ops' list"]
    for i, op in enumerate(plan["ops"]):
        if not isinstance(op, dict) or "op" not in op:
            probs.append(f"op {i}: not an object with an 'op' key")
            continue
        name = op["op"]
        if name not in OPS:
            probs.append(f"op {i}: unknown op {name!r}")
            continue
        req, opt = OPS[name]
        for k in req:
            if k not in op:
                probs.append(f"op {i} ({name}): missing {k!r}")
        for k in op:
            if k != "op" and k not in req and k not in opt and k != "comment":
                probs.append(f"op {i} ({name}): unexpected key {k!r}")
        for k in XY_KEYS & set(op):
            if not isinstance(op[k], (int, float)) or isinstance(op[k], bool):
                probs.append(f"op {i} ({name}): {k} must be a number (mm)")
        if name == "remove" and not set(op) - {"op", "kinds", "comment", "pours", "free"}:
            probs.append(f"op {i}: remove needs a selector (net, layer, box, polygon or component); "
                         "refusing to remove everything")
        if name == "merge_polygons" and len(op.get("polygons", [])) != 2:
            probs.append(f"op {i}: merge_polygons takes exactly two polygons")
    return probs


def _rel(plan, doc, op):
    if not plan.get("origin_relative"):
        return op
    ox, oy = doc.origin
    out = dict(op)
    for k in XY_KEYS & set(op):
        out[k] = op[k] + (ox if k.startswith(("x", "cx")) or k == "x" else oy)
    for k in ("outline",):
        if k in op:
            out[k] = [(x + ox, y + oy) for x, y in op[k]]
    if "holes" in op:
        out["holes"] = [[(x + ox, y + oy) for x, y in h] for h in op["holes"]]
    if "box" in op:
        b = op["box"]
        out["box"] = [b[0] + ox, b[1] + oy, b[2] + ox, b[3] + oy]
    return out


def _sel(op):
    keys = ("kinds", "net", "layer", "box", "polygon", "component", "free", "pours")
    return {k: op[k] for k in keys if k in op}


def run_op(ed, op):
    name = op["op"]
    a = {k: v for k, v in op.items() if k not in ("op", "comment")}
    if name == "add_track":
        ed.add_track(a["layer"], a["x1"], a["y1"], a["x2"], a["y2"], a["width"], a.get("net"))
    elif name == "add_arc":
        ed.add_arc(a["layer"], a["cx"], a["cy"], a["radius"], a["start_angle"], a["end_angle"],
                   a["width"], a.get("net"))
    elif name == "add_via":
        ed.add_via(a["x"], a["y"], a["diameter"], a["hole"], a.get("net"),
                   a.get("low", "Top Layer"), a.get("high", "Bottom Layer"))
    elif name == "add_fill":
        ed.add_fill(a["layer"], a["x1"], a["y1"], a["x2"], a["y2"], a.get("rotation", 0.0),
                    a.get("net"))
    elif name == "add_region":
        ed.add_region(a["layer"], [tuple(p) for p in a["outline"]],
                      [[tuple(p) for p in h] for h in a.get("holes", [])], a.get("net"))
    elif name == "add_text":
        ed.add_text(a["text"], a["x"], a["y"], a["height"], a.get("layer", "Top Overlay"),
                    a.get("rotation", 0.0))
    elif name == "add_pad":
        ed.add_pad(a["component"], a["name"], a["x"], a["y"], a["sx"], a["sy"],
                   a.get("layer", "Top Layer"), a.get("shape", "rect"), a.get("hole", 0.0),
                   a.get("rotation", 0.0), a.get("net"), True, a.get("corner_pct", 0))
    elif name == "remove":
        sel = _sel(a)
        if "kinds" not in sel:
            sel["kinds"] = ("track", "arc", "via", "pad", "fill", "region", "text")
        recs = ed.select(**sel)
        ed.remove(recs)
        return {"removed": len(recs)}
    elif name == "set_net":
        sel = _sel(a.get("select", {}))
        if "kinds" not in sel:
            sel["kinds"] = ("track", "arc", "via", "pad", "fill", "region")
        return {"moved": ed.set_net(ed.select(**sel), a["net"])}
    elif name == "add_net":
        ed.add_net(a["name"])
    elif name == "rename_net":
        ed.rename_net(a["from"], a["to"])
    elif name == "merge_nets":
        return {"moved": ed.merge_nets(a["from"], a["into"])}
    elif name == "remove_net":
        ed.remove_net(a["name"])
    elif name == "add_component":
        ed.add_component(a["designator"], a["pattern"], a["x"], a["y"], a.get("rotation", 0.0),
                         a.get("side", "top"), a.get("children", ()), a.get("template"),
                         a.get("source_unique_id"), a.get("comment", ""))
    elif name == "remove_component":
        return {"removed": ed.remove_component(a["designator"], a.get("keep_copper", False))}
    elif name == "move_component":
        ed.move_component(a["designator"], a.get("x"), a.get("y"), a.get("rotation"), a.get("side"))
    elif name == "add_polygon":
        return {"polygon": ed.add_polygon(a["layer"], [tuple(p) for p in a["outline"]], a.get("net"),
                                          a.get("name"), a.get("hatch", "Solid"),
                                          a.get("pour_index"), **a.get("settings", {}))}
    elif name == "remove_polygon":
        ed.remove_polygon(a["polygon"])
    elif name == "set_polygon_outline":
        ed.set_polygon_outline(a["polygon"], [tuple(p) for p in a["outline"]])
    elif name == "set_polygon":
        props = dict(a.get("props", {}))
        if "net" in a:
            props["net"] = a["net"]
        ed.set_polygon(a["polygon"], **props)
    elif name == "unpour":
        return {"removed": ed.unpour(a["polygon"])}
    elif name == "merge_polygons":
        p1, p2 = a["polygons"]
        return {"polygon": ed.merge_polygons(p1, p2, a.get("bridge_fills", True))}
    elif name == "add_net_class":
        ed.add_net_class(a["name"], a["nets"])
    elif name == "add_rule":
        ed.add_rule(a["kind"], a["name"], a.get("scope1", "All"), a.get("scope2", "All"),
                    a.get("priority"), a.get("values"), a.get("enabled", True))
    elif name == "set_rule_scope":
        ed.set_rule_scope(a["name"], a.get("scope1"), a.get("scope2"))
    elif name == "set_pad_net":
        return {"pads": ed.set_pad_net(a["designator"], a["pad"], a["net"])}
    elif name == "set_component":
        ed.set_component_props(a["designator"], a["props"])
    elif name in ("place_footprint", "swap_footprint"):
        from . import footprints as F
        sources = a.get("source") or ed.plan_libraries
        if isinstance(sources, (str, Path)):
            sources = [sources]
        fp = F.find(a["footprint"], list(sources or []))
        if fp is None:
            raise EditError(f"footprint {a['footprint']!r} not found in "
                            f"{[str(x) for x in (sources or [])] or 'any source (none given)'}")
        if name == "swap_footprint":
            return F.swap(ed, a["designator"], fp)
        return F.place(ed, fp, a["designator"], a["x"], a["y"], a.get("rotation", 0.0),
                       a.get("side", "top"), a.get("nets"), a.get("comment", ""),
                       a.get("source_unique_id"), a.get("props"))
    return {}


def apply(doc, plan):
    """Run a plan offline. Returns (editor, per-op results). Stops at the first
    failing op with PlanError naming it; the document is then half-edited and
    must not be saved."""
    plan = load(plan)
    probs = validate(plan)
    if probs:
        raise PlanError("; ".join(probs))
    ed = Editor(doc)
    ed.plan_libraries = plan.get("libraries", [])
    results = []
    for i, op in enumerate(plan["ops"]):
        try:
            res = run_op(ed, _rel(plan, doc, op)) or {}
        except (EditError, KeyError, ValueError) as e:
            raise PlanError(f"op {i} ({op['op']}) failed: {e}") from e
        results.append({"op": op["op"], **res})
    return ed, results


def cmd_edit(args):
    from .pcbdoc import PcbDoc
    plan = load(args.plan)
    probs = validate(plan)
    if probs:
        print(json.dumps({"ok": False, "problems": probs}, indent=2))
        return 2
    if args.live:
        from . import live
        out = live.write_scripts(plan, args.board_path or str(Path(args.board).resolve()),
                                 args.live, doc=PcbDoc.open(args.board))
        print(json.dumps(out, indent=2))
        return 0 if out["ok"] else 2
    doc = PcbDoc.open(args.board)
    ed, results = apply(doc, plan)
    if args.dry_run or not args.out:
        print(json.dumps({"ok": True, "dry_run": True, "results": results, **ed.report()},
                         indent=2))
        return 0
    rep = ed.save(args.out)
    rep["results"] = results
    print(json.dumps(rep, indent=2, default=str))
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("board")
        p.add_argument("plan", help="EditPlan JSON file")
        p.add_argument("-o", "--out", help="write the edited board here (never onto the input)")
        p.add_argument("--dry-run", action="store_true", help="apply in memory and report only")
        p.add_argument("--live", metavar="DIR",
                       help="instead of editing the file, write sandbox scripts for "
                            "run_altium_script to DIR")
        p.add_argument("--board-path", help="path of the board as Altium has it open "
                                            "(for --live; default: the board argument)")
    command("edit", cmd_edit, "apply an EditPlan (offline) or emit its live scripts", build)
