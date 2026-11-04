"""Specctra DSN export and SES import: the files Freerouting reads and writes.

    python -m offline dsn board.PcbDoc -o board.dsn [--nets S1 S2] [--pours fixed]
    python -m offline ses routed.ses board.PcbDoc [-o plan.json]

Normally both run through `python -m offline autoroute`.

The DSN is written in micrometres at a 0.1 um resolution, Y up like the
PcbDoc, so no coordinate is flipped. Each component becomes its own image,
placed at the origin unrotated, with its pins at their board positions and
each pad's shape already rotated: nothing is transformed in either
direction. Ids are made safe (no quotes, no '-' inside a component or pin
id) and mapped back on import.

What goes in: the board outline (cutouts and the outline clearance as
keep-outs), the signal layers, every pad, keep-outs by their restriction
mask, the existing copper as fixed wiring (tracks, arcs as polylines, fills,
regions, vias; poured copper only with pours="fixed" or for a routed net),
and net classes: the nets to route grouped by width, clearance and via, the
rest in one class the router is told to ignore. Format facts come from the
Specctra DSN/SES description as KiCad and Freerouting read it; the code is
this repo's own.
"""

import math
import re

from . import geom
from . import layers as L
from .pcbdoc import KEEPOUT_COPPER, KEEPOUT_TRACK, KEEPOUT_VIA

FIXED_CLASS = "kept_fixed"
_SAFE = re.compile(r"^[A-Za-z0-9_]+$")
UNIT_MM = {"um": 1e-3, "mm": 1.0, "cm": 10.0, "inch": 25.4, "mil": 0.0254}


class SpecctraError(ValueError):
    pass


# ---------------------------------------------------------------- s-expressions

class Quoted(str):
    """A token that was quoted in the file."""


def parse(text):
    """Nested lists of tokens from Specctra s-expression text.

    The quote character is '"' until a (string_quote X) changes it; X itself
    is written bare, so the token after string_quote is one character.
    """
    quote = '"'
    stack = [[]]
    i, n = 0, len(text)
    prev = None
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "(":
            stack.append([])
            i += 1
            prev = "("
            continue
        if c == ")":
            if len(stack) == 1:
                raise SpecctraError(f"unbalanced ')' at offset {i}")
            lst = stack.pop()
            stack[-1].append(lst)
            i += 1
            prev = ")"
            continue
        if prev == "string_quote":
            quote = c
            stack[-1].append(c)
            i += 1
            prev = c
            continue
        if c == quote:
            j = text.find(quote, i + 1)
            if j < 0:
                raise SpecctraError(f"unterminated quoted token at offset {i}")
            tok = Quoted(text[i + 1:j])
            stack[-1].append(tok)
            i = j + 1
            prev = tok
            continue
        j = i
        while j < n and text[j] not in " \t\r\n()":
            j += 1
        tok = text[i:j]
        stack[-1].append(tok)
        i = j
        prev = tok
    if len(stack) != 1:
        raise SpecctraError("unbalanced '(': the file ends inside a list")
    return stack[0]


def find(node, key):
    for x in node:
        if isinstance(x, list) and x and x[0] == key:
            return x
    return None


def find_all(node, key):
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


# ---------------------------------------------------------------- writing

def q(name):
    """A quoted name. Specctra has no escape, so a quote becomes an apostrophe."""
    return '"' + str(name).replace('"', "'") + '"'


def um(v_mm):
    s = f"{v_mm * 1000.0:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return "0" if s in ("-0", "-0.0") else s


def _coords(points):
    return " ".join(f"{um(x)} {um(y)}" for x, y in points)


def _holeless(poly):
    """Polygons without holes covering `poly` (Specctra polygons have none):
    cut through each hole until none is left."""
    sh = geom.require()
    from .pour import pieces
    out, todo = [], [p for p in pieces(poly)]
    while todo:
        p = todo.pop()
        if p.is_empty or p.area < 1e-9:
            continue
        if not p.interiors:
            out.append(p)
            continue
        x = sh.Polygon(p.interiors[0]).representative_point().x
        x0, y0, x1, y1 = p.bounds
        for half in (sh.box(x0 - 1, y0 - 1, x, y1 + 1), sh.box(x, y0 - 1, x1 + 1, y1 + 1)):
            todo.extend(pieces(p.intersection(half)))
    return out


class Dsn:
    """An exported design: the text and the maps the SES import needs."""

    def __init__(self):
        self.text = ""
        self.layer_name = {}        # Altium layer id -> DSN layer name
        self.layer_id = {}          # DSN layer name -> Altium layer id
        self.net_name = {}          # DSN net name -> board net name
        self.via_stacks = {}        # padstack name -> {"diameter", "hole", "low", "high"}
        self.routing_via = None
        self.routed = []            # board net names offered to the router
        self.fixed_class = FIXED_CLASS
        self.classes = []           # [{"name", "nets", "width", "clearance", "via"}]
        self.pins = {}              # "comp-pin" -> (designator, pad name)
        self.stats = {}


