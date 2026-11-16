"""GoogleMapsTransitProvider: parsing of the Maps transit page, URL, cache, pacing, cooldown and health.

No network and no browser. Page text comes from tests/fixtures/gmaps (captured Maps text dumps); the
fetch is injected, except for the browser-job test, which uses a fake browser pool.
"""

import os
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from travel_engine.providers import google_maps_transit_provider as gmt
from travel_engine.providers.rail_bus_provider import MapsBudget
from travel_engine.providers.google_maps_transit_provider import (
    GoogleMapsTransitProvider,
    build_url,
    classify_mode,
    parse_departures,
)

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "gmaps")
_DATE = "2026-11-09"  # a Monday
_RATE = 50.0


def _fixture(name):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class _ParseCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("travel_engine.fx.get_eur_try_rate", return_value=(_RATE, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _parse(self, name, origin, dest, date_str=_DATE):
        return parse_departures(_fixture(name), date_str=date_str, origin=origin, dest=dest)


class ParseFixtureTests(_ParseCase):
    def test_milano_venezia_options_with_one_shown_fare(self):
        segs = self._parse("milano_venezia_en.txt", "Milano", "Venezia")
        self.assertEqual(len(segs), 6)
        first = segs[0]
        self.assertEqual((first.departure_time, first.arrival_time, first.duration_mins), ("07:15", "09:30", 135))
        self.assertEqual(first.operator, "FR")
        self.assertEqual(first.mode, "train")
        self.assertEqual(first.origin_station_name, "Milan Central")
        self.assertEqual((first.price_eur, first.price_try), (29.9, 1495.0))
        self.assertFalse(first.price_is_estimated)
        self.assertFalse(first.is_estimated)
        self.assertEqual(first.source, "google_maps")
        self.assertEqual((first.origin_station, first.dest_station, first.date), ("Milano", "Venezia", _DATE))

    def test_option_without_fare_has_no_price(self):
        segs = self._parse("milano_venezia_en.txt", "Milano", "Venezia")
        second = segs[1]  # "2 hr 17 min 7:35 AM (Monday)—9:52 AM" with a station-pair line as operator
        self.assertEqual((second.departure_time, second.arrival_time, second.duration_mins), ("07:35", "09:52", 137))
        self.assertIsNone(second.price_try)
        self.assertIsNone(second.price_eur)
        self.assertFalse(second.price_is_estimated)
        self.assertEqual(second.operator, "Milano Centrale - Venezia Santa Lucia")
        self.assertEqual(second.mode, "train")

    def test_paris_lyon_tgv_fare_is_converted_and_the_rest_are_unpriced(self):
        segs = self._parse("paris_lyon_en.txt", "Paris", "Lyon")
        self.assertEqual(len(segs), 6)
        self.assertEqual((segs[0].price_eur, segs[0].price_try), (51.0, 2550.0))
        self.assertEqual(segs[0].operator, "TGV INOUI")
        self.assertEqual(segs[0].origin_station_name, "Gare de Lyon")
        self.assertTrue(all(seg.mode == "train" for seg in segs))
        self.assertTrue(all(seg.price_try is None for seg in segs[1:]))

    def test_thessaloniki_sofia_is_all_bus_in_12_hour_clock_times(self):
        segs = self._parse("thessaloniki_sofia_en.txt", "Thessaloniki", "Sofia")
        self.assertEqual([seg.departure_time for seg in segs], ["08:00", "08:00", "08:15", "08:20", "09:15", "15:30"])
        self.assertTrue(all(seg.mode == "bus" for seg in segs))
        flix = next(seg for seg in segs if seg.operator == "FlixBus")
        self.assertEqual((flix.arrival_time, flix.duration_mins), ("13:00", 280))
        self.assertEqual(flix.origin_station_name, "Thessaloniki Train Station Bus Stop")
        self.assertTrue(next(seg for seg in segs if seg.operator.startswith("Union Ivkoni")).mode == "bus")

    def test_istanbul_selanik_keeps_the_monday_options_and_the_next_day_arrival(self):
        segs = self._parse("istanbul_selanik_tr.txt", "İstanbul", "Selanik")
        # Three options leave on Tuesday or Wednesday and are not the requested date.
        self.assertEqual([seg.departure_time for seg in segs], ["11:00", "23:00", "23:00"])
        first = segs[0]
        self.assertEqual((first.arrival_time, first.duration_mins), ("00:30", 870))  # 14 sa. 30 dk., next day
        self.assertEqual(first.operator, "FlixBus Union Ivkoni - Unibus 3877")
        self.assertEqual(first.origin_station_name, "Istanbul Central Bus Station")
        self.assertEqual(segs[1].duration_mins, 1020)

    def test_istanbul_ankara_turkish_24_hour_clock(self):
        segs = self._parse("istanbul_ankara_tr.txt", "İstanbul", "Ankara")
        self.assertEqual([seg.departure_time for seg in segs], ["05:00", "05:00", "05:30", "07:00", "07:30", "08:00"])
        self.assertTrue(all(seg.mode == "bus" for seg in segs))
        self.assertTrue(all(seg.operator.startswith("Kamil Koç") for seg in segs))
        self.assertEqual(segs[2].arrival_time, "12:15")
        self.assertEqual(segs[2].duration_mins, 405)  # "6 sa. 45 dk."
        self.assertTrue(all(seg.price_try is None for seg in segs))

    def test_madrid_barcelona_trains_are_real_trains_with_a_station(self):
        segs = self._parse("madrid_barcelona_en.txt", "Madrid", "Barcelona")
        # The page text carries icon glyphs and narrow spaces; the L1 option is the one that needs them cleaned.
        self.assertEqual([seg.operator for seg in segs],
                         ["Madrid-Barcelona L3", "Madrid-Barcelona L1", "AVE", "AVE", "AVE", "OUIGO"])
        self.assertTrue(all(seg.mode == "train" for seg in segs))
        self.assertEqual(segs[0].origin_station_name, "Madrid Puerta de Atocha")
        self.assertEqual(segs[0].duration_mins, 203)
        self.assertEqual((segs[1].departure_time, segs[1].arrival_time), ("07:22", "10:49"))

    def test_no_route_page_gives_no_options(self):
        self.assertEqual(self._parse("no_routes_milano_venezia_en.txt", "Milano", "Venezia"), [])

    def test_results_are_sorted_by_departure(self):
        for name, origin, dest in [("milano_venezia_en.txt", "Milano", "Venezia"),
                                   ("istanbul_ankara_tr.txt", "İstanbul", "Ankara")]:
            times = [seg.departure_time for seg in self._parse(name, origin, dest)]
            self.assertEqual(times, sorted(times))

    def test_wrong_date_or_bad_date_text_gives_nothing(self):
        self.assertEqual(self._parse("milano_venezia_en.txt", "Milano", "Venezia", date_str="2026-11-10"), [])
        self.assertEqual(self._parse("milano_venezia_en.txt", "Milano", "Venezia", date_str="09-11-2026"), [])


class ParseRuleTests(_ParseCase):
    """Option rules on small synthetic texts."""

    @staticmethod
    def _option(duration, times, *lines):
        return "\n".join([duration, times, *lines, "Details"])

    def test_options_under_30_minutes_are_skipped(self):
        text = self._option("25 min", "10:00 AM (Monday)—10:25 AM", "FR")
        self.assertEqual(parse_departures(text, date_str=_DATE, origin="A", dest="B"), [])

    def test_metro_or_tram_only_options_are_skipped(self):
        text = self._option("45 min", "9:00 AM (Monday)—9:45 AM", "Metro Line M1", "Metro")
        self.assertEqual(parse_departures(text, date_str=_DATE, origin="A", dest="B"), [])

    def test_a_walking_line_under_an_option_is_not_an_operator(self):
        text = self._option("2 hr", "9:00 AM (Monday)—11:00 AM", "AVE", "9:00 AM from Madrid Atocha", " 6 min")
        segs = parse_departures(text, date_str=_DATE, origin="A", dest="B")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].operator, "AVE")

    def test_option_on_another_weekday_is_skipped(self):
        text = self._option("2 hr", "9:00 AM (Tuesday)—11:00 AM", "AVE")
        self.assertEqual(parse_departures(text, date_str=_DATE, origin="A", dest="B"), [])

    def test_twelve_hour_clock_and_next_day_arrival(self):
        text = self._option("11 hr", "12:30 PM (Monday)—11:30 PM", "Snälltåget")
        seg = parse_departures(text, date_str=_DATE, origin="A", dest="B")[0]
        self.assertEqual((seg.departure_time, seg.arrival_time), ("12:30", "23:30"))
        text = self._option("2 hr", "12:05 AM (Monday)—2:05 AM", "AVE")
        seg = parse_departures(text, date_str=_DATE, origin="A", dest="B")[0]
        self.assertEqual((seg.departure_time, seg.arrival_time), ("00:05", "02:05"))

    def test_shown_try_fare_is_taken_as_it_is(self):
        text = self._option("4 hr", "9:00 AM (Monday)—1:00 PM", "FlixBus", "₺1.250")
        seg = parse_departures(text, date_str=_DATE, origin="A", dest="B")[0]
        self.assertEqual((seg.price_try, seg.price_eur), (1250.0, None))
        self.assertFalse(seg.price_is_estimated)

    def test_duplicate_options_are_kept_once(self):
        one = self._option("2 hr", "9:00 AM (Monday)—11:00 AM", "AVE")
        self.assertEqual(len(parse_departures(one + "\n" + one, date_str=_DATE, origin="A", dest="B")), 1)


