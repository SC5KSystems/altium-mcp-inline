"""Offline DRC tests: one synthetic board per check, plus the DRC results
Altium saved in a fixture.

    python -m unittest discover -s server/tests -p "test_offline_drc.py"
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from offline import fixtures  # noqa: E402
from offline import geom  # noqa: E402
from offline import pcbwrite  # noqa: E402
from offline.drc import DRC, cached_violations, compare_cached  # noqa: E402
from offline.pcbdoc import PcbDoc  # noqa: E402
from offline.synth import Synth  # noqa: E402

try:
    geom.require()
    HAVE_SHAPELY = True
except ImportError:  # pragma: no cover
    HAVE_SHAPELY = False


def board(**extra_rules):
    s = Synth(40, 30)
    for n in ("A", "B", "GND"):
        s.net(n)
    s.rule("Clearance", "Clearance", GAP=0.2)
    s.rule("ShortCircuit", "ShortCircuit", ALLOWED="FALSE")
    s.rule("Width", "Width", MINLIMIT=0.15, MAXLIMIT=1.0, PREFEREDWIDTH=0.25,
           BOTTOMLAYER_MINWIDTH=0.3)
    s.rule("HoleSize", "HoleSize", ABSOLUTEVALUES="TRUE", MINLIMIT=0.25, MAXLIMIT=3.0)
    s.rule("MinimumAnnularRing", "MinimumAnnularRing", MINIMUMRING=0.1)
    s.rule("HoleToHoleClearance", "HoleToHoleClearance", GAP=0.3)
    s.rule("BoardOutlineClearance", "BoardOutlineClearance", GAP=0.3)
    s.rule("SilkToSolderMaskClearance", "SilkToSolderMaskClearance", "IsPad", "All", GAP=0.0,
           MINSILKSCREENTOMASKGAP=0.1)
    s.rule("SolderMaskExpansion", "SolderMaskExpansion", EXPANSION=0.05)
    s.rule("UnRoutedNet", "UnRoutedNet")
    for kind, values in extra_rules.items():
        s.rule(kind, kind, **values)
    return s


def run(s, **kw):
    doc = s.build()
    d = DRC(doc, **kw)
    d.run()
    return d


def kinds(d):
    return sorted(v.kind for v in d.violations)


@unittest.skipUnless(HAVE_SHAPELY, "shapely not installed")
class Checks(unittest.TestCase):
    def test_clean_board_has_no_violations(self):
        s = board()
        s.track("Top Layer", 5, 5, 15, 5, 0.25, net="A")
        s.track("Top Layer", 5, 6, 15, 6, 0.25, net="B")
        self.assertEqual(kinds(run(s)), [])

    def test_clearance_between_nets(self):
        s = board()
        s.track("Top Layer", 5, 5, 15, 5, 0.25, net="A")
        s.track("Top Layer", 5, 5.4, 15, 5.4, 0.25, net="B")
        d = run(s)
        self.assertEqual(kinds(d), ["Clearance"])
        self.assertAlmostEqual(d.violations[0].actual, 0.15, places=4)
        self.assertAlmostEqual(d.violations[0].limit, 0.2, places=4)
        s2 = board()
        s2.track("Top Layer", 5, 5, 15, 5, 0.25, net="A")
        s2.track("Top Layer", 5, 5.4, 15, 5.4, 0.25, net="A")
        self.assertEqual(kinds(run(s2)), [], "same net is never a clearance problem")

    def test_short_circuit(self):
        s = board()
        s.track("Top Layer", 5, 5, 15, 5, 0.25, net="A")
        s.track("Top Layer", 10, 2, 10, 8, 0.25, net="B")
        self.assertEqual(kinds(run(s)), ["Clearance", "ShortCircuit"])

    def test_no_net_copper_touching_is_joined(self):
        s = board()
        s.track("Top Layer", 5, 5, 15, 5, 0.25)
        s.track("Top Layer", 15, 5, 15, 9, 0.25)
        self.assertEqual(kinds(run(s)), [])
        s2 = board()
        s2.track("Top Layer", 5, 5, 15, 5, 0.25)
        s2.track("Top Layer", 5, 5.35, 15, 5.35, 0.25)
        self.assertEqual(kinds(run(s2)), ["Clearance"], "near miss between no-net copper")

    def test_keepout_restrictions(self):
        s = board()
        s.track("Top Layer", 5, 15, 15, 15, 0.25, net="A")
        s.via(20, 15, 0.6, 0.3, net="A")
        s.track("Top Layer", 15, 15, 20, 15, 0.25, net="A")
        s.region("Top Layer", [(8, 13), (22, 13), (22, 17), (8, 17)])
        doc = s.build()
        ko = doc.regions[-1]
        ko.subs[ko._main][2] = 2                   # keep-out flag
        ko.props.set("KEEPOUTRESTRICTIONS", "1")   # vias only
        d = DRC(doc)
        d.run()
        hit = [v for v in d.violations if v.kind == "Clearance"]
        self.assertEqual(len(hit), 1)
        self.assertEqual({o["kind"] for o in hit[0].objects}, {"via", "region"})
        ko.props.set("KEEPOUTRESTRICTIONS", "2")   # tracks only
        d = DRC(doc)
        d.run()
        self.assertEqual({o["kind"] for v in d.violations for o in v.objects}, {"track", "region"})

    def test_width_with_layer_override(self):
        s = board()
        s.track("Top Layer", 5, 5, 15, 5, 0.1, net="A")       # under 0.15
        s.track("Top Layer", 5, 10, 15, 10, 1.5, net="A")     # over 1.0
        s.track("Bottom Layer", 5, 15, 15, 15, 0.25, net="A")  # under the bottom-layer 0.3
        s.track("Top Layer", 5, 20, 15, 20, 0.25, net="B")    # fine
        d = run(s, checks=["Width"])
        self.assertEqual(len(d.violations), 3)
        lim = sorted(round(v.limit, 3) for v in d.violations)
        self.assertEqual(lim, [0.15, 0.3, 1.0])

    def test_holes(self):
        s = board()
        s.via(10, 10, 0.5, 0.2, net="A")      # hole under 0.25; ring 0.15
        s.via(20, 10, 0.4, 0.3, net="A")      # ring 0.05 under 0.1
        s.via(20.5, 10, 0.4, 0.3, net="A")    # holes 0.2 apart, under 0.3
        d = run(s, checks=["HoleSize", "MinimumAnnularRing", "HoleToHoleClearance"])
        self.assertEqual(sorted(v.kind for v in d.violations),
                         ["HoleSize", "HoleToHoleClearance", "MinimumAnnularRing",
                          "MinimumAnnularRing"])

    def test_board_outline(self):
        s = board()
        s.track("Top Layer", 0.2, 5, 0.2, 15, 0.2, net="A")   # 0.1 mm from the left edge
        d = run(s, checks=["BoardOutlineClearance"])
        self.assertEqual(len(d.violations), 1)
        self.assertAlmostEqual(d.violations[0].actual, 0.1, places=4)

    def test_silk_over_pad(self):
        s = board()
        s.component("R1", "R0805", 10, 10)
        s.pad("R1", "1", 10, 10, 1.0, 1.0, net="A")
        s.track("Top Overlay", 9, 10, 11, 10, 0.15)           # straight across the pad
        s.track("Top Overlay", 9, 12, 11, 12, 0.15)           # clear
        d = run(s, checks=["SilkToSolderMaskClearance"])
        self.assertEqual(len(d.violations), 1)

    def test_unrouted(self):
        s = board()
        s.component("R1", "R0805", 10, 10)
        s.pad("R1", "1", 10, 10, 1.0, 1.0, net="A")
        s.pad("R1", "2", 14, 10, 1.0, 1.0, net="A")
        d = run(s, checks=["UnRoutedNet"])
        self.assertEqual(kinds(d), ["UnRoutedNet"])
        s.track("Top Layer", 10, 10, 14, 10, 0.25, net="A")
        self.assertEqual(kinds(run(s, checks=["UnRoutedNet"])), [])

    def test_preset(self):
        s = board()
        s.track("Top Layer", 5, 5, 15, 5, 0.155, net="A")    # passes the board's 0.15
        d = run(s, checks=["Width"], preset="jlcpcb-2oz")
        self.assertEqual([v.rule for v in d.violations], ["dfm:jlcpcb-2oz"])
        with self.assertRaises(ValueError):
            DRC(board().build(), preset="nofab")

    def test_box_limits_the_check(self):
        s = board()
        s.track("Top Layer", 5, 5, 15, 5, 0.25, net="A")
        s.track("Top Layer", 5, 5.4, 15, 5.4, 0.25, net="B")
        self.assertEqual(kinds(run(s, box=(20, 20, 30, 30))), [])
        self.assertEqual(kinds(run(s, box=(0, 0, 8, 8))), ["Clearance"])

    def test_unpoured_polygon_is_repoured(self):
        s = board()
        s.via(10, 10, 0.6, 0.3, net="GND")
        s.track("Top Layer", 15, 5, 15, 25, 0.25, net="A")
        s.polygon("Top Layer", [(2, 2), (30, 2), (30, 28), (2, 28)], net="GND")
        d = run(s, checks=["Clearance"])
        self.assertEqual(d.report()["copper"]["repoured_polygons"], [0])
        self.assertEqual(kinds(d), [], "a fresh pour keeps its clearance")
        d = run(s, checks=["Clearance"], copper="saved")
        self.assertEqual(d.report()["copper"]["repoured_polygons"], [])

    def test_cli(self):
        from offline.__main__ import main
        tmp = Path(tempfile.mkdtemp())
        try:
            s = board()
            s.track("Top Layer", 5, 5, 15, 5, 0.25, net="A")
            s.track("Top Layer", 5, 5.4, 15, 5.4, 0.25, net="B")
            path = tmp / "b.PcbDoc"
            pcbwrite.save(s.build(), path)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["drc", str(path), "--json", "--markers", str(tmp / "m.json")])
            self.assertEqual(rc, 1)
            rep = json.loads(buf.getvalue())
            self.assertEqual(rep["summary"]["by_kind"], {"Clearance": 1})
            marks = json.loads((tmp / "m.json").read_text())
            self.assertEqual(len(marks), 1)
        finally:
            shutil.rmtree(tmp)


@unittest.skipUnless(HAVE_SHAPELY and fixtures.by_name("PCB1.PcbDoc") if hasattr(fixtures, "by_name")
                     else False, "fixtures not available")
class AgainstAltium(unittest.TestCase):
    def test_cached_clearance_results_match(self):
        """issue24847's file holds Altium's own DRC result: two clearance
        violations, keep-out arcs against fiducial pads."""
        f = [f for f in fixtures.boards() if f.parent.name == "issue24847"]
        if not f:
            self.skipTest("issue24847 fixture not cached")
        doc = PcbDoc.open(f[0])
        cached = cached_violations(doc)
        self.assertEqual(len(cached), 2)
        d = DRC(doc, copper="saved")
        d.run()
        cmp_ = compare_cached(d, cached)
        self.assertEqual(cmp_["matched"], 2)
        self.assertEqual(cmp_["missed"], [])
        self.assertEqual(cmp_["ours_not_cached_same_kinds"], 0)


if __name__ == "__main__":
    unittest.main()
