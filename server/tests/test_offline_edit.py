"""Editing: removal and renumbering, nets, components, polygons, rules, EditPlan, live scripts.

    python -m unittest discover -s server/tests -p "test_offline_edit.py"
"""

import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import script_preflight as sp  # noqa: E402
from offline import editplan, fixtures, live, pcbwrite  # noqa: E402
from offline import layers as L  # noqa: E402
from offline.cfb import CompoundFile  # noqa: E402
from offline.connect import Connectivity  # noqa: E402
from offline.edits import EditError, Editor  # noqa: E402
from offline.pcbdoc import PcbDoc  # noqa: E402
from offline.synth import Synth  # noqa: E402

BOARDS = fixtures.boards()
needs_fixtures = unittest.skipUnless(BOARDS, "no fixture boards: run dev/fetch_fixtures.py")


def board():
    s = Synth(50, 40)
    for n in ("VBAT", "GND", "SENSE", "OUT"):
        s.net(n)
    s.component("J1", "CONN2", 5, 20)
    s.component("R1", "RES", 25, 20)
    s.component("U1", "LOAD", 45, 20, rotation=90)
    s.pad("J1", "1", 5, 20, 2, 2, net="VBAT")
    s.pad("J1", "2", 5, 10, 2, 2, net="GND")
    s.pad("R1", "1", 23, 20, 1, 1.4, net="VBAT")
    s.pad("R1", "2", 27, 20, 1, 1.4, net="SENSE")
    s.pad("U1", "1", 45, 22, 1.5, 1, net="SENSE")
    s.pad("U1", "2", 45, 18, 1.5, 1, net="GND")
    s.track("Top Layer", 5, 20, 23, 20, 1.5, net="VBAT")
    s.track("Top Layer", 27, 20, 45, 22, 1.0, net="SENSE")
    s.track("Bottom Layer", 5, 10, 45, 18, 1.0, net="GND")
    s.via(5, 10, net="GND")
    s.via(45, 18, net="GND")
    s.fill("Top Layer", 40, 30, 48, 36, net="GND")
    s.text("R1", 25, 23, 1.0, component="R1", designator=True)
    s.polygon("BOTTOM", [(1, 1), (24, 1), (24, 8), (1, 8)], net="GND", name="GNDA")
    s.polygon("BOTTOM", [(22, 1), (49, 1), (49, 8), (22, 8)], net="GND", name="GNDB")
    s.polygon("TOP", [(1, 30), (30, 30), (30, 38), (1, 38)], net="VBAT", name="PWR")
    s.region("Bottom Layer", [(2, 2), (10, 2), (10, 6), (2, 6)], net=None, polygon=0)
    s.region("Bottom Layer", [(30, 2), (40, 2), (40, 6), (30, 6)], net=None, polygon=1)
    s.rule("Clearance", "Clearance", GAP=0.2)
    s.rule("Width", "Width", MINLIMIT=0.2, MAXLIMIT=5, PREFEREDWIDTH=0.5)
    s.net_class("PWR", ["VBAT"])
    return s


class RemoveTest(unittest.TestCase):
    def test_select_and_remove_keeps_integrity(self):
        doc = board().build()
        ed = Editor(doc)
        recs = ed.select(kinds=["track", "via"], net="GND")
        self.assertEqual(len(recs), 3)
        ed.remove(recs)
        self.assertEqual(len(doc.vias), 0)
        self.assertEqual(len(doc.tracks), 2)
        self.assertEqual(pcbwrite.check_integrity(doc), [])

    def test_box_selection(self):
        doc = board().build()
        ed = Editor(doc)
        self.assertEqual(len(ed.select(kinds=["pad"], box=(0, 0, 10, 30))), 2)
        self.assertEqual(len(ed.select(kinds=["track"], box=(0, 15, 24, 25))), 1)

    def test_pours_need_the_polygon_selector(self):
        doc = board().build()
        ed = Editor(doc)
        self.assertEqual(ed.select(kinds=["region"]), [])
        self.assertEqual(len(ed.select(kinds=["region"], polygon="GNDA")), 1)

    def test_removing_a_used_net_is_refused(self):
        doc = board().build()
        with self.assertRaises(EditError):
            Editor(doc).remove_net("VBAT")
        self.assertTrue(doc.has_net("VBAT"))

    def test_pad_unique_ids_renumbered(self):
        doc = board().build()
        ed = Editor(doc)
        ed.remove(ed.select(kinds=["pad"], component="J1"))
        uids = doc.storages["UniqueIDPrimitiveInformation"].records
        self.assertEqual(sorted(int(u.get("PRIMITIVEINDEX")) for u in uids), list(range(len(doc.pads))))


