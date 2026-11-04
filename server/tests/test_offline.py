"""Offline PcbDoc tools: reader, writer, connectivity, rules, raster, PDN.

    python -m unittest discover -s server/tests -p "test_offline*.py"

Synthetic boards (offline/synth.py) always run. Tests on real files use the
cache filled by dev/fetch_fixtures.py and $ALTIUM_TEST_PCBDOC, and skip when
neither has a board. Nothing here imports main.py (it needs pywin32).
"""

import math
import os
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from offline import fixtures, connect, pcbwrite, pdn, raster, records as R  # noqa: E402
from offline import layers as L  # noqa: E402
from offline.cfb import CompoundFile  # noqa: E402
from offline.cfb_write import tree_from, to_bytes  # noqa: E402
from offline.pcbdoc import (PcbDoc, Props, fmt_float, fmt_mil, mm2u, parse_length,  # noqa: E402
                            u2mm, v7_for)
from offline.rules import RuleSet  # noqa: E402
from offline.scope import ScopeError, compile_scope  # noqa: E402
from offline.synth import Synth  # noqa: E402

BOARDS = fixtures.boards()
needs_fixtures = unittest.skipUnless(BOARDS, "no fixture boards: run dev/fetch_fixtures.py")


def two_net_board():
    s = Synth(40, 30)
    s.net("VBAT")
    s.net("GND")
    s.component("J1", "CONN2", 5, 15)
    s.component("U1", "LOAD", 35, 15)
    s.pad("J1", "1", 5, 15, 2, 2, net="VBAT")
    s.pad("J1", "2", 5, 10, 2, 2, net="GND")
    s.pad("U1", "1", 35, 15, 2, 2, net="VBAT")
    s.pad("U1", "2", 35, 10, 2, 2, net="GND")
    s.track("Top Layer", 5, 15, 35, 15, 1.0, net="VBAT")
    s.track("Top Layer", 5, 10, 20, 10, 1.0, net="GND")
    s.via(20, 10, net="GND")
    s.track("Bottom Layer", 20, 10, 35, 10, 1.0, net="GND")
    s.via(35, 10, net="GND")
    s.rule("Clearance", "Clearance", GAP=0.2)
    s.rule("Clearance", "PowerGap", "InNet('VBAT')", "All", priority=0, GAP=0.5)
    s.rule("Width", "Width", MINLIMIT=0.2, MAXLIMIT=5, PREFEREDWIDTH=0.5)
    s.net_class("PWR", ["VBAT"])
    return s


class UnitsTest(unittest.TestCase):
    def test_round_trip(self):
        for mm in (0, 1, -3.3, 25.4, 116.0):
            self.assertAlmostEqual(u2mm(mm2u(mm)), mm, places=5)
        self.assertEqual(mm2u(0.0254), 10000)

    def test_text_forms(self):
        self.assertAlmostEqual(parse_length("1000mil"), 25.4)
        self.assertAlmostEqual(parse_length("2mm"), 2.0)
        self.assertEqual(fmt_mil(25.4), "1000mil")
        self.assertEqual(fmt_mil(0.0), "0mil")
        self.assertEqual(fmt_float(270.0), " 2.70000000000000E+0002")
        self.assertEqual(fmt_float(-45.0), "-4.50000000000000E+0001")

    def test_layers(self):
        self.assertEqual(L.parse("Top Layer"), L.TOP)
        self.assertEqual(L.parse("MID3"), 4)
        self.assertEqual(L.parse("Mechanical 17"), 1017)
        self.assertEqual(L.name(1017), "Mechanical 17")
        self.assertEqual(v7_for(L.BOTTOM), 0x0100FFFF)
        self.assertEqual(v7_for(L.parse("MECHANICAL15")), 0x0102000F)
        self.assertTrue(L.is_copper(40) and not L.is_signal(40))


class PropsTest(unittest.TestCase):
    def test_lossless_until_edited(self):
        raw = b"|NAME=GND|X=1.5mil|\x81odd=\xe9|LAST=1\0"
        p = Props(raw)
        self.assertEqual(p.text_bytes(), raw)
        self.assertEqual(p.get("name"), "GND")
        p.set("X", "2mil")
        self.assertIn(b"X=2mil", p.text_bytes())
        self.assertIn(b"\x81odd=\xe9", p.text_bytes())

    def test_unicode_side_channel(self):
        p = Props(b"|NAME=x|UNICODE__NAME=86,66,65,84\0")
        self.assertEqual(p.text("NAME"), "VBAT")
        p.set("NAME", "GND")
        self.assertEqual(p.text("NAME"), "GND")


