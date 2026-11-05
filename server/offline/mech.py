"""Mechanical checks of a board: drilled holes, mounting holes, screw heads,
parts near the edge and silkscreen over it.

    python -m offline holes board.PcbDoc [--min 3.5] [--mounting] [--json]
    python -m offline fasteners board.PcbDoc [--screw M4-pan] [--washer] [--head 8]
        [--margin 1.0] [--json]
    python -m offline edges board.PcbDoc [--within 3.0] [--gap 0.2] [--json]

Distances to the board edge are to the nearest edge of the board shape,
cutouts included. A mounting hole is a drilled pad that belongs to no
component. Ported from the phase-6 branch (98b87ff) onto this reader.
"""

import json

from . import geom
from . import layers as L

# Bearing-face diameters (mm). head = the screw head; washer = plain washer
# outside diameter (ISO 7089).
SCREWS = {
    "M3-pan": {"head": 5.6, "washer": 7.0, "clear_hole": 3.4},
    "M3-cap": {"head": 5.5, "washer": 7.0, "clear_hole": 3.4},
    "M4-pan": {"head": 8.0, "washer": 9.0, "clear_hole": 4.5},
    "M4-cap": {"head": 7.0, "washer": 9.0, "clear_hole": 4.5},
    "M5-pan": {"head": 10.0, "washer": 10.0, "clear_hole": 5.5},
    "M5-cap": {"head": 8.5, "washer": 10.0, "clear_hole": 5.5},
    "M6-pan": {"head": 12.0, "washer": 12.0, "clear_hole": 6.6},
}


def _board(doc):
    b = geom.board_shape(doc)
    if b is None or b.is_empty:
        raise ValueError("the board has no outline")
    return b


def _r(v, n=3):
    return round(float(v), n)


def holes(doc, min_dia=0.0):
    """Every drilled hole larger than min_dia (pad barrels and vias) with its
    distance to the board edge, largest first."""
    sh = geom.require()
    board = _board(doc)
    edge = board.boundary
    out = []
    for p in doc.pads:
        if p.hole > min_dia:
            d = edge.distance(sh.Point(p.x, p.y))
            sx, sy = p.top_size
            out.append({"kind": "pad", "ref": doc.components[p.component].designator
                        if p.component is not None else "(free)", "pad": p.name,
                        "x": _r(p.x), "y": _r(p.y), "hole_mm": _r(p.hole),
                        "land_mm": [_r(sx), _r(sy)], "plated": p.plated, "net": p.net_name,
                        "centre_to_edge_mm": _r(d), "hole_edge_to_board_edge_mm": _r(d - p.hole / 2)})
    for v in doc.vias:
        if v.hole > min_dia:
            d = edge.distance(sh.Point(v.x, v.y))
            out.append({"kind": "via", "ref": "", "pad": "", "x": _r(v.x), "y": _r(v.y),
                        "hole_mm": _r(v.hole), "land_mm": [_r(v.diameter)] * 2, "plated": True,
                        "net": doc.nets[v.net].name if v.net is not None else None,
                        "centre_to_edge_mm": _r(d), "hole_edge_to_board_edge_mm": _r(d - v.hole / 2)})
    return sorted(out, key=lambda h: -h["hole_mm"])


def mounting_holes(doc, min_dia=3.5):
    """Large holes that belong to no component (board fasteners)."""
    return [h for h in holes(doc, min_dia) if h["kind"] == "pad" and h["ref"] == "(free)"]


def edge_items(doc, within=3.0):
    """Components whose pads come within `within` mm of the board edge, lie
    outside it, or whose body hangs over it."""
    board = _board(doc)
    edge = board.boundary
    pads, bodies = {}, {}
    for p in doc.pads:
        if p.component is not None:
            pads.setdefault(p.component, []).append(p)
    for b in doc.bodies:
        if b.component is not None:
            bodies.setdefault(b.component, []).append(b)
    out = []
    for c in doc.components:
        ps = pads.get(c.index, [])
        if not ps:
            continue
        shapes = [geom.pad_shape(p, L.TOP if p.on_layer(L.TOP) else L.BOTTOM) for p in ps]
        shapes = [s for s in shapes if not s.is_empty]
        if not shapes:
            continue
        dmin = min(edge.distance(s) if board.contains(s) else 0.0 for s in shapes)
        outside = sum(1 for s in shapes if not board.contains(s))
        over = 0.0
        for b in bodies.get(c.index, []):
            g = geom.region_shape(b)
            if g.is_empty:
                continue
            rest = g.difference(board)
            if not rest.is_empty:
                pt = geom.require().Point
                over = max([over] + [board.distance(pt(x, y)) for x, y in _coords(rest)])
        if dmin < within or outside or over > 0.05:
            out.append({"ref": c.designator, "pattern": c.pattern, "side": c.side,
                        "nearest_pad_to_edge_mm": _r(dmin, 2), "pads_outside_board": outside,
                        "body_overhang_mm": _r(over, 2)})
    return sorted(out, key=lambda r: r["nearest_pad_to_edge_mm"])


