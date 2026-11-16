"""obilet "from" fares: route slugs, the inline bottomTable, operator matching, cache, pacing and health.

No network. Route pages come from tests/fixtures/obilet (the captured bottomTable JSON, wrapped as the page
has it); the fetch is injected.
"""

import os
import unittest
import urllib.error
from unittest import mock

from travel_engine.providers import obilet_fares as ob
from travel_engine.providers.obilet_fares import (
    BASE_URL,
    ObiletFromPrices,
    min_fares_try,
    normalise_operator,
    parse_bottom_table,
    price_for_operator,
    route_url,
)

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "obilet")
_RATE = 50.0


def _fixture(name):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class _Case(unittest.TestCase):
    def setUp(self):
        ObiletFromPrices.reset_shared_state()
        patcher = mock.patch("travel_engine.fx.get_eur_try_rate", return_value=(_RATE, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = [5000.0]
        for name in ("_now",):
            p = mock.patch.object(ob, name, lambda: self.clock[0])
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(ob, "_sleep", side_effect=lambda s: self.sleeps.append(s))
        self.sleeps = []
        p.start()
        self.addCleanup(p.stop)


class RouteTests(unittest.TestCase):
    def test_turkish_city_pair_slug(self):
        self.assertEqual(route_url("İstanbul", "Ankara"), BASE_URL + "istanbul-ankara")
        self.assertEqual(route_url("Istanbul", "Izmir"), BASE_URL + "istanbul-izmir")

    def test_istanbul_variants_map_to_one_slug(self):
        for name in ("İstanbul Havalimanı", "Istanbul Avrupa", "istanbul (Anadolu)", "Istanbul, Turkey"):
            self.assertEqual(route_url(name, "Ankara"), BASE_URL + "istanbul-ankara", name)

    def test_turkish_letters_fold_to_ascii_slugs(self):
        self.assertEqual(route_url("Ankara", "Şanlıurfa"), BASE_URL + "ankara-sanliurfa")
        self.assertEqual(route_url("Kahramanmaraş", "Eskişehir"), BASE_URL + "kahramanmaras-eskisehir")

    def test_verified_international_slugs(self):
        self.assertEqual(route_url("Istanbul", "Sofia"), BASE_URL + "istanbul-sofya")
        self.assertEqual(route_url("Istanbul", "Thessaloniki"), BASE_URL + "istanbul-selanik")

    def test_unmapped_or_same_city_has_no_route(self):
        self.assertIsNone(route_url("Madrid", "Barcelona"))
        self.assertIsNone(route_url("Ankara", "Ankara"))
        self.assertIsNone(route_url("", "Ankara"))


class OperatorNameTests(unittest.TestCase):
    def test_numbers_and_noise_words_are_dropped(self):
        self.assertEqual(normalise_operator("Kamil Koç 81030"), "kamil koc")
        self.assertEqual(normalise_operator("Arda Tur New"), "arda tur")
        self.assertEqual(normalise_operator("Metro Turizm"), "metro turizm")

    def test_spacing_does_not_matter_for_an_exact_match(self):
        prices = {"arda tur": 1500.0}
        self.assertEqual(price_for_operator(prices, "ArdaTur  27"), 1500.0)
        self.assertEqual(price_for_operator(prices, "Arda Tur"), 1500.0)

    def test_core_words_match_a_longer_operator_name(self):
        prices = {"metro turizm": 900.0, "kamil koc": 1099.0}
        self.assertEqual(price_for_operator(prices, "Metro"), 900.0)
        self.assertEqual(price_for_operator(prices, "Kamil Koç   81030"), 1099.0)

    def test_no_match_gives_none(self):
        self.assertIsNone(price_for_operator({"kamil koc": 1099.0}, "FlixBus"))
        self.assertIsNone(price_for_operator({"kamil koc": 1099.0}, ""))
        self.assertIsNone(price_for_operator({}, "Kamil Koç"))

    def test_short_names_do_not_loosely_match(self):
        self.assertIsNone(price_for_operator({"ben turizm": 690.0}, "Ben"))

    def test_ambiguous_loose_match_gives_none(self):
        prices = {"lider elbistan": 600.0, "lider vip travel": 700.0}
        self.assertIsNone(price_for_operator(prices, "Lider"))


class TableTests(_Case):
    def test_ankara_page_has_77_operator_rows(self):
        rows = parse_bottom_table(_fixture("istanbul_ankara_route.html"))
        self.assertEqual(len(rows), 77)
        first = rows[0]
        self.assertEqual((first["Name"], first["Currency"], first["Price"]), ("Selçuk Canlar Turizm", "TRY", 560.0))
        self.assertEqual(first["Duration"], "7Saat 11Dakika")

    def test_minimum_fare_per_operator_in_try(self):
        prices = min_fares_try(parse_bottom_table(_fixture("istanbul_ankara_route.html")))
        self.assertEqual(prices["selcuk canlar turizm"], 560.0)
        self.assertEqual(prices["kamil koc"], 1099.0)
        self.assertEqual(prices["metro turizm"], 900.0)
        self.assertTrue(all(isinstance(v, float) for v in prices.values()))

    def test_eur_rows_are_converted_and_unknown_currency_is_left_out(self):
        rows = [
            {"Name": "Flix Bus", "Currency": "EUR", "Price": 37.0},
            {"Name": "Flix Bus", "Currency": "EUR", "Price": 30.0},
            {"Name": "Odd Coach", "Currency": "XYZ", "Price": 10.0},
            {"Name": "No Price", "Currency": "TRY"},
            {"Name": "Free Ride", "Currency": "TRY", "Price": 0},
        ]
        self.assertEqual(min_fares_try(rows), {"flix bus": 1500.0})

    def test_istanbul_sofya_page_mixes_eur_and_try(self):
        prices = min_fares_try(parse_bottom_table(_fixture("istanbul_sofya_route.html")))
        self.assertEqual(prices["panorama trans"], 1500.0)  # 30 EUR at 50
        self.assertEqual(prices["flix bus"], 1850.0)        # 37 EUR at 50
        self.assertEqual(prices["kamil koc"], 2059.0)

    def test_page_without_the_table_is_none(self):
        self.assertIsNone(parse_bottom_table("<html><body>no table here</body></html>"))
        self.assertIsNone(parse_bottom_table(None))

    def test_broken_json_or_missing_data_is_none(self):
        self.assertIsNone(parse_bottom_table('"bottomTable": {"data": [ {"Name": '))
        self.assertIsNone(parse_bottom_table('"bottomTable": {"headers": []}'))
        self.assertIsNone(parse_bottom_table('"bottomTable": ["not", "an", "object"]'))


class FromPricesTests(_Case):
    def _provider(self, pages=None, error=None, **kwargs):
        self.requested = []

        def fetch(url):
            self.requested.append(url)
            if error is not None:
                raise error
            return pages.get(url, "") if pages is not None else _fixture("istanbul_ankara_route.html")

        options = dict(fetch_html=fetch, min_interval_secs=1.0)
        options.update(kwargs)
        return ObiletFromPrices(**options)

    def test_istanbul_ankara_returns_the_operator_table(self):
        prices = self._provider().from_prices("İstanbul", "Ankara")
        self.assertEqual(prices["kamil koc"], 1099.0)
        self.assertEqual(len(prices), 77)

    def test_unmapped_route_makes_no_request(self):
        provider = self._provider()
        self.assertEqual(provider.from_prices("Madrid", "Barcelona"), {})
        self.assertEqual(self.requested, [])

    def test_only_route_pages_are_requested(self):
        provider = self._provider()
        provider.from_prices("İstanbul", "Ankara")
        provider.from_prices("İstanbul", "Sofia")
        self.assertTrue(self.requested)
        self.assertTrue(all(url.startswith(BASE_URL) for url in self.requested))
        self.assertNotIn("api", " ".join(self.requested).lower())

    def test_results_are_cached_for_24_hours(self):
        provider = self._provider()
        provider.from_prices("İstanbul", "Ankara")
        provider.from_prices("İstanbul", "Ankara")
        self.assertEqual(len(self.requested), 1)
        self.clock[0] += 23 * 3600
        provider.from_prices("İstanbul", "Ankara")
        self.assertEqual(len(self.requested), 1)
        self.clock[0] += 2 * 3600
        provider.from_prices("İstanbul", "Ankara")
        self.assertEqual(len(self.requested), 2)

    def test_cached_result_is_a_copy(self):
        provider = self._provider()
        first = provider.from_prices("İstanbul", "Ankara")
        first["kamil koc"] = 1.0
        self.assertEqual(provider.from_prices("İstanbul", "Ankara")["kamil koc"], 1099.0)

    def test_missing_route_page_is_cached_as_no_fares(self):
        error = urllib.error.HTTPError(BASE_URL + "istanbul-ankara", 404, "Not Found", {}, None)
        provider = self._provider(error=error)
        self.assertEqual(provider.from_prices("İstanbul", "Ankara"), {})
        self.assertEqual(provider.from_prices("İstanbul", "Ankara"), {})
        self.assertEqual(len(self.requested), 1)
        self.assertEqual(provider.health_status()["status"], "ok")

    def test_network_error_returns_empty_and_is_not_cached(self):
        provider = self._provider(error=OSError("no route to host"))
        self.assertEqual(provider.from_prices("İstanbul", "Ankara"), {})
        self.assertEqual(provider.from_prices("İstanbul", "Ankara"), {})
        self.assertEqual(len(self.requested), 2)
        health = provider.health_status()
        self.assertEqual((health["status"], health["consecutive_failures"]), ("error", 2))
        self.assertIn("no route to host", health["last_error"])

    def test_server_error_is_a_failure_not_a_missing_route(self):
        error = urllib.error.HTTPError(BASE_URL + "istanbul-ankara", 503, "Unavailable", {}, None)
        provider = self._provider(error=error)
        self.assertEqual(provider.from_prices("İstanbul", "Ankara"), {})
        self.assertIn("503", provider.health_status()["last_error"])

    def test_page_without_the_table_is_a_parse_error_and_not_cached(self):
        provider = self._provider(pages={BASE_URL + "istanbul-ankara": "<html>changed layout</html>"})
        self.assertEqual(provider.from_prices("İstanbul", "Ankara"), {})
        self.assertEqual(provider.health_status()["status"], "parse_error")
        provider.from_prices("İstanbul", "Ankara")
        self.assertEqual(len(self.requested), 2)

    def test_fetches_are_spaced_by_one_second(self):
        provider = self._provider()
        provider.from_prices("İstanbul", "Ankara")
        provider.from_prices("İstanbul", "Sofia")
        self.assertEqual(len(self.requested), 2)
        self.assertEqual(self.sleeps, [1.0])

    def test_an_unexpected_error_never_raises(self):
        provider = self._provider()
        with mock.patch.object(ob, "parse_bottom_table", side_effect=RuntimeError("boom")):
            self.assertEqual(provider.from_prices("İstanbul", "Ankara"), {})
        self.assertEqual(provider.health_status()["consecutive_failures"], 1)

    def test_health_before_any_request(self):
        health = self._provider().health_status()
        self.assertEqual(health["source"], "obilet")
        self.assertEqual((health["status"], health["healthy"], health["requests"]), ("unknown", True, 0))

    def test_three_failures_are_unhealthy(self):
        provider = self._provider(error=OSError("down"))
        for destination in ("Ankara", "Izmir", "Bursa"):
            provider.from_prices("İstanbul", destination)
        self.assertFalse(provider.health_status()["healthy"])


if __name__ == "__main__":
    unittest.main()
