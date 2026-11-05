"""Offline design rule check: the board's own rules (Rules6), without Altium.

    python -m offline drc board.PcbDoc [--json] [--box X0 Y0 X1 Y1] [--check Clearance Width ...]
                                       [--preset jlcpcb-2oz] [--copper auto|saved|repour]
                                       [--markers drc.json] [--compare-cached]

Checks, each driven by the board's rule of that kind (scopes through scope.py):
Clearance (copper of different nets, keep-outs, bare drill holes, copper
text), ShortCircuit, UnRoutedNet, Width, HoleSize, MinimumAnnularRing,
HoleToHoleClearance, BoardOutlineClearance, SilkToSolderMaskClearance and
ComponentClearance (3D body outlines and heights). A DFM preset adds a fab's
published limits for 2 oz copper on top of the board's rules.

Pours: by default the copper saved in the file, with polygons that are
unpoured (or listed stale) poured by pour.py first; the report names the
polygons it repoured. Objects without a net that touch are taken as joined
(Altium does not flag them); keep-outs are checked against every copper
object, their own footprint's included.

Approximations, flagged per violation: text is its box; vias are tented
unless a SolderMaskExpansion rule says otherwise.
"""

import json
import math
import time
from collections import Counter

from . import geom
from . import layers as L
from .pcbdoc import (KEEPOUT_COPPER, KEEPOUT_SMD_PAD, KEEPOUT_TH_PAD, KEEPOUT_TRACK, KEEPOUT_VIA,
                     parse_bool)
from .rules import RuleSet, clearance_class

TOL = 5e-4          # mm: distances this close to the limit pass
TILE = 8.0          # mm: big shapes are cut into tiles this size for the spatial index
TILE_POINTS = 600   # shapes with more vertices than this are tiled

CHECKS = ("Clearance", "ShortCircuit", "UnRoutedNet", "Width", "HoleSize", "MinimumAnnularRing",
          "HoleToHoleClearance", "BoardOutlineClearance", "SilkToSolderMaskClearance",
          "ComponentClearance")

# Fab limits for 2 oz (70 um) finished outer copper, from each fab's own
# capability page as read on the date given. Fabs change these: check the page.
PRESETS = {
    "jlcpcb-2oz": {
        "source": "https://jlcpcb.com/capabilities/pcb-capabilities",
        "read": "2026-11-04",
        "notes": "2-layer rows; via ring from 'via diameter 0.1 mm larger than hole'",
        "min_trace": 0.16, "min_space": 0.16, "min_drill": 0.15,
        "min_ring_via": 0.05, "min_ring_pth": 0.254,
        "hole_to_hole_via": 0.20, "hole_to_hole_pad": 0.45,
        "hole_to_copper_via": 0.20, "hole_to_copper_pth": 0.28,
        "copper_to_edge": 0.20,
    },
    "pcbway-2oz": {
        "source": "https://www.pcbway.com/capabilities.html",
        "read": "2026-11-04",
        "notes": "advanced table, 70 um outer copper, normal-process column",
        "min_trace": 0.178, "min_space": 0.203, "min_drill": 0.20,
        "min_ring_via": 0.178, "min_ring_pth": 0.305,
        "hole_to_hole_via": 0.279, "hole_to_hole_pad": 0.406,
        "copper_to_edge": 0.25,
    },
    "oshpark-2oz": {
        "source": "https://docs.oshpark.com/services/two-layer-hhdc/",
        "read": "2026-11-04",
        "notes": "2 Layer 2oz 0.8mm service; edge value is the board edge keepout",
        "min_trace": 0.1524, "min_space": 0.1524, "min_drill": 0.254,
        "min_ring_via": 0.127, "min_ring_pth": 0.127,
        "hole_to_hole_via": 0.127, "hole_to_hole_pad": 0.127,
        "copper_to_edge": 0.381,
    },
}


class Violation:
    __slots__ = ("rule", "kind", "layer", "x", "y", "actual", "limit", "objects", "message",
                 "approximate")

    def __init__(self, rule, kind, layer, x, y, actual, limit, objects, message, approximate=False):
        self.rule, self.kind, self.layer = rule, kind, layer
        self.x, self.y, self.actual, self.limit = x, y, actual, limit
        self.objects, self.message, self.approximate = objects, message, approximate

    def as_dict(self):
        r4 = lambda v: None if v is None else round(v, 4)  # noqa: E731
        return {"rule": self.rule, "kind": self.kind, "layer": self.layer,
                "x": r4(self.x), "y": r4(self.y), "actual_mm": r4(self.actual),
                "limit_mm": r4(self.limit), "objects": self.objects, "message": self.message,
                "approximate": self.approximate}


class _Item:
    __slots__ = ("obj", "kind", "net", "shape", "layer", "hole", "keepout", "approx")

    def __init__(self, obj, kind, net, shape, layer, hole=False, keepout=False, approx=False):
        self.obj, self.kind, self.net, self.shape, self.layer = obj, kind, net, shape, layer
        self.hole, self.keepout, self.approx = hole, keepout, approx


