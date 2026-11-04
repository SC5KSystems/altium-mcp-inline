"""Compare the offline DRC with Altium's own DRC. Run on the Altium PC.

1. In Altium open the board and run Tools > Design Rule Check > Run Design
   Rule Check (the MCP tools cannot start it: it opens a modal dialog).
2. Call the MCP tool get_drc_violations with max_violations=100000 and save
   the JSON it returns as altium_drc.json.
3. From server/: python -m offline drc C:\\path\\board.PcbDoc --json --copper saved > offline_drc.json
   (--copper saved: check the pours Altium holds, as Altium does.)
4. python dev/compare_drc.py offline_drc.json altium_drc.json [--tol 1.0]

Per rule it prints how many violations both found, only we found and only
Altium found, and lists the differences with locations. Matching: same rule
name, markers within --tol mm, and the same nets when both sides name them.
Altium reports coordinates in mils; the offline DRC in mm (both absolute).
"""
import argparse
import json
import math
import sys
from collections import defaultdict

MIL = 0.0254


def load(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def altium_rows(data):
    rows = []
    for v in data.get("violations", []):
        x, y = v.get("x"), v.get("y")
        nets = {n for n in (v.get("primitive1_net"), v.get("primitive2_net")) if n}
        rows.append({"rule": v.get("rule") or "", "x": x * MIL if x is not None else None,
                     "y": y * MIL if y is not None else None, "nets": nets,
                     "text": v.get("descriptor") or v.get("violation") or ""})
    return rows, data.get("total_violations"), data.get("truncated")


def offline_rows(data):
    rows = []
    for v in data.get("violations", []):
        nets = {o.get("net") for o in v.get("objects", []) if o.get("net")}
        rows.append({"rule": v.get("rule") or "", "x": v.get("x"), "y": v.get("y"), "nets": nets,
                     "text": v.get("message", ""), "approximate": v.get("approximate", False)})
    return rows


def match(ours, theirs, tol):
    """Greedy nearest matching within each rule name."""
    pairs, used = [], set()
    by_rule = defaultdict(list)
    for j, t in enumerate(theirs):
        by_rule[t["rule"].upper()].append(j)
    for i, o in enumerate(ours):
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
    ours = offline_rows(load(a.offline_json))
    theirs, total, truncated = altium_rows(load(a.altium_json))
    if truncated:
        print(f"warning: Altium's list was truncated ({len(theirs)} of {total}); raise "
              "max_violations", file=sys.stderr)
    ours_cmp = [o for o in ours if not o["rule"].startswith("dfm:")]
    pairs = match(ours_cmp, theirs, a.tol)
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
    agree = len(pairs) / max(1, len(ours_cmp) + len(theirs) - len(pairs))
    print(f"\nagreement {agree:.1%} ({len(pairs)} matched, {len(ours_cmp) - len(pairs)} offline "
          f"only, {len(theirs) - len(pairs)} Altium only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
