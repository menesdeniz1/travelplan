"""Tests for last service and next departure detection in RouteEngine (A12)."""

import unittest
from dataclasses import replace
from unittest import mock

from travel_engine.airports import AirportDB
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import RouteEngine

DAY = "2026-10-25"


def flight(airline, times, price, route="ATH ➔ PRG"):
    dep, arr = [t.strip() for t in times.split("–")]
    return {
        "airline": airline,
        "times": times,
        "departure_time": dep,
        "arrival_time": arr,
        "duration": "2h",
        "stops": "Aktarmasız",
        "route": route,
        "is_direct": True,
        "layover_info": None,
        "total_price_try": float(price),
        "currency": "TRY",
        "trip_type": "Tek yön",
        "badge": None,
        "google_flights_url": "https://www.google.com/travel/flights?q=test",
    }


def seg(origin_station, dest_station, dep, mins=120, price=500.0, operator="RegioJet", mode="train"):
    return ScheduledSegment(
        origin_station=origin_station,
        dest_station=dest_station,
        departure_time=dep,
        arrival_time="",
        duration_mins=mins,
        price_try=float(price),
        operator=operator,
        mode=mode,
        date=DAY,
        is_estimated=False,
        price_eur=None,
        price_is_estimated=False,
        source="table",
    )


class FakeFlights:
    def __init__(self, table=None):
        self.table = table or {}

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        return [dict(item) for item in self.table.get((origin, destination, departure_date), [])]


class FakeGround:
    def __init__(self, segments=None):
        self.segments = segments or {}

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        return 300.0

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=12, max_road_km=700.0):
        out = []
        for item in self.segments.get((from_place, to_place), []):
            if earliest_dep_time and item.departure_time < earliest_dep_time:
                continue
            out.append(replace(item, date=date_str))
        return out[:max_results]


class LastServiceTests(unittest.TestCase):
    def setUp(self):
        self.fx_patcher = mock.patch("travel_engine.fx.get_eur_try_rate", return_value=(50.0, "test", False))
        self.fx_patcher.start()

    def tearDown(self):
        self.fx_patcher.stop()

    def test_three_services_last_is_marked_and_earlier_has_next_dep(self):
        # Kara sağlayıcısı aynı gün 18:00, 20:00, 22:00 seferleri veriyor (3 < 12).
        # 22:00'yi kullanan seçeneğin tren bacağında is_last_service True ve next_departure_at None;
        # 20:00'yi kullananın bacağında False ve next_departure_at "…T22:00".
        segments = {
            ("Viyana", "Prag"): [
                seg("Wien Hbf", "Praha hl.n.", "18:00", price=500.0),
                seg("Wien Hbf", "Praha hl.n.", "20:00", price=600.0),
                seg("Wien Hbf", "Praha hl.n.", "22:00", price=700.0),
            ]
        }
        engine = RouteEngine(FakeFlights({}), FakeGround(segments), airport_db=AirportDB.load_default())
        result = engine.search("Viyana", "Prag", [DAY], max_stops=0)
        options = result["options"]
        self.assertGreaterEqual(len(options), 3)

        opt_22 = next(o for o in options if o["legs"][0]["depart_at"].endswith("22:00"))
        leg_22 = opt_22["legs"][0]
        self.assertTrue(leg_22["is_last_service"])
        self.assertIsNone(leg_22["next_departure_at"])

        opt_20 = next(o for o in options if o["legs"][0]["depart_at"].endswith("20:00"))
        leg_20 = opt_20["legs"][0]
        self.assertFalse(leg_20["is_last_service"])
        self.assertIsNotNone(leg_20["next_departure_at"])
        self.assertTrue(leg_20["next_departure_at"].endswith("T22:00"))

        opt_18 = next(o for o in options if o["legs"][0]["depart_at"].endswith("18:00"))
        leg_18 = opt_18["legs"][0]
        self.assertFalse(leg_18["is_last_service"])
        self.assertIsNotNone(leg_18["next_departure_at"])
        self.assertTrue(leg_18["next_departure_at"].endswith("T20:00"))

    def test_flight_connected_to_last_service_has_reason(self):
        # Uçuştan sonra son sefere bağlanan seçeneğin risk["reasons"] listesinde
        # son seferle ilgili gerekçe var (costs.connection_risk'in eklediği metin).
        flight_table = {
            ("BGY", "GRO", DAY): [flight("Ryanair", "07:00 – 08:20", 1200, "BGY ➔ GRO")],
        }
        segments = {
            ("Girona", "Barcelona"): [
                seg("Girona", "Barcelona-Sants", "10:00", price=300.0),
            ]
        }
        engine = RouteEngine(FakeFlights(flight_table), FakeGround(segments), airport_db=AirportDB.load_default())
        result = engine.search("Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=10)
        options = [o for o in result["options"] if any(l["type"] == "flight" and l["from"] == "BGY" for l in o["legs"])]
        self.assertTrue(options)
        opt = options[0]
        train_leg = next(l for l in opt["legs"] if l["type"] == "train")
        self.assertTrue(train_leg["is_last_service"])

        reasons = opt["risk"]["reasons"]
        self.assertTrue(any("günün son seferi" in r for r in reasons), f"Reasons did not mention last service: {reasons}")

    def test_twelve_services_list_truncated_so_last_is_not_marked(self):
        # (koruma) Sağlayıcı 12 sefer verdiğinde (liste kesik) son segment is_last_service False.
        segs_12 = [
            seg("Wien Hbf", "Praha hl.n.", f"{h:02d}:00", price=500.0 + h * 10)
            for h in range(8, 20)  # 8:00, 9:00, ..., 19:00 (12 items)
        ]
        self.assertEqual(len(segs_12), 12)
        segments = {("Viyana", "Prag"): segs_12}
        engine = RouteEngine(FakeFlights({}), FakeGround(segments), airport_db=AirportDB.load_default())
        result = engine.search("Viyana", "Prag", [DAY], max_stops=0, max_results=20)
        options = result["options"]

        opt_19 = next(o for o in options if o["legs"][0]["depart_at"].endswith("19:00"))
        leg_19 = opt_19["legs"][0]
        self.assertFalse(leg_19["is_last_service"])
        self.assertIsNone(leg_19["next_departure_at"])


if __name__ == "__main__":
    unittest.main()
