"""Steady-state board temperature rise, for screening. Not CFD and not FEA.

    res = solve(doc, pdn=[pdn_result], powers={"Q1": 1.5})
    res.report()   # rise per layer, hottest spots, per-component rise, assumptions

Model (2.5-D: one node per copper layer per grid cell):
  * in-plane conduction in each copper layer where there is copper (any
    net), plus the dielectric half-layers on either side of it;
  * through-thickness conduction between neighbouring copper layers across
    the dielectric, from the board's stack-up;
  * plated barrels (vias, through-hole pads) join the layers they span;
  * natural convection and radiation from both outer faces to ambient: a
    horizontal board by default (upper face h = 1.32 (dT/L)^0.25, lower face
    0.59 (dT/L)^0.25, L = area / perimeter), radiation linearised with the
    emissivity; both are re-evaluated at the solved temperature;
  * heat from pdn.py solves (rho J^2 t in every copper cell, I^2 R in every
    barrel) and component dissipation spread over each component's pads.
Board edges are adiabatic. No forced air, no conduction into connectors,
leads, heatsinks or the enclosure, no heat capacity (steady state only),
and copper resistance is taken at the PDN solve's temperature.

ipc_checks() runs this model on the configurations behind published
IPC-2152 figures and returns model vs published rise.
"""

import math

import numpy as np

from . import geom
from . import layers as L
from .raster import Grid, draw, layer_nets

SIGMA = 5.670374e-8      # W / m^2 K^4
K_COPPER = 390.0         # W / m K
K_FR4 = 0.35             # W / m K (screening value; in-plane is higher, through-plane lower)
SOLDER_H = 6.7e5         # W / m^2 K: a 75 um joint of k 50 W/m K (SAC solder)


class ThermalError(ValueError):
    pass


def natural_h(rise_c, length_m, face):
    """Laminar natural convection from a horizontal plate, W/m^2 K."""
    dt = max(rise_c, 0.5)
    c = 1.32 if face == "up" else 0.59
    return c * (dt / max(length_m, 1e-3)) ** 0.25


def radiation_h(rise_c, ambient_c, emissivity):
    ta = ambient_c + 273.15
    ts = ta + max(rise_c, 0.0)
    return emissivity * SIGMA * (ts * ts + ta * ta) * (ts + ta)


def _sparse_solve(n, rows, cols, vals, rhs):
    try:
        import scipy.sparse as sp
        import scipy.sparse.linalg as spla
        m = sp.csr_matrix((vals, (rows, cols)), shape=(n, n))
        return spla.spsolve(m.tocsc(), rhs, permc_spec="MMD_AT_PLUS_A")
    except ImportError:
        from .pdn import _cg
        return _cg(n, rows, cols, vals, rhs)


class ThermalResult:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def report(self, top=5):
        hot = []
        for lay, m in self.rise.items():
            if np.isfinite(m).any():
                i = int(np.nanargmax(m))
                r, c = np.unravel_index(i, m.shape)
                x, y = self.grid.center(r, c)
                hot.append({"layer": L.name(lay), "x": round(float(x), 3), "y": round(float(y), 3),
                            "rise_c": round(float(m[r, c]), 3)})
        hot.sort(key=lambda h: -h["rise_c"])
        comps = sorted(self.components.items(), key=lambda kv: -kv[1])[:top * 4]
        return {"ambient_c": self.ambient_c, "max_rise_c": round(self.max_rise, 3),
                "hottest": hot[:top], "heat_w": round(self.heat_w, 4),
                "components_rise_c": {k: round(v, 3) for k, v in comps},
                "h_w_per_m2k": {k: round(v, 3) for k, v in self.h.items()},
                "resolution_mm": self.grid.res, "assumptions": ASSUMPTIONS}


