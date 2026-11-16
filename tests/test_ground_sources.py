"""find_ground with several live ground sources: RegioJet (LiveRailProvider) and FlixBus.

No network. The live sources are stubs, except the FlixBus end-to-end test, which serves the
autocomplete and search responses from tests/fixtures/flixbus through a fake urlopen.
"""

import copy
import json
import os
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from travel_engine.airports import AirportDB
from travel_engine.providers import flixbus_provider as fp
from travel_engine.providers.flixbus_provider import FlixBusProvider
from travel_engine.providers.rail_bus_provider import (
    MAPS_BUDGET_ENV, MapsBudget, RailBusProvider, ScheduledSegment, maps_budget_from_env,
)

_DATE = "2026-11-05"
_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "flixbus")
MILANO = (45.4642, 9.1900)
VENEZIA = (45.4408, 12.3155)

ANADOLU = "523ae3fc-b96a-4b21-ad03-fe5bc6ce818a"
AVRUPA = "99c50ec5-3ecb-11ea-8017-02437075395e"
SOFIA = "40e09335-8646-11e6-9066-549f350fcb0c"


def _seg(source, dep, arr, *, mode="train", price_try=1000.0, estimated=False, operator=None):
    """A segment as a source returns it: ``source`` is set again by find_ground anyway."""
    return ScheduledSegment(
        origin_station="as given", dest_station="as given",
        departure_time=dep, arrival_time=arr, duration_mins=120,
        price_try=price_try, operator=operator or source, mode=mode, date=_DATE,
        is_estimated=False,
        price_eur=None if estimated else round(price_try / 50, 2),
        price_is_estimated=estimated, source=source,
    )


class _StubLive:
    """RegioJet stand-in: can_resolve and search only (no health_status)."""

    def __init__(self, *, resolvable=True, resolve_error=None, result=None, error=None):
        self.resolvable = resolvable
        self.resolve_error = resolve_error
        self.result = result
        self.error = error
        self.search_calls = []

    def can_resolve(self, place):
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.resolvable

    def search(self, from_place, to_place, date_str, earliest_dep_time=None,
               modes=("TRAIN", "BUS"), max_transfers=1):
        self.search_calls.append((from_place, to_place, date_str, earliest_dep_time))
        if self.error is not None:
            raise self.error
        return None if self.result is None else [copy.copy(s) for s in self.result]


class _HealthyLive(_StubLive):
    def __init__(self, health, **kwargs):
        super().__init__(**kwargs)
        self.health = health

    def health_status(self):
        if isinstance(self.health, Exception):
            raise self.health
        return self.health


class _StubBus:
    """FlixBus stand-in: can_resolve, search and health_status like FlixBusProvider."""

    def __init__(self, *, resolvable=True, resolve_error=None, result=None, error=None, health=None):
        self.resolvable = resolvable
        self.resolve_error = resolve_error
        self.result = result
        self.error = error
        self.health = health if health is not None else {"source": "flixbus", "status": "ok"}
        self.search_calls = []

    def can_resolve(self, place):
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.resolvable

    def search(self, from_place, to_place, date_str, earliest_dep_time=None,
               max_results=6, max_transfers=1):
        self.search_calls.append((from_place, to_place, date_str, earliest_dep_time, max_results))
        if self.error is not None:
            raise self.error
        return None if self.result is None else [copy.copy(s) for s in self.result]

    def health_status(self):
        if isinstance(self.health, Exception):
            raise self.health
        return self.health


class _StubTimetable:
    """Transitous stand-in: can_resolve, search (with coordinates) and health_status."""

    def __init__(self, *, resolvable=True, resolve_error=None, result=None, error=None, health=None):
        self.resolvable = resolvable
        self.resolve_error = resolve_error
        self.result = result
        self.error = error
        self.health = health if health is not None else {"source": "transitous", "enabled": True, "status": "ok"}
        self.resolve_calls = []
        self.search_calls = []

    def can_resolve(self, place):
        self.resolve_calls.append(place)
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.resolvable

    def search(self, from_place, to_place, date_str, earliest_dep_time=None,
               from_coord=None, to_coord=None, max_results=6, max_transfers=2):
        self.search_calls.append((from_place, to_place, date_str, earliest_dep_time,
                                  from_coord, to_coord, max_results))
        if self.error is not None:
            raise self.error
        return None if self.result is None else [copy.copy(s) for s in self.result]

    def health_status(self):
        return self.health


class _GroundCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("travel_engine.fx.get_eur_try_rate", return_value=(50.0, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.airports = AirportDB.load_default()

    def _provider(self, live=None, bus=None, timetable=None):
        return RailBusProvider(
            live_provider=live, bus_provider=bus, airport_db=self.airports, timetable_provider=timetable,
        )

    @staticmethod
    def _times(segs):
        return [s.departure_time for s in segs]


class MergeRuleTests(_GroundCase):
    def _merge_pair(self, live_seg, bus_seg, max_results=6):
        live = _StubLive(result=[live_seg])
        bus = _StubBus(result=[bus_seg])
        return self._provider(live, bus).find_ground("Vienna", "Budapest", _DATE, max_results=max_results)

    def test_same_service_keeps_the_known_fare_over_an_estimate(self):
        segs = self._merge_pair(
            _seg("live", "09:00", "11:00", price_try=800, estimated=True),
            _seg("flixbus", "09:03", "11:08", price_try=1000),
        )
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].source, "flixbus")
        self.assertEqual(segs[0].price_try, 1000)

    def test_same_service_with_two_known_fares_keeps_the_cheaper(self):
        segs = self._merge_pair(
            _seg("live", "09:00", "11:00", price_try=1080),
            _seg("flixbus", "09:00", "11:00", price_try=900),
        )
        self.assertEqual([(s.source, s.price_try) for s in segs], [("flixbus", 900)])

    def test_same_service_keeps_the_live_result_when_it_is_cheaper(self):
        segs = self._merge_pair(
            _seg("live", "09:00", "11:00", price_try=700),
            _seg("flixbus", "09:00", "11:00", price_try=900),
        )
        self.assertEqual([(s.source, s.price_try) for s in segs], [("live", 700)])

    def test_departure_five_minutes_apart_is_the_same_service(self):
        segs = self._merge_pair(_seg("live", "09:00", "11:00"), _seg("flixbus", "09:05", "11:00"))
        self.assertEqual(len(segs), 1)

    def test_departure_six_minutes_apart_is_a_different_service(self):
        segs = self._merge_pair(_seg("live", "09:00", "11:00"), _seg("flixbus", "09:06", "11:00"))
        self.assertEqual(len(segs), 2)

    def test_arrival_ten_minutes_apart_is_the_same_service(self):
        segs = self._merge_pair(_seg("live", "09:00", "11:00"), _seg("flixbus", "09:00", "11:10"))
        self.assertEqual(len(segs), 1)

    def test_arrival_over_ten_minutes_apart_is_a_different_service(self):
        segs = self._merge_pair(_seg("live", "09:00", "11:00"), _seg("flixbus", "09:00", "11:11"))
        self.assertEqual(len(segs), 2)

    def test_different_mode_is_never_a_duplicate(self):
        segs = self._merge_pair(
            _seg("live", "09:00", "11:00", mode="train"),
            _seg("flixbus", "09:00", "11:00", mode="bus"),
        )
        self.assertEqual(len(segs), 2)

    def test_duplicate_across_midnight(self):
        segs = self._merge_pair(_seg("live", "23:58", "01:30"), _seg("flixbus", "00:02", "01:32"))
        self.assertEqual(len(segs), 1)

    def test_duplicates_within_one_source_are_kept(self):
        live = _StubLive(result=[_seg("live", "09:00", "11:00"), _seg("live", "09:00", "11:00")])
        segs = self._provider(live, _StubBus(result=None)).find_ground("Vienna", "Budapest", _DATE)
        self.assertEqual(len(segs), 2)

    def test_merged_results_are_sorted_by_departure(self):
        live = _StubLive(result=[_seg("live", "10:00", "12:00"), _seg("live", "12:00", "14:00")])
        bus = _StubBus(result=[_seg("flixbus", "08:00", "10:00", mode="bus")])
        segs = self._provider(live, bus).find_ground("Vienna", "Budapest", _DATE, max_results=10)
        self.assertEqual(self._times(segs), ["08:00", "10:00", "12:00"])
        self.assertEqual([s.source for s in segs], ["flixbus", "live", "live"])


