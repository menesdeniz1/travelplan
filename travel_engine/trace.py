"""Search trace (OLC-1): wall-clock phase times and counters of one search.

A phase's time is the sum of all its entries. Phases nest: a nested phase's time is also inside its
parent's. Thread-safe, because the flight searches of a batch may run in threads; the times of
parallel entries add up, so a phase can show more seconds than the wall time it covered.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator


class SearchTrace:
    def __init__(self, clock: Callable[[], float] = time.perf_counter):
        self._clock = clock
        self._lock = threading.Lock()
        self._started = clock()
        self.phases: Dict[str, float] = {}
        self.counters: Dict[str, Any] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started = self._clock()
        try:
            yield
        finally:
            elapsed = self._clock() - started
            with self._lock:
                self.phases[name] = self.phases.get(name, 0.0) + elapsed

    def add(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + amount

    def set(self, name: str, value: Any) -> None:
        with self._lock:
            self.counters[name] = value

    def to_dict(self) -> Dict[str, Any]:
        """{"total_secs", "phases": {name: secs}, "counters": {name: value}}; seconds rounded to ms."""
        with self._lock:
            return {
                "total_secs": round(self._clock() - self._started, 3),
                "phases": {name: round(secs, 3) for name, secs in self.phases.items()},
                "counters": dict(self.counters),
            }
