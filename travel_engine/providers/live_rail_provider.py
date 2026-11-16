"""Live rail schedules and fares from RegioJet's public JSON API (undocumented, no auth).

Two entry points:

* ``get_schedules(from_station, to_station, ...)`` serves the station corridors in
  ``LIVE_CORRIDORS`` only, through the station ids in ``REGIOJET_STATION_IDS``.
  The Deutsche Bahn source (v6.db.transport.rest) is not used: its backend has been
  shut off, so every request returns HTTP 503.
* ``search(from_place, to_place, date_str, ...)`` works for any pair of places that
  RegioJet knows (cities, plus stations by name). It resolves both places through the
  public locations list and then calls the city-level route search.

Every failure (network, HTTP error, bad JSON, unexpected shape) makes the public
methods return ``None`` so the caller can fall back to the static timetable table in
``rail_bus_provider``. An empty result is ``[]``.
"""

import dataclasses
import json
import logging
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from travel_engine import fx
from travel_engine.providers.rail_bus_provider import ScheduledSegment

logger = logging.getLogger(__name__)

ROUTE_SEARCH_URL = "https://brn-ybus-pubapi.sa.cz/restapi/routes/search/simple"
LOCATIONS_URL = "https://brn-ybus-pubapi.sa.cz/restapi/consts/locations"
TIMEOUT_SECONDS = 8
CACHE_TTL_SECONDS = 30 * 60
SEARCH_CACHE_TTL_SECONDS = 30 * 60
LOCATIONS_TTL_SECONDS = 24 * 60 * 60
LOCATIONS_RETRY_SECONDS = 5 * 60
OPERATOR = "RegioJet"
DEFAULT_TRANSFER_MODES: Tuple[str, ...] = ("TRAIN", "BUS")

# Re-exported for callers and tests. The value and its meaning live in ``fx``.
DEFAULT_EUR_TRY_RATE = fx.DEFAULT_EUR_TRY_RATE

# Station name (as used by RailBusProvider) -> RegioJet station id.
# Ids come from https://brn-ybus-pubapi.sa.cz/restapi/consts/locations.
REGIOJET_STATION_IDS: Dict[str, int] = {
    "Bratislava hl.st.": 1841058000,  # "Bratislava - MS" (Bratislava Hlavna stanica)
    "Wien Hbf": 3741302011,           # "Vienna - Hbf"
    "Praha hl.n.": 372825000,         # "Prague - MS" (Wilsonova 300/8, Praha hlavni nadrazi)
    "Budapest Kelenföld": 5404232005,  # Budapest-Prague RegioJet trains depart here
}

# Corridors verified with train results on 2026-10-28 (see tests/fixtures/rail).
# Budapest Keleti is absent: RegioJet has no train from Keleti (id 7063331001)
# to Praha; Budapest-Prague trains depart Budapest Kelenföld (id 5404232005).
LIVE_CORRIDORS = frozenset({
    ("Bratislava hl.st.", "Praha hl.n."),
    ("Wien Hbf", "Praha hl.n."),
    ("Praha hl.n.", "Wien Hbf"),
    ("Budapest Kelenföld", "Praha hl.n."),
})

# Turkish names -> English names used in RegioJet's locations list. Zagreb and Graz are
# not in that list (RegioJet does not serve them), so they are deliberately absent.
TURKISH_ALIASES: Dict[str, str] = {
    "Viyana": "Vienna",
    "Prag": "Prague",
    "Budapeşte": "Budapest",
    "Münih": "Munich",
    "Varşova": "Warsaw",
    "Krakov": "Cracow",
    "Bratislava": "Bratislava",
    "Brno": "Brno",
    "Berlin": "Berlin",
    "Dresden": "Dresden",
    "Linz": "Linz",
    "Salzburg": "Salzburg",
    "Košice": "Košice",
}

CacheKey = Tuple[str, str, str]
SearchKey = Tuple[str, str, str, Tuple[str, ...]]

_FOLD_MAP = str.maketrans({
    "ı": "i", "ł": "l", "đ": "d", "ø": "o", "ß": "ss", "æ": "ae", "œ": "oe", "þ": "th",
})
_NON_WORD = re.compile(r"[\W_]+")


