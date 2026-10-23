"""Working with schematic netlists: load from any source, compare, BOM, part view, rule checks.

A netlist here is the plain form every source can give:
    nets: {net name: set of (designator, pin number)}
    components: {designator: dict of what the source knows about the part}
Sources: a .PrjPcb (all its sheets) or one .SchDoc, through `schnet`; a .PcbDoc, from
which net each pad carries; or a text dump of Altium's compiled netlist, one line per
net as `NET|name|U1.3|R5.1|...` (dev/exp_compiled_netlist.pas writes one).
"""
import re
from collections import defaultdict
from pathlib import Path

from .schnet import Netlist, build_netlist, natural_key, project_netlist, resolve_scope
from .schdoc import SchDoc


class Plain:
    def __init__(self, nets, components, source, kind, netlist=None):
        self.nets = nets
        self.components = components
        self.source = source
        self.kind = kind
        self.netlist = netlist          # the full schnet.Netlist for schematic sources

    def pin_net(self):
        return {pin: name for name, pins in self.nets.items() for pin in pins}


def _from_netlist(nl: Netlist, source, kind):
    nets = {n.name: {(p.designator, p.pin) for p in n.pins} for n in nl.nets}
    return Plain(nets, nl.components, source, kind, nl)


def load_any(path, t_connects=False):
    """Load a netlist from a .PrjPcb, .SchDoc, .PcbDoc or compiled-netlist text file."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".prjpcb":
        nl, _, _ = project_netlist(p, t_connects)
        return _from_netlist(nl, str(p), "schematic project")
    if ext == ".schdoc":
        docs = [SchDoc.load(p)]
        nl = build_netlist(docs, resolve_scope(0, docs), t_connects=t_connects)
        return _from_netlist(nl, str(p), "single sheet")
    if ext == ".pcbdoc":
        from .pcbdoc import PcbDoc
        doc = PcbDoc.load(str(p))
        by_idx = {c.idx: c for c in doc.components}
        nets = defaultdict(set)
        for pad in doc.pads:
            comp = by_idx.get(pad.comp)
            if comp is not None and 0 <= pad.net < len(doc.nets):
                nets[doc.nets[pad.net].name].add((comp.designator, pad.name))
        comps = {c.designator: {"designator": c.designator, "footprint": c.pattern,
                                "description": c.description} for c in doc.components}
        return Plain(dict(nets), comps, str(p), "pcb")
    nets = {}
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        f = line.split("|")
        if f and f[0] == "NET" and len(f) >= 2:
            nets[f[1]] = {tuple(x.split(".", 1)) for x in f[2:] if "." in x}
    if not nets:
        raise ValueError("%s: not a .PrjPcb, .SchDoc or .PcbDoc, and no NET| lines found" % p.name)
    comps = {d: {"designator": d} for pins in nets.values() for d, _ in pins}
    return Plain(nets, comps, str(p), "compiled netlist")


def _pin_str(pin):
    return "%s.%s" % pin


def compare(a: Plain, b: Plain, ignore_single_pin=True):
    """What changed from netlist a to netlist b.

    Nets are matched by the pins they share, so a renamed net shows as renamed rather
    than as one net removed and another added. With ignore_single_pin, a pin alone on
    its net counts as unconnected on both sides (a PCB gives such pads no net at all).
    """
    def pins_of(x):
        out = {}
        for name, pins in x.nets.items():
            if ignore_single_pin and len(pins) < 2:
                continue
            for pin in pins:
                out[pin] = name
        return out

    pa, pb = pins_of(a), pins_of(b)
    common = set(pa) & set(pb)
    a_to_b, b_to_a = defaultdict(set), defaultdict(set)
    for pin in common:
        a_to_b[pa[pin]].add(pb[pin])
        b_to_a[pb[pin]].add(pa[pin])
    key = lambda pin: (natural_key(pin[0]), natural_key(pin[1]))
    res = {"a": {"source": a.source, "kind": a.kind}, "b": {"source": b.source, "kind": b.kind}}
    res["components_removed"] = sorted(set(a.components) - set(b.components), key=natural_key)
    res["components_added"] = sorted(set(b.components) - set(a.components), key=natural_key)
    changed = []
    for d in sorted(set(a.components) & set(b.components), key=natural_key):
        for field in ("libref", "comment", "design_item_id", "footprint"):
            va, vb = a.components[d].get(field), b.components[d].get(field)
            if va and vb and va != vb:
                changed.append({"designator": d, "field": field, "from": va, "to": vb})
    res["components_changed"] = changed
    res["nets_split"] = [{"net": n, "into": sorted(bs, key=natural_key)}
                         for n, bs in sorted(a_to_b.items()) if len(bs) > 1]
    res["nets_merged"] = [{"net": n, "from": sorted(as_, key=natural_key)}
                          for n, as_ in sorted(b_to_a.items()) if len(as_) > 1]
    res["nets_renamed"] = [{"from": n, "to": next(iter(bs))} for n, bs in sorted(a_to_b.items())
                           if len(bs) == 1 and len(b_to_a[next(iter(bs))]) == 1 and n not in bs]
    res["pins_removed"] = [{"pin": _pin_str(p), "net": pa[p]} for p in sorted(set(pa) - set(pb), key=key)]
    res["pins_added"] = [{"pin": _pin_str(p), "net": pb[p]} for p in sorted(set(pb) - set(pa), key=key)]
    res["nets_removed"] = sorted({pa[p] for p in pa} - set(a_to_b), key=natural_key)
    res["nets_added"] = sorted({pb[p] for p in pb} - set(b_to_a), key=natural_key)
    res["pins_compared"] = len(common)
    res["identical"] = not any(res[k] for k in ("components_removed", "components_added", "components_changed",
                                                 "nets_split", "nets_merged", "nets_renamed", "pins_removed",
                                                 "pins_added", "nets_removed", "nets_added"))
    return res


def format_compare(res):
    out = ["%s (%s) -> %s (%s): %d pins in both" % (res["a"]["source"], res["a"]["kind"], res["b"]["source"],
                                                     res["b"]["kind"], res["pins_compared"])]
    if res["identical"]:
        return out + ["No differences in components or connectivity."]
    for k, label in (("components_removed", "components removed"), ("components_added", "components added"),
                     ("nets_removed", "nets removed"), ("nets_added", "nets added")):
        if res[k]:
            out.append("%s (%d): %s" % (label, len(res[k]), ", ".join(res[k])))
    for c in res["components_changed"]:
        out.append("changed %s %s: %s -> %s" % (c["designator"], c["field"], c["from"], c["to"]))
    for s in res["nets_split"]:
        out.append("net %s split into %s" % (s["net"], ", ".join(s["into"])))
    for m in res["nets_merged"]:
        out.append("nets %s merged into %s" % (", ".join(m["from"]), m["net"]))
    for r in res["nets_renamed"]:
        out.append("net renamed %s -> %s" % (r["from"], r["to"]))
    for k, verb in (("pins_removed", "no longer on"), ("pins_added", "now on")):
        by_net = defaultdict(list)
        for x in res[k]:
            by_net[x["net"]].append(x["pin"])
        for net in sorted(by_net, key=natural_key):
            out.append("%s %s: %s" % (verb, net, ", ".join(by_net[net])))
    return out


# ---- BOM --------------------------------------------------------------------

MPN_KEY = re.compile(r"^(manufacturer[ _]?part[ _]?number|mfr[ _.]?part[ _]?(number|no)?|mpn|part[ _]?number)( ?1)?$", re.I)
MFR_KEY = re.compile(r"^(manufacturer|manufacturer[ _]?name|mfr)( ?1)?$", re.I)
# Ordering suffixes that distributors' databases often leave out: Infineon packing codes
# (XUMA1, XTMA1, AUMA1 ...), tape and reel, lead-free markers.
PACKING = re.compile(r"(X[A-Z]{3}\d|A[A-Z]{3}\d|[-/#](TR|T|CT|PBF|ND))$", re.I)


def mpn_search_terms(mpn):
    """Spellings of an MPN worth trying in a parts search, most specific first."""
    terms = []
    for t in (mpn, PACKING.sub("", mpn)):
        t = t.strip()
        if t and t not in terms:
            terms.append(t)
    m = re.match(r"([A-Za-z]+)(\d+)", terms[-1]) if terms else None
    if m:
        # Family stems. Some makers drop a hyphen from the orderable code (BTS7008-1EPP is
        # ordered as BTS70081EPPXUMA1), running the digits together, so a 4-digit stem is
        # offered as well when the digit run is longer.
        stems = [m.group(0)]
        if len(m.group(2)) > 4:
            stems.append(m.group(1) + m.group(2)[:4])
        for t in stems:
            if t not in terms and len(t) >= 5:
                terms.append(t)
    return terms


def bom(plain: Plain):
    groups = {}
    for d, c in plain.components.items():
        params = c.get("params", {})
        mpn = next((v for k, v in params.items() if MPN_KEY.match(k.strip()) and v.strip()), "")
        mfr = next((v for k, v in params.items() if MFR_KEY.match(k.strip()) and v.strip()), "")
        key = (c.get("design_item_id") or c.get("libref", ""), c.get("comment", ""), mpn)
        g = groups.setdefault(key, {"design_item_id": c.get("design_item_id", ""), "libref": c.get("libref", ""),
                                    "comment": c.get("comment", ""), "description": c.get("description", ""),
                                    "manufacturer": mfr, "mpn": mpn,
                                    "search_terms": mpn_search_terms(mpn) if mpn else [],
                                    "designators": []})
        g["designators"].append(d)
    rows = []
    for g in groups.values():
        g["designators"].sort(key=natural_key)
        g["quantity"] = len(g["designators"])
        rows.append(g)
    rows.sort(key=lambda g: natural_key(g["designators"][0]))
    return rows


# ---- one part and what it connects to ---------------------------------------

def part_view(plain: Plain, designator, crowd=8):
    """Each pin of a part, its net, and the other parts on that net.

    Nets with more than `crowd` pins (ground, supplies) are summarised by count.
    """
    if plain.netlist is None:
        raise ValueError("part_view needs a schematic source (.PrjPcb or .SchDoc)")
    comps = plain.components
    if designator not in comps:
        raise ValueError("no component %s" % designator)
    rows = []
    for net in plain.netlist.nets:
        mine = [p for p in net.pins if p.designator == designator]
        for p in mine:
            others = [q for q in net.pins if not (q.designator == designator and q.pin == p.pin)]
            row = {"pin": p.pin, "name": p.pin_name, "electrical": p.electrical, "net": net.name,
                   "net_pins": len(net.pins)}
            if not others:
                row["unconnected"] = True
            elif len(others) > crowd:
                row["others"] = "%d pins" % len(others)
            else:
                row["others"] = ["%s.%s %s" % (q.designator, q.pin, comps.get(q.designator, {}).get("comment", ""))
                                 for q in others]
            rows.append(row)
    rows.sort(key=lambda r: natural_key(r["pin"]))
    c = comps[designator]
    return {"designator": designator, "comment": c.get("comment", ""), "libref": c.get("libref", ""),
            "description": c.get("description", ""), "sheet": c.get("sheet", ""), "pins": rows,
            "unconnected_pins": [r["pin"] for r in rows if r.get("unconnected")]}


# ---- rule checks ------------------------------------------------------------

EXPECTS = ("connected", "net_name", "same_net", "resistor_to", "capacitor_to", "diode_to", "two_pin_to",
           "connects_to")
DEFAULT_PREFIX = {"resistor_to": "R", "capacitor_to": "C", "diode_to": "D|TVS|Z"}


def _matches(rx, d, comp):
    fields = [d, comp.get("comment", ""), comp.get("libref", ""), comp.get("design_item_id", ""),
              comp.get("description", "")]
    fields += [v for k, v in comp.get("params", {}).items() if MPN_KEY.match(k.strip())]
    return any(re.search(rx, f or "", re.I) for f in fields)


def check_rules(plain: Plain, rules):
    """Evaluate rules against a schematic netlist. Returns one result per rule and part.

    A rule is a dict:
        parts   regex searched in each part's designator, comment, library reference,
                design item ID, description and MPN
        pin     regex for pin NAMES (whole name, any case), or "#3" for pin number 3
        expect  connected    every matching pin has company on its net
                net_name     every matching pin's net name matches `net`
                same_net     all matching pins of the part share one net
                resistor_to / capacitor_to / diode_to / two_pin_to
                             each matching pin's net has a two-pin part (designator
                             prefix `prefix`, defaulting to R, C or D) whose other end
                             is on a net matching `net`
                connects_to  each matching pin's net reaches a part matching `part`,
                             directly or, with `via` (a designator prefix), through one
                             two-pin part in series
        name    optional label for the report
    """
    if plain.netlist is None:
        raise ValueError("rule checks need a schematic source (.PrjPcb or .SchDoc)")
    nl = plain.netlist
    comps = plain.components
    pin_net, pin_name, by_comp = {}, {}, defaultdict(list)
    for net in nl.nets:
        for p in net.pins:
            pin_net[(p.designator, p.pin)] = net
            pin_name[(p.designator, p.pin)] = p.pin_name
            by_comp[p.designator].append(p.pin)

    def two_pin_bridges(net, prefix_rx):
        """(designator, net at the other end) for two-terminal parts touching net.

        A part counts as two-terminal when its connected pins land on exactly two nets, so a
        dual-anode diode (two anode pins on ground, cathode on the output) or a Zener with an
        unconnected NC pin qualifies like a plain diode.
        """
        out, seen = [], set()
        for p in net.pins:
            if p.designator in seen or not re.match(r"(%s)\d" % prefix_rx, p.designator, re.I):
                continue
            seen.add(p.designator)
            # pins left unconnected (an NC pin alone on an unnamed net) do not count as a terminal;
            # a pin alone on a named net (a GND power port) does
            ends = {pin_net[(p.designator, q)].name: pin_net[(p.designator, q)] for q in by_comp[p.designator]
                    if len(pin_net[(p.designator, q)].pins) > 1 or not pin_net[(p.designator, q)].auto_named}
            if len(ends) == 2:
                other = [n for name, n in ends.items() if name != net.name]
                if other:
                    out.append((p.designator, other[0]))
        return out

    results = []
    for ri, rule in enumerate(rules):
        expect = rule.get("expect")
        label = rule.get("name") or "%s %s %s" % (rule.get("parts"), rule.get("pin"), expect)
        if expect not in EXPECTS:
            results.append({"rule": label, "status": "error", "detail": "unknown expect %r; use one of %s"
                            % (expect, ", ".join(EXPECTS))})
            continue
        targets = [d for d, c in comps.items() if _matches(rule.get("parts", "$^"), d, c)]
        if not targets:
            results.append({"rule": label, "status": "no_match", "detail": "no part matches %r" % rule.get("parts")})
            continue
        prx = rule.get("pin", ".*")
        for d in sorted(targets, key=natural_key):
            if prx.startswith("#"):
                pins = [n for n in by_comp[d] if n == prx[1:]]
            else:
                pins = [n for n in by_comp[d] if re.fullmatch(prx, pin_name[(d, n)], re.I)]
            if not pins:
                results.append({"rule": label, "designator": d, "status": "fail",
                                "detail": "no pin named %r on %s" % (prx, d)})
                continue
            fails = []
            if expect == "same_net":
                nets = {pin_net[(d, n)].name for n in pins}
                if len(nets) > 1:
                    fails.append("pins %s are on %s" % (",".join(pins), ", ".join(sorted(nets))))
            for n in pins if expect != "same_net" else []:
                net = pin_net[(d, n)]
                where = "%s.%s (%s) on %s" % (d, n, pin_name[(d, n)], net.name)
                if expect == "connected" and len(net.pins) < 2:
                    fails.append("%s is unconnected" % where)
                elif expect == "net_name" and not re.fullmatch(rule.get("net", ".*"), net.name, re.I):
                    fails.append("%s does not match %r" % (where, rule.get("net")))
                elif expect in DEFAULT_PREFIX or expect == "two_pin_to":
                    prefix = rule.get("prefix") or DEFAULT_PREFIX.get(expect, "[A-Z]+")
                    hits = [(b, o.name) for b, o in two_pin_bridges(net, prefix)
                            if re.fullmatch(rule.get("net", ".*"), o.name, re.I)]
                    if not hits:
                        fails.append("%s has no %s part to %r" % (where, prefix, rule.get("net")))
                elif expect == "connects_to":
                    want = rule.get("part", "$^")
                    direct = [q.designator for q in net.pins
                              if q.designator != d and _matches(want, q.designator, comps.get(q.designator, {}))]
                    hop = []
                    if not direct and rule.get("via"):
                        for b, other in two_pin_bridges(net, rule["via"]):
                            hop += ["%s via %s" % (q.designator, b) for q in other.pins
                                    if q.designator not in (d, b)
                                    and _matches(want, q.designator, comps.get(q.designator, {}))]
                    if not direct and not hop:
                        fails.append("%s does not reach %r" % (where, want))
            results.append({"rule": label, "designator": d, "status": "fail" if fails else "pass",
                            "detail": "; ".join(fails) if fails else "%d pin(s) checked" % len(pins)})
    return results
