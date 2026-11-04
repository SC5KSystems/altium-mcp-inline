"""Draw a board, or part of it, to PNG or SVG.

    python -m offline render board.PcbDoc -o out.png
        [--layers "Top Layer" "Bottom Layer"] [--net VBAT --net GND]
        [--around U5 --margin 10 | --box x0 y0 x1 y1] [--ratsnest]
        [--markers drc.json] [--pdn VBAT --source J1.1 --sink U5.3 --current 30]
        [--diff other.PcbDoc] [--width 2000]

Copper comes from the same shapes connectivity uses, so what is drawn is what
the checks see: pads, tracks, arcs, vias, fills, free and poured regions,
plane areas. Polygon outlines are dashed, holes are punched, the board
outline and component outlines (with designators) are drawn on top.

PNG output needs Pillow; SVG needs nothing beyond shapely.
"""

import base64
import io
import json
import math
from pathlib import Path

import numpy as np

from . import geom
from . import layers as L
from .raster import Grid, draw

BACKGROUND = (16, 16, 20)
OUTLINE = (255, 220, 60)
RATS = (255, 255, 255)
MARKER = (255, 0, 255)
HOLE = (8, 8, 8)
COMPONENT = (200, 200, 200)

LAYER_COLORS = {
    L.TOP: (220, 40, 40), L.BOTTOM: (40, 90, 230), L.TOP_OVERLAY: (240, 230, 70),
    L.BOTTOM_OVERLAY: (170, 160, 60), L.KEEPOUT: (230, 0, 230), L.MULTI: (190, 190, 200),
    L.TOP_SOLDER: (130, 50, 130), L.BOTTOM_SOLDER: (80, 50, 130),
    L.TOP_PASTE: (140, 140, 140), L.BOTTOM_PASTE: (110, 110, 110),
}
MID_COLORS = [(230, 150, 30), (40, 190, 80), (200, 80, 200), (50, 200, 200), (180, 120, 60),
              (120, 200, 40), (240, 120, 160), (100, 140, 240)]


def layer_color(layer):
    if layer in LAYER_COLORS:
        return LAYER_COLORS[layer]
    if 2 <= layer <= 31:
        return MID_COLORS[(layer - 2) % len(MID_COLORS)]
    if L.is_plane(layer):
        return (60 + 25 * ((layer - 39) % 5), 140, 60)
    return (150, 120, 200)


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:
        raise ImportError("PNG output needs Pillow: pip install -r server/offline/requirements.txt") from e
    return Image, ImageDraw, ImageFont


def _font(size):
    _, _, ImageFont = _pil()
    try:
        return ImageFont.load_default(size=max(8, int(size)))
    except TypeError:
        return ImageFont.load_default()


# ------------------------------------------------------------------ scene