class AmountAndModeTests(unittest.TestCase):
    def test_amount_forms(self):
        self.assertEqual(gmt._amount("29.90"), 29.9)
        self.assertEqual(gmt._amount("29,90"), 29.9)
        self.assertEqual(gmt._amount("1.234"), 1234.0)
        self.assertEqual(gmt._amount("1.234,50"), 1234.5)
        self.assertEqual(gmt._amount("1,234.50"), 1234.5)

    def test_clock_forms(self):
        self.assertEqual(gmt._clock_minutes("7:15 AM"), 435)
        self.assertEqual(gmt._clock_minutes("12:00 PM"), 720)
        self.assertEqual(gmt._clock_minutes("12:15 AM"), 15)
        self.assertEqual(gmt._clock_minutes("05:30"), 330)
        self.assertIsNone(gmt._clock_minutes("25:00"))
        self.assertIsNone(gmt._clock_minutes("soon"))

    def test_duration_forms(self):
        self.assertEqual(gmt._duration_minutes("2 hr 15 min"), 135)
        self.assertEqual(gmt._duration_minutes("18 hr"), 1080)
        self.assertEqual(gmt._duration_minutes("14 sa. 30 dk."), 870)
        self.assertEqual(gmt._duration_minutes("17 sa."), 1020)
        self.assertEqual(gmt._duration_minutes("6 min"), 6)
        self.assertIsNone(gmt._duration_minutes("Details"))
        self.assertIsNone(gmt._duration_minutes("12"))

    def test_mode_from_operator_and_station_words(self):
        # 1. Bus operators
        self.assertEqual(classify_mode("FlixBus"), "bus")
        self.assertEqual(classify_mode("Kamil Koç 81030"), "bus")
        self.assertEqual(classify_mode("Union Ivkoni - Unibus 3783"), "bus")
        self.assertEqual(classify_mode("ArdaTur"), "bus")
        self.assertEqual(classify_mode("Metro Turizm"), "bus")
        self.assertEqual(classify_mode("Pamukkale Turizm"), "bus")
        self.assertEqual(classify_mode("BlaBlaCar Bus"), "bus")
        self.assertEqual(classify_mode("Ecolines"), "bus")

        # 2. Bus station / mode keywords (Otogar, Busbahnhof, ZOB, Autogare, Coach, Bus)
        self.assertEqual(classify_mode("Berlin ZOB"), "bus")
        self.assertEqual(classify_mode("Hamburg Busbahnhof"), "bus")
        self.assertEqual(classify_mode("Autogare de Nice"), "bus")
        self.assertEqual(classify_mode("Victoria Coach Station London"), "bus")
        self.assertEqual(classify_mode("Esenler Otogarı"), "bus")
        self.assertEqual(classify_mode("Istanbul Central Bus Station, kalkış: 05:30"), "bus")

        # 3. Train operators
        self.assertEqual(classify_mode("FR"), "train")
        self.assertEqual(classify_mode("TGV INOUI  Rhônexpress   A"), "train")
        self.assertEqual(classify_mode("AVE"), "train")
        self.assertEqual(classify_mode("OUIGO"), "train")
        self.assertEqual(classify_mode("Madrid-Barcelona L1"), "train")
        self.assertEqual(classify_mode("Madrid-Barcelona Euromed"), "train")
        self.assertEqual(classify_mode("rjx"), "train")
        self.assertEqual(classify_mode("ICE 723"), "train")
        self.assertEqual(classify_mode("Trenitalia Frecciarossa 1000"), "train")

        # 4. Train station keywords (Hbf, Centrale, Gare, Estación, hl.n., Station, Bahnhof, Keleti, etc.)
        self.assertEqual(classify_mode("Berlin Hbf"), "train")
        self.assertEqual(classify_mode("Milano Centrale - Venezia Santa Lucia"), "train")
        self.assertEqual(classify_mode("Paris Gare de Lyon"), "train")
        self.assertEqual(classify_mode("Madrid Estación de Atocha"), "train")
        self.assertEqual(classify_mode("Praha hl.n."), "train")
        self.assertEqual(classify_mode("München Bahnhof"), "train")
        self.assertEqual(classify_mode("Budapest Keleti"), "train")
        self.assertEqual(classify_mode("Frankfurt Hauptbahnhof"), "train")
        self.assertEqual(classify_mode("Torino Porta Nuova"), "train")
        self.assertEqual(classify_mode("Ankara Garı"), "train")

        # 5. Unknown text without train/bus keywords returns None
        self.assertIsNone(classify_mode("Some Unknown Transit 123"))
        self.assertIsNone(classify_mode("Transfer Express"))



