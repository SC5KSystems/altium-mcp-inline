"""Geometry for the board editing tools.

Pure Python with no Altium or win32 dependency, so it runs and is tested
anywhere (server/tests/test_board_geometry.py). main.py turns tool arguments
into these structures and hands Altium a plain pipe-delimited spec file; the
DelphiScript side only has to apply numbers it is given.

Conventions, matching the other PCB tools:
  * Coordinates and sizes are mils, relative to the board origin.
  * Angles are degrees. Arcs run counterclockwise from start_angle to
    end_angle, as Altium's do.

An outline is a list of segments. Segment i starts at its vertex (x, y) and
runs to the next segment's vertex; the last one wraps to the first:

    {"kind": "line", "x": .., "y": ..}
    {"kind": "arc",  "x": .., "y": .., "cx": .., "cy": .., "radius": ..,
     "start_angle": .., "end_angle": ..}

This mirrors Altium's polygon segment record (TPolySegment: Kind, vx, vy, cx,
cy, Radius, Angle1, Angle2), which is what the board outline is made of.
"""

import math
import re

MILS_PER_MM = 1000.0 / 25.4

# Altium stores coordinates in units of 0.0001 mil. Anything closer than this
# is the same point for every purpose here, and it absorbs the float noise of
# a mm -> mil -> internal unit round trip.
DEFAULT_TOLERANCE_MILS = 0.01

MAX_PRIMITIVES = 2000
# The DelphiScript side re-scans a spec line from its start for every field,
# so very long vertex lists get slow there; real outlines and regions stay far
# below this.
MAX_VERTICES = 500

# Region kinds as Altium numbers them (TRegionKind). Board cutout is 3 in
# current releases; get_board_outline reports the number any existing cutout
# actually carries, so a mismatch shows up there.
REGION_KINDS = {"copper": 0, "polygon_cutout": 1, "board_cutout": 3}

PAD_SHAPES = ("round", "rect", "rounded_rect")

_LAYER_ALIASES = {
    "top": "Top Layer",
    "top layer": "Top Layer",
    "toplayer": "Top Layer",
    "bottom": "Bottom Layer",
    "bottom layer": "Bottom Layer",
    "bottomlayer": "Bottom Layer",
    "top overlay": "Top Overlay",
    "topoverlay": "Top Overlay",
    "top silk": "Top Overlay",
    "top silkscreen": "Top Overlay",
    "bottom overlay": "Bottom Overlay",
    "bottomoverlay": "Bottom Overlay",
    "bottom silk": "Bottom Overlay",
    "bottom silkscreen": "Bottom Overlay",
    "keepout": "Keep Out Layer",
    "keep out": "Keep Out Layer",
    "keep-out": "Keep Out Layer",
    "keepout layer": "Keep Out Layer",
    "keep out layer": "Keep Out Layer",
    "keep-out layer": "Keep Out Layer",
    "multilayer": "Multi Layer",
    "multi layer": "Multi Layer",
    "multi-layer": "Multi Layer",
}

_MECH_RE = re.compile(r"^(?:mech|mechanical|m)\s*(\d{1,2})$")


class GeometryError(ValueError):
    """Bad geometry in a tool argument. The message is shown to the caller."""


# ---------------------------------------------------------------------------
# Units and formatting
# ---------------------------------------------------------------------------

def normalize_units(units):
    u = str(units if units is not None else "").strip().lower()
    if u in ("mm", "millimeter", "millimeters", "millimetre", "millimetres"):
        return "mm"
    if u in ("mil", "mils", "thou"):
        return "mil"
    raise GeometryError(f"units must be 'mm' or 'mil', got {units!r}")


def _finite(value, what):
    if isinstance(value, bool):
        raise GeometryError(f"{what} must be a number, got {value!r}")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise GeometryError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(v):
        raise GeometryError(f"{what} must be finite, got {value!r}")
    return v


def to_mils(value, units, what="value"):
    v = _finite(value, what)
    return v * MILS_PER_MM if units == "mm" else v


def from_mils(value, units):
    return value / MILS_PER_MM if units == "mm" else value


def fmt(value):
    """Fixed-point text for the spec file. Six decimals keeps well under
    Altium's 0.0001 mil resolution, so rounding happens once, in Altium."""
    text = f"{value:.6f}"
    return "0.000000" if text == "-0.000000" else text


