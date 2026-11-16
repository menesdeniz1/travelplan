import tempfile
import unittest
import os
from travel_engine.price_history import PriceHistoryManager


class TestPriceHistory(unittest.TestCase):
    def setUp(self):
        # Temp dir, not the working directory: the SQLite database lives next to this path.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.test_storage = os.path.join(tmp.name, "price_history.json")
        self.mgr = PriceHistoryManager(storage_path=self.test_storage)

    def test_record_and_alias_lookup(self):
        # Record with Turkish city names
        self.mgr.record_price("Atina", "Prag", "2026-10-27", "oneway", 10984.0, airline="SKY express")
        self.mgr.record_price("Atina", "Prag", "2026-10-28", "oneway", 15687.0, airline="KLM")

        # Query with IATA codes
        rec27 = self.mgr.find_recorded_price("ATH", "PRG", "2026-10-27", "oneway")
        self.assertIsNotNone(rec27)
        self.assertEqual(rec27[0], 10984.0)
        self.assertEqual(rec27[1], "SKY express")

        rec28 = self.mgr.find_recorded_price("Athens", "Prague", "2026-10-28", "oneway")
        self.assertIsNotNone(rec28)
        self.assertEqual(rec28[0], 15687.0)
        self.assertEqual(rec28[1], "KLM")

    def test_date_matrix_uses_persisted_live_prices(self):
        # Record 27th and 28th
        self.mgr.record_price("Atina", "Prag", "2026-10-27", "oneway", 10984.0, airline="SKY express")
        self.mgr.record_price("Atina", "Prag", "2026-10-28", "oneway", 15687.0, airline="KLM")

        # View matrix for 28th (selected price = 15687)
        matrix_28 = self.mgr.get_date_matrix("ATH", "PRG", "2026-10-28", "oneway", 15687.0, current_airline="KLM")
        day_27 = next(d for d in matrix_28["days"] if d["date"] == "2026-10-27")
        day_28 = next(d for d in matrix_28["days"] if d["date"] == "2026-10-28")
        day_29 = next(d for d in matrix_28["days"] if d["date"] == "2026-10-29")

        # 27th must be the REAL live price (10984.0), NOT an estimate
        self.assertEqual(day_27["price"], 10984.0)
        self.assertTrue(day_27["is_live"])
        self.assertTrue(day_27["is_cheapest"])
        self.assertEqual(day_27["airline"], "SKY express")

        # 28th is selected and live
        self.assertEqual(day_28["price"], 15687.0)
        self.assertTrue(day_28["is_live"])

        # 29th was never searched, so it is an estimate
        self.assertFalse(day_29["is_live"])

        # When viewing matrix for 27th (selected price = 10984)
        matrix_27 = self.mgr.get_date_matrix("Atina", "Prag", "2026-10-27", "oneway", 10984.0, current_airline="SKY express")
        day_28_from_27 = next(d for d in matrix_27["days"] if d["date"] == "2026-10-28")
        # 28th must show the real 15687.0 TL, NOT ~11101.0 TL
        self.assertEqual(day_28_from_27["price"], 15687.0)
        self.assertTrue(day_28_from_27["is_live"])

    def test_calendar_lookup_in_date_matrix(self):
        cal_prices = {
            "2026-10-25": 14400.0,
            "2026-10-26": 9430.0,
            "2026-10-27": 11000.0,
            "2026-10-28": 15200.0,
            "2026-10-29": 13100.0,
            "2026-10-30": 10300.0,
            "2026-10-31": 26500.0,
        }
        res = self.mgr.get_date_matrix(
            "ATH", "PRG", "2026-10-28", "oneway",
            current_min_price=15200.0,
            calendar_lookup=cal_prices,
            current_airline="Aegean"
        )
        # Check all calendar prices are reflected as real live prices
        day_26 = next(d for d in res["days"] if d["date"] == "2026-10-26")
        self.assertEqual(day_26["price"], 9430.0)
        self.assertTrue(day_26["has_price"])
        self.assertTrue(day_26["is_live"])
        self.assertTrue(day_26["is_cheapest"])

        # Check maximum saving calculation: 15200 - 9430 = 5770
        self.assertEqual(res["max_saving_try"], 5770.0)
        self.assertEqual(res["best_date"], "2026-10-26")


if __name__ == "__main__":
    unittest.main()
