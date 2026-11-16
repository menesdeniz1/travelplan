import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from travel_engine import telegram_bot as tb
from travel_engine.telegram_bot import (
    TelegramCommandBot,
    format_results,
    format_trip_results,
    parse_gezi_command,
    parse_search_command,
    parse_sil_command,
    parse_takip_command,
)
from travel_engine.tracker import PriceTracker

TODAY = date(2026, 10, 19)
OWNER = "111"


class _StubNotifier:
    def __init__(self, chat_id=OWNER, token="token", enabled=True):
        self.config = {
            "telegram_bot_token": token,
            "telegram_chat_id": chat_id,
            "notifications_enabled": enabled,
        }
        self.sent = []

    def _post_telegram(self, text, disable_preview=True):
        self.sent.append(text)
        return True, ""


class _StubProvider:
    def __init__(self, flights=None, error=None):
        self.flights = flights if flights is not None else []
        self.error = error
        self.calls = []

    def search_flights(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return list(self.flights)


class _StubRouter:
    def __init__(self, result=None):
        self.result = result if result is not None else {"multimodal_routes": []}
        self.calls = []

    def search_multimodal(self, origin, destination, date, **kwargs):
        self.calls.append({"origin": origin, "destination": destination, "date": date, **kwargs})
        return self.result


def _direct(airline="Wizz Air", price=2500.0):
    return {
        "airline": airline,
        "times": "07:10 – 09:30",
        "duration": "2 sa 20 dk",
        "is_direct": True,
        "total_price_try": price,
        "google_flights_url": "https://www.google.com/travel/flights?q=x",
    }


def _journey(price=3000.0, **extra):
    base = {
        "route_id": "MM-1",
        "total_price_try": price,
        "savings_try": 500.0,
        "total_duration_mins": 375,
        "segments": [
            {"type": "flight", "operator": "Wizz Air", "from": "ATH", "to": "BTS",
             "departure_time": "23:50", "arrival_time": "01:05", "price_try": 1000.0,
             "is_estimated": False},
            {"type": "transfer", "operator": "Airport Ground Transit", "from": "BTS Airport Terminal",
             "to": "Bratislava", "departure_time": "01:05", "arrival_time": "06:00",
             "price_try": 0.0, "is_estimated": True},
            {"type": "train", "operator": "RegioJet", "from": "Bratislava", "to": "Praha",
             "departure_time": "06:00", "arrival_time": "10:20", "price_try": 500.0,
             "is_estimated": False},
        ],
    }
    base.update(extra)
    return base


def _update(text, chat_id=int(OWNER), update_id=1):
    return {"update_id": update_id, "message": {"message_id": 5, "chat": {"id": chat_id}, "text": text}}


class ParseSearchCommandTests(unittest.TestCase):
    def parse(self, text, today=TODAY):
        return parse_search_command(text, today)

    def test_day_month_without_year(self):
        self.assertEqual(
            self.parse("/ara Atina Prag 28.10"),
            {"origin": "Atina", "destination": "Prag", "date": "2026-10-28"},
        )

    def test_day_month_year(self):
        self.assertEqual(self.parse("/ara Atina Prag 28.10.2026")["date"], "2026-10-28")

    def test_iso_date(self):
        self.assertEqual(self.parse("/ara Atina Prag 2026-10-28")["date"], "2026-10-28")

    def test_two_digit_year(self):
        self.assertEqual(self.parse("/ara Atina Prag 28.10.26")["date"], "2026-10-28")

    def test_tomorrow_and_today(self):
        self.assertEqual(self.parse("/ara Atina Prag yarın")["date"], "2026-10-20")
        self.assertEqual(self.parse("/ara Atina Prag yarin")["date"], "2026-10-20")
        self.assertEqual(self.parse("/ara Atina Prag bugün")["date"], "2026-10-19")

    def test_quoted_multiword_city(self):
        self.assertEqual(
            self.parse('/ara "New York" Londra 3.11'),
            {"origin": "New York", "destination": "Londra", "date": "2026-11-03"},
        )

    def test_dash_is_space(self):
        self.assertEqual(self.parse("/ara New-York Londra 3.11")["origin"], "New York")

    def test_year_rollover(self):
        self.assertEqual(
            self.parse("/ara A B 3.1", today=date(2026, 12, 30))["date"], "2027-01-03"
        )

    def test_today_counts_as_not_passed(self):
        self.assertEqual(self.parse("/ara A B 19.10")["date"], "2026-10-19")

    def test_past_day_month_rolls_to_next_year(self):
        self.assertEqual(self.parse("/ara A B 18.10")["date"], "2027-10-18")

    def test_feb_29_rolls_to_next_leap_year(self):
        self.assertEqual(self.parse("/ara A B 29.2")["date"], "2028-02-29")

    def test_bot_username_suffix(self):
        self.assertEqual(self.parse("/ara@TravelBot Atina Prag 28.10")["destination"], "Prag")

    def test_help_commands(self):
        self.assertEqual(self.parse("/start"), {"help": True})
        self.assertEqual(self.parse("/yardim"), {"help": True})

    def test_invalid_input_returns_none(self):
        invalid = [
            "",
            "merhaba",
            "/ara",
            "/ara Atina",
            "/ara Atina Prag",
            "/ara Atina Prag 31.02",
            "/ara Atina Prag 2026-13-01",
            "/ara Atina Prag bir gün",
            "/ara A B C D 3.11",
            '/ara "New York Londra 3.11',
            "/ara Prag Prag 3.11",
            "/ara - Prag 3.11",
            "/bilinmeyen Atina Prag 28.10",
        ]
        for text in invalid:
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_non_string_input(self):
        self.assertIsNone(parse_search_command(None, TODAY))


class FormatResultsTests(unittest.TestCase):
    def test_no_direct_and_no_routes(self):
        text = format_results("Atina", "Prag", "2026-10-28", [], {"multimodal_routes": []})
        self.assertIn("Direkt uçuş bulunamadı", text)
        self.assertIn("Yan rota bulunamadı", text)
        self.assertIn("28.10.2026", text)

    def test_cheapest_direct_and_chain(self):
        text = format_results(
            "Atina", "Prag", "2026-10-28",
            [_direct(price=3000), _direct("Ryanair", 2100)],
            {"multimodal_routes": [_journey(price=3000.0, true_total_try=1800.0)]},
        )
        self.assertIn("Ryanair", text)
        self.assertIn("2,100 ₺", text)
        self.assertIn("✈️ Wizz Air ATH→BTS 23:50–01:05", text)
        self.assertIn("🚆 Bratislava→Praha 06:00–10:20", text)
        self.assertIn("1,800 ₺", text)  # true_total_try preferred over total_price_try
        self.assertNotIn("3,000 ₺", text)
        self.assertIn("<code>+300 ₺</code> tasarruf", text)  # 2100 direct - 1800 total
        self.assertIn("6 sa 15 dk", text)

    def test_no_savings_line_when_journey_costs_more(self):
        text = format_results(
            "Atina", "Prag", "2026-10-28", [_direct(price=2100)],
            {"multimodal_routes": [_journey(price=3000.0)]},
        )
        self.assertIn("3,000 ₺", text)
        self.assertNotIn("tasarruf", text)

    def test_flags(self):
        journey = _journey(night_wait=True, night_wait_mins=420, arrival_day_offset=1)
        journey["segments"][2]["is_estimated"] = True  # ground timetable is estimated
        text = format_results("Atina", "Prag", "2026-10-28", [], {"multimodal_routes": [journey]})
        self.assertIn("🌙 gece beklemesi", text)
        self.assertIn("+1 gün", text)
        self.assertIn("⚠️ tren saatleri tahmini", text)

    def test_estimated_flag_absent_for_live_trains(self):
        journey = _journey()
        journey["segments"][2]["is_estimated"] = False
        text = format_results("Atina", "Prag", "2026-10-28", [], {"multimodal_routes": [journey]})
        self.assertNotIn("tren saatleri tahmini", text)

    def test_mm_failure_still_shows_direct(self):
        text = format_results("Atina", "Prag", "2026-10-28", [_direct()], None)
        self.assertIn("Wizz Air", text)
        self.assertIn("Yan rota araması tamamlanamadı", text)

    def test_airline_names_are_escaped(self):
        text = format_results("Atina", "Prag", "2026-10-28", [_direct("<b>Evil</b> & Co")], None)
        self.assertIn("&lt;b&gt;Evil&lt;/b&gt; &amp; Co", text)
        self.assertNotIn("<b>Evil</b>", text)

    def test_route_fields_are_escaped(self):
        journey = _journey()
        journey["segments"][0]["operator"] = "<i>x</i>"
        text = format_results("Atina", "Prag", "2026-10-28", [], {"multimodal_routes": [journey]})
        self.assertIn("&lt;i&gt;x&lt;/i&gt;", text)

    def test_limit_and_length_cap_with_50_routes(self):
        long_name = "Z" * 300
        routes = [_journey(price=1000 + i, segments=[
            {"type": "flight", "operator": long_name, "from": "ATH", "to": "BTS",
             "departure_time": "23:50", "arrival_time": "01:05"},
            {"type": "train", "operator": long_name, "from": long_name, "to": long_name,
             "departure_time": "06:00", "arrival_time": "10:20"},
        ]) for i in range(50)]
        text = format_results("Atina", "Prag", "2026-10-28", [], {"multimodal_routes": routes}, limit=50)
        self.assertLess(len(text), 4000)
        self.assertIn("rota daha", text)

    def test_default_limit_is_three(self):
        routes = [_journey(price=1000 + i) for i in range(10)]
        text = format_results("Atina", "Prag", "2026-10-28", [], {"multimodal_routes": routes})
        self.assertIn("<b>3.</b>", text)
        self.assertNotIn("<b>4.</b>", text)
        self.assertIn("7 rota daha", text)


class HandleUpdateTests(unittest.TestCase):
    def setUp(self):
        self.notifier = _StubNotifier()
        self.provider = _StubProvider(flights=[_direct()])
        self.router = _StubRouter({"multimodal_routes": [_journey()]})
        self.bot = TelegramCommandBot(self.notifier, self.provider, self.router)

    def test_foreign_chat_is_ignored_silently(self):
        self.bot.handle_update(_update("/ara Atina Prag 2030-11-03", chat_id=999))
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.notifier.sent, [])

    def test_stale_message_from_before_restart_is_ignored(self):
        update = _update("/ara Atina Prag 2030-11-03")
        update["message"]["date"] = int(time.time()) - 3600
        self.bot.handle_update(update)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.notifier.sent, [])

    def test_recent_message_is_processed(self):
        update = _update("/start")
        update["message"]["date"] = int(time.time()) - 5
        self.bot.handle_update(update)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_string_chat_id_in_update_matches(self):
        self.bot.handle_update(_update("/start", chat_id=OWNER))
        self.assertEqual(len(self.notifier.sent), 1)

    def test_owner_search_runs_and_sends_progress_then_result(self):
        self.bot.handle_update(_update("/ara Atina Prag 2030-11-03"))

        self.assertEqual(self.provider.calls, [{
            "origin": "Atina",
            "destination": "Prag",
            "departure_date": "2030-11-03",
            "trip_type": "oneway",
        }])
        self.assertEqual(len(self.router.calls), 1)
        self.assertEqual(self.router.calls[0]["prefetched_direct_flights"], [_direct()])
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertIn("Aranıyor", self.notifier.sent[0])
        self.assertIn("Wizz Air", self.notifier.sent[1])
        self.assertFalse(self.bot.is_busy)

    def test_provider_error_replies_with_turkish_error(self):
        self.bot.flight_provider = _StubProvider(error=RuntimeError("boom"))
        self.bot.handle_update(_update("/ara Atina Prag 2030-11-03"))
        self.assertIn("hata", self.notifier.sent[-1])
        self.assertFalse(self.bot.is_busy)

    def test_router_error_still_sends_direct_results(self):
        self.bot.router = MagicMock()
        self.bot.router.search_multimodal.side_effect = RuntimeError("router down")
        self.bot.handle_update(_update("/ara Atina Prag 2030-11-03"))
        self.assertIn("Wizz Air", self.notifier.sent[-1])
        self.assertIn("Yan rota araması tamamlanamadı", self.notifier.sent[-1])

    def test_busy_flag_rejects_concurrent_search(self):
        self.assertTrue(self.bot._search_lock.acquire(blocking=False))
        try:
            self.bot.handle_update(_update("/ara Atina Prag 2030-11-03"))
        finally:
            self.bot._search_lock.release()
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.notifier.sent, ["Önceki arama sürüyor, lütfen bekleyin."])

    def test_busy_flag_released_after_search(self):
        self.bot.handle_update(_update("/ara Atina Prag 2030-11-03"))
        self.bot.handle_update(_update("/ara Atina Prag 2030-11-04"))
        self.assertEqual(len(self.provider.calls), 2)

    def test_unparsable_slash_command_gets_hint(self):
        self.bot.handle_update(_update("/ara Atina"))
        self.assertIn("anlaşılamadı", self.notifier.sent[0])
        self.assertEqual(self.provider.calls, [])

    def test_plain_text_is_ignored(self):
        self.bot.handle_update(_update("merhaba"))
        self.assertEqual(self.notifier.sent, [])

    def test_non_message_update_is_ignored(self):
        self.bot.handle_update({"update_id": 3, "edited_message": {"chat": {"id": int(OWNER)}}})
        self.bot.handle_update({})
        self.assertEqual(self.notifier.sent, [])


