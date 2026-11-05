"""Altium schematics (.SchDoc) and projects (.PrjPcb): parts, pins and the netlist.

    prj = Project.open("board.PrjPcb")
    net = prj.netlist()          # Netlist: parts by designator, nets of (designator, pin)

A SchDoc is a compound file whose FileHeader stream holds text records
(|RECORD=n|KEY=VALUE|...). Children point at their owner with OWNERINDEX,
counted from the first record after the header. Coordinates are in units of
10 mil plus *_FRAC in 1e-5 of a unit; this module keeps them as exact
integers (1e-5 unit) and converts to mm only for display.

Connectivity, per sheet: wires join at shared vertices and where one wire's
end lands on another wire; crossing wires join only at a junction; a pin
joins at its electrical end (location + pin length along its orientation)
when that lies on a wire or on another pin; net labels and power ports join
where their hot spot lies. Across sheets: power ports are global; net labels
are global when the project has no ports or sheet symbols (Altium's
automatic scope), ports are global in flat mode. Hierarchical designs (sheet
symbols) are not netlisted and raise.
"""

import configparser
import re
import struct
from pathlib import Path

from .cfb import CompoundFile

FRAC = 100000            # *_FRAC units per coordinate unit
MM_PER_UNIT = 0.254      # one schematic unit is 10 mil


class SchError(ValueError):
    pass


def _int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _coord(rec, key):
    return _int(rec.get(key)) * FRAC + _int(rec.get(key + "_FRAC"))


def to_mm(v):
    return v / FRAC * MM_PER_UNIT


def _text(rec, key, default=""):
    uni = rec.get("%UTF8%" + key)
    if uni is not None:
        try:
            return uni.encode("cp1252", errors="surrogateescape").decode("utf-8")
        except UnicodeError:
            pass
    v = rec.get(key)
    return default if v is None else v


