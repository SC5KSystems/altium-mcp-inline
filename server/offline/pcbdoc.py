"""Read an Altium .PcbDoc into plain objects, losslessly.

Units: the Python API is millimetres, Y up, in absolute (database) coordinates
as the file stores them; `board.origin` is the user origin for reports that
want origin-relative numbers. Altium's internal unit is 1e-7 inch (0.0001 mil).

Every record keeps its own bytes. Fields are decoded from them and written
back into them, so a record nobody edits re-encodes byte for byte, including
fields nobody has decoded. Streams this module does not parse are kept as raw
bytes. The writer (pcbwrite.py) relies on both.

    b = PcbDoc.open("board.PcbDoc")
    t = b.tracks[0]; t.layer, t.net_name, t.x1, t.width
    b.component("U5").x;  b.nets[3].name;  b.polygons[0].outline
"""

import math
import struct
from pathlib import Path

from . import layers as L
from .cfb import CompoundFile

MM_PER_UNIT = 2.54e-6          # 1 unit = 1e-7 inch
NONE16 = 0xFFFF
BOARD16 = 0xFFFE      # polygon field of plane-boundary tracks: a marker, not an index


class FormatError(ValueError):
    pass


def u2mm(v):
    return v * MM_PER_UNIT


def mm2u(v):
    u = int(round(float(v) / MM_PER_UNIT))
    if not -2 ** 31 <= u < 2 ** 31:
        raise FormatError(f"{v} mm is outside Altium's coordinate range")
    return u


def parse_length(s, default=0.0):
    """'1162.5mil' / '0.2mm' / '1.5in' -> mm."""
    if s is None:
        return default
    s = str(s).strip()
    if not s:
        return default
    low = s.lower()
    try:
        if low.endswith("mil"):
            return float(low[:-3]) * 0.0254
        if low.endswith("mm"):
            return float(low[:-2])
        if low.endswith("in"):
            return float(low[:-2]) * 25.4
        return float(low) * MM_PER_UNIT
    except ValueError:
        raise FormatError(f"bad length {s!r}")


def fmt_mil(mm):
    """mm -> Altium's '1234.5678mil' text form."""
    mil = round(mm / 0.0254, 4)
    s = f"{mil:.4f}".rstrip("0").rstrip(".")
    return ("0" if s in ("-0", "") else s) + "mil"


def fmt_float(v):
    """Altium's ' 2.70000000000000E+0002' text form for angles."""
    if v == 0:
        return " 0.00000000000000E+0000"
    sign = "-" if v < 0 else " "
    m, e = f"{abs(v):.14E}".split("E")
    return f"{sign}{m}E{e[0]}{int(e[1:]):04d}"


def parse_float(s, default=0.0):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return default


def parse_bool(s, default=False):
    if s is None:
        return default
    return str(s).strip().upper() in ("TRUE", "T", "1", "YES")


# ------------------------------------------------------------------ props

class Props:
    """One |KEY=VALUE| text record, kept byte-exact until edited.

    Keys compare case-insensitively. Values are text decoded as cp1252 with
    surrogate escapes, so every byte survives a round trip.
    """

    __slots__ = ("prefix", "flag", "lead", "items", "terminated", "_raw", "dirty", "_idx")

    def __init__(self, text_bytes, prefix=b"", flag=0):
        self.prefix = prefix
        self.flag = flag
        self._raw = text_bytes
        self.dirty = False
        self._idx = None
        self.terminated = text_bytes.endswith(b"\0")
        body = text_bytes[:-1] if self.terminated else text_bytes
        text = body.decode("cp1252", errors="surrogateescape")
        self.lead = text.startswith("|")
        parts = text.split("|")
        if self.lead:
            parts = parts[1:]
        self.items = []
        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                self.items.append([k, v])
            else:
                self.items.append([p, None])

    @classmethod
    def new(cls, pairs, prefix=b"", lead=True):
        p = cls(b"\0", prefix)
        p.items = [[k, v] for k, v in pairs]
        p._idx = None
        p.lead = lead
        p.terminated = True
        p.dirty = True
        return p

    def _find(self, key):
        if self._idx is None:
            idx = {}
            for it in self.items:
                idx.setdefault(it[0].upper(), it)
            self._idx = idx
        return self._idx.get(key.upper())

    def get(self, key, default=None):
        it = self._find(key)
        return default if it is None or it[1] is None else it[1]

    def __contains__(self, key):
        return self._find(key) is not None

    def text(self, key, default=""):
        """A value with Altium's Unicode side channels applied."""
        uni = self.get("UNICODE__" + key)
        if uni:
            try:
                return "".join(chr(int(c)) for c in uni.split(",") if c.strip())
            except ValueError:
                pass
        utf = self.get("%UTF8%" + key)
        if utf is not None:
            try:
                return utf.encode("cp1252", errors="surrogateescape").decode("utf-8")
            except UnicodeError:
                pass
        v = self.get(key)
        if v is None:
            return default
        if "," in v and key.upper() == "NAME" and all(c.strip().isdigit() for c in v.split(",")):
            return "".join(chr(int(c)) for c in v.split(","))
        return v

    def set(self, key, value):
        value = str(value)
        it = self._find(key)
        if it is None:
            self.items.append([key, value])
            self._idx = None
        elif it[1] == value:
            return
        else:
            k = key.upper()
            for it2 in self.items:
                if it2[0].upper() == k:
                    it2[1] = value
        self.dirty = True
        for side in ("UNICODE__" + key, "%UTF8%" + key):
            if self._find(side) is not None:
                self.remove(side)

    def remove(self, key):
        k = key.upper()
        n = len(self.items)
        self.items = [it for it in self.items if it[0].upper() != k]
        if len(self.items) != n:
            self.dirty = True
            self._idx = None

    def keys(self):
        return [it[0] for it in self.items]

    def text_bytes(self):
        if not self.dirty:
            return self._raw
        parts = [k if v is None else f"{k}={v}" for k, v in self.items]
        text = ("|" if self.lead else "") + "|".join(parts)
        return text.encode("cp1252", errors="surrogateescape") + (b"\0" if self.terminated else b"")

    def encode(self):
        body = self.text_bytes()
        n = len(body)
        if n >= 1 << 24:
            raise FormatError("property record too long")
        return self.prefix + struct.pack("<I", n | (self.flag << 24)) + body


def split_props(data, prefix=0):
    out, pos = [], 0
    while pos < len(data):
        pre = data[pos:pos + prefix]
        pos += prefix
        if pos + 4 > len(data):
            raise FormatError("truncated property record")
        n, = struct.unpack_from("<I", data, pos)
        pos += 4
        flag, n = n >> 24, n & 0xFFFFFF
        if pos + n > len(data):
            raise FormatError("property record runs past the stream end")
        out.append(Props(data[pos:pos + n], pre, flag))
        pos += n
    return out


# ------------------------------------------------------------------ binary records

