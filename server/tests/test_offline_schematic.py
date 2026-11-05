"""The schematic reader, netlist, comparison, rule checks and footprint check
(ported from the phase-6 branch, 98b87ff).

Synthetic sheets are built from records the way a .SchDoc stores them, so every
connectivity rule is pinned down without Altium. Fixture tests use KiCad's
public test project; real-file tests run when ALTIUM_TEST_PRJPCB points at a
project (and ALTIUM_TEST_PCBDOC at its board).

    python -m unittest discover -s server/tests -p "test_offline_schematic.py"
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from offline import fixtures, schtools  # noqa: E402
from offline.schdoc import SchDoc, parse_records  # noqa: E402
from offline.schnet import build_netlist  # noqa: E402

HEADER = {"HEADER": "Protel for Windows - Schematic Capture Binary File Version 5.0"}


class Sheet:
    """Builds a sheet's records. Coordinates in file units (10 mil); pins are 2 units long."""

    def __init__(self, name="A.SchDoc"):
        self.name = name
        self.recs = []

    def _add(self, **kw):
        self.recs.append({k.upper().replace("_", "."): str(v) for k, v in kw.items()})

    def part(self, des, pins, libref="X", comment="", part_id=1, units=1):
        """pins: (number, name, base_x, base_y, orientation[, owner_part])"""
        idx = len(self.recs)
        self._add(record=1, libreference=libref, currentpartid=part_id, partcount=units + 1)
        self._add(record=34, ownerindex=idx, text=des, name="Designator")
        if comment:
            self._add(record=41, ownerindex=idx, name="Comment", text=comment)
        for pin in pins:
            num, name, x, y, orient = pin[:5]
            owner_part = pin[5] if len(pin) > 5 else 1
            self.recs.append({"RECORD": "2", "OWNERINDEX": str(idx), "OWNERPARTID": str(owner_part),
                              "DESIGNATOR": num, "NAME": name, "LOCATION.X": str(x), "LOCATION.Y": str(y),
                              "PINLENGTH": "2", "PINCONGLOMERATE": str(orient), "ELECTRICAL": "4"})
        return self

    def wire(self, *pts):
        r = {"RECORD": "27", "LOCATIONCOUNT": str(len(pts))}
        for i, (x, y) in enumerate(pts, 1):
            r["X%d" % i], r["Y%d" % i] = str(x), str(y)
        self.recs.append(r)
        return self

    def label(self, x, y, text):
        self.recs.append({"RECORD": "25", "LOCATION.X": str(x), "LOCATION.Y": str(y), "TEXT": text})
        return self

    def power(self, x, y, text):
        self.recs.append({"RECORD": "17", "LOCATION.X": str(x), "LOCATION.Y": str(y), "TEXT": text, "STYLE": "4"})
        return self

    def junction(self, x, y):
        self.recs.append({"RECORD": "29", "LOCATION.X": str(x), "LOCATION.Y": str(y)})
        return self

    def doc(self):
        return SchDoc(self.name, [HEADER] + self.recs)


def netlist(*sheets, scope="global", t_connects=False):
    return build_netlist([s.doc() for s in sheets], scope, t_connects=t_connects)


def nets_by_pins(nl):
    return {n.name: sorted("%s.%s" % (p.designator, p.pin) for p in n.pins) for n in nl.nets}


def kinds(nl):
    return sorted(f.kind for f in nl.findings)


