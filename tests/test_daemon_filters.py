import glob
import json
import os
import shutil
import tempfile
import threading
import unittest

from unittest import mock

from travel_engine.daemon_tracker import DaemonTracker
from travel_engine.providers.live_rail_provider import LiveRailProvider
from travel_engine.providers.rail_bus_provider import RailBusProvider
from travel_engine.storage import DB_ENV_VAR, NO_MIGRATE_ENV_VAR
from travel_engine.tracker import PriceTracker


def make_flight(airline, times, price, stops, duration="2h 15m"):
    return {
        "airline": airline,
        "times": times,
        "duration": duration,
        "stops_count": stops,
        "total_price_try": price,
        "google_flights_url": "https://www.google.com/travel/flights?fake=1",
    }


class FakeProvider:
    """Stands in for GoogleFlightsProvider; returns canned flight dicts."""

    def __init__(self, flights=None, on_search=None):
        self.flights = list(flights or [])
        self.on_search = on_search
        self.calls = 0

    def search_flights(self, origin, destination, departure_date, return_date=None, trip_type="oneway"):
        self.calls += 1
        if self.on_search:
            self.on_search()
        return [dict(f) for f in self.flights]


class FakeNotifier:
    """Records alert calls instead of sending anything."""

    def __init__(self):
        self.alerts = []
        self.mm_alerts = []

    def send_alert(self, **kwargs):
        self.alerts.append(kwargs)
        return kwargs

    def send_multimodal_alert(self, **kwargs):
        self.mm_alerts.append(kwargs)
        return kwargs


class FakeRailProvider:
    """Stands in for the server's RailBusProvider; identity is all these tests check."""


class DaemonFilterTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="daemon_filters_")
        self.storage = os.path.join(self.tmpdir, "watches.json")
        self.tracker = PriceTracker(storage_path=self.storage)
        self.notifier = FakeNotifier()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def make_daemon(self, flights, on_search=None):
        provider = FakeProvider(flights, on_search=on_search)
        daemon = DaemonTracker(tracker=self.tracker, notifier=self.notifier, provider=provider)
        return daemon, provider

    def make_watch(self, **overrides):
        kwargs = dict(
            origin="ATH",
            destination="PRG",
            target_budget_try=6000.0,
            departure_date="2026-10-25",
            include_multimodal=False,
        )
        kwargs.update(overrides)
        return self.tracker.add_watch(**kwargs)


class TestDaemonFilters(DaemonFilterTestBase):
    def test_direct_only_watch_rejects_one_stop_flights(self):
        daemon, _ = self.make_daemon([
            make_flight("Ryanair", "09:30 – 11:45", 3000.0, stops=1),
            make_flight("Pegasus", "13:00 – 15:10", 2500.0, stops=1),
        ])
        watch = self.make_watch(max_transfers=0)

        res = daemon.scan_watch(watch)

        self.assertEqual(res["status"], "no_flights_within_transfer_limit")
        self.assertFalse(res["triggered"])
        self.assertEqual(self.notifier.alerts, [])
        self.assertEqual(watch.history, [])
        self.assertIsNone(watch.last_alert_price)

    def test_direct_only_watch_picks_direct_flight_over_cheaper_one_stop(self):
        daemon, _ = self.make_daemon([
            make_flight("Ryanair", "09:30 – 11:45", 3000.0, stops=1),
            make_flight("Aegean", "10:00 – 12:00", 5000.0, stops=0),
        ])
        watch = self.make_watch(max_transfers=0)

        res = daemon.scan_watch(watch)

        self.assertNotIn("status", res)
        self.assertTrue(res["triggered"])
        self.assertEqual(res["current_price"], 5000.0)
        self.assertEqual(len(self.notifier.alerts), 1)
        self.assertEqual(self.notifier.alerts[0]["current_price"], 5000.0)

    def test_max_transfers_one_rejects_two_stop_only_results(self):
        daemon, _ = self.make_daemon([
            make_flight("Lufthansa", "07:00 – 13:00", 2000.0, stops=2),
        ])
        watch = self.make_watch(max_transfers=1)

        res = daemon.scan_watch(watch)

        self.assertEqual(res["status"], "no_flights_within_transfer_limit")
        self.assertFalse(res["triggered"])
        self.assertEqual(self.notifier.alerts, [])

    def test_time_slot_mismatch_still_reports_no_flights_in_time_slot(self):
        daemon, _ = self.make_daemon([
            make_flight("Ryanair", "09:30 – 11:45", 3000.0, stops=0),
        ])
        watch = self.make_watch(departure_time_slot="evening")

        res = daemon.scan_watch(watch)

        self.assertEqual(res["status"], "no_flights_in_time_slot")
        self.assertFalse(res["triggered"])
        self.assertEqual(self.notifier.alerts, [])

    def test_search_runs_without_tracker_lock_held(self):
        lock = self.tracker._lock
        seen_by_other_thread = []

        def probe_lock_from_other_thread():
            # RLock.acquire from the same thread always succeeds, so probe from another thread.
            def target():
                got = lock.acquire(blocking=False)
                if got:
                    lock.release()
                seen_by_other_thread.append(got)

            t = threading.Thread(target=target)
            t.start()
            t.join()

        daemon, _ = self.make_daemon(
            [make_flight("Aegean", "10:00 – 12:00", 5000.0, stops=0)],
            on_search=probe_lock_from_other_thread,
        )
        watch = self.make_watch(max_transfers=0)

        daemon.scan_watch(watch)

        self.assertEqual(seen_by_other_thread, [True])