class BinRecord:
    """A typed binary record: type byte, then length-prefixed sub-records."""

    KIND = None
    __slots__ = ("doc", "storage", "index", "rtype", "subs")

    def __init__(self, doc, storage, index, rtype, subs):
        self.doc = doc
        self.storage = storage
        self.index = index
        self.rtype = rtype
        self.subs = subs  # list of bytearray

    def encode(self):
        out = bytearray([self.rtype])
        for s in self.subs:
            out += struct.pack("<I", len(s))
            out += s
        return bytes(out)

    # field access on sub-record 0 unless told otherwise
    def _get(self, fmt, off, sub=0):
        return struct.unpack_from("<" + fmt, self.subs[sub], off)[0]

    def _set(self, fmt, off, value, sub=0):
        struct.pack_into("<" + fmt, self.subs[sub], off, value)

    def _has(self, off, size, sub=0):
        return len(self.subs[sub]) >= off + size

    @property
    def layer(self):
        v7 = self.v7_layer
        if v7 is not None and (v7 >> 16) & 0xFF == 2 and self.subs[self._main][0] == 72:
            return L.mech(v7 & 0xFFFF)  # mechanical above 16: byte says 16, V7 says which
        return self.subs[self._main][0]

    _main = 0
    _v7_off = None

    @property
    def v7_layer(self):
        off = self._v7_off
        if off is None or not self._has(off, 4, self._main):
            return None
        v = self._get("I", off, self._main)
        return v if v >> 24 == 1 else None

    def set_layer(self, layer):
        layer = L.parse(layer)
        has_v7 = self._v7_off is not None and self._has(self._v7_off, 4, self._main)
        if layer > 1000 and not has_v7:
            raise FormatError(f"{L.name(layer)} needs a V7 layer id this record has no room for")
        self.subs[self._main][0] = 72 if layer > 1000 else layer
        if has_v7:
            self._set("I", self._v7_off, v7_for(layer), self._main)

    @property
    def layer_name(self):
        return L.name(self.layer)

    @property
    def flags1(self):
        return self.subs[self._main][1]

    @property
    def flags2(self):
        return self.subs[self._main][2]

    @property
    def locked(self):
        return not self.flags1 & 0x04

    @property
    def is_keepout(self):
        return self.flags2 == 2

    @property
    def keepout_restrictions(self):
        """What a keep-out keeps out: KEEPOUT_VIA | _TRACK | _COPPER | _SMD_PAD |
        _TH_PAD. The mask byte follows the V7 layer id; a keep-out without one
        (or with 0), and anything on the Keep-Out layer, keeps out everything.
        0 for objects that are not keep-outs."""
        if not self.is_keepout and self.layer != L.KEEPOUT:
            return 0
        off = self._v7_off
        if off is not None and self._has(off + 4, 1, self._main):
            v = self.subs[self._main][off + 4]
            if v:
                return v
        return KEEPOUT_ALL

    @property
    def net(self):
        v = self._get("H", 3, self._main)
        return None if v == NONE16 else v

    @net.setter
    def net(self, idx):
        self._set("H", 3, NONE16 if idx is None else int(idx), self._main)

    @property
    def net_name(self):
        n = self.effective_net
        return self.doc.nets[n].name if n is not None and n < len(self.doc.nets) else None

    @property
    def effective_net(self):
        """The record's net, or its parent polygon's for poured copper."""
        n = self.net
        if n is None:
            p = self.polygon
            if p is not None and p < len(self.doc.polygons):
                return self.doc.polygons[p].net
        return n

    @property
    def polygon(self):
        """Owning polygon index; None for none and for the 0xFFFE marker that
        plane-boundary tracks carry (it is not an index)."""
        v = self._get("H", 5, self._main)
        return None if v >= BOARD16 else v

    @property
    def plane_boundary(self):
        return self._get("H", 5, self._main) == BOARD16

    @polygon.setter
    def polygon(self, idx):
        self._set("H", 5, NONE16 if idx is None else int(idx), self._main)

    @property
    def component(self):
        v = self._get("H", 7, self._main)
        return None if v == NONE16 else v

    @component.setter
    def component(self, idx):
        self._set("H", 7, NONE16 if idx is None else int(idx), self._main)

    @property
    def designator(self):
        c = self.component
        return self.doc.components[c].designator if c is not None and c < len(self.doc.components) else None

    def __repr__(self):
        return f"<{type(self).__name__} {self.storage}[{self.index}] {self.layer_name} net={self.net_name}>"


def v7_for(layer):
    """The 32-bit V7 layer id this file format pairs with a V6 layer number."""
    if 1 <= layer <= 31:
        return 0x01000000 | layer
    if layer == L.BOTTOM:
        return 0x0100FFFF
    if L.is_plane(layer):
        return 0x01010000 | (layer - 38)
    if L.is_mechanical(layer):
        return 0x01020000 | L.mech_number(layer)
    special = {33: 6, 34: 7, 35: 8, 36: 9, 37: 10, 38: 11, 55: 12, 56: 13, 73: 14, 74: 15}
    if layer in special:
        return 0x01030000 | special[layer]
    raise FormatError(f"no V7 id known for layer {layer}")


def _xy(rec, off, sub=0):
    x, y = struct.unpack_from("<ii", rec.subs[sub], off)
    return u2mm(x), u2mm(y)


def _set_xy(rec, off, x, y, sub=0):
    struct.pack_into("<ii", rec.subs[sub], off, mm2u(x), mm2u(y))


class Track(BinRecord):
    KIND = "track"
    _v7_off = 41

    @property
    def x1(self): return u2mm(self._get("i", 13))
    @property
    def y1(self): return u2mm(self._get("i", 17))
    @property
    def x2(self): return u2mm(self._get("i", 21))
    @property
    def y2(self): return u2mm(self._get("i", 25))
    @property
    def width(self): return u2mm(self._get("i", 29))
    @property
    def start(self): return (self.x1, self.y1)
    @property
    def end(self): return (self.x2, self.y2)

    def set_geometry(self, x1=None, y1=None, x2=None, y2=None, width=None):
        for off, v in ((13, x1), (17, y1), (21, x2), (25, y2), (29, width)):
            if v is not None:
                self._set("i", off, mm2u(v))

    @property
    def length(self):
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)


class Arc(BinRecord):
    KIND = "arc"
    _v7_off = 52

    @property
    def cx(self): return u2mm(self._get("i", 13))
    @property
    def cy(self): return u2mm(self._get("i", 17))
    @property
    def radius(self): return u2mm(self._get("i", 21))
    @property
    def start_angle(self): return self._get("d", 25)
    @property
    def end_angle(self): return self._get("d", 33)
    @property
    def width(self): return u2mm(self._get("i", 41))

    def set_geometry(self, cx=None, cy=None, radius=None, start_angle=None, end_angle=None,
                     width=None):
        for fmt, off, v, conv in (("i", 13, cx, mm2u), ("i", 17, cy, mm2u),
                                  ("i", 21, radius, mm2u), ("d", 25, start_angle, float),
                                  ("d", 33, end_angle, float), ("i", 41, width, mm2u)):
            if v is not None:
                self._set(fmt, off, conv(v))

    @property
    def sweep(self):
        s = (self.end_angle - self.start_angle) % 360.0
        return 360.0 if s == 0 and self.end_angle != self.start_angle else s

    def point(self, angle_deg):
        a = math.radians(angle_deg)
        return (self.cx + self.radius * math.cos(a), self.cy + self.radius * math.sin(a))

    @property
    def start(self): return self.point(self.start_angle)
    @property
    def end(self): return self.point(self.end_angle)


