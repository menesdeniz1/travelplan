"""Google Hotels provider for overnight stays (Phase 3b / T10).

Fetches the cheapest hotel for 1 adult for 1 night from Google Hotels:
``https://www.google.com/travel/hotels/<city>?dates=YYYY-MM-DD,YYYY-MM-DD&hl=tr&gl=tr``.

Features:
- Opt-in: only runs when ``TRAVELPLAN_HOTELS=1`` (or "true", "yes").
- Shared Playwright browser pool from ``google_flights_provider``.
- Pacing: at least 8.0 s between page loads in the same process.
- 30-minute cooldown on block/captcha (``/sorry/`` or unusual traffic markers).
- 24-hour cache: fast in-memory dictionary + SQLite persistence (temporary DB under test runs).
- Per-search page load budget: ``HotelsBudget(limit=2)`` (via ``TRAVELPLAN_HOTELS_BUDGET``).
- Safe: never raises exceptions to callers; reports health via ``health_status()``.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from travel_engine.providers import google_flights_provider as gf
from travel_engine.storage import BUSY_TIMEOUT_MS, is_test_run, retry_on_busy, test_run_temp_dir

logger = logging.getLogger(__name__)

SOURCE = "google_hotels"
ENV_HOTELS = "TRAVELPLAN_HOTELS"
ENV_HOTELS_BUDGET = "TRAVELPLAN_HOTELS_BUDGET"
ENV_CACHE_PATH = "TRAVELPLAN_HOTELS_CACHE"

DEFAULT_BUDGET_LIMIT = 2
DEFAULT_MIN_INTERVAL_SECS = 8.0
DEFAULT_CACHE_TTL_SECS = 24 * 3600
BLOCK_COOLDOWN_SECS = 30 * 60
PAGE_WAIT_MS = 3000
PAGE_JOB_TIMEOUT_SECS = 90.0
SLOT_WAIT_SECS = 120.0
FAILURE_THRESHOLD = 3

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PRICE_RE = re.compile(
    r"(?:(?:₺|TL)\s*(?P<amt1>\d[\d.,]*))|(?P<amt2>\d[\d.,]*)\s*(?:₺|TL)",
    re.IGNORECASE,
)

_BLOCK_MARKERS = (
    "unusual traffic",
    "are you a robot",
    "sira disi bir trafik",
    "robot oldugunuzu",
    "robot olmadiginizi",
)

_EXCLUDED_HOTEL_NAMES = {
    "ana icerige gec",
    "ana içeriğe geç",
    "erisilebilirlikle ilgili geri bildirim",
    "erişilebilirlikle ilgili geri bildirim",
    "kesfet",
    "keşfet",
    "ucuslar",
    "uçuşlar",
    "oteller",
    "kiralik yerler",
    "kiralık yerler",
    "oturum acin",
    "oturum açın",
    "tum filtreler",
    "tüm filtreler",
    "fiyat",
    "mulk turu",
    "mülk türü",
    "teklifler",
    "konuk puani",
    "konuk puanı",
    "otel sinifi",
    "otel sınıfı",
    "sunulan olanaklar",
    "markalar",
    "eko sertifikali",
    "eko sertifikalı",
    "siralama olcutu",
    "sıralama ölçütü",
    "nerede kalinabilir?",
    "nerede kalınabilir?",
    "gidilecek en uygun zaman",
    "odeyeceginiz tutar",
    "ödeyeceğiniz tutar",
    "booking.com",
    "agoda",
    "expedia",
    "hotels.com",
    "tripadvisor",
    "trivago",
}


class _BudgetExhausted(Exception):
    """The hotel search page-load budget was exhausted."""


def _now() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _fold(text: str) -> str:
    text = (text or "").replace("İ", "I").replace("ı", "i")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _amount(text: str) -> float:
    s = text.strip()
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", "") if len(s.rsplit(",", 1)[1]) == 3 else s.replace(",", ".")
    elif "." in s and len(s.rsplit(".", 1)[1]) == 3:
        s = s.replace(".", "")
    return float(s)


def _is_block_page(final_url: str, text: str) -> bool:
    if "/sorry/" in (final_url or ""):
        return True
    folded = _fold(text or "")
    return any(marker in folded for marker in _BLOCK_MARKERS)


def parse_hotel_cards(text: str) -> List[Dict[str, Any]]:
    """Parse hotel name and TRY prices from the page body text."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    cards: List[Dict[str, Any]] = []

    for i, line in enumerate(lines):
        m = _PRICE_RE.search(line)
        if not m:
            continue
        amt_str = m.group("amt1") or m.group("amt2")
        if not amt_str:
            continue
        try:
            price = _amount(amt_str)
        except (ValueError, TypeError):
            continue

        if price < 100.0 or price > 200000.0:
            continue

        if i == 0:
            continue
        candidate_name = lines[i - 1].strip()

        # Validate candidate name
        folded_name = _fold(candidate_name)
        if folded_name in _EXCLUDED_HOTEL_NAMES:
            continue
        if any(folded_name.startswith(p) for p in ("ucretli", "ücretli", "sponsorlu", "otel", "puan")):
            continue
        if "/5" in candidate_name or len(candidate_name) < 2 or len(candidate_name) > 100:
            continue
        if all(ch.isdigit() or ch in ".,₺TL " for ch in candidate_name):
            continue

        cards.append({
            "hotel": candidate_name,
            "price_try": round(price, 2),
            "source": SOURCE,
        })

    return cards