def _coords(g):
    if hasattr(g, "geoms"):
        for part in g.geoms:
            yield from _coords(part)
    elif hasattr(g, "exterior"):
        yield from g.exterior.coords
    elif hasattr(g, "coords"):
        yield from g.coords


def outline_clearance(doc, gap=0.2, layers=(L.TOP_OVERLAY, L.BOTTOM_OVERLAY)):
    """Silkscreen tracks and arcs that cross the board edge, lie outside it or
    come closer than `gap`: what Altium's Board Outline Clearance check flags
    for silkscreen."""
    board = _board(doc)
    edge = board.boundary
    out = []
    for obj in list(doc.tracks) + list(doc.arcs):
        if obj.layer not in layers:
            continue
        s = geom.track_shape(obj) if obj.KIND == "track" else geom.arc_shape(obj)
        if s.is_empty:
            continue
        d = edge.distance(s)
        inside = board.contains(s)
        if not inside and board.intersects(s):
            why = "crosses edge"
        elif not inside:
            why = "outside board"
        elif d < gap:
            why = f"closer than {gap:.2f} mm"
        else:
            continue
        x0, y0, x1, y1 = s.bounds
        out.append({"kind": obj.KIND, "layer": L.name(obj.layer),
                    "box": [_r(x0), _r(y0), _r(x1), _r(y1)], "gap_mm": _r(d if inside else 0.0),
                    "why": why})
    return out


def copper_near(doc, x, y, layer, radius, skip=None, items=None):
    """{net: distance from (x, y) to that net's copper edge} for copper within
    `radius` mm on one layer (0 = the point is on copper). Poured copper
    counts with its polygon's net. items: geom.layer_items(doc, layer), when
    the caller asks many times."""
    sh = geom.require()
    pt = sh.Point(x, y)
    found = {}
    for obj, g in items if items is not None else geom.layer_items(doc, layer):
        if obj is skip:
            continue
        d = g.distance(pt)
        if d <= radius:
            n = doc.nets[obj.effective_net].name if obj.effective_net is not None else "(no net)"
            found[n] = min(d, found.get(n, 1e9))
    return {n: _r(d, 2) for n, d in sorted(found.items(), key=lambda kv: kv[1])}


def fastener_report(doc, screw="M4-pan", washer=False, head_mm=None, edge_margin=1.0,
                    min_hole=3.0):
    """For each mounting hole: how far the screw head (or washer) is from the
    board edge, how far the hole must move for `edge_margin`, and which other
    nets' copper and which components lie under the head."""
    sh = geom.require()
    if head_mm is None:
        spec = SCREWS.get(screw)
        if spec is None:
            raise ValueError(f"unknown screw {screw!r}; choose from {sorted(SCREWS)} or give head_mm")
        head_mm = spec["washer"] if washer else spec["head"]
    board = _board(doc)
    edge = board.boundary
    R = head_mm / 2
    comp_pads = [(doc.components[q.component].designator, q) for q in doc.pads
                 if q.component is not None]
    items = {}
    out = []
    for p in doc.pads:
        if p.component is not None or p.hole < min_hole:
            continue
        d_edge = edge.distance(sh.Point(p.x, p.y))
        own = p.net_name or "(none)"
        row = {"x": _r(p.x, 2), "y": _r(p.y, 2), "hole_mm": _r(p.hole, 2),
               "land_mm": _r(max(p.top_size), 2), "own_net": own, "head_mm": head_mm,
               "centre_to_edge_mm": _r(d_edge, 2),
               "head_edge_to_board_edge_mm": _r(d_edge - R, 2),
               "min_centre_to_edge_for_margin_mm": _r(R + edge_margin, 2),
               "move_inward_mm": _r(max(R + edge_margin - d_edge, 0.0), 2)}
        for lay, side in ((L.TOP, "top"), (L.BOTTOM, "bottom")):
            if lay not in items:
                items[lay] = geom.layer_items(doc, lay)
            near = copper_near(doc, p.x, p.y, lay, R, skip=p, items=items[lay])
            row["copper_within_head_" + side] = {n: d for n, d in near.items()
                                                 if n != own and d < R}
        head = sh.Point(p.x, p.y).buffer(R + 2.0)
        best = {}
        for des, q in comp_pads:
            for lay in (L.TOP, L.BOTTOM):
                if q.on_layer(lay):
                    g = geom.pad_shape(q, lay)
                    if not g.is_empty and g.intersects(head):
                        d = g.distance(sh.Point(p.x, p.y))
                        best[des] = min(_r(d, 2), best.get(des, 1e9))
        row["components_near_head"] = dict(sorted(best.items(), key=lambda kv: kv[1]))
        out.append(row)
    return out


