"""Power-copper planner: shape a net's copper to carry a current.

    python -m offline copper plan.json board.PcbDoc [-o plan_out.json] [--png copper.png]

plan.json:
    {"net": "VBAT", "sources": ["J1.1"], "sinks": ["Q1.5", "Q1.6"], "current": 30,
     "layers": ["Top Layer", "Bottom Layer"],          # default: the outer layers
     "area": [[x, y], ...],                             # allowed outline (default: board)
     "keepouts": [[[x, y], ...], {"component": "U5", "margin": 1.0}, {"box": [x0, y0, x1, y1]}],
     "targets": {"drop_mv": 50, "density_a_per_mm2": 40, "rise_c": 30},
     "via": {"diameter": 0.6, "hole": 0.3, "in_pad": false},
     "margin_mm": 3, "grow_mm": 2, "max_iterations": 16, "res": 0.2,
     "respect_pours": true, "ambient_c": 25, "powers": {"Q1": 2.0}}

Copper may go wherever the net's copper is allowed: inside the area and
the board, outside keep-outs, and clear of every other net's object by the
clearance rule for a region of this net (the same voids pour.py uses). It
starts from that free room within a corridor around the terminals, joins
the layers with via arrays at both ends (sized by power.size_vias), then
loops against pdn.py: solve, find the worst neck, open the corridor there,
repeat until the targets are met or the free room stops growing. It then
names the obstacle that limits the neck. The result is an EditPlan
(add_region / add_via on the net) plus a report: drop, watts, current
density at the worst point, via currents and a thermal estimate.
"""

import json
import math
import time
from pathlib import Path

from . import geom
from . import layers as L
from .pcbdoc import KEEPOUT_COPPER


class PlanError(ValueError):
    pass


class _Index:
    def __init__(self, items):
        self.items = items


DEFAULT_TARGETS = {"drop_mv": None, "density_a_per_mm2": 40.0, "rise_c": None}


