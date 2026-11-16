"""Tests for city destination and origin searches (AIR-4 / T14).

Verifies that RouteEngine searches targeting multi-airport cities (e.g. İstanbul with
IST and SAW) return all matching direct flights when queried by city name.
"""

import unittest
from travel_engine.airports import AirportDB, haversine_km
from travel_engine.route_engine import RouteEngine


class FakeFlightProvider:
    def __init__(self, flights):
        self.flights = flights
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        res = []
        for f in self.flights:
            if departure_date == "2026-10-31":
                res.append(dict(f))
        return res


class FakeGroundProvider:
    def __init__(self):
        self.calls = []

    def find_ground(self, *args, **kwargs):
        return []

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        if from_coord and to_coord:
            return haversine_km(from_coord[0], from_coord[1], to_coord[0], to_coord[1]) * 1.25
        return None


class CityDestinationTests(unittest.TestCase):
    def setUp(self):
        self.f_ist = {
            "source": "test",
            "airline": "A3",
            "times": "07:00 – 09:30",
            "duration": "1 sa. 30 dk.",
            "airports": "ATH – IST",
            "stops": "Aktarmasız",
            "stops_count": 0,
            "is_direct": True,
            "price": 2000,
            "total_price_try": 2000.0,
            "currency": "TRY",
            "trip_type": "Tek yön",
        }
        self.f_saw = {
            "source": "test",
            "airline": "PC",
            "times": "09:30 – 11:55",
            "duration": "1 sa. 25 dk.",
            "airports": "ATH – SAW",
            "stops": "Aktarmasız",
            "stops_count": 0,
            "is_direct": True,
            "price": 1800,
            "total_price_try": 1800.0,
            "currency": "TRY",
            "trip_type": "Tek yön",
        }
        self.fp = FakeFlightProvider([self.f_ist, self.f_saw])
        self.gp = FakeGroundProvider()
        self.engine = RouteEngine(self.fp, self.gp)

    def test_city_destination_ath_to_istanbul(self):
        """ATH -> İstanbul city search returns both IST and SAW direct flight options."""
        res = self.engine.search(
            "ATH",
            "İstanbul",
            ["2026-10-30", "2026-10-31"],
            depart_after="2026-10-30T20:00",
            arrive_by="2026-10-31T23:59",
            max_stops=0,
        )
        options = res.get("options", [])
        self.assertEqual(len(options), 2, f"Expected 2 options, got {len(options)}; warnings={res.get('warnings')}")

        dest_airports = {
            leg["to"]
            for opt in options
            for leg in opt.get("legs", [])
            if leg.get("type") != "transfer"
        }
        self.assertEqual(dest_airports, {"IST", "SAW"})

    def test_city_origin_and_destination_atina_to_istanbul_english(self):
        """Atina -> Istanbul search returns both IST and SAW direct flight options."""
        res = self.engine.search(
            "Atina",
            "Istanbul",
            ["2026-10-30", "2026-10-31"],
            depart_after="2026-10-30T20:00",
            arrive_by="2026-10-31T23:59",
            max_stops=0,
        )
        options = res.get("options", [])
        self.assertEqual(len(options), 2, f"Expected 2 options, got {len(options)}; warnings={res.get('warnings')}")

        dest_airports = {
            leg["to"]
            for opt in options
            for leg in opt.get("legs", [])
            if leg.get("type") != "transfer"
        }
        self.assertEqual(dest_airports, {"IST", "SAW"})

    def test_city_destination_ath_to_istanbul_english(self):
        """ATH -> Istanbul search returns both IST and SAW direct flight options."""
        res = self.engine.search(
            "ATH",
            "Istanbul",
            ["2026-10-30", "2026-10-31"],
            depart_after="2026-10-30T20:00",
            arrive_by="2026-10-31T23:59",
            max_stops=0,
        )
        options = res.get("options", [])
        self.assertEqual(len(options), 2, f"Expected 2 options, got {len(options)}; warnings={res.get('warnings')}")

        dest_airports = {
            leg["to"]
            for opt in options
            for leg in opt.get("legs", [])
            if leg.get("type") != "transfer"
        }
        self.assertEqual(dest_airports, {"IST", "SAW"})


if __name__ == "__main__":
    unittest.main()