class Scene:
    """What to draw and where."""

    def __init__(self, doc, layers=None, nets=None, box=None, ratsnest=False, markers=None,
                 heat=None, index=None, components=True, margin=2.0):
        from .connect import CopperIndex
        self.doc = doc
        self.index = index or CopperIndex(doc)
        copper = doc.copper_layers
        if layers:
            self.layers = [L.parse(x) for x in layers]
        else:
            self.layers = list(copper) + [L.TOP_OVERLAY, L.BOTTOM_OVERLAY]
        self.nets = None
        if nets:
            self.nets = {n if isinstance(n, int) else doc.net_index(n) for n in nets}
        self.box = box or self.board_box(margin)
        self.ratsnest = ratsnest
        self.markers = markers or []
        self.heat = heat
        self.components = components

    def board_box(self, margin):
        pts = self.doc.outline()
        if not pts:
            pts = [(p.x, p.y) for p in self.doc.pads] or [(0, 0), (10, 10)]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)

    def draw_order(self):
        """Bottom of the stack first, so the top layer ends up on top."""
        copper = self.doc.copper_layers
        def key(lay):
            if lay in copper:
                return (1, -copper.index(lay))
            if lay in (L.BOTTOM_OVERLAY, L.BOTTOM_SOLDER, L.BOTTOM_PASTE):
                return (0, 0)
            if lay in (L.TOP_OVERLAY, L.TOP_SOLDER, L.TOP_PASTE):
                return (3, 0)
            return (2, lay)
        return sorted(self.layers, key=key)

    def copper_items(self, layer):
        return self.index.items.get(layer, [])

    def other_items(self, layer):
        """Non-copper primitives on a drawing layer (silk, mechanical, keepout)."""
        sh = geom.require()
        out = []
        for r in self.doc.primitives(("track", "arc", "fill", "region")):
            if r.layer != layer:
                continue
            if r.KIND == "region" and r.storage != "Regions6":
                continue
            try:
                g = geom.copper_shape(r, layer)
            except Exception:
                continue
            if not g.is_empty:
                out.append((r, None, g))
        if layer == L.KEEPOUT or L.is_copper(layer):
            for r in self.doc.primitives(("track", "arc", "fill", "region")):
                if r.is_keepout and r.layer == layer:
                    out.append((r, None, geom.copper_shape(r, layer)))
        return out

    def in_box(self, g):
        x0, y0, x1, y1 = self.box
        bx0, by0, bx1, by1 = g.bounds
        return not (bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1)

    def component_shapes(self):
        """[(designator, outline geometry, (x, y))] from bodies or pads."""
        sh = geom.require()
        doc = self.doc
        by_comp = {}
        for p in doc.pads:
            if p.component is not None:
                lay = L.TOP if p.on_layer(L.TOP) else L.BOTTOM
                by_comp.setdefault(p.component, []).append(geom.pad_shape(p, lay))
        bodies = {}
        for b in doc.bodies:
            if b.component is not None:
                pts = b.outline()
                if len(pts) >= 3:
                    bodies.setdefault(b.component, []).append(sh.Polygon(pts).buffer(0))
        out = []
        for c in doc.components:
            parts = bodies.get(c.index) or by_comp.get(c.index)
            if not parts:
                continue
            g = sh.union_all(parts)
            g = g.envelope if c.index not in bodies else g
            out.append((c.designator, g.boundary, (c.x, c.y)))
        return out

    def holes(self):
        out = []
        for v in self.doc.vias:
            out.append(geom.hole_shape(v))
        for p in self.doc.pads:
            if p.hole > 0:
                out.append(geom.hole_shape(p))
        return [h for h in out if not h.is_empty]

    def rats(self):
        from .connect import Connectivity
        if not self.ratsnest:
            return []
        c = Connectivity(self.doc, self.index)
        return [e for e in c.unrouted(self.nets)]


# ------------------------------------------------------------------ png

def _blend(img, mask, color, alpha):
    if not mask.any():
        return
    c = np.array(color, dtype=float)
    img[mask] = img[mask] * (1 - alpha) + c * alpha


def _lines(geomobj):
    if geomobj.is_empty:
        return
    t = geomobj.geom_type
    if t in ("LineString", "LinearRing"):
        yield list(geomobj.coords)
    elif t == "Polygon":
        yield list(geomobj.exterior.coords)
        for i in geomobj.interiors:
            yield list(i.coords)
    elif hasattr(geomobj, "geoms"):
        for g in geomobj.geoms:
            yield from _lines(g)


def _dash(coords, on, off):
    out, carry, drawing = [], 0.0, True
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        pos = 0.0
        while pos < seg:
            step = (on if drawing else off) - carry
            end = min(seg, pos + step)
            if drawing:
                t0, t1 = pos / seg, end / seg
                out.append([(x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0),
                            (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)])
            carry = carry + (end - pos)
            if carry >= (on if drawing else off) - 1e-12:
                drawing, carry = not drawing, 0.0
            pos = end
    return out


