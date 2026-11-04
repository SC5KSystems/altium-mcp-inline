"""Footprints: read them from a .PcbLib or from a component on a .PcbDoc, and
place or swap them on a board.

    lib = PcbLib.open("parts.PcbLib")
    fp = lib.footprint("SOIC127P600X175-8N")
    place(Editor(doc), fp, "U9", x=120, y=40, nets={"1": "VBAT"})

A PcbLib is a compound file with one storage per footprint; its Data stream
is a name sub-record followed by the same binary primitive records as a
PcbDoc's (type byte, then sub-records), all kinds interleaved. Footprint
coordinates are local: the reference point is (0, 0).

Placing clones the primitive records (so a pad keeps its full layer stack,
hole shape and mask settings), adds the component at the origin, then moves
it with Editor.move_component, which owns every rotation and flip rule.
3D bodies are not copied (their models live elsewhere); the report says so.
"""

import struct
from pathlib import Path

from . import layers as L
from . import records as R
from .cfb import CompoundFile
from .pcbdoc import (Arc, Body, Fill, FormatError, Pad, Props, Region, Text, Track, Via,
                     parse_widestrings, split_props)

KIND_CLASS = {1: Arc, 2: Pad, 3: Via, 4: Track, 5: Text, 6: Fill, 11: Region, 12: Body}
NSUB = {1: 1, 2: 6, 3: 1, 4: 1, 5: 2, 6: 1, 11: 1, 12: 1}
STORAGE = {"arc": "Arcs6", "pad": "Pads6", "via": "Vias6", "track": "Tracks6", "text": "Texts6",
           "fill": "Fills6"}


class FootprintError(ValueError):
    pass


class _LibDoc:
    """The little a record needs from its document when it lives in a library."""

    def __init__(self, widestrings=None):
        self.widestrings = widestrings or {}
        self.storages = {}
        self.polygons = []
        self.nets = []
        self.components = []
        self.copper_layers = [L.TOP, L.BOTTOM]


class Footprint:
    def __init__(self, name, records, source, description="", source_path=None):
        self.name = name
        self.records = records
        self.source = source              # for people: where it came from
        self.source_path = source_path    # a file a plan can name, or None
        self.description = description

    @property
    def pads(self):
        return [r for r in self.records if r.KIND == "pad"]

    def pad_names(self):
        return [p.name for p in self.pads]

    def summary(self):
        kinds = {}
        for r in self.records:
            kinds[r.KIND] = kinds.get(r.KIND, 0) + 1
        return {"name": self.name, "source": str(self.source), "primitives": kinds,
                "pads": self.pad_names()}


def parse_footprint_data(data, doc, name=""):
    """Primitive records of one footprint Data stream."""
    if len(data) < 4:
        raise FootprintError(f"{name}: footprint data is empty")
    n, = struct.unpack_from("<I", data, 0)
    pos = 4 + n
    out = []
    while len(data) - pos >= 4:
        t = data[pos]
        pos += 1
        if t not in NSUB:
            raise FootprintError(f"{name}: unknown primitive type {t} at byte {pos - 1}")
        subs = []
        for _ in range(NSUB[t]):
            if pos + 4 > len(data):
                raise FootprintError(f"{name}: record header truncated")
            ln, = struct.unpack_from("<I", data, pos)
            pos += 4
            if pos + ln > len(data):
                raise FootprintError(f"{name}: record runs past the end of the stream")
            subs.append(bytearray(data[pos:pos + ln]))
            pos += ln
        cls = KIND_CLASS[t]
        storage = {1: "Arcs6", 2: "Pads6", 3: "Vias6", 4: "Tracks6", 5: "Texts6", 6: "Fills6",
                   11: "Regions6", 12: "ComponentBodies6"}[t]
        if cls in (Region, Body):
            out.append(cls(doc, storage, len(out), t, subs, False))
        else:
            out.append(cls(doc, storage, len(out), t, subs))
    return out


