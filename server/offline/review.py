"""Layout review for power: the checks a reviewer of a high-current board
makes by eye, as a list.

    python -m offline review-power board.PcbDoc [--current VBAT=30 ...] [--edge 1.0]
        [--rise 10] [--json]

- joins: pads of power nets that meet a pour of their net through a thermal
  relief (spokes x width x copper is all the current gets), with the density
  in the spokes at the pad's share of the current
- vias per amp: each power net's vias against the IPC-2221 figure for its
  barrels at a `rise` C rise (a layer change carries the current)
- narrowest copper: the narrowest track or arc per power net and the IPC-2221
  current it carries at that rise (external layers)
- board edge: parts with a pad closer than `edge` mm to the outline
  (depaneling cracks ceramic capacitors)
- test points: power nets without a test point (a TP* part pad) on them
- polarity: polarized parts (diodes, LEDs, polarized capacitors, ICs) whose
  silkscreen is symmetric about pins 1 and 2, so it shows no polarity

Power nets are those named with --current, else the ones autoroute.power_nets
finds. Screening only: each finding says what it measured.
"""

import json
import math
import re

from . import geom
from . import layers as L

POLARIZED = re.compile(r"^(D|LED|ZD|U|IC|Q|BT|CP|C)\d", re.I)
POLAR_CAP = re.compile(r"(CP|ELEC|TANT|POL|CAPAE|CAE|EEE|PANASONIC_[A-Z]|SMD_ELEC)", re.I)


def _currents(doc, spec):
    out = {}
    for s in spec or []:
        if "=" not in s:
            raise ValueError(f"--current wants NET=AMPS, not {s!r}")
        n, a = s.split("=", 1)
        if not doc.has_net(n):
            raise ValueError(f"no net {n!r}")
        out[doc.nets[doc.net_index(n)].name] = float(a)
    return out


