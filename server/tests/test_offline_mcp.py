"""The offline_pcb_* MCP tools (server/offline_tools.py), the JSON netlist
and the worked example. Needs neither pywin32 nor mcp; the FastMCP check
runs only where mcp is installed.

    python -m unittest discover -s server/tests -p "test_offline_mcp.py"
"""

import asyncio
import hashlib
import inspect
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import offline_tools as T  # noqa: E402
from offline import eco  # noqa: E402
from offline.sch import Netlist, SchError  # noqa: E402

try:
    import shapely  # noqa: F401
    import scipy  # noqa: F401
    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False

try:
    from mcp.server.fastmcp import FastMCP
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def add_tool(self, fn, name=None, description=None):
        self.tools[name] = (fn, description)


class FakeImage:
    def __init__(self, path=None):
        self.path = path


class Surface(unittest.TestCase):
    def test_names_and_docstrings(self):
        names = [f.__name__ for f in T.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for f in T.TOOLS:
            self.assertTrue(f.__name__.startswith("offline_pcb_"), f.__name__)
            doc = inspect.getdoc(f) or ""
            self.assertIn("Use ", doc, f"{f.__name__}: say when to use it")
            for p in inspect.signature(f).parameters:
                self.assertFalse(p.startswith("_"), f"{f.__name__}({p})")

    def test_no_clash_with_the_altium_tools(self):
        text = (HERE.parent / "main.py").read_text(encoding="utf-8")
        live = set(re.findall(r"@mcp\.tool\(\)\s*\nasync def (\w+)\(", text))
        self.assertGreater(len(live), 40)
        self.assertFalse(live & {f.__name__ for f in T.TOOLS})
        self.assertIn("offline_tools.register(mcp)", text)
        self.assertNotIn("import win32", (HERE.parent / "offline_tools.py").read_text())

    def test_register_wraps_every_tool(self):
        m = FakeMCP()
        names = T.register(m)
        self.assertEqual(names, [f.__name__ for f in T.TOOLS])
        for f in T.TOOLS:
            tool, desc = m.tools[f.__name__]
            self.assertTrue(inspect.iscoroutinefunction(tool))
            self.assertEqual(desc, inspect.cleandoc(f.__doc__))
            a, b = inspect.signature(tool), inspect.signature(f)
            self.assertEqual(list(a.parameters), list(b.parameters))
            want = inspect.Signature.empty if "show" in b.parameters else str
            self.assertIs(a.return_annotation, want)

    def test_can_be_disabled(self):
        import os
        os.environ["ALTIUM_MCP_OFFLINE_TOOLS"] = "0"
        try:
            self.assertEqual(T.register(FakeMCP()), [])
        finally:
            del os.environ["ALTIUM_MCP_OFFLINE_TOOLS"]

    def test_errors_come_back_named(self):
        m = FakeMCP()
        T.register(m)
        tool = m.tools["offline_pcb_info"][0]
        missing = str(Path(tempfile.gettempdir()) / "no" / "such" / "board.PcbDoc")
        r = json.loads(asyncio.run(tool(board=missing, limit=5)))
        self.assertFalse(r["ok"])
        self.assertIn("board not found", r["error"])
        self.assertEqual(r["tool"], "offline_pcb_info")
        r = json.loads(asyncio.run(tool(board="relative/board.PcbDoc", limit=5)))
        self.assertIn("absolute path", r["error"])
        r = json.loads(asyncio.run(m.tools["offline_pcb_via_array"][0](
            current=30, hole=0.3, plating=0.025, length=1.6, rise_c=10.0, layers=None,
            margin=1.25)))
        self.assertTrue(r["ok"])
        self.assertGreater(r["vias"], 0)

    def test_clean(self):
        out = T._clean({"a": float("nan"), "b": {1, 2}, (1, 2): Path("x"), "c": [float("inf")]})
        self.assertEqual(out, {"a": None, "b": [1, 2], "(1, 2)": "x", "c": [None]})
        json.dumps(out, allow_nan=False)


class OutputPaths(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.src = self.dir / "in.PcbDoc"
        self.src.write_bytes(b"x")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_refusals(self):
        with self.assertRaisesRegex(T.ToolError, "over an input"):
            T._out(self.src, [self.src])
        with self.assertRaisesRegex(T.ToolError, "over an input"):
            T._out(str(self.dir / "." / "in.PcbDoc"), [str(self.src)])
        for bad in ("N:\\libs\\x.PcbDoc", "n:/x.png", "N:x.json"):
            with self.assertRaisesRegex(T.ToolError, "N: drive"):
                T._out(bad)
            with self.assertRaisesRegex(T.ToolError, "N: drive"):
                T._out_dir(bad)
        other = self.dir / "other.PcbDoc"
        other.write_bytes(b"y")
        with self.assertRaisesRegex(T.ToolError, "overwrite=true"):
            T._out(other)
        self.assertEqual(T._out(other, overwrite=True), other)
        png = self.dir / "a.png"
        png.write_bytes(b"z")
        self.assertEqual(T._out(png), png, "pictures and reports may be replaced")
        with self.assertRaisesRegex(T.ToolError, "folder"):
            T._out(self.dir)
        made = T._out(self.dir / "new" / "deeper" / "b.json")
        self.assertTrue(made.parent.is_dir())
        for rel in ("out.png", "sub/out.PcbDoc"):
            with self.assertRaisesRegex(T.ToolError, "absolute"):
                T._out(rel)
        with self.assertRaisesRegex(T.ToolError, "absolute"):
            T._out_dir("scripts")
        self.assertFalse(T._protected(Path("Notes/N:.txt")))


class JsonNetlist(unittest.TestCase):
    def test_round_trip_and_errors(self):
        nl = Netlist.from_dict({"parts": {"J1": {"footprint": "CONN2", "unique_id": "AAAAAAAA"},
                                          "R1": {"footprint": "R0603", "pins": ["1", "2", "3"]}},
                                "nets": {"VBAT": ["J1.1", ["R1", "1"]], "GND": ["J1.2"]}})
        self.assertEqual(nl.nets["VBAT"], {("J1", "1"), ("R1", "1")})
        self.assertEqual(nl.parts["J1"]["pins"], ["1", "2"])
        self.assertEqual(nl.parts["R1"]["pins"], ["1", "2", "3"])
        self.assertEqual(nl.parts["J1"]["unique_ids"], ["AAAAAAAA"])
        again = Netlist.from_dict(nl.to_dict())
        self.assertEqual(again.nets, nl.nets)
        self.assertEqual(again.parts["R1"]["pins"], nl.parts["R1"]["pins"])
        self.assertIs(eco.load_netlist(nl.to_dict())[1], None)
        for bad, msg in (({"parts": {}}, "needs"),
                         ({"parts": {}, "nets": {"A": ["U9.1"]}}, "not in 'parts'"),
                         ({"parts": {"U1": {}}, "nets": {"A": ["U1"]}}, "DES.PIN"),
                         ({"parts": {"U1": {}}, "nets": {"A": ["U1.1"], "B": ["U1.1"]}},
                          "both")):
            with self.assertRaisesRegex(SchError, msg):
                Netlist.from_dict(bad)


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class WorkedExample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from offline import example
        cls.dir = Path(tempfile.mkdtemp())
        lines = []
        cls.steps = example.run(cls.dir, say=lines.append)
        cls.lines = lines

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_story(self):
        s = self.steps
        self.assertEqual([r["designator"] for r in s["eco_preview"]["parts"]["remove"]], ["R2"])
        self.assertEqual(s["eco_preview"]["nets"]["merges"][0]["into"], "VBAT")
        self.assertEqual(s["eco_apply"]["ops"], 2)
        self.assertEqual(s["eco_apply"]["problems"], [])
        gaps = s["gaps"]["gaps"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["net"], "VBAT")
        self.assertAlmostEqual(gaps[0]["gap_mm"], 4.85, places=2)
        cu = s["copper_plan"]
        self.assertTrue(cu["met"], cu["final"])
        self.assertLessEqual(cu["final"]["drop_mv"], 30)
        self.assertGreater(cu["final"]["vias"], 0)
        self.assertEqual(s["drc"]["summary"]["violations"], 0, s["drc"]["violations"])
        c = s["connectivity"]["summary"]
        self.assertEqual((c["unrouted_connections"], c["shorts"], c["dead_copper_islands"]),
                         (0, 0, 0))
        self.assertEqual(len(s["pour"]["polygons"]), 1)
        for k in ("final", "before", "after", "diff", "copper_plan", "eco_plan", "netlist"):
            self.assertTrue(Path(s["files"][k]).stat().st_size > 0, k)

    def test_tools_keep_inputs(self):
        f = self.steps["files"]
        before = sha(f["final"])
        for fn, kw in ((T.offline_pcb_repour, {"out": f["final"]}),
                       (T.offline_pcb_apply_plan, {"plan": {"version": 1, "ops": []},
                                                   "out": f["final"], "overwrite": True}),
                       (T.offline_pcb_render, {"out": f["final"]})):
            r = T.call(fn, board=f["final"], **kw)
            self.assertFalse(r["ok"])
            self.assertIn("input", r["error"])
        self.assertEqual(sha(f["final"]), before)

    def test_render_answers_with_the_image(self):
        f = self.steps["files"]
        res = T.call(T.offline_pcb_render, board=f["final"], out=str(self.dir / "small.png"),
                     width=300)
        out = T._respond(res, FakeImage)
        self.assertIsInstance(out, list)
        self.assertEqual(json.loads(out[0])["image"], str(self.dir / "small.png"))
        self.assertEqual(out[1].path, str(self.dir / "small.png"))
        self.assertIsInstance(T._respond(dict(res, show=False), FakeImage), str)
        svg = T.call(T.offline_pcb_render, board=f["final"], out=str(self.dir / "b.svg"))
        self.assertIsInstance(T._respond(svg, FakeImage), str)

    def test_dry_run_and_live_scripts(self):
        f = self.steps["files"]
        plan = {"version": 1, "ops": [{"op": "rename_net", "from": "GATE", "to": "GATE_DRV"}]}
        r = T.call(T.offline_pcb_apply_plan, board=f["final"], plan=json.dumps(plan))
        self.assertTrue(r["ok"] and r["dry_run"], r)
        r = T.call(T.offline_pcb_live_scripts, board=f["final"], plan=plan,
                   board_path="C:\\scratch\\b.PcbDoc", out_dir=str(self.dir / "live"))
        self.assertTrue(r["ok"], r)
        self.assertTrue((self.dir / "live" / "manifest.json").exists())

    @unittest.skipUnless(HAVE_MCP, "mcp not installed")
    def test_fastmcp(self):
        m = FastMCP("offline-test")
        T.register(m)

        async def go():
            listed = {t.name: t for t in await m.list_tools()}
            self.assertEqual(set(listed), {f.__name__ for f in T.TOOLS})
            out = await m.call_tool("offline_pcb_info", {"board": self.steps["files"]["final"]})
            return json.loads(out[0].text)
        r = asyncio.run(go())
        self.assertTrue(r["ok"])
        self.assertEqual(r["summary"]["components"], 5)


if __name__ == "__main__":
    unittest.main()
