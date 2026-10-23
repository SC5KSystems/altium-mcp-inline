"""How each pad is actually tied into a pour, measured from the saved copper.

Rules say what Altium *should* do; the poured regions say what it *did*. For a pad,
sample a ring just outside its land and see where same-net pour copper crosses it:

  - copper all the way round  -> merged into the pour (direct)
  - a few separate crossings  -> thermal relief; each crossing is a spoke, its arc
                                  length is the spoke width
  - no copper                 -> not tied into that polygon on this layer

Sampling is deliberately simple (a few hundred points per ring), so widths are good
to about the sampling step (~0.02 mm), which is finer than a 0.2 mm spoke needs.
"""
import math

from . import geom
from .pcbdoc import NONE
from .scope import Ctx, Scope

SAMPLES = 720


def _covered(pt, regions, tracks=()):
    for ring, holes in regions:
        if geom.point_in_polygon(pt, ring) and not any(geom.point_in_polygon(pt, h) for h in holes):
            return True
    for x1, y1, x2, y2, w in tracks:
        if geom.seg_point_dist(pt, (x1, y1), (x2, y2)) <= w / 2:
            return True
    return False


def _ring_points(pad, offset):
    """Points evenly spaced along the pad outline grown by `offset`, plus the step length."""
    if pad.shape == 1 and abs(pad.sx - pad.sy) < 1e-6:
        r = pad.sx / 2 + offset
        pts = [(pad.x + r * math.cos(2 * math.pi * k / SAMPLES), pad.y + r * math.sin(2 * math.pi * k / SAMPLES))
               for k in range(SAMPLES)]
        return pts, 2 * math.pi * r / SAMPLES
    if pad.shape in (1, 9):
        base = geom.stadium_ring(pad.x, pad.y, pad.sx + 2 * offset, pad.sy + 2 * offset, pad.rotation, 24)
    else:
        base = geom.rect_ring(pad.x, pad.y, pad.sx + 2 * offset, pad.sy + 2 * offset, pad.rotation)
    perim = sum(math.dist(base[i], base[(i + 1) % len(base)]) for i in range(len(base)))
    step = perim / SAMPLES
    pts, walked, i = [], 0.0, 0
    target = 0.0
    segs = [(base[i], base[(i + 1) % len(base)]) for i in range(len(base))]
    for a, b in segs:
        L = math.dist(a, b)
        while target <= walked + L and len(pts) < SAMPLES:
            t = (target - walked) / L if L else 0.0
            pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            target += step
        walked += L
    return pts, step


def pad_tie(doc, pad, polygon, gap=0.203):
    """Measure how `pad` joins `polygon`. Returns a dict, or None if the pad is not inside it.

    Same-net copper that is not part of the pour (footprint regions, tracks) counts as
    copper too: Altium pours over it, so it carries current into the pad just the same."""
    if not geom.point_in_polygon((pad.x, pad.y), polygon.ring()):
        return None
    lay = 1 if polygon.layer == "TOP" else 32
    regions = [(r.outline, r.holes) for r in doc.polygon_regions(polygon.idx) if r.kind == 0]
    if not regions:
        return None
    regions += [(r.outline, r.holes) for r in doc.regions
                if r.poly == NONE and r.kind == 0 and r.layer == lay and r.net == pad.net]
    tracks = [(t.x1, t.y1, t.x2, t.y2, t.width) for t in doc.tracks if t.layer == lay and t.net == pad.net
              and geom.seg_point_dist((pad.x, pad.y), (t.x1, t.y1), (t.x2, t.y2)) < max(pad.sx, pad.sy) + 1.0]
    pts, step = _ring_points(pad, gap / 2)
    flags = [_covered(p, regions, tracks) for p in pts]
    n = len(flags)
    if all(flags):
        return {"style": "direct", "spokes": 0, "spoke_widths_mm": [], "tie_width_mm": None}
    if not any(flags):
        return {"style": "none", "spokes": 0, "spoke_widths_mm": [], "tie_width_mm": 0.0}
    # contiguous runs of covered samples, wrapping around the start
    start = next(i for i in range(n) if not flags[i])
    runs, cur = [], 0
    for k in range(1, n + 1):
        if flags[(start + k) % n]:
            cur += 1
        elif cur:
            runs.append(cur * step)
            cur = 0
    if cur:
        runs.append(cur * step)
    return {"style": "relief", "spokes": len(runs), "spoke_widths_mm": [round(w, 3) for w in runs],
            "tie_width_mm": round(sum(runs), 3)}


def governing_rule(doc, scope, pad, polygon):
    """The PolygonConnect rule Altium should apply (lowest priority number wins)."""
    cands = []
    for r in doc.rules:
        if r.kind != "PolygonConnect" or not r.enabled:
            continue
        pc = Ctx(doc, "polygon", net=doc.net_name(polygon.net), layer=polygon.layer, polygon=polygon.name)
        xc = Ctx(doc, "pad", net=doc.net_name(pad.net), comp=doc.comp_ref(pad.comp), pad=pad.name)
        if scope.matches(r.scope1, pc) and scope.matches(r.scope2, xc):
            cands.append(r)
    return min(cands, key=lambda r: r.priority) if cands else None


def pad_connections(doc, nets, gap=0.203):
    """One row per (pad, polygon) for pads on the given net names."""
    scope = Scope(doc)
    want = {doc.net_index(n) for n in nets if doc.net_index(n) is not None}
    rows = []
    for pad in doc.pads:
        if pad.net not in want:
            continue
        for poly in doc.polygons:
            if poly.net != pad.net:
                continue
            side = 1 if poly.layer == "TOP" else 32
            if pad.layer not in (side, 74):
                continue
            tie = pad_tie(doc, pad, poly, gap)
            if tie is None:
                continue
            rule = governing_rule(doc, scope, pad, poly)
            style_key = "SMDPAD" if pad.hole == 0 else "THPAD"
            intended = None
            if rule is not None:
                intended = (rule.props.get(style_key + ".CONNECTSTYLE") or rule.props.get("CONNECTSTYLE") or "")
            rows.append(dict(ref=doc.comp_ref(pad.comp) or "(free)", pad=pad.name, net=doc.net_name(pad.net),
                             layer=poly.layer, x=round(pad.x, 2), y=round(pad.y, 2),
                             rule=rule.name if rule else None, rule_style=intended, **tie))
    return rows, sorted(scope.unknown)
