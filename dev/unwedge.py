"""Clear a wedged Altium script executor, without a human in the loop.

A DelphiScript runtime error leaves the script PAUSED IN THE DEBUGGER. While
paused, every later RunScript silently does nothing, so recovery has to be
automatic or every later experiment is a false negative.

The work is done by server/altium_guard.recover(), which the MCP tool
recover_altium_script_engine also uses:

1. It checks X2.EXE first. A dead Altium is reported, never restarted (the old
   probe launched X2.EXE, which starts a new Altium when the old one has
   died); a hung one is reported and left alone.
2. It closes Altium's own error dialogs and dispatches `X2.EXE -REditScript:Stop`
   into the running instance - no window focus, no keystrokes.
3. It proves recovery by running a probe in the SAME script project that
   wedged: server/SandboxScript (what run_altium_script runs) by default, or
   dev/sandbox with --project dev (what sandbox_runner.py runs). Probing a
   different project can succeed while the wedged one stays parked.
4. Only if that fails: it opens the project's Sandbox.pas in Altium (so a
   script editor is the active document), checks the foreground frame's title
   names it, and only then sends Ctrl+F3, then probes once more. Ctrl+F3 sent
   to a PCB or schematic means something else, so it is never sent blind.

Success clears the wedge marker that makes run_altium_script refuse scripts.

Usage:
    python dev/unwedge.py                  # server sandbox (run_altium_script)
    python dev/unwedge.py --project dev    # dev sandbox (sandbox_runner.py)
    python dev/unwedge.py --status         # report Altium and wedge state only
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))
import altium_guard  # noqa: E402

PROJECTS = {
    "server": REPO / "server" / "SandboxScript" / "Sandbox.PrjScr",
    "dev": REPO / "dev" / "sandbox" / "Sandbox.PrjScr",
}
CONFIG = REPO / "server" / "config.json"


def altium_exe():
    """Resolve X2.EXE from the MCP server's config, else the newest install."""
    if CONFIG.is_file():
        try:
            p = json.loads(CONFIG.read_text()).get("altium_exe_path", "")
            if p and Path(p).is_file():
                return p
        except (ValueError, OSError):
            pass
    for base in (r"C:\Program Files\Altium", r"C:\Program Files (x86)\Altium"):
        if Path(base).is_dir():
            for d in sorted(Path(base).iterdir(), reverse=True):
                exe = d / "X2.EXE"
                if exe.is_file():
                    return str(exe)
    return ""


def unwedge(verbose=True, verify=True, project="server"):
    """True when the executor is proven to run scripts again."""
    if not verify:
        st = altium_guard.altium_state()
        if st["state"] != "running":
            if verbose:
                print(f"Altium is {st['state']}; nothing dispatched")
            return False
        altium_guard.dispatch(altium_exe(), "EditScript:Stop")
        if verbose:
            print("EditScript:Stop dispatched (not verified)")
        return True
    report = altium_guard.recover(altium_exe(), PROJECTS[project],
                                  log=print if verbose else None)
    if verbose and not report["recovered"]:
        print(report.get("error", "not recovered"))
    return report["recovered"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Clear a wedged Altium script executor")
    ap.add_argument("--project", choices=sorted(PROJECTS), default="server",
                    help="script project to probe: the one that wedged")
    ap.add_argument("--no-verify", action="store_true",
                    help="dispatch EditScript:Stop and stop there")
    ap.add_argument("--status", action="store_true",
                    help="print Altium's process state and the wedge marker, change nothing")
    a = ap.parse_args(argv)
    if a.status:
        print(json.dumps({"altium": altium_guard.altium_state(),
                          "wedge": altium_guard.read_wedge()}, indent=2))
        return 0
    return 0 if unwedge(verify=not a.no_verify, project=a.project) else 1


if __name__ == "__main__":
    sys.exit(main())
