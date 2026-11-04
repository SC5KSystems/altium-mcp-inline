"""Autorouter bridge: Specctra DSN export, SES import, net selection, and an
end-to-end route on a fixture when Java and Freerouting are installed.

    python -m unittest discover -s server/tests -p "test_offline_autoroute.py"

The end-to-end test finds Freerouting like the tool does ($FREEROUTING_JAR,
or `python -m offline autoroute --fetch`) and is skipped without it.
"""

import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from offline import autoroute as AR  # noqa: E402
from offline import fixtures  # noqa: E402
from offline import pcbwrite, records as R  # noqa: E402
from offline import specctra as S  # noqa: E402
from offline.synth import Synth  # noqa: E402

try:
    import shapely  # noqa: F401
    import scipy  # noqa: F401
    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False

HIFIVE = fixtures.by_name("HiFive1.B01", ".PcbDoc")


def small_board():
    """Two parts to join on SIG, a power net with a pour, a wide net, odd pads."""
    s = Synth(40, 30, layers=2, copper_mm=0.035)
    for n in ("SIG", "VBAT", "GND", "WIDE", "OTHER"):
        s.net(n)
    s.rule("Clearance", "Clearance", GAP=0.2)
    s.rule("Width", "Width", MINLIMIT=0.15, MAXLIMIT=1.0, PREFEREDWIDTH=0.25)
    s.component("U1", "SOIC", 10, 15)
    s.pad("U1", "1", 8, 15, 0.6, 1.5, net="SIG")
    s.pad("U1", "2", 12, 15, 0.6, 1.5, rotation=45, net="OTHER")
    s.pad("U1", "3", 10, 11, 0.5, 1.2, shape="round", rotation=90, net="OTHER")
    s.component("J1", "HDR", 30, 15)
    s.pad("J1", "1", 30, 15, 1.7, 1.7, layer="MULTILAYER", shape="round", hole=1.0, net="SIG")
    s.pad("J1", "2", 30, 20, 1.7, 1.7, layer="MULTILAYER", shape="rect", hole=1.0, net="VBAT")
    s.pad("U1", "4", 5, 25, 0.6, 1.5, net="VBAT")
    s.component("H1", "HOLE", 35, 5)
    s._later(lambda d: pcbwrite.add(d, R.pad(d, "1", 35, 5, 3.2, 3.2, "MULTILAYER", "round", 3.0,
                                             0.0, None, "H1", plated=False)))
    s.track("Top Layer", 20, 26, 20, 26, 0.3, net="OTHER")          # a dot of copper
    s.track("Bottom Layer", 5, 3, 25, 3, 0.6, net="WIDE")
    s.track("Bottom Layer", 25, 3, 26, 4, 0.6, net="WIDE")
    s.track("Top Layer", 12, 15, 14, 17, 0.25, net="OTHER")
    s.track("Top Layer", 12, 15.3, 11, 17, 0.25, net="OTHER")      # ends off the pad centre
    s.track("Top Layer", 8, 15.4, 8, 17, 0.25, net="SIG")           # likewise, on a routed net
    s.via(14, 17, 0.6, 0.3, net="OTHER")
    s.polygon("Bottom Layer", [(2, 20), (12, 20), (12, 28), (2, 28)], net="GND")
    s.via(5, 22, 0.6, 0.3, net="GND")                                # keeps the pour alive
    s.region("KEEPOUT", [(15, 20), (18, 20), (18, 23), (15, 23)])
    return s