class PollingTests(unittest.TestCase):
    class _ScriptedStop:
        """Stop event that records wait() timeouts and stops after `max_waits` waits."""

        def __init__(self, max_waits):
            self.waits = []
            self.max_waits = max_waits

        def is_set(self):
            return len(self.waits) >= self.max_waits

        def wait(self, timeout=None):
            self.waits.append(timeout)
            return self.is_set()

    def setUp(self):
        self.notifier = _StubNotifier()
        self.bot = TelegramCommandBot(self.notifier, _StubProvider(), _StubRouter(), poll_timeout=25)

    def test_get_updates_request_parameters(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"ok": True, "result": [{"update_id": 7}]}).encode()
        cm = MagicMock()
        cm.__enter__.return_value = response
        with patch("travel_engine.telegram_bot.urllib.request.urlopen", return_value=cm) as urlopen:
            self.bot._offset = 7
            updates = self.bot._get_updates()

        self.assertEqual(updates, [{"update_id": 7}])
        req, = urlopen.call_args[0]
        self.assertIn("/bottoken/getUpdates", req.full_url)
        self.assertIn("timeout=25", req.full_url)
        self.assertIn("offset=7", req.full_url)
        self.assertIn("allowed_updates", req.full_url)
        self.assertEqual(urlopen.call_args[1]["timeout"], 35)

    def test_dispatch_advances_offset(self):
        with patch.object(self.bot, "_safe_handle") as handle:
            self.bot._dispatch([{"update_id": 10}, {"update_id": 11}])
            for t in threading.enumerate():
                if t.name == "telegram-cmd":
                    t.join(1)
        self.assertEqual(self.bot._offset, 12)
        self.assertEqual(handle.call_count, 2)

    def test_run_forever_idle_when_not_configured(self):
        self.notifier.config["telegram_bot_token"] = ""
        stop = self._ScriptedStop(max_waits=1)
        with patch.object(self.bot, "_get_updates") as get_updates:
            self.bot.run_forever(stop)
        get_updates.assert_not_called()
        self.assertEqual(stop.waits, [30])

    def test_run_forever_idle_when_notifications_disabled(self):
        self.notifier.config["notifications_enabled"] = False
        stop = self._ScriptedStop(max_waits=1)
        with patch.object(self.bot, "_get_updates") as get_updates:
            self.bot.run_forever(stop)
        get_updates.assert_not_called()
        self.assertEqual(stop.waits, [30])

    def test_run_forever_backs_off_on_network_errors(self):
        stop = self._ScriptedStop(max_waits=6)
        with patch.object(self.bot, "_get_updates", side_effect=OSError("offline")):
            self.bot.run_forever(stop)
        self.assertEqual(stop.waits, [5, 10, 20, 40, 60, 60])

    def test_run_forever_dispatches_updates_and_resets_backoff(self):
        stop = self._ScriptedStop(max_waits=100)
        calls = {"n": 0}

        def fake_get_updates():
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("first failure")
            if calls["n"] == 2:
                return [{"update_id": 4}]
            self.bot.stop()  # end the loop
            return []

        with patch.object(self.bot, "_get_updates", side_effect=fake_get_updates), \
                patch.object(self.bot, "_dispatch") as dispatch:
            self.bot.run_forever(stop)
        self.assertEqual(stop.waits, [5])
        dispatch.assert_called_once_with([{"update_id": 4}])

    def test_start_in_thread_and_stop(self):
        with patch.object(self.bot, "_get_updates", return_value=[]) as get_updates:
            thread = self.bot.start_in_thread()
            self.assertTrue(thread.daemon)
            self.assertTrue(thread.is_alive())
            self.bot.stop()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(get_updates.call_count, 1)


