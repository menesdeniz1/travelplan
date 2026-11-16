"""Tests for city name resolution to airports (AIR-4, T09)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from travel_engine.airports import AirportDB


class CityNamesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = AirportDB.load_default()

    def test_required_city_names_resolve_to_airports(self):
        """(kırmızı) Frankfurt, Leipzig, Graz, Cologne, Köln, Basel havalimanı bulur."""
        cases = [
            ("Frankfurt", "FRA"),
            ("Leipzig", "LEJ"),
            ("Graz", "GRZ"),
            ("Cologne", "CGN"),
            ("Köln", "CGN"),
            ("Basel", "BSL"),
            ("Bâle", "BSL"),
            ("Munich", "MUC"),
            ("Münih", "MUC"),
            ("Vienna", "VIE"),
            ("Viyana", "VIE"),
            ("Prague", "PRG"),
            ("Prag", "PRG"),
            ("Milan", "MXP"),
            ("Venice", "VCE"),
            ("Rome", "FCO"),
            ("Krakow", "KRK"),
            ("Kraków", "KRK"),
            ("Wroclaw", "WRO"),
            ("Thessaloniki", "SKG"),
            ("Selanik", "SKG"),
        ]
        for name, expected_code in cases:
            with self.subTest(city=name, expected=expected_code):
                airports = self.db.find_city(name)
                codes = [a.iata for a in airports]
                self.assertGreater(len(codes), 0, f"No airports found for city {name}")
                self.assertIn(
                    expected_code,
                    codes,
                    f"Expected airport {expected_code} not found for {name} (got {codes})",
                )

    def test_existing_mappings_protected(self):
        """(koruma) Mevcut eşlemeler (Atina, İstanbul, Budapeşte...) aynı."""
        protection_cases = [
            ("Atina", "ATH"),
            ("Athens", "ATH"),
            ("İstanbul", "IST"),
            ("Istanbul", "IST"),
            ("Budapeşte", "BUD"),
            ("Budapest", "BUD"),
            ("Londra", "LHR"),
            ("London", "LHR"),
            ("Varşova", "WAW"),
            ("Warsaw", "WAW"),
            ("Berlin", "BER"),
        ]
        for name, expected_code in protection_cases:
            with self.subTest(city=name, expected=expected_code):
                codes = [a.iata for a in self.db.find_city(name)]
                self.assertIn(expected_code, codes, f"Protection check failed for {name}: {codes}")


    def test_t14_city_names_resolve_to_airports(self):
        """(kırmızı) T14: Ostrava ve denetimden çıkan Avrupa/Türkiye havalimanları şehir adıyla çözülür."""
        cases = [
            ("Ostrava", "OSR"),
            ("Banja Luka", "BNX"),
            ("Tuzla", "TZL"),
            ("Bursa", "YEI"),
            ("Bergamo", "BGY"),
            ("Verona", "VRN"),
            ("Torino", "TRN"),
            ("Genoa", "GOA"),
            ("Lamezia", "SUF"),
            ("Karlsruhe", "FKB"),
            ("Baden-Baden", "FKB"),
            ("Münster", "FMO"),
            ("Osnabrück", "FMO"),
            ("Rzeszów", "RZE"),
            ("Modlin", "WMI"),
            ("Torp", "TRF"),
            ("Chania", "CHQ"),
            ("Hanya", "CHQ"),
            ("Corfu", "CFU"),
            ("Korfu", "CFU"),
            ("Murcia", "RMU"),
            ("Ercan", "ECN"),
            ("Lefkoşa", "ECN"),
            ("Lille", "LIL"),
            ("Marseille", "MRS"),
            ("Montpellier", "MPL"),
            ("Toulouse", "TLS"),
            ("Brescia", "VBS"),
            ("Ancona", "AOI"),
            ("Trieste", "TRS"),
            ("Tampere", "TMP"),
            ("Västerås", "VST"),
            ("Vitoria", "VIT"),
            ("Jerez", "XRY"),
            ("Trapani", "TPS"),
            ("Crotone", "CRV"),
        ]
        for name, expected_code in cases:
            with self.subTest(city=name, expected=expected_code):
                airports = self.db.find_city(name)
                codes = [a.iata for a in airports]
                self.assertGreater(len(codes), 0, f"No airports found for city {name}")
                self.assertIn(
                    expected_code,
                    codes,
                    f"Expected airport {expected_code} not found for {name} (got {codes})",
                )


if __name__ == "__main__":
    unittest.main()
