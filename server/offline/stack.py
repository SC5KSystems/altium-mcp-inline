"""Stack check: two boards joined by mating headers.

    python -m offline stack a.PcbDoc b.PcbDoc --map J7:J1 [--map J8:J2] [--facing]
        [--tol 0.1] [--json]

Board B sits above board A. Both are seen from above with their top sides
up, so B's mating connector is on B's bottom side and its footprint is
already mirrored in B's file; --facing says B is upside down instead (its
top faces A's top). --map A_PART:B_PART pairs the mating connectors; their
pins pair by pad name.

Checks: the rigid fit of B's mating pins onto A's (rotation and shift,
per-pin misalignment, pin 1), whether only a mirrored fit lines up (a
connector on the wrong side or a mirrored footprint), whether pin 1 lands
on another pin (a connector turned round), mounting-hole alignment after
the fit, the pin-to-pin net map with ground and power mismatches flagged,
and the stack height from the connectors' 3D bodies against the tallest
parts between the boards.
"""

import json
import math
import re

import numpy as np

from . import geom
from . import layers as L

GROUND = re.compile(r"(^|[^A-Z])(P?GND|AGND|DGND|PGND|GROUND|VSS|EARTH|CHASSIS)", re.I)
HOLE_PART = re.compile(r"^(H|MH|MTG|HOLE|FID)\d*", re.I)


def _pins(doc, des):
    comp = doc.component(des)
    return {p.name: p for p in doc.pads if p.component == comp.index}


def fit(src, dst, reflect=False):
    """Least-squares map of src points onto dst by a rotation (or, with
    reflect=True, a mirror) and a shift (Kabsch): (R 2x2, t, residuals)."""
    P, Q = np.asarray(src, float), np.asarray(dst, float)
    pc, qc = P.mean(0), Q.mean(0)
    U, _S, Vt = np.linalg.svd((P - pc).T @ (Q - qc))
    V = Vt.T
    D = np.eye(2)
    if np.sign(np.linalg.det(V @ U.T)) != (-1.0 if reflect else 1.0):
        D[1, 1] = -1.0
    R = V @ D @ U.T
    t = qc - R @ pc
    res = np.linalg.norm((R @ P.T).T + t - Q, axis=1)
    return R, t, res


def _apply(R, t, pts):
    return (R @ np.asarray(pts, float).T).T + t


def _height(doc, comp):
    hs = [b.overall_height for b in doc.bodies if b.component == comp.index]
    hs = [h for h in hs if h and h > 0]
    if hs:
        return max(hs), "3D body"
    return (comp.height or None), "component height" if comp.height else None


def _holes(doc):
    out = []
    for p in doc.pads:
        if p.hole <= 0:
            continue
        free = p.component is None
        part = None if free else doc.components[p.component].designator
        if free or (part and HOLE_PART.match(part)):
            out.append({"ref": part or f"pad {p.name}", "x": p.x, "y": p.y, "hole": p.hole,
                        "plated": p.plated})
    return out