# -- Playwright Fetching ----------------------------------------------------

_IMAGE_OR_FONT = re.compile(r"\.(?:png|jpg|jpeg|gif|webp|svg|woff2?|ttf|eot)(?:\?.*)?$", re.IGNORECASE)


def _route(route: Any) -> None:
    try:
        url = route.request.url
        if _IMAGE_OR_FONT.search(url):
            route.abort()
        else:
            route.continue_()
    except Exception:
        pass


def _reject_consent(page: Any) -> bool:
    for label in ("Tümünü reddet", "Reject all", "Tümünü reddedin", "Reject"):
        try:
            button = page.get_by_role("button", name=label)
            if button.count():
                button.first.click(timeout=3000)
                page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    return False


def _page_job(browser: Any, url: str, locale: str, wait_ms: int) -> Dict[str, Any]:
    """Execute one page load on the shared browser, set 1 adult, and return final_url + text."""
    context = None
    try:
        context = browser.new_context(
            user_agent=gf.CONTEXT_OPTIONS["user_agent"],
            locale=locale,
            viewport={"width": 1366, "height": 900},
        )
        context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = context.new_page()
        page.route("**/*", _route)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if "consent." in page.url and _reject_consent(page):
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(wait_ms)

        # Set 1 adult via UI controls
        try:
            picker = page.locator(
                'div[role="button"][aria-label*="Yolcu sayısı"], '
                'div[role="button"][aria-label*="yolcu"], '
                'div[role="button"][aria-label*="occupancy"], '
                'div[role="button"][aria-label*="guest"], '
                'div[role="button"][aria-label*="Guests"]'
            ).first
            if picker.count():
                picker.click(timeout=3000)
                page.wait_for_timeout(500)
                remove_btn = page.locator(
                    'button[aria-label="Yetişkini kaldır"], '
                    'button[aria-label*="kaldır"], '
                    'button[aria-label*="Remove adult"]'
                ).first
                if remove_btn.count():
                    remove_btn.click(timeout=3000)
                    page.wait_for_timeout(500)
                done_btn = page.locator('button:has-text("Bitti"), button:has-text("Tamam"), button:has-text("Done")').first
                if done_btn.count():
                    done_btn.click(timeout=3000)
                    page.wait_for_timeout(1500)
        except Exception as exc:
            logger.debug("Google Hotels: error setting 1 adult: %s", exc)

        page.wait_for_timeout(wait_ms)
        return {"final_url": page.url, "text": page.inner_text("body")}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        if context is not None:
            gf._close_quietly(context, "close")


def _playwright_fetch(url: str, *, locale: str, wait_ms: int) -> Tuple[str, str]:
    """Acquire shared browser slot and run page job."""
    slots = gf._browser_slots()
    if not slots.acquire(timeout=SLOT_WAIT_SECS):
        raise RuntimeError("no browser slot free")
    try:
        result = gf._get_browser_worker().run(
            lambda browser: _page_job(browser, url, locale, wait_ms),
            timeout=PAGE_JOB_TIMEOUT_SECS,
        )
    finally:
        slots.release()
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["final_url"], result["text"]


# -- Shared State & Pacing --------------------------------------------------

class _Shared:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.next_page_at = 0.0
        self.blocked_until = 0.0
        self.requests = 0
        self.last_ok_at: Optional[str] = None
        self.last_status: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_error_at: Optional[str] = None
        self.consecutive_failures = 0


_SHARED = _Shared()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _reserve_page_slot(min_interval: float) -> None:
    now = _now()
    with _SHARED.lock:
        start = max(now, _SHARED.next_page_at)
        _SHARED.next_page_at = start + max(0.0, min_interval)
        _SHARED.requests += 1
    if start > now:
        _sleep(start - now)


