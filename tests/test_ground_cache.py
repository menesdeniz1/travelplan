"""Whole-day ground cache: the SQLite store, the environment switches, and RailBusProvider's use of it.

No network. Ground sources are stubs that honour the earliest time and the cap like the real providers,
count their calls, can sleep (to show that the sources are asked at the same time) and can fail.
"""

import dataclasses
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from travel_engine import ground_cache as gc
from travel_engine.airports import AirportDB
from travel_engine.ground_cache import GroundCache
from travel_engine.providers import rail_bus_provider as rbp
from travel_engine.providers.rail_bus_provider import (
    MapsBudget, RailBusProvider, ScheduledSegment, segment_from_dict,
)

DAY = "2026-11-12"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FX = "travel_engine.fx.get_eur_try_rate"


def seg(dep, arr, *, operator="Op", mode="train", price_try=500.0):
    return ScheduledSegment(
        origin_station="as given", dest_station="as given", departure_time=dep, arrival_time=arr,
        duration_mins=120, price_try=price_try, operator=operator, mode=mode, date=DAY,
        is_estimated=False, price_eur=10.0, price_is_estimated=False,
    )


class StubSource:
    """A ground source: can_resolve, search (whole-day aware), with call counter, sleep and failure switches.

    ``departures`` is the whole day. ``search`` returns the departures from ``earliest_dep_time`` on, sorted,
    cut to ``max_results`` (the real providers cut the same way). ``answer_none_on`` lists 1-based call
    numbers that answer None (a failure); ``raise_always`` makes every call raise.
    """

    def __init__(self, departures=(), *, sleep=0.0, answer_none_on=(), raise_always=False, resolvable=True,
                 empty_answer=False, shared=None):
        self.departures = list(departures)
        # ``shared``: a dict with "in_flight", "max_in_flight" and "lock", counted across several sources.
        self.shared = shared
        self.sleep = sleep
        self.answer_none_on = set(answer_none_on)
        self.raise_always = raise_always
        self.resolvable = resolvable
        self.empty_answer = empty_answer
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def can_resolve(self, place):
        return self.resolvable

    def search(self, from_place, to_place, date_str, earliest_dep_time=None, max_results=6, **kwargs):
        with self._lock:
            self.calls.append((from_place, to_place, date_str, earliest_dep_time, max_results))
            call_no = len(self.calls)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.shared is not None:
            with self.shared["lock"]:
                self.shared["in_flight"] += 1
                self.shared["max_in_flight"] = max(self.shared["max_in_flight"], self.shared["in_flight"])
        try:
            if self.sleep:
                time.sleep(self.sleep)
            if self.raise_always:
                raise RuntimeError("source down")
            if call_no in self.answer_none_on:
                return None
            if self.empty_answer:
                return []
            out = []
            for item in sorted(self.departures, key=lambda s: s.departure_time):
                if earliest_dep_time and item.departure_time < earliest_dep_time:
                    continue
                copy = dataclasses.replace(item, origin_station=from_place, dest_station=to_place)
                out.append(copy)
            return out[:max(0, int(max_results))]
        finally:
            with self._lock:
                self.in_flight -= 1
            if self.shared is not None:
                with self.shared["lock"]:
                    self.shared["in_flight"] -= 1


class StubMaps(StubSource):
    """Google Maps stand-in: charges one page load per search to ``budget`` (a refused load answers None)."""

    def search(self, from_place, to_place, date_str, earliest_dep_time=None, max_results=6,
               from_coord=None, to_coord=None, budget=None):
        if budget is not None and not budget.take():
            return None
        return super().search(from_place, to_place, date_str, earliest_dep_time, max_results)


def make_provider(cache=None, clock=None, **sources):
    kwargs = dict(airport_db=AirportDB.load_default(), ground_cache=cache, maps_budget_limit=4)
    if clock is not None:
        kwargs["clock"] = clock
    if "live" in sources:
        kwargs["live_provider"] = sources.pop("live")
    if "bus" in sources:
        kwargs["bus_provider"] = sources.pop("bus")
    if "trenitalia" in sources:
        kwargs["trenitalia_provider"] = sources.pop("trenitalia")
    if "timetable" in sources:
        kwargs["timetable_provider"] = sources.pop("timetable")
    if "maps" in sources:
        kwargs["timetable_maps_provider"] = sources.pop("maps")
    return RailBusProvider(**kwargs)


class GroundCacheStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ground_cache_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = [1_000_000.0]
        self.cache = GroundCache(os.path.join(self.dir, "ground_cache.db"), clock=lambda: self.now[0])

    def payload(self, *deps):
        return [seg(dep, dep).to_dict() for dep in deps]

    def test_round_trip_and_case_insensitive_places(self):
        self.assertTrue(self.cache.put("regiojet", "Vienna  Hbf", "Praha hl.n.", DAY, self.payload("09:00")))
        rows = self.cache.get("regiojet", "vienna hbf", "PRAHA HL.N.", DAY)
        self.assertEqual([row["departure_time"] for row in rows], ["09:00"])

    def test_keys_separate_source_day_anchor_and_coordinates(self):
        self.cache.put("regiojet", "A", "B", DAY, self.payload("09:00"))
        self.assertIsNone(self.cache.get("flixbus", "A", "B", DAY))
        self.assertIsNone(self.cache.get("regiojet", "A", "B", "2026-11-13"))
        self.assertIsNone(self.cache.get("regiojet", "A", "B", DAY, anchor="08:00"))
        self.assertIsNone(self.cache.get("regiojet", "A", "B", DAY, coords="1.0000,2.0000|-"))

    def test_none_is_refused_so_an_error_is_never_stored(self):
        self.assertFalse(self.cache.put("flixbus", "A", "B", DAY, None))
        self.assertIsNone(self.cache.get("flixbus", "A", "B", DAY))
        self.assertEqual(self.cache.row_count(), 0)

    def test_non_empty_answer_lives_the_full_ttl(self):
        self.cache.put("flixbus", "A", "B", DAY, self.payload("09:00"))
        self.now[0] += gc.DEFAULT_TTL_SECS - 1
        self.assertIsNotNone(self.cache.get("flixbus", "A", "B", DAY))
        self.now[0] += 2
        self.assertIsNone(self.cache.get("flixbus", "A", "B", DAY))

    def test_empty_answer_lives_one_hour(self):
        self.cache.put("flixbus", "A", "B", DAY, [])
        self.now[0] += gc.EMPTY_TTL_SECS - 1
        self.assertEqual(self.cache.get("flixbus", "A", "B", DAY), [])
        self.now[0] += 2
        self.assertIsNone(self.cache.get("flixbus", "A", "B", DAY))

    def test_ttl_env_overrides_the_default(self):
        with mock.patch.dict(os.environ, {gc.TTL_ENV: "120"}):
            cache = GroundCache(os.path.join(self.dir, "other.db"), clock=lambda: self.now[0])
        self.assertEqual(cache.ttl_secs, 120.0)
        cache.put("flixbus", "A", "B", DAY, self.payload("09:00"))
        self.now[0] += 121
        self.assertIsNone(cache.get("flixbus", "A", "B", DAY))

    def test_prune_drops_expired_rows_then_the_oldest_beyond_the_limit(self):
        small = GroundCache(os.path.join(self.dir, "small.db"), max_rows=2, clock=lambda: self.now[0])
        for i in range(4):
            small.put("flixbus", f"P{i}", "B", DAY, self.payload("09:00"))
            self.now[0] += 1
        self.assertEqual(small.row_count(), 4)
        small.prune()
        self.assertEqual(small.row_count(), 2)
        self.assertIsNotNone(small.get("flixbus", "P3", "B", DAY))
        self.assertIsNone(small.get("flixbus", "P0", "B", DAY))
        self.now[0] += gc.DEFAULT_TTL_SECS + 1
        small.prune()
        self.assertEqual(small.row_count(), 0)

    def test_concurrent_writers_and_readers_do_not_fail(self):
        errors = []

        def worker(n):
            try:
                for i in range(5):
                    self.cache.put("flixbus", f"Place {n}", f"Dest {i}", DAY, self.payload("10:00"))
                    self.assertIsNotNone(self.cache.get("flixbus", f"place {n}", f"dest {i}", DAY))
            except Exception as exc:  # collected for the main thread
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.cache.row_count(), 30)

    def test_database_is_created_on_first_use(self):
        path = os.path.join(self.dir, "lazy", "ground_cache.db")
        cache = GroundCache(path)
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(cache.get("flixbus", "A", "B", DAY))
        self.assertTrue(os.path.exists(path))

    def test_unreadable_database_is_a_miss_not_an_error(self):
        path = os.path.join(self.dir, "broken.db")
        with open(path, "wb") as handle:
            handle.write(b"this is not sqlite" * 50)
        cache = GroundCache(path)
        self.assertIsNone(cache.get("flixbus", "A", "B", DAY))
        self.assertFalse(cache.put("flixbus", "A", "B", DAY, []))


