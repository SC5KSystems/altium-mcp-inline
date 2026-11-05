"""dev/live_validate_tools.py against a fake Altium: every live step's logic,
with the answers a working Altium would give (and a wedge on purpose). The
real run is on the Altium PC; this only proves the script's own logic.

    python -m unittest discover -s server/tests -p "test_offline_accept.py"
"""

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "dev"))

try:
    import shapely  # noqa: F401
    import scipy  # noqa: F401
    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False

MIL = 0.0254


class FakeAltium:
    """What main.py's tools would answer with a working Altium."""

    def __init__(self, tmp, state="running"):
        import offline_tools as T
        self.T = T
        self.EXCHANGE_DIR = tmp / "exchange"
        self.EXCHANGE_DIR.mkdir()
        self.SANDBOX_PAS = tmp / "Sandbox.pas"
        self.SANDBOX_PAS.write_text("// sandbox\n")
        self.state = state
        self.wedged = False
        self.altium_guard = SimpleNamespace(
            altium_state=lambda: {"state": self.state, "pids": [1] if self.state != "not_running"
                                  else []})
        self.mcp = SimpleNamespace(list_tools=self._list)
        self.bodies = []

    async def _list(self):
        return [SimpleNamespace(name=f.__name__) for f in self.T.TOOLS] + \
            [SimpleNamespace(name="run_altium_script")]

    @staticmethod
    def _ok(result):
        return json.dumps({"success": True, "result": json.dumps(result), "steps": []})

    async def run_altium_script(self, ctx, script, timeout_seconds=120, allow_unknown=False):
        self.bodies.append(script)
        if "Result :=" in script.replace("ResultText :=", ""):
            return json.dumps({"success": False, "error": "preflight rejected the script; "
                                                          "nothing was sent to Altium"})
        if self.state == "not_running":
            return json.dumps({"success": False, "blocked": "not_running"})
        if self.wedged:
            return json.dumps({"success": False, "blocked": "wedged"})
        if "S1 := Obj1.Name" in script:
            self.wedged = True
            return json.dumps({"success": False, "error": "script started but did not finish",
                               "last_step_reached": "before", "executor_wedged": True})
        if "SandboxLog('hello')" in script:
            return self._ok({"ok": True})
        if "GroupIterator_Create" in script:
            return self._ok({"checked": 42, "stale_members": 0, "unregistered": 0, "fixed": 0,
                             "findings": []})
        if "DoFileSave" in script:
            self._save(script)
            return self._ok({"saved": 1})
        return self._ok({"done": 1})

    def _save(self, script):
        """Altium saving the copy with the live plan applied."""
        from offline.editplan import apply
        from offline.pcbdoc import PcbDoc
        path = Path(script.split("GetDocumentByPath('", 1)[1].split("')", 1)[0])
        plan = json.loads((path.parent / "live_plan.json").read_text())
        ed, _ = apply(PcbDoc.open(path), plan)
        tmp = path.with_name("altium_saved.PcbDoc")
        ed.save(tmp)
        os.replace(tmp, path)

    async def recover_altium_script_engine(self, ctx):
        self.wedged = False
        return json.dumps({"recovered": True, "method": "EditScript:Stop"})

    async def get_drc_violations(self, ctx, rule_names=None, max_violations=200):
        from offline.drc import DRC
        from offline.pcbdoc import PcbDoc
        board = next(self.EXCHANGE_DIR.parent.glob("work/run_*/board_copy.PcbDoc"))
        d = DRC(PcbDoc.open(board), copper="saved")
        d.run()
        vs = [{"rule": v.rule, "x": None if v.x is None else v.x / MIL,
               "y": None if v.y is None else v.y / MIL, "descriptor": v.message}
              for v in d.violations]
        return json.dumps({"total_violations": len(vs), "truncated": False, "violations": vs})


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class Acceptance(unittest.TestCase):
    def setUp(self):
        from offline import autoroute
        from offline.example import board
        self.tmp = Path(tempfile.mkdtemp())
        self.board = self.tmp / "named.PcbDoc"
        board().save(self.board)
        self.work = self.tmp / "work"
        self._jar = autoroute.find_freerouting
        autoroute.find_freerouting = lambda path=None: None    # no Freerouting run here

    def tearDown(self):
        from offline import autoroute
        autoroute.find_freerouting = self._jar
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, live, *extra, answers="y"):
        import live_validate_tools as V
        said = []

        def ask(prompt):
            # the person: does what the last instruction says, then answers
            last = next((s for s in reversed(said) if "[!]" in s or "[?]" in s), "")
            if "Save As" in last:
                run = next(self.work.glob("run_*"))
                shutil.copy(run / "pour.PcbDoc", run / "after_repour.PcbDoc")
            if "press Enter" in prompt:
                return ""
            return answers
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = V.main([str(self.board), "--work", str(self.work), *extra], live=live,
                        ask=ask, say=said.append)
        res = json.loads(next(self.work.glob("run_*/results.json")).read_text())
        return rc, {r["step"]: r for r in res["results"]}, said

    def test_full_run_passes_with_a_working_altium(self):
        live = FakeAltium(self.tmp)
        rc, res, said = self.run_script(live)
        status = {k: v["status"] for k, v in res.items()}
        for step in ("env", "copy", "read", "outputs:writer", "outputs:pour", "outputs:solid",
                     "outputs:unpoured", "outputs:blank", "outputs:live", "outputs:audit",
                     "preflight", "sandbox", "open:writer", "open:pour", "open:solid",
                     "open:blank", "repour-compare", "audit", "drc-agree", "live-edit",
                     "original-untouched"):
            self.assertEqual(status.get(step), "PASS", (step, res.get(step)))
        self.assertEqual(status["not-running"], "SKIP")
        self.assertEqual(status["wedge"], "SKIP")
        self.assertEqual(status["outputs:autoroute"], "SKIP")
        self.assertEqual(rc, 0, [r for r in res.values() if r["status"] == "FAIL"])
        self.assertIn("IoU 1.0000", res["repour-compare"]["detail"])
        self.assertIn("agreement 100.0%", res["drc-agree"]["detail"])
        import offline_tools
        self.assertIn(f"main.py registers {len(offline_tools.TOOLS)}", res["env"]["detail"])
        preflight = [b for b in live.bodies if "Result := 'x'" in b]
        self.assertEqual(len(preflight), 1)

    def test_a_failed_check_fails_the_run(self):
        live = FakeAltium(self.tmp)
        rc, res, _ = self.run_script(live, "--only", "open-files", answers="n")
        self.assertEqual(res["open:pour"]["status"], "FAIL")
        self.assertEqual(rc, 1)
        self.assertNotIn("live-edit", res, "--only runs only what it names (and what that needs)")
        self.assertIn("outputs:pour", res)

    def test_live_edit_that_did_not_happen_fails(self):
        live = FakeAltium(self.tmp)
        live._save = lambda script: None       # Altium "saved" nothing
        rc, res, _ = self.run_script(live, "--only", "live-edit")
        self.assertEqual(res["live-edit"]["status"], "FAIL")
        self.assertIn("net renamed False", res["live-edit"]["detail"])

    def test_not_running_and_wedge(self):
        live = FakeAltium(self.tmp, state="not_running")
        rc, res, _ = self.run_script(live, "--only", "not-running", "--no-ask")
        self.assertEqual(res["not-running"]["status"], "PASS", res["not-running"])
        shutil.rmtree(self.work)
        (self.tmp / "w2").mkdir()
        live = FakeAltium(self.tmp / "w2")
        rc, res, _ = self.run_script(live, "--only", "wedge", "--wedge")
        self.assertEqual(res["wedge"]["status"], "PASS", res["wedge"])
        self.assertFalse(live.wedged)

    def test_without_live_and_without_a_console(self):
        rc, res, _ = self.run_script(None, "--no-live", "--no-ask")
        self.assertEqual(res["preflight"]["status"], "SKIP")
        self.assertEqual(res["open:pour"]["status"], "MANUAL")
        self.assertEqual(res["repour-compare"]["status"], "MANUAL")
        self.assertEqual(res["original-untouched"]["status"], "PASS")
        self.assertEqual(rc, 0)

    def test_refuses_the_n_drive(self):
        import live_validate_tools as V
        from contextlib import redirect_stderr
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()):
            V.main([str(self.board), "--work", "N:\\scratch", "--no-live", "--no-ask"])
        self.assertTrue(V.on_n_drive("n:/x"))
        self.assertFalse(V.on_n_drive(str(self.tmp)))


if __name__ == "__main__":
    unittest.main()
