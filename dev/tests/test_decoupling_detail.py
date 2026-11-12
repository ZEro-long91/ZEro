"""Offline synthetic power-vs-hr response shapes; no athlete data or network."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _harness import (SYNC_PATH, install_verb_guard, load_module_by_path,
                      restore_verb_guard)

mod = load_module_by_path("s11_decoupling_detail", SYNC_PATH)
Sync = mod.IntervalsSync
originals = None


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


def body(watts=(180, 180, 180, 180), hrs=(100, 100, 110, 110)):
    # Same keys as the supplied real response; invented values only.
    mid = len(watts) // 2
    ratios = [sum(watts[a:b]) / sum(hrs[a:b]) for a, b in ((0, mid), (mid, len(watts)))]
    return {"bucketSize": 60, "warmup": 0, "cooldown": 0, "elapsedTime": len(watts) * 60 + 15,
            "hrLag": 15, "start": 0, "mid": mid, "end": len(watts),
            "powerHrFirst": ratios[0], "powerHrSecond": ratios[1],
            "decoupling": (ratios[0] - ratios[1]) / ratios[0] * 100,
            "series": [{"start": i * 60, "secs": 60, "movingSecs": 60,
                        "watts": w, "hr": h, "cadence": 90}
                       for i, (w, h) in enumerate(zip(watts, hrs))],
            "curves": [{"id": "all", "coefficients": [90, .4], "r2": .7}]}


def raw(**fields):
    act = {"id": "synthetic", "type": "VirtualRide", "moving_time": 5400,
           "start_date_local": "2026-01-01T10:00:00", "elapsed_time": 5400,
           "icu_average_watts": 180, "average_heartrate": 105}
    act.update(fields)
    return act


def summary(targets=(160, 200), actuals=(160, 200), status="followed"):
    return {"method": "plan_match_v2", "status": status,
            "plan": {"event_id": "synthetic-plan"},
            "steps": [[599, target, actual, True] for target, actual in zip(targets, actuals)]}


def response(status=200, data=None):
    result = mock.Mock(status_code=status, headers={})
    result.json.return_value = body() if data is None else data
    return result


class Detail(unittest.TestCase):
    def test_same_window_halves_work_changes_and_reproduction(self):
        d = Sync._decoupling_detail(body())
        self.assertEqual(d["early"], {"duration_s": 120, "span_s": 120, "paired_s": 120,
                         "bucket_coverage_pct": 100, "avg_power_w": 180,
                         "avg_hr_bpm": 100, "work_kj": 21.6})
        self.assertEqual(d["change"]["avg_hr_bpm"], {"absolute": 10, "pct": 10})
        self.assertAlmostEqual(d["bucket_decoupling"], 100 / 11, places=5)
        self.assertAlmostEqual(d["upstream_decoupling"], 100 / 11)
        self.assertEqual(d["comparability"], "steady_power")
        self.assertNotIn("series", d)
        self.assertNotIn("curves", d)

    def test_progressive_descending_and_power_down_hr_up(self):
        for powers, trend in (((160, 160, 200, 200), "rising"),
                               ((200, 200, 160, 160), "falling"),
                               ((180, 180, 180, 180), "steady")):
            d = Sync._decoupling_detail(body(powers))
            self.assertEqual(d["power_trend"], trend)
            self.assertEqual(d["comparability"], trend + "_power")
        self.assertGreater(Sync._decoupling_detail(body((200, 200, 160, 160)))["upstream_decoupling"], 20)

    def test_threshold_boundaries_require_both_absolute_and_relative_change(self):
        for before, after, expected in ((200, 210, "steady"), (200, 210.001, "rising"),
                                        (200, 190, "steady"), (200, 189.999, "falling"),
                                        (50, 55, "steady"), (50, 55.001, "rising"),
                                        (50, 45, "steady"), (50, 44.999, "falling")):
            self.assertEqual(Sync._decoupling_power_trend(before, after), expected)

    def test_trimmed_window_uses_bucket_indices_and_hr_lag_only_as_metadata(self):
        data = body((999, 180, 180, 180, 180, 999), (99, 100, 100, 110, 110, 99))
        data.update(start=1, mid=3, end=5, warmup=60, cooldown=60, hrLag=59,
                    powerHrFirst=1.8, powerHrSecond=180/110, decoupling=100/11)
        d = Sync._decoupling_detail(data)
        self.assertEqual(d["window"], {"bucket_size_s": 60, "start_bucket": 1, "mid_bucket": 3,
                         "end_bucket": 5, "start_s": 60, "mid_s": 180, "end_s": 300,
                         "warmup_s": 60, "cooldown_s": 60, "hr_lag_s": 59})
        self.assertEqual(d["early"]["avg_power_w"], 180)
        self.assertEqual(d["late"]["avg_hr_bpm"], 110)

    def test_gaps_coasting_and_partial_bucket_do_not_fill_or_drop_zero_power(self):
        data = body((0, 180, 180, 180))
        data["series"][1]["start"] = 90
        data["series"][2]["start"] = 150
        data["series"][3].update(start=210, secs=30, movingSecs=15)
        d = Sync._decoupling_detail(data)
        self.assertEqual(d["early"]["avg_power_w"], 90)
        self.assertEqual(d["early"]["work_kj"], 10.8)
        self.assertEqual(d["early"]["bucket_coverage_pct"], 80)
        self.assertEqual(d["late"]["duration_s"], 90)
        self.assertEqual(d["late"]["work_kj"], 13.5)
        self.assertAlmostEqual(d["late"]["bucket_coverage_pct"], 83.333)

    def test_upstream_bucket_can_span_a_long_gap_without_extrapolating_work(self):
        data = body()
        data["series"][0].update(secs=181, movingSecs=181)
        for bucket in data["series"][1:]:
            bucket["start"] += 121
        d = Sync._decoupling_detail(data)
        self.assertEqual(d["early"]["duration_s"], 241)
        self.assertEqual(d["early"]["paired_s"], 120)
        self.assertEqual(d["early"]["work_kj"], 21.6)
        self.assertEqual(d["early"]["avg_power_w"], 180)
        self.assertEqual(d["early"]["bucket_coverage_pct"], 49.793)

    def test_missing_values_hr_zero_and_partial_coverage(self):
        data = body()
        data["series"][0]["watts"] = None
        data["series"][2]["hr"] = 0
        d = Sync._decoupling_detail(data)
        self.assertEqual(d["early"]["bucket_coverage_pct"], 50)
        self.assertEqual(d["late"]["bucket_coverage_pct"], 50)
        self.assertEqual(d["early"]["work_kj"], 10.8)
        data["series"][1]["hr"] = None
        self.assertIsNone(Sync._decoupling_detail(data))

    def test_upstream_precision_remains_distinct_from_rounded_bucket_reconstruction(self):
        data = body()
        data["powerHrSecond"] = 1.637
        data["decoupling"] = (1.8 - 1.637) / 1.8 * 100
        d = Sync._decoupling_detail(data)
        self.assertEqual(d["upstream_decoupling"], data["decoupling"])
        self.assertNotAlmostEqual(d["bucket_decoupling"], d["upstream_decoupling"], places=3)

    def test_malformed_shape_rejected_without_guessed_defaults(self):
        mutations = [("series", []), ("start", -1), ("mid", True), ("end", 99),
                     ("bucketSize", 0), ("warmup", None), ("cooldown", -1),
                     ("hrLag", "15"), ("powerHrFirst", 0), ("powerHrSecond", float("nan")),
                     ("decoupling", 50)]
        for field, value in mutations:
            data = body()
            data[field] = value
            with self.subTest(field=field):
                self.assertIsNone(Sync._decoupling_detail(data))
        for field, value in (("start", -1), ("secs", 0), ("movingSecs", 61),
                             ("watts", -1), ("hr", True)):
            data = body()
            data["series"][0][field] = value
            self.assertIsNone(Sync._decoupling_detail(data))

    def test_zero_and_negative_decoupling_preserved(self):
        for hrs in ((100, 100, 100, 100), (110, 110, 100, 100)):
            data = body(hrs=hrs)
            d = Sync._decoupling_detail(data)
            self.assertEqual(d["upstream_decoupling"], data["decoupling"])
            self.assertLessEqual(d["upstream_decoupling"], 0)


class PlanContext(unittest.TestCase):
    def test_followed_partly_followed_progressive_descending_steady(self):
        for status in ("followed", "partly_followed"):
            for targets, expected in (((160, 200), True), ((200, 160), True), ((180, 180), False)):
                result = Sync._decoupling_plan_context(summary(targets, status=status))
                self.assertEqual(result["changes_power"], expected)
                self.assertEqual(result["basis"], "qualified_step_work_time_halves")

    def test_uncertain_or_unpaired_summary_never_claims_plan_changes(self):
        for status in ("not_followed", "not_assessed", "no_steps", "no_plan", "unknown"):
            self.assertIsNone(Sync._decoupling_plan_context(summary(status=status)))
        for field, value in (("method", "plan_match_v1"), ("plan", {}), ("omitted_steps", 1),
                             ("issues", ["pairing_conflict"]), ("steps", [])):
            data = summary()
            data[field] = value
            self.assertIsNone(Sync._decoupling_plan_context(data))

    def test_null_actual_or_match_is_not_qualified(self):
        for index, value in ((0, 0), (1, None), (2, None), (3, None)):
            data = summary()
            data["steps"][0][index] = value
            self.assertIsNone(Sync._decoupling_plan_context(data))
        # Disagreement is still an observed actual under partly_followed.
        data = summary(status="partly_followed")
        data["steps"][0][3] = False
        self.assertTrue(Sync._decoupling_plan_context(data)["changes_power"])

    def test_target_ranges_need_separation_not_only_midpoint_change(self):
        self.assertFalse(Sync._decoupling_plan_context(summary(([160, 200], [180, 220])))["changes_power"])
        self.assertTrue(Sync._decoupling_plan_context(summary(([150, 160], [180, 190])))["changes_power"])
        self.assertFalse(Sync._decoupling_plan_context(summary((200, 210)))["changes_power"])

    def test_duration_weighting_and_repeats_use_only_supplied_measured_work_time(self):
        data = summary()
        data["steps"] = [[999, 160, 160, True], [299, 200, 200, True, [3, 100, 15]],
                         [999, 160, 160, True]]
        result = Sync._decoupling_plan_context(data)
        self.assertEqual(result["early_target_w"], result["late_target_w"])
        self.assertFalse(result["changes_power"])


class FetchCache(unittest.TestCase):
    def setUp(self):
        self.sync = Sync("synthetic", "synthetic")
        patcher = mock.patch.object(mod.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, act=None, previous=None, plan=None, sync=None):
        return (sync or self.sync)._build_decoupling_for_activity(
            act or raw(), previous or {}, True, True, plan or {})

    def test_url_headers_timeout_and_single_new_request(self):
        with mock.patch.object(mod.requests, "get", return_value=response()) as get:
            self.assertIn("decoupling_detail", self.build())
        self.assertEqual(get.call_count, 1)
        args, kwargs = get.call_args
        self.assertEqual(args[0], self.sync.INTERVALS_BASE_URL + "/activity/synthetic/power-vs-hr")
        self.assertEqual(kwargs["headers"], {"Authorization": "Basic " + self.sync.intervals_auth,
                                           "Accept": "application/json"})
        self.assertEqual(kwargs["timeout"], (5, 30))

    def test_no_power_no_hr_noncycling_no_id_cost_no_requests(self):
        with mock.patch.object(mod.requests, "get") as get:
            for act, power, hr in ((raw(), False, True), (raw(), True, False),
                                    (raw(type="Run"), True, True), (raw(id=None), True, True)):
                self.assertEqual(self.sync._build_decoupling_for_activity(act, {}, power, hr, {}), {})
            get.assert_not_called()

    def test_success_cache_hit_same_run_and_next_instance(self):
        with mock.patch.object(mod.requests, "get", return_value=response()) as get:
            first = self.build()
            self.assertEqual(self.build(), first)
            second = self.build(previous={"synthetic": first}, sync=Sync("synthetic", "synthetic"))
            self.assertEqual(second, first)
            self.assertEqual(get.call_count, 1)
        second["decoupling_detail"]["early"]["avg_power_w"] = 0
        self.assertEqual(first["decoupling_detail"]["early"]["avg_power_w"], 180)

    def test_changed_fingerprint_refetches_and_failed_refresh_drops_old_detail(self):
        with mock.patch.object(mod.requests, "get", return_value=response()):
            first = self.build()
        for field, value in (("icu_sync_date", "new"), ("moving_time", 5500),
                             ("icu_warmup_time", 600), ("icu_cooldown_time", 60),
                             ("decoupling", 4), ("average_heartrate", 106)):
            sync = Sync("synthetic", "synthetic")
            with mock.patch.object(mod.requests, "get", return_value=response(503)) as get:
                self.assertEqual(self.build(raw(**{field: value}), {"synthetic": first}, sync=sync), {})
                self.assertEqual(get.call_count, 3)

    def test_plan_context_recomputed_on_cache_hit_including_revocation(self):
        with mock.patch.object(mod.requests, "get", return_value=response()) as get:
            first = self.build(plan=summary())
            self.assertEqual(first["decoupling_detail"]["comparability"], "plan_changes_power")
            later = self.build(previous={"synthetic": first}, plan=summary(status="not_assessed"))
            self.assertEqual(later["decoupling_detail"]["comparability"], "steady_power")
            self.assertNotIn("plan_comparison", later["decoupling_detail"])
            self.assertEqual(get.call_count, 1)

    def test_terminal_cache_and_fingerprint_invalidation(self):
        for status in (404, 410):
            sync = Sync("synthetic", "synthetic")
            with mock.patch.object(mod.requests, "get", return_value=response(status)) as get:
                first = self.build(sync=sync)
                self.assertNotIn("decoupling_detail", first)
                self.assertEqual(first["decoupling_cache"]["error"], f"http_{status}")
                self.assertEqual(self.build(previous={"synthetic": first}, sync=sync), first)
                self.assertEqual(get.call_count, 1)
                self.build(raw(icu_sync_date="changed"), {"synthetic": first}, sync=sync)
                self.assertEqual(get.call_count, 2)

    def test_transient_http_transport_parse_and_shape_retry_next_sync(self):
        for status in (401, 403, 429, 500, 503):
            sync = Sync("synthetic", "synthetic")
            with mock.patch.object(mod.requests, "get", return_value=response(status)):
                self.assertEqual(self.build(sync=sync), {})
        for error in (mod.requests.exceptions.Timeout(), mod.requests.exceptions.ConnectionError()):
            with mock.patch.object(mod.requests, "get", side_effect=error):
                self.assertEqual(self.build(sync=Sync("synthetic", "synthetic")), {})
        for bad in ([], {}, {"series": []}):
            with mock.patch.object(mod.requests, "get", return_value=response(data=bad)):
                self.assertEqual(self.build(sync=Sync("synthetic", "synthetic")), {})
        bad = response()
        bad.json.side_effect = ValueError("synthetic")
        with mock.patch.object(mod.requests, "get", return_value=bad):
            self.assertEqual(self.build(), {})
        with mock.patch.object(mod.requests, "get", return_value=response()):
            self.assertIn("decoupling_detail", self.build(sync=Sync("synthetic", "synthetic")))

    def test_success_after_retry_and_shared_retry_budget(self):
        with mock.patch.object(mod.requests, "get", side_effect=[response(503), response()]) as get:
            self.assertIn("decoupling_detail", self.build())
            self.assertEqual(get.call_count, 2)
        self.sync._read_retry_extra_attempts_used = 4
        with mock.patch.object(mod.requests, "get", return_value=response(503)) as get:
            self.assertEqual(self.build(raw(id="another")), {})
            self.assertEqual(get.call_count, 1)

    def test_activity_endpoint_disagreement_does_not_attach_or_cache_mismatched_detail(self):
        with mock.patch.object(mod.requests, "get", return_value=response()):
            self.assertEqual(self.build(raw(decoupling=0)), {})
        with mock.patch.object(mod.requests, "get", return_value=response(data=body(hrs=(100, 100, 100, 100)))):
            self.assertIn("decoupling_detail", self.build(raw(decoupling=0), sync=Sync("synthetic", "synthetic")))

    def test_budget_success_and_failed_duplicate_requests(self):
        for status, expected in ((200, 8), (503, 12)):
            sync = Sync("synthetic", "synthetic")
            with mock.patch.object(mod.requests, "get", return_value=response(status)) as get:
                for i in range(20):
                    act = raw(id=f"synthetic-{i}")
                    first = self.build(act, sync=sync)
                    self.assertEqual(self.build(act, sync=sync), first)
                    self.assertEqual("decoupling_detail" in first, status == 200 and i < 8)
                self.assertEqual(get.call_count, expected)
                self.assertEqual(sync._decoupling_fetches_used, 8)

    def test_previous_latest_carries_success_and_terminal_state(self):
        with mock.patch.object(mod.requests, "get", return_value=response()):
            first = dict(id="synthetic", **self.build())
        with tempfile.TemporaryDirectory() as directory:
            self.sync.data_dir = Path(directory)
            (self.sync.data_dir / "latest.json").write_text(json.dumps({"recent_activities": [first]}))
            previous = self.sync._load_previous_latest()
        with mock.patch.object(mod.requests, "get") as get:
            self.assertIn("decoupling_detail", self.build(previous=previous))
            get.assert_not_called()

    def test_format_integration_runs_after_all_curves_and_does_not_mutate_intervals(self):
        self.sync._intervals_data = {"activities": []}
        before = copy.deepcopy(self.sync._intervals_data)
        calls = []
        def get(url, **kwargs):
            calls.append(url.rsplit("/", 1)[-1])
            if url.endswith("power-curve.json"):
                return response(data={"secs": [1], "watts": [500]})
            return response(data=body(hrs=(100, 100, 100, 100) if "/a/" in url else (102, 102, 100, 100)))
        with mock.patch.object(self.sync, "_build_terrain_for_activity", return_value={}), \
                mock.patch.object(self.sync, "_build_weather_for_activity", return_value={}), \
                mock.patch.object(self.sync, "_get_activity_messages", return_value=("ok", [])), \
                mock.patch.object(mod.requests, "get", side_effect=get):
            rows = self.sync._format_activities([raw(id="a", decoupling=0), raw(id="b", decoupling=-2)])
        self.assertEqual(calls, ["power-curve.json", "power-curve.json", "power-vs-hr", "power-vs-hr"])
        self.assertEqual([r["decoupling"] for r in rows], [0, -2])
        self.assertTrue(all("decoupling_detail" in r for r in rows))
        self.assertEqual(self.sync._intervals_data, before)

    def test_durability_gates_and_trim_context_do_not_change_aggregates(self):
        acts = [raw(decoupling=0, icu_variability_index=1.05, icu_warmup_time=600),
                raw(decoupling=-2, icu_variability_index=1),
                raw(decoupling=99, moving_time=5399, icu_variability_index=1),
                raw(decoupling=99, icu_variability_index=1.051)]
        result = self.sync._calculate_durability(acts, acts)
        self.assertEqual(result["mean_decoupling_7d"], -1)
        self.assertEqual(result["mean_decoupling_28d"], -1)


if __name__ == "__main__":
    unittest.main()
