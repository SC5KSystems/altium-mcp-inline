"""Draw a board as SVG (stdlib only): copper pours, tracks, pads, holes, outline, labels.

Units are millimetres; the image is scaled so 1 mm = `px_per_mm` pixels. Overlays let a
review annotate the picture (a screw-head circle at true size, a highlighted pad, a label).
"""
import math

from .pcbdoc import NONE

_SWATCH = ["#d94040", "#f08a2a", "#e2c63c", "#34c0c0", "#58c878", "#c25fd6", "#7a8cff", "#9a9a9a"]


def net_color(name):
    """Default net colouring: ground blue, everything else a stable colour picked from the name.
    Pass your own function as `colorer` to board_svg to colour by meaning instead."""
    if name.upper() in ("GND", "GROUND", "0V"):
        return "#3d5fa8"
    if not name:
        return "#8a8f98"
    return _SWATCH[sum(ord(c) for c in name) % len(_SWATCH)]


def _pts(ring, tx):
    return " ".join("%.2f,%.2f" % tx(p) for p in ring)


def _path(rings, tx):
    return " ".join("M " + " L ".join("%.2f %.2f" % tx(p) for p in r) + " Z" for r in rings if len(r) > 2)


def board_svg(doc, side="top", px_per_mm=10.0, overlays=(), labels=True, margin=6.0, title=None, colorer=None):
    color_of = colorer or net_color
    x0, y0, x1, y1 = doc.board.bbox
    W, H = (x1 - x0 + 2 * margin), (y1 - y0 + 2 * margin)

    def tx(p):
        return ((p[0] - x0 + margin), (y1 - p[1] + margin))

    lay = 1 if side == "top" else 32
    want_layers = (lay, 74)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %.2f %.2f" width="%d" height="%d" '
           'font-family="Segoe UI, Arial, sans-serif">' % (W, H, int(W * px_per_mm), int(H * px_per_mm))]
    if title:
        out.append("<title>%s</title>" % title)
    out.append('<rect width="100%" height="100%" fill="#0f1b14"/>')
    out.append('<polygon points="%s" fill="#1d4a2c"/>' % _pts(doc.board.ring(), tx))
    # pours and free regions
    for r in doc.regions:
        if r.layer != lay or r.kind != 0:
            continue
        net = doc.polygons[r.poly].net if r.poly != NONE and r.poly < len(doc.polygons) else r.net
        out.append('<path d="%s" fill="%s" fill-rule="evenodd" opacity="0.92"/>' % (
            _path([r.outline] + r.holes, tx), color_of(doc.net_name(net))))
    for t in doc.tracks:
        if t.layer == lay:
            a, b = tx((t.x1, t.y1)), tx((t.x2, t.y2))
            out.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="%s" stroke-width="%.3f" '
                       'stroke-linecap="round"/>' % (a[0], a[1], b[0], b[1], color_of(doc.net_name(t.net)), t.width))
    for p in doc.pads:
        if p.layer in want_layers:
            out.append('<polygon points="%s" fill="%s" stroke="#111" stroke-width="0.05"/>' % (
                _pts(p.ring(), tx), "#e8e8e8" if p.layer == 74 else "#f4e46a"))
            if p.hole > 0:
                cx, cy = tx((p.x, p.y))
                out.append('<circle cx="%.2f" cy="%.2f" r="%.3f" fill="#0a0a0a"/>' % (cx, cy, p.hole / 2))
    for v in doc.vias:
        cx, cy = tx((v.x, v.y))
        out.append('<circle cx="%.2f" cy="%.2f" r="%.3f" fill="none" stroke="#fff" stroke-width="0.06"/>' % (cx, cy, v.dia / 2))
    ring = doc.board.ring()
    out.append('<polygon points="%s" fill="none" stroke="#ff4fd8" stroke-width="0.25"/>' % _pts(ring + [ring[0]], tx))
    if labels:
        for c in doc.components:
            if c.bottom == (side == "top"):
                continue
            cx, cy = tx((c.x, c.y))
            out.append('<text x="%.2f" y="%.2f" font-size="1.7" fill="#fff" text-anchor="middle" '
                       'stroke="#000" stroke-width="0.12" paint-order="stroke">%s</text>' % (cx, cy, c.designator))
    for o in overlays:
        out.append(_overlay(o, tx))
    out.append("</svg>")
    return "\n".join(out)


def _overlay(o, tx):
    kind = o["type"]
    col = o.get("color", "#ffd400")
    if kind == "circle":
        cx, cy = tx((o["x"], o["y"]))
        dash = ' stroke-dasharray="%s"' % o["dash"] if o.get("dash") else ""
        s = '<circle cx="%.2f" cy="%.2f" r="%.3f" fill="%s" fill-opacity="%s" stroke="%s" stroke-width="%s"%s/>' % (
            cx, cy, o["r"], o.get("fill", "none"), o.get("fill_opacity", 0.18), col, o.get("width", 0.22), dash)
        if o.get("label"):
            s += '<text x="%.2f" y="%.2f" font-size="%s" fill="%s" text-anchor="middle" stroke="#000" ' \
                 'stroke-width="0.15" paint-order="stroke">%s</text>' % (cx, cy + o.get("label_dy", -o["r"] - 0.8),
                                                                      o.get("font", 2.0), col, o["label"])
        return s
    if kind == "text":
        cx, cy = tx((o["x"], o["y"]))
        return '<text x="%.2f" y="%.2f" font-size="%s" fill="%s" text-anchor="%s" stroke="#000" ' \
               'stroke-width="0.15" paint-order="stroke">%s</text>' % (cx, cy, o.get("font", 2.2), col,
                                                                        o.get("anchor", "middle"), o["text"])
    if kind == "line":
        a, b = tx((o["x1"], o["y1"])), tx((o["x2"], o["y2"]))
        dash = ' stroke-dasharray="%s"' % o["dash"] if o.get("dash") else ""
        return '<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="%s" stroke-width="%s"%s/>' % (
            a[0], a[1], b[0], b[1], col, o.get("width", 0.25), dash)
    if kind == "rect":
        (ax, ay), (bx, by) = tx((o["x1"], o["y2"])), tx((o["x2"], o["y1"]))
        return '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="%s" fill-opacity="%s" stroke="%s" ' \
               'stroke-width="%s"/>' % (ax, ay, bx - ax, by - ay, o.get("fill", "none"), o.get("fill_opacity", 0.15),
                                        col, o.get("width", 0.22))
    raise ValueError("unknown overlay type %r" % kind)