def _leg(kind, operator, frm, to, dep, arr, estimated=False):
    return {
        "type": kind, "operator": operator, "from": frm, "to": to,
        "depart_at": dep, "arrive_at": arr, "price_try": 1000.0,
        "is_estimated": estimated, "booking_url": "",
    }


def _side_option(**extra):
    option = {
        "kind": "side_route",
        "depart_at": "2026-10-28T23:50",
        "arrive_at": "2026-10-29T10:20",
        "total_try": 1800.0,
        "true_total_try": 1800.0,
        "duration_mins": 630,
        "separate_tickets": True,
        "via_stop": None,
        "night_wait": True,
        "has_estimated_segments": True,
        "score": 1.0,
        "badges": [],
        "legs": [
            _leg("flight", "Wizz Air", "ATH", "BTS", "2026-10-28T23:50", "2026-10-29T01:05"),
            _leg("transfer", "Airport Ground Transit", "BTS Airport", "Bratislava",
                 "2026-10-29T01:05", "2026-10-29T06:00"),
            _leg("train", "RegioJet", "Bratislava", "Praha", "2026-10-29T06:00", "2026-10-29T10:20", True),
        ],
    }
    option.update(extra)
    return option


def _direct_option(price=2100.0):
    return {
        "kind": "direct",
        "depart_at": "2026-10-28T07:00",
        "arrive_at": "2026-10-28T09:20",
        "total_try": price,
        "true_total_try": price,
        "duration_mins": 140,
        "separate_tickets": False,
        "via_stop": None,
        "night_wait": False,
        "has_estimated_segments": False,
        "score": 2.0,
        "badges": [],
        "legs": [_leg("flight", "Ryanair", "ATH", "PRG", "2026-10-28T07:00", "2026-10-28T09:20")],
    }


class _FakePlanner:
    def __init__(self):
        self.calls = []
        side = _side_option()
        direct = _direct_option()
        self.result = {
            "options": [side, direct],
            "best_direct": direct,
            "best_side_route": side,
            "cheapest_direct": direct,
            "cheapest_side_route": side,
            "warnings": ["2030-11-04 için kurallara uyan seçenek bulunamadı"],
        }

    def plan_trip(self, origin, destination, depart_dates, **kwargs):
        self.calls.append({"origin": origin, "destination": destination, "depart_dates": depart_dates, **kwargs})
        return self.result


class _FakeHealth:
    def __init__(self, status=None, error=None):
        self.status = status
        self.error = error

    def health_status(self):
        if self.error:
            raise self.error
        return self.status


