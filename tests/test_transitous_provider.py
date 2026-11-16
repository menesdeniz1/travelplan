"""TransitousProvider: opt-in gating, plan parsing, time conversion, filters, caching, health.

No network. ``urlopen`` is replaced by a fake that serves the files in tests/fixtures/transitous:

* ``plan_milano_venezia_2026-11-05.json``: a real capture (see its ``_capture_note``) of the plan from
  Milano Centrale to Venezia S. Lucia, six itineraries.
* ``plan_capture_milano_venezia_2026-11-05.json``: the live response for the city-centre points
  (Milano Duomo to Venezia city centre), which has no itineraries.
* ``geocode_venezia_capture.json``: the live geocode response for "Venezia", unchanged.
"""

import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from travel_engine import fx
from travel_engine.airports import AirportDB, haversine_km
from travel_engine.providers import transitous_provider as tp
from travel_engine.providers.rail_bus_provider import (
    ROAD_DISTANCE_FACTOR,
    RailBusProvider,
    estimate_price_eur,
)
from travel_engine.providers.transitous_provider import TransitousProvider

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "transitous")
_DAY = "2026-11-05"
MILANO = (45.4642, 9.1900)
VENEZIA = (45.4408, 12.3155)
# Fixed price basis for every test: 1 EUR = 50 TRY.
_RATE = 50.0


def _fixture(name: str) -> bytes:
    with open(os.path.join(_FIXTURES, name), "rb") as f:
        return f.read()


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Net:
    """Fake urlopen. Routes /plan and /geocode; ``status``/``error``/``raw`` override every reply."""

    def __init__(self, plan=b"", geocode=b""):
        self.plan_body = plan
        self.geocode_body = geocode
        self.status = None
        self.error = None
        self.raw = None
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        url = request.full_url
        if self.error is not None:
            raise self.error
        if self.status is not None:
            raise urllib.error.HTTPError(url, self.status, "fake", {}, None)
        if self.raw is not None:
            return _FakeResponse(self.raw)
        if "/api/v1/plan" in url:
            return _FakeResponse(self.plan_body)
        if "/api/v1/geocode" in url:
            return _FakeResponse(self.geocode_body)
        raise urllib.error.URLError(f"unexpected URL {url}")

    def plan_calls(self):
        return [r for r in self.calls if "/api/v1/plan" in r.full_url]

    def geocode_calls(self):
        return [r for r in self.calls if "/api/v1/geocode" in r.full_url]


def _query(request) -> dict:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query).items()}


def _stop(name, lat, lon, tz="Europe/Rome"):
    return {"name": name, "lat": lat, "lon": lon, "tz": tz}


def _leg(mode, frm, to, start, end, distance=None, agency=None, route=None):
    leg = {"mode": mode, "from": frm, "to": to, "startTime": start, "endTime": end}
    if distance is not None:
        leg["distance"] = distance
    if agency is not None:
        leg["agencyName"] = agency
    if route is not None:
        leg["routeShortName"] = route
    return leg


def _itinerary(legs, start, end, transfers=None):
    itinerary = {"startTime": start, "endTime": end, "legs": legs}
    if transfers is not None:
        itinerary["transfers"] = transfers
    return itinerary


MI_C = _stop("Milano Centrale", 45.48616, 9.20428)
VE_SL = _stop("Venezia Santa Lucia", 45.44147, 12.32089)
VR_PN = _stop("Verona Porta Nuova", 45.4288, 10.9868)


def _train_direct(start_utc, end_utc, transfers=0):
    """Milano -> Venezia by one train, times as given (UTC)."""
    return _itinerary(
        [_leg("HIGHSPEED_RAIL", MI_C, VE_SL, start_utc, end_utc, 262000, agency="Trenitalia")],
        start_utc, end_utc, transfers,
    )