def export(doc, route_nets, *, rules=None, pours="repour", via=None, keep_fixed=None,
           name=None):
    """A Dsn for routing `route_nets` (board net names) on `doc`.

    pours: "repour" leaves other nets' poured copper out (it repours around
    new tracks), "fixed" keeps it as fixed copper. via: (diameter, hole) mm
    for new vias; default from the RoutingVias rule. keep_fixed: board net
    names whose existing wiring the router may not move (default: all).
    Everything not routed is in FIXED_CLASS, which the router must ignore.
    """
    from .rules import RuleSet
    sh = geom.require()
    rules = rules or RuleSet(doc)
    out = Dsn()
    route = [n for n in route_nets]
    route_idx = {doc.net_index(n) for n in route}
    fixed_nets = None if keep_fixed is None else {doc.net_index(n) for n in keep_fixed}
    layers = [lay for lay in doc.copper_layers if L.is_signal(lay)]
    if not layers:
        raise SpecctraError("the board has no signal layers")
    for lay in layers:
        nm = re.sub(r"[^A-Za-z0-9_]", "_", L.name(lay))
        out.layer_name[lay] = nm
        out.layer_id[nm] = lay
    board = geom.board_shape(doc)
    if board is None or board.is_empty:
        raise SpecctraError("the board has no outline")

    # net names: DSN names are the board's, made quote-safe; kept unique
    used = set()
    dsn_net = {}
    for i, n in enumerate(doc.nets):
        nm = str(n.name).replace('"', "'")
        if nm in used:
            nm = f"{nm}__{i}"
        used.add(nm)
        dsn_net[i] = nm
        out.net_name[nm] = n.name

    island = _islands(doc, route_idx, pours)
    for k in set(island.values()):
        out.net_name[f"_I{k}"] = None

    def label(obj):
        """The DSN net of a copper object: a routed net's own name, else
        its island's (an island is copper joined without poured copper).
        Each island is complete, so the router finds nothing to connect
        outside the nets it routes, and joined items never clash."""
        net = obj.effective_net
        if net in route_idx:
            return dsn_net[net]
        k = island.get((obj.storage, obj.index))
        return f"_I{k}" if k is not None else None

    lines = []
    w = lines.append
    w(f"(pcb {q(name or 'board')}")
    w("  (parser")
    w('    (string_quote ")')
    w("    (space_in_quoted_tokens on)")
    w('    (host_cad "altium-mcp offline")')
    w('    (host_version "1")')
    w("  )")
    w("  (resolution um 10)")
    w("  (unit um)")

    # ------------------------------------------------ rules
    probe_n = {}

    def probe_track(net):
        if net not in probe_n:
            from . import records as R
            t = R.track(doc, layers[0], 0, 0, 1, 0, 0.2, net)
            t.storage = f"dsn-probe-track:{net}"
            v = R.via(doc, 0, 0, 0.6, 0.3, net)
            v.storage = f"dsn-probe-via:{net}"
            probe_n[net] = (t, v)
        return probe_n[net]

    # what new copper (track, via) meets: copper of other nets, and poured
    # copper only when pours stay as they are
    meets = {"Track", "Arc", "Via", "SMDPad", "THPad", "Fill", "Region"}
    if pours == "fixed":
        meets.add("Poly")

    def worst_clearance(a, b):
        vals = []
        for x in a:
            for y in b:
                c = rules.best("Clearance", x, y)
                if c is None:
                    continue
                gap = c.length("GAP", c.length("GENERICCLEARANCE"))
                for mine in ("Track", "Via"):
                    for other in meets:
                        vals.append(c.matrix.get((mine, other), gap))
        return max(vals) if vals else None

    default_clear = worst_clearance(probe_track(None), probe_track(None)) or 0.2
    default_width = _route_width(rules, probe_track(None)[0], layers, doc) or 0.25
    all_nets = list(range(len(doc.nets))) + [None]
    if via is None:
        vs = rules.via_style(probe_track(None)[1])
        if vs and vs.get("diameter") and vs.get("hole"):
            via = (vs["diameter"], vs["hole"])
        else:
            sizes = {}
            for v in doc.vias:
                k = (round(v.diameter, 4), round(v.hole, 4))
                sizes[k] = sizes.get(k, 0) + 1
            via = max(sizes, key=sizes.get) if sizes else (0.6, 0.3)
    groups = {}
    for n in sorted(route_idx):
        pt = probe_track(n)
        clear = 0.0
        for m in all_nets:
            if m == n:
                continue
            c = worst_clearance(pt, probe_track(m))
            if c is not None:
                clear = max(clear, c)
        clear = clear or default_clear
        width = _route_width(rules, pt[0], layers, doc) or default_width
        vs = rules.via_style(pt[1])
        v = (vs["diameter"], vs["hole"]) if vs and vs.get("diameter") and vs.get("hole") else via
        groups.setdefault((round(width, 4), round(clear, 4), round(v[0], 4), round(v[1], 4)),
                          []).append(n)

    # ------------------------------------------------ padstacks for vias
    stacks = {}          # signature -> name
    stack_text = []

    def via_stack(d, h, lo=None, hi=None):
        span = [lay for lay in layers if lo is None or _spans(lo, hi, lay, doc.copper_layers)]
        through = len(span) == len(layers)
        key = ("via", round(d, 4), round(h, 4), tuple(span))
        if key in stacks:
            return stacks[key]
        nm = f"VIA_{um(d)}_{um(h)}" + ("" if through else "_" + "_".join(
            str(L.name(s)).replace(" ", "") for s in (span[0], span[-1])))
        nm = nm.replace(".", "p")
        stacks[key] = nm
        stack_text.append(f"    (padstack {q(nm)}")
        for lay in span:
            stack_text.append(f"      (shape (circle {out.layer_name[lay]} {um(d)} 0 0))")
        stack_text.append("      (attach off)")
        stack_text.append("    )")
        out.via_stacks[nm] = {"diameter": d, "hole": h,
                              "low": L.name(span[0]), "high": L.name(span[-1])}
        return nm

    route_vias = {}
    for (wd, cl, vd, vh), nets in groups.items():
        route_vias[(vd, vh)] = via_stack(vd, vh)
    out.routing_via = via_stack(via[0], via[1])

    # ------------------------------------------------ structure
    w("  (structure")
    for i, lay in enumerate(layers):
        w(f"    (layer {out.layer_name[lay]} (type signal) (property (index {i})))")
    ext = board if board.geom_type == "Polygon" else max(
        (p for p in getattr(board, "geoms", [board])), key=lambda p: p.area)
    pts = list(ext.exterior.coords)
    w(f"    (boundary (path pcb 0 {_coords(pts)}))")
    keepouts = []
    for hole in ext.interiors:
        keepouts.append(("keepout", "signal", sh.Polygon(hole)))
    edge = _edge_gap(doc, rules, layers[0])
    if edge > 0:
        inner = ext.buffer(-edge, join_style="mitre")
        ring_ = sh.Polygon(ext.exterior).difference(inner)
        for p in _holeless(ring_):
            keepouts.append(("keepout", "signal", p))
    nko = 0
    for kind, lay, obj in _keepout_objects(doc, layers):
        keepouts.append((kind, lay, obj))
    for lay, g in _netless_copper(doc, layers):
        # copper on no net: an obstacle at the default clearance
        keepouts.append(("keepout", lay, g.buffer(default_clear, join_style="mitre")))
    hole_gap = _hole_gap(rules, probe_track(None)[0])
    for p in doc.pads:
        if p.hole > 0 and not p.plated:
            keepouts.append(("keepout", "signal", geom.hole_shape(p, hole_gap)))
    for kind, lay, g in keepouts:
        for p in _holeless(g):
            nko += 1
            ln = "signal" if lay == "signal" else out.layer_name[lay]
            pts = list(p.exterior.coords)[:-1]
            w(f"    ({kind} \"ko{nko}\" (polygon {ln} 0 {_coords(pts)}))")
    w(f"    (via {q(out.routing_via)}" + "".join(
        f" {q(v)}" for v in sorted(set(route_vias.values())) if v != out.routing_via) + ")")
    w(f"    (rule (width {um(default_width)}) (clearance {um(default_clear)}))")
    w("    (control (via_at_smd off))")
    w("  )")

    # ------------------------------------------------ placement and library
    images, pin_stacks = [], {}
    place = []
    comp_ids = set()

    def comp_id(des, idx):
        cid = des if des and _SAFE.match(des) and des not in comp_ids else f"X{idx}"
        while cid in comp_ids:
            cid += "_"
        comp_ids.add(cid)
        return cid

    def pad_stack(p):
        shapes = []
        for lay in layers:
            if not p.on_layer(lay):
                continue
            s = _pad_shape_text(p, lay, out.layer_name[lay])
            if s:
                shapes.append(s)
        if not shapes:
            return None
        key = ("pad",) + tuple(shapes)
        if key not in pin_stacks:
            nm = f"PS{len(pin_stacks) + 1}"
            pin_stacks[key] = nm
            stack_text.append(f"    (padstack {q(nm)}")
            for s in shapes:
                stack_text.append(f"      (shape {s})")
            stack_text.append("      (attach off)")
            stack_text.append("    )")
        return pin_stacks[key]

    by_comp = {}
    for p in doc.pads:
        by_comp.setdefault(p.component, []).append(p)
    net_pins = {}
    npins = 0
    for cidx, pads in by_comp.items():
        groups_ = [pads] if cidx is not None else [[p] for p in pads]
        for k, grp in enumerate(groups_):
            if cidx is not None:
                des = doc.components[cidx].designator
                cid = comp_id(des, cidx)
            else:
                des = None
                cid = comp_id(None, f"F{grp[0].index}")
            img = f"IMG_{cid}"
            body = []
            pin_ids = set()
            for p in grp:
                ps = pad_stack(p)
                if ps is None:
                    continue
                pid = p.name if p.name and _SAFE.match(p.name) and p.name not in pin_ids else \
                    f"P{p.index}"
                while pid in pin_ids:
                    pid += "_"
                pin_ids.add(pid)
                body.append(f"      (pin {q(ps)} {q(pid)} {um(p.x)} {um(p.y)})")
                ref = f"{cid}-{pid}"
                out.pins[ref] = (des, p.name)
                npins += 1
                lab = label(p)
                if lab is not None:
                    net_pins.setdefault(lab, []).append(ref)
            if not body:
                continue
            images.append(f"    (image {q(img)}")
            images.extend(body)
            images.append("    )")
            place.append(f"    (component {q(img)} (place {q(cid)} 0 0 front 0))")
    w("  (placement")
    lines.extend(place)
    w("  )")

    # existing vias need their padstacks before the library is written
    wiring = []
    n_fixed = {"track": 0, "arc": 0, "via": 0, "fill": 0, "region": 0, "pour": 0}

    def fixed_type(net):
        if net in route_idx and fixed_nets is not None and net not in fixed_nets:
            return "route"
        return "protect"

    def net_part(obj):
        lab = label(obj)
        return f" (net {q(lab)})" if lab is not None else ""

    for t in doc.tracks:
        if t.layer not in out.layer_name or t.is_keepout or t.effective_net is None:
            continue
        if t.polygon is not None and not _keep_pour(t.effective_net, route_idx, pours):
            continue
        net = t.effective_net
        if math.dist(t.start, t.end) < 1e-6:
            # a dot of copper: a zero-length path would be dropped
            dot = sh.Point(t.start).buffer(t.width / 2, quad_segs=4)
            wiring.append(f"    (wire (polygon {out.layer_name[t.layer]} 0 "
                          f"{_coords(list(dot.exterior.coords)[:-1])}){net_part(t)} "
                          f"(type protect))")
        else:
            wiring.append(f"    (wire (path {out.layer_name[t.layer]} {um(t.width)} "
                          f"{_coords([t.start, t.end])}){net_part(t)} "
                          f"(type {fixed_type(net)}))")
        n_fixed["track"] += 1
    for a in doc.arcs:
        if a.layer not in out.layer_name or a.is_keepout or a.effective_net is None:
            continue
        if a.polygon is not None and not _keep_pour(a.effective_net, route_idx, pours):
            continue
        net = a.effective_net
        pts = geom.arc_points(a.cx, a.cy, a.radius, a.start_angle, a.end_angle, tol=0.002)
        wiring.append(f"    (wire (path {out.layer_name[a.layer]} {um(a.width)} "
                      f"{_coords(pts)}){net_part(a)} (type {fixed_type(net)}))")
        n_fixed["arc"] += 1
    for v in doc.vias:
        nm = via_stack(v.diameter, v.hole, v.low_layer, v.high_layer)
        net = v.effective_net
        wiring.append(f"    (via {q(nm)} {um(v.x)} {um(v.y)}{net_part(v)} "
                      f"(type {fixed_type(net)}))")
        n_fixed["via"] += 1
    areas = [(f, "fill") for f in doc.fills] + [(r, "region") for r in doc.regions]
    for obj, kind in areas:
        if obj.layer not in out.layer_name or obj.keepout_restrictions:
            continue
        if kind == "region" and (obj.kind != "copper" or obj.is_board_cutout):
            continue
        if obj.polygon is not None and not _keep_pour(obj.effective_net, route_idx, pours):
            continue
        net = obj.effective_net
        if net is None:
            continue                # a keep-out already (see _netless_copper)
        g = geom.copper_shape(obj, obj.layer)
        for p in _holeless(g):
            pts = list(p.exterior.coords)[:-1]
            if len(pts) < 3:
                continue
            wiring.append(f"    (wire (polygon {out.layer_name[obj.layer]} 0 {_coords(pts)})"
                          f"{net_part(obj)} (type protect))")
        n_fixed["pour" if obj.polygon is not None else kind] += 1

    wiring.extend(_pin_stubs(doc, layers, out.layer_name, label, pours, route_idx,
                             only=lambda lab: lab.startswith("_I")))

    w("  (library")
    lines.extend(images)
    lines.extend(stack_text)
    w("  )")

    # ------------------------------------------------ network
    w("  (network")
    # routed nets by name; everything else as one net per island (see label)
    for net in sorted(route_idx):
        pins = net_pins.get(dsn_net[net], [])
        w(f"    (net {q(dsn_net[net])}" + (f" (pins {' '.join(pins)})" if pins else "") + ")")
    isl_names = [f"_I{k}" for k in sorted(set(island.values()))]
    for nm in isl_names:
        pins = net_pins.get(nm, [])
        w(f"    (net {q(nm)}" + (f" (pins {' '.join(pins)})" if pins else "") + ")")
    for (wd, cl, vd, vh), nets in sorted(groups.items()):
        cname = f"route_{len(out.classes) + 1}"
        names = " ".join(q(dsn_net[n]) for n in nets)
        w(f"    (class {cname} {names}")
        w(f"      (circuit (use_via {q(route_vias[(vd, vh)])}))")
        w(f"      (rule (width {um(wd)}) (clearance {um(cl)}))")
        w("    )")
        out.classes.append({"name": cname, "nets": [doc.nets[n].name for n in nets],
                            "width": wd, "clearance": cl, "via": [vd, vh]})
    if isl_names:
        w(f"    (class {FIXED_CLASS} {' '.join(q(n) for n in isl_names)}")
        w(f"      (rule (width {um(default_width)}) (clearance {um(default_clear)}))")
        w("    )")
    w("  )")
    w("  (wiring")
    lines.extend(wiring)
    w("  )")
    w(")")
    out.text = "\n".join(lines) + "\n"
    out.routed = [doc.nets[n].name for n in sorted(route_idx)]
    out.stats = {"layers": [L.name(x) for x in layers], "pins": npins,
                 "images": len(place), "padstacks": len(pin_stacks) + len(stacks),
                 "keepouts": nko, "fixed": n_fixed, "default_width_mm": default_width,
                 "default_clearance_mm": default_clear, "routing_via_mm": list(via),
                 "board_edge_clearance_mm": edge}
    return out