class CfbTest(unittest.TestCase):
    def test_synthetic_container(self):
        doc = two_net_board().build()
        data = pcbwrite.to_document_bytes(doc)
        cf = CompoundFile(data)
        again = CompoundFile(to_bytes(tree_from(cf), like=cf))
        self.assertEqual(cf.streams(), again.streams())

    def test_many_streams_force_difat(self):
        from offline.cfb import TYPE_ROOT
        from offline.cfb_write import DirNode
        root = DirNode("Root Entry", TYPE_ROOT)
        big = os.urandom(100000)
        for i in range(120):   # ~9 MB: more than 109 FAT sectors, so DIFAT sectors exist
            root.set_stream(f"S{i}/Data", big[: 60000 + i * 300])
            root.set_stream(f"S{i}/Header", struct.pack("<I", i))
        data = to_bytes(root)
        cf = CompoundFile(data)
        self.assertGreater(cf.n_fat_sectors, 109)
        self.assertGreater(cf.n_difat, 0)
        for i in (0, 57, 119):
            self.assertEqual(cf.read(f"S{i}/Data"), big[: 60000 + i * 300])
            self.assertEqual(cf.read(f"S{i}/Header"), struct.pack("<I", i))

    @needs_fixtures
    def test_fixture_round_trip(self):
        for f in BOARDS + fixtures.find(".PcbLib"):
            with self.subTest(f.name):
                cf = CompoundFile.open(f)
                for preserve in (True, False):
                    out = CompoundFile(to_bytes(tree_from(cf), like=cf, preserve=preserve))
                    self.assertEqual(out.streams(), cf.streams())


class ReaderTest(unittest.TestCase):
    @needs_fixtures
    def test_every_storage_reencodes_exactly(self):
        from offline.pcbdoc import encode_widestrings
        for f in BOARDS:
            with self.subTest(f.name):
                b = PcbDoc.open(f)
                for name, st in b.storages.items():
                    enc = (encode_widestrings(b.widestrings, b.widestring_order)
                           if st.kind == "widestrings" else st.encode_data())
                    self.assertEqual(enc, st.raw, name)
                self.assertEqual(pcbwrite.check_integrity(b), [])

    @needs_fixtures
    def test_geometry_inside_outline(self):
        for f in BOARDS:
            b = PcbDoc.open(f)
            pts = b.outline()
            if not pts or not b.pads:
                continue
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            for p in b.pads:
                self.assertTrue(min(xs) - 1 <= p.x <= max(xs) + 1 and min(ys) - 1 <= p.y <= max(ys) + 1,
                                f"{f.name} pad {p.designator}.{p.name} outside the board")

    def test_synthetic_fields(self):
        doc = two_net_board().build()
        p = doc.pads[0]
        self.assertEqual((p.designator, p.name, p.net_name), ("J1", "1", "VBAT"))
        self.assertAlmostEqual(p.x, 5.0, places=5)
        t = doc.tracks[2]
        self.assertEqual(t.layer, L.BOTTOM)
        self.assertEqual(t.v7_layer, 0x0100FFFF)
        self.assertAlmostEqual(t.width, 1.0, places=5)


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_refuses_to_overwrite_input(self):
        doc = two_net_board().save(self.dir / "a.PcbDoc")
        with self.assertRaises(Exception):
            pcbwrite.save(doc, self.dir / "a.PcbDoc")

    def test_add_then_reread(self):
        doc = two_net_board().save(self.dir / "a.PcbDoc")
        pcbwrite.add(doc, R.track(doc, "Top Layer", 1, 1, 2, 2, 0.3, "GND"))
        pcbwrite.add(doc, R.pad(doc, "9", 30, 25, 1, 1, "TOP", "roundrect", net="GND",
                                component="U1", corner_pct=40))
        pcbwrite.add(doc, R.region(doc, "Top Layer", [(1, 20), (5, 20), (5, 25), (1, 25)],
                                   [[(2, 21), (3, 21), (3, 22), (2, 22)]], net="GND"))
        pcbwrite.add(doc, R.text(doc, "REV A", 2, 28, 1.2))
        pcbwrite.save(doc, self.dir / "b.PcbDoc")
        b = PcbDoc.open(self.dir / "b.PcbDoc")
        self.assertEqual(len(b.tracks), 4)
        self.assertEqual(len(b.regions), len(b.shape_regions))
        self.assertEqual(b.pads[-1].shape_on(L.TOP)[0], "roundrect")
        self.assertAlmostEqual(b.pads[-1].shape_on(L.TOP)[2], 0.40)
        self.assertEqual(b.regions[-1].holes and len(b.regions[-1].holes), 1)
        self.assertEqual(b.texts[-1].text, "REV A")
        uids = b.storages["UniqueIDPrimitiveInformation"].records
        self.assertEqual(len(uids), len(b.pads))
        self.assertEqual(pcbwrite.check_integrity(b), [])

    @needs_fixtures
    def test_noop_save_is_stream_identical(self):
        for f in BOARDS:
            with self.subTest(f.name):
                out = self.dir / f.name
                pcbwrite.save(PcbDoc.open(f), out)
                self.assertEqual(CompoundFile.open(out).streams(), CompoundFile.open(f).streams())


