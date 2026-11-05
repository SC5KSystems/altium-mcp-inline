"""Construct new binary records for a board.

Each constructor clones a record of the same kind from the board itself when
it has one (so undecoded bytes match that file's version), else a built-in
template, then resets every field it knows: layer, flags, net, owner indices,
geometry, union/sub-polygon indices and the V7 layer id. Records come back
unattached; pcbwrite.add() appends them and keeps the indices consistent.
"""

import collections
import math
import random
import string
import struct

from . import layers as L
from .pcbdoc import (Arc, Fill, FormatError, NONE16, Pad, Props, Region, Text, Track, Via,
                     mm2u, v7_for)
from .templates import TEMPLATES

KINDS = {"track": ("Tracks6", Track), "arc": ("Arcs6", Arc), "via": ("Vias6", Via),
         "pad": ("Pads6", Pad), "fill": ("Fills6", Fill), "text": ("Texts6", Text)}


def _template(doc, kind):
    storage, cls = KINDS[kind]
    st = doc.storages.get(storage)
    if st is not None and st.records:
        sig = collections.Counter(tuple(len(s) for s in r.subs) for r in st.records).most_common(1)[0][0]
        for r in st.records:
            if tuple(len(s) for s in r.subs) == sig:
                return r.rtype, [bytearray(s) for s in r.subs]
    rtype, subs = TEMPLATES[kind]
    return rtype, [bytearray.fromhex(s) for s in subs]


def _new(doc, kind):
    storage, cls = KINDS[kind]
    rtype, subs = _template(doc, kind)
    return cls(doc, storage, -1, rtype, subs)


def _common(rec, layer, net, component=None, polygon=None, sub=0):
    s = rec.subs[sub]
    s[1] = (s[1] | 0x04) & ~0x03       # unlocked, not a polygon outline, not selected
    s[2] = 0                            # not a keepout
    struct.pack_into("<HHH", s, 3, NONE16 if net is None else net,
                     NONE16 if polygon is None else polygon,
                     NONE16 if component is None else component)
    rec.set_layer(layer)


def _net_index(doc, net):
    if net is None or isinstance(net, int):
        return net
    return doc.net_index(net)


def _comp_index(doc, comp):
    if comp is None or isinstance(comp, int):
        return comp
    return doc.component(comp).index


def track(doc, layer, x1, y1, x2, y2, width, net=None, component=None, polygon=None):
    t = _new(doc, "track")
    _common(t, L.parse(layer), _net_index(doc, net), _comp_index(doc, component), polygon)
    t.set_geometry(x1, y1, x2, y2, width)
    s = t.subs[0]
    struct.pack_into("<H", s, 33, 0)
    if len(s) > 36:
        s[36] = 0
    return t


def arc(doc, layer, cx, cy, radius, start_angle, end_angle, width, net=None, component=None,
        polygon=None):
    a = _new(doc, "arc")
    _common(a, L.parse(layer), _net_index(doc, net), _comp_index(doc, component), polygon)
    a.set_geometry(cx, cy, radius, start_angle % 360.0, end_angle % 360.0, width)
    s = a.subs[0]
    struct.pack_into("<H", s, 45, 0)
    if len(s) > 48:
        s[48] = 0
    return a


def via(doc, x, y, diameter, hole, net=None, low=L.TOP, high=L.BOTTOM):
    if hole >= diameter:
        raise FormatError("via hole must be smaller than its diameter")
    v = _new(doc, "via")
    s = v.subs[0]
    s[0] = L.MULTI
    s[1] = (s[1] | 0x04) & ~0x03
    s[2] = s[2] & ~0x02
    struct.pack_into("<H", s, 3, NONE16 if net is None else _net_index(doc, net))
    s[29], s[30] = L.parse(low), L.parse(high)
    if len(s) > 75 + 128:
        s[74] = 0
    v.set_geometry(x, y, diameter, hole)
    if len(s) >= 207:
        struct.pack_into("<I", s, 203, v7_for(L.MULTI))
    return v


SHAPES = {"round": 1, "circle": 1, "rect": 2, "rectangle": 2, "octagonal": 3, "octagon": 3,
          "roundrect": 2}


def pad(doc, name, x, y, size_x, size_y, layer=L.TOP, shape="rect", hole=0.0, rotation=0.0,
        net=None, component=None, plated=True, corner_pct=0):
    shape = shape.lower()
    if shape not in SHAPES:
        raise FormatError(f"unknown pad shape {shape!r}")
    layer = L.parse(layer)
    if hole > 0:
        layer = L.MULTI
        # a plated hole needs a ring; an unplated one may be the pad's size
        # (Altium's usual non-plated mounting hole)
        if hole > min(size_x, size_y) or (plated and hole >= min(size_x, size_y)):
            raise FormatError("pad hole must be smaller than the pad"
                              + ("" if plated else " (or equal, when unplated)"))
    p = _new(doc, "pad")
    p.set_name(str(name))
    s = p.subs[4]
    s[0] = layer
    s[1] = (s[1] | 0x04) & ~0x03
    s[2] = 0
    struct.pack_into("<H", s, 3, NONE16 if net is None else _net_index(doc, net))
    struct.pack_into("<H", s, 7, NONE16 if component is None else _comp_index(doc, component))
    struct.pack_into("<ii", s, 13, mm2u(x), mm2u(y))
    for off in (21, 29, 37):
        struct.pack_into("<ii", s, off, mm2u(size_x), mm2u(size_y))
    struct.pack_into("<i", s, 45, mm2u(hole))
    code = SHAPES[shape]
    s[49] = s[50] = s[51] = code
    struct.pack_into("<d", s, 52, float(rotation) % 360.0)
    s[60] = 1 if (plated and hole > 0) or hole == 0 else 0
    s[62] = 0
    if len(s) >= 114:
        struct.pack_into("<d", s, 106, 0.0)
    if len(s) >= 118:
        struct.pack_into("<I", s, 114, v7_for(layer))
    if shape == "roundrect":
        p.subs[5] = _stack_record(size_x, size_y, max(1, min(100, int(round(corner_pct or 25)))))
    elif len(p.subs) > 5 and p.subs[5]:
        p.subs[5] = bytearray()
    return p