def _pin_stubs(doc, layers, lname, label, pours, route_idx, only=lambda lab: True):
    """Fixed hairline wires from a pad's centre to its own net's copper that
    meets the pad elsewhere. Freerouting counts a track as joined to an SMD
    pad only when it ends at the pad's centre, and a region only when it
    holds the centre; without these, copper that is joined on the board
    looks open to it (and its optimizer never runs). Design-only: they lie
    inside the pad, and `only` keeps them to nets that are not routed, whose
    wiring never comes back in the plan."""
    sh = geom.require()
    out = []
    ends = {}
    for t in list(doc.tracks) + list(doc.arcs):
        if t.layer not in lname or t.is_keepout:
            continue
        if t.polygon is not None and not _keep_pour(t.effective_net, route_idx, pours):
            continue
        lab = label(t)
        if lab is None or not only(lab):
            continue
        pts = [t.start, t.end]
        for pt in pts:
            ends.setdefault((t.layer, lab), []).append(pt)
    areas = {}
    for obj in list(doc.fills) + list(doc.regions):
        if obj.layer not in lname or obj.keepout_restrictions:
            continue
        if obj.KIND == "region" and (obj.kind != "copper" or obj.is_board_cutout):
            continue
        if obj.polygon is not None and not _keep_pour(obj.effective_net, route_idx, pours):
            continue
        lab = label(obj)
        if lab is not None and only(lab):
            areas.setdefault((obj.layer, lab), []).append(geom.copper_shape(obj, obj.layer))
    vias = {}
    for v in doc.vias:
        lab = label(v)
        if lab is not None and only(lab):
            vias.setdefault(lab, []).append((v.x, v.y))
    for p in doc.pads:
        lab = label(p)
        if lab is None or not only(lab):
            continue
        c = (p.x, p.y)
        for lay in layers:
            if not p.on_layer(lay):
                continue
            shape = geom.pad_shape(p, lay)
            if shape.is_empty:
                continue
            core = shape.buffer(-0.01)
            # every track end in the pad but off its centre needs its own stub
            targets = [q for q in ends.get((lay, lab), ())
                       if math.dist(c, q) >= 1e-3 and core.contains(sh.Point(q))]
            targets += [q for q in vias.get(lab, ())
                        if math.dist(c, q) >= 1e-3 and core.contains(sh.Point(q))]
            for g in areas.get((lay, lab), ()):
                if g.contains(sh.Point(c)):
                    continue
                o = g.intersection(core)
                if not o.is_empty:
                    targets.append(o.representative_point().coords[0])
            for q in dict.fromkeys((round(x, 6), round(y, 6)) for x, y in targets):
                out.append(f"    (wire (path {lname[lay]} 10 {_coords([c, q])}) (net {q_(lab)}) "
                           f"(type protect))")
    return out