class NetTest(unittest.TestCase):
    def test_merge_after_part_removal(self):
        doc = board().build()
        ed = Editor(doc)
        ed.remove_component("R1")
        ed.merge_nets(["SENSE"], into="VBAT")
        self.assertFalse(doc.has_net("SENSE"))
        self.assertEqual(doc.pads[-2].net_name, "VBAT")
        un = Connectivity(doc).unrouted()
        gaps = [u for u in un if u["net_name"] == "VBAT"]
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0]["gap_mm"], 27 - 0.5 - 23 - 0.75, places=2)
        self.assertEqual(pcbwrite.check_integrity(doc), [])

    def test_rename_updates_classes_rules_planes(self):
        s = board()
        s.rule("Clearance", "VbatGap", "InNet('VBAT')", "All", priority=0, GAP=0.5)
        doc = s.build()
        Editor(doc).rename_net("VBAT", "VIN")
        self.assertTrue(doc.has_net("VIN"))
        cls = next(c for c in doc.classes if c.name == "PWR")
        self.assertEqual(cls.members, ["VIN"])
        rule = next(r for r in doc.rules if r.name == "VbatGap")
        self.assertIn("'VIN'", rule.scope1)

    def test_set_net_refuses_poured_copper(self):
        doc = board().build()
        ed = Editor(doc)
        with self.assertRaises(EditError):
            ed.set_net(ed.select(kinds=["region"], polygon="GNDA"), "VBAT")


class ComponentTest(unittest.TestCase):
    def pads(self, doc, des):
        c = doc.component(des)
        return sorted((p.name, round(p.x, 4), round(p.y, 4), round(p.rotation % 180, 3))
                      for p in doc.pads if p.component == c.index)

    def test_move_rotate_flip_round_trip(self):
        doc = board().build()
        ed = Editor(doc)
        before = self.pads(doc, "U1")
        ed.move_component("U1", 30, 30, 0)
        moved = self.pads(doc, "U1")
        self.assertNotEqual(before, moved)
        ed.move_component("U1", 45, 20, 90, side="bottom")
        self.assertEqual(doc.component("U1").layer, L.BOTTOM)
        self.assertTrue(all(p.layer == L.BOTTOM for p in doc.pads if p.component ==
                            doc.component("U1").index))
        ed.move_component("U1", 45, 20, 90, side="top")
        self.assertEqual(self.pads(doc, "U1"), before)

    def test_bottom_convention(self):
        doc = board().build()
        ed = Editor(doc)
        ed.move_component("R1", 25, 20, 0, side="bottom")
        p1 = next(p for p in doc.pads if p.designator == "R1" and p.name == "1")
        self.assertAlmostEqual(p1.x, 23.0, places=4)   # y-mirror keeps x for rotation 0
        ed.move_component("R1", 25, 20, 90, side="bottom")
        p1 = next(p for p in doc.pads if p.designator == "R1" and p.name == "1")
        self.assertAlmostEqual(p1.y, 18.0, places=4)   # Rot(90) . (-2, 0) -> (0, -2)

    def test_add_component_with_footprint(self):
        doc = board().build()
        ed = Editor(doc)
        kids = [{"kind": "pad", "name": "1", "x": -1.0, "y": 0, "sx": 0.8, "sy": 1.0},
                {"kind": "pad", "name": "2", "x": 1.0, "y": 0, "sx": 0.8, "sy": 1.0},
                {"kind": "track", "layer": "Top Overlay", "x1": -1.5, "y1": 1, "x2": 1.5, "y2": 1,
                 "width": 0.15}]
        ed.add_component("C9", "CAP0603", 10, 35, 90, "top", kids)
        c = doc.component("C9")
        pads = sorted((p.name, round(p.x, 4), round(p.y, 4)) for p in doc.pads if p.component == c.index)
        self.assertEqual(pads, [("1", 10.0, 34.0), ("2", 10.0, 36.0)])
        self.assertEqual(pcbwrite.check_integrity(doc), [])


