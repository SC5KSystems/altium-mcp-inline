"""KiCad's Altium importer as a second, independent reader of what we write.

    python -m unittest discover -s server/tests -p "test_offline_kicad.py"

Skips unless KiCad's Python module is importable by $KICAD_PYTHON (default
python3.12) and fixture boards are cached. This proves KiCad parses the
output and sees the expected changes; it does not prove Altium opens it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))

from offline import fixtures  # noqa: E402
from offline.edits import Editor  # noqa: E402
from offline.pcbdoc import PcbDoc  # noqa: E402
from offline import pcbwrite  # noqa: E402

KICAD_PY = os.environ.get("KICAD_PYTHON", "python3.12")


def kicad_available():
    try:
        r = subprocess.run([KICAD_PY, "-c", "import pcbnew"], capture_output=True, timeout=120)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


HAVE = kicad_available() and bool(fixtures.boards())


def kicad_counts(*paths):
    r = subprocess.run([KICAD_PY, str(REPO / "dev" / "kicad_read.py"), *map(str, paths)],
                       capture_output=True, text=True, timeout=900)
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    if not line:
        raise AssertionError(f"kicad_read produced no JSON: {r.stderr[-500:]}")
    return json.loads(line[-1])


@unittest.skipUnless(HAVE, "KiCad pcbnew or fixture boards not available")
class KicadCrossCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def test_noop_save_reads_the_same(self):
        pairs = []
        for f in fixtures.boards():
            out = self.dir / f"noop_{f.parent.name}_{f.name}"
            pcbwrite.save(PcbDoc.open(f), out)
            pairs.append((f, out))
        res = kicad_counts(*[p for pair in pairs for p in pair])
        for f, out in pairs:
            with self.subTest(f.name):
                a, b = res[str(f)], res[str(out)]
                self.assertNotIn("error", b, b)
                self.assertEqual(a, b)

    def test_edits_show_up(self):
        for f in fixtures.boards():
            doc = PcbDoc.open(f)
            if len(doc.components) < 2 or not doc.nets:
                continue
            with self.subTest(f.name):
                ed = Editor(doc)
                dups = doc.duplicate_designators
                comp = max((c for c in doc.components if c.designator not in dups),
                           key=lambda c: sum(1 for p in doc.pads if p.component == c.index))
                npads = sum(1 for p in doc.pads if p.component == comp.index)
                nvias = sum(1 for v in doc.vias if v.component == comp.index)
                ed.remove_component(comp.designator)
                x, y = doc.outline()[0]
                ed.add_track("Top Layer", x + 1, y + 1, x + 3, y + 1, 0.3, doc.nets[0].name)
                ed.add_via(x + 3, y + 1, 0.6, 0.3, doc.nets[0].name)
                ed.rename_net(doc.nets[0].name, "RENAMED_BY_TEST")
                out = self.dir / f"edit_{f.parent.name}_{f.name}"
                ed.save(out)
                res = kicad_counts(f, out)
                a, b = res[str(f)], res[str(out)]
                self.assertNotIn("error", b, b)
                self.assertEqual(b["footprints"], a["footprints"] - 1)
                self.assertEqual(b["pads"], a["pads"] - npads)
                self.assertEqual(b["vias"], a["vias"] - nvias + 1)
                self.assertIn("RENAMED_BY_TEST", b["net_names"])
                self.assertNotIn(comp.designator, b["references"])

    def test_stored_pours_read_the_same(self):
        from offline.pour import PourEngine
        pairs = []
        for f in fixtures.boards():
            doc = PcbDoc.open(f)
            eng = PourEngine(doc)
            if not eng.pourable():
                continue
            out = self.dir / f"poured_{f.parent.name}_{f.name}"
            eng.store(eng.repour_all("clean"), "pour").save(out)
            pairs.append((f, out))
        res = kicad_counts(*[p for pair in pairs for p in pair])
        for f, out in pairs:
            with self.subTest(f.name):
                a, b = res[str(f)], res[str(out)]
                self.assertNotIn("error", b, b)
                self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
