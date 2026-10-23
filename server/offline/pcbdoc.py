"""Read an Altium .PcbDoc without Altium.

Coordinates come out in millimetres, y up, as in the editor. Binary primitives
store 1/10000 mil per unit; text properties carry explicit units ("2578.5761mil").

Everything here was decoded from real files and checked against Altium's own
API output (track/via/polygon counts, pour outline areas, stackup), because the
format is undocumented. Unknown trailing fields are skipped, never guessed at.
Anything that fails to parse is recorded in PcbDoc.warnings rather than
raised, so a partial read is still useful.
"""
import math
import re
import struct
from dataclasses import dataclass, field
from typing import Optional

from . import geom
from .cfb import CompoundFile

MM_PER_MIL = 0.0254
MM_PER_UNIT = MM_PER_MIL / 10000.0
NONE = 0xFFFF

LAYER_NAMES = {1: "Top Layer", 32: "Bottom Layer", 33: "Top Overlay", 34: "Bottom Overlay",
               35: "Top Paste", 36: "Bottom Paste", 37: "Top Solder", 38: "Bottom Solder",
               55: "Drill Guide", 56: "Keep-Out Layer", 73: "Drill Drawing", 74: "Multi-Layer"}
for _i in range(2, 32):
    LAYER_NAMES[_i] = "Mid-Layer %d" % (_i - 1)
for _i in range(1, 17):
    LAYER_NAMES[38 + _i] = "Internal Plane %d" % _i
    LAYER_NAMES[56 + _i] = "Mechanical %d" % _i
COPPER_LAYERS = {1, 32} | set(range(2, 32))
PAD_SHAPES = {1: "round", 2: "rect", 3: "octagon", 9: "roundrect"}


def layer_name(n):
    return LAYER_NAMES.get(n, "Layer %d" % n)


# ---- small parsing helpers -------------------------------------------------

def _text_records(data):
    i, n = 0, len(data)
    while i + 4 <= n:
        ln = struct.unpack_from("<I", data, i)[0]
        i += 4
        yield data[i:i + ln]
        i += ln


def _props(rec):
    """Ordered (key, value) pairs from a |KEY=VALUE|... record."""
    text = rec.split(b"\x00")[0].decode("latin-1")
    out = []
    for part in text.split("|"):
        if part:
            k, _, v = part.partition("=")
            out.append((k, v))
    return out


_UNIT = re.compile(r"^\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)\s*(mil|mm|in)?\s*$")


def to_mm(s, default=None):
    """12mil / 3mm / 0.5in -> mm. Bare numbers are taken as mils."""
    m = _UNIT.match(s or "")
    if not m:
        return default
    v, unit = float(m.group(1)), m.group(2)
    return v * {"mil": MM_PER_MIL, "mm": 1.0, "in": 25.4, None: MM_PER_MIL}[unit]


