"""Tests for PLN-15a tour exploration (T13).

Covers candidate filtering and detour ratios, explore_tour orchestration,
night detection, /api/tour-explore HTTP validation, and Telegram /tur command formatting.
"""

import json
from datetime import date
from io import BytesIO
from unittest import mock
import pytest

from travel_engine.airports import AirportDB
from travel_engine.tour_explore import (
    TOUR_CITIES,
    default_candidates,
    explore_tour,
    nights,
)
from travel_engine.telegram_bot import (
    parse_tur_command,
    format_tur_results,
)
import server_v2
from server_v2 import TravelPlanAPIHandler


class MockAPIHandler(TravelPlanAPIHandler):
    def __init__(self, request_bytes):
        self.client_address = ("127.0.0.1", 12345)
        self.rfile = BytesIO(request_bytes)
        self.wfile = BytesIO()
        self.raw_requestline = self.rfile.readline()
        self.parse_request()

    def log_message(self, format, *args):
        pass


def _get(path):
    handler = MockAPIHandler(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode("utf-8"))
    handler.do_GET()
    raw = handler.wfile.getvalue()
    header_blob, body = raw.split(b"\r\n\r\n", 1)
    status = int(header_blob.decode("latin-1").split("\r\n")[0].split(" ")[1])
    return status, json.loads(body.decode("utf-8"))


def test_default_candidates():
    db = AirportDB.load_default()
    # Budapeşte, Viyana, Krakov are valid intermediate cities within detour ratio <= 1.6
    cands = default_candidates(db, "Atina", "Prag", limit=12)
    assert "Budapeşte" in cands
    assert "Viyana" in cands
    assert "Krakov" in cands
    # Lisbon has detour ratio ~3.3 (> 1.6)
    assert "Lizbon" not in cands
    # Origin and destination must not be candidate stops
    assert "Atina" not in cands
    assert "Prag" not in cands

    # Also test calling without db explicitly
    cands_no_db = default_candidates("Atina", "Prag", limit=12)
    assert "Budapeşte" in cands_no_db
    assert "Lizbon" not in cands_no_db


def test_explore_tour():
    class FakePlanner:
        def __init__(self):
            self.calls = []

        def plan_trip(
            self,
            origin,
            destination,
            depart_dates,
            arrive_by=None,
            depart_after=None,
            via=None,
            min_stay_mins=None,
            flight_search_budget=None,
            **kwargs,
        ):
            self.calls.append({
                "origin": origin,
                "destination": destination,
                "via": via,
                "min_stay_mins": min_stay_mins,
                "flight_search_budget": flight_search_budget,
            })
            if via is None:
                # Baseline option
                return {
                    "options": [
                        {
                            "true_total_try": 8272.0,
                            "price_complete": True,
                            "depart_at": "2026-10-25T10:00",
                            "arrive_at": "2026-10-26T15:00",
                            "legs": [
                                {
                                    "from": "Atina",
                                    "to": "Prag",
                                    "type": "flight",
                                    "depart_at": "2026-10-25T10:00",
                                    "arrive_at": "2026-10-26T15:00",
                                }
                            ],
                        }
                    ]
                }
            cand = via[0]
            if cand == "Budapeşte":
                return {
                    "options": [
                        {
                            "true_total_try": 10597.0,
                            "price_complete": True,
                            "depart_at": "2026-10-25T18:00",
                            "arrive_at": "2026-10-28T06:00",
                            "legs": [
                                {
                                    "from": "ATH",
                                    "to": "Budapeşte",
                                    "type": "flight",
                                    "depart_at": "2026-10-25T18:00",
                                    "arrive_at": "2026-10-25T20:00",
                                },
                                {
                                    "from": "Budapeşte",
                                    "to": "Prag",
                                    "type": "bus",
                                    "depart_at": "2026-10-27T23:00",
                                    "arrive_at": "2026-10-28T06:00",
                                },
                            ],
                        }
                    ]
                }
            elif cand == "Krakov":
                return {
                    "options": [
                        {
                            "true_total_try": 8654.0,
                            "price_complete": True,
                            "depart_at": "2026-10-25T12:00",
                            "arrive_at": "2026-10-27T08:00",
                            "legs": [
                                {
                                    "from": "ATH",
                                    "to": "Krakov",
                                    "type": "flight",
                                    "depart_at": "2026-10-25T12:00",
                                    "arrive_at": "2026-10-25T15:00",
                                },
                                {
                                    "from": "Krakov",
                                    "to": "Prag",
                                    "type": "train",
                                    "depart_at": "2026-10-26T22:00",
                                    "arrive_at": "2026-10-27T08:00",
                                },
                            ],
                        }
                    ]
                }
            elif cand == "Viyana":
                return {
                    "options": [],
                    "warnings": ["Viyana için uygun bağlantı bulunamadı"],
                }
            return {"options": []}

    planner = FakePlanner()
    res = explore_tour(
        planner,
        origin="Atina",
        destination="Prag",
        depart_dates=["2026-10-25"],
        candidates=["Budapeşte", "Krakov", "Viyana"],
        budget_per_candidate=8,
    )

    # 1. Baseline search has via=None
    assert planner.calls[0]["via"] is None
    # 2. Every candidate is queried with budget_per_candidate
    for call in planner.calls:
        assert call["flight_search_budget"] == 8

    # 3. Candidates are sorted ascending by total price (Krakov 8654 < Budapeşte 10597)
    assert len(res["rows"]) == 2
    assert res["rows"][0]["candidate"] == "Krakov"
    assert res["rows"][0]["true_total_try"] == 8654.0
    assert res["rows"][0]["diff_vs_baseline_try"] == round(8654.0 - 8272.0, 2)
    assert res["rows"][1]["candidate"] == "Budapeşte"
    assert res["rows"][1]["true_total_try"] == 10597.0
    assert res["rows"][1]["diff_vs_baseline_try"] == round(10597.0 - 8272.0, 2)

    # 4. Failed candidate is placed in skipped list with reason
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["candidate"] == "Viyana"
    assert "uygun bağlantı bulunamadı" in res["skipped"][0]["reason"]

    # 5. Baseline row verification
    assert res["baseline"]["true_total_try"] == 8272.0


