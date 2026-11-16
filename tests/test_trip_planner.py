import unittest

from travel_engine.geo_clusters import GeoClusterRegistry
from travel_engine.multimodal_router import MultiModalRouter
from travel_engine.trip_planner import TripPlanner, _to_option


def gflight(airline, times, price, *, direct=True, layover="", airports=""):
    """A flight dict in the GoogleFlightsProvider output format."""
    return {
        "source": "test",
        "airline": airline,
        "times": times,
        "duration": "2 sa.",
        "airports": airports,
        "stops": "Aktarmasız" if direct else "1 aktarma",
        "stops_count": 0 if direct else 1,
        "is_direct": direct,
        "layover_info": layover,
        "total_price_try": float(price),
        "currency": "TRY",
        "trip_type": "Tek Yön",
        "badge": None,
        "google_flights_url": "https://example.test/flights",
    }


class StubFlightProvider:
    """Canned flights keyed by (origin, destination, date). Records every call. No network."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", trip_type="oneway", **_):
        self.calls.append((origin, destination, departure_date))
        key = (origin.upper(), destination.upper(), departure_date)
        return [dict(f) for f in self.table.get(key, [])]


# Outbound: Athens -> Prague, arrive by 2026-10-28 12:00
OUTBOUND_TABLE = {
    ("ATH", "PRG", "2026-10-27"): [gflight("Aegean", "18:00 – 20:10", 6000)],
    ("ATH", "PRG", "2026-10-28"): [gflight("KLM", "13:45 – 19:30", 16286)],
    ("ATH", "BTS", "2026-10-27"): [gflight("Wizz Air", "19:00 – 21:10", 4447)],
    ("ATH", "BTS", "2026-10-28"): [gflight("Ryanair", "07:55 – 09:10", 5524)],
}

# Return: Prague -> Istanbul via Greece, arrive by 2026-10-31 23:59
RETURN_TABLE = {
    ("PRG", "IST", "2026-10-31"): [
        gflight("Turkish Airlines", "09:00 – 16:30", 9000, direct=False,
                airports="PRG ➔ IST", layover="1 sa. 30 dk. ATH (Atina)"),
        gflight("Pegasus", "11:00 – 14:45", 4000),
        gflight("LOT", "08:00 – 14:00", 7000, direct=False, layover="2 sa. 10 dk. VIE (Viyana)"),
    ],
    ("PRG", "ATH", "2026-10-31"): [gflight("Aegean", "10:00 – 13:15", 2500)],
    ("ATH", "IST", "2026-10-31"): [
        gflight("Pegasus", "14:00 – 16:00", 2000),   # buffer too short after 13:15 arrival
        gflight("Pegasus", "18:30 – 20:30", 3500),   # valid second ticket
        gflight("Pegasus", "22:00 – 00:30+1", 1800), # lands 2026-11-01 00:30
    ],
}


def make_planner(table):
    provider = StubFlightProvider(table)
    router = MultiModalRouter(flight_provider=provider)  # real router, real RailBusProvider, no network
    return TripPlanner(provider, router), provider


class TestOutboundScenario(unittest.TestCase):
    """Athens -> Prague, depart 27 or 28 Oct, arrive by 28 Oct 12:00."""

    def setUp(self):
        self.planner, self.provider = make_planner(OUTBOUND_TABLE)
        self.result = self.planner.plan_trip(
            "ATH", "PRG", ["2026-10-27", "2026-10-28"], arrive_by="2026-10-28T12:00")
        self.options = self.result["options"]

    def test_every_option_arrives_before_deadline(self):
        self.assertTrue(self.options)
        for option in self.options:
            self.assertLessEqual(option["arrive_at"], "2026-10-28T12:00")

    def test_28th_1345_direct_is_excluded(self):
        for option in self.options:
            self.assertNotEqual(option["depart_at"], "2026-10-28T13:45")
        self.assertIsNone(
            next((o for o in self.options if o["depart_at"] == "2026-10-28T13:45"), None))

    def test_27th_evening_side_route_via_bratislava_is_included(self):
        route = next(
            o for o in self.options
            if o["kind"] == "side_route" and o["depart_at"] == "2026-10-27T19:00")
        self.assertEqual(route["arrive_at"], "2026-10-28T10:20")
        self.assertTrue(route["night_wait"])
        self.assertTrue(route["separate_tickets"])
        self.assertIsNone(route["via_stop"])
        self.assertEqual([leg["type"] for leg in route["legs"]], ["flight", "transfer", "train"])
        self.assertEqual(route["legs"][0]["depart_at"], "2026-10-27T19:00")
        self.assertEqual(route["legs"][0]["arrive_at"], "2026-10-27T21:10")
        self.assertEqual(route["legs"][1]["arrive_at"], "2026-10-28T06:00")
        self.assertEqual(route["legs"][2]["depart_at"], "2026-10-28T06:00")
        self.assertEqual(route["legs"][2]["arrive_at"], "2026-10-28T10:20")

    def test_28th_ryanair_bratislava_train_arriving_1620_is_excluded(self):
        for option in self.options:
            self.assertNotEqual(option["arrive_at"], "2026-10-28T16:20")
            self.assertNotEqual(option["arrive_at"], "2026-10-28T12:20")
        self.assertIsNone(
            next((o for o in self.options if o["depart_at"] == "2026-10-28T07:55"), None))

    def test_best_direct_is_27th_direct_and_best_side_route_is_the_bratislava_route(self):
        self.assertEqual(self.result["best_direct"]["kind"], "direct")
        self.assertEqual(self.result["best_direct"]["depart_at"], "2026-10-27T18:00")
        self.assertEqual(self.result["best_side_route"]["kind"], "side_route")
        self.assertEqual(self.result["best_side_route"]["arrive_at"], "2026-10-28T10:20")
        # cheapest_* pick the lowest true total per category (used for budget alerts)
        directs = [o for o in self.result["options"] if o["kind"] == "direct"]
        self.assertEqual(self.result["cheapest_direct"]["true_total_try"], min(o["true_total_try"] for o in directs))
        self.assertIn(self.result["cheapest_side_route"]["kind"], ("side_route", "via_separate"))

    def test_options_sorted_by_score_and_badges_assigned(self):
        scores = [o["score"] for o in self.options]
        self.assertEqual(scores, sorted(scores))
        cheapest = min(self.options, key=lambda o: o["true_total_try"])
        self.assertIn("En Ucuz", cheapest["badges"])
        self.assertIn("Önerilen", self.options[0]["badges"])
        fastest = min(self.options, key=lambda o: o["duration_mins"])
        self.assertIn("En Hızlı", fastest["badges"])

    def test_search_count_matches_provider_calls(self):
        # 2 direct searches + 6 Prague catchment searches per date (router internal)
        self.assertEqual(len(self.provider.calls), 14)
        self.assertEqual(self.result["flight_searches"], len(self.provider.calls))
        self.assertEqual(self.result["searched_dates"], ["2026-10-27", "2026-10-28"])

    def test_warns_for_date_whose_options_all_miss_the_deadline(self):
        # 28 Oct: KLM 13:45 and the Ryanair/train route both arrive after 12:00, so nothing is left
        self.assertEqual(self.result["warnings"], ["28.10 kalkışlı uygun seçenek bulunamadı"])

    def test_depart_after_filters_earlier_departures(self):
        result = self.planner.plan_trip(
            "ATH", "PRG", ["2026-10-27", "2026-10-28"], depart_after="2026-10-27T19:30")
        self.assertTrue(result["options"])
        for option in result["options"]:
            self.assertGreaterEqual(option["depart_at"], "2026-10-27T19:30")

    def test_side_routes_can_be_disabled(self):
        planner, provider = make_planner(OUTBOUND_TABLE)
        result = planner.plan_trip(
            "ATH", "PRG", ["2026-10-27", "2026-10-28"], arrive_by="2026-10-28T12:00",
            include_side_routes=False)
        self.assertTrue(all(o["kind"] == "direct" for o in result["options"]))
        self.assertIsNone(result["best_side_route"])
        self.assertEqual(result["flight_searches"], 2)
        self.assertEqual(len(provider.calls), 2)

    def test_max_results_caps_options(self):
        result = self.planner.plan_trip(
            "ATH", "PRG", ["2026-10-27", "2026-10-28"], arrive_by="2026-10-28T12:00", max_results=1)
        self.assertEqual(len(result["options"]), 1)
        self.assertEqual(result["options"][0]["score"], self.options[0]["score"])
        self.assertIsNotNone(result["best_direct"])

    def test_next_day_arrival_suffix_and_unparsable_times(self):
        table = {
            ("ATH", "BTS", "2026-10-27"): [
                gflight("Wizz Air", "23:50 – 01:05+1", 4447),
                gflight("Broken", "", 100.0),
            ],
        }
        planner, _ = make_planner(table)
        result = planner.plan_trip("ATH", "BTS", ["2026-10-27"], include_side_routes=False)
        self.assertEqual(len(result["options"]), 1)
        self.assertEqual(result["options"][0]["depart_at"], "2026-10-27T23:50")
        self.assertEqual(result["options"][0]["arrive_at"], "2026-10-28T01:05")

    def test_identical_flights_are_deduplicated(self):
        table = {
            ("ATH", "PRG", "2026-10-27"): [
                gflight("Aegean", "18:00 – 20:10", 6000),
                gflight("Aegean", "18:00 – 20:10", 6000),
            ],
        }
        planner, _ = make_planner(table)
        result = planner.plan_trip("ATH", "PRG", ["2026-10-27"], include_side_routes=False)
        self.assertEqual(len(result["options"]), 1)

    def test_date_without_options_produces_warning(self):
        result = self.planner.plan_trip(
            "ATH", "PRG", ["2026-10-27", "2026-10-29"], arrive_by="2026-10-28T12:00",
            include_side_routes=False)
        self.assertIn("29.10 kalkışlı uygun seçenek bulunamadı", result["warnings"])
        self.assertFalse(any(w.startswith("2026-10-27") for w in result["warnings"]), result["warnings"])


class TestReturnViaGreeceScenario(unittest.TestCase):
    """Prague -> Istanbul on 31 Oct, must exit via Greece, arrive by 31 Oct 23:59."""

    def setUp(self):
        self.planner, self.provider = make_planner(RETURN_TABLE)
        self.result = self.planner.plan_trip(
            "PRG", "IST", ["2026-10-31"], arrive_by="2026-10-31T23:59", via="Yunanistan")
        self.options = self.result["options"]

    def _find(self, kind, depart_at):
        return next((o for o in self.options if o["kind"] == kind and o["depart_at"] == depart_at), None)

    def test_single_ticket_via_athens_is_included(self):
        option = self._find("via_single_ticket", "2026-10-31T09:00")
        self.assertIsNotNone(option)
        self.assertEqual(option["via_stop"], "ATH")
        self.assertFalse(option["separate_tickets"])
        self.assertEqual(option["arrive_at"], "2026-10-31T16:30")
        self.assertEqual(len(option["legs"]), 1)

    def test_non_stop_flight_is_excluded(self):
        self.assertIsNone(
            next((o for o in self.options if o["arrive_at"] == "2026-10-31T14:45"), None))
        self.assertFalse(any(o["kind"] == "direct" for o in self.options))

    def test_single_ticket_via_non_greek_airport_is_excluded(self):
        self.assertIsNone(
            next((o for o in self.options if o["arrive_at"] == "2026-10-31T14:00"), None))

    def test_separate_tickets_prg_ath_then_ath_ist_is_included(self):
        option = self._find("via_separate", "2026-10-31T10:00")
        self.assertIsNotNone(option)
        self.assertEqual(option["via_stop"], "ATH")
        self.assertTrue(option["separate_tickets"])
        self.assertEqual(option["arrive_at"], "2026-10-31T20:30")
        self.assertEqual(option["total_try"], 6000.0)
        self.assertEqual(len(option["legs"]), 2)
        self.assertEqual(option["legs"][0]["to"], "ATH")
        self.assertEqual(option["legs"][1]["from"], "ATH")
        self.assertEqual(option["legs"][1]["to"], "IST")

    def test_second_ticket_inside_buffer_is_not_combined(self):
        for option in self.options:
            if option["kind"] == "via_separate":
                self.assertNotEqual(option["legs"][1]["depart_at"], "2026-10-31T14:00")

    def test_pair_arriving_after_deadline_is_excluded(self):
        for option in self.options:
            self.assertNotEqual(option["arrive_at"], "2026-11-01T00:30")
            self.assertLessEqual(option["arrive_at"], "2026-10-31T23:59")

    def test_no_side_routes_when_via_is_set(self):
        self.assertFalse(any(o["kind"] == "side_route" for o in self.options))
        self.assertTrue(all(o["kind"] in ("via_single_ticket", "via_separate") for o in self.options))

    def test_best_direct_and_best_side_route_for_via(self):
        self.assertEqual(self.result["best_direct"]["kind"], "via_single_ticket")
        self.assertEqual(self.result["best_side_route"]["kind"], "via_separate")
        self.assertEqual(self.result["best_side_route"]["depart_at"], "2026-10-31T10:00")

    def test_separate_ticket_risk_and_badges(self):
        separate = self._find("via_separate", "2026-10-31T10:00")
        single = self._find("via_single_ticket", "2026-10-31T09:00")
        # separate: 6000 + 10.5h*150 + 500 ; single: 9000 + 7.5h*150
        self.assertLess(separate["score"], single["score"])
        self.assertIn("Önerilen", separate["badges"])
        self.assertIn("En Ucuz", separate["badges"])
        self.assertIn("En Hızlı", single["badges"])

    def test_search_count_is_own_calls_only(self):
        # Direct PRG->IST, PRG->ATH, ATH->IST (same day only: next day is past arrive_by),
        # then PRG->SKG, PRG->HER, PRG->RHO (empty, so no second leg searched)
        self.assertEqual(len(self.provider.calls), 6)
        self.assertEqual(self.result["flight_searches"], 6)
        self.assertEqual(self.result["searched_dates"], ["2026-10-31"])

    def test_via_list_of_codes_works(self):
        planner, _ = make_planner(RETURN_TABLE)
        result = planner.plan_trip(
            "PRG", "IST", ["2026-10-31"], arrive_by="2026-10-31T23:59", via=["ATH"])
        self.assertEqual(result["query"]["resolved_via"], ["ATH"])
        self.assertTrue(any(o["kind"] == "via_separate" for o in result["options"]))


class TestViaWarnings(unittest.TestCase):

    def test_unresolvable_via_warns_and_searches_nothing(self):
        planner, provider = make_planner(RETURN_TABLE)
        result = planner.plan_trip("PRG", "IST", ["2026-10-31"], via="xyz")
        self.assertEqual(result["options"], [])
        self.assertIsNone(result["best_direct"])
        self.assertIsNone(result["best_side_route"])
        self.assertEqual(provider.calls, [])
        self.assertEqual(result["flight_searches"], 0)
        self.assertTrue(any("xyz" in w for w in result["warnings"]))
        # No option for any date: the general message replaces the per-date note
        self.assertIn("Seçilen tarihler için kurallara uyan seçenek bulunamadı", result["warnings"])
        self.assertFalse(any("kalkışlı" in w for w in result["warnings"]), result["warnings"])

    def test_registry_resolve_via_unknown_token(self):
        self.assertEqual(GeoClusterRegistry().resolve_via("xyz"), [])


class TestPlannerValidation(unittest.TestCase):

    def test_invalid_date_raises(self):
        planner, _ = make_planner({})
        with self.assertRaises(ValueError):
            planner.plan_trip("ATH", "PRG", ["28-10-2026"])

    def test_invalid_deadline_raises(self):
        planner, _ = make_planner({})
        with self.assertRaises(ValueError):
            planner.plan_trip("ATH", "PRG", ["2026-10-28"], arrive_by="2026-10-28 12:00")

    def test_empty_dates_raise(self):
        planner, _ = make_planner({})
        with self.assertRaises(ValueError):
            planner.plan_trip("ATH", "PRG", [])



# ---------------------------------------------------------------- route engine mode

def eopt(kind, total, depart, arrive, duration, *, score=None, risk="low", stopover=None, via_stop=None,
         legs=None):
    """An option shaped like RouteEngine's output (kind, costs, risk, stopover, ...)."""
    return {
        "kind": kind,
        "depart_at": depart,
        "arrive_at": arrive,
        "total_try": total,
        "true_total_try": total,
        "duration_mins": duration,
        "separate_tickets": kind in ("multi_flight", "via_separate"),
        "via_stop": via_stop,
        "night_wait": False,
        "has_estimated_segments": False,
        "score": float(total if score is None else score),
        "badges": [],
        "legs": legs or [],
        "stops": [],
        "risk": {"level": risk, "score": 0, "reasons": [], "connections": []},
        "cost_breakdown": {"total": total},
        "stopover": stopover,
    }