class _ProviderCase(unittest.TestCase):
    """Enabled provider with the fake network and a fixed EUR/TRY rate."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {tp.ENV_FLAG: "1"})
        env.start()
        self.addCleanup(env.stop)
        rate = mock.patch.object(fx, "get_eur_try_rate", return_value=(_RATE, "test", False))
        rate.start()
        self.addCleanup(rate.stop)
        self.net = _Net(plan=_fixture("plan_milano_venezia_2026-11-05.json"),
                        geocode=_fixture("geocode_venezia_capture.json"))
        patcher = mock.patch.object(tp.urllib.request, "urlopen", side_effect=self.net)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.airports = AirportDB.load_default()
        self.provider = self._make()

    def _make(self, **kwargs) -> TransitousProvider:
        kwargs.setdefault("min_interval_secs", 0)
        kwargs.setdefault("airport_db", self.airports)
        kwargs.setdefault("anchor_cache_path", None)  # no file in the temp dir during tests
        return TransitousProvider(**kwargs)

    def _search_fixture_day(self, **kwargs):
        options = dict(from_coord=MILANO, to_coord=VENEZIA)
        options.update(kwargs)
        return self.provider.search("Milano", "Venezia", _DAY, **options)


class GatingTests(_ProviderCase):
    def test_disabled_by_default_and_makes_no_request(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(tp.ENV_FLAG, None)
            provider = self._make()
            self.assertFalse(tp.is_enabled())
            self.assertFalse(provider.can_resolve("Venezia"))
            self.assertIsNone(provider.search("Milano", "Venezia", _DAY, from_coord=MILANO, to_coord=VENEZIA))
        self.assertEqual(self.net.calls, [])

    def test_only_the_value_one_enables(self):
        for value in ("", "0", "true", "yes", "on", " 2 "):
            with self.subTest(value=value), mock.patch.dict(os.environ, {tp.ENV_FLAG: value}):
                self.assertFalse(tp.is_enabled())
                self.assertIsNone(self._search_fixture_day())
        with mock.patch.dict(os.environ, {tp.ENV_FLAG: " 1 "}):
            self.assertTrue(tp.is_enabled())
        self.assertEqual(self.net.calls, [])

    def test_flag_is_read_on_every_call(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(tp.ENV_FLAG, None)
            self.assertIsNone(self._search_fixture_day())
            os.environ[tp.ENV_FLAG] = "1"
            self.assertIsNotNone(self._search_fixture_day())
        self.assertEqual(len(self.net.plan_calls()), 1)

    def test_health_reports_disabled_without_a_request(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(tp.ENV_FLAG, None)
            health = self._make().health_status()
        self.assertEqual(health["source"], "transitous")
        self.assertFalse(health["enabled"])
        self.assertEqual(health["status"], "disabled")
        self.assertEqual(health["requests"], 0)
        self.assertEqual(self.net.calls, [])

    def test_server_builds_a_disabled_transitous_source(self):
        import server_v2

        self.assertIsInstance(server_v2.rail_provider, RailBusProvider)
        self.assertIsInstance(server_v2.rail_provider.timetable_provider, TransitousProvider)
        with mock.patch.dict(os.environ):
            os.environ.pop(tp.ENV_FLAG, None)
            health = server_v2.rail_provider.sources_health()["transitous"]
        self.assertFalse(health["enabled"])
        self.assertEqual(self.net.calls, [])


class FixtureSearchTests(_ProviderCase):
    """The real capture: Milano Centrale to Venezia S. Lucia on 2026-11-05 (local time CET, +01:00)."""

    def test_one_segment_per_journey_in_departure_order(self):
        segs = self._search_fixture_day()
        self.assertEqual([s.departure_time for s in segs],
                         ["07:14", "07:44", "08:14", "08:44", "08:54", "09:44"])
        self.assertEqual([s.arrival_time for s in segs],
                         ["09:45", "10:15", "10:45", "11:15", "11:45", "12:15"])
        self.assertEqual([s.duration_mins for s in segs], [151, 151, 151, 151, 171, 151])

    def test_segment_fields_follow_the_contract(self):
        for seg in self._search_fixture_day():
            self.assertFalse(seg.is_estimated)
            self.assertFalse(seg.price_is_estimated)
            self.assertIsNone(seg.price_try)
            self.assertIsNone(seg.price_eur)
            self.assertEqual(seg.source, "transitous")
            self.assertEqual(seg.date, _DAY)
            self.assertEqual((seg.origin_station, seg.dest_station), ("Milano", "Venezia"))

    def test_modes_operators_and_station_names(self):
        segs = self._search_fixture_day()
        self.assertEqual([s.mode for s in segs], ["train"] * 6)
        self.assertEqual([s.operator for s in segs],
                         ["TRENITALIA"] * 4 + ["Trenord / TRENITALIA", "TRENITALIA"])
        self.assertEqual(
            [(s.origin_station_name, s.dest_station_name) for s in segs],
            [("MILANO CENTRALE", "VENEZIA S.LUCIA")] * 4
            + [("Milano Repubblica", "VENEZIA S.LUCIA"), ("MILANO CENTRALE", "VENEZIA S.LUCIA")],
        )

    def test_price_is_unknown(self):
        # The feed has no fares: the price stays unknown ("fiyat yok"), never a distance estimate.
        segs = self._search_fixture_day()
        self.assertTrue(segs)
        self.assertTrue(all(s.price_try is None and not s.price_is_estimated for s in segs))

    def test_earliest_departure_is_inclusive(self):
        self.assertEqual([s.departure_time for s in self._search_fixture_day(earliest_dep_time="08:14")],
                         ["08:14", "08:44", "08:54", "09:44"])
        self.assertEqual([s.departure_time for s in self._search_fixture_day(earliest_dep_time="08:15")],
                         ["08:44", "08:54", "09:44"])

    def test_max_transfers_drops_the_journeys_with_a_change(self):
        # The Mestre change (07:44) and the Milano Repubblica start (07:54) each have one transfer.
        segs = self._search_fixture_day(max_transfers=0)
        self.assertEqual([s.departure_time for s in segs], ["07:14", "08:14", "08:44", "09:44"])
        self.assertEqual(len(self._search_fixture_day(max_transfers=1)), 6)

    def test_max_results_caps_after_sorting(self):
        segs = self._search_fixture_day(max_results=2)
        self.assertEqual([s.departure_time for s in segs], ["07:14", "07:44"])
        self.assertEqual(self._search_fixture_day(max_results=0), [])

    def test_returned_segments_do_not_alias_the_cache(self):
        first = self._search_fixture_day()
        first[0].source = "changed"
        second = self._search_fixture_day()
        self.assertEqual(second[0].source, "transitous")
        self.assertEqual(len(self.net.plan_calls()), 1)

    def test_request_uses_coordinates_modes_and_identifies_the_client(self):
        self._search_fixture_day(earliest_dep_time="08:00")
        (request,) = self.net.plan_calls()
        query = _query(request)
        self.assertEqual(query["fromPlace"], "45.464200,9.190000")
        self.assertEqual(query["toPlace"], "45.440800,12.315500")
        self.assertEqual(query["transitModes"], "RAIL,BUS,COACH")
        self.assertEqual(query["maxTransfers"], "2")
        self.assertEqual(query["numItineraries"], "6")
        self.assertEqual(query["time"], "2026-11-05T07:00:00+01:00")  # 08:00 minus the one-hour margin
        self.assertEqual(request.get_header("User-agent"), tp.DEFAULT_USER_AGENT)
        self.assertEqual(self.net.geocode_calls(), [])  # both places had coordinates

    def test_place_without_coordinates_is_geocoded_once(self):
        segs = self.provider.search("Milano", "Zzyzx Stop", _DAY, from_coord=MILANO)
        self.assertIsNotNone(segs)
        self.assertEqual(len(self.net.geocode_calls()), 1)
        self.assertEqual(_query(self.net.geocode_calls()[0])["text"], "Zzyzx Stop")
        # No city centre is known, so the first PLACE result is the reference; the nearest rail STOP
        # within 15 km of it is Venezia S. Lucia (the station), not the bus stop or Mestre.
        self.assertEqual(_query(self.net.plan_calls()[0])["toPlace"], "45.441466,12.320887")

    def test_city_centre_origin_returns_no_journeys_live(self):
        # Live response (2026-10-19) for Milano Duomo to Venezia city centre: the empty result that
        # led to the station anchors. Not an error.
        self.net.plan_body = _fixture("plan_capture_milano_venezia_2026-11-05.json")
        self.assertEqual(self._search_fixture_day(), [])
        health = self.provider.health_status()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["requests"], 1)

    def test_same_coordinates_on_both_sides_is_empty(self):
        self.assertEqual(self.provider.search("Milano", "Milano", _DAY, from_coord=MILANO, to_coord=MILANO), [])

    def test_geocoded_place_sets_the_request_offset_from_its_zone(self):
        self.provider.search("Zzyzx Stop", "Venezia", _DAY, to_coord=VENEZIA)
        self.assertEqual(_query(self.net.plan_calls()[0])["time"], "2026-11-05T00:00:00+01:00")


class StationAnchorTests(_ProviderCase):
    """Known cities are searched from their main station, other places from the city centre."""

    def test_known_city_is_searched_from_its_station(self):
        segs = self.provider.search("Milano", "Venezia", _DAY, earliest_dep_time="08:00")
        self.assertEqual(self.net.geocode_calls(), [])
        (request,) = self.net.plan_calls()
        query = _query(request)
        # The exact request that returned four journeys live (2026-10-19), after the 08:00 earliest time
        self.assertEqual(query["fromPlace"], "45.486200,9.204400")
        self.assertEqual(query["toPlace"], "45.441000,12.321000")
        self.assertEqual(query["time"], "2026-11-05T07:00:00+01:00")
        self.assertEqual([s.departure_time for s in segs], ["08:14", "08:44", "08:54", "09:44"])

    def test_names_are_matched_loosely_and_in_both_languages(self):
        for name in ("MILANO", "  Milano  ", "Milan", "milan"):
            place = self.provider._resolve_place(name, None)
            self.assertEqual((place.lat, place.lon), (45.4862, 9.2044), name)
        self.assertEqual(self.provider._resolve_place("Venice", None).lat, 45.4410)
        self.assertEqual(self.net.geocode_calls(), [])

    def test_station_anchor_carries_its_zone(self):
        place = self.provider._resolve_place("Prague", None)
        self.assertEqual((place.lat, place.lon, place.tz), (50.0830, 14.4350, "Europe/Prague"))
        self.assertTrue(self.provider.can_resolve("Praha"))
        self.assertEqual(self.net.geocode_calls(), [])

    def test_request_offset_follows_the_zone_of_the_station_in_summer(self):
        hub = tp._hub_place("Praha")
        self.assertEqual(tp._request_time(date(2026, 7, 5), 8 * 60, hub), "2026-07-05T07:00:00+02:00")
        self.assertEqual(tp._request_time(date(2026, 11, 5), 8 * 60, hub), "2026-11-05T07:00:00+01:00")

    def test_city_without_a_station_near_its_centre_uses_the_city_centre(self):
        # The geocode fixture (Venezia) has no station within 15 km of Amsterdam.
        place = self.provider._resolve_place("Amsterdam", None)
        self.assertEqual((place.lat, place.lon), (52.3731, 4.8926))
        self.assertEqual(len(self.net.geocode_calls()), 1)

    def test_every_anchor_has_a_known_zone_and_a_european_position(self):
        from zoneinfo import ZoneInfo

        for name, (lat, lon, tz) in tp.HUB_STATIONS.items():
            with self.subTest(name=name):
                ZoneInfo(tz)
                self.assertTrue(35.0 <= lat <= 60.0 and -10.0 <= lon <= 30.0)


def _geo(name, lat, lon, kind="STOP", modes=None, tz="Europe/Amsterdam"):
    item = {"type": kind, "name": name, "lat": lat, "lon": lon, "tz": tz}
    if modes is not None:
        item["modes"] = modes
    return item


# Synthetic geocode answer for a city without a HUB_STATIONS entry (Amsterdam centre 52.3731, 4.8926).
AMSTERDAM_HITS = [
    _geo("Amsterdam", 52.3731, 4.8926, kind="PLACE"),
    _geo("Bus Dam", 52.3730, 4.8930, modes=["BUS"]),                       # nearest, but a bus stop
    _geo("Amsterdam Centraal", 52.3791, 4.9003, modes=["HIGHSPEED_RAIL", "REGIONAL_RAIL"]),
    _geo("Schiphol Airport", 52.3105, 4.7683, modes=["REGIONAL_RAIL"]),     # rail, but further out
    _geo("Far Stop", 53.5000, 5.0000, modes=["REGIONAL_RAIL"]),             # beyond 15 km
]


class GeocodeAnchorTests(_ProviderCase):
    """Places without a HUB_STATIONS entry: the station is a geocode STOP near the city centre."""

    def _resolve(self, name="Amsterdam", hits=AMSTERDAM_HITS):
        self.net.geocode_body = json.dumps(hits).encode("utf-8")
        return self.provider._resolve_place(name, None)

    def test_rail_station_near_the_centre_beats_a_nearer_bus_stop(self):
        place = self._resolve()
        self.assertEqual((place.lat, place.lon, place.tz), (52.3791, 4.9003, "Europe/Amsterdam"))
        self.assertEqual(len(self.net.geocode_calls()), 1)

    def test_station_name_is_enough_without_a_stop_type(self):
        hits = [_geo("Bahnhof Süd", 52.3600, 4.9000, kind="PLACE")]
        place = self._resolve(hits=hits)
        self.assertEqual((place.lat, place.lon), (52.3600, 4.9000))

    def test_no_station_within_the_radius_uses_the_city_centre(self):
        place = self._resolve(hits=[_geo("Far Stop", 53.5000, 5.0000, modes=["REGIONAL_RAIL"])])
        self.assertEqual((place.lat, place.lon), (52.3731, 4.8926))

    def test_anchor_is_cached_for_thirty_days(self):
        self._resolve()
        self.provider._resolve_place("amsterdam", None)
        self.assertEqual(len(self.net.geocode_calls()), 1)
        with mock.patch.object(tp, "_wall", return_value=time.time() + 29 * 24 * 3600):
            self.provider._resolve_place("Amsterdam", None)
        self.assertEqual(len(self.net.geocode_calls()), 1)
        with mock.patch.object(tp, "_wall", return_value=time.time() + 31 * 24 * 3600), \
                mock.patch.object(tp, "_now", return_value=time.monotonic() + 31 * 24 * 3600):
            self.provider._resolve_place("Amsterdam", None)
        self.assertEqual(len(self.net.geocode_calls()), 2)

    def test_geocode_failure_falls_back_to_the_centre_and_is_not_cached(self):
        self.net.geocode_body = b'{"not": "a list"}'
        place = self.provider._resolve_place("Amsterdam", None)
        self.assertEqual((place.lat, place.lon), (52.3731, 4.8926))
        self.assertEqual(self.provider.health_status()["last_error"], "geocode response is not a list")
        self._resolve()  # the next lookup asks again and gets the station
        self.assertEqual(len(self.net.geocode_calls()), 2)

    def test_anchor_is_searched_from_and_reported_in_the_plan_request(self):
        self._resolve()
        self.net.plan_body = _fixture("plan_milano_venezia_2026-11-05.json")
        self.provider.search("Amsterdam", "Venezia", _DAY, to_coord=VENEZIA)
        self.assertEqual(_query(self.net.plan_calls()[0])["fromPlace"], "52.379100,4.900300")

    def test_anchor_file_is_read_by_a_new_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "anchors.json")
            self.provider = self._make(anchor_cache_path=path)
            self._resolve()
            with open(path, encoding="utf-8") as f:
                stored = json.load(f)["anchors"]
            self.assertEqual(stored["amsterdam"]["lat"], 52.3791)
            restarted = self._make(anchor_cache_path=path)
            place = restarted._resolve_place("Amsterdam", None)
            self.assertEqual((place.lat, place.lon), (52.3791, 4.9003))
            self.assertEqual(len(self.net.geocode_calls()), 1)

    def test_expired_and_broken_anchor_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "anchors.json")
            old = time.time() - 31 * 24 * 3600
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "anchors": {"amsterdam": {
                    "at": old, "lat": 1.0, "lon": 2.0, "tz": None}}}, f)
            self.provider = self._make(anchor_cache_path=path)
            self._resolve()
            self.assertEqual(len(self.net.geocode_calls()), 1)  # the stale entry was not used
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            broken = self._make(anchor_cache_path=path)
            place = broken._resolve_place("Amsterdam", None)
            self.assertEqual(len(self.net.geocode_calls()), 2)
            self.assertEqual((place.lat, place.lon), (52.3791, 4.9003))

    def test_unwritable_anchor_file_does_not_fail_the_lookup(self):
        with tempfile.TemporaryDirectory() as folder:
            self.provider = self._make(anchor_cache_path=os.path.join(folder, "missing", "anchors.json"))
            place = self._resolve()
            self.assertEqual((place.lat, place.lon), (52.3791, 4.9003))

    def test_pick_anchor_ranks_rail_then_distance(self):
        centre = (52.3731, 4.8926)
        hits = [tp._Hit(*c) for c in [
            (52.3730, 4.8930, "Europe/Amsterdam", "STOP", "Bus Dam", ("BUS",)),
            (52.3791, 4.9003, "Europe/Amsterdam", "STOP", "Amsterdam Centraal", ("HIGHSPEED_RAIL",)),
            (52.3100, 4.7700, "Europe/Amsterdam", "PLACE", "Schiphol", ()),  # not a STOP, no station name
        ]]
        self.assertEqual(tp._pick_anchor(hits, centre).name, "Amsterdam Centraal")
        self.assertIsNone(tp._pick_anchor(hits[2:], centre))


class TimeConversionTests(unittest.TestCase):
    """UTC instants to local clock times, zone by zone, DST switch included."""

    @staticmethod
    def _utc(*args):
        return datetime(*args, tzinfo=timezone.utc)

    def test_utc_to_the_zone_of_the_stop(self):
        local = tp._local_time(self._utc(2026, 11, 5, 7, 10), {"tz": "Europe/Rome"})
        self.assertEqual((local.hour, local.minute), (8, 10))
        self.assertEqual(local.utcoffset(), timedelta(hours=1))

    def test_dst_switch_day_in_the_stop_zone(self):
        before = tp._local_time(self._utc(2026, 10, 24, 7, 0), {"tz": "Europe/Rome"})
        self.assertEqual((before.hour, before.utcoffset()), (9, timedelta(hours=2)))
        after = tp._local_time(self._utc(2026, 10, 25, 6, 30), {"tz": "Europe/Rome"})
        self.assertEqual((after.hour, after.minute, after.utcoffset()), (7, 30, timedelta(hours=1)))

    def test_repeated_hour_on_the_switch_night(self):
        first = tp._local_time(self._utc(2026, 10, 25, 0, 30), {"tz": "Europe/Rome"})
        second = tp._local_time(self._utc(2026, 10, 25, 1, 30), {"tz": "Europe/Rome"})
        self.assertEqual((first.hour, first.minute, first.utcoffset()), (2, 30, timedelta(hours=2)))
        self.assertEqual((second.hour, second.minute, second.utcoffset()), (2, 30, timedelta(hours=1)))

    def test_missing_or_unknown_zone_falls_back_to_a_guess(self):
        cet = tp._local_time(self._utc(2026, 11, 5, 7, 10), {"lat": 45.46, "lon": 9.19})
        self.assertEqual(cet.utcoffset(), timedelta(hours=1))
        bad = tp._local_time(self._utc(2026, 11, 5, 7, 10), {"tz": "Mars/Olympus", "lat": 45.46, "lon": 9.19})
        self.assertEqual(bad.utcoffset(), timedelta(hours=1))

    def test_guessed_offsets_by_region(self):
        nov, jul = date(2026, 11, 5), date(2026, 7, 15)
        self.assertEqual(tp._guess_offset(45.46, 9.19, nov), timedelta(hours=1))   # Milano, CET
        self.assertEqual(tp._guess_offset(45.46, 9.19, jul), timedelta(hours=2))   # Milano, CEST
        self.assertEqual(tp._guess_offset(51.51, -0.13, jul), timedelta(hours=1))  # London, BST
        self.assertEqual(tp._guess_offset(51.51, -0.13, nov), timedelta(hours=0))  # London, GMT
        self.assertEqual(tp._guess_offset(38.72, -9.14, jul), timedelta(hours=1))  # Lisbon, WEST
        self.assertEqual(tp._guess_offset(37.98, 23.73, nov), timedelta(hours=2))  # Athens, EET
        self.assertEqual(tp._guess_offset(37.98, 23.73, jul), timedelta(hours=3))  # Athens, EEST
        self.assertEqual(tp._guess_offset(41.01, 28.98, jul), timedelta(hours=3))  # Istanbul, +03 all year
        self.assertEqual(tp._guess_offset(41.01, 28.98, nov), timedelta(hours=3))

    def test_eu_summer_time_boundaries_2026(self):
        self.assertEqual(tp._last_sunday(2026, 3), date(2026, 3, 29))
        self.assertEqual(tp._last_sunday(2026, 10), date(2026, 10, 25))
        self.assertTrue(tp._eu_summer_time(date(2026, 10, 24)))
        self.assertFalse(tp._eu_summer_time(date(2026, 10, 25)))
        self.assertFalse(tp._eu_summer_time(date(2026, 3, 28)))
        self.assertTrue(tp._eu_summer_time(date(2026, 3, 29)))

    def test_request_start_follows_the_zone_across_the_switch(self):
        rome = tp._Place(45.46, 9.19, "Europe/Rome")
        self.assertEqual(tp._request_time(date(2026, 10, 24), 480, rome), "2026-10-24T07:00:00+02:00")
        self.assertEqual(tp._request_time(date(2026, 10, 25), 480, rome), "2026-10-25T07:00:00+01:00")
        self.assertEqual(tp._request_time(date(2026, 11, 5), None, rome), "2026-11-05T00:00:00+01:00")
        guessed = tp._Place(45.46, 9.19, None)
        self.assertEqual(tp._request_time(date(2026, 11, 5), 0, guessed), "2026-11-05T00:00:00+01:00")


class JourneyParsingTests(_ProviderCase):
    """Itineraries built in code, parsed through the same helpers as the search."""

    day = date(2026, 11, 5)

    def _journey(self, itinerary, max_transfers=2):
        return tp._journey(itinerary, self.day, max_transfers)

    def test_arrival_after_midnight_keeps_the_departure_date(self):
        itinerary = _train_direct("2026-11-05T21:30:00Z", "2026-11-06T00:15:00Z")
        journey = self._journey(itinerary)
        self.assertEqual((journey.departure_time, journey.arrival_time), ("22:30", "01:15"))
        self.assertEqual(journey.duration_mins, 165)

    def test_departure_on_another_local_day_is_skipped(self):
        # Local 23:30 on Nov 4 (the day before) and local 00:30 on Nov 6 (the day after)
        self.assertIsNone(self._journey(_train_direct("2026-11-04T22:30:00Z", "2026-11-05T00:40:00Z")))
        self.assertIsNone(self._journey(_train_direct("2026-11-05T23:30:00Z", "2026-11-06T01:40:00Z")))

    def test_transfer_count_from_the_key_or_from_the_legs(self):
        four_legs = [
            _leg("REGIONAL_RAIL", MI_C, VR_PN, "2026-11-05T07:00:00Z", "2026-11-05T08:00:00Z", 150000),
            _leg("REGIONAL_RAIL", VR_PN, VE_SL, "2026-11-05T08:10:00Z", "2026-11-05T09:00:00Z", 120000),
            _leg("REGIONAL_RAIL", VE_SL, VE_SL, "2026-11-05T09:10:00Z", "2026-11-05T09:20:00Z", 500),
            _leg("REGIONAL_RAIL", VE_SL, VE_SL, "2026-11-05T09:30:00Z", "2026-11-05T09:40:00Z", 500),
        ]
        no_key = _itinerary(four_legs, "2026-11-05T07:00:00Z", "2026-11-05T09:40:00Z")
        self.assertIsNone(self._journey(no_key, max_transfers=2))  # 3 transfers from the legs
        self.assertIsNotNone(self._journey(no_key, max_transfers=3))
        keyed = _itinerary(four_legs, "2026-11-05T07:00:00Z", "2026-11-05T09:40:00Z", transfers=1)
        self.assertIsNotNone(self._journey(keyed, max_transfers=2))

    def test_walk_only_and_broken_itineraries_are_skipped_without_raising(self):
        walk = _leg("WALK", MI_C, VE_SL, "2026-11-05T07:00:00Z", "2026-11-05T07:20:00Z", 1500)
        self.assertIsNone(self._journey(_itinerary([walk], "2026-11-05T07:00:00Z", "2026-11-05T07:20:00Z")))
        good = _train_direct("2026-11-05T07:00:00Z", "2026-11-05T09:10:00Z")
        broken = [
            {"legs": "not a list", "startTime": "2026-11-05T07:00:00Z", "endTime": "2026-11-05T09:00:00Z"},
            _itinerary(good["legs"], "2026-11-05T09:10:00Z", "2026-11-05T07:00:00Z"),  # ends before start
            _itinerary(good["legs"], "2026-11-05 07:00", "2026-11-05T09:10:00Z"),       # no offset or Z
            _itinerary(good["legs"], 1234, "2026-11-05T09:10:00Z"),
            "a string, not an object",
            None,
        ]
        payload = {"itineraries": broken + [good]}
        journeys = tp._parse_plan(payload, self.day, 2)
        self.assertEqual([j.departure_time for j in journeys], ["08:00"])

    def test_operator_lists_distinct_agencies_up_to_three(self):
        legs = [
            _leg("REGIONAL_RAIL", MI_C, VR_PN, "2026-11-05T07:00:00Z", "2026-11-05T08:00:00Z", 150000, agency="Trenitalia"),
            _leg("REGIONAL_RAIL", VR_PN, VE_SL, "2026-11-05T08:10:00Z", "2026-11-05T09:00:00Z", 120000, agency="Trenitalia"),
            _leg("COACH", VE_SL, VE_SL, "2026-11-05T09:10:00Z", "2026-11-05T09:20:00Z", 60000, agency="FlixBus"),
            _leg("BUS", VE_SL, VE_SL, "2026-11-05T09:30:00Z", "2026-11-05T09:40:00Z", 60000, route="X9"),
            _leg("BUS", VE_SL, VE_SL, "2026-11-05T09:50:00Z", "2026-11-05T10:00:00Z", 60000, agency="Other"),
        ]
        journey = self._journey(_itinerary(legs, "2026-11-05T07:00:00Z", "2026-11-05T10:00:00Z"), max_transfers=4)
        self.assertEqual(journey.operator, "Trenitalia / FlixBus / X9")

    def test_mode_is_bus_only_when_every_transit_leg_is_bus_or_coach(self):
        coach = _leg("COACH", MI_C, VE_SL, "2026-11-05T07:00:00Z", "2026-11-05T11:00:00Z", 260000, agency="FlixBus")
        mixed_legs = [coach, _leg("HIGHSPEED_RAIL", VE_SL, VE_SL, "2026-11-05T11:10:00Z",
                                  "2026-11-05T11:30:00Z", 30000)]
        self.assertEqual(self._journey(_itinerary([coach], "2026-11-05T07:00:00Z", "2026-11-05T11:00:00Z")).mode, "bus")
        self.assertEqual(self._journey(_itinerary(mixed_legs, "2026-11-05T07:00:00Z", "2026-11-05T11:30:00Z"),
                                       max_transfers=1).mode, "train")

    def test_transit_distance_uses_the_leg_distance_before_the_straight_line(self):
        short = _leg("REGIONAL_RAIL", MI_C, VE_SL, "2026-11-05T07:00:00Z", "2026-11-05T07:40:00Z", distance=12000)
        self.assertIsNone(self._journey(_itinerary([short], "2026-11-05T07:00:00Z", "2026-11-05T07:40:00Z")))
        no_distance = _leg("REGIONAL_RAIL", MI_C, VE_SL, "2026-11-05T07:00:00Z", "2026-11-05T09:00:00Z")
        self.assertIsNotNone(self._journey(_itinerary([no_distance], "2026-11-05T07:00:00Z", "2026-11-05T09:00:00Z")))


class ParseFailureTests(_ProviderCase):
    def test_unexpected_plan_shape_returns_none_and_records_an_error(self):
        self.net.plan_body = json.dumps({"error": "nope"}).encode("utf-8")
        self.assertIsNone(self._search_fixture_day())
        health = self.provider.health_status()
        self.assertEqual(health["status"], "error")
        self.assertIn("unexpected plan response", health["last_error"])

    def test_invalid_json_returns_none(self):
        self.net.plan_body = b"<html>maintenance</html>"
        self.assertIsNone(self._search_fixture_day())
        self.assertEqual(self.provider.health_status()["last_error"], "response is not JSON")

    def test_malformed_input_makes_no_request(self):
        self.assertIsNone(self.provider.search("Milano", "Venezia", "2026-13-40", from_coord=MILANO, to_coord=VENEZIA))
        self.assertIsNone(self.provider.search("Milano", "Venezia", _DAY, "25:00", from_coord=MILANO, to_coord=VENEZIA))
        self.assertIsNone(self.provider.search("", "Venezia", _DAY, from_coord=MILANO, to_coord=VENEZIA))
        self.assertEqual(self.net.calls, [])

    def test_unexpected_exception_never_escapes(self):
        with mock.patch.object(tp, "_segments", side_effect=RuntimeError("boom")):
            self.assertIsNone(self._search_fixture_day())
        self.assertIn("unexpected error", self.provider.health_status()["last_error"])

    def test_geocode_must_return_a_list(self):
        self.net.geocode_body = b'{"not": "a list"}'
        self.assertFalse(self.provider.can_resolve("Zzyzx Stop"))
        self.assertIsNone(self.provider.search("Zzyzx Stop", "Venezia", _DAY, to_coord=VENEZIA))
        self.assertEqual(self.provider.health_status()["last_error"], "geocode response is not a list")

    def test_geocode_without_hits_is_cached_as_unknown(self):
        self.net.geocode_body = b"[]"
        self.assertFalse(self.provider.can_resolve("Nowhere Special"))
        self.assertFalse(self.provider.can_resolve("Nowhere Special"))
        self.assertEqual(len(self.net.geocode_calls()), 1)


class ResilienceTests(_ProviderCase):
    def test_http_429_blocks_the_provider_for_a_cooldown(self):
        self.net.status = 429
        self.assertIsNone(self._search_fixture_day())
        health = self.provider.health_status()
        self.assertEqual(health["status"], "blocked")
        self.assertTrue(health["blocked_cooldown_active"])
        self.assertEqual(health["last_http_status"], 429)
        requests_before = len(self.net.calls)
        self.assertIsNone(self._search_fixture_day(earliest_dep_time="09:00"))
        self.assertEqual(len(self.net.calls), requests_before)

    def test_cooldown_ends_after_fifteen_minutes(self):
        self.net.status = 429
        self._search_fixture_day()
        self.net.status = None
        with mock.patch.object(tp, "_now", return_value=time.monotonic() + tp.BLOCK_COOLDOWN_SECONDS + 1):
            self.assertIsNotNone(self._search_fixture_day(earliest_dep_time="09:00"))

    def test_http_500_is_an_error_not_a_block(self):
        self.net.status = 500
        self.assertIsNone(self._search_fixture_day())
        health = self.provider.health_status()
        self.assertEqual((health["status"], health["last_http_status"]), ("error", 500))
        self.assertFalse(health["blocked_cooldown_active"])

    def test_network_error_is_recorded(self):
        self.net.error = urllib.error.URLError("timed out")
        self.assertIsNone(self._search_fixture_day())
        self.assertTrue(self.provider.health_status()["last_error"].startswith("network error"))

    def test_successful_call_clears_the_error_state(self):
        self.net.status = 500
        self._search_fixture_day()
        self.net.status = None
        self.assertIsNotNone(self._search_fixture_day(earliest_dep_time="09:00"))
        health = self.provider.health_status()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["consecutive_failures"], 0)


class CacheAndRateTests(_ProviderCase):
    def test_journeys_are_cached_for_the_ttl(self):
        self._search_fixture_day()
        self._search_fixture_day()
        self.assertEqual(len(self.net.plan_calls()), 1)
        with mock.patch.object(tp, "_now", return_value=time.monotonic() + self.provider.cache_ttl + 1):
            self._search_fixture_day()
        self.assertEqual(len(self.net.plan_calls()), 2)

    def test_geocoding_is_cached_for_seven_days(self):
        self.provider._geocode_hits("Zzyzx Stop")
        self.provider._geocode_hits("zzyzx   stop")
        self.assertEqual(len(self.net.geocode_calls()), 1)
        with mock.patch.object(tp, "_now", return_value=time.monotonic() + tp.GEOCODE_TTL_SECONDS + 10):
            self.provider._geocode_hits("Zzyzx Stop")
        self.assertEqual(len(self.net.geocode_calls()), 2)

    def test_requests_are_spaced_by_the_minimum_interval(self):
        slept = []
        provider = self._make(min_interval_secs=1.0)
        with mock.patch.object(tp, "_limiter", tp._RateLimiter()), \
                mock.patch.object(tp, "_sleep", side_effect=slept.append), \
                mock.patch.object(tp, "_now", return_value=100.0):
            provider.search("Milano", "Venezia", _DAY, from_coord=MILANO, to_coord=VENEZIA)
            provider.search("Milano", "Venezia", _DAY, earliest_dep_time="09:00",
                            from_coord=MILANO, to_coord=VENEZIA)
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 1.0)

    def test_cache_stays_bounded(self):
        with mock.patch.object(tp, "MAX_CACHE_ENTRIES", 2):
            provider = self._make()
            for minute in ("08:00", "08:30", "09:00"):
                provider.search("Milano", "Venezia", _DAY, earliest_dep_time=minute,
                                from_coord=MILANO, to_coord=VENEZIA)
        self.assertLessEqual(len(provider._plan_cache), 2)


class HealthTests(_ProviderCase):
    def test_fresh_enabled_provider_is_unknown(self):
        health = self.provider.health_status()
        self.assertEqual((health["enabled"], health["status"], health["requests"]), (True, "unknown", 0))

    def test_ok_after_a_successful_search(self):
        self._search_fixture_day()
        health = self.provider.health_status()
        self.assertEqual(health["status"], "ok")
        self.assertIsNotNone(health["last_ok_at"])
        self.assertEqual(health["cached_searches"], 1)

    def test_health_is_json_safe(self):
        self._search_fixture_day()
        json.dumps(self.provider.health_status(), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
