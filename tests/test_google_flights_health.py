"""Health tracking, rate limiting, blocked cooldown, page classification and parser fixtures for
GoogleFlightsProvider. No network: sync_playwright is replaced by an in-memory fake, and time is
replaced by a fake clock inside the provider module."""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from travel_engine.providers import google_flights_provider as gfp
from travel_engine.flight_cache import FlightCache, route_key
from travel_engine.providers.google_flights_provider import GoogleFlightsProvider, classify_page
from travel_engine.search_log import SearchLog

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "google"
FLIGHTS_URL = "https://www.google.com/travel/flights?q=one-way%20flights%20from%20Athens%20to%20Prague&hl=tr&gl=tr&curr=TRY"
SORRY_URL = "https://www.google.com/sorry/index?continue=https://www.google.com/travel/flights"


def _load(name):
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


ATH_CARDS = _load("ath_prg_oneway_cards.json")["cards"]
NO_RESULTS = _load("tia_nuuk_no_results.json")
SYNTHETIC = _load("synthetic_cards.json")
SAW_CARDS = _load("saw_bkk_oneway_cards.json")["cards"]


def _find(cards, needle):
    matches = [c for c in cards if needle in c]
    assert matches, f"no fixture card contains {needle!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeElement:
    def __init__(self, text):
        self._text = text

    def inner_text(self):
        return self._text

    def get_attribute(self, _name):
        return None


class FakePage:
    def __init__(self, scenario):
        self.url = scenario["url"]
        self._body = scenario["body"]
        self._cards = scenario["cards"]

    def route(self, *_args, **_kwargs):
        pass

    def goto(self, _url, **_kwargs):
        pass

    def query_selector(self, _selector):
        return None  # no consent button, no date input, no expand button

    def query_selector_all(self, selector):
        return [FakeElement(c) for c in self._cards] if selector == "li.pIav2d" else []

    def wait_for_selector(self, _selector, **_kwargs):
        return None

    def wait_for_timeout(self, _ms):
        pass

    def inner_text(self, selector):
        return self._body if selector == "body" else ""

    def close(self):
        pass


class FakeSession:
    """Serves one scenario per browser launch; the last scenario repeats once the list is used up."""

    def __init__(self, clock):
        self.clock = clock
        self.scenarios = []
        self.launches = []
        self.launch_error = None

    def next_page(self):
        scenario = self.scenarios.pop(0) if len(self.scenarios) > 1 else self.scenarios[0]
        return FakePage(scenario)

    def sync_playwright(self):
        return _FakePlaywrightContext(self)


class _FakePlaywrightContext:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return SimpleNamespace(chromium=_FakeChromium(self._session))

    def __exit__(self, *_exc):
        return False


class _FakeChromium:
    def __init__(self, session):
        self._session = session

    def launch(self, **_kwargs):
        self._session.launches.append(self._session.clock.now)
        if self._session.launch_error is not None:
            raise self._session.launch_error
        return _FakeBrowser(self._session)


class _FakeBrowser:
    def __init__(self, session):
        self._session = session

    def new_context(self, **_kwargs):
        return _FakeContext(self._session)

    def close(self):
        pass


class _FakeContext:
    def __init__(self, session):
        self._session = session

    def new_page(self):
        return self._session.next_page()

    def close(self):
        pass


def ok_scenario(cards=None):
    return {"url": FLIGHTS_URL, "body": "Arama sonuçları\n5 sonuç bulundu.", "cards": cards or ATH_CARDS[:5]}


def empty_scenario():
    return {"url": FLIGHTS_URL, "body": "Ana içeriğe geç\nKeşfet\nUçuşlar", "cards": []}


def no_flights_scenario():
    return {"url": FLIGHTS_URL, "body": NO_RESULTS["body"], "cards": []}


def parse_error_scenario():
    return {"url": FLIGHTS_URL, "body": "Arama sonuçları", "cards": ["junk", "more junk"]}


def blocked_scenario():
    return {"url": SORRY_URL, "body": "", "cards": []}


