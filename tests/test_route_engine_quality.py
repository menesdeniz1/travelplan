"""Quality tests for travel_engine.route_engine: side eligibility, budget policy, next-day connections.

No network: fake flight/ground providers and costs. The airport data is the real AirportDB, except where a
stub is used on purpose (a DB without ground_reachable, a DB with a stricter ground_reachable).
"""

import math
import unittest
from dataclasses import replace
from datetime import datetime
from unittest import mock

from travel_engine import route_engine
from travel_engine.airports import AirportDB, haversine_km
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import (
    BUDGET_WARNING,
    LCC_HUBS,
    SEARCH_LIMIT_WARNING,
    RouteEngine,
)

DAY = "2026-10-28"
NEXT = "2026-10-29"
ISLANDS = {"JTR", "CHQ", "ZTH"}


# ---------------------------------------------------------------- fakes

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


class FakeFlights:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        return [dict(item) for item in self.table.get((origin, destination, departure_date), [])]


class FakeGround:
    def __init__(self, segments=None):
        self.segments = segments or {}
        self.calls = []

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=700.0):
        self.calls.append((from_place, to_place, date_str, earliest_dep_time))
        out = []
        for item in self.segments.get((from_place, to_place), []):
            if earliest_dep_time and item.departure_time < earliest_dep_time:
                continue
            out.append(replace(item, date=date_str))
        return out[:max_results]

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        if from_coord and to_coord:
            return haversine_km(from_coord[0], from_coord[1], to_coord[0], to_coord[1]) * 1.25
        return None


class FakeCosts:
    """Transfers are free and 30 minutes; hotel 2000 TRY per night; separate tickets are 'medium' risk."""

    def airport_transfer(self, iata):
        return {"duration_mins": 30, "price_try": 0.0, "mode": "bus", "is_estimated": False, "note": ""}

    def cost_breakdown(self, legs, hotel_nights=0, hotel_place=None, cabin_bag=True, checked_bag=False):
        tickets = sum(leg["price_try"] for leg in legs if leg["type"] != "transfer")
        transfers = sum(leg["price_try"] for leg in legs if leg["type"] == "transfer")
        hotel = 2000.0 * hotel_nights
        return {"tickets": tickets, "baggage": 0.0, "transfers": transfers, "hotel": hotel,
                "total": tickets + transfers + hotel, "estimated_parts": []}

    def journey_risk(self, legs, separate_tickets):
        return {"level": "medium" if separate_tickets else "low", "score": 0, "reasons": [],
                "connections": 0}


class FakeSearchLog:
    """SearchLog.cheapest_destinations stand-in: a fixed average price per destination code."""

    def __init__(self, prices):
        self.prices = prices

    def cheapest_destinations(self, origin, candidates, days=90, source="google_flights"):
        scored = [(c, float(self.prices.get(c, math.inf))) for c in candidates]
        scored.sort(key=lambda item: item[1])
        return scored


class NoReachabilityDB:
    """AirportDB without ground_reachable / is_island (the method is hidden on purpose)."""

    def __init__(self, base):
        self._base = base

    def __getattr__(self, name):
        if name in ("ground_reachable", "is_island"):
            raise AttributeError(name)
        return getattr(self._base, name)


class StrictReachabilityDB(NoReachabilityDB):
    """Adds a ground_reachable that accepts nothing across the origin (ATH) : only own airports remain."""

    def ground_reachable(self, a, b):
        return a == b

    def __getattr__(self, name):
        if name == "ground_reachable":
            return self.ground_reachable
        return super().__getattr__(name)


def make_engine(table, segments=None, *, db=None, search_log=None):
    flights = FakeFlights(table)
    ground = FakeGround(segments)
    engine = RouteEngine(flights, ground, airport_db=db or AirportDB.load_default(),
                         search_log=search_log, costs=FakeCosts())
    return engine, flights, ground


