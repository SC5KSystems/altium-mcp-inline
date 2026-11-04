"""Power tools: via sizing, thermal screening, the copper planner, fuse and
FET checks. Synthetic boards only (plus published reference figures).

    python -m unittest discover -s server/tests -p "test_offline_power.py"
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from offline import power  # noqa: E402
from offline.synth import Synth  # noqa: E402

try:
    import shapely  # noqa: F401
    import scipy  # noqa: F401
    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False


def power_board():
    """A connector feeding a FET drain past two signal fences, 2 oz, 2 layers."""
    s = Synth(80, 50, layers=2, copper_mm=0.07)
    for n in ("VBAT", "GND", "SIG1", "SIG2", "GATE"):
        s.net(n)
    s.rule("Clearance", "Clearance", GAP=0.3)
    s.rule("Width", "Width", MINLIMIT=0.2, MAXLIMIT=20, PREFEREDWIDTH=0.5)
    s.component("J1", "CONN2", 8, 29)
    s.pad("J1", "1", 8, 25, 3.0, 3.0, layer="MULTILAYER", shape="round", hole=1.5, net="VBAT")
    s.pad("J1", "2", 8, 33, 3.0, 3.0, layer="MULTILAYER", shape="round", hole=1.5, net="GND")
    s.component("Q1", "LFPAK56", 65, 25)
    s.pad("Q1", "D", 65, 25, 5.0, 4.0, net="VBAT")
    s.pad("Q1", "G", 61.5, 21, 0.8, 1.0, net="GATE")
    s.pad("Q1", "S", 61.5, 29, 0.8, 1.0, net="GND")
    s.track("Top Layer", 35, 0.5, 35, 21, 0.25, net="SIG1")
    s.track("Top Layer", 35, 29, 35, 49.5, 0.25, net="SIG1")
    s.track("Bottom Layer", 45, 0.5, 45, 22, 0.25, net="SIG2")
    s.track("Bottom Layer", 45, 28, 45, 49.5, 0.25, net="SIG2")
    for y in (10, 14, 36, 40):
        s.via(50, y, 0.6, 0.3, net="SIG2")
    return s


def fuse_board(width):
    s = Synth(60, 30, layers=2, copper_mm=0.035)
    for n in ("VIN", "VOUT"):
        s.net(n)
    s.rule("Clearance", "Clearance", GAP=0.2)
    s.component("F1", "FUSE", 10, 15)
    s.pad("F1", "1", 8, 15, 1.5, 2.0, net="VIN")
    s.pad("F1", "2", 12, 15, 1.5, 2.0, net="VOUT")
    s.component("L1", "LOAD", 50, 15)
    s.pad("L1", "1", 50, 15, 2.0, 2.0, net="VOUT")
    s.track("Top Layer", 12, 15, 50, 15, width, net="VOUT")
    return s


class Formulas(unittest.TestCase):
    def test_ipc2221_matches_the_conservative_chart(self):
        # J. Olson (2010), reading IPC-2152's conservative chart: 4 A at 10 C
        # needs 300 sq mil; 10 A at 20 C needs 680 sq mil
        self.assertAlmostEqual(power.ipc2221_current(300 / power.SQMIL_PER_MM2, 10), 4.0, delta=0.25)
        self.assertAlmostEqual(power.ipc2221_current(680 / power.SQMIL_PER_MM2, 20), 10.0, delta=0.5)

    def test_onderdonk(self):
        # a 10 mil, 1 oz trace: about 2.6 A for one second, adiabatic
        i2t = power.onderdonk_i2t(0.254 * 0.035)
        self.assertAlmostEqual(i2t ** 0.5, 2.57, delta=0.05)

    def test_via_sizing(self):
        r = power.size_vias(30, hole=0.3, layers=[0.07, 0.07])
        self.assertEqual(r["transfer_a"], 15.0)
        self.assertGreaterEqual(r["vias"] * r["per_via_capacity_a"], 15.0)
        self.assertLessEqual(r["per_via_current_a"], r["per_via_capacity_a"])
        one = power.size_vias(10, hole=0.3)
        self.assertEqual(one["transfer_a"], 10.0)


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class PdnMaps(unittest.TestCase):
    def test_thin_track_density_and_dissipation(self):
        import numpy as np
        from offline.copper import neck
        from offline.pdn import solve
        r = solve(fuse_board(0.5).build(), "VOUT", ["F1.2"], ["L1.1"], 5.0, res=0.25)
        # two cells across: both are copper-edge cells and still carry the current
        self.assertAlmostEqual(neck(r, 1.0)["j_a_per_mm2"], 5.0 / (0.5 * 0.035), delta=3.0)
        heat = sum(float(np.nansum(m["p"])) for m in r.maps.values())
        self.assertAlmostEqual(heat, r.power_w, delta=0.005 * r.power_w)


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class Thermal(unittest.TestCase):
    def test_rise_scales_with_current(self):
        from offline.pdn import solve as pdn_solve
        from offline.thermal import solve
        rises = []
        for amps in (5.0, 10.0):
            doc = fuse_board(2.0).build()
            p = pdn_solve(doc, "VOUT", ["F1.2"], ["L1.1"], amps, res=0.25)
            t = solve(doc, pdn=[p], res=1.0)
            self.assertGreater(t.max_rise, 0.0)
            self.assertAlmostEqual(t.heat_w, p.power_w, delta=0.05 * p.power_w + 1e-3)
            rises.append(t.max_rise)
        # heat goes as I^2; convection gets stronger as it warms, so a bit less than 4x
        self.assertGreater(rises[1] / rises[0], 3.0)
        self.assertLess(rises[1] / rises[0], 4.2)

    def test_component_dissipation(self):
        from offline.thermal import solve
        doc = power_board().build()
        t = solve(doc, powers={"Q1": 2.0}, res=1.0)
        self.assertGreater(t.components["Q1"], t.components["J1"])
        self.assertAlmostEqual(t.heat_w, 2.0)

    def test_against_ipc2152_universal_chart(self):
        from offline.thermal import IPC_CASES, ipc_checks
        rows = ipc_checks(res=1.0)
        self.assertEqual(len(rows), len(IPC_CASES))
        universal = rows[0]
        # screening accuracy: within 35 % of the published rise
        self.assertGreater(universal["ratio"], 0.65, universal)
        self.assertLess(universal["ratio"], 1.35, universal)
        for r in rows:
            if r["kind"].startswith("upper bound"):
                self.assertLess(r["model_rise_c"], r["published_rise_c"], r)


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class Planner(unittest.TestCase):
    def plan(self, **extra):
        p = {"net": "VBAT", "sources": ["J1.1"], "sinks": ["Q1.D"], "current": 30,
             "targets": {"density_a_per_mm2": 40, "drop_mv": 30}, "res": 0.25}
        p.update(extra)
        return p

    def test_meets_targets_and_applies_cleanly(self):
        from offline.copper import Planner as P
        from offline.drc import DRC
        from offline.editplan import apply as apply_plan
        doc = power_board().build()
        edit, report, _ = P(doc, self.plan()).run()
        self.assertTrue(report["met"], report["final"])
        self.assertLessEqual(report["final"]["drop_mv"], 30)
        self.assertLessEqual(report["final"]["max_density_a_per_mm2"], 40)
        vias = [o for o in edit["ops"] if o["op"] == "add_via"]
        self.assertGreaterEqual(len(vias), report["vias_needed"] // 2)
        from shapely.geometry import Point
        from offline import geom
        pads = [geom.pad_shape(p, lay) for p in doc.pads for lay in (1, 32) if p.on_layer(lay)]
        for v in vias:
            ring = Point(v["x"], v["y"]).buffer(v["diameter"] / 2)
            self.assertFalse(any(pd.intersects(ring) for pd in pads), v)
        self.assertIn("thermal", report)
        fresh = power_board().build()
        apply_plan(fresh, edit)
        d = DRC(fresh, checks=["Clearance", "ShortCircuit", "UnRoutedNet", "HoleToHoleClearance",
                               "BoardOutlineClearance"])
        d.run()
        self.assertEqual([v.message for v in d.violations], [])

    def test_unreachable_target_names_the_limit(self):
        from offline.copper import Planner as P
        doc = power_board().build()
        edit, report, _ = P(doc, self.plan(targets={"density_a_per_mm2": 5},
                                           max_iterations=6)).run()
        self.assertFalse(report["met"])
        self.assertIn("limiting_obstacle", report)
        self.assertTrue(edit["ops"], "the best copper found is still returned")

    def test_bad_plans_are_refused(self):
        from offline.copper import PlanError, Planner as P
        doc = power_board().build()
        with self.assertRaises(PlanError):
            P(doc, self.plan(net="NOPE"))
        with self.assertRaises(PlanError):
            P(doc, self.plan(sinks=["J1.2"]))      # a GND pad on a VBAT plan
        with self.assertRaises(PlanError):
            P(doc, self.plan(current=0))


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class FuseAndFets(unittest.TestCase):
    def test_thin_copper_fails_before_the_fuse(self):
        doc = fuse_board(0.3).build()
        r = power.fuse_check(doc, "F1.2", rating=10, i2t=500, thermal=False)
        self.assertFalse(r["ok"])
        self.assertLess(r["copper_melt_i2t_a2s"], 500)

    def test_wide_copper_outlasts_the_fuse(self):
        doc = fuse_board(10.0).build()
        r = power.fuse_check(doc, "F1.2", rating=10, i2t=500, thermal=True)
        self.assertTrue(r["ok"], r["problems"])
        self.assertGreater(r["copper_melt_i2t_a2s"], 500)

    def test_fet_entry_density(self):
        s = power_board()
        s.track("Top Layer", 55, 25, 62.5, 25, 0.5, net="VBAT")   # a thin feed into the drain
        doc = s.build()
        rep = power.fet_screen(doc, {"VBAT": 20})
        self.assertEqual(rep["fets"], ["Q1"])
        row = [r for r in rep["pads"] if r["pad"] == "D"][0]
        self.assertEqual(row["verdict"], "fail")
        self.assertGreater(row["entry_density_a_per_mm2"], 100)


if __name__ == "__main__":
    unittest.main()
