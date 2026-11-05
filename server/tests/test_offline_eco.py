"""Offline ECO tests on the KiCad fixture project (schematics + board), with
schematic changes made to the netlist in memory.

    python -m unittest discover -s server/tests -p "test_offline_eco.py"
"""

import contextlib
import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from offline import eco  # noqa: E402
from offline import fixtures  # noqa: E402
from offline import footprints as F  # noqa: E402
from offline import pcbwrite  # noqa: E402
from offline.editplan import apply as apply_plan  # noqa: E402
from offline.pcbdoc import PcbDoc  # noqa: E402
from offline.sch import Project  # noqa: E402

PRJ = fixtures.by_name("eDP_adapter_dvt1.PrjPcb", ".PrjPcb")
LIB = fixtures.by_name("GW_JTLMS1_CM", ".PcbLib")
ESP = fixtures.by_name("ESP32-WROOM", ".PcbLib")

try:
    import shapely  # noqa: F401
    HAVE_SHAPELY = True
except ImportError:  # pragma: no cover
    HAVE_SHAPELY = False


def two_pin_bridge(doc, netlist):
    """A two-pad part between two nets that both reach other parts."""
    pins = netlist.pin_net()
    count = {}
    for (d, p), n in pins.items():
        count[n] = count.get(n, 0) + 1
    for c in doc.components:
        pads = [p for p in doc.pads if p.component == c.index]
        if len(pads) != 2 or c.designator not in netlist.parts:
            continue
        a, b = (pins.get((c.designator, p.name)) for p in pads)
        if a and b and a != b and count[a] >= 3 and count[b] >= 3 and \
                not a.upper().startswith(("GND", "P")) and not b.upper().startswith(("GND", "P")):
            return c.designator, a, b
    raise AssertionError("no suitable part")


@unittest.skipUnless(PRJ is not None, "fixture project not available")
class Schematic(unittest.TestCase):
    def test_netlist_matches_board(self):
        prj = Project.open(PRJ)
        nl = prj.netlist()
        doc = PcbDoc.open(prj.pcb)
        self.assertEqual(set(nl.parts), {c.designator for c in doc.components})
        board = eco.board_view(doc)["pins"]
        pins = nl.pin_net()
        self.assertEqual(len(pins), 368)
        self.assertTrue(all(board[k] == v for k, v in pins.items()), "every pin on its board net")
        for d, part in nl.parts.items():
            self.assertEqual(part["footprint"], doc.component(d).pattern, d)

    def test_unchanged_project_previews_clean(self):
        nl, prj = eco.load_netlist(PRJ)
        pv = eco.preview(nl, PcbDoc.open(prj.pcb))
        s = pv["summary"]
        self.assertEqual({k: v for k, v in s.items() if k != "relinks"},
                         {k: 0 for k in s if k != "relinks"})
        self.assertEqual([r["designator"] for r in pv["parts"]["source_links_to_update"]], ["C22P"])

    def test_link(self):
        nl, prj = eco.load_netlist(PRJ)
        doc = PcbDoc.open(prj.pcb)
        ed, done = eco.link(nl, doc)
        self.assertEqual(done, ["C22P"])
        self.assertEqual(eco.preview(nl, doc)["summary"]["relinks"], 0)


