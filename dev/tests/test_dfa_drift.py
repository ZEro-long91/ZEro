"""Deterministic DFA producer regressions. All streams and identities are synthetic.

These tests establish arithmetic and eligibility contracts, not physiological
validity. In particular, eligible missing data can still bias a contrast, and an
interval session with little supra time is not necessarily steady work.
"""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from _harness import (SYNC_PATH, install_verb_guard, load_module_by_path,
                      restore_verb_guard)

sync_mod = load_module_by_path("s11_sync_drift", SYNC_PATH)
IntervalsSync = sync_mod.IntervalsSync
_ORIGINAL_VERBS = {}


def setUpModule():
    global _ORIGINAL_VERBS
    _ORIGINAL_VERBS = install_verb_guard(sync_mod.requests)


def tearDownModule():
    restore_verb_guard(sync_mod.requests, _ORIGINAL_VERBS)


def stream(n=3600, declining=False):
    return {
        "dfa_a1": [1.5 - .6 * (i + .5) / n for i in range(n)] if declining else [1.2] * n,
        "artifacts": [0.0] * n,
        "heartrate": [120] * n,
        "watts": [180] * n,
        "time": list(range(n)),
    }


def reject(data, indices):
    for i in indices:
        data["artifacts"][i] = 6.0
    return data


def activity(block, order=1, sport="VirtualRide"):
    return {"activity_id": "synthetic-" + str(order), "date": "2000-01-01",
            "start_datetime": "2000-01-01T%02d:00:00" % order,
            "type": sport, "dfa": block}


