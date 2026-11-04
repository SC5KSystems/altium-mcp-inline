"""Offline ECO: what Altium's Update PCB would do, without clicking through it.

    python -m offline eco preview board.PrjPcb [board.PcbDoc] [--json]
    python -m offline eco link board.PrjPcb board.PcbDoc -o linked.PcbDoc
    python -m offline eco apply board.PrjPcb board.PcbDoc --lib parts.PcbLib [--plan plan.json] [-o out.PcbDoc]
    python -m offline eco swap board.PcbDoc U1 NEW_FOOTPRINT --lib parts.PcbLib -o out.PcbDoc
    python -m offline eco gaps board.PcbDoc [--net VBAT]

The schematic side comes from sch.py (parts, pins and the netlist of every
sheet in the project). Parts are matched by designator. Nets are compared by
membership, pin by pin, so the preview tells a rename (same pins, new name)
from a merge (one schematic net holding pins of several board nets) and a
split (one board net whose pins now belong to several).

apply writes an EditPlan (editplan.py): remove dropped parts, merge and
rename nets so tracks and pours follow, move the remaining pins, swap changed
footprints and place new ones beside the board. Footprints come from the
given .PcbLib/.PcbDoc sources; a footprint it cannot find is reported, never
invented. Components on the board without a schematic link (no
SOURCEUNIQUEID: fiducials, holes placed in the PCB) are left alone.
"""

import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .sch import Netlist, Project, natural_key


class EcoError(ValueError):
    pass


# ---------------------------------------------------------------- inputs

def load_netlist(source):
    """A Netlist from a .PrjPcb, one or more .SchDoc paths, or a Netlist."""
    if isinstance(source, Netlist):
        return source, None
    if isinstance(source, (list, tuple)):
        prj = Project.from_sheets(source)
        return prj.netlist(), prj
    p = Path(source)
    if p.suffix.lower() == ".prjpcb":
        prj = Project.open(p)
        return prj.netlist(), prj
    if p.suffix.lower() == ".schdoc":
        prj = Project.from_sheets([p])
        return prj.netlist(), prj
    raise EcoError(f"{source}: give a .PrjPcb or .SchDoc")


def board_view(doc):
    """{"parts": {designator: {...}}, "pins": {(designator, pad): net or None}}."""
    comment_text = {}
    for t in doc.texts:
        if t.component is not None and t.is_comment:
            comment_text[t.component] = t.text
    parts = {}
    for c in doc.components:
        sid = c.props.get("SOURCEUNIQUEID", "") or ""
        parts[c.designator] = {"designator": c.designator, "footprint": c.pattern,
                               "source_unique_id": sid.lstrip("\\").split("\\")[-1] if sid else "",
                               "linked": bool(sid.strip("\\")), "index": c.index,
                               "comment": comment_text.get(c.index, "")}
    pins = {}
    for p in doc.pads:
        if p.component is None or not p.name:
            continue
        des = doc.components[p.component].designator
        pins[(des, p.name)] = p.net_name
    return {"parts": parts, "pins": pins}


# ---------------------------------------------------------------- preview