class FakeRouteEngine:
    """Stands in for RouteEngine.search. Canned options per (origin, destination); records every call."""

    def __init__(self, responses=None, budget_exhausted=False, warnings=None, via_groups=None):
        self.responses = responses or {}
        self.budget_exhausted = budget_exhausted
        self.warnings = list(warnings or [])
        # Airport codes per fixed waypoint, as RouteEngine reports them in query.resolved.via
        self.via_groups = [list(group) for group in (via_groups or [])]
        self.calls = []

    def search(self, origin, destination, depart_dates, **kwargs):
        self.calls.append(dict(origin=origin, destination=destination, depart_dates=list(depart_dates), **kwargs))
        options = sorted((dict(o, badges=[]) for o in self.responses.get((origin, destination), [])),
                         key=lambda o: (o["score"], o["true_total_try"]))
        if kwargs.get("max_stops") == 0:  # a single main leg only, as the real engine would return
            options = [o for o in options if o["kind"] in ("direct", "ground")]
        cheapest = min(options, key=lambda o: o["true_total_try"], default=None)
        fastest = min(options, key=lambda o: o["duration_mins"], default=None)
        if options:
            options[0]["badges"].append("Önerilen")
        return {
            "query": {"resolved": {
                "origin": [origin], "destination": [destination],
                "via": self.via_groups if kwargs.get("via_fixed") else [],
            }},
            "options": options[: kwargs.get("max_results", 10)],
            "best": options[0] if options else None,
            "cheapest": cheapest,
            "fastest": fastest,
            "flight_searches": 3,
            "budget_exhausted": self.budget_exhausted,
            "candidates": {"origin_side": ["KLV"], "dest_side": ["BTS"], "hubs": ["BUD"]},
            "warnings": list(self.warnings),
        }


