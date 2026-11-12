"""Synthetic offline checks for numeric definition identity and ride-day FTP."""
import contextlib
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from _harness import SYNC_PATH, load_module_by_path, install_verb_guard, restore_verb_guard
from test_session_structure import inputs

mod = load_module_by_path("s11_plan_definition", SYNC_PATH)
originals = None
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


class Definition(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sync = mod.IntervalsSync("synthetic", "synthetic")
        self.sync.data_dir = Path(tmp.name)
        self.sync._intervals_data = {}
        self.act, self.event, self.streams = inputs(powers=(220,), duration=120)
        self.doc = self.event["workout_doc"]
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for method, value in (("_build_terrain_for_activity", {}), ("_build_weather_for_activity", {}),
                              ("_get_activity_messages", ("ok", [])),
                              ("_fetch_activity_power_curve", ("no_data", None))):
            stack.enter_context(mock.patch.object(self.sync, method, return_value=value))

    def key(self, doc=None):
        return self.sync._plan_definition_key(self.doc if doc is None else doc)

    def percent(self, units="%ftp"):
        self.doc["ftp"] = 287
        self.doc["steps"][0].update(power={"value": 80, "units": units},
                                    _power={"value": 229.6, "start": 220, "end": 240})

    def history(self, data):
        (self.sync.data_dir / "ftp_history.json").write_text(json.dumps(data))

    def build(self):
        return self.sync._build_session_structure(self.act, self.event, self.streams, [])

    def formatted(self, prior=None, events=None):
        return self.sync._format_activities([self.act], previous_latest_lookup=prior,
                    paired_events=[self.event] if events is None else events)[0]

    def prepare(self, events=None):
        with mock.patch.object(self.sync, "_fetch_activity_streams", return_value=("ok", self.streams)):
            return self.sync._prepare_session_structures([self.act], [self.event] if events is None else events,
                                                        {}, NOW)[self.act["id"]]

    def test_endpoint_enrichment_and_text_do_not_change_key(self):
        key = self.key()
        enriched = copy.deepcopy(self.doc)
        enriched.update(ftp=310, lthr=180, target="POWER", average_watts=222,
                        normalized_power=230, strain_score=17, polarization_index=1.2,
                        variability_index=1.1, zoneTimes=[{"secs": 120}], name="Renamed")
        enriched["steps"][0].update(_power={"value": 999}, _hr={"value": 999},
                                    _pace={"value": 999}, _cadence={"value": 999},
                                    text="Optional label", name="Optional name", description="Context")
        self.assertEqual(self.key(enriched), key)
        enriched["steps"][0]["duration"] = 120.0
        enriched["steps"][0]["power"]["value"] = 220.0
        self.assertEqual(self.key(enriched), key)

    def test_numeric_structure_and_all_target_families_change_key(self):
        key = self.key()
        for field, value in (("duration", 121), ("distance", 200), ("ramp", True), ("reps", 2)):
            edited = copy.deepcopy(self.doc)
            edited["steps"][0][field] = value
            self.assertNotEqual(self.key(edited), key)
        for kind in ("power", "hr", "pace", "cadence"):
            for field, value in (("value", 230), ("start", 190), ("end", 250),
                                 ("min", 180), ("max", 260), ("units", "zone"),
                                 ("zone", 3), ("ramp", True)):
                with self.subTest(kind=kind, field=field):
                    edited = copy.deepcopy(self.doc)
                    edited["steps"][0].setdefault(kind, {})[field] = value
                    self.assertNotEqual(self.key(edited), key)

    def test_nested_repeats_and_order_are_preserved(self):
        nested = {"duration": 720, "steps": [{"reps": 3, "steps": [
            {"duration": 120, "power": {"value": 80, "units": "%ftp"}},
            {"duration": 120, "power": {"value": 50, "units": "%ftp"}}]}]}
        key = self.key(nested)
        for change in ("reps", "duration", "value", "order"):
            edited = copy.deepcopy(nested)
            group = edited["steps"][0]
            if change == "reps":
                group["reps"] = 4
            elif change == "duration":
                group["steps"][0]["duration"] = 121
            elif change == "value":
                group["steps"][0]["power"]["value"] = 81
            else:
                group["steps"].reverse()
            self.assertNotEqual(self.key(edited), key)
        nested["steps"][0]["text"] = "Repeat"
        nested["steps"][0]["steps"][0]["_power"] = {"value": 250}
        self.assertEqual(self.key(nested), key)

    def test_invalid_and_over_limit_definitions_have_no_partial_key(self):
        for value in (None, {}, {"steps": None}, {"steps": [None]},
                      {"steps": [{"duration": float("nan")}]},
                      {"steps": [{"duration": 1}] * 10001}):
            self.assertIsNone(self.sync._plan_definition_key(value))
        self.assertIsNotNone(self.key({"steps": []}))
        deep = {"steps": []}
        for _ in range(66):
            deep = {"steps": [deep]}
        self.assertIsNone(self.key(deep))

    def test_legacy_adoption_preserves_all_assessed_statuses_then_detects_edit(self):
        for version in (None, 1):
            for status in ("followed", "partly_followed", "not_followed"):
                with self.subTest(version=version, status=status):
                    prior = self.formatted()
                    prior["interval_data"]["plan_match"].update(status=status, steps=[[119, 220, 220, True]])
                    prior["plan_pairing"]["definition_key"] = "legacy-digest"
                    if version is None:
                        prior["plan_pairing"].pop("definition_key_version")
                    else:
                        prior["plan_pairing"]["definition_key_version"] = version
                    adopted = self.formatted({self.act["id"]: prior})
                    self.assertEqual(adopted["interval_data"]["plan_match"]["status"], status)
                    self.assertTrue(adopted["interval_data"]["plan_match"]["assessment_retained"])
                    self.assertEqual(adopted["interval_data"]["plan_match"]["steps"], prior["interval_data"]["plan_match"]["steps"])
                    self.assertEqual(adopted["plan_pairing"]["definition_key_version"], 2)
                    self.assertEqual(adopted["plan_pairing"]["definition_key"], self.key())
                    self.doc["steps"][0]["power"]["value"] += 1
                    edited = self.formatted({self.act["id"]: adopted})
                    self.assertEqual(edited["interval_data"]["plan_match"]["status"], "not_assessed")
                    self.assertEqual(edited["interval_data"]["plan_match"]["retained_step_assessment"]["status"], status)
                    self.doc["steps"][0]["power"]["value"] -= 1

    def test_upgrade_blind_spot_and_identity_conflicts_are_distinct(self):
        prior = self.formatted()
        prior["interval_data"]["plan_match"]["status"] = "followed"
        prior["plan_pairing"].pop("definition_key_version")
        self.doc["steps"][0]["duration"] += 1
        self.assertEqual(self.formatted({self.act["id"]: prior})["interval_data"]["plan_match"]["status"], "followed")
        self.event["paired_activity_id"] = "different-activity"
        self.assertEqual(self.formatted({self.act["id"]: prior})["interval_data"]["plan_match"]["status"], "not_assessed")
        self.event.pop("paired_activity_id")
        prior["interval_data"]["plan_match"]["plan"]["event_id"] = "old-event"
        self.assertEqual(self.formatted({self.act["id"]: prior})["interval_data"]["plan_match"]["status"], "not_assessed")

    def test_upgrade_does_not_resurrect_demoted_assessment(self):
        prior = self.formatted()
        prior["interval_data"]["plan_match"].update(status="not_assessed", retained_step_assessment={"status": "followed"})
        prior["plan_pairing"].pop("definition_key_version")
        self.assertEqual(self.formatted({self.act["id"]: prior})["interval_data"]["plan_match"]["status"], "not_assessed")

    def test_activity_ftp_wins_and_difference_is_detail_only(self):
        self.act["icu_ftp"] = 275
        self.history({"indoor": {"2025-12-01": 300}})
        for units in ("%ftp", "%FTP", "%", "percent_ftp"):
            self.percent(units)
            with mock.patch.object(self.sync, "_load_ftp_history", side_effect=AssertionError("unnecessary history")):
                block = self.build()
            self.assertEqual(block["status"], "followed")
            self.assertEqual(block["steps"][0]["target_w"], 220)
            self.assertEqual(block["power_target_basis"], {"source": "activity.icu_ftp", "ftp": 275, "plan_ftp": 287})
            self.assertNotIn("power_target_basis", self.sync._structure_summary(block))
        self.doc["ftp"] = 275
        self.assertNotIn("plan_ftp", self.build()["power_target_basis"])
        self.doc["steps"][0].pop("_power")
        self.assertEqual(self.build()["steps"][0]["target_w"], 220)

    def test_history_uses_date_and_activity_environment(self):
        self.percent()
        self.history({"indoor": {"2025-12-01": 250, "2026-01-01": 275, "2026-01-02": 320},
                      "outdoor": {"2025-12-01": 287}})
        for kind, trainer, expected, environment in (("VirtualRide", False, 275, "indoor"),
                ("Ride", True, 275, "indoor"), ("Ride", False, 287, "outdoor")):
            self.act.update(type=kind, trainer=trainer)
            block = self.build()
            self.assertAlmostEqual(block["steps"][0]["target_w"], expected * .8)
            self.assertEqual(block["power_target_basis"]["source"], "ftp_history")
            self.assertEqual(block["power_target_basis"]["environment"], environment)
            self.assertLessEqual(block["power_target_basis"]["date"], "2026-01-01")

    def test_invalid_activity_ftp_uses_history(self):
        self.percent()
        self.history({"indoor": {"2025-12-01": 275}})
        for invalid in (None, 0, -1, True, "275", float("nan"), float("inf")):
            self.act["icu_ftp"] = invalid
            self.assertEqual(self.build()["steps"][0]["target_w"], 220)

    def test_missing_history_unknown_environment_and_future_only_fall_back(self):
        self.percent()
        for history in ({}, {"outdoor": {"2025-12-01": 287}},
                        {"indoor": {"2026-01-02": 275}}, {"indoor": {"bad-date": 275}}):
            self.history(history)
            block = self.build()
            self.assertEqual(block["power_target_basis"], {"source": "intervals_resolved"})
            self.assertEqual(block["steps"][0]["target_w"], 229.6)
        self.history({"indoor": {"2025-12-01": 275}, "outdoor": {"2025-12-01": 287}})
        self.act.update(type="Ride", trainer="unknown")
        self.assertEqual(self.build()["power_target_basis"]["source"], "intervals_resolved")
        self.act.update(type="VirtualRide", start_date_local="invalid")
        self.assertEqual(self.build()["power_target_basis"]["source"], "intervals_resolved")
        self.doc["steps"][0].pop("_power")
        self.assertEqual(self.build()["status"], "unknown")

    def test_percent_ranges_and_ramps(self):
        self.percent()
        self.act["icu_ftp"] = 275
        self.doc["steps"][0]["power"] = {"units": "%ftp", "min": 75, "max": 85}
        block = self.build()
        self.assertEqual(block["steps"][0]["target_w"], [206.25, 233.75])
        self.assertEqual(block["status"], "followed")
        self.doc["steps"][0]["power"].update(value=80, min="bad", max=None)
        self.assertEqual(self.build()["status"], "unknown")
        self.doc["steps"][0]["power"] = {"units": "%ftp", "start": 75, "end": 85}
        self.assertEqual(self.build()["status"], "unknown")

    def test_absolute_watts_ignore_all_ftp_and_resolved_values(self):
        original = self.build()
        self.act["icu_ftp"] = 275
        self.doc["ftp"] = 287
        self.doc["steps"][0]["_power"] = {"value": 500, "start": 490, "end": 510}
        with mock.patch.object(self.sync, "_load_ftp_history", side_effect=AssertionError("absolute target")):
            self.assertEqual(self.build(), original)
        self.assertNotIn("power_target_basis", original)

    def test_cache_refreshes_on_upgrade_and_ftp_basis_change(self):
        self.percent()
        self.act["icu_ftp"] = 275
        first = self.prepare()
        self.assertEqual(first["cache"]["plan_key_version"], 2)
        with mock.patch.object(self.sync, "_fetch_activity_streams", side_effect=AssertionError("cache hit")):
            self.assertEqual(self.sync._prepare_session_structures([self.act], [self.event], {}, NOW)[self.act["id"]], first)
        first["cache"].pop("plan_key_version")
        with mock.patch.object(self.sync, "_fetch_activity_streams", return_value=("ok", self.streams)) as fetch:
            self.sync._prepare_session_structures([self.act], [self.event], {}, NOW)
            self.assertEqual(fetch.call_count, 1)
        self.act["icu_ftp"] = 280
        next_block = self.prepare()
        self.assertEqual(next_block["steps"][0]["target_w"], 224)
        self.assertEqual(next_block["cache"]["plan_key"], first["cache"]["plan_key"])
        self.act.pop("icu_ftp")
        self.history({"indoor": {"2025-12-01": 275}})
        self.prepare()
        self.history({"indoor": {"2025-12-01": 280}})
        self.assertEqual(self.prepare()["steps"][0]["target_w"], 224)

    def test_single_event_fallback_reuses_versioned_key(self):
        expected = self.formatted()["plan_pairing"]["definition_key"]
        with mock.patch.object(self.sync, "_intervals_get", return_value=self.event):
            block = self.prepare(events=[])
        row = self.formatted(events=[])
        self.assertEqual(row["plan_pairing"]["definition_key"], expected)
        self.assertEqual(row["plan_pairing"]["definition_key_version"], 2)
        self.assertEqual(block["cache"]["plan_key"], expected)

    def test_failed_legacy_refresh_does_not_relabel_key_or_break_backoff(self):
        first = self.prepare()
        first["cache"]["plan_key"] = "legacy-key"
        first["cache"].pop("plan_key_version")
        with mock.patch.object(self.sync, "_intervals_get", return_value=None), \
                mock.patch.object(self.sync, "_fetch_activity_streams", return_value=("transient", None)) as fetch:
            block = self.sync._prepare_session_structures([self.act], [], {}, NOW)[self.act["id"]]
            self.assertIsNone(block["cache"]["plan_key"])
            self.sync._prepare_session_structures([self.act], [], {}, NOW + timedelta(minutes=1))
            self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
