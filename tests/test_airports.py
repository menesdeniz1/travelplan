import csv
import unittest

from travel_engine.airports import (
    CITY_CENTRES_PATH,
    DATA_PATH,
    ISLAND_AIRPORTS,
    METRO_GROUPS,
    Airport,
    AirportDB,
    _ISLAND_GROUPS,
    _norm,
    haversine_km,
)

EUROPE_AND_NEIGHBOURS = set(
    """AD AL AT AX BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GG GI GR HR HU IE IM IS IT JE LI
    LT LU LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SK SM UA VA XK
    TR GE AM AZ IL EG MA TN""".split()
)


def _codes(airports):
    return {a.iata for a in airports}


class AirportDataFileTests(unittest.TestCase):
    def test_csv_header_and_rows(self):
        with open(DATA_PATH, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, ["iata", "name", "city", "country", "lat", "lon", "size"])
            rows = list(reader)
        self.assertGreater(len(rows), 500)
        self.assertEqual(len({r["iata"] for r in rows}), len(rows), "IATA codes must be unique")
        for r in rows:
            self.assertRegex(r["iata"], r"^[A-Z]{3}$")
            self.assertIn(r["size"], ("large", "medium"))
            self.assertIn(r["country"], EUROPE_AND_NEIGHBOURS, r)
            self.assertTrue(-90 <= float(r["lat"]) <= 90)
            self.assertTrue(-180 <= float(r["lon"]) <= 180)

    def test_default_db_is_cached(self):
        self.assertIs(AirportDB.load_default(), AirportDB.load_default())


class AirportLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = AirportDB.load_default()

    def test_get_is_case_insensitive(self):
        ap = self.db.get("prg")
        self.assertIsInstance(ap, Airport)
        self.assertEqual(ap.iata, "PRG")
        self.assertIsNone(self.db.get("ZZZ"))
        self.assertIsNone(self.db.get(""))

    def test_haversine_prague_vienna(self):
        # Straight-line PRG-VIE is about 278 km.
        d = haversine_km(50.1009, 14.2599, 48.1103, 16.5697)
        self.assertGreater(d, 265)
        self.assertLess(d, 290)

    def test_haversine_zero_distance(self):
        self.assertAlmostEqual(haversine_km(41.0, 29.0, 41.0, 29.0), 0.0, places=6)

    def test_resolve_prague_exonym(self):
        self.assertIn("PRG", _codes(self.db.resolve("Prag")))
        self.assertEqual(self.db.resolve("Prag")[0].iata, "PRG")
        self.assertIn("PRG", _codes(self.db.resolve("Praha")))

    def test_resolve_londra_multi_airport(self):
        self.assertTrue({"LHR", "LGW", "STN"} <= _codes(self.db.resolve("Londra")))

    def test_resolve_london_metro_code(self):
        self.assertTrue({"LHR", "LGW", "STN"} <= _codes(self.db.resolve("LON")))

    def test_resolve_milano_multi_airport(self):
        self.assertTrue({"MXP", "LIN", "BGY"} <= _codes(self.db.resolve("Milano")))
        self.assertTrue({"MXP", "LIN", "BGY"} <= _codes(self.db.resolve("Milan")))

    def test_resolve_greece_name_and_code(self):
        by_name = _codes(self.db.resolve("Yunanistan"))
        by_code = _codes(self.db.resolve("GR"))
        self.assertTrue({"ATH", "SKG"} <= by_name)
        self.assertEqual(by_name, by_code)

    def test_resolve_istanbul_case_and_diacritics(self):
        self.assertEqual(_codes(self.db.resolve("İSTANBUL")), {"IST", "SAW"})
        self.assertEqual(_codes(self.db.resolve("istanbul")), {"IST", "SAW"})

    def test_resolve_iata_code_returns_single_airport(self):
        self.assertEqual([a.iata for a in self.db.resolve("ist")], ["IST"])

    def test_resolve_turkish_city_exonyms(self):
        cases = {
            "Atina": "ATH", "Selanik": "SKG", "Viyana": "VIE", "Münih": "MUC",
            "Budapeşte": "BUD", "Roma": "FCO", "Varşova": "WAW", "Brüksel": "BRU",
            "Kopenhag": "CPH", "Lizbon": "LIS", "Venedik": "VCE", "Floransa": "FLR",
            "Napoli": "NAP", "Cenevre": "GVA", "Zürih": "ZRH", "Belgrad": "BEG",
            "Sofya": "SOF", "Bükreş": "OTP", "Barselona": "BCN", "Edinburg": "EDI",
            "Krakov": "KRK", "Saraybosna": "SJJ", "Üsküp": "SKP", "Tiran": "TIA",
            "Madrid": "MAD", "Amsterdam": "AMS", "Dublin": "DUB", "Bratislava": "BTS",
            "Tiflis": "TBS", "Kahire": "CAI",
        }
        for name, code in cases.items():
            with self.subTest(name=name):
                self.assertIn(code, _codes(self.db.resolve(name)))

    def test_resolve_country_names(self):
        cases = {
            "Türkiye": ("IST", "SAW"),
            "Çekya": ("PRG",),
            "Birleşik Krallık": ("LHR", "LGW"),
            "Almanya": ("FRA", "MUC"),
            "İtalya": ("FCO", "MXP"),
            "UK": ("LHR",),
        }
        for name, codes in cases.items():
            with self.subTest(name=name):
                self.assertTrue(set(codes) <= _codes(self.db.resolve(name)))

    def test_resolve_unknown_is_empty(self):
        self.assertEqual(self.db.resolve("Xyzzyville"), [])
        self.assertEqual(self.db.resolve(""), [])

    def test_find_city_large_first(self):
        airports = self.db.find_city("London")
        self.assertTrue({"LHR", "LGW", "STN"} <= _codes(airports))
        sizes = [a.size for a in airports]
        self.assertEqual(sizes, sorted(sizes, key=lambda s: s != "large"))

    def test_country_airports(self):
        airports = self.db.country_airports("GR")
        self.assertIn("ATH", _codes(airports))
        self.assertTrue(all(a.country == "GR" for a in airports))
        self.assertEqual(self.db.country_airports("ZZ"), [])

    def test_city_center_prefers_city_table(self):
        # Changed behaviour: city names resolve to the city centre, not the airport.
        lat, lon = self.db.city_center("Prague")
        self.assertAlmostEqual(lat, 50.0875, places=4)
        self.assertAlmostEqual(lon, 14.4214, places=4)

    def test_city_center_airport_code_is_airport(self):
        lat, lon = self.db.city_center("PRG")
        self.assertAlmostEqual(lat, 50.1009, places=3)
        self.assertAlmostEqual(lon, 14.2599, places=3)

    def test_city_center_multi_airport_city(self):
        lat, _ = self.db.city_center("Milano")
        self.assertTrue(45.3 <= lat <= 45.8)

    def test_city_center_unknown_and_country(self):
        self.assertIsNone(self.db.city_center("Xyzzyville"))
        self.assertIsNone(self.db.city_center("GR"))

    def test_nearby_sorted_and_limited(self):
        hits = self.db.nearby(50.1009, 14.2599, 350, limit=5)
        self.assertLessEqual(len(hits), 5)
        dists = [d for _, d in hits]
        self.assertEqual(dists, sorted(dists))
        self.assertTrue(all(d <= 350 for d in dists))

    def test_nearby_size_filter(self):
        hits = self.db.nearby(50.1009, 14.2599, 350, sizes=("large",))
        self.assertTrue(hits)
        self.assertTrue(all(a.size == "large" for a, _ in hits))

    def test_nearby_airports_prague(self):
        hits = self.db.nearby_airports("Prague", 350, limit=50)
        codes = {a.iata for a, _ in hits}
        self.assertTrue({"VIE", "BTS", "DRS"} <= codes)
        self.assertLessEqual(len(self.db.nearby_airports("Prague", 350)), 15)
        self.assertNotIn("PRG", codes)
        vie = next(d for a, d in hits if a.iata == "VIE")
        self.assertGreater(vie, 265)
        self.assertLess(vie, 290)
        dists = [d for _, d in hits]
        self.assertEqual(dists, sorted(dists))

    def test_nearby_airports_milano(self):
        hits = self.db.nearby_airports("Milano", 300)
        codes = {a.iata for a, _ in hits}
        self.assertTrue(codes & {"TRN", "VCE", "BLQ"})
        self.assertFalse(codes & {"MXP", "LIN", "BGY"})

    def test_nearby_airports_unknown_and_country(self):
        self.assertEqual(self.db.nearby_airports("Xyzzyville"), [])
        self.assertEqual(self.db.nearby_airports("GR"), [])


class IslandAndLandmassTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = AirportDB.load_default()

    def test_every_island_code_exists_in_db(self):
        missing = sorted(c for c in ISLAND_AIRPORTS if self.db.get(c) is None)
        self.assertEqual(missing, [], "island codes not in airports.csv")

    def test_island_codes_are_iata_and_island_flag(self):
        for code in ISLAND_AIRPORTS:
            with self.subTest(code=code):
                self.assertRegex(code, r"^[A-Z]{3}$")
                self.assertTrue(self.db.is_island(code))
        self.assertFalse(self.db.is_island("ATH"))
        self.assertFalse(self.db.is_island("LHR"))

    def test_island_groups_are_island_codes(self):
        for group, codes in _ISLAND_GROUPS.items():
            for code in codes:
                with self.subTest(group=group, code=code):
                    self.assertIn(code, ISLAND_AIRPORTS)
                    self.assertIsNotNone(self.db.get(code))

    def test_landmass_values(self):
        self.assertEqual(self.db.landmass("HER"), "island:HER")
        self.assertEqual(self.db.landmass("LHR"), "GB")
        self.assertEqual(self.db.landmass("BFS"), "IE")
        self.assertEqual(self.db.landmass("DUB"), "IE")
        self.assertEqual(self.db.landmass("VIE"), "continent")
        self.assertEqual(self.db.landmass("ZZZ"), "")

    def test_ground_reachable(self):
        cases = {
            ("ATH", "JTR"): False,
            ("HER", "CHQ"): True,
            ("ATH", "SKG"): True,
            ("LHR", "CDG"): True,
            ("DUB", "LHR"): False,
            ("VIE", "PRG"): True,
            ("ATH", "CHQ"): False,
            ("BFS", "LHR"): False,
            ("ZZZ", "ATH"): False,
        }
        for (a, b), expected in cases.items():
            with self.subTest(a=a, b=b):
                self.assertIs(self.db.ground_reachable(a, b), expected)

    def test_no_ground_between_separate_islands(self):
        self.assertFalse(self.db.ground_reachable("JTR", "JMK"))
        self.assertTrue(self.db.ground_reachable("CAG", "OLB"))
        self.assertFalse(self.db.ground_reachable("TFS", "LPA"))
        self.assertTrue(self.db.ground_reachable("TFS", "TFN"))


class CityCentreTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = AirportDB.load_default()

    def test_city_without_an_airport_takes_its_country_zone(self):
        self.assertEqual(self.db.timezone("Lyon"), "Europe/Paris")  # in the table, no airport of its own
        self.assertEqual(self.db.timezone("Bergamo"), "Europe/Rome")
        self.assertEqual(self.db.timezone("Ghent"), "Europe/Brussels")

    def test_unknown_city_without_an_airport_is_utc(self):
        self.assertEqual(self.db.timezone("Metz"), "UTC")  # neither an airport nor in the table

    def test_city_centre_csv_header_and_rows(self):
        with open(CITY_CENTRES_PATH, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, ["city", "country", "lat", "lon"])
            rows = list(reader)
        self.assertGreaterEqual(len(rows), 150)
        keys = [_norm(r["city"]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)), "normalised city names must be unique")
        for r in rows:
            self.assertTrue(-90 <= float(r["lat"]) <= 90, r)
            self.assertTrue(-180 <= float(r["lon"]) <= 180, r)

    def test_prague_and_milan(self):
        lat, lon = self.db.city_center("Prag")
        self.assertLess(haversine_km(lat, lon, 50.0875, 14.4214), 2.0)
        lat, lon = self.db.city_center("Milano")
        self.assertLess(haversine_km(lat, lon, 45.4642, 9.19), 0.5)
        self.assertEqual(self.db.city_center("Londra"), self.db.city_center("London"))

    def test_city_code_london_uses_table(self):
        self.assertEqual(self.db.city_center("LON"), self.db.city_center("London"))

    def test_metro_group_main_cities_listed(self):
        for name in METRO_GROUPS:
            with self.subTest(metro=name):
                self.assertIsNotNone(self.db.city_center(name))

    def test_spec_capitals_listed(self):
        for name in ("Athens", "Vienna", "Berlin", "Paris", "Rome", "Madrid", "Lisbon",
                     "Warsaw", "Riga", "Vilnius", "Tallinn", "Helsinki", "Dublin", "Oslo",
                     "Stockholm", "Copenhagen", "Amsterdam", "Brussels", "Luxembourg", "Bern"):
            with self.subTest(city=name):
                self.assertIsNotNone(self.db.city_center(name))


class AirportCityDistanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = AirportDB.load_default()

    def test_vienna_airport_to_city(self):
        d = self.db.airport_city_km("VIE")
        self.assertIsNotNone(d)
        self.assertGreater(d, 15)
        self.assertLess(d, 22)

    def test_beauvais_own_municipality_and_metro(self):
        # Own municipality (Beauvais) is listed, so airport_city_km is short.
        self.assertGreater(self.db.airport_city_km("BVA"), 2)
        self.assertLess(self.db.airport_city_km("BVA"), 6)
        # Straight-line BVA to Paris centre is about 69 km (not 75-90 km: that was road distance).
        self.assertGreater(self.db.metro_city_km("BVA"), 65)
        self.assertLess(self.db.metro_city_km("BVA"), 75)

    def test_bergamo_own_municipality_and_milan_metro(self):
        bergamo = self.db.airport_city_km("BGY")
        self.assertGreater(bergamo, 3)
        self.assertLess(bergamo, 6)
        milan = self.db.metro_city_km("BGY")
        self.assertGreater(milan, 40)
        self.assertLess(milan, 50)

    def test_unknown_is_none(self):
        self.assertIsNone(self.db.airport_city_km("ZZZ"))
        self.assertIsNone(self.db.metro_city_km("ZZZ"))
        self.assertIsNone(self.db.airport_city_km("AAC"))
        self.assertIsNone(self.db.metro_city_km("AAC"))


if __name__ == "__main__":
    unittest.main()