def q_(name):
    return q(name)


def _islands(doc, route_idx, pours):
    """{(storage, index): island} for the copper of nets not routed: pieces
    of one net that touch, poured copper left out when it is to be repoured
    (it is not in the design then)."""
    from .connect import CopperIndex, UnionFind
    idx = CopperIndex(doc)
    uf = UnionFind()
    keys = set()

    def wanted(o, n):
        if n is None or n in route_idx or o.KIND == "plane":
            return False
        return not (pours != "fixed" and getattr(o, "polygon", None) is not None)
    for lay in idx.layers:
        items = idx.items[lay]
        for o, n, _g in items:
            if wanted(o, n):
                keys.add((o.storage, o.index))
                uf.find((o.storage, o.index))
        for i, j in idx.pairs(lay):
            oa, na, _ = items[i]
            ob, nb, _ = items[j]
            if na == nb and wanted(oa, na) and wanted(ob, nb):
                uf.union((oa.storage, oa.index), (ob.storage, ob.index))
    roots = {}
    return {k: roots.setdefault(uf.find(k), len(roots)) for k in sorted(keys)}


def _netless_copper(doc, layers):
    """(layer, shape) of copper that belongs to no net: tracks, arcs, fills
    and free regions that are not keep-outs."""
    for obj in list(doc.tracks) + list(doc.arcs) + list(doc.fills) + list(doc.regions):
        if obj.layer not in layers or obj.keepout_restrictions or obj.effective_net is not None:
            continue
        if obj.KIND == "region" and (obj.kind != "copper" or obj.is_board_cutout):
            continue
        if obj.polygon is not None:
            continue
        g = geom.copper_shape(obj, obj.layer)
        if g is not None and not g.is_empty:
            yield obj.layer, g