def _blocked_remaining() -> float:
    with _SHARED.lock:
        return max(0.0, _SHARED.blocked_until - _now())


def _start_cooldown(cooldown_secs: float) -> None:
    with _SHARED.lock:
        _SHARED.blocked_until = _now() + max(0.0, cooldown_secs)


def _record_ok() -> None:
    with _SHARED.lock:
        _SHARED.last_ok_at = _iso_now()
        _SHARED.last_status = "ok"
        _SHARED.consecutive_failures = 0


def _record_failure(status: str, message: str) -> None:
    with _SHARED.lock:
        _SHARED.last_status = status
        _SHARED.last_error = message
        _SHARED.last_error_at = _iso_now()
        _SHARED.consecutive_failures += 1


# -- Budget -----------------------------------------------------------------

class HotelsBudget:
    """Per-search budget limiting the number of Google Hotels page loads."""

    def __init__(self, limit: int = DEFAULT_BUDGET_LIMIT) -> None:
        self.limit = max(0, int(limit))
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True


# -- SQLite Cache -----------------------------------------------------------

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS hotel_cache (
        city TEXT NOT NULL,
        checkin TEXT NOT NULL,
        hotel TEXT NOT NULL,
        price_try REAL NOT NULL,
        source TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        PRIMARY KEY (city, checkin)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hotel_cache_expiry ON hotel_cache (expires_at)",
)


def _default_db_path() -> Optional[str]:
    override = os.environ.get(ENV_CACHE_PATH, "").strip()
    if override.lower() in ("off", "0", "false", "no", "none"):
        return None
    if override:
        return os.path.abspath(override)
    if is_test_run():
        return os.path.join(test_run_temp_dir(), f"hotel_cache_{uuid.uuid4().hex}.db")
    return os.path.join(_REPO_ROOT, "hotel_cache.db")


# -- Main Provider ----------------------------------------------------------

