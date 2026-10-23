"""Netlist of an Altium schematic project, built from the saved files.

Connectivity follows Altium's compiler as far as flat designs need it (the same
rules as dev/netlist.py, which was checked against Altium):
  - a wire connects all of its own vertices
  - wires connect where a vertex or end of one coincides with a vertex or end of another
  - a wire end or bend that lands inside another wire's segment connects only through a
    junction there; each such spot without one is reported as `t_without_junction`
  - plain crossings do not connect
  - a pin connects at its electrical end: to wire vertices, segment interiors, other pin
    ends, net labels and power ports at that point
  - a junction joins every wire passing through it
  - net labels and power ports join whatever they touch
  - across sheets, power ports always join by name. Net labels join by name across sheets
    in global scope and only within their sheet in flat scope. Sheet symbols, sheet
    entries and ports (hierarchical designs) are reported, not followed.
"""
import configparser
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .schdoc import SchDoc, to_mils

# Project option HierarchyMode (Net Identifier Scope). Only 0, Automatic, has been seen in a
# real project; the other numbers follow the order of Altium's drop-down and are unconfirmed.
SCOPES = {0: "automatic", 1: "flat", 2: "hierarchical", 3: "strict_hierarchical", 4: "global"}


class DSU:
    def __init__(self):
        self.parent = {}

    def find(self, a):
        self.parent.setdefault(a, a)
        root = a
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[a] != root:
            self.parent[a], a = root, self.parent[a]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _inside(p, a, b):
    """True if p lies on segment a-b strictly between its ends (exact integer test)."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    if p == a or p == b:
        return False
    if (bx - ax) * (py - ay) - (by - ay) * (px - ax) != 0:
        return False
    return min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by)


@dataclass
class NetPin:
    designator: str
    pin: str
    pin_name: str
    electrical: str
    sheet: str


@dataclass
class Net:
    name: str
    auto_named: bool
    label_names: list
    power_names: list
    pins: list = field(default_factory=list)
    sheets: list = field(default_factory=list)

    def pin_keys(self):
        return sorted((p.designator, p.pin) for p in self.pins)


# How much each kind of finding matters. "info" covers drafting style that Altium accepts,
# such as a stub wire whose far end is left free after a net label.
SEVERITY = {
    "duplicate_designator": "error", "unannotated": "error", "multiple_outputs": "error",
    "unconnected_pin": "warning", "single_pin_net": "warning", "net_without_pins": "warning",
    "floating_net_label": "warning", "floating_power_port": "warning", "empty_net_label": "warning",
    "multiple_net_names": "warning", "t_without_junction": "warning", "duplicate_net_name": "error",
    "dangling_wire_end": "info", "hierarchy_not_followed": "info", "bus_not_followed": "info",
}


@dataclass
class Finding:
    kind: str
    sheet: str
    detail: str
    at_mils: tuple = None

    @property
    def severity(self):
        return SEVERITY.get(self.kind, "warning")

    def as_dict(self):
        d = {"severity": self.severity, "kind": self.kind, "sheet": self.sheet, "detail": self.detail}
        if self.at_mils is not None:
            d["at_mils"] = list(self.at_mils)
        return d


def _mils(pt):
    return (round(to_mils(pt[0]), 3), round(to_mils(pt[1]), 3))


def sheet_clusters(doc: SchDoc, t_connects=False):
    """Group one sheet's objects into connected clusters.

    Returns (clusters, findings). Each cluster is a dict with lists `pins` (indexes into
    doc.pins), `labels` and `power` (texts), and `wires` (count).
    """
    dsu = DSU()
    findings = []
    at_point = defaultdict(list)          # exact point -> object keys found there
    for wi, w in enumerate(doc.wires):
        dsu.find(("w", wi))
        for pt in w.points:
            at_point[pt].append(("w", wi))
    for pi, p in enumerate(doc.pins):
        dsu.find(("p", pi))
        at_point[p.hot].append(("p", pi))
    for li, lab in enumerate(doc.net_labels):
        dsu.find(("l", li))
        at_point[lab.at].append(("l", li))
    for ki, pp in enumerate(doc.power_ports):
        dsu.find(("pp", ki))
        at_point[pp.at].append(("pp", ki))
    junctions = set(doc.junctions)
    for ji, j in enumerate(doc.junctions):
        dsu.find(("j", ji))
        at_point[j].append(("j", ji))

    # 1. everything sharing an exact point is joined
    for keys in at_point.values():
        for k in keys[1:]:
            dsu.union(keys[0], k)

    # 2. points that land inside another wire's segment
    segments = [(wi, a, b) for wi, w in enumerate(doc.wires) for a, b in zip(w.points, w.points[1:])]
    for pt, keys in at_point.items():
        kinds = {k[0] for k in keys}
        own_wires = {k[1] for k in keys if k[0] == "w"}
        for wi, a, b in segments:
            if wi in own_wires or not _inside(pt, a, b):
                continue
            if kinds & {"p", "l", "pp", "j"}:
                dsu.union(keys[0], ("w", wi))
            if "w" in kinds and pt not in junctions:
                findings.append(Finding("t_without_junction", doc.name,
                                        "a wire end or bend lands inside another wire with no junction",
                                        _mils(pt)))
                if t_connects:
                    dsu.union(keys[0], ("w", wi))

    clusters = defaultdict(lambda: {"pins": [], "labels": [], "power": [], "wires": 0, "points": []})
    for wi in range(len(doc.wires)):
        clusters[dsu.find(("w", wi))]["wires"] += 1
    for pi, p in enumerate(doc.pins):
        c = clusters[dsu.find(("p", pi))]
        c["pins"].append(pi)
        c["points"].append(p.hot)
    for li, lab in enumerate(doc.net_labels):
        c = clusters[dsu.find(("l", li))]
        if lab.text.strip():
            c["labels"].append(lab.text)
        else:
            findings.append(Finding("empty_net_label", doc.name, "net label with no text", _mils(lab.at)))
        c["points"].append(lab.at)
    for ki, pp in enumerate(doc.power_ports):
        c = clusters[dsu.find(("pp", ki))]
        c["power"].append(pp.text)
        c["points"].append(pp.at)

    # objects that touch nothing
    for li, lab in enumerate(doc.net_labels):
        c = clusters[dsu.find(("l", li))]
        if not c["wires"] and not c["pins"]:
            findings.append(Finding("floating_net_label", doc.name, lab.text or "(no text)", _mils(lab.at)))
    for ki, pp in enumerate(doc.power_ports):
        c = clusters[dsu.find(("pp", ki))]
        if not c["wires"] and not c["pins"]:
            findings.append(Finding("floating_power_port", doc.name, pp.text, _mils(pp.at)))
    no_erc = set(doc.no_ercs)
    for pi, p in enumerate(doc.pins):
        c = clusters[dsu.find(("p", pi))]
        if len(c["pins"]) == 1 and not c["wires"] and not c["labels"] and not c["power"] \
                and not p.hidden_net and p.hot not in no_erc:
            comp = doc.components[p.comp]
            findings.append(Finding("unconnected_pin", doc.name,
                                    "%s pin %s (%s)" % (comp.designator, p.number, p.name), _mils(p.hot)))
    # wire ends that reach nothing
    for wi, w in enumerate(doc.wires):
        for end in (w.points[0], w.points[-1]):
            others = [k for k in at_point[end] if k != ("w", wi)]
            if others:
                continue
            if any(_inside(end, a, b) for wj, a, b in segments if wj != wi):
                continue
            findings.append(Finding("dangling_wire_end", doc.name, "wire end touches nothing", _mils(end)))
    if doc.sheet_symbols or doc.sheet_entries or doc.ports:
        findings.append(Finding("hierarchy_not_followed", doc.name,
                                "%d sheet symbols, %d sheet entries, %d ports: their connections are not followed"
                                % (len(doc.sheet_symbols), len(doc.sheet_entries), len(doc.ports))))
    if doc.buses:
        findings.append(Finding("bus_not_followed", doc.name, "%d bus objects ignored" % doc.buses))
    return list(clusters.values()), findings


class Project:
    """The schematic sheets of a .PrjPcb and the project options that affect nets."""

    def __init__(self, path):
        self.path = Path(path)
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read_string(self.path.read_text(encoding="utf-8-sig", errors="replace"))   # Altium writes a BOM
        self.sheet_paths = []
        self.pcb_paths = []
        for sec in sorted((s for s in cp.sections() if re.fullmatch(r"Document\d+", s)),
                          key=lambda s: int(s[8:])):
            doc = cp[sec].get("DocumentPath", "")
            full = (self.path.parent / doc.replace("\\", "/")).resolve()
            if doc.lower().endswith(".schdoc"):
                self.sheet_paths.append(full)
            elif doc.lower().endswith(".pcbdoc"):
                self.pcb_paths.append(full)
        design = cp["Design"] if cp.has_section("Design") else {}
        self.hierarchy_mode = int(design.get("HierarchyMode", "0") or 0)
        self.power_names_first = design.get("PowerPortNamesTakePriority", "0") == "1"

    def load_sheets(self):
        return [SchDoc.load(p) for p in self.sheet_paths]


def resolve_scope(hierarchy_mode, docs):
    """Altium's Automatic scope: sheet entries -> hierarchical, else ports -> flat, else global."""
    scope = SCOPES.get(hierarchy_mode, "automatic")
    if scope != "automatic":
        return scope
    if any(d.sheet_entries for d in docs):
        return "hierarchical"
    if any(d.ports for d in docs):
        return "flat"
    return "global"