class Via(BinRecord):
    KIND = "via"
    _v7_off = 203

    @property
    def x(self): return u2mm(self._get("i", 13))
    @property
    def y(self): return u2mm(self._get("i", 17))
    @property
    def diameter(self): return u2mm(self._get("i", 21))
    @property
    def hole(self): return u2mm(self._get("i", 25))
    @property
    def low_layer(self): return self.subs[0][29]
    @property
    def high_layer(self): return self.subs[0][30]

    @property
    def layer(self):
        return L.MULTI

    # Simple / top-middle-bottom / full-stack mode sits at 74, right before a
    # 32-entry per-layer diameter table at 75..202 (all 802 fixture vias agree).
    @property
    def mode(self):
        return self.subs[0][74] if self._has(75 + 128, 0) else 0

    def diameter_on(self, layer):
        if self.mode == 0 or not self._has(75, 128) or not 1 <= layer <= 32:
            return self.diameter
        return u2mm(self._get("i", 75 + 4 * (layer - 1)))

    @property
    def polygon(self):
        return None

    def set_geometry(self, x=None, y=None, diameter=None, hole=None):
        if x is not None:
            self._set("i", 13, mm2u(x))
        if y is not None:
            self._set("i", 17, mm2u(y))
        if hole is not None:
            self._set("i", 25, mm2u(hole))
        if diameter is not None:
            self._set("i", 21, mm2u(diameter))
            if self._has(75, 128):
                for i in range(32):
                    self._set("i", 75 + 4 * i, mm2u(diameter))

    def spans(self, layer, order):
        """True when the via barrel reaches copper layer `layer` in `order`."""
        if layer not in order:
            return False
        lo, hi = self.low_layer, self.high_layer
        if lo not in order or hi not in order:
            return True
        a, b = sorted((order.index(lo), order.index(hi)))
        return a <= order.index(layer) <= b


PAD_SHAPES = {0: "unknown", 1: "round", 2: "rect", 3: "octagonal"}
PAD_ALT_ROUNDRECT = 9
HOLE_SHAPES = {0: "round", 1: "square", 2: "slot"}


class Pad(BinRecord):
    KIND = "pad"
    _main = 4
    _v7_off = 114

    @property
    def name(self):
        s = self.subs[0]
        return s[1:1 + s[0]].decode("cp1252", errors="replace") if s else ""

    def set_name(self, name):
        b = name.encode("cp1252")
        if len(b) > 255:
            raise FormatError("pad name longer than 255 bytes")
        self.subs[0] = bytearray([len(b)]) + b

    @property
    def x(self): return u2mm(self._get("i", 13, 4))
    @property
    def y(self): return u2mm(self._get("i", 17, 4))
    @property
    def top_size(self): return _xy(self, 21, 4)
    @property
    def mid_size(self): return _xy(self, 29, 4)
    @property
    def bottom_size(self): return _xy(self, 37, 4)
    @property
    def hole(self): return u2mm(self._get("i", 45, 4))
    @property
    def top_shape(self): return self.subs[4][49]
    @property
    def mid_shape(self): return self.subs[4][50]
    @property
    def bottom_shape(self): return self.subs[4][51]
    @property
    def rotation(self): return self._get("d", 52, 4)
    @property
    def plated(self): return bool(self.subs[4][60])
    @property
    def mode(self): return self.subs[4][62]
    @property
    def hole_rotation(self):
        return self._get("d", 106, 4) if self._has(106, 8, 4) else 0.0

    @property
    def polygon(self):
        return None

    @property
    def has_stack(self):
        return len(self.subs) > 5 and len(self.subs[5]) >= 596

    @property
    def hole_shape(self):
        return HOLE_SHAPES.get(self.subs[5][262], "round") if self.has_stack else "round"

    @property
    def slot_size(self):
        return u2mm(self._get("i", 263, 5)) if self.has_stack else 0.0

    @property
    def slot_rotation(self):
        return self._get("d", 267, 5) if self.has_stack else 0.0

    def corner_radius_pct(self, layer):
        if not self.has_stack:
            return 0
        i = 0 if layer == L.TOP else 31 if layer == L.BOTTOM else max(0, min(31, layer - 1))
        return self.subs[5][564 + i]

    def alt_shape(self, layer):
        if not self.has_stack:
            return 0
        i = 0 if layer == L.TOP else 31 if layer == L.BOTTOM else max(0, min(31, layer - 1))
        return self.subs[5][532 + i]

    @property
    def is_smd(self):
        return self.hole <= 0 and self.layer != L.MULTI

    def shape_on(self, layer):
        """(shape name, (sx, sy), corner fraction) on a copper layer.

        A round-rect's corner radius is fraction * min(sx, sy) / 2.
        """
        if self.mode == 0 or layer == L.TOP:
            shape, size = self.top_shape, self.top_size
        elif layer == L.BOTTOM:
            shape, size = self.bottom_shape, self.bottom_size
        else:
            shape, size = self.mid_shape, self.mid_size
            if self.mode == 2 and self.has_stack and 2 <= layer <= 30:
                i = layer - 2
                sx = u2mm(self._get("i", 4 * i, 5))
                sy = u2mm(self._get("i", 116 + 4 * i, 5))
                shape, size = self.subs[5][232 + i], (sx, sy)
        name = PAD_SHAPES.get(shape, "round")
        frac = 0.0
        if self.alt_shape(layer) == PAD_ALT_ROUNDRECT and name == "rect":
            name, frac = "roundrect", self.corner_radius_pct(layer) / 100.0
        return name, size, frac

    def on_layer(self, layer):
        if self.layer == L.MULTI:
            return L.is_copper(layer)
        return self.layer == layer

    def set_geometry(self, x=None, y=None, rotation=None):
        if x is not None:
            self._set("i", 13, mm2u(x), 4)
        if y is not None:
            self._set("i", 17, mm2u(y), 4)
        if rotation is not None:
            self._set("d", 52, float(rotation) % 360.0, 4)


