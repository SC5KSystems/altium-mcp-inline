"""Read an Altium .SchDoc without Altium.

A .SchDoc is an OLE compound file. Its `FileHeader` stream is a run of records,
each a 4-byte header (3-byte little-endian length, then a flag byte; 0 means
text) followed by `|KEY=value|KEY=value...` text ending in NUL. The first record
is the file header. Every later record's position in the run, counting from 0
after the header, is the number that child records give as OwnerIndex.

Coordinates are in units of 10 mil, each with an optional `_FRAC` part in
1/100000 of a unit. They are held here as exact integers of 1/100000 unit
("fine units") so that the connectivity tests in `schnet` compare points
exactly, and converted to mils only for output.

Record numbers used (others are counted in `SchDoc.unknown`):
    1 component      2 pin            15 sheet symbol   16 sheet entry
    17 power port    18 port          22 no-ERC         25 net label
    26 bus           27 wire          29 junction       34 designator
    37 bus entry     41 parameter
"""
from dataclasses import dataclass, field
from pathlib import Path

from .cfb import CompoundFile

FINE = 100000          # fine units per file unit
MIL_PER_UNIT = 10.0    # one file unit is 10 mil

# Pin.Electrical, as Altium numbers it
ELECTRICAL = {0: "input", 1: "io", 2: "output", 3: "open_collector", 4: "passive",
              5: "hiz", 6: "open_emitter", 7: "power"}


def to_mils(fine):
    return fine * MIL_PER_UNIT / FINE


def parse_records(stream: bytes):
    """Records of a FileHeader stream as dicts with upper-case keys, header included."""
    recs = []
    i = 0
    while i + 4 <= len(stream):
        n = stream[i] | stream[i + 1] << 8 | stream[i + 2] << 16
        flag = stream[i + 3]
        body = stream[i + 4:i + 4 + n]
        i += 4 + n
        if flag != 0:
            recs.append({"RECORD": "binary", "_BYTES": body})
            continue
        text = body.rstrip(b"\x00")
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            text = text.decode("cp1252", errors="replace")
        rec = {}
        for part in text.split("|"):
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.upper()
                if k.startswith("%UTF8%"):
                    rec[k[6:]] = v      # the UTF-8 copy wins over the code-page one
                elif k not in rec:
                    rec[k] = v
        recs.append(rec)
    return recs


def _num(rec, key, default=0):
    try:
        return int(rec.get(key, default))
    except ValueError:
        return int(float(rec.get(key, default)))


def _coord(rec, key):
    """A coordinate field plus its _FRAC part, in fine units."""
    return _num(rec, key) * FINE + _num(rec, key + "_FRAC")


def _point(rec, prefix="LOCATION"):
    return (_coord(rec, prefix + ".X"), _coord(rec, prefix + ".Y"))


@dataclass
class SchComponent:
    index: int                 # record index (what OwnerIndex points at)
    designator: str
    libref: str
    description: str
    design_item_id: str
    library: str
    part_id: int
    part_count: int            # real parts; the file stores one more than this
    display_mode: int
    location: tuple
    unique_id: str
    item_guid: str
    revision_guid: str
    params: dict = field(default_factory=dict)

    @property
    def comment(self):
        return self.params.get("Comment", "")


@dataclass
class SchPin:
    comp: int                  # owning component's record index
    number: str                # pin designator
    name: str
    electrical: str
    base: tuple                # end on the symbol body
    hot: tuple                 # electrical end, where wires attach
    orientation: int           # 0 right, 1 up, 2 left, 3 down
    hidden: bool
    hidden_net: str


@dataclass
class Wire:
    points: list               # polyline vertices in fine units


@dataclass
class NetLabel:
    at: tuple
    text: str


@dataclass
class PowerPort:
    at: tuple
    text: str
    style: int


@dataclass
class Port:
    at: tuple
    name: str
    width: int                 # in fine units
    style: int
    io_type: int


@dataclass
class SheetSymbol:
    index: int
    file_name: str
    name: str


@dataclass
class SheetEntry:
    owner: int
    name: str


def _pin_hot(base, orientation, length):
    x, y = base
    if orientation == 0:
        return (x + length, y)
    if orientation == 1:
        return (x, y + length)
    if orientation == 2:
        return (x - length, y)
    return (x, y - length)


