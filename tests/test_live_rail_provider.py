import copy
import json
import os
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from travel_engine import fx
from travel_engine.providers import live_rail_provider as lrp
from travel_engine.providers.live_rail_provider import (
    DEFAULT_EUR_TRY_RATE,
    LIVE_CORRIDORS,
    REGIOJET_STATION_IDS,
    TURKISH_ALIASES,
    LiveRailProvider,
)
from travel_engine.providers.rail_bus_provider import ScheduledSegment

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "rail")
URLOPEN = "travel_engine.providers.live_rail_provider.urllib.request.urlopen"


def load_fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class LiveRailProviderParsingTests(unittest.TestCase):
    def test_bratislava_to_prague_fixture_parsed(self):
        provider = LiveRailProvider(eur_try_rate=50.0)
        body = load_fixture("regiojet_bts_prg_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)):
            segs = provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")

        self.assertEqual(len(segs), 4)
        first = segs[0]
        self.assertEqual(first.origin_station, "Bratislava hl.st.")
        self.assertEqual(first.dest_station, "Praha hl.n.")
        self.assertEqual(first.departure_time, "05:17")
        self.assertEqual(first.arrival_time, "09:58")
        self.assertEqual(first.duration_mins, 281)  # travelTime "04:41 h"
        self.assertEqual(first.operator, "RegioJet")
        self.assertEqual(first.mode, "train")
        self.assertEqual(first.date, "2026-10-28")
        self.assertFalse(first.is_estimated)
        self.assertEqual(first.price_eur, 11.9)
        self.assertEqual(first.price_try, 595.0)  # 11.9 EUR * 50.0
        self.assertFalse(first.price_is_estimated)

        self.assertEqual([s.departure_time for s in segs], ["05:17", "08:26", "12:26", "16:15"])
        self.assertTrue(all(s.date == "2026-10-28" for s in segs))

    def test_vienna_to_prague_fixture_parsed(self):
        provider = LiveRailProvider(eur_try_rate=50.0)
        body = load_fixture("regiojet_wien_prg_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)):
            segs = provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28")
        self.assertEqual(len(segs), 4)
        self.assertEqual((segs[0].departure_time, segs[0].arrival_time), ("06:39", "10:56"))
        self.assertEqual(segs[0].price_eur, 12.9)
        self.assertTrue(all(s.origin_station == "Wien Hbf" for s in segs))

    def test_sold_out_route_has_no_price_and_is_flagged(self):
        provider = LiveRailProvider(eur_try_rate=50.0)
        body = load_fixture("regiojet_prg_wien_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)):
            segs = provider.get_schedules("Praha hl.n.", "Wien Hbf", "2026-10-28")
        self.assertEqual(len(segs), 4)
        sold_out = segs[0]  # 06:01 bookable False, priceFrom 0 in the real response
        self.assertEqual(sold_out.departure_time, "06:01")
        self.assertFalse(sold_out.is_estimated)  # times are still real
        self.assertIsNone(sold_out.price_eur)
        self.assertTrue(sold_out.price_is_estimated)
        self.assertIsNone(sold_out.price_try)  # unknown, never 0; RailBusProvider fills in the table price
        self.assertEqual(segs[2].price_eur, 35.9)
        self.assertFalse(segs[2].price_is_estimated)

    def test_unsupported_corridor_returns_none_without_request(self):
        provider = LiveRailProvider(eur_try_rate=50.0)
        # Budapest Keleti is not a supported corridor at all.
        with mock.patch(URLOPEN) as opened:
            self.assertIsNone(provider.get_schedules("Budapest Keleti", "Praha hl.n.", "2026-10-28"))
            opened.assert_not_called()

    def test_no_connection_response_yields_empty_list(self):
        provider = LiveRailProvider(eur_try_rate=50.0)
        body = load_fixture("regiojet_bud_keleti_prg_no_connection.json")
        # Same real response, reused for a supported corridor to exercise the empty path.
        with mock.patch(URLOPEN, return_value=FakeResponse(body)):
            self.assertEqual(provider.get_schedules("Praha hl.n.", "Wien Hbf", "2026-10-28"), [])

    def test_earliest_departure_filter(self):
        provider = LiveRailProvider(eur_try_rate=50.0)
        body = load_fixture("regiojet_bts_prg_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)):
            segs = provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28", "12:00")
        self.assertEqual([s.departure_time for s in segs], ["12:26", "16:15"])

    def test_request_url_and_currency_header(self):
        provider = LiveRailProvider(eur_try_rate=50.0)
        body = load_fixture("regiojet_bts_prg_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)) as opened:
            provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")
        request = opened.call_args[0][0]
        self.assertEqual(opened.call_args[1]["timeout"], 8)
        self.assertTrue(request.full_url.startswith("https://brn-ybus-pubapi.sa.cz/restapi/routes/search/simple?"))
        self.assertIn("fromLocationId=1841058000", request.full_url)
        self.assertIn("toLocationId=372825000", request.full_url)
        self.assertIn("departureDate=2026-10-28", request.full_url)
        self.assertIn("fromLocationType=STATION", request.full_url)
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(headers["x-currency"], "EUR")

    def test_station_ids_match_locations_fixture(self):
        locs = json.loads(load_fixture("regiojet_locations_pilot.json").decode("utf-8"))
        by_id = {}
        for country in locs:
            for city in country["cities"]:
                for station in city["stations"]:
                    by_id[station["id"]] = station["fullname"]
        self.assertEqual(by_id[REGIOJET_STATION_IDS["Bratislava hl.st."]], "Bratislava - MS")
        self.assertEqual(by_id[REGIOJET_STATION_IDS["Wien Hbf"]], "Vienna - Hbf")
        self.assertEqual(by_id[REGIOJET_STATION_IDS["Praha hl.n."]], "Prague - MS")

    def test_supports_only_verified_corridors(self):
        provider = LiveRailProvider()
        self.assertTrue(provider.supports("Bratislava hl.st.", "Praha hl.n."))
        self.assertTrue(provider.supports(" Wien Hbf ", "Praha hl.n."))
        self.assertTrue(provider.supports("Praha hl.n.", "Wien Hbf"))
        self.assertTrue(provider.supports("Budapest Kelenföld", "Praha hl.n."))
        self.assertFalse(provider.supports("Budapest Keleti", "Praha hl.n."))
        self.assertEqual(len(LIVE_CORRIDORS), 4)


class LiveRailProviderErrorTests(unittest.TestCase):
    def setUp(self):
        self.provider = LiveRailProvider(eur_try_rate=50.0)

    def _assert_none_for(self, side_effect):
        with mock.patch(URLOPEN, side_effect=side_effect):
            self.assertIsNone(self.provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28"))

    def test_network_error_returns_none(self):
        self._assert_none_for(urllib.error.URLError("no route to host"))

    def test_timeout_returns_none(self):
        self._assert_none_for(TimeoutError("timed out"))

    def test_http_error_returns_none(self):
        self._assert_none_for(urllib.error.HTTPError(lrp.ROUTE_SEARCH_URL, 503, "Service Unavailable", None, None))

    def test_bad_json_returns_none(self):
        with mock.patch(URLOPEN, return_value=FakeResponse(b"<html>503</html>")):
            self.assertIsNone(self.provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28"))

    def test_unexpected_shape_returns_none(self):
        with mock.patch(URLOPEN, return_value=FakeResponse(b'{"nope": []}')):
            self.assertIsNone(self.provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28"))

    def test_malformed_date_returns_none_without_request(self):
        with mock.patch(URLOPEN) as opened:
            self.assertIsNone(self.provider.get_schedules("Wien Hbf", "Praha hl.n.", "28.10.2026"))
            opened.assert_not_called()

    def test_errors_are_not_cached(self):
        body = load_fixture("regiojet_wien_prg_2026-10-28.json")
        with mock.patch(URLOPEN, side_effect=[urllib.error.URLError("down"), FakeResponse(body)]) as opened:
            self.assertIsNone(self.provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28"))
            segs = self.provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28")
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(len(segs), 4)


class LiveRailProviderCacheTests(unittest.TestCase):
    def test_second_call_served_from_cache(self):
        clock = FakeClock()
        provider = LiveRailProvider(eur_try_rate=50.0, clock=clock)
        body = load_fixture("regiojet_bts_prg_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)) as opened:
            first = provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")
            second = provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28", "12:00")
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(first), 4)
        self.assertEqual(len(second), 2)  # the filter is applied on the cached day

    def test_cache_key_includes_date(self):
        clock = FakeClock()
        provider = LiveRailProvider(eur_try_rate=50.0, clock=clock)
        body = load_fixture("regiojet_bts_prg_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)) as opened:
            provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")
            provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-29")
        self.assertEqual(opened.call_count, 2)

    def test_entry_expires_after_30_minutes(self):
        clock = FakeClock(1000.0)
        provider = LiveRailProvider(eur_try_rate=50.0, clock=clock)
        body = load_fixture("regiojet_bts_prg_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)) as opened:
            provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")
            clock.now = 1000.0 + 29 * 60
            provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")
            self.assertEqual(opened.call_count, 1)
            clock.now = 1000.0 + 31 * 60
            provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")
            self.assertEqual(opened.call_count, 2)

    def test_returned_segments_do_not_alias_cache(self):
        clock = FakeClock()
        provider = LiveRailProvider(eur_try_rate=50.0, clock=clock)
        body = load_fixture("regiojet_bts_prg_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)):
            first = provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")
            first[0].price_try = 999999.0
            again = provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")
        self.assertEqual(again[0].price_try, 595.0)


class LiveRailProviderEurConversionTests(unittest.TestCase):
    def test_explicit_rate_converts_price(self):
        provider = LiveRailProvider(eur_try_rate=40.0)
        body = load_fixture("regiojet_bts_prg_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)):
            segs = provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")
        self.assertEqual(segs[0].price_eur, 11.9)
        self.assertEqual(segs[0].price_try, 476.0)  # 11.9 * 40

    def test_env_var_sets_default_rate(self):
        with mock.patch.dict(os.environ, {"TRAVELPLAN_EUR_TRY": "45.5"}):
            provider = LiveRailProvider()
        self.assertEqual(provider.eur_try_rate, 45.5)

    def test_missing_env_var_uses_constant(self):
        # Changed: without an env override the rate now comes from fx (live). With the
        # network down and no earlier live value, the fallback is still the constant.
        fx._reset_for_tests()
        env = {k: v for k, v in os.environ.items() if k != "TRAVELPLAN_EUR_TRY"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch(URLOPEN, side_effect=urllib.error.URLError("offline")):
            provider = LiveRailProvider()
            self.assertEqual(provider.eur_try_rate, DEFAULT_EUR_TRY_RATE)

    def test_invalid_env_var_falls_back_to_constant(self):
        # Changed: an invalid env value now means "no override", so the live rate is asked
        # for next. With the network down the fallback is still the constant.
        fx._reset_for_tests()
        with mock.patch.dict(os.environ, {"TRAVELPLAN_EUR_TRY": "abc"}), \
                mock.patch(URLOPEN, side_effect=urllib.error.URLError("offline")):
            provider = LiveRailProvider()
            self.assertEqual(provider.eur_try_rate, DEFAULT_EUR_TRY_RATE)


class ScheduledSegmentLiveFieldsTests(unittest.TestCase):
    def test_new_fields_default_and_serialize(self):
        seg = ScheduledSegment(
            origin_station="A", dest_station="B", departure_time="10:00", arrival_time="11:00",
            duration_mins=60, price_try=100.0, operator="X", mode="train", date="2026-10-28",
        )
        self.assertIsNone(seg.price_eur)
        self.assertTrue(seg.price_is_estimated)
        d = seg.to_dict()
        self.assertIsNone(d["price_eur"])
        self.assertTrue(d["price_is_estimated"])


CITY_VIE_BUD = ("10202052", "10202091")   # Vienna -> Budapest
CITY_BTS_PRG = ("10202001", "10202003")   # Bratislava -> Prague
CITY_BRN_VIE = ("10202002", "10202052")   # Brno -> Vienna
CITY_PRG_BER = ("10202003", "10202072")   # Prague -> Berlin (bus only)
EMPTY_ROUTES = b'{"routes": []}'
VIENNA, BUDAPEST, PRAGUE, BRATISLAVA, BRNO, BERLIN = (
    "10202052", "10202091", "10202003", "10202001", "10202002", "10202072",
)


def _reply(item):
    if isinstance(item, BaseException):
        raise item
    return FakeResponse(item)


class RegioJetRouter:
    """Fake ``urlopen``: serves the locations fixture and city-level search fixtures keyed by (fromId, toId).

    Unknown city pairs get an empty route list. Every request is kept in ``requests``.
    """

    def __init__(self, locations=None, searches=None):
        self.locations = locations if locations is not None else load_fixture("regiojet_locations_subset.json")
        self.searches = searches if searches is not None else {
            CITY_VIE_BUD: load_fixture("regiojet_city_vie_bud_2026-10-28.json"),
            CITY_BTS_PRG: load_fixture("regiojet_city_bts_prg_2026-10-28.json"),
            CITY_BRN_VIE: load_fixture("regiojet_city_brn_vie_2026-10-28.json"),
            CITY_PRG_BER: load_fixture("regiojet_city_prg_ber_2026-10-28.json"),
        }
        self.requests = []
        self.location_calls = 0
        self.search_calls = 0

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        url = request.full_url
        if url.startswith(lrp.LOCATIONS_URL):
            self.location_calls += 1
            return _reply(self.locations)
        self.search_calls += 1
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        key = (query["fromLocationId"][0], query["toLocationId"][0])
        return _reply(self.searches.get(key, EMPTY_ROUTES))

    def last_search_query(self):
        url = [r.full_url for r in self.requests if not r.full_url.startswith(lrp.LOCATIONS_URL)][-1]
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


class _LocationTestCase(unittest.TestCase):
    def setUp(self):
        lrp._reset_location_cache_for_tests()
        fx._reset_for_tests()


class PlaceNormalisationTests(unittest.TestCase):
    def test_folds_turkish_and_european_letters(self):
        self.assertEqual(lrp._fold("İstanbul"), "istanbul")
        self.assertEqual(lrp._fold("Budapeşte"), "budapeste")
        self.assertEqual(lrp._fold("Münih"), "munih")
        self.assertEqual(lrp._fold("Varşova"), "varsova")
        self.assertEqual(lrp._fold("Košice"), "kosice")
        self.assertEqual(lrp._fold("Kraków"), "krakow")
        self.assertEqual(lrp._fold("Praha hl.n."), "praha hl n")
        self.assertEqual(lrp._fold("  Łódź  "), "lodz")

    def test_turkish_alias_table_points_at_the_same_city_as_the_english_name(self):
        index = lrp._build_index(json.loads(load_fixture("regiojet_locations_subset.json").decode("utf-8")))
        for turkish, english in TURKISH_ALIASES.items():
            expected = index.resolve(english)
            self.assertIsNotNone(expected, english)
            self.assertEqual(index.resolve(turkish), expected, turkish)


class LocationResolutionTests(_LocationTestCase):
    def _provider(self, router=None):
        self.router = router or RegioJetRouter()
        return LiveRailProvider(eur_try_rate=50.0)

    def test_turkish_names_resolve_to_regiojet_cities(self):
        provider = self._provider()
        with mock.patch(URLOPEN, side_effect=self.router):
            for name in ("Viyana", "Prag", "Budapeşte", "Münih", "Varşova", "Krakov", "Brno", "Košice"):
                self.assertTrue(provider.can_resolve(name), name)

    def test_unknown_places_do_not_resolve(self):
        provider = self._provider()
        with mock.patch(URLOPEN, side_effect=self.router):
            self.assertFalse(provider.can_resolve("İstanbul"))
            self.assertFalse(provider.can_resolve("Zagreb"))  # not in RegioJet's list
            self.assertFalse(provider.can_resolve("Graz"))
            self.assertFalse(provider.can_resolve(""))

    def test_many_places_share_one_cached_locations_request(self):
        # can_resolve reads the cached locations index: no request per place, known or unknown.
        provider = self._provider()
        with mock.patch(URLOPEN, side_effect=self.router):
            for name in ("Prag", "Viyana", "Athens", "Istanbul", "Atlantis", "Prag", "Budapeşte"):
                provider.can_resolve(name)
        self.assertEqual(self.router.location_calls, 1)
        self.assertEqual(self.router.search_calls, 0)

    def test_station_name_with_city_prefix_uses_station_id(self):
        provider = self._provider()
        with mock.patch(URLOPEN, side_effect=self.router):
            provider.search("Budapest Keleti", "Praha", "2026-10-28")
        query = self.router.last_search_query()
        self.assertEqual(query["fromLocationType"], ["STATION"])
        self.assertEqual(query["fromLocationId"], ["7063331001"])  # Budapest Keleti pu.
        self.assertEqual(query["toLocationType"], ["CITY"])

    def test_city_name_uses_city_type_and_id(self):
        provider = self._provider()
        with mock.patch(URLOPEN, side_effect=self.router):
            provider.search("Viyana", "Budapeşte", "2026-10-28")
        query = self.router.last_search_query()
        self.assertEqual(query["fromLocationType"], ["CITY"])
        self.assertEqual(query["fromLocationId"], [VIENNA])
        self.assertEqual(query["toLocationType"], ["CITY"])
        self.assertEqual(query["toLocationId"], [BUDAPEST])
        self.assertEqual(query["departureDate"], ["2026-10-28"])

    def test_unresolvable_place_returns_none_without_search_request(self):
        provider = self._provider()
        with mock.patch(URLOPEN, side_effect=self.router):
            self.assertIsNone(provider.search("İstanbul", "Praha", "2026-10-28"))
        self.assertEqual(self.router.search_calls, 0)

    def test_locations_failure_returns_none(self):
        router = RegioJetRouter(locations=urllib.error.URLError("down"))
        provider = self._provider(router)
        with mock.patch(URLOPEN, side_effect=router):
            self.assertFalse(provider.can_resolve("Prag"))
            self.assertIsNone(provider.search("Prag", "Viyana", "2026-10-28"))
        self.assertEqual(router.search_calls, 0)

    def test_locations_cached_for_24_hours(self):
        clock = FakeClock(1000.0)
        provider = self._provider()
        with mock.patch.object(lrp, "_now", clock), mock.patch(URLOPEN, side_effect=self.router):
            self.assertTrue(provider.can_resolve("Prag"))
            clock.now += 23 * 3600
            self.assertTrue(provider.can_resolve("Viyana"))
            self.assertEqual(self.router.location_calls, 1)
            clock.now += 2 * 3600
            self.assertTrue(provider.can_resolve("Viyana"))
            self.assertEqual(self.router.location_calls, 2)

    def test_failed_locations_refresh_retries_after_5_minutes(self):
        clock = FakeClock(1000.0)
        router = RegioJetRouter(locations=urllib.error.URLError("down"))
        provider = self._provider(router)
        with mock.patch.object(lrp, "_now", clock), mock.patch(URLOPEN, side_effect=router):
            self.assertFalse(provider.can_resolve("Prag"))
            clock.now += 4 * 60
            self.assertFalse(provider.can_resolve("Prag"))
            self.assertEqual(router.location_calls, 1)
            clock.now += 2 * 60
            self.assertFalse(provider.can_resolve("Prag"))
            self.assertEqual(router.location_calls, 2)

    def test_stale_locations_kept_when_refresh_fails(self):
        clock = FakeClock(1000.0)
        router = RegioJetRouter()
        provider = self._provider(router)
        with mock.patch.object(lrp, "_now", clock), mock.patch(URLOPEN, side_effect=router):
            self.assertTrue(provider.can_resolve("Prag"))
            clock.now += 25 * 3600
            router.locations = urllib.error.URLError("down")
            self.assertTrue(provider.can_resolve("Prag"))
            self.assertEqual(router.location_calls, 2)


class SearchParsingTests(_LocationTestCase):
    def _search(self, _pair, *args, **kwargs):
        # _pair documents which fixture pair the call is expected to hit; the router serves all four.
        router = RegioJetRouter()
        provider = LiveRailProvider(eur_try_rate=50.0)
        with mock.patch(URLOPEN, side_effect=router):
            result = provider.search(*args, **kwargs)
        return result, router, provider

    def test_vienna_to_budapest_default_modes(self):
        segs, _, _ = self._search(CITY_VIE_BUD, "Viyana", "Budapeşte", "2026-10-28")
        # 4 direct trains plus the 22:50 bus (one transfer, allowed by default); 2026-10-29 excluded.
        self.assertEqual([s.departure_time for s in segs], ["07:34", "10:27", "13:27", "17:27", "22:50"])
        first = segs[0]
        self.assertEqual(first.origin_station, "Viyana")
        self.assertEqual(first.dest_station, "Budapeşte")
        self.assertEqual((first.departure_time, first.arrival_time, first.duration_mins), ("07:34", "10:02", 148))
        self.assertEqual(first.mode, "train")
        self.assertEqual(first.operator, "RegioJet")
        self.assertEqual(first.date, "2026-10-28")
        self.assertFalse(first.is_estimated)
        self.assertEqual(first.price_eur, 9.0)
        self.assertEqual(first.price_try, 450.0)  # 9.0 EUR * 50.0
        self.assertFalse(first.price_is_estimated)

        bus = segs[4]
        self.assertEqual(bus.mode, "bus")
        self.assertEqual((bus.departure_time, bus.arrival_time, bus.duration_mins), ("22:50", "06:05", 435))
        self.assertEqual(bus.price_eur, 20.8)

    def test_modes_filter_drops_buses(self):
        segs, _, _ = self._search(CITY_VIE_BUD, "Viyana", "Budapeşte", "2026-10-28", modes=("TRAIN",))
        self.assertEqual([s.mode for s in segs], ["train"] * 4)
        self.assertNotIn("22:50", [s.departure_time for s in segs])

    def test_bus_only_route_has_no_train_results(self):
        segs, _, _ = self._search(CITY_PRG_BER, "Praha", "Berlín", "2026-10-28", modes=("TRAIN",))
        self.assertEqual(segs, [])

    def test_bus_only_route_is_returned_with_bus_mode(self):
        segs, _, _ = self._search(CITY_PRG_BER, "Praha", "Berlín", "2026-10-28")
        self.assertEqual(len(segs), 6)  # six departures on 2026-10-28
        self.assertTrue(all(s.mode == "bus" for s in segs))
        self.assertEqual(segs[0].departure_time, "06:30")
        self.assertEqual(segs[0].duration_mins, 270)

    def test_transfers_filter(self):
        segs, _, _ = self._search(CITY_VIE_BUD, "Viyana", "Budapeşte", "2026-10-28", max_transfers=0)
        self.assertEqual([s.departure_time for s in segs], ["07:34", "10:27", "13:27", "17:27"])

    def test_mixed_bus_and_train_route_maps_to_train_mode(self):
        segs, _, _ = self._search(CITY_BTS_PRG, "Bratislava", "Praha", "2026-10-28")
        mixed = [s for s in segs if s.departure_time == "08:30"]
        self.assertEqual(len(mixed), 1)
        self.assertEqual(mixed[0].mode, "train")
        self.assertEqual(mixed[0].duration_mins, 386)
        self.assertEqual(mixed[0].price_eur, 34.8)

    def test_mixed_route_excluded_by_train_only_or_zero_transfers(self):
        train_only, _, _ = self._search(CITY_BTS_PRG, "Bratislava", "Praha", "2026-10-28", modes=("TRAIN",))
        self.assertEqual([s.departure_time for s in train_only], ["05:17", "08:26", "12:26", "16:15"])
        zero_transfers, _, _ = self._search(CITY_BTS_PRG, "Bratislava", "Praha", "2026-10-28", max_transfers=0)
        self.assertNotIn("08:30", [s.departure_time for s in zero_transfers])
        self.assertNotIn("17:30", [s.departure_time for s in zero_transfers])

    def test_sold_out_connections_are_skipped(self):
        segs, _, _ = self._search(CITY_BRN_VIE, "Brno", "Viyana", "2026-10-28")
        # 15 connections on the day: 5 are sold out (bookable False, priceFrom 0) and are dropped.
        self.assertEqual(len(segs), 10)
        self.assertTrue(all(s.price_eur and s.price_eur > 0 for s in segs))
        self.assertNotIn("08:48", [s.departure_time for s in segs])
        self.assertNotIn("11:48", [s.departure_time for s in segs])

    def test_train_only_from_brno_to_vienna(self):
        segs, _, _ = self._search(CITY_BRN_VIE, "Brno", "Viyana", "2026-10-28", modes=("TRAIN",))
        self.assertEqual([(s.departure_time, s.price_eur) for s in segs], [("15:48", 17.9), ("18:48", 10.9)])

    def test_earliest_departure_filter_applies_to_search(self):
        segs, _, _ = self._search(CITY_VIE_BUD, "Viyana", "Budapeşte", "2026-10-28", earliest_dep_time="12:00")
        self.assertEqual([s.departure_time for s in segs], ["13:27", "17:27", "22:50"])

    def test_day_with_no_connections_returns_empty_list(self):
        segs, _, _ = self._search(CITY_VIE_BUD, "Viyana", "Budapeşte", "2026-11-30")
        self.assertEqual(segs, [])

    def test_http_error_returns_none_and_is_not_cached(self):
        router = RegioJetRouter(searches={CITY_VIE_BUD: urllib.error.HTTPError(lrp.ROUTE_SEARCH_URL, 503, "x", None, None)})
        provider = LiveRailProvider(eur_try_rate=50.0)
        with mock.patch(URLOPEN, side_effect=router):
            self.assertIsNone(provider.search("Viyana", "Budapeşte", "2026-10-28"))
            self.assertIsNone(provider.search("Viyana", "Budapeşte", "2026-10-28"))
        self.assertEqual(router.search_calls, 2)

    def test_bad_json_and_bad_shape_return_none(self):
        provider = LiveRailProvider(eur_try_rate=50.0)
        for body in (b"<html>503</html>", b'{"nope": []}', b'{"routes": "x"}'):
            router = RegioJetRouter(searches={CITY_VIE_BUD: body})
            with mock.patch(URLOPEN, side_effect=router):
                self.assertIsNone(provider.search("Viyana", "Budapeşte", "2026-10-28"), body)

    def test_malformed_date_returns_none_without_request(self):
        router = RegioJetRouter()
        provider = LiveRailProvider(eur_try_rate=50.0)
        with mock.patch(URLOPEN, side_effect=router):
            self.assertIsNone(provider.search("Viyana", "Budapeşte", "28.10.2026"))
        self.assertEqual(router.requests, [])

    def test_price_try_follows_current_rate_on_return(self):
        router = RegioJetRouter()
        provider = LiveRailProvider(eur_try_rate=40.0)
        with mock.patch(URLOPEN, side_effect=router):
            first = provider.search("Viyana", "Budapeşte", "2026-10-28")
            self.assertEqual(first[0].price_try, 360.0)  # 9.0 * 40
            provider.eur_try_rate = 60.0
            again = provider.search("Viyana", "Budapeşte", "2026-10-28")
        self.assertEqual(again[0].price_try, 540.0)  # cached connection, new rate
        self.assertEqual(router.search_calls, 1)

    def test_rate_comes_from_fx_when_not_given(self):
        router = RegioJetRouter()
        env = {k: v for k, v in os.environ.items() if k != "TRAVELPLAN_EUR_TRY"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(fx, "get_eur_try_rate", return_value=(47.5, "frankfurter.dev (ECB)", True)), \
                mock.patch(URLOPEN, side_effect=router):
            provider = LiveRailProvider()
            segs = provider.search("Viyana", "Budapeşte", "2026-10-28")
        self.assertEqual(segs[0].price_try, 427.5)  # 9.0 * 47.5


class SearchCacheTests(_LocationTestCase):
    def test_second_search_served_from_cache(self):
        clock = FakeClock()
        router = RegioJetRouter()
        provider = LiveRailProvider(eur_try_rate=50.0, clock=clock)
        with mock.patch(URLOPEN, side_effect=router):
            first = provider.search("Viyana", "Budapeşte", "2026-10-28")
            second = provider.search("Viyana", "Budapeşte", "2026-10-28", earliest_dep_time="12:00")
        self.assertEqual(router.search_calls, 1)
        self.assertEqual(len(first), 5)
        self.assertEqual(len(second), 3)  # filter applied on the cached day

    def test_entry_expires_after_30_minutes(self):
        clock = FakeClock(1000.0)
        router = RegioJetRouter()
        provider = LiveRailProvider(eur_try_rate=50.0, clock=clock)
        with mock.patch(URLOPEN, side_effect=router):
            provider.search("Viyana", "Budapeşte", "2026-10-28")
            clock.now = 1000.0 + 29 * 60
            provider.search("Viyana", "Budapeşte", "2026-10-28")
            self.assertEqual(router.search_calls, 1)
            clock.now = 1000.0 + 31 * 60
            provider.search("Viyana", "Budapeşte", "2026-10-28")
            self.assertEqual(router.search_calls, 2)

    def test_cache_key_includes_modes_and_date(self):
        clock = FakeClock()
        router = RegioJetRouter()
        provider = LiveRailProvider(eur_try_rate=50.0, clock=clock)
        with mock.patch(URLOPEN, side_effect=router):
            provider.search("Viyana", "Budapeşte", "2026-10-28")
            provider.search("Viyana", "Budapeşte", "2026-10-28", modes=("TRAIN",))
            provider.search("Viyana", "Budapeşte", "2026-10-29")
        self.assertEqual(router.search_calls, 3)

    def test_returned_segments_do_not_alias_cache(self):
        clock = FakeClock()
        router = RegioJetRouter()
        provider = LiveRailProvider(eur_try_rate=50.0, clock=clock)
        with mock.patch(URLOPEN, side_effect=router):
            first = provider.search("Viyana", "Budapeşte", "2026-10-28")
            first[0].departure_time = "99:99"
            first[0].price_eur = 0.0
            again = provider.search("Viyana", "Budapeşte", "2026-10-28")
        self.assertEqual(again[0].departure_time, "07:34")
        self.assertEqual(again[0].price_eur, 9.0)


class ConstructorRateTests(unittest.TestCase):
    def test_explicit_rate_beats_env(self):
        with mock.patch.dict(os.environ, {"TRAVELPLAN_EUR_TRY": "45.5"}):
            provider = LiveRailProvider(eur_try_rate=50.0)
        self.assertEqual(provider.eur_try_rate, 50.0)

    def test_without_env_or_explicit_rate_the_live_rate_is_used(self):
        env = {k: v for k, v in os.environ.items() if k != "TRAVELPLAN_EUR_TRY"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(fx, "get_eur_try_rate", return_value=(55.1, "open.er-api.com", True)):
            provider = LiveRailProvider()
            self.assertEqual(provider.eur_try_rate, 55.1)


class StationNameTests(_LocationTestCase):
    def test_search_segments_carry_regiojet_station_names(self):
        router = RegioJetRouter()
        with mock.patch(URLOPEN, side_effect=router):
            segs = LiveRailProvider(eur_try_rate=50.0).search("Viyana", "Budapeşte", "2026-10-28")
        first = segs[0]  # 07:34 train
        self.assertEqual((first.origin_station, first.dest_station), ("Viyana", "Budapeşte"))
        self.assertEqual(first.origin_station_name, "Vienna - Hbf")
        self.assertEqual(first.dest_station_name, "Budapest - Kelenföld")
        bus = segs[4]  # 22:50 bus from Vienna's Südtiroler Platz stop to Népliget
        self.assertEqual(bus.origin_station_name, "Vienna - Hbf Main train station (Südtiroler Platz)")
        self.assertEqual(bus.dest_station_name, "Budapest - Népliget aut.áll.")
        self.assertTrue(all(s.origin_station_name and s.dest_station_name for s in segs))

    def test_unknown_station_id_gives_no_name(self):
        payload = json.dumps({"routes": [{
            "departureTime": "2026-10-28T07:00:00.000+01:00",
            "arrivalTime": "2026-10-28T09:00:00.000+01:00",
            "bookable": True, "vehicleTypes": ["TRAIN"], "transfersCount": 0, "priceFrom": 9.0,
            "departureStationId": 999, "arrivalStationId": 5404232005,
        }]}).encode("utf-8")
        router = RegioJetRouter(searches={CITY_VIE_BUD: payload})
        with mock.patch(URLOPEN, side_effect=router):
            segs = LiveRailProvider(eur_try_rate=50.0).search("Viyana", "Budapeşte", "2026-10-28")
        self.assertEqual(len(segs), 1)
        self.assertIsNone(segs[0].origin_station_name)
        self.assertEqual(segs[0].dest_station_name, "Budapest - Kelenföld")

    def test_cached_search_keeps_station_names(self):
        router = RegioJetRouter()
        provider = LiveRailProvider(eur_try_rate=50.0, clock=FakeClock())
        with mock.patch(URLOPEN, side_effect=router):
            provider.search("Viyana", "Budapeşte", "2026-10-28")
            again = provider.search("Viyana", "Budapeşte", "2026-10-28")
        self.assertEqual(router.search_calls, 1)
        self.assertEqual(again[0].origin_station_name, "Vienna - Hbf")

    def test_station_name_lookup_takes_only_integer_ids(self):
        index = lrp._build_index(json.loads(load_fixture("regiojet_locations_subset.json").decode("utf-8")))
        self.assertEqual(index.station_name(5404232005), "Budapest - Kelenföld")
        self.assertIsNone(index.station_name(999))
        self.assertIsNone(index.station_name(True))
        self.assertIsNone(index.station_name("5404232005"))
        self.assertIsNone(index.station_name(None))

    def test_corridor_segments_carry_station_names(self):
        provider = LiveRailProvider(eur_try_rate=50.0)
        body = load_fixture("regiojet_bts_prg_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)):
            segs = provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")
        self.assertTrue(segs)
        self.assertTrue(all(s.origin_station_name == "Bratislava hl.st." for s in segs))
        self.assertTrue(all(s.dest_station_name == "Praha hl.n." for s in segs))


class LegacyExportTests(unittest.TestCase):
    def test_legacy_names_still_exported(self):
        self.assertEqual(lrp.ROUTE_SEARCH_URL, "https://brn-ybus-pubapi.sa.cz/restapi/routes/search/simple")
        self.assertEqual(DEFAULT_EUR_TRY_RATE, fx.DEFAULT_EUR_TRY_RATE)


class LiveRailHealthTests(_LocationTestCase):
    """health_status(): unknown -> ok/error transitions, request counting, and what is not a request."""

    SHAPE = {
        "source", "status", "last_ok_at", "last_error", "last_error_at", "last_http_status",
        "consecutive_failures", "requests", "cached_searches",
    }

    def _provider(self):
        return LiveRailProvider(eur_try_rate=50.0, clock=FakeClock())

    def test_fresh_provider_is_unknown_with_flixbus_shape(self):
        health = self._provider().health_status()
        self.assertEqual(set(health), self.SHAPE)
        self.assertEqual(health["status"], "unknown")
        self.assertEqual(health["source"], "regiojet")
        self.assertIsNone(health["last_ok_at"])
        self.assertIsNone(health["last_error"])
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(health["requests"], 0)
        self.assertEqual(health["cached_searches"], 0)

    def test_corridor_success_sets_ok_and_counts_request(self):
        provider = self._provider()
        body = load_fixture("regiojet_wien_prg_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)):
            provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28")
        health = provider.health_status()
        self.assertEqual(health["status"], "ok")
        self.assertIsNotNone(health["last_ok_at"])
        self.assertIsNone(health["last_error"])
        self.assertEqual(health["requests"], 1)
        self.assertEqual(health["cached_searches"], 1)

    def test_corridor_failure_sets_error_and_counts_consecutive_failures(self):
        provider = self._provider()
        err = urllib.error.HTTPError(lrp.ROUTE_SEARCH_URL, 503, "Service Unavailable", None, None)
        with mock.patch(URLOPEN, side_effect=[err, urllib.error.URLError("down")]):
            provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28")
            provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-29")
        health = provider.health_status()
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["consecutive_failures"], 2)
        self.assertEqual(health["last_http_status"], None)  # the last failure was a network error
        self.assertIn("down", health["last_error"])
        self.assertIsNotNone(health["last_error_at"])
        self.assertIsNone(health["last_ok_at"])
        self.assertEqual(health["requests"], 2)

    def test_http_status_is_kept_for_an_http_failure(self):
        provider = self._provider()
        err = urllib.error.HTTPError(lrp.ROUTE_SEARCH_URL, 503, "Service Unavailable", None, None)
        with mock.patch(URLOPEN, side_effect=err):
            provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28")
        self.assertEqual(provider.health_status()["last_http_status"], 503)

    def test_success_after_failures_recovers_to_ok(self):
        provider = self._provider()
        body = load_fixture("regiojet_wien_prg_2026-10-28.json")
        with mock.patch(URLOPEN, side_effect=[urllib.error.URLError("down"), FakeResponse(body)]):
            provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28")
            self.assertEqual(provider.health_status()["status"], "error")
            provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28")
        health = provider.health_status()
        self.assertEqual(health["status"], "ok")
        self.assertIsNone(health["last_error"])
        self.assertIsNone(health["last_error_at"])
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(health["requests"], 2)

    def test_unsupported_corridor_and_bad_date_change_nothing(self):
        provider = self._provider()
        with mock.patch(URLOPEN) as opened:
            self.assertIsNone(provider.get_schedules("Brno", "Praha hl.n.", "2026-10-28"))
            self.assertIsNone(provider.get_schedules("Wien Hbf", "Praha hl.n.", "28.10.2026"))
            self.assertIsNone(provider.search("Viyana", "Budapeşte", "28.10.2026"))
            opened.assert_not_called()
        health = provider.health_status()
        self.assertEqual(health["status"], "unknown")
        self.assertEqual(health["requests"], 0)
        self.assertEqual(health["consecutive_failures"], 0)

    def test_search_success_counts_locations_and_search_requests(self):
        provider = self._provider()
        router = RegioJetRouter()
        with mock.patch(URLOPEN, side_effect=router):
            provider.search("Viyana", "Budapeşte", "2026-10-28")
        health = provider.health_status()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["requests"], 2)  # locations + city-level search
        self.assertEqual(health["cached_searches"], 1)

    def test_search_failure_sets_error(self):
        provider = self._provider()
        router = RegioJetRouter()
        with mock.patch(URLOPEN, side_effect=router):
            provider.search("Viyana", "Budapeşte", "2026-10-28")  # ok, fills the locations cache
        router_fail = RegioJetRouter(searches={CITY_VIE_BUD: urllib.error.URLError("boom")})
        provider._search_cache.clear()
        with mock.patch(URLOPEN, side_effect=router_fail):
            self.assertIsNone(provider.search("Viyana", "Budapeşte", "2026-10-28"))
        health = provider.health_status()
        self.assertEqual(health["status"], "error")
        self.assertIn("boom", health["last_error"])
        self.assertEqual(health["consecutive_failures"], 1)

    def test_locations_failure_is_recorded_by_can_resolve(self):
        provider = self._provider()
        with mock.patch(URLOPEN, side_effect=urllib.error.URLError("locations down")):
            self.assertFalse(provider.can_resolve("Prag"))
        health = provider.health_status()
        self.assertEqual(health["status"], "error")
        self.assertIn("locations", health["last_error"])
        self.assertEqual(health["requests"], 1)

    def test_unknown_place_is_not_a_failure(self):
        provider = self._provider()
        router = RegioJetRouter()
        with mock.patch(URLOPEN, side_effect=router):
            self.assertIsNone(provider.search("Atlantis", "Praha", "2026-10-28"))
        health = provider.health_status()
        self.assertEqual(health["status"], "ok")  # the locations list itself was fetched fine
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(router.search_calls, 0)

    def test_cached_hit_makes_no_request(self):
        provider = self._provider()
        body = load_fixture("regiojet_wien_prg_2026-10-28.json")
        with mock.patch(URLOPEN, return_value=FakeResponse(body)) as opened:
            provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28")
            provider.get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28")
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(provider.health_status()["requests"], 1)


if __name__ == "__main__":
    unittest.main()
