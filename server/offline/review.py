"""Design-review checks on a parsed PcbDoc. Each returns plain dicts/lists (JSON-safe)."""
import math

from . import geom
from .pcbdoc import COPPER_LAYERS, NONE, layer_name


def summary(doc):
    b = doc.board
    x0, y0, x1, y1 = b.bbox
    copper = [l for l in b.layers if l.copper_mm]
    poured = {}
    for p in doc.polygons:
        key = p.layer
        poured[key] = poured.get(key, 0.0) + doc.poured_area(p.idx)
    return {
        "file": doc.path,
        "board_mm": {"width": round(x1 - x0, 3), "height": round(y1 - y0, 3), "area_mm2": round(b.area, 1),
                     "x_range": [round(x0, 3), round(x1, 3)], "y_range": [round(y0, 3), round(y1, 3)],
                     "corner_radius_mm": max([v.r for v in b.vertices if v.kind == 1] or [0.0])},
        "stackup": [{"layer": l.name, "copper_um": round(l.copper_mm * 1000, 1) if l.copper_mm else None,
                     "dielectric_mm": round(l.dielectric_mm, 4) if l.dielectric_mm else None,
                     "material": l.material} for l in b.layers if l.copper_mm or l.dielectric_mm],
        "copper_layers": len(copper),
        "copper_oz": [round(l.copper_mm * 1000 / 35.0, 2) for l in copper],
        "counts": {"components": len(doc.components), "pads": len(doc.pads), "vias": len(doc.vias),
                   "tracks": len(doc.tracks), "arcs": len(doc.arcs), "polygons": len(doc.polygons),
                   "nets": len(doc.nets), "rules": len(doc.rules)},
        "poured_copper_mm2": {k: round(v, 1) for k, v in poured.items()},
        "warnings": doc.warnings,
    }


def holes(doc, min_dia=0.0):
    """Every drilled hole (pad barrels and vias) with its distance to the board edge."""
    ring = doc.board.ring()
    out = []
    for p in doc.pads:
        if p.hole > min_dia:
            d = geom.point_to_ring_dist((p.x, p.y), ring)
            out.append({"kind": "pad", "ref": doc.comp_ref(p.comp) or "(free)", "pad": p.name,
                        "x": round(p.x, 3), "y": round(p.y, 3), "hole_mm": round(p.hole, 3),
                        "land_mm": [round(p.sx, 3), round(p.sy, 3)], "plated": p.plated,
                        "net": doc.net_name(p.net), "centre_to_edge_mm": round(d, 3),
                        "hole_edge_to_board_edge_mm": round(d - p.hole / 2, 3)})
    for v in doc.vias:
        if v.hole > min_dia:
            d = geom.point_to_ring_dist((v.x, v.y), ring)
            out.append({"kind": "via", "ref": "", "pad": "", "x": round(v.x, 3), "y": round(v.y, 3),
                        "hole_mm": round(v.hole, 3), "land_mm": [round(v.dia, 3)] * 2, "plated": True,
                        "net": doc.net_name(v.net), "centre_to_edge_mm": round(d, 3),
                        "hole_edge_to_board_edge_mm": round(d - v.hole / 2, 3)})
    return sorted(out, key=lambda h: -h["hole_mm"])


def mounting_holes(doc, min_dia=3.5):
    """Large holes that are not part of a connector or terminal footprint (board fasteners)."""
    return [h for h in holes(doc, min_dia) if h["kind"] == "pad" and h["ref"] == "(free)"]


def edge_items(doc, within=3.0):
    """Components whose pads come within `within` mm of the board edge, plus overhang."""
    ring = doc.board.ring()
    out = []
    for c in doc.components:
        pads = [p for p in doc.pads if p.comp == c.idx]
        if not pads:
            continue
        dmin = min(geom.point_to_ring_dist((p.x, p.y), ring) - max(p.sx, p.sy) / 2 for p in pads)
        outside = [p for p in pads if not geom.point_in_polygon((p.x, p.y), ring)]
        bodies = [b for b in doc.bodies if b.comp == c.idx and b.outline]
        body_out = 0.0
        for b in bodies:
            for q in b.outline:
                if not geom.point_in_polygon(q, ring):
                    body_out = max(body_out, geom.point_to_ring_dist(q, ring))
        if dmin < within or outside or body_out > 0.05:
            out.append({"ref": c.designator, "pattern": c.pattern, "side": "bottom" if c.bottom else "top",
                        "nearest_pad_to_edge_mm": round(dmin, 2), "pads_outside_board": len(outside),
                        "body_overhang_mm": round(body_out, 2)})
    return sorted(out, key=lambda r: r["nearest_pad_to_edge_mm"])


