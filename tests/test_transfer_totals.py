"""DOG-1: an option's total counts the transfer legs it shows, and no transfer it does not show (no network)."""
import unittest
from unittest import mock

from travel_engine.airports import AirportDB, haversine_km
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import RouteEngine

DAY = "2026-10-28"


class _Flights:
    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        if (origin, destination, departure_date) == ("ATH", "BTS", DAY):
            return [{"airline": "Wizz Air", "times": "23:50 – 01:05+1", "duration": "2 sa. 15 dk.",
                     "airports": "ATH ➔ BTS", "stops": "Aktarmasız", "stops_count": 0, "is_direct": True,
                     "layover_info": "", "total_price_try": 4447.0, "google_flights_url": ""}]
        return []


class _Ground:
    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        if from_coord and to_coord:
            return haversine_km(from_coord[0], from_coord[1], to_coord[0], to_coord[1]) * 1.25
        return None

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None, to_coord=None,
                    max_results=6, max_road_km=700.0, **_):
        if from_place == "Bratislava" and to_place == "Prag" and date_str == "2026-10-29":
            seg = ScheduledSegment("Bratislava", "Prag", "06:00", "10:20", 260, 900.0, "RegioJet", "train",
                                   date_str, is_estimated=False, price_is_estimated=False, source="live")
            if earliest_dep_time and seg.departure_time < earliest_dep_time:
                return []
            return [seg]
        return []


class NightLandingTotalTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("travel_engine.fx.get_eur_try_rate", return_value=(50.0, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _flight_and_train(self):
        engine = RouteEngine(_Flights(), _Ground(), airport_db=AirportDB.load_default(), max_parallel_searches=1)
        result = engine.search("ATH", "Prag", [DAY], max_stops=1, flight_search_budget=10)
        for option in result["options"]:
            if any(leg["type"] == "train" for leg in option["legs"]):
                return option
        self.fail(f"no flight + train option: {result['warnings']}")

    def test_transfers_equal_the_shown_transfer_legs(self):
        option = self._flight_and_train()
        shown = [leg for leg in option["legs"] if leg["type"] == "transfer"]
        # Only BTS -> Bratislava after the 01:05 landing; "ATH" was typed as an airport, so no transfer before it
        self.assertEqual(len(shown), 1)
        self.assertEqual(option["cost_breakdown"]["transfers"], round(sum(leg["price_try"] for leg in shown), 2))

    def test_total_is_the_sum_of_its_parts(self):  # guard: true before the change too
        option = self._flight_and_train()
        b = option["cost_breakdown"]
        self.assertEqual(option["true_total_try"],
                         round(b["tickets"] + b["baggage"] + b["transfers"] + b["hotel"], 2))


if __name__ == "__main__":
    unittest.main()