def check(a, b, maps, facing=False, tol=0.1):
    """The stack report (dict). a, b: PcbDoc; maps: [(a_des, b_des)]."""
    geom.require()
    pairs, unpaired = [], []
    for ad, bd in maps:
        pa, pb = _pins(a, ad), _pins(b, bd)
        for name in sorted(set(pa) | set(pb), key=_natural):
            if name in pa and name in pb:
                pairs.append((ad, bd, name, pa[name], pb[name]))
            else:
                unpaired.append({"a": f"{ad}.{name}" if name in pa else None,
                                 "b": f"{bd}.{name}" if name in pb else None})
    if len(pairs) < 2:
        raise ValueError("need at least two pins that pair by name to place the boards")
    flip = np.array([[-1.0, 0.0], [0.0, 1.0]]) if facing else np.eye(2)
    src = [tuple(flip @ np.array([q.x, q.y])) for *_r, q in pairs]
    dst = [(p.x, p.y) for *_r, p, _q in pairs]
    R, t, res = fit(src, dst)
    Rm, tm, res_m = fit(src, dst, reflect=True)
    rot = math.degrees(math.atan2(R[1, 0], R[0, 0]))
    out = {"facing": facing, "tolerance_mm": tol,
           "transform": {"rotation_deg": round(rot, 3), "shift_mm": [round(float(v), 4) for v in t]},
           "pins": [], "unpaired": unpaired, "problems": []}
    for (ad, bd, name, p, q), r in zip(pairs, res):
        row = {"pin": name, "a": f"{ad}.{name}", "b": f"{bd}.{name}",
               "misalignment_mm": round(float(r), 4),
               "a_net": a.nets[p.effective_net].name if p.effective_net is not None else None,
               "b_net": b.nets[q.effective_net].name if q.effective_net is not None else None}
        row["net_check"] = _net_check(row["a_net"], row["b_net"])
        out["pins"].append(row)
    worst = float(res.max())
    out["max_misalignment_mm"] = round(worst, 4)
    p1 = [r for r in out["pins"] if r["pin"] in ("1", "A1")]
    if p1:
        out["pin1_misalignment_mm"] = p1[0]["misalignment_mm"]
    if worst > tol:
        if float(res_m.max()) <= tol:
            out["problems"].append(
                "the pins only line up mirrored: B's connector is on the wrong side or its "
                "footprint is mirrored" + ("" if facing else " (or the boards face each other: "
                                           "--facing)"))
        else:
            alt = _geometric_match(src, dst, pairs)
            if alt:
                out["problems"].append("pin 1 does not land on pin 1: by position the pins pair "
                                       "as " + ", ".join(alt[:6]) + (" ..." if len(alt) > 6 else ""))
            else:
                out["problems"].append(f"the pins do not line up: worst {worst:.3f} mm")
    if abs(((rot + 180.0) % 360.0) - 180.0) > 1.0:
        out["notes"] = [f"B mates only turned {rot:.1f} degrees against A; if the boards are "
                        "meant to sit square, a connector is turned round"]
    bad_nets = [r for r in out["pins"] if r["net_check"] not in ("ok", "same name")]
    for r in bad_nets:
        if r["net_check"].startswith(("ground", "power meets")):
            out["problems"].append(f"{r['a']} ({r['a_net']}) meets {r['b']} ({r['b_net']}): "
                                   f"{r['net_check']}")

    # mounting holes after the fit
    ha, hb = _holes(a), _holes(b)
    holes = []
    for h in hb:
        x, y = _apply(R, t, [tuple(flip @ np.array([h["x"], h["y"]]))])[0]
        best = min(ha, key=lambda g: math.hypot(g["x"] - x, g["y"] - y), default=None)
        row = {"b": h["ref"], "b_hole": h["hole"], "at_on_a": [round(float(x), 3), round(float(y), 3)]}
        if best is not None:
            d = math.hypot(best["x"] - x, best["y"] - y)
            row.update({"a": best["ref"], "a_hole": best["hole"], "offset_mm": round(d, 4)})
            if d > max(tol, 0.2):
                out["problems"].append(f"B's hole {h['ref']} lands {d:.2f} mm from A's nearest "
                                       f"hole {best['ref']}")
        holes.append(row)
    out["holes"] = holes

    # heights
    hts = {}
    for ad, bd in maps:
        hA, srcA = _height(a, a.component(ad))
        hB, srcB = _height(b, b.component(bd))
        hts[f"{ad}:{bd}"] = {"a_mm": hA, "a_from": srcA, "b_mm": hB, "b_from": srcB,
                             "sum_mm": round(hA + hB, 3) if hA and hB else None}
    out["connector_heights"] = hts
    sums = [v["sum_mm"] for v in hts.values() if v["sum_mm"]]
    if sums:
        stack = min(sums)
        out["stack_height_mm"] = stack
        out["stack_note"] = ("sum of the mating connectors' body heights; the mated height in "
                             "the connector datasheet (engagement) is what counts")
        ob = geom.board_shape(b)
        overlap = None
        if ob is not None:
            from shapely import affinity
            g = ob
            if facing:
                g = affinity.scale(g, xfact=-1, yfact=1, origin=(0, 0))
            m = [R[0, 0], R[0, 1], R[1, 0], R[1, 1], float(t[0]), float(t[1])]
            overlap = affinity.affine_transform(g, m)
        tall_a = _tallest(a, side=L.TOP, region=overlap, skip={ad for ad, _ in maps})
        tall_b = _tallest(b, side=L.TOP if facing else L.BOTTOM, region=None,
                          skip={bd for _, bd in maps})
        out["tallest_between"] = {"a": tall_a, "b": tall_b}
        if tall_a and tall_b:
            room = stack - tall_a["height_mm"] - tall_b["height_mm"]
            out["clearance_mm"] = round(room, 3)
            if room < 0:
                out["problems"].append(f"{tall_a['component']} on A and {tall_b['component']} on "
                                       f"B together stand {-room:.2f} mm taller than the stack")
    out["ok"] = not out["problems"]
    return out


