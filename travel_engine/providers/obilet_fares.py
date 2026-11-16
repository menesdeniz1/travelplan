"""obilet.com bus fares: indicative "from" prices per operator, read from the route pages.

A route page https://www.obilet.com/otobus-bileti/<from>-<to> carries, in an inline JSON object under
``"bottomTable"``, one row per operator: the lowest fare it shows for the route, the number of daily
departures and the usual duration. Only that HTML is read. The site's embedded API credential is never
used (owner decision).

``ObiletFromPrices.from_prices(from, to)`` returns ``{normalised operator name: lowest fare in TRY}``.
Rows priced in EUR (international routes) are converted with ``fx``. These are "from" prices: indicative
only, not a fare for a given day. A segment gets one only through ``price_from_try``, never as ``price_try``.

Politeness: pages are fetched at most once a second in this process and cached for 24 hours. A route page
that does not exist (HTTP 404) is cached as "no fares". Network and server errors are not cached.
Nothing here raises.

Route slugs: only the cities in ``CITY_SLUGS`` (Turkish cities, İstanbul and two international names with
their Turkish slugs) are mapped. Any other pair makes no request. ``istanbul-ankara``, ``istanbul-sofya`` and
``istanbul-selanik`` were checked against the live site; the other Turkish slugs follow the same pattern.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from travel_engine import fx

logger = logging.getLogger(__name__)

SOURCE = "obilet"
BASE_URL = "https://www.obilet.com/otobus-bileti/"
CACHE_TTL_SECS = 24 * 3600
MIN_INTERVAL_SECS = 1.0
REQUEST_TIMEOUT_SECS = 10.0
MAX_BYTES = 4_000_000
USER_AGENT = "TravelPlan/1.0 (personal, non-commercial)"
MAX_CACHE_ENTRIES = 512
FAILURE_THRESHOLD = 3

# Folded place name -> obilet route slug. İstanbul variants are handled in _slug.
CITY_SLUGS: Dict[str, str] = {
    "ankara": "ankara", "izmir": "izmir", "antalya": "antalya", "bursa": "bursa", "adana": "adana",
    "konya": "konya", "gaziantep": "gaziantep", "kayseri": "kayseri", "eskisehir": "eskisehir",
    "trabzon": "trabzon", "samsun": "samsun", "mersin": "mersin", "diyarbakir": "diyarbakir",
    "erzurum": "erzurum", "van": "van", "sanliurfa": "sanliurfa", "canakkale": "canakkale",
    "edirne": "edirne", "denizli": "denizli", "malatya": "malatya", "sivas": "sivas",
    "nevsehir": "nevsehir", "kahramanmaras": "kahramanmaras", "mugla": "mugla", "aydin": "aydin",
    "balikesir": "balikesir", "afyonkarahisar": "afyonkarahisar",
    # International: obilet uses the Turkish names ("Sofya" for Sofia, "Selanik" for Thessaloniki).
    # Live-checked international slugs (T16)
    "sofia": "sofya", "sofya": "sofya", "thessaloniki": "selanik", "selanik": "selanik",
    "dedeagac": "dedeagac", "alexandroupoli": "dedeagac", "alexandroupolis": "dedeagac",
    "filibe": "filibe", "plovdiv": "filibe",
}

# Operator names that differ only by these words are the same operator ("Arda Tur New" = "ArdaTur").
_NOISE_WORDS = frozenset({"new", "yeni", "ltd", "sti", "as", "a", "s", "ve"})
# Words that name the business rather than the operator (used only for the looser match).
_GENERIC_WORDS = frozenset({"turizm", "seyahat", "tur", "tourism", "travel", "yolcu", "tasimacilik",
                            "ulasim", "nakliyat", "otobus"})
_MIN_LOOSE_MATCH_CHARS = 5

_now = time.monotonic


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _fold(text: str) -> str:
    text = (text or "").replace("İ", "I").replace("ı", "i")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def route_slug(place: str) -> Optional[str]:
    """obilet slug of a city name ("İstanbul Havalimanı" -> "istanbul"); None when the city is not mapped."""
    name = re.split(r"[(,]", _fold(place))[0].strip()
    if not name:
        return None
    if name.startswith("istanbul"):
        return "istanbul"
    return CITY_SLUGS.get(name)


def route_url(from_place: str, to_place: str) -> Optional[str]:
    """Route page for two places, or None when either has no slug or both are the same city."""
    a, b = route_slug(from_place), route_slug(to_place)
    if a is None or b is None or a == b:
        return None
    return f"{BASE_URL}{a}-{b}"


def normalise_operator(name: str) -> str:
    """Lower-case, diacritics-free words of an operator name, without numbers and noise words.

    "Kamil Koç 81030" -> "kamil koc"; "Arda Tur New" and "ArdaTur" both give joined forms "ardatur".
    """
    tokens = [t for t in re.findall(r"[a-z0-9]+", _fold(name)) if not t.isdigit() and t not in _NOISE_WORDS]
    return " ".join(tokens)


def _core(key: str) -> set:
    tokens = set(key.split()) - _GENERIC_WORDS
    return tokens or set(key.split())


def price_for_operator(prices: Dict[str, float], operator: str) -> Optional[float]:
    """The "from" fare of one operator from ``from_prices``; None when no operator matches with certainty.

    Exact match after normalisation first (spacing ignored). Otherwise the operator's core words must be
    contained in one listed operator's core words, and the shorter name must be long enough. Several
    different fares matching the same name are ambiguous, so the result is None.
    """
    key = normalise_operator(operator)
    if not key or not prices:
        return None
    joined = key.replace(" ", "")
    for other, value in prices.items():
        if other.replace(" ", "") == joined:
            return value
    core = _core(key)
    candidates = set()
    for other, value in prices.items():
        other_joined = other.replace(" ", "")
        other_core = _core(other)
        if min(len(joined), len(other_joined)) < _MIN_LOOSE_MATCH_CHARS:
            continue
        if core <= other_core or other_core <= core:
            candidates.add(value)
    return next(iter(candidates)) if len(candidates) == 1 else None


_BOTTOM_TABLE_RE = re.compile(r'"bottomTable"\s*:\s*(?=\{)')


def parse_bottom_table(html: str) -> Optional[List[Dict[str, Any]]]:
    """Rows of the inline ``bottomTable`` JSON (Name, Count, Currency, Price, Duration); None when absent."""
    m = _BOTTOM_TABLE_RE.search(html or "")
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError:
        return None
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, list):
        return None
    return [row for row in data if isinstance(row, dict)]


def min_fares_try(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    """{normalised operator: lowest fare in TRY}. Rows without a usable price or currency are left out."""
    prices: Dict[str, float] = {}
    for row in rows:
        name, price = row.get("Name"), row.get("Price")
        if not isinstance(name, str) or not isinstance(price, (int, float)) or price <= 0:
            continue
        key = normalise_operator(name)
        if not key:
            continue
        try:
            amount = fx.convert_to_try(float(price), str(row.get("Currency") or "TRY"))
        except Exception:
            amount = None
        if amount is None:
            continue
        value = round(float(amount), 2)
        prices[key] = min(prices.get(key, value), value)
    return prices


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.5",
    })
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECS) as response:
        status = getattr(response, "status", 200)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        return response.read(MAX_BYTES).decode("utf-8", "replace")


class _Shared:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.next_fetch_at = 0.0
        self.cache: Dict[str, tuple] = {}
        self.requests = 0
        self.last_ok_at: Optional[str] = None
        self.last_status: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_error_at: Optional[str] = None
        self.consecutive_failures = 0


_SHARED = _Shared()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ObiletFromPrices:
    """Lowest fare per operator on an obilet route, as indicative "from" prices. Never raises."""

    def __init__(
        self,
        *,
        fetch_html: Optional[Callable[[str], str]] = None,
        min_interval_secs: float = MIN_INTERVAL_SECS,
        cache_ttl_secs: float = CACHE_TTL_SECS,
    ):
        self._fetch_html = fetch_html or _http_get
        self.min_interval_secs = float(min_interval_secs)
        self.cache_ttl_secs = float(cache_ttl_secs)

    @staticmethod
    def reset_shared_state() -> None:
        """Forget pacing, cache and health of this process (tests)."""
        global _SHARED
        _SHARED = _Shared()

    def from_prices(self, from_place: str, to_place: str) -> Dict[str, float]:
        """{normalised operator: lowest fare in TRY}, or {} when the route is unmapped, unknown or failed."""
        try:
            return self._from_prices(from_place, to_place)
        except Exception:
            logger.exception("obilet from-prices failed for %s -> %s", from_place, to_place)
            _record_failure("error", "unexpected error")
            return {}

    def health_status(self) -> Dict[str, Any]:
        """Current state. Makes no request."""
        with _SHARED.lock:
            failures = _SHARED.consecutive_failures
            return {
                "source": SOURCE,
                "status": _SHARED.last_status or "unknown",
                "healthy": failures < FAILURE_THRESHOLD,
                "last_ok_at": _SHARED.last_ok_at,
                "last_error": _SHARED.last_error,
                "last_error_at": _SHARED.last_error_at,
                "consecutive_failures": failures,
                "requests": _SHARED.requests,
                "cached_routes": len(_SHARED.cache),
                "min_interval_secs": self.min_interval_secs,
            }

    def _from_prices(self, from_place: str, to_place: str) -> Dict[str, float]:
        url = route_url(from_place, to_place)
        if url is None:
            return {}
        cached = self._cache_get(url)
        if cached is not None:
            return dict(cached)
        self._reserve_fetch_slot()
        try:
            html = self._fetch_html(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # no page for this route: a stable answer, cached
                self._cache_put(url, {})
                _record_ok()
                return {}
            _record_failure("error", f"HTTP {exc.code} for {url}")
            return {}
        except Exception as exc:
            _record_failure("error", f"{type(exc).__name__}: {exc}")
            return {}
        rows = parse_bottom_table(html)
        prices = min_fares_try(rows) if rows else {}
        if rows is None:
            _record_failure("parse_error", f"no operator table on {url}")
            return {}
        _record_ok()
        self._cache_put(url, prices)
        return dict(prices)

    def _reserve_fetch_slot(self) -> None:
        now = _now()
        with _SHARED.lock:
            start = max(now, _SHARED.next_fetch_at)
            _SHARED.next_fetch_at = start + max(0.0, self.min_interval_secs)
            _SHARED.requests += 1
        if start > now:
            _sleep(start - now)

    def _cache_get(self, url: str) -> Optional[Dict[str, float]]:
        with _SHARED.lock:
            entry = _SHARED.cache.get(url)
            if entry is None:
                return None
            if _now() - entry[0] >= self.cache_ttl_secs:
                _SHARED.cache.pop(url, None)
                return None
            return entry[1]

    def _cache_put(self, url: str, prices: Dict[str, float]) -> None:
        with _SHARED.lock:
            _SHARED.cache.pop(url, None)
            _SHARED.cache[url] = (_now(), dict(prices))
            while len(_SHARED.cache) > MAX_CACHE_ENTRIES:
                _SHARED.cache.pop(next(iter(_SHARED.cache)))


def _record_ok() -> None:
    with _SHARED.lock:
        _SHARED.last_ok_at = _iso_now()
        _SHARED.last_status = "ok"
        _SHARED.consecutive_failures = 0


def _record_failure(status: str, message: str) -> None:
    logger.warning("obilet fares: %s", message)
    with _SHARED.lock:
        _SHARED.last_status = status
        _SHARED.last_error = message
        _SHARED.last_error_at = _iso_now()
        _SHARED.consecutive_failures += 1
