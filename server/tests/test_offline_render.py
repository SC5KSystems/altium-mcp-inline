"""Renderer: PNG and SVG output, highlight, zoom, ratsnest, markers, heat, diff.

    python -m unittest discover -s server/tests -p "test_offline_render.py"
"""

import json
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from offline import fixtures, pcbwrite, pdn, records as R, render  # noqa: E402
from offline.pcbdoc import PcbDoc  # noqa: E402
from offline.synth import Synth  # noqa: E402

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

needs_pil = unittest.skipUnless(Image, "Pillow not installed")


def board():
    s = Synth(40, 30)
    s.net("SIG")
    s.net("GND")
    s.component("U1", "X", 5, 10)
    s.component("U2", "Y", 30, 20)
    s.pad("U1", "1", 5, 5, 1.5, 1.5, net="SIG")
    s.pad("U2", "1", 30, 20, 1.5, 1.5, net="SIG")
    s.pad("U1", "2", 5, 15, 1.5, 1.5, net="GND")
    s.pad("U2", "2", 30, 10, 1.5, 1.5, net="GND")
    s.track("Top Layer", 5, 5, 15, 5, 0.4, net="SIG")
    s.track("Bottom Layer", 5, 15, 30, 10, 0.6, net="GND")
    s.via(5, 15, net="GND")
    s.via(30, 10, net="GND")
    return s


@needs_pil
class RenderTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.doc = board().build()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def pixels(self, path):
        return np.array(Image.open(path).convert("RGB"))

    def test_png_and_svg(self):
        rep = render.render(self.doc, self.dir / "a.png", width=800)
        self.assertEqual(rep["size_px"][0], 800)
        px = self.pixels(self.dir / "a.png")
        top = np.all(np.abs(px.astype(int) - (219, 37, 37)) < 60, axis=2)
        self.assertGreater(top.sum(), 50)
        render.render(self.doc, self.dir / "a.svg")
        root = ET.parse(self.dir / "a.svg").getroot()
        self.assertTrue(root.tag.endswith("svg"))
        self.assertIn("U1", (self.dir / "a.svg").read_text())

    def test_highlight_dims_other_nets(self):
        render.render(self.doc, self.dir / "plain.png", width=600)
        render.render(self.doc, self.dir / "hi.png", nets=["SIG"], width=600)
        a, b = self.pixels(self.dir / "plain.png"), self.pixels(self.dir / "hi.png")
        gnd_px = (b.astype(int).sum(axis=2) < a.astype(int).sum(axis=2) - 60).sum()
        self.assertGreater(gnd_px, 100)

    def test_zoom_box(self):
        rep = render.render(self.doc, self.dir / "z.png", around="U2", margin=3, width=400)
        x0, y0, x1, y1 = rep["box"]
        self.assertLess(x1 - x0, 10)
        self.assertTrue(x0 < 30 < x1 and y0 < 10 < y1)

    def test_ratsnest_and_markers(self):
        marks = self.dir / "m.json"
        marks.write_text(json.dumps({"violations": [{"x": 15, "y": 5, "rule": "gap"}]}))
        scene = render.Scene(self.doc, ratsnest=True, markers=render.load_markers(marks))
        rats = scene.rats()
        self.assertEqual(len(rats), 1)
        self.assertEqual(rats[0]["net_name"], "SIG")
        render.render_png(scene, self.dir / "r.png", width=600)
        px = self.pixels(self.dir / "r.png")
        self.assertTrue(np.all(px == (255, 0, 255), axis=2).any())

    def test_diff_reports_added_copper(self):
        a = board().save(self.dir / "a.PcbDoc")
        b = PcbDoc.open(self.dir / "a.PcbDoc")
        pcbwrite.add(b, R.track(b, "Top Layer", 15, 5, 30, 20, 0.4, "SIG"))
        rep = render.diff_png(a, b, self.dir / "d.png", width=1600)
        added = rep["layers"]["Top Layer"]["added_mm2"]
        self.assertAlmostEqual(added, ((15 ** 2 + 15 ** 2) ** 0.5) * 0.4, delta=1.5)
        self.assertEqual(rep["layers"]["Top Layer"]["removed_mm2"], 0.0)

    def test_heat_overlay(self):
        r = pdn.solve(self.doc, "GND", ["U1.2"], ["U2.2"], 2.0, res=0.1)
        rep = render.pdn_png(self.doc, r, self.dir / "h.png", width=600)
        self.assertGreater(rep["vmax_a_per_mm2"], 0)

    @unittest.skipUnless(fixtures.boards(), "no fixture boards")
    def test_fixture_renders(self):
        f = fixtures.boards()[0]
        rep = render.render(PcbDoc.open(f), self.dir / "f.png", width=500)
        self.assertTrue(Path(rep["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
