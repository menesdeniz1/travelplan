import os
import unittest
import urllib.parse
from unittest import mock

from travel_engine.airports import AirportDB, haversine_km
from travel_engine.providers import live_rail_provider as lrp
from travel_engine.providers.live_rail_provider import LiveRailProvider
from travel_engine.providers.rail_bus_provider import RailBusProvider, ScheduledSegment

_FX_RATE = "travel_engine.fx.get_eur_try_rate"
_URLOPEN = "travel_engine.providers.live_rail_provider.urllib.request.urlopen"
_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "rail")
_DATE = "2026-10-28"
_CITY_BTS_PRG = ("10202001", "10202003")  # RegioJet city ids: Bratislava -> Prague

# City-centre coordinates used where the test must not depend on the airport data.
MILANO = (45.4642, 9.1900)
VENEZIA = (45.4408, 12.3155)
VIENNA = (48.2082, 16.3738)
MUNICH = (48.1351, 11.5820)
BERLIN = (52.5200, 13.4050)


class _StubLive:
    """Stands in for LiveRailProvider (can_resolve and search only) so these tests never touch the network."""

    def __init__(self, *, resolvable=True, resolve_error=None, search_result=None, search_error=None):
        self.resolvable = resolvable
        self.resolve_error = resolve_error
        self.search_result = search_result
        self.search_error = search_error
        self.resolve_calls = []
        self.search_calls = []

    def can_resolve(self, place):
        self.resolve_calls.append(place)
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.resolvable

    def search(self, from_place, to_place, date_str, earliest_dep_time=None,
               modes=("TRAIN", "BUS"), max_transfers=1):
        self.search_calls.append((from_place, to_place, date_str, earliest_dep_time))
        if self.search_error is not None:
            raise self.search_error
        # Like LiveRailProvider.search: segments carry the places as given.
        for seg in self.search_result or []:
            seg.origin_station = from_place
            seg.dest_station = to_place
        return self.search_result


def _live_seg(dep, arr, dur=180, price_eur=20.0, price_try=1080.0):
    # Live search results come back as fresh objects, so each test builds its own list.
    return ScheduledSegment(
        origin_station="ignored", dest_station="ignored",
        departure_time=dep, arrival_time=arr, duration_mins=dur,
        price_try=price_try, operator="RegioJet", mode="train", date=_DATE,
        is_estimated=False, price_eur=price_eur, price_is_estimated=False,
    )


def _hhmm(total_mins):
    total_mins %= 24 * 60
    return f"{total_mins // 60:02d}:{total_mins % 60:02d}"


class FindGroundTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch(_FX_RATE, return_value=(54.0, "test", False))
        self.fx = patcher.start()
        self.addCleanup(patcher.stop)
        self.airports = AirportDB.load_default()

    def _provider(self, live=None):
        return RailBusProvider(live_provider=live, airport_db=self.airports)

    # -- source order: live, table, estimate -----------------------------------

    def test_live_result_is_used_and_marked_live(self):
        live = _StubLive(search_result=[
            _live_seg("09:30", "12:10"),
            _live_seg("07:30", "10:05"),
        ])
        segs = self._provider(live).find_ground("Bratislava", "Prague", _DATE)

        self.assertEqual(live.resolve_calls, ["Bratislava", "Prague"])
        self.assertEqual(live.search_calls, [("Bratislava", "Prague", _DATE, None)])
        self.assertEqual([s.departure_time for s in segs], ["07:30", "09:30"])
        for seg in segs:
            self.assertEqual(seg.source, "live")
            self.assertFalse(seg.is_estimated)
            self.assertEqual(seg.origin_station, "Bratislava")
            self.assertEqual(seg.dest_station, "Prague")

    def test_live_is_asked_for_the_whole_day_and_filtered_by_earliest(self):
        live = _StubLive(search_result=[_live_seg("11:00", "13:30"), _live_seg("13:00", "15:30")])
        segs = self._provider(live).find_ground("Bratislava", "Prague", _DATE, earliest_dep_time="12:00")
        self.assertEqual(live.search_calls, [("Bratislava", "Prague", _DATE, None)])
        self.assertEqual([s.departure_time for s in segs], ["13:00"])

    def test_live_skipped_when_a_place_cannot_be_resolved(self):
        live = _StubLive(resolvable=False, search_result=[_live_seg("07:00", "09:00")])
        segs = self._provider(live).find_ground("Wien Hbf", "Praha hl.n.", _DATE)

        self.assertEqual(live.search_calls, [])
        self.assertTrue(segs)
        self.assertTrue(all(s.source == "table" for s in segs))

    def test_live_failures_fall_back_to_table(self):
        cases = {
            "search raises": _StubLive(search_error=RuntimeError("boom")),
            "search returns None": _StubLive(search_result=None),
            "search returns empty": _StubLive(search_result=[]),
            "can_resolve raises": _StubLive(resolve_error=RuntimeError("boom")),
        }
        expected = self._provider().find_ground("Wien Hbf", "Praha hl.n.", _DATE)
        for label, live in cases.items():
            with self.subTest(label):
                segs = self._provider(live).find_ground("Wien Hbf", "Praha hl.n.", _DATE)
                self.assertEqual([s.to_dict() for s in segs], [s.to_dict() for s in expected])
                self.assertTrue(all(s.source == "table" for s in segs))

    def test_table_used_when_no_live_provider(self):
        segs = self._provider().find_ground("Wien Hbf", "Praha hl.n.", _DATE, max_results=10)

        self.assertEqual([s.departure_time for s in segs],
                         ["06:00", "08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00"])
        for seg in segs:
            self.assertEqual(seg.source, "table")
            self.assertTrue(seg.is_estimated)
            self.assertTrue(seg.price_is_estimated)
            self.assertEqual(seg.price_try, 750.0)
            self.assertEqual(seg.origin_station, "Wien Hbf")
            self.assertEqual(seg.dest_station, "Praha hl.n.")
        self.fx.assert_not_called()  # table prices are TRY constants

    def test_cluster_city_names_resolve_to_table(self):
        segs = self._provider().find_ground("Vienna", "Prague", _DATE, max_results=10)

        self.assertTrue(segs)
        for seg in segs:
            self.assertEqual(seg.source, "table")
            self.assertEqual(seg.duration_mins, 255)
            self.assertEqual(seg.origin_station, "Vienna")
            self.assertEqual(seg.dest_station, "Prague")

    def test_budapest_to_prague_uses_kelenfoeld_corridor(self):
        segs = self._provider().find_ground("Budapest", "Prague", _DATE, max_results=20)

        self.assertTrue(segs)
        for seg in segs:
            self.assertEqual(seg.source, "table")
            self.assertEqual(seg.duration_mins, 420)  # Budapest Kelenföld -> Praha hl.n.
            self.assertEqual(seg.price_try, 1100.0)

    def test_airport_code_uses_registry_hub_links(self):
        segs = self._provider().find_ground("DRS", "Prague", _DATE)

        self.assertTrue(segs)
        for seg in segs:
            self.assertEqual(seg.source, "table")
            self.assertEqual(seg.duration_mins, 140)  # Dresden Hbf -> Praha hl.n.
            self.assertEqual(seg.origin_station, "DRS")

    def test_table_respects_earliest_departure(self):
        segs = self._provider().find_ground(
            "Wien Hbf", "Praha hl.n.", _DATE, earliest_dep_time="14:00", max_results=10,
        )
        self.assertEqual([s.departure_time for s in segs], ["14:00", "16:00", "18:00", "20:00"])

    def test_same_place_returns_empty(self):
        self.assertEqual(self._provider().find_ground("Praha hl.n.", "Praha hl.n.", _DATE), [])

    # -- distance estimate -------------------------------------------------------

    def test_unknown_pair_gets_distance_estimate(self):
        segs = self._provider().find_ground("Milano", "Venezia", _DATE, max_results=10)

        road_km = self._provider().ground_distance_km("Milano", "Venezia")
        self.assertIsNotNone(road_km)
        self.assertGreater(road_km, 250)
        self.assertLess(road_km, 350)

        self.assertEqual([s.departure_time for s in segs],
                         ["06:00", "08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00"])
        duration = round(road_km / 75 * 60 + 20)
        price_eur = round(max(8.0, road_km * 0.07), 2)
        for seg in segs:
            self.assertEqual(seg.source, "estimate")
            self.assertTrue(seg.is_estimated)
            self.assertTrue(seg.price_is_estimated)
            self.assertEqual(seg.mode, "bus")
            self.assertEqual(seg.operator, "Tahmini tren/otobüs")
            self.assertEqual(seg.duration_mins, duration)
            self.assertEqual(seg.price_eur, price_eur)
            self.assertEqual(seg.price_try, round(price_eur * 54.0, 2))
            self.assertEqual(seg.origin_station, "Milano")
            self.assertEqual(seg.dest_station, "Venezia")
            self.assertEqual(seg.date, _DATE)

    def test_estimate_numbers_for_milan_venice_coordinates(self):
        segs = self._provider().find_ground(
            "Milano", "Venezia", _DATE, from_coord=MILANO, to_coord=VENEZIA, max_results=1,
        )
        self.assertEqual(len(segs), 1)
        seg = segs[0]
        road_km = haversine_km(*MILANO, *VENEZIA) * 1.25
        self.assertAlmostEqual(road_km, 306, delta=2)
        self.assertAlmostEqual(seg.duration_mins, 265, delta=2)
        self.assertAlmostEqual(seg.price_eur, 21.4, delta=0.2)
        self.assertEqual(seg.departure_time, "06:00")

    def test_estimate_earliest_departure_filter(self):
        provider = self._provider()
        segs = provider.find_ground("Milano", "Venezia", _DATE, earliest_dep_time="13:00",
                                    from_coord=MILANO, to_coord=VENEZIA, max_results=10)
        self.assertEqual([s.departure_time for s in segs], ["14:00", "16:00", "18:00", "20:00"])

        none_left = provider.find_ground("Milano", "Venezia", _DATE, earliest_dep_time="20:01",
                                         from_coord=MILANO, to_coord=VENEZIA)
        self.assertEqual(none_left, [])

    def test_estimate_over_max_road_km_returns_empty(self):
        provider = self._provider()
        self.assertEqual(provider.find_ground("Milano", "Venezia", _DATE,
                                              from_coord=MILANO, to_coord=VENEZIA, max_road_km=300.0), [])
        # Lisbon to Helsinki is far beyond the default 700 km limit.
        self.assertEqual(provider.find_ground("Lisbon", "Helsinki", _DATE,
                                              from_coord=(38.7223, -9.1393), to_coord=(60.1699, 24.9384)), [])

    def test_estimate_below_minimum_distance_returns_empty(self):
        segs = self._provider().find_ground(
            "Milano", "Monza", _DATE, from_coord=(45.0, 9.0), to_coord=(45.01, 9.0),
        )
        self.assertEqual(segs, [])

    def test_estimate_arrival_wraps_past_midnight(self):
        segs = self._provider().find_ground(
            "Berlin", "Munich", _DATE, earliest_dep_time="20:00",
            from_coord=BERLIN, to_coord=MUNICH,
        )
        self.assertEqual(len(segs), 1)
        seg = segs[0]
        self.assertEqual(seg.departure_time, "20:00")
        self.assertEqual(seg.arrival_time, _hhmm(20 * 60 + seg.duration_mins))
        self.assertLess(seg.arrival_time, seg.departure_time)  # next day

    def test_estimate_uses_given_coordinates_for_unknown_names(self):
        provider = self._provider()
        segs = provider.find_ground("Nowhere A", "Nowhere B", _DATE,
                                    from_coord=MILANO, to_coord=VENEZIA, max_results=2)
        self.assertEqual(len(segs), 2)
        self.assertTrue(all(s.source == "estimate" for s in segs))
        # Without coordinates an unknown name has no estimate.
        self.assertEqual(provider.find_ground("Nowhere A", "Nowhere B", _DATE), [])

    # -- ordering and truncation -----------------------------------------------

    def test_live_results_sorted_and_capped(self):
        live = _StubLive(search_result=[
            _live_seg("12:00", "14:00"),
            _live_seg("09:00", "11:00"),
            _live_seg("15:00", "17:00"),
        ])
        segs = self._provider(live).find_ground("Bratislava", "Prague", _DATE, max_results=2)
        self.assertEqual([s.departure_time for s in segs], ["09:00", "12:00"])

    def test_max_results_applies_to_estimates(self):
        segs = self._provider().find_ground(
            "Milano", "Venezia", _DATE, from_coord=MILANO, to_coord=VENEZIA, max_results=3,
        )
        self.assertEqual([s.departure_time for s in segs], ["06:00", "08:00", "10:00"])

    def test_max_results_applies_to_table(self):
        segs = self._provider().find_ground("Wien Hbf", "Praha hl.n.", _DATE, max_results=3)
        self.assertEqual([s.departure_time for s in segs], ["06:00", "08:00", "10:00"])

    def test_zero_max_results_returns_empty(self):
        self.assertEqual(self._provider().find_ground("Wien Hbf", "Praha hl.n.", _DATE, max_results=0), [])


