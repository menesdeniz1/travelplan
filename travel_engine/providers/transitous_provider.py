"""Transitous timetables: real train and bus journeys in Europe, opt-in and off by default.

Transitous (https://transitous.org) is a community-run public transit router built on MOTIS. Two of
its public GET endpoints are used:

* ``/api/v1/plan`` returns journeys between two coordinates for one start time, limited to rail and
  bus/coach (``transitModes=RAIL,BUS,COACH``) with ``maxTransfers`` and ``numItineraries`` set.
  Times are UTC (``startTime``/``endTime``); each stop carries its IANA time zone (``tz``).
* ``/api/v1/geocode`` resolves a place name to coordinates and a time zone. It is used to find a station
  anchor for places without a ``HUB_STATIONS`` entry (see ``TransitousProvider._anchor``).

Why station anchors: the city centre can be 2-3 km from the station that has the long-distance trains.
Milano Duomo is 2.7 km from Milano Centrale, and the plan request from the Duomo returned no itineraries
(live, 2026-10-19), while the same request from Milano Centrale returned four. The walk to the first stop
is too long for the router. The search therefore starts at a station: the ``HUB_STATIONS`` entry when
the city has one, else the best geocode STOP within ``ANCHOR_RADIUS_KM`` of the city centre (anchors are
cached for ``ANCHOR_TTL_SECONDS``, in memory and in an optional JSON file), else the city centre.

The provider does nothing until the environment variable ``TRAVELPLAN_TRANSITOUS=1`` is set
(``is_enabled``). While disabled, ``can_resolve`` is False and ``search`` returns None without any
network call. The Transitous usage policy asks for an identifying User-Agent, light load and no
commercial use. Requests therefore carry ``DEFAULT_USER_AGENT``, are spaced by ``min_interval_secs``
(process-wide) and cached (``cache_ttl`` for journeys, 7 days for geocoding). The /plan endpoint is
resource intensive, so the owner contacts the Transitous team before enabling it.

Segments: one ``ScheduledSegment`` per journey covering the whole trip (walk legs included). Times are
converted from UTC to the local time zone of the first and last stop. ``is_estimated`` is False (real
timetable) but fares are not in the feed, so ``price_try`` is None (unknown, shown as "fiyat yok"); ``price_is_estimated`` is False and ``price_eur`` None.

Nothing here raises. Failures make ``search`` return None and are reported by ``health_status``.
"""

import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from travel_engine.airports import AirportDB, haversine_km
from travel_engine.providers.rail_bus_provider import ScheduledSegment

logger = logging.getLogger(__name__)

