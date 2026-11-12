"""Offline C1-C3 regressions, including real push HTTP policy with mock responses."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import requests
from _harness import PUSH_PATH, RefuseEverything, install_verb_guard, load_module_by_path, restore_verb_guard
from test_route_weather_map import mod, Map, Weather, NOW, fixture
from test_data_overview import tiny_png
import test_route_weather as weather_fixtures

push = load_module_by_path('s11_push_time', PUSH_PATH)


def setUpModule():
    global guard, original_push
    guard = install_verb_guard(mod.requests)
    original_push = push._requests
    push._requests = RefuseEverything()


def tearDownModule():
    restore_verb_guard(mod.requests, guard)
    push._requests = original_push


class DepartureTests(unittest.TestCase):
    def test_midnight_and_date_only_assume_ten_but_nearby_times_are_provided(self):
        for raw in ('2026-10-20', '2026-10-20T00:00:00', '2026-10-20T00:00:00.000'):
            departure = Weather.departure({'start_date_local': raw}, {'timezone': 'Europe/Copenhagen'})
            self.assertEqual(departure['source'], 'assumed')
            self.assertEqual(departure['at_utc'], '2026-10-20T08:00:00+00:00')
        for time in ('00:01:00', '23:59:00', '00:00:01', '00:00:00.001', '13:45:00'):
            raw = '2026-10-20T' + time
            departure = Weather.departure({'start_date_local': raw}, {'timezone': 'Europe/Copenhagen'})
            self.assertEqual(departure['source'], 'athlete_timezone')
            self.assertEqual(mod.IntervalsSync._event_start_fields({'start_date_local': raw})['start_time_status'], 'provided')

    def test_configuration_precedence_validation_and_midnight_default(self):
        with mock.patch.dict(mod.os.environ, {}, clear=True):
            self.assertEqual(Weather.default_start({}), '10:00')
            self.assertEqual(Weather.default_start({'route_weather_default_start': '09:15'}), '09:15')
        with mock.patch.dict(mod.os.environ, {'SECTION11_ROUTE_WEATHER_DEFAULT_START': '11:45'}):
            self.assertEqual(Weather.default_start({'route_weather_default_start': '09:15'}), '11:45')
        event = {'start_date_local': '2026-10-20T00:00:00'}
        self.assertIn('09:15:', Weather.departure(event, {'timezone': 'UTC'}, '09:15')['at_utc'])
        self.assertIn('00:00:', Weather.departure(event, {'timezone': 'UTC'}, '00:00')['at_utc'])
        for value in ('24:00', '9:00', '10:60', '', None, 10):
            with self.assertRaisesRegex(ValueError, 'invalid_default_start'):
                Weather.departure(event, {'timezone': 'UTC'}, value)
        self.assertEqual(Weather.departure({'start_date_local': '2026-10-20T09:00:00Z'}, {}, 'bad')['source'], 'event_explicit_offset')

    def test_default_uses_departure_offset_on_dst_transition_dates(self):
        for date, utc in (('2026-03-29', '08:00'), ('2026-10-25', '09:00')):
            for suffix in ('', 'T00:00:00', 'T00:00:00+01:00'):
                result = Weather.departure({'start_date_local': date + suffix}, {'timezone': 'Europe/Copenhagen'})
                self.assertEqual(result['at_utc'], date + 'T' + utc + ':00+00:00')
                self.assertEqual(result['source'], 'assumed')
            with self.assertRaisesRegex(ValueError, 'ambiguous_or_nonexistent'):
                Weather.departure({'start_date_local': date}, {'timezone': 'Europe/Copenhagen'}, '02:30')

    def test_midnight_status_truthful_and_invalid_input_still_unavailable(self):
        self.assertEqual(mod.IntervalsSync._event_start_fields({'start_date_local': '2026-10-20T00:00:00'}),
                         {'start_time_status': 'midnight_treated_as_unset'})
        self.assertEqual(mod.IntervalsSync._event_start_fields({'start_date_local': '2026-10-20'}), {'start_time_status': 'missing'})
        for value in (None, '', 'nonsense', '2026-99-99', '2026-10-20Tbad'):
            with self.assertRaises(ValueError):
                Weather.departure({'start_date_local': value}, {'timezone': 'UTC'})


class DisplayAndOrderingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.sync = object.__new__(mod.IntervalsSync)
        self.sync.data_dir = Path(temp.name)
        self.sync.athlete_id, self.sync.debug = 'synthetic', False
        self.sync.route_weather_enabled = True
        self.sync.route_weather_default_start = '10:00'

    def assumed_fixture(self):
        original = Weather.departure
        def midnight(event, athlete, default_start='10:00'):
            event = {**event, 'start_date_local': '2026-10-20T00:00:00'}
            return original(event, athlete, default_start)
        with mock.patch.object(Weather, 'departure', side_effect=midnight):
            row, event = fixture(self.sync, athlete={'timezone': 'Europe/Copenhagen'})
        event['start_date_local'] = '2026-10-20T00:00:00'
        self.sync._routes_data = {'events': [row]}
        self.sync._overview_events = [event]
        self.sync._overview_athlete = {'timezone': 'Europe/Copenhagen'}
        return row, event

    def test_assumed_brief_map_overview_and_first_snapshot_binding(self):
        row, event = self.assumed_fixture()
        self.assertIn('10:00 CEST+0200 (ASSUMED)', row['route_weather']['summary']['display']['departure'])
        labels = [item[3] for item in Map.scene(row) if item[0] == 'text']
        self.assertIn('assumed', labels)
        self.assertIn('START', labels)
        self.assertIn('10:00', labels)
        self.assertTrue(any('Tue 20 Oct' in value and 'CEST' in value and '+0200' not in value for value in labels))
        with mock.patch.object(Map, 'png', return_value=tiny_png()):
            Map.generate(self.sync, [event], self.sync._overview_athlete, NOW)
        overview = mod.DataOverview.build(self.sync, {}, now=NOW)
        ride = overview['rides'][0]
        self.assertTrue(ride['departure_assumed'])
        self.assertEqual(ride['start_time_status'], 'midnight_treated_as_unset')
        self.assertTrue(ride['map']['present'])
        self.assertIn('(ASSUMED)', ride['departure_display'])
        producer = Weather(self.sync, NOW)
        producer.save_snapshot(row['route_weather'])
        first = copy.deepcopy(producer.snapshots[0])
        later = copy.deepcopy(row['route_weather'])
        later['departure'] = Weather.departure({**event, 'start_date_local': '2026-10-20T09:00:00'}, self.sync._overview_athlete)
        later['brief_id'] = Weather.digest(later)
        producer.save_snapshot(later)
        self.assertEqual(len(producer.snapshots), 2)
        self.assertEqual(producer.snapshots[0], first)
        self.assertNotEqual(producer.snapshots[1]['brief']['departure'], first['brief']['departure'])

    def test_overview_first_or_immediately_after_read_this_first_on_disk(self):
        for first in (False, True):
            data = {'metadata': {'kept': True}, 'recent_activities': []}
            if first:
                data['READ_THIS_FIRST'] = {'existing': 'kept'}
            with redirect_stdout(io.StringIO()):
                self.sync.save_local_outputs(data, self.sync.data_dir / 'latest.json')
            saved = json.loads((self.sync.data_dir / 'latest.json').read_text())
            expected = ['READ_THIS_FIRST', 'data_overview'] if first else ['data_overview']
            self.assertEqual(list(saved)[:len(expected)], expected)
            self.assertEqual(saved['metadata'], {'kept': True})
            if first:
                self.assertEqual(saved['READ_THIS_FIRST']['existing'], 'kept')
                self.assertIn('data_overview', saved['READ_THIS_FIRST']['data_overview'])

    def test_configured_default_reaches_brief_map_binding_and_overview(self):
        self.sync.route_weather_default_start = '11:15'
        row, event = self.assumed_fixture()
        self.assertIn('11:15 CEST+0200 (ASSUMED)', row['route_weather']['summary']['display']['departure'])
        with mock.patch.object(Map, 'png', return_value=tiny_png()):
            Map.generate(self.sync, [event], self.sync._overview_athlete, NOW)
        overview = mod.DataOverview.build(self.sync, {}, now=NOW)
        self.assertTrue(overview['rides'][0]['map']['present'])
        self.assertIn('11:15 CEST+0200 (ASSUMED)', overview['rides'][0]['departure_display'])

    def test_compact_outlook_clusters_variable_and_missing(self):
        self.assertEqual(Map.outlook_wind([270] * 8 + [225] * 2), 'MOSTLY W')
        self.assertEqual(Map.outlook_wind([225, 247.5, 270] * 4), 'MOSTLY SW-W')
        self.assertEqual(Map.outlook_wind([337.5, 0] * 4), 'MOSTLY NNW-N')
        self.assertEqual(Map.outlook_wind([i * 22.5 for i in range(16)]), 'VARIABLE')
        self.assertEqual(Map.outlook_wind([270] * 3 + [90]), 'MOSTLY W')
        self.assertEqual(Map.outlook_wind([270] * 7 + [90] * 3), 'VARIABLE')
        self.assertEqual(Map.outlook_wind([None, float('nan')]), 'UNKNOWN')
        row, _ = fixture(self.sync, outlook=True)
        self.assertEqual(row['route_weather']['outlook']['display_wind_from'], 'VARIABLE')
        self.assertIn('Wind variable', json.dumps(Map.scene(row)))
        self.assertNotIn('display_wind_from_sectors', row['route_weather']['outlook'])

    def test_assumed_start_sorts_after_earlier_provided_starts_in_budget_and_overview(self):
        row, event = self.assumed_fixture()
        events = [{**event, 'id': i, 'start_date_local': '2026-10-20T' + ('00:00:00' if i == 0 else f'08:0{i}:00')}
                  for i in range(7)]
        self.sync._routes_data = {'events': [{**copy.deepcopy(row), 'event_id': i} for i in range(7)]}
        self.sync._overview_events = events
        with mock.patch.object(mod.requests, 'get', side_effect=weather_fixtures.success):
            Weather(self.sync, NOW).generate(events, [], {}, self.sync._overview_athlete)
        self.assertEqual(self.sync._routes_data['events'][0]['route_weather']['reason_codes'][0], 'event_budget_exhausted')
        self.assertEqual(self.sync._routes_data['events'][6]['route_weather']['status'], 'ok')
        self.assertEqual([r['event_id'] for r in mod.DataOverview.build(self.sync, {}, now=NOW)['rides']], [1, 2, 3, 4, 5, 6, 0])

    def test_today_assumed_time_remains_visible_for_clarification_after_default_passes(self):
        row, event = self.assumed_fixture()
        event['start_date_local'] = '2026-10-19T00:00:00'
        with mock.patch.object(mod.requests, 'get', side_effect=weather_fixtures.success):
            Weather(self.sync, NOW).generate([event], [], {}, self.sync._overview_athlete)
        overview = mod.DataOverview.build(self.sync, {}, now=NOW)
        self.assertEqual(len(overview['rides']), 1)
        self.assertTrue(overview['rides'][0]['departure_assumed'])
        self.assertEqual(overview['rides'][0]['start_time_status'], 'passed_not_completed')
        self.assertEqual(overview['rides'][0]['weather']['status'], 'ok')


class PushTimeTests(unittest.TestCase):
    def setUp(self):
        self.pusher = push.IntervalsPush('i123456', 'synthetic')
        self.transport = mock.Mock()
        self.transport.exceptions = requests.exceptions
        self.transport.get.side_effect = AssertionError('unexpected read')
        self.transport.put.side_effect = AssertionError('unexpected write')
        self.transport.post.side_effect = AssertionError('unexpected write')
        self.transport.delete.side_effect = AssertionError('unexpected write')
        self.patch = mock.patch.object(push, '_requests', self.transport)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    @staticmethod
    def response(body):
        result = mock.Mock(status_code=200, headers={})
        result.json.return_value = body
        result.raise_for_status.return_value = None
        return result

    def test_push_optional_time_and_preview_show_exact_time(self):
        for time in (None, '00:00', '00:01', '23:59', '09:30'):
            workout = {'name': 'Synthetic', 'date': '2099-01-01'}
            if time is not None:
                workout['time'] = time
            self.assertTrue(self.pusher.validate_workout(workout)[0])
            self.assertEqual(self.pusher._build_event(workout)['start_date_local'], '2099-01-01T' + (time or '00:00') + ':00')
            self.assertEqual(self.pusher.preview_push([workout])['summary'][0]['time'], time or '00:00')
        self.transport.put.assert_not_called()
        self.transport.post.assert_not_called()

    def test_invalid_times_are_rejected_before_http(self):
        for time in ('24:00', '9:30', '09:60', '09:30:00', '', 9, None):
            self.assertFalse(self.pusher.validate_workout({'name': 'Synthetic', 'date': '2099-01-01', 'time': time})[0])
            if time is not None:
                self.assertFalse(self.pusher.preview_move(42, '2099-01-01', time)['success'])
                self.assertEqual(self.pusher.move_event(42, '2099-01-01', time)['outcome'], 'not_applied')
        self.transport.get.assert_not_called()
        self.transport.put.assert_not_called()

    def test_confirmed_push_sends_per_workout_time_after_pre_read(self):
        workout = {'name': 'Synthetic', 'date': '2099-01-01', 'time': '09:30', 'external_id': 'synthetic-time'}
        self.transport.get.side_effect = None
        self.transport.get.return_value = self.response([])
        self.transport.post.side_effect = None
        self.transport.post.return_value = self.response([{**self.pusher._build_event(workout), 'id': 42}])
        self.assertEqual(self.pusher.push_workouts([workout])['outcome'], 'applied')
        self.assertEqual(self.transport.post.call_args.kwargs['json'][0]['start_date_local'], '2099-01-01T09:30:00')
        self.assertEqual(self.transport.post.call_count, 1)
        self.assertEqual(self.transport.get.call_count, 1)

    def test_move_preserves_existing_time_and_seconds_with_one_pre_read_one_put(self):
        for time in ('09:45:27', '00:00:00', '23:59:00', '09:45:27.123'):
            self.transport.reset_mock()
            self.transport.get.side_effect = None
            self.transport.get.return_value = self.response({'id': 42, 'start_date_local': '2099-01-01T' + time})
            self.transport.put.side_effect = None
            self.transport.put.return_value = self.response({'id': 42, 'start_date_local': '2099-01-02T' + time})
            self.assertEqual(self.pusher.move_event(42, '2099-01-02')['outcome'], 'applied')
            self.assertEqual(self.transport.put.call_args.kwargs['json'], {'start_date_local': '2099-01-02T' + time})
            self.assertEqual(self.transport.get.call_count, 1)
            self.assertEqual(self.transport.put.call_count, 1)

    def test_same_date_time_only_preview_and_confirmed_update(self):
        self.transport.get.side_effect = None
        self.transport.get.return_value = self.response({'id': 42, 'start_date_local': '2099-01-01T00:00:00'})
        preview = self.pusher.preview_move(42, '2099-01-01', '10:30')
        self.assertEqual(preview['from_time'], '00:00:00')
        self.assertEqual(preview['to_time'], '10:30:00')
        self.transport.put.assert_not_called()
        self.transport.put.side_effect = None
        self.transport.put.return_value = self.response({'id': 42, 'start_date_local': '2099-01-01T10:30:00'})
        self.assertEqual(self.pusher.move_event(42, '2099-01-01', '10:30')['outcome'], 'applied')
        self.assertEqual(self.transport.put.call_args.kwargs['json'], {'start_date_local': '2099-01-01T10:30:00'})

    def test_ambiguous_put_reads_back_exact_time_without_retry(self):
        for actual, expected in (('10:30', 'applied'), ('10:31', 'unknown')):
            self.transport.reset_mock()
            self.transport.get.side_effect = [self.response({'id': 42, 'start_date_local': '2099-01-01T09:00:00'}),
                                              self.response({'id': 42, 'start_date_local': '2099-01-02T' + actual + ':00'})]
            self.transport.put.side_effect = requests.Timeout('synthetic timeout')
            self.assertEqual(self.pusher.move_event(42, '2099-01-02', '10:30')['outcome'], expected)
            self.assertEqual(self.transport.put.call_count, 1)
            self.assertEqual(self.transport.get.call_count, 2)

    def test_unusable_existing_time_fails_closed_but_explicit_time_resolves_it(self):
        self.transport.get.side_effect = None
        self.transport.get.return_value = self.response({'id': 42, 'start_date_local': 'bad'})
        self.assertEqual(self.pusher.move_event(42, '2099-01-01')['outcome'], 'not_applied')
        self.transport.put.assert_not_called()
        self.transport.put.side_effect = None
        self.transport.put.return_value = self.response({'id': 42})
        self.assertEqual(self.pusher.move_event(42, '2099-01-01', '10:00')['outcome'], 'applied')

    def test_cli_time_is_preview_by_default(self):
        for args, expected in ((['push', '--name', 'Synthetic', '--date', '2099-01-01', '--time', '11:15'], '11:15'),
                               (['move', '--event-id', '42', '--date', '2099-01-01', '--time', '11:15'], '11:15:00')):
            self.transport.get.side_effect = None
            self.transport.get.return_value = self.response({'id': 42, 'start_date_local': '2099-01-01T00:00:00'})
            output = io.StringIO()
            with mock.patch('sys.argv', ['push.py', '--athlete-id', 'i123456', '--api-key', 'synthetic'] + args), redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
                push.main()
            self.assertEqual(stopped.exception.code, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result['mode'], 'preview')
            self.assertEqual(result['summary'][0]['time'] if args[0] == 'push' else result['to_time'], expected)
        self.transport.post.assert_not_called()
        self.transport.put.assert_not_called()
