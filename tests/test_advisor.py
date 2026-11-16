import json
import math
import unittest
from datetime import datetime, timezone

from travel_engine.advisor import advice_for_route, buy_or_wait
from travel_engine.search_log import SearchLog

TODAY = "2026-10-19"
FAR_DEPARTURE = "2026-12-01"  # 43 days after TODAY
NEAR_DEPARTURE = "2026-10-25"  # 6 days after TODAY


def _old_points(prices, start_day=1, step=1):
    """Observations on distinct days in August 2026 (outside the 7-day trend window), oldest first."""
    return [(f"2026-08-{start_day + i * step:02d}T10:00:00+00:00", float(p)) for i, p in enumerate(prices)]


TEN_PRICES = [3000, 3200, 3400, 3600, 3800, 4000, 4200, 4400, 4600, 5000]


class BuyOrWaitTests(unittest.TestCase):
    def assertJsonSafe(self, result):
        json.dumps(result, allow_nan=False)
        for key, value in result["stats"].items():
            if isinstance(value, float):
                self.assertTrue(math.isfinite(value), key)

    def test_observations_span_days_in_stats(self):
        empty_res = buy_or_wait([], 3000.0, FAR_DEPARTURE, today=TODAY)
        self.assertIsNone(empty_res["stats"]["observations_span_days"])

        one_pt = [("2026-08-01T10:00:00", 3000.0)]
        one_res = buy_or_wait(one_pt, 3000.0, FAR_DEPARTURE, today=TODAY)
        self.assertEqual(one_res["stats"]["observations_span_days"], 0)

        five_pts = _old_points([3000, 3100, 3200, 3300, 3400], start_day=1, step=1)
        five_res = buy_or_wait(five_pts, 3000.0, FAR_DEPARTURE, today=TODAY)
        self.assertEqual(five_res["stats"]["observations_span_days"], 4)

        ten_res = buy_or_wait(_old_points(TEN_PRICES), 2800.0, FAR_DEPARTURE, today=TODAY)
        self.assertEqual(ten_res["stats"]["observations_span_days"], 9)
        self.assertJsonSafe(ten_res)

    def test_span_under_seven_days_caps_confidence_at_low(self):
        # 10 prices (which normally give "yüksek") but compressed into 3 days (span = 2 < 7)
        compressed_10 = [
            (f"2026-08-0{1 + (i % 3)}T10:00:00", float(p)) for i, p in enumerate(TEN_PRICES)
        ]
        res10 = buy_or_wait(compressed_10, 2800.0, FAR_DEPARTURE, today=TODAY)
        self.assertEqual(res10["advice"], "al")
        self.assertEqual(res10["stats"]["observations_span_days"], 2)
        self.assertEqual(res10["confidence"], "düşük")

        # 5 prices (normally "orta") spanning 4 days (span = 4 < 7)
        res5 = buy_or_wait(_old_points([3000, 3010, 4000, 5000, 6000], step=1), 3050.0, FAR_DEPARTURE, today=TODAY)
        self.assertEqual(res5["advice"], "al")
        self.assertEqual(res5["stats"]["observations_span_days"], 4)
        self.assertEqual(res5["confidence"], "düşük")

    def test_insufficient_data_is_belirsiz(self):
        points = [("2026-10-10T10:00:00", 3000.0), ("2026-10-11T10:00:00", 3100.0)]
        result = buy_or_wait(points, 3000.0, FAR_DEPARTURE, today=TODAY)
        self.assertEqual(result["advice"], "belirsiz")
        self.assertEqual(result["confidence"], "düşük")
        self.assertEqual(result["stats"]["n"], 2)
        self.assertTrue(any("Yetersiz" in reason for reason in result["reasons"]))
        self.assertJsonSafe(result)

    def test_no_points_is_belirsiz_with_empty_stats(self):
        result = buy_or_wait([], 3000.0, FAR_DEPARTURE, today=TODAY)
        self.assertEqual(result["advice"], "belirsiz")
        self.assertEqual(result["stats"]["n"], 0)
        self.assertIsNone(result["stats"]["min"])
        self.assertIsNone(result["stats"]["current_percentile"])
        self.assertEqual(result["stats"]["days_to_departure"], 43)
        self.assertJsonSafe(result)

    def test_low_percentile_is_al_with_high_confidence(self):
        result = buy_or_wait(_old_points(TEN_PRICES), 2800.0, FAR_DEPARTURE, today=TODAY)
        self.assertEqual(result["advice"], "al")
        self.assertEqual(result["confidence"], "yüksek")
        self.assertEqual(result["stats"]["current_percentile"], 0.0)
        self.assertEqual(result["stats"]["min"], 3000.0)
        self.assertEqual(result["stats"]["max"], 5000.0)
        self.assertEqual(result["stats"]["median"], 3900.0)
        self.assertTrue(any("2.800 TL" in reason for reason in result["reasons"]))
        self.assertJsonSafe(result)

    def test_near_minimum_within_three_percent_is_al(self):
        # p10 = 3200 here, so only the min * 1.03 rule (3090) applies to 3050; step=2 gives span=8 >= 7
        result = buy_or_wait(_old_points([3000, 3010, 4000, 5000, 6000], step=2), 3050.0, FAR_DEPARTURE, today=TODAY)
        self.assertEqual(result["advice"], "al")
        self.assertEqual(result["confidence"], "orta")
        self.assertTrue(any("%3" in reason for reason in result["reasons"]))

    def test_near_departure_leans_al_with_low_confidence(self):
        result = buy_or_wait(_old_points([4000, 4500, 5000, 5500]), 5200.0, NEAR_DEPARTURE, today=TODAY)
        self.assertEqual(result["advice"], "al")
        self.assertEqual(result["confidence"], "düşük")
        self.assertEqual(result["stats"]["days_to_departure"], 6)
        self.assertTrue(any("genel bir eğilim" in reason for reason in result["reasons"]))

    def test_honesty_disclaimer_in_reasons(self):
        # 1. Near departure "al" reason
        res_al = buy_or_wait(_old_points([4000, 4500, 5000, 5500]), 5200.0, NEAR_DEPARTURE, today=TODAY)
        days = res_al["stats"]["days_to_departure"]
        expected_al_reason = f"Kalkışa {days} gün kaldı; fiyatlar genelde son iki haftada yükselir (genel bir eğilim, garanti değil)."
        self.assertIn(expected_al_reason, res_al["reasons"])

        # 2. Falling trend "bekle" reason
        points = [
            ("2026-09-01T10:00:00", 6200.0),
            ("2026-09-10T10:00:00", 6100.0),
            ("2026-10-12T10:00:00", 6000.0),
            ("2026-10-15T10:00:00", 5500.0),
            ("2026-10-18T10:00:00", 5000.0),
        ]
        res_bekle = buy_or_wait(points, 5300.0, "2026-12-15", today=TODAY)
        days_bekle = res_bekle["stats"]["days_to_departure"]
        expected_bekle_reason = f"{days_bekle} gün var; düşüş sürebilir, ama bu garanti değil."
        self.assertIn(expected_bekle_reason, res_bekle["reasons"])

    def test_low_price_near_departure_is_al_with_price_confidence(self):
        result = buy_or_wait(_old_points(TEN_PRICES), 2800.0, "2026-10-24", today=TODAY)
        self.assertEqual(result["advice"], "al")
        self.assertEqual(result["confidence"], "yüksek")

    def test_downward_trend_far_out_is_bekle(self):
        points = [
            ("2026-09-01T10:00:00", 6200.0),
            ("2026-09-10T10:00:00", 6100.0),
            ("2026-10-12T10:00:00", 6000.0),
            ("2026-10-15T10:00:00", 5500.0),
            ("2026-10-18T10:00:00", 5000.0),
        ]
        result = buy_or_wait(points, 5300.0, "2026-12-15", today=TODAY)
        self.assertEqual(result["advice"], "bekle")
        self.assertEqual(result["confidence"], "düşük")
        self.assertAlmostEqual(result["stats"]["trend_7d_pct"], -11.67, places=2)
        self.assertJsonSafe(result)

    def test_falling_trend_but_price_at_low_is_mixed_belirsiz(self):
        points = [
            ("2026-09-01T10:00:00", 6200.0),
            ("2026-09-10T10:00:00", 6100.0),
            ("2026-10-12T10:00:00", 6000.0),
            ("2026-10-15T10:00:00", 5500.0),
            ("2026-10-18T10:00:00", 5000.0),
        ]
        result = buy_or_wait(points, 5000.0, "2026-12-15", today=TODAY)
        self.assertEqual(result["advice"], "belirsiz")
        self.assertTrue(any("çelişiyor" in reason for reason in result["reasons"]))

    def test_flat_mid_range_price_far_out_is_belirsiz(self):
        result = buy_or_wait(_old_points([5000, 5100, 5200, 5300, 5400]), 5350.0, FAR_DEPARTURE, today=TODAY)
        self.assertEqual(result["advice"], "belirsiz")
        self.assertEqual(result["confidence"], "düşük")
        self.assertIsNone(result["stats"]["trend_7d_pct"])
        self.assertTrue(any("Belirgin" in reason for reason in result["reasons"]))

    def test_falling_trend_near_departure_is_not_bekle(self):
        points = [
            ("2026-10-12T10:00:00", 6000.0),
            ("2026-10-15T10:00:00", 5500.0),
            ("2026-10-18T10:00:00", 5000.0),
        ]
        result = buy_or_wait(points, 5300.0, NEAR_DEPARTURE, today=TODAY)
        self.assertEqual(result["advice"], "al")

    def test_invalid_and_future_points_are_ignored(self):
        points = [
            ("2026-08-01T10:00:00", float("nan")),
            ("2026-08-02T10:00:00", None),
            ("2026-08-03T10:00:00", -1.0),
            ("2026-08-04T10:00:00", True),
            ("not-a-date", 3000.0),
            ("2026-11-01T00:00:00", 100.0),  # after today
            ("2026-08-05T10:00:00", 4000.0),
            ("2026-08-06T10:00:00", 4500.0),
            ("2026-08-07T10:00:00", 5000.0),
        ]
        result = buy_or_wait(points, 4200.0, FAR_DEPARTURE, today=TODAY)
        self.assertEqual(result["stats"]["n"], 3)
        self.assertEqual(result["stats"]["min"], 4000.0)
        self.assertNotEqual(result["advice"], "al")  # 4200 is mid-range, not a low price
        self.assertJsonSafe(result)

    def test_departure_in_the_past_is_belirsiz(self):
        result = buy_or_wait(_old_points(TEN_PRICES), 2800.0, "2026-10-01", today=TODAY)
        self.assertEqual(result["advice"], "belirsiz")
        self.assertEqual(result["stats"]["days_to_departure"], -18)
        self.assertTrue(any("geçmiş" in reason for reason in result["reasons"]))

    def test_invalid_current_price_is_belirsiz_and_json_safe(self):
        for bad in (None, 0, -10, float("nan"), float("inf"), True):
            result = buy_or_wait(_old_points(TEN_PRICES), bad, FAR_DEPARTURE, today=TODAY)
            self.assertEqual(result["advice"], "belirsiz", bad)
            self.assertIsNone(result["stats"]["current_percentile"])
            self.assertIsNone(result["stats"]["trend_7d_pct"])
            self.assertJsonSafe(result)

    def test_invalid_depart_date_raises(self):
        with self.assertRaises(ValueError):
            buy_or_wait(_old_points(TEN_PRICES), 2800.0, "yarın", today=TODAY)

    def test_points_are_not_required_to_be_sorted(self):
        shuffled = list(reversed(_old_points(TEN_PRICES)))
        result = buy_or_wait(shuffled, 2800.0, FAR_DEPARTURE, today=TODAY)
        self.assertEqual(result["advice"], "al")


