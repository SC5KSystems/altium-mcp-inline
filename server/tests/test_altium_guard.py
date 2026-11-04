"""Offline tests for server/altium_guard.py, with every OS call faked.

    python -m unittest discover -s server/tests -p "test_altium_guard.py"

What the real Windows calls do on a live Altium is UNVERIFIED here; see
dev/CLOUD_HANDOFF.md for the local check.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import altium_guard as ag  # noqa: E402

TASKLIST = ('"X2.EXE","4242","Console","1","1,234,567 K"\r\n'
            '"X2.EXE","4343","Console","1","12,345 K"\r\n')


def state(kind, pids=(4242,)):
    return lambda: {"state": kind, "pids": list(pids) if kind != "not_running" else []}


class TasklistTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ag.parse_tasklist(TASKLIST), [4242, 4343])

    def test_none_running(self):
        self.assertEqual(ag.parse_tasklist(
            "INFO: No tasks are running which match the specified criteria.\r\n"), [])

    def test_state_from_windows(self):
        wins = [(1, 4242, "TDocumentForm", "Altium Designer", False)]
        s = ag.altium_state(lambda: [4242], lambda pids: wins)
        self.assertEqual(s["state"], "running")
        hung = [(1, 4242, "TDocumentForm", "Altium Designer", True)]
        self.assertEqual(ag.altium_state(lambda: [4242], lambda pids: hung)["state"], "hung")
        self.assertEqual(ag.altium_state(lambda: [], lambda pids: [])["state"], "not_running")
        self.assertEqual(ag.altium_state(lambda: None, lambda pids: [])["state"], "unknown")


class MarkerTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_clear_when_nothing_recorded(self):
        self.assertIsNone(ag.launch_blocker(self.dir, state_fn=state("running")))

    def test_not_running_blocks_only_when_required(self):
        b = ag.launch_blocker(self.dir, state_fn=state("not_running"))
        self.assertEqual(b["blocked"], "not_running")
        self.assertIsNone(ag.launch_blocker(self.dir, require_running=False,
                                            state_fn=state("not_running")))

    def test_hung_blocks_even_the_bridge(self):
        b = ag.launch_blocker(self.dir, require_running=False, state_fn=state("hung"))
        self.assertEqual(b["blocked"], "hung")

    def test_wedge_blocks_until_cleared(self):
        ag.mark_wedged("test", "unit", last_step="step 3", exchange=self.dir, pids=[4242])
        b = ag.launch_blocker(self.dir, state_fn=state("running"))
        self.assertEqual(b["blocked"], "wedged")
        self.assertEqual(b["last_step"], "step 3")
        self.assertIn("recover_altium_script_engine", b["recovery"])
        ag.clear_wedge(self.dir)
        self.assertIsNone(ag.launch_blocker(self.dir, state_fn=state("running")))

    def test_restarted_altium_clears_the_wedge(self):
        ag.mark_wedged("test", "unit", exchange=self.dir, pids=[1111])
        self.assertIsNone(ag.launch_blocker(self.dir, state_fn=state("running", [4242])))
        self.assertIsNone(ag.read_wedge(self.dir))

    def test_dead_altium_clears_the_wedge(self):
        ag.mark_wedged("test", "unit", exchange=self.dir, pids=[4242])
        ag.launch_blocker(self.dir, state_fn=state("not_running"))
        self.assertIsNone(ag.read_wedge(self.dir))

    def test_unknown_process_state_still_honours_the_marker(self):
        ag.mark_wedged("test", "unit", exchange=self.dir, pids=[])
        b = ag.launch_blocker(self.dir, state_fn=state("unknown", []))
        self.assertEqual(b["blocked"], "wedged")

    def test_unreadable_marker_is_a_wedge(self):
        ag.wedge_path(self.dir).write_text("{not json", encoding="utf-8")
        b = ag.launch_blocker(self.dir, state_fn=state("running"))
        self.assertEqual(b["blocked"], "wedged")


class SandboxCopy(unittest.TestCase):
    """A throwaway copy of the server sandbox project to probe against."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.exchange = self.dir / "exchange"
        self.exchange.mkdir()
        src = HERE.parent / "SandboxScript"
        for name in ("Sandbox.pas", "Sandbox.PrjScr"):
            shutil.copy(src / name, self.dir / name)
        self.project = self.dir / "Sandbox.PrjScr"
        self.original = (self.dir / "Sandbox.pas").read_text(encoding="utf-8")
        self.dispatched = []

    def tearDown(self):
        shutil.rmtree(self.dir)