class PlaywrightFakeTestCase(unittest.TestCase):
    def setUp(self):
        GoogleFlightsProvider.reset_shared_state()
        self.clock = FakeClock()
        self.session = FakeSession(self.clock)
        self.session.scenarios = [ok_scenario()]
        patchers = [
            mock.patch.object(gfp, "time", self.clock),
            mock.patch.object(gfp, "PLAYWRIGHT_AVAILABLE", True),
            mock.patch.object(gfp, "sync_playwright", self.session.sync_playwright),
            # These tests pin the launch-per-search behaviour; the shared browser has its own tests.
            mock.patch.dict(os.environ, {"TRAVELPLAN_BROWSER_REUSE": "0", "TRAVELPLAN_FLIGHT_CACHE": "off"}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(GoogleFlightsProvider.reset_shared_state)

    def make_provider(self, **kwargs):
        kwargs.setdefault("min_interval_secs", 0.0)
        return GoogleFlightsProvider(**kwargs)

    def search(self, provider, day=15, **kwargs):
        """One-way Athens -> Prague on 2026-12-<day>. A new date avoids the cache."""
        return provider.search_flights(
            origin="Athens",
            destination="Prague",
            departure_date=f"2026-12-{day:02d}",
            return_date=None,
            trip_type="oneway",
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Page classification
# ---------------------------------------------------------------------------


class ClassifyPageTests(unittest.TestCase):
    def test_parsed_flights_are_ok(self):
        self.assertEqual(classify_page(FLIGHTS_URL, "", 60, 60), "ok")

    def test_real_turkish_no_results_page_is_no_flights(self):
        self.assertEqual(classify_page(NO_RESULTS["url"], NO_RESULTS["body"], 0, 0), "no_flights")

    def test_english_no_results_text_is_no_flights(self):
        self.assertEqual(classify_page(FLIGHTS_URL, "No flights found for these dates", 0, 0), "no_flights")
        self.assertEqual(classify_page(FLIGHTS_URL, "No results", 0, 0), "no_flights")

    def test_turkish_no_results_text_is_case_insensitive(self):
        self.assertEqual(classify_page(FLIGHTS_URL, "UÇUŞ BULUNAMADI", 0, 0), "no_flights")
        self.assertEqual(classify_page(FLIGHTS_URL, "Hiçbir sonuç yok", 0, 0), "no_flights")

    def test_cards_without_parsed_flights_are_parse_error(self):
        self.assertEqual(classify_page(FLIGHTS_URL, "anything", 5, 0), "parse_error")

    def test_no_cards_and_no_text_is_empty_page(self):
        self.assertEqual(classify_page(FLIGHTS_URL, "Ana içeriğe geç\nKeşfet", 0, 0), "empty_page")

    def test_sorry_url_is_blocked(self):
        self.assertEqual(classify_page(SORRY_URL, "", 0, 0), "blocked")

    def test_block_text_is_blocked(self):
        self.assertEqual(classify_page(FLIGHTS_URL, SYNTHETIC["blocked_body"], 0, 0), "blocked")
        self.assertEqual(classify_page(FLIGHTS_URL, "Our systems have detected unusual traffic", 0, 0), "blocked")
        self.assertEqual(classify_page(FLIGHTS_URL, "Please solve the captcha", 0, 0), "blocked")

    def test_consent_wall_is_blocked(self):
        url = "https://consent.google.com/m?continue=https://www.google.com/travel/flights"
        self.assertEqual(classify_page(url, "", 0, 0), "blocked")

    def test_parsed_flights_win_over_block_text(self):
        self.assertEqual(classify_page(FLIGHTS_URL, "unusual traffic", 3, 3), "ok")


# ---------------------------------------------------------------------------
# Parser on captured and synthetic card texts
# ---------------------------------------------------------------------------


class ParserFixtureTests(unittest.TestCase):
    def setUp(self):
        self.provider = GoogleFlightsProvider()

    def parse(self, text):
        return self.provider._parse_flight_item(text, FLIGHTS_URL, "Tek Yön")

    def test_all_captured_athens_cards_parse(self):
        parsed = [self.parse(c) for c in ATH_CARDS]
        self.assertTrue(all(p is not None for p in parsed))

    def test_one_stop_with_layover_airport(self):
        flight = self.parse(ATH_CARDS[0])
        self.assertEqual(flight["airline"], "KLM")
        self.assertEqual(flight["times"], "13:35 – 19:30")
        self.assertEqual(flight["duration"], "6 sa. 55 dk.")
        self.assertEqual(flight["airports"], "ATH ➔ PRG")
        self.assertFalse(flight["is_direct"])
        self.assertEqual(flight["stops_count"], 1)
        self.assertEqual(flight["layover_info"], "2 sa. AMS (Amsterdam)")
        self.assertEqual(flight["total_price_try"], 4830.0)

    def test_direct_flight(self):
        flight = self.parse(ATH_CARDS[2])
        self.assertEqual(flight["airline"], "SKY express")
        self.assertEqual(flight["times"], "07:45 – 09:25")
        self.assertTrue(flight["is_direct"])
        self.assertEqual(flight["stops"], "Aktarmasız")
        self.assertEqual(flight["layover_info"], "")
        self.assertEqual(flight["total_price_try"], 7744.0)

    def test_next_day_arrival_and_layover_city(self):
        flight = self.parse(_find(ATH_CARDS, "Air Serbia"))
        self.assertEqual(flight["times"], "16:35 – 08:45+1")
        self.assertEqual(flight["airline"], "Air Serbia")
        self.assertEqual(flight["layover_info"], "13 sa. 20 dk. BEG (Belgrad)")
        self.assertEqual(flight["total_price_try"], 3902.0)

    def test_combined_airline_name_is_split(self):
        flight = self.parse(_find(SAW_CARDS, "flydubaiEmirates"))
        self.assertEqual(flight["airline"], "flydubai / Emirates")
        self.assertEqual(flight["airports"], "SAW ➔ DMK")

    def test_cards_without_price_are_skipped(self):
        no_price = [c for c in SAW_CARDS if "Fiyat yok" in c]
        self.assertEqual(len(no_price), 4)
        for card in no_price:
            self.assertIsNone(self.parse(card))

    def test_codeshare_and_turkish_price_formats(self):
        codeshare = self.parse(SYNTHETIC["cards"][0])
        self.assertEqual(codeshare["airline"], "Austrian / Aegean")
        self.assertEqual(codeshare["total_price_try"], 13281.0)
        self.assertTrue(codeshare["is_direct"])

        tl_format = self.parse(SYNTHETIC["cards"][1])
        self.assertEqual(tl_format["airline"], "Austrian")
        self.assertEqual(tl_format["total_price_try"], 13281.0)

    def test_synthetic_next_day_with_layover(self):
        flight = self.parse(SYNTHETIC["cards"][2])
        self.assertEqual(flight["times"], "22:05 – 06:35+1")
        self.assertEqual(flight["layover_info"], "1 sa. 40 dk. VIE (Viyana)")
        self.assertEqual(flight["total_price_try"], 3120.0)
        self.assertFalse(flight["is_direct"])


class NonstopDetectionTests(unittest.TestCase):
    """A card is direct only when it says so. A connection without stop text is never shown as non-stop (live QA:
    Pegasus ATH->BER 09:30->17:00, 7.5 h, was shown as direct). Durations above the non-stop limit are connections."""

    def setUp(self):
        self.provider = GoogleFlightsProvider()

    def parse(self, text):
        return self.provider._parse_flight_item(text, FLIGHTS_URL, "Tek Yön")

    def test_card_without_stop_text_is_not_direct(self):
        flight = self.parse(_card(("09:30", "12:40"), "Pegasus", "ATH–BER", 5000, duration="3 sa. 10 dk.", stops=""))
        self.assertFalse(flight["is_direct"])
        self.assertEqual(flight["stops"], "bilinmiyor")
        self.assertEqual(flight["stops_count"], 1)

    def test_live_qa_connection_without_stop_text_is_flagged(self):
        # ATH-BER non-stop limit is about 5.2 h (1797 km / 750 km/h + 0.5 h, x 1.8)
        flight = self.parse(_card(("09:30", "17:00"), "Pegasus", "ATH–BER", 5000, duration="7 sa. 30 dk.", stops=""))
        self.assertFalse(flight["is_direct"])
        self.assertEqual(flight["stops"], "Aktarmalı (tahmini)")

    def test_explicit_nonstop_with_implausible_duration_is_a_connection(self):
        flight = self.parse(_card(("09:30", "17:00"), "Pegasus", "ATH–BER", 5000, duration="7 sa. 30 dk.",
                                  stops="Aktarmasız"))
        self.assertFalse(flight["is_direct"])
        self.assertEqual(flight["stops"], "Aktarmalı (tahmini)")

    def test_explicit_nonstop_with_plausible_duration_stays_direct(self):
        flight = self.parse(_card(("09:30", "12:35"), "Pegasus", "ATH–BER", 5000, duration="3 sa. 5 dk.",
                                  stops="Aktarmasız"))
        self.assertTrue(flight["is_direct"])
        self.assertEqual(flight["stops"], "Aktarmasız")
        self.assertEqual(flight["stops_count"], 0)

    def test_english_nonstop_wording_is_direct(self):
        flight = self.parse(_card(("09:30", "11:10"), "Pegasus", "ATH–PRG", 5000, duration="2 sa. 40 dk.",
                                  stops="Nonstop"))
        self.assertTrue(flight["is_direct"])

    def test_stated_stop_count_is_kept_even_with_long_duration(self):
        flight = self.parse(_card(("09:30", "17:00"), "Pegasus", "ATH–BER", 5000, duration="7 sa. 30 dk.",
                                  stops="1 aktarma"))
        self.assertFalse(flight["is_direct"])
        self.assertEqual(flight["stops"], "1 aktarma")

    def test_duration_check_needs_both_airports_in_the_database(self):
        # BKK is outside AirportDB (Europe and Turkey only): no distance, so no duration check
        flight = self.parse(_card(("09:30", "17:00"), "Thai", "ATH–BKK", 5000, duration="19 sa.", stops=""))
        self.assertEqual(flight["stops"], "bilinmiyor")

    def test_duration_parsing(self):
        self.assertAlmostEqual(gfp.parse_duration_hours("2 sa. 40 dk."), 2 + 40 / 60)
        self.assertEqual(gfp.parse_duration_hours("13 sa."), 13.0)
        self.assertAlmostEqual(gfp.parse_duration_hours("55 dk."), 55 / 60)
        self.assertIsNone(gfp.parse_duration_hours("Bilinmiyor"))

    def test_captured_cards_are_direct_only_when_they_say_so(self):
        for card in ATH_CARDS + SAW_CARDS:
            flight = self.parse(card)
            if flight is None:
                continue
            if "Aktarmasız" not in card:
                self.assertFalse(flight["is_direct"], card)
            else:
                self.assertEqual(flight["is_direct"], flight["stops"] == "Aktarmasız", card)


# ---------------------------------------------------------------------------
# Health tracking through the live search path
# ---------------------------------------------------------------------------


class HealthTests(PlaywrightFakeTestCase):
    def test_ok_search_is_healthy_and_records_last_status(self):
        provider = self.make_provider()
        results = self.search(provider)
        self.assertEqual(len(results), 5)
        status = provider.last_search_status
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["count"], len(results))
        self.assertEqual(status["origin"], "Athens")
        self.assertEqual(status["destination"], "Prague")
        self.assertEqual(status["date"], "2026-12-15")
        self.assertTrue(status["at"].endswith("+00:00"))
        health = provider.health_status()
        self.assertTrue(health["healthy"])
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertIsNotNone(health["last_ok_at"])
        self.assertEqual(health["last_status"], "ok")

    def test_three_consecutive_failures_flip_unhealthy_and_callback_fires_once(self):
        callback = mock.Mock()
        provider = self.make_provider(on_health_change=callback)
        self.session.scenarios = [empty_scenario(), empty_scenario(), parse_error_scenario()]
        for day in (15, 16):
            self.search(provider, day=day)
            self.assertTrue(provider.health_status()["healthy"])
            callback.assert_not_called()

        self.search(provider, day=17)
        health = provider.health_status()
        self.assertFalse(health["healthy"])
        self.assertEqual(health["consecutive_failures"], 3)
        callback.assert_called_once()
        payload = callback.call_args[0][0]
        self.assertFalse(payload["healthy"])
        self.assertEqual(payload["consecutive_failures"], 3)

        # Further failures keep it unhealthy but do not fire the callback again.
        self.search(provider, day=18)
        self.search(provider, day=19)
        self.assertEqual(provider.health_status()["consecutive_failures"], 5)
        callback.assert_called_once()

        # Recovery flips back once.
        self.session.scenarios = [ok_scenario()]
        self.search(provider, day=20)
        self.assertEqual(callback.call_count, 2)
        self.assertTrue(callback.call_args[0][0]["healthy"])
        self.assertEqual(provider.health_status()["consecutive_failures"], 0)

    def test_no_flights_is_a_successful_read(self):
        callback = mock.Mock()
        provider = self.make_provider(on_health_change=callback)
        self.session.scenarios = [no_flights_scenario()]
        for day in (15, 16, 17, 18):
            self.assertEqual(self.search(provider, day=day), [])
        health = provider.health_status()
        self.assertTrue(health["healthy"])
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertIsNotNone(health["last_ok_at"])
        self.assertEqual(provider.last_search_status["status"], "no_flights")
        callback.assert_not_called()

    def test_success_resets_failure_streak(self):
        provider = self.make_provider()
        self.session.scenarios = [empty_scenario(), empty_scenario(), ok_scenario()]
        for day in (15, 16, 17):
            self.search(provider, day=day)
        self.assertEqual(provider.health_status()["consecutive_failures"], 0)
        self.assertEqual(provider.health_status()["last_status"], "ok")

    def test_blocked_counts_as_failure_and_starts_cooldown(self):
        provider = self.make_provider()
        self.session.scenarios = [blocked_scenario()]
        self.assertEqual(self.search(provider), [])
        self.assertEqual(provider.last_search_status["status"], "blocked")
        self.assertEqual(provider.health_status()["consecutive_failures"], 1)
        self.assertEqual(provider.health_status()["blocked_cooldown_remaining_secs"], 900.0)

    def test_cooldown_refuses_live_searches_without_launching(self):
        provider = self.make_provider()
        self.session.scenarios = [blocked_scenario()]
        self.search(provider, day=15)
        self.assertEqual(len(self.session.launches), 1)

        self.assertEqual(self.search(provider, day=16), [])
        self.assertEqual(len(self.session.launches), 1)  # no browser was launched
        self.assertEqual(provider.last_search_status["status"], "blocked_cooldown")
        # The cooldown refusal is not a new failure.
        self.assertEqual(provider.health_status()["consecutive_failures"], 1)

    def test_cooldown_expires_after_configured_seconds(self):
        provider = self.make_provider(blocked_cooldown_secs=900)
        self.session.scenarios = [blocked_scenario()]
        self.search(provider, day=15)
        self.clock.now += 899
        self.assertEqual(self.search(provider, day=16), [])
        self.assertEqual(provider.last_search_status["status"], "blocked_cooldown")

        self.clock.now += 2
        self.session.scenarios = [ok_scenario()]
        self.assertEqual(len(self.search(provider, day=17)), 5)
        self.assertEqual(provider.last_search_status["status"], "ok")
        self.assertEqual(len(self.session.launches), 2)

    def test_cooldown_is_shared_across_instances(self):
        first = self.make_provider()
        second = self.make_provider()
        self.session.scenarios = [blocked_scenario()]
        self.search(first, day=15)
        self.assertEqual(self.search(second, day=16), [])
        self.assertEqual(second.last_search_status["status"], "blocked_cooldown")

    def test_launch_failure_is_error_and_counts_as_failure(self):
        provider = self.make_provider()
        self.session.launch_error = RuntimeError("Target closed")
        self.assertEqual(self.search(provider), [])
        self.assertEqual(provider.last_search_status["status"], "error")
        self.assertIn("RuntimeError", provider.last_search_status["detail"])
        self.assertEqual(provider.health_status()["consecutive_failures"], 1)

    def test_playwright_missing_is_unavailable_and_not_a_failure(self):
        provider = self.make_provider()
        with mock.patch.object(gfp, "PLAYWRIGHT_AVAILABLE", False):
            self.assertEqual(self.search(provider), [])
        self.assertEqual(provider.last_search_status["status"], "unavailable")
        self.assertEqual(provider.health_status()["consecutive_failures"], 0)
        self.assertEqual(len(self.session.launches), 0)

    def test_cache_hits_do_not_affect_health(self):
        provider = self.make_provider()
        first = self.search(provider, day=15)
        second = self.search(provider, day=15)
        self.assertEqual(first, second)
        self.assertEqual(len(self.session.launches), 1)
        self.assertEqual(len(provider.health_status()["recent"]), 1)

    def test_callback_exceptions_are_swallowed(self):
        provider = self.make_provider(on_health_change=mock.Mock(side_effect=RuntimeError("telegram down")))
        self.session.scenarios = [empty_scenario()]
        for day in (15, 16, 17):
            self.assertEqual(self.search(provider, day=day), [])
        self.assertFalse(provider.health_status()["healthy"])

    def test_health_status_is_json_friendly(self):
        provider = self.make_provider()
        self.search(provider)
        payload = json.loads(json.dumps(provider.health_status()))
        self.assertEqual(set(payload["recent"]), {"ok"})
        self.assertEqual(payload["last_search"]["status"], "ok")

    def test_recent_keeps_last_twenty_statuses(self):
        provider = self.make_provider()
        self.session.scenarios = [no_flights_scenario()]
        for day in range(1, 26):
            self.search(provider, day=day)
        self.assertEqual(len(provider.health_status()["recent"]), 20)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class RateLimitTests(PlaywrightFakeTestCase):
    def test_wait_for_launch_slot_spaces_launches(self):
        launch_times = []
        for _ in range(3):
            GoogleFlightsProvider._wait_for_launch_slot(2.0)
            launch_times.append(self.clock.now)
        self.assertEqual(launch_times, [1000.0, 1002.0, 1004.0])

    def test_no_wait_when_interval_is_zero(self):
        for _ in range(3):
            GoogleFlightsProvider._wait_for_launch_slot(0.0)
        self.assertEqual(self.clock.sleeps, [])

    def test_live_searches_are_spaced_by_min_interval(self):
        provider = self.make_provider(min_interval_secs=2.0)
        self.session.scenarios = [ok_scenario()]
        for day in (15, 16, 17):
            self.search(provider, day=day)
        self.assertEqual(self.session.launches, [1000.0, 1002.0, 1004.0])

    def test_cache_hit_does_not_wait_for_a_slot(self):
        provider = self.make_provider(min_interval_secs=2.0)
        self.search(provider, day=15)
        before = list(self.clock.sleeps)
        self.search(provider, day=15)
        self.assertEqual(self.clock.sleeps, before)



class RealTimeRateLimitTests(unittest.TestCase):
    """Concurrency check against the real clock (no fake time)."""

    def setUp(self):
        GoogleFlightsProvider.reset_shared_state()
        self.addCleanup(GoogleFlightsProvider.reset_shared_state)

    def test_concurrent_threads_are_spaced_in_real_time(self):
        stamps = []
        stamps_lock = threading.Lock()

        def worker():
            GoogleFlightsProvider._wait_for_launch_slot(0.05)
            with stamps_lock:
                stamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertEqual(len(stamps), 4)
        self.assertTrue(all(gap >= 0.04 for gap in gaps), gaps)


# ---------------------------------------------------------------------------
# Search log integration
# ---------------------------------------------------------------------------


class SearchLogIntegrationTests(PlaywrightFakeTestCase):
    def setUp(self):
        super().setUp()
        self.log = SearchLog(":memory:")
        self.addCleanup(self.log.close)

    def test_live_search_is_recorded_with_status_count_and_min_price(self):
        provider = self.make_provider(search_log=self.log)
        results = self.search(provider, day=15)
        rows = self.log.recent()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["origin"], "ATH")  # Athens -> IATA 'ath'
        self.assertEqual(row["destination"], "PRG")
        self.assertEqual(row["date"], "2026-12-15")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["count"], len(results))
        self.assertEqual(row["min_price"], min(r["total_price_try"] for r in results))
        self.assertEqual(row["source"], "google_flights")

    def test_failure_statuses_are_recorded(self):
        provider = self.make_provider(search_log=self.log)
        self.session.scenarios = [no_flights_scenario(), parse_error_scenario()]
        self.search(provider, day=15)
        self.search(provider, day=16)
        statuses = [r["status"] for r in self.log.recent()]
        self.assertEqual(statuses, ["parse_error", "no_flights"])
        self.assertIsNone(self.log.recent()[0]["min_price"])

    def test_cache_hits_are_not_recorded(self):
        provider = self.make_provider(search_log=self.log)
        self.search(provider, day=15)
        self.search(provider, day=15)
        self.assertEqual(len(self.log.recent()), 1)

    def test_roundtrip_uses_separate_source_and_keeps_return_date(self):
        provider = self.make_provider(search_log=self.log)
        provider.search_flights(
            origin="Athens",
            destination="Prague",
            departure_date="2026-12-15",
            return_date="2026-12-20",
            trip_type="roundtrip",
        )
        row = self.log.recent()[0]
        self.assertEqual(row["source"], "google_flights_roundtrip")
        self.assertIn("return=2026-12-20", row["detail"])
        self.assertEqual(self.log.pair_stats("ATH", "PRG")["searches"], 0)

    def test_log_failure_does_not_break_search(self):
        broken = mock.Mock()
        broken.record.side_effect = RuntimeError("disk full")
        provider = self.make_provider(search_log=broken)
        self.assertEqual(len(self.search(provider)), 5)
        self.assertEqual(provider.last_search_status["status"], "ok")