class AdviceForRouteTests(unittest.TestCase):
    def test_wraps_search_log_price_series(self):
        log = SearchLog(":memory:")
        try:
            for price in (3000, 3500, 4000, 4500):
                log.record("ATH", "PRG", "2999-12-31", "ok", 1, float(price))
            today = datetime.now(timezone.utc).date().isoformat()
            result = advice_for_route(log, "ath", "prg", "2999-12-31", 2900.0, today=today)
            self.assertEqual(result["stats"]["n"], 4)
            self.assertEqual(result["stats"]["observations_span_days"], 0)
            self.assertEqual(result["advice"], "al")
            # Same-day log entries have span=0 < 7, so confidence is capped at "düşük"
            self.assertEqual(result["confidence"], "düşük")
            json.dumps(result, allow_nan=False)

            # When records span 9 days (>= 7), confidence rises to "orta"
            for day, p in [("2026-08-01", 3000.0), ("2026-08-05", 3500.0), ("2026-08-10", 4000.0)]:
                log._conn.execute(
                    "INSERT INTO searches (searched_at, origin, destination, travel_date, status, result_count, min_price, source) "
                    "VALUES (?, 'ATH', 'PRG', '2026-12-31', 'ok', 1, ?, 'google_flights')",
                    (f"{day}T10:00:00+00:00", p),
                )
            log._conn.commit()
            res_span = advice_for_route(log, "ath", "prg", "2026-12-31", 2900.0, today="2026-08-25")
            self.assertEqual(res_span["stats"]["observations_span_days"], 9)
            self.assertEqual(res_span["advice"], "al")
            self.assertEqual(res_span["confidence"], "orta")
        finally:
            log.close()

    def test_route_without_history_is_belirsiz(self):
        log = SearchLog(":memory:")
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            result = advice_for_route(log, "ATH", "PRG", "2999-12-31", 2900.0, today=today)
            self.assertEqual(result["advice"], "belirsiz")
            self.assertEqual(result["stats"]["n"], 0)
        finally:
            log.close()

    def test_failed_searches_without_price_are_not_counted(self):
        log = SearchLog(":memory:")
        try:
            log.record("ATH", "PRG", "2999-12-31", "error", 0, None)
            log.record("ATH", "PRG", "2999-12-31", "ok", 1, 3000.0)
            today = datetime.now(timezone.utc).date().isoformat()
            result = advice_for_route(log, "ATH", "PRG", "2999-12-31", 3000.0, today=today)
            self.assertEqual(result["stats"]["n"], 1)
        finally:
            log.close()


if __name__ == "__main__":
    unittest.main()
