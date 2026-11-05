"""How each pad is actually tied into its pour, measured from the saved copper.

    python -m offline pad-ties board.PcbDoc --nets VBAT GND [--json]

Rules say what Altium should do; the poured copper says what it did. For a
pad inside a same-net polygon, sample a ring just outside its land (half the
rule's air gap out) and see where same-net copper covers it:

  - covered all the way round  -> merged into the pour (direct)
  - mostly covered, or a run far wider than a spoke
                               -> direct_partial: merged, with part of the
                                  edge cleared around another net's copper
  - narrow covered runs        -> thermal relief; each run is a spoke and its
                                  length along the ring is the spoke's width
  - not covered                -> not tied into that polygon on this layer

Same-net copper that is not the pour's own (free regions, fills, tracks)
counts too: Altium pours over it and it carries current all the same. Widths
are good to the sampling step (720 samples per ring, about 0.02 mm on a
small pad). Ported from the phase-6 branch (98b87ff) onto this reader.
"""

import json

from . import geom
from . import layers as L

SAMPLES = 720


def _ring(pad, layer, offset):
    """Points evenly spaced along the pad outline grown by `offset`, and the step."""
    import numpy as np
    line = geom.pad_shape(pad, layer, offset).exterior
    n = SAMPLES
    d = np.linspace(0.0, line.length, n, endpoint=False)
    pts = [line.interpolate(v) for v in d]
    return np.array([p.x for p in pts]), np.array([p.y for p in pts]), line.length / n


def _runs(flags, step):
    n = len(flags)
    start = next(i for i in range(n) if not flags[i])
    runs, cur = [], 0
    for k in range(1, n + 1):
        if flags[(start + k) % n]:
            cur += 1
        elif cur:
            runs.append(cur * step)
            cur = 0
    if cur:
        runs.append(cur * step)
    return runs


def pad_tie(doc, pad, poly, layer, offset, copper=None, spoke_max=1.0):
    """{style, spokes, spoke_widths_mm, tie_width_mm, covered_fraction} for one
    pad and polygon, or None when the polygon has no poured copper on this
    layer. A covered run wider than `spoke_max` mm is not a spoke."""
    sh = geom.require()
    if copper is None:
        copper = _net_copper(doc, poly, layer, pad)
    if copper is None or copper.is_empty:
        return None
    xs, ys, step = _ring(pad, layer, offset)
    flags = [bool(f) for f in sh.contains_xy(copper, xs, ys)]
    frac = round(sum(flags) / len(flags), 3)
    if all(flags):
        return {"style": "direct", "spokes": 0, "spoke_widths_mm": [], "tie_width_mm": None,
                "covered_fraction": 1.0}
    if not any(flags):
        return {"style": "none", "spokes": 0, "spoke_widths_mm": [], "tie_width_mm": 0.0,
                "covered_fraction": 0.0}
    runs = _runs(flags, step)
    style = "direct_partial" if frac >= 0.5 or max(runs) > spoke_max else "relief"
    return {"style": style, "spokes": len(runs) if style == "relief" else 0,
            "spoke_widths_mm": [round(w, 3) for w in runs], "tie_width_mm": round(sum(runs), 3),
            "covered_fraction": frac}


def _net_copper(doc, poly, layer, pad):
    """The polygon's poured copper plus the net's other copper on the layer,
    without the pad itself."""
    sh = geom.require()
    shapes = []
    for r in doc.regions:
        if r.polygon == poly.index and r.kind == "copper" and r.layer == layer:
            shapes.append(geom.region_shape(r))
    for coll in (doc.tracks, doc.arcs):
        for t in coll:
            if t.polygon == poly.index and t.layer == layer:
                shapes.append(geom.copper_shape(t, layer))
    if not shapes:
        return None
    for obj, g in geom.layer_items(doc, layer, nets={pad.effective_net},
                                   kinds=("track", "arc", "fill", "region")):
        if getattr(obj, "polygon", None) is None:
            shapes.append(g)
    g = sh.union_all(shapes)
    sh.prepare(g)
    return g


def pad_connections(doc, nets):
    """One row per (pad, polygon, layer) for pads on the named nets."""
    from .rules import RuleSet
    from .review import _probe_region
    rules = RuleSet(doc)
    want = set()
    for n in nets:
        if not doc.has_net(n):
            raise ValueError(f"no net {n!r} on this board")
        want.add(doc.net_index(n))
    cache = {}
    rows = []
    for pad in doc.pads:
        if pad.effective_net not in want:
            continue
        for poly in doc.polygons:
            if poly.net != pad.effective_net:
                continue
            lay = poly.layer
            if not pad.on_layer(lay):
                continue
            outline = geom.polygon_outline(poly)
            if outline.is_empty or not outline.contains(geom.require().Point(pad.x, pad.y)):
                continue
            region = _probe_region(doc, poly)
            pc = rules.polygon_connect(pad, region) if region is not None else None
            offset = (pc["air_gap"] if pc else 0.2032) / 2
            key = (poly.index, lay, pad.effective_net)
            if key not in cache:
                cache[key] = _net_copper(doc, poly, lay, pad)
            # the pad's own land must not count: take it out of the copper
            copper = cache[key]
            if copper is not None:
                copper = copper.difference(geom.pad_shape(pad, lay))
            spoke_max = max(1.0, 2.5 * pc["conductor_width"]) if pc else 1.0
            tie = pad_tie(doc, pad, poly, lay, offset, copper, spoke_max)
            if tie is None:
                continue
            rows.append(dict(
                ref=doc.components[pad.component].designator if pad.component is not None
                else "(free)", pad=pad.name, net=pad.net_name, layer=L.name(lay),
                polygon=poly.name or poly.index, x=round(pad.x, 3), y=round(pad.y, 3),
                rule=pc["rule"] if pc else None, rule_style=pc["style"] if pc else None,
                rule_spokes=pc["entries"] if pc and pc["style"] == "relief" else None,
                rule_spoke_width_mm=pc["conductor_width"] if pc and pc["style"] == "relief"
                else None,
                matches_rule=(pc is not None and tie["style"].split("_")[0] == pc["style"]
                              and (tie["style"] != "relief" or tie["spokes"] == pc["entries"])),
                **tie))
    return {"pads": rows, "unevaluated_rules": rules.unevaluated}


def cmd_pad_ties(args):
    from .pcbdoc import PcbDoc
    rep = pad_connections(PcbDoc.open(args.board), args.nets)
    if args.json:
        print(json.dumps(rep, indent=1))
        return 0
    for r in rep["pads"]:
        spokes = (f" {r['spokes']} spoke(s) {r['spoke_widths_mm']} mm"
                  if r["style"] == "relief" else "")
        flag = "" if r["matches_rule"] else f"  (rule {r['rule']}: {r['rule_style']})"
        print(f"  {r['ref']}.{r['pad']} {r['net']} {r['layer']}: {r['style']}{spokes}{flag}")
    print(f"{len(rep['pads'])} pad tie(s); {sum(1 for r in rep['pads'] if not r['matches_rule'])} "
          "differ from the rule")
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("board")
        p.add_argument("--nets", nargs="+", required=True)
        p.add_argument("--json", action="store_true")
    command("pad-ties", cmd_pad_ties, "how pads tie into their pours, measured from the copper",
            build)
