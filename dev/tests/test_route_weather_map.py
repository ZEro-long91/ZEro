"""Offline PNG structure, binding, presentation and lifecycle checks.

Weather fixtures are synthetic. No provider requests or real-account data.
"""
import copy
import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from datetime import timedelta
from pathlib import Path
from unittest import mock

from _harness import (SYNC_PATH, NetworkBlocked, install_verb_guard,
                      load_module_by_path, restore_verb_guard)
from test_route_validity import route_bytes, event
from test_route_weather import NOW, om_point

mod = load_module_by_path("s11_sync_weather_map", SYNC_PATH)
Weather, Map = mod.RouteWeather, mod.RouteWeatherMap


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


def fixture(sync, terrain=None, outlook=False, athlete=None, eid=7):
    """Build a production brief with mocked local provider records, not HTTP."""
    athlete = athlete or {"timezone": "UTC"}
    terrain = terrain or sync._parse_route_file(route_bytes(), "synthetic.gpx")["terrain_summary"]
    departure = NOW + timedelta(hours=96 if outlook else 2)
    evt = event(eid, start_date_local=departure.isoformat())
    row = {"event_id": eid, "route_status": "ok", "terrain_summary": terrain,
           "route_ref": {"route_id": f"intervals:{eid}:attachment:12", "attachment_id": 12,
                         "revision": "a" * 64, "terrain_version": "3.142"}}
    producer = Weather(sync, NOW)
    def records(provider, points, new_points=()):
        result = []
        for i, point in enumerate(points):
            payload = om_point(point["lat"], point["lon"])
            hours = payload["hourly"]
            hours["temperature_2m"] = [9 + i + n % 3 for n in range(192)]
            hours["apparent_temperature"] = [7 + i + n % 3 for n in range(192)]
            hours["wind_speed_10m"] = [4 + i for n in range(192)]
            hours["wind_direction_10m"] = [(i * 70 + n * 15) % 360 for n in range(192)]
            hours["wind_gusts_10m"] = [7 + i for n in range(192)]
            hours["precipitation"] = [(n % 4) * .2 for n in range(192)]
            hours["precipitation_probability"] = [20 + (n % 4) * 15 for n in range(192)]
            result.append({"forecast": Weather.parse_open_meteo(payload),
                           "fetched_at": NOW.timestamp(), "validated_at": NOW.timestamp(),
                           "expires_at": NOW.timestamp() + 3600})
        return result
    with mock.patch.object(producer, "fetch_provider", side_effect=records):
        row["route_weather"] = producer.brief(row, evt, athlete, Weather.prepare_eta([], None))
    return row, evt