class Fill(BinRecord):
    KIND = "fill"
    _v7_off = 42

    @property
    def x1(self): return u2mm(self._get("i", 13))
    @property
    def y1(self): return u2mm(self._get("i", 17))
    @property
    def x2(self): return u2mm(self._get("i", 21))
    @property
    def y2(self): return u2mm(self._get("i", 25))
    @property
    def rotation(self): return self._get("d", 29)

    def set_geometry(self, x1=None, y1=None, x2=None, y2=None, rotation=None):
        for fmt, off, v, conv in (("i", 13, x1, mm2u), ("i", 17, y1, mm2u), ("i", 21, x2, mm2u),
                                  ("i", 25, y2, mm2u), ("d", 29, rotation, float)):
            if v is not None:
                self._set(fmt, off, conv(v))

    def corners(self):
        cx, cy = (self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2
        hx, hy = abs(self.x2 - self.x1) / 2, abs(self.y2 - self.y1) / 2
        a = math.radians(self.rotation)
        c, s = math.cos(a), math.sin(a)
        return [(cx + dx * c - dy * s, cy + dx * s + dy * c)
                for dx, dy in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))]


KEEPOUT_VIA, KEEPOUT_TRACK, KEEPOUT_COPPER, KEEPOUT_SMD_PAD, KEEPOUT_TH_PAD = 1, 2, 4, 8, 16
KEEPOUT_ALL = 31

REGION_KINDS = {0: "copper", 1: "polygon_cutout", 2: "dashed_outline", 3: "unknown3",
                4: "cavity"}


class Region(BinRecord):
    """A region (Regions6, ShapeBasedRegions6, BoardRegions) or a body (bodies use the same layout)."""

    KIND = "region"
    __slots__ = ("extended", "_parsed")

    def __init__(self, doc, storage, index, rtype, subs, extended):
        super().__init__(doc, storage, index, rtype, subs)
        self.extended = extended
        self._parsed = None

    def _parse(self):
        if self._parsed is not None:
            return self._parsed
        s = self.subs[0]
        holes, = struct.unpack_from("<H", s, 14)
        plen, = struct.unpack_from("<I", s, 18)
        props = Props(bytes(s[22:22 + plen]))
        pos = 22 + plen
        verts = []
        if pos + 4 <= len(s):
            n, = struct.unpack_from("<I", s, pos)
            pos += 4
            if self.extended:
                for _ in range(n + 1):
                    if pos + 37 > len(s):
                        raise FormatError(f"{self.storage}[{self.index}]: vertex list truncated")
                    rnd = s[pos]
                    px, py, cx, cy, r = struct.unpack_from("<iiiii", s, pos + 1)
                    a1, a2 = struct.unpack_from("<dd", s, pos + 21)
                    verts.append((bool(rnd), u2mm(px), u2mm(py), u2mm(cx), u2mm(cy), u2mm(r),
                                  a1, a2))
                    pos += 37
            else:
                for _ in range(n):
                    x, y = struct.unpack_from("<dd", s, pos)
                    verts.append((False, u2mm(x), u2mm(y), 0.0, 0.0, 0.0, 0.0, 0.0))
                    pos += 16
        hole_list = []
        for _ in range(holes):
            if pos + 4 > len(s):
                raise FormatError(f"{self.storage}[{self.index}]: hole list truncated")
            m, = struct.unpack_from("<I", s, pos)
            pos += 4
            pts = []
            for _ in range(m):
                x, y = struct.unpack_from("<dd", s, pos)
                pts.append((u2mm(x), u2mm(y)))
                pos += 16
            hole_list.append(pts)
        self._parsed = (props, verts, hole_list, pos)
        return self._parsed

    @property
    def props(self):
        return self._parse()[0]

    @property
    def vertices(self):
        return self._parse()[1]

    @property
    def holes(self):
        return self._parse()[2]

    @property
    def tail(self):
        """Bytes after the hole lists (normally none)."""
        return bytes(self.subs[0][self._parse()[3]:])

    @property
    def layer(self):
        v = self.props.get("V7_LAYER")
        if v:
            try:
                return L.parse(v)
            except ValueError:
                pass
        return self.subs[0][0]

    @property
    def kind(self):
        k = int(parse_float(self.props.get("KIND", "0")))
        if k == 0 and parse_bool(self.props.get("ISBOARDCUTOUT")):
            return "board_cutout"
        return REGION_KINDS.get(k, f"kind{k}")

    @property
    def is_board_cutout(self):
        return parse_bool(self.props.get("ISBOARDCUTOUT"))

    @property
    def keepout_restrictions(self):
        if not self.is_keepout and self.layer != L.KEEPOUT:
            return 0
        v = int(parse_float(self.props.get("KEEPOUTRESTRICTIONS"), KEEPOUT_ALL))
        return v or KEEPOUT_ALL

    def outline(self, arc_step_deg=5.0):
        """Closed outline points (mm). A Regions6 record borrows its
        ShapeBasedRegions6 twin's vertices, which keep arcs."""
        return vertices_to_points(self.twin_vertices(), arc_step_deg)

    def twin_vertices(self):
        if not self.extended:
            twin = {"Regions6": "ShapeBasedRegions6",
                    "ComponentBodies6": "ShapeBasedComponentBodies6"}.get(self.storage)
            st = self.doc.storages.get(twin) if twin else None
            if st is not None and self.index < len(st.records):
                return st.records[self.index].vertices
        return self.vertices

    def set_shape(self, outline, holes=()):
        """Replace the outline (list of (x, y) mm) and holes; arcs become lines."""
        props, _v, _h, end = self._parse()
        s = self.subs[0]
        head = bytearray(s[:18])
        struct.pack_into("<H", head, 14, len(holes))
        body = bytearray()
        pb = props.text_bytes()
        body += struct.pack("<I", len(pb)) + pb
        if self.extended:
            body += struct.pack("<I", len(outline))
            pts = list(outline) + [outline[0]]
            for x, y in pts:
                body += bytes([0]) + struct.pack("<iiiii", mm2u(x), mm2u(y), 0, 0, 0)
                body += struct.pack("<dd", 0.0, 0.0)
        else:
            body += struct.pack("<I", len(outline))
            for x, y in outline:
                body += struct.pack("<dd", float(mm2u(x)), float(mm2u(y)))
        for h in holes:
            body += struct.pack("<I", len(h))
            for x, y in h:
                body += struct.pack("<dd", float(mm2u(x)), float(mm2u(y)))
        self.subs[0] = head + body + bytearray(s[end:])
        self._parsed = None


