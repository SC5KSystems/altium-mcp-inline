"""Notch finder: same-net pours that nearly meet.

    python -m offline notches board.PcbDoc [--gap 0.5] [--net VBAT] [-o merge_plan.json]
        [--png notches.png] [--json]

Two pours of one net on one layer whose outlines come within `gap` mm of
each other (a part removed from between them leaves this) pour as two
pieces with a seam of bare board between them and a V-notch where the seam
opens at each end. Each such pair is reported with the seam (area, length,
widest point) and the notch tips at its ends, and a merged outline is
proposed: the two outlines closed over the seam (grown by half the gap and
shrunk back, mitred so square corners stay square). The plan sets the
first polygon's outline to that shape and then merges the pair with
merge_polygons, which picks the survivor by pour order. Repour afterwards.
"""

import json

from . import geom
from . import layers as L


def pours(doc, nets=None):
    out = []
    for p in doc.polygons:
        if p.net is None or str(p.kind).lower() not in ("polygon", "0", ""):
            continue
        if nets and doc.nets[p.net].name.upper() not in nets:
            continue
        g = geom.polygon_outline(p)
        if not g.is_empty:
            out.append((p, g))
    return out


def find(doc, gap=0.5, nets=None):
    """[{polygons, net, layer, gap_mm, seam, notch_tips, outline, ops}]."""
    geom.require()
    from .pour import pieces
    nets = {n.upper() for n in nets} if nets else None
    items = pours(doc, nets)
    found = []
    for i, (pa, ga) in enumerate(items):
        for pb, gb in items[i + 1:]:
            if pa.layer != pb.layer or pa.net != pb.net:
                continue
            d = ga.distance(gb)
            if d > gap:
                continue
            if ga.intersection(gb).area > 1e-6:
                continue                    # they overlap: one pour already
            both = ga.union(gb)
            r = gap / 2 + 0.01
            closed = both.buffer(r, join_style="mitre").buffer(-r, join_style="mitre")
            closed = closed.union(both)
            seam = closed.difference(both)
            seam_parts = [s for s in pieces(seam) if s.area > 1e-6]
            if closed.geom_type != "Polygon" or not seam_parts:
                continue
            seam_main = max(seam_parts, key=lambda s: s.area)
            rect = seam_main.minimum_rotated_rectangle
            corners = list(rect.exterior.coords)[:4]
            sides = [(corners[k], corners[(k + 1) % 4]) for k in range(4)]
            long_side = max(sides, key=lambda s: (s[0][0] - s[1][0]) ** 2 +
                            (s[0][1] - s[1][1]) ** 2)
            length = ((long_side[0][0] - long_side[1][0]) ** 2 +
                      (long_side[0][1] - long_side[1][1]) ** 2) ** 0.5
            # notch tips: the seam's two ends along its length
            ax = ((long_side[1][0] - long_side[0][0]) / max(length, 1e-9),
                  (long_side[1][1] - long_side[0][1]) / max(length, 1e-9))
            pts = list(seam_main.exterior.coords)
            proj = [(x * ax[0] + y * ax[1], (x, y)) for x, y in pts]
            tips = [min(proj)[1], max(proj)[1]]
            width = seam_main.area / max(length, 1e-9)
            outline = [[round(x, 4), round(y, 4)] for x, y in list(closed.exterior.coords)[:-1]]
            ref_a = pa.name or pa.index
            ref_b = pb.name or pb.index
            found.append({
                "polygons": [{"index": pa.index, "name": pa.name, "pour_index": pa.pour_index},
                             {"index": pb.index, "name": pb.name, "pour_index": pb.pour_index}],
                "net": doc.nets[pa.net].name, "layer": L.name(pa.layer),
                "gap_mm": round(d, 4),
                "seam": {"area_mm2": round(seam.area, 4), "length_mm": round(length, 3),
                         "mean_width_mm": round(width, 4), "pieces": len(seam_parts),
                         "box": [round(v, 3) for v in seam.bounds]},
                "notch_tips": [[round(x, 3), round(y, 3)] for x, y in tips],
                "outline": outline,
                "ops": [{"op": "set_polygon_outline", "polygon": ref_a, "outline": outline},
                        {"op": "merge_polygons", "polygons": [ref_a, ref_b]}],
            })
    found.sort(key=lambda f: f["gap_mm"])
    return found


def cmd_notches(args):
    from .pcbdoc import PcbDoc
    doc = PcbDoc.open(args.board)
    found = find(doc, args.gap, args.net)
    if args.out:
        if not found:
            raise ValueError("no notches found; nothing to plan")
        ops = []
        for f in found:
            ops.extend(f["ops"])
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "ops": ops}, fh, indent=1)
    if args.png:
        from . import render
        x0 = min(f["seam"]["box"][0] for f in found) - 5 if found else None
        if found:
            box = (x0, min(f["seam"]["box"][1] for f in found) - 5,
                   max(f["seam"]["box"][2] for f in found) + 5,
                   max(f["seam"]["box"][3] for f in found) + 5)
            markers = [{"x": t[0], "y": t[1], "label": "notch"} for f in found
                       for t in f["notch_tips"]]
            render.render(doc, args.png, box=box, markers=markers)
    if args.json:
        print(json.dumps({"gap_mm": args.gap, "pairs": [{k: v for k, v in f.items()
                                                          if k != "ops"} for f in found]},
                         indent=1))
    else:
        if not found:
            print(f"no same-net pours within {args.gap} mm of each other")
        for f in found:
            a, b = f["polygons"]
            print(f"{f['net']} on {f['layer']}: polygons {a['index']} and {b['index']} "
                  f"{f['gap_mm']} mm apart; seam {f['seam']['length_mm']} mm long, "
                  f"{f['seam']['mean_width_mm']} mm wide; notch tips at {f['notch_tips']}")
        if args.out and found:
            print(f"merge plan ({len(found) * 2} ops) -> {args.out}; repour afterwards")
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("board")
        p.add_argument("--gap", type=float, default=0.5)
        p.add_argument("--net", nargs="+")
        p.add_argument("-o", "--out", help="write an EditPlan that merges each pair")
        p.add_argument("--png")
        p.add_argument("--json", action="store_true")
    command("notches", cmd_notches, "same-net pours that nearly meet: seams, notches, merges",
            build)
