"""TRAVELPLAN_SCAN_INTERVAL: the watch scan interval read by the server at start (B11)."""
import os
import unittest
from unittest import mock

import server_v2


class ScanIntervalFromEnvTests(unittest.TestCase):
    def interval(self, value=None):
        env = dict(os.environ)
        env.pop("TRAVELPLAN_SCAN_INTERVAL", None)
        if value is not None:
            env["TRAVELPLAN_SCAN_INTERVAL"] = value
        with mock.patch.dict(os.environ, env, clear=True):
            return server_v2._scan_interval_from_env()

    def test_unset_is_six_hours(self):
        self.assertEqual(self.interval(), 21600.0)

    def test_valid_value_is_used(self):
        self.assertEqual(self.interval("3600"), 3600.0)

    def test_invalid_or_empty_falls_back_to_six_hours(self):
        self.assertEqual(self.interval("abc"), 21600.0)
        self.assertEqual(self.interval(""), 21600.0)
        self.assertEqual(self.interval("nan"), 21600.0)

    def test_too_short_is_raised_to_fifteen_minutes(self):
        self.assertEqual(self.interval("60"), 900.0)


if __name__ == "__main__":
    unittest.main()
