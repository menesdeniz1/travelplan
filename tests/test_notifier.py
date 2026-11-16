import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

from travel_engine import notifier as notifier_module
from travel_engine.notifier import AlertNotifier


def _ok_response():
    """A urlopen() return value whose context-managed response reports HTTP 200."""
    cm = MagicMock()
    cm.__enter__.return_value.status = 200
    return cm


def _sent_payload(mock_urlopen, call_index=-1):
    """Decode the JSON body of the Request passed to urlopen."""
    req = mock_urlopen.call_args_list[call_index][0][0]
    return json.loads(req.data.decode("utf-8"))


class NotifierTestBase(unittest.TestCase):
    def setUp(self):
        # Never let real environment Telegram settings leak into tests.
        env_patch = patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
        env_patch.start()
        self.addCleanup(env_patch.stop)

        self.tmp_dir = tempfile.mkdtemp(prefix="notifier_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self.alerts_path = os.path.join(self.tmp_dir, "alerts.json")
        self.config_path = os.path.join(self.tmp_dir, "config.json")

    def make_notifier(self, telegram=False):
        n = AlertNotifier(alerts_file=self.alerts_path, config_file=self.config_path)
        if telegram:
            n.save_config("123456:TEST-TOKEN", "987654", True)
        return n

    def read_alerts_file(self):
        """Alerts as a fresh notifier sees them (newest first), i.e. as stored in SQLite."""
        return AlertNotifier(alerts_file=self.alerts_path, config_file=self.config_path).get_alerts()


class TestTelegramHtmlFormatting(NotifierTestBase):
    @patch("urllib.request.urlopen")
    def test_send_alert_uses_html_and_escapes_dynamic_values(self, mock_urlopen):
        mock_urlopen.return_value = _ok_response()
        n = self.make_notifier(telegram=True)

        record = n.send_alert(
            origin="Istanbul",
            destination="Paris",
            current_price=1000,
            target_budget=1500,
            airline="Air_Europa *Promo* <x>",
            duration="3s 10d",
            google_url="https://www.google.com/travel/flights?q=a&b=c",
            trip_type="Tek Yön",
            departure_date="2026-11-01",
            times="08:30 – 10:50",
        )

        self.assertTrue(record["telegram_sent"])
        payload = _sent_payload(mock_urlopen)
        self.assertEqual(payload["parse_mode"], "HTML")
        text = payload["text"]
        self.assertIn("Air_Europa *Promo* &lt;x&gt;", text)
        self.assertNotIn("<x>", text)
        # href is attribute-escaped
        self.assertIn('<a href="https://www.google.com/travel/flights?q=a&amp;b=c">', text)

    @patch("urllib.request.urlopen")
    def test_send_alert_without_google_url_omits_link(self, mock_urlopen):
        mock_urlopen.return_value = _ok_response()
        n = self.make_notifier(telegram=True)

        n.send_alert("IST", "CDG", 900, 1000, "Pegasus", "2s", google_url="", trip_type="Tek Yön")

        text = _sent_payload(mock_urlopen)["text"]
        self.assertNotIn("<a ", text)

    @patch("urllib.request.urlopen")
    def test_send_multimodal_alert_stores_type_and_escapes_segments(self, mock_urlopen):
        mock_urlopen.return_value = _ok_response()
        n = self.make_notifier(telegram=True)

        record = n.send_multimodal_alert(
            origin="Istanbul",
            destination="London",
            multimodal_price=2500,
            target_budget=3000,
            savings_vs_direct=400,
            journey_duration_str="12s 30d",
            segments_summary="✈️ IST ➔ BUD <br> `Tren_Hat` *x*",
            departure_date="2026-12-01",
            route_id="r1",
        )

        self.assertEqual(record["type"], "multimodal")
        self.assertTrue(record["id"].startswith("alert_mm_"))
        self.assertTrue(record["telegram_sent"])

        payload = _sent_payload(mock_urlopen)
        self.assertEqual(payload["parse_mode"], "HTML")
        text = payload["text"]
        self.assertIn("✈️ IST ➔ BUD &lt;br&gt; `Tren_Hat` *x*", text)
        self.assertNotIn("<br>", text)

        stored = self.read_alerts_file()
        self.assertEqual(stored[0]["type"], "multimodal")
        self.assertEqual(stored[0]["id"], record["id"])

    @patch("urllib.request.urlopen")
    def test_test_telegram_connection_sends_html(self, mock_urlopen):
        mock_urlopen.return_value = _ok_response()
        n = self.make_notifier(telegram=True)

        result = n.test_telegram_connection()

        self.assertTrue(result["success"])
        self.assertEqual(_sent_payload(mock_urlopen)["parse_mode"], "HTML")


class TestAlertIdsAndStorage(NotifierTestBase):
    def test_two_alerts_in_quick_succession_get_different_ids(self):
        n = self.make_notifier(telegram=False)

        first = n.send_alert("IST", "CDG", 900, 1000, "Pegasus", "2s")
        second = n.send_alert("IST", "CDG", 900, 1000, "Pegasus", "2s")

        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(first["id"].startswith("alert_"))
        self.assertEqual(len(self.read_alerts_file()), 2)

    def test_concurrent_saves_are_not_lost(self):
        n = self.make_notifier(telegram=False)

        def worker():
            for _ in range(5):
                n.send_alert("IST", "CDG", 900, 1000, "Pegasus", "2s")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(n.get_alerts()), 40)

    def test_clear_alerts_empties_file(self):
        n = self.make_notifier(telegram=False)
        n.send_alert("IST", "CDG", 900, 1000, "Pegasus", "2s")

        n.clear_alerts()

        self.assertEqual(n.get_alerts(), [])
        self.assertEqual(self.read_alerts_file(), [])

    def test_no_temp_files_left_behind(self):
        # The session database (conftest.py) is replaced by one in this test's temp dir
        with patch.dict(os.environ, {"TRAVELPLAN_DB": os.path.join(self.tmp_dir, "travelplan.db")}):
            n = self.make_notifier(telegram=False)
            n.send_alert("IST", "CDG", 900, 1000, "Pegasus", "2s")
            n.save_config("tok", "chat", True)

        leftovers = [f for f in os.listdir(self.tmp_dir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        # Alerts live in the SQLite file next to the alerts path; the JSON file is never written.
        self.assertFalse(os.path.exists(self.alerts_path))
        self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "travelplan.db")))
        self.assertIn("config.json", os.listdir(self.tmp_dir))

    def test_save_config_round_trips_through_injected_config_file(self):
        n = self.make_notifier(telegram=True)

        reloaded = AlertNotifier(alerts_file=self.alerts_path, config_file=self.config_path)

        self.assertEqual(reloaded.config["telegram_bot_token"], "123456:TEST-TOKEN")
        self.assertEqual(reloaded.config["telegram_chat_id"], "987654")

    def test_default_paths_come_from_module_constants(self):
        alerts = os.path.join(self.tmp_dir, "default_alerts.json")
        config = os.path.join(self.tmp_dir, "default_config.json")
        with patch.object(notifier_module, "ALERTS_FILE", alerts), \
                patch.object(notifier_module, "CONFIG_FILE", config):
            n = AlertNotifier()

        self.assertEqual(n.alerts_file, alerts)
        self.assertEqual(n.config_file, config)


