"""Synthetic fixed-scope load and day-level zone coverage controls, offline."""
import copy
import json
import statistics
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from _harness import SYNC_PATH, install_verb_guard, load_module_by_path, restore_verb_guard
from test_report_intensity import activity, collect
from test_phase_scope import Clock, plan, rows

mod = load_module_by_path("s11_main_load", SYNC_PATH)
originals = None


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


def block(current=100, sport="Ride", start="2026-10-18"):
    anchor = datetime.fromisoformat(start)
    return [activity(f"w{w}d{d}", sport if w == 0 else "Ride",
                     (anchor - timedelta(weeks=w) + timedelta(days=d)).strftime("%Y-%m-%d"),
                     current if w == 0 else 100)
            for w in range(4) for d in range(7)]


class MainSportLoad(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sync = mod.IntervalsSync("synthetic", "synthetic", week_start_day=6)
        self.sync.data_dir = Path(tmp.name)
        self.sync._intervals_data = {}
        clock = mock.patch.object(mod, "datetime", Clock)
        clock.start()
        self.addCleanup(clock.stop)

    def mono(self, acts, **kw):
        return self.sync._main_monotony(acts, "2026-10-17", "2026-10-23", **kw)

    def detect(self, acts, plans=None, weeks=None, **kw):
        return self.sync._detect_phase_v2(rows() if weeks is None else weeks, plans, {},
            today="2026-10-23", primary_sport="cycling", load_activities=acts, **kw)

    def basis(self, acts, **kw):
        return self.detect(acts, **kw)["basis"]["main_sport_load"]

    def test_combines_days_before_sample_sd_including_rest(self):
        acts = [activity("bike", date="2026-10-23", tss=50),
                activity("run", "Run", "2026-10-23", 50),
                activity("swim", "Swim", "2026-10-21", 80)]
        result = self.mono(acts)
        self.assertEqual(result["sports"], ["cycling", "running", "swimming"])
        self.assertEqual(result["value"], round(statistics.mean([0, 0, 0, 0, 80, 0, 100]) /
                                               statistics.stdev([0, 0, 0, 0, 80, 0, 100]), 2))
        self.assertEqual((result["known_tss"], result["active_days"]), (180, 2))
        for n in (1, 2):
            rides = [activity(str(i), date=f"2026-10-{23-i}", tss=80) for i in range(n)]
            expected = [80] * n + [0] * (7-n)
            self.assertEqual(self.mono(rides)["value"], round(statistics.mean(expected) / statistics.stdev(expected), 2))

    def test_empty_zero_and_constant_are_distinct(self):
        self.assertEqual(self.mono([])["status"], "no_main_sports")
        self.assertEqual(self.mono(None)["status"], "unavailable")
        self.assertEqual(self.mono([activity("z", date="2026-10-23", tss=0)])["status"], "zero_load")
        constant = [activity(str(i), date=f"2026-10-{17+i}", tss=100) for i in range(7)]
        self.assertEqual((self.mono(constant)["status"], self.mono(constant)["value"]), ("zero_variance", None))

    def test_excluded_sports_even_when_selected_or_overridden(self):
        self.sync.report_main_sports = ["cycling", "walking", "hiking", "e_cycling"]
        acts = block()
        for sport in ("Walk", "Hike", "EBikeRide", "WeightTraining"):
            acts.append(activity(sport, sport, "2026-10-22", 99999))
        self.assertEqual(self.mono(acts)["sports"], ["cycling"])
        result = self.basis(acts)
        self.assertEqual(result["sports"], ["cycling"])
        self.assertEqual([r["tss"] for r in result["weeks"]], [700, 700, 700, 600])
        report = self.sync._report_intensity(acts, "2026-10-17", "2026-10-23")
        self.assertTrue({"walking", "hiking", "e_cycling"}.isdisjoint(report["selected_sports"]))

    def test_weekend_union_retains_small_ski_block_in_every_week(self):
        acts = block(current=25)
        for act in acts:
            if act["id"] in ("w0d4", "w0d5"):
                act.update(type="NordicSki", icu_training_load=50, moving_time=60)
        result = self.basis(acts)
        self.assertNotIn("nordic_skiing", result["selections"][0]["sports"])
        self.assertIn("nordic_skiing", result["sports"])
        self.assertEqual(result["weeks"][-1]["tss"], 200)
        # Equal-load replacement cannot create a drop by changing modality.
        holiday = self.basis(block(sport="VirtualSki"))
        aligned = holiday["load_evidence"]["day_aligned"]
        self.assertEqual((aligned["completed_tss"], aligned["reference_tss_mean"], aligned["tss_delta"]), (600, 600, 1))
        self.assertEqual(holiday["sports"], ["cycling", "nordic_skiing"])

    def test_selection_partial_invalid_time_selected_load_and_no_fallback(self):
        for bad in (None, True, -1, float("nan"), float("inf"), "12"):
            acts = block()
            acts[0]["icu_training_load"] = bad
            with self.subTest(bad=bad):
                self.assertIsNone(self.mono(acts)["value"])
                result = self.basis(acts, plans=[plan(tss=10, id=i) for i in range(3)])
                self.assertEqual(result["status"], "partial")
                self.assertIsNone(result["planned_tss_delta"])
                self.assertIsNone(result["load_evidence"]["day_aligned"]["tss_delta"])
                self.assertNotEqual(self.detect(acts, [plan()])["phase"], "Deload")
        partial = [activity("bike", date="2026-10-23", tss=None, moving_time=5000),
                   activity("run", "Run", "2026-10-23", 50, moving_time=1000)]
        self.assertEqual(self.mono(partial)["sports"], ["cycling"])
        self.assertEqual(self.mono(partial)["status"], "incomplete_selection")
        absent = self.basis(None, plans=[plan()])
        self.assertEqual(absent["status"], "unavailable")
        self.assertIsNone(absent["load_evidence"]["reference_week_tss"])

    def test_unknown_dates_duplicates_and_future_conflicts(self):
        acts = block()
        baseline = self.basis(acts)
        self.assertEqual(baseline, self.basis(acts + [copy.deepcopy(acts[0])]))
        conflict = {**acts[0], "icu_training_load": 9}
        self.assertEqual(self.basis(acts + [conflict])["status"], "partial")
        future = {**conflict, "start_date_local": "2026-10-24T10:00:00"}
        self.assertEqual(baseline, self.basis(acts + [future]))
        invalid = {**conflict, "id": "invalid", "start_date_local": "bad-date"}
        self.assertEqual(self.basis(acts + [invalid])["status"], "partial")

    def test_fixed_plan_scope_missing_known_zero_and_name_support(self):
        acts = block(current=40, sport="VirtualSki")
        plans = [plan(tss=10, name="Deload", sport_type="NordicSki"),
                 plan("2026-10-25", 560, id=2, sport_type="VirtualSki"),
                 plan("2026-10-25", 9000, id=3, sport_type="EBikeRide")]
        result = self.detect(acts, plans)
        basis = result["basis"]["main_sport_load"]
        self.assertEqual((result["phase"], result["confidence"]), ("Deload", "high"))
        self.assertEqual(basis["next_week_tss_delta"], .8)
        self.assertEqual(basis["load_evidence"]["next_week"]["excluded_sports"], ["e_cycling"])
        self.assertIn("DELOAD_NAME_SUPPORT", result["reason_codes"])
        for missing in (None, -1, True):
            changed = copy.deepcopy(plans)
            changed[1]["planned_tss"] = missing
            b = self.basis(acts, plans=changed)
            self.assertIsNone(b["next_week_tss_delta"])
            self.assertEqual(b["load_evidence"]["next_week"]["missing_tss"], 1)
        changed = copy.deepcopy(plans)
        changed[1].update(planned_tss=0, sport_type="Unknown")
        self.assertIsNone(self.basis(acts, plans=changed)["next_week_tss_delta"])
        changed[1]["sport_type"] = "NordicSki"
        self.assertEqual(self.basis(acts, plans=changed)["load_evidence"]["next_week"]["missing_tss"], 0)

    def test_missing_row_and_round_once_not_sum_rounded_sports(self):
        weeks = rows()
        weeks.pop(0)
        self.assertEqual(self.basis(block(), weeks=weeks)["status"], "partial")
        acts = [activity("a", "Ride", "2026-09-27", 10.4),
                activity("b", "Run", "2026-09-27", 10.4)]
        result = self.basis(acts)
        self.assertEqual(result["weeks"][0]["tss"], 21)
        self.assertEqual(result["weeks"][0]["known_tss"], 20.8)

    def test_legacy_summary_cannot_replace_raw_decision_load(self):
        weeks = rows(completed=9999)
        result = self.detect(block(current=40), [plan()], weeks=weeks)
        self.assertEqual(result["basis"]["stream_2"]["load_evidence"]["completed_tss"], 9999)
        self.assertEqual(result["basis"]["main_sport_load"]["load_evidence"]["completed_tss"], 240)
        self.assertEqual((result["phase"], result["confidence"]), ("Deload", "medium"))
        self.assertEqual(result["basis"]["decision_load_source"], "main_sport_load")

    def test_unsupported_selection_and_date_gaps_do_not_fall_back(self):
        for report in ({}, {"schema_version": 999}):
            result = self.mono([a for a in block() if a["start_date_local"][:10] <= "2026-10-23"], report=report)
            self.assertEqual(result["selection_status"], "unavailable")
            self.assertIsNone(result["value"])
        weeks = rows()
        for row in weeks:
            row["_phase_load_compatible"] = True
            row["phase_load_basis"] = {"rule": self.sync.PHASE_LOAD_RULE, "status": "complete",
                "observation_cutoff": (datetime.fromisoformat(row["week_start"]) + timedelta(days=6)).strftime("%Y-%m-%d")}
        result = self.detect(block(), [plan(name="Threshold")], weeks=weeks, previous_phase="Build")
        self.assertEqual(result["phase_duration_weeks"], 4)
        weeks[-3]["phase_load_basis"]["observation_cutoff"] = "2026-10-01"
        result = self.detect(block(), [plan(name="Threshold")], weeks=weeks, previous_phase="Build")
        self.assertEqual(result["phase_duration_weeks"], 2)

    def test_target_local_history_path_c_and_future_sport_independence(self):
        weeks = rows()
        acts = block(current=80, sport="VirtualSki")
        result = self.sync._detect_phase_v2(weeks, [], {}, today="2026-10-18",
            is_backfill=True, load_activities=acts)
        self.assertEqual(result["phase"], "Deload")
        basis = result["basis"]["main_sport_load"]
        self.assertEqual(basis["weeks"][-1]["tss"], 560)
        future = activity("future", "Run", "2026-10-25", 99999)
        again = self.sync._detect_phase_v2(rows(), [], {}, today="2026-10-18",
            is_backfill=True, load_activities=acts + [future])
        self.assertEqual(basis, again["basis"]["main_sport_load"])
        self.assertNotIn("running", basis["sports"])
        # A switch at unchanged load is not a retrospective Deload.
        steady = self.sync._detect_phase_v2(rows(), [], {}, today="2026-10-18",
            is_backfill=True, load_activities=block(sport="VirtualSki"))
        self.assertNotEqual(steady["phase"], "Deload")

    def test_compact_history_retains_target_selection_and_load_missingness(self):
        acts = block(sport="NordicSki")
        acts[0]["icu_training_load"] = None
        rich = self.basis(acts)
        compact = self.sync._pack_phase_load(rich)
        self.assertEqual(compact["sports"], ["cycling", "nordic_skiing"])
        self.assertEqual(compact["observation_cutoff"], "2026-10-23")
        self.assertEqual(compact["weeks"][-1], ["2026-10-18", 500.0, 1, None, "partial"])
        self.assertEqual(compact["selections"][-1], [["nordic_skiing"], "complete"])
        self.assertEqual(compact["status"], "partial")
        self.assertEqual(self.sync._phase_load_schema()["week"], "week_start,known_tss,missing_tss,tss,status")
        self.assertFalse(any("name" in key for key in compact))

    def test_unmarked_history_does_not_supply_transition_or_duration(self):
        p = self.detect(block(), [plan(name="Threshold")], previous_phase="Deload")
        self.assertIsNone(p["previous_phase"])
        self.assertIsNone(p["phase_duration_weeks"])
        history = {"weekly_180d": rows(), "phase_load_schema": self.sync._phase_load_schema(),
                   "report_intensity_schema": self.sync._report_intensity_schema()}
        path = self.sync.data_dir / self.sync.HISTORY_FILE
        path.write_text(json.dumps(history))
        self.assertTrue(all(r["_phase_load_compatible"] for r in self.sync._load_weekly_rows_for_phase()))
        history["phase_load_schema"]["week_start_day"] = 0
        path.write_text(json.dumps(history))
        self.assertFalse(any(r["_phase_load_compatible"] for r in self.sync._load_weekly_rows_for_phase()))
        weeks = rows()
        weeks[-2]["phase_load_basis"] = {"rule": self.sync.PHASE_LOAD_RULE, "status": "complete",
                                        "observation_cutoff": "2026-10-17"}
        weeks[-2]["_phase_load_compatible"] = True
        p = self.detect(block(), [plan(name="Threshold")], weeks=weeks, previous_phase="Deload")
        self.assertEqual(p["previous_phase"], "Deload")

    def test_monotony_alert_uses_main_basis_and_same_chronic_sports(self):
        acts = [activity(str(i), date=f"2026-10-{17+i}", tss=100 if i < 6 else 50) for i in range(7)]
        for i in range(21):
            date = (datetime(2026, 10, 16) - timedelta(days=i)).strftime("%Y-%m-%d")
            acts.append(activity(f"prior{i}", date=date, tss=200))
        result = self.mono(acts, chronic=True)
        self.assertEqual(result["sports"], ["cycling"])
        self.assertIsNotNone(result["deload_context"])
        dm = {"monotony": 99, "effective_monotony": 99, "monotony_main_sports": result}
        alert = next(a for a in self.sync._generate_alerts(dm, [], 99999, 99999) if a["metric"] == "monotony")
        self.assertEqual((alert["value"], alert["severity"], alert["basis"], alert["sports"]),
                         (result["value"], "info", "main_sports", ["cycling"]))
        acts[-1]["icu_training_load"] = None
        self.assertIsNone(self.mono(acts, chronic=True)["deload_context"])
        dm.pop("monotony_main_sports")
        self.assertFalse(any(a["metric"] == "monotony" for a in self.sync._generate_alerts(dm, [], 1, 1)))

    def test_weekly_monotony_gates_and_coverage_counts(self):
        acts = [activity("hard", date="2026-10-18", icu_zone_times=[{"id": "Z4", "secs": 600}]),
                activity("easy", date="2026-10-19"),
                activity("nozones", date="2026-10-20", icu_zone_times=[])]
        expected = {"days_with_activity": 3, "days_without_zone_data": 1, "status": "partial"}
        self.assertEqual(self.sync._hard_days_coverage(acts), expected)
        self.assertEqual(self.sync._hard_days_coverage(acts[:2])["status"], "complete")
        self.assertEqual(self.sync._hard_days_coverage(acts[2:])["status"], "unknown")
        self.assertEqual(self.sync._hard_days_coverage([])["status"], "no_activities")
        groups = defaultdict(list)
        for act in acts:
            groups[act["start_date_local"][:10]].append(act)
        week = self.sync._build_weekly_tier(groups, {}, 7)[-1]
        self.assertEqual((week["hard_days"], week["hard_days_coverage"]), (1, expected))
        self.assertEqual(week["monotony_main_sports"]["value"], week["monotony"])
        dm = self.sync._calculate_derived_metrics(acts, acts, [], [], 80, 85, -5,
            [], acts, {}, (None, None, None), (None, None, None), None, formatted_planned_workouts=[])
        self.assertEqual((dm["hard_days_this_week"], dm["hard_days_coverage"]), (1, expected))
        chunks = self.sync._rolling_7d_chunks(acts, [], [], "2026-10-23", 80)
        self.assertEqual((chunks[0]["hard_days"], chunks[0]["hard_days_coverage"]), (1, expected))
        partial = self.sync._main_monotony(acts, "2026-10-18", "2026-10-20", weekly=True)
        self.assertEqual(partial["status"], "insufficient_days")

    def test_power_hr_preference_and_same_day_coverage_do_not_change_ladders(self):
        nozone = activity("n", date="2026-10-23", icu_zone_times=[])
        hr = activity("h", "Run", "2026-10-23", icu_zone_times=[], icu_hr_zone_times=[0, 0, 0, 600, 0])
        self.assertEqual(self.sync._hard_days_coverage([nozone, hr])["status"], "complete")
        self.assertTrue(self.sync._hard_day_evidence([nozone, hr], {}))
        dual = activity("both", date="2026-10-23", icu_zone_times=[{"id": "Z2", "secs": 3600}],
                        icu_hr_zone_times=[0, 0, 0, 600, 0])
        self.assertIsNone(self.sync._hard_day_evidence([dual], {}))
        self.sync.zone_preference = {"cycling": "hr"}
        self.assertTrue(self.sync._hard_day_evidence([dual], {}))


if __name__ == "__main__":
    unittest.main()