class DiversityTests(_GroundCase):
    def _lists(self):
        live = _StubLive(result=[
            _seg("live", "06:00", "08:00"), _seg("live", "07:00", "09:00"),
            _seg("live", "08:00", "10:00"), _seg("live", "09:00", "11:00"),
        ])
        bus = _StubBus(result=[_seg("flixbus", "10:00", "12:00", mode="bus")])
        return live, bus

    def test_each_source_keeps_a_slot_when_max_results_allows(self):
        live, bus = self._lists()
        segs = self._provider(live, bus).find_ground("Vienna", "Budapest", _DATE, max_results=2)
        self.assertEqual([(s.departure_time, s.source) for s in segs], [("06:00", "live"), ("10:00", "flixbus")])

    def test_late_source_replaces_the_latest_segment_of_the_crowded_source(self):
        live, bus = self._lists()
        segs = self._provider(live, bus).find_ground("Vienna", "Budapest", _DATE, max_results=3)
        self.assertEqual(self._times(segs), ["06:00", "07:00", "10:00"])
        self.assertEqual({s.source for s in segs}, {"live", "flixbus"})

    def test_max_results_one_is_the_plain_earliest(self):
        live, bus = self._lists()
        segs = self._provider(live, bus).find_ground("Vienna", "Budapest", _DATE, max_results=1)
        self.assertEqual(self._times(segs), ["06:00"])

    def test_single_source_is_not_diversified(self):
        live, _ = self._lists()
        segs = self._provider(live, _StubBus(result=[])).find_ground("Vienna", "Budapest", _DATE, max_results=2)
        self.assertEqual(self._times(segs), ["06:00", "07:00"])


class GatingTests(_GroundCase):
    def test_bus_skipped_when_it_cannot_resolve_a_place(self):
        live = _StubLive(result=[_seg("live", "09:00", "11:00")])
        bus = _StubBus(resolvable=False, result=[_seg("flixbus", "08:00", "10:00", mode="bus")])
        segs = self._provider(live, bus).find_ground("Vienna", "Budapest", _DATE)
        self.assertEqual(bus.search_calls, [])
        self.assertEqual([s.source for s in segs], ["live"])

    def test_live_skipped_when_it_cannot_resolve_a_place(self):
        live = _StubLive(resolvable=False, result=[_seg("live", "09:00", "11:00")])
        bus = _StubBus(result=[_seg("flixbus", "08:00", "10:00", mode="bus")])
        segs = self._provider(live, bus).find_ground("Vienna", "Budapest", _DATE)
        self.assertEqual(live.search_calls, [])
        self.assertEqual([s.source for s in segs], ["flixbus"])

    def test_resolve_error_skips_only_that_source(self):
        live = _StubLive(resolve_error=RuntimeError("boom"), result=[_seg("live", "09:00", "11:00")])
        bus = _StubBus(result=[_seg("flixbus", "08:00", "10:00", mode="bus")])
        segs = self._provider(live, bus).find_ground("Vienna", "Budapest", _DATE)
        self.assertEqual(live.search_calls, [])
        self.assertEqual([s.source for s in segs], ["flixbus"])

    def test_bus_gets_the_earliest_time_and_the_result_cap(self):
        bus = _StubBus(result=[])
        self._provider(_StubLive(result=[]), bus).find_ground(
            "Vienna", "Budapest", _DATE, earliest_dep_time="12:00", max_results=4,
        )
        # The bus is asked for the whole day (no earliest time, no cap of its own); the cap of 4 applies after
        # the earliest filter, in the caller's order.
        self.assertEqual([call[:4] for call in bus.search_calls], [("Vienna", "Budapest", _DATE, None)])


class IsolationTests(_GroundCase):
    def test_live_error_keeps_the_bus_results(self):
        live = _StubLive(error=RuntimeError("boom"))
        bus = _StubBus(result=[_seg("flixbus", "08:00", "10:00", mode="bus")])
        segs = self._provider(live, bus).find_ground("Vienna", "Budapest", _DATE)
        self.assertEqual([(s.source, s.departure_time) for s in segs], [("flixbus", "08:00")])

    def test_bus_none_keeps_the_live_results(self):
        live = _StubLive(result=[_seg("live", "09:00", "11:00")])
        segs = self._provider(live, _StubBus(result=None)).find_ground("Vienna", "Budapest", _DATE)
        self.assertEqual([s.source for s in segs], ["live"])

    def test_bus_error_keeps_the_live_results(self):
        live = _StubLive(result=[_seg("live", "09:00", "11:00")])
        segs = self._provider(live, _StubBus(error=RuntimeError("boom"))).find_ground(
            "Vienna", "Budapest", _DATE,
        )
        self.assertEqual([s.source for s in segs], ["live"])

    def test_both_sources_empty_fall_back_to_the_table(self):
        provider = self._provider(_StubLive(result=[]), _StubBus(result=[]))
        segs = provider.find_ground("Wien Hbf", "Praha hl.n.", _DATE)
        self.assertTrue(segs)
        self.assertEqual({s.source for s in segs}, {"table"})

    def test_both_sources_unresolvable_fall_back_to_the_table(self):
        provider = self._provider(_StubLive(resolvable=False), _StubBus(resolvable=False))
        segs = provider.find_ground("Wien Hbf", "Praha hl.n.", _DATE)
        self.assertEqual({s.source for s in segs}, {"table"})

    def test_both_sources_failing_fall_back_to_the_estimate(self):
        provider = self._provider(_StubLive(error=RuntimeError("x")), _StubBus(error=RuntimeError("y")))
        segs = provider.find_ground("Milano", "Venezia", _DATE, from_coord=MILANO, to_coord=VENEZIA)
        self.assertTrue(segs)
        self.assertEqual({s.source for s in segs}, {"estimate"})


