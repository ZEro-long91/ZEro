"""Hand-derived terrain segmentation profiles; no athlete fixtures or network access."""
import math
import unittest
from unittest.mock import patch

from _harness import (SYNC_PATH, load_module_by_path, install_verb_guard,
                      restore_verb_guard)

mod = load_module_by_path("terrain_segmentation_sync", SYNC_PATH)
C = mod.IntervalsSync


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


class NativeBoundaries(unittest.TestCase):
    def test_absolute_drop_boundary(self):
        d = [0, 200, 400, 600, 800, 1000]
        self.assertEqual(C._terrain_native_boundaries(d, [0, 10, 20, 17.5, 27.5, 37.5], True), [(0, 5)])
        self.assertEqual(C._terrain_native_boundaries(d, [0, 10, 20, 17.49, 27.5, 37.5], True), [(0, 2), (3, 5)])

    def test_relative_drop_boundary(self):
        d = [0, 200, 400, 600, 800, 1000]
        self.assertEqual(C._terrain_native_boundaries(d, [0, 100, 200, 190, 290, 390], True), [(0, 5)])
        self.assertEqual(C._terrain_native_boundaries(d, [0, 100, 200, 189.99, 290, 390], True), [(0, 2), (3, 5)])

    def test_reference_excess(self):
        for height, expected in ((1.49, []), (1.5, []), (1.51, [(0, 2)])):
            with self.subTest(height=height):
                self.assertEqual(C._terrain_native_boundaries([0, 100, 200], [0, height, 2*height], True), expected)

    def test_unused_prefix_and_suffix(self):
        self.assertEqual(C._terrain_native_boundaries([0, 100, 2000, 2100], [0, 5, 5, 15], True), [(0, 1), (2, 3)])
        self.assertEqual(C._terrain_native_boundaries([0, 100, 2000, 2100], [0, 15, 15, 20], True), [(0, 1), (2, 3)])

    def test_partial_final_rise(self):
        self.assertEqual(C._terrain_native_boundaries([0, 100, 150], [0, 5, 8], True), [(0, 2)])

    def test_repeated_distance_and_earliest_peak(self):
        self.assertEqual(C._terrain_native_boundaries([0, 0, 100], [0, 0, 5], True), [(1, 2)])
        self.assertEqual(C._terrain_native_boundaries([0, 100, 100, 200], [0, 5, 5, 0], True), [(0, 1)])

    def test_degenerate(self):
        for d, y in (([], []), ([0], [0]), ([0, 0], [2, 2]), ([0, 100, 200], [7, 7, 7])):
            self.assertEqual(C._terrain_boundaries(d, y, True), [])

    def test_centered_native_secant(self):
        self.assertEqual(C._terrain_rolling_gradient([0, 50, 100, 200], [0, 1, 4, 8]), [4, 4, 4, 4])
        self.assertEqual(C._terrain_rolling_gradient([0, 0], [1, 1]), [0, 0])


class ValleyAndExit(unittest.TestCase):
    def setUp(self):
        self.d = list(range(0, 1001, 50))
        self.y = [.04*min(x, 350) + .002*max(0, min(x-350, 300)) + .04*max(0, x-650) for x in self.d]
        self.g = [4.0]*21
        self.g[9:11] = [0.0, 0.0]

    def test_shared_saddle(self):
        self.assertEqual(C._terrain_native_boundaries(self.d, self.y, True), [(0, 20)])
        self.assertEqual(C._terrain_boundaries(self.d, self.y, True), [(0, 9), (9, 20)])
        self.assertEqual(self.d[9], 450)

    def test_dwell_exact_and_below(self):
        self.assertEqual(C._terrain_valley_splits([(0, 20)], self.d, self.y, self.g), [(0, 9), (9, 20)])
        self.d[9] = 451
        self.assertEqual(C._terrain_valley_splits([(0, 20)], self.d, self.y, self.g), [(0, 20)])

    def test_quarter_peak_strict(self):
        self.g[9:11] = [1.0, 1.0]
        self.assertEqual(C._terrain_valley_splits([(0, 20)], self.d, self.y, self.g), [(0, 20)])
        self.g[9:11] = [.99, .99]
        self.assertEqual(C._terrain_valley_splits([(0, 20)], self.d, self.y, self.g), [(0, 9), (9, 20)])

    def test_major_gain_discontinuity(self):
        for gain, expected in ((100, [(0, 20)]), (99.99, [(0, 9), (9, 20)])):
            y = [v/self.y[-1]*gain for v in self.y]
            self.assertEqual(C._terrain_valley_splits([(0, 20)], self.d, y, self.g), expected)

    def test_narrow_deepest_valley_does_not_fall_back(self):
        self.g[5] = -1
        self.assertEqual(C._terrain_valley_splits([(0, 20)], self.d, self.y, self.g), [(0, 20)])

    def test_extension_cap_and_strict_support(self):
        d = list(range(0, 1001, 100)) + [1100, 1101]
        for support, end in ((.6, 11), (.5, 10), (.50001, 11)):
            self.assertEqual(C._terrain_bounded_ends([(0, 10)], d, [4.0]*11+[support, .6]), [(0, end)])

    def test_midpoint_and_no_start_movement(self):
        self.assertEqual(C._terrain_bounded_ends([(0, 8), (12, 20)], self.d, [4.0]*21), [(0, 10), (12, 20)])

    def test_no_minimum_length_padding(self):
        self.assertEqual(C._terrain_boundaries([0, 100, 200, 300, 400], [0, 5, 10, 15, 20], True), [(0, 4)])

    def test_direction_symmetry(self):
        self.assertEqual(C._terrain_boundaries(self.d, self.y, True), C._terrain_boundaries(self.d, [-v for v in self.y], False))