def _restricts(keepout, it):
    """Whether a keep-out object keeps out the object of item `it`."""
    mask = keepout.keepout_restrictions
    k = it.kind
    if k == "via" or (it.hole and it.obj.KIND == "via"):
        return bool(mask & KEEPOUT_VIA)
    if k in ("track", "arc"):
        return bool(mask & KEEPOUT_TRACK)
    if k == "pad" or it.hole:
        return bool(mask & (KEEPOUT_SMD_PAD if it.obj.is_smd else KEEPOUT_TH_PAD))
    return bool(mask & KEEPOUT_COPPER)   # fills, regions, pours, copper text


def _tiles(g):
    """A big shape cut into TILE-sized pieces (all of it, nothing added)."""
    sh = geom.require()
    if sh.get_num_coordinates(g) <= TILE_POINTS:
        return [g]
    x0, y0, x1, y1 = g.bounds
    out = []
    nx, ny = max(1, math.ceil((x1 - x0) / TILE)), max(1, math.ceil((y1 - y0) / TILE))
    for i in range(nx):
        for j in range(ny):
            part = sh.clip_by_rect(g, x0 + i * TILE, y0 + j * TILE,
                                   x0 + (i + 1) * TILE, y0 + (j + 1) * TILE)
            if not part.is_empty:
                out.append(part)
    return out or [g]


