"""Neck width, ampacity and DC resistance of a net's copper (numpy).

  neck_path()  - narrowest point on the *best* route between two pad sets: the widest
                 route is found with a max-min (bottleneck) search over the local copper
                 width, so a wide detour around a constriction is credited, as it would be
                 by current.
  solve_dc()   - finite-difference Laplace solve on one layer or two layers joined by
                 vias: resistance between pad sets, plus the I^2R heat map.

Copper resistivity is 1.72e-8 ohm-m at 20 C with +0.39 %/K, the figures the Keysight
PowerAnalyzer run in this project used (58.8 MS/m, 0.00386/K at 25 C).
"""
import heapq
import math

import numpy as np

from . import raster

RHO_20C = 1.724e-8
TEMPCO = 0.00386


def rho(temp_c):
    return RHO_20C * (1.0 + TEMPCO * (temp_c - 20.0))


def edt(mask, res):
    """Distance (mm) from every copper pixel to the nearest non-copper pixel. Exact (Meijster)."""
    H, W = mask.shape
    INF = H + W + 10
    g = np.where(mask, INF, 0).astype(np.int64)
    for y in range(1, H):
        np.minimum(g[y], g[y - 1] + 1, out=g[y])
    for y in range(H - 2, -1, -1):
        np.minimum(g[y], g[y + 1] + 1, out=g[y])
    out = np.zeros((H, W), dtype=np.float64)
    rows = np.where(mask.any(axis=1))[0]
    xs = np.arange(W)
    for y in rows:
        gy = g[y]
        s = [0]
        t = [0]
        q = 0

        def f(x, i):
            return (x - i) * (x - i) + gy[i] * gy[i]

        def sep(i, u):
            return (u * u - i * i + gy[u] * gy[u] - gy[i] * gy[i]) // (2 * (u - i))

        for u in range(1, W):
            while q >= 0 and f(t[q], s[q]) > f(t[q], u):
                q -= 1
                s.pop()
                t.pop()
            if q < 0:
                q = 0
                s = [u]
                t = [0]
            else:
                w = 1 + sep(s[q], u)
                if w < W:
                    q += 1
                    s.append(u)
                    t.append(w)
        row = np.empty(W)
        for x in range(W - 1, -1, -1):
            row[x] = f(x, s[q])
            if x == t[q]:
                q -= 1
        out[y] = row
    d = np.sqrt(out) * res
    d[~mask] = 0.0
    return d


def neck_path(mask, dt, src, dst, grid):
    """Widest route from pixels `src` to pixels `dst` (boolean masks, both inside `mask`).

    Returns {'width_mm', 'at': (x, y), 'path_len_mm'} or None if not connected.
    Width is 2 x the local inscribed-circle radius at the tightest point of the best route;
    'at' is where that width is first reached going from `src` to `dst`.
    """
    H, W = mask.shape
    best = np.full((H, W), -1.0)
    pred = -np.ones((H, W), dtype=np.int64)
    heap = []
    for r, c in zip(*np.where(src & mask)):
        best[r, c] = dt[r, c]
        heapq.heappush(heap, (-dt[r, c], int(r), int(c)))
    nbrs = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]
    goal = None
    while heap:
        nb, r, c = heapq.heappop(heap)
        if -nb < best[r, c] - 1e-12:
            continue
        if dst[r, c]:
            goal = (r, c)
            break
        for dr, dc in nbrs:
            rr, cc = r + dr, c + dc
            if 0 <= rr < H and 0 <= cc < W and mask[rr, cc]:
                v = min(-nb, dt[rr, cc])
                if v > best[rr, cc]:
                    best[rr, cc] = v
                    pred[rr, cc] = r * W + c
                    heapq.heappush(heap, (-v, rr, cc))
    if goal is None:
        return None
    # walk back along the route. The reported location is where, going from src to dst, the
    # route first reaches its minimum width: a long uniform neck has no single narrowest pixel,
    # and the route can also run along other pixels at that same width past the constriction.
    r, c = goal
    path = [(r, c)]
    while pred[r, c] >= 0:
        r, c = divmod(int(pred[r, c]), W)
        path.append((r, c))
    path.reverse()
    dmin = min(dt[q] for q in path)
    at_min = [q for q in path if dt[q] <= dmin * (1 + 1e-9) + 1e-12]
    lr, lc = at_min[0]
    x, y = grid.to_xy(lr, lc)
    if best[goal] > 1e5:          # every route point lay inside the ignored terminal zones
        return {"width_mm": None, "at": None, "path_len_mm": round(len(path) * grid.res, 1)}
    return {"width_mm": round(2 * best[goal], 3), "at": (round(x, 2), round(y, 2)),
            "path_len_mm": round(len(path) * grid.res, 1)}


