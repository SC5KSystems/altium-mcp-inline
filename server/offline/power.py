"""Power screening: via arrays, copper against its fuse, FET pad current density.

    size_vias(30, hole=0.3)                    # vias for 30 A, per via and total
    fuse_check(doc, "F1.2", rating=30, i2t=850)
    fet_screen(doc, {"VBAT": 30}, pdn=[result])

All three are screening tools with their assumptions stated in what they
return. Formulas used:

  * IPC-2221 conductor capacity, I = k dT^0.44 A^0.725 (A in sq mil;
    k = 0.024 internal, 0.048 external). A via barrel is treated as an
    internal conductor of the barrel's cross-section (conservative).
  * Onderdonk's fusing equation for adiabatic heating of copper,
    I^2 t = (A_cmil)^2 * log10((Tm - Ta) / (234 + Ta) + 1) / 33, Tm = 1083 C.
"""

import math
import re

import numpy as np

from . import geom
from . import layers as L
from .pdn import rho_at

CMIL_PER_MM2 = 1973.525
SQMIL_PER_MM2 = 1550.0031
K_INTERNAL, K_EXTERNAL = 0.024, 0.048


def ipc2221_current(area_mm2, rise_c, external=False):
    """Amps an IPC-2221 conductor of `area_mm2` carries for `rise_c` rise."""
    k = K_EXTERNAL if external else K_INTERNAL
    return k * max(rise_c, 0.0) ** 0.44 * (area_mm2 * SQMIL_PER_MM2) ** 0.725


def barrel_area(hole, plating=0.025):
    r = hole / 2.0
    return math.pi * ((r + plating) ** 2 - r ** 2)


def size_vias(current, hole=0.3, plating=0.025, length=1.6, rise_c=10.0, layers=None,
              margin=1.25, temp_c=25.0):
    """How many vias carry `current` amps between layers.

    layers: copper thicknesses (mm) of the layers sharing the current, top
    first: the vias at a layer change carry the share of the other layers
    (2 equal layers: half). margin multiplies the count.
    """
    area = barrel_area(hole, plating)
    per = ipc2221_current(area, rise_c, external=False)
    share = 1.0
    if layers and len(layers) > 1:
        share = 1.0 - layers[0] / float(sum(layers))
    transfer = current * share
    n = max(1, int(math.ceil(transfer / per * margin))) if transfer > 0 else 0
    r = rho_at(temp_c + rise_c) * length / area
    each = transfer / n if n else 0.0
    return {"current_a": current, "transfer_a": round(transfer, 4), "vias": n, "rise_c": rise_c,
            "per_via_capacity_a": round(per, 4), "per_via_current_a": round(each, 4),
            "barrel_area_mm2": round(area, 5), "via_resistance_mohm": round(r * 1e3, 4),
            "array_resistance_mohm": round(r / n * 1e3, 5) if n else None,
            "array_power_w": round(each * each * r * n, 5) if n else 0.0,
            "assumptions": ["barrel treated as an IPC-2221 internal conductor (conservative)",
                            f"plating {plating} mm, barrel length {length} mm",
                            "current shared evenly by the vias of the array",
                            f"count includes a {margin}x margin"]}


def onderdonk_i2t(area_mm2, ambient_c=25.0, melt_c=1083.0):
    """A^2 s at which a copper conductor of `area_mm2` melts, adiabatically."""
    a = area_mm2 * CMIL_PER_MM2
    return a * a * math.log10((melt_c - ambient_c) / (234.0 + ambient_c) + 1.0) / 33.0