def _stack_record(sx, sy, corner_pct):
    """A size-and-shape sub-record: every layer the same, round-rect corners."""
    out = bytearray()
    out += struct.pack("<29i", *([mm2u(sx)] * 29))
    out += struct.pack("<29i", *([mm2u(sy)] * 29))
    out += bytes([2] * 29)
    out += b"\0"
    out += bytes([0])                        # hole shape: round
    out += struct.pack("<i", 0)              # slot size
    out += struct.pack("<d", 0.0)            # slot rotation
    out += struct.pack("<32i", *([0] * 32))  # hole offsets x
    out += struct.pack("<32i", *([0] * 32))  # hole offsets y
    out += b"\0"
    out += bytes([9] * 32)                   # alternate shape: round-rect
    out += bytes([corner_pct] * 32)
    assert len(out) == 596
    return out


def fill(doc, layer, x1, y1, x2, y2, rotation=0.0, net=None, component=None):
    f = _new(doc, "fill")
    _common(f, L.parse(layer), _net_index(doc, net), _comp_index(doc, component), None)
    f.set_geometry(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), rotation)
    return f


def text(doc, value, x, y, height, layer=L.TOP_OVERLAY, rotation=0.0, stroke_width=None,
         component=None, designator=False, comment=False):
    """A stroke-font text. The string goes to WideStrings6 as well; pcbwrite.add()
    assigns its index."""
    t = _new(doc, "text")
    s = t.subs[0]
    layer = L.parse(layer)
    if layer > 1000:
        raise FormatError("text on mechanical layers above 16 is not supported by this writer")
    s[0] = layer
    s[1] = (s[1] | 0x04) & ~0x03
    s[2] = 0
    struct.pack_into("<HH", s, 3, NONE16, NONE16)
    struct.pack_into("<H", s, 7, NONE16 if component is None else _comp_index(doc, component))
    t.set_geometry(x, y, height, rotation % 360.0)
    s[35] = 0
    struct.pack_into("<i", s, 36, mm2u(stroke_width if stroke_width else max(height / 10, 0.1)))
    if len(s) > 41:
        s[40] = 1 if comment else 0
        s[41] = 1 if designator else 0
    if len(s) > 132:
        s[132] = 3                       # left bottom: the position is the text's corner
    if len(s) > 230:
        s[230] = 0                       # no text frame (the template carries one)
    b = str(value).encode("cp1252", errors="replace")[:255]
    t.subs[1] = bytearray([len(b)]) + b
    t.pending_string = str(value)
    return t


def region_props(layer, kind=0, shape_based=False, polygon=None, extra=()):
    pairs = [("V7_LAYER", L.TOKENS.get(layer, f"MECHANICAL{L.mech_number(layer)}")),
             ("NAME", " "), ("KIND", str(kind)),
             ("SUBPOLYINDEX", "0" if polygon is not None else "-1"), ("UNIONINDEX", "0"),
             ("ARCRESOLUTION", "0.5mil"), ("ISSHAPEBASED", "TRUE" if shape_based else "FALSE"),
             ("CAVITYHEIGHT", "0mil")]
    pairs += list(extra)
    return Props.new(pairs, lead=False)


def region(doc, layer, outline, holes=(), net=None, polygon=None, component=None, kind=0,
           extra_props=()):
    """(Regions6 record, ShapeBasedRegions6 record) for a polygonal region.

    Both streams must stay index-parallel, so callers add the pair together.
    """
    layer = L.parse(layer)
    if len(outline) < 3:
        raise FormatError("a region needs at least three points")
    out = []
    for storage, extended in (("Regions6", False), ("ShapeBasedRegions6", True)):
        head = bytearray(18)
        head[0] = layer if layer < 256 else 72
        head[1] = 0x0C
        head[2] = 0
        struct.pack_into("<HHH", head, 3, NONE16 if net is None else _net_index(doc, net),
                         NONE16 if polygon is None else polygon,
                         NONE16 if component is None else _comp_index(doc, component))
        head[9:13] = b"\xff\xff\xff\xff"
        props = region_props(layer, kind, False, polygon, extra_props)
        pb = props.text_bytes()
        sub = head + struct.pack("<I", len(pb)) + pb
        r = Region(doc, storage, -1, 11, [bytearray(sub)], extended)
        r.set_shape(outline, holes)
        out.append(r)
    return tuple(out)


def unique_id(existing=()):
    taken = set(existing)
    while True:
        u = "".join(random.choice(string.ascii_uppercase) for _ in range(8))
        if u not in taken:
            return u


def pad_uid_record(pad_index, uid):
    return Props.new([("PRIMITIVEINDEX", str(pad_index)), ("PRIMITIVEOBJECTID", "Pad"),
                      ("UNIQUEID", uid)])
