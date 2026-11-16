import json
import math
import unittest

from travel_engine.flexdates import cheapest_by_date, expand_flex_dates, flex_summary


def _opt(depart_at, price, kind="direct"):
    return {"kind": kind, "depart_at": depart_at, "arrive_at": depart_at, "true_total_try": price}


class ExpandFlexDatesTests(unittest.TestCase):
    def test_center_first_then_nearest_alternating_later_first(self):
        result = expand_flex_dates("2026-10-20", 2, min_date="2026-01-01", max_count=10)
        self.assertEqual(result, ["2026-10-20", "2026-10-21", "2026-10-19", "2026-10-22", "2026-10-18"])

    def test_default_cap_is_seven(self):
        result = expand_flex_dates("2026-10-20", 3, min_date="2026-01-01")
        self.assertEqual(len(result), 7)
        self.assertEqual(result[0], "2026-10-20")

    def test_month_end_crosses_into_next_month(self):
        result = expand_flex_dates("2026-10-31", 3, min_date="2026-01-01", max_count=7)
        self.assertEqual(
            result,
            ["2026-10-31", "2026-11-01", "2026-10-30", "2026-11-02", "2026-10-29", "2026-11-03", "2026-10-28"],
        )

    def test_month_start_crosses_into_previous_month(self):
        result = expand_flex_dates("2026-03-01", 2, min_date="2026-01-01", max_count=5)
        self.assertEqual(result, ["2026-03-01", "2026-03-02", "2026-02-28", "2026-03-03", "2026-02-27"])

    def test_year_end_crosses_into_next_year(self):
        result = expand_flex_dates("2026-12-31", 2, min_date="2026-01-01", max_count=5)
        self.assertEqual(result, ["2026-12-31", "2027-01-01", "2026-12-30", "2027-01-02", "2026-12-29"])

    def test_leap_day_is_handled(self):
        result = expand_flex_dates("2028-02-28", 2, min_date="2028-01-01", max_count=5)
        self.assertEqual(result, ["2028-02-28", "2028-02-29", "2028-02-27", "2028-03-01", "2028-02-26"])

    def test_min_date_clips_earlier_days(self):
        result = expand_flex_dates("2026-10-20", 3, min_date="2026-10-19", max_count=10)
        self.assertEqual(result, ["2026-10-20", "2026-10-21", "2026-10-19", "2026-10-22", "2026-10-23"])

    def test_min_date_after_center_drops_center(self):
        result = expand_flex_dates("2026-10-20", 2, min_date="2026-10-21", max_count=10)
        self.assertEqual(result, ["2026-10-21", "2026-10-22"])

    def test_zero_flex_returns_only_center(self):
        self.assertEqual(expand_flex_dates("2026-10-20", 0, min_date="2026-01-01"), ["2026-10-20"])

    def test_negative_flex_is_treated_as_zero(self):
        self.assertEqual(expand_flex_dates("2026-10-20", -3, min_date="2026-01-01"), ["2026-10-20"])

    def test_max_count_caps_and_zero_gives_empty(self):
        self.assertEqual(
            expand_flex_dates("2026-10-20", 3, min_date="2026-01-01", max_count=3),
            ["2026-10-20", "2026-10-21", "2026-10-19"],
        )
        self.assertEqual(expand_flex_dates("2026-10-20", 3, min_date="2026-01-01", max_count=0), [])

    def test_default_min_date_is_today_so_past_days_are_dropped(self):
        result = expand_flex_dates("2000-01-01", 1)
        self.assertEqual(result, [])
        far = expand_flex_dates("2999-01-01", 1)
        self.assertEqual(far, ["2999-01-01", "2999-01-02", "2998-12-31"])

    def test_invalid_center_raises(self):
        with self.assertRaises(ValueError):
            expand_flex_dates("27.10.2026", 1, min_date="2026-01-01")


class CheapestByDateTests(unittest.TestCase):
    def test_groups_by_departure_day_and_picks_cheapest_with_index(self):
        options = [
            _opt("2026-10-27T18:00", 4000),
            _opt("2026-10-27T08:00", 3200),
            _opt("2026-10-25T09:00", 3500),
            _opt("2026-10-27T12:00", 3200),  # tie with index 1: lower index wins
        ]
        self.assertEqual(
            cheapest_by_date(options),
            [
                {"date": "2026-10-25", "best_true_total_try": 3500.0, "option_index": 2},
                {"date": "2026-10-27", "best_true_total_try": 3200.0, "option_index": 1},
            ],
        )

    def test_invalid_entries_are_skipped(self):
        options = [
            _opt("2026-10-27T08:00", None),
            _opt("2026-10-27T08:00", float("nan")),
            _opt("2026-10-27T08:00", float("inf")),
            _opt("2026-10-27T08:00", True),
            _opt("2026-10-27T08:00", -5),
            _opt("2026-10-27T08:00", 0),
            {"depart_at": None, "true_total_try": 100},
            {"depart_at": "not-a-date", "true_total_try": 100},
            _opt("2026-10-28T08:00", 2900),
        ]
        self.assertEqual(
            cheapest_by_date(options),
            [{"date": "2026-10-28", "best_true_total_try": 2900.0, "option_index": 8}],
        )

    def test_empty_inputs(self):
        self.assertEqual(cheapest_by_date([]), [])
        self.assertEqual(cheapest_by_date(None), [])


