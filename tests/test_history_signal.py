"""Tests for search history signal in airport candidate ranking (A14)."""

import os
import tempfile
import unittest

from travel_engine.airports import AirportDB, haversine_km
from travel_engine.route_engine import RouteEngine
from travel_engine.search_log import SearchLog


class DummyFlights:
    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        return []


class DummyGround:
    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        if from_coord and to_coord:
            return haversine_km(from_coord[0], from_coord[1], to_coord[0], to_coord[1]) * 1.25
        return None

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=12, max_road_km=700.0):
        return []


class HistorySignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = SearchLog(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_search_log_codes_with_history_by_role(self):
        # record("KTW", "WRO", ...) fiyatlı kayıt
        self.log.record("KTW", "WRO", "2026-10-25", "ok", 1, 1500.0)

        # destination rolü WRO'yu bulur, POZ geçmişte yok
        dest_history = self.log.codes_with_history(["WRO", "POZ"], "destination")
        self.assertEqual(dest_history, {"WRO"})

        # origin rolü WRO veya POZ için boştur (KTW origin olarak kayıtlı)
        orig_history = self.log.codes_with_history(["WRO", "POZ"], "origin")
        self.assertEqual(orig_history, set())

        # KTW origin olarak kayıtlıdır
        ktw_orig = self.log.codes_with_history(["KTW", "WRO"], "origin")
        self.assertEqual(ktw_orig, {"KTW"})

    def test_engine_destination_side_includes_non_lcc_airport_with_history(self):
        # Varış tarafında LCC listesinde olmayan (ör. DRS) ve yalnız geçmişi olan bir
        # havalimanı aday listesine girer (result["candidates"]["dest_side"]), geçmiş yokken girmez.
        db = AirportDB.load_default()

        # 1. Geçmiş yokken: DRS dest_side adaylarında yer almaz
        engine_no_hist = RouteEngine(DummyFlights(), DummyGround(), airport_db=db, search_log=self.log)
        res_no_hist = engine_no_hist.search("Istanbul", "Prag", ["2026-10-25"], flight_search_budget=0)
        self.assertNotIn("DRS", res_no_hist["candidates"]["dest_side"])

        # 2. Geçmiş eklenir: DRS varışlı fiyatlı arama kaydı
        self.log.record("IST", "DRS", "2026-10-25", "ok", 2, 1200.0)

        # 3. Geçmiş varken: DRS dest_side adaylarına girer
        engine_with_hist = RouteEngine(DummyFlights(), DummyGround(), airport_db=db, search_log=self.log)
        res_with_hist = engine_with_hist.search("Istanbul", "Prag", ["2026-10-25"], flight_search_budget=0)
        self.assertIn("DRS", res_with_hist["candidates"]["dest_side"])


if __name__ == "__main__":
    unittest.main()