class GoogleHotelsProvider:
    """Fetches real hotel prices for overnight stays from Google Hotels."""

    def __init__(
        self,
        *,
        fetch_page: Optional[Callable[..., Tuple[str, str]]] = None,
        db_path: Optional[str] = None,
        min_interval_secs: float = DEFAULT_MIN_INTERVAL_SECS,
        cache_ttl_secs: float = DEFAULT_CACHE_TTL_SECS,
        blocked_cooldown_secs: float = BLOCK_COOLDOWN_SECS,
        wait_ms: int = PAGE_WAIT_MS,
    ) -> None:
        self._fetch_page = fetch_page or _playwright_fetch
        self.min_interval_secs = float(min_interval_secs)
        self.cache_ttl_secs = float(cache_ttl_secs)
        self.blocked_cooldown_secs = float(blocked_cooldown_secs)
        self.wait_ms = int(wait_ms)
        self.db_path = _default_db_path() if db_path is None else db_path
        self._mem_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        self._db_ready = False

    @property
    def enabled(self) -> bool:
        """Opt-in: enabled only when TRAVELPLAN_HOTELS is '1', 'true' or 'yes'."""
        val = os.environ.get(ENV_HOTELS, "").strip().lower()
        return val in ("1", "true", "yes")

    @staticmethod
    def reset_shared_state() -> None:
        """Reset pacing, cooldown and health (for tests)."""
        global _SHARED
        _SHARED = _Shared()

    def new_budget(self) -> HotelsBudget:
        limit_str = os.environ.get(ENV_HOTELS_BUDGET, "").strip()
        try:
            limit = int(limit_str) if limit_str else DEFAULT_BUDGET_LIMIT
        except ValueError:
            limit = DEFAULT_BUDGET_LIMIT
        return HotelsBudget(limit=limit)

    def health_status(self) -> Dict[str, Any]:
        """Return health status without making requests."""
        if not self.enabled:
            return {
                "source": SOURCE,
                "status": "disabled",
                "healthy": True,
                "enabled": False,
                "cached_searches": len(self._mem_cache),
            }
        remaining = _blocked_remaining()
        with _SHARED.lock:
            status = "blocked" if remaining > 0 else (_SHARED.last_status or "unknown")
            failures = _SHARED.consecutive_failures
            return {
                "source": SOURCE,
                "status": status,
                "healthy": remaining <= 0 and failures < FAILURE_THRESHOLD,
                "enabled": True,
                "last_ok_at": _SHARED.last_ok_at,
                "last_error": _SHARED.last_error,
                "last_error_at": _SHARED.last_error_at,
                "consecutive_failures": failures,
                "requests": _SHARED.requests,
                "cached_searches": len(self._mem_cache),
                "blocked_cooldown_remaining_secs": round(remaining, 1),
                "min_interval_secs": self.min_interval_secs,
            }

    def cheapest_night(
        self,
        city: str,
        date: str,
        budget: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return cheapest hotel for 1 adult for 1 night as {"price_try": float, "hotel": str, "source": "google_hotels"}."""
        try:
            return self._cheapest_night(city, date, budget)
        except _BudgetExhausted:
            return None
        except Exception as exc:
            logger.exception("GoogleHotelsProvider error for %s on %s", city, date)
            _record_failure("error", f"unexpected error: {exc}")
            return None

    def _cheapest_night(
        self,
        city: str,
        date: str,
        budget: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        clean_city = str(city or "").strip()
        if not clean_city:
            return None
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
        checkout_dt = dt + timedelta(days=1)
        checkout_str = checkout_dt.strftime("%Y-%m-%d")

        key = (clean_city.casefold(), date)

        # 1. Check Cache (memory and SQLite)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        # 2. Check Opt-In
        if not self.enabled:
            return None

        # 3. Check Cooldown
        if _blocked_remaining() > 0:
            return None

        # 4. Check Budget
        if budget is not None and not budget.take():
            raise _BudgetExhausted()

        from travel_engine.google_budget import get_google_budget, get_current_priority
        if not get_google_budget().take(get_current_priority()):
            _record_failure("budget_exhausted", "Google budget limit exceeded")
            return None

        # 5. Fetch Page
        url = (
            f"https://www.google.com/travel/hotels/{urllib.parse.quote(clean_city)}"
            f"?dates={date},{checkout_str}&hl=tr&gl=tr"
        )
        _reserve_page_slot(self.min_interval_secs)

        try:
            final_url, text = self._fetch_page(url, locale="tr-TR", wait_ms=self.wait_ms)
        except Exception as exc:
            _record_failure("error", f"page load failed: {exc}")
            return None

        if _is_block_page(final_url, text):
            _start_cooldown(self.blocked_cooldown_secs)
            _record_failure("blocked", "block page returned")
            return None

        cards = parse_hotel_cards(text)
        if not cards:
            _record_failure("parse_error", f"no hotel cards parsed for {clean_city}")
            return None

        cheapest = min(cards, key=lambda c: c["price_try"])
        result = {
            "price_try": float(cheapest["price_try"]),
            "hotel": str(cheapest["hotel"]),
            "source": SOURCE,
        }
        _record_ok()
        self._cache_put(key, result)
        return result

    # -- Cache Helpers -------------------------------------------------------

    def _ensure_db(self) -> None:
        if self._db_ready or not self.db_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)

        def init_schema():
            conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000)
            try:
                conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                except sqlite3.OperationalError:
                    pass
                with conn:
                    for stmt in _SCHEMA:
                        conn.execute(stmt)
            finally:
                conn.close()

        retry_on_busy(init_schema, "hotel cache schema")
        self._db_ready = True

    def _cache_get(self, key: Tuple[str, str]) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._cache_lock:
            mem = self._mem_cache.get(key)
            if mem is not None:
                exp, val = mem
                if exp > now:
                    return dict(val)
                self._mem_cache.pop(key, None)

        if not self.db_path:
            return None

        self._ensure_db()
        try:
            conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000)
            try:
                cur = conn.execute(
                    "SELECT hotel, price_try, source, expires_at FROM hotel_cache WHERE city = ? AND checkin = ?",
                    (key[0], key[1]),
                )
                row = cur.fetchone()
                if row:
                    hotel, price, source, exp = row
                    if exp > now:
                        res = {"hotel": hotel, "price_try": float(price), "source": source}
                        with self._cache_lock:
                            self._mem_cache[key] = (exp, res)
                        return res
                    # Expired row
                    with conn:
                        conn.execute("DELETE FROM hotel_cache WHERE city = ? AND checkin = ?", (key[0], key[1]))
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("hotel cache get error: %s", exc)
        return None

    def _cache_put(self, key: Tuple[str, str], val: Dict[str, Any]) -> None:
        now = time.time()
        exp = now + self.cache_ttl_secs
        with self._cache_lock:
            self._mem_cache[key] = (exp, val)

        if not self.db_path:
            return

        self._ensure_db()
        try:
            conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000)
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO hotel_cache
                        (city, checkin, hotel, price_try, source, created_at, expires_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (key[0], key[1], val["hotel"], val["price_try"], val["source"], now, exp),
                    )
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("hotel cache put error: %s", exc)
