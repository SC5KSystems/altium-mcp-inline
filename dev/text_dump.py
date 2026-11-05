"""Print what the offline reader sees for the texts of some parts: string,
anchor, height, rotation, mirroring, stroke width, whether it is shown, and
the box the DRC checks with its distance to the nearest pads on that side.

    python dev/text_dump.py board.PcbDoc C20 B1 [D9 ...]

For text the offline DRC places differently from Altium (a silk-to-mask
violation only one side reports). Prints nothing but those texts and numbers.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from offline import geom  # noqa: E402
from offline import layers as L  # noqa: E402
from offline.pcbdoc import PcbDoc  # noqa: E402


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print(__doc__)
        return 2
    doc = PcbDoc.open(argv[0])
    ox, oy = doc.origin
    print(f"board origin ({ox:.4f}, {oy:.4f}) mm; positions below as stored, then from the origin")
    for des in argv[1:]:
        comp = doc.component(des)
        texts = [t for t in doc.texts if t.component == comp.index]
        if not texts:
            print(f"{des}: no texts")
        for t in texts:
            role = "designator" if t.is_designator else "comment" if t.is_comment else "text"
            print(f"{des} {role} {t.text!r}: at ({t.x:.4f}, {t.y:.4f}) / ({t.x - ox:.4f}, "
                  f"{t.y - oy:.4f}), layer {L.name(t.layer)}, height {t.height:.4f}, rotation "
                  f"{t.rotation:.3f}, mirrored {t.mirrored}, stroke {t.stroke_width:.4f}, "
                  f"hidden {t.hidden}, record bytes {len(t.subs[0])}")
            fr = t.frame
            print(f"  font {t.font_type}, justification {t.justification}, frame "
                  + (f"{fr[0]:.4f} x {fr[1]:.4f} margin {fr[2]:.4f}" if fr else "none"))
            g = geom.text_box(t)
            print(f"  box {[round(v, 4) for v in g.bounds]}")
            copper = L.TOP if t.layer == L.TOP_OVERLAY else L.BOTTOM
            near = []
            for p in doc.pads:
                if p.on_layer(copper):
                    s = geom.pad_shape(p, copper)
                    if not s.is_empty:
                        near.append((s.distance(g), p))
            near.sort(key=lambda x: x[0])
            for d, p in near[:3]:
                owner = doc.components[p.component].designator if p.component is not None else "Free"
                print(f"  pad {owner}-{p.name} at ({p.x:.4f}, {p.y:.4f}): {d:.4f} mm from the box")
    return 0


if __name__ == "__main__":
    sys.exit(main())
