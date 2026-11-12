"""Synthetic live day-alignment, rebound and name-support regressions."""
import copy
import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

import test_phase_scope as scope
from test_phase_scope import plan, rows


def activities(start="2026-10-18", current=40):
    start = datetime.fromisoformat(start)
    result = []
    for week in range(4):
        for day in range(7):
            date = start - timedelta(weeks=week) + timedelta(days=day)
            result.append({"id": f"synthetic-{week}-{day}", "type": "Ride", "name": "Easy ride",
                           "start_date_local": date.strftime("%Y-%m-%dT10:00:00"),
                           "moving_time": 3600, "icu_training_load": current if week == 0 else 100})
    return result


class PhaseAlignment(unittest.TestCase):
    setUp = scope.PhaseScope.setUp
    derive = scope.PhaseScope.derive

    def detect(self, plans=None, today="2026-10-19", acts=None, weeks=None, **kwargs):
        week = rows(completed=80) if weeks is None else weeks
        for row in week[:-1]:
            row["primary_sport_tss"] = 700
        return self.sync._detect_phase_v2(week, plans, {}, today=today, primary_sport="cycling",
            completed_activities=activities() if acts is None else acts, **kwargs)

    def evidence(self, **kwargs):
        return self.detect(**kwargs)["basis"]["stream_2"]["load_evidence"]["day_aligned"]

    def test_two_dates_gate_and_names_cannot_enable_rebound_high_on_first_date(self):
        plans = [plan("2026-10-18", 10, name="Deload"), plan("2026-10-25", 560)]
        first = self.detect(plans, today="2026-10-18")
        self.assertEqual((first["phase"], first["confidence"]), ("Deload", "medium"))
        self.assertEqual(first["reason_codes"], ["DELOAD_CURRENT_REDUCTION_UNVERIFIED", "DELOAD_NAME_SUPPORT"])
        self.assertEqual(first["basis"]["stream_2"]["load_evidence"]["day_aligned"]["status"], "insufficient_elapsed")
        second = self.detect(plans)
        self.assertEqual((second["phase"], second["confidence"]), ("Deload", "high"))
        self.assertIn("BUILD_HISTORY_REDUCED_LOAD_REBOUND_CONFIRMED", second["reason_codes"])

    def test_name_support_promotes_sparse_and_reduced_remainder_candidates(self):
        for count in (1, 3):
            for source in ("planned", "completed"):
                for today, missing in (("2026-10-18", False), ("2026-10-19", True)):
                    with self.subTest(count=count, source=source, today=today):
                        values = activities()
                        plans = [plan("2026-10-20", 10, id=i) for i in range(count)]
                        if missing:
                            values[0]["icu_training_load"] = None
                        if source == "planned":
                            plans[0]["name"] = "DELOAD endurance"
                        else:
                            values[0]["name"] = "Deload endurance"
                        p = self.detect(plans, today=today, acts=values)
                        if missing:
                            self.assertEqual((p["phase"], p["confidence"]), ("Build", "low"))
                            self.assertIn("MAIN_SPORT_LOAD_UNAVAILABLE", p["reason_codes"])
                        else:
                            self.assertEqual((p["phase"], p["confidence"]), ("Deload", "medium"))
                            self.assertEqual(p["reason_codes"], ["DELOAD_CURRENT_REDUCTION_UNVERIFIED", "DELOAD_NAME_SUPPORT"])
                        aligned = p["basis"]["stream_2"]["load_evidence"]["day_aligned"]
                        self.assertEqual(aligned["status"], "unavailable" if missing else "insufficient_elapsed")

    def test_unsupported_remainder_candidates_stay_low_even_with_rebound(self):
        for count in (1, 3):
            plans = [plan("2026-10-20", 10, id=i) for i in range(count)] + [plan("2026-10-25", 900)]
            for today, missing in (("2026-10-18", False), ("2026-10-19", True)):
                values = activities()
                if missing:
                    values[0]["icu_training_load"] = None
                p = self.detect(plans, today=today, acts=values)
                self.assertEqual((p["phase"], p["confidence"]), ("Build" if missing else "Deload", "low"))
                self.assertIn("MAIN_SPORT_LOAD_UNAVAILABLE" if missing else "DELOAD_CURRENT_REDUCTION_UNVERIFIED", p["reason_codes"])

    def test_name_promotion_cannot_override_either_completed_load_guard(self):
        for count in (1, 3):
            plans = [plan("2026-10-20", 10, id=i, name="Deload") for i in range(count)]
            for current, completed, expected in ((80, 80, "Deload"), (80.01, 80, None), (561, 561, None)):
                p = self.detect(plans, today="2026-10-18", acts=activities(current=current),
                                weeks=rows(completed=completed))
                self.assertEqual((p["phase"], p["confidence"]), (expected, "medium" if expected else "low"))
                self.assertEqual(p["basis"]["stream_2"]["load_evidence"]["day_aligned"]["status"], "insufficient_elapsed")
                if expected is None:
                    self.assertEqual(p["reason_codes"], ["DELOAD_CONTRADICTED_BY_COMPLETED_LOAD"])

    def test_name_promotion_requires_whole_word_matching_sport_and_current_week(self):
        for changes in ({"name": "deloaded"}, {"name": "Recovery"},
                        {"name": "Deload", "sport_type": "Run"},
                        {"name": "Deload", "date": "2026-10-25"},
                        {"name": "Deload", "date": "2026-10-11"}):
            event = {**plan("2026-10-20", 10), **changes}
            p = self.detect([plan("2026-10-20", 10, id=202), event], today="2026-10-18")
            self.assertEqual((p["phase"], p["confidence"]), ("Deload", "low"))
            self.assertNotIn("DELOAD_NAME_SUPPORT", p["reason_codes"])

    def test_day_alignment_alone_and_with_name_are_medium(self):
        for plans in (None, [], [plan("2026-10-20", name="DELOAD week")]):
            with self.subTest(plans=plans):
                p = self.detect(plans)
                self.assertEqual((p["phase"], p["confidence"]), ("Deload", "medium"))
                self.assertIn("BUILD_HISTORY_DAY_ALIGNED_REDUCTION", p["reason_codes"])
                self.assertEqual("DELOAD_NAME_SUPPORT" in p["reason_codes"], bool(plans))

    def test_exact_reduction_boundary_is_unrounded_and_matched_span(self):
        exact = self.detect([], acts=activities(current=80))
        above = self.detect([plan("2026-10-19", 1)], acts=activities(current=80.01))
        self.assertEqual((exact["phase"], exact["confidence"]), ("Deload", "medium"))
        self.assertEqual((above["phase"], above["confidence"]), (None, "low"))
        e = exact["basis"]["stream_2"]["load_evidence"]["day_aligned"]
        self.assertEqual((e["completed_tss"], e["reference_tss_mean"], e["tss_delta"]), (160, 200, .8))

    def test_next_week_boundary_changes_confidence_only_after_reduction(self):
        for tss, confidence in ((550, "medium"), (560, "high"), (900, "high")):
            p = self.detect([plan("2026-10-25", tss)])
            self.assertEqual((p["phase"], p["confidence"]), ("Deload", confidence))
        heavy = self.detect([plan("2026-10-19", 1, name="Deload"), plan("2026-10-25", 900)],
                            acts=activities(current=110))
        self.assertEqual((heavy["phase"], heavy["confidence"]), (None, "low"))
        self.assertEqual(heavy["reason_codes"], ["DELOAD_CONTRADICTED_BY_COMPLETED_LOAD"])

    def test_missing_load_zero_and_zero_reference_are_distinct(self):
        missing = activities()
        missing[0]["icu_training_load"] = None
        e = self.evidence(acts=missing)
        self.assertEqual((e["status"], e["missing_tss"], e["tss_delta"]), ("unavailable", 1, None))
        self.assertEqual(self.evidence(acts=activities(current=0))["status"], "reduced")
        empty_ref = activities()
        for act in empty_ref[7:]:
            act["icu_training_load"] = 0
        self.assertEqual(self.evidence(acts=empty_ref)["status"], "unavailable")
        for bad in (True, -1, float('nan')):
            values = activities()
            values[7]["icu_training_load"] = bad
            self.assertEqual(self.evidence(acts=values)["status"], "unavailable")

    def test_missing_reference_week_never_bridges_gap(self):
        week = rows(completed=80)
        week[0]["week_start"] = "2026-09-20"
        self.assertEqual(self.evidence(weeks=week)["status"], "unavailable")

    def test_future_actuals_and_other_sports_cannot_change_matching_reduction(self):
        values = activities()
        future = {**values[0], "start_date_local": "2026-10-20T10:00:00", "icu_training_load": 9999}
        other = {**values[0], "type": "Run", "icu_training_load": None}
        self.assertEqual(self.evidence(acts=values), self.evidence(acts=values + [future, other]))

    def test_all_week_starts_use_exact_three_matching_spans(self):
        for weekday in range(7):
            with self.subTest(weekday=weekday):
                self.sync.week_start_day = weekday
                start = datetime(2026, 10, 19) + timedelta(days=weekday)
                start_str = start.strftime("%Y-%m-%d")
                today = (start + timedelta(days=1)).strftime("%Y-%m-%d")
                e = self.evidence(today=today, acts=activities(start_str), weeks=rows(start_str, 80))
                self.assertEqual((e["elapsed_days"], e["completed_tss"], e["reference_tss_mean"]), (2, 80, 200))
                self.assertEqual(e["reference_weeks"], [(start - timedelta(weeks=i)).strftime("%Y-%m-%d") for i in (3, 2, 1)])

    def test_keyword_is_whole_word_and_recovery_is_not_deload_intent(self):
        for name, supported in (("Deload endurance", True), ("DELOAD", True),
                                ("deloaded", False), ("Recovery", False),
                                ("Push/Recovery Candidate", False)):
            p = self.detect([plan("2026-10-20", name=name)])
            self.assertEqual("DELOAD_NAME_SUPPORT" in p["reason_codes"], supported)
            self.assertEqual((p["phase"], p["confidence"]), ("Deload", "medium"))

    def test_names_never_create_deload_or_change_build_base_scoring(self):
        for hard, slope, current in ((3, 3, 110), (0, 0, 110), (0, 0, 40)):
            week = rows(completed=80)
            for i, row in enumerate(week):
                row.update(ctl_end=80+i*slope, hard_days=hard)
            plain = self.detect([plan("2026-10-20")], acts=activities(current=current), weeks=copy.deepcopy(week))
            named = activities(current=current)
            named[0]["name"] = "Deload"
            label = self.detect([plan("2026-10-20", name="Deload")], acts=named, weeks=copy.deepcopy(week))
            self.assertEqual((plain["phase"], plain["confidence"], plain["reason_codes"]),
                             (label["phase"], label["confidence"], label["reason_codes"]))
            self.assertNotEqual(label["phase"], "Deload")

    def test_week_phrases_support_existing_candidate_in_plans_and_completed_names(self):
        for name in ("recovery week", "REST WEEK", "Deload week", "Easy ride (Recovery week) 60min",
                     "Rest week - easy ride", "Deload week / Recovery week"):
            for source in ("planned", "completed"):
                with self.subTest(name=name, source=source):
                    values = activities()
                    plans = [plan("2026-10-20", 10)]
                    if source == "planned":
                        plans[0]["name"] = name
                    else:
                        values[0]["name"] = name
                    p = self.detect(plans, today="2026-10-18", acts=values)
                    self.assertEqual((p["phase"], p["confidence"]), ("Deload", "medium"))
                    self.assertEqual(p["reason_codes"], ["DELOAD_CURRENT_REDUCTION_UNVERIFIED", "DELOAD_NAME_SUPPORT"])
                    support = p["basis"]["stream_2"]["load_evidence"]["day_aligned"]["name_support"]
                    self.assertEqual(support["planned_events"], int(source == "planned"))
                    self.assertEqual(support["completed_activities"], int(source == "completed"))

    def test_single_recovery_and_partial_week_phrases_do_not_support(self):
        for name in ("Recovery", "Recovery 1h30m", "Push/Recovery Candidate", "recovery weekend",
                     "rest weekdays", "prerest week", "prerecovery week", "recovery weeklong",
                     "Restweek", "deloaded week"):
            for source in ("planned", "completed"):
                with self.subTest(name=name, source=source):
                    values = activities()
                    plans = [plan("2026-10-20", 10)]
                    if source == "planned":
                        plans[0]["name"] = name
                    else:
                        values[0]["name"] = name
                    p = self.detect(plans, today="2026-10-18", acts=values)
                    self.assertEqual((p["phase"], p["confidence"]), ("Deload", "low"))
                    self.assertEqual(p["reason_codes"], ["DELOAD_CURRENT_REDUCTION_UNVERIFIED"])

    def test_week_phrases_preserve_sport_week_and_observation_scope(self):
        for name in ("Recovery week", "Rest week"):
            plans = [plan("2026-10-20", 10)]
            for changes in ({"date": "2026-10-11"}, {"date": "2026-10-25"}, {"sport_type": "Run"}):
                event = {**plan("2026-10-20", 10, id=202, name=name), **changes}
                p = self.detect(plans + [event], today="2026-10-18")
                self.assertEqual((p["phase"], p["confidence"]), ("Deload", "low"))
            for changes in ({"start_date_local": "2026-10-11T10:00:00"},
                            {"start_date_local": "2026-10-19T10:00:00"}, {"type": "Walk"}):
                values = activities()
                values.append({**values[0], "id": "other", "name": name, **changes})
                p = self.detect(plans, today="2026-10-18", acts=values)
                self.assertEqual((p["phase"], p["confidence"]), ("Deload", "low"))

    def test_week_phrases_cannot_create_candidates_override_guards_or_enable_high(self):
        for name in ("Recovery week", "Rest week", "Deload week"):
            for count in (1, 3):
                plans = [plan("2026-10-20", 10, id=i, name=name) for i in range(count)] + [plan("2026-10-25", 900)]
                for current, completed in ((80.01, 80), (561, 561)):
                    p = self.detect(plans, today="2026-10-18", acts=activities(current=current),
                                    weeks=rows(completed=completed))
                    self.assertEqual((p["phase"], p["confidence"]), (None, "low"))
                    self.assertEqual(p["reason_codes"], ["DELOAD_CONTRADICTED_BY_COMPLETED_LOAD"])
                first = self.detect(plans, today="2026-10-18")
                self.assertEqual((first["phase"], first["confidence"]), ("Deload", "medium"))
            for hard, slope, current in ((3, 3, 110), (0, 0, 40)):
                week = rows(completed=80)
                for i, row in enumerate(week):
                    row.update(ctl_end=80+i*slope, hard_days=hard)
                plain = self.detect([plan("2026-10-20")], acts=activities(current=current), weeks=copy.deepcopy(week))
                named = activities(current=current)
                named[0]["name"] = name
                label = self.detect([plan("2026-10-20", name=name)], acts=named, weeks=copy.deepcopy(week))
                self.assertEqual((plain["phase"], plain["confidence"], plain["reason_codes"]),
                                 (label["phase"], label["confidence"], label["reason_codes"]))
                self.assertNotEqual(label["phase"], "Deload")

    def test_past_current_names_are_counted_once_next_week_and_future_actual_names_excluded(self):
        acts = activities()
        acts[0]["name"] = "Deload"
        acts[2]["name"] = "Deload"
        past = [{"id": 1, "category": "WORKOUT", "type": "Ride", "name": "Deload", "start_date_local": "2026-10-18T10:00:00"},
                {"id": 2, "category": "WORKOUT", "type": "Ride", "name": "Deload", "start_date_local": "2026-10-11T10:00:00"}]
        e = self.evidence(acts=acts, current_week_events=past,
                          plans=[plan("2026-10-18", id=1, name="Deload"), plan("2026-10-25", id=3, name="Deload")])
        self.assertEqual(e["name_support"], {"keyword": "deload",
                         "week_phrases": ["recovery week", "rest week", "deload week"],
                         "planned_events": 1, "completed_activities": 1})

    def test_remaining_hard_session_gate_is_preserved(self):
        for count, total, expected in ((0, 0, True), (1, 2, False), (1, 3, True), (2, 3, False)):
            plans = [plan("2026-10-20", id=i, name="Threshold" if i < count else "Easy") for i in range(total)]
            p = self.detect(plans)
            self.assertEqual(p["phase"] == "Deload", expected)

    def test_live_caller_supplies_raw_activity_and_past_event_evidence(self):
        past = [{"id": 101, "category": "WORKOUT", "type": "Ride", "name": "Deload",
                 "start_date_local": "2026-10-18T10:00:00"}]
        values = activities()
        values[0].update(name="Deload", paired_event_id=101)
        for act in values[7:]:
            act["icu_zone_times"] = [{"id": "Z4", "secs": 700}]
        with mock.patch.object(self.sync, '_load_weekly_rows_for_phase', return_value=rows(completed=80)):
            p = self.derive(values, [plan("2026-10-23", 20, id=101, name="Threshold")], past)["phase_detection"]
        e = p["basis"]["stream_2"]["load_evidence"]
        self.assertEqual(e["day_aligned"]["completed_tss"], 240)
        self.assertEqual(e["day_aligned"]["reference_tss_mean"], 600)
        self.assertEqual(e["day_aligned"]["name_support"]["completed_activities"], 1)
        self.assertEqual(e["completed_events_excluded"], 1)
        self.assertEqual((p["phase"], p["confidence"]), ("Deload", "medium"))

    def test_no_input_mutation_and_no_accumulating_evidence(self):
        values, week, plans = activities(), rows(completed=80), [plan("2026-10-20", name="Deload")]
        for row in week[:-1]:
            row["primary_sport_tss"] = 700
        before = copy.deepcopy((values, week, plans))
        expected = self.detect(plans, acts=values, weeks=week)
        for _ in range(10):
            self.assertEqual(self.detect(plans, acts=values, weeks=week), expected)
        self.assertEqual((values, week, plans), before)
        legacy = copy.deepcopy(expected)
        legacy["basis"].pop("main_sport_load")
        legacy["basis"].pop("decision_load_source")
        self.assertLess(len(json.dumps(legacy)), 2500)
        self.assertLess(len(json.dumps(expected)), 10000)  # additive weekly load/monotony evidence


if __name__ == '__main__':
    unittest.main()