def _keep_pour(net, route_idx, pours):
    return pours == "fixed" or net in route_idx


def _spans(lo, hi, lay, order):
    if lo not in order or hi not in order:
        return True
    a, b = sorted((order.index(lo), order.index(hi)))
    return a <= order.index(lay) <= b


def _route_width(rules, probe, layers, doc):
    """A width every routed layer allows, as close to the preferred as they
    let (per-layer limits may differ)."""
    lo, hi, pref = 0.0, math.inf, None
    for lay in layers:
        r = rules.width(_OnLayer(probe, lay))
        if not r:
            continue
        lo = max(lo, r["min"] or 0.0)
        hi = min(hi, r["max"] or math.inf)
        if pref is None and r.get("preferred"):
            pref = r["preferred"]
    if pref is None:
        return None
    return min(max(pref, lo), hi)


class _OnLayer:
    """A probe seen on another layer (for per-layer width limits)."""

    def __init__(self, obj, layer):
        self._obj = obj
        self.layer = layer
        self.storage = f"{getattr(obj, 'storage', 'probe')}@{layer}"

    def __getattr__(self, k):
        return getattr(self._obj, k)


def _hole_gap(rules, probe):
    """Clearance from new copper to a bare drilled hole."""
    c = rules.best("Clearance", probe, probe)
    if c is None:
        return 0.0
    gap = c.length("GAP", c.length("GENERICCLEARANCE"))
    return max(c.matrix.get(("Track", "Hole"), gap), c.matrix.get(("Via", "Hole"), gap))


