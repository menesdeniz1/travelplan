import json
import os
import shutil
import tempfile
import unittest
from io import BytesIO
from unittest import mock

import server_v2
from server_v2 import TravelPlanAPIHandler
from travel_engine.daemon_tracker import DaemonTracker
from travel_engine.storage import DB_ENV_VAR, NO_MIGRATE_ENV_VAR
from travel_engine.tracker import PriceTracker


OUTBOUND_QUERY = {
    "depart_dates": ["2026-10-27", "2026-10-28"],
    "arrive_by": "2026-10-28T12:00",
    "depart_after": None,
    "via": None,
    "include_side_routes": True,
}


def leg(type_, operator, frm, to, dep, arr, price, estimated=False):
    return {
        "type": type_, "operator": operator, "from": frm, "to": to,
        "depart_at": dep, "arrive_at": arr, "price_try": price,
        "is_estimated": estimated, "booking_url": "https://example.test/book",
    }


def option(kind, price, legs, dep, arr, duration_mins, via_stop=None):
    return {
        "kind": kind, "depart_at": dep, "arrive_at": arr,
        "total_try": price, "true_total_try": price, "duration_mins": duration_mins,
        "separate_tickets": kind == "via_separate", "via_stop": via_stop,
        "night_wait": False, "has_estimated_segments": False, "score": 0.0,
        "badges": [], "legs": legs,
    }


class FakePlanner:
    """Stands in for TripPlanner; prices are set per test and read on each call."""

    def __init__(self, direct_price=None, side_price=None, warnings=None, error=None):
        self.direct_price = direct_price
        self.side_price = side_price
        self.warnings = list(warnings or [])
        self.error = error
        self.calls = []

    def plan_trip(self, origin, destination, depart_dates, arrive_by=None, depart_after=None, via=None,
                  include_side_routes=True, time_value_try_per_hour=150.0, max_results=10):
        self.calls.append(dict(
            origin=origin, destination=destination, depart_dates=list(depart_dates),
            arrive_by=arrive_by, depart_after=depart_after, via=via,
            include_side_routes=include_side_routes, time_value_try_per_hour=time_value_try_per_hour,
        ))
        if self.error is not None:
            raise self.error
        options, best_direct, best_side = [], None, None
        if self.direct_price is not None:
            best_direct = option(
                "direct", self.direct_price,
                [leg("flight", "Aegean", origin, destination, "2026-10-27T09:00", "2026-10-27T11:30", self.direct_price)],
                "2026-10-27T09:00", "2026-10-27T11:30", 150,
            )
            options.append(best_direct)
        if self.side_price is not None:
            best_side = option(
                "side_route", self.side_price,
                [
                    leg("flight", "Ryanair", "ATH", "BTS", "2026-10-27T19:30", "2026-10-27T20:45", 2000.0),
                    leg("train", "RegioJet", "BTS", "PRG", "2026-10-27T22:00", "2026-10-28T01:30", self.side_price - 2000.0),
                ],
                "2026-10-27T19:30", "2026-10-28T01:30", 360, via_stop="BTS",
            )
            options.append(best_side)
        return {
            "query": {"origin": origin, "destination": destination},
            "options": options,
            "best_direct": best_direct,
            "best_side_route": best_side,
            "searched_dates": list(depart_dates),
            "flight_searches": 2,
            "warnings": list(self.warnings),
        }


class FakeNotifier:
    """Records alert calls instead of sending anything."""

    def __init__(self):
        self.alerts = []
        self.mm_alerts = []

    def send_alert(self, **kwargs):
        self.alerts.append(kwargs)
        return dict(kwargs)

    def send_multimodal_alert(self, **kwargs):
        self.mm_alerts.append(kwargs)
        return dict(kwargs)


class RecordingProvider:
    """Flight provider stand-in that counts searches and returns nothing."""

    def __init__(self):
        self.calls = 0

    def search_flights(self, *args, **kwargs):
        self.calls += 1
        return []


class TripWatchTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="trip_watches_")
        self.storage = os.path.join(self.tmpdir, "watches.json")
        self.tracker = PriceTracker(storage_path=self.storage)
        self.notifier = FakeNotifier()
        self.planner = FakePlanner()
        self.provider = RecordingProvider()
        self.daemon = DaemonTracker(
            tracker=self.tracker, notifier=self.notifier, provider=self.provider, trip_planner=self.planner,
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def make_outbound_watch(self, **overrides):
        kwargs = dict(
            origin="ATH",
            destination="PRG",
            target_budget_try=7000.0,
            direct_budget_try=10000.0,
            trip_query=dict(OUTBOUND_QUERY),
            include_multimodal=False,
        )
        kwargs.update(overrides)
        return self.tracker.add_watch(**kwargs)


class TestTripWatchAlerts(TripWatchTestBase):
    def test_alert_uses_cheapest_option_not_best_scored(self):
        # Best-scored direct (fast) is over budget, but a slower one is under it -> must alert
        self.planner.direct_price = 11000.0
        original = self.planner.plan_trip

        def plan_with_cheaper_slow_flight(*args, **kwargs):
            plan = original(*args, **kwargs)
            slow = option("direct", 9200.0,
                          [leg("flight", "TAROM", "ATH", "PRG", "2026-10-27T06:00", "2026-10-27T18:00", 9200.0)],
                          "2026-10-27T06:00", "2026-10-27T18:00", 720)
            plan["options"].append(slow)
            plan["cheapest_direct"] = slow
            return plan

        self.planner.plan_trip = plan_with_cheaper_slow_flight
        res = self.daemon.scan_watch(self.make_outbound_watch())
        self.assertEqual(res["best_direct_price"], 9200.0)
        self.assertEqual(len(self.notifier.alerts), 1)
        self.assertEqual(self.notifier.alerts[0]["current_price"], 9200.0)

    def test_alerts_for_direct_under_10000_and_side_under_7000(self):
        self.planner.direct_price = 9500.0
        self.planner.side_price = 6500.0
        watch = self.make_outbound_watch()

        res = self.daemon.scan_watch(watch)

        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["triggered"])
        self.assertEqual(res["best_direct_price"], 9500.0)
        self.assertEqual(res["best_side_price"], 6500.0)
        self.assertEqual(len(self.notifier.alerts), 1)
        self.assertEqual(self.notifier.alerts[0]["current_price"], 9500.0)
        self.assertEqual(self.notifier.alerts[0]["target_budget"], 10000.0)
        self.assertEqual(len(self.notifier.mm_alerts), 1)
        self.assertEqual(self.notifier.mm_alerts[0]["multimodal_price"], 6500.0)
        self.assertEqual(self.notifier.mm_alerts[0]["target_budget"], 7000.0)
        # The planner received the watch's rules
        self.assertEqual(self.planner.calls[0]["origin"], "ATH")
        self.assertEqual(self.planner.calls[0]["destination"], "PRG")
        self.assertEqual(self.planner.calls[0]["depart_dates"], ["2026-10-27", "2026-10-28"])
        self.assertEqual(self.planner.calls[0]["arrive_by"], "2026-10-28T12:00")
        # Trip watches never run the single-date flight search
        self.assertEqual(self.provider.calls, 0)

    def test_side_route_segments_summary_format(self):
        self.planner.side_price = 6500.0
        watch = self.make_outbound_watch()

        self.daemon.scan_watch(watch)

        summary = self.notifier.mm_alerts[0]["segments_summary"]
        self.assertIn("✈️ Ryanair ATH→BTS 27.10 19:30–20:45", summary)
        self.assertIn("🚆 RegioJet BTS→PRG 27.10 22:00–01:30", summary)
        self.assertEqual(self.notifier.mm_alerts[0]["departure_date"], "2026-10-27")

    def test_no_duplicate_alert_at_same_price(self):
        self.planner.direct_price = 9500.0
        self.planner.side_price = 6500.0
        watch = self.make_outbound_watch()

        self.daemon.scan_watch(watch)
        res = self.daemon.scan_watch(watch)

        self.assertTrue(res["triggered"])  # budget still met
        self.assertEqual(len(self.notifier.alerts), 1)
        self.assertEqual(len(self.notifier.mm_alerts), 1)

    def test_re_alerts_on_further_drop(self):
        self.planner.direct_price = 9500.0
        self.planner.side_price = 6500.0
        watch = self.make_outbound_watch()
        self.daemon.scan_watch(watch)

        self.planner.direct_price = 9000.0
        self.daemon.scan_watch(watch)

        self.assertEqual(len(self.notifier.alerts), 2)
        self.assertEqual(self.notifier.alerts[1]["current_price"], 9000.0)
        # Side price did not drop, so no second side alert
        self.assertEqual(len(self.notifier.mm_alerts), 1)

    def test_no_alert_above_budgets_but_reading_recorded(self):
        self.planner.direct_price = 10500.0
        self.planner.side_price = 7500.0
        watch = self.make_outbound_watch()

        res = self.daemon.scan_watch(watch)

        self.assertFalse(res["triggered"])
        self.assertEqual(self.notifier.alerts, [])
        self.assertEqual(self.notifier.mm_alerts, [])
        self.assertIsNone(watch.last_trip_alert)
        self.assertEqual([r.price_try for r in watch.history], [7500.0])

    def test_reading_is_best_overall_true_total(self):
        self.planner.direct_price = 9500.0
        self.planner.side_price = 6500.0
        watch = self.make_outbound_watch()

        self.daemon.scan_watch(watch)

        self.assertEqual(watch.history[-1].price_try, 6500.0)

    def test_categories_alert_independently(self):
        self.planner.direct_price = 9000.0   # direct under budget
        self.planner.side_price = 8000.0     # side above its budget
        watch = self.make_outbound_watch()

        self.daemon.scan_watch(watch)

        self.assertEqual(len(self.notifier.alerts), 1)
        self.assertEqual(self.notifier.mm_alerts, [])
        self.assertEqual(watch.last_trip_alert, {"direct": 9000.0})

    def test_no_direct_alert_without_direct_budget(self):
        self.planner.direct_price = 5000.0
        watch = self.make_outbound_watch(direct_budget_try=None)

        res = self.daemon.scan_watch(watch)

        self.assertFalse(res["triggered"])
        self.assertEqual(self.notifier.alerts, [])

    def test_planner_warnings_are_returned(self):
        self.planner.side_price = 8000.0
        self.planner.warnings = ["Tren seferi bulunamadı"]
        watch = self.make_outbound_watch()

        res = self.daemon.scan_watch(watch)

        self.assertEqual(res["warnings"], ["Tren seferi bulunamadı"])

    def test_planner_exception_returns_error_without_alerts(self):
        self.planner.error = RuntimeError("arama başarısız")
        watch = self.make_outbound_watch()

        res = self.daemon.scan_watch(watch)

        self.assertEqual(res["status"], "error")
        self.assertFalse(res["triggered"])
        self.assertEqual(self.notifier.alerts, [])
        self.assertEqual(watch.history, [])

    def test_no_planner_skips_trip_watch_without_flight_search(self):
        daemon = DaemonTracker(tracker=self.tracker, notifier=self.notifier, provider=self.provider)
        watch = self.make_outbound_watch()

        res = daemon.scan_watch(watch)

        self.assertEqual(res["status"], "no_trip_planner")
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.notifier.alerts, [])

    def test_no_options_is_no_results(self):
        watch = self.make_outbound_watch()

        res = self.daemon.scan_watch(watch)

        self.assertEqual(res["status"], "no_results")
        self.assertFalse(res["triggered"])
        self.assertEqual(watch.history, [])