def parse_records(data):
    """[dict] from a SchDoc record stream; binary records are kept raw."""
    out, pos = [], 0
    while pos + 4 <= len(data):
        hdr, = struct.unpack_from("<I", data, pos)
        n, flag = hdr & 0xFFFFFF, hdr >> 24
        body = data[pos + 4:pos + 4 + n]
        pos += 4 + n
        if flag != 0:
            out.append({"RECORD": "binary", "_raw": body, "_flag": flag})
            continue
        txt = body.rstrip(b"\0").decode("cp1252", errors="surrogateescape")
        rec = {}
        for kv in txt.split("|"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                rec[k.upper()] = v
        out.append(rec)
    return out


class Pin:
    __slots__ = ("number", "name", "point", "hidden", "electrical", "part")

    def __init__(self, number, name, point, hidden, electrical, part):
        self.number, self.name, self.point = number, name, point
        self.hidden, self.electrical, self.part = hidden, electrical, part


class Part:
    """One placed component record (one unit of a multi-part component)."""

    def __init__(self, sheet, index, rec):
        self.sheet = sheet
        self.index = index
        self.rec = rec
        self.unique_id = rec.get("UNIQUEID", "")
        self.libref = _text(rec, "LIBREFERENCE")
        self.design_item = _text(rec, "DESIGNITEMID")
        self.unit = _int(rec.get("CURRENTPARTID"), 1)
        self.display_mode = _int(rec.get("DISPLAYMODE"), 0)
        self.location = (_coord(rec, "LOCATION.X"), _coord(rec, "LOCATION.Y"))
        self.designator = ""
        self.comment = ""
        self.footprint = None
        self.footprints = []
        self.parameters = {}
        self.pins = []

    def __repr__(self):
        return f"Part({self.designator} {self.libref} {self.footprint})"


class SchDoc:
    def __init__(self, path, data=None):
        self.path = Path(path)
        cf = CompoundFile(data) if data is not None else CompoundFile.open(path)
        self.records = parse_records(cf.read("FileHeader"))
        if not self.records or "HEADER" not in self.records[0]:
            raise SchError(f"{path}: not a schematic (no header record)")
        self.body = self.records[1:]       # OWNERINDEX counts from here
        binary = sum(1 for r in self.body if r.get("RECORD") == "binary")
        if binary:
            raise SchError(f"{path}: {binary} binary record(s) (a newer pin format) are not "
                           "read; a netlist without them would miss pins")
        self.parts = []
        self.wires = []                    # [[(x, y), ...]]
        self.junctions = []
        self.net_labels = []               # [(text, (x, y))]
        self.power_ports = []
        self.ports = []                    # [(name, [(x, y), ...])]
        self.sheet_symbols = 0
        self._build()

    @classmethod
    def open(cls, path):
        return cls(path)

    def _build(self):
        recs = self.body
        parts = {}
        for i, r in enumerate(recs):
            if r.get("RECORD") == "1":
                parts[i] = Part(self, i, r)
        impl_lists = {}
        for i, r in enumerate(recs):
            t = r.get("RECORD")
            own = _int(r.get("OWNERINDEX"), -1)
            if t == "2" and own in parts:
                p = parts[own]
                unit = _int(r.get("OWNERPARTID"), -1)
                if unit > 0 and unit != p.unit:
                    continue
                if _int(r.get("OWNERPARTDISPLAYMODE"), 0) != p.display_mode:
                    continue
                cong = _int(r.get("PINCONGLOMERATE"))
                x, y = _coord(r, "LOCATION.X"), _coord(r, "LOCATION.Y")
                ln = _coord(r, "PINLENGTH")
                dx, dy = ((ln, 0), (0, ln), (-ln, 0), (0, -ln))[cong & 3]
                p.pins.append(Pin(_text(r, "DESIGNATOR"), _text(r, "NAME"), (x + dx, y + dy),
                                  bool(cong & 0x04), _int(r.get("ELECTRICAL")), p.unit))
            elif t == "34" and own in parts:
                p = parts[own]
                p.designator = _text(r, "TEXT")
            elif t == "41" and own in parts:
                parts[own].parameters[_text(r, "NAME")] = _text(r, "TEXT")
            elif t == "44" and own in parts:
                impl_lists[i] = parts[own]
            elif t == "27":
                n = _int(r.get("LOCATIONCOUNT"))
                pts = [(_coord(r, f"X{k}"), _coord(r, f"Y{k}")) for k in range(1, n + 1)]
                if len(pts) >= 2:
                    self.wires.append(pts)
            elif t == "29":
                self.junctions.append((_coord(r, "LOCATION.X"), _coord(r, "LOCATION.Y")))
            elif t == "25":
                self.net_labels.append((_text(r, "TEXT"), (_coord(r, "LOCATION.X"),
                                                           _coord(r, "LOCATION.Y"))))
            elif t == "17":
                self.power_ports.append((_text(r, "TEXT"), (_coord(r, "LOCATION.X"),
                                                            _coord(r, "LOCATION.Y"))))
            elif t == "18":
                x, y = _coord(r, "LOCATION.X"), _coord(r, "LOCATION.Y")
                w = _coord(r, "WIDTH")
                style = _int(r.get("STYLE"))
                ends = [(x, y), (x + w, y)] if style in (0, 1, 2, 3) else [(x, y), (x, y + w)]
                self.ports.append((_text(r, "NAME"), ends))
            elif t == "15":
                self.sheet_symbols += 1
        for i, r in enumerate(recs):
            if r.get("RECORD") == "45" and _int(r.get("OWNERINDEX"), -1) in impl_lists:
                p = impl_lists[_int(r.get("OWNERINDEX"))]
                if r.get("MODELTYPE", "").upper() != "PCBLIB":
                    continue
                name = _text(r, "MODELNAME")
                p.footprints.append(name)
                if r.get("ISCURRENT", "").upper() == "T" or p.footprint is None:
                    p.footprint = name
        for p in parts.values():
            c = p.parameters.get("Comment", "")
            if c.startswith("=") and c[1:] in p.parameters:
                c = p.parameters[c[1:]]
            p.comment = c
        self.parts = list(parts.values())


# ---------------------------------------------------------------- connectivity

class _DSU:
    def __init__(self):
        self.p = {}

    def find(self, a):
        p = self.p
        p.setdefault(a, a)
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _on_segment(pt, a, b):
    (x, y), (x1, y1), (x2, y2) = pt, a, b
    if (x2 - x1) * (y - y1) != (y2 - y1) * (x - x1):
        return False
    return min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2)


def sheet_groups(sheet):
    """Union-find groups on one sheet: keys are ("wire", i), ("pin", part, k),
    ("label", i), ("power", i), ("port", i)."""
    d = _DSU()
    wires = sheet.wires
    vertex = {}
    for i, w in enumerate(wires):
        d.find(("wire", i))
        for pt in w:
            vertex.setdefault(pt, []).append(i)
    for pt, ws in vertex.items():
        for j in ws[1:]:
            d.union(("wire", ws[0]), ("wire", j))
    segs = [(i, a, b) for i, w in enumerate(wires) for a, b in zip(w, w[1:])]

    def wires_at(pt, ends_only=False):
        hit = set(vertex.get(pt, ()))
        if not ends_only:
            for i, a, b in segs:
                if i not in hit and _on_segment(pt, a, b):
                    hit.add(i)
        return hit

    for i, w in enumerate(wires):              # a wire end landing on another wire
        for pt in (w[0], w[-1]):
            for j in wires_at(pt):
                d.union(("wire", i), ("wire", j))
    for pt in sheet.junctions:                 # crossings joined by a junction
        ws = list(wires_at(pt))
        for j in ws[1:]:
            d.union(("wire", ws[0]), ("wire", j))
    points = {}
    for part in sheet.parts:
        for k, pin in enumerate(part.pins):
            key = ("pin", part.index, k)
            d.find(key)
            points.setdefault(pin.point, []).append(key)
    for i, (_t, pt) in enumerate(sheet.net_labels):
        points.setdefault(pt, []).append(("label", i))
    for i, (_t, pt) in enumerate(sheet.power_ports):
        points.setdefault(pt, []).append(("power", i))
    for i, (_t, ends) in enumerate(sheet.ports):
        for pt in ends:
            points.setdefault(pt, []).append(("port", i))
    for pt, keys in points.items():
        for k in keys[1:]:
            d.union(keys[0], k)
        for j in wires_at(pt):
            d.union(keys[0], ("wire", j))
    return d


