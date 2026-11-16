"""real_data_only and unknown prices on the planner, the daemon, Telegram and the static UI (no network)."""

import os
import unittest
from datetime import date

from travel_engine import telegram_bot as tb
from travel_engine.daemon_tracker import TRIP_QUERY_KEYS, _option_price
from travel_engine.trip_planner import (
    TripPlanner,
    _legacy_real_only,
    _legacy_real_option,
    _rank_total,
)
from travel_engine.route_engine import REAL_ONLY_EMPTY_WARNING

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class RecordingEngine:
    """Stands in for RouteEngine.search: records the keyword arguments of every call."""

    def __init__(self):
        self.calls = []

    def search(self, origin, destination, depart_dates, **kwargs):
        self.calls.append(dict(kwargs))
        return {"options": [], "best": None, "warnings": [], "flight_searches": 0, "query": {}}


class PlannerRealOnlyTests(unittest.TestCase):
    def test_flag_is_passed_to_the_engine_only_when_set(self):
        engine = RecordingEngine()
        planner = TripPlanner(None, None, route_engine=engine)
        result = planner.plan_trip("Atina", "Prag", ["2030-10-28"], real_data_only=True)
        self.assertIs(engine.calls[-1]["real_data_only"], True)
        self.assertIs(result["query"]["real_data_only"], True)
        planner.plan_trip("Atina", "Prag", ["2030-10-28"])
        self.assertNotIn("real_data_only", engine.calls[-1])  # an engine without the keyword keeps working

    def test_planner_default_is_small_bag_and_no_real_only(self):
        engine = RecordingEngine()
        planner = TripPlanner(None, None, route_engine=engine)
        result = planner.plan_trip("Atina", "Prag", ["2030-10-28"])
        self.assertIs(engine.calls[-1]["cabin_bag"], False)
        self.assertIs(engine.calls[-1]["checked_bag"], False)
        self.assertIs(result["query"]["real_data_only"], False)

    def test_cheapest_is_chosen_by_the_ranking_total(self):
        # A known total of 300 with 500 of ranking value ranks above a known total of 400 with nothing unknown
        options = [
            {"true_total_try": 300.0, "rank_total_try": 800.0, "price_complete": False},
            {"true_total_try": 400.0, "rank_total_try": 400.0, "price_complete": True},
        ]
        self.assertEqual(min(options, key=_rank_total)["true_total_try"], 400.0)
        self.assertEqual(_rank_total({"true_total_try": 250.0}), 250.0)  # no ranking value: the known total


class LegacyRealOnlyTests(unittest.TestCase):
    def test_legacy_filter_drops_estimated_legs_and_night_waits(self):
        clean = {"kind": "direct", "night_wait": False, "has_estimated_segments": False,
                 "legs": [{"type": "flight", "is_estimated": False, "price_is_estimated": False}]}
        estimated = {"kind": "side_route", "night_wait": False, "has_estimated_segments": True,
                     "legs": [{"type": "train", "is_estimated": True, "price_is_estimated": True}]}
        night = {"kind": "direct", "night_wait": True, "has_estimated_segments": False,
                 "legs": [{"type": "flight", "is_estimated": False, "price_is_estimated": False}]}
        self.assertTrue(_legacy_real_option(clean))
        self.assertFalse(_legacy_real_option(estimated))
        self.assertFalse(_legacy_real_option(night))

    def test_legacy_result_keeps_only_real_options_and_recomputes_picks(self):
        clean = {"kind": "direct", "night_wait": False, "has_estimated_segments": False, "true_total_try": 900.0,
                 "legs": [{"type": "flight", "is_estimated": False, "price_is_estimated": False}]}
        estimated = {"kind": "side_route", "night_wait": False, "has_estimated_segments": True,
                     "true_total_try": 100.0, "legs": [{"type": "train", "is_estimated": True}]}
        raw = {"query": {}, "options": [clean, estimated], "best_direct": clean, "best_side_route": estimated,
               "cheapest_direct": clean, "cheapest_side_route": estimated}
        kept, out = _legacy_real_only([clean, estimated], raw)
        self.assertEqual(kept, [clean])
        self.assertIsNone(out["best_side_route"])
        self.assertIsNone(out["cheapest_side_route"])
        self.assertIs(out["cheapest_direct"], clean)
        self.assertIs(out["query"]["real_data_only"], True)

    def test_legacy_result_with_nothing_left_says_so(self):
        estimated = {"kind": "side_route", "night_wait": False, "has_estimated_segments": True,
                     "legs": [{"type": "train", "is_estimated": True}]}
        raw = {"query": {}, "options": [estimated]}
        kept, out = _legacy_real_only([estimated], raw)
        self.assertEqual(kept, [])
        self.assertIn(REAL_ONLY_EMPTY_WARNING, out["warnings"])


class DaemonUnknownPriceTests(unittest.TestCase):
    def test_incomplete_option_has_no_usable_price(self):
        self.assertIsNone(_option_price({"true_total_try": 500.0, "price_complete": False}))
        self.assertEqual(_option_price({"true_total_try": 500.0, "price_complete": True}), 500.0)
        self.assertEqual(_option_price({"true_total_try": 500.0}), 500.0)  # older options: unchanged

    def test_watch_keeps_the_real_only_rule(self):
        self.assertIn("real_data_only", TRIP_QUERY_KEYS)