def _tallest(doc, side, region=None, skip=()):
    best = None
    for c in doc.components:
        if c.designator in skip or c.layer != side:
            continue
        h, _src = _height(doc, c)
        if not h:
            continue
        if region is not None:
            from shapely.geometry import Point
            if not region.contains(Point(c.x, c.y)):
                continue
        if best is None or h > best["height_mm"]:
            best = {"component": c.designator, "height_mm": round(h, 3)}
    return best


def _geometric_match(src, dst, pairs):
    """Pin pairs by position for the best of four rotations about the pins'
    centre (0/90/180/270): names that differ, e.g. 'B.1 on A.10'."""
    P, Q = np.asarray(src, float), np.asarray(dst, float)
    pc, qc = P.mean(0), Q.mean(0)
    best = None
    for k in range(4):
        a = math.pi / 2 * k
        R = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        M = (R @ (P - pc).T).T + qc
        idx = [int(np.argmin(np.linalg.norm(Q - m, axis=1))) for m in M]
        cost = sum(float(np.linalg.norm(Q[j] - m)) for j, m in zip(idx, M))
        if best is None or cost < best[0]:
            best = (cost, idx)
    _cost, idx = best
    out = []
    for i, j in enumerate(idx):
        if i != j:
            out.append(f"{pairs[i][1]}.{pairs[i][2]} on {pairs[j][0]}.{pairs[j][2]}")
    return out


def _net_check(na, nb):
    from .autoroute import POWER_NAME
    if na is None or nb is None:
        return "ok" if na == nb else "one side has no net"
    if na.upper() == nb.upper():
        return "same name"
    ga, gb = bool(GROUND.search(na)), bool(GROUND.search(nb))
    if ga != gb:
        return "ground meets a non-ground net"
    pa, pb = bool(POWER_NAME.match(na)), bool(POWER_NAME.match(nb))
    if pa != pb:
        return "power meets a non-power net"
    if pa and pb:
        return "power nets with different names (check the pairing)"
    return "different names (check the pairing)"


def _natural(s):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", str(s))]


def cmd_stack(args):
    from .pcbdoc import PcbDoc
    a, b = PcbDoc.open(args.a), PcbDoc.open(args.b)
    maps = []
    for m in args.map:
        if ":" not in m:
            raise ValueError(f"--map wants A_PART:B_PART, not {m!r}")
        maps.append(tuple(m.split(":", 1)))
    rep = check(a, b, maps, facing=args.facing, tol=args.tol)
    if args.json:
        print(json.dumps(rep, indent=1, default=float))
    else:
        tr = rep["transform"]
        print(f"B onto A: rotate {tr['rotation_deg']} deg, shift {tr['shift_mm']} mm; worst pin "
              f"{rep['max_misalignment_mm']} mm" + (f", pin 1 {rep['pin1_misalignment_mm']} mm"
                                                    if "pin1_misalignment_mm" in rep else ""))
        for r in rep["pins"]:
            print(f"  {r['a']:>8} {r['a_net'] or '-':>14}  <->  {r['b']:<8} {r['b_net'] or '-':<14}"
                  f" {r['misalignment_mm']:.3f} mm  {r['net_check']}")
        for h in rep["holes"]:
            if "a" in h:
                print(f"  hole {h['b']} -> {h['a']}: {h['offset_mm']} mm "
                      f"(holes {h['b_hole']} / {h['a_hole']} mm)")
        if rep.get("stack_height_mm"):
            print(f"  stack height {rep['stack_height_mm']} mm (connector bodies); clearance "
                  f"{rep.get('clearance_mm')} mm")
        for p in rep["problems"]:
            print(f"  PROBLEM: {p}")
        print("ok" if rep["ok"] else f"{len(rep['problems'])} problem(s)")
    return 0 if rep["ok"] else 1


def register_commands(command):
    def build(p):
        p.add_argument("a", help="the lower board")
        p.add_argument("b", help="the board above it")
        p.add_argument("--map", action="append", required=True, metavar="A_PART:B_PART")
        p.add_argument("--facing", action="store_true", help="B is upside down")
        p.add_argument("--tol", type=float, default=0.1)
        p.add_argument("--json", action="store_true")
    command("stack", cmd_stack, "check two boards joined by mating headers", build)