def preview(netlist, doc):
    """Differences between the schematic netlist and the board, as a dict."""
    b = board_view(doc)
    sp, bp = netlist.parts, b["parts"]
    add = sorted((d for d in sp if d not in bp), key=natural_key)
    remove = sorted((d for d in bp if d not in sp and bp[d]["linked"]), key=natural_key)
    board_only = sorted((d for d in bp if d not in sp and not bp[d]["linked"]), key=natural_key)
    fp_change = []
    relink = []
    for d in sorted(set(sp) & set(bp), key=natural_key):
        s, p = sp[d], bp[d]
        if s["footprint"] and s["footprint"] != p["footprint"]:
            fp_change.append({"designator": d, "from": p["footprint"], "to": s["footprint"]})
        ids = s.get("unique_ids") or [s["unique_id"]]
        if s["unique_id"] and p["source_unique_id"] not in ids:
            relink.append({"designator": d, "board": p["source_unique_id"],
                           "schematic": s["unique_id"]})
    common = set(sp) & set(bp)
    sch_pin = {k: v for k, v in netlist.pin_net().items() if k[0] in common}
    board_pin = {k: v for k, v in b["pins"].items() if k[0] in common}
    sch_pins_by_part = defaultdict(set)
    for d in common:
        for pin in sp[d]["pins"]:
            sch_pins_by_part[d].add(pin)
    unmatched_pins = sorted(((d, pin) for d in common for pin in sch_pins_by_part[d]
                             if (d, pin) not in board_pin), key=lambda k: (natural_key(k[0]),
                                                                           natural_key(k[1])))
    unmatched_pads = sorted(((d, pad) for (d, pad), net in board_pin.items()
                             if pad not in sch_pins_by_part[d] and net is not None),
                            key=lambda k: (natural_key(k[0]), natural_key(k[1])))
    keys = [k for k in board_pin if k[1] in sch_pins_by_part[k[0]]]
    to_board = defaultdict(Counter)    # schematic net -> board nets of its pins
    to_sch = defaultdict(Counter)      # board net -> schematic nets of its pins
    for k in keys:
        s, p = sch_pin.get(k), board_pin.get(k)
        if s is not None and p is not None:
            to_board[s][p] += 1
            to_sch[p][s] += 1
    merges, renames, splits = [], [], []
    for s, boards in sorted(to_board.items()):
        if len(boards) >= 2:
            merges.append({"into": s, "from": sorted(boards), "pins": dict(boards)})
    for p, schs in sorted(to_sch.items()):
        if len(schs) >= 2:
            splits.append({"net": p, "into": dict(schs)})
    merged_from = {n for m in merges for n in m["from"]}
    for s, boards in sorted(to_board.items()):
        if len(boards) == 1:
            (p,) = boards
            if p != s and len(to_sch[p]) == 1 and p not in merged_from:
                renames.append({"from": p, "to": s})
    moves = []
    final = _final_names(merges, renames, splits)
    for k in sorted(keys, key=lambda k: (natural_key(k[0]), natural_key(k[1]))):
        s, p = sch_pin.get(k), board_pin.get(k)
        after = final.get(p, p)
        if s != after:
            moves.append({"designator": k[0], "pad": k[1], "from": p, "to": s})
    sch_names = set(netlist.nets)
    board_names = {n.name for n in doc.nets}
    new_nets = sorted(sch_names - board_names - {r["to"] for r in renames} -
                      {m["into"] for m in merges})
    gone = sorted(n for n in board_names - sch_names
                  if n not in {r["from"] for r in renames} and n not in merged_from)
    comment = [{"designator": d, "from": bp[d]["comment"], "to": sp[d]["comment"]}
               for d in sorted(common, key=natural_key)
               if sp[d]["comment"] and bp[d]["comment"] and sp[d]["comment"] != bp[d]["comment"]]
    return {
        "parts": {"add": [{"designator": d, "footprint": sp[d]["footprint"],
                           "comment": sp[d]["comment"]} for d in add],
                  "remove": [{"designator": d, "footprint": bp[d]["footprint"]} for d in remove],
                  "board_only_unlinked": board_only,
                  "footprint_changes": fp_change, "comment_changes": comment,
                  "source_links_to_update": relink},
        "nets": {"renames": renames, "merges": merges, "splits": splits, "pin_moves": moves,
                 "new": new_nets, "no_longer_in_schematic": gone},
        "unmatched": {"schematic_pins_without_pad": [list(k) for k in unmatched_pins],
                      "pads_without_schematic_pin": [list(k) for k in unmatched_pads]},
        "warnings": list(netlist.warnings),
        "summary": {"add": len(add), "remove": len(remove), "footprint_changes": len(fp_change),
                    "renames": len(renames), "merges": len(merges), "splits": len(splits),
                    "pin_moves": len(moves), "new_nets": len(new_nets),
                    "unmatched_pins": len(unmatched_pins), "unmatched_pads": len(unmatched_pads),
                    "relinks": len(relink)},
    }


def _final_names(merges, renames, splits):
    """Board net -> its name after merges and renames (splits keep the name
    of their biggest share)."""
    out = {}
    for m in merges:
        for n in m["from"]:
            out[n] = m["into"]
    for r in renames:
        out[r["from"]] = r["to"]
    for s in splits:
        if s["net"] not in out:
            out[s["net"]] = max(s["into"].items(), key=lambda kv: (kv[1], kv[0]))[0]
    return out


