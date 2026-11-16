"""Flexible-date helpers (Faz 5 "esnek tarih takibi").

Pure functions: no network, no I/O. They work on the option dicts produced by
``travel_engine.trip_planner`` / ``route_engine`` (keys used here: ``depart_at`` as
"YYYY-MM-DDTHH:MM", ``true_total_try``). When an option has ``depart_utc`` and ``depart_tz``, its
departure day is the day of that instant in the origin zone (see ``_option_day``).

- ``expand_flex_dates``: the candidate travel dates around a requested date.
- ``cheapest_by_date``: the cheapest option per departure date.
- ``flex_summary``: the cheapest date, and the saving against the requested date.

Prices are plain floats rounded to 2 decimals. Missing or unusable prices (None, NaN, inf,
zero or negative, bool) are ignored, so the results never contain NaN or inf. Formatting for
display (Turkish thousands separators, the ₺ sign) is left to the UI.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

_DATE_FMT = "%Y-%m-%d"


def _parse_day(text: str) -> date:
    """Parse the calendar day of "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM[:SS]". Raises ValueError."""
    return date.fromisoformat(str(text).strip()[:10])


def _parse_utc(value: Any) -> Optional[datetime]:
    """An aware ISO timestamp as UTC, or None when it is missing, malformed or has no offset."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _usable_price(value: Any) -> Optional[float]:
    """A positive finite number as float, otherwise None (bools are rejected)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return round(price, 2)


def _zone(name: Any) -> Optional[ZoneInfo]:
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except (ValueError, KeyError, OSError):  # unknown name, or no tz database on this machine
        return None


def _option_day(option: Dict[str, Any]) -> Optional[str]:
    """The departure calendar day of an option as "YYYY-MM-DD", or None if unusable.

    When the option has ``depart_utc`` (aware) and ``depart_tz``, the instant is converted to the
    origin's zone and its date is used. The UTC calendar date itself is not used: a 01:30 Istanbul
    departure is 22:30 UTC on the previous day and belongs to the day it leaves on. Without a usable
    zone, or without ``depart_utc``, the local ``depart_at`` day is used (legacy options).
    """
    if not isinstance(option, dict):
        return None
    instant = _parse_utc(option.get("depart_utc"))
    zone = _zone(option.get("depart_tz")) if instant is not None else None
    if instant is not None and zone is not None:
        return instant.astimezone(zone).strftime(_DATE_FMT)
    raw = option.get("depart_at")
    if not raw:
        return None
    try:
        return _parse_day(raw).strftime(_DATE_FMT)
    except (TypeError, ValueError):
        return None


def expand_flex_dates(
    center: str,
    flex_days: int,
    min_date: Optional[str] = None,
    max_count: int = 7,
) -> List[str]:
    """Dates within ±flex_days of ``center``, as "YYYY-MM-DD" strings.

    - Ordering: the center date first, then the nearest days, alternating later and earlier
      (+1, -1, +2, -2, ...). For equal distance the later day comes first.
    - Dates before ``min_date`` (default: today) are dropped, so a center in the past only keeps
      the days that are still in the future.
    - At most ``max_count`` dates are returned (<= 0 gives an empty list). A negative
      ``flex_days`` is treated as 0.

    Raises ValueError if ``center`` or ``min_date`` is not a valid date.
    """
    center_day = _parse_day(center)
    floor = _parse_day(min_date) if min_date else date.today()
    span = max(0, int(flex_days))
    limit = int(max_count)
    if limit <= 0:
        return []

    offsets = [0]
    for step in range(1, span + 1):
        offsets.append(step)
        offsets.append(-step)

    result: List[str] = []
    for offset in offsets:
        day = center_day + timedelta(days=offset)
        if day < floor:
            continue
        result.append(day.strftime(_DATE_FMT))
        if len(result) >= limit:
            break
    return result


def cheapest_by_date(options: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The cheapest usable option for each departure date.

    Returns ``[{"date", "best_true_total_try", "option_index"}]`` sorted by date.
    ``option_index`` is the position of that option in ``options``. Ties go to the lower index.
    Options without a valid ``depart_at`` day or a usable ``true_total_try`` are skipped.
    """
    best: Dict[str, Dict[str, Any]] = {}
    for index, option in enumerate(options or []):
        day = _option_day(option)
        price = _usable_price(option.get("true_total_try")) if isinstance(option, dict) else None
        if day is None or price is None:
            continue
        current = best.get(day)
        if current is None or price < current["best_true_total_try"]:
            best[day] = {"date": day, "best_true_total_try": price, "option_index": index}
    return [best[day] for day in sorted(best)]


def flex_summary(options: List[Dict[str, Any]], requested_date: str) -> Dict[str, Any]:
    """Summarise a flexible-date search against the date the user asked for.

    Returns::

        {
          "requested_date": "YYYY-MM-DD",
          "requested_best": float | None,   # cheapest on the requested date; None if not searched
          "best_date": str | None,          # cheapest date overall (earliest on ties)
          "best_price": float | None,
          "saving_vs_requested": float | None,
          "by_date": [...],                 # same as cheapest_by_date(options)
        }

    ``saving_vs_requested`` is ``requested_best - best_price`` (never negative, 0.0 when the
    requested date is itself the cheapest). It is None when either price is unknown, so the UI
    can say "bu tarih için fiyat yok" rather than showing a fake saving.
    """
    requested = _parse_day(requested_date).strftime(_DATE_FMT)
    by_date = cheapest_by_date(options)

    requested_best: Optional[float] = None
    best_date: Optional[str] = None
    best_price: Optional[float] = None
    for entry in by_date:
        if entry["date"] == requested:
            requested_best = entry["best_true_total_try"]
        if best_price is None or entry["best_true_total_try"] < best_price:
            best_date = entry["date"]
            best_price = entry["best_true_total_try"]

    saving: Optional[float] = None
    if requested_best is not None and best_price is not None:
        saving = round(max(0.0, requested_best - best_price), 2)

    return {
        "requested_date": requested,
        "requested_best": requested_best,
        "best_date": best_date,
        "best_price": best_price,
        "saving_vs_requested": saving,
        "by_date": by_date,
    }