def ipc2221_current(width_mm, thickness_mm, dT=30.0, external=True):
    """IPC-2221 conductor current (A): I = k dT^0.44 A^0.725, A in mil^2."""
    a = (width_mm / 0.0254) * (thickness_mm / 0.0254)
    return (0.048 if external else 0.024) * dT ** 0.44 * a ** 0.725


def width_for_current(amps, thickness_mm, dT=30.0, external=True):
    """Inverse of ipc2221_current: conductor width (mm) needed for `amps`."""
    k = 0.048 if external else 0.024
    a = (amps / (k * dT ** 0.44)) ** (1 / 0.725)
    return a * 0.0254 * 0.0254 / thickness_mm


# ---- DC solve ---------------------------------------------------------------

def _select(doc, net_idx, names):
    """Pads of this net picked by 'REF.PAD' or 'REF' (every pad of REF on the net)."""
    out = []
    for n in names:
        ref, _, pad = n.partition(".")
        for p in doc.pads:
            if p.net == net_idx and doc.comp_ref(p.comp) == ref and (not pad or p.name == pad):
                out.append(p)
    return out


def solve_masks(m, g_sq, links, src_m, dst_m, amps, res, tol=1e-9, maxit=30000):
    """Conjugate-gradient solve of a sheet-resistance network.

    m: bool (L,H,W) copper per layer.  g_sq: (L,1,1) sheet conductance (S/square).
    links: (rows, cols, conductance) joining layer 0 and layer 1 (vias, plated barrels).
    src_m / dst_m: bool (L,H,W); `amps` enters at src_m (spread evenly) and leaves at dst_m.
    Returns R (ohm), mean source voltage, peak sheet current (A/mm), dissipation and hot spots.
    """
    lr, lc, lg = links
    big = 1e3 * float(g_sq.max())
    hm = m[:, :, :-1] & m[:, :, 1:]
    vm = m[:, :-1, :] & m[:, 1:, :]
    diag = np.zeros(m.shape)
    diag[:, :, :-1] += hm * g_sq
    diag[:, :, 1:] += hm * g_sq
    diag[:, :-1, :] += vm * g_sq
    diag[:, 1:, :] += vm * g_sq
    if len(lr):
        np.add.at(diag, (0, lr, lc), lg)
        np.add.at(diag, (1, lr, lc), lg)
    diag += dst_m * big
    diag[~m] = 1.0

    def apply(v):
        out = np.zeros_like(v)
        d = (v[:, :, :-1] - v[:, :, 1:]) * hm * g_sq
        out[:, :, :-1] += d
        out[:, :, 1:] -= d
        d = (v[:, :-1, :] - v[:, 1:, :]) * vm * g_sq
        out[:, :-1, :] += d
        out[:, 1:, :] -= d
        if len(lr):
            d = (v[0, lr, lc] - v[1, lr, lc]) * lg
            np.add.at(out, (0, lr, lc), d)
            np.add.at(out, (1, lr, lc), -d)
        out += dst_m * big * v
        out[~m] = v[~m]
        return out

    b = np.zeros(m.shape)
    b[src_m] = amps / src_m.sum()
    x = np.zeros(m.shape)
    r = b - apply(x)
    z = r / diag
    p_ = z.copy()
    rz = float((r * z).sum())
    bn = math.sqrt(float((b * b).sum())) or 1.0
    it = 0
    for it in range(1, maxit + 1):
        Ap = apply(p_)
        alpha = rz / float((p_ * Ap).sum())
        x += alpha * p_
        r -= alpha * Ap
        if math.sqrt(float((r * r).sum())) / bn < tol:
            break
        z = r / diag
        rz_new = float((r * z).sum())
        p_ = z + (rz_new / rz) * p_
        rz = rz_new
    v_src = float(x[src_m].mean())
    dh = (x[:, :, :-1] - x[:, :, 1:]) * hm
    dv = (x[:, :-1, :] - x[:, 1:, :]) * vm
    kh = np.abs(dh) * g_sq / res
    kv = np.abs(dv) * g_sq / res
    heat = np.zeros(m.shape)
    heat[:, :, :-1] += 0.5 * dh ** 2 * g_sq
    heat[:, :, 1:] += 0.5 * dh ** 2 * g_sq
    heat[:, :-1, :] += 0.5 * dv ** 2 * g_sq
    heat[:, 1:, :] += 0.5 * dv ** 2 * g_sq
    via_p = float(((x[0, lr, lc] - x[1, lr, lc]) ** 2 * lg).sum()) if len(lr) else 0.0
    kk = np.maximum(np.pad(kh, ((0, 0), (0, 0), (0, 1))), np.pad(kv, ((0, 0), (0, 1), (0, 0))))
    peak = np.unravel_index(int(kk.argmax()), kk.shape)
    hot = [np.unravel_index(int(f), heat.shape) for f in np.argsort(heat.ravel())[::-1][:5]]
    return {"R_ohm": v_src / amps, "v_src": v_src, "iterations": it, "power_w": float(heat.sum()) + via_p,
            "peak_a_per_mm": float(kk.max()), "peak_at": tuple(int(i) for i in peak),
            "hot": [(tuple(int(i) for i in h), float(heat[h])) for h in hot]}


