"""Identity, attributed curves and reporting corrections with synthetic transport only."""
import copy
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest import mock

from _harness import SYNC_PATH, NetworkBlocked, install_verb_guard, load_module_by_path, restore_verb_guard
from test_report_intensity import activity, collect
from test_sleep_context import Clock, D

mod = load_module_by_path("s11_multisport_corrections", SYNC_PATH)
originals = None
DATES = ("2026-05-19", "2026-06-15", "2026-04-21", "2026-05-18")
ENDURANCE = {
    "cycling": "Ride VirtualRide MountainBikeRide GravelRide TrackRide Cyclocross Velomobile".split(),
    "running": "Run VirtualRun TrailRun".split(), "swimming": "Swim OpenWaterSwim".split(),
    "rowing": "Rowing VirtualRow".split(),
    "nordic_skiing": "NordicSki VirtualSki RollerSki BackcountrySki".split(),
    "alpine_skiing": "AlpineSki Snowboard".split(),
    "paddling": "Canoeing Kayaking StandUpPaddling".split(), "handcycling": ["Handcycle"],
    "gym_cardio": "Elliptical StairStepper".split()}
EVERYDAY = {"Walk": "walking", "Hike": "hiking", "Snowshoe": "hiking",
            "EBikeRide": "e_cycling", "EMountainBikeRide": "e_cycling"}
ORDINARY = "WeightTraining Badminton Crossfit Golf HighIntensityIntervalTraining Hockey IceSkate InlineSkate Kitesurf Padel Pilates Pickleball Racquetball Rugby RockClimbing Sail Skateboard Soccer Squash Surfing TableTennis Tennis Transition WaterSport Wheelchair Windsurf Workout Yoga Other".split()


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


def curve(metric="power", ids=("now", "old"), value=200, attributed=True):
    secs = [5, 60, 300, 1200, 3600] if metric == "power" else [60, 300, 1200, 3600]
    rows = []
    for i, (start, end) in enumerate((DATES[:2], DATES[2:])):
        row = {"id": f"r.{start}.{end}", "secs": secs[:],
               "values": [value - i * 20] * len(secs)}
        if attributed:
            row["activity_id"] = [ids[i]] * len(secs)
        rows.append(row)
    return {"list": rows}


def truncated_rank_curves(metric):
    """Synthetic structure from the reviewed 210/204-point grouped responses."""
    seconds = (list(range(1, 61)) + list(range(68, 301, 8))
               + list(range(345, 1201, 45)) + list(range(1260, 3601, 60))
               + list(range(3660, 7201, 60)))
    rows, acts = [], []
    for wi, (start, end) in enumerate((DATES[:2], DATES[2:])):
        secs = seconds[:(210, 204)[wi]]
        ids = [f"i9000000{wi + 1}{rank}" for rank in range(4)]
        date = (D, "2026-05-10")[wi]
        acts.extend(activity(aid, "VirtualRide", date=date) for aid in ids)
        values = [800 - i * 3 - wi * 20 if metric == "power" else round(190 - i * .1 - wi * 4, 1)
                  for i in range(len(secs))]
        lengths = ((204, 198, 174), (198, 198, 174))[wi]
        row = {"id": f"r.{start}.{end}", "start_date_local": start + "T00:00:00",
               "end_date_local": end + "T23:59:59", "secs": secs,
               "values": values, "activity_id": [ids[0]] * len(secs),
               "submax_values": [[round(v - rank * (10 if metric == "power" else 2), 1)
                                   for v in values[:length]] for rank, length in enumerate(lengths, 1)],
               "submax_activity_id": [[ids[rank]] * length for rank, length in enumerate(lengths, 1)]}
        if metric == "power":
            row["watts"] = values[:]
            row["watts_per_kg"] = [round(v / 70, 2) for v in values]
        rows.append(row)
    metadata = {a["id"]: {"id": a["id"], "name": "Synthetic session", "distance": 40000,
                "moving_time": 7200, "training_load": 100, "icu_weight": 70,
                "start_date_local": a["start_date_local"], "race": False} for a in acts}
    return acts, {"list": rows, "activities": metadata}


