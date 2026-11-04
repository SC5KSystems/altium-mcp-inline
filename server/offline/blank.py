"""Blank board: a new PcbDoc from a template, for a new board's first layout.

    python -m offline blank template.PcbDoc -o new.PcbDoc (--rect W H | --outline "x,y x,y ...")
        [--at X Y] [--hole X Y D ...] [--plated-hole X Y D PAD ...] [--corner R]

Keeps the template's layer stack, rules, classes (emptied), board options
and everything else that is not design; removes every component, net,
polygon and primitive. The outline is a W x H rectangle with its lower left
corner at --at (default: where the template's outline started), optionally
with corners cut at 45 degrees (--corner), or any polygon given as points.
Mounting holes go in as free pads: unplated (pad = hole, as Altium draws a
non-plated hole) or plated with a copper pad. Coordinates are absolute mm,
like the rest of the tools. The template is never written.
"""

import json

from . import layers as L
from .pcbdoc import set_props_vertices


def rect(w, h, x0=0.0, y0=0.0, corner=0.0):
    c = max(0.0, min(corner, w / 2, h / 2))
    if c <= 0:
        return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    return [(x0 + c, y0), (x0 + w - c, y0), (x0 + w, y0 + c), (x0 + w, y0 + h - c),
            (x0 + w - c, y0 + h), (x0 + c, y0 + h), (x0, y0 + h - c), (x0, y0 + c)]


def blank(template, outline, holes=(), plated_holes=()):
    """(Editor, report) for a stripped copy of `template` in memory."""
    from .edits import Editor, KIND_STORAGE
    from .pcbdoc import PcbDoc
    if len(outline) < 3:
        raise ValueError("an outline needs at least three points")
    doc = PcbDoc.open(template)
    ed = Editor(doc)
    before = {"components": len(doc.components), "nets": len(doc.nets),
              "polygons": len(doc.polygons)}
    removals = {}
    for name in list(KIND_STORAGE.values()) + ["Polygons6", "Components6", "Nets6"]:
        st = doc.storages.get(name)
        if st is not None and st.records:
            removals[name] = set(range(len(st.records)))
    before["primitives"] = sum(len(v) for k, v in removals.items()
                               if k not in ("Polygons6", "Components6", "Nets6"))
    ed._remove(removals)
    emptied = 0
    for c in doc.classes:
        i, had = 0, False
        while c.props.get(f"M{i}") is not None:
            c.props.remove(f"M{i}")
            i += 1
            had = True
        emptied += had
    pts = [(float(x), float(y)) for x, y in outline]
    set_props_vertices(doc.board_props, pts + [pts[0]])
    for r in doc.board_regions:
        r.set_shape(pts)
    added = []
    for k, (x, y, d) in enumerate(holes, 1):
        ed.add_pad(None, f"MH{k}", x, y, d, d, layer="MULTILAYER", shape="round", hole=d,
                   plated=False)
        added.append({"name": f"MH{k}", "x": x, "y": y, "hole": d, "plated": False})
    for k, (x, y, d, pad) in enumerate(plated_holes, len(added) + 1):
        ed.add_pad(None, f"MH{k}", x, y, pad, pad, layer="MULTILAYER", shape="round", hole=d)
        added.append({"name": f"MH{k}", "x": x, "y": y, "hole": d, "pad": pad, "plated": True})
    rep = {"removed": before, "classes_emptied": emptied, "outline": [list(p) for p in pts],
           "holes": added, "layers": [L.name(x) for x in doc.copper_layers],
           "rules": len(doc.rules)}
    return ed, rep


def _points(text):
    pts = []
    for tok in text.replace(";", " ").split():
        x, y = tok.split(",")
        pts.append((float(x), float(y)))
    return pts


def cmd_blank(args):
    import os
    from .pcbdoc import PcbDoc
    if os.path.abspath(args.out) == os.path.abspath(args.template):
        raise ValueError("refusing to write over the template")
    if args.outline:
        outline = _points(args.outline)
    elif args.rect:
        if args.at:
            x0, y0 = args.at
        else:
            t = PcbDoc.open(args.template).outline()
            x0, y0 = (min(p[0] for p in t), min(p[1] for p in t)) if t else (0.0, 0.0)
        outline = rect(args.rect[0], args.rect[1], x0, y0, args.corner)
    else:
        raise ValueError("give --rect W H or --outline")
    ed, rep = blank(args.template, outline, [tuple(h) for h in args.hole or []],
                    [tuple(h) for h in args.plated_hole or []])
    saved = ed.save(args.out)
    rep["saved"] = args.out
    rep["integrity"] = saved.get("integrity", saved.get("problems", []))
    print(json.dumps(rep, indent=1) if args.json else
          f"{args.out}: blank {len(rep['layers'])}-layer board, {len(outline)}-point outline, "
          f"{len(rep['holes'])} hole(s); removed {rep['removed']['components']} components, "
          f"{rep['removed']['nets']} nets, {rep['removed']['primitives']} primitives; "
          f"kept {rep['rules']} rules")
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("template")
        p.add_argument("-o", "--out", required=True)
        p.add_argument("--rect", nargs=2, type=float, metavar=("W", "H"))
        p.add_argument("--at", nargs=2, type=float, metavar=("X", "Y"))
        p.add_argument("--corner", type=float, default=0.0, help="45 degree corner cut, mm")
        p.add_argument("--outline", help="'x,y x,y ...' in mm")
        p.add_argument("--hole", nargs=3, type=float, action="append", metavar=("X", "Y", "D"))
        p.add_argument("--plated-hole", nargs=4, type=float, action="append",
                       metavar=("X", "Y", "D", "PAD"))
        p.add_argument("--json", action="store_true")
    command("blank", cmd_blank, "a new empty board from a template", build)
