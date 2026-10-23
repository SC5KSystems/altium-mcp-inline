"""Tests for the offline analysis package (server/offline). None of these need Altium.

Run from server/:   python -m unittest tests.test_offline -v
Real-file tests run only when ALTIUM_TEST_PCBDOC points at a .PcbDoc; design files are not
kept in this repo, so nothing here names a net or component of a real board.
"""
import math
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from offline import geom, power  # noqa: E402
from offline.pcbdoc import NONE, ObjClass, PcbDoc, to_mm  # noqa: E402
from offline.scope import Ctx, Scope, predicates  # noqa: E402

try:
    import numpy as np
    from offline import pdn, raster
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


class _StubDoc:
    """Just enough of a PcbDoc for the scope evaluator."""

    def __init__(self):
        self.classes = [ObjClass("Power", 0, False, ["NET_A"]), ObjClass("Empty", 3, False, []),
                        ObjClass("Pads", 3, False, ["U1.1"])]
        self.nets = []


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.doc = _StubDoc()
        self.sc = Scope(self.doc)

    def test_boolean_structure(self):
        c = Ctx(self.doc, "pad", net="NET_A", comp="U1", pad="1")
        self.assertTrue(self.sc.matches("InNetClass('Power') or IsVia", c))
        self.assertFalse(self.sc.matches("InNetClass('Power') and IsVia", c))
        self.assertTrue(self.sc.matches("not IsVia", c))
        self.assertTrue(self.sc.matches("(InNet('NET_A')) && !(IsVia)", c))

    def test_pad_class_uses_ref_dot_pad(self):
        self.assertTrue(self.sc.matches("InPadClass('Pads')", Ctx(self.doc, "pad", comp="U1", pad="1")))
        self.assertFalse(self.sc.matches("InPadClass('Pads')", Ctx(self.doc, "pad", comp="U1", pad="2")))

    def test_missing_things_are_false_and_reported(self):
        c = Ctx(self.doc, "pad", net="X")
        self.assertFalse(self.sc.matches("WithinRoom('Nowhere')", c))
        self.assertIn("WithinRoom(Nowhere)", self.sc.unknown)
        self.assertFalse(self.sc.matches("InNetClass('Nope')", c))

    def test_numeric_comparison_does_not_crash(self):
        self.assertFalse(self.sc.matches("(CompPinCount < 5)", Ctx(self.doc, "pad")))

    def test_predicates_listing(self):
        self.assertEqual(predicates("(InNetClass('A')) or IsVia or WithinRoom('B')"),
                         [("InNetClass", "A"), ("IsVia", None), ("WithinRoom", "B")])


class GeomTests(unittest.TestCase):
    def test_area_and_containment(self):
        sq = [(0, 0), (4, 0), (4, 3), (0, 3)]
        self.assertAlmostEqual(geom.polygon_area(sq), 12.0)
        self.assertTrue(geom.point_in_polygon((1, 1), sq))
        self.assertFalse(geom.point_in_polygon((5, 1), sq))

    def test_distances(self):
        self.assertAlmostEqual(geom.seg_point_dist((1, 1), (0, 0), (2, 0)), 1.0)
        self.assertAlmostEqual(geom.seg_point_dist((5, 0), (0, 0), (2, 0)), 3.0)
        sq = [(0, 0), (4, 0), (4, 4), (0, 4)]
        self.assertAlmostEqual(geom.point_to_ring_dist((1, 2), sq), 1.0)

    def test_arc_is_counter_clockwise_and_on_radius(self):
        pts = geom.arc_points(0, 0, 2.0, 180, 270)
        self.assertAlmostEqual(pts[0][0], -2.0)
        self.assertAlmostEqual(pts[-1][1], -2.0)
        self.assertTrue(all(abs(math.hypot(x, y) - 2.0) < 1e-9 for x, y in pts))

    def test_units(self):
        self.assertAlmostEqual(to_mm("100mil"), 2.54)
        self.assertAlmostEqual(to_mm("3mm"), 3.0)
        self.assertIsNone(to_mm("nonsense"))