class UrlTests(unittest.TestCase):
    def test_transit_directions_url_with_epoch_and_country(self):
        epoch = int(datetime(2026, 11, 9, 8, 0, tzinfo=ZoneInfo("Europe/Rome")).timestamp())
        url = build_url("Milan", "Venice", epoch, "en", "it")
        self.assertEqual(url, "https://www.google.com/maps/dir/Milan/Venice/data=!4m6!4m5!2m3!6e0!7e2!8j"
                              f"{epoch}!3e3?hl=en&gl=it")

    def test_names_are_path_quoted_and_country_is_optional(self):
        url = build_url("İstanbul", "Selanik, Yunanistan", 1, "tr")
        self.assertIn("/maps/dir/%C4%B0stanbul/Selanik%2C%20Yunanistan/", url)
        self.assertNotIn("&gl=", url)


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class _ProviderCase(unittest.TestCase):
    def setUp(self):
        GoogleMapsTransitProvider.reset_shared_state()
        patcher = mock.patch("travel_engine.fx.get_eur_try_rate", return_value=(_RATE, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = _Clock()
        self.sleeps = []
        for target, value in [(gmt, "_now"), ]:
            p = mock.patch.object(target, value, self.clock)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(gmt, "_sleep", side_effect=lambda s: self.sleeps.append(s))
        p.start()
        self.addCleanup(p.stop)
        self.pages = []  # (url, locale, timezone_id, wait_ms) of each fake page load
        self.reply = (lambda url: ("https://www.google.com/maps/dir/x", _fixture("milano_venezia_en.txt")))

    def _fetch(self, url, *, locale, timezone_id, wait_ms):
        self.pages.append((url, locale, timezone_id, wait_ms))
        return self.reply(url)

    def _provider(self, **kwargs):
        options = dict(min_interval_secs=0, fetch_page=self._fetch, wait_ms=8500)
        options.update(kwargs)
        return GoogleMapsTransitProvider(**options)


class SearchTests(_ProviderCase):
    def test_search_loads_one_page_with_the_local_epoch_and_parses_it(self):
        segs = self._provider().search("Milano", "Venezia", _DATE, "07:00")
        self.assertEqual(len(segs), 6)
        url, locale, zone, wait_ms = self.pages[0]
        epoch = int(datetime(2026, 11, 9, 7, 0, tzinfo=ZoneInfo("Europe/Rome")).timestamp())
        self.assertIn(f"!8j{epoch}!3e3", url)
        self.assertEqual((locale, zone, wait_ms), ("en-US", "Europe/Rome", 8500))

    def test_default_start_is_midnight_in_the_origin_zone(self):
        self._provider().search("Istanbul", "Ankara", _DATE)
        epoch = int(datetime(2026, 11, 9, 0, 0, tzinfo=ZoneInfo("Europe/Istanbul")).timestamp())
        self.assertIn(f"!8j{epoch}!", self.pages[0][0])
        self.assertIn("&gl=tr", self.pages[0][0])

    def test_earliest_departure_filters_the_cached_page(self):
        provider = self._provider()
        provider.search("Milano", "Venezia", _DATE, "07:00")
        self.assertEqual(len(self.pages), 1)
        provider.search("Milano", "Venezia", _DATE, "07:00")  # the same key: the cached page
        self.assertEqual(len(self.pages), 1)
        provider.search("Milano", "Venezia", _DATE, "08:00")  # another start time: another page load
        self.assertGreater(len(self.pages), 1)
        self.assertIn(f"!8j{int(datetime(2026, 11, 9, 8, 0, tzinfo=ZoneInfo('Europe/Rome')).timestamp())}!",
                      self.pages[1][0])
        later = provider.search("Milano", "Venezia", _DATE, "08:00")
        self.assertTrue(all(seg.departure_time >= "08:00" for seg in later))

    def test_sparse_first_page_loads_a_second_page_from_its_last_departure(self):
        # The 07:00 Madrid page lists 06:09 to 07:22: one option at or after 07:00.
        texts = [_fixture("madrid_barcelona_0700_en.txt"), _fixture("madrid_barcelona_en.txt")]
        self.reply = lambda url: ("https://www.google.com/maps/dir/x", texts.pop(0))
        provider = self._provider()
        segs = provider.search("Madrid", "Barcelona", _DATE, "07:00")
        self.assertEqual(len(self.pages), 2)
        second_epoch = int(datetime(2026, 11, 9, 7, 23, tzinfo=ZoneInfo("Europe/Madrid")).timestamp())
        self.assertIn(f"!8j{second_epoch}!", self.pages[1][0])
        self.assertIn("AVE", [seg.operator for seg in segs])
        self.assertTrue(all(seg.departure_time >= "07:00" for seg in segs))
        self.assertEqual(len({(s.departure_time, s.arrival_time, s.operator) for s in segs}), len(segs))

    def test_full_first_page_is_not_extended(self):
        provider = self._provider()
        self.assertEqual(len(provider.search("Milano", "Venezia", _DATE, "07:00")), 6)
        self.assertEqual(len(self.pages), 1)

    def test_failed_second_page_keeps_the_first_page(self):
        texts = [_fixture("madrid_barcelona_0700_en.txt")]

        def reply(url):
            if texts:
                return ("https://www.google.com/maps/dir/x", texts.pop(0))
            raise RuntimeError("second page failed")

        self.reply = reply
        provider = self._provider()
        segs = provider.search("Madrid", "Barcelona", _DATE, "07:00")
        self.assertEqual([seg.departure_time for seg in segs], ["07:22"])
        self.assertEqual(len(self.pages), 2)
        self.assertEqual(provider.health_status()["consecutive_failures"], 1)

    def test_max_results_caps_the_list(self):
        self.assertEqual(len(self._provider().search("Milano", "Venezia", _DATE, "07:00", max_results=2)), 2)

    def test_same_search_inside_30_minutes_is_served_from_cache(self):
        provider = self._provider()
        provider.search("Milano", "Venezia", _DATE, "07:00")
        self.clock.now += 29 * 60
        provider.search("Milano", "Venezia", _DATE, "07:00")
        self.assertEqual(len(self.pages), 1)

    def test_cache_expires_after_30_minutes(self):
        provider = self._provider()
        provider.search("Milano", "Venezia", _DATE, "07:00")
        self.clock.now += 31 * 60
        provider.search("Milano", "Venezia", _DATE, "07:00")
        self.assertEqual(len(self.pages), 2)

    def test_cached_segments_are_copies(self):
        provider = self._provider()
        first = provider.search("Milano", "Venezia", _DATE, "07:00")
        first[0].source = "changed by a caller"
        second = provider.search("Milano", "Venezia", _DATE, "07:00")
        self.assertEqual(second[0].source, "google_maps")

    def test_page_loads_are_spaced_by_the_minimum_interval(self):
        provider = self._provider(min_interval_secs=8.0)
        provider.search("Milano", "Venezia", _DATE, "07:00")
        provider.search("Paris", "Lyon", _DATE, "07:00")
        self.assertEqual(len(self.pages), 2)  # the clock did not move, so the second load waits
        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], 8.0)

    def test_no_route_page_is_an_empty_answer_not_a_failure(self):
        self.reply = lambda url: ("https://www.google.com/maps/dir/x", _fixture("no_routes_milano_venezia_en.txt"))
        provider = self._provider()
        self.assertEqual(provider.search("Milano", "Venezia", _DATE, "07:00"), [])
        self.assertEqual(provider.health_status()["status"], "ok")
        self.assertTrue(provider.health_status()["healthy"])

    def test_options_on_another_day_are_an_empty_answer_not_a_failure(self):
        # The page lists Monday options; a Tuesday search keeps none of them.
        provider = self._provider()
        self.assertEqual(provider.search("Milano", "Venezia", "2026-11-10", "07:00"), [])
        self.assertEqual(provider.health_status()["status"], "ok")
        self.assertEqual(provider.health_status()["consecutive_failures"], 0)

    def test_page_without_options_is_a_parse_error(self):
        self.reply = lambda url: ("https://www.google.com/maps/dir/x", "Some unexpected layout")
        provider = self._provider()
        self.assertIsNone(provider.search("Milano", "Venezia", _DATE, "07:00"))
        self.assertEqual(provider.health_status()["status"], "parse_error")

    def test_origin_without_a_time_zone_is_not_searched(self):
        provider = self._provider()
        self.assertIsNone(provider.search("Metz", "Paris", _DATE, "07:00"))  # no airport, not in the table: UTC
        self.assertEqual(self.pages, [])
        self.assertEqual(provider.health_status()["consecutive_failures"], 0)

    def test_bad_input_returns_none_without_a_page_load(self):
        provider = self._provider()
        self.assertIsNone(provider.search("", "Venezia", _DATE, "07:00"))
        self.assertIsNone(provider.search("Milano", "Venezia", "2026-13-40", "07:00"))
        self.assertIsNone(provider.search("Milano", "Venezia", _DATE, "7 o'clock"))
        self.assertEqual(provider.search("Milano", "milano", _DATE), [])
        self.assertEqual(self.pages, [])

    def test_failed_page_load_returns_none_and_is_recorded(self):
        def boom(url):
            raise RuntimeError("browser gone")

        self.reply = boom
        provider = self._provider()
        self.assertIsNone(provider.search("Milano", "Venezia", _DATE, "07:00"))
        health = provider.health_status()
        self.assertEqual((health["status"], health["consecutive_failures"]), ("error", 1))
        self.assertIn("browser gone", health["last_error"])

    def test_unexpected_error_inside_search_never_raises(self):
        provider = self._provider()
        with mock.patch.object(gmt, "parse_departures", side_effect=ValueError("bad text")):
            self.assertIsNone(provider.search("Milano", "Venezia", _DATE, "07:00"))
        self.assertEqual(provider.health_status()["consecutive_failures"], 1)

    def test_three_failures_make_the_source_unhealthy(self):
        self.reply = lambda url: ("https://www.google.com/maps/dir/x", "nothing here")
        provider = self._provider()
        for dest in ["Venezia", "Padova", "Verona"]:
            provider.search("Milano", dest, _DATE, "07:00")
        health = provider.health_status()
        self.assertEqual(health["consecutive_failures"], 3)
        self.assertFalse(health["healthy"])

    def test_success_resets_the_failure_count(self):
        self.reply = lambda url: ("https://www.google.com/maps/dir/x", "nothing here")
        provider = self._provider()
        provider.search("Milano", "Venezia", _DATE, "07:00")
        self.reply = lambda url: ("https://www.google.com/maps/dir/x", _fixture("milano_venezia_en.txt"))
        provider.search("Milano", "Padova", _DATE, "07:00")
        health = provider.health_status()
        self.assertEqual((health["status"], health["consecutive_failures"], health["healthy"]), ("ok", 0, True))
        self.assertIsNotNone(health["last_ok_at"])