def normalize_layer(name, what="layer"):
    """Map common spellings onto the names Altium's String2Layer knows.

    Anything unrecognised passes through unchanged: the Altium side also
    matches copper and mechanical layers by their displayed (custom) names.
    """
    if name is None:
        raise GeometryError(f"{what} is required")
    text = str(name).strip()
    if not text:
        raise GeometryError(f"{what} is required")
    if "|" in text or "\n" in text or "\r" in text:
        raise GeometryError(f"{what} must not contain '|' or line breaks: {name!r}")
    key = text.lower()
    if key in _LAYER_ALIASES:
        return _LAYER_ALIASES[key]
    match = _MECH_RE.match(key)
    if match:
        return f"Mechanical {int(match.group(1))}"
    return text


def _net_field(value, what):
    if value is None:
        return ""
    text = str(value).strip()
    if "|" in text or "\n" in text or "\r" in text:
        raise GeometryError(f"{what} must not contain '|' or line breaks: {value!r}")
    return text


# ---------------------------------------------------------------------------
# Outline construction
# ---------------------------------------------------------------------------

def line_seg(x, y):
    return {"kind": "line", "x": float(x), "y": float(y)}


def arc_seg(x, y, cx, cy, radius, start_angle, end_angle):
    return {"kind": "arc", "x": float(x), "y": float(y), "cx": float(cx),
            "cy": float(cy), "radius": float(radius),
            "start_angle": float(start_angle) % 360.0,
            "end_angle": float(end_angle) % 360.0}


def _same_point(ax, ay, bx, by, tol=1e-9):
    return abs(ax - bx) <= tol and abs(ay - by) <= tol


def _drop_zero_length_lines(segments):
    kept = list(segments)
    changed = True
    while changed and len(kept) > 1:
        changed = False
        for i, seg in enumerate(kept):
            nxt = kept[(i + 1) % len(kept)]
            if seg["kind"] == "line" and _same_point(seg["x"], seg["y"], nxt["x"], nxt["y"]):
                del kept[i]
                changed = True
                break
    return kept


def rectangle_outline(width, height, x=0.0, y=0.0, corner_radius=0.0):
    """Counterclockwise rectangle, lower-left corner at (x, y), all in mils.

    A corner radius turns each corner into a quarter arc. A radius of half the
    shorter side gives a stadium; half of both sides gives a circle.
    """
    if width <= 0 or height <= 0:
        raise GeometryError("width and height must be greater than zero")
    if corner_radius < 0:
        raise GeometryError("corner_radius must not be negative")
    limit = min(width, height) / 2.0
    if corner_radius > limit + DEFAULT_TOLERANCE_MILS:
        raise GeometryError(
            f"corner_radius {corner_radius:.4f} mil is more than half the shorter side "
            f"({limit:.4f} mil)")
    r = min(corner_radius, limit)
    x2, y2 = x + width, y + height
    if r <= 1e-9:
        return [line_seg(x, y), line_seg(x2, y), line_seg(x2, y2), line_seg(x, y2)]
    segments = [
        line_seg(x + r, y),
        arc_seg(x2 - r, y, x2 - r, y + r, r, 270, 0),
        line_seg(x2, y + r),
        arc_seg(x2, y2 - r, x2 - r, y2 - r, r, 0, 90),
        line_seg(x2 - r, y2),
        arc_seg(x + r, y2, x + r, y2 - r, r, 90, 180),
        line_seg(x, y2 - r),
        arc_seg(x, y + r, x + r, y + r, r, 180, 270),
    ]
    return _drop_zero_length_lines(segments)