class TestTelegramFailures(NotifierTestBase):
    @patch("urllib.request.urlopen")
    def test_telegram_http_error_keeps_alert_saved(self, mock_urlopen):
        body = b'{"ok":false,"error_code":400,"description":"Bad Request: can\'t parse entities"}'
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.telegram.org/bot/sendMessage", 400, "Bad Request", {}, io.BytesIO(body)
        )
        n = self.make_notifier(telegram=True)

        record = n.send_alert("IST", "CDG", 900, 1000, "Air_Europa", "2s")

        self.assertFalse(record["telegram_sent"])
        stored = self.read_alerts_file()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], record["id"])

    @patch("urllib.request.urlopen")
    def test_telegram_connection_reports_response_body_on_error(self, mock_urlopen):
        body = b'{"ok":false,"description":"Bad Request: chat not found"}'
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.telegram.org/bot/sendMessage", 400, "Bad Request", {}, io.BytesIO(body)
        )
        n = self.make_notifier(telegram=True)

        result = n.test_telegram_connection()

        self.assertFalse(result["success"])
        self.assertIn("chat not found", result["message"])

    @patch("urllib.request.urlopen")
    def test_multimodal_telegram_failure_keeps_alert_saved(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("network down")
        n = self.make_notifier(telegram=True)

        record = n.send_multimodal_alert(
            "IST", "London", 2500, 3000, 0, "12s", "segment", "2026-12-01", "r1"
        )

        self.assertFalse(record["telegram_sent"])
        self.assertEqual(self.read_alerts_file()[0]["id"], record["id"])

    @patch("urllib.request.urlopen")
    def test_no_telegram_call_when_not_configured(self, mock_urlopen):
        n = self.make_notifier(telegram=False)

        record = n.send_alert("IST", "CDG", 900, 1000, "Pegasus", "2s")

        self.assertFalse(record["telegram_sent"])
        mock_urlopen.assert_not_called()


class TestSystemMessages(NotifierTestBase):
    @patch("urllib.request.urlopen")
    def test_system_message_is_escaped_for_telegram_and_stored_as_plain_text(self, mock_urlopen):
        mock_urlopen.return_value = _ok_response()
        n = self.make_notifier(telegram=True)
        text = "⚠️ Google Flights okunamıyor: <b>son 3</b> & arama başarısız (parse_error)."

        sent = n.send_system_message(text)

        self.assertTrue(sent)
        payload = _sent_payload(mock_urlopen)
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(
            payload["text"],
            "⚠️ Google Flights okunamıyor: &lt;b&gt;son 3&lt;/b&gt; &amp; arama başarısız (parse_error).",
        )
        stored = self.read_alerts_file()
        self.assertEqual(stored[0]["type"], "system")
        self.assertEqual(stored[0]["message"], text)
        self.assertFalse(stored[0]["read"])

    @patch("urllib.request.urlopen")
    def test_system_message_is_saved_but_not_sent_when_telegram_unconfigured(self, mock_urlopen):
        n = self.make_notifier(telegram=False)

        sent = n.send_system_message("✅ Google Flights araması tekrar çalışıyor.")

        self.assertFalse(sent)
        mock_urlopen.assert_not_called()
        stored = self.read_alerts_file()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["type"], "system")
        self.assertTrue(stored[0]["id"].startswith("alert_sys_"))

    @patch("urllib.request.urlopen")
    def test_system_message_not_sent_when_notifications_disabled(self, mock_urlopen):
        n = self.make_notifier()
        n.save_config("123456:TEST-TOKEN", "987654", False)

        self.assertFalse(n.send_system_message("test"))

        mock_urlopen.assert_not_called()
        self.assertEqual(self.read_alerts_file()[0]["type"], "system")

    @patch("urllib.request.urlopen")
    def test_empty_system_message_is_ignored(self, mock_urlopen):
        n = self.make_notifier(telegram=True)

        self.assertFalse(n.send_system_message("   "))

        mock_urlopen.assert_not_called()
        self.assertFalse(os.path.exists(self.alerts_path))

    @patch("urllib.request.urlopen")
    def test_telegram_failure_keeps_system_alert(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("offline")
        n = self.make_notifier(telegram=True)

        self.assertFalse(n.send_system_message("⚠️ test"))

        self.assertEqual(self.read_alerts_file()[0]["type"], "system")


if __name__ == "__main__":
    unittest.main()
