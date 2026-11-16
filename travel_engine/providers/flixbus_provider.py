"""FlixBus schedules and lowest fares from the public endpoints behind shop.flixbus.com.

No login and no API key. Two undocumented GET endpoints are used:

* ``https://global.api.flixbus.com/search/autocomplete/cities?q=<place>&lang=en&flixbus_cities_only=false``
  resolves a place name to FlixBus city ids. Results are cached for 7 days.
* ``https://global.api.flixbus.com/search/service/v4/search?from_city_id=<uuid>&to_city_id=<uuid>
  &departure_date=DD.MM.YYYY&products={"adult":1}&currency=EUR&locale=en&search_by=cities
  &include_after_midnight_rides=1`` returns the trips of one day. Each connection has a status,
  local departure and arrival times, a duration, a price and its legs (bus or train, brand).

Notes on use: FlixBus publishes GTFS timetable feeds as well; they are not used here. The
booking terms prohibit fictitious bookings; this provider only searches and never books. These
endpoints are not a supported public API and can change or block clients at any time, so every
failure (including HTTP 403 and 429) makes the public methods return ``None``, and the state is
reported by ``health_status()``. After a 403/429 the provider stays silent for a cooldown period.

Requests are rate limited process-wide (``min_interval_secs``) and cached in memory.
"""

import json
import logging
import os
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from travel_engine import fx
from travel_engine.airports import AirportDB
from travel_engine.providers.rail_bus_provider import ScheduledSegment

logger = logging.getLogger(__name__)

SOURCE = "flixbus"
AUTOCOMPLETE_URL = "https://global.api.flixbus.com/search/autocomplete/cities"
SEARCH_URL = "https://global.api.flixbus.com/search/service/v4/search"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "en",
    "Origin": "https://shop.flixbus.com",
    "Referer": "https://shop.flixbus.com/",
}
CITY_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_CACHE_TTL_SECONDS = 30 * 60
BLOCKED_STATUSES = frozenset({403, 429})
BLOCK_COOLDOWN_SECONDS = 15 * 60
DEFAULT_MIN_INTERVAL_SECS = 0.8
MIN_INTERVAL_ENV = "TRAVELPLAN_FLIXBUS_MIN_INTERVAL"
# A place with no main city of its own can stand for several FlixBus cities ("Istanbul" -> Anadolu and
# Avrupa). Up to this many are searched, then merged. A place with a main city ("Prague", "Paris") is
# searched in that one city only, and "<City> Airport" cities are skipped unless the place is the airport.
MAX_CITIES_PER_PLACE = 2
AIRPORT_WORDS = frozenset({
    "airport", "airports", "havalimani", "havaalani", "flughafen", "aeroporto", "aeroport",
    "aeropuerto", "lotnisko", "letiste", "luchthaven",
})

# Monotonic clock and sleep, module level so tests can replace them.
_now = time.monotonic
_sleep = time.sleep

_FOLD_MAP = str.maketrans({
    "ı": "i", "ł": "l", "đ": "d", "ø": "o", "ß": "ss", "æ": "ae", "œ": "oe", "þ": "th",
})

# Place names as people write them (Turkish and other local forms) -> the English name that
# FlixBus's autocomplete knows. Keys are folded with ``_fold`` at import time.
_PLACE_ALIASES_RAW: Dict[str, str] = {
    "Milano": "Milan", "Venedik": "Venice", "Venezia": "Venice", "Roma": "Rome",
    "Napoli": "Naples", "Floransa": "Florence", "Atina": "Athens", "Selanik": "Thessaloniki",
    "Solun": "Thessaloniki", "Sofya": "Sofia", "Sofija": "Sofia", "Belgrad": "Belgrade",
    "Beograd": "Belgrade", "Viyana": "Vienna", "Wien": "Vienna", "Prag": "Prague",
    "Praha": "Prague", "Münih": "Munich", "München": "Munich", "Budapeşte": "Budapest",
    "Varşova": "Warsaw", "Warschau": "Warsaw", "Krakov": "Krakow", "Kraków": "Krakow",
    "Krakau": "Krakow", "Cenevre": "Geneva", "Genf": "Geneva", "Brüksel": "Brussels",
    "Bruxelles": "Brussels", "Lizbon": "Lisbon", "Lisboa": "Lisbon", "Köln": "Cologne",
    "Bükreş": "Bucharest", "Üsküp": "Skopje", "Priştine": "Pristina", "Saraybosna": "Sarajevo",
}
_ALIASES: Dict[str, str] = {}


