"""Edit a PcbDoc in memory while keeping every cross-reference consistent.

    ed = Editor(PcbDoc.open("in.PcbDoc"))
    ed.remove(ed.select(kinds=["track", "via"], net="SENSE"))
    ed.merge_nets(["SENSE"], into="VBAT")
    ed.save("out.PcbDoc")

Removing anything renumbers whatever cites it by index (PCBDOC_FORMAT.md
lists each reference): owner fields in primitives, Polygons6 NET, both region
and body twins, PrimitiveGuids, the pad/via library links, pad unique ids,
extended primitive info, custom mask shapes, shared unions and dimension
references. Cached DRC violations cite objects by index too; they are dropped
whenever indices shift (Altium rebuilds them on its next DRC). An edit that
would leave a dangling reference raises EditError and changes nothing.

Coordinates are absolute mm. Bottom-side parts follow the convention measured
on the fixtures: world = centre + Rot(r) . (lx, -ly).
"""

import copy
import math
import re
import struct
import uuid

from . import geom
from . import layers as L
from . import pcbwrite
from . import records as R
from .pcbdoc import (BOARD16, NONE16, FormatError, Props, fmt_float, fmt_mil, mm2u, parse_float,
                     parse_length, props_vertices, set_props_vertices, u2mm, vertices_to_points)
from .pcbwrite import GUID_KINDS, OBJECT_STORAGE, PADVIA_KINDS

TWINS = {"Regions6": "ShapeBasedRegions6", "ShapeBasedRegions6": "Regions6",
         "ComponentBodies6": "ShapeBasedComponentBodies6",
         "ShapeBasedComponentBodies6": "ComponentBodies6"}
PRIMITIVE_STORAGES = ("Tracks6", "Arcs6", "Vias6", "Pads6", "Fills6", "Regions6",
                      "ShapeBasedRegions6", "Texts6", "ComponentBodies6",
                      "ShapeBasedComponentBodies6")
KIND_STORAGE = {"track": "Tracks6", "arc": "Arcs6", "via": "Vias6", "pad": "Pads6",
                "fill": "Fills6", "region": "Regions6", "text": "Texts6", "body": "ComponentBodies6"}
SIDE_SWAP = {L.TOP: L.BOTTOM, L.BOTTOM: L.TOP, L.TOP_OVERLAY: L.BOTTOM_OVERLAY,
             L.BOTTOM_OVERLAY: L.TOP_OVERLAY, L.TOP_PASTE: L.BOTTOM_PASTE,
             L.BOTTOM_PASTE: L.TOP_PASTE, L.TOP_SOLDER: L.BOTTOM_SOLDER,
             L.BOTTOM_SOLDER: L.TOP_SOLDER}


class EditError(ValueError):
    pass


def _shift_map(n, removed):
    out, k = [], 0
    for i in range(n):
        if i in removed:
            out.append(None)
        else:
            out.append(k)
            k += 1
    return out


def rot(x, y, a):
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return x * c - y * s, x * s + y * c


class Placement:
    """Where a component sits: centre, rotation and side."""

    def __init__(self, x, y, rotation, bottom):
        self.x, self.y, self.r, self.bottom = x, y, rotation % 360.0, bottom

    def to_local(self, x, y):
        lx, ly = rot(x - self.x, y - self.y, -self.r)
        return (lx, -ly) if self.bottom else (lx, ly)

    def to_world(self, lx, ly):
        if self.bottom:
            ly = -ly
        dx, dy = rot(lx, ly, self.r)
        return self.x + dx, self.y + dy

    def angle_to_local(self, a):
        return (self.r - a) if self.bottom else (a - self.r)

    def angle_to_world(self, a):
        return (self.r - a) if self.bottom else (a + self.r)

    def arc_to_local(self, a1, a2):
        return (self.r - a2, self.r - a1) if self.bottom else (a1 - self.r, a2 - self.r)

    def arc_to_world(self, a1, a2):
        return (self.r - a2, self.r - a1) if self.bottom else (a1 + self.r, a2 + self.r)


