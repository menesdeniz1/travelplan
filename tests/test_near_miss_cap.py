"""Tests for near miss chain cap and ground refactoring (T07)."""

from datetime import datetime
import unittest
from unittest.mock import patch

from travel_engine.airports import AirportDB
from travel_engine.route_engine import RouteEngine, _Run
from travel_engine.providers.rail_bus_provider import ScheduledSegment


class FakeFlights:
    def __init__(self, table=None):
        self.table = table or {}
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
        return []

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        return None


def make_flight(flight_no, origin, dest, dep_date, dep_time, arr_time, price):
    return {
        "flight_no": flight_no,
        "airline": flight_no[:2],
        "airline_name": "Test Air",
        "origin": origin,
        "destination": dest,
        "departure_date": dep_date,
        "departure_time": dep_time,
        "arrival_time": arr_time,
        "price_try": price,
        "total_price_try": price,
        "is_connected": False,
        "duration_mins": 120,
        "is_direct": True,
    }


class NearMissCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.airports = AirportDB.load_default()

    def test_near_miss_chain_cap_constant_defined(self):
        """NEAR_MISS_CHAIN_CAP route_engine içinde 300 olarak tanımlıdır."""
        import travel_engine.route_engine as re_mod
        self.assertEqual(getattr(re_mod, "NEAR_MISS_CHAIN_CAP", None), 300)

    def test_late_chains_do_not_exhaust_max_complete_chains(self):
        """(kırmızı) MAX_COMPLETE_CHAINS küçükken, yığında önce çok sayıda geç zincir varken kurala uyan seçenek yine options'ta."""
        # 5 geç uçuş (12:00 sonrasına varan, örn: 14:00, 15:00, 16:00, 17:00, 18:00)
        # 1 kurala uyan uçuş (10:00 varış)
        flights = [
            make_flight(f"LATE_{i}", "ATH", "PRG", "2026-10-28", f"{12+i:02d}:00", f"{14+i:02d}:00", 3000 + i*100)
            for i in range(5)
        ]
        compliant_flight = make_flight("COMPLIANT", "ATH", "PRG", "2026-10-28", "08:00", "10:00", 8000)
        # Yığında pop sırası ters olduğu için late uçuşlar önce işlensin diye compliant'ı listenin başına koyuyoruz
        table = {
            ("ATH", "PRG", "2026-10-28"): [compliant_flight] + flights
        }
        engine = RouteEngine(FakeFlights(table), FakeGround(), airport_db=self.airports)

        # MAX_COMPLETE_CHAINS=2 yamalandığında, geç zincirler complete listesine girmez;
        # compliant seçenek options'ta bulunur.
        with patch("travel_engine.route_engine.MAX_COMPLETE_CHAINS", 2):
            res = engine.search("ATH", "PRG", ["2026-10-28"], arrive_by="2026-10-28T12:00", max_stops=0)

        options = res.get("options") or []
        compliant_found = any(opt["legs"][0]["operator"] == "CO" for opt in options)
        self.assertTrue(compliant_found, "Kurala uyan seçenek MAX_COMPLETE_CHAINS geç zincirlerle dolduğunda da options'ta olmalı")

    def test_late_chains_capped_at_near_miss_chain_cap(self):
        """(kırmızı) late listesi en fazla NEAR_MISS_CHAIN_CAP öğe alır."""
        # 10 geç uçuş
        late_flights = [
            make_flight(f"LATE_{i}", "ATH", "PRG", "2026-10-28", f"{13+i:02d}:00", f"{15+i:02d}:00", 3000)
            for i in range(10)
        ]
        table = {
            ("ATH", "PRG", "2026-10-28"): late_flights
        }
        engine = RouteEngine(FakeFlights(table), FakeGround(), airport_db=self.airports)

        # NEAR_MISS_CHAIN_CAP = 3 yamalayalım
        with patch("travel_engine.route_engine.NEAR_MISS_CHAIN_CAP", 3):
            run = _Run(
                engine=engine,
                origin="ATH",
                destination="PRG",
                dates=["2026-10-28"],
                arrive_by=datetime(2026, 10, 28, 12, 0),
                depart_after=None,
                max_stops=0,
                via_tokens=None,
                budget=10,
                time_value=0.0,
                min_stay={},
                cabin_bag=False,
                checked_bag=False,
                costs=None,
                db=engine.airport_db,
                ground=engine.ground_provider,
            )
            run.setup_auto()
            run.bind_times()
            run.plan_auto()
            states = run.dfs("2026-10-28")

            # states içinde geç kalan zincirler en fazla 3 olmalı
            late_states = [s for s in states if s.legs and s.legs[-1].arr > run.arrive_by_utc]
            self.assertEqual(len(late_states), 3)


if __name__ == "__main__":
    unittest.main()
