"""End to end: the real RouteEngine behind TripPlanner.plan_trip, with fake flight and ground providers.

No network and no server import. The flight and ground fakes are the same shape as in
tests/test_route_engine.py; the scenario is Milano -> Barcelona (flight, or Bergamo + bus-train chain).
"""

import unittest
from dataclasses import replace
from datetime import datetime

from travel_engine.airports import AirportDB, haversine_km
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import RouteEngine
from travel_engine.search_log import SearchLog
from travel_engine.trip_planner import TripPlanner

DAY = "2030-10-28"
RETURN_DAY = "2030-11-02"


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
    """Transfers are free, a hotel night costs 2000 TRY, separate tickets are 'medium' risk."""

    def airport_transfer(self, iata, at_time=None):
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


MILANO_BARCELONA = {
    ("Milano", "Barcelona", DAY): [flight("Iberia", "08:00 – 10:10", 9000, "MXP ➔ BCN")],
    ("BGY", "GRO", DAY): [flight("Ryanair", "07:00 – 08:20", 1200, "BGY ➔ GRO")],
    ("Barcelona", "Milano", RETURN_DAY): [flight("Iberia", "18:00 – 20:10", 8000, "BCN ➔ MXP")],
}
GIRONA_SEGMENTS = {("Girona", "Barcelona"): [seg("Girona", "Barcelona-Sants", "10:00", 60, 300)]}


def make_planner(table=None, segments=None, search_log=None):
    flights = FakeFlights(table if table is not None else MILANO_BARCELONA)
    engine = RouteEngine(flights, FakeGround(segments or GIRONA_SEGMENTS), airport_db=AirportDB.load_default(),
                         costs=FakeCosts())
    planner = TripPlanner(flights, None, route_engine=engine, search_log=search_log)
    return planner, flights


class RouteEngineThroughPlannerTests(unittest.TestCase):
    def setUp(self):
        self.planner, self.flights = make_planner()
        self.result = self.planner.plan_trip("Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=30)

    def test_contract_and_engine_keys_are_present(self):
        for key in ("options", "best_direct", "best_side_route", "cheapest_direct", "cheapest_side_route",
                    "best", "cheapest", "fastest", "budget_exhausted", "candidates", "warnings",
                    "flight_searches"):
            self.assertIn(key, self.result)
        self.assertEqual(self.result["query"]["engine"], "route_engine")
        self.assertGreater(self.result["flight_searches"], 0)

    def test_direct_flight_is_the_direct_category(self):
        self.assertIsNotNone(self.result["cheapest_direct"])
        self.assertEqual(self.result["cheapest_direct"]["kind"], "direct")
        self.assertEqual(self.result["cheapest_direct"]["true_total_try"], 9000.0)

    def test_side_route_is_the_side_category_and_beats_direct(self):
        best = self.result["best"]
        self.assertEqual(best["kind"], "side_route")
        self.assertIn("side_route", [o["kind"] for o in self.result["options"]])
        self.assertEqual([leg["type"] for leg in best["legs"]], ["transfer", "flight", "transfer", "train"])
        self.assertLess(best["true_total_try"], self.result["cheapest_direct"]["true_total_try"])

    def test_options_carry_risk_and_cost_breakdown(self):
        for option in self.result["options"]:
            self.assertIn(option["risk"]["level"], ("low", "medium", "high"))
            self.assertIn("total", option["cost_breakdown"])

    def test_max_stops_zero_gives_flights_only(self):
        planner, _ = make_planner()
        result = planner.plan_trip("Milano", "Barcelona", [DAY], include_side_routes=False)
        self.assertEqual({o["kind"] for o in result["options"]}, {"direct"})
        self.assertIsNone(result["best_side_route"])

    def test_tiny_budget_is_reported(self):
        planner, _ = make_planner()
        result = planner.plan_trip("Milano", "Barcelona", [DAY], flight_search_budget=1)
        self.assertTrue(result["budget_exhausted"])
        self.assertLessEqual(result["flight_searches"], 1)

    def test_flex_and_roundtrip_run_on_the_real_engine(self):
        planner, _ = make_planner()
        result = planner.plan_trip("Milano", "Barcelona", [DAY], flex_days=1, return_dates=[RETURN_DAY],
                                   min_stay_hours=24.0)
        self.assertEqual(result["flex"]["requested_date"], DAY)
        self.assertEqual(result["query"]["flex_dates"], ["2030-10-27", DAY, "2030-10-29"])
        self.assertEqual(result["query"]["depart_dates"], [DAY])
        combos = result["roundtrip"]["combos"]
        self.assertTrue(combos)
        best_combo = combos[0]
        self.assertEqual(best_combo["total_try"],
                         round(best_combo["outbound"]["true_total_try"] + best_combo["return"]["true_total_try"], 2))
        self.assertGreaterEqual(best_combo["stay_hours"], 24.0)
        self.assertEqual(result["roundtrip"]["return"]["query"]["origin"], "Barcelona")

    def test_advice_from_an_empty_search_log_is_belirsiz(self):
        log = SearchLog(":memory:")
        self.addCleanup(log.close)
        planner, _ = make_planner(search_log=log)
        result = planner.plan_trip("Milano", "Barcelona", [DAY])
        self.assertEqual(result["advice"]["advice"], "belirsiz")

    def test_planner_is_deterministic(self):
        planner, _ = make_planner()
        again = planner.plan_trip("Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=30)
        self.assertEqual([o["true_total_try"] for o in again["options"]],
                         [o["true_total_try"] for o in self.result["options"]])


class ViaThroughPlannerTests(unittest.TestCase):
    def test_fixed_via_string_is_a_waypoint_for_the_engine(self):
        planner, flights = make_planner()
        # A waypoint that matches no airport: the engine returns no options, the planner stays healthy
        result = planner.plan_trip("Milano", "Barcelona", [DAY], via="Narnia")
        self.assertEqual(result["query"]["via_fixed"], ["Narnia"])
        self.assertIsInstance(result["options"], list)


class StationNamesThroughPlannerTests(unittest.TestCase):
    """Real station names survive ScheduledSegment -> RouteEngine -> TripPlanner."""

    def test_ground_station_names_reach_the_planner_options(self):
        named = replace(seg("Wien Hbf", "Praha hl.n.", "08:00", 255, 750),
                        origin_station_name="Wien Hbf", dest_station_name="Praha hl.n.")
        ground = FakeGround({("Viyana", "Prag"): [named]})
        engine = RouteEngine(FakeFlights({}), ground, airport_db=AirportDB.load_default(), costs=FakeCosts())
        planner = TripPlanner(FakeFlights({}), None, route_engine=engine)
        result = planner.plan_trip("Viyana", "Prag", [DAY], include_side_routes=True)
        train = next(leg for option in result["options"] for leg in option["legs"] if leg["type"] == "train")
        self.assertEqual(train["origin_station_name"], "Wien Hbf")
        self.assertEqual(train["dest_station_name"], "Praha hl.n.")


if __name__ == "__main__":
    unittest.main()