class DepartureDayTimeZoneTests(unittest.TestCase):
    """The departure day follows depart_utc in the origin zone (depart_tz) when both are given."""

    def test_early_departure_belongs_to_the_origin_day_not_the_utc_day(self):
        # 01:30 Istanbul (UTC+3) on 28 Oct is 22:30 UTC on 27 Oct
        option = {"kind": "direct", "depart_utc": "2026-10-27T22:30:00Z", "depart_tz": "Europe/Istanbul",
                  "true_total_try": 3000}
        self.assertEqual(cheapest_by_date([option]), [{"date": "2026-10-28", "best_true_total_try": 3000.0,
                                                       "option_index": 0}])

    def test_utc_instant_is_enough_without_a_local_string(self):
        option = {"kind": "direct", "depart_utc": "2026-10-27T22:30:00Z", "depart_tz": "Europe/Istanbul",
                  "true_total_try": 3000}
        self.assertEqual([entry["date"] for entry in cheapest_by_date([option])], ["2026-10-28"])

    def test_options_on_the_same_origin_day_are_grouped_together(self):
        options = [
            {"kind": "direct", "depart_at": "2026-10-28T01:30", "depart_utc": "2026-10-27T22:30:00Z",
             "depart_tz": "Europe/Istanbul", "true_total_try": 3000},
            {"kind": "direct", "depart_at": "2026-10-28T09:00", "depart_utc": "2026-10-28T06:00:00Z",
             "depart_tz": "Europe/Istanbul", "true_total_try": 2500},
        ]
        self.assertEqual(cheapest_by_date(options),
                         [{"date": "2026-10-28", "best_true_total_try": 2500.0, "option_index": 1}])

    def test_late_evening_in_london_stays_on_the_same_day(self):
        # 27 Oct 23:30 UTC is 23:30 in London (GMT after 25 Oct)
        option = {"kind": "direct", "depart_utc": "2026-10-27T23:30:00Z", "depart_tz": "Europe/London",
                  "true_total_try": 3000}
        self.assertEqual([entry["date"] for entry in cheapest_by_date([option])], ["2026-10-27"])

    def test_without_a_usable_zone_the_local_string_decides(self):
        no_zone = {"kind": "direct", "depart_at": "2026-10-28T01:30", "depart_utc": "2026-10-27T22:30:00Z",
                   "true_total_try": 3000}
        bad_zone = dict(no_zone, depart_tz="Mars/Base")
        for option in (no_zone, bad_zone):
            with self.subTest(option=option.get("depart_tz")):
                self.assertEqual([entry["date"] for entry in cheapest_by_date([option])], ["2026-10-28"])

    def test_naive_utc_value_is_ignored(self):
        option = {"kind": "direct", "depart_at": "2026-10-28T01:30", "depart_utc": "2026-10-27T22:30:00",
                  "depart_tz": "Europe/Istanbul", "true_total_try": 3000}
        self.assertEqual([entry["date"] for entry in cheapest_by_date([option])], ["2026-10-28"])


class FlexSummaryTests(unittest.TestCase):
    OPTIONS = [
        _opt("2026-10-26T08:00", 4200),
        _opt("2026-10-27T08:00", 3100),
        _opt("2026-10-27T20:00", 3300),
        _opt("2026-10-28T08:00", 3600),
    ]

    def test_summary_with_requested_date_and_saving(self):
        summary = flex_summary(self.OPTIONS, "2026-10-26")
        self.assertEqual(summary["requested_date"], "2026-10-26")
        self.assertEqual(summary["requested_best"], 4200.0)
        self.assertEqual(summary["best_date"], "2026-10-27")
        self.assertEqual(summary["best_price"], 3100.0)
        self.assertEqual(summary["saving_vs_requested"], 1100.0)
        self.assertEqual([entry["date"] for entry in summary["by_date"]],
                         ["2026-10-26", "2026-10-27", "2026-10-28"])

    def test_requested_date_is_cheapest_gives_zero_saving(self):
        options = [_opt("2026-10-27T08:00", 3000), _opt("2026-10-28T08:00", 3500)]
        summary = flex_summary(options, "2026-10-27")
        self.assertEqual(summary["best_date"], "2026-10-27")
        self.assertEqual(summary["saving_vs_requested"], 0.0)

    def test_requested_date_missing_from_options(self):
        summary = flex_summary(self.OPTIONS, "2026-10-30")
        self.assertIsNone(summary["requested_best"])
        self.assertIsNone(summary["saving_vs_requested"])
        self.assertEqual(summary["best_date"], "2026-10-27")
        self.assertEqual(summary["best_price"], 3100.0)

    def test_empty_options(self):
        summary = flex_summary([], "2026-10-27")
        self.assertEqual(
            summary,
            {
                "requested_date": "2026-10-27",
                "requested_best": None,
                "best_date": None,
                "best_price": None,
                "saving_vs_requested": None,
                "by_date": [],
            },
        )

    def test_tie_on_best_price_prefers_earliest_date(self):
        options = [_opt("2026-10-29T08:00", 3000), _opt("2026-10-27T08:00", 3000)]
        summary = flex_summary(options, "2026-10-29")
        self.assertEqual(summary["best_date"], "2026-10-27")
        self.assertEqual(summary["saving_vs_requested"], 0.0)

    def test_output_is_json_safe(self):
        options = self.OPTIONS + [_opt("2026-10-30T08:00", float("nan")), _opt("2026-10-31T08:00", None)]
        for requested in ("2026-10-26", "2026-11-05"):
            summary = flex_summary(options, requested)
            json.dumps(summary, allow_nan=False)
            for value in (summary["requested_best"], summary["best_price"], summary["saving_vs_requested"]):
                self.assertTrue(value is None or math.isfinite(value))
        json.dumps(flex_summary([], "2026-10-27"), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
