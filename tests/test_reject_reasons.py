"""Tests for flight rejection reasons in RouteEngine (T10).

Verifies the 5 rejection reasons:
- depart_after (kalkış saati)
- arrive_by (son varış)
- buffer (aktarma süresi)
- not_target (farklı havalimanı)
- date (tarih)
and protection test ensuring normal option-producing searches are unaffected.
"""

import unittest
from datetime import datetime

from tests.test_route_engine import FakeCosts
from travel_engine.airports import AirportDB
from travel_engine.route_engine import RouteEngine, SearchTrace, _Flight, _Run


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


class FakeFlights:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        return [dict(item) for item in self.table.get((origin, destination, departure_date), [])]


def make_engine(table):
    flights = FakeFlights(table)
    engine = RouteEngine(flights, None, airport_db=AirportDB.load_default(), costs=FakeCosts())
    return engine, flights


class TestRejectReasons(unittest.TestCase):
    def test_reject_depart_after(self):
        table = {
            ("ATH", "PRG", DAY): [
                flight("A3", "08:00 – 10:00", 3000, "ATH-PRG"),
                flight("A3", "12:00 – 14:00", 3200, "ATH-PRG"),
            ]
        }
        engine, _ = make_engine(table)
        res = engine.search(
            "ATH", "PRG", [DAY],
            depart_after="2026-10-28T15:00",
        )
        self.assertEqual(len(res["options"]), 0)
        self.assertTrue(any("kalkış saati: 2" in w for w in res["warnings"]), f"Warnings: {res['warnings']}")
        self.assertEqual(res["trace"]["counters"].get("reject_depart_after"), 2)

    def test_reject_arrive_by(self):
        # arrive_by 10:00 -> arrival_limit_utc is 21:00 UTC (PRG is UTC+1)
        # Flights arriving at 23:00 and 23:55 are well past arrival_limit_utc
        table = {
            ("ATH", "PRG", DAY): [
                flight("A3", "20:00 – 23:00", 3000, "ATH-PRG"),
                flight("A3", "21:00 – 23:55", 3200, "ATH-PRG"),
            ]
        }
        engine, _ = make_engine(table)
        res = engine.search(
            "ATH", "PRG", [DAY],
            arrive_by="2026-10-28T10:00",
        )
        self.assertEqual(len(res["options"]), 0)
        self.assertTrue(any("son varış: 2" in w for w in res["warnings"]), f"Warnings: {res['warnings']}")
        self.assertEqual(res["trace"]["counters"].get("reject_arrive_by"), 2)

    def test_reject_buffer(self):
        # Via search: ATH -> VIE -> PRG
        # Leg 1 arrives at VIE at 12:00.
        # Leg 2 from VIE leaves at 12:30 and 13:00 (buffer is 120 mins, so earliest is 14:00).
        table = {
            ("ATH", "VIE", DAY): [
                flight("OS", "10:00 – 12:00", 2000, "ATH-VIE"),
            ],
            ("VIE", "PRG", DAY): [
                flight("OS", "12:30 – 13:30", 1500, "VIE-PRG"),
                flight("OS", "13:00 – 14:00", 1600, "VIE-PRG"),
            ],
        }
        engine, _ = make_engine(table)
        res = engine.search(
            "ATH", "PRG", [DAY],
            via_fixed=["VIE"],
        )
        self.assertEqual(len(res["options"]), 0)
        self.assertTrue(any("aktarma süresi: 2" in w for w in res["warnings"]), f"Warnings: {res['warnings']}")
        self.assertEqual(res["trace"]["counters"].get("reject_buffer"), 2)

    def test_reject_not_target(self):
        # Provider returns flights to WAW (Warsaw is neither PRG nor a hub)
        table = {
            ("ATH", "PRG", DAY): [
                flight("LO", "10:00 – 12:00", 2500, "ATH-WAW"),
                flight("LO", "14:00 – 16:00", 2700, "ATH-WAW"),
            ]
        }
        engine, _ = make_engine(table)
        res = engine.search("ATH", "PRG", [DAY])
        self.assertEqual(len(res["options"]), 0)
        self.assertTrue(any("farklı havalimanı: 2" in w for w in res["warnings"]), f"Warnings: {res['warnings']}")
        self.assertEqual(res["trace"]["counters"].get("reject_not_target"), 2)

    def test_reject_date(self):
        engine, _ = make_engine({})
        run = _Run(
            engine=engine, origin="ATH", destination="PRG", dates=[DAY],
            arrive_by=None, depart_after=None, max_stops=1,
            via_tokens=None, budget=10, time_value=0.0, min_stay={},
            cabin_bag=False, checked_bag=False, costs=FakeCosts(),
            db=engine.airport_db, ground=None, trace=SearchTrace(),
        )
        run.setup_auto()
        run.bind_times()
        f1 = _Flight(
            frm="ATH", to="PRG", dep=datetime(2026, 10, 30, 10, 0),
            arr=datetime(2026, 10, 30, 12, 0), price=1000.0,
            operator="A3", url="", layovers=(),
        )
        f2 = _Flight(
            frm="ATH", to="PRG", dep=datetime(2026, 10, 30, 14, 0),
            arr=datetime(2026, 10, 30, 16, 0), price=1200.0,
            operator="A3", url="", layovers=(),
        )
        run.flight_index[("2026-10-30", "ATH")] = [f1, f2]
        engine._build_options(run, True)
        reason = run.no_option_reason()
        self.assertIsNotNone(reason)
        self.assertIn("tarih: 2", reason)
        self.assertEqual(run.reject_counts.get("date"), 2)

    def test_protection_option_producing_search(self):
        table = {
            ("ATH", "PRG", DAY): [
                flight("A3", "10:00 – 12:00", 3000, "ATH-PRG"),
                flight("OK", "14:00 – 16:00", 2800, "ATH-PRG"),
            ]
        }
        engine, flights = make_engine(table)
        res = engine.search("ATH", "PRG", [DAY])
        self.assertEqual(len(res["options"]), 2)
        self.assertIsNotNone(res["best"])
        self.assertEqual(res["best"]["legs"][0]["operator"], "OK")
        self.assertEqual(len(flights.calls), 9)


if __name__ == "__main__":
    unittest.main()
