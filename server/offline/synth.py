"""Small synthetic boards built in code, for tests that need no fixture file.

    s = Synth(40, 30)                         # mm, 2 copper layers
    vbat = s.net("VBAT")
    s.component("U1", "SOIC8", 10, 10)
    s.pad("U1", "1", 8, 9, 1.5, 0.6, net="VBAT")
    s.track("Top Layer", 8, 9, 20, 9, 0.5, net="VBAT")
    doc = s.build()                           # a PcbDoc, parsed back from bytes

Built boards go through the real writer and reader, so they test both. They
are deliberately minimal and are not meant to open in Altium: a real board
carries dozens of storages these leave out (dev/validate_pcbwrite.py is the
check against Altium itself).
"""

import struct
import uuid

from . import layers as L
from . import pcbwrite
from . import records as R
from .cfb import TYPE_ROOT, TYPE_STORAGE, CompoundFile
from .cfb_write import DirNode, to_bytes
from .pcbdoc import PcbDoc, Props, fmt_float, fmt_mil

FILE_HEADER = struct.pack("<I", 19) + "PCB 5.0 Bi".encode("utf-16-le")

BINARY_STORAGES = ("Arcs6", "Pads6", "Vias6", "Tracks6", "Texts6", "Fills6", "Regions6",
                   "ShapeBasedRegions6", "ComponentBodies6", "ShapeBasedComponentBodies6")


def _props_stream(records, prefix=b""):
    return b"".join(p.encode() for p in records)


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


