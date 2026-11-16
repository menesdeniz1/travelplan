"""Google Maps transit timetables: real departure times from the Maps directions page.

``GoogleMapsTransitProvider.search`` opens ``https://www.google.com/maps/dir/<from>/<to>/data=...!3e3``
(transit) at the requested local departure (``!8j<epoch>``), waits for the options to render and reads
the page text. Each option becomes one ``ScheduledSegment`` (``source="google_maps"``):

* times: the departure and arrival clock times Maps shows, in local time. ``duration_mins`` is the
  absolute journey time Maps prints (it can cross midnight, and the arrival may be on another day);
* operator: the operator line (``FR``, ``TGV INOUI``, ``Kamil Koç``...). The mode (``train`` or ``bus``)
  comes from the operator and station words, see ``classify_mode``;
* fare: only when Maps prints one. A euro fare is converted with ``fx`` (``price_eur`` keeps the shown
  amount, ``price_is_estimated`` False). Without a shown fare ``price_try`` is None. No price is invented.

Skipped: options shorter than 30 minutes, metro or tram options, and options whose departure weekday
is not the weekday of the requested date (Maps showing another day).

Politeness: page loads are spaced ``min_interval_secs`` apart in this process, and they use the shared
browser pool and browser cap of ``google_flights_provider`` (``TRAVELPLAN_MAX_BROWSERS``). Results are
cached in memory for 30 minutes (``flight_cache`` has no generic key/value API). A block page (``/sorry/``
or the "unusual traffic" text) starts a 30 minute cooldown during which no page is loaded. Every page load
is charged to the caller's budget (``search(..., budget=)``, see ``rail_bus_provider.MapsBudget``); a refused
load gives None without a page load and without counting as a failure.

Nothing here raises: failures return None and are reported by ``health_status``. ``[]`` means Maps
answered with no transit route for the day.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from travel_engine import fx
from travel_engine.airports import UTC_FALLBACK, AirportDB
from travel_engine.providers import google_flights_provider as gf
from travel_engine.providers.rail_bus_provider import ScheduledSegment

logger = logging.getLogger(__name__)

SOURCE = "google_maps"
MAPS_DIR_URL = "https://www.google.com/maps/dir/{origin}/{destination}/data=!4m6!4m5!2m3!6e0!7e2!8j{epoch}!3e3?hl={hl}"
DEFAULT_HL = "en"
PAGE_WAIT_MS = 8500                 # the options render after domcontentloaded; 8-9 s observed
DEFAULT_MIN_INTERVAL_SECS = 8.0     # between two page loads of this process
CACHE_TTL_SECS = 30 * 60
BLOCK_COOLDOWN_SECS = 30 * 60
MIN_TRIP_MINS = 30
# One page lists about six options, and in a dense corridor they can all leave before the requested time
# (a 07:00 Madrid page showed 06:09 to 07:22). When a page leaves fewer options than this at or after the
# requested time, one more page is loaded from its last departure on: at most two page loads per search.
MIN_OPTIONS_PER_SEARCH = 3
FAILURE_THRESHOLD = 3               # consecutive failures before the source reports unhealthy
PAGE_JOB_TIMEOUT_SECS = 90.0
SLOT_WAIT_SECS = 120.0
MAX_CACHE_ENTRIES = 256
NO_OPERATOR = "Google Maps (operator not shown)"


class _BudgetExhausted(Exception):
    """The search's page-load budget refused a page load (not a failure: nothing is recorded)."""


def _now() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# -- text helpers -----------------------------------------------------------

def _fold(text: str) -> str:
    """Lower case without diacritics; dotless and dotted Turkish i map to i."""
    text = (text or "").replace("İ", "I").replace("ı", "i")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