def _edge_gap(doc, rules, layer):
    from . import records as R
    t = R.track(doc, layer, 0, 0, 1, 0, 0.2, None)
    t.storage = "dsn-probe-edge"
    gap, _ = rules.board_outline(t)
    return gap or 0.0


def _keepout_objects(doc, layers):
    """("keepout" | "via_keepout", layer or "signal", shape) for keep-outs."""
    for obj in list(doc.tracks) + list(doc.arcs) + list(doc.fills) + list(doc.regions):
        mask = obj.keepout_restrictions
        if not mask:
            continue
        if obj.layer == L.KEEPOUT:
            lay = "signal"
        elif obj.layer in layers:
            lay = obj.layer
        else:
            continue
        if mask & (KEEPOUT_TRACK | KEEPOUT_COPPER):
            kind = "keepout"
        elif mask & KEEPOUT_VIA:
            kind = "via_keepout"
        else:
            continue            # pad-only keep-outs do not concern routing
        g = geom.copper_shape(obj, obj.layer if obj.layer != L.KEEPOUT else layers[0])
        if g is not None and not g.is_empty:
            yield kind, lay, g


def _pad_shape_text(p, layer, lname):
    shape, (sx, sy), frac = p.shape_on(layer)
    if sx <= 0 or sy <= 0:
        return None
    rot = p.rotation % 360.0
    square = min(abs(rot - a) for a in (0, 90, 180, 270, 360)) < 1e-6
    if shape == "round" and abs(sx - sy) < 1e-9:
        return f"(circle {lname} {um(sx)} 0 0)"
    if shape == "round":
        # an obround: Specctra's own form is a path with round ends
        half = abs(sx - sy) / 2
        a = math.radians(rot + (0.0 if sx > sy else 90.0))
        dx, dy = half * math.cos(a), half * math.sin(a)
        return f"(path {lname} {um(min(sx, sy))} {um(-dx)} {um(-dy)} {um(dx)} {um(dy)})"
    if shape == "rect" and square:
        if abs(rot - 90) < 1e-6 or abs(rot - 270) < 1e-6:
            sx, sy = sy, sx
        return f"(rect {lname} {um(-sx / 2)} {um(-sy / 2)} {um(sx / 2)} {um(sy / 2)})"
    g = geom.pad_shape(p, layer)
    if g.is_empty:
        return None
    pts = [(x - p.x, y - p.y) for x, y in list(g.exterior.coords)[:-1]]
    return f"(polygon {lname} 0 {_coords(pts)})"


