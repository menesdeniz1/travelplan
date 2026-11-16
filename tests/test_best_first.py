"""Tests for best-first chain search order in RouteEngine (T11)."""

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

from tests.test_route_engine import FakeCosts
from travel_engine.airports import AirportDB
from travel_engine.route_engine import RouteEngine

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


class TestBestFirst(unittest.TestCase):
    def test_best_first_beats_stack_order_when_capped(self):
        """When MAX_COMPLETE_CHAINS is capped, best-first ensures the cheapest chain

        is found and is #1, whereas stack LIFO would pop the expensive branches first
        and terminate before ever reaching the cheap chain.
        """
        table = {
            ("ATH", "PRG", DAY): [
                flight("CheapAir", "08:00 – 10:00", 1000, "ATH-PRG"),
                flight("ExpAir1", "09:00 – 11:00", 5000, "ATH-PRG"),
                flight("ExpAir2", "10:00 – 12:00", 6000, "ATH-PRG"),
                flight("ExpAir3", "11:00 – 13:00", 7000, "ATH-PRG"),
                flight("ExpAir4", "12:00 – 14:00", 8000, "ATH-PRG"),
            ]
        }
        engine, _ = make_engine(table)
        with patch("travel_engine.route_engine.MAX_COMPLETE_CHAINS", 2):
            res = engine.search("ATH", "PRG", [DAY], max_results=5)

        self.assertGreater(len(res["options"]), 0)
        best = res["best"]
        self.assertIsNotNone(best)
        # With stack order, CheapAir is at the bottom of the stack and never explored.
        # With best-first, CheapAir has the lowest key and is explored first.
        self.assertEqual(best["legs"][0]["operator"], "CheapAir")
        self.assertEqual(best["legs"][0]["price_try"], 1000.0)

    def test_pythonhashseed_determinism(self):
        """Search results must be identical under PYTHONHASHSEED=1 and PYTHONHASHSEED=2."""
        code = """
import json
from travel_engine.airports import AirportDB
from travel_engine.route_engine import RouteEngine
from tests.test_route_engine import FakeCosts
from tests.test_best_first import flight, FakeFlights, DAY

table = {
    ("ATH", "PRG", DAY): [
        flight("A1", "08:00 – 10:00", 3000, "ATH-PRG"),
        flight("A2", "12:00 – 14:00", 2500, "ATH-PRG"),
        flight("A3", "16:00 – 18:00", 4000, "ATH-PRG"),
    ]
}
engine = RouteEngine(FakeFlights(table), None, airport_db=AirportDB.load_default(), costs=FakeCosts())
res = engine.search("ATH", "PRG", [DAY])
options_summary = [
    (o["legs"][0]["operator"], o["legs"][0]["price_try"], o["score"])
    for o in res["options"]
]
print(json.dumps(options_summary))
"""
        outputs = []
        for seed in ["1", "2"]:
            env = os.environ.copy()
            env["PYTHONHASHSEED"] = seed
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                env=env,
                cwd=os.path.abspath("."),
            )
            self.assertEqual(proc.returncode, 0, f"Error with seed {seed}: {proc.stderr}")
            outputs.append(proc.stdout.strip())

        self.assertEqual(outputs[0], outputs[1])

    def test_small_search_options_unchanged(self):
        """Uncapped small searches produce the expected options in score order."""
        table = {
            ("ATH", "PRG", DAY): [
                flight("AirA", "10:00 – 12:00", 3000, "ATH-PRG"),
                flight("AirB", "14:00 – 16:00", 2000, "ATH-PRG"),
            ]
        }
        engine, _ = make_engine(table)
        res = engine.search("ATH", "PRG", [DAY])
        self.assertEqual(len(res["options"]), 2)
        self.assertEqual(res["best"]["legs"][0]["operator"], "AirB")
        self.assertEqual(res["best"]["legs"][0]["price_try"], 2000.0)


if __name__ == "__main__":
    unittest.main()
