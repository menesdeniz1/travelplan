"""Trenitalia schedules and fares from the public lefrecce.it endpoints behind trenitalia.com.

No login and no API key. Two undocumented endpoints are used, both with the header ``channel: 41``:

* ``GET https://www.lefrecce.it/Channels.Website.BFF.WEB/website/locations/search?name=<name>&limit=<n>``
  resolves a station name to a station id: ``[{"id": 830001700, "name": "Milano Centrale", ...}]``.
  Results are cached for 7 days.
* ``POST https://www.lefrecce.it/Channels.Website.BFF.WEB/website/ticket/solutions`` with a JSON body
  returns the trains of one search, sorted by departure. Each ``solution`` has the local departure and
  arrival times (with the station's UTC offset), the train list, the node list (one node per train, so
  changes = len(nodes) - 1) and ``price`` ({"currency": "€", "amount": 22.4}, or null when no fare is shown).

The site runs a bot manager. Requests are spaced at least ``min_interval_secs`` apart within the process,
results are cached in memory, and an HTTP 403, an HTTP 429 or an HTTP 400 whose body says "riprova"
puts every instance in the process on a 15 minute cooldown. Every failure makes the public methods
return ``None`` (``can_resolve`` returns False); nothing is raised. ``health_status()`` reports the state.

Only Italian places (Italy, San Marino, Vatican City) reach lefrecce.it. Any other place is refused by
``is_italian_place`` (AirportDB, no network) before a request is made. ``can_resolve`` caches its answers,
negative ones included; failed lookups are not cached.

A missing fare is never estimated: the segment's ``price_try`` stays None.
"""

import gzip
import json
import logging
import math
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from travel_engine import fx
from travel_engine.airports import AirportDB, _city_table_country
from travel_engine.providers.rail_bus_provider import ScheduledSegment

logger = logging.getLogger(__name__)

SOURCE = "trenitalia"
CHANNEL = "41"
LOCATIONS_URL = "https://www.lefrecce.it/Channels.Website.BFF.WEB/website/locations/search"
SOLUTIONS_URL = "https://www.lefrecce.it/Channels.Website.BFF.WEB/website/ticket/solutions"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
ROME = ZoneInfo("Europe/Rome")
DEFAULT_CACHE_TTL_SECONDS = 30 * 60
LOCATION_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_MIN_INTERVAL_SECS = 1.0
BLOCK_COOLDOWN_SECONDS = 15 * 60
BLOCKED_STATUSES = frozenset({403, 429})
LOCATION_LIMIT = 10
SOLUTIONS_LIMIT = 10
EUR_SYMBOLS = ("€", "EUR")

# Monotonic clock and sleep, module level so tests can replace them.
_now = time.monotonic
_sleep = time.sleep

# Process-wide state: one request spacing and one cooldown for every instance.
_shared_lock = threading.Lock()
_last_request_at: Optional[float] = None
_blocked_until: Optional[float] = None

_FOLD_MAP = str.maketrans({
    "ı": "i", "ł": "l", "đ": "d", "ø": "o", "ß": "ss", "æ": "ae", "œ": "oe", "þ": "th",
})

# Station words that win when a city has several stations (in this order).
_PREFERRED_STATION_WORDS = ("centrale", "termini", "s lucia", "s m novella")

# Names people write (Turkish, English) -> the Italian city name searched on lefrecce.it.
_PLACE_ALIASES_RAW: Dict[str, str] = {
    "Milan": "Milano", "Rome": "Roma", "Venedik": "Venezia", "Venice": "Venezia",
    "Floransa": "Firenze", "Florence": "Firenze", "Naples": "Napoli", "Turin": "Torino",
    "Genoa": "Genova", "Cenova": "Genova", "Ceneviz": "Genova", "Mestre": "Venezia",
}

