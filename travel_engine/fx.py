"""EUR -> TRY and CZK/HUF/PLN -> TRY exchange rates for showing rail fares in lira.

Live rates come from two free sources, tried in order:
  1. Frankfurter (ECB reference rates). The old host api.frankfurter.app now
     answers 301 to api.frankfurter.dev, so the new host is used directly.
  2. open.er-api.com (daily rates).

Live results are cached in memory for 12 hours. After a failed refresh, no new
request is made for 10 minutes. Lookups never raise.

Fallback order for ``get_eur_try_rate``:
  env ``TRAVELPLAN_EUR_TRY`` (explicit override, always wins)
  -> live rate (fresh cache or network)
  -> last successful live value (stale, reported as ``last-known``)
  -> ``DEFAULT_EUR_TRY_RATE``.
"""

import json
import logging
import math
import os
import threading
import time
import urllib.request
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

ENV_VAR = "TRAVELPLAN_EUR_TRY"

# EUR -> TRY used only as a last resort (no env override, no live rate, no earlier live value).
# Snapshot from July 2026 (about 53.9). It is NOT updated automatically: when the live
# sources are reachable (about 55.7 in mid-October 2026) they are used instead.
DEFAULT_EUR_TRY_RATE = 54.0

FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest?base=EUR&symbols=TRY,CZK,HUF,PLN"
OPEN_ER_API_URL = "https://open.er-api.com/v6/latest/EUR"
_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("frankfurter.dev (ECB)", FRANKFURTER_URL),
    ("open.er-api.com", OPEN_ER_API_URL),
)
_SYMBOLS = ("TRY", "CZK", "HUF", "PLN")
_CROSS_CURRENCIES = ("CZK", "HUF", "PLN")

CACHE_TTL_SECONDS = 12 * 3600
RETRY_AFTER_FAILURE_SECONDS = 10 * 60

_now = time.monotonic  # module clock; tests replace it


class _RateCache:
    def __init__(self) -> None:
        self.rates: Optional[Dict[str, float]] = None  # EUR -> currency, last successful fetch
        self.provider = ""
        self.fetched_at: Optional[float] = None
        self.failed_at: Optional[float] = None


_lock = threading.Lock()
_cache = _RateCache()


def env_eur_try_rate() -> Optional[float]:
    """Value of TRAVELPLAN_EUR_TRY, or None when unset or invalid."""
    raw = os.environ.get(ENV_VAR)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", ENV_VAR, raw)
        return None
    if not math.isfinite(value) or value <= 0:
        logger.warning("Ignoring non-positive %s=%r", ENV_VAR, raw)
        return None
    return value


def _parse_rates(payload: object) -> Dict[str, float]:
    rates = payload.get("rates") if isinstance(payload, dict) else None
    if not isinstance(rates, dict):
        raise ValueError("response has no rates object")
    parsed: Dict[str, float] = {}
    for symbol in _SYMBOLS:
        value = rates.get(symbol)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
            parsed[symbol] = float(value)
    if "TRY" not in parsed:
        raise ValueError("response has no TRY rate")
    return parsed


def _fetch(url: str, timeout: float) -> Dict[str, float]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "travelplan/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return _parse_rates(json.loads(body.decode("utf-8")))


def _current_rates(timeout: float) -> Tuple[Optional[Dict[str, float]], str, bool]:
    """Return (EUR -> currency rates, provider, is_live). Rates are None when nothing is known."""
    with _lock:
        now = _now()
        if _cache.rates is not None and _cache.fetched_at is not None:
            if now - _cache.fetched_at < CACHE_TTL_SECONDS:
                return dict(_cache.rates), _cache.provider, True

        retry_allowed = _cache.failed_at is None or now - _cache.failed_at >= RETRY_AFTER_FAILURE_SECONDS
        if retry_allowed:
            for provider, url in _SOURCES:
                try:
                    rates = _fetch(url, timeout)
                except Exception as exc:  # network, HTTP, JSON or shape problems: fall through
                    logger.warning("FX lookup via %s failed: %s", provider, exc)
                    continue
                _cache.rates = rates
                _cache.provider = provider
                _cache.fetched_at = now
                _cache.failed_at = None
                return dict(rates), provider, True
            _cache.failed_at = now

        if _cache.rates is not None:
            return dict(_cache.rates), "last-known", False
        return None, "", False


def get_eur_try_rate(timeout: float = 6.0) -> Tuple[float, str, bool]:
    """Return ``(rate, source, is_live)`` for one euro in lira. Never raises.

    ``source`` is ``env``, the live provider name, ``last-known`` or ``default``.
    ``is_live`` is True only when the rate came from a live source (fresh cache or network).
    """
    try:
        override = env_eur_try_rate()
        if override is not None:
            return override, "env", False
        rates, provider, is_live = _current_rates(timeout)
        if rates is not None and "TRY" in rates:
            return rates["TRY"], provider, is_live
    except Exception:  # defensive: a rate lookup must never break a search
        logger.exception("Unexpected failure while resolving the EUR/TRY rate")
    return DEFAULT_EUR_TRY_RATE, "default", False


def convert_to_try(amount: float, currency: str, timeout: float = 6.0) -> Optional[float]:
    """Convert ``amount`` in EUR, CZK, HUF, PLN or TRY to lira. None when unknown. Never raises."""
    try:
        value = float(amount)
        if not math.isfinite(value):
            return None
        code = str(currency).strip().upper()
        if code == "TRY":
            return value
        if code not in ("EUR",) + _CROSS_CURRENCIES:
            return None
        eur_try = get_eur_try_rate(timeout)[0]
        if code == "EUR":
            return value * eur_try
        rates, _, _ = _current_rates(timeout)
        if rates is None or code not in rates:
            return None
        return value / rates[code] * eur_try  # currency -> EUR -> TRY
    except Exception:
        logger.exception("Unexpected failure converting %r %s to TRY", amount, currency)
        return None


def _reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = _RateCache()