class SExpressions(unittest.TestCase):
    def test_parse(self):
        t = S.parse('(pcb "my board" (parser (string_quote ") (space_in_quoted_tokens on))'
                    ' (a (b 1 2) "x y" -3.5))')
        pcb = S.find(t, "pcb")
        self.assertEqual(pcb[1], "my board")
        self.assertIsInstance(pcb[1], S.Quoted)
        a = S.find(pcb, "a")
        self.assertEqual(S.find(a, "b"), ["b", "1", "2"])
        self.assertEqual(a[2:], ["x y", "-3.5"])

    def test_other_quote_character(self):
        t = S.parse("(pcb (parser (string_quote ')) (net 'a b' (pins X-1)))")
        self.assertEqual(S.find(S.find(t, "pcb"), "net")[1], "a b")

    def test_errors_are_loud(self):
        for bad in ("(a (b)", "(a))", '(a "open)'):
            with self.assertRaises(S.SpecctraError):
                S.parse(bad)


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class Export(unittest.TestCase):
    def setUp(self):
        self.doc = small_board().build()
        self.dsn = S.export(self.doc, ["SIG"])
        self.tree = S.find(S.parse(self.dsn.text), "pcb")

    def test_structure(self):
        st = S.find(self.tree, "structure")
        self.assertEqual([x[1] for x in S.find_all(st, "layer")], ["Top_Layer", "Bottom_Layer"])
        self.assertTrue(S.find(st, "boundary"))
        self.assertEqual(S.find(st, "rule"), ["rule", ["width", "250"], ["clearance", "200"]])
        via = S.find(st, "via")[1]
        self.assertIn(via, self.dsn.via_stacks)
        kinds = [x[0] for x in st if isinstance(x, list)]
        self.assertGreaterEqual(kinds.count("keepout"), 2, "the keep-out region and the hole")

    def test_parts_and_pads(self):
        lib = S.find(self.tree, "library")
        imgs = {x[1]: x for x in S.find_all(lib, "image")}
        self.assertEqual(set(imgs), {"IMG_U1", "IMG_J1", "IMG_H1"})
        pins = {p[2]: p for p in S.find_all(imgs["IMG_U1"], "pin")}
        self.assertEqual(pins["1"][3:5], ["8000", "15000"], "pins sit at board coordinates")
        stacks = {x[1]: x for x in S.find_all(lib, "padstack")}

        def shape(pin):
            return S.find(stacks[pin[1]], "shape")[1][0]
        self.assertEqual(shape(pins["1"]), "rect")
        self.assertEqual(shape(pins["2"]), "polygon", "a 45 degree rectangle")
        self.assertEqual(shape(pins["3"]), "path", "an obround")
        place = S.find_all(S.find(self.tree, "placement"), "component")
        self.assertTrue(all(c[2][2:6] == ["0", "0", "front", "0"] for c in place))

    def test_network_and_wiring(self):
        net = S.find(self.tree, "network")
        nets = {x[1]: x for x in S.find_all(net, "net")}
        self.assertEqual(S.find(nets["SIG"], "pins")[1:], ["U1-1", "J1-1"])
        self.assertNotIn("OTHER", nets, "nets not routed appear only as islands")
        classes = {x[1]: x for x in S.find_all(net, "class")}
        self.assertIn("SIG", classes["route_1"])
        fixed = classes[S.FIXED_CLASS]
        isl = [n for n in nets if n.startswith("_I")]
        self.assertEqual(set(isl), {x for x in fixed[2:] if isinstance(x, str)})
        wiring = S.find(self.tree, "wiring")
        wires = S.find_all(wiring, "wire")
        dots = [w for w in wires if w[1][0] == "polygon"]
        self.assertEqual(len(dots), 1, "the zero-length track goes in as a polygon")
        self.assertTrue(all(S.find(w, "type")[1] == "protect" for w in wires))
        # the OTHER pad, track and via that touch form one island
        other = [w for w in wires if S.find(w, "net") and w[1][0] == "path"
                 and w[1][3:5] == ["12000", "15000"]][0]
        isl_name = S.find(other, "net")[1]
        via = [v for v in S.find_all(wiring, "via") if v[2:4] == ["14000", "17000"]][0]
        self.assertEqual(S.find(via, "net")[1], isl_name)
        self.assertIn("U1-2", S.find(nets[isl_name], "pins"))
        # hairline stubs join off-centre track ends to pad centres, only on
        # nets not routed (a routed net's wiring can come back in the plan)
        stubs = [w for w in wires if w[1][0] == "path" and w[1][2] == "10"]
        self.assertEqual([S.find(w, "net")[1] for w in stubs], [isl_name])
        self.assertEqual(stubs[0][1][3:7], ["12000", "15000", "12000", "15300"])

    def test_pours_left_out_unless_fixed(self):
        self.doc = small_board().build()
        from offline.pour import PourEngine
        eng = PourEngine(self.doc)
        eng.store({p.index: eng.pour(p) for p in eng.pourable()}, "pour")
        a = S.export(self.doc, ["SIG"], pours="repour")
        b = S.export(self.doc, ["SIG"], pours="fixed")
        self.assertEqual(a.stats["fixed"]["pour"], 0)
        self.assertGreater(b.stats["fixed"]["pour"], 0)