def fuse_check(doc, pad, rating, i2t=None, fault=None, sinks=None, ambient_c=25.0,
               hold_factor=1.1, max_temp_c=105.0, res=0.2, thermal=True):
    """Would the copper on the fuse's net fail before the fuse?

    pad: the fuse's load-side pad ('F1.2'). rating: fuse rating in A.
    i2t: the fuse's clearing I^2 t (A^2 s) at the fault current; or fault:
    (amps, seconds) from its time-current curve. sinks: load pads (default
    every other pad of the net). hold_factor x rating is the current the
    fuse may carry without opening; the copper must survive it hot-steady.
    """
    from .pdn import parse_terminal, solve
    term = parse_terminal(doc, pad)
    if term["kind"] != "pads":
        raise ValueError("give the fuse pad as designator.pin")
    p0 = term["pads"][0]
    net = p0.effective_net
    if net is None:
        raise ValueError(f"{pad} has no net")
    net_name = doc.nets[net].name
    if sinks is None:
        comp_of = {c.index: c.designator for c in doc.components}
        sinks = sorted({f"{comp_of[p.component]}.{p.name}" for p in doc.pads
                        if p.effective_net == net and p.component is not None and
                        p.component != p0.component})
        if not sinks:
            raise ValueError(f"net {net_name} has no other pads to carry current to")
    hold = rating * hold_factor
    res_ = solve(doc, net, [pad], sinks, hold, res=res, temp_c=ambient_c + 20.0)
    worst = max(res_.hotspots, key=lambda h: h["j_a_per_mm2"])
    a_min = hold / worst["j_a_per_mm2"]
    out = {"net": net_name, "fuse_pad": pad, "rating_a": rating, "hold_current_a": round(hold, 3),
           "sinks": sinks, "narrowest": {"layer": L.name(worst["layer"]),
                                         "x": round(worst["x"], 3), "y": round(worst["y"], 3),
                                         "current_density_a_per_mm2": round(worst["j_a_per_mm2"], 2),
                                         "effective_area_mm2": round(a_min, 5)},
           "problems": []}
    cu_i2t = onderdonk_i2t(a_min, ambient_c)
    out["copper_melt_i2t_a2s"] = round(cu_i2t, 3)
    if fault is not None:
        i2t = float(fault[0]) ** 2 * float(fault[1])
    if i2t is not None:
        out["fuse_i2t_a2s"] = round(float(i2t), 3)
        out["i2t_margin"] = round(cu_i2t / float(i2t), 3)
        if float(i2t) >= cu_i2t:
            out["problems"].append(
                f"copper at ({worst['x']:.2f}, {worst['y']:.2f}) melts at {cu_i2t:.1f} A^2s, "
                f"below the fuse's {float(i2t):.1f} A^2s: it would open before the fuse")
    if thermal:
        from .thermal import solve as thermal_solve
        th = thermal_solve(doc, pdn=[res_], ambient_c=ambient_c)
        rise = th.max_rise
        out["hold_rise_c"] = round(rise, 2)
        if ambient_c + rise > max_temp_c:
            out["problems"].append(
                f"at {hold:.1f} A (the fuse holds this) the copper reaches "
                f"{ambient_c + rise:.0f} C, above {max_temp_c:.0f} C")
    out["ok"] = not out["problems"]
    out["assumptions"] = ["narrowest point = where the DC solve's current density peaks",
                          "melting: Onderdonk, adiabatic (valid for clearing times up to a "
                          "few seconds)", "sustained: thermal.py screening model, still air"]
    return out


FET_PATTERN = re.compile(r"(DPAK|D2PAK|D3PAK|TO-?2[2-9]\d|TO-?263|TO-?252|TO-?220|TO-?247|"
                         r"LFPAK|POWERPAK|PQFN|TDSON|SUPERSO|SO-?8FL|DFN5X6|DFN3X3|TOLL|STOLL|"
                         r"HSOF|SOT-?669|WPAK|DIRECTFET)", re.I)


def find_fets(doc):
    """Components that look like power transistors: Q designators, or a
    power package in the footprint name."""
    out = []
    for c in doc.components:
        if re.match(r"^Q\d", c.designator, re.I) or FET_PATTERN.search(c.pattern or ""):
            out.append(c.designator)
    return out


