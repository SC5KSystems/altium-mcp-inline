"""Pour engine tests: synthetic boards for each modelled behaviour, the fixture
pours for accuracy, and a large synthetic board for speed.

    python -m unittest discover -s server/tests -p "test_offline_pour.py"
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from offline import fixtures  # noqa: E402
from offline import geom  # noqa: E402
from offline import pcbwrite  # noqa: E402
from offline.pcbdoc import PcbDoc  # noqa: E402
from offline.pour import PourEngine, pieces  # noqa: E402
from offline.synth import Synth, stress_board  # noqa: E402

try:
    sh = geom.require()
except ImportError:  # pragma: no cover
    sh = None


def basic(**poly):
    """One GND pour with a signal track, a relief pad, a direct via, a dead
    pocket and a live pocket."""
    s = Synth(40, 30)
    for n in ("GND", "SIG", "RING"):
        s.net(n)
    s.rule("Clearance", "Clearance", GAP=0.2)
    s.rule("PolygonConnect", "Vias", "IsVia", "All", priority=1, CONNECTSTYLE="Direct")
    s.rule("PolygonConnect", "PolygonConnect", priority=2, CONNECTSTYLE="Relief",
           RELIEFCONDUCTORWIDTH=0.3, RELIEFENTRIES="4", POLYGONRELIEFANGLE="90 Angle",
           AIRGAPWIDTH=0.25)
    s.component("R1", "R0805", 8, 8)
    s.pad("R1", "1", 8, 8, 1.0, 1.0, net="GND")
    s.track("Top Layer", 10, 15, 30, 15, 0.3, net="SIG")
    s.via(20, 25, 0.6, 0.3, net="GND")
    # two closed rings of another net: one empty (dead copper inside), one
    # with a GND via inside (live)
    for cx, cy, via in ((32, 8, False), (32, 22, True)):
        pts = [(cx - 2, cy - 2), (cx + 2, cy - 2), (cx + 2, cy + 2), (cx - 2, cy + 2)]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            s.track("Top Layer", x1, y1, x2, y2, 0.3, net="RING")
        if via:
            s.via(cx, cy, 0.6, 0.3, net="GND")
    # a pinch between two signal vias: 0.4 mm of copper between their gaps
    s.via(25, 6.0, 0.6, 0.3, net="SIG")
    s.via(25, 7.4, 0.6, 0.3, net="SIG")
    settings = dict(remove_dead=True, remove_islands=True, island_area_mm2=0.5,
                    remove_necks=False, neck_width=0.5)
    settings.update(poly)
    s.polygon("Top Layer", [(2, 2), (38, 2), (38, 28), (2, 28)], net="GND", **settings)
    return s.build()


def overlapping(stale=False):
    """GND (pour index 0) and VIN (pour index 1) whose outlines overlap."""
    s = Synth(40, 30)
    for n in ("GND", "VIN"):
        s.net(n)
    s.rule("Clearance", "Clearance", GAP=0.3)
    s.via(5, 5, 0.6, 0.3, net="GND")
    s.via(35, 25, 0.6, 0.3, net="VIN")
    s.polygon("Top Layer", [(2, 2), (24, 2), (24, 28), (2, 28)], net="GND", pour_index=0,
              remove_islands=False)
    s.polygon("Top Layer", [(16, 2), (38, 2), (38, 28), (16, 28)], net="VIN", pour_index=1,
              remove_islands=False)
    if stale:  # VIN's saved copper still covers the overlap
        s.region("Top Layer", [(16, 2), (38, 2), (38, 28), (16, 28)], polygon=1)
    return s.build()


@unittest.skipIf(sh is None, "shapely not installed")
class PourBehaviour(unittest.TestCase):
    def test_clearance_gap_to_other_net(self):
        doc = basic()
        res = PourEngine(doc).pour(0)
        track = [t for t in doc.tracks if t.net_name == "SIG"][0]
        gap = res.copper.distance(geom.track_shape(track))
        self.assertAlmostEqual(gap, 0.2, delta=0.003)

    def test_relief_pad_spokes_and_square_void(self):
        doc = basic()
        eng = PourEngine(doc)
        res = eng.pour(0)
        self.assertEqual(res.relief_pads, 1)
        self.assertEqual(res.spokes, 4)
        pad = [p for p in doc.pads if p.net_name == "GND"][0]
        # a corner of the air gap: 0.25 mm out from the pad's corner, diagonally
        corner = sh.Point(pad.x + 0.5 + 0.2, pad.y + 0.5 + 0.2)
        self.assertFalse(res.copper.contains(corner), "relief gap should keep a square corner")
        # copper reaches the pad along each spoke
        for dx, dy in ((0.6, 0), (-0.6, 0), (0, 0.6), (0, -0.6)):
            self.assertTrue(res.copper.contains(sh.Point(pad.x + dx, pad.y + dy)))

    def test_direct_via_is_merged(self):
        doc = basic()
        res = PourEngine(doc).pour(0)
        self.assertTrue(res.copper.buffer(1e-6).contains(sh.Point(20.4, 25)))

    def test_dead_pocket_removed_live_pocket_kept(self):
        doc = basic()
        res = PourEngine(doc).pour(0)
        self.assertFalse(res.copper.contains(sh.Point(33, 9)), "empty pocket is dead copper")
        self.assertTrue(res.copper.contains(sh.Point(33, 23)), "pocket with a GND via is live")
        self.assertGreaterEqual(res.removed["dead"], 1)
        keep = PourEngine(basic(remove_dead=False)).pour(0)
        self.assertTrue(keep.copper.contains(sh.Point(33, 9)))

    def test_island_area_threshold(self):
        doc = basic(remove_dead=False, island_area_mm2=50.0)
        res = PourEngine(doc).pour(0)
        self.assertFalse(res.copper.contains(sh.Point(33, 9)), "10 mm2 pocket under 50 mm2")
        self.assertGreaterEqual(res.removed["islands"], 1)

    def test_neck_removal(self):
        pinch = sh.Point(25, 6.7)
        wide = PourEngine(basic(remove_necks=True, neck_width=0.3)).pour(0)
        self.assertTrue(wide.copper.contains(pinch), "0.4 mm neck kept under a 0.3 mm limit")
        narrow = PourEngine(basic(remove_necks=True, neck_width=0.5)).pour(0)
        self.assertFalse(narrow.copper.contains(pinch), "0.4 mm neck removed under 0.5 mm")
        self.assertGreater(narrow.removed["necks_area"], 0.0)

    def test_outline_and_board_clip(self):
        doc = basic()
        res = PourEngine(doc).pour(0)
        self.assertTrue(sh.box(2, 2, 38, 28).buffer(1e-6).contains(res.copper))

    def test_pour_order_clean(self):
        doc = overlapping()
        run = PourEngine(doc).repour_all("clean")
        mid = sh.Point(20, 15)
        self.assertTrue(run[0].copper.contains(mid), "lower pour index takes the overlap")
        self.assertFalse(run[1].copper.contains(mid))
        gap = run[0].copper.distance(run[1].copper)
        self.assertAlmostEqual(gap, 0.3, delta=0.003)

    def test_stale_copper_leaves_a_dent(self):
        doc = overlapping(stale=True)
        eng = PourEngine(doc)
        found, clean, seq = eng.dents()
        self.assertEqual([d["polygon"] for d in found], [0, 1])
        self.assertFalse(seq[0].copper.contains(sh.Point(20, 15)), "GND dented by stale VIN")
        self.assertTrue(clean[0].copper.contains(sh.Point(20, 15)))
        self.assertGreater(found[0]["missing_mm2"], 50.0)

    def test_effective_copper_repours_only_unpoured(self):
        doc = overlapping(stale=True)
        copper, used = PourEngine(doc).effective_copper()
        self.assertEqual(used, {1: "saved", 0: "repoured"})
        copper, used = PourEngine(doc).effective_copper("repour")
        self.assertEqual(set(used.values()), {"repoured"})


@unittest.skipIf(sh is None, "shapely not installed")
class PourStore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_store_as_pour_reads_back(self):
        doc = basic()
        eng = PourEngine(doc)
        run = eng.repour_all("clean")
        ed = eng.store(run, "pour")
        out = self.dir / "poured.PcbDoc"
        ed.save(out)
        back = PcbDoc.open(out)
        self.assertEqual(pcbwrite.check_integrity(back), [])
        got = PourEngine(back).saved_copper()[0]
        self.assertLess(got.symmetric_difference(run[0].copper).area, 1e-3)
        self.assertEqual(len([r for r in back.regions if r.polygon == 0]), len(run[0].parts))

    def test_store_as_solid_removes_polygon(self):
        doc = basic()
        eng = PourEngine(doc)
        run = eng.repour_all("clean")
        ed = eng.store(run, "solid")
        out = self.dir / "solid.PcbDoc"
        ed.save(out)
        back = PcbDoc.open(out)
        self.assertEqual(len(back.polygons), 0)
        regs = [r for r in back.regions if r.kind == "copper"]
        self.assertEqual(len(regs), len(run[0].parts))
        self.assertTrue(all(r.net_name == "GND" for r in regs))

    def test_store_unpoured(self):
        doc = overlapping(stale=True)
        eng = PourEngine(doc)
        found, clean, _ = eng.dents()
        ed = eng.store({d["polygon"]: clean[d["polygon"]] for d in found}, "unpoured")
        out = self.dir / "unpoured.PcbDoc"
        ed.save(out)
        back = PcbDoc.open(out)
        self.assertEqual([r for r in back.regions if r.polygon is not None], [])

    def test_cli(self):
        from offline.__main__ import main
        doc = basic()
        src = self.dir / "in.PcbDoc"
        pcbwrite.save(doc, src)
        out = self.dir / "out.PcbDoc"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["pour", str(src), "--json", "--compare", "-o", str(out)])
        self.assertEqual(rc, 0)
        rep = json.loads(buf.getvalue())
        self.assertEqual(len(rep["polygons"]), 1)
        self.assertEqual(rep["polygons"][0]["spokes"], 4)
        self.assertTrue(out.exists())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["pour", str(src), "-o", str(src)]), 2)


@unittest.skipUnless(sh is not None and fixtures.boards(), "fixture boards not available")
class PourAccuracy(unittest.TestCase):
    """Our pour of each fixture polygon against the copper Altium saved."""

    def test_fixture_pours_match(self):
        tot = {"all": [0.0, 0.0, 0.0], "kicad": [0.0, 0.0, 0.0]}
        worst = []
        for f in fixtures.boards():
            doc = PcbDoc.open(f)
            eng = PourEngine(doc)
            groups = ["all"] + (["kicad"] if "kicad" in f.parts else [])
            for i, res in eng.repour_all("saved").items():
                c = eng.compare(res)
                for g in groups:
                    tot[g][0] += c["saved_mm2"]
                    tot[g][1] += c["ours_mm2"]
                    tot[g][2] += c["overlap_mm2"]
                if c["iou"] is not None:
                    worst.append((c["iou"], f"{f.parent.name}/{f.stem}/{i}"))
        iou = {g: o / (a + b - o) for g, (a, b, o) in tot.items() if a + b > 0}
        worst.sort()
        # KiCad's boards were the calibration set; AltiumSharp's 4-layer boards'
        # big GND pours come out about 1 % larger than saved (dev/CLOUD_HANDOFF.md)
        if "kicad" in iou:
            self.assertGreater(iou["kicad"], 0.99, worst[:5])
        self.assertGreater(iou["all"], 0.985, worst[:5])
        # Fastino's top-layer pours connect SMD pads directly although its rule
        # says relief (see dev/CLOUD_HANDOFF.md); every other polygon is close
        others = [w for w in worst if "issue24456" not in w[1]]
        self.assertGreater(others[0][0], 0.97, others[:3])


    def test_repoured_boards_stay_connected(self):
        from offline.connect import Connectivity
        for f in fixtures.boards():
            doc = PcbDoc.open(f)
            eng = PourEngine(doc)
            if not eng.pourable():
                continue
            with self.subTest(f.parent.name):
                before = Connectivity(doc).summary()
                eng.store(eng.repour_all("clean"), "pour")
                after = Connectivity(doc).summary()
                self.assertEqual(after["shorts"], 0)
                self.assertEqual(after, before)


@unittest.skipIf(sh is None, "shapely not installed")
class PourSpeed(unittest.TestCase):
    def test_large_board_repour(self):
        doc = stress_board().build()
        self.assertGreater(len(doc.tracks), 3000)
        t0 = time.time()
        run = PourEngine(doc).repour_all("clean")
        dt = time.time() - t0
        self.assertEqual(len(run), 48)
        self.assertLess(dt, 60.0)


if __name__ == "__main__":
    unittest.main()