SOURCE = "transitous"
ENV_FLAG = "TRAVELPLAN_TRANSITOUS"
API_BASE = "https://api.transitous.org/api/v1"
PLAN_URL = API_BASE + "/plan"
GEOCODE_URL = API_BASE + "/geocode"
DEFAULT_USER_AGENT = "TravelPlan/1.0 (+https://github.com/menesdeniz1/travelplan; personal non-commercial)"
DEFAULT_CACHE_TTL_SECONDS = 30 * 60
GEOCODE_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_MIN_INTERVAL_SECS = 1.0
BLOCKED_STATUSES = frozenset({429})
BLOCK_COOLDOWN_SECONDS = 15 * 60
TRANSIT_MODES = "RAIL,BUS,COACH"
NUM_ITINERARIES = 6
# Journeys whose transit legs add up to less than this are not worth a ground option.
MIN_TRANSIT_KM = 30.0
# The request starts this long before the wanted local time. The offset of a place is only guessed
# when no time zone is known, and a guess can be an hour off; results are filtered exactly afterwards.
REQUEST_MARGIN = timedelta(hours=1)
MAX_CACHE_ENTRIES = 256
MAX_OPERATORS = 3
NON_TRANSIT_MODES = frozenset({"WALK", "BIKE", "CAR", "RIDE_SHARING"})
BUS_MODES = frozenset({"BUS", "COACH"})
# Main long-distance stations of some large cities, used as the search point instead of the city centre
# (see the module docstring). Coordinates are approximate station positions. Keys are folded names
# (lower case, single spaces). Milano, Venezia, Praha and Wien were checked against live results
# (2026-10-19); Verona, Firenze, Roma and München are station positions from memory.
_HUBS = (
    (("milano", "milan"), 45.4862, 9.2044, "Europe/Rome"),
    (("venezia", "venice"), 45.4410, 12.3210, "Europe/Rome"),
    (("verona",), 45.4288, 10.9868, "Europe/Rome"),
    (("firenze", "florence"), 43.7764, 11.2481, "Europe/Rome"),
    (("roma", "rome"), 41.9009, 12.5024, "Europe/Rome"),
    (("praha", "prague"), 50.0830, 14.4350, "Europe/Prague"),
    (("wien", "vienna"), 48.1850, 16.3760, "Europe/Vienna"),
    (("münchen", "munchen", "munich"), 48.1402, 11.5600, "Europe/Berlin"),
)
HUB_STATIONS: Dict[str, Tuple[float, float, str]] = {
    name: (lat, lon, tz) for names, lat, lon, tz in _HUBS for name in names
}
# Places without a HUB_STATIONS entry: the station anchor is the best geocode STOP (or station-named
# result) within this distance of the city centre. Anchors are kept for 30 days.
ANCHOR_RADIUS_KM = 15.0
ANCHOR_TTL_SECONDS = 30 * 24 * 3600
ANCHOR_FILE_VERSION = 1
# Optional on-disk anchor cache (small JSON). It lives in the OS temp directory, never in the repo.
DEFAULT_ANCHOR_CACHE_PATH = os.path.join(tempfile.gettempdir(), "travelplan_transitous_anchors.json")
# Station words in the names of major stations (English, German, Italian, French, Spanish, Hungarian, Dutch).
_STATION_NAME = re.compile(
    r"(?<![^\W\d_])(centrale|central|centraal|hbf|hl\.\s?n\.|hauptbahnhof|bahnhof|gare|station|stazione"
    r"|estaci[oó]n|keleti|nyugati|termini)(?![^\W\d_])",
    re.IGNORECASE,
)
# Modes that mark a rail station (used to prefer the station over a bus stop of the same city).
RAIL_MODES = frozenset({"HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "REGIONAL_RAIL", "RAIL", "SUBURBAN"})

# Monotonic clock and sleep, module level so tests can replace them.
_now = time.monotonic
_sleep = time.sleep
# Wall clock: anchor timestamps are stored in the JSON file, so they must survive a restart.
_wall = time.time


def is_enabled() -> bool:
    """True only when ``TRAVELPLAN_TRANSITOUS`` is exactly ``1``. Read on every call, default off."""
    return os.environ.get(ENV_FLAG, "").strip() == "1"


class _Failure(Exception):
    """A request or response problem that has already been recorded in the health state."""

    def __init__(self, status: Optional[int], message: str):
        super().__init__(message)
        self.status = status


class _RateLimiter:
    """Process-wide spacing between Transitous requests."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last: Optional[float] = None

    def reserve(self, min_interval: float) -> None:
        with self.lock:
            now = _now()
            wait = 0.0
            if self.last is not None and min_interval > 0:
                wait = max(0.0, self.last + min_interval - now)
            self.last = now + wait
        if wait > 0:
            _sleep(wait)


_limiter = _RateLimiter()


@dataclass(frozen=True)
class _Place:
    lat: float
    lon: float
    tz: Optional[str]  # IANA name when the place came from geocoding


@dataclass(frozen=True)
class _Hit:
    """One geocode result with coordinates. ``kind`` is the result type (STOP, PLACE, ...), upper case."""
    lat: float
    lon: float
    tz: Optional[str]
    kind: str
    name: str
    modes: Tuple[str, ...]


@dataclass(frozen=True)
class _Journey:
    """One itinerary of a plan response, reduced to what a ScheduledSegment needs."""
    dep_mins: int              # local minutes after midnight at the origin stop
    departure_time: str        # "HH:MM" local, origin stop
    arrival_time: str          # "HH:MM" local, destination stop
    duration_mins: int
    mode: str                  # "train" or "bus"
    operator: str
    origin_station_name: str
    dest_station_name: str


# -- pure helpers --------------------------------------------------------------

def _num(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _zone(name: object) -> Optional[ZoneInfo]:
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except (ValueError, KeyError, OSError):  # unknown name, or no tz database on this machine
        return None


def _minutes(hhmm: str) -> int:
    """'HH:MM' -> minutes after midnight. Raises ValueError for anything else."""
    parts = str(hhmm).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"bad time {hhmm!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time {hhmm!r}")
    return h * 60 + m


def _last_sunday(year: int, month: int) -> date:
    first_of_next = date(year + (month == 12), month % 12 + 1, 1)
    last = first_of_next - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 6) % 7)


def _eu_summer_time(day: date) -> bool:
    """EU summer time by date: last Sunday of March to last Sunday of October (switch hour ignored)."""
    return _last_sunday(day.year, 3) <= day < _last_sunday(day.year, 10)


def _guess_offset(lat: Optional[float], lon: Optional[float], day: date) -> timedelta:
    """UTC offset of a place on a day, used only when no IANA zone is known.

    Turkey (east of 26E, south of 42.5N) is +03 all year. Elsewhere the zone follows longitude:
    the UK, Ireland and Portugal (west of 5W, or west of Greenwich in the north) are WET, up to 22.5E
    CET, further east EET; EU summer time is added to all but Turkey. Errors of one hour are possible
    near borders (the request margin covers them).
    """
    if lat is not None and lon is not None and lon >= 26.0 and lat < 42.5:
        return timedelta(hours=3)
    if lon is None:
        standard = 1
    elif lon < -5.0 or (lon < 0.0 and lat is not None and lat > 49.0):
        standard = 0
    elif lon < 22.5:
        standard = 1
    else:
        standard = 2
    if _eu_summer_time(day):
        standard += 1
    return timedelta(hours=standard)


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("time is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("time has no offset")
    return parsed.astimezone(timezone.utc)


def _local_time(utc: datetime, stop: dict) -> datetime:
    """The UTC instant in the local time of a stop (its ``tz``, else a guess from its coordinates)."""
    zone = _zone(stop.get("tz"))
    if zone is not None:
        return utc.astimezone(zone)
    offset = _guess_offset(_num(stop.get("lat")), _num(stop.get("lon")), utc.date())
    return utc.astimezone(timezone(offset))


def _fold(name: str) -> str:
    """Cache key of a place name: case and spacing ignored."""
    return " ".join(str(name).casefold().split())


def _hub_place(name: str) -> Optional[_Place]:
    """The station anchor of a city (``HUB_STATIONS``), or None. Case and spacing are ignored."""
    hit = HUB_STATIONS.get(_fold(name))
    if hit is None:
        return None
    return _Place(hit[0], hit[1], hit[2])


def _is_station_name(name: str) -> bool:
    return _STATION_NAME.search(name or "") is not None


def _pick_anchor(hits: List[_Hit], reference: Tuple[float, float]) -> Optional[_Hit]:
    """The station to search from: STOP or station-named results within ``ANCHOR_RADIUS_KM`` of the
    reference point. Rail stations (station name or a rail mode) rank first, then the nearest one."""
    best: Optional[_Hit] = None
    best_key: Optional[Tuple[int, float]] = None
    for hit in hits:
        if hit.kind != "STOP" and not _is_station_name(hit.name):
            continue
        distance = haversine_km(reference[0], reference[1], hit.lat, hit.lon)
        if distance > ANCHOR_RADIUS_KM:
            continue
        rail = _is_station_name(hit.name) or any(mode in RAIL_MODES for mode in hit.modes)
        key = (0 if rail else 1, distance)
        if best_key is None or key < best_key:
            best, best_key = hit, key
    return best


def _reference_point(hits: List[_Hit]) -> Optional[Tuple[float, float]]:
    """Where the city is, when no city centre is known: its first PLACE result, else the first result."""
    for hit in hits:
        if hit.kind == "PLACE":
            return (hit.lat, hit.lon)
    return (hits[0].lat, hits[0].lon) if hits else None


def _read_anchor_file(path: str) -> Dict[str, Tuple[float, Optional[float], Optional[float], Optional[str]]]:
    """Anchors stored by an earlier run: folded name -> (stored_at, lat, lon, tz); lat None = no anchor.
    A missing, unreadable or malformed file gives an empty dict."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError, RecursionError):
        return {}
    raw = data.get("anchors") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Tuple[float, Optional[float], Optional[float], Optional[str]]] = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        stored_at, lat, lon = _num(entry.get("at")), _num(entry.get("lat")), _num(entry.get("lon"))
        if stored_at is None or (lat is None) != (lon is None):
            continue
        tz = entry.get("tz") if isinstance(entry.get("tz"), str) else None
        out[key] = (stored_at, lat, lon, tz)
    return out


def _mode(leg: dict) -> str:
    return str(leg.get("mode") or "").strip().upper()


def _leg_km(leg: dict) -> float:
    """Length of a transit leg in km: the leg's own distance (metres) when given, else the straight line."""
    metres = _num(leg.get("distance"))
    if metres is not None and metres > 0:
        return metres / 1000.0
    frm, to = leg.get("from"), leg.get("to")
    if isinstance(frm, dict) and isinstance(to, dict):
        a = (_num(frm.get("lat")), _num(frm.get("lon")))
        b = (_num(to.get("lat")), _num(to.get("lon")))
        if None not in a and None not in b:
            return haversine_km(a[0], a[1], b[0], b[1])
    return 0.0


def _stop_name(stop: dict) -> Optional[str]:
    name = stop.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _operator(transit: List[dict]) -> str:
    """Distinct agency names (or route names where no agency is given), at most MAX_OPERATORS."""
    names: List[str] = []
    for leg in transit:
        raw = leg.get("agencyName") or leg.get("routeShortName")
        name = str(raw).strip() if raw else ""
        if name and name not in names:
            names.append(name)
    return " / ".join(names[:MAX_OPERATORS]) if names else "Transitous"


def _journey(itinerary: object, day: date, max_transfers: int) -> Optional[_Journey]:
    """One itinerary as a journey on ``day``, or None when it is not a usable rail or bus journey."""
    if not isinstance(itinerary, dict) or not isinstance(itinerary.get("legs"), list):
        return None
    legs = itinerary["legs"]
    transit = [leg for leg in legs if isinstance(leg, dict) and _mode(leg) not in NON_TRANSIT_MODES]
    if not transit:
        return None
    try:
        start = _parse_utc(itinerary.get("startTime"))
        end = _parse_utc(itinerary.get("endTime"))
    except (TypeError, ValueError):
        return None
    if end <= start:
        return None

    transfers = itinerary.get("transfers")
    if isinstance(transfers, bool) or not isinstance(transfers, int):
        transfers = len(transit) - 1
    if transfers > max_transfers:
        return None
    if sum(_leg_km(leg) for leg in transit) < MIN_TRANSIT_KM:
        return None

    first_stop, last_stop = transit[0].get("from"), transit[-1].get("to")
    if not isinstance(first_stop, dict) or not isinstance(last_stop, dict):
        return None
    origin_name, dest_name = _stop_name(first_stop), _stop_name(last_stop)
    if origin_name is None or dest_name is None:
        return None

    dep = _local_time(start, first_stop)
    arr = _local_time(end, last_stop)
    if dep.date() != day:
        return None  # the search covers one day; the rollover is find_ground_next's job
    return _Journey(
        dep_mins=dep.hour * 60 + dep.minute,
        departure_time=dep.strftime("%H:%M"),
        arrival_time=arr.strftime("%H:%M"),
        duration_mins=int((end - start).total_seconds() // 60),
        mode="bus" if all(_mode(leg) in BUS_MODES for leg in transit) else "train",
        operator=_operator(transit),
        origin_station_name=origin_name,
        dest_station_name=dest_name,
    )


def _parse_plan(payload: object, day: date, max_transfers: int) -> List[_Journey]:
    """All usable journeys of one plan response. Raises ValueError when the shape is unexpected."""
    if not isinstance(payload, dict) or not isinstance(payload.get("itineraries"), list):
        raise ValueError("plan response has no itineraries list")
    journeys: List[_Journey] = []
    for itinerary in payload["itineraries"]:
        journey = _journey(itinerary, day, max_transfers)
        if journey is not None:
            journeys.append(journey)
    return journeys


def _request_time(day: date, earliest_mins: Optional[int], origin: _Place) -> str:
    """ISO start time with the origin's offset, ``REQUEST_MARGIN`` before the wanted time, not before midnight."""
    midnight = datetime.combine(day, datetime.min.time())
    wanted = midnight + timedelta(minutes=earliest_mins or 0)
    naive = max(midnight, wanted - REQUEST_MARGIN)
    zone = _zone(origin.tz)
    if zone is not None:
        offset = naive.replace(tzinfo=zone).utcoffset()
    else:
        offset = _guess_offset(origin.lat, origin.lon, day)
    return naive.replace(tzinfo=timezone(offset)).isoformat(timespec="seconds")


def _segments(
    journeys: List[_Journey],
    origin_name: str,
    dest_name: str,
    day: date,
    earliest_mins: Optional[int],
    max_results: int,
) -> List[ScheduledSegment]:
    picked = sorted(
        (j for j in journeys if earliest_mins is None or j.dep_mins >= earliest_mins),
        key=lambda j: j.dep_mins,
    )
    return [
        ScheduledSegment(
            origin_station=origin_name,
            dest_station=dest_name,
            departure_time=j.departure_time,
            arrival_time=j.arrival_time,
            duration_mins=j.duration_mins,
            price_try=None,
            operator=j.operator,
            mode=j.mode,
            date=day.isoformat(),
            is_estimated=False,
            price_eur=None,
            price_is_estimated=False,
            source=SOURCE,
            origin_station_name=j.origin_station_name,
            dest_station_name=j.dest_station_name,
        )
        for j in picked[:max(0, max_results)]
    ]


def _remember(cache: Dict, key, value) -> None:
    """Store a cache entry; the oldest entry goes when the cache is full. Caller holds the lock."""
    if key not in cache and len(cache) >= MAX_CACHE_ENTRIES:
        oldest = min(cache, key=lambda k: cache[k][0])
        del cache[oldest]
    cache[key] = value


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TransitousProvider:
    """Transitous journeys between two places on one day, as ``ScheduledSegment`` (``source="transitous"``).

    ``search`` returns the journeys that depart on ``date_str`` (after ``earliest_dep_time`` when
    given), sorted by departure and capped at ``max_results``. ``[]`` means Transitous answered with
    no usable journey; ``None`` means the provider is disabled, a place cannot be resolved, the input
    is malformed or the service failed.
    """

    def __init__(
        self,
        timeout: float = 10.0,
        cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS,
        min_interval_secs: float = DEFAULT_MIN_INTERVAL_SECS,
        user_agent: str = DEFAULT_USER_AGENT,
        airport_db: Optional[AirportDB] = None,
        anchor_cache_path: Optional[str] = DEFAULT_ANCHOR_CACHE_PATH,
    ):
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.min_interval_secs = min_interval_secs
        self.user_agent = user_agent
        self.anchor_cache_path = anchor_cache_path  # None keeps the anchor cache in memory only
        self._airport_db = airport_db
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        # folded place name -> (stored_at, geocode hits)
        self._place_cache: Dict[str, Tuple[float, List[_Hit]]] = {}
        # folded place name -> (stored_at wall clock, lat, lon, tz); lat None = no station anchor found
        self._anchor_cache: Dict[str, Tuple[float, Optional[float], Optional[float], Optional[str]]] = {}
        self._anchor_file_read = False
        # (coordinates, date, request time, max_transfers) -> (stored_at, journeys)
        self._plan_cache: Dict[tuple, Tuple[float, List[_Journey]]] = {}
        self._requests = 0
        self._last_ok_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_error_at: Optional[str] = None
        self._last_http_status: Optional[int] = None
        self._consecutive_failures = 0
        self._blocked_until: Optional[float] = None

    # -- public API ----------------------------------------------------------

    def search(
        self,
        from_place: str,
        to_place: str,
        date_str: str,
        earliest_dep_time: Optional[str] = None,
        from_coord: Optional[Tuple[float, float]] = None,
        to_coord: Optional[Tuple[float, float]] = None,
        max_results: int = 6,
        max_transfers: int = 2,
    ) -> Optional[List[ScheduledSegment]]:
        if not is_enabled():
            return None
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
            earliest = _minutes(earliest_dep_time) if earliest_dep_time else None
        except (TypeError, ValueError):
            return None  # a malformed caller input is not a service failure
        origin = str(from_place or "").strip()
        dest = str(to_place or "").strip()
        if not origin or not dest:
            return None
        try:
            return self._search(origin, dest, day, earliest, from_coord, to_coord, max_results, max_transfers)
        except _Failure:
            return None  # already recorded in the health state
        except Exception as exc:  # the provider must never break the route engine
            logger.exception("Transitous search failed for %s -> %s on %s", origin, dest, date_str)
            self._record_failure(None, f"unexpected error: {exc}")
            return None

    def can_resolve(self, place: str) -> bool:
        """True when the place has coordinates (airport, city centre or geocoding). False when disabled or on any error."""
        if not is_enabled():
            return False
        name = str(place or "").strip()
        if not name:
            return False
        try:
            return self._resolve_place(name, None) is not None
        except Exception:  # _Failure is recorded already; anything else is treated the same
            return False

    def health_status(self) -> dict:
        """Current state: ``status`` is disabled, unknown, ok, error or blocked. Makes no request."""
        enabled = is_enabled()
        with self._lock:
            blocked_now = self._blocked_until is not None and _now() < self._blocked_until
            if not enabled:
                status = "disabled"
            elif blocked_now:
                status = "blocked"
            elif self._last_error is not None:
                status = "blocked" if self._last_http_status in BLOCKED_STATUSES else "error"
            elif self._last_ok_at is not None:
                status = "ok"
            else:
                status = "unknown"
            return {
                "source": SOURCE,
                "enabled": enabled,
                "status": status,
                "last_ok_at": self._last_ok_at,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at,
                "last_http_status": self._last_http_status,
                "consecutive_failures": self._consecutive_failures,
                "requests": self._requests,
                "blocked_cooldown_active": blocked_now,
                "cached_places": len(self._place_cache),
                "cached_anchors": len(self._anchor_cache),
                "cached_searches": len(self._plan_cache),
                "min_interval_secs": self.min_interval_secs,
            }

    # -- search steps --------------------------------------------------------

    def _search(
        self,
        origin: str,
        dest: str,
        day: date,
        earliest: Optional[int],
        from_coord: Optional[Tuple[float, float]],
        to_coord: Optional[Tuple[float, float]],
        max_results: int,
        max_transfers: int,
    ) -> Optional[List[ScheduledSegment]]:
        a = self._resolve_place(origin, from_coord)
        b = self._resolve_place(dest, to_coord)
        if a is None or b is None:
            return None
        if (a.lat, a.lon) == (b.lat, b.lon):
            return []
        journeys = self._plan(a, b, day, _request_time(day, earliest, a), max_transfers)
        return _segments(journeys, origin, dest, day, earliest, max_results)

    def _airports(self) -> AirportDB:
        if self._airport_db is None:
            self._airport_db = AirportDB.load_default()
        return self._airport_db

    def _resolve_place(self, name: str, coord: Optional[Tuple[float, float]]) -> Optional[_Place]:
        """Coordinates for a place: the given coordinate, else the station (HUB_STATIONS, else a geocode
        station near the city centre), else the city centre, else the first geocode result."""
        if coord is not None:
            return _Place(float(coord[0]), float(coord[1]), None)
        hub = _hub_place(name)
        if hub is not None:
            return hub
        centre = self._airports().city_center(name)
        try:
            anchor = self._anchor(name, centre)
        except _Failure:  # recorded already; without a centre there is nothing else to try
            if centre is None:
                raise
            anchor = None
        if anchor is not None:
            return anchor
        if centre is not None:
            return _Place(float(centre[0]), float(centre[1]), None)
        return self._geocode(name)

    def _anchor(self, name: str, centre: Optional[Tuple[float, float]]) -> Optional[_Place]:
        """The station a search starts from for a place without a HUB_STATIONS entry, or None (use the
        centre). Cached ``ANCHOR_TTL_SECONDS`` in memory and in the JSON file when one is configured.
        A geocode failure raises ``_Failure`` and is not cached, so the next search asks again."""
        key = _fold(name)
        now = _wall()
        with self._lock:
            self._read_anchor_file_locked()
            hit = self._anchor_cache.get(key)
        if hit is not None and 0 <= now - hit[0] < ANCHOR_TTL_SECONDS:
            return None if hit[1] is None else _Place(hit[1], hit[2], hit[3])

        hits = self._geocode_hits(name)
        reference = (float(centre[0]), float(centre[1])) if centre is not None else _reference_point(hits)
        found = _pick_anchor(hits, reference) if reference is not None else None
        entry = (now, None, None, None) if found is None else (now, found.lat, found.lon, found.tz)
        with self._lock:
            _remember(self._anchor_cache, key, entry)
            snapshot = dict(self._anchor_cache)
        self._write_anchor_file(snapshot)
        return None if found is None else _Place(found.lat, found.lon, found.tz)

    def _read_anchor_file_locked(self) -> None:
        """Load the JSON anchor cache once; unexpired entries only. Caller holds ``self._lock``."""
        if self._anchor_file_read:
            return
        self._anchor_file_read = True
        if not self.anchor_cache_path:
            return
        now = _wall()
        for key, entry in _read_anchor_file(self.anchor_cache_path).items():
            if 0 <= now - entry[0] < ANCHOR_TTL_SECONDS and key not in self._anchor_cache:
                _remember(self._anchor_cache, key, entry)

    def _write_anchor_file(self, entries: Dict[str, tuple]) -> None:
        """Replace the JSON anchor cache atomically. Failures are logged and ignored (the cache is optional)."""
        path = self.anchor_cache_path
        if not path:
            return
        payload = {
            "version": ANCHOR_FILE_VERSION,
            "anchors": {key: {"at": at, "lat": lat, "lon": lon, "tz": tz}
                        for key, (at, lat, lon, tz) in entries.items()},
        }
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with self._file_lock:
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, allow_nan=False)
                os.replace(tmp, path)
            except (OSError, ValueError, TypeError) as exc:
                logger.debug("Transitous anchor cache not written: %s", exc)
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _geocode_hits(self, name: str) -> List[_Hit]:
        """All geocode results with coordinates, in the order given (cached ``GEOCODE_TTL_SECONDS``)."""
        key = _fold(name)
        now = _now()
        with self._lock:
            hit = self._place_cache.get(key)
        if hit is not None and now - hit[0] < GEOCODE_TTL_SECONDS:
            return hit[1]

        payload = self._get_json(GEOCODE_URL + "?" + urllib.parse.urlencode({"text": name}))
        if not isinstance(payload, list):
            raise self._record_failure(None, "geocode response is not a list")
        hits: List[_Hit] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            lat, lon = _num(item.get("lat")), _num(item.get("lon"))
            if lat is None or lon is None:
                continue
            tz = item.get("tz") if isinstance(item.get("tz"), str) else None
            kind = str(item.get("type") or "").strip().upper()
            raw_modes = item.get("modes") if isinstance(item.get("modes"), list) else []
            modes = tuple(str(mode).strip().upper() for mode in raw_modes)
            hits.append(_Hit(lat, lon, tz, kind, str(item.get("name") or ""), modes))
        with self._lock:
            _remember(self._place_cache, key, (now, hits))
        return hits

    def _geocode(self, name: str) -> Optional[_Place]:
        """The first geocode result with coordinates, or None."""
        hits = self._geocode_hits(name)
        if not hits:
            return None
        return _Place(hits[0].lat, hits[0].lon, hits[0].tz)

    def _plan(
        self, origin: _Place, dest: _Place, day: date, request_time: str, max_transfers: int,
    ) -> List[_Journey]:
        key = (round(origin.lat, 5), round(origin.lon, 5), round(dest.lat, 5), round(dest.lon, 5),
               day.isoformat(), request_time, max_transfers)
        now = _now()
        with self._lock:
            hit = self._plan_cache.get(key)
        if hit is not None and now - hit[0] < self.cache_ttl:
            return list(hit[1])

        url = PLAN_URL + "?" + urllib.parse.urlencode({
            "fromPlace": f"{origin.lat:.6f},{origin.lon:.6f}",
            "toPlace": f"{dest.lat:.6f},{dest.lon:.6f}",
            "time": request_time,
            "transitModes": TRANSIT_MODES,
            "maxTransfers": str(max_transfers),
            "numItineraries": str(NUM_ITINERARIES),
        }, quote_via=urllib.parse.quote, safe=",:")
        payload = self._get_json(url)
        try:
            journeys = _parse_plan(payload, day, max_transfers)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise self._record_failure(200, f"unexpected plan response: {exc}")
        with self._lock:
            _remember(self._plan_cache, key, (now, journeys))
        return list(journeys)

    # -- HTTP and health -----------------------------------------------------

    def _get_json(self, url: str):
        with self._lock:
            blocked_until = self._blocked_until
        if blocked_until is not None and _now() < blocked_until:
            raise _Failure(None, "cooling down after a blocked response")

        _limiter.reserve(self.min_interval_secs)
        with self._lock:
            self._requests += 1
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:  # before URLError: it is a subclass
            if exc.code in BLOCKED_STATUSES:
                with self._lock:
                    self._blocked_until = _now() + BLOCK_COOLDOWN_SECONDS
            raise self._record_failure(exc.code, f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            raise self._record_failure(None, f"network error: {exc}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise self._record_failure(200, "response is not JSON")
        with self._lock:
            self._last_ok_at = _iso_now()
            self._last_error = None
            self._last_error_at = None
            self._last_http_status = 200
            self._consecutive_failures = 0
        return payload

    def _record_failure(self, status: Optional[int], message: str) -> _Failure:
        """Record a failed call in the health state and return the exception to raise."""
        logger.warning("Transitous: %s", message)
        with self._lock:
            self._consecutive_failures += 1
            self._last_error = message
            self._last_error_at = _iso_now()
            self._last_http_status = status
        return _Failure(status, message)