class DRC:
    def __init__(self, doc, copper="auto", stale=(), box=None, checks=None, preset=None,
                 rules=None, text=False):
        from .pour import PourEngine
        self.doc = doc
        self.box = box
        self.checks = set(checks or CHECKS)
        bad = self.checks - set(CHECKS)
        if bad:
            raise ValueError(f"unknown check(s) {sorted(bad)}; known: {', '.join(CHECKS)}")
        if preset is not None and preset not in PRESETS:
            raise ValueError(f"unknown preset {preset!r}; known: {', '.join(PRESETS)}")
        self.preset = PRESETS.get(preset)
        self.preset_name = preset
        self.pour_used = PourEngine(doc).refresh(copper, stale)
        self.copper_policy = copper
        self.rules = rules or RuleSet(doc)
        self.text = text
        self.violations = []
        self.skipped = [] if text else [
            "text: not checked (no glyph outlines here; --text checks each text's box)"]
        self.seconds = {}
        self._layer_cache = {}
        sh = geom.require()
        self._box = sh.box(*box) if box else None

    # ------------------------------------------------------------ helpers
    def _in_box(self, g):
        return self._box is None or g.intersects(self._box)

    def has_rules(self, kind):
        return bool(self.rules.of_kind(kind))

    def describe(self, obj, kind=None):
        doc = self.doc
        kind = kind or getattr(obj, "KIND", "?")
        d = {"kind": kind, "index": getattr(obj, "index", None)}
        net = getattr(obj, "effective_net", None) if hasattr(obj, "effective_net") else None
        if net is not None and net < len(doc.nets):
            d["net"] = doc.nets[net].name
        comp = getattr(obj, "component", None)
        if comp is not None and comp < len(doc.components):
            d["component"] = doc.components[comp].designator
        lay = getattr(obj, "layer", None)
        if lay is not None:
            d["layer"] = L.name(lay)
        if getattr(obj, "KIND", "") == "pad":
            d["pad"] = f"{d.get('component', 'Free')}-{obj.name}"
        if getattr(obj, "KIND", "") == "region" and getattr(obj, "polygon", None) is not None:
            d["polygon"] = obj.polygon
        if getattr(obj, "KIND", "") == "text":
            d["text"] = obj.text
        if getattr(obj, "is_keepout", False):
            d["keepout"] = True
        return d

    def _label(self, d):
        what = d.get("pad") and f"Pad {d['pad']}" or \
            (f"Polygon {d['polygon']} pour" if "polygon" in d else d["kind"].capitalize())
        if d.get("keepout"):
            what = f"Keep-out {d['kind']}"
        if d["kind"] == "hole":
            what = f"Hole of {d.get('pad') and 'pad ' + d['pad'] or 'via'}"
        net = d.get("net")
        return f"{what} ({net})" if net else what

    def add(self, rule, kind, layer, at, actual, limit, objs, message, approximate=False):
        x, y = at if at is not None else (None, None)
        self.violations.append(Violation(rule, kind, layer, x, y, actual, limit, objs, message,
                                         approximate))

    # ------------------------------------------------------------ items
    def layer_items(self, layer):
        """Copper, keep-outs, bare holes and copper text on one copper layer."""
        if layer in self._layer_cache:
            return self._layer_cache[layer]
        doc = self.doc
        items = []
        for obj in doc.copper(layer=layer):
            g = geom.copper_shape(obj, layer, fine=True)
            if g.is_empty or not self._in_box(g):
                continue
            kind = obj.KIND
            for part in (_tiles(g) if kind == "region" else [g]):
                items.append(_Item(obj, kind, obj.effective_net, part, layer))
            if kind in ("pad", "via") and obj.hole > 0:
                h = geom.hole_shape(obj)
                if not h.is_empty and not g.buffer(1e-6).contains(h):
                    items.append(_Item(obj, "hole", obj.effective_net, h, layer, hole=True))
        for obj in doc.pads:  # holes of pads with no copper on this layer
            if obj.hole > 0 and obj.on_layer(layer) and geom.pad_shape(obj, layer).is_empty:
                h = geom.hole_shape(obj)
                if not h.is_empty and self._in_box(h):
                    items.append(_Item(obj, "hole", obj.effective_net, h, layer, hole=True))
        for t in (doc.texts if self.text else []):
            if t.layer == layer:
                g = geom.text_box(t)
                if self._in_box(g):
                    items.append(_Item(t, "text", None, g, layer, approx=True))
        for kind, lst in (("track", doc.tracks), ("arc", doc.arcs), ("fill", doc.fills),
                          ("region", doc.regions)):
            for r in lst:
                if kind == "region" and r.kind != "copper":
                    continue
                if r.layer == L.KEEPOUT or (r.is_keepout and r.layer == layer):
                    g = geom.copper_shape(r, layer, fine=True)
                    if not g.is_empty and self._in_box(g):
                        items.append(_Item(r, kind, None, g, layer, keepout=True))
        self._layer_cache[layer] = items
        return items

    @staticmethod
    def _cls(it):
        if it.hole:
            return "Hole"
        if it.kind == "text":
            return "Text"
        return clearance_class(it.obj)

    # ------------------------------------------------------------ checks
    def run(self):
        steps = (("Clearance", self.check_clearance), ("Width", self.check_width),
                 ("HoleSize", self.check_hole_size), ("MinimumAnnularRing", self.check_rings),
                 ("HoleToHoleClearance", self.check_hole_to_hole),
                 ("BoardOutlineClearance", self.check_board_outline),
                 ("SilkToSolderMaskClearance", self.check_silk),
                 ("ComponentClearance", self.check_components),
                 ("UnRoutedNet", self.check_unrouted))
        for name, fn in steps:
            if name in self.checks or (name == "Clearance" and "ShortCircuit" in self.checks):
                t0 = time.time()
                fn()
                self.seconds[name] = round(time.time() - t0, 3)
        return self.violations

    def check_clearance(self):
        sh = geom.require()
        import numpy as np
        rules = self.rules
        clear_on = "Clearance" in self.checks and self.has_rules("Clearance")
        short_on = "ShortCircuit" in self.checks and self.has_rules("ShortCircuit")
        space = self.preset["min_space"] if self.preset else 0.0
        if not (clear_on or short_on or space):
            self.skipped.append("Clearance: no Clearance or ShortCircuit rule")
            return
        reach = max(rules.max_clearance() if clear_on else 0.0, space) + TOL
        found = {}
        for layer in self.doc.copper_layers:
            if not L.is_signal(layer):
                continue
            items = self.layer_items(layer)
            if len(items) < 2:
                continue
            shapes = np.array([it.shape for it in items], dtype=object)
            tree = sh.STRtree(shapes)
            left, right = tree.query(shapes, predicate="dwithin", distance=reach)
            keep = left < right
            left, right = left[keep], right[keep]
            if not len(left):
                continue
            dist = sh.distance(shapes[left], shapes[right])
            for i, j, d in zip(left.tolist(), right.tolist(), dist.tolist()):
                a, b = items[i], items[j]
                if a.obj is b.obj:
                    continue
                if a.keepout and b.keepout:
                    continue
                if (a.keepout and not _restricts(a.obj, b)) or (b.keepout and not _restricts(b.obj, a)):
                    continue
                if not (a.keepout or b.keepout):
                    if a.net is not None and a.net == b.net:
                        continue
                    if a.net is None and b.net is None and d <= 0:
                        continue  # no-net copper that touches is one piece
                if a.hole and b.hole:
                    continue  # hole-to-hole has its own rule
                key = tuple(sorted(((a.obj.storage, a.obj.index, a.hole), (b.obj.storage, b.obj.index, b.hole))))
                if clear_on:
                    gap, rule = rules.clearance(a.obj, b.obj, classes=(self._cls(a), self._cls(b)))
                    if gap is not None and rule is not None:
                        if a.kind == b.kind == "pad" and a.obj.component is not None and \
                                a.obj.component == b.obj.component and \
                                parse_bool(rule.rule.props.get("IGNOREPADTOPADCLEARANCEINFOOTPRINT")):
                            gap = None
                    if gap is not None and d < gap - TOL:
                        self._keep(found, key + ("Clearance",), rule.name, "Clearance", layer, a, b,
                                   d, gap, a.approx or b.approx)
                if short_on and d <= 0 and a.net is not None and b.net is not None and \
                        not (a.keepout or b.keepout or a.hole or b.hole) and \
                        a.shape.intersects(b.shape):
                    allowed, rule = rules.short_allowed(a.obj, b.obj)
                    if rule is not None and not allowed:
                        self._keep(found, key + ("ShortCircuit",), rule.name, "ShortCircuit",
                                   layer, a, b, 0.0, 0.0, False)
                if space and d < space - TOL and not (a.keepout or b.keepout or a.hole or b.hole
                                                      or a.kind == "text" or b.kind == "text"):
                    self._keep(found, key + ("dfm",), f"dfm:{self.preset_name}", "Clearance",
                               layer, a, b, d, space, False)
        for key, (rule, kind, layer, a, b, d, gap, approx, at) in found.items():
            da, db = self.describe(a.obj, "hole" if a.hole else None), \
                self.describe(b.obj, "hole" if b.hole else None)
            what = "Short circuit" if kind == "ShortCircuit" else \
                f"Clearance {d:.4f} mm < {gap:.4f} mm"
            self.add(rule, kind, L.name(layer), at, d, gap, [da, db],
                     f"{what} between {self._label(da)} and {self._label(db)}", approx)

    def _keep(self, found, key, rule, kind, layer, a, b, d, gap, approx):
        old = found.get(key)
        if old is not None and old[5] <= d:
            return
        sh = geom.require()
        line = sh.shortest_line(a.shape, b.shape)
        c = line.interpolate(0.5, normalized=True)
        found[key] = (rule, kind, layer, a, b, d, gap, approx, (c.x, c.y))

    def check_width(self):
        if not self.has_rules("Width") and not self.preset:
            self.skipped.append("Width: no Width rule")
            return
        sh = geom.require()
        for obj in list(self.doc.tracks) + list(self.doc.arcs):
            if obj.polygon is not None or obj.is_keepout or not L.is_signal(obj.layer):
                continue
            g = None
            if self._box is not None:
                g = geom.copper_shape(obj, obj.layer)
                if not self._in_box(g):
                    continue
            at = ((obj.x1 + obj.x2) / 2, (obj.y1 + obj.y2) / 2) if obj.KIND == "track" else \
                (obj.cx + obj.radius * math.cos(math.radians((obj.start_angle + obj.end_angle) / 2)),
                 obj.cy + obj.radius * math.sin(math.radians((obj.start_angle + obj.end_angle) / 2)))
            w = self.rules.width(obj) if self.has_rules("Width") else None
            desc = [self.describe(obj)]
            if w is not None:
                if obj.width < w["min"] - TOL:
                    self.add(w["rule"], "Width", L.name(obj.layer), at, obj.width, w["min"], desc,
                             f"{self._label(desc[0])} {obj.width:.4f} mm is under the minimum "
                             f"{w['min']:.4f} mm")
                elif w["max"] and obj.width > w["max"] + TOL:
                    self.add(w["rule"], "Width", L.name(obj.layer), at, obj.width, w["max"], desc,
                             f"{self._label(desc[0])} {obj.width:.4f} mm is over the maximum "
                             f"{w['max']:.4f} mm")
            if self.preset and obj.layer in (L.TOP, L.BOTTOM) and \
                    obj.width < self.preset["min_trace"] - TOL:
                self.add(f"dfm:{self.preset_name}", "Width", L.name(obj.layer), at, obj.width,
                         self.preset["min_trace"], desc,
                         f"{self._label(desc[0])} {obj.width:.4f} mm is under the fab's "
                         f"{self.preset['min_trace']} mm")
        del sh

    def _drilled(self):
        out = []
        for v in self.doc.vias:
            if v.hole > 0:
                out.append(v)
        for p in self.doc.pads:
            if p.hole > 0:
                out.append(p)
        if self._box is not None:
            out = [o for o in out if self._in_box(geom.hole_shape(o))]
        return out

    def check_hole_size(self):
        on = self.has_rules("HoleSize")
        if not on and not self.preset:
            self.skipped.append("HoleSize: no HoleSize rule")
            return
        for o in self._drilled():
            desc = [self.describe(o)]
            at = (o.x, o.y)
            if on:
                hs = self.rules.hole_size(o)
                if hs and hs.get("relative"):
                    self.skipped.append(f"HoleSize {hs['rule']}: relative limits not checked")
                elif hs:
                    if o.hole < hs["min"] - TOL:
                        self.add(hs["rule"], "HoleSize", "Multi-Layer", at, o.hole, hs["min"], desc,
                                 f"{self._label(desc[0])} hole {o.hole:.4f} mm is under "
                                 f"{hs['min']:.4f} mm")
                    elif hs["max"] and o.hole > hs["max"] + TOL:
                        self.add(hs["rule"], "HoleSize", "Multi-Layer", at, o.hole, hs["max"], desc,
                                 f"{self._label(desc[0])} hole {o.hole:.4f} mm is over "
                                 f"{hs['max']:.4f} mm")
            if self.preset and o.hole < self.preset["min_drill"] - TOL:
                self.add(f"dfm:{self.preset_name}", "HoleSize", "Multi-Layer", at, o.hole,
                         self.preset["min_drill"], desc,
                         f"{self._label(desc[0])} hole {o.hole:.4f} mm is under the fab's "
                         f"{self.preset['min_drill']} mm")
        self.skipped = list(dict.fromkeys(self.skipped))

    def ring(self, o):
        """(smallest annular ring mm, layer) over the copper layers it has copper on."""
        best = None
        h = geom.hole_shape(o)
        for lay in self.doc.copper_layers:
            if o.KIND == "via":
                if not o.spans(lay, self.doc.copper_layers):
                    continue
                r = (o.diameter_on(lay) - o.hole) / 2.0
            else:
                if not o.on_layer(lay):
                    continue
                g = geom.pad_shape(o, lay)
                if g.is_empty:
                    continue
                r = g.exterior.distance(h) if g.contains(h) else 0.0
            if best is None or r < best[0]:
                best = (r, lay)
        return best

    def check_rings(self):
        on = self.has_rules("MinimumAnnularRing")
        if not on and not self.preset:
            self._no_rule("MinimumAnnularRing")
            return
        for o in self._drilled():
            if o.KIND == "pad" and not o.plated:
                continue
            got = self.ring(o)
            if got is None:
                continue
            ring, lay = got
            desc = [self.describe(o)]
            if on:
                need, rule = self.rules.annular_ring(o)
                if need is not None and ring < need - TOL:
                    self.add(rule.name, "MinimumAnnularRing", L.name(lay), (o.x, o.y), ring, need,
                             desc, f"{self._label(desc[0])} ring {ring:.4f} mm < {need:.4f} mm")
            if self.preset:
                need = self.preset["min_ring_via" if o.KIND == "via" else "min_ring_pth"]
                if ring < need - TOL:
                    self.add(f"dfm:{self.preset_name}", "MinimumAnnularRing", L.name(lay),
                             (o.x, o.y), ring, need, desc,
                             f"{self._label(desc[0])} ring {ring:.4f} mm < fab {need} mm")

    def check_hole_to_hole(self):
        on = self.has_rules("HoleToHoleClearance")
        if not on and not self.preset:
            self._no_rule("HoleToHoleClearance")
            return
        sh = geom.require()
        import numpy as np
        objs = self._drilled()
        if len(objs) < 2:
            return
        shapes = np.array([geom.hole_shape(o) for o in objs], dtype=object)
        reach = max([c.length("GAP") for c in self.rules.of_kind("HoleToHoleClearance")] +
                    [self.preset["hole_to_hole_pad"] if self.preset else 0.0]) + TOL
        tree = sh.STRtree(shapes)
        left, right = tree.query(shapes, predicate="dwithin", distance=reach)
        keep = left < right
        left, right = left[keep], right[keep]
        if not len(left):
            return
        dist = sh.distance(shapes[left], shapes[right])
        for i, j, d in zip(left.tolist(), right.tolist(), dist.tolist()):
            a, b = objs[i], objs[j]
            da, db = self.describe(a), self.describe(b)
            at = ((a.x + b.x) / 2, (a.y + b.y) / 2)
            if on:
                gap, rule = self.rules.hole_to_hole(a, b)
                if gap is not None and d < gap - TOL:
                    self.add(rule.name, "HoleToHoleClearance", "Multi-Layer", at, d, gap, [da, db],
                             f"Holes of {self._label(da)} and {self._label(db)} {d:.4f} mm apart "
                             f"< {gap:.4f} mm")
            if self.preset:
                need = self.preset["hole_to_hole_via" if a.KIND == b.KIND == "via"
                                   else "hole_to_hole_pad"]
                if d < need - TOL:
                    self.add(f"dfm:{self.preset_name}", "HoleToHoleClearance", "Multi-Layer", at, d,
                             need, [da, db], f"Holes {d:.4f} mm apart < fab {need} mm")
        if self.preset and self.preset.get("hole_to_copper_via"):
            self._hole_to_copper(objs)

    def _hole_to_copper(self, objs):
        """Fab drill-to-copper limits: other nets' copper near a drilled hole."""
        sh = geom.require()
        reach = max(self.preset.get("hole_to_copper_via", 0), self.preset.get("hole_to_copper_pth", 0))
        for layer in self.doc.copper_layers:
            if not L.is_signal(layer):
                continue
            items = [it for it in self.layer_items(layer)
                     if not (it.hole or it.keepout or it.kind == "text")]
            if not items:
                continue
            tree = sh.STRtree([it.shape for it in items])
            for o in objs:
                need = self.preset["hole_to_copper_via" if o.KIND == "via" else "hole_to_copper_pth"]
                h = geom.hole_shape(o)
                for k in tree.query(h, predicate="dwithin", distance=reach):
                    it = items[k]
                    if it.obj is o or (it.net is not None and it.net == o.effective_net):
                        continue
                    d = h.distance(it.shape)
                    if d < need - TOL:
                        da, db = self.describe(o, "hole"), self.describe(it.obj)
                        self.add(f"dfm:{self.preset_name}", "HoleToCopper", L.name(layer),
                                 (o.x, o.y), d, need, [da, db],
                                 f"{self._label(db)} {d:.4f} mm from the hole of "
                                 f"{self._label(self.describe(o))} < fab {need} mm")

    def _no_rule(self, kind):
        if self.rules.has_unevaluated(kind):
            self.skipped.append(f"{kind}: its rule(s) could not be evaluated (see "
                                "unevaluated_rules); not checked")
        else:
            self.skipped.append(f"{kind}: no rule")

    def check_board_outline(self):
        on = self.has_rules("BoardOutlineClearance")
        edge = self.preset["copper_to_edge"] if self.preset else 0.0
        if not on and not edge:
            self._no_rule("BoardOutlineClearance")
            return
        sh = geom.require()
        import numpy as np
        board = geom.board_shape(self.doc)
        if board is None or board.is_empty:
            self.skipped.append("BoardOutlineClearance: board has no outline")
            return
        rim = board.boundary
        found = {}
        for layer in self.doc.copper_layers:
            if not L.is_signal(layer):
                continue
            items = [it for it in self.layer_items(layer)
                     if not (it.keepout or it.hole or it.kind == "text")]
            if not items:
                continue
            shapes = np.array([it.shape for it in items], dtype=object)
            dist = sh.distance(shapes, rim)
            inside = sh.covers(board, shapes)
            for it, d, ok in zip(items, dist.tolist(), inside.tolist()):
                if not ok:
                    d = 0.0
                limit, rname = None, None
                if on:
                    gap, rule = self.rules.board_outline(it.obj)
                    if gap is not None:
                        limit, rname = gap, rule.name
                for lim, name in ((limit, rname),
                                  (edge or None, f"dfm:{self.preset_name}" if edge else None)):
                    if lim is None or d >= lim - TOL:
                        continue
                    key = (it.obj.storage, it.obj.index, name)
                    if key in found and found[key][1] <= d:
                        continue
                    p = sh.shortest_line(it.shape, rim).interpolate(0.5, normalized=True)
                    found[key] = (name, d, lim, layer, it, (p.x, p.y))
        for name, d, lim, layer, it, at in found.values():
            desc = [self.describe(it.obj)]
            self.add(name, "BoardOutlineClearance", L.name(layer), at, d, lim, desc,
                     f"{self._label(desc[0])} {d:.4f} mm from the board edge < {lim:.4f} mm")

    def check_silk(self):
        if not self.has_rules("SilkToSolderMaskClearance"):
            self._no_rule("SilkToSolderMaskClearance")
            return
        sh = geom.require()
        doc = self.doc
        for side, overlay, copper in (("top", L.TOP_OVERLAY, L.TOP), ("bottom", L.BOTTOM_OVERLAY, L.BOTTOM)):
            silk = []
            for kind, lst in (("track", doc.tracks), ("arc", doc.arcs), ("fill", doc.fills),
                              ("region", doc.regions)):
                for r in lst:
                    if r.layer == overlay and (kind != "region" or r.kind == "copper"):
                        silk.append((r, geom.copper_shape(r, overlay), False))
            for t in (doc.texts if self.text else []):
                if t.layer == overlay:
                    silk.append((t, geom.text_box(t), True))
            silk = [s for s in silk if not s[1].is_empty and self._in_box(s[1])]
            openings = []
            for p in doc.pads:
                if p.on_layer(copper):
                    g = geom.pad_shape(p, copper)
                    if not g.is_empty:
                        openings.append((p, g.buffer(self.rules.solder_mask_expansion(p),
                                                     join_style="mitre")))
            for v in doc.vias:
                if v.spans(copper, doc.copper_layers) and not self.rules.tented(v, side):
                    openings.append((v, geom.via_shape(v, copper,
                                                       self.rules.solder_mask_expansion(v))))
            if not silk or not openings:
                continue
            reach = max(c.length("MINSILKSCREENTOMASKGAP")
                        for c in self.rules.of_kind("SilkToSolderMaskClearance")) + TOL
            tree = sh.STRtree([g for _o, g in openings])
            for s_obj, s_shape, approx in silk:
                for k in tree.query(s_shape, predicate="dwithin", distance=reach):
                    o, g = openings[k]
                    gap, rule = self.rules.silk_to_mask(o, s_obj)
                    if gap is None:
                        continue
                    d = s_shape.distance(g)
                    if d < gap - TOL or (gap <= 0 and s_shape.intersection(g).area > 1e-6):
                        p = sh.shortest_line(s_shape, g).interpolate(0.5, normalized=True)
                        da, db = self.describe(o), self.describe(s_obj)
                        self.add(rule.name, "SilkToSolderMaskClearance", L.name(overlay), (p.x, p.y),
                                 d, gap, [da, db],
                                 f"Silk {self._label(db)} {d:.4f} mm from the mask opening of "
                                 f"{self._label(da)} < {gap:.4f} mm" +
                                 (" (overlaps)" if d <= 0 else ""), approx)

    def check_components(self):
        if not self.has_rules("ComponentClearance"):
            self._no_rule("ComponentClearance")
            return
        sh = geom.require()
        doc = self.doc
        bodies = {}
        st = doc.storages.get("ComponentBodies6")
        for b in (st.records if st else []):
            if b.component is None:
                continue
            g = geom.region_shape(b)
            if g.is_empty:
                continue
            z0, z1 = b.standoff, max(b.overall_height, b.standoff)
            bodies.setdefault(b.component, []).append((g, z0, z1))
        missing = [c.designator for c in doc.components if c.index not in bodies]
        if missing:
            self.skipped.append(f"ComponentClearance: {len(missing)} component(s) without a 3D "
                                f"body not checked ({', '.join(missing[:10])}"
                                f"{', ...' if len(missing) > 10 else ''})")
        comps = [c for c in doc.components if c.index in bodies]
        if self._box is not None:
            comps = [c for c in comps if any(self._in_box(g) for g, _a, _b in bodies[c.index])]
        unions = [sh.union_all([g for g, _a, _b in bodies[c.index]]) for c in comps]
        if len(comps) < 2:
            return
        reach = max(max(c.length("GAP"), 0.0) for c in self.rules.of_kind("ComponentClearance")) + TOL
        tree = sh.STRtree(unions)
        left, right = tree.query(unions, predicate="dwithin", distance=reach)
        for i, j in zip(left.tolist(), right.tolist()):
            if i >= j:
                continue
            a, b = comps[i], comps[j]
            if (a.layer == L.BOTTOM) != (b.layer == L.BOTTOM):
                continue  # opposite sides
            gap, rule = self.rules.component_clearance(a, b)
            if gap is None:
                continue
            vgap = rule.length("VERTICALGAP", gap)
            worst = None
            for ga, a0, a1 in bodies[a.index]:
                for gb, b0, b1 in bodies[b.index]:
                    vsep = max(b0 - a1, a0 - b1)   # > 0: one sits above the other
                    if vsep >= vgap - TOL:
                        continue
                    d = ga.distance(gb)
                    if ga.intersects(gb):
                        d = -ga.intersection(gb).area ** 0.5 if ga.intersection(gb).area > 0 else 0.0
                    if d < gap - TOL and (worst is None or d < worst[0]):
                        worst = (d, ga, gb)
            if worst is not None:
                d, ga, gb = worst
                p = sh.shortest_line(ga, gb).interpolate(0.5, normalized=True) \
                    if d > 0 else ga.intersection(gb).representative_point()
                da = {"kind": "component", "component": a.designator}
                db = {"kind": "component", "component": b.designator}
                self.add(rule.name, "ComponentClearance", "Top" if a.layer != L.BOTTOM else "Bottom",
                         (p.x, p.y), max(d, 0.0), gap, [da, db],
                         f"Components {a.designator} and {b.designator} "
                         + ("overlap" if d <= 0 else f"{d:.4f} mm apart") + f" < {gap:.4f} mm "
                         "(3D body outlines and heights; Altium checks the model shapes)", True)

    def check_unrouted(self):
        if not self.has_rules("UnRoutedNet"):
            self._no_rule("UnRoutedNet")
            return
        from .connect import Connectivity
        rule = self.rules.of_kind("UnRoutedNet")[0]
        c = Connectivity(self.doc)
        for e in c.unrouted():
            fx, fy = e["from"][:2]
            tx, ty = e["to"][:2]
            at = ((fx + tx) / 2, (fy + ty) / 2)
            if self._box is not None:
                from shapely import Point
                if not self._box.contains(Point(*at)):
                    continue
            self.add(rule.name, "UnRoutedNet", None, at, e["gap_mm"], 0.0,
                     [{"kind": "net", "net": e["net_name"]}],
                     f"Net {e['net_name']}: two pieces not joined, {e['gap_mm']:.3f} mm apart")

    # ------------------------------------------------------------ output
    def report(self):
        by_rule = Counter(v.rule for v in self.violations)
        by_kind = Counter(v.kind for v in self.violations)
        repoured = sorted(i for i, u in self.pour_used.items() if u == "repoured")
        return {
            "summary": {"violations": len(self.violations), "by_kind": dict(by_kind),
                        "by_rule": dict(by_rule),
                        "approximate": sum(1 for v in self.violations if v.approximate)},
            "copper": {"policy": self.copper_policy, "repoured_polygons": repoured,
                       "saved_polygons": sorted(i for i, u in self.pour_used.items()
                                                if u == "saved")},
            "preset": dict(PRESETS[self.preset_name], name=self.preset_name) if self.preset else None,
            "unevaluated_rules": self.rules.unevaluated,
            "skipped": self.skipped,
            "warnings": self.doc.board_warnings(),
            "seconds": self.seconds,
            "violations": [v.as_dict() for v in self.violations],
        }

    def markers(self):
        return [{"x": v.x, "y": v.y, "label": f"{v.kind}: {v.message}"}
                for v in self.violations if v.x is not None]


