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
| WP1 script preflight + unwedge | done (offline tests); live UNVERIFIED | `bf2d5e8` |
| Reader foundation (rebuilt: phase-6 missing) | done | `83dcf11` |
| WP2 renderer | done | `00a2c0b` |
| WP3 PcbDoc writer, editor, EditPlan, live backend | done offline; Altium open + live scripts UNVERIFIED | see git log ("Add the PcbDoc writer") |

## How to run

- Preflight a body by hand: `python server/script_preflight.py body.pas [--sandbox dev/sandbox/Sandbox.pas] [--allow-unknown]`
- Recover a wedged executor: `python dev/unwedge.py [--project dev]`; state only: `python dev/unwedge.py --status`
- Fetch fixtures: `python dev/fetch_fixtures.py [--list]`
- Offline tools run from `server/`: `python -m offline <command> --help`
- Render: `python -m offline render board.PcbDoc -o out.png|svg [--layers ...] [--net VBAT] [--around U5 --margin 10 | --box x0 y0 x1 y1] [--ratsnest] [--markers drc.json] [--pdn VBAT --source J1.1 --sink U5.3 --current 30] [--diff other.PcbDoc]`
- PDN: `python -m offline pdn board.PcbDoc --net VBAT --source J1.1 --sink U5.3 --current 30 [--png heat.png]`
- Connectivity: `python -m offline connect board.PcbDoc`
- Apply an EditPlan offline: `python -m offline edit board.PcbDoc plan.json -o edited.PcbDoc [--dry-run]`
- Same plan as live scripts: `python -m offline edit board.PcbDoc plan.json --live scripts_dir --board-path "C:\...\board.PcbDoc"`
- Check what Altium accepts: `python dev/validate_pcbwrite.py board.PcbDoc --out folder`
- Second reader (Linux, KiCad installed): `python3.12 dev/kicad_read.py a.PcbDoc b.PcbDoc`
- All offline tests: `python -m unittest discover -s server/tests -p "test_[!a]*.py"`.
  The pattern leaves out `test_altium_script.py`, the pre-existing live
  integration test, which errors at setUpClass on any machine without
  `C:/AltiumMCP` and a running Altium (it did before this session too).

## Reader foundation

Rebuilt from scratch under `server/offline/` (see `server/offline/README.md`).
Measured on the KiCad fixtures:

- The compound-file reader matches olefile stream for stream on all 15 files.
  The writer round-trips all 14 compound files (PcbDoc, PcbLib, SchDoc) with
  identical streams and entry metadata, checked by both readers.
- Every parsed storage of all 7 PcbDocs re-encodes to its original bytes; a
  no-op `pcbwrite.save` gives byte-identical stream contents.
- Connectivity on the 5 routed boards: islands equal nets, 0 unrouted, 0
  shorts, 0 dead copper. That only holds once vias, planes and pours are all
  decoded right, so it is the main evidence the geometry is correct.
- All 218 fixture rules compile in the scope evaluator.
- PDN: a 28 mm x 1 mm x 35 um track gives 137.93 mV at 10 A (0.05 mm grid)
  against 137.6 mV from rho*L/(w*t), and converges to it as the grid refines.
- Speed: parse 0.01-0.11 s; connectivity 0.1-0.7 s; a whole-board plane PDN
  solve 3.8 s at 0.1 mm.

Bugs of the earlier reader that the brief lists, handled here: free fills are
copper; poured regions take their polygon's net; each shape is rasterized on
its own (even-odd over its own rings), so holes never blank other copper.

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

## WP3 notes (writer, editor, EditPlan, live backend)

- Format facts and every cross-reference the editor keeps consistent are in
  `server/offline/PCBDOC_FORMAT.md`.
- `cfb_write.py` rewrites the compound file. When the set of streams is
  unchanged it keeps the source's directory (names, CLSIDs, timestamps, tree)
  and only re-lays sectors. Writers refuse to write onto their input
  (`pcbwrite.save`, `cfb_write.write`, `Editor.save`, the `edit` CLI).
- `pcbwrite.save` re-parses what it wrote and runs `check_integrity` before
  and after; it raises (and leaves no file) when a count or reference is off.
- Edits (`edits.Editor`): select/remove primitives, add track/arc/via/fill/
  region/text/pad, add/rename/merge/remove nets, set_net, add/remove/move/
  rotate/flip components, add/remove/unpour/reshape polygons, merge two
  polygons, add net classes and rules, change rule scopes. EditPlan JSON
  (`editplan.py`) wraps the same ops; `--dry-run` applies in memory and
  reports. Ops and fields: `editplan.OPS`.
