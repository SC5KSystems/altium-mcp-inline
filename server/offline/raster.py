"""Rasterize board copper onto a grid, per layer and per net.

Three faults of the earlier reader are handled here: free fills count as
copper; poured regions take their polygon's net (they carry none of their
own); and each shape is drawn into its own scratch mask and then OR-ed into
the layer, so one region's holes can never blank copper another object drew.

Grids are numpy arrays with row 0 at the top (largest y). A pixel is copper
when its centre lies inside a shape (exact scanline fill, numpy only).
"""

import math

import numpy as np

from . import geom
from . import layers as L


class Grid:
    def __init__(self, x0, y0, x1, y1, res):
        if res <= 0:
            raise ValueError("resolution must be positive")
        self.res = float(res)
        self.x0, self.y0 = float(x0), float(y0)
        self.nx = max(1, int(math.ceil((x1 - x0) / res)))
        self.ny = max(1, int(math.ceil((y1 - y0) / res)))
        self.x1 = self.x0 + self.nx * self.res
        self.y1 = self.y0 + self.ny * self.res
        if self.nx * self.ny > 60_000_000:
            raise ValueError(f"grid {self.nx}x{self.ny} is too large; use a coarser resolution")

    @classmethod
    def for_board(cls, doc, res, margin=1.0, box=None):
        if box is not None:
            x0, y0, x1, y1 = box
        else:
            pts = doc.outline()
            if not pts:
                xs = [p.x for p in doc.pads] + [t.x1 for t in doc.tracks] or [0, 10]
                ys = [p.y for p in doc.pads] + [t.y1 for t in doc.tracks] or [0, 10]
                pts = list(zip(xs, ys))
            x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
            y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
        return cls(x0 - margin, y0 - margin, x1 + margin, y1 + margin, res)

    @property
    def shape(self):
        return (self.ny, self.nx)

    def to_px(self, x, y):
        """World mm -> continuous pixel coords (col, row)."""
        return ((x - self.x0) / self.res, (self.y1 - y) / self.res)

    def cell(self, x, y):
        c, r = self.to_px(x, y)
        return int(math.floor(r)), int(math.floor(c))

    def center(self, row, col):
        return (self.x0 + (col + 0.5) * self.res, self.y1 - (row + 0.5) * self.res)

    def empty(self, dtype=bool):
        return np.zeros(self.shape, dtype=dtype)


def _rings(g):
    if g.is_empty:
        return
    if g.geom_type == "Polygon":
        yield g
    elif hasattr(g, "geoms"):
        for p in g.geoms:
            yield from _rings(p)


def _ring_edges(poly, grid):
    """Edge endpoints of all rings of a polygon, in pixel-centre coordinates
    (pixel (r, c) has its centre at integer (c, r))."""
    parts = []
    for ring in [poly.exterior, *poly.interiors]:
        a = np.asarray(ring.coords, dtype=float)
        if len(a) < 3:
            continue
        cx = (a[:, 0] - grid.x0) / grid.res - 0.5
        cy = (grid.y1 - a[:, 1]) / grid.res - 0.5
        parts.append(np.stack([cx[:-1], cy[:-1], cx[1:], cy[1:]], axis=1))
    return np.concatenate(parts) if parts else np.zeros((0, 4))


def _scanline(mask, grid, poly, value):
    """Exact pixel-centre, even-odd fill of one polygon (holes included)."""
    e = _ring_edges(poly, grid)
    if not len(e):
        return
    xa, ya, xb, yb = e[:, 0], e[:, 1], e[:, 2], e[:, 3]
    lo, hi = np.minimum(ya, yb), np.maximum(ya, yb)
    r_first = np.ceil(lo).astype(np.int64)
    r_last = np.ceil(hi).astype(np.int64)           # exclusive: lo <= r < hi
    r_first = np.maximum(r_first, 0)
    r_last = np.minimum(r_last, grid.ny)
    counts = np.maximum(r_last - r_first, 0)
    keep = counts > 0
    if not keep.any():
        return
    xa, ya, xb, yb, r_first, counts = xa[keep], ya[keep], xb[keep], yb[keep], r_first[keep], counts[keep]
    idx = np.repeat(np.arange(len(counts)), counts)
    rows = r_first[idx] + (np.arange(len(idx)) - np.repeat(np.cumsum(counts) - counts, counts))
    t = (rows - ya[idx]) / (yb[idx] - ya[idx])
    xs = xa[idx] + t * (xb[idx] - xa[idx])
    order = np.lexsort((xs, rows))
    rows, xs = rows[order], xs[order]
    if len(rows) % 2:
        rows, xs = rows[:-1], xs[:-1]
    r = rows[0::2]
    c0 = np.clip(np.ceil(xs[0::2]).astype(np.int64), 0, grid.nx)
    c1 = np.clip(np.ceil(xs[1::2]).astype(np.int64), 0, grid.nx)
    ok = (c1 > c0) & (rows[0::2] == rows[1::2])
    r, c0, c1 = r[ok], c0[ok], c1[ok]
    if not len(r):
        return
    rmin, rmax = int(r.min()), int(r.max()) + 1
    diff = np.zeros((rmax - rmin, grid.nx + 1), dtype=np.int32)
    np.add.at(diff, (r - rmin, c0), 1)
    np.add.at(diff, (r - rmin, c1), -1)
    filled = np.cumsum(diff, axis=1)[:, :grid.nx] > 0
    if value is True:
        mask[rmin:rmax] |= filled
    else:
        mask[rmin:rmax][filled] = value


def draw(mask, grid, g, value=True):
    """Set every pixel whose centre lies inside the geometry.

    Each polygon is filled on its own (even-odd over its own rings), so its
    holes only remove its own copper, never copper drawn by another object.
    """
    for poly in _rings(g):
        _scanline(mask, grid, poly, value)


def net_copper(doc, layer, net, grid, index=None):
    """Bool mask of one net's copper on one layer: tracks, arcs, vias, pads,
    fills, free regions, poured copper and plane areas."""
    mask = grid.empty()
    for obj, n, g in _layer_items(doc, layer, index):
        if n == net:
            draw(mask, grid, g)
    return mask


def _layer_items(doc, layer, index=None):
    if index is not None:
        return index.items.get(layer, [])
    from .connect import CopperIndex
    return CopperIndex(doc).items.get(layer, [])


def layer_nets(doc, layer, grid, index=None):
    """int32 array: net index per pixel (-1 none, -2 copper without a net)."""
    out = np.full(grid.shape, -1, dtype=np.int32)
    for obj, n, g in _layer_items(doc, layer, index):
        m = grid.empty()
        draw(m, grid, g)
        out[m] = -2 if n is None else n
    return out


def shapes_mask(grid, geoms):
    m = grid.empty()
    for g in geoms:
        draw(m, grid, g)
    return m
