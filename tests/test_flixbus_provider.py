"""FlixBus provider tests. No network: urlopen is replaced by a fake that serves fixtures.

The fixtures in tests/fixtures/flixbus are trimmed real responses of the public FlixBus
endpoints (captured 2026-10-19 for departures on 2026-11-05).
"""

import copy
import json
import os
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from travel_engine import fx
from travel_engine.providers import flixbus_provider as fp
from travel_engine.providers.flixbus_provider import FlixBusProvider
from travel_engine.providers.rail_bus_provider import ScheduledSegment

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "flixbus")

MILAN = "40ddcc6e-8646-11e6-9066-549f350fcb0c"
VENICE = "40dea03b-8646-11e6-9066-549f350fcb0c"
PARIS = "40de8964-8646-11e6-9066-549f350fcb0c"
LYON = "40df89c1-8646-11e6-9066-549f350fcb0c"
MUNICH = "40d901a5-8646-11e6-9066-549f350fcb0c"
PRAGUE = "40de1ad1-8646-11e6-9066-549f350fcb0c"
VIENNA = "40de1f31-8646-11e6-9066-549f350fcb0c"
BUDAPEST = "40de6527-8646-11e6-9066-549f350fcb0c"
THESSALONIKI = "ab6b59e0-0ea1-4783-833f-070f79f1a2ca"
SOFIA = "40e09335-8646-11e6-9066-549f350fcb0c"
ISTANBUL_AVRUPA = "99c50ec5-3ecb-11ea-8017-02437075395e"
ISTANBUL_ANADOLU = "523ae3fc-b96a-4b21-ad03-fe5bc6ce818a"

DAY = "2026-11-05"

# Synthetic autocomplete answers (real ids, trimmed to the fields the provider reads).
def _city(name, cid, country="xx"):
    return {"name": name, "id": cid, "country": country, "is_flixbus_city": True}

AUTOCOMPLETE_EXTRA = {
    "Paris": [_city("Paris", PARIS, "fr"), _city("Paris CDG Airport", "472ef68b-be37-43d2-94ce-72d570493e52", "fr")],
    "Lyon": [_city("Lyon", LYON, "fr")],
    "Munich": [_city("Munich", MUNICH, "de")],
    "Prague": [_city("Prague", PRAGUE, "cz")],
    "Vienna": [_city("Vienna", VIENNA, "at")],
    "Budapest": [_city("Budapest", BUDAPEST, "hu")],
    "Thessaloniki": [_city("Thessaloniki", THESSALONIKI, "gr")],
    "Sofia": [_city("Sofia", SOFIA, "bg"), _city("Sofia Airport", "738e6cce-15fb-444f-9819-cfa7336b4c99", "bg")],
    "Istanbul": [_city("İstanbul Anadolu", ISTANBUL_ANADOLU, "tr"), _city("İstanbul Avrupa", ISTANBUL_AVRUPA, "tr")],
    "İstanbul Avrupa": [_city("İstanbul Avrupa", ISTANBUL_AVRUPA, "tr")],
}

SEARCH_FIXTURES = {
    (MILAN, VENICE): "search_milan_venice_2026-11-05.json",
    (PARIS, LYON): "search_paris_lyon_2026-11-05.json",
    (MUNICH, PRAGUE): "search_munich_prague_2026-11-05.json",
    (VIENNA, BUDAPEST): "search_vienna_budapest_2026-11-05.json",
    (THESSALONIKI, SOFIA): "search_thessaloniki_sofia_2026-11-05.json",
    (ISTANBUL_AVRUPA, SOFIA): "search_istanbul_sofia_2026-11-05.json",
}


