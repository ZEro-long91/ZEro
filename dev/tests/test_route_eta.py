"""Synthetic personal ETA validation; no athlete records or live requests."""
import copy
import json
import math
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from _harness import SYNC_PATH, install_verb_guard, load_module_by_path, restore_verb_guard
from test_route_weather import NOW, history

mod = load_module_by_path("s11_personal_eta", SYNC_PATH)
Weather = mod.RouteWeather


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


def synthetic_history(count=60, spread=.15, ascent=False, paired=False, bias=1.25):
    rows = []
    for i in range(count):
        km = 60 + (i * 17) % 61
        gain_per_km = (i * 7) % 31
        noise = 1 + spread * (2 * ((i * 19) % count) / max(1, count - 1) - 1)
        moving = km * (95 + 2.5 * gain_per_km if ascent else 130) * noise
        row = {"id": "synthetic-" + str(i), "type": "Ride", "distance": km * 1000,
               "moving_time": moving, "elapsed_time": moving * (1.02 + .001 * (i % 9)),
               "start_date_local": (NOW - timedelta(days=i + 1)).date().isoformat()}
        if ascent:
            row["recorded_ascent_m"] = km * gain_per_km * bias
            if paired:
                row["producer_ascent_m"] = km * gain_per_km
        rows.append(row)
    return rows


POWER_INPUTS = {"ftp_w": 250, "body_weight_kg": 75}


def powered_history(count=30, hilly=False):
    context = Weather.physics_context(POWER_INPUTS)
    rows = synthetic_history(count)
    for i, row in enumerate(rows):
        gain = row["distance"] / 1000 * ((i % 15) * 2 if hilly else i % 4)
        power = 195 + (i % 5) * 2
        row.update(producer_ascent_m=gain, average_power_w=power)
        row["moving_time"] = Weather.proxy_seconds(context, row["distance"] / 1000, gain, power) * (1.03 + .02 * math.sin(i))
        row["elapsed_time"] = row["moving_time"] * 1.04
    return rows


def route_terrain(gain=0):
    return {"total_elevation_m": gain, "total_distance_km": 90,
            "polyline": [[0, 55, 12, 50], [15, 55.1, 12, 50 + gain],
                         [30, 55.2, 12, 50], [90, 55.8, 12, 50]],
            "climbs": [{"position_km": 0, "distance_km": 15}] if gain else [],
            "descents": [{"position_km": 15, "distance_km": 15}] if gain else []}


def rolling_terrain():
    # 90 km, repeated 500 m rises/falls, 630 m gain (7 m/km), not a private route.
    return {"total_distance_km": 90, "total_elevation_m": 630,
            "polyline": [[i / 2, 55 + i / 222.39, 12, 50 + (7 if i % 2 else 0)] for i in range(181)],
            "climbs": [], "descents": []}


def rolling_history(count=30):
    rows = powered_history(count)
    context = Weather.physics_context(POWER_INPUTS)
    for i, row in enumerate(rows):
        km, gain = row["distance"] / 1000, row["distance"] / 1000 * (4 + i % 7)
        row["producer_ascent_m"] = gain
        row["moving_time"] = Weather.proxy_seconds(context, km, gain, row["average_power_w"]) * (1.03 + .02 * math.sin(i))
        row["elapsed_time"] = row["moving_time"] * 1.04
    return rows


