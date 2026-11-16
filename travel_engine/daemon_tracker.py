"""Background Daemon Price Tracker for TravelPlan.

Monitors active trip watches, executes periodic or on-demand price scans
using GoogleFlightsProvider, and triggers Telegram / In-App alerts when
prices fall below target user budgets.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Callable

from .models import TripWatch
from .tracker import PriceTracker
from .notifier import AlertNotifier
from .providers.google_flights_provider import GoogleFlightsProvider
from .geo_clusters import GeoClusterRegistry
from .providers.rail_bus_provider import RailBusProvider
from .providers.live_rail_provider import LiveRailProvider
from .multimodal_router import MultiModalRouter

logger = logging.getLogger("DaemonTracker")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Keys of TripWatch.trip_query that are passed to TripPlanner.plan_trip. The route engine keys are
# optional: a watch stored before they existed simply has none of them.
TRIP_QUERY_KEYS = (
    "depart_dates", "arrive_by", "depart_after", "via", "include_side_routes",
    "max_stops", "flight_search_budget", "min_stay_mins", "cabin_bag", "checked_bag", "flex_days",
    "return_dates", "return_arrive_by", "return_depart_after", "return_via", "min_stay_hours",
    "real_data_only",
)
TRIP_ICONS = {"flight": "✈️", "train": "🚆", "bus": "🚌"}


_EXTRA_LABELS = {
    "transfers": "transfer",
    "transfer": "transfer",
    "baggage": "bagaj",
    "hotel": "otel",
}


def _extras_note(option: Any) -> str:
    """Note for unknown extras (transfers, baggage, hotel) in an option whose ticket prices are known."""
    if not isinstance(option, dict):
        return ""
    parts = option.get("unknown_price_parts")
    if not parts or not isinstance(parts, (list, tuple, set)):
        return ""
    seen = set()
    labels = []
    for p in parts:
        label = _EXTRA_LABELS.get(str(p).strip().lower())
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    if not labels:
        return ""
    return f"Ek masraflar dahil değil (fiyat yok): {', '.join(labels)}"


def _option_price(option: Any) -> float | None:
    """True total price of a planner option, or None when it has none.

    Every ticket price must be known: an option with unknown ticket legs (unknown_price_legs > 0)
    has no usable total and yields None. Unknown extras (transfers, baggage, hotel) do not prevent
    a reading; they are noted in the alert instead. When unknown_price_legs is absent (legacy planner
    output), price_complete False yields None.
    """
    if not isinstance(option, dict):
        return None
    if "unknown_price_legs" in option:
        try:
            if int(option.get("unknown_price_legs") or 0) > 0:
                return None
        except (TypeError, ValueError):
            return None
    elif option.get("price_complete") is False:
        return None
    try:
        value = option.get("true_total_try")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _minutes(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _cheapest_roundtrip_option(plan: Dict[str, Any]) -> Dict[str, Any] | None:
    """Cheapest outbound + return combination of a round-trip plan as one option-like dict.

    Built from plan["roundtrip"]["combos"] (see TripPlanner.plan_trip). Its true_total_try is the
    combination total; legs are the outbound legs followed by the return legs. None without combos.
    """
    roundtrip = plan.get("roundtrip") if isinstance(plan.get("roundtrip"), dict) else {}
    best: tuple[float, Dict[str, Any]] | None = None
    for combo in roundtrip.get("combos") or []:
        if not isinstance(combo, dict):
            continue
        try:
            total = float(combo.get("total_try"))
        except (TypeError, ValueError):
            continue
        if best is None or total < best[0]:
            best = (total, combo)
    if best is None:
        return None
    total, combo = best
    outbound = combo.get("outbound") if isinstance(combo.get("outbound"), dict) else {}
    back = combo.get("return") if isinstance(combo.get("return"), dict) else {}
    legs = [leg for leg in list(outbound.get("legs") or []) + list(back.get("legs") or []) if isinstance(leg, dict)]
    return {
        "kind": "roundtrip",
        "legs": legs,
        "true_total_try": total,
        "duration_mins": _minutes(outbound.get("duration_mins")) + _minutes(back.get("duration_mins")),
        "depart_at": outbound.get("depart_at"),
        "arrive_at": back.get("arrive_at"),
        "stay_hours": combo.get("stay_hours"),
    }


def _iso_date(value: Any) -> str:
    """YYYY-MM-DD part of a YYYY-MM-DDTHH:MM string ('' if missing)."""
    text = str(value or "")
    return text[:10] if len(text) >= 10 else ""


def _iso_time(value: Any) -> str:
    """HH:MM part of a YYYY-MM-DDTHH:MM string ('' if missing)."""
    text = str(value or "")
    return text[11:16] if len(text) >= 16 else ""


def _format_duration(mins: Any) -> str:
    if mins is None:
        return "Bilinmiyor"
    mins = int(mins)
    return f"{mins // 60} sa. {mins % 60} dk."


def _segment_line(leg: Dict[str, Any]) -> str:
    """One leg as text, e.g. '✈️ Ryanair ATH→BTS 27.10 19:30–20:45'."""
    icon = TRIP_ICONS.get(str(leg.get("type", "")).lower(), "🔄")
    operator = leg.get("operator") or leg.get("type") or ""
    dep, arr = str(leg.get("depart_at") or ""), str(leg.get("arrive_at") or "")
    day = f"{dep[8:10]}.{dep[5:7]}" if len(dep) >= 10 else ""
    parts = [icon, operator, f"{leg.get('from', '')}→{leg.get('to', '')}", day,
             f"{_iso_time(dep)}–{_iso_time(arr)}"]
    line = " ".join(p for p in parts if p and p != "–")
    return line + (" (tahmini)" if leg.get("is_estimated") else "")


def _should_alert(price: float, last_price: float | None) -> bool:
    """Alert on the first sighting, then only when the price dropped further."""
    return last_price is None or price < last_price


def check_flight_time_match(
    flight_times: str,
    time_slot: str = "all",
    window_start: str | None = None,
    window_end: str | None = None,
) -> bool:
    """Check if flight departure matches requested slot or custom time window."""
    if not time_slot or time_slot == "all":
        return True
    if not flight_times:
        return True

    m = re.match(r'^(\d{1,2}):(\d{2})', flight_times.strip())
    if not m:
        return True

    h = int(m.group(1))
    mins = h * 60 + int(m.group(2))

    if time_slot == "morning":
        return 6 * 60 <= mins < 12 * 60
    elif time_slot == "afternoon":
        return 12 * 60 <= mins < 18 * 60
    elif time_slot == "evening":
        return 18 * 60 <= mins < 24 * 60
    elif time_slot == "night":
        return 0 <= mins < 6 * 60
    elif time_slot == "custom" and (window_start or window_end):
        def parse_str_mins(s: str | None, default_val: int) -> int:
            if not s:
                return default_val
            clean = s.strip().replace(".", ":")
            clean = re.sub(r'\s+', ':', clean)
            if ":" in clean:
                parts = clean.split(":")
                try:
                    ch = int(parts[0])
                    cm = int(parts[1]) if len(parts) > 1 and parts[1] else 0
                    if ch >= 24:
                        return 24 * 60
                    return ch * 60 + min(cm, 59)
                except Exception:
                    return default_val
            elif re.match(r'^\d{3,4}$', clean):
                try:
                    ch = int(clean[:-2])
                    cm = int(clean[-2:])
                    if ch >= 24:
                        return 24 * 60
                    return ch * 60 + min(cm, 59)
                except Exception:
                    return default_val
            else:
                try:
                    ch = int(clean)
                    if ch >= 24:
                        return 24 * 60
                    return ch * 60
                except Exception:
                    return default_val

        s_mins = parse_str_mins(window_start, 0)
        e_mins = parse_str_mins(window_end, 24 * 60)
        if s_mins <= e_mins:
            return s_mins <= mins <= e_mins
        else:
            return mins >= s_mins or mins <= e_mins

    return True


def parse_prefetch_window(window_str: str | None = None) -> tuple[int, int, int, int]:
    """Parse a prefetch window string like '03:00-05:00' into (start_h, start_m, end_h, end_m)."""
    default = (3, 0, 5, 0)
    if not window_str:
        return default
    text = window_str.strip()
    if "-" not in text:
        return default
    parts = text.split("-", 1)

    def parse_hm(s: str) -> tuple[int, int] | None:
        clean = s.strip()
        m = re.match(r"^(\d{1,2}):(\d{2})$", clean)
        if not m:
            return None
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h < 24 and 0 <= mn < 60:
            return h, mn
        return None

    p1 = parse_hm(parts[0])
    p2 = parse_hm(parts[1])
    if p1 is None or p2 is None:
        return default
    return (p1[0], p1[1], p2[0], p2[1])


def is_in_prefetch_window(now: datetime, window_str: str | None = None) -> bool:
    """Check if the given local datetime falls inside the nightly prefetch window."""
    sh, sm, eh, em = parse_prefetch_window(window_str or os.environ.get("TRAVELPLAN_PREFETCH_WINDOW", "03:00-05:00"))
    mins = now.hour * 60 + now.minute
    s_mins = sh * 60 + sm
    e_mins = eh * 60 + em
    if s_mins <= e_mins:
        return s_mins <= mins <= e_mins
    else:
        return mins >= s_mins or mins <= e_mins


def is_prefetch_enabled() -> bool:
    """Check whether nightly prefetch is enabled via TRAVELPLAN_PREFETCH (default 1)."""
    val = os.environ.get("TRAVELPLAN_PREFETCH", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


def _watch_is_past(watch: Any, today: str) -> bool:
    """True when a watch's departure date(s) are strictly before ``today`` ("YYYY-MM-DD").

    - Trip watches (trip_query set): False if depart_dates is empty. Otherwise True if the latest depart
      date plus flex_days (or 0) is before today.
    - Flight watches: True if departure_date is set and before today.
    """
    trip_query = getattr(watch, "trip_query", None)
    if trip_query:
        dates = trip_query.get("depart_dates") or []
        if not dates:
            return False
        max_date = max(str(d) for d in dates)
        try:
            flex = int(trip_query.get("flex_days") or 0)
        except (TypeError, ValueError):
            flex = 0
        if flex > 0:
            try:
                dt = datetime.strptime(max_date, "%Y-%m-%d") + timedelta(days=flex)
                max_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
        return max_date < today

    dep = getattr(watch, "departure_date", None)
    if dep:
        return str(dep) < today
    return False


class DaemonTracker:
    def __init__(
        self,
        tracker: PriceTracker | None = None,
        notifier: AlertNotifier | None = None,
        provider: GoogleFlightsProvider | None = None,
        trip_planner: Any | None = None,
        rail_provider: RailBusProvider | None = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        self._now = now_fn or datetime.now
        self.tracker = tracker or PriceTracker(storage_path=os.path.join(BASE_DIR, "watches.json"))
        self.notifier = notifier or AlertNotifier()
        # Pass the server's provider in to share its flight cache.
        self.provider = provider if provider is not None else GoogleFlightsProvider()
        # TripPlanner used by trip watches (watches with a trip_query)
        self.trip_planner = trip_planner
        self.geo_registry = GeoClusterRegistry()
        # Pass the server's rail provider in to share its ground sources and their health state.
        if rail_provider is not None:
            self.rail_provider = rail_provider
        else:
            # Standalone runs (python -m travel_engine.daemon_tracker) use every ground source, the same set
            # server_v2 wires. The providers are imported here to keep module import light.
            from .providers.flixbus_provider import FlixBusProvider
            from .providers.google_maps_transit_provider import GoogleMapsTransitProvider
            from .providers.obilet_fares import ObiletFromPrices
            from .providers.trenitalia_provider import TrenitaliaProvider
            from .providers.transitous_provider import TransitousProvider
            self.rail_provider = RailBusProvider(
                self.geo_registry,
                live_provider=LiveRailProvider(),
                bus_provider=FlixBusProvider(),
                trenitalia_provider=TrenitaliaProvider(),
                timetable_provider=TransitousProvider(),
                timetable_maps_provider=GoogleMapsTransitProvider(),
                bus_from_price_provider=ObiletFromPrices(),
            )
        self.multimodal_router = MultiModalRouter(
            flight_provider=self.provider,
            rail_provider=self.rail_provider,
            geo_registry=self.geo_registry,
        )

    def _state_lock(self):
        """Lock guarding watch field mutations and saves (the tracker's RLock)."""
        lock = getattr(self.tracker, "_lock", None)
        return lock if lock is not None else contextlib.nullcontext()

    def _scan_trip_watch(self, watch: TripWatch) -> Dict[str, Any]:
        """Scan a trip watch with the TripPlanner and alert on its direct / side-route budgets.

        Direct budget: cheapest direct option. Side budget: cheapest side route, or for a round-trip
        watch (return_dates in trip_query) the cheapest outbound + return combination total. The side
        alert then shows the combination's legs (outbound followed by return).
        """
        if self.trip_planner is None:
            logger.warning(f"Trip watch {watch.id} skipped: no trip planner configured")
            return {"watch_id": watch.id, "status": "no_trip_planner", "triggered": False}

        query = {k: v for k, v in dict(watch.trip_query or {}).items() if k in TRIP_QUERY_KEYS}
        logger.info(f"Scanning trip watch {watch.id}: {watch.origin} -> {watch.destination} ({query})")

        # Network search: the state lock must NOT be held here.
        try:
            plan = self.trip_planner.plan_trip(origin=watch.origin, destination=watch.destination, **query)
        except Exception as e:
            logger.error(f"Error planning trip for {watch.id}: {e}")
            return {"watch_id": watch.id, "status": "error", "error": str(e), "triggered": False}

        options = [o for o in (plan.get("options") or []) if isinstance(o, dict)]
        # Budget checks use the cheapest option per category (the best-scored one may be pricier)
        best_direct = plan.get("cheapest_direct_priced") or plan.get("cheapest_direct") or plan.get("best_direct")
        best_side = plan.get("cheapest_side_route_priced") or plan.get("cheapest_side_route") or plan.get("best_side_route")
        # Round-trip watch (return_dates set): the side budget is compared with the cheapest
        # outbound + return combination total instead of a one-way side route.
        roundtrip_best = _cheapest_roundtrip_option(plan)
        side_option = roundtrip_best if roundtrip_best is not None else best_side
        direct_price = _option_price(best_direct)
        side_price = _option_price(side_option)
        priced = [p for p in (_option_price(o) for o in options + [best_direct, best_side]) if p is not None]
        warnings = list(plan.get("warnings") or [])
        if not priced:
            return {"watch_id": watch.id, "status": "no_results", "triggered": False, "warnings": warnings}
        overall_price = min(priced)

        direct_budget = watch.direct_budget_try
        side_budget = watch.target_budget_try
        direct_hit = direct_price is not None and direct_budget is not None and direct_price <= direct_budget
        side_hit = side_price is not None and side_price <= side_budget

        # Record the reading and claim each alert under the lock, then send outside it.
        pending: list[tuple[str, Dict[str, Any], float]] = []
        with self._state_lock():
            watch.add_reading(overall_price)
            last = dict(watch.last_trip_alert or {})
            if direct_hit and _should_alert(direct_price, last.get("direct")):
                pending.append(("direct", best_direct, direct_price))
                last["direct"] = direct_price
            if side_hit and _should_alert(side_price, last.get("side")):
                pending.append(("side", side_option, side_price))
                last["side"] = side_price
            if pending:
                watch.last_trip_alert = last

        alerts = []
        for kind, option, price in pending:
            note = _extras_note(option)
            extra = {"note": note} if note else {}
            try:
                if kind == "direct":
                    kwargs = self._direct_alert_kwargs(watch, option, price, direct_budget)
                    kwargs.update(extra)
                    alerts.append(self.notifier.send_alert(**kwargs))
                else:
                    kwargs = self._side_alert_kwargs(watch, option, price, side_budget, direct_price)
                    kwargs.update(extra)
                    alerts.append(self.notifier.send_multimodal_alert(**kwargs))
            except Exception as e:
                logger.error(f"Error sending {kind} trip alert for {watch.id}: {e}")

        with self._state_lock():
            if self.tracker:
                self.tracker.save()

        return {
            "watch_id": watch.id,
            "status": "ok",
            "origin": watch.origin,
            "destination": watch.destination,
            "departure_date": _iso_date(options[0].get("depart_at")) if options else None,
            "current_price": overall_price,
            "best_direct_price": direct_price,
            "best_side_price": side_price,
            "roundtrip_price": roundtrip_best["true_total_try"] if roundtrip_best is not None else None,
            "direct_budget": direct_budget,
            "target_budget": side_budget,
            "triggered": direct_hit or side_hit,
            "alerts": alerts,
            "alert": alerts[0] if alerts else None,
            "warnings": warnings,
            "flight_searches": plan.get("flight_searches", 0),
        }

    @staticmethod
    def _direct_alert_kwargs(watch: TripWatch, option: Dict[str, Any], price: float, budget: float) -> Dict[str, Any]:
        legs = [l for l in (option.get("legs") or []) if isinstance(l, dict)]
        operators = list(dict.fromkeys(str(l["operator"]) for l in legs if l.get("operator")))
        return dict(
            origin=watch.origin,
            destination=watch.destination,
            current_price=price,
            target_budget=budget,
            airline=", ".join(operators) or "Havayolu",
            duration=_format_duration(option.get("duration_mins")),
            google_url=(legs[0].get("booking_url") or "") if legs else "",
            trip_type="Tek Yön",
            departure_date=_iso_date(option.get("depart_at")),
            times=f"{_iso_time(option.get('depart_at'))} – {_iso_time(option.get('arrive_at'))}",
        )

    @staticmethod
    def _side_alert_kwargs(
        watch: TripWatch, option: Dict[str, Any], price: float, budget: float, direct_price: float | None,
    ) -> Dict[str, Any]:
        legs = [l for l in (option.get("legs") or []) if isinstance(l, dict)]
        lines = [f"  {_segment_line(l)}" for l in legs]
        if option.get("kind") == "roundtrip":
            # The combination carries the return legs after the outbound ones: say so in the alert
            lines.insert(0, "Gidiş-dönüş (gidiş + dönüş toplamı)")
        return dict(
            origin=watch.origin,
            destination=watch.destination,
            multimodal_price=price,
            target_budget=budget,
            savings_vs_direct=max(0.0, direct_price - price) if direct_price is not None else 0.0,
            journey_duration_str=_format_duration(option.get("duration_mins")),
            segments_summary="\n".join(lines),
            departure_date=_iso_date(option.get("depart_at")),
            route_id="",
        )

    def scan_watch(self, watch: TripWatch) -> Dict[str, Any]:
        """Perform a live scan for a single watch and evaluate target budget."""
        from travel_engine.google_budget import set_current_priority, PRIORITY_WATCH, _current_priority
        token = set_current_priority(PRIORITY_WATCH)
        try:
            return self._scan_watch_inner(watch)
        finally:
            _current_priority.reset(token)

    def _scan_watch_inner(self, watch: TripWatch) -> Dict[str, Any]:
        if getattr(watch, "trip_query", None):
            return self._scan_trip_watch(watch)

        dep_date = watch.departure_date or datetime.now().strftime("%Y-%m-%d")
        ret_date = watch.return_date if watch.trip_type != "oneway" else None

        logger.info(
            f"Scanning watch {watch.id}: {watch.origin} -> {watch.destination} "
            f"({dep_date} | Budget: {watch.target_budget_try:,.0f} TL)"
        )

        # Network search: the state lock must NOT be held here.
        try:
            flights = self.provider.search_flights(
                origin=watch.origin,
                destination=watch.destination,
                departure_date=dep_date,
                return_date=ret_date,
                trip_type=watch.trip_type,
            )
        except Exception as e:
            logger.error(f"Error scraping flights for {watch.id}: {e}")
            return {
                "watch_id": watch.id,
                "status": "error",
                "error": str(e),
                "triggered": False,
            }

        if not flights:
            logger.warning(f"No flights found for {watch.origin} -> {watch.destination} on {dep_date}")
            return {
                "watch_id": watch.id,
                "status": "no_results",
                "triggered": False,
            }

        # Filter valid prices and find cheapest
        def get_price(fl):
            return float(fl.get("total_price_try") or fl.get("price_try") or 0)

        valid_flights = [f for f in flights if get_price(f) > 0]
        if not valid_flights:
            return {
                "watch_id": watch.id,
                "status": "no_valid_prices",
                "triggered": False,
            }

        # Filter by departure time slot or custom window if specified
        time_slot = getattr(watch, "departure_time_slot", "all")
        win_start = getattr(watch, "time_window_start", None)
        win_end = getattr(watch, "time_window_end", None)

        if time_slot and time_slot != "all":
            slot_flights = [
                f for f in valid_flights
                if check_flight_time_match(f.get("times", ""), time_slot, win_start, win_end)
            ]
            if slot_flights:
                valid_flights = slot_flights
            else:
                return {
                    "watch_id": watch.id,
                    "status": "no_flights_in_time_slot",
                    "triggered": False,
                }

        # Filter by max transfers if specified. Never relax this filter: if no
        # flight satisfies it, report that instead of alerting on a non-matching flight.
        max_transfers = getattr(watch, "max_transfers", 3)
        if max_transfers is not None and max_transfers < 3:
            valid_flights = [f for f in valid_flights if f.get("stops_count", 0) <= max_transfers]
            if not valid_flights:
                logger.info(f"No flights for {watch.id} within {max_transfers} transfer(s); skipping")
                return {
                    "watch_id": watch.id,
                    "status": "no_flights_within_transfer_limit",
                    "triggered": False,
                }

        cheapest = min(valid_flights, key=get_price)
        current_price = get_price(cheapest)

        triggered = False
        alert_result = None

        # Record the reading and claim the alert under the lock (so two scans cannot both
        # send it), but send it after releasing the lock: Telegram may take up to 10s.
        pending_alert = None
        with self._state_lock():
            watch.add_reading(current_price)

            if current_price <= watch.target_budget_try:
                triggered = True
                logger.info(
                    f"🎯 TARGET REACHED for {watch.id}! Found {current_price:,.0f} TL <= Budget {watch.target_budget_try:,.0f} TL"
                )

                # Build URL for alert
                google_url = cheapest.get("google_flights_url") or self.provider._build_google_url(
                    watch.origin, watch.destination, dep_date, ret_date
                )

                airline_name = cheapest.get("airline") or cheapest.get("airline_name") or "Havayolu"
                duration = cheapest.get("duration") or "Bilinmiyor"
                trip_type_tr = "Gidiş - Dönüş" if watch.trip_type != "oneway" else "Tek Yön"

                # Check if we should notify (avoid duplicate alert if price hasn't dropped further)
                last_alert_price = getattr(watch, "last_alert_price", None)
                should_notify = (last_alert_price is None) or (current_price < last_alert_price)

                if should_notify:
                    pending_alert = dict(
                        origin=watch.origin,
                        destination=watch.destination,
                        current_price=current_price,
                        target_budget=watch.target_budget_try,
                        airline=airline_name,
                        duration=duration,
                        google_url=google_url,
                        trip_type=trip_type_tr,
                        departure_date=dep_date,
                        times=cheapest.get("times", ""),
                    )
                    setattr(watch, "last_alert_price", current_price)
                    setattr(watch, "last_alert_at", datetime.now(timezone.utc).isoformat())
                else:
                    logger.info(f"Skipping duplicate alert for {watch.id} (already alerted at {last_alert_price} TL)")

        if pending_alert:
            alert_result = self.notifier.send_alert(**pending_alert)

        # Check Multi-Modal opportunities if enabled for this watch
        if getattr(watch, "include_multimodal", True):
            try:
                prefetched = [cheapest] if cheapest else None
                # Network search: the state lock must NOT be held here.
                mm_res = self.multimodal_router.search_multimodal(
                    origin=watch.origin,
                    destination=watch.destination,
                    date=dep_date,
                    prefetched_direct_flights=prefetched,
                )
                if mm_res and mm_res.get("has_multimodal") and mm_res.get("multimodal_routes"):
                    best_mm = mm_res["multimodal_routes"][0]
                    mm_price = float(best_mm["total_price_try"])
                    mm_savings = float(best_mm.get("savings_try", 0))

                    if mm_price <= watch.target_budget_try or (mm_savings > 1000.0 and mm_price < current_price):
                        logger.info(
                            f"🚆 MultiModal Opportunity found for {watch.id}: {mm_price:,.0f} TL (Savings: {mm_savings:,.0f} TL)"
                        )
                        dur_h = best_mm["total_duration_mins"] // 60
                        dur_m = best_mm["total_duration_mins"] % 60
                        dur_str = f"{dur_h} sa. {dur_m} dk."

                        steps = []
                        for s in best_mm.get("segments", []):
                            stype = s.get("type", "")
                            if stype == "flight":
                                steps.append(f"  ✈️ Uçuş: {s.get('operator')} ({s.get('from')} ➔ {s.get('to')}) {s.get('departure_time')}-{s.get('arrival_time')}")
                            elif stype == "transfer":
                                steps.append(f"  🔄 Transfer: {s.get('from')} ➔ {s.get('to')}")
                            elif stype in ("train", "bus"):
                                steps.append(f"  🚆 Tren: {s.get('operator')} ({s.get('from')} ➔ {s.get('to')}) {s.get('departure_time')}-{s.get('arrival_time')}")

                        steps_summary = "\n".join(steps)

                        with self._state_lock():
                            last_mm_price = getattr(watch, "last_mm_alert_price", None)
                            should_notify_mm = (last_mm_price is None) or (mm_price < last_mm_price)
                            if should_notify_mm:
                                setattr(watch, "last_mm_alert_price", mm_price)
                                setattr(watch, "last_mm_alert_at", datetime.now(timezone.utc).isoformat())

                        if should_notify_mm:
                            mm_alert = self.notifier.send_multimodal_alert(
                                origin=watch.origin,
                                destination=watch.destination,
                                multimodal_price=mm_price,
                                target_budget=watch.target_budget_try,
                                savings_vs_direct=mm_savings,
                                journey_duration_str=dur_str,
                                segments_summary=steps_summary,
                                departure_date=dep_date,
                                route_id=best_mm.get("route_id", ""),
                            )
                            alert_result = alert_result or mm_alert
                        else:
                            logger.info(f"Skipping duplicate multimodal alert for {watch.id} (already alerted at {last_mm_price} TL)")
                        triggered = True
            except Exception as e:
                logger.error(f"Error checking multimodal for {watch.id}: {e}")

        with self._state_lock():
            if self.tracker:
                self.tracker.save()

        return {
            "watch_id": watch.id,
            "origin": watch.origin,
            "destination": watch.destination,
            "departure_date": dep_date,
            "current_price": current_price,
            "target_budget": watch.target_budget_try,
            "airline": cheapest.get("airline", "Bilinmiyor"),
            "duration": cheapest.get("duration", ""),
            "triggered": triggered,
            "alert": alert_result,
        }

    def scan_all_watches(self) -> List[Dict[str, Any]]:
        """Scan all active watches and return scan summaries."""
        # prune=True drops watches deleted by another process (e.g. the server),
        # so a later save() here does not resurrect them.
        self.tracker.load(prune=True)
        with self._state_lock():
            active_watches = [w for w in self.tracker.watches.values() if w.active]
        logger.info(f"Starting scan for {len(active_watches)} active watches...")

        results = []
        today_str = self._now().strftime("%Y-%m-%d")
        for watch in active_watches:
            if _watch_is_past(watch, today_str):
                logger.info(f"Watch {watch.id} skipped: departure date is in the past")
                results.append({"watch_id": watch.id, "status": "past_dates", "triggered": False})
                continue
            res = self.scan_watch(watch)
            results.append(res)
            # Short sleep between queries to avoid high server strain
            time.sleep(1)

        logger.info(f"Scan completed. Checked {len(results)} watches.")
        return results

    def prefetch_watches(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Warm route and ground/flight caches for active trip watches without sending alerts."""
        from travel_engine.google_budget import set_current_priority, PRIORITY_PREFETCH, _current_priority
        token = set_current_priority(PRIORITY_PREFETCH)
        try:
            return self._prefetch_watches_inner(now)
        finally:
            _current_priority.reset(token)

    def _prefetch_watches_inner(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Inner implementation of prefetch_watches with prefetch priority active.

        Iterates active trip watches (watches with trip_query) and triggers plan_trip for each.
        At most one search is performed per watch (using all future departure dates).
        Does not send alerts or modify watch history.
        """
        if self.trip_planner is None:
            logger.warning("Trip prefetch skipped: no trip planner configured")
            return []

        self.tracker.load(prune=True)
        with self._state_lock():
            active_trip_watches = [
                w for w in self.tracker.watches.values()
                if w.active and getattr(w, "trip_query", None)
            ]

        logger.info(f"Starting prefetch for {len(active_trip_watches)} active trip watches...")
        results = []
        now_dt = now or datetime.now()
        today_str = now_dt.strftime("%Y-%m-%d")

        for watch in active_trip_watches:
            query = {k: v for k, v in dict(watch.trip_query or {}).items() if k in TRIP_QUERY_KEYS}
            dates = list(query.get("depart_dates") or [])
            if not dates and getattr(watch, "departure_date", None):
                dates = [watch.departure_date]

            future_dates = [d for d in dates if str(d) >= today_str] if dates else []
            if dates and not future_dates:
                logger.info(f"Skipping prefetch for {watch.id}: all depart dates in the past")
                results.append({"watch_id": watch.id, "status": "past_dates", "searched": False})
                continue

            # Limit to single plan_trip call per watch with all future dates
            if future_dates:
                query["depart_dates"] = list(future_dates)
            elif dates:
                query["depart_dates"] = list(dates)

            try:
                plan = self.trip_planner.plan_trip(
                    origin=watch.origin,
                    destination=watch.destination,
                    **query
                )
                results.append({
                    "watch_id": watch.id,
                    "status": "ok",
                    "origin": watch.origin,
                    "destination": watch.destination,
                    "searched": True,
                    "plan": plan,
                })
            except Exception as e:
                logger.error(f"Error prefetching trip for {watch.id}: {e}")
                results.append({
                    "watch_id": watch.id,
                    "status": "error",
                    "error": str(e),
                    "searched": False,
                })

        logger.info(f"Prefetch completed. Processed {len(results)} trip watches.")
        return results

    def run_loop(self, interval_hours: float = 6.0) -> None:
        """Run continuous daemon monitoring loop."""
        logger.info(f"Starting 7/24 Daemon Tracker (Interval: {interval_hours} hours)...")
        while True:
            try:
                self.scan_all_watches()
            except Exception as e:
                logger.error(f"Error in scan loop: {e}")

            sleep_seconds = int(interval_hours * 3600)
            logger.info(f"Sleeping for {interval_hours} hours ({sleep_seconds}s) until next scan...")
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [DaemonTracker] %(message)s"
    )

    parser = argparse.ArgumentParser(description="TravelPlan Daemon Price Tracker")
    parser.add_argument("--once", action="store_true", help="Perform single scan pass and exit")
    parser.add_argument("--loop", action="store_true", help="Run continuously in background")
    parser.add_argument("--interval", type=float, default=6.0, help="Interval in hours for loop mode (default: 6)")
    args = parser.parse_args()

    daemon = DaemonTracker()

    if args.once:
        print("[Daemon] Running single scan pass...")
        summaries = daemon.scan_all_watches()
        print(f"[Daemon] Done. Results: {len(summaries)}")
    elif args.loop:
        daemon.run_loop(interval_hours=args.interval)
    else:
        print("Usage: python daemon_tracker.py [--once | --loop [--interval HOURS]]")