def outline_from_vertices(vertices, units, what="vertices"):
    """Outline from [{"x":.., "y":.., "arc": {"cx":.., "cy":..}}, ...].

    An "arc" on a vertex makes the edge from that vertex to the next a
    counterclockwise arc about (cx, cy); otherwise the edge is straight.
    A straight-edged outline given clockwise is reversed to counterclockwise.
    One with arcs must already run counterclockwise, since reversing it would
    turn every arc inside out.
    """
    if not isinstance(vertices, list):
        raise GeometryError(f"{what} must be a list of {{x, y}} objects")
    if len(vertices) > MAX_VERTICES:
        raise GeometryError(f"{what} has {len(vertices)} entries; the limit is {MAX_VERTICES}")
    points = []
    for i, v in enumerate(vertices):
        where = f"{what}[{i}]"
        if not isinstance(v, dict):
            raise GeometryError(f"{where} must be an object with x and y")
        if "x" not in v or "y" not in v:
            raise GeometryError(f"{where} needs both x and y")
        px = to_mils(v["x"], units, f"{where}.x")
        py = to_mils(v["y"], units, f"{where}.y")
        arc = v.get("arc")
        centre = None
        if arc is not None:
            if not isinstance(arc, dict) or "cx" not in arc or "cy" not in arc:
                raise GeometryError(f"{where}.arc must be an object with cx and cy")
            centre = (to_mils(arc["cx"], units, f"{where}.arc.cx"),
                      to_mils(arc["cy"], units, f"{where}.arc.cy"))
        points.append((px, py, centre))

    has_arcs = any(p[2] is not None for p in points)
    if len(points) < 3 and not (len(points) == 2 and all(p[2] is not None for p in points)):
        raise GeometryError(f"{what} needs at least 3 vertices (or 2 joined by arcs)")

    segments = _segments_from_points(points, what)
    # Crossing first: a bow tie's signed area can be exactly zero, and "crosses
    # itself" is the message that tells the caller what is actually wrong.
    check_simple(segments, what)
    area = signed_area(segments)
    if abs(area) <= DEFAULT_TOLERANCE_MILS:
        raise GeometryError(f"{what} encloses no area")
    if area < 0:
        if has_arcs:
            raise GeometryError(
                f"{what} runs clockwise. List the vertices counterclockwise; arcs are "
                "always drawn counterclockwise from a vertex to the next")
        points.reverse()
        segments = _segments_from_points(points, what)
    return segments


def _segments_from_points(points, what):
    segments = []
    n = len(points)
    for i, (px, py, centre) in enumerate(points):
        nx, ny, _ = points[(i + 1) % n]
        if _same_point(px, py, nx, ny, DEFAULT_TOLERANCE_MILS):
            raise GeometryError(f"{what}[{i}] repeats the next vertex")
        if centre is None:
            segments.append(line_seg(px, py))
            continue
        cx, cy = centre
        r1 = math.hypot(px - cx, py - cy)
        r2 = math.hypot(nx - cx, ny - cy)
        if r1 <= DEFAULT_TOLERANCE_MILS:
            raise GeometryError(f"{what}[{i}].arc centre sits on the vertex")
        if abs(r1 - r2) > max(DEFAULT_TOLERANCE_MILS, 1e-7 * r1):
            raise GeometryError(
                f"{what}[{i}].arc centre is {r1:.4f} mil from this vertex but {r2:.4f} mil "
                "from the next; an arc needs both on the same circle")
        a1 = math.degrees(math.atan2(py - cy, px - cx)) % 360.0
        a2 = math.degrees(math.atan2(ny - cy, nx - cx)) % 360.0
        segments.append(arc_seg(px, py, cx, cy, (r1 + r2) / 2.0, a1, a2))
    return segments


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def arc_point(seg, angle_deg):
    a = math.radians(angle_deg)
    return (seg["cx"] + seg["radius"] * math.cos(a),
            seg["cy"] + seg["radius"] * math.sin(a))


def arc_sweep(seg):
    """Counterclockwise sweep from start_angle to end_angle, in (0, 360]."""
    sweep = (seg["end_angle"] - seg["start_angle"]) % 360.0
    return 360.0 if sweep <= 1e-9 else sweep


def arc_runs_forward(seg):
    """True when the edge leaves its vertex at start_angle.

    Outlines built here always do. One read back from Altium may have been
    drawn clockwise, in which case the edge starts at end_angle instead.
    """
    sx, sy = arc_point(seg, seg["start_angle"])
    ex, ey = arc_point(seg, seg["end_angle"])
    return math.hypot(seg["x"] - sx, seg["y"] - sy) <= math.hypot(seg["x"] - ex, seg["y"] - ey)