def _fold(text: str) -> str:
    """Lower-case, strip diacritics (ş->s, ğ->g, ü->u, İ->i ...) and collapse punctuation."""
    text = unicodedata.normalize("NFKD", str(text).lower().translate(_FOLD_MAP))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _NON_WORD.sub(" ", text).strip()


def _minutes_of(hhmm: str) -> int:
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


def _valid_date(date_str: object) -> bool:
    """True for YYYY-MM-DD. A malformed caller date is an input error, not a service failure."""
    try:
        datetime.strptime(str(date_str), "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _get_json(url: str, timeout: float, headers: Optional[Dict[str, str]] = None):
    request_headers = {"Accept": "application/json", "User-Agent": "travelplan/1.0"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body.decode("utf-8"))


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _http_status(exc: BaseException) -> Optional[int]:
    return exc.code if isinstance(exc, urllib.error.HTTPError) else None


class _Health:
    """Request and error counters behind ``LiveRailProvider.health_status()``. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.last_ok_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_error_at: Optional[str] = None
        self.last_http_status: Optional[int] = None
        self.consecutive_failures = 0

    def request(self) -> None:
        with self._lock:
            self.requests += 1

    def ok(self) -> None:
        with self._lock:
            self.last_ok_at = _iso_now()
            self.last_error = None
            self.last_error_at = None
            self.last_http_status = 200
            self.consecutive_failures = 0

    def fail(self, message: str, status: Optional[int] = None) -> None:
        logger.warning("RegioJet: %s", message)
        with self._lock:
            self.last_error = message
            self.last_error_at = _iso_now()
            self.last_http_status = status
            self.consecutive_failures += 1

    def snapshot(self, cached_searches: int) -> dict:
        with self._lock:
            if self.last_error is not None:
                status = "error"
            elif self.last_ok_at is not None:
                status = "ok"
            else:
                status = "unknown"
            return {
                "source": "regiojet",
                "status": status,
                "last_ok_at": self.last_ok_at,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "last_http_status": self.last_http_status,
                "consecutive_failures": self.consecutive_failures,
                "requests": self.requests,
                "cached_searches": cached_searches,
            }


# ---------------------------------------------------------------------------
# Location index (cities and stations by name), shared by all instances.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Station:
    id: int
    name: str
    fullname: str
    aliases: Tuple[str, ...]
    tokens: FrozenSet[str]


@dataclass
class _City:
    id: int
    name: str
    aliases: Tuple[str, ...] = ()
    stations: List[_Station] = field(default_factory=list)


class _LocationIndex:
    def __init__(self, cities: Sequence[_City]):
        self.by_name: Dict[str, _City] = {}
        self.by_station: Dict[str, _Station] = {}
        self.by_station_id: Dict[int, _Station] = {}

        # City names beat aliases; a key is never overwritten once set.
        for city in cities:
            self.by_name.setdefault(_fold(city.name), city)
        for city in cities:
            for alias in city.aliases:
                self.by_name.setdefault(_fold(alias), city)
        for tr_name, canonical in TURKISH_ALIASES.items():
            target = self.by_name.get(_fold(canonical))
            if target is not None:
                self.by_name.setdefault(_fold(tr_name), target)

        # Full station names ("Vienna - Hbf") and station aliases always index.
        # A short name ("Kelenföld") indexes only when no other station shares it.
        short_names = Counter(_fold(s.name) for c in cities for s in c.stations)
        stations = [s for c in cities for s in c.stations]
        for station in stations:
            self.by_station_id.setdefault(station.id, station)
            self.by_station.setdefault(_fold(station.fullname), station)
            for alias in station.aliases:
                self.by_station.setdefault(_fold(alias), station)
        for station in stations:
            short = _fold(station.name)
            if short and short_names[short] == 1:
                self.by_station.setdefault(short, station)

    def station_name(self, station_id: object) -> Optional[str]:
        """RegioJet's full station name (e.g. "Vienna - Hbf") for a station id, or None when unknown."""
        if isinstance(station_id, bool) or not isinstance(station_id, int):
            return None
        station = self.by_station_id.get(station_id)
        return station.fullname if station is not None else None

    def resolve(self, place: str) -> Optional[Tuple[str, int]]:
        """Return ``("CITY", id)`` or ``("STATION", id)``, or None when nothing matches.

        Order: city name or alias; exact station name; a city prefix plus a station
        qualifier (``"Budapest Keleti"``) when exactly one station matches; the city.
        """
        key = _fold(place)
        if not key:
            return None
        city = self.by_name.get(key)
        if city is not None:
            return ("CITY", city.id)
        station = self.by_station.get(key)
        if station is not None:
            return ("STATION", station.id)
        tokens = key.split()
        for cut in range(len(tokens) - 1, 0, -1):
            city = self.by_name.get(" ".join(tokens[:cut]))
            if city is None:
                continue
            qualifier = tokens[cut:]
            matches = [s for s in city.stations if all(t in s.tokens for t in qualifier)]
            if len(matches) == 1:
                return ("STATION", matches[0].id)
            return ("CITY", city.id)
        return None


def _alias_list(raw: object) -> Tuple[str, ...]:
    """Aliases come as a list, sometimes as one comma-separated string."""
    items: Iterable[object]
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, list):
        items = raw
    else:
        return ()
    return tuple(str(item).strip() for item in items if str(item).strip())


