import unittest
import json
import os
import tempfile
from io import BytesIO
from unittest import mock
import server_v2
from server_v2 import TravelPlanAPIHandler
from travel_engine.tracker import PriceTracker
from travel_engine.geo_clusters import GeoClusterRegistry
from travel_engine.providers.rail_bus_provider import RailBusProvider
from travel_engine.multimodal_router import MultiModalRouter

class MockAPIHandler(TravelPlanAPIHandler):
    def __init__(self, request_bytes):
        self.client_address = ('127.0.0.1', 12345)
        self.rfile = BytesIO(request_bytes)
        self.wfile = BytesIO()
        self.raw_requestline = self.rfile.readline()
        self.parse_request()

class NoNetworkFlightProvider:
    """Stands in for GoogleFlightsProvider so endpoint tests never launch Playwright."""
    def search_flights(self, *args, **kwargs):
        return []


class TestServerEndpoints(unittest.TestCase):
    def setUp(self):
        self.geo_registry = GeoClusterRegistry()
        self.rail_provider = RailBusProvider(self.geo_registry)

        # Isolate handlers from live scraping, the real watches.json and background scans
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        patches = [
            mock.patch.object(server_v2.multimodal_router, "flight_provider", NoNetworkFlightProvider()),
            mock.patch.object(server_v2, "tracker", PriceTracker(storage_path=os.path.join(tmp_dir.name, "watches.json"))),
            mock.patch.object(server_v2.daemon_tracker, "scan_watch", lambda watch: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_clusters_api_handler_response(self):
        req = b'GET /api/clusters HTTP/1.1\r\nHost: localhost\r\n\r\n'
        handler = MockAPIHandler(req)
        handler.do_GET()
        raw_resp = handler.wfile.getvalue().decode('utf-8')
        header_part, body_part = raw_resp.split('\r\n\r\n', 1)
        self.assertIn("200 OK", header_part)
        self.assertIn("Content-Type: application/json", header_part)
        data = json.loads(body_part)
        self.assertGreaterEqual(data["count"], 5)
        prague_cluster = next((c for c in data["clusters"] if c["city"] == "Prague"), None)
        self.assertIsNotNone(prague_cluster)
        catchments = [c["code"] for c in prague_cluster["catchment_airports"]]
        self.assertIn("VIE", catchments)

    def test_multimodal_search_logic_integration(self):
        class SimpleFlightStub:
            def search_flights(self, origin, destination, departure_date, return_date=None, trip_type="oneway"):
                if "VIE" in destination.upper():
                    return [{
                        "airline": "Wizz Air",
                        "flight_number": "W6 123",
                        "origin": origin,
                        "destination": "VIE",
                        "departure_time": "09:30",
                        "arrival_time": "11:45",
                        "total_price_try": 3200.0,
                    }]
                elif "PRG" in destination.upper() or "PRAGUE" in destination.upper():
                    return [{
                        "airline": "Czech Airlines",
                        "flight_number": "OK 456",
                        "origin": origin,
                        "destination": "PRG",
                        "departure_time": "08:00",
                        "arrival_time": "10:30",
                        "total_price_try": 9800.0,
                    }]
                return []

        router = MultiModalRouter(
            flight_provider=SimpleFlightStub(),
            rail_provider=self.rail_provider,
            geo_registry=self.geo_registry,
        )
        res = router.search_multimodal(
            origin="Athens",
            destination="Prague",
            date="2026-10-25"
        )
        self.assertTrue(res["has_multimodal"])
        self.assertGreater(len(res["multimodal_routes"]), 0)
        first_route = res["multimodal_routes"][0]
        self.assertGreater(first_route["savings_try"], 5000.0)
        self.assertIn("map_points", first_route)
        self.assertIn("origin", first_route["map_points"])
        self.assertIn("destination", first_route["map_points"])
        self.assertTrue(first_route["segments"][0]["booking_url"].startswith("http"))

    def test_post_watches_include_multimodal(self):
        payload = json.dumps({
            "origin": "Athens",
            "destination": "Prague",
            "target_budget_try": 5500.0,
            "departure_date": "2026-10-25",
            "include_multimodal": True
        }).encode('utf-8')
        req = (
            f'POST /api/watches HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n'
        ).encode('utf-8') + payload

        handler = MockAPIHandler(req)
        handler.do_POST()
        raw_resp = handler.wfile.getvalue().decode('utf-8')
        header_part, body_part = raw_resp.split('\r\n\r\n', 1)
        self.assertIn("201 Created", header_part)
        data = json.loads(body_part)
        self.assertEqual(data.get("status"), "success")
        watch_data = data.get("watch", {})
        self.assertTrue(watch_data.get("include_multimodal"))

def _split_response(raw: bytes):
    header, body = raw.decode("utf-8").split("\r\n\r\n", 1)
    return int(header.split(" ")[1]), json.loads(body)


class RequiredParamsTests(unittest.TestCase):
    """Missing or malformed required parameters are a JSON 400, never a silently invented city or date.

    Before, these endpoints defaulted to Istanbul/Tokyo and to fixed 2026 dates. Those defaults were
    removed, so the tests below cover the 400 path only; nothing here reaches a flight scraper.
    """

    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tracker = PriceTracker(storage_path=os.path.join(tmp_dir.name, "watches.json"))
        patches = [
            mock.patch.object(server_v2, "tracker", self.tracker),
            mock.patch.object(server_v2, "google_flights_provider", NoNetworkFlightProvider()),
            mock.patch.object(server_v2.daemon_tracker, "scan_watch", lambda watch: None),
            # Telegram settings are written to config.json in BASE_DIR: never write it from a test
            mock.patch.object(server_v2.notifier, "save_config"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _get(self, path):
        handler = MockAPIHandler(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode("utf-8"))
        handler.do_GET()
        return _split_response(handler.wfile.getvalue())

    def _post(self, path, payload):
        body = json.dumps(payload).encode("utf-8")
        head = (f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n").encode("utf-8")
        handler = MockAPIHandler(head + body)
        handler.do_POST()
        return _split_response(handler.wfile.getvalue())

    def test_watch_without_origin_is_400_and_creates_nothing(self):
        status, body = self._post("/api/watches", {"destination": "Prague"})
        self.assertEqual(status, 400)
        self.assertEqual(body["status"], "error")
        self.assertIn("Kalkış şehri zorunludur", body["message"])
        self.assertEqual(self.tracker.watches, {})

    def test_watch_with_blank_destination_is_400(self):
        status, body = self._post("/api/watches", {"origin": "Athens", "destination": "   "})
        self.assertEqual(status, 400)
        self.assertIn("Varış şehri zorunludur", body["message"])
        self.assertEqual(self.tracker.watches, {})

    def test_watch_with_non_string_origin_is_400(self):
        status, body = self._post("/api/watches", {"origin": 123, "destination": "Prague"})
        self.assertEqual(status, 400)
        self.assertIn("metin olmalı", body["message"])
        self.assertEqual(self.tracker.watches, {})

    def test_price_history_requires_route_and_a_valid_date(self):
        status, body = self._get("/api/price-history")
        self.assertEqual(status, 400)
        self.assertIn("Kalkış şehri zorunludur", body["message"])

        status, body = self._get("/api/price-history?origin=Athens&destination=Prague")
        self.assertEqual(status, 400)
        self.assertIn("Gidiş tarihi zorunludur", body["message"])

        status, body = self._get("/api/price-history?origin=Athens&destination=Prague&departure_date=2026-13-40")
        self.assertEqual(status, 400)
        self.assertIn("Tarih", body["message"])

    def test_price_matrix_requires_destination(self):
        status, body = self._get("/api/price-matrix?origin=Athens&departure_date=2026-10-27")
        self.assertEqual(status, 400)
        self.assertIn("Varış şehri zorunludur", body["message"])

    def test_live_flights_requires_date_and_return_date_for_roundtrip(self):
        status, body = self._get("/api/live-flights?origin=Athens&destination=Prague")
        self.assertEqual(status, 400)
        self.assertIn("Gidiş tarihi zorunludur", body["message"])

        # trip_type defaults to roundtrip, so a missing return date is refused
        status, body = self._get("/api/live-flights?origin=Athens&destination=Prague&departure_date=2026-10-27")
        self.assertEqual(status, 400)
        self.assertIn("Dönüş tarihi zorunludur", body["message"])

    def test_removed_mock_endpoints_are_404(self):
        # /api/routes (mock optimizer) and /api/multimodal-search (old UI panel) were removed
        for path in ["/api/routes?origin=IST&destination=PRG&date=2026-10-25",
                     "/api/multimodal-search?origin=Athens&destination=Prague&date=2026-10-25"]:
            status, body = self._get(path)
            self.assertEqual(status, 404, path)
            self.assertIn("error", body)

    def test_telegram_non_string_bot_token_is_400_json(self):
        status, body = self._post("/api/settings/telegram", {"bot_token": 123456, "chat_id": "987654"})
        self.assertEqual(status, 400)
        self.assertEqual(body["status"], "error")
        self.assertIn("Bot token metin olmalı", body["message"])
        server_v2.notifier.save_config.assert_not_called()

    def test_telegram_numeric_chat_id_is_coerced_to_text(self):
        status, body = self._post("/api/settings/telegram", {"bot_token": "", "chat_id": 987654})
        self.assertEqual(status, 200)
        self.assertTrue(body["saved"])
        saved_chat_id = server_v2.notifier.save_config.call_args[0][1]
        self.assertEqual(saved_chat_id, "987654")

    def test_telegram_list_chat_id_is_400_json(self):
        status, body = self._post("/api/settings/telegram", {"chat_id": ["987654"]})
        self.assertEqual(status, 400)
        self.assertIn("Chat ID metin olmalı", body["message"])
        server_v2.notifier.save_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