ATH_PRG_OPTIONS = [
    eopt("direct", 16000.0, "2030-10-28T10:00", "2030-10-28T12:30", 150, score=16200.0),
    eopt("multi_flight", 5000.0, "2030-10-28T07:55", "2030-10-28T13:00", 305, score=5500.0,
         legs=[{"type": "flight", "from": "ATH", "to": "BTS"}]),
    eopt("side_route", 6000.0, "2030-10-28T06:00", "2030-10-28T13:00", 420, score=6100.0,
         stopover={"place": "Selanik", "mins": 480}),
    eopt("ground", 4000.0, "2030-10-28T08:00", "2030-10-28T12:00", 240, score=9500.0, risk="high"),
]


def make_engine_planner(responses, **engine_kwargs):
    engine = FakeRouteEngine(responses, **engine_kwargs)
    provider = StubFlightProvider({})
    planner = TripPlanner(provider, None, route_engine=engine)
    return planner, engine


class RouteEngineMappingTests(unittest.TestCase):
    def setUp(self):
        self.planner, self.engine = make_engine_planner({("ATH", "PRG"): ATH_PRG_OPTIONS})
        self.result = self.planner.plan_trip("ATH", "PRG", ["2030-10-28"])

    def test_contract_keys_are_present(self):
        for key in ("query", "options", "best_direct", "best_side_route", "cheapest_direct",
                    "cheapest_side_route", "searched_dates", "flight_searches", "warnings",
                    "budget_exhausted", "candidates", "best", "cheapest", "fastest"):
            self.assertIn(key, self.result)
        self.assertEqual(self.result["query"]["engine"], "route_engine")
        self.assertEqual(self.result["query"]["depart_dates"], ["2030-10-28"])
        self.assertEqual(self.result["flight_searches"], 3)
        self.assertFalse(self.result["budget_exhausted"])

    def test_direct_kind_is_direct_category(self):
        self.assertEqual(self.result["best_direct"]["kind"], "direct")
        self.assertEqual(self.result["cheapest_direct"]["kind"], "direct")

    def test_side_categories_include_multi_flight_and_ground(self):
        # Best-scored side is the multi-flight (score 5500); cheapest side is the ground leg (4000)
        self.assertEqual(self.result["best_side_route"]["kind"], "multi_flight")
        self.assertEqual(self.result["cheapest_side_route"]["kind"], "ground")
        self.assertEqual(self.result["cheapest_side_route"]["true_total_try"], 4000.0)

    def test_options_keep_engine_fields(self):
        side = next(o for o in self.result["options"] if o["kind"] == "side_route")
        self.assertEqual(side["stopover"], {"place": "Selanik", "mins": 480})
        self.assertEqual(side["risk"]["level"], "low")
        self.assertEqual(side["cost_breakdown"], {"total": 6000.0})

    def test_best_cheapest_fastest_come_from_the_engine(self):
        self.assertEqual(self.result["best"]["true_total_try"], 5000.0)
        self.assertEqual(self.result["cheapest"]["true_total_try"], 4000.0)
        self.assertEqual(self.result["fastest"]["duration_mins"], 150)

    def test_via_string_becomes_via_fixed_list_and_single_ticket_direct(self):
        planner, engine = make_engine_planner({("ATH", "PRG"): [
            eopt("direct", 7000.0, "2030-10-28T10:00", "2030-10-28T14:00", 240, via_stop="BUD"),
            eopt("multi_flight", 5000.0, "2030-10-28T07:00", "2030-10-28T13:00", 360, via_stop="BUD"),
        ]})
        result = planner.plan_trip("ATH", "PRG", ["2030-10-28"], via="Budapeşte")
        self.assertEqual(engine.calls[0]["via_fixed"], ["Budapeşte"])
        self.assertEqual(result["best_direct"]["kind"], "via_single_ticket")
        self.assertEqual(result["best_side_route"]["kind"], "via_separate")
        self.assertEqual(result["query"]["via"], "Budapeşte")

    def test_via_list_is_kept_in_order(self):
        planner, engine = make_engine_planner({("ATH", "PRG"): ATH_PRG_OPTIONS})
        planner.plan_trip("ATH", "PRG", ["2030-10-28"], via=["Budapeşte", "Viyana"])
        self.assertEqual(engine.calls[0]["via_fixed"], ["Budapeşte", "Viyana"])

    def test_no_via_keeps_kinds_and_sends_no_waypoints(self):
        self.assertIsNone(self.engine.calls[0]["via_fixed"])
        self.assertEqual({o["kind"] for o in self.result["options"]}, {"direct", "multi_flight", "side_route", "ground"})

    def test_include_side_routes_false_means_no_stops_and_no_ground(self):
        result = self.planner.plan_trip("ATH", "PRG", ["2030-10-28"], include_side_routes=False)
        self.assertEqual(self.engine.calls[-1]["max_stops"], 0)
        self.assertNotIn("ground", {o["kind"] for o in result["options"]})
        self.assertIsNone(result["best_side_route"])
        self.assertIsNone(result["cheapest_side_route"])
        self.assertIn("Önerilen", result["options"][0]["badges"])  # badges recomputed for the filtered list

    def test_rules_are_forwarded_to_the_engine(self):
        self.planner.plan_trip(
            "ATH", "PRG", ["2030-10-28"], arrive_by="2030-10-28T18:00", depart_after="2030-10-28T06:00",
            max_stops=1, flight_search_budget=7, min_stay_mins={"Selanik": 480},
            cabin_bag=False, checked_bag=True, time_value_try_per_hour=200.0,
        )
        call = self.engine.calls[-1]
        self.assertEqual(call["arrive_by"], "2030-10-28T18:00")
        self.assertEqual(call["depart_after"], "2030-10-28T06:00")
        self.assertEqual(call["max_stops"], 1)
        self.assertEqual(call["flight_search_budget"], 7)
        self.assertEqual(call["min_stay_mins"], {"Selanik": 480})
        self.assertFalse(call["cabin_bag"])
        self.assertTrue(call["checked_bag"])
        self.assertEqual(call["time_value_try_per_hour"], 200.0)

    def test_max_results_trims_options_but_category_best_sees_everything(self):
        many = [eopt("side_route", 9000.0 - i, "2030-10-28T06:00", "2030-10-28T12:00", 360, score=i)
                for i in range(12)]
        many[-1] = eopt("side_route", 100.0, "2030-10-28T06:00", "2030-10-28T12:00", 360, score=50.0)
        planner, _ = make_engine_planner({("ATH", "PRG"): many})
        result = planner.plan_trip("ATH", "PRG", ["2030-10-28"], max_results=3)
        self.assertEqual(len(result["options"]), 3)
        self.assertEqual(result["cheapest_side_route"]["true_total_try"], 100.0)
        self.assertEqual(result["query"]["max_results"], 3)

    def test_budget_and_warnings_are_passed_through(self):
        planner, _ = make_engine_planner({("ATH", "PRG"): ATH_PRG_OPTIONS},
                                         budget_exhausted=True, warnings=["2030-10-28 için seçenek yok"])
        result = planner.plan_trip("ATH", "PRG", ["2030-10-28"])
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(result["warnings"], ["2030-10-28 için seçenek yok"])