class PcbLib:
    def __init__(self, path, data=None):
        self.path = Path(path)
        self.cf = CompoundFile(data) if data is not None else CompoundFile.open(path)
        self._dirs = {}
        for p in self.cf.streams():
            head, _, rest = p.partition("/")
            if rest == "Data" and head not in ("Library", "FileVersionInfo"):
                self._dirs[head] = None
        self._names = {}
        for d in self._dirs:
            name = d
            raw = self.cf.read(f"{d}/Parameters") if f"{d}/Parameters" in self.cf.streams() else b""
            if raw:
                for p in split_props(raw, 0):
                    if p.get("PATTERN"):
                        name = p.text("PATTERN")
            self._names[name] = d

    @classmethod
    def open(cls, path):
        return cls(path)

    def names(self):
        return sorted(self._names)

    def footprint(self, name):
        d = self._names.get(name)
        if d is None:
            hits = [k for k in self._names if k.upper() == str(name).upper()]
            if len(hits) != 1:
                raise FootprintError(f"{self.path.name} has no footprint {name!r}")
            name, d = hits[0], self._names[hits[0]]
        ws = {}
        if f"{d}/WideStrings" in self.cf.streams():
            try:
                ws = parse_widestrings(self.cf.read(f"{d}/WideStrings"))
            except (FormatError, ValueError, struct.error):
                ws = {}
        doc = _LibDoc(ws if isinstance(ws, dict) else {})
        recs = parse_footprint_data(self.cf.read(f"{d}/Data"), doc, name)
        desc = ""
        if f"{d}/Parameters" in self.cf.streams():
            for p in split_props(self.cf.read(f"{d}/Parameters"), 0):
                desc = p.text("DESCRIPTION") or desc
        return Footprint(name, recs, self.path, desc, source_path=self.path)


def extract(doc, designator):
    """A placed component's footprint in local coordinates (the board is not
    changed: the component is moved to the origin on a copy in memory)."""
    from .edits import Editor
    from .pcbdoc import PcbDoc
    from .pcbwrite import to_document_bytes
    copy = PcbDoc(CompoundFile(to_document_bytes(doc)))
    comp = copy.component(designator)
    ed = Editor(copy)
    ed.move_component(designator, 0.0, 0.0, 0.0, side="top")
    recs = []
    for r in copy.primitives(("track", "arc", "via", "pad", "fill", "region", "text", "body")):
        if r.component == comp.index:
            recs.append(r)
    path = getattr(doc, "path", None)
    return Footprint(comp.pattern, recs, f"{designator} on {path or 'the board'}",
                     source_path=path)


def find(name, sources):
    """The first footprint called `name` in the sources (PcbLib paths or
    objects; (PcbDoc, designator) pairs; or PcbDocs, searched by pattern)."""
    for src in sources:
        if isinstance(src, Footprint):
            if src.name.upper() == str(name).upper():
                return src
            continue
        if isinstance(src, (str, Path)):
            p = Path(src)
            if p.suffix.lower() == ".pcblib":
                src = PcbLib.open(p)
            elif p.suffix.lower() == ".pcbdoc":
                from .pcbdoc import PcbDoc
                src = PcbDoc.open(p)
        if isinstance(src, PcbLib):
            try:
                return src.footprint(name)
            except FootprintError:
                continue
        if hasattr(src, "components"):
            for c in src.components:
                if c.pattern.upper() == str(name).upper():
                    return extract(src, c.designator)
    return None


def _clone(rec, doc):
    cls = type(rec)
    subs = [bytearray(s) for s in rec.subs]
    new = cls(doc, STORAGE[rec.KIND], -1, rec.rtype, subs)
    return new