def decode_png(data):
    """Independent PNG parser, validates bounds/CRC and decodes every scanline."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    position, chunks, packed = 8, [], bytearray()
    while position < len(data):
        size = struct.unpack(">I", data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + size]
        crc = struct.unpack(">I", data[position + 8 + size:position + 12 + size])[0]
        assert len(body) == size and crc == zlib.crc32(kind + body) & 0xffffffff
        chunks.append(kind)
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            assert (depth, color, compression, filtering, interlace) == (8, 2, 0, 0, 0)
        if kind == b"IDAT":
            packed.extend(body)
        position += size + 12
    assert position == len(data) and chunks == [b"IHDR", b"IDAT", b"IEND"]
    pixels = zlib.decompress(packed)
    assert len(pixels) == height * (width * 3 + 1)
    assert all(pixels[y * (width * 3 + 1)] == 0 for y in range(height))
    return width, height, pixels


class WeatherMapTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sync = mod.IntervalsSync.__new__(mod.IntervalsSync)
        self.sync.data_dir = Path(tmp.name)
        self.sync.athlete_id = "synthetic"
        self.sync.debug = False
        self.sync.route_weather_enabled = True
        self.row, self.evt = fixture(self.sync)
        self.sync._routes_data = {"events": [self.row]}
        self.sync._planned_routes = {7: self.row}

    def generate(self):
        Map.generate(self.sync, [self.evt], {"timezone": "UTC"}, NOW)

    def cheap_generate(self):
        # File lifecycle tests do not need to re-rasterize the same picture.
        with mock.patch.object(Map, "png", return_value=b"synthetic-png"):
            self.generate()

    def test_png_signature_chunks_crc_zlib_size_and_determinism(self):
        scene = Map.scene(self.row)
        first, second = Map.png(scene), Map.png(scene)
        self.assertEqual(first, second)
        width, height, pixels = decode_png(first)
        self.assertEqual((width, height), (1080, scene[0][2]))
        self.assertLessEqual(height, Map.HEIGHT)
        self.assertLess(len(first), Map.MAX_BYTES)
        self.assertGreater(len(set(pixels)), 20)  # Includes antialiased edge shades.
        corrupted = bytearray(first)
        corrupted[50] ^= 1
        with self.assertRaises(AssertionError):
            decode_png(corrupted)

    def test_wind_from_direction_relative_to_route_and_boundaries(self):
        for bearing in (0, 90, 180, 270, 359):
            for difference, kind in ((0, "head"), (79.99, "head"), (80, "cross"),
                                     (80.01, "cross"), (109.99, "cross"), (110, "tail"),
                                     (110.01, "tail"), (180, "tail"), (280, "cross"), (315, "head")):
                for sign in (-1, 1):
                    section = {"bearing_deg": bearing,
                               "arrival_window_utc": [Weather.iso(0)] * 2,
                               "end_window_utc": [Weather.iso(0)] * 2}
                    record = {"forecast": {"instants": [{"at": 0, "speed": 5,
                              "direction": (bearing + sign * difference) % 360}]}}
                    sample = Map.central_sample(section, record, 1, "m/s")
                    self.assertEqual(sample["wind_class"], kind)
                    section["weather"] = {"map_sample": sample}
                    self.assertEqual(Map.sample_kind(section), kind)
                    self.assertEqual(Weather.section_weather(section, record, "open_meteo")["wind_classes"], [kind])
        self.assertEqual(Weather.wind(0, None, 0)["class"], "calm")
        self.assertEqual(Weather.wind(5, None, 0)["class"], "unknown")
        self.assertEqual(Weather.wind(5, 0, 0)["head_mps"], 5)
        self.assertEqual(Weather.wind(5, 180, 0)["head_mps"], -5)

    def test_changed_band_dominance_reselects_representative_before_rendering(self):
        brief = self.row["route_weather"]
        prototype = brief["sections"][0]
        self.row["terrain_summary"]["polyline"] = [[km, 55 + km * .01, 12, 0] for km in (0, 1, 4, 6)]
        self.row["terrain_summary"]["total_distance_km"] = 6
        brief["sampling"]["points"] = brief["sampling"]["points"][:2]
        brief["sampling"]["points"][0]["km"] = 0.5
        brief["sections"] = []
        # Old rule: Crosswind 4 km, Headwind 2 km; nearest Crosswind is index 0.
        # New rule: Headwind 5 km, Crosswind 1 km; index 0 is no longer eligible.
        for i, (start, end, angle, old_kind) in enumerate(
                ((0, 1, 90, "cross"), (1, 4, 60, "cross"), (4, 6, 10, "head"))):
            section = copy.deepcopy(prototype)
            section.update(section_id=i, start_km=start, end_km=end, bearing_deg=0, point_id=0)
            section["display"]["start"]["value"] = start
            section["display"]["end"]["value"] = end
            section["weather"]["map_sample"].update(
                wind_speed_mps=5, wind_from_deg=angle, wind_class=old_kind)
            brief["sections"].append(section)
        with mock.patch.object(Weather, "HEAD_MAX_DEG", 45), mock.patch.object(Weather, "TAIL_MIN_DEG", 135):
            old = Weather.forecast_bands(brief)[0]
        self.assertEqual((old["wind_class"], old["representative_section_index"]), ("cross", 0))
        brief["bands"] = Weather.forecast_bands(brief)
        new = brief["bands"][0]
        self.assertEqual((new["wind_class"], new["representative_section_index"]), ("head", 1))
        self.assertEqual(new["display"]["wind_class"], "Headwind")
        self.assertEqual(Map.sample_kind(brief["sections"][new["representative_section_index"]]), "head")
        self.assertIs(Map.bands(brief), brief["bands"])
        # No other band metadata, coverage, extrema or timing changed.
        normalized = copy.deepcopy(new)
        normalized.update(wind_class="cross", representative_section_index=0)
        normalized["display"]["wind_class"] = "Crosswind"
        self.assertEqual(normalized, old)
        labels = [op[3] for op in Map.scene(self.row) if op[0] == "text"]
        self.assertIn("Headwind", labels)

    def test_sample_uses_nearest_native_instant_and_earlier_tie(self):
        section = {"arrival_window_utc": [Weather.iso(0), Weather.iso(0)],
                   "end_window_utc": [Weather.iso(3600), Weather.iso(3600)], "bearing_deg": 0}
        record = {"forecast": {"instants": [{"at": 0, "speed": 5, "direction": 359},
                                           {"at": 3600, "speed": 10, "direction": 1}]}}
        sample = Map.central_sample(section, record, 3.6, "km/h")
        self.assertEqual(sample["forecast_at_utc"], Weather.iso(0))
        self.assertEqual(sample["wind_from_deg"], 359)
        self.assertEqual(sample["display_wind_speed"], {"value": 18.0, "unit": "km/h"})
        record["forecast"]["instants"] = [{"at": 9000, "speed": 5, "direction": 0}]
        self.assertIsNone(Map.central_sample(section, record, 1, "m/s"))

    def test_unknown_direction_and_calm_do_not_draw_invented_wind(self):
        for speed in (0, 5):
            row = copy.deepcopy(self.row)
            for section in row["route_weather"]["sections"]:
                section["weather"]["map_sample"].update(wind_from_deg=None, wind_speed_mps=speed)
            row['route_weather']['bands']=Weather.forecast_bands(row['route_weather'])
            messages = "\n".join(i[3] for i in Map.scene(row) if i[0] == "text")
            self.assertIn("Calm" if speed == 0 else "Unknown", messages)

    def test_outlook_has_no_section_colours_or_weather_arrows(self):
        row, _ = fixture(self.sync, outlook=True)
        self.assertEqual(row["route_weather"]["sections"], [])
        scene = Map.scene(row)
        text = "\n".join(i[3] for i in scene if i[0] == "text")
        self.assertIn("Outlook only", text)
        self.assertIn("rain timing uncertain", text)
        self.assertFalse(any(i[0] == "line" and i[3] in [Map.STYLES[k][0] for k in ("head", "cross", "tail")] for i in scene))
        row["route_weather"]["sections"] = self.row["route_weather"]["sections"]
        with self.assertRaisesRegex(ValueError, "outlook_has_sections"):
            Map.scene(row)

    def test_scene_covers_all_sections_and_contains_provenance(self):
        text = "\n".join(i[3] for i in Map.scene(self.row) if i[0] == "text")
        for needle in ("START", "FINISH", "RAIN", "GUSTS", "°C", "fetched",
                       "WIND ALONG THE ROUTE", "CC BY 4.0", "not a safety assessment", "section11.net"):
            self.assertIn(needle, text)
        bands = Map.bands(self.row["route_weather"])
        self.assertEqual(sum(b['section_count'] for b in bands), len(self.row['route_weather']['sections']))
        for band in bands:
            self.assertIn(band['letter'], text)
            self.assertEqual(band['section_count'],sum(s['point_id']==band['point_id'] for s in self.row['route_weather']['sections']))

    def test_map_metadata_hash_binding_and_stable_filename(self):
        before = copy.deepcopy(self.row["route_weather"])
        self.cheap_generate()
        descriptor = self.row["route_weather_map"]
        path = self.sync.data_dir / descriptor["path"]
        self.assertEqual(descriptor["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(descriptor["brief_id"], before["brief_id"])
        self.assertEqual(descriptor["format"], "png")
        self.assertEqual(self.row["route_weather"], before)
        stamp = path.stat().st_mtime_ns
        self.cheap_generate()
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertNotIn("Synthetic route", path.name)
        self.assertNotIn("intervals:", path.name)

    def test_stale_partial_unavailable_expired_and_changed_binding_remove_map(self):
        original = copy.deepcopy(self.row["route_weather"])
        cases = [("status", "stale"), ("status", "partial"), ("status", "unavailable"),
                 ("expires_at", NOW.isoformat()), ("event_id", 8), ("route_ref", {}),
                 ("departure", {}), ("brief_id", None), ("cache_key", None)]
        for key, value in cases:
            self.row["route_weather"] = copy.deepcopy(original)
            self.cheap_generate()
            path = self.sync.data_dir / self.row["route_weather_map"]["path"]
            self.row["route_weather"][key] = value
            self.cheap_generate()
            self.assertNotIn("route_weather_map", self.row)
            self.assertFalse(path.exists())

    def test_removed_events_clean_only_managed_images(self):
        self.cheap_generate()
        path = self.sync.data_dir / self.row["route_weather_map"]["path"]
        unrelated = path.parent / "personal.png"
        unrelated.write_bytes(b"keep")
        self.sync._routes_data = {"events": []}
        self.cheap_generate()
        self.assertFalse(path.exists())
        self.assertEqual(unrelated.read_bytes(), b"keep")

    def test_off_switch_cleans_maps_and_current_brief_has_no_report_values(self):
        self.cheap_generate()
        path = self.sync.data_dir / self.row["route_weather_map"]["path"]
        self.sync.route_weather_enabled = False
        Weather(self.sync, NOW).generate([self.evt], [], {}, {"timezone": "UTC"})
        self.cheap_generate()
        self.assertFalse(path.exists())
        self.assertNotIn("route_weather_map", self.row)
        brief = self.row["route_weather"]
        self.assertEqual(brief["reason_codes"], ["disabled"])
        for key in ("summary", "sections", "sampling", "outlook", "bands"):
            self.assertNotIn(key, brief)

    def test_indoor_past_and_beyond_horizon_never_create_map(self):
        for changes in ({"indoor": True}, {"type": "VirtualRide"},
                        {"start_date_local": NOW.isoformat()},
                        {"start_date_local": (NOW + timedelta(hours=121)).isoformat()}):
            self.evt.update(changes)
            self.cheap_generate()
            self.assertNotIn("route_weather_map", self.row)
            self.evt = fixture(self.sync)[1]

    def test_geometry_mismatch_is_rejected(self):
        self.row["terrain_summary"]["polyline"][1][0] += .01
        self.cheap_generate()
        self.assertNotIn("route_weather_map", self.row)
        self.assertEqual(self.row["route_weather_map_status"], "invalid_map_data")

    def test_write_failure_retains_text_and_exposes_status(self):
        before = copy.deepcopy(self.row["route_weather"])
        with mock.patch.object(Map, "png", side_effect=OSError("synthetic write failure")):
            self.generate()
        self.assertEqual(self.row["route_weather"], before)
        self.assertEqual(self.row["route_weather_map_status"], "image_write_failed")

    def test_symlink_directory_never_followed(self):
        target = self.sync.data_dir / "untouched"
        target.mkdir()
        (self.sync.data_dir / Map.DIRECTORY).parent.mkdir(exist_ok=True)
        (self.sync.data_dir / Map.DIRECTORY).symlink_to(target, target_is_directory=True)
        self.cheap_generate()
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.row["route_weather_map_status"], "unsafe_directory")

    def test_six_event_bound(self):
        pairs = [fixture(self.sync, eid=i) for i in range(8)]
        self.sync._routes_data = {"events": [r for r, e in pairs]}
        with mock.patch.object(Map, "png", return_value=b"png"):
            Map.generate(self.sync, [e for r, e in pairs], {"timezone": "UTC"}, NOW)
        self.assertEqual(sum("route_weather_map" in r for r, e in pairs), 6)
        self.assertEqual(len(list((self.sync.data_dir / Map.DIRECTORY).glob("*.png"))), 6)

    def test_local_generation_does_not_call_http_and_guard_is_effective(self):
        self.cheap_generate()
        with self.assertRaises(NetworkBlocked):
            mod.requests.get("https://example.invalid/offline-negative-control")

    def test_sync_wrapper_calls_renderer_after_forecast(self):
        calls = []
        with mock.patch.object(Weather, "generate", side_effect=lambda *a: calls.append("brief")), \
             mock.patch.object(Map, "generate", side_effect=lambda *a: calls.append("map")):
            self.sync._generate_route_weather([self.evt], [], {}, {"timezone": "UTC"})
        self.assertEqual(calls, ["brief", "map"])

    def test_longitude_wrap_and_straight_flat_geometry(self):
        for longitudes in ([179.9, -179.9, -179.8], [12, 12, 12]):
            terrain = {"polyline": [[i * 10, 55 + i * .05, lon, 0] for i, lon in enumerate(longitudes)],
                       "total_distance_km": 20, "total_elevation_m": 0, "climbs": [], "descents": []}
            row, _ = fixture(self.sync, terrain=terrain)
            self.assertTrue(Map.scene(row))

    def test_font_has_antialiased_masks_for_labels(self):
        for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789–·°↑↓←→ÆØÅæøåÄÖÜäöüß":
            advance, left, top, width, height, mask = mod.RouteMapFont.glyphs()[char]
            self.assertGreater(advance, 0)
            self.assertEqual(len(mask), width * height)
            self.assertTrue(any(0 < value < 255 for value in mask))

    def test_imperial_weather_uses_producer_display_values(self):
        row, _ = fixture(self.sync, athlete={"timezone": "UTC", "fahrenheit": True,
            "wind_speed": "MPH", "rain": "IN", "measurement_preference": "feet"})
        scene = Map.scene(row)
        text = "\n".join(i[3] for i in scene if i[0] == "text")
        self.assertIn(" mph", text)
        self.assertIn(" in/h ·", text)
        self.assertIn(" °F", text)
        self.assertIn(" mi", text)

    def test_cleanup_failure_is_explicit_and_keeps_text(self):
        self.cheap_generate()
        self.sync.route_weather_enabled = False
        with mock.patch.object(Path, "unlink", side_effect=OSError("synthetic cleanup failure")):
            self.cheap_generate()
        self.assertEqual(self.sync._routes_data["route_weather_map_cleanup_status"], "cleanup_failed")
        self.assertNotIn("route_weather_map", self.row)
        self.cheap_generate()
        self.assertNotIn("route_weather_map_cleanup_status", self.sync._routes_data)

    def test_optional_image_exception_does_not_abort_sync(self):
        before = copy.deepcopy(self.row["route_weather"])
        with mock.patch.object(Weather, "generate"), mock.patch.object(Map, "generate", side_effect=RuntimeError("synthetic failure")):
            self.sync._generate_route_weather([self.evt], [], {}, {"timezone": "UTC"})
        self.assertEqual(self.row["route_weather"], before)
        self.assertEqual(self.row["route_weather_map_status"], "image_processing_failed")

    def test_outlook_native_periods_and_fallback_attribution(self):
        row, _ = fixture(self.sync, outlook=True)
        brief = row["route_weather"]
        brief["provenance"].update(provider="met_norway", fallback=True)
        brief["outlook"]["precipitation_period_hours"] = [6, 12]
        text = "\n".join(i[3] for i in Map.scene(row) if i[0] == "text")
        self.assertIn("MET Norway (fallback)", text)
        self.assertIn("6h/12h", text)
        self.assertIn("api.met.no", text)

    def test_png_size_limit_fails_without_partial_image(self):
        with mock.patch.object(Map, "MAX_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "map_size_limit"):
                Map.png([])

    def test_reversed_geometry_and_nan_never_reuse_previous_map(self):
        for mutate in (lambda p: p.reverse(), lambda p: p[1].__setitem__(1, float("nan"))):
            original = copy.deepcopy(self.row["terrain_summary"]["polyline"])
            self.cheap_generate()
            path = self.sync.data_dir / self.row["route_weather_map"]["path"]
            mutate(self.row["terrain_summary"]["polyline"])
            self.cheap_generate()
            self.assertNotIn("route_weather_map", self.row)
            self.assertFalse(path.exists())
            self.row["terrain_summary"]["polyline"] = original


if __name__ == "__main__":
    unittest.main()
