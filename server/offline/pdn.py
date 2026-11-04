"""DC current flow in one net's copper: voltage drop, current density, power.

Model (a resistor network on the raster grid):
  * each copper pixel of the net is a node; neighbouring pixels on a layer
    are joined by the copper's sheet conductance t / rho;
  * vias and through-hole pads join the layers their barrel reaches through
    a plated barrel (area pi*((r+p)^2 - r^2), length the layer spacing);
  * every source or sink pad is one node tied to all of its pixels, so a
    pad is an equipotential, like a soldered lead;
  * current enters at the sources (split evenly) and leaves at the sinks,
    which are held at 0 V.
Copper resistivity is 1.72e-8 ohm*m at 20 C with alpha 0.00393/K.

This is DC and two-dimensional: no skin effect, no solder or lead
resistance, no current crowding through the thickness of the copper.
"""

import math

import numpy as np

from . import geom
from . import layers as L
from .raster import Grid, draw, net_copper

RHO_20 = 1.72e-5      # ohm * mm
ALPHA = 0.00393
PLATING = 0.025       # mm of barrel copper (1 mil)
TIE = 1e4             # S, pad node to its pixels


class PdnError(ValueError):
    pass


def rho_at(temp_c):
    return RHO_20 * (1 + ALPHA * (temp_c - 20.0))


def parse_terminal(doc, ref):
    """'U1.3' / 'U1-3' -> [pads]; 'x,y' or 'x,y@Top Layer' -> point terminal."""
    ref = str(ref).strip()
    if "," in ref:
        xy, _, lay = ref.partition("@")
        x, y = (float(v) for v in xy.split(","))
        return {"kind": "point", "ref": ref, "x": x, "y": y,
                "layer": L.parse(lay) if lay else None}
    for sep in (".", "-", ":"):
        if sep in ref:
            des, pin = ref.split(sep, 1)
            if doc.has_component(des):
                comp = doc.component(des)
                pads = [p for p in doc.pads if p.component == comp.index and p.name == pin]
                if not pads:
                    raise PdnError(f"{des} has no pad named {pin!r}")
                return {"kind": "pads", "ref": ref, "pads": pads}
    if doc.has_component(ref):
        comp = doc.component(ref)
        return {"kind": "pads", "ref": ref, "pads": [p for p in doc.pads if p.component == comp.index]}
    raise PdnError(f"cannot find terminal {ref!r} (use U1.3 or x,y[@layer])")


class Network:
    def __init__(self):
        self.rows, self.cols, self.vals = [], [], []
        self.n = 0

    def add(self, i, j, g):
        i = np.asarray(i, dtype=np.int64)
        j = np.asarray(j, dtype=np.int64)
        g = np.broadcast_to(np.asarray(g, dtype=float), i.shape)
        self.rows += [i, j, i, j]
        self.cols += [i, j, j, i]
        self.vals += [g, g, -g, -g]


def _solve(n, rows, cols, vals, rhs, fixed):
    """Solve G v = rhs with v[fixed] = 0. Returns v (length n)."""
    free = np.ones(n, dtype=bool)
    free[fixed] = False
    remap = -np.ones(n, dtype=np.int64)
    remap[free] = np.arange(int(free.sum()))
    keep = free[rows] & free[cols]
    r, c, v = remap[rows[keep]], remap[cols[keep]], vals[keep]
    m = int(free.sum())
    b = rhs[free]
    try:
        import scipy.sparse as sp
        import scipy.sparse.linalg as spl
        A = sp.csr_matrix((v, (r, c)), shape=(m, m))
        if m <= 1_200_000:
            # symmetric minimum-degree ordering: ~5x faster than the default
            # COLAMD on these Laplacians (3.4 s vs 17.5 s at 361k unknowns)
            x = spl.spsolve(A.tocsc(), b, permc_spec="MMD_AT_PLUS_A")
        else:
            d = A.diagonal()
            M = sp.diags(1.0 / np.where(d > 0, d, 1.0))
            x, info = spl.cg(A, b, M=M, rtol=1e-10, maxiter=20000)
            if info != 0:
                raise PdnError(f"iterative solve did not converge (info={info})")
    except ImportError:
        x = _cg(m, r, c, v, b)
    out = np.zeros(n)
    out[free] = x
    return out