class SchDoc:
    """One schematic sheet: its parts, pins and connectivity objects."""

    def __init__(self, path, records):
        self.path = Path(path)
        self.name = self.path.name
        self.header = records[0] if records else {}
        self.records = records[1:]
        self.components = {}       # record index -> SchComponent
        self.pins = []
        self.wires = []
        self.junctions = []
        self.net_labels = []
        self.power_ports = []
        self.ports = []
        self.sheet_symbols = []
        self.sheet_entries = []
        self.no_ercs = []
        self.buses = 0
        self.unknown = {}
        self._build()

    @classmethod
    def load(cls, path):
        cf = CompoundFile(Path(path).read_bytes())
        return cls(path, parse_records(cf.read("FileHeader")))

    def _build(self):
        recs = self.records
        for i, r in enumerate(recs):
            if r.get("RECORD") == "1":
                self.components[i] = SchComponent(
                    index=i, designator="", libref=r.get("LIBREFERENCE", ""),
                    description=r.get("COMPONENTDESCRIPTION", ""),
                    design_item_id=r.get("DESIGNITEMID", ""), library=r.get("SOURCELIBRARYNAME", ""),
                    part_id=_num(r, "CURRENTPARTID", 1), part_count=max(1, _num(r, "PARTCOUNT", 2) - 1),
                    display_mode=_num(r, "DISPLAYMODE"), location=_point(r),
                    unique_id=r.get("UNIQUEID", ""), item_guid=r.get("ITEMGUID", ""),
                    revision_guid=r.get("REVISIONGUID", ""))
        for i, r in enumerate(recs):
            kind = r.get("RECORD")
            owner = _num(r, "OWNERINDEX", -1) if "OWNERINDEX" in r else -1
            comp = self.components.get(owner)
            if kind == "2" and comp is not None:
                part = _num(r, "OWNERPARTID", 1)
                mode = _num(r, "OWNERPARTDISPLAYMODE")
                if part not in (comp.part_id, 0) or mode != comp.display_mode:
                    continue                      # pin of another part or display mode
                cong = _num(r, "PINCONGLOMERATE")
                orient = cong & 3
                base = _point(r)
                length = _coord(r, "PINLENGTH")
                self.pins.append(SchPin(
                    comp=owner, number=r.get("DESIGNATOR", ""), name=r.get("NAME", ""),
                    electrical=ELECTRICAL.get(_num(r, "ELECTRICAL"), "unknown"),
                    base=base, hot=_pin_hot(base, orient, length), orientation=orient,
                    hidden=bool(cong & 4), hidden_net=r.get("HIDDENNETNAME", "")))
            elif kind == "34" and comp is not None:
                comp.designator = r.get("TEXT", "")
            elif kind == "41" and comp is not None:
                comp.params[r.get("NAME", "")] = r.get("TEXT", "")
            elif kind == "27":
                n = _num(r, "LOCATIONCOUNT")
                pts = [(_coord(r, "X%d" % k), _coord(r, "Y%d" % k)) for k in range(1, n + 1)]
                if len(pts) >= 2:
                    self.wires.append(Wire(pts))
            elif kind == "29":
                self.junctions.append(_point(r))
            elif kind == "25":
                self.net_labels.append(NetLabel(_point(r), r.get("TEXT", "")))
            elif kind == "17":
                self.power_ports.append(PowerPort(_point(r), r.get("TEXT", ""), _num(r, "STYLE")))
            elif kind == "18":
                self.ports.append(Port(_point(r), r.get("NAME", ""), _num(r, "WIDTH") * FINE,
                                       _num(r, "STYLE"), _num(r, "IOTYPE")))
            elif kind == "15":
                self.sheet_symbols.append(SheetSymbol(i, "", ""))
            elif kind == "16":
                self.sheet_entries.append(SheetEntry(owner, r.get("NAME", "")))
            elif kind == "22":
                self.no_ercs.append(_point(r))
            elif kind in ("26", "37"):
                self.buses += 1
            elif kind not in ("1", "2", "34", "41"):
                self.unknown[kind] = self.unknown.get(kind, 0) + 1
        # Sheet symbol file and name live in child records 32 and 33
        by_index = {s.index: s for s in self.sheet_symbols}
        for r in recs:
            s = by_index.get(_num(r, "OWNERINDEX", -1)) if "OWNERINDEX" in r else None
            if s is not None:
                if r.get("RECORD") == "33":
                    s.file_name = r.get("TEXT", "")
                elif r.get("RECORD") == "32":
                    s.name = r.get("TEXT", "")

    def component_by_designator(self):
        return {c.designator: c for c in self.components.values()}
