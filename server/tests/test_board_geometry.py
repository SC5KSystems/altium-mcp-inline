"""Offline tests for server/board_geometry.py.

No Altium needed. Run from the repo root:

    python -m unittest discover -s server/tests -p "test_board_geometry.py"
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import board_geometry as bg  # noqa: E402

TOL = 1e-6


def fields(line):
    return line.split("|")


class UnitsTest(unittest.TestCase):
    def test_mm_to_mils(self):
        self.assertAlmostEqual(bg.to_mils(25.4, "mm"), 1000.0, places=9)
        self.assertAlmostEqual(bg.to_mils(100, "mm"), 3937.007874015748, places=9)
        self.assertEqual(bg.to_mils(250, "mil"), 250.0)

    def test_unit_names(self):
        self.assertEqual(bg.normalize_units("MM"), "mm")
        self.assertEqual(bg.normalize_units("mils"), "mil")
        with self.assertRaises(bg.GeometryError):
            bg.normalize_units("inch")
        with self.assertRaises(bg.GeometryError):
            bg.normalize_units(None)

    def test_rejects_non_numbers(self):
        for bad in ("abc", None, float("nan"), float("inf"), True):
            with self.assertRaises(bg.GeometryError):
                bg.to_mils(bad, "mil")

    def test_fmt(self):
        self.assertEqual(bg.fmt(3937.007874015748), "3937.007874")
        self.assertEqual(bg.fmt(-0.0000001), "0.000000")
        self.assertEqual(bg.fmt(-12.5), "-12.500000")


class LayerTest(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(bg.normalize_layer("top"), "Top Layer")
        self.assertEqual(bg.normalize_layer("Keep-Out"), "Keep Out Layer")
        self.assertEqual(bg.normalize_layer("multi-layer"), "Multi Layer")
        self.assertEqual(bg.normalize_layer("mech1"), "Mechanical 1")
        self.assertEqual(bg.normalize_layer("Mechanical 13"), "Mechanical 13")
        self.assertEqual(bg.normalize_layer("M15"), "Mechanical 15")

    def test_custom_names_pass_through(self):
        self.assertEqual(bg.normalize_layer("Board Outline"), "Board Outline")
        self.assertEqual(bg.normalize_layer("GND Plane"), "GND Plane")

    def test_rejects_separators(self):
        for bad in ("Top|Layer", "Top\nLayer", "", None):
            with self.assertRaises(bg.GeometryError):
                bg.normalize_layer(bad)


class RectangleTest(unittest.TestCase):
    def test_plain_rectangle(self):
        segs = bg.rectangle_outline(4000, 3000, 100, 200)
        self.assertEqual(len(segs), 4)
        self.assertTrue(all(s["kind"] == "line" for s in segs))
        self.assertEqual(bg.outline_bbox(segs), (100, 200, 4100, 3200))
        self.assertAlmostEqual(bg.signed_area(segs), 12_000_000, delta=TOL)
        self.assertAlmostEqual(bg.perimeter(segs), 14000, delta=TOL)

    def test_rounded_rectangle(self):
        w, h, r = 4000.0, 3000.0, 250.0
        segs = bg.rectangle_outline(w, h, 0, 0, r)
        self.assertEqual(len(segs), 8)
        self.assertEqual(sum(s["kind"] == "arc" for s in segs), 4)
        self.assertAlmostEqual(bg.signed_area(segs), w * h - (4 - math.pi) * r * r, delta=1e-3)
        left, bottom, right, top = bg.outline_bbox(segs)
        self.assertAlmostEqual(left, 0, delta=TOL)
        self.assertAlmostEqual(bottom, 0, delta=TOL)
        self.assertAlmostEqual(right, w, delta=TOL)
        self.assertAlmostEqual(top, h, delta=TOL)
        self.assertAlmostEqual(bg.perimeter(segs), 2 * (w + h) - 8 * r + 2 * math.pi * r, delta=1e-6)
        bg.check_simple(segs)

    def test_arcs_meet_their_neighbours(self):
        segs = bg.rectangle_outline(4000, 3000, 0, 0, 250)
        for i, s in enumerate(segs):
            if s["kind"] != "arc":
                continue
            nxt = segs[(i + 1) % len(segs)]
            sx, sy = bg.arc_point(s, s["start_angle"])
            ex, ey = bg.arc_point(s, s["end_angle"])
            self.assertAlmostEqual(sx, s["x"], delta=1e-9)
            self.assertAlmostEqual(sy, s["y"], delta=1e-9)
            self.assertAlmostEqual(ex, nxt["x"], delta=1e-9)
            self.assertAlmostEqual(ey, nxt["y"], delta=1e-9)
            self.assertAlmostEqual(bg.arc_sweep(s), 90.0, delta=1e-9)

    def test_stadium_drops_zero_length_edges(self):
        segs = bg.rectangle_outline(4000, 1000, 0, 0, 500)
        self.assertEqual(len(segs), 6)
        self.assertAlmostEqual(bg.signed_area(segs), 3000 * 1000 + math.pi * 500 ** 2, delta=1e-3)

    def test_circle(self):
        segs = bg.rectangle_outline(1000, 1000, 0, 0, 500)
        self.assertEqual(len(segs), 4)
        self.assertTrue(all(s["kind"] == "arc" for s in segs))
        self.assertAlmostEqual(bg.signed_area(segs), math.pi * 500 ** 2, delta=1e-3)
        self.assertEqual(bg.outline_bbox(segs), (0.0, 0.0, 1000.0, 1000.0))

    def test_rejects_bad_sizes(self):
        with self.assertRaises(bg.GeometryError):
            bg.rectangle_outline(0, 100)
        with self.assertRaises(bg.GeometryError):
            bg.rectangle_outline(100, 100, corner_radius=-1)
        with self.assertRaises(bg.GeometryError):
            bg.rectangle_outline(100, 100, corner_radius=51)

    def test_exact_metric_board(self):
        w = bg.to_mils(100, "mm")
        h = bg.to_mils(80, "mm")
        segs = bg.rectangle_outline(w, h, 0, 0, bg.to_mils(3, "mm"))
        summary = bg.describe_outline(segs, "mm")
        self.assertAlmostEqual(summary["width"], 100.0, delta=1e-9)
        self.assertAlmostEqual(summary["height"], 80.0, delta=1e-9)
        self.assertEqual(summary["orientation"], "counterclockwise")


class VerticesTest(unittest.TestCase):
    def test_clockwise_straight_outline_is_reversed(self):
        cw = [{"x": 0, "y": 0}, {"x": 0, "y": 10}, {"x": 10, "y": 10}, {"x": 10, "y": 0}]
        segs = bg.outline_from_vertices(cw, "mm")
        self.assertGreater(bg.signed_area(segs), 0)
        self.assertAlmostEqual(bg.signed_area(segs), bg.to_mils(10, "mm") ** 2, delta=1e-3)

    def test_l_shape(self):
        verts = [{"x": 0, "y": 0}, {"x": 60, "y": 0}, {"x": 60, "y": 20},
                 {"x": 20, "y": 20}, {"x": 20, "y": 50}, {"x": 0, "y": 50}]
        segs = bg.outline_from_vertices(verts, "mm")
        self.assertAlmostEqual(bg.signed_area(segs), bg.to_mils(1, "mm") ** 2 * (60 * 20 + 20 * 30),
                               delta=1e-2)

    def test_arc_edge(self):
        # Square with its top edge replaced by a half circle bulging upward.
        verts = [{"x": 0, "y": 0}, {"x": 20, "y": 0}, {"x": 20, "y": 10, "arc": {"cx": 10, "cy": 10}},
                 {"x": 0, "y": 10}]
        segs = bg.outline_from_vertices(verts, "mil")
        arc = segs[2]
        self.assertEqual(arc["kind"], "arc")
        self.assertAlmostEqual(arc["radius"], 10.0, delta=1e-9)
        self.assertAlmostEqual(arc["start_angle"], 0.0, delta=1e-9)
        self.assertAlmostEqual(arc["end_angle"], 180.0, delta=1e-9)
        self.assertAlmostEqual(bg.signed_area(segs), 200 + math.pi * 50, delta=1e-6)
        self.assertAlmostEqual(bg.outline_bbox(segs)[3], 20.0, delta=1e-9)

    def test_rejects_off_circle_arc_centre(self):
        verts = [{"x": 0, "y": 0}, {"x": 20, "y": 0}, {"x": 20, "y": 10, "arc": {"cx": 9, "cy": 10}},
                 {"x": 0, "y": 10}]
        with self.assertRaises(bg.GeometryError):
            bg.outline_from_vertices(verts, "mil")

    def test_rejects_clockwise_with_arcs(self):
        verts = [{"x": 0, "y": 10}, {"x": 20, "y": 10}, {"x": 20, "y": 0},
                 {"x": 0, "y": 0, "arc": {"cx": 0, "cy": 5}}]
        with self.assertRaises(bg.GeometryError):
            bg.outline_from_vertices(verts, "mil")

    def test_rejects_bow_tie(self):
        # Its signed area is exactly zero; the message must still name the crossing.
        verts = [{"x": 0, "y": 0}, {"x": 10, "y": 10}, {"x": 10, "y": 0}, {"x": 0, "y": 10}]
        with self.assertRaises(bg.GeometryError) as ctx:
            bg.outline_from_vertices(verts, "mil")
        self.assertIn("crosses itself", str(ctx.exception))

    def test_rejects_degenerate(self):
        with self.assertRaises(bg.GeometryError):
            bg.outline_from_vertices([{"x": 0, "y": 0}, {"x": 1, "y": 1}], "mil")
        with self.assertRaises(bg.GeometryError):
            bg.outline_from_vertices([{"x": 0, "y": 0}, {"x": 1, "y": 1}, {"x": 2, "y": 2}], "mil")
        with self.assertRaises(bg.GeometryError):
            bg.outline_from_vertices([{"x": 0, "y": 0}, {"x": 0, "y": 0}, {"x": 2, "y": 2}], "mil")
        with self.assertRaises(bg.GeometryError):
            bg.outline_from_vertices("not a list", "mil")


class ReadBackTest(unittest.TestCase):
    """Outlines as Altium might report them."""

    def clockwise_rounded(self, w, h, r):
        # The CCW rounded rectangle walked backwards. Arcs keep Altium's
        # counterclockwise angles, so each one now starts at its end_angle.
        ccw = bg.rectangle_outline(w, h, 0, 0, r)
        n = len(ccw)
        cw = []
        for k in range(n):
            vertex = ccw[(n - k) % n]
            edge = ccw[(n - k - 1) % n]
            if edge["kind"] == "arc":
                cw.append(bg.arc_seg(vertex["x"], vertex["y"], edge["cx"], edge["cy"],
                                     edge["radius"], edge["start_angle"], edge["end_angle"]))
            else:
                cw.append(bg.line_seg(vertex["x"], vertex["y"]))
        return ccw, cw

    def test_clockwise_arcs_measure_correctly(self):
        ccw, cw = self.clockwise_rounded(4000, 3000, 250)
        self.assertTrue(all(not bg.arc_runs_forward(s) for s in cw if s["kind"] == "arc"))
        self.assertAlmostEqual(bg.signed_area(cw), -bg.signed_area(ccw), delta=1e-3)
        for a, b in zip(bg.outline_bbox(cw), bg.outline_bbox(ccw)):
            self.assertAlmostEqual(a, b, delta=1e-9)
        self.assertEqual(bg.describe_outline(cw, "mil")["orientation"], "clockwise")

    def test_restore_round_trip(self):
        ccw, cw = self.clockwise_rounded(4000, 3000, 250)
        for original in (ccw, cw):
            verts = bg.restorable_vertices(original, "mm")
            rebuilt = bg.outline_from_vertices(verts, "mm")
            report = bg.compare_outlines(ccw, rebuilt)
            self.assertTrue(report["match"], report)

    def test_segments_from_altium(self):
        raw = [{"kind": 0, "x": 0, "y": 0},
               {"kind": 1, "x": 10, "y": 0, "cx": 10, "cy": 5, "radius": 5,
                "start_angle": 270, "end_angle": 90},
               {"kind": 0, "x": 10, "y": 10}]
        segs = bg.segments_from_altium(raw)
        self.assertEqual([s["kind"] for s in segs], ["line", "arc", "line"])

    def test_closing_vertex_repeat_is_dropped(self):
        raw = [{"kind": 0, "x": 0, "y": 0}, {"kind": 0, "x": 10, "y": 0},
               {"kind": 0, "x": 10, "y": 10}, {"kind": 0, "x": 0, "y": 0}]
        segs = bg.segments_from_altium(raw)
        self.assertEqual(len(segs), 3)
        self.assertTrue(bg.compare_outlines(bg.outline_from_vertices(
            [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "mil"), segs)["match"])


class CompareTest(unittest.TestCase):
    def test_identical(self):
        segs = bg.rectangle_outline(4000, 3000, 0, 0, 250)
        self.assertTrue(bg.compare_outlines(segs, segs)["match"])

    def test_rotated_start_matches(self):
        segs = bg.rectangle_outline(4000, 3000, 0, 0, 250)
        rotated = segs[3:] + segs[:3]
        self.assertTrue(bg.compare_outlines(segs, rotated)["match"])

    def test_small_errors_within_tolerance(self):
        segs = bg.rectangle_outline(4000, 3000)
        nudged = [dict(s, x=s["x"] + 0.0001) for s in segs]
        self.assertTrue(bg.compare_outlines(segs, nudged)["match"])

    def test_detects_wrong_size(self):
        segs = bg.rectangle_outline(4000, 3000)
        other = bg.rectangle_outline(4000, 3001)
        report = bg.compare_outlines(segs, other)
        self.assertFalse(report["match"])
        self.assertAlmostEqual(report["bbox_deviation_mils"], 1.0, delta=1e-9)

    def test_detects_missing_arcs(self):
        rounded = bg.rectangle_outline(4000, 3000, 0, 0, 250)
        square = bg.rectangle_outline(4000, 3000)
        self.assertFalse(bg.compare_outlines(rounded, square)["match"])

    def test_empty_actual(self):
        self.assertFalse(bg.compare_outlines(bg.rectangle_outline(10, 10), [])["match"])


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.poly = bg.outline_polyline(bg.rectangle_outline(1000, 1000, 0, 0, 100))

    def test_inside(self):
        self.assertEqual(bg.classify_box((200, 200, 300, 300), self.poly), "inside")

    def test_outside(self):
        self.assertEqual(bg.classify_box((1200, 200, 1300, 300), self.poly), "outside")

    def test_crossing(self):
        self.assertEqual(bg.classify_box((950, 400, 1050, 500), self.poly), "crossing")

    def test_rounded_corner_is_outside_the_board(self):
        # The square corner region cut away by the 100 mil radius.
        self.assertEqual(bg.classify_box((1, 1, 20, 20), self.poly), "outside")

    def test_box_larger_than_board(self):
        self.assertEqual(bg.classify_box((-100, -100, 1100, 1100), self.poly), "crossing")


class OutlineSpecTest(unittest.TestCase):
    def test_lines(self):
        lines = bg.outline_spec_lines(bg.rectangle_outline(1000, 500, 0, 0, 100))
        self.assertEqual(len(lines), 8)
        self.assertEqual(fields(lines[0]), ["SEG", "LINE", "100.000000", "0.000000"])
        arc = fields(lines[1])
        self.assertEqual(arc[:2], ["SEG", "ARC"])
        self.assertEqual(len(arc), 9)
        # Ends at 0 degrees, sent as 360 so the sweep cannot read as negative
        self.assertEqual(arc[6:], ["100.000000", "270.000000", "360.000000"])
        self.assertEqual(fields(lines[3])[7:], ["0.000000", "90.000000"])

    def test_arc_crossing_zero_keeps_its_angles(self):
        # A D-shaped board: the right side is a half circle from 270 through 0 to 90.
        verts = [{"x": 0, "y": 0}, {"x": 40, "y": 0, "arc": {"cx": 40, "cy": 20}},
                 {"x": 40, "y": 40}, {"x": 0, "y": 40}]
        segs = bg.outline_from_vertices(verts, "mm")
        arc_line = fields(bg.outline_spec_lines(segs)[1])
        self.assertEqual(arc_line[7:], ["270.000000", "90.000000"])
        self.assertAlmostEqual(bg.describe_outline(segs, "mm")["width"], 60.0, delta=1e-9)


class PrimitiveSpecTest(unittest.TestCase):
    def one(self, prim, units="mil"):
        lines, counts = bg.primitive_spec_lines([prim], units)
        self.assertEqual(len(lines), 1)
        return fields(lines[0])

    def test_track(self):
        f = self.one({"type": "track", "layer": "top", "x1": 0, "y1": 0, "x2": 100, "y2": 0,
                      "width": 10, "net": "GND"})
        self.assertEqual(f, ["TRACK", "Top Layer", "GND", "0.000000", "0.000000", "100.000000",
                             "0.000000", "10.000000"])

    def test_track_in_mm(self):
        f = self.one({"type": "track", "layer": "Mechanical 1", "x1": 0, "y1": 0, "x2": 25.4,
                      "y2": 0, "width": 0.254}, units="mm")
        self.assertEqual(f[5], "1000.000000")
        self.assertEqual(f[7], "10.000000")
        self.assertEqual(f[2], "")

    def test_arc_full_circle(self):
        f = self.one({"type": "arc", "layer": "top overlay", "cx": 0, "cy": 0, "radius": 50,
                      "start_angle": 0, "end_angle": 360, "width": 5})
        self.assertEqual(f[6:8], ["0.000000", "360.000000"])
        with self.assertRaises(bg.GeometryError):
            self.one({"type": "arc", "layer": "top", "cx": 0, "cy": 0, "radius": 5,
                      "start_angle": 10, "end_angle": 10, "width": 1})

    def test_via_defaults(self):
        f = self.one({"type": "via", "x": 10, "y": 20, "diameter": 20, "hole": 10, "net": "GND"})
        self.assertEqual(f, ["VIA", "GND", "10.000000", "20.000000", "20.000000", "10.000000",
                             "Top Layer", "Bottom Layer"])

    def test_via_hole_must_fit(self):
        with self.assertRaises(bg.GeometryError):
            self.one({"type": "via", "x": 0, "y": 0, "diameter": 10, "hole": 10})

    def test_fill_normalises_corners(self):
        f = self.one({"type": "fill", "layer": "top", "x1": 100, "y1": 50, "x2": 0, "y2": 0})
        self.assertEqual(f[3:7], ["0.000000", "0.000000", "100.000000", "50.000000"])

    def test_board_cutout_defaults_to_multi_layer(self):
        f = self.one({"type": "region", "kind": "board_cutout",
                      "points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}]})
        self.assertEqual(f[:5], ["REGION", "Multi Layer", "", "3", "3"])
        self.assertEqual(len(f), 5 + 6)

    def test_region_kind_number_passes_through(self):
        f = self.one({"type": "region", "kind": 4, "layer": "Multi Layer",
                      "points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}]})
        self.assertEqual(f[3], "4")

    def test_region_net_only_on_copper(self):
        with self.assertRaises(bg.GeometryError):
            self.one({"type": "region", "kind": "board_cutout", "net": "GND",
                      "points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}]})

    def test_mounting_hole(self):
        f = self.one({"type": "pad", "x": 125, "y": 125, "size": 3.2, "hole": 3.2, "plated": False},
                     units="mm")
        self.assertEqual(f[0:3], ["PAD", "Multi Layer", ""])
        self.assertEqual(f[8:10], ["round", "0"])

    def test_plated_by_default_when_drilled(self):
        f = self.one({"type": "pad", "x": 0, "y": 0, "size": 100, "hole": 50})
        self.assertEqual(f[9], "1")

    def test_smd_pad_layer_rules(self):
        f = self.one({"type": "pad", "x": 0, "y": 0, "size": 40, "shape": "round"})
        self.assertEqual(f[1], "Top Layer")
        with self.assertRaises(bg.GeometryError):
            self.one({"type": "pad", "x": 0, "y": 0, "size": 40, "layer": "Mechanical 1"})
        with self.assertRaises(bg.GeometryError):
            self.one({"type": "pad", "x": 0, "y": 0, "size": 40, "hole": 20, "layer": "top"})
        with self.assertRaises(bg.GeometryError):
            self.one({"type": "pad", "x": 0, "y": 0, "size": 40, "hole": 60})

    def test_text_escaping_and_mirror(self):
        f = self.one({"type": "text", "layer": "bottom silk", "x": 0, "y": 0, "height": 60,
                      "text": "REV A|B\nline 2"})
        self.assertEqual(f[1], "Bottom Overlay")
        self.assertEqual(f[7], "1")
        self.assertEqual(f[8], "REV A<PIPE>B<NL>line 2")
        self.assertEqual(f[5], "10.000000")

    def test_polygon(self):
        f = self.one({"type": "polygon", "layer": "top", "net": "GND", "pour": True,
                      "points": [{"x": 0, "y": 0}, {"x": 0, "y": 10}, {"x": 10, "y": 10},
                                 {"x": 10, "y": 0}]})
        self.assertEqual(f[:5], ["POLYGON", "Top Layer", "GND", "1", "4"])
        coords = [float(v) for v in f[5:]]
        pts = list(zip(coords[0::2], coords[1::2]))
        self.assertGreater(bg.signed_area([bg.line_seg(x, y) for x, y in pts]), 0)

    def test_errors_name_the_primitive(self):
        with self.assertRaises(bg.GeometryError) as ctx:
            bg.primitive_spec_lines([{"type": "track", "layer": "top", "x1": 0, "y1": 0,
                                      "x2": 1, "y2": 1, "width": 1},
                                     {"type": "track", "layer": "top", "x1": 0, "y1": 0,
                                      "x2": 1, "y2": 1, "width": -1}], "mil")
        self.assertIn("primitives[1].width", str(ctx.exception))

    def test_rejects_unknown_type_and_bad_net(self):
        with self.assertRaises(bg.GeometryError):
            bg.primitive_spec_lines([{"type": "banana"}], "mil")
        with self.assertRaises(bg.GeometryError):
            bg.primitive_spec_lines([{"type": "via", "x": 0, "y": 0, "diameter": 20, "hole": 10,
                                      "net": "A|B"}], "mil")
        with self.assertRaises(bg.GeometryError):
            bg.primitive_spec_lines([], "mil")

    def test_counts(self):
        lines, counts = bg.primitive_spec_lines(
            [{"type": "via", "x": 0, "y": 0, "diameter": 20, "hole": 10},
             {"type": "via", "x": 50, "y": 0, "diameter": 20, "hole": 10},
             {"type": "text", "x": 0, "y": 0, "height": 50, "text": "X"}], "mil")
        self.assertEqual(counts, {"via": 2, "text": 1})
        self.assertEqual(len(lines), 3)


if __name__ == "__main__":
    unittest.main()
