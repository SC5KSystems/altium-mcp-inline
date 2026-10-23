"""Small 2-D geometry helpers (millimetres, y up). Stdlib only."""
import math


def polygon_area(pts):
    """Signed area; positive for counter-clockwise."""
    n = len(pts)
    if n < 3:
        return 0.0
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))


def bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def point_in_polygon(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi:
            inside = not inside
        j = i
    return inside


def seg_point_dist(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _ccw(a, b, c):
    return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])


def segs_intersect(a, b, c, d):
    d1, d2 = _ccw(a, b, c), _ccw(a, b, d)
    d3, d4 = _ccw(c, d, a), _ccw(c, d, b)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def seg_seg_dist(a, b, c, d):
    if segs_intersect(a, b, c, d):
        return 0.0
    return min(seg_point_dist(a, c, d), seg_point_dist(b, c, d),
               seg_point_dist(c, a, b), seg_point_dist(d, a, b))


def point_to_ring_dist(p, ring):
    """Distance from a point to the boundary of a closed polyline."""
    n = len(ring)
    return min(seg_point_dist(p, ring[i], ring[(i + 1) % n]) for i in range(n))


def ring_to_ring_dist(r1, r2):
    """Minimum boundary-to-boundary distance between two closed polylines (0 if they cross)."""
    best = float("inf")
    n1, n2 = len(r1), len(r2)
    for i in range(n1):
        a, b = r1[i], r1[(i + 1) % n1]
        for j in range(n2):
            d = seg_seg_dist(a, b, r2[j], r2[(j + 1) % n2])
            if d < best:
                best = d
                if best == 0.0:
                    return 0.0
    return best


def arc_points(cx, cy, r, sa_deg, ea_deg, tol=0.01):
    """Points along a counter-clockwise arc, endpoints included."""
    sweep = (ea_deg - sa_deg) % 360.0
    if sweep == 0.0 and ea_deg != sa_deg:
        sweep = 360.0
    step = 2 * math.degrees(math.acos(max(0.0, 1.0 - tol / r))) if r > tol else 30.0
    n = max(2, int(math.ceil(sweep / max(step, 1.0))) + 1)
    return [(cx + r * math.cos(math.radians(sa_deg + sweep * k / (n - 1))),
             cy + r * math.sin(math.radians(sa_deg + sweep * k / (n - 1)))) for k in range(n)]


def rotate(pt, deg, about=(0.0, 0.0)):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    x, y = pt[0] - about[0], pt[1] - about[1]
    return (about[0] + x * c - y * s, about[1] + x * s + y * c)


def rect_ring(cx, cy, w, h, rot_deg=0.0):
    pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    return [rotate((cx + x, cy + y), rot_deg, (cx, cy)) for x, y in pts]


def circle_ring(cx, cy, r, n=32):
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def stadium_ring(cx, cy, w, h, rot_deg=0.0, n=8):
    """Obround: a rectangle with fully rounded short ends."""
    r = min(w, h) / 2
    pts = []
    if w >= h:
        half = w / 2 - r
        for k in range(n + 1):
            a = -math.pi / 2 + math.pi * k / n
            pts.append((half + r * math.cos(a), r * math.sin(a)))
        for k in range(n + 1):
            a = math.pi / 2 + math.pi * k / n
            pts.append((-half + r * math.cos(a), r * math.sin(a)))
    else:
        half = h / 2 - r
        for k in range(n + 1):
            a = 0 + math.pi * k / n
            pts.append((r * math.cos(a), half + r * math.sin(a)))
        for k in range(n + 1):
            a = math.pi + math.pi * k / n
            pts.append((r * math.cos(a), -half + r * math.sin(a)))
    return [rotate((cx + x, cy + y), rot_deg, (cx, cy)) for x, y in pts]
