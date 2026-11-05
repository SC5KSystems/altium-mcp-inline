"""One-command acceptance run for the offline PCB tools. Run on the Altium PC.

    server\\.venv\\Scripts\\python.exe dev\\live_validate_tools.py C:\\boards\\x.PcbDoc
        [--work C:\\scratch\\offline_check] [--only STEP ...] [--from STEP] [--list]
        [--no-ask] [--no-live] [--wedge] [--project X.PrjPcb] [--gerbers FOLDER]
        [--power NET:AMPS:SRC>SINK]

It copies the named board into a new run folder under --work and works on
the copy only: the named board is never written (its hash is checked at the
end) and nothing is written on the N: drive. The steps run in order, safest
first: offline checks, then a script that is refused before Altium sees it,
then read-only scripts, then files for you to open, and last the scripts that
edit and save the copy. Steps that need your eyes ask a y/n question; with
--no-ask (or no console) they are listed as MANUAL with what to do. Use the
server's Python (it has pywin32 and mcp); with any other Python, or with
--no-live, the steps that talk to Altium are skipped.

Each step prints PASS, FAIL, SKIP or MANUAL and a reason; everything is also
saved as results.json in the run folder. The summary at the end holds only
step names and results; the lines above it can name nets, parts and paths
of the board, so read them before sending them on. While this runs, do not
use the MCP server's Altium tools: both drive the same script engine.
"""
import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

DEV = Path(__file__).resolve().parent
SERVER = DEV.parent / "server"
for p in (str(SERVER), str(DEV)):
    if p not in sys.path:
        sys.path.insert(0, p)

PASS, FAIL, SKIP, MANUAL = "PASS", "FAIL", "SKIP", "MANUAL"
HELLO = "SandboxLog('hello');\nResultText := '{\"ok\": true}';"
WEDGE = "SandboxLog('before');\nObj1 := nil;\nS1 := Obj1.Name;\nResultText := 'unreachable';"
SLOW_S = 60.0


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def on_n_drive(p):
    raw = str(p).replace("/", "\\").upper()
    return raw.startswith("N:") or os.path.splitdrive(os.path.abspath(str(p)))[0].upper() == "N:"


def load_live():
    """main.py (the MCP server) as a module, or (None, why)."""
    try:
        import main
    except Exception as e:  # noqa: BLE001 - any failure means no live steps
        return None, f"{type(e).__name__}: {e}"
    for h in logging.getLogger().handlers:
        if type(h) is logging.StreamHandler:
            h.setLevel(logging.WARNING)
    return main, None