class Editor:
    def __init__(self, doc):
        self.doc = doc
        self.log = []
        self.stale_pours = set()
        self.dropped_violations = 0
        self.warnings = []
        self.plan_libraries = []

    # ------------------------------------------------------------ helpers
    def _note(self, msg):
        self.log.append(msg)

    def _st(self, name):
        return self.doc.storages.get(name)

    def net_index(self, net):
        if net is None or isinstance(net, int):
            return net
        if not self.doc.has_net(net):
            raise EditError(f"no net named {net!r}")
        return self.doc.net_index(net)

    def polygon_index(self, poly):
        if isinstance(poly, int):
            if not 0 <= poly < len(self.doc.polygons):
                raise EditError(f"no polygon {poly}")
            return poly
        hits = [p.index for p in self.doc.polygons if p.name.upper() == str(poly).upper()]
        if len(hits) != 1:
            raise EditError(f"polygon {poly!r} matches {len(hits)} polygons; use its index")
        return hits[0]

    # ------------------------------------------------------------ selection
    def select(self, kinds=("track", "arc", "via", "pad", "fill", "region", "text"), net=None,
               layer=None, box=None, polygon=None, component=None, free=False, pours=False):
        """Primitives matching every given criterion.

        box (x0, y0, x1, y1): the object's reference points all lie inside
        (track ends, arc centre, via/pad/text location, fill corners, region
        vertices). polygon: the pour primitives of that polygon. free: not in
        a component. pours=False leaves poured copper out unless polygon is set.
        """
        doc = self.doc
        net_i = self.net_index(net) if net is not None else None
        lay = L.parse(layer) if layer is not None else None
        poly_i = self.polygon_index(polygon) if polygon is not None else None
        comp_i = doc.component(component).index if component is not None else None
        out = []
        for k in kinds:
            st = self._st(KIND_STORAGE[k])
            for r in (st.records if st else []):
                if net_i is not None and r.effective_net != net_i:
                    continue
                if lay is not None and r.layer != lay:
                    continue
                if poly_i is not None and r.polygon != poly_i:
                    continue
                if poly_i is None and not pours and k in ("track", "arc", "region") and \
                        r.polygon is not None:
                    continue
                if comp_i is not None and r.component != comp_i:
                    continue
                if free and r.component is not None:
                    continue
                if box is not None and not _inside(r, box):
                    continue
                out.append(r)
        return out

    # ------------------------------------------------------------ removal core
    def remove(self, recs):
        """Remove primitives (and their twins); returns how many were removed."""
        by = {}
        for r in recs:
            by.setdefault(r.storage, set()).add(r.index)
        n = sum(len(v) for v in by.values())
        self._remove(by)
        self._note(f"removed {n} primitives")
        return n

    def _remove(self, removals):
        doc = self.doc
        removals = {k: set(v) for k, v in removals.items() if v}
        if not removals:
            return
        for a, b in list(TWINS.items()):
            if a in removals and b in doc.storages:
                removals.setdefault(b, set()).update(removals[a])
        for name in removals:
            if name not in doc.storages:
                raise EditError(f"nothing to remove from {name}")
        maps = {name: _shift_map(len(doc.storages[name].records), idx)
                for name, idx in removals.items()}

        # every owner reference that survives must still point at something
        owner = {"Nets6": 3, "Polygons6": 5, "Components6": 7}
        for oname, off in owner.items():
            if oname not in maps:
                continue
            m = maps[oname]
            for sname in PRIMITIVE_STORAGES:
                st = doc.storages.get(sname)
                if st is None:
                    continue
                gone = maps.get(sname)
                for r in st.records:
                    if gone is not None and gone[r.index] is None:
                        continue
                    if oname == "Nets6" and r.KIND in ("text", "body"):
                        continue
                    if oname == "Polygons6" and r.KIND in ("via", "pad", "text", "body"):
                        continue
                    v = r._get("H", off, r._main)
                    if v in (NONE16, BOARD16) or v >= len(m):
                        continue
                    if m[v] is None:
                        raise EditError(f"{sname}[{r.index}] still uses {oname}[{v}], which "
                                        "is being removed; remove or reassign it first")
            if oname == "Nets6":
                for p in doc.polygons:
                    if maps.get("Polygons6") and maps["Polygons6"][p.index] is None:
                        continue
                    if p.net is not None and p.net < len(m) and m[p.net] is None:
                        raise EditError(f"polygon {p.index} is on a net being removed")

        # remap owner fields in surviving primitives
        for oname, off in owner.items():
            if oname not in maps:
                continue
            m = maps[oname]
            for sname in PRIMITIVE_STORAGES:
                st = doc.storages.get(sname)
                for r in (st.records if st else []):
                    if oname == "Nets6" and r.KIND in ("text", "body"):
                        continue
                    if oname == "Polygons6" and r.KIND in ("via", "pad", "text", "body"):
                        continue
                    v = r._get("H", off, r._main)
                    if v not in (NONE16, BOARD16) and v < len(m) and m[v] is not None:
                        r._set("H", off, m[v], r._main)
        if "Nets6" in maps:
            m = maps["Nets6"]
            for p in doc.polygons:
                if p.net is not None and p.net < len(m) and m[p.net] is not None:
                    p.props.set("NET", str(m[p.net]))

        self._remap_tables(maps)

        for name, idx in removals.items():
            st = doc.storages[name]
            st.records = [r for i, r in enumerate(st.records) if i not in idx]
        if any(name in maps for name in ("Nets6", "Components6", "Polygons6", "Rules6") +
               PRIMITIVE_STORAGES):
            self._drop_violations()
        doc.reindex()

    def _remap_tables(self, maps):
        doc = self.doc

        def idx_map(storage, i):
            m = maps.get(storage)
            if m is None:
                return i
            return m[i] if 0 <= i < len(m) else i

        st = doc.storages.get("PrimitiveGuids")
        if st is not None:
            keep = []
            for e in st.records:
                kind, i = struct.unpack_from("<II", e, 0)
                name = GUID_KINDS.get(kind)
                if name is None and kind >> 8 == 0x09:
                    name = "Rules6"
                if name in maps:
                    j = idx_map(name, i)
                    if j is None:
                        continue
                    struct.pack_into("<I", e, 4, j)
                keep.append(e)
            st.records = keep
        st = doc.storages.get("PadViaCacheLibraryLinksSection")
        if st is not None:
            keep = []
            for e in st.records:
                i, kind = struct.unpack_from("<I", e, 1)[0], e[5]
                name = PADVIA_KINDS.get(kind)
                if name in maps:
                    j = idx_map(name, i)
                    if j is None:
                        continue
                    struct.pack_into("<I", e, 1, j)
                keep.append(e)
            st.records = keep
        for sname, okey in (("UniqueIDPrimitiveInformation", "PRIMITIVEOBJECTID"),
                            ("ExtendedPrimitiveInformation", "PRIMITIVEOBJECTID"),
                            ("CustomMaskShapes", None)):
            st = doc.storages.get(sname)
            if st is None:
                continue
            keep = []
            for p in st.records:
                name = OBJECT_STORAGE.get(p.get(okey, "")) if okey else "Pads6"
                if name in maps:
                    i = int(parse_float(p.get("PRIMITIVEINDEX", "-1"), -1))
                    j = idx_map(name, i)
                    if j is None:
                        continue
                    p.set("PRIMITIVEINDEX", str(j))
                keep.append(p)
            st.records = keep
        st = doc.storages.get("SharedUnion")
        if st is not None:
            keep = []
            for p in st.records:
                ok = True
                refs = [("PRIMITIVEINDEX", "OBJECTID")]
                k = 0
                while p.get(f"REF{k}INDEX") is not None:
                    refs.append((f"REF{k}INDEX", f"REF{k}OBJID"))
                    k += 1
                for ik, ok_key in refs:
                    name = OBJECT_STORAGE.get(p.get(ok_key, ""))
                    if name in maps:
                        j = idx_map(name, int(parse_float(p.get(ik, "-1"), -1)))
                        if j is None:
                            ok = False
                            break
                        p.set(ik, str(j))
                if ok:
                    keep.append(p)
                else:
                    self._note("dropped a shared union whose member was removed")
            st.records = keep
        st = doc.storages.get("Dimensions6")
        for d in (st.records if st else []):
            k = 0
            while d.get(f"REFERENCE{k}PRIM") is not None:
                name = OBJECT_STORAGE.get(d.get(f"REFERENCE{k}OBJECTSTRING", ""))
                if name in maps:
                    j = idx_map(name, int(parse_float(d.get(f"REFERENCE{k}PRIM"), -1)))
                    if j is None:
                        d.set(f"REFERENCE{k}OBJECTSTRING", "NoObject")
                        d.set(f"REFERENCE{k}OBJECTID", "0")
                        d.set(f"REFERENCE{k}PRIM", "-1")
                        self._note("a dimension lost the object it was attached to")
                    else:
                        d.set(f"REFERENCE{k}PRIM", str(j))
                k += 1

    def _drop_violations(self):
        from .pcbdoc import is_violation_storage
        for name, st in self.doc.storages.items():
            if is_violation_storage(name) and st.records:
                self.dropped_violations += len(st.records)
                st.records = []

    def _new_guid(self, storage, index, kind=None):
        st = self.doc.storages.get("PrimitiveGuids")
        if st is None:
            return
        if kind is None:
            kinds = [k for k, v in GUID_KINDS.items() if v == storage]
            if not kinds:
                return
            kind = kinds[0]
        st.records.append(bytearray(struct.pack("<II", kind, index) + uuid.uuid4().bytes))

    def _added(self, rec):
        """Book-keeping for a primitive pcbwrite.add() appended."""
        if isinstance(rec, tuple):
            for r in rec:
                self._new_guid(r.storage, r.index)
        else:
            self._new_guid(rec.storage, rec.index)
        return rec[0] if isinstance(rec, tuple) else rec

    # ------------------------------------------------------------ adding primitives
    def add(self, rec):
        out = pcbwrite.add(self.doc, rec)
        if isinstance(rec, tuple):
            self._added(rec)
        else:
            self._added(out)
        return out

    def add_track(self, layer, x1, y1, x2, y2, width, net=None):
        return self.add(R.track(self.doc, layer, x1, y1, x2, y2, width, self.net_index(net)))

    def add_arc(self, layer, cx, cy, radius, start_angle, end_angle, width, net=None):
        return self.add(R.arc(self.doc, layer, cx, cy, radius, start_angle, end_angle, width,
                              self.net_index(net)))

    def add_via(self, x, y, diameter, hole, net=None, low="Top Layer", high="Bottom Layer"):
        return self.add(R.via(self.doc, x, y, diameter, hole, self.net_index(net), low, high))

    def add_fill(self, layer, x1, y1, x2, y2, rotation=0.0, net=None):
        return self.add(R.fill(self.doc, layer, x1, y1, x2, y2, rotation, self.net_index(net)))

    def add_region(self, layer, outline, holes=(), net=None, kind=0):
        return self.add(R.region(self.doc, layer, outline, holes, self.net_index(net), kind=kind))

    def add_poured_region(self, poly, outline, holes=()):
        """Poured copper of a polygon; its net is the polygon's."""
        i = self.polygon_index(poly)
        return self.add(R.region(self.doc, self.doc.polygons[i].layer, outline, holes, None,
                                 polygon=i))

    def add_text(self, text, x, y, height, layer="Top Overlay", rotation=0.0):
        return self.add(R.text(self.doc, text, x, y, height, layer, rotation))

    def add_pad(self, component, name, x, y, sx, sy, layer="Top Layer", shape="rect", hole=0.0,
                rotation=0.0, net=None, plated=True, corner_pct=0):
        comp = self.doc.component(component).index if component is not None else None
        return self.add(R.pad(self.doc, name, x, y, sx, sy, layer, shape, hole, rotation,
                              self.net_index(net), comp, plated, corner_pct))

    # ------------------------------------------------------------ nets
    def add_net(self, name):
        doc = self.doc
        if doc.has_net(name):
            raise EditError(f"net {name!r} already exists")
        st = pcbwrite.ensure_storage(doc, "Nets6", "props")
        if st.records:
            p = Props(st.records[0].text_bytes())
            for k in [k for k in p.keys() if k.upper().startswith(("UNICODE", "%UTF8%"))]:
                p.remove(k)
        else:
            p = Props.new([("SELECTION", "FALSE"), ("LAYER", "TOP"), ("LOCKED", "FALSE")])
        p.set("NAME", name)
        p.set("UNIQUEID", R.unique_id())
        p.dirty = True
        st.records.append(p)
        doc.reindex()
        self._new_guid("Nets6", len(st.records) - 1)
        self._note(f"added net {name}")
        return doc.net_index(name)

    def set_net(self, recs, net):
        """Move primitives to another net. Poured copper follows its polygon:
        change the polygon's net instead."""
        n = self.net_index(net)
        count = 0
        for r in recs:
            if r.KIND in ("text", "body"):
                continue
            if r.polygon is not None and r.KIND in ("track", "arc", "region"):
                raise EditError(f"{r.storage}[{r.index}] is poured copper of polygon "
                                f"{r.polygon}; change the polygon's net")
            r.net = n
            if r.storage in TWINS and r.storage == "Regions6":
                twin = self._st("ShapeBasedRegions6").records[r.index]
                twin.net = n
            count += 1
        self.doc.invalidate()
        self._note(f"moved {count} primitives to net {self.doc.nets[n].name if n is not None else None}")
        return count

    def set_pad_net(self, designator, pad, net):
        """Put every pad named `pad` of `designator` on `net` (None: no net),
        creating the net if needed. Returns how many pads changed."""
        doc = self.doc
        comp = doc.component(designator)
        pads = [p for p in doc.pads if p.component == comp.index and p.name == str(pad)]
        if not pads:
            raise EditError(f"{designator} has no pad {pad!r}")
        if net is not None and not doc.has_net(net):
            self.add_net(net)
        idx = None if net is None else doc.net_index(net)
        for p in pads:
            p.net = idx
        self._note(f"{designator}-{pad} -> {net if net is not None else '(no net)'}")
        return len(pads)

    def set_component_props(self, designator, props):
        """Set Components6 values (e.g. SOURCEUNIQUEID) on one component."""
        comp = self.doc.component(designator)
        for k, v in props.items():
            if str(k).upper() in ("X", "Y", "ROTATION", "LAYER", "PATTERN"):
                raise EditError(f"{k} is changed by move_component or swap_footprint, not here")
            comp.props.set(str(k).upper(), str(v))
        comp.props.dirty = True
        self.doc.invalidate()

    def rename_net(self, old, new):
        doc = self.doc
        net = doc.net(old)
        if doc.has_net(new) and doc.net(new).index != net.index:
            raise EditError(f"net {new!r} already exists; use merge_nets")
        old_name = net.name
        net.props.set("NAME", new)
        for c in doc.classes:
            if c.kind == "net":
                _rename_member(c.props, old_name, new)
        st = self._st("DifferentialPairs6")
        for p in (st.records if st else []):
            for key in ("POSITIVENETNAME", "NEGATIVENETNAME"):
                if p.get(key, "").upper() == old_name.upper():
                    p.set(key, new)
        for i in range(1, 17):
            if doc.board_props.get(f"PLANE{i}NETNAME", "").upper() == old_name.upper():
                doc.board_props.set(f"PLANE{i}NETNAME", new)
        for r in doc.rules:
            for key in ("SCOPE1EXPRESSION", "SCOPE2EXPRESSION"):
                expr = r.props.get(key)
                if expr and re.search(re.escape(f"'{old_name}'"), expr, re.I):
                    r.props.set(key, re.sub(re.escape(f"'{old_name}'"), f"'{new}'", expr,
                                            flags=re.I))
        doc.reindex()
        self._note(f"renamed net {old_name} -> {new}")

    def merge_nets(self, sources, into):
        """Move everything on the source nets (pads included) to `into`, then
        delete the emptied nets."""
        doc = self.doc
        if isinstance(sources, str):
            sources = [sources]
        if not doc.has_net(into):
            self.add_net(into)
        target = doc.net_index(into)
        src = [doc.net_index(s) for s in sources]
        if target in src:
            raise EditError("a net cannot be merged into itself")
        moved = 0
        for sname in PRIMITIVE_STORAGES:
            st = self._st(sname)
            for r in (st.records if st else []):
                if r.KIND in ("text", "body"):
                    continue
                if r._get("H", 3, r._main) in src:
                    r._set("H", 3, target, r._main)
                    moved += 1
        for p in doc.polygons:
            if p.net in src:
                p.props.set("NET", str(target))
        names = [doc.nets[i].name for i in src]
        tname = doc.nets[target].name
        for c in doc.classes:
            if c.kind == "net":
                for nm in names:
                    _remove_member(c.props, nm)
        st = self._st("DifferentialPairs6")
        if st is not None:
            keep = []
            for p in st.records:
                if {p.get("POSITIVENETNAME", "").upper(), p.get("NEGATIVENETNAME", "").upper()} & \
                        {n.upper() for n in names}:
                    self._note(f"dropped differential pair {p.get('NAME')} (a member net was merged)")
                    continue
                keep.append(p)
            st.records = keep
        for i in range(1, 17):
            if doc.board_props.get(f"PLANE{i}NETNAME", "").upper() in {n.upper() for n in names}:
                doc.board_props.set(f"PLANE{i}NETNAME", tname)
        for r in doc.rules:
            for key in ("SCOPE1EXPRESSION", "SCOPE2EXPRESSION"):
                expr = r.props.get(key, "")
                for nm in names:
                    if re.search(re.escape(f"'{nm}'"), expr, re.I):
                        self.warnings.append(f"rule {r.name!r} still names merged net {nm}")
        doc.invalidate()
        self._remove({"Nets6": set(src)})
        self._note(f"merged {', '.join(names)} into {tname} ({moved} primitives)")
        return moved

    def remove_net(self, name):
        i = self.doc.net_index(name)
        self._remove({"Nets6": {i}})
        self._note(f"removed net {name}")

    # ------------------------------------------------------------ components
    def placement(self, comp):
        return Placement(comp.x, comp.y, comp.rotation, comp.layer == L.BOTTOM)

    def remove_component(self, designator, keep_copper=False):
        """Remove a component with everything it owns. keep_copper leaves
        its free-standing tracks/arcs/fills/vias in place as free copper."""
        doc = self.doc
        comp = doc.component(designator)
        removals = {}
        for sname in PRIMITIVE_STORAGES:
            st = self._st(sname)
            for r in (st.records if st else []):
                if r.component == comp.index:
                    if keep_copper and r.KIND in ("track", "arc", "fill", "via"):
                        r.component = None
                        continue
                    removals.setdefault(sname, set()).add(r.index)
        for c in doc.classes:
            if c.kind == "component":
                _remove_member(c.props, comp.designator)
        removals["Components6"] = {comp.index}
        n = sum(len(v) for k, v in removals.items() if k != "Components6")
        self._remove(removals)
        self._note(f"removed component {designator} and {n} primitives")
        return n

    def move_component(self, designator, x=None, y=None, rotation=None, side=None):
        """Move, rotate and/or flip a component with all its children."""
        doc = self.doc
        comp = doc.component(designator)
        old = self.placement(comp)
        new = Placement(old.x if x is None else x, old.y if y is None else y,
                        old.r if rotation is None else rotation,
                        old.bottom if side is None else str(side).lower().startswith("b"))
        flip = new.bottom != old.bottom
        order = doc.copper_layers

        def swap(layer):
            if not flip:
                return layer
            if layer in SIDE_SWAP:
                return SIDE_SWAP[layer]
            if layer in order:
                return order[len(order) - 1 - order.index(layer)]
            return layer

        def P(x_, y_):
            return new.to_world(*old.to_local(x_, y_))

        def A(a):
            return new.angle_to_world(old.angle_to_local(a)) % 360.0

        for sname in PRIMITIVE_STORAGES:
            st = self._st(sname)
            for r in (st.records if st else []):
                if r.component != comp.index:
                    continue
                k = r.KIND
                if k == "track":
                    (a1, b1), (a2, b2) = P(r.x1, r.y1), P(r.x2, r.y2)
                    r.set_geometry(a1, b1, a2, b2)
                    r.set_layer(swap(r.layer))
                elif k == "arc":
                    cx, cy = P(r.cx, r.cy)
                    la1, la2 = old.arc_to_local(r.start_angle, r.end_angle)
                    w1, w2 = new.arc_to_world(la1, la2)
                    r.set_geometry(cx, cy, None, w1 % 360.0, w2 % 360.0)
                    r.set_layer(swap(r.layer))
                elif k == "via":
                    r.set_geometry(*P(r.x, r.y))
                elif k == "pad":
                    px, py = P(r.x, r.y)
                    r.set_geometry(px, py, A(r.rotation))
                    if flip and r.layer != L.MULTI:
                        r.subs[4][0] = swap(r.layer)
                        if r._has(114, 4, 4):
                            r._set("I", 114, R.v7_for(swap(r.layer)), 4)
                    if flip and r.mode == 1:
                        _swap_pad_sides(r)
                elif k == "fill":
                    cx, cy = (r.x1 + r.x2) / 2, (r.y1 + r.y2) / 2
                    hx, hy = abs(r.x2 - r.x1) / 2, abs(r.y2 - r.y1) / 2
                    nx, ny = P(cx, cy)
                    r.set_geometry(nx - hx, ny - hy, nx + hx, ny + hy, A(r.rotation))
                    r.set_layer(swap(r.layer))
                elif k == "text":
                    tx, ty = P(r.x, r.y)
                    r.set_geometry(tx, ty, None, A(r.rotation))
                    if flip:
                        r.subs[0][35] = 0 if r.mirrored else 1
                        r.subs[0][0] = swap(r.subs[0][0])
                elif k in ("region", "body"):
                    _transform_region(r, P, old, new, swap if k == "region" else (lambda v: v))
                    if k == "body":
                        _transform_body_props(r.props, P, A, flip)
        comp.props.set("X", fmt_mil(new.x))
        comp.props.set("Y", fmt_mil(new.y))
        comp.props.set("ROTATION", fmt_float(new.r))
        comp.props.set("LAYER", "BOTTOM" if new.bottom else "TOP")
        if flip:
            self.warnings.append(f"{designator} was flipped: 3D body rotation is not "
                                 "re-derived; check its model in Altium")
        doc.invalidate()
        self._note(f"moved {designator} to ({new.x:.4f}, {new.y:.4f}) rot {new.r:g} "
                   f"{'bottom' if new.bottom else 'top'}")

    def add_component(self, designator, pattern, x, y, rotation=0.0, side="top",
                      children=(), template=None, source_unique_id=None, comment=""):
        """Add a component and its footprint primitives.

        children: footprint primitives in component-local mm (as
        footprints.extract() returns them, or plain dicts; see
        _child_record). template: an existing component whose properties are
        copied (default: the first component on the board).
        """
        doc = self.doc
        if doc.has_component(designator):
            raise EditError(f"component {designator} already exists")
        st = pcbwrite.ensure_storage(doc, "Components6", "props")
        base = doc.component(template) if template else (doc.components[0] if doc.components else None)
        if base is not None:
            p = Props(base.props.text_bytes())
            for k in [k for k in p.keys() if k.upper().startswith(("UNICODE", "%UTF8%"))]:
                p.remove(k)
        else:
            p = Props.new([("SELECTION", "FALSE"), ("LOCKED", "FALSE"), ("NAMEON", "TRUE"),
                           ("COMMENTON", "FALSE")])
        bottom = str(side).lower().startswith("b")
        for k, v in (("LAYER", "BOTTOM" if bottom else "TOP"), ("X", fmt_mil(x)),
                     ("Y", fmt_mil(y)), ("ROTATION", fmt_float(rotation % 360.0)),
                     ("PATTERN", pattern), ("SOURCEDESIGNATOR", designator),
                     ("UNIQUEID", R.unique_id()),
                     ("SOURCEUNIQUEID", source_unique_id or "\\" + R.unique_id()),
                     ("SOURCELIBREFERENCE", comment or pattern)):
            p.set(k, v)
        p.dirty = True
        st.records.append(p)
        doc.reindex()
        ci = len(st.records) - 1
        self._new_guid("Components6", ci)
        place = Placement(x, y, rotation, bottom)
        made = 0
        for ch in children:
            rec = _child_record(self, ch, place, ci, bottom)
            if rec is not None:
                self.add(rec)
                made += 1
        self._note(f"added component {designator} ({pattern}) with {made} primitives")
        return ci

    # ------------------------------------------------------------ polygons
    def add_polygon(self, layer, outline, net=None, name=None, hatch="Solid", pour_index=None,
                    template=None, **settings):
        doc = self.doc
        st = pcbwrite.ensure_storage(doc, "Polygons6", "props")
        base = doc.polygons[self.polygon_index(template)] if template is not None else (
            doc.polygons[0] if doc.polygons else None)
        if base is not None:
            p = Props(base.props.text_bytes())
            set_props_vertices(p, [])
        else:
            p = Props.new([("POLYGONTYPE", "Polygon"), ("POUROVER", "TRUE"),
                           ("REMOVEDEAD", "TRUE"), ("GRIDSIZE", "20mil"), ("TRACKWIDTH", "8mil"),
                           ("MINPRIMLENGTH", "3mil"), ("REMOVEISLANDSBYAREA", "TRUE"),
                           ("AREATHRESHOLD", "250000000000"), ("REMOVENECKS", "TRUE"),
                           ("NECKWIDTHTHRESHOLD", "5mil"), ("ARCRESOLUTION", "0.5mil")])
        n = self.net_index(net)
        p.set("LAYER", L.TOKENS[L.parse(layer)])
        p.set("NET", str(n) if n is not None else str(NONE16))
        p.set("HATCHSTYLE", hatch)
        p.set("POURINDEX", str(pour_index if pour_index is not None else
                               max([q.pour_index for q in doc.polygons], default=-1) + 1))
        if name:
            p.set("NAME", name)
            p.remove("UNICODE__NAME")
        p.set("UNIQUEID", R.unique_id())
        for k, v in settings.items():
            p.set(k.upper(), fmt_mil(v) if isinstance(v, float) else str(v))
        set_props_vertices(p, outline)
        p.dirty = True
        st.records.append(p)
        doc.reindex()
        idx = len(st.records) - 1
        self._new_guid("Polygons6", idx)
        self.stale_pours.add(idx)
        self._note(f"added polygon {idx} on {L.name(L.parse(layer))} (needs a pour)")
        return idx

    def unpour(self, poly):
        """Remove a polygon's poured copper: Altium shows it unpoured until a repour."""
        i = self.polygon_index(poly)
        recs = list(self.doc.poured(i))
        if recs:
            self._remove({r.storage: {x.index for x in recs if x.storage == r.storage}
                          for r in recs})
        self.stale_pours.add(i)
        return len(recs)

    def remove_polygon(self, poly):
        doc = self.doc
        i = self.polygon_index(poly)
        name = doc.polygons[i].name
        removals = {}
        for r in doc.poured(i):
            removals.setdefault(r.storage, set()).add(r.index)
        removals["Polygons6"] = {i}
        for c in doc.classes:
            if c.kind == "polygon":
                _remove_member(c.props, name)
        self._remove(removals)
        self.stale_pours = {j - (1 if j > i else 0) for j in self.stale_pours if j != i}
        self._note(f"removed polygon {i} ({name})")

    def set_polygon_outline(self, poly, outline):
        i = self.polygon_index(poly)
        set_props_vertices(self.doc.polygons[i].props, outline)
        n = self.unpour(i)
        self._note(f"polygon {i} outline replaced ({len(outline)} points); "
                   f"{n} poured primitives removed, repour needed")

    def set_polygon(self, poly, **props):
        i = self.polygon_index(poly)
        p = self.doc.polygons[i].props
        for k, v in props.items():
            if k.lower() == "net":
                n = self.net_index(v)
                p.set("NET", str(n) if n is not None else str(NONE16))
            else:
                p.set(k.upper(), fmt_mil(v) if isinstance(v, float) else str(v))
        self.unpour(i)

    def merge_polygons(self, a, b, bridge_fills=True):
        """Merge two same-net, same-layer polygons into one.

        The outlines are unioned (with any same-net fill that bridges them,
        which is then deleted). The survivor keeps its own settings and pour
        index: when either polygon overlaps another net's polygon on the
        layer it is the one with the higher pour index, otherwise `a`. The
        other polygon goes; the survivor needs a repour. Notes name the
        neighbours whose pour order may change.
        """
        sh = geom.require()
        doc = self.doc
        ia, ib = self.polygon_index(a), self.polygon_index(b)
        pa, pb = doc.polygons[ia], doc.polygons[ib]
        if pa.layer != pb.layer or pa.net != pb.net:
            raise EditError("merge_polygons needs two polygons on the same layer and net")
        ga, gb = geom.polygon_outline(pa), geom.polygon_outline(pb)
        parts = [ga, gb]
        bridges = []
        if bridge_fills:
            for f in doc.fills:
                if f.layer == pa.layer and f.effective_net == pa.net and f.component is None:
                    fg = geom.fill_shape(f)
                    if fg.intersects(ga) and fg.intersects(gb):
                        parts.append(fg)
                        bridges.append(f)
        merged = sh.union_all(parts)
        if merged.geom_type != "Polygon":
            raise EditError("the two polygons do not touch or overlap (no bridging fill either); "
                            "draw a joining shape first")
        others = [(q, geom.polygon_outline(q)) for q in doc.polygons
                  if q.layer == pa.layer and q.net != pa.net and q.index not in (ia, ib)]
        near_a = [q.index for q, g in others if g.intersects(ga)]
        near_b = [q.index for q, g in others if g.intersects(gb)]
        new_near = [q.index for q, g in others if g.intersects(merged)
                    and q.index not in near_a + near_b]
        keep, drop = (ia, ib)
        if (near_a or near_b) and pb.pour_index > pa.pour_index:
            keep, drop = ib, ia
        lost = near_a if keep == ib else near_b
        if lost:
            self._note(f"polygons {lost} overlapped the polygon that goes; the merged one pours "
                       f"in polygon {keep}'s place, check Polygon Pour Order")
        if new_near:
            self._note(f"the merged outline now overlaps polygons {new_near}; check pour order")
        pts = list(merged.exterior.coords)[:-1]
        if bridges:
            self.remove(bridges)
        set_props_vertices(doc.polygons[keep].props, pts)
        self.unpour(keep)
        self.remove_polygon(drop)
        keep_new = keep - (1 if keep > drop else 0)
        self._note(f"merged polygons {ia} and {ib} into {keep_new}"
                   + (f"; deleted {len(bridges)} bridge fill(s)" if bridges else ""))
        return keep_new

    # ------------------------------------------------------------ classes and rules
    def add_net_class(self, name, nets):
        doc = self.doc
        for n in nets:
            if not doc.has_net(n):
                raise EditError(f"net class {name}: no net named {n!r}")
        st = pcbwrite.ensure_storage(doc, "Classes6", "props")
        pairs = [("SELECTION", "FALSE"), ("LAYER", "MULTILAYER"), ("LOCKED", "FALSE"),
                 ("NAME", name), ("KIND", "0"), ("SUPERCLASS", "FALSE"), ("SELECTED", "FALSE"),
                 ("UNIQUEID", R.unique_id())]
        pairs += [(f"M{i}", doc.net(n).name) for i, n in enumerate(nets)]
        st.records.append(Props.new(pairs))
        doc.reindex()
        self._new_guid("Classes6", len(st.records) - 1)
        self._note(f"added net class {name} ({len(nets)} nets)")

    def add_rule(self, kind, name, scope1="All", scope2="All", priority=None, values=None,
                 enabled=True):
        from .scope import compile_scope
        compile_scope(scope1)
        compile_scope(scope2)
        doc = self.doc
        st = pcbwrite.ensure_storage(doc, "Rules6", "props")
        base = next((r for r in doc.rules if r.kind == kind), None)
        if base is not None:
            p = Props(base.props.text_bytes(), prefix=base.props.prefix)
        else:
            p = Props.new([("SELECTION", "FALSE"), ("LAYER", "TOP"), ("LOCKED", "FALSE"),
                           ("RULEKIND", kind)], prefix=b"\x02\x00")
        if priority is None:
            priority = 1 + max([r.priority for r in doc.rules if r.kind == kind], default=0)
        for k, v in (("RULEKIND", kind), ("NAME", name), ("ENABLED", "TRUE" if enabled else "FALSE"),
                     ("PRIORITY", str(int(priority))), ("SCOPE1EXPRESSION", scope1),
                     ("SCOPE2EXPRESSION", scope2), ("UNIQUEID", R.unique_id())):
            p.set(k, v)
        for k, v in (values or {}).items():
            p.set(k.upper(), fmt_mil(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v))
        p.dirty = True
        st.records.append(p)
        doc.reindex()
        self._note(f"added {kind} rule {name}")

    def set_rule_scope(self, name, scope1=None, scope2=None):
        from .scope import compile_scope
        hits = [r for r in self.doc.rules if r.name.upper() == name.upper()]
        if len(hits) != 1:
            raise EditError(f"rule {name!r} matches {len(hits)} rules")
        for key, val in (("SCOPE1EXPRESSION", scope1), ("SCOPE2EXPRESSION", scope2)):
            if val is not None:
                compile_scope(val)
                hits[0].props.set(key, val)

    # ------------------------------------------------------------ save
    def report(self):
        return {"log": self.log, "warnings": self.warnings,
                "stale_pours": sorted(self.stale_pours),
                "dropped_violations": self.dropped_violations}

    def save(self, path):
        rep = pcbwrite.save(self.doc, path)
        rep.update(self.report())
        if self.stale_pours:
            rep["next_step"] = ("Open in Altium and repour (Tools > Polygon Pours > Repour All), "
                                "or let the live backend rebuild them in pour-index order.")
        return rep