class TestTrackerLoadSave(DaemonFilterTestBase):
    def setUp(self):
        # conftest.py turns the legacy JSON migration off for the session; these tests exercise the
        # legacy-file path, so it is switched back on here. The database goes into a temp dir set
        # before the base setUp creates the tracker, so every tracker in a test shares that file.
        db_dir = tempfile.mkdtemp(prefix="daemon_db_")
        self.addCleanup(shutil.rmtree, db_dir, True)
        env = mock.patch.dict(os.environ, {DB_ENV_VAR: os.path.join(db_dir, "travelplan.db")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(NO_MIGRATE_ENV_VAR, None)
        super().setUp()

    def test_load_keeps_in_memory_reading_added_after_file_written(self):
        watch = self.make_watch()
        watch.add_reading(7777.0)  # not yet persisted

        self.tracker.load()

        self.assertIs(self.tracker.watches[watch.id], watch)
        self.assertEqual([r.price_try for r in watch.history], [7777.0])

    def test_load_adds_watches_written_by_another_instance(self):
        other = PriceTracker(storage_path=self.storage)
        other.add_watch(
            origin="VIE", destination="ATH", target_budget_try=4000.0, watch_id="watch_other",
        )

        self.tracker.load()

        self.assertIn("watch_other", self.tracker.watches)
        self.assertEqual(self.tracker.watches["watch_other"].origin, "VIE")

    def test_load_does_not_prune_by_default_but_prune_true_removes_deleted(self):
        watch = self.make_watch()
        other = PriceTracker(storage_path=self.storage)
        other.delete_watch(watch.id)

        self.tracker.load()
        self.assertIn(watch.id, self.tracker.watches)

        self.tracker.load(prune=True)
        self.assertNotIn(watch.id, self.tracker.watches)

    def test_corrupt_legacy_file_keeps_memory_and_is_never_overwritten(self):
        # The database is authoritative after migration, so a corrupt watches.json that
        # appears later is only logged. It is never read as data and never overwritten.
        watch = self.make_watch()
        corrupt_text = "{this is not json"
        with open(self.storage, "w", encoding="utf-8") as f:
            f.write(corrupt_text)

        with self.assertLogs("travel_engine.storage", level="WARNING"):
            self.tracker.load()

        self.assertIs(self.tracker.watches[watch.id], watch)

        self.tracker.save()

        with open(self.storage, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), corrupt_text)
        self.assertEqual(glob.glob(self.storage + ".corrupt-*"), [])
        # The watch was written to the database, not to the JSON file.
        self.assertIn(watch.id, PriceTracker(storage_path=self.storage).watches)

    def test_corrupt_legacy_file_at_startup_starts_empty_and_is_kept(self):
        corrupt_text = "[1, 2"
        with open(self.storage, "w", encoding="utf-8") as f:
            f.write(corrupt_text)

        with self.assertLogs("travel_engine.storage", level="WARNING"):
            fresh = PriceTracker(storage_path=self.storage)
        self.assertEqual(fresh.watches, {})
        fresh.save()

        with open(self.storage, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), corrupt_text)
        self.assertEqual(glob.glob(self.storage + ".migrated*"), [])

        # Not recorded as migrated, so a repaired file is imported on the next start.
        with open(self.storage, "w", encoding="utf-8") as f:
            json.dump({"watch_repaired": {
                "id": "watch_repaired", "origin": "VIE", "destination": "ATH",
                "target_budget_try": 4000.0,
            }}, f)
        repaired = PriceTracker(storage_path=self.storage)
        self.assertIn("watch_repaired", repaired.watches)
        self.assertFalse(os.path.exists(self.storage))