def _cg(m, r, c, v, b, tol=1e-10, maxiter=50000):
    """Jacobi-preconditioned conjugate gradient on a COO matrix, numpy only."""
    diag = np.bincount(r[r == c], weights=v[r == c], minlength=m)
    diag[diag <= 0] = 1.0

    def mv(x):
        return np.bincount(r, weights=v * x[c], minlength=m)

    x = np.zeros(m)
    res = b - mv(x)
    z = res / diag
    p = z.copy()
    rz = res @ z
    bn = np.linalg.norm(b) or 1.0
    for _ in range(maxiter):
        Ap = mv(p)
        alpha = rz / (p @ Ap)
        x += alpha * p
        res -= alpha * Ap
        if np.linalg.norm(res) / bn < tol:
            return x
        z = res / diag
        rz_new = res @ z
        p = z + (rz_new / rz) * p
        rz = rz_new
    raise PdnError("conjugate gradient did not converge; install scipy for a direct solve")


class PdnResult:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def report(self, top=5):
        worst = sorted(self.hotspots, key=lambda h: -h["j_a_per_mm2"])[:top]
        return {
            "net": self.net, "current_a": self.current, "resolution_mm": self.grid.res,
            "temperature_c": self.temp_c,
            "drop_mv": round(self.drop_v * 1e3, 4),
            "resistance_mohm": round(self.drop_v / self.current * 1e3, 5) if self.current else None,
            "power_w": round(self.power_w, 5),
            "sources_mv": {k: round(v * 1e3, 4) for k, v in self.terminal_v.items()},
            "max_current_density_a_per_mm2": [
                {"layer": L.name(h["layer"]), "x": round(h["x"], 3), "y": round(h["y"], 3),
                 "j_a_per_mm2": round(h["j_a_per_mm2"], 3)} for h in worst],
            "vias": sorted(({"x": round(v["x"], 3), "y": round(v["y"], 3),
                             "current_a": round(v["current_a"], 4)} for v in self.vias),
                           key=lambda v: -v["current_a"])[:top * 4],
            "layers": [L.name(x) for x in self.layers],
            "warnings": self.warnings,
            "model": "DC, 2-D sheet per layer, plated via barrels; no solder or lead resistance",
        }