class RouteEngineFlexTests(unittest.TestCase):
    def test_flex_expands_dates_and_summarises_against_requested_date(self):
        planner, engine = make_engine_planner({("ATH", "PRG"): [
            eopt("direct", 6000.0, "2030-10-28T10:00", "2030-10-28T12:00", 120),
            eopt("direct", 4000.0, "2030-10-29T10:00", "2030-10-29T12:00", 120),
        ]})
        result = planner.plan_trip("ATH", "PRG", ["2030-10-28"], flex_days=1)
        self.assertEqual(engine.calls[0]["depart_dates"], ["2030-10-27", "2030-10-28", "2030-10-29"])
        self.assertEqual(result["query"]["depart_dates"], ["2030-10-28"])
        self.assertEqual(result["query"]["flex_dates"], ["2030-10-27", "2030-10-28", "2030-10-29"])
        flex = result["flex"]
        self.assertEqual(flex["requested_date"], "2030-10-28")
        self.assertEqual(flex["requested_best"], 6000.0)
        self.assertEqual(flex["best_date"], "2030-10-29")
        self.assertEqual(flex["best_price"], 4000.0)
        self.assertEqual(flex["saving_vs_requested"], 2000.0)

    def test_flex_is_capped_at_seven_dates(self):
        planner, engine = make_engine_planner({})
        planner.plan_trip("ATH", "PRG", ["2030-10-01", "2030-10-20"], flex_days=3)
        self.assertEqual(len(engine.calls[0]["depart_dates"]), 7)

    def test_no_flex_means_no_flex_key_and_one_date_searched(self):
        planner, engine = make_engine_planner({})
        result = planner.plan_trip("ATH", "PRG", ["2030-10-28"])
        self.assertNotIn("flex", result)
        self.assertEqual(engine.calls[0]["depart_dates"], ["2030-10-28"])


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.engine = FakeRouteEngine({
            ("ATH", "PRG"): [eopt("direct", 6000.0, "2030-10-28T06:00", "2030-10-28T12:00", 360)],
            ("PRG", "ATH"): [
                eopt("direct", 2000.0, "2030-10-28T20:00", "2030-10-28T23:00", 180),   # stay 8 h
                eopt("direct", 2500.0, "2030-11-02T08:00", "2030-11-02T11:00", 180),   # stay 4 days
            ],
        })
        self.planner = TripPlanner(StubFlightProvider({}), None, route_engine=self.engine)

    def test_return_search_swaps_places_and_uses_return_rules(self):
        result = self.planner.plan_trip(
            "ATH", "PRG", ["2030-10-28"], return_dates=["2030-11-02"],
            return_arrive_by="2030-11-02T18:00", return_via="Budapeşte")
        back = self.engine.calls[-1]
        self.assertEqual((back["origin"], back["destination"]), ("PRG", "ATH"))
        self.assertEqual(back["depart_dates"], ["2030-11-02"])
        self.assertEqual(back["arrive_by"], "2030-11-02T18:00")
        self.assertEqual(back["via_fixed"], ["Budapeşte"])
        self.assertEqual(result["roundtrip"]["return"]["query"]["origin"], "PRG")

    def test_combos_respect_min_stay_hours(self):
        result = self.planner.plan_trip("ATH", "PRG", ["2030-10-28"], return_dates=["2030-11-02"],
                                        min_stay_hours=24.0)
        combos = result["roundtrip"]["combos"]
        self.assertEqual(len(combos), 1)
        self.assertEqual(combos[0]["return"]["depart_at"], "2030-11-02T08:00")
        self.assertEqual(combos[0]["total_try"], 8500.0)
        self.assertGreater(combos[0]["stay_hours"], 24.0)

    def test_zero_min_stay_keeps_both_return_options(self):
        result = self.planner.plan_trip("ATH", "PRG", ["2030-10-28"], return_dates=["2030-11-02"])
        self.assertEqual(len(result["roundtrip"]["combos"]), 2)

    def test_no_return_dates_means_no_roundtrip_key(self):
        result = self.planner.plan_trip("ATH", "PRG", ["2030-10-28"])
        self.assertNotIn("roundtrip", result)
        self.assertEqual(len(self.engine.calls), 1)

    def test_no_combo_when_return_is_before_outbound_arrival(self):
        result = self.planner.plan_trip("ATH", "PRG", ["2030-10-28"], return_dates=["2030-10-28"],
                                        min_stay_hours=500.0)
        self.assertEqual(result["roundtrip"]["combos"], [])


