"""Tests for Google Flights query names: IATA codes and English metro names (B03)."""

import unittest
from travel_engine.providers.google_flights_provider import GoogleFlightsProvider


class GoogleQueryNamesTests(unittest.TestCase):
    def setUp(self):
        self.provider = GoogleFlightsProvider()

    def test_normalize_city_table(self):
        # Table of test cases: input -> (query_text, key)
        # Multi-airport metros -> English metro name
        # Single-airport cities/airports -> IATA code (upper, lower)
        test_cases = [
            ("Münih", ("MUC", "muc")),
            ("Munih", ("MUC", "muc")),
            ("Budapeşte", ("BUD", "bud")),
            ("Budapest", ("BUD", "bud")),
            ("Brüksel", ("Brussels", "bru")),
            ("Bruksel", ("Brussels", "bru")),
            ("Londra", ("London", "lon")),
            ("London", ("London", "lon")),
            ("ATH", ("ATH", "ath")),
            ("ath", ("ATH", "ath")),
            ("Atina", ("ATH", "ath")),
            ("Selanik", ("SKG", "skg")),
            ("Istanbul", ("Istanbul", "ist")),
            ("İstanbul", ("Istanbul", "ist")),
            ("Paris", ("Paris", "par")),
            ("Roma", ("Rome", "rom")),
            ("Rome", ("Rome", "rom")),
            ("Milano", ("Milan", "mil")),
            ("Milan", ("Milan", "mil")),
            ("Viyana", ("VIE", "vie")),
            ("Vienna", ("VIE", "vie")),
            ("Prag", ("PRG", "prg")),
            ("Prague", ("PRG", "prg")),
            ("Berlin", ("BER", "ber")),
            ("Venedik", ("Venice", "vce")),
            ("Oslo", ("Oslo", "osl")),
            ("Stockholm", ("Stockholm", "sto")),
            ("Tenerife", ("Tenerife", "tfn")),
            ("Belfast", ("Belfast", "bfs")),
        ]

        for raw_name, expected in test_cases:
            with self.subTest(name=raw_name):
                result = self.provider._normalize_city(raw_name)
                self.assertEqual(result, expected, f"Failed for {raw_name}: got {result}, expected {expected}")

    def test_unresolved_name_fallback(self):
        # Non-European / unindexed names fall back to existing behavior (CITY_MAP or title/4-char)
        tokyo_res = self.provider._normalize_city("Tokyo")
        self.assertEqual(tokyo_res, ("Tokyo", "tyoa"))

        ny_res = self.provider._normalize_city("New York")
        self.assertEqual(ny_res, ("New York", "nyca"))

        unknown_res = self.provider._normalize_city("BilinmeyenSehirXYZ")
        self.assertEqual(unknown_res, ("Bilinmeyensehirxyz", "bili"))

    def test_url_contains_new_query_text(self):
        # Single-airport cities produce URLs with IATA codes
        url_single = self.provider._build_google_url("Münih", "Selanik", "2026-11-12")
        self.assertIn("from%20MUC%20to%20SKG", url_single)

        # Multi-airport metros produce URLs with English metro names
        url_metro = self.provider._build_google_url("Brüksel", "Londra", "2026-11-12")
        self.assertIn("from%20Brussels%20to%20London", url_metro)

        # Mixed single airport and metro
        url_mixed = self.provider._build_google_url("ATH", "Budapeşte", "2026-11-12", "2026-11-18")
        self.assertIn("from%20ATH%20to%20BUD", url_mixed)

    def test_multi_airport_city_regression(self):
        """Regression test: For any city in AirportDB with >= 2 large/medium airports,
        _normalize_city(city)[0] must not be one of the individual airport codes.
        """
        from collections import defaultdict
        from travel_engine.airports import AirportDB

        db = AirportDB.load_default()
        city_airports = defaultdict(list)
        for ap in db._airports:
            if ap.size in ("large", "medium"):
                city_airports[ap.city].append(ap)

        multi_airport_cities = {city: aps for city, aps in city_airports.items() if len(aps) >= 2}
        self.assertGreaterEqual(len(multi_airport_cities), 5)

        for city, aps in multi_airport_cities.items():
            with self.subTest(city=city):
                query_text, _ = self.provider._normalize_city(city)
                codes = {a.iata for a in aps}
                self.assertNotIn(
                    query_text,
                    codes,
                    f"Query text for multi-airport city '{city}' should not be single airport code {query_text} (codes: {codes})",
                )
