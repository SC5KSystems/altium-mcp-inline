"""Copper connectivity: islands per net, unrouted gaps (ratsnest) and shorts.

Two copper objects on a layer are connected when their shapes touch. Vias and
through-hole pads join every layer they reach. Poured copper counts with its
polygon's net. One pass over each layer answers islands, gaps and shorts.
"""

import math
from collections import defaultdict

from . import geom
from . import layers as L


def obj_key(obj):
    return (obj.storage, obj.index)


class PlaneArea:
    """Copper of an internal plane layer: a split-plane polygon, or the whole
    board for a single-net plane. Planes are negative, so a plane never shorts
    to another net's via or pad: Altium clears around them."""

    KIND = "plane"
    designator = None
    name = ""

    def __init__(self, layer, polygon, net):
        self.storage = f"plane{layer}"
        self.index = -1 if polygon is None else polygon
        self.layer = layer
        self.polygon = polygon
        self.effective_net = net
        self.net = net


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, a):
        p = self.parent.setdefault(a, a)
        while p != self.parent[p]:
            self.parent[p] = self.parent[self.parent[p]]
            p = self.parent[p]
        self.parent[a] = p
        return p

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


class CopperIndex:
    """Every copper shape on every copper layer, built once per board state."""

    def __init__(self, doc, extra=0.0):
        self.doc = doc
        self.layers = doc.copper_layers
        self.items = {}       # layer -> [(obj, net, geometry)]
        self.trees = {}
        sh = geom.require()
        for layer in self.layers:
            items = []
            for obj in doc.copper(layer=layer):
                g = geom.copper_shape(obj, layer, extra)
                if g is not None and not g.is_empty:
                    items.append((obj, obj.effective_net, g))
            if L.is_plane(layer):
                for poly, net, pts in doc.plane_areas(layer):
                    if net is None or len(pts) < 3:
                        continue
                    g = sh.Polygon(pts)
                    g = g if g.is_valid else sh.make_valid(g)
                    items.append((PlaneArea(layer, poly, net), net, g))
            self.items[layer] = items
            self.trees[layer] = sh.STRtree([g for _, _, g in items]) if items else None

    def pairs(self, layer):
        """(i, j) index pairs of touching shapes on a layer, i < j."""
        tree = self.trees.get(layer)
        if tree is None:
            return []
        geoms = [g for _, _, g in self.items[layer]]
        a, b = tree.query(geoms, predicate="intersects")
        return [(int(i), int(j)) for i, j in zip(a, b) if i < j]


class Island:
    __slots__ = ("net", "members", "layers", "_geom")

    def __init__(self, net):
        self.net = net
        self.members = []
        self.layers = set()
        self._geom = None

    @property
    def pads(self):
        return [m for m in self.members if m.KIND == "pad"]

    @property
    def kinds(self):
        out = defaultdict(int)
        for m in self.members:
            out[m.KIND] += 1
        return dict(out)

    def geometry(self, index):
        """All of the island's copper, every layer flattened into one shape."""
        if self._geom is None:
            sh = geom.require()
            keys = {obj_key(m) for m in self.members}
            parts = [g for layer in self.layers for (o, _n, g) in index.items[layer]
                     if obj_key(o) in keys]
            self._geom = sh.union_all(parts) if parts else sh.Polygon()
        return self._geom

    def describe(self, doc):
        pads = self.pads
        return {
            "objects": self.kinds,
            "layers": sorted(L.name(x) for x in self.layers),
            "pads": [f"{p.designator}.{p.name}" if p.designator else f"free pad {p.name}"
                     for p in pads[:20]] + (["..."] if len(pads) > 20 else []),
        }