class Text(BinRecord):
    KIND = "text"

    @property
    def x(self): return u2mm(self._get("i", 13))
    @property
    def y(self): return u2mm(self._get("i", 17))
    @property
    def height(self): return u2mm(self._get("i", 21))
    @property
    def rotation(self): return self._get("d", 27)
    @property
    def mirrored(self): return bool(self.subs[0][35])
    @property
    def stroke_width(self): return u2mm(self._get("i", 36))

    @property
    def is_comment(self):
        return self._has(41, 1) and bool(self.subs[0][40])

    @property
    def is_designator(self):
        return self._has(42, 1) and bool(self.subs[0][41])

    # newer records (layout as KiCad's importer reads it): font, frame, justification
    @property
    def font_type(self):
        """'stroke', 'truetype' or 'barcode'."""
        v = self.subs[0][43] if self._has(43, 1) else 0
        return {0: "stroke", 1: "truetype", 2: "barcode"}.get(v, "stroke")

    @property
    def frame(self):
        """(width, height, margin) of the text frame, or None."""
        if not self._has(132, 1):
            return None
        w, h = u2mm(self._get("i", 124)), u2mm(self._get("i", 128))
        on = bool(self.subs[0][230]) if self._has(230, 1) else (w > 0 and h > 0)
        return (w, h, u2mm(self._get("i", 111))) if on and w > 0 and h > 0 else None

    @property
    def justification(self):
        """Where text sits in its frame: 1-9 = left/center/right x top/center/
        bottom (3 = left bottom, the default when not stored)."""
        if self._has(240, 1) and self.subs[0][240]:
            return self.subs[0][132] or 3
        return 3

    @property
    def hidden(self):
        """A designator or comment its component does not show (NAMEON or
        COMMENTON false): Altium neither prints nor checks it."""
        c = self.component
        if c is None or c >= len(self.doc.components):
            return False
        props = self.doc.components[c].props
        if self.is_designator:
            return str(props.get("NAMEON", "TRUE")).upper() == "FALSE"
        if self.is_comment:
            return str(props.get("COMMENTON", "TRUE")).upper() == "FALSE"
        return False

    @property
    def widestring_index(self):
        return self._get("I", 115) if self._has(115, 4) else None

    @property
    def raw_text(self):
        s = self.subs[1] if len(self.subs) > 1 else b""
        return s[1:1 + s[0]].decode("cp1252", errors="replace") if s else ""

    @property
    def text(self):
        i = self.widestring_index
        ws = self.doc.widestrings
        if i is not None and i in ws:
            return ws[i]
        return self.raw_text.replace("\r\n", "\n")

    @property
    def net(self):
        return None

    @property
    def polygon(self):
        return None

    def set_geometry(self, x=None, y=None, height=None, rotation=None):
        for fmt, off, v, conv in (("i", 13, x, mm2u), ("i", 17, y, mm2u), ("i", 21, height, mm2u),
                                  ("d", 27, rotation, float)):
            if v is not None:
                self._set(fmt, off, conv(v))


class Body(Region):
    KIND = "body"

    @property
    def model_id(self):
        return self.props.get("MODELID", "")

    @property
    def standoff(self):
        return parse_length(self.props.get("STANDOFFHEIGHT"))

    @property
    def overall_height(self):
        return parse_length(self.props.get("OVERALLHEIGHT"))