SES = """(session test
  (base_design test)
  (routes
    (resolution um 10)
    (parser (host_cad "x") (host_version "1"))
    (library_out
      (padstack "{via}" (shape (circle Top_Layer 6000 0 0)) (shape (circle Bottom_Layer 6000 0 0)))
    )
    (network_out
      (net "SIG"
        (wire (path Top_Layer 2500 80000 150000 80000 180000 200000 180000))
        (via "{via}" 200000 180000)
        (wire (path Bottom_Layer 2500 200000 180000 300000 150000))
        (wire (path Top_Layer 2500 1000 1000 2000 1000) (type protect))
      )
      (net "_I0"
        (wire (path Top_Layer 2500 0 0 1000 1000))
      )
    )
  )
)
"""


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class Import(unittest.TestCase):
    def setUp(self):
        self.doc = small_board().build()
        self.dsn = S.export(self.doc, ["SIG"])
        self.text = SES.format(via=self.dsn.routing_via)

    def test_read(self):
        s = S.read_ses(self.text, self.dsn)
        sig = [w for w in s["wires"] if w["net"] == "SIG"]
        self.assertEqual(sig[0]["points"], [(8.0, 15.0), (8.0, 18.0), (20.0, 18.0)])
        self.assertAlmostEqual(sig[0]["width"], 0.25)
        self.assertEqual(sig[0]["layer"], 1)
        self.assertEqual(sig[1]["layer"], 32)
        self.assertEqual(s["vias"][0]["diameter"], 0.6)
        self.assertEqual(s["vias"][0]["hole"], 0.3)

    def test_plan(self):
        s = S.read_ses(self.text, self.dsn)
        s["wires"] = [w for w in s["wires"] if w["net"] == "SIG"]
        ops, skipped = S.plan_from_session(self.doc, s, self.dsn)
        self.assertEqual([o["op"] for o in ops], ["add_track"] * 3 + ["add_via"])
        self.assertEqual(skipped["fixed"], 1)
        self.assertEqual(ops[3]["low"], "Top Layer")
        ops2, _ = S.plan_from_session(self.doc, s, self.dsn, replace=["SIG"])
        self.assertEqual(ops2[0], {"op": "remove", "kinds": ["track", "arc", "via"], "net": "SIG",
                                   "free": True})
        self.assertEqual(len(ops2), 6, "a replaced net takes even its protected wiring")

    def test_whole_pipeline_from_a_session(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "in.ses").write_text(self.text)
            plan, rep = AR.autoroute(self.doc, ["SIG"], ses_in=tmp / "in.ses", workdir=tmp,
                                     check=True)
            self.assertEqual(rep["connections_before"], 1)
            self.assertEqual(rep["connections_after"], 0)
            self.assertEqual(rep["completion"], 1.0)
            self.assertEqual(rep["skipped_from_session"]["other_nets"], 1)
            self.assertEqual(rep["drc"]["new"], 0, rep["drc"])
        finally:
            shutil.rmtree(tmp)

    def test_unknown_layer_is_an_error(self):
        with self.assertRaises(S.SpecctraError):
            S.read_ses(self.text.replace("Bottom_Layer 2500", "Inner9 2500"), self.dsn)