class GeziParseTests(unittest.TestCase):
    def parse(self, text):
        return parse_gezi_command(text, TODAY)

    def test_not_gezi_returns_none(self):
        self.assertIsNone(self.parse("/ara Atina Prag 28.10"))
        self.assertIsNone(self.parse("merhaba"))

    def test_single_date(self):
        self.assertEqual(self.parse("/gezi Atina Prag 28.10"), {
            "origin": "Atina", "destination": "Prag", "depart_dates": ["2026-10-28"],
            "arrive_by": None, "via": None,
        })

    def test_day_range(self):
        result = self.parse("/gezi Atina Prag 27.10-28.10")
        self.assertEqual(result["depart_dates"], ["2026-10-27", "2026-10-28"])

    def test_bare_day_borrows_month_of_range_end(self):
        self.assertEqual(self.parse("/gezi Atina Prag 28-30.10")["depart_dates"],
                         ["2026-10-28", "2026-10-29", "2026-10-30"])

    def test_iso_range(self):
        self.assertEqual(self.parse("/gezi Atina Prag 2030-11-03-2030-11-04")["depart_dates"],
                         ["2030-11-03", "2030-11-04"])

    def test_range_across_year_end(self):
        result = parse_gezi_command("/gezi A B 30.12-2.01", date(2026, 12, 1))
        self.assertEqual(result["depart_dates"], ["2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02"])

    def test_range_of_seven_days_is_allowed_eight_is_not(self):
        self.assertEqual(len(self.parse("/gezi A B 20.10-26.10")["depart_dates"]), 7)
        result = self.parse("/gezi A B 20.10-27.10")
        self.assertIn("error", result)
        self.assertIn("7 gün", result["error"])

    def test_reversed_range_is_error(self):
        self.assertIn("error", self.parse("/gezi A B 28.10-27.10"))

    def test_arrive_by_with_en_gec(self):
        result = self.parse("/gezi Atina Prag 27.10-28.10 en geç 28.10 12:00")
        self.assertEqual(result["arrive_by"], "2026-10-28T12:00")
        self.assertIsNone(result["via"])

    def test_arrive_by_accepts_dot_time_and_iso_date(self):
        self.assertEqual(self.parse("/gezi A B 27.10 en geç 2026-10-28 9.30")["arrive_by"],
                         "2026-10-28T09:30")

    def test_arrive_by_before_departure_is_error(self):
        result = self.parse("/gezi Atina Prag 28.10 en geç 27.10 12:00")
        self.assertIn("error", result)
        self.assertIn("kalkış tarihinden önce", result["error"])

    def test_en_gec_without_time_is_helpful_error(self):
        for text in ("/gezi A B 28.10 en geç", "/gezi A B 28.10 en geç 28.10", "/gezi A B 28.10 en geç 12:00"):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertIn("en geç", result["error"])
                self.assertIn("Kullanım", result["error"])

    def test_via_single_place(self):
        result = self.parse("/gezi Atina Prag 27.10 üzerinden Yunanistan")
        self.assertEqual(result["via"], ["Yunanistan"])
        self.assertIsNone(result["arrive_by"])

    def test_via_lowercase_without_turkish_letter(self):
        self.assertEqual(self.parse("/gezi A B 27.10 uzerinden Yunanistan")["via"], ["Yunanistan"])

    def test_via_multi_word_quoted_and_comma_list(self):
        self.assertEqual(self.parse('/gezi A B 27.10 üzerinden "Yeni Delhi"')["via"], ["Yeni Delhi"])
        self.assertEqual(self.parse("/gezi A B 27.10 üzerinden Yunanistan, Macaristan")["via"],
                         ["Yunanistan", "Macaristan"])

    def test_via_and_arrive_by_in_either_order(self):
        expected = {"arrive_by": "2030-11-04T12:00", "via": ["Yunanistan"]}
        for text in (
            "/gezi A B 2030-11-03 üzerinden Yunanistan en geç 2030-11-04 12:00",
            "/gezi A B 2030-11-03 en geç 2030-11-04 12:00 üzerinden Yunanistan",
        ):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual({"arrive_by": result["arrive_by"], "via": result["via"]}, expected)

    def test_quoted_multiword_city_and_dash(self):
        result = self.parse('/gezi "New York" Londra 3.11')
        self.assertEqual((result["origin"], result["destination"]), ("New York", "Londra"))
        self.assertEqual(self.parse("/gezi New-York Londra 3.11")["origin"], "New York")

    def test_missing_parts_give_specific_message(self):
        self.assertIn("Kullanım", self.parse("/gezi")["error"])
        self.assertIn("nereye", self.parse("/gezi Atina")["error"])
        self.assertIn("tarih", self.parse("/gezi Atina Prag")["error"])

    def test_invalid_pieces_give_errors(self):
        invalid = [
            "/gezi Atina Prag 31.02",
            "/gezi Atina Prag bir gün",
            "/gezi Atina Atina 28.10",
            "/gezi Atina Prag 28.10 hızlı",
            "/gezi Atina Prag 28.10 üzerinden",
            "/gezi Atina Prag 28.10 üzerinden Yun4nistan",
            "/gezi Atina Prag 28.10 en geç 28.10 25:00",
            "/gezi Atina Prag 28.10 en geç 28.10 12:00 en geç 28.10 13:00",
            '/gezi "Atina Prag 28.10',
        ]
        for text in invalid:
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertIsNotNone(result)
                self.assertIn("error", result)


class TakipAndSilParseTests(unittest.TestCase):
    def test_takip_budgets(self):
        self.assertEqual(parse_takip_command("/takip 9500 6500"),
                         {"direct_budget_try": 9500.0, "side_budget_try": 6500.0})
        self.assertEqual(parse_takip_command("/takip 9500")["side_budget_try"], None)

    def test_takip_budget_formats(self):
        self.assertEqual(parse_takip_command("/takip 9.500")["direct_budget_try"], 9500.0)
        self.assertEqual(parse_takip_command("/takip 9.500,50 6500₺")["direct_budget_try"], 9500.5)
        self.assertEqual(parse_takip_command("/takip 9500 6.500")["side_budget_try"], 6500.0)
        self.assertEqual(parse_takip_command("/takip 9500TL")["direct_budget_try"], 9500.0)

    def test_takip_errors(self):
        for text in ("/takip", "/takip abc", "/takip 0", "/takip -5", "/takip 9500 abc", "/takip 1 2 3"):
            with self.subTest(text=text):
                self.assertIn("error", parse_takip_command(text))

    def test_takip_other_text_is_none(self):
        self.assertIsNone(parse_takip_command("/liste"))

    def test_sil_numbers(self):
        self.assertEqual(parse_sil_command("/sil 2"), {"index": 2})
        self.assertEqual(parse_sil_command("/sil #3"), {"index": 3})
        for text in ("/sil", "/sil x", "/sil 0", "/sil -1", "/sil 1.5"):
            with self.subTest(text=text):
                self.assertIn("error", parse_sil_command(text))
        self.assertIsNone(parse_sil_command("/liste"))


class FormatTripResultsTests(unittest.TestCase):
    def test_sections_and_flags(self):
        query = {"origin": "Atina", "destination": "Prag", "depart_dates": ["2026-10-27", "2026-10-28"],
                 "arrive_by": "2026-10-28T12:00", "via": ["Yunanistan"]}
        result = _FakePlanner().result
        text = format_trip_results(query, result)
        self.assertIn("27.10.2026–28.10.2026", text)
        self.assertIn("en geç 28.10.2026 12:00", text)
        self.assertIn("üzerinden Yunanistan", text)
        self.assertIn("En iyi direkt", text)
        self.assertIn("En iyi yan rota", text)
        self.assertIn("Öneriler", text)
        self.assertIn("✈️ Wizz Air ATH→BTS 23:50–01:05", text)
        self.assertIn("🚆 Bratislava→Praha 06:00–10:20", text)
        self.assertNotIn("Airport Ground Transit", text)  # transfer legs are not shown in the chain
        self.assertIn("🌙 gece beklemesi", text)
        self.assertIn("⚠️ ayrı bilet", text)
        self.assertIn("⚠️ tahmini", text)
        self.assertIn("+1 gün", text)
        self.assertIn("<code>+300 ₺</code> tasarruf", text)  # direct 2100 vs side 1800
        self.assertIn("kurallara uyan seçenek bulunamadı", text)

    def test_three_options_at_most(self):
        result = {"options": [_side_option() for _ in range(6)], "best_direct": None,
                  "best_side_route": None, "warnings": []}
        text = format_trip_results({"origin": "A", "destination": "B", "depart_dates": ["2026-10-28"]}, result)
        self.assertIn("<b>3.</b>", text)
        self.assertNotIn("<b>4.</b>", text)
        self.assertIn("Direkt seçenek bulunamadı", text)
        self.assertIn("Yan rota seçeneği bulunamadı", text)

    def test_names_are_escaped(self):
        option = _direct_option()
        option["legs"][0]["operator"] = "<b>Evil</b>"
        text = format_trip_results({"origin": "A<", "destination": "B", "depart_dates": ["2026-10-28"]},
                                   {"options": [option], "best_direct": option})
        self.assertIn("&lt;b&gt;Evil&lt;/b&gt;", text)
        self.assertNotIn("<b>Evil</b>", text)


class TripCommandBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tracker = PriceTracker(storage_path=os.path.join(self.tmp.name, "watches.json"))
        self.planner = _FakePlanner()
        self.notifier = _StubNotifier()
        self.provider = _StubProvider(flights=[_direct()])
        self.router = _StubRouter()
        self.bot = TelegramCommandBot(
            self.notifier, self.provider, self.router,
            tracker=self.tracker, trip_planner=self.planner,
        )

    def send(self, text, chat_id=int(OWNER)):
        self.bot.handle_update(_update(text, chat_id=chat_id))

    def last_reply(self):
        return self.notifier.sent[-1]

    # /gezi

    def test_gezi_runs_planner_and_replies_with_progress_and_result(self):
        self.send("/gezi Atina Prag 2030-11-03-2030-11-04 en geç 2030-11-04 12:00 üzerinden Yunanistan")
        self.assertEqual(self.planner.calls, [{
            "origin": "Atina",
            "destination": "Prag",
            "depart_dates": ["2030-11-03", "2030-11-04"],
            "arrive_by": "2030-11-04T12:00",
            "via": ["Yunanistan"],
            "include_side_routes": True,
        }])
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertIn("Planlanıyor", self.notifier.sent[0])
        self.assertIn("En iyi yan rota", self.notifier.sent[1])
        self.assertFalse(self.bot.is_busy)

    def test_gezi_parse_error_replies_usage_without_planning(self):
        self.send("/gezi Atina")
        self.assertEqual(self.planner.calls, [])
        self.assertIn("nereye", self.last_reply())
        self.assertIn("Kullanım", self.last_reply())

    def test_gezi_busy_rejects_concurrent_search(self):
        self.assertTrue(self.bot._search_lock.acquire(blocking=False))
        try:
            self.send("/gezi Atina Prag 2030-11-03")
        finally:
            self.bot._search_lock.release()
        self.assertEqual(self.planner.calls, [])
        self.assertEqual(self.notifier.sent, ["Önceki arama sürüyor, lütfen bekleyin."])

    def test_gezi_without_planner_is_unavailable(self):
        self.bot.trip_planner = None
        self.send("/gezi Atina Prag 2030-11-03")
        self.assertIn("kullanılamıyor", self.last_reply())

    def test_gezi_planner_failure_releases_busy_flag(self):
        self.planner.plan_trip = MagicMock(side_effect=RuntimeError("boom"))
        self.send("/gezi Atina Prag 2030-11-03")
        self.assertIn("hata", self.last_reply())
        self.assertFalse(self.bot.is_busy)

    def test_foreign_chat_cannot_plan_or_watch(self):
        self.send("/gezi Atina Prag 2030-11-03", chat_id=999)
        self.send("/takip 9500", chat_id=999)
        self.send("/liste", chat_id=999)
        self.send("/sil 1", chat_id=999)
        self.send("/durum", chat_id=999)
        self.assertEqual(self.planner.calls, [])
        self.assertEqual(self.tracker.watches, {})
        self.assertEqual(self.notifier.sent, [])

    # /takip

    def test_takip_without_prior_search_gives_helpful_error(self):
        self.send("/takip 9500 6500")
        self.assertIn("Önce /gezi veya /ara", self.last_reply())
        self.assertEqual(self.tracker.watches, {})

    def test_takip_after_gezi_creates_trip_watch(self):
        self.send("/gezi Atina Prag 2030-11-03-2030-11-04 en geç 2030-11-04 12:00 üzerinden Yunanistan")
        self.send("/takip 9500 6500")
        watches = list(self.tracker.watches.values())
        self.assertEqual(len(watches), 1)
        watch = watches[0]
        self.assertEqual((watch.origin, watch.destination), ("Atina", "Prag"))
        self.assertEqual(watch.trip_query, {
            "depart_dates": ["2030-11-03", "2030-11-04"],
            "arrive_by": "2030-11-04T12:00",
            "depart_after": None,
            "via": ["Yunanistan"],
            "include_side_routes": True,
        })
        self.assertEqual(watch.departure_date, "2030-11-03")
        self.assertEqual(watch.target_budget_try, 6500.0)
        self.assertEqual(watch.direct_budget_try, 9500.0)
        self.assertTrue(watch.include_multimodal)
        self.assertIn("Takip oluşturuldu", self.last_reply())
        self.assertIn("<b>1.</b>", self.last_reply())

    def test_takip_after_ara_uses_single_date_and_direct_budget_for_side(self):
        self.send("/ara Atina Prag 2030-11-03")
        self.send("/takip 9500")
        watch = next(iter(self.tracker.watches.values()))
        self.assertEqual(watch.trip_query["depart_dates"], ["2030-11-03"])
        self.assertIsNone(watch.trip_query["arrive_by"])
        self.assertIsNone(watch.trip_query["via"])
        self.assertEqual(watch.target_budget_try, 9500.0)
        self.assertEqual(watch.direct_budget_try, 9500.0)

    def test_takip_uses_latest_query_per_chat(self):
        self.send("/ara Atina Prag 2030-11-03")
        self.send("/gezi Atina Londra 2030-12-01")
        self.send("/takip 4000")
        watch = next(iter(self.tracker.watches.values()))
        self.assertEqual((watch.origin, watch.destination), ("Atina", "Londra"))

    def test_failed_gezi_parse_keeps_previous_query(self):
        self.send("/ara Atina Prag 2030-11-03")
        self.send("/gezi Atina")  # invalid, must not replace the remembered query
        self.send("/takip 4000")
        watch = next(iter(self.tracker.watches.values()))
        self.assertEqual(watch.destination, "Prag")

    def test_takip_invalid_budget_replies_error(self):
        self.send("/ara Atina Prag 2030-11-03")
        self.send("/takip abc")
        self.assertIn("Direkt bütçe anlaşılamadı", self.last_reply())
        self.assertEqual(self.tracker.watches, {})

    # /liste, /sil

    def _add(self, wid, origin, destination, **kwargs):
        return self.tracker.add_watch(origin, destination, target_budget_try=kwargs.pop("target", 5000.0),
                                      watch_id=wid, **kwargs)

    def test_liste_numbers_active_watches_only(self):
        self._add("w1", "Atina", "Prag", departure_date="2030-11-03")
        inactive = self._add("w2", "Istanbul", "Viyana", departure_date="2030-11-05")
        inactive.active = False
        self._add("w3", "Atina", "Londra", departure_date="2030-12-01", direct_budget_try=9000.0,
                  trip_query={"depart_dates": ["2030-12-01", "2030-12-02"], "arrive_by": "2030-12-02T10:00",
                              "via": ["Yunanistan"], "include_side_routes": True})
        self.tracker.get_watch("w3").add_reading(8200.0)
        self.send("/liste")
        reply = self.last_reply()
        self.assertIn("<b>1.</b> Atina ➔ Prag", reply)
        self.assertIn("<b>2.</b> Atina ➔ Londra", reply)
        self.assertNotIn("Viyana", reply)
        self.assertIn("03.11.2030", reply)
        self.assertIn("en geç 02.12.2030 10:00", reply)
        self.assertIn("üzerinden Yunanistan", reply)
        self.assertIn("direkt ≤ 9,000 ₺", reply)
        self.assertIn("son fiyat: 8,200 ₺", reply)

    def test_liste_empty(self):
        self.send("/liste")
        self.assertIn("Aktif takip yok", self.last_reply())

    def test_sil_deletes_the_numbered_watch(self):
        self._add("w1", "Atina", "Prag", departure_date="2030-11-03")
        self._add("w2", "Atina", "Londra", departure_date="2030-11-04")
        self._add("w3", "Atina", "Roma", departure_date="2030-11-05")
        self.send("/sil 2")
        self.assertEqual(set(self.tracker.watches), {"w1", "w3"})
        self.assertIn("Takip 2 silindi", self.last_reply())
        self.assertIn("Londra", self.last_reply())

    def test_sil_out_of_range_and_bad_input(self):
        self._add("w1", "Atina", "Prag", departure_date="2030-11-03")
        self.send("/sil 5")
        self.assertIn("Geçersiz takip numarası: 5", self.last_reply())
        self.send("/sil abc")
        self.assertIn("Kullanım", self.last_reply())
        self.assertEqual(set(self.tracker.watches), {"w1"})

    def test_takip_then_liste_then_sil_round_trip(self):
        self.send("/ara Atina Prag 2030-11-03")
        self.send("/takip 9500")
        self.send("/sil 1")
        self.assertEqual(self.tracker.watches, {})

    # /durum

    def test_durum_with_health_provider(self):
        self._add("w1", "Atina", "Prag", departure_date="2030-11-03")
        self.tracker.get_watch("w1").add_reading(7000.0, datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc))
        health = _FakeHealth({"healthy": False, "last_status": "captcha",
                              "consecutive_failures": 3,
                              "last_scan_at": datetime(2030, 1, 2, 9, 30, tzinfo=timezone.utc)})
        self.bot.health_provider = health
        self.send("/durum")
        reply = self.last_reply()
        self.assertIn("Takip: 1 aktif (1 kayıtlı)", reply)
        self.assertIn("Sağlık: sorunlu", reply)
        self.assertIn("Son durum: captcha", reply)
        self.assertIn("Art arda hata: 3", reply)
        self.assertIn("Son tarama:", reply)

    def test_durum_healthy_and_derived_last_reading(self):
        self._add("w1", "Atina", "Prag", departure_date="2030-11-03")
        self.tracker.get_watch("w1").add_reading(7000.0, datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc))
        self.bot.health_provider = _FakeHealth({"healthy": True, "last_status": "ok", "consecutive_failures": 0})
        self.send("/durum")
        reply = self.last_reply()
        self.assertIn("Sağlık: sağlıklı", reply)
        self.assertIn("Son fiyat kaydı:", reply)

    def test_durum_without_health_provider_or_watches(self):
        self.send("/durum")
        reply = self.last_reply()
        self.assertIn("Sağlık bilgisi yok", reply)
        self.assertIn("Takip: 0 aktif (0 kayıtlı)", reply)
        self.assertIn("Son tarama: bilinmiyor", reply)

    def test_durum_health_error_is_reported(self):
        self.bot.health_provider = _FakeHealth(error=RuntimeError("down"))
        self.send("/durum")
        self.assertIn("Sağlık bilgisi alınamadı", self.last_reply())

    def test_durum_shows_busy_state(self):
        self.assertTrue(self.bot._search_lock.acquire(blocking=False))
        try:
            self.send("/durum")
        finally:
            self.bot._search_lock.release()
        self.assertIn("Arama: meşgul", self.last_reply())

    # help and wiring

    def test_help_lists_all_commands(self):
        self.send("/yardim")
        reply = self.last_reply()
        for command in ("/ara", "/gezi", "/takip", "/liste", "/sil", "/durum"):
            with self.subTest(command=command):
                self.assertIn(command, reply)

    def test_commands_without_tracker_reply_unavailable(self):
        bot = TelegramCommandBot(self.notifier, self.provider, self.router)
        bot.handle_update(_update("/liste"))
        bot.handle_update(_update("/takip 9500"))
        self.assertEqual(self.notifier.sent.count("Takip servisi kullanılamıyor."), 2)

    def test_durum_works_without_tracker(self):
        bot = TelegramCommandBot(self.notifier, self.provider, self.router)
        bot.handle_update(_update("/durum"))
        self.assertIn("Takip servisi yok", self.notifier.sent[-1])

    def test_existing_ara_still_works_with_new_wiring(self):
        self.send("/ara Atina Prag 2030-11-03")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.tracker.watches, {})


