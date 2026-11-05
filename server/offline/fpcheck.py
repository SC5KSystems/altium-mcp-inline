"""Footprint checks to run before Design > Update PCB Document.

    python -m offline sch-footprints project.PrjPcb [--board board.PcbDoc] [--lib folder ...] [--json]

For every part in a project: does it have a current PCB footprint, can the
footprint be found, and does every pin of the symbol have a pad of the same
designator. Then, against the project's board, what the update will do:
parts added and removed, footprints swapped, designators changed, matching
parts by unique ID as Altium does (eco.py matches by designator instead).

Where footprints are found, in order:
  1. the project's own .PcbLib files (what Altium searches for a footprint with no server link)
  2. the board itself: a footprint already placed there under the same name
  3. any extra .PcbLib files or folders passed in, e.g. the copies Altium leaves of released
     workspace footprints in %LOCALAPPDATA%\\Temp\\EDMS_Release\\*\\Files
A workspace (server) footprint found nowhere is reported as not checked; Altium resolves it
from the server, and the update's Validate Changes is the final word. A library that cannot
be read is listed under "unreadable_libraries", never skipped silently. Ported from the
phase-6 branch (98b87ff); footprints are read with footprints.PcbLib.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from .pcbdoc import PcbDoc
from .schnet import Project, natural_key

# COMPONENTKIND values that never reach the board
NOT_ON_BOARD = {"2": "graphical", "4": "net tie (no BOM)"}


@dataclass
class Footprint:
    name: str
    pads: list          # pad designators in file order; mechanical pads have ""
    source: str


def read_pcblib(path):
    """{footprint name: Footprint} for one .PcbLib (pad designators in file order)."""
    from .footprints import PcbLib
    lib = PcbLib.open(path)
    out = {}
    for name in lib.names():
        out[name] = Footprint(name, lib.footprint(name).pad_names(), str(path))
    return out


def board_footprints(board):
    """{footprint name: Footprint} for the footprints placed on a board (first use of each)."""
    pads = {}
    for p in board.pads:
        if p.component is not None:
            pads.setdefault(p.component, []).append(p.name)
    out = {}
    for c in board.components:
        if c.pattern and c.pattern not in out:
            out[c.pattern] = Footprint(c.pattern, pads.get(c.index, []),
                                       "%s (on %s)" % (Path(board.path).name, c.designator))
    return out


def _lib_files(paths):
    for p in paths:
        p = Path(p)
        if p.is_dir():
            yield from sorted(p.rglob("*.[Pp][Cc][Bb][Ll][Ii][Bb]"))
        elif p.is_file():
            yield p


@dataclass
class SchPart:
    designator: str
    unique_ids: list    # one per placed part of a multi-part component
    sheet: str
    kind: str           # "" standard, or a NOT_ON_BOARD label
    pins: set
    footprints: list    # (name, current, server-linked)
    libref: str


def schematic_parts(sheets):
    """{designator: SchPart} across sheets, multi-part components merged."""
    parts = {}
    for doc in sheets:
        recs = doc.records
        impls_of = {}           # component index -> implementation records
        lists = {i: int(r["OWNERINDEX"]) for i, r in enumerate(recs)
                 if r.get("RECORD") == "44" and "OWNERINDEX" in r}
        for r in recs:
            if r.get("RECORD") == "45" and r.get("MODELTYPE", "").upper() == "PCBLIB":
                comp = lists.get(int(r.get("OWNERINDEX", "-1")))
                if comp is not None:
                    impls_of.setdefault(comp, []).append(r)
        pins_of = {}
        for p in doc.pins:
            pins_of.setdefault(p.comp, set()).add(p.number)
        for idx, c in doc.components.items():
            kind = NOT_ON_BOARD.get(recs[idx].get("COMPONENTKIND", "0"), "")
            fps = [(r.get("MODELNAME", ""), r.get("ISCURRENT") == "T", bool(r.get("MODELITEMGUID")))
                   for r in impls_of.get(idx, [])]
            sp = parts.get(c.designator)
            if sp is None:
                sp = parts[c.designator] = SchPart(c.designator, [], doc.name, kind, set(), fps, c.libref)
            sp.unique_ids.append(c.unique_id)
            sp.pins |= pins_of.get(idx, set())
    return parts


def check(project_path, board_path=None, extra_libs=()):
    """Footprint findings for a project and, with a board, the update preview."""
    prj = Project(project_path)
    parts = schematic_parts(prj.load_sheets())
    if board_path is None and prj.pcb_paths:
        board_path = prj.pcb_paths[0]
    board = PcbDoc.open(board_path) if board_path and Path(board_path).is_file() else None
    unreadable = []

    local = {}
    for p in prj.footprint_lib_paths:
        if not p.is_file():
            unreadable.append({"file": str(p), "error": "listed in the project, not on disk"})
            continue
        try:
            local.update({k: v for k, v in read_pcblib(p).items() if k not in local})
        except Exception as e:     # noqa: BLE001 - reported, not skipped
            unreadable.append({"file": str(p), "error": f"{type(e).__name__}: {e}"})
    on_board = board_footprints(board) if board else {}
    extra = {}
    for p in _lib_files(extra_libs):
        try:
            extra.update({k: v for k, v in read_pcblib(p).items() if k not in extra})
        except Exception as e:     # noqa: BLE001 - a damaged or foreign file: reported
            unreadable.append({"file": str(p), "error": f"{type(e).__name__}: {e}"})

    rows, counts = [], {"error": 0, "warning": 0, "ok": 0, "not_checked": 0, "not_on_board": 0}
    for des in sorted(parts, key=natural_key):
        sp = parts[des]
        row = {"designator": des, "sheet": sp.sheet, "libref": sp.libref}
        if sp.kind:
            row.update(status="not_on_board", detail=sp.kind)
            counts["not_on_board"] += 1
            rows.append(row)
            continue
        current = [f for f in sp.footprints if f[1]] or sp.footprints[:1]
        problems, warnings = [], []
        if not sp.footprints:
            problems.append("no PCB footprint")
        elif not any(f[1] for f in sp.footprints):
            warnings.append("no footprint marked current; the update uses %s" % current[0][0])
        fp = None
        if current:
            name, _cur, server = current[0]
            row["footprint"] = name
            row["link"] = "server" if server else "library"
            if server:
                fp = on_board.get(name) or extra.get(name) or local.get(name)
            else:
                fp = local.get(name)
                if fp is None:
                    fp = on_board.get(name) or extra.get(name)
                    if fp is None:
                        problems.append("footprint %s is in none of the project's PcbLibs" % name)
                    else:
                        # Altium also searches installed libraries, which are not seen here
                        warnings.append("footprint %s is in none of the project's PcbLibs; "
                                        "checked against %s" % (name, fp.source))
            if fp is not None:
                row["checked_against"] = fp.source
                pads = {p for p in fp.pads if p}
                no_pad = sorted(sp.pins - pads, key=natural_key)
                no_pin = sorted(pads - sp.pins, key=natural_key)
                if no_pad:
                    problems.append("pins with no pad: %s" % ", ".join(no_pad))
                if no_pin:
                    warnings.append("pads with no pin (left without a net): %s" % ", ".join(no_pin))
        status = "error" if problems else "warning" if warnings else "ok" if fp is not None else "not_checked"
        counts[status] += 1
        row["status"] = status
        if problems or warnings:
            row["detail"] = "; ".join(problems + warnings)
        rows.append(row)
    result = {"project": str(project_path), "counts": counts, "parts": rows,
              "sources": {"project_libraries": [str(p) for p in prj.footprint_lib_paths],
                          "board": str(board_path) if board else None,
                          "extra_footprints": len(extra)},
              "unreadable_libraries": unreadable}
    if board:
        result["update_preview"] = update_preview(parts, board)
    return result


def update_preview(parts, board):
    """What Update PCB Document will propose, matching parts as Altium does (unique ID)."""
    by_uid = {}
    for sp in parts.values():
        if not sp.kind:
            for u in sp.unique_ids:
                by_uid[u] = sp
    matched, removed, unlinked = set(), [], []
    renamed, swapped = [], []
    for c in board.components:
        uid = c.props.get("SOURCEUNIQUEID", "").lstrip("\\")
        if not uid:
            unlinked.append({"designator": c.designator, "footprint": c.pattern})
            continue
        sp = by_uid.get(uid)
        if sp is None:
            removed.append({"designator": c.designator, "footprint": c.pattern})
            continue
        matched.add(sp.designator)
        if c.designator != sp.designator:
            renamed.append({"from": c.designator, "to": sp.designator})
        cur = [f for f in sp.footprints if f[1]] or sp.footprints[:1]
        if cur and cur[0][0] != c.pattern:
            swapped.append({"designator": sp.designator, "from": c.pattern, "to": cur[0][0]})
    added = [{"designator": d, "footprint": ([f for f in sp.footprints if f[1]] or sp.footprints[:1] or
                                             [("", 0, 0)])[0][0]}
             for d, sp in parts.items() if not sp.kind and d not in matched]
    key = lambda r: natural_key(r.get("designator") or r.get("to"))   # noqa: E731
    return {"add": sorted(added, key=key), "remove": sorted(removed, key=key),
            "footprint_change": sorted(swapped, key=key), "designator_change": sorted(renamed, key=key),
            "board_only_unlinked": sorted(unlinked, key=key),
            "unchanged": len(matched) - len(swapped)}


def format_check(res):
    """Readable report of check()."""
    c = res["counts"]
    out = ["%s: %d ok, %d errors, %d warnings, %d not checked (server footprint not found offline), "
           "%d not placed on the board (graphical)" % (Path(res["project"]).name, c["ok"], c["error"],
                                                         c["warning"], c["not_checked"], c["not_on_board"])]
    for status in ("error", "warning"):
        for r in res["parts"]:
            if r["status"] == status:
                out.append("  %-7s %-6s %-28s %s" % (status.upper(), r["designator"], r.get("footprint", ""),
                                                     r.get("detail", "")))
    unchecked = [r["designator"] for r in res["parts"] if r["status"] == "not_checked"]
    if unchecked:
        out.append("  not checked: " + ", ".join(unchecked))
    up = res.get("update_preview")
    if up:
        out.append("Update PCB preview against %s:" % Path(res["sources"]["board"]).name)
        out.append("  add %d, remove %d, footprint change %d, designator change %d, unchanged %d"
                   % (len(up["add"]), len(up["remove"]), len(up["footprint_change"]),
                      len(up["designator_change"]), up["unchanged"]))
        for k, label in (("add", "add"), ("remove", "remove")):
            if up[k]:
                out.append("  %s: %s" % (label, ", ".join("%s (%s)" % (r["designator"], r["footprint"])
                                                          for r in up[k])))
        if up["footprint_change"]:
            out.append("  footprint change: " + ", ".join("%s %s -> %s" % (r["designator"], r["from"], r["to"])
                                                         for r in up["footprint_change"]))
        if up["designator_change"]:
            out.append("  designator change: " + ", ".join("%s -> %s" % (r["from"], r["to"])
                                                          for r in up["designator_change"]))
        if up["board_only_unlinked"]:
            out.append("  on the board with no schematic link (the update offers to delete these): "
                       + ", ".join("%s (%s)" % (r["designator"], r["footprint"]) for r in up["board_only_unlinked"]))
    for u in res.get("unreadable_libraries", []):
        out.append("  UNREADABLE library %s: %s" % (u["file"], u["error"]))
    return out


def cmd_sch_footprints(args):
    res = check(args.project, args.board, args.lib or [])
    if args.json:
        print(json.dumps(res, indent=1, default=str))
    else:
        print("\n".join(format_check(res)))
    return 1 if res["counts"]["error"] or res["unreadable_libraries"] else 0


def register_commands(command):
    def build(p):
        p.add_argument("project", help=".PrjPcb")
        p.add_argument("--board", help="the board to preview against (default: the project's)")
        p.add_argument("--lib", action="append", help="extra .PcbLib file or folder (repeatable)")
        p.add_argument("--json", action="store_true")
    command("sch-footprints", cmd_sch_footprints,
            "footprint check and Update PCB preview (unique IDs) for a project", build)