class CooldownTests(_ProviderCase):
    """A block page starts a 30 minute cooldown: no page is loaded while it runs."""

    def _block(self):
        self.reply = lambda url: ("https://www.google.com/sorry/index?continue=https://www.google.com/maps",
                                  _fixture("sorry_captcha_tr.txt"))

    def test_sorry_redirect_returns_none_and_starts_a_cooldown(self):
        self._block()
        provider = self._provider()
        self.assertIsNone(provider.search("Milano", "Venezia", _DATE, "07:00"))
        health = provider.health_status()
        self.assertEqual(health["status"], "blocked")
        self.assertFalse(health["healthy"])
        self.assertAlmostEqual(health["blocked_cooldown_remaining_secs"], 1800.0)

    def test_no_page_is_loaded_during_the_cooldown(self):
        self._block()
        provider = self._provider()
        provider.search("Milano", "Venezia", _DATE, "07:00")
        self.clock.now += 10 * 60
        self.reply = lambda url: ("https://www.google.com/maps/dir/x", _fixture("milano_venezia_en.txt"))
        self.assertIsNone(provider.search("Paris", "Lyon", _DATE, "07:00"))
        self.assertEqual(len(self.pages), 1)

    def test_pages_resume_after_the_cooldown(self):
        self._block()
        provider = self._provider()
        provider.search("Milano", "Venezia", _DATE, "07:00")
        self.clock.now += 31 * 60
        self.reply = lambda url: ("https://www.google.com/maps/dir/x", _fixture("milano_venezia_en.txt"))
        self.assertEqual(len(provider.search("Milano", "Venezia", _DATE, "07:00")), 6)
        self.assertEqual(len(self.pages), 2)
        self.assertEqual(provider.health_status()["status"], "ok")

    def test_unusual_traffic_text_is_a_block_even_without_redirect(self):
        self.reply = lambda url: ("https://www.google.com/maps/dir/x",
                                  "Our systems have detected unusual traffic from your network.")
        provider = self._provider()
        self.assertIsNone(provider.search("Milano", "Venezia", _DATE, "07:00"))
        self.assertEqual(provider.health_status()["status"], "blocked")


