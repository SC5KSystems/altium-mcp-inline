"""Which Altium-dependent commands have live evidence, and which still need a run.

Reads the DelphiScript dispatcher (every command Altium_API.pas can execute) and the
MCP server log, and reports per command: calls, successes, failures, first/last seen,
and the date the command string was last touched in git (so a success logged *before*
the last edit does not count as evidence for the code as it is now).

The log is only as long as it is. "No evidence" means this log has none, not that the
command was never run: earlier work used the dev/ harness and may predate the log.

Usage:
    python dev/validation_ledger.py [--log server/altium_mcp.log] [--json out.json]
"""
import argparse
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAS = ROOT / "server" / "AltiumScript" / "Altium_API.pas"
STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def dispatcher_commands(text):
    """Command names from the `case CommandName of` block in ExecuteCommand."""
    start = text.index("case CommandName of")
    end = text.index("else", start)
    return re.findall(r"^\s*'([a-z_0-9]+)'\s*:", text[start:end], re.M)


def scan_log(path):
    """command -> list of (timestamp, outcome) in order. Outcome is judged from the
    response that follows the request: a parsed success or a `"success": false`."""
    calls = defaultdict(list)
    cur = None
    stamp = ""
    for line in open(path, encoding="utf-8", errors="replace"):
        m = STAMP.match(line)
        if m:
            stamp = m.group(1)
        c = re.search(r"Wrote request file for command: ([a-z_0-9]+)", line)
        if c:
            cur = [stamp, c.group(1), None]
            calls[c.group(1)].append(cur)
            continue
        if cur is not None and cur[2] is None:
            if "Successfully parsed JSON response" in line:
                cur[2] = "ok"
            elif '"success": false' in line or "timed out" in line.lower():
                cur[2] = "failed"
    return calls


def last_touched(cmd):
    """ISO date of the last commit that added or removed the command string."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cI", "-S'%s'" % cmd, "--", "server/"],
                             cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.strip()
        return out[:10] or None
    except (OSError, subprocess.SubprocessError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(ROOT / "server" / "altium_mcp.log"))
    ap.add_argument("--json")
    a = ap.parse_args()
    cmds = dispatcher_commands(PAS.read_text(encoding="utf-8", errors="replace"))
    calls = scan_log(a.log)
    rows = []
    for cmd in sorted(set(cmds) | set(calls)):
        cs = calls.get(cmd, [])
        ok = [c for c in cs if c[2] == "ok"]
        bad = [c for c in cs if c[2] == "failed"]
        touched = last_touched(cmd)
        last_ok = max((c[0] for c in ok), default=None)
        if not cs:
            status = "NO EVIDENCE"
        elif not ok:
            status = "FAILED ONLY" if bad else "NO RESULT LOGGED"
        elif touched and last_ok[:10] < touched:
            status = "STALE (edited after last success)"
        else:
            status = "ok"
        rows.append({"command": cmd, "in_dispatcher": cmd in cmds, "calls": len(cs), "ok": len(ok),
                     "failed": len(bad), "first": cs[0][0] if cs else None, "last_ok": last_ok,
                     "last_edit": touched, "status": status})
    print("%-34s %5s %4s %6s  %-19s %-10s %s" % ("command", "calls", "ok", "failed", "last ok", "last edit", "status"))
    for r in rows:
        print("%-34s %5d %4d %6d  %-19s %-10s %s" % (r["command"], r["calls"], r["ok"], r["failed"],
                                                      r["last_ok"] or "-", r["last_edit"] or "-", r["status"]))
    by = defaultdict(int)
    for r in rows:
        by[r["status"]] += 1
    print("\n" + ", ".join("%s: %d" % kv for kv in sorted(by.items())))
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
