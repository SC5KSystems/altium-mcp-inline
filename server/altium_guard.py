"""Keep scripts off an Altium that cannot run them.

Launching a script is harmful, not just useless, in three states:

  not running  X2.EXE -R... starts a fresh Altium, with no board open
  hung         the UI thread is not pumping messages (a long script is still
               running, or Altium is frozen); the dispatch waits behind it
  wedged       a runtime error parked the script debugger; every RunScript
               silently does nothing, and each new one queues behind it

A wedge is recorded in the marker file when a run ends without its result, and
launch_blocker() checks the process, its windows and the marker before
anything is sent. The marker names the X2.EXE processes alive when it was
written, so a restarted Altium clears it. Otherwise only recover() clears it:
it stops the debugger, then proves the executor runs by having a probe script
write a file. No X2.EXE dispatch happens unless Altium is running and
responsive, so nothing here ever starts a new Altium.

Windows calls go through ctypes (no pywin32), and every OS dependency can be
replaced, so the logic is tested on any platform.
"""

import csv
import io
import json
import os
import subprocess
import time
from pathlib import Path

EXCHANGE_DIR = Path("C:/Users/Public/altium_mcp")
WEDGE_NAME = "executor_wedged.json"
SERVER_DIR = Path(__file__).resolve().parent
SERVER_SANDBOX_PRJ = SERVER_DIR / "SandboxScript" / "Sandbox.PrjScr"
BEGIN_MARK = "// === BEGIN EXPERIMENT"
END_MARK = "// === END EXPERIMENT"
PROBE_BODY = "SandboxLog('probe');\nResultText := '{\"probe\": \"alive\"}';"

RECOVERY = ("Call recover_altium_script_engine, or run `python dev/unwedge.py` on the Altium "
            "PC. Both stop the script debugger and prove a script runs again before clearing "
            "this state. Restarting Altium also clears it.")


def is_windows():
    return os.name == "nt"


# ---------------------------------------------------------------- process

def parse_tasklist(text):
    """PIDs from `tasklist /FO CSV /NH` output for X2.EXE."""
    pids = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[0].strip().lower() == "x2.exe":
            try:
                pids.append(int(row[1].strip()))
            except ValueError:
                pass
    return pids