class RecordTests(unittest.TestCase):
    def test_parse_records_round_trip(self):
        def enc(text):
            body = text.encode("latin-1") + b"\x00"
            return bytes([len(body) & 0xFF, len(body) >> 8 & 0xFF, 0, 0]) + body
        stream = enc("|HEADER=Protel|") + enc("|RECORD=25|Location.X=10|Text=VCC|%UTF8%Text=VCC2|")
        recs = parse_records(stream)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[1]["LOCATION.X"], "10")
        self.assertEqual(recs[1]["TEXT"], "VCC2")      # the UTF-8 copy wins

    def test_pin_electrical_end(self):
        s = Sheet().part("U1", [("1", "A", 0, 0, 0), ("2", "B", 0, 0, 1), ("3", "C", 0, 0, 2), ("4", "D", 0, 0, 3)])
        d = s.doc()
        hot = {p.number: (p.hot[0] // 100000, p.hot[1] // 100000) for p in d.pins}
        self.assertEqual(hot, {"1": (2, 0), "2": (0, 2), "3": (-2, 0), "4": (0, -2)})

    def test_pins_of_other_parts_are_ignored(self):
        s = Sheet().part("U1", [("1", "A", 0, 0, 0, 1), ("2", "B", 0, 10, 0, 2)], part_id=1)
        self.assertEqual([p.number for p in s.doc().pins], ["1"])


class ConnectivityTests(unittest.TestCase):
    def two_resistors(self):
        # R1.1 electrical end at (2,0), R2.1 at (12,0)
        return Sheet().part("R1", [("1", "1", 0, 0, 0), ("2", "2", 0, -10, 3)]) \
                      .part("R2", [("1", "1", 14, 0, 2), ("2", "2", 14, -10, 3)])

    def test_wire_joins_pins_and_label_names_net(self):
        s = self.two_resistors().wire((2, 0), (12, 0)).label(6, 0, "MID")
        self.assertEqual(nets_by_pins(netlist(s))["MID"], ["R1.1", "R2.1"])

    def test_t_without_junction_does_not_connect(self):
        s = self.two_resistors().wire((2, 0), (12, 0))
        s.part("R3", [("1", "1", 7, 12, 3), ("2", "2", 7, 20, 1)]).wire((7, 10), (7, 0))
        nl = netlist(s)
        self.assertIn("t_without_junction", kinds(nl))
        self.assertNotIn("R3.1", sum(nets_by_pins(nl).values(), []) and nets_by_pins(nl)["NetR1_1"])
        self.assertIn("R3.1", nets_by_pins(netlist(s, t_connects=True))["NetR1_1"])
        s.junction(7, 0)
        self.assertIn("R3.1", nets_by_pins(netlist(s))["NetR1_1"])

    def test_plain_crossing_does_not_connect(self):
        s = Sheet().part("R1", [("1", "1", -2, 5, 0)]).part("R2", [("1", "1", 12, 5, 2)]) \
                   .part("R3", [("1", "1", 5, -2, 1)]).part("R4", [("1", "1", 5, 12, 3)])
        s.wire((0, 5), (10, 5)).wire((5, 0), (5, 10))
        nets = nets_by_pins(netlist(s, t_connects=True))
        self.assertEqual(nets["NetR1_1"], ["R1.1", "R2.1"])
        s.junction(5, 5)
        self.assertEqual(nets_by_pins(netlist(s))["NetR1_1"], ["R1.1", "R2.1", "R3.1", "R4.1"])

    def test_pin_end_inside_a_wire_connects(self):
        s = self.two_resistors().wire((2, 0), (12, 0)).part("R3", [("1", "1", 6, 2, 3)])   # end at (6,0)
        self.assertIn("R3.1", nets_by_pins(netlist(s))["NetR1_1"])

    def test_label_and_power_port_of_one_name_join_across_sheets(self):
        a = Sheet("A.SchDoc").part("R1", [("1", "1", 0, 0, 0)]).wire((2, 0), (4, 0)).label(4, 0, "GND")
        b = Sheet("B.SchDoc").part("R2", [("1", "1", 0, 0, 0)]).wire((2, 0), (4, 0)).power(4, 0, "GND")
        self.assertEqual(nets_by_pins(netlist(a, b))["GND"], ["R1.1", "R2.1"])

    def test_flat_scope_keeps_labels_on_their_sheet(self):
        a = Sheet("A.SchDoc").part("R1", [("1", "1", 0, 0, 0)]).wire((2, 0), (4, 0)).label(4, 0, "SIG")
        b = Sheet("B.SchDoc").part("R2", [("1", "1", 0, 0, 0)]).wire((2, 0), (4, 0)).label(4, 0, "SIG")
        nl = netlist(a, b, scope="flat")
        self.assertEqual(sum(1 for n in nl.nets if n.name == "SIG"), 2)
        self.assertIn("duplicate_net_name", kinds(nl))
        self.assertEqual(sum(1 for n in netlist(a, b).nets if n.name == "SIG"), 1)

    def test_unnamed_net_takes_lowest_designator(self):
        s = Sheet().part("R10", [("2", "2", 0, 0, 0)]).part("R9", [("1", "1", 10, 0, 2)]).wire((2, 0), (8, 0))
        self.assertIn("NetR9_1", nets_by_pins(netlist(s)))

    def test_findings(self):
        s = self.two_resistors().wire((2, 0), (12, 0)).label(6, 0, "")
        s.label(50, 50, "LOST")
        found = kinds(netlist(s))
        for k in ("empty_net_label", "floating_net_label", "unconnected_pin", "single_pin_net"):
            self.assertIn(k, found)


    def test_units_of_one_part_are_not_duplicates(self):
        s = Sheet().part("U1", [("1", "A", 0, 0, 0, 1)], units=2, part_id=1)
        s.part("U1", [("2", "B", 0, 20, 0, 2)], units=2, part_id=2)
        self.assertNotIn("duplicate_designator", kinds(netlist(s)))
        s.part("U1", [("3", "C", 0, 40, 0, 2)], units=2, part_id=2)
        self.assertIn("duplicate_designator", kinds(netlist(s)), "unit B twice")
        r = Sheet().part("R1", [("1", "", 0, 0, 0)]).part("R1", [("1", "", 0, 20, 0)])
        self.assertIn("duplicate_designator", kinds(netlist(r)))


class CompareTests(unittest.TestCase):
    def plain(self, nets, comps=None):
        comps = comps or {d: {"designator": d} for pins in nets.values() for d, _ in pins}
        return schtools.Plain({k: set(v) for k, v in nets.items()}, comps, "x", "test")

    def test_identical(self):
        a = self.plain({"N1": [("R1", "1"), ("R2", "1")]})
        self.assertTrue(schtools.compare(a, a)["identical"])

    def test_rename_split_merge_and_pins(self):
        a = self.plain({"A": [("R1", "1"), ("R2", "1")], "B": [("R3", "1"), ("R4", "1"), ("R5", "1")],
                        "C": [("R6", "1"), ("R7", "1")], "D": [("R8", "1"), ("R9", "1")]})
        b = self.plain({"A2": [("R1", "1"), ("R2", "1"), ("R10", "1")], "B1": [("R3", "1"), ("R4", "1")],
                        "B2": [("R5", "1"), ("R11", "1")], "CD": [("R6", "1"), ("R7", "1"), ("R8", "1"), ("R9", "1")]})
        r = schtools.compare(a, b)
        self.assertEqual(r["nets_renamed"], [{"from": "A", "to": "A2"}])
        self.assertEqual(r["nets_split"], [{"net": "B", "into": ["B1", "B2"]}])
        self.assertEqual(r["nets_merged"], [{"net": "CD", "from": ["C", "D"]}])
        self.assertEqual([x["pin"] for x in r["pins_added"]], ["R10.1", "R11.1"])
        self.assertEqual(r["components_added"], ["R10", "R11"])

    def test_single_pin_nets_count_as_unconnected(self):
        a = self.plain({"N1": [("R1", "1"), ("R2", "1")], "NetR3_1": [("R3", "1")]})
        b = self.plain({"N1": [("R1", "1"), ("R2", "1")]}, {"R1": {}, "R2": {}, "R3": {}})
        self.assertTrue(schtools.compare(a, b)["identical"])


class BomAndRuleTests(unittest.TestCase):
    def test_mpn_search_terms(self):
        self.assertEqual(schtools.mpn_search_terms("BTS70081EPPXUMA1"),
                         ["BTS70081EPPXUMA1", "BTS70081EPP", "BTS70081", "BTS7008"])
        self.assertEqual(schtools.mpn_search_terms("STM32F103C8T6-TR")[:2], ["STM32F103C8T6-TR", "STM32F103C8T6"])

    def profet_sheet(self, with_sense_resistor=True):
        # U1 high-side switch: IS (pin 4) to ADC U2 through R2, R1 from IS to GND, OUT on two pins
        s = Sheet().part("U1", [("4", "IS", 0, 0, 2), ("8", "OUT", 20, 0, 0), ("9", "OUT", 20, -10, 0)],
                         libref="HSW-1", comment="HSW-1")
        s.part("U2", [("1", "AIN0", -40, 0, 0)], libref="ADC-8", comment="ADC-8")
        s.part("R2", [("1", "1", -10, 0, 0), ("2", "2", -20, 0, 2)])                       # ends at -8 and -22
        s.wire((-2, 0), (-8, 0)).wire((-22, 0), (-38, 0))
        if with_sense_resistor:
            s.part("R1", [("1", "1", -5, 4, 3), ("2", "2", -5, -6, 3)]).power(-5, -8, "GND")   # 1 at (-5,2)
            s.wire((-5, 2), (-5, 0)).junction(-5, 0)     # a T needs its junction
        s.wire((22, 0), (22, -10))
        return s

    RULES = [
        {"parts": "HSW", "pin": "IS", "expect": "resistor_to", "net": "GND"},
        {"parts": "HSW", "pin": "OUT", "expect": "same_net"},
        {"parts": "HSW", "pin": "IS", "expect": "connects_to", "part": "ADC", "via": "R"},
        {"parts": "NOPE", "pin": "X", "expect": "connected"},
        {"parts": "HSW", "pin": "IS", "expect": "bogus"},
    ]

    def run_rules(self, sheet):
        docs = [sheet.doc()]
        plain = schtools._from_netlist(build_netlist(docs, "global"), "x", "test")
        return [r["status"] for r in schtools.check_rules(plain, self.RULES)]

    def test_rules_pass_on_a_complete_stage(self):
        self.assertEqual(self.run_rules(self.profet_sheet()), ["pass", "pass", "pass", "no_match", "error"])

    def test_missing_sense_resistor_fails(self):
        self.assertEqual(self.run_rules(self.profet_sheet(False))[0], "fail")

    def test_dual_anode_diode_counts_as_two_terminal(self):
        # D1 has two anode pins on GND and its cathode on the output: three pins, two nets
        s = Sheet().part("U1", [("8", "OUT", 0, 0, 0)], libref="HSW-1")
        s.part("D1", [("1", "A1", 10, 4, 3), ("2", "A2", 12, 4, 3), ("3", "K", 4, 0, 2)])   # K end at (2,0)
        s.power(10, 0, "GND").power(12, 0, "GND")                                         # A ends at (10,2),(12,2)
        s.wire((10, 2), (10, 0)).wire((12, 2), (12, 0))
        plain = schtools._from_netlist(build_netlist([s.doc()], "global"), "x", "test")
        r = schtools.check_rules(plain, [{"parts": "HSW", "pin": "OUT", "expect": "diode_to", "net": "GND"}])
        self.assertEqual(r[0]["status"], "pass", r[0]["detail"])


PRJ = os.environ.get("ALTIUM_TEST_PRJPCB", "")
PCB = os.environ.get("ALTIUM_TEST_PCBDOC", "")


LIB = fixtures.by_name("GW_JTLMS1_CM", ".PcbLib")
EDP = fixtures.by_name("eDP_adapter_dvt1.PrjPcb", ".PrjPcb")


class FootprintTests(unittest.TestCase):
    @unittest.skipUnless(LIB, "fixture library not available")
    def test_read_pcblib(self):
        from offline.fpcheck import read_pcblib
        fps = read_pcblib(LIB)
        self.assertTrue(fps)
        for fp in fps.values():
            self.assertTrue(fp.pads, fp.name)
            self.assertEqual(fp.source, str(LIB))

    def test_pins_against_pads(self):
        from offline import fpcheck as footprints
        from offline.fpcheck import Footprint, SchPart
        sp = SchPart("U1", ["AAAA"], "A.SchDoc", "", {"1", "2", "3"}, [("FP", True, False)], "X")
        lib = {"FP": Footprint("FP", ["1", "2", "4", ""], "lib")}
        import tempfile
        orig = footprints.schematic_parts, footprints.read_pcblib
        with tempfile.TemporaryDirectory() as tmp:
            prj, lib_file = Path(tmp, "p.PrjPcb"), Path(tmp, "local.PcbLib")
            prj.write_text("[Document1]\nDocumentPath=local.PcbLib\n", encoding="utf-8")
            lib_file.write_bytes(b"")
            try:
                footprints.schematic_parts = lambda sheets: {"U1": sp}
                footprints.read_pcblib = lambda p: lib
                row = footprints.check(prj)["parts"][0]
                lib_file.unlink()           # a library footprint whose PcbLib is gone
                missing = footprints.check(prj)["parts"][0]
            finally:
                footprints.schematic_parts, footprints.read_pcblib = orig
        self.assertEqual(missing["status"], "error")
        self.assertIn("in none of the project's PcbLibs", missing["detail"])
        self.assertEqual(row["status"], "error")
        self.assertIn("pins with no pad: 3", row["detail"])
        self.assertIn("pads with no pin (left without a net): 4", row["detail"])


@unittest.skipUnless(PRJ and Path(PRJ).exists(), "set ALTIUM_TEST_PRJPCB to a .PrjPcb to run")
class RealProjectTests(unittest.TestCase):
    def test_footprints_resolve(self):
        from offline import fpcheck as footprints
        res = footprints.check(PRJ)
        self.assertEqual(sum(res["counts"].values()), len(res["parts"]))
        self.assertTrue(all(r["status"] != "error" or "detail" in r for r in res["parts"]))

    def test_project_reads(self):
        src = schtools.load_any(PRJ)
        s = src.netlist.summary()
        self.assertGreater(s["nets"], 0)
        self.assertGreater(s["pins"], 0)
        self.assertNotIn("duplicate_net_name", s["findings"])

    @unittest.skipUnless(PCB and Path(PCB).exists(), "set ALTIUM_TEST_PCBDOC too")
    def test_against_its_board(self):
        r = schtools.compare(schtools.load_any(PRJ), schtools.load_any(PCB))
        self.assertGreater(r["pins_compared"], 0)
        self.assertFalse(r["nets_split"] or r["nets_merged"], "\n".join(schtools.format_compare(r)))


@unittest.skipUnless(EDP, "fixture project not available")
class FixtureProject(unittest.TestCase):
    """KiCad's public test project: its schematic and its board."""

    def test_schematic_matches_its_board(self):
        prj = Path(EDP)
        board = prj.with_name("eDP_adapter_dvt1.PcbDoc")
        r = schtools.compare(schtools.load_any(prj), schtools.load_any(board))
        self.assertGreater(r["pins_compared"], 300)
        self.assertFalse(r["nets_split"] or r["nets_merged"], "\n".join(schtools.format_compare(r)))

    def test_update_preview(self):
        from offline import fpcheck
        res = fpcheck.check(EDP)
        self.assertEqual(res["unreadable_libraries"], [])
        self.assertEqual(res["counts"]["error"], 0)
        up = res["update_preview"]
        # the one part whose unique id on the board is stale (eco preview lists it as a relink)
        self.assertEqual([r["designator"] for r in up["remove"]], ["C22P"])
        self.assertEqual([r["designator"] for r in up["add"]], ["C22P"])


if __name__ == "__main__":
    unittest.main()