# Verified station ids (lefrecce.it locations search, captured 19 Oct 2026). A city key is the
# folded query, so these skip the locations request. Every other place is looked up.
_KNOWN_CITY_STATIONS: Dict[str, Tuple[int, str]] = {
    "milano": (830001700, "Milano Centrale"),
    "venezia": (830002593, "Venezia S. Lucia"),
    "roma": (830008409, "Roma Termini"),
}


def _fold(text: str) -> str:
    """Lower-case, strip diacritics (ş->s, ğ->g, İ->i ...) and collapse punctuation."""
    text = unicodedata.normalize("NFKD", str(text).lower().translate(_FOLD_MAP))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


# Folded alias -> the Italian name sent to the locations search (e.g. "venedik" -> "Venezia").
_ALIASES: Dict[str, str] = {_fold(k): v for k, v in _PLACE_ALIASES_RAW.items()}

# Country codes whose places may be searched on lefrecce.it. San Marino and Vatican City have no airports,
# so their names are listed in _MICROSTATE_NAMES instead of being looked up in AirportDB.
ITALIAN_COUNTRIES = frozenset({"IT", "SM", "VA"})
_MICROSTATE_NAMES: Tuple[str, ...] = (
    "San Marino", "Citta del Vaticano", "Vaticano", "Vatican", "Vatican City", "Vatikan", "SM", "VA",
)
# Italian station towns that AirportDB and the city-centre table may not list. Folded when used.
_ITALIAN_TOWNS: Tuple[str, ...] = (
    "Mestre", "Monza", "Lecco", "Como", "Brescia", "Verona", "Padova", "Vicenza", "Treviso", "Udine",
    "Trieste", "Rovigo", "Ferrara", "Modena", "Reggio Emilia", "Parma", "Piacenza", "Cremona", "Pavia",
    "Novara", "Vercelli", "Alessandria", "Asti", "Cuneo", "Savona", "La Spezia", "Lucca", "Livorno",
    "Grosseto", "Siena", "Arezzo", "Prato", "Empoli", "Pistoia", "Perugia", "Foligno", "Terni", "Ancona",
    "Pescara", "Foggia", "Brindisi", "Taranto", "Matera", "Potenza", "Salerno", "Caserta", "Benevento",
    "Avellino", "Cosenza", "Catanzaro", "Reggio Calabria", "Messina", "Siracusa", "Sassari", "Bolzano",
    "Trento", "Rimini", "Forli", "Cesena", "Ravenna", "Faenza", "Latina", "Frosinone", "Cassino",
    "L'Aquila", "Campobasso", "Sorrento", "Afragola", "Monfalcone", "Gorizia", "Pordenone", "Belluno",
    "Aosta", "Ivrea", "Biella", "Varese", "Sesto San Giovanni", "Lodi", "Mantova", "Sondrio", "Merano",
    "Sanremo", "Ventimiglia", "Imperia", "Chiavari", "Bologna", "Firenze", "Pisa", "Bari", "Lecce",
    "Cagliari", "Catania", "Palermo", "Genova", "Torino", "Milano", "Roma", "Napoli", "Venezia",
)
# Folded names that are Italian with no lookup: every alias (all of them name Italian cities), the
# Italian spellings they map to, the seeded cities, the station towns and the microstates.
_ITALIAN_NAMES: frozenset = frozenset(
    set(_ALIASES) | {_fold(v) for v in _ALIASES.values()} | set(_KNOWN_CITY_STATIONS)
    | {_fold(n) for n in _ITALIAN_TOWNS + _MICROSTATE_NAMES}
)


def _country_of_name(name: str, db: AirportDB) -> Optional[str]:
    """ISO country of a folded city name: its airports' country, else the city-centre table's, else None."""
    airports = db.find_city(name)
    if airports:
        return airports[0].country
    return _city_table_country(name)