def render_png(scene, out, width=2000):
    Image, ImageDraw, _ = _pil()
    x0, y0, x1, y1 = scene.box
    res = (x1 - x0) / max(64, width)
    grid = Grid(x0, y0, x1, y1, res)
    img = np.empty(grid.shape + (3,), dtype=float)
    img[:] = BACKGROUND
    dim = scene.nets is not None
    for layer in scene.draw_order():
        color = layer_color(layer)
        items = scene.copper_items(layer) if L.is_copper(layer) else scene.other_items(layer)
        if not items:
            continue
        hi, lo = grid.empty(), grid.empty()
        for obj, net, g in items:
            if not scene.in_box(g):
                continue
            target = hi if (not dim or net in scene.nets) else lo
            draw(target, grid, g)
        if dim:
            _blend(img, lo & ~hi, color, 0.18)
            _blend(img, hi, tuple(min(255, int(c * 1.15 + 40)) for c in color), 0.95)
        else:
            _blend(img, hi, color, 0.70 if L.is_copper(layer) else 0.85)
    if scene.heat is not None:
        _heat_overlay(img, grid, scene.heat)
    holes = grid.empty()
    for h in scene.holes():
        if scene.in_box(h):
            draw(holes, grid, h)
    _blend(img, holes, HOLE, 1.0)
    pil = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(pil)

    def px(x, y):
        return ((x - grid.x0) / res, (grid.y1 - y) / res)

    lw = max(1, int(round(width / 1000)))
    for poly in scene.doc.polygons:
        if poly.layer not in scene.layers:
            continue
        pts = poly.outline()
        if len(pts) < 2:
            continue
        for seg in _dash(pts + [pts[0]], on=1.0, off=0.6):
            d.line([px(*p) for p in seg], fill=layer_color(poly.layer), width=lw)
    outline = scene.doc.outline()
    if outline:
        d.line([px(*p) for p in outline + [outline[0]]], fill=OUTLINE, width=lw + 1)
    for cut in scene.doc.board_cutouts():
        d.line([px(*p) for p in cut + [cut[0]]], fill=OUTLINE, width=lw)
    if scene.components:
        font = _font(min(36, max(10, 1.0 / res)))
        for des, g, (cx, cy) in scene.component_shapes():
            if not scene.in_box(g):
                continue
            for coords in _lines(g):
                d.line([px(*p) for p in coords], fill=COMPONENT, width=lw)
            c = g.centroid
            d.text(px(c.x, c.y), des, fill=(255, 255, 255), font=font, anchor="mm")
    for e in scene.rats():
        a, b = px(*e["from"]), px(*e["to"])
        d.line([a, b], fill=RATS, width=lw)
        for p in (a, b):
            d.ellipse([p[0] - 3 * lw, p[1] - 3 * lw, p[0] + 3 * lw, p[1] + 3 * lw], outline=RATS)
    font = _font(min(36, max(11, 1.2 / res)))
    for i, m in enumerate(scene.markers):
        p = px(m["x"], m["y"])
        r = max(6, 0.6 / res)
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], outline=MARKER, width=lw + 1)
        d.line([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=MARKER, width=lw)
        d.line([p[0] - r, p[1] + r, p[0] + r, p[1] - r], fill=MARKER, width=lw)
        d.text((p[0] + r + 2, p[1] - r), str(m.get("label", i + 1)), fill=MARKER, font=font)
    if scene.heat is not None:
        _legend(pil, scene.heat)
    pil.save(out)
    return {"path": str(out), "box": [round(v, 3) for v in scene.box],
            "mm_per_px": round(res, 5), "size_px": list(pil.size),
            "layers": [L.name(x) for x in scene.draw_order()]}


# ------------------------------------------------------------------ heat map

INFERNO = [(0, 0, 4), (40, 11, 84), (101, 21, 110), (159, 42, 99), (212, 72, 66),
           (245, 125, 21), (250, 193, 39), (252, 255, 164)]


def colormap(t):
    t = np.clip(t, 0, 1) * (len(INFERNO) - 1)
    i = np.minimum(t.astype(int), len(INFERNO) - 2)
    f = (t - i)[..., None]
    a = np.array(INFERNO, dtype=float)
    return a[i] * (1 - f) + a[i + 1] * f


def _heat_overlay(img, grid, heat):
    """heat: {"maps": {layer: j array}, "grid": Grid, "vmax": A/mm2}."""
    src = heat["grid"]
    vmax = heat.get("vmax") or max((np.nanmax(m["j"]) for m in heat["maps"].values()
                                    if np.isfinite(m["j"]).any()), default=1.0)
    heat["vmax"] = vmax
    cols = np.arange(grid.nx)
    rows = np.arange(grid.ny)
    xs = grid.x0 + (cols + 0.5) * grid.res
    ys = grid.y1 - (rows + 0.5) * grid.res
    sc = np.floor((xs - src.x0) / src.res).astype(int)
    sr = np.floor((src.y1 - ys) / src.res).astype(int)
    okc = (sc >= 0) & (sc < src.nx)
    okr = (sr >= 0) & (sr < src.ny)
    best = np.full(grid.shape, np.nan)
    for lay, m in heat["maps"].items():
        j = m["j"]
        sub = np.full(grid.shape, np.nan)
        rr, cc = np.meshgrid(np.where(okr, sr, 0), np.where(okc, sc, 0), indexing="ij")
        vals = j[rr, cc]
        vals[~okr, :] = np.nan
        vals[:, ~okc] = np.nan
        sub = vals
        best = np.fmax(best, sub)
    sel = np.isfinite(best)
    if not sel.any():
        return
    rgb = colormap(best[sel] / vmax)
    img[sel] = img[sel] * 0.15 + rgb * 0.85


def _legend(pil, heat):
    _, ImageDraw, _ = _pil()
    d = ImageDraw.Draw(pil)
    w, h = pil.size
    font = _font(max(11, h // 70))
    label = f"{heat['vmax']:.0f} A/mm2"
    tw = d.textbbox((0, 0), label, font=font)[2]
    bw, bh = max(12, w // 60), h // 3
    x0, y0 = w - bw - tw - 16, 20
    for i in range(bh):
        c = colormap(np.array(1 - i / bh))
        d.line([(x0, y0 + i), (x0 + bw, y0 + i)], fill=tuple(int(v) for v in c))
    d.text((x0 + bw + 4, y0), label, fill=(255, 255, 255), font=font)
    d.text((x0 + bw + 4, y0 + bh - 12), "0", fill=(255, 255, 255), font=font)


def pdn_png(doc, result, out, width=2000, layers=None):
    heat = {"maps": result.maps, "grid": result.grid}
    nets = [result.net]
    scene = Scene(doc, layers=layers or result.layers, nets=nets, heat=heat)
    rep = render_png(scene, out, width)
    rep["vmax_a_per_mm2"] = round(heat["vmax"], 3)
    return rep


# ------------------------------------------------------------------ svg

def _path(g):
    parts = []
    for coords in _lines(g):
        if len(coords) < 2:
            continue
        parts.append("M" + " L".join(f"{x:.4f},{y:.4f}" for x, y in coords) + " Z")
    return " ".join(parts)


def render_svg(scene, out, heat_png=None):
    x0, y0, x1, y1 = scene.box
    w, h = x1 - x0, y1 - y0
    el = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.4f} {-y1:.4f} {w:.4f} {h:.4f}" '
          f'width="{w * 10:.0f}" height="{h * 10:.0f}">',
          f'<rect x="{x0}" y="{-y1}" width="{w}" height="{h}" fill="rgb{BACKGROUND}"/>',
          '<g transform="scale(1,-1)">']
    dim = scene.nets is not None
    for layer in scene.draw_order():
        color = layer_color(layer)
        items = scene.copper_items(layer) if L.is_copper(layer) else scene.other_items(layer)
        if not items:
            continue
        el.append(f'<g id="layer-{L.name(layer).replace(" ", "_")}" fill="rgb{color}" '
                  f'fill-rule="evenodd">')
        for obj, net, g in items:
            if not scene.in_box(g):
                continue
            op = 0.7 if not dim else (0.95 if net in scene.nets else 0.18)
            el.append(f'<path d="{_path(g)}" fill-opacity="{op}"/>')
        el.append("</g>")
    if heat_png:
        el.append(f'<image href="data:image/png;base64,{heat_png}" x="{x0}" y="{y0}" '
                  f'width="{w}" height="{h}" transform="translate(0,{y0 + y1}) scale(1,-1)" '
                  'opacity="0.85"/>')
    el.append('<g fill="black">' + "".join(f'<path d="{_path(hg)}"/>' for hg in scene.holes()
                                          if scene.in_box(hg)) + "</g>")
    for poly in scene.doc.polygons:
        if poly.layer in scene.layers and len(poly.outline()) >= 2:
            pts = " ".join(f"{x:.4f},{y:.4f}" for x, y in poly.outline())
            el.append(f'<polygon points="{pts}" fill="none" stroke="rgb{layer_color(poly.layer)}" '
                      'stroke-width="0.1" stroke-dasharray="1,0.6"/>')
    outline = scene.doc.outline()
    if outline:
        pts = " ".join(f"{x:.4f},{y:.4f}" for x, y in outline)
        el.append(f'<polygon points="{pts}" fill="none" stroke="rgb{OUTLINE}" stroke-width="0.15"/>')
    labels = []
    if scene.components:
        for des, g, _xy in scene.component_shapes():
            if scene.in_box(g):
                el.append(f'<path d="{_path(g)}" fill="none" stroke="rgb{COMPONENT}" stroke-width="0.08"/>')
                c = g.centroid
                labels.append((c.x, c.y, des, (255, 255, 255)))
    for e in scene.rats():
        (ax, ay), (bx, by) = e["from"], e["to"]
        el.append(f'<line x1="{ax:.4f}" y1="{ay:.4f}" x2="{bx:.4f}" y2="{by:.4f}" '
                  f'stroke="rgb{RATS}" stroke-width="0.08"/>')
    for i, m in enumerate(scene.markers):
        el.append(f'<circle cx="{m["x"]:.4f}" cy="{m["y"]:.4f}" r="0.6" fill="none" '
                  f'stroke="rgb{MARKER}" stroke-width="0.12"/>')
        labels.append((m["x"] + 0.8, m["y"] + 0.4, str(m.get("label", i + 1)), MARKER))
    el.append("</g>")
    for x, y, t, c in labels:
        safe = (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        el.append(f'<text x="{x:.4f}" y="{-y:.4f}" font-size="1" fill="rgb{c}" '
                  f'text-anchor="middle" font-family="sans-serif">{safe}</text>')
    el.append("</svg>")
    Path(out).write_text("\n".join(el), encoding="utf-8")
    return {"path": str(out), "box": [round(v, 3) for v in scene.box],
            "layers": [L.name(x) for x in scene.draw_order()]}


# ------------------------------------------------------------------ diff

def diff_png(a, b, out, layers=None, width=2000, margin=2.0):
    """Copper only in a red, only in b green, in both grey; returns areas."""
    Image, ImageDraw, _ = _pil()
    from .connect import CopperIndex
    sa, sb = Scene(a, layers=layers, margin=margin), Scene(b, layers=layers, margin=margin)
    bx = (min(sa.box[0], sb.box[0]), min(sa.box[1], sb.box[1]),
          max(sa.box[2], sb.box[2]), max(sa.box[3], sb.box[3]))
    res = (bx[2] - bx[0]) / max(64, width)
    grid = Grid(*bx, res)
    img = np.empty(grid.shape + (3,), dtype=float)
    img[:] = BACKGROUND
    stats = {}
    copper = [x for x in sa.layers if L.is_copper(x)]
    for layer in copper:
        ma, mb = grid.empty(), grid.empty()
        for _o, _n, g in sa.copper_items(layer):
            draw(ma, grid, g)
        for _o, _n, g in sb.copper_items(layer):
            draw(mb, grid, g)
        both, only_a, only_b = ma & mb, ma & ~mb, mb & ~ma
        _blend(img, both, (110, 110, 110), 0.5)
        _blend(img, only_a, (255, 50, 50), 0.95)
        _blend(img, only_b, (60, 255, 60), 0.95)
        px2 = res * res
        stats[L.name(layer)] = {"removed_mm2": round(float(only_a.sum()) * px2, 3),
                                "added_mm2": round(float(only_b.sum()) * px2, 3)}
    pil = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(pil)
    for doc, color in ((a, (255, 120, 120)), (b, (120, 255, 120))):
        o = doc.outline()
        if o:
            d.line([((x - grid.x0) / res, (grid.y1 - y) / res) for x, y in o + [o[0]]],
                   fill=color, width=2)
    pil.save(out)
    return {"path": str(out), "layers": stats, "legend": "red: only in A, green: only in B, grey: both"}


# ------------------------------------------------------------------ entry points

def component_box(doc, designator, margin):
    c = doc.component(designator)
    pts = [(p.x, p.y) for p in doc.pads if p.component == c.index] or [(c.x, c.y)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


def render(doc, out, layers=None, nets=None, around=None, margin=10.0, box=None,
           ratsnest=False, markers=None, heat=None, width=2000, components=True):
    if around:
        box = component_box(doc, around, margin)
    scene = Scene(doc, layers=layers, nets=nets, box=box, ratsnest=ratsnest, markers=markers,
                  heat=heat, components=components)
    out = Path(out)
    if out.suffix.lower() == ".svg":
        heat_png = None
        if heat is not None:
            buf = io.BytesIO()
            tmp = Scene(doc, layers=[], box=scene.box, heat=heat, components=False)
            Image, _, _ = _pil()
            grid = Grid(*scene.box, (scene.box[2] - scene.box[0]) / width)
            arr = np.zeros(grid.shape + (3,))
            _heat_overlay(arr, grid, heat)
            Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(buf, "PNG")
            heat_png = base64.b64encode(buf.getvalue()).decode()
        return render_svg(scene, out, heat_png)
    return render_png(scene, out, width)


def load_markers(path):
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        data = data.get("violations") or data.get("markers") or []
    out = []
    for i, m in enumerate(data):
        if "x" in m and "y" in m:
            out.append({"x": float(m["x"]), "y": float(m["y"]),
                        "label": m.get("label") or m.get("rule") or m.get("kind") or i + 1})
    return out


def cmd_render(args):
    from .pcbdoc import PcbDoc
    doc = PcbDoc.open(args.board)
    if args.diff:
        rep = diff_png(doc, PcbDoc.open(args.diff), args.out, args.layers, args.width)
        print(json.dumps(rep, indent=2))
        return 0
    heat = None
    if args.pdn:
        from . import pdn
        if not (args.source and args.sink and args.current):
            raise ValueError("--pdn needs --source, --sink and --current")
        res = pdn.solve(doc, args.pdn, args.source, args.sink, args.current, res=args.pdn_res)
        heat = {"maps": res.maps, "grid": res.grid}
    markers = load_markers(args.markers) if args.markers else None
    box = tuple(args.box) if args.box else None
    rep = render(doc, args.out, layers=args.layers, nets=args.net, around=args.around,
                 margin=args.margin, box=box, ratsnest=args.ratsnest, markers=markers,
                 heat=heat, width=args.width, components=not args.no_components)
    print(json.dumps(rep, indent=2))
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("board")
        p.add_argument("-o", "--out", required=True, help="out.png or out.svg")
        p.add_argument("--layers", nargs="*", help="layer names; default copper + overlays")
        p.add_argument("--net", action="append", help="highlight a net (repeatable)")
        p.add_argument("--around", help="zoom to a component's pads")
        p.add_argument("--margin", type=float, default=10.0, help="mm around --around")
        p.add_argument("--box", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"))
        p.add_argument("--ratsnest", action="store_true", help="draw unrouted connections")
        p.add_argument("--markers", help="JSON list of {x, y, label} (e.g. DRC output)")
        p.add_argument("--pdn", metavar="NET", help="overlay current density for this net")
        p.add_argument("--source", nargs="+")
        p.add_argument("--sink", nargs="+")
        p.add_argument("--current", type=float)
        p.add_argument("--pdn-res", type=float, default=0.1)
        p.add_argument("--diff", help="second board: draw what changed")
        p.add_argument("--width", type=int, default=2000, help="image width in pixels")
        p.add_argument("--no-components", action="store_true")
    command("render", cmd_render, "draw a board to PNG or SVG", build)
