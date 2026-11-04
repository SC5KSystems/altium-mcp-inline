"""Probe Altium processes that might run a batch DRC or Update PCB with no
dialog. Nothing here is known to work: these are candidates to test.

    python dev/probe_processes.py --list
    python dev/probe_processes.py --board "C:\\scratch\\copy.PcbDoc" --next
    python dev/probe_processes.py --board "C:\\scratch\\copy.PcbDoc" --run 5 [--timeout 120]
    python dev/probe_processes.py --board ... --run 8 --outjob "C:\\scratch\\drc.OutJob" --container "Report Output"
    python dev/probe_processes.py --show 5        (print a candidate's script body)

Candidates come from dev/altium_process_catalog.txt (PCB:DesignRuleCheck,
WorkspaceManager:Compile / GenerateReport / Compare / Synchronize, ...) and
from server/AltiumScript/other_utils.pas, which already runs OutJob containers
through WorkspaceManager:GenerateReport (ObjectKind=OutputBatch) and passes
DisableDialog=True to WorkspaceManager:Print. The parameter sets are guesses.

They run one per invocation, safest first, through dev/sandbox_runner.py
(preflight, wedge guard, dialog capture). Each body opens the board, counts
its violation objects, runs the process, counts again and reports; it never
saves. Every run is appended to <out>/probe_log.jsonl with what the runner
saw (dialogs included), so a local session can tell which candidates ran
without a dialog and whether they produced violations or a report.

Use a scratch copy of a board in a project, never the original; paths on
N: are refused (dev/README.md).
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT = Path("C:/Users/Public/altium_mcp/probe")

# (title, process, parameters, what to look for); safest first
CANDIDATES = [
    ("netlist status report", "PCB:ReportNetlistStatus", {},
     "read-only; expect a report document or nothing"),
    ("reset error markers", "PCB:ResetAllErrorMarkers", {},
     "clears DRC markers only"),
    ("compile the project", "WorkspaceManager:Compile", {"ObjectKind": "Project"},
     "read-only; Messages panel fills"),
    ("DRC, no parameters (baseline)", "PCB:DesignRuleCheck", {},
     "expect the DRC dialog: the baseline the others are measured against"),
    ("DRC, Action=Run", "PCB:DesignRuleCheck", {"Action": "Run"},
     "no dialog and violations_after > 0 on a board with errors would be the find"),
    ("DRC, DisableDialog=True", "PCB:DesignRuleCheck", {"DisableDialog": "True"},
     "the convention WorkspaceManager:Print accepts"),
    ("DRC, Action=Run + DisableDialog=True", "PCB:DesignRuleCheck",
     {"Action": "Run", "DisableDialog": "True"}, "both together"),
    ("OutJob container (needs an OutJob with a Design Rules Check report)",
     "WorkspaceManager:GenerateReport", {"Action": "Run", "ObjectKind": "OutputBatch"},
     "the pattern other_utils.pas runs; look for the DRC report file in the output folder"),
    ("compare project with the PCB", "WorkspaceManager:Compare", {"ObjectKind": "Project"},
     "expect the comparator dialog"),
    ("update PCB: Synchronize, ObjectKind=Project", "WorkspaceManager:Synchronize",
     {"ObjectKind": "Project"}, "changes the board in memory only (never saved); expect the "
     "ECO dialog"),
    ("update PCB: Synchronize + DisableDialog=True", "WorkspaceManager:Synchronize",
     {"ObjectKind": "Project", "DisableDialog": "True"}, "the find would be an ECO with no dialog"),
]


def q(s):
    return "'" + str(s).replace("'", "''") + "'"


def body(n, board, outjob=None, container=None):
    """The sandbox body for candidate n (1-based)."""
    title, process, params, _look = CANDIDATES[n - 1]
    params = dict(params)
    lines = [f"SandboxLog('probe {n}: {title.replace(chr(39), '')}');",
             f"Obj1 := Client.OpenDocument('PCB', {q(board)});",
             "if Obj1 = nil then",
             "    ResultText := '{\"error\": \"could not open the board\"}'",
             "else",
             "begin",
             "    Client.ShowDocument(Obj1);",
             f"    Obj2 := PCBServer.GetPCBBoardByPath({q(board)});",
             "    I1 := -1;", "    I2 := -1;",
             "    if Obj2 <> nil then",
             "    begin",
             "        I1 := 0;",
             "        Obj3 := Obj2.BoardIterator_Create;",
             "        Obj3.AddFilter_ObjectSet(MkSet(eViolationObject));",
             "        Obj3.AddFilter_LayerSet(AllLayers);",
             "        Obj3.AddFilter_Method(eProcessAll);",
             "        Obj4 := Obj3.FirstPCBObject;",
             "        while Obj4 <> nil do",
             "        begin",
             "            I1 := I1 + 1;",
             "            Obj4 := Obj3.NextPCBObject;",
             "        end;",
             "        Obj2.BoardIterator_Destroy(Obj3);",
             "    end;"]
    if process == "WorkspaceManager:GenerateReport":
        if not outjob or not container:
            raise SystemExit("candidate 8 needs --outjob and --container")
        lines += [f"    Obj5 := Client.OpenDocument('OUTPUTJOB', {q(outjob)});",
                  "    if Obj5 <> nil then Client.ShowDocument(Obj5);"]
        params["OutputMedium"] = container
    lines += ["    ResetParameters;"]
    lines += [f"    AddStringParameter({q(k)}, {q(v)});" for k, v in params.items()]
    lines += ["    SandboxLog('run process');",
              f"    RunProcess({q(process)});",
              "    SandboxLog('process returned');",
              "    if Obj2 <> nil then",
              "    begin",
              "        I2 := 0;",
              "        Obj3 := Obj2.BoardIterator_Create;",
              "        Obj3.AddFilter_ObjectSet(MkSet(eViolationObject));",
              "        Obj3.AddFilter_LayerSet(AllLayers);",
              "        Obj3.AddFilter_Method(eProcessAll);",
              "        Obj4 := Obj3.FirstPCBObject;",
              "        while Obj4 <> nil do",
              "        begin",
              "            I2 := I2 + 1;",
              "            Obj4 := Obj3.NextPCBObject;",
              "        end;",
              "        Obj2.BoardIterator_Destroy(Obj3);",
              "    end;",
              f"    ResultText := '{{\"candidate\": {n}, \"process\": \"{process}\", \"returned\": true, "
              "\"violations_before\": ' + IntToStr(I1) + ', \"violations_after\": ' + IntToStr(I2) + '}';",
              "end;"]
    return "\n".join(lines) + "\n"


def check_body(text):
    """Preflight against the dev sandbox (the one sandbox_runner injects into)."""
    sys.path.insert(0, str(REPO / "server"))
    import script_preflight as sp
    rep = sp.check(text, REPO / "dev" / "sandbox" / "Sandbox.pas")
    return rep["ok"], sp.format_report(rep)


def _log(out, entry):
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "probe_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _done(out):
    p = out / "probe_log.jsonl"
    if not p.exists():
        return set()
    seen = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            seen.add(json.loads(line)["candidate"])
        except (ValueError, KeyError):
            continue
    return seen


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--show", type=int, metavar="N")
    ap.add_argument("--run", type=int, metavar="N")
    ap.add_argument("--next", action="store_true", help="the first candidate not yet in the log")
    ap.add_argument("--board", help="a scratch copy of a board, open in a project")
    ap.add_argument("--outjob")
    ap.add_argument("--container")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--out", default=str(OUT), help=f"log folder (default {OUT})")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if args.list or not (args.show or args.run or args.next):
        done = _done(out)
        for i, (title, process, params, look) in enumerate(CANDIDATES, 1):
            mark = "x" if i in done else " "
            print(f"[{mark}] {i:2d}  {process:<34} {params or ''}  {title}")
            print(f"         look for: {look}")
        return 0
    board = args.board or "C:\\scratch\\copy.PcbDoc"
    n = args.show or args.run
    if args.next:
        done = _done(out)
        left = [i for i in range(1, len(CANDIDATES) + 1) if i not in done]
        if not left:
            print("every candidate has run; see the log")
            return 0
        n = left[0]
    if not 1 <= n <= len(CANDIDATES):
        raise SystemExit(f"no candidate {n} (1..{len(CANDIDATES)})")
    text = body(n, board, args.outjob, args.container)
    ok, report = check_body(text)
    if args.show:
        print(text)
        print("preflight:", "ok" if ok else report)
        return 0 if ok else 1
    if not args.board:
        raise SystemExit("--board is required to run a candidate")
    if re.match(r"^[Nn]:", args.board):
        raise SystemExit("refusing a board on N: (dev/README.md)")
    if not ok:
        raise SystemExit("preflight refused the body:\n" + report)
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{n:02d}_probe.pas"
    f.write_text(text, encoding="utf-8")
    print(f"candidate {n}: {CANDIDATES[n - 1][1]} {CANDIDATES[n - 1][2]}")
    p = subprocess.run([sys.executable, str(HERE / "sandbox_runner.py"), str(f), str(args.timeout)],
                       capture_output=True, text=True, errors="replace")
    result = None
    res = Path("C:/Users/Public/altium_mcp/sandbox_result.json")
    if res.exists():
        try:
            result = json.loads(res.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            result = res.read_text(encoding="utf-8", errors="replace")[:2000]
    entry = {"when": datetime.datetime.now().isoformat(timespec="seconds"), "candidate": n,
             "process": CANDIDATES[n - 1][1], "params": CANDIDATES[n - 1][2],
             "runner_exit": p.returncode, "runner_output": (p.stdout + p.stderr)[-4000:],
             "result": result}
    _log(out, entry)
    print(p.stdout[-3000:] + p.stderr[-1000:])
    print(f"logged to {out / 'probe_log.jsonl'}")
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