# ---------------------------------------------------------------- reading SES

def read_ses(text, dsn=None):
    """Wires and vias of a Specctra session file, in mm.

    Returns {"wires": [{"net", "layer", "width", "points", "type"}],
    "vias": [{"net", "x", "y", "padstack", "diameter", "hole", "type"}],
    "resolution": (unit, value)}. Layer and net names map back through the
    Dsn the design was exported with, when given.
    """
    tree = parse(text)
    ses = find(tree, "session")
    if ses is None:
        raise SpecctraError("not a session file: no (session ...)")
    routes = find(ses, "routes")
    if routes is None:
        raise SpecctraError("the session has no (routes ...): nothing was routed")
    res = find(routes, "resolution") or find(find(ses, "placement") or [], "resolution") or \
        ["resolution", "um", "1"]
    unit, per = res[1].lower(), float(res[2])
    if unit not in UNIT_MM or per <= 0:
        raise SpecctraError(f"unknown resolution {res[1:]}")
    k = UNIT_MM[unit] / per

    lib = {}
    for ps in find_all(find(routes, "library_out") or [], "padstack"):
        d = None
        for s in find_all(ps, "shape"):
            for c in s[1:]:
                if isinstance(c, list) and c and c[0] == "circle":
                    d = float(c[2]) * k
        lib[ps[1]] = d

    def layer_of(nm):
        if dsn is not None:
            if nm not in dsn.layer_id:
                raise SpecctraError(f"the session uses layer {nm!r}, which the design does not have")
            return dsn.layer_id[nm]
        return nm

    def net_of(nm):
        return (dsn.net_name.get(nm) or nm) if dsn is not None else nm

    wires, vias = [], []
    for net in find_all(find(routes, "network_out") or [], "net"):
        nm = net_of(net[1])
        for wire in find_all(net, "wire"):
            shape = wire[1] if len(wire) > 1 and isinstance(wire[1], list) else None
            typ = find(wire, "type")
            typ = typ[1] if typ else "route"
            if shape is None:
                continue
            if shape[0] == "path":
                nums = [float(x) for x in shape[3:]]
                pts = [(nums[i] * k, nums[i + 1] * k) for i in range(0, len(nums) - 1, 2)]
                wires.append({"net": nm, "layer": layer_of(shape[1]),
                              "width": float(shape[2]) * k, "points": pts, "type": typ})
            elif shape[0] == "polygon":
                nums = [float(x) for x in shape[3:]]
                pts = [(nums[i] * k, nums[i + 1] * k) for i in range(0, len(nums) - 1, 2)]
                wires.append({"net": nm, "layer": layer_of(shape[1]), "width": 0.0,
                              "polygon": pts, "type": typ})
        for v in find_all(net, "via"):
            ps = v[1]
            typ = find(v, "type")
            info = (dsn.via_stacks.get(ps) if dsn is not None else None) or _via_from_name(ps)
            vias.append({"net": nm, "x": float(v[2]) * k, "y": float(v[3]) * k, "padstack": ps,
                         "diameter": info.get("diameter", lib.get(ps)),
                         "hole": info.get("hole"), "low": info.get("low"),
                         "high": info.get("high"), "type": typ[1] if typ else "route"})
    return {"wires": wires, "vias": vias, "resolution": (unit, per)}


def _via_from_name(name):
    """{"diameter", "hole"} from a VIA_<d>_<h> padstack name (um, 'p' for
    the decimal point), as export() names them."""
    m = re.match(r"^VIA_(\d+(?:p\d+)?)_(\d+(?:p\d+)?)", str(name))
    if not m:
        return {}
    d, h = (float(x.replace("p", ".")) / 1000.0 for x in m.groups())
    return {"diameter": d, "hole": h}


