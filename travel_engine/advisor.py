"""Buy-or-wait advice from observed prices (Faz 5 "şimdi al / bekle").

Pure logic. ``advice_for_route`` reads the observations from a ``SearchLog``; ``buy_or_wait`` takes
the price points directly, so it also works with ``PriceHistoryManager`` data.

Heuristics (conservative by design; the reasons are returned in Turkish so the UI can show them)
-------------------------------------------------------------------------------------------------
Let n be the number of usable observations for the same route and travel date, and cur the current
price. Observations dated after ``today`` are ignored.

1. Insufficient data: cur missing/invalid, departure already past, or n < 3 -> "belirsiz".
2. Price signals ("al" side):
   - low price: cur <= 10th percentile of the observations (linear interpolation), or
     cur <= min * 1.03.
   - near departure: 0 <= days_to_departure <= 14. Fares often rise in the last two weeks. This is
     a general tendency, not a fact, so it is reported with low confidence.
3. Trend signal ("bekle" side): the price fell by at least 5% over the last 7 days (the earliest
   observation in the window vs the current price) AND departure is more than 30 days away.
4. Decision:
   - price and trend signals both present -> "belirsiz" (the signals contradict each other);
   - only price signals -> "al";
   - only the trend signal -> "bekle";
   - nothing -> "belirsiz".

Confidence: "al" from a low price is "yüksek" with >= 10 observations, otherwise "orta". "al" from
the near-departure rule alone is always "düşük". "bekle" is "orta" with >= 10 observations, otherwise
"düşük". "belirsiz" is "düşük".

Stats (all JSON-safe; None when not computable, never nan or inf): n, min, max, median,
current_percentile (mid-rank percentage of observations below the current price, counting ties as
half, 0-100), days_to_departure (int), trend_7d_pct (percent change over the last 7 days, or None).
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

MIN_POINTS = 3
HIGH_CONFIDENCE_POINTS = 10
LOW_PERCENTILE = 0.10
LOW_NEAR_MIN_MULTIPLIER = 1.03
NEAR_DEPARTURE_DAYS = 14
FAR_DEPARTURE_DAYS = 30
TREND_WINDOW_DAYS = 7
FALLING_TREND_PCT = -5.0


def _parse_day(text: Any) -> dt.date:
    return dt.date.fromisoformat(str(text).strip()[:10])


def _usable_price(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _tl(value: float) -> str:
    """Turkish-style whole-lira text, e.g. 3000 -> "3.000 TL"."""
    return f"{value:,.0f}".replace(",", ".") + " TL"


def _clean_points(
    price_points: Sequence[Any], today: dt.date
) -> List[Tuple[dt.date, float]]:
    """(day, price) for every usable point, oldest first. Invalid or future-dated points are dropped."""
    cleaned: List[Tuple[dt.date, float]] = []
    for item in price_points or []:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        price = _usable_price(item[1])
        if price is None:
            continue
        try:
            day = _parse_day(item[0])
        except (TypeError, ValueError):
            continue
        if day > today:
            continue
        cleaned.append((day, price))
    cleaned.sort(key=lambda pair: pair[0])  # stable: same-day points keep their order
    return cleaned


def _percentile_linear(sorted_prices: List[float], q: float) -> float:
    position = q * (len(sorted_prices) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_prices[lower]
    weight = position - lower
    return sorted_prices[lower] * (1 - weight) + sorted_prices[upper] * weight


def _median(sorted_prices: List[float]) -> float:
    return _percentile_linear(sorted_prices, 0.5)


def _mid_rank_percent(prices: List[float], current: float) -> float:
    below = sum(1 for p in prices if p < current)
    equal = sum(1 for p in prices if p == current)
    return 100.0 * (below + 0.5 * equal) / len(prices)


def buy_or_wait(
    price_points: List[Tuple[str, float]],
    current_price: float,
    depart_date: str,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """Advice ("al" / "bekle" / "belirsiz") for one route and departure date.

    ``price_points`` are ``(iso_timestamp, price)`` pairs for the same route and date, oldest first.
    ``today`` (YYYY-MM-DD) defaults to the local date; pass it explicitly for reproducible results.
    Raises ValueError if ``depart_date`` or ``today`` is not a valid date.
    """
    today_day = _parse_day(today) if today else dt.date.today()
    depart = _parse_day(depart_date)
    days_to_departure = (depart - today_day).days
    current = _usable_price(current_price)

    observations = _clean_points(price_points, today_day)
    prices = [price for _, price in observations]
    n = len(prices)
    sorted_prices = sorted(prices)

    span_days = (observations[-1][0] - observations[0][0]).days if n else None
    stats: Dict[str, Any] = {
        "n": n,
        "min": _round(sorted_prices[0]) if n else None,
        "max": _round(sorted_prices[-1]) if n else None,
        "median": _round(_median(sorted_prices)) if n else None,
        "current_percentile": None,
        "days_to_departure": days_to_departure,
        "trend_7d_pct": None,
        "observations_span_days": span_days,
    }

    if current is not None and n:
        stats["current_percentile"] = _round(_mid_rank_percent(prices, current), 1)
        window_start = today_day - dt.timedelta(days=TREND_WINDOW_DAYS)
        window = [price for day, price in observations if window_start <= day <= today_day]
        series = window + [current]
        if len(series) >= 2 and series[0] > 0:
            stats["trend_7d_pct"] = _round((current - series[0]) / series[0] * 100.0)

    def result(advice: str, confidence: str, reasons: List[str]) -> Dict[str, Any]:
        span = stats.get("observations_span_days")
        if span is None or span < 7:
            confidence = "düşük"
        deduped = list(dict.fromkeys(reasons))
        return {"advice": advice, "confidence": confidence, "reasons": deduped, "stats": stats}

    if current is None:
        return result("belirsiz", "düşük", ["Güncel fiyat geçersiz veya eksik, öneri üretilemedi."])
    if days_to_departure < 0:
        return result("belirsiz", "düşük", ["Kalkış tarihi geçmiş, öneri üretilemez."])
    if n < MIN_POINTS:
        return result(
            "belirsiz",
            "düşük",
            [f"Yetersiz veri: en az {MIN_POINTS} gözlem gerekli, elimizde {n} tane var."],
        )

    p10 = _percentile_linear(sorted_prices, LOW_PERCENTILE)
    lowest = sorted_prices[0]
    low_by_percentile = current <= p10
    low_near_min = current <= lowest * LOW_NEAR_MIN_MULTIPLIER
    low_price = low_by_percentile or low_near_min
    near_departure = 0 <= days_to_departure <= NEAR_DEPARTURE_DAYS
    trend = stats["trend_7d_pct"]
    falling = trend is not None and trend <= FALLING_TREND_PCT and days_to_departure > FAR_DEPARTURE_DAYS

    buy_reasons: List[str] = []
    if low_by_percentile:
        buy_reasons.append(
            f"Güncel fiyat ({_tl(current)}) gözlenen fiyatların en düşük %10'luk diliminde."
        )
    elif low_near_min:
        buy_reasons.append(
            f"Güncel fiyat ({_tl(current)}) gözlenen en düşük fiyatın ({_tl(lowest)}) %3'ü içinde."
        )
    if near_departure:
        buy_reasons.append(
            f"Kalkışa {days_to_departure} gün kaldı; fiyatlar genelde son iki haftada yükselir (genel bir eğilim, garanti değil)."
        )

    if falling:
        wait_reasons = [
            f"{days_to_departure} gün var; düşüş sürebilir, ama bu garanti değil."
        ]
    else:
        wait_reasons = []

    if low_price or near_departure:
        if falling:
            return result(
                "belirsiz",
                "düşük",
                [
                    "Fiyat sinyalleri çelişiyor.",
                    *buy_reasons,
                    *wait_reasons,
                ],
            )
        if low_price:
            confidence = "yüksek" if n >= HIGH_CONFIDENCE_POINTS else "orta"
        else:
            confidence = "düşük"
        return result("al", confidence, buy_reasons)

    if falling:
        confidence = "orta" if n >= HIGH_CONFIDENCE_POINTS else "düşük"
        return result("bekle", confidence, wait_reasons)

    return result(
        "belirsiz",
        "düşük",
        ["Belirgin bir fiyat sinyali yok: güncel fiyat gözlemlerin ortasında ve trend net değil."],
    )


def advice_for_route(
    search_log: Any,
    origin: str,
    destination: str,
    date: str,
    current_price: float,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """``buy_or_wait`` fed with ``search_log.price_series(origin, destination, date)``."""
    series = search_log.price_series(origin, destination, date)
    return buy_or_wait(series, current_price, date, today=today)
