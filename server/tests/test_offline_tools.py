"""The small tools: membership audit script, notch finder, power review,
blank board, stack check, assembly page, Gerber/drill read-back, 3D model
sanity, process probe. Synthetic boards, plus fixtures where present.

    python -m unittest discover -s server/tests -p "test_offline_tools.py"
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "dev"))

from offline import fixtures, pcbwrite, records as R  # noqa: E402
from offline.synth import Synth  # noqa: E402

try:
    import shapely  # noqa: F401
    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False

HIFIVE = fixtures.by_name("HiFive1.B01", ".PcbDoc")


def silk(s, des, pts, w=0.15):
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        s._later(lambda d, a=(x1, y1, x2, y2): pcbwrite.add(
            d, R.track(d, "TOPOVERLAY", a[0], a[1], a[2], a[3], w, component=des)))


class MembershipAudit(unittest.TestCase):
    def test_scripts_pass_preflight(self):
        from offline import audit
        plain = audit.membership_script("C:\\boards\\x.PcbDoc")
        fix = audit.membership_script("C:\\boards\\x.PcbDoc", fix=True, limit=10)
        self.assertIn("GroupIterator_Create", plain)
        self.assertNotIn("RemovePCBObject", plain)
        self.assertNotIn("AddPCBObject", plain)
        self.assertIn("RemovePCBObject", fix)
        self.assertIn("(I1 < 1000)", fix, "a removal that does not take cannot loop for ever")
        self.assertIn("if B1 < 10 then", fix)
        self.assertNotIn("DoFileSave", fix)


@unittest.skipUnless(HAVE, "shapely not installed")
class Notches(unittest.TestCase):
    def test_seam_found_and_merged(self):
        from offline import notch
        from offline.editplan import apply as apply_plan
        s = Synth(40, 30, layers=2)
        s.net("GND")
        s.net("SIG")
        s.rule("Clearance", "Clearance", GAP=0.2)
        s.polygon("Bottom Layer", [(2, 2), (19.85, 2), (19.85, 28), (2, 28)], net="GND")
        s.polygon("Bottom Layer", [(20.15, 4), (38, 4), (38, 26), (20.15, 26)], net="GND")
        s.polygon("Bottom Layer", [(25, 27), (38, 27), (38, 29), (25, 29)], net="SIG")
        s.polygon("Top Layer", [(30, 2), (38, 2), (38, 10), (30, 10)], net="GND")
        doc = s.build()
        found = notch.find(doc, 0.5)
        self.assertEqual(len(found), 1, "same net, same layer, within the gap only")
        f = found[0]
        self.assertAlmostEqual(f["gap_mm"], 0.3, places=3)
        self.assertAlmostEqual(f["seam"]["length_mm"], 22.0, delta=0.05)
        self.assertEqual(len(f["notch_tips"]), 2)
        plan = {"version": 1, "ops": f["ops"]}
        apply_plan(doc, plan)
        gnd = [p for p in doc.polygons if doc.nets[p.net].name == "GND" and p.layer == 32]
        self.assertEqual(len(gnd), 1)
        self.assertEqual(notch.find(doc, 0.5), [])


@unittest.skipUnless(HAVE, "shapely not installed")
class PowerReview(unittest.TestCase):
    def board(self):
        s = Synth(50, 40, layers=2, copper_mm=0.07)
        for n in ("VBAT", "GND", "SIG"):
            s.net(n)
        s.rule("Clearance", "Clearance", GAP=0.2)
        s.rule("PolygonConnect", "PolygonConnect", CONNECTSTYLE="Relief", RELIEFCONDUCTORWIDTH=0.3,
               RELIEFENTRIES="4", AIRGAPWIDTH=0.25, POLYGONRELIEFANGLE="90 Angle")
        s.polygon("Top Layer", [(5, 5), (30, 5), (30, 30), (5, 30)], net="VBAT")
        s.component("Q1", "LFPAK56", 15, 15)
        s.pad("Q1", "1", 15, 15, 4.0, 4.0, net="VBAT")
        s.via(20, 20, 0.6, 0.3, net="VBAT")
        s.track("Top Layer", 30, 15, 45, 15, 0.3, net="VBAT")
        s.component("C1", "C0603", 48.6, 35)
        s.pad("C1", "1", 48.6, 34.2, 0.8, 0.9, net="SIG")
        s.pad("C1", "2", 48.6, 35.8, 0.8, 0.9, net="GND")
        s.component("D1", "SOD123", 40, 25)
        s.pad("D1", "1", 38.4, 25, 0.9, 1.2, net="SIG")
        s.pad("D1", "2", 41.6, 25, 0.9, 1.2, net="GND")
        silk(s, "D1", [(37.5, 24), (42.5, 24), (42.5, 26), (37.5, 26), (37.5, 24)])
        s.component("D2", "SOD123", 40, 10)
        s.pad("D2", "1", 38.4, 10, 0.9, 1.2, net="SIG")
        s.pad("D2", "2", 41.6, 10, 0.9, 1.2, net="GND")
        silk(s, "D2", [(37.5, 9), (42.5, 9), (42.5, 11), (37.5, 11), (37.5, 9)])
        silk(s, "D2", [(37.3, 9), (37.3, 11)], w=0.3)                  # the cathode bar
        s.component("TP1", "TESTPOINT", 25, 35)
        s.pad("TP1", "1", 25, 35, 1.0, 1.0, net="GND")
        return s.build()

    def test_review(self):
        from offline.review import review
        rep = review(self.board(), {"VBAT": 20.0, "GND": 5.0}, edge=1.0)
        j = [r for r in rep["joins"] if r["pad"] == "Q1.1"]
        self.assertEqual(len(j), 1)
        self.assertEqual(j[0]["spokes"], 4)
        self.assertAlmostEqual(j[0]["spoke_copper_mm2"], 4 * 0.3 * 0.07, places=5)
        self.assertGreater(j[0]["spoke_density_a_per_mm2"], 200)
        v = [r for r in rep["vias"] if r["net"] == "VBAT"][0]
        self.assertFalse(v["ok"], "one 0.3 mm via for 20 A")
        n = [r for r in rep["narrowest"] if r["net"] == "VBAT"][0]
        self.assertAlmostEqual(n["width_mm"], 0.3)
        self.assertFalse(n["ok"])
        self.assertEqual([r["component"] for r in rep["board_edge"]], ["C1"])
        self.assertEqual([r["net"] for r in rep["test_points"]], ["VBAT"])
        self.assertEqual([r["component"] for r in rep["polarity"]], ["D1"],
                         "a symmetric box shows no polarity; a cathode bar does")


@unittest.skipUnless(HAVE, "shapely not installed")
class BlankBoard(unittest.TestCase):
    def test_blank_from_template(self):
        from offline import blank
        from offline.pcbdoc import PcbDoc
        tmp = Path(tempfile.mkdtemp())
        try:
            s = Synth(40, 30, layers=2)
            s.net("GND")
            s.rule("Clearance", "Clearance", GAP=0.2)
            s.component("U1", "SOIC", 10, 10)
            s.pad("U1", "1", 10, 10, 1, 1, net="GND")
            s.track("Top Layer", 1, 1, 9, 9, 0.3, net="GND")
            s.polygon("Top Layer", [(20, 2), (30, 2), (30, 12), (20, 12)], net="GND")
            s.net_class("PWR", ["GND"])
            tpl = tmp / "template.PcbDoc"
            s.save(tpl)
            before = tpl.read_bytes()
            outline = blank.rect(60, 40, 0, 0, corner=2)
            ed, rep = blank.blank(tpl, outline, holes=[(3, 3, 3.2)], plated_holes=[(57, 37, 3.2, 6)])
            out = tmp / "new.PcbDoc"
            ed.save(out)
            self.assertEqual(tpl.read_bytes(), before, "the template is never written")
            doc = PcbDoc.open(out)
            self.assertEqual((len(doc.components), len(doc.nets), len(doc.tracks), len(doc.polygons)),
                             (0, 0, 0, 0))
            self.assertEqual(len(doc.pads), 2)
            self.assertEqual(sorted(p.plated for p in doc.pads), [False, True])
            self.assertEqual(len(doc.rules), 1)
            pts = doc.outline()
            self.assertEqual(len(pts), 8)
            self.assertAlmostEqual(max(p[0] for p in pts), 60.0, places=3)
            self.assertEqual(pcbwrite.check_integrity(doc), [])
            with self.assertRaises(Exception):
                blank.blank(tpl, [(0, 0), (1, 1)])
        finally:
            shutil.rmtree(tmp)


@unittest.skipUnless(HAVE, "shapely not installed")
class StackCheck(unittest.TestCase):
    @staticmethod
    def board(offset=(0, 0), turned=False, mirror=False, nets=None):
        s = Synth(60, 40, layers=2)
        for n in ("GND", "VBAT", "SIG"):
            s.net(n)
        s.component("J", "HDR2X5", 30, 20)
        for k in range(10):
            col, row = k // 2, k % 2
            x, y = 25 + col * 2.54, 19 + row * 2.54
            if turned:
                x, y = 25 + (4 - col) * 2.54, 19 + (1 - row) * 2.54
            if mirror:
                x = 60 - x
            net = (nets or {}).get(k + 1, "GND" if (k + 1) % 2 else "SIG")
            s.pad("J", str(k + 1), x + offset[0], y + offset[1], 1.7, 1.7, layer="MULTILAYER",
                  shape="round", hole=1.0, net=net)
        s.component("H1", "HOLE", 5, 5)
        s.pad("H1", "1", 5 + offset[0], 5 + offset[1], 3.2, 3.2, layer="MULTILAYER",
              shape="round", hole=3.0)
        return s.build()

    def test_cases(self):
        from offline import stack
        a = self.board()
        r = stack.check(a, self.board((7.5, -3)), [("J", "J")])
        self.assertTrue(r["ok"], r["problems"])
        self.assertEqual(r["transform"]["shift_mm"], [-7.5, 3.0])
        self.assertEqual(r["holes"][0]["offset_mm"], 0.0)
        r = stack.check(a, self.board((7.5, -3), mirror=True), [("J", "J")])
        self.assertTrue(any("mirrored" in p for p in r["problems"]))
        r = stack.check(a, self.board((7.5, -3), turned=True), [("J", "J")])
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("notes"), "a turned connector is named")
        r = stack.check(a, self.board((7.5, -3), nets={2: "VBAT"}), [("J", "J")])
        self.assertEqual(r["problems"], ["J.2 (SIG) meets J.2 (VBAT): power meets a non-power net"])


@unittest.skipUnless(HAVE, "shapely not installed")
class AssemblyPage(unittest.TestCase):
    def test_bom_and_page(self):
        from offline import assembly
        bom = assembly.read_bom("Item;Designator;Value;Footprint;Qty\n1;R1-R3;10k;0402;3\n"
                                "2;\"C1, C2\";100n;0402;2\n3;Z9;ghost;X;1\n")
        self.assertEqual(bom[0]["designators"], ["R1", "R2", "R3"])
        self.assertEqual(bom[1]["designators"], ["C1", "C2"])
        s = Synth(30, 20, layers=2)
        s.net("N")
        for i, des in enumerate(("R1", "R2", "R3", "C1", "C2", "U1")):
            s.component(des, "0402", 3 + 4 * i, 10, layer="BOTTOM" if des == "C2" else "TOP")
            s.pad(des, "1", 3 + 4 * i - 0.5, 10, 0.5, 0.6, layer="BOTTOM" if des == "C2" else "TOP")
            s.pad(des, "2", 3 + 4 * i + 0.5, 10, 0.5, 0.6, layer="BOTTOM" if des == "C2" else "TOP")
        html, data = assembly.page(s.build(), bom, "Test")
        self.assertEqual(data["missing_from_board"], ["Z9"])
        lines = {tuple(l_["refs"]): l_ for l_ in data["lines"]}
        self.assertIn(("R1", "R2", "R3"), lines)
        self.assertTrue(lines[("U1",)].get("not_in_bom"))
        self.assertEqual(data["parts"]["C2"]["side"], "B")
        self.assertNotIn("http://", html.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("https://", html)
        self.assertIn("localStorage", html)


GERBER = """%FSLAX26Y26*%
%MOMM*%
%ADD10C,1.000000*%
%ADD11R,0.600000X1.500000*%
D11*
X8000000Y15000000D03*
D10*
X10000000Y10000000D02*
X20000000Y10000000D01*
M02*
"""
DRILL = """M48
;FILE_FORMAT=2:4
METRIC,LZ
T1C0.300
T2C1.000
%
T1
X0140000Y0170000
T2
X0300000Y0150000
M30
"""


@unittest.skipUnless(HAVE, "shapely not installed")
class FabCheck(unittest.TestCase):
    def test_parsers(self):
        from offline import gerber
        g = gerber.parse_gerber(GERBER, "x.GTL")
        self.assertEqual((g["units"], g["format"], g["flashes"], g["draws"]), ("mm", "2.6", 1, 1))
        self.assertEqual(g["box"], [7.7, 9.5, 20.5, 15.75])
        d = gerber.parse_drill(DRILL, "x-Plated.TXT")
        self.assertEqual(d["tools"], {1: 0.3, 2: 1.0})
        self.assertEqual(d["holes"], [(14.0, 17.0, 0.3), (30.0, 15.0, 1.0)])
        self.assertTrue(d["plated"])
        rout = "M48\nMETRIC\nT1C0.800\n%\nT1\nG00X1.0Y2.0\nM15\nG01X3.0Y2.0\nM16\nG05\nM30\n"
        r = gerber.parse_drill(rout, "slots.drl")
        self.assertEqual(r["holes"], [(2.0, 2.0, 0.8)], "a routed slot is one hole at its middle")
        from types import SimpleNamespace as P
        # drill files list a slot under the tool for its width, either field may be shorter
        self.assertEqual(gerber.drill_size(P(hole=2.2, slot_size=0.9, hole_shape="slot")), 0.9)
        self.assertEqual(gerber.drill_size(P(hole=0.8, slot_size=2.0, hole_shape="slot")), 0.8)
        self.assertEqual(gerber.drill_size(P(hole=1.1, slot_size=0.0, hole_shape="round")), 1.1)

    def test_against_a_board(self):
        from offline import gerber
        s = Synth(40, 30, layers=2)
        s.net("A")
        s.component("U1", "X", 8, 15)
        s.pad("U1", "1", 8, 15, 0.6, 1.5, net="A")
        s.component("J1", "H", 30, 15)
        s.pad("J1", "1", 30, 15, 1.7, 1.7, layer="MULTILAYER", shape="round", hole=1.0, net="A")
        s.via(14, 17, 0.6, 0.3, net="A")
        doc = s.build()
        tmp = Path(tempfile.mkdtemp())
        try:
            for ext in (".GTL", ".GBL", ".GTS", ".GBS", ".GTP", ".GKO"):
                (tmp / f"b{ext}").write_text(GERBER)
            # the drill file 5 mm off and mirrored left to right: found anyway
            shifted = DRILL.replace("X0140000Y0170000", "X-0090000Y0220000") \
                .replace("X0300000Y0150000", "X-0250000Y0200000")
            (tmp / "b.TXT").write_text(shifted)
            rep = gerber.check(doc, tmp)
            self.assertEqual(rep["missing"], [])
            self.assertEqual(rep["problems"], [])
            self.assertEqual(rep["drill_alignment"]["matched"], 2)
            self.assertEqual(rep["drill_alignment"]["orientation"], "mirrored left to right")
            (tmp / "b.GTP").unlink()
            (tmp / "b.TXT").write_text(DRILL.replace("T2C1.000", "T2C1.100"))
            rep = gerber.check(doc, tmp)
            self.assertEqual([m["layer"] for m in rep["missing"]], ["paste:top"])
            self.assertTrue(any("1.0 mm holes" in p for p in rep["problems"]))
        finally:
            shutil.rmtree(tmp)

    @unittest.skipUnless(HIFIVE is not None and shutil.which("python3.12"),
                         "needs the HiFive fixture and KiCad's python to plot it")
    def test_kicad_plot_reads_back(self):
        """KiCad's own Gerber X2 and drill output of a fixture: every hole is
        found, at the board origin offset."""
        import subprocess
        from offline import gerber
        from offline.pcbdoc import PcbDoc
        tmp = Path(tempfile.mkdtemp())
        try:
            script = tmp / "plot.py"
            script.write_text(
                "import sys, pcbnew\n"
                "b = pcbnew.LoadBoard(sys.argv[1], pcbnew.IO_MGR.ALTIUM_DESIGNER)\n"
                "pc = pcbnew.PLOT_CONTROLLER(b)\npo = pc.GetPlotOptions()\n"
                "po.SetOutputDirectory(sys.argv[2])\npo.SetUseGerberX2format(True)\n"
                "for lid, n in ((pcbnew.F_Cu, 'F_Cu'), (pcbnew.B_Cu, 'B_Cu'), (pcbnew.Edge_Cuts, 'Edge')):\n"
                "    pc.SetLayer(lid)\n    pc.OpenPlotfile(n, pcbnew.PLOT_FORMAT_GERBER, n)\n"
                "    pc.PlotLayer()\npc.ClosePlot()\n"
                "dw = pcbnew.EXCELLON_WRITER(b)\ndw.SetFormat(True)\n"
                "dw.SetOptions(False, False, b.GetDesignSettings().GetAuxOrigin(), False)\n"
                "dw.CreateDrillandMapFilesSet(sys.argv[2], True, False)\n")
            p = subprocess.run(["python3.12", str(script), str(HIFIVE), str(tmp)],
                               capture_output=True, text=True, timeout=300)
            if not list(tmp.glob("*.drl")):
                self.skipTest("KiCad could not plot: " + p.stderr[-300:])
            rep = gerber.check(PcbDoc.open(HIFIVE), tmp, tol=0.02)
            a = rep["drill_alignment"]
            self.assertEqual(a["matched"], a["file_holes"])
            self.assertEqual(a["file_holes"], a["board_holes"])
            self.assertEqual([r for r in rep["drill_sizes"] if r["board"] != r["files"]], [])
        finally:
            shutil.rmtree(tmp)


STEP_BOX = """ISO-10303-21;
HEADER;
ENDSEC;
DATA;
#1=(CONVERSION_BASED_UNIT('INCH',#2)LENGTH_UNIT()NAMED_UNIT(#3));
#2=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(0.0254),#4);
#4=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT($,.METRE.));
{points}
ENDSEC;
END-ISO-10303-21;
"""


class Models3D(unittest.TestCase):
    def step(self, size_in):
        sx, sy, sz = size_in
        pts, verts = [], []
        k = 10
        for x in (0.0, sx):
            for y in (0.0, sy):
                for z in (0.0, sz):
                    pts.append(f"#{k}=CARTESIAN_POINT('',({x - sx / 2},{y - sy / 2},{z}));")
                    verts.append(f"#{k + 1}=VERTEX_POINT('',#{k});")
                    k += 2
        return STEP_BOX.replace("{points}", "\n".join(pts + verts))

    def test_units(self):
        from offline import models3d as M
        self.assertAlmostEqual(M.length_scale(self.step((1, 1, 1))), 25.4)
        self.assertEqual(M.length_scale("#1=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));"),
                         1.0)
        self.assertEqual(M.length_scale("#1=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT($,.METRE.));"),
                         1000.0)
        allp, verts = M.step_points(self.step((0.2, 0.25, 0.04)))
        self.assertAlmostEqual(float(allp[:, 2].max()), 1.016, places=3)

    def test_on_end_and_fix(self):
        from offline import models3d as M
        # a 5 x 6 x 1.1 mm part modelled Y-up: 5 wide, 1.1 deep, 6 tall
        allp, verts = M.step_points(self.step((5 / 25.4, 1.1 / 25.4, 6 / 25.4)))
        props = {"MODEL.3D.ROTX": "0", "MODEL.3D.ROTY": "0", "MODEL.3D.ROTZ": "0",
                 "MODEL.3D.DZ": "0mil", "MODEL.2D.X": "0mil", "MODEL.2D.Y": "0mil",
                 "OVERALLHEIGHT": "6mm"}
        pads = [(-1.9, -2.5), (1.9, -2.5), (0, 1.8)]
        r = M.check_body(props, (allp, verts), (-2.5, -3.0, 2.5, 3.0), pads, False,
                         "POWERPAK_SO-8")
        self.assertTrue(any("stands on end" in f for f in r["findings"]), r["findings"])
        self.assertTrue(any("far from its class" in f for f in r["findings"]))
        self.assertIn(r["suggest"]["rotx"] % 180, (90.0,))
        self.assertAlmostEqual(r["suggest"]["height_after_mm"], 1.1, places=2)
        self.assertGreaterEqual(r["suggest"]["dz_mm"], 0.0)
        props["MODEL.3D.ROTX"] = str(r["suggest"]["rotx"])
        props["MODEL.3D.DZ"] = f"{r['suggest']['dz_mm']}mm"
        props["OVERALLHEIGHT"] = "1.1mm"
        r2 = M.check_body(props, (allp, verts), (-2.5, -3.0, 2.5, 3.0), pads, False,
                          "POWERPAK_SO-8")
        self.assertEqual(r2["findings"], [])

    @unittest.skipUnless(HAVE and fixtures.by_name("Fastino_Ground_Isolator", ".PcbDoc"),
                         "fixture not available")
    def test_fixture_models_read(self):
        from offline import models3d as M
        from offline.pcbdoc import PcbDoc
        doc = PcbDoc.open(fixtures.by_name("Fastino_Ground_Isolator", ".PcbDoc"))
        rows = M.check_doc(doc, everything=True)
        self.assertGreater(len(rows), 30)
        self.assertEqual([r for r in rows if r.get("findings") or r.get("error")], [])
        by = {r["component"]: r for r in rows}
        self.assertAlmostEqual(by["U202"]["step_height_mm"], 2.01, delta=0.05,
                               msg="a STEP in inches")


class ProcessProbe(unittest.TestCase):
    def test_every_candidate_passes_preflight(self):
        import probe_processes as P
        for n in range(1, len(P.CANDIDATES) + 1):
            text = P.body(n, "C:\\scratch\\x.PcbDoc", "C:\\scratch\\drc.OutJob", "Report Output")
            ok, rep = P.check_body(text)
            self.assertTrue(ok, f"candidate {n}: {rep}")
            self.assertNotIn("DoFileSave", text)


if __name__ == "__main__":
    unittest.main()