ASSUMPTIONS = [
    "steady state, still air, horizontal board, ambient everywhere",
    "convection and radiation from both outer faces only; edges adiabatic",
    "no heat leaves through connectors, leads, screws, heatsinks or the enclosure",
    "one temperature per copper layer per cell; dielectric conductivity uniform",
    "copper resistance at the PDN solve's temperature (rerun the PDN hotter to couple)",
    "a powered component is one package node tied to its pads through 75 um solder "
    "joints; its figure is the case, add P x Rth(j-c) for the junction",
    "a screening estimate: expect tens of percent, not a few degrees",
]


def solve(doc, pdn=(), powers=None, ambient_c=25.0, emissivity=0.9, k_dielectric=K_FR4,
          k_copper=K_COPPER, res=0.5, h=None, orientation="horizontal", index=None,
          iterations=3, grid=None):
    """Temperature rise over ambient on every copper layer.

    pdn: pdn.solve results whose Joule heat is the source. powers:
    {designator: watts}, each a package node tied to its pads on its own side
    through solder joints (its reported rise is the case).
    h: {"top": W/m^2K, "bottom": W/m^2K} to fix the face coefficients
    (convection + radiation) instead of computing them.
    """
    from .connect import CopperIndex
    geom.require()
    index = index or CopperIndex(doc)
    order = [lay for lay in doc.copper_layers]
    if not order:
        raise ThermalError("board has no copper layers")
    board = geom.board_shape(doc)
    if board is None or board.is_empty:
        raise ThermalError("board has no outline")
    g = grid or Grid.for_board(doc, res, margin=0.0)
    inside = g.empty()
    draw(inside, g, board)
    if not inside.any():
        raise ThermalError("board outline covers no grid cell")
    cell_m2 = (g.res * 1e-3) ** 2

    # copper fill fraction per layer: rasterise at a finer pitch and average
    sub = 4
    fine = Grid(g.x0, g.y0, g.x0 + g.nx * g.res, g.y0 + g.ny * g.res, g.res / sub)
    fill = {}
    for lay in order:
        m = layer_nets(doc, lay, fine, index) != -1
        m = m[:g.ny * sub, :g.nx * sub]
        if m.shape != (g.ny * sub, g.nx * sub):
            pad = np.zeros((g.ny * sub, g.nx * sub), dtype=bool)
            pad[:m.shape[0], :m.shape[1]] = m
            m = pad
        fill[lay] = m.reshape(g.ny, sub, g.nx, sub).mean(axis=(1, 3))

    # stack: copper thickness and the dielectric between neighbours
    stack = doc.layer_stack
    t_cu = {lay: doc.copper_thickness(lay) for lay in order}
    diel = []
    for a, b in zip(order, order[1:]):
        d = stack.get(a, {}).get("diel_mm", 0.0)
        diel.append(d if d > 0 else max(doc.thickness - sum(t_cu.values()), 0.1) / (len(order) - 1))
    if len(order) == 1:
        diel = []
    half = {lay: 0.0 for lay in order}
    for i, d in enumerate(diel):
        half[order[i]] += d / 2
        half[order[i + 1]] += d / 2
    if len(order) == 1:
        half[order[0]] = doc.thickness

    ids, n = {}, 0
    for lay in order:
        a = np.full(g.shape, -1, dtype=np.int64)
        k = int(inside.sum())
        a[inside] = np.arange(n, n + k)
        ids[lay] = a
        n += k

    rows, cols, vals = [], [], []

    lat = {}
    for lay in order:
        sheet = k_copper * t_cu[lay] * 1e-3 * fill[lay] + k_dielectric * half[lay] * 1e-3
        lat[lay] = sheet
        a = ids[lay]
        for (x, y, sx, sy) in ((a[:, :-1], a[:, 1:], sheet[:, :-1], sheet[:, 1:]),
                               (a[:-1, :], a[1:, :], sheet[:-1, :], sheet[1:, :])):
            ok = (x >= 0) & (y >= 0)
            gg = 2 * sx[ok] * sy[ok] / (sx[ok] + sy[ok])
            rows.append(np.concatenate([x[ok], y[ok], x[ok], y[ok]]))
            cols.append(np.concatenate([x[ok], y[ok], y[ok], x[ok]]))
            vals.append(np.concatenate([gg, gg, -gg, -gg]))
    for i, d in enumerate(diel):
        a, b = ids[order[i]], ids[order[i + 1]]
        ok = (a >= 0) & (b >= 0)
        gg = np.full(int(ok.sum()), k_dielectric * cell_m2 / (d * 1e-3))
        rows.append(np.concatenate([a[ok], b[ok], a[ok], b[ok]]))
        cols.append(np.concatenate([a[ok], b[ok], b[ok], a[ok]]))
        vals.append(np.concatenate([gg, gg, -gg, -gg]))
    # plated barrels
    zpos, z = {}, 0.0
    for i, lay in enumerate(order):
        zpos[lay] = z
        if i < len(diel):
            z += diel[i]
    barrels = [v for v in doc.vias if v.hole > 0] + [p for p in doc.pads if p.hole > 0 and p.plated]
    brow, bcol, bval = [], [], []
    for b in barrels:
        r, c = g.cell(b.x, b.y)
        if not (0 <= r < g.ny and 0 <= c < g.nx) or not inside[r, c]:
            continue
        area = math.pi * ((b.hole / 2 + 0.025) ** 2 - (b.hole / 2) ** 2) * 1e-6
        span = [lay for lay in order if (b.spans(lay, order) if b.KIND == "via" else True)]
        for la, lb in zip(span, span[1:]):
            length = max(abs(zpos[lb] - zpos[la]), 0.05) * 1e-3
            gg = k_copper * area / length
            i, j = ids[la][r, c], ids[lb][r, c]
            brow += [i, j, i, j]
            bcol += [i, j, j, i]
            bval += [gg, gg, -gg, -gg]
    if brow:
        rows.append(np.array(brow, dtype=np.int64))
        cols.append(np.array(bcol, dtype=np.int64))
        vals.append(np.array(bval))

    # a powered component: one package node tied to its pad cells
    packages = {}
    for des, w in (powers or {}).items():
        a = _component_cells(ids, g, doc, des, order)
        gg = np.full(len(a), SOLDER_H * cell_m2)
        k = np.full(len(a), n)
        rows.append(np.concatenate([k, a, k, a]))
        cols.append(np.concatenate([k, a, a, k]))
        vals.append(np.concatenate([gg, gg, -gg, -gg]))
        packages[des] = (n, float(w))
        n += 1

    # heat sources
    q = np.zeros(n)
    heat = 0.0
    for res_ in (pdn if isinstance(pdn, (list, tuple)) else [pdn]):
        heat += _add_pdn_heat(q, ids, g, res_, doc)
    for des, (node, w) in packages.items():
        q[node] += w
        heat += w

    rows_a = np.concatenate([np.asarray(x, dtype=np.int64) for x in rows])
    cols_a = np.concatenate([np.asarray(x, dtype=np.int64) for x in cols])
    vals_a = np.concatenate([np.asarray(x, dtype=float) for x in vals])
    top, bottom = order[0], order[-1]
    bx0, by0, bx1, by1 = board.bounds
    char_len = board.area / max(board.length, 1e-9) * 1e-3   # area / perimeter, m
    rise_guess = 10.0
    hv = {}
    t = np.zeros(n)
    for _ in range(max(1, iterations)):
        if h:
            hv = {"top": float(h["top"]), "bottom": float(h["bottom"])}
        elif orientation == "vertical":
            hc = 1.42 * (max(rise_guess, 0.5) / max((by1 - by0) * 1e-3, 1e-3)) ** 0.25
            hv = {"top": hc, "bottom": hc}
        else:
            hv = {"top": natural_h(rise_guess, char_len, "up"),
                  "bottom": natural_h(rise_guess, char_len, "down")}
        if not h:
            hr = radiation_h(rise_guess, ambient_c, emissivity)
            hv = {"top": hv["top"] + hr, "bottom": hv["bottom"] + hr}
        sr = []
        for lay, face in ((top, "top"), (bottom, "bottom")):
            a = ids[lay][inside]
            sr.append((a, np.full(len(a), hv[face] * cell_m2)))
            if top == bottom:
                break
        d_rows = np.concatenate([a for a, _ in sr])
        d_vals = np.concatenate([v for _, v in sr])
        t = _sparse_solve(n, np.concatenate([rows_a, d_rows]), np.concatenate([cols_a, d_rows]),
                          np.concatenate([vals_a, d_vals]), q)
        surface = np.concatenate([t[ids[top][inside]], t[ids[bottom][inside]]])
        if heat <= 0 or h:
            break
        new_guess = float(np.average(surface, weights=np.maximum(surface, 1e-12)))
        if abs(new_guess - rise_guess) < 0.05 * max(rise_guess, 1.0):
            rise_guess = new_guess
            break
        rise_guess = max(new_guess, 0.5)
    rise = {}
    for lay in order:
        m = np.full(g.shape, np.nan)
        a = ids[lay]
        m[inside] = t[a[inside]]
        rise[lay] = m
    comps = {}
    for c in doc.components:
        cells = []
        for p in doc.pads:
            if p.component == c.index:
                r, cc = g.cell(p.x, p.y)
                if 0 <= r < g.ny and 0 <= cc < g.nx and inside[r, cc]:
                    lay = top if (p.layer in (L.TOP, L.MULTI) or c.layer != L.BOTTOM) else bottom
                    cells.append(rise[lay][r, cc])
        if cells:
            comps[c.designator] = float(np.nanmax(cells))
    for des, (node, w) in packages.items():
        comps[des] = float(t[node])
    return ThermalResult(grid=g, rise=rise, max_rise=float(np.nanmax(t)) if n else 0.0,
                         heat_w=heat, components=comps, h=hv, ambient_c=ambient_c,
                         layers=order)