class TelegramRealOnlyTests(unittest.TestCase):
    TODAY = date(2026, 10, 19)

    def test_gerçek_token_sets_the_rule(self):
        parsed = tb.parse_gezi_command("/gezi Atina Prag 27.10 gerçek", self.TODAY)
        self.assertIs(parsed["real_data_only"], True)
        parsed = tb.parse_gezi_command("/gezi Atina Prag 27.10 gercek", self.TODAY)
        self.assertIs(parsed["real_data_only"], True)
        plain = tb.parse_gezi_command("/gezi Atina Prag 27.10", self.TODAY)
        self.assertNotIn("real_data_only", plain)
        self.assertEqual(tb._planner_options(parsed), {"real_data_only": True})
        self.assertIn("yalnız gerçek veriler", tb._trip_rule_lines(parsed)[0])

    @staticmethod
    def _incomplete_option():
        return {
            "kind": "side_route", "depart_at": "2026-10-27T07:00", "arrive_at": "2026-10-27T13:00",
            "true_total_try": 1200.0, "rank_total_try": 1450.0, "duration_mins": 360, "score": 1500.0,
            "price_complete": False, "unknown_price_legs": 1, "unknown_price_parts": ["transfers"],
            "legs": [
                {"type": "flight", "operator": "Ryanair", "from": "ATH", "to": "BTS", "depart_at": "2026-10-27T07:00",
                 "arrive_at": "2026-10-27T08:30", "price_try": 1200.0, "price_known": True, "is_estimated": False},
                {"type": "train", "operator": "RegioJet", "from": "BTS", "to": "PRG", "depart_at": "2026-10-27T10:00",
                 "arrive_at": "2026-10-27T13:00", "price_try": None, "price_known": False,
                 "price_from_try": 250.0, "is_estimated": False, "origin_station_name": "Bratislava",
                 "dest_station_name": "Praha"},
            ],
        }

    def test_incomplete_totals_read_as_a_lower_bound_with_unknown_legs(self):
        option = self._incomplete_option()
        query = {"origin": "Atina", "destination": "Prag", "depart_dates": ["2026-10-27"], "real_data_only": True}
        text = tb.format_trip_results(query, {"best_side_route": option, "options": [option],
                                              "cheapest_direct": None, "warnings": []})
        self.assertIn("≥", text)
        self.assertIn("+1 bacak fiyatı bilinmiyor", text)
        self.assertIn("transfer fiyatı yok", text)
        self.assertIn("250 ₺'den başlayan", text)
        self.assertNotIn("gerçek toplam", text)  # an incomplete total is never called the real total
        self.assertNotIn("tasarruf", text)  # no saving is claimed against a lower bound

    def test_complete_totals_keep_the_plain_wording(self):
        option = dict(self._incomplete_option(), price_complete=True, unknown_price_legs=0, unknown_price_parts=[])
        option["legs"] = [dict(option["legs"][0]), dict(option["legs"][1], price_try=250.0, price_known=True)]
        text = tb.format_trip_results({"origin": "Atina", "destination": "Prag", "depart_dates": ["2026-10-27"]},
                                      {"best_side_route": option, "options": [option], "warnings": []})
        self.assertIn("gerçek toplam", text)
        self.assertNotIn("≥", text)
        self.assertNotIn("fiyat yok", text)

    def test_unknown_leg_without_from_price_says_fiyat_yok(self):
        option = self._incomplete_option()
        option["legs"][1].pop("price_from_try")
        text = tb.format_trip_results({"origin": "Atina", "destination": "Prag", "depart_dates": ["2026-10-27"]},
                                      {"best_side_route": option, "options": [option], "warnings": []})
        self.assertIn("fiyat yok", text)


class StaticUiWiringTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
            self.html = fh.read()
        with open(os.path.join(STATIC, "js", "app.js"), encoding="utf-8") as fh:
            self.js = fh.read()

    def test_toggle_and_small_bag_default_are_in_the_form(self):
        self.assertIn('id="tpRealOnly"', self.html)
        self.assertIn("✅ Sadece gerçek veriler", self.html)
        self.assertIn('name="tpBag" value="small" checked', self.html)
        self.assertNotIn('name="tpBag" value="cabin" checked', self.html)

    def test_flag_is_sent_persisted_and_restored(self):
        self.assertIn("params.set('real_data_only'", self.js)
        self.assertIn("real_data_only: !!f.realOnly", self.js)
        self.assertIn("real_data_only: rules.real_data_only", self.js)
        self.assertIn("travelplan_trip_real_only_v1", self.js)
        self.assertIn("setTripRealOnly(tq.real_data_only === true)", self.js)
        self.assertIn("Gerçek veriyle seçenek bulunamadı", self.js)

    def test_unknown_prices_render_as_text_not_numbers(self):
        self.assertIn("fiyat yok", self.js)
        self.assertIn("'den başlayan", self.js)
        self.assertIn("≥ ", self.js)


if __name__ == "__main__":
    unittest.main()