# ---------------------------------------------------------------- CLI

def cmd_holes(args):
    from .pcbdoc import PcbDoc
    doc = PcbDoc.open(args.board)
    rows = mounting_holes(doc, args.min) if args.mounting else holes(doc, args.min)
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    for h in rows:
        print(f"  {h['kind']:4s} {h['ref']:>8s} {h['pad']:>4s} ({h['x']}, {h['y']}) "
              f"hole {h['hole_mm']} mm, {h['hole_edge_to_board_edge_mm']} mm to the edge")
    print(f"{len(rows)} hole(s)")
    return 0


def cmd_fasteners(args):
    from .pcbdoc import PcbDoc
    rows = fastener_report(PcbDoc.open(args.board), args.screw, args.washer, args.head,
                           args.margin)
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    for r in rows:
        print(f"  hole ({r['x']}, {r['y']}) {r['hole_mm']} mm: head edge "
              f"{r['head_edge_to_board_edge_mm']} mm from the board edge"
              + (f", move {r['move_inward_mm']} mm inward" if r["move_inward_mm"] else ""))
        for side in ("top", "bottom"):
            if r["copper_within_head_" + side]:
                print(f"    under the head ({side}): {r['copper_within_head_' + side]}")
        if r["components_near_head"]:
            print(f"    parts near the head: {r['components_near_head']}")
    print(f"{len(rows)} mounting hole(s)")
    return 0


def cmd_edges(args):
    from .pcbdoc import PcbDoc
    doc = PcbDoc.open(args.board)
    rep = {"near_or_over_edge": edge_items(doc, args.within),
           "silkscreen_outline_clearance": outline_clearance(doc, args.gap)}
    if args.json:
        print(json.dumps(rep, indent=1))
        return 0
    for r in rep["near_or_over_edge"]:
        print(f"  {r['ref']} ({r['pattern']}, {r['side']}): pad {r['nearest_pad_to_edge_mm']} mm "
              f"from the edge, {r['pads_outside_board']} pad(s) outside, body over by "
              f"{r['body_overhang_mm']} mm")
    for r in rep["silkscreen_outline_clearance"]:
        print(f"  silkscreen {r['kind']} on {r['layer']} at {r['box']}: {r['why']}")
    return 0


def register_commands(command):
    def b_holes(p):
        p.add_argument("board")
        p.add_argument("--min", type=float, default=0.0, help="smallest hole to list, mm")
        p.add_argument("--mounting", action="store_true", help="only holes of no component")
        p.add_argument("--json", action="store_true")
    command("holes", cmd_holes, "drilled holes and their distance to the board edge", b_holes)

    def b_fast(p):
        p.add_argument("board")
        p.add_argument("--screw", default="M4-pan", choices=sorted(SCREWS))
        p.add_argument("--washer", action="store_true")
        p.add_argument("--head", type=float, help="bearing diameter, mm (overrides --screw)")
        p.add_argument("--margin", type=float, default=1.0, help="head to board edge, mm")
        p.add_argument("--json", action="store_true")
    command("fasteners", cmd_fasteners, "screw heads against the board edge and copper", b_fast)

    def b_edges(p):
        p.add_argument("board")
        p.add_argument("--within", type=float, default=3.0)
        p.add_argument("--gap", type=float, default=0.2, help="silkscreen to edge, mm")
        p.add_argument("--json", action="store_true")
    command("edges", cmd_edges, "parts near or over the board edge; silkscreen over it", b_edges)