def neck(res, window=1.0):
    """The worst current density averaged over a `window` mm square: a neck,
    not the singular corner of one grid cell. {layer, x, y, j_a_per_mm2}."""
    import numpy as np
    k = max(1, int(round(window / res.grid.res)))
    if k % 2 == 0:
        k += 1
    best = None
    for lay, maps in res.maps.items():
        j = maps["j"]
        ok = np.isfinite(j)
        if not ok.any():
            continue
        v = np.where(ok, j, 0.0)
        c = ok.astype(float)
        # box sums by cumulative sums (window k x k, centred)
        def box(a):
            p = np.pad(a, k // 2 + 1)
            s_ = p.cumsum(0).cumsum(1)
            return s_[k:, k:] - s_[:-k, k:] - s_[k:, :-k] + s_[:-k, :-k]
        bs, bc = box(v), box(c)
        bs, bc = bs[:j.shape[0], :j.shape[1]], bc[:j.shape[0], :j.shape[1]]
        mean = np.where((bc >= max(1, k * k // 3)) & ok, bs / np.maximum(bc, 1), 0.0)
        i = int(np.argmax(mean))
        r, cc = np.unravel_index(i, mean.shape)
        if best is None or mean[r, cc] > best["j_a_per_mm2"]:
            x, y = res.grid.center(r, cc)
            best = {"layer": lay, "x": float(x), "y": float(y), "j_a_per_mm2": float(mean[r, cc])}
    return best or max(res.hotspots, key=lambda h: h["j_a_per_mm2"])


class Planner:
    def __init__(self, doc, plan):
        from .rules import RuleSet
        self.doc = doc
        self.plan = plan
        self.rules = RuleSet(doc)
        net = plan.get("net")
        if not net or not doc.has_net(net):
            raise PlanError(f"plan net {net!r} is not on the board")
        self.net = net
        self.net_index = doc.net_index(net)
        self.current = float(plan.get("current") or 0)
        if self.current <= 0:
            raise PlanError("plan needs a positive current")
        self.sources = list(plan.get("sources") or [])
        self.sinks = list(plan.get("sinks") or [])
        if not self.sources or not self.sinks:
            raise PlanError("plan needs sources and sinks (pads such as J1.1)")
        sig = [lay for lay in doc.copper_layers if L.is_signal(lay)]
        self.layers = [L.parse(x) for x in plan.get("layers") or [sig[0], sig[-1]]]
        for lay in self.layers:
            if lay not in sig:
                raise PlanError(f"{L.name(lay)} is not a signal layer of this board")
        t = dict(DEFAULT_TARGETS)
        t.update(plan.get("targets") or {})
        self.targets = t
        v = plan.get("via") or {}
        self.via_d = float(v.get("diameter", 0.6))
        self.via_h = float(v.get("hole", 0.3))
        self.via_in_pad = bool(v.get("in_pad", False))
        self.margin = float(plan.get("margin_mm", 3.0))
        self.grow = float(plan.get("grow_mm", 2.0))
        self.max_iter = int(plan.get("max_iterations", 16))
        self.res = float(plan.get("res", 0.2))
        self.respect_pours = bool(plan.get("respect_pours", True))
        self.ambient = float(plan.get("ambient_c", 25.0))
        self.powers = plan.get("powers") or {}
        self.min_piece = float(plan.get("min_piece_mm2", 0.5))
        self.window = float(plan.get("density_window_mm", 1.0))
        self.neck_clusters = int(plan.get("neck_via_clusters", 4))
        from .pdn import parse_terminal
        self.terms = [parse_terminal(doc, r) for r in self.sources + self.sinks]
        for t_ in self.terms:
            if t_["kind"] == "pads":
                for p in t_["pads"]:
                    if p.effective_net != self.net_index:
                        raise PlanError(f"{t_['ref']} is on net {p.net_name}, not {net}")
        self._free = {}
        self.log = []

    # ------------------------------------------------------------ room
    def term_shapes(self, layer=None):
        sh = geom.require()
        out = []
        for t in self.terms:
            for p in t.get("pads", []):
                for lay in ([layer] if layer is not None else self.layers):
                    if p.on_layer(lay):
                        g = geom.pad_shape(p, lay)
                        if not g.is_empty:
                            out.append(g)
            if t["kind"] == "point":
                out.append(sh.Point(t["x"], t["y"]).buffer(0.3))
        return out

    def free_room(self, layer, pours=None):
        """Where this net's copper may go on `layer`. pours=False ignores
        other nets' poured copper (it repours around whatever is added)."""
        pours = self.respect_pours if pours is None else pours
        if (layer, pours) in self._free:
            return self._free[(layer, pours)]
        sh = geom.require()
        from . import records as R
        doc = self.doc
        area = sh.Polygon(self.plan["area"]) if self.plan.get("area") else geom.board_shape(doc)
        if area is None or area.is_empty:
            raise PlanError("no area: the board has no outline and the plan gives none")
        board = geom.board_shape(doc)
        if board is not None:
            area = area.intersection(board)
        probe = R.region(doc, layer, [(0, 0), (1, 0), (1, 1)], (), self.net_index)[1]
        probe.storage = f"copper-plan:{layer}"
        if board is not None:
            gap, _ = self.rules.board_outline(probe)
            if gap:
                area = area.intersection(board.buffer(-gap))
        for k in self.plan.get("keepouts") or []:
            area = area.difference(self._keepout(k))
        idx = self._layer_index(layer)
        voids = []
        for it in idx.items:
            same = it.net == self.net_index and not it.keepout
            if same and not it.hole:
                continue
            if it.keepout and not it.obj.keepout_restrictions & KEEPOUT_COPPER:
                continue
            if it.hole:
                gap, _ = self.rules.clearance(probe, it.obj, classes=("Region", "Hole"))
            else:
                gap, _ = self.rules.clearance(probe, it.obj)
            gap = (gap or 0.0) + 0.002
            voids.append(it.shape.buffer(gap, quad_segs=8))
        for c in idx.cutouts:
            voids.append(c)
        if pours:
            for r in doc.regions:
                if r.polygon is not None and r.layer == layer and r.effective_net != self.net_index \
                        and r.kind == "copper":
                    gap, _ = self.rules.clearance(probe, r)
                    voids.append(geom.region_shape(r).buffer((gap or 0.0) + 0.002))
        free = area.difference(sh.union_all(voids)) if voids else area
        self._free[(layer, pours)] = free
        return free

    def _keepout(self, k):
        sh = geom.require()
        if isinstance(k, dict):
            if "box" in k:
                return sh.box(*k["box"]).buffer(float(k.get("margin", 0.0)))
            if "component" in k:
                comp = self.doc.component(k["component"])
                parts = [geom.pad_shape(p, p.layer if p.layer != L.MULTI else L.TOP)
                         for p in self.doc.pads if p.component == comp.index]
                parts += [geom.region_shape(b) for b in self.doc.bodies if b.component == comp.index]
                parts = [p for p in parts if not p.is_empty]
                if not parts:
                    return sh.Point(comp.x, comp.y).buffer(float(k.get("margin", 1.0)))
                return sh.union_all(parts).envelope.buffer(float(k.get("margin", 1.0)),
                                                           join_style="mitre")
            if "outline" in k:
                return sh.Polygon(k["outline"])
            raise PlanError(f"keep-out {k!r}: give box, component or outline")
        return sh.Polygon(k)

    # ------------------------------------------------------------ copper
    def corridor(self):
        sh = geom.require()
        shapes = self.term_shapes()
        if not shapes:
            raise PlanError("terminals have no copper on the planned layers")
        return sh.union_all(shapes).convex_hull.buffer(self.margin)

    def candidate(self, corridor):
        """The copper to plan inside the corridor: ({layer: geometry}, joins,
        gap). Free-room pieces become a graph (a piece touches a terminal, or
        overlaps a piece on another layer where a via fits); copper stays
        when it touches a terminal or lies on a path between the sources and
        the sinks, so nothing floats. joins are the overlaps those paths
        cross, which need vias. gap is None when the sources reach the sinks,
        else where the two sides come closest."""
        sh = geom.require()
        from .pour import pieces
        nodes = []
        whole = self._covers_board(corridor)
        for lay in self.layers:
            room = self.free_room(lay)
            free = room if whole else room.intersection(corridor)
            e = max(0.2, self._min_width(lay)) / 2
            # an opening drops slivers narrower than a track. Buffered arcs
            # are chords (sagitta under 0.02 e at quad_segs 4), so erode a
            # little further than the dilation and it stays inside the room
            free = free.buffer(-(e + 0.002 + 0.02 * e), quad_segs=4).buffer(e, quad_segs=4)
            nodes += [(lay, p) for p in pieces(free) if p.area >= self.min_piece]
        m = len(nodes)
        S, T = m, m + 1
        adj = {i: {} for i in range(m + 2)}
        for i, (lay, p) in enumerate(nodes):
            for src, tl, g in self._terminal_shapes():
                if tl == lay and p.intersects(g):
                    k = S if src else T
                    adj[i][k] = adj[k][i] = None
        r = self.via_d / 2 + 0.05
        eroded = {}
        if len(self.layers) > 1 and m:
            # a via fits where both pieces, shrunk by its radius, still meet
            # (shrinking distributes over intersection)
            idx = []
            for i, (lay, p) in enumerate(nodes):
                e = p.buffer(-r, quad_segs=4)
                if not e.is_empty:
                    sh.prepare(e)
                    eroded[i] = e
                    idx.append(i)
            if idx:
                tree = sh.STRtree([eroded[i] for i in idx])
                for i in idx:
                    for k in tree.query(eroded[i], predicate="intersects"):
                        j = idx[int(k)]
                        if j > i and nodes[j][0] != nodes[i][0]:
                            adj[i][j] = adj[j][i] = (i, j)

        def reach(start):
            seen, todo = {start}, [start]
            while todo:
                for k in adj[todo.pop()]:
                    if k not in seen:
                        seen.add(k)
                        todo.append(k)
            return seen
        rs, rt = reach(S), reach(T)
        if T not in rs:
            keep = sorted(i for i in (rs | rt) if i < m)
            gap = self._gap(nodes, rs, rt)
        else:
            # drop dead ends: pieces with one neighbour that touch no terminal
            live = {i for i in rs if i < m}
            deg = {i: sum(1 for k in adj[i] if k in live or k in (S, T)) for i in live}
            todo = [i for i in live if deg[i] <= 1 and S not in adj[i] and T not in adj[i]]
            while todo:
                i = todo.pop()
                if i not in live:
                    continue
                live.discard(i)
                for k in adj[i]:
                    if k in live:
                        deg[k] -= 1
                        if deg[k] <= 1 and S not in adj[k] and T not in adj[k]:
                            todo.append(k)
            keep = sorted(live)
            gap = None
        # vias go only on the joins of a breadth-first tree from the sources:
        # pieces a terminal touches need none, and each other piece one
        kept = set(keep)
        joins, seen, frontier = [], {S}, [S]
        while frontier:
            nxt = []
            for a in frontier:
                for b, o in adj[a].items():
                    if b in seen or (b < m and b not in kept):
                        continue
                    seen.add(b)
                    nxt.append(b)
                    if o is not None:
                        joins.append(eroded[o[0]].intersection(eroded[o[1]]))
            frontier = nxt
        out = {lay: [] for lay in self.layers}
        for i in keep:
            out[nodes[i][0]].append(nodes[i][1])
        copper = {lay: sh.union_all(ps) if ps else sh.Polygon() for lay, ps in out.items()}
        return copper, joins, gap

    def _layer_index(self, layer):
        from .pour import _LayerIndex
        cache = self.__dict__.setdefault("_lindex", {})
        if layer not in cache:
            cache[layer] = _LayerIndex(self.doc, layer)
        return cache[layer]

    def _terminal_shapes(self):
        """[(is_source, layer, shape)] for every terminal on the planned layers."""
        if not hasattr(self, "_term_cache"):
            sh = geom.require()
            out = []
            for k, t in enumerate(self.terms):
                src = k < len(self.sources)
                for lay in self.layers:
                    for p in t.get("pads", []):
                        if p.on_layer(lay):
                            g = geom.pad_shape(p, lay)
                            if not g.is_empty:
                                out.append((src, lay, g.buffer(0.01)))
                    if t["kind"] == "point" and (t.get("layer") in (None, lay)):
                        out.append((src, lay, sh.Point(t["x"], t["y"]).buffer(0.3)))
            self._term_cache = out
        return self._term_cache

    def _gap(self, nodes, rs, rt):
        """Where the copper the sources reach comes closest to the copper the
        sinks reach, per layer, and what lies between."""
        sh = geom.require()
        from shapely.ops import nearest_points
        best = None
        for lay in self.layers:
            a = [p for i, (l_, p) in enumerate(nodes) if l_ == lay and i in rs]
            b = [p for i, (l_, p) in enumerate(nodes) if l_ == lay and i in rt]
            a += [g for src, l_, g in self._terminal_shapes() if src and l_ == lay]
            b += [g for src, l_, g in self._terminal_shapes() if not src and l_ == lay]
            if not a or not b:
                continue
            ua, ub = sh.union_all(a), sh.union_all(b)
            pa, pb = nearest_points(ua, ub)
            d = pa.distance(pb)
            if best is None or d < best["distance_mm"]:
                best = {"layer": L.name(lay), "distance_mm": round(d, 3),
                        "from": [round(pa.x, 3), round(pa.y, 3)],
                        "to": [round(pb.x, 3), round(pb.y, 3)],
                        "_lay": lay, "_mid": ((pa.x + pb.x) / 2, (pa.y + pb.y) / 2)}
        if best is None:
            return {"layer": None, "distance_mm": None,
                    "note": "the sources and sinks share no planned layer"}
        lay, (x, y) = best.pop("_lay"), best.pop("_mid")
        best["blocked_by"] = self._limiter({"layer": lay, "x": x, "y": y})
        return best

    def _min_width(self, layer):
        from . import records as R
        probe = R.track(self.doc, layer, 0, 0, 1, 0, 0.5, self.net_index)
        w = self.rules.width(probe)
        return w["min"] if w and w.get("min") else 0.2

    def via_sites(self, copper, near, count, avoid=(), room=None):
        """Up to `count` via positions in the overlap of every planned layer's
        `copper` (or in `room`, a ready region for via centres), clear of
        other nets on the other signal layers and of pads, nearest to `near`
        first, a pitch away from the board's vias and from `avoid`."""
        import numpy as np
        sh = geom.require()
        if len(self.layers) < 2 or count <= 0:
            return []
        if room is None:
            room = self._via_room(copper)
        if room is None or room.is_empty:
            return []
        pitch = max(self.via_d + 0.3, 1.0)
        step = pitch / 2
        x0, y0, x1, y1 = room.bounds
        xs = np.arange(x0 + step / 2, x1 + 1e-9, step)
        ys = np.arange(y0 + step / 2, y1 + 1e-9, step)
        if not len(xs) or not len(ys):
            xs, ys = np.array([(x0 + x1) / 2]), np.array([(y0 + y1) / 2])
        gx, gy = np.meshgrid(xs, ys)
        gx, gy = gx.ravel(), gy.ravel()
        ok = sh.contains_xy(room, gx, gy)
        allowed, blocked = self._via_limits()
        if allowed is not None:
            ok &= sh.contains_xy(allowed, gx, gy)
        if blocked is not None:
            ok &= ~sh.contains_xy(blocked, gx, gy)
        gx, gy = gx[ok], gy[ok]
        if not len(gx):
            return []
        if near:
            d = sh.distance(sh.points(gx, gy), sh.union_all(list(near)))
            order = np.argsort(d, kind="stable")
            gx, gy = gx[order], gy[order]
        taken = {}

        def put(x, y):
            taken.setdefault((math.floor(x / pitch), math.floor(y / pitch)), []).append((x, y))

        def free(x, y):
            i, j = math.floor(x / pitch), math.floor(y / pitch)
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for q in taken.get((i + di, j + dj), ()):
                        if math.hypot(x - q[0], y - q[1]) < pitch - 1e-6:
                            return False
            return True
        for v in self.doc.vias:
            put(v.x, v.y)
        for q in avoid:
            put(*q)
        out = []
        for x, y in zip(gx.tolist(), gy.tolist()):
            if len(out) >= count:
                break
            if free(x, y):
                out.append((x, y))
                put(x, y)
        return out

    def _via_room(self, copper):
        """Where via centres may go inside every planned layer's copper; kept
        for the copper dict it was made from."""
        sh = geom.require()
        if getattr(self, "_room_src", None) is copper:
            return self._room
        r = self.via_d / 2
        overlap = None
        for lay in self.layers:
            room = copper[lay].buffer(-r - 0.05, quad_segs=4)
            overlap = room if overlap is None else overlap.intersection(room)
        if overlap is not None and not overlap.is_empty:
            sh.prepare(overlap)
        self._room_src, self._room = copper, overlap
        return overlap

    def _via_limits(self):
        """(allowed, blocked) for via centres, fixed for the run: inside the
        free room of every signal layer not planned (a via passes them all;
        pours there repour around it) and, unless in_pad, outside every pad
        (an open via in a pad wicks solder away from the joint)."""
        if not hasattr(self, "_limits"):
            sh = geom.require()
            r = self.via_d / 2
            allowed = None
            for lay in self.doc.copper_layers:
                if L.is_signal(lay) and lay not in self.layers:
                    room = self.free_room(lay, pours=False).buffer(-r - 0.05, quad_segs=4)
                    allowed = room if allowed is None else allowed.intersection(room)
            blocked = None if self.via_in_pad else self._pads().buffer(r + 0.05, quad_segs=4)
            for g in (allowed, blocked):
                if g is not None and not g.is_empty:
                    sh.prepare(g)
            self._limits = (allowed, blocked)
        return self._limits

    # ------------------------------------------------------------ loop
    def build(self, copper, vias):
        """The board in memory with the planned copper and vias added."""
        from .cfb import CompoundFile
        from .edits import Editor
        from .pcbdoc import PcbDoc
        from .pcbwrite import to_document_bytes
        from .pour import pieces
        if not hasattr(self, "_bytes"):
            self._bytes = to_document_bytes(self.doc)
        doc = PcbDoc(CompoundFile(self._bytes))
        ed = Editor(doc)
        with ed.batch():
            for lay, g in copper.items():
                for p in pieces(g):
                    ed.add_region(lay, list(p.exterior.coords)[:-1],
                                  [list(h.coords)[:-1] for h in p.interiors], net=self.net)
            for x, y in vias:
                ed.add_via(x, y, self.via_d, self.via_h, net=self.net)
        return doc

    def index(self, copper, vias):
        """The board's copper index plus the planned copper: what pdn.solve
        reads, without re-reading the whole board each iteration."""
        from .connect import CopperIndex
        from .pour import pieces
        sh = geom.require()
        if not hasattr(self, "_base_index"):
            self._base_index = CopperIndex(self.doc)
        idx = _Index({lay: list(items) for lay, items in self._base_index.items.items()})
        for lay, g in copper.items():
            idx.items.setdefault(lay, []).extend((None, self.net_index, p) for p in pieces(g))
        for x, y in vias:
            ring = sh.Point(x, y).buffer(self.via_d / 2, quad_segs=8)
            for lay in self.doc.copper_layers:
                idx.items.setdefault(lay, []).append((None, self.net_index, ring))
        return idx

    def run(self):
        from .pdn import PdnError, solve
        from .power import size_vias
        sh = geom.require()
        t0 = time.time()
        corridor = self.corridor()
        thick = [self.doc.copper_thickness(l) for l in self.layers]
        sized = size_vias(self.current, hole=self.via_h, layers=thick, length=self.doc.thickness)
        need = sized["vias"] if len(self.layers) > 1 else 0
        # a join between layers in series with the path carries all of it
        need_join = size_vias(self.current, hole=self.via_h,
                              length=self.doc.thickness)["vias"] if need else 0
        src_shapes = [g for t in self.terms[:len(self.sources)] for g in
                      [geom.pad_shape(p, p.layer if p.layer != L.MULTI else self.layers[0])
                       for p in t.get("pads", [])]]
        snk_shapes = [g for t in self.terms[len(self.sources):] for g in
                      [geom.pad_shape(p, p.layer if p.layer != L.MULTI else self.layers[0])
                       for p in t.get("pads", [])]]
        best = None
        limit = None
        last_score = None
        stalls = 0
        local_r = 0.0
        necks = []          # points where a neck stalled: stitch the layers there
        last_hot = None
        gap, no_path, last_area, settled = None, 0, -1.0, False
        for it in range(1, self.max_iter + 1):
            copper, joins, gap = self.candidate(corridor)
            area = sum(g.area for g in copper.values())
            if gap is not None:
                # the free room does not join the terminals yet: widen fast
                self.log.append({"iteration": it, "copper_mm2": round(area, 2), "no_path": gap})
                if area <= last_area + 0.5:
                    settled = True
                    break
                last_area = area
                no_path += 1
                corridor = corridor.buffer(self.grow * 2 ** min(no_path, 5)).simplify(0.05)
                continue
            vias = []
            if need:
                vias = self.via_sites(copper, src_shapes, (need + 1) // 2)
                vias += self.via_sites(copper, snk_shapes, (need + 1) // 2, avoid=vias)
                for o in joins:
                    vias += self.via_sites(None, [o.representative_point()], need_join,
                                           avoid=vias, room=o)
                for pt in necks:
                    vias += self.via_sites(copper, [pt], max(4, (need + 1) // 2), avoid=vias)
            doc = self.build(copper, vias)
            try:
                res = solve(doc, self.net, self.sources, self.sinks, self.current, res=self.res,
                            temp_c=self.ambient + 20.0, box=self._box(corridor),
                            index=self.index(copper, vias))
            except PdnError as e:
                self.log.append({"iteration": it, "error": str(e)})
                if "not connected" in str(e):
                    corridor = corridor.buffer(self.grow)
                    continue
                raise
            hot = neck(res, self.window)
            peak = max(res.hotspots, key=lambda h: h["j_a_per_mm2"])
            via_max = max((v["current_a"] for v in res.vias), default=0.0)
            step = {"iteration": it, "copper_mm2": round(area, 2), "vias": len(vias),
                    "drop_mv": round(res.drop_v * 1e3, 3), "power_w": round(res.power_w, 4),
                    "max_density_a_per_mm2": round(hot["j_a_per_mm2"], 2),
                    "at": {"layer": L.name(hot["layer"]), "x": round(hot["x"], 3),
                           "y": round(hot["y"], 3)},
                    "peak_cell_density_a_per_mm2": round(peak["j_a_per_mm2"], 2),
                    "max_via_current_a": round(float(via_max), 3)}
            self.log.append(step)
            best = (copper, vias, res, step, doc)
            if self._met(step):
                break
            # progress counts only on the targets still missed
            score = {k: step[k] for k in self._missed(step)}
            if last_score is not None and score and \
                    all(v > last_score.get(k, math.inf) * 0.98 for k, v in score.items()):
                stalls += 1
            else:
                stalls = 0
            last_score = score
            same_neck = last_hot is not None and \
                math.hypot(hot["x"] - last_hot["x"], hot["y"] - last_hot["y"]) < 1.5
            last_hot = hot
            if stalls >= (1 if same_neck else 2) and self._stitch(necks, hot, need):
                stalls, last_score = 0, None
                continue
            if stalls >= 2:
                limit = self._limiter(hot)
                break
            # widen where it is worst; when that finds no more room, or the
            # neck is fine and the drop is not, widen everywhere
            grown = None
            if self.targets.get("density_a_per_mm2") and \
                    hot["j_a_per_mm2"] > self.targets["density_a_per_mm2"]:
                local_r += self.grow
                c2 = corridor.union(sh.Point(hot["x"], hot["y"]).buffer(local_r))
                if self._adds_room(corridor, c2):
                    grown = c2
            if grown is None:
                c2 = corridor.buffer(self.grow).simplify(0.05)
                if self._adds_room(corridor, c2):
                    grown = c2
            if grown is None and self._stitch(necks, hot, need):
                stalls, last_score = 0, None
                continue
            if grown is None:
                limit = self._limiter(hot)
                break
            corridor = grown
        if best is None:
            if gap is not None:
                where = (f"; the closest approach is {gap['distance_mm']} mm on {gap['layer']} "
                         f"from {gap['from']} to {gap['to']}" if gap.get("distance_mm") is not None
                         else f"; {gap.get('note', '')}")
                b = gap.get("blocked_by")
                if not settled:
                    raise PlanError(
                        f"after {self.max_iter} iterations the corridor still had not joined "
                        f"{', '.join(self.sources)} to {', '.join(self.sinks)}{where}; raise "
                        "max_iterations or grow_mm")
                raise PlanError(
                    f"no free room joins {', '.join(self.sources)} to {', '.join(self.sinks)} on "
                    f"{', '.join(L.name(x) for x in self.layers)}{where}"
                    + (f", blocked by {b}" if b else "")
                    + ". Clear a path, plan on more layers"
                    + (", or set respect_pours false" if self.respect_pours else "") + ".")
            raise PlanError("no copper could be planned: " + "; ".join(
                sorted({s.get("error", "") for s in self.log if s.get("error")})))
        copper, vias, res, step, doc = best
        report = {"net": self.net, "current_a": self.current, "targets": self.targets,
                  "met": self._met(step), "final": step, "iterations": self.log,
                  "vias_needed": need, "seconds": round(time.time() - t0, 2)}
        if not report["met"]:
            report["limiting_obstacle"] = limit or self._limiter(neck(res, self.window))
        if need:
            cap = sized["per_via_capacity_a"]
            report["via_capacity_a"] = cap
            over = sum(1 for v in res.vias if v["current_a"] > cap)
            if over:
                report["via_warning"] = (f"{over} via(s) carry more than {cap} A, the "
                                         f"IPC-2221 figure for a {self.via_h} mm barrel at a "
                                         f"{sized['rise_c']} C rise; add vias or check the "
                                         "thermal estimate")
        if self.targets.get("rise_c") is not None or self.plan.get("thermal", True):
            from .thermal import solve as thermal_solve
            th = thermal_solve(doc, pdn=[res], powers=self.powers, ambient_c=self.ambient,
                               res=max(0.5, self.res))
            report["thermal"] = th.report()
            if self.targets.get("rise_c") is not None and th.max_rise > self.targets["rise_c"]:
                report["met"] = False
                report["thermal_problem"] = (f"estimated rise {th.max_rise:.1f} C exceeds "
                                             f"{self.targets['rise_c']} C")
        ops = []
        from .pour import pieces
        for lay, g in copper.items():
            for p in pieces(g):
                ops.append({"op": "add_region", "layer": L.name(lay),
                            "outline": [[round(x, 4), round(y, 4)] for x, y in
                                        list(p.exterior.coords)[:-1]],
                            "holes": [[[round(x, 4), round(y, 4)] for x, y in
                                       list(h.coords)[:-1]] for h in p.interiors],
                            "net": self.net})
        for x, y in vias:
            ops.append({"op": "add_via", "x": round(x, 4), "y": round(y, 4),
                        "diameter": self.via_d, "hole": self.via_h, "net": self.net})
        return {"version": 1, "ops": ops}, report, doc

    def _pads(self):
        if not hasattr(self, "_pad_union"):
            from shapely.ops import unary_union
            shapes = []
            for p in self.doc.pads:
                for lay in self.layers:
                    if p.on_layer(lay):
                        shapes.append(geom.pad_shape(p, lay))
            self._pad_union = unary_union([g for g in shapes if not g.is_empty])
        return self._pad_union

    def _stitch(self, necks, hot, need):
        """When a neck stops improving, let its current change layers around
        the obstacle: a via cluster at the neck (kept for later iterations)."""
        sh = geom.require()
        if not need or len(necks) >= self.neck_clusters:
            return False
        if not (self.targets.get("density_a_per_mm2") and
                hot["j_a_per_mm2"] > self.targets["density_a_per_mm2"]):
            return False
        p = sh.Point(hot["x"], hot["y"])
        if any(p.distance(q) < 2.0 for q in necks):
            return False
        necks.append(p)
        return True

    def _covers_board(self, corridor):
        sh = geom.require()
        x0, y0, x1, y1 = self._extent()
        return corridor.contains(sh.box(x0, y0, x1, y1))

    def _extent(self):
        if not hasattr(self, "_ext"):
            bounds = [self.free_room(lay).bounds for lay in self.layers
                      if not self.free_room(lay).is_empty]
            if not bounds:
                raise PlanError("no free room on the planned layers")
            self._ext = (min(b[0] for b in bounds), min(b[1] for b in bounds),
                         max(b[2] for b in bounds), max(b[3] for b in bounds))
        return self._ext

    def _adds_room(self, corridor, wider):
        """Whether widening the corridor reaches any more free room."""
        ring = wider.difference(corridor)
        return sum(self.free_room(lay).intersection(ring).area for lay in self.layers) > 0.5

    def _box(self, corridor):
        x0, y0, x1, y1 = corridor.buffer(2.0).bounds
        b = geom.board_shape(self.doc)
        if b is not None:
            bx0, by0, bx1, by1 = b.bounds
            x0, y0, x1, y1 = max(x0, bx0 - 1), max(y0, by0 - 1), min(x1, bx1 + 1), min(y1, by1 + 1)
        return (x0, y0, x1, y1)

    def _missed(self, step):
        t = self.targets
        out = []
        if t.get("drop_mv") is not None and step["drop_mv"] > t["drop_mv"]:
            out.append("drop_mv")
        if t.get("density_a_per_mm2") is not None and \
                step["max_density_a_per_mm2"] > t["density_a_per_mm2"]:
            out.append("max_density_a_per_mm2")
        return out

    def _met(self, step):
        return not self._missed(step)

    def _limiter(self, hot):
        """The other-net object nearest the worst neck."""
        sh = geom.require()
        lay = hot["layer"]
        p = sh.Point(hot["x"], hot["y"])
        idx = self._layer_index(lay)
        best = None
        for it in idx.near(p.buffer(5.0)):
            if it.net == self.net_index and not it.keepout:
                continue
            d = it.shape.distance(p)
            if best is None or d < best[0]:
                best = (d, it)
        if best is None:
            b = geom.board_shape(self.doc)
            if b is not None:
                return {"what": "board edge or allowed area", "distance_mm":
                        round(b.boundary.distance(p), 3)}
            return None
        d, it = best
        o = it.obj
        desc = {"what": it.kind + (" (keep-out)" if it.keepout else "") +
                (" hole" if it.hole else ""), "distance_mm": round(d, 3), "layer": L.name(lay)}
        if it.net is not None:
            desc["net"] = self.doc.nets[it.net].name
        if getattr(o, "component", None) is not None:
            desc["component"] = self.doc.components[o.component].designator
        if it.kind == "pad":
            desc["pad"] = o.name
        return desc


def run_plan(doc, plan):
    return Planner(doc, plan).run()


def cmd_copper(args):
    from .pcbdoc import PcbDoc
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    board = args.board or plan.get("board")
    if not board:
        raise PlanError("give the board after the plan, or 'board' in the plan")
    doc = PcbDoc.open(board)
    edit, report, built = Planner(doc, plan).run()
    out = {"plan": edit, "report": report}
    if args.out:
        Path(args.out).write_text(json.dumps(edit, indent=2), encoding="utf-8")
        report["editplan"] = str(args.out)
    if args.png:
        from .render import render
        render(built, args.png, nets=[plan["net"]], box=Planner(doc, plan)._box(
            Planner(doc, plan).corridor()))
        report["image"] = str(args.png)
    if args.json:
        print(json.dumps(out if not args.out else report, indent=2))
    else:
        f = report["final"]
        print(f"{report['net']} {report['current_a']} A: {'targets met' if report['met'] else 'targets NOT met'}"
              f" after {len(report['iterations'])} iteration(s), {report['seconds']} s")
        print(f"  drop {f['drop_mv']} mV, {f['power_w']} W, worst {f['max_density_a_per_mm2']} A/mm2 at "
              f"{f['at']['layer']} ({f['at']['x']}, {f['at']['y']}); {f['vias']} vias, "
              f"max {f['max_via_current_a']} A per via")
        if "limiting_obstacle" in report and report["limiting_obstacle"]:
            print(f"  limited by: {report['limiting_obstacle']}")
        if "thermal" in report:
            th = report["thermal"]
            hot = (th.get("hottest") or [{}])[0]
            print(f"  thermal estimate: +{th['max_rise_c']} C max over {th['ambient_c']} C "
                  f"ambient" + (f" at {hot['layer']} ({hot['x']}, {hot['y']})" if hot else ""))
        for k in ("thermal_problem", "via_warning"):
            if report.get(k):
                print(f"  {k.replace('_', ' ')}: {report[k]}")
        if args.out:
            print(f"  EditPlan with {len(edit['ops'])} op(s) -> {args.out}")
    return 0 if report["met"] else 1


def register_commands(command):
    def build(p):
        p.add_argument("plan", help="copper plan JSON")
        p.add_argument("board", nargs="?", help="the board (or 'board' in the plan)")
        p.add_argument("-o", "--out", help="write the EditPlan here")
        p.add_argument("--png", help="draw the planned copper")
        p.add_argument("--json", action="store_true")
    command("copper", cmd_copper, "plan power copper for a current, offline", build)