class SourcesHealthTests(_GroundCase):
    def test_reports_both_sources_through_their_own_health_status(self):
        live = _HealthyLive({"source": "regiojet", "status": "ok"})
        bus = _StubBus(health={"source": "flixbus", "status": "blocked"})
        health = self._provider(live, bus).sources_health()
        self.assertEqual(health, {
            "regiojet": {"source": "regiojet", "status": "ok"},
            "flixbus": {"source": "flixbus", "status": "blocked"},
            "trenitalia": {"available": False},
            "transitous": {"available": False},
            "google_maps": {"available": False},
            "obilet": {"available": False},
        })

    def test_unconfigured_sources_are_unavailable(self):
        self.assertEqual(self._provider().sources_health(), {
            "regiojet": {"available": False},
            "flixbus": {"available": False},
            "trenitalia": {"available": False},
            "transitous": {"available": False},
            "google_maps": {"available": False},
            "obilet": {"available": False},
        })

    def test_source_without_health_status_is_available(self):
        health = self._provider(_StubLive(), None).sources_health()
        self.assertEqual(health, {
            "regiojet": {"available": True},
            "flixbus": {"available": False},
            "trenitalia": {"available": False},
            "transitous": {"available": False},
            "google_maps": {"available": False},
            "obilet": {"available": False},
        })

    def test_transitous_health_is_its_own_report(self):
        timetable = _StubTimetable(health={"source": "transitous", "enabled": False, "status": "disabled"})
        health = self._provider(None, None, timetable).sources_health()
        self.assertEqual(health["transitous"], {"source": "transitous", "enabled": False, "status": "disabled"})
        self.assertEqual(health["regiojet"], {"available": False})

    def test_failing_health_check_does_not_raise(self):
        health = self._provider(None, _StubBus(health=RuntimeError("boom"))).sources_health()
        self.assertEqual(health["flixbus"], {"available": True, "error": "boom"})
        self.assertEqual(health["regiojet"], {"available": False})


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _city(name, cid):
    return {"name": name, "id": cid, "country": "xx", "is_flixbus_city": True}