def _flt(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def decode_name(s):
    """Polygon names are stored as comma-separated character codes."""
    if s and re.fullmatch(r"[0-9]+(,[0-9]+)*,?", s):
        try:
            return "".join(chr(int(c)) for c in s.split(",") if c)
        except ValueError:
            pass
    return s or ""


def _bin_records(data, warnings, what):
    """type byte + uint32 length + payload."""
    i, n = 0, len(data)
    while i + 5 <= n:
        ln = struct.unpack_from("<I", data, i + 1)[0]
        if i + 5 + ln > n:
            warnings.append("%s: truncated record at %d" % (what, i))
            return
        yield data[i], data[i + 5:i + 5 + ln]
        i += 5 + ln


# ---- data classes -----------------------------------------------------------

@dataclass
class Vertex:
    kind: int          # 0 = line to the next vertex, 1 = arc to the next vertex
    x: float
    y: float
    cx: float = 0.0
    cy: float = 0.0
    r: float = 0.0
    sa: float = 0.0
    ea: float = 0.0


def outline_points(vertices, tol=0.01):
    """Flatten a vertex list (with arcs) into a closed polyline.

    An arc vertex carries centre, radius and start/end angles, and the arc always runs
    counter-clockwise from the start angle to the end angle. Whether this vertex sits at
    the start or at the end depends on which way the polygon is wound, so the direction
    is taken from which end of the arc coincides with this vertex.
    """
    pts = []
    n = len(vertices)
    for i, v in enumerate(vertices):
        nxt = vertices[(i + 1) % n]
        if v.kind == 1 and v.r > 0:
            arc = geom.arc_points(v.cx, v.cy, v.r, v.sa, v.ea, tol)
            fwd = math.dist(arc[0], (v.x, v.y)) + math.dist(arc[-1], (nxt.x, nxt.y))
            rev = math.dist(arc[-1], (v.x, v.y)) + math.dist(arc[0], (nxt.x, nxt.y))
            if rev < fwd:
                arc.reverse()
            arc[0] = (v.x, v.y)
            arc[-1] = (nxt.x, nxt.y)
            pts.extend(arc[:-1])
        else:
            pts.append((v.x, v.y))
    return pts


@dataclass
class Net:
    idx: int
    name: str


@dataclass
class ObjClass:
    name: str
    kind: int          # 0 net, 1 component, 2 from-to, 3 pad, 4 layer, 6 diff pair, 7 polygon
    superclass: bool
    members: list = field(default_factory=list)

    @property
    def kind_name(self):
        names = {0: "net", 1: "component", 2: "from-to", 3: "pad", 4: "layer", 5: "design channel",
                 6: "diff pair", 7: "polygon"}
        return names.get(self.kind, "kind %d" % self.kind)


@dataclass
class Rule:
    name: str
    kind: str
    priority: int
    enabled: bool
    scope1: str
    scope2: str
    props: dict

    def mm(self, key, default=None):
        return to_mm(self.props.get(key), default)


@dataclass
class Component:
    idx: int
    designator: str
    pattern: str
    layer: str
    x: float
    y: float
    rotation: float
    height: float
    description: str
    props: dict

    @property
    def bottom(self):
        return self.layer.upper().startswith("BOTTOM")


@dataclass
class Track:
    layer: int
    net: int
    comp: int
    x1: float
    y1: float
    x2: float
    y2: float
    width: float

    @property
    def length(self):
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)


@dataclass
class Arc:
    layer: int
    net: int
    comp: int
    cx: float
    cy: float
    r: float
    sa: float
    ea: float
    width: float


@dataclass
class Via:
    net: int
    x: float
    y: float
    dia: float
    hole: float
    layer_from: int
    layer_to: int


@dataclass
class Pad:
    name: str
    layer: int
    net: int
    comp: int
    x: float
    y: float
    sx: float          # size on the pad's own layer, before rotation
    sy: float
    hole: float        # 0 for SMD
    shape: int
    rotation: float
    plated: bool

    @property
    def shape_name(self):
        return PAD_SHAPES.get(self.shape, "shape %d" % self.shape)

    def ring(self):
        """Outline of the copper land as a closed polyline."""
        if self.shape == 1 and abs(self.sx - self.sy) < 1e-6:
            return geom.circle_ring(self.x, self.y, self.sx / 2)
        if self.shape in (1, 9):
            return geom.stadium_ring(self.x, self.y, self.sx, self.sy, self.rotation)
        return geom.rect_ring(self.x, self.y, self.sx, self.sy, self.rotation)


@dataclass
class Fill:
    layer: int
    net: int
    comp: int
    x1: float
    y1: float
    x2: float
    y2: float
    rotation: float


@dataclass
class Polygon:
    idx: int
    name: str
    layer: str
    net: int
    props: dict
    vertices: list

    def ring(self):
        return outline_points(self.vertices)

    @property
    def outline_area(self):
        return abs(geom.polygon_area(self.ring()))


@dataclass
class Region:
    layer: int
    net: int
    poly: int          # index into PcbDoc.polygons, or NONE
    comp: int
    kind: int          # 0 copper, 1 polygon cutout, 2 board cutout, 3 cavity
    params: dict
    outline: list
    holes: list

    @property
    def area(self):
        return abs(geom.polygon_area(self.outline)) - sum(abs(geom.polygon_area(h)) for h in self.holes)


