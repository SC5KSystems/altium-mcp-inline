"""Polygon pour engine: what Altium's repour would produce, computed offline.

    eng = PourEngine(doc)
    res = eng.pour(3)                 # polygon 3, the rest of the board as saved
    run = eng.repour_all("clean")     # every polygon, in POURINDEX order
    eng.store(run, "pour")            # write as the polygons' poured copper

Modelled, from each polygon's own settings and the board's rules: the outline
(arcs included); the board outline and cutouts at BoardOutlineClearance;
polygon cutouts; keep-outs; every other-net object on the layer (pads, vias,
tracks, arcs, fills, regions, copper text as its box, other pours, bare drill
holes) at the gap the clearance rule gives poured copper; same-net objects by
the pour-over style; pads and vias by PolygonConnect (direct, relief with
spokes, no connect); neck removal; islands under the area threshold; dead
copper; pour order.

Pour order: Altium repours one polygon at a time in POURINDEX order, and each
pour avoids whatever copper the others hold at that moment, stale or not.
mode "saved" pours one polygon against the board as saved (Repour Selected),
"sequential" replays Repour All from the saved copper, "clean" unpours every
polygon first (what Repour All gives once our writer has stripped the stale
copper). Where sequential and clean differ, the saved copper leaves a dent.

Not modelled: hatched pours are computed solid and flagged; text is its box;
inner-layer pad removal. Accuracy against fixture pours: dev/CLOUD_HANDOFF.md.
"""

import math
import time

from . import geom
from . import layers as L
from .pcbdoc import KEEPOUT_COPPER, MM_PER_UNIT, parse_bool, parse_float, parse_length
from .rules import RuleSet

QUAD = 8                     # buffer segments per quarter circle
VOID_MARGIN = 0.002          # mm added to every clearance gap: polygonal arcs never cut in
EPS_AREA = 1e-6              # mm^2: smaller slivers are geometry noise
AREA_UNIT = MM_PER_UNIT ** 2  # AREATHRESHOLD is stored in internal units squared
NECK_FACTOR = 0.5           # erosion radius as a fraction of the neck width
NECK_JOIN = "round"         # opening with round joins: mitred ones regrow cut necks
SPOKE_EXTEND = 0.5          # spoke length past the air gap, in conductor widths
SQUARE_PAD_CLEARANCE = True  # clearance around rectangular pads: square corners
DEAD_TRACKS_CONNECT = True  # an island touching only a same-net track/fill/region is alive
OTHER_POURS_CONNECT = False  # another polygon's same-net copper does not keep a piece alive (VCOCXO fixture)
SPOKE_COVER = 0.95         # share of the spoke's continuation that must be copper
SPOKE_CAP = "flat"          # "round": the spoke is a track ending at the air gap's edge
OTHER_KINDS = ("track", "arc", "via", "pad", "fill", "region", "text")


class PourError(ValueError):
    pass


class Settings:
    """A polygon's pour settings (Polygons6 properties), lengths in mm."""

    def __init__(self, poly):
        p = poly.props
        self.hatch = poly.hatch
        self.grid = parse_length(p.get("GRIDSIZE"), 0.508)
        self.track_width = parse_length(p.get("TRACKWIDTH"), 0.2032)
        style = p.get("POUROVERSTYLE")
        if style is None:
            style = "1" if parse_bool(p.get("POUROVER"), True) else "0"
        # 0: don't pour over same-net objects, 1: pour over all of them,
        # 2: pour over same-net polygons only
        self.pour_over = int(parse_float(style, 1))
        self.remove_dead = parse_bool(p.get("REMOVEDEAD"), True)
        self.remove_islands = parse_bool(p.get("REMOVEISLANDSBYAREA"), False)
        self.island_area = parse_float(p.get("AREATHRESHOLD"), 0.0) * AREA_UNIT
        self.remove_necks = parse_bool(p.get("REMOVENECKS"), False)
        self.neck_width = parse_length(p.get("NECKWIDTHTHRESHOLD"), 0.127)
        self.neck_from_rule = parse_bool(p.get("NECKWIDTHFROMRULE"), False)
        self.arc_resolution = parse_length(p.get("ARCRESOLUTION"), 0.0127)
        self.obey_cutouts = parse_bool(p.get("OBEYPOLYGONCUTOUT"), True)
        self.shelved = parse_bool(p.get("SHELVED"), False)
        self.pour_index = poly.pour_index

    def as_dict(self):
        return dict(vars(self))