def _fold(text: str) -> str:
    """Lower-case, strip diacritics (ş->s, ğ->g, ü->u, İ->i ...) and collapse punctuation."""
    text = unicodedata.normalize("NFKD", str(text).lower().translate(_FOLD_MAP))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


_ALIASES.update({_fold(k): v for k, v in _PLACE_ALIASES_RAW.items()})


def _min_interval_from_env() -> float:
    """Seconds between FlixBus requests: TRAVELPLAN_FLIXBUS_MIN_INTERVAL, else the default. Bad values are ignored."""
    raw = os.environ.get(MIN_INTERVAL_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_MIN_INTERVAL_SECS
    except ValueError:
        return DEFAULT_MIN_INTERVAL_SECS
    return value if value >= 0 else DEFAULT_MIN_INTERVAL_SECS


def _minutes(hhmm: str) -> int:
    """'HH:MM' -> minutes after midnight. Raises ValueError for anything else."""
    parts = str(hhmm).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"bad time {hhmm!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time {hhmm!r}")
    return h * 60 + m


def _names_airport(qkey: str) -> bool:
    """True when the place names an airport itself ("Sofia Airport", "Havalimanı"); "Sofia" is not one."""
    return bool(AIRPORT_WORDS & set(qkey.split()))


def _pick_cities(cities: List[dict], qkey: str, names_airport: bool) -> List[dict]:
    """The FlixBus cities a place is searched in (at most MAX_CITIES_PER_PLACE), best first.

    * The place names an airport: its airport cities are the right ones (exact, then prefixed).
    * Otherwise "<City> Airport"-style cities are skipped. An exact main-city match ("Prague") is the
      one city; without one, the main cities that start with the query's words are kept ("Istanbul").
    * A place whose only FlixBus cities are airports keeps them, so it is not lost.
    """
    query_words = qkey.split()

    def exact(c: dict) -> bool:
        return _fold(c["name"]) == qkey

    def prefixed(c: dict) -> bool:
        return not exact(c) and _fold(c["name"]).split()[:len(query_words)] == query_words

    def airport_first(group: List[dict]) -> List[dict]:
        return [c for c in group if exact(c)] + [c for c in group if prefixed(c)]

    if names_airport:
        ordered = airport_first(cities)
    else:
        main = [c for c in cities if not (AIRPORT_WORDS & set(_fold(c["name"]).split()))]
        ordered = [c for c in main if exact(c)] or [c for c in main if prefixed(c)]
        if not ordered:
            ordered = airport_first(cities)
    return ordered[:MAX_CITIES_PER_PLACE]


def _island_without_bus_service(place: str) -> bool:
    """True when every airport of the place is an island airport outside Italy (no bus or ferry service).

    Conservative on purpose: only a place AirportDB resolves to island airports qualifies, and Italian
    islands are never excluded (FlixBus serves Sicilian cities). Makes no network request.
    """
    db = AirportDB.load_default()
    airports = db.find_city(place)
    return bool(airports) and all(db.is_island(ap.iata) and ap.country != "IT" for ap in airports)


class _HttpFailure(Exception):
    """A request or response problem that has already been recorded in the health state."""

    def __init__(self, status: Optional[int], message: str):
        super().__init__(message)
        self.status = status


class _RateLimiter:
    """Process-wide spacing between FlixBus requests."""

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
class _Option:
    """One bookable connection of a search, before the caller's filters."""
    dep_mins: int
    departure_time: str
    arrival_time: str
    duration_mins: int
    price_eur: float
    operator: str
    mode: str
    transfers: int
    origin_station_name: Optional[str]
    dest_station_name: Optional[str]


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _station_name(stations: dict, station_id: object) -> Optional[str]:
    station = stations.get(station_id) if isinstance(station_id, str) else None
    name = station.get("name") if isinstance(station, dict) else None
    return str(name) if name else None