def is_italian_place(text: str) -> bool:
    """True for an Italian, San Marino or Vatican place name. Makes no network request.

    A known name is answered directly. Otherwise the longest leading part of the name that AirportDB
    knows decides the country: "Firenze S. M. Novella" -> "firenze" -> IT, "Paris Nord" -> "paris" -> FR.
    Unknown names are not Italian.
    """
    key = _fold(text)
    if not key:
        return False
    if key in _ITALIAN_NAMES:
        return True
    db = AirportDB.load_default()
    words = key.split()
    for cut in range(len(words), 0, -1):
        country = _country_of_name(" ".join(words[:cut]), db)
        if country is not None:
            return country in ITALIAN_COUNTRIES
    return False


def _minutes(hhmm: str) -> int:
    """'HH:MM' -> minutes after midnight. Raises ValueError for anything else."""
    parts = str(hhmm).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"bad time {hhmm!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time {hhmm!r}")
    return h * 60 + m


def _reset_shared_state_for_tests() -> None:
    global _last_request_at, _blocked_until
    with _shared_lock:
        _last_request_at = None
        _blocked_until = None


class _Failure(Exception):
    """A request or response problem that has already been recorded in the health state."""

    def __init__(self, status: Optional[int], message: str):
        super().__init__(message)
        self.status = status


def _reserve_slot(min_interval: float) -> None:
    """Space requests process-wide: sleep until ``min_interval`` has passed since the last one."""
    global _last_request_at
    with _shared_lock:
        now = _now()
        wait = 0.0
        if _last_request_at is not None and min_interval > 0:
            wait = max(0.0, _last_request_at + min_interval - now)
        _last_request_at = now + wait
    if wait > 0:
        _sleep(wait)


def _start_cooldown() -> None:
    global _blocked_until
    with _shared_lock:
        _blocked_until = _now() + BLOCK_COOLDOWN_SECONDS


