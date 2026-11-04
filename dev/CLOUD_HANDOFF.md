# Cloud session handoff: offline PCB tools

Written by an unattended cloud session with no Altium. Everything that needs
Altium is listed under **UNVERIFIED** with the exact local check. "Tests pass"
below means the offline unit tests in this repo pass on Linux; it never means
Altium accepted anything.

Branch: `claude/gracious-rubin-f1hbv2`.

## Setup facts

- `origin/phase-6-pdm-review-tools` does **not exist on the remote** (checked
  2026-11-04 with `git ls-remote`). Per the brief, work started from
  `origin/main` (`e29e0ac`) with `origin/claude/eager-ptolemy-4w4c4m` merged in
  (fast-forward to `09efd44`). The offline reader that branch was meant to
  carry (`server/offline/*`, `server/offline_tools.py`,
  `server/tests/test_offline.py`, `dev/VALIDATION.md`) was therefore rebuilt
  here from scratch. If the phase-6 branch exists locally, expect overlapping
  module names (`pcbdoc.py`, `cfb.py`, `raster.py`, ...) with different
  internals; merge by keeping one reader, not both.
- Fixtures: `python dev/fetch_fixtures.py` downloads KiCad's public Altium
  importer test files (GitLab `kicad/code/kicad`, pinned commit
  `bbe344f935a87778920165cb8e019224dbd476f2`) to a cache outside the repo
  (`%LOCALAPPDATA%\altium-mcp\fixtures`, `~/.cache/altium-mcp/fixtures`, or
  `$ALTIUM_FIXTURE_CACHE`). 9 PcbDocs, 3 PcbLibs, 3 SchDocs, 2 PrjPcbs.
- Network: GitLab, raw.githubusercontent.com and `git clone` from GitHub work;
  `api.github.com` returns 403 through the session proxy.

## Packages

| Package | State | Commit |
|---|---|---|
| WP1 script preflight + unwedge | done (offline tests); live UNVERIFIED | see git log |

## How to run

- Preflight a body by hand: `python server/script_preflight.py body.pas [--sandbox dev/sandbox/Sandbox.pas] [--allow-unknown]`
- Recover a wedged executor: `python dev/unwedge.py [--project dev]`; state only: `python dev/unwedge.py --status`
- Fetch fixtures: `python dev/fetch_fixtures.py [--list]`
- WP1 tests: `python -m unittest discover -s server/tests -p "test_script_preflight.py"` and `-p "test_altium_guard.py"`

## WP1 notes

- `run_altium_script` now runs a static preflight (`server/script_preflight.py`)
  before injecting anything. Allow-lists come from the target sandbox's own
  declarations, every `.pas` file in the repo (names used after a dot become
  members; bare names the repo uses without declaring become API globals; names
  the repo declares as locals are *not* allowed in a body), and a seed list of
  calls proven on a live board. New parameter `allow_unknown`.
- `server/altium_guard.py` checks X2.EXE before any launch (tasklist + the
  `IsHungAppWindow` state of X2.EXE's own windows) and keeps a wedge marker at
  `C:\Users\Public\altium_mcp\executor_wedged.json`. While it exists,
  `run_altium_script` and every bridge tool return at once without sending
  anything. A bridge timeout also writes the marker. Recovery (new MCP tool
  `recover_altium_script_engine`, or `dev/unwedge.py`) clears it only after a
  probe script runs; an Altium restart (new PID) clears it too.
- `dev/unwedge.py` fixes: probes `server/SandboxScript` by default (`--project
  dev` for the runner's sandbox); Ctrl+F3 is sent only after
  `WorkspaceManager:OpenObject` brings `Sandbox.pas` forward and the
  foreground frame's title names it; never dispatches to a dead or hung
  Altium; reads `server/config.json` (it used to read a file nothing writes).
- Dialog closing now only touches X2.EXE's own windows. The old code closed
  every `#32770` dialog on the desktop, from any program.
- Both sandbox templates had leftover experiment bodies naming a personal
  user path and a project; they now hold a placeholder. The old text is still
  in git history.

## UNVERIFIED (needs Altium)

Run these on the Altium PC, in order. Each says what passing looks like.

1. **Sandbox still compiles after the template change.** Restart the MCP
   server, then call `run_altium_script` with
   `SandboxLog('hello'); ResultText := '{"ok": true}';`.
   Pass: `success: true`, `result` is `{"ok": true}`. Fail: "script never
   started" means the template no longer compiles; `git diff
   server/SandboxScript/Sandbox.pas` shows what changed.
2. **Preflight refuses without launching.** `run_altium_script` with
   `Result := 'x';`. Pass: returns at once with `preflight rejected the
   script`, and Altium shows no activity.
3. **Process check.** Close Altium, call `run_altium_script` with the body
   from step 1. Pass: `blocked: not_running` and Altium does **not** start.
   Fail: Altium starts; then `tasklist /FI "IMAGENAME eq X2.EXE" /FO CSV
   /NH` output is not what `parse_tasklist` expects; paste it into the report.
4. **Wedge, refuse, recover.** Start Altium, open any board. Call
   `run_altium_script` with `SandboxLog('before'); Obj1 := nil;
   S1 := Obj1.Name; ResultText := 'unreachable';`. Pass: `executor_wedged:
   true`, `last_step_reached: before`. Then call it again with the step-1 body.
   Pass: returns in under a second with `blocked: wedged`. Then call
   `recover_altium_script_engine`. Pass: `recovered: true`, method
   `EditScript:Stop`; the step-1 body then succeeds. If method is `Ctrl+F3`,
   the Stop dispatch alone did not work; report that. If `recovered: false`,
   paste its `steps`.
5. **Ctrl+F3 fallback safety.** Only if step 4 needed it: confirm the keys
   went to a window titled with `Sandbox.pas`, never a PCB.
6. **Hung detection.** Optional: run a body with a long loop
   (`for I1 := 0 to 200000000 do I2 := I1;`), and while it runs call
   `python dev/unwedge.py --status`. Pass: `state: hung` while it runs.
7. Assumption to confirm: a parked `Altium_API` (bridge) script blocks the
   sandbox project as well, so a sandbox probe proves the whole engine runs.
   If a bridge tool times out, `recover_altium_script_engine` reports
   recovered, and the next bridge tool still times out, this is false.

## Decisions made without asking

- Work from `origin/main` + eager-ptolemy because phase-6 is missing (as the
  brief says).
- The bridge still starts Altium when it is not running (old behaviour, kept
  deliberately); only `run_altium_script` and the recovery path refuse a dead
  Altium.
- `GetCurrentPCBBoard` in a sandbox body is an error (overridable), because
  the brief records it wedging Altium in the sandbox.