class GeziRouteOptionTokenTests(unittest.TestCase):
    """Optional /gezi tokens for the route engine: durak, bütçe, esnek, dönüş, bagajsız, valiz."""

    def parse(self, text):
        return parse_gezi_command(text, TODAY)

    def test_plain_query_has_no_option_keys(self):
        result = self.parse("/gezi Atina Prag 27.10")
        self.assertEqual(set(result), {"origin", "destination", "depart_dates", "arrive_by", "via"})

    def test_durak_budget_and_flex(self):
        result = self.parse("/gezi Atina Prag 27.10 durak 1 bütçe 40 esnek 2")
        self.assertEqual((result["max_stops"], result["flight_search_budget"], result["flex_days"]), (1, 40, 2))

    def test_budget_spelling_without_umlaut_and_case(self):
        self.assertEqual(self.parse("/gezi A B 27.10 Butce 5")["flight_search_budget"], 5)
        self.assertEqual(self.parse("/gezi A B 27.10 BÜTÇE 60")["flight_search_budget"], 60)

    def test_out_of_range_values_are_errors(self):
        cases = {
            "/gezi A B 27.10 durak 4": "durak",
            "/gezi A B 27.10 bütçe 0": "bütçe",
            "/gezi A B 27.10 bütçe 61": "bütçe",
            "/gezi A B 27.10 esnek 4": "esnek",
            "/gezi A B 27.10 durak": "durak",
            "/gezi A B 27.10 durak x": "durak",
        }
        for text, word in cases.items():
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertIn("error", result)
                self.assertIn(word, result["error"])
                self.assertIn("Kullanım", result["error"])

    def test_repeated_option_is_error(self):
        self.assertIn("error", self.parse("/gezi A B 27.10 durak 1 durak 2"))
        self.assertIn("error", self.parse("/gezi A B 27.10 bütçe 10 bütçe 20"))

    def test_via_stops_before_option_words(self):
        result = self.parse("/gezi Atina Prag 27.10 üzerinden Yunanistan durak 1 bagajsız")
        self.assertEqual(result["via"], ["Yunanistan"])
        self.assertEqual(result["max_stops"], 1)
        self.assertFalse(result["cabin_bag"])

    def test_return_date_range(self):
        result = self.parse("/gezi Atina Prag 27.10 dönüş 30.10-01.11")
        self.assertEqual(result["return_dates"], ["2026-10-30", "2026-10-31", "2026-11-01"])
        self.assertNotIn("return_arrive_by", result)

    def test_return_arrive_by(self):
        result = self.parse("/gezi Atina Prag 27.10 dönüş 30.10 dönüş en geç 30.10 18:00")
        self.assertEqual(result["return_dates"], ["2026-10-30"])
        self.assertEqual(result["return_arrive_by"], "2026-10-30T18:00")

    def test_return_arrive_by_needs_a_return_date(self):
        result = self.parse("/gezi Atina Prag 27.10 dönüş en geç 30.10 18:00")
        self.assertIn("error", result)
        self.assertIn("dönüş tarihi", result["error"])

    def test_return_before_departure_is_error(self):
        result = self.parse("/gezi Atina Prag 2026-10-27 dönüş 2026-10-20")
        self.assertIn("error", result)
        self.assertIn("gidiş tarihinden önce", result["error"])

    def test_return_without_date_is_error(self):
        self.assertIn("error", self.parse("/gezi Atina Prag 27.10 dönüş"))

    def test_bag_options(self):
        self.assertFalse(self.parse("/gezi A B 27.10 bagajsız")["cabin_bag"])
        self.assertFalse(self.parse("/gezi A B 27.10 bagajsiz")["cabin_bag"])
        self.assertTrue(self.parse("/gezi A B 27.10 valiz")["checked_bag"])

    def test_unknown_word_is_still_an_error(self):
        self.assertIn("error", self.parse("/gezi A B 27.10 hızlı"))


