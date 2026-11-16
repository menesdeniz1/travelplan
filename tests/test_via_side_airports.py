"""Tests for side airports in fixed/via mode (T12)."""

import sys
import unittest
from datetime import datetime

sys.path.insert(0, ".")

from travel_engine.airports import AirportDB
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import RouteEngine
from tests.test_route_engine import FakeCosts

DAY = "2026-10-28"


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


def seg(origin_station, dest_station, dep, mins, price, operator="FlixBus", mode="bus", date=DAY):
    return ScheduledSegment(
        origin_station=origin_station, dest_station=dest_station, departure_time=dep,
        arrival_time="", duration_mins=mins, price_try=float(price),
        operator=operator, mode=mode, date=date, source="flixbus",
        is_estimated=False, price_is_estimated=False,
    )


class FakeFlights:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        res = self.table.get((origin, destination, departure_date))
        if res is None and origin == "Prag":
            res = self.table.get(("PRG", destination, departure_date))
        if res is None:
            res = []
        return [dict(item) for item in res]


class FakeGround:
    def __init__(self, segments=None, distances=None):
        self.segments = segments or {}
        self.distances = distances or {}
        self.calls = []

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=800.0):
        self.calls.append((from_place, to_place, date_str, earliest_dep_time))
        out = []
        for item in self.segments.get((from_place, to_place), []):
            if earliest_dep_time and item.departure_time < earliest_dep_time:
                continue
            out.append(item)
        return out[:max_results]

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        return self.distances.get((from_place, to_place), 100.0)


def make_engine(flight_table, ground_segments=None, ground_distances=None):
    flights = FakeFlights(flight_table)
    ground = FakeGround(ground_segments, ground_distances)
    engine = RouteEngine(flights, ground, airport_db=AirportDB.load_default(), costs=FakeCosts())
    return engine, flights, ground