class ConnectTest(unittest.TestCase):
    def test_synthetic_connected(self):
        c = connect.Connectivity(two_net_board().build())
        self.assertEqual(c.summary()["islands"], 2)
        self.assertEqual(c.shorts, [])

    def test_gap_and_short(self):
        s = two_net_board()
        s.track("Top Layer", 20, 20, 30, 20, 0.5, net="GND")      # dead copper
        s.track("Top Layer", 10, 14.6, 10, 11, 0.5, net="GND")     # overlaps the VBAT track
        doc = s.build()
        c = connect.Connectivity(doc)
        nets = {tuple(sorted(L_ for L_ in (c.net_name(x["net_a"]), c.net_name(x["net_b"]))))
                for x in c.shorts}
        self.assertIn(("GND", "VBAT"), nets)
        self.assertTrue(c.dead_copper())

    def test_unrouted_gap_reported(self):
        s = Synth(40, 30)
        s.net("SIG")
        s.component("U1", "X", 5, 5)
        s.pad("U1", "1", 5, 5, 1, 1, net="SIG")
        s.pad("U1", "2", 15, 5, 1, 1, net="SIG")
        s.track("Top Layer", 5, 5, 10, 5, 0.3, net="SIG")
        c = connect.Connectivity(s.build())
        un = c.unrouted()
        self.assertEqual(len(un), 1)
        self.assertAlmostEqual(un[0]["gap_mm"], 15 - 0.5 - 10 - 0.15, places=3)

    @needs_fixtures
    def test_finished_fixtures_fully_connected(self):
        for f in BOARDS:
            with self.subTest(f.name):
                c = connect.Connectivity(PcbDoc.open(f))
                s = c.summary()
                self.assertEqual(s["unrouted_connections"], 0)
                self.assertEqual(s["shorts"], 0)


class ScopeRulesTest(unittest.TestCase):
    def setUp(self):
        self.doc = two_net_board().build()

    def test_scopes(self):
        d = self.doc
        vb_track = d.tracks[0]
        self.assertTrue(compile_scope("InNet('vbat')").match(d, vb_track))
        self.assertTrue(compile_scope("InNetClass('PWR') and IsTrack").match(d, vb_track))
        self.assertFalse(compile_scope("IsVia or OnBottom").match(d, vb_track))
        self.assertTrue(compile_scope("ObjectKind = 'Track' and Net LIKE 'VB*'").match(d, vb_track))
        self.assertTrue(compile_scope("InComponent('J1')").match(d, d.pads[0]))
        self.assertTrue(compile_scope("CompPinCount < 3").match(d, d.pads[0]))
        with self.assertRaises(ScopeError):
            compile_scope("IsMadeUpThing")
        with self.assertRaises(ScopeError):
            compile_scope("InNet('a'")

    def test_rule_priority(self):
        rs = RuleSet(self.doc)
        gap, rule = rs.clearance(self.doc.tracks[0], self.doc.tracks[1])
        self.assertEqual(rule.name, "PowerGap")
        self.assertAlmostEqual(gap, 0.5, places=4)
        gap, rule = rs.clearance(self.doc.tracks[1], self.doc.tracks[2])
        self.assertEqual(rule.name, "Clearance")


class RasterPdnTest(unittest.TestCase):
    def test_exact_coverage(self):
        from shapely.geometry import box, Polygon
        g = raster.Grid(0, 0, 20, 20, 0.1)
        m = g.empty()
        raster.draw(m, g, box(5, 5, 6, 15))
        self.assertEqual(int(m.sum()), 1000)
        m = g.empty()
        raster.draw(m, g, box(0, 0, 10, 10))
        raster.draw(m, g, Polygon([(1, 1), (9, 1), (9, 9), (1, 9)], [[(3, 3), (7, 3), (7, 7), (3, 7)]]))
        self.assertEqual(int(m.sum()), 10000)

    def test_track_resistance_matches_formula(self):
        doc = two_net_board().build()
        r = pdn.solve(doc, "VBAT", ["J1.1"], ["U1.1"], 10.0, res=0.05)
        expect = 10 * pdn.RHO_20 * 28 / (1.0 * 0.035)
        self.assertAlmostEqual(r.drop_v, expect, delta=expect * 0.01)
        self.assertAlmostEqual(r.power_w, 10 * r.drop_v, delta=r.power_w * 0.01)

    def test_via_carries_all_current(self):
        doc = two_net_board().build()
        r = pdn.solve(doc, "GND", ["J1.2"], ["U1.2"], 3.0, res=0.05)
        self.assertAlmostEqual(max(v["current_a"] for v in r.vias), 3.0, places=3)

    def test_disconnected_source_is_an_error(self):
        s = two_net_board()
        s.pad("U1", "3", 20, 25, 1, 1, net="VBAT")
        with self.assertRaises(pdn.PdnError):
            pdn.solve(s.build(), "VBAT", ["U1.3"], ["U1.1"], 1.0)


if __name__ == "__main__":
    unittest.main()