def _option_from_result(res: object, date_str: str, brands: dict, stations: dict) -> Optional[_Option]:
    """Parse one search result. None for sold-out, unavailable, unpriced or other-day trips."""
    if not isinstance(res, dict) or res.get("status") != "available":
        return None
    dep = res.get("departure") or {}
    arr = res.get("arrival") or {}
    dep_iso = str(dep.get("date") or "")
    if dep_iso[:10] != date_str:
        return None  # the API also returns the following day
    legs = res.get("legs") or []
    if not isinstance(legs, list) or not legs:
        return None
    if (res.get("available") or {}).get("seats") == 0:
        return None
    price = res.get("price") or {}
    fare = _number(price.get("total_with_platform_fee"))
    if fare is None:
        fare = _number(price.get("total"))
    if fare is None or fare <= 0:
        return None
    duration = res.get("duration") or {}
    hours, minutes = _number(duration.get("hours")), _number(duration.get("minutes"))
    if hours is None or minutes is None:
        return None
    duration_mins = int(hours) * 60 + int(minutes)
    if duration_mins <= 0:
        return None

    dep_hhmm = dep_iso[11:16]
    arr_hhmm = str(arr.get("date") or "")[11:16]
    dep_mins = _minutes(dep_hhmm)
    _minutes(arr_hhmm)  # validates the arrival time

    names: List[str] = []
    for leg in legs:
        brand = brands.get(leg.get("brand_id")) if isinstance(leg, dict) else None
        name = str(brand.get("name")) if isinstance(brand, dict) and brand.get("name") else "FlixBus"
        if name not in names:
            names.append(name)
    all_train = all(isinstance(leg, dict) and leg.get("means_of_transport") == "train" for leg in legs)
    return _Option(
        dep_mins=dep_mins,
        departure_time=dep_hhmm,
        arrival_time=arr_hhmm,
        duration_mins=duration_mins,
        price_eur=round(fare, 2),
        operator=" / ".join(names),
        mode="train" if all_train else "bus",
        transfers=len(legs) - 1,
        origin_station_name=_station_name(stations, dep.get("station_id")),
        dest_station_name=_station_name(stations, arr.get("station_id")),
    )


def _parse_search(payload: object, date_str: str) -> List[_Option]:
    """All available connections of one search response. Raises ValueError on an unexpected shape."""
    if not isinstance(payload, dict) or not isinstance(payload.get("trips"), list):
        raise ValueError("search response has no trips list")
    brands = payload.get("brands") if isinstance(payload.get("brands"), dict) else {}
    stations = payload.get("stations") if isinstance(payload.get("stations"), dict) else {}
    options: List[_Option] = []
    for trip in payload["trips"]:
        results = trip.get("results") if isinstance(trip, dict) else None
        if not isinstance(results, dict):
            continue
        for res in results.values():
            opt = _option_from_result(res, date_str, brands, stations)
            if opt is not None:
                options.append(opt)
    return options


def _eur_to_try(amount: float) -> float:
    converted = fx.convert_to_try(amount, "EUR")
    if converted is None:
        converted = amount * fx.DEFAULT_EUR_TRY_RATE
    return round(converted, 2)


def _dedupe_options(options: List[_Option]) -> List[_Option]:
    """Keep one option per identical trip (same times, brand, mode and stations), the cheapest."""
    best: Dict[tuple, _Option] = {}
    for opt in options:
        key = (opt.departure_time, opt.arrival_time, opt.operator, opt.mode,
               opt.origin_station_name, opt.dest_station_name)
        current = best.get(key)
        if current is None or opt.price_eur < current.price_eur:
            best[key] = opt
    kept_ids = {id(opt) for opt in best.values()}
    return [opt for opt in options if id(opt) in kept_ids]


