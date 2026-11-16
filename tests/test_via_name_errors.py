"""Tests for via name error handling and airport DB resolution (SUN-10, T05)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server_v2 import _parse_via as server_parse_via
from travel_engine.airports import AirportDB
from travel_engine.telegram_bot import _parse_via as telegram_parse_via


class ViaNameErrorsTests(unittest.TestCase):
    def test_corrupted_char_gives_specific_error_in_server(self):
        """(kırmızı) server_v2._parse_via('Mnih') bozuk karakter mesajıyla ValueError verir."""
        corrupted = "M\ufffdnih"
        expected_msg = (
            f"Ara durak adında bozuk karakter var: '{corrupted}'. "
            "İstek UTF-8 olmalı; İngilizce ad ya da havalimanı kodu da olur (ör. Munich, MUC)."
        )
        with self.assertRaises(ValueError) as ctx:
            server_parse_via(corrupted)
        self.assertEqual(str(ctx.exception), expected_msg)

    def test_corrupted_char_gives_specific_error_in_telegram_bot(self):
        """(kırmızı) telegram_bot._parse_via(['Mnih']) bozuk karakter mesajıyla ValueError verir."""
        corrupted = "M\ufffdnih"
        expected_msg = (
            f"Ara durak adında bozuk karakter var: '{corrupted}'. "
            "İstek UTF-8 olmalı; İngilizce ad ya da havalimanı kodu da olur (ör. Munich, MUC)."
        )
        with self.assertRaises(ValueError) as ctx:
            telegram_parse_via([corrupted])
        self.assertEqual(str(ctx.exception), expected_msg)

    def test_valid_munich_names_do_not_raise(self):
        """(koruma) 'Munich', 'MUC', 'Münih' geçerlidir ve hata vermez."""
        for name in ("Munich", "MUC", "Münih"):
            # server_v2
            res_server = server_parse_via(name)
            self.assertEqual(res_server, name)

            # telegram_bot
            res_bot = telegram_parse_via([name])
            self.assertEqual(res_bot, [name])

    def test_airport_db_resolves_all_munich_variants_to_muc(self):
        """AirportDB resolve: 'Munich', 'MUC' ve 'Münih' aynı havalimanını (MUC) içeriyor."""
        db = AirportDB.load_default()
        for name in ("Munich", "MUC", "Münih"):
            resolved = db.resolve(name)
            self.assertTrue(bool(resolved), f"Failed to resolve {name}")
            iatas = [ap.iata for ap in resolved]
            self.assertIn("MUC", iatas, f"{name} does not resolve to MUC: {iatas}")


if __name__ == "__main__":
    unittest.main()