def pieces(g):
    """The polygons of a geometry, whatever its type."""
    if g is None or g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    if hasattr(g, "geoms"):
        out = []
        for x in g.geoms:
            out.extend(pieces(x))
        return out
    return []


def _multi(parts):
    sh = geom.require()
    parts = [p for p in parts if p.area > EPS_AREA]
    return sh.MultiPolygon(parts) if parts else sh.MultiPolygon()


class _Item:
    __slots__ = ("obj", "kind", "net", "shape", "hole", "keepout")

    def __init__(self, obj, kind, net, shape, hole=False, keepout=False):
        self.obj, self.kind, self.net, self.shape = obj, kind, net, shape
        self.hole, self.keepout = hole, keepout


class _LayerIndex:
    """Every obstacle on one copper layer, with an STRtree over its shapes."""

    def __init__(self, doc, layer):
        sh = geom.require()
        items = []
        for obj in doc.copper(layer=layer):
            if obj.KIND == "region" and obj.polygon is not None:
                continue  # poured copper: handled per polygon
            g = geom.copper_shape(obj, layer)
            if not g.is_empty:
                items.append(_Item(obj, obj.KIND, obj.effective_net, g))
            if obj.KIND in ("pad", "via") and obj.hole > 0:
                h = geom.hole_shape(obj)
                if not h.is_empty:
                    items.append(_Item(obj, obj.KIND, obj.effective_net, h, hole=True))
        for t in doc.texts:
            if t.layer == layer and not t.is_keepout and not t.hidden:
                items.append(_Item(t, "text", None, geom.text_box(t)))
        for kind, lst in (("track", doc.tracks), ("arc", doc.arcs), ("fill", doc.fills),
                          ("region", doc.regions)):
            for r in lst:
                if kind == "region" and r.kind != "copper":
                    continue
                if r.layer == L.KEEPOUT or (r.is_keepout and r.layer == layer):
                    if not r.keepout_restrictions & KEEPOUT_COPPER:
                        continue  # a keep-out for vias or pads only lets pours in
                    g = geom.copper_shape(r, layer)
                    if not g.is_empty:
                        items.append(_Item(r, kind, None, g, keepout=True))
        self.items = items
        self.tree = sh.STRtree([it.shape for it in items]) if items else None
        self.cutouts = [geom.region_shape(r) for r in doc.regions
                        if r.kind == "polygon_cutout" and r.layer in (layer, L.MULTI, L.KEEPOUT)]

    def near(self, box):
        if self.tree is None:
            return []
        return [self.items[i] for i in self.tree.query(box)]


class PourResult:
    def __init__(self, poly, settings):
        self.polygon = poly.index
        self.layer = poly.layer
        self.net = poly.net
        self.settings = settings
        self.copper = None
        self.spokes = 0
        self.relief_pads = 0
        self.removed = {"islands": 0, "islands_area": 0.0, "dead": 0, "dead_area": 0.0,
                        "necks_area": 0.0}
        self.notes = []
        self.seconds = 0.0

    @property
    def area(self):
        return self.copper.area if self.copper is not None else 0.0

    @property
    def parts(self):
        return pieces(self.copper)

    def summary(self, doc=None):
        net = (doc.nets[self.net].name if doc is not None and self.net is not None
               else self.net)
        return {"polygon": self.polygon, "layer": L.name(self.layer), "net": net,
                "area_mm2": round(self.area, 4), "pieces": len(self.parts),
                "relief_pads": self.relief_pads, "spokes": self.spokes,
                "removed": {k: round(v, 4) if isinstance(v, float) else v
                            for k, v in self.removed.items()},
                "notes": self.notes, "seconds": round(self.seconds, 3)}