@unittest.skipUnless(HAVE, "shapely/scipy not installed")
class Selection(unittest.TestCase):
    def test_power_nets(self):
        doc = small_board().build()
        p = AR.power_nets(doc)
        self.assertIn("VBAT", p)
        self.assertIn("GND", p)
        self.assertIn("wide", p["WIDE"])
        self.assertNotIn("SIG", p)
        self.assertNotIn("OTHER", p)

    def test_select(self):
        doc = small_board().build()
        nets, skipped = AR.select_nets(doc)
        self.assertEqual(sorted(nets), ["OTHER", "SIG"], "every open signal net, no power")
        nets, skipped = AR.select_nets(doc, ["*"])
        self.assertNotIn("VBAT", nets)
        self.assertIn("power", skipped["VBAT"])
        self.assertIn("fewer than two pads", skipped["WIDE"])
        nets, _ = AR.select_nets(doc, ["VBAT"])
        self.assertEqual(nets, ["VBAT"], "a power net named exactly is routed")
        nets, _ = AR.select_nets(doc, ["sig"])
        self.assertEqual(nets, ["SIG"], "names match without case")
        with self.assertRaises(AR.AutorouteError):
            AR.select_nets(doc, ["NOPE*"])

    def test_jar_java(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            jar = tmp / "x.jar"
            with zipfile.ZipFile(jar, "w") as z:
                z.writestr("META-INF/MANIFEST.MF", "Main-Class: app.Main\n")
                z.writestr("app/Main.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x45")
            self.assertEqual(AR.jar_java(jar), 25)
        finally:
            shutil.rmtree(tmp)


def _freerouting():
    try:
        jar = AR.find_freerouting()
        return jar, AR.find_java(need=AR.jar_java(jar))
    except AR.AutorouteError:
        return None, None


JAR, JAVA = _freerouting()


def pick_nets(doc, count=4):
    """Signal nets to strip and route back, chosen by rule (no names): two or
    three pads, none on a part with pads closer than 0.5 mm (fine-pitch
    escapes are the router's weak spot, measured separately), 5 to 60 mm of
    free routing; the shortest first."""
    import math
    power = AR.power_nets(doc)
    pads, by_comp = {}, {}
    for p in doc.pads:
        by_comp.setdefault(p.component, []).append(p)
        if p.effective_net is not None:
            pads.setdefault(p.effective_net, []).append(p)
    pitch = {c: min((math.hypot(a.x - b.x, a.y - b.y) for i, a in enumerate(ps)
                     for b in ps[i + 1:]), default=99.0) for c, ps in by_comp.items()}
    length = {}
    for t in doc.tracks:
        if t.effective_net is not None and t.polygon is None and t.component is None:
            length[t.effective_net] = length.get(t.effective_net, 0.0) + t.length
    cands = sorted((length[i], doc.nets[i].name) for i in length
                   if 2 <= len(pads.get(i, [])) <= 3 and doc.nets[i].name not in power
                   and 5 <= length[i] <= 60 and all(pitch[p.component] >= 0.5 for p in pads[i]))
    return [n for _, n in cands[:count]]


@unittest.skipUnless(HAVE and HIFIVE is not None and JAR is not None,
                     "needs the HiFive fixture, Freerouting and a Java that runs it")
class EndToEnd(unittest.TestCase):
    def test_route_apply_drc(self):
        from offline.connect import Connectivity
        from offline.editplan import apply as apply_plan
        from offline.pcbdoc import PcbDoc
        tmp = Path(tempfile.mkdtemp())
        try:
            doc = PcbDoc.open(HIFIVE)
            nets = pick_nets(doc)
            self.assertEqual(len(nets), 4)
            strip = {"version": 1, "ops": [{"op": "remove", "kinds": ["track", "arc", "via"],
                                            "net": n, "free": True} for n in nets]}
            ed, _ = apply_plan(doc, strip)
            board = tmp / "stripped.PcbDoc"
            ed.save(board)
            plan, rep = AR.autoroute(str(board), nets, check=True, workdir=tmp / "w",
                                     jar=JAR, java=JAVA)
            self.assertGreater(rep["connections_before"], 0)
            print(f"\nend-to-end: {rep['connections_before']} open connection(s), "
                  f"completion {rep['completion'] * 100:.1f} %, {rep['added']}, "
                  f"router {rep.get('router_seconds')} s, DRC new {rep['drc']['new']}")
            self.assertGreaterEqual(rep["completion"], 0.5, rep.get("unrouted_after"))
            self.assertEqual(rep["drc"]["new"], 0, rep["drc"]["new_list"])
            after = PcbDoc.open(board)
            apply_plan(after, plan)
            idx = {after.net_index(n) for n in nets}
            self.assertEqual(len(Connectivity(after).unrouted(idx)), rep["connections_after"])
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
