"""Offline display, horizon boundaries and paired JSON example contracts."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from test_route_weather_map import fixture, mod, Map, NOW
from _harness import install_verb_guard, restore_verb_guard

Weather = mod.RouteWeather


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


class FinalWeatherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sync = object.__new__(mod.IntervalsSync)
        self.sync.data_dir = Path(tmp.name)
        self.sync.debug = False
        self.sync.athlete_id = 'synthetic-final'
        self.sync.route_weather_enabled = True
        self.row, self.event = fixture(self.sync)
        self.sync._routes_data = {'events': [self.row]}
        self.sync._overview_events = [self.event]
        self.sync._overview_athlete = {'timezone': 'UTC'}

    def overview(self):
        return mod.DataOverview.build(self.sync, {}, now=NOW)

    def test_display_rounding_equal_endpoints_and_scalar_decimals(self):
        for value, unit, label in (
            ([4.01, 4.04], 'm/s', '4.0 m/s'),
            ([4.04, 4.06], 'm/s', '4.0–4.1 m/s'),
            (14, 'm/s', '14.0 m/s'), (1.5, 'm/s', '1.5 m/s'),
            ([-2, -1.5], 'C', '-2.0–-1.5 °C'),
            ([9, 16], 'C', '9.0–16.0 °C'), (1, 'mm', '1.0 mm')):
            original = {'value': value, 'unit': unit}
            before = copy.deepcopy(original)
            self.assertEqual(Map.display_label(original), label)
            self.assertEqual(original, before)

    def test_trace_rain_boundary_probability_and_native_period(self):
        section = copy.deepcopy(self.row['route_weather']['sections'][0])
        for amount, probability, hours, label in (
            (0, 0, 1, 'Dry · 0%'), (0, 1, 1, '<0.1 mm/h · 1%'),
            (.049, 20, 1, '<0.1 mm/h · 20%'),
            (.05, 20, 1, '0.1 mm/h · 20%'),
            (1, 20, 1, '1.0 mm/h · 20%'),
            (.01, 20, 6, '<0.1 mm/6h · 20%'),
            (1, 20, 6, '1.0 mm/6h · 20%')):
            section['weather']['precipitation_intervals'] = [{
                'amount_mm': amount, 'probability_pct': probability,
                'period_hours': hours, 'display_amount': {'value': amount, 'unit': 'mm'}}]
            before = copy.deepcopy(section)
            self.assertEqual(Map.rain([section])[0], label)
            self.assertEqual(section, before)
        section['weather']['precipitation_intervals'] = []
        self.assertIn('unknown', Map.rain([section])[0])

    def test_incomplete_rain_never_becomes_dry_in_brief_or_band(self):
        section = copy.deepcopy(self.row['route_weather']['sections'][0])
        section['weather']['coverage']['precipitation'] = False
        for period in section['weather']['precipitation_intervals']:
            period.update(amount_mm=0, probability_pct=0, display_amount={'value': 0, 'unit': 'mm'})
        self.assertEqual(Map.rain([section])[0], 'Rain incomplete · 0%')

    def test_producer_summary_band_and_map_share_decimal_labels(self):
        brief = self.row['route_weather']
        display = brief['summary']['display']
        self.assertEqual(display['gust_max']['label'], '12.0 m/s')
        self.assertEqual(display['temperature_range']['label'], '9.0–16.0 °C')
        self.assertEqual(display['wind'], '4.0–9.0 m/s')
        self.assertEqual(display['rain'], Map.rain(brief['sections'])[0])
        text = '\n'.join(op[3] for op in Map.scene(self.row) if op[0] == 'text')
        for band in brief['bands']:
            self.assertIn(band['display']['wind'], text)
            self.assertIn(band['display']['rain'], text)
        self.assertIn('12.0 m/s', text)

    def test_exact_horizon_is_included_next_second_unrouted_is_counted(self):
        self.sync._routes_data = {'events': []}
        self.sync._overview_events = [
            dict(self.event, id=1, start_date_local=(NOW+timedelta(hours=120)).isoformat()),
            dict(self.event, id=2, start_date_local=(NOW+timedelta(hours=120, seconds=1)).isoformat())]
        result = self.overview()
        self.assertEqual([r['event_id'] for r in result['rides']], [1])
        self.assertEqual(result['rides_without_route_beyond_horizon'], 1)
        self.assertEqual((result['rides_total'], result['rides_omitted']), (1, 0))

    def test_later_routes_all_statuses_keep_climb_inventory_without_weather(self):
        rows, events = [], []
        for i, status in enumerate(('ok', 'failed', 'no_elevation', 'no_gps', 'unsupported_format')):
            row = dict(self.row, event_id=i, route_status=status)
            if status != 'ok':
                row.pop('terrain_summary')
            rows.append(row)
            events.append(dict(self.event, id=i, start_date_local=(NOW+timedelta(days=28)).isoformat()))
        self.sync._routes_data = {'events': rows}
        self.sync._overview_events = events + events
        before = copy.deepcopy(rows)
        result = self.overview()
        self.assertEqual(result['rides_total'], 5)
        for ride in result['rides']:
            self.assertTrue(ride['has_route'])
            self.assertEqual(ride['weather'], {'status': 'beyond_horizon'})
            self.assertEqual(ride['map'], {'present': False, 'reason': 'beyond_horizon'})
            self.assertEqual(ride['climbs_present'], ride['route_status'] == 'ok')
        self.assertEqual(rows, before)

    def test_cap_omission_counts_only_eligible_rides(self):
        later = (NOW+timedelta(days=14)).isoformat()
        self.sync._overview_events = [dict(self.event, id=i, start_date_local=later) for i in range(100)]
        self.sync._routes_data = {'events': [dict(self.row, event_id=i) for i in range(70)]}
        result = self.overview()
        self.assertEqual(len(result['rides']), 64)
        self.assertEqual((result['rides_total'], result['rides_omitted']), (70, 6))
        self.assertEqual(result['rides_without_route_beyond_horizon'], 30)

    def test_near_off_expired_and_partial_statuses_survive_horizon_filter(self):
        for status in ('off', 'expired', 'partial'):
            with self.subTest(status=status):
                self.sync.route_weather_enabled = status != 'off'
                self.row['route_weather']['status'] = 'partial' if status == 'partial' else 'ok'
                self.row['route_weather']['expires_at'] = Weather.iso(NOW.timestamp() + (-1 if status == 'expired' else 3600))
                self.assertEqual(self.overview()['rides'][0]['weather']['status'], status)

    def test_unresolved_later_date_filtered_but_unknown_date_not_invented(self):
        self.sync._routes_data = {'events': []}
        self.sync._overview_athlete = {}
        self.sync._overview_events = [dict(self.event, id=1, start_date_local='2026-11-15T10:00:00'),
                                      dict(self.event, id=2, start_date_local='unknown')]
        result = self.overview()
        self.assertEqual(result['rides_without_route_beyond_horizon'], 1)
        self.assertEqual([r['event_id'] for r in result['rides']], [2])

    def test_later_route_cannot_publish_an_old_map(self):
        with mock.patch.object(Map, 'png', return_value=b'fixture-png'):
            Map.generate(self.sync, [self.event], {'timezone': 'UTC'}, NOW)
        path = self.row['route_weather_map']['path']
        self.event['start_date_local'] = (NOW+timedelta(hours=121)).isoformat()
        result = self.overview()
        self.assertNotIn(path, result['managed_files'])
        self.assertEqual(result['rides'][0]['map'], {'present': False, 'reason': 'beyond_horizon'})


class WeatherExampleTests(unittest.TestCase):
    def test_eta_history_example_matches_persisted_writer_shape(self):
        root = Path(__file__).resolve().parents[2] / 'examples/json-examples'
        example = json.loads((root/'route_eta_history.example.json').read_text())
        self.assertEqual(example['owner_key'], 'synthetic-owner-key-not-a-real-athlete')
        self.assertEqual(len(example['activities']), 4)
        self.assertEqual({r['power_availability'] for r in example['activities']},
                         {'average', 'weighted_proxy', 'unavailable'})
        activities, previous = [], {}
        for row in example['activities']:
            activity = {key: row[key] for key in
                        ('id', 'type', 'start_date_local', 'distance', 'moving_time')}
            for stored, upstream in (
                    ('elapsed_time', 'elapsed_time'),
                    ('recorded_ascent_m', 'total_elevation_gain'),
                    ('average_power_w', 'average_watts'),
                    ('weighted_power_w', 'weighted_average_watts')):
                if stored in row:
                    activity[upstream] = row[stored]
            activities.append(activity)
            terrain = copy.deepcopy(row.get('terrain_summary', {}))
            if 'producer_ascent_m' in row:
                terrain['total_elevation_m'] = row['producer_ascent_m']
            previous[str(row['id'])] = {'terrain_summary': terrain}
        with tempfile.TemporaryDirectory() as tmp:
            sync = object.__new__(mod.IntervalsSync)
            sync.data_dir = Path(tmp)
            sync.athlete_id = 'synthetic-eta-example'
            result = sync._update_route_eta_history(
                activities, previous, full_history=True,
                now=datetime.fromisoformat(example['last_full_fetch_at']))
            self.assertEqual(result['persistence_status'], 'persisted')
            written = json.loads((sync.data_dir/Weather.ETA_HISTORY_FILE).read_text())
        self.assertNotIn('persistence_status', written)
        self.assertEqual(written['owner_key'], Weather.digest(sync.athlete_id))
        # The documentation uses an obvious placeholder instead of an owner digest.
        written['owner_key'] = example['owner_key']
        self.assertEqual(written, example)

    def test_example_json_valid_and_weather_binding_matches_producer(self):
        root = Path(__file__).resolve().parents[2] / 'examples/json-examples'
        for path in root.glob('*.json'):
            json.loads(path.read_text())
        latest = json.loads((root/'latest.example.json').read_text())
        routes = json.loads((root/'routes.example.json').read_text())
        row = next(r for r in routes['events'] if 'route_weather' in r)
        brief = row['route_weather']
        workout = next(w for w in latest['planned_workouts'] if w['id'] == row['event_id'])
        sync = object.__new__(mod.IntervalsSync)
        sync._planned_routes = {row['event_id']: row}
        summary = sync._planned_route_fields({'id': row['event_id']})['route_weather_summary']
        self.assertEqual(workout['route_weather_summary'], summary)
        self.assertEqual(brief['schema_version'], Weather.VERSION)
        bound = copy.deepcopy(brief)
        identity = bound.pop('brief_id')
        self.assertEqual(Weather.digest(bound), identity)
        self.assertEqual(Weather.forecast_bands(brief), brief['bands'])
        self.assertEqual(brief['tier'], 'forecast')
        self.assertTrue(brief['sections'])
        self.assertLessEqual(len(brief['sections']), 4)
        self.assertEqual(len(Map.scenes(row)), row['route_weather_map']['page_count'])
        descriptor = row['route_weather_map']
        for key in ('brief_id', 'cache_key', 'tier', 'expires_at'):
            self.assertEqual(descriptor[key], brief[key])
        self.assertEqual(len(mod.DataOverview.map_pages(descriptor)), descriptor['page_count'])
        overview = latest['data_overview']
        ride = next(r for r in overview['rides'] if r['event_id'] == row['event_id'])
        self.assertEqual(ride['map']['pages'][0]['path'], descriptor['pages'][0]['path'])
        self.assertEqual(ride['weather']['expires_at'], brief['expires_at'])
        self.assertIn('routes.json', overview['managed_files'])
        self.assertIn(descriptor['path'], overview['managed_files'])
        self.assertEqual(overview['rides_total'] - len(overview['rides']), overview['rides_omitted'])
        self.assertIsInstance(overview['rides_without_route_beyond_horizon'], int)
        # Newly added geometry is invented, not copied from the public Harz route.
        for _, lat, lon, _ in row['terrain_summary']['polyline']:
            self.assertTrue(45 <= lat <= 45.02 and 5 <= lon <= 5.02)


if __name__ == '__main__':
    unittest.main()