class ProbeTest(SandboxCopy):
    def test_probe_runs_injected_body_and_restores_template(self):
        seen = {}

        def launch(exe, process):
            seen["body"] = (self.dir / "Sandbox.pas").read_text(encoding="utf-8")
            seen["process"] = process
            (self.exchange / "sandbox_result.json").write_text('{"probe": "alive"}')

        self.assertTrue(ag.probe("X2.EXE", self.project, self.exchange, timeout=2,
                                 launch=launch))
        self.assertIn("'{\"probe\": \"alive\"}'", seen["body"])
        self.assertIn(str(self.project), seen["process"])
        self.assertEqual((self.dir / "Sandbox.pas").read_text(encoding="utf-8"), self.original)

    def test_probe_times_out_when_nothing_runs(self):
        self.assertFalse(ag.probe("X2.EXE", self.project, self.exchange, timeout=1,
                                  launch=lambda exe, p: None))
        self.assertEqual((self.dir / "Sandbox.pas").read_text(encoding="utf-8"), self.original)

    def test_probe_body_passes_preflight(self):
        import script_preflight as sp
        self.assertTrue(sp.check(ag.PROBE_BODY)["ok"])


class RecoverTest(SandboxCopy):
    def run_recover(self, kind, probes, keys=(True, "sent")):
        results = list(probes)
        calls = {"keys": 0}

        def keys_fn():
            calls["keys"] += 1
            return keys

        report = ag.recover("X2.EXE", self.project, self.exchange, state_fn=state(kind),
                            launch=lambda exe, p: self.dispatched.append(p),
                            prober=lambda: results.pop(0), keys_fn=keys_fn,
                            closer=lambda: 0, log=None, settle=0)
        return report, calls

    def test_dead_altium_is_reported_not_started(self):
        report, _ = self.run_recover("not_running", [])
        self.assertFalse(report["recovered"])
        self.assertEqual(report["altium"], "not_running")
        self.assertEqual(self.dispatched, [])

    def test_hung_altium_gets_nothing(self):
        report, _ = self.run_recover("hung", [])
        self.assertEqual(report["altium"], "hung")
        self.assertEqual(self.dispatched, [])

    def test_stop_then_probe(self):
        ag.mark_wedged("t", "unit", exchange=self.exchange, pids=[4242])
        report, calls = self.run_recover("running", [True])
        self.assertTrue(report["recovered"])
        self.assertEqual(report["method"], "EditScript:Stop")
        self.assertEqual(self.dispatched, ["EditScript:Stop"])
        self.assertEqual(calls["keys"], 0)
        self.assertIsNone(ag.read_wedge(self.exchange))

    def test_keys_fallback(self):
        report, calls = self.run_recover("running", [False, True])
        self.assertTrue(report["recovered"])
        self.assertEqual(report["method"], "Ctrl+F3")
        self.assertEqual(calls["keys"], 1)

    def test_keys_not_sent_means_no_second_probe(self):
        report, _ = self.run_recover("running", [False], keys=(False, "no frame"))
        self.assertFalse(report["recovered"])
        self.assertEqual(ag.read_wedge(self.exchange)["source"], "recover")

    def test_still_wedged_is_recorded(self):
        report, _ = self.run_recover("running", [False, False])
        self.assertFalse(report["recovered"])
        self.assertIn("restart", report["error"])
        self.assertIsNotNone(ag.read_wedge(self.exchange))


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.opened = []

    def call(self, frames, focus_ok=True):
        return ag.stop_via_keys("X2.EXE", Path("C:/x/Sandbox.pas"), [4242],
                                launch=lambda exe, p: self.opened.append(p),
                                windows_fn=lambda pids: frames,
                                focus=lambda h: focus_ok, keys=lambda: self.sent.append(1),
                                settle=0)

    def test_opens_the_script_first(self):
        self.call([])
        self.assertIn("WorkspaceManager:OpenObject", self.opened[0])
        self.assertIn("Sandbox.pas", self.opened[0])

    def test_no_script_frame_no_keys(self):
        ok, info = self.call([(1, 4242, "TFrame", "Altium Designer - board.PcbDoc", False)])
        self.assertFalse(ok)
        self.assertEqual(self.sent, [])

    def test_focus_refused_no_keys(self):
        ok, _ = self.call([(1, 4242, "TFrame", "Sandbox.pas - Altium Designer", False)],
                          focus_ok=False)
        self.assertFalse(ok)
        self.assertEqual(self.sent, [])

    def test_keys_sent_to_script_frame(self):
        ok, info = self.call([(1, 4242, "TFrame", "Sandbox.pas - Altium Designer", False)])
        self.assertTrue(ok)
        self.assertEqual(self.sent, [1])


class DialogTest(unittest.TestCase):
    def test_only_altium_dialogs_are_closed(self):
        closed = []
        wins = [(1, 4242, "#32770", "Error", False),
                (2, 4242, "TMessageForm", "Error", False),
                (3, 4242, "TMessageForm", "Some panel", False)]
        n = ag.close_dialogs([4242], windows_fn=lambda pids: [w for w in wins if w[1] in pids],
                             post=closed.append)
        self.assertEqual(n, 2)
        self.assertEqual(closed, [1, 2])

    def test_no_altium_no_closing(self):
        self.assertEqual(ag.close_dialogs([], windows_fn=lambda p: 1 / 0), 0)


if __name__ == "__main__":
    unittest.main()