def signed_area(segments):
    """Green's theorem over lines and arcs. Positive means counterclockwise."""
    total = 0.0
    n = len(segments)
    for i, seg in enumerate(segments):
        nxt = segments[(i + 1) % n]
        if seg["kind"] == "arc":
            a = math.radians(seg["start_angle"])
            b = a + math.radians(arc_sweep(seg))
            cx, cy, r = seg["cx"], seg["cy"], seg["radius"]
            term = (cx * r * (math.sin(b) - math.sin(a))
                    - cy * r * (math.cos(b) - math.cos(a))
                    + r * r * (b - a))
            total += term if arc_runs_forward(seg) else -term
        else:
            total += seg["x"] * nxt["y"] - nxt["x"] * seg["y"]
    return total / 2.0


def perimeter(segments):
    total = 0.0
    n = len(segments)
    for i, seg in enumerate(segments):
        nxt = segments[(i + 1) % n]
        if seg["kind"] == "arc":
            total += seg["radius"] * math.radians(arc_sweep(seg))
        else:
            total += math.hypot(nxt["x"] - seg["x"], nxt["y"] - seg["y"])
    return total


def outline_bbox(segments):
    """(min_x, min_y, max_x, max_y), counting the bulge of every arc."""
    xs, ys = [], []
    for seg in segments:
        xs.append(seg["x"])
        ys.append(seg["y"])
        if seg["kind"] == "arc":
            sweep = arc_sweep(seg)
            angles = [seg["start_angle"], seg["end_angle"]]
            for quadrant in (0.0, 90.0, 180.0, 270.0):
                if (quadrant - seg["start_angle"]) % 360.0 <= sweep + 1e-9:
                    angles.append(quadrant)
            for angle in angles:
                px, py = arc_point(seg, angle)
                xs.append(px)
                ys.append(py)
    return min(xs), min(ys), max(xs), max(ys)


def outline_polyline(segments, max_step_deg=5.0):
    """Vertices of the outline with arcs broken into short chords."""
    points = []
    for seg in segments:
        points.append((seg["x"], seg["y"]))
        if seg["kind"] != "arc":
            continue
        sweep = arc_sweep(seg)
        steps = max(1, int(math.ceil(sweep / max_step_deg)))
        forward = arc_runs_forward(seg)
        for k in range(1, steps):
            t = k / steps
            if forward:
                angle = seg["start_angle"] + sweep * t
            else:
                angle = seg["end_angle"] - sweep * t
            points.append(arc_point(seg, angle))
    return points


def _cross(ax, ay, bx, by, cx, cy):
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _segments_cross(p1, p2, p3, p4):
    """Proper crossing only: shared endpoints and touching do not count."""
    d1 = _cross(*p3, *p4, *p1)
    d2 = _cross(*p3, *p4, *p2)
    d3 = _cross(*p1, *p2, *p3)
    d4 = _cross(*p1, *p2, *p4)
    return ((d1 > 0) != (d2 > 0) and abs(d1) > 1e-9 and abs(d2) > 1e-9 and
            (d3 > 0) != (d4 > 0) and abs(d3) > 1e-9 and abs(d4) > 1e-9)


