import os
import unittest
import urllib.error
from unittest import mock
from travel_engine.providers.rail_bus_provider import RailBusProvider, ScheduledSegment
from travel_engine.providers.live_rail_provider import LiveRailProvider
from travel_engine.airports import AirportDB, haversine_km

_RAIL_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "rail")
_URLOPEN = "travel_engine.providers.live_rail_provider.urllib.request.urlopen"


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _load_rail_fixture(name: str) -> bytes:
    with open(os.path.join(_RAIL_FIXTURES, name), "rb") as f:
        return f.read()

class TestRailBusProvider(unittest.TestCase):
    def setUp(self):
        self.provider = RailBusProvider()

    def test_get_schedules_vienna_to_prague(self):
        schedules = self.provider.get_schedules(
            from_station="Wien Hbf",
            to_station="Praha hl.n.",
            date_str="2026-10-25"
        )
        self.assertTrue(len(schedules) >= 5)
        for seg in schedules:
            self.assertEqual(seg.origin_station, "Wien Hbf")
            self.assertEqual(seg.dest_station, "Praha hl.n.")
            self.assertEqual(seg.mode, "train")
            self.assertGreater(seg.price_try, 0)
            self.assertIn(":", seg.departure_time)
            self.assertIn(":", seg.arrival_time)

    def test_earliest_departure_filter(self):
        # Only return trains departing at or after 14:00
        schedules = self.provider.get_schedules(
            from_station="Wien Hbf",
            to_station="Praha hl.n.",
            date_str="2026-10-25",
            earliest_dep_time="14:00"
        )
        for seg in schedules:
            dep_hour = int(seg.departure_time.split(":")[0])
            self.assertGreaterEqual(dep_hour, 14)

    def test_catchment_airport_lookup_resolution(self):
        # Looking up by airport code "VIE" to "Prague"
        schedules = self.provider.get_hub_connections(
            from_hub="VIE",
            to_city="Prague",
            date_str="2026-10-25",
            earliest_dep_time="13:30"
        )
        self.assertTrue(len(schedules) > 0)
        self.assertEqual(schedules[0].dest_station, "Praha hl.n.")

    def test_unknown_corridor_returns_empty(self):
        schedules = self.provider.get_schedules(
            from_station="NonExistentStation",
            to_station="Praha hl.n.",
            date_str="2026-10-25"
        )
        self.assertEqual(schedules, [])

    def test_budapest_reverse_corridors_available(self):
        vie_bud = self.provider.get_schedules("Wien Hbf", "Budapest Keleti", "2026-10-25")
        self.assertTrue(len(vie_bud) >= 4)
        for s in vie_bud:
            self.assertEqual(s.origin_station, "Wien Hbf")
            self.assertEqual(s.dest_station, "Budapest Keleti")

        bts_bud = self.provider.get_schedules("Bratislava hl.st.", "Budapest Keleti", "2026-10-25")
        self.assertTrue(len(bts_bud) >= 4)
        for s in bts_bud:
            self.assertEqual(s.origin_station, "Bratislava hl.st.")
            self.assertEqual(s.dest_station, "Budapest Keleti")

    def test_budapest_keleti_to_prague_corridor(self):
        schedules = self.provider.get_schedules("Budapest Kelenföld", "Praha hl.n.", "2026-10-28")
        self.assertTrue(len(schedules) >= 4)
        for s in schedules:
            self.assertEqual(s.mode, "train")
            self.assertEqual(s.duration_mins, 420)
            self.assertEqual(s.price_try, 1100.0)
            self.assertTrue(s.is_estimated)

    def test_budapest_to_prague_via_hub_connections(self):
        schedules = self.provider.get_hub_connections("BUD", "Prague", "2026-10-28", earliest_dep_time="12:35")
        self.assertTrue(len(schedules) > 0)
        self.assertEqual(schedules[0].dest_station, "Praha hl.n.")
        self.assertEqual(schedules[0].departure_time, "14:00")

    def test_munich_zob_to_prague_bus_corridor(self):
        schedules = self.provider.get_schedules("München ZOB", "Praha hl.n.", "2026-10-28")
        self.assertTrue(len(schedules) >= 4)
        for s in schedules:
            self.assertEqual(s.mode, "bus")
            self.assertEqual(s.operator, "FlixBus")
            self.assertEqual(s.duration_mins, 300)
            self.assertEqual(s.price_try, 800.0)

    def test_munich_to_prague_via_hub_connections(self):
        schedules = self.provider.get_hub_connections("MUC", "Prague", "2026-10-28", earliest_dep_time="09:00")
        self.assertTrue(len(schedules) > 0)
        self.assertTrue(all(s.mode == "bus" for s in schedules))
        self.assertEqual(schedules[0].origin_station, "München ZOB")

