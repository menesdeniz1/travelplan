"""Precision Google Flights Real-Time Search Provider.

Key Improvements:
1. Robust Turkish character mapping: 'İstanbul' correctly maps to 'Istanbul' and IATA 'ista' (no more 'stanbul' or broken 'stan/prg' links).
2. Outlier filtering: Removes absurd flights (e.g. 4 transfers, 30+ hours, or >2.5x min price like 98.000 TL).
3. Smart ranking: Identifies and highlights Direct/Fastest flights vs Lowest-price flights.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import re
import subprocess
import threading
import time
import time as _stdlib_time
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from ..airports import METRO_GROUPS, CITY_CODES, AirportDB, _metro_name, haversine_km


from ..flight_cache import (
    CACHEABLE_STATUSES,
    CALENDAR_RETENTION_SECS,
    STATUS_OK,
    FlightCache,
    default_path,
    route_key,
)

if TYPE_CHECKING:
    from ..search_log import SearchLog

logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

_PLAYWRIGHT_MISSING_WARNED = False

_METRO_CITY_CODES = {v.lower(): k.lower() for k, v in CITY_CODES.items()}



# Brand names the camel-case splitter must not cut: "easyJet" is one airline, not "easy / Jet".
# "flydubaiEmirates" is a codeshare of two airlines and is deliberately not listed.
PROTECTED_AIRLINE_NAMES: Tuple[str, ...] = (
    "easyJet", "SunExpress", "AJet", "airBaltic", "EgyptAir", "AirAsia", "LOT", "KLM", "TAP", "SAS", "ITA",
    "SKY express", "Wizz Air", "Air Serbia", "TAP Air Portugal", "ITA Airways", "Turkish Airlines",
    "Qatar Airways", "British Airways", "Air France", "Brussels Airlines",
    "Pegasus", "Jet2", "Eurowings", "Vueling", "Ryanair", "Transavia", "Volotea", "Smartwings", "Norwegian",
)
# Spaced spellings of brand names, written with the brand spelling ("Sun Express" -> "SunExpress").
PROTECTED_AIRLINE_VARIANTS: Dict[str, str] = {
    "Sun Express": "SunExpress", "Egypt Air": "EgyptAir", "Air Asia": "AirAsia",
    "Air Baltic": "airBaltic", "Easy Jet": "easyJet",
}
_PROTECTED_PATTERNS: Tuple[Tuple[Any, str], ...] = tuple(
    (re.compile(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", re.IGNORECASE), canonical)
    for name, canonical in sorted(
        [(name, name) for name in PROTECTED_AIRLINE_NAMES] + list(PROTECTED_AIRLINE_VARIANTS.items()),
        key=lambda item: len(item[0]), reverse=True)
)


def clean_airline_name(raw: str) -> str:
    """Airline text of a result card: operator notes removed, codeshares as "A / B", known brands intact.

    Protected names are hidden behind placeholders while the camel-case split runs, then restored with
    their canonical spelling. Commas are normalised to ", ".
    """
    text = re.sub(r"İşleten:\s*.*", "", str(raw or "")).strip()
    text = re.sub(r"Operated by:\s*.*", "", text).strip()
    slots: List[str] = []
    for pattern, canonical in _PROTECTED_PATTERNS:
        def _hide(match: Any, canonical: str = canonical) -> str:
            slots.append(canonical)
            return f"\x00{len(slots) - 1}\x00"
        text = pattern.sub(_hide, text)
    text = re.sub(r"([a-z]{2,})([A-Z][a-z]+|[A-Z]{2,})", r"\1 / \2", text).strip()
    for index, name in enumerate(slots):
        text = text.replace(f"\x00{index}\x00", name)
    return re.sub(r"\s*,\s*", ", ", text).strip()


def _warn_playwright_missing() -> None:
    """Log the missing-Playwright warning once per process instead of failing silently."""
    global _PLAYWRIGHT_MISSING_WARNED
    if _PLAYWRIGHT_MISSING_WARNED:
        return
    _PLAYWRIGHT_MISSING_WARNED = True
    logger.warning(
        "Playwright is not installed, so Google Flights search returns no results. "
        "Run: pip install playwright && playwright install chromium"
    )


# ---------------------------------------------------------------------------
# Resource limits: browser concurrency, one reused browser, heavy-request blocking
# ---------------------------------------------------------------------------

DEFAULT_MAX_BROWSERS = 1  # one Chromium: parallel searches are opt-in (TRAVELPLAN_MAX_BROWSERS)
DEFAULT_BROWSER_IDLE_SECS = 300.0
# The shared browser is replaced after this many searches (memory creep in long-lived Chromium).
RELAUNCH_AFTER_SEARCHES = 50
# How long a caller waits for a free browser slot before the search is reported as "busy".
BROWSER_SLOT_WAIT_SECS = 300.0
# How long a caller waits for the shared browser worker to finish one search.
BROWSER_JOB_TIMEOUT_SECS = 180.0

LAUNCH_ARGS = [
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--no-first-run",
    "--no-default-browser-check",
    "--disk-cache-size=1",
    "--media-cache-size=1",
    "--disable-background-networking",
]
CONTEXT_OPTIONS = {
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "locale": "tr-TR",
}

BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
BLOCKED_HOST_MARKERS = ("googletagmanager", "google-analytics", "doubleclick", "googlesyndication")
_BLOCKED_PATH_RE = re.compile(r"\.(png|jpg|jpeg|gif|webp|svg|ico|woff|woff2|ttf|mp4|webm)$")


def should_block_request(url: str, resource_type: str) -> bool:
    """True for requests that are not needed to read flight text: media resources, analytics/ads hosts, image or font files."""
    if (resource_type or "").lower() in BLOCKED_RESOURCE_TYPES:
        return True
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if any(marker in host for marker in BLOCKED_HOST_MARKERS):
        return True
    return bool(_BLOCKED_PATH_RE.search((parts.path or "").lower()))


def _route_request(route) -> None:
    request = route.request
    if should_block_request(request.url, request.resource_type):
        route.abort()
    else:
        route.continue_()


def _is_timeout_error(exc: BaseException) -> bool:
    """True for Playwright or builtin timeouts (Playwright's class is named TimeoutError)."""
    return isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError" or "timeout" in str(exc).lower()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def max_browsers_from_env() -> int:
    """TRAVELPLAN_MAX_BROWSERS: the process-wide cap on concurrent browser pages (default 2, minimum 1)."""
    return max(1, _env_int("TRAVELPLAN_MAX_BROWSERS", DEFAULT_MAX_BROWSERS))


def browser_idle_secs_from_env() -> float:
    """TRAVELPLAN_BROWSER_IDLE_SECS: seconds without a search before the shared browser is closed (default 300)."""
    return max(1.0, _env_float("TRAVELPLAN_BROWSER_IDLE_SECS", DEFAULT_BROWSER_IDLE_SECS))


def browser_reuse_enabled() -> bool:
    """TRAVELPLAN_BROWSER_REUSE=0 turns the shared browser off and restores launch-per-search."""
    return os.environ.get("TRAVELPLAN_BROWSER_REUSE", "1").strip().lower() not in ("0", "false", "off", "no")


def _monotonic() -> float:
    return time.monotonic()


def _close_quietly(obj: Any, method: str) -> None:
    if obj is None:
        return
    try:
        getattr(obj, method)()
    except Exception:
        logger.debug("Ignoring error while closing browser resources", exc_info=True)


def process_rss_mb() -> Optional[float]:
    """Working set of this process in MB (Windows: GetProcessMemoryInfo; POSIX/macOS: ps -o rss=). None when unavailable."""
    if os.name != "nt":
        try:
            res = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(os.getpid())],
                capture_output=True,
                text=True,
                timeout=2,
            )
            raw = res.stdout.strip() if res.stdout else ""
            if not raw:
                return None
            kb = float(raw)
            return round(kb / 1024.0, 1)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    try:
        import ctypes
        from ctypes import wintypes

        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return round(counters.WorkingSetSize / (1024 * 1024), 1)
    except Exception:
        return None


DEFAULT_BROWSER_STARVATION_LIMIT = 3
BLOCK_RATE_THRESHOLD = 0.01       # 1%
EMPTY_PAGE_RATE_THRESHOLD = 0.20  # 20%
HEALTH_WINDOW_1H_SECS = 3600.0
HEALTH_WINDOW_24H_SECS = 86400.0


