"""Synthetic activity-list/parallel-array fixtures; no athlete data or network."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _harness import (SYNC_PATH, install_verb_guard, load_module_by_path,
                      restore_verb_guard)

mod = load_module_by_path("s11_post_workout_fields", SYNC_PATH)
Sync = mod.IntervalsSync
originals = None


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


def activity(**fields):
    row = {"id": "synthetic-a", "type": "VirtualRide", "name": "Synthetic ride",
           "start_date_local": "2026-01-01T10:00:00", "moving_time": 3630,
           "elapsed_time": 3729, "icu_average_watts": 180, "icu_weight": 75,
           "average_speed": 8, "icu_joules": 650000}
    row.update(fields)
    return row


def curve(secs=(1, 5, 60, 300, 1200, 3600), watts=(500, 450, 300, 250, 210, 190)):
    return {"secs": list(secs), "watts": list(watts),
            "values": [999] * len(secs), "watts_per_kg": [99] * len(secs), "weight": 99}


def response(status=200, body=None):
    out = mock.Mock(status_code=status, headers={})
    out.json.return_value = curve() if body is None else body
    return out


class Fields(unittest.TestCase):
    def setUp(self):
        self.sync = Sync("synthetic", "synthetic")
        self.sync._intervals_data = {}
        for method, result in (("_build_terrain_for_activity", {}),
                               ("_build_weather_for_activity", {}),
                               ("_get_activity_messages", ("ok", []))):
            patch = mock.patch.object(self.sync, method, return_value=result)
            patch.start()
            self.addCleanup(patch.stop)

    def output(self, raw=None, **kwargs):
        with mock.patch.object(self.sync, "_fetch_activity_power_curve",
                               return_value=("ok", {})):
            return self.sync._format_activities([raw or activity()], **kwargs)[0]

    def test_present_and_legacy_duration_work_unchanged(self):
        row = self.output()
        self.assertEqual(row["duration_formatted"], "1h30s")
        self.assertEqual(row["duration_hours"], 1.01)
        self.assertEqual(row["display"]["moving_time"], "1h1m")
        self.assertEqual(row["elapsed_formatted"], "1h2m")
        self.assertEqual(row["elapsed_hours"], 1.04)
        self.assertEqual(row["display"]["elapsed_time"], row["elapsed_formatted"])
        self.assertEqual(row["work_kj"], 650)
        self.assertEqual(row["avg_power_wkg"], 2.4)
        self.assertEqual(row["power_weight_kg"], 75)
        self.assertEqual(row["display"]["power_weight"], {"value": 75, "unit": "kg"})

    def test_half_minute_rounding_equal_after_rounding_and_zero(self):
        for seconds, expected in ((0, "0m"), (29, "0m"), (30, "1m"),
                                  (59, "1m"), (3570, "1h"), (5390, "1h30m")):
            with self.subTest(seconds=seconds):
                row = self.output(activity(moving_time=seconds, elapsed_time=seconds))
                self.assertEqual(row["display"]["moving_time"], expected)
                self.assertEqual(row["display"]["elapsed_time"], expected)
        row = self.output(activity(moving_time=3631, elapsed_time=3659))
        self.assertEqual(row["display"]["moving_time"], row["display"]["elapsed_time"])
        # 89m50s rounds to 90m for display but remains ineligible for decoupling.
        self.assertEqual(self.output(activity(moving_time=5390))["duration_formatted"], "1h29m50s")

    def test_missing_and_invalid_new_fields(self):
        for value in (None, -1, "3600", True, float("nan")):
            row = self.output(activity(elapsed_time=value, icu_weight=None))
            self.assertIsNone(row["elapsed_hours"])
            self.assertIsNone(row["elapsed_formatted"])
            self.assertIsNone(row["display"]["elapsed_time"])
        row = self.output(activity(moving_time=None, average_speed=None, icu_average_watts=None))
        for key in ("min_temp", "max_temp", "temp_source", "power_weight_kg", "avg_power_wkg"):
            self.assertIsNone(row[key])
        for key in ("moving_time", "min_temp", "avg_temp", "max_temp", "power_weight", "pace"):
            self.assertIsNone(row["display"][key])
        self.assertNotIn("power_curve", row)

    def test_weather_source_wins_including_zero_no_mixed_endpoints(self):
        row = self.output(activity(average_weather_temp=0, min_weather_temp=-2,
                                   max_weather_temp=None, average_temp=20,
                                   min_temp=18, max_temp=23))
        self.assertEqual((row["avg_temp"], row["min_temp"], row["max_temp"], row["temp_source"]),
                         (0, -2, None, "weather"))
        self.assertEqual(row["display"]["avg_temp"], {"value": 0, "unit": "°C"})
        self.assertIsNone(row["display"]["max_temp"])

    def test_device_source_ignores_orphan_weather_endpoints(self):
        row = self.output(activity(average_weather_temp=None, min_weather_temp=-5,
                                   max_weather_temp=6, average_temp=21,
                                   min_temp=20, max_temp=23))
        self.assertEqual((row["min_temp"], row["avg_temp"], row["max_temp"], row["temp_source"]),
                         (20, 21, 23, "device"))
        self.assertEqual(row["display"]["max_temp"], {"value": 23, "unit": "°C"})

    def test_legacy_average_preserved_without_invented_source(self):
        row = self.output(activity(avg_temp=7, min_temp=4, max_temp=9))
        self.assertEqual(row["avg_temp"], 7)
        for field in ("temp_source", "min_temp", "max_temp"):
            self.assertIsNone(row[field])
        self.assertIsNone(row["display"]["avg_temp"])

    def test_imperial_temperature_is_not_converted_twice(self):
        row = self.output(activity(average_weather_temp=68, min_weather_temp=65,
                                   max_weather_temp=72), temp_unit="F",
                          athlete_units={"distance": "imperial", "weight": "lb", "temp": "f"})
        self.assertEqual(row["display"]["avg_temp"], {"value": 68, "unit": "°F"})
        self.assertEqual(row["display"]["min_temp"], {"value": 65, "unit": "°F"})
        self.assertEqual(row["display"]["power_weight"], {"value": 165.3, "unit": "lb"})
        self.assertEqual(row["avg_power_wkg"], 2.4)
        self.assertEqual(row["display"]["avg_speed"], {"value": 17.9, "unit": "mph"})

    def test_temperature_display_whole_degrees_preserves_canonical_values(self):
        for source, unit, values, expected in (
                ("weather", "C", (8.6, 9.047999, 9.6), (9, 9, 10)),
                ("device", "C", (-2.6, -0.4, 0), (-3, 0, 0)),
                ("weather", "F", (64.6, 68.047999, 72.4), (65, 68, 72)),
                ("device", "F", (None, 68.6, None), (None, 69, None))):
            with self.subTest(source=source, unit=unit):
                keys = (("min_weather_temp", "average_weather_temp", "max_weather_temp")
                        if source == "weather" else ("min_temp", "average_temp", "max_temp"))
                raw = activity(**dict(zip(keys, values)))
                original = raw.copy()
                row = self.output(raw, temp_unit=unit,
                                  athlete_units={"temp": unit.lower()})
                self.assertEqual(raw, original)
                self.assertEqual(row["temp_source"], source)
                self.assertEqual(tuple(row[k] for k in ("min_temp", "avg_temp", "max_temp")),
                                 values)
                for key, value in zip(("min_temp", "avg_temp", "max_temp"), expected):
                    self.assertEqual(row["display"][key],
                                     None if value is None else {"value": value, "unit": "°" + unit})

    def test_weight_guard_endpoints_and_invalid_inputs(self):
        for weight, expected in ((30, 6), (200, .9), (72, 2.5)):
            row = self.output(activity(icu_weight=weight))
            self.assertEqual(row["avg_power_wkg"], expected)
            self.assertEqual(row["power_weight_kg"], weight)
        for weight in (29.99, 200.01, 0, None, "75", True, float("inf"), float("nan")):
            with self.subTest(weight=weight):
                row = self.output(activity(icu_weight=weight))
                self.assertIsNone(row["avg_power_wkg"])
                self.assertIsNone(row["power_weight_kg"])
        row = self.output(activity(icu_average_watts=None))
        self.assertIsNone(row["avg_power_wkg"])
        self.assertIsNone(row["power_weight_kg"])

    def test_wkg_two_decimals_and_no_curve_weight_substitution(self):
        row = self.output(activity(icu_average_watts=187, icu_weight=73))
        self.assertEqual(row["avg_power_wkg"], 2.56)
        self.assertEqual(row["avg_power"], 187)

    def test_pace_by_sport_and_units(self):
        for sport in ("Run", "TrailRun", "VirtualRun", "Walk", "Hike"):
            for pref, pace in (("metric", {"value": "6:40", "unit": "/km"}),
                               ("imperial", {"value": "10:44", "unit": "/mi"})):
                row = self.output(activity(type=sport, average_speed=2.5),
                                  athlete_units={"distance": pref})
                self.assertEqual(row["display"]["pace"], pace)
                self.assertEqual(row["avg_speed"], 9)
                self.assertIsNotNone(row["display"]["avg_speed"])

    def test_pace_omitted_for_erg_ride_nordic_strength_and_zero_speed(self):
        for sport in ("Ride", "VirtualRide", "NordicSki", "VirtualSki",
                      "Rowing", "VirtualRow", "VirtualRowing", "WeightTraining"):
            self.assertIsNone(self.output(activity(type=sport))["display"]["pace"])
        for speed in (None, 0, -1):
            self.assertIsNone(self.output(activity(type="Run", average_speed=speed))["display"]["pace"])

    def test_pace_rounds_seconds_with_carry(self):
        self.assertEqual(self.output(activity(type="Run", average_speed=10/3))["display"]["pace"],
                         {"value": "5:00", "unit": "/km"})

    def test_format_integration_max_is_one_second_not_power_model(self):
        with mock.patch.object(mod.requests, "get", return_value=response()) as get:
            row = self.sync._format_activities([activity(icu_pm_p_max=9999)])[0]
        self.assertEqual(row["power_curve"]["1"], 500)
        self.assertEqual(row["power_curve"]["3600"], 190)
        self.assertEqual(len(row["power_curve"]), 6)
        self.assertEqual(get.call_count, 1)

    def test_no_power_no_request_and_watts_stream_is_power(self):
        with mock.patch.object(mod.requests, "get", return_value=response()) as get:
            raw = activity(icu_average_watts=None)
            self.assertNotIn("power_curve", self.sync._format_activities([raw])[0])
            get.assert_not_called()
            raw["stream_types"] = ["time", "watts"]
            self.assertIn("power_curve", self.sync._format_activities([raw])[0])
            self.assertEqual(get.call_count, 1)

    def test_success_copy_forward_refreshes_display_preferences(self):
        with mock.patch.object(mod.requests, "get", return_value=response()) as get:
            first = self.sync._format_activities([activity(type="Run", average_speed=2.5)])[0]
            second = self.sync._format_activities(
                [activity(type="Run", average_speed=2.5)],
                previous_latest_lookup={"synthetic-a": first},
                athlete_units={"distance": "imperial", "weight": "lb"})[0]
        self.assertEqual(get.call_count, 1)
        self.assertEqual(second["power_curve"], first["power_curve"])
        self.assertEqual(second["display"]["pace"], {"value": "10:44", "unit": "/mi"})
        self.assertEqual(second["display"]["power_weight"]["unit"], "lb")
        self.assertEqual(second["avg_power_wkg"], first["avg_power_wkg"])


class Curves(unittest.TestCase):
    def setUp(self):
        self.sync = Sync("synthetic", "synthetic")
        self.sleep = mock.patch.object(mod.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_endpoint_auth_timeout_exact_points_and_short_activities(self):
        with mock.patch.object(mod.requests, "get", return_value=response()) as get:
            out = self.sync._build_power_curve_for_activity(activity(moving_time=75), {}, True)
        self.assertEqual(out["power_curve"], {"1": 500, "5": 450, "60": 300})
        args, kwargs = get.call_args
        self.assertEqual(args[0], self.sync.INTERVALS_BASE_URL + "/activity/synthetic-a/power-curve.json")
        self.assertEqual(kwargs["timeout"], (5, 30))
        self.assertEqual(kwargs["headers"]["Authorization"], "Basic " + self.sync.intervals_auth)

    def test_missing_exact_points_never_interpolated(self):
        with mock.patch.object(mod.requests, "get",
                               return_value=response(body=curve((1, 4, 6, 59, 61), (400, 390, 380, 220, 210)))):
            status, out = self.sync._fetch_activity_power_curve("synthetic")
        self.assertEqual(status, "ok")
        self.assertEqual(out, {"1": 400})

    def test_null_points_absent_real_zero_preserved(self):
        with mock.patch.object(mod.requests, "get",
                               return_value=response(body=curve((1, 5, 60), (None, 0, 200)))):
            self.assertEqual(self.sync._fetch_activity_power_curve("synthetic"),
                             ("ok", {"5": 0, "60": 200}))

    def test_transient_then_next_sync_success(self):
        with mock.patch.object(mod.requests, "get", return_value=response(503)) as get:
            self.assertEqual(self.sync._build_power_curve_for_activity(activity(), {}, True), {})
            self.assertEqual(get.call_count, 3)
        second = Sync("synthetic", "synthetic")
        with mock.patch.object(mod.requests, "get", return_value=response()):
            self.assertIn("power_curve", second._build_power_curve_for_activity(activity(), {}, True))

    def test_transient_http_and_transport_not_cached_as_terminal(self):
        for failure in (response(401), response(403), response(429), response(500),
                        mod.requests.exceptions.Timeout("synthetic"),
                        mod.requests.exceptions.ConnectionError("synthetic")):
            sync = Sync("synthetic", "synthetic")
            with mock.patch.object(mod.requests, "get", side_effect=(
                    failure if isinstance(failure, Exception) else lambda *a, **k: failure)):
                self.assertEqual(sync._build_power_curve_for_activity(activity(), {}, True), {})

    def test_terminal_and_copy_forward(self):
        for status in (404, 410):
            sync = Sync("synthetic", "synthetic")
            with mock.patch.object(mod.requests, "get", return_value=response(status)) as get:
                out = sync._build_power_curve_for_activity(activity(), {}, True)
                self.assertEqual(out, {"power_curve_status": "failed", "power_curve_error": f"http_{status}"})
                self.assertEqual(get.call_count, 1)
            with mock.patch.object(mod.requests, "get") as get:
                self.assertEqual(sync._build_power_curve_for_activity(
                    activity(), {"synthetic-a": out}, True), out)
                get.assert_not_called()

    def test_success_empty_and_normal_copy_forward(self):
        for stored in ({}, {"1": 400, "5": 350}):
            with mock.patch.object(mod.requests, "get") as get:
                self.assertEqual(self.sync._build_power_curve_for_activity(
                    activity(), {"synthetic-a": {"power_curve": stored}}, True),
                    {"power_curve": stored})
                get.assert_not_called()
        with mock.patch.object(mod.requests, "get", return_value=response(body=curve((), ()))):
            self.assertEqual(self.sync._build_power_curve_for_activity(activity(), {}, True),
                             {"power_curve": {}})

    def test_previous_latest_loads_curves_and_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            self.sync.data_dir = Path(directory)
            rows = [{"id": "a", "power_curve": {"1": 500}},
                    {"id": 2, "power_curve_status": "failed", "power_curve_error": "http_404"}]
            (self.sync.data_dir / "latest.json").write_text(json.dumps({"recent_activities": rows}))
            previous = self.sync._load_previous_latest()
        self.assertEqual(previous["a"]["power_curve"], {"1": 500})
        self.assertEqual(previous["2"]["power_curve_error"], "http_404")

    def test_copy_forward_does_not_mutate_input(self):
        previous = {"synthetic-a": {"power_curve": {"1": 500}}}
        out = self.sync._build_power_curve_for_activity(activity(), previous, True)
        out["power_curve"]["1"] = 1
        self.assertEqual(previous["synthetic-a"]["power_curve"]["1"], 500)

    def test_shortened_activity_cannot_display_long_cached_point(self):
        cached = {"synthetic-a": {"power_curve": {"1": 500, "3600": 190}}}
        with mock.patch.object(mod.requests, "get") as get:
            self.assertEqual(self.sync._build_power_curve_for_activity(
                activity(moving_time=300), cached, True), {"power_curve": {"1": 500}})
            get.assert_not_called()
        self.assertEqual(cached["synthetic-a"]["power_curve"]["3600"], 190)

    def test_request_bound_includes_retries_and_duplicate_failures(self):
        with mock.patch.object(mod.requests, "get", return_value=response(503)) as get:
            for index in range(20):
                raw = activity(id=f"synthetic-{index}")
                self.assertEqual(self.sync._build_power_curve_for_activity(raw, {}, True), {})
                self.assertEqual(self.sync._build_power_curve_for_activity(raw, {}, True), {})
            self.assertEqual(get.call_count, 12)
            self.assertEqual(self.sync._power_curve_fetches_used, 8)

    def test_success_request_bound_and_cache_hits_cost_nothing(self):
        with mock.patch.object(mod.requests, "get", return_value=response()) as get:
            for index in range(10):
                raw = activity(id=f"synthetic-{index}")
                out = self.sync._build_power_curve_for_activity(raw, {}, True)
                self.assertEqual("power_curve" in out, index < 8)
            self.assertEqual(get.call_count, 8)
            out = self.sync._build_power_curve_for_activity(
                activity(id="cached"), {"cached": {"power_curve": {"1": 500}}}, True)
            self.assertEqual(out, {"power_curve": {"1": 500}})
            self.assertEqual(get.call_count, 8)

    def test_success_after_safe_read_retry(self):
        with mock.patch.object(mod.requests, "get", side_effect=[response(503), response()]) as get:
            self.assertEqual(self.sync._fetch_activity_power_curve("synthetic")[0], "ok")
            self.assertEqual(get.call_count, 2)

    def test_malformed_is_transient_not_success(self):
        for body in ([], {}, {"secs": [1], "watts": []},
                     {"secs": [1], "values": [500]},
                     curve((1, 1), (500, 499)), curve((True,), (500,)),
                     curve((1,), (-1,)), curve((1,), (float("nan"),)),
                     curve((1,), ("500",))):
            sync = Sync("synthetic", "synthetic")
            with mock.patch.object(mod.requests, "get", return_value=response(body=body)):
                self.assertEqual(sync._fetch_activity_power_curve("synthetic")[0], "transient")
        bad = response()
        bad.json.side_effect = ValueError("synthetic parse failure")
        with mock.patch.object(mod.requests, "get", return_value=bad):
            self.assertEqual(self.sync._fetch_activity_power_curve("synthetic"), ("transient", "parse_error"))


if __name__ == "__main__":
    unittest.main()
