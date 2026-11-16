"""Tests for Telegram near-miss display, city layover note, and mixed vehicle types (SUN-9, T04)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from travel_engine.telegram_bot import format_trip_results


def _leg(kind, operator, frm, to, dep, arr, last=False, vehicle_types=None):
    leg = {
        "type": kind,
        "operator": operator,
        "from": frm,
        "to": to,
        "depart_at": dep,
        "arrive_at": arr,
        "depart_utc": f"{dep}:00Z" if len(dep) == 16 else dep,
        "arrive_utc": f"{arr}:00Z" if len(arr) == 16 else arr,
        "is_last_service": last,
    }
    if vehicle_types is not None:
        leg["vehicle_types"] = list(vehicle_types)
    return leg


def _base_option(legs=None, total=14000.0, **extra):
    opt = {
        "kind": "side_route",
        "depart_at": "2026-10-28T08:00",
        "arrive_at": "2026-10-28T18:00",
        "total_try": total,
        "true_total_try": total,
        "duration_mins": 600,
        "price_complete": True,
        "legs": legs or [
            _leg("flight", "Aegean", "ATH", "PRG", "2026-10-28T08:00", "2026-10-28T10:30"),
        ],
    }
    opt.update(extra)
    return opt


class TelegramNearMissTests(unittest.TestCase):
    QUERY = {
        "origin": "Atina",
        "destination": "İstanbul",
        "depart_dates": ["2026-10-28"],
        "arrive_by": "2026-10-31T23:59",
    }

    def test_near_miss_section_displayed_in_telegram(self):
        """(kırmızı) near_misses varsa 'Kurala kıl payı uymayanlar' bölümü ve fark bilgisi görünür."""
        main_opt = _base_option(total=14000.0)
        nm_opt = _base_option(
            total=8000.0,
            late_by_mins=480,  # 8 hours late
            legs=[
                _leg("flight", "Austrian", "VIE", "SKG", "2026-10-31T14:00", "2026-10-31T16:50"),
                _leg("bus", "Metro", "Selanik", "İstanbul", "2026-10-31T17:30", "2026-11-01T08:00"),
            ],
        )

        result = {
            "options": [main_opt],
            "near_misses": [nm_opt],
            "warnings": [],
        }

        text = format_trip_results(self.QUERY, result)

        self.assertIn("Kurala kıl payı uymayanlar", text)
        self.assertIn("geç varıyor", text)
        self.assertIn("8 saat geç varıyor", text)
        self.assertIn("6,000 TL daha ucuz", text)
        self.assertIn("8,000 ₺", text)

    def test_near_miss_empty_does_not_render_section(self):
        """(koruma) near_misses boşken 'Kurala kıl payı uymayanlar' başlığı çıkmaz."""
        result = {
            "options": [_base_option(total=10000.0)],
            "near_misses": [],
            "warnings": [],
        }
        text = format_trip_results(self.QUERY, result)
        self.assertNotIn("Kurala kıl payı uymayanlar", text)

    def test_city_layover_note_when_wait_ge_180_mins(self):
        """(kırmızı) Şehirde bekleme >= 180 dk ise '<Şehir>'de <s> saat <dk> dk serbest' notu eklenir."""
        # Passenger arrives in Krakov at 12:00, leaves Krakov at 15:30 -> 210 mins (3 sa 30 dk)
        option = _base_option(legs=[
            _leg("flight", "Wizz Air", "ATH", "KRK", "2026-10-28T09:00", "2026-10-28T11:00"),
            _leg("transfer", "Airport Transit", "KRK Airport", "Krakov", "2026-10-28T11:15", "2026-10-28T12:00"),
            _leg("train", "RegioJet", "Krakov", "Praha", "2026-10-28T15:30", "2026-10-28T21:00"),
        ])
        result = {"options": [option], "warnings": []}

        text = format_trip_results(self.QUERY, result)
        self.assertIn("Krakov'da 3 saat 30 dk serbest", text)

    def test_city_layover_note_not_shown_when_wait_under_180_mins(self):
        """(koruma) Şehirde bekleme < 180 dk ise serbest zaman notu çıkmaz."""
        # Arrives Krakov at 12:00, leaves Krakov at 14:00 -> 120 mins (< 180)
        option = _base_option(legs=[
            _leg("transfer", "Airport Transit", "KRK Airport", "Krakov", "2026-10-28T11:15", "2026-10-28T12:00"),
            _leg("train", "RegioJet", "Krakov", "Praha", "2026-10-28T14:00", "2026-10-28T20:00"),
        ])
        result = {"options": [option], "warnings": []}

        text = format_trip_results(self.QUERY, result)
        self.assertNotIn("serbest", text)

    def test_mixed_vehicle_types_displayed_as_bus_and_train(self):
        """(kırmızı) vehicle_types ['bus','train'] olan bacak için 'otobüs + tren' yazılır."""
        option = _base_option(legs=[
            _leg("train", "RegioJet", "Krakov", "Praha", "2026-10-28T08:00", "2026-10-28T14:00",
                 vehicle_types=["bus", "train"]),
        ])
        result = {"options": [option], "warnings": []}

        text = format_trip_results(self.QUERY, result)
        self.assertIn("otobüs + tren", text)

    def test_city_layover_duplicate_with_stopover_suppressed(self):
        """(kırmızı) stopover Selanik olan seçenekte '🏙️' satırı yok; stopover olmayan seçenekte var."""
        legs = [
            _leg("flight", "Olympic", "ATH", "SKG", "2026-10-28T08:00", "2026-10-28T09:00"),
            _leg("transfer", "Transit", "SKG", "Selanik", "2026-10-28T09:15", "2026-10-28T10:00"),
            _leg("bus", "Metro", "Selanik", "Istanbul", "2026-10-28T18:00", "2026-10-29T02:00"),
        ]
        opt_with_stopover = _base_option(
            legs=legs,
            stopover={"place": "Selanik", "mins": 480},
        )
        text_with = format_trip_results(self.QUERY, {"options": [opt_with_stopover], "warnings": []})
        self.assertIn("⏸ Selanik", text_with)
        self.assertNotIn("🏙️", text_with)

        opt_without = _base_option(legs=legs)
        text_without = format_trip_results(self.QUERY, {"options": [opt_without], "warnings": []})
        self.assertIn("🏙️", text_without)

    def test_city_layover_zero_minutes_formats_hours_only(self):
        """(kırmızı) 480 dakika -> '8 saat', '8 saat 0 dk' değil."""
        legs = [
            _leg("flight", "Olympic", "ATH", "SKG", "2026-10-28T08:00", "2026-10-28T09:00"),
            _leg("transfer", "Transit", "SKG", "Selanik", "2026-10-28T09:15", "2026-10-28T10:00"),
            _leg("bus", "Metro", "Selanik", "Istanbul", "2026-10-28T18:00", "2026-10-29T02:00"),
        ]
        opt = _base_option(legs=legs)
        text = format_trip_results(self.QUERY, {"options": [opt], "warnings": []})
        self.assertIn("Selanik'te 8 saat serbest", text)
        self.assertNotIn("8 saat 0 dk", text)


if __name__ == "__main__":
    unittest.main()
