"""Telegram command bot: search flights from a phone via the existing alert bot.

Commands (only from the configured owner chat, telegram_chat_id):
    /ara Atina Prag 28.10              (also 28.10.2026, 2026-10-28, yarın, bugün)
    /ara "New York" Londra 3.11        (or /ara New-York Londra 3.11)
    /gezi Atina Prag 27.10-28.10 [en geç 28.10 12:00] [üzerinden Yunanistan]
         [durak 0-3] [bütçe 1-60] [esnek 0-3] [dönüş <tarih> [dönüş en geç <tarih> <saat>]]
         [bagajsız | valiz]
    /takip <direkt_bütçe> [yan_yol_bütçe]   (watch the last /gezi or /ara, whole query kept)
    /liste, /sil <no>, /durum
    /start, /yardim                    (usage help)

Uses Telegram long polling (getUpdates) with the token from AlertNotifier.config
and replies through AlertNotifier._post_telegram. Stdlib only.
"""

import html
import json
import logging
import math
import re
import shlex
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from travel_engine.tour_explore import format_nights_line

logger = logging.getLogger(__name__)

# Messages older than this (e.g. sent while the server was off) are ignored
MAX_MESSAGE_AGE_SECS = 10 * 60

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 3900  # Telegram hard limit is 4096; keep a safety margin
MAX_FIELD_CHARS = 80      # per dynamic value, so one huge name cannot blow the limit

HELP_TEXT = (
    "🤖 <b>Komutlar</b>\n\n"
    "<b>Arama</b>\n"
    "<code>/ara Atina Prag 28.10</code> · direkt ve yan rota\n"
    "<code>/ara \"New York\" Londra 3.11</code>\n"
    "<code>/gezi Atina Prag 27.10-28.10</code> · tarih aralığı (en fazla 7 gün)\n"
    "<code>/gezi Atina Prag 27.10 en geç 28.10 12:00</code> · varış sınırı\n"
    "<code>/gezi Atina Prag 27.10 üzerinden Yunanistan</code> · ara durak\n"
    "<code>/gezi Atina Prag 27.10 durak 1</code> · en fazla 1 otomatik ara durak (0–3)\n"
    "<code>/gezi Atina Prag 27.10 bütçe 40</code> · uçuş araması bütçesi (varsayılan 20, en fazla 60)\n"
    "<code>/gezi Atina Prag 27.10 esnek 2</code> · tarihler ±2 gün taransın\n"
    "<code>/gezi Atina Prag 27.10 dönüş 30.10</code> · gidiş-dönüş (<code>dönüş en geç 30.10 18:00</code> ile saat sınırı)\n"
    "<code>/gezi ... bagajsız</code> · yalnız kabin bagajı · <code>valiz</code> · kayıtlı bagaj\n"
    "<code>/gezi ... gerçek</code> · yalnız gerçek veriler (tahmini ücret, rota ve otel yok)\n"
    "<code>/tur Atina Prag 25.10 en geç 28.10 12:00</code> · ara duraklı tur keşfi\n\n"
    "<b>Takip</b>\n"
    "<code>/takip 9500 6500</code> · son aramaya göre: direkt 9.500 ₺, yan yol 6.500 ₺\n"
    "<code>/takip 9500</code> · yalnızca direkt bütçe\n"
    "<code>/liste</code> · aktif takipler\n"
    "<code>/sil 2</code> · 2 numaralı takibi sil\n\n"
    "<b>Durum</b>\n"
    "<code>/durum</code> · sunucu ve sağlık durumu\n\n"
    "Tarih: 28.10, 28.10.2026, 2026-10-28, yarın, bugün. "
    "Şehir adı iki kelimeyse tırnak veya tire kullanın (New-York). "
    "Yıl verilmezse en yakın gelecek tarih alınır."
)

_HELP_COMMANDS = {"/start", "/yardim", "/yardım", "/help"}
_EXTENDED_COMMANDS = {"/gezi", "/takip", "/liste", "/sil", "/durum", "/tur"}
_TODAY_WORDS = {"bugün", "bugun"}
_TOMORROW_WORDS = {"yarın", "yarin"}
_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY_RE = re.compile(r"^(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?$")


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------

def _parse_date_token(token: str, today: date) -> Optional[date]:
    """Parse a date token. Missing year means the next occurrence on or after today."""
    low = token.strip().lower()
    if low in _TODAY_WORDS:
        return today
    if low in _TOMORROW_WORDS:
        return today + timedelta(days=1)

    m = _ISO_RE.match(low)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = _DMY_RE.match(low)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year_text = m.group(3)
    if year_text is not None:
        year = int(year_text)
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None

    # No year: first valid date on/after today (handles year rollover and 29.02).
    for year in range(today.year, today.year + 9):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


def _clean_city(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.replace("-", " ")).strip()