def _add_pdn_heat(q, ids, g, res, doc):
    """Joule heat of a pdn.solve result binned onto the thermal grid; W.
    Uses the solver's per-cell dissipation, which already carries each via
    barrel's I^2 R at the cells it joins."""
    total = 0.0
    pg = res.grid
    for lay, maps in res.maps.items():
        if lay not in ids:
            continue
        pm = maps.get("p")
        if pm is None:
            raise ValueError("this pdn result has no dissipation map; re-run pdn.solve")
        ok = np.isfinite(pm) & (pm > 0)
        if not ok.any():
            continue
        p = pm[ok]
        rr, cc = np.nonzero(ok)
        xs = pg.x0 + (cc + 0.5) * pg.res
        ys = pg.y1 - (rr + 0.5) * pg.res
        tr = np.floor((g.y1 - ys) / g.res).astype(np.int64)
        tc = np.floor((xs - g.x0) / g.res).astype(np.int64)
        inb = (tr >= 0) & (tr < g.ny) & (tc >= 0) & (tc < g.nx)
        p, tr, tc = p[inb], tr[inb], tc[inb]
        a = ids[lay][tr, tc]
        keep = a >= 0
        np.add.at(q, a[keep], p[keep])
        total += float(p[keep].sum())
    return total


def _component_cells(ids, g, doc, designator, order):
    """Node ids of the cells under a component's pads on its own side."""
    comp = doc.component(designator)
    side = order[-1] if comp.layer == L.BOTTOM else order[0]
    cells = []
    for p in doc.pads:
        if p.component == comp.index:
            shape = geom.pad_shape(p, side if p.layer == L.MULTI else p.layer)
            if shape.is_empty:
                continue
            m = g.empty()
            draw(m, g, shape)
            if not m.any():
                r, c = g.cell(p.x, p.y)
                if 0 <= r < g.ny and 0 <= c < g.nx:
                    m[r, c] = True
            cells.append(m)
    if not cells:
        r, c = g.cell(comp.x, comp.y)
        m = g.empty()
        if 0 <= r < g.ny and 0 <= c < g.nx:
            m[r, c] = True
        cells.append(m)
    mask = np.any(cells, axis=0)
    a = ids[side][mask]
    a = a[a >= 0]
    if not len(a):
        raise ThermalError(f"{designator} is outside the board")
    return a


