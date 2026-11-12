"""Offline phase scope/uncertainty controls. All fixtures here are synthetic."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from _harness import SYNC_PATH, install_verb_guard, load_module_by_path, restore_verb_guard

mod = load_module_by_path("s11_phase_scope", SYNC_PATH)
originals = None


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 23, 12, tzinfo=tz)


def plan(date="2026-10-23", tss=20, **changes):
    result = {"id": 101, "date": date, "type": "WORKOUT", "sport_type": "Ride",
              "name": "Easy ride", "planned_tss": tss, "workout_summary": None}
    result.update(changes)
    return result


def rows(start="2026-10-18", completed=800):
    ws = datetime.strptime(start, "%Y-%m-%d")
    return [{"week_start": (ws - timedelta(weeks=3-i)).strftime("%Y-%m-%d"),
             "total_tss": 700 if i < 3 else completed,
             "primary_sport_tss": 650 if i < 3 else completed,
             "primary_sport": "cycling", "hard_days": 3, "ctl_end": 80 + i*3,
             "activity_count": 10, "acwr": 1.0, "monotony": 1.0,
             "phase_detected": "Build"} for i in range(4)]


def load_activities(weeks, backfill=False):
    """Raw complete load evidence; legacy summary rows cannot substitute for it."""
    result = []
    for i, row in enumerate(weeks):
        days = 7 if backfill or i < len(weeks)-1 else 6
        for day in range(days):
            date = datetime.fromisoformat(row["week_start"]) + timedelta(days=day)
            result.append({"id": f"load-{i}-{day}", "type": "Ride", "moving_time": 3600,
                "start_date_local": date.strftime("%Y-%m-%dT10:00:00"),
                "icu_training_load": row["total_tss"] * (0.5 if day == days-1 else 1) / (days-0.5)})
    return result


class PhaseScope(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sync = mod.IntervalsSync("synthetic", "synthetic", week_start_day=6)
        self.sync.data_dir = Path(tmp.name)
        self.sync._intervals_data = {}
        patch = mock.patch.object(mod, "datetime", Clock)
        patch.start()
        self.addCleanup(patch.stop)

    def detect(self, plans, weeks=None, today="2026-10-23", **kw):
        return self.sync._detect_phase_v2(weeks or rows(), plans, {}, today=today,
                                          primary_sport="cycling", **kw)

    def stream(self, plans, weeks=None, today="2026-10-23", **kw):
        return self.detect(plans, weeks=weeks, today=today, **kw)["basis"]["stream_2"]

    def derive(self, acts, plans, past=()):
        return self.sync._calculate_derived_metrics(acts, acts, [], [], 80, 85, -5,
            list(past), acts, {}, (None, None, None), (None, None, None), None,
            formatted_planned_workouts=plans)

    def test_completed_facts_survive_empty_marker_and_next_only_calendars(self):
        for plans in ([], [plan(type="HOLIDAY")], [plan("2026-10-25")], None):
            with self.subTest(plans=plans):
                s = self.stream(plans)
                self.assertEqual(s["current_week_hard_days_completed"], 3)
                self.assertEqual(s["current_week_hard_days_total"], 3)
                self.assertEqual(s["hard_sessions_planned"], 0)
                self.assertEqual(s["plan_coverage_current_week"], 0)
                self.assertEqual(s["load_evidence"]["completed_tss"], 800)

    def test_sparse_and_three_entry_remainders_cannot_override_completed_load(self):
        for plans in ([plan(tss=250)], [plan(tss=10, id=i) for i in range(3)]):
            for rebound in ([], [plan("2026-10-25", 800, name="Threshold")]):
                with self.subTest(plans=plans, rebound=rebound):
                    p = self.detect(plans + rebound, load_activities=load_activities(rows()))
                    self.assertEqual((p["phase"], p["confidence"]), (None, "low"))
                    self.assertEqual(p["reason_codes"], ["DELOAD_CONTRADICTED_BY_COMPLETED_LOAD"])

    def test_complete_raw_reduced_execution_establishes_live_reduction(self):
        for completed in (0, 200, 450):
            for hard_days in (0, 2):
                week = rows(completed=completed)
                week[-1]["hard_days"] = hard_days
                p = self.detect([plan(tss=10, id=i) for i in range(3)], weeks=week,
                                load_activities=load_activities(week))
                self.assertEqual((p["phase"], p["confidence"]), ("Deload", "medium"))
                self.assertIn("BUILD_HISTORY_DAY_ALIGNED_REDUCTION", p["reason_codes"])

    def test_unknown_zero_empty_and_unavailable_load_are_distinct(self):
        unknown = self.stream([plan(tss=None, id=i) for i in range(20)])["load_evidence"]
        self.assertEqual(unknown["remaining"]["missing_tss"], 20)
        self.assertEqual(unknown["remaining"]["unknown_intensity"], 20)
        duration_only = self.stream([plan(workout_summary="1h")])["load_evidence"]
        self.assertEqual(duration_only["remaining"]["unknown_intensity"], 1)
        zero = self.stream([plan(tss=0)])["load_evidence"]
        self.assertEqual(zero["remaining"]["missing_tss"], 0)
        self.assertEqual(zero["remaining"]["known_tss"], 0)
        self.assertEqual(zero["remaining"]["sessions"], 1)
        self.assertEqual(self.stream([])["load_evidence"]["calendar_status"], "available")
        self.assertEqual(self.stream(None)["load_evidence"]["calendar_status"], "unavailable")
        self.assertEqual(self.derive([], None)["phase_detection"]["basis"]["stream_2"]
                         ["load_evidence"]["calendar_status"], "unavailable")

    def test_next_week_mutation_cannot_certify_current_reduction_or_hard_days(self):
        base = self.detect([plan()], weeks=rows(completed=200))
        heavy = self.detect([plan(), plan("2026-10-25", 900, name="Threshold")],
                            weeks=rows(completed=200))
        self.assertEqual((base["phase"], base["confidence"]), (heavy["phase"], heavy["confidence"]))
        before, after = base["basis"]["stream_2"], heavy["basis"]["stream_2"]
        for key in ("planned_tss_delta", "hard_sessions_planned", "current_week_hard_days_total"):
            self.assertEqual(before[key], after[key])
        self.assertIsNone(before["next_week_load"])
        self.assertGreater(after["next_week_load"], 1)
        self.assertEqual(after["load_evidence"]["next_week_rebound"], "unverified")

    def test_all_seven_configured_rollovers_partition_sunday_and_monday(self):
        for ws in range(7):
            self.sync.week_start_day = ws
            start = datetime(2026, 10, 19) + timedelta(days=ws)
            end = start + timedelta(days=6)
            following = end + timedelta(days=1)
            plans = [plan(end.strftime("%Y-%m-%d"), 25),
                     plan(following.strftime("%Y-%m-%d"), 125, id=102, name="Threshold")]
            s = self.stream(plans, weeks=rows(start.strftime("%Y-%m-%d"), 200),
                            today=end.strftime("%Y-%m-%d"))
            self.assertEqual(s["hard_sessions_planned"], 0)
            self.assertEqual(s["load_evidence"]["remaining"]["known_tss"], 25)
            self.assertEqual(s["load_evidence"]["next_week"]["known_tss"], 125)
            s = self.stream(plans, weeks=rows(following.strftime("%Y-%m-%d"), 0),
                            today=following.strftime("%Y-%m-%d"))
            self.assertEqual(s["hard_sessions_planned"], 1)
            self.assertEqual(s["load_evidence"]["remaining"]["known_tss"], 125)

    def test_future_actuals_do_not_enter_current_execution(self):
        def act(aid, date, tss):
            return {"id": aid, "start_date_local": date + "T10:00:00", "type": "Ride",
                    "moving_time": 3600, "icu_training_load": tss,
                    "icu_zone_times": [{"id": "Z4", "secs": 700}]}
        p = self.derive([act("a1", "2026-10-22", 50), act("a2", "2026-10-24", 500)], [])
        s = p["phase_detection"]["basis"]["stream_2"]
        self.assertEqual(s["current_week_hard_days_completed"], 1)
        self.assertEqual(s["load_evidence"]["completed_tss"], 50)

    def test_exact_same_day_pair_is_excluded_even_after_plan_edit(self):
        act = {"id": "a1", "paired_event_id": 101, "type": "Ride", "moving_time": 3600,
               "start_date_local": "2026-10-23T10:00:00", "icu_training_load": 50,
               "icu_zone_times": [{"id": "Z4", "secs": 700}]}
        for tss in (50, 999):
            p = self.derive([act], [plan(tss=tss, name="Threshold")])
            s = p["phase_detection"]["basis"]["stream_2"]
            self.assertEqual(s["current_week_hard_days_total"], 1)
            self.assertEqual(s["hard_sessions_planned"], 0)
            self.assertEqual(s["load_evidence"]["remaining"]["known_tss"], 0)
            self.assertEqual(s["load_evidence"]["completed_tss"], 50)
            self.assertEqual(s["load_evidence"]["completed_events_excluded"], 1)
        del act["paired_event_id"]
        p = self.derive([act], [plan(name="Threshold")], [{"id": 101, "paired_activity_id": "a1"}])
        self.assertEqual(p["phase_detection"]["basis"]["stream_2"]["hard_sessions_planned"], 0)

    def test_absent_pairing_stays_uncertain_and_never_creates_combined_tss(self):
        s = self.stream([plan(name="Threshold")])
        e = s["load_evidence"]
        self.assertEqual(e["remaining"]["same_day_unresolved"], 1)
        self.assertEqual(e["completed_events_excluded"], 0)
        self.assertNotIn("full_week_tss", e)

    def test_unmarked_previous_phase_cannot_supply_transition(self):
        p = self.detect([plan(tss=None, name="Threshold", id=i) for i in range(2)],
                        previous_phase="Deload")
        self.assertEqual((p["phase"], p["confidence"]), ("Build", "low"))
        self.assertEqual(p["reason_codes"], ["MAIN_SPORT_LOAD_UNAVAILABLE"])
        self.assertIsNone(p["previous_phase"])

    def test_retrospective_threshold_and_retained_quality_are_unchanged(self):
        for completed, expected in ((560, "Deload"), (561, "Build")):
            week = rows(completed=completed)
            week[-1]["hard_days"] = 2
            p = self.detect([], weeks=week, today="2026-10-18", is_backfill=True,
                            load_activities=load_activities(week, backfill=True))
            self.assertEqual(p["phase"], expected)
            if expected == "Deload":
                self.assertEqual(p["confidence"], "medium")

    def test_supported_build_base_and_race_overreached_priorities(self):
        self.assertEqual(self.detect([plan(name="Threshold")])["phase"], "Build")
        base = rows()
        for row in base:
            row.update(ctl_end=80, hard_days=0)
        self.assertEqual(self.detect([], weeks=base)["phase"], "Base")
        for days, expected in ((7, "Taper"), (18, "Peak")):
            p = self.sync._detect_phase_v2(rows(), [], {"next_race": {"days_until": days,
                "category": "RACE_A"}}, today="2026-10-23")
            self.assertEqual((p["phase"], p["confidence"]), (expected, "medium"))
        week = rows()
        week[-2].update(monotony=3.0, acwr=1.6)
        p = self.detect([], weeks=week, load_activities=load_activities(week))
        self.assertEqual((p["phase"], p["confidence"]), ("Overreached", "high"))

    def test_provenance_live_backfill_duration_and_no_accumulation(self):
        week = rows()
        p = self.detect([plan(name="Threshold")], weeks=week)
        self.assertIsNone(p["phase_duration_weeks"])  # unmarked legacy history
        self.assertEqual(p["provenance"], {
            "as_of_date": "2026-10-23", "week_start": "2026-10-18", "week_end": "2026-10-24",
            "mode": "live", "completed_weeks_used": ["2026-09-27", "2026-10-04", "2026-10-11"],
            "observation_cutoff": "2026-10-23", "availability": "current_snapshot_not_historical_knowledge"})
        saved = json.dumps(p, sort_keys=True)
        for _ in range(20):
            self.assertEqual(json.dumps(self.detect([plan(name="Threshold")], weeks=week), sort_keys=True), saved)
        retro = self.detect([], weeks=week, today="2026-10-18", is_backfill=True)["provenance"]
        self.assertEqual(retro["mode"], "retrospective")
        self.assertEqual(retro["as_of_date"], "2026-10-25")
        self.assertEqual(retro["observation_cutoff"], "2026-10-24")
        self.assertEqual(len(retro["completed_weeks_used"]), 4)

    def test_matched_sport_scope_never_mixes_completed_and_reference(self):
        week = rows(completed=200)
        week[-1]["primary_sport"] = "running"
        e = self.stream([plan()], weeks=week)["load_evidence"]
        self.assertEqual(e["sport_scope"], "cycling")
        self.assertIsNone(e["completed_tss"])
        self.assertIsNone(e["completed_tss_delta"])


if __name__ == "__main__":
    unittest.main()