class PersonalEtaTests(unittest.TestCase):
    def test_consistent_history_gives_observed_narrow_rolling_window(self):
        rows = synthetic_history()
        eta = Weather.estimate_eta(rows, {})
        low, high, center = Weather.moving_window(eta, 90, [70, 10, 10], 650)
        self.assertEqual(eta["status"], "history_prior")
        self.assertGreater((high - low) / center, .18)
        self.assertLess((high - low) / center, .35)
        self.assertAlmostEqual(center, 90 * 130, delta=1)
        self.assertEqual(eta["stops"]["status"], "history")

    def test_inconsistent_history_widens_from_observations(self):
        narrow = Weather.estimate_eta(synthetic_history(), {})
        wide = Weather.estimate_eta(synthetic_history(spread=.4), {})
        a = Weather.moving_window(narrow, 100)
        b = Weather.moving_window(wide, 100)
        self.assertGreater(b[1] - b[0], 2 * (a[1] - a[0]))

    def test_thin_history_explicit_and_power_fallback_order(self):
        rows = synthetic_history()
        power = {"ftp_w": 240, "body_weight_kg": 70}
        self.assertEqual(Weather.estimate_eta(rows[:2], {}, power)["status"], "power_estimate")
        eta = Weather.estimate_eta(rows[:3], {}, power)
        self.assertEqual(eta["status"], "history_prior")
        self.assertIn("thin samples", eta["interval_basis"])
        paces = [r["moving_time"] / (r["distance"] / 1000) for r in rows[:3]]
        self.assertEqual(eta["pace_range_s_per_km"], [min(paces) * .9, max(paces) * 1.1])

    def test_ascent_model_hilly_route_takes_longer_than_flat(self):
        eta = Weather.estimate_eta(synthetic_history(ascent=True, paired=True, spread=.08), {})
        self.assertEqual(eta["method"], "personal_ascent_pace_v1")
        flat = Weather.moving_window(eta, 90, ascent_m=200)
        hilly = Weather.moving_window(eta, 90, ascent_m=1700)
        self.assertGreater(hilly[2], flat[2] * 1.3)
        self.assertLess((hilly[1] - hilly[0]) / hilly[2], .25)

    def test_uniform_device_ascent_bias_cancels_with_paired_source_ratio(self):
        first = Weather.estimate_eta(synthetic_history(ascent=True, paired=True, bias=1.2), {})
        second = Weather.estimate_eta(synthetic_history(ascent=True, paired=True, bias=1.8), {})
        for a, b in zip(Weather.moving_window(first, 90, ascent_m=1000), Weather.moving_window(second, 90, ascent_m=1000)):
            self.assertAlmostEqual(a, b, places=7)
        self.assertEqual(first["ascent_model"]["source_bias_status"], "paired_producer_ratio")
        self.assertGreater(first["ascent_model"]["device_over_route_ascent_range"][0], 1)

    def test_unpaired_source_allowance_is_labelled_and_widens_hilly_route(self):
        eta = Weather.estimate_eta(synthetic_history(ascent=True, spread=.08), {})
        self.assertEqual(eta["ascent_model"]["device_over_route_ascent_range"], [1, 1.5])
        self.assertEqual(eta["ascent_model"]["source_bias_status"], "assumed_1_to_1_5")
        route = Weather.moving_window(eta, 90, ascent_m=1200)
        same_source = Weather.moving_window(eta, 90, ascent_m=1200, ascent_source="recorded")
        self.assertGreater(route[1] - route[0], same_source[1] - same_source[0])

    def test_no_ascent_or_no_supported_relationship_uses_observed_pace(self):
        rows = synthetic_history()
        for i, row in enumerate(rows):
            row["recorded_ascent_m"] = 500  # no demonstrated pace/ascent fit
        self.assertEqual(Weather.estimate_eta(rows, {})["status"], "history_prior")
        eta = Weather.estimate_eta(synthetic_history(ascent=True), {})
        low, high, center = Weather.moving_window(eta, 90)
        self.assertEqual([low, high], [p * 90 for p in eta["pace_range_s_per_km"]])

    def test_historical_stops_replace_default_without_double_counting(self):
        rows = synthetic_history()
        eta = Weather.estimate_eta(rows, {})
        terrain = {"polyline": [[0, 55, 12, 0], [90, 55.8, 12, 0]], "climbs": [], "descents": []}
        moving = Weather.moving_window(eta, 90)
        stop_low, stop_high = eta["stops"]["ratio_range"]
        Weather.sections(terrain, eta, {"at_utc": NOW.isoformat()})
        self.assertEqual(eta["duration_range_s"], [math.floor(moving[0] * stop_low / 300) * 300,
                                                  math.ceil(moving[1] * stop_high / 300) * 300])
        self.assertLess(stop_high, 1.04)
        self.assertEqual(eta["moving_duration_range_s"], [math.floor(moving[0] / 300) * 300, math.ceil(moving[1] / 300) * 300])

    def test_stop_missing_invalid_and_thin_are_explicit(self):
        rows = synthetic_history(3)
        for row in rows:
            row.pop("elapsed_time")
        self.assertEqual(Weather.stop_model(rows)["ratio_range"], [1, 1.25])
        rows[0]["elapsed_time"] = rows[0]["moving_time"] * 1.03
        rows[1]["elapsed_time"] = rows[1]["moving_time"] * .9
        rows[2]["elapsed_time"] = rows[2]["moving_time"] * 5
        result = Weather.stop_model(rows)
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["status"], "history_thin")
        self.assertEqual(result["ratio_range"], [1.03, 1.03])

    def test_record_migrates_optional_facts_without_discarding_old_rides(self):
        with tempfile.TemporaryDirectory() as folder:
            sync = mod.IntervalsSync.__new__(mod.IntervalsSync)
            sync.data_dir, sync.athlete_id = Path(folder), "synthetic"
            old = synthetic_history()
            for row in old:
                row.pop("elapsed_time")
            Weather.update_history(sync, old, {}, now=NOW)
            updated = copy.deepcopy(old[0])
            updated.update(elapsed_time=updated["moving_time"] * 1.04, total_elevation_gain=800)
            previous = {updated["id"]: {"terrain_summary": {"total_distance_km": updated["distance"] / 1000,
                                                            "total_elevation_m": 640, "climbs": [], "descents": []}}}
            result = Weather.update_history(sync, [updated], previous, now=NOW)
            self.assertEqual(len(result["activities"]), 60)
            row = next(r for r in result["activities"] if r["id"] == updated["id"])
            self.assertEqual(row["recorded_ascent_m"], 800)
            self.assertEqual(row["producer_ascent_m"], 640)
            self.assertEqual(row["elapsed_time"], updated["elapsed_time"])
            self.assertEqual(result["version"], 1)

    def test_section_total_matches_ascent_prediction_and_preserves_terrain(self):
        eta = Weather.estimate_eta(synthetic_history(ascent=True, paired=True), {})
        terrain = {"total_elevation_m": 800, "polyline": [[0, 55, 12, 0], [45, 55.4, 12, 800], [90, 55.8, 12, 0]],
                   "climbs": [{"position_km": 0, "distance_km": 45}], "descents": [{"position_km": 45, "distance_km": 45}]}
        before = copy.deepcopy(terrain)
        expected = Weather.moving_window(eta, 90, ascent_m=800)
        Weather.sections(terrain, eta, {"at_utc": NOW.isoformat()})
        self.assertEqual(terrain, before)
        self.assertEqual(eta["moving_duration_range_s"], [math.floor(expected[0] / 300) * 300, math.ceil(expected[1] / 300) * 300])

    def test_loo_observed_pace_coverage_and_width(self):
        record = {"activities": synthetic_history()}
        result = Weather.evaluate_eta(record, NOW)
        self.assertEqual(result["evaluated_rides"], 60)
        self.assertGreaterEqual(result["coverage"], .70)
        self.assertLessEqual(result["coverage"], .85)
        self.assertLess(result["median_relative_window_width"], .35)
        self.assertFalse(result["includes_stops"])
        self.assertNotIn("synthetic-", json.dumps(result))

    def test_loo_ascent_coverage_and_bias_handling(self):
        result = Weather.evaluate_eta({"activities": synthetic_history(ascent=True, paired=True, spread=.08)}, NOW)
        self.assertEqual(result["methods"], {"personal_ascent_pace_v1": 60})
        self.assertGreaterEqual(result["coverage"], .70)
        self.assertLessEqual(result["coverage"], .85)
        self.assertLess(result["median_relative_window_width"], .25)
        self.assertEqual(result["held_out_ascent_sources"], {"route": 60})

    def test_loo_excludes_held_out_identity_and_does_not_mutate_record(self):
        record = {"activities": synthetic_history(15)}
        before = copy.deepcopy(record)
        seen = []
        original = Weather.estimate_eta
        def inspect(rows, lookup):
            seen.append({r["id"] for r in rows})
            return original(rows, lookup)
        with mock.patch.object(Weather, "estimate_eta", side_effect=inspect):
            Weather.evaluate_eta(record, NOW)
        self.assertEqual(record, before)
        for row, training_ids in zip(record["activities"], seen):
            self.assertNotIn(row["id"], training_ids)
            self.assertEqual(len(training_ids), 14)

    def test_loo_filters_old_indoor_and_reports_insufficient_data(self):
        rows = synthetic_history(5)
        rows[0]["trainer"] = True
        rows[1]["start_date_local"] = (NOW - timedelta(days=181)).date().isoformat()
        result = Weather.evaluate_eta({"activities": rows}, NOW)
        self.assertEqual(result["eligible_rides"], 3)
        self.assertEqual(result["evaluated_rides"], 0)
        self.assertIsNone(result["coverage"])

    def test_evaluation_entrypoint_is_read_only_and_preserves_http_guard(self):
        tool = load_module_by_path("eta_eval_tool", Path(__file__).resolve().parents[1] / "evaluate_route_eta.py")
        with tempfile.TemporaryDirectory() as folder:
            record = Path(folder) / "route_eta_history.json"
            record.write_text(json.dumps({"activities": synthetic_history()}))
            before = record.read_bytes()
            get = mod.requests.get
            result = tool.evaluate(record, NOW)
            self.assertEqual(result["evaluated_rides"], 60)
            self.assertEqual(record.read_bytes(), before)
            self.assertIs(mod.requests.get, get)