# ---------------------------------------------------------------- helpers

def _rename_member(props, old, new):
    i = 0
    while props.get(f"M{i}") is not None:
        if props.get(f"M{i}").upper() == old.upper():
            props.set(f"M{i}", new)
        i += 1


def _remove_member(props, name):
    members, i = [], 0
    while props.get(f"M{i}") is not None:
        members.append(props.get(f"M{i}"))
        props.remove(f"M{i}")
        i += 1
    kept = [m for m in members if m.upper() != name.upper()]
    for j, m in enumerate(kept):
        props.set(f"M{j}", m)
    if len(kept) != len(members):
        props.dirty = True


def _inside(r, box):
    x0, y0, x1, y1 = min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3])

    def ok(x, y):
        return x0 <= x <= x1 and y0 <= y <= y1
    k = r.KIND
    if k == "track":
        return ok(r.x1, r.y1) and ok(r.x2, r.y2)
    if k == "arc":
        return ok(r.cx, r.cy)
    if k in ("via", "pad", "text"):
        return ok(r.x, r.y)
    if k == "fill":
        return all(ok(x, y) for x, y in r.corners())
    if k in ("region", "body"):
        pts = r.outline()
        return bool(pts) and all(ok(x, y) for x, y in pts)
    return False


def _swap_pad_sides(p):
    s = p.subs[4]
    top = bytes(s[21:29])
    s[21:29] = s[37:45]
    s[37:45] = top
    s[49], s[51] = s[51], s[49]


