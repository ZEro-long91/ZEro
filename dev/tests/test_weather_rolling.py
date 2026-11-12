"""Passed-start weather, local-date completion and bounded cache refresh."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from _harness import install_verb_guard, restore_verb_guard
import test_route_weather as fixtures

mod, Weather, NOW = fixtures.mod, fixtures.Weather, fixtures.NOW
Map = mod.RouteWeatherMap


def setUpModule():
    global guard
    guard = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, guard)


class RollingTests(unittest.TestCase):
    setUp = fixtures.RouteWeatherTests.setUp
    run_weather = fixtures.RouteWeatherTests.run_weather
    persist = fixtures.RouteWeatherTests.persist

    def test_provided_and_unset_starts_roll_and_bind_map_overview(self):
        for raw in ('2026-10-19T10:00:00', '2026-10-19T00:00:00', '2026-10-19'):
            with self.subTest(raw=raw):
                self.events[0]['start_date_local'] = raw
                self.now = NOW + timedelta(minutes=17)
                brief, request = self.run_weather(fixtures.window_success)
                self.assertEqual(brief['status'], 'ok')
                self.assertEqual(brief['departure'], {
                    'at_utc': '2026-10-19T10:30:00+00:00', 'timezone': 'UTC',
                    'source': 'assumed', 'assumed_reason': 'start_passed',
                    'planned_local': '2026-10-19T10:00:00+00:00'})
                self.assertEqual(brief['sections'][0]['arrival_window_utc'],
                                 ['2026-10-19T10:30:00+00:00'] * 2)
                self.assertIn('10:30', brief['bands'][0]['display']['time'])
                self.assertIn('(ASSUMED)', brief['summary']['display']['departure'])
                row = self.s._routes_data['events'][0]
                text = '\n'.join(op[3] for op in Map.scene(row) if op[0] == 'text')
                self.assertIn('assumed', text.lower())
                self.assertIn('10:30', text)
                with mock.patch.object(Map, 'png', return_value=b'synthetic-map'):
                    Map.generate(self.s, self.events, self.athlete)
                self.assertEqual(row['route_weather_map']['brief_id'], brief['brief_id'])
                self.s._overview_events, self.s._overview_athlete = self.events, self.athlete
                overview = mod.DataOverview.build(self.s, {})
                ride = overview['rides'][0]
                self.assertEqual(ride['start_time_status'], 'passed_not_completed')
                self.assertTrue(ride['departure_assumed'])
                self.assertIn('10:30', ride['departure_display'])
                self.assertTrue(ride['map']['present'])
                self.assertEqual(request.call_count, 1)

    def test_future_assumed_metadata_and_provided_binding(self):
        event = dict(self.events[0], start_date_local='2026-10-20')
        value = Weather.effective_departure(event, self.athlete, NOW)
        self.assertEqual(value['assumed_reason'], 'unset_time')
        self.assertEqual(value['planned_local'], '2026-10-20T10:00:00+00:00')
        self.assertEqual(Weather.effective_departure(self.events[0], self.athlete, NOW),
                         Weather.departure(self.events[0], self.athlete))

    def test_quarter_boundaries_and_display_zone_dst_transitions(self):
        cases = (
            ('2026-10-19T10:14:59.999999+02:00', '2026-10-19T08:15:00+00:00'),
            ('2026-10-19T10:15:00+02:00', '2026-10-19T08:30:00+00:00'),
            ('2026-03-29T01:59:59+01:00', '2026-03-29T01:00:00+00:00'),
            ('2026-10-25T02:59:59+02:00', '2026-10-25T01:00:00+00:00'))
        for raw, expected in cases:
            now = datetime.fromisoformat(raw)
            event = dict(self.events[0], start_date_local=raw[:10] + 'T00:01:00',
                         timezone='Europe/Copenhagen')
            result = Weather.effective_departure(event, {}, now)
            self.assertEqual(result['at_utc'], expected)
            self.assertGreater(Weather.parse_time(result['at_utc']), now.timestamp())
            self.assertEqual(result['timezone'], 'Europe/Copenhagen')

    def test_local_date_survives_utc_midnight_then_becomes_missed(self):
        self.events[0].update(start_date_local='2026-10-19T10:00:00', timezone='America/New_York')
        self.now = datetime(2026, 10, 20, 3, 59, tzinfo=timezone.utc)
        brief, _ = self.run_weather(fixtures.success)
        self.assertEqual(brief['status'], 'ok')
        self.assertEqual(brief['departure']['at_utc'], '2026-10-20T04:00:00+00:00')
        self.now += timedelta(minutes=1)
        brief, _ = self.run_weather()
        self.assertEqual(brief['reason_codes'], ['missed'])
        self.assertEqual(self.s._routes_data['route_weather_requests_this_sync'], 0)

    def test_exact_pair_completion_is_network_free_and_removes_map(self):
        self.events[0]['start_date_local'] = '2026-10-19T09:00:00+00:00'
        self.run_weather(fixtures.success)
        row = self.s._routes_data['events'][0]
        with mock.patch.object(Map, 'png', return_value=b'synthetic-map'):
            Map.generate(self.s, self.events, self.athlete)
        path = self.s.data_dir / row['route_weather_map']['path']
        self.assertTrue(path.exists())
        # An exact pairing is authoritative even for an indoor activity.
        self.activities = [{'id': 'done', 'type': 'VirtualRide', 'paired_event_id': 7}]
        brief, _ = self.run_weather()
        self.assertEqual(brief['reason_codes'], ['completed'])
        Map.generate(self.s, self.events, self.athlete)
        self.assertFalse(path.exists())
        self.assertNotIn('route_weather_map', self.s._routes_data['events'][0])
        self.s._overview_events, self.s._overview_athlete = self.events, self.athlete
        ride = mod.DataOverview.build(self.s, {})['rides'][0]
        self.assertEqual(ride['weather']['reason'], 'completed')
        self.assertFalse(ride['map']['present'])

    def test_unpaired_outdoor_completion_inclusive_two_hour_boundary(self):
        self.events[0]['start_date_local'] = '2026-10-19T10:00:00'
        self.now = NOW + timedelta(minutes=17)
        for kind in ('Ride', 'GravelRide', 'MountainBikeRide'):
            self.activities = [{'type': kind, 'start_date_local': '2026-10-19T08:00:00'}]
            self.assertEqual(self.run_weather()[0]['reason_codes'], ['completed'])
        self.events[0]['timezone'] = 'Europe/Copenhagen'
        self.activities = [{'type': 'Ride', 'start_date': '2026-10-19T06:00:00Z'}]
        self.assertEqual(self.run_weather()[0]['reason_codes'], ['completed'])

    def test_nearby_noncompletions_keep_weather(self):
        self.events[0]['start_date_local'] = '2026-10-19T10:00:00'
        self.now = NOW + timedelta(minutes=17)
        base = {'type': 'Ride', 'start_date_local': '2026-10-19T09:00:00'}
        negatives = [
            dict(base, type='VirtualRide'), dict(base, trainer=True), dict(base, indoor=True),
            dict(base, virtual=True), dict(base, is_virtual=True), dict(base, sub_type='virtual'),
            dict(base, type='Run'), dict(base, start_date_local='2026-10-19T07:59:59'),
            dict(base, start_date_local='2026-10-18T23:30:00'),
            dict(base, start_date_local='2026-10-19T11:00:00'),
            {'type': 'Ride', 'start_date_local': 'invalid'},
            {'type': 'VirtualRide', 'paired_event_id': 8}]
        for activity in negatives:
            with self.subTest(activity=activity):
                self.activities = [activity]
                self.assertEqual(self.run_weather(fixtures.success)[0]['status'], 'ok')

    def test_first_snapshot_before_start_survives_rolling_latest(self):
        self.events[0]['start_date_local'] = '2026-10-19T10:00:00+00:00'
        self.now = NOW - timedelta(minutes=1)
        self.run_weather(fixtures.success)
        first = copy.deepcopy(self.s._routes_data['brief_snapshots'][0])
        self.persist()
        self.now = NOW + timedelta(minutes=17)
        brief, _ = self.run_weather()  # cached covering response, no request
        snapshots = self.s._routes_data['brief_snapshots']
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0], first)
        self.assertEqual(snapshots[1]['brief']['departure'], brief['departure'])
        self.assertEqual(snapshots[1]['departure_basis'],
                         'assumed after passed start; actual departure not known')

    def simulate(self, provider):
        self.events[0]['start_date_local'] = '2026-10-19T09:00:00+00:00'
        counts, departures, first = [], [], None
        for minute in range(120):
            self.now = NOW + timedelta(minutes=minute)
            brief, request = self.run_weather(provider)
            self.assertEqual(brief['status'], 'ok')
            counts.append(request.call_count)
            departures.append(brief['departure']['at_utc'])
            self.assertEqual(counts[-1], self.s._routes_data['route_weather_requests_this_sync'])
            self.assertLessEqual(counts[-1], 8)
            snapshots = self.s._routes_data['brief_snapshots']
            if first is None:
                first = copy.deepcopy(snapshots[0])
            self.assertEqual(snapshots[0], first)
            self.assertLessEqual(len(snapshots), 2)
            self.assertEqual(snapshots[-1]['brief']['brief_id'], brief['brief_id'])
            self.persist()
        self.assertEqual(len(set(departures)), 8)
        self.assertEqual(len(self.s._routes_data['brief_snapshots']), 2)
        return counts

    def test_two_hours_window_limited_provider_reuses_covering_cache(self):
        counts = self.simulate(fixtures.window_success)
        self.assertLessEqual(sum(counts), 5)
        self.assertGreaterEqual(counts.count(0), 115)

    def test_two_hours_broad_provider_reuses_covering_cache(self):
        counts = self.simulate(fixtures.success)
        self.assertEqual(sum(counts), 2)

    def test_rolling_timeout_split_respects_eight_request_budget_and_backoff(self):
        self.events[0]['start_date_local'] = '2026-10-19T09:00:00+00:00'
        def timeout(*args, **kwargs):
            raise mod.requests.exceptions.ReadTimeout('synthetic')
        with mock.patch.object(mod.time, 'monotonic', return_value=0):
            brief, request = self.run_weather(timeout)
        self.assertEqual(request.call_count, 8)
        self.assertEqual(brief['status'], 'unavailable')
        self.persist()
        self.now += timedelta(minutes=1)
        self.run_weather()
        self.assertEqual(self.s._routes_data['route_weather_requests_this_sync'], 0)


if __name__ == '__main__':
    unittest.main()