def solve_net(doc, net_name, src, dst, amps, res=0.1, temp_c=25.0, plating_um=25.0, board_mm=None,
              margin=1.0, tol=1e-9, maxit=30000):
    """Resistance of one net between two pad groups, through both copper layers.

    src / dst: lists like ['J1'] or ['U1.2'] (REF = every pad of that part on the net).
    amps: current forced from src to dst (used for voltage drop, power and heat map only;
    resistance itself does not depend on it).
    """
    ni = doc.net_index(net_name)
    if ni is None:
        raise ValueError("no net named %r" % net_name)
    sp, dp = _select(doc, ni, src), _select(doc, ni, dst)
    if not sp or not dp:
        raise ValueError("source or destination selects no pads on net %s" % net_name)
    xs, ys = [], []
    for p in doc.pads:
        if p.net == ni:
            xs += [p.x - p.sx, p.x + p.sx]
            ys += [p.y - p.sy, p.y + p.sy]
    for pg in doc.polygons:
        if pg.net == ni:
            for r in doc.polygon_regions(pg.idx):
                xs += [q[0] for q in r.outline]
                ys += [q[1] for q in r.outline]
    for t in doc.tracks:
        if t.net == ni:
            xs += [t.x1, t.x2]
            ys += [t.y1, t.y2]
    grid = raster.Grid(min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin, res)
    t_cu = {l.name: l.copper_mm for l in doc.board.layers if l.copper_mm}
    layers = [1, 32]
    m = np.stack([raster.net_copper(doc, ni, lay, grid) for lay in layers])
    rs = [rho(temp_c) / (t_cu.get("Top Layer" if lay == 1 else "Bottom Layer", 0.07) * 1e-3) for lay in layers]
    g_sq = np.array([1.0 / r for r in rs])[:, None, None]
    thick = board_mm if board_mm is not None else sum((l.copper_mm or 0) + (l.dielectric_mm or 0)
                                                     for l in doc.board.layers if "Solder" not in l.name)
    links = [(v.x, v.y, v.hole) for v in doc.vias if v.net == ni]
    links += [(p.x, p.y, p.hole) for p in doc.pads if p.net == ni and p.hole > 0 and p.plated]
    lr, lc, lg = [], [], []
    for x, y, hole in links:
        r, c = grid.to_rc(x, y)
        if 0 <= r < grid.H and 0 <= c < grid.W and m[0, r, c] and m[1, r, c]:
            area = math.pi * (hole + plating_um * 1e-3) * (plating_um * 1e-3)       # mm^2 of copper
            lr.append(r)
            lc.append(c)
            lg.append(area * 1e-6 / (rho(temp_c) * thick * 1e-3))
    src_m = np.stack([raster.pad_mask(doc, sp, grid, l) & m[i] for i, l in enumerate(layers)])
    dst_m = np.stack([raster.pad_mask(doc, dp, grid, l) & m[i] for i, l in enumerate(layers)])
    if not src_m.any() or not dst_m.any():
        raise ValueError("source or destination pads are not on copper of this net")
    s = solve_masks(m, g_sq, (np.array(lr, dtype=int), np.array(lc, dtype=int), np.array(lg)),
                    src_m, dst_m, amps, res, tol, maxit)
    name = lambda li: "Top" if layers[li] == 1 else "Bottom"
    hot = []
    for (li, r, c), h in s["hot"]:
        hx, hy = grid.to_xy(r, c)
        hot.append({"layer": name(li), "x": round(hx, 2), "y": round(hy, 2),
                    "mW_per_mm2": round(h * 1e3 / (res * res), 2)})
    li, r, c = s["peak_at"]
    kx, ky = grid.to_xy(r, c)
    return {"net": net_name, "R_mohm": round(s["R_ohm"] * 1e3, 4), "drop_mV": round(s["v_src"] * 1e3, 3),
            "power_W": round(s["power_w"], 4), "peak_A_per_mm": round(s["peak_a_per_mm"], 2),
            "peak_at": (round(kx, 2), round(ky, 2), name(li)), "hot_spots": hot, "iterations": s["iterations"],
            "layers_with_copper": [name(i) for i in range(len(layers)) if m[i].any()],
            "vias_and_barrels": len(lr), "res_mm": res, "temp_C": temp_c}