def _build_index(payload: object) -> _LocationIndex:
    if not isinstance(payload, list):
        raise ValueError("locations payload is not a list")
    cities: List[_City] = []
    for country in payload:
        for raw_city in country.get("cities") or []:
            city = _City(
                id=int(raw_city["id"]),
                name=str(raw_city["name"]),
                aliases=_alias_list(raw_city.get("aliases")),
            )
            for raw_station in raw_city.get("stations") or []:
                name = str(raw_station.get("name") or "")
                fullname = str(raw_station.get("fullname") or name)
                city.stations.append(_Station(
                    id=int(raw_station["id"]),
                    name=name,
                    fullname=fullname,
                    aliases=_alias_list(raw_station.get("aliases")),
                    tokens=frozenset(_fold(fullname).split()),
                ))
            cities.append(city)
    return _LocationIndex(cities)


def _station_name(index: Optional[_LocationIndex], raw_id: object) -> Optional[str]:
    """Station name for a route's station id, or None without an index or for an unknown id."""
    if index is None:
        return None
    return index.station_name(raw_id)


class _LocationCache:
    def __init__(self) -> None:
        self.index: Optional[_LocationIndex] = None
        self.loaded_at: Optional[float] = None
        self.failed_at: Optional[float] = None


_locations_lock = threading.Lock()
_locations = _LocationCache()
_now = time.monotonic  # module clock for the locations cache; tests replace it


def _load_location_index(timeout: float, health: Optional[_Health] = None) -> Optional[_LocationIndex]:
    """Cached locations index (24 h). After a failed refresh the last good index is used; None if never loaded.

    ``health`` (optional) records the request made here. Cache hits and retry-window skips make no request.
    """
    with _locations_lock:
        now = _now()
        if _locations.index is not None and _locations.loaded_at is not None:
            if now - _locations.loaded_at < LOCATIONS_TTL_SECONDS:
                return _locations.index
        if _locations.failed_at is not None and now - _locations.failed_at < LOCATIONS_RETRY_SECONDS:
            return _locations.index
        try:
            if health is not None:
                health.request()
            index = _build_index(_get_json(LOCATIONS_URL, timeout))
        except Exception as exc:
            logger.warning("RegioJet locations lookup failed: %s", exc)
            if health is not None:
                health.fail(f"locations lookup failed: {exc}", _http_status(exc))
            _locations.failed_at = now
            return _locations.index
        if health is not None:
            health.ok()
        _locations.index = index
        _locations.loaded_at = now
        _locations.failed_at = None
        return index


def _reset_location_cache_for_tests() -> None:
    global _locations
    with _locations_lock:
        _locations = _LocationCache()


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Connection:
    transfers: int
    segment: ScheduledSegment  # price_eur set; price_try is recomputed on every return


