"""Notification system for TravelPlan price drop alerts.

Supports:
1. Telegram Bot API (direct push notification to mobile phone)
2. In-App Web Alerts (saved to the SQLite alerts table for display in Web UI)
3. Console & System Logging
"""

import html
import json
import os
import sqlite3
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

from .storage import get_store, import_legacy_json, resolve_db_path

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Legacy alerts file. Alerts now live in the SQLite database next to it; the file is
# imported once and then renamed to alerts.json.migrated.bak.
ALERTS_FILE = os.path.join(BASE_DIR, "alerts.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# Newest alerts kept for the web UI.
ALERT_LIMIT = 50


def _atomic_write_json(path: str, data: Any, indent: Optional[int] = None) -> None:
    """Write JSON to a temp file in the same directory, then atomically replace `path`."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _esc(value: Any) -> str:
    """Escape a dynamic value for Telegram HTML parse mode."""
    return html.escape(str(value))


def _new_alert_id(prefix: str) -> str:
    """Unique id: prefix + unix seconds + short random suffix (avoids same-second collisions)."""
    ts = int(datetime.now(timezone.utc).timestamp())
    return f"{prefix}{ts}_{uuid.uuid4().hex[:8]}"


def _validate_legacy_alerts(data: Any) -> List[Dict[str, Any]]:
    """Check a legacy alerts.json payload (newest first) and keep the newest ALERT_LIMIT."""
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON list at top level, got {type(data).__name__}")
    return [a for a in data if isinstance(a, dict)][:ALERT_LIMIT]


def _insert_alert_rows(conn: sqlite3.Connection, alerts: List[Dict[str, Any]]) -> int:
    # The list is newest first. Insert oldest first so rowid order matches arrival order.
    seen = set()
    for alert in reversed(alerts):
        aid = str(alert.get("id") or "")
        if not aid or aid in seen:
            aid = _new_alert_id("alert_legacy_")
            alert = {**alert, "id": aid}
        seen.add(aid)
        conn.execute(
            "INSERT OR REPLACE INTO alerts (id, ts, data) VALUES (?, ?, ?)",
            (aid, alert.get("timestamp"), json.dumps(alert, ensure_ascii=False)),
        )
    return len(alerts)


class AlertNotifier:
    def __init__(self, alerts_file: Optional[str] = None, config_file: Optional[str] = None):
        self.alerts_file = alerts_file or ALERTS_FILE
        self.config_file = config_file or CONFIG_FILE
        self._store = get_store(resolve_db_path(self.alerts_file))
        self.db_path = self._store.db_path
        import_legacy_json(
            self._store, "alerts", self.alerts_file, "alerts",
            _validate_legacy_alerts, _insert_alert_rows,
        )
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        cfg = {
            "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
            "notifications_enabled": True,
        }
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    cfg.update(saved)
            except Exception:
                pass
        return cfg

    def save_config(self, bot_token: str, chat_id: str, enabled: bool = True) -> None:
        self.config["telegram_bot_token"] = bot_token.strip()
        self.config["telegram_chat_id"] = chat_id.strip()
        self.config["notifications_enabled"] = enabled
        try:
            _atomic_write_json(self.config_file, self.config, indent=2)
        except Exception as e:
            print(f"[Notifier] Error saving config: {e}")

    def _telegram_enabled(self) -> bool:
        return bool(
            self.config.get("notifications_enabled")
            and self.config.get("telegram_bot_token")
            and self.config.get("telegram_chat_id")
        )

    def _post_telegram(self, text: str, disable_preview: bool = True) -> Tuple[bool, str]:
        """Send an HTML-formatted message to the configured chat.

        Returns (success, error_message). On HTTP errors Telegram's response body
        is included so the failure reason (e.g. a parse error) is visible.
        """
        token = str(self.config.get("telegram_bot_token", "")).strip()
        chat_id = self.config.get("telegram_chat_id", "")
        if not token or not chat_id:
            return False, "Bot Token veya Chat ID eksik."

        api_url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                api_url,
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True, ""
                error = f"Beklenmeyen HTTP durumu: {resp.status}"
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace").strip()
            except Exception:
                body = ""
            error = f"HTTP {e.code}: {body or e.reason}"
        except Exception as e:
            error = str(e)

        print(f"[Notifier] Telegram send failed: {error}")
        return False, error

    def send_alert(
        self,
        origin: str,
        destination: str,
        current_price: float,
        target_budget: float,
        airline: str,
        duration: str,
        google_url: str = "",
        trip_type: str = "Tek Yön",
        departure_date: str = "",
        times: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        """Trigger alert across all enabled notification channels."""
        saving = round(target_budget - current_price, 2)
        discount_pct = round((saving / target_budget) * 100, 1) if target_budget > 0 else 0
        now = datetime.now(timezone.utc)

        alert_record = {
            "id": _new_alert_id("alert_"),
            "timestamp": now.isoformat(),
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "times": times,
            "current_price": current_price,
            "target_budget": target_budget,
            "saving": saving,
            "discount_pct": discount_pct,
            "airline": airline,
            "duration": duration,
            "trip_type": trip_type,
            "google_url": google_url,
            "read": False,
            "note": note,
        }

        # 1. Save to in-app alerts storage
        self._save_to_alerts_file(alert_record)

        # 2. Send via Telegram Bot if configured
        telegram_status = False
        if self._telegram_enabled():
            telegram_status = self._send_telegram_message(alert_record)

        alert_record["telegram_sent"] = telegram_status
        return alert_record

    def send_multimodal_alert(
        self,
        origin: str,
        destination: str,
        multimodal_price: float,
        target_budget: float,
        savings_vs_direct: float,
        journey_duration_str: str,
        segments_summary: str,
        departure_date: str = "",
        route_id: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        """Trigger multi-modal hybrid route opportunity alert across all notification channels."""
        budget_saving = round(target_budget - multimodal_price, 2)
        alert_record = {
            "id": _new_alert_id("alert_mm_"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "multimodal",
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "route_id": route_id,
            "current_price": multimodal_price,
            "target_budget": target_budget,
            "saving": budget_saving,
            "savings_vs_direct": savings_vs_direct,
            "duration": journey_duration_str,
            "segments_summary": segments_summary,
            "read": False,
            "note": note,
        }

        self._save_to_alerts_file(alert_record)

        telegram_status = False
        if self._telegram_enabled():
            date_str = f" ({_esc(departure_date)})" if departure_date else ""
            direct_saving_str = (
                f"\n💡 <b>Doğrudan Uçuşa Göre Tasarruf:</b> <code>+{savings_vs_direct:,.0f} ₺</code>"
                if savings_vs_direct > 0 else ""
            )
            note_str = f"\n\nℹ️ <i>{_esc(note)}</i>" if note else ""

            text = (
                f"🚆 <b>AKILLI ÇOK MODLU ROTA FIRSATI!</b>\n\n"
                f"📍 <b>Rota:</b> {_esc(origin)} ➔ {_esc(destination)}{date_str}\n"
                f"⏱️ <b>Toplam Süre:</b> {_esc(journey_duration_str)}\n"
                f"🛣️ <b>Aşama Detayı:</b>\n{_esc(segments_summary)}\n\n"
                f"💰 <b>Toplam Fiyat:</b> <code>{multimodal_price:,.0f} ₺</code>\n"
                f"🎯 <b>Hedef Bütçeniz:</b> <code>{target_budget:,.0f} ₺</code>"
                f"{direct_saving_str}\n"
                f"🔥 <b>Bütçe Kazancı:</b> <code>+{budget_saving:,.0f} ₺</code>"
                f"{note_str}\n\n"
                f"✨ <i>Virtual Interlining ile çevre havalimanı ve tren birleştirildi.</i>"
            )

            ok, _ = self._post_telegram(text)
            telegram_status = ok

        alert_record["telegram_sent"] = telegram_status
        return alert_record

    def _send_telegram_message(self, alert: Dict[str, Any]) -> bool:
        date_str = f" ({_esc(alert['departure_date'])})" if alert.get("departure_date") else ""
        times_str = f"⏰ <b>Kalkış-Varış:</b> {_esc(alert['times'])}\n" if alert.get("times") else ""
        google_url = alert.get("google_url") or ""
        link_str = ""
        if google_url:
            href = html.escape(str(google_url), quote=True)
            link_str = f'\n\n🔗 <a href="{href}">Google Flights\'ta Bileti Gör</a>'

        note_str = f"\n\nℹ️ <i>{_esc(alert['note'])}</i>" if alert.get("note") else ""
        text = (
            f"🚨 <b>FIRSAT BİLETİ ALARMI!</b>\n\n"
            f"📍 <b>Rota:</b> {_esc(alert['origin'])} ➔ {_esc(alert['destination'])}{date_str}\n"
            f"✈️ <b>Havayolu:</b> {_esc(alert['airline'])}\n"
            f"{times_str}"
            f"⏱️ <b>Süre:</b> {_esc(alert['duration'])} ({_esc(alert['trip_type'])})\n\n"
            f"💰 <b>Yeni Fiyat:</b> <code>{alert['current_price']:,.0f} ₺</code>\n"
            f"🎯 <b>Hedef Bütçeniz:</b> <code>{alert['target_budget']:,.0f} ₺</code>\n"
            f"🔥 <b>Tasarruf:</b> <code>+{alert['saving']:,.0f} ₺</code> (%{alert['discount_pct']})"
            f"{note_str}"
            f"{link_str}"
        )

        ok, _ = self._post_telegram(text)
        if ok:
            print(f"[Notifier] Telegram message sent successfully to {self.config.get('telegram_chat_id')}!")
        return ok

    def test_telegram_connection(self) -> Dict[str, Any]:
        """Test sending a ping message to verify Telegram setup."""
        token = self.config.get("telegram_bot_token", "").strip()
        chat_id = self.config.get("telegram_chat_id", "").strip()

        if not token or not chat_id:
            return {"success": False, "message": "Bot Token veya Chat ID eksik."}

        text = "🔔 <b>TravelPlan Test Bildirimi:</b> Telegram bot entegrasyonu başarıyla bağlandı! Fırsat biletleri tespit edildiğinde buraya alarm mesajı gelecektir."
        ok, error = self._post_telegram(text)
        if ok:
            return {"success": True, "message": "Test mesajı başarıyla Telegram'a gönderildi!"}
        return {"success": False, "message": f"Telegram API hatası: {error}"}

    def send_system_message(self, text: str) -> bool:
        """Show a plain-text system notice (e.g. scraper health) in the web alerts and, if configured, on Telegram.

        `text` is plain text: it is HTML-escaped for Telegram and stored unescaped (the web UI escapes it).
        The alert is saved even when Telegram is off. Returns True only when Telegram delivered the message.
        """
        message = str(text or "").strip()
        if not message:
            return False

        # 1. Save to in-app alerts storage (type "system" so the UI can style it)
        self._save_to_alerts_file({
            "id": _new_alert_id("alert_sys_"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "system",
            "message": message,
            "read": False,
        })

        # 2. Send via Telegram Bot if configured
        if not self._telegram_enabled():
            return False
        ok, _ = self._post_telegram(_esc(message))
        return bool(ok)

    def _save_to_alerts_file(self, alert: Dict[str, Any]) -> None:
        """Store an alert as the newest one and keep only the newest ALERT_LIMIT."""
        try:
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO alerts (id, ts, data) VALUES (?, ?, ?)",
                    (str(alert.get("id") or _new_alert_id("alert_")),
                     alert.get("timestamp"),
                     json.dumps(alert, ensure_ascii=False)),
                )
                conn.execute(
                    "DELETE FROM alerts WHERE rowid NOT IN ("
                    "  SELECT rowid FROM alerts ORDER BY rowid DESC LIMIT ?)",
                    (ALERT_LIMIT,),
                )
        except Exception as e:
            print(f"[Notifier] Error writing alert to {self.db_path}: {e}")

    def get_alerts(self) -> List[Dict[str, Any]]:
        """Newest first, at most ALERT_LIMIT alerts."""
        try:
            rows = self._store.query(
                "SELECT data FROM alerts ORDER BY rowid DESC LIMIT ?", (ALERT_LIMIT,),
            )
        except sqlite3.Error as e:
            print(f"[Notifier] Error reading alerts from {self.db_path}: {e}")
            return []
        return [json.loads(data) for (data,) in rows]

    def clear_alerts(self) -> None:
        try:
            with self._store.transaction() as conn:
                conn.execute("DELETE FROM alerts")
        except Exception as e:
            print(f"[Notifier] Error clearing alerts in {self.db_path}: {e}")