def neck_between(doc, net_name, src, dst, res=0.1, margin=1.0, ignore_mm=3.0):
    """Narrowest point on the best copper route between two pad groups, per layer.

    Copper within `ignore_mm` of either pad group is not counted as a constriction: local
    width there is set by the pad sitting near a pour edge, not by the conductor between them.

    Returns {layer: {'width_mm', 'at', 'path_len_mm'}} for each layer where both groups are
    reachable, plus 'thickness_mm'. Raster resolution `res` should be at most a quarter of
    the narrowest feature you care about.
    """
    ni = doc.net_index(net_name)
    if ni is None:
        raise ValueError("no net named %r" % net_name)
    sp, dp = _select(doc, ni, src), _select(doc, ni, dst)
    if not sp or not dp:
        raise ValueError("source or destination selects no pads on net %s" % net_name)
    xs, ys = [], []
    for p in sp + dp:
        xs += [p.x - 2, p.x + 2]; ys += [p.y - 2, p.y + 2]
    for pg in doc.polygons:
        if pg.net == ni:
            for r in doc.polygon_regions(pg.idx):
                xs += [q[0] for q in r.outline]; ys += [q[1] for q in r.outline]
    for t in doc.tracks:
        if t.net == ni:
            xs += [t.x1, t.x2]; ys += [t.y1, t.y2]
    grid = raster.Grid(min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin, res)
    t_cu = {l.name: l.copper_mm for l in doc.board.layers if l.copper_mm}
    out = {}
    for lay, nm in ((1, "Top Layer"), (32, "Bottom Layer")):
        mask = raster.net_copper(doc, ni, lay, grid)
        s = raster.pad_mask(doc, sp, grid, lay) & mask
        d = raster.pad_mask(doc, dp, grid, lay) & mask
        if not s.any() or not d.any():
            continue
        dt = edt(mask, res)
        if ignore_mm > 0:
            near = edt(~(s | d), res) < ignore_mm
            dt = np.where(near, 1e6, dt)
        r = neck_path(mask, dt, s, d, grid)
        if r:
            out[nm.split()[0]] = r
    out["thickness_mm"] = t_cu.get("Top Layer", 0.07)
    return out