def load_bytes(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def load_json(name: str):
    return json.loads(load_bytes(name).decode("utf-8"))


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


class FakeFlixBus:
    """Serves the autocomplete and search endpoints from fixtures; records every call."""

    def __init__(self):
        self.autocomplete = {
            "Milan": load_json("autocomplete_milan.json"),
            "Venice": load_json("autocomplete_venice.json"),
            "Venezia": load_json("autocomplete_venice.json"),
        }
        for name, cities in AUTOCOMPLETE_EXTRA.items():
            self.autocomplete.setdefault(name, cities)
        self.search_bodies = {pair: load_bytes(name) for pair, name in SEARCH_FIXTURES.items()}
        self.error = None          # an exception instance or an HTTPError, raised on every call
        self.calls = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if "/autocomplete/cities" in url:
            return FakeResponse(json.dumps(self.autocomplete.get(query["q"][0], [])).encode("utf-8"))
        if "/search/service/v4/search" in url:
            pair = (query["from_city_id"][0], query["to_city_id"][0])
            if pair in self.search_bodies:
                return FakeResponse(self.search_bodies[pair])
            return FakeResponse(json.dumps({"trips": []}).encode("utf-8"))
        raise AssertionError(f"unexpected URL {url}")

    def count(self, part: str) -> int:
        return sum(1 for url in self.calls if part in url)


class FlixBusTestCase(unittest.TestCase):
    def setUp(self):
        self.net = FakeFlixBus()
        self.clock = FakeClock()
        self.sleeps = []
        patches = [
            mock.patch.object(fp.urllib.request, "urlopen", side_effect=self.net),
            mock.patch.object(fp, "_now", self.clock),
            mock.patch.object(fp, "_sleep", side_effect=self.sleeps.append),
            mock.patch.object(fx, "get_eur_try_rate", return_value=(50.0, "test", True)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        fp._limiter.last = None
        self.addCleanup(setattr, fp._limiter, "last", None)

    def provider(self, **kwargs) -> FlixBusProvider:
        kwargs.setdefault("min_interval_secs", 0)
        return FlixBusProvider(**kwargs)

    def search_fixture(self, pair):
        return json.loads(self.net.search_bodies[pair].decode("utf-8"))

    def set_search_body(self, pair, payload):
        self.net.search_bodies[pair] = json.dumps(payload).encode("utf-8")


class FlixBusParsingTests(FlixBusTestCase):
    def test_milan_venice_fixture_segments(self):
        segs = self.provider().search("Milan", "Venice", DAY, max_results=10, max_transfers=1)

        # Six results in the fixture: one is for the next day and is dropped.
        self.assertEqual(len(segs), 5)
        self.assertTrue(all(isinstance(s, ScheduledSegment) for s in segs))
        first = segs[0]
        self.assertEqual(first.origin_station, "Milan")
        self.assertEqual(first.dest_station, "Venice")
        self.assertEqual(first.departure_time, "00:10")
        self.assertEqual(first.arrival_time, "04:45")
        self.assertEqual(first.duration_mins, 4 * 60 + 35)
        self.assertEqual(first.date, DAY)
        self.assertEqual(first.mode, "bus")
        self.assertEqual(first.operator, "FlixBus")
        self.assertEqual(first.source, "flixbus")
        self.assertFalse(first.is_estimated)
        self.assertFalse(first.price_is_estimated)
        self.assertEqual(first.price_eur, 17.48)  # total_with_platform_fee
        self.assertEqual(first.price_try, 874.0)  # 17.48 EUR at the mocked 50.0 rate
        self.assertEqual(first.origin_station_name, "Milan (Lampugnano bus station)")
        self.assertIsNotNone(first.dest_station_name)

    def test_results_sorted_by_departure(self):
        segs = self.provider().search("Milan", "Venice", DAY, max_results=10, max_transfers=1)
        times = [s.departure_time for s in segs]
        self.assertEqual(times, sorted(times))

    def test_transfer_connection_counted_as_one_transfer(self):
        prov = self.provider()
        with_transfer = prov.search("Milan", "Venice", DAY, max_results=10, max_transfers=1)
        direct_only = prov.search("Milan", "Venice", DAY, max_results=10, max_transfers=0)

        self.assertEqual(len(with_transfer), 5)
        self.assertEqual(len(direct_only), 4)
        self.assertNotIn("12:35", [s.departure_time for s in direct_only])
        transfer = next(s for s in with_transfer if s.departure_time == "12:35")
        self.assertEqual(transfer.arrival_time, "16:50")
        self.assertEqual(transfer.duration_mins, 4 * 60 + 15)
        self.assertEqual(transfer.price_eur, 22.98)

    def test_fast_bus_product_is_direct(self):
        segs = self.provider().search("Munich", "Prague", DAY, max_results=10, max_transfers=0)
        fast = [s for s in segs if s.departure_time == "05:05"]
        self.assertEqual(len(fast), 1)
        self.assertEqual(fast[0].arrival_time, "10:35")
        self.assertEqual(fast[0].duration_mins, 5 * 60 + 30)
        self.assertEqual(fast[0].price_eur, 18.98)
        self.assertEqual(fast[0].dest_station_name, "Prague (Main Station)")

    def test_paris_lyon_fixture(self):
        segs = self.provider().search("Paris", "Lyon", DAY, max_results=10)
        self.assertEqual(len(segs), 4)  # the fifth fixture result is for the next day
        self.assertEqual([s.departure_time for s in segs], ["00:30", "01:10", "06:40", "07:00"])
        self.assertTrue(all(s.mode == "bus" and s.source == "flixbus" for s in segs))
        self.assertEqual(min(s.price_eur for s in segs), 13.98)

    def test_vienna_budapest_fixture(self):
        segs = self.provider().search("Vienna", "Budapest", DAY, max_results=10)
        self.assertEqual(len(segs), 4)
        self.assertTrue(all(s.origin_station == "Vienna" and s.dest_station == "Budapest" for s in segs))

    def test_thessaloniki_sofia_fixture(self):
        segs = self.provider().search("Thessaloniki", "Sofia", DAY, max_results=10)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].departure_time, "17:30")
        self.assertEqual(segs[0].operator, "FlixBus")

    def test_istanbul_sofia_partner_operator(self):
        segs = self.provider().search("İstanbul Avrupa", "Sofia", DAY, max_results=10)
        self.assertEqual([s.departure_time for s in segs], ["11:00", "23:00"])
        first = segs[0]
        self.assertEqual(first.operator, "Kamil Koç")
        self.assertEqual(first.arrival_time, "18:30")
        self.assertEqual(first.duration_mins, 8 * 60 + 30)
        self.assertEqual(first.price_eur, 37.99)
        self.assertEqual(first.mode, "bus")

    def test_flixtrain_leg_gives_train_mode(self):
        payload = self.search_fixture((PARIS, LYON))
        trip = payload["trips"][0]
        brand_id = "train-brand-id"
        payload["brands"][brand_id] = {"name": "FlixTrain", "type": "core"}
        first_key = next(iter(trip["results"]))
        result = trip["results"][first_key]
        for leg in result["legs"]:
            leg["means_of_transport"] = "train"
            leg["brand_id"] = brand_id
        self.set_search_body((PARIS, LYON), payload)

        segs = self.provider().search("Paris", "Lyon", DAY, max_results=10)
        train = [s for s in segs if s.mode == "train"]
        self.assertEqual(len(train), 1)
        self.assertEqual(train[0].operator, "FlixTrain")

    def test_empty_trip_list_gives_empty_list(self):
        self.set_search_body((VENICE, MILAN), {"trips": []})
        self.assertEqual(self.provider().search("Venice", "Milan", DAY), [])


class FlixBusFilterTests(FlixBusTestCase):
    def test_sold_out_status_is_skipped(self):
        payload = self.search_fixture((MILAN, VENICE))
        results = payload["trips"][0]["results"]
        first_key = next(iter(results))
        results[first_key]["status"] = "sold_out"
        self.set_search_body((MILAN, VENICE), payload)

        segs = self.provider().search("Milan", "Venice", DAY, max_results=10, max_transfers=1)
        self.assertEqual(len(segs), 4)
        # Two fixture trips leave at 00:10; the sold-out one is gone, the other stays.
        self.assertEqual([s.departure_time for s in segs].count("00:10"), 1)

    def test_zero_seats_is_skipped(self):
        payload = self.search_fixture((MILAN, VENICE))
        results = payload["trips"][0]["results"]
        first_key = next(iter(results))
        results[first_key]["available"]["seats"] = 0
        self.set_search_body((MILAN, VENICE), payload)

        segs = self.provider().search("Milan", "Venice", DAY, max_results=10, max_transfers=1)
        self.assertEqual(len(segs), 4)

    def test_unpriced_trip_is_skipped(self):
        payload = self.search_fixture((PARIS, LYON))
        results = payload["trips"][0]["results"]
        for r in list(results.values())[:2]:
            r["price"]["total_with_platform_fee"] = 0
            r["price"]["total"] = 0
        self.set_search_body((PARIS, LYON), payload)

        segs = self.provider().search("Paris", "Lyon", DAY, max_results=10)
        self.assertEqual(len(segs), 2)

    def test_transfers_filter(self):
        prov = self.provider()
        self.assertEqual(len(prov.search("Milan", "Venice", DAY, max_results=10, max_transfers=0)), 4)
        self.assertEqual(len(prov.search("Milan", "Venice", DAY, max_results=10, max_transfers=1)), 5)

    def test_earliest_departure_filter(self):
        segs = self.provider().search("Paris", "Lyon", DAY, earliest_dep_time="06:00", max_results=10)
        self.assertEqual([s.departure_time for s in segs], ["06:40", "07:00"])

    def test_earliest_departure_filter_can_empty_the_day(self):
        self.assertEqual(self.provider().search("Paris", "Lyon", DAY, earliest_dep_time="23:59"), [])

    def test_max_results_keeps_earliest(self):
        segs = self.provider().search("Paris", "Lyon", DAY, max_results=2)
        self.assertEqual([s.departure_time for s in segs], ["00:30", "01:10"])

    def test_max_results_zero_gives_empty_list(self):
        self.assertEqual(self.provider().search("Paris", "Lyon", DAY, max_results=0), [])

    def test_fx_failure_uses_default_rate(self):
        with mock.patch.object(fx, "convert_to_try", return_value=None):
            segs = self.provider().search("Paris", "Lyon", DAY, max_results=1)
        self.assertEqual(segs[0].price_try, round(segs[0].price_eur * fx.DEFAULT_EUR_TRY_RATE, 2))


class FlixBusResolutionTests(FlixBusTestCase):
    def test_turkish_names_are_aliased_in_the_query(self):
        prov = self.provider()
        self.assertTrue(prov.can_resolve("Milano"))
        self.assertTrue(prov.can_resolve("Viyana"))
        self.assertTrue(prov.can_resolve("Prag"))
        self.assertTrue(prov.can_resolve("Münih"))
        self.assertTrue(prov.can_resolve("Budapeşte"))
        self.assertTrue(prov.can_resolve("Selanik"))
        self.assertTrue(prov.can_resolve("Sofya"))
        queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)["q"][0]
                   for u in self.net.calls if "autocomplete" in u]
        self.assertEqual(queries, ["Milan", "Vienna", "Prague", "Munich", "Budapest", "Thessaloniki", "Sofia"])

    def test_city_resolution_is_cached_for_seven_days(self):
        prov = self.provider()
        prov.can_resolve("Milan")
        prov.can_resolve("Milan")
        self.assertEqual(self.net.count("autocomplete"), 1)

        self.clock.now += 7 * 24 * 3600 + 1
        prov.can_resolve("Milan")
        self.assertEqual(self.net.count("autocomplete"), 2)

    def test_unknown_place_is_not_resolvable(self):
        self.assertFalse(self.provider().can_resolve("Atlantis"))
        self.assertIsNone(self.provider().search("Atlantis", "Venice", DAY))

    def test_istanbul_exact_name_wins(self):
        prov = self.provider()
        self.assertTrue(prov.can_resolve("İstanbul Avrupa"))
        self.assertEqual(prov._resolve_city("İstanbul Avrupa"), (ISTANBUL_AVRUPA, "İstanbul Avrupa"))

    def test_wien_and_praha_are_aliased(self):
        prov = self.provider()
        self.assertEqual(prov._resolve_city("Wien"), (VIENNA, "Vienna"))
        self.assertEqual(prov._resolve_city("Praha"), (PRAGUE, "Prague"))

    def test_word_prefix_fallback_does_not_match_partial_words(self):
        self.net.autocomplete["Lyo"] = [_city("Lyon", LYON, "fr")]
        self.assertIsNone(self.provider()._resolve_city("Lyo"))

    def test_plain_istanbul_takes_first_scored_city_with_that_leading_word(self):
        self.assertEqual(self.provider()._resolve_city("Istanbul"), (ISTANBUL_ANADOLU, "İstanbul Anadolu"))

    def test_same_city_gives_empty_list(self):
        self.assertEqual(self.provider().search("Milan", "Milano", DAY), [])


