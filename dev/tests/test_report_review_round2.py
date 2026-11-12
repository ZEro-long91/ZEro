"""Owner-approved report producer corrections, synthetic and offline."""
import contextlib
import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from _harness import SYNC_PATH, load_module_by_path, install_verb_guard, restore_verb_guard
from test_session_structure import inputs
from test_dfa_drift import stream

mod = load_module_by_path("s11_report_round2", SYNC_PATH)
originals = None


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


class ReportProducers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sync = mod.IntervalsSync("synthetic", "synthetic")
        self.sync.data_dir = Path(tmp.name)
        self.sync._intervals_data = {}
        self.sync.week_start_day = 6
        self.act = {"id": "s1", "type": "Ride", "name": "Synthetic easy", "trainer": True,
                    "start_date_local": "2026-07-01T08:00:00", "moving_time": 5520,
                    "elapsed_time": 5520, "paired_event_id": 42, "icu_ftp": 240}
        self.event = {"id": 42, "paired_activity_id": "s1", "name": "Easy plan",
                      "moving_time": 5400, "workout_doc": {"steps": []}}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for method, value in (("_build_terrain_for_activity", {}), ("_build_weather_for_activity", {}),
                              ("_get_activity_messages", ("ok", [])),
                              ("_fetch_activity_power_curve", ("no_data", None))):
            stack.enter_context(mock.patch.object(self.sync, method, return_value=value))

    def formatted(self, act=None, event=None, prior=None):
        return self.sync._format_activities([act or self.act], paired_events=[event or self.event],
                                           previous_latest_lookup=prior)[0]

    def test_duration_tolerance_and_power_match_are_independent(self):
        for tail, expected in ((120, "followed"), (300, "followed"), (301, "partly_followed")):
            act, event, streams = inputs(powers=(100,), duration=5400, tail=tail)
            block = self.sync._build_session_structure(act, event, streams)
            self.assertEqual(block["status"], expected)
        act, event, streams = inputs(powers=(100,), duration=7200, tail=360)
        self.assertEqual(self.sync._build_session_structure(act, event, streams)["status"], "followed")
        streams["watts"][:7200] = [130] * 7200
        self.assertEqual(self.sync._build_session_structure(act, event, streams)["status"], "not_followed")
        self.assertTrue(self.sync._plan_duration_check(7200, 6840)["within_tolerance"])
        self.assertFalse(self.sync._plan_duration_check(7200, 6839)["within_tolerance"])
        self.assertIsNone(self.sync._plan_duration_check(None, 7200)["within_tolerance"])

    def test_pairing_any_sport_and_reverse_identity_no_requests(self):
        with mock.patch.object(self.sync, "_intervals_get", side_effect=AssertionError("extra request")):
            for sport in ("Walk", "Hike", "Run", "NordicSki", "WeightTraining", "Ride"):
                row = self.formatted(dict(self.act, type=sport, paired_event_id=None))
                self.assertEqual(row["plan_pairing"]["plan"]["name"], "Easy plan")
                self.assertTrue(row["plan_pairing"]["duration"]["within_tolerance"])
                self.assertFalse(row["plan_pairing"]["targets_intensity"])
                if sport in ("Walk", "Hike"):
                    self.assertEqual(row["interval_data"]["plan_match"]["issues"], ["sport_out_of_scope"])

    def test_old_pairing_refresh_and_assessment_retention(self):
        prior = {"s1": {"structure_summary": {"status": "no_plan"}}}
        row = self.formatted(prior=prior)
        self.assertEqual(row["interval_data"]["plan_match"]["status"], "no_steps")
        self.assertTrue(row["plan_pairing"]["duration"]["within_tolerance"])
        structured = dict(self.event, workout_doc={"steps": [{"duration": 5400, "power": {"value": 60, "units": "%ftp"}}]})
        row = self.formatted(event=structured)
        row["interval_data"]["plan_match"].update(status="followed")
        refreshed = self.formatted(event=dict(structured, name="Renamed"), prior={"s1": row})
        self.assertEqual(refreshed["interval_data"]["plan_match"]["plan"]["name"], "Renamed")
        self.assertTrue(refreshed["interval_data"]["plan_match"]["assessment_retained"])
        changed = dict(structured, workout_doc={"steps": [{"duration": 5400, "power": {"value": 95, "units": "%ftp"}}]})
        refreshed = self.formatted(event=changed, prior={"s1": row})
        self.assertEqual(refreshed["interval_data"]["plan_match"]["status"], "not_assessed")
        self.assertEqual(refreshed["interval_data"]["plan_match"]["retained_step_assessment"]["status"], "followed")
        conflict = self.formatted(event=dict(structured, paired_activity_id="other"), prior={"s1": row})
        self.assertEqual(conflict["interval_data"]["plan_match"]["status"], "not_assessed")
        self.assertIn("pairing_conflict", conflict["interval_data"]["plan_match"]["issues"])

    def test_z3_both_bases_plan_intent_and_hard_day_ladder(self):
        act = dict(self.act, icu_zone_times=[{"id": "Z3", "secs": 2400}],
                   icu_hr_zone_times=[2000, 3000, 180, 0, 0])
        row = self.formatted(act)
        self.assertEqual(row["z3_time"], {"power_s": 2400, "hr_s": 180})
        detail = self.sync._hard_day_evidence([act], {"s1": row})
        self.assertTrue(detail["power_z3_only_non_intensity"])
        self.assertEqual(detail["triggers"], [{"basis": "power", "rung": "z3+", "seconds": 2400, "threshold_s": 1800}])
        for target, expected in ((75, False), (76, True)):
            event = dict(self.event, workout_doc={"steps": [{"steps": [{"duration": 60, "power": {"value": target, "units": "%ftp"}}]}]})
            row = self.formatted(act, event)
            self.assertIs(row["plan_pairing"]["targets_intensity"], expected)
            self.assertEqual(self.sync._hard_day_evidence([act], {"s1": row})["power_z3_only_non_intensity"], not expected)
        row = self.formatted(act, dict(self.event, workout_doc={"steps": [{"duration": 10}]}))
        self.assertIsNone(row["plan_pairing"]["targets_intensity"])
        hr = dict(act, type="Run", icu_zone_times=[], icu_hr_zone_times=[0, 0, 0, 600, 0])
        self.assertEqual(self.sync._hard_day_evidence([hr], {})["triggers"][0]["basis"], "hr")
        hr["icu_hr_zone_times"][3] = 599
        self.assertIsNone(self.sync._hard_day_evidence([hr], {}))
        for bpm, expected in ((140, False), (141, True)):
            event = dict(self.event, workout_doc={"steps": [{"duration": 60, "hr": {"value": bpm, "units": "bpm"}}]})
            self.assertIs(self.formatted(dict(act, icu_hr_zones=[120, 140, 160]), event)["plan_pairing"]["targets_intensity"], expected)

    def test_28_day_wellness_counts_latest_ranges_and_chunks(self):
        start = datetime(2026, 6, 17)
        rows = [{"id": (start + timedelta(days=i)).strftime("%Y-%m-%d"), "hrv": 40 + i,
                 "restingHR": 50, "sleepSecs": 27000, "sleepQuality": 2, "weight": 70 + i / 10}
                for i in range(28)]
        rows += [{"id": "2026-06-16", "weight": 200}, {"id": "2026-07-15", "weight": 200}]
        rollup = self.sync._report_wellness(rows, "2026-06-17", "2026-07-14")
        self.assertEqual(rollup["weight_kg"]["days"], 28)
        self.assertEqual(rollup["weight_kg"]["avg"], 71.35)
        self.assertEqual(rollup["weight_kg"]["latest_date"], "2026-07-14")
        self.assertEqual(rollup["sleep_hours"]["avg"], 7.5)
        chunks = self.sync._rolling_7d_chunks([], rows, [], "2026-07-14", 40)
        self.assertEqual([c["wellness"]["hrv"]["days"] for c in chunks], [7, 7, 7, 7])
        self.assertEqual(chunks[-1]["start"], "2026-06-17")
        self.assertEqual(chunks[0]["start"], "2026-07-08")

    def test_dfa_28_days_same_eligibility_not_last_seven_sessions(self):
        block = self.sync._compute_dfa_block(stream(5400))
        rows = []
        for i in range(12):
            date = (datetime(2026, 6, 20) + timedelta(days=i * 2)).isoformat()
            rows.append(dict(self.act, id=str(i), date=date, dfa_rollup={
                "date": date, "schema_version": 2, "dfa": copy.deepcopy(block)}))
        rows[0]["dfa_rollup"]["dfa"]["drift_quality"]["status"] = "descriptive_only"
        rows[1]["dfa_rollup"]["dfa"]["quality"]["sufficient"] = False
        profile = self.sync._dfa_28d_profile(rows, "2026-06-17", "2026-07-14")
        sport = profile["trailing_by_sport"]["cycling"]
        self.assertEqual(sport["n_sessions"], 11)
        self.assertEqual(sport["drift_sessions"], 10)
        self.assertEqual(profile["recorded_sessions"], 12)
        self.sync._intervals_data = {"schema_version": 2, "activities": [
            {"date": r["date"], "type": r["type"], "dfa": r["dfa_rollup"]["dfa"]} for r in rows]}
        self.assertEqual(self.sync._calculate_dfa_a1_profile()["trailing_by_sport"]["cycling"]["n_sessions"], 7)
        self.assertIsNone(self.sync._dfa_28d_profile(rows, "2026-08-01", "2026-08-28"))

    def test_dfa_compact_retention_and_fresh_absence_supersedes(self):
        block = self.sync._compute_dfa_block(stream(5400))
        self.sync._intervals_data = {"schema_version": 2, "activities": [{"activity_id": "s1", "dfa": block}]}
        first = self.formatted()
        self.sync._intervals_data = {}
        retained = self.formatted(prior={"s1": first})
        self.assertEqual(retained["dfa_rollup"], first["dfa_rollup"])
        self.assertNotIn("streams", retained["dfa_rollup"])
        self.sync._intervals_data = {"schema_version": 2, "activities": [{"activity_id": "s1"}]}
        self.assertNotIn("dfa_rollup", self.formatted(prior={"s1": first}))

    def test_sustainability_tie_uses_activity_flag_and_environment_ftp(self):
        window = ("2026-06-01", "2026-07-12")
        curve = {"list": [{"id": "r." + ".".join(window), "secs": [300, 1200, 3600],
                            "watts": [300, 250, 210], "activity_ids": ["s1"] * 3}]}
        for activity_type, trainer, source, ftp in (("VirtualRide", True, "observed_indoor", 220),
                                                    ("Ride", False, "observed_outdoor", 240),
                                                    ("Ride", "false", "observed_unknown", None)):
            for types in (("Ride", "VirtualRide"), ("VirtualRide", "Ride")):
                result = self.sync._calculate_sustainability_profile(
                    {"cycling": {"power": {k: curve for k in types}}}, window,
                    {"w_prime": 18000}, {"cycling": {"ftp": 240, "ftp_indoor": 220}}, [], [], 70,
                    activities=[dict(self.act, date="2026-07-01", type=activity_type, trainer=trainer)])
                anchors = result["cycling"]["anchors"]
                for anchor in anchors.values():
                    if anchor["actual_watts"] is not None:
                        self.assertEqual(anchor["source"], source)
                        self.assertEqual(anchor["ftp_used"], ftp)
                self.assertEqual(result["cycling"]["ftp_used"], ftp)

    def test_sustainability_cached_observation_and_unknown_identity(self):
        window = ("2026-06-01", "2026-07-12")
        curve = {"list": [{"id": "r." + ".".join(window), "secs": [300, 1200], "watts": [300, 250]}]}
        args = ({"cycling": {"power": {"Ride": curve}}}, window, {"w_prime": 18000},
                {"cycling": {"ftp": 240, "ftp_indoor": 220}}, [], [], 70)
        row = dict(self.act, date="2026-07-01", power_curve={"300": 300, "1200": 250})
        result = self.sync._calculate_sustainability_profile(*args, activities=[row])
        self.assertEqual(result["cycling"]["anchors"]["300s"]["source"], "observed_indoor")
        row["power_curve"] = {"300": 299, "1200": 249}
        result = self.sync._calculate_sustainability_profile(*args, activities=[row])
        self.assertEqual(result["cycling"]["anchors"]["300s"]["source"], "observed_unknown")
        self.assertIsNone(result["cycling"]["anchors"]["300s"]["cp_model_watts"])


if __name__ == "__main__":
    unittest.main()