class _BrowserSlots:
    """Prioritized browser slot manager backed by threading.Condition.

    Grants available slots to interactive requests first while preventing
    background starvation (a background waiter receives a slot after at most
    `starvation_limit` interactive grants). FIFO order is strictly preserved
    within the same priority tier.
    """

    def __init__(self, capacity: int, starvation_limit: Optional[int] = None):
        self.capacity = int(capacity)
        if starvation_limit is None:
            try:
                self.starvation_limit = int(
                    os.environ.get("TRAVELPLAN_BROWSER_STARVATION_LIMIT", DEFAULT_BROWSER_STARVATION_LIMIT)
                )
            except (ValueError, TypeError):
                self.starvation_limit = DEFAULT_BROWSER_STARVATION_LIMIT
        else:
            self.starvation_limit = int(starvation_limit)

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self.active = 0
        self._consecutive_interactive = 0
        self._interactive_waiters: deque = deque()
        self._background_waiters: deque = deque()

    @property
    def waiting_interactive(self) -> int:
        with self._lock:
            return len(self._interactive_waiters)

    @property
    def waiting_background(self) -> int:
        with self._lock:
            return len(self._background_waiters)

    @property
    def waiting_total(self) -> int:
        with self._lock:
            return len(self._interactive_waiters) + len(self._background_waiters)

    @property
    def consecutive_interactive(self) -> int:
        with self._lock:
            return self._consecutive_interactive

    def _is_chosen(self, token: object, is_interactive: bool) -> bool:
        # 1. Anti-starvation: if background is waiting and starvation limit is reached
        if self._background_waiters and self._consecutive_interactive >= self.starvation_limit:
            return (not is_interactive) and (self._background_waiters[0] is token)

        # 2. Priority: interactive requests take precedence
        if self._interactive_waiters:
            return is_interactive and (self._interactive_waiters[0] is token)

        # 3. Otherwise, oldest background waiter
        if self._background_waiters:
            return (not is_interactive) and (self._background_waiters[0] is token)

        return False

    def _can_grant_immediate(self, is_interactive: bool) -> bool:
        """Helper for non-blocking try (timeout <= 0) when self.active < self.capacity."""
        if is_interactive:
            if self._background_waiters and self._consecutive_interactive >= self.starvation_limit:
                return False
            return len(self._interactive_waiters) == 0
        else:
            return len(self._interactive_waiters) == 0 and len(self._background_waiters) == 0

    def _grant_slot(self, is_interactive: bool) -> None:
        self.active += 1
        if is_interactive:
            if len(self._background_waiters) > 0:
                self._consecutive_interactive += 1
            else:
                self._consecutive_interactive = 0
        else:
            self._consecutive_interactive = 0

    def _pop_waiter(self, token: object, is_interactive: bool) -> None:
        queue_ref = self._interactive_waiters if is_interactive else self._background_waiters
        try:
            queue_ref.remove(token)
        except ValueError:
            pass

    def _remove_waiter_if_present(self, token: object, is_interactive: bool) -> bool:
        queue_ref = self._interactive_waiters if is_interactive else self._background_waiters
        try:
            queue_ref.remove(token)
            if len(self._background_waiters) == 0:
                self._consecutive_interactive = 0
            return True
        except ValueError:
            return False

    def acquire(self, timeout: Optional[float] = None, priority: Optional[str] = None) -> bool:
        import time as _stdlib_time
        if priority is None:
            try:
                from travel_engine.google_budget import get_current_priority, PRIORITY_INTERACTIVE
                priority = get_current_priority()
            except Exception:
                priority = "interactive"
        is_interactive = (str(priority).lower() == "interactive")

        token = object()
        has_timeout = (timeout is not None)
        deadline = (_stdlib_time.monotonic() + max(0.0, float(timeout))) if has_timeout else None

        with self._cond:
            # Immediate non-blocking check for timeout <= 0
            if has_timeout and timeout <= 0:
                if self.active < self.capacity and self._can_grant_immediate(is_interactive):
                    self._grant_slot(is_interactive)
                    return True
                return False

            if is_interactive:
                self._interactive_waiters.append(token)
            else:
                self._background_waiters.append(token)

            try:
                while True:
                    if self.active < self.capacity and self._is_chosen(token, is_interactive):
                        self._pop_waiter(token, is_interactive)
                        self._grant_slot(is_interactive)
                        if self.active < self.capacity:
                            self._cond.notify_all()
                        return True

                    if has_timeout:
                        remaining = deadline - _stdlib_time.monotonic()
                        if remaining <= 0:
                            return False
                        self._cond.wait(remaining)
                    else:
                        self._cond.wait()
            finally:
                if self._remove_waiter_if_present(token, is_interactive):
                    if self.active < self.capacity:
                        self._cond.notify_all()

    def release(self) -> None:
        with self._cond:
            if self.active <= 0:
                logger.warning("_BrowserSlots.release() called when active is %d", self.active)
                return
            self.active -= 1
            self._cond.notify_all()

    def reset(self) -> None:
        with self._cond:
            self.active = 0
            self._consecutive_interactive = 0
            self._interactive_waiters.clear()
            self._background_waiters.clear()
            self._cond.notify_all()


_slots_lock = threading.Lock()
_slots: Optional[_BrowserSlots] = None


def _browser_slots() -> _BrowserSlots:
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = _BrowserSlots(max_browsers_from_env())
        return _slots


def _reset_browser_slots() -> None:
    global _slots
    with _slots_lock:
        if _slots is not None:
            _slots.reset()
        _slots = None


class _WorkerTimeout(RuntimeError):
    """The shared browser did not finish a search inside BROWSER_JOB_TIMEOUT_SECS."""


class _BrowserWorker:
    """Owns the single Chromium of this process.

    The Playwright sync API is bound to the thread that started it, so every browser call runs on
    this worker's thread. Callers hand over a job (a function taking the browser) and wait for its
    result. The browser is launched on first use, relaunched after RELAUNCH_AFTER_SEARCHES searches
    or after a crash, and closed after `idle_secs` without a search.
    """

    def __init__(self, idle_secs: float, relaunch_after: int, poll_secs: Optional[float] = None):
        self._idle_secs = float(idle_secs)
        self._relaunch_after = int(relaunch_after)
        self._poll_secs = float(poll_secs) if poll_secs else min(5.0, self._idle_secs)
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._thread_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self._pw: Any = None
        self._browser: Any = None
        self._searches = 0
        self._last_used = _monotonic()

    # -- caller side (any thread) ------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def browser_running(self) -> bool:
        return self._browser is not None

    def searches_since_launch(self) -> int:
        return self._searches if self._browser is not None else 0

    def run(self, job: Callable[[Any], Any], timeout: float) -> Any:
        """Run job(browser) on the worker thread. Re-raises the job's exception."""
        with self._thread_lock:
            if self._closed:
                raise RuntimeError("shared browser worker is shut down")
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="travelplan-browser", daemon=True)
                self._thread.start()
        box: Dict[str, Any] = {}
        done = threading.Event()
        self._queue.put((job, box, done))
        if not done.wait(timeout):
            raise _WorkerTimeout(f"no answer within {timeout:.0f} s")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def shutdown(self, wait: bool = False) -> None:
        """Close the browser on the worker thread once queued work is done."""
        with self._thread_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        self._queue.put(None)
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    # -- worker thread -----------------------------------------------------

    def _loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=self._poll_secs)
            except queue.Empty:
                try:
                    self._check_idle()
                except Exception:
                    logger.warning("Idle check for the shared browser failed", exc_info=True)
                continue
            if item is None:
                break
            job, box, done = item
            try:
                box["value"] = self._execute(job)
            except Exception as exc:
                box["error"] = exc
            finally:
                done.set()
        self._close_browser()

    def _execute(self, job: Callable[[Any], Any]) -> Any:
        if self._browser is not None and self._searches >= self._relaunch_after:
            logger.info("Relaunching the shared browser after %d searches", self._searches)
            self._close_browser()
        if self._browser is None:
            self._launch()
        self._searches += 1
        self._last_used = _monotonic()
        try:
            return job(self._browser)
        except Exception:
            # Browser-level failure (page errors are handled inside the job): its state is unknown, drop it.
            logger.warning("Shared browser job failed; the browser will be relaunched for the next search")
            self._close_browser()
            raise
        finally:
            self._last_used = _monotonic()
            if self._browser is not None and not self._is_connected():
                logger.warning("Shared browser disconnected; it will be relaunched for the next search")
                self._close_browser()

    def _launch(self) -> None:
        pw = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True, args=list(LAUNCH_ARGS))
        except BaseException:
            _close_quietly(pw, "stop")
            raise
        self._pw = pw
        self._browser = browser
        self._searches = 0
        logger.info("Launched the shared Chromium browser")

    def _close_browser(self) -> None:
        browser, pw = self._browser, self._pw
        self._browser = None
        self._pw = None
        self._searches = 0
        _close_quietly(browser, "close")
        _close_quietly(pw, "stop")

    def _check_idle(self) -> None:
        if self._browser is not None and _monotonic() - self._last_used >= self._idle_secs:
            logger.info("Closing the shared browser after %.0f s without a search", self._idle_secs)
            self._close_browser()

    def _is_connected(self) -> bool:
        try:
            return bool(self._browser.is_connected())
        except Exception:
            return False