class _StubLiveProvider:
    """Stands in for LiveRailProvider so these tests never touch the network."""

    def __init__(self, result=None, error=None, supported=(("Bratislava hl.st.", "Praha hl.n."),)):
        self.result = result
        self.error = error
        self.supported = set(supported)
        self.calls = []

    def supports(self, from_station, to_station):
        return (from_station, to_station) in self.supported

    def get_schedules(self, from_station, to_station, date_str, earliest_dep_time=None):
        self.calls.append((from_station, to_station, date_str, earliest_dep_time))
        if self.error is not None:
            raise self.error
        return self.result


def _live_segment(price_eur=None, price_try=0.0):
    return ScheduledSegment(
        origin_station="Bratislava hl.st.", dest_station="Praha hl.n.",
        departure_time="05:17", arrival_time="09:58", duration_mins=281,
        price_try=price_try, operator="RegioJet", mode="train", date="2026-10-28",
        is_estimated=False, price_eur=price_eur, price_is_estimated=price_eur is None,
    )


class RailBusProviderLiveTests(unittest.TestCase):
    BTS_PRG = ("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")

    def test_without_live_provider_behaviour_is_unchanged(self):
        plain = RailBusProvider()
        self.assertIsNone(plain.live_provider)
        segs = plain.get_schedules(*self.BTS_PRG)
        self.assertTrue(segs)
        self.assertTrue(all(s.is_estimated for s in segs))
        self.assertTrue(all(s.price_is_estimated for s in segs))
        self.assertTrue(all(s.price_eur is None for s in segs))

    def test_live_segments_used_when_available(self):
        live = _StubLiveProvider(result=[_live_segment(price_eur=11.9, price_try=595.0)])
        provider = RailBusProvider(live_provider=live)
        segs = provider.get_schedules(*self.BTS_PRG, earliest_dep_time="05:00")
        self.assertEqual(live.calls, [("Bratislava hl.st.", "Praha hl.n.", "2026-10-28", "05:00")])
        self.assertEqual(len(segs), 1)
        self.assertFalse(segs[0].is_estimated)
        self.assertEqual(segs[0].price_try, 595.0)  # live fare kept, table price not applied
        self.assertFalse(segs[0].price_is_estimated)

    def test_table_price_filled_when_live_fare_missing(self):
        live = _StubLiveProvider(result=[_live_segment(price_eur=None, price_try=0.0)])
        provider = RailBusProvider(live_provider=live)
        segs = provider.get_schedules(*self.BTS_PRG)
        self.assertFalse(segs[0].is_estimated)  # times are live
        self.assertEqual(segs[0].price_try, 650.0)  # table price for Bratislava -> Praha
        self.assertTrue(segs[0].price_is_estimated)
        self.assertIsNone(segs[0].price_eur)

    def test_falls_back_to_table_when_live_returns_none(self):
        provider = RailBusProvider(live_provider=_StubLiveProvider(result=None))
        expected = RailBusProvider().get_schedules(*self.BTS_PRG)
        segs = provider.get_schedules(*self.BTS_PRG)
        self.assertEqual([s.to_dict() for s in segs], [s.to_dict() for s in expected])
        self.assertTrue(all(s.is_estimated for s in segs))

    def test_falls_back_to_table_when_live_returns_empty_list(self):
        provider = RailBusProvider(live_provider=_StubLiveProvider(result=[]))
        expected = RailBusProvider().get_schedules(*self.BTS_PRG)
        segs = provider.get_schedules(*self.BTS_PRG)
        self.assertEqual([s.to_dict() for s in segs], [s.to_dict() for s in expected])

    def test_falls_back_to_table_when_live_raises(self):
        provider = RailBusProvider(live_provider=_StubLiveProvider(error=RuntimeError("boom")))
        expected = RailBusProvider().get_schedules(*self.BTS_PRG)
        segs = provider.get_schedules(*self.BTS_PRG)
        self.assertEqual([s.to_dict() for s in segs], [s.to_dict() for s in expected])

    def test_live_not_consulted_for_unsupported_corridor(self):
        live = _StubLiveProvider(result=[_live_segment(price_eur=1.0, price_try=50.0)])
        provider = RailBusProvider(live_provider=live)
        segs = provider.get_schedules("Dresden Hbf", "Praha hl.n.", "2026-10-28")
        self.assertEqual(live.calls, [])
        self.assertTrue(segs)
        self.assertTrue(all(s.is_estimated for s in segs))

    def test_real_live_provider_with_fixture_and_mocked_urlopen(self):
        body = _load_rail_fixture("regiojet_bts_prg_2026-10-28.json")
        provider = RailBusProvider(live_provider=LiveRailProvider(eur_try_rate=50.0))
        with mock.patch(_URLOPEN, return_value=_FakeResponse(body)):
            segs = provider.get_schedules(*self.BTS_PRG, earliest_dep_time="12:00")
        self.assertEqual([s.departure_time for s in segs], ["12:26", "16:15"])
        self.assertFalse(segs[0].is_estimated)
        self.assertEqual(segs[0].price_eur, 16.9)
        self.assertEqual(segs[0].price_try, 845.0)

    def test_real_live_provider_network_error_falls_back_to_table(self):
        provider = RailBusProvider(live_provider=LiveRailProvider(eur_try_rate=50.0))
        expected = RailBusProvider().get_schedules(*self.BTS_PRG)
        with mock.patch(_URLOPEN, side_effect=urllib.error.URLError("offline")):
            segs = provider.get_schedules(*self.BTS_PRG)
        self.assertEqual([s.to_dict() for s in segs], [s.to_dict() for s in expected])


