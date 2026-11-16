"""Round-trip combinations (Faz 2c "Gidiş-dönüş kombinasyonları").

Outbound and return options are optimised separately by the route/trip planner. This module pairs
them: a return must leave after the outbound arrives, with a minimum stay, and optionally within a
maximum stay. Pure functions, no network.

Option dicts are the ones produced by ``travel_engine.trip_planner`` / ``route_engine`` and use
``kind``, ``depart_at``, ``arrive_at``, ``true_total_try``, ``duration_mins``, ``score`` and
``night_wait``. Timestamps are naive or timezone-aware ISO strings.

Time zones: ``depart_at`` / ``arrive_at`` are local wall-clock times (origin and destination zones).
When an option carries ``depart_utc`` and ``arrive_utc`` (aware, from the route engine), the stay of a
pair is computed on those instants, so an outbound that lands in London and a return that leaves
Istanbul are compared correctly. A pair uses the UTC instants only when both sides have them; otherwise
the local strings are used, as for legacy options. Pairs whose local strings cannot be compared (mixed
naive and aware) are skipped.

Combination dict::

    {"outbound": opt, "return": opt, "total_try": float, "stay_hours": float,
     "score": float, "label": "Gidiş: ... + Dönüş: ...", "badges": [...]}

Badges (assigned among the returned combinations): "En Ucuz Kombinasyon" (lowest total_try),
"En Hızlı Kombinasyon" (shortest outbound + return travel time, stay excluded), "Önerilen" (first
in the list, i.e. lowest score).

Algorithm: each side is sorted by score and only its best ``SIDE_CANDIDATES`` (30) options are
paired, which bounds the work at 900 pairs. A good pair can therefore be missed if both of its
options rank outside the top 30 on their side. Combinations are sorted by score, then total, then
stay, and cut to ``top_n``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

SIDE_CANDIDATES = 30

BADGE_CHEAPEST = "En Ucuz Kombinasyon"
BADGE_FASTEST = "En Hızlı Kombinasyon"
BADGE_BEST = "Önerilen"

KIND_LABELS = {
    "direct": "Direkt uçuş",
    "via_single_ticket": "Aktarmalı uçuş (tek bilet)",
    "via_separate": "Ayrı biletli aktarma",
    "side_route": "Kara + uçuş zinciri",
    "multi_flight": "Ayrı biletli uçuşlar",
    "ground": "Kara yolu (tren/otobüs)",
}

# Mirrors the planner's fallback penalties (travel_engine.multimodal_router / trip_planner), used only
# when an option has no usable "score" of its own.
_NIGHT_WAIT_PENALTY_TRY = 1500.0
_NIGHT_WAIT_REFERENCE_TV = 150.0
_SEPARATE_TICKET_RISK_TRY = 500.0


def _usable_price(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return round(price, 2)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_utc(value: Any) -> Optional[datetime]:
    """An aware timestamp as UTC, or None when it is missing, malformed or has no offset."""
    parsed = _parse_ts(value)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fallback_score(option: Dict[str, Any], price: float, duration_mins: float, time_value: float) -> float:
    score = price + (duration_mins / 60.0) * time_value
    if option.get("night_wait"):
        score += _NIGHT_WAIT_PENALTY_TRY * time_value / _NIGHT_WAIT_REFERENCE_TV
    if option.get("separate_tickets"):
        score += _SEPARATE_TICKET_RISK_TRY
    return score


class _Side:
    """One usable option with its parsed times, price and score."""

    __slots__ = ("option", "price", "depart", "arrive", "depart_utc", "arrive_utc", "duration_mins", "score")

    def __init__(self, option, price, depart, arrive, depart_utc, arrive_utc, duration_mins, score):
        self.option = option
        self.price = price
        self.depart = depart            # local wall-clock, may be None when only the UTC pair exists
        self.arrive = arrive
        self.depart_utc = depart_utc    # both set or both None
        self.arrive_utc = arrive_utc
        self.duration_mins = duration_mins
        self.score = score


def _prepare(options: List[Dict[str, Any]], time_value: float) -> List[_Side]:
    sides: List[_Side] = []
    for option in options or []:
        if not isinstance(option, dict):
            continue
        price = _usable_price(option.get("true_total_try"))
        if price is None:
            continue
        depart = _parse_ts(option.get("depart_at"))
        arrive = _parse_ts(option.get("arrive_at"))
        depart_utc = _parse_utc(option.get("depart_utc"))
        arrive_utc = _parse_utc(option.get("arrive_utc"))
        if depart_utc is None or arrive_utc is None:
            depart_utc = arrive_utc = None  # the UTC pair is used as a whole or not at all
        if depart_utc is not None:
            span_mins = (arrive_utc - depart_utc).total_seconds() / 60.0
        elif depart is not None and arrive is not None:
            try:
                span_mins = (arrive - depart).total_seconds() / 60.0
            except TypeError:
                continue
        else:
            continue
        if not math.isfinite(span_mins) or span_mins < 0:
            continue
        duration = option.get("duration_mins")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) \
                and duration >= 0:
            duration_mins = float(duration)
        else:
            duration_mins = span_mins
        score_raw = option.get("score")
        if isinstance(score_raw, (int, float)) and not isinstance(score_raw, bool) and math.isfinite(score_raw):
            score = float(score_raw)
        else:
            score = _fallback_score(option, price, duration_mins, time_value)
        sides.append(_Side(option, price, depart, arrive, depart_utc, arrive_utc, duration_mins, score))
    sides.sort(key=lambda s: s.score)
    return sides[:SIDE_CANDIDATES]


def _label(option: Dict[str, Any]) -> str:
    kind = str(option.get("kind") or "")
    return KIND_LABELS.get(kind, kind or "Bilinmeyen")


def combine_roundtrip(
    outbound_options: List[Dict[str, Any]],
    return_options: List[Dict[str, Any]],
    min_stay_hours: float = 0.0,
    max_stay_days: Optional[float] = None,
    top_n: int = 10,
    time_value_try_per_hour: float = 150.0,
) -> List[Dict[str, Any]]:
    """Pair outbound and return options into round-trip combinations, best score first.

    - A pair is kept when ``return.depart_at >= outbound.arrive_at + min_stay_hours``
      (``min_stay_hours`` is clamped to >= 0).
    - If ``max_stay_days`` is given, the stay must also be at most that many days.
    - ``top_n`` caps the result (<= 0 or empty inputs give ``[]``).
    """
    if top_n is None or int(top_n) <= 0:
        return []
    time_value = float(time_value_try_per_hour)
    min_stay = timedelta(hours=max(0.0, float(min_stay_hours or 0.0)))
    max_stay = None if max_stay_days is None else timedelta(days=float(max_stay_days))

    outs = _prepare(outbound_options, time_value)
    rets = _prepare(return_options, time_value)

    pairs: List[Tuple[Tuple, Dict[str, Any]]] = []
    for out in outs:
        for ret in rets:
            if out.arrive_utc is not None and ret.depart_utc is not None:
                gap = ret.depart_utc - out.arrive_utc
            elif out.arrive is not None and ret.depart is not None:
                try:
                    gap = ret.depart - out.arrive
                except TypeError:  # mixed naive and aware local strings
                    continue
            else:
                continue
            if gap < min_stay:
                continue
            if max_stay is not None and gap > max_stay:
                continue
            total = round(out.price + ret.price, 2)
            stay_hours = round(gap.total_seconds() / 3600.0, 2)
            score = round(out.score + ret.score, 2)
            combo = {
                "outbound": out.option,
                "return": ret.option,
                "total_try": total,
                "stay_hours": stay_hours,
                "score": score,
                "label": f"Gidiş: {_label(out.option)} + Dönüş: {_label(ret.option)}",
                "badges": [],
            }
            travel = out.duration_mins + ret.duration_mins
            pairs.append(((score, total, stay_hours, combo["label"]), {"combo": combo, "travel": travel}))

    pairs.sort(key=lambda item: item[0])
    shown = [item[1] for item in pairs[: int(top_n)]]
    if not shown:
        return []

    cheapest = min(shown, key=lambda s: (s["combo"]["total_try"], s["combo"]["score"]))
    fastest = min(shown, key=lambda s: (s["travel"], s["combo"]["score"]))
    shown[0]["combo"]["badges"].append(BADGE_BEST)
    cheapest["combo"]["badges"].append(BADGE_CHEAPEST)
    fastest["combo"]["badges"].append(BADGE_FASTEST)
    return [s["combo"] for s in shown]