class PolygonTest(unittest.TestCase):
    def test_outline_edit_marks_stale(self):
        doc = board().build()
        ed = Editor(doc)
        ed.set_polygon_outline("GNDA", [(1, 1), (20, 1), (20, 9), (1, 9)])
        self.assertEqual(list(doc.poured(0)), [])
        self.assertIn(0, ed.stale_pours)
        self.assertAlmostEqual(max(p[0] for p in doc.polygons[0].outline()), 20.0, places=4)

    def test_merge_polygons(self):
        doc = board().build()
        ed = Editor(doc)
        keep = ed.merge_polygons("GNDA", "GNDB")
        self.assertEqual(len(doc.polygons), 2)
        xs = [p[0] for p in doc.polygons[keep].outline()]
        self.assertAlmostEqual(min(xs), 1.0, places=4)
        self.assertAlmostEqual(max(xs), 49.0, places=4)
        self.assertEqual(pcbwrite.check_integrity(doc), [])

    def test_remove_polygon_renumbers_pours(self):
        doc = board().build()
        ed = Editor(doc)
        ed.remove_polygon("GNDA")
        self.assertEqual(len(doc.polygons), 2)
        self.assertEqual([r.polygon for r in doc.regions], [0])
        self.assertEqual(pcbwrite.check_integrity(doc), [])


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir)

    def changed(self, a, b):
        sa, sb = CompoundFile.open(a).streams(), CompoundFile.open(b).streams()
        return sorted({k for k in set(sa) | set(sb) if sa.get(k) != sb.get(k)})

    def test_single_edits_touch_only_their_streams(self):
        src = board().save(self.dir / "a.PcbDoc")
        cases = [
            ({"op": "add_track", "layer": "Top Layer", "x1": 1, "y1": 1, "x2": 2, "y2": 2,
              "width": 0.3, "net": "GND"}, {"Tracks6/Data", "Tracks6/Header"}),
            ({"op": "rename_net", "from": "OUT", "to": "OUT2"}, {"Nets6/Data"}),
            ({"op": "add_rule", "kind": "Clearance", "name": "HV", "scope1": "InNet('VBAT')",
              "values": {"GAP": 1.0}}, {"Rules6/Data", "Rules6/Header"}),
        ]
        for i, (op, expect) in enumerate(cases):
            with self.subTest(op["op"]):
                doc = PcbDoc.open(src.path)
                ed, _ = editplan.apply(doc, {"ops": [op]})
                out = self.dir / f"b{i}.PcbDoc"
                ed.save(out)
                self.assertEqual(set(self.changed(src.path, out)), expect)

    def test_plan_round_trip_and_reread(self):
        src = board().save(self.dir / "a.PcbDoc")
        plan = {"ops": [
            {"op": "remove_component", "designator": "R1"},
            {"op": "merge_nets", "from": ["SENSE"], "into": "VBAT"},
            {"op": "add_track", "layer": "Top Layer", "x1": 23, "y1": 20, "x2": 45, "y2": 22,
             "width": 1.5, "net": "VBAT"},
            {"op": "add_via", "x": 30, "y": 25, "diameter": 0.8, "hole": 0.4, "net": "VBAT"},
            {"op": "add_net_class", "name": "HV", "nets": ["VBAT"]},
        ]}
        ed, res = editplan.apply(PcbDoc.open(src.path), plan)
        rep = ed.save(self.dir / "c.PcbDoc")
        doc = PcbDoc.open(self.dir / "c.PcbDoc")
        self.assertEqual(pcbwrite.check_integrity(doc), [])
        self.assertFalse(doc.has_component("R1"))
        c = Connectivity(doc)
        self.assertEqual(c.summary()["unrouted_connections"], 0, c.unrouted())
        self.assertEqual(c.shorts, [])

    def test_plan_validation(self):
        bad = {"ops": [{"op": "remove"}, {"op": "nope"}, {"op": "add_track", "layer": "Top Layer"},
                       {"op": "add_via", "x": "1", "y": 2, "diameter": 1, "hole": 0.5}]}
        probs = editplan.validate(bad)
        self.assertEqual(len([p for p in probs if p.startswith("op 0")]), 1)
        self.assertTrue(any("unknown op" in p for p in probs))
        self.assertTrue(any("missing 'x1'" in p for p in probs))
        self.assertTrue(any("must be a number" in p for p in probs))

    def test_failed_op_is_reported(self):
        doc = board().build()
        with self.assertRaises(editplan.PlanError) as cm:
            editplan.apply(doc, {"ops": [{"op": "remove_net", "name": "VBAT"}]})
        self.assertIn("op 0", str(cm.exception))

    @needs_fixtures
    def test_fixture_component_removal(self):
        for f in BOARDS:
            doc = PcbDoc.open(f)
            if not doc.components:
                continue
            with self.subTest(f.name):
                ed = Editor(doc)
                comp = max(doc.components, key=lambda c: sum(1 for p in doc.pads if p.component == c.index))
                ed.remove_component(comp.designator)
                rep = ed.save(self.dir / f.name)
                again = PcbDoc.open(self.dir / f.name)
                self.assertEqual(pcbwrite.check_integrity(again), [])
                self.assertEqual(len(again.components), len(doc.components))

    @needs_fixtures
    def test_fixture_add_track_changes_only_tracks(self):
        f = BOARDS[0]
        doc = PcbDoc.open(f)
        ed = Editor(doc)
        ed.add_track("Top Layer", 30, 30, 31, 31, 0.2)
        out = self.dir / "t.PcbDoc"
        ed.save(out)
        expect = {"Tracks6/Data", "Tracks6/Header"}
        if "PrimitiveGuids" in doc.storages:
            expect |= {"PrimitiveGuids/Data", "PrimitiveGuids/Header"}
        self.assertEqual(set(self.changed(f, out)), expect)