@unittest.skipUnless(PRJ is not None and HAVE_SHAPELY, "fixture project or shapely not available")
class Changes(unittest.TestCase):
    def setUp(self):
        self.nl, prj = eco.load_netlist(PRJ)
        self.board = prj.pcb
        self.doc = PcbDoc.open(self.board)
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def changed(self):
        return copy.deepcopy(self.nl)

    def test_removed_part_merges_nets_and_leaves_a_gap(self):
        des, a, b = two_pin_bridge(self.doc, self.nl)
        nl = self.changed()
        del nl.parts[des]
        merged = (nl.nets.pop(a) | nl.nets.pop(b)) - {(des, p) for p in ("1", "2")}
        nl.nets[a] = merged
        pv = eco.preview(nl, self.doc)
        self.assertEqual([r["designator"] for r in pv["parts"]["remove"]], [des])
        self.assertEqual(pv["nets"]["merges"], [{"into": a, "from": sorted([a, b]),
                                                 "pins": pv["nets"]["merges"][0]["pins"]}])
        plan, problems, _ = eco.plan(nl, self.doc)
        self.assertEqual([o["op"] for o in plan["ops"]], ["remove_component", "merge_nets"])
        doc = PcbDoc.open(self.board)
        ed, _ = apply_plan(doc, plan)
        g = eco.gaps(doc, [a])
        self.assertEqual(len(g), 1, g)
        comp = self.doc.component(des)
        mid = (comp.x, comp.y)
        at = g[0]["from"]
        self.assertLess(((at[0] - mid[0]) ** 2 + (at[1] - mid[1]) ** 2) ** 0.5, 3.0,
                        "the gap is where the part was")
        self.assertGreater(g[0]["gap_mm"], 0.0)
        out = self.tmp / "after.PcbDoc"
        ed.save(out)
        self.assertEqual(pcbwrite.check_integrity(PcbDoc.open(out)), [])

    def test_rename_and_pin_move(self):
        nl = self.changed()
        name = sorted(n for n in nl.nets if n.startswith("Net"))[0]
        nl.nets["RENAMED_BY_ECO"] = nl.nets.pop(name)
        # move one pin of a busy net to a fresh net
        busy = max((n for n in nl.nets if n != "RENAMED_BY_ECO"), key=lambda n: len(nl.nets[n]))
        pin = sorted(nl.nets[busy])[0]
        nl.nets[busy].discard(pin)
        nl.nets["MOVED_PIN"] = {pin}
        pv = eco.preview(nl, self.doc)
        self.assertEqual(pv["nets"]["renames"], [{"from": name, "to": "RENAMED_BY_ECO"}])
        self.assertEqual(pv["nets"]["pin_moves"], [{"designator": pin[0], "pad": pin[1],
                                                    "from": busy, "to": "MOVED_PIN"}])
        plan, _p, _ = eco.plan(nl, self.doc)
        doc = PcbDoc.open(self.board)
        apply_plan(doc, plan)
        self.assertTrue(doc.has_net("RENAMED_BY_ECO"))
        self.assertFalse(doc.has_net(name))
        self.assertEqual(eco.preview(nl, doc)["summary"]["pin_moves"], 0)

    @unittest.skipUnless(LIB is not None, "fixture PcbLib not available")
    def test_footprint_change_swaps_in_place(self):
        nl = self.changed()
        des = next(d for d, p in nl.parts.items() if len(p["pins"]) == 2 and d.startswith("C"))
        nl.parts[des]["footprint"] = "GW JTLMS1_CM"
        plan, problems, _ = eco.plan(nl, self.doc, [LIB])
        ops = [o for o in plan["ops"] if o["op"] == "swap_footprint"]
        self.assertEqual(len(ops), 1)
        doc = PcbDoc.open(self.board)
        before = {p.name: p.net_name for p in doc.pads if p.component == doc.component(des).index}
        x, y = doc.component(des).x, doc.component(des).y
        ed, results = apply_plan(doc, plan)
        comp = doc.component(des)
        self.assertEqual(comp.pattern, "GW JTLMS1_CM")
        self.assertAlmostEqual(comp.x, x, places=4)
        self.assertAlmostEqual(comp.y, y, places=4)
        after = {p.name: p.net_name for p in doc.pads if p.component == comp.index}
        self.assertEqual(after, before)
        rep = [r for r in results if r["op"] == "swap_footprint"][0]
        self.assertIn("stranded_tracks", rep)
        nl2, _ = eco.load_netlist(PRJ)
        back, problems, _ = eco.plan(nl2, doc, [self.board])
        self.assertEqual([o["op"] for o in back["ops"]], ["swap_footprint"],
                         "the original schematic wants the old footprint back, copied from "
                         "the original board")
        apply_plan(doc, back)
        self.assertEqual(doc.component(des).pattern, self.doc.component(des).pattern)

    @unittest.skipUnless(ESP is not None, "fixture PcbLib not available")
    def test_new_part_is_placed_beside_the_board(self):
        nl = self.changed()
        lib = F.PcbLib.open(ESP)
        name = lib.names()[0]
        nl.parts["U99"] = {"designator": "U99", "footprint": name, "footprints": [name],
                           "comment": "module", "libref": "MOD", "unique_id": "ABCDEFGH",
                           "unique_ids": ["ABCDEFGH"], "sheet": "x", "pins": ["1", "2"],
                           "units": [1]}
        nl.nets.setdefault("GND", set()).add(("U99", "1"))
        nl.nets.setdefault("P3.3V", set()).add(("U99", "2"))
        plan, problems, pv = eco.plan(nl, self.doc, [ESP])
        self.assertEqual(problems, [])
        place = [o for o in plan["ops"] if o["op"] == "place_footprint"]
        self.assertEqual(len(place), 1)
        self.assertGreater(place[0]["x"], max(p[0] for p in self.doc.outline()))
        doc = PcbDoc.open(self.board)
        apply_plan(doc, plan)
        comp = doc.component("U99")
        nets = {p.name: p.net_name for p in doc.pads if p.component == comp.index}
        self.assertEqual(nets["1"], "GND")
        self.assertEqual(nets["2"], "P3.3V")
        self.assertEqual(comp.props.get("SOURCEUNIQUEID"), "\\ABCDEFGH")

    def test_missing_footprint_is_reported_not_invented(self):
        nl = self.changed()
        nl.parts["U98"] = dict(nl.parts[next(iter(nl.parts))], designator="U98",
                               footprint="NO_SUCH_FOOTPRINT")
        plan, problems, _ = eco.plan(nl, self.doc, [])
        self.assertTrue(any("NO_SUCH_FOOTPRINT" in p for p in problems))
        self.assertFalse(any(o["op"] == "place_footprint" for o in plan["ops"]))

    def test_cli(self):
        from offline.__main__ import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["eco", "preview", str(PRJ), "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["summary"]["add"], 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["eco", "gaps", str(self.board), "--json"])
        self.assertEqual(json.loads(buf.getvalue())["gaps"], [])
        self.assertEqual(rc, 0)


@unittest.skipUnless(LIB is not None and ESP is not None, "fixture PcbLibs not available")
class Libraries(unittest.TestCase):
    def test_read_footprints(self):
        esp = F.PcbLib.open(ESP)
        fp = esp.footprint(esp.names()[0])
        self.assertEqual(len(fp.pads), 47)
        gw = F.PcbLib.open(LIB)
        fp = gw.footprint("GW JTLMS1_CM")
        self.assertEqual(sorted(fp.pad_names()), ["1", "2"])
        with self.assertRaises(F.FootprintError):
            gw.footprint("NOPE")


class NotFollowed(unittest.TestCase):
    """Sheet symbols and ports join nets the reader does not follow: the
    preview says so, and no plan is built from such a netlist."""

    def test_ports_are_warned_and_refuse_a_plan(self):
        from types import SimpleNamespace
        from offline.example import board
        from offline.schnet import Finding
        nl2 = SimpleNamespace(nets=[], components={}, findings=[
            Finding("hierarchy_not_followed", "A.SchDoc", "0 sheet symbols, 0 sheet entries, "
                    "2 ports: their connections are not followed"),
            Finding("dangling_wire_end", "A.SchDoc", "wire end touches nothing")])
        nl, prj = eco._from_schnet([], nl2, None, Path("A.SchDoc"))
        self.assertEqual(len(nl.warnings), 1)
        self.assertIn("2 ports", nl.warnings[0])
        doc = board().build()
        self.assertIn(nl.warnings[0], eco.preview(nl, doc)["warnings"])
        with self.assertRaisesRegex(eco.EcoError, "sheet symbols, ports"):
            eco.plan(nl, doc)

    def test_project_without_sheets_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "empty.PrjPcb"
            p.write_text("[Design]\nVersion=1.0\n", encoding="utf-8")
            with self.assertRaisesRegex(eco.EcoError, "lists no schematic sheets"):
                eco.load_netlist(p)

    def test_binary_records_refuse_a_plan(self):
        from types import SimpleNamespace
        from offline.example import board
        from offline.schdoc import SchDoc
        sheet = SchDoc("B.SchDoc", [{"HEADER": "Protel for Windows"}, {"RECORD": "binary"}])
        nl2 = SimpleNamespace(nets=[], components={}, findings=[])
        nl, _ = eco._from_schnet([sheet], nl2, None, Path("B.SchDoc"))
        self.assertEqual(nl.warnings, ["binary_records: B.SchDoc: 1 record(s) in a binary "
                                       "format were not read (pins in them are missing)"])
        with self.assertRaisesRegex(eco.EcoError, "binary"):
            eco.plan(nl, board().build())


if __name__ == "__main__":
    unittest.main()
