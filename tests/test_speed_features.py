"""Speed features of the route search: parallel flight searches, calendar pruning, persistent flight cache.

No network. Flight searches are fakes (a table of cards, optional calendar, optional delays); the
Google Flights page is replaced by a canned scrape in the provider tests.
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime
from unittest import mock

from travel_engine import flight_cache as fc
from travel_engine.airports import AirportDB, haversine_km
from travel_engine.flight_cache import FlightCache
from travel_engine.providers import google_flights_provider as gfp
from travel_engine.providers.google_flights_provider import GoogleFlightsProvider
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import CALENDAR_PRUNE_MARGIN, RouteEngine, _Run

DAY = "2026-10-28"
FLIGHTS_URL = "https://www.google.com/travel/flights?q=one-way%20flights%20from%20Athens%20to%20Prague&hl=tr&gl=tr&curr=TRY"


# ---------------------------------------------------------------- route engine fakes

def flight(airline, times, price, airports, *, direct=True, layover=""):
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
        "trip_type": "Tek yön",
        "badge": None,
        "google_flights_url": "https://www.google.com/travel/flights?q=test",
    }


def seg(origin_station, dest_station, dep, mins, price, operator="RegioJet", mode="train", est=False):
    return ScheduledSegment(
        origin_station=origin_station, dest_station=dest_station, departure_time=dep,
        arrival_time="", duration_mins=mins, price_try=float(price), operator=operator, mode=mode,
        date="", is_estimated=est, price_eur=None, price_is_estimated=est, source="table",
    )


class TimedFlights:
    """Flight provider fake without a calendar. Records calls and the peak number of searches in flight."""

    def __init__(self, table, delay=None, fail_on=()):
        self.table = table
        self.delay = delay
        self.fail_on = set(fail_on)
        self.calls = []
        self.peak = 0
        self._active = 0
        self._lock = threading.Lock()

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        key = (origin, destination, departure_date)
        with self._lock:
            self.calls.append(key)
            self._active += 1
            self.peak = max(self.peak, self._active)
        try:
            if self.delay is not None:
                time.sleep(self.delay(key))
            if key in self.fail_on:
                raise RuntimeError("page blocked")
            return [dict(item) for item in self.table.get(key, [])]
        finally:
            with self._lock:
                self._active -= 1


class CalendarFlights(TimedFlights):
    """Adds the calendar method. ``calendar`` maps (origin, destination) -> {date: price}."""

    def __init__(self, table, calendar, **kwargs):
        super().__init__(table, **kwargs)
        self.calendar = calendar

    def get_calendar_prices(self, origin, destination, trip_type="oneway"):
        return dict(self.calendar.get((origin, destination), {}))


class ParallelFlights(TimedFlights):
    """Declares how many searches may run at once (what GoogleFlightsProvider does with its browsers)."""

    def __init__(self, table, capacity, **kwargs):
        super().__init__(table, **kwargs)
        self.capacity = capacity

    def search_concurrency(self):
        return self.capacity


class FakeGround:
    def __init__(self, segments=None):
        self.segments = segments or {}

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=700.0):
        out = []
        for item in self.segments.get((from_place, to_place), []):
            if earliest_dep_time and item.departure_time < earliest_dep_time:
                continue
            out.append(replace(item, date=date_str))
        return out[:max_results]

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        if from_coord and to_coord:
            return haversine_km(from_coord[0], from_coord[1], to_coord[0], to_coord[1]) * 1.2
        return None


class FakeCosts:
    def airport_transfer(self, iata, at_time=None):
        return {"duration_mins": 30, "price_try": 0.0, "mode": "bus", "is_estimated": False, "note": ""}

    def cost_breakdown(self, legs, hotel_nights=0, hotel_place=None, cabin_bag=True, checked_bag=False):
        tickets = sum(leg["price_try"] for leg in legs if leg["type"] != "transfer")
        transfers = sum(leg["price_try"] for leg in legs if leg["type"] == "transfer")
        hotel = 2000.0 * hotel_nights
        return {"tickets": tickets, "baggage": 0.0, "transfers": transfers, "hotel": hotel,
                "total": tickets + transfers + hotel, "estimated_parts": []}

    def journey_risk(self, legs, separate_tickets):
        return {"level": "medium" if separate_tickets else "low", "score": 0, "reasons": [], "connections": 0}


MILANO_TABLE = {
    ("Milano", "Barcelona", DAY): [flight("Iberia", "08:00 – 10:10", 9000, "MXP ➔ BCN")],
    ("BGY", "GRO", DAY): [flight("Ryanair", "07:00 – 08:20", 1200, "BGY ➔ GRO")],
    # An expensive connection-free route from another Milan airport: the calendar can rule it out
    ("MXP", "GRO", DAY): [flight("Expensive Air", "09:00 – 11:00", 50000, "MXP ➔ GRO")],
}
MILANO_SEGMENTS = {("Girona", "Barcelona"): [seg("Girona", "Barcelona-Sants", "10:00", 60, 300)]}


def truthful_calendar(table):
    """Calendar minimum per (origin, destination) and date, as Google would show it for the same table."""
    out = {}
    for (origin, destination, date), items in table.items():
        prices = [item["total_price_try"] for item in items]
        out.setdefault((origin, destination), {})[date] = min(prices)
    return out


def run_engine(flights, budget=30, max_parallel=None, segments=MILANO_SEGMENTS):
    engine = RouteEngine(flights, FakeGround(segments), airport_db=AirportDB.load_default(),
                         costs=FakeCosts(), max_parallel_searches=max_parallel)
    return engine.search("Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=budget)


def canonical(result):
    # "trace" holds wall-clock times, which differ on every run
    if isinstance(result, dict):
        result = {k: v for k, v in result.items() if k != "trace"}
    return json.dumps(result, sort_keys=True, default=str)


def jitter(key):
    """Deterministic, uneven search durations (10-28 ms): parallel searches overlap and finish out of order."""
    return 0.01 + 0.002 * ((len(key[0]) + len(key[1])) % 10)


# ---------------------------------------------------------------- parallel flight searches

class ParallelSearchTests(unittest.TestCase):
    def test_parallel_result_is_identical_to_sequential(self):
        sequential = run_engine(TimedFlights(MILANO_TABLE, delay=jitter), max_parallel=1)
        flights = ParallelFlights(MILANO_TABLE, capacity=3, delay=jitter)
        parallel = run_engine(flights, max_parallel=3)
        self.assertEqual(canonical(sequential), canonical(parallel))
        self.assertGreaterEqual(flights.peak, 2, "two searches should have been in flight at once")

    def test_concurrency_comes_from_the_provider_unless_overridden(self):
        flights = ParallelFlights(MILANO_TABLE, capacity=2, delay=jitter)
        run_engine(flights, max_parallel=None)
        self.assertEqual(flights.peak, 2)
        flights = ParallelFlights(MILANO_TABLE, capacity=2, delay=jitter)
        run_engine(flights, max_parallel=1)
        self.assertEqual(flights.peak, 1)

    def test_provider_without_concurrency_is_searched_one_at_a_time(self):
        flights = TimedFlights(MILANO_TABLE, delay=jitter)
        run_engine(flights, max_parallel=None)
        self.assertEqual(flights.peak, 1)

    def test_budget_is_never_exceeded_in_parallel(self):
        sequential = TimedFlights(MILANO_TABLE)
        seq_result = run_engine(sequential, budget=3, max_parallel=1)
        flights = ParallelFlights(MILANO_TABLE, capacity=3, delay=jitter)
        result = run_engine(flights, budget=3, max_parallel=3)
        self.assertLessEqual(len(flights.calls), 3)
        self.assertEqual(result["flight_searches"], len(flights.calls))
        self.assertEqual(result["budget_exhausted"], seq_result["budget_exhausted"])
        self.assertEqual(sorted(flights.calls), sorted(sequential.calls))  # the same first searches, in plan order
        self.assertEqual(canonical(result["best"]), canonical(seq_result["best"]))

    def test_failing_search_is_a_warning_and_the_rest_still_runs(self):
        failing = ("BGY", "GRO", DAY)
        flights = ParallelFlights(MILANO_TABLE, capacity=2, delay=jitter, fail_on=[failing])
        result = run_engine(flights, max_parallel=2)
        self.assertIn(failing, flights.calls)
        self.assertTrue(any("Uçuş araması başarısız: BGY ➔ GRO" in w for w in result["warnings"]))
        self.assertIsNotNone(result["best"])
        self.assertEqual(result["flight_searches"], len(flights.calls))

    def test_searches_are_never_repeated(self):
        flights = ParallelFlights(MILANO_TABLE, capacity=4, delay=jitter)
        run_engine(flights, max_parallel=4)
        self.assertEqual(len(flights.calls), len(set(flights.calls)))


# ---------------------------------------------------------------- calendar pruning and ordering

class CalendarPruneTests(unittest.TestCase):
    def test_leg_far_above_the_best_option_is_not_searched(self):
        without = TimedFlights(MILANO_TABLE)
        base = run_engine(without, max_parallel=1)
        self.assertIn(("MXP", "GRO", DAY), without.calls, "the expensive leg is a planned candidate")

        flights = CalendarFlights(MILANO_TABLE, truthful_calendar(MILANO_TABLE))
        result = run_engine(flights, max_parallel=1)
        self.assertNotIn(("MXP", "GRO", DAY), flights.calls)
        self.assertGreaterEqual(result["pruned_searches"], 1)
        self.assertLess(len(flights.calls), len(without.calls))
        # Pruning is conservative: the best and the cheapest option do not change
        self.assertEqual(canonical(result["best"]), canonical(base["best"]))
        self.assertEqual(result["cheapest"]["true_total_try"], base["cheapest"]["true_total_try"])
        self.assertFalse(result["budget_exhausted"])

    def test_direct_search_is_never_pruned(self):
        calendar = truthful_calendar(MILANO_TABLE)
        calendar[("Milano", "Barcelona")] = {DAY: 10_000_000.0}  # absurd, but the direct search must still run
        flights = CalendarFlights(MILANO_TABLE, calendar)
        result = run_engine(flights, max_parallel=1)
        self.assertIn(("Milano", "Barcelona", DAY), flights.calls)
        self.assertIsNotNone(result["best"])

    def test_no_calendar_means_no_pruning(self):
        without = TimedFlights(MILANO_TABLE)
        run_engine(without, max_parallel=1)
        flights = CalendarFlights(MILANO_TABLE, calendar={})
        result = run_engine(flights, max_parallel=1)
        self.assertEqual(result["pruned_searches"], 0)
        self.assertEqual(flights.calls, without.calls)

    def test_prune_margin_is_applied_to_the_best_score(self):
        self.assertGreaterEqual(CALENDAR_PRUNE_MARGIN, 1.0)
        flights = CalendarFlights(MILANO_TABLE, truthful_calendar(MILANO_TABLE))
        engine = RouteEngine(flights, FakeGround(MILANO_SEGMENTS), airport_db=AirportDB.load_default(),
                             costs=FakeCosts())
        run = self._run(engine, flights)
        run.flight_version = 1
        run.best_score_memo = (1, 1000.0)  # pretend the best option scores 1000
        price_just_inside = 1000.0 * CALENDAR_PRUNE_MARGIN - 1
        flights.calendar[("MXP", "GRO")] = {DAY: price_just_inside}
        self.assertFalse(run.should_prune("side_pairs", ("MXP", "MXP"), ("GRO", "GRO"), DAY))
        flights.calendar[("MXP", "GRO")] = {DAY: 1000.0 * CALENDAR_PRUNE_MARGIN + 1}
        self.assertTrue(run.should_prune("side_pairs", ("MXP", "MXP"), ("GRO", "GRO"), DAY))

    def test_calendar_orders_pairs_cheapest_first_within_a_round(self):
        calendar = {("BGY", "GRO"): {DAY: 900.0}, ("MXP", "GRO"): {DAY: 300.0}}
        flights = CalendarFlights({}, calendar)
        run = self._run(RouteEngine(flights, FakeGround(), airport_db=AirportDB.load_default(), costs=FakeCosts()),
                        flights, dates=[DAY])
        pairs = [(("BGY", "BGY"), ("GRO", "GRO")), (("MXP", "MXP"), ("GRO", "GRO"))]
        queue = run.round_robin(pairs)
        self.assertEqual([item[0][0] for item in queue], ["MXP", "BGY"])

    def test_without_calendar_round_robin_keeps_the_plan_order(self):
        flights = TimedFlights({})
        run = self._run(RouteEngine(flights, FakeGround(), airport_db=AirportDB.load_default(), costs=FakeCosts()),
                        flights, dates=[DAY])
        pairs = [(("BGY", "BGY"), ("GRO", "GRO")), (("MXP", "MXP"), ("GRO", "GRO"))]
        self.assertEqual([item[0][0] for item in run.round_robin(pairs)], ["BGY", "MXP"])

    @staticmethod
    def _run(engine, flights, dates=None):
        return _Run(
            engine=engine, origin="Milano", destination="Barcelona", dates=list(dates or [DAY]),
            arrive_by=None, depart_after=None, max_stops=2, via_tokens=None, budget=10, time_value=150.0,
            min_stay={}, cabin_bag=True, checked_bag=False, costs=FakeCosts(), db=engine.airport_db,
            ground=None,
        )


# ---------------------------------------------------------------- persistent cache in the provider

def _card(price=4200.0):
    return {
        "source": "Google Flights (Canlı)", "airline": "Test Air", "times": "08:00 – 10:00", "duration": "2 sa.",
        "airports": "ATH ➔ PRG", "stops": "Aktarmasız", "stops_count": 0, "is_direct": True, "layover_info": "",
        "total_price_try": float(price), "currency": "TRY", "trip_type": "Tek Yön", "badge": None,
        "google_flights_url": FLIGHTS_URL,
    }


SCENARIOS = {
    "ok": {"body": "Uçuşlar", "cards": [_card()], "calendar": {"2026-12-15": 4200.0}},
    "no_flights": {"body": "Hiçbir sonuç yok.", "cards": [], "calendar": {}},
    "empty_page": {"body": "Ana içeriğe geç", "cards": [], "calendar": {}},
}


class PersistentProviderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fc_provider_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "flight_cache.db")
        self.now = 2_000_000.0
        self.scenario = "ok"
        self.scrapes = []
        GoogleFlightsProvider.reset_shared_state()
        self.addCleanup(GoogleFlightsProvider.reset_shared_state)
        patchers = [
            mock.patch.object(gfp, "PLAYWRIGHT_AVAILABLE", True),
            mock.patch.object(GoogleFlightsProvider, "_run_scrape",
                              lambda _provider, *args, **kwargs: self._scrape(*args, **kwargs)),
            mock.patch.dict(os.environ, {"TRAVELPLAN_FLIGHT_CACHE": self.path}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _scrape(self, url, is_one_way, cal_key, now_ts, want_calendar=True):
        self.scrapes.append(url)
        data = SCENARIOS[self.scenario]
        state = gfp._new_scrape_state()
        state.update(
            page_read=True, page_url=FLIGHTS_URL, body_text=data["body"], card_count=len(data["cards"]),
            raw_results=[dict(card) for card in data["cards"]], calendar=dict(data["calendar"]),
        )
        return state

    def store(self):
        return FlightCache(self.path, clock=lambda: self.now)

    def provider(self, **kwargs):
        kwargs.setdefault("min_interval_secs", 0.0)
        kwargs.setdefault("flight_cache", self.store())
        return GoogleFlightsProvider(**kwargs)

    def search(self, provider, day="2026-12-15"):
        return provider.search_flights(origin="Athens", destination="Prague", departure_date=day,
                                       return_date=None, trip_type="oneway")

    def test_a_fresh_provider_is_served_from_disk_without_a_page(self):
        first = self.provider()
        results = self.search(first)
        self.assertEqual(len(self.scrapes), 1)
        second = self.provider()  # empty in-memory cache, same database
        self.assertEqual(self.search(second), results)
        self.assertEqual(len(self.scrapes), 1)
        self.assertEqual(second.last_search_status["source"], "cache")
        self.assertEqual(second.health_status()["recent"], [], "a cache hit is not a live read")

    def test_in_memory_cache_still_answers_before_the_disk(self):
        provider = self.provider()
        self.search(provider)
        self.search(provider)
        self.assertEqual(len(self.scrapes), 1)

    def test_no_flights_is_reused_for_ten_minutes(self):
        self.scenario = "no_flights"
        self.assertEqual(self.search(self.provider()), [])
        self.now += 9 * 60
        self.assertEqual(self.search(self.provider()), [])
        self.assertEqual(len(self.scrapes), 1)
        self.now += 2 * 60
        self.assertEqual(self.search(self.provider()), [])
        self.assertEqual(len(self.scrapes), 2)

    def test_found_results_are_reused_for_forty_five_minutes(self):
        self.search(self.provider())
        self.now += 44 * 60
        self.search(self.provider())
        self.assertEqual(len(self.scrapes), 1)
        self.now += 2 * 60
        self.search(self.provider())
        self.assertEqual(len(self.scrapes), 2)

    def test_failed_reads_are_never_stored(self):
        for name in ("empty_page", "empty_page"):
            self.scenario = name
            self.assertEqual(self.search(self.provider()), [])
        self.assertEqual(len(self.scrapes), 2)
        self.assertEqual(self.store().row_count(), 0)

    def test_calendar_is_stored_and_read_back_without_a_page(self):
        self.search(self.provider())
        self.assertEqual(self.provider().get_calendar_prices("Athens", "Prague", "oneway"), {"2026-12-15": 4200.0})
        self.assertEqual(len(self.scrapes), 1)

    def test_roundtrip_searches_use_their_own_entries(self):
        provider = self.provider()
        provider.search_flights(origin="Athens", destination="Prague", departure_date="2026-12-15",
                                return_date="2026-12-20", trip_type="roundtrip")
        self.search(self.provider())  # the one-way search of the same date is a different entry
        self.assertEqual(len(self.scrapes), 2)

    def test_disabled_cache_never_creates_a_file(self):
        with mock.patch.dict(os.environ, {"TRAVELPLAN_FLIGHT_CACHE": "off"}):
            provider = GoogleFlightsProvider(min_interval_secs=0.0)
            self.assertIsNone(provider.persistent_cache())
            self.search(provider)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(len(self.scrapes), 1)

    def test_database_is_created_on_the_first_search_not_at_construction(self):
        provider = GoogleFlightsProvider(min_interval_secs=0.0)
        self.assertFalse(os.path.exists(self.path))
        self.search(provider)
        self.assertTrue(os.path.exists(self.path))


# ---------------------------------------------------------------- ground: one day per pair

class LiveGround(FakeGround):
    """Adds ``find_ground_live`` (the live part of RailBusProvider) and records both entry points."""

    def __init__(self, segments=None):
        super().__init__(segments)
        self.live_calls = []
        self.live_answers = {}  # live call -> number of segments it gave
        self.ground_calls = []

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=700.0):
        self.ground_calls.append((from_place, to_place, date_str, earliest_dep_time))
        return super().find_ground(from_place, to_place, date_str, earliest_dep_time, from_coord, to_coord,
                                   max_results, max_road_km)

    def find_ground_live(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                         to_coord=None, max_results=6):
        key = (from_place, to_place, date_str, earliest_dep_time)
        self.live_calls.append(key)
        found = super().find_ground(from_place, to_place, date_str, earliest_dep_time, from_coord, to_coord,
                                    max_results)
        self.live_answers[key] = len(found)
        return found


class SharedGroundTests(unittest.TestCase):
    def test_live_answers_are_asked_once_per_variant_and_never_through_find_ground(self):
        ground = LiveGround(MILANO_SEGMENTS)
        engine = RouteEngine(TimedFlights(MILANO_TABLE), ground, airport_db=AirportDB.load_default(),
                             costs=FakeCosts())
        engine.search("Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=30)
        self.assertEqual(len(ground.live_calls), len(set(ground.live_calls)))  # no repeated variant
        for key in ground.ground_calls:
            # find_ground runs only when the live entry point gave nothing for that exact variant.
            self.assertEqual(ground.live_answers.get(key), 0, key)

    def test_result_is_identical_with_and_without_the_live_entry_point(self):
        plain = RouteEngine(TimedFlights(MILANO_TABLE), FakeGround(MILANO_SEGMENTS),
                            airport_db=AirportDB.load_default(), costs=FakeCosts())
        shared = RouteEngine(TimedFlights(MILANO_TABLE), LiveGround(MILANO_SEGMENTS),
                             airport_db=AirportDB.load_default(), costs=FakeCosts())
        a = plain.search("Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=30)
        b = shared.search("Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=30)
        self.assertEqual(canonical(a), canonical(b))


if __name__ == "__main__":
    unittest.main()