def _fixture(name):
    with open(os.path.join(_FIXTURES, name), "rb") as f:
        return f.read()


class _Body:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RegioJetFake:
    """Fake urlopen for RegioJet's locations list and city-level route search (no network)."""

    def __init__(self, searches):
        self.searches = searches
        self.locations = _fixture("regiojet_locations_subset.json")
        self.search_calls = 0

    def __call__(self, request, timeout=None):
        url = request.full_url
        if url.startswith(lrp.LOCATIONS_URL):
            return _Body(self.locations)
        self.search_calls += 1
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        key = (query["fromLocationId"][0], query["toLocationId"][0])
        return _Body(self.searches.get(key, b'{"routes": []}'))


class _GroundTestCase(unittest.TestCase):
    def setUp(self):
        lrp._reset_location_cache_for_tests()
        self.addCleanup(lrp._reset_location_cache_for_tests)
        patcher = mock.patch(_FX_RATE, return_value=(54.0, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.airports = AirportDB.load_default()

    def _provider(self, live=None, airports=None):
        return RailBusProvider(live_provider=live, airport_db=airports or self.airports)


class FindGroundStationNameTests(_GroundTestCase):
    def test_table_segments_carry_corridor_station_names(self):
        segs = self._provider().find_ground("Vienna", "Prague", _DATE, max_results=10)
        self.assertTrue(segs)
        for seg in segs:
            self.assertEqual(seg.origin_station, "Vienna")  # as given
            self.assertEqual(seg.dest_station, "Prague")
            self.assertEqual(seg.origin_station_name, "Wien Hbf")
            self.assertEqual(seg.dest_station_name, "Praha hl.n.")
        # Budapest-Prague trains leave Kelenföld, not Keleti.
        first = self._provider().find_ground("Budapest", "Prague", _DATE, max_results=1)[0]
        self.assertEqual(first.origin_station_name, "Budapest Kelenföld")

    def test_live_segments_carry_regiojet_station_names(self):
        live = LiveRailProvider(eur_try_rate=50.0)
        router = _RegioJetFake({_CITY_BTS_PRG: _fixture("regiojet_city_bts_prg_2026-10-28.json")})
        with mock.patch(_URLOPEN, side_effect=router):
            segs = self._provider(live).find_ground("Bratislava", "Prague", _DATE, max_results=50)

        self.assertTrue(segs)
        self.assertTrue(all(s.source == "live" for s in segs))
        by_time = {s.departure_time: s for s in segs}
        train = by_time["05:17"]
        self.assertEqual(train.origin_station, "Bratislava")  # as given
        self.assertEqual(train.origin_station_name, "Bratislava - MS")
        self.assertEqual(train.dest_station_name, "Prague - MS")
        self.assertEqual(by_time["01:45"].origin_station_name, "Bratislava - Nivy bus station")
        self.assertTrue(all(s.origin_station_name and s.dest_station_name for s in segs))

    def test_estimate_segments_have_no_station_names(self):
        segs = self._provider().find_ground(
            "Milano", "Venezia", _DATE, from_coord=MILANO, to_coord=VENEZIA, max_results=2)
        self.assertEqual(len(segs), 2)
        for seg in segs:
            self.assertEqual(seg.source, "estimate")
            self.assertIsNone(seg.origin_station_name)
            self.assertIsNone(seg.dest_station_name)
            self.assertIsNone(seg.to_dict()["origin_station_name"])
            self.assertEqual(seg.origin_station, "Milano")


class FindGroundNextTests(_GroundTestCase):
    def test_same_day_result_is_used_without_rollover(self):
        date_used, segs = self._provider().find_ground_next(
            "Wien Hbf", "Praha hl.n.", _DATE, "14:00", max_results=10)
        self.assertEqual(date_used, _DATE)
        self.assertEqual([s.departure_time for s in segs], ["14:00", "16:00", "18:00", "20:00"])
        self.assertTrue(all(s.date == _DATE for s in segs))

    def test_rolls_over_to_next_day_from_midnight_when_the_day_is_empty(self):
        # The table's last departure is 20:00, so nothing is left after 20:01 on the given day.
        self.assertEqual(self._provider().find_ground("Wien Hbf", "Praha hl.n.", _DATE, "20:01"), [])
        date_used, segs = self._provider().find_ground_next(
            "Wien Hbf", "Praha hl.n.", _DATE, "20:01", max_results=10)
        self.assertEqual(date_used, "2026-10-29")
        self.assertEqual([s.departure_time for s in segs],
                         ["06:00", "08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00"])
        self.assertTrue(all(s.date == "2026-10-29" for s in segs))

    def test_rollover_crosses_the_year_end(self):
        date_used, segs = self._provider().find_ground_next(
            "Wien Hbf", "Praha hl.n.", "2026-12-31", "23:00")
        self.assertEqual(date_used, "2027-01-01")
        self.assertEqual(segs[0].departure_time, "06:00")
        self.assertEqual(segs[0].date, "2027-01-01")

    def test_rollover_for_distance_estimates(self):
        date_used, segs = self._provider().find_ground_next(
            "Milano", "Venezia", _DATE, "20:01", from_coord=MILANO, to_coord=VENEZIA)
        self.assertEqual(date_used, "2026-10-29")
        self.assertEqual(segs[0].departure_time, "06:00")
        self.assertTrue(all(s.source == "estimate" and s.date == "2026-10-29" for s in segs))

    def test_no_connection_on_either_day_keeps_the_given_date(self):
        result = self._provider().find_ground_next(
            "Milano", "Monza", _DATE, from_coord=(45.0, 9.0), to_coord=(45.01, 9.0))
        self.assertEqual(result, (_DATE, []))

    def test_malformed_date_gives_no_segments(self):
        self.assertEqual(self._provider().find_ground_next("Wien Hbf", "Praha hl.n.", "28.10.2026"),
                         ("28.10.2026", []))


class CountryTokenTests(_GroundTestCase):
    def test_country_token_gives_no_options_and_no_live_request(self):
        live = _StubLive(search_result=[_live_seg("07:00", "10:00")])
        provider = self._provider(live)
        for origin, dest in [("Greece", "Athens"), ("Athens", "Greece"), ("Italy", "Venezia"),
                             ("Milano", "Germany"), ("Yunanistan", "Thessaloniki")]:
            with self.subTest(origin=origin, dest=dest):
                self.assertEqual(provider.find_ground(origin, dest, _DATE), [])
                self.assertEqual(provider.find_ground_next(origin, dest, _DATE), (_DATE, []))
        self.assertEqual(live.resolve_calls, [])
        self.assertEqual(live.search_calls, [])

    def test_country_token_has_no_distance(self):
        provider = self._provider()
        self.assertIsNone(provider.ground_distance_km("Italy", "Venezia"))
        self.assertIsNone(provider.ground_distance_km("Greece", "Athens"))

    def test_coordinate_given_for_a_country_side_is_used(self):
        # A coordinate is a real point, so the country check does not apply to that side.
        self.assertIsNotNone(self._provider().ground_distance_km("Italy", "Venezia", from_coord=MILANO))
        segs = self._provider().find_ground(
            "Italy", "Venezia", _DATE, from_coord=MILANO, to_coord=VENEZIA, max_results=1)
        self.assertEqual([s.source for s in segs], ["estimate"])
        self.assertEqual(segs[0].origin_station, "Italy")

    def test_heuristic_separates_countries_from_cities_and_airports(self):
        provider = self._provider()
        for token in ["Milano", "Venezia", "Wien", "Budapest", "Athens", "ATH", "MUC"]:
            self.assertFalse(provider._is_country_token(token), token)
        for token in ["Italy", "Greece", "Germany", "Almanya", "GR", "IT"]:
            self.assertTrue(provider._is_country_token(token), token)

    def test_registry_countries_work_without_airport_data(self):
        provider = self._provider(airports=AirportDB([]))
        self.assertEqual(provider.find_ground("Greece", "Athens", _DATE), [])
        self.assertIsNone(provider.ground_distance_km("Greece", "Athens"))


if __name__ == "__main__":
    unittest.main()