class FlixBusMultiCityTests(FlixBusTestCase):
    def test_istanbul_resolves_to_both_cities(self):
        prov = self.provider()
        self.assertEqual(
            prov._resolve_cities("Istanbul"),
            [(ISTANBUL_ANADOLU, "İstanbul Anadolu"), (ISTANBUL_AVRUPA, "İstanbul Avrupa")],
        )
        self.assertEqual(prov._resolve_city("Istanbul"), (ISTANBUL_ANADOLU, "İstanbul Anadolu"))

    def test_split_city_without_a_main_city_is_capped_at_two(self):
        # No city is named exactly "Hamburg": the two leading main cities are searched, a third is not.
        self.net.autocomplete["Hamburg"] = [
            _city("Hamburg Nord", "h1", "de"), _city("Hamburg Sud", "h2", "de"), _city("Hamburg Central", "h3", "de"),
        ]
        cities = self.provider()._resolve_cities("Hamburg")
        self.assertEqual([cid for cid, _ in cities], ["h1", "h2"])
        self.assertEqual(fp.MAX_CITIES_PER_PLACE, 2)

    def test_main_city_wins_over_its_airport_and_variants(self):
        self.net.autocomplete["Hamburg"] = [
            _city("Hamburg", "h1", "de"), _city("Hamburg Airport", "h2", "de"), _city("Hamburg Central", "h3", "de"),
        ]
        self.assertEqual(self.provider()._resolve_cities("Hamburg"), [("h1", "Hamburg")])

    def test_airport_city_is_skipped_for_a_city_named_in_the_place(self):
        self.net.autocomplete["Prague"] = [
            _city("Prague", PRAGUE, "cz"), _city("Prague Airport", "p-airport", "cz"),
        ]
        self.assertEqual(self.provider()._resolve_cities("Prague"), [(PRAGUE, "Prague")])

    def test_place_that_names_an_airport_keeps_the_airport_city(self):
        self.net.autocomplete["Sofia Airport"] = [_city("Sofia Airport", "738e6cce-15fb-444f-9819-cfa7336b4c99", "bg")]
        self.assertEqual(self.provider()._resolve_cities("Sofia Airport"),
                         [("738e6cce-15fb-444f-9819-cfa7336b4c99", "Sofia Airport")])

    def test_airport_only_place_keeps_its_airport_city(self):
        self.net.autocomplete["Ljubljana"] = [_city("Ljubljana Airport", "lj-airport", "si")]
        self.assertEqual(self.provider()._resolve_cities("Ljubljana"), [("lj-airport", "Ljubljana Airport")])

    def test_istanbul_search_merges_both_cities(self):
        # Anadolu pair: the Milan-Venice fixture trips (different stations, so nothing merges away).
        # Avrupa pair: the Istanbul-Sofia fixture trips (11:00 and 23:00).
        self.set_search_body((ISTANBUL_ANADOLU, SOFIA), self.search_fixture((MILAN, VENICE)))
        segs = self.provider().search("Istanbul", "Sofia", DAY, max_results=20)
        times = [s.departure_time for s in segs]
        self.assertEqual(len(segs), 7)
        self.assertEqual(times, sorted(times))
        self.assertIn("00:10", times)
        self.assertIn("11:00", times)
        self.assertEqual(self.net.count("search/service"), 2)  # 2 Istanbul cities x 1 Sofia city (Sofia Airport skipped)
        self.assertTrue(all(s.origin_station == "Istanbul" and s.dest_station == "Sofia" for s in segs))

    def test_identical_trip_from_both_cities_is_kept_once_at_lowest_price(self):
        avrupa = self.search_fixture((ISTANBUL_AVRUPA, SOFIA))
        dearer = json.loads(json.dumps(avrupa))
        for trip in dearer["trips"]:
            for res in trip["results"].values():
                price = res.get("price")
                if isinstance(price, dict) and price.get("total_with_platform_fee"):
                    price["total_with_platform_fee"] += 10
        self.set_search_body((ISTANBUL_ANADOLU, SOFIA), dearer)

        segs = self.provider().search("Istanbul", "Sofia", DAY, max_results=20)
        self.assertEqual([s.departure_time for s in segs], ["11:00", "23:00"])
        self.assertEqual(segs[0].price_eur, 37.99)  # the Avrupa fare, the cheaper copy

    def test_one_failing_city_pair_keeps_the_other_and_shows_the_error(self):
        real = self.net.__call__

        def flaky(request, timeout=None):
            url = request.full_url
            if "search/service" in url and f"from_city_id={ISTANBUL_ANADOLU}" in url:
                raise urllib.error.HTTPError(url, 500, "x", {}, None)
            return real(request, timeout)

        prov = self.provider()
        with mock.patch.object(fp.urllib.request, "urlopen", side_effect=flaky):
            segs = prov.search("Istanbul", "Sofia", DAY, max_results=20)
        self.assertEqual([s.departure_time for s in segs], ["11:00", "23:00"])
        health = prov.health_status()
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["last_http_status"], 500)

    def test_all_city_pairs_failing_gives_none(self):
        self.net.error = urllib.error.URLError("down")
        self.assertIsNone(self.provider().search("Istanbul", "Sofia", DAY))

    def test_same_city_across_variants_gives_empty_list(self):
        self.assertEqual(self.provider().search("Istanbul", "İstanbul Avrupa", DAY), [])