def outline_clearance_violations(doc, gap=0.2, layers=(33, 34)):
    """Silkscreen (or other) tracks that touch, cross or lie outside the board edge, or sit
    closer than `gap` to it. Mirrors Altium's Board Outline Clearance check for primitives
    that can be tested from the file alone (it reports off-board objects too)."""
    ring = doc.board.ring()
    out = []
    for t in doc.tracks:
        if t.layer not in layers:
            continue
        a, b = (t.x1, t.y1), (t.x2, t.y2)
        d = geom.ring_to_ring_dist([a, b], ring) - t.width / 2
        ina, inb = geom.point_in_polygon(a, ring), geom.point_in_polygon(b, ring)
        why = None
        if ina != inb or d <= 0:
            why = "crosses edge"
        elif not ina and not inb:
            why = "outside board"
        elif d < gap:
            why = "closer than %.2f mm" % gap
        if why:
            out.append({"layer": layer_name(t.layer), "from": (round(a[0], 3), round(a[1], 3)),
                        "to": (round(b[0], 3), round(b[1], 3)), "gap_mm": round(max(d, 0), 3), "why": why})
    return out


# ---- fasteners --------------------------------------------------------------

# Bearing-face diameters (mm). Head = the screw head; with_washer = plain washer OD (ISO 7089).
SCREWS = {
    "M3-pan": {"head": 5.6, "washer": 7.0, "clear_hole": 3.4},
    "M3-cap": {"head": 5.5, "washer": 7.0, "clear_hole": 3.4},
    "M4-pan": {"head": 8.0, "washer": 9.0, "clear_hole": 4.5},
    "M4-cap": {"head": 7.0, "washer": 9.0, "clear_hole": 4.5},
    "M5-pan": {"head": 10.0, "washer": 10.0, "clear_hole": 5.5},
    "M5-cap": {"head": 8.5, "washer": 10.0, "clear_hole": 5.5},
    "M6-pan": {"head": 12.0, "washer": 12.0, "clear_hole": 6.6},
}


def copper_gaps(doc, pt, layer, radius, skip_pad=None):
    """Nearest copper of each net to `pt` on one layer, for copper within `radius` (mm).
    Distance is to the copper edge (0 = the point is on copper)."""
    found = {}

    def note(net, d):
        if d <= radius and d < found.get(net, 1e9):
            found[net] = d

    for t in doc.tracks:
        if t.layer == layer:
            note(t.net, geom.seg_point_dist(pt, (t.x1, t.y1), (t.x2, t.y2)) - t.width / 2)
    for a in doc.arcs:
        if a.layer == layer:
            d = abs(math.hypot(pt[0] - a.cx, pt[1] - a.cy) - a.r) - a.width / 2
            if math.hypot(pt[0] - a.cx, pt[1] - a.cy) < a.r + radius + 1:
                pts = geom.arc_points(a.cx, a.cy, a.r, a.sa, a.ea, 0.05)
                d = min(geom.seg_point_dist(pt, p, q) for p, q in zip(pts[:-1], pts[1:])) - a.width / 2
                note(a.net, d)
    for p in doc.pads:
        if p is skip_pad or not (p.layer == layer or p.layer == 74):
            continue
        if math.hypot(pt[0] - p.x, pt[1] - p.y) > radius + max(p.sx, p.sy):
            continue
        ring = p.ring()
        d = 0.0 if geom.point_in_polygon(pt, ring) else geom.point_to_ring_dist(pt, ring)
        note(p.net, d)
    for v in doc.vias:
        note(v.net, max(math.hypot(pt[0] - v.x, pt[1] - v.y) - v.dia / 2, 0.0))
    side = "TOP" if layer == 1 else "BOTTOM"
    for poly in doc.polygons:
        if poly.layer != side:
            continue
        for r in doc.polygon_regions(poly.idx):
            if r.kind != 0:
                continue
            on = geom.point_in_polygon(pt, r.outline) and not any(geom.point_in_polygon(pt, h) for h in r.holes)
            if on:
                note(poly.net, 0.0)
            else:
                d = geom.point_to_ring_dist(pt, r.outline) if not geom.point_in_polygon(pt, r.outline) else \
                    min(geom.point_to_ring_dist(pt, h) for h in r.holes if geom.point_in_polygon(pt, h))
                note(poly.net, d)
    return {doc.net_name(n) or "(no net)": round(max(d, 0.0), 2) for n, d in found.items()}