# ---------------------------------------------------------------- link

def link(netlist, doc, editor=None):
    """Point each board component at its schematic part (by designator), so
    Altium's Update PCB matches them. Returns (editor, [designators])."""
    from .edits import Editor
    ed = editor or Editor(doc)
    done = []
    for c in doc.components:
        part = netlist.parts.get(c.designator)
        if part is None or not part["unique_id"]:
            continue
        have = (c.props.get("SOURCEUNIQUEID") or "").lstrip("\\").split("\\")[-1]
        if have in (part.get("unique_ids") or [part["unique_id"]]) and \
                c.props.get("SOURCEDESIGNATOR") == c.designator:
            continue
        want = "\\" + part["unique_id"]
        props = {"SOURCEUNIQUEID": want, "SOURCEDESIGNATOR": c.designator}
        if part.get("libref"):
            props["SOURCELIBREFERENCE"] = part["libref"]
        if c.props.get("SOURCEUNIQUEID") != want or \
                c.props.get("SOURCEDESIGNATOR") != c.designator:
            ed.set_component_props(c.designator, props)
            done.append(c.designator)
    return ed, done


# ---------------------------------------------------------------- apply

def plan(netlist, doc, sources=(), remove_unlinked=False):
    """An EditPlan that brings the board in line with the schematic, plus a
    report of what it could not do."""
    from . import footprints as F
    pv = preview(netlist, doc)
    ops, problems = [], []
    for r in pv["parts"]["remove"]:
        ops.append({"op": "remove_component", "designator": r["designator"]})
    if remove_unlinked:
        for d in pv["parts"]["board_only_unlinked"]:
            ops.append({"op": "remove_component", "designator": d})
    # merges first, so tracks and pours of every merged net follow the new name
    board_names = {n.name for n in doc.nets}
    taken = set(board_names)
    for m in pv["nets"]["merges"]:
        src = [n for n in m["from"] if n != m["into"]]
        if src:
            ops.append({"op": "merge_nets", "from": src, "into": m["into"]})
            taken.add(m["into"])
    renames = list(pv["nets"]["renames"])
    clash = [r for r in renames if r["to"] in taken and r["to"] not in
             {x["from"] for x in renames}]
    for r in clash:
        problems.append(f"rename {r['from']} -> {r['to']}: {r['to']} is still a board net; "
                        "renamed through a temporary name")
    # two-step renames avoid A->B, B->A collisions
    tmp = {}
    for i, r in enumerate(renames):
        if r["to"] in taken:
            t = f"__ECO_TMP_{i}"
            ops.append({"op": "rename_net", "from": r["from"], "to": t})
            tmp[t] = r["to"]
        else:
            ops.append({"op": "rename_net", "from": r["from"], "to": r["to"]})
            taken.discard(r["from"])
            taken.add(r["to"])
    for t, final in tmp.items():
        if final in taken:
            ops.append({"op": "merge_nets", "from": [t], "into": final})
        else:
            ops.append({"op": "rename_net", "from": t, "to": final})
            taken.add(final)
    for s in pv["nets"]["splits"]:
        problems.append(f"board net {s['net']} splits into {sorted(s['into'])}: pins are moved "
                        "one by one; check its tracks (eco gaps)")
    for mv in pv["nets"]["pin_moves"]:
        ops.append({"op": "set_pad_net", "designator": mv["designator"], "pad": mv["pad"],
                    "net": mv["to"]})
    for fc in pv["parts"]["footprint_changes"]:
        fp = F.find(fc["to"], list(sources))
        if fp is None:
            problems.append(f"{fc['designator']}: footprint {fc['to']} not found in the sources; "
                            "not swapped")
            continue
        op = {"op": "swap_footprint", "designator": fc["designator"], "footprint": fc["to"]}
        if fp.source_path:
            op["source"] = str(fp.source_path)
        ops.append(op)
    if pv["parts"]["add"]:
        pts = doc.outline()
        if pts:
            x0 = max(p[0] for p in pts) + 10.0
            y = max(p[1] for p in pts)
        else:
            x0, y = 0.0, 0.0
        pin_net = netlist.pin_net()
        for a in pv["parts"]["add"]:
            fp = F.find(a["footprint"], list(sources)) if a["footprint"] else None
            if fp is None:
                problems.append(f"{a['designator']}: footprint {a['footprint']!r} not found in "
                                "the sources; not placed")
                continue
            nets = {pin: net for (d, pin), net in pin_net.items() if d == a["designator"]}
            ext = _extent(fp)
            y -= ext[3] - ext[1] + 3.0 if ext else 5.0
            op = {"op": "place_footprint", "designator": a["designator"], "footprint": fp.name,
                  "x": round(x0 - (ext[0] if ext else 0.0), 4), "y": round(y - (ext[1] if ext else 0.0), 4),
                  "nets": nets, "comment": a["comment"] or ""}
            if fp.source_path:
                op["source"] = str(fp.source_path)
            part = netlist.parts[a["designator"]]
            if part.get("unique_id"):
                op["source_unique_id"] = "\\" + part["unique_id"]
            ops.append(op)
    for c in pv["parts"]["comment_changes"]:
        problems.append(f"{c['designator']}: comment {c['from']!r} -> {c['to']!r} not applied "
                        "(edit the comment text in Altium)")
    return {"version": 1, "libraries": [str(s) for s in sources if isinstance(s, (str, Path))],
            "ops": ops}, problems, pv


