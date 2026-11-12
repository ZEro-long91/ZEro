"""Synthetic v3.149 ACWR, paired-plan and descriptive-fact regression guards."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from _harness import (NetworkBlocked, SYNC_PATH, install_verb_guard,
                      load_module_by_path, restore_verb_guard)

mod = load_module_by_path("s11_sync_facts", SYNC_PATH)
Sync = mod.IntervalsSync
originals = None


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 21, 12, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def ride(aid="i101", date="2026-10-17", **changes):
    result = {"id": aid, "start_date_local": date + "T10:00:00", "name": "Synthetic ride",
              "type": "Ride", "moving_time": 4000, "elapsed_time": 4100,
              "distance": 40000, "total_elevation_gain": 300,
              "icu_joules": 800000, "carbs_used": 120, "max_heartrate": 170,
              "icu_average_watts": 200, "icu_training_load": 80}
    result.update(changes)
    return result


def payload(*acts):
    return {"after_kj": 0, "secs": [1, 5, 60, 300, 1200, 3600], "curves": [
        {"id": a["id"], "start_date_local": a["start_date_local"], "weight": 70.0,
         "watts": [700, 650, 400, 300, 260, 240]} for a in acts]}


class FactsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sync = Sync("synthetic", "synthetic", week_start_day=0)
        self.sync.data_dir = Path(tmp.name)
        self.sync._intervals_data = {}
        patch = mock.patch.object(mod, "datetime", Clock)
        patch.start()
        self.addCleanup(patch.stop)

    def facts(self, activities, start="2026-09-24", end="2026-10-21", **kw):
        return self.sync._build_fun_facts(activities, start, end, **kw)

    def weeks(self, activities=(), starts=("2026-10-05", "2026-10-12", "2026-10-19"), data=None, previous=None):
        weeks = [{"week_start": s} for s in starts]
        with mock.patch.object(self.sync, "_intervals_get", return_value=data or payload(*activities)) as get:
            meta = self.sync._history_fun_fact_power(weeks, activities, previous or {})
        return weeks, meta, get


class Acwr(FactsCase):
    def test_given_example_weekly_tier_and_first_three_nulls(self):
        self.sync.week_start_day = 6
        acts = {d: [ride(date=d, icu_training_load=t)] for d, t in
                (("2026-09-20", 751), ("2026-09-27", 721), ("2026-10-04", 739), ("2026-10-11", 501))}
        weeks = self.sync._build_weekly_tier(acts, {}, 31)
        target = next(w for w in weeks if w["week_start"] == "2026-10-11")
        self.assertEqual(round((501 / 7) / ((739 + 721 + 751) / 21), 2), .68)
        self.assertEqual(target["acwr"], .74)
        self.assertEqual([w["acwr"] for w in weeks[:3]], [None, None, None])

    def test_zero_chronic_and_zero_acute(self):
        rows = [{"week_start": (datetime(2026, 9, 21) + timedelta(weeks=i)).strftime("%Y-%m-%d"),
                 "total_tss": 0, "acwr": 99} for i in range(4)]
        self.sync._couple_weekly_acwr(rows)
        self.assertIsNone(rows[-1]["acwr"])
        rows[0]["total_tss"] = 100
        self.sync._couple_weekly_acwr(rows)
        self.assertEqual(rows[-1]["acwr"], 0)

    def test_partial_week_is_not_extrapolated_and_gap_is_null(self):
        rows = [{"week_start": (datetime(2026, 9, 28) + timedelta(weeks=i)).strftime("%Y-%m-%d"),
                 "total_tss": t} for i, t in enumerate((700, 700, 700, 200))]
        self.sync._couple_weekly_acwr(rows)
        self.assertEqual(rows[-1]["acwr"], .35)
        rows[-1]["week_start"] = "2026-10-27"
        self.sync._couple_weekly_acwr(rows)
        self.assertIsNone(rows[-1]["acwr"])

    def test_retained_legacy_history_is_coupled_before_phase_slice(self):
        rows = [{"week_start": (datetime(2026, 9, 7) + timedelta(weeks=i)).strftime("%Y-%m-%d"),
                 "total_tss": t, "acwr": 9} for i, t in enumerate((751, 721, 739, 501, 600, 700))]
        target = self.sync.data_dir / self.sync.HISTORY_FILE
        target.write_text(json.dumps({"weekly_180d": rows}))
        before = target.read_bytes()
        loaded = self.sync._load_weekly_rows_for_phase()
        self.assertEqual(len(loaded), 4)
        self.assertEqual(loaded[1]["acwr"], .74)
        self.assertIsNone(loaded[0]["acwr"])
        self.assertEqual(target.read_bytes(), before)
        features = self.sync._phase_stream1_features(loaded, is_backfill=True)
        self.assertEqual(features["acwr_trend"], "rising")

    def test_overreached_gate_still_requires_monotony(self):
        for mono, acwr, trend, expected in (("normal", 2, "rising", None),
                ("elevated", 1.5, "stable", "Overreached"),
                ("elevated", 1.3, "rising", "Overreached"),
                ("elevated", 1.29, "rising", None)):
            self.assertEqual(self.sync._phase_from_stream1({"monotony_trend": mono,
                "acwr_trend": trend}, [{"acwr": acwr}]), expected)

    def test_live_and_start_of_day_stay_coupled(self):
        current = [ride(date="2026-10-20", icu_training_load=656)]
        extended = current + [ride("i102", "2026-10-01", icu_training_load=2072)]
        result = self.sync._calculate_derived_metrics(current, extended, [], [], 50, 60, -10,
            [], current, {}, (None, None, None), (None, None, None), None)
        self.assertEqual(result["acwr"], .96)
        self.assertEqual(result["acwr_start_of_day"]["value"], .96)


class Plans(FactsCase):
    def build(self, act, event):
        return self.sync._build_session_structure(act, event, {"time": [0, 1], "watts": [100, 100]}, [])

    def test_empty_paired_plan_retains_exact_metadata_on_both_surfaces(self):
        act = ride(paired_event_id=42)
        event = {"id": 42, "paired_activity_id": "i101", "name": "Synthetic 999h ride",
                 "moving_time": 5400, "workout_doc": {"steps": []}}
        block = self.build(act, event)
        summary = self.sync._structure_summary(block)
        self.assertEqual((block["status"], summary["status"]), ("no_steps", "no_steps"))
        self.assertEqual(summary["plan"], block["plan"])
        self.assertEqual(block["plan"]["duration_s"], 5400)
        self.assertEqual(block["plan"]["name"], "Synthetic 999h ride")
        self.assertNotIn("steps", block)

    def test_no_plan_missing_steps_missing_event_and_conflict_differ(self):
        self.assertEqual(self.build(ride(), None)["status"], "no_plan")
        for event in (None, {"id": 42}, {"id": 42, "workout_doc": {}}, {"id": 43, "name": "Wrong"}):
            block = self.build(ride(paired_event_id=42), event)
            self.assertEqual(block["status"], "unknown")
            if event and event["id"] == 43:
                self.assertNotIn("plan", block)

    def test_explicit_duration_fallback_never_parses_names_or_eta(self):
        event = {"id": 42, "name": "2h to 3h", "moving_duration_range_s": [7200, 10800],
                 "workout_doc": {"steps": [], "duration": 3601}}
        for invalid in (None, True, -1, float("nan"), "7200"):
            event["moving_time"] = invalid
            plan = self.build(ride(paired_event_id=42), event)["plan"]
            self.assertEqual(plan["duration_s"], 3601)
            self.assertEqual(plan["duration_source"], "workout_doc.duration")
            self.assertNotIn("moving_duration_range_s", plan)
        del event["workout_doc"]["duration"]
        self.assertNotIn("duration_s", self.build(ride(paired_event_id=42), event)["plan"])

    def test_old_detail_version_refreshes_metadata_and_name_edit(self):
        act = ride(paired_event_id=42)
        event = {"id": 42, "name": "Original", "moving_time": 5000, "workout_doc": {"steps": []}}
        with mock.patch.object(self.sync, "_fetch_activity_streams", return_value=("ok", {"time": [0, 1]})), \
                mock.patch.object(self.sync, "_fetch_activity_intervals", return_value=("no_data", [])):
            first = self.sync._prepare_session_structures([act], [event], {}, Clock.now(timezone.utc))["i101"]
            first["version"] = 2
            event["name"] = "Revised"
            second = self.sync._prepare_session_structures([act], [event], {}, Clock.now(timezone.utc))["i101"]
        self.assertEqual(second["version"], 6)
        self.assertEqual(second["status"], "no_steps")
        self.assertEqual(second["plan"]["name"], "Revised")


class PeriodFacts(FactsCase):
    def test_known_totals_winners_and_indoor_split(self):
        a = ride(power_curve={"1": 700, "5": 650, "60": 400})
        b = ride("i102", "2026-10-18", type="VirtualRide", moving_time=8000,
                 distance=80000, total_elevation_gain=600, icu_joules=1600000,
                 carbs_used=240, max_heartrate=180, power_curve={"1": 600, "5": 500})
        facts = self.facts([a, b], include_week=True)
        self.assertEqual((facts["moving_time_s"], facts["work_kj"], facts["carbs_used"]), (12000, 2400, 360))
        self.assertEqual(facts["max_power"]["activity_id"], "i101")
        self.assertEqual(facts["max_hr"]["bpm"], 180)
        self.assertEqual(facts["longest_ride"]["distance_m"], 80000)
        self.assertEqual(facts["distance_elevation"]["outdoor"]["distance_m"], 40000)
        self.assertEqual(facts["distance_elevation"]["indoor_virtual"]["elevation_m"], 600)
        self.assertEqual(facts["biggest_day"]["date"], "2026-10-18")
        self.assertEqual(facts["biggest_week"]["week_start"], "2026-10-15")
        self.assertEqual(facts["coverage"]["power"]["status"], "partial")

    def test_rolling_boundaries_future_sport_filter_and_deduplication(self):
        acts = [ride("i1", "2026-10-14"), ride("i2", "2026-10-15"), ride("i3", "2026-10-21"),
                ride("i4", "2026-10-22"), ride("i5", "2026-10-18", type="Run"), ride("i6", "2026-10-18", type="Walk")]
        facts = self.facts(acts + [acts[1]], "2026-10-15", "2026-10-21")
        self.assertEqual(facts["cycling_activity_count"], 2)
        self.assertEqual(facts["work_kj"], 1600)
        self.assertNotIn("biggest_week", facts)

    def test_empty_and_missing_are_distinct_and_zero_carbs_ascent_survive(self):
        empty = self.facts([])
        missing = self.facts([ride(icu_joules=None, carbs_used=None, total_elevation_gain=None)])
        self.assertEqual(empty["work_kj"], 0)
        self.assertIsNone(empty["max_power"])
        self.assertIsNone(missing["work_kj"])
        self.assertIsNone(missing["biggest_day"])
        zero = self.facts([ride(carbs_used=0, total_elevation_gain=0)])
        self.assertEqual(zero["carbs_used"], 0)
        self.assertEqual(zero["distance_elevation"]["outdoor"]["elevation_m"], 0)

    def test_partial_work_and_carbs_coverage_same_day(self):
        facts = self.facts([ride(), ride("i102", icu_joules=None, carbs_used=None)])
        self.assertEqual(facts["work_kj"], 800)
        self.assertEqual(facts["coverage"]["work_kj"], 1)
        self.assertEqual(facts["biggest_day"]["carbs_used"], 120)
        self.assertEqual(facts["biggest_day"]["cycling_activity_count"], 2)

    def test_ties_are_date_then_id_independent_of_input_order(self):
        acts = [ride("i3", "2026-10-18"), ride("i2"), ride("i1")]
        for ordered in (acts, list(reversed(acts))):
            self.assertEqual(self.facts(ordered)["longest_ride"]["activity_id"], "i1")

    def test_environment_flags_never_names_and_unknown_not_outdoor(self):
        for kind in ("Run", "VirtualRun", "Walk", "VirtualRow"):
            self.assertIsNone(self.sync._fact_indoor(ride(type=kind)))
        for change in ({"trainer": True}, {"indoor": True}, {"virtual": True}, {"is_virtual": True},
                       {"sub_type": "virtual"}, {"type": "VirtualRide"}):
            self.assertEqual(self.facts([ride(**change)])["distance_elevation"]["indoor_virtual"]["distance_m"], 40000)
        self.assertEqual(self.facts([ride(name="Zwift indoors")])["distance_elevation"]["outdoor"]["distance_m"], 40000)
        self.assertEqual(self.facts([ride(indoor="false")])["distance_elevation"]["unknown"]["distance_m"], 40000)

    def test_short_curves_no_interpolation_and_invalid_numbers(self):
        facts = self.facts([ride(moving_time=100, power_curve={"1": 500, "5": 450, "60": 300,
                            "300": 999, "1200": float("nan"), "3600": True})])
        self.assertIsNone(facts["peaks"]["300"])
        self.assertEqual(facts["coverage"]["power"]["expected"]["300"], 0)
        self.assertEqual(facts["coverage"]["power"]["status"], "complete")

    def test_rolling_week_edges_ignore_configured_calendar_start(self):
        self.sync.week_start_day = 6
        facts = self.facts([ride("i1", "2026-09-24"), ride("i2", "2026-10-21")], include_week=True)
        self.assertEqual(facts["biggest_week"]["week_start"], "2026-09-24")
        self.assertEqual(facts["biggest_week"]["start"], "2026-09-24")
        self.assertFalse(facts["biggest_week"]["partial"])
        self.assertEqual(facts["biggest_week"]["chunk"], 4)

    def test_formatted_rows_keep_exact_fields_and_curve_budget_gaps(self):
        with mock.patch.object(self.sync, "_build_terrain_for_activity", return_value={}), \
                mock.patch.object(self.sync, "_build_weather_for_activity", return_value={}), \
                mock.patch.object(self.sync, "_get_activity_messages", return_value=("ok", [])), \
                mock.patch.object(self.sync, "_fetch_activity_power_curve", return_value=("transient", "request_budget")):
            rows = self.sync._format_activities([ride(moving_time=4001, distance=40001, trainer=True)])
        self.assertEqual(rows[0]["moving_time_s"], 4001)
        self.assertEqual(rows[0]["distance_m"], 40001)
        self.assertTrue(rows[0]["indoor"])
        self.assertEqual(self.facts(rows, formatted=True)["coverage"]["power"]["status"], "unavailable")

    def test_full_latest_producer_wires_rolling_facts_without_season_call(self):
        acts = [ride(), ride("i102", "2026-10-01", type="VirtualRide")]
        calls = []
        def get(endpoint, params=None):
            calls.append(endpoint)
            if endpoint == "activities":
                return acts
            if endpoint == "events":
                return []
            if endpoint == "wellness":
                return [{"id": "2026-10-21", "ctl": 50, "atl": 60}]
            if endpoint.startswith("wellness/"):
                return {"ctl": 50, "atl": 60}
            return {}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.sync, "_intervals_get", side_effect=get))
            for method, value in (("_generate_saved_workouts", None), ("_generate_terrain", set()),
                    ("_generate_route_weather", {}),
                    ("_generate_intervals", set()), ("_prepare_session_structures", {}),
                    ("_build_terrain_for_activity", {}), ("_build_weather_for_activity", {}),
                    ("_get_activity_messages", ("ok", [])),
                    ("_fetch_activity_power_curve", ("ok", {"1": 700, "5": 650}))):
                stack.enter_context(mock.patch.object(self.sync, method, return_value=value))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            output = self.sync.collect_training_data()
        self.assertEqual(output["fun_facts"]["rolling_7d"]["cycling_activity_count"], 1)
        self.assertEqual(output["fun_facts"]["rolling_28d"]["cycling_activity_count"], 2)
        self.assertEqual(output["fun_facts"]["rolling_28d"]["max_power"]["activity_id"], "i102")
        self.assertNotIn("activity-power-curves", calls)
        self.assertNotIn("fun_facts", output["derived_metrics"])
        chunks = output["rolling_7d_chunks"]
        self.assertEqual(chunks[0]["fun_facts"], output["fun_facts"]["rolling_7d"])
        self.assertEqual(chunks[0]["total_tss"], output["derived_metrics"]["tss_7d_total"])
        self.assertEqual(chunks[0]["zone_distribution"], output["derived_metrics"]["zone_distribution_7d"])
        self.assertEqual(chunks[0]["total_hours"], output["weekly_summary"]["total_training_hours"])
        self.assertEqual(chunks[0]["ctl_end"], output["current_status"]["fitness"]["ctl"])


class RollingChunks(FactsCase):
    def chunks(self, acts, wellness=()):
        formatted = [{"id": a["id"], "date": a["start_date_local"][:10], "type": a["type"],
                      "name": a["name"], "moving_time_s": a.get("moving_time"),
                      "distance_m": a.get("distance"), "elevation_m": a.get("total_elevation_gain"),
                      "work_kj": a["icu_joules"] / 1000 if a.get("icu_joules") else None,
                      "carbs_used": a.get("carbs_used"), "max_hr": a.get("max_heartrate"),
                      "indoor": self.sync._fact_indoor(a), "power_curve": a.get("power_curve"),
                      "power_curve_expected": True} for a in acts]
        with mock.patch.object(self.sync, "_detect_phase_v2", side_effect=AssertionError("phase on chunks")):
            return self.sync._rolling_7d_chunks(acts, list(wellness), formatted, "2026-10-21", 55)

    def test_four_exact_windows_partition_28_days_without_calendar_phase(self):
        acts = [ride("i" + str(i), (datetime(2026, 9, 23) + timedelta(days=i)).strftime("%Y-%m-%d"))
                for i in range(30)]
        chunks = self.chunks(acts)
        self.assertEqual([(c["start"], c["end"]) for c in chunks], [
            ("2026-10-15", "2026-10-21"), ("2026-10-08", "2026-10-14"),
            ("2026-10-01", "2026-10-07"), ("2026-09-24", "2026-09-30")])
        for c in chunks:
            self.assertEqual(c["total_tss"], 560)
            self.assertEqual(c["fun_facts"]["cycling_activity_count"], 7)
            self.assertNotIn("phase", c)
        self.sync.week_start_day = 6
        self.assertEqual(chunks, self.chunks(acts))

    def test_capability_means_match_existing_7d_gates_and_sample_minima(self):
        acts = [ride("i1", moving_time=5400, icu_variability_index=1.05,
                     decoupling=0, icu_efficiency_factor=0, icu_hrr={"value": 0, "hrr": 99}),
                ride("i2", moving_time=5400, icu_variability_index=1,
                     decoupling=-2, icu_efficiency_factor=2, icu_hrr={"hrr": 30}),
                ride("i3", moving_time=5399, icu_variability_index=1.051,
                     decoupling=99, icu_efficiency_factor=99, icu_hrr=-1)]
        chunk = self.chunks(acts)[0]
        self.assertEqual((chunk["durability_mean"], chunk["durability_qualifying"]), (-1, 2))
        self.assertEqual((chunk["ef_mean"], chunk["ef_qualifying"]), (1, 2))
        self.assertEqual((chunk["hrrc_mean"], chunk["hrrc_qualifying"]), (30, 1))
        single = self.chunks(acts[1:2])[0]
        self.assertIsNone(single["durability_mean"])
        self.assertIsNone(single["ef_mean"])
        self.assertEqual(single["durability_qualifying"], 1)
        running = self.chunks([dict(a, type="Run") for a in acts])[0]
        self.assertIsNone(running["durability_mean"])
        self.assertEqual(running["durability_qualifying"], 0)
        self.assertIsNone(running["ef_mean"])
        self.assertEqual(running["fun_facts"]["cycling_activity_count"], 0)

    def test_zone_basis_hard_days_and_all_sport_load(self):
        acts = [ride("i1", "2026-10-17", icu_zone_times=[{"id": "Z4", "secs": 300}]),
                ride("i2", "2026-10-17", icu_zone_times=[{"id": "Z4", "secs": 300}]),
                ride("i3", "2026-10-18", type="Run", icu_hr_zone_times=[0, 0, 0, 0, 300])]
        chunk = self.chunks(acts)[0]
        self.assertEqual(chunk["hard_days"], 2)
        self.assertEqual(chunk["zone_distribution"], {"z1_hours": 0, "z2_hours": 0,
            "z3_hours": 0, "z4_plus_hours": .25, "total_hours": .25, "zone_basis": "mixed"})
        self.assertEqual(chunk["total_tss"], 240)
        self.assertEqual(chunk["fun_facts"]["cycling_activity_count"], 2)
        self.sync.zone_preference["cycling"] = "hr"
        both = ride(icu_zone_times=[{"id": "Z4", "secs": 600}], icu_hr_zone_times=[3600])
        preferred = self.chunks([both])[0]
        self.assertEqual(preferred["zone_distribution"]["zone_basis"], "hr")
        self.assertEqual(preferred["hard_days"], 0)

    def test_wellness_averages_and_exact_end_ctl_missing_and_zero(self):
        days = [{"id": "2026-10-21", "hrv": 60, "restingHR": 50, "sleepSecs": 28800, "ctl": 999},
                {"id": "2026-10-20", "hrv": 80, "restingHR": 40, "sleepSecs": 21600},
                {"id": "2026-10-19", "hrv": 0, "restingHR": 0, "sleepSecs": 0},
                {"id": "2026-10-14", "ctl": 0}, {"id": "2026-10-06", "ctl": 42},
                {"id": "2026-09-23", "hrv": 999, "ctl": 999}]
        chunks = self.chunks([], days)
        self.assertEqual((chunks[0]["avg_hrv"], chunks[0]["avg_resting_hr"], chunks[0]["avg_sleep_hours"]), (70, 45, 7))
        self.assertEqual(chunks[0]["wellness_days"], {"hrv": 2, "resting_hr": 2, "sleep": 2})
        self.assertEqual([c["ctl_end"] for c in chunks], [55, 0, None, None])
        self.assertIsNone(chunks[1]["avg_hrv"])
        self.assertEqual(chunks[0]["total_hours"], 0)
        self.assertIsNone(chunks[0]["fun_facts"]["max_power"])

    def test_biggest_week_is_chunk_not_calendar_bucket(self):
        acts = [ride("i1", "2026-10-14", icu_joules=1000000),
                ride("i2", "2026-10-15", icu_joules=1000000),
                ride("i3", "2026-10-05", icu_joules=1500000)]
        chunks = self.chunks(acts)
        winner = self.facts(acts, include_week=True)["biggest_week"]
        self.assertEqual((winner["chunk"], winner["start"], winner["end"]), (3, "2026-10-01", "2026-10-07"))
        self.assertEqual(winner["work_kj"], chunks[2]["fun_facts"]["work_kj"])
        self.assertFalse(winner["partial"])


class SeasonCurves(FactsCase):
    def test_one_request_exact_contract_and_truncated_arrays(self):
        acts = [ride(), ride("i102", "2026-10-18", type="VirtualRide", moving_time=1000)]
        data = payload(*acts)
        data["curves"][1]["watts"] = [500, 450, 300, 250]
        weeks, meta, get = self.weeks(acts, data=data)
        get.assert_called_once_with("activity-power-curves", {"oldest": "2026-10-05", "newest": "2026-10-21",
            "type": "Ride", "secs": "1,5,60,300,1200,3600"})
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(weeks[1]["fun_facts"]["coverage"]["power"]["status"], "complete")
        self.assertEqual(weeks[1]["fun_facts"]["peaks"]["3600"]["activity_id"], "i101")

    def test_missing_and_mismatched_activities_are_counted(self):
        acts = [ride(), ride("i102")]
        data = payload(acts[0], ride("i999"))
        weeks, meta, _ = self.weeks(acts, data=data)
        power = weeks[1]["fun_facts"]["coverage"]["power"]
        self.assertEqual((power["expected"]["1"], power["available"]["1"]), (2, 1))
        self.assertEqual(meta["unmatched_curves"], 1)
        self.assertEqual(power["status"], "partial")

    def test_success_empty_is_not_failure(self):
        weeks, meta, _ = self.weeks([ride()], data=payload())
        self.assertEqual(meta["status"], "ok")
        self.assertIsNone(weeks[1]["fun_facts"]["max_power"])
        self.assertEqual(weeks[1]["fun_facts"]["coverage"]["power"]["status"], "unavailable")

    def test_invalid_payloads_do_not_become_zero_peaks(self):
        good = payload(ride())
        invalid = [None, {}, {**good, "after_kj": False}, {**good, "after_kj": 1},
                   {**good, "secs": [1, 1]}, {**good, "curves": good["curves"] * 2}]
        for patch in ({"watts": [True]}, {"watts": [-1]}, {"watts": [float("inf")]},
                      {"watts": [1] * 7}, {"start_date_local": "2027-01-01"}, {"id": None}):
            invalid.append({**good, "curves": [{**good["curves"][0], **patch}]})
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.sync._parse_fun_fact_curves(data, "2026-10-01", "2026-10-21")

    def test_same_instance_memoizes_success_and_failure(self):
        acts = [ride()]
        _, _, get = self.weeks(acts)
        self.assertEqual(get.call_count, 1)
        _, _, get = self.weeks(acts)
        self.assertEqual(get.call_count, 0)
        self.sync._fun_fact_curve_run = (("2026-10-05", "2026-10-21"), None, "transport_error")
        _, meta, get = self.weeks(acts)
        self.assertEqual(get.call_count, 0)
        self.assertEqual(meta["error"], "transport_error")

    def test_fresh_persisted_cache_no_request_and_changed_activity_invalidates(self):
        acts = [ride()]
        weeks, meta, _ = self.weeks(acts)
        previous = json.loads(json.dumps({"weekly_180d": weeks, "fun_facts_power": meta}))
        del self.sync._fun_fact_curve_run
        reused, new_meta, get = self.weeks(acts, previous=previous)
        self.assertEqual(get.call_count, 0)
        self.assertEqual(reused, weeks)
        acts[0]["name"] = "Changed"
        _, _, get = self.weeks(acts, previous=previous)
        self.assertEqual(get.call_count, 1)

    def test_old_sport_identity_cache_cannot_restore_excluded_peaks(self):
        acts = [ride()]
        weeks, meta, _ = self.weeks(acts)
        del self.sync._fun_fact_curve_run
        previous = self.packed_previous(weeks, meta)
        previous["fun_facts_power"].pop("sport_identity_rule")
        fresh = [{"week_start": w["week_start"]} for w in weeks]
        with mock.patch.object(self.sync, "_intervals_get", side_effect=mod.requests.exceptions.Timeout("synthetic")) as get:
            failed = self.sync._history_fun_fact_power(fresh, acts, previous)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(failed["status"], "unavailable")
        self.assertEqual(failed["sport_identity_rule"], self.sync.SPORT_IDENTITY_RULE)
        self.assertTrue(all(w["fun_facts"]["max_power"] is None for w in fresh))
        self.assertTrue(all(p is None for w in fresh for p in w["fun_facts"]["peaks"].values()))

    def test_failure_preserves_old_peaks_and_marks_uncovered_weeks(self):
        acts = [ride()]
        weeks, meta, _ = self.weeks(acts)
        previous = {"weekly_180d": weeks[1:], "fun_facts_power": {**meta, "sampled_at": "2026-10-19T12:00:00+00:00"}}
        old_peak = copy.deepcopy(weeks[1]["fun_facts"]["max_power"])
        del self.sync._fun_fact_curve_run
        with mock.patch.object(self.sync, "_intervals_get", side_effect=mod.requests.exceptions.Timeout("SECRET URL")) as get:
            fresh = [{"week_start": s} for s in ("2026-10-05", "2026-10-12", "2026-10-19")]
            failed = self.sync._history_fun_fact_power(fresh, acts, previous)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(failed["status"], "stale")
        self.assertEqual(fresh[1]["fun_facts"]["max_power"], old_peak)
        self.assertEqual(fresh[1]["fun_facts"]["coverage"]["power"]["status"], "stale")
        self.assertEqual(fresh[0]["fun_facts"]["coverage"]["power"]["status"], "unavailable")
        self.assertNotIn("SECRET", json.dumps(failed))

    def test_completed_season_excludes_extreme_current_week(self):
        acts = [ride(), ride("i102", "2026-10-20", moving_time=99999, icu_joules=9999999)]
        data = payload(*acts)
        data["curves"][1]["watts"] = [9999] * 6
        weeks, _, _ = self.weeks(acts, data=data)
        season = self.sync._season_fun_facts(weeks)
        self.assertEqual(season["cycling_activity_count"], 1)
        self.assertEqual(season["work_kj"], 800)
        self.assertEqual(season["max_power"]["watts"], 700)
        self.assertEqual(season["biggest_week"]["week_start"], "2026-10-12")
        self.assertEqual(season["end"], "2026-10-18")
        self.assertEqual(season["completed_weeks"], 2)

    def test_empty_weeks_do_not_turn_missing_season_totals_into_zero(self):
        weeks, _, _ = self.weeks([ride(icu_joules=None, carbs_used=None, distance=None)])
        season = self.sync._season_fun_facts(weeks)
        self.assertIsNone(season["work_kj"])
        self.assertIsNone(season["carbs_used"])
        self.assertIsNone(season["distance_elevation"]["outdoor"]["distance_m"])
        self.assertIsNone(season["biggest_week"])

    def test_invalid_previous_cache_is_not_retained_as_truth(self):
        bad = {"weekly_180d": [{"week_start": "2026-10-12", "fun_facts": {"peaks": {}}}],
               "fun_facts_power": []}
        weeks = [{"week_start": "2026-10-12"}, {"week_start": "2026-10-19"}]
        with mock.patch.object(self.sync, "_intervals_get", return_value={}):
            meta = self.sync._history_fun_fact_power(weeks, [ride()], bad)
        self.assertEqual(meta["status"], "unavailable")
        self.assertIsNone(weeks[0]["fun_facts"]["max_power"])

    def test_retained_partial_week_exposes_its_original_coverage_end(self):
        acts = [ride()]
        weeks, meta, _ = self.weeks(acts)
        weeks[1]["fun_facts"]["end"] = "2026-10-17"
        weeks[1]["fun_facts"]["coverage"]["power"]["end"] = "2026-10-17"
        previous = {"weekly_180d": weeks, "fun_facts_power": {**meta, "status": "stale"}}
        del self.sync._fun_fact_curve_run
        fresh, _, _ = self.weeks(acts, data={"bad": True}, previous=previous)
        self.assertEqual(fresh[1]["fun_facts"]["end"], "2026-10-18")
        self.assertEqual(fresh[1]["fun_facts"]["coverage"]["power"]["end"], "2026-10-17")
        self.assertEqual(self.sync._season_fun_facts(fresh)["coverage"]["power"]["status"], "stale")

    def test_http_single_send_no_retry_redirect_or_budget_change(self):
        response = mock.Mock(status_code=503)
        with mock.patch.object(mod.requests, "get", return_value=response) as get:
            with self.assertRaises(mod.requests.exceptions.HTTPError):
                self.sync._intervals_get("activity-power-curves", {})
        self.assertEqual(get.call_count, 1)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(get.call_args.kwargs["timeout"], self.sync.INTERVALS_READ_TIMEOUT)
        self.assertEqual(self.sync._read_retry_extra_attempts_used, 0)
        self.assertEqual(self.sync._power_curve_fetches_used, 0)

    def test_generate_history_writes_facts_and_coupled_marker(self):
        acts = [ride(), ride("i102", "2026-10-20", type="VirtualRide")]
        def get(endpoint, params=None):
            return {"activities": acts, "wellness": [], "": {}, "activity-power-curves": payload(*acts)}[endpoint]
        with mock.patch.object(self.sync, "_intervals_get", side_effect=get), \
                mock.patch.object(self.sync, "_update_route_eta_history"), contextlib.redirect_stdout(io.StringIO()):
            result = self.sync.generate_history()
        saved = json.loads((self.sync.data_dir / self.sync.HISTORY_FILE).read_text())
        self.assertEqual(saved, result)
        self.assertEqual(result["acwr_definition"], "coupled_7_28")
        self.assertEqual(result["fun_facts"]["season"]["cycling_activity_count"], 1)
        self.assertEqual(result["fun_facts_power"]["status"], "ok")
        self.assertEqual(result["fun_facts_power"]["weekly_cache_version"], 1)
        self.assertTrue(all(set(w["fun_facts"]) <= {"power", "expected", "coverage"}
                            for w in saved["weekly_180d"]))
        del self.sync._fun_fact_curve_run
        with mock.patch.object(self.sync, "_intervals_get", side_effect=get) as cached_get, \
                mock.patch.object(self.sync, "_update_route_eta_history"), contextlib.redirect_stdout(io.StringIO()):
            reused = self.sync.generate_history()
        self.assertNotIn("activity-power-curves", [c.args[0] for c in cached_get.call_args_list])
        self.assertEqual(result["fun_facts"]["season"], reused["fun_facts"]["season"])

    def packed_previous(self, weeks, meta):
        return {"fun_facts_power": meta, "fun_facts": {"season": self.sync._season_fun_facts(weeks)},
                "weekly_180d": [{"week_start": w["week_start"],
                    "fun_facts": self.sync._pack_weekly_fact_power(w["fun_facts"], meta)} for w in weeks]}

    def test_compact_cache_roundtrip_preserves_each_duration_winner_and_coverage(self):
        acts = [ride(), ride("i102", "2026-10-18", moving_time=100)]
        data = payload(*acts)
        data["curves"][1]["watts"] = [900, 550, 500]
        weeks, meta, _ = self.weeks(acts, data=data)
        previous = self.packed_previous(weeks, meta)
        stored = previous["weekly_180d"][1]["fun_facts"]
        self.assertNotIn("coverage", stored)
        self.assertNotIn("activity_name", json.dumps(stored))
        self.assertEqual(len(stored["power"]), 2)
        self.assertEqual(stored["expected"], [2, 2, 2, 1, 1, 1])
        del self.sync._fun_fact_curve_run
        reused, _, get = self.weeks(acts, previous=json.loads(json.dumps(previous)))
        get.assert_not_called()
        self.assertEqual(reused, weeks)
        self.assertEqual(self.sync._season_fun_facts(reused), previous["fun_facts"]["season"])

    def test_compact_incomplete_counts_are_not_silently_complete(self):
        acts = [ride(), ride("i102")]
        weeks, meta, _ = self.weeks(acts, data=payload(acts[0]))
        previous = self.packed_previous(weeks, meta)
        self.assertEqual(previous["weekly_180d"][1]["fun_facts"]["coverage"],
                         {"status": "partial", "available": 1})
        del self.sync._fun_fact_curve_run
        reused, _, get = self.weeks(acts, previous=previous)
        get.assert_not_called()
        self.assertEqual(reused, weeks)

    def test_compact_stale_fallback_keeps_names_counts_and_original_window(self):
        acts = [ride()]
        weeks, meta, _ = self.weeks(acts)
        weeks[1]["fun_facts"]["end"] = "2026-10-17"
        power = weeks[1]["fun_facts"]["coverage"]["power"]
        power["end"] = "2026-10-17"
        power["sampled_at"] = "2026-10-17T12:00:00+00:00"
        previous = self.packed_previous(weeks, {**meta, "status": "stale"})
        del self.sync._fun_fact_curve_run
        # The activity is absent in the new list: previous season attribution
        # retains its name, while the old weekly cache remains explicitly stale.
        fresh, fresh_meta, _ = self.weeks([], data={"bad": True}, previous=previous)
        retained = fresh[1]["fun_facts"]
        self.assertEqual(retained["max_power"], weeks[1]["fun_facts"]["max_power"])
        self.assertEqual(retained["coverage"]["power"]["end"], "2026-10-17")
        self.assertEqual(retained["coverage"]["power"]["sampled_at"], "2026-10-17T12:00:00+00:00")
        packed = self.packed_previous(fresh, fresh_meta)
        again, _, _ = self.weeks([], data={"bad": True}, previous=packed)
        self.assertEqual(again[1]["fun_facts"]["coverage"]["power"], retained["coverage"]["power"])

    def test_malformed_compact_cache_is_rejected_before_reuse(self):
        weeks, meta, _ = self.weeks([ride()])
        good = self.packed_previous(weeks, meta)
        for change in ({"expected": True}, {"expected": [1]}, {"power": [["i101", "bad", 9]]},
                       {"power": []}, {"coverage": {"available": 3}},
                       {"coverage": {"end": "2027-01-01"}},
                       {"power": [["i101", "2026-10-17", True, 650, 400, 300, 260, 240]]}):
            with self.subTest(change=change):
                previous = copy.deepcopy(good)
                previous["weekly_180d"][1]["fun_facts"].update(change)
                self.sync._fun_fact_curve_run = (("2026-10-05", "2026-10-21"), None, "transport_error")
                fresh, _, _ = self.weeks([ride()], previous=previous)
                self.assertIsNone(fresh[1]["fun_facts"]["max_power"])
                self.assertEqual(fresh[1]["fun_facts"]["coverage"]["power"]["status"], "unavailable")

    def test_network_guard_negative_control(self):
        with self.assertRaises(NetworkBlocked):
            mod.requests.get("https://invalid.example.test")


if __name__ == "__main__":
    unittest.main()
