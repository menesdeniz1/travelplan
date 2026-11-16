"""Unit tests for faster page reading (GOO-6, card B04)."""

import unittest
from unittest import mock

from travel_engine.providers.google_flights_provider import (
    GoogleFlightsProvider,
    _new_scrape_state,
)

SAMPLE_CARD_TEXT = "\n".join([
    "10:30",
    "–",
    "12:45",
    "Pegasus",
    "2 sa. 15 dk.",
    "SAW–PRG",
    "Aktarmasız",
    "3.250 TL",
])


class FasterPageReadTests(unittest.TestCase):
    def setUp(self):
        self.provider = GoogleFlightsProvider()

    def test_eval_on_selector_all_used_and_body_not_read_when_cards_present(self):
        """When flight cards are present, eval_on_selector_all is used and body text is NOT read."""
        fake_page = mock.MagicMock()
        fake_page.url = "https://www.google.com/travel/flights/search?q=test"
        # Return list of card texts via eval_on_selector_all
        fake_page.eval_on_selector_all.return_value = [SAMPLE_CARD_TEXT]
        fake_page.query_selector_all.return_value = []

        fake_context = mock.MagicMock()
        fake_context.new_page.return_value = fake_page

        state = _new_scrape_state()
        self.provider._scrape_page(
            context=fake_context,
            state=state,
            google_url=fake_page.url,
            is_one_way=True,
            cal_key="test_key",
            now_ts=1000.0,
            want_calendar=False,
        )

        # 1. eval_on_selector_all must be called for "li.pIav2d"
        fake_page.eval_on_selector_all.assert_called_with(
            "li.pIav2d", "els => els.map(e => e.innerText)"
        )

        # 2. Results were parsed
        self.assertEqual(len(state["raw_results"]), 1)
        self.assertEqual(state["raw_results"][0]["airline"], "Pegasus")

        # 3. body inner_text must NOT be read when cards are parsed
        body_calls = [
            call for call in fake_page.inner_text.call_args_list if call.args == ("body",)
        ]
        self.assertEqual(len(body_calls), 0, "page.inner_text('body') should not be called when cards are parsed")

    def test_body_text_read_when_no_cards_present(self):
        """When no flight cards are present, body text IS read for classify_page."""
        fake_page = mock.MagicMock()
        fake_page.url = "https://www.google.com/travel/flights/search?q=test"
        fake_page.eval_on_selector_all.return_value = []
        fake_page.inner_text.return_value = "Üzgünüz, aramanızla eşleşen uçuş bulunamadı."

        fake_context = mock.MagicMock()
        fake_context.new_page.return_value = fake_page

        state = _new_scrape_state()
        self.provider._scrape_page(
            context=fake_context,
            state=state,
            google_url=fake_page.url,
            is_one_way=True,
            cal_key="test_key",
            now_ts=1000.0,
            want_calendar=False,
        )

        # body inner_text MUST be read when cards are absent
        body_calls = [
            call for call in fake_page.inner_text.call_args_list if call.args == ("body",)
        ]
        self.assertEqual(len(body_calls), 1, "page.inner_text('body') must be called when no cards present")
        self.assertEqual(state["body_text"], "Üzgünüz, aramanızla eşleşen uçuş bulunamadı.")


if __name__ == "__main__":
    unittest.main()