class LiveTest(unittest.TestCase):
    PLAN = {"ops": [
        {"op": "remove", "kinds": ["track", "via"], "net": "GND", "box": [0, 0, 50, 40]},
        {"op": "set_net", "select": {"kinds": ["pad"], "component": "R1"}, "net": "VBAT"},
        {"op": "rename_net", "from": "OUT", "to": "OUT2"},
        {"op": "merge_nets", "from": ["SENSE"], "into": "VBAT"},
        {"op": "add_track", "layer": "Top Layer", "x1": 1, "y1": 1, "x2": 9, "y2": 1, "width": 1,
         "net": "GND"},
        {"op": "add_via", "x": 9, "y": 1, "diameter": 0.6, "hole": 0.3, "net": "GND"},
        {"op": "add_fill", "layer": "Bottom Layer", "x1": 1, "y1": 1, "x2": 3, "y2": 3},
        {"op": "add_arc", "layer": "Top Layer", "cx": 5, "cy": 5, "radius": 1, "start_angle": 0,
         "end_angle": 180, "width": 0.3},
        {"op": "add_region", "layer": "Top Layer", "outline": [[1, 1], [2, 1], [2, 2]]},
        {"op": "add_text", "text": "it's", "x": 3, "y": 3, "height": 1},
        {"op": "move_component", "designator": "U1", "x": 40, "y": 20, "rotation": 0,
         "side": "bottom"},
        {"op": "remove_component", "designator": "J1"},
        {"op": "unpour", "polygon": "PWR"},
        {"op": "remove_polygon", "polygon": "GNDB"},
        {"op": "set_rule_scope", "name": "Clearance", "scope2": "InNet('GND')"},
        {"op": "add_net_class", "name": "PWR2", "nets": ["VBAT"]},
    ]}

    def test_every_script_passes_preflight(self):
        doc = board().build()
        scripts = live.scripts_for_plan(self.PLAN, "C:\\scratch\\x.PcbDoc", doc)
        self.assertGreaterEqual(len(scripts), len(self.PLAN["ops"]))
        for sc in scripts:
            with self.subTest(sc["title"]):
                rep = sp.check(sc["script"], live.SANDBOX)
                self.assertTrue(rep["ok"], sp.format_report(rep))
                self.assertNotIn("Exit", sc["script"])
                self.assertIn("GetPCBBoardByPath", sc["script"])
        removal = scripts[0]
        self.assertEqual(removal["expected"], 3)
        self.assertIn("Obj5.Count <> 3", removal["script"])
        self.assertIn("'it''s'", next(s["script"] for s in scripts if s["op"] == "add_text"))

    def test_unsupported_ops_fail_loudly(self):
        doc = board().build()
        for op in ({"op": "add_component", "designator": "X1", "pattern": "P", "x": 1, "y": 1},
                   {"op": "remove", "kinds": ["region"], "polygon": "GNDA"}):
            with self.subTest(op["op"]):
                with self.assertRaises(live.LiveError):
                    live.scripts_for_plan({"ops": [op]}, "C:\\x.PcbDoc", doc)

    def test_write_scripts(self):
        d = Path(tempfile.mkdtemp())
        try:
            out = live.write_scripts(self.PLAN, "C:\\scratch\\x.PcbDoc", d, board().build())
            self.assertTrue(out["ok"], out)
            manifest = json.loads((d / "manifest.json").read_text())
            self.assertEqual(len(manifest), out["scripts"])
            self.assertTrue((d / "999_save.pas").is_file())
            self.assertTrue(sp.check((d / "999_save.pas").read_text(), live.SANDBOX)["ok"])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
