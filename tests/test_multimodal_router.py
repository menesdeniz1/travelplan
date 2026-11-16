import unittest
from travel_engine.multimodal_router import MultiModalRouter, MultiModalJourney, extract_flight_times, extract_flight_day_offset
from travel_engine.geo_clusters import GeoClusterRegistry
from travel_engine.providers.rail_bus_provider import RailBusProvider

class MockFlightProvider:
    """Deterministic mock flight provider for router tests."""
    def __init__(self, cheap_direct=False, late_evening=False, midnight_red_eye=False):
        self.cheap_direct = cheap_direct
        self.late_evening = late_evening
        self.midnight_red_eye = midnight_red_eye
        self.calls = []

    def search_flights(self, origin, destination, departure_date, return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        orig = origin.upper()
        dest = destination.upper()

        if self.midnight_red_eye and ("VIE" in dest or "VIENNA" in dest):
            return [{
                "airline": "RedEye Air",
                "flight_number": "RE 101",
                "origin": "ATH",
                "destination": "VIE",
                "departure_time": "23:45",
                "arrival_time": "01:30",
                "duration_mins": 105,
                "total_price_try": 3100.0,
                "stops": 0,
            }]

        if self.late_evening and ("VIE" in dest or "VIENNA" in dest):
            return [{
                "airline": "Austrian Airlines",
                "flight_number": "OS 802",
                "origin": "ATH",
                "destination": "VIE",
                "departure_time": "21:00",
                "arrival_time": "23:15",
                "duration_mins": 135,
                "total_price_try": 3500.0,
                "stops": 0,
            }]

        # Direct flight Athens -> Prague
        if ("ATH" in orig or "ATHENS" in orig or "ATINA" in orig) and ("PRG" in dest or "PRAGUE" in dest or "PRAG" in dest):
            direct_price = 2000.0 if self.cheap_direct else 11500.0
            return [
                {
                    "airline": "Aegean Airlines",
                    "flight_number": "A3 864",
                    "origin": "ATH",
                    "destination": "PRG",
                    "departure_time": "09:00",
                    "arrival_time": "10:45",
                    "duration_mins": 165,
                    "total_price_try": direct_price,
                    "stops": 0,
                }
            ]
        # Catchment flight Athens -> Vienna
        elif ("ATH" in orig or "ATHENS" in orig or "ATINA" in orig) and ("VIE" in dest or "VIENNA" in dest or "VIYANA" in dest):
            return [
                {
                    "airline": "Ryanair",
                    "flight_number": "FR 1234",
                    "origin": "ATH",
                    "destination": "VIE",
                    "departure_time": "10:15",
                    "arrival_time": "12:30",
                    "duration_mins": 135,
                    "total_price_try": 3650.0,
                    "stops": 0,
                }
            ]
        # Catchment flight Athens -> Bratislava
        elif ("ATH" in orig or "ATHENS" in orig or "ATINA" in orig) and ("BTS" in dest or "BRATISLAVA" in dest):
            return [
                {
                    "airline": "Ryanair",
                    "flight_number": "FR 5678",
                    "origin": "ATH",
                    "destination": "BTS",
                    "departure_time": "08:30",
                    "arrival_time": "10:00",
                    "duration_mins": 150,
                    "total_price_try": 4100.0,
                    "stops": 0,
                }
            ]
        return []

class TestMultiModalRouter(unittest.TestCase):
    def setUp(self):
        self.geo_registry = GeoClusterRegistry()
        self.rail_provider = RailBusProvider(self.geo_registry)
        self.flight_provider = MockFlightProvider()
        self.router = MultiModalRouter(
            flight_provider=self.flight_provider,
            rail_provider=self.rail_provider,
            geo_registry=self.geo_registry,
        )

    def test_finds_vienna_and_bratislava_multimodal_routes_to_prague(self):
        result = self.router.search_multimodal(
            origin="Athens",
            destination="Prague",
            date="2026-10-25"
        )
        self.assertTrue(result["has_multimodal"])
        self.assertGreater(len(result["multimodal_routes"]), 0)

        # Check savings calculation
        best_route = result["multimodal_routes"][0]
        self.assertGreater(best_route["savings_try"], 5000.0)
        self.assertEqual(best_route["destination_city"], "Prague")
        self.assertIn("flight", best_route["segments"][0]["type"])
        self.assertIn("train", best_route["segments"][-1]["type"])

    def test_respects_arrival_deadline(self):
        # Must arrive before 15:00
        result = self.router.search_multimodal(
            origin="Athens",
            destination="Prague",
            date="2026-10-25",
            arrival_deadline="15:00"
        )
        for route in result["multimodal_routes"]:
            arr_h = int(route["final_arrival_time"].split(":")[0])
            self.assertLessEqual(arr_h, 15)

    def test_respects_max_duration_hours(self):
        # Journey must not exceed 8 hours (480 mins)
        result = self.router.search_multimodal(
            origin="Athens",
            destination="Prague",
            date="2026-10-25",
            max_duration_hours=8.0
        )
        for route in result["multimodal_routes"]:
            self.assertLessEqual(route["total_duration_mins"], 480)

    def test_enforces_minimum_transfer_buffer(self):
        result = self.router.search_multimodal(
            origin="Athens",
            destination="Prague",
            date="2026-10-25"
        )
        for route in result["multimodal_routes"]:
            flight_arr = route["segments"][0]["arrival_time"]
            train_dep = route["segments"][-1]["departure_time"]
            f_mins = int(flight_arr.split(":")[0]) * 60 + int(flight_arr.split(":")[1])
            t_mins = int(train_dep.split(":")[0]) * 60 + int(train_dep.split(":")[1])
            self.assertGreaterEqual(t_mins - f_mins, 90)

    def test_turkish_city_name_resolves_multimodal_routes(self):
        result = self.router.search_multimodal(
            origin="Atina",
            destination="Prag",
            date="2026-10-25"
        )
        self.assertTrue(result["has_multimodal"])
        self.assertEqual(result["destination_cluster"], "Prague")
        self.assertGreater(len(result["multimodal_routes"]), 0)

    def test_late_evening_arrival_never_produces_negative_transfer(self):
        # Late evening provider: flight lands at 23:15 at VIE
        late_flight_provider = MockFlightProvider(late_evening=True)
        late_router = MultiModalRouter(
            flight_provider=late_flight_provider,
            rail_provider=self.rail_provider,
            geo_registry=self.geo_registry,
        )
        result = late_router.search_multimodal(
            origin="Athens",
            destination="Prague",
            date="2026-10-25"
        )
        for route in result["multimodal_routes"]:
            transfer_seg = next(s for s in route["segments"] if s["type"] == "transfer")
            self.assertGreaterEqual(transfer_seg["duration_mins"], 90)
            self.assertGreater(route["total_duration_mins"], 0)

    def test_negative_savings_does_not_award_best_value_badge(self):
        # Direct flight is cheap (₺2000), making multimodal savings negative
        cheap_direct_provider = MockFlightProvider(cheap_direct=True)
        cheap_router = MultiModalRouter(
            flight_provider=cheap_direct_provider,
            rail_provider=self.rail_provider,
            geo_registry=self.geo_registry,
        )
        result = cheap_router.search_multimodal(
            origin="Athens",
            destination="Prague",
            date="2026-10-25"
        )
        for route in result["multimodal_routes"]:
            self.assertNotIn("En İyi Değer", route["badges"])

    def test_all_route_ids_are_unique(self):
        result = self.router.search_multimodal(
            origin="Athens",
            destination="Prague",
            date="2026-10-25"
        )
        route_ids = [r["route_id"] for r in result["multimodal_routes"]]
        self.assertEqual(len(route_ids), len(set(route_ids)))

    def test_map_points_and_booking_urls(self):
        result = self.router.search_multimodal(
            origin="Athens",
            destination="Prague",
            date="2026-10-25"
        )
        self.assertGreater(len(result["multimodal_routes"]), 0)
        route = result["multimodal_routes"][0]

        mp = route.get("map_points")
        self.assertIsNotNone(mp)
        self.assertIn("origin", mp)
        self.assertIn("catchment", mp)
        self.assertIn("destination", mp)
        self.assertIn("lat", mp["origin"])
        self.assertIn("lon", mp["origin"])
        self.assertIn("lat", mp["destination"])

        flight_seg = next(s for s in route["segments"] if s["type"] == "flight")
        self.assertTrue(flight_seg["booking_url"].startswith("http"))
        self.assertIn("google.com/travel/flights", flight_seg["booking_url"])

        ground_seg = next(s for s in route["segments"] if s["type"] in ("train", "bus"))
        self.assertTrue(ground_seg["booking_url"].startswith("http"))

    def test_midnight_crossing_flight_calculates_positive_duration(self):
        red_eye_provider = MockFlightProvider(midnight_red_eye=True)
        red_eye_router = MultiModalRouter(
            flight_provider=red_eye_provider,
            rail_provider=self.rail_provider,
            geo_registry=self.geo_registry,
        )
        result = red_eye_router.search_multimodal(
            origin="Athens",
            destination="Prague",
            date="2026-10-25"
        )
        self.assertTrue(result["has_multimodal"])
        for route in result["multimodal_routes"]:
            self.assertGreater(route["total_duration_mins"], 0)
            self.assertGreaterEqual(route["segments"][1]["duration_mins"], 90)

    def test_midnight_red_eye_is_flagged_as_night_wait_with_hotel(self):
        # Lands VIE 01:30 next day; first train 06:00 -> 270 min overlap with 00:00-06:00
        red_eye_router = MultiModalRouter(
            flight_provider=MockFlightProvider(midnight_red_eye=True),
            rail_provider=self.rail_provider,
            geo_registry=self.geo_registry,
        )
        result = red_eye_router.search_multimodal(origin="Athens", destination="Prague", date="2026-10-25")
        vie_routes = [r for r in result["multimodal_routes"] if r["segments"][0]["to"] == "VIE"]
        self.assertTrue(vie_routes)
        for route in vie_routes:
            self.assertEqual(route["arrival_day_offset"], 1)
            self.assertTrue(route["night_wait"])
            self.assertGreaterEqual(route["night_wait_mins"], 180)
            self.assertEqual(route["estimated_hotel_try"], 3600.0)  # Vienna catchment estimate
            self.assertTrue(route["segments"][1]["is_night_wait"])
            self.assertAlmostEqual(route["true_total_try"], route["total_price_try"] + 3600.0)

    def test_extract_flight_day_offset_reads_plus_n_suffix(self):
        self.assertEqual(extract_flight_day_offset({"times": "23:10 – 1:35+1"}), 1)
        self.assertEqual(extract_flight_day_offset({"times": "19:30 – 21:45"}), 0)

    def test_prefetched_direct_flights_skips_direct_flight_scrape(self):
        prefetched = [{
            "airline": "Direct Air",
            "flight_number": "DA 1",
            "origin": "ATH",
            "destination": "PRG",
            "departure_time": "10:00",
            "arrival_time": "12:00",
            "duration_mins": 120,
            "total_price_try": 12000.0,
            "stops": 0,
        }]
        result = self.router.search_multimodal(
            origin="Athens",
            destination="Prague",
            date="2026-10-25",
            prefetched_direct_flights=prefetched
        )
        prg_calls = [c for c in self.flight_provider.calls if "PRG" in c[1].upper() or "PRAGUE" in c[1].upper()]
        self.assertEqual(len(prg_calls), 0)
        self.assertEqual(result["cheapest_direct_flight"]["total_price_try"], 12000.0)


class GoogleFormatFlightProvider:
    """Returns flights shaped like GoogleFlightsProvider._parse_flight_item (only a "times" string)."""

    def __init__(self, vie_times="19:30 – 21:45"):
        self.vie_times = vie_times

    def search_flights(self, origin, destination, departure_date, return_date=None, trip_type="oneway"):
        dest = destination.upper()
        if "VIE" in dest:
            return [{"airline": "Aegean", "times": self.vie_times, "duration": "2 sa. 15 dk.", "total_price_try": 3500.0}]
        if "PRG" in dest or "PRAGUE" in dest:
            return [{"airline": "Aegean", "times": "09:00 – 10:45", "total_price_try": 11500.0}]
        return []


class TestRealProviderFlightFormat(unittest.TestCase):
    def _search(self, vie_times, **kwargs):
        registry = GeoClusterRegistry()
        router = MultiModalRouter(
            flight_provider=GoogleFormatFlightProvider(vie_times),
            rail_provider=RailBusProvider(registry),
            geo_registry=registry,
        )
        return router.search_multimodal(origin="Athens", destination="Prague", date="2026-10-25", **kwargs)

    def test_extract_flight_times_from_google_times_string(self):
        self.assertEqual(extract_flight_times({"times": "19:30 – 21:45"}), ("19:30", "21:45"))
        self.assertEqual(extract_flight_times({"times": "23:10 – 1:35+1"}), ("23:10", "01:35"))
        self.assertEqual(
            extract_flight_times({"departure_time": "08:00", "arrival_time": "10:00", "times": "x"}),
            ("08:00", "10:00"),
        )
        self.assertIsNone(extract_flight_times({"times": "Bilinmiyor"}))
        self.assertIsNone(extract_flight_times({}))

    def test_trains_depart_after_real_flight_arrival(self):
        result = self._search("09:30 – 11:45")
        self.assertTrue(result["has_multimodal"])
        for route in result["multimodal_routes"]:
            flight, transfer, ground = route["segments"]
            self.assertEqual(flight["departure_time"], "09:30")
            self.assertEqual(flight["arrival_time"], "11:45")
            self.assertEqual(flight["duration_mins"], 135)
            self.assertGreaterEqual(transfer["duration_mins"], 90)
            dep_h, dep_m = map(int, ground["departure_time"].split(":"))
            self.assertGreaterEqual(dep_h * 60 + dep_m, 11 * 60 + 45 + 90)

    def test_late_flight_respects_arrival_deadline(self):
        # Lands VIE 21:45; no train can reach Prague by 22:00
        result = self._search("19:30 – 21:45", arrival_deadline="22:00")
        self.assertFalse(result["has_multimodal"])

    def test_flight_without_parsable_times_is_skipped(self):
        result = self._search("Bilinmiyor")
        self.assertFalse(result["has_multimodal"])


if __name__ == "__main__":
    unittest.main()