class Synth:
    def __init__(self, width=40.0, height=30.0, layers=2, copper_mm=0.035, x0=0.0, y0=0.0,
                 board_thickness=1.6, plane_nets=None, outline=None):
        if layers not in (2, 4, 6):
            raise ValueError("synthetic boards have 2, 4 or 6 copper layers")
        self.outline = outline or _rect(x0, y0, x0 + width, y0 + height)
        self.origin = (x0, y0)
        mids = {2: [], 4: [2, 3], 6: [2, 3, 4, 5]}[layers]
        self.copper = [L.TOP] + mids + [L.BOTTOM]
        self.copper_mm = copper_mm
        self.thickness = board_thickness
        self.plane_nets = plane_nets or {}
        self.nets, self.components, self.rules, self.classes, self.polygons = [], [], [], [], []
        self.ops = []

    # ---------------------------------------------------------------- objects
    def net(self, name):
        if name not in self.nets:
            self.nets.append(name)
        return self.nets.index(name)

    def component(self, designator, pattern, x, y, rotation=0.0, layer="TOP", comment=""):
        self.components.append((designator, pattern, x, y, rotation, layer, comment))
        return len(self.components) - 1

    def rule(self, kind, name, scope1="All", scope2="All", priority=1, enabled=True, **values):
        self.rules.append((kind, name, scope1, scope2, priority, enabled, values))

    def net_class(self, name, members, kind=0):
        self.classes.append((name, kind, list(members)))

    def polygon(self, layer, outline, net=None, hatch="Solid", pour_index=None, **settings):
        self.polygons.append((layer, outline, net, hatch, pour_index, settings))
        return len(self.polygons) - 1

    def _later(self, fn):
        self.ops.append(fn)

    def track(self, layer, x1, y1, x2, y2, width, net=None):
        self._later(lambda d: pcbwrite.add(d, R.track(d, layer, x1, y1, x2, y2, width, net)))

    def arc(self, layer, cx, cy, r, a1, a2, width, net=None):
        self._later(lambda d: pcbwrite.add(d, R.arc(d, layer, cx, cy, r, a1, a2, width, net)))

    def via(self, x, y, diameter=0.6, hole=0.3, net=None):
        self._later(lambda d: pcbwrite.add(d, R.via(d, x, y, diameter, hole, net)))

    def pad(self, component, name, x, y, sx, sy, layer="TOP", shape="rect", hole=0.0,
            rotation=0.0, net=None, corner_pct=0):
        self._later(lambda d: pcbwrite.add(d, R.pad(d, name, x, y, sx, sy, layer, shape, hole,
                                                    rotation, net, component,
                                                    corner_pct=corner_pct)))

    def fill(self, layer, x1, y1, x2, y2, net=None, rotation=0.0):
        self._later(lambda d: pcbwrite.add(d, R.fill(d, layer, x1, y1, x2, y2, rotation, net)))

    def region(self, layer, outline, holes=(), net=None, polygon=None, kind=0):
        self._later(lambda d: pcbwrite.add(d, R.region(d, layer, outline, holes, net, polygon,
                                                       kind=kind)))

    def text(self, value, x, y, height=1.0, layer="TOPOVERLAY", component=None, designator=False):
        self._later(lambda d: pcbwrite.add(d, R.text(d, value, x, y, height, layer,
                                                     component=component,
                                                     designator=designator)))

    # ---------------------------------------------------------------- build
    def _board_props(self):
        pairs = [("RECORD", "Board"), ("ORIGINX", fmt_mil(self.origin[0])),
                 ("ORIGINY", fmt_mil(self.origin[1]))]
        gaps = len(self.copper) - 1
        diel = (self.thickness - self.copper_mm * len(self.copper)) / gaps
        for i, lay in enumerate(self.copper):
            prev = self.copper[i - 1] if i else 0
            nxt = self.copper[i + 1] if i + 1 < len(self.copper) else 0
            pairs += [(f"LAYER{lay}NAME", L.name(lay)), (f"LAYER{lay}PREV", str(prev)),
                      (f"LAYER{lay}NEXT", str(nxt)), (f"LAYER{lay}MECHENABLED", "FALSE"),
                      (f"LAYER{lay}COPTHICK", fmt_mil(self.copper_mm)),
                      (f"LAYER{lay}DIELTYPE", "1"), (f"LAYER{lay}DIELCONST", "4.500"),
                      (f"LAYER{lay}DIELHEIGHT", fmt_mil(diel if nxt else 0.0)),
                      (f"LAYER{lay}DIELMATERIAL", "FR-4")]
        for i in range(1, 17):
            pairs.append((f"PLANE{i}NETNAME", self.plane_nets.get(i, "(No Net)")))
        for i, (x, y) in enumerate(self.outline):
            pairs += [(f"KIND{i}", "0"), (f"VX{i}", fmt_mil(x)), (f"VY{i}", fmt_mil(y)),
                      (f"CX{i}", "0mil"), (f"CY{i}", "0mil"), (f"SA{i}", fmt_float(0.0)),
                      (f"EA{i}", fmt_float(0.0)), (f"R{i}", "0mil")]
        return Props.new(pairs)

    def _skeleton(self):
        root = DirNode("Root Entry", TYPE_ROOT)
        root.set_stream("FileHeader", FILE_HEADER)

        def storage(name, data, count):
            root.set_stream(f"{name}/Header", struct.pack("<I", count))
            root.set_stream(f"{name}/Data", data)

        storage("Board6", self._board_props().encode(), 1)
        nets = [Props.new([("SELECTION", "FALSE"), ("LAYER", "TOP"), ("LOCKED", "FALSE"),
                           ("NAME", n), ("VISIBLE", "TRUE"), ("UNIQUEID", R.unique_id())])
                for n in self.nets]
        storage("Nets6", _props_stream(nets), len(nets))
        comps = []
        for des, pat, x, y, rot, layer, comment in self.components:
            comps.append(Props.new([
                ("SELECTION", "FALSE"), ("LAYER", L.TOKENS[L.parse(layer)]), ("LOCKED", "FALSE"),
                ("X", fmt_mil(x)), ("Y", fmt_mil(y)), ("PATTERN", pat), ("NAMEON", "TRUE"),
                ("COMMENTON", "FALSE"), ("ROTATION", fmt_float(rot)),
                ("SOURCEDESIGNATOR", des), ("SOURCEUNIQUEID", "\\" + R.unique_id()),
                ("SOURCELIBREFERENCE", comment or pat), ("UNIQUEID", R.unique_id())]))
        storage("Components6", _props_stream(comps), len(comps))
        rules = []
        for kind, name, s1, s2, prio, enabled, values in self.rules:
            pairs = [("SELECTION", "FALSE"), ("RULEKIND", kind), ("NAME", name),
                     ("ENABLED", "TRUE" if enabled else "FALSE"), ("PRIORITY", str(prio)),
                     ("SCOPE1EXPRESSION", s1), ("SCOPE2EXPRESSION", s2),
                     ("UNIQUEID", R.unique_id())]
            for k, v in values.items():
                pairs.append((k.upper(), fmt_mil(v) if isinstance(v, (int, float)) and
                              not isinstance(v, bool) else str(v)))
            rules.append(Props.new(pairs, prefix=b"\x02\x00"))
        storage("Rules6", _props_stream(rules), len(rules))
        classes = []
        for name, kind, members in self.classes:
            pairs = [("NAME", name), ("KIND", str(kind)), ("SUPERCLASS", "FALSE")]
            pairs += [(f"M{i}", m) for i, m in enumerate(members)]
            classes.append(Props.new(pairs))
        storage("Classes6", _props_stream(classes), len(classes))
        polys = []
        for i, (layer, outline, net, hatch, pour_index, settings) in enumerate(self.polygons):
            pairs = [("LAYER", L.TOKENS[L.parse(layer)]),
                     ("NET", str(self.nets.index(net)) if net else "65535"),
                     ("POLYGONTYPE", settings.get("kind", "Polygon")), ("HATCHSTYLE", hatch),
                     ("POURINDEX", str(i if pour_index is None else pour_index)),
                     ("GRIDSIZE", fmt_mil(settings.get("grid", 0.5))),
                     ("TRACKWIDTH", fmt_mil(settings.get("track_width", 0.25))),
                     ("MINPRIMLENGTH", fmt_mil(0.0762)),
                     ("POUROVER", "TRUE" if settings.get("pour_over", True) else "FALSE"),
                     ("REMOVEDEAD", "TRUE" if settings.get("remove_dead", True) else "FALSE"),
                     ("REMOVEISLANDSBYAREA", "TRUE" if settings.get("remove_islands", True) else "FALSE"),
                     ("AREATHRESHOLD", str(settings.get("island_area_mm2", 0.0) / (2.54e-6) ** 2)),
                     ("REMOVENECKS", "TRUE" if settings.get("remove_necks", False) else "FALSE"),
                     ("NECKWIDTHTHRESHOLD", fmt_mil(settings.get("neck_width", 0.127))),
                     ("ARCRESOLUTION", "0.5mil"), ("NAME", settings.get("name", f"Poly{i}"))]
            for k, (x, y) in enumerate(outline):
                pairs += [(f"KIND{k}", "0"), (f"VX{k}", fmt_mil(x)), (f"VY{k}", fmt_mil(y)),
                          (f"CX{k}", "0mil"), (f"CY{k}", "0mil"), (f"SA{k}", fmt_float(0.0)),
                          (f"EA{k}", fmt_float(0.0)), (f"R{k}", "0mil")]
            polys.append(Props.new(pairs))
        storage("Polygons6", _props_stream(polys), len(polys))
        storage("WideStrings6", b"", 0)
        storage("UniqueIDPrimitiveInformation", b"", 0)
        storage("Dimensions6", b"", 0)
        for name in BINARY_STORAGES:
            storage(name, b"", 0)
        root.set_stream("FileHeaderSix", struct.pack("<I", 19) + b"\x13PCB 6.0 Binary File" +
                        f"{{{str(uuid.uuid4()).upper()}}}".encode())
        return root

    def build(self):
        data = to_bytes(self._skeleton())
        doc = PcbDoc(CompoundFile(data))
        for op in self.ops:
            op(doc)
        doc.reindex()
        problems = pcbwrite.check_integrity(doc)
        if problems:
            raise pcbwrite.IntegrityError("; ".join(problems))
        return doc

    def save(self, path):
        doc = self.build()
        pcbwrite.save(doc, path)
        return PcbDoc.open(path)


