import unittest
from travel_engine.geo_clusters import (
    GeoClusterRegistry,
    CityCluster,
    CatchmentAirport,
    TransitConnection,
)

class TestGeoClusters(unittest.TestCase):
    def setUp(self):
        self.registry = GeoClusterRegistry()

    def test_prague_cluster_has_vienna_and_bratislava_catchment(self):
        cluster = self.registry.get_cluster("Prague")
        self.assertIsNotNone(cluster)
        self.assertEqual(cluster.primary_airport, "PRG")
        catchment_codes = [c.airport_code for c in cluster.catchment_airports]
        self.assertIn("VIE", catchment_codes)
        self.assertIn("BTS", catchment_codes)
        self.assertIn("DRS", catchment_codes)

    def test_turkish_city_aliases_resolve_clusters(self):
        self.assertEqual(self.registry.get_cluster("Prag").city_name, "Prague")
        self.assertEqual(self.registry.get_cluster("Viyana").city_name, "Vienna")
        self.assertEqual(self.registry.get_cluster("Atina").city_name, "Athens")
        self.assertEqual(self.registry.get_cluster("Selanik").city_name, "Thessaloniki")
        self.assertEqual(self.registry.get_cluster("Münih").city_name, "Munich")
        self.assertEqual(self.registry.get_cluster("Budapeşte").city_name, "Budapest")

    def test_transit_connection_between_vienna_and_prague(self):
        connections = self.registry.get_transit_connections("VIE", "Prague")
        self.assertTrue(len(connections) > 0)
        conn = connections[0]
        self.assertEqual(conn.mode, "train")
        self.assertGreater(conn.duration_mins, 180)  # ~4 hours
        self.assertLess(conn.duration_mins, 300)
        self.assertGreater(conn.estimated_price_try, 0)

    def test_airport_to_station_transfer_buffer_minimum_90(self):
        for code in ["VIE", "BTS", "PRG", "BUD", "MUC", "BER", "DRS", "SKG", "ATH"]:
            buffer_mins = self.registry.get_transfer_buffer_mins(code)
            self.assertGreaterEqual(buffer_mins, 90, f"Expected buffer >= 90 for {code}, got {buffer_mins}")

    def test_all_major_european_cities_present(self):
        cities = self.registry.list_cities()
        expected = ["Prague", "Vienna", "Bratislava", "Budapest", "Munich", "Berlin", "Thessaloniki", "Athens"]
        for exp in expected:
            self.assertIn(exp, cities)

    def test_unknown_city_returns_none(self):
        self.assertIsNone(self.registry.get_cluster("Atlantis"))

    def test_coordinates_lookup(self):
        c_prg = self.registry.get_coordinates("Prague")
        self.assertIsNotNone(c_prg)
        self.assertAlmostEqual(c_prg["lat"], 50.0755, places=2)

        c_ath_tr = self.registry.get_coordinates("Atina")
        self.assertIsNotNone(c_ath_tr)
        self.assertAlmostEqual(c_ath_tr["lat"], 37.9838, places=2)

        c_vie_iata = self.registry.get_coordinates("VIE")
        self.assertIsNotNone(c_vie_iata)
        self.assertAlmostEqual(c_vie_iata["lat"], 48.1103, places=2)

    def test_prague_cluster_has_budapest_and_munich_catchment(self):
        cluster = self.registry.get_cluster("Prague")
        catchment_codes = [c.airport_code for c in cluster.catchment_airports]
        self.assertIn("BUD", catchment_codes)
        self.assertIn("MUC", catchment_codes)
        bud = next(c for c in cluster.catchment_airports if c.airport_code == "BUD")
        self.assertEqual(bud.city_name, "Budapest")
        self.assertEqual(bud.transfer_mode, "train")
        muc = next(c for c in cluster.catchment_airports if c.airport_code == "MUC")
        self.assertEqual(muc.city_name, "Munich")
        self.assertEqual(muc.transfer_mode, "bus")

    def test_prague_transit_connections_from_budapest_and_munich(self):
        bud_conns = self.registry.get_transit_connections("BUD", "Prague")
        self.assertEqual(len(bud_conns), 1)
        self.assertEqual(bud_conns[0].station_from, "Budapest Kelenföld")
        self.assertEqual(bud_conns[0].station_to, "Praha hl.n.")
        self.assertEqual(bud_conns[0].mode, "train")
        self.assertEqual(bud_conns[0].duration_mins, 420)

        muc_conns = self.registry.get_transit_connections("MUC", "Prague")
        self.assertEqual(len(muc_conns), 1)
        self.assertEqual(muc_conns[0].station_from, "München ZOB")
        self.assertEqual(muc_conns[0].station_to, "Praha hl.n.")
        self.assertEqual(muc_conns[0].mode, "bus")
        self.assertEqual(muc_conns[0].primary_operator, "FlixBus")

    def test_budapest_and_munich_airports_have_map_coordinates(self):
        self.assertIsNotNone(self.registry.get_coordinates("BUD"))
        self.assertIsNotNone(self.registry.get_coordinates("MUC"))
        self.assertIsNotNone(self.registry.get_coordinates("Budapest"))
        self.assertIsNotNone(self.registry.get_coordinates("Munich"))

    def test_every_catchment_has_positive_hotel_estimate(self):
        for city in self.registry.list_cities():
            for catchment in self.registry.get_cluster(city).catchment_airports:
                self.assertIsNotNone(catchment.avg_hotel_try, f"{catchment.airport_code} missing avg_hotel_try")
                self.assertGreater(catchment.avg_hotel_try, 0)

    def test_turkish_dotted_i_normalization(self):
        coords_ist = self.registry.get_coordinates("İstanbul")
        self.assertIsNotNone(coords_ist)
        self.assertAlmostEqual(coords_ist["lat"], 41.0082, places=2)

        coords_izm = self.registry.get_coordinates("İzmir")
        self.assertIsNotNone(coords_izm)
        self.assertAlmostEqual(coords_izm["lat"], 38.4237, places=2)

        cluster_mun = self.registry.get_cluster("Münih")
        self.assertIsNotNone(cluster_mun)
        self.assertEqual(cluster_mun.city_name, "Munich")

    def test_resolve_via_greece_by_name_and_code(self):
        for token in ["Yunanistan", "Greece", "GR", "gr", "YUNANİSTAN", "yunanistán"]:
            codes = self.registry.resolve_via(token)
            self.assertIn("ATH", codes, token)
            self.assertIn("SKG", codes, token)
            self.assertEqual(codes[0], "ATH", token)

    def test_resolve_via_country_airports_are_uppercase_and_unique(self):
        codes = self.registry.resolve_via("Yunanistan")
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(c == c.upper() and len(c) == 3 for c in codes))
        self.assertIn("HER", codes)
        self.assertIn("RHO", codes)
        self.assertIn("CFU", codes)

    def test_resolve_via_turkey_and_other_countries(self):
        tr = self.registry.resolve_via("Türkiye")
        for code in ["IST", "SAW", "ESB", "ADB", "AYT"]:
            self.assertIn(code, tr)
        self.assertIn("PRG", self.registry.resolve_via("Çekya"))
        self.assertIn("VIE", self.registry.resolve_via("Avusturya"))
        self.assertIn("BTS", self.registry.resolve_via("SK"))
        self.assertIn("BUD", self.registry.resolve_via("Macaristan"))
        self.assertIn("MUC", self.registry.resolve_via("Germany"))

    def test_resolve_via_city_and_airport_codes(self):
        self.assertEqual(self.registry.resolve_via("Prag"), ["PRG"])
        self.assertIn("ATH", self.registry.resolve_via("Atina"))
        self.assertEqual(self.registry.resolve_via("SKG"), ["SKG"])
        self.assertEqual(self.registry.resolve_via("ist"), ["IST"])
        self.assertIn("IST", self.registry.resolve_via("İstanbul"))
        self.assertIn("SAW", self.registry.resolve_via("İstanbul"))

    def test_resolve_via_unknown_or_empty_returns_empty_list(self):
        self.assertEqual(self.registry.resolve_via("xyz"), [])
        self.assertEqual(self.registry.resolve_via("Atlantis"), [])
        self.assertEqual(self.registry.resolve_via(""), [])
        self.assertEqual(self.registry.resolve_via(None), [])

    def test_country_airports_module_constant(self):
        from travel_engine.geo_clusters import COUNTRY_AIRPORTS
        self.assertIn("ATH", COUNTRY_AIRPORTS["GR"])
        self.assertEqual(COUNTRY_AIRPORTS["GR"][:2], ["ATH", "SKG"])
        for code in ["IST", "SAW", "ESB", "ADB", "AYT"]:
            self.assertIn(code, COUNTRY_AIRPORTS["TR"])
        for code, airport in [("CZ", "PRG"), ("AT", "VIE"), ("SK", "BTS"), ("HU", "BUD"), ("DE", "BER")]:
            self.assertIn(airport, COUNTRY_AIRPORTS[code])