def fastener_report(doc, screw="M4-pan", washer=False, head_mm=None, edge_margin=1.0, hole_filter=None):
    """For each board mounting hole: does the head/washer reach the board edge, and which
    live copper lies under it? `head_mm` overrides the standard table."""
    spec = SCREWS.get(screw)
    if head_mm is None:
        if spec is None:
            raise ValueError("unknown screw %r; choose from %s or pass head_mm" % (screw, sorted(SCREWS)))
        head_mm = spec["washer"] if washer else spec["head"]
    ring = doc.board.ring()
    out = []
    for p in doc.pads:
        if p.comp != NONE or p.hole < 3.0:
            continue
        if hole_filter and not hole_filter(p):
            continue
        pt = (p.x, p.y)
        R = head_mm / 2
        d_edge = geom.point_to_ring_dist(pt, ring)
        row = {"x": round(p.x, 2), "y": round(p.y, 2), "hole_mm": round(p.hole, 2),
               "land_mm": round(max(p.sx, p.sy), 2), "own_net": doc.net_name(p.net) or "(none)",
               "head_mm": head_mm, "centre_to_edge_mm": round(d_edge, 2),
               "head_edge_to_board_edge_mm": round(d_edge - R, 2),
               "min_centre_to_edge_for_margin_mm": round(R + edge_margin, 2),
               "move_inward_mm": round(max(R + edge_margin - d_edge, 0.0), 2)}
        for lay, nm in ((1, "top"), (32, "bottom")):
            gaps = copper_gaps(doc, pt, lay, R + 1.0, skip_pad=p)
            under = {n: g for n, g in gaps.items() if g < R - p.hole / 2 - 0.0 or g <= R - p.hole / 2}
            row["copper_within_head_" + nm] = {n: g for n, g in sorted(gaps.items(), key=lambda kv: kv[1])
                                                if n != row["own_net"] and kv_under(g, R, p)}
        comps = []
        for c in doc.components:
            for q in doc.pads:
                if q.comp == c.idx:
                    d = max(math.hypot(q.x - p.x, q.y - p.y) - max(q.sx, q.sy) / 2, 0.0)
                    if d < R + 2.0:
                        comps.append((c.designator, round(d, 2)))
        best = {}
        for ref, d in comps:
            best[ref] = min(d, best.get(ref, 1e9))
        row["components_near_head"] = dict(sorted(best.items(), key=lambda kv: kv[1]))
        out.append(row)
    return out


def kv_under(gap, R, pad):
    """Copper is 'under the head' if its edge is inside the head radius."""
    return gap < R


# ---- rules -------------------------------------------------------------------

def dead_scopes(doc):
    """Rules whose scope mentions something that does not exist or is empty, so part of the
    scope can never match. A rule with `or` branches still works through its live branches;
    `effective_only_via` says what is left when every named branch is dead."""
    from .scope import predicates
    classes = {}
    for c in doc.classes:
        classes.setdefault((c.kind, c.name), []).extend(c.members)
    nets = {n.name for n in doc.nets}
    comps = {c.designator for c in doc.components}
    room_rules = {r.props.get("NAME") for r in doc.rules if "Room" in r.kind}
    out = []
    for r in doc.rules:
        for which, expr in (("scope1", r.scope1), ("scope2", r.scope2)):
            for name, arg in predicates(expr):
                n = name.lower()
                why = None
                if n == "innetclass":
                    if (0, arg) not in classes:
                        why = "no net class named %r exists" % arg
                    elif not classes[(0, arg)]:
                        why = "net class %r has no members" % arg
                elif n == "inpadclass":
                    if (3, arg) not in classes:
                        why = "no pad class named %r exists" % arg
                    elif not classes[(3, arg)]:
                        why = "pad class %r has no members" % arg
                elif n == "incomponentclass":
                    if (1, arg) not in classes:
                        why = "no component class named %r exists" % arg
                    elif not classes[(1, arg)]:
                        why = "component class %r has no members" % arg
                elif n == "innet" and arg not in nets:
                    why = "no net named %r" % arg
                elif n == "incomponent" and arg not in comps:
                    why = "no component %r" % arg
                elif n == "withinroom" and arg not in room_rules:
                    why = "no room named %r is defined" % arg
                if why:
                    out.append({"rule": r.name, "kind": r.kind, "priority": r.priority, "where": which,
                                "predicate": "%s('%s')" % (name, arg), "problem": why, "scope": expr})
    return out