def solve(doc, net, sources, sinks, current, res=0.2, temp_c=20.0, thickness=None,
          plating=PLATING, index=None, box=None):
    """Solve DC flow for `current` amps from `sources` to `sinks` on `net`.

    thickness: {layer: mm} overrides the stack's copper thickness.
    """
    from .connect import CopperIndex
    if current <= 0:
        raise PdnError("current must be positive")
    net_idx = net if isinstance(net, int) else doc.net_index(net)
    net_name = doc.nets[net_idx].name
    index = index or CopperIndex(doc)
    grid = Grid.for_board(doc, res, box=box)
    rho = rho_at(temp_c)
    warnings = []

    order = doc.copper_layers
    masks = {}
    for lay in order:
        m = net_copper(doc, lay, net_idx, grid, index)
        if m.any():
            masks[lay] = m
    if not masks:
        raise PdnError(f"net {net_name} has no copper")
    ids, n = {}, 0
    for lay, m in masks.items():
        a = np.full(m.shape, -1, dtype=np.int64)
        k = int(m.sum())
        a[m] = np.arange(n, n + k)
        ids[lay] = a
        n += k
    net_ = Network()

    def t_of(lay):
        if thickness and lay in thickness:
            return thickness[lay]
        return doc.copper_thickness(lay)

    for lay, a in ids.items():
        g = t_of(lay) / rho
        for x, y in ((a[:, :-1], a[:, 1:]), (a[:-1, :], a[1:, :])):
            both = (x >= 0) & (y >= 0)
            net_.add(x[both], y[both], g)

    # layer z positions for barrel lengths
    stack = doc.layer_stack
    z, zpos = 0.0, {}
    for lay in order:
        zpos[lay] = z
        st = stack.get(lay, {})
        z += st.get("copper_mm", 0.035) + (st.get("diel_mm", 0.0) if lay != L.BOTTOM else 0.0)
    if z <= 0.1:
        span = doc.thickness
        zpos = {lay: span * i / max(1, len(order) - 1) for i, lay in enumerate(order)}

    via_links = []
    barrels = [v for v in doc.vias if v.effective_net == net_idx]
    barrels += [p for p in doc.pads if p.effective_net == net_idx and p.hole > 0 and p.plated]
    for b in barrels:
        r = b.hole / 2
        area = math.pi * ((r + plating) ** 2 - r ** 2)
        row, col = grid.cell(b.x, b.y)
        if not (0 <= row < grid.ny and 0 <= col < grid.nx):
            continue
        reach = [lay for lay in order if lay in ids and ids[lay][row, col] >= 0 and
                 (b.spans(lay, order) if b.KIND == "via" else True)]
        for la, lb in zip(reach, reach[1:]):
            length = max(abs(zpos[lb] - zpos[la]), 0.05)
            g = area / (rho * length)
            i, j = ids[la][row, col], ids[lb][row, col]
            net_.add([i], [j], g)
            via_links.append((b, i, j, g))

    terminals = {}
    sh = geom.require()

    def terminal_nodes(term):
        nodes = []
        if term["kind"] == "pads":
            for p in term["pads"]:
                if p.effective_net != net_idx:
                    raise PdnError(f"{term['ref']} pad {p.name} is on net "
                                   f"{p.net_name}, not {net_name}")
                for lay in ids:
                    if not p.on_layer(lay):
                        continue
                    m = grid.empty()
                    draw(m, grid, geom.pad_shape(p, lay))
                    sel = ids[lay][m]
                    nodes.extend(int(v) for v in sel[sel >= 0])
        else:
            lays = [term["layer"]] if term["layer"] else list(ids)
            for lay in lays:
                if lay not in ids:
                    continue
                m = grid.empty()
                draw(m, grid, sh.Point(term["x"], term["y"]).buffer(max(res, 0.25)))
                sel = ids[lay][m]
                nodes.extend(int(v) for v in sel[sel >= 0])
        if not nodes:
            raise PdnError(f"terminal {term['ref']} touches none of {net_name}'s copper")
        return sorted(set(nodes))

    src_nodes, sink_nodes = [], []
    for ref in sources:
        t = parse_terminal(doc, ref)
        nodes = terminal_nodes(t)
        tid = n
        n += 1
        net_.add(np.full(len(nodes), tid), nodes, TIE)
        src_nodes.append(tid)
        terminals[t["ref"]] = tid
    for ref in sinks:
        t = parse_terminal(doc, ref)
        nodes = terminal_nodes(t)
        tid = n
        n += 1
        net_.add(np.full(len(nodes), tid), nodes, TIE)
        sink_nodes.append(tid)
    if not src_nodes or not sink_nodes:
        raise PdnError("need at least one source and one sink")

    rows = np.concatenate(net_.rows)
    cols = np.concatenate(net_.cols)
    vals = np.concatenate(net_.vals)

    # Copper the sinks cannot reach floats: pin it (it carries no current) and
    # report it as NaN. A source that floats is an error, not a result.
    reach = _reachable(n, rows, cols, sink_nodes)
    for ref, tid in terminals.items():
        if not reach[tid]:
            raise PdnError(f"source {ref} is not connected to any sink through {net_name}'s copper")
    floating = np.flatnonzero(~reach)
    if len(floating):
        warnings.append(f"{len(floating)} copper cells of {net_name} connect to no sink "
                        "and were left out")
    rhs = np.zeros(n)
    rhs[src_nodes] = current / len(src_nodes)
    try:
        v = _solve(n, rows, cols, vals, rhs, np.concatenate([np.array(sink_nodes), floating]))
    except (RuntimeError, ValueError) as e:
        raise PdnError(f"solve failed: {e}")
    v[floating] = np.nan

    edge = (rows != cols) & (rows < cols)
    er, ec = rows[edge], cols[edge]
    pe = np.nan_to_num(-vals[edge] * (v[er] - v[ec]) ** 2)
    power = float(pe.sum())
    # each link's I^2 R split between its two ends: W per node
    node_p = 0.5 * (np.bincount(er, pe, minlength=n) + np.bincount(ec, pe, minlength=n))

    layers_out, hotspots = {}, []
    for lay, a in ids.items():
        vm = np.full(a.shape, np.nan)
        sel = a >= 0
        vm[sel] = v[a[sel]]
        jmag = np.where(sel, np.hypot(*_face_gradient(vm, res)) / rho, np.nan)
        pm = np.full(a.shape, np.nan)
        pm[sel] = node_p[a[sel]]
        layers_out[lay] = {"v": vm, "j": jmag, "p": pm}
        if np.isfinite(jmag).any():
            idx = np.nanargmax(jmag)
            rr, cc = np.unravel_index(idx, jmag.shape)
            x, y = grid.center(rr, cc)
            hotspots.append({"layer": lay, "x": x, "y": y, "j_a_per_mm2": float(jmag[rr, cc])})
    vias = []
    for b, i, j, g in via_links:
        vias.append({"x": b.x, "y": b.y, "current_a": abs(v[i] - v[j]) * g,
                     "kind": b.KIND})
    merged = {}
    for item in vias:
        k = (round(item["x"], 4), round(item["y"], 4))
        merged[k] = max(merged.get(k, 0.0), item["current_a"])
    vias = [{"x": k[0], "y": k[1], "current_a": c} for k, c in merged.items()]
    if res > 0.25:
        warnings.append(f"resolution {res} mm is coarse; necks narrower than {2 * res} mm "
                        "are not resolved")
    return PdnResult(net=net_name, current=current, grid=grid, temp_c=temp_c,
                     drop_v=float(max(v[t] for t in src_nodes)), power_w=power,
                     terminal_v={ref: float(v[t]) for ref, t in terminals.items()},
                     layers=list(ids), maps=layers_out, hotspots=hotspots, vias=vias,
                     warnings=warnings)