def parse_search_command(text: str, today: date) -> Optional[Dict[str, Any]]:
    """Parse a bot message.

    Returns {"help": True} for /start and /yardim, {"origin", "destination", "date"}
    (date as YYYY-MM-DD) for a search, or None when the text is not understood.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(None, 1)
    command = parts[0].lower().split("@", 1)[0]  # "/ara@MyBot" -> "/ara"
    rest = parts[1] if len(parts) > 1 else ""

    if command in _HELP_COMMANDS:
        return {"help": True}
    if command != "/ara":
        return None

    try:
        tokens = shlex.split(rest)
    except ValueError:  # unbalanced quotes
        return None
    if len(tokens) not in (3, 4):
        return None
    schengen = False
    if len(tokens) == 4:
        if tokens[3].lower() != "schengen":
            return None
        schengen = True

    origin_raw, destination_raw, date_raw = tokens[:3]
    travel_date = _parse_date_token(date_raw, today)
    if travel_date is None:
        return None

    origin = _clean_city(origin_raw)
    destination = _clean_city(destination_raw)
    if not origin or not destination or origin.lower() == destination.lower():
        return None
    if len(origin) > 60 or len(destination) > 60:
        return None

    out = {
        "origin": origin,
        "destination": destination,
        "date": travel_date.isoformat(),
    }
    if schengen:
        out["schengen_single_entry"] = True
    return out


GEZI_USAGE = "Kullanım: /gezi Nereden Nereye 27.10-28.10 [en geç 28.10 12:00] [üzerinden Yunanistan]"
TAKIP_USAGE = "Kullanım: /takip <direkt_bütçe> [yan_yol_bütçe], örn. /takip 9500 6500"
SIL_USAGE = "Kullanım: /sil <numara>. Numaralar /liste ile görünür."
TRIP_MAX_DAYS = 7
_CLOCK_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_BARE_DAY_RE = re.compile(r"^\d{1,2}$")
_VIA_PART_RE = re.compile(r"[A-Za-zÇĞİÖŞÜçğıöşü][A-Za-zÇĞİÖŞÜçğıöşü .'\-]{1,59}")
_GEC_WORDS = {"geç", "gec"}
_VIA_WORDS = {"üzerinden", "uzerinden"}
_SMALL_INT_RE = re.compile(r"\d{1,3}")
# Option keywords of /gezi (besides "en geç" and "üzerinden"); each takes a value or stands alone
_DURAK_WORDS = {"durak"}
_BUDGET_WORDS = {"bütçe", "butce"}
_FLEX_WORDS = {"esnek"}
_RETURN_WORDS = {"dönüş", "donus"}
_CABIN_ONLY_WORDS = {"bagajsız", "bagajsiz"}
_CHECKED_BAG_WORDS = {"valiz"}
_REAL_ONLY_WORDS = {"gerçek", "gercek"}
_OPTION_WORDS = (_DURAK_WORDS | _BUDGET_WORDS | _FLEX_WORDS | _RETURN_WORDS | _CABIN_ONLY_WORDS
                 | _CHECKED_BAG_WORDS | _REAL_ONLY_WORDS)


def _split_command(text: Any) -> Tuple[Optional[str], str]:
    """("/gezi", "Atina Prag 27.10") for a slash command, (None, "") for anything else."""
    if not isinstance(text, str):
        return None, ""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None, ""
    parts = stripped.split(None, 1)
    name = parts[0].lower().split("@", 1)[0]
    return name, (parts[1] if len(parts) > 1 else "")


def _validate_route(origin: str, destination: str) -> None:
    if not origin or not destination:
        raise ValueError("Nereden ve nereye boş olamaz.")
    if origin.lower() == destination.lower():
        raise ValueError("Nereden ve nereye aynı olamaz.")
    if len(origin) > 60 or len(destination) > 60:
        raise ValueError("Şehir adı en fazla 60 karakter olabilir.")


def _parse_date_range(token: str, today: date) -> List[str]:
    """One date ("28.10", "yarın", "2026-10-28") or an inclusive range ("27.10-28.10").

    A bare day on the left borrows the month of the right side ("28-30.10").
    Returns ISO dates; raises ValueError with a Turkish message.
    """
    single = _parse_date_token(token, today)
    if single is not None:
        return [single.isoformat()]
    for match in re.finditer("-", token):
        left, right = token[: match.start()], token[match.end():]
        if not right.strip():
            continue
        if _BARE_DAY_RE.match(left):
            right_parts = _DMY_RE.match(right.strip().lower())
            if right_parts:
                left = f"{left}.{right_parts.group(2)}"
                if right_parts.group(3):
                    left += f".{right_parts.group(3)}"
        start = _parse_date_token(left, today)
        if start is None:
            continue
        end = _parse_date_token(right, start)  # no year: first occurrence on/after start
        if end is None:
            continue
        if end < start:
            raise ValueError("Bitiş tarihi başlangıç tarihinden önce olamaz.")
        days = (end - start).days + 1
        if days > TRIP_MAX_DAYS:
            raise ValueError(f"Tarih aralığı en fazla {TRIP_MAX_DAYS} gün olabilir.")
        return [(start + timedelta(days=i)).isoformat() for i in range(days)]
    raise ValueError(f"Tarih anlaşılamadı: '{token}'. Örnek: 28.10, yarın veya 27.10-28.10")


def _nearest_date(token: str, anchor: date) -> Optional[date]:
    """Date token resolved to the occurrence closest to anchor, so "27.10" before a 28.10 departure stays in 2026."""
    forward = _parse_date_token(token, anchor)
    backward = _parse_date_token(token, anchor - timedelta(days=365))
    if forward is None or backward is None:
        return forward
    return backward if abs((backward - anchor).days) < abs((forward - anchor).days) else forward


def _parse_clock(token: str) -> Optional[Tuple[int, int]]:
    match = _CLOCK_RE.match(token.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _parse_via(words: List[str]) -> List[str]:
    """Place names after "üzerinden"; commas separate several places."""
    places = [part.strip() for part in " ".join(words).split(",")]
    places = [place for place in places if place]
    for place in places:
        if "\ufffd" in place:
            raise ValueError(f"Ara durak adında bozuk karakter var: '{place}'. İstek UTF-8 olmalı; İngilizce ad ya da havalimanı kodu da olur (ör. Munich, MUC).")
        if not _VIA_PART_RE.fullmatch(place):
            raise ValueError(f"Ara durak geçersiz: '{place}'. Ülke veya şehir adı girin (ör. Yunanistan).")
    return places


def _int_arg(tokens: List[str], i: int, low: int, high: int, word: str, example: str) -> int:
    """The whole number after the keyword at tokens[i], within [low, high]."""
    if i + 1 >= len(tokens) or not _SMALL_INT_RE.fullmatch(tokens[i + 1]):
        raise ValueError(f"'{word}' için {low}-{high} arasında bir sayı girin (ör. {example}).")
    value = int(tokens[i + 1])
    if not low <= value <= high:
        raise ValueError(f"'{word}' {low} ile {high} arasında olmalı.")
    return value


def parse_gezi_command(text: str, today: date) -> Optional[Dict[str, Any]]:
    """Parse "/gezi Nereden Nereye <tarih|tarih1-tarih2> [options]".

    Options (any order after the dates; each at most once):
      en geç <tarih> <saat>            outbound must arrive by this time
      üzerinden <yer>[, <yer>]         fixed waypoints, in order
      durak <0-3>                      automatic stops (route engine max_stops)
      bütçe <1-60>                     flight searches allowed for this query
      esnek <0-3>                      flexible dates, +-N days around each date
      dönüş <tarih|tarih1-tarih2>      round trip: return dates
      dönüş en geç <tarih> <saat>      return must arrive by this time (needs dönüş)
      bagajsız | valiz                 cabin bag only (no checked bag) | checked bag
      gerçek                           real data only (no estimated price, route or hotel)

    Returns None when the text is not /gezi. Returns {"error": "..."} (Turkish, with usage)
    when it is /gezi but incomplete or invalid. Otherwise returns {"origin", "destination",
    "depart_dates" (ISO list), "arrive_by" ("YYYY-MM-DDTHH:MM" or None), "via" (list or None)}
    plus only the keys the user wrote: "max_stops", "flight_search_budget", "flex_days",
    "return_dates", "return_arrive_by", "cabin_bag", "checked_bag", "real_data_only".
    """
    name, rest = _split_command(text)
    if name != "/gezi":
        return None
    try:
        tokens = shlex.split(rest)
    except ValueError:
        return {"error": f"Tırnak işaretleri eksik. {GEZI_USAGE}"}
    if not tokens:
        return {"error": GEZI_USAGE}
    if len(tokens) < 3:
        missing = ["nereden", "nereye", "tarih"][len(tokens)]
        return {"error": f"Eksik bilgi: {missing}. {GEZI_USAGE}"}

    try:
        origin = _clean_city(tokens[0])
        destination = _clean_city(tokens[1])
        _validate_route(origin, destination)
        depart_dates = _parse_date_range(tokens[2], today)
        depart_start = date.fromisoformat(depart_dates[0])

        arrive_by: Optional[str] = None
        via: Optional[List[str]] = None
        return_dates: Optional[List[str]] = None
        return_arrive_by: Optional[str] = None
        extras: Dict[str, Any] = {}
        i = 3
        while i < len(tokens):
            low = tokens[i].lower()
            if low == "en" and i + 1 < len(tokens) and tokens[i + 1].lower() in _GEC_WORDS:
                if arrive_by is not None:
                    raise ValueError("'en geç' yalnızca bir kez yazılabilir.")
                if i + 3 >= len(tokens):
                    raise ValueError("'en geç' için tarih ve saat girin (ör. en geç 28.10 12:00).")
                arr_date = _nearest_date(tokens[i + 2], depart_start)
                clock = _parse_clock(tokens[i + 3])
                if arr_date is None or clock is None:
                    raise ValueError("'en geç' için geçerli tarih ve saat girin (ör. en geç 28.10 12:00).")
                arrive_by = f"{arr_date.isoformat()}T{clock[0]:02d}:{clock[1]:02d}"
                i += 4
            elif low in _VIA_WORDS:
                if via is not None:
                    raise ValueError("'üzerinden' yalnızca bir kez yazılabilir.")
                words: List[str] = []
                j = i + 1
                while j < len(tokens) and not _ends_via(tokens, j):
                    words.append(tokens[j])
                    j += 1
                if not words:
                    raise ValueError("'üzerinden' için bir yer girin (ör. üzerinden Yunanistan).")
                via = _parse_via(words) or None
                if via is None:
                    raise ValueError("'üzerinden' için bir yer girin (ör. üzerinden Yunanistan).")
                i = j
            elif low in _DURAK_WORDS:
                if "max_stops" in extras:
                    raise ValueError("'durak' yalnızca bir kez yazılabilir.")
                extras["max_stops"] = _int_arg(tokens, i, 0, 3, "durak", "durak 1")
                i += 2
            elif low in _BUDGET_WORDS:
                if "flight_search_budget" in extras:
                    raise ValueError("'bütçe' yalnızca bir kez yazılabilir.")
                extras["flight_search_budget"] = _int_arg(tokens, i, 1, 60, "bütçe", "bütçe 40")
                i += 2
            elif low in _FLEX_WORDS:
                if "flex_days" in extras:
                    raise ValueError("'esnek' yalnızca bir kez yazılabilir.")
                extras["flex_days"] = _int_arg(tokens, i, 0, 3, "esnek", "esnek 2")
                i += 2
            elif low in _RETURN_WORDS:
                if i + 2 < len(tokens) and tokens[i + 1].lower() == "en" and tokens[i + 2].lower() in _GEC_WORDS:
                    # "dönüş en geç <tarih> <saat>": the return must arrive by this time
                    if return_arrive_by is not None:
                        raise ValueError("'dönüş en geç' yalnızca bir kez yazılabilir.")
                    if i + 4 >= len(tokens):
                        raise ValueError("'dönüş en geç' için tarih ve saat girin (ör. dönüş en geç 30.10 18:00).")
                    arr_date = _nearest_date(tokens[i + 3], depart_start)
                    clock = _parse_clock(tokens[i + 4])
                    if arr_date is None or clock is None:
                        raise ValueError("'dönüş en geç' için geçerli tarih ve saat girin (ör. dönüş en geç 30.10 18:00).")
                    return_arrive_by = f"{arr_date.isoformat()}T{clock[0]:02d}:{clock[1]:02d}"
                    i += 5
                else:
                    # "dönüş <tarih|aralık>": the return dates
                    if return_dates is not None:
                        raise ValueError("'dönüş' tarihi yalnızca bir kez yazılabilir.")
                    if i + 1 >= len(tokens):
                        raise ValueError("'dönüş' için tarih girin (ör. dönüş 30.10 veya dönüş 30.10-02.11).")
                    return_dates = _parse_date_range(tokens[i + 1], depart_start)
                    if date.fromisoformat(return_dates[0]) < depart_start:
                        raise ValueError("Dönüş tarihi gidiş tarihinden önce olamaz.")
                    i += 2
            elif low in _CABIN_ONLY_WORDS:
                extras["cabin_bag"] = False
                i += 1
            elif low in _CHECKED_BAG_WORDS:
                extras["checked_bag"] = True
                i += 1
            elif low in _REAL_ONLY_WORDS:
                extras["real_data_only"] = True
                i += 1
            else:
                raise ValueError(f"Anlaşılmayan ifade: '{tokens[i]}'.")

        if arrive_by is not None and arrive_by < f"{depart_dates[0]}T00:00":
            raise ValueError("Varış zamanı kalkış tarihinden önce olamaz.")
        if return_arrive_by is not None and return_dates is None:
            raise ValueError("'dönüş en geç' için önce dönüş tarihi girin (ör. dönüş 30.10).")
        if return_arrive_by is not None and return_arrive_by < f"{return_dates[0]}T00:00":
            raise ValueError("Dönüş varış zamanı dönüş tarihinden önce olamaz.")
    except ValueError as exc:
        return {"error": f"{exc} {GEZI_USAGE}"}

    result: Dict[str, Any] = {
        "origin": origin,
        "destination": destination,
        "depart_dates": depart_dates,
        "arrive_by": arrive_by,
        "via": via,
        **extras,
    }
    if return_dates is not None:
        result["return_dates"] = return_dates
    if return_arrive_by is not None:
        result["return_arrive_by"] = return_arrive_by
    return result


TUR_USAGE = "Kullanım: /tur <kalkış> <varış> <tarihler> [son varış], örn. /tur Atina Prag 25.10 en geç 28.10 12:00"


def parse_tur_command(text: str, today: date) -> Optional[Dict[str, Any]]:
    """Parse "/tur <kalkış> <varış> <tarihler> [son varış]"."""
    name, rest = _split_command(text)
    if name != "/tur":
        return None
    try:
        tokens = shlex.split(rest)
    except ValueError:
        return {"error": f"Tırnak işaretleri eksik. {TUR_USAGE}"}
    if not tokens:
        return {"error": TUR_USAGE}
    if len(tokens) < 3:
        missing = ["nereden", "nereye", "tarih"][len(tokens)]
        return {"error": f"Eksik bilgi: {missing}. {TUR_USAGE}"}

    try:
        origin = _clean_city(tokens[0])
        destination = _clean_city(tokens[1])
        _validate_route(origin, destination)
        depart_dates = _parse_date_range(tokens[2], today)
        depart_start = date.fromisoformat(depart_dates[0])

        arrive_by: Optional[str] = None
        if len(tokens) > 3:
            rem = tokens[3:]
            if rem[0].lower() == "en" and len(rem) > 1 and rem[1].lower() in _GEC_WORDS:
                if len(rem) < 4:
                    raise ValueError("'en geç' için tarih ve saat girin (ör. en geç 28.10 12:00).")
                arr_date = _nearest_date(rem[2], depart_start)
                clock = _parse_clock(rem[3])
                if arr_date is None or clock is None:
                    raise ValueError("'en geç' için geçerli tarih ve saat girin (ör. en geç 28.10 12:00).")
                arrive_by = f"{arr_date.isoformat()}T{clock[0]:02d}:{clock[1]:02d}"
            elif len(rem) >= 2:
                arr_date = _nearest_date(rem[0], depart_start)
                clock = _parse_clock(rem[1])
                if arr_date is not None and clock is not None:
                    arrive_by = f"{arr_date.isoformat()}T{clock[0]:02d}:{clock[1]:02d}"

        return {
            "origin": origin,
            "destination": destination,
            "depart_dates": depart_dates,
            "arrive_by": arrive_by,
        }
    except ValueError as e:
        return {"error": f"{e} {TUR_USAGE}"}

def _ends_via(tokens: List[str], j: int) -> bool:
    """True where the words after "üzerinden" stop: "en geç" or the start of another option."""
    low = tokens[j].lower()
    if low == "en" and j + 1 < len(tokens) and tokens[j + 1].lower() in _GEC_WORDS:
        return True
    return low in _OPTION_WORDS


def _parse_budget(token: str) -> Optional[float]:
    """"9500", "9.500", "9.500,50", "9500₺", "9500 TL" -> TRY amount, or None."""
    text = token.strip().lower().replace("₺", "").replace("tl", "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", text):
        text = re.sub(r"[.,]", "", text)  # thousands separators
    elif "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text:  # 1.234,50
        text = text.replace(".", "").replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0 or value > 10_000_000:
        return None
    return value


def parse_takip_command(text: str) -> Optional[Dict[str, Any]]:
    """Parse "/takip <direkt_bütçe> [yan_yol_bütçe]".

    Returns None when the text is not /takip, {"error": ...} when invalid, otherwise
    {"direct_budget_try": float, "side_budget_try": float or None}.
    """
    name, rest = _split_command(text)
    if name != "/takip":
        return None
    tokens = rest.split()
    if not tokens or len(tokens) > 2:
        return {"error": TAKIP_USAGE}
    direct = _parse_budget(tokens[0])
    if direct is None:
        return {"error": f"Direkt bütçe anlaşılamadı: '{tokens[0]}'. {TAKIP_USAGE}"}
    side: Optional[float] = None
    if len(tokens) == 2:
        side = _parse_budget(tokens[1])
        if side is None:
            return {"error": f"Yan yol bütçesi anlaşılamadı: '{tokens[1]}'. {TAKIP_USAGE}"}
    return {"direct_budget_try": direct, "side_budget_try": side}


def parse_sil_command(text: str) -> Optional[Dict[str, Any]]:
    """Parse "/sil <numara>". Returns None for other text, {"error"} or {"index": int}."""
    name, rest = _split_command(text)
    if name != "/sil":
        return None
    token = rest.strip().lstrip("#")
    if not re.fullmatch(r"[0-9]+", token) or int(token) < 1:
        return {"error": SIL_USAGE}
    return {"index": int(token)}


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def _e(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    """Escape a dynamic value for Telegram HTML parse mode, truncated."""
    text = "" if value is None else str(value)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return html.escape(text)


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.0f} ₺"
    except (TypeError, ValueError):
        return "?"


def _price(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def _duration(mins: Any) -> str:
    try:
        total = int(mins)
    except (TypeError, ValueError):
        return ""
    hours, minutes = divmod(max(total, 0), 60)
    return f"{hours} sa {minutes} dk" if hours else f"{minutes} dk"


def _display_date(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(iso)


def _flight_times(flight: Dict[str, Any]) -> str:
    dep = flight.get("departure_time")
    arr = flight.get("arrival_time")
    if dep and arr:
        return f"{dep}–{arr}"
    return str(flight.get("times") or "").strip()


def _locative(city: str) -> str:
    name = (city or "").strip()
    if not name:
        return "'de"
    vowels = [c for c in name.lower() if c in "aıoueiöü"]
    last_vowel = vowels[-1] if vowels else "e"
    last_char = name[-1].lower()
    d_or_t = "t" if last_char in "fstkçşhp" else "d"
    a_or_e = "a" if last_vowel in "aıou" else "e"
    return f"'{d_or_t}{a_or_e}"


def _is_city_name(place: Any) -> bool:
    if not place or not isinstance(place, str):
        return False
    p = place.strip()
    if len(p) == 3 and p.isupper() and p.isalpha():
        return False
    if p.endswith(" Airport") or p.endswith(" Havalimanı"):
        return False
    return True


def _parse_iso_dt(val: Any) -> Optional[datetime]:
    if not val:
        return None
    s = str(val).replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            pass
    return None


def _city_layover_note(leg_a: Dict[str, Any], leg_b: Dict[str, Any], stopover_place: Optional[str] = None) -> Optional[str]:
    city = leg_a.get("to")
    if not _is_city_name(city):
        return None
    if stopover_place and str(city).strip().casefold() == str(stopover_place).strip().casefold():
        return None
    arr = leg_a.get("arrive_utc") or leg_a.get("arrive_at")
    dep = leg_b.get("depart_utc") or leg_b.get("depart_at")
    dt_arr = _parse_iso_dt(arr)
    dt_dep = _parse_iso_dt(dep)
    if not dt_arr or not dt_dep or dt_dep <= dt_arr:
        return None
    wait_mins = int((dt_dep - dt_arr).total_seconds() // 60)
    if wait_mins < 180:
        return None
    h = wait_mins // 60
    m = wait_mins % 60
    time_str = f"{h} saat" if m == 0 else f"{h} saat {m} dk"
    return f"{city}{_locative(city)} {time_str} serbest"


def _is_mixed_vehicle(item: Dict[str, Any]) -> bool:
    vt = [str(x).lower() for x in (item.get("vehicle_types") or [])]
    return "bus" in vt and "train" in vt


def _segment_text(segment: Dict[str, Any]) -> str:
    seg_type = str(segment.get("type") or "")
    route = f"{_e(segment.get('from'))}→{_e(segment.get('to'))}"
    times = f"{_e(segment.get('departure_time'))}–{_e(segment.get('arrival_time'))}"
    if seg_type == "flight":
        res = f"✈️ {_e(segment.get('operator'))} {route} {times}"
    elif _is_mixed_vehicle(segment):
        res = f"🚌+🚆 otobüs + tren {route} {times}"
    else:
        icon = "🚆" if seg_type == "train" else "🚌" if seg_type == "bus" else "🚐"
        res = f"{icon} {route} {times}"
    if segment.get("is_last_service"):
        res = f"{res} · son sefer"
    next_dep = segment.get("next_departure_at")
    if next_dep:
        hm = _hhmm(next_dep) or _e(str(next_dep).strip(), 20)
        if hm:
            res = f"{res} · sonraki {hm}"
    return res


def _route_block(index: int, journey: Dict[str, Any], direct_price: Optional[float]) -> str:
    segments = [s for s in (journey.get("segments") or []) if isinstance(s, dict)]
    chain = " · ".join(_segment_text(s) for s in segments if s.get("type") != "transfer")

    total_value = journey.get("true_total_try", journey.get("total_price_try"))
    total_line = f"💰 <b>{_money(total_value)}</b>"
    if direct_price is not None:
        try:
            savings = direct_price - float(total_value)
        except (TypeError, ValueError):
            savings = 0.0
        if savings > 0:
            total_line += f" (direkte göre <code>+{savings:,.0f} ₺</code> tasarruf)"
    if journey.get("estimated_hotel_try"):
        total_line += f" · otel ~{_money(journey.get('estimated_hotel_try'))} dahil"

    details = [f"⏱ {_duration(journey.get('total_duration_mins'))}"]
    if journey.get("night_wait"):
        wait = journey.get("night_wait_mins")
        details.append("🌙 gece beklemesi" + (f" ({_duration(wait)})" if wait else ""))
    try:
        day_offset = int(journey.get("arrival_day_offset") or 0)
    except (TypeError, ValueError):
        day_offset = 0
    if day_offset > 0:
        details.append(f"+{day_offset} gün")
    if any(s.get("is_estimated") for s in segments if s.get("type") not in ("flight", "transfer")):
        details.append("⚠️ tren saatleri tahmini")

    nights_str = format_nights_line(journey)
    nights_line = f"\n    {nights_str}" if nights_str else ""
    return (
        f"<b>{index}.</b> {chain}\n"
        f"    {total_line} · {' · '.join(details)}{nights_line}"
    )


def format_results(
    origin: str,
    destination: str,
    date: str,
    direct_flights: List[Dict[str, Any]],
    mm_result: Optional[Dict[str, Any]],
    limit: int = 3,
) -> str:
    """Build the Telegram HTML result message (always under 4000 characters)."""
    lines: List[str] = [
        f"🔎 <b>{_e(origin)} ➔ {_e(destination)}</b> · {_e(_display_date(date))}",
        "",
    ]

    # Cheapest direct flight
    candidates = [f for f in (direct_flights or []) if isinstance(f, dict)]
    cheapest = min(candidates, key=lambda f: _price(f.get("total_price_try")), default=None)
    if cheapest is None and mm_result:
        cheapest = mm_result.get("cheapest_direct_flight") or None
    direct_price: Optional[float] = None
    if cheapest is not None:
        direct_price = _price(cheapest.get("total_price_try"))
        if direct_price == float("inf"):
            direct_price = None
        stops = "" if cheapest.get("is_direct", True) else " (aktarmalı)"
        lines.append(
            f"✈️ <b>En ucuz direkt:</b> {_e(cheapest.get('airline'))}{stops} · "
            f"{_e(_flight_times(cheapest))} · <b>{_money(cheapest.get('total_price_try'))}</b>"
        )
        if cheapest.get("duration"):
            lines.append(f"    ⏱ {_e(cheapest.get('duration'))}")
        url = cheapest.get("google_flights_url")
        if isinstance(url, str) and url.startswith("https://"):
            lines.append(f'    🔗 <a href="{html.escape(url, quote=True)}">Google Flights</a>')
    else:
        lines.append("✈️ Direkt uçuş bulunamadı.")

    # Side routes
    lines.append("")
    if mm_result is None:
        lines.append("🚆 Yan rota araması tamamlanamadı.")
    else:
        routes = [r for r in (mm_result.get("multimodal_routes") or []) if isinstance(r, dict)]
        if not routes:
            lines.append("🚆 Yan rota bulunamadı.")
        else:
            lines.append("🚆 <b>Yan rota önerileri</b>")
            lines.append("")
            shown = 0
            footer_len = 200  # room for the note and the "more routes" line
            body_len = sum(len(l) + 1 for l in lines)
            for journey in routes[: max(0, limit)]:
                block = _route_block(shown + 1, journey, direct_price)
                if body_len + len(block) + footer_len > MAX_MESSAGE_CHARS:
                    break
                lines.append(block)
                lines.append("")
                body_len += len(block) + 2
                shown += 1
            remaining = len(routes) - shown
            if remaining > 0:
                lines.append(f"… {remaining} rota daha (mesaj kısaltıldı)")

    lines.append("")
    lines.append(
        "ℹ️ <i>Her segment ayrı bilet olarak alınır. Bir aksama olursa bağlantı "
        "kaçabilir; aktarma sürelerini kendiniz kontrol edin.</i>"
    )
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1].rsplit("\n", 1)[0] + "\n…"
    return text


def _fit(text: str) -> str:
    """Keep a message under Telegram's limit, cutting at a line boundary."""
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[: MAX_MESSAGE_CHARS - 1].rsplit("\n", 1)[0] + "\n…"