class _RecordingSearchLog:
    def __init__(self, series=None, error=None):
        self.series = series or []
        self.error = error
        self.calls = []

    def price_series(self, origin, destination, date, source="google_flights"):
        self.calls.append((origin, destination, date))
        if self.error is not None:
            raise self.error
        return list(self.series)


class AdviceTests(unittest.TestCase):
    SERIES = [("2020-01-01T00:00", 9000.0), ("2020-01-02T00:00", 8000.0), ("2020-01-03T00:00", 7000.0)]

    def test_advice_uses_the_first_date_and_best_price(self):
        log = _RecordingSearchLog(self.SERIES)
        engine = FakeRouteEngine({("ATH", "PRG"): [eopt("direct", 6000.0, "2030-10-28T06:00", "2030-10-28T12:00", 360)]})
        planner = TripPlanner(StubFlightProvider({}), None, route_engine=engine, search_log=log)
        result = planner.plan_trip("ATH", "PRG", ["2030-10-28"])
        self.assertEqual(log.calls, [("ATH", "PRG", "2030-10-28")])
        self.assertEqual(result["advice"]["advice"], "al")  # 6000 is below every past observation

    def test_no_search_log_means_no_advice_key(self):
        planner, _ = make_engine_planner({})
        self.assertNotIn("advice", planner.plan_trip("ATH", "PRG", ["2030-10-28"]))

    def test_failing_advice_is_none_not_an_error(self):
        log = _RecordingSearchLog(error=RuntimeError("db locked"))
        planner = TripPlanner(StubFlightProvider({}), None, route_engine=FakeRouteEngine({}), search_log=log)
        result = planner.plan_trip("ATH", "PRG", ["2030-10-28"])
        self.assertIsNone(result["advice"])