class _BrowserPool:
    """TRAVELPLAN_MAX_BROWSERS browsers, each owned by its own _BrowserWorker thread.

    The Playwright sync API cannot be shared across threads, so parallel pages need one
    Chromium per thread: N concurrent searches = N browsers (N = max browsers, the same cap the
    slot semaphore enforces). Dispatch prefers the least busy worker and, among equals, one whose
    browser is already running, so sequential searches keep using one browser exactly as before.
    A job counts as busy until it has actually finished on its worker, also after its caller
    gave up on a timeout.
    """

    def __init__(self, size: int, idle_secs: float, relaunch_after: int, poll_secs: Optional[float] = None):
        self._workers = [
            _BrowserWorker(idle_secs=idle_secs, relaunch_after=relaunch_after, poll_secs=poll_secs)
            for _ in range(max(1, int(size)))
        ]
        self._lock = threading.Lock()
        self._busy = [0] * len(self._workers)

    @property
    def size(self) -> int:
        return len(self._workers)

    @property
    def closed(self) -> bool:
        return all(worker.closed for worker in self._workers)

    def browser_running(self) -> bool:
        return any(worker.browser_running() for worker in self._workers)

    def browsers_running(self) -> int:
        return sum(1 for worker in self._workers if worker.browser_running())

    def searches_since_launch(self) -> int:
        return max((worker.searches_since_launch() for worker in self._workers), default=0)

    def run(self, job: Callable[[Any], Any], timeout: float) -> Any:
        """Run job(browser) on the least busy worker. Re-raises the job's exception."""
        with self._lock:
            index = min(
                range(len(self._workers)),
                key=lambda i: (self._busy[i], 0 if self._workers[i].browser_running() else 1, i),
            )
            self._busy[index] += 1
        released = threading.Event()

        def release() -> None:
            with self._lock:
                if not released.is_set():
                    released.set()
                    self._busy[index] -= 1

        def tracked(browser: Any) -> Any:
            try:
                return job(browser)
            finally:
                release()

        try:
            return self._workers[index].run(tracked, timeout=timeout)
        except _WorkerTimeout:
            raise  # the job is still queued or running on its worker; it releases itself when it ends
        except BaseException:
            release()  # the worker refused the job (shut down) or the job failed before it was tracked
            raise

    def shutdown(self, wait: bool = False) -> None:
        for worker in self._workers:
            worker.shutdown(wait=wait)


_worker_lock = threading.Lock()
_worker: Optional[_BrowserPool] = None


def _get_browser_worker() -> _BrowserPool:
    global _worker
    with _worker_lock:
        if _worker is None or _worker.closed:
            _worker = _BrowserPool(
                size=max_browsers_from_env(),
                idle_secs=browser_idle_secs_from_env(),
                relaunch_after=RELAUNCH_AFTER_SEARCHES,
            )
        return _worker


def _current_browser_worker() -> Optional[_BrowserPool]:
    with _worker_lock:
        return _worker


def _reset_browser_worker(wait: bool = False) -> None:
    global _worker
    with _worker_lock:
        worker, _worker = _worker, None
    if worker is not None:
        worker.shutdown(wait=wait)


def _shutdown_at_exit() -> None:
    _reset_browser_worker(wait=True)


atexit.register(_shutdown_at_exit)


def _new_scrape_state() -> Dict[str, Any]:
    return {
        "raw_results": [],
        "card_count": 0,
        "page_url": "",
        "body_text": "",
        "page_read": False,
        "error_detail": "",
        "calendar": {},  # calendar minimum per ISO date read from this page (persisted by search_flights)
    }


def _calendar_kind(trip_type: Optional[str]) -> str:
    """Calendar key part: "oneway" for one-way searches, "roundtrip" for everything else."""
    return "oneway" if (trip_type or "oneway") == "oneway" else "roundtrip"


def normalize_turkish(text: str) -> str:
    mapping = {
        'İ': 'I', 'I': 'I', 'ı': 'i', 'i': 'i',
        'Ğ': 'G', 'ğ': 'g',
        'Ü': 'U', 'ü': 'u',
        'Ş': 'S', 'ş': 's',
        'Ö': 'O', 'ö': 'o',
        'Ç': 'C', 'ç': 'c',
    }
    return "".join(mapping.get(c, c) for c in text)


# Stop wording used in the "stops" field of a parsed card (kept in Turkish, like the rest of the UI text)
STOPS_UNKNOWN = "bilinmiyor"
STOPS_ESTIMATED_CONNECTION = "Aktarmalı (tahmini)"
# Connection sanity check (ESTIMATE): a non-stop cannot take longer than
# DURATION_SLACK_FACTOR x (great-circle km / CRUISE_KMH + TAKEOFF_LANDING_HOURS).
CRUISE_KMH = 750.0
TAKEOFF_LANDING_HOURS = 0.5
DURATION_SLACK_FACTOR = 1.8


def parse_duration_hours(text: Any) -> Optional[float]:
    """"2 sa. 40 dk." -> 2.67, "13 sa." -> 13.0, "55 dk." -> 0.92; None when no duration is on the card."""
    norm = normalize_turkish(str(text or "")).lower()
    hours = re.search(r"(\d+)\s*sa\.", norm)
    minutes = re.search(r"(\d+)\s*dk\.", norm)
    if not hours and not minutes:
        return None
    return (int(hours.group(1)) if hours else 0) + (int(minutes.group(1)) if minutes else 0) / 60.0


def duration_too_long_for_nonstop(duration: Any, airport_pair: Optional["re.Match"]) -> bool:
    """True when the card's duration is above the non-stop limit for its two airports.

    Needs both airport codes ("ATH–PRG") to be in AirportDB; without them the check cannot say anything.
    """
    hours = parse_duration_hours(duration)
    if hours is None or airport_pair is None:
        return False
    db = AirportDB.load_default()
    origin, destination = db.get(airport_pair.group(1)), db.get(airport_pair.group(2))
    if origin is None or destination is None:
        return False
    km = haversine_km(origin.lat, origin.lon, destination.lat, destination.lon)
    limit = DURATION_SLACK_FACTOR * (km / CRUISE_KMH + TAKEOFF_LANDING_HOURS)
    return hours > limit


CITY_MAP = {
    "istanbul": {"query": "Istanbul", "iata": "ista"},
    "ist": {"query": "Istanbul", "iata": "ista"},
    "saw": {"query": "Sabiha Gokcen", "iata": "saw"},
    "atina": {"query": "Athens", "iata": "ath"},
    "athens": {"query": "Athens", "iata": "ath"},
    "ath": {"query": "Athens", "iata": "ath"},
    "prag": {"query": "Prague", "iata": "prg"},
    "prague": {"query": "Prague", "iata": "prg"},
    "prg": {"query": "Prague", "iata": "prg"},
    "roma": {"query": "Rome", "iata": "rome"},
    "rome": {"query": "Rome", "iata": "rome"},
    "londra": {"query": "London", "iata": "lond"},
    "london": {"query": "London", "iata": "lond"},
    "paris": {"query": "Paris", "iata": "pari"},
    "tokyo": {"query": "Tokyo", "iata": "tyoa"},
    "viyana": {"query": "Vienna", "iata": "vie"},
    "vienna": {"query": "Vienna", "iata": "vie"},
    "amsterdam": {"query": "Amsterdam", "iata": "ams"},
    "berlin": {"query": "Berlin", "iata": "ber"},
    "ankara": {"query": "Ankara", "iata": "esb"},
    "antalya": {"query": "Antalya", "iata": "ayt"},
    "izmir": {"query": "Izmir", "iata": "adb"},
    "madrid": {"query": "Madrid", "iata": "mad"},
    "barselona": {"query": "Barcelona", "iata": "bcn"},
    "barcelona": {"query": "Barcelona", "iata": "bcn"},
    "dubai": {"query": "Dubai", "iata": "dxb"},
    "new york": {"query": "New York", "iata": "nyca"},
    "moskova": {"query": "Moscow", "iata": "mowa"},
    "moscow": {"query": "Moscow", "iata": "mowa"},
    "baku": {"query": "Baku", "iata": "gyd"},
    "tiflis": {"query": "Tbilisi", "iata": "tbs"},
    "tbilisi": {"query": "Tbilisi", "iata": "tbs"},
    "singapur": {"query": "Singapore", "iata": "sin"},
    "singapore": {"query": "Singapore", "iata": "sin"},
    "bangkok": {"query": "Bangkok", "iata": "bkk"},
    "seul": {"query": "Seoul", "iata": "sela"},
    "seoul": {"query": "Seoul", "iata": "sela"},
    "pekin": {"query": "Beijing", "iata": "bjsa"},
    "beijing": {"query": "Beijing", "iata": "bjsa"},
    "los angeles": {"query": "Los Angeles", "iata": "laxa"},
    "chicago": {"query": "Chicago", "iata": "chia"},
    "miami": {"query": "Miami", "iata": "mia"},
    "san francisco": {"query": "San Francisco", "iata": "sfo"},
    "toronto": {"query": "Toronto", "iata": "ytoa"},
    "kahire": {"query": "Cairo", "iata": "cai"},
    "cairo": {"query": "Cairo", "iata": "cai"},
}