class TestTripWatchPersistence(TripWatchTestBase):
    def test_trip_fields_persist_through_save_and_load(self):
        self.planner.direct_price = 9500.0
        self.planner.side_price = 6500.0
        watch = self.make_outbound_watch()
        self.daemon.scan_watch(watch)

        reloaded = PriceTracker(storage_path=self.storage).get_watch(watch.id)

        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.trip_query, OUTBOUND_QUERY)
        self.assertEqual(reloaded.direct_budget_try, 10000.0)
        self.assertEqual(reloaded.target_budget_try, 7000.0)
        self.assertEqual(reloaded.departure_date, "2026-10-27")
        self.assertEqual(reloaded.last_trip_alert, {"direct": 9500.0, "side": 6500.0})

    def test_add_watch_sets_departure_date_from_first_depart_date(self):
        watch = self.make_outbound_watch(departure_date="2000-01-01")

        self.assertEqual(watch.departure_date, "2026-10-27")
        self.assertEqual(watch.trip_query["depart_dates"], ["2026-10-27", "2026-10-28"])

    def test_to_dict_includes_trip_fields(self):
        watch = self.make_outbound_watch()

        data = watch.to_dict()

        self.assertEqual(data["trip_query"], OUTBOUND_QUERY)
        self.assertEqual(data["direct_budget_try"], 10000.0)
        self.assertIsNone(data["last_trip_alert"])

    def test_old_watch_file_without_trip_fields_loads(self):
        old_entry = {
            "id": "watch_old_1",
            "origin": "VIE",
            "destination": "ATH",
            "target_budget_try": 4000.0,
            "departure_date": "2026-10-25",
            "history": [{"timestamp": "2026-10-01T10:00:00+00:00", "price_try": 4500.0}],
        }
        with open(self.storage, "w", encoding="utf-8") as f:
            json.dump({"watch_old_1": old_entry}, f)

        # The legacy JSON migration is off for the session (conftest.py); switch it on for this test only
        with mock.patch.dict(os.environ, {DB_ENV_VAR: os.path.join(self.tmpdir, "travelplan.db")}):
            os.environ.pop(NO_MIGRATE_ENV_VAR, None)
            loaded = PriceTracker(storage_path=self.storage).get_watch("watch_old_1")

        self.assertIsNone(loaded.trip_query)
        self.assertIsNone(loaded.direct_budget_try)
        self.assertIsNone(loaded.last_trip_alert)
        self.assertEqual(loaded.target_budget_try, 4000.0)
        self.assertEqual([r.price_try for r in loaded.history], [4500.0])


