"""Offline migration and bounded adjacent-source evidence, synthetic data only."""
import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import test_session_structure as base
import test_short_bouts as bouts
import test_sleep_context as sleep
from _harness import install_verb_guard, restore_verb_guard


def setUpModule():
    global originals
    originals = install_verb_guard(base.mod.requests)


def tearDownModule():
    restore_verb_guard(base.mod.requests, originals)


class IntervalData(unittest.TestCase):
    repeated = base.LiveShape.repeated
    resolve = base.LiveShape.resolve
    build = base.Structure.build

    def setUp(self):
        self.sync = base.Sync("synthetic", "synthetic")
        self.act, self.event, self.streams = base.inputs()
        self.repeated(count=3, lag=0)
        for method in ("_build_terrain_for_activity", "_build_weather_for_activity",
                       "_build_power_curve_for_activity", "_build_decoupling_for_activity"):
            p = mock.patch.object(self.sync, method, return_value={})
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(self.sync, "_get_activity_messages", return_value=("ok", []))
        p.start()
        self.addCleanup(p.stop)

    def prepare(self):
        self.sync._intervals_data = {"activities": []}
        with mock.patch.object(self.sync, "_fetch_activity_streams", return_value=("ok", self.streams)):
            return self.sync._prepare_session_structures([self.act], [self.event], {}, now=base.NOW)[self.act["id"]]

    def formatted(self, prior=None):
        return self.sync._format_activities([self.act], paired_events=[self.event],
                                           previous_latest_lookup=prior)[0]

    def test_adjacent_sources_disagree_without_changing_judge(self):
        block = self.prepare()
        self.act["interval_summary"] = ["1x 60s 99w"]
        row = self.formatted()
        self.assertNotIn("structure_summary", row)
        data = row["interval_data"]
        self.assertEqual(data["version"], 1)
        expected = self.sync._structure_summary(block)
        expected["plan"].setdefault("name", None)
        self.assertEqual(data["plan_match"], expected)
        self.assertEqual(data["icu_detected"], self.act["interval_summary"])
        evidence = data["compliance"]
        self.assertEqual((evidence["completion"], evidence["on_target"], evidence["total"]), (True, 3, 3))
        self.assertEqual(evidence["sets"], [{"step": 2, "completion": True, "on_target": 3, "total": 3}])
        self.assertEqual(evidence["status"], "context_required")
        self.assertFalse(evidence["adherence_available"])
        self.assertNotIn('"bouts"', json.dumps(data))
        self.assertNotIn('"sprints"', json.dumps(data))
        self.assertNotIn("progression", evidence)
        self.act["interval_summary"] = []
        self.assertEqual(self.formatted()["interval_data"]["compliance"], evidence)

    def test_transition_and_nearby_miss_match_independent_oracle(self):
        for second, expected in ((89, 3), (88, 2)):
            self.repeated(count=3, lag=0)
            self.streams["watts"][second] = 0
            block = self.prepare()
            result = self.formatted()["interval_data"]["compliance"]
            self.assertEqual(result["on_target"], expected)
            self.assertEqual(result["on_target"], bouts.count_result([block["steps"][1]], block.get("offset_s", 0))[0])

    def test_standard_coverage_and_incomplete_work_block_counts(self):
        self.streams["watts"][61:63] = [None, None]
        self.prepare()
        result = self.formatted()["interval_data"]["compliance"]
        self.assertIsNone(result["completion"])
        self.assertIsNone(result["on_target"])
        self.assertEqual(result["total"], 3)
        self.repeated(count=3, lag=0)
        self.act["elapsed_time"] = 165
        self.streams = {k: v[:166] for k, v in self.streams.items()}
        self.prepare()
        result = self.formatted()["interval_data"]["compliance"]
        self.assertFalse(result["completion"])
        self.assertIsNone(result["on_target"])

    def test_sprint_and_average_boundaries(self):
        for duration in (20, 21, 60, 61):
            sequence = [(60, 120)] + [(duration, 280), (10, 90)] * 3 + [(60, 110)]
            self.event["workout_doc"] = {"duration": sum(d for d, _ in sequence), "steps": [
                {"duration": d, "power": {"value": w, "units": "w"}} for d, w in sequence]}
            watts = [w for d, w in sequence for _ in range(d)] + [0]
            self.streams = {"time": list(range(len(watts))), "watts": watts}
            self.act["elapsed_time"] = len(watts) - 1
            block = self.prepare()
            result = self.formatted()["interval_data"]["compliance"]
            self.assertEqual((result["on_target"], result["total"]), (3, 3), duration)
            step = block["steps"][1]
            if duration <= 20:
                step["sprints"][0][2] = step["sprint_target_w"] - .01
                self.assertEqual(self.formatted()["interval_data"]["compliance"]["on_target"], 2)

    def test_invalid_rows_fail_closed_without_reducing_denominator(self):
        block = self.prepare()
        for mutate in (lambda b: b["steps"][1]["bouts"].pop(),
                       lambda b: b["steps"][1]["bouts"][0].__setitem__(5, 1000),
                       lambda b: b["steps"][1]["bouts"][0].__setitem__(6, float("nan")),
                       lambda b: b["steps"][1]["bouts"][1].__setitem__(0, 999)):
            bad = copy.deepcopy(block)
            mutate(bad)
            self.sync._intervals_data["activities"][0]["session_structure"] = bad
            result = self.formatted()["interval_data"]["compliance"]
            self.assertEqual(result["reason"], "invalid_evidence")
            self.assertIsNone(result["on_target"])
            json.dumps(result, allow_nan=False)

    def test_stale_legacy_and_conflicting_identity_never_certify(self):
        block = self.prepare()
        for key, value in (("version", 5), ("bout_metrics_version", 3), ("status", "not_followed")):
            old = block[key]
            block[key] = value
            self.assertIsNone(self.formatted()["interval_data"]["compliance"]["on_target"])
            block[key] = old
        block["cache"]["status"] = "stale"
        self.assertIsNone(self.formatted()["interval_data"]["compliance"]["on_target"])
        block["cache"].pop("status")
        self.event["workout_doc"]["steps"][0]["duration"] += 1
        self.assertIsNone(self.formatted()["interval_data"]["compliance"]["on_target"])

    def test_expiry_preserves_plan_match_only_and_old_new_readers(self):
        self.prepare()
        first = self.formatted()
        self.sync._intervals_data = {"activities": []}
        for old_shape in (False, True):
            saved = copy.deepcopy(first)
            if old_shape:
                saved["structure_summary"] = saved.pop("interval_data")["plan_match"]
            with tempfile.TemporaryDirectory() as folder:
                self.sync.data_dir = Path(folder)
                (self.sync.data_dir / "latest.json").write_text(json.dumps({"recent_activities": [saved]}))
                prior = self.sync._load_previous_latest()
            current = self.formatted(prior)
            self.assertTrue(current["interval_data"]["plan_match"]["assessment_retained"])
            self.assertIsNone(current["interval_data"]["compliance"]["on_target"])
            self.assertNotIn("structure_summary", current)

    def test_detector_absence_is_not_empty_or_retained_compliance(self):
        self.prepare()
        entry = self.sync._intervals_data["activities"][0]
        entry["interval_summary"] = ["3x 30s 280w"]
        self.assertEqual(self.formatted()["interval_data"]["icu_detected"], entry["interval_summary"])
        for value in (None, {}, [300], "3x 30s 280w"):
            self.act["interval_summary"] = value
            self.assertIsNone(self.formatted()["interval_data"]["icu_detected"])

    def test_ordinary_steps_require_main_work_selection(self):
        self.act, self.event, self.streams = base.inputs()
        self.prepare()
        result = self.formatted()["interval_data"]["compliance"]
        self.assertIn("ai_selects_main_work_from_authoritative_prescription", result["reason"])
        self.assertEqual(result["status"], "context_required")
        self.assertEqual(result["aggregate_reason"], "no_compressed_sets")
        self.assertEqual(result["steps"], [[i, p, 100, "long_avg", True, 1, 1, True]
                                         for i, p in enumerate((100, 200, 300), 1)])
        self.assertIsNone(result["completion"])
        self.assertIsNone(result["on_target"])
        self.assertIsNone(result["total"])
        self.assertFalse(result["adherence_available"])

    def test_mixed_patterns_keep_set_observations_without_combined_count(self):
        sequence = [(60, 120)] + [(30, 280), (10, 90)] * 3 + [(60, 110)]
        sequence += [(40, 280), (10, 90)] * 3 + [(60, 110)]
        self.event["workout_doc"] = {"duration": sum(d for d, _ in sequence), "steps": [
            {"duration": d, "power": {"value": w, "units": "w"}} for d, w in sequence]}
        watts = [w for d, w in sequence for _ in range(d)] + [0]
        self.streams = {"time": list(range(len(watts))), "watts": watts}
        self.act["elapsed_time"] = len(watts) - 1
        self.prepare()
        result = self.formatted()["interval_data"]["compliance"]
        self.assertEqual(result["aggregate_reason"], "mixed_patterns")
        self.assertEqual([s["on_target"] for s in result["sets"]], [3, 3])
        self.assertIsNone(result["on_target"])

    def test_new_format_needs_no_extra_read(self):
        self.prepare()
        with mock.patch.object(self.sync, "_intervals_get", side_effect=AssertionError("extra read")):
            self.assertEqual(self.formatted()["interval_data"]["compliance"]["on_target"], 3)

    def test_single_step_evaluator_boundaries_and_exact_durations(self):
        for duration, evaluator in ((20, "sprint"), (21, "short_avg"), (60, "short_avg"), (61, "long_avg")):
            self.act, self.event, self.streams = base.inputs(duration=duration)
            block = self.prepare()
            self.assertEqual([s["duration_s"] for s in block["steps"]], [duration] * 3)
            rows = self.formatted()["interval_data"]["compliance"]["steps"]
            self.assertEqual(rows, [[i, p, duration, evaluator, True, 1, 1, True]
                                    for i, p in enumerate((100, 200, 300), 1)])

    def test_single_range_targets_and_sprint_lower_bound(self):
        for duration in (20, 30, 100):
            self.act, self.event, self.streams = base.inputs(duration=duration)
            self.event["workout_doc"]["steps"][0]["power"] = {"units": "w", "min": 100, "max": 110}
            self.prepare()
            row = self.formatted()["interval_data"]["compliance"]["steps"][0]
            self.assertEqual(row[1], [100, 110])
            self.assertEqual(row[4:], [True, 1, 1, True])
        self.act, self.event, self.streams = base.inputs(duration=20)
        self.event["workout_doc"]["steps"][0]["power"] = {"units": "w", "min": 100.01, "max": 110}
        self.prepare()
        self.assertEqual(self.formatted()["interval_data"]["compliance"]["steps"][0][4:], [True, 0, 1, True])

    def test_lower_target_steps_remain_visible_without_roles_or_new_aggregate(self):
        self.prepare()
        result = self.formatted()["interval_data"]["compliance"]
        self.assertEqual([r[0] for r in result["steps"]], [1, 2, 3])
        self.assertEqual([r[6] for r in result["steps"]], [1, 3, 1])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["on_target"], 3)
        self.assertNotIn("role", json.dumps(result))
        self.assertNotIn("warm", json.dumps(result))
        self.assertNotIn("cool", json.dumps(result))
        self.assertNotIn("recovery", json.dumps(result))

    def test_rounded_long_mean_cannot_cross_band_edge(self):
        for actual, expected in ((96.9, 0), (97, None), (97.1, 1),
                                 (104.9, 1), (105, None), (105.1, 0)):
            self.act, self.event, self.streams = base.inputs()
            self.streams["watts"][:100] = [actual] * 100
            self.prepare()
            row = self.formatted()["interval_data"]["compliance"]["steps"][0]
            self.assertEqual(row[5], expected, actual)
            self.assertEqual(row[7], expected is not None)

    def test_rounded_standard_coverage_and_missing_adherence_remain_unavailable(self):
        self.act, self.event, self.streams = base.inputs(duration=30)
        block = self.prepare()
        for pct, completion in ((94.99, None), (95, None), (95.01, True)):
            block["steps"][0]["actual"][6] = pct
            self.assertIs(self.formatted()["interval_data"]["compliance"]["steps"][0][4], completion)
        block["steps"][0].pop("adherence")
        self.assertIsNone(self.formatted()["interval_data"]["compliance"]["steps"][0][5])

    def test_clipped_step_uses_prescribed_evaluator_and_no_completion_credit(self):
        self.act, self.event, self.streams = base.inputs(duration=60)
        self.act["elapsed_time"] = 165
        self.streams = {k: v[:166] for k, v in self.streams.items()}
        self.prepare()
        row = self.formatted()["interval_data"]["compliance"]["steps"][-1]
        self.assertEqual(row[2:4], [60, "short_avg"])
        self.assertEqual(row[4:], [False, None, 1, False])

    def test_missing_prescribed_metadata_refreshes_without_changing_schema_version(self):
        self.act, self.event, self.streams = base.inputs()
        block = self.prepare()
        block["steps"][0].pop("duration_s")
        self.assertEqual(self.formatted()["interval_data"]["compliance"]["steps"][0][2:],
                         [None, None, None, None, 1, False])
        with mock.patch.object(self.sync, "_fetch_activity_streams", return_value=("ok", self.streams)) as fetch:
            refreshed = self.sync._prepare_session_structures([self.act], [self.event], {}, now=base.NOW + timedelta(hours=1))[self.act["id"]]
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(refreshed["version"], 6)
            self.sync._prepare_session_structures([self.act], [self.event], {}, now=base.NOW + timedelta(hours=2))
            self.assertEqual(fetch.call_count, 1)
        self.assertTrue(all(r[7] for r in self.formatted()["interval_data"]["compliance"]["steps"]))

    def test_no_per_step_verdict_for_unqualified_or_expired_detail(self):
        block = self.prepare()
        block["cache"]["status"] = "stale"
        result = self.formatted()["interval_data"]["compliance"]
        self.assertEqual((result["status"], result["steps"]), ("unavailable", []))
        self.sync._intervals_data = {}
        result = self.formatted()["interval_data"]["compliance"]
        self.assertEqual((result["status"], result["steps"]), ("unavailable", []))

    def test_guidance_is_emitted_in_all_three_locations(self):
        with tempfile.TemporaryDirectory() as folder:
            output, _, sync = sleep.collect(base.mod, Path(folder))
            sync._generate_intervals([])
            overview = base.mod.DataOverview.build(sync, output)
            notes = [output["READ_THIS_FIRST"]["interval_data_note"],
                     overview["interval_data_note"], sync._intervals_data["interval_data_note"]]
        for note in notes:
            for token in ("interval_data", "plan_match", "timeline", "compliance", "session_structure",
                          "per-bout", ">=95%", "icu_detected", "interval_summary", "descriptive",
                          "device/app lap", "whole-session average/NP", "set mean", "#which-interval-data-to-use"):
                self.assertIn(token, note)


if __name__ == "__main__":
    unittest.main()