# ---------------------------------------------------------------- netlist

def natural_key(s):
    return [int(t) if t.isdigit() else t.upper() for t in re.split(r"(\d+)", s or "")]


class Netlist:
    """parts: {designator: info}; nets: {name: set((designator, pin))}."""

    def __init__(self):
        self.parts = {}
        self.nets = {}
        self.warnings = []

    def pin_net(self):
        out = {}
        for name, pins in self.nets.items():
            for p in pins:
                out[p] = name
        return out

    @classmethod
    def from_dict(cls, data):
        """A netlist written by hand or by another tool:

            {"parts": {"J1": {"footprint": "CONN2", "comment": "", "unique_id": "ABCDEFGH",
                              "pins": ["1", "2"]}, ...},
             "nets": {"VBAT": ["J1.1", "Q1.D"], ...}}

        A pin is "DES.PIN" (split at the first '.') or [DES, PIN]. Pins named
        in nets are added to their part; a pin of a part not listed, or a pin
        on two nets, is an error."""
        if not isinstance(data, dict) or not isinstance(data.get("parts"), dict) or \
                not isinstance(data.get("nets"), dict):
            raise SchError("a JSON netlist needs 'parts' and 'nets' objects")
        out = cls()
        for des, info in data["parts"].items():
            info = dict(info or {})
            uid = str(info.get("unique_id") or "")
            fp = str(info.get("footprint") or "")
            out.parts[des] = {
                "designator": des, "footprint": fp,
                "footprints": list(info.get("footprints") or ([fp] if fp else [])),
                "comment": str(info.get("comment") or ""),
                "libref": str(info.get("libref") or ""), "unique_id": uid,
                "unique_ids": list(info.get("unique_ids") or ([uid] if uid else [])),
                "sheet": str(info.get("sheet") or "netlist.json"),
                "pins": sorted({str(p) for p in info.get("pins") or []}, key=natural_key),
                "units": [1]}
        seen = {}
        for name, pins in data["nets"].items():
            members = set()
            for p in pins:
                if isinstance(p, (list, tuple)) and len(p) == 2:
                    des, pin = str(p[0]), str(p[1])
                elif isinstance(p, str) and "." in p:
                    des, pin = p.split(".", 1)
                else:
                    raise SchError(f"net {name}: pin {p!r} is not 'DES.PIN' or [DES, PIN]")
                if des not in out.parts:
                    raise SchError(f"net {name}: {des} is not in 'parts'")
                if (des, pin) in seen and seen[(des, pin)] != name:
                    raise SchError(f"{des}.{pin} is on both {seen[(des, pin)]} and {name}")
                seen[(des, pin)] = name
                members.add((des, pin))
                part = out.parts[des]
                if pin not in part["pins"]:
                    part["pins"] = sorted(set(part["pins"]) | {pin}, key=natural_key)
            out.nets[str(name)] = members
        return out

    def to_dict(self):
        """The inverse of from_dict (pins as "DES.PIN")."""
        parts = {d: {k: p[k] for k in ("footprint", "comment", "unique_id", "libref", "pins")}
                 for d, p in self.parts.items()}
        nets = {n: sorted((f"{d}.{p}" for d, p in pins), key=natural_key)
                for n, pins in self.nets.items()}
        return {"parts": parts, "nets": nets}