# ---------------------------------------------------------------------------
# Airline names on real-looking card texts (protected brand names)
# ---------------------------------------------------------------------------


def _card(times, airline, pair, price, duration="2 sa. 10 dk.", stops="Aktarmasız"):
    """A result card in the layout of the captured fixtures (times, airline, duration, pair, stops, price)."""
    start, end = times
    lines = [start, "–", end, airline, duration, pair, stops, "95 kg CO2e", f"₺{price}"]
    return "\n".join(lines)


class ProtectedAirlineNameTests(unittest.TestCase):
    def setUp(self):
        self.provider = GoogleFlightsProvider()

    def parse(self, text):
        return self.provider._parse_flight_item(text, FLIGHTS_URL, "Tek Yön")

    def test_brand_names_stay_intact(self):
        cases = {
            "easyJet": "easyJet",
            "SunExpress": "SunExpress",
            "AJet": "AJet",
            "airBaltic": "airBaltic",
            "EgyptAir": "EgyptAir",
            "AirAsia": "AirAsia",
            "LOT": "LOT",
            "KLM": "KLM",
            "TAP Air Portugal": "TAP Air Portugal",
            "SAS": "SAS",
            "ITA Airways": "ITA Airways",
            "SKY express": "SKY express",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                flight = self.parse(_card(("10:15", "12:05"), raw, "SAW–ATH", "3.890"))
                self.assertEqual(flight["airline"], expected)

    def test_easyjet_card_is_not_split_into_easy_and_jet(self):
        flight = self.parse(_card(("21:05", "22:45"), "easyJet", "MXP–BCN", "2.448"))
        self.assertEqual(flight["airline"], "easyJet")
        self.assertNotIn("/", flight["airline"])
        self.assertEqual(flight["airports"], "MXP ➔ BCN")

    def test_spaced_spellings_use_the_brand_spelling(self):
        self.assertEqual(self.parse(_card(("09:00", "11:00"), "Sun Express", "SAW–AYT", "1.900"))["airline"],
                         "SunExpress")
        self.assertEqual(self.parse(_card(("09:00", "11:00"), "Egypt Air", "IST–CAI", "5.900"))["airline"],
                         "EgyptAir")

    def test_real_codeshare_is_still_split(self):
        flight = self.parse(_card(("10:15", "12:05"), "Vueling / Iberia", "BCN–MXP", "3.953"))
        self.assertEqual(flight["airline"], "Vueling / Iberia")
        flight = self.parse(_card(("03:10", "20:10"), "flydubaiEmirates", "SAW–DMK", "25.637"))
        self.assertEqual(flight["airline"], "flydubai / Emirates")

    def test_comma_separated_codeshare_with_brand(self):
        flight = self.parse(_card(("10:15", "12:05"), "easyJet, Wizz Air", "BCN–BTS", "1.200"))
        self.assertEqual(flight["airline"], "easyJet, Wizz Air")

    def test_operator_note_is_removed(self):
        flight = self.parse(_card(("10:15", "12:05"), "easyJet İşleten: Lauda", "BCN–BTS", "1.200"))
        self.assertEqual(flight["airline"], "easyJet")


# ---------------------------------------------------------------------------
# Calendar timeout is non-fatal and logged below WARNING
# ---------------------------------------------------------------------------


class _CalendarTimeoutPage(FakePage):
    """The calendar's date-input wait times out; every other step works as on a normal results page."""

    def wait_for_selector(self, selector, **kwargs):
        if "Gidiş" in selector:
            raise TimeoutError("Page.wait_for_selector: Timeout 3500ms exceeded.")
        return None


class CalendarTimeoutLogTests(PlaywrightFakeTestCase):
    def test_calendar_timeout_is_info_not_warning_and_results_survive(self):
        scenario = ok_scenario()
        self.session.next_page = lambda: _CalendarTimeoutPage(scenario)
        provider = self.make_provider()
        logger_name = "travel_engine.providers.google_flights_provider"
        with self.assertLogs(logger_name, level="INFO") as captured:
            results = self.search(provider)
        self.assertEqual(len(results), 5)
        self.assertEqual(provider.last_search_status["status"], "ok")
        calendar_records = [r for r in captured.records if "Calendar extraction" in r.getMessage()]
        self.assertEqual(len(calendar_records), 1)
        self.assertEqual(calendar_records[0].levelname, "INFO")
        self.assertIn("timed out", calendar_records[0].getMessage())

    def test_other_calendar_errors_stay_warnings(self):
        class _BrokenCalendar(FakePage):
            def wait_for_selector(self, selector, **kwargs):
                if "Gidiş" in selector:
                    raise RuntimeError("calendar layout changed")
                return None

        scenario = ok_scenario()
        self.session.next_page = lambda: _BrokenCalendar(scenario)
        provider = self.make_provider()
        logger_name = "travel_engine.providers.google_flights_provider"
        with self.assertLogs(logger_name, level="WARNING") as captured:
            self.search(provider)
        messages = [r for r in captured.records if "Calendar extraction error" in r.getMessage()]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].levelname, "WARNING")


