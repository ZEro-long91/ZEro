"""Synthetic executed-load contradictions and post-Deload resumption."""
import copy
import unittest
from unittest import mock

import test_phase_scope as scope
from test_phase_alignment import activities
from test_phase_scope import plan, rows


class PhaseResumption(unittest.TestCase):
    setUp = scope.PhaseScope.setUp
    derive = scope.PhaseScope.derive

    def streams(self, ratio=1.2, hard_avg=1.7, hard_done=3):
        s1 = {"ctl_slope": -1.5, "hard_day_avg": hard_avg, "weeks_available": 4,
              "acwr_trend": "falling"}
        s2 = {"planned_tss_delta": 0.7, "next_7d_sessions": 4,
              "hard_sessions_planned": 0, "current_week_hard_days_completed": hard_done,
              "current_week_hard_days_total": hard_done,
              "plan_coverage_current_week": 0.4, "plan_coverage_next_week": 1.2,
              "next_week_tss_delta": None,
              "load_evidence": {"completed_tss_delta": None,
                  "next_week": {"known_tss": 0, "missing_tss": 6, "unknown_intensity": 12},
                  "day_aligned": {"elapsed_days": 5, "tss_delta": ratio,
                      "status": "reduced" if ratio <= 0.80 else "not_reduced"}}}
        return s1, s2

    def classify(self, s1, s2, previous="Deload"):
        return self.sync._phase_classify(s1, s2, previous, "good")

    def test_live_caller_resumes_after_deload_with_executed_hard_days_and_unknown_future_load(self):
        self.sync.week_start_day = 0
        weeks = rows("2026-10-19", completed=720)
        for i, row in enumerate(weeks):
            row.update(ctl_end=(80, 81, 77, 90)[i], acwr=(1.1, 1.0, 0.75, 1.0)[i],
                       hard_days=(2, 2, 1, 3)[i], primary_sport_tss=700 if i < 3 else 720)
        weeks[-2]["phase_detected"] = "Deload"
        weeks[-2]["_phase_load_compatible"] = True
        weeks[-2]["phase_load_basis"] = {"rule": self.sync.PHASE_LOAD_RULE,
                                        "status": "complete", "observation_cutoff": "2026-10-18"}
        values = [a for a in activities("2026-10-19", current=144)
                  if a["start_date_local"][:10] <= "2026-10-23"]
        # Real zone-shaped inputs produce the current and prior hard-day counts.
        for act in values:
            week, day = map(int, act["id"].split("-")[1:])
            if day < (3, 1, 2, 2)[week]:
                act["icu_zone_times"] = [{"id": "Z4", "secs": 700}]
        plans = [plan("2026-10-24", 70 if i < 3 else None, id=i, name="Scheduled session") for i in range(4)]
        plans += [plan("2026-10-26", None, id=100+i, name="Scheduled session",
                       sport_type="Ride" if i < 6 else "Run") for i in range(12)]
        with mock.patch.object(self.sync, '_load_weekly_rows_for_phase', return_value=weeks):
            result = self.derive(values, plans)["phase_detection"]
        self.assertEqual((result["phase"], result["confidence"], result["reason_codes"]),
                         ("Build", "low", ["BUILD_RESUMING_AFTER_DELOAD_TENTATIVE"]))
        self.assertEqual(result["previous_phase"], "Deload")
        s1, s2 = result["basis"]["stream_1"], result["basis"]["stream_2"]
        self.assertLess(s1["ctl_slope"], -1)
        self.assertEqual(s1["acwr_trend"], "falling")
        self.assertEqual(s2["current_week_hard_days_completed"], 3)
        self.assertEqual(s2["hard_sessions_planned"], 0)
        self.assertLess(s2["planned_tss_delta"], 0.80)
        self.assertIsNone(s2["next_week_load"])
        evidence = s2["load_evidence"]
        self.assertEqual(evidence["day_aligned"]["elapsed_days"], 5)
        self.assertGreater(evidence["day_aligned"]["tss_delta"], 1)
        self.assertEqual(evidence["full_week_reduction"], "contradicted")
        self.assertEqual(evidence["next_week"]["missing_tss"], 6)
        self.assertEqual(evidence["next_week"]["unknown_intensity"], 12)

    def test_both_recovery_branches_reject_nonreduced_execution(self):
        for hard_avg in (0, 1.7):
            for previous in ("Base", "Deload", None):
                with self.subTest(hard_avg=hard_avg, previous=previous):
                    s1, s2 = self.streams(hard_avg=hard_avg)
                    result = self.classify(s1, s2, previous)
                    self.assertNotIn(result[0], ("Recovery", "Deload"))
                    if previous == "Deload":
                        self.assertEqual(result, ("Build", "low", ["BUILD_RESUMING_AFTER_DELOAD_TENTATIVE"]))

    def test_reduced_execution_preserves_both_recovery_branches(self):
        for hard_avg in (0, 1.7):
            s1, s2 = self.streams(ratio=0.5, hard_avg=hard_avg, hard_done=0)
            self.assertEqual(self.classify(s1, s2, "Base")[:2], ("Recovery", "medium"))

    def test_unrounded_contradiction_boundary_also_applies_before_two_dates(self):
        for elapsed in (1, 2, 5):
            for ratio, expected in ((0.80, "Recovery"), (0.800001, "Build"), (1.0, "Build")):
                s1, s2 = self.streams(ratio=ratio, hard_done=1)
                aligned = s2["load_evidence"]["day_aligned"]
                aligned["elapsed_days"] = elapsed
                if elapsed == 1:
                    aligned["status"] = "insufficient_elapsed"
                self.assertEqual(self.classify(s1, s2)[0], expected)

    def test_completed_full_week_guard_survives_unavailable_day_alignment(self):
        for hard_avg in (0, 1.7):
            s1, s2 = self.streams(hard_avg=hard_avg)
            s2["load_evidence"].update(completed_tss_delta=1.1,
                day_aligned={"status": "unavailable", "tss_delta": None})
            self.assertEqual(self.classify(s1, s2),
                             ("Build", "low", ["BUILD_RESUMING_AFTER_DELOAD_TENTATIVE"]))
            self.assertNotEqual(self.classify(s1, s2, "Base")[0], "Recovery")

    def test_missing_execution_does_not_establish_resumption(self):
        s1, s2 = self.streams()
        s2["load_evidence"]["day_aligned"] = {"status": "unavailable", "tss_delta": None}
        self.assertEqual(self.classify(s1, s2)[0], "Recovery")

    def test_executed_resumption_requires_a_completed_hard_day(self):
        s1, s2 = self.streams(hard_done=0)
        result = self.classify(s1, s2)
        self.assertNotIn(result[0], ("Recovery", "Deload"))
        self.assertNotIn("BUILD_RESUMING_AFTER_DELOAD_TENTATIVE", result[2])

    def test_backfill_without_live_evidence_keeps_legacy_recovery(self):
        s1, s2 = self.streams(hard_avg=0)
        s2["load_evidence"].pop("day_aligned")
        s2["load_evidence"]["completed_tss_delta"] = 1.1
        self.assertEqual(self.classify(s1, s2, "Base")[:2], ("Recovery", "medium"))

    def test_live_retrospective_fallback_cannot_ignore_current_contradiction(self):
        s1, s2 = self.streams()
        s1.update(ctl_slope=1.5, hard_day_avg=3, tss_values=[700, 700, 700, 400], hard_day_values=[3]*4)
        s2.update(next_7d_sessions=0, planned_tss_delta=None)
        self.assertEqual(self.classify(s1, s2, "Build"),
                         (None, "low", ["DELOAD_CONTRADICTED_BY_COMPLETED_LOAD"]))

    def test_real_higher_priority_evidence_retains_precedence(self):
        s1, s2 = self.streams()
        for phase in ("Overreached", "Taper", "Peak"):
            a, b = copy.deepcopy(s1), copy.deepcopy(s2)
            if phase == "Overreached":
                a["suggested_phase"] = phase
            else:
                b.update(race_category="RACE_A", race_proximity=10 if phase == "Taper" else 20)
                if phase == "Peak":
                    a.update(ctl_values=[80, 81, 82], ctl_slope=0.1)
                    b["planned_tss_delta"] = 1.0
            self.assertEqual(self.classify(a, b)[0], phase)


if __name__ == '__main__':
    unittest.main()