class FlixBusProvider:
    """FlixBus trips between two places on one day, with the lowest fare per connection.

    Segments from ``search`` have ``source="flixbus"``, ``is_estimated=False`` (real times),
    ``price_eur`` set to the fare including FlixBus's platform fee (``total_with_platform_fee``,
    falling back to ``total``), ``price_try`` converted with ``fx`` and ``price_is_estimated=False``.
    ``operator`` is the brand of the bus or train ("FlixBus", "FlixTrain", or a partner brand);
    ``mode`` is "train" only when every leg is a train, otherwise "bus".
    """

    def __init__(
        self,
        timeout: float = 10.0,
        cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS,
        min_interval_secs: Optional[float] = None,
    ):
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.min_interval_secs = _min_interval_from_env() if min_interval_secs is None else float(min_interval_secs)
        self._lock = threading.Lock()
        # folded query -> (stored_at, [(city_id, city_name), ...]), best match first; may be empty
        self._city_cache: Dict[str, Tuple[float, List[Tuple[str, str]]]] = {}
        # (from_city_id, to_city_id, date_str) -> (stored_at, options)
        self._search_cache: Dict[Tuple[str, str, str], Tuple[float, List[_Option]]] = {}
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
        max_results: int = 6,
        max_transfers: int = 1,
    ) -> Optional[List[ScheduledSegment]]:
        """FlixBus connections on ``date_str`` (YYYY-MM-DD), sorted by departure.

        Returns ``[]`` when both places resolve but nothing is bookable on that day, and ``None``
        when a place cannot be resolved, the date or time is malformed, or the service failed
        (network, HTTP 403/429, unexpected response). Sold-out trips and trips with more than
        ``max_transfers`` changes are skipped. ``earliest_dep_time`` ("HH:MM") drops earlier departures.

        A place that stands for several FlixBus cities (see ``MAX_CITIES_PER_PLACE`` and ``_pick_cities``)
        is searched for every city pair and the trips are merged; identical trips are kept once, the cheapest.
        A failed pair is skipped when another pair succeeded; the failure stays in ``health_status``.
        """
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
            earliest = _minutes(earliest_dep_time) if earliest_dep_time else None
        except (TypeError, ValueError):
            return None  # a malformed caller input is not a service failure
        try:
            return self._search(from_place, to_place, day, earliest, max_results, max_transfers)
        except _HttpFailure:
            return None  # already recorded in the health state
        except Exception as exc:  # the provider must never break the route engine
            logger.exception("FlixBus search failed for %s -> %s on %s", from_place, to_place, date_str)
            self._record_failure(None, f"unexpected error: {exc}")
            return None

    def can_resolve(self, place: str) -> bool:
        """True when the place names a FlixBus city. False on any error."""
        try:
            return bool(self._resolve_cities(place))
        except Exception:  # _HttpFailure is recorded already; anything else is treated the same
            return False

    def health_status(self) -> dict:
        """Current state of the service: status is unknown, ok, error or blocked."""
        with self._lock:
            blocked_now = self._blocked_until is not None and _now() < self._blocked_until
            if blocked_now:
                status = "blocked"
            elif self._last_error is not None:
                status = "blocked" if self._last_http_status in BLOCKED_STATUSES else "error"
            elif self._last_ok_at is not None:
                status = "ok"
            else:
                status = "unknown"
            return {
                "source": SOURCE,
                "status": status,
                "last_ok_at": self._last_ok_at,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at,
                "last_http_status": self._last_http_status,
                "consecutive_failures": self._consecutive_failures,
                "requests": self._requests,
                "blocked_cooldown_active": blocked_now,
                "cached_cities": len(self._city_cache),
                "cached_searches": len(self._search_cache),
            }

    # -- search steps --------------------------------------------------------

    def _search(
        self,
        from_place: str,
        to_place: str,
        day,
        earliest: Optional[int],
        max_results: int,
        max_transfers: int,
    ) -> Optional[List[ScheduledSegment]]:
        origins = self._resolve_cities(from_place)
        dests = self._resolve_cities(to_place)
        if not origins or not dests:
            return None
        if {city_id for city_id, _ in origins} & {city_id for city_id, _ in dests}:
            return []  # the same FlixBus city on both sides

        options: List[_Option] = []
        succeeded = 0
        failures: List[_HttpFailure] = []
        for origin_id, _origin_name in origins:
            for dest_id, _dest_name in dests:
                try:
                    options.extend(self._search_options(origin_id, dest_id, day.isoformat(), day.strftime("%d.%m.%Y")))
                    succeeded += 1
                except _HttpFailure as exc:
                    failures.append(exc)
        if succeeded == 0 and failures:
            raise failures[0]
        if failures:
            # The other pairs still count, but a later success would clear the error: keep it visible.
            self._note_error(failures[0])

        picked = [
            opt for opt in options
            if opt.transfers <= max_transfers and (earliest is None or opt.dep_mins >= earliest)
        ]
        picked = _dedupe_options(picked)
        picked.sort(key=lambda opt: opt.dep_mins)

        origin_name = str(from_place).strip()
        dest_name = str(to_place).strip()
        segments: List[ScheduledSegment] = []
        for opt in picked[:max(0, max_results)]:
            segments.append(ScheduledSegment(
                origin_station=origin_name,
                dest_station=dest_name,
                departure_time=opt.departure_time,
                arrival_time=opt.arrival_time,
                duration_mins=opt.duration_mins,
                price_try=_eur_to_try(opt.price_eur),
                operator=opt.operator,
                mode=opt.mode,
                date=day.isoformat(),
                is_estimated=False,
                price_eur=opt.price_eur,
                price_is_estimated=False,
                source=SOURCE,
                origin_station_name=opt.origin_station_name,
                dest_station_name=opt.dest_station_name,
            ))
        return segments

    def _resolve_city(self, place: object) -> Optional[Tuple[str, str]]:
        """(city_id, city_name) of the best FlixBus match for a place, or None when there is none."""
        cities = self._resolve_cities(place)
        return cities[0] if cities else None

    def _resolve_cities(self, place: object) -> List[Tuple[str, str]]:
        """Up to ``MAX_CITIES_PER_PLACE`` FlixBus cities for a place, best match first.

        An exact name match comes first; then cities whose leading words are the query's words
        ("Istanbul" -> "Istanbul Anadolu", "Istanbul Avrupa"; "Wiener Neustadt" does not match
        "Wien"). An empty list means FlixBus has no such city.

        Raises ``_HttpFailure`` when the autocomplete request fails (not cached).
        """
        raw = str(place or "").strip()
        key = _fold(raw)
        if not key:
            return []
        query = _ALIASES.get(key, raw)
        qkey = _fold(query)
        if _island_without_bus_service(query):
            return []  # no autocomplete request for an island that FlixBus cannot reach
        now = _now()
        with self._lock:
            hit = self._city_cache.get(qkey)
        if hit is not None and now - hit[0] < CITY_TTL_SECONDS:
            return list(hit[1])

        url = AUTOCOMPLETE_URL + "?" + urllib.parse.urlencode({
            "q": query, "lang": "en", "flixbus_cities_only": "false",
        })
        payload = self._get_json(url)
        if not isinstance(payload, list):
            raise self._record_failure(None, "autocomplete response is not a list")
        cities = [
            c for c in payload
            if isinstance(c, dict) and c.get("is_flixbus_city") is True and c.get("id") and c.get("name")
        ]
        chosen = _pick_cities(cities, qkey, _names_airport(qkey))
        result = [(str(c["id"]), str(c["name"])) for c in chosen]
        with self._lock:
            self._city_cache[qkey] = (now, result)
        return list(result)

    def _search_options(self, from_id: str, to_id: str, date_iso: str, date_flix: str) -> List[_Option]:
        key = (from_id, to_id, date_iso)
        now = _now()
        with self._lock:
            hit = self._search_cache.get(key)
        if hit is not None and now - hit[0] < self.cache_ttl:
            return hit[1]

        url = SEARCH_URL + "?" + urllib.parse.urlencode({
            "from_city_id": from_id,
            "to_city_id": to_id,
            "departure_date": date_flix,
            "products": '{"adult":1}',
            "currency": "EUR",
            "locale": "en",
            "search_by": "cities",
            "include_after_midnight_rides": "1",
        })
        payload = self._get_json(url)
        try:
            options = _parse_search(payload, date_iso)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise self._record_failure(None, f"unexpected search response: {exc}")
        with self._lock:
            self._search_cache[key] = (now, options)
        return options

    # -- HTTP and health -----------------------------------------------------

    def _get_json(self, url: str):
        with self._lock:
            blocked_until = self._blocked_until
        if blocked_until is not None and _now() < blocked_until:
            raise _HttpFailure(None, "cooling down after a blocked response")

        _limiter.reserve(self.min_interval_secs)
        with self._lock:
            self._requests += 1
        request = urllib.request.Request(url, headers=HEADERS)
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

    def _note_error(self, exc: _HttpFailure) -> None:
        """Put an already-counted failure back as the last error (no change to the failure count)."""
        with self._lock:
            self._last_error = str(exc)
            self._last_error_at = _iso_now()
            self._last_http_status = exc.status

    def _record_failure(self, status: Optional[int], message: str) -> _HttpFailure:
        """Record a failed call in the health state and return the exception to raise."""
        logger.warning("FlixBus: %s", message)
        with self._lock:
            self._consecutive_failures += 1
            self._last_error = message
            self._last_error_at = _iso_now()
            self._last_http_status = status
        return _HttpFailure(status, message)


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")