class TripServerTestBase(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.planner = FakePlanner(direct_price=9500.0, side_price=6500.0)
        patches = [
            mock.patch.object(server_v2, "trip_planner", self.planner),
            mock.patch.object(server_v2, "tracker", PriceTracker(storage_path=os.path.join(tmp_dir.name, "watches.json"))),
            mock.patch.object(server_v2.daemon_tracker, "scan_watch", lambda watch: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class MockAPIHandler(TravelPlanAPIHandler):
    def __init__(self, request_bytes):
        self.client_address = ("127.0.0.1", 12345)
        self.rfile = BytesIO(request_bytes)
        self.wfile = BytesIO()
        self.raw_requestline = self.rfile.readline()
        self.parse_request()

    def log_message(self, format, *args):
        pass


def _run(request_bytes, method):
    handler = MockAPIHandler(request_bytes)
    if method == "GET":
        handler.do_GET()
    else:
        handler.do_POST()
    raw = handler.wfile.getvalue()
    header_blob, body = raw.split(b"\r\n\r\n", 1)
    lines = header_blob.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    return status, body


def _get(path, host="localhost:8085"):
    return f"GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode("utf-8")


def _post(path, payload):
    body = json.dumps(payload).encode("utf-8")
    head = (
        f"POST {path} HTTP/1.1\r\nHost: localhost:8085\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode("utf-8")
    return head + body


class TestTripSearchEndpoint(TripServerTestBase):
    def _search(self, query):
        status, body = _run(_get(f"/api/trip-search?{query}"), "GET")
        return status, json.loads(body)

    def test_bad_date_returns_400(self):
        status, data = self._search("origin=Athens&destination=Prague&depart_dates=2026-13-45")
        self.assertEqual(status, 400)
        self.assertEqual(data["status"], "error")
        self.assertIn("Tarih", data["message"])
        self.assertEqual(self.planner.calls, [])

    def test_more_than_seven_dates_returns_400(self):
        dates = ",".join(f"2026-10-{d:02d}" for d in range(1, 9))  # 8 dates
        status, data = self._search(f"origin=Athens&destination=Prague&depart_dates={dates}")
        self.assertEqual(status, 400)
        self.assertIn("7", data["message"])
        self.assertEqual(self.planner.calls, [])

    def test_seven_dates_are_accepted(self):
        dates = ",".join(f"2026-10-{d:02d}" for d in range(1, 8))  # 7 dates
        status, _ = self._search(f"origin=Athens&destination=Prague&depart_dates={dates}")
        self.assertEqual(status, 200)

    def test_datetime_without_time_returns_400(self):
        status, data = self._search(
            "origin=Athens&destination=Prague&depart_dates=2026-10-28&arrive_by=2026-10-28"
        )
        self.assertEqual(status, 400)
        self.assertIn("Varış", data["message"])

    def test_missing_depart_dates_returns_400(self):
        status, _ = self._search("origin=Athens&destination=Prague")
        self.assertEqual(status, 400)

    def test_missing_destination_returns_400(self):
        status, _ = self._search("origin=Athens&depart_dates=2026-10-27")
        self.assertEqual(status, 400)

    def test_happy_path_returns_planner_result(self):
        status, data = self._search(
            "origin=Athens&destination=Prague&depart_dates=2026-10-27,2026-10-28"
            "&arrive_by=2026-10-28T12:00&via=GR&include_side_routes=1&time_value_try_per_hour=150"
        )

        self.assertEqual(status, 200)
        self.assertEqual(data["best_side_route"]["true_total_try"], 6500.0)
        self.assertEqual(data["best_direct"]["true_total_try"], 9500.0)
        call = self.planner.calls[-1]
        self.assertEqual(call["origin"], "Athens")
        self.assertEqual(call["depart_dates"], ["2026-10-27", "2026-10-28"])
        self.assertEqual(call["arrive_by"], "2026-10-28T12:00")
        self.assertEqual(call["via"], "GR")
        self.assertTrue(call["include_side_routes"])
        self.assertEqual(call["time_value_try_per_hour"], 150.0)

    def test_include_side_routes_zero_is_passed_through(self):
        status, _ = self._search(
            "origin=Athens&destination=Prague&depart_dates=2026-10-27&include_side_routes=0"
        )
        self.assertEqual(status, 200)
        self.assertFalse(self.planner.calls[-1]["include_side_routes"])

    def test_planner_exception_returns_500_json(self):
        self.planner.error = RuntimeError("boom")
        status, data = self._search("origin=Athens&destination=Prague&depart_dates=2026-10-27")
        self.assertEqual(status, 500)
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["error"], "boom")

    def test_foreign_host_is_rejected(self):
        status, _ = _run(_get("/api/trip-search?origin=A&destination=B&depart_dates=2026-10-27", host="evil.example"), "GET")
        self.assertEqual(status, 403)


class TestPostTripWatch(TripServerTestBase):
    def test_post_with_trip_creates_trip_watch(self):
        payload = {
            "origin": "Athens",
            "destination": "Prague",
            "target_budget_try": 7000.0,
            "direct_budget_try": 10000.0,
            "trip": {
                "depart_dates": ["2026-10-27", "2026-10-28"],
                "arrive_by": "2026-10-28T12:00",
                "depart_after": "",
                "via": None,
                "include_side_routes": True,
            },
        }

        status, body = _run(_post("/api/watches", payload), "POST")

        self.assertEqual(status, 201)
        watch_data = json.loads(body)["watch"]
        self.assertEqual(watch_data["departure_date"], "2026-10-27")
        self.assertEqual(watch_data["direct_budget_try"], 10000.0)
        self.assertEqual(watch_data["target_budget_try"], 7000.0)
        self.assertEqual(watch_data["trip_query"]["arrive_by"], "2026-10-28T12:00")
        self.assertIsNone(watch_data["trip_query"]["depart_after"])
        self.assertEqual(watch_data["trip_query"]["via"], None)
        stored = server_v2.tracker.get_watch(watch_data["id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored.trip_query["depart_dates"], ["2026-10-27", "2026-10-28"])

    def test_post_with_via_string_creates_trip_watch(self):
        payload = {
            "origin": "Prague",
            "destination": "Istanbul",
            "target_budget_try": 7000.0,
            "direct_budget_try": 10000.0,
            "trip": {"depart_dates": "2026-10-31", "arrive_by": "2026-10-31T23:59", "via": "GR"},
        }

        status, body = _run(_post("/api/watches", payload), "POST")

        self.assertEqual(status, 201)
        watch_data = json.loads(body)["watch"]
        self.assertEqual(watch_data["trip_query"]["via"], "GR")
        self.assertEqual(watch_data["trip_query"]["depart_dates"], ["2026-10-31"])
        self.assertTrue(watch_data["trip_query"]["include_side_routes"])

    def test_post_with_bad_trip_date_returns_400_and_creates_nothing(self):
        payload = {
            "origin": "Athens",
            "destination": "Prague",
            "target_budget_try": 7000.0,
            "trip": {"depart_dates": ["2026-02-30"]},
        }

        status, body = _run(_post("/api/watches", payload), "POST")

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["status"], "error")
        self.assertEqual(server_v2.tracker.watches, {})

    def test_post_with_too_many_trip_dates_returns_400(self):
        payload = {
            "origin": "Athens",
            "destination": "Prague",
            "target_budget_try": 7000.0,
            "trip": {"depart_dates": [f"2026-10-{d:02d}" for d in range(1, 9)]},
        }

        status, _ = _run(_post("/api/watches", payload), "POST")

        self.assertEqual(status, 400)
        self.assertEqual(server_v2.tracker.watches, {})

    def test_post_with_bad_direct_budget_returns_400(self):
        payload = {
            "origin": "Athens",
            "destination": "Prague",
            "target_budget_try": 7000.0,
            "direct_budget_try": "çok",
            "trip": {"depart_dates": ["2026-10-27"]},
        }

        status, _ = _run(_post("/api/watches", payload), "POST")

        self.assertEqual(status, 400)
        self.assertEqual(server_v2.tracker.watches, {})

    def test_post_without_trip_keeps_plain_watch(self):
        payload = {
            "origin": "Athens",
            "destination": "Prague",
            "target_budget_try": 5500.0,
            "departure_date": "2026-10-25",
        }

        status, body = _run(_post("/api/watches", payload), "POST")

        self.assertEqual(status, 201)
        watch_data = json.loads(body)["watch"]
        self.assertIsNone(watch_data["trip_query"])
        self.assertIsNone(watch_data["direct_budget_try"])
        self.assertEqual(watch_data["departure_date"], "2026-10-25")


if __name__ == "__main__":
    unittest.main()
