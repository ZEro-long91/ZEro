"""
Tests that recent_activities[] keeps legitimate zero values (sync.py v3.139).

Standard library only: unittest and unittest.mock. Activities are synthetic and
built inline; no network, real athlete data, credentials or machine-specific path.

Up to v3.138, _format_activities built decoupling, avg_temp and wind_speed with an
"or" fallback chain. A real 0 (0.0 % decoupling, 0 °C, calm air) was treated as
missing and became null, or the next source in the chain was used instead.

What this module guards:

  1. Zero is kept for decoupling, avg_temp and wind_speed, including when a later
     fallback source holds a different value.
  2. Fallback still works when the first source is absent or null.
  3. Fields where an upstream 0 means "no sensor or no data" (power, NP, HR,
     max HR, cadence, speed, work, calories) and humidity keep their previous
     behaviour: 0 still reads as null. This is deliberate, not an oversight.
"""

import unittest
from unittest import mock

from _harness import (SYNC_PATH, install_verb_guard, load_module_by_path,
                      restore_verb_guard)

sync_mod = load_module_by_path("s11_sync_zero_values", SYNC_PATH)
IntervalsSync = sync_mod.IntervalsSync
_originals = None


def setUpModule():
    global _originals
    _originals = install_verb_guard(sync_mod.requests)


def tearDownModule():
    restore_verb_guard(sync_mod.requests, _originals)


def _out(**fields):
    act = {"id": "i1", "start_date_local": "2026-10-01T09:00:00",
           "type": "Ride", "name": "Synthetic ride", "moving_time": 3600}
    act.update(fields)
    s = IntervalsSync("athlete_test", "key_test")
    s._intervals_data = {}  # set during a normal run before formatting
    # Terrain, weather and chat notes are separate per-activity enrichments that
    # can reach the network; they are neutralised so only field formatting runs.
    with mock.patch.object(IntervalsSync, "_build_terrain_for_activity", return_value={}), \
            mock.patch.object(IntervalsSync, "_build_weather_for_activity", return_value={}), \
            mock.patch.object(IntervalsSync, "_get_activity_messages", return_value=("ok", [])):
        rows = s._format_activities([act])
    assert len(rows) == 1
    return rows[0]


class Decoupling(unittest.TestCase):
    def test_zero_is_kept(self):
        self.assertEqual(_out(icu_hr_decoupling=0.0)["decoupling"], 0.0)
        self.assertEqual(_out(icu_hr_decoupling=0)["decoupling"], 0)

    def test_zero_wins_over_legacy_field(self):
        self.assertEqual(_out(icu_hr_decoupling=0.0, decoupling=7.5)["decoupling"], 0.0)

    def test_nonzero_and_negative_unchanged(self):
        self.assertEqual(_out(icu_hr_decoupling=4.2)["decoupling"], 4.2)
        self.assertEqual(_out(icu_hr_decoupling=-3.9)["decoupling"], -3.9)

    def test_null_or_absent_falls_back(self):
        self.assertEqual(_out(icu_hr_decoupling=None, decoupling=5.0)["decoupling"], 5.0)
        self.assertEqual(_out(decoupling=0.0)["decoupling"], 0.0)

    def test_all_absent_is_none(self):
        self.assertIsNone(_out()["decoupling"])


class Temperature(unittest.TestCase):
    def test_zero_is_kept(self):
        self.assertEqual(_out(average_weather_temp=0.0)["avg_temp"], 0.0)

    def test_zero_weather_not_replaced_by_device(self):
        self.assertEqual(_out(average_weather_temp=0.0, average_temp=4.0)["avg_temp"], 0.0)

    def test_negative_and_positive_unchanged(self):
        self.assertEqual(_out(average_weather_temp=-2.5)["avg_temp"], -2.5)
        self.assertEqual(_out(average_weather_temp=11.0)["avg_temp"], 11.0)

    def test_fallback_order_on_absent_or_null(self):
        self.assertEqual(_out(average_temp=0.0, avg_temp=6.0)["avg_temp"], 0.0)
        self.assertEqual(_out(average_weather_temp=None, average_temp=3.0)["avg_temp"], 3.0)
        self.assertEqual(_out(average_temperature=-1.0)["avg_temp"], -1.0)

    def test_all_absent_is_none(self):
        self.assertIsNone(_out()["avg_temp"])


class Wind(unittest.TestCase):
    def test_calm_is_kept(self):
        self.assertEqual(_out(average_wind_speed=0.0)["wind_speed"], 0.0)
        self.assertEqual(_out(average_wind_speed=0.0, wind_speed=3.0)["wind_speed"], 0.0)

    def test_fallback_and_absent(self):
        self.assertEqual(_out(wind_speed=2.5)["wind_speed"], 2.5)
        self.assertIsNone(_out()["wind_speed"])


class NoSensorZeroStaysNull(unittest.TestCase):
    """Deliberately unchanged: an upstream 0 here means no sensor or no data."""

    def test_zero_reads_as_null(self):
        row = _out(average_watts=0, weighted_average_watts=0, average_heartrate=0,
                   max_heartrate=0, average_cadence=0, average_speed=0, max_speed=0,
                   icu_joules=0, calories=0, humidity=0)
        for key in ("avg_power", "normalized_power", "avg_hr", "max_hr", "avg_cadence",
                    "avg_speed", "max_speed", "work_kj", "calories", "humidity"):
            with self.subTest(key=key):
                self.assertIsNone(row[key])

    def test_real_values_unchanged(self):
        row = _out(average_watts=180, average_heartrate=138, average_cadence=88)
        self.assertEqual((row["avg_power"], row["avg_hr"], row["avg_cadence"]), (180, 138, 88))


if __name__ == "__main__":
    unittest.main()