# ---------------------------------------------------------------- cached results

CACHED_KIND = {"TClearanceViolation": "Clearance", "TShortCircuitViolation": "ShortCircuit",
               "TUnRoutedNetViolation": "UnRoutedNet", "TWidthViolation": "Width",
               "THoleSizeViolation": "HoleSize", "TAnnularRingViolation": "MinimumAnnularRing",
               "THoleToHoleClearanceViolation": "HoleToHoleClearance",
               "TBoardOutlineClearanceViolation": "BoardOutlineClearance",
               "TSilkToSolderMaskClearanceViolation": "SilkToSolderMaskClearance",
               "TComponentClearanceViolation": "ComponentClearance"}
PRIM_KIND = {"Arc": "arc", "Pad": "pad", "Track": "track", "Via": "via", "Fill": "fill",
             "Region": "region", "Text": "text", "Polygon": "polygon", "Component": "component",
             "Net": "net"}


def cached_violations(doc):
    """DRC results Altium saved in the file (T*Violation storages)."""
    from .pcbdoc import is_violation_storage, parse_length
    out = []
    rules = {r.index: r for r in doc.rules}
    for name, st in doc.storages.items():
        if not is_violation_storage(name):
            continue
        for p in st.records:
            if not hasattr(p, "get"):
                continue
            ri = p.get("RULEINDEX")
            rule = rules.get(int(ri)) if ri is not None and ri.isdigit() else None
            locs = []
            for k in (1, 2):
                x, y = p.get(f"LOCATION{k}.X"), p.get(f"LOCATION{k}.Y")
                if x is not None and y is not None:
                    locs.append((parse_length(x), parse_length(y)))
            prims = []
            for k in (1, 2):
                pid, pix = p.get(f"PRIM{k}ID"), p.get(f"PRIM{k}INDEX")
                if pid is not None and pix is not None and pix.lstrip("-").isdigit():
                    prims.append((PRIM_KIND.get(pid, pid.lower()), int(pix)))
            at = (sum(x for x, _ in locs) / len(locs), sum(y for _, y in locs) / len(locs)) \
                if locs else None
            out.append({"storage": name, "kind": CACHED_KIND.get(name, name[1:].replace("Violation", "")),
                        "rule": rule.name if rule else None, "objects": prims, "at": at,
                        "description": p.get("DESCRIPTION", "")})
    return out