def place(ed, fp, designator, x, y, rotation=0.0, side="top", nets=None, comment="",
          source_unique_id=None, props=None):
    """Add `designator` with footprint `fp` at (x, y). nets: {pad name: net
    name}; pads not named get no net. props: Components6 values to copy
    (e.g. the SOURCE* fields of a part being swapped). Returns a report."""
    doc = ed.doc
    nets = nets or {}
    ci = ed.add_component(designator, fp.name, 0.0, 0.0, 0.0, "top", children=(),
                          source_unique_id=source_unique_id, comment=comment or fp.name)
    if props:
        p = doc.components[ci].props
        for k, v in props.items():
            if k.upper() not in ("X", "Y", "ROTATION", "LAYER", "PATTERN", "UNIQUEID"):
                p.set(k, v)
        p.dirty = True
    report = {"designator": designator, "footprint": fp.name, "source": str(fp.source),
              "pads": 0, "primitives": 0, "skipped": {}}
    has_text = {"designator": False, "comment": False}
    for r in fp.records:
        k = r.KIND
        if k == "body":
            report["skipped"]["3d_body"] = report["skipped"].get("3d_body", 0) + 1
            continue
        if k == "region":
            outline = r.outline()
            if len(outline) < 3:
                continue
            new = R.region(doc, r.layer, outline, [h for h in r.holes if len(h) >= 3], None,
                           None, ci, kind=int(r.props.get("KIND", "0") or 0))
            if r.is_keepout:
                for t in new:
                    t.subs[0][2] = 2
                    if r.props.get("KEEPOUTRESTRICTIONS") is not None:
                        t.props.set("KEEPOUTRESTRICTIONS", r.props.get("KEEPOUTRESTRICTIONS"))
            ed.add(new)
            report["primitives"] += 1
            continue
        if k == "text":
            if r.is_designator:
                has_text["designator"] = True
                value = designator
            elif r.is_comment:
                has_text["comment"] = True
                value = comment or fp.name
            else:
                value = r.text
            t = R.text(doc, value, r.x, r.y, r.height, r.layer if r.layer < 1000 else L.TOP_OVERLAY,
                       r.rotation, r.stroke_width or None, component=ci,
                       designator=r.is_designator, comment=r.is_comment)
            ed.add(t)
            report["primitives"] += 1
            continue
        new = _clone(r, doc)
        new.component = ci
        if k in ("pad", "via", "track", "arc", "fill"):
            net = nets.get(r.name) if k == "pad" else None
            new.net = ed.net_index(net) if net else None
        if hasattr(new, "polygon") and k in ("track", "arc", "region"):
            try:
                new.polygon = None
            except AttributeError:
                pass
        ed.add(new)
        report["primitives"] += 1
        if k == "pad":
            report["pads"] += 1
    # every placed component carries one designator and one comment text
    if not has_text["designator"]:
        ed.add(R.text(doc, designator, 0.0, 1.5, 1.0, L.TOP_OVERLAY, component=ci,
                      designator=True))
    if not has_text["comment"]:
        ed.add(R.text(doc, comment or fp.name, 0.0, -1.5, 1.0, L.TOP_OVERLAY, component=ci,
                      comment=True))
    ed.move_component(designator, x, y, rotation, side=side)
    missing = sorted(set(nets) - set(fp.pad_names()))
    if missing:
        report["nets_without_pad"] = missing
    return report


def swap(ed, designator, fp):
    """Replace a component's footprint in place, keeping its position, side,
    source links and the nets of pads with the same number. Reports pads that
    moved, pins with no pad now, and tracks left stranded."""
    from . import geom
    doc = ed.doc
    comp = doc.component(designator)
    x, y, rot = comp.x, comp.y, comp.rotation
    side = "bottom" if comp.layer == L.BOTTOM else "top"
    old = {}
    for p in doc.pads:
        if p.component == comp.index:
            old[p.name] = (p.net_name, p.x, p.y, geom.pad_shape(p, p.layer if p.layer != L.MULTI
                                                              else L.TOP))
    keep = {k: v for k, v in comp.props.items
            if k.upper().startswith("SOURCE") or k.upper() in ("COMMENTON", "NAMEON", "LOCKED")}
    # free copper that ended on the old pads
    ends = []
    for t in doc.tracks:
        if t.component is None and t.polygon is None:
            for px, py in ((t.x1, t.y1), (t.x2, t.y2)):
                for name, (net, _x, _y, shape) in old.items():
                    if net and t.net_name == net and shape.buffer(1e-4).contains(
                            geom.require().Point(px, py)):
                        ends.append((t.index, px, py, name, net))
    ed.remove_component(designator)
    report = place(ed, fp, designator, x, y, rot, side,
                   nets={n: v[0] for n, v in old.items() if v[0]}, props=keep)
    comp = doc.component(designator)
    new = {p.name: p for p in doc.pads if p.component == comp.index}
    moved = []
    for name, (net, ox, oy, _s) in old.items():
        if name in new:
            d = ((new[name].x - ox) ** 2 + (new[name].y - oy) ** 2) ** 0.5
            if d > 1e-4:
                moved.append({"pad": name, "net": net, "moved_mm": round(d, 4),
                              "from": [round(ox, 4), round(oy, 4)],
                              "to": [round(new[name].x, 4), round(new[name].y, 4)]})
    stranded = []
    for idx, px, py, name, net in ends:
        p = new.get(name)
        ok = p is not None and geom.pad_shape(p, p.layer if p.layer != L.MULTI else L.TOP
                                              ).buffer(1e-4).contains(geom.require().Point(px, py))
        if not ok:
            stranded.append({"track": idx, "net": net, "at": [round(px, 4), round(py, 4)],
                             "was_on_pad": name})
    report.update({
        "moved_pads": moved,
        "pins_without_pad": sorted((set(old) - set(new)), key=_nat),
        "pads_without_old_pin": sorted((set(new) - set(old)), key=_nat),
        "stranded_tracks": stranded})
    return report


def _nat(s):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
