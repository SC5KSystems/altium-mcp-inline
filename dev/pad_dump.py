"""Print what the offline reader sees for some pads: sizes and shapes per
layer, hole, slot, rotations, and the shapes the DRC builds from them.

    python dev/pad_dump.py board.PcbDoc K1.1 K1.2 [U3.4 ...]

For reports of a pad the offline DRC gets wrong (an annular ring of zero, a
hole touching a pour). Prints nothing but numbers about the named pads.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from offline import geom  # noqa: E402
from offline import layers as L  # noqa: E402
from offline.pcbdoc import PcbDoc  # noqa: E402


def bounds(g):
    return [round(v, 4) for v in g.bounds] if not g.is_empty else None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print(__doc__)
        return 2
    doc = PcbDoc.open(argv[0])
    for ref in argv[1:]:
        des, _, name = ref.partition(".")
        comp = doc.component(des)
        pads = [p for p in doc.pads if p.component == comp.index and (not name or p.name == name)]
        if not pads:
            print(f"{ref}: no such pad")
            continue
        for p in pads:
            print(f"{des}.{p.name}: at ({p.x:.4f}, {p.y:.4f}) mm, layer {L.name(p.layer)}, "
                  f"rotation {p.rotation:.3f}, component rotation {comp.rotation:.3f}, "
                  f"side {comp.side}")
            print(f"  sizes top {tuple(round(v, 4) for v in p.top_size)} {p.top_shape}, "
                  f"mid {tuple(round(v, 4) for v in p.mid_size)} {p.mid_shape}, "
                  f"bottom {tuple(round(v, 4) for v in p.bottom_size)} {p.bottom_shape}, "
                  f"mode {p.mode}")
            print(f"  hole {p.hole:.4f} mm, shape {p.hole_shape}, slot size {p.slot_size:.4f}, "
                  f"slot rotation {p.slot_rotation:.3f}, hole rotation {p.hole_rotation:.3f}, "
                  f"plated {p.plated}, stack record {p.has_stack}")
            h = geom.hole_shape(p)
            print(f"  hole shape box {bounds(h)}")
            for lay in doc.copper_layers:
                if not p.on_layer(lay):
                    continue
                g = geom.pad_shape(p, lay)
                inside = (not g.is_empty) and g.contains(h)
                ring = g.exterior.distance(h) if inside else 0.0
                print(f"  {L.name(lay)}: {p.shape_on(lay)} box {bounds(g)}, hole inside "
                      f"{inside}, ring {ring:.4f} mm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