def _extent(fp):
    xs, ys = [], []
    for r in fp.records:
        if r.KIND == "pad":
            sx, sy = r.top_size
            xs += [r.x - sx / 2, r.x + sx / 2]
            ys += [r.y - sy / 2, r.y + sy / 2]
        elif r.KIND == "track":
            xs += [r.x1, r.x2]
            ys += [r.y1, r.y2]
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------- gaps

def gaps(doc, nets=None):
    """Pieces of a net that are not joined: after a part comes out its two
    sides are one net but the copper is still apart. Each gap gives the two
    nearest points and their distance."""
    from .connect import Connectivity
    c = Connectivity(doc)
    if nets:
        nets = {doc.net_index(n) if isinstance(n, str) else n for n in nets}
    out = []
    for e in c.unrouted(nets):
        out.append({"net": e["net_name"], "gap_mm": round(e["gap_mm"], 4),
                    "from": [round(v, 4) for v in e["from"][:2]],
                    "to": [round(v, 4) for v in e["to"][:2]],
                    "from_island": e["from_island"].describe(doc),
                    "to_island": e["to_island"].describe(doc)})
    out.sort(key=lambda g: (natural_key(g["net"]), g["gap_mm"]))
    return out


# ---------------------------------------------------------------- CLI

def _out(rep, as_json):
    if as_json:
        print(json.dumps(rep, indent=2, default=str))