AIRPORT_CITIES = {
    # Türkiye Hubs
    "IST": "İstanbul",
    "SAW": "Sabiha Gökçen",
    "ESB": "Ankara",
    "ADB": "İzmir",
    "AYT": "Antalya",
    "BJV": "Bodrum",
    "DLM": "Dalaman",
    "TZX": "Trabzon",
    "ADA": "Adana",
    "GZT": "Gaziantep",
    "GNY": "Şanlıurfa",
    "ASR": "Kayseri",
    "NAV": "Nevşehir Kapadokya",
    "DIY": "Diyarbakır",
    # Western & Central Europe
    "CDG": "Paris CDG",
    "ORY": "Paris Orly",
    "BVA": "Paris Beauvais",
    "LHR": "Londra Heathrow",
    "LGW": "Londra Gatwick",
    "STN": "Londra Stansted",
    "LTN": "Londra Luton",
    "MAN": "Manchester",
    "EDI": "Edinburgh",
    "BHX": "Birmingham",
    "AMS": "Amsterdam",
    "FRA": "Frankfurt",
    "MUC": "Münih",
    "BER": "Berlin",
    "DUS": "Düsseldorf",
    "HAM": "Hamburg",
    "CGN": "Köln",
    "STR": "Stuttgart",
    "BRU": "Brüksel",
    "CRL": "Brüksel Charleroi",
    "ZRH": "Zürih",
    "GVA": "Cenevre",
    "BSL": "Basel",
    "VIE": "Viyana",
    "WAW": "Varşova",
    "KRK": "Krakow",
    "PRG": "Prag",
    "BUD": "Budapeşte",
    # Southern & Eastern Europe
    "FCO": "Roma Fiumicino",
    "CIA": "Roma Ciampino",
    "MXP": "Milano Malpensa",
    "LIN": "Milano Linate",
    "BGY": "Bergamo",
    "VCE": "Venedik",
    "BLQ": "Bologna",
    "NAP": "Napoli",
    "BCN": "Barselona",
    "MAD": "Madrid",
    "VLC": "Valensiya",
    "AGP": "Malaga",
    "SVQ": "Sevilla",
    "LIS": "Lizbon",
    "OPO": "Porto",
    "ATH": "Atina",
    "SKG": "Selanik",
    "HER": "Girit Kandiye",
    "RHO": "Rodos",
    "BEG": "Belgrad",
    "SOF": "Sofya",
    "OTP": "Bükreş",
    "ZAG": "Zagreb",
    "SJJ": "Saraybosna",
    "SKP": "Üsküp",
    "TIA": "Tiran",
    "LCA": "Larnaka",
    "PFO": "Baf",
    # Northern Europe
    "CPH": "Kopenhag",
    "ARN": "Stockholm",
    "OSL": "Oslo",
    "HEL": "Helsinki",
    "DUB": "Dublin",
    # Middle East & Gulf
    "DOH": "Doha",
    "DXB": "Dubai",
    "DWC": "Dubai Al Maktoum",
    "AUH": "Abu Dhabi",
    "SHJ": "Şarika",
    "AMM": "Amman",
    "KWI": "Kuveyt",
    "BAH": "Bahreyn",
    "JED": "Cidde",
    "RUH": "Riyad",
    "MED": "Medine",
    "DMM": "Dammam",
    "MCT": "Maskat",
    "BEY": "Beyrut",
    # Caucasus, Russia & CIS
    "GYD": "Bakü",
    "TBS": "Tiflis",
    "EVN": "Erivan",
    "SVO": "Moskova Şeremetyevo",
    "DME": "Moskova Domodedovo",
    "VKO": "Moskova Vnukovo",
    "LED": "St. Petersburg",
    "TAS": "Taşkent",
    "ALA": "Almatı",
    "NQZ": "Astana",
    "FRU": "Bişkek",
    "DYU": "Duşanbe",
    # Asia & Pacific
    "BKK": "Bangkok",
    "DMK": "Bangkok Don Mueang",
    "HKT": "Phuket",
    "CNX": "Chiang Mai",
    "KUL": "Kuala Lumpur",
    "SIN": "Singapur",
    "DPS": "Bali",
    "CGK": "Cakarta",
    "MNL": "Manila",
    "SGN": "Ho Chi Minh",
    "HAN": "Hanoi",
    "ICN": "Seul Incheon",
    "GMP": "Seul Gimpo",
    "PEK": "Pekin Capital",
    "PKX": "Pekin Daxing",
    "PVG": "Şanghay Pudong",
    "SHA": "Şanghay Hongqiao",
    "CAN": "Guangzhou",
    "HKG": "Hong Kong",
    "TPE": "Taipei",
    "URC": "Ürümçi",
    "TFU": "Çengdu Tianfu",
    "CTU": "Çengdu",
    "XIY": "Xi'an",
    "WUH": "Vuhan",
    "KMG": "Kunming",
    "XMN": "Xiamen",
    "HND": "Tokyo Haneda",
    "NRT": "Tokyo Narita",
    "KIX": "Osaka Kansai",
    "DEL": "Yeni Delhi",
    "BOM": "Mumbai",
    "MLE": "Maldivler Male",
    "CMB": "Kolombo",
    "SYD": "Sidney",
    "MEL": "Melbourne",
    "BNE": "Brisbane",
    "AKL": "Auckland",
    # North America
    "JFK": "New York JFK",
    "EWR": "New York Newark",
    "LGA": "New York LaGuardia",
    "LAX": "Los Angeles",
    "SFO": "San Francisco",
    "ORD": "Chicago O'Hare",
    "MIA": "Miami",
    "MCO": "Orlando",
    "BOS": "Boston",
    "IAD": "Washington Dulles",
    "DCA": "Washington Reagan",
    "ATL": "Atlanta",
    "DFW": "Dallas",
    "IAH": "Houston",
    "SEA": "Seattle",
    "LAS": "Las Vegas",
    "YYZ": "Toronto",
    "YUL": "Montreal",
    "YVR": "Vancouver",
    "MEX": "Mexico City",
    "CUN": "Cancun",
    # Africa & South America
    "CAI": "Kahire",
    "HRG": "Hurgada",
    "SSH": "Şarm El Şeyh",
    "CMN": "Kazablanka",
    "RAK": "Marakeş",
    "TUN": "Tunus",
    "ALG": "Cezayir",
    "JNB": "Johannesburg",
    "CPT": "Cape Town",
    "NBO": "Nairobi",
    "ADD": "Addis Ababa",
    "GRU": "Sao Paulo",
    "GIG": "Rio de Janeiro",
    "EZE": "Buenos Aires",
    "SCL": "Santiago",
    "BOG": "Bogota",
    "LIM": "Lima",
}


# ---------------------------------------------------------------------------
# Page classification and health tracking
# ---------------------------------------------------------------------------

# Statuses that count as a failed read (feed the consecutive-failure counter).
FAILURE_STATUSES = frozenset({"parse_error", "empty_page", "blocked", "error"})
# Statuses that prove the page was read correctly.
SUCCESS_STATUSES = frozenset({"ok", "no_flights"})
# This many failures in a row make the provider report itself unhealthy.
UNHEALTHY_AFTER = 3
RECENT_WINDOW = 20

# Matched against text folded with normalize_turkish(...).lower().
# The Turkish no-results texts were seen on a live hl=tr page; the English ones are assumed.
_NO_RESULTS_MARKERS = (
    "hicbir sonuc yok",                 # "Hiçbir sonuç yok."  (verified live)
    "aramanizla eslesen secenek yok",   # "Aramanızla eşleşen seçenek yok" (verified live)
    "ucus bulunamadi",                  # "uçuş bulunamadı" (assumed)
    "sonuc yok",                        # "sonuç yok" (assumed)
    "no flights found",                 # assumed
    "no matching flights",              # assumed
    "no results",                       # assumed
)
_BLOCKED_MARKERS = (
    "unusual traffic",
    "olagan disi trafik",               # "olağan dışı trafik" (Google /sorry page, assumed)
    "bilgisayar aginizdan",             # "Bilgisayar ağınızdan..." (Google /sorry page, assumed)
    "captcha",
    "robot",
    "before you continue to google",    # consent wall (assumed)
    "google'a gitmeden once",           # consent wall, Turkish (assumed)
)


def _fold(text: str) -> str:
    return normalize_turkish(text).lower()


def classify_page(url: str, body_text: str, card_count: int, parsed_count: int) -> str:
    """Classify a loaded Google Flights results page.

    Returns one of: "ok", "no_flights", "parse_error", "empty_page", "blocked".
    ("error" and "unavailable" are decided by the caller: exception / Playwright missing.)
    """
    url_l = (url or "").lower()
    if "/sorry/" in url_l:
        return "blocked"
    if parsed_count > 0:
        return "ok"
    if card_count > 0:
        return "parse_error"
    text = _fold(body_text or "")
    if "consent.google" in url_l or any(marker in text for marker in _BLOCKED_MARKERS):
        return "blocked"
    if any(marker in text for marker in _NO_RESULTS_MARKERS):
        return "no_flights"
    return "empty_page"