def compare_cached(drc, cached, tol=0.5):
    """Match our violations to Altium's cached ones: by the two objects, else
    by rule kind and location within `tol` mm."""
    ours = drc.violations
    used = set()
    matched, missed = [], []
    for c in cached:
        hit = None
        for i, v in enumerate(ours):
            if i in used or v.kind != c["kind"]:
                continue
            objs = {(o.get("kind"), o.get("index")) for o in v.objects}
            if c["objects"] and all(p in objs for p in c["objects"]):
                hit = i
                break
        if hit is None and c["at"] is not None:
            for i, v in enumerate(ours):
                if i in used or v.kind != c["kind"] or v.x is None:
                    continue
                if math.hypot(v.x - c["at"][0], v.y - c["at"][1]) <= tol:
                    hit = i
                    break
        if hit is None:
            missed.append(c)
        else:
            used.add(hit)
            matched.append((c, ours[hit]))
    extra = [v for i, v in enumerate(ours) if i not in used and v.kind in {c["kind"] for c in cached}]
    return {"cached": len(cached), "matched": len(matched), "missed": missed,
            "ours_not_cached_same_kinds": len(extra)}


# ---------------------------------------------------------------- CLI

def cmd_drc(args):
    from .pcbdoc import PcbDoc
    doc = PcbDoc.open(args.board)
    cached = cached_violations(doc) if args.compare_cached else None
    drc = DRC(doc, copper=args.copper, box=args.box, checks=args.check, preset=args.preset,
              text=args.text)
    t0 = time.time()
    drc.run()
    rep = drc.report()
    rep["board"] = str(args.board)
    rep["seconds_total"] = round(time.time() - t0, 2)
    if cached is not None:
        cmp_ = compare_cached(drc, cached)
        rep["cached_comparison"] = dict(cmp_, missed=[{k: v for k, v in c.items()}
                                                      for c in cmp_["missed"]])
    if args.markers:
        with open(args.markers, "w", encoding="utf-8") as f:
            json.dump(drc.markers(), f, indent=1)
    if args.json:
        print(json.dumps(rep, indent=2))
        return 1 if rep["summary"]["violations"] else 0
    s = rep["summary"]
    print(f"{s['violations']} violation(s) in {rep['seconds_total']} s"
          + (f" ({s['approximate']} approximate)" if s["approximate"] else ""))
    for k, n in sorted(s["by_rule"].items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {k}")
    if rep["copper"]["repoured_polygons"]:
        print(f"pours: repoured polygons {rep['copper']['repoured_polygons']} (unpoured or stale); "
              "the rest as saved")
    for v in drc.violations[:args.limit]:
        at = f"({v.x:.3f}, {v.y:.3f})" if v.x is not None else ""
        print(f"  [{v.kind}] {v.layer or ''} {at} {v.message}" + (" ~" if v.approximate else ""))
    if len(drc.violations) > args.limit:
        print(f"  ... {len(drc.violations) - args.limit} more (--json for all)")
    for msg in rep["warnings"]:
        print(f"WARNING: {msg}")
    for msg in rep["skipped"]:
        print(f"skipped: {msg}")
    for u in rep["unevaluated_rules"]:
        print(f"rule not evaluated: {u['rule']} ({u['kind']}): {u['error']}")
    if cached is not None:
        c = rep["cached_comparison"]
        print(f"Altium's cached results: {c['cached']}, matched {c['matched']}, "
              f"missed {len(c['missed'])}")
    return 1 if s["violations"] else 0


def register_commands(command):
    def build(p):
        p.add_argument("board")
        p.add_argument("--json", action="store_true")
        p.add_argument("--box", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"),
                       help="only objects touching this box (mm)")
        p.add_argument("--check", nargs="+", choices=CHECKS, help="run only these checks")
        p.add_argument("--preset", choices=sorted(PRESETS),
                       help="also check a fab's published 2 oz limits")
        p.add_argument("--copper", choices=("auto", "saved", "repour"), default="auto",
                       help="pours: auto repours unpoured polygons; saved never; repour all")
        p.add_argument("--markers", help="write violation markers for render --markers")
        p.add_argument("--compare-cached", action="store_true",
                       help="compare with the DRC results Altium saved in the file")
        p.add_argument("--text", action="store_true",
                       help="also check text, measured as its box (approximate)")
        p.add_argument("--limit", type=int, default=50, help="violations to print")
    command("drc", cmd_drc, "design rule check from the board's own rules", build)