class LegacyPathUnchangedTests(unittest.TestCase):
    """Without route_engine the original planner runs; the new keyword arguments do not change it."""

    def test_route_only_arguments_are_ignored_by_the_legacy_path(self):
        planner, _ = make_planner(OUTBOUND_TABLE)
        plain = planner.plan_trip("ATH", "PRG", ["2026-10-27", "2026-10-28"], arrive_by="2026-10-28T12:00")
        planner2, _ = make_planner(OUTBOUND_TABLE)
        with_rules = planner2.plan_trip(
            "ATH", "PRG", ["2026-10-27", "2026-10-28"], arrive_by="2026-10-28T12:00",
            max_stops=0, flight_search_budget=3, min_stay_mins={"Selanik": 60}, cabin_bag=False,
        )
        self.assertEqual(plain, with_rules)
        self.assertEqual(set(plain), {"query", "options", "best_direct", "best_side_route", "cheapest_direct",
                                      "searched_dates", "cheapest_side_route", "flight_searches", "warnings"})

    def test_legacy_options_are_capped_by_max_results(self):
        planner, _ = make_planner(OUTBOUND_TABLE)
        result = planner.plan_trip("ATH", "PRG", ["2026-10-27", "2026-10-28"], include_side_routes=False,
                                   max_results=1)
        self.assertEqual(len(result["options"]), 1)
        self.assertEqual(result["query"]["max_results"], 1)

    def test_legacy_roundtrip_and_flex_work_without_route_engine(self):
        table = {
            ("ATH", "PRG", "2030-10-27"): [gflight("Aegean", "18:00 – 20:10", 6000)],
            ("ATH", "PRG", "2030-10-28"): [gflight("KLM", "13:45 – 19:30", 16286)],
            ("PRG", "ATH", "2030-11-02"): [gflight("Aegean", "10:00 – 13:15", 2500)],
        }
        planner, provider = make_planner(table)
        result = planner.plan_trip("ATH", "PRG", ["2030-10-28"], flex_days=1, return_dates=["2030-11-02"])
        self.assertIn("ATH", [c[0] for c in provider.calls])
        self.assertEqual(sorted({c[2] for c in provider.calls if c[:2] == ("ATH", "PRG")}),
                         ["2030-10-27", "2030-10-28", "2030-10-29"])
        self.assertEqual(result["flex"]["best_date"], "2030-10-27")
        self.assertEqual(result["roundtrip"]["return"]["query"]["origin"], "PRG")
        self.assertTrue(result["roundtrip"]["combos"])
        # The cheapest-scored outbound (6000, Aegean) with the only return (2500)
        self.assertEqual(result["roundtrip"]["combos"][0]["total_try"], 6000.0 + 2500.0)

    def test_legacy_advice_with_search_log(self):
        planner, _ = make_planner(OUTBOUND_TABLE)
        planner.search_log = _RecordingSearchLog(AdviceTests.SERIES)
        result = planner.plan_trip("ATH", "PRG", ["2026-10-28"])
        self.assertIn("advice", result)
        self.assertEqual(planner.search_log.calls[0][2], "2026-10-28")