# ---------------------------------------------------------------------------
# Calendar extraction: read on the first search of a route, skipped while the route's calendar is fresh
# ---------------------------------------------------------------------------


class _DateInput:
    def click(self, force=False):
        pass

    def is_visible(self):
        return True


class _CalendarCell(FakeElement):
    def __init__(self, iso, text):
        super().__init__(text)
        self._iso = iso

    def get_attribute(self, name):
        return self._iso if name == "data-iso" else None


class _CalendarPage(FakePage):
    """A results page whose date picker opens: the calendar cells carry their prices."""

    def __init__(self, scenario, cells, probes):
        super().__init__(scenario)
        self._cells = cells
        self._probes = probes  # shared list: one entry per page that opened the date picker
        self.keyboard = SimpleNamespace(press=lambda _key: None)

    def query_selector(self, selector):
        if "Gidiş" in selector:
            self._probes.append(selector)
            return _DateInput()
        return None

    def query_selector_all(self, selector):
        if selector == "div[data-iso]":
            return [_CalendarCell(iso, text) for iso, text in self._cells]
        return super().query_selector_all(selector)


CALENDAR_CELLS = [("2026-12-15", "₺4.200"), ("2026-12-16", "₺3.900"), ("2026-12-17", "")]
CALENDAR_PRICES = {"2026-12-15": 4200.0, "2026-12-16": 3900.0}
GFP_LOGGER = "travel_engine.providers.google_flights_provider"