# ---------------------------------------------------------------- IPC-2152 checks

IPC_CASES = [
    {"name": "IPC-2152 universal chart: 10 A, 520 sq mil, 3 oz, 0.070 in polyimide, still air",
     "published_rise_c": 20.0, "current": 10.0, "copper_mm": 0.105, "area_sqmil": 520,
     "thickness_mm": 1.778, "board_mm": (300.0, 120.0), "layers": 2, "k_dielectric": 0.543,
     "source": "IPC-2152 Fig. 5-2 as read in J. Olson, 'Determining circuit board current "
               "carrying capability' (2010), worked example"},
    {"name": "Olson worked example: 10 A, 180 mil 1 oz external, FR4 0.063 in, 3x5 in, "
             "1 oz planes 20 mil below",
     "published_rise_c": 20.0, "current": 10.0, "copper_mm": 0.035, "width_mm": 4.572,
     "thickness_mm": 1.6, "board_mm": (127.0, 76.2), "layers": 4, "planes": True,
     "k_dielectric": 0.35,
     "source": "J. Olson (2010): IPC-2152 universal chart with copper, plane, thickness and "
               "material multipliers and 5 % de-rating gives 180 mil for 20 C"},
    {"name": "Conservative chart (old internal chart, ~free air): 4 A, 300 sq mil, 10 C",
     "published_rise_c": 10.0, "current": 4.0, "copper_mm": 0.035, "area_sqmil": 300,
     "thickness_mm": 1.6, "board_mm": (150.0, 100.0), "layers": 2, "k_dielectric": 0.35,
     "upper_bound": True,
     "source": "IPC-2152 conservative chart as read in J. Olson (2010), worked example"},
    {"name": "Conservative chart (old internal chart, ~free air): 10 A, 680 sq mil, 20 C",
     "published_rise_c": 20.0, "current": 10.0, "copper_mm": 0.035, "area_sqmil": 680,
     "thickness_mm": 1.6, "board_mm": (150.0, 100.0), "layers": 2, "k_dielectric": 0.35,
     "upper_bound": True,
     "source": "J. Olson (2010) example: conservative chart gives 680 sq mil for 10 A at 20 C"},
]