class TestViaSideAirports(unittest.TestCase):
    def test_via_side_airport_finds_cheap_route(self):
        """Prag -> Yunanistan -> İstanbul:

        Prague to Greece by air is expensive (15000 TRY).
        Prague -> Budapest by bus (1000 TRY) + BUD -> SKG flight (2000 TRY) +
        Thessaloniki -> Istanbul bus (1500 TRY) is found via side airports and is #1.
        """
        ground_segs = {
            ("Prag", "Budapest"): [
                seg("Praha", "Budapest", "06:00", 360, 1000.0),
            ],
            ("Thessaloniki", "İstanbul"): [
                seg("Thessaloniki", "Istanbul", "18:00", 480, 1500.0),
            ],
        }
        ground_dist = {
            ("Prag", "Budapest"): 525.0,
            ("Thessaloniki", "İstanbul"): 590.0,
        }
        flight_table = {
            ("PRG", "SKG", DAY): [
                flight("ExpensiveAir", "10:00 – 12:00", 15000.0, "PRG-SKG"),
            ],
            ("BUD", "SKG", DAY): [
                flight("CheapAir", "15:00 – 17:30", 2000.0, "BUD-SKG"),
            ],
        }
        engine, _, _ = make_engine(flight_table, ground_segs, ground_dist)
        res = engine.search("Prag", "İstanbul", [DAY], via_fixed=["Yunanistan"], flight_search_budget=20)
        self.assertGreater(len(res["options"]), 0)
        best = res["best"]
        self.assertIsNotNone(best)
        # Check that the cheap route via BUD was chosen
        flight_legs = [l for l in best["legs"] if l["type"] == "flight"]
        self.assertEqual(len(flight_legs), 1)
        self.assertEqual(flight_legs[0]["from"], "BUD")
        self.assertEqual(flight_legs[0]["to"], "SKG")
        self.assertLess(best["true_total_try"], 10000.0)

    def test_side_airport_does_not_skip_waypoint(self):
        """Even if BUD -> IST is dirt cheap, it cannot skip waypoint Yunanistan."""
        ground_segs = {
            ("Prag", "Budapest"): [
                seg("Praha", "Budapest", "06:00", 360, 1000.0),
            ],
            ("Thessaloniki", "İstanbul"): [
                seg("Thessaloniki", "Istanbul", "18:00", 480, 1500.0),
            ],
        }
        ground_dist = {
            ("Prag", "Budapest"): 525.0,
            ("Thessaloniki", "İstanbul"): 590.0,
        }
        flight_table = {
            ("PRG", "SKG", DAY): [
                flight("ExpensiveAir", "10:00 – 12:00", 15000.0, "PRG-SKG"),
            ],
            ("BUD", "SKG", DAY): [
                flight("CheapAir", "15:00 – 17:30", 2000.0, "BUD-SKG"),
            ],
            ("BUD", "IST", DAY): [
                flight("SkipAir", "14:00 – 16:30", 500.0, "BUD-IST"),
            ],
        }
        engine, _, _ = make_engine(flight_table, ground_segs, ground_dist)
        res = engine.search("Prag", "İstanbul", [DAY], via_fixed=["Yunanistan"], flight_search_budget=20)
        self.assertGreater(len(res["options"]), 0)
        for opt in res["options"]:
            # Every option must visit Greece (e.g. SKG or ATH)
            codes = [l.get("from") for l in opt["legs"]] + [l.get("to") for l in opt["legs"]]
            self.assertTrue("SKG" in codes or "ATH" in codes, f"Option skipped Greece: {opt}")

    def test_via_on_date_applies_to_side_departure(self):
        """via_on_date for a waypoint applies when departing from that waypoint's side airport."""
        ground_segs = {
            ("Prag", "Viyana"): [
                seg("Praha", "Wien", "06:00", 240, 500.0),
            ],
            ("Viyana", "Bratislava"): [
                seg("Wien", "Bratislava", "12:00", 60, 300.0),
            ],
        }
        ground_dist = {
            ("Prag", "Viyana"): 330.0,
            ("Viyana", "Bratislava"): 65.0,
        }
        flight_table = {
            ("BTS", "IST", DAY): [
                flight("CheapAir", "16:00 – 19:00", 2000.0, "BTS-IST"),
            ],
        }
        engine, _, _ = make_engine(flight_table, ground_segs, ground_dist)
        # Viyana has via_on_date: 2026-10-29. Flight on 2026-10-28 departing from side BTS should be rejected.
        res = engine.search(
            "Prag", "İstanbul", [DAY], via_fixed=["Viyana"],
            via_on_date={"Viyana": "2026-10-29"}, flight_search_budget=20,
        )
        self.assertEqual(len(res["options"]), 0)

    def test_via_side_searches_capped_at_four_when_budget_plenty(self):
        """Even with plenty of budget (50), via_side searches must not exceed VIA_SIDE_MAX_SEARCHES (4)."""
        from scripts.bench_engine import BenchGround, BenchFlights
        flights = BenchFlights(calendar=False)
        ground = BenchGround()
        engine = RouteEngine(flights, ground, airport_db=AirportDB.load_default(), max_parallel_searches=1)
        res = engine.search(
            "İstanbul", "Prag", ["2026-10-28"],
            via_fixed=["Selanik", "Bratislava", "Viyana"],
            min_stay_mins={"Selanik": 480, "Viyana": 600},
            flight_search_budget=50,
        )
        # Main searches: 1 direct + 5 segments + 6 rolled = 12 searches.
        # via_side searches must be capped at 4, so total searches == 16.
        self.assertLessEqual(len(flights.calls), 16)
        self.assertEqual(len(flights.calls), 16)

    def test_max_stops_zero_disables_side_airports(self):
        """When max_stops=0, no side airports are considered."""
        ground_segs = {
            ("Prag", "Budapest"): [
                seg("Praha", "Budapest", "06:00", 360, 1000.0),
            ],
            ("Thessaloniki", "İstanbul"): [
                seg("Thessaloniki", "Istanbul", "18:00", 480, 1500.0),
            ],
        }
        ground_dist = {
            ("Prag", "Budapest"): 525.0,
            ("Thessaloniki", "İstanbul"): 590.0,
        }
        flight_table = {
            ("PRG", "SKG", DAY): [
                flight("DirectAir", "10:00 – 12:00", 15000.0, "PRG-SKG"),
            ],
            ("BUD", "SKG", DAY): [
                flight("CheapAir", "15:00 – 17:30", 2000.0, "BUD-SKG"),
            ],
        }
        engine, flights, _ = make_engine(flight_table, ground_segs, ground_dist)
        res = engine.search("Prag", "İstanbul", [DAY], via_fixed=["Yunanistan"], max_stops=0)
        # BUD must not have been searched
        searched_origins = {call[0] for call in flights.calls}
        self.assertNotIn("BUD", searched_origins)


if __name__ == "__main__":
    unittest.main()