class Run:
    def __init__(self, args, live=None, live_error=None, ask=None, say=print):
        self.args = args
        self.board = Path(args.board)
        stamp = time.strftime("run_%Y%m%d_%H%M%S")
        self.dir = Path(args.work).resolve() / stamp
        # every file the person opens or saves carries the run's stamp, so Altium's
        # recent-files list cannot hand them an earlier run's copy
        self.stamp = stamp[len("run_"):]
        self.copy = self.dir / self.name("board_copy")
        self.live, self.live_error = live, live_error
        self.ask_fn = ask
        self.say = say
        self.results = []
        self.files = {}
        self.loop = None
        self.copy_open = False
        self.info, self.conn = {}, {}
        self.board_hash = None

    # ------------------------------------------------------------ plumbing
    def record(self, step, status, detail=""):
        self.results.append({"step": step, "status": status, "detail": detail})
        self.say(f"  {status:6s} {step}: {detail}")

    def tools(self):
        import offline_tools
        return offline_tools

    def tool(self, fn_name, **kw):
        T = self.tools()
        return T.call(getattr(T, fn_name), **kw)

    def ask(self, step, what, passes):
        """A question for the person at the PC. y -> PASS, n -> FAIL, s -> SKIP."""
        if self.ask_fn is None:
            self.record(step, MANUAL, f"{what} Pass: {passes}")
            return MANUAL
        self.say(f"\n  [?] {what}\n      pass: {passes}")
        while True:
            a = self.ask_fn("      y = passed, n = failed, s = skip: ").strip().lower()
            if a in ("y", "n", "s"):
                break
        if a == "y":
            self.record(step, PASS, "confirmed by you")
            return PASS
        if a == "s":
            self.record(step, SKIP, "skipped by you")
            return SKIP
        note = self.ask_fn("      what happened (dialog text, what looked wrong)? ").strip()
        self.record(step, FAIL, note or "reported failed")
        return FAIL

    def name(self, base):
        return f"{base}_{self.stamp}.PcbDoc"

    def wait(self, what, check=None, tries=3, why=None):
        """Ask the person to do something first. With `check`, re-ask (up to
        `tries` times) until check() is true. False when they skip, cannot,
        or it never comes true."""
        if self.ask_fn is None:
            return False
        self.say(f"\n  [!] {what}")
        for n in range(tries):
            a = self.ask_fn("      press Enter when done, or s then Enter to skip: ")
            if a.strip().lower() == "s":
                return False
            if check is None or check():
                return True
            self.say(f"      not yet: {why() if why else 'the check did not pass'}"
                     + ("; try again" if n + 1 < tries else ""))
        return False

    def elsewhere(self, filename):
        """The same base name saved into another run folder (Altium's Save As
        dialog opens in the folder of the last file opened)."""
        base = filename.rsplit("_", 1)[0] if "_" in filename else filename
        hits = [p for p in self.dir.parent.glob(f"run_*/{base}*.PcbDoc")
                if p.parent != self.dir]
        hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return hits[0] if hits else None

    def missing_file(self, path):
        def why():
            other = self.elsewhere(path.name)
            return (f"{path} is not there" + (f"; a file of that kind was saved in {other.parent} "
                                              "(an earlier run's folder)" if other else ""))
        return why

    def board_probe(self, path):
        """What Altium sees of a board path: {"client": bool, "board": bool},
        or None when the probe could not run. Read-only."""
        from offline.live import q
        body = (f"S1 := {q(str(path))};\n"
                "S2 := 'client=0';\n"
                "Obj2 := Client.GetDocumentByPath(S1);\n"
                "if Obj2 <> nil then S2 := 'client=1';\n"
                "S3 := 'board=0';\n"
                "if PCBServer <> nil then\n"
                "begin\n"
                "    Obj1 := PCBServer.GetPCBBoardByPath(S1);\n"
                "    if Obj1 <> nil then S3 := 'board=1';\n"
                "end;\n"
                "ResultText := '{\"probe\": \"' + S2 + ',' + S3 + '\"}';")
        r = self.script("probe", body, timeout=60)
        try:
            got = json.loads(r.get("result") or "{}").get("probe", "")
        except ValueError:
            return None
        if not r.get("success") or not got:
            return None
        return {"client": "client=1" in got, "board": "board=1" in got}

    def call_live(self, name, *a, **kw):
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
        out = self.loop.run_until_complete(getattr(self.live, name)(None, *a, **kw))
        return json.loads(out) if isinstance(out, str) else out

    def altium_state(self):
        return self.live.altium_guard.altium_state()["state"]

    def need_live(self, step):
        if self.live is None:
            self.record(step, SKIP, f"live steps off: {self.live_error}")
            return False
        return True

    def need_running(self, step):
        if not self.need_live(step):
            return False
        if self.altium_state() == "running":
            return True
        if self.wait("Start Altium (no board needs to be open yet)."):
            if self.altium_state() == "running":
                return True
        self.record(step, SKIP, "Altium is not running")
        return False

    def need_copy_open(self, step):
        if not self.need_running(step):
            return False
        if self.copy_open:
            return True
        seen = {}

        def is_open():
            seen["probe"] = self.board_probe(self.copy)
            return bool(seen["probe"] and seen["probe"]["board"])

        def why():
            pr = seen.get("probe")
            if pr is None:
                return "the probe script did not run (see the sandbox log)"
            if pr["client"]:
                return ("Altium has the document open but PCBServer.GetPCBBoardByPath does not "
                        "find it: click into the board once so the PCB editor loads it")
            return (f"Altium does not have {self.copy} open; close any earlier board_copy and "
                    "open this exact path (paste it into File > Open)")
        if self.wait(f"In Altium: close every other PCB document, then File > Open {self.copy} "
                     "(paste the full path; on its own, not in a project) and leave it open.",
                     check=is_open, why=why):
            self.copy_open = True
            return True
        pr = seen.get("probe")
        self.record(step, FAIL if pr is not None else SKIP,
                    f"{self.copy.name} not found open in Altium: {why()}"
                    if pr is not None else f"needs {self.copy.name} open in Altium")
        return False

    def script(self, step, body, timeout=120):
        """Run a body in the sandbox; recovers the engine if it wedges."""
        r = self.call_live("run_altium_script", body, timeout_seconds=timeout)
        if r.get("executor_wedged"):
            rec = self.call_live("recover_altium_script_engine")
            r["recovered"] = rec.get("recovered")
            if not rec.get("recovered"):
                self.say("  the script engine did not recover: restart Altium before going on")
        return r

    # ------------------------------------------------------------ steps
    def step_env(self):
        missing, versions = [], {}
        for mod in ("numpy", "shapely", "scipy", "PIL"):
            try:
                m = __import__(mod)
                versions[mod] = getattr(m, "__version__", "?")
            except ImportError:
                missing.append(mod)
        if "shapely" in versions and int(versions["shapely"].split(".")[0]) < 2:
            missing.append("shapely>=2")
        if missing:
            self.record("env", FAIL, f"missing {missing}: \"{sys.executable}\" -m pip install -r "
                                     f"\"{SERVER / 'offline' / 'requirements.txt'}\"")
            return
        T = self.tools()

        class Fake:
            names = []

            def add_tool(self, fn, name=None, description=None):
                self.names.append(name)
        names = set(T.register(Fake()))
        n = len(names)
        detail = f"python {sys.version.split()[0]}, " + ", ".join(
            f"{k} {v}" for k, v in versions.items()) + f"; {n} offline tools"
        if self.live is not None:
            listed = [t for t in self.loop_run(self.live.mcp.list_tools()) if t.name in names]
            detail += f"; main.py registers {len(listed)}"
            if len(listed) != n:
                self.record("env", FAIL, detail)
                return
        else:
            detail += f"; main.py not loaded ({self.live_error})"
        self.record("env", PASS, detail)

    def loop_run(self, coro):
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
        return self.loop.run_until_complete(coro)

    def step_copy(self):
        b = self.board
        if not b.exists() or b.suffix.lower() != ".pcbdoc":
            raise SystemExit(f"not a .PcbDoc: {b}")
        if on_n_drive(self.dir):
            raise SystemExit("the work folder is on the N: drive; choose a local folder")
        self.dir.mkdir(parents=True, exist_ok=False)
        self.board_hash = sha(b)
        shutil.copy2(b, self.copy)
        self.record("copy", PASS, f"{b.name} -> {self.copy}")

    def step_read(self):
        timings, bad = {}, []
        for name, kw in (("offline_pcb_info", {}), ("offline_pcb_rules", {}),
                         ("offline_pcb_connectivity", {}),
                         ("offline_pcb_drc", {"copper": "saved", "limit": 0}),
                         ("offline_pcb_repour", {"compare": True})):
            r = self.tool(name, board=str(self.copy), **kw)
            timings[name.replace("offline_pcb_", "")] = r["seconds"]
            if not r["ok"]:
                bad.append(f"{name}: {r['error']}")
                continue
            if name == "offline_pcb_info":
                self.info = r
            if name == "offline_pcb_connectivity":
                self.conn = r
        slow = {k: v for k, v in timings.items() if v > SLOW_S}
        s = self.info.get("summary", {})
        detail = (f"{s.get('tracks')} tracks, {s.get('pads')} pads, {s.get('polygons')} "
                  f"polygons; seconds {timings}")
        if bad:
            self.record("read", FAIL, "; ".join(bad))
        elif slow:
            self.record("read", FAIL, f"slower than {SLOW_S:.0f} s: {slow}; {detail}")
        else:
            self.record("read", PASS, detail)

    def step_outputs(self):
        d, c = self.dir, str(self.copy)
        # the writer, one edit per file (dev/validate_pcbwrite.py)
        try:
            import validate_pcbwrite
            buf = io.StringIO()
            with redirect_stdout(buf):
                validate_pcbwrite.main([c, "--out", str(d / "pcbwrite")])
            (d / "pcbwrite.txt").write_text(buf.getvalue(), encoding="utf-8")
            steps = json.loads((d / "pcbwrite" / "steps.json").read_text(encoding="utf-8"))
            self.files["pcbwrite"] = d / "pcbwrite"
            self.record("outputs:writer", PASS, f"{len(steps['steps'])} step files in "
                                                f"{d / 'pcbwrite'} (list: {d / 'pcbwrite.txt'})")
        except SystemExit as e:
            self.record("outputs:writer", SKIP, f"board not suitable: {e}")
        except Exception as e:  # noqa: BLE001
            self.record("outputs:writer", FAIL, f"{type(e).__name__}: {e}")
        # pours stored three ways
        for store in ("pour", "solid", "unpoured"):
            out = d / self.name(store)
            r = self.tool("offline_pcb_repour", board=c, out=str(out), store=store)
            if r["ok"]:
                self.files[store] = out
                self.record(f"outputs:{store}", PASS, f"{len(r['polygons'])} polygon(s) -> {out}")
            else:
                self.record(f"outputs:{store}", FAIL, r["error"])
        # a blank board with the copy as template
        box = (self.info.get("outline") or {}).get("box")
        if box:
            x0, y0, x1, y1 = box
            out = d / self.name("blank")
            r = self.tool("offline_pcb_blank_board", template=c, out=str(out),
                          width=round(min(x1 - x0, 100.0), 3), height=round(min(y1 - y0, 80.0), 3),
                          at=[x0, y0], holes=[[x0 + 4, y0 + 4, 3.2]])
            if r["ok"]:
                self.files["blank"] = out
                self.record("outputs:blank", PASS, str(out))
            else:
                self.record("outputs:blank", FAIL, r["error"])
        else:
            self.record("outputs:blank", SKIP, "the copy has no outline")
        # live scripts: rename a two-pad net, add a short track
        try:
            self.make_live_plan()
        except SystemExit as e:
            self.record("outputs:live", SKIP, f"board not suitable: {e}")
        except Exception as e:  # noqa: BLE001
            self.record("outputs:live", FAIL, f"{type(e).__name__}: {e}")
        # the membership audit body
        r = self.tool("offline_pcb_membership_audit_script", board_path=c)
        if r["ok"]:
            (d / "audit.pas").write_text(r["script"], encoding="utf-8")
            self.files["audit"] = d / "audit.pas"
            self.record("outputs:audit", PASS, str(d / "audit.pas"))
        else:
            self.record("outputs:audit", FAIL, r["error"])
        self.outputs_optional()

    def make_live_plan(self):
        from offline.pcbdoc import PcbDoc
        from validate_pcbwrite import pick
        doc = PcbDoc.open(self.copy)
        t = pick(doc)
        x0, y0 = t["spot"]
        self.live_check = {"renamed": (t["rename_net"], "LIVE_RENAMED"),
                           "track": (round(x0, 4), round(y0, 4), round(x0 + 3, 4), t["net"])}
        plan = {"version": 1, "ops": [
            {"op": "add_track", "layer": "Top Layer", "x1": x0, "y1": y0, "x2": x0 + 3, "y2": y0,
             "width": 0.25, "net": t["net"]},
            {"op": "rename_net", "from": t["rename_net"], "to": "LIVE_RENAMED"}]}
        (self.dir / "live_plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
        r = self.tool("offline_pcb_live_scripts", board=str(self.copy), plan=plan,
                      board_path=str(self.copy), out_dir=str(self.dir / "live"))
        if not r["ok"]:
            raise RuntimeError(r["error"])
        self.files["live"] = self.dir / "live"
        self.record("outputs:live", PASS, f"{r['scripts']} script(s) in {self.dir / 'live'}; "
                                          f"unverified: {r['unverified'] or 'none'}")

    def outputs_optional(self):
        a, d, c = self.args, self.dir, str(self.copy)
        if a.power:
            try:
                net, amps, ends = a.power.split(":", 2)
                src, snk = ends.split(">", 1)
                plan = {"net": net, "sources": src.split("+"), "sinks": snk.split("+"),
                        "current": float(amps), "targets": {"density_a_per_mm2": 40}}
            except ValueError:
                self.record("outputs:copper", FAIL, "--power wants NET:AMPS:SRC>SINK")
                plan = None
            if plan:
                r = self.tool("offline_pcb_copper_plan", board=c, plan=plan,
                              plan_out=str(d / "copper_plan.json"), png=str(d / "copper.png"))
                if r["ok"]:
                    r2 = self.tool("offline_pcb_apply_plan", board=c, plan=r["plan"],
                                   out=str(d / self.name("planned")))
                    if r2["ok"]:
                        self.files["planned"] = d / self.name("planned")
                    self.record("outputs:copper", PASS if r2["ok"] else FAIL,
                                f"targets {'met' if r['met'] else 'NOT met'}, {r['ops']} op(s)"
                                if r2["ok"] else r2["error"])
                else:
                    self.record("outputs:copper", FAIL, r["error"])
        else:
            self.record("outputs:copper", SKIP, "no --power NET:AMPS:SRC>SINK given")
        self.outputs_autoroute()
        if a.project:
            r = self.tool("offline_pcb_eco_preview", schematic=a.project, board=c)
            if not r["ok"]:
                self.record("eco-preview", FAIL, r["error"])
            else:
                s = {k: v for k, v in r["summary"].items() if v and k != "relinks"}
                self.record("eco-preview", PASS if not s else MANUAL,
                            "schematic and board agree" if not s else
                            f"differences {s}: expected only if the project has unsynced edits")
        else:
            self.record("eco-preview", SKIP, "no --project given")
        if a.gerbers:
            r = self.tool("offline_pcb_fab_check", board=c, folder=a.gerbers)
            if not r["ok"]:
                self.record("fab-check", FAIL, r["error"])
            else:
                self.record("fab-check", PASS if r["passed"] else FAIL,
                            f"missing {[m['layer'] for m in r['missing']]}, "
                            f"problems {r['problems'][:5]}")
        else:
            self.record("fab-check", SKIP, "no --gerbers folder given")

    def outputs_autoroute(self):
        from offline import autoroute as A
        try:
            jar = A.find_freerouting()
            java = A.find_java(need=A.jar_java(jar)) if jar else None
        except Exception as e:  # noqa: BLE001
            jar, java = None, None
            why = f"{type(e).__name__}: {e}"
        else:
            why = "Freerouting jar not found (python -m offline autoroute --fetch)" \
                if not jar else "no suitable Java found"
        if not jar or not java:
            self.record("outputs:autoroute", SKIP, why)
            return
        doc = self.open_copy()
        power = A.power_nets(doc)
        nets = []
        for u in self.conn.get("unrouted", []):
            if u["net"] not in nets and u["net"] not in power:
                nets.append(u["net"])
        d, base, ripped = self.dir, str(self.copy), ""
        if not nets:
            # a routed board: take the wiring off a few small signal nets, route those
            nets = self.rip_candidates(doc, power)
            if not nets:
                self.record("outputs:autoroute", SKIP, "no small signal net to route")
                return
            plan = {"version": 1, "ops": [{"op": "remove", "kinds": ["track", "arc", "via"],
                                           "net": n, "free": True} for n in nets]}
            r = self.tool("offline_pcb_apply_plan", board=base, plan=plan,
                          out=str(d / self.name("ripped")))
            if not r["ok"]:
                self.record("outputs:autoroute", FAIL, f"ripping up {nets}: {r['error']}")
                return
            base = str(d / self.name("ripped"))
            ripped = f" (wiring removed first: {base})"
        r = self.tool("offline_pcb_autoroute", board=base, plan_out=str(d / "route.json"),
                      nets=nets, check=True, timeout=600, workdir=str(d / "freerouting"))
        if not r["ok"]:
            self.record("outputs:autoroute", FAIL, r["error"])
            return
        r2 = self.tool("offline_pcb_apply_plan", board=base, plan=r["plan"],
                       out=str(d / self.name("routed")))
        if r2["ok"]:
            self.files["routed"] = d / self.name("routed")
        drc = r.get("drc") or {}
        self.record("outputs:autoroute", PASS if r2["ok"] else FAIL,
                    f"{len(nets)} net(s){ripped}, {r['connections_after']} open after, "
                    f"{drc.get('new', '?')} new DRC" if r2["ok"] else r2["error"])

    @staticmethod
    def rip_candidates(doc, power, count=3):
        """Small signal nets wired with free tracks only (no pour, no plane)."""
        pads, wired = {}, set()
        for p in doc.pads:
            if p.net is not None:
                pads[p.net] = pads.get(p.net, 0) + 1
        for t in doc.tracks:
            if t.net is not None and t.component is None and t.polygon is None:
                wired.add(t.net)
        poured = {p.net for p in doc.polygons if p.net is not None}
        names = {n.index: n.name for n in doc.nets}
        cands = [i for i, k in pads.items() if 2 <= k <= 6 and i in wired and i not in poured
                 and names.get(i) not in power]
        cands.sort(key=lambda i: (pads[i], names[i]))
        return [names[i] for i in cands[:count]]

    def open_copy(self):
        from offline.pcbdoc import PcbDoc
        return PcbDoc.open(self.copy)

    def step_preflight(self):
        if not self.need_live("preflight"):
            return
        ex = Path(self.live.EXCHANGE_DIR)
        watch = [ex / "sandbox_log.txt", ex / "sandbox_result.json", Path(self.live.SANDBOX_PAS)]
        before = [f.stat().st_mtime if f.exists() else None for f in watch]
        r = self.call_live("run_altium_script", "Result := 'x';")
        after = [f.stat().st_mtime if f.exists() else None for f in watch]
        ok = not r.get("success") and "preflight" in (r.get("error") or "") and before == after
        self.record("preflight", PASS if ok else FAIL,
                    "refused before anything was written or sent" if ok else
                    f"answer {r.get('error')!r}; files touched: {before != after}")

    def step_not_running(self):
        if not self.need_live("not-running"):
            return
        if self.altium_state() != "not_running":
            self.record("not-running", SKIP, "Altium is running; close it and run again with "
                                             "--only not-running")
            return
        r = self.call_live("run_altium_script", HELLO)
        time.sleep(3)
        still = self.altium_state()
        ok = r.get("blocked") == "not_running" and still == "not_running"
        self.record("not-running", PASS if ok else FAIL,
                    "refused and Altium not started" if ok else
                    f"answer {r.get('blocked') or r.get('error')!r}, Altium now {still}")

    def step_sandbox(self):
        if not self.need_running("sandbox"):
            return
        r = self.script("sandbox", HELLO)
        try:
            ok = r.get("success") and json.loads(r.get("result") or "null") == {"ok": True}
        except ValueError:
            ok = False
        self.record("sandbox", PASS if ok else FAIL, "the sandbox ran a body" if ok else
                    f"{r.get('error') or r.get('result')}; steps {r.get('steps')}")

    def step_open_files(self):
        if self.ask_fn is not None and not self.need_running("open-files"):
            return
        if "pcbwrite" in self.files:
            self.ask("open:writer",
                     f"Open each file in {self.files['pcbwrite']} in order (File > Open, not "
                     "in a project), check the line printed for it in "
                     f"{self.dir / 'pcbwrite.txt'}, close without saving.",
                     "every file opens with no dialog and every check line holds")
        checks = {
            "pour": "opens with no dialog; the pours look like the original's",
            "solid": "opens with no dialog; the PCB panel lists no polygons; the copper is "
                     "there as regions on the right nets",
            "blank": "opens with no dialog; outline and the hole are there; Design > Rules "
                     "shows the original's rules; Design > Classes is empty",
            "routed": "opens with no dialog; new tracks on their nets; after Repour All the "
                      "DRC shows nothing new against the original",
            "planned": "opens with no dialog; after Repour All the new copper is on the net "
                       "(PCB panel), matches copper.png, and DRC shows no new Clearance or "
                       "Short-Circuit violation",
        }
        for key, passes in checks.items():
            if key in self.files:
                self.ask(f"open:{key}", f"File > Open {self.files[key]}; close without saving.",
                         passes)

    def step_repour_compare(self):
        if "unpoured" not in self.files:
            self.record("repour-compare", SKIP, "no unpoured file was written")
            return
        after = self.dir / self.name("after_repour")
        what = (f"File > Open {self.files['unpoured']} (paste the full path), Tools > Polygon "
                f"Pours > Repour All, then File > Save As {after} (paste the full path: the "
                "dialog may open in another folder).")
        if self.ask_fn is None:
            self.record("repour-compare", MANUAL, what + " Then from server/: python -m offline "
                        f"pour {after} --mode saved --compare (pass: area-weighted IoU >= 0.98, "
                        "every polygon >= 0.90)")
            return
        if not self.need_running("repour-compare"):
            return
        if not self.wait(what, check=after.exists, why=self.missing_file(after)):
            if after.exists():
                self.record("repour-compare", SKIP, "skipped by you")
            else:
                self.record("repour-compare", FAIL, self.missing_file(after)())
            return
        r = self.tool("offline_pcb_repour", board=str(after), mode="saved", compare=True)
        if not r["ok"]:
            self.record("repour-compare", FAIL, r["error"])
            return
        rows = [p["vs_saved"] for p in r["polygons"] if p.get("vs_saved", {}).get("iou")
                is not None]
        if not rows:
            self.record("repour-compare", SKIP, "no poured polygons to compare")
            return
        inter = sum(x["saved_mm2"] * x["iou"] for x in rows)
        total = sum(x["saved_mm2"] for x in rows) or 1.0
        worst = min(x["iou"] for x in rows)
        iou = inter / total
        ok = iou >= 0.98 and worst >= 0.90
        self.record("repour-compare", PASS if ok else FAIL,
                    f"{len(rows)} polygon(s): area-weighted IoU {iou:.4f}, worst {worst:.4f} "
                    "(pass: >= 0.98 and every polygon >= 0.90)")

    def step_audit(self):
        if "audit" not in self.files:
            self.record("audit", SKIP, "no audit script was written")
            return
        if not self.need_copy_open("audit"):
            return
        r = self.script("audit", self.files["audit"].read_text(encoding="utf-8"), timeout=600)
        if not r.get("success"):
            self.record("audit", FAIL, f"{r.get('error')}; last step {r.get('last_step_reached')}"
                                       f"; recovered {r.get('recovered')}")
            return
        try:
            res = json.loads(r.get("result") or "{}")
        except ValueError:
            self.record("audit", FAIL, f"ResultText is not JSON: {r.get('result')!r:.200}")
            return
        if res.get("error"):
            self.record("audit", FAIL, res["error"])
            return
        stale, unreg = res.get("stale_members"), res.get("unregistered")
        counts = f"checked {res.get('checked')}, stale {stale}, unregistered {unreg}"
        if not res.get("checked"):
            self.record("audit", FAIL, counts)
        elif stale in (0, None) and unreg in (0, None):
            self.record("audit", PASS, counts)
        else:
            self.record("audit", MANUAL, counts + ": look at the listed primitives in "
                        "Altium; on a clean board both should be 0")

    def step_drc_agree(self):
        if not self.need_copy_open("drc-agree"):
            return
        if not self.wait(f"Close every other PCB document, then with {self.copy.name} in front: "
                         "Tools > Design Rule Check > Run Design Rule Check, then close the "
                         "report (get_drc_violations reads the board in front)."):
            self.record("drc-agree", SKIP, "Altium's DRC was not run")
            return
        import compare_drc as C
        alt = self.call_live("get_drc_violations", max_violations=100000)
        if alt.get("success") is False or "violations" not in alt:
            self.record("drc-agree", FAIL, f"get_drc_violations: {alt.get('error') or alt}")
            return
        # text on: Altium checks designators and other text (offline as boxes, approximate)
        off = self.tool("offline_pcb_drc", board=str(self.copy), copper="saved", limit=100000,
                        text=True)
        if not off["ok"]:
            self.record("drc-agree", FAIL, off["error"])
            return
        a_path, o_path = self.dir / "altium_drc.json", self.dir / "offline_drc.json"
        a_path.write_text(json.dumps(alt, indent=1), encoding="utf-8")
        o_path.write_text(json.dumps(off, indent=1), encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            C.main([str(o_path), str(a_path)])
        (self.dir / "drc_compare.txt").write_text(buf.getvalue(), encoding="utf-8")
        ours = [o for o in C.offline_rows(off) if not o["rule"].startswith("dfm:")]
        theirs, total, truncated = C.altium_rows(alt)
        pairs = C.match(ours, theirs, 1.0)
        union = len(ours) + len(theirs) - len(pairs)
        agree = len(pairs) / union if union else 1.0     # both clean is agreement
        detail = (f"agreement {agree:.1%}: {len(pairs)} matched, {len(ours) - len(pairs)} offline "
                  f"only, {len(theirs) - len(pairs)} Altium only (table: "
                  f"{self.dir / 'drc_compare.txt'})")
        if ours and theirs and not pairs:
            detail += ("; nothing matched at all: Altium's markers may be from another board "
                       "(the one in front when get_drc_violations ran), or from before a save")
        if truncated:
            self.record("drc-agree", FAIL, "Altium's list was truncated; " + detail)
        else:
            self.record("drc-agree", PASS if agree >= 0.9 else FAIL, detail)

    def step_live_edit(self):
        if "live" not in self.files:
            self.record("live-edit", SKIP, "no live scripts were written")
            return
        if not self.need_copy_open("live-edit"):
            return
        folder = self.files["live"]
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        bodies = [m["file"] for m in manifest] + ["999_save.pas"]
        for f in bodies:
            r = self.script("live-edit", (folder / f).read_text(encoding="utf-8"), timeout=300)
            res = None
            try:
                res = json.loads(r.get("result") or "null")
            except ValueError:
                pass
            if not r.get("success") or not isinstance(res, dict) or res.get("error"):
                self.record("live-edit", FAIL, f"{f}: {r.get('error') or res or r.get('result')}"
                                               f"; last step {r.get('last_step_reached')}")
                return
            self.say(f"    {f}: {json.dumps(res)[:120]}")
        time.sleep(1)
        doc = self.open_copy()
        old, new = self.live_check["renamed"]
        x1, y1, x2, net = self.live_check["track"]
        renamed = doc.has_net(new) and not doc.has_net(old)
        want = doc.net_index(net) if doc.has_net(net) else None
        track = any(abs(t.x1 - x1) < 0.01 and abs(t.y1 - y1) < 0.01 and abs(t.x2 - x2) < 0.01
                    and t.net == want for t in doc.tracks)
        ok = renamed and track
        self.record("live-edit", PASS if ok else FAIL,
                    "Altium made both edits and saved; the saved file reads back with them"
                    if ok else f"saved file: net renamed {renamed}, track present {track}")

    def step_wedge(self):
        if not self.args.wedge:
            self.record("wedge", SKIP, "only with --wedge (it wedges the engine on purpose)")
            return
        if not self.need_running("wedge"):
            return
        r = self.call_live("run_altium_script", WEDGE, timeout_seconds=30, allow_unknown=True)
        if not (r.get("executor_wedged") and r.get("last_step_reached") == "before"):
            self.record("wedge", FAIL, f"expected a wedge after 'before': {r.get('error')}")
            return
        t0 = time.time()
        r2 = self.call_live("run_altium_script", HELLO)
        quick = r2.get("blocked") == "wedged" and time.time() - t0 < 1.0
        rec = self.call_live("recover_altium_script_engine")
        r3 = self.call_live("run_altium_script", HELLO)
        ok = quick and rec.get("recovered") and r3.get("success")
        self.record("wedge", PASS if ok else FAIL,
                    f"refused while wedged: {quick}; recovered: {rec.get('recovered')} "
                    f"({rec.get('method')}); next body ran: {r3.get('success')}")

    def step_untouched(self):
        same = sha(self.board) == self.board_hash
        self.record("original-untouched", PASS if same else FAIL,
                    "the named board is byte for byte as before" if same else
                    "THE NAMED BOARD CHANGED during the run")


STEPS = [
    ("env", "step_env", "offline requirements import; the tools register (no Altium)"),
    ("copy", "step_copy", "copy the named board into a new run folder"),
    ("read", "step_read", "info, rules, connectivity, DRC, repour of the copy, timed"),
    ("outputs", "step_outputs", "write the test files: writer steps, pours, blank, live "
                                "scripts, audit body; copper/autoroute/ECO/fab when possible"),
    ("preflight", "step_preflight", "a bad body is refused before anything reaches Altium"),
    ("not-running", "step_not_running", "with Altium closed: refused, Altium not started"),
    ("sandbox", "step_sandbox", "a one-line body runs in the sandbox"),
    ("open-files", "step_open_files", "you open each written file: no dialog, checks hold"),
    ("repour-compare", "step_repour_compare", "Altium's Repour All against the offline pour"),
    ("audit", "step_audit", "the membership audit runs read-only on the open copy"),
    ("drc-agree", "step_drc_agree", "offline DRC against Altium's DRC markers on the copy"),
    ("live-edit", "step_live_edit", "live scripts add a track, rename a net and save the copy"),
    ("wedge", "step_wedge", "(--wedge) wedge the engine on purpose, refuse, recover"),
]


def main(argv=None, live=None, ask=None, say=print):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board", nargs="?")
    ap.add_argument("--work", default=None, help="folder for the run folders "
                                                 "(default: C:\\scratch\\offline_check)")
    ap.add_argument("--only", nargs="+", metavar="STEP")
    ap.add_argument("--from", dest="start", metavar="STEP")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-ask", action="store_true", help="never prompt; human checks are MANUAL")
    ap.add_argument("--no-live", action="store_true", help="skip every step that talks to Altium")
    ap.add_argument("--wedge", action="store_true", help="also run the wedge-and-recover step")
    ap.add_argument("--project", help="the board's .PrjPcb, for the ECO preview")
    ap.add_argument("--gerbers", help="a folder of Altium's Gerber and drill outputs")
    ap.add_argument("--power", help="NET:AMPS:SRC>SINK for a copper plan, e.g. VBAT:30:J1.1>Q1.D")
    a = ap.parse_args(argv)
    names = [s[0] for s in STEPS]
    if a.list:
        for n, _, what in STEPS:
            say(f"  {n:15s} {what}")
        return 0
    if not a.board:
        ap.error("give the board to copy")
    for n in (a.only or []) + ([a.start] if a.start else []):
        if n not in names:
            ap.error(f"unknown step {n!r}; --list shows them")
    a.work = a.work or ("C:\\scratch\\offline_check" if os.name == "nt" else
                        str(Path.home() / "offline_check"))
    if on_n_drive(a.work):
        ap.error("the work folder is on the N: drive; choose a local folder")
    live_error = "--no-live" if a.no_live else None
    if live is None and not a.no_live:
        live, live_error = load_live()
    if a.no_ask:
        ask = None
    elif ask is None and sys.stdin and sys.stdin.isatty():
        ask = input
    run = Run(a, live=live if not a.no_live else None, live_error=live_error, ask=ask, say=say)
    need = set(names[names.index(a.start):] if a.start else names)
    if a.only:
        need = set(a.only)
    need |= {"env", "copy"}
    if need & {"open-files", "repour-compare", "audit", "live-edit"}:
        need.add("outputs")     # they use the files it writes
    if "outputs" in need:
        need.add("read")        # it uses the outline and open connections read finds
    say(f"offline tools acceptance run on a copy of {a.board}")
    say("do not use the MCP server's Altium tools while this runs")
    for n, meth, what in STEPS:
        if n not in need:
            continue
        say(f"- {n}: {what}")
        try:
            getattr(run, meth)()
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 - a step that crashes is a FAIL, named
            run.record(n, FAIL, f"crashed: {type(e).__name__}: {e}")
        if n == "env" and run.results[-1]["status"] == FAIL:
            break
    if run.board_hash is not None:
        run.step_untouched()
    out = {"board": a.board, "folder": str(run.dir), "results": run.results}
    run.dir.mkdir(parents=True, exist_ok=True)
    (run.dir / "results.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    counts = {s: sum(1 for r in run.results if r["status"] == s)
              for s in (PASS, FAIL, SKIP, MANUAL)}
    say("\nsummary: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    for r in run.results:
        say(f"  {r['status']:6s} {r['step']}")
    say(f"saved {run.dir / 'results.json'}")
    if run.loop is not None:
        run.loop.close()
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