class PourEngine:
    """Pour polygons of one board. Holds per-layer obstacle indexes."""

    def __init__(self, doc, rules=None, quad=QUAD):
        self.doc = doc
        self.rules = rules or RuleSet(doc)
        self.quad = quad
        self._layers = {}
        self._probes = {}
        self._board = None
        self._saved = None

    # ------------------------------------------------------------ inputs
    def polygon(self, ident):
        doc = self.doc
        if hasattr(ident, "index") and hasattr(ident, "props"):
            return ident
        if isinstance(ident, int) or (isinstance(ident, str) and ident.isdigit()):
            i = int(ident)
            if not 0 <= i < len(doc.polygons):
                raise PourError(f"no polygon {i} (board has {len(doc.polygons)})")
            return doc.polygons[i]
        hits = [p for p in doc.polygons if p.name and p.name.upper() == str(ident).upper()]
        if len(hits) != 1:
            raise PourError(f"polygon {ident!r}: {len(hits)} polygons have that name")
        return hits[0]

    def pourable(self):
        """Polygons a repour touches: signal-layer pours, not split planes or shelved ones."""
        return [p for p in self.doc.polygons
                if "split" not in p.kind.lower() and L.is_signal(p.layer)
                and not Settings(p).shelved]

    def layer_index(self, layer):
        if layer not in self._layers:
            self._layers[layer] = _LayerIndex(self.doc, layer)
        return self._layers[layer]

    def board(self):
        if self._board is None:
            self._board = geom.board_shape(self.doc) or False
        return self._board or None

    def probe(self, poly):
        """A stand-in poured region of `poly`, for rule scope matching."""
        if poly.index not in self._probes:
            from . import records as R
            pts = poly.outline()
            if len(pts) < 3:
                raise PourError(f"polygon {poly.index} has no outline")
            r = R.region(self.doc, poly.layer, pts, (), None, polygon=poly.index)[1]
            r.storage = f"pour:{poly.index}"
            self._probes[poly.index] = r
        return self._probes[poly.index]

    def saved_copper(self):
        """{polygon index: geometry} of the poured copper stored in the file."""
        if self._saved is None:
            sh = geom.require()
            parts = {}
            for kind, lst in (("region", self.doc.regions), ("track", self.doc.tracks),
                              ("arc", self.doc.arcs)):
                for r in lst:
                    p = r.polygon
                    if p is None or p >= len(self.doc.polygons):
                        continue
                    if kind == "region" and r.kind != "copper":
                        continue
                    g = geom.copper_shape(r, r.layer)
                    if not g.is_empty:
                        parts.setdefault(p, []).append(g)
            self._saved = {p: sh.union_all(gs) for p, gs in parts.items()}
        return self._saved

    def _gap(self, probe, item):
        if item.hole:
            gap, _ = self.rules.clearance(probe, item.obj, classes=("Poly", "Hole"))
        else:
            gap, _ = self.rules.clearance(probe, item.obj)
        return gap or 0.0

    # ------------------------------------------------------------ pouring
    def pour(self, ident, others=None):
        """Pour one polygon. others: {polygon index: geometry} of the other
        polygons' copper at this moment (default: as saved in the file)."""
        sh = geom.require()
        t0 = time.time()
        poly = self.polygon(ident)
        s = Settings(poly)
        res = PourResult(poly, s)
        if "split" in poly.kind.lower() or not L.is_signal(poly.layer):
            raise PourError(f"polygon {poly.index} is a split plane; planes are not poured")
        if s.hatch != "solid":
            res.notes.append(f"hatch style {s.hatch} is computed as solid copper")
        others = self.saved_copper() if others is None else others
        probe = self.probe(poly)
        net = poly.net
        idx = self.layer_index(poly.layer)
        self._cur_layer = poly.layer

        area = geom.polygon_outline(poly)
        board = self.board()
        if board is not None:
            gap, rule = self.rules.board_outline(probe)
            area = area.intersection(board.buffer(-(gap or 0.0), quad_segs=self.quad)
                                     if gap else board)
        if s.obey_cutouts:
            for c in idx.cutouts:
                if c.intersects(area):
                    area = area.difference(c)
        if area.is_empty:
            res.copper = sh.MultiPolygon()
            res.notes.append("outline lies outside the board or under cutouts")
            res.seconds = time.time() - t0
            return res

        reach = self.rules.max_clearance() + 1.0
        box = sh.box(*area.buffer(reach).bounds)
        voids, anchors, reliefs = [], [], []
        for it in idx.near(box):
            same = net is not None and it.net == net and not it.keepout
            if same and it.kind in ("pad", "via"):
                if it.hole:
                    continue
                pc = self.rules.polygon_connect(it.obj, probe) or {"style": "direct"}
                style = pc["style"]
                if style == "direct":
                    anchors.append(it.shape)
                elif style.startswith("relief"):
                    reliefs.append((it, pc))
                    voids.append(self._relief_void(it, pc))
                else:  # no connect: keep the clearance gap
                    voids.append(self._clear_void(it, self._gap(probe, it)))
                continue
            if same and s.pour_over == 1:
                if DEAD_TRACKS_CONNECT:
                    anchors.append(it.shape)
                continue
            gap = self._gap(probe, it)
            voids.append(self._clear_void(it, gap))
        for p_index, g in others.items():
            if p_index == poly.index or g is None or g.is_empty:
                continue
            other = self.doc.polygons[p_index]
            if other.layer != poly.layer or not g.intersects(box):
                continue
            if net is not None and other.net == net:
                if s.pour_over in (1, 2):
                    if OTHER_POURS_CONNECT:
                        anchors.append(g)
                    continue
            oprobe = self.probe(other)
            gap, _ = self.rules.clearance(probe, oprobe)
            voids.append(g.buffer((gap or 0.0) + VOID_MARGIN, quad_segs=self.quad))

        blocked = sh.union_all(voids) if voids else sh.Polygon()
        copper = area.difference(blocked)

        if s.remove_necks and s.neck_width > 0:
            before = copper.area
            r = self._neck_width(probe, s) * NECK_FACTOR
            copper = copper.buffer(-r, quad_segs=self.quad, join_style=NECK_JOIN, mitre_limit=2.0)
            copper = copper.buffer(r, quad_segs=self.quad, join_style=NECK_JOIN, mitre_limit=2.0)
            copper = copper.intersection(area.difference(blocked))
            res.removed["necks_area"] = max(0.0, before - copper.area)

        res.debug = {"area": area, "blocked": blocked, "copper": copper, "reliefs": reliefs}
        spokes = []
        if reliefs:
            for it, pc in reliefs:
                got = self._spokes(it, pc, copper, area, blocked)
                if got:
                    res.relief_pads += 1
                    res.spokes += len(got)
                    spokes.extend(got)
        if spokes:
            copper = sh.union_all([copper] + spokes)

        keep = []
        anchor_tree = sh.STRtree(anchors + spokes) if (anchors or spokes) else None
        anchor_geoms = anchors + spokes
        for part in pieces(copper):
            a = part.area
            if a <= EPS_AREA:
                continue
            if s.remove_islands and a < s.island_area:
                res.removed["islands"] += 1
                res.removed["islands_area"] += a
                continue
            if s.remove_dead and net is not None:
                live = False
                if anchor_tree is not None:
                    for i in anchor_tree.query(part):
                        if anchor_geoms[i].intersects(part):
                            live = True
                            break
                if not live:
                    res.removed["dead"] += 1
                    res.removed["dead_area"] += a
                    continue
            keep.append(part)
        res.copper = _multi(keep)
        res.seconds = time.time() - t0
        return res

    def _neck_width(self, probe, s):
        if s.neck_from_rule:
            w = self.rules.width(probe)
            if w and w.get("min"):
                return w["min"]
        return s.neck_width

    def _clear_void(self, it, gap):
        """An obstacle grown by its clearance gap. Rectangular pads keep
        square corners when SQUARE_PAD_CLEARANCE is set."""
        if gap <= 0:
            return it.shape
        gap += VOID_MARGIN
        if SQUARE_PAD_CLEARANCE and it.kind == "pad" and not it.hole and \
                it.obj.shape_on(self._cur_layer)[0] in ("rect", "octagonal"):
            return it.shape.buffer(gap, join_style="mitre")
        return it.shape.buffer(gap, quad_segs=self.quad)

    def _relief_void(self, it, pc):
        """The air gap around a relief-connected pad or via. Measured on the
        fixtures: square corners around rectangular pads."""
        square = it.kind == "pad" and it.obj.shape_on(self._layer_of(it))[0] in ("rect", "octagonal")
        if square:
            return it.shape.buffer(pc["air_gap"], join_style="mitre")
        return it.shape.buffer(pc["air_gap"], quad_segs=self.quad)

    def _layer_of(self, it):
        return getattr(self, "_cur_layer", L.TOP)

    def spoke_bars(self, it, pc, extend=None):
        """[(direction degrees, bar)] for a relief connection: each bar runs
        from the pad centre to `extend` mm past the air gap."""
        sh = geom.require()
        o = it.obj
        x, y = o.x, o.y
        rot = o.rotation if it.kind == "pad" else 0.0
        void = self._relief_void(it, pc)
        w = pc["conductor_width"]
        ext = SPOKE_EXTEND * w if extend is None else extend
        start = 45.0 if pc["angle"] == 45 else 0.0
        n = 2 if pc["entries"] == 2 else 4
        step = 180.0 if n == 2 else 90.0
        b = void.bounds
        far = math.hypot(b[2] - b[0], b[3] - b[1])
        out = []
        for k in range(n):
            deg = (rot + start + k * step) % 360.0
            a = math.radians(deg)
            ray = sh.LineString([(x, y), (x + far * math.cos(a), y + far * math.sin(a))])
            # how far the air gap reaches along this direction
            cut = ray.intersection(void)
            reach = cut.length if not cut.is_empty else 0.0
            if SPOKE_CAP == "round" and extend is None:
                line = sh.LineString([(x, y), (x + reach * math.cos(a), y + reach * math.sin(a))])
                out.append((deg, line.buffer(w / 2.0, quad_segs=self.quad)))
                continue
            end = reach + ext
            line = sh.LineString([(x, y), (x + end * math.cos(a), y + end * math.sin(a))])
            out.append((deg, line.buffer(w / 2.0, cap_style="flat")))
        return out

    def _spokes(self, it, pc, copper, area, blocked):
        """Spokes joining a relief-connected pad or via to the pour.

        Measured on the fixture pours (1,200 spoke positions): Altium keeps a
        spoke when pour copper covers its continuation for half a conductor
        width past the air gap, and drops it otherwise (a cut-back spoke is
        rare). So: run the bar SPOKE_EXTEND conductor widths past the gap and
        keep it when copper covers at least SPOKE_COVER of that stretch.
        """
        sh = geom.require()
        void = self._relief_void(it, pc)
        bars = self.spoke_bars(it, pc)
        if not bars:
            return []
        box = sh.union_all([b for _d, b in bars]).bounds
        copper = sh.clip_by_rect(copper, *box)
        area = sh.clip_by_rect(area, *box)
        out = []
        for _deg, bar in bars:
            beyond = bar.difference(void)
            if beyond.area <= EPS_AREA:
                continue
            if beyond.intersection(copper).area < SPOKE_COVER * beyond.area:
                continue
            part = bar.intersection(void).intersection(area)
            if part.is_empty or part.difference(it.shape).area <= EPS_AREA:
                continue
            out.append(part)
        return out

    # ------------------------------------------------------------ order
    def order(self, polys=None):
        polys = self.pourable() if polys is None else [self.polygon(p) for p in polys]
        return sorted(polys, key=lambda p: (p.pour_index, p.index))

    def repour_all(self, mode="clean", polys=None):
        """{polygon index: PourResult}, poured in POURINDEX order.

        mode: "clean" unpours every selected polygon first; "sequential"
        starts from the saved copper, as Altium's Repour All does; "saved"
        pours each polygon against the board as saved.
        """
        if mode not in ("clean", "sequential", "saved"):
            raise PourError(f"unknown mode {mode!r}")
        todo = self.order(polys)
        state = dict(self.saved_copper())
        if mode == "clean":
            for p in todo:
                state.pop(p.index, None)
        out = {}
        for p in todo:
            res = self.pour(p, others=self.saved_copper() if mode == "saved" else state)
            out[p.index] = res
            if mode != "saved":
                state[p.index] = res.copper
        return out

    def dents(self, polys=None, min_area=0.01):
        """Where Repour All from the saved copper differs from a clean pour:
        [{polygon, missing_mm2, extra_mm2, at}] with the stale copper to blame."""
        clean = self.repour_all("clean", polys)
        seq = self.repour_all("sequential", polys)
        out = []
        for i, c in clean.items():
            a, b = c.copper, seq[i].copper
            miss = a.difference(b)
            extra = b.difference(a)
            if miss.area < min_area and extra.area < min_area:
                continue
            big = max(pieces(miss) or [None], key=lambda g: g.area if g is not None else 0)
            at = big.representative_point().coords[0] if big is not None else None
            out.append({"polygon": i, "missing_mm2": round(miss.area, 4),
                        "extra_mm2": round(extra.area, 4),
                        "at": [round(v, 3) for v in at] if at else None})
        return out, clean, seq

    def unpoured(self):
        """Pourable polygons with no poured copper in the board as it stands."""
        saved = self.saved_copper()
        return [p.index for p in self.pourable()
                if saved.get(p.index) is None or saved[p.index].is_empty]

    def effective_copper(self, policy="auto", stale=()):
        """({polygon: geometry}, {polygon: "saved" | "repoured"}) for checks.

        policy "auto" repours the polygons that have no copper or are listed
        in `stale` (e.g. Editor.stale_pours) and keeps the saved copper of
        the rest; "saved" never repours; "repour" repours every polygon.
        """
        if policy not in ("auto", "saved", "repour"):
            raise PourError(f"unknown copper policy {policy!r}")
        saved = self.saved_copper()
        todo = set()
        if policy == "repour":
            todo = {p.index for p in self.pourable()}
        elif policy == "auto":
            todo = set(self.unpoured()) | {int(i) for i in stale}
        copper, used = {}, {}
        for i, g in saved.items():
            if i not in todo:
                copper[i], used[i] = g, "saved"
        if todo:
            state = {i: g for i, g in saved.items() if i not in todo}
            for p in self.order(sorted(todo)):
                res = self.pour(p, others=state)
                state[p.index] = copper[p.index] = res.copper
                used[p.index] = "repoured"
        return copper, used

    def refresh(self, policy="auto", stale=()):
        """Make the board in memory hold current pours for checks: repour
        what effective_copper() would repour and store it as the polygons'
        copper. Returns {polygon: "saved" | "repoured"}."""
        if policy not in ("auto", "saved", "repour"):
            raise PourError(f"unknown copper policy {policy!r}")
        saved = self.saved_copper()
        todo = set()
        if policy == "repour":
            todo = {p.index for p in self.pourable()}
        elif policy == "auto":
            todo = set(self.unpoured()) | {int(i) for i in stale}
        used = {i: "saved" for i in saved if i not in todo}
        if not todo:
            return used
        state = {i: g for i, g in saved.items() if i not in todo}
        results = {}
        for p in self.order(sorted(todo)):
            res = self.pour(p, others=state)
            state[p.index] = res.copper
            results[p.index] = res
            used[p.index] = "repoured"
        self.store(results, "pour")
        return used

    # ------------------------------------------------------------ checking
    def same_net_copper(self, poly):
        """Union of the polygon's own-net objects on its layer (pads, vias,
        tracks, arcs, fills, free regions)."""
        sh = geom.require()
        poly = self.polygon(poly)
        if poly.net is None:
            return sh.Polygon()
        idx = self.layer_index(poly.layer)
        return sh.union_all([it.shape for it in idx.items
                             if it.net == poly.net and not it.hole and not it.keepout])

    def compare(self, res, saved=None):
        """Our pour against the copper saved for that polygon.

        Copper of the polygon's own net is copper whichever object holds it,
        so both sides are compared with those objects' area taken out
        (Altium's regions skip over same-net pads and tracks, ours may not).
        """
        g = (saved if saved is not None else self.saved_copper()).get(res.polygon)
        a = res.copper
        own = self.same_net_copper(res.polygon)
        if not own.is_empty:
            a = a.difference(own)
            g = g.difference(own) if g is not None else None
        if g is None or g.is_empty:
            return {"polygon": res.polygon, "saved_mm2": 0.0, "ours_mm2": round(a.area, 4),
                    "overlap_mm2": 0.0, "iou": None, "missing_mm2": 0.0,
                    "extra_mm2": round(a.area, 4)}
        inter = a.intersection(g).area
        union = a.union(g).area
        return {"polygon": res.polygon, "saved_mm2": round(g.area, 4),
                "ours_mm2": round(a.area, 4), "overlap_mm2": round(inter, 4),
                "iou": round(inter / union, 5) if union > 0 else None,
                "missing_mm2": round(g.area - inter, 4), "extra_mm2": round(a.area - inter, 4)}

    # ------------------------------------------------------------ storing
    def store(self, results, as_="pour", editor=None):
        """Write pour results into the board through an Editor.

        as_="pour": replace each polygon's poured copper with ours (Altium
        shows it and may repour it). as_="solid": replace each polygon with
        plain regions on its net and delete the polygon, so nothing repours.
        as_="unpoured": strip the polygons' copper, so Altium's Repour All
        starts from nothing (the fix for dents left by stale copper).
        Returns the Editor.
        """
        from .edits import Editor
        ed = editor or Editor(self.doc)
        if as_ not in ("pour", "solid", "unpoured"):
            raise PourError("store as 'pour', 'solid' or 'unpoured'")
        done = sorted(results.values(), key=lambda r: r.polygon, reverse=True)
        for res in done:
            if as_ == "unpoured":
                ed.unpour(res.polygon)
                continue
            if res.settings.hatch != "solid":
                ed.unpour(res.polygon)
                ed._note(f"polygon {res.polygon}: hatched, left unpoured for Altium to repour")
                continue
            ed.unpour(res.polygon)
            for part in res.parts:
                outline = list(part.exterior.coords)[:-1]
                holes = [list(h.coords)[:-1] for h in part.interiors]
                if as_ == "pour":
                    ed.add_poured_region(res.polygon, outline, holes)
                else:
                    ed.add_region(res.layer, outline, holes,
                                  net=self.doc.nets[res.net].name if res.net is not None else None)
            if as_ == "solid":
                ed.remove_polygon(res.polygon)
            else:
                ed.stale_pours.discard(res.polygon)
        self._saved = None
        self._layers = {}
        return ed


