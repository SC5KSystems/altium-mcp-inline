"""Rasterise a net's copper so neck width and resistance can be measured.

numpy only (imported lazily by callers that need it). Polygons are filled with an
even-odd scanline fill, so a pour outline plus its cutouts can be passed together.
Row r / column c sit at y0 + (r+0.5)*res, x0 + (c+0.5)*res (y up, like the editor).
"""
import math

import numpy as np

from . import geom
from .pcbdoc import NONE


class Grid:
    def __init__(self, x0, y0, x1, y1, res):
        self.x0, self.y0, self.res = x0, y0, res
        self.W = int(math.ceil((x1 - x0) / res))
        self.H = int(math.ceil((y1 - y0) / res))

    def blank(self):
        return np.zeros((self.H, self.W), dtype=bool)

    def to_rc(self, x, y):
        return (int((y - self.y0) / self.res), int((x - self.x0) / self.res))

    def to_xy(self, r, c):
        return (self.x0 + (c + 0.5) * self.res, self.y0 + (r + 0.5) * self.res)


def fill(grid, mask, rings):
    """OR the even-odd fill of `rings` (outline plus holes) into `mask`."""
    xs1, ys1, xs2, ys2 = [], [], [], []
    for ring in rings:
        if len(ring) < 3:
            continue
        P = np.asarray(ring, dtype=float)
        Q = np.roll(P, -1, axis=0)
        xs1.append(P[:, 0]); ys1.append(P[:, 1]); xs2.append(Q[:, 0]); ys2.append(Q[:, 1])
    if not xs1:
        return
    x1, y1, x2, y2 = (np.concatenate(a) for a in (xs1, ys1, xs2, ys2))
    keep = y1 != y2
    x1, y1, x2, y2 = x1[keep], y1[keep], x2[keep], y2[keep]
    if len(x1) == 0:
        return
    ylo, yhi = np.minimum(y1, y2), np.maximum(y1, y2)
    r_lo = np.ceil((ylo - grid.y0) / grid.res - 0.5).astype(np.int64)
    r_hi = np.ceil((yhi - grid.y0) / grid.res - 0.5).astype(np.int64)   # exclusive
    r_lo = np.clip(r_lo, 0, grid.H)
    r_hi = np.clip(r_hi, 0, grid.H)
    n = np.maximum(r_hi - r_lo, 0)
    total = int(n.sum())
    if total == 0:
        return
    idx = np.repeat(np.arange(len(x1)), n)
    offs = np.arange(total) - np.repeat(np.cumsum(n) - n, n)
    rows = r_lo[idx] + offs
    yc = grid.y0 + (rows + 0.5) * grid.res
    x = x1[idx] + (yc - y1[idx]) * ((x2[idx] - x1[idx]) / (y2[idx] - y1[idx]))
    order = np.lexsort((x, rows))
    rows, x = rows[order], x[order]
    # position of each crossing within its row; even = span start, odd = span end
    first = np.r_[True, rows[1:] != rows[:-1]]
    start_of_row = np.maximum.accumulate(np.where(first, np.arange(len(rows)), 0))
    k = np.arange(len(rows)) - start_of_row
    starts = np.where((k % 2 == 0))[0]
    ends = starts + 1
    ok = ends < len(rows)
    starts, ends = starts[ok], ends[ok]
    ok = rows[starts] == rows[ends]
    starts, ends = starts[ok], ends[ok]
    c0 = np.clip(np.ceil((x[starts] - grid.x0) / grid.res - 0.5).astype(np.int64), 0, grid.W)
    c1 = np.clip(np.ceil((x[ends] - grid.x0) / grid.res - 0.5).astype(np.int64), 0, grid.W)
    diff = np.zeros((grid.H, grid.W + 1), dtype=np.int16)
    np.add.at(diff, (rows[starts], c0), 1)
    np.add.at(diff, (rows[starts], c1), -1)
    mask |= np.cumsum(diff, axis=1)[:, :grid.W] > 0


def net_copper(doc, net_idx, layer, grid, include_pads=True):
    """Boolean mask of all copper of one net on one layer (1 top, 32 bottom)."""
    mask = grid.blank()
    side = "TOP" if layer == 1 else "BOTTOM"
    for poly in doc.polygons:
        if poly.net == net_idx and poly.layer == side:
            for reg in doc.polygon_regions(poly.idx):
                if reg.kind == 0:
                    fill(grid, mask, [reg.outline] + reg.holes)
    for reg in doc.regions:
        if reg.poly == NONE and reg.kind == 0 and reg.layer == layer and reg.net == net_idx:
            fill(grid, mask, [reg.outline] + reg.holes)
    for t in doc.tracks:
        if t.layer == layer and t.net == net_idx:
            L = t.length
            ang = math.degrees(math.atan2(t.y2 - t.y1, t.x2 - t.x1))
            fill(grid, mask, [geom.stadium_ring((t.x1 + t.x2) / 2, (t.y1 + t.y2) / 2, L + t.width, t.width, ang, 6)])
    for a in doc.arcs:
        if a.layer == layer and a.net == net_idx:
            pts = geom.arc_points(a.cx, a.cy, a.r, a.sa, a.ea, 0.02)
            for p, q in zip(pts[:-1], pts[1:]):
                L = math.dist(p, q)
                ang = math.degrees(math.atan2(q[1] - p[1], q[0] - p[0]))
                fill(grid, mask, [geom.stadium_ring((p[0] + q[0]) / 2, (p[1] + q[1]) / 2, L + a.width, a.width, ang, 6)])
    if include_pads:
        for p in doc.pads:
            if p.net == net_idx and (p.layer == layer or p.layer == 74):
                fill(grid, mask, [p.ring()])
        for v in doc.vias:
            if v.net == net_idx:
                fill(grid, mask, [geom.circle_ring(v.x, v.y, v.dia / 2, 16)])
    return mask


def pad_mask(doc, pads, grid, layer):
    mask = grid.blank()
    for p in pads:
        if p.layer == layer or p.layer == 74:
            fill(grid, mask, [p.ring()])
    return mask