def test_nights():
    # Budapeşte'de bir gece + gece otobüsü
    option = {
        "depart_at": "2026-10-25T18:00",
        "arrive_at": "2026-10-27T06:00",
        "legs": [
            {
                "from": "ATH",
                "to": "Budapeşte",
                "type": "flight",
                "depart_at": "2026-10-25T18:00",
                "arrive_at": "2026-10-25T20:00",
            },
            {
                "from": "Budapeşte",
                "to": "Prag",
                "type": "bus",
                "depart_at": "2026-10-26T23:00",
                "arrive_at": "2026-10-27T06:00",
            },
        ],
    }
    n = nights(option)
    assert n == [
        {"date": "2026-10-26", "type": "stay", "place": "Budapeşte"},
        {"date": "2026-10-27", "type": "transit"},
    ]


def test_api_tour_explore():
    # 1. Invalid budget_per_candidate (> 20 or < 1) returns 400
    status, body = _get(
        "/api/tour-explore?origin=Atina&destination=Prag&depart_dates=2026-10-25&budget_per_candidate=25"
    )
    assert status == 400
    assert "status" in body and body["status"] == "error"

    # 2. Valid request returns 200 and schema
    with mock.patch("server_v2.trip_planner") as mock_tp:
        mock_tp.plan_trip.return_value = {
            "options": [
                {
                    "true_total_try": 10597.0,
                    "price_complete": True,
                    "depart_at": "2026-10-25T18:00",
                    "arrive_at": "2026-10-28T06:00",
                    "legs": [],
                }
            ]
        }
        status, body = _get(
            "/api/tour-explore?origin=Atina&destination=Prag&depart_dates=2026-10-25&candidates=Budape%C5%9Fte&budget_per_candidate=8"
        )
        assert status == 200
        assert "baseline" in body
        assert "rows" in body
        assert "skipped" in body
        assert "candidates" in body


def test_telegram_tur_command():
    # 1. Parse valid /tur command
    cmd = parse_tur_command("/tur Atina Prag 25.10 en geç 28.10 12:00", today=date(2026, 10, 1))
    assert cmd is not None
    assert cmd["origin"] == "Atina"
    assert cmd["destination"] == "Prag"
    assert cmd["depart_dates"] == ["2026-10-25"]
    assert cmd["arrive_by"] == "2026-10-28T12:00"

    # 2. Format /tur output with fake result matching card's example
    fake_result = {
        "baseline": {
            "candidate": None,
            "true_total_try": 8272.0,
            "price_complete": True,
            "depart_at": "2026-10-25T10:00",
            "arrive_at": "2026-10-26T15:00",
            "legs": [{"type": "flight"}],
        },
        "rows": [
            {
                "candidate": "Budapeşte",
                "true_total_try": 10597.0,
                "price_complete": True,
                "depart_at": "2026-10-25T18:00",
                "arrive_at": "2026-10-28T06:00",
                "diff_vs_baseline_try": 2325.0,
                "nights": [{"date": "2026-10-26", "type": "stay", "place": "Budapeşte"}],
                "legs": [{"type": "bus"}],
            }
        ],
        "skipped": [],
        "candidates": ["Budapeşte"],
    }
    msg = format_tur_results(cmd, fake_result)
    assert "Tur" in msg or "tur" in msg
    assert "10.597 TL" in msg
    assert "(+2.325)" in msg
    assert "Budapeşte" in msg
    assert "🛏 Budapeşte" in msg
    assert "🚌" in msg


def test_nights_with_engine_leg_format():
    """Engine legs carry local depart_at and aware depart_utc ('...Z'); nights() must not mix them."""
    option = {
        "depart_at": "2026-10-26T23:50", "arrive_at": "2026-10-28T09:58",
        "legs": [
            {"from": "ATH", "to": "BTS", "depart_at": "2026-10-26T23:50", "arrive_at": "2026-10-27T01:05",
             "depart_utc": "2026-10-26T21:50:00Z", "arrive_utc": "2026-10-27T00:05:00Z"},
            {"from": "Bratislava", "to": "Prague", "depart_at": "2026-10-28T05:17", "arrive_at": "2026-10-28T09:58",
             "depart_utc": "2026-10-28T04:17:00Z", "arrive_utc": "2026-10-28T08:58:00Z"},
        ],
    }
    assert [n["type"] for n in nights(option)] == ["stay", "stay"]
    assert [n["date"] for n in nights(option)] == ["2026-10-27", "2026-10-28"]
