"""Find the first offline edit Altium rejects. Run on the Altium PC.

    python dev/validate_pcbwrite.py C:\\path\\to\\board.PcbDoc [--out C:\\scratch\\pcbwrite_check]

Works on a copy of the board, never the board itself. Writes one file per
edit, each holding only that edit (so a failure names its cause), in order of
increasing risk, then one file with every edit. For each it prints what to
open in Altium and what to look for. Pick a board you can lose; never one on
a shared drive.

Open the files in order (File > Open, not from a project). For each, report:
  OPENED - opened with no repair, warning or "file is corrupt" dialog
  DIALOG - any dialog appeared (copy its text)
  WRONG  - opened, but the check line does not hold
The first DIALOG or WRONG is the edit to fix; later steps still give useful
evidence. Close each file without saving.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from offline import layers as L  # noqa: E402
from offline.edits import Editor  # noqa: E402
from offline.pcbdoc import PcbDoc  # noqa: E402


def pick(doc):
    """Targets for each edit, chosen from the board itself."""
    from collections import Counter
    out = {}
    ox, oy = doc.origin
    pts = doc.outline()
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    out["box"] = (min(xs), min(ys), max(xs), max(ys))
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    out["spot"] = (min(xs) + 3, min(ys) + 3)
    # Every edit gets its own net and part, so the cumulative file can apply them all:
    # a net renamed or merged away early must not be the one a later step draws on.
    cnt = Counter(p.effective_net for p in doc.pads if p.effective_net is not None)
    multi = [n for n, k in cnt.items() if k >= 2]
    if len(multi) < 4:
        raise SystemExit("need a board with at least four nets of two or more pads")
    rename = next((n for n in multi if cnt[n] == 2), multi[0])
    rest = [n for n in multi if n != rename]
    out["rename_net"] = doc.nets[rename].name
    out["net"] = doc.nets[rest[0]].name
    out["merge"] = (doc.nets[rest[-1]].name, doc.nets[rest[-2]].name)
    npads = Counter(p.component for p in doc.pads)
    comps = sorted(doc.components, key=lambda c: npads[c.index])
    if len(comps) < 3:
        raise SystemExit("need a board with at least three components")
    small = [c for c in comps if npads[c.index] == 2]
    pool = small if len(small) >= 3 else comps
    out["move"], out["flip"], out["remove"] = pool[0].designator, pool[1].designator, pool[-1].designator
    out["polygon"] = doc.polygons[0].index if doc.polygons else None
    return out


def steps(t):
    x0, y0 = t["spot"]
    n = t["net"]
    s = [
        ("00_noop", "rewrite the container only", lambda ed: None,
         "board looks exactly like the original"),
        ("01_rename_net", f"rename net {t['rename_net']} to RENAMED_NET",
         lambda ed: ed.rename_net(t["rename_net"], "RENAMED_NET"),
         "PCB panel > Nets lists RENAMED_NET with the same pads"),
        ("02_add_track", "add a 0.25 mm track on the top layer",
         lambda ed: ed.add_track("Top Layer", x0, y0, x0 + 5, y0, 0.25, n),
         f"a track from ({x0:.2f}, {y0:.2f}) to ({x0 + 5:.2f}, {y0:.2f}) mm (absolute) on net {n}"),
        ("03_add_via", "add a via",
         lambda ed: ed.add_via(x0 + 5, y0, 0.6, 0.3, n), "a via at the track's end on the same net"),
        ("04_add_fill", "add a fill",
         lambda ed: ed.add_fill("Top Layer", x0, y0 + 2, x0 + 2, y0 + 3, 0.0, n), "a 2 x 1 mm fill"),
        ("05_add_arc", "add an arc",
         lambda ed: ed.add_arc("Top Layer", x0 + 8, y0 + 2, 1.0, 0, 180, 0.25, n),
         "a half circle of radius 1 mm"),
        ("06_add_text", "add a text (new WideStrings6 entry)",
         lambda ed: ed.add_text("OFFLINE EDIT", x0, y0 + 5, 1.0), "the words OFFLINE EDIT on top silk"),
        ("07_add_pad", "add a free pad (new unique id)",
         lambda ed: ed.add_pad(None, "T1", x0 + 12, y0, 1.5, 1.5, "Top Layer", "round", net=n),
         "a 1.5 mm round pad named T1"),
        ("08_add_region", "add a copper region (two parallel streams)",
         lambda ed: ed.add_region("Top Layer", [(x0, y0 + 7), (x0 + 3, y0 + 7), (x0 + 3, y0 + 9),
                                                 (x0, y0 + 9)], net=n), "a 3 x 2 mm copper region"),
        ("09_move_component", f"move {t['move']} by 1 mm and rotate 90",
         lambda ed: ed.move_component(t["move"], ed.doc.component(t["move"]).x + 1,
                                      ed.doc.component(t["move"]).y,
                                      ed.doc.component(t["move"]).rotation + 90),
         f"{t['move']} moved and turned, its pads and silk with it"),
        ("10_remove_tracks", "remove free top-layer tracks and arcs in a 10 mm box",
         lambda ed: ed.remove(ed.select(kinds=["track", "arc"], free=True, layer="Top Layer",
                                        box=(x0 - 3, y0 - 3, x0 + 7, y0 + 7))),
         "those tracks are gone; dimensions on the board still point at the right objects"),
        ("11_remove_component", f"remove {t['remove']} with its pads",
         lambda ed: ed.remove_component(t["remove"]),
         f"{t['remove']} is gone; Design > Update from schematic would offer to add it back"),
        ("12_flip_component", f"flip {t['flip']} to the bottom",
         lambda ed: ed.move_component(t["flip"], side="bottom"),
         f"{t['flip']} on the bottom, mirrored, pads on the bottom layer"),
    ]
    if t["merge"]:
        a, b = t["merge"]
        s.append(("13_merge_nets", f"merge net {a} into {b}",
                  lambda ed: ed.merge_nets([a], b), f"no net {a}; its pads are on {b}"))
    if t["polygon"] is not None:
        s.append(("14_unpour_polygon", f"remove polygon {t['polygon']}'s poured copper",
                  lambda ed: ed.unpour(t["polygon"]),
                  "that polygon shows unpoured; Tools > Polygon Pours > Repour All fills it again"))
    s.append(("15_rules_classes", "add a clearance rule and a net class",
              lambda ed: (ed.add_rule("Clearance", "OfflineRule", f"InNet('{n}')", "All",
                                      values={"GAP": 0.5}),
                          ed.add_net_class("OfflineClass", [n])),
              "Design > Rules shows OfflineRule; Design > Classes shows OfflineClass"))
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board")
    ap.add_argument("--out", default=None, help="folder for the step files")
    a = ap.parse_args(argv)
    src = Path(a.board).resolve()
    out = Path(a.out) if a.out else src.parent / f"{src.stem}_pcbwrite_check"
    out.mkdir(parents=True, exist_ok=True)
    work = out / f"original_copy{src.suffix}"
    shutil.copyfile(src, work)
    t = pick(PcbDoc.open(work))
    report = []
    all_steps = steps(t)
    for name, what, fn, check in all_steps:
        doc = PcbDoc.open(work)
        ed = Editor(doc)
        try:
            fn(ed)
            dest = out / f"step{name}.PcbDoc"
            ed.save(dest)
            status = "written"
        except Exception as e:  # report and carry on: one broken step must not hide the rest
            dest, status = None, f"NOT WRITTEN: {e}"
        report.append({"file": dest.name if dest else None, "edit": what, "check": check,
                       "status": status})
        print(f"{name}: {what}\n    open: {dest}\n    check: {check}\n    {status}\n")
    doc = PcbDoc.open(work)
    ed = Editor(doc)
    done = []
    for name, what, fn, check in all_steps[1:]:
        try:
            fn(ed)
            done.append(name)
        except Exception as e:
            print(f"cumulative: skipped {name}: {e}")
    dest = out / "step99_all.PcbDoc"
    ed.save(dest)
    print(f"99_all: every edit above together ({', '.join(done)})\n    open: {dest}\n")
    (out / "steps.json").write_text(json.dumps({"targets": t, "steps": report}, indent=2,
                                               default=str), encoding="utf-8")
    print(f"Steps and checks saved to {out / 'steps.json'}. Report OPENED / DIALOG / WRONG "
          "for each file, in order.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
