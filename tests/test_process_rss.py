"""OPS-3: process_rss_mb on non-Windows platforms (macOS / Linux)."""
import subprocess
import unittest
from unittest import mock

from travel_engine.providers.google_flights_provider import process_rss_mb


class ProcessRssTests(unittest.TestCase):
    def test_posix_rss_parses_kb_to_mb(self):
        # (kırmızı) ps çıktısı "  204800\n" → 200.0
        fake_res = mock.MagicMock()
        fake_res.stdout = "  204800\n"
        fake_res.returncode = 0

        with mock.patch("os.name", "posix"):
            with mock.patch("subprocess.run", return_value=fake_res) as mock_run:
                val = process_rss_mb()
                self.assertEqual(val, 200.0)
                mock_run.assert_called_once()

    def test_posix_rss_error_or_empty_returns_none(self):
        # (kırmızı) ps hata verirse / boş çıktı → None
        with mock.patch("os.name", "posix"):
            # Empty stdout
            fake_empty = mock.MagicMock(stdout="", returncode=0)
            with mock.patch("subprocess.run", return_value=fake_empty):
                self.assertIsNone(process_rss_mb())

            # Whitespace only
            fake_ws = mock.MagicMock(stdout="   \n", returncode=0)
            with mock.patch("subprocess.run", return_value=fake_ws):
                self.assertIsNone(process_rss_mb())

            # SubprocessError (e.g. TimeoutExpired)
            with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ps", timeout=2)):
                self.assertIsNone(process_rss_mb())

            # OSError (e.g. FileNotFoundError)
            with mock.patch("subprocess.run", side_effect=OSError("command not found")):
                self.assertIsNone(process_rss_mb())

            # ValueError (invalid float)
            fake_bad = mock.MagicMock(stdout="not_a_number\n", returncode=0)
            with mock.patch("subprocess.run", return_value=fake_bad):
                self.assertIsNone(process_rss_mb())


if __name__ == "__main__":
    unittest.main()
