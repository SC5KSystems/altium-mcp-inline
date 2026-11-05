"""Every public fixture in the cache (KiCad's and AltiumSharp's test data):
boards open, survive a no-op save byte for byte, and go through
connectivity; every library footprint parses; panels say they are panels.
Skips when the cache is empty (python dev/fetch_fixtures.py fills it).

    python -m unittest discover -s server/tests -p "test_offline_corpus.py"
"""

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from offline import fixtures  # noqa: E402
from offline.cfb import CompoundFile  # noqa: E402
from offline.pcbdoc import PcbDoc  # noqa: E402

try:
    import shapely  # noqa: F401
    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False

BOARDS = fixtures.find(".PcbDoc")
LIBS = fixtures.find(".PcbLib")
PANEL = fixtures.by_name("Power Adapter Panel", ".PcbDoc")
VCOCXO = fixtures.by_name("VCOCXO Breakout", ".PcbDoc")


@unittest.skipUnless(BOARDS, "no fixture boards cached")
class Boards(unittest.TestCase):
    def test_open_and_noop_save(self):
        from offline import pcbwrite
        with tempfile.TemporaryDirectory() as tmp:
            for f in BOARDS:
                with self.subTest(board=f.name):
                    doc = PcbDoc.open(f)
                    out = Path(tmp) / "out.PcbDoc"
                    pcbwrite.save(doc, out)
                    a, b = CompoundFile.open(f).streams(), CompoundFile.open(out).streams()
                    self.assertEqual([k for k in a if a[k] != b.get(k)], [])

    @unittest.skipUnless(HAVE, "shapely not installed")
    def test_connectivity_runs(self):
        from offline import connect
        for f in BOARDS:
            with self.subTest(board=f.name):
                rep = connect.report(PcbDoc.open(f))
                self.assertIn("summary", rep)


@unittest.skipUnless(LIBS, "no fixture libraries cached")
class Libraries(unittest.TestCase):
    def test_every_footprint_parses(self):
        from offline.footprints import PcbLib
        n = 0
        for f in LIBS:
            lib = PcbLib.open(f)
            for name in lib.names():
                with self.subTest(lib=f.name, footprint=name):
                    lib.footprint(name)
                    n += 1
        self.assertGreater(n, 0)


class Panels(unittest.TestCase):
    @unittest.skipUnless(PANEL, "panel fixture not cached")
    def test_panel_is_reported(self):
        doc = PcbDoc.open(PANEL)
        eb = doc.embedded_boards
        self.assertEqual(len(eb), 1)
        self.assertEqual(eb[0]["document"], "USB Power Adapter.PcbDoc")
        self.assertEqual((eb[0]["rows"], eb[0]["cols"]), (4, 3))
        self.assertIn("12 embedded board instance(s)", doc.panel_warning())
        self.assertEqual(doc.summary()["embedded_boards"], 1)

    @unittest.skipUnless(PANEL and HAVE, "panel fixture or shapely missing")
    def test_reports_carry_the_warning(self):
        from offline import connect
        from offline.drc import DRC
        doc = PcbDoc.open(PANEL)
        self.assertTrue(connect.report(doc)["warnings"])
        d = DRC(doc)
        d.run()
        self.assertTrue(d.report()["warnings"])

    @unittest.skipUnless(BOARDS, "no fixture boards cached")
    def test_plain_boards_have_no_warning(self):
        doc = PcbDoc.open(fixtures.by_name("HiFive1.B01", ".PcbDoc") or BOARDS[0])
        if not doc.embedded_boards:
            self.assertIsNone(doc.panel_warning())


class Designators(unittest.TestCase):
    @unittest.skipUnless(fixtures.by_name("MAX5719", ".PcbDoc"), "fixture not cached")
    def test_duplicates_refuse(self):
        doc = PcbDoc.open(fixtures.by_name("MAX5719", ".PcbDoc"))
        self.assertIn("IC5", doc.duplicate_designators)
        self.assertIn("IC5", doc.summary()["duplicate_designators"])
        with self.assertRaisesRegex(KeyError, "used by 2 components"):
            doc.component("IC5")
        self.assertTrue(any("designator" in w for w in doc.board_warnings()))
        doc.component("IC1")


class Pours(unittest.TestCase):
    @unittest.skipUnless(VCOCXO and HAVE, "fixture or shapely missing")
    def test_other_pours_do_not_keep_a_piece_alive(self):
        """A piece touching only another GND polygon's copper is dead copper:
        Altium removed it in the saved file."""
        from offline.pour import PourEngine
        doc = PcbDoc.open(VCOCXO)
        eng = PourEngine(doc)
        res = eng.repour_all("clean")
        self.assertGreater(eng.compare(res[0])["iou"], 0.95)


class Scopes(unittest.TestCase):
    def test_is_poly(self):
        from offline.scope import compile_scope
        self.assertFalse(compile_scope("IsPoly").is_all)
        compile_scope("IsPoly or IsPolygon")

    @unittest.skipUnless(VCOCXO and HAVE, "fixture or shapely missing")
    def test_poured_copper_is_polygon(self):
        """A pour's copper answers IsPolygon (its BoardOutlineClearance rule is
        scoped so), other copper does not."""
        from offline.scope import compile_scope
        doc = PcbDoc.open(VCOCXO)
        sc = compile_scope("IsPolygon")
        poured = next(r for r in doc.regions if r.polygon is not None)
        self.assertTrue(sc.match(doc, poured))
        self.assertFalse(sc.match(doc, doc.tracks[0]) and doc.tracks[0].polygon is None)
        self.assertFalse(sc.match(doc, doc.pads[0]))


if __name__ == "__main__":
    unittest.main()


class FabFolders(unittest.TestCase):
    def test_outputs_in_subfolders_are_found(self):
        from offline.gerber import check
        from offline.synth import Synth
        s = Synth(20, 20)
        s.net("GND")
        doc = s.build()
        gerber = ("%FSLAX46Y46*%\n%MOMM*%\n%TF.FileFunction,Copper,L1,Top*%\n"
                  "%ADD10C,0.100000*%\nD10*\nX1000000Y1000000D03*\nM02*\n")
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "Gerber"
            sub.mkdir()
            (sub / "board.GTL").write_text(gerber)
            (Path(tmp) / "outputs.zip").write_bytes(b"PK")
            rep = check(doc, tmp)
            self.assertEqual([g["file"] for g in rep["gerbers"]], [str(Path("Gerber") / "board.GTL")])
            self.assertTrue(any("zip" in n for n in rep["notes"]))
            empty = Path(tmp) / "empty"
            empty.mkdir()
            rep = check(doc, empty)
            self.assertTrue(any("no Gerber or drill file" in p for p in rep["problems"]))