class FlixBusIstanbulEndToEndTests(_GroundCase):
    """find_ground("Istanbul", "Sofia") with a real FlixBusProvider and a fake HTTP layer."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(_FIXTURES, "search_istanbul_sofia_2026-11-05.json"), "rb") as f:
            self.avrupa_body = f.read()
        self.autocomplete = {
            "Istanbul": [_city("İstanbul Anadolu", ANADOLU), _city("İstanbul Avrupa", AVRUPA)],
            "Sofia": [_city("Sofia", SOFIA)],
        }
        self.calls = []
        patcher = mock.patch.object(fp.urllib.request, "urlopen", side_effect=self._urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _urlopen(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if "/autocomplete/cities" in url:
            return _FakeResponse(json.dumps(self.autocomplete.get(query["q"][0], [])).encode("utf-8"))
        if "/search/service/v4/search" in url:
            if query["from_city_id"][0] == AVRUPA:
                return _FakeResponse(self.avrupa_body)
            return _FakeResponse(json.dumps({"trips": []}).encode("utf-8"))
        raise urllib.error.URLError(f"unexpected URL {url}")

    def test_istanbul_to_sofia_merges_both_istanbul_cities(self):
        bus = FlixBusProvider(min_interval_secs=0)
        segs = self._provider(None, bus).find_ground("Istanbul", "Sofia", _DATE, max_results=10)

        self.assertEqual(self._times(segs), ["11:00", "23:00"])
        self.assertEqual({s.source for s in segs}, {"flixbus"})
        self.assertEqual({s.origin_station for s in segs}, {"Istanbul"})
        searches = [u for u in self.calls if "/search/service/v4/search" in u]
        self.assertEqual(len(searches), 2)  # Anadolu -> Sofia and Avrupa -> Sofia


class FindGroundSourceUseTests(_GroundCase):
    """The live sources are tried before the table, and the table only when they are empty."""

    def test_live_result_hides_the_table_for_that_pair(self):
        live = _StubLive(result=[_seg("live", "09:30", "11:30")])
        segs = self._provider(live, _StubBus(result=[])).find_ground("Wien Hbf", "Praha hl.n.", _DATE)
        self.assertEqual([(s.source, s.departure_time) for s in segs], [("live", "09:30")])

    def test_bus_result_hides_the_table_for_that_pair(self):
        bus = _StubBus(result=[_seg("flixbus", "07:00", "11:00", mode="bus")])
        segs = self._provider(_StubLive(result=[]), bus).find_ground("Wien Hbf", "Praha hl.n.", _DATE)
        self.assertEqual([s.source for s in segs], ["flixbus"])

    def test_no_bus_provider_keeps_the_previous_behaviour(self):
        live = _StubLive(result=[_seg("live", "09:30", "11:30")])
        segs = RailBusProvider(live_provider=live, airport_db=self.airports).find_ground(
            "Wien Hbf", "Praha hl.n.", _DATE,
        )
        self.assertEqual([s.source for s in segs], ["live"])


class TransitMergeTests(_GroundCase):
    """Transitous joins the live merge after RegioJet and FlixBus; priced live fares win duplicates."""

    def _transit_seg(self, dep, arr, *, price_try=800.0, mode="train"):
        return _seg("transitous", dep, arr, mode=mode, price_try=price_try, estimated=True)

    def test_priced_regiojet_fare_beats_a_transitous_estimate(self):
        live = _StubLive(result=[_seg("live", "09:00", "11:00", price_try=1000)])
        timetable = _StubTimetable(result=[self._transit_seg("09:00", "11:00", price_try=600)])
        segs = self._provider(live, _StubBus(result=[]), timetable).find_ground("Vienna", "Budapest", _DATE)
        self.assertEqual([(s.source, s.price_try) for s in segs], [("live", 1000)])

    def test_priced_flixbus_fare_beats_a_cheaper_transitous_estimate(self):
        bus = _StubBus(result=[_seg("flixbus", "09:02", "11:02", mode="bus", price_try=900)])
        timetable = _StubTimetable(result=[self._transit_seg("09:00", "11:00", mode="bus", price_try=300)])
        segs = self._provider(_StubLive(result=[]), bus, timetable).find_ground("Vienna", "Budapest", _DATE)
        self.assertEqual([(s.source, s.price_try) for s in segs], [("flixbus", 900)])

    def test_transitous_only_service_is_kept_and_hides_the_table(self):
        timetable = _StubTimetable(result=[self._transit_seg("07:10", "11:20", price_try=700)])
        segs = self._provider(_StubLive(result=[]), _StubBus(result=[]), timetable).find_ground(
            "Wien Hbf", "Praha hl.n.", _DATE,
        )
        self.assertEqual([(s.source, s.departure_time) for s in segs], [("transitous", "07:10")])

    def test_transitous_segments_merge_by_departure_with_other_sources(self):
        live = _StubLive(result=[_seg("live", "10:00", "12:00")])
        timetable = _StubTimetable(result=[self._transit_seg("08:00", "10:00"), self._transit_seg("12:30", "14:30")])
        segs = self._provider(live, _StubBus(result=[]), timetable).find_ground(
            "Vienna", "Budapest", _DATE, max_results=10,
        )
        self.assertEqual([(s.departure_time, s.source) for s in segs],
                         [("08:00", "transitous"), ("10:00", "live"), ("12:30", "transitous")])

    def test_each_live_source_keeps_a_slot_when_max_results_allows(self):
        live = _StubLive(result=[_seg("live", "06:00", "08:00"), _seg("live", "07:00", "09:00")])
        timetable = _StubTimetable(result=[self._transit_seg("11:00", "13:00")])
        segs = self._provider(live, _StubBus(result=[]), timetable).find_ground(
            "Vienna", "Budapest", _DATE, max_results=2,
        )
        self.assertEqual({s.source for s in segs}, {"live", "transitous"})

    def test_transitous_gets_coordinates_the_cap_and_the_earliest_time(self):
        timetable = _StubTimetable(result=[])
        self._provider(_StubLive(result=[]), _StubBus(result=[]), timetable).find_ground(
            "Milano", "Venezia", _DATE, earliest_dep_time="08:00", from_coord=MILANO, to_coord=VENEZIA,
            max_results=4,
        )
        # Whole day from the first page (no earliest time, coordinates given); the empty answer ends the day.
        self.assertEqual(timetable.search_calls[0][:6], ("Milano", "Venezia", _DATE, None, MILANO, VENEZIA))
        self.assertEqual(len(timetable.search_calls), 1)

    def test_given_coordinates_skip_the_name_lookup(self):
        timetable = _StubTimetable(result=[])
        self._provider(_StubLive(result=[]), _StubBus(result=[]), timetable).find_ground(
            "Milano", "Venezia", _DATE, from_coord=MILANO, to_coord=VENEZIA,
        )
        self.assertEqual(timetable.resolve_calls, [])
        self.assertEqual(len(timetable.search_calls), 1)

    def test_place_without_coordinates_must_resolve_by_name(self):
        timetable = _StubTimetable(resolvable=False, result=[self._transit_seg("09:00", "11:00")])
        segs = self._provider(_StubLive(result=[]), _StubBus(result=[]), timetable).find_ground(
            "Wien Hbf", "Praha hl.n.", _DATE,
        )
        self.assertEqual(timetable.resolve_calls, ["Wien Hbf"])
        self.assertEqual(timetable.search_calls, [])
        self.assertEqual({s.source for s in segs}, {"table"})

    def test_transitous_error_keeps_the_other_sources(self):
        live = _StubLive(result=[_seg("live", "09:00", "11:00")])
        segs = self._provider(live, _StubBus(result=[]), _StubTimetable(error=RuntimeError("boom"))).find_ground(
            "Vienna", "Budapest", _DATE,
        )
        self.assertEqual([s.source for s in segs], ["live"])

    def test_transitous_none_or_empty_falls_back_to_the_table(self):
        for result in (None, []):
            with self.subTest(result=result):
                segs = self._provider(_StubLive(result=[]), _StubBus(result=[]),
                                      _StubTimetable(result=result)).find_ground("Wien Hbf", "Praha hl.n.", _DATE)
                self.assertEqual({s.source for s in segs}, {"table"})

    def test_failing_transitous_alone_leaves_the_estimate(self):
        timetable = _StubTimetable(error=RuntimeError("x"))
        segs = self._provider(None, None, timetable).find_ground(
            "Milano", "Venezia", _DATE, from_coord=MILANO, to_coord=VENEZIA,
        )
        self.assertEqual({s.source for s in segs}, {"estimate"})


class DisabledTransitousTests(_GroundCase):
    """The real provider, switched off: find_ground never touches the network."""

    def test_disabled_provider_is_skipped_without_a_request(self):
        from travel_engine.providers.transitous_provider import TransitousProvider

        with mock.patch.dict(os.environ):
            os.environ.pop("TRAVELPLAN_TRANSITOUS", None)
            timetable = TransitousProvider(min_interval_secs=0, airport_db=self.airports, anchor_cache_path=None)
            with mock.patch.object(fp.urllib.request, "urlopen", side_effect=AssertionError("network")) as urlopen:
                segs = self._provider(None, None, timetable).find_ground(
                    "Milano", "Venezia", _DATE, from_coord=MILANO, to_coord=VENEZIA,
                )
                health = self._provider(None, None, timetable).sources_health()["transitous"]
        urlopen.assert_not_called()
        self.assertEqual({s.source for s in segs}, {"estimate"})
        self.assertFalse(health["enabled"])


class EnabledTransitousEndToEndTests(_GroundCase):
    """find_ground with an enabled real TransitousProvider and the fixture plan response (no network)."""

    def test_fixture_journeys_come_through_find_ground(self):
        from travel_engine.providers import transitous_provider as tp
        from travel_engine.providers.transitous_provider import TransitousProvider

        with open(os.path.join(os.path.dirname(__file__), "fixtures", "transitous",
                               "plan_milano_venezia_2026-11-05.json"), "rb") as f:
            plan = f.read()
        with mock.patch.dict(os.environ, {"TRAVELPLAN_TRANSITOUS": "1"}):
            timetable = TransitousProvider(min_interval_secs=0, airport_db=self.airports, anchor_cache_path=None)
            with mock.patch.object(tp.urllib.request, "urlopen", return_value=_FakeResponse(plan)) as urlopen:
                segs = self._provider(None, None, timetable).find_ground(
                    "Milano", "Venezia", "2026-11-05", from_coord=MILANO, to_coord=VENEZIA, max_results=10,
                )
        # One request for the first page, one from the last departure on: the fixture repeats itself, so the day is complete.
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual({s.source for s in segs}, {"transitous"})
        self.assertEqual([s.departure_time for s in segs],
                         ["07:14", "07:44", "08:14", "08:44", "08:54", "09:44"])
        self.assertTrue(all(s.price_try is None and not s.price_is_estimated and not s.is_estimated for s in segs))


class TransitousAllowEstimateFalseTests(_GroundCase):
    """find_ground(allow_estimate=False) keeps Transitous segments because price_is_estimated is False (DOG-3)."""

    def test_transitous_segments_are_kept_under_allow_estimate_false(self):
        from travel_engine.providers import transitous_provider as tp
        from travel_engine.providers.transitous_provider import TransitousProvider

        with open(os.path.join(os.path.dirname(__file__), "fixtures", "transitous",
                               "plan_milano_venezia_2026-11-05.json"), "rb") as f:
            plan = f.read()
        with mock.patch.dict(os.environ, {"TRAVELPLAN_TRANSITOUS": "1"}):
            timetable = TransitousProvider(min_interval_secs=0, airport_db=self.airports, anchor_cache_path=None)
            with mock.patch.object(tp.urllib.request, "urlopen", return_value=_FakeResponse(plan)):
                segs = self._provider(None, None, timetable).find_ground(
                    "Milano", "Venezia", "2026-11-05", from_coord=MILANO, to_coord=VENEZIA,
                    max_results=10, allow_estimate=False,
                )
        self.assertTrue(segs)
        self.assertEqual({s.source for s in segs}, {"transitous"})
        self.assertTrue(all(s.price_try is None and not s.price_is_estimated for s in segs))


if __name__ == "__main__":
    unittest.main()


# -- Google Maps timetables and obilet from-prices ------------------------------------------------

_MONDAY = "2026-11-09"  # the Maps fixtures are the options of this Monday
_MAPS_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "gmaps")
_OBILET_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "obilet")


def _maps_seg(dep, arr, *, mode="train", price_try=None, estimated=False, operator="FR"):
    """A Google Maps option as the provider returns it: no fare unless price_try is given."""
    return ScheduledSegment(
        origin_station="as given", dest_station="as given",
        departure_time=dep, arrival_time=arr, duration_mins=120,
        price_try=price_try, operator=operator, mode=mode, date=_DATE,
        is_estimated=False, price_eur=None,
        price_is_estimated=estimated if price_try is not None else False,
        source="google_maps",
    )


class _StubMaps:
    """Google Maps timetable stand-in: can_resolve, search (with max_results and budget) and health_status.

    One page load is charged to ``budget`` per search, as the real provider does; a refused one gives None.
    """

    def __init__(self, *, resolvable=True, result=None, error=None, health=None):
        self.resolvable = resolvable
        self.result = result
        self.error = error
        self.health = health if health is not None else {"source": "google_maps", "status": "ok"}
        self.search_calls = []
        self.refused = 0

    def can_resolve(self, place):
        return self.resolvable

    def search(self, from_place, to_place, date_str, earliest_dep_time=None,
               from_coord=None, to_coord=None, max_results=6, budget=None):
        self.search_calls.append((from_place, to_place, date_str, earliest_dep_time, max_results))
        if budget is not None and not budget.take():
            self.refused += 1
            return None
        if self.error is not None:
            raise self.error
        return None if self.result is None else [copy.copy(s) for s in self.result]

    def health_status(self):
        if isinstance(self.health, Exception):
            raise self.health
        return self.health


class _StubObilet:
    """obilet stand-in: from_prices only."""

    def __init__(self, prices=None, error=None, health=None):
        self.prices = prices or {}
        self.error = error
        self.health = health if health is not None else {"source": "obilet", "status": "ok"}
        self.calls = []

    def from_prices(self, from_place, to_place):
        self.calls.append((from_place, to_place))
        if self.error is not None:
            raise self.error
        return dict(self.prices)

    def health_status(self):
        if isinstance(self.health, Exception):
            raise self.health
        return self.health


class MapsMergeTests(_GroundCase):
    """Google Maps segments among the live sources, and the fare ranking that decides duplicates."""

    def test_maps_only_service_is_kept_without_a_fare_and_hides_the_table(self):
        maps = _StubMaps(result=[_maps_seg("07:10", "11:20")])
        segs = self._provider(_StubLive(result=[]), _StubBus(result=[]), None).find_ground(
            "Wien Hbf", "Praha hl.n.", _DATE, timetable_maps_provider=maps)
        self.assertEqual([(s.source, s.departure_time) for s in segs], [("google_maps", "07:10")])
        self.assertIsNone(segs[0].price_try)
        self.assertFalse(segs[0].price_is_estimated)
        self.assertFalse(segs[0].is_estimated)

    def test_maps_is_queried_with_the_earliest_time_and_the_cap(self):
        maps = _StubMaps(result=[_maps_seg("09:00", "11:00")])
        self._provider().find_ground("Vienna", "Budapest", _DATE, "08:30", max_results=4,
                                     timetable_maps_provider=maps)
        # Maps keeps its own anchor (the earliest time); the cap of 4 is applied to its answer.
        self.assertEqual([call[:4] for call in maps.search_calls], [("Vienna", "Budapest", _DATE, "08:30")])

    def test_maps_none_or_empty_falls_back_to_the_table(self):
        for result in (None, []):
            with self.subTest(result=result):
                maps = _StubMaps(result=result)
                segs = self._provider(_StubLive(result=[]), _StubBus(result=[]), None).find_ground(
                    "Wien Hbf", "Praha hl.n.", _DATE, timetable_maps_provider=maps)
                self.assertEqual({s.source for s in segs}, {"table"})

    def test_maps_error_keeps_the_other_sources(self):
        live = _StubLive(result=[_seg("live", "09:00", "11:00", price_try=1000)])
        maps = _StubMaps(error=RuntimeError("browser gone"))
        segs = self._provider(live, _StubBus(result=[]), None).find_ground(
            "Vienna", "Budapest", _DATE, timetable_maps_provider=maps)
        self.assertEqual({s.source for s in segs}, {"live"})

    def test_maps_unresolvable_place_is_skipped_without_a_search(self):
        maps = _StubMaps(resolvable=False, result=[_maps_seg("09:00", "11:00")])
        self._provider(_StubLive(result=[]), _StubBus(result=[]), None).find_ground(
            "Wien Hbf", "Praha hl.n.", _DATE, timetable_maps_provider=maps)
        self.assertEqual(maps.search_calls, [])

    def test_maps_is_not_asked_when_regiojet_answers(self):
        live = _StubLive(result=[_seg("live", "09:00", "11:00", price_try=1000)])
        maps = _StubMaps(result=[_maps_seg("09:00", "11:00", price_try=None)])
        segs = self._provider(live, _StubBus(result=[]), None).find_ground(
            "Vienna", "Budapest", _DATE, timetable_maps_provider=maps)
        self.assertEqual([(s.source, s.price_try) for s in segs], [("live", 1000)])
        self.assertEqual(maps.search_calls, [])

    def test_maps_is_not_asked_when_transitous_has_an_estimate(self):
        timetable = _StubTimetable(result=[_seg("transitous", "09:00", "11:00", price_try=600, estimated=True)])
        maps = _StubMaps(result=[_maps_seg("09:00", "11:00", price_try=None)])
        segs = self._provider(_StubLive(result=[]), _StubBus(result=[]), timetable).find_ground(
            "Vienna", "Budapest", _DATE, timetable_maps_provider=maps)
        self.assertEqual([(s.source, s.price_try, s.price_is_estimated) for s in segs],
                         [("transitous", 600, True)])
        self.assertEqual(maps.search_calls, [])

    def test_maps_is_asked_only_after_transitous_came_back_empty(self):
        timetable = _StubTimetable(result=[])
        maps = _StubMaps(result=[_maps_seg("09:00", "11:00", price_try=800)])
        segs = self._provider(_StubLive(result=[]), _StubBus(result=[]), timetable).find_ground(
            "Vienna", "Budapest", _DATE, timetable_maps_provider=maps)
        self.assertEqual([(s.source, s.price_try) for s in segs], [("google_maps", 800)])
        self.assertEqual(len(maps.search_calls), 1)

    def test_two_unpriced_duplicates_keep_the_earlier_source(self):
        live_unpriced = _maps_seg("09:00", "11:00")
        live_unpriced.source = "live"
        timetable_unpriced = _maps_seg("09:00", "11:00")
        timetable_unpriced.source = "transitous"
        segs = self._provider(_StubLive(result=[live_unpriced]), _StubBus(result=[]),
                              _StubTimetable(result=[timetable_unpriced])).find_ground(
            "Vienna", "Budapest", _DATE)
        self.assertEqual([s.source for s in segs], ["live"])


class PriceFromTests(_GroundCase):
    """obilet "from" fares: only on bus segments without a fare, only when the operator matches."""

    def _bus(self, operator, *, price_try=None, dep="09:00"):
        return _maps_seg(dep, "13:00", mode="bus", price_try=price_try, operator=operator)

    def _find(self, maps_segments, obilet):
        maps = _StubMaps(result=maps_segments)
        return self._provider(_StubLive(result=[]), _StubBus(result=[]), None).find_ground(
            "Istanbul", "Ankara", _DATE, timetable_maps_provider=maps, bus_from_price_provider=obilet,
        )

    def test_unpriced_bus_gets_the_operator_from_price(self):
        obilet = _StubObilet({"kamil koc": 1099.0})
        segs = self._find([self._bus("Kamil Koç 81030")], obilet)
        self.assertEqual((segs[0].price_try, segs[0].price_from_try), (None, 1099.0))
        self.assertEqual(obilet.calls, [("Istanbul", "Ankara")])

    def test_operator_without_a_match_gets_no_from_price(self):
        segs = self._find([self._bus("Metro Turizm")], _StubObilet({"kamil koc": 1099.0}))
        self.assertIsNone(segs[0].price_from_try)
        self.assertIsNone(segs[0].price_try)

    def test_train_segments_are_not_given_a_bus_fare_hint(self):
        obilet = _StubObilet({"kamil koc": 1099.0})
        segs = self._find([_maps_seg("09:00", "11:00", mode="train", operator="Kamil Koç")], obilet)
        self.assertIsNone(segs[0].price_from_try)
        self.assertEqual(obilet.calls, [])

    def test_priced_bus_is_not_touched_and_obilet_is_not_asked(self):
        bus = _StubBus(result=[_seg("flixbus", "09:00", "13:00", mode="bus", price_try=900, operator="Kamil Koç")])
        obilet = _StubObilet({"kamil koc": 1099.0})
        segs = self._provider(_StubLive(result=[]), bus, None).find_ground(
            "Istanbul", "Ankara", _DATE, timetable_maps_provider=_StubMaps(result=[]),
            bus_from_price_provider=obilet)
        self.assertEqual([(s.source, s.price_try, s.price_from_try) for s in segs], [("flixbus", 900, None)])
        self.assertEqual(obilet.calls, [])

    def test_empty_obilet_answer_attaches_nothing(self):
        segs = self._find([self._bus("Kamil Koç")], _StubObilet({}))
        self.assertIsNone(segs[0].price_from_try)

    def test_obilet_error_never_breaks_the_search(self):
        segs = self._find([self._bus("Kamil Koç")], _StubObilet(error=RuntimeError("down")))
        self.assertEqual(len(segs), 1)
        self.assertIsNone(segs[0].price_from_try)

    def test_no_obilet_provider_means_no_hint(self):
        segs = self._find([self._bus("Kamil Koç")], None)
        self.assertIsNone(segs[0].price_from_try)

    def test_to_dict_carries_the_hint_and_a_null_price(self):
        segs = self._find([self._bus("Kamil Koç")], _StubObilet({"kamil koc": 1099.0}))
        data = segs[0].to_dict()
        self.assertIsNone(data["price_try"])
        self.assertIsNone(data["price_eur"])
        self.assertEqual(data["price_from_try"], 1099.0)


class MapsWiringTests(_GroundCase):
    def test_constructor_providers_are_used_by_default(self):
        maps = _StubMaps(result=[_maps_seg("09:00", "11:00")])
        provider = RailBusProvider(airport_db=self.airports, timetable_maps_provider=maps)
        segs = provider.find_ground("Vienna", "Budapest", _DATE)
        self.assertEqual(len(maps.search_calls), 1)
        self.assertEqual([s.source for s in segs], ["google_maps"])

    def test_find_ground_argument_overrides_the_constructor(self):
        built_in = _StubMaps(result=[_maps_seg("09:00", "11:00")])
        per_call = _StubMaps(result=[_maps_seg("10:00", "12:00")])
        provider = RailBusProvider(airport_db=self.airports, timetable_maps_provider=built_in)
        segs = provider.find_ground("Vienna", "Budapest", _DATE, timetable_maps_provider=per_call)
        self.assertEqual(built_in.search_calls, [])
        self.assertEqual([s.departure_time for s in segs], ["10:00"])

    def test_sources_health_reports_google_maps_and_obilet_through_their_own_health(self):
        maps = _StubMaps(health={"source": "google_maps", "status": "blocked", "healthy": False})
        obilet = _StubObilet(health={"source": "obilet", "status": "ok", "healthy": True})
        health = RailBusProvider(airport_db=self.airports, timetable_maps_provider=maps,
                                 bus_from_price_provider=obilet).sources_health()
        self.assertEqual(health["google_maps"], {"source": "google_maps", "status": "blocked", "healthy": False})
        self.assertEqual(health["obilet"], {"source": "obilet", "status": "ok", "healthy": True})

    def test_sources_health_without_the_new_providers_is_unavailable(self):
        health = RailBusProvider(airport_db=self.airports).sources_health()
        self.assertEqual(health["google_maps"], {"available": False})
        self.assertEqual(health["obilet"], {"available": False})

    def test_failing_health_of_the_new_sources_does_not_raise(self):
        provider = RailBusProvider(airport_db=self.airports,
                                   timetable_maps_provider=_StubMaps(health=RuntimeError("boom")),
                                   bus_from_price_provider=_StubObilet(health=RuntimeError("boom")))
        health = provider.sources_health()
        self.assertEqual(health["google_maps"], {"available": True, "error": "boom"})
        self.assertEqual(health["obilet"], {"available": True, "error": "boom"})


class MapsObiletEndToEndTests(_GroundCase):
    """find_ground with the real Google Maps and obilet providers and fixture pages (no network)."""

    def setUp(self):
        super().setUp()
        from travel_engine.providers import google_maps_transit_provider as gmt
        from travel_engine.providers import obilet_fares as ob

        gmt.GoogleMapsTransitProvider.reset_shared_state()
        ob.ObiletFromPrices.reset_shared_state()
        self.addCleanup(gmt.GoogleMapsTransitProvider.reset_shared_state)
        self.addCleanup(ob.ObiletFromPrices.reset_shared_state)
        with open(os.path.join(_MAPS_FIXTURES, "istanbul_ankara_tr.txt"), encoding="utf-8") as f:
            self.maps_text = f.read()
        with open(os.path.join(_OBILET_FIXTURES, "istanbul_ankara_route.html"), encoding="utf-8") as f:
            self.obilet_html = f.read()
        self.maps_urls = []
        self.obilet_urls = []

        def fetch_page(url, *, locale, timezone_id, wait_ms):
            self.maps_urls.append(url)
            return url, self.maps_text

        def fetch_html(url):
            self.obilet_urls.append(url)
            return self.obilet_html

        self.maps = gmt.GoogleMapsTransitProvider(fetch_page=fetch_page, min_interval_secs=0, wait_ms=0)
        self.obilet = ob.ObiletFromPrices(fetch_html=fetch_html, min_interval_secs=0)

    def test_turkish_bus_options_come_with_the_obilet_from_price(self):
        provider = RailBusProvider(airport_db=self.airports, timetable_maps_provider=self.maps,
                                   bus_from_price_provider=self.obilet)
        segs = provider.find_ground("İstanbul", "Ankara", _MONDAY)
        self.assertEqual(len(segs), 6)
        self.assertTrue(all(s.source == "google_maps" and s.mode == "bus" for s in segs))
        self.assertTrue(all(s.price_try is None for s in segs))
        self.assertTrue(all(s.price_from_try == 1099.0 for s in segs))  # obilet's Kamil Koç from-price
        self.assertEqual(len(self.maps_urls), 1)
        self.assertEqual(self.obilet_urls, ["https://www.obilet.com/otobus-bileti/istanbul-ankara"])
        self.assertEqual(provider.sources_health()["google_maps"]["status"], "ok")
        self.assertEqual(provider.sources_health()["obilet"]["status"], "ok")

    def test_without_obilet_the_same_options_have_no_hint(self):
        segs = RailBusProvider(airport_db=self.airports, timetable_maps_provider=self.maps).find_ground(
            "İstanbul", "Ankara", _MONDAY)
        self.assertTrue(all(s.price_from_try is None and s.price_try is None for s in segs))
        self.assertEqual(self.obilet_urls, [])


class _StubTrenitalia(_StubBus):
    """Trenitalia stand-in: can_resolve, search (with max_results) and health_status like TrenitaliaProvider."""

    def __init__(self, **kwargs):
        kwargs.setdefault("health", {"source": "trenitalia", "status": "ok"})
        super().__init__(**kwargs)


class TrenitaliaSourceTests(_GroundCase):
    """Trenitalia is a priced live source: merged with the others, and ahead of Maps, table and estimate."""

    def _provider(self, trenitalia=None, live=None, maps=None):
        return RailBusProvider(live_provider=live, trenitalia_provider=trenitalia,
                               timetable_maps_provider=maps, airport_db=self.airports)

    def test_trenitalia_answer_hides_maps_table_and_estimate(self):
        trenitalia = _StubTrenitalia(result=[_seg("trenitalia", "09:00", "11:00", price_try=700)])
        maps = _StubMaps(result=[_maps_seg("08:00", "10:00", price_try=500)])
        segs = self._provider(trenitalia, maps=maps).find_ground("Milano", "Venezia", _DATE)
        self.assertEqual([(s.source, s.departure_time, s.price_try) for s in segs], [("trenitalia", "09:00", 700)])
        self.assertEqual(maps.search_calls, [])

    def test_trenitalia_gets_the_earliest_time_and_the_cap(self):
        trenitalia = _StubTrenitalia(result=[])
        self._provider(trenitalia).find_ground("Milano", "Venezia", _DATE, "08:30", max_results=4)
        # Trenitalia is asked page by page from the start of the day; the empty first page ends it.
        self.assertEqual(trenitalia.search_calls[0][:4], ("Milano", "Venezia", _DATE, None))
        self.assertEqual(len(trenitalia.search_calls), 1)

    def test_cheaper_known_trenitalia_fare_beats_a_regiojet_duplicate(self):
        live = _StubLive(result=[_seg("live", "09:00", "11:00", price_try=1000)])
        trenitalia = _StubTrenitalia(result=[_seg("trenitalia", "09:00", "11:00", price_try=800)])
        segs = self._provider(trenitalia, live).find_ground("Milano", "Venezia", _DATE)
        self.assertEqual([(s.source, s.price_try) for s in segs], [("trenitalia", 800)])

    def test_unpriced_trenitalia_train_is_kept_without_a_fare(self):
        trenitalia = _StubTrenitalia(result=[_maps_seg("09:00", "11:00")])  # no fare, times are real
        segs = self._provider(trenitalia).find_ground("Milano", "Venezia", _DATE)
        self.assertEqual([(s.source, s.price_try, s.price_is_estimated, s.is_estimated) for s in segs],
                         [("trenitalia", None, False, False)])

    def test_trenitalia_error_keeps_the_other_sources(self):
        live = _StubLive(result=[_seg("live", "09:00", "11:00", price_try=1000)])
        segs = self._provider(_StubTrenitalia(error=RuntimeError("blocked")), live).find_ground(
            "Vienna", "Budapest", _DATE)
        self.assertEqual({s.source for s in segs}, {"live"})

    def test_unresolvable_place_is_skipped_without_a_search(self):
        trenitalia = _StubTrenitalia(resolvable=False, result=[_seg("trenitalia", "09:00", "11:00")])
        self._provider(trenitalia).find_ground("Milano", "Venezia", _DATE)
        self.assertEqual(trenitalia.search_calls, [])

    def test_health_is_its_own_report_and_unconfigured_is_unavailable(self):
        trenitalia = _StubTrenitalia(health={"source": "trenitalia", "status": "blocked"})
        self.assertEqual(self._provider(trenitalia).sources_health()["trenitalia"],
                         {"source": "trenitalia", "status": "blocked"})
        self.assertEqual(self._provider().sources_health()["trenitalia"], {"available": False})


class MapsBudgetTests(_GroundCase):
    """Google Maps is a fallback charged per page load to a budget shared by one search."""

    def test_budget_counts_page_loads_and_refuses_past_the_limit(self):
        budget = MapsBudget(2)
        self.assertEqual([budget.take() for _ in range(3)], [True, True, False])
        self.assertEqual((budget.used, budget.denied, budget.exhausted), (2, 1, True))

    def test_zero_limit_turns_maps_off_without_a_refusal(self):
        maps = _StubMaps(result=[_maps_seg("09:00", "11:00")])
        segs = RailBusProvider(airport_db=self.airports, timetable_maps_provider=maps,
                               maps_budget_limit=0).find_ground("Vienna", "Budapest", _DATE)
        self.assertEqual(maps.search_calls, [])
        self.assertNotIn("google_maps", {s.source for s in segs})

    def test_one_budget_is_shared_by_the_calls_of_one_search(self):
        maps = _StubMaps(result=[_maps_seg("09:00", "11:00")])
        provider = RailBusProvider(airport_db=self.airports, timetable_maps_provider=maps)
        budget = MapsBudget(1)
        first = provider.find_ground("Vienna", "Budapest", _DATE, maps_budget=budget)
        second = provider.find_ground("Lyon", "Geneva", _DATE, maps_budget=budget)
        self.assertIn("google_maps", {s.source for s in first})
        self.assertNotIn("google_maps", {s.source for s in second})
        self.assertEqual((budget.used, maps.refused), (1, 1))
        self.assertTrue(budget.exhausted)

    def test_refused_lookup_falls_through_to_the_table(self):
        maps = _StubMaps(result=[_maps_seg("09:00", "11:00")])
        budget = MapsBudget(1)
        budget.take()  # used up before this search reaches Maps
        segs = RailBusProvider(airport_db=self.airports, timetable_maps_provider=maps).find_ground(
            "Wien Hbf", "Praha hl.n.", _DATE, maps_budget=budget)
        self.assertEqual({s.source for s in segs}, {"table"})
        self.assertTrue(budget.exhausted)

    def test_budget_is_not_touched_when_a_live_source_answers(self):
        live = _StubLive(result=[_seg("live", "09:00", "11:00", price_try=1000)])
        budget = MapsBudget(1)
        RailBusProvider(live_provider=live, airport_db=self.airports,
                        timetable_maps_provider=_StubMaps(result=[])).find_ground(
            "Vienna", "Budapest", _DATE, maps_budget=budget)
        self.assertEqual((budget.used, budget.denied), (0, 0))

    def test_find_ground_next_shares_one_budget_across_both_days(self):
        maps = _StubMaps(result=None)  # Maps answers nothing; each day still asks
        budget = MapsBudget(1)
        provider = RailBusProvider(airport_db=self.airports, timetable_maps_provider=maps)
        date_used, segs = provider.find_ground_next("Lyon", "Geneva", _DATE, allow_estimate=False,
                                                    maps_budget=budget)
        self.assertEqual((date_used, segs), (_DATE, []))
        self.assertEqual((budget.used, maps.refused), (1, 1))


class AllowEstimateTests(_GroundCase):
    """allow_estimate=False: only live answers (and Maps) come back; the table and the estimate never run."""

    def test_table_and_estimate_are_skipped(self):
        provider = RailBusProvider(airport_db=self.airports)
        self.assertEqual(provider.find_ground("Wien Hbf", "Praha hl.n.", _DATE, allow_estimate=False), [])
        self.assertEqual(provider.find_ground("Lyon", "Marseille", _DATE, allow_estimate=False), [])
        self.assertEqual({s.source for s in provider.find_ground("Wien Hbf", "Praha hl.n.", _DATE)}, {"table"})
        self.assertEqual({s.source for s in provider.find_ground("Lyon", "Marseille", _DATE)}, {"estimate"})

    def test_live_and_maps_answers_are_still_returned(self):
        live = _StubLive(result=[_seg("live", "09:00", "11:00", price_try=1000)])
        segs = RailBusProvider(live_provider=live, airport_db=self.airports).find_ground(
            "Wien Hbf", "Praha hl.n.", _DATE, allow_estimate=False)
        self.assertEqual([s.source for s in segs], ["live"])
        maps = _StubMaps(result=[_maps_seg("09:00", "11:00", price_try=800)])
        segs = RailBusProvider(airport_db=self.airports, timetable_maps_provider=maps).find_ground(
            "Lyon", "Geneva", _DATE, allow_estimate=False)
        self.assertEqual([(s.source, s.price_try) for s in segs], [("google_maps", 800)])


class MapsBudgetConfigTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(MAPS_BUDGET_ENV, None)

    def test_env_value_is_read_and_bad_values_fall_back_to_the_default(self):
        for raw, expected in [(None, 4), ("", 4), ("2", 2), (" 0 ", 0), ("x", 4), ("-1", 4), ("2.5", 4)]:
            with self.subTest(raw=raw):
                os.environ.pop(MAPS_BUDGET_ENV, None)
                if raw is not None:
                    os.environ[MAPS_BUDGET_ENV] = raw
                self.assertEqual(maps_budget_from_env(), expected)

    def test_provider_reads_the_env_unless_a_limit_is_given(self):
        os.environ[MAPS_BUDGET_ENV] = "2"
        self.assertEqual(RailBusProvider().maps_budget_limit, 2)
        self.assertEqual(RailBusProvider(maps_budget_limit=0).maps_budget_limit, 0)