def _cooldown_active() -> bool:
    with _shared_lock:
        return _blocked_until is not None and _now() < _blocked_until


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_read(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read() or b""
    except Exception:
        return b""


def _decode_body(raw: bytes) -> bytes:
    if raw[:2] == b"\x1f\x8b":  # gzip magic, in case the server ignores the missing Accept-Encoding
        return gzip.decompress(raw)
    return raw


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _pick_station(city_fold: str, candidates: List[Tuple[int, str]]) -> Optional[Tuple[int, str]]:
    """Choose the station for a city: exact name, then a preferred station word, then the first match.

    Only names that start with the city name count, so a far-off hit ("Istanbul" for an Italian
    query) never resolves.
    """
    matches = [
        (sid, name) for sid, name in candidates
        if _fold(name) == city_fold or _fold(name).startswith(city_fold + " ")
    ]
    if not matches:
        return None
    for sid, name in matches:
        if _fold(name) == city_fold:
            return sid, name
    for word in _PREFERRED_STATION_WORDS:
        for sid, name in matches:
            if word in _fold(name):
                return sid, name
    return matches[0]


def _parse_locations(payload: object) -> List[Tuple[int, str]]:
    if not isinstance(payload, list):
        raise ValueError("locations response is not a list")
    found: List[Tuple[int, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        sid, name = item.get("id"), item.get("name")
        if isinstance(sid, int) and not isinstance(sid, bool) and isinstance(name, str) and name.strip():
            found.append((sid, name.strip()))
    return found


def _local_time(value: object) -> datetime:
    """ISO timestamp with offset (e.g. "2026-11-09T13:25:00.000+01:00") -> Europe/Rome datetime."""
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"no offset in {value!r}")
    return parsed.astimezone(ROME)


def _euro_amount(price: object) -> Optional[float]:
    """The fare in EUR, or None when the service shows no fare or shows it in another currency."""
    if not isinstance(price, dict):
        return None
    if str(price.get("currency") or "").strip() not in EUR_SYMBOLS:
        return None
    amount = price.get("amount")
    if not _is_number(amount) or amount <= 0:
        return None
    return float(amount)


def _operator(trains: object) -> str:
    """Train acronyms and names joined with " + ", e.g. "RE 2627 + RV 3503"; "UB" when a name is missing."""
    parts: List[str] = []
    for train in trains if isinstance(trains, list) else []:
        if not isinstance(train, dict):
            continue
        acronym = str(train.get("acronym") or "").strip()
        name = train.get("name")
        name = str(name).strip() if name not in (None, "") else ""
        label = " ".join(part for part in (acronym, name) if part)
        if label:
            parts.append(label)
    return " + ".join(parts) or "Trenitalia"


def _changes(solution: dict) -> int:
    nodes = solution.get("nodes")
    if isinstance(nodes, list) and nodes:
        return len(nodes) - 1
    trains = solution.get("trains")
    return max(0, len(trains) - 1) if isinstance(trains, list) else 0


class TrenitaliaProvider:
    """Trenitalia trains between two Italian places on one day, with the fare when the service shows one.

    Segments from ``search`` have ``source="trenitalia"``, ``mode="train"``, ``is_estimated=False``
    (live times), ``price_eur`` set to the fare from the service, ``price_try`` converted with ``fx``
    and ``price_is_estimated=False``. A trip with no fare has ``price_eur`` and ``price_try`` None.
    ``operator`` joins the train acronyms and names ("FR 9731", "RE 2627 + RV 3503").
    """

    def __init__(
        self,
        timeout: float = 10.0,
        cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS,
        min_interval_secs: float = DEFAULT_MIN_INTERVAL_SECS,
    ):
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.min_interval_secs = min_interval_secs
        self._lock = threading.Lock()
        # folded city query -> (stored_at, [(station_id, station_name), ...]); may be empty
        self._location_cache: Dict[str, Tuple[float, List[Tuple[int, str]]]] = {}
        # (from_id, to_id, date, earliest, max_results, max_transfers) -> (stored_at, segments)
        self._search_cache: Dict[tuple, Tuple[float, List[ScheduledSegment]]] = {}
        # folded place -> (stored_at, can_resolve answer); only answers from a finished lookup are kept
        self._can_resolve_cache: Dict[str, Tuple[float, bool]] = {}
        self._requests = 0
        self._last_ok_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_error_at: Optional[str] = None
        self._last_http_status: Optional[int] = None
        self._consecutive_failures = 0

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
        """Trenitalia trains on ``date_str`` (YYYY-MM-DD), sorted by departure.

        Returns ``[]`` when both places resolve but no train matches, and ``None`` when a place cannot
        be resolved, the date or time is malformed, or the service failed (network, HTTP 403/429,
        cooldown, unexpected response). Trains with more than ``max_transfers`` changes are skipped.
        ``earliest_dep_time`` ("HH:MM") drops earlier departures.
        """
        try:
            datetime.strptime(str(date_str), "%Y-%m-%d")
            earliest = _minutes(earliest_dep_time) if earliest_dep_time else None
            max_results = int(max_results)
            max_transfers = int(max_transfers)
        except (TypeError, ValueError):
            return None  # a malformed caller input is not a service failure
        earliest_hhmm = f"{earliest // 60:02d}:{earliest % 60:02d}" if earliest is not None else ""
        try:
            return self._search(from_place, to_place, str(date_str), earliest_hhmm, max_results, max_transfers)
        except _Failure:
            return None  # already recorded in the health state
        except Exception as exc:  # the provider must never break the route engine
            logger.exception("Trenitalia search failed for %s -> %s on %s", from_place, to_place, date_str)
            self._record_failure(None, f"unexpected error: {exc}")
            return None

    def can_resolve(self, place: str) -> bool:
        """True when the place names an Italian station city (SM and VA included). False on any error.

        Non-Italian places answer False with no request. Answers are cached for ``LOCATION_TTL_SECONDS``,
        negative ones too; a failed lookup is not cached, so the next call tries again.
        """
        text = str(place or "").strip()
        key = _fold(text)
        if not key:
            return False
        now = _now()
        with self._lock:
            hit = self._can_resolve_cache.get(key)
        if hit is not None and now - hit[0] < LOCATION_TTL_SECONDS:
            return hit[1]
        try:
            answer = self._resolve_station(text) is not None
        except Exception:  # _Failure is recorded already; anything else is treated the same, uncached
            return False
        with self._lock:
            self._can_resolve_cache[key] = (now, answer)
        return answer

    def health_status(self) -> dict:
        """Current state of the service: status is unknown, ok, error or blocked."""
        blocked_now = _cooldown_active()
        with self._lock:
            if blocked_now or (self._last_error is not None and self._last_http_status in BLOCKED_STATUSES):
                status = "blocked"
            elif self._last_error is not None:
                status = "error"
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
                "cached_locations": len(self._location_cache),
                "cached_searches": len(self._search_cache),
            }

    # -- search steps --------------------------------------------------------

    def _search(
        self,
        from_place: str,
        to_place: str,
        date_str: str,
        earliest_hhmm: str,
        max_results: int,
        max_transfers: int,
    ) -> Optional[List[ScheduledSegment]]:
        origin = self._resolve_station(from_place)
        dest = self._resolve_station(to_place)
        if origin is None or dest is None or origin[0] == dest[0]:
            return None

        key = (origin[0], dest[0], date_str, earliest_hhmm, max_results, max_transfers)
        now = _now()
        with self._lock:
            hit = self._search_cache.get(key)
        if hit is not None and now - hit[0] < self.cache_ttl:
            return list(hit[1])

        body = {
            "departureLocationId": origin[0],
            "arrivalLocationId": dest[0],
            "departureTime": f"{date_str}T{earliest_hhmm or '00:00'}:00.000",
            "adults": 1,
            "children": 0,
            "criteria": {
                "frecceOnly": False,
                "regionalOnly": False,
                "intercityOnly": False,
                "tourismOnly": False,
                "noChanges": False,
                "order": "DEPARTURE_DATE",
                "offset": 0,
                "limit": max(SOLUTIONS_LIMIT, max_results),
            },
            "advancedSearchRequest": {"bestFare": False, "bikeFilter": False, "forwardDiscountCodes": []},
        }
        payload = self._request(SOLUTIONS_URL, body=body)
        try:
            segments = self._parse_solutions(payload, from_place, to_place, date_str, earliest_hhmm, max_transfers)
        except ValueError as exc:
            raise self._record_failure(200, f"unexpected response: {exc}")
        segments = segments[:max_results]
        with self._lock:
            self._search_cache[key] = (now, segments)
        return list(segments)

    def _parse_solutions(
        self,
        payload: object,
        from_place: str,
        to_place: str,
        date_str: str,
        earliest_hhmm: str,
        max_transfers: int,
    ) -> List[ScheduledSegment]:
        if not isinstance(payload, dict) or not isinstance(payload.get("solutions"), list):
            raise ValueError("no solutions list")
        earliest = _minutes(earliest_hhmm) if earliest_hhmm else None
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
        found: List[ScheduledSegment] = []
        for item in payload["solutions"]:
            solution = item.get("solution") if isinstance(item, dict) else None
            if not isinstance(solution, dict):
                continue
            segment = self._parse_solution(solution, from_place, to_place, day, earliest, max_transfers)
            if segment is not None:
                found.append(segment)
        found.sort(key=lambda seg: seg.departure_time)
        return found

    def _parse_solution(
        self,
        solution: dict,
        from_place: str,
        to_place: str,
        day,
        earliest: Optional[int],
        max_transfers: int,
    ) -> Optional[ScheduledSegment]:
        try:
            dep = _local_time(solution.get("departureTime"))
            arr = _local_time(solution.get("arrivalTime"))
        except (TypeError, ValueError):
            return None
        if dep.date() != day:
            return None
        if earliest is not None and dep.hour * 60 + dep.minute < earliest:
            return None
        if _changes(solution) > max_transfers:
            return None
        duration = int((arr - dep).total_seconds() // 60)
        if duration <= 0:
            return None

        amount = _euro_amount(solution.get("price"))
        price_try = None
        if amount is not None:
            converted = fx.convert_to_try(amount, "EUR")
            price_try = round(converted, 2) if converted is not None else None

        origin_name = solution.get("origin")
        dest_name = solution.get("destination")
        return ScheduledSegment(
            origin_station=from_place,
            dest_station=to_place,
            departure_time=dep.strftime("%H:%M"),
            arrival_time=arr.strftime("%H:%M"),
            duration_mins=duration,
            price_try=price_try,
            operator=_operator(solution.get("trains")),
            mode="train",
            date=dep.strftime("%Y-%m-%d"),
            is_estimated=False,
            price_eur=amount,
            price_is_estimated=False,
            source=SOURCE,
            origin_station_name=str(origin_name) if origin_name else None,
            dest_station_name=str(dest_name) if dest_name else None,
        )

    # -- station resolution --------------------------------------------------

    def _resolve_station(self, place: object) -> Optional[Tuple[int, str]]:
        """``(station_id, station_name)`` for an Italian place, or None. Raises ``_Failure`` on a service error."""
        text = str(place or "").strip()
        if not text or not is_italian_place(text):
            return None  # other countries never reach lefrecce.it
        query = _ALIASES.get(_fold(text), text)
        city_fold = _fold(query)
        if not city_fold:
            return None
        known = _KNOWN_CITY_STATIONS.get(city_fold)
        if known is not None:
            return known
        return _pick_station(city_fold, self._location_candidates(query, city_fold))

    def _location_candidates(self, query: str, city_fold: str) -> List[Tuple[int, str]]:
        now = _now()
        with self._lock:
            hit = self._location_cache.get(city_fold)
        if hit is not None and now - hit[0] < LOCATION_TTL_SECONDS:
            return list(hit[1])
        url = LOCATIONS_URL + "?" + urllib.parse.urlencode({"name": query, "limit": LOCATION_LIMIT})
        payload = self._request(url)
        try:
            found = _parse_locations(payload)
        except ValueError as exc:
            raise self._record_failure(200, f"unexpected locations response: {exc}")
        with self._lock:
            self._location_cache[city_fold] = (now, found)
        return list(found)

    # -- HTTP and health -----------------------------------------------------

    def _request(self, url: str, body: Optional[dict] = None):
        if _cooldown_active():
            raise _Failure(None, "cooling down after a blocked response")

        _reserve_slot(self.min_interval_secs)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "it-IT",
            "channel": CHANNEL,
        }
        data = None
        method = "GET"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with self._lock:
            self._requests += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:  # before URLError: it is a subclass
            status = exc.code
            if status in BLOCKED_STATUSES or (status == 400 and b"riprova" in _safe_read(exc).lower()):
                _start_cooldown()
            raise self._record_failure(status, f"HTTP {status}")
        except (urllib.error.URLError, OSError) as exc:
            raise self._record_failure(None, f"network error: {exc}")
        try:
            payload = json.loads(_decode_body(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, OSError):
            raise self._record_failure(200, "response is not JSON")
        self._record_ok()
        return payload

    def _record_ok(self) -> None:
        with self._lock:
            self._last_ok_at = _iso_now()
            self._last_error = None
            self._last_error_at = None
            self._last_http_status = 200
            self._consecutive_failures = 0

    def _record_failure(self, status: Optional[int], message: str) -> _Failure:
        """Record a failed call in the health state and return the exception to raise."""
        logger.warning("Trenitalia: %s", message)
        with self._lock:
            self._consecutive_failures += 1
            self._last_error = message
            self._last_error_at = _iso_now()
            self._last_http_status = status
        return _Failure(status, message)