class HealthTests(_ProviderCase):
    def test_health_before_any_search_is_unknown_and_healthy(self):
        health = self._provider().health_status()
        self.assertEqual(health["source"], "google_maps")
        self.assertEqual((health["status"], health["healthy"], health["requests"]), ("unknown", True, 0))

    def test_health_counts_page_loads(self):
        provider = self._provider()
        provider.search("Milano", "Venezia", _DATE, "07:00")
        self.assertEqual(provider.health_status()["requests"], 1)
        self.assertEqual(provider.health_status()["cached_searches"], 1)

    def test_can_resolve_accepts_any_name(self):
        provider = self._provider()
        self.assertTrue(provider.can_resolve("Milano"))
        self.assertFalse(provider.can_resolve("  "))


class DefaultFetchTests(unittest.TestCase):
    """_playwright_fetch runs the page job on the shared pool, under the shared browser cap."""

    def setUp(self):
        self.slots = _FakeSlots()
        self.browser = _FakeBrowser()
        patchers = [
            mock.patch.object(gmt.gf, "_browser_slots", lambda: self.slots),
            mock.patch.object(gmt.gf, "_get_browser_worker", lambda: _FakePool(self.browser)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_page_text_and_final_url_come_back_and_the_slot_is_released(self):
        final_url, text = gmt._playwright_fetch("https://www.google.com/maps/dir/A/B/", locale="en-US",
                                                timezone_id="Europe/Rome", wait_ms=0)
        self.assertEqual(text, "rendered text")
        self.assertEqual(final_url, "https://www.google.com/maps/dir/A/B/")
        self.assertEqual(self.browser.context_options["timezone_id"], "Europe/Rome")
        self.assertEqual(self.browser.context_options["locale"], "en-US")
        self.assertEqual((self.slots.acquired, self.slots.released), (1, 1))
        self.assertTrue(self.browser.context_closed)

    def test_page_error_raises_and_still_releases_the_slot(self):
        self.browser.fail = True
        with self.assertRaises(RuntimeError):
            gmt._playwright_fetch("https://www.google.com/maps/dir/A/B/", locale="en-US",
                                  timezone_id="Europe/Rome", wait_ms=0)
        self.assertEqual(self.slots.released, 1)
        self.assertTrue(self.browser.context_closed)

    def test_no_free_slot_raises(self):
        self.slots.free = False
        with self.assertRaises(RuntimeError):
            gmt._playwright_fetch("https://www.google.com/maps/dir/A/B/", locale="en-US",
                                  timezone_id="Europe/Rome", wait_ms=0)


class _FakeSlots:
    def __init__(self):
        self.free = True
        self.acquired = 0
        self.released = 0

    def acquire(self, timeout):
        if self.free:
            self.acquired += 1
        return self.free

    def release(self):
        self.released += 1


class _FakePage:
    def __init__(self, browser, url):
        self.browser = browser
        self.url = "about:blank"

    def route(self, pattern, handler):
        pass

    def goto(self, url, **kwargs):
        if self.browser.fail:
            raise RuntimeError("navigation timeout")
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def inner_text(self, selector):
        return "rendered text"


class _FakeContext:
    def __init__(self, browser):
        self.browser = browser

    def add_init_script(self, script):
        pass

    def new_page(self):
        return _FakePage(self.browser, "about:blank")

    def close(self):
        self.browser.context_closed = True


class _FakeBrowser:
    def __init__(self):
        self.fail = False
        self.context_options = {}
        self.context_closed = False

    def new_context(self, **kwargs):
        self.context_options = kwargs
        return _FakeContext(self)


class _FakePool:
    def __init__(self, browser):
        self.browser = browser

    def run(self, job, timeout):
        return job(self.browser)


if __name__ == "__main__":
    unittest.main()


class PageBudgetTests(_ProviderCase):
    """Each page load is charged to the search's MapsBudget; a refused load is no answer, not a failure."""

    def test_each_page_load_is_charged(self):
        budget = MapsBudget(5)
        segs = self._provider().search("Milano", "Venezia", _DATE, "07:00", budget=budget)
        self.assertEqual(len(segs), 6)
        self.assertEqual((budget.used, budget.denied), (1, 0))

    def test_two_page_lookup_uses_two_units_when_allowed(self):
        texts = [_fixture("madrid_barcelona_0700_en.txt"), _fixture("madrid_barcelona_en.txt")]
        self.reply = lambda url: ("https://www.google.com/maps/dir/x", texts.pop(0))
        budget = MapsBudget(2)
        self._provider().search("Madrid", "Barcelona", _DATE, "07:00", budget=budget)
        self.assertEqual(len(self.pages), 2)
        self.assertEqual((budget.used, budget.denied), (2, 0))

    def test_refused_first_page_gives_no_answer_and_loads_no_page(self):
        budget = MapsBudget(0)
        provider = self._provider()
        self.assertIsNone(provider.search("Milano", "Venezia", _DATE, "07:00", budget=budget))
        self.assertEqual(self.pages, [])
        self.assertTrue(budget.exhausted)
        health = provider.health_status()
        self.assertEqual((health["consecutive_failures"], health["status"]), (0, "unknown"))

    def test_refused_lookup_waits_for_nothing_and_reserves_no_page_spacing(self):
        # The budget is decided before the process-wide spacing or the browser slot is touched: a refusal
        # returns at once, with no sleep and no request counted.
        provider = self._provider(min_interval_secs=8.0)
        provider.search("Milano", "Venezia", _DATE, "07:00", budget=MapsBudget(0))
        self.assertEqual(self.sleeps, [])
        self.assertEqual(gmt._SHARED.requests, 0)
        self.assertEqual(gmt._SHARED.next_page_at, 0.0)

    def test_refused_second_page_keeps_the_first_page_and_is_not_a_failure(self):
        texts = [_fixture("madrid_barcelona_0700_en.txt")]
        self.reply = lambda url: ("https://www.google.com/maps/dir/x", texts.pop(0))
        budget = MapsBudget(1)
        provider = self._provider()
        segs = provider.search("Madrid", "Barcelona", _DATE, "07:00", budget=budget)
        self.assertEqual([seg.departure_time for seg in segs], ["07:22"])
        self.assertEqual(len(self.pages), 1)
        self.assertEqual((budget.used, budget.denied), (1, 1))
        self.assertEqual(provider.health_status()["consecutive_failures"], 0)

    def test_cached_answer_costs_no_budget(self):
        provider = self._provider()
        first = provider.search("Milano", "Venezia", _DATE, "07:00", budget=MapsBudget(10))
        empty = MapsBudget(0)
        second = provider.search("Milano", "Venezia", _DATE, "07:00", budget=empty)
        self.assertEqual(len(second), len(first))
        self.assertEqual(len(self.pages), 1)
        self.assertEqual((empty.used, empty.denied), (0, 0))