def fet_screen(doc, currents, pdn=(), fets=None, warn=50.0, fail=100.0, ring=0.4):
    """Current density where copper enters each power-FET pad.

    currents: {net: amps through the FETs on that net}. With pdn results
    for a net, the density is the solve's peak in a ring of `ring` mm around
    each pad; without, the net's current is split over the FET's pads on it
    and divided by the cross-section where same-net copper meets the pad
    (pad outline in copper x copper thickness).
    """
    geom.require()
    fets = list(fets) if fets else find_fets(doc)
    solves = {r.net: r for r in (pdn if isinstance(pdn, (list, tuple)) else [pdn])}
    rows = []
    for des in fets:
        comp = doc.component(des)
        pads = [p for p in doc.pads if p.component == comp.index]
        by_net = {}
        for p in pads:
            if p.net_name in currents:
                by_net.setdefault(p.net_name, []).append(p)
        for net, ps in by_net.items():
            amps = float(currents[net])
            each = amps / len(ps)
            for p in ps:
                lay = p.layer if p.layer != L.MULTI else (L.BOTTOM if comp.layer == L.BOTTOM
                                                          else L.TOP)
                shape = geom.pad_shape(p, lay)
                row = {"component": des, "pad": p.name, "net": net, "layer": L.name(lay),
                       "pad_current_a": round(each, 3)}
                res = solves.get(net)
                if res is not None and lay in res.maps:
                    jm = res.maps[lay]["j"]
                    m = res.grid.empty()
                    from .raster import draw
                    draw(m, res.grid, shape.buffer(ring).difference(shape))
                    vals = jm[m & np.isfinite(jm)]
                    if len(vals):
                        row["entry_density_a_per_mm2"] = round(float(vals.max()), 2)
                        row["method"] = "pdn solve, peak in a ring around the pad"
                if "entry_density_a_per_mm2" not in row:
                    edge = _copper_edge(doc, p, lay, shape)
                    t = doc.copper_thickness(lay)
                    if edge <= 1e-6:
                        row["entry_density_a_per_mm2"] = None
                        row["method"] = "no same-net copper touches this pad"
                    else:
                        row["entry_width_mm"] = round(edge, 3)
                        row["entry_density_a_per_mm2"] = round(each / (edge * t), 2)
                        row["method"] = "net current split over the FET's pads / entry width x t"
                j = row["entry_density_a_per_mm2"]
                row["verdict"] = ("no copper" if j is None else "fail" if j >= fail else
                                  "warn" if j >= warn else "ok")
                rows.append(row)
    rows.sort(key=lambda r: -(r["entry_density_a_per_mm2"] or 0))
    return {"fets": fets, "pads": rows, "limits_a_per_mm2": {"warn": warn, "fail": fail},
            "assumptions": ["limits are screening values for short pad-entry necks, not a "
                            "standard; compare pads with each other",
                            "without a solve the current splits evenly over the FET's pads "
                            "on the net"]}


def _copper_edge(doc, pad, layer, shape):
    """Length of the pad outline covered by same-net copper on `layer`."""
    sh = geom.require()
    ring = shape.exterior if shape.geom_type == "Polygon" else shape.boundary
    covered = []
    for obj in doc.copper(layer=layer):
        if obj is pad or obj.effective_net != pad.effective_net or obj.effective_net is None:
            continue
        if obj.KIND == "pad" and obj.component == pad.component and obj.name == pad.name:
            continue
        g = geom.copper_shape(obj, layer)
        if g.is_empty or not g.buffer(1e-6).intersects(shape):
            continue
        covered.append(g)
    if not covered:
        return 0.0
    return ring.intersection(sh.union_all(covered).buffer(1e-4)).length


# ---------------------------------------------------------------- CLI

def cmd_vias(args):
    import json
    layers = [float(t) for t in args.layers] if args.layers else None
    rep = size_vias(args.current, hole=args.hole, plating=args.plating, length=args.length,
                    rise_c=args.rise, layers=layers, margin=args.margin)
    print(json.dumps(rep, indent=2))
    return 0


