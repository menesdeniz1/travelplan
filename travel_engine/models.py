"""TravelPlan core domain models: price watches, their price readings and trip rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any


@dataclass
class PriceReading:
    timestamp: datetime
    price_try: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "price_try": self.price_try,
        }


@dataclass
class TripWatch:
    """User-configured watch for route budget alerts."""
    id: str
    origin: str
    destination: str
    target_budget_try: float
    departure_date: Optional[str] = None
    return_date: Optional[str] = None
    trip_type: str = "oneway"
    max_transfers: int = 3
    departure_time_slot: str = "all"  # "all" | "morning" | "afternoon" | "evening" | "night" | "custom"
    time_window_start: Optional[str] = None  # e.g. "00:00"
    time_window_end: Optional[str] = None    # e.g. "11:00"
    include_multimodal: bool = True
    active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_alert_price: Optional[float] = None
    last_alert_at: Optional[str] = None
    last_mm_alert_price: Optional[float] = None
    last_mm_alert_at: Optional[str] = None
    history: List[PriceReading] = field(default_factory=list)
    # Trip watches: the traveller's rules for a multi-leg search. Keys of trip_query:
    # depart_dates, arrive_by, depart_after, via, include_side_routes. When set, the
    # daemon uses the trip planner instead of the single-date flight search.
    trip_query: Optional[Dict[str, Any]] = None
    # Budget for a direct (single-airline, no side route) option. target_budget_try is
    # the side-route budget for trip watches.
    direct_budget_try: Optional[float] = None
    # Last price alerted per category, e.g. {"direct": 9500.0, "side": 6500.0}.
    last_trip_alert: Optional[Dict[str, float]] = None

    def add_reading(self, price: float, ts: Optional[datetime] = None) -> None:
        self.history.append(PriceReading(ts or datetime.now(timezone.utc), price))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "origin": self.origin,
            "destination": self.destination,
            "target_budget_try": self.target_budget_try,
            "departure_date": self.departure_date,
            "return_date": self.return_date,
            "trip_type": self.trip_type,
            "max_transfers": self.max_transfers,
            "departure_time_slot": getattr(self, "departure_time_slot", "all"),
            "time_window_start": getattr(self, "time_window_start", None),
            "time_window_end": getattr(self, "time_window_end", None),
            "include_multimodal": getattr(self, "include_multimodal", True),
            "active": self.active,
            "created_at": self.created_at.isoformat(),
            "latest_price": self.history[-1].price_try if self.history else None,
            "last_alert_price": self.last_alert_price,
            "last_alert_at": self.last_alert_at,
            "last_mm_alert_price": getattr(self, "last_mm_alert_price", None),
            "last_mm_alert_at": getattr(self, "last_mm_alert_at", None),
            "trip_query": self.trip_query,
            "direct_budget_try": self.direct_budget_try,
            "last_trip_alert": self.last_trip_alert,
            "history": [r.to_dict() for r in self.history[-10:]],
        }