def ipc_checks(res=0.5):
    """Model rise against published IPC-2152-style figures, one dict per case."""
    from .pdn import solve as pdn_solve
    from .synth import Synth
    out = []
    for case in IPC_CASES:
        length, width = case["board_mm"]
        t = case["copper_mm"]
        if "width_mm" in case:
            w = case["width_mm"]
        else:
            w = case["area_sqmil"] * 0.0254 ** 2 / t
        s = Synth(length, width, layers=case["layers"], copper_mm=t,
                  board_thickness=case["thickness_mm"])
        s.net("TRACE")
        y = width / 2
        s.track("Top Layer", 0.5, y, length - 0.5, y, w, net="TRACE")
        s.component("J1", "PAD", 0.5, y)
        s.pad("J1", "1", 0.5 + w / 2, y, w, w, net="TRACE")
        s.component("J2", "PAD", length - 0.5, y)
        s.pad("J2", "1", length - 0.5 - w / 2, y, w, w, net="TRACE")
        if case.get("planes"):
            for k, lay in enumerate(("Mid-Layer 1", "Mid-Layer 2")):
                s.net(f"PLANE{k + 1}")
                s.region(lay, [(0, 0), (length, 0), (length, width), (0, width)],
                         net=f"PLANE{k + 1}")
        doc = s.build()
        temp = 25.0 + case["published_rise_c"]
        pdn = pdn_solve(doc, "TRACE", ["J1.1"], ["J2.1"], case["current"], res=min(res, 0.25),
                        temp_c=temp)
        th = solve(doc, pdn=[pdn], ambient_c=25.0, k_dielectric=case["k_dielectric"], res=res)
        mid = th.rise[doc.copper_layers[0]]
        r, c = th.grid.cell(length / 2, y)
        rise = float(mid[r, c])
        out.append({"case": case["name"], "published_rise_c": case["published_rise_c"],
                    "model_rise_c": round(rise, 2),
                    "ratio": round(rise / case["published_rise_c"], 3),
                    "kind": "upper bound (free-air chart)" if case.get("upper_bound") else
                    "expected", "trace_width_mm": round(w, 3), "source": case["source"]})
    return out