class CalendarFlagTests(PlaywrightFakeTestCase):
    """The calendar is read only while the route has no fresh calendar (memory or disk, 45 min TTL)."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="calendar_flag_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.disk_now = 2_000_000.0
        self.store = FlightCache(os.path.join(self.dir, "flight_cache.db"), clock=lambda: self.disk_now)
        self.probes = []  # one entry per page that opened the date picker (the calendar step)
        self.cells = CALENDAR_CELLS
        self.session.next_page = lambda: _CalendarPage(ok_scenario(), self.cells, self.probes)

    def provider(self):
        return self.make_provider(flight_cache=self.store)

    def test_first_search_reads_the_calendar_and_stores_it(self):
        with self.assertLogs(GFP_LOGGER, level="INFO") as captured:
            self.search(self.provider(), day=15)
        self.assertEqual(len(self.probes), 1)
        messages = [r.getMessage() for r in captured.records]
        self.assertTrue(any("Calendar extracted: 2 real dates" in m for m in messages), messages)
        self.assertEqual(self.store.get_calendar(route_key("ATH", "PRG", "oneway")), CALENDAR_PRICES)

    def test_repeat_on_the_same_route_skips_the_calendar(self):
        provider = self.provider()
        self.search(provider, day=15)
        with self.assertLogs(GFP_LOGGER, level="INFO") as captured:
            self.search(provider, day=16)  # new date: a live search, but the route's calendar is fresh
        self.assertEqual(len(self.probes), 1, "the date picker was opened again")
        messages = [r.getMessage() for r in captured.records]
        self.assertTrue(any("is fresh, skipped" in m for m in messages), messages)
        self.assertFalse(any("Calendar extracted" in m for m in messages), messages)

    def test_fresh_disk_calendar_is_used_by_a_new_provider(self):
        self.search(self.provider(), day=15)
        self.search(self.provider(), day=16)  # empty memory, same database
        self.assertEqual(len(self.probes), 1)

    def test_calendar_older_than_the_flight_ttl_is_read_again(self):
        self.search(self.provider(), day=15)
        self.disk_now += 46 * 60  # past the 45 min flight TTL that the calendar shares
        self.search(self.provider(), day=16)
        self.assertEqual(len(self.probes), 2)

    def test_route_without_calendar_prices_is_read_every_time(self):
        self.cells = []
        provider = self.provider()
        self.search(provider, day=15)
        self.search(provider, day=16)
        self.assertEqual(len(self.probes), 2)

    def test_stored_calendar_is_visible_to_the_provider(self):
        provider = self.provider()
        self.assertEqual(provider.get_calendar_prices("Athens", "Prague", "oneway"), {})
        self.search(provider, day=15)
        self.assertEqual(provider.get_calendar_prices("Athens", "Prague", "oneway"), CALENDAR_PRICES)


if __name__ == "__main__":
    unittest.main()
