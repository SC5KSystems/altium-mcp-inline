"""The narrowest point of a net's copper between two groups of pads.

    python -m offline neck board.PcbDoc --net VBAT --src J1 --dst Q1.D [--res 0.1]
        [--ignore 3.0] [--json]

Per copper layer, the widest route from the source pads to the sink pads is
found with a bottleneck (max-min) search over the local copper width (twice
the distance to the nearest copper edge), so a wide detour around a
constriction is credited as current would credit it. The route's narrowest
point is the neck; its width is good to about one raster step. Copper within
`ignore` mm of the pads is not counted as a neck (there the pad's own edge
sets the width). Each neck carries the IPC-2221 current of that width for a
10 and a 20 C rise (external layer). Layers are judged one at a time: a neck
bridged by vias to another layer still shows; offline_pcb_pdn solves the
whole net. Ported from the phase-6 branch (98b87ff) onto this reader.
"""

import heapq
import json

from . import geom
from . import layers as L


def _terminal_pads(doc, net, refs):
    from .pdn import parse_terminal
    out = []
    for ref in refs:
        t = parse_terminal(doc, ref)
        if t["kind"] != "pads":
            raise ValueError(f"{ref}: give pads (U1 or U1.3), not a point")
        hit = [p for p in t["pads"] if p.effective_net == net]
        if not hit:
            raise ValueError(f"{ref} has no pad on net {doc.nets[net].name}")
        out += hit
    return out


def bottleneck(mask, dt, src, dst):
    """Widest route from src pixels to dst pixels inside mask: (min dt along
    it, the route as [(row, col)]) or None when they are not connected."""
    import numpy as np
    H, W = mask.shape
    best = np.full((H, W), -1.0)
    pred = np.full((H, W), -1, dtype=np.int64)
    heap = []
    for r, c in zip(*np.where(src & mask)):
        best[r, c] = dt[r, c]
        heapq.heappush(heap, (-dt[r, c], int(r), int(c)))
    nbrs = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))
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
    r, c = goal
    path = [(r, c)]
    while pred[r, c] >= 0:
        r, c = divmod(int(pred[r, c]), W)
        path.append((r, c))
    path.reverse()
    return best[goal], path


def neck_between(doc, net, src, dst, res=0.1, ignore_mm=3.0, margin=1.0):
    """{layer name: {width_mm, at, path_len_mm, ipc2221 currents}} for each
    copper layer on which both pad groups have copper and are joined."""
    import numpy as np
    from scipy import ndimage
    from .connect import CopperIndex
    from .power import ipc2221_current
    from .raster import Grid, draw, net_copper
    if isinstance(net, str):
        if not doc.has_net(net):
            raise ValueError(f"no net {net!r} on this board")
        net = doc.net_index(net)
    sp, dp = _terminal_pads(doc, net, src), _terminal_pads(doc, net, dst)
    index = CopperIndex(doc)
    xs, ys = [], []
    for lay, items in index.items.items():
        for obj, n, g in items:
            if n == net:
                x0, y0, x1, y1 = g.bounds
                xs += [x0, x1]
                ys += [y0, y1]
    if not xs:
        raise ValueError(f"net {doc.nets[net].name} has no copper")
    grid = Grid(min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin, res)
    out = {}
    for lay in doc.copper_layers:
        mask = net_copper(doc, lay, net, grid, index)
        s, d = grid.empty(), grid.empty()
        for pads, m in ((sp, s), (dp, d)):
            for p in pads:
                if p.on_layer(lay):
                    g = geom.pad_shape(p, lay)
                    if not g.is_empty:
                        draw(m, grid, g)
        s &= mask
        d &= mask
        if not s.any() or not d.any():
            continue
        dt = ndimage.distance_transform_edt(mask) * res
        if ignore_mm > 0:
            near = ndimage.distance_transform_edt(~(s | d)) * res < ignore_mm
            dt = np.where(near & mask, 1e6, dt)
        found = bottleneck(mask, dt, s, d)
        name = L.name(lay)
        if found is None:
            out[name] = {"width_mm": None, "joined": False}
            continue
        low, path = found
        low = float(low)
        length = round(len(path) * res, 1)
        if low > 1e5:
            out[name] = {"width_mm": None, "joined": True, "path_len_mm": length,
                         "note": "every route point lies within ignore_mm of the pads"}
            continue
        at = next(q for q in path if dt[q] <= low * (1 + 1e-9) + 1e-12)
        x, y = grid.center(*at)
        w = 2 * low
        t = doc.copper_thickness(lay)
        out[name] = {"width_mm": round(w, 3), "joined": True, "at": [round(x, 2), round(y, 2)],
                     "path_len_mm": length, "copper_mm": round(t, 4),
                     "ipc2221_a_rise10": round(ipc2221_current(w * t, 10.0, external=True), 1),
                     "ipc2221_a_rise20": round(ipc2221_current(w * t, 20.0, external=True), 1)}
    return out


def cmd_neck(args):
    from .pcbdoc import PcbDoc
    doc = PcbDoc.open(args.board)
    rep = neck_between(doc, args.net, args.src, args.dst, args.res, args.ignore)
    if args.json:
        print(json.dumps(rep, indent=1))
        return 0
    if not rep:
        print("the pad groups share no copper layer")
    for lay, r in rep.items():
        if r.get("width_mm") is None:
            print(f"  {lay}: {'no neck outside the pad zones' if r['joined'] else 'not joined'}")
        else:
            print(f"  {lay}: {r['width_mm']} mm at {tuple(r['at'])}, route {r['path_len_mm']} mm; "
                  f"IPC-2221 {r['ipc2221_a_rise10']} A at +10 C, {r['ipc2221_a_rise20']} A at +20 C")
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("board")
        p.add_argument("--net", required=True)
        p.add_argument("--src", nargs="+", required=True, help="J1 (every pad on the net) or J1.1")
        p.add_argument("--dst", nargs="+", required=True)
        p.add_argument("--res", type=float, default=0.1, help="raster step, mm")
        p.add_argument("--ignore", type=float, default=3.0, help="mm around the pads not judged")
        p.add_argument("--json", action="store_true")
    command("neck", cmd_neck, "narrowest copper between two pad groups on one net", build)