@dataclass
class Body:
    layer: int
    comp: int
    params: dict
    outline: list

    @property
    def overall_height(self):
        return to_mm(self.params.get("OVERALLHEIGHT"), 0.0)

    @property
    def standoff(self):
        return to_mm(self.params.get("STANDOFFHEIGHT"), 0.0)


@dataclass
class StackLayer:
    name: str
    copper_mm: Optional[float]
    dielectric_mm: Optional[float]
    material: str


@dataclass
class Board:
    vertices: list
    origin: tuple
    layers: list
    props: dict

    def ring(self):
        return outline_points(self.vertices)

    @property
    def bbox(self):
        return geom.bbox(self.ring())

    @property
    def size(self):
        x0, y0, x1, y1 = self.bbox
        return (x1 - x0, y1 - y0)

    @property
    def area(self):
        return abs(geom.polygon_area(self.ring()))

    @property
    def thickness_mm(self):
        return sum((l.copper_mm or 0) + (l.dielectric_mm or 0) for l in self.layers)


# ---- the document -----------------------------------------------------------

def _vertices(props):
    verts = []
    i = 0
    while ("VX%d" % i) in props:
        verts.append(Vertex(int(props.get("KIND%d" % i, "0") or 0), to_mm(props["VX%d" % i], 0.0),
                            to_mm(props.get("VY%d" % i), 0.0), to_mm(props.get("CX%d" % i), 0.0),
                            to_mm(props.get("CY%d" % i), 0.0), to_mm(props.get("R%d" % i), 0.0),
                            _flt(props.get("SA%d" % i), 0.0), _flt(props.get("EA%d" % i), 0.0)))
        i += 1
    # Altium repeats the first vertex to close the loop.
    if len(verts) > 2 and abs(verts[0].x - verts[-1].x) < 1e-6 and abs(verts[0].y - verts[-1].y) < 1e-6:
        verts.pop()
    return verts


def _parse_region(pl):
    """Region or component-body record: a 13-byte primitive header, 5 unknown bytes,
    a parameter string, the outline, then zero or more hole loops to the end of the
    record. Each loop is a uint32 count followed by that many (x, y) double pairs."""
    try:
        layer, _fl, net, poly, comp = struct.unpack_from("<BHHHH", pl, 0)
        o = 18
        plen = struct.unpack_from("<I", pl, o)[0]
        params = dict(_props(pl[o + 4:o + 4 + plen]))
        o += 4 + plen

        def loop(o):
            n = struct.unpack_from("<I", pl, o)[0]
            o += 4
            pts = [(a * MM_PER_UNIT, b * MM_PER_UNIT) for a, b in struct.iter_unpack("<dd", pl[o:o + 16 * n])]
            if len(pts) != n:
                raise struct.error("short loop")
            return pts, o + 16 * n

        outline, o = loop(o)
        holes = []
        while o + 4 <= len(pl):
            h, o = loop(o)
            holes.append(h)
        kind = int(params.get("KIND", "0") or 0)
        return Region(layer, net, poly, comp, kind, params, outline, holes)
    except (struct.error, ValueError):
        return None


