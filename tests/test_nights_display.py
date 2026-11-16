"""Tests for nights display across all results (T17 - SUN-11)."""

import unittest
from travel_engine.tour_explore import format_nights_line, nights
from travel_engine import telegram_bot as tb


class TestNightsDisplay(unittest.TestCase):
    def test_night_bus_option(self):
        """1. Gece otobüsü olan seçenek için 'Geceler: 🚌 yolda' satırı."""
        option = {
            "depart_at": "2026-10-25T21:00",
            "arrive_at": "2026-10-26T07:00",
            "legs": [
                {
                    "type": "bus",
                    "operator": "FlixBus",
                    "from": "Viyana",
                    "to": "Krakov",
                    "depart_at": "2026-10-25T21:00",
                    "arrive_at": "2026-10-26T07:00",
                }
            ],
        }
        line = format_nights_line(option)
        self.assertEqual(line, "Geceler: 🚌 yolda")

    def test_night_flight_option(self):
        """2. Gece uçuşu olan seçenek için 'Geceler: ✈️ yolda' satırı."""
        option = {
            "depart_at": "2026-10-25T23:45",
            "arrive_at": "2026-10-26T05:30",
            "legs": [
                {
                    "type": "flight",
                    "operator": "Pegasus",
                    "from": "SAW",
                    "to": "DXB",
                    "depart_at": "2026-10-25T23:45",
                    "arrive_at": "2026-10-26T05:30",
                }
            ],
        }
        line = format_nights_line(option)
        self.assertEqual(line, "Geceler: ✈️ yolda")

    def test_city_stay_option(self):
        """3. Şehirde kalış olan seçenek için 'Geceler: 🛏 <Şehir>' satırı."""
        option = {
            "depart_at": "2026-10-25T14:00",
            "arrive_at": "2026-10-26T18:00",
            "legs": [
                {
                    "type": "bus",
                    "operator": "RegioJet",
                    "from": "Viyana",
                    "to": "Bratislava",
                    "origin_station_name": "Wien Hbf",
                    "dest_station_name": "Bratislava",
                    "depart_at": "2026-10-25T14:00",
                    "arrive_at": "2026-10-25T15:00",
                },
                {
                    "type": "train",
                    "operator": "ZSSK",
                    "from": "Bratislava",
                    "to": "Kösice",
                    "origin_station_name": "Bratislava",
                    "dest_station_name": "Kosice",
                    "depart_at": "2026-10-26T08:00",
                    "arrive_at": "2026-10-26T13:00",
                },
            ],
        }
        line = format_nights_line(option)
        self.assertEqual(line, "Geceler: 🛏 Bratislava")

    def test_airport_stay_option(self):
        """Havalimanında geceleyen seçenek için 'Geceler: 🛏 <Şehir> (havalimanı)' satırı."""
        option = {
            "depart_at": "2026-10-25T18:00",
            "arrive_at": "2026-10-26T11:00",
            "legs": [
                {
                    "type": "flight",
                    "operator": "Aegean",
                    "from": "PRG",
                    "to": "ATH",
                    "depart_at": "2026-10-25T18:00",
                    "arrive_at": "2026-10-25T21:30",
                },
                {
                    "type": "flight",
                    "operator": "Olympic Air",
                    "from": "ATH",
                    "to": "AXD",
                    "depart_at": "2026-10-26T07:30",
                    "arrive_at": "2026-10-26T08:30",
                },
            ],
        }
        line = format_nights_line(option)
        self.assertEqual(line, "Geceler: 🛏 Atina (havalimanı)")

    def test_multi_night_chain(self):
        """Bratislava + gece otobüsü + Atina havalimanı gecesi zinciri."""
        option = {
            "depart_at": "2026-10-25T14:00",
            "arrive_at": "2026-10-28T12:00",
            "legs": [
                # Night 1 (25-26 Oct): stays in Bratislava
                {
                    "type": "train",
                    "operator": "RegioJet",
                    "from": "Wien",
                    "to": "Bratislava",
                    "dest_station_name": "Bratislava",
                    "depart_at": "2026-10-25T14:00",
                    "arrive_at": "2026-10-25T15:00",
                },
                # Night 2 (26-27 Oct): overnight bus to Athens
                {
                    "type": "bus",
                    "operator": "FlixBus",
                    "from": "Bratislava",
                    "to": "Atina",
                    "dest_station_name": "Athens",
                    "depart_at": "2026-10-26T20:00",
                    "arrive_at": "2026-10-27T10:00",
                },
                # Transfer to airport
                {
                    "type": "transfer",
                    "from": "Athens",
                    "to": "ATH",
                    "depart_at": "2026-10-27T18:00",
                    "arrive_at": "2026-10-27T19:00",
                },
                # Night 3 (27-28 Oct): stays at ATH airport
                {
                    "type": "flight",
                    "operator": "Olympic",
                    "from": "ATH",
                    "to": "IST",
                    "depart_at": "2026-10-28T08:00",
                    "arrive_at": "2026-10-28T09:30",
                },
            ],
        }
        line = format_nights_line(option)
        self.assertEqual(line, "Geceler: 🛏 Bratislava · 🚌 yolda · 🛏 Atina (havalimanı)")

    def test_single_day_option_has_no_nights_line(self):
        """4. Tek günlük seçenekte satır yok."""
        option = {
            "depart_at": "2026-10-25T08:00",
            "arrive_at": "2026-10-25T16:00",
            "legs": [
                {
                    "type": "train",
                    "from": "Wien",
                    "to": "Praha",
                    "depart_at": "2026-10-25T08:00",
                    "arrive_at": "2026-10-25T16:00",
                }
            ],
        }
        line = format_nights_line(option)
        self.assertEqual(line, "")

    def test_score_and_ranking_unchanged(self):
        """5. Skor ve sıralama aynı."""
        opt1 = {
            "score": 100.0,
            "true_total_try": 1500.0,
            "duration_mins": 300,
            "depart_at": "2026-10-25T08:00",
            "arrive_at": "2026-10-25T13:00",
            "legs": [{"type": "flight", "depart_at": "2026-10-25T08:00", "arrive_at": "2026-10-25T13:00"}],
        }
        opt2 = {
            "score": 120.0,
            "true_total_try": 1800.0,
            "duration_mins": 720,
            "depart_at": "2026-10-25T20:00",
            "arrive_at": "2026-10-26T08:00",
            "legs": [{"type": "bus", "depart_at": "2026-10-25T20:00", "arrive_at": "2026-10-26T08:00"}],
        }
        pool = [opt2, opt1]
        sorted_before = sorted(pool, key=lambda o: (o["score"], o["true_total_try"]))

        # Formatting nights does not mutate option or affect keys
        _ = format_nights_line(opt1)
        _ = format_nights_line(opt2)

        sorted_after = sorted(pool, key=lambda o: (o["score"], o["true_total_try"]))
        self.assertEqual(sorted_before, sorted_after)
        self.assertEqual(opt1["score"], 100.0)
        self.assertEqual(opt2["score"], 120.0)

    def test_telegram_option_block_includes_nights(self):
        """Telegram seçenek bloğunda gece satırı gösterimi."""
        option_single = {
            "kind": "direct",
            "true_total_try": 500.0,
            "duration_mins": 150,
            "depart_at": "2026-10-25T08:00",
            "arrive_at": "2026-10-25T10:30",
            "legs": [
                {
                    "type": "train",
                    "operator": "RegioJet",
                    "from": "Viyana",
                    "to": "Prag",
                    "depart_at": "2026-10-25T08:00",
                    "arrive_at": "2026-10-25T10:30",
                    "price_try": 500.0,
                }
            ],
        }
        text_single = tb._option_block(1, option_single, None)
        self.assertNotIn("Geceler:", text_single)

        option_night = {
            "kind": "direct",
            "true_total_try": 800.0,
            "duration_mins": 600,
            "depart_at": "2026-10-25T21:00",
            "arrive_at": "2026-10-26T07:00",
            "legs": [
                {
                    "type": "bus",
                    "operator": "FlixBus",
                    "from": "Viyana",
                    "to": "Krakov",
                    "depart_at": "2026-10-25T21:00",
                    "arrive_at": "2026-10-26T07:00",
                    "price_try": 800.0,
                }
            ],
        }
        text_night = tb._option_block(1, option_night, None)
        self.assertIn("Geceler: 🚌 yolda", text_night)

    def test_app_js_contains_nights_display(self):
        """Arayüzde (app.js) seçenek kartında ve multimodal kartında gece gösterimi."""
        import os
        static_dir = os.path.join(os.path.dirname(__file__), "..", "static", "js")
        app_js_path = os.path.join(static_dir, "app.js")
        with open(app_js_path, "r", encoding="utf-8") as f:
            code = f.read()

        self.assertIn("function formatNightsLine", code)
        self.assertIn("tp-nights", code)
        self.assertIn("nightsHtml", code)


if __name__ == "__main__":
    unittest.main()
