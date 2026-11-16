"""Tests for via country ground exit (PLN-12, T03)."""

import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from travel_engine.airports import AirportDB, haversine_km
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import RouteEngine, VIA_ROAD_LIMIT_KM


def make_flight(airline, frm, to, dep_date, times, price, *, direct=True, layover=""):
    return {
        "source": "test",
        "airline": airline,
        "times": times,
        "duration": "2 sa.",
        "airports": f"{frm} - {to}",
        "stops": "Aktarmasız" if direct else "1 aktarma",
        "stops_count": 0 if direct else 1,
        "is_direct": direct,
        "layover_info": layover,
        "price": price,
        "price_currency": "TL",
        "price_try": float(price),
        "total_price_try": float(price),
        "booking_url": f"https://example.com/book/{airline}",
    }


def make_segment(from_place, to_place, dep_time, arr_time, price, mode="bus", operator="MetroTurizm", date="2026-10-30"):
    return ScheduledSegment(
        origin_station=from_place,
        dest_station=to_place,
        departure_time=dep_time,
        arrival_time=arr_time,
        duration_mins=480,
        price_try=float(price),
        operator=operator,
        mode=mode,
        date=date,
        is_estimated=False,
        price_is_estimated=False,
    )


class FakeFlights:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        return [dict(item) for item in self.table.get((origin, destination, departure_date), [])]


class FakeGround:
    def __init__(self, segments=None, distances=None):
        self.segments = segments or {}
        self.distances = distances or {}
        self.calls = []

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=700.0):
        self.calls.append((from_place, to_place, date_str, earliest_dep_time))
        out = []
        items = self.segments.get((from_place, to_place, date_str))
        if items is None:
            items = self.segments.get((from_place, to_place), [])
        for item in items:
            if earliest_dep_time and item.departure_time < earliest_dep_time:
                continue
            out.append(replace(item, date=date_str))
        return out[:max_results]

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        if (from_place, to_place) in self.distances:
            return self.distances[(from_place, to_place)]
        if from_coord and to_coord:
            return haversine_km(from_coord[0], from_coord[1], to_coord[0], to_coord[1]) * 1.2
        return None