def altium_pids(run=subprocess.run):
    """PIDs of running X2.EXE processes, or None when they cannot be listed."""
    if not is_windows():
        return None
    try:
        r = run(["tasklist", "/FI", "IMAGENAME eq X2.EXE", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_tasklist(r.stdout or "")


def _user32():
    import ctypes
    return ctypes.windll.user32


def top_windows(pids=None):
    """[(hwnd, pid, class, title, hung)] for visible top-level windows."""
    import ctypes
    from ctypes import wintypes
    user32 = _user32()
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pids is not None and pid.value not in pids:
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        out.append((hwnd, pid.value, cls.value, buf.value, bool(user32.IsHungAppWindow(hwnd))))
        return True

    user32.EnumWindows(cb, 0)
    return out


def altium_state(pids_fn=altium_pids, windows_fn=top_windows):
    """{"state": running | not_running | hung | unknown, "pids": [...]}."""
    pids = pids_fn()
    if pids is None:
        return {"state": "unknown", "pids": []}
    if not pids:
        return {"state": "not_running", "pids": []}
    wins = windows_fn(set(pids))
    if any(w[4] for w in wins):
        return {"state": "hung", "pids": pids,
                "hung_windows": [w[3] or w[2] for w in wins if w[4]][:5]}
    return {"state": "running", "pids": pids}


# ---------------------------------------------------------------- marker

def wedge_path(exchange=EXCHANGE_DIR):
    return Path(exchange) / WEDGE_NAME


def read_wedge(exchange=EXCHANGE_DIR):
    p = wedge_path(exchange)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # unreadable marker: still a wedge, never silently ignored
        return {"reason": "unreadable wedge marker", "source": str(p), "pids": []}


def mark_wedged(reason, source, last_step=None, exchange=EXCHANGE_DIR, pids=None,
                pids_fn=altium_pids):
    if pids is None:
        pids = pids_fn() or []
    rec = {"reason": reason, "source": source, "last_step": last_step, "pids": list(pids),
           "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    p = wedge_path(exchange)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return rec


def clear_wedge(exchange=EXCHANGE_DIR):
    try:
        wedge_path(exchange).unlink()
        return True
    except FileNotFoundError:
        return False


def launch_blocker(exchange=EXCHANGE_DIR, require_running=True, state=None,
                   state_fn=altium_state):
    """None when a script may be sent; otherwise a dict saying why not.

    require_running=False keeps the bridge's old behaviour of starting Altium
    when it is not running; hung and wedged still block. Pass `state` when the
    caller already has altium_state().
    """
    st = state if state is not None else state_fn()
    if st["state"] == "not_running":
        clear_wedge(exchange)
        if require_running:
            return {"blocked": "not_running",
                    "error": "Altium is not running. Nothing was sent: launching X2.EXE now "
                             "would start a fresh Altium with no design open. Start Altium, "
                             "open the design, and retry."}
        return None
    if st["state"] == "hung":
        return {"blocked": "hung", "pids": st["pids"],
                "error": "Altium is not responding (a long script is still running, or it is "
                         "hung). Nothing was sent. Wait for it to respond; if it never does, "
                         "Windows will offer to close it.",
                "hung_windows": st.get("hung_windows", [])}
    w = read_wedge(exchange)
    if not w:
        return None
    if st["state"] == "running" and w.get("pids") and not set(w["pids"]) & set(st["pids"]):
        clear_wedge(exchange)  # Altium restarted since the wedge was recorded
        return None
    return {"blocked": "wedged",
            "error": f"Altium's script executor is wedged (recorded {w.get('time', '?')} by "
                     f"{w.get('source', '?')}: {w.get('reason', '?')}). Nothing was sent: a "
                     "script queued now waits behind the paused one, and a second failing "
                     "script makes recovery harder.",
            "last_step": w.get("last_step"), "recovery": RECOVERY}


# ---------------------------------------------------------------- recovery

def dispatch(exe, process):
    """Send `-R<process>` to the running Altium. Callers check it is running."""
    subprocess.Popen(f'"{exe}" -R{process}', shell=True)


def close_dialogs(pids=None, windows_fn=top_windows, post=None):
    """Close Altium's modal error popups (#32770 task dialogs, TMessageForm boxes).

    Only windows owned by X2.EXE are touched, never another program's dialog.
    """
    if pids is None:
        pids = altium_pids()
    if not pids:
        return 0
    hits = [w for w in windows_fn(set(pids))
            if w[2] == "#32770" or (w[2] == "TMessageForm" and
                                    w[3] in ("Error", "Warning", "Information", "Confirm"))]
    post = post or (lambda h: _user32().PostMessageW(h, 0x0010, 0, 0))
    for w in hits:
        post(w[0])
    return len(hits)


def inject_body(pas_path, body):
    """Replace the experiment body of a sandbox template; returns the old text."""
    pas_path = Path(pas_path)
    src = pas_path.read_text(encoding="utf-8")
    pre, rest = src.split(BEGIN_MARK, 1)
    marker_line, rest = rest.split("\n", 1)
    _, post = rest.split(END_MARK, 1)
    indented = "\n".join("        " + ln if ln.strip() else ln
                         for ln in body.strip("\n").splitlines())
    pas_path.write_text(pre + BEGIN_MARK + marker_line + "\n" + indented + "\n        "
                        + END_MARK + post, encoding="utf-8")
    return src


def probe(exe, project, exchange=EXCHANGE_DIR, timeout=45, launch=None, closer=None):
    """True when a trivial script in `project` runs and writes its result."""
    project = Path(project)
    pas = project.with_name("Sandbox.pas")
    result = Path(exchange) / "sandbox_result.json"
    saved = inject_body(pas, PROBE_BODY)
    try:
        try:
            result.unlink()
        except FileNotFoundError:
            pass
        (launch or dispatch)(exe, f'ScriptingSystem:RunScript(ProjectName="{project}"'
                                  f'^|ProcName="Sandbox>Run")')
        start = time.time()
        while time.time() - start < timeout:
            if result.exists():
                return True
            time.sleep(0.5)
            if closer:
                closer()
        return False
    finally:
        pas.write_text(saved, encoding="utf-8")


def focus_window(hwnd):
    """Bring hwnd to the foreground; True only if Windows actually allowed it."""
    import ctypes
    u = _user32()
    u.ShowWindow(hwnd, 9 if u.IsIconic(hwnd) else 5)
    fg = u.GetForegroundWindow()
    cur = ctypes.windll.kernel32.GetCurrentThreadId()
    tgt = u.GetWindowThreadProcessId(hwnd, None)
    fgt = u.GetWindowThreadProcessId(fg, None) if fg else 0
    for t in (fgt, tgt):
        if t and t != cur:
            u.AttachThreadInput(t, cur, True)
    try:
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    finally:
        for t in (fgt, tgt):
            if t and t != cur:
                u.AttachThreadInput(t, cur, False)
    time.sleep(0.5)
    return u.GetForegroundWindow() == hwnd


def send_ctrl_f3():
    u = _user32()
    VK_CONTROL, VK_F3, KEYUP = 0x11, 0x72, 0x0002
    u.keybd_event(VK_CONTROL, 0, 0, 0)
    u.keybd_event(VK_F3, 0, 0, 0)
    u.keybd_event(VK_F3, 0, KEYUP, 0)
    u.keybd_event(VK_CONTROL, 0, KEYUP, 0)


def stop_via_keys(exe, script_doc, pids, launch=None, windows_fn=top_windows,
                  focus=focus_window, keys=send_ctrl_f3, settle=2.0):
    """Ctrl+F3 into the script editor, only once that editor is in front.

    Ctrl+F3 reaches the debugger only when a script document is the active
    one; sent to a PCB or schematic it means something else. So the script
    document is opened (which brings it forward) and the keys are sent only if
    the foreground Altium frame's title names it.
    """
    script_doc = Path(script_doc)
    (launch or dispatch)(exe, f'WorkspaceManager:OpenObject(ObjectKind=Document'
                              f'^|FileName="{script_doc}")')
    time.sleep(settle)
    frames = [w for w in windows_fn(set(pids)) if script_doc.name.lower() in w[3].lower()]
    if not frames:
        return False, (f"no Altium frame shows {script_doc.name} after opening it; "
                       "Ctrl+F3 not sent")
    if not focus(frames[0][0]):
        return False, "Windows refused to bring the script editor forward; Ctrl+F3 not sent"
    keys()
    return True, f"Ctrl+F3 sent to '{frames[0][3]}'"


def recover(exe, project=SERVER_SANDBOX_PRJ, exchange=EXCHANGE_DIR, state_fn=altium_state,
            launch=None, prober=None, keys_fn=None, closer=None, log=print, settle=1.5):
    """Stop a parked debugger and prove the executor runs. Returns a report dict.

    `project` must be the script project that wedged: the debugger is parked in
    that project, and a probe of a different project can succeed while it
    stays parked.
    """
    project = Path(project)
    steps = []

    def note(s):
        steps.append(s)
        if log:
            log(s)

    if not exe:
        return {"recovered": False, "steps": steps, "error": "X2.EXE path unknown"}
    st = state_fn()
    if st["state"] == "not_running":
        clear_wedge(exchange)
        return {"recovered": False, "altium": "not_running", "steps": steps,
                "error": "Altium is not running; nothing to recover and nothing was started. "
                         "Start Altium and open the design."}
    if st["state"] == "hung":
        return {"recovered": False, "altium": "hung", "steps": steps,
                "error": "Altium is not responding. Recovery needs a responsive UI: wait for "
                         "it, or close Altium if Windows reports it as not responding.",
                "hung_windows": st.get("hung_windows", [])}

    if closer is None:
        closer = (lambda: close_dialogs(st["pids"])) if is_windows() else (lambda: 0)
    prober = prober or (lambda: probe(exe, project, exchange, closer=closer))
    n = closer()
    if n:
        note(f"closed {n} dialog(s)")
    (launch or dispatch)(exe, "EditScript:Stop")
    note("EditScript:Stop dispatched")
    time.sleep(settle)
    closer()
    if prober():
        clear_wedge(exchange)
        note("probe ran: executor alive")
        return {"recovered": True, "steps": steps, "method": "EditScript:Stop"}

    note("probe did not run; trying Ctrl+F3 in the script editor")
    keys_fn = keys_fn or (lambda: stop_via_keys(exe, project.with_name("Sandbox.pas"),
                                                st["pids"], launch=launch))
    ok, info = keys_fn()
    note(info)
    closer()
    if ok and prober():
        clear_wedge(exchange)
        note("probe ran: executor alive")
        return {"recovered": True, "steps": steps, "method": "Ctrl+F3"}
    mark_wedged("recovery failed", "recover", exchange=exchange, pids=st["pids"])
    return {"recovered": False, "steps": steps,
            "error": "Still wedged. Save your work in Altium if you can, then restart it "
                     "(dev/sandbox_runner.py --auto-restart does this, losing unsaved work)."}