def plan_from_session(doc, session, dsn, tol=0.001, replace=()):
    """EditPlan ops for what the router added: tracks for each new wire
    segment and vias, leaving out anything already on the board. Nets in
    `replace` (ones the router was allowed to move) lose their free tracks,
    arcs and vias first and get the session's wiring in full."""
    ops = []
    replace = set(replace)
    for nm in sorted(replace):
        ops.append({"op": "remove", "kinds": ["track", "arc", "via"], "net": nm, "free": True})
    have_t = {}
    for t in doc.tracks:
        if t.effective_net is not None:
            have_t.setdefault((t.effective_net, t.layer), []).append(t)
    have_v = {}
    for v in doc.vias:
        have_v.setdefault(v.effective_net, []).append(v)
    skipped = {"fixed": 0, "existing": 0, "polygons": 0}

    def exists_track(net, layer, a, b, wdt):
        for t in have_t.get((net, layer), ()):
            for p, q_ in ((t.start, t.end), (t.end, t.start)):
                if math.dist(p, a) <= tol and math.dist(q_, b) <= tol and abs(t.width - wdt) <= tol:
                    return True
        return False

    for wire in session["wires"]:
        mine = wire["net"] in replace
        if wire["type"] in ("protect", "fix") and not mine:
            skipped["fixed"] += 1
            continue
        if "polygon" in wire:
            skipped["polygons"] += 1
            continue
        net = doc.net_index(wire["net"])
        pts = wire["points"]
        for a, b in zip(pts, pts[1:]):
            if math.dist(a, b) < 1e-6:
                continue
            if not mine and exists_track(net, wire["layer"], a, b, wire["width"]):
                skipped["existing"] += 1
                continue
            ops.append({"op": "add_track", "layer": L.name(wire["layer"]),
                        "x1": round(a[0], 6), "y1": round(a[1], 6),
                        "x2": round(b[0], 6), "y2": round(b[1], 6),
                        "width": round(wire["width"], 6), "net": wire["net"]})
    for v in session["vias"]:
        mine = v["net"] in replace
        if v["type"] in ("protect", "fix") and not mine:
            skipped["fixed"] += 1
            continue
        net = doc.net_index(v["net"])
        if not mine and any(math.hypot(o.x - v["x"], o.y - v["y"]) <= tol
                            for o in have_v.get(net, ())):
            skipped["existing"] += 1
            continue
        if not v.get("diameter") or not v.get("hole"):
            raise SpecctraError(f"the session's via padstack {v['padstack']!r} is not one the "
                                "design defined, so its hole is unknown")
        op = {"op": "add_via", "x": round(v["x"], 6), "y": round(v["y"], 6),
              "diameter": round(v["diameter"], 6), "hole": round(v["hole"], 6), "net": v["net"]}
        if v.get("low") and v.get("high"):
            op["low"], op["high"] = v["low"], v["high"]
        ops.append(op)
    return ops, skipped


# ---------------------------------------------------------------- CLI

def cmd_dsn(args):
    from .pcbdoc import PcbDoc
    doc = PcbDoc.open(args.board)
    nets = args.nets or [n.name for n in doc.nets]
    d = export(doc, nets, pours=args.pours, via=tuple(args.via) if args.via else None)
    out = args.out
    if out and _same(out, args.board):
        raise ValueError("refusing to write the DSN over the board")
    if out:
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(d.text)
        print(f"{out}: {len(d.routed)} net(s) to route, {d.stats['pins']} pins, "
              f"{d.stats['keepouts']} keep-out(s)")
    else:
        print(d.text, end="")
    return 0


def cmd_ses(args):
    import json
    from .pcbdoc import PcbDoc
    doc = PcbDoc.open(args.board)
    d = export(doc, [], pours="fixed")       # only the name maps are needed
    with open(args.session, encoding="utf-8", errors="replace") as f:
        s = read_ses(f.read(), d)
    for k in ("wires", "vias"):
        s[k] = [x for x in s[k] if doc.has_net(x["net"]) and
                (not args.nets or x["net"].upper() in {n.upper() for n in args.nets})]
    ops, skipped = plan_from_session(doc, s, d)
    plan = {"version": 1, "ops": ops}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=1)
    print(f"{len(ops)} op(s): {sum(1 for o in ops if o['op'] == 'add_track')} track(s), "
          f"{sum(1 for o in ops if o['op'] == 'add_via')} via(s); skipped {skipped}")
    return 0


def _same(a, b):
    import os
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def register_commands(command):
    def build_dsn(p):
        p.add_argument("board")
        p.add_argument("-o", "--out", help="write the DSN here (default: stdout)")
        p.add_argument("--nets", nargs="+", help="nets to route (default: all)")
        p.add_argument("--pours", choices=("repour", "fixed"), default="repour")
        p.add_argument("--via", nargs=2, type=float, metavar=("DIAMETER", "HOLE"))
    command("dsn", cmd_dsn, "export a Specctra DSN for an autorouter", build_dsn)

    def build_ses(p):
        p.add_argument("session", help="the router's .ses file")
        p.add_argument("board", help="the board the DSN came from")
        p.add_argument("--nets", nargs="+", help="import only these nets")
        p.add_argument("-o", "--out", help="write the EditPlan here")
    command("ses", cmd_ses, "turn a Specctra session into an EditPlan", build_ses)
