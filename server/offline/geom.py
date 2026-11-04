"""Shapely geometry for board primitives, in mm.

shapely is an optional dependency of the offline tools: modules that need
real geometry (connectivity, pours, DRC) import it through require().
"""

import math

from . import layers as L

_SHAPELY = None


def require():
    global _SHAPELY
    if _SHAPELY is None:
        try:
            import shapely
            from shapely import geometry, ops, affinity, strtree  # noqa: F401
        except ImportError as e:
            raise ImportError("this offline tool needs shapely 2.x: pip install -r "
                              "server/offline/requirements.txt") from e
        if int(shapely.__version__.split(".")[0]) < 2:
            raise ImportError(f"shapely {shapely.__version__} is too old; 2.x is required")
        _SHAPELY = shapely
    return _SHAPELY


def arc_points(cx, cy, r, a1, a2, tol=0.005, max_step=10.0, ccw=True):
    """Points along an arc from a1 to a2 (degrees), chord error <= tol mm."""
    sweep = (a2 - a1) % 360.0 if ccw else (a1 - a2) % 360.0
    if sweep == 0:
        sweep = 360.0
    if r <= 0:
        return [(cx, cy)]
    step = max_step
    if r > tol:
        step = min(max_step, math.degrees(2 * math.acos(max(-1.0, 1 - tol / r))))
    n = max(2, int(math.ceil(sweep / max(step, 0.5))) + 1)
    sgn = 1 if ccw else -1
    return [(cx + r * math.cos(math.radians(a1 + sgn * sweep * i / (n - 1))),
             cy + r * math.sin(math.radians(a1 + sgn * sweep * i / (n - 1)))) for i in range(n)]


def stadium(x1, y1, x2, y2, width, quad_segs=8):
    sh = require()
    if math.hypot(x2 - x1, y2 - y1) < 1e-9:
        return sh.Point(x1, y1).buffer(width / 2, quad_segs=quad_segs)
    return sh.LineString([(x1, y1), (x2, y2)]).buffer(width / 2, quad_segs=quad_segs)


def track_shape(t, extra=0.0):
    return stadium(t.x1, t.y1, t.x2, t.y2, t.width + 2 * extra)


def arc_shape(a, extra=0.0, tol=0.005):
    """An arc track as copper. The centre line's chords cut inside the true
    arc by at most `tol` mm, so the concave edge may sit up to `tol` too far
    in; checks that measure gaps pass a finer tol."""
    sh = require()
    pts = arc_points(a.cx, a.cy, a.radius, a.start_angle, a.end_angle, tol=tol)
    w = a.width + 2 * extra
    if len(pts) < 2:
        return sh.Point(a.cx, a.cy).buffer(w / 2)
    return sh.LineString(pts).buffer(w / 2, quad_segs=8)


def via_shape(v, layer=None, extra=0.0):
    sh = require()
    d = v.diameter_on(layer) if layer is not None else v.diameter
    return sh.Point(v.x, v.y).buffer(d / 2 + extra, quad_segs=16)


def _rotate(geom, angle, x, y):
    if not angle % 360:
        return geom
    from shapely import affinity
    return affinity.rotate(geom, angle, origin=(x, y))


def pad_shape(p, layer, extra=0.0):
    """Copper of pad p on `layer`, grown by `extra` mm (negative shrinks)."""
    sh = require()
    shape, (sx, sy), frac = p.shape_on(layer)
    sx, sy = sx + 2 * extra, sy + 2 * extra
    x, y = p.x, p.y
    if sx <= 0 or sy <= 0:
        return sh.Polygon()
    if shape == "round":
        if abs(sx - sy) < 1e-9:
            g = sh.Point(x, y).buffer(sx / 2, quad_segs=16)
        elif sx > sy:
            g = stadium(x - (sx - sy) / 2, y, x + (sx - sy) / 2, y, sy)
        else:
            g = stadium(x, y - (sy - sx) / 2, x, y + (sy - sx) / 2, sx)
    elif shape == "octagonal":
        c = min(sx, sy) * 0.25
        hx, hy = sx / 2, sy / 2
        g = sh.Polygon([(x - hx + c, y - hy), (x + hx - c, y - hy), (x + hx, y - hy + c),
                        (x + hx, y + hy - c), (x + hx - c, y + hy), (x - hx + c, y + hy),
                        (x - hx, y + hy - c), (x - hx, y - hy + c)])
    elif shape == "roundrect" and frac > 0:
        r = min(sx, sy) / 2 * min(frac, 1.0)
        g = sh.box(x - sx / 2 + r, y - sy / 2 + r, x + sx / 2 - r, y + sy / 2 - r).buffer(
            r, quad_segs=8) if r > 1e-9 else sh.box(x - sx / 2, y - sy / 2, x + sx / 2, y + sy / 2)
    else:
        g = sh.box(x - sx / 2, y - sy / 2, x + sx / 2, y + sy / 2)
    return _rotate(g, p.rotation, x, y)