class TestGeoClusterListing(unittest.TestCase):
    def setUp(self):
        self.registry = GeoClusterRegistry()

    def test_all_clusters_matches_list_cities(self):
        clusters = self.registry.all_clusters()
        self.assertIsInstance(clusters, list)
        self.assertEqual([c.city_name for c in clusters], self.registry.list_cities())
        self.assertTrue(all(isinstance(c, CityCluster) for c in clusters))

    def test_all_clusters_is_a_copy(self):
        self.registry.all_clusters().clear()
        self.assertEqual(len(self.registry.all_clusters()), len(self.registry.list_cities()))

    def test_stations_for_city_is_main_station_then_transit_stations(self):
        self.assertEqual(self.registry.stations_for("Vienna"), ["Wien Hauptbahnhof (Hbf)", "Wien Hbf"])
        self.assertEqual(self.registry.stations_for("Prague"),
                         ["Praha hlavní nádraží (hl.n.)", "Praha hl.n."])

    def test_stations_for_airport_adds_stations_of_links_leaving_it(self):
        # Budapest Kelenföld is where the RegioJet BUD -> Prague trains leave, not Keleti.
        self.assertEqual(self.registry.stations_for("BUD"), ["Budapest Keleti", "Budapest Kelenföld"])
        # Munich's airport is served by a bus link from Prague's cluster that leaves München ZOB.
        self.assertEqual(self.registry.stations_for("MUC"),
                         ["München Hauptbahnhof", "München Hbf", "München ZOB"])

    def test_stations_for_is_unique_and_follows_cluster_lookup(self):
        names = self.registry.stations_for("Prag")
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names, self.registry.stations_for("PRG"))
        self.assertEqual(names, self.registry.stations_for("Prague"))

    def test_stations_for_non_cluster_is_empty(self):
        self.assertEqual(self.registry.stations_for("Wien Hbf"), [])  # a station, not a cluster
        self.assertEqual(self.registry.stations_for("Atlantis"), [])
        self.assertEqual(self.registry.stations_for(""), [])
        self.assertEqual(self.registry.stations_for(None), [])

    def test_is_country_for_registry_tables_only(self):
        for token in ["Greece", "Yunanistan", "Türkiye", "Germany", "GR", "sk", "Czechia"]:
            self.assertTrue(self.registry.is_country(token), token)
        for token in ["Athens", "Atina", "ATH", "Milano", "Prag", "", None]:
            self.assertFalse(self.registry.is_country(token), token)


if __name__ == "__main__":
    unittest.main()