def review(doc, currents=None, edge=1.0, rise=10.0):
    from .autoroute import power_nets
    from .power import barrel_area, ipc2221_current
    from .rules import RuleSet
    sh = geom.require()
    rules = RuleSet(doc)
    currents = dict(currents or {})
    nets = list(currents) if currents else sorted(power_nets(doc))
    idx = {doc.net_index(n): n for n in nets}
    out = {"nets": nets, "currents_a": currents, "joins": [], "vias": [], "narrowest": [],
           "board_edge": [], "test_points": [], "polarity": []}

    # joins: relief spokes on power pads that meet their pour
    polys = {}
    for p in doc.polygons:
        if p.net in idx:
            polys.setdefault((p.net, p.layer), []).append((p, geom.polygon_outline(p)))
    pads_of = {}
    for p in doc.pads:
        if p.effective_net in idx:
            pads_of.setdefault(p.effective_net, []).append(p)
    for net, pads in pads_of.items():
        amps = currents.get(idx[net])
        for p in pads:
            for lay in doc.copper_layers:
                if not p.on_layer(lay):
                    continue
                for poly, outline in polys.get((net, lay), ()):
                    if not outline.intersects(geom.pad_shape(p, lay)):
                        continue
                    region = _probe_region(doc, poly)
                    pc = rules.polygon_connect(p, region) if region is not None else None
                    if not pc or pc["style"] != "relief":
                        continue
                    t = doc.copper_thickness(lay)
                    area = pc["entries"] * pc["conductor_width"] * t
                    row = {"pad": _pad_ref(doc, p), "net": idx[net], "layer": L.name(lay),
                           "polygon": poly.name or poly.index, "spokes": pc["entries"],
                           "spoke_width_mm": pc["conductor_width"],
                           "spoke_copper_mm2": round(area, 5), "rule": pc["rule"]}
                    if amps:
                        share = amps / max(1, len(pads))
                        row["pad_current_a"] = round(share, 3)
                        row["spoke_density_a_per_mm2"] = round(share / area, 1) if area else None
                    out["joins"].append(row)
    out["joins"].sort(key=lambda r: -(r.get("spoke_density_a_per_mm2") or 0))

    # vias per amp
    for net, name in idx.items():
        vias = [v for v in doc.vias if v.effective_net == net]
        row = {"net": name, "vias": len(vias)}
        amps = currents.get(name)
        if vias:
            per = min(ipc2221_current(barrel_area(v.hole), rise, external=False) for v in vias)
            row["per_via_a"] = round(per, 3)
            if amps:
                row["vias_per_amp"] = round(len(vias) / amps, 3)
                row["needed_for_full_transfer"] = math.ceil(amps / per)
                row["ok"] = len(vias) * per >= amps
        elif amps:
            row["ok"] = None
            row["note"] = "no vias: fine only if the net stays on one layer"
        out["vias"].append(row)

    # narrowest copper per power net
    for net, name in idx.items():
        objs = [t for t in list(doc.tracks) + list(doc.arcs)
                if t.effective_net == net and t.polygon is None and t.width > 0]
        if not objs:
            continue
        o = min(objs, key=lambda t: t.width)
        t = doc.copper_thickness(o.layer)
        cap = ipc2221_current(o.width * t, rise, external=o.layer in (L.TOP, L.BOTTOM))
        row = {"net": name, "width_mm": round(o.width, 4), "layer": L.name(o.layer),
               "at": [round(c, 3) for c in (o.start if o.KIND == "track" else (o.cx, o.cy))],
               "ipc2221_a": round(cap, 2)}
        amps = currents.get(name)
        if amps:
            row["ok"] = cap >= amps
        out["narrowest"].append(row)

    # parts near the board edge
    board = geom.board_shape(doc)
    if board is not None:
        rim = board.boundary
        for c in doc.components:
            pads = [p for p in doc.pads if p.component == c.index]
            if not pads:
                continue
            d = min(rim.distance(geom.pad_shape(p, p.layer if p.layer != L.MULTI else L.TOP))
                    for p in pads)
            if d < edge:
                out["board_edge"].append({"component": c.designator, "pattern": c.pattern,
                                          "distance_mm": round(d, 3)})
        out["board_edge"].sort(key=lambda r: r["distance_mm"])

    # test points on power nets
    tp_nets = set()
    for p in doc.pads:
        if p.component is not None and p.effective_net is not None and \
                re.match(r"^TP", doc.components[p.component].designator or "", re.I):
            tp_nets.add(p.effective_net)
    for net, name in idx.items():
        if net not in tp_nets:
            out["test_points"].append({"net": name, "note": "no test point (TP*) on this net"})

    # polarity marking
    over = {}
    for prim in list(doc.tracks) + list(doc.arcs) + list(doc.texts):
        if prim.layer in (L.TOP_OVERLAY, L.BOTTOM_OVERLAY) and prim.component is not None \
                and not getattr(prim, "hidden", False):
            over.setdefault(prim.component, []).append(prim)
    for c in doc.components:
        if not _polarized(c):
            continue
        pads = {p.name: p for p in doc.pads if p.component == c.index}
        p1 = pads.get("1") or pads.get("A") or pads.get("+")
        p2 = pads.get("2") or pads.get("K") or pads.get("-")
        if p1 is None or p2 is None:
            continue
        marks = [_overlay_shape(o) for o in over.get(c.index, [])]
        marks = [m for m in marks if m is not None and not m.is_empty]
        if not marks:
            out["polarity"].append({"component": c.designator, "pattern": c.pattern,
                                    "note": "no silkscreen at all"})
            continue
        silk = sh.union_all(marks)
        d1 = silk.distance(sh.Point(p1.x, p1.y))
        d2 = silk.distance(sh.Point(p2.x, p2.y))
        if abs(d1 - d2) < 0.1 and _symmetric(silk, p1, p2):
            out["polarity"].append({"component": c.designator, "pattern": c.pattern,
                                    "note": "silkscreen is symmetric about pins 1 and 2"})
    out["summary"] = {k: len(v) for k, v in out.items() if isinstance(v, list) and k != "nets"}
    out["summary"]["vias_short"] = sum(1 for r in out["vias"] if r.get("ok") is False)
    out["summary"]["necks_short"] = sum(1 for r in out["narrowest"] if r.get("ok") is False)
    return out