class TestDaemonProviderParam(DaemonFilterTestBase):
    def test_injected_provider_is_used_for_daemon_and_router(self):
        provider = FakeProvider([])
        daemon = DaemonTracker(tracker=self.tracker, notifier=self.notifier, provider=provider)

        self.assertIs(daemon.provider, provider)
        self.assertIs(daemon.multimodal_router.flight_provider, provider)

    def test_injected_rail_provider_is_reused_by_daemon_and_router(self):
        rail = FakeRailProvider()
        daemon = DaemonTracker(
            tracker=self.tracker, notifier=self.notifier, provider=FakeProvider([]), rail_provider=rail,
        )

        self.assertIs(daemon.rail_provider, rail)
        self.assertIs(daemon.multimodal_router.rail_provider, rail)

    def test_without_rail_provider_daemon_builds_its_own_as_before(self):
        daemon = DaemonTracker(tracker=self.tracker, notifier=self.notifier, provider=FakeProvider([]))

        self.assertIsInstance(daemon.rail_provider, RailBusProvider)
        self.assertIsInstance(daemon.rail_provider.live_provider, LiveRailProvider)
        self.assertIs(daemon.multimodal_router.rail_provider, daemon.rail_provider)

    def test_default_rail_provider_includes_flixbus_bus_source(self):
        # Stub the FlixBus class so the test never touches the network.
        class StubFlixBus:
            def __init__(self):
                self.constructed = True

        with mock.patch("travel_engine.providers.flixbus_provider.FlixBusProvider", StubFlixBus):
            daemon = DaemonTracker(tracker=self.tracker, notifier=self.notifier, provider=FakeProvider([]))

        self.assertIsInstance(daemon.rail_provider.bus_provider, StubFlixBus)
        self.assertIs(daemon.multimodal_router.rail_provider, daemon.rail_provider)

    def test_default_rail_provider_has_the_same_ground_sources_as_the_server(self):
        from travel_engine.providers.google_maps_transit_provider import GoogleMapsTransitProvider
        from travel_engine.providers.obilet_fares import ObiletFromPrices
        from travel_engine.providers.trenitalia_provider import TrenitaliaProvider

        daemon = DaemonTracker(tracker=self.tracker, notifier=self.notifier, provider=FakeProvider([]))
        rail = daemon.rail_provider
        self.assertIsInstance(rail.trenitalia_provider, TrenitaliaProvider)
        self.assertIsInstance(rail.timetable_maps_provider, GoogleMapsTransitProvider)
        self.assertIsInstance(rail.bus_from_price_provider, ObiletFromPrices)
        self.assertEqual(set(rail.sources_health()),
                         {"regiojet", "flixbus", "trenitalia", "transitous", "google_maps", "obilet"})


# ---------------------------------------------------------------- trip watches with route-engine rules

