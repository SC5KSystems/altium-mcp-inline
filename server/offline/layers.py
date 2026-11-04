"""Altium layer numbers, names and the copper stack.

Binary records carry the V6 layer byte below. Text records name layers
("TOP", "MID3", "PLANE1", "MECHANICAL13", ...). Newer files add a 32-bit V7
layer id to some records; it is kept in the raw bytes and only consulted for
mechanical layers above 16.
"""

TOP, BOTTOM = 1, 32
TOP_OVERLAY, BOTTOM_OVERLAY = 33, 34
TOP_PASTE, BOTTOM_PASTE = 35, 36
TOP_SOLDER, BOTTOM_SOLDER = 37, 38
DRILL_GUIDE, KEEPOUT = 55, 56
DRILL_DRAWING, MULTI = 73, 74

NAMES = {TOP: "Top Layer", BOTTOM: "Bottom Layer"}
TOKENS = {TOP: "TOP", BOTTOM: "BOTTOM"}
for _i in range(1, 31):
    NAMES[1 + _i] = f"Mid-Layer {_i}"
    TOKENS[1 + _i] = f"MID{_i}"
for _i in range(1, 17):
    NAMES[38 + _i] = f"Internal Plane {_i}"
    TOKENS[38 + _i] = f"PLANE{_i}"
    NAMES[56 + _i] = f"Mechanical {_i}"
    TOKENS[56 + _i] = f"MECHANICAL{_i}"
NAMES.update({
    TOP_OVERLAY: "Top Overlay", BOTTOM_OVERLAY: "Bottom Overlay",
    TOP_PASTE: "Top Paste", BOTTOM_PASTE: "Bottom Paste",
    TOP_SOLDER: "Top Solder", BOTTOM_SOLDER: "Bottom Solder",
    DRILL_GUIDE: "Drill Guide", KEEPOUT: "Keep-Out Layer",
    DRILL_DRAWING: "Drill Drawing", MULTI: "Multi-Layer",
})
TOKENS.update({
    TOP_OVERLAY: "TOPOVERLAY", BOTTOM_OVERLAY: "BOTTOMOVERLAY",
    TOP_PASTE: "TOPPASTE", BOTTOM_PASTE: "BOTTOMPASTE",
    TOP_SOLDER: "TOPSOLDER", BOTTOM_SOLDER: "BOTTOMSOLDER",
    DRILL_GUIDE: "DRILLGUIDE", KEEPOUT: "KEEPOUT",
    DRILL_DRAWING: "DRILLDRAWING", MULTI: "MULTILAYER",
})
BY_TOKEN = {v: k for k, v in TOKENS.items()}

_ALIASES = {
    "top": TOP, "toplayer": TOP, "bottom": BOTTOM, "bottomlayer": BOTTOM,
    "multilayer": MULTI, "multi": MULTI, "keepout": KEEPOUT, "keepoutlayer": KEEPOUT,
    "topoverlay": TOP_OVERLAY, "topsilk": TOP_OVERLAY, "bottomoverlay": BOTTOM_OVERLAY,
    "bottomsilk": BOTTOM_OVERLAY, "toppaste": TOP_PASTE, "bottompaste": BOTTOM_PASTE,
    "topsolder": TOP_SOLDER, "bottomsolder": BOTTOM_SOLDER,
}


def is_signal(layer):
    return 1 <= layer <= 32


def is_plane(layer):
    return 39 <= layer <= 54


def is_copper(layer):
    return is_signal(layer) or is_plane(layer)


def mech(n):
    """Layer number of Mechanical n. Mechanical 17 and up live above 1000:
    the V6 byte cannot hold them (records say 72 and carry the real number in
    their V7 id)."""
    return 56 + n if n <= 16 else 1000 + n


def mech_number(layer):
    return layer - 56 if 57 <= layer <= 72 else layer - 1000 if layer > 1000 else None


def is_mechanical(layer):
    return 57 <= layer <= 72 or 1000 < layer <= 2024


def name(layer):
    if layer > 1000:
        return f"Mechanical {layer - 1000}"
    return NAMES.get(layer, f"Layer {layer}")


def parse(value):
    """Layer number from a number, a record token ("MID2") or a display name."""
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    up = s.upper().replace(" ", "").replace("-", "").replace("_", "")
    if up in BY_TOKEN:
        return BY_TOKEN[up]
    low = up.lower()
    if low in _ALIASES:
        return _ALIASES[low]
    for num, nm in NAMES.items():
        if nm.lower().replace(" ", "").replace("-", "") == low:
            return num
    for prefix in ("mechanical", "mech", "m"):
        if low.startswith(prefix) and low[len(prefix):].isdigit():
            n = int(low[len(prefix):])
            if 1 <= n <= 1024:
                return mech(n)
    raise ValueError(f"unknown layer {value!r}")


def copper_order(stack):
    """Physical copper order, top first, from a Board6 layer stack dict.

    stack maps layer number -> {"next": n, "prev": n, ...} as read from the
    LAYERnNEXT/PREV fields. Falls back to [TOP, BOTTOM].
    """
    order, seen, cur = [], set(), TOP
    while cur and cur not in seen and cur in stack:
        if is_copper(cur):
            order.append(cur)
        seen.add(cur)
        if cur == BOTTOM:
            break
        cur = stack[cur].get("next", 0)
    if not order or order[-1] != BOTTOM:
        return [TOP, BOTTOM]
    return order