def cmd_fuse(args):
    import json
    from .pcbdoc import PcbDoc
    doc = PcbDoc.open(args.board)
    fault = tuple(args.fault) if args.fault else None
    rep = fuse_check(doc, args.pad, args.rating, i2t=args.i2t, fault=fault,
                     sinks=args.sink, ambient_c=args.ambient, hold_factor=args.hold,
                     max_temp_c=args.max_temp, thermal=not args.no_thermal)
    print(json.dumps(rep, indent=2))
    return 0 if rep["ok"] else 1


def cmd_fets(args):
    import json
    from .pcbdoc import PcbDoc
    from .pdn import solve
    doc = PcbDoc.open(args.board)
    currents, sols = {}, []
    for spec in args.flow or []:
        net, amps, ends = spec.split(":", 2)
        currents[net] = float(amps)
        if ">" in ends:
            src, snk = ends.split(">", 1)
            sols.append(solve(doc, net, src.split("+"), snk.split("+"), float(amps), res=args.res,
                              temp_c=45.0))
    for spec in args.current or []:
        net, _, amps = spec.partition("=")
        currents[net] = float(amps)
    if not currents:
        raise ValueError("give --current NET=AMPS or --flow NET:AMPS:SRC>SINK")
    rep = fet_screen(doc, currents, pdn=sols, fets=args.fet, warn=args.warn, fail=args.fail)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(f"power FETs: {', '.join(rep['fets']) or 'none found (give --fet)'}")
        for r in rep["pads"]:
            j = r["entry_density_a_per_mm2"]
            print(f"  {r['verdict']:7s} {r['component']}-{r['pad']} {r['net']}: "
                  f"{'-' if j is None else j} A/mm2 ({r['pad_current_a']} A; {r['method']})")
    return 1 if any(r["verdict"] == "fail" for r in rep["pads"]) else 0


def register_commands(command):
    def build_vias(p):
        p.add_argument("current", type=float)
        p.add_argument("--hole", type=float, default=0.3)
        p.add_argument("--plating", type=float, default=0.025)
        p.add_argument("--length", type=float, default=1.6, help="barrel length (board thickness)")
        p.add_argument("--rise", type=float, default=10.0)
        p.add_argument("--layers", nargs="+", help="copper thickness of each layer sharing the "
                                                  "current, top first (mm)")
        p.add_argument("--margin", type=float, default=1.25)
    command("vias", cmd_vias, "size a via array for a current", build_vias)

    def build_fuse(p):
        p.add_argument("board")
        p.add_argument("pad", help="the fuse's load-side pad, e.g. F1.2")
        p.add_argument("--rating", type=float, required=True, help="fuse rating, A")
        p.add_argument("--i2t", type=float, help="fuse clearing I^2t, A^2 s")
        p.add_argument("--fault", type=float, nargs=2, metavar=("AMPS", "SECONDS"),
                       help="a point on the fuse's time-current curve")
        p.add_argument("--sink", action="append", help="load pads (default: the rest of the net)")
        p.add_argument("--hold", type=float, default=1.1, help="current the fuse holds, x rating")
        p.add_argument("--ambient", type=float, default=25.0)
        p.add_argument("--max-temp", type=float, default=105.0)
        p.add_argument("--no-thermal", action="store_true")
    command("fuse", cmd_fuse, "check copper against its fuse (I2t and sustained)", build_fuse)

    def build_fets(p):
        p.add_argument("board")
        p.add_argument("--current", action="append", help="NET=AMPS through the FETs")
        p.add_argument("--flow", action="append", help="NET:AMPS:SRC>SINK to solve the net")
        p.add_argument("--fet", action="append", help="FET designators (default: found by name)")
        p.add_argument("--warn", type=float, default=50.0)
        p.add_argument("--fail", type=float, default=100.0)
        p.add_argument("--res", type=float, default=0.1)
        p.add_argument("--json", action="store_true")
    command("fets", cmd_fets, "screen power-FET pads for current density", build_fets)