class GoogleFlightsProvider:
    # Process-wide state: Playwright launches and Google's IP-level block are shared by all instances.
    _shared_lock = threading.Lock()
    _next_launch_at = 0.0   # time.monotonic() value before which no new browser may launch
    _blocked_until = 0.0    # time.monotonic() value until which live searches are refused
    _stats_page_loads = 0
    _stats_page_secs = 0.0
    _stats_l1_hits = 0
    _stats_l2_hits = 0
    _stats_blocked = 0
    _stats_failed = 0
    _page_history: deque = deque(maxlen=5000)
    _threshold_alert_active: bool = False
    _threshold_callbacks: List[Callable[[str, Dict[str, Any]], None]] = []

    def __init__(
        self,
        cache_ttl_seconds: int = 600,
        min_interval_secs: float = 2.0,
        blocked_cooldown_secs: float = 900.0,
        on_health_change: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_threshold_alert: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        search_log: Optional["SearchLog"] = None,
        flight_cache: Optional[FlightCache] = None,
    ):
        # L1: this process, in memory. L2: persistent SQLite (flight_cache.py), opened on first use.
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.calendar_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.RLock()
        self._flight_cache = flight_cache
        self._flight_cache_resolved = flight_cache is not None
        self.cache_ttl = cache_ttl_seconds
        self.min_interval_secs = float(min_interval_secs)
        self.blocked_cooldown_secs = float(blocked_cooldown_secs)
        self.on_health_change = on_health_change
        self.on_threshold_alert = on_threshold_alert
        if on_threshold_alert is not None:
            with GoogleFlightsProvider._shared_lock:
                if on_threshold_alert not in GoogleFlightsProvider._threshold_callbacks:
                    GoogleFlightsProvider._threshold_callbacks.append(on_threshold_alert)
        self.search_log = search_log
        self.last_search_status: Optional[Dict[str, Any]] = None
        self.health: Dict[str, Any] = {
            "consecutive_failures": 0,
            "last_ok_at": None,
            "last_status": None,
            "recent": deque(maxlen=RECENT_WINDOW),
        }
        self._health_lock = threading.RLock()

    # -- rate limiting and blocked cooldown (process-wide) ------------------

    @classmethod
    def reset_shared_state(cls) -> None:
        """Forget the launch timestamp and any blocked cooldown (for tests or a manual reset)."""
        with GoogleFlightsProvider._shared_lock:
            GoogleFlightsProvider._next_launch_at = 0.0
            GoogleFlightsProvider._blocked_until = 0.0
            GoogleFlightsProvider._stats_page_loads = 0
            GoogleFlightsProvider._stats_page_secs = 0.0
            GoogleFlightsProvider._stats_l1_hits = 0
            GoogleFlightsProvider._stats_l2_hits = 0
            GoogleFlightsProvider._stats_blocked = 0
            GoogleFlightsProvider._stats_failed = 0
            GoogleFlightsProvider._page_history.clear()
            GoogleFlightsProvider._threshold_alert_active = False
            GoogleFlightsProvider._threshold_callbacks.clear()
        _reset_browser_worker()
        _reset_browser_slots()
        try:
            from travel_engine.google_budget import reset_google_budget
            reset_google_budget()
        except Exception:
            pass

    @classmethod
    def _compute_window_metrics_locked(
        cls,
        window_secs: float,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if now_ts is None:
            now_ts = _stdlib_time.time()
        cutoff = float(now_ts) - float(window_secs)
        matching = [e for e in cls._page_history if e["ts"] >= cutoff]
        page_count = len(matching)
        if page_count == 0:
            return {
                "page_count": 0,
                "block_rate": 0.0,
                "empty_page_rate": 0.0,
                "avg_page_duration": 0.0,
                "avg_page_secs": 0.0,
                "blocked_count": 0,
                "empty_page_count": 0,
            }
        blocked_count = sum(1 for e in matching if e["status"] == "blocked")
        empty_count = sum(1 for e in matching if e["status"] == "empty_page")
        total_duration = sum(e["duration"] for e in matching)
        avg_dur = round(total_duration / page_count, 3)
        return {
            "page_count": page_count,
            "block_rate": round(blocked_count / page_count, 4),
            "empty_page_rate": round(empty_count / page_count, 4),
            "avg_page_duration": avg_dur,
            "avg_page_secs": avg_dur,
            "blocked_count": blocked_count,
            "empty_page_count": empty_count,
        }

    @classmethod
    def window_metrics(cls, window_secs: float, now_ts: Optional[float] = None) -> Dict[str, Any]:
        with GoogleFlightsProvider._shared_lock:
            return cls._compute_window_metrics_locked(window_secs, now_ts)

    @classmethod
    def record_page_scrape(
        cls,
        status: str,
        duration: float,
        timestamp: Optional[float] = None,
    ) -> None:
        if timestamp is None:
            timestamp = _stdlib_time.time()
        alert_to_send = None
        metrics_snapshot = None
        with GoogleFlightsProvider._shared_lock:
            cls._page_history.append({
                "ts": float(timestamp),
                "status": str(status),
                "duration": float(duration or 0.0),
            })
            m1h = cls._compute_window_metrics_locked(HEALTH_WINDOW_1H_SECS, now_ts=timestamp)
            m24h = cls._compute_window_metrics_locked(HEALTH_WINDOW_24H_SECS, now_ts=timestamp)

            reasons = []
            if m1h["page_count"] > 0 and m1h["block_rate"] > BLOCK_RATE_THRESHOLD:
                reasons.append(f"son 1 saatte engel oranı %{m1h['block_rate'] * 100:.1f} (eşik %1)")
            elif m24h["page_count"] > 0 and m24h["block_rate"] > BLOCK_RATE_THRESHOLD:
                reasons.append(f"son 24 saatte engel oranı %{m24h['block_rate'] * 100:.1f} (eşik %1)")

            if m1h["page_count"] > 0 and m1h["empty_page_rate"] > EMPTY_PAGE_RATE_THRESHOLD:
                reasons.append(f"son 1 saatte boş sayfa oranı %{m1h['empty_page_rate'] * 100:.1f} (eşik %20)")
            elif m24h["page_count"] > 0 and m24h["empty_page_rate"] > EMPTY_PAGE_RATE_THRESHOLD:
                reasons.append(f"son 24 saatte boş sayfa oranı %{m24h['empty_page_rate'] * 100:.1f} (eşik %20)")

            is_threshold_exceeded = bool(reasons)

            if is_threshold_exceeded:
                if not cls._threshold_alert_active:
                    cls._threshold_alert_active = True
                    reason_text = ", ".join(reasons)
                    alert_to_send = f"⚠️ Google Flights sağlık uyarısı: {reason_text}. Aramalar etkilenebilir."
                    metrics_snapshot = {"1h": m1h, "24h": m24h}
            else:
                cls._threshold_alert_active = False

            callbacks = list(cls._threshold_callbacks)

        if alert_to_send and callbacks:
            for cb in callbacks:
                try:
                    cb(alert_to_send, metrics_snapshot)
                except Exception:
                    logger.exception("on_threshold_alert callback failed")

    @classmethod
    def stats(cls) -> Dict[str, Any]:
        with GoogleFlightsProvider._shared_lock:
            return {
                "page_loads": cls._stats_page_loads,
                "page_secs": round(float(cls._stats_page_secs), 3),
                "l1_hits": cls._stats_l1_hits,
                "l2_hits": cls._stats_l2_hits,
                "blocked": cls._stats_blocked,
                "failed": cls._stats_failed,
            }

    @classmethod
    def _wait_for_launch_slot(cls, min_interval: float) -> None:
        """Reserve the next launch slot under the lock, then sleep outside it."""
        now = time.monotonic()
        with GoogleFlightsProvider._shared_lock:
            start = max(now, GoogleFlightsProvider._next_launch_at)
            GoogleFlightsProvider._next_launch_at = start + max(0.0, float(min_interval))
        delay = start - now
        if delay > 0:
            time.sleep(delay)

    @classmethod
    def _cooldown_remaining(cls) -> float:
        with GoogleFlightsProvider._shared_lock:
            return max(0.0, GoogleFlightsProvider._blocked_until - time.monotonic())

    # -- health and search status ------------------------------------------

    def health_status(self) -> Dict[str, Any]:
        """JSON-friendly health summary."""
        with self._health_lock:
            return self._health_snapshot_locked()

    def _health_snapshot_locked(self) -> Dict[str, Any]:
        failures = self.health["consecutive_failures"]
        from travel_engine.google_budget import get_google_budget
        budget_stats = get_google_budget().stats()
        with GoogleFlightsProvider._shared_lock:
            m1h = GoogleFlightsProvider._compute_window_metrics_locked(HEALTH_WINDOW_1H_SECS)
            m24h = GoogleFlightsProvider._compute_window_metrics_locked(HEALTH_WINDOW_24H_SECS)
            stats_dict = {
                "page_loads": GoogleFlightsProvider._stats_page_loads,
                "page_secs": round(float(GoogleFlightsProvider._stats_page_secs), 3),
                "l1_hits": GoogleFlightsProvider._stats_l1_hits,
                "l2_hits": GoogleFlightsProvider._stats_l2_hits,
                "blocked": GoogleFlightsProvider._stats_blocked,
                "failed": GoogleFlightsProvider._stats_failed,
            }
        return {
            "healthy": failures < UNHEALTHY_AFTER,
            "consecutive_failures": failures,
            "last_ok_at": self.health["last_ok_at"],
            "last_status": self.health["last_status"],
            "recent": list(self.health["recent"]),
            "last_search": dict(self.last_search_status) if self.last_search_status else None,
            "blocked_cooldown_remaining_secs": round(self._cooldown_remaining(), 1),
            "budget": budget_stats,
            "google_budget": budget_stats,
            "metrics": {
                "1h": m1h,
                "24h": m24h,
            },
            "metrics_1h": m1h,
            "metrics_24h": m24h,
            "stats": stats_dict,
        }

    def _finish_search(
        self,
        meta: Dict[str, Any],
        status: str,
        count: int,
        min_price: Optional[float],
        detail: str,
        duration: Optional[float] = None,
    ) -> None:
        """Record the outcome of a live (non-cached) search: status, health, cooldown, search log."""
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if status == "blocked":
            with GoogleFlightsProvider._shared_lock:
                GoogleFlightsProvider._stats_blocked += 1
                GoogleFlightsProvider._blocked_until = time.monotonic() + self.blocked_cooldown_secs
            logger.warning(
                "Google Flights blocked the scraper; live searches paused for %.0f s",
                self.blocked_cooldown_secs,
            )
        if status in FAILURE_STATUSES:
            with GoogleFlightsProvider._shared_lock:
                GoogleFlightsProvider._stats_failed += 1
        with self._health_lock:
            self.last_search_status = {
                "status": status,
                "origin": meta["origin"],
                "destination": meta["destination"],
                "date": meta["date"],
                "count": count,
                "at": at,
                "detail": detail,
            }
        if duration is not None:
            GoogleFlightsProvider.record_page_scrape(status, duration)
        self._update_health(status, at)
        self._log_search(meta, status, count, min_price, detail)

    def _update_health(self, status: str, at_iso: str) -> None:
        payload = None
        with self._health_lock:
            was_healthy = self.health["consecutive_failures"] < UNHEALTHY_AFTER
            if status in FAILURE_STATUSES:
                self.health["consecutive_failures"] += 1
            elif status in SUCCESS_STATUSES:
                self.health["consecutive_failures"] = 0
                self.health["last_ok_at"] = at_iso
            self.health["last_status"] = status
            self.health["recent"].append(status)
            is_healthy = self.health["consecutive_failures"] < UNHEALTHY_AFTER
            if is_healthy != was_healthy:
                payload = self._health_snapshot_locked()
        if payload is not None and self.on_health_change is not None:
            try:
                self.on_health_change(payload)
            except Exception:
                logger.exception("on_health_change callback failed")

    def _log_search(
        self,
        meta: Dict[str, Any],
        status: str,
        count: int,
        min_price: Optional[float],
        detail: str,
    ) -> None:
        if self.search_log is None:
            return
        try:
            extra = f"return={meta['return_date']};" if meta.get("return_date") else ""
            self.search_log.record(
                meta["log_origin"],
                meta["log_destination"],
                meta["date"],
                status,
                count,
                min_price,
                source=meta["source"],
                detail=f"{extra}{detail}",
            )
        except Exception:
            logger.warning("Could not write search log entry", exc_info=True)

    def _parse_calendar_price(self, txt: str) -> Optional[float]:
        if not txt:
            return None
        # Check for thousands "B" notation (e.g. ₺13,1 B or 11 B)
        m_bin = re.search(r'[₺\$\€\£]\s*([\d\.,]+)\s*B', txt, re.IGNORECASE)
        if m_bin:
            val_str = m_bin.group(1).replace(".", "").replace(",", ".")
            try:
                return round(float(val_str) * 1000, 0)
            except Exception:
                pass
        # Check for standard price with currency symbol (₺8.090 or $120)
        m_reg = re.search(r'[₺\$\€\£]\s*([\d\.,]+)', txt)
        if m_reg:
            val_str = m_reg.group(1).replace(".", "").replace(",", ".")
            try:
                return float(val_str)
            except Exception:
                pass
        # Check for price followed by TL (e.g. 8.090 TL)
        m_tl = re.search(r'\b([\d\.,]{3,})\s*(?:TL|TRY)\b', txt, re.IGNORECASE)
        if m_tl:
            val_str = m_tl.group(1).replace(".", "").replace(",", ".")
            try:
                return float(val_str)
            except Exception:
                pass
        return None

    def get_calendar_prices(self, origin: str, destination: str, trip_type: str = "oneway",
                            max_age_secs: Optional[float] = None) -> Dict[str, float]:
        """Known Google calendar minimum per ISO date for a route (no browser). {} when nothing is known.

        Read from memory first, then from the persistent cache (entries live FLIGHT_CACHE TTL).
        """
        norm_orig, _ = self._normalize_city(origin)
        norm_dest, _ = self._normalize_city(destination)
        key = f"{norm_orig}_{norm_dest}_{trip_type}"
        with self._cache_lock:
            entry = self.calendar_cache.get(key)
            if entry:
                # Calendar prices go stale like flight results; honour a TTL of 6x the flight cache TTL (1h by default)
                now_ts = datetime.now(timezone.utc).timestamp()
                effective_mem_ttl = (
                    min(float(max_age_secs), float(CALENDAR_RETENTION_SECS))
                    if max_age_secs is not None
                    else (6 * self.cache_ttl)
                )
                if now_ts - entry.get("timestamp", 0) < effective_mem_ttl:
                    return entry.get("prices", {})
                self.calendar_cache.pop(key, None)
        store = self.persistent_cache()
        if store is None:
            return {}
        return store.get_calendar(
            route_key(norm_orig, norm_dest, _calendar_kind(trip_type)),
            max_age_secs=max_age_secs,
        )

    def persistent_cache(self) -> Optional[FlightCache]:
        """The SQLite cache (L2), or None when disabled (TRAVELPLAN_FLIGHT_CACHE=off). Created on first use."""
        with self._cache_lock:
            if not self._flight_cache_resolved:
                self._flight_cache_resolved = True
                path = default_path()
                self._flight_cache = FlightCache(path) if path else None
            return self._flight_cache

    def search_concurrency(self) -> int:
        """How many flight searches the caller may run at once: one page per browser."""
        return max_browsers_from_env()

    def _normalize_city(self, name: str) -> tuple[str, str]:
        raw_name = (name or "").strip()
        base_name = re.sub(r'\(.*?\)', '', raw_name).strip()
        if not base_name:
            base_name = raw_name

        db = AirportDB.load_default()
        kind, aps = db._resolve_kind(base_name)
        if kind in ("airport", "city") and aps:
            if kind == "city":
                metro = _metro_name(aps[0].iata)
                if metro and len(METRO_GROUPS.get(metro, ())) > 1:
                    key = _METRO_CITY_CODES.get(metro.lower(), METRO_GROUPS[metro][0].lower())
                    return metro, key
                if len(aps) > 1:
                    return aps[0].city, aps[0].iata.lower()
            return aps[0].iata.upper(), aps[0].iata.lower()

        # Fallback to existing behavior if unresolved
        ascii_clean = normalize_turkish(base_name).strip().lower()
        if ascii_clean in CITY_MAP:
            return CITY_MAP[ascii_clean]["query"], CITY_MAP[ascii_clean]["iata"]

        clean_text = re.sub(r'[^a-zA-Z0-9\s]', '', ascii_clean).strip()
        iata = clean_text[:4].lower() if clean_text else "ist"
        return clean_text.title(), iata

    def search_flights(
        self,
        origin: str = "Istanbul",
        destination: str = "Prague",
        departure_date: str = "2026-10-25",
        return_date: Optional[str] = "2026-10-31",
        trip_type: str = "roundtrip",
    ) -> List[Dict[str, Any]]:
        is_one_way = (trip_type == "oneway") or not return_date

        norm_orig_query, orig_iata = self._normalize_city(origin)
        norm_dest_query, dest_iata = self._normalize_city(destination)

        cache_key = f"{norm_orig_query}_{norm_dest_query}_{departure_date}_{return_date if not is_one_way else 'oneway'}"
        now_ts = datetime.now(timezone.utc).timestamp()

        with self._cache_lock:
            entry = self.cache.get(cache_key)
            if entry is not None and now_ts - entry["timestamp"] < self.cache_ttl:
                with GoogleFlightsProvider._shared_lock:
                    GoogleFlightsProvider._stats_l1_hits += 1
                return entry["results"]

        route = route_key(norm_orig_query, norm_dest_query, "oneway" if is_one_way else f"roundtrip:{return_date}")
        store = self.persistent_cache()
        if store is not None:
            hit = store.get_results(route, departure_date)
            if hit is not None:
                with GoogleFlightsProvider._shared_lock:
                    GoogleFlightsProvider._stats_l2_hits += 1
                # Served from disk: no page, no health change, no search-log row (it is not a live search).
                results = hit["results"]
                self.last_search_status = {
                    "status": hit["status"], "origin": origin, "destination": destination,
                    "date": departure_date, "count": len(results), "source": "cache",
                    "at": datetime.fromtimestamp(hit["stored_at"], timezone.utc).isoformat(timespec="seconds"),
                    "detail": "persistent cache",
                }
                if results:
                    self._remember(cache_key, now_ts, results)
                return results

        # Build Google Flights Query
        enc_orig = urllib.parse.quote(norm_orig_query)
        enc_dest = urllib.parse.quote(norm_dest_query)

        if is_one_way:
            google_url = (
                f"https://www.google.com/travel/flights?q=one-way%20flights%20from%20{enc_orig}%20to%20{enc_dest}%20on%20"
                f"{departure_date}&hl=tr&gl=tr&curr=TRY"
            )
        else:
            google_url = (
                f"https://www.google.com/travel/flights?q=round-trip%20flights%20from%20{enc_orig}%20to%20{enc_dest}%20on%20"
                f"{departure_date}%20through%20{return_date}&hl=tr&gl=tr&curr=TRY"
            )

        raw_results = []
        meta = {
            "origin": origin,
            "destination": destination,
            "date": departure_date,
            "log_origin": orig_iata if len(orig_iata) == 3 else norm_orig_query,
            "log_destination": dest_iata if len(dest_iata) == 3 else norm_dest_query,
            "source": "google_flights" if is_one_way else "google_flights_roundtrip",
            "return_date": None if is_one_way else return_date,
        }
        if not PLAYWRIGHT_AVAILABLE:
            _warn_playwright_missing()
            self._finish_search(meta, "unavailable", 0, None, "playwright not installed")
            return raw_results

        if self._cooldown_remaining() > 0:
            self._finish_search(meta, "blocked_cooldown", 0, None, "live search refused during block cooldown")
            return raw_results

        cal_key = f"{norm_orig_query}_{norm_dest_query}_{'oneway' if is_one_way else 'roundtrip'}"
        # A fresh calendar of this route (memory or disk) is not read again from the page.
        want_calendar = not self.get_calendar_prices(origin, destination, "oneway" if is_one_way else "roundtrip")
        logger.info("Search %s -> %s on %s: calendar %s", origin, destination, departure_date,
                    "will be read" if want_calendar else "is fresh, skipped")

        from travel_engine.google_budget import get_google_budget, get_current_priority
        priority = get_current_priority()
        if not get_google_budget().take(priority):
            logger.warning("Google Flights budget exhausted for priority %s (%s -> %s)", priority, origin, destination)
            self._finish_search(meta, "budget_exhausted", 0, None, "budget_exhausted")
            return raw_results

        # One search = one browser page. The slot caps concurrent pages (and so Chromium work)
        # across every caller in this process: UI searches, route engine and daemon.
        slots = _browser_slots()
        if not slots.acquire(timeout=BROWSER_SLOT_WAIT_SECS):
            logger.warning("No browser slot free for %s -> %s after %.0f s", origin, destination, BROWSER_SLOT_WAIT_SECS)
            self._finish_search(meta, "error", 0, None, "busy")
            return raw_results
        t_start = time.monotonic()
        try:
            state = self._run_scrape(google_url, is_one_way, cal_key, now_ts, want_calendar=want_calendar)
        finally:
            slots.release()
            duration = time.monotonic() - t_start
            with GoogleFlightsProvider._shared_lock:
                GoogleFlightsProvider._stats_page_loads += 1
                GoogleFlightsProvider._stats_page_secs += duration

        raw_results = state["raw_results"]
        page_read = state["page_read"]
        page_url = state["page_url"]
        body_text = state["body_text"]
        card_count = state["card_count"]
        error_detail = state["error_detail"]

        if page_read:
            status = classify_page(page_url, body_text, card_count, len(raw_results))
        else:
            status = "error"

        # Clean and Filter Outliers
        filtered_results = self._filter_and_rank(raw_results)
        min_price = min((f["total_price_try"] for f in filtered_results), default=None)
        detail = error_detail or f"cards={card_count} parsed={len(raw_results)} url={page_url.split('?')[0][:120]}"
        self._finish_search(meta, status, len(filtered_results), min_price, detail)

        if store is not None:
            if state.get("calendar"):
                store.put_calendar(
                    route_key(norm_orig_query, norm_dest_query, _calendar_kind("oneway" if is_one_way else "roundtrip")),
                    state["calendar"])
            # Only ok / no_flights reach the disk cache; the store refuses every other status.
            if status in CACHEABLE_STATUSES:
                store.put_results(route, departure_date, status, filtered_results)

        if filtered_results:
            self._remember(cache_key, now_ts, filtered_results)

        return filtered_results

    def _remember(self, cache_key: str, now_ts: float, results: List[Dict[str, Any]]) -> None:
        """Store results in the in-memory cache (L1), evicting the oldest entries beyond 100."""
        with self._cache_lock:
            if len(self.cache) > 100:
                oldest_keys = sorted(self.cache.keys(), key=lambda k: self.cache[k].get("timestamp", 0))[:20]
                for ok in oldest_keys:
                    self.cache.pop(ok, None)
            self.cache[cache_key] = {"timestamp": now_ts, "results": results}

    # -- browser execution -------------------------------------------------------

    def resource_status(self) -> Dict[str, Any]:
        """Browser and memory figures for /api/health. Makes no Playwright calls."""
        worker = _current_browser_worker()
        running = worker is not None and worker.browser_running()
        slots = _browser_slots()
        return {
            "browser_running": bool(running),
            "browsers_running": worker.browsers_running() if running else 0,
            "active_pages": slots.active,
            "max_browsers": slots.capacity,
            "searches_since_launch": worker.searches_since_launch() if running else 0,
            "process_rss_mb": process_rss_mb(),
        }

    def _run_scrape(self, google_url: str, is_one_way: bool, cal_key: str, now_ts: float,
                    want_calendar: bool = True) -> Dict[str, Any]:
        # Space out browser work (shared across instances and threads).
        self._wait_for_launch_slot(self.min_interval_secs)
        if browser_reuse_enabled():
            try:
                return self._scrape_persistent(google_url, is_one_way, cal_key, now_ts, want_calendar)
            except _WorkerTimeout as exc:
                # Do not fall back: the shared browser is still busy and a second one would exceed the cap.
                logger.warning("Shared browser did not finish the search: %s", exc)
                state = _new_scrape_state()
                state["error_detail"] = f"{type(exc).__name__}: {exc}"
                return state
            except Exception as exc:
                logger.warning(
                    "Shared browser failed (%s: %s); using launch-per-search for this search",
                    type(exc).__name__, exc,
                )
        return self._scrape_launch_per_search(google_url, is_one_way, cal_key, now_ts, want_calendar)

    def _scrape_persistent(self, google_url: str, is_one_way: bool, cal_key: str, now_ts: float,
                           want_calendar: bool = True) -> Dict[str, Any]:
        def job(browser: Any) -> Dict[str, Any]:
            state = _new_scrape_state()
            # A failure here means the browser is unusable; the caller falls back to launch-per-search.
            context = browser.new_context(**CONTEXT_OPTIONS)
            try:
                self._scrape_page_guarded(context, state, google_url, is_one_way, cal_key, now_ts, want_calendar)
            finally:
                _close_quietly(context, "close")
            return state

        return _get_browser_worker().run(job, timeout=BROWSER_JOB_TIMEOUT_SECS)

    def _scrape_launch_per_search(self, google_url: str, is_one_way: bool, cal_key: str, now_ts: float,
                                  want_calendar: bool = True) -> Dict[str, Any]:
        state = _new_scrape_state()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=list(LAUNCH_ARGS))
                try:
                    context = browser.new_context(**CONTEXT_OPTIONS)
                    try:
                        self._scrape_page(context, state, google_url, is_one_way, cal_key, now_ts, want_calendar)
                    finally:
                        context.close()
                finally:
                    browser.close()
        except Exception as e:
            logger.warning("Search error: %s", e)
            state["error_detail"] = f"{type(e).__name__}: {e}"
        return state

    def _scrape_page_guarded(self, context: Any, state: Dict[str, Any], google_url: str,
                             is_one_way: bool, cal_key: str, now_ts: float, want_calendar: bool = True) -> None:
        """Page-level failures are recorded in `state`; browser-level failures still propagate."""
        try:
            self._scrape_page(context, state, google_url, is_one_way, cal_key, now_ts, want_calendar)
        except Exception as e:
            logger.warning("Search error: %s", e)
            state["error_detail"] = f"{type(e).__name__}: {e}"

    def _scrape_page(self, context: Any, state: Dict[str, Any], google_url: str,
                     is_one_way: bool, cal_key: str, now_ts: float, want_calendar: bool = True) -> None:
        page = context.new_page()

        # RAM & network: media, fonts, images and analytics/ads hosts are aborted before download.
        # Flight cards are plain text, so this does not change what gets parsed.
        page.route("**/*", _route_request)

        page.goto(google_url, wait_until="domcontentloaded", timeout=25000)

        # Cookie consent
        try:
            accept_btn = page.query_selector("button:has-text('Tümünü kabul et'), button:has-text('Accept all')")
            if accept_btn and accept_btn.is_visible():
                accept_btn.click()
        except Exception:
            pass

        # Wait for flight cards to be present first
        try:
            page.wait_for_selector("li.pIav2d", timeout=6000)
        except Exception:
            pass

        # Extract Real Google Flights Calendar Lowest Prices. Skipped when this route's calendar is still
        # fresh (want_calendar False): the calendar costs about 2 s of browser work per search.
        if want_calendar:
            try:
                page.wait_for_selector("input[placeholder='Gidiş'], input[aria-label='Gidiş']", timeout=3500)
                date_inp = page.query_selector("input[placeholder='Gidiş'], input[aria-label='Gidiş']")
                if date_inp:
                    date_inp.click(force=True)
                    page.wait_for_selector("div[data-iso]", timeout=5000)
                    # The cells exist before their price text is rendered: without this pause every cell reads empty.
                    time.sleep(0.5)
                    cal_cells = page.query_selector_all("div[data-iso]")
                    cal_prices = {}
                    for c in cal_cells:
                        iso = c.get_attribute("data-iso")
                        if iso:
                            cell_txt = c.inner_text().replace("\n", " ").strip()
                            p_val = self._parse_calendar_price(cell_txt)
                            if p_val and p_val > 0:
                                cal_prices[iso] = p_val
                    if cal_prices:
                        with self._cache_lock:
                            if len(self.calendar_cache) > 100:
                                oldest_cal = sorted(self.calendar_cache.keys(), key=lambda k: self.calendar_cache[k].get("timestamp", 0))[:20]
                                for ok in oldest_cal:
                                    self.calendar_cache.pop(ok, None)
                            self.calendar_cache[cal_key] = {
                                "timestamp": now_ts,
                                "prices": cal_prices
                            }
                        state["calendar"] = dict(cal_prices)
                        logger.info("Calendar extracted: %d real dates cached for %s", len(cal_prices), cal_key)
                    page.keyboard.press("Escape")
            except Exception as e:
                # The calendar is optional: a timeout only means no calendar prices for this search (not a warning).
                if _is_timeout_error(e):
                    logger.info("Calendar extraction timed out (non-fatal, live results still used): %s", e)
                else:
                    logger.warning("Calendar extraction error: %s", e)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass

        # Click expand / show more button ONCE to reveal all other flights
        try:
            expand_btn = page.query_selector("button:has-text('Daha fazla uçuş'), [aria-label*='Daha fazla uçuş']")
            if expand_btn and expand_btn.is_visible():
                expand_btn.click()
                page.wait_for_timeout(400)
        except Exception:
            pass

        card_texts = None
        if hasattr(page, "eval_on_selector_all") and callable(getattr(page, "eval_on_selector_all", None)):
            try:
                card_texts = page.eval_on_selector_all("li.pIav2d", "els => els.map(e => e.innerText)")
            except Exception:
                card_texts = None

        if card_texts is None:
            try:
                items = page.query_selector_all("li.pIav2d")
                card_texts = [it.inner_text() for it in items]
            except Exception:
                card_texts = []

        trip_label = "Tek Yön" if is_one_way else "Gidiş - Dönüş Toplam"
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Parse up to 150 flight items across the expanded catalog
        for raw_text in card_texts[:150]:
            try:
                parsed = self._parse_flight_item(raw_text, google_url, trip_label, observed_at=observed_at)
                if parsed:
                    state["raw_results"].append(parsed)
            except Exception:
                pass
        state["card_count"] = len(card_texts)
        state["page_url"] = page.url
        # Body text is read only when classify_page really needs it (no cards or no parsed flights)
        if not state["raw_results"] or not card_texts:
            try:
                state["body_text"] = page.inner_text("body")
            except Exception:
                state["body_text"] = ""
        else:
            state["body_text"] = ""
        state["page_read"] = True

    def _filter_and_rank(self, flights: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter duplicates, remove absurd multi-leg junk flights, and rank by best value."""
        if not flights:
            return []

        # Deduplicate identical flights
        unique_map = {}
        for f in flights:
            key = (f["airline"], f["times"], f["total_price_try"], f["duration"])
            if key not in unique_map:
                unique_map[key] = f
        unique_flights = list(unique_map.values())
        if not unique_flights:
            return []

        # Find minimum price
        min_price = min(f["total_price_try"] for f in unique_flights)

        # Filter out extreme multi-hop outliers
        reasonable = []
        for f in unique_flights:
            # Always keep direct flights regardless of price
            if f.get("is_direct"):
                reasonable.append(f)
                continue
            # Discard flights with 3+ transfers
            if "3 aktarma" in f.get("stops", "") or "4 aktarma" in f.get("stops", ""):
                continue
            # Keep connecting flights up to 3.0x min price
            if f["total_price_try"] > min_price * 3.0:
                continue
            reasonable.append(f)

        if not reasonable:
            reasonable = unique_flights[:20]

        # Sort by total_price_try ascending (cheapest first)
        reasonable.sort(key=lambda x: x["total_price_try"])

        # Mark direct flights and cheapest flights
        direct_flights = [f for f in reasonable if f["is_direct"]]
        if direct_flights:
            cheapest_direct = min(direct_flights, key=lambda x: x["total_price_try"])
            cheapest_direct["badge"] = "⚡ Aktarmasız / En Hızlı"

        # Make sure direct flights are never cut off by the list limit
        final_list = reasonable[:40]
        for df in direct_flights:
            if df not in final_list:
                final_list.append(df)

        if final_list:
            if not final_list[0].get("badge"):
                final_list[0]["badge"] = "🏷️ En Uygun Fiyat"

        return final_list

    def _parse_flight_item(self, text: str, google_url: str, trip_label: str, observed_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if len(lines) < 4:
            return None
        if observed_at is None:
            observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Price extraction: matches ₺3.890 or 3.890 TL
        price_val = None
        for line in lines:
            m = re.search(r'(?:₺|TL)\s*([\d\.]+)|([\d\.]+)\s*(?:₺|TL)', line)
            if m:
                num_str = m.group(1) or m.group(2)
                try:
                    clean_str = num_str.replace(".", "").replace(",", ".")
                    price_val = float(clean_str)
                    break
                except ValueError:
                    pass

        if not price_val:
            return None

        # Times
        times_str = lines[0]
        if len(lines) > 2 and "–" in lines[1]:
            times_str = f"{lines[0]} – {lines[2]}"

        # Airline & Duration
        airline = lines[3] if len(lines) > 3 and "sa." not in lines[3] and "dk." not in lines[3] else (lines[1] if len(lines) > 1 else "Havayolu")
        duration = lines[4] if len(lines) > 4 and ("sa." in lines[4] or "dk." in lines[4]) else (lines[2] if len(lines) > 2 and ("sa." in lines[2] or "dk." in lines[2]) else "Bilinmiyor")

        # Clean airline string (known brand names stay intact; codeshares become "A / B")
        airline = clean_airline_name(airline)
        if not airline:
            airline = "Havayolu"

        # Airport Pair (e.g. IST ➔ NRT, SAW ➔ HND)
        full_text = " ".join(lines)
        airports = ""
        m_air = re.search(r'\b([A-Z]{3})\s*[–\-]\s*([A-Z]{3})\b', full_text)
        if m_air:
            airports = f"{m_air.group(1)} ➔ {m_air.group(2)}"

        # Stops: a card is direct only when it says so ("Aktarmasız" / "Nonstop"). A card without any stop
        # text is NOT assumed non-stop (live QA: a 7.5 h Pegasus connection was shown as direct).
        norm_full = normalize_turkish(full_text).lower()
        m_stop = re.search(r'(\d+)\s*aktarma', norm_full)
        explicit_nonstop = bool(re.search(r'aktarmasiz|nonstop', norm_full))
        layover_info = ""
        if m_stop:
            stops_count = int(m_stop.group(1))
            is_direct, stops = False, f"{stops_count} aktarma"
        elif explicit_nonstop:
            is_direct, stops_count, stops = True, 0, "Aktarmasız"
        elif "aktarma" in norm_full:
            is_direct, stops_count, stops = False, 1, "1 aktarma"
        else:
            # No stop information on the card at all: a connection of unknown length
            is_direct, stops_count, stops = False, 1, STOPS_UNKNOWN

        # Sanity check: a duration far above what the great-circle distance allows is a connection
        m_air_codes = re.search(r'\b([A-Z]{3})\s*[–\-]\s*([A-Z]{3})\b', full_text)
        if (is_direct or stops == STOPS_UNKNOWN) and duration_too_long_for_nonstop(duration, m_air_codes):
            is_direct, stops_count, stops = False, 1, STOPS_ESTIMATED_CONNECTION

        if not is_direct:
            # Extract layover airport and time (e.g. 2 sa. 40 dk. BKK)
            for line in lines:
                if ("sa." in line or "saat" in line or "dk." in line) and line != duration:
                    m_lay = re.search(r'(\d+.*?\b([A-Z]{3})\b)', line)
                    if m_lay:
                        code = m_lay.group(2)
                        if code not in ["IST", "SAW", "TRY"]:
                            city = AIRPORT_CITIES.get(code, "")
                            layover_raw = m_lay.group(1)
                            layover_info = f"{layover_raw} ({city})" if city else layover_raw
                            break

        return {
            "source": "Google Flights (Canlı)",
            "airline": airline,
            "times": times_str,
            "duration": duration,
            "airports": airports,
            "stops": stops,
            "stops_count": stops_count,
            "is_direct": is_direct,
            "layover_info": layover_info,
            "total_price_try": price_val,
            "currency": "TRY",
            "trip_type": trip_label,
            "badge": None,
            "google_flights_url": google_url,
            "observed_at": observed_at,
        }

    def _build_google_url(self, origin: str, destination: str, dep: str, ret: Optional[str] = None) -> str:
        norm_orig, _ = self._normalize_city(origin)
        norm_dest, _ = self._normalize_city(destination)
        enc_orig = urllib.parse.quote(norm_orig)
        enc_dest = urllib.parse.quote(norm_dest)
        if not ret:
            return f"https://www.google.com/travel/flights?q=one-way%20flights%20from%20{enc_orig}%20to%20{enc_dest}%20on%20{dep}&hl=tr&gl=tr&curr=TRY"
        return f"https://www.google.com/travel/flights?q=round-trip%20flights%20from%20{enc_orig}%20to%20{enc_dest}%20on%20{dep}%20through%20{ret}&hl=tr&gl=tr&curr=TRY"