def cmd_eco(args):
    from .pcbdoc import PcbDoc
    if args.action == "gaps":
        doc = PcbDoc.open(args.inputs[0])
        g = gaps(doc, args.net)
        if args.json:
            _out({"gaps": g}, True)
        else:
            print(f"{len(g)} gap(s)")
            for x in g:
                print(f"  {x['net']}: {x['gap_mm']} mm between {tuple(x['from'])} and "
                      f"{tuple(x['to'])}")
        return 1 if g else 0
    if args.action == "swap":
        from . import footprints as F
        from .edits import Editor
        board, designator, name = args.inputs[:3]
        doc = PcbDoc.open(board)
        fp = F.find(name, args.lib or [])
        if fp is None:
            raise EcoError(f"footprint {name!r} not found in {args.lib or 'any source (give --lib)'}")
        ed = Editor(doc)
        rep = F.swap(ed, designator, fp)
        if args.out:
            ed.save(args.out)
            rep["written"] = str(args.out)
        if args.json:
            _out(rep, True)
        else:
            print(f"{designator}: {rep['footprint']} placed; {len(rep['moved_pads'])} pad(s) moved, "
                  f"{len(rep['stranded_tracks'])} track end(s) stranded")
            for p in rep["moved_pads"]:
                print(f"  pad {p['pad']} ({p['net']}) moved {p['moved_mm']} mm")
            for t in rep["stranded_tracks"]:
                print(f"  stranded: track {t['track']} ({t['net']}) at {tuple(t['at'])}")
            if rep["pins_without_pad"]:
                print(f"  pins with no pad now: {rep['pins_without_pad']}")
        return 0
    src = args.inputs[0]
    netlist, prj = load_netlist(src)
    board = args.inputs[1] if len(args.inputs) > 1 else (prj.pcb if prj and prj.pcb else None)
    if board is None:
        raise EcoError("no board: give the .PcbDoc after the project")
    doc = PcbDoc.open(board)
    if args.action == "preview":
        rep = preview(netlist, doc)
        if args.json:
            _out(rep, True)
        else:
            s = rep["summary"]
            print(" ".join(f"{k}={v}" for k, v in s.items()))
            for a in rep["parts"]["add"]:
                print(f"  add      {a['designator']} ({a['footprint']})")
            for r in rep["parts"]["remove"]:
                print(f"  remove   {r['designator']} ({r['footprint']})")
            for f in rep["parts"]["footprint_changes"]:
                print(f"  footprint {f['designator']}: {f['from']} -> {f['to']}")
            for r in rep["nets"]["renames"]:
                print(f"  rename   {r['from']} -> {r['to']}")
            for m in rep["nets"]["merges"]:
                print(f"  merge    {', '.join(m['from'])} -> {m['into']}")
            for sp in rep["nets"]["splits"]:
                print(f"  split    {sp['net']} -> {', '.join(sp['into'])}")
            for mv in rep["nets"]["pin_moves"][:args.limit]:
                print(f"  pin      {mv['designator']}-{mv['pad']}: {mv['from']} -> {mv['to']}")
            for d in rep["parts"]["board_only_unlinked"]:
                print(f"  board only (no schematic link, left alone): {d}")
            for w in rep["warnings"]:
                print(f"  warning: {w}")
        return 0
    if args.action == "link":
        ed, done = link(netlist, doc)
        if args.out:
            ed.save(args.out)
        _out({"linked": done, "written": str(args.out) if args.out else None}, args.json)
        if not args.json:
            print(f"{len(done)} component(s) relinked" + (f"; wrote {args.out}" if args.out else ""))
        return 0
    if args.action == "apply":
        p, problems, pv = plan(netlist, doc, args.lib or [], args.remove_unlinked)
        if args.plan:
            Path(args.plan).write_text(json.dumps(p, indent=2), encoding="utf-8")
        rep = {"ops": len(p["ops"]), "problems": problems, "summary": pv["summary"]}
        if args.out:
            from .editplan import apply as apply_plan
            ed, results = apply_plan(doc, p)
            ed.save(args.out)
            rep["written"] = str(args.out)
            rep["gaps_after"] = gaps(doc)
        if args.json:
            _out(rep, True)
        else:
            print(f"{len(p['ops'])} op(s)" + (f" -> {args.plan}" if args.plan else ""))
            for pr in problems:
                print(f"  problem: {pr}")
            if args.out:
                print(f"wrote {args.out}; {len(rep['gaps_after'])} gap(s) left to route")
                for g in rep["gaps_after"][:args.limit]:
                    print(f"  gap {g['net']}: {g['gap_mm']} mm at {tuple(g['from'])}")
        return 0
    raise EcoError(f"unknown eco action {args.action}")


def register_commands(command):
    def build(p):
        p.add_argument("action", choices=("preview", "link", "apply", "swap", "gaps"))
        p.add_argument("inputs", nargs="+",
                       help="preview/link/apply: project [board]; swap: board designator "
                            "footprint; gaps: board")
        p.add_argument("--lib", action="append", help="footprint source (.PcbLib or .PcbDoc)")
        p.add_argument("--plan", help="apply: write the EditPlan here")
        p.add_argument("-o", "--out", help="write the changed board here (never the input)")
        p.add_argument("--remove-unlinked", action="store_true",
                       help="apply: also remove board parts with no schematic link")
        p.add_argument("--net", action="append", help="gaps: only these nets")
        p.add_argument("--json", action="store_true")
        p.add_argument("--limit", type=int, default=40)
    command("eco", cmd_eco, "compare, link and apply schematic changes offline", build)