def ts(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M")


def minutes_between(start, end):
    return int((ts(end) - ts(start)).total_seconds() // 60)


# ---------------------------------------------------------------- Athens -> Prague regression

class AthensPragueRegressionTests(unittest.TestCase):
    """Live case: ATH->PRG 16,286; ATH->BTS 07:55-09:10 5,524; BTS/Bratislava->Prague train 12:26-16:58."""

    TABLE = {
        ("Atina", "Prag", DAY): [flight("Aegean", "09:00 – 11:10", 16286, "ATH ➔ PRG")],
        ("ATH", "BTS", DAY): [flight("Ryanair", "07:55 – 09:10", 5524, "ATH ➔ BTS")],
        ("ATH", "VIE", DAY): [flight("Aegean", "08:00 – 10:30", 13281, "ATH ➔ VIE")],
        # Island flights are cheap; they must never be searched as origin-side ground candidates.
        ("ATH", "JTR", DAY): [flight("Aegean", "07:00 – 07:50", 900, "ATH ➔ JTR")],
        ("JTR", "Prag", DAY): [flight("Aegean", "08:00 – 09:00", 800, "JTR ➔ PRG")],
        ("ATH", "CHQ", DAY): [flight("Aegean", "07:00 – 07:50", 900, "ATH ➔ CHQ")],
        ("ATH", "ZTH", DAY): [flight("Aegean", "07:00 – 07:50", 900, "ATH ➔ ZTH")],
    }
    SEGMENTS = {
        # 900 TRY is about EUR 16.9 at the test rate.
        ("Bratislava", "Prag"): [seg("Bratislava hl.st.", "Praha hl.n.", "12:26", 272, 900)],
    }

    @classmethod
    def setUpClass(cls):
        engine, flights, _ = make_engine(cls.TABLE, cls.SEGMENTS)
        cls.result = engine.search("Atina", "Prag", [DAY], max_stops=1, flight_search_budget=10)
        cls.calls = list(flights.calls)

    def test_best_option_is_the_bratislava_chain(self):
        best = self.result["best"]
        self.assertIsNotNone(best)
        flights_in_best = [(l["from"], l["to"]) for l in best["legs"] if l["type"] == "flight"]
        self.assertEqual(flights_in_best, [("ATH", "BTS")])
        self.assertEqual(best["legs"][-1]["type"], "train")
        self.assertEqual(best["legs"][-1]["from"], "Bratislava hl.st.")
        self.assertEqual(best["legs"][-1]["depart_at"], f"{DAY}T12:26")
        self.assertTrue(best["separate_tickets"])

    def test_direct_flight_is_still_an_option(self):
        kinds = [(o["kind"], o["true_total_try"]) for o in self.result["options"]]
        self.assertIn(("direct", 16286.0), kinds)

    def test_island_airports_are_not_origin_side_candidates(self):
        origin_side = self.result["candidates"]["origin_side"]
        self.assertFalse(ISLANDS & set(origin_side), origin_side)
        self.assertNotIn(("ATH", "JTR", DAY), self.calls)
        for option in self.result["options"]:
            for leg in option["legs"]:
                self.assertNotIn("JTR", (leg["from"], leg["to"]))
                self.assertNotIn("CHQ", (leg["from"], leg["to"]))
                self.assertNotIn("ZTH", (leg["from"], leg["to"]))

    def test_dest_side_has_bts_and_no_tiny_airports(self):
        dest_side = self.result["candidates"]["dest_side"]
        self.assertIn("BTS", dest_side)
        self.assertNotIn("KLV", dest_side)
        self.assertNotIn("PED", dest_side)

    def test_budget_is_respected(self):
        self.assertLessEqual(self.result["flight_searches"], 10)
        self.assertLessEqual(len(self.calls), 10)

    def test_lcc_dest_side_search_is_planned_before_hub_chains(self):
        self.assertIn(("ATH", "BTS", DAY), self.calls)


# ---------------------------------------------------------------- budget diversity

class BudgetDiversityTests(unittest.TestCase):
    TABLE = AthensPragueRegressionTests.TABLE

    def test_small_budget_still_searches_an_lcc_dest_side_candidate(self):
        engine, flights, _ = make_engine(self.TABLE)
        result = engine.search("Atina", "Prag", [DAY], max_stops=1, flight_search_budget=6)
        lcc_dest = set(result["candidates"]["dest_side"]) & set(LCC_HUBS)
        self.assertTrue(lcc_dest)
        searched = [dest for origin, dest, _ in flights.calls if origin == "ATH"]
        self.assertTrue(set(searched) & lcc_dest, (searched, lcc_dest))
        self.assertLessEqual(len(flights.calls), 6)

    def test_history_moves_the_cheapest_known_pair_first(self):
        log = FakeSearchLog({"BER": 3000.0})
        engine, flights, _ = make_engine(self.TABLE, search_log=log)
        engine.search("Atina", "Prag", [DAY], max_stops=1, flight_search_budget=6)
        self.assertEqual(flights.calls[0], ("Atina", "Prag", DAY))
        self.assertEqual(flights.calls[1], ("ATH", "BER", DAY))

    def test_round_robin_spreads_pairs_over_dates(self):
        dates = ["2026-10-28", "2026-10-29"]
        engine, _, _ = make_engine({})
        result = engine.search("Atina", "Prag", dates, max_stops=0, flight_search_budget=0)
        self.assertEqual(result["query"]["depart_dates"], dates)
        run = route_engine._Run(
            engine=engine, origin="Atina", destination="Prag", dates=dates, arrive_by=None,
            depart_after=None, max_stops=1, via_tokens=None, budget=10, time_value=0.0, min_stay={},
            cabin_bag=True, checked_bag=False, costs=FakeCosts(), db=engine.airport_db, ground=None)
        queue = run.round_robin([(("A", "A"), ("B", "B")), (("C", "C"), ("D", "D"))])
        self.assertEqual([(q[0][0], q[2]) for q in queue],
                         [("A", DAY), ("C", NEXT), ("A", NEXT), ("C", DAY)])


# ---------------------------------------------------------------- next-day second flight

class NextDayConnectionTests(unittest.TestCase):
    """ATH->BUD lands at 21:40; the next morning BUD->PRG leaves at 07:00 (separate tickets)."""

    TABLE = {
        ("Atina", "BUD", DAY): [flight("Wizz Air", "20:00 – 21:40", 1000, "ATH ➔ BUD")],
        ("BUD", "Prag", NEXT): [flight("Wizz Air", "07:00 – 08:30", 900, "BUD ➔ PRG")],
    }

    def chains(self, result):
        return [o for o in result["options"]
                if [l["type"] for l in o["legs"]].count("flight") == 2
                and any(l["depart_at"].startswith(NEXT) for l in o["legs"] if l["type"] == "flight")]

    def test_second_flight_on_the_next_day_is_found_when_allowed(self):
        engine, flights, _ = make_engine(self.TABLE)
        result = engine.search("Atina", "Prag", [DAY], max_stops=1, flight_search_budget=30)
        self.assertIn("BUD", result["candidates"]["hubs"])
        self.assertIn(("BUD", "Prag", NEXT), flights.calls)
        chains = self.chains(result)
        self.assertTrue(chains)
        second = [l for l in chains[0]["legs"] if l["type"] == "flight"][1]
        self.assertEqual((second["from"], second["to"]), ("BUD", "PRG"))
        self.assertEqual(second["depart_at"], f"{NEXT}T07:00")
        self.assertTrue(chains[0]["separate_tickets"])

    def test_next_day_search_is_skipped_when_arrive_by_is_the_same_day(self):
        engine, flights, _ = make_engine(self.TABLE)
        result = engine.search("Atina", "Prag", [DAY], max_stops=1, flight_search_budget=30,
                               arrive_by=f"{DAY}T23:59")
        self.assertFalse([c for c in flights.calls if c[2] == NEXT])
        self.assertEqual(self.chains(result), [])

    def test_next_day_search_runs_when_arrive_by_allows_it(self):
        engine, flights, _ = make_engine(self.TABLE)
        result = engine.search("Atina", "Prag", [DAY], max_stops=1, flight_search_budget=30,
                               arrive_by=f"{NEXT}T12:00")
        self.assertIn(("BUD", "Prag", NEXT), flights.calls)
        self.assertTrue(self.chains(result))


# ---------------------------------------------------------------- fallback without ground_reachable

class FallbackRuleTests(unittest.TestCase):
    TABLE = AthensPragueRegressionTests.TABLE

    def test_fallback_excludes_islands_without_ground_reachable(self):
        db = NoReachabilityDB(AirportDB.load_default())
        self.assertFalse(hasattr(db, "ground_reachable"))
        engine, flights, _ = make_engine(self.TABLE, db=db, search_log=None)
        result = engine.search("Atina", "Prag", [DAY], max_stops=1, flight_search_budget=10)
        self.assertFalse(ISLANDS & set(result["candidates"]["origin_side"]))
        self.assertIn("BTS", result["candidates"]["dest_side"])
        self.assertFalse([c for c in flights.calls if c[0] in ISLANDS])

    def test_ground_reachable_is_consulted_when_present(self):
        db = StrictReachabilityDB(AirportDB.load_default())
        engine, _, _ = make_engine(self.TABLE, db=db)
        result = engine.search("Atina", "Prag", [DAY], max_stops=1, flight_search_budget=10)
        self.assertEqual(result["candidates"]["origin_side"], ["ATH"])
        self.assertNotIn("BTS", result["candidates"]["dest_side"])  # not reachable by ground in this stub
        self.assertEqual(result["candidates"]["dest_side"], ["PRG"])


# ---------------------------------------------------------------- user-facing DFS bound warning

class SearchLimitWarningTests(unittest.TestCase):
    def test_dfs_bound_is_reported_to_the_user(self):
        table = AthensPragueRegressionTests.TABLE
        engine, _, _ = make_engine(table, AthensPragueRegressionTests.SEGMENTS)
        with mock.patch.object(route_engine, "MAX_DFS_STATES", 3):
            result = engine.search("Atina", "Prag", [DAY], max_stops=1, flight_search_budget=10)
        self.assertIn(SEARCH_LIMIT_WARNING, result["warnings"])

    def test_no_warning_without_a_bound_hit(self):
        table = AthensPragueRegressionTests.TABLE
        engine, _, _ = make_engine(table, AthensPragueRegressionTests.SEGMENTS)
        result = engine.search("Atina", "Prag", [DAY], max_stops=1, flight_search_budget=10)
        self.assertNotIn(SEARCH_LIMIT_WARNING, result["warnings"])


# ---------------------------------------------------------------- country names and the flight-volume gate

class CountryPlaceTests(unittest.TestCase):
    def test_country_origin_ranks_airports_by_size_and_lcc_not_alphabetically(self):
        engine, _, _ = make_engine({})
        result = engine.search("Yunanistan", "Prag", [DAY], max_stops=1, flight_search_budget=0)
        resolved = result["query"]["resolved"]["origin"]
        self.assertEqual(resolved[0], "ATH")
        self.assertIn("ATH", result["candidates"]["origin_side"])
        self.assertNotIn("AOK", result["candidates"]["origin_side"])  # medium, not an LCC base

    def test_medium_airport_with_history_is_eligible(self):
        log = FakeSearchLog({"KLV": 2000.0})
        engine, _, _ = make_engine({}, search_log=log)
        result = engine.search("Atina", "Prag", [DAY], max_stops=1, flight_search_budget=0)
        self.assertIn("KLV", result["candidates"]["dest_side"])


if __name__ == "__main__":
    unittest.main()