class FlixBusInputTests(FlixBusTestCase):
    def test_bad_date_returns_none_without_network(self):
        prov = self.provider()
        self.assertIsNone(prov.search("Milan", "Venice", "2026-13-40"))
        self.assertIsNone(prov.search("Milan", "Venice", "05.11.2026"))
        self.assertEqual(self.net.calls, [])
        self.assertEqual(prov.health_status()["status"], "unknown")

    def test_bad_earliest_time_returns_none(self):
        self.assertIsNone(self.provider().search("Milan", "Venice", DAY, earliest_dep_time="25:00"))
        self.assertEqual(self.net.calls, [])

    def test_empty_place_returns_none(self):
        self.assertIsNone(self.provider().search("  ", "Venice", DAY))


class FlixBusIslandGateTests(FlixBusTestCase):
    """Island cities outside Italy are refused without an autocomplete request; Italian islands are not."""

    def test_island_only_places_outside_italy_make_no_request(self):
        prov = self.provider()
        for place in ("Mykonos", "Kerkyra", "Ibiza"):
            self.assertFalse(prov.can_resolve(place), place)
            self.assertIsNone(prov.search(place, "Milan", DAY), place)
            self.assertIsNone(prov.search("Milan", place, DAY), place)
        queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)["q"][0]
                   for u in self.net.calls if "autocomplete" in u]
        self.assertEqual(queries, ["Milan"])  # only the other side of each search is looked up
        self.assertEqual(self.net.count("search/service"), 0)

    def test_italian_island_cities_are_still_searched(self):
        self.net.autocomplete["Palermo"] = []
        self.assertFalse(self.provider().can_resolve("Palermo"))
        self.assertEqual(self.net.count("autocomplete"), 1)

    def test_mainland_and_unknown_places_are_not_gated(self):
        self.assertFalse(self.provider().can_resolve("Atlantis"))
        self.assertEqual(self.net.count("autocomplete"), 1)
        self.assertFalse(fp._island_without_bus_service("Prag"))
        self.assertFalse(fp._island_without_bus_service("Athens"))
        self.assertTrue(fp._island_without_bus_service("Mykonos"))
        self.assertFalse(fp._island_without_bus_service("Palermo"))