# ---------------------------------------------------------------- station names and resolved_via

def named_leg(kind, operator, frm, to, dep, arr, price, origin_name=None, dest_name=None):
    """A leg as RouteEngine serialises it, with its station names."""
    return {"type": kind, "operator": operator, "from": frm, "to": to, "depart_at": dep, "arrive_at": arr,
            "price_try": price, "is_estimated": False, "booking_url": "",
            "origin_station_name": origin_name, "dest_station_name": dest_name}


class EngineStationNameTests(unittest.TestCase):
    GROUND_LEGS = [
        named_leg("train", "RegioJet", "Viyana", "Prag", "2030-10-28T08:00", "2030-10-28T12:15", 750.0,
                  "Wien Hbf", "Praha hl.n."),
        named_leg("transfer", "Airport Ground Transit", "PRG", "Prag", "2030-10-28T12:15", "2030-10-28T12:45",
                  0.0),
    ]

    def setUp(self):
        options = [eopt("ground", 750.0, "2030-10-28T08:00", "2030-10-28T12:15", 255, legs=self.GROUND_LEGS)]
        self.planner, self.engine = make_engine_planner(
            {("ATH", "PRG"): options}, via_groups=[["SKG"], ["SKG", "ATH"]])

    def test_engine_ground_legs_keep_station_names(self):
        result = self.planner.plan_trip("ATH", "PRG", ["2030-10-28"])
        train = result["options"][0]["legs"][0]
        self.assertEqual(train["origin_station_name"], "Wien Hbf")
        self.assertEqual(train["dest_station_name"], "Praha hl.n.")

    def test_legs_without_names_carry_null_names(self):
        result = self.planner.plan_trip("ATH", "PRG", ["2030-10-28"])
        transfer = result["options"][0]["legs"][1]
        self.assertIsNone(transfer["origin_station_name"])
        self.assertIsNone(transfer["dest_station_name"])

    def test_blank_station_names_become_null(self):
        leg = named_leg("bus", "FlixBus", "Viyana", "Prag", "2030-10-28T08:00", "2030-10-28T12:15", 300.0,
                        "   ", "Praha hl.n.")
        planner, _ = make_engine_planner({("ATH", "PRG"): [
            eopt("ground", 300.0, "2030-10-28T08:00", "2030-10-28T12:15", 255, legs=[leg])]})
        bus = planner.plan_trip("ATH", "PRG", ["2030-10-28"])["options"][0]["legs"][0]
        self.assertIsNone(bus["origin_station_name"])
        self.assertEqual(bus["dest_station_name"], "Praha hl.n.")

    def test_resolved_via_lists_the_resolved_codes_without_duplicates(self):
        result = self.planner.plan_trip("ATH", "PRG", ["2030-10-28"], via="Yunanistan")
        self.assertEqual(result["query"]["resolved_via"], ["SKG", "ATH"])
        self.assertEqual(result["query"]["via_fixed"], ["Yunanistan"])

    def test_resolved_via_is_none_without_via(self):
        result = self.planner.plan_trip("ATH", "PRG", ["2030-10-28"])
        self.assertIsNone(result["query"]["resolved_via"])