class FormatRouteOptionTests(unittest.TestCase):
    QUERY = {"origin": "Atina", "destination": "Prag", "depart_dates": ["2026-10-27"]}

    def _result(self, **extra):
        option = _side_option()
        option["risk"] = {"level": "low", "score": 0, "reasons": [], "connections": []}
        base = {"options": [option], "best_direct": None, "best_side_route": option,
                "cheapest_direct": None, "cheapest_side_route": option, "warnings": []}
        base.update(extra)
        return base

    def test_risk_emoji_and_true_total_label(self):
        text = format_trip_results(self.QUERY, self._result())
        self.assertIn("🟢 düşük risk", text)
        self.assertIn("gerçek toplam", text)

    def test_high_and_medium_risk_emoji(self):
        high = _side_option()
        high["risk"] = {"level": "high"}
        medium = _side_option()
        medium["risk"] = {"level": "medium"}
        text = format_trip_results(self.QUERY, self._result(options=[high, medium]))
        self.assertIn("🔴 yüksek risk", text)
        self.assertIn("🟡 orta risk", text)

    def test_stopover_and_station_names(self):
        option = _side_option()
        option["stopover"] = {"place": "Selanik", "mins": 480}
        option["legs"][2]["origin_station_name"] = "Bratislava hl.st."  # the train leg (legs[1] is a transfer)
        option["legs"][2]["dest_station_name"] = "Praha hl.n."
        text = format_trip_results(self.QUERY, self._result(options=[option]))
        self.assertIn("⏸ Selanik 8 sa 0 dk kalış", text)
        self.assertIn("🚆 Bratislava hl.st. → Praha hl.n. 06:00–10:20", text)

    def test_canonical_station_names_show_for_ground_legs(self):
        option = _side_option()
        option["legs"][2]["origin_station_name"] = "Wien Hbf"
        option["legs"][2]["dest_station_name"] = "Praha hl.n."
        text = format_trip_results(self.QUERY, self._result(options=[option]))
        self.assertIn("🚆 Wien Hbf → Praha hl.n.", text)
        self.assertNotIn("Bratislava hl.st.", text)

    def test_legacy_station_keys_are_still_read(self):
        option = _side_option()
        option["legs"][2]["from_station_name"] = "Vienna - Hbf"
        option["legs"][2]["to_station_name"] = "Prague - Holesovice"
        text = format_trip_results(self.QUERY, self._result(options=[option]))
        self.assertIn("🚆 Vienna - Hbf → Prague - Holesovice", text)

    def test_ground_leg_without_station_names_keeps_city_names(self):
        option = _side_option()
        option["legs"][2]["origin_station_name"] = None
        option["legs"][2]["dest_station_name"] = None
        text = format_trip_results(self.QUERY, self._result(options=[option]))
        self.assertIn("🚆 Bratislava→Praha 06:00–10:20", text)

    def test_flight_leg_station_name_is_not_shown_as_a_station(self):
        option = _side_option()
        option["legs"][0]["origin_station_name"] = "ATH - Athens International Airport"
        option["legs"][0]["dest_station_name"] = "BTS - Bratislava"
        text = format_trip_results(self.QUERY, self._result(options=[option]))
        self.assertIn("✈️ Wizz Air ATH→BTS", text)
        self.assertNotIn("ATH - Athens", text)

    def _train_source(self, source):
        option = _side_option()
        option["legs"][2]["source"] = source
        return format_trip_results(self.QUERY, self._result(options=[option]))

    def test_ground_leg_source_tags(self):
        self.assertIn("🚆 Bratislava→Praha 06:00–10:20 (RegioJet)", self._train_source("live"))
        self.assertIn("🚆 Bratislava→Praha 06:00–10:20 (RegioJet)", self._train_source("regiojet"))
        self.assertIn("🚆 Bratislava→Praha 06:00–10:20 (FlixBus)", self._train_source("flixbus"))
        self.assertIn("🚆 Bratislava→Praha 06:00–10:20 (tahmini)", self._train_source("table"))
        self.assertIn("🚆 Bratislava→Praha 06:00–10:20 (tahmini)", self._train_source("estimate"))

    def test_ground_leg_without_source_has_no_tag(self):
        text = self._train_source(None)
        self.assertIn("🚆 Bratislava→Praha 06:00–10:20\n", text)
        self.assertNotIn("(RegioJet)", text)
        self.assertNotIn("(tahmini)", text)

    def test_source_tag_follows_station_names(self):
        option = _side_option()
        option["legs"][2]["source"] = "live"
        option["legs"][2]["origin_station_name"] = "Wien Hbf"
        option["legs"][2]["dest_station_name"] = "Praha hl.n."
        text = format_trip_results(self.QUERY, self._result(options=[option]))
        self.assertIn("🚆 Wien Hbf → Praha hl.n. 06:00–10:20 (RegioJet)", text)

    def test_flight_and_transfer_legs_get_no_source_tag(self):
        option = _side_option()
        option["legs"][0]["source"] = "google_flights"
        option["legs"][1]["source"] = "estimate"
        text = format_trip_results(self.QUERY, self._result(options=[option]))
        # The chain has no tag on the flight, the transfer is not shown, and the train (no source) has none either
        self.assertIn("✈️ Wizz Air ATH→BTS 23:50–01:05 · 🚆 Bratislava→Praha 06:00–10:20\n", text)
        self.assertNotIn("(tahmini)", text)
        self.assertNotIn("(RegioJet)", text)

    def test_price_advice_line(self):
        advice = {"advice": "bekle", "confidence": "orta", "reasons": ["Son 7 günde fiyat düştü"]}
        text = format_trip_results(self.QUERY, self._result(advice=advice))
        self.assertIn("Fiyat tavsiyesi: <b>bekle</b> (güven: orta)", text)
        self.assertIn("Son 7 günde fiyat düştü", text)

    def test_flex_summary_line(self):
        flex = {"best_date": "2026-10-29", "best_price": 4000.0, "saving_vs_requested": 2000.0}
        text = format_trip_results(self.QUERY, self._result(flex=flex))
        self.assertIn("En ucuz tarih: <b>29.10.2026</b>", text)
        self.assertIn("2,000 ₺", text)

    def test_roundtrip_combinations(self):
        combo = {"total_try": 8500.0, "stay_hours": 116.0, "label": "Gidiş: Direkt uçuş + Dönüş: Direkt uçuş"}
        text = format_trip_results(self.QUERY, self._result(roundtrip={"return": {}, "combos": [combo]}))
        self.assertIn("Gidiş-dönüş kombinasyonları", text)
        self.assertIn("8,500 ₺", text)
        self.assertIn("kalış 116 sa", text)

    def test_roundtrip_without_combinations(self):
        text = format_trip_results(self.QUERY, self._result(roundtrip={"return": {}, "combos": []}))
        self.assertIn("kombinasyon bulunamadı", text)

    def test_budget_exhausted_note_suggests_deeper_budget(self):
        text = format_trip_results(self.QUERY, self._result(budget_exhausted=True,
                                                           query={"flight_search_budget": 20}))
        self.assertIn("bütçesi doldu", text)
        self.assertIn("<code>bütçe 40</code>", text)
        text = format_trip_results(self.QUERY, self._result(budget_exhausted=True,
                                                           query={"flight_search_budget": 40}))
        self.assertIn("<code>bütçe 60</code>", text)
        text = format_trip_results(self.QUERY, self._result(budget_exhausted=True,
                                                           query={"flight_search_budget": 60}))
        self.assertIn("en fazla (60)", text)

    def test_rule_header_lines(self):
        query = dict(self.QUERY, max_stops=1, flight_search_budget=30, flex_days=2, cabin_bag=False,
                     checked_bag=True, return_dates=["2026-10-30"], return_arrive_by="2026-10-30T18:00")
        text = format_trip_results(query, self._result())
        self.assertIn("durak 1", text)
        self.assertIn("bütçe 30", text)
        self.assertIn("esnek ±2 gün", text)
        self.assertIn("bagajsız", text)
        self.assertIn("valiz", text)
        self.assertIn("dönüş 30.10.2026 · en geç 30.10.2026 18:00", text)

    def test_plain_query_shows_no_rule_header(self):
        text = format_trip_results(self.QUERY, self._result())
        self.assertNotIn("⚙️", text)
        self.assertNotIn("🔁 dönüş", text)


class RouteOptionCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tracker = PriceTracker(storage_path=os.path.join(self.tmp.name, "watches.json"))
        self.planner = _FakePlanner()
        self.notifier = _StubNotifier()
        self.bot = TelegramCommandBot(
            self.notifier, _StubProvider(flights=[_direct()]), _StubRouter(),
            tracker=self.tracker, trip_planner=self.planner,
        )

    def send(self, text):
        self.bot.handle_update(_update(text))

    def test_gezi_forwards_route_options_to_the_planner(self):
        self.send("/gezi Atina Prag 2030-11-03 durak 1 bütçe 40 esnek 1 dönüş 2030-11-04 bagajsız")
        call = self.planner.calls[-1]
        self.assertEqual(call["max_stops"], 1)
        self.assertEqual(call["flight_search_budget"], 40)
        self.assertEqual(call["flex_days"], 1)
        self.assertEqual(call["return_dates"], ["2030-11-04"])
        self.assertFalse(call["cabin_bag"])
        self.assertNotIn("checked_bag", call)

    def test_gezi_without_options_sends_no_route_options(self):
        self.send("/gezi Atina Prag 2030-11-03")
        self.assertEqual(set(self.planner.calls[-1]), {"origin", "destination", "depart_dates", "arrive_by",
                                                       "via", "include_side_routes"})

    def test_takip_keeps_the_whole_query(self):
        self.send("/gezi Atina Prag 2030-11-03 durak 1 bütçe 40 esnek 1 dönüş 2030-11-04 dönüş en geç 2030-11-04 20:00 valiz")
        self.send("/takip 9500 6500")
        watch = next(iter(self.tracker.watches.values()))
        self.assertEqual(watch.trip_query["max_stops"], 1)
        self.assertEqual(watch.trip_query["flight_search_budget"], 40)
        self.assertEqual(watch.trip_query["flex_days"], 1)
        self.assertEqual(watch.trip_query["return_dates"], ["2030-11-04"])
        self.assertEqual(watch.trip_query["return_arrive_by"], "2030-11-04T20:00")
        self.assertTrue(watch.trip_query["checked_bag"])
        self.assertNotIn("cabin_bag", watch.trip_query)

    def test_budget_exhausted_reply_from_a_gezi_search(self):
        self.planner.result = dict(self.planner.result, budget_exhausted=True)
        self.send("/gezi Atina Prag 2030-11-03")
        self.assertIn("bütçe 40", self.notifier.sent[-1])

    def test_help_lists_the_new_tokens(self):
        self.send("/yardim")
        text = self.notifier.sent[-1]
        for token in ("durak 1", "bütçe 40", "esnek 2", "dönüş 30.10", "bagajsız", "valiz"):
            self.assertIn(token, text)




class LastServiceLegDisplayTests(unittest.TestCase):
    def test_option_block_with_last_service_and_next_dep(self):
        leg_with_fields = {
            "type": "train",
            "operator": "RegioJet",
            "from": "Viyana",
            "to": "Prag",
            "origin_station_name": "Wien Hbf",
            "dest_station_name": "Praha hl.n.",
            "depart_at": "2026-10-25T20:00",
            "arrive_at": "2026-10-25T22:30",
            "price_try": 500.0,
            "is_last_service": True,
            "next_departure_at": "2026-10-25T22:00",
        }
        option = {
            "kind": "direct",
            "true_total_try": 500.0,
            "duration_mins": 150,
            "legs": [leg_with_fields],
        }
        text = tb._option_block(1, option, None)
        self.assertIn(" · son sefer", text)
        self.assertIn(" · sonraki 22:00", text)

    def test_option_block_without_fields_is_unchanged(self):
        leg_without = {
            "type": "train",
            "operator": "RegioJet",
            "from": "Viyana",
            "to": "Prag",
            "origin_station_name": "Wien Hbf",
            "dest_station_name": "Praha hl.n.",
            "depart_at": "2026-10-25T20:00",
            "arrive_at": "2026-10-25T22:30",
            "price_try": 500.0,
        }
        option = {
            "kind": "direct",
            "true_total_try": 500.0,
            "duration_mins": 150,
            "legs": [leg_without],
        }
        text = tb._option_block(1, option, None)
        self.assertNotIn("son sefer", text)
        self.assertNotIn("sonraki", text)
        self.assertIn("🚆 Wien Hbf → Praha hl.n. 20:00–22:30", text)

    def test_segment_text_with_last_service_and_next_dep(self):
        seg = {
            "type": "bus",
            "operator": "FlixBus",
            "from": "Wien",
            "to": "Praha",
            "departure_time": "20:00",
            "arrival_time": "23:00",
            "is_last_service": True,
            "next_departure_at": "2026-10-25T22:00",
        }
        text = tb._segment_text(seg)
        self.assertIn(" · son sefer", text)
        self.assertIn(" · sonraki 22:00", text)

    def test_segment_text_without_fields_is_unchanged(self):
        seg = {
            "type": "bus",
            "operator": "FlixBus",
            "from": "Wien",
            "to": "Praha",
            "departure_time": "20:00",
            "arrival_time": "23:00",
        }
        text = tb._segment_text(seg)
        self.assertEqual(text, "🚌 Wien→Praha 20:00–23:00")

if __name__ == "__main__":
    unittest.main()