class EnvironmentTests(unittest.TestCase):
    def test_off_disables_the_default_cache(self):
        for value in ("off", "0", "none", "false", "no", "OFF"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {gc.PATH_ENV: value}):
                self.assertIsNone(gc.from_env())

    def test_explicit_path_is_used(self):
        with mock.patch.dict(os.environ, {gc.PATH_ENV: "C:/scratch/gc.db"}):
            cache = gc.from_env()
        self.assertEqual(cache.path, os.path.abspath("C:/scratch/gc.db"))

    def test_test_runs_never_use_the_repo_root(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(gc.PATH_ENV, None)
            first, second = gc.from_env(), gc.from_env()
        self.assertNotEqual(first.path, second.path)
        self.assertFalse(first.path.startswith(ROOT + os.sep))

    def test_norm_place_and_coords_text(self):
        self.assertEqual(gc.norm_place("  Athens   Airport "), "athens airport")
        self.assertEqual(gc.coords_text(None, None), "")
        self.assertEqual(gc.coords_text((48.2, 16.37), None), "48.2000,16.3700|-")

    def test_segment_dict_round_trip(self):
        original = seg("09:15", "11:20", operator="RegioJet")
        self.assertEqual(segment_from_dict(original.to_dict()), original)


class ProviderCacheTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch(FX, return_value=(50.0, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = tempfile.mkdtemp(prefix="ground_provider_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "ground_cache.db")
        self.now = [5_000_000.0]
        self.clock = lambda: self.now[0]  # noqa: E731

    def cache(self):
        return GroundCache(self.path, clock=self.clock)

    def live_day(self):
        return StubSource([seg("07:00", "09:00", operator="RJ"), seg("13:00", "15:00", operator="RJ"),
                           seg("19:00", "21:00", operator="RJ")])

    def bus_day(self):
        return StubSource([seg("08:30", "11:00", operator="FB", mode="bus"),
                           seg("14:00", "16:30", operator="FB", mode="bus")])

    def test_one_source_call_per_pair_and_day_for_many_earliest_times(self):
        live, bus = self.live_day(), self.bus_day()
        provider = make_provider(self.cache(), clock=self.clock, live=live, bus=bus)
        for earliest in (None, "06:00", "08:00", "12:00", "18:00", "23:00"):
            provider.find_ground("Vienna", "Prague", DAY, earliest_dep_time=earliest)
        self.assertEqual(len(live.calls), 1)
        self.assertEqual(len(bus.calls), 1)
        self.assertEqual(live.calls[0][3], None)  # the whole day

    def test_the_day_is_asked_again_after_the_ttl(self):
        live = self.live_day()
        provider = make_provider(self.cache(), clock=self.clock, live=live)
        provider.find_ground("Vienna", "Prague", DAY)
        self.now[0] += gc.DEFAULT_TTL_SECS - 60
        provider.find_ground("Vienna", "Prague", DAY)
        self.assertEqual(len(live.calls), 1)
        self.now[0] += 120
        provider.find_ground("Vienna", "Prague", DAY)
        self.assertEqual(len(live.calls), 2)

    def test_a_new_provider_with_the_same_cache_path_is_served_from_disk(self):
        live, bus = self.live_day(), self.bus_day()
        first = make_provider(self.cache(), clock=self.clock, live=live, bus=bus)
        before = first.find_ground("Vienna", "Prague", DAY, earliest_dep_time="12:00")
        second_live, second_bus = self.live_day(), self.bus_day()
        second = make_provider(self.cache(), clock=self.clock, live=second_live, bus=second_bus)
        after = second.find_ground("Vienna", "Prague", DAY, earliest_dep_time="12:00")
        self.assertEqual(second_live.calls, [])
        self.assertEqual(second_bus.calls, [])
        self.assertEqual([s.to_dict() for s in before], [s.to_dict() for s in after])

    def test_empty_answer_is_reused_for_an_hour_only(self):
        live = StubSource([])
        provider = make_provider(self.cache(), clock=self.clock, live=live)
        provider.find_ground("Vienna", "Prague", DAY)
        self.now[0] += gc.EMPTY_TTL_SECS - 60
        provider.find_ground("Vienna", "Prague", DAY)
        self.assertEqual(len(live.calls), 1)
        self.now[0] += 120
        provider.find_ground("Vienna", "Prague", DAY)
        self.assertEqual(len(live.calls), 2)

    def test_failed_source_is_never_cached(self):
        live = StubSource(self.live_day().departures, answer_none_on={1})
        provider = make_provider(self.cache(), clock=self.clock, live=live)
        self.assertEqual(provider.find_ground("Vienna", "Prague", DAY, allow_estimate=False), [])
        # Another process (a fresh provider) asks again: nothing was stored for the failure.
        second = make_provider(self.cache(), clock=self.clock, live=live)
        found = second.find_ground("Vienna", "Prague", DAY, allow_estimate=False)
        self.assertEqual([s.departure_time for s in found], ["07:00", "13:00", "19:00"])
        self.assertEqual(len(live.calls), 2)

    def test_raising_source_is_never_cached_and_the_others_still_answer(self):
        live = StubSource(raise_always=True)
        bus = self.bus_day()
        provider = make_provider(self.cache(), clock=self.clock, live=live, bus=bus)
        found = provider.find_ground("Vienna", "Prague", DAY, allow_estimate=False)
        self.assertEqual({s.source for s in found}, {"flixbus"})
        provider.find_ground("Vienna", "Prague", DAY, allow_estimate=False)
        self.assertEqual(len(live.calls), 2)
        self.assertEqual(len(bus.calls), 1)

    def test_partial_paged_answer_is_used_but_not_cached(self):
        # Trenitalia: page 1 answers, page 2 fails. The call still gets page 1; the next call asks again.
        departures = [seg(f"{h:02d}:{m:02d}", f"{h + 1:02d}:{m:02d}", operator="IC", mode="train")
                      for h in range(6, 9) for m in (0, 15, 30, 45)]
        trenitalia = StubSource(departures, answer_none_on={2})
        provider = make_provider(self.cache(), clock=self.clock, trenitalia=trenitalia)
        first = provider.find_ground("Milano", "Venezia", DAY, allow_estimate=False)
        self.assertTrue(first)
        self.assertEqual(len(trenitalia.calls), 2)
        provider.find_ground("Milano", "Venezia", DAY, allow_estimate=False)
        self.assertGreater(len(trenitalia.calls), 2)

    def test_disabled_cache_still_shares_the_day_inside_one_provider(self):
        live = self.live_day()
        provider = make_provider(None, clock=self.clock, live=live)
        provider.ground_cache = None  # what TRAVELPLAN_GROUND_CACHE=off gives
        provider.find_ground("Vienna", "Prague", DAY)
        provider.find_ground("Vienna", "Prague", DAY, earliest_dep_time="12:00")
        self.assertEqual(len(live.calls), 1)
        fresh = make_provider(None, clock=self.clock, live=live)
        fresh.ground_cache = None
        fresh.find_ground("Vienna", "Prague", DAY)
        self.assertEqual(len(live.calls), 2)
        self.assertFalse(os.path.exists(self.path))

    def test_concurrent_identical_searches_ask_each_source_once(self):
        live = StubSource(self.live_day().departures, sleep=0.2)
        provider = make_provider(self.cache(), clock=self.clock, live=live)
        results = []

        def search():
            results.append(provider.find_ground("Vienna", "Prague", DAY, earliest_dep_time="08:00"))

        threads = [threading.Thread(target=search) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(live.calls), 1)
        self.assertTrue(all(len(r) == 2 for r in results))

    def test_maps_answer_is_kept_per_anchor_and_costs_no_page_load_when_cached(self):
        maps = StubMaps([seg("09:00", "12:00", operator="Maps", mode="bus")])
        provider = make_provider(self.cache(), clock=self.clock, maps=maps)
        budget = MapsBudget(4)
        provider.find_ground("Athens", "Thessaloniki", DAY, "08:30", maps_budget=budget, allow_estimate=False)
        provider.find_ground("Athens", "Thessaloniki", DAY, "08:30", maps_budget=budget, allow_estimate=False)
        self.assertEqual(len(maps.calls), 1)
        self.assertEqual(budget.used, 1)
        provider.find_ground("Athens", "Thessaloniki", DAY, "12:00", maps_budget=budget, allow_estimate=False)
        self.assertEqual(len(maps.calls), 2)

    def test_refused_maps_page_load_is_not_cached(self):
        maps = StubMaps([seg("09:00", "12:00", operator="Maps", mode="bus")])
        provider = make_provider(self.cache(), clock=self.clock, maps=maps)
        self.assertEqual(provider.find_ground("Athens", "Thessaloniki", DAY, "08:30",
                                              maps_budget=MapsBudget(0), allow_estimate=False), [])
        budget = MapsBudget(1)
        found = provider.find_ground("Athens", "Thessaloniki", DAY, "08:30", maps_budget=budget,
                                     allow_estimate=False)
        self.assertEqual([s.source for s in found], ["google_maps"])
        self.assertEqual(budget.used, 1)

    def test_sources_are_asked_at_the_same_time(self):
        shared = {"in_flight": 0, "max_in_flight": 0, "lock": threading.Lock()}
        sources = {name: StubSource(self.live_day().departures, sleep=0.3, shared=shared)
                   for name in ("live", "bus", "trenitalia", "timetable")}
        provider = make_provider(None, clock=self.clock, live=sources["live"], bus=sources["bus"],
                                 trenitalia=sources["trenitalia"], timetable=sources["timetable"])
        started = time.monotonic()
        provider.find_ground("Vienna", "Prague", DAY, allow_estimate=False)
        elapsed = time.monotonic() - started
        # All four sources were in flight together, and the day took about one source's time, not the sum
        # (sequential: 0.3 + 0.3 + 2 x 0.3 + 2 x 0.3 s, the two paged sources asking twice).
        self.assertEqual(shared["max_in_flight"], 4)
        self.assertLess(elapsed, 1.0)


class LiveDayEquivalenceTests(unittest.TestCase):
    """The whole-day path gives the same answer as one search per earliest time (the old per-call path)."""

    def setUp(self):
        patcher = mock.patch(FX, return_value=(50.0, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = tempfile.mkdtemp(prefix="ground_equiv_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.departures = {
            "live": [seg("07:10", "09:30", operator="RJ", price_try=600.0),
                     seg("12:40", "15:05", operator="RJ", price_try=610.0),
                     seg("18:20", "20:40", operator="RJ", price_try=620.0)],
            "bus": [seg("06:00", "08:40", operator="FB", mode="bus", price_try=400.0),
                    seg("09:45", "12:30", operator="FB", mode="bus", price_try=420.0),
                    seg("12:50", "15:20", operator="FB", mode="bus", price_try=None),
                    seg("17:30", "20:00", operator="FB", mode="bus", price_try=430.0),
                    seg("21:05", "23:30", operator="FB", mode="bus", price_try=440.0)],
            "trenitalia": [seg("08:05", "10:10", operator="FR", price_try=700.0),
                           seg("14:05", "16:10", operator="FR", price_try=650.0)],
            "timetable": [seg("09:00", "11:10", operator="Transit", mode="bus", price_try=500.0)],
        }

    def sources(self):
        return {name: StubSource(items) for name, items in self.departures.items()}

    def sequential_reference(self, provider, sources, earliest, cap):
        """The old per-call semantics, one source call per earliest time, merged the same way."""
        groups = []
        for name, call in (
            ("live", lambda s: s.search("Vienna", "Prague", DAY, earliest, max_results=500)),
            ("bus", lambda s: s.search("Vienna", "Prague", DAY, earliest, max_results=cap)),
            ("trenitalia", lambda s: s.search("Vienna", "Prague", DAY, earliest, max_results=cap)),
            ("timetable", lambda s: s.search("Vienna", "Prague", DAY, earliest, from_coord=None,
                                             to_coord=None, max_results=cap)),
        ):
            found = call(sources[name]) or []
            found = [dataclasses.replace(seg_, source={"live": "live", "bus": "flixbus",
                                                       "trenitalia": "trenitalia",
                                                       "timetable": "transitous"}[name]) for seg_ in found]
            if name != "live":
                found = found[:cap]
            if found:
                groups.append(found)
        return provider._merge_ground(groups, cap) if groups else []

    def test_same_results_as_sequential_searches_for_every_earliest_time(self):
        for earliest in (None, "00:00", "07:10", "07:11", "09:00", "12:00", "12:50", "18:00", "21:30", "23:59"):
            for cap in (2, 4, 6, 12):
                with self.subTest(earliest=earliest, cap=cap):
                    sources = self.sources()
                    provider = make_provider(None, live=sources["live"], bus=sources["bus"],
                                             trenitalia=sources["trenitalia"], timetable=sources["timetable"])
                    got = provider.find_ground("Vienna", "Prague", DAY, earliest_dep_time=earliest,
                                               max_results=cap, allow_estimate=False)
                    want = self.sequential_reference(provider, self.sources(), earliest, cap)
                    self.assertEqual([s.to_dict() for s in got], [s.to_dict() for s in want])

    def test_each_source_is_asked_once_for_all_earliest_times(self):
        sources = self.sources()
        provider = make_provider(None, live=sources["live"], bus=sources["bus"],
                                 trenitalia=sources["trenitalia"], timetable=sources["timetable"])
        for earliest in (None, "08:00", "12:00", "20:00"):
            provider.find_ground("Vienna", "Prague", DAY, earliest_dep_time=earliest, allow_estimate=False)
        self.assertEqual(len(sources["live"].calls), 1)
        self.assertEqual(len(sources["bus"].calls), 1)
        self.assertEqual(len(sources["trenitalia"].calls), 2)  # first page, then from its last departure
        self.assertEqual(len(sources["timetable"].calls), 2)  # first page, then from its last departure

    def test_find_ground_live_is_the_live_part_only(self):
        sources = self.sources()
        maps = StubMaps([seg("05:00", "07:00", operator="Maps", mode="bus")])
        provider = make_provider(None, live=StubSource([]), bus=StubSource([]), maps=maps)
        self.assertEqual(provider.find_ground_live("Vienna", "Prague", DAY), [])
        self.assertEqual(maps.calls, [])
        provider = make_provider(None, live=sources["live"], bus=sources["bus"], maps=maps)
        live = provider.find_ground_live("Vienna", "Prague", DAY, earliest_dep_time="12:00", max_results=6)
        full = provider.find_ground("Vienna", "Prague", DAY, earliest_dep_time="12:00", max_results=6,
                                    allow_estimate=False)
        self.assertEqual([s.to_dict() for s in live], [s.to_dict() for s in full])
        self.assertEqual(maps.calls, [])


if __name__ == "__main__":
    unittest.main()
