"""Report-only multisport selection and coverage, entirely synthetic and offline."""
import copy
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from _harness import (SYNC_PATH, install_verb_guard, load_module_by_path,
                      restore_verb_guard)
from test_report_intensity import activity, collect
from test_sleep_context import Clock, D

mod = load_module_by_path("s11_multisport", SYNC_PATH)
originals = None


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


def by_date(acts):
    result = defaultdict(list)
    for act in acts:
        result[act["start_date_local"][:10]].append(act)
    return result


class MultisportIntensity(unittest.TestCase):
    def setUp(self):
        self.sync = mod.IntervalsSync("synthetic", "synthetic")
        clock = mock.patch.object(mod, "datetime", Clock)
        clock.start()
        self.addCleanup(clock.stop)

    def block(self, acts, start="2026-06-09", end=D, **kwargs):
        return self.sync._report_intensity(acts, start, end, as_of=D, **kwargs)

    def test_design_share_scenarios(self):
        cases = [
            ([("Ride", 93.8, 80), ("Walk", 3.9, 16.6), ("VirtualSki", 2.3, 3.4)], ["cycling"]),
            ([("Ride", 60, 50), ("Run", 28, 30), ("Swim", 12, 20)], ["cycling", "running", "swimming"]),
            ([("Ride", 75, 60), ("NordicSki", 25, 40)], ["cycling", "nordic_skiing"]),
            ([("Ride", 88, 7), ("Hike", 12, 35)], ["cycling"]),
            ([("Run", 88, 75), ("Ride", 12, 25)], ["cycling", "running"]),
            ([("Ride", 92, 1), ("Walk", 8, 2)], ["cycling"]),
            ([("WeightTraining", 900, 8), ("Run", 80, 1.5), ("Ride", 20, .5)], ["cycling", "running"]),
            ([("Ride", 92, 55), ("Walk", 8, 45)], ["cycling"]),
            ([("Ride", 62, 50), ("Run", 30, 30), ("Swim", 8, 20)], ["cycling", "running"]),
        ]
        for spec, expected in cases:
            with self.subTest(spec=spec):
                acts = [activity(str(i), s, tss=l, moving_time=t * 3600)
                        for i, (s, l, t) in enumerate(spec)]
                self.assertEqual(self.block(acts)["selected_sports"], expected)

    def test_exact_report_map_and_no_title_inference(self):
        types = list(self.sync.REPORT_SPORTS) + ["WeightTraining", "Yoga", "Workout", "Other", "Unknown", "ride"]
        acts = [activity(str(i), t, name="Main sport cycling", tss=10) for i, t in enumerate(types)]
        result = self.block(acts)
        self.assertEqual(result["denominator"]["sessions"], 26)
        self.assertEqual(set(result["sports"]), self.sync.LOAD_SPORTS)
        self.assertEqual(result["sports"]["nordic_skiing"]["sessions"], 4)
        self.assertTrue({"walking", "hiking", "e_cycling"}.isdisjoint(result["sports"]))
        self.assertEqual(self.sync.SPORT_FAMILIES["EBikeRide"], "cycling")
        self.assertEqual(self.sync.SPORT_FAMILIES["Hike"], "walk")

    def test_inclusive_unrounded_thresholds_and_all_reasons(self):
        for share, expected in ((9.9999, False), (10, True), (10.0001, True)):
            b = self.block([activity("b", tss=100-share, moving_time=9000),
                            activity("w", "Run", tss=share, moving_time=1000)])
            self.assertEqual(b["sports"]["running"]["selected"], expected)
        for share, expected in ((49.9999, False), (50, True), (50.0001, True)):
            b = self.block([activity("b", tss=99, moving_time=100-share),
                            activity("w", "Run", tss=1, moving_time=share)])
            self.assertEqual(b["sports"]["running"]["selected"], expected)
        self.sync.report_main_sports = ["cycling"]
        b = self.block([activity("b")])
        self.assertEqual(b["sports"]["cycling"]["selection_reasons"],
                         ["period_primary", "load_share_10pct", "time_share_50pct", "explicit_main_sport"])

    def test_missing_load_is_not_zero_and_no_ten_percent_time_fallback(self):
        acts = [activity("b", tss=None, moving_time=5000),
                activity("r", "Run", tss=30, moving_time=3000),
                activity("s", "Swim", tss=20, moving_time=2000)]
        b = self.block(acts)
        self.assertEqual(b["selected_sports"], ["cycling"])
        self.assertEqual(b["primary_basis"], "moving_time")
        self.assertIsNone(b["denominator"]["tss"])
        self.assertEqual(b["denominator"]["known_tss"], 50)
        self.assertEqual(b["sports"]["running"]["qualification"], "indeterminate")
        self.assertTrue(all(s["load_share"] is None for s in b["sports"].values()))
        for a in acts:
            a["icu_training_load"] = None
        b = self.block(acts)
        self.assertEqual(b["selected_sports"], ["cycling"])
        self.assertEqual(b["denominator"]["missing_load"], 3)
        for a in acts:
            a["icu_training_load"] = 0
        b = self.block(acts)
        self.assertEqual(b["selected_sports"], ["cycling"])
        self.assertEqual(b["denominator"]["tss"], 0)
        self.assertEqual(b["sports"]["running"]["qualification"], "not_selected")

    def test_invalid_values_disable_only_their_routes(self):
        for bad in (None, True, "12", -1, float("nan"), float("inf")):
            b = self.block([activity("b", tss=80, moving_time=bad),
                            activity("w", "Run", tss=20)])
            self.assertEqual(b["selected_sports"], ["cycling", "running"])
            self.assertIsNone(b["denominator"]["moving_seconds"])
            b = self.block([activity("b", tss=bad), activity("r", "Run", tss=20, moving_time=1)])
            self.assertEqual(b["selected_sports"], ["cycling"])
            self.assertEqual(b["denominator"]["missing_load"], 1)
        b = self.block([activity("b", tss=None, moving_time=None)])
        self.assertEqual(b["selected_sports"], [])
        self.assertEqual(b["sports"]["cycling"]["qualification"], "indeterminate")

    def test_explicit_main_sport_only_when_present(self):
        self.sync.report_main_sports = self.sync._parse_report_main_sports("swimming,rowing,swimming")
        acts = [activity("b", tss=99), activity("s", "Swim", tss=1, moving_time=10,
                  icu_zone_times=[], pace_zone_times=[0, 10, 0, 0, 0])]
        b = self.block(acts)
        self.assertEqual(b["selected_sports"], ["cycling", "swimming"])
        self.assertEqual(b["sports"]["swimming"]["selection_reasons"], ["explicit_main_sport"])
        self.assertEqual(b["sports"]["swimming"]["zone_status"], "zones_unavailable")
        self.assertNotIn("rowing", b["sports"])
        for bad in ("Swim", "strength", True, [1]):
            with self.assertRaises(ValueError):
                self.sync._parse_report_main_sports(bad)

    def test_ties_and_duplicate_ids_are_input_order_independent(self):
        acts = [activity("b", tss=80), activity("r", "Run", tss=80)]
        self.assertEqual(self.block(acts), self.block(list(reversed(acts))))
        self.assertEqual(self.block(acts)["primary_sport"], "cycling")
        acts[1]["moving_time"] = 3601
        self.assertEqual(self.block(acts)["primary_sport"], "running")
        duplicate = self.block(acts + [copy.deepcopy(acts[0])])
        self.assertEqual(duplicate["denominator"]["sessions"], 2)
        self.assertEqual(duplicate["identity"]["duplicate_records"], 1)
        self.assertEqual(duplicate["sports"]["cycling"]["by_basis"][0]["total_seconds"], 3600)
        conflict = copy.deepcopy(acts[0])
        conflict["icu_training_load"] = 81
        a, b = self.block(acts + [conflict]), self.block([conflict] + list(reversed(acts)))
        self.assertEqual(a, b)
        self.assertEqual(a["identity"]["conflicting_ids"], 1)
        self.assertIsNone(a["denominator"]["tss"])
        self.assertEqual(a["sports"]["cycling"]["zone_status"], "zones_unavailable")

    def test_seasonal_ski_rescue_and_quiet_week_rejection(self):
        start = datetime(2025, 12, 22)
        acts = []
        for i in range(24):
            acts.append(activity(str(i), "Ride" if i < 20 else "NordicSki",
                date=(start + timedelta(days=7*i)).strftime("%Y-%m-%d"),
                tss=92/20 if i < 20 else 8/4, moving_time=(4.4 if i < 20 else 3)*3600))
        end = (start + timedelta(days=24*7-1)).strftime("%Y-%m-%d")
        b = self.block(acts, start.strftime("%Y-%m-%d"), end, weekly_rescue=True)
        self.assertEqual(b["selected_sports"], ["cycling", "nordic_skiing"])
        self.assertEqual(b["sports"]["nordic_skiing"]["selection_reasons"], ["guarded_week_winner"])
        self.assertEqual(b["weekly_guard"]["minimum_winner_seconds"], 2.2*3600)
        quiet = [activity(str(i), date=(start+timedelta(days=7*i)).strftime("%Y-%m-%d"),
                          tss=100, moving_time=8*3600) for i in range(24)]
        quiet.append(activity("quiet", "Run", date=(start+timedelta(days=24*7)).strftime("%Y-%m-%d"),
                              tss=1, moving_time=1800))
        b = self.block(quiet, start.strftime("%Y-%m-%d"), "2026-06-14", weekly_rescue=True)
        self.assertEqual(b["selected_sports"], ["cycling"])
        self.assertEqual(b["weekly_guard"]["minimum_winner_seconds"], 4*3600)

    def test_weekly_guard_boundary_and_minimum_known_full_weeks(self):
        acts = [activity("b", date="2026-06-01", tss=1000, moving_time=1000),
                activity("b2", date="2026-05-25", tss=1000, moving_time=1000)]
        for seconds, expected in ((499.99, False), (500, True)):
            b = self.block(acts + [activity("w", "Run", date="2026-06-08", tss=1, moving_time=seconds)],
                           "2026-05-25", "2026-06-14", weekly_rescue=True)
            self.assertEqual(b["sports"]["running"]["selected"], expected)
        b = self.block(acts[:1], "2026-05-25", "2026-06-14", weekly_rescue=True)
        self.assertIsNone(b["weekly_guard"]["median_moving_seconds"])
        acts[1]["moving_time"] = None
        b = self.block(acts, "2026-05-25", "2026-06-14", weekly_rescue=True)
        self.assertEqual(b["weekly_guard"]["known_nonempty_full_weeks"], 1)

    def test_non_monday_month_clipped_edges_cannot_win(self):
        self.sync.week_start_day = 6
        acts = [activity("b1", date="2026-05-04", tss=1000, moving_time=3600),
                activity("b2", date="2026-05-11", tss=1000, moving_time=3600),
                activity("edge", "AlpineSki", date="2026-05-01", tss=1, moving_time=2000)]
        b = self.block(acts, "2026-05-01", "2026-05-31", weekly_rescue=True)
        self.assertEqual(b["selected_sports"], ["cycling"])
        self.assertFalse(b["weeks"][0]["winner_eligible"])
        self.assertFalse(b["weeks"][-1]["winner_eligible"])
        self.assertEqual(b["weeks"][1]["start"], "2026-05-03")
        acts[-1]["start_date_local"] = "2026-05-18T10:00:00"
        self.assertIn("alpine_skiing", self.block(acts, "2026-05-01", "2026-05-31", weekly_rescue=True)["selected_sports"])

    def test_rolling_chunk_edges_and_ski_holiday_share_one_sport(self):
        acts = [activity("b", date="2026-05-20", tss=900, moving_time=7200),
                activity("b2", date="2026-05-27", tss=900, moving_time=7200),
                activity("ski", "NordicSki", date="2026-06-08", tss=30, moving_time=4000),
                activity("erg", "VirtualSki", date="2026-06-09", tss=2, moving_time=600)]
        b = self.block(acts, "2026-05-19", D, week_mode="rolling", weekly_rescue=True)
        self.assertEqual(b["selected_sports"], ["cycling", "nordic_skiing"])
        self.assertEqual(b["sports"]["nordic_skiing"]["sessions"], 2)
        self.assertEqual(b["sports"]["nordic_skiing"]["active_weeks"], [2, 3])
        self.assertEqual(len(b["sports"]["nordic_skiing"]["by_basis"]), 1)
        self.assertEqual(b["weeks"][2]["end"], "2026-06-08")
        self.assertEqual(b["weeks"][3]["start"], "2026-06-09")

    def test_independent_periods_and_nonqualifier_seconds_retained(self):
        acts = [activity("b", date="2026-06-02", tss=95),
                activity("r", "Run", date="2026-06-02", tss=5, moving_time=900,
                         icu_zone_times=[{"id": "Z2", "secs": 900}]),
                activity("r2", "Run", date=D, tss=100)]
        chunks = self.sync._rolling_7d_chunks(acts, [], [], D, 50, "run")
        self.assertEqual(chunks[0]["report_intensity"], {"ref": "derived_metrics.report_intensity_7d"})
        self.assertEqual(list(chunks[1]["report_intensity"]["selected"]), ["cycling"])
        self.assertEqual(chunks[1]["report_intensity"]["sports"]["running"][4]["p7"][3], 1)
        b = self.block(acts, "2026-05-19", D, week_mode="rolling", weekly_rescue=True)
        self.assertEqual(b["sports"]["running"]["by_basis"][0]["total_seconds"], 4500)
        acts.append(activity("historical", "NordicSki", date="2026-05-18", tss=200))
        before = self.sync._build_monthly_tier(by_date(acts), {}, days=60)
        acts[2] = activity("new", tss=900)
        after = self.sync._build_monthly_tier(by_date(acts), {}, days=60)
        may_before = next(m for m in before if m["month"] == "2026-05")
        may_after = next(m for m in after if m["month"] == "2026-05")
        self.assertEqual(list(may_before["report_intensity"]["selected"]), ["nordic_skiing"])
        self.assertEqual(may_before["report_intensity"], may_after["report_intensity"])
        self.assertNotEqual(may_before["seiler_tid_primary"], may_after["seiler_tid_primary"])

    def test_basis_preference_fallback_weighting_and_coverage(self):
        acts = [activity("p", moving_time=300, icu_zone_times=[{"id": "Z2", "secs": 300}],
                         icu_hr_zone_times=[300, 0, 0, 0, 0]),
                activity("p2", moving_time=100, icu_zone_times=[{"id": "Z3", "secs": 50}, {"id": "Z4", "secs": 50}]),
                activity("hr", date="2026-06-14", moving_time=100, icu_zone_times=[], icu_hr_zone_times=[100, 0, 0, 0, 0]),
                activity("pace", moving_time=50, icu_zone_times=[], pace_zone_times=[50, 0, 0, 0, 0])]
        b = self.block(acts, week_mode="rolling")["sports"]["cycling"]
        bases = {v["zone_basis"]: v for v in b["by_basis"]}
        self.assertEqual(set(bases), {"power", "hr"})
        self.assertEqual([bases["power"]["z"+str(i)+"_pct"] for i in (1, 2, 3)], [75, 12.5, 12.5])
        self.assertEqual(bases["power"]["total_seconds"], 400)
        self.assertEqual(bases["hr"]["total_seconds"], 100)
        self.assertEqual(bases["power"]["coverage"]["status"], "partial")
        self.assertEqual(b["known_moving_seconds"], 550)
        self.assertEqual(b["unavailable_zone_sessions"], 1)
        self.assertIsNone(bases["power"]["coverage"]["moving_ratio"])
        self.sync.zone_preference = {"cycling": "hr"}
        self.assertEqual(self.sync._report_zone_source(acts[0])[0], "hr")
        self.assertEqual(self.sync._report_zone_source(acts[1])[0], "power")

    def test_mapping_validation_and_no_pace_guess(self):
        for bad in ([{"id": "Z8", "secs": 30}], [{"id": "Z2", "secs": True}],
                    [{"id": "Z2", "secs": -1}], [{"id": "Z2", "secs": float("nan")}],
                    [{"id": "Z2", "secs": 1}, {"id": "Z2", "secs": 2}], [3]):
            self.assertIsNone(self.sync._report_zone_source(activity("x", icu_zone_times=bad)))
        for hr in ([1, 2, 3], [1, 2, 3, 4, True], [0]*5, [1]*8):
            self.assertIsNone(self.sync._report_zone_source(activity("x", icu_zone_times=[], icu_hr_zone_times=hr)))
        acts = [activity("h5", icu_zone_times=[], icu_hr_zone_times=[100, 0, 0, 0, 0]),
                activity("h7", icu_zone_times=[], icu_hr_zone_times=[100, 0, 0, 0, 0, 0, 0])]
        self.assertEqual(len(self.block(acts)["sports"]["cycling"]["by_basis"]), 2)
        self.assertIsNone(self.sync._report_zone_source(activity("pace", icu_zone_times=[], gap_zone_times=[100]*5)))

    def test_power_overlay_real_payload_shape_and_primary_parity(self):
        # Synthetic counts shaped like the reported Z1-Z7 plus SS payload.
        # SS overlaps numbered zones; it must never enter their denominator.
        power = [{"id": "Z" + str(i + 1), "secs": s}
                 for i, s in enumerate((1000, 7250, 1510, 160, 50, 20, 10))]
        power.append({"id": "SS", "secs": 1200})
        rides = [activity("ride-" + str(i), "VirtualRide",
                    date=(datetime.strptime(D, "%Y-%m-%d") - timedelta(days=i)).strftime("%Y-%m-%d"),
                    tss=938/6, moving_time=10000, icu_zone_times=copy.deepcopy(power),
                    icu_hr_zone_times=[1000, 8240, 480, 200, 40, 30, 10]) for i in range(6)]
        acts = rides + [activity("walk", "Walk", tss=39, moving_time=12450),
                        activity("ski", "VirtualSki", tss=23, moving_time=2550)]
        self.assertTrue(all(self.sync._zone_reference(ride)["basis"] == "power" for ride in rides))
        with tempfile.TemporaryDirectory() as folder:
            output, _, _ = collect(mod, folder, acts)
        dm = output["derived_metrics"]
        block = dm["report_intensity_7d"]
        self.assertEqual(list(block["selected"]), ["cycling"])
        self.assertEqual(set(block["sports"]["cycling"][4]), {"p7"})
        self.assertEqual(block["tid"]["cycling"]["p7"][:3], [82.5, 15.1, 2.4])
        legacy = dm["seiler_tid_7d_primary"]
        self.assertEqual(legacy["zone_basis"], "power")
        self.assertEqual(block["tid"]["cycling"]["p7"][:3],
                         [legacy["z" + str(i) + "_pct"] for i in (1, 2, 3)])
        self.assertEqual(sum(block["sports"]["cycling"][4]["p7"][:3]), 60000)
        for row in self.sync._build_weekly_tier(by_date(acts), {}, days=14):
            cycling = row["report_intensity"]["sports"].get("cycling")
            if cycling:
                self.assertEqual(set(cycling[4]), {"p7"})
        for invalid in ([{"id": "SS", "secs": -1}], [{"id": "SS", "secs": True}],
                        [{"id": "SS"}], [{"id": "SS", "secs": 1}] * 2,
                        [{"id": "Z3", "secs": 5}], [{"id": "unknown", "secs": 5}], [None]):
            with self.subTest(invalid=invalid):
                self.assertIsNone(self.sync._report_zone_source(
                    activity("bad", icu_zone_times=power[:-1] + invalid)))
        self.assertIsNone(self.sync._report_zone_source(
            activity("overlay-only", icu_zone_times=[{"id": "SS", "secs": 1200}])))
        self.assertEqual(self.sync._report_zone_source(activity("order", icu_zone_times=list(reversed(power))))[0], "power")

    def test_compact_decisions_missingness_and_raw_seconds(self):
        acts = [activity("b", tss=95), activity("run", "Run", tss=5, moving_time=100),
                activity("swim", "Swim", tss=None, moving_time=None, icu_zone_times=[])]
        self.sync.report_main_sports = ["cycling"]
        rich = self.block(acts, week_mode="rolling")
        packed = self.sync._report_pack(rich, ready=True)
        schema = self.sync._report_intensity_schema()
        self.assertEqual(schema["schema_version"], 2)
        self.assertEqual(list(packed["selected"]), rich["selected_sports"])
        self.assertEqual(packed["start"], rich["start"])
        self.assertEqual(packed["end"], rich["end"])
        for sport, values in rich["sports"].items():
            compact = packed["sports"][sport]
            self.assertEqual(compact[:3], [values[k] for k in
                             ("sessions", "known_tss", "known_moving_seconds")])
            self.assertEqual([schema["reasons"][c] for c in packed["selected"].get(sport, "")],
                             values["selection_reasons"])
            exceptions = compact[5] if len(compact) > 5 else {}
            self.assertEqual(exceptions.get("missing", [0, 0]), [values["missing_load"], values["missing_time"]])
            self.assertEqual(exceptions.get("indeterminate", False), values["qualification"] == "indeterminate")
            for source in values["by_basis"]:
                key = next(k for k, v in schema["mappings"].items() if v.endswith("/" + source["mapping_version"]))
                self.assertEqual(compact[4][key], [source["z" + str(i) + "_seconds"] for i in (1, 2, 3)] +
                                 [source["coverage"]["sessions"], len(source["coverage"]["covered_weeks"])])
        self.assertEqual(packed["sports"]["swimming"][1:3], [0, 0])
        self.assertEqual(packed["sports"]["swimming"][5]["missing"], [1, 1])
        self.assertNotIn("running", packed["selected"])
        self.assertIn("running", packed["sports"])
        self.assertEqual(packed["sports"]["running"][4]["p7"][0], 2880)
        mismatched = self.sync._report_pack(self.block([activity("clock", moving_time=3000)]), ready=True)
        self.assertEqual(mismatched["sports"]["cycling"][5]["unaligned"], ["p7"])
        cancelling = self.sync._report_pack(self.block([
            activity("short", moving_time=3000), activity("long", moving_time=4200)]), ready=True)
        self.assertEqual(cancelling["sports"]["cycling"][2], sum(cancelling["sports"]["cycling"][4]["p7"][:3]))
        self.assertEqual(cancelling["sports"]["cycling"][5]["unaligned"], ["p7"])
        self.assertEqual(self.sync._report_pack(self.block([]))["selected"], {})

    def test_chunk_references_require_identical_period_values(self):
        acts = [activity("a", date=D), activity("b", date="2026-06-08"),
                activity("c", "Run", date="2026-06-01")]
        chunks = self.sync._rolling_7d_chunks(acts, [], [], D, 50, "cycling")
        self.assertEqual(chunks[0]["report_intensity"], chunks[1]["report_intensity"])
        self.assertNotEqual(chunks[0]["start"], chunks[1]["start"])
        self.assertNotIn("ref", chunks[2]["report_intensity"])
        self.assertEqual(list(chunks[2]["report_intensity"]["selected"]), ["running"])
        acts[1]["icu_training_load"] += .0001
        changed = self.sync._rolling_7d_chunks(acts, [], [], D, 50, "cycling")
        self.assertNotIn("ref", changed[1]["report_intensity"])

    def test_comparison_missing_partial_incompatible_and_empty_authoritative(self):
        a = self.block([activity("b")], week_mode="rolling")
        b = self.block([activity("b")], "2026-05-19", D, week_mode="rolling", weekly_rescue=True)
        comparison = self.sync._report_intensity_comparison(a, b)
        self.assertEqual(comparison["lines"][0]["drift"], "consistent")
        self.assertIn("unknown", comparison["lines"][0]["note"])
        b["sports"]["cycling"]["by_basis"][0]["coverage"]["status"] = "partial"
        self.assertIsNone(self.sync._report_intensity_comparison(a, b)["lines"][0]["drift"])
        self.assertEqual(self.sync._report_intensity_comparison(a, None)["status"], "unavailable")
        empty = self.block([])
        self.assertEqual(self.sync._report_intensity_comparison(empty, empty)["lines"], [])
        b["rule_version"] = "old"
        self.assertEqual(self.sync._report_intensity_comparison(a, b)["status"], "unavailable")
        a["rule_version"] = "old"
        self.assertEqual(self.sync._report_intensity_comparison(a, b)["status"], "unavailable")

    def test_local_dates_unknown_dates_and_mismatched_clocks(self):
        acts = [activity("local", date="2026-06-09"),
                activity("outside", date="2026-06-08"),
                activity("bad", date="2026-02-30")]
        acts[0]["start_date_local"] = "2026-06-09T00:05:00+14:00"
        result = self.block(acts)
        self.assertEqual(result["denominator"]["sessions"], 1)
        self.assertEqual(result["identity"]["invalid_date_records"], 1)
        self.assertEqual(result["sports"]["cycling"]["by_basis"][0]["coverage"]["covered_ranges"],
                         [["2026-06-09", "2026-06-09"]])
        acts[0]["moving_time"] = 3000
        b = self.block(acts)
        coverage = b["sports"]["cycling"]["by_basis"][0]["coverage"]
        self.assertEqual(coverage["zone_seconds"], 3600)
        self.assertEqual(coverage["known_moving_seconds"], 3000)
        self.assertIsNone(coverage["moving_ratio"])
        self.assertEqual(coverage["status"], "partial")
        self.assertIsNone(self.sync._report_intensity_comparison(b, b)["lines"][0]["drift"])

    def test_collector_and_history_integration(self):
        acts = [activity("b"), activity("r", "Run", date="2026-06-02", tss=100)]
        with tempfile.TemporaryDirectory() as folder:
            output, _, _ = collect(mod, folder, acts)
        dm = output["derived_metrics"]
        self.assertEqual(output["report_intensity_schema"]["schema_version"], 2)
        self.assertEqual(output["rolling_7d_chunks"][0]["report_intensity"],
                         {"ref": "derived_metrics.report_intensity_7d"})
        self.assertEqual(list(dm["report_intensity_7d"]["selected"]), ["cycling"])
        self.assertEqual(list(dm["report_intensity_28d"]["selected"]), ["cycling", "running"])
        weeks = self.sync._build_weekly_tier(by_date(acts), {}, days=28)
        season = self.sync._report_season_intensity(acts, weeks, D)
        self.assertEqual(season["selected_sports"], ["running"])
        self.assertLess(season["end"], weeks[-1]["week_start"])
        self.assertTrue(all(b["classification"] is None for s in season["sports"].values() for b in s["by_basis"]))
        cache = {}
        a = self.sync._build_monthly_tier(by_date(acts), {}, days=30, report_cache=cache)
        b = self.sync._build_monthly_tier(by_date(acts), {}, days=60, report_cache=cache)
        self.assertIs(a[-1]["report_intensity"], b[-1]["report_intensity"])


if __name__ == "__main__":
    unittest.main()