class SectionPhysicsTests(unittest.TestCase):
    def test_gradients_elevation_and_long_climb_change_speed(self):
        context = Weather.physics_context(POWER_INPUTS)
        flat = Weather.physics_speed(context, 190, 0, 0)
        climb = Weather.physics_speed(context, 190, .05, 0)
        steep = Weather.physics_speed(context, 190, .10, 0)
        self.assertGreater(flat, climb * 1.5)
        self.assertGreater(climb, steep)
        self.assertGreater(Weather.physics_speed(context, 190, 0, 2000), flat)
        self.assertLess(Weather.physics_speed(context, 190, .05, 0, True), climb)

    def test_steep_descent_coasting_already_reaches_cap(self):
        context = Weather.physics_context(POWER_INPUTS)
        self.assertEqual(Weather.physics_speed(context, 0, -.2, 0), 50)
        self.assertEqual(Weather.physics_speed(context, 40, -.2, 0), 50)
        self.assertEqual(Weather.physics_speed(context, 500, -.2, 0), 50)

    def test_shallow_descent_is_pedalled_at_flat_target_power(self):
        context = Weather.physics_context(POWER_INPUTS)
        flat = Weather.physics_speed(context, 190, 0, 0)
        coast = Weather.physics_speed(context, 0, -.014, 0)
        pedalled = Weather.physics_speed(context, 190, -.014, 0)
        assumptions = {**context["assumptions"], "air_density_kg_m3": 1.225}
        expected = Weather.steady_speed(190, 85, -.014, assumptions)
        self.assertAlmostEqual(pedalled, expected)
        self.assertGreater(pedalled, coast * 1.3)
        self.assertGreater(pedalled, flat)
        self.assertLess(pedalled, 50)

    def test_real_polyline_consistency_agrees_with_pace_within_five_percent(self):
        rows, terrain = rolling_history(), rolling_terrain()
        eta = Weather.prepare_eta(rows, POWER_INPUTS)
        before = copy.deepcopy(terrain)
        pace_center = Weather.moving_window(Weather.pace_model(rows), 90)[2]
        eta["physics_comparison"]["safeguard_passed"] = False
        Weather.sections(terrain, eta, {"at_utc": NOW.isoformat()})
        self.assertFalse(eta["terrain_extrapolation"])
        self.assertEqual(eta["method"], "outdoor_moving_pace_quantiles_v2")
        self.assertEqual(eta["route_consistency"]["status"], "local_in_range")
        self.assertLess(abs(eta["physics_adjusted_moving_duration_central_s"] / pace_center - 1), .05)
        self.assertEqual(eta["moving_duration_central_s"], round(pace_center))
        self.assertEqual(terrain, before)

    def test_consistency_is_bounded_shrunk_and_respects_descent_cap(self):
        eta = Weather.prepare_eta(rolling_history(), POWER_INPUTS)
        base = sum(p[0] for p in Weather.route_physics_parts(rolling_terrain(), eta))
        for multiplier, bound in ((.1, .75), (10, 1.25)):
            changed = {**eta, "median_pace_s_per_km": base / 90 * multiplier, "sample_count": 4}
            correction = Weather.route_consistency(changed, rolling_terrain(), base)
            self.assertEqual(correction["bounded_ratio"], bound)
            self.assertAlmostEqual(correction["factor"], 1 + .2 * (bound - 1))
        mountain = route_terrain(1600)
        eta["route_consistency_reference"] = {"factor": .75, "reference_count": 1, "source": "current_routes"}
        eta["physics_model"]["time_factor"] = .7
        legs, _ = Weather.sections(mountain, eta, {"at_utc": NOW.isoformat()})
        self.assertEqual(eta["route_consistency"]["factor"], .875)
        # The 15 km steep descending leg cannot take less than 18 minutes,
        # allowing the existing outward five-minute rounding at both endpoints.
        descent = legs[1]
        fastest = Weather.parse_time(descent["end_window_utc"][0]) - Weather.parse_time(descent["arrival_window_utc"][0])
        self.assertGreaterEqual(fastest, 15 / 50 * 3600 - 300)

    def test_consistency_reference_is_order_independent_and_not_self_trained_on_mountains(self):
        eta = Weather.prepare_eta(rolling_history(), POWER_INPUTS)
        mountain, rolling = route_terrain(1600), rolling_terrain()
        now = NOW.timestamp()
        first, saved = Weather.prepare_route_consistency(eta, [mountain, rolling, rolling], now)
        second, _ = Weather.prepare_route_consistency(eta, [rolling, mountain], now)
        self.assertEqual(first, second)
        self.assertEqual(first["reference_count"], 1)
        self.assertEqual(Weather.prepare_route_consistency(eta, [mountain], now), (None, None))
        corrected = copy.deepcopy(eta)
        corrected["route_consistency_reference"] = first
        Weather.sections(mountain, corrected, {"at_utc": NOW.isoformat()})
        self.assertEqual(corrected["route_consistency"]["status"], "transferred_in_range_reference")
        self.assertAlmostEqual(corrected["route_consistency"]["factor"], 1 + .5 * (first["factor"] - 1))

    def test_reference_cache_expiry_model_owner_and_ftp_binding(self):
        eta = Weather.prepare_eta(rolling_history(), POWER_INPUTS)
        now = NOW.timestamp()
        first, saved = Weather.prepare_route_consistency(eta, [rolling_terrain()], now, owner_key="a")
        reused, _ = Weather.prepare_route_consistency(eta, [], now + 60, saved, owner_key="a")
        self.assertEqual(reused["factor"], first["factor"])
        self.assertEqual(reused["source"], "cached_routes")
        self.assertEqual(Weather.prepare_route_consistency(eta, [], now + Weather.ROUTE_CONSISTENCY_MAX_AGE_SECONDS, saved, "a"), (None, None))
        self.assertEqual(Weather.prepare_route_consistency(eta, [], now + 60, saved, "b"), (None, None))
        changed = Weather.prepare_eta(rolling_history(), {**POWER_INPUTS, "body_weight_kg": 85})
        self.assertEqual(Weather.prepare_route_consistency(changed, [], now + 60, saved, "a"), (None, None))
        low_ftp = Weather.prepare_eta(rolling_history(), {**POWER_INPUTS, "ftp_w": 212.5})
        self.assertIsNotNone(Weather.prepare_route_consistency(low_ftp, [], now + 60, saved, "a")[0])
        corrupted = {**saved, "factor": float("nan")}
        self.assertEqual(Weather.prepare_route_consistency(eta, [], now + 60, corrupted, "a"), (None, None))

    def test_generate_transfers_familiar_geometry_before_event_order_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as folder:
            sync = mod.IntervalsSync.__new__(mod.IntervalsSync)
            sync.data_dir, sync.athlete_id = Path(folder), "synthetic"
            rows = rolling_history()
            sync._update_route_eta_history = mock.Mock(return_value={"activities": rows, "owner_key": "synthetic"})
            events = [{"id": i, "type": "Ride", "category": "WORKOUT", "start_date_local": "2026-10-20T10:00:00+00:00"}
                      for i in (1, 2)]
            entries = [{"event_id": i, "route_status": "ok", "terrain_summary": terrain, "route_ref": {}}
                       for i, terrain in ((1, route_terrain(1600)), (2, rolling_terrain()))]
            def brief(row, event, athlete, calibration, departure=None):
                eta = copy.deepcopy(calibration)
                Weather.sections(row["terrain_summary"], eta, departure or {"at_utc": NOW.isoformat()})
                return {"status": "unavailable", "eta": eta}
            sync._routes_data = {"events": entries}
            with mock.patch.object(Weather, "brief", side_effect=brief):
                Weather(sync, NOW).generate(events, [], {}, {}, POWER_INPUTS)
                first = entries[0]["route_weather"]["eta"]["route_consistency"]
                self.assertEqual(first["status"], "transferred_in_range_reference")
                (sync.data_dir / sync.ROUTES_FILE).write_text(json.dumps(sync._routes_data))
                sync._routes_data = {"events": [entries[0]]}
                Weather(sync, NOW + timedelta(seconds=60)).generate(events[:1], [], {}, {}, POWER_INPUTS)
                second = entries[0]["route_weather"]["eta"]["route_consistency"]
            self.assertEqual(first["factor"], second["factor"])
            self.assertEqual(second["reference_source"], "cached_routes")
            self.assertEqual(sync._routes_data["route_weather_requests_this_sync"], 0)

    def test_hilly_route_slower_than_flat_and_terrain_preserved(self):
        terrain = route_terrain(1600)
        before = copy.deepcopy(terrain)
        flat, hill = Weather.prepare_eta([], POWER_INPUTS), Weather.prepare_eta([], POWER_INPUTS)
        Weather.sections(route_terrain(), flat, {"at_utc": NOW.isoformat()})
        Weather.sections(terrain, hill, {"at_utc": NOW.isoformat()})
        self.assertGreater(hill["moving_duration_central_s"], flat["moving_duration_central_s"] * 1.3)
        self.assertEqual(terrain, before)
        self.assertEqual(hill["method"], "ftp_zones_physics_v1")
        self.assertEqual(hill["physics_model"]["residual_ratio_range"], [.7, 1.4])

    def test_shrinkage_and_factor_bounds(self):
        context = Weather.physics_context(POWER_INPUTS)
        rows = Weather.physics_rows(powered_history(), context)
        few = Weather.fit_physics(rows[:2], context)
        many = Weather.fit_physics(rows, context)
        self.assertEqual(few["history_weight"], .1)
        self.assertEqual(many["history_weight"], 1)
        self.assertLess(few["flat_power_w"], many["flat_power_w"])
        for row in rows:
            row["_own_power_ratio"] = 10
            row["_power"] = 600
        bounded = Weather.fit_physics(rows, context)
        self.assertEqual(bounded["time_factor"], 1.5)
        self.assertEqual(bounded["flat_power_w"], 500)

    def test_low_main_ftp_cancels_with_sufficient_observed_power(self):
        rows = powered_history()
        first = Weather.prepare_eta(rows, POWER_INPUTS)
        second = Weather.prepare_eta(rows, {**POWER_INPUTS, "ftp_w": 250 * .85})
        for eta in (first, second):
            Weather.sections(route_terrain(1600), eta, {"at_utc": NOW.isoformat()})
            self.assertFalse(eta["model_inputs"]["ftp_used"])
        self.assertEqual(first["moving_duration_range_s"], second["moving_duration_range_s"])
        self.assertEqual(first["moving_duration_central_s"], second["moving_duration_central_s"])
        self.assertNotEqual(first["ftp_consistency"], second["ftp_consistency"])

    def test_main_ftp_only_and_missing_ftp_uses_labelled_fallback(self):
        cycling = {"ftp_indoor": 300, "eftp": 310, "eFTP": 320}
        inputs = Weather.eta_inputs(cycling, {"weight": 75}, {"icu_ftp": 330, "icu_eftp": 340})
        self.assertIsNone(inputs["ftp_w"])
        eta = Weather.prepare_eta(powered_history(), inputs)
        Weather.sections(route_terrain(1600), eta, {"at_utc": NOW.isoformat()})
        self.assertEqual(eta["method"], "pace_plus_excess_ascent_v1")
        self.assertTrue(eta["terrain_extrapolation"])
        self.assertEqual(eta["climbing_allowance_s_per_100m"], [180, 360])
        self.assertFalse(eta["model_inputs"]["ftp_used"])
        self.assertEqual(Weather.eta_inputs({**cycling, "ftp": 250}, {}, {})["ftp_w"], 250)

    def test_zone_settings_drop_out_of_fully_observed_power_model(self):
        rows = powered_history()
        first = Weather.prepare_eta(rows, POWER_INPUTS)
        second = Weather.prepare_eta(rows, {**POWER_INPUTS, "power_zone_upper_pct": [60, 80, 100]})
        for eta in (first, second):
            Weather.sections(route_terrain(1600), eta, {"at_utc": NOW.isoformat()})
        self.assertEqual(first["moving_duration_central_s"], second["moving_duration_central_s"])
        self.assertEqual(first["model_inputs"]["target_power_w"], second["model_inputs"]["target_power_w"])

    def test_zone_boundaries_used_without_indoor_substitution(self):
        athlete = {"sportSettings": [{"types": ["Ride"], "ftp": 250, "power_zones": [60, 80, 95, 105]},
                                     {"types": ["VirtualRide"], "ftp": 250, "power_zones": [50, 70, 85]}]}
        inputs = Weather.eta_inputs({"ftp": 250}, {"weight": 75}, athlete)
        context = Weather.physics_context(inputs)
        self.assertEqual(context["prior_intensities"], {"flat": .70, "long_climb": .875, "short_climb": .95})
        self.assertIn("sportSettings", context["zone_source"])
        inputs["power_zone_upper_pct"] = [60, 55, 95]
        self.assertEqual(Weather.physics_context(inputs)["prior_intensities"]["flat"], .65)

    def test_safeguard_retains_exact_v6_pace_when_failed(self):
        rows = powered_history()
        eta = Weather.prepare_eta(rows, POWER_INPUTS)
        eta["physics_comparison"]["safeguard_passed"] = False
        expected = Weather.estimate_eta(rows, {})
        terrain = route_terrain()
        Weather.sections(terrain, eta, {"at_utc": NOW.isoformat()})
        Weather.sections(terrain, expected, {"at_utc": NOW.isoformat()})
        self.assertEqual(eta["method"], "outdoor_moving_pace_quantiles_v2")
        self.assertEqual(eta["duration_range_s"], expected["duration_range_s"])
        self.assertEqual(eta["moving_duration_range_s"], expected["moving_duration_range_s"])
        self.assertEqual(eta["moving_duration_central_s"], expected["moving_duration_central_s"])
        self.assertIn("physics_base_moving_duration_central_s", eta)

    def test_safeguard_selects_physics_in_range_only_when_passed(self):
        eta = Weather.prepare_eta(powered_history(), POWER_INPUTS)
        eta["physics_comparison"]["safeguard_passed"] = True
        Weather.sections(route_terrain(), eta, {"at_utc": NOW.isoformat()})
        self.assertEqual(eta["method"], "ride_power_physics_v1")
        self.assertFalse(eta["terrain_extrapolation"])
        self.assertEqual(eta["selection_reason"], "safeguard_passed")

    def test_safeguard_requires_comparable_width_coverage_and_complete_record(self):
        context = Weather.physics_context(POWER_INPUTS)
        rows = Weather.physics_rows(powered_history(hilly=True), context)
        result = Weather.compare_physics(rows, context)
        pace, physics = result["methods"]["pace"], result["methods"]["ride_power_physics"]
        expected = (physics["coverage"] >= pace["coverage"]
                    and physics["median_window_width_s"] <= pace["median_window_width_s"] * 1.1
                    and physics["median_relative_window_width"] <= pace["median_relative_window_width"] * 1.1)
        self.assertEqual(result["safeguard_passed"], expected)
        incomplete = Weather.compare_physics(rows, context, len(rows) + 1)
        self.assertFalse(incomplete["safeguard_passed"])
        self.assertFalse(Weather.compare_physics(rows[:8], context)["safeguard_passed"])

    def test_no_weight_or_no_history_fallbacks(self):
        eta = Weather.prepare_eta(powered_history(), {"ftp_w": 250})
        Weather.sections(route_terrain(1600), eta, {"at_utc": NOW.isoformat()})
        self.assertEqual(eta["method"], "pace_plus_excess_ascent_v1")
        nothing = Weather.prepare_eta([], {})
        Weather.sections(route_terrain(), nothing, {"at_utc": NOW.isoformat()})
        self.assertEqual(nothing["method"], "generic_default_v2")
        self.assertEqual(nothing["status"], "default")

    def test_power_without_ascent_still_anchors_intensity(self):
        rows = powered_history()
        for row in rows:
            row.pop("producer_ascent_m")
        eta = Weather.prepare_eta(rows, POWER_INPUTS)
        self.assertEqual(eta["physics_model"]["power_sample_count"], len(rows))
        self.assertEqual(eta["physics_model"]["sample_count"], 0)
        self.assertEqual(eta["physics_model"]["time_factor"], 1)
        self.assertEqual(eta["physics_model"]["flat_power_w"], 199)
        self.assertFalse(eta["physics_comparison"]["safeguard_passed"])

    def test_ftp_diagnostic_is_aggregate_and_does_not_change_settings(self):
        inputs = {**POWER_INPUTS, "ftp_w": 190}
        eta = Weather.prepare_eta(powered_history(), inputs)
        self.assertEqual(eta["ftp_consistency"]["status"], "likely_stale_ftp_or_zones")
        self.assertEqual(inputs["ftp_w"], 190)
        self.assertEqual(eta["ftp_consistency"]["sample_count"], 30)

    def test_history_power_migration_availability_and_invalid_removal(self):
        with tempfile.TemporaryDirectory() as folder:
            sync = mod.IntervalsSync.__new__(mod.IntervalsSync)
            sync.data_dir, sync.athlete_id = Path(folder), "synthetic"
            row = synthetic_history(1)[0]
            row.update(average_watts=190, weighted_average_watts=220)
            first = Weather.update_history(sync, [row], {}, now=NOW)["activities"][0]
            self.assertEqual(first["average_power_w"], 190)
            self.assertEqual(first["weighted_power_w"], 220)
            self.assertEqual(first["power_availability"], "average")
            row.pop("average_watts")
            self.assertEqual(Weather.update_history(sync, [row], {}, now=NOW)["activities"][0]["average_power_w"], 190)
            row["average_watts"] = float("nan")
            changed = Weather.update_history(sync, [row], {}, now=NOW)["activities"][0]
            self.assertNotIn("average_power_w", changed)
            self.assertEqual(changed["power_availability"], "weighted_proxy")

    def test_nested_loo_excludes_held_power_and_time_from_fit_and_band(self):
        context = Weather.physics_context(POWER_INPUTS)
        rows = Weather.physics_rows(powered_history(14), context)
        seen = []
        original = Weather.physics_residuals
        def residuals(training, ctx):
            seen.append({r["id"] for r in training})
            return original(training, ctx)
        with mock.patch.object(Weather, "physics_residuals", side_effect=residuals):
            Weather.compare_physics(rows, context)
        for held, training_ids in zip(rows, seen):
            self.assertNotIn(held["id"], training_ids)
        training = rows[1:]
        fit = Weather.fit_physics(training, context)
        prediction = Weather.physics_prediction(rows[0], context, fit)
        altered = {**rows[0], "_power": 500, "moving_time": 999999, "elapsed_time": 9999999}
        self.assertEqual(prediction, Weather.physics_prediction(altered, context, fit))

    def test_evaluator_three_methods_same_rides_no_identifiers(self):
        record = {"activities": powered_history(15)}
        before = copy.deepcopy(record)
        result = Weather.evaluate_eta(record, NOW, POWER_INPUTS)
        comparison = result["physics_comparison"]
        self.assertEqual(set(comparison["methods"]), {"pace", "ftp_zones_physics", "ride_power_physics"})
        self.assertTrue(all(m["evaluated_rides"] == 15 for m in comparison["methods"].values()))
        self.assertEqual(record, before)
        self.assertNotIn("synthetic-", json.dumps(result))