_CLOCK = r"\d{1,2}:\d{2}(?:\s*[AaPp]\.?\s*[Mm]\.?)?"
_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?")
# "7:15 AM (Monday)—9:30 AM", "05:30 (Pazartesi)—12:15", "11:00 (Pazartesi)—00:30 (Salı)"
_TIME_RANGE_RE = re.compile(
    rf"(?P<dep>{_CLOCK})\s*(?:\((?P<dep_day>[^()]*)\))?\s*[—–-]\s*(?P<arr>{_CLOCK})\s*(?:\((?P<arr_day>[^()]*)\))?"
)
# "2 hr 15 min", "18 hr", "6 min", "14 sa. 30 dk.", "17 sa."
_DURATION_RE = re.compile(
    r"(?:(?P<days>\d+)\s*(?:days?|gün)\.?\s*)?"
    r"(?:(?P<hours>\d+)\s*(?:hrs?|hours?|h|sa|saat)\.?\s*)?"
    r"(?:(?P<mins>\d+)\s*(?:mins?|minutes?|m|dk|dakika)\.?)?",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(
    r"(?:from\s+|starting at\s+)?(?:(?P<sym>€|₺|TL)\s?(?P<amt>\d[\d.,]*)|(?P<amt2>\d[\d.,]*)\s?(?P<sym2>€|₺|TL))",
    re.IGNORECASE,
)
_FROM_RE = re.compile(r"^(?:\d{1,2}:\d{2}(?:\s*[AaPp][Mm])?\s+)?from\s+(?P<name>.+)$", re.IGNORECASE)
_FROM_TR_RE = re.compile(r"^(?P<name>.+?),\s*kalk[ıi]ş\b.*$", re.IGNORECASE)
# Lines that end the option list (the details panel, the "Explore" section) and lines never an operator.
_STOP_PREFIXES = ("details", "ayrıntılar", "ayrintilar", "buy tickets", "bilet satın al", "bilet satin al",
                  "explore", "keşfedin", "kesfedin", "send directions", "copy link")
_NOT_OPERATOR_PREFIXES = ("one-way price", "modified schedule", "sign in", "options", "seçenekler", "secenekler",
                          "more info", "buy ", "walk")

_WEEKDAYS = {
    "monday": 0, "pazartesi": 0, "tuesday": 1, "sali": 1, "wednesday": 2, "carsamba": 2,
    "thursday": 3, "persembe": 3, "friday": 4, "cuma": 4, "saturday": 5, "cumartesi": 5,
    "sunday": 6, "pazar": 6,
}

_BUS_PATTERNS = tuple(re.compile(p) for p in (
    r"\bflixbus\b", r"\barda ?tur\b", r"\bkamil ?koc\b", r"\bunion ivkoni\b", r"\bunibus\b",
    r"\bmetro turizm\b", r"\bpamukkale\b", r"\bvaran\b", r"\bulusoy\b", r"\bmegabus\b", r"\beurolines\b",
    r"\blux ?express\b", r"\becolines\b", r"\bblablacar\b", r"\bbus\b", r"\bcoach\b", r"\botobus\b", r"\botogar\b",
    r"\botogari\b", r"\bbusbahnhof\b", r"\bzob\b", r"\bautogare\b",
))
_TRAIN_PATTERNS = tuple(re.compile(p) for p in (
    r"\bfr\b", r"\btgv\b", r"\binoui\b", r"\bave\b", r"\bavlo\b", r"\balvia\b", r"\biryo\b", r"\bouigo\b",
    r"\bice\b", r"\brjx?\b", r"\brailjet\b", r"\bec\b", r"\bic\b", r"\beurocity\b", r"\beuronight\b",
    r"\bitalo\b", r"\btrenitalia\b", r"\bfrecc\w*\b", r"\bregiojet\b", r"\btrenord\b", r"\btreno\b", r"\btren\b",
    r"\beuromed\b", r"\bavant\b",
    r"\bbahn\b", r"\bsncf\b", r"\brenfe\b", r"\bflixtrain\b", r"\bhellenic train\b", r"\beurostar\b",
    r"\bthalys\b", r"\bwestbahn\b", r"\bsnalltaget\b", r"\brail\b", r"\brailway\b", r"\bobb\b",
    # Maps labels Spanish rail services by line code ("Madrid-Barcelona L1"): a heuristic, not an operator.
    r"\bl\d{1,2}\b",
))
_STATION_PATTERNS = tuple(re.compile(p) for p in (
    r"\bstation\b", r"\bstazione\b", r"\bcentrale\b", r"\bcentral\b", r"\btermini\b", r"\bsanta lucia\b",
    r"\bhbf\b", r"\bbahnhof\b", r"\bhauptbahnhof\b", r"\bgare\b", r"\batocha\b", r"\bkeleti\b", r"\bnyugati\b",
    r"\bporta nuova\b", r"\bgari\b", r"\bgar\b", r"\bmeidling\b", r"\bsirkeci\b", r"\bhaydarpasa\b",
    r"\bestacion\b", r"\bhl\.n\.?(?!\w)", r"\bhlavni nadrazi\b", r"\bcentraal\b",
))
_METRO_PATTERNS = tuple(re.compile(p) for p in (
    r"\bmetro\b", r"\bmetropolitana\b", r"\btram\b", r"\btramway\b", r"\btramvay\b", r"\bsubway\b",
    r"\bfunicular\b",
))
_BLOCK_MARKERS = ("unusual traffic", "are you a robot", "sira disi bir trafik", "robot oldugunuzu",
                  "robot olmadiginizi")
_NO_ROUTE_MARKERS = ("could not calculate", "no transit", "could not find")


_SPACE_CHARS = {" ": " ", " ": " ", " ": " ", " ": " "}


def _clean_line(line: str) -> str:
    """A rendered line without icon glyphs (Private Use characters) and with plain spaces."""
    kept = "".join(_SPACE_CHARS.get(ch, ch) for ch in line if unicodedata.category(ch) != "Co")
    return re.sub(r"\s+", " ", kept).strip()


def _amount(text: str) -> float:
    """"29.90", "29,90", "1.234", "1.234,50" and "1,234.50" as a float."""
    s = text.strip()
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", "") if len(s.rsplit(",", 1)[1]) == 3 else s.replace(",", ".")
    elif "." in s and len(s.rsplit(".", 1)[1]) == 3:
        s = s.replace(".", "")
    return float(s)


def _clock_minutes(text: Optional[str]) -> Optional[int]:
    """Minutes after midnight of "7:15 AM", "05:30" or "12:15 PM"; None when it is not a clock time."""
    m = _CLOCK_RE.fullmatch((text or "").strip())
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm.lower() == "p" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _duration_minutes(text: str) -> Optional[int]:
    """Minutes of a duration line ("2 hr 15 min", "14 sa. 30 dk."); None for any other line."""
    raw = (text or "").strip()
    if not raw:
        return None
    m = _DURATION_RE.fullmatch(raw)
    if not m or not any(m.group(g) for g in ("days", "hours", "mins")):
        return None
    return int(m.group("days") or 0) * 1440 + int(m.group("hours") or 0) * 60 + int(m.group("mins") or 0)


def _weekday(text: Optional[str]) -> Optional[int]:
    return _WEEKDAYS.get(_fold(text or "").strip())


def _price(line: str) -> Optional[Tuple[str, float]]:
    """("EUR" | "TRY", amount) for a fare line such as "€29.90"; None otherwise."""
    m = _PRICE_RE.fullmatch(line.strip())
    if not m:
        return None
    sym = m.group("sym") or m.group("sym2")
    try:
        value = _amount(m.group("amt") or m.group("amt2"))
    except (TypeError, ValueError):
        return None
    return ("EUR" if sym == "€" else "TRY"), value


def _station_name(raw: str) -> str:
    """"Madrid Puerta de Atocha - Almudena Grandes" -> "Madrid Puerta de Atocha"."""
    return re.split(r"\s+-\s+|,", raw)[0].strip()


def _from_station(line: str) -> Optional[str]:
    m = _FROM_RE.match(line) or _FROM_TR_RE.match(line)
    return _station_name(m.group("name")) if m else None


def classify_mode(text: str) -> Optional[str]:
    """"bus" or "train" from operator and station words; None when the text has neither."""
    folded = _fold(text)
    if any(p.search(folded) for p in _BUS_PATTERNS):
        return "bus"
    if any(p.search(folded) for p in _TRAIN_PATTERNS):
        return "train"
    if any(p.search(folded) for p in _STATION_PATTERNS):
        return "train"
    return None


def _option_body(lines: List[str]) -> List[str]:
    body = []
    for line in lines:
        folded = _fold(line)
        if folded.startswith(_STOP_PREFIXES):
            break
        body.append(line)
    return body


def _rendered_lines(text: str) -> List[str]:
    return [cleaned for cleaned in (_clean_line(line) for line in (text or "").splitlines()) if cleaned]


def _option_starts(lines: List[str]) -> List[int]:
    """Indexes of the option headers: a duration line followed by a departure-arrival line."""
    return [
        i for i in range(len(lines) - 1)
        if _duration_minutes(lines[i]) is not None and _TIME_RANGE_RE.fullmatch(lines[i + 1])
    ]


def has_options(text: str) -> bool:
    """True when the rendered page lists at least one transit option (whatever its date or length)."""
    return bool(_option_starts(_rendered_lines(text)))


def parse_departures(text: str, *, date_str: str, origin: str, dest: str) -> List[ScheduledSegment]:
    """Options of a rendered Maps transit page as segments, sorted by departure. Pure: no network."""
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return []
    lines = _rendered_lines(text)
    starts = _option_starts(lines)
    segments: List[ScheduledSegment] = []
    seen = set()
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        option = _parse_option(lines[start], lines[start + 1], _option_body(lines[start + 2:end]), day)
        if option is None:
            continue
        dep, arr, duration, mode, operator, from_name, price = option
        key = (dep, arr, operator, mode)
        if key in seen:
            continue
        seen.add(key)
        price_try, price_eur = _fare(price)
        segments.append(ScheduledSegment(
            origin_station=origin,
            dest_station=dest,
            departure_time=_hhmm(dep),
            arrival_time=_hhmm(arr),
            duration_mins=duration,
            price_try=price_try,
            operator=operator,
            mode=mode,
            date=date_str,
            is_estimated=False,
            price_eur=price_eur,
            price_is_estimated=False,
            source=SOURCE,
            origin_station_name=from_name,
            dest_station_name=None,
        ))
    segments.sort(key=lambda seg: _clock_minutes(seg.departure_time) or 0)
    return segments


def _parse_option(duration_line: str, range_line: str, body: List[str], day) -> Optional[tuple]:
    """(dep, arr, duration, mode, operator, from station, fare) of one option, None when it is skipped."""
    m = _TIME_RANGE_RE.fullmatch(range_line)
    dep, arr = _clock_minutes(m.group("dep")), _clock_minutes(m.group("arr"))
    if dep is None or arr is None:
        return None
    dep_day = _weekday(m.group("dep_day")) if m.group("dep_day") else None
    if dep_day is not None and dep_day != day.weekday():
        return None  # Maps is showing another day
    duration = _duration_minutes(duration_line) or 0
    if duration < MIN_TRIP_MINS:
        return None

    from_name: Optional[str] = None
    operator_line: Optional[str] = None
    fare: Optional[Tuple[str, float]] = None
    for line in body:
        if _TIME_RANGE_RE.fullmatch(line) or _duration_minutes(line) is not None:
            continue  # the walking or transfer time under an option
        found = _price(line)
        if found is not None:
            fare = fare or found
            continue
        station = _from_station(line)
        if station is not None:
            from_name = from_name or station
            continue
        if operator_line is None and not _fold(line).startswith(_NOT_OPERATOR_PREFIXES):
            operator_line = re.sub(r"\s+", " ", line)

    text = " | ".join(body)
    mode = classify_mode(text)
    if mode is None:
        folded = _fold(text)
        if any(p.search(folded) for p in _METRO_PATTERNS):
            return None  # metro or tram only: not a ground connection
        mode = "train" if any(p.search(folded) for p in _STATION_PATTERNS) else "bus"
    return dep, arr, duration, mode, operator_line or NO_OPERATOR, from_name, fare


def _fare(fare: Optional[Tuple[str, float]]) -> Tuple[Optional[float], Optional[float]]:
    """(price_try, price_eur) of a shown fare. Euro fares are converted; no fare gives (None, None)."""
    if fare is None:
        return None, None
    currency, amount = fare
    if currency == "TRY":
        return round(amount, 2), None
    converted = fx.convert_to_try(amount, "EUR")
    return (round(converted, 2) if converted is not None else None), round(amount, 2)


# -- URL and browser --------------------------------------------------------

def build_url(origin: str, destination: str, epoch: int, hl: str = DEFAULT_HL, gl: Optional[str] = None) -> str:
    """Transit directions page for one local departure (``epoch`` = Unix time)."""
    url = MAPS_DIR_URL.format(
        origin=urllib.parse.quote(origin.strip(), safe=""),
        destination=urllib.parse.quote(destination.strip(), safe=""),
        epoch=int(epoch),
        hl=hl,
    )
    return f"{url}&gl={gl}" if gl else url


def _route(route) -> None:
    request = route.request
    if gf.should_block_request(request.url, request.resource_type):
        route.abort()
    else:
        route.continue_()


_CONSENT_LABELS = ("Reject all", "Tümünü reddet", "Alle ablehnen", "Tout refuser", "Rifiuta tutto", "Rechazar todo")


def _reject_consent(page) -> bool:
    """Click a "reject all" consent button when Google shows the consent wall. True when one was clicked."""
    for label in _CONSENT_LABELS:
        try:
            button = page.get_by_role("button", name=label)
            if button.count():
                button.first.click(timeout=3000)
                page.wait_for_timeout(2000)
                return True
        except Exception:
            continue
    return False


def _page_job(browser: Any, url: str, locale: str, timezone_id: str, wait_ms: int) -> Dict[str, Any]:
    """One page load on the shared browser. Page errors come back as {"error": ...} so the browser survives."""
    context = None
    try:
        context = browser.new_context(
            user_agent=gf.CONTEXT_OPTIONS["user_agent"],
            locale=locale,
            timezone_id=timezone_id,
            viewport={"width": 1366, "height": 900},
        )
        context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = context.new_page()
        page.route("**/*", _route)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if "consent." in page.url and _reject_consent(page):
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(wait_ms)
        return {"final_url": page.url, "text": page.inner_text("body")}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        if context is not None:
            gf._close_quietly(context, "close")


def _playwright_fetch(url: str, *, locale: str, timezone_id: str, wait_ms: int) -> Tuple[str, str]:
    """(final url, rendered body text) of a Maps page, on the shared browser pool and browser cap."""
    slots = gf._browser_slots()
    if not slots.acquire(timeout=SLOT_WAIT_SECS):
        raise RuntimeError("no browser slot free")
    try:
        result = gf._get_browser_worker().run(
            lambda browser: _page_job(browser, url, locale, timezone_id, wait_ms),
            timeout=PAGE_JOB_TIMEOUT_SECS,
        )
    finally:
        slots.release()
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["final_url"], result["text"]


# -- shared state (one per process: pacing, cooldown, cache, health) --------

class _Shared:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.next_page_at = 0.0
        self.blocked_until = 0.0
        self.cache: Dict[tuple, Tuple[float, List[ScheduledSegment]]] = {}
        self.requests = 0
        self.last_ok_at: Optional[str] = None
        self.last_status: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_error_at: Optional[str] = None
        self.consecutive_failures = 0


_SHARED = _Shared()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _reserve_page_slot(min_interval: float) -> None:
    now = _now()
    with _SHARED.lock:
        start = max(now, _SHARED.next_page_at)
        _SHARED.next_page_at = start + max(0.0, min_interval)
        _SHARED.requests += 1
    if start > now:
        _sleep(start - now)


def _blocked_remaining() -> float:
    with _SHARED.lock:
        return max(0.0, _SHARED.blocked_until - _now())


def _start_cooldown(seconds: float) -> None:
    with _SHARED.lock:
        _SHARED.blocked_until = _now() + seconds
    logger.warning("Google Maps blocked the timetable lookup; no page loads for %.0f s", seconds)


def _record_ok() -> None:
    with _SHARED.lock:
        _SHARED.last_ok_at = _iso_now()
        _SHARED.last_status = "ok"
        _SHARED.consecutive_failures = 0


def _record_failure(status: str, message: str) -> None:
    logger.warning("Google Maps timetable: %s", message)
    with _SHARED.lock:
        _SHARED.last_status = status
        _SHARED.last_error = message
        _SHARED.last_error_at = _iso_now()
        _SHARED.consecutive_failures += 1


def _cache_get(key: tuple, ttl: float) -> Optional[List[ScheduledSegment]]:
    with _SHARED.lock:
        entry = _SHARED.cache.get(key)
        if entry is None:
            return None
        if _now() - entry[0] >= ttl:
            _SHARED.cache.pop(key, None)
            return None
        return entry[1]


def _cache_put(key: tuple, segments: List[ScheduledSegment]) -> None:
    with _SHARED.lock:
        _SHARED.cache.pop(key, None)
        _SHARED.cache[key] = (_now(), segments)
        while len(_SHARED.cache) > MAX_CACHE_ENTRIES:
            _SHARED.cache.pop(next(iter(_SHARED.cache)))


def _is_block_page(final_url: str, text: str) -> bool:
    if "/sorry/" in (final_url or ""):
        return True
    folded = _fold(text or "")
    return any(marker in folded for marker in _BLOCK_MARKERS)


def _has_no_route_message(text: str) -> bool:
    folded = _fold(text or "")
    return any(marker in folded for marker in _NO_ROUTE_MARKERS)


# -- provider ---------------------------------------------------------------

class GoogleMapsTransitProvider:
    """Google Maps transit options between two places on one day, as ``ScheduledSegment``.

    ``search`` returns the options that depart on ``date_str`` at or after ``earliest_dep_time``
    (default 00:00), sorted by departure and capped at ``max_results``. ``[]`` means Maps answered
    with no transit route; ``None`` means the lookup failed, is blocked or cannot be timed (the
    origin's time zone is unknown). ``fetch_page`` replaces the browser in tests:
    ``fetch_page(url, locale=..., timezone_id=..., wait_ms=...) -> (final_url, text)``.
    """

    def __init__(
        self,
        *,
        airport_db: Optional[AirportDB] = None,
        fetch_page: Optional[Callable[..., Tuple[str, str]]] = None,
        min_interval_secs: float = DEFAULT_MIN_INTERVAL_SECS,
        cache_ttl_secs: float = CACHE_TTL_SECS,
        blocked_cooldown_secs: float = BLOCK_COOLDOWN_SECS,
        wait_ms: int = PAGE_WAIT_MS,
    ):
        self._airport_db = airport_db
        self._fetch_page = fetch_page or _playwright_fetch
        self.min_interval_secs = float(min_interval_secs)
        self.cache_ttl_secs = float(cache_ttl_secs)
        self.blocked_cooldown_secs = float(blocked_cooldown_secs)
        self.wait_ms = int(wait_ms)

    @staticmethod
    def reset_shared_state() -> None:
        """Forget pacing, cooldown, cache and health of this process (tests)."""
        global _SHARED
        _SHARED = _Shared()

    def can_resolve(self, place: str) -> bool:
        """Any non-empty name: Maps does its own geocoding. The origin's time zone is checked in search."""
        return bool(str(place or "").strip())

    def search(
        self,
        from_place: str,
        to_place: str,
        date_str: str,
        earliest_dep_time: Optional[str] = None,
        from_coord: Optional[Tuple[float, float]] = None,
        to_coord: Optional[Tuple[float, float]] = None,
        max_results: int = 6,
        budget: Optional[Any] = None,
    ) -> Optional[List[ScheduledSegment]]:
        """``budget`` (a ``MapsBudget`` or any object with ``take() -> bool``) is charged one unit per page
        load. When it refuses one, the answer is None, as for a failed lookup, but nothing is recorded as a
        failure and no page is loaded."""
        try:
            return self._search(from_place, to_place, date_str, earliest_dep_time, max_results, budget)
        except _BudgetExhausted:
            return None
        except Exception as exc:  # the provider must never break the route engine
            logger.exception("Google Maps timetable failed for %s -> %s", from_place, to_place)
            _record_failure("error", f"unexpected error: {exc}")
            return None

    def health_status(self) -> Dict[str, Any]:
        """Current state. Makes no request."""
        remaining = _blocked_remaining()
        with _SHARED.lock:
            status = "blocked" if remaining > 0 else (_SHARED.last_status or "unknown")
            failures = _SHARED.consecutive_failures
            return {
                "source": SOURCE,
                "status": status,
                "healthy": remaining <= 0 and failures < FAILURE_THRESHOLD,
                "last_ok_at": _SHARED.last_ok_at,
                "last_error": _SHARED.last_error,
                "last_error_at": _SHARED.last_error_at,
                "consecutive_failures": failures,
                "requests": _SHARED.requests,
                "cached_searches": len(_SHARED.cache),
                "blocked_cooldown_remaining_secs": round(remaining, 1),
                "min_interval_secs": self.min_interval_secs,
            }

    # -- internals -----------------------------------------------------------

    def _search(self, from_place, to_place, date_str, earliest_dep_time, max_results, budget=None):
        origin = str(from_place or "").strip()
        dest = str(to_place or "").strip()
        if not origin or not dest:
            return None
        if origin.casefold() == dest.casefold():
            return []
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except (TypeError, ValueError):
            return None
        start = earliest_dep_time or "00:00"
        start_mins = _clock_minutes(start)
        if start_mins is None:
            return None
        if max_results <= 0:
            return []
        request_start = _hhmm(start_mins)
        key = (origin.casefold(), dest.casefold(), date_str, request_start)
        segments = _cache_get(key, self.cache_ttl_secs)
        if segments is None:
            if _blocked_remaining() > 0:
                return None
            segments = self._load(origin, dest, date_str, request_start, budget)
            if segments is None:
                return None
            _cache_put(key, segments)
        earliest = _clock_minutes(earliest_dep_time) if earliest_dep_time else 0
        out = [replace(seg) for seg in segments if (_clock_minutes(seg.departure_time) or 0) >= (earliest or 0)]
        return out[:max_results]

    def _load(self, origin: str, dest: str, date_str: str, start: str,
              budget: Optional[Any] = None) -> Optional[List[ScheduledSegment]]:
        """Options from ``start`` on: the first page, and a second page from its last departure when it is sparse."""
        zone = self._zone(origin)
        if zone is None:
            logger.info("Google Maps timetable skipped: no time zone known for %s", origin)
            return None
        first = self._load_page(origin, dest, date_str, start, zone, budget)
        if not first:  # a failure (already recorded) or no route at all
            return first
        start_mins = _clock_minutes(start) or 0
        last_mins = max(_clock_minutes(seg.departure_time) or 0 for seg in first)
        from_start = sum(1 for seg in first if (_clock_minutes(seg.departure_time) or 0) >= start_mins)
        if from_start >= MIN_OPTIONS_PER_SEARCH or last_mins + 1 >= 23 * 60:
            return first
        try:
            more = self._load_page(origin, dest, date_str, _hhmm(last_mins + 1), zone, budget)
        except _BudgetExhausted:
            return first  # no budget for a second page: the first page is still a valid answer
        if not more:  # the first page is still a valid answer
            return first
        seen = {(seg.departure_time, seg.arrival_time, seg.operator, seg.mode) for seg in first}
        merged = list(first) + [seg for seg in more if (seg.departure_time, seg.arrival_time, seg.operator, seg.mode) not in seen]
        merged.sort(key=lambda seg: _clock_minutes(seg.departure_time) or 0)
        return merged

    def _load_page(self, origin: str, dest: str, date_str: str, start: str, zone: str,
                   budget: Optional[Any] = None) -> Optional[List[ScheduledSegment]]:
        """One Maps page for a departure time. None on failure (recorded); [] when Maps has no route.

        Raises ``_BudgetExhausted`` before any work when ``budget`` refuses the page load."""
        if budget is not None and not budget.take():
            raise _BudgetExhausted()
        from travel_engine.google_budget import get_google_budget, get_current_priority
        if not get_google_budget().take(get_current_priority()):
            _record_failure("budget_exhausted", "Google budget limit exceeded")
            return None
        local = datetime.strptime(f"{date_str} {start}", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(zone))
        url = build_url(origin, dest, int(local.timestamp()), DEFAULT_HL, self._country(origin))
        _reserve_page_slot(self.min_interval_secs)
        try:
            final_url, text = self._fetch_page(url, locale="en-US", timezone_id=zone, wait_ms=self.wait_ms)
        except Exception as exc:
            _record_failure("error", f"page load failed ({type(exc).__name__}: {exc})")
            return None
        if _is_block_page(final_url, text):
            _start_cooldown(self.blocked_cooldown_secs)
            _record_failure("blocked", "block page returned")
            return None
        segments = parse_departures(text, date_str=date_str, origin=origin, dest=dest)
        # Options on the page that all fall on another day or are too short are a valid empty answer.
        if not segments and not has_options(text) and not _has_no_route_message(text):
            _record_failure("parse_error", f"no transit options on the page for {origin} -> {dest}")
            return None
        _record_ok()
        return segments

    def _airports(self) -> AirportDB:
        if self._airport_db is None:
            self._airport_db = AirportDB.load_default()
        return self._airport_db

    def _zone(self, place: str) -> Optional[str]:
        zone = self._airports().timezone(place)
        return None if zone == UTC_FALLBACK else zone

    def _country(self, place: str) -> Optional[str]:
        airports = self._airports().resolve(place)
        country = getattr(airports[0], "country", "") if airports else ""
        return country.lower() if country and len(country) == 2 else None
