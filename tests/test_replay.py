"""Tests for record/replay infrastructure (TST-1, card B09)."""

import copy
import os
import shutil
import tempfile
import unittest
from unittest import mock

from travel_engine.airports import AirportDB
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import RouteEngine


def make_flight(airline, times, price, airports):
    return {
        "source": "test",
        "airline": airline,
        "times": times,
        "duration": "2 sa.",
        "airports": airports,
        "stops": "Aktarmasız",
        "stops_count": 0,
        "is_direct": True,
        "total_price_try": float(price),
        "currency": "TRY",
        "trip_type": "Tek yön",
        "google_flights_url": "https://www.google.com/travel/flights?q=test",
    }


class FakeFlightProvider:
    def __init__(self, table=None):
        self.table = table or {}
        self.calls = []

    def search_flights(
        self,
        origin: str = "Istanbul",
        destination: str = "Prague",
        departure_date: str = "2026-10-25",
        return_date=None,
        trip_type="roundtrip",
        **kwargs,
    ):
        self.calls.append((origin, destination, departure_date))
        key = (origin, destination, departure_date)
        if key in self.table:
            return copy.deepcopy(self.table[key])
        return [
            {
                "airline": "Pegasus",
                "flight_number": "PC123",
                "departure_time": "10:00",
                "arrival_time": "12:00",
                "total_price_try": 3500.0,
                "origin": origin,
                "destination": destination,
                "departure_date": departure_date,
            }
        ]

    def get_calendar_prices(self, origin: str, destination: str, trip_type: str = "oneway", **kwargs):
        self.calls.append(("calendar", origin, destination))
        return {"2026-11-01": 3500.0, "2026-11-02": 3200.0}


class FakeGroundProvider:
    def __init__(self, segments=None):
        self.segments = segments or {}
        self.calls = []

    def find_ground(
        self,
        origin: str = "",
        dest: str = "",
        date_str: str = "",
        max_results: int = 10,
        from_coord=None,
        to_coord=None,
        allow_estimate: bool = True,
        maps_budget=None,
        **kwargs,
    ):
        self.calls.append((origin, dest, date_str))
        key = (origin, dest)
        if key in self.segments:
            return copy.deepcopy(self.segments[key])
        return [
            ScheduledSegment(
                origin_station=origin,
                dest_station=dest,
                departure_time="08:00",
                arrival_time="12:00",
                duration_mins=240,
                price_try=800.0,
                operator="RegioJet",
                mode="train",
                date=date_str,
                is_estimated=False,
                price_eur=22.0,
            )
        ]

    def find_ground_live(self, origin="", dest="", date_str="", **kwargs):
        return self.find_ground(origin, dest, date_str, **kwargs)

    def find_ground_next(self, origin="", dest="", date_str="", **kwargs):
        return self.find_ground(origin, dest, date_str, **kwargs)

    def get_hub_connections(self, origin="", dest="", date_str="", **kwargs):
        return self.find_ground(origin, dest, date_str, **kwargs)

    def get_schedules(self, origin="", dest="", date_str="", **kwargs):
        return self.find_ground(origin, dest, date_str, **kwargs)

    def ground_distance_km(self, *args, **kwargs):
        self.calls.append(("distance", args, kwargs))
        return 330.5


class ReplayInfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_import_and_env_mode_detection(self):
        from travel_engine.replay import (
            get_replay_mode,
            wrap_flight_provider,
            wrap_ground_provider,
        )

        dummy_fp = object()
        dummy_gp = object()

        # 1. No env vars set -> mode is None, wrappers return original objects
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_replay_mode())
            self.assertIs(wrap_flight_provider(dummy_fp), dummy_fp)
            self.assertIs(wrap_ground_provider(dummy_gp), dummy_gp)

        # 2. TRAVELPLAN_RECORD_DIR set -> mode is "record", providers are wrapped
        with mock.patch.dict(os.environ, {"TRAVELPLAN_RECORD_DIR": self.temp_dir}, clear=True):
            self.assertEqual(get_replay_mode(), "record")
            w_fp = wrap_flight_provider(dummy_fp)
            self.assertIsNot(w_fp, dummy_fp)
            self.assertEqual(w_fp.mode, "record")

        # 3. TRAVELPLAN_REPLAY_DIR set -> mode is "replay", providers are wrapped
        with mock.patch.dict(os.environ, {"TRAVELPLAN_REPLAY_DIR": self.temp_dir}, clear=True):
            self.assertEqual(get_replay_mode(), "replay")
            w_gp = wrap_ground_provider(dummy_gp)
            self.assertIsNot(w_gp, dummy_gp)
            self.assertEqual(w_gp.mode, "replay")

    def test_flight_provider_record_and_replay(self):
        from travel_engine.replay import RecordReplayFlightProvider

        fake = FakeFlightProvider()
        recorder = RecordReplayFlightProvider(provider=fake, storage_dir=self.temp_dir, mode="record")

        # Call in record mode
        res1 = recorder.search_flights("SAW", "PRG", "2026-11-01", trip_type="oneway")
        cal1 = recorder.get_calendar_prices("SAW", "PRG", trip_type="oneway")
        self.assertEqual(len(res1), 1)
        self.assertEqual(res1[0]["flight_number"], "PC123")
        self.assertEqual(cal1["2026-11-01"], 3500.0)
        self.assertEqual(len(fake.calls), 2)

        # Now create strict mock that raises if called
        strict_mock = mock.Mock()
        strict_mock.search_flights.side_effect = AssertionError("Underlying provider should not be called in replay!")
        strict_mock.get_calendar_prices.side_effect = AssertionError("Underlying provider should not be called in replay!")

        replayer = RecordReplayFlightProvider(provider=strict_mock, storage_dir=self.temp_dir, mode="replay")
        res2 = replayer.search_flights("SAW", "PRG", "2026-11-01", trip_type="oneway")
        cal2 = replayer.get_calendar_prices("SAW", "PRG", trip_type="oneway")

        self.assertEqual(res1, res2)
        self.assertEqual(cal1, cal2)
        strict_mock.search_flights.assert_not_called()
        strict_mock.get_calendar_prices.assert_not_called()
        self.assertEqual(replayer.stats()["replayed_calls"], 2)
        self.assertEqual(replayer.stats()["unrecorded_calls"], 0)

    def test_flight_provider_replay_unrecorded_call_returns_empty_and_counts(self):
        from travel_engine.replay import RecordReplayFlightProvider

        strict_mock = mock.Mock()
        strict_mock.search_flights.side_effect = AssertionError("Underlying provider should not be called!")
        strict_mock.get_calendar_prices.side_effect = AssertionError("Underlying provider should not be called!")

        replayer = RecordReplayFlightProvider(provider=strict_mock, storage_dir=self.temp_dir, mode="replay")

        # Unrecorded search_flights
        res = replayer.search_flights("XYZ", "ABC", "2026-12-01", trip_type="oneway")
        self.assertEqual(res, [])
        self.assertEqual(replayer.unrecorded_calls, 1)

        # Unrecorded get_calendar_prices
        cal = replayer.get_calendar_prices("XYZ", "ABC", trip_type="oneway")
        self.assertEqual(cal, {})
        self.assertEqual(replayer.unrecorded_calls, 2)

        strict_mock.search_flights.assert_not_called()
        strict_mock.get_calendar_prices.assert_not_called()

    def test_ground_provider_record_and_replay_with_segments(self):
        from travel_engine.replay import RecordReplayGroundProvider

        fake = FakeGroundProvider()
        recorder = RecordReplayGroundProvider(provider=fake, storage_dir=self.temp_dir, mode="record")

        # Record calls
        segs = recorder.find_ground("Prague", "Vienna", "2026-10-28")
        dist = recorder.ground_distance_km("Prague", "Vienna")
        live = recorder.find_ground_live("Prague", "Vienna", "2026-10-28")
        next_s = recorder.find_ground_next("Prague", "Vienna", "2026-10-28")
        hubs = recorder.get_hub_connections("Prague", "Vienna", "2026-10-28")
        sched = recorder.get_schedules("Prague", "Vienna", "2026-10-28")

        self.assertEqual(len(segs), 1)
        self.assertIsInstance(segs[0], ScheduledSegment)
        self.assertEqual(dist, 330.5)
        self.assertEqual(len(live), 1)
        self.assertEqual(len(next_s), 1)
        self.assertEqual(len(hubs), 1)
        self.assertEqual(len(sched), 1)

        # Replay with strict mock
        strict_mock = mock.Mock()
        strict_mock.find_ground.side_effect = AssertionError("Underlying provider called!")
        strict_mock.find_ground_live.side_effect = AssertionError("Underlying provider called!")
        strict_mock.find_ground_next.side_effect = AssertionError("Underlying provider called!")
        strict_mock.get_hub_connections.side_effect = AssertionError("Underlying provider called!")
        strict_mock.get_schedules.side_effect = AssertionError("Underlying provider called!")
        strict_mock.ground_distance_km.side_effect = AssertionError("Underlying provider called!")

        replayer = RecordReplayGroundProvider(provider=strict_mock, storage_dir=self.temp_dir, mode="replay")
        replayed_segs = replayer.find_ground("Prague", "Vienna", "2026-10-28")
        replayed_dist = replayer.ground_distance_km("Prague", "Vienna")
        replayed_live = replayer.find_ground_live("Prague", "Vienna", "2026-10-28")
        replayed_next = replayer.find_ground_next("Prague", "Vienna", "2026-10-28")
        replayed_hubs = replayer.get_hub_connections("Prague", "Vienna", "2026-10-28")
        replayed_sched = replayer.get_schedules("Prague", "Vienna", "2026-10-28")

        self.assertEqual(len(replayed_segs), 1)
        self.assertIsInstance(replayed_segs[0], ScheduledSegment)
        self.assertEqual(replayed_segs[0].origin_station, "Prague")
        self.assertEqual(replayed_segs[0].dest_station, "Vienna")
        self.assertEqual(replayed_segs[0].price_eur, 22.0)
        self.assertEqual(replayed_segs[0].operator, "RegioJet")
        self.assertEqual(replayed_dist, 330.5)
        self.assertEqual(len(replayed_live), 1)
        self.assertEqual(len(replayed_next), 1)
        self.assertEqual(len(replayed_hubs), 1)
        self.assertEqual(len(replayed_sched), 1)

        strict_mock.find_ground.assert_not_called()
        strict_mock.find_ground_live.assert_not_called()
        strict_mock.find_ground_next.assert_not_called()
        strict_mock.get_hub_connections.assert_not_called()
        strict_mock.get_schedules.assert_not_called()
        strict_mock.ground_distance_km.assert_not_called()
        self.assertEqual(replayer.stats()["replayed_calls"], 6)

    def test_ground_provider_replay_unrecorded_call(self):
        from travel_engine.replay import RecordReplayGroundProvider

        strict_mock = mock.Mock()
        strict_mock.find_ground.side_effect = AssertionError("Underlying provider called!")
        strict_mock.ground_distance_km.side_effect = AssertionError("Underlying provider called!")

        replayer = RecordReplayGroundProvider(provider=strict_mock, storage_dir=self.temp_dir, mode="replay")

        # Unrecorded find_ground returns []
        segs = replayer.find_ground("Nowhere", "Elsewhere", "2026-12-01")
        self.assertEqual(segs, [])
        self.assertEqual(replayer.unrecorded_calls, 1)

        # Unrecorded ground_distance_km returns None
        dist = replayer.ground_distance_km("Nowhere", "Elsewhere")
        self.assertIsNone(dist)
        self.assertEqual(replayer.unrecorded_calls, 2)

        strict_mock.find_ground.assert_not_called()
        strict_mock.ground_distance_km.assert_not_called()

    def test_attribute_delegation_and_passthrough(self):
        from travel_engine.replay import RecordReplayFlightProvider, RecordReplayGroundProvider

        fp_mock = mock.Mock()
        fp_mock.health_status.return_value = {"status": "ok"}
        fp_mock.resource_status.return_value = {"browsers": 1}
        fp_mock.cache = {"key": "value"}
        fp_mock.search_concurrency.return_value = 2

        wrapped_fp = RecordReplayFlightProvider(provider=fp_mock, storage_dir=self.temp_dir, mode="replay")
        self.assertEqual(wrapped_fp.health_status(), {"status": "ok"})
        self.assertEqual(wrapped_fp.resource_status(), {"browsers": 1})
        self.assertEqual(wrapped_fp.cache, {"key": "value"})
        self.assertEqual(wrapped_fp.search_concurrency(), 2)

        gp_mock = mock.Mock()
        gp_mock.sources_health.return_value = {"regiojet": "up"}
        gp_mock.maps_budget_limit = 50

        wrapped_gp = RecordReplayGroundProvider(provider=gp_mock, storage_dir=self.temp_dir, mode="replay")
        self.assertEqual(wrapped_gp.sources_health(), {"regiojet": "up"})
        self.assertEqual(wrapped_gp.maps_budget_limit, 50)

    def test_fixture_replay_from_disk(self):
        """Verify reading pre-recorded fixtures from tests/fixtures/replay/."""
        from travel_engine.replay import RecordReplayFlightProvider, RecordReplayGroundProvider

        fixture_dir = os.path.join(os.path.dirname(__file__), "fixtures", "replay")
        self.assertTrue(os.path.isdir(fixture_dir), f"Fixture dir missing: {fixture_dir}")

        fp = RecordReplayFlightProvider(provider=None, storage_dir=fixture_dir, mode="replay")
        res = fp.search_flights("SAW", "PRG", "2026-11-01", trip_type="oneway")
        self.assertTrue(len(res) > 0)
        self.assertEqual(res[0]["flight_number"], "PC1234")

        cal = fp.get_calendar_prices("SAW", "PRG", trip_type="oneway")
        self.assertEqual(cal.get("2026-11-01"), 2850.0)

        gp = RecordReplayGroundProvider(provider=None, storage_dir=fixture_dir, mode="replay")
        segs = gp.find_ground("PRG", "VIE", "2026-10-28")
        self.assertTrue(len(segs) > 0)
        self.assertIsInstance(segs[0], ScheduledSegment)
        self.assertEqual(segs[0].operator, "RegioJet")
        self.assertEqual(segs[0].price_eur, 18.5)

    def test_end_to_end_search_record_and_replay(self):
        """Record an end-to-end multi-modal search, then replay without network/mock calls."""
        from travel_engine.replay import (
            RecordReplayFlightProvider,
            RecordReplayGroundProvider,
        )

        day = "2026-11-12"
        flight_table = {
            ("BGY", "GRO", day): [
                make_flight("Ryanair", "08:00 – 09:30", 1200, "BGY ➔ GRO")
            ],
            ("MXP", "BCN", day): [
                make_flight("Iberia", "09:00 – 10:45", 4000, "MXP ➔ BCN")
            ],
        }
        segments_table = {
            ("Girona", "Barcelona"): [
                ScheduledSegment(
                    origin_station="Girona",
                    dest_station="Barcelona-Sants",
                    departure_time="11:00",
                    arrival_time="12:00",
                    duration_mins=60,
                    price_try=300.0,
                    operator="Renfe",
                    mode="train",
                    date=day,
                    is_estimated=False,
                    price_eur=10.0,
                )
            ]
        }

        # Step 1: Record run
        base_fp = FakeFlightProvider(table=flight_table)
        base_gp = FakeGroundProvider(segments=segments_table)
        rec_fp = RecordReplayFlightProvider(provider=base_fp, storage_dir=self.temp_dir, mode="record")
        rec_gp = RecordReplayGroundProvider(provider=base_gp, storage_dir=self.temp_dir, mode="record")

        engine1 = RouteEngine(
            rec_fp,
            rec_gp,
            airport_db=AirportDB.load_default(),
            costs=None,
        )
        res1 = engine1.search("Milano", "Barcelona", [day], max_stops=2, flight_search_budget=30)
        self.assertIsNotNone(res1.get("best"))

        # Step 2: Replay run with strict mocks that crash if called
        strict_fp = mock.Mock()
        strict_fp.search_flights.side_effect = AssertionError("Network/mock called on flight provider!")
        strict_fp.get_calendar_prices.side_effect = AssertionError("Network/mock called on flight provider!")

        strict_gp = mock.Mock()
        strict_gp.find_ground.side_effect = AssertionError("Network/mock called on ground provider!")
        strict_gp.ground_distance_km.side_effect = AssertionError("Network/mock called on ground provider!")

        rep_fp = RecordReplayFlightProvider(provider=strict_fp, storage_dir=self.temp_dir, mode="replay")
        rep_gp = RecordReplayGroundProvider(provider=strict_gp, storage_dir=self.temp_dir, mode="replay")

        engine2 = RouteEngine(
            rep_fp,
            rep_gp,
            airport_db=AirportDB.load_default(),
            costs=None,
        )
        res2 = engine2.search("Milano", "Barcelona", [day], max_stops=2, flight_search_budget=30)

        # Elapsed trace timing differs on every run, remove before comparison
        res1.pop("trace", None)
        res2.pop("trace", None)

        self.assertEqual(res1["best"]["legs"], res2["best"]["legs"])
        self.assertEqual(res1["best"]["true_total_try"], res2["best"]["true_total_try"])
        self.assertEqual(len(res1["options"]), len(res2["options"]))

        strict_fp.search_flights.assert_not_called()
        strict_gp.find_ground.assert_not_called()
        self.assertGreater(rep_fp.stats()["replayed_calls"], 0)
        self.assertEqual(rep_fp.stats()["unrecorded_calls"], 0)

    def test_server_v2_provider_wrapping(self):
        import server_v2
        from travel_engine.replay import wrap_flight_provider, wrap_ground_provider

        with mock.patch.dict(os.environ, {"TRAVELPLAN_RECORD_DIR": self.temp_dir}):
            wrapped_f = wrap_flight_provider(server_v2.google_flights_provider)
            wrapped_g = wrap_ground_provider(server_v2.rail_provider)
            self.assertEqual(wrapped_f.mode, "record")
            self.assertEqual(wrapped_g.mode, "record")



if __name__ == "__main__":
    unittest.main()