class PowerTests(unittest.TestCase):
    def test_rl_average_and_high_frequency_limit(self):
        r = power.rl_ripple(12.0, 1.0, 1e-3, 100e3, 0.25)
        self.assertAlmostEqual(r["avg_a"], 3.0, places=2)
        self.assertLess(r["pp_a"], 0.1)

    def test_rl_low_frequency_swings_most_of_the_range(self):
        r = power.rl_ripple(13.5, 0.8429, 551.1e-6, 243, 0.3)
        self.assertGreater(r["pp_pct_of_avg"], 150)
        self.assertLess(r["min_a"], 0.5)

    def test_rl_more_frequency_less_ripple(self):
        a = power.rl_ripple(13.5, 0.8429, 551.1e-6, 2500, 0.5)["pp_a"]
        b = power.rl_ripple(13.5, 0.8429, 551.1e-6, 5000, 0.5)["pp_a"]
        self.assertLess(b, a)

    def test_thermal_model_reproduces_datasheet_nominal_current(self):
        # Datasheet: I_L(NOM) = 11 A at TA = 85 C on the JEDEC board for the single-channel part.
        self.assertAlmostEqual(power.max_current_for_tj("BTS7008-1EPP", 85.0), 11.0, delta=0.5)

    def test_profets_cannot_do_2k5_pwm_at_pump_current(self):
        for part in power.PROFETS:
            for col in (0, 1, 2):
                self.assertNotEqual(power.profet_pwm(part, 2500, 12, which=col)["verdict"], "usable")

    def test_profets_are_fine_at_fan_pwm(self):
        self.assertEqual(power.profet_pwm("BTS7008-1EPP", 50, 6, which=1)["verdict"], "usable")

    def test_input_clamp_current(self):
        self.assertAlmostEqual(power.input_network(14.4, 10e3)["clamp_current_ma"], 0.74, places=2)

    def test_solenoid_pulse_needs_freewheel_for_large_inductance(self):
        r = power.solenoid_pulse("BTS7008-2EPA", 40, 0.01, l_h=2e-3)
        self.assertTrue(r["within_overload_limit"])
        self.assertTrue(r["needs_external_freewheel"])


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class RasterAndSolverTests(unittest.TestCase):
    RES = 0.1

    def test_fill_area_with_hole(self):
        g = raster.Grid(0, 0, 12, 12, self.RES)
        m = g.blank()
        raster.fill(g, m, [[(1, 1), (11, 1), (11, 11), (1, 11)], [(4, 4), (8, 4), (8, 8), (4, 8)]])
        self.assertAlmostEqual(m.sum() * self.RES ** 2, 100 - 16, delta=1.0)

    def test_edt_of_a_strip(self):
        m = np.zeros((60, 300), dtype=bool)
        m[5:55, :] = True                     # 50 px = 5 mm wide
        d = pdn.edt(m, self.RES)
        self.assertAlmostEqual(d[30, 150], 2.5, delta=0.15)
        self.assertEqual(d[0, 0], 0.0)

    def test_neck_finds_the_constriction(self):
        g = raster.Grid(0, 0, 40, 12, self.RES)
        m = g.blank()
        raster.fill(g, m, [[(0, 1), (16, 1), (16, 11), (0, 11)]])            # left block, 10 mm wide
        raster.fill(g, m, [[(24, 1), (40, 1), (40, 11), (24, 11)]])          # right block
        raster.fill(g, m, [[(15, 5), (25, 5), (25, 7), (15, 7)]])            # 2 mm neck
        dt = pdn.edt(m, self.RES)
        src, dst = g.blank(), g.blank()
        src[:, :50] = True                    # terminals sit inside the two blocks
        dst[:, -50:] = True
        src &= m
        dst &= m
        dt = np.where(pdn.edt(~(src | dst), self.RES) < 3.0, 1e6, dt)   # ignore terminal zones, as neck_between does
        r = pdn.neck_path(m, dt, src, dst, g)
        self.assertAlmostEqual(r["width_mm"], 2.0, delta=0.25)
        self.assertTrue(15.0 <= r["at"][0] <= 26.0, r["at"])   # the neck spans x = 15..25

    def test_strip_resistance_matches_closed_form(self):
        res, t = 0.1, 70e-6
        H, W = 100, 500                       # 10 mm wide, 50 mm long
        m = np.ones((1, H, W), dtype=bool)
        rs = pdn.rho(20.0) / t
        src = np.zeros_like(m)
        dst = np.zeros_like(m)
        src[0, :, 0] = True
        dst[0, :, -1] = True
        none = (np.array([], dtype=int), np.array([], dtype=int), np.array([]))
        out = pdn.solve_masks(m, np.array([1.0 / rs])[:, None, None], none, src, dst, 10.0, res)
        expected = rs * (0.1 * (W - 1)) / (0.1 * H)
        self.assertAlmostEqual(out["R_ohm"], expected, delta=0.02 * expected)
        self.assertAlmostEqual(out["power_w"], 100.0 * out["R_ohm"], delta=0.02 * 100.0 * expected)

    def test_a_narrower_strip_has_proportionally_more_resistance(self):
        res, t = 0.1, 70e-6
        rs = pdn.rho(20.0) / t
        none = (np.array([], dtype=int), np.array([], dtype=int), np.array([]))
        R = []
        for H in (100, 50):
            m = np.ones((1, H, 300), dtype=bool)
            src = np.zeros_like(m)
            dst = np.zeros_like(m)
            src[0, :, 0] = True
            dst[0, :, -1] = True
            R.append(pdn.solve_masks(m, np.array([1.0 / rs])[:, None, None], none, src, dst, 5.0, res)["R_ohm"])
        self.assertAlmostEqual(R[1] / R[0], 2.0, delta=0.05)

    def test_ipc2221_round_trip(self):
        w = pdn.width_for_current(20.0, 0.07, 30.0)
        self.assertAlmostEqual(pdn.ipc2221_current(w, 0.07, 30.0), 20.0, places=3)
        self.assertGreater(pdn.ipc2221_current(5, 0.07, 30), pdn.ipc2221_current(5, 0.035, 30))