def vertices_to_points(verts, arc_step_deg=5.0):
    """Altium outline vertices -> closed polyline points (mm), arcs approximated.

    A round vertex is an arc around (cx, cy) from start angle to end angle;
    which end it starts from is decided by which arc end lies nearer the
    vertex position, as Altium does.
    """
    pts = []
    for (rnd, x, y, cx, cy, r, a1, a2) in verts:
        if not rnd or r <= 0:
            pts.append((x, y))
            continue
        s1 = (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1)))
        s2 = (cx + r * math.cos(math.radians(a2)), cy + r * math.sin(math.radians(a2)))
        sweep = (a2 - a1) % 360.0 or 360.0
        n = max(2, int(math.ceil(sweep / arc_step_deg)) + 1)
        if math.dist(s1, (x, y)) <= math.dist(s2, (x, y)):
            angs = [a1 + sweep * i / (n - 1) for i in range(n)]
        else:
            angs = [a2 - sweep * i / (n - 1) for i in range(n)]
        pts.extend((cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
                   for a in angs)
    cleaned = []
    for p in pts:
        if not cleaned or math.dist(p, cleaned[-1]) > 1e-9:
            cleaned.append(p)
    if len(cleaned) > 1 and math.dist(cleaned[0], cleaned[-1]) <= 1e-9:
        cleaned.pop()
    return cleaned


def props_vertices(props):
    """Outline vertices from KIND#/VX#/VY#/CX#/CY#/SA#/EA#/R# properties."""
    out = []
    i = 0
    while props.get(f"VX{i}") is not None and props.get(f"VY{i}") is not None:
        out.append((int(parse_float(props.get(f"KIND{i}", "0"))) != 0,
                    parse_length(props.get(f"VX{i}")), parse_length(props.get(f"VY{i}")),
                    parse_length(props.get(f"CX{i}")), parse_length(props.get(f"CY{i}")),
                    parse_length(props.get(f"R{i}")),
                    parse_float(props.get(f"SA{i}")), parse_float(props.get(f"EA{i}"))))
        i += 1
    return out


def set_props_vertices(props, points):
    """Replace a props outline with straight segments through `points` (mm)."""
    i = 0
    while props.get(f"VX{i}") is not None:
        for k in ("KIND", "VX", "VY", "CX", "CY", "SA", "EA", "R"):
            props.remove(f"{k}{i}")
        i += 1
    for i, (x, y) in enumerate(points):
        props.set(f"KIND{i}", "0")
        props.set(f"VX{i}", fmt_mil(x))
        props.set(f"VY{i}", fmt_mil(y))
        props.set(f"CX{i}", "0mil")
        props.set(f"CY{i}", "0mil")
        props.set(f"SA{i}", fmt_float(0.0))
        props.set(f"EA{i}", fmt_float(0.0))
        props.set(f"R{i}", "0mil")


# ------------------------------------------------------------------ text-record views

class Net:
    __slots__ = ("index", "props")

    def __init__(self, index, props):
        self.index, self.props = index, props

    @property
    def name(self):
        return self.props.text("NAME")

    def __repr__(self):
        return f"<Net {self.index} {self.name}>"


class Component:
    __slots__ = ("index", "props")

    def __init__(self, index, props):
        self.index, self.props = index, props

    @property
    def designator(self):
        return self.props.text("SOURCEDESIGNATOR")

    @property
    def pattern(self):
        return self.props.text("PATTERN")

    @property
    def x(self):
        return parse_length(self.props.get("X"))

    @property
    def y(self):
        return parse_length(self.props.get("Y"))

    @property
    def rotation(self):
        return parse_float(self.props.get("ROTATION"))

    @property
    def layer(self):
        try:
            return L.parse(self.props.get("LAYER", "TOP"))
        except ValueError:
            return L.TOP

    @property
    def side(self):
        return "bottom" if self.layer == L.BOTTOM else "top"

    @property
    def unique_id(self):
        return self.props.get("UNIQUEID", "")

    @property
    def source_unique_id(self):
        return self.props.get("SOURCEUNIQUEID", "")

    @property
    def comment(self):
        return self.props.text("COMMENT") or self.props.text("SOURCELIBREFERENCE")

    @property
    def height(self):
        return parse_length(self.props.get("HEIGHT"))

    def __repr__(self):
        return f"<Component {self.index} {self.designator} {self.pattern}>"


HATCH = {"solid": "solid", "45degree": "45", "90degree": "90", "horizontal": "horizontal",
         "vertical": "vertical", "none": "none"}


class Polygon:
    __slots__ = ("index", "props")

    def __init__(self, index, props):
        self.index, self.props = index, props

    @property
    def net(self):
        v = self.props.get("NET")
        if v is None or v.strip() == "":
            return None
        n = int(parse_float(v, -1))
        return None if n < 0 or n == NONE16 else n

    @property
    def layer(self):
        try:
            return L.parse(self.props.get("LAYER", "TOP"))
        except ValueError:
            return L.TOP

    @property
    def name(self):
        return self.props.text("NAME")

    @property
    def kind(self):
        return self.props.get("POLYGONTYPE", "Polygon")

    @property
    def pour_index(self):
        return int(parse_float(self.props.get("POURINDEX", "0")))

    @property
    def hatch(self):
        return HATCH.get(self.props.get("HATCHSTYLE", "Solid").lower(), "solid")

    @property
    def vertices(self):
        return props_vertices(self.props)

    def outline(self, arc_step_deg=5.0):
        return vertices_to_points(self.vertices, arc_step_deg)

    @property
    def settings(self):
        p = self.props
        return {
            "hatch": self.hatch,
            "grid": parse_length(p.get("GRIDSIZE")),
            "track_width": parse_length(p.get("TRACKWIDTH")),
            "min_prim_length": parse_length(p.get("MINPRIMLENGTH")),
            "pour_over_same_net": parse_bool(p.get("POUROVER")),
            "remove_dead": parse_bool(p.get("REMOVEDEAD")),
            "remove_islands_by_area": parse_bool(p.get("REMOVEISLANDSBYAREA")),
            "island_area_mm2": parse_float(p.get("AREATHRESHOLD")) * MM_PER_UNIT ** 2,
            "remove_necks": parse_bool(p.get("REMOVENECKS")),
            "neck_width": parse_length(p.get("NECKWIDTHTHRESHOLD")),
            "arc_resolution": parse_length(p.get("ARCRESOLUTION")),
            "use_octagons": parse_bool(p.get("USEOCTAGONS")),
            "shelved": parse_bool(p.get("SHELVED")),
            "pour_index": self.pour_index,
            "kind": self.kind,
        }

    def __repr__(self):
        return f"<Polygon {self.index} {self.kind} layer={L.name(self.layer)} net={self.net}>"


CLASS_KINDS = {0: "net", 1: "component", 2: "from_to", 3: "pad", 4: "layer", 6: "diff_pair",
               7: "polygon"}


class NetClass:
    __slots__ = ("index", "props")

    def __init__(self, index, props):
        self.index, self.props = index, props

    @property
    def name(self):
        return self.props.text("NAME")

    @property
    def kind(self):
        return CLASS_KINDS.get(int(parse_float(self.props.get("KIND", "0"))), "other")

    @property
    def superclass(self):
        return parse_bool(self.props.get("SUPERCLASS"))

    @property
    def members(self):
        out, i = [], 0
        while self.props.get(f"M{i}") is not None:
            out.append(self.props.text(f"M{i}"))
            i += 1
        return out

    def __repr__(self):
        return f"<Class {self.kind} {self.name} ({len(self.members)})>"


class Rule:
    __slots__ = ("index", "props")

    def __init__(self, index, props):
        self.index, self.props = index, props

    @property
    def kind(self):
        return self.props.get("RULEKIND", "")

    @property
    def name(self):
        return self.props.text("NAME")

    @property
    def enabled(self):
        return parse_bool(self.props.get("ENABLED"), True)

    @property
    def priority(self):
        return int(parse_float(self.props.get("PRIORITY", "1")))

    @property
    def scope1(self):
        return self.props.get("SCOPE1EXPRESSION", "All")

    @property
    def scope2(self):
        return self.props.get("SCOPE2EXPRESSION", "All")

    def length(self, key, default=0.0):
        return parse_length(self.props.get(key), default)

    def __repr__(self):
        return f"<Rule {self.kind} {self.name!r} p{self.priority}>"


# ------------------------------------------------------------------ storages

BINARY = {
    "Arcs6": (Arc, 1, 1), "Pads6": (Pad, 2, 6), "Vias6": (Via, 3, 1), "Tracks6": (Track, 4, 1),
    "Texts6": (Text, 5, 2), "Fills6": (Fill, 6, 1), "Regions6": (Region, 11, 1),
    "ShapeBasedRegions6": (Region, 11, 1), "BoardRegions": (Region, 11, 1),
    "ComponentBodies6": (Body, 12, 1), "ShapeBasedComponentBodies6": (Body, 12, 1),
}
EXTENDED = {"ShapeBasedRegions6", "ShapeBasedComponentBodies6"}
PROPS = {
    "Board6": 0, "Nets6": 0, "Components6": 0, "Polygons6": 0, "Classes6": 0, "Rules6": 2,
    "Dimensions6": 2, "UniqueIDPrimitiveInformation": 0, "PrimitiveParameters": 0,
    "DifferentialPairs6": 0, "SmartUnions": 0, "SignalClasses": 0,
    "ExtendedPrimitiveInformation": 0, "CustomMaskShapes": 0, "SharedUnion": 0,
}
# Fixed-size binary tables keyed by object index (see PCBDOC_FORMAT.md).
TABLES = {"PrimitiveGuids": 24, "PadViaCacheLibraryLinksSection": 10}


def is_violation_storage(name):
    """Cached DRC results (TClearanceViolation, ...): they cite objects by index."""
    return name.startswith("T") and "Viola" in name


class Storage:
    """One top-level storage: its Header and Data streams and parsed records."""

    def __init__(self, name, kind, header, records, raw):
        self.name = name
        self.kind = kind          # "binary" | "props" | "widestrings"
        self.header = header      # bytes of <name>/Header, or None
        self.records = records
        self.raw = raw            # original Data bytes
        self.header_is_count = (header is not None and len(header) == 4 and
                                struct.unpack("<I", header)[0] == len(records))

    def encode_data(self):
        if self.kind == "table":
            return b"".join(bytes(r) for r in self.records)
        return b"".join(r.encode() for r in self.records)


def split_binary(data, nsub, name):
    out, pos = [], 0
    while pos < len(data):
        rtype = data[pos]
        pos += 1
        subs = []
        for _ in range(nsub):
            if pos + 4 > len(data):
                raise FormatError(f"{name}: record header truncated")
            n, = struct.unpack_from("<I", data, pos)
            pos += 4
            if pos + n > len(data):
                raise FormatError(f"{name}: sub-record runs past the stream end")
            subs.append(bytearray(data[pos:pos + n]))
            pos += n
        out.append((rtype, subs))
    return out


def parse_widestrings(data):
    """WideStrings6: [u32 index][u32 byte length][UTF-16 text + NUL]...

    An empty string is written with length 2 and no bytes at all, as seen in
    every fixture that has one.
    """
    out, pos = {}, 0
    order = []
    while pos + 8 <= len(data):
        idx, n = struct.unpack_from("<II", data, pos)
        pos += 8
        take = 0 if n == 2 else n
        if pos + take > len(data):
            raise FormatError("WideStrings6: entry runs past the stream end")
        out[idx] = data[pos:pos + take].decode("utf-16-le", errors="replace").rstrip("\0")
        order.append(idx)
        pos += take
    if pos != len(data):
        raise FormatError("WideStrings6: trailing bytes")
    return out, order


def encode_widestrings(table, order):
    out = bytearray()
    for idx in order:
        s = table[idx]
        if s == "":
            out += struct.pack("<II", idx, 2)
        else:
            b = (s + "\0").encode("utf-16-le")
            out += struct.pack("<II", idx, len(b)) + b
    return bytes(out)


class PcbDoc:
    def __init__(self, cf, path=None):
        self.cf = cf
        self.path = Path(path) if path else None
        self.streams = cf.streams()
        self.storages = {}
        self.unparsed = []
        self._parse()

    @classmethod
    def open(cls, path):
        return cls(CompoundFile.open(path), path)

    # ------------------------------------------------------------ parsing
    def _stream(self, path):
        for k, v in self.streams.items():
            if k.lower() == path.lower():
                return v
        return None

    def _parse(self):
        top = {}
        for path in self.streams:
            head, _, rest = path.partition("/")
            top.setdefault(head, []).append(rest)
        for name, parts in top.items():
            data = self._stream(f"{name}/Data")
            header = self._stream(f"{name}/Header")
            if data is None:
                continue
            if name in BINARY:
                cls, rtype, nsub = BINARY[name]
                recs = []
                for i, (t, subs) in enumerate(split_binary(data, nsub, name)):
                    if t != rtype:
                        raise FormatError(f"{name}[{i}]: record type {t}, expected {rtype}")
                    if cls in (Region, Body):
                        recs.append(cls(self, name, i, t, subs, name in EXTENDED))
                    else:
                        recs.append(cls(self, name, i, t, subs))
                self.storages[name] = Storage(name, "binary", header, recs, data)
            elif name in PROPS:
                recs = split_props(data, PROPS[name])
                self.storages[name] = Storage(name, "props", header, recs, data)
            elif name in TABLES:
                size = TABLES[name]
                if len(data) % size:
                    raise FormatError(f"{name}: {len(data)} bytes is not a whole number of "
                                      f"{size}-byte entries")
                recs = [bytearray(data[i:i + size]) for i in range(0, len(data), size)]
                self.storages[name] = Storage(name, "table", header, recs, data)
            elif is_violation_storage(name):
                self.storages[name] = Storage(name, "props", header, split_props(data), data)
            elif name == "WideStrings6":
                table, order = parse_widestrings(data)
                self.widestring_order = order
                st = Storage(name, "widestrings", header, order, data)
                self.storages[name] = st
                self.widestrings = table
            else:
                self.unparsed.append(name)
        if not hasattr(self, "widestrings"):
            self.widestrings, self.widestring_order = {}, []
        for name in ("Regions6", "ComponentBodies6"):
            twin = "ShapeBased" + name
            if name in self.storages and twin in self.storages:
                a, b = self.storages[name].records, self.storages[twin].records
                if len(a) != len(b):
                    raise FormatError(f"{name} has {len(a)} records but {twin} has {len(b)}")
        self._index()

    def _index(self):
        p = lambda n: self.storages[n].records if n in self.storages else []
        self.nets = [Net(i, r) for i, r in enumerate(p("Nets6"))]
        self.components = [Component(i, r) for i, r in enumerate(p("Components6"))]
        self.polygons = [Polygon(i, r) for i, r in enumerate(p("Polygons6"))]
        self.classes = [NetClass(i, r) for i, r in enumerate(p("Classes6"))]
        self.rules = [Rule(i, r) for i, r in enumerate(p("Rules6"))]
        self.tracks = p("Tracks6")
        self.arcs = p("Arcs6")
        self.vias = p("Vias6")
        self.pads = p("Pads6")
        self.fills = p("Fills6")
        self.regions = p("Regions6")
        self.shape_regions = p("ShapeBasedRegions6")
        self.texts = p("Texts6")
        self.bodies = p("ComponentBodies6")
        self.shape_bodies = p("ShapeBasedComponentBodies6")
        self.board_regions = p("BoardRegions")
        board = p("Board6")
        self.board_props = board[0] if board else Props.new([])
        self._net_by_name = {n.name.upper(): n for n in self.nets}
        self._comp_by_des = {c.designator.upper(): c for c in self.components}
        self._comp_dups = {}
        for c in self.components:
            self._comp_dups.setdefault(c.designator.upper(), []).append(c.index)
        self._comp_dups = {k: v for k, v in self._comp_dups.items() if len(v) > 1}

    def invalidate(self):
        """Forget cached derived data after an edit."""
        self._cache = {}

    def _cached(self, key, fn):
        c = self.__dict__.setdefault("_cache", {})
        if key not in c:
            c[key] = fn()
        return c[key]

    def reindex(self):
        """Renumber record indices after records were added or removed."""
        self.invalidate()
        for st in self.storages.values():
            if st.kind == "binary":
                for i, r in enumerate(st.records):
                    r.index = i
        self._index()

    # ------------------------------------------------------------ lookups
    def net(self, name):
        n = self._net_by_name.get(str(name).upper())
        if n is None:
            raise KeyError(f"no net named {name!r}")
        return n

    def net_index(self, name):
        return self.net(name).index

    def has_net(self, name):
        return str(name).upper() in self._net_by_name

    def component(self, designator):
        key = str(designator).upper()
        c = self._comp_by_des.get(key)
        if c is None:
            raise KeyError(f"no component {designator!r}")
        if key in self._comp_dups:
            raise KeyError(f"designator {designator!r} is used by {len(self._comp_dups[key])} "
                           f"components (indexes {self._comp_dups[key]}); annotate the board "
                           "so each is unique, or refer to one by index")
        return c

    @property
    def duplicate_designators(self):
        """{designator: [component indexes]} for non-empty designators used more
        than once (unnamed parts are common and not reported; a lookup by an
        empty name still refuses)."""
        return {self.components[v[0]].designator: list(v) for k, v in self._comp_dups.items()
                if k.strip()}

    def has_component(self, designator):
        return str(designator).upper() in self._comp_by_des

    @property
    def origin(self):
        return (parse_length(self.board_props.get("ORIGINX")),
                parse_length(self.board_props.get("ORIGINY")))

    @property
    def layer_stack(self):
        """{layer: {"name", "next", "prev", "copper_mm", "diel_mm", "diel_const"}}."""
        return self._cached("layer_stack", self._layer_stack)

    def _layer_stack(self):
        out = {}
        p = self.board_props
        for i in range(1, 83):
            nm = p.get(f"LAYER{i}NAME")
            if nm is None:
                continue
            out[i] = {
                "name": nm,
                "next": int(parse_float(p.get(f"LAYER{i}NEXT", "0"))),
                "prev": int(parse_float(p.get(f"LAYER{i}PREV", "0"))),
                "copper_mm": parse_length(p.get(f"LAYER{i}COPTHICK"), 0.03556),
                "diel_mm": parse_length(p.get(f"LAYER{i}DIELHEIGHT"), 0.0),
                "diel_const": parse_float(p.get(f"LAYER{i}DIELCONST"), 4.5),
                "diel_material": p.get(f"LAYER{i}DIELMATERIAL", "").strip(),
            }
        return out

    @property
    def copper_layers(self):
        """Copper layers in physical order, top first."""
        return list(self._cached("copper_layers", self._copper_layers))

    def _copper_layers(self):
        stack = self.layer_stack
        order = L.copper_order(stack)
        used = set()
        for coll in (self.tracks, self.arcs, self.fills, self.regions):
            for r in coll:
                if L.is_copper(r.layer):
                    used.add(r.layer)
        for pd in self.pads:
            if L.is_copper(pd.layer):
                used.add(pd.layer)
        missing = sorted(used - set(order))
        if missing:
            order = [order[0]] + [m for m in missing if m != L.BOTTOM] + order[1:]
        return order

    def copper_thickness(self, layer):
        st = self.layer_stack.get(layer)
        return st["copper_mm"] if st else 0.03556

    @property
    def thickness(self):
        """Board thickness from the layer stack (mm)."""
        total, stack = 0.0, self.layer_stack
        for lay in self.copper_layers:
            st = stack.get(lay, {})
            total += st.get("copper_mm", 0.0)
            if lay != L.BOTTOM:
                total += st.get("diel_mm", 0.0)
        return total or 1.6

    def outline_vertices(self):
        v = props_vertices(self.board_props)
        if v:
            return v
        for r in self.board_regions:
            return r.vertices
        return []

    def outline(self, arc_step_deg=5.0):
        """Board outline as a closed list of (x, y) mm points."""
        return vertices_to_points(self.outline_vertices(), arc_step_deg)

    def board_cutouts(self, arc_step_deg=5.0):
        out = []
        for r in self.shape_regions or self.regions:
            if r.is_board_cutout:
                out.append(r.outline(arc_step_deg))
        return out

    # ------------------------------------------------------------ groups
    def primitives(self, kinds=("track", "arc", "via", "pad", "fill", "region", "text")):
        for kind in kinds:
            for r in {"track": self.tracks, "arc": self.arcs, "via": self.vias, "pad": self.pads,
                      "fill": self.fills, "region": self.regions, "text": self.texts,
                      "body": self.bodies}[kind]:
                yield r

    def copper(self, layer=None, net=None):
        """Copper primitives (tracks, arcs, vias, pads, fills, regions) on a layer/net."""
        for r in self.primitives(("track", "arc", "via", "pad", "fill", "region")):
            if r.KIND == "region" and r.kind not in ("copper",):
                continue
            if r.KIND in ("track", "arc", "fill", "region") and (
                    r.is_keepout or not L.is_signal(r.layer)):
                continue  # plane layers are negative: anything drawn there removes copper
            if r.KIND == "pad" and not (r.layer == L.MULTI or L.is_copper(r.layer)):
                continue
            if layer is not None:
                if r.KIND == "via":
                    if not r.spans(layer, self.copper_layers):
                        continue
                elif r.KIND == "pad":
                    if not r.on_layer(layer):
                        continue
                elif r.layer != layer:
                    continue
            if net is not None and r.effective_net != net:
                continue
            yield r

    def plane_net(self, layer):
        """Net index a whole internal plane carries, or None ('(No Net)',
        '(Multiple Nets)' for split planes, or unknown)."""
        if not L.is_plane(layer):
            return None
        name = self.board_props.get(f"PLANE{layer - 38}NETNAME", "")
        return self.net(name).index if name and self.has_net(name) else None

    def plane_areas(self, layer):
        """[(polygon index or None, net, outline points)] of copper on a plane layer.

        Split planes are Polygons6 records of type 'Split Plane'. A plane with
        none carries its single net over the whole board.
        """
        out = [(p.index, p.net, p.outline()) for p in self.polygons
               if p.layer == layer and "split" in p.kind.lower()]
        if not out:
            n = self.plane_net(layer)
            if n is not None:
                out.append((None, n, self.outline()))
        return out

    def component_primitives(self, comp_index):
        for st in self.storages.values():
            if st.kind != "binary":
                continue
            for r in st.records:
                if r.component == comp_index:
                    yield r

    def poured(self, polygon_index):
        """Pour copper (regions, tracks, arcs) owned by a polygon."""
        for coll in (self.regions, self.tracks, self.arcs):
            for r in coll:
                if r.polygon == polygon_index:
                    yield r

    @property
    def embedded_boards(self):
        """Boards placed on this one by reference (a panel): [{document, x, y,
        rows, cols, row_spacing, col_spacing, rotation}] in mm. Their copper
        is in their own files, not in this document."""
        return self._cached("embedded_boards", self._embedded_boards)

    def _embedded_boards(self):
        if not self.cf.exists("EmbeddedBoards6/Data"):
            return []
        out = []
        for r in split_props(self.cf.read("EmbeddedBoards6/Data")):
            out.append({"document": r.text("DOCUMENTPATH"),
                        "x": parse_length(r.get("X"), 0.0), "y": parse_length(r.get("Y"), 0.0),
                        "rows": int(parse_float(r.get("ROWCOUNT", "1"))),
                        "cols": int(parse_float(r.get("COLCOUNT", "1"))),
                        "row_spacing": parse_length(r.get("ROWSPACING"), 0.0),
                        "col_spacing": parse_length(r.get("COLSPACING"), 0.0),
                        "rotation": parse_float(r.get("ROTATION"), 0.0)})
        return out

    def panel_warning(self):
        """A sentence for reports when this board embeds others, else None."""
        eb = self.embedded_boards
        if not eb:
            return None
        n = sum(max(1, e["rows"]) * max(1, e["cols"]) for e in eb)
        docs = sorted({e["document"] for e in eb})
        return (f"this is a panel: {n} embedded board instance(s) of {', '.join(docs)}; their "
                "copper lives in those files and is not part of this analysis")

    def board_warnings(self):
        """Sentences every report should carry: a panel, duplicate designators."""
        out = []
        w = self.panel_warning()
        if w:
            out.append(w)
        dups = self.duplicate_designators
        if dups:
            out.append(f"{len(dups)} designator(s) used by more than one component ("
                       + ", ".join(sorted(dups)[:10]) + (", ..." if len(dups) > 10 else "")
                       + "); tools refuse to act on them by name")
        return out

    def summary(self):
        return {
            "duplicate_designators": sorted(self.duplicate_designators),
            "embedded_boards": len(self.embedded_boards),
            "file": self.path.name if self.path else None,
            "nets": len(self.nets), "components": len(self.components),
            "tracks": len(self.tracks), "arcs": len(self.arcs), "vias": len(self.vias),
            "pads": len(self.pads), "fills": len(self.fills), "regions": len(self.regions),
            "texts": len(self.texts), "polygons": len(self.polygons), "rules": len(self.rules),
            "classes": len(self.classes), "copper_layers": [L.name(x) for x in self.copper_layers],
            "origin_mm": self.origin, "unparsed_storages": sorted(self.unparsed),
        }