class Netlist:
    def __init__(self, nets, components, findings, scope):
        self.nets = nets
        self.components = components      # designator -> dict
        self.findings = findings
        self.scope = scope

    def pin_to_net(self):
        return {(p.designator, p.pin): n.name for n in self.nets for p in n.pins}

    def net(self, name):
        for n in self.nets:
            if n.name == name:
                return n
        return None

    def summary(self):
        kinds, levels = defaultdict(int), defaultdict(int)
        for f in self.findings:
            kinds[f.kind] += 1
            levels[f.severity] += 1
        return {"scope": self.scope, "components": len(self.components), "nets": len(self.nets),
                "named_nets": sum(1 for n in self.nets if not n.auto_named),
                "pins": sum(len(n.pins) for n in self.nets),
                "findings_by_severity": dict(levels), "findings": dict(kinds)}


def build_netlist(docs, scope="global", power_names_first=False, t_connects=False):
    """Join the sheets' clusters into project nets."""
    findings = []
    dsu = DSU()
    sheet_cl = []
    for si, doc in enumerate(docs):
        clusters, f = sheet_clusters(doc, t_connects)
        findings.extend(f)
        for ci, c in enumerate(clusters):
            key = ("c", si, ci)
            dsu.find(key)
            sheet_cl.append((si, ci, c))
            # Same-named objects on one sheet always join, whatever their kind (a GND net label
            # joins a GND power port). Power ports and hidden pins are also global; net labels
            # are global only in global scope.
            for name in c["power"] + [docs[si].pins[pi].hidden_net for pi in c["pins"]]:
                if name:
                    dsu.union(key, ("name", name))
                    dsu.union(key, ("sheet_name", si, name))
            for name in c["labels"]:
                dsu.union(key, ("name", name) if scope == "global" else ("sheet_name", si, name))

    groups = defaultdict(list)
    for si, ci, c in sheet_cl:
        groups[dsu.find(("c", si, ci))].append((si, c))

    components = {}
    for doc in docs:
        for comp in doc.components.values():
            if comp.designator in components:
                findings.append(Finding("duplicate_designator", doc.name,
                                        "%s also on %s" % (comp.designator, components[comp.designator]["sheet"])))
            if "?" in comp.designator or not comp.designator:
                findings.append(Finding("unannotated", doc.name, comp.designator or comp.libref))
            components[comp.designator] = {
                "designator": comp.designator, "libref": comp.libref, "comment": comp.comment,
                "description": comp.description, "design_item_id": comp.design_item_id,
                "library": comp.library, "sheet": doc.name, "params": dict(comp.params),
                "item_guid": comp.item_guid, "revision_guid": comp.revision_guid}

    nets = []
    for members in groups.values():
        pins, labels, power, sheets = [], set(), set(), set()
        for si, c in members:
            doc = docs[si]
            labels.update(c["labels"])
            power.update(c["power"])
            for pi in c["pins"]:
                p = doc.pins[pi]
                pins.append(NetPin(doc.components[p.comp].designator, p.number, p.name, p.electrical, doc.name))
                sheets.add(doc.name)
                if p.hidden_net:
                    power.add(p.hidden_net)
            if c["labels"] or c["power"]:
                sheets.add(doc.name)
        if not pins and not labels and not power:
            continue                                  # bare wire fragments
        first, second = (power, labels) if power_names_first else (labels, power)
        names = sorted(first, key=natural_key) + sorted(second, key=natural_key)
        pins.sort(key=lambda p: (natural_key(p.designator), natural_key(p.pin)))
        if names:
            name, auto = names[0], False
        elif pins:
            name, auto = "Net%s_%s" % (pins[0].designator, pins[0].pin), True
        else:
            continue
        if len(set(names)) > 1:
            findings.append(Finding("multiple_net_names", ",".join(sorted(sheets)),
                                    "one net carries the names %s" % ", ".join(names)))
        if len(pins) == 1:
            findings.append(Finding("single_pin_net", pins[0].sheet,
                                    "%s has only %s pin %s" % (name, pins[0].designator, pins[0].pin)))
        if not pins:
            findings.append(Finding("net_without_pins", ",".join(sorted(sheets)), name))
        drivers = [p for p in pins if p.electrical == "output"]
        if len({p.designator for p in drivers}) > 1:      # one part's tied output pins are fine
            findings.append(Finding("multiple_outputs", ",".join(sorted(sheets)),
                                    "%s is driven by %s" % (name, ", ".join("%s.%s" % (p.designator, p.pin) for p in drivers))))
        nets.append(Net(name, auto, sorted(labels, key=natural_key), sorted(power, key=natural_key),
                        pins, sorted(sheets)))
    nets.sort(key=lambda n: (n.auto_named, natural_key(n.name)))
    seen = defaultdict(int)
    for n in nets:
        seen[n.name] += 1
    for name, count in seen.items():
        if count > 1:
            findings.append(Finding("duplicate_net_name", "", "%d separate nets are all named %s" % (count, name)))
    return Netlist(nets, components, findings, scope)


def project_netlist(prjpcb, t_connects=False):
    proj = Project(prjpcb)
    docs = proj.load_sheets()
    scope = resolve_scope(proj.hierarchy_mode, docs)
    return build_netlist(docs, scope, proj.power_names_first, t_connects), proj, docs