def hole_shape(obj, extra=0.0):
    """Drill hole of a pad or via (slots included)."""
    sh = require()
    d = obj.hole + 2 * extra
    if d <= 0:
        return sh.Polygon()
    if obj.KIND == "pad" and obj.hole_shape == "slot" and obj.slot_size > obj.hole:
        L_ = obj.slot_size + 2 * extra
        g = stadium(obj.x - (L_ - d) / 2, obj.y, obj.x + (L_ - d) / 2, obj.y, d)
        return _rotate(g, obj.rotation + obj.slot_rotation, obj.x, obj.y)
    if obj.KIND == "pad" and obj.hole_shape == "square":
        g = sh.box(obj.x - d / 2, obj.y - d / 2, obj.x + d / 2, obj.y + d / 2)
        return _rotate(g, obj.rotation + obj.hole_rotation, obj.x, obj.y)
    return sh.Point(obj.x, obj.y).buffer(d / 2, quad_segs=16)


def fill_shape(f, extra=0.0):
    sh = require()
    g = sh.Polygon(f.corners())
    return g.buffer(extra, join_style="mitre") if extra else g


def ring(points):
    out = list(points)
    return out if len(out) >= 3 else None


def region_shape(r, extra=0.0, arc_step_deg=5.0):
    sh = require()
    outer = ring(r.outline(arc_step_deg))
    if outer is None:
        return sh.Polygon()
    holes = [h for h in (ring(x) for x in r.holes) if h]
    g = sh.Polygon(outer, holes)
    if not g.is_valid:
        g = sh.make_valid(g)
    return g.buffer(extra) if extra else g


def text_box(t):
    """Rough footprint of a text string: its box, rotated (mm)."""
    sh = require()
    w = max(len(t.text), 1) * t.height * 0.8
    g = sh.box(t.x, t.y, t.x + w, t.y + t.height)
    return _rotate(g, t.rotation, t.x, t.y)


def copper_shape(obj, layer, extra=0.0, fine=False):
    """Copper of a primitive on a layer. fine=True approximates arcs closely
    enough (0.2 um) for clearance measurements."""
    k = obj.KIND
    if k == "track":
        return track_shape(obj, extra)
    if k == "arc":
        return arc_shape(obj, extra, tol=0.0002 if fine else 0.005)
    if k == "via":
        return via_shape(obj, layer, extra)
    if k == "pad":
        return pad_shape(obj, layer, extra)
    if k == "fill":
        return fill_shape(obj, extra)
    if k == "region":
        return region_shape(obj, extra, arc_step_deg=1.0 if fine else 5.0)
    raise ValueError(f"no copper shape for {k}")


def polygon_outline(poly):
    sh = require()
    pts = ring(poly.outline())
    if pts is None:
        return sh.Polygon()
    g = sh.Polygon(pts)
    return g if g.is_valid else sh.make_valid(g)


def board_shape(doc):
    """Board outline minus board cutouts."""
    sh = require()
    pts = ring(doc.outline())
    if pts is None:
        return None
    g = sh.Polygon(pts)
    if not g.is_valid:
        g = sh.make_valid(g)
    for c in doc.board_cutouts():
        if len(c) >= 3:
            g = g.difference(sh.Polygon(c))
    return g


def layer_items(doc, layer, nets=None, kinds=("track", "arc", "via", "pad", "fill", "region")):
    """[(obj, geometry)] for copper on one layer (optionally only some nets)."""
    out = []
    for obj in doc.copper(layer=layer):
        if obj.KIND not in kinds:
            continue
        if nets is not None and obj.effective_net not in nets:
            continue
        g = copper_shape(obj, layer)
        if not g.is_empty:
            out.append((obj, g))
    return out