class Corrections(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sync = mod.IntervalsSync("synthetic", "synthetic", week_start_day=6)
        self.sync.data_dir = Path(tmp.name)
        self.sync._intervals_data = {}
        clock = mock.patch.object(mod, "datetime", Clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.acts = [activity("now"), activity("old", date="2026-05-10")]

    def deltas(self, acts=None, selected=("cycling",), response=None):
        calls = []
        def get(endpoint, params):
            calls.append((endpoint, params))
            if response:
                return response(endpoint, params)
            return curve("power" if endpoint == "power-curves" else "hr")
        with mock.patch.object(self.sync, "_intervals_get", side_effect=get):
            out = self.sync._sport_curve_deltas(self.acts if acts is None else acts, selected, DATES)
        return out, calls

    def test_literal_inventory_and_alias(self):
        expected = {typ: sport for sport, types in ENDURANCE.items() for typ in types}
        self.assertEqual(len(expected), 26)
        self.assertEqual(len(ORDINARY), 29)
        self.assertEqual(mod.IntervalsSync.REPORT_SPORTS, {**expected, **EVERYDAY})
        self.assertEqual(len(set(expected) | set(EVERYDAY) | set(ORDINARY)), 60)
        for typ in ORDINARY + ["ride", "Unknown", None]:
            self.assertNotIn(typ, self.sync.REPORT_SPORTS)
        for raw in (" skiing, nordic_skiing ", ["skiing", "nordic_skiing"]):
            self.assertEqual(self.sync._parse_report_main_sports(raw), ["nordic_skiing"])
        self.assertEqual(self.sync.SPORT_FAMILIES["EBikeRide"], "cycling")
        self.assertNotIn("Handcycle", self.sync.SPORT_FAMILIES)
        for malformed in (None, False, [], {}, 1):
            block = self.sync._report_intensity([activity("bad", malformed)], D, D)
            self.assertEqual(block["selected_sports"], [])
            self.assertEqual(block["denominator"]["sessions"], 0)

    def test_walk_denominator_ski_qualifies_only_after_exclusion(self):
        rides = [activity("bike", date="2026-06-10", tss=880, moving_time=20000)]
        skis = [activity("ski" + str(i), "VirtualSki", date=f"2026-06-{10+i:02d}",
                         tss=40, moving_time=1000) for i in range(3)]
        walks = [activity("walk" + str(i), "Walk", date=f"2026-06-{9+i:02d}",
                          tss=30, moving_time=3000) for i in range(7)]
        block = self.sync._report_intensity(rides + skis + walks, "2026-05-19", D)
        self.assertEqual(block["denominator"]["tss"], 1000)
        self.assertEqual(block["sports"]["nordic_skiing"]["load_share"], .12)
        self.assertLess(120 / 1210, .10)
        self.assertEqual(block["sports"]["nordic_skiing"]["selection_reasons"], ["load_share_10pct"])
        self.sync.report_main_sports = list(EVERYDAY.values())
        for walk in walks:
            walk.update(icu_training_load=None, moving_time=None)
        self.assertEqual(self.sync._report_intensity(rides + skis + walks, "2026-05-19", D)["denominator"],
                         self.sync._report_intensity(rides + skis, "2026-05-19", D)["denominator"])

    def test_all_endurance_types_eligible_without_zones(self):
        for sport, types in ENDURANCE.items():
            acts = [activity(t, t, icu_zone_times=[], icu_hr_zone_times=[]) for t in types]
            b = self.sync._report_intensity(acts, D, D)
            self.assertEqual(b["selected_sports"], [sport])
            self.assertEqual(b["sports"][sport]["sessions"], len(types))

    def test_cycling_fact_identity_environment_and_detail_gate(self):
        for typ in ENDURANCE["cycling"]:
            raw = activity(typ, typ, trainer=True, distance=1000)
            formatted = {"id": typ, "type": typ, "date": D, "distance_m": 1000}
            self.assertEqual(self.sync._fact_activity(raw)["environment"], "indoor_virtual")
            facts = self.sync._build_fun_facts([formatted], D, D, formatted=True, raw_activities=[raw])
            self.assertEqual(facts["distance_elevation"]["indoor_virtual"]["distance_m"], 1000)
            self.assertEqual(self.sync._build_fun_facts([formatted], D, D, formatted=True)["distance_elevation"]["unknown"]["distance_m"], 1000)
            conflict = {**raw, "trainer": False}
            self.assertEqual(self.sync._build_fun_facts([formatted], D, D, formatted=True,
                raw_activities=[raw, conflict])["distance_elevation"]["unknown"]["distance_m"], 1000)
        for typ in ("EBikeRide", "EMountainBikeRide", "Handcycle"):
            raw = activity(typ, typ, distance=999999)
            self.assertIsNone(self.sync._fact_activity(raw))
            self.assertEqual(self.sync._build_decoupling_for_activity(raw, {typ: {"decoupling_detail": "old"}}, True, True, {}), {})
        self.assertIsNone(self.sync._fact_indoor(activity("track", "TrackRide")))
        self.assertFalse(self.sync._cycling_fact_indoor(activity("track", "TrackRide", name="virtual indoor")))

    def test_hard_day_explanation_does_not_change_classification(self):
        for typ, cycling in (("Ride", True), ("TrackRide", True), ("EBikeRide", False), ("Handcycle", False)):
            raw = activity("a", typ, icu_zone_times=[{"id": "Z3", "secs": 2000}])
            result = self.sync._hard_day_evidence([raw], {"a": {"plan_pairing": {"targets_intensity": False}}})
            self.assertEqual(result["activities"][0]["cycling"], cycling)
            self.assertEqual(result["power_z3_only_non_intensity"], cycling)
            self.assertEqual(result["triggers"][0]["rung"], "z3+")

    def test_durability_cohort_gates_and_weekly_monthly(self):
        good = activity("good", moving_time=5400, decoupling=0, icu_variability_index=1.05)
        excluded = [dict(good, id=t, type=t, decoupling=99) for t in
                    ("Run", "Rowing", "VirtualSki", "Handcycle", "EBikeRide", "EMountainBikeRide")]
        for changes in ({"moving_time": 5399}, {"icu_variability_index": 0},
                        {"icu_variability_index": 1.051}, {"decoupling": None}):
            self.assertEqual(self.sync._cycling_durability_values([{**good, **changes}]), [])
        one = self.sync._calculate_durability([good] + excluded, [good] + excluded)
        self.assertEqual(one["qualifying_sessions_7d"], 1)
        self.assertIsNone(one["mean_decoupling_7d"])
        negative = dict(good, id="negative", type="Velomobile", decoupling=-2)
        two = self.sync._calculate_durability([good, negative] + excluded, [good, negative])
        self.assertEqual(two["mean_decoupling_7d"], -1)
        by_date = {D: [good] + excluded}
        weeks = self.sync._build_weekly_tier(by_date, {}, days=28)
        self.assertEqual(weeks[-1]["durability_mean"], 0)
        months = self.sync._build_monthly_tier(by_date, {}, days=60)
        self.assertTrue(all(not any(k.startswith("durability") for k in row) for row in months))

    def test_longest_compact_exact_ties_conflicts_and_legacy(self):
        acts = [activity("b", moving_time=7200), activity("a", moving_time=7200),
                activity("run", "Run", moving_time=5000), activity("hike", "Hike", moving_time=99999)]
        result = self.sync._longest_sessions(acts + [copy.deepcopy(acts[0])])
        self.assertEqual(result["cycling"], [7200, D, "a", "Ride", "available"])
        self.assertEqual(result["running"][0], 5000)
        self.assertNotIn("hiking", result)
        partial = self.sync._longest_sessions(acts + [dict(acts[1], moving_time=8000)])
        self.assertEqual(partial["cycling"], [7200, D, "b", "Ride", "partial"])
        self.assertEqual(self.sync._longest_sessions([activity("z", moving_time=0)])["cycling"][0], 0)
        self.assertEqual(self.sync._longest_sessions([activity("z", moving_time=None)])["cycling"][-1], "no_valid_duration")
        weeks = self.sync._build_weekly_tier({D: acts}, {}, days=28)
        self.assertEqual(weeks[-1]["longest_ride_hours"], round(99999/3600, 2))
        self.assertEqual(weeks[-1]["longest_session_by_sport"], result)
        months = self.sync._build_monthly_tier({D: acts}, {}, days=60)
        self.assertEqual(months[-1]["longest_session_by_sport"], result)
        self.assertEqual(len(self.sync._longest_session_schema()["columns"]), 5)

    def test_longest_conflicting_id_across_week_and_month_boundaries(self):
        acts = [activity("moved", date="2026-05-15", moving_time=99999),
                activity("moved", date=D, moving_time=99999), activity("valid", moving_time=3600)]
        by_date = defaultdict(list)
        for act in acts:
            by_date[act["start_date_local"][:10]].append(act)
        weeks = self.sync._build_weekly_tier(by_date, {}, days=60)
        months = self.sync._build_monthly_tier(by_date, {}, days=60)
        for rows in (weeks, months):
            self.assertEqual(rows[-1]["longest_session_by_sport"]["cycling"],
                             [3600, D, "valid", "Ride", "partial"])
            self.assertTrue(all(row.get("longest_session_by_sport", {}).get("cycling", [None, None, None])[2]
                                != "moved" for row in rows))

    def test_strain_same_set_no_legacy_fallback_and_boundary(self):
        rides = [activity(str(i), date="2026-06-" + str(10+i), tss=100+i*10) for i in range(5)]
        before = self.sync._main_monotony(rides, "2026-06-09", D)
        after = self.sync._main_monotony(rides + [activity("w", "Walk", tss=9999)], "2026-06-09", D)
        self.assertEqual(before, after)
        result = self.sync._main_strain(after)
        self.assertEqual(result["value"], round(after["tss"] * after["value"], 0))
        for amount, expected in ((3499, False), (3500, False), (3501, True)):
            metrics = {"strain": 99999, "strain_main_sports": dict(result, value=amount)}
            alerts = self.sync._generate_alerts(metrics, [], 0, 0)
            strain = [a for a in alerts if a["metric"] == "strain"]
            self.assertEqual(bool(strain), expected)
            if strain:
                self.assertEqual((strain[0]["tier"], strain[0]["readiness_eligible"]), (2, False))
        self.assertFalse(any(a["metric"] == "strain" for a in self.sync._generate_alerts({"strain": 99999}, [], 0, 0)))
        self.assertIsNone(self.sync._main_strain({"status": "zero_variance", "tss": 700})["value"])

    def test_curves_values_shape_source_and_legacy_arithmetic(self):
        out, calls = self.deltas()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(p["subMaxEfforts"] == 3 and p["type"] == "Ride" for _, p in calls))
        for metric in ("power", "hr"):
            result = out["sports"]["cycling"][metric]
            self.assertEqual(result["status"], "available")
            self.assertEqual(result["anchors"]["60s"]["pct_change"], 11.1)
            self.assertEqual(result["anchors"]["60s"]["current_source"]["activity_id"], "now")
            self.assertEqual(result["rotation_index"], 0)

    def test_attribution_exclusion_and_submax_recovery(self):
        acts = self.acts + [activity("ebike", "EBikeRide"), activity("eold", "Handcycle", date="2026-05-10")]
        def response(endpoint, params):
            payload = curve("power" if endpoint == "power-curves" else "hr", ("ebike", "eold"), 900)
            for i, row in enumerate(payload["list"]):
                row["submax_values"] = [[200-i*20] * len(row["secs"])]
                row["submax_activity_id"] = [[("now", "old")[i]] * len(row["secs"])]
            return payload
        out, _ = self.deltas(acts, response=response)
        self.assertEqual(out["sports"]["cycling"]["power"]["status"], "available")
        self.assertEqual(out["sports"]["cycling"]["power"]["anchors"]["60s"]["current_watts"], 200)
        out, _ = self.deltas(acts, response=lambda e, p: curve("power" if e == "power-curves" else "hr", ("ebike", "eold")))
        power = out["sports"]["cycling"]["power"]
        self.assertEqual(power["status"], "partial")
        self.assertIsNone(power["anchors"]["60s"]["current_watts"])
        self.assertEqual(power["anchors"]["60s"]["current_reason"], "no_eligible_attributed_effort")

    def test_typed_unattributed_fallback_and_shared_group_risk(self):
        response = lambda e, p: curve("power" if e == "power-curves" else "hr", attributed=False)
        out, _ = self.deltas(response=response)
        self.assertEqual(out["sports"]["cycling"]["hr"]["status"], "available")
        self.assertIsNone(out["sports"]["cycling"]["hr"]["anchors"]["60s"]["current_source"]["activity_id"])
        out, _ = self.deltas(self.acts + [activity("bad", "EBikeRide")], response=response)
        self.assertEqual(out["sports"]["cycling"]["hr"]["status"], "partial")
        out, _ = self.deltas(self.acts + [activity("runner", "Run")], response=response)
        self.assertEqual(out["sports"]["cycling"]["hr"]["status"], "available")

    def test_wrong_window_unknown_conflicting_identity(self):
        for ids, acts in ((("old", "now"), self.acts), (("missing", "old"), self.acts),
                         (("now", "old"), self.acts + [activity("now", "Handcycle")])):
            out, _ = self.deltas(acts, response=lambda e, p: curve("power" if e == "power-curves" else "hr", ids))
            self.assertEqual(out["sports"]["cycling"]["power"]["status"], "partial")

    def test_prior_only_sibling_max_merge_and_failed_constituent(self):
        acts = self.acts + [activity("vold", "VirtualRide", date="2026-05-11")]
        out, calls = self.deltas(acts)
        self.assertEqual(len(calls), 4)
        self.assertEqual(out["sports"]["cycling"]["power"]["anchors"]["60s"]["previous_watts"], 180)
        def fail(endpoint, params):
            if params["type"] == "VirtualRide":
                raise mod.requests.exceptions.Timeout("synthetic")
            return curve("power" if endpoint == "power-curves" else "hr")
        out, calls = self.deltas(acts, response=fail)
        self.assertEqual(len(calls), 4)
        result = out["sports"]["cycling"]["power"]
        self.assertEqual(result["failed_types"], ["VirtualRide"])
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["anchors"]["60s"]["pct_change"])

    def test_windows_sparse_malformed_and_invalid_numbers(self):
        for mode in ("reverse", "missing", "sparse", "conflict", "duplicate_secs", "invalid"):
            def response(endpoint, params):
                payload = curve("power" if endpoint == "power-curves" else "hr")
                if mode == "reverse":
                    payload["list"].reverse()
                elif mode == "missing":
                    payload["list"].pop()
                elif mode == "sparse":
                    payload["list"][1]["values"] = [100]
                elif mode == "conflict":
                    payload["list"].append({**payload["list"][0], "values": [999]})
                elif mode == "duplicate_secs":
                    payload["list"][0]["secs"] = [60, 60]
                else:
                    payload["list"][0]["values"] = [None, True, float("nan"), float("inf"), 0]
                return payload
            out, _ = self.deltas(response=response)
            result = out["sports"]["cycling"]["power"]
            self.assertEqual(result["status"], {"reverse": "available", "missing": "one_window_only",
                "sparse": "insufficient_data", "conflict": "partial", "duplicate_secs": "partial",
                "invalid": "one_window_only"}[mode])
            if mode != "reverse":
                self.assertIsNone(result["rotation_index"])

    def test_request_counts_power_gate_and_unselected_exclusion(self):
        cases = [("Ride", 2), ("Ride VirtualRide", 4), ("Ride Run Swim", 4),
                 ("Ride VirtualRide Run TrailRun Swim OpenWaterSwim", 8)]
        for raw_types, count in cases:
            types = raw_types.split()
            acts = [activity(t, t, icu_zone_times=[] if t not in ENDURANCE["cycling"] else
                             [{"id": "Z2", "secs": 3600}]) for t in types]
            selected = {self.sync.REPORT_SPORTS[t] for t in types}
            out, calls = self.deltas(acts, selected, response=lambda e, p: {"list": []})
            self.assertEqual(len(calls), count)
            if "running" in selected:
                self.assertEqual(out["sports"]["running"]["power"]["status"], "no_power_observed")
        _, calls = self.deltas([activity("run", "Run")], (), response=lambda e, p: {})
        self.assertEqual(calls, [])
        all_acts = [activity(t, t) for types in ENDURANCE.values() for t in types]
        _, calls = self.deltas(all_acts, ENDURANCE, response=lambda e, p: {"list": []})
        self.assertEqual(len(calls), 52)

    def test_collector_listing_slices_and_new_calls_follow_legacy(self):
        acts = self.acts + [activity("outside", date="2026-04-20"), activity("forty", date="2026-05-06")]
        output, calls, _ = collect(mod, self.sync.data_dir, acts)
        listing = [p for e, p in calls if e == "activities"]
        self.assertEqual(listing, [{"oldest": DATES[2], "newest": D}])
        self.assertEqual([a["id"] for a in output["recent_activities"]], ["now"])
        new = [i for i, (_, p) in enumerate(calls) if p and "subMaxEfforts" in p]
        legacy = [i for i, (e, p) in enumerate(calls) if e in ("power-curves", "hr-curves") and "subMaxEfforts" not in p]
        self.assertLess(max(legacy), min(new))

    def test_tid_alerts_use_complete_compatible_sport_mapping_only(self):
        def block(seconds, start):
            act = activity("a", icu_zone_times=[{"id": z, "secs": s} for z, s in
                           zip(("Z2", "Z3", "Z4"), seconds)])
            return self.sync._report_intensity([act], start, D, week_mode="rolling")
        acute = block((2160, 684, 756), "2026-06-09")
        chronic = block((3000, 60, 540), DATES[0])
        comparison = self.sync._report_intensity_comparison(acute, chronic)
        metrics = {"report_intensity_7d": self.sync._report_pack(acute, ready=True),
                   "report_intensity_28d": self.sync._report_pack(chronic, ready=True),
                   "capability": {"report_intensity_comparison": self.sync._report_pack_comparison(comparison),
                                  "tid_comparison": {"drift": "consistent"}}}
        alerts = self.sync._report_tid_alerts(metrics)
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0]["sport"], alerts[0]["mapping"], alerts[0]["tier"],
                          alerts[0]["readiness_eligible"]), ("cycling", "p7", 3, False))
        for failure in ("partial", "old_rule", "missing", "one_sided", "different_mapping", "unaligned"):
            broken = copy.deepcopy(metrics)
            if failure == "partial":
                broken["capability"]["report_intensity_comparison"]["lines"][0]["status"] = "unavailable_or_partial"
            elif failure == "old_rule":
                broken["capability"]["report_intensity_comparison"]["rule_version"] = "endurance_10_50_week_guard_v1"
            elif failure == "missing":
                broken["capability"].pop("report_intensity_comparison")
            elif failure == "one_sided":
                broken["report_intensity_7d"]["selected"] = {}
            elif failure == "different_mapping":
                broken["report_intensity_7d"]["tid"]["cycling"] = {"h5": [60, 19, 21, 1.82, "Pyramidal"]}
            else:
                broken["report_intensity_7d"]["sports"]["cycling"].append({"unaligned": ["p7"]})
            broken["capability"]["tid_comparison"]["drift"] = "acute_depolarization"
            self.assertEqual(self.sync._report_tid_alerts(broken), [], failure)

    def test_durability_alert_boundaries_and_metadata(self):
        base = {"mean_decoupling_28d": 5, "qualifying_sessions_28d": 5,
                "mean_decoupling_7d": 7, "qualifying_sessions_7d": 3,
                "trend": "declining", "high_drift_count_7d": 2}
        def alerts(**changes):
            return [a for a in self.sync._generate_alerts(
                {"capability": {"durability": {**base, **changes}}}, [], 0, 0)
                if a["metric"] == "durability"]
        self.assertEqual(alerts(), [])
        self.assertEqual(alerts(mean_decoupling_28d=5.01, qualifying_sessions_28d=4), [])
        self.assertEqual(len(alerts(mean_decoupling_28d=5.01)), 1)
        self.assertEqual(alerts(mean_decoupling_7d=7.01, qualifying_sessions_7d=2), [])
        self.assertEqual(len(alerts(mean_decoupling_7d=7.01)), 1)
        high = alerts(high_drift_count_7d=3, qualifying_sessions_28d=0)
        self.assertEqual(len(high), 1)
        self.assertEqual((high[0]["sport"], high[0]["scope"], high[0]["readiness_eligible"]),
                         ("cycling", "reporting", False))

    def test_handcycling_uses_own_attribution_and_no_cycling_ftp_fallback(self):
        hands = [dict(a, type="Handcycle", icu_ftp=None) for a in self.acts]
        with mock.patch.object(self.sync, "_load_ftp_history", return_value={"outdoor": {"2026-01-01": 999}}) as history:
            self.assertEqual(self.sync._structure_ftp_basis(hands[0]), {"source": "intervals_resolved"})
            history.assert_not_called()
        out, calls = self.deltas(hands, ("handcycling",))
        self.assertTrue(all(p["type"] == "Handcycle" for _, p in calls))
        self.assertEqual(out["sports"]["handcycling"]["power"]["status"], "available")
        self.assertEqual(out["sports"]["handcycling"]["power"]["anchors"]["60s"]["current_source"]["activity_id"], "now")
        self.assertIsNone(self.sync._zone_reference(hands[0])["ftp_w"])

    def test_submax_point_major_and_invalid_source_identity(self):
        acts = self.acts + [activity("wrong", "Run"), activity("wrong_old", "Run", date="2026-05-10")]
        def response(endpoint, params):
            payload = curve("power" if endpoint == "power-curves" else "hr", ("wrong", "wrong_old"), 999)
            for i, row in enumerate(payload["list"]):
                row["submax_values"] = [[220-i*20, 210-i*20] for _ in row["secs"]]
                row["submax_activity_id"] = [[("now", "old")[i], "unjoined"] for _ in row["secs"]]
            return payload
        out, _ = self.deltas(acts, response=response)
        self.assertEqual(out["sports"]["cycling"]["power"]["anchors"]["60s"]["current_watts"], 220)
        self.assertEqual(out["sports"]["cycling"]["power"]["status"], "available")

    def test_live_shaped_truncated_ranks_available(self):
        acts, power = truncated_rank_curves("power")
        _, hr = truncated_rank_curves("hr")
        payloads = {"power-curves": power, "hr-curves": hr}
        out, calls = self.deltas(acts, response=lambda e, p: copy.deepcopy(payloads[e]))
        self.assertEqual([(e, p["type"], p["subMaxEfforts"]) for e, p in calls],
                         [("power-curves", "VirtualRide", 3), ("hr-curves", "VirtualRide", 3)])
        for metric, payload in (("power", power), ("hr", hr)):
            block = out["sports"]["cycling"][metric]
            self.assertEqual(block["status"], "available")
            self.assertEqual(block["failed_types"], [])
            anchors = {"5s": 4, "60s": 59, "300s": 89, "1200s": 109, "3600s": 149}
            if metric == "hr":
                anchors.pop("5s")
            self.assertEqual((block["current_valid_anchors"], block["previous_valid_anchors"]),
                             (len(anchors), len(anchors)))
            calculate = self.sync._calculate_power_curve_delta if metric == "power" else self.sync._calculate_hr_curve_delta
            legacy = calculate(payload, DATES)
            self.assertEqual(block["rotation_index"], legacy["rotation_index"])
            for anchor, index in anchors.items():
                row = block["anchors"][anchor]
                for wi, side in enumerate(("current", "previous")):
                    self.assertEqual(row[side + ("_watts" if metric == "power" else "_bpm")],
                                     payload["list"][wi]["values"][index])
                    self.assertEqual(row[side + "_source"], {"request_type": "VirtualRide",
                        "activity_id": f"i9000000{wi + 1}0", "date": (D, "2026-05-10")[wi]})
                self.assertEqual(row["pct_change"], legacy["anchors"][anchor]["pct_change"])

    def test_live_shaped_truncated_recovery_and_missing_effort(self):
        for mode in ("recover", "missing_rank", "missing_anchor", "unused_tail", "empty_ranks"):
            acts, _ = truncated_rank_curves("power")
            acts.append(activity("i900000099", "EBikeRide"))
            def response(endpoint, params):
                _, payload = truncated_rank_curves("power" if endpoint == "power-curves" else "hr")
                row = payload["list"][0]
                if mode not in ("unused_tail", "empty_ranks"):
                    row["activity_id"] = ["i900000099"] * len(row["secs"])
                    # Reject the highest sub-max at 60s; the next eligible rank wins.
                    row["submax_activity_id"][0][59] = "i900000099"
                if mode in ("missing_rank", "unused_tail"):
                    for key in ("submax_values", "submax_activity_id"):
                        row[key] = [rank[:149] for rank in row[key]]
                elif mode == "missing_anchor":
                    for rank in row["submax_values"]:
                        rank[149] = None
                elif mode == "empty_ranks":
                    row["submax_values"] = [[], [], []]
                    row["submax_activity_id"] = [[], [], []]
                return payload
            out, _ = self.deltas(acts, response=response)
            for metric in ("power", "hr"):
                block = out["sports"]["cycling"][metric]
                missing = mode in ("missing_rank", "missing_anchor")
                self.assertEqual(block["status"], "partial" if missing else "available", mode)
                self.assertEqual(block["failed_types"], [])
                value_key = "current_watts" if metric == "power" else "current_bpm"
                row = block["anchors"]["3600s"]
                if missing:
                    self.assertIsNone(row[value_key])
                    self.assertEqual(row["current_reason"], "no_eligible_attributed_effort")
                    self.assertIsNone(block["rotation_index"])
                    self.assertTrue(all(a["pct_change"] is None for a in block["anchors"].values()))
                elif mode == "recover":
                    self.assertEqual(row[value_key], 343 if metric == "power" else 173.1)
                    self.assertEqual(row["current_source"]["activity_id"], "i900000011")
                else:
                    self.assertEqual(row[value_key], 353 if metric == "power" else 175.1)
                if mode not in ("unused_tail", "empty_ranks"):
                    self.assertEqual(block["anchors"]["60s"][value_key], 603 if metric == "power" else 180.1)
                    self.assertEqual(block["anchors"]["60s"]["current_source"]["activity_id"], "i900000012")

    def test_truncated_submax_shape_rejections(self):
        for mode in ("too_many_ranks", "too_long", "mismatched_inner", "mismatched_outer",
                     "non_list_entry", "non_list_outer"):
            acts, payload = truncated_rank_curves("power")
            row = payload["list"][0]
            if mode == "too_many_ranks":
                row["submax_values"].append([])
                row["submax_activity_id"].append([])
            elif mode == "too_long":
                row["submax_values"][0] = [100] * 211
                row["submax_activity_id"][0] = ["i900000011"] * 211
            elif mode == "mismatched_inner":
                row["submax_activity_id"][0].pop()
            elif mode == "mismatched_outer":
                row["submax_activity_id"].pop()
            elif mode == "non_list_entry":
                row["submax_values"][0] = None
            else:
                row["submax_values"] = {}
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "invalid_submax_"):
                self.sync._sport_curve_points(payload, "VirtualRide", "cycling", "power",
                                              DATES, acts, {})

    def test_new_curve_reads_share_remaining_retry_budget(self):
        self.sync._read_retry_extra_attempts_used = 3
        acts = self.acts + [activity("v", "VirtualRide")]
        with mock.patch.object(mod.requests, "get", side_effect=mod.requests.exceptions.Timeout("synthetic")) as get, \
                mock.patch.object(mod.time, "sleep"), mock.patch.object(mod.time, "monotonic", return_value=0):
            result = self.sync._sport_curve_deltas(acts, ["cycling"], DATES)
        self.assertEqual(get.call_count, 5)  # Four logical reads, only one shared retry remains.
        self.assertEqual(self.sync._read_retry_extra_attempts_used, 4)
        self.assertTrue(all(c.kwargs["timeout"] == (5, 30) for c in get.call_args_list))
        self.assertEqual(result["sports"]["cycling"]["hr"]["status"], "partial")

    def test_network_guard_control(self):
        with self.assertRaises(NetworkBlocked):
            mod.requests.get("https://synthetic.invalid/never-sent")


if __name__ == "__main__":
    unittest.main()