# ---------------------------------------------------------------- CLI

def _select(eng, names):
    if not names:
        return None
    return [eng.polygon(n) for n in names]


def cmd_pour(args):
    import json
    from .pcbdoc import PcbDoc
    doc = PcbDoc.open(args.board)
    eng = PourEngine(doc)
    polys = _select(eng, args.polygon)
    t0 = time.time()
    if args.dents:
        found, clean, seq = eng.dents(polys)
        results = clean
    else:
        results = eng.repour_all(args.mode, polys)
        found = None
    rows = []
    for i, res in results.items():
        row = res.summary(doc)
        if args.compare:
            row["vs_saved"] = eng.compare(res)
        rows.append(row)
    report = {"board": str(args.board), "mode": "clean" if args.dents else args.mode,
              "seconds": round(time.time() - t0, 2), "polygons": rows}
    if found is not None:
        report["dents"] = found
    if args.out or args.png:
        ed = eng.store(results, args.store)
        if args.out:
            ed.save(args.out)
            report["written"] = str(args.out)
            report["stored_as"] = args.store
        if args.png:
            from .render import render
            layers = sorted({L.name(results[i].layer) for i in results}) or None
            render(doc, args.png, layers=layers)
            report["image"] = str(args.png)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"{len(rows)} polygon(s) poured in {report['seconds']} s ({report['mode']})")
    for r in rows:
        line = (f"  {r['polygon']:3d} {r['layer']:14s} {str(r['net']):16s} {r['area_mm2']:10.3f} mm2 "
                f"{r['pieces']:3d} piece(s) {r['spokes']:4d} spokes")
        if "vs_saved" in r:
            c = r["vs_saved"]
            line += (f" | without own-net objects: ours {c['ours_mm2']:10.3f} saved "
                     f"{c['saved_mm2']:10.3f} iou {c['iou']} "
                     f"missing {c['missing_mm2']:.3f} extra {c['extra_mm2']:.3f}")
        print(line)
        for n in r["notes"]:
            print(f"        note: {n}")
    if found:
        print("dents (Repour All from the saved copper differs from a clean pour):")
        for d in found:
            print(f"  polygon {d['polygon']}: missing {d['missing_mm2']} mm2, extra "
                  f"{d['extra_mm2']} mm2, e.g. at {d['at']}")
        print("  fix: write the board with them unpoured (-o out.PcbDoc --store unpoured), "
              "then Repour All in Altium")
    if args.out:
        print(f"wrote {args.out} ({args.store})")
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("board")
        p.add_argument("--polygon", action="append",
                       help="polygon index or name (repeatable; default every pour)")
        p.add_argument("--mode", choices=("clean", "sequential", "saved"), default="clean",
                       help="clean: unpour all first; sequential: Altium's Repour All from the "
                            "saved copper; saved: each polygon against the board as saved")
        p.add_argument("--dents", action="store_true",
                       help="report where stale copper makes Repour All differ from a clean pour")
        p.add_argument("--compare", action="store_true",
                       help="compare with the poured copper saved in the file")
        p.add_argument("-o", "--out", help="write the board with the new pours (never the input)")
        p.add_argument("--store", choices=("pour", "solid", "unpoured"), default="pour",
                       help="pour: as the polygons' copper; solid: plain regions, polygon "
                            "removed; unpoured: strip the copper for Altium to repour")
        p.add_argument("--png", help="draw the poured layers to this PNG/SVG")
        p.add_argument("--json", action="store_true")
    command("pour", cmd_pour, "compute polygon pours offline", build)
