"""Route engine parameters of /api/trip-search and POST /api/watches (validation and happy paths).

The planner is patched, so nothing touches the network. Importing server_v2 is what these tests need.
"""

import json
import os
import tempfile
import unittest
import urllib.parse
from io import BytesIO
from unittest import mock

import server_v2
from server_v2 import TravelPlanAPIHandler, _normalize_trip_query
from travel_engine.tracker import PriceTracker

OLD_KEYS = {"depart_dates", "arrive_by", "depart_after", "via", "include_side_routes"}


class MockAPIHandler(TravelPlanAPIHandler):
    def __init__(self, request_bytes):
        self.client_address = ("127.0.0.1", 12345)
        self.rfile = BytesIO(request_bytes)
        self.wfile = BytesIO()
        self.raw_requestline = self.rfile.readline()
        self.parse_request()

    def log_message(self, format, *args):
        pass


def _send(request_bytes, method):
    handler = MockAPIHandler(request_bytes)
    if method == "GET":
        handler.do_GET()
    else:
        handler.do_POST()
    raw = handler.wfile.getvalue()
    header_blob, body = raw.split(b"\r\n\r\n", 1)
    status = int(header_blob.decode("latin-1").split("\r\n")[0].split(" ")[1])
    return status, json.loads(body.decode("utf-8"))


def _get(path):
    return _send(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode("utf-8"), "GET")