class TestDfaDrift(unittest.TestCase):
    def setUp(self):
        self.sync = IntervalsSync.__new__(IntervalsSync)

    def compute(self, data):
        return self.sync._compute_dfa_block(data)

    def assert_status(self, block, status, reasons=None):
        # Preserve accepted short arithmetic fixtures and their other reasons.
        # Direct boundary tests below independently assert the duration veto.
        if status != "unavailable" and block["drift"] is not None and block["quality"]["total_secs"] < 5400:
            status = "descriptive_only"
            if reasons is not None:
                reasons = ["short_session"] + reasons
            else:
                self.assertEqual(block["drift_quality"]["reasons"][0], "short_session")
        self.assertEqual(block["drift_quality"]["status"], status)
        self.assertEqual(block["drift_quality"]["method"], "original_index_thirds_v1")
        if reasons is not None:
            self.assertEqual(block["drift_quality"]["reasons"], reasons)
        if block["drift"] is not None:
            self.assertEqual(block["drift"]["interpretable"], status == "eligible")

    def test_complete_stable_zero_survives_summary(self):
        block = self.compute(stream(5400))
        self.assert_status(block, "eligible", [])
        self.assertEqual(block["drift"], {"first_third_avg": 1.2, "last_third_avg": 1.2,
                                         "delta": 0.0, "interpretable": True})
        summary = self.sync._build_dfa_summary(block)
        self.assertEqual(summary["drift_delta"], 0)
        self.assertTrue(summary["drift_interpretable"])
        self.assertEqual(summary["quality"]["coverage_by_third"], block["quality"]["coverage_by_third"])

    def test_complete_decline_has_hand_derived_means(self):
        block = self.compute(stream(5400, declining=True))
        self.assertEqual(block["drift"], {"first_third_avg": 1.4, "last_third_avg": 1.0,
                                         "delta": -.4, "interpretable": True})

    def test_equal_loss_counts_have_different_location_results(self):
        cases = [(range(0, 3600, 5), -.4, "eligible"),
                 (range(720), -.34, "descriptive_only"),
                 (range(1440, 2160), -.4, "eligible"),
                 (range(2880, 3600), -.34, "descriptive_only")]
        for missing, delta, status in cases:
            with self.subTest(status=status, missing_start=missing.start):
                block = self.compute(reject(stream(declining=True), missing))
                self.assertEqual(block["quality"]["valid_pct"], 80)
                self.assertEqual(block["drift"]["delta"], delta)
                self.assert_status(block, status)
                early, middle, late = block["quality"]["coverage_by_third"]
                self.assertEqual((early["start_sample_idx"], early["end_sample_idx_exclusive"]), (0, 1200))
                self.assertEqual((late["start_sample_idx"], late["end_sample_idx_exclusive"]), (2400, 3600))
                self.assertIsNone(middle["bins"])

    def test_near_flag_boundary_is_not_rescued_by_moving_windows(self):
        data = stream()
        data["dfa_a1"] = [1.5 - .32 * (i + .5) / 3600 for i in range(3600)]
        self.assertEqual(self.compute(data)["drift"]["delta"], -.214)
        block = self.compute(reject(data, range(720)))
        self.assertEqual(block["drift"]["delta"], -.182)
        self.assert_status(block, "descriptive_only")

    def test_remainders_belong_to_middle(self):
        for n in (3601, 3602):
            with self.subTest(n=n):
                spans = self.compute(stream(n))["quality"]["coverage_by_third"]
                self.assertEqual([s["total_samples"] for s in spans], [1200, n - 2400, 1200])
                self.assertEqual(spans[0]["end_sample_idx_exclusive"], spans[1]["start_sample_idx"])
                self.assertEqual(spans[1]["end_sample_idx_exclusive"], spans[2]["start_sample_idx"])
                self.assertEqual(sum(b["total_samples"] for b in spans[2]["bins"]), 1200)

    def test_absent_empty_and_tiny_streams(self):
        self.assertIsNone(self.compute({}))
        self.assertIsNone(self.compute(stream(0)))
        for n in (1, 2, 5, 17, 177, 180):
            with self.subTest(n=n):
                block = self.compute(stream(n))
                self.assert_status(block, "unavailable", ["whole_session_duration"])
                self.assertIsNone(block["drift"])
                json.dumps(block, allow_nan=False)
                if n < 3:
                    self.assertIsNone(block["quality"]["coverage_by_third"][0]["valid_pct"])
                if n < 18:
                    self.assertEqual(block["quality"]["coverage_by_third"][0]["bins"], [])

    def test_direct_window_length_boundary(self):
        # Isolate this helper's 59/60-slot boundary from the outer 1200 gate.
        for length in (59, 60):
            n = length * 3
            mask = [True] * n
            quality = {"sufficient": True, "coverage_by_third":
                       self.sync._dfa_coverage_by_third(mask, mask, [0] * n)}
            drift, meta = self.sync._compute_dfa_drift([1.2] * n, mask, list(range(n)), quality, 0)
            if length == 59:
                self.assertIsNone(drift)
                self.assertEqual(meta["reasons"], ["window_too_short", "early_too_few_samples", "late_too_few_samples"])
            else:
                self.assertEqual(meta["status"], "descriptive_only")
                self.assertEqual(meta["reasons"], ["short_session"])

    def test_minimum_endpoint_count_does_not_salvage_later_data(self):
        for count in (59, 60):
            data = reject(stream(1713), range(count, 571))
            block = self.compute(data)
            self.assertTrue(block["quality"]["sufficient"])
            if count == 59:
                self.assert_status(block, "unavailable", ["early_too_few_samples"])
                summary = self.sync._build_dfa_summary(block)
                self.assertNotIn("drift_delta", summary)
                self.assertEqual(summary["drift_quality"], block["drift_quality"])
            else:
                self.assert_status(block, "descriptive_only")
                self.assertEqual(block["drift"]["first_third_avg"], 1.2)

    def test_whole_gate_count_and_emitted_percentage_boundaries(self):
        cases = [(1200, 1199, False, ["whole_session_duration"]),
                 (1200, 1200, True, []),
                 (2000, 1398, False, ["whole_session_coverage"]),
                 (2000, 1399, True, []),
                 (2000, 1199, False, ["whole_session_duration", "whole_session_coverage"])]
        for n, retained, sufficient, reasons in cases:
            with self.subTest(n=n, retained=retained):
                block = self.compute(reject(stream(n), range(retained, n)))
                self.assertEqual(block["quality"]["sufficient"], sufficient)
                if not sufficient:
                    self.assert_status(block, "unavailable", reasons)
                    for key in ("avg", "p25", "tiz_easy", "drift", "lt1_crossing"):
                        self.assertIsNone(block[key])

    def test_early_concentrated_insufficient_mask_emits_only_quality(self):
        data = stream(18000)
        reject(data, (i for i in range(6000) if i % 10 >= 3))
        reject(data, (i for i in range(6000, 12000) if i % 10 >= 8))
        reject(data, (i for i in range(12000, 18000) if i % 10 >= 9))
        block = self.compute(data)
        self.assertEqual(block["quality"]["valid_pct"], 66.7)
        self.assert_status(block, "unavailable", ["whole_session_coverage"])
        self.assertEqual([s["valid_pct"] for s in block["quality"]["coverage_by_third"]], [30, 80, 90])
        self.assertIsNone(block["drift"])
        self.assertIsNone(block["avg"])

    def test_endpoint_coverage_exact_count_boundary(self):
        data = reject(stream(), (i for i in range(1200) if i % 10 < 3))
        self.assert_status(self.compute(data), "eligible", [])
        data["artifacts"][1199] = 6
        block = self.compute(data)
        self.assert_status(block, "descriptive_only", ["early_coverage"])
        self.assertEqual(block["quality"]["coverage_by_third"][0]["drift_valid_samples"], 839)

    def test_sixth_coverage_exact_boundary(self):
        data = reject(stream(), range(0, 200, 2))
        self.assert_status(self.compute(data), "eligible", [])
        data["artifacts"][199] = 6
        self.assert_status(self.compute(data), "descriptive_only", ["early_distribution"])

    def test_many_short_gaps_fail_spread_with_high_endpoint_coverage(self):
        data = reject(stream(), (i for i in range(200) if i % 5 < 3))
        block = self.compute(data)
        self.assert_status(block, "descriptive_only", ["early_distribution"])
        early = block["quality"]["coverage_by_third"][0]
        self.assertEqual(early["drift_valid_pct"], 90)
        self.assertEqual(early["min_bin_drift_valid_pct"], 40)
        self.assertEqual(early["max_drift_gap_samples"], 3)

    def test_gap_floor_percentage_and_ceiling_boundaries(self):
        for n, cap, center in ((1800, 60, 100), (3600, 120, 400), (10800, 300, 600)):
            for gap in (cap, cap + 1):
                with self.subTest(n=n, gap=gap):
                    start = center - gap // 2
                    block = self.compute(reject(stream(n), range(start, start + gap)))
                    self.assertEqual(block["quality"]["coverage_by_third"][0]["allowed_drift_gap_samples"], cap)
                    self.assert_status(block, "eligible" if gap == cap else "descriptive_only",
                                       [] if gap == cap else ["early_gap"])

    def test_long_recording_leading_trailing_and_six_minute_gaps(self):
        for gap in (150, 180, 360):
            for end in (False, True):
                with self.subTest(gap=gap, end=end):
                    start = 10800 - gap if end else 0
                    block = self.compute(reject(stream(10800), range(start, start + gap)))
                    self.assert_status(block, "eligible" if gap <= 180 else "descriptive_only")
                    if gap == 360:
                        self.assertIn("late_gap" if end else "early_gap", block["drift_quality"]["reasons"])

    def test_gap_crossing_span_boundary_is_counted_on_each_side(self):
        block = self.compute(reject(stream(), range(1140, 1260)))
        spans = block["quality"]["coverage_by_third"]
        self.assertEqual([s["max_drift_gap_samples"] for s in spans], [60, 60, 0])
        self.assert_status(block, "eligible", [])

    def test_missing_artifact_is_a_drift_only_gap(self):
        data = stream()
        data["artifacts"][0] = None
        data["dfa_a1"][0] = 2.4
        block = self.compute(data)
        self.assertEqual(block["quality"]["valid_secs"], 3600)
        self.assertEqual(block["quality"]["artifact_state"], "partial")
        early = block["quality"]["coverage_by_third"][0]
        self.assertEqual((early["valid_samples"], early["drift_valid_samples"]), (1200, 1199))
        self.assertEqual((early["max_invalid_run_samples"], early["max_drift_gap_samples"]), (0, 1))
        self.assertEqual(block["drift"]["first_third_avg"], 1.2)
        self.assert_status(block, "eligible", [])

    def test_unknown_artifact_gap_limit_does_not_change_sufficiency(self):
        data = stream()
        data["artifacts"][140:261] = [None] * 121
        block = self.compute(data)
        self.assertEqual(block["quality"]["valid_pct"], 100)
        self.assert_status(block, "descriptive_only", ["early_gap"])

    def test_absent_artifacts_keep_tiz_but_cannot_produce_drift(self):
        data = stream()
        del data["artifacts"]
        block = self.compute(data)
        self.assertTrue(block["quality"]["sufficient"])
        self.assertEqual(block["tiz_easy"]["pct"], 100)
        self.assert_status(block, "unavailable", ["early_too_few_samples", "late_too_few_samples"])

    def test_malformed_artifacts_normalize_to_unknown_and_short_stream_pads(self):
        for value in (None, -1, True, "0", float("nan"), float("inf")):
            with self.subTest(value=value):
                data = stream()
                data["artifacts"][10] = value
                block = self.compute(data)
                self.assertEqual(block["quality"]["valid_secs"], 3600)
                self.assertEqual(block["quality"]["artifact_state"], "partial")
                self.assertEqual(block["quality"]["coverage_by_third"][0]["drift_valid_samples"], 1199)
                self.assert_status(block, "eligible")
        data = stream()
        data["artifacts"] = data["artifacts"][:-1]
        self.assertEqual(self.compute(data)["quality"]["coverage_by_third"][2]["drift_valid_samples"], 1199)

    def test_artifact_filter_five_is_retained_above_five_is_not(self):
        data = stream()
        data["artifacts"] = [5.0] * 3600
        self.assertEqual(self.compute(data)["quality"]["valid_secs"], 3600)
        data["artifacts"][0] = 5.01
        self.assertEqual(self.compute(data)["quality"]["valid_secs"], 3599)

    def test_nan_alpha1_is_excluded_from_all_rollups(self):
        data = stream()
        for i, value in enumerate((float("nan"), float("inf"), float("-inf"), True, "1.2", None, 0)):
            data["dfa_a1"][100 + i] = value
        block = self.compute(data)
        self.assertEqual(block["quality"]["valid_secs"], 3593)
        self.assertEqual(block["avg"], 1.2)
        self.assertEqual(block["tiz_easy"]["secs"], 3593)
        self.assertEqual(block["drift"]["delta"], 0)
        json.dumps(block, allow_nan=False)

    def test_all_nan_is_insufficient_with_finite_quality(self):
        data = stream()
        data["dfa_a1"] = [float("nan")] * 3600
        block = self.compute(data)
        self.assertEqual(block["quality"]["valid_secs"], 0)
        self.assert_status(block, "unavailable", ["whole_session_duration", "whole_session_coverage"])
        json.dumps(block, allow_nan=False)

    def test_nonfinite_drift_arithmetic_is_unavailable(self):
        data = stream()
        data["dfa_a1"] = [1e308] * 3600
        block = self.compute(data)
        self.assertIsNone(block["drift"])
        self.assert_status(block, "unavailable", ["invalid_dfa"])
        json.dumps(block["drift_quality"], allow_nan=False)

    def test_time_missing_short_or_nonnumeric_is_unverified(self):
        for times in (None, [], list(range(3599)), [None] * 3600, [True] * 3600,
                      ["0"] * 3600, [float("nan")] * 3600):
            with self.subTest(kind=type(times).__name__):
                data = stream()
                data["time"] = times
                block = self.compute(data)
                self.assert_status(block, "descriptive_only", ["timebase_unverified"])
                self.assertEqual(block["drift"]["delta"], 0)

    def test_offset_time_is_valid(self):
        data = stream()
        data["time"] = [100 + i for i in range(3600)]
        self.assert_status(self.compute(data), "eligible", [])

    def test_duplicates_reverse_nonunit_and_pause_jump_are_nonuniform(self):
        variants = [[i // 2 for i in range(3600)], list(reversed(range(3600))),
                    [i / 2 for i in range(3600)], [2 * i for i in range(3600)],
                    [i if i < 1200 else i + 60 for i in range(3600)]]
        for times in variants:
            data = stream()
            data["time"] = times
            self.assert_status(self.compute(data), "descriptive_only", ["timebase_nonuniform"])

    def test_preserved_pause_slots_are_gaps_and_zero_power_stays_in_tiz(self):
        data = stream()
        data["dfa_a1"][100:120] = [None] * 20
        data["watts"] = [0] * 3600
        data["heartrate"] = [0] * 3600
        block = self.compute(data)
        self.assert_status(block, "eligible", [])
        self.assertEqual(block["tiz_easy"]["avg_watts"], 0)
        self.assertEqual(block["tiz_easy"]["avg_hr"], 0)
        self.assertEqual(block["quality"]["coverage_by_third"][0]["max_drift_gap_samples"], 20)

    def test_supra_emitted_percentage_boundary_is_preserved(self):
        for count, pct, status in ((540, 15.0, "eligible"), (541, 15.0, "eligible"),
                                   (542, 15.1, "descriptive_only")):
            data = stream()
            data["dfa_a1"][1200:1200 + count] = [.4] * count
            block = self.compute(data)
            self.assertEqual(block["tiz_supra"]["pct"], pct)
            self.assert_status(block, status, [] if status == "eligible" else ["supra_fraction"])

    def test_reason_order_is_time_coverage_distribution_gap_supra(self):
        data = stream()
        del data["time"]
        reject(data, list(range(361)) + list(range(3239, 3600)))
        data["dfa_a1"][1200:1800] = [.4] * 600
        block = self.compute(data)
        self.assert_status(block, "descriptive_only", ["timebase_unverified", "early_coverage", "late_coverage",
                           "early_distribution", "late_distribution", "early_gap", "late_gap", "supra_fraction"])

    def test_value_dependent_loss_can_bias_an_eligible_contrast(self):
        data = stream(5400)
        data["dfa_a1"] = [2.2 if i % 5 == 0 else 1.0 for i in range(5400)]
        self.assertEqual(self.compute(data)["drift"]["delta"], 0)
        block = self.compute(reject(data, range(3600, 5400, 5)))
        self.assert_status(block, "eligible", [])
        self.assertEqual(block["drift"]["delta"], -.24)

    def test_lost_climb_block_hides_real_endpoint_contrast(self):
        data = stream(10800)
        # A synthetic triangular hard-effort response: .5 -> .1 -> .5.
        low = [.1 + .4 * abs(2 * (i + .5) / 180 - 1) for i in range(180)]
        data["dfa_a1"][7200:7380] = low
        complete = self.compute(data)
        self.assertEqual(complete["drift"]["delta"], -.045)
        lost = self.compute(reject(data, range(7200, 7380)))
        self.assert_status(lost, "eligible", [])
        self.assertEqual(lost["drift"]["delta"], 0)
        self.assertIsNone(lost["tiz_supra"])

    def test_short_window_gap_floor_can_hide_more_than_flag_magnitude(self):
        data = stream(1200)
        data["dfa_a1"] = [1.5] * 1200
        data["dfa_a1"][836:896] = [.01] * 60
        complete = self.compute(data)
        self.assertLess(complete["drift"]["delta"], -.2)
        # Unknown artifacts do not reduce whole-session sufficiency. A gap
        # straddling two sixths passes the gap screens, but the duration veto
        # keeps this short contrast descriptive only.
        data["artifacts"][836:896] = [None] * 60
        lost = self.compute(data)
        self.assert_status(lost, "descriptive_only", [])
        self.assertEqual(lost["drift_quality"]["reasons"], ["short_session"])
        self.assertEqual(lost["drift"]["delta"], 0)
        self.assertEqual(lost["quality"]["valid_secs"], 1200)

    def test_interval_work_with_low_supra_time_can_pass_numerical_gate(self):
        data = stream(5400)
        data["watts"] = [300 if i % 300 < 60 else 140 for i in range(5400)]
        data["dfa_a1"] = [.6 if i % 300 < 60 else 1.2 for i in range(5400)]
        block = self.compute(data)
        self.assertIsNone(block["tiz_supra"])
        self.assert_status(block, "eligible", [])
        # No producer physiological flag exists. Independent workload context
        # remains necessary even when the observed supra veto does not fire.
        self.assertNotIn("physiological_drift", block)

    def test_tiz_boundaries_empty_bands_and_ties_remain_count_based(self):
        data = stream(4000)
        data["dfa_a1"] = [1.1] * 1000 + [1.0] * 500 + [.75] * 500 + [.5] * 1000 + [.49] * 1000
        block = self.compute(data)
        self.assertEqual([block[k]["secs"] for k in ("tiz_easy", "tiz_endurance", "tiz_tempo", "tiz_supra")], [1000] * 4)
        self.assertEqual(self.sync._build_dfa_summary(block)["dominant_band"], "supra")
        self.assertIsNone(self.compute(stream())["tiz_supra"])

    def test_tiz_selection_bias_remains_disclosed_not_reweighted(self):
        data = stream()
        data["dfa_a1"] = [1.2] * 2000 + [.7] * 1600
        self.assertEqual(self.sync._build_dfa_summary(self.compute(data))["dominant_band"], "easy")
        block = self.compute(reject(data, range(720)))
        summary = self.sync._build_dfa_summary(block)
        self.assertEqual(summary["dominant_band"], "tempo")
        self.assertEqual(summary["tiz_pct"]["easy"], 44.4)
        self.assertEqual(summary["quality"]["coverage_by_third"][0]["valid_pct"], 40)

    def test_raw_count_wins_even_when_displayed_band_percentages_tie(self):
        data = stream(10001)
        data["dfa_a1"] = [1.2] * 5001 + [.6] * 5000
        block = self.compute(data)
        self.assertEqual(block["tiz_easy"]["pct"], block["tiz_tempo"]["pct"])
        self.assertEqual(self.sync._build_dfa_summary(block)["dominant_band"], "easy")

    def test_crossing_contiguity_and_eligibility_remain_independent(self):
        data = stream()
        data["dfa_a1"][300:360] = [.75] * 60
        data["dfa_a1"][365:425] = [.75] * 60
        block = self.compute(data)
        crossing = block["lt1_crossing"]
        self.assertEqual(crossing["n_qualifying_segments"], 1)
        self.assertEqual(crossing["contiguous_secs"], 120)
        self.assertTrue(crossing["estimate_eligible"])
        data["dfa_a1"][365] = 1.2
        block = self.compute(data)
        self.assertEqual(block["lt1_crossing"]["contiguous_secs"], 60)
        self.assertEqual(block["lt1_crossing"]["n_qualifying_segments"], 1)
        data["artifacts"][250] = None
        block = self.compute(data)
        self.assertFalse(block["lt1_crossing"]["estimate_eligible"])
        self.assertNotIn("lt1_watts", self.sync._build_dfa_summary(block))


class TestDfaShortSession(unittest.TestCase):
    """Direct production-default expectations, without the status helper."""

    def setUp(self):
        self.sync = IntervalsSync.__new__(IntervalsSync)

    def test_89_minutes_59_seconds_is_descriptive_with_numbers(self):
        block = self.sync._compute_dfa_block(stream(5399, declining=True))
        self.assertTrue(block["quality"]["sufficient"])
        self.assertEqual(block["drift_quality"]["status"], "descriptive_only")
        self.assertEqual(block["drift_quality"]["reasons"], ["short_session"])
        self.assertEqual(block["drift"], {"first_third_avg": 1.4, "last_third_avg": 1.0,
                                         "delta": -.4, "interpretable": False})

    def test_90_minutes_is_eligible_when_other_checks_pass(self):
        self.assertEqual(IntervalsSync.DFA_DRIFT_MIN_SESSION_SAMPLES, 5400)
        block = self.sync._compute_dfa_block(stream(5400, declining=True))
        self.assertEqual(block["drift_quality"]["status"], "eligible")
        self.assertEqual(block["drift_quality"]["reasons"], [])
        self.assertEqual(block["drift"], {"first_third_avg": 1.4, "last_third_avg": 1.0,
                                         "delta": -.4, "interpretable": True})
        self.assertEqual(block["quality"]["coverage_by_third"][0]["total_samples"], 1800)

    def test_45_minute_clean_ride_keeps_descriptive_drift(self):
        block = self.sync._compute_dfa_block(stream(2700))
        self.assertEqual(block["drift_quality"]["status"], "descriptive_only")
        self.assertEqual(block["drift_quality"]["reasons"], ["short_session"])
        self.assertFalse(block["drift"]["interpretable"])
        self.assertEqual(block["drift"]["delta"], 0)
        self.assertEqual(block["quality"]["valid_secs"], 2700)
        self.assertEqual(block["tiz_easy"]["pct"], 100)

    def test_original_slot_count_not_retained_count_or_timestamp_offset(self):
        data = reject(stream(5400), range(0, 5400, 5))
        data["time"] = [100000 + i for i in range(5400)]
        block = self.sync._compute_dfa_block(data)
        self.assertEqual(block["quality"]["valid_secs"], 4320)
        self.assertEqual(block["quality"]["total_secs"], 5400)
        self.assertEqual(block["drift_quality"]["status"], "eligible")
        self.assertEqual(block["drift_quality"]["reasons"], [])

    def test_short_reason_precedes_every_other_computable_reason(self):
        data = stream()
        del data["time"]
        reject(data, list(range(361)) + list(range(3239, 3600)))
        data["dfa_a1"][1200:1800] = [.4] * 600
        block = self.sync._compute_dfa_block(data)
        self.assertEqual(block["drift_quality"]["status"], "descriptive_only")
        self.assertEqual(block["drift_quality"]["reasons"], [
            "short_session", "timebase_unverified", "early_coverage", "late_coverage",
            "early_distribution", "late_distribution", "early_gap", "late_gap", "supra_fraction"])

    def test_unavailable_reasons_are_not_replaced_or_extended(self):
        cases = [(stream(1199), ["whole_session_duration"]),
                 (reject(stream(2700), range(1199, 2700)),
                  ["whole_session_duration", "whole_session_coverage"]),
                 (reject(stream(1713), range(59, 571)), ["early_too_few_samples"])]
        for data, reasons in cases:
            with self.subTest(reasons=reasons):
                block = self.sync._compute_dfa_block(data)
                self.assertIsNone(block["drift"])
                self.assertEqual(block["drift_quality"]["status"], "unavailable")
                self.assertEqual(block["drift_quality"]["reasons"], reasons)

    def test_short_status_propagates_and_excludes_trailing_drift(self):
        block = self.sync._compute_dfa_block(stream(2700, declining=True))
        summary = self.sync._build_dfa_summary(block)
        self.assertEqual(summary["drift_delta"], -.4)
        self.assertFalse(summary["drift_interpretable"])
        self.assertEqual(summary["drift_quality"]["reasons"], ["short_session"])
        self.sync._intervals_data = {"schema_version": 2, "activities": [activity(block)]}
        profile = self.sync._calculate_dfa_a1_profile()
        self.assertEqual(profile["latest_session"]["drift_delta"], -.4)
        self.assertFalse(profile["latest_session"]["drift_interpretable"])
        self.assertEqual(profile["latest_session"]["drift_quality"]["reasons"], ["short_session"])
        trailing = profile["trailing_by_sport"]["cycling"]
        self.assertEqual(trailing["n_sessions"], 1)
        self.assertEqual(trailing["drift_sessions"], 0)
        self.assertIsNone(trailing["drift_delta_mean"])

    def test_90_minutes_does_not_override_other_failures(self):
        data = stream(5400)
        del data["time"]
        block = self.sync._compute_dfa_block(data)
        self.assertEqual(block["drift_quality"]["status"], "descriptive_only")
        self.assertEqual(block["drift_quality"]["reasons"], ["timebase_unverified"])
        self.assertFalse(block["drift"]["interpretable"])


class TestDfaConsumers(unittest.TestCase):
    def setUp(self):
        self.sync = IntervalsSync.__new__(IntervalsSync)
        self.good = self.sync._compute_dfa_block(stream(5400))

    def profile(self, activities, schema=2):
        self.sync._intervals_data = {"schema_version": schema, "activities": activities}
        return self.sync._calculate_dfa_a1_profile()

    def test_legacy_unknown_method_and_contradictory_metadata_fail_closed(self):
        variants = []
        legacy = copy.deepcopy(self.good)
        del legacy["drift_quality"]
        variants.append(legacy)
        for key, value in (("method", "future_method"), ("status", "descriptive_only"),
                           ("reasons", ["early_gap"]), ("reasons", None),
                           ("timebase_status", "unverified"), ("time_basis", "moving_time")):
            block = copy.deepcopy(self.good)
            block["drift_quality"][key] = value
            variants.append(block)
        for key, value in (("interpretable", False), ("delta", float("nan")),
                           ("first_third_avg", None), ("last_third_avg", float("inf"))):
            block = copy.deepcopy(self.good)
            block["drift"][key] = value
            variants.append(block)
        for block in variants:
            with self.subTest(metadata=block.get("drift_quality")):
                self.assertFalse(self.sync._build_dfa_summary(block)["drift_interpretable"])
                trailing = self.profile([activity(block)])["trailing_by_sport"]["cycling"]
                self.assertIsNone(trailing["drift_delta_mean"])
                self.assertEqual(trailing["drift_sessions"], 0)

    def test_missing_or_false_quality_never_licenses_drift(self):
        for quality in (None, {}, {"sufficient": False}):
            block = copy.deepcopy(self.good)
            block["quality"] = quality
            self.assertFalse(self.sync._dfa_drift_eligible(block))
            profile = self.profile([activity(block)])
            self.assertFalse(profile["latest_session"]["sufficient"])
            self.assertIsNone(profile["latest_session"]["drift_delta"])
            self.assertEqual(profile["trailing_by_sport"], {})

    def test_unsupported_schema_excludes_drift_without_changing_latest_selection(self):
        for schema in (None, 1, 3):
            profile = self.profile([activity(self.good)], schema)
            self.assertEqual(profile["latest_session"]["activity_id"], "synthetic-1")
            self.assertFalse(profile["latest_session"]["drift_interpretable"])
            self.assertEqual(profile["trailing_by_sport"]["cycling"]["drift_sessions"], 0)

    def test_latest_sufficient_selection_keeps_its_own_descriptive_result(self):
        data = stream(declining=True)
        del data["time"]
        descriptive = self.sync._compute_dfa_block(data)
        insufficient = self.sync._compute_dfa_block(stream(100))
        profile = self.profile([activity(self.good, 1), activity(descriptive, 2), activity(insufficient, 3)])
        latest = profile["latest_session"]
        self.assertEqual(latest["activity_id"], "synthetic-2")
        self.assertEqual(latest["drift_delta"], -.4)
        self.assertFalse(latest["drift_interpretable"])
        self.assertEqual(latest["drift_quality"]["reasons"], ["short_session", "timebase_unverified"])
        self.assertEqual(latest["quality"]["valid_secs"], 3600)

    def test_all_insufficient_fallback_has_counts_but_no_readings(self):
        bad = self.sync._compute_dfa_block(stream(100))
        profile = self.profile([activity(bad, 2), activity(bad, 1)])
        latest = profile["latest_session"]
        self.assertEqual(latest["activity_id"], "synthetic-2")
        self.assertFalse(latest["sufficient"])
        self.assertIsNone(latest["drift_delta"])
        self.assertIsNone(latest["avg"])
        self.assertEqual(latest["drift_quality"]["reasons"], ["whole_session_duration"])
        self.assertEqual(profile["trailing_by_sport"], {})

    def test_trailing_uses_only_eligible_in_latest_seven_sufficient(self):
        legacy = copy.deepcopy(self.good)
        del legacy["drift_quality"]
        unknown = copy.deepcopy(self.good)
        unknown["drift_quality"]["method"] = "future_method"
        descriptive = self.sync._compute_dfa_block(reject(stream(), range(720)))
        unavailable = self.sync._compute_dfa_block(reject(stream(1713), range(59, 571)))
        decline = self.sync._compute_dfa_block(stream(5400, declining=True))
        rows = [activity(block, i + 1) for i, block in enumerate(
            (decline, self.good, legacy, unknown, descriptive, unavailable, decline, self.good))]
        profile = self.profile(rows)
        trailing = profile["trailing_by_sport"]["cycling"]
        self.assertEqual(trailing["n_sessions"], 7)
        self.assertEqual(trailing["drift_sessions"], 3)
        self.assertEqual(trailing["drift_delta_mean"], -.133)
        self.assertEqual(trailing["drift_method"], "original_index_thirds_v1")
        self.assertEqual(trailing["lt1_eligible_sessions"], 0)

    def test_empty_and_noncycling_profile(self):
        self.assertIsNone(self.profile([]))
        profile = self.profile([activity(self.good, sport="Run")])
        self.assertFalse(profile["latest_session"]["validated"])
        self.assertFalse(profile["trailing_by_sport"]["run"]["validated"])


class TestDfaIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sync = IntervalsSync("athlete_test", "key_test")
        self.sync.data_dir = Path(self.tmp.name)

    def test_stream_adapter_preserves_time_from_synthetic_api_shape(self):
        payload = [{"type": k, "data": v} for k, v in stream(5400).items()]
        payload.append({"type": "altitude", "data": [10] * 5400})
        response = mock.Mock(status_code=200, headers={})
        response.json.return_value = payload
        with mock.patch.object(sync_mod.requests, "get", return_value=response):
            status, data = self.sync._fetch_activity_streams("synthetic", ["dfa_a1", "artifacts", "time"])
        self.assertEqual(status, "ok")
        self.assertEqual(data["time"], list(range(5400)))
        self.assertNotIn("altitude", data)
        self.assertTrue(self.sync._compute_dfa_block(data)["drift"]["interpretable"])

    def test_hash_invalidation_refetches_old_activity_with_time_and_schema_two(self):
        now = datetime.now()
        old = {"id": "synthetic-old", "start_date_local": (now - timedelta(days=10)).isoformat(),
               "type": "VirtualRide", "name": "Synthetic", "interval_summary": None}
        too_old = dict(old, id="synthetic-expired", start_date_local=(now - timedelta(days=16)).isoformat())
        cache = {"script_hash": "obsolete-hash", "schema_version": 1,
                 "activities": [{"activity_id": old["id"], "date": old["start_date_local"][:10],
                                 "dfa": {"avg": 9.9}}],
                 "fetch_state": {old["id"]: {"streams": {"status": "ok"}, "intervals": {"status": "ok"}}}}
        (self.sync.data_dir / self.sync.INTERVALS_FILE).write_text(json.dumps(cache))
        self.sync._last_retry_after_secs = None
        with mock.patch.object(self.sync, "_fetch_activity_intervals", return_value=("no_data", None)), \
             mock.patch.object(self.sync, "_fetch_activity_streams", return_value=("ok", stream(5400))) as fetch:
            ids = self.sync._generate_intervals([old, too_old], events=[])
        fetch.assert_called_once_with("synthetic-old", ["dfa_a1", "artifacts", "heartrate", "watts", "time"])
        data = self.sync._intervals_data
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["script_hash"], self.sync.script_hash)
        self.assertEqual(data["retention_days"], 14)
        self.assertEqual(len(data["activities"]), 1)
        self.assertEqual(data["activities"][0]["dfa"]["avg"], 1.2)
        self.assertEqual(data["activities"][0]["dfa"]["drift_quality"]["status"], "eligible")
        self.assertIn("synthetic-old", ids)  # Cached payload IDs; flags are set by the formatter.

    def test_activity_flags_and_summary_follow_existing_contract(self):
        raw = {"id": "synthetic", "start_date_local": "2000-01-01T10:00:00",
               "type": "VirtualRide", "name": "Synthetic ride", "moving_time": 3600}
        for n, intervals, has_dfa, has_intervals, has_summary in (
                (0, [], False, False, False), (100, [], True, False, False),
                (3600, [{"type": "RECOVERY"}], True, False, True),
                (3600, [{"type": "WORK"}], True, True, True)):
            with self.subTest(n=n, intervals=intervals):
                block = self.sync._compute_dfa_block(stream(n))
                self.sync._intervals_data = {"schema_version": 2, "activities": [
                    {"activity_id": "synthetic", "dfa": block, "intervals": intervals}]}
                with mock.patch.object(self.sync, "_build_terrain_for_activity", return_value={}), \
                     mock.patch.object(self.sync, "_build_weather_for_activity", return_value={}), \
                     mock.patch.object(self.sync, "_get_activity_messages", return_value=("ok", [])):
                    row = self.sync._format_activities([raw])[0]
                self.assertEqual(row["has_dfa"], has_dfa)
                self.assertEqual(row["has_intervals"], has_intervals)
                self.assertEqual("dfa_summary" in row, has_summary)

    def test_activity_summary_checks_enclosing_schema(self):
        raw = {"id": "synthetic", "start_date_local": "2000-01-01T10:00:00",
               "type": "VirtualRide", "name": "Synthetic ride", "moving_time": 3600}
        block = self.sync._compute_dfa_block(stream(5400))
        for schema in (None, 1, 2, 3):
            with self.subTest(schema=schema):
                self.sync._intervals_data = {"schema_version": schema, "activities": [
                    {"activity_id": "synthetic", "dfa": block, "intervals": []}]}
                with mock.patch.object(self.sync, "_build_terrain_for_activity", return_value={}), \
                     mock.patch.object(self.sync, "_build_weather_for_activity", return_value={}), \
                     mock.patch.object(self.sync, "_get_activity_messages", return_value=("ok", [])):
                    row = self.sync._format_activities([raw])[0]
                self.assertTrue(row["has_dfa"])
                self.assertFalse(row["has_intervals"])
                self.assertEqual(row["dfa_summary"]["drift_delta"], 0)
                self.assertEqual(row["dfa_summary"]["drift_interpretable"], schema == 2)


if __name__ == "__main__":
    unittest.main()