def _transform_region(r, P, old, new, swap):
    """Rewrite a region/body's vertices in place, keeping arcs as arcs."""
    s = r.subs[0]
    props, verts, holes, end = r._parse()
    plen = struct.unpack_from("<I", s, 18)[0]
    pos = 22 + plen + 4
    for v in verts:
        if r.extended:
            rnd, x, y, cx, cy, rad, a1, a2 = v
            nx, ny = P(x, y)
            ncx, ncy = P(cx, cy) if rnd else (0.0, 0.0)
            if rnd:
                la1, la2 = old.arc_to_local(a1, a2)
                a1, a2 = (w % 360.0 for w in new.arc_to_world(la1, la2))
            struct.pack_into("<iiiii", s, pos + 1, mm2u(nx), mm2u(ny),
                             mm2u(ncx) if rnd else 0, mm2u(ncy) if rnd else 0, mm2u(rad))
            struct.pack_into("<dd", s, pos + 21, a1, a2)
            pos += 37
        else:
            nx, ny = P(v[1], v[2])
            struct.pack_into("<dd", s, pos, float(mm2u(nx)), float(mm2u(ny)))
            pos += 16
    for h in holes:
        pos += 4
        for (x, y) in h:
            nx, ny = P(x, y)
            struct.pack_into("<dd", s, pos, float(mm2u(nx)), float(mm2u(ny)))
            pos += 16
    lay = swap(s[0])
    if lay != s[0]:
        s[0] = lay
        if props.get("V7_LAYER") is not None:
            props.set("V7_LAYER", L.TOKENS.get(lay, props.get("V7_LAYER")))
            pb = props.text_bytes()
            s[18:22 + plen] = struct.pack("<I", len(pb)) + pb
    r._parsed = None