class FlixBusErrorTests(FlixBusTestCase):
    def _http_error(self, code):
        return urllib.error.HTTPError("https://global.api.flixbus.com/", code, "x", {}, None)

    def test_http_500_gives_none_and_error_health(self):
        prov = self.provider()
        self.net.error = self._http_error(500)
        self.assertIsNone(prov.search("Milan", "Venice", DAY))
        health = prov.health_status()
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["last_http_status"], 500)
        self.assertEqual(health["consecutive_failures"], 1)

    def test_403_gives_none_blocked_and_cools_down(self):
        prov = self.provider()
        self.net.error = self._http_error(403)
        self.assertIsNone(prov.search("Milan", "Venice", DAY))
        self.assertEqual(prov.health_status()["status"], "blocked")
        calls_after_block = len(self.net.calls)

        self.net.error = None
        self.assertIsNone(prov.search("Milan", "Venice", DAY))  # still cooling down: no request
        self.assertEqual(len(self.net.calls), calls_after_block)
        self.assertTrue(prov.health_status()["blocked_cooldown_active"])

        self.clock.now += fp.BLOCK_COOLDOWN_SECONDS + 1
        segs = prov.search("Milan", "Venice", DAY, max_results=10)
        self.assertEqual(len(segs), 5)
        self.assertEqual(prov.health_status()["status"], "ok")

    def test_429_is_treated_like_403(self):
        prov = self.provider()
        self.net.error = self._http_error(429)
        self.assertIsNone(prov.search("Paris", "Lyon", DAY))
        self.assertEqual(prov.health_status()["last_http_status"], 429)
        self.assertEqual(prov.health_status()["status"], "blocked")

    def test_network_error_gives_none(self):
        prov = self.provider()
        self.net.error = urllib.error.URLError("timed out")
        self.assertIsNone(prov.search("Milan", "Venice", DAY))
        self.assertEqual(prov.health_status()["status"], "error")

    def test_unexpected_exception_never_raises(self):
        prov = self.provider()
        self.net.error = RuntimeError("boom")
        self.assertIsNone(prov.search("Milan", "Venice", DAY))
        self.assertFalse(prov.can_resolve("Milan"))
        self.assertEqual(prov.health_status()["status"], "error")

    def test_invalid_json_gives_none(self):
        prov = self.provider()
        with mock.patch.object(fp.urllib.request, "urlopen", side_effect=lambda req, timeout=None: FakeResponse(b"<html>")):
            self.assertIsNone(prov.search("Milan", "Venice", DAY))
        self.assertIn("not JSON", prov.health_status()["last_error"])

    def test_unexpected_search_shape_gives_none(self):
        # Munich and Prague each resolve to one city, so there is no other pair to succeed.
        self.set_search_body((MUNICH, PRAGUE), {"something": "else"})
        prov = self.provider()
        self.assertIsNone(prov.search("Munich", "Prague", DAY))
        self.assertEqual(prov.health_status()["status"], "error")

    def test_error_is_not_cached(self):
        prov = self.provider()
        self.net.error = self._http_error(500)
        self.assertIsNone(prov.search("Milan", "Venice", DAY))
        self.net.error = None
        self.assertEqual(len(prov.search("Milan", "Venice", DAY, max_results=10)), 5)