# ---------------------------------------------------------------- CLI

def _parse_flow(doc, spec):
    """'NET:AMPS:SRC1+SRC2>SINK1+SINK2' -> (net, amps, sources, sinks)."""
    try:
        net, amps, ends = spec.split(":", 2)
        src, snk = ends.split(">", 1)
        return net, float(amps), src.split("+"), snk.split("+")
    except ValueError:
        raise ThermalError(f"--flow {spec!r}: use NET:AMPS:SRC>SINK (pads like J1.1, '+' joins several)")


def cmd_thermal(args):
    import json
    from .pcbdoc import PcbDoc
    from .pdn import solve as pdn_solve
    if args.ipc_check:
        rows = ipc_checks(res=args.res)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                print(f"  {r['model_rise_c']:6.1f} C model vs {r['published_rise_c']:5.1f} C published "
                      f"({r['kind']}): {r['case']}")
        return 0
    if not args.board:
        raise ThermalError("give a board (or --ipc-check)")
    doc = PcbDoc.open(args.board)
    flows = [_parse_flow(doc, f) for f in (args.flow or [])]
    powers = {}
    for p in args.power or []:
        des, _, w = p.partition("=")
        powers[des] = float(w)
    if not flows and not powers:
        raise ThermalError("nothing heats the board: give --flow and/or --power")
    sols = [pdn_solve(doc, net, src, snk, amps, res=args.pdn_res,
                      temp_c=args.ambient + args.copper_rise)
            for net, amps, src, snk in flows]
    res = solve(doc, pdn=sols, powers=powers, ambient_c=args.ambient, res=args.res)
    rep = res.report()
    rep["flows"] = [{"net": f[0], "current_a": f[1], "drop_mv": round(s.drop_v * 1e3, 3),
                     "power_w": round(s.power_w, 4)} for f, s in zip(flows, sols)]
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(f"max rise {rep['max_rise_c']} C over {rep['ambient_c']} C ambient; "
              f"{rep['heat_w']} W into the board")
        for h in rep["hottest"]:
            print(f"  {h['rise_c']:7.2f} C  {h['layer']} ({h['x']}, {h['y']})")
        for k, v in list(rep["components_rise_c"].items())[:10]:
            print(f"  {v:7.2f} C  {k}")
        print("assumptions: " + "; ".join(rep["assumptions"]))
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("board", nargs="?")
        p.add_argument("--flow", action="append",
                       help="NET:AMPS:SRC>SINK, e.g. VBAT:30:J1.1>Q1.5+Q1.6 (repeatable)")
        p.add_argument("--power", action="append", help="component dissipation, e.g. Q1=1.8")
        p.add_argument("--ambient", type=float, default=25.0)
        p.add_argument("--copper-rise", type=float, default=20.0,
                       help="copper temperature assumed for resistance, above ambient")
        p.add_argument("--res", type=float, default=0.5, help="thermal grid, mm")
        p.add_argument("--pdn-res", type=float, default=0.2)
        p.add_argument("--ipc-check", action="store_true",
                       help="run the model on published IPC-2152 configurations")
        p.add_argument("--json", action="store_true")
    command("thermal", cmd_thermal, "screening estimate of board temperature rise", build)
