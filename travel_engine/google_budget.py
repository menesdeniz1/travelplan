"""Process-wide Google request governor with priority classes (GOO-8, card B05).

Limits Google requests per hour and per day across all Google scrapers/APIs
(Google Flights, Google Maps Transit, Google Hotels).

Priority classes:
- interactive: High-priority user searches. Can use up to 100% of the limit.
- watch: Medium-priority price watch checks by the daemon.
- prefetch: Low-priority nightly prefetch jobs.

Background classes (watch, prefetch) are collectively capped at 70% of the limit,
reserving the remaining 30% for interactive user searches.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

ENV_HOURLY_LIMIT = "TRAVELPLAN_GOOGLE_HOURLY_LIMIT"
ENV_DAILY_LIMIT = "TRAVELPLAN_GOOGLE_DAILY_LIMIT"

DEFAULT_HOURLY_LIMIT = 60
DEFAULT_DAILY_LIMIT = 300
BACKGROUND_RATIO = 0.70

PRIORITY_INTERACTIVE = "interactive"
PRIORITY_WATCH = "watch"
PRIORITY_PREFETCH = "prefetch"
PRIORITY_CLASSES = frozenset({PRIORITY_INTERACTIVE, PRIORITY_WATCH, PRIORITY_PREFETCH})
BACKGROUND_PRIORITIES = frozenset({PRIORITY_WATCH, PRIORITY_PREFETCH})

_current_priority: contextvars.ContextVar[str] = contextvars.ContextVar(
    "google_priority", default=PRIORITY_INTERACTIVE
)


def get_current_priority() -> str:
    """Return the active request priority class for the current context."""
    return _current_priority.get()


def set_current_priority(priority: str) -> contextvars.Token:
    """Set the active request priority class for the current context."""
    if priority not in PRIORITY_CLASSES:
        priority = PRIORITY_INTERACTIVE
    return _current_priority.set(priority)


def _hourly_limit_from_env() -> int:
    val = os.environ.get(ENV_HOURLY_LIMIT, "").strip()
    if not val:
        return DEFAULT_HOURLY_LIMIT
    try:
        return max(1, int(val))
    except (ValueError, TypeError):
        return DEFAULT_HOURLY_LIMIT


def _daily_limit_from_env() -> int:
    val = os.environ.get(ENV_DAILY_LIMIT, "").strip()
    if not val:
        return DEFAULT_DAILY_LIMIT
    try:
        return max(1, int(val))
    except (ValueError, TypeError):
        return DEFAULT_DAILY_LIMIT


class GoogleBudgetGovernor:
    """Thread-safe rate limiter managing process-wide Google requests."""

    def __init__(
        self,
        hourly_limit: Optional[int] = None,
        daily_limit: Optional[int] = None,
        background_ratio: float = BACKGROUND_RATIO,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.hourly_limit = hourly_limit if hourly_limit is not None else _hourly_limit_from_env()
        self.daily_limit = daily_limit if daily_limit is not None else _daily_limit_from_env()
        self.background_ratio = background_ratio
        self.clock = clock or time.time

        self._lock = threading.Lock()

        # Window state
        now = self.clock()
        self._current_hour = int(now // 3600)
        self._current_day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")

        self._hourly_count = 0
        self._daily_count = 0
        self._hourly_by_kind: Dict[str, int] = {p: 0 for p in PRIORITY_CLASSES}
        self._daily_by_kind: Dict[str, int] = {p: 0 for p in PRIORITY_CLASSES}
        self._refused_by_kind: Dict[str, int] = {p: 0 for p in PRIORITY_CLASSES}

        self._total_granted = 0
        self._total_refused = 0

    @property
    def background_hourly_limit(self) -> int:
        return int(self.hourly_limit * self.background_ratio)

    @property
    def background_daily_limit(self) -> int:
        return int(self.daily_limit * self.background_ratio)

    def _refresh_windows_locked(self, now: float) -> None:
        """Reset hourly and daily counters if their windows have elapsed."""
        hour_now = int(now // 3600)
        if hour_now != self._current_hour:
            self._current_hour = hour_now
            self._hourly_count = 0
            self._hourly_by_kind = {p: 0 for p in PRIORITY_CLASSES}

        day_now = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        if day_now != self._current_day:
            self._current_day = day_now
            self._daily_count = 0
            self._daily_by_kind = {p: 0 for p in PRIORITY_CLASSES}

    def take(self, kind: Optional[str] = None) -> bool:
        """Attempt to consume 1 Google page quota for the given priority kind.

        Returns True if granted, False if refused.
        """
        priority = kind or get_current_priority()
        if priority not in PRIORITY_CLASSES:
            priority = PRIORITY_INTERACTIVE

        now = self.clock()
        with self._lock:
            self._refresh_windows_locked(now)

            # Check background caps if priority is watch or prefetch
            if priority in BACKGROUND_PRIORITIES:
                bg_hourly = sum(self._hourly_by_kind.get(p, 0) for p in BACKGROUND_PRIORITIES)
                bg_daily = sum(self._daily_by_kind.get(p, 0) for p in BACKGROUND_PRIORITIES)

                if bg_hourly >= self.background_hourly_limit:
                    self._refused_by_kind[priority] += 1
                    self._total_refused += 1
                    logger.warning(
                        "Google budget refused %s: background hourly limit %d/%d reached",
                        priority,
                        bg_hourly,
                        self.background_hourly_limit,
                    )
                    return False

                if bg_daily >= self.background_daily_limit:
                    self._refused_by_kind[priority] += 1
                    self._total_refused += 1
                    logger.warning(
                        "Google budget refused %s: background daily limit %d/%d reached",
                        priority,
                        bg_daily,
                        self.background_daily_limit,
                    )
                    return False

            # Check overall caps
            if self._hourly_count >= self.hourly_limit:
                self._refused_by_kind[priority] += 1
                self._total_refused += 1
                logger.warning(
                    "Google budget refused %s: total hourly limit %d/%d reached",
                    priority,
                    self._hourly_count,
                    self.hourly_limit,
                )
                return False

            if self._daily_count >= self.daily_limit:
                self._refused_by_kind[priority] += 1
                self._total_refused += 1
                logger.warning(
                    "Google budget refused %s: total daily limit %d/%d reached",
                    priority,
                    self._daily_count,
                    self.daily_limit,
                )
                return False

            # Request granted
            self._hourly_count += 1
            self._daily_count += 1
            self._hourly_by_kind[priority] = self._hourly_by_kind.get(priority, 0) + 1
            self._daily_by_kind[priority] = self._daily_by_kind.get(priority, 0) + 1
            self._total_granted += 1
            return True

    def stats(self) -> Dict[str, Any]:
        """Return a snapshot of current budget limits and consumption."""
        now = self.clock()
        with self._lock:
            self._refresh_windows_locked(now)
            return {
                "hourly_limit": self.hourly_limit,
                "daily_limit": self.daily_limit,
                "background_hourly_limit": self.background_hourly_limit,
                "background_daily_limit": self.background_daily_limit,
                "hourly_count": self._hourly_count,
                "daily_count": self._daily_count,
                "hourly_remaining": max(0, self.hourly_limit - self._hourly_count),
                "daily_remaining": max(0, self.daily_limit - self._daily_count),
                "hourly_by_kind": dict(self._hourly_by_kind),
                "daily_by_kind": dict(self._daily_by_kind),
                "refused_by_kind": dict(self._refused_by_kind),
                "total_granted": self._total_granted,
                "total_refused": self._total_refused,
            }


_governor: Optional[GoogleBudgetGovernor] = None
_gov_lock = threading.Lock()


def get_google_budget() -> GoogleBudgetGovernor:
    """Return the process-wide GoogleBudgetGovernor singleton."""
    global _governor
    with _gov_lock:
        if _governor is None:
            _governor = GoogleBudgetGovernor()
        return _governor


def reset_google_budget(
    hourly_limit: Optional[int] = None,
    daily_limit: Optional[int] = None,
    clock: Optional[Callable[[], float]] = None,
) -> GoogleBudgetGovernor:
    """Reset and reinitialize the process-wide singleton (mainly for tests)."""
    global _governor
    with _gov_lock:
        _governor = GoogleBudgetGovernor(
            hourly_limit=hourly_limit,
            daily_limit=daily_limit,
            clock=clock,
        )
        return _governor