def _dates_text(dates: List[str]) -> str:
    if not dates:
        return "?"
    if len(dates) == 1:
        return _display_date(dates[0])
    return f"{_display_date(dates[0])}–{_display_date(dates[-1])}"


def _display_datetime(value: Any) -> str:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M").strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return str(value)


def _iso_day(value: Any) -> Optional[date]:
    try:
        return datetime.strptime(str(value or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _hhmm(value: Any) -> str:
    """"2026-10-28T23:50" -> "23:50" ("" when unparsable)."""
    text = str(value or "")
    return text[11:16] if len(text) >= 16 and text[10:11] == "T" else ""


def _via_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v]


RISK_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🔴"}
RISK_LABEL = {"low": "düşük risk", "medium": "orta risk", "high": "yüksek risk"}
ADVICE_LABEL = {"al": "şimdi al", "bekle": "bekle", "belirsiz": "belirsiz"}
# /gezi keywords that map to TripPlanner.plan_trip keyword arguments
_TRIP_OPTION_KEYS = ("max_stops", "flight_search_budget", "cabin_bag", "checked_bag", "flex_days",
                     "return_dates", "return_arrive_by", "real_data_only", "schengen_single_entry")


def _planner_options(query: Dict[str, Any]) -> Dict[str, Any]:
    """The route-engine keyword arguments present in a parsed /gezi query (or a stored trip_query)."""
    return {key: query[key] for key in _TRIP_OPTION_KEYS if key in query}


def _risk_text(risk: Any) -> str:
    """"🟢 düşük risk" for an option's journey risk; "" when the option has none."""
    if not isinstance(risk, dict):
        return ""
    level = str(risk.get("level") or "")
    if level not in RISK_EMOJI:
        return ""
    return f"{RISK_EMOJI[level]} {RISK_LABEL[level]}"


# Canonical leg keys for the real station names; the older from_/to_ keys are read as a fallback.
_STATION_KEYS = {
    "from": ("origin_station_name", "from_station_name"),
    "to": ("dest_station_name", "to_station_name"),
}


def _station(leg: Dict[str, Any], side: str) -> str:
    """Real station name of a leg ("" when unknown). side is "from" or "to"."""
    for key in _STATION_KEYS[side]:
        text = str(leg.get(key) or "").strip()
        if text:
            return text
    return ""


GROUND_SOURCE_TAG = {
    "live": "(RegioJet)",
    "regiojet": "(RegioJet)",
    "flixbus": "(FlixBus)",
    "table": "(tahmini)",
    "estimate": "(tahmini)",
}


def _ground_source_tag(leg: Dict[str, Any]) -> str:
    """Short source tag for a ground leg ("(RegioJet)", "(FlixBus)", "(tahmini)"); "" for flights and unknown sources."""
    if leg.get("type") in ("flight", "transfer"):
        return ""
    return GROUND_SOURCE_TAG.get(str(leg.get("source") or "").strip().lower(), "")


UNKNOWN_PART_LABEL = {"baggage": "bagaj", "transfers": "transfer", "hotel": "otel"}


def _leg_price_tag(leg: Dict[str, Any]) -> str:
    """"fiyat yok" for a ground leg whose price is unknown, "X ₺'den başlayan" when a from-price is known;
    "" for a leg with a price (or a flight / transfer, which are never shown this way)."""
    if leg.get("type") in ("flight", "transfer") or leg.get("price_try") is not None:
        return ""
    from_price = leg.get("price_from_try")
    if from_price is not None and _price(from_price) != float("inf"):
        return f"{_e(_money(from_price))}'den başlayan"
    return "fiyat yok"


def _total_text(option: Dict[str, Any]) -> str:
    """The option total line: "💰 <b>X ₺</b> gerçek toplam" when every price is known, otherwise
    "💰 <b>≥ X ₺</b> (+N bacak fiyatı bilinmiyor · transfer fiyatı yok)" (X is a lower bound)."""
    total = option.get("true_total_try")
    if option.get("price_complete") is not False:
        return f"💰 <b>{_money(total)}</b> gerçek toplam"
    bits: List[str] = []
    try:
        unknown_legs = int(option.get("unknown_price_legs") or 0)
    except (TypeError, ValueError):
        unknown_legs = 0
    if unknown_legs > 0:
        bits.append(f"+{unknown_legs} bacak fiyatı bilinmiyor")
    for part in option.get("unknown_price_parts") or []:
        label = UNKNOWN_PART_LABEL.get(str(part))
        if label:
            bits.append(f"{label} fiyatı yok")
    suffix = f" ({' · '.join(bits)})" if bits else ""
    return f"💰 <b>≥ {_money(total)}</b>{_e(suffix, 200)}"


def _option_block(index: Optional[int], option: Dict[str, Any], direct_price: Optional[float]) -> str:
    """One planner option (see TripPlanner.plan_trip) as a Telegram block."""
    chain_parts = []
    for leg in option.get("legs") or []:
        if not isinstance(leg, dict) or leg.get("type") == "transfer":
            continue
        segment = _segment_text({
            "type": leg.get("type"),
            "operator": leg.get("operator"),
            "from": leg.get("from"),
            "to": leg.get("to"),
            "departure_time": _hhmm(leg.get("depart_at")),
            "arrival_time": _hhmm(leg.get("arrive_at")),
            "vehicle_types": leg.get("vehicle_types"),
        })
        from_station, to_station = _station(leg, "from"), _station(leg, "to")
        if (from_station or to_station) and leg.get("type") != "flight":
            # Real stations where the traveller boards and alights, e.g. "🚆 Wien Hbf → Praha hl.n. 10:00–12:30"
            if _is_mixed_vehicle(leg):
                icon = "🚌+🚆 otobüs + tren"
            else:
                icon = "🚆" if leg.get("type") == "train" else "🚌" if leg.get("type") == "bus" else "🚐"
            segment = (f"{icon} {_e(from_station or leg.get('from'), 60)} → {_e(to_station or leg.get('to'), 60)} "
                       f"{_e(_hhmm(leg.get('depart_at')))}–{_e(_hhmm(leg.get('arrive_at')))}")
        source_tag = _ground_source_tag(leg)
        if source_tag:
            segment = f"{segment} {source_tag}"
        price_tag = _leg_price_tag(leg)
        if price_tag:
            segment = f"{segment} · {price_tag}"
        if leg.get("is_last_service"):
            segment = f"{segment} · son sefer"
        next_dep = leg.get("next_departure_at")
        if next_dep:
            hm = _hhmm(next_dep) or _e(str(next_dep).strip(), 20)
            if hm:
                segment = f"{segment} · sonraki {hm}"
        chain_parts.append(segment)
    chain = " · ".join(chain_parts) or "—"

    total = option.get("true_total_try")
    complete = option.get("price_complete") is not False
    total_line = _total_text(option)
    if complete and direct_price is not None:
        # A saving is claimed only against a complete total (an incomplete one is a lower bound)
        try:
            savings = direct_price - float(total)
        except (TypeError, ValueError):
            savings = 0.0
        if savings > 0:
            total_line += f" (direkte göre <code>+{savings:,.0f} ₺</code> tasarruf)"

    details = []
    risk = _risk_text(option.get("risk"))
    if risk:
        details.append(risk)
    details.append(f"⏱ {_duration(option.get('duration_mins'))}")
    depart_day = _iso_day(option.get("depart_at"))
    arrive_day = _iso_day(option.get("arrive_at"))
    if depart_day and arrive_day and arrive_day > depart_day:
        details.append(f"+{(arrive_day - depart_day).days} gün")
    if option.get("via_stop"):
        details.append(f"🛬 {_e(option.get('via_stop'), 10)} üzerinden")
    stopover = option.get("stopover")
    stopover_place = None
    if isinstance(stopover, dict) and stopover.get("place"):
        stopover_place = stopover.get("place")
        details.append(f"⏸ {_e(stopover_place)} {_duration(stopover.get('mins'))} kalış")
    opt_legs = [l for l in (option.get("legs") or []) if isinstance(l, dict)]
    for i in range(len(opt_legs) - 1):
        lnote = _city_layover_note(opt_legs[i], opt_legs[i + 1], stopover_place)
        if lnote:
            details.append(f"🏙️ {lnote}")
    if option.get("night_wait"):
        details.append("🌙 gece beklemesi")
    if option.get("separate_tickets"):
        details.append("⚠️ ayrı bilet")
    if option.get("has_estimated_segments"):
        details.append("⚠️ tahmini")

    nights_str = format_nights_line(option)
    nights_line = f"\n    {nights_str}" if nights_str else ""
    prefix = f"<b>{index}.</b> " if index is not None else ""
    return f"{prefix}{chain}\n    {total_line} · {' · '.join(details)}{nights_line}"


def format_trip_results(query: Dict[str, Any], result: Dict[str, Any], limit: int = 3) -> str:
    """Telegram HTML for a /gezi answer: best direct, best side route and the top options."""
    dates = list(query.get("depart_dates") or [])
    lines: List[str] = [
        f"🗺 <b>{_e(query.get('origin'))} ➔ {_e(query.get('destination'))}</b> · {_e(_dates_text(dates))}",
    ]
    if query.get("arrive_by"):
        lines.append(f"🎯 en geç {_e(_display_datetime(query.get('arrive_by')))}")
    via = _via_list(query.get("via"))
    if via:
        lines.append(f"🌍 üzerinden {_e(', '.join(via))}")
    lines.extend(_trip_rule_lines(query))
    lines.append("")

    cheapest_direct = result.get("cheapest_direct")
    direct_price: Optional[float] = None
    if isinstance(cheapest_direct, dict):
        try:
            direct_price = float(cheapest_direct.get("true_total_try"))
        except (TypeError, ValueError):
            direct_price = None

    best_direct = result.get("best_direct")
    if isinstance(best_direct, dict):
        lines.append("✈️ <b>En iyi direkt</b>")
        lines.append(_option_block(None, best_direct, None))
    else:
        lines.append("✈️ Direkt seçenek bulunamadı.")
    lines.append("")

    best_side = result.get("best_side_route")
    if isinstance(best_side, dict):
        lines.append("🚆 <b>En iyi yan rota</b>")
        lines.append(_option_block(None, best_side, direct_price))
    else:
        lines.append("🚆 Yan rota seçeneği bulunamadı.")

    options = [o for o in (result.get("options") or []) if isinstance(o, dict)]
    if options:
        lines.append("")
        lines.append("<b>Öneriler</b>")
        for number, option in enumerate(options[: max(0, limit)], 1):
            lines.append(_option_block(number, option, direct_price))
            lines.append("")
    else:
        lines.append("")
        lines.append("Kurallara uyan seçenek bulunamadı.")

    near_misses = [nm for nm in (result.get("near_misses") or []) if isinstance(nm, dict)]
    if near_misses:
        lines.append("")
        lines.append("<b>Kurala kıl payı uymayanlar</b>")
        comp_prices = [
            float(o.get("true_total_try"))
            for o in options
            if o.get("price_complete") is not False and o.get("true_total_try") is not None
        ]
        cheapest_comp = min(comp_prices) if comp_prices else None

        for nm in near_misses[:3]:
            late_mins = int(nm.get("late_by_mins") or 0)
            lh, lm = divmod(late_mins, 60)
            late_str = f"{lh} saat {lm} dk" if lm else f"{lh} saat"
            diff = None
            if cheapest_comp is not None and nm.get("true_total_try") is not None:
                try:
                    diff = cheapest_comp - float(nm["true_total_try"])
                except (TypeError, ValueError):
                    pass
            diff_str = f" · {diff:,.0f} TL daha ucuz" if diff is not None and diff > 0 else ""
            nm_chain = " · ".join(
                _segment_text({
                    "type": l.get("type"),
                    "operator": l.get("operator"),
                    "from": l.get("from"),
                    "to": l.get("to"),
                    "departure_time": _hhmm(l.get("depart_at")),
                    "arrival_time": _hhmm(l.get("arrive_at")),
                    "vehicle_types": l.get("vehicle_types"),
                })
                for l in (nm.get("legs") or [])
                if isinstance(l, dict) and l.get("type") != "transfer"
            ) or "—"
            lines.append(f"• {late_str} geç varıyor{diff_str} · <b>{_money(nm.get('true_total_try'))}</b> · {nm_chain}")

    lines.extend(_trip_extra_lines(query, result))

    for warning in [str(w) for w in (result.get("warnings") or [])][:5]:
        lines.append(f"⚠️ {_e(warning, 200)}")
    lines.append("ℹ️ <i>Ayrı biletli seçeneklerde bağlantı kaçabilir; aktarma sürelerini kendiniz kontrol edin.</i>")
    return _fit("\n".join(lines))


_TUR_MONTHS = {
    1: "Oca", 2: "Şub", 3: "Mar", 4: "Nis", 5: "May", 6: "Haz",
    7: "Tem", 8: "Ağu", 9: "Eyl", 10: "Eki", 11: "Kas", 12: "Ara",
}


def _tur_money(amount: Any) -> str:
    try:
        val = int(round(float(amount)))
        return f"{val:,} TL".replace(",", ".")
    except (TypeError, ValueError):
        return f"{amount} TL"


def _tur_datetime(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text[:19], fmt)
            hm = dt.strftime("%H:%M")
            return f"{dt.day} {_TUR_MONTHS.get(dt.month, '')} {hm}"
        except ValueError:
            pass
    return text


def format_tur_results(query: Dict[str, Any], result: Dict[str, Any]) -> str:
    """Format tour exploration results for Telegram (PLN-15a)."""
    origin = query.get("origin", "")
    destination = query.get("destination", "")
    depart_dates = query.get("depart_dates") or []
    dates_text = _dates_text(depart_dates) if depart_dates else ""

    lines = [f"🗺 <b>Tur keşfi: {_e(origin)} ➔ {_e(destination)}</b> ({dates_text})"]

    baseline = result.get("baseline")
    if baseline:
        b_modes: List[str] = []
        for leg in baseline.get("legs") or []:
            t = leg.get("type")
            if t == "flight" and "✈️" not in b_modes:
                b_modes.append("✈️")
            elif t == "bus" and "🚌" not in b_modes:
                b_modes.append("🚌")
            elif t == "train" and "🚆" not in b_modes:
                b_modes.append("🚆")
        b_mode_str = f" · {' '.join(b_modes)}" if b_modes else ""
        b_arrive = f" · {_tur_datetime(baseline.get('arrive_at'))}" if baseline.get("arrive_at") else ""
        lines.append(f"Taban: {_tur_money(baseline.get('true_total_try'))}{b_arrive}{b_mode_str}")
    else:
        lines.append("Taban: Seçenek bulunamadı")

    lines.append("")

    rows = result.get("rows") or []
    if not rows:
        lines.append("Uygun ara durak seçeneği bulunamadı.")
    else:
        for row in rows[:6]:
            cand = row.get("candidate", "")
            price = _tur_money(row.get("true_total_try"))
            diff = row.get("diff_vs_baseline_try")
            if diff is not None:
                if diff > 0:
                    diff_str = f" (+{int(round(diff)):,})".replace(",", ".")
                elif diff < 0:
                    diff_str = f" (-{int(round(abs(diff))):,})".replace(",", ".")
                else:
                    diff_str = " (+0)"
            else:
                diff_str = ""

            arr_str = f" · {_tur_datetime(row.get('arrive_at'))}" if row.get("arrive_at") else ""

            night_items = row.get("nights") or []
            stays = [n["place"] for n in night_items if n.get("type") == "stay" and n.get("place")]
            if stays:
                night_str = f" · 🛏 {', '.join(dict.fromkeys(stays))}"
            elif any(n.get("type") == "transit" for n in night_items):
                night_str = " · 🌙 transit"
            else:
                night_str = ""

            row_modes: List[str] = []
            for leg in row.get("legs") or []:
                t = leg.get("type")
                if t == "flight" and "✈️" not in row_modes:
                    row_modes.append("✈️")
                elif t == "bus" and "🚌" not in row_modes:
                    row_modes.append("🚌")
                elif t == "train" and "🚆" not in row_modes:
                    row_modes.append("🚆")
            modes_str = f" · {' '.join(row_modes)}" if row_modes else ""

            lines.append(f"{cand}: {price}{diff_str}{arr_str}{night_str}{modes_str}")

    skipped = result.get("skipped") or []
    if skipped:
        lines.append("")
        cands_skipped = ", ".join(s["candidate"] for s in skipped if s.get("candidate"))
        lines.append(f"<i>Bulunamayanlar: {cands_skipped}</i>")

    return _fit("\n".join(lines))

def _trip_rule_lines(query: Dict[str, Any]) -> List[str]:
    """Header lines for the optional /gezi rules the user asked for (only those present)."""
    lines: List[str] = []
    rules = []
    if query.get("max_stops") is not None:
        rules.append(f"durak {_e(query.get('max_stops'), 4)}")
    if query.get("flight_search_budget") is not None:
        rules.append(f"bütçe {_e(query.get('flight_search_budget'), 4)}")
    if query.get("flex_days"):
        rules.append(f"esnek ±{_e(query.get('flex_days'), 4)} gün")
    if query.get("cabin_bag") is False:
        rules.append("bagajsız")
    if query.get("checked_bag"):
        rules.append("valiz")
    if query.get("real_data_only"):
        rules.append("yalnız gerçek veriler")
    if rules:
        lines.append("⚙️ " + " · ".join(rules))
    if query.get("return_dates"):
        text = f"🔁 dönüş {_e(_dates_text(list(query['return_dates'])))}"
        if query.get("return_arrive_by"):
            text += f" · en geç {_e(_display_datetime(query['return_arrive_by']))}"
        lines.append(text)
    return lines


def _trip_extra_lines(query: Dict[str, Any], result: Dict[str, Any]) -> List[str]:
    """Flexible-date summary, round-trip combinations, price advice and the budget note."""
    lines: List[str] = []

    flex = result.get("flex")
    if isinstance(flex, dict) and flex.get("best_date"):
        best_text = f"📅 En ucuz tarih: <b>{_display_date(flex['best_date'])}</b> ({_money(flex.get('best_price'))})"
        saving = flex.get("saving_vs_requested")
        if isinstance(saving, (int, float)) and saving > 0:
            best_text += f" · istenen tarihe göre <code>{saving:,.0f} ₺</code> tasarruf"
        lines.append("")
        lines.append(best_text)

    roundtrip = result.get("roundtrip")
    if isinstance(roundtrip, dict):
        combos = [c for c in (roundtrip.get("combos") or []) if isinstance(c, dict)]
        lines.append("")
        if combos:
            lines.append("🔁 <b>Gidiş-dönüş kombinasyonları</b>")
            for number, combo in enumerate(combos[:3], 1):
                stay = combo.get("stay_hours")
                stay_text = f" · kalış {float(stay):.0f} sa" if isinstance(stay, (int, float)) else ""
                lines.append(f"<b>{number}.</b> <b>{_money(combo.get('total_try'))}</b>{stay_text}")
                lines.append(f"    {_e(combo.get('label'), 120)}")
        else:
            lines.append("🔁 Gidiş-dönüş için kurallara uyan kombinasyon bulunamadı.")

    advice = result.get("advice")
    if isinstance(advice, dict) and advice.get("advice"):
        label = ADVICE_LABEL.get(str(advice.get("advice")), str(advice.get("advice")))
        reasons = [str(r) for r in (advice.get("reasons") or []) if r]
        text = f"💡 Fiyat tavsiyesi: <b>{_e(label)}</b>"
        if advice.get("confidence"):
            text += f" (güven: {_e(advice.get('confidence'))})"
        if reasons:
            text += f" · {_e(reasons[0], 160)}"
        lines.append("")
        lines.append(text)

    if result.get("budget_exhausted"):
        budget = (result.get("query") or {}).get("flight_search_budget")
        lines.append("")
        if isinstance(budget, (int, float)) and budget >= 60:
            lines.append("⚠️ Arama bütçesi en fazla (60) doldu; daha fazla aday var. "
                         "Aramayı daraltmak için <code>durak 1</code> veya bir <code>en geç</code> saati ekleyin.")
        else:
            deeper = 40 if not isinstance(budget, (int, float)) or budget < 40 else 60
            lines.append(
                "⚠️ Arama bütçesi doldu; daha fazla aday var. Derin arama için aynı komuta "
                f"<code>bütçe {deeper}</code> ekleyin (en fazla 60)."
            )
    return lines


def _fmt_timestamp(value: Any) -> str:
    """Local "dd.mm.yyyy HH:MM" for a datetime or ISO string."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return _e(value)
    if not isinstance(value, datetime):
        return "bilinmiyor"
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%d.%m.%Y %H:%M")


def format_watch(number: int, watch: Any) -> str:
    """Numbered listing entry for a TripWatch (or a plain price watch)."""
    query = dict(getattr(watch, "trip_query", None) or {})
    dates = list(query.get("depart_dates") or ([watch.departure_date] if watch.departure_date else []))
    lines = [f"<b>{number}.</b> {_e(watch.origin)} ➔ {_e(watch.destination)} · {_e(_dates_text(dates))}"]

    rules = []
    if query.get("arrive_by"):
        rules.append(f"en geç {_e(_display_datetime(query['arrive_by']))}")
    if query.get("depart_after"):
        rules.append(f"en erken {_e(_display_datetime(query['depart_after']))}")
    via = _via_list(query.get("via"))
    if via:
        rules.append(f"üzerinden {_e(', '.join(via))}")
    if query.get("return_dates"):
        rules.append(f"dönüş {_e(_dates_text(list(query['return_dates'])))}")
    if query.get("flex_days"):
        rules.append(f"esnek ±{_e(query['flex_days'], 4)} gün")
    if rules:
        lines.append("    🎯 " + " · ".join(rules))

    budgets = []
    if watch.direct_budget_try is not None:
        budgets.append(f"direkt ≤ {_money(watch.direct_budget_try)}")
    if watch.direct_budget_try is None or watch.target_budget_try != watch.direct_budget_try:
        label = "yan yol" if watch.direct_budget_try is not None else "bütçe"
        budgets.append(f"{label} ≤ {_money(watch.target_budget_try)}")
    latest = watch.history[-1].price_try if watch.history else None
    latest_text = _money(latest) if latest is not None else "henüz yok"
    lines.append(f"    💰 {' · '.join(budgets)} · son fiyat: {latest_text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class TelegramCommandBot:
    """Long-polling Telegram bot that answers flight searches from the owner's chat."""

    def __init__(
        self,
        notifier: Any,
        flight_provider: Any,
        router: Any,
        poll_timeout: int = 25,
        tracker: Any = None,
        trip_planner: Any = None,
        health_provider: Any = None,
    ):
        self.notifier = notifier
        self.flight_provider = flight_provider
        self.router = router
        self.tracker = tracker                  # PriceTracker: watches for /takip, /liste, /sil
        self.trip_planner = trip_planner        # TripPlanner: /gezi
        self.health_provider = health_provider  # optional; health_status() -> dict
        self.poll_timeout = int(poll_timeout)
        self._offset: Optional[int] = None
        self._search_lock = threading.Lock()  # held while a search runs ("busy flag")
        self._queries_lock = threading.Lock()
        self._last_queries: Dict[str, Dict[str, Any]] = {}  # chat id -> last /ara or /gezi query
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- state helpers -----------------------------------------------------

    @property
    def is_busy(self) -> bool:
        return self._search_lock.locked()

    def _config(self) -> Dict[str, Any]:
        return getattr(self.notifier, "config", {}) or {}

    def _owner_chat_id(self) -> str:
        return str(self._config().get("telegram_chat_id", "") or "").strip()

    def _is_configured(self) -> bool:
        cfg = self._config()
        return bool(
            cfg.get("notifications_enabled")
            and str(cfg.get("telegram_bot_token", "") or "").strip()
            and self._owner_chat_id()
        )

    def _reply(self, text: str) -> bool:
        ok, err = self.notifier._post_telegram(text, disable_preview=True)
        if not ok:
            logger.warning("Telegram reply failed: %s", err)
        return bool(ok)

    # -- command handling --------------------------------------------------

    def handle_update(self, update: Dict[str, Any]) -> None:
        """Process one Telegram update. Messages from any chat other than the owner are ignored."""
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            return

        chat = message.get("chat") or {}
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        owner = self._owner_chat_id()
        if chat_id is None or not owner or str(chat_id) != owner:
            return  # silently ignore everyone else

        text = message.get("text")
        if not isinstance(text, str):
            return

        # Skip commands queued while the server was off, so a restart doesn't fire stale searches
        sent_at = message.get("date")
        if isinstance(sent_at, (int, float)) and time.time() - sent_at > MAX_MESSAGE_AGE_SECS:
            logger.info("Ignoring stale Telegram message (%.0fs old)", time.time() - sent_at)
            return

        chat_key = str(chat_id)
        try:
            command = parse_search_command(text, today=date.today())
            if command is None:
                if self._handle_extended_command(text, chat_key):
                    return
                if text.strip().startswith("/"):
                    self._reply("Komut anlaşılamadı. Örnek: /ara Atina Prag 28.10 — yardım için /yardim")
                return
            if command.get("help"):
                self._reply(HELP_TEXT)
                return
            self._remember_query(chat_key, {
                "origin": command["origin"],
                "destination": command["destination"],
                "depart_dates": [command["date"]],
                "arrive_by": None,
                "via": None,
                "schengen_single_entry": command.get("schengen_single_entry", False),
            })
            self._run_search_command(command)
        except Exception:
            logger.exception("Telegram command failed")
            self._reply("⚠️ Komut işlenirken bir hata oluştu. Lütfen tekrar deneyin.")

    def _run_search_command(self, command: Dict[str, Any]) -> None:
        if not self._search_lock.acquire(blocking=False):
            self._reply("Önceki arama sürüyor, lütfen bekleyin.")
            return
        try:
            origin = command["origin"]
            destination = command["destination"]
            travel_date = command["date"]
            self._reply(
                f"🔎 Aranıyor: {_e(origin)} ➔ {_e(destination)} "
                f"({_e(_display_date(travel_date))})… ~1 dk sürebilir"
            )

            direct_flights = self.flight_provider.search_flights(
                origin=origin,
                destination=destination,
                departure_date=travel_date,
                trip_type="oneway",
            ) or []

            try:
                mm_result = self.router.search_multimodal(
                    origin, destination, travel_date,
                    prefetched_direct_flights=direct_flights,
                )
            except Exception:
                logger.exception("Multimodal search failed; sending direct results only")
                mm_result = None

            self._reply(format_results(origin, destination, travel_date, direct_flights, mm_result))
        finally:
            self._search_lock.release()

    # -- trip, watch and status commands -------------------------------------

    def _remember_query(self, chat_key: str, query: Dict[str, Any]) -> None:
        with self._queries_lock:
            self._last_queries[chat_key] = query

    def _last_query(self, chat_key: str) -> Optional[Dict[str, Any]]:
        with self._queries_lock:
            return self._last_queries.get(chat_key)

    def _active_watches(self) -> List[Any]:
        """Active watches in insertion order. /liste numbers and /sil take these positions."""
        if self.tracker is None:
            return []
        with self.tracker._lock:
            return [w for w in self.tracker.watches.values() if w.active]

    def _watch_number(self, watch: Any) -> int:
        for index, candidate in enumerate(self._active_watches(), 1):
            if candidate is watch:
                return index
        return 0

    def _handle_extended_command(self, text: str, chat_key: str) -> bool:
        """Run /gezi, /takip, /liste, /sil or /durum. Returns False for any other text."""
        name, _ = _split_command(text)
        if name not in _EXTENDED_COMMANDS:
            return False
        if name == "/gezi":
            self._run_gezi(text, chat_key)
        elif name == "/takip":
            self._cmd_takip(text, chat_key)
        elif name == "/liste":
            self._cmd_liste()
        elif name == "/sil":
            self._cmd_sil(text)
        elif name == "/tur":
            self._run_tur(text, chat_key)
        else:
            self._cmd_durum()
        return True

    def _run_gezi(self, text: str, chat_key: str) -> None:
        query = parse_gezi_command(text, today=date.today())
        if query is None:
            return
        if "error" in query:
            self._reply(html.escape(query["error"]))
            return
        if self.trip_planner is None:
            self._reply("Gezi planlayıcısı kullanılamıyor.")
            return
        self._remember_query(chat_key, query)  # /takip can use it even if this search fails
        if not self._search_lock.acquire(blocking=False):
            self._reply("Önceki arama sürüyor, lütfen bekleyin.")
            return
        try:
            self._reply(
                f"🔎 Planlanıyor: {_e(query['origin'])} ➔ {_e(query['destination'])} "
                f"({_e(_dates_text(query['depart_dates']))})… birkaç dakika sürebilir"
            )
            result = self.trip_planner.plan_trip(
                query["origin"],
                query["destination"],
                query["depart_dates"],
                arrive_by=query["arrive_by"],
                via=query["via"],
                include_side_routes=True,
                **_planner_options(query),
            ) or {}
            self._reply(format_trip_results(query, result))
        finally:
            self._search_lock.release()

    def _run_tur(self, text: str, chat_key: str) -> None:
        query = parse_tur_command(text, today=date.today())
        if query is None:
            return
        if "error" in query:
            self._reply(html.escape(query["error"]))
            return
        if self.trip_planner is None:
            self._reply("Gezi planlayıcısı kullanılamıyor.")
            return
        if not self._search_lock.acquire(blocking=False):
            self._reply("Önceki arama sürüyor, lütfen bekleyin.")
            return
        try:
            from travel_engine.tour_explore import explore_tour
            self._reply(
                f"🔎 Tur aranıyor: {_e(query['origin'])} ➔ {_e(query['destination'])} "
                f"({_e(_dates_text(query['depart_dates']))})… birkaç dakika sürebilir"
            )
            result = explore_tour(
                planner=self.trip_planner,
                origin=query["origin"],
                destination=query["destination"],
                depart_dates=query["depart_dates"],
                arrive_by=query.get("arrive_by"),
            )
            self._reply(format_tur_results(query, result))
        finally:
            self._search_lock.release()

    def _cmd_takip(self, text: str, chat_key: str) -> None:
        if self.tracker is None:
            self._reply("Takip servisi kullanılamıyor.")
            return
        parsed = parse_takip_command(text)
        if parsed is None:
            return
        if "error" in parsed:
            self._reply(html.escape(parsed["error"]))
            return
        last = self._last_query(chat_key)
        if last is None:
            self._reply("Önce /gezi veya /ara ile bir arama yapın, sonra /takip kullanın.")
            return

        direct = parsed["direct_budget_try"]
        side = parsed["side_budget_try"]
        trip_query = {
            "depart_dates": list(last["depart_dates"]),
            "arrive_by": last.get("arrive_by"),
            "depart_after": None,
            "via": last.get("via"),
            "include_side_routes": True,
        }
        # The watch keeps the whole query: stops, budget, flex dates, return leg and bags
        trip_query.update(_planner_options(last))
        # Without a side-route budget, the direct budget also applies to side routes
        watch = self.tracker.add_watch(
            origin=last["origin"],
            destination=last["destination"],
            target_budget_try=side if side is not None else direct,
            direct_budget_try=direct,
            departure_date=trip_query["depart_dates"][0],
            trip_query=trip_query,
            include_multimodal=True,
            # Explicit id: the default is per route and second, so two quick /takip calls would collide
            watch_id=f"watch_tg_{uuid.uuid4().hex[:12]}",
        )
        number = self._watch_number(watch)
        self._reply(
            "✅ <b>Takip oluşturuldu</b>\n"
            + format_watch(number, watch)
            + "\nNumara /liste ve /sil ile aynıdır."
        )

    def _cmd_liste(self) -> None:
        if self.tracker is None:
            self._reply("Takip servisi kullanılamıyor.")
            return
        active = self._active_watches()
        if not active:
            self._reply("Aktif takip yok. Önce /gezi veya /ara yapıp /takip ile ekleyin.")
            return
        lines = ["📋 <b>Aktif takipler</b>", ""]
        for number, watch in enumerate(active, 1):
            lines.append(format_watch(number, watch))
            lines.append("")
        self._reply(_fit("\n".join(lines)))

    def _cmd_sil(self, text: str) -> None:
        if self.tracker is None:
            self._reply("Takip servisi kullanılamıyor.")
            return
        parsed = parse_sil_command(text)
        if parsed is None:
            return
        if "error" in parsed:
            self._reply(html.escape(parsed["error"]))
            return
        number = parsed["index"]
        active = self._active_watches()
        if number > len(active):
            self._reply(f"Geçersiz takip numarası: {number}. /liste ile numaraları kontrol edin.")
            return
        watch = active[number - 1]
        if not self.tracker.delete_watch(watch.id):
            self._reply("Bu takip zaten silinmiş.")
            return
        self._reply(f"🗑 Takip {number} silindi: {_e(watch.origin)} ➔ {_e(watch.destination)}")

    def _cmd_durum(self) -> None:
        lines = ["📊 <b>Sunucu durumu</b>", ""]
        readings: List[datetime] = []
        if self.tracker is not None:
            with self.tracker._lock:
                all_watches = list(self.tracker.watches.values())
            active_count = len([w for w in all_watches if w.active])
            lines.append(f"📌 Takip: {active_count} aktif ({len(all_watches)} kayıtlı)")
            for watch in all_watches:
                if watch.history:
                    readings.append(watch.history[-1].timestamp)
        else:
            lines.append("📌 Takip servisi yok")
        lines.append(f"🔎 Arama: {'meşgul' if self.is_busy else 'boşta'}")

        health: Optional[Dict[str, Any]] = None
        health_error = False
        if self.health_provider is not None and callable(getattr(self.health_provider, "health_status", None)):
            try:
                status = self.health_provider.health_status()
                health = status if isinstance(status, dict) else None
            except Exception:
                logger.exception("Health status could not be read")
                health_error = True

        if health_error:
            lines.append("🩺 Sağlık bilgisi alınamadı")
        elif health is None:
            lines.append("🩺 Sağlık bilgisi yok")
        else:
            healthy = bool(health.get("healthy"))
            lines.append(f"🩺 Sağlık: {'sağlıklı' if healthy else 'sorunlu'}")
            if health.get("last_status") is not None:
                lines.append(f"Son durum: {_e(health.get('last_status'))}")
            if health.get("consecutive_failures") is not None:
                lines.append(f"Art arda hata: {_e(health.get('consecutive_failures'))}")

        if health is not None and health.get("last_scan_at"):
            lines.append(f"🕒 Son tarama: {_fmt_timestamp(health.get('last_scan_at'))}")
        elif readings:
            latest = max(readings, key=lambda ts: ts.timestamp())
            lines.append(f"🕒 Son fiyat kaydı: {_fmt_timestamp(latest)}")
        else:
            lines.append("🕒 Son tarama: bilinmiyor")
        self._reply("\n".join(lines))

    # -- polling -----------------------------------------------------------

    def _get_updates(self) -> List[Dict[str, Any]]:
        token = str(self._config().get("telegram_bot_token", "") or "").strip()
        params: Dict[str, Any] = {
            "timeout": self.poll_timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        if self._offset is not None:
            params["offset"] = self._offset
        url = f"{TELEGRAM_API}/bot{token}/getUpdates?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(urllib.request.Request(url), timeout=self.poll_timeout + 10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError("getUpdates returned not ok")
        result = data.get("result") or []
        return [u for u in result if isinstance(u, dict)]

    def _dispatch(self, updates: List[Dict[str, Any]]) -> None:
        """Advance the offset and hand each update to its own worker thread.

        Workers keep polling alive while a slow search runs, so a second command
        gets the "busy" reply instead of waiting in the queue.
        """
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1
            worker = threading.Thread(
                target=self._safe_handle, args=(update,), name="telegram-cmd", daemon=True
            )
            worker.start()

    def _safe_handle(self, update: Dict[str, Any]) -> None:
        try:
            self.handle_update(update)
        except Exception:
            logger.exception("Unhandled error in Telegram update handler")

    def run_forever(self, stop_event: threading.Event) -> None:
        """Poll until stop_event (or stop()) is set."""
        backoff = 5
        while not stop_event.is_set() and not self._stop_event.is_set():
            if not self._is_configured():
                stop_event.wait(30)
                continue
            try:
                updates = self._get_updates()
            except Exception as exc:  # network errors, HTTP errors, bad JSON
                logger.warning("Telegram polling failed (%s); retrying in %ss", type(exc).__name__, backoff)
                stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 5
            if updates:
                self._dispatch(updates)

    def start_in_thread(self) -> threading.Thread:
        """Start run_forever on a daemon thread and return it."""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self.run_forever, args=(self._stop_event,), name="telegram-bot", daemon=True
        )
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        """Ask the polling loop to exit after its current poll."""
        self._stop_event.set()