PCB = os.environ.get("ALTIUM_TEST_PCBDOC", "")


@unittest.skipUnless(PCB and Path(PCB).exists(), "set ALTIUM_TEST_PCBDOC to a .PcbDoc to run")
class RealFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = PcbDoc.load(PCB)

    def test_reads_without_warnings(self):
        self.assertEqual(self.doc.warnings, [])

    def test_has_the_basics(self):
        d = self.doc
        self.assertGreater(len(d.nets), 0)
        self.assertGreater(len(d.pads), 0)
        self.assertGreater(len(d.components), 0)
        self.assertGreater(d.board.area, 0)
        self.assertEqual(len(d.board.layers) > 0, True)

    def test_every_reference_resolves(self):
        d = self.doc
        for r in d.regions:
            self.assertTrue(r.poly == NONE or r.poly < len(d.polygons))
        for p in d.pads:
            self.assertTrue(p.comp == NONE or p.comp < len(d.components))
            self.assertTrue(p.net == NONE or p.net < len(d.nets))
        for t in d.tracks:
            self.assertTrue(t.net == NONE or t.net < len(d.nets))

    def test_outline_contains_its_own_holes(self):
        from offline import review
        ring = self.doc.board.ring()
        for h in review.mounting_holes(self.doc):
            self.assertTrue(geom.point_in_polygon((h["x"], h["y"]), ring))

    def test_poured_area_is_no_more_than_outline_area(self):
        # Also guards arc handling: a polygon outline drawn with its arcs on the wrong side
        # comes out with an area that disagrees with the copper Altium actually poured.
        for p in self.doc.polygons:
            self.assertLessEqual(self.doc.poured_area(p.idx), p.outline_area * 1.005 + 0.2, p.name)


if __name__ == "__main__":
    unittest.main()
