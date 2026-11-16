import unittest
import tempfile
from unittest import mock
from travel_engine.daemon_tracker import check_flight_time_match
from travel_engine.tracker import PriceTracker
from travel_engine.models import TripWatch
from travel_engine.storage import DB_ENV_VAR
import os


class TestTimeSlots(unittest.TestCase):
    def test_standard_slots(self):
        # Morning: 06:00 - 12:00
        self.assertTrue(check_flight_time_match("08:30 – 10:50", time_slot="morning"))
        self.assertFalse(check_flight_time_match("13:45 – 19:30", time_slot="morning"))
        self.assertFalse(check_flight_time_match("02:15 – 06:00", time_slot="morning"))

        # Afternoon: 12:00 - 18:00
        self.assertTrue(check_flight_time_match("13:45 – 19:30", time_slot="afternoon"))
        self.assertFalse(check_flight_time_match("08:30 – 10:50", time_slot="afternoon"))
        self.assertFalse(check_flight_time_match("19:50 – 23:45", time_slot="afternoon"))

        # Evening: 18:00 - 24:00 (18:00 - 00:00)
        self.assertTrue(check_flight_time_match("19:50 – 23:45", time_slot="evening"))
        self.assertTrue(check_flight_time_match("22:30 – 01:15", time_slot="evening"))
        self.assertFalse(check_flight_time_match("02:15 – 06:00", time_slot="evening"))
        self.assertFalse(check_flight_time_match("15:30 – 17:15", time_slot="evening"))

        # Night: 00:00 - 06:00
        self.assertTrue(check_flight_time_match("00:50 – 02:10", time_slot="night"))
        self.assertTrue(check_flight_time_match("04:30 – 07:00", time_slot="night"))
        self.assertFalse(check_flight_time_match("08:30 – 10:50", time_slot="night"))
        self.assertFalse(check_flight_time_match("21:00 – 23:30", time_slot="night"))

    def test_custom_range_midnight_to_eleven(self):
        # User specified: 00:00 - 11:00 (Gece 00'dan sabah 11'e kadar)
        # Flights in range:
        self.assertTrue(check_flight_time_match("00:50 – 02:10", time_slot="custom", window_start="00:00", window_end="11:00"))
        self.assertTrue(check_flight_time_match("08:30 – 10:50", time_slot="custom", window_start="00:00", window_end="11:00"))
        self.assertTrue(check_flight_time_match("10:50 – 12:45", time_slot="custom", window_start="00:00", window_end="11:00"))

        # Flights out of range:
        self.assertFalse(check_flight_time_match("12:30 – 15:00", time_slot="custom", window_start="00:00", window_end="11:00"))
        self.assertFalse(check_flight_time_match("15:30 – 17:15", time_slot="custom", window_start="00:00", window_end="11:00"))
        self.assertFalse(check_flight_time_match("20:45 – 22:35", time_slot="custom", window_start="00:00", window_end="11:00"))

    def test_space_and_midnight_to_24_formatting(self):
        # User types "00 00" to "24 00" or "00 01" to "24:00"
        # Matches any flight in the 24-hour day
        self.assertTrue(check_flight_time_match("00:50 – 02:10", time_slot="custom", window_start="00 00", window_end="24 00"))
        self.assertTrue(check_flight_time_match("13:45 – 19:30", time_slot="custom", window_start="00 00", window_end="24 00"))
        self.assertTrue(check_flight_time_match("23:45 – 01:20", time_slot="custom", window_start="00 00", window_end="24 00"))

        # User types "00 01" to "11 00"
        self.assertTrue(check_flight_time_match("00:50 – 02:10", time_slot="custom", window_start="00 01", window_end="11 00"))
        self.assertTrue(check_flight_time_match("08:30 – 10:50", time_slot="custom", window_start="00 01", window_end="11 00"))
        self.assertFalse(check_flight_time_match("15:30 – 17:15", time_slot="custom", window_start="00 01", window_end="11 00"))

    def test_watch_persistence_with_custom_range(self):
        # Own temp directory and database; never the repo root or the session database.
        with tempfile.TemporaryDirectory(prefix="travelplan_time_slots_") as tmp:
            test_file = os.path.join(tmp, "test_time_watches.json")
            with mock.patch.dict(os.environ, {DB_ENV_VAR: os.path.join(tmp, "travelplan.db")}):
                tracker = PriceTracker(storage_path=test_file)
                w = tracker.add_watch(
                    origin="Atina",
                    destination="Prag",
                    target_budget_try=12000.0,
                    departure_date="2026-10-27",
                    departure_time_slot="custom",
                    time_window_start="00:00",
                    time_window_end="11:00"
                )
                self.assertEqual(w.departure_time_slot, "custom")
                self.assertEqual(w.time_window_start, "00:00")
                self.assertEqual(w.time_window_end, "11:00")

                # Reload from storage
                tracker2 = PriceTracker(storage_path=test_file)
                w2 = tracker2.watches[w.id]
                self.assertEqual(w2.departure_time_slot, "custom")
                self.assertEqual(w2.time_window_start, "00:00")
                self.assertEqual(w2.time_window_end, "11:00")


if __name__ == "__main__":
    unittest.main()