class FlixBusCacheAndRateTests(FlixBusTestCase):
    def test_search_cache_for_thirty_minutes(self):
        # Milan and Venice each have a main city (the airports are skipped): one pair.
        prov = self.provider()
        prov.search("Milan", "Venice", DAY)
        prov.search("Milan", "Venice", DAY, max_results=2)
        self.assertEqual(self.net.count("search/service"), 1)

        self.clock.now += 29 * 60
        prov.search("Milan", "Venice", DAY)
        self.assertEqual(self.net.count("search/service"), 1)

        self.clock.now += 2 * 60
        prov.search("Milan", "Venice", DAY)
        self.assertEqual(self.net.count("search/service"), 2)

    def test_cache_ttl_is_configurable(self):
        prov = self.provider(cache_ttl=60)
        prov.search("Milan", "Venice", DAY)
        self.clock.now += 61
        prov.search("Milan", "Venice", DAY)
        self.assertEqual(self.net.count("search/service"), 2)

    def test_filters_applied_on_cached_results(self):
        prov = self.provider()
        self.assertEqual(len(prov.search("Paris", "Lyon", DAY, max_results=10)), 4)
        self.assertEqual(len(prov.search("Paris", "Lyon", DAY, earliest_dep_time="06:00", max_results=10)), 2)
        # Paris has a main city (Paris CDG Airport is skipped) and Lyon one: one search, cached.
        self.assertEqual(self.net.count("search/service"), 1)

    def test_process_wide_rate_limit_spaces_requests(self):
        # Requests queue up at min_interval spacing: the first is free, then each waits one more interval.
        first = self.provider(min_interval_secs=1.5)
        # autocomplete Milan, autocomplete Venice, then one city-pair search
        first.search("Milan", "Venice", DAY)
        self.assertEqual(self.sleeps, [1.5, 3.0])
        self.sleeps.clear()

        # Another instance shares the process-wide limiter: its request also waits for the slot.
        second = self.provider(min_interval_secs=1.5)
        second.search("Paris", "Lyon", DAY)  # the clock has not moved, last slot is at +3.0 s
        self.assertTrue(self.sleeps and self.sleeps[0] > 0, self.sleeps)

    def test_no_sleep_when_requests_are_far_apart(self):
        prov = self.provider(min_interval_secs=1.5)
        prov.search("Milan", "Venice", DAY)
        self.sleeps.clear()
        self.clock.now += 10  # the last reserved slot is long past
        fp._limiter.reserve(1.5)
        self.assertEqual(self.sleeps, [])
        fp._limiter.reserve(1.5)  # back-to-back: waits one interval
        self.assertEqual(self.sleeps, [1.5])