def stress_board(seed=1):
    """A large two-layer board for speed tests, laid out pseudo-randomly but
    deterministically: 120 x 100 mm, about 3,200 tracks, 500 arcs, 480 pads,
    300 vias and 48 polygons on four power nets. Not a real design.
    """
    import random
    rnd = random.Random(seed)
    s = Synth(120.0, 100.0, layers=2, copper_mm=0.07)
    s.rule("Clearance", "Clearance", GAP=0.2)
    s.rule("Width", "Width", MINLIMIT=0.15, MAXLIMIT=10, PREFEREDWIDTH=0.3)
    s.rule("PolygonConnect", "PolygonConnect", CONNECTSTYLE="Relief", RELIEFCONDUCTORWIDTH=0.3,
           RELIEFENTRIES="4", POLYGONRELIEFANGLE="90 Angle", AIRGAPWIDTH=0.25)
    power = ["GND", "VIN", "VOUT", "VAUX"]
    for n in power:
        s.net(n)
    signals = [f"S{i}" for i in range(200)]
    for n in signals:
        s.net(n)
    pads = {}   # net -> [(x, y, layer)]
    cols, rows, cw, ch = 16, 10, 7.2, 9.6
    n_part = 0
    for r in range(rows):
        for c in range(cols):
            x0, y0 = 4.0 + c * cw, 4.0 + r * ch
            n_part += 1
            if (r * cols + c) % 8 == 3:          # a SOIC-8 in one cell of eight
                des = f"U{n_part}"
                s.component(des, "SOIC8", x0 + 2.5, y0 + 3.0)
                for k in range(8):
                    px = x0 + 0.6 + (k % 4) * 1.27
                    py = y0 + (0.8 if k < 4 else 5.2)
                    net = rnd.choice(signals) if k not in (3, 4) else power[k % 2]
                    s.pad(des, str(k + 1), px, py, 0.6, 1.5, net=net)
                    pads.setdefault(net, []).append((px, py, "Top Layer"))
            else:
                des = f"R{n_part}"
                rot = rnd.choice((0.0, 90.0))
                cx, cy = x0 + 2.5, y0 + 3.0
                s.component(des, "R0805", cx, cy, rotation=rot)
                for k, sgn in enumerate((-1, 1)):
                    px, py = (cx + sgn * 1.0, cy) if rot == 0 else (cx, cy + sgn * 1.0)
                    net = rnd.choice(power) if k == 1 and rnd.random() < 0.6 else rnd.choice(signals)
                    s.pad(des, str(k + 1), px, py, 1.0, 1.2, rotation=rot, net=net)
                    pads.setdefault(net, []).append((px, py, "Top Layer"))
    for j, (jx, jy) in enumerate(((60.0, 2.0), (60.0, 98.0))):
        des = f"J{j + 1}"
        s.component(des, "HDR20", jx, jy)
        for k in range(20):
            px = jx - 24.13 + k * 2.54
            net = power[k % 4] if k % 5 == 0 else rnd.choice(signals)
            s.pad(des, str(k + 1), px, jy, 1.7, 1.7, layer="MULTILAYER", shape="round", hole=1.0,
                  net=net)
            pads.setdefault(net, []).append((px, jy, "Top Layer"))
    tracks = arcs = vias = 0
    for net, pts in pads.items():
        if net in power or len(pts) < 2:
            continue
        for (xa, ya, _), (xb, yb, _) in zip(pts, pts[1:]):
            layer = rnd.choice(("Top Layer", "Bottom Layer"))
            if layer == "Bottom Layer":
                s.via(xa + 0.8, ya, 0.6, 0.3, net=net)
                s.via(xb + 0.8, yb, 0.6, 0.3, net=net)
                vias += 2
                xa += 0.8
                xb += 0.8
            # a route in short segments, with a rounded corner
            steps = 7
            xm = xa + (xb - xa) * rnd.uniform(0.3, 0.7)
            pts_route = [(xa + (xm - xa) * i / steps, ya) for i in range(steps + 1)]
            pts_route += [(xm, ya + (yb - ya) * i / steps) for i in range(1, steps + 1)]
            pts_route += [(xm + (xb - xm) * i / steps, yb) for i in range(1, steps + 1)]
            for (x1, y1), (x2, y2) in zip(pts_route, pts_route[1:]):
                if abs(x1 - x2) + abs(y1 - y2) > 1e-6:
                    s.track(layer, x1, y1, x2, y2, 0.25, net=net)
                    tracks += 1
            for a0 in (0.0, 90.0, 180.0):
                if arcs < 500:
                    s.arc(layer, xm, ya, 0.5, a0, a0 + 90.0, 0.25, net=net)
                    arcs += 1
    while vias < 300:                            # ground stitching
        s.via(rnd.uniform(3, 117), rnd.uniform(3, 97), 0.6, 0.3, net="GND")
        vias += 1
    k = 0
    for layer in ("Top Layer", "Bottom Layer"):
        for r in range(4):
            for c in range(6):
                x0, y0 = 1.0 + c * 19.6, 1.0 + r * 24.5
                outline = [(x0, y0), (x0 + 20.0, y0), (x0 + 20.0, y0 + 25.0), (x0, y0 + 25.0)]
                s.polygon(layer, outline, net=power[(r + c + k) % 4], pour_index=k,
                          remove_necks=True, neck_width=0.127, island_area_mm2=1.0)
                k += 1
    return s