class LegacyStationNameTests(unittest.TestCase):
    """Legacy side routes come from MultiModalRouter; its segments may carry no station names."""

    def _option(self, segment):
        planner = TripPlanner(StubFlightProvider({}), None)
        journey = {"segments": [dict(segment, departure_time="06:00", duration_mins=260, price_try=900.0)],
                   "total_price_try": 900.0}
        candidate = planner._journey_candidate(journey, "2030-10-27")
        return _to_option(candidate, 60.0)

    def test_router_segment_without_names_maps_to_null(self):
        option = self._option({"type": "train", "operator": "RegioJet", "from": "Bratislava", "to": "Praha"})
        leg = option["legs"][0]
        self.assertIsNone(leg["origin_station_name"])
        self.assertIsNone(leg["dest_station_name"])

    def test_router_segment_names_are_preserved(self):
        option = self._option({"type": "train", "operator": "RegioJet", "from": "Bratislava", "to": "Praha",
                               "origin_station_name": "Bratislava hl.st.", "dest_station_name": "Praha hl.n."})
        leg = option["legs"][0]
        self.assertEqual(leg["origin_station_name"], "Bratislava hl.st.")
        self.assertEqual(leg["dest_station_name"], "Praha hl.n.")

    def test_legacy_flight_legs_carry_null_names(self):
        planner, _ = make_planner(OUTBOUND_TABLE)
        result = planner.plan_trip("ATH", "PRG", ["2026-10-27"], include_side_routes=False)
        leg = result["options"][0]["legs"][0]
        self.assertEqual(leg["type"], "flight")
        self.assertIn("origin_station_name", leg)
        self.assertIsNone(leg["origin_station_name"])
        self.assertIsNone(leg["dest_station_name"])


class LegSourceTests(unittest.TestCase):
    """Leg source and price_is_estimated survive planning on the engine and the legacy path; null when unset."""

    def _engine_leg(self, **extra):
        return {**named_leg("train", "RegioJet", "Viyana", "Prag", "2030-10-28T08:00", "2030-10-28T12:15", 750.0,
                            "Wien Hbf", "Praha hl.n."), **extra}

    def _legacy_option(self, segment):
        planner = TripPlanner(StubFlightProvider({}), None)
        journey = {"segments": [dict(segment, departure_time="06:00", duration_mins=260, price_try=900.0)],
                   "total_price_try": 900.0}
        return _to_option(planner._journey_candidate(journey, "2030-10-27"), 60.0)

    def _engine_train(self, leg):
        planner, _ = make_engine_planner({("ATH", "PRG"): [
            eopt("ground", 750.0, "2030-10-28T08:00", "2030-10-28T12:15", 255, legs=[leg])]})
        return planner.plan_trip("ATH", "PRG", ["2030-10-28"])["options"][0]["legs"][0]

    def test_engine_ground_leg_keeps_source_and_price_flag(self):
        train = self._engine_train(self._engine_leg(source="flixbus", price_is_estimated=False))
        self.assertEqual(train["source"], "flixbus")
        self.assertIs(train["price_is_estimated"], False)

    def test_engine_estimated_leg_keeps_estimated_flag(self):
        train = self._engine_train(self._engine_leg(source="estimate", price_is_estimated=True))
        self.assertEqual(train["source"], "estimate")
        self.assertIs(train["price_is_estimated"], True)

    def test_engine_leg_without_source_keys_maps_to_null(self):
        train = self._engine_train(self._engine_leg())
        self.assertIn("source", train)
        self.assertIn("price_is_estimated", train)
        self.assertIsNone(train["source"])
        self.assertIsNone(train["price_is_estimated"])

    def test_engine_blank_source_becomes_null(self):
        train = self._engine_train(self._engine_leg(source="   ", price_is_estimated=None))
        self.assertIsNone(train["source"])
        self.assertIsNone(train["price_is_estimated"])

    def test_legacy_router_segment_keeps_source_and_price_flag(self):
        option = self._legacy_option({
            "type": "bus", "operator": "FlixBus", "from": "Bratislava", "to": "Praha",
            "source": "flixbus", "price_is_estimated": False,
        })
        leg = option["legs"][0]
        self.assertEqual(leg["source"], "flixbus")
        self.assertIs(leg["price_is_estimated"], False)

    def test_legacy_router_segment_without_source_maps_to_null(self):
        option = self._legacy_option({"type": "train", "operator": "RegioJet",
                                      "from": "Bratislava", "to": "Praha"})
        leg = option["legs"][0]
        self.assertIsNone(leg["source"])
        self.assertIsNone(leg["price_is_estimated"])

    def test_legacy_flight_legs_carry_null_source(self):
        planner, _ = make_planner(OUTBOUND_TABLE)
        leg = planner.plan_trip("ATH", "PRG", ["2026-10-27"], include_side_routes=False)["options"][0]["legs"][0]
        self.assertEqual(leg["type"], "flight")
        self.assertIsNone(leg["source"])
        self.assertIsNone(leg["price_is_estimated"])

    def test_engine_legs_keep_coords(self):
        leg = self._engine_leg(from_coord={"lat": 37.9364, "lon": 23.9445}, to_coord={"lat": 50.1009, "lon": 14.2599})
        train = self._engine_train(leg)
        self.assertEqual(train["from_coord"], {"lat": 37.9364, "lon": 23.9445})
        self.assertEqual(train["to_coord"], {"lat": 50.1009, "lon": 14.2599})


if __name__ == "__main__":
    unittest.main()