class PcbDoc:
    def __init__(self, path):
        self.path = path
        self.warnings = []
        self.nets = []
        self.classes = []
        self.rules = []
        self.components = []
        self.tracks = []
        self.arcs = []
        self.vias = []
        self.pads = []
        self.fills = []
        self.polygons = []
        self.regions = []
        self.bodies = []
        self.board = None
        self._by_ref = {}

    # -- lookups
    def net_name(self, idx):
        return self.nets[idx].name if 0 <= idx < len(self.nets) else ""

    def comp_ref(self, idx):
        return self.components[idx].designator if 0 <= idx < len(self.components) else ""

    def component(self, designator):
        return self._by_ref.get(designator)

    def net_index(self, name):
        for n in self.nets:
            if n.name == name:
                return n.idx
        return None

    def pads_of(self, designator):
        c = self.component(designator)
        return [p for p in self.pads if c is not None and p.comp == c.idx]

    def polygon_regions(self, poly_idx):
        return [r for r in self.regions if r.poly == poly_idx]

    def poured_area(self, poly_idx):
        """Copper area of one polygon after pouring (outline minus cutouts), mm^2."""
        return sum(r.area for r in self.polygon_regions(poly_idx) if r.kind == 0)

    # -- loading
    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            cf = CompoundFile(f.read())
        doc = cls(str(path))
        doc._read(cf)
        return doc

    @staticmethod
    def _stream(cf, name):
        return cf.read(name) if cf.exists(name) else b""

    def _read(self, cf):
        self._read_board(cf)
        for i, rec in enumerate(_text_records(self._stream(cf, "Nets6/Data"))):
            self.nets.append(Net(i, dict(_props(rec)).get("NAME", "")))
        self._read_classes(cf)
        self._read_rules(cf)
        self._read_components(cf)
        self._read_tracks(cf)
        self._read_arcs(cf)
        self._read_vias(cf)
        self._read_pads(cf)
        self._read_fills(cf)
        self._read_polygons(cf)
        for t, pl in _bin_records(self._stream(cf, "Regions6/Data"), self.warnings, "Regions6"):
            r = _parse_region(pl)
            if r is None:
                self.warnings.append("Regions6: unparsable region")
            else:
                self.regions.append(r)
        for t, pl in _bin_records(self._stream(cf, "ComponentBodies6/Data"), self.warnings, "ComponentBodies6"):
            r = _parse_region(pl)
            if r is not None:
                self.bodies.append(Body(r.layer, r.comp, r.params, r.outline))

    def _read_board(self, cf):
        recs = list(_text_records(self._stream(cf, "Board6/Data")))
        if not recs:
            self.warnings.append("Board6: no record")
            self.board = Board([], (0.0, 0.0), [], {})
            return
        props = {}
        for k, v in _props(recs[0]):
            props[k] = v
        layers = []
        i = 0
        while ("V9_STACK_LAYER%d_NAME" % i) in props:
            p = "V9_STACK_LAYER%d_" % i
            layers.append(StackLayer(props[p + "NAME"], to_mm(props.get(p + "COPTHICK")),
                                     to_mm(props.get(p + "DIELHEIGHT")), props.get(p + "DIELMATERIAL", "")))
            i += 1
        self.board = Board(_vertices(props), (to_mm(props.get("ORIGINX"), 0.0), to_mm(props.get("ORIGINY"), 0.0)),
                           layers, props)

    def _read_classes(self, cf):
        for rec in _text_records(self._stream(cf, "Classes6/Data")):
            p = _props(rec)
            d = dict(p)
            members = [v for k, v in p if re.fullmatch(r"M[0-9]+", k)]
            self.classes.append(ObjClass(d.get("NAME", ""), int(d.get("KIND", "-1") or -1),
                                         d.get("SUPERCLASS", "FALSE") == "TRUE", members))

    def _read_rules(self, cf):
        d = self._stream(cf, "Rules6/Data")
        i = 0
        while i + 6 <= len(d):
            ln = struct.unpack_from("<I", d, i + 2)[0]
            i += 6
            props = dict(_props(d[i:i + ln]))
            i += ln
            self.rules.append(Rule(props.get("NAME", ""), props.get("RULEKIND", ""),
                                   int(props.get("PRIORITY", "1") or 1), props.get("ENABLED", "TRUE") == "TRUE",
                                   props.get("SCOPE1EXPRESSION", "All"), props.get("SCOPE2EXPRESSION", "All"), props))

    def _read_components(self, cf):
        for i, rec in enumerate(_text_records(self._stream(cf, "Components6/Data"))):
            d = dict(_props(rec))
            c = Component(i, d.get("SOURCEDESIGNATOR", ""), d.get("PATTERN", ""), d.get("LAYER", "TOP"),
                          to_mm(d.get("X"), 0.0), to_mm(d.get("Y"), 0.0), _flt(d.get("ROTATION"), 0.0),
                          to_mm(d.get("HEIGHT"), 0.0), d.get("SOURCEDESCRIPTION", ""), d)
            self.components.append(c)
            self._by_ref[c.designator] = c

    def _read_tracks(self, cf):
        for t, pl in _bin_records(self._stream(cf, "Tracks6/Data"), self.warnings, "Tracks6"):
            if len(pl) < 33:
                continue
            layer, _fl, net, _poly, comp = struct.unpack_from("<BHHHH", pl, 0)
            x1, y1, x2, y2, w = struct.unpack_from("<5i", pl, 13)
            self.tracks.append(Track(layer, net, comp, x1 * MM_PER_UNIT, y1 * MM_PER_UNIT,
                                     x2 * MM_PER_UNIT, y2 * MM_PER_UNIT, w * MM_PER_UNIT))

    def _read_arcs(self, cf):
        for t, pl in _bin_records(self._stream(cf, "Arcs6/Data"), self.warnings, "Arcs6"):
            if len(pl) < 45:
                continue
            layer, _fl, net, _poly, comp = struct.unpack_from("<BHHHH", pl, 0)
            cx, cy, r = struct.unpack_from("<3i", pl, 13)
            sa, ea = struct.unpack_from("<2d", pl, 25)
            w = struct.unpack_from("<i", pl, 41)[0]
            self.arcs.append(Arc(layer, net, comp, cx * MM_PER_UNIT, cy * MM_PER_UNIT, r * MM_PER_UNIT, sa, ea,
                                 w * MM_PER_UNIT))

    def _read_vias(self, cf):
        for t, pl in _bin_records(self._stream(cf, "Vias6/Data"), self.warnings, "Vias6"):
            if len(pl) < 31:
                continue
            _layer, _fl, net = struct.unpack_from("<BHH", pl, 0)
            x, y, dia, hole = struct.unpack_from("<4i", pl, 13)
            l0, l1 = struct.unpack_from("<BB", pl, 29)
            self.vias.append(Via(net, x * MM_PER_UNIT, y * MM_PER_UNIT, dia * MM_PER_UNIT, hole * MM_PER_UNIT, l0, l1))

    def _read_fills(self, cf):
        for t, pl in _bin_records(self._stream(cf, "Fills6/Data"), self.warnings, "Fills6"):
            if len(pl) < 37:
                continue
            layer, _fl, net, _poly, comp = struct.unpack_from("<BHHHH", pl, 0)
            x1, y1, x2, y2 = struct.unpack_from("<4i", pl, 13)
            rot = struct.unpack_from("<d", pl, 29)[0]
            self.fills.append(Fill(layer, net, comp, x1 * MM_PER_UNIT, y1 * MM_PER_UNIT, x2 * MM_PER_UNIT,
                                   y2 * MM_PER_UNIT, rot))

    def _read_polygons(self, cf):
        for i, rec in enumerate(_text_records(self._stream(cf, "Polygons6/Data"))):
            d = dict(_props(rec))
            net = int(d["NET"]) if d.get("NET", "").lstrip("-").isdigit() else -1
            self.polygons.append(Polygon(i, decode_name(d.get("NAME", "")), d.get("LAYER", ""), net, d, _vertices(d)))

    def _read_pads(self, cf):
        d = self._stream(cf, "Pads6/Data")
        i, n = 0, len(d)
        while i < n:
            if d[i] != 2:
                self.warnings.append("Pads6: unexpected record type %d at %d" % (d[i], i))
                break
            i += 1
            subs = []
            for _ in range(6):
                if i + 4 > n:
                    break
                ln = struct.unpack_from("<I", d, i)[0]
                subs.append(d[i + 4:i + 4 + ln])
                i += 4 + ln
            if len(subs) < 5 or len(subs[4]) < 61:
                self.warnings.append("Pads6: short pad record")
                continue
            name = subs[0][1:1 + subs[0][0]].decode("latin-1") if subs[0] else ""
            m = subs[4]
            layer, _fl, net, _poly, comp = struct.unpack_from("<BHHHH", m, 0)
            x, y, sx, sy = struct.unpack_from("<4i", m, 13)
            hole = struct.unpack_from("<i", m, 45)[0]
            rot = struct.unpack_from("<d", m, 52)[0]
            self.pads.append(Pad(name, layer, net, comp, x * MM_PER_UNIT, y * MM_PER_UNIT, sx * MM_PER_UNIT,
                                 sy * MM_PER_UNIT, hole * MM_PER_UNIT, m[49], rot, bool(m[60])))