def _leg_dict(type_, operator, frm, to, dep, arr, price):
    return {"type": type_, "operator": operator, "from": frm, "to": to, "depart_at": dep,
            "arrive_at": arr, "price_try": price, "is_estimated": False, "booking_url": ""}


def _option_dict(kind, total, legs, dep, arr, minutes):
    return {"kind": kind, "depart_at": dep, "arrive_at": arr, "true_total_try": total,
            "total_try": total, "duration_mins": minutes, "legs": legs, "score": total}


class RecordingTripPlanner:
    """Records plan_trip keyword arguments and returns a canned plan (no network)."""

    def __init__(self, plan=None):
        self.plan = plan if plan is not None else {"options": [], "warnings": []}
        self.calls = []

    def plan_trip(self, origin, destination, depart_dates, **kwargs):
        self.calls.append(dict(origin=origin, destination=destination, depart_dates=list(depart_dates), **kwargs))
        return dict(self.plan)


class TripWatchRouteOptionTests(DaemonFilterTestBase):
    ROUTE_QUERY = {
        "depart_dates": ["2030-10-27"],
        "arrive_by": None,
        "depart_after": None,
        "via": None,
        "include_side_routes": True,
        "max_stops": 1,
        "flight_search_budget": 30,
        "min_stay_mins": {"Selanik": 480},
        "cabin_bag": False,
        "checked_bag": True,
        "flex_days": 2,
        "return_dates": ["2030-11-02"],
        "return_arrive_by": "2030-11-02T18:00",
        "return_depart_after": None,
        "return_via": ["Budapeşte"],
        "min_stay_hours": 24.0,
    }

    def make_trip_daemon(self, plan=None):
        planner = RecordingTripPlanner(plan)
        daemon = DaemonTracker(tracker=self.tracker, notifier=self.notifier,
                               provider=FakeProvider([]), trip_planner=planner)
        return daemon, planner

    def test_route_engine_keys_are_passed_to_plan_trip(self):
        daemon, planner = self.make_trip_daemon()
        watch = self.make_watch(trip_query=dict(self.ROUTE_QUERY), direct_budget_try=9000.0)
        daemon.scan_watch(watch)
        call = planner.calls[-1]
        for key in ("max_stops", "flight_search_budget", "min_stay_mins", "cabin_bag", "checked_bag",
                    "flex_days", "return_dates", "return_arrive_by", "return_via", "min_stay_hours"):
            self.assertEqual(call[key], self.ROUTE_QUERY[key], key)

    def test_unknown_keys_are_not_passed_and_old_watches_send_only_old_keys(self):
        daemon, planner = self.make_trip_daemon()
        query = dict(OLD_TRIP_QUERY, junk="x")
        daemon.scan_watch(self.make_watch(trip_query=query, direct_budget_try=9000.0))
        self.assertEqual(set(planner.calls[-1]),
                         {"origin", "destination", "depart_dates", "arrive_by", "depart_after", "via",
                          "include_side_routes"})

    def test_round_trip_alert_uses_cheapest_combo_total_against_side_budget(self):
        out = _option_dict("direct", 4000.0,
                           [_leg_dict("flight", "Ryanair", "ATH", "BTS", "2030-10-27T07:55", "2030-10-27T09:10", 4000.0)],
                           "2030-10-27T07:55", "2030-10-27T09:10", 75)
        back_a = _option_dict("direct", 2500.0,
                              [_leg_dict("flight", "Wizz Air", "BTS", "ATH", "2030-11-02T06:00", "2030-11-02T08:00", 2500.0)],
                              "2030-11-02T06:00", "2030-11-02T08:00", 120)
        back_b = _option_dict("direct", 3000.0,
                              [_leg_dict("flight", "Wizz Air", "BTS", "ATH", "2030-11-02T12:00", "2030-11-02T14:00", 3000.0)],
                              "2030-11-02T12:00", "2030-11-02T14:00", 120)
        combos = [
            {"outbound": out, "return": back_a, "total_try": 6500.0, "stay_hours": 100.0, "label": "a"},
            {"outbound": out, "return": back_b, "total_try": 7000.0, "stay_hours": 100.0, "label": "b"},
        ]
        plan = {"options": [out], "warnings": [], "cheapest_direct": out, "best_direct": out,
                "roundtrip": {"return": {}, "combos": combos}}
        daemon, _ = self.make_trip_daemon(plan)
        watch = self.make_watch(trip_query=dict(OLD_TRIP_QUERY, return_dates=["2030-11-02"]),
                                target_budget_try=6600.0, direct_budget_try=None)
        res = daemon.scan_watch(watch)
        self.assertEqual(res["roundtrip_price"], 6500.0)
        self.assertEqual(res["best_side_price"], 6500.0)
        self.assertEqual(len(self.notifier.mm_alerts), 1)
        alert = self.notifier.mm_alerts[0]
        self.assertEqual(alert["multimodal_price"], 6500.0)
        self.assertEqual(alert["target_budget"], 6600.0)
        # The alert lists the outbound and the return legs
        self.assertIn("ATH→BTS", alert["segments_summary"])
        self.assertIn("BTS→ATH", alert["segments_summary"])
        # The alert says it is a round trip, and that the total covers both ways
        self.assertIn("Gidiş-dönüş", alert["segments_summary"])
        self.assertTrue(alert["segments_summary"].startswith("Gidiş-dönüş"))

    def test_round_trip_over_side_budget_does_not_alert(self):
        out = _option_dict("direct", 4000.0, [], "2030-10-27T07:55", "2030-10-27T09:10", 75)
        back = _option_dict("direct", 5000.0, [], "2030-11-02T06:00", "2030-11-02T08:00", 120)
        plan = {"options": [out], "warnings": [], "roundtrip": {"return": {}, "combos": [
            {"outbound": out, "return": back, "total_try": 9000.0, "stay_hours": 100.0}]}}
        daemon, _ = self.make_trip_daemon(plan)
        watch = self.make_watch(trip_query=dict(OLD_TRIP_QUERY, return_dates=["2030-11-02"]),
                                target_budget_try=7000.0, direct_budget_try=None)
        res = daemon.scan_watch(watch)
        self.assertFalse(res["triggered"])
        self.assertEqual(self.notifier.mm_alerts, [])
        self.assertEqual(res["roundtrip_price"], 9000.0)

    def test_one_way_plan_without_combos_keeps_the_side_route_alert(self):
        side = _option_dict("side_route", 6000.0, [_leg_dict("train", "RegioJet", "BTS", "PRG",
                                                             "2030-10-27T10:00", "2030-10-27T13:00", 6000.0)],
                            "2030-10-27T10:00", "2030-10-27T13:00", 180)
        plan = {"options": [side], "warnings": [], "cheapest_side_route": side, "best_side_route": side}
        daemon, _ = self.make_trip_daemon(plan)
        res = daemon.scan_watch(self.make_watch(trip_query=dict(OLD_TRIP_QUERY), target_budget_try=7000.0,
                                                direct_budget_try=None))
        self.assertTrue(res["triggered"])
        self.assertIsNone(res["roundtrip_price"])
        self.assertEqual(self.notifier.mm_alerts[0]["multimodal_price"], 6000.0)
        self.assertNotIn("Gidiş-dönüş", self.notifier.mm_alerts[0]["segments_summary"])

    def test_cheapest_roundtrip_option_helper(self):
        from travel_engine.daemon_tracker import _cheapest_roundtrip_option
        self.assertIsNone(_cheapest_roundtrip_option({}))
        self.assertIsNone(_cheapest_roundtrip_option({"roundtrip": {"combos": [{"total_try": "x"}]}}))


OLD_TRIP_QUERY = {
    "depart_dates": ["2030-10-27"],
    "arrive_by": None,
    "depart_after": None,
    "via": None,
    "include_side_routes": True,
}


if __name__ == "__main__":
    unittest.main()