class LiveRailProvider:
    """Fetches real train timetables and lowest fares from RegioJet, with a 30-minute cache.

    Segments returned from ``get_schedules`` and ``search``:
      * ``is_estimated=False`` (times are real);
      * ``price_eur`` holds RegioJet's lowest fare in EUR and ``price_try`` is that
        fare times ``eur_try_rate``;
      * ``get_schedules`` only: when no fare is available, ``price_eur`` and ``price_try`` are None
        (unknown, never 0) and ``price_is_estimated`` is True. ``RailBusProvider`` fills in the table price.

    ``eur_try_rate`` is the explicit constructor value when given, otherwise
    ``TRAVELPLAN_EUR_TRY`` when set, otherwise the live rate from ``fx``.
    """

    def __init__(
        self,
        eur_try_rate: Optional[float] = None,
        timeout: float = TIMEOUT_SECONDS,
        cache_ttl: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._fixed_rate: Optional[float] = None
        self.eur_try_rate = eur_try_rate if eur_try_rate is not None else fx.env_eur_try_rate()
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._clock = clock
        self._cache: Dict[CacheKey, Tuple[float, List[ScheduledSegment]]] = {}
        self._search_cache: Dict[SearchKey, Tuple[float, List[_Connection]]] = {}
        self._cache_lock = threading.Lock()
        self._health = _Health()

    def health_status(self) -> dict:
        """RegioJet service state: status unknown (no request yet), ok or error, plus request counters.

        Only real requests count (corridor, search and locations); unsupported corridors, malformed
        dates and unresolvable places make no request and change nothing. ``cached_searches`` is the
        number of cached corridor days plus cached searches.
        """
        return self._health.snapshot(cached_searches=len(self._cache) + len(self._search_cache))

    @property
    def eur_try_rate(self) -> float:
        if self._fixed_rate is not None:
            return self._fixed_rate
        return fx.get_eur_try_rate()[0]

    @eur_try_rate.setter
    def eur_try_rate(self, value: Optional[float]) -> None:
        self._fixed_rate = float(value) if value is not None else None

    def supports(self, from_station: str, to_station: str) -> bool:
        return (from_station.strip(), to_station.strip()) in LIVE_CORRIDORS

    def get_schedules(
        self,
        from_station: str,
        to_station: str,
        date_str: str,
        earliest_dep_time: Optional[str] = None,
    ) -> Optional[List[ScheduledSegment]]:
        """Return live train segments, ``[]`` when none run, or ``None`` on any error or unsupported corridor."""
        from_station = from_station.strip()
        to_station = to_station.strip()
        if not self.supports(from_station, to_station) or not _valid_date(date_str):
            return None

        key: CacheKey = (from_station, to_station, date_str)
        day = self._cache_get(key)
        if day is None:
            day = self._fetch(from_station, to_station, date_str)
            if day is None:
                return None
            self._cache[key] = (self._clock(), day)

        # Copies, so callers may change segments without corrupting the cache.
        result = [dataclasses.replace(seg) for seg in day]
        if earliest_dep_time:
            earliest = _minutes_of(earliest_dep_time)
            result = [seg for seg in result if _minutes_of(seg.departure_time) >= earliest]
        return result

    def search(
        self,
        from_place: str,
        to_place: str,
        date_str: str,
        earliest_dep_time: Optional[str] = None,
        modes: Tuple[str, ...] = DEFAULT_TRANSFER_MODES,
        max_transfers: int = 1,
    ) -> Optional[List[ScheduledSegment]]:
        """Connections between any two RegioJet places on one day.

        Each place is resolved to a RegioJet city (or station, when the name points at one).
        Only connections with at most ``max_transfers`` changes and vehicle types inside
        ``modes`` are kept. Sold-out or unpriced connections are skipped.

        ``origin_station``/``dest_station`` are the places as given. ``origin_station_name``/
        ``dest_station_name`` are RegioJet's names for the route's own stations (e.g.
        "Vienna - Hbf"), taken from the route's station ids; None when an id is unknown.

        Returns ``None`` when a place cannot be resolved or on any error, and ``[]``
        when both places resolve but no connection matches.
        """
        if not _valid_date(date_str):
            return None
        try:
            mode_set = frozenset(str(m).strip().upper() for m in modes)
            # Keyed by the names as given: cached segments carry those names as origin/dest.
            key: SearchKey = (from_place.strip(), to_place.strip(), date_str, tuple(sorted(mode_set)))
            connections = self._search_cache_get(key)
            if connections is None:
                connections = self._fetch_search(from_place, to_place, date_str, mode_set)
                if connections is None:
                    return None
                with self._cache_lock:
                    self._search_cache[key] = (self._clock(), connections)

            earliest = _minutes_of(earliest_dep_time) if earliest_dep_time else None
            rate = self.eur_try_rate
            result: List[ScheduledSegment] = []
            for conn in connections:
                if conn.transfers > max_transfers:
                    continue
                seg = dataclasses.replace(conn.segment)
                if earliest is not None and _minutes_of(seg.departure_time) < earliest:
                    continue
                seg.price_try = round(seg.price_eur * rate, 2)
                result.append(seg)
            return result
        except Exception:  # a search problem must never break the caller
            logger.exception("RegioJet search failed for %s -> %s on %s", from_place, to_place, date_str)
            self._health.fail("search failed: unexpected error")
            return None

    def can_resolve(self, place: str) -> bool:
        """True when the place names a RegioJet city or station (uses the cached locations list)."""
        try:
            index = _load_location_index(self.timeout, self._health)
            return index is not None and index.resolve(place) is not None
        except Exception:
            logger.exception("Could not check RegioJet place %r", place)
            return False

    # -- station corridors -------------------------------------------------

    def _cache_get(self, key: CacheKey) -> Optional[List[ScheduledSegment]]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, segments = entry
        if self._clock() - stored_at > self.cache_ttl:
            self._cache.pop(key, None)
            return None
        return segments

    def _fetch(self, from_station: str, to_station: str, date_str: str) -> Optional[List[ScheduledSegment]]:
        try:
            url = self._build_url(from_station, to_station, date_str)
            self._health.request()
            payload = _get_json(url, self.timeout, headers={"X-Currency": "EUR"})
            segments = self._parse(payload, from_station, to_station, date_str)
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self._health.fail(
                f"corridor {from_station} -> {to_station} on {date_str} failed: {exc}", _http_status(exc),
            )
            return None
        self._health.ok()
        return segments

    @staticmethod
    def _build_url(from_station: str, to_station: str, date_str: str) -> str:
        datetime.strptime(date_str, "%Y-%m-%d")  # reject malformed dates before the request
        return LiveRailProvider._search_url(
            ("STATION", REGIOJET_STATION_IDS[from_station]),
            ("STATION", REGIOJET_STATION_IDS[to_station]),
            date_str,
        )

    @staticmethod
    def _search_url(origin: Tuple[str, int], destination: Tuple[str, int], date_str: str) -> str:
        params = {
            "tariffs": "REGULAR",
            "fromLocationType": origin[0],
            "fromLocationId": origin[1],
            "toLocationType": destination[0],
            "toLocationId": destination[1],
            "departureDate": date_str,
        }
        return ROUTE_SEARCH_URL + "?" + urllib.parse.urlencode(params)

    def _parse(self, payload: dict, from_station: str, to_station: str, date_str: str) -> List[ScheduledSegment]:
        routes = payload["routes"]
        if not isinstance(routes, list):
            raise ValueError("routes is not a list")

        segments: List[ScheduledSegment] = []
        for route in routes:
            dep_raw = route["departureTime"]
            if not dep_raw.startswith(date_str):
                continue  # the API also returns the following days
            if route.get("transfersCount", 0) != 0 or set(route.get("vehicleTypes", [])) != {"TRAIN"}:
                continue  # only direct trains; buses and transfers are other providers' job

            dep = datetime.fromisoformat(dep_raw)
            arr = datetime.fromisoformat(route["arrivalTime"])
            duration = int((arr - dep).total_seconds() // 60)
            if duration <= 0:
                continue

            price_eur = self._fare_eur(route)
            seg = ScheduledSegment(
                origin_station=from_station,
                dest_station=to_station,
                departure_time=dep.strftime("%H:%M"),
                arrival_time=arr.strftime("%H:%M"),
                duration_mins=duration,
                price_try=round(price_eur * self.eur_try_rate, 2) if price_eur is not None else None,
                operator=OPERATOR,
                mode="train",
                date=date_str,
                is_estimated=False,
                price_eur=price_eur,
                price_is_estimated=price_eur is None,
                # The corridor keys are the real station names (see REGIOJET_STATION_IDS).
                origin_station_name=from_station,
                dest_station_name=to_station,
                vehicle_types=["train"],
            )
            segments.append(seg)
        return segments

    @staticmethod
    def _fare_eur(route: dict) -> Optional[float]:
        price = route.get("priceFrom")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
            return None  # 0 means sold out or no fare published
        return float(price)

    # -- general search ----------------------------------------------------

    def _search_cache_get(self, key: SearchKey) -> Optional[List[_Connection]]:
        with self._cache_lock:
            entry = self._search_cache.get(key)
            if entry is None:
                return None
            stored_at, connections = entry
            if self._clock() - stored_at > SEARCH_CACHE_TTL_SECONDS:
                self._search_cache.pop(key, None)
                return None
            return connections

    def _fetch_search(
        self, from_place: str, to_place: str, date_str: str, modes: FrozenSet[str],
    ) -> Optional[List[_Connection]]:
        try:
            index = _load_location_index(self.timeout, self._health)  # a locations failure is recorded there
            if index is None:
                return None
            origin = index.resolve(from_place)
            destination = index.resolve(to_place)
            if origin is None or destination is None:
                return None  # an unknown place is a caller matter, not a service failure
            self._health.request()
            payload = _get_json(
                self._search_url(origin, destination, date_str),
                self.timeout,
                headers={"X-Currency": "EUR"},
            )
            connections = self._parse_search(payload, from_place.strip(), to_place.strip(), date_str, modes, index)
        except Exception as exc:
            self._health.fail(
                f"search {from_place} -> {to_place} on {date_str} failed: {exc}", _http_status(exc),
            )
            return None
        self._health.ok()
        return connections

    @staticmethod
    def _parse_search(
        payload: dict,
        from_place: str,
        to_place: str,
        date_str: str,
        modes: FrozenSet[str],
        stations: Optional[_LocationIndex] = None,
    ) -> List[_Connection]:
        """Connections of one search. ``stations`` maps the route's station ids to names (None: no names)."""
        routes = payload["routes"]
        if not isinstance(routes, list):
            raise ValueError("routes is not a list")

        connections: List[_Connection] = []
        for route in routes:
            dep_raw = route["departureTime"]
            if not dep_raw.startswith(date_str):
                continue  # the API also returns the following days
            if route.get("bookable") is not True:
                continue  # sold out
            vehicles = frozenset(str(v).upper() for v in route.get("vehicleTypes") or [])
            if not vehicles or not vehicles <= modes:
                continue
            price_eur = LiveRailProvider._fare_eur(route)
            if price_eur is None:
                continue  # no published fare (0 or missing)

            dep = datetime.fromisoformat(dep_raw)
            arr = datetime.fromisoformat(route["arrivalTime"])
            duration = int((arr - dep).total_seconds() // 60)
            if duration <= 0:
                continue

            if vehicles == {"TRAIN"}:
                mode = "train"
            elif vehicles == {"BUS"}:
                mode = "bus"
            else:
                mode = "train"  # mixed bus and train connections
            vtypes = sorted(list({str(v).lower() for v in (route.get("vehicleTypes") or [])}))
            seg = ScheduledSegment(
                origin_station=from_place,
                dest_station=to_place,
                departure_time=dep.strftime("%H:%M"),
                arrival_time=arr.strftime("%H:%M"),
                duration_mins=duration,
                price_try=None,  # set from price_eur on every return
                operator=OPERATOR,
                mode=mode,
                date=date_str,
                is_estimated=False,
                price_eur=price_eur,
                price_is_estimated=False,
                origin_station_name=_station_name(stations, route.get("departureStationId")),
                dest_station_name=_station_name(stations, route.get("arrivalStationId")),
                vehicle_types=vtypes,
            )
            connections.append(_Connection(
                transfers=int(route.get("transfersCount", 0)),
                segment=seg,
            ))
        return connections