def _face_gradient(vm, res):
    """Per-cell (dv/dx, dv/dy) from the faces a cell shares with copper
    neighbours: interior cells average both faces, cells on a copper edge use
    the one they have, so a one- or two-cell neck still shows its density."""
    out = []
    for axis in (1, 0):
        d = np.diff(vm, axis=axis) / res
        ok = np.isfinite(d)
        dz = np.where(ok, d, 0.0)
        s = np.zeros(vm.shape)
        c = np.zeros(vm.shape)
        lo = (slice(None), slice(None, -1)) if axis == 1 else (slice(None, -1), slice(None))
        hi = (slice(None), slice(1, None)) if axis == 1 else (slice(1, None), slice(None))
        s[lo] += dz
        c[lo] += ok
        s[hi] += dz
        c[hi] += ok
        out.append(np.where(c > 0, s / np.maximum(c, 1), 0.0))
    return out


def _reachable(n, rows, cols, start):
    try:
        import scipy.sparse as sp
        from scipy.sparse.csgraph import connected_components
        g = sp.csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n))
        _k, lab = connected_components(g, directed=False)
        return np.isin(lab, lab[list(start)])
    except ImportError:
        pass
    adj_r = np.concatenate([rows, cols])
    adj_c = np.concatenate([cols, rows])
    order = np.argsort(adj_r, kind="stable")
    adj_r, adj_c = adj_r[order], adj_c[order]
    starts = np.searchsorted(adj_r, np.arange(n + 1))
    seen = np.zeros(n, dtype=bool)
    stack = list(start)
    for s in start:
        seen[s] = True
    while stack:
        u = stack.pop()
        for w in adj_c[starts[u]:starts[u + 1]]:
            if not seen[w]:
                seen[w] = True
                stack.append(int(w))
    return seen