def _transform_body_props(p, P, A, flip):
    x, y = parse_length(p.get("MODEL.2D.X")), parse_length(p.get("MODEL.2D.Y"))
    if p.get("MODEL.2D.X") is not None:
        nx, ny = P(x, y)
        p.set("MODEL.2D.X", fmt_mil(nx))
        p.set("MODEL.2D.Y", fmt_mil(ny))
    if p.get("MODEL.2D.ROTATION") is not None:
        p.set("MODEL.2D.ROTATION", f"{A(parse_float(p.get('MODEL.2D.ROTATION'))):.3f}")


def _child_record(ed, ch, place, comp_index, bottom):
    """A footprint primitive (component-local mm) placed on the board."""
    doc = ed.doc
    k = ch["kind"]
    flip = (lambda lay: SIDE_SWAP.get(lay, lay)) if bottom else (lambda lay: lay)
    lay = flip(L.parse(ch.get("layer", "Top Layer")))
    if k == "pad":
        x, y = place.to_world(ch["x"], ch["y"])
        rec = R.pad(doc, ch.get("name", ""), x, y, ch["sx"], ch["sy"], lay if ch.get("hole", 0) <= 0
                    else L.MULTI, ch.get("shape", "rect"), ch.get("hole", 0.0),
                    place.angle_to_world(ch.get("rotation", 0.0)) % 360.0,
                    ed.net_index(ch.get("net")), comp_index, ch.get("plated", True),
                    ch.get("corner_pct", 0))
        return rec
    if k == "track":
        (x1, y1), (x2, y2) = place.to_world(ch["x1"], ch["y1"]), place.to_world(ch["x2"], ch["y2"])
        return R.track(doc, lay, x1, y1, x2, y2, ch["width"], ed.net_index(ch.get("net")),
                       comp_index)
    if k == "arc":
        cx, cy = place.to_world(ch["cx"], ch["cy"])
        a1, a2 = place.arc_to_world(ch["start_angle"], ch["end_angle"])
        return R.arc(doc, lay, cx, cy, ch["radius"], a1, a2, ch["width"],
                     ed.net_index(ch.get("net")), comp_index)
    if k == "fill":
        cx, cy = place.to_world((ch["x1"] + ch["x2"]) / 2, (ch["y1"] + ch["y2"]) / 2)
        hx, hy = abs(ch["x2"] - ch["x1"]) / 2, abs(ch["y2"] - ch["y1"]) / 2
        return R.fill(doc, lay, cx - hx, cy - hy, cx + hx, cy + hy,
                      place.angle_to_world(ch.get("rotation", 0.0)) % 360.0,
                      ed.net_index(ch.get("net")), comp_index)
    if k == "region":
        pts = [place.to_world(x, y) for x, y in ch["outline"]]
        holes = [[place.to_world(x, y) for x, y in h] for h in ch.get("holes", [])]
        return R.region(doc, lay, pts, holes, ed.net_index(ch.get("net")), None, comp_index)
    if k == "text":
        x, y = place.to_world(ch["x"], ch["y"])
        t = R.text(doc, ch["text"], x, y, ch["height"], lay,
                   place.angle_to_world(ch.get("rotation", 0.0)) % 360.0,
                   component=comp_index, designator=ch.get("designator", False),
                   comment=ch.get("comment", False))
        if bottom:
            t.subs[0][35] = 1
        return t
    if k == "via":
        x, y = place.to_world(ch["x"], ch["y"])
        v = R.via(doc, x, y, ch["diameter"], ch["hole"], ed.net_index(ch.get("net")))
        v.component = comp_index
        return v
    raise EditError(f"unknown footprint primitive kind {k!r}")