class ViaCountryGroundTests(unittest.TestCase):
    def setUp(self):
        self.airports = AirportDB.load_default()

    def _engine(self, flights_table, ground_segs=None, distances=None):
        flights = FakeFlights(flights_table)
        ground = FakeGround(ground_segs, distances)
        engine = RouteEngine(flights, ground, airport_db=self.airports)
        return engine, flights, ground

    def test_country_ground_option_built_with_flight_transfer_bus(self):
        """(kırmızı) VIE → SKG uçuşu + Selanik → İstanbul otobüsü seçeneği kuruluyor; bacak sırası uçuş, transfer, otobüs."""
        table = {
            ("VIE", "SKG", "2026-10-30"): [
                make_flight("Ryanair", "VIE", "SKG", "2026-10-30", "08:00 - 10:45", 2000),
            ],
        }
        segs = {
            ("Thessaloniki", "İstanbul", "2026-10-30"): [
                make_segment("Thessaloniki", "İstanbul", "14:00", "22:00", 3500, mode="bus", operator="MetroTurizm"),
            ],
            ("Thessaloniki", "Istanbul", "2026-10-30"): [
                make_segment("Thessaloniki", "Istanbul", "14:00", "22:00", 3500, mode="bus", operator="MetroTurizm"),
            ],
        }
        distances = {
            ("Thessaloniki", "İstanbul"): 550.0,
            ("Thessaloniki", "Istanbul"): 550.0,
        }
        engine, _, _ = self._engine(table, segs, distances)
        res = engine.search("VIE", "İstanbul", ["2026-10-30"], via_fixed=["Yunanistan"])

        options = res["options"]
        self.assertEqual(len(options), 1, "Expected 1 option with flight + transfer + bus")
        legs = options[0]["legs"]
        self.assertEqual(len(legs), 3, "Expected 3 legs: flight, transfer, bus")
        self.assertEqual([leg["type"] for leg in legs], ["flight", "transfer", "bus"])
        self.assertEqual(legs[0]["operator"], "Ryanair")
        self.assertEqual(legs[0]["from"], "VIE")
        self.assertEqual(legs[0]["to"], "SKG")
        self.assertEqual(legs[1]["type"], "transfer")
        self.assertEqual(legs[1]["from"], "SKG")
        self.assertEqual(legs[1]["to"], "Thessaloniki")
        self.assertEqual(legs[2]["type"], "bus")
        self.assertEqual(legs[2]["operator"], "MetroTurizm")
        self.assertEqual(legs[2]["from"], "Thessaloniki")
        self.assertEqual(legs[2]["to"], "İstanbul")

    def test_bus_cheaper_than_flight_ranked_ahead(self):
        """(kırmızı) Otobüs uçuştan ucuzsa sıralamada önde."""
        table = {
            ("VIE", "SKG", "2026-10-30"): [
                make_flight("Ryanair", "VIE", "SKG", "2026-10-30", "08:00 - 10:45", 2000),
            ],
            ("SKG", "IST", "2026-10-30"): [
                make_flight("Olympic", "SKG", "IST", "2026-10-30", "15:00 - 16:30", 17000),
            ],
        }
        segs = {
            ("Thessaloniki", "İstanbul", "2026-10-30"): [
                make_segment("Thessaloniki", "İstanbul", "14:00", "22:00", 3500, mode="bus", operator="MetroTurizm"),
            ],
        }
        distances = {
            ("Thessaloniki", "İstanbul"): 550.0,
        }
        engine, _, _ = self._engine(table, segs, distances)
        res = engine.search("VIE", "İstanbul", ["2026-10-30"], via_fixed=["Yunanistan"])

        options = res["options"]
        self.assertEqual(len(options), 2, "Expected 2 options: bus option and flight option")
        # First option should be the cheaper bus option
        self.assertTrue(any(leg["type"] == "bus" for leg in options[0]["legs"]))
        # Second option should be the flight option
        self.assertTrue(any(leg["type"] == "flight" and leg["to"] == "IST" for leg in options[1]["legs"]))
        self.assertFalse(any(leg["type"] == "bus" for leg in options[1]["legs"]))
        self.assertLess(options[0]["total_try"], options[1]["total_try"])

    def test_distance_exceeding_limit_skips_ground(self):
        """(kırmızı) Sonraki durağa uzaklık sınırı aşınca kara bacağı denenmiyor (kara sağlayıcısına çağrı yok)."""
        table = {
            ("VIE", "ATH", "2026-10-30"): [
                make_flight("Ryanair", "VIE", "ATH", "2026-10-30", "08:00 - 11:30", 2000),
            ],
        }
        # Spata-Artemida (ATH) to Istanbul distance > VIA_ROAD_LIMIT_KM (800 km)
        distances = {
            ("Spata-Artemida", "İstanbul"): 950.0,
            ("Athens", "İstanbul"): 950.0,
        }
        segs = {
            ("Spata-Artemida", "İstanbul"): [
                make_segment("Spata-Artemida", "İstanbul", "14:00", "22:00", 3500, mode="bus"),
            ],
        }
        engine, _, ground = self._engine(table, segs, distances)
        res = engine.search("VIE", "İstanbul", ["2026-10-30"], via_fixed=["Yunanistan"])

        country_calls = [c for c in ground.calls if c[0] in ("Spata-Artemida", "Athens") and c[1] in ("İstanbul", "Istanbul")]
        self.assertEqual(country_calls, [], "Ülke kara bacağı 800 km sınırını aşınca denenmemeli")
        self.assertEqual(len(res["options"]), 0)

    def test_protection_city_to_city_via_unchanged(self):
        """(koruma) Şehir → şehir zorunlu duraklı aramalarda sonuç ve çağrılar bugünküyle aynı."""
        table = {
            ("PRG", "ATH", "2026-10-30"): [
                make_flight("A3", "PRG", "ATH", "2026-10-30", "08:00 - 11:30", 3000),
            ],
            ("ATH", "IST", "2026-10-30"): [
                make_flight("TK", "ATH", "IST", "2026-10-30", "18:05 - 19:30", 2000),
            ],
        }
        engine1, flights1, ground1 = self._engine(table)
        res1 = engine1.search("PRG", "IST", ["2026-10-30"], via_fixed=["ATH"])

        self.assertEqual(len(res1["options"]), 1)
        self.assertEqual(res1["options"][0]["legs"][0]["operator"], "A3")
        self.assertEqual(res1["options"][0]["legs"][-1]["operator"], "TK")

    def test_min_stay_and_via_on_date_applied_to_country_ground(self):
        """Ara durak kuralları (min_stay ve via_on_date) kara bacağı için de geçerlidir."""
        table = {
            ("VIE", "SKG", "2026-10-30"): [
                make_flight("Ryanair", "VIE", "SKG", "2026-10-30", "08:00 - 10:45", 2000),
            ],
        }
        # Bus leaving at 11:30 is only 45 mins after landing + transfer (~11:45 arrival in city) -> violated min_stay of 180 mins
        # Bus leaving at 16:00 satisfies min_stay of 180 mins
        segs = {
            ("Thessaloniki", "İstanbul", "2026-10-30"): [
                make_segment("Thessaloniki", "İstanbul", "11:30", "19:30", 3500, operator="EarlyBus"),
                make_segment("Thessaloniki", "İstanbul", "16:00", "23:59", 3500, operator="CompliantBus"),
            ],
        }
        distances = {
            ("Thessaloniki", "İstanbul"): 550.0,
        }
        engine, _, _ = self._engine(table, segs, distances)
        res = engine.search("VIE", "İstanbul", ["2026-10-30"], via_fixed=["Yunanistan"], min_stay_mins={"Yunanistan": 180})

        self.assertEqual(len(res["options"]), 1)
        self.assertEqual(res["options"][0]["legs"][2]["operator"], "CompliantBus")


if __name__ == "__main__":
    unittest.main()