class FlixBusSpacingTests(FlixBusTestCase):
    def test_default_spacing_is_point_eight_seconds(self):
        self.assertEqual(fp.DEFAULT_MIN_INTERVAL_SECS, 0.8)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fp.MIN_INTERVAL_ENV, None)
            self.assertEqual(FlixBusProvider().min_interval_secs, 0.8)

    def test_env_override_sets_the_spacing(self):
        with mock.patch.dict(os.environ, {fp.MIN_INTERVAL_ENV: "0.25"}):
            self.assertEqual(FlixBusProvider().min_interval_secs, 0.25)
            self.assertEqual(FlixBusProvider(min_interval_secs=0).min_interval_secs, 0.0)

    def test_bad_env_value_falls_back_to_the_default(self):
        for raw in ("fast", "-1", ""):
            with mock.patch.dict(os.environ, {fp.MIN_INTERVAL_ENV: raw}):
                self.assertEqual(FlixBusProvider().min_interval_secs, 0.8, raw)


class FlixBusHealthTests(FlixBusTestCase):
    def test_health_before_and_after_success(self):
        prov = self.provider()
        before = prov.health_status()
        self.assertEqual(before["status"], "unknown")
        self.assertEqual(before["requests"], 0)

        prov.search("Milan", "Venice", DAY)
        after = prov.health_status()
        self.assertEqual(after["status"], "ok")
        self.assertIsNotNone(after["last_ok_at"])
        self.assertIsNone(after["last_error"])
        self.assertEqual(after["requests"], 3)  # two autocomplete calls and one city-pair search
        self.assertEqual(after["cached_searches"], 1)
        self.assertEqual(after["cached_cities"], 2)

    def test_error_then_recovery_clears_error(self):
        prov = self.provider()
        self.net.error = urllib.error.URLError("down")
        prov.search("Milan", "Venice", DAY)
        self.assertEqual(prov.health_status()["consecutive_failures"], 1)
        self.net.error = None
        prov.search("Milan", "Venice", DAY)
        health = prov.health_status()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertIsNone(health["last_error"])


if __name__ == "__main__":
    unittest.main()