- Acceptance, measured here:
  - No-op save: every stream byte-identical on all 7 fixture PcbDocs.
  - Single edits change only the streams they must (test asserts the set).
  - KiCad 7's Altium importer (independent code) reads every no-op save
    with the same counts as the original, and sees each edit (component
    removed with its pads and owned vias, track and via added, net renamed).
    Test: `server/tests/test_offline_kicad.py` (skips without pcbnew).
  - `dev/validate_pcbwrite.py` on a copy of the Fastino fixture: all 16
    single-edit files and the cumulative file written; KiCad reads each with
    the expected change (track +1, via +1, fill +1 zone, arc +1, text +1,
    pad +1, region +1, component -1 with 2 pads, merged net -1, unpoured
    polygon -1 filled zone). Altium has not opened any of them.
- Live backend (`live.py`): one DelphiScript body per op for
  `run_altium_script`, written with a `manifest.json`. Every body passes
  the preflight against `server/SandboxScript/Sandbox.pas`. Given the saved
  board, `remove` and `set_net` bodies first count their selection and
  refuse (changing nothing) if the live board differs. `proven: false` in
  the manifest marks bodies using calls never run on a live board (arcs,
  regions, texts, component removal, polygon lookup by name, net classes).
  Ops without a safe live form (add_pad, add_component, add_net,
  remove_net, add_polygon, set_polygon, merge_polygons, add_rule) raise and
  say to apply them offline.
- `set_polygon_outline` live needs `Seg : TPolySegment;` in the var section
  of `server/SandboxScript/Sandbox.pas`. Not added: the template is shared by
  every sandbox body, and a change can't be compiled here. Add it by hand if
  you want that op live.
- Cached DRC results (`*Violation` storages) cite objects by index; when an
  edit renumbers anything they are dropped. Altium rebuilds them on the next
  DRC; the report says how many were dropped.
- Streams this code does not parse are carried through byte for byte (list:
  `PcbDoc.unparsed()`); an edit that would need to change one raises.

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
8. **Altium opens what the writer writes.** Copy any board you can lose to a
   local folder (not N:), then
   `python dev\validate_pcbwrite.py C:\scratch\copy\board.PcbDoc --out C:\scratch\pcbwrite_check`.
   Open the files it lists, in order, with File > Open (not from a
   project); for each, note OPENED (no dialog), DIALOG (paste its text) or
   WRONG (opened but the printed check line is false). Close without
   saving. Pass: every file OPENED and every check holds, including
   `step99_all`. On failure: the first DIALOG/WRONG names one edit;
   `steps.json` has the targets. Send the step name, the dialog text and,
   if possible, the file; the suspect is that edit's storage in
   `pcbwrite.py`/`edits.py` (PCBDOC_FORMAT.md says what each touches).
9. **Altium agrees after a repour and DRC.** On `step99_all`: Tools >
   Polygon Pours > Repour All, then Tools > Design Rule Check. Pass: no
   crash, the pours fill, and DRC reports only violations explained by the
   test edits (the added track/via/fill/arc/pad/region on one net, the moved
   and flipped parts, the removed part's now-unconnected nets).
10. **Live scripts.** Open a scratch copy of a board in Altium. Write a plan
    with one `add_track` on a known net and one `rename_net`, then
    `python -m offline edit board.PcbDoc plan.json --live C:\scratch\live --board-path "<the path Altium shows>"`.
    Run each `.pas` body in `manifest.json` order through
    `run_altium_script`. Pass: each returns its manifest `expect` (e.g.
    `{"created": 1}`), the track appears, the net is renamed, and
    `999_save.pas` saves. Bodies with `"proven": false` are the ones to
    watch; if one fails, paste its result and the body.

## Decisions made without asking

- Work from `origin/main` + eager-ptolemy because phase-6 is missing (as the
  brief says).
- The bridge still starts Altium when it is not running (old behaviour, kept
  deliberately); only `run_altium_script` and the recovery path refuse a dead
  Altium.
- `GetCurrentPCBBoard` in a sandbox body is an error (overridable), because
  the brief records it wedging Altium in the sandbox.
- New primitives get a fresh PrimitiveGuids entry (random GUID) and, for
  pads, a new random 8-letter unique id, as Altium writes them.
- `remove_component` removes the vias a component owns (field @7) as well;
  `keep_copper` keeps its free-standing tracks, arcs, fills and vias as free
  copper.
- Offline `merge_nets` deletes the emptied source nets; the live script
  leaves them (deleting a net live is not proven).
- `merge_polygons` keeps the polygon whose outline overlaps another net's
  polygon (keeps that pour order); when both do it keeps the first and says
  to check Polygon Pour Order.
- The sandbox template was not changed to add a `TPolySegment` variable.