def _probe_region(doc, poly):
    """A poured region of the polygon (what the PolygonConnect rule scopes
    against), or a probe region on its layer and net."""
    for r in doc.regions:
        if r.polygon == poly.index:
            return r
    from . import records as R
    probe = R.region(doc, poly.layer, [(0, 0), (1, 0), (1, 1)], (), poly.net,
                     polygon=poly.index)[1]
    probe.storage = f"review-probe:{poly.index}"
    return probe


def _pad_ref(doc, p):
    if p.component is not None:
        return f"{doc.components[p.component].designator}.{p.name}"
    return f"free pad {p.name} at ({p.x:.3f}, {p.y:.3f})"


def _polarized(c):
    des = c.designator or ""
    if not POLARIZED.match(des):
        return False
    if re.match(r"^C\d", des, re.I):
        return bool(POLAR_CAP.search(c.pattern or ""))
    return True


def _overlay_shape(o):
    if o.KIND == "track":
        return geom.track_shape(o)
    if o.KIND == "arc":
        return geom.arc_shape(o)
    if o.KIND == "text":
        return geom.text_box(o)
    return None


def _symmetric(silk, p1, p2):
    """Whether the silkscreen looks the same mirrored across the line
    halfway between the two pads (a marking would break that)."""
    from shapely import affinity
    mx, my = (p1.x + p2.x) / 2, (p1.y + p2.y) / 2
    ang = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
    g = affinity.rotate(silk, -ang, origin=(mx, my))
    m = affinity.scale(g, xfact=-1, yfact=1, origin=(mx, my))
    diff = g.symmetric_difference(m).area
    return diff < 0.05 * max(g.area, 1e-9)


def cmd_review_power(args):
    from .pcbdoc import PcbDoc
    doc = PcbDoc.open(args.board)
    rep = review(doc, _currents(doc, args.current), edge=args.edge, rise=args.rise)
    if args.json:
        print(json.dumps(rep, indent=1))
        return 0
    print(f"power nets: {', '.join(rep['nets']) or '(none found)'}")
    for r in rep["joins"]:
        dens = f", {r['spoke_density_a_per_mm2']} A/mm2 in the spokes" \
            if r.get("spoke_density_a_per_mm2") is not None else ""
        print(f"  relief join: {r['pad']} ({r['net']}, {r['layer']}) {r['spokes']} x "
              f"{r['spoke_width_mm']} mm spokes{dens}")
    for r in rep["vias"]:
        if r.get("ok") is False:
            print(f"  vias: {r['net']} has {r['vias']} (each {r['per_via_a']} A); "
                  f"{r['needed_for_full_transfer']} carry it all")
    for r in rep["narrowest"]:
        flag = " (short)" if r.get("ok") is False else ""
        print(f"  narrowest: {r['net']} {r['width_mm']} mm on {r['layer']} at {r['at']}: "
              f"{r['ipc2221_a']} A at a {args.rise} C rise{flag}")
    for r in rep["board_edge"]:
        print(f"  near the edge: {r['component']} ({r['pattern']}) {r['distance_mm']} mm")
    for r in rep["test_points"]:
        print(f"  no test point: {r['net']}")
    for r in rep["polarity"]:
        print(f"  polarity: {r['component']} ({r['pattern']}): {r['note']}")
    print("summary: " + ", ".join(f"{k} {v}" for k, v in rep["summary"].items()))
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("board")
        p.add_argument("--current", nargs="+", metavar="NET=AMPS")
        p.add_argument("--edge", type=float, default=1.0, help="mm from the outline")
        p.add_argument("--rise", type=float, default=10.0, help="C, for the IPC-2221 figures")
        p.add_argument("--json", action="store_true")
    command("review-power", cmd_review_power, "power layout review: joins, vias, necks, edge, "
            "test points, polarity", build)
