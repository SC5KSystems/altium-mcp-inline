"""Compare the offline DRC with Altium's own DRC. Run on the Altium PC.

1. In Altium open the board and run Tools > Design Rule Check > Run Design
   Rule Check (the MCP tools cannot start it: it opens a modal dialog).
2. Call the MCP tool get_drc_violations with max_violations=100000 and save
   the JSON it returns as altium_drc.json.
3. From server/: python -m offline drc C:\\path\\board.PcbDoc --json --copper saved > offline_drc.json
   (--copper saved: check the pours Altium holds, as Altium does.)
4. python dev/compare_drc.py offline_drc.json altium_drc.json [--tol 1.0]

Per rule it prints how many violations both found, only we found and only
Altium found, and lists the differences with locations. Matching, within one
rule name: first by the objects Altium's message names (pads by name, text
by string and position, tracks by their ends; positions taken either as they
are or from the board origin), then the rest by markers within --tol mm and
the same nets when both sides name them. Altium's marker is the middle of
the violation's box, ours the middle of the gap, so long objects need the
first kind. Altium reports markers in mils; the offline DRC in mm (both
absolute).
"""
import argparse
import json
import math
import re
import sys
from collections import defaultdict

MIL = 0.0254
NUM = r"(-?[\d.]+)(mm|mil)"
PT = r"\(" + NUM + r"\s*,\s*" + NUM + r"\)"


def _mm(v, unit):
    return float(v) * (MIL if unit == "mil" else 1.0)


def altium_objects(text):
    """The objects an Altium violation message names: ("pad", name),
    ("text", string, x, y), ("track", x1, y1, x2, y2), ("via", x, y)."""
    out = []
    for m in re.finditer(r"\bPad ([^\s(]+)\(", text):
        out.append(("pad", m.group(1)))
    for m in re.finditer(r'\bText "((?:[^"]|"")*)"\s*' + PT, text):
        out.append(("text", m.group(1), _mm(*m.group(2, 3)), _mm(*m.group(4, 5))))
    for m in re.finditer(r"\bTrack\s*" + PT + r"\s*" + PT, text):
        g = m.groups()
        out.append(("track", _mm(g[0], g[1]), _mm(g[2], g[3]), _mm(g[4], g[5]), _mm(g[6], g[7])))
    for m in re.finditer(r"\bVia\s*" + PT, text):
        out.append(("via", _mm(*m.group(1, 2)), _mm(*m.group(3, 4))))
    return out


def _same(a, objs, shifts, tol=0.02):
    """Whether Altium's object a is one of our described objects."""
    for o in objs:
        at = o.get("at") or []
        if a[0] == "pad" and o.get("pad") == a[1]:
            return True
        for ox, oy in shifts:
            if a[0] == "text" and o.get("kind") == "text" and o.get("text") == a[1] \
                    and len(at) == 2 and math.hypot(at[0] - a[2] - ox, at[1] - a[3] - oy) <= tol:
                return True
            if a[0] == "via" and o.get("kind") == "via" and len(at) == 2 \
                    and math.hypot(at[0] - a[1] - ox, at[1] - a[2] - oy) <= tol:
                return True
            if a[0] == "track" and o.get("kind") == "track" and len(at) == 4:
                p, q = (a[1] + ox, a[2] + oy), (a[3] + ox, a[4] + oy)
                u, v = (at[0], at[1]), (at[2], at[3])
                if (math.dist(p, u) <= tol and math.dist(q, v) <= tol) or \
                        (math.dist(p, v) <= tol and math.dist(q, u) <= tol):
                    return True
    return False