class Project:
    def __init__(self, sheets, settings=None, path=None, pcb=None):
        self.sheets = sheets
        self.settings = settings or {}
        self.path = path
        self.pcb = pcb

    @classmethod
    def open(cls, path):
        path = Path(path)
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read_string(path.read_text(encoding="utf-8-sig", errors="replace"))
        docs = []
        for sec in cp.sections():
            if sec.startswith("Document") and cp.has_option(sec, "DocumentPath"):
                docs.append(cp.get(sec, "DocumentPath"))
        sheets, pcb = [], None
        for rel in docs:
            p = (path.parent / rel.replace("\\", "/"))
            if p.suffix.lower() == ".schdoc":
                if not p.exists():
                    raise SchError(f"{path.name} lists {rel}, which is missing")
                sheets.append(SchDoc.open(p))
            elif p.suffix.lower() == ".pcbdoc":
                pcb = p
        settings = dict(cp.items("Design")) if cp.has_section("Design") else {}
        return cls(sheets, settings, path, pcb)

    @classmethod
    def from_sheets(cls, paths):
        return cls([SchDoc.open(p) for p in paths])

    def scope(self):
        """'global' (net labels and ports cross sheets), 'flat' (ports only)."""
        mode = _int(self.settings.get("HierarchyMode"), 0)
        if any(s.sheet_symbols for s in self.sheets):
            raise SchError("hierarchical schematics (sheet symbols) are not netlisted yet")
        has_ports = any(s.ports for s in self.sheets)
        if mode == 1 or (mode == 0 and has_ports):
            return "flat"
        return "global"

    def netlist(self):
        out = Netlist()
        scope = self.scope()
        groups = []          # [(sheet, root, members)]
        for s in self.sheets:
            d = sheet_groups(s)
            by = {}
            for key in list(d.p):
                by.setdefault(d.find(key), []).append(key)
            for root, keys in by.items():
                groups.append((s, root, keys))
        # merge across sheets by name
        g = _DSU()
        named = {}
        for gi, (s, _root, keys) in enumerate(groups):
            g.find(gi)
            for k in keys:
                if k[0] == "power":
                    name = ("power", s.power_ports[k[1]][0].upper())
                elif k[0] == "label":
                    txt = s.net_labels[k[1]][0].upper()
                    name = ("label", txt) if scope == "global" else ("label", str(s.path), txt)
                elif k[0] == "port":
                    name = ("port", s.ports[k[1]][0].upper())
                else:
                    continue
                if name in named:
                    g.union(gi, named[name])
                else:
                    named[name] = gi
        nets = {}
        for gi, (s, _root, keys) in enumerate(groups):
            nets.setdefault(g.find(gi), []).append((s, keys))
        parts_by_sheet = {s: {p.index: p for p in s.parts} for s in self.sheets}
        label_first = _int(self.settings.get("PowerPortNamesTakePriority"), 0) == 0
        allow_port = _int(self.settings.get("AllowPortNetNames"), 0) == 1
        single = _int(self.settings.get("NetlistSinglePinNets"), 0) == 1
        used = set()
        for root, members in nets.items():
            pins, labels, powers, ports = set(), [], [], []
            for s, keys in members:
                for k in keys:
                    if k[0] == "pin":
                        part = parts_by_sheet[s][k[1]]
                        pins.add((part.designator, part.pins[k[2]].number))
                    elif k[0] == "label":
                        labels.append(s.net_labels[k[1]][0])
                    elif k[0] == "power":
                        powers.append(s.power_ports[k[1]][0])
                    elif k[0] == "port":
                        ports.append(s.ports[k[1]][0])
            if not pins:
                continue
            if len(pins) < 2 and not (labels or powers) and not single:
                continue
            first = (sorted(labels) + sorted(powers)) if label_first else \
                (sorted(powers) + sorted(labels))
            if not first and allow_port:
                first = sorted(ports)
            if first:
                name = first[0]
            else:
                d0, p0 = min(pins, key=lambda dp: (natural_key(dp[0]), natural_key(dp[1])))
                name = f"Net{d0}_{p0}"
            if name in used:
                out.warnings.append(f"two separate nets would both be called {name}")
                k = 2
                while f"{name}_{k}" in used:
                    k += 1
                name = f"{name}_{k}"
            used.add(name)
            out.nets[name] = pins
        for s in self.sheets:
            for p in s.parts:
                if not p.designator or p.designator.endswith("?"):
                    out.warnings.append(f"{s.path.name}: unannotated part {p.libref}")
                    continue
                info = out.parts.get(p.designator)
                if info is None:
                    out.parts[p.designator] = {
                        "designator": p.designator, "footprint": p.footprint,
                        "footprints": list(p.footprints), "comment": p.comment,
                        "libref": p.libref, "unique_id": p.unique_id,
                        "unique_ids": [p.unique_id], "sheet": s.path.name,
                        "pins": sorted({pin.number for pin in p.pins}, key=natural_key),
                        "units": [p.unit]}
                else:
                    info["units"].append(p.unit)
                    info["unique_ids"].append(p.unique_id)
                    info["pins"] = sorted(set(info["pins"]) | {pin.number for pin in p.pins},
                                          key=natural_key)
                    if p.unit < min(info["units"][:-1]):
                        info["unique_id"] = p.unique_id
        return out
