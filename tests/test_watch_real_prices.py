"""DOG-4 & DOG-5: Real data watch price readings and alerts."""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from travel_engine.daemon_tracker import DaemonTracker, _option_price
from travel_engine.notifier import AlertNotifier
from travel_engine.tracker import PriceTracker
from travel_engine.trip_planner import TripPlanner

OUTBOUND_QUERY = {
    "depart_dates": ["2026-10-27", "2026-10-28"],
    "arrive_by": "2026-10-28T12:00",
    "depart_after": None,
    "via": None,
    "include_side_routes": True,
}


def leg(type_, operator, frm, to, dep, arr, price, estimated=False):
    return {
        "type": type_, "operator": operator, "from": frm, "to": to,
        "depart_at": dep, "arrive_at": arr, "price_try": price,
        "is_estimated": estimated, "booking_url": "https://example.test/book",
    }


def option(kind, price, legs, dep="2026-10-27T09:00", arr="2026-10-27T11:30", duration_mins=150, **extra):
    base = {
        "kind": kind, "depart_at": dep, "arrive_at": arr,
        "total_try": price, "true_total_try": price, "duration_mins": duration_mins,
        "separate_tickets": False, "via_stop": None,
        "night_wait": False, "has_estimated_segments": False, "score": 0.0,
        "badges": [], "legs": legs,
        "price_complete": True, "unknown_price_legs": 0, "unknown_price_parts": [],
    }
    base.update(extra)
    return base


class FakeNotifier:
    """Records alert calls instead of sending anything."""

    def __init__(self):
        self.alerts = []
        self.mm_alerts = []

    def send_alert(self, **kwargs):
        self.alerts.append(kwargs)
        return dict(kwargs)

    def send_multimodal_alert(self, **kwargs):
        self.mm_alerts.append(kwargs)
        return dict(kwargs)


class FakeProvider:
    def search_flights(self, *args, **kwargs):
        return []


def _ok_response():
    cm = MagicMock()
    cm.__enter__.return_value.status = 200
    return cm


def _sent_payload(mock_urlopen, call_index=-1):
    req = mock_urlopen.call_args_list[call_index][0][0]
    return json.loads(req.data.decode("utf-8"))


class OptionPriceTests(unittest.TestCase):
    def test_option_price_with_known_ticket_legs_and_unknown_extras(self):
        # (kırmızı) _option_price({"price_complete": False, "unknown_price_legs": 0, "unknown_price_parts": ["transfers"], "true_total_try": 9000.0}) -> 9000.0
        opt = {
            "price_complete": False,
            "unknown_price_legs": 0,
            "unknown_price_parts": ["transfers"],
            "true_total_try": 9000.0,
        }
        self.assertEqual(_option_price(opt), 9000.0)

    def test_option_price_guard_unknown_ticket_legs_or_legacy_price_complete_false(self):
        # (koruma) unknown_price_legs: 1 -> None; alan yok ve price_complete: False -> None
        opt_with_unknown_leg = {
            "price_complete": False,
            "unknown_price_legs": 1,
            "unknown_price_parts": [],
            "true_total_try": 9000.0,
        }
        self.assertIsNone(_option_price(opt_with_unknown_leg))

        opt_legacy = {
            "price_complete": False,
            "true_total_try": 9000.0,
        }
        self.assertIsNone(_option_price(opt_legacy))


class RealDataAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="watch_test_")
        self.storage = os.path.join(self.tmpdir, "watches.json")
        self.tracker = PriceTracker(storage_path=self.storage)
        self.notifier = FakeNotifier()
        self.provider = FakeProvider()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_direct_alert_in_real_data_mode_includes_extras_note(self):
        # (kırmızı) Gerçek veri kipinde direkt seçenek (price_complete False, unknown_price_legs 0,
        # unknown_price_parts ["transfers","baggage"], 9.000 TL, bütçe 10.000) -> bir send_alert; kwargs'ta note "transfer" ve "bagaj" içerir.
        direct_opt = option(
            "direct", 9000.0,
            [leg("flight", "Aegean", "ATH", "PRG", "2026-10-27T09:00", "2026-10-27T11:30", 9000.0)],
            price_complete=False,
            unknown_price_legs=0,
            unknown_price_parts=["transfers", "baggage"],
        )
        fake_planner = MagicMock()
        fake_planner.plan_trip.return_value = {
            "query": {"origin": "ATH", "destination": "PRG"},
            "options": [direct_opt],
            "best_direct": direct_opt,
            "cheapest_direct": direct_opt,
            "best_side_route": None,
            "cheapest_side_route": None,
            "searched_dates": ["2026-10-27"],
            "warnings": [],
        }
        daemon = DaemonTracker(
            tracker=self.tracker, notifier=self.notifier, provider=self.provider, trip_planner=fake_planner,
        )
        watch = self.tracker.add_watch(
            origin="ATH", destination="PRG",
            target_budget_try=7000.0, direct_budget_try=10000.0,
            trip_query=dict(OUTBOUND_QUERY), include_multimodal=False,
        )
        res = daemon.scan_watch(watch)
        self.assertEqual(len(self.notifier.alerts), 1)
        alert = self.notifier.alerts[0]
        self.assertEqual(alert["current_price"], 9000.0)
        self.assertIn("note", alert)
        self.assertIn("transfer", alert["note"])
        self.assertIn("bagaj", alert["note"])

    def test_cheapest_side_route_priced_used_when_cheapest_side_route_unpriced(self):
        # (kırmızı) Planda cheapest_side_route bilet fiyatı eksik (5.000, unknown_price_legs 1),
        # cheapest_side_route_priced 6.500 ve bütçe 7.000 -> bir send_multimodal_alert, fiyat 6.500.
        incomplete_side = option(
            "side_route", 5000.0,
            [leg("train", "RegioJet", "ATH", "PRG", "2026-10-27T09:00", "2026-10-27T15:00", None)],
            price_complete=False,
            unknown_price_legs=1,
            unknown_price_parts=[],
        )
        complete_side = option(
            "side_route", 6500.0,
            [leg("bus", "FlixBus", "ATH", "PRG", "2026-10-27T08:00", "2026-10-27T17:00", 6500.0)],
            price_complete=True,
            unknown_price_legs=0,
            unknown_price_parts=[],
        )
        fake_planner = MagicMock()
        fake_planner.plan_trip.return_value = {
            "query": {"origin": "ATH", "destination": "PRG"},
            "options": [incomplete_side, complete_side],
            "best_direct": None,
            "cheapest_direct": None,
            "best_side_route": incomplete_side,
            "cheapest_side_route": incomplete_side,
            "cheapest_side_route_priced": complete_side,
            "searched_dates": ["2026-10-27"],
            "warnings": [],
        }
        daemon = DaemonTracker(
            tracker=self.tracker, notifier=self.notifier, provider=self.provider, trip_planner=fake_planner,
        )
        watch = self.tracker.add_watch(
            origin="ATH", destination="PRG",
            target_budget_try=7000.0, direct_budget_try=10000.0,
            trip_query=dict(OUTBOUND_QUERY), include_multimodal=True,
        )
        res = daemon.scan_watch(watch)
        self.assertEqual(len(self.notifier.mm_alerts), 1)
        alert = self.notifier.mm_alerts[0]
        self.assertEqual(alert["multimodal_price"], 6500.0)


class NotifierNoteTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmpdir = tempfile.mkdtemp(prefix="notifier_note_")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.alerts_file = os.path.join(self.tmpdir, "alerts.json")
        self.config_file = os.path.join(self.tmpdir, "config.json")

    def test_send_alert_records_note_and_includes_in_telegram_when_enabled(self):
        # (kırmızı) AlertNotifier.send_alert(..., note="X") (Telegram kapalı, geçici dosyalar; tests/test_notifier.py deseni)
        # -> kayıtta note == "X"; Telegram açıkken (urlopen yamalı) metin "X" içerir.
        n = AlertNotifier(alerts_file=self.alerts_file, config_file=self.config_file)
        record = n.send_alert(
            origin="ATH", destination="PRG", current_price=9000.0, target_budget=10000.0,
            airline="Aegean", duration="2 sa.", note="Ek masraflar dahil değil (fiyat yok): transfer, bagaj",
        )
        self.assertEqual(record.get("note"), "Ek masraflar dahil değil (fiyat yok): transfer, bagaj")
        saved = n.get_alerts()
        self.assertTrue(len(saved) >= 1)
        self.assertEqual(saved[0].get("note"), "Ek masraflar dahil değil (fiyat yok): transfer, bagaj")

        # Telegram açıkken
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _ok_response()
            n_tel = AlertNotifier(alerts_file=self.alerts_file, config_file=self.config_file)
            n_tel.save_config("123456:TOKEN", "987654", True)
            record_tel = n_tel.send_alert(
                origin="ATH", destination="PRG", current_price=9000.0, target_budget=10000.0,
                airline="Aegean", duration="2 sa.", note="Özel Not X",
            )
            self.assertTrue(record_tel["telegram_sent"])
            payload = _sent_payload(mock_urlopen)
            self.assertIn("Özel Not X", payload["text"])


class TripPlannerPricedDirectTests(unittest.TestCase):
    def test_trip_planner_result_includes_cheapest_direct_priced_skipping_unpriced(self):
        # (kırmızı) TripPlanner sonucu cheapest_direct_priced alanını içerir ve fiyatı eksik seçeneği atlar (sahte motor).
        unpriced = option(
            "direct", 8000.0,
            [leg("flight", "Aegean", "ATH", "PRG", "2026-10-27T09:00", "2026-10-27T11:30", None)],
            price_complete=False,
            unknown_price_legs=1,
            unknown_price_parts=[],
        )
        priced = option(
            "direct", 9500.0,
            [leg("flight", "TAROM", "ATH", "PRG", "2026-10-27T10:00", "2026-10-27T13:00", 9500.0)],
            price_complete=True,
            unknown_price_legs=0,
            unknown_price_parts=[],
        )
        fake_engine = MagicMock()
        fake_engine.search.return_value = {
            "options": [unpriced, priced],
            "best": unpriced,
            "cheapest": unpriced,
            "fastest": unpriced,
            "flight_searches": 1,
            "warnings": [],
            "candidates": {},
            "budget_exhausted": False,
        }
        planner = TripPlanner(flight_provider=FakeProvider(), router=MagicMock(), route_engine=fake_engine)
        plan = planner.plan_trip("ATH", "PRG", ["2026-10-27"])
        self.assertIn("cheapest_direct_priced", plan)
        self.assertIsNotNone(plan["cheapest_direct_priced"])
        self.assertEqual(plan["cheapest_direct_priced"]["true_total_try"], 9500.0)


if __name__ == "__main__":
    unittest.main()
