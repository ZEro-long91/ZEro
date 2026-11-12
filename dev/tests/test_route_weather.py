"""Synthetic provider contracts from official docs staged 2026-10-19, not live captures.

Open-Meteo /en/docs: hourly arrays, comma-separated locations, explicit units,
unixtime, preceding-hour precipitation/gusts. MET Locationforecast 2.0 swagger,
datamodel and HOWTO: GeoJSON properties.meta/timeseries, instant and next_1_hours,
Expires/Last-Modified/304. Unknown and absent fields are exercised deliberately.
"""
import copy
import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from _harness import SYNC_PATH, NetworkBlocked, install_verb_guard, load_module_by_path, restore_verb_guard
from test_route_validity import route_bytes, event

mod = load_module_by_path("s11_sync_route_weather", SYNC_PATH)
Weather = mod.RouteWeather
NOW = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
BASE = int(NOW.replace(hour=0).timestamp())
_originals = None


def setUpModule():
    global _originals
    _originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, _originals)


def reply(payload=None, status=200, headers=None):
    result = mock.Mock(status_code=status, headers=headers or {})
    result.json.return_value = payload
    result.iter_content.side_effect = lambda **kwargs: [json.dumps(result.json.return_value).encode()]
    return result


def om_point(lat=55, lon=12):
    return {"latitude": lat, "longitude": lon, "utc_offset_seconds": 0,
            "hourly_units": {"time": "unixtime", "temperature_2m": "°C", "apparent_temperature": "°C",
                             "wind_speed_10m": "m/s", "wind_direction_10m": "°", "wind_gusts_10m": "m/s",
                             "precipitation_probability": "%", "precipitation": "mm"},
            "hourly": {"time": [BASE + i * 3600 for i in range(192)],
                       "temperature_2m": [0] * 192, "apparent_temperature": [-2] * 192,
                       "wind_speed_10m": [5] * 192, "wind_direction_10m": [0] * 192,
                       "wind_gusts_10m": [9] * 192, "precipitation_probability": [40] * 192,
                       "precipitation": [0.5] * 192}, "unknown_future_field": {"ignored": True}}


def met_point():
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [12, 55, 10]},
            "properties": {"meta": {"updated_at": NOW.isoformat(),
                                    "units": {"air_temperature": "celsius", "wind_speed": "m/s",
                                              "wind_from_direction": "degrees", "wind_speed_of_gust": "m/s",
                                              "precipitation_amount": "mm", "probability_of_precipitation": "%"}},
                           "timeseries": [{"time": Weather.iso(BASE + i * 3600),
                                           "data": {"instant": {"details": {"air_temperature": 0, "wind_speed": 5,
                                                                           "wind_from_direction": 0, "wind_speed_of_gust": 9}},
                                                    "next_1_hours": {"details": {"precipitation_amount": 0.5,
                                                                                 "probability_of_precipitation": 40},
                                                                     "summary": {"symbol_code": "rain"}}}}
                                          for i in range(192)]}}


def success(url, params, **kwargs):
    if url == Weather.OM_URL:
        rows = [om_point(float(lat), float(lon)) for lat, lon in
                zip(params["latitude"].split(","), params["longitude"].split(","))]
        return reply(rows if len(rows) > 1 else rows[0])
    return reply(met_point())


def window_success(url, params, **kwargs):
    """Return only the requested hours, so fixtures cannot hide cache window gaps."""
    result = success(url, params, **kwargs)
    if url == Weather.OM_URL:
        start = Weather.parse_time(params["start_hour"] + ":00+00:00")
        end = Weather.parse_time(params["end_hour"] + ":00+00:00")
        payload = result.json.return_value
        for row in payload if isinstance(payload, list) else [payload]:
            indices = [i for i, stamp in enumerate(row["hourly"]["time"]) if start <= stamp <= end]
            row["hourly"] = {key: [values[i] for i in indices] for key, values in row["hourly"].items()}
    return result


def history():
    activities, lookup = [], {}
    # Independent mixtures with hand-defined class paces: 180, 360, 120 s/km.
    for i, distances in enumerate(([30, 0, 0], [20, 10, 0], [20, 0, 10],
                                    [30, 5, 2], [15, 3, 12], [22, 8, 7],
                                    [35, 4, 1], [25, 2, 8], [12, 11, 3])):
        flat, climb, descent = distances
        activities.append({"id": str(i), "type": "Ride", "moving_time": flat * 180 + climb * 360 + descent * 120,
                           "distance": sum(distances) * 1000, "start_date_local": "2026-10-18T10:00:00"})
        lookup[str(i)] = {"terrain_summary": {"total_distance_km": sum(distances),
                                              "climbs": [{"distance_km": climb}] if climb else [],
                                              "descents": [{"distance_km": descent}] if descent else []}}
    return activities, lookup


class RouteWeatherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.s = mod.IntervalsSync.__new__(mod.IntervalsSync)
        self.s.data_dir = Path(tmp.name)
        self.s.debug = False
        self.s._cached_script_hash = "synthetic"
        self.s.route_weather_enabled = True
        self.s.athlete_id = "synthetic"
        self.now = NOW
        self.events = [event(start_date_local="2026-10-19T12:00:00+00:00")]
        terrain = self.s._parse_route_file(route_bytes(), "route.gpx")["terrain_summary"]
        self.row = {"event_id": 7, "route_status": "ok", "terrain_summary": terrain,
                    "route_ref": {"route_id": "intervals:7:attachment:12", "attachment_id": 12,
                                  "revision": "a" * 64, "terrain_version": "3.142"}}
        self.activities, self.previous = [], {}
        self.athlete = {"timezone": "UTC"}
        self.power_inputs = None

    def run_weather(self, get=None, rows=None):
        entries = copy.deepcopy(rows or [self.row])
        self.s._routes_data = {"events": entries}
        self.s._planned_routes = {r["event_id"]: r for r in entries}
        producer = Weather(self.s, self.now)
        if get:
            with mock.patch.object(mod.requests, "get", side_effect=get) as request:
                producer.generate(self.events, self.activities, self.previous, self.athlete, self.power_inputs)
        else:
            request = None
            producer.generate(self.events, self.activities, self.previous, self.athlete, self.power_inputs)
        return entries[0].get("route_weather"), request

    def persist(self):
        (self.s.data_dir / self.s.ROUTES_FILE).write_text(json.dumps(self.s._routes_data))

    def two_routes(self, same_location=False):
        self.events = [event(7, start_date_local="2026-10-19T17:00:00+00:00"),
                       event(8, start_date_local="2026-10-20T17:00:00+00:00")]
        first, second = copy.deepcopy(self.row), copy.deepcopy(self.row)
        first["terrain_summary"]["climbs"] = []
        first["terrain_summary"]["descents"] = []
        second["event_id"] = 8
        second["route_ref"]["route_id"] = "intervals:8:attachment:13"
        second["route_ref"]["attachment_id"] = 13
        if not same_location:
            for point in second["terrain_summary"]["polyline"]:
                point[2] += 1
        return [first, second]

    def test_two_cold_events_get_distinct_bounded_forecasts(self):
        rows = self.two_routes()
        first, get = self.run_weather(window_success, rows)
        briefs = [r["route_weather"] for r in self.s._routes_data["events"]]
        self.assertEqual([b["status"] for b in briefs], ["ok", "ok"])
        self.assertTrue(all(b["eta"] and b["sections"] for b in briefs))
        self.assertEqual(get.call_count, 2)
        self.assertEqual(self.s._routes_data["route_weather_requests_this_sync"], 2)
        self.assertNotEqual(get.call_args_list[0].kwargs["params"]["start_hour"],
                            get.call_args_list[1].kwargs["params"]["start_hour"])
        self.persist()
        self.now += timedelta(seconds=60)
        self.run_weather(rows=rows)  # guard proves no HTTP for either cache hit
        self.assertEqual([r["route_weather"] for r in self.s._routes_data["events"]], briefs)

    def test_shared_locations_keep_disjoint_event_time_windows(self):
        rows = self.two_routes(same_location=True)
        _, get = self.run_weather(window_success, rows)
        self.assertEqual(get.call_count, 2)
        self.assertEqual([r["route_weather"]["status"] for r in self.s._routes_data["events"]], ["ok", "ok"])
        self.persist()
        self.now += timedelta(seconds=60)
        self.run_weather(rows=rows)
        self.assertEqual(self.s._routes_data["route_weather_requests_this_sync"], 0)
        self.assertEqual([r["route_weather"]["status"] for r in self.s._routes_data["events"]], ["ok", "ok"])

    def test_request_window_encloses_only_event_and_tier_rounding(self):
        for lead in (7.25, 31, 72, 72.25, 120):
            with self.subTest(lead=lead):
                self.events[0]["start_date_local"] = Weather.iso(NOW.timestamp() + lead * 3600)
                brief, get = self.run_weather(window_success)
                params = get.call_args.kwargs["params"]
                start = Weather.parse_time(params["start_hour"] + ":00Z")
                end = Weather.parse_time(params["end_hour"] + ":00Z")
                departure = Weather.parse_time(brief["departure"]["at_utc"])
                upper = departure + brief["eta"]["duration_range_s"][1]
                rounding = 21600 if lead > 72 else 3600
                self.assertEqual(start, math.floor(departure / rounding) * rounding)
                self.assertEqual(end, math.ceil(upper / rounding) * rounding)
                self.assertEqual(brief["status"], "ok")
                self.assertNotIn("forecast_days", params)
                self.assertTrue(get.call_args.kwargs["stream"])
                self.assertEqual(get.call_args.kwargs["timeout"], (2, 5))

    def test_slow_dripping_second_event_times_out_closes_and_retries_after_backoff(self):
        rows = self.two_routes()
        elapsed, calls = [0.0], []
        slow = reply()
        def drip(**kwargs):
            self.assertEqual(kwargs["chunk_size"], 1)
            for byte in b' {        ':
                elapsed[0] += 4  # each chunk arrives within the 5 s read timeout
                yield bytes([byte])
        slow.iter_content.side_effect = drip
        def get(url, **kwargs):
            calls.append(url)
            return window_success(url, **kwargs) if len(calls) == 1 else slow
        with mock.patch.object(mod.time, "monotonic", side_effect=lambda: elapsed[0]):
            first, request = self.run_weather(get, rows)
        second = self.s._routes_data["events"][1]["route_weather"]
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "unavailable")
        self.assertIn("provider_timeout", second["reason_codes"])
        self.assertIn("request_admission_cutoff", second["reason_codes"])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(elapsed[0], 28)  # first returned chunk after the 25 s deadline
        slow.close.assert_called_once()
        errors = [r for r in self.s._routes_data["route_weather_cache"]["points"].values() if r.get("error")]
        self.assertTrue(errors)
        self.assertTrue(all(r["error"] == "provider_timeout" for r in errors))
        self.persist()
        snapshots = copy.deepcopy(self.s._routes_data["brief_snapshots"])
        self.now += timedelta(seconds=899)
        self.assertEqual(self.run_weather(rows=rows)[0], first)
        self.assertIn("provider_timeout", self.s._routes_data["events"][1]["route_weather"]["reason_codes"])
        self.assertEqual(self.s._routes_data["brief_snapshots"], snapshots)
        self.persist()
        self.now += timedelta(seconds=1)
        _, retried = self.run_weather(window_success, rows)
        self.assertEqual(retried.call_count, 1)
        self.assertEqual([r["route_weather"]["status"] for r in self.s._routes_data["events"]], ["ok", "ok"])

    def test_timeout_batch_splits_and_recovers_inside_same_budget(self):
        elapsed, sizes = [0.0], []
        def get(url, params, **kwargs):
            sizes.append(len(params["latitude"].split(",")))
            if len(sizes) == 1:
                elapsed[0] += 5
                raise mod.requests.exceptions.ReadTimeout("synthetic slow batch")
            elapsed[0] += 1
            return window_success(url, params, **kwargs)
        with mock.patch.object(mod.time, "monotonic", side_effect=lambda: elapsed[0]):
            brief, request = self.run_weather(get)
        self.assertEqual(sizes, [6, 3, 3])
        self.assertEqual(brief["status"], "ok")
        self.assertEqual(request.call_count, 3)
        self.assertIn("provider_timeout", brief["reason_codes"])

    def test_split_failures_never_exceed_eight_calls(self):
        def timeout(*args, **kwargs):
            raise mod.requests.exceptions.ReadTimeout("synthetic")
        with mock.patch.object(mod.time, "monotonic", return_value=0):
            brief, get = self.run_weather(timeout)
        self.assertEqual(get.call_count, 8)
        self.assertEqual(self.s._routes_data["route_weather_requests_this_sync"], 8)
        self.assertIn("request_budget_exhausted", brief["reason_codes"])
        self.assertIn("provider_timeout", brief["reason_codes"])

    def test_request_errors_distinguish_timeout_connection_http_and_json(self):
        wrapped = mod.requests.exceptions.ConnectionError(type("ReadTimeoutError", (Exception,), {})("synthetic"))
        for exc, expected in ((mod.requests.exceptions.ConnectTimeout(), "provider_timeout"),
                              (wrapped, "provider_timeout"),
                              (mod.requests.exceptions.ConnectionError(), "connection_error"),
                              (mod.requests.exceptions.RequestException(), "transport_failure")):
            with self.subTest(expected=expected):
                producer = Weather(self.s, self.now)
                with mock.patch.object(mod.requests, "get", side_effect=exc):
                    self.assertEqual(producer.request("open_meteo", Weather.OM_URL, {})["error"], expected)
        for response, expected in ((reply(status=503), "http_503"), (reply(), "invalid_json")):
            if expected == "invalid_json":
                response.iter_content.side_effect = lambda **kwargs: [b'broken']
            producer = Weather(self.s, self.now)
            with mock.patch.object(mod.requests, "get", return_value=response):
                self.assertEqual(producer.request("open_meteo", Weather.OM_URL, {})["error"], expected)
            response.close.assert_called_once()

    def test_legacy_covering_point_cache_is_reused(self):
        self.run_weather(success)
        state = self.s._routes_data["route_weather_cache"]
        state["points"] = {r["location_key"]: {k: v for k, v in r.items() if k != "location_key"}
                           for r in state["points"].values()}
        self.persist()
        self.now += timedelta(seconds=60)
        brief, _ = self.run_weather()
        self.assertEqual(brief["status"], "ok")
        self.assertEqual(self.s._routes_data["route_weather_requests_this_sync"], 0)

    def test_guard_rejects_unmocked_forecast(self):
        with self.assertRaises(NetworkBlocked):
            self.run_weather()

    def test_primary_batch_success_and_exact_binding(self):
        brief, get = self.run_weather(success)
        self.assertEqual(brief["status"], "ok")
        self.assertEqual(get.call_count, 1)
        params = get.call_args.kwargs["params"]
        self.assertEqual(len(params["latitude"].split(",")), 6)
        self.assertEqual(params["wind_speed_unit"], "ms")
        self.assertEqual(params["timeformat"], "unixtime")
        self.assertEqual(brief["event_id"], 7)
        self.assertEqual(brief["route_ref"], self.row["route_ref"])
        self.assertFalse(brief["provenance"]["fallback"])
        self.assertEqual(brief["summary"]["temperature_range_c"], [0, 0])
        self.assertNotIn("auth", get.call_args.kwargs)
        self.assertNotIn("Authorization", get.call_args.kwargs["headers"])

    def test_next_minute_cache_hit_zero_http_and_stable_snapshot(self):
        first, _ = self.run_weather(success)
        self.persist()
        snapshot = copy.deepcopy(self.s._routes_data["brief_snapshots"])
        self.now += timedelta(seconds=60)
        second, _ = self.run_weather()  # active guard, no request mock
        self.assertEqual(second, first)
        self.assertEqual(self.s._routes_data["route_weather_requests_this_sync"], 0)
        self.assertEqual(self.s._routes_data["brief_snapshots"], snapshot)

    def test_success_refresh_waits_at_least_one_hour(self):
        self.run_weather(success)
        self.persist()
        self.now += timedelta(seconds=3599)
        self.run_weather()
        self.persist()
        self.now += timedelta(seconds=302)
        _, get = self.run_weather(success)
        self.assertEqual(get.call_count, 1)

    def test_failed_route_backs_off_then_retries(self):
        brief, get = self.run_weather(lambda *a, **k: reply(status=503))
        self.assertEqual(brief["status"], "unavailable")
        self.assertEqual(get.call_count, 7)
        self.persist()
        self.now += timedelta(seconds=899)
        self.run_weather()
        self.persist()
        self.now += timedelta(seconds=1)
        brief, get = self.run_weather(success)
        self.assertEqual(brief["status"], "ok")
        self.assertEqual(get.call_count, 1)

    def test_failure_preserves_stale_forecast_and_original_fetch_time(self):
        first, _ = self.run_weather(success)
        self.persist()
        self.now += timedelta(seconds=3901)
        brief, _ = self.run_weather(lambda *a, **k: reply(status=503))
        self.assertEqual(brief["status"], "stale")
        self.assertEqual(brief["fetched_at"], first["fetched_at"])
        self.assertEqual(brief["summary"], first["summary"])

    def test_partial_primary_does_not_blend_fallback_fields(self):
        def missing(url, params, **kwargs):
            result = success(url, params, **kwargs)
            for row in result.json.return_value:
                del row["hourly"]["wind_gusts_10m"]
            return result
        brief, get = self.run_weather(missing)
        self.assertEqual(brief["status"], "partial")
        self.assertEqual(get.call_count, 1)
        self.assertIsNone(brief["summary"]["gust_max_mps"])
        self.assertEqual(brief["summary"]["complete_distance_pct"], 0)

    def test_fallback_is_labelled_and_identified(self):
        def fallback(url, params, **kwargs):
            return reply(status=503) if url == Weather.OM_URL else success(url, params, **kwargs)
        brief, get = self.run_weather(fallback)
        self.assertEqual(brief["status"], "ok")
        self.assertTrue(brief["provenance"]["fallback"])
        self.assertEqual(brief["provenance"]["attribution"], "Data from MET Norway")
        self.assertEqual(get.call_count, 7)
        for call in get.call_args_list[1:]:
            self.assertIn("github.com/CrankAddict/section-11", call.kwargs["headers"]["User-Agent"])
            self.assertLessEqual(len(call.kwargs["params"]["lat"].split(".")[1]), 4)

    def test_met_global_missing_gust_probability_stays_partial(self):
        def fallback(url, params, **kwargs):
            if url == Weather.OM_URL:
                return reply(status=503)
            payload = met_point()
            for row in payload["properties"]["timeseries"]:
                del row["data"]["instant"]["details"]["wind_speed_of_gust"]
                del row["data"]["next_1_hours"]["details"]["probability_of_precipitation"]
            return reply(payload)
        brief, _ = self.run_weather(fallback)
        self.assertEqual(brief["status"], "partial")
        self.assertIsNone(brief["summary"]["gust_max_mps"])
        self.assertIsNone(brief["summary"]["precipitation_probability_max_pct"])

    def test_met_expires_and_conditional_304(self):
        expires = "Mon, 19 Oct 2026 13:00:00 GMT"
        modified = "Mon, 19 Oct 2026 09:00:00 GMT"
        def fallback(url, params, **kwargs):
            return reply(status=503) if url == Weather.OM_URL else reply(
                met_point(), headers={"Expires": expires, "Last-Modified": modified})
        first, _ = self.run_weather(fallback)
        self.persist()
        self.now += timedelta(seconds=3601)
        _, get = self.run_weather(lambda *a, **k: reply(status=503))
        self.assertEqual(get.call_count, 1)  # primary retry, MET not yet expired
        self.persist()
        self.now = NOW + timedelta(hours=3, seconds=1)
        self.events[0]["start_date_local"] = "2026-10-19T15:00:00+00:00"
        def not_modified(url, params, **kwargs):
            if url == Weather.OM_URL:
                return reply(status=503)
            self.assertEqual(kwargs["headers"]["If-Modified-Since"], modified)
            return reply(status=304, headers={"Expires": "Mon, 19 Oct 2026 15:00:00 GMT"})
        brief, get = self.run_weather(not_modified)
        self.assertEqual(get.call_count, 7)
        self.assertEqual(brief["fetched_at"], first["fetched_at"])
        self.assertEqual(brief["status"], "ok")

    def test_429_retry_after_stops_sibling_provider_reads(self):
        brief, get = self.run_weather(lambda *a, **k: reply(status=429, headers={"Retry-After": "7200"}))
        self.assertEqual(get.call_count, 2)  # one call per provider, then blocked
        self.persist()
        self.now += timedelta(seconds=1000)
        self.run_weather()  # global cycle due, provider cooldowns still prohibit HTTP

    def test_changed_departure_and_revision_rebind_without_refetch(self):
        first, _ = self.run_weather(success)
        self.persist()
        self.row["route_ref"]["revision"] = "b" * 64
        self.events[0]["start_date_local"] = "2026-10-19T13:00:00+00:00"
        second, _ = self.run_weather()
        self.assertNotEqual(first["cache_key"], second["cache_key"])
        self.assertNotEqual(first["brief_id"], second["brief_id"])
        self.assertEqual(second["route_ref"]["revision"], "b" * 64)
        self.assertEqual(len(self.s._routes_data["brief_snapshots"]), 2)

    def test_snapshot_bytes_survive_refresh_and_event_removal(self):
        first, _ = self.run_weather(success)
        self.persist()
        frozen = json.dumps(self.s._routes_data["brief_snapshots"][0], sort_keys=True)
        self.now += timedelta(seconds=3901)
        self.run_weather(success)
        self.assertEqual(json.dumps(self.s._routes_data["brief_snapshots"][0], sort_keys=True), frozen)
        self.persist()
        self.s._routes_data = {"events": []}
        Weather(self.s, self.now + timedelta(days=1)).generate([], [], {}, self.athlete)
        self.assertEqual(self.s._routes_data["brief_snapshots"][0]["brief"]["brief_id"], first["brief_id"])

    def test_missed_local_date_never_fetches_or_creates_snapshot(self):
        self.events[0]["start_date_local"] = "2026-10-18T09:00:00+00:00"
        brief, _ = self.run_weather()
        self.assertEqual(brief["reason_codes"], ["missed"])
        self.assertEqual(self.s._routes_data["brief_snapshots"], [])

    def test_horizon_missing_time_and_missing_zone_are_network_free(self):
        for start, athlete, reason in (
            ("2026-10-24T10:00:01Z", {}, "outside_120_hour_horizon"),
            ("2026-10-19", {}, "missing_timezone"),
            ("2026-10-19T12:00:00", {}, "missing_timezone"),
            ("2026-10-19T12:00:00", {"timezone": "invalid"}, "invalid_timezone")):
            with self.subTest(reason=reason):
                self.events[0]["start_date_local"] = start
                self.athlete = athlete
                self.assertEqual(self.run_weather()[0]["reason_codes"], [reason])

    def test_dst_ambiguous_and_missing_hour_rejected_midnight_assumed(self):
        for raw in ("2026-10-25T02:30:00", "2026-03-29T02:30:00"):
            with self.assertRaisesRegex(ValueError, "ambiguous_or_nonexistent"):
                Weather.departure({"start_date_local": raw}, {"timezone": "Europe/Berlin"})
        result = Weather.departure({"start_date_local": "2026-10-20T00:00:00"}, {"timezone": "UTC"})
        self.assertEqual(result["at_utc"], "2026-10-20T10:00:00+00:00")
        self.assertEqual(result["source"], "assumed")

    def test_disabled_and_indoor_are_network_free(self):
        self.s.route_weather_enabled = False
        self.assertEqual(self.run_weather()[0]["reason_codes"], ["disabled"])
        self.s.route_weather_enabled = True
        self.events[0]["indoor"] = True
        self.assertEqual(self.run_weather()[0]["reason_codes"], ["not_outdoor_cycling"])

    def test_default_on_and_independent_off_switches(self):
        self.assertTrue(Weather.enabled({}, {}))
        del self.s.route_weather_enabled
        brief, get = self.run_weather(success)
        self.assertEqual(brief["status"], "ok")
        self.assertEqual(get.call_count, 1)
        for config, environ in (({"route_weather": False}, {}), ({}, {"SECTION11_ROUTE_WEATHER": "0"}),
                                ({"route_weather": False}, {"SECTION11_ROUTE_WEATHER": "1"}),
                                ({"route_weather": True}, {"SECTION11_ROUTE_WEATHER": "0"})):
            with self.subTest(config=config, environ=environ):
                self.s.route_weather_enabled = Weather.enabled(config, environ)
                self.assertFalse(self.s.route_weather_enabled)
                self.assertEqual(self.run_weather()[0]["reason_codes"], ["disabled"])
                self.assertEqual(self.s._routes_data["route_weather_requests_this_sync"], 0)

    def test_120_hour_boundary_and_outlook_has_no_section_forecast(self):
        self.events[0]["start_date_local"] = Weather.iso(NOW.timestamp() + 120 * 3600)
        brief, get = self.run_weather(success)
        self.assertEqual(get.call_count, 1)
        self.assertNotIn("forecast_days", get.call_args.kwargs["params"])
        self.assertEqual(get.call_args.kwargs["params"]["start_hour"], "2026-10-24T06:00")
        self.assertEqual(brief["status"], "ok")
        self.assertEqual(brief["tier"], "outlook")
        self.assertEqual(brief["sections"], [])
        self.assertIn("uncertain", brief["outlook"]["interpretation"])
        self.assertIn("period_precipitation_max_mm", brief["summary"])
        self.assertNotIn("hourly_precipitation_max_mm", brief["summary"])
        self.assertEqual(brief["outlook"]["wind_from_sectors"], ["N"])
        pointer = self.s._planned_route_fields(self.events[0])["route_weather_summary"]
        self.assertEqual(pointer["tier"], "outlook")

    def test_72_hour_tier_boundary(self):
        self.events[0]["start_date_local"] = Weather.iso(NOW.timestamp() + 72 * 3600)
        brief, _ = self.run_weather(success)
        self.assertEqual(brief["tier"], "forecast")
        self.assertTrue(brief["sections"])
        self.events[0]["start_date_local"] = Weather.iso(NOW.timestamp() + 72 * 3600 + 1)
        brief, _ = self.run_weather(success)
        self.assertEqual(brief["tier"], "outlook")
        self.assertEqual(brief["sections"], [])

    def test_five_day_window_counts_near_hourly_far_six_hourly(self):
        rows = []
        self.events = []
        for day in range(1, 6):
            self.events.append(event(day, start_date_local=Weather.iso(NOW.timestamp() + day * 86400)))
            row = {**copy.deepcopy(self.row), "event_id": day}
            for point in row["terrain_summary"]["polyline"]:
                point[2] += day
            rows.append(row)
        _, get = self.run_weather(success, rows)
        self.assertEqual(get.call_count, 5)
        self.assertEqual([r["route_weather"]["tier"] for r in self.s._routes_data["events"]],
                         ["forecast"] * 3 + ["outlook"] * 2)
        self.persist()
        self.now += timedelta(seconds=60)
        self.run_weather(rows=rows)  # active guard proves zero HTTP
        self.assertEqual(self.s._routes_data["route_weather_requests_this_sync"], 0)
        self.persist()
        self.now = NOW + timedelta(seconds=3901)
        _, get = self.run_weather(success, rows)
        self.assertEqual(get.call_count, 3)
        self.persist()
        self.now = NOW + timedelta(hours=6, seconds=301)
        _, get = self.run_weather(success, rows)
        self.assertEqual(get.call_count, 5)

    def test_outlook_cache_hit_stable_then_near_transition(self):
        self.events[0]["start_date_local"] = Weather.iso(NOW.timestamp() + 73 * 3600)
        first, _ = self.run_weather(success)
        self.persist()
        self.now += timedelta(seconds=60)
        second, _ = self.run_weather()
        self.assertEqual(first, second)
        self.persist()
        self.now = NOW + timedelta(seconds=3901)
        brief, get = self.run_weather(success)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(brief["tier"], "forecast")
        self.assertNotEqual(brief["cache_key"], first["cache_key"])
        self.assertTrue(brief["sections"])

    def test_met_coarse_outlook_preserves_six_hour_amount(self):
        self.events[0]["start_date_local"] = Weather.iso(NOW.timestamp() + 120 * 3600)
        payload = met_point()
        payload["properties"]["timeseries"] = payload["properties"]["timeseries"][::6]
        for row in payload["properties"]["timeseries"]:
            row["data"]["next_6_hours"] = row["data"].pop("next_1_hours")
            row["data"]["next_6_hours"]["details"]["precipitation_amount"] = 3
        brief, get = self.run_weather(lambda url, **kw: reply(status=503) if url == Weather.OM_URL else reply(payload))
        self.assertEqual(get.call_count, 7)
        self.assertEqual(brief["status"], "ok")
        self.assertEqual(brief["tier"], "outlook")
        self.assertTrue(brief["provenance"]["fallback"])
        self.assertEqual(brief["outlook"]["precipitation_period_hours"], [6])
        self.assertEqual(brief["summary"]["period_precipitation_max_mm"], 3)
        self.assertIn("coarse_provider_periods", brief["reason_codes"])
        self.assertEqual(brief["sections"], [])

    def test_coarse_met_near_forecast_does_not_invent_hourly_values(self):
        payload = met_point()
        for row in payload["properties"]["timeseries"]:
            row["data"]["next_6_hours"] = row["data"].pop("next_1_hours")
        brief, _ = self.run_weather(lambda url, **kw: reply(status=503) if url == Weather.OM_URL else reply(payload))
        self.assertEqual(brief["status"], "partial")
        self.assertIsNone(brief["summary"]["hourly_precipitation_max_mm"])

    def test_180_day_rich_terrain_history_survives_cache_clear(self):
        activities, previous = history()
        for i, act in enumerate(activities):
            act["start_date_local"] = (NOW - timedelta(days=45 + i * 15)).isoformat()
        record = self.s._update_route_eta_history(activities, previous, full_history=True, now=NOW)
        self.assertEqual(len(record["activities"]), 9)
        persisted = (self.s.data_dir / Weather.ETA_HISTORY_FILE).read_bytes()
        for name in ("history.json", "intervals.json", "routes.json"):
            (self.s.data_dir / name).write_text("{}")
            (self.s.data_dir / name).unlink()  # simulate the supported updater's exact clear set
        self.assertEqual((self.s.data_dir / Weather.ETA_HISTORY_FILE).read_bytes(), persisted)
        brief, _ = self.run_weather(success)  # no recent rides; winter record still used
        self.assertEqual(brief["eta"]["status"], "pace_with_climbing_allowance")
        self.assertEqual(brief["eta"]["selection_reason"], "missing_or_invalid_main_ftp")
        self.assertEqual(brief["eta"]["sample_count"], 9)
        self.assertEqual(brief["eta"]["window_days"], 180)
        self.assertLess(brief["eta"]["date_range"][1], (NOW - timedelta(days=28)).date().isoformat())

    def test_180_day_no_thin_and_activity_only_history(self):
        self.assertEqual(self.run_weather(success)[0]["eta"]["sample_count"], 0)
        activities, _ = history()
        for i, act in enumerate(activities):
            act["start_date_local"] = (NOW - timedelta(days=60 + i * 10)).isoformat()
        self.s._update_route_eta_history(activities[:2], full_history=True, now=NOW)
        thin, _ = self.run_weather(success)
        self.assertEqual(thin["eta"]["status"], "default")
        self.assertEqual(thin["eta"]["sample_count"], 2)
        self.s._update_route_eta_history(activities, full_history=True, now=NOW)
        rich, _ = self.run_weather(success)
        self.assertEqual(rich["eta"]["status"], "pace_with_climbing_allowance")
        self.assertEqual(rich["eta"]["sample_count"], 9)
        self.assertEqual(rich["eta"]["terrain_sample_count"], 0)
        self.assertIn("not measured", rich["eta"]["uncertainty"])

    def test_record_excludes_old_future_trainer_and_changed_owner(self):
        activities, previous = history()
        activities[0]["start_date_local"] = (NOW - timedelta(days=181)).isoformat()
        activities[1]["start_date_local"] = (NOW + timedelta(days=1)).isoformat()
        activities[2]["trainer"] = True
        activities[3]["type"] = "VirtualRide"
        record = self.s._update_route_eta_history(activities, previous, now=NOW)
        self.assertEqual(len(record["activities"]), 5)
        self.s.athlete_id = "different-synthetic-user"
        self.assertEqual(self.s._update_route_eta_history([], now=NOW)["activities"], [])

    def test_device_ascent_cannot_change_calibration(self):
        activities, previous = history()
        self.s._update_route_eta_history(activities, previous, now=NOW)
        first, _ = self.run_weather(success)
        for act in activities:
            act["total_elevation_gain"] = 99999
        self.s._update_route_eta_history(activities, previous, now=NOW)
        second, _ = self.run_weather(success)
        self.assertEqual(first["eta"], second["eta"])
        text = (self.s.data_dir / Weather.ETA_HISTORY_FILE).read_text()
        self.assertNotIn("99999", text)
        self.assertNotIn("total_elevation_gain", text)

    def test_personal_whole_ride_pace_changes_prior(self):
        activities, _ = history()
        first = Weather.calibrate(activities, {})
        for act in activities:
            act["moving_time"] *= 1.5
        second = Weather.calibrate(activities, {})
        self.assertLess(second["speed_ranges_kmh"]["flat"][1], first["speed_ranges_kmh"]["flat"][1])
        self.assertEqual(second["status"], "history_prior")

    def test_existing_long_history_fetch_rebuilds_record_without_extra_calls(self):
        activities, _ = history()
        for act in activities:
            act["start_date_local"] = (NOW - timedelta(days=100)).isoformat()
        sync = mod.IntervalsSync("synthetic", "unused-test-key")
        sync.data_dir = self.s.data_dir
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
        with mock.patch.object(mod, "datetime", Frozen), mock.patch.object(
                sync, "_intervals_get", side_effect=lambda endpoint, *a: activities if endpoint == "activities" else [] if endpoint == "wellness" else {}) as get:
            sync.generate_history()
        # Route history still adds no calls; v3.149 separately admits one
        # descriptive season-curve request during history generation.
        self.assertEqual([call.args[0] for call in get.call_args_list],
                         ["activities", "wellness", "", "activity-power-curves"])
        self.assertTrue((sync.data_dir / Weather.ETA_HISTORY_FILE).is_file())
        brief, _ = self.run_weather(success)
        self.assertEqual(brief["eta"]["sample_count"], 9)
        self.assertEqual(brief["eta"]["status"], "pace_with_climbing_allowance")

    def test_record_write_failure_is_noncritical_and_reported(self):
        activities, _ = history()
        with mock.patch.object(mod.os, "replace", side_effect=OSError("synthetic write failure")):
            record = self.s._update_route_eta_history(activities, now=NOW)
        self.assertEqual(record["persistence_status"], "persistence_failed")
        self.assertEqual(len(record["activities"]), 9)

    def test_no_forecast_for_non_ok_route_or_calendar_note(self):
        self.row["route_status"] = "no_elevation"
        self.assertIsNone(self.run_weather()[0])
        self.row["route_status"] = "ok"
        self.events[0]["category"] = "NOTE"
        self.assertIsNone(self.run_weather()[0])

    def test_wind_components_boundaries_calm_and_unknown(self):
        self.assertEqual(Weather.wind(5, 0, 0), {"class": "head", "head_mps": 5, "cross_mps": 0})
        self.assertEqual(Weather.wind(5, 0, 180)["head_mps"], -5)
        self.assertEqual(Weather.wind(5, 0, 90)["cross_mps"], 5)
        for angle, kind in ((0, "head"), (45, "head"), (79.999, "head"),
                            (80, "cross"), (80.001, "cross"), (109.999, "cross"),
                            (110, "tail"), (110.001, "tail"), (135, "tail"), (180, "tail")):
            for sign in (-1, 1):
                for bearing in (0, 90, 180, 270, 359):
                    direction = (bearing + sign * angle) % 360
                    for speed in (5, 0.000001):
                        with self.subTest(angle=angle, sign=sign, bearing=bearing, speed=speed):
                            self.assertEqual(Weather.wind(speed, direction, bearing), {
                                "class": kind,
                                "head_mps": round(speed * math.cos(math.radians(angle)), 3),
                                "cross_mps": round(speed * math.sin(math.radians(angle)), 3)})
        self.assertEqual(Weather.wind(5, 360, 0), Weather.wind(5, 0, 360))
        for direction in (None, 0, 360, float("nan")):
            self.assertEqual(Weather.wind(0, direction, 0),
                             {"class": "calm", "head_mps": 0.0, "cross_mps": 0.0})
        unknown = {"class": "unknown", "head_mps": None, "cross_mps": None}
        for speed in (None, -1, True, "5", float("nan"), float("inf")):
            self.assertEqual(Weather.wind(speed, 0, 0), unknown)
        for direction in (None, -1, 361, True, "90", float("nan"), float("inf")):
            self.assertEqual(Weather.wind(5, direction, 0), unknown)

    def test_cached_weather_rebuilds_old_labels_and_preserves_first_snapshot(self):
        def oblique(url, params, **kwargs):
            result = success(url, params, **kwargs)
            payload = result.json.return_value
            for point in payload if isinstance(payload, list) else [payload]:
                point["hourly"]["wind_direction_10m"] = [60] * 192
            return result

        # Simulate the previous release using the same synthetic native records.
        with mock.patch.object(Weather, "HEAD_MAX_DEG", 45), mock.patch.object(Weather, "TAIL_MIN_DEG", 135):
            old, _ = self.run_weather(oblique)
        self.assertEqual(old["status"], "ok")
        self.assertTrue(all(s["weather"]["wind_classes"] == ["cross"] for s in old["sections"]))
        retained = copy.deepcopy(self.s._routes_data["brief_snapshots"][0])
        cached_rows = copy.deepcopy(self.s._routes_data["events"])
        self.persist()
        self.now += timedelta(seconds=60)
        new, _ = self.run_weather(rows=cached_rows)  # Network guard rejects any uncached fetch.
        self.assertEqual(self.s._routes_data["route_weather_requests_this_sync"], 0)
        self.assertEqual(new["cache_key"], old["cache_key"])
        self.assertNotEqual(new["brief_id"], old["brief_id"])
        normalized = copy.deepcopy(new)
        for section in normalized["sections"]:
            self.assertEqual(section["weather"]["wind_classes"], ["head"])
            self.assertEqual(section["weather"]["map_sample"]["wind_class"], "head")
            section["weather"]["wind_classes"] = ["cross"]
            section["weather"]["map_sample"]["wind_class"] = "cross"
        for band in normalized["bands"]:
            if band["section_count"]:
                self.assertEqual(band["wind_class"], "head")
                self.assertEqual(band["display"]["wind_class"], "Headwind")
                band["wind_class"] = "cross"
                band["display"]["wind_class"] = "Crosswind"
        normalized["brief_id"] = old["brief_id"]
        self.assertEqual(normalized, old)  # Every non-classification field is preserved.
        snapshots = self.s._routes_data["brief_snapshots"]
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0], retained)
        self.assertEqual(snapshots[1]["brief"]["brief_id"], new["brief_id"])

    def test_precipitation_intervals_have_correct_time_direction(self):
        om = Weather.parse_open_meteo(om_point())
        met = Weather.parse_met(met_point())
        self.assertEqual(om["periods"][0]["start"], BASE - 3600)
        self.assertEqual(om["periods"][0]["end"], BASE)
        self.assertEqual(met["periods"][0]["start"], BASE)
        self.assertEqual(met["periods"][0]["end"], BASE + 3600)

    def test_fictional_route_wind_classes_match_components_and_central_angles(self):
        routes = json.loads((SYNC_PATH.parent / "json-examples/routes.example.json").read_text())
        for row in routes["events"]:
            for section in row.get("route_weather", {}).get("sections", []):
                weather = section["weather"]
                speed_lo, speed_hi = weather["wind_speed_range_mps"]
                self.assertEqual(speed_lo, speed_hi)  # These fictional envelopes have fixed speed.
                angles = [math.degrees(math.acos(head / speed_lo))
                          for head in weather["head_component_range_mps"]]
                expected = sorted({"head" if a < 80 else "cross" if a < 110 else "tail" for a in angles})
                self.assertEqual(weather["wind_classes"], expected)
                sample = weather["map_sample"]
                angle = abs((sample["wind_from_deg"] - section["bearing_deg"] + 180) % 360 - 180)
                self.assertEqual(sample["wind_class"],
                                 "head" if angle < 80 else "cross" if angle < 110 else "tail")

    def test_unit_mismatch_is_unknown_not_zero(self):
        payload = om_point()
        payload["hourly_units"]["wind_speed_10m"] = "km/h"
        result = Weather.parse_open_meteo(payload)
        self.assertIsNone(result["instants"][0]["speed"])
        self.assertEqual(result["instants"][0]["temperature"], 0)
        met = met_point()
        met["properties"]["meta"]["units"]["air_temperature"] = "C"
        self.assertEqual(Weather.parse_met(met)["instants"][0]["temperature"], 0)
        met["properties"]["meta"]["units"]["air_temperature"] = "kelvin"
        self.assertIsNone(Weather.parse_met(met)["instants"][0]["temperature"])

    def test_missing_hours_and_arrays_cannot_claim_complete(self):
        self.row["terrain_summary"]["polyline"] = [[0, 55, 12, 0], [1, 55.01, 12, 0]]
        def truncated(url, params, **kwargs):
            response = success(url, params, **kwargs)
            for row in response.json.return_value:
                row["hourly"]["temperature_2m"] = [1]
            return response
        brief, _ = self.run_weather(truncated)
        self.assertEqual(brief["status"], "partial")
        self.assertIsNone(brief["summary"]["temperature_range_c"])

    def test_no_and_thin_history_defaults_are_explicit(self):
        self.assertEqual(Weather.calibrate([], {})["sample_count"], 0)
        activities, previous = history()
        result = Weather.calibrate(activities[:2], previous)
        self.assertEqual(result["status"], "default")
        self.assertEqual(result["sample_count"], 2)

    def test_rich_history_recovers_hand_defined_class_paces(self):
        result = Weather.calibrate(*history())
        self.assertEqual(result["status"], "calibrated")
        for kind, speed in (("flat", 20), ("climb", 10), ("descent", 30)):
            lower, upper = result["speed_ranges_kmh"][kind]
            self.assertAlmostEqual(lower, speed / 1.1, delta=0.2)
            self.assertAlmostEqual(upper, speed / 0.9, delta=0.2)

    def test_calibrated_history_precedes_power_and_is_unchanged(self):
        activities, previous = history()
        expected = Weather.calibrate(activities, previous)
        result = Weather.estimate_eta(activities, previous, {"ftp_w": 400, "body_weight_kg": 70})
        self.assertEqual(result, expected)
        self.assertEqual(result["status"], "calibrated")
        self.assertNotIn("power_estimate", result)

    def test_personal_prior_precedes_power_and_uses_observed_paces(self):
        activities, _ = history()
        activities = activities[:3]
        result = Weather.estimate_eta(activities, {}, {"ftp_w": 400, "body_weight_kg": 70})
        self.assertEqual(result, Weather.calibrate(activities, {}))
        self.assertEqual(result["status"], "history_prior")
        paces = [a["moving_time"] / (a["distance"] / 1000) for a in activities]
        for kind in ("flat", "climb", "descent"):
            self.assertEqual(result["speed_ranges_kmh"][kind], [3600 / (max(paces) * 1.1), 3600 / (min(paces) * .9)])

    def test_thin_history_uses_power_and_preserves_sample_coverage(self):
        activities, previous = history()
        result = Weather.estimate_eta(activities[:2], previous, {"ftp_w": 240, "body_weight_kg": 70})
        self.assertEqual(result["method"], "power_estimate")
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["sample_activity_ids"], ["0", "1"])
        self.assertEqual(result["reason"], "insufficient_outdoor_history")

    def test_no_history_uses_power_with_complete_assumptions(self):
        result = Weather.estimate_eta([], {}, {"ftp_w": 240, "body_weight_kg": 70})
        self.assertEqual((result["status"], result["sample_count"]), ("power_estimate", 0))
        model = result["power_estimate"]
        self.assertEqual(model["ftp_w"], 240)
        self.assertEqual(model["body_weight_kg"], 70)
        self.assertEqual(model["effort_fraction_ftp"], [0.6, 0.75])
        self.assertEqual(model["assumptions"]["climb_gradient"], 0.05)
        self.assertEqual(model["assumptions"]["position"], "road_hoods")
        self.assertIn("not personal speed observations", result["uncertainty"])

    def test_missing_power_input_uses_raised_generic_default(self):
        for inputs, reason in (({}, "missing_ftp_w"), ({"body_weight_kg": 70}, "missing_ftp_w"),
                               ({"ftp_w": 240}, "missing_body_weight_kg")):
            with self.subTest(inputs=inputs):
                result = Weather.estimate_eta([], {}, inputs)
                self.assertEqual(result["status"], "default")
                self.assertEqual(result["method"], "generic_default_v2")
                self.assertEqual(result["power_estimate_unavailable_reason"], reason)
                self.assertEqual(result["speed_ranges_kmh"], {"flat": [22, 32], "climb": [8, 18], "descent": [20, 50]})
                self.assertNotIn("power_estimate", result)

    def test_implausible_power_inputs_do_not_enter_solver(self):
        invalid = [0, -1, True, "240", float("nan"), float("inf")]
        for key, values in (("ftp_w", invalid + [49.9, 600.1]), ("body_weight_kg", invalid + [29.9, 200.1])):
            for value in values:
                with self.subTest(key=key, value=value), mock.patch.object(Weather, "steady_speed") as solve:
                    result = Weather.estimate_eta([], {}, {"ftp_w": 240, "body_weight_kg": 70, key: value})
                    self.assertEqual(result["status"], "default")
                    self.assertEqual(result["power_estimate_unavailable_reason"], "implausible_" + key)
                    solve.assert_not_called()
        for ftp, weight in ((50, 100), (600, 60)):
            result = Weather.estimate_eta([], {}, {"ftp_w": ftp, "body_weight_kg": weight})
            self.assertEqual(result["power_estimate_unavailable_reason"], "implausible_ftp_per_kg")

    def test_power_input_boundaries_remain_usable(self):
        for ftp, weight in ((50, 60), (600, 100), (150, 30), (200, 200), (75, 100), (420, 60)):
            with self.subTest(ftp=ftp, weight=weight):
                result = Weather.estimate_eta([], {}, {"ftp_w": ftp, "body_weight_kg": weight})
                self.assertEqual(result["status"], "power_estimate")
                for low, high in result["speed_ranges_kmh"].values():
                    self.assertGreater(low, 0)
                    self.assertGreater(high, low)

    def test_power_solution_balances_drag_rolling_gravity_and_loss(self):
        # Independent recomputation with explicit constants, including slope geometry.
        for ftp, weight in ((180, 60), (300, 90)):
            result = Weather.estimate_eta([], {}, {"ftp_w": ftp, "body_weight_kg": weight})
            for kind, grade in (("flat", 0), ("climb", 0.05)):
                for effort, speed in zip((0.6, 0.75), result["power_estimate"]["model_speed_ranges_kmh"][kind]):
                    velocity = speed / 3.6
                    theta = math.atan(grade)
                    needed = (0.5 * 1.225 * 0.32 * velocity ** 3
                              + (weight + 10) * 9.80665 * (0.005 * math.cos(theta) + math.sin(theta)) * velocity)
                    self.assertAlmostEqual(needed, ftp * effort * 0.97, delta=0.03)

    def test_power_speed_monotonicity_and_descent_independent_cap(self):
        low = Weather.estimate_eta([], {}, {"ftp_w": 180, "body_weight_kg": 70})
        high = Weather.estimate_eta([], {}, {"ftp_w": 300, "body_weight_kg": 70})
        heavy = Weather.estimate_eta([], {}, {"ftp_w": 300, "body_weight_kg": 110})
        for kind in ("flat", "climb"):
            self.assertLess(low["speed_ranges_kmh"][kind][0], high["speed_ranges_kmh"][kind][0])
            self.assertLess(heavy["speed_ranges_kmh"][kind][1], high["speed_ranges_kmh"][kind][1])
        for result in (low, high, heavy):
            self.assertEqual(result["speed_ranges_kmh"]["descent"], [20, 50])
            self.assertNotIn("descent", result["power_estimate"]["model_speed_ranges_kmh"])

    def test_power_inputs_use_outdoor_ftp_and_wellness_before_profile(self):
        cycling = {"ftp": 240, "ftp_indoor": 400, "eftp": 500}
        inputs = Weather.eta_inputs(cycling, {"weight": 70, "id": "2026-10-19"}, {"icu_weight": 90})
        self.assertEqual(inputs, {"ftp_w": 240, "ftp_source": "cycling.ftp", "body_weight_kg": 70,
                                  "weight_source": "latest_wellness.weight", "weight_date": "2026-10-19"})
        fallback = Weather.eta_inputs(cycling, {}, {"icu_weight": 90})
        self.assertEqual((fallback["body_weight_kg"], fallback["weight_source"], fallback["weight_date"]),
                         (90, "athlete.icu_weight", None))
        invalid = Weather.eta_inputs(cycling, {"weight": 0}, {"icu_weight": 90})
        self.assertEqual(Weather.estimate_eta([], {}, invalid)["status"], "default")
        no_outdoor = Weather.eta_inputs({"ftp_indoor": 400}, {}, {"icu_weight": 70})
        self.assertEqual(Weather.estimate_eta([], {}, no_outdoor)["status"], "default")

    def test_power_change_rebinds_brief_without_http_or_snapshot_mutation(self):
        self.power_inputs = Weather.eta_inputs({"ftp": 180}, {"weight": 70}, {})
        first, _ = self.run_weather(success)
        self.assertEqual(first["eta"]["status"], "uncalibrated_physics")
        self.persist()
        frozen = copy.deepcopy(self.s._routes_data["brief_snapshots"][0])
        self.power_inputs["ftp_w"] = 300
        self.now += timedelta(seconds=60)
        second, _ = self.run_weather()  # network guard proves zero requests
        self.assertNotEqual(first["cache_key"], second["cache_key"])
        self.assertNotEqual(first["eta"]["speed_ranges_kmh"], second["eta"]["speed_ranges_kmh"])
        self.assertEqual(self.s._routes_data["route_weather_requests_this_sync"], 0)
        self.assertEqual(self.s._routes_data["brief_snapshots"][0], frozen)
        self.assertEqual(second["eta"]["model_inputs"]["configured_main_ftp_w"], 300)

    def test_sync_weather_wrapper_forwards_existing_power_inputs(self):
        inputs = Weather.eta_inputs({"ftp": 240}, {}, {"icu_weight": 70})
        with mock.patch.object(mod, "RouteWeather") as weather:
            self.s._generate_route_weather(self.events, [], {}, self.athlete, inputs)
        weather.return_value.generate.assert_called_once_with(self.events, [], {}, self.athlete, inputs)

    def test_power_and_generic_windows_keep_stop_allowance_and_rounding(self):
        terrain = {"polyline": [[0, 55, 12, 0], [10, 55.1, 12, 0]], "climbs": [], "descents": []}
        for inputs in (None, {"ftp_w": 240, "body_weight_kg": 70}):
            result = Weather.estimate_eta([], {}, inputs)
            low, high = result["speed_ranges_kmh"]["flat"]
            Weather.sections(terrain, result, Weather.departure(self.events[0], {}))
            self.assertEqual(result["duration_range_s"],
                             [math.floor(10 / high * 3600 / 300) * 300,
                              math.ceil(10 / low * 3600 * 1.25 / 300) * 300])
            if inputs:
                model = result["power_estimate"]["model_speed_ranges_kmh"]["flat"]
                self.assertLessEqual(low, model[0] * 0.8 + 0.001)
                self.assertGreaterEqual(high, model[1] * 1.2 - 0.001)

    def test_virtual_trainer_indoor_and_other_sports_excluded(self):
        activities, previous = history()
        for a in activities:
            a["trainer"] = True
        self.assertEqual(Weather.calibrate(activities, previous)["sample_count"], 0)
        for a in activities:
            a.pop("trainer")
            a["type"] = "VirtualRide"
        self.assertEqual(Weather.calibrate(activities, previous)["sample_count"], 0)

    def test_correlated_terrain_history_is_not_identifiable(self):
        activities, previous = history()
        for terrain in previous.values():
            terrain["terrain_summary"]["descents"] = copy.deepcopy(terrain["terrain_summary"]["climbs"])
        result = Weather.calibrate(activities, previous)
        self.assertEqual(result["status"], "history_prior")
        self.assertEqual(result["reason"], "terrain_mixtures_not_identifiable")

    def test_nonmonotonic_geometry_is_explicit_and_network_free(self):
        self.row["terrain_summary"]["polyline"][1][0] = 0
        self.assertEqual(self.run_weather()[0]["reason_codes"], ["nonmonotonic_geometry"])

    def test_summary_pointer_matches_planned_and_race_outputs(self):
        brief, _ = self.run_weather(success)
        planned = self.s._format_events(self.events, "2026-10-19")[0]
        race = self.s._build_race_calendar(self.events, 50, 50, 0, [], "2026-10-19")["next_race"]
        for entry in (planned, race):
            pointer = entry["route_weather_summary"]
            self.assertEqual(pointer["brief_id"], brief["brief_id"])
            self.assertEqual(pointer["event_id"], brief["event_id"])
            self.assertEqual(pointer["route_ref"], brief["route_ref"])
            self.assertEqual(pointer["detail_file"], "routes.json")

    def test_weather_exception_isolated_from_sync(self):
        self.s._routes_data = {"events": [copy.deepcopy(self.row)]}
        with mock.patch.object(mod, "RouteWeather", side_effect=RuntimeError("synthetic")):
            self.s._generate_route_weather(self.events, [], {}, {})
        self.assertEqual(self.s._routes_data["events"][0]["route_weather"]["status"], "unavailable")

    def test_budget_bounds_many_events_and_provider_errors(self):
        self.events = [event(i, start_date_local="2026-10-19T12:00:00Z") for i in range(8)]
        rows = [{**copy.deepcopy(self.row), "event_id": i} for i in range(8)]
        for i, row in enumerate(rows):
            for point in row["terrain_summary"]["polyline"]:
                point[2] += i
        _, get = self.run_weather(lambda *a, **k: reply(status=503), rows)
        self.assertEqual(get.call_count, Weather.MAX_REQUESTS)
        self.assertEqual(self.s._routes_data["events"][6]["route_weather"]["reason_codes"], ["event_budget_exhausted"])

    def test_changed_eta_rebinds_without_http(self):
        first, _ = self.run_weather(success)
        self.persist()
        self.activities, self.previous = history()
        second, _ = self.run_weather()
        self.assertNotEqual(first["cache_key"], second["cache_key"])
        self.assertEqual(second["eta"]["status"], "pace_with_climbing_allowance")
        self.assertEqual(self.s._routes_data["route_weather_requests_this_sync"], 0)

    def test_changed_coordinates_fetch_through_normal_refresh_gate(self):
        self.run_weather(success)
        self.persist()
        for point in self.row["terrain_summary"]["polyline"]:
            point[2] += 1
        self.now += timedelta(seconds=60)
        brief, get = self.run_weather(success)
        self.assertEqual(brief["status"], "ok")
        self.assertEqual(get.call_count, 1)

    def test_sampling_retains_opposite_bearings_and_bounds(self):
        terrain = {"polyline": [[0, 55, 12, 0], [1, 55.01, 12, 0], [2, 55, 12, 0]],
                   "climbs": [], "descents": []}
        legs, points = Weather.sections(terrain, Weather.calibrate([], {}), Weather.departure(self.events[0], {}))
        self.assertEqual([leg["bearing_deg"] for leg in legs], [0, 180])
        self.assertEqual([leg["start_km"] for leg in legs], [0, 1])
        self.assertLessEqual(len(points), Weather.MAX_POINTS)
        terrain["polyline"] *= 200
        with self.assertRaisesRegex(ValueError, "oversized"):
            Weather.sections(terrain, Weather.calibrate([], {}), Weather.departure(self.events[0], {}))

    def test_missing_met_period_is_partial_without_invented_dry_weather(self):
        payload = met_point()
        for row in payload["properties"]["timeseries"]:
            row["data"]["next_1_hours"] = None
        brief, _ = self.run_weather(lambda url, **kw: reply(status=503) if url == Weather.OM_URL else reply(payload))
        self.assertEqual(brief["status"], "partial")
        self.assertIsNone(brief["summary"]["hourly_precipitation_max_mm"])
        self.assertEqual(brief["summary"]["temperature_range_c"], [0, 0])

    def test_precipitation_gap_is_not_complete(self):
        self.assertFalse(Weather.covered([{"start": 0, "end": 3600, "precipitation": 0},
                                         {"start": 7200, "end": 10800, "precipitation": 0}],
                                        0, 10800, "precipitation"))
        self.assertTrue(Weather.covered([{"start": 0, "end": 3600, "precipitation": 0},
                                        {"start": 3600, "end": 7200, "precipitation": 0}],
                                       0, 7200, "precipitation"))

    def test_forecast_with_no_route_time_overlap_is_unavailable(self):
        def short_forecast(url, params, **kwargs):
            result = success(url, params, **kwargs)
            for row in result.json.return_value:
                row["hourly"] = {key: values[:6] for key, values in row["hourly"].items()}
            return result
        brief, get = self.run_weather(short_forecast)
        self.assertEqual(brief["status"], "unavailable")
        self.assertEqual(brief["reason_codes"], ["no_forecast_values_in_route_window"])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(self.s._routes_data["brief_snapshots"], [])

    def test_met_redirect_is_same_product_and_consumes_budget(self):
        producer = Weather(self.s, self.now)
        target = Weather.MET_URL + "?lat=55&lon=12"
        with mock.patch.object(mod.requests, "get", side_effect=[reply(status=302, headers={"Location": target}),
                                                                  reply(met_point())]) as get:
            response = producer.request("met_norway", Weather.MET_URL, {"lat": 55, "lon": 12})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(producer.requests_made, 2)
        self.assertIn("payload", response)
        with mock.patch.object(mod.requests, "get", return_value=reply(status=302, headers={"Location": "https://example.org/"})) as get:
            response = producer.request("met_norway", Weather.MET_URL, {})
        self.assertEqual(get.call_count, 1)
        self.assertEqual(response["error"], "http_302")

    def test_request_admission_deadline_skips_http(self):
        producer = Weather(self.s, self.now)
        with mock.patch.object(mod.time, "monotonic", return_value=producer.started + Weather.ADMISSION_SECONDS):
            self.assertIsNone(producer.request("open_meteo", Weather.OM_URL, {}))
        self.assertEqual(producer.requests_made, 0)

    def test_transport_failure_and_invalid_json_are_unavailable(self):
        def timeout(*args, **kwargs):
            raise mod.requests.exceptions.Timeout("synthetic")
        self.assertEqual(self.run_weather(timeout)[0]["status"], "unavailable")
        response = reply()
        response.iter_content.return_value = [b'invalid json']
        response.iter_content.side_effect = None
        self.assertEqual(self.run_weather(lambda *a, **kw: response)[0]["status"], "unavailable")

    def test_snapshot_retention_and_capacity_are_explicit(self):
        self.run_weather(success)
        snapshot = self.s._routes_data["brief_snapshots"][0]
        expired = copy.deepcopy(snapshot)
        expired["saved_at"] = Weather.iso(NOW.timestamp() - 30 * 86400)
        self.s._routes_data["brief_snapshots"].append(expired)
        self.persist()
        self.run_weather()
        self.assertEqual(len(self.s._routes_data["brief_snapshots"]), 1)
        self.persist()
        self.events[0]["start_date_local"] = "2026-10-19T13:00:00Z"
        with mock.patch.object(Weather, "MAX_SNAPSHOTS", 1):
            brief, _ = self.run_weather()
        self.assertEqual(brief["snapshot_status"], "capacity_reached")
        self.assertEqual(self.s._routes_data["brief_snapshots"], [snapshot])

    def test_imperial_display_preserves_canonical_forecast(self):
        first, _ = self.run_weather(success)
        self.persist()
        self.athlete.update(fahrenheit=True, wind_speed="MPH", rain="IN", measurement_preference="feet")
        second, _ = self.run_weather()
        self.assertEqual(first["summary"]["temperature_range_c"], second["summary"]["temperature_range_c"])
        self.assertEqual(second["summary"]["display"]["temperature_range"],
                         {"value": [32, 32], "unit": "F", "label": "32.0 °F"})
        self.assertEqual(second["summary"]["display"]["gust_max"]["unit"], "mph")
