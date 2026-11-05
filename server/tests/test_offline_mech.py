"""The board tools ported from the phase-6 branch: holes, fasteners, edges,
dead rule scopes, pad ties measured from poured copper, the neck finder and
the power-stage calculators. Synthetic boards, plus fixtures where present.

    python -m unittest discover -s server/tests -p "test_offline_mech.py"
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from offline import calc, fixtures  # noqa: E402
from offline.synth import Synth  # noqa: E402

try:
    import shapely  # noqa: F401
    import scipy  # noqa: F401
    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False

HIFIVE = fixtures.by_name("HiFive1.B01", ".PcbDoc")


def mech_board():
    s = Synth(40, 30, layers=2)
    for n in ("GND", "SIG", "VBAT"):
        s.net(n)
    s.rule("Clearance", "Clearance", GAP=0.2)
    s.pad(None, "MH1", 3, 3, 3.6, 3.6, layer="MULTILAYER", shape="round", hole=3.2)
    s.pad(None, "MH2", 20, 15, 6.0, 6.0, layer="MULTILAYER", shape="round", hole=3.2)
    s.track("Top Layer", 3, 6, 15, 6, 0.3, net="SIG")          # 3 mm from MH1's centre
    s.component("J1", "CONN", 1.0, 20)
    s.pad("J1", "1", 0.6, 20, 1.0, 1.0, net="VBAT")            # 0.1 mm inside the edge
    s.pad("J1", "2", -0.8, 23, 1.0, 1.0, net="GND")            # off the board
    s.component("R1", "R0603", 30, 25)
    s.pad("R1", "1", 29.2, 25, 0.8, 0.9, net="SIG")
    s.pad("R1", "2", 30.8, 25, 0.8, 0.9, net="GND")
    s.track("TOPOVERLAY", 38, 10, 42, 10, 0.15)                # crosses the right edge
    s.track("TOPOVERLAY", 45, 12, 46, 12, 0.15)                # off the board
    s.track("TOPOVERLAY", 10, 29.9, 12, 29.9, 0.15)            # too close to the top edge
    return s


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class Mechanical(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = mech_board().build()

    def test_holes(self):
        from offline import mech
        hs = mech.holes(self.doc)
        self.assertEqual({h["pad"] for h in hs}, {"MH1", "MH2"})
        mh1 = next(h for h in hs if h["pad"] == "MH1")
        self.assertAlmostEqual(mh1["centre_to_edge_mm"], 3.0, places=3)
        self.assertAlmostEqual(mh1["hole_edge_to_board_edge_mm"], 1.4, places=3)
        self.assertEqual(len(mech.mounting_holes(self.doc, 3.0)), 2)
        self.assertEqual(mech.mounting_holes(self.doc, 3.5), [])

    def test_fasteners(self):
        from offline import mech
        rows = {(r["x"], r["y"]): r for r in mech.fastener_report(self.doc, "M3-pan")}
        r = rows[(3.0, 3.0)]
        self.assertEqual(r["head_mm"], 5.6)
        self.assertAlmostEqual(r["head_edge_to_board_edge_mm"], 0.2, places=2)
        self.assertAlmostEqual(r["move_inward_mm"], 0.8, places=2)
        self.assertEqual(r["copper_within_head_top"], {})
        r = mech.fastener_report(self.doc, head_mm=8.0)[0]
        self.assertIn("SIG", r["copper_within_head_top"])           # 3 - 0.15 mm < 4 mm
        self.assertAlmostEqual(r["copper_within_head_top"]["SIG"], 2.85, places=2)
        self.assertNotIn("SIG", r["copper_within_head_bottom"])
        with self.assertRaises(ValueError):
            mech.fastener_report(self.doc, "M99")

    def test_edges(self):
        from offline import mech
        rows = {r["ref"]: r for r in mech.edge_items(self.doc, 1.0)}
        self.assertEqual(set(rows), {"J1"})
        self.assertEqual(rows["J1"]["pads_outside_board"], 1)
        silk = sorted(r["why"] for r in mech.outline_clearance(self.doc, 0.2))
        self.assertEqual(silk, ["closer than 0.20 mm", "crosses edge", "outside board"])


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class RuleScopes(unittest.TestCase):
    def test_dead_scopes(self):
        from offline.rules import dead_scopes
        from offline.scope import predicates
        self.assertEqual(predicates("(InNetClass('A')) or IsVia or WithinRoom('B')"),
                         [("InNetClass", "A"), ("WithinRoom", "B")])
        s = Synth(20, 20)
        s.net("VBAT")
        s.net_class("Empty", [])
        s.net_class("Power", ["VBAT"])
        s.net_class("All Differential Pairs", [], kind=6)     # Altium's built-in, empty
        s.rule("Width", "W1", scope1="InNetClass('Missing') or IsVia")
        s.rule("Width", "W2", scope1="InNetClass('Empty')")
        s.rule("Width", "W3", scope1="InNetClass('Power') and not InNet('NOPE')")
        s.rule("Clearance", "C1", scope1="WithinRoom('Nowhere')")
        s.rule("Clearance", "C2", scope1="InDifferentialPairClass('All Differential Pairs')")
        rows = dead_scopes(s.build())
        got = {(r["rule"], r["predicate"]) for r in rows}
        self.assertEqual(got, {("W1", "InNetClass('Missing')"), ("W2", "InNetClass('Empty')"),
                               ("W3", "InNet('NOPE')"), ("C1", "WithinRoom('Nowhere')")})

    @unittest.skipUnless(HIFIVE, "fixture not available")
    def test_fixture(self):
        from offline.pcbdoc import PcbDoc
        from offline.rules import dead_scopes
        rows = dead_scopes(PcbDoc.open(HIFIVE))
        self.assertTrue(rows)
        self.assertTrue(all(r["problem"].startswith("no ") for r in rows))


def tie_board(style):
    s = Synth(30, 20, layers=2)
    s.net("GND")
    s.net("SIG")
    s.rule("Clearance", "Clearance", GAP=0.3)
    s.rule("PolygonConnect", "PolygonConnect", CONNECTSTYLE=style, RELIEFCONDUCTORWIDTH=0.3,
           RELIEFENTRIES="4", POLYGONRELIEFANGLE="90 Angle", AIRGAPWIDTH=0.3)
    s.component("U1", "SOIC", 10, 10)
    s.pad("U1", "1", 10, 10, 1.5, 1.5, net="GND")
    s.component("U2", "SOIC", 20, 10)
    s.pad("U2", "1", 20, 10, 1.5, 1.5, net="GND")
    # SIG on three sides of U2, 0.3 mm from its land: only the bottom joins the pour
    s.track("Top Layer", 21.2, 6, 21.2, 14, 0.3, net="SIG")
    s.track("Top Layer", 18.8, 6, 18.8, 14, 0.3, net="SIG")
    s.track("Top Layer", 17, 11.2, 23, 11.2, 0.3, net="SIG")
    s.polygon("Top Layer", [(1, 1), (29, 1), (29, 19), (1, 19)], net="GND", name="GND_TOP")
    return s


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class PadTies(unittest.TestCase):
    def poured(self, style):
        from offline.pour import PourEngine
        doc = tie_board(style).build()
        eng = PourEngine(doc)
        ed = eng.store(eng.repour_all("clean"), "pour")
        return ed.doc

    def test_relief_measured(self):
        from offline.padties import pad_connections
        rows = {r["ref"]: r for r in pad_connections(self.poured("Relief"), ["GND"])["pads"]}
        u1 = rows["U1"]
        self.assertEqual(u1["style"], "relief")
        self.assertEqual(u1["spokes"], 4)
        for w in u1["spoke_widths_mm"]:
            self.assertAlmostEqual(w, 0.3, delta=0.08)
        self.assertTrue(u1["matches_rule"])

    def test_direct_measured(self):
        from offline.padties import pad_connections
        rows = {r["ref"]: r for r in pad_connections(self.poured("Direct"), ["GND"])["pads"]}
        self.assertEqual(rows["U1"]["style"], "direct")
        self.assertTrue(rows["U1"]["matches_rule"])
        # U2 sits in a pocket the SIG tracks cut: joined on one side only
        self.assertEqual(rows["U2"]["style"], "direct_partial")
        self.assertLess(rows["U2"]["covered_fraction"], 0.5)
        self.assertTrue(rows["U2"]["matches_rule"])

    def test_unknown_net(self):
        from offline.padties import pad_connections
        with self.assertRaises(ValueError):
            pad_connections(tie_board("Direct").build(), ["NOPE"])


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class Neck(unittest.TestCase):
    def board(self):
        s = Synth(60, 30, layers=2, copper_mm=0.07)
        s.net("VBAT")
        s.rule("Clearance", "Clearance", GAP=0.2)
        s.component("J1", "C", 5, 15)
        s.pad("J1", "1", 5, 15, 3, 3, net="VBAT")
        s.component("Q1", "C", 55, 15)
        s.pad("Q1", "D", 55, 15, 3, 3, net="VBAT")
        s.track("Top Layer", 5, 15, 25, 15, 3.0, net="VBAT")
        s.track("Top Layer", 25, 15, 35, 15, 1.0, net="VBAT")
        s.track("Top Layer", 35, 15, 55, 15, 3.0, net="VBAT")
        s.track("Bottom Layer", 5, 15, 20, 15, 2.0, net="VBAT")   # not joined on the bottom
        return s.build()

    def test_neck_width_and_place(self):
        from offline.neck import neck_between
        r = neck_between(self.board(), "VBAT", ["J1"], ["Q1.D"], res=0.1)
        top = r["Top Layer"]
        self.assertAlmostEqual(top["width_mm"], 1.0, delta=0.1)
        self.assertTrue(26.4 <= top["at"][0] <= 33.6, top)
        self.assertGreater(top["ipc2221_a_rise20"], top["ipc2221_a_rise10"])
        self.assertNotIn("Bottom Layer", r, "Q1.D has no copper on the bottom")

    def test_bad_terminals(self):
        from offline.neck import neck_between
        with self.assertRaises(ValueError):
            neck_between(self.board(), "VBAT", ["J1"], ["9,9"])
        with self.assertRaises(ValueError):
            neck_between(self.board(), "NOPE", ["J1"], ["Q1.D"])


class Calculators(unittest.TestCase):
    def test_rl_average_and_high_frequency_limit(self):
        r = calc.rl_ripple(12.0, 1.0, 1e-3, 100e3, 0.25)
        self.assertAlmostEqual(r["avg_a"], 3.0, places=2)
        self.assertLess(r["pp_a"], 0.1)

    def test_rl_low_frequency_swings_most_of_the_range(self):
        r = calc.rl_ripple(13.5, 0.85, 550e-6, 240, 0.3)
        self.assertGreater(r["pp_pct_of_avg"], 150)
        self.assertLess(r["min_a"], 0.5)

    def test_rl_more_frequency_less_ripple(self):
        a = calc.rl_ripple(13.5, 0.85, 550e-6, 2500, 0.5)["pp_a"]
        b = calc.rl_ripple(13.5, 0.85, 550e-6, 5000, 0.5)["pp_a"]
        self.assertLess(b, a)

    def test_thermal_model_reproduces_datasheet_nominal_current(self):
        # the single-channel part's I_L(NOM) is 11 A at 85 C on the JEDEC board
        self.assertAlmostEqual(calc.max_current_for_tj("BTS7008-1EPP", 85.0), 11.0, delta=0.5)

    def test_fast_pwm_at_high_current_is_not_usable(self):
        for part in calc.PROFETS:
            for col in (0, 1, 2):
                self.assertNotEqual(calc.profet_pwm(part, 2500, 12, which=col)["verdict"],
                                    "usable")

    def test_slow_pwm_at_moderate_current_is_usable(self):
        self.assertEqual(calc.profet_pwm("BTS7008-1EPP", 50, 6, which=1)["verdict"], "usable")

    def test_input_clamp_current(self):
        self.assertAlmostEqual(calc.input_network(14.4, 10e3)["clamp_current_ma"], 0.74, places=2)

    def test_pulse_needs_freewheel_for_large_inductance(self):
        r = calc.solenoid_pulse("BTS7008-2EPA", 40, 0.01, l_h=2e-3)
        self.assertTrue(r["within_overload_limit"])
        self.assertTrue(r["needs_external_freewheel"])

    def test_freewheel_diode(self):
        self.assertAlmostEqual(calc.freewheel_diode(0.5, 12.0, 0.5)["p_diode_w"], 3.0)
        self.assertAlmostEqual(calc.freewheel_diode(0.7, 10.0, 0.25, qrr_nc=100, v_bus=14,
                                                    f_hz=2500)["p_recovery_w"], 0.0035, places=4)


if __name__ == "__main__":
    unittest.main()