def load(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def altium_rows(data):
    rows = []
    for v in data.get("violations", []):
        x, y = v.get("x"), v.get("y")
        nets = {n for n in (v.get("primitive1_net"), v.get("primitive2_net")) if n}
        text = v.get("descriptor") or v.get("violation") or ""
        rows.append({"rule": v.get("rule") or "", "x": x * MIL if x is not None else None,
                     "y": y * MIL if y is not None else None, "nets": nets,
                     "text": text, "named": altium_objects(text)})
    return rows, data.get("total_violations"), data.get("truncated")


def offline_rows(data):
    rows = []
    for v in data.get("violations", []):
        nets = {o.get("net") for o in v.get("objects", []) if o.get("net")}
        rows.append({"rule": v.get("rule") or "", "x": v.get("x"), "y": v.get("y"), "nets": nets,
                     "text": v.get("message", ""), "approximate": v.get("approximate", False),
                     "objects": v.get("objects", [])})
    return rows


def match(ours, theirs, tol, origin=(0.0, 0.0)):
    """Within each rule name: the same named objects first, then greedy
    nearest markers."""
    pairs, used, done = [], set(), set()
    by_rule = defaultdict(list)
    for j, t in enumerate(theirs):
        by_rule[t["rule"].upper()].append(j)
    shifts = [(0.0, 0.0)] + ([tuple(origin)] if any(origin) else [])
    for i, o in enumerate(ours):
        for j in by_rule.get(o["rule"].upper(), []):
            named = theirs[j].get("named") or []
            if j not in used and named and all(_same(a, o.get("objects", []), shifts)
                                               for a in named):
                used.add(j)
                done.add(i)
                pairs.append((i, j))
                break
    for i, o in enumerate(ours):
        if i in done:
            continue
        best = None
        for j in by_rule.get(o["rule"].upper(), []):
            if j in used:
                continue
            t = theirs[j]
            if o["nets"] and t["nets"] and not (o["nets"] & t["nets"]):
                continue
            if None in (o["x"], t["x"]):
                d = 0.0 if o["x"] is None and t["x"] is None else math.inf
            else:
                d = math.hypot(o["x"] - t["x"], o["y"] - t["y"])
            if d <= tol and (best is None or d < best[0]):
                best = (d, j)
        if best is not None:
            used.add(best[1])
            pairs.append((i, best[1]))
    return pairs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("offline_json")
    ap.add_argument("altium_json")
    ap.add_argument("--tol", type=float, default=1.0, help="marker distance in mm (default 1)")
    ap.add_argument("--show", type=int, default=20, help="differences to list per side")
    a = ap.parse_args(argv)
    off = load(a.offline_json)
    ours = offline_rows(off)
    theirs, total, truncated = altium_rows(load(a.altium_json))
    if truncated:
        print(f"warning: Altium's list was truncated ({len(theirs)} of {total}); raise "
              "max_violations", file=sys.stderr)
    ours_cmp = [o for o in ours if not o["rule"].startswith("dfm:")]
    pairs = match(ours_cmp, theirs, a.tol, off.get("origin_mm") or (0.0, 0.0))
    mi, mj = {i for i, _ in pairs}, {j for _, j in pairs}
    rules = sorted({o["rule"] for o in ours_cmp} | {t["rule"] for t in theirs})
    print(f"{'rule':32s} {'both':>6s} {'offline only':>13s} {'Altium only':>12s}")
    for r in rules:
        b = sum(1 for i, _ in pairs if ours_cmp[i]["rule"] == r)
        oo = sum(1 for i, o in enumerate(ours_cmp) if o["rule"] == r and i not in mi)
        ao = sum(1 for j, t in enumerate(theirs) if t["rule"] == r and j not in mj)
        print(f"{r:32s} {b:6d} {oo:13d} {ao:12d}")
    for title, rows, seen in (("only offline", ours_cmp, mi), ("only Altium", theirs, mj)):
        rest = [r for k, r in enumerate(rows) if k not in seen]
        if rest:
            print(f"\n{title} ({len(rest)}):")
            for r in rest[:a.show]:
                at = f"({r['x']:.3f}, {r['y']:.3f})" if r["x"] is not None else ""
                print(f"  {r['rule']}: {at} {r['text'][:140]}")
    union = len(ours_cmp) + len(theirs) - len(pairs)
    agree = len(pairs) / union if union else 1.0     # both clean is agreement
    print(f"\nagreement {agree:.1%} ({len(pairs)} matched, {len(ours_cmp) - len(pairs)} offline "
          f"only, {len(theirs) - len(pairs)} Altium only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
