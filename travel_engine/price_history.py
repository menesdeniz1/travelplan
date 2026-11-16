"""Price History Tracker and Flexible Date Matrix Manager."""

import json
import logging
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from travel_engine.providers.google_flights_provider import CITY_MAP, normalize_turkish
from travel_engine.storage import get_store, import_legacy_json, resolve_db_path

logger = logging.getLogger(__name__)

# Recorded points kept per route (oldest are dropped).
POINTS_PER_ROUTE = 50

TURKISH_MONTHS = ["", "Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]
TURKISH_DAYS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]

# Weekday multiplier: Tuesday/Wednesday are cheaper, Friday/Sunday are peak
WEEKDAY_MULTIPLIER = {
    0: 0.97,  # Pazartesi
    1: 0.94,  # Salı (en ucuz günlerden biri)
    2: 0.95,  # Çarşamba
    3: 0.98,  # Perşembe
    4: 1.05,  # Cuma (haftasonu başlangıcı)
    5: 1.02,  # Cumartesi
    6: 1.04,  # Pazar (dönüş/yoğunluk)
}


def _validate_legacy_history(data: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Check a legacy price_history.json payload: {route key: [point, ...]}."""
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object at top level, got {type(data).__name__}")
    history: Dict[str, List[Dict[str, Any]]] = {}
    for key, points in data.items():
        if not isinstance(points, list):
            raise ValueError(f"route {key!r} is not a list")
        history[key] = [p for p in points if isinstance(p, dict)]
    return history


def _insert_history_rows(conn: sqlite3.Connection, history: Dict[str, List[Dict[str, Any]]]) -> int:
    count = 0
    for key, points in history.items():
        for entry in points:
            conn.execute(
                "INSERT INTO price_history (key, ts, price, airline, data) VALUES (?, ?, ?, ?, ?)",
                (key, entry.get("timestamp"), entry.get("price"), entry.get("airline"),
                 json.dumps(entry, ensure_ascii=False)),
            )
            count += 1
    return count


class PriceHistoryManager:
    def __init__(self, storage_path: str = "price_history.json"):
        self.storage_path = storage_path
        self._store = get_store(resolve_db_path(storage_path))
        self.db_path = self._store.db_path
        import_legacy_json(
            self._store, "price_history", storage_path, "price_history",
            _validate_legacy_history, _insert_history_rows,
        )
        self.history: Dict[str, List[Dict[str, Any]]] = self._load()

    def _load(self) -> Dict[str, List[Dict[str, Any]]]:
        """Read every stored point into memory, oldest first per route."""
        history: Dict[str, List[Dict[str, Any]]] = {}
        try:
            rows = self._store.query("SELECT key, data FROM price_history ORDER BY rowid")
        except sqlite3.Error as e:
            logger.warning("Could not read price history from %s: %s", self.db_path, e)
            return history
        for key, data in rows:
            try:
                point = json.loads(data)
            except ValueError:
                continue
            history.setdefault(key, []).append(point)
        return history

    def _persist_point(self, key: str, entry: Dict[str, Any]) -> None:
        """Insert one point and keep only the newest POINTS_PER_ROUTE for its route."""
        try:
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT INTO price_history (key, ts, price, airline, data) VALUES (?, ?, ?, ?, ?)",
                    (key, entry["timestamp"], entry["price"], entry["airline"],
                     json.dumps(entry, ensure_ascii=False)),
                )
                conn.execute(
                    "DELETE FROM price_history WHERE key = ? AND rowid NOT IN ("
                    "  SELECT rowid FROM price_history WHERE key = ? ORDER BY rowid DESC LIMIT ?)",
                    (key, key, POINTS_PER_ROUTE),
                )
        except Exception:
            logger.exception("Failed to save price history to %s", self.db_path)

    def _normalize_name(self, text: str) -> str:
        if not text:
            return ""
        mapping = {
            'İ': 'i', 'I': 'i', 'ı': 'i', 'i': 'i',
            'Ğ': 'g', 'ğ': 'g',
            'Ü': 'u', 'ü': 'u',
            'Ş': 's', 'ş': 's',
            'Ö': 'o', 'ö': 'o',
            'Ç': 'c', 'ç': 'c',
        }
        res = "".join(mapping.get(c, c) for c in text)
        res = unicodedata.normalize('NFKD', res).encode('ascii', 'ignore').decode().strip().lower()
        return res

    def _get_aliases(self, place: str) -> set[str]:
        aliases = set()
        if not place:
            return aliases
        raw = place.strip().lower()
        aliases.add(raw)
        norm = self._normalize_name(place)
        if norm:
            aliases.add(norm)

        for k, v in CITY_MAP.items():
            q_norm = self._normalize_name(v["query"])
            iata = v["iata"].lower()
            if k in (raw, norm) or q_norm in (raw, norm) or iata in (raw, norm):
                aliases.add(k)
                aliases.add(q_norm)
                aliases.add(iata)
                aliases.add(v["query"].lower())
                aliases.add(v["query"])
        return aliases

    def _get_key(self, origin: str, destination: str, dep_date: str, trip_type: str) -> str:
        orig_clean = self._normalize_name(origin) or origin.strip().lower()
        dest_clean = self._normalize_name(destination) or destination.strip().lower()
        return f"{orig_clean}_{dest_clean}_{dep_date}_{trip_type}"

    def record_price(
        self,
        origin: str,
        destination: str,
        dep_date: str,
        trip_type: str,
        min_price: float,
        airline: str = "Havayolu",
    ):
        if not min_price or min_price <= 0:
            return

        key = self._get_key(origin, destination, dep_date, trip_type)
        if key not in self.history:
            self.history[key] = []

        now = datetime.now()
        entry = {
            "timestamp": now.isoformat(),
            "time_label": now.strftime("%H:%M"),
            "date_label": now.strftime("%d.%m"),
            "price": float(min_price),
            "airline": airline,
        }

        # Avoid spamming duplicates within 5 minutes if price hasn't changed
        points = self.history[key]
        if points:
            last = points[-1]
            if last["price"] == entry["price"] and last["time_label"] == entry["time_label"]:
                return

        points.append(entry)
        # Keep last 50 points per route
        if len(points) > POINTS_PER_ROUTE:
            self.history[key] = points[-POINTS_PER_ROUTE:]

        self._persist_point(key, entry)

    def find_recorded_price(
        self,
        origin: str,
        destination: str,
        date_str: str,
        trip_type: str,
        cache_lookup: Optional[Dict[str, Any]] = None,
    ) -> Optional[tuple[float, str]]:
        orig_a = self._get_aliases(origin)
        dest_a = self._get_aliases(destination)

        # 1. Check in-memory cache_lookup (from GoogleFlightsProvider)
        if cache_lookup:
            for oa in orig_a:
                for da in dest_a:
                    for tt in [trip_type, "oneway", "roundtrip"]:
                        ckey = f"{oa}_{da}_{date_str}_{tt}"
                        if ckey in cache_lookup:
                            cres = cache_lookup[ckey].get("results", [])
                            if cres and cres[0].get("total_price_try"):
                                return float(cres[0]["total_price_try"]), cres[0].get("airline", "Havayolu")

        # 2. Check self.history (persisted in the price_history table)
        for oa in orig_a:
            for da in dest_a:
                for tt in [trip_type, "oneway", "roundtrip"]:
                    ckey = f"{oa}_{da}_{date_str}_{tt}"
                    if ckey in self.history and self.history[ckey]:
                        pts = self.history[ckey]
                        return float(pts[-1]["price"]), pts[-1].get("airline", "Havayolu")

        # 3. Fallback fuzzy scan over all history keys
        for k, pts in self.history.items():
            if date_str in k and pts:
                k_lower = k.lower()
                has_orig = any(oa.lower() in k_lower for oa in orig_a)
                has_dest = any(da.lower() in k_lower for da in dest_a)
                if has_orig and has_dest:
                    return float(pts[-1]["price"]), pts[-1].get("airline", "Havayolu")

        return None

    def get_history(
        self,
        origin: str,
        destination: str,
        dep_date: str,
        trip_type: str,
        current_price: Optional[float] = None,
        current_airline: Optional[str] = None,
        days_range: int = 1,
    ) -> List[Dict[str, Any]]:
        orig_a = self._get_aliases(origin)
        dest_a = self._get_aliases(destination)
        points = []
        for oa in orig_a:
            for da in dest_a:
                for tt in [trip_type, "oneway", "roundtrip"]:
                    k = f"{oa}_{da}_{dep_date}_{tt}"
                    if k in self.history and self.history[k]:
                        points = list(self.history[k])
                        break
                if points:
                    break
            if points:
                break

        now = datetime.now()
        base = current_price if (current_price and current_price > 0) else (points[-1]["price"] if points else 10000.0)
        airline = current_airline or (points[-1]["airline"] if points else "Havayolu")
        has_now_price = bool(current_price and current_price > 0)

        # Multi-day timeline (7, 14, 30 days)
        if days_range > 1:
            # Days without a real recording are ESTIMATES (weekday multiplier + drift). They exist
            # only so the chart gets one point per day; they are flagged is_estimated=True /
            # is_live=False so the UI never presents them as observed prices.
            history_pts = []
            for i in range(days_range - 1, -1, -1):
                day_dt = now - timedelta(days=i)
                date_str = day_dt.strftime("%Y-%m-%d")
                d_label = f"{day_dt.day} {TURKISH_MONTHS[day_dt.month]}"
                
                day_matches = [p for p in points if p.get("timestamp", "").startswith(date_str)]
                if day_matches:
                    history_pts.append({
                        "timestamp": day_matches[-1]["timestamp"],
                        "time_label": day_matches[-1].get("time_label", "12:00"),
                        "date_label": d_label,
                        "price": day_matches[-1]["price"],
                        "airline": day_matches[-1].get("airline", airline),
                        "is_live": True,
                        "is_estimated": False,
                    })
                elif i == 0 and has_now_price:
                    # Today without a stored recording: the live price we were just given
                    history_pts.append({
                        "timestamp": now.isoformat(),
                        "time_label": now.strftime("%H:%M"),
                        "date_label": d_label,
                        "price": float(current_price),
                        "airline": airline,
                        "is_live": True,
                        "is_estimated": False,
                    })
                else:
                    w_mult = WEEKDAY_MULTIPLIER.get(day_dt.weekday(), 1.0)
                    now_w_mult = WEEKDAY_MULTIPLIER.get(now.weekday(), 1.0)
                    trend_drift = 1.0 + ((i % 5) - 2) * 0.016
                    calc_price = round(base * (w_mult / now_w_mult) * trend_drift, 0)
                    if i == 0:
                        calc_price = base
                    history_pts.append({
                        "timestamp": day_dt.isoformat(),
                        "time_label": "12:00" if i > 0 else now.strftime("%H:%M"),
                        "date_label": d_label,
                        "price": float(calc_price),
                        "airline": airline,
                        "is_live": False,
                        "is_estimated": True,
                    })
            return history_pts

        # Intraday (1 day). Only real observations are returned: recorded scans, plus the
        # current search price as a single "now" point on cold start. No synthetic intraday curve.
        real_points = [{**p, "is_live": True, "is_estimated": False} for p in points]
        if len(points) >= 2 or not has_now_price:
            return real_points

        # Cold start: fewer than 2 recordings, but we have the current search price
        real_points.append({
            "timestamp": now.isoformat(),
            "time_label": now.strftime("%H:%M"),
            "date_label": now.strftime("%d.%m"),
            "price": float(current_price),
            "airline": airline,
            "is_live": True,
            "is_estimated": False,
        })
        return real_points

    def get_date_matrix(
        self,
        origin: str,
        destination: str,
        dep_date: str,
        trip_type: str,
        current_min_price: float,
        cache_lookup: Optional[Dict[str, Any]] = None,
        calendar_lookup: Optional[Dict[str, float]] = None,
        current_airline: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            center_dt = datetime.strptime(dep_date, "%Y-%m-%d")
        except Exception:
            center_dt = datetime.now()

        days_matrix = []
        for offset in range(-3, 4):
            day_dt = center_dt + timedelta(days=offset)
            date_str = day_dt.strftime("%Y-%m-%d")
            day_name = TURKISH_DAYS[day_dt.weekday()]
            month_name = TURKISH_MONTHS[day_dt.month]
            display_label = f"{day_dt.day} {month_name} {day_name}"

            is_selected = (offset == 0)

            if is_selected:
                price = current_min_price
                has_price = (current_min_price is not None and current_min_price > 0)
                is_live = True
                day_airline = current_airline or "Havayolu"
            elif calendar_lookup and date_str in calendar_lookup:
                price = float(calendar_lookup[date_str])
                has_price = True
                is_live = True
                day_airline = "Google Flights"
            else:
                recorded = self.find_recorded_price(origin, destination, date_str, trip_type, cache_lookup)
                if recorded:
                    price = float(recorded[0])
                    has_price = True
                    is_live = True
                    day_airline = recorded[1]
                else:
                    # No synthetic guessing / fake estimating: report exact availability
                    price = None
                    has_price = False
                    is_live = False
                    day_airline = None

            days_matrix.append({
                "offset": offset,
                "date": date_str,
                "display_label": display_label,
                "day_name": day_name,
                "price": price,
                "has_price": has_price,
                "is_selected": is_selected,
                "is_live": is_live,
                "is_estimated": False,
                "airline": day_airline,
            })

        valid_days = [d for d in days_matrix if d["has_price"] and d["price"] is not None]
        if valid_days:
            min_val = min(d["price"] for d in valid_days)
            for d in days_matrix:
                if d["has_price"] and d["price"] is not None:
                    d["is_cheapest"] = (d["price"] == min_val)
                    d["saving_vs_selected"] = max(0.0, (current_min_price or 0.0) - d["price"])
                else:
                    d["is_cheapest"] = False
                    d["saving_vs_selected"] = 0.0

            best_day = next((d for d in valid_days if d["is_cheapest"]), valid_days[0])
            best_date = best_day["date"]
            best_price = best_day["price"]
            best_display = best_day["display_label"]
            best_is_live = best_day.get("is_live", False)
            max_saving = round(max(0.0, (current_min_price or 0.0) - best_day["price"]), 0)
        else:
            best_date = dep_date
            best_price = current_min_price
            best_display = None
            best_is_live = False
            max_saving = 0.0

        return {
            "selected_date": dep_date,
            "selected_price": current_min_price,
            "best_date": best_date,
            "best_price": best_price,
            "best_display": best_display,
            "best_is_live": best_is_live,
            "max_saving_try": max_saving,
            "days": days_matrix,
        }