class RailBusStationNameTests(unittest.TestCase):
    def test_station_name_fields_default_to_none_and_serialize(self):
        seg = _live_segment(price_eur=11.9, price_try=595.0)
        self.assertIsNone(seg.origin_station_name)
        self.assertIsNone(seg.dest_station_name)
        d = seg.to_dict()
        self.assertIn("origin_station_name", d)
        self.assertIn("dest_station_name", d)
        self.assertIsNone(d["origin_station_name"])
        self.assertEqual(d["origin_station"], "Bratislava hl.st.")  # the existing field is unchanged

    def test_table_segments_carry_the_corridor_station_names(self):
        segs = RailBusProvider().get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-28")
        self.assertTrue(segs)
        for seg in segs:
            self.assertEqual((seg.origin_station_name, seg.dest_station_name), ("Wien Hbf", "Praha hl.n."))

    def test_live_segments_carry_the_station_names(self):
        provider = RailBusProvider(live_provider=LiveRailProvider(eur_try_rate=50.0))
        body = _load_rail_fixture("regiojet_bts_prg_2026-10-28.json")
        with mock.patch(_URLOPEN, return_value=_FakeResponse(body)):
            segs = provider.get_schedules("Bratislava hl.st.", "Praha hl.n.", "2026-10-28")
        self.assertTrue(segs)
        self.assertTrue(all(s.source == "live" for s in segs))
        self.assertTrue(all(s.origin_station_name == "Bratislava hl.st." for s in segs))
        self.assertTrue(all(s.dest_station_name == "Praha hl.n." for s in segs))


class RailBusProviderSourceAndDistanceTests(unittest.TestCase):
    def test_source_defaults_to_table_and_is_in_to_dict(self):
        self.assertEqual(ScheduledSegment.__dataclass_fields__["source"].default, "table")
        plain = RailBusProvider().get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-25")
        self.assertTrue(all(s.source == "table" for s in plain))
        self.assertTrue(all(s.to_dict()["source"] == "table" for s in plain))

    def test_live_schedule_segments_are_marked_live(self):
        live = _StubLiveProvider(result=[_live_segment(price_eur=11.9, price_try=595.0)])
        segs = RailBusProvider(live_provider=live).get_schedules(
            "Bratislava hl.st.", "Praha hl.n.", "2026-10-28", earliest_dep_time="05:00")
        self.assertEqual([s.source for s in segs], ["live"])

    def test_ground_distance_km_from_coordinates(self):
        provider = RailBusProvider(airport_db=AirportDB([]))
        milano = (45.4642, 9.1900)
        venezia = (45.4408, 12.3155)
        road = provider.ground_distance_km("Milano", "Venezia", from_coord=milano, to_coord=venezia)
        self.assertAlmostEqual(road, haversine_km(*milano, *venezia) * 1.25, places=6)
        self.assertAlmostEqual(road, 305.4, delta=1.0)

    def test_ground_distance_km_same_point_is_zero(self):
        provider = RailBusProvider(airport_db=AirportDB([]))
        self.assertAlmostEqual(
            provider.ground_distance_km("A", "B", from_coord=(48.2, 16.4), to_coord=(48.2, 16.4)), 0.0)

    def test_ground_distance_km_none_without_coordinates(self):
        provider = RailBusProvider(airport_db=AirportDB([]))
        self.assertIsNone(provider.ground_distance_km("Nowhere A", "Nowhere B"))
        # One known coordinate is not enough.
        self.assertIsNone(provider.ground_distance_km("Nowhere A", "Nowhere B", from_coord=(45.0, 9.0)))

    def test_ground_distance_km_uses_airport_db_by_name(self):
        provider = RailBusProvider()  # bundled airport data
        road = provider.ground_distance_km("Milano", "Venezia")
        self.assertIsNotNone(road)
        self.assertGreater(road, 200)
        self.assertLess(road, 400)


if __name__ == "__main__":
    unittest.main()