def check_simple(segments, what="outline"):
    """Reject outlines whose edges cross each other (a bow tie, say)."""
    pts = outline_polyline(segments)
    n = len(pts)
    edges = [(pts[i], pts[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue  # neighbours through the wrap-around
            if _segments_cross(edges[i][0], edges[i][1], edges[j][0], edges[j][1]):
                raise GeometryError(f"{what} crosses itself; its edges must not intersect")


def point_in_outline(x, y, polyline):
    inside = False
    n = len(polyline)
    for i in range(n):
        x1, y1 = polyline[i]
        x2, y2 = polyline[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_at = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x_at > x:
                inside = not inside
    return inside


def classify_box(box, polyline):
    """'inside', 'outside' or 'crossing' for a (left, bottom, right, top) box."""
    left, bottom, right, top = box
    corners = [(left, bottom), (right, bottom), (right, top), (left, top)]
    inside = sum(point_in_outline(cx, cy, polyline) for cx, cy in corners)
    rect_edges = [(corners[i], corners[(i + 1) % 4]) for i in range(4)]
    n = len(polyline)
    touches = False
    for i in range(n):
        a, b = polyline[i], polyline[(i + 1) % n]
        if left < a[0] < right and bottom < a[1] < top:
            touches = True
            break
        if any(_segments_cross(a, b, r1, r2) for r1, r2 in rect_edges):
            touches = True
            break
    if inside == 4 and not touches:
        return "inside"
    if inside == 0 and not touches:
        return "outside"
    return "crossing"


def describe_outline(segments, units):
    """Summary numbers for a tool result, converted to the caller's units."""
    left, bottom, right, top = outline_bbox(segments)
    area = signed_area(segments)
    scale = from_mils(1.0, units)
    return {
        "units": units,
        "segment_count": len(segments),
        "arc_count": sum(1 for s in segments if s["kind"] == "arc"),
        "width": (right - left) * scale,
        "height": (top - bottom) * scale,
        "bbox": {"left": left * scale, "bottom": bottom * scale,
                 "right": right * scale, "top": top * scale},
        "area": abs(area) * scale * scale,
        "perimeter": perimeter(segments) * scale,
        "orientation": "counterclockwise" if area > 0 else "clockwise",
    }


def restorable_vertices(segments, units):
    """The outline in the caller's units, in the form set_board_outline takes
    (vertices, each with an optional arc centre), so a previous outline can
    be put back by passing this list straight in.

    set_board_outline wants counterclockwise vertices with counterclockwise
    arcs. An outline Altium holds clockwise is walked backwards to get there:
    each edge keeps its arc centre, which then belongs to the vertex the
    reversed edge starts from.
    """
    scale = from_mils(1.0, units)
    n = len(segments)
    if n == 0:
        return []
    if signed_area(segments) >= 0:
        order = [(i, i) for i in range(n)]  # (vertex index, edge index)
    else:
        # Vertex k of the reversed walk is old vertex (n - k) % n, and the
        # edge leaving it is old edge (n - k - 1) % n run backwards.
        order = [((n - k) % n, (n - k - 1) % n) for k in range(n)]
    out = []
    for vertex_index, edge_index in order:
        vertex = segments[vertex_index]
        edge = segments[edge_index]
        entry = {"x": vertex["x"] * scale, "y": vertex["y"] * scale}
        if edge["kind"] == "arc":
            entry["arc"] = {"cx": edge["cx"] * scale, "cy": edge["cy"] * scale}
        out.append(entry)
    return out


def compare_outlines(requested, actual, tol=DEFAULT_TOLERANCE_MILS):
    """How closely what Altium holds matches what was asked for.

    Altium is free to start the outline at a different vertex, so vertices
    are matched by position rather than by index.
    """
    report = {"requested_segments": len(requested), "actual_segments": len(actual)}
    if not actual:
        report.update(match=False, reason="Altium reports an empty outline")
        return report

    def nearest(x, y, pool):
        return min(math.hypot(x - p["x"], y - p["y"]) for p in pool)

    worst_vertex = max(nearest(s["x"], s["y"], actual) for s in requested)
    worst_extra = max(nearest(s["x"], s["y"], requested) for s in actual)
    rb, ab = outline_bbox(requested), outline_bbox(actual)
    bbox_dev = max(abs(r - a) for r, a in zip(rb, ab))
    area_dev = abs(abs(signed_area(requested)) - abs(signed_area(actual)))
    area_tol = tol * max(perimeter(requested), 1.0)

    arcs_missing = 0
    for seg in requested:
        if seg["kind"] != "arc":
            continue
        found = any(a["kind"] == "arc" and
                    math.hypot(a["cx"] - seg["cx"], a["cy"] - seg["cy"]) <= tol and
                    abs(a["radius"] - seg["radius"]) <= tol for a in actual)
        if not found:
            arcs_missing += 1

    report.update(
        max_vertex_deviation_mils=max(worst_vertex, worst_extra),
        bbox_deviation_mils=bbox_dev,
        area_deviation_sq_mils=area_dev,
        arcs_not_found=arcs_missing,
        tolerance_mils=tol,
    )
    report["match"] = (len(requested) == len(actual) and worst_vertex <= tol and
                       worst_extra <= tol and bbox_dev <= tol and
                       area_dev <= area_tol and arcs_missing == 0)
    return report


def outline_spec_lines(segments):
    """SEG lines for the Altium side, one per segment, in mils."""
    lines = []
    for seg in segments:
        if seg["kind"] == "arc":
            end = seg["end_angle"]
            # An arc finishing at 0 degrees goes to Altium as ending at 360,
            # so no reading of the pair can make its sweep negative.
            if end <= 1e-9 < seg["start_angle"]:
                end = 360.0
            lines.append("SEG|ARC|{}|{}|{}|{}|{}|{}|{}".format(
                fmt(seg["x"]), fmt(seg["y"]), fmt(seg["cx"]), fmt(seg["cy"]),
                fmt(seg["radius"]), fmt(seg["start_angle"]), fmt(end)))
        else:
            lines.append("SEG|LINE|{}|{}".format(fmt(seg["x"]), fmt(seg["y"])))
    return lines


def segments_from_altium(raw_segments):
    """Outline segments from what the Altium side reports (kind 0 = line,
    1 = arc, TPolySegmentType order)."""
    segments = []
    for raw in raw_segments or []:
        kind = raw.get("kind")
        if kind in (1, "1", "arc"):
            segments.append(arc_seg(raw["x"], raw["y"], raw["cx"], raw["cy"], raw["radius"],
                                    raw["start_angle"], raw["end_angle"]))
        else:
            segments.append(line_seg(raw["x"], raw["y"]))
    # Altium's polygons carry the first vertex again at index N to close the
    # shape. Should it be reported, it is not a segment of its own.
    if (len(segments) > 2 and segments[-1]["kind"] == "line" and
            _same_point(segments[-1]["x"], segments[-1]["y"],
                        segments[0]["x"], segments[0]["y"], DEFAULT_TOLERANCE_MILS)):
        segments.pop()
    return segments


# ---------------------------------------------------------------------------
# Primitives for create_pcb_primitives
# ---------------------------------------------------------------------------

def _field(p, key, where, units=None, default=None, required=True, positive=False,
           non_negative=False):
    if key not in p or p[key] is None:
        if required:
            raise GeometryError(f"{where}.{key} is required")
        return default
    value = to_mils(p[key], units, f"{where}.{key}") if units else _finite(p[key], f"{where}.{key}")
    if positive and value <= 0:
        raise GeometryError(f"{where}.{key} must be greater than zero")
    if non_negative and value < 0:
        raise GeometryError(f"{where}.{key} must not be negative")
    return value


def _points(p, key, where, units, minimum=3):
    pts = p.get(key)
    if not isinstance(pts, list) or len(pts) < minimum:
        raise GeometryError(f"{where}.{key} must list at least {minimum} {{x, y}} points")
    if len(pts) > MAX_VERTICES:
        raise GeometryError(f"{where}.{key} has {len(pts)} points; the limit is {MAX_VERTICES}")
    out = []
    for i, pt in enumerate(pts):
        if not isinstance(pt, dict) or "x" not in pt or "y" not in pt:
            raise GeometryError(f"{where}.{key}[{i}] must be an object with x and y")
        out.append((to_mils(pt["x"], units, f"{where}.{key}[{i}].x"),
                    to_mils(pt["y"], units, f"{where}.{key}[{i}].y")))
    segments = [line_seg(x, y) for x, y in out]
    check_simple(segments, f"{where}.{key}")
    if abs(signed_area(segments)) <= DEFAULT_TOLERANCE_MILS:
        raise GeometryError(f"{where}.{key} encloses no area")
    return out


def _flag(p, key, default):
    value = p.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _escape_text(text):
    return (str(text).replace("|", "<PIPE>").replace("\r\n", "<NL>")
            .replace("\n", "<NL>").replace("\r", "<NL>"))


def _track(p, units, where):
    layer = normalize_layer(p.get("layer"), f"{where}.layer")
    x1 = _field(p, "x1", where, units)
    y1 = _field(p, "y1", where, units)
    x2 = _field(p, "x2", where, units)
    y2 = _field(p, "y2", where, units)
    width = _field(p, "width", where, units, positive=True)
    if _same_point(x1, y1, x2, y2, 1e-6):
        raise GeometryError(f"{where} has zero length")
    net = _net_field(p.get("net"), f"{where}.net")
    return ["TRACK|{}|{}|{}|{}|{}|{}|{}".format(
        layer, net, fmt(x1), fmt(y1), fmt(x2), fmt(y2), fmt(width))]


def _arc(p, units, where):
    layer = normalize_layer(p.get("layer"), f"{where}.layer")
    cx = _field(p, "cx", where, units)
    cy = _field(p, "cy", where, units)
    radius = _field(p, "radius", where, units, positive=True)
    start_raw = _field(p, "start_angle", where)
    end_raw = _field(p, "end_angle", where)
    if abs(end_raw - start_raw) <= 1e-9:
        raise GeometryError(f"{where} sweeps no angle (start_angle == end_angle)")
    if abs((end_raw - start_raw) % 360.0) <= 1e-9:
        start, end = 0.0, 360.0  # a whole circle; reducing both mod 360 would erase it
    else:
        start, end = start_raw % 360.0, end_raw % 360.0
    width = _field(p, "width", where, units, positive=True)
    net = _net_field(p.get("net"), f"{where}.net")
    return ["ARC|{}|{}|{}|{}|{}|{}|{}|{}".format(
        layer, net, fmt(cx), fmt(cy), fmt(radius), fmt(start), fmt(end), fmt(width))]


def _via(p, units, where):
    x = _field(p, "x", where, units)
    y = _field(p, "y", where, units)
    diameter = _field(p, "diameter", where, units, positive=True)
    hole = _field(p, "hole", where, units, positive=True)
    if hole >= diameter:
        raise GeometryError(f"{where}.hole must be smaller than its diameter")
    low = normalize_layer(p.get("low_layer", "Top Layer"), f"{where}.low_layer")
    high = normalize_layer(p.get("high_layer", "Bottom Layer"), f"{where}.high_layer")
    net = _net_field(p.get("net"), f"{where}.net")
    return ["VIA|{}|{}|{}|{}|{}|{}|{}".format(
        net, fmt(x), fmt(y), fmt(diameter), fmt(hole), low, high)]


def _fill(p, units, where):
    layer = normalize_layer(p.get("layer"), f"{where}.layer")
    x1 = _field(p, "x1", where, units)
    y1 = _field(p, "y1", where, units)
    x2 = _field(p, "x2", where, units)
    y2 = _field(p, "y2", where, units)
    if abs(x1 - x2) <= 1e-6 or abs(y1 - y2) <= 1e-6:
        raise GeometryError(f"{where} has no area (x1 == x2 or y1 == y2)")
    rotation = _field(p, "rotation", where, default=0.0, required=False)
    net = _net_field(p.get("net"), f"{where}.net")
    return ["FILL|{}|{}|{}|{}|{}|{}|{}".format(
        layer, net, fmt(min(x1, x2)), fmt(min(y1, y2)), fmt(max(x1, x2)),
        fmt(max(y1, y2)), fmt(rotation % 360.0))]


def _region(p, units, where):
    kind_value = p.get("kind", "copper")
    if isinstance(kind_value, bool):
        raise GeometryError(f"{where}.kind must be a name or a number")
    if isinstance(kind_value, (int, float)):
        kind = int(kind_value)
        kind_name = next((k for k, v in REGION_KINDS.items() if v == kind), str(kind))
    else:
        kind_name = str(kind_value).strip().lower().replace(" ", "_").replace("-", "_")
        if kind_name not in REGION_KINDS:
            raise GeometryError(
                f"{where}.kind must be one of {', '.join(REGION_KINDS)} (or a number), "
                f"got {kind_value!r}")
        kind = REGION_KINDS[kind_name]
    default_layer = "Multi Layer" if kind_name == "board_cutout" else None
    layer = normalize_layer(p.get("layer", default_layer), f"{where}.layer")
    net = _net_field(p.get("net"), f"{where}.net")
    if net and kind_name != "copper":
        raise GeometryError(f"{where}: only copper regions carry a net")
    pts = _points(p, "points", where, units)
    coords = "|".join(f"{fmt(x)}|{fmt(y)}" for x, y in pts)
    return ["REGION|{}|{}|{}|{}|{}".format(layer, net, kind, len(pts), coords)]


def _pad(p, units, where):
    x = _field(p, "x", where, units)
    y = _field(p, "y", where, units)
    size = _field(p, "size", where, units, required=False, positive=True)
    size_x = _field(p, "size_x", where, units, required=False, positive=True)
    size_y = _field(p, "size_y", where, units, required=False, positive=True)
    if size is not None:
        size_x = size_x if size_x is not None else size
        size_y = size_y if size_y is not None else size
    if size_x is None or size_y is None:
        raise GeometryError(f"{where} needs size (or both size_x and size_y)")
    hole = _field(p, "hole", where, units, default=0.0, required=False, non_negative=True)
    if hole > min(size_x, size_y) + 1e-6:
        raise GeometryError(f"{where}.hole is larger than the pad")
    shape = str(p.get("shape", "round")).strip().lower().replace(" ", "_").replace("-", "_")
    if shape not in PAD_SHAPES:
        raise GeometryError(f"{where}.shape must be one of {', '.join(PAD_SHAPES)}")
    plated = _flag(p, "plated", hole > 0)
    if hole > 0:
        layer = normalize_layer(p.get("layer", "Multi Layer"), f"{where}.layer")
        if layer != "Multi Layer":
            raise GeometryError(f"{where}: a pad with a hole must be on Multi Layer")
    else:
        layer = normalize_layer(p.get("layer", "Top Layer"), f"{where}.layer")
        if layer not in ("Top Layer", "Bottom Layer"):
            raise GeometryError(f"{where}: a pad without a hole must be on Top Layer or Bottom Layer")
    rotation = _field(p, "rotation", where, default=0.0, required=False) % 360.0
    name = _net_field(p.get("name", ""), f"{where}.name")
    net = _net_field(p.get("net"), f"{where}.net")
    return ["PAD|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}".format(
        layer, net, fmt(x), fmt(y), fmt(size_x), fmt(size_y), fmt(hole), shape,
        1 if plated else 0, fmt(rotation), name)]


def _text(p, units, where):
    text = p.get("text")
    if text is None or str(text) == "":
        raise GeometryError(f"{where}.text is required")
    layer = normalize_layer(p.get("layer", "Top Overlay"), f"{where}.layer")
    x = _field(p, "x", where, units)
    y = _field(p, "y", where, units)
    height = _field(p, "height", where, units, positive=True)
    stroke = _field(p, "stroke_width", where, units, default=height / 6.0,
                    required=False, positive=True)
    rotation = _field(p, "rotation", where, default=0.0, required=False) % 360.0
    bottom_side = layer in ("Bottom Overlay", "Bottom Layer")
    mirror = _flag(p, "mirror", bottom_side)
    return ["TEXT|{}|{}|{}|{}|{}|{}|{}|{}".format(
        layer, fmt(x), fmt(y), fmt(height), fmt(stroke), fmt(rotation),
        1 if mirror else 0, _escape_text(text))]


def _polygon(p, units, where):
    layer = normalize_layer(p.get("layer"), f"{where}.layer")
    net = _net_field(p.get("net"), f"{where}.net")
    pts = _points(p, "points", where, units)
    segments = [line_seg(x, y) for x, y in pts]
    if signed_area(segments) < 0:
        pts.reverse()
    pour = _flag(p, "pour", False)
    coords = "|".join(f"{fmt(x)}|{fmt(y)}" for x, y in pts)
    return ["POLYGON|{}|{}|{}|{}|{}".format(layer, net, 1 if pour else 0, len(pts), coords)]


_BUILDERS = {
    "track": _track,
    "arc": _arc,
    "via": _via,
    "fill": _fill,
    "region": _region,
    "pad": _pad,
    "text": _text,
    "polygon": _polygon,
}

PRIMITIVE_TYPES = tuple(_BUILDERS)


def primitive_spec_lines(primitives, units):
    """Validate primitives and turn them into spec lines, one per object.

    Every problem is caught here, before anything reaches Altium, so a batch
    is either sent whole or not at all.
    """
    units = normalize_units(units)
    if not isinstance(primitives, list) or not primitives:
        raise GeometryError("primitives must be a non-empty list")
    if len(primitives) > MAX_PRIMITIVES:
        raise GeometryError(
            f"{len(primitives)} primitives in one call; the limit is {MAX_PRIMITIVES}")
    lines = []
    counts = {}
    for i, prim in enumerate(primitives):
        where = f"primitives[{i}]"
        if not isinstance(prim, dict):
            raise GeometryError(f"{where} must be an object")
        kind = str(prim.get("type", "")).strip().lower()
        builder = _BUILDERS.get(kind)
        if builder is None:
            raise GeometryError(
                f"{where}.type must be one of {', '.join(PRIMITIVE_TYPES)}, got {prim.get('type')!r}")
        lines.extend(builder(prim, units, where))
        counts[kind] = counts.get(kind, 0) + 1
    return lines, counts
