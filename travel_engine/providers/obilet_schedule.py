"""obilet.com route page bus schedules (T16 - KARA-9).

Reads the bus departures timetable from obilet route pages (e.g.
https://www.obilet.com/otobus-bileti/dedeagac-istanbul).

The schedule list on the page is general (not tied to a specific date).
Returned segments have:
- source = "obilet_page"
- is_estimated = True
- price_from_try = float(lowest price)
- price_try = None (indicative "from" price only)
- note = "saatler obilet genel listesinden, tarihi doğrulayın"
- arrival_day_offset = 1 if arrival_time < departure_time else 0

Politeness: pages are fetched at most once a second, sharing the pace and lock with
obilet_fares._SHARED, and cached for 24 hours. HTTP 404 is cached as empty.
Nothing raises.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from travel_engine import fx
from travel_engine.providers.obilet_fares import (
    CACHE_TTL_SECS,
    MIN_INTERVAL_SECS,
    REQUEST_TIMEOUT_SECS,
    USER_AGENT,
    _SHARED,
    _now,
    _sleep,
    route_url,
)
from travel_engine.providers.rail_bus_provider import ScheduledSegment

logger = logging.getLogger(__name__)

SOURCE = "obilet_page"
MAX_BYTES = 4_000_000
SCHEDULE_NOTE = "saatler obilet genel listesinden, tarihi doğrulayın"


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.5",
    })
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECS) as response:
        status = getattr(response, "status", 200)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        return response.read(MAX_BYTES).decode("utf-8", "replace")


def parse_schedule(
    html: str,
    from_place: str,
    to_place: str,
    date_str: str = "",
) -> List[ScheduledSegment]:
    """Parse ScheduledSegments from the HTML busTrip list of an obilet route page.

    Never raises; returns [] on invalid or empty HTML.
    """
    if not html:
        return []

    try:
        trips = re.findall(r'<li[^>]*itemprop="busTrip"[^>]*>([\s\S]*?)</li>', html)
        if not trips:
            return []

        segments: List[ScheduledSegment] = []
        seen = set()

        for item in trips:
            op_m = re.search(r'itemprop="provider"[^>]*>[\s\S]*?<meta itemprop="name" content="([^"]+)"', item)
            if not op_m:
                op_m = re.search(r'itemprop="provider"[^>]*>[\s\S]*?alt="([^"]+)"', item)
            operator = op_m.group(1).strip() if op_m else "Bus"

            dep_t_m = re.search(r'itemprop="departureTime"[^>]*>\s*([0-9]{1,2}:[0-9]{2})\s*<', item)
            arr_t_m = re.search(r'itemprop="arrivalTime"[^>]*>\s*([0-9]{1,2}:[0-9]{2})\s*<', item)
            if not dep_t_m or not arr_t_m:
                continue
            dep_time = dep_t_m.group(1).strip()
            arr_time = arr_t_m.group(1).strip()

            dep_s_m = re.search(r'itemprop="departureBusStop"[^>]*>[\s\S]*?<span itemprop="name">([^<]+)</span>', item)
            arr_s_m = re.search(r'itemprop="arrivalBusStop"[^>]*>[\s\S]*?<span itemprop="name">([^<]+)</span>', item)
            dep_stop = dep_s_m.group(1).strip() if dep_s_m else None
            arr_stop = arr_s_m.group(1).strip() if arr_s_m else None

            price_m = re.search(r'data-price="([^"]+)"', item)
            curr_m = re.search(r'data-currency="([^"]+)"', item)
            currency = curr_m.group(1).strip() if curr_m else "TRY"

            price_from_try: Optional[float] = None
            if price_m:
                try:
                    raw_val = float(price_m.group(1).strip().replace(",", "."))
                    if currency == "TRY":
                        price_from_try = round(raw_val, 2)
                    else:
                        converted = fx.convert_to_try(raw_val, currency)
                        if converted is not None:
                            price_from_try = round(float(converted), 2)
                except Exception:
                    price_from_try = None

            # Calculate times and arrival_day_offset
            dep_parts = dep_time.split(":")
            arr_parts = arr_time.split(":")
            dep_mins = int(dep_parts[0]) * 60 + int(dep_parts[1])
            arr_mins = int(arr_parts[0]) * 60 + int(arr_parts[1])

            if arr_mins < dep_mins:
                arrival_day_offset = 1
                duration_mins = (arr_mins + 24 * 60) - dep_mins
            else:
                arrival_day_offset = 0
                duration_mins = arr_mins - dep_mins

            # Deduplication key: identical rows kept once
            dedup_key = (operator, dep_time, arr_time, dep_stop, arr_stop, price_from_try)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            seg = ScheduledSegment(
                origin_station=from_place,
                dest_station=to_place,
                departure_time=dep_time,
                arrival_time=arr_time,
                duration_mins=duration_mins,
                price_try=None,
                operator=operator,
                mode="bus",
                date=date_str,
                is_estimated=True,
                price_is_estimated=True,
                source=SOURCE,
                origin_station_name=dep_stop,
                dest_station_name=arr_stop,
                price_from_try=price_from_try,
                arrival_day_offset=arrival_day_offset,
                note=SCHEDULE_NOTE,
            )
            segments.append(seg)

        return segments
    except Exception as exc:
        logger.warning("parse_schedule failed: %s", exc)
        return []


class ObiletScheduleProvider:
    """Bus schedule provider that scrapes obilet route pages. Never raises."""

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
        self._cache: Dict[str, tuple] = {}

    def departures(
        self,
        from_place: str,
        to_place: str,
        date_str: Optional[str] = None,
    ) -> List[ScheduledSegment]:
        """Scheduled departures on the obilet route page between from_place and to_place.

        Returns [] on unknown route, network error, or parse error. Never raises.
        """
        try:
            return self._departures(from_place, to_place, date_str or "")
        except Exception:
            logger.exception("obilet departures failed for %s -> %s", from_place, to_place)
            return []

    def health_status(self) -> Dict[str, Any]:
        """Current state of this provider."""
        return {
            "source": SOURCE,
            "status": "ok",
            "healthy": True,
            "cached_routes": len(self._cache),
            "min_interval_secs": self.min_interval_secs,
        }

    def _departures(self, from_place: str, to_place: str, date_str: str) -> List[ScheduledSegment]:
        url = route_url(from_place, to_place)
        if url is None:
            return []

        cached = self._cache_get(url)
        if cached is not None:
            return [dataclasses_replace_date(seg, date_str) for seg in cached]

        self._reserve_fetch_slot()
        try:
            html = self._fetch_html(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self._cache_put(url, [])
                return []
            logger.warning("HTTP %s for %s", exc.code, url)
            return []
        except Exception as exc:
            logger.warning("Fetch failed for %s: %s", url, exc)
            return []

        segments = parse_schedule(html, from_place, to_place, date_str)
        if segments:
            self._cache_put(url, segments)
        return [dataclasses_replace_date(seg, date_str) for seg in segments]

    def _reserve_fetch_slot(self) -> None:
        now = _now()
        with _SHARED.lock:
            start = max(now, _SHARED.next_fetch_at)
            _SHARED.next_fetch_at = start + max(0.0, self.min_interval_secs)
            _SHARED.requests += 1
        if start > now:
            _sleep(start - now)

    def _cache_get(self, url: str) -> Optional[List[ScheduledSegment]]:
        with _SHARED.lock:
            entry = self._cache.get(url)
            if entry is None:
                return None
            if _now() - entry[0] >= self.cache_ttl_secs:
                self._cache.pop(url, None)
                return None
            return entry[1]

    def _cache_put(self, url: str, segs: List[ScheduledSegment]) -> None:
        with _SHARED.lock:
            self._cache[url] = (_now(), list(segs))


def dataclasses_replace_date(seg: ScheduledSegment, date_str: str) -> ScheduledSegment:
    import dataclasses
    return dataclasses.replace(seg, date=date_str) if date_str else dataclasses.replace(seg)


_DEFAULT_PROVIDER: Optional[ObiletScheduleProvider] = None


def departures(from_place: str, to_place: str, date_str: Optional[str] = None) -> List[ScheduledSegment]:
    global _DEFAULT_PROVIDER
    if _DEFAULT_PROVIDER is None:
        _DEFAULT_PROVIDER = ObiletScheduleProvider()
    return _DEFAULT_PROVIDER.departures(from_place, to_place, date_str)