class Connectivity:
    def __init__(self, doc, index=None):
        self.doc = doc
        self.index = index or CopperIndex(doc)
        uf = UnionFind()
        objs = {}
        on_layers = defaultdict(set)
        self.shorts = []
        seen_short = set()
        for layer in self.index.layers:
            items = self.index.items[layer]
            for obj, net, _g in items:
                objs[obj_key(obj)] = obj
                on_layers[obj_key(obj)].add(layer)
                uf.find(obj_key(obj))
            for i, j in self.index.pairs(layer):
                oa, na, ga = items[i]
                ob, nb, gb = items[j]
                if na == nb:
                    uf.union(obj_key(oa), obj_key(ob))
                elif "plane" in (oa.KIND, ob.KIND):
                    continue
                elif na is not None and nb is not None:
                    k = (min(obj_key(oa), obj_key(ob)), max(obj_key(oa), obj_key(ob)))
                    if k in seen_short:
                        continue
                    seen_short.add(k)
                    inter = ga.intersection(gb)
                    if inter.is_empty or inter.area < 1e-9:
                        continue
                    c = inter.representative_point()
                    self.shorts.append({"layer": layer, "a": oa, "b": ob, "net_a": na, "net_b": nb,
                                        "x": c.x, "y": c.y, "area_mm2": inter.area})
        groups = defaultdict(list)
        for k, obj in objs.items():
            groups[uf.find(k)].append(obj)
        self.islands = defaultdict(list)
        for members in groups.values():
            net = members[0].effective_net
            isl = Island(net)
            isl.members = members
            for m in members:
                isl.layers |= on_layers[obj_key(m)]
            self.islands[net].append(isl)

    def net_name(self, net):
        return self.doc.nets[net].name if net is not None and net < len(self.doc.nets) else None

    def unrouted(self, nets=None):
        """Missing connections: for each net whose pads sit on more than one
        island, the shortest links (a spanning tree over islands) that would
        join them. Each link gives both end points and the gap in mm."""
        sh = geom.require()
        from shapely.ops import nearest_points
        out = []
        for net, isls in self.islands.items():
            if net is None or (nets is not None and net not in nets):
                continue
            with_pads = [i for i in isls if i.pads]
            if len(with_pads) < 2:
                continue
            gs = [i.geometry(self.index) for i in with_pads]
            n = len(with_pads)
            joined, edges = {0}, []
            best = {j: (gs[0].distance(gs[j]), 0) for j in range(1, n)}
            while len(joined) < n:
                j = min(best, key=lambda k: best[k][0])
                d, i = best.pop(j)
                joined.add(j)
                pa, pb = nearest_points(gs[i], gs[j])
                edges.append({"net": net, "net_name": self.net_name(net), "gap_mm": d,
                              "from": (pa.x, pa.y), "to": (pb.x, pb.y),
                              "from_island": with_pads[i], "to_island": with_pads[j]})
                for k in list(best):
                    dk = gs[j].distance(gs[k])
                    if dk < best[k][0]:
                        best[k] = (dk, j)
            out.extend(edges)
        return out

    def dead_copper(self):
        """Islands of a net that hold no pad at all (antennas, orphaned pours)."""
        out = []
        for net, isls in self.islands.items():
            if net is None:
                continue
            for i in isls:
                if not i.pads and any(m.KIND != "via" or len(i.members) > 1 for m in i.members):
                    out.append(i)
        return out

    def summary(self):
        un = self.unrouted()
        return {
            "nets": len([n for n in self.islands if n is not None]),
            "islands": sum(len(v) for k, v in self.islands.items() if k is not None),
            "unrouted_connections": len(un),
            "shorts": len(self.shorts),
            "dead_copper_islands": len(self.dead_copper()),
        }


def report(doc, nets=None, limit=200):
    """JSON-ready connectivity report."""
    c = Connectivity(doc)
    un = c.unrouted(nets)
    return {
        "summary": c.summary(),
        "unrouted": [{"net": e["net_name"], "gap_mm": round(e["gap_mm"], 4),
                      "from": [round(v, 4) for v in e["from"]],
                      "to": [round(v, 4) for v in e["to"]],
                      "from_island": e["from_island"].describe(doc),
                      "to_island": e["to_island"].describe(doc)} for e in un[:limit]],
        "shorts": [{"layer": L.name(s["layer"]), "nets": [c.net_name(s["net_a"]), c.net_name(s["net_b"])],
                    "objects": [f"{s['a'].KIND}#{s['a'].index}", f"{s['b'].KIND}#{s['b'].index}"],
                    "at": [round(s["x"], 4), round(s["y"], 4)],
                    "overlap_mm2": round(s["area_mm2"], 6)} for s in c.shorts[:limit]],
    }