def _post(path, payload):
    body = json.dumps(payload).encode("utf-8")
    head = (f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode("utf-8")
    return _send(head + body, "POST")


class RecordingPlanner:
    def __init__(self):
        self.calls = []

    def plan_trip(self, origin, destination, depart_dates, **kwargs):
        self.calls.append(dict(origin=origin, destination=destination, depart_dates=depart_dates, **kwargs))
        return {"query": {"origin": origin}, "options": [], "warnings": [], "budget_exhausted": False}


class NormalizeRouteParamsTests(unittest.TestCase):
    def test_route_engine_params_are_normalised(self):
        trip = _normalize_trip_query({
            "depart_dates": "2030-10-28",
            "max_stops": "2",
            "flight_search_budget": "40",
            "min_stay_mins": "Selanik:480;Budapeşte:120",
            "cabin_bag": "0",
            "checked_bag": "1",
            "flex_days": "2",
            "return_dates": "2030-11-02",
            "return_arrive_by": "2030-11-02T18:00",
            "return_via": "Budapeşte",
            "min_stay_hours": "24",
        })
        self.assertEqual(trip["max_stops"], 2)
        self.assertEqual(trip["flight_search_budget"], 40)
        self.assertEqual(trip["min_stay_mins"], {"Selanik": 480, "Budapeşte": 120})
        self.assertIs(trip["cabin_bag"], False)
        self.assertIs(trip["checked_bag"], True)
        self.assertEqual(trip["flex_days"], 2)
        self.assertEqual(trip["return_dates"], ["2030-11-02"])
        self.assertEqual(trip["return_arrive_by"], "2030-11-02T18:00")
        self.assertEqual(trip["return_via"], "Budapeşte")
        self.assertEqual(trip["min_stay_hours"], 24.0)

    def test_real_data_only_flag_is_normalised(self):
        on = _normalize_trip_query({"depart_dates": "2030-10-28", "real_data_only": "1"})
        self.assertIs(on["real_data_only"], True)
        off = _normalize_trip_query({"depart_dates": "2030-10-28", "real_data_only": "0"})
        self.assertIs(off["real_data_only"], False)
        absent = _normalize_trip_query({"depart_dates": "2030-10-28", "real_data_only": ""})
        self.assertNotIn("real_data_only", absent)
        with self.assertRaises(ValueError) as caught:
            _normalize_trip_query({"depart_dates": "2030-10-28", "real_data_only": "belki"})
        self.assertIn("Sadece gerçek veriler için 0 veya 1", str(caught.exception))

    def test_absent_params_leave_the_old_shape(self):
        trip = _normalize_trip_query({"depart_dates": "2030-10-28", "via": ""})
        self.assertEqual(set(trip), OLD_KEYS)

    def test_min_stay_accepts_a_dict_from_json(self):
        trip = _normalize_trip_query({"depart_dates": ["2030-10-28"], "min_stay_mins": {"Selanik": 300}})
        self.assertEqual(trip["min_stay_mins"], {"Selanik": 300})

    def test_via_list_keeps_order(self):
        trip = _normalize_trip_query({"depart_dates": "2030-10-28", "via": "Budapeşte,Viyana"})
        self.assertEqual(trip["via"], ["Budapeşte", "Viyana"])

    def test_return_via_list_keeps_order(self):
        trip = _normalize_trip_query({"depart_dates": "2030-10-28", "return_dates": "2030-11-02",
                                      "return_via": ["Budapeşte", "Viyana"]})
        self.assertEqual(trip["return_via"], ["Budapeşte", "Viyana"])

    def test_invalid_values_raise_turkish_messages(self):
        cases = [
            ({"max_stops": "4"}, "Ara durak sayısı 0 ile 3"),
            ({"max_stops": "x"}, "Ara durak sayısı sayı olmalı"),
            ({"flight_search_budget": "0"}, "Arama bütçesi 1 ile 60"),
            ({"flight_search_budget": "61"}, "Arama bütçesi 1 ile 60"),
            ({"flex_days": "4"}, "Esnek tarih günü 0 ile 3"),
            ({"return_dates": "2030-11-02", "min_stay_hours": "721"}, "Minimum kalış (saat) 0 ile 720"),
            ({"min_stay_mins": "Selanik"}, "Minimum kalış biçimi geçersiz"),
            ({"min_stay_mins": "Selanik:çok"}, "dakika olmalı"),
            ({"cabin_bag": "maybe"}, "Kabin bagajı için 0 veya 1"),
            ({"return_dates": ",".join(f"2030-11-0{d}" for d in range(1, 9))}, "En fazla 7 dönüş tarihi"),
            ({"return_arrive_by": "2030-11-02T18:00"}, "dönüş tarihi girin"),
            ({"return_dates": "2030-11-02", "return_arrive_by": "02.11.2030"}, "Dönüş varış zamanı"),
        ]
        for extra, message in cases:
            with self.subTest(extra=extra):
                raw = {"depart_dates": "2030-10-28", **extra}
                with self.assertRaises(ValueError) as caught:
                    _normalize_trip_query(raw)
                self.assertIn(message, str(caught.exception))


class TripSearchEndpointParamsTests(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.planner = RecordingPlanner()
        patches = [
            mock.patch.object(server_v2, "trip_planner", self.planner),
            mock.patch.object(server_v2, "tracker",
                              PriceTracker(storage_path=os.path.join(tmp_dir.name, "watches.json"))),
            mock.patch.object(server_v2.daemon_tracker, "scan_watch", lambda watch: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_real_data_only_is_forwarded_and_small_bag_is_the_default(self):
        base = urllib.parse.urlencode({"origin": "Atina", "destination": "Prag", "depart_dates": "2030-10-28"})
        status, _ = _get(f"/api/trip-search?{base}&real_data_only=1")
        self.assertEqual(status, 200)
        self.assertIs(self.planner.calls[-1]["real_data_only"], True)
        status, _ = _get(f"/api/trip-search?{base}")
        self.assertEqual(status, 200)
        self.assertNotIn("real_data_only", self.planner.calls[-1])  # planner default: off
        self.assertNotIn("cabin_bag", self.planner.calls[-1])       # planner default: small bag only
        status, body = _get(f"/api/trip-search?{base}&real_data_only=maybe")
        self.assertEqual(status, 400)

    def test_happy_path_forwards_every_route_parameter(self):
        query = urllib.parse.urlencode({
            "origin": "Atina", "destination": "Prag", "depart_dates": "2030-10-28", "max_stops": "1",
            "budget": "40", "min_stay": "Selanik:480", "cabin_bag": "0", "checked_bag": "1", "flex_days": "1",
            "return_dates": "2030-11-02", "return_arrive_by": "2030-11-02T18:00", "return_via": "Budapeşte",
            "min_stay_hours": "24", "via": "Budapeşte,Viyana",
        })
        status, body = _get(f"/api/trip-search?{query}")
        self.assertEqual(status, 200)
        self.assertEqual(body["query"]["origin"], "Atina")
        call = self.planner.calls[-1]
        self.assertEqual(call["max_stops"], 1)
        self.assertEqual(call["flight_search_budget"], 40)
        self.assertEqual(call["min_stay_mins"], {"Selanik": 480})
        self.assertIs(call["cabin_bag"], False)
        self.assertIs(call["checked_bag"], True)
        self.assertEqual(call["flex_days"], 1)
        self.assertEqual(call["return_dates"], ["2030-11-02"])
        self.assertEqual(call["return_arrive_by"], "2030-11-02T18:00")
        self.assertEqual(call["return_via"], "Budapeşte")
        self.assertEqual(call["min_stay_hours"], 24.0)
        self.assertEqual(call["via"], ["Budapeşte", "Viyana"])

    def test_no_route_parameters_calls_the_planner_with_the_old_keys_only(self):
        status, _ = _get("/api/trip-search?origin=Atina&destination=Prag&depart_dates=2030-10-28")
        self.assertEqual(status, 200)
        self.assertEqual(set(self.planner.calls[-1]) - {"origin", "destination", "time_value_try_per_hour"},
                         OLD_KEYS)

    def test_invalid_budget_is_a_turkish_400_and_the_planner_is_not_called(self):
        status, body = _get("/api/trip-search?origin=Atina&destination=Prag&depart_dates=2030-10-28&budget=99")
        self.assertEqual(status, 400)
        self.assertEqual(body["status"], "error")
        self.assertIn("Arama bütçesi 1 ile 60", body["message"])
        self.assertEqual(self.planner.calls, [])

    def test_invalid_max_stops_and_flex_are_400(self):
        for query in ("max_stops=5", "flex_days=9", "min_stay_hours=-1"):
            with self.subTest(query=query):
                status, body = _get(f"/api/trip-search?origin=Atina&destination=Prag&depart_dates=2030-10-28&{query}")
                self.assertEqual(status, 400)
                self.assertEqual(body["status"], "error")
        self.assertEqual(self.planner.calls, [])


class WatchRouteParamsTests(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tracker = PriceTracker(storage_path=os.path.join(tmp_dir.name, "watches.json"))
        patches = [
            mock.patch.object(server_v2, "trip_planner", RecordingPlanner()),
            mock.patch.object(server_v2, "tracker", self.tracker),
            mock.patch.object(server_v2.daemon_tracker, "scan_watch", lambda watch: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_trip_watch_stores_route_engine_keys(self):
        payload = {
            "origin": "Athens", "destination": "Prague", "target_budget_try": 7000.0,
            "direct_budget_try": 10000.0,
            "trip": {
                "depart_dates": ["2030-10-28"], "max_stops": 1, "flight_search_budget": 30,
                "min_stay_mins": "Selanik:480", "checked_bag": "1", "flex_days": 1,
                "return_dates": ["2030-11-02"], "min_stay_hours": 24,
            },
        }
        status, body = _post("/api/watches", payload)
        self.assertEqual(status, 201)
        stored = self.tracker.get_watch(json.loads(json.dumps(body))["watch"]["id"]).trip_query
        self.assertEqual(stored["max_stops"], 1)
        self.assertEqual(stored["flight_search_budget"], 30)
        self.assertEqual(stored["min_stay_mins"], {"Selanik": 480})
        self.assertIs(stored["checked_bag"], True)
        self.assertEqual(stored["flex_days"], 1)
        self.assertEqual(stored["return_dates"], ["2030-11-02"])
        self.assertEqual(stored["min_stay_hours"], 24.0)

    def test_invalid_route_key_in_a_watch_is_a_400_and_creates_nothing(self):
        payload = {
            "origin": "Athens", "destination": "Prague", "target_budget_try": 7000.0,
            "trip": {"depart_dates": ["2030-10-28"], "max_stops": 9},
        }
        status, body = _post("/api/watches", payload)
        self.assertEqual(status, 400)
        self.assertIn("Ara durak sayısı", body["message"])
        self.assertEqual(self.tracker.watches, {})


class RouteEngineWiringTests(unittest.TestCase):
    def test_planner_uses_the_route_engine_and_the_shared_search_log(self):
        self.assertIsNotNone(server_v2.route_engine)
        self.assertIs(server_v2.trip_planner.route_engine, server_v2.route_engine)
        self.assertIs(server_v2.trip_planner.search_log, server_v2.search_log)
        self.assertIs(server_v2.route_engine.ground_provider, server_v2.rail_provider)
        self.assertIs(server_v2.route_engine.flight_provider, server_v2.google_flights_provider)


if __name__ == "__main__":
    unittest.main()