class AdmissionAndCallers(unittest.TestCase):
    def setUp(self):
        self.sync = object.__new__(C)
        self.sync.debug = False

    def test_24_formatter_controls(self):
        cases = [(1000, 40, True), (1000, 49.999, True), (1000, 50, True),
                 (499, 20, False), (500, 20, True), (1000, 20, False),
                 (1000, 29.6, True), (1000, 29.4, False), (11000, 110, False),
                 (8000, 120, True), (6000, 99.5, True), (6000, 99.4, False)]
        for length, gain, expected in cases:
            for asc in (True, False):
                with self.subTest(length=length, gain=gain, ascending=asc):
                    d = [length*k/100 for k in range(101)]
                    y = [(gain if asc else -gain)*k/100 for k in range(101)]
                    p = [dict(lat=0.0, lon=math.degrees(x/6371000), ele=h) for x, h in zip(d, y)]
                    # Isolate unchanged gates from the independently tested boundary stage.
                    with patch.object(self.sync, "_terrain_boundaries", return_value=[(0, 100)]):
                        emitted = self.sync._detect_segments(p, d, y, 1.5, 500, asc)
                    admitted = [c for c in emitted if abs(c["elevation_m"]) >= 100 or abs(c["avg_gradient_pct"]) >= 3]
                    self.assertEqual(bool(admitted), expected)
                    if emitted:
                        c = emitted[0]
                        self.assertEqual(c["elevation_m"], round(gain)*(1 if asc else -1))
                        self.assertEqual(c["avg_gradient_pct"], round(gain/length*100, 1)*(1 if asc else -1))
                        self.assertEqual(c["distance_km"], round(length/1000, 1))
                        self.assertEqual(c["start_coords"], [0.0, 0.0])
                        self.assertEqual(c["end_coords"], [0.0, round(p[-1]["lon"], 5)])
                        self.assertEqual("category" in c, asc)

    def test_raw_gradient_boundary(self):
        for gain, expected in ((14.99, False), (15, True), (15.01, True)):
            for asc in (True, False):
                with patch.object(self.sync, "_terrain_boundaries", return_value=[(0, 1)]):
                    rows = self.sync._detect_segments([dict(lat=0, lon=0), dict(lat=0, lon=.01)], [0, 1000], [0, gain if asc else -gain], 1.5, 500, asc)
                self.assertEqual(bool(rows), expected)

    def test_outdoor_and_missing_gps(self):
        with patch.object(self.sync, "_fetch_terrain_streams", side_effect=AssertionError("must not fetch")):
            self.assertEqual(self.sync._build_terrain_for_activity(dict(id="x", type="VirtualRide"), {}), {})
        with patch.object(self.sync, "_fetch_terrain_streams", return_value=("ok", [dict(type="altitude", data=[0, 2])])):
            self.assertEqual(self.sync._build_terrain_for_activity(dict(id="x", type="Ride"), {}), {"terrain_status": "no_gps"})

    def test_no_elevation(self):
        streams = [dict(type="latlng", data=[0, 0], data2=[0, .01])]
        with patch.object(self.sync, "_fetch_terrain_streams", return_value=("ok", streams)):
            self.assertEqual(self.sync._build_terrain_for_activity(dict(id="x", type="Ride"), {}), {"terrain_status": "no_elevation"})

    def test_completed_terrain_copy_forward(self):
        previous = {"x": {"terrain_summary": {"climbs": [], "descents": [], "total_distance_km": 1}}}
        with patch.object(self.sync, "_fetch_terrain_streams", side_effect=AssertionError("no backfill")):
            result = self.sync._build_terrain_for_activity(dict(id="x", type="Ride"), previous)
        self.assertEqual(result["terrain_summary"]["total_distance_km"], 1)


if __name__ == "__main__":
    unittest.main()
