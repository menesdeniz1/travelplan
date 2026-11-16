"""Load ``KEY=VALUE`` settings from a ``.env`` file into ``os.environ``.

The server reads its settings from environment variables. This module lets the user keep
them in one file next to the code (copy ``.env.example`` to ``.env``) instead of in shell
profiles or in the launchd / Task Scheduler definitions. Standard library only.

Rules:
- Path: the ``path`` argument if given; else ``TRAVELPLAN_ENV_FILE``; else ``<repo>/.env``.
- A variable that is already set (non-empty) in the process environment is kept, so the
  real environment wins, unless ``override=True``.
- Blank values are ignored, so a template line such as ``TELEGRAM_CHAT_ID=`` changes nothing.
- Values are never logged; only key names and counts are.
- During a test run the default path is not read (``load_env()`` returns ``{}``). An
  explicit ``path`` is always honoured.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Dict, Optional

try:
    from .storage import is_test_run
except ImportError:  # pragma: no cover - storage is part of the package; fallback only
    def is_test_run() -> bool:
        return "pytest" in sys.modules

logger = logging.getLogger(__name__)

ENV_FILE_VAR = "TRAVELPLAN_ENV_FILE"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A comment starts at a "#" that is preceded by whitespace (so "a#b" keeps its "#").
_INLINE_COMMENT_RE = re.compile(r"\s#")
# Escapes inside double quotes. Unknown escapes stay literal, so "C:\Users" survives.
_DOUBLE_QUOTE_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def env_file_path() -> str:
    """The ``.env`` path that ``load_env()`` uses by default."""
    explicit = os.environ.get(ENV_FILE_VAR, "").strip()
    if explicit:
        return os.path.abspath(explicit)
    return os.path.join(REPO_DIR, ".env")


def _parse_value(raw: str) -> Optional[str]:
    """The value part of a line (text after the first ``=``).

    Returns None for a quoted value whose closing quote is missing on the same line.
    Text after a closing quote is ignored.
    """
    value = raw.lstrip()
    if not value:
        return ""
    quote = value[0]
    if quote in ("'", '"'):
        out = []
        i = 1
        while i < len(value):
            ch = value[i]
            if quote == '"' and ch == "\\" and i + 1 < len(value):
                nxt = value[i + 1]
                out.append(_DOUBLE_QUOTE_ESCAPES.get(nxt, "\\" + nxt))
                i += 2
                continue
            if ch == quote:
                return "".join(out)
            out.append(ch)
            i += 1
        return None
    if value.startswith("#"):
        return ""
    return _INLINE_COMMENT_RE.split(value, maxsplit=1)[0].rstrip()


def parse_env_text(text: str) -> Dict[str, str]:
    """Parse ``.env`` content into ``{key: value}``. Later duplicates win.

    Supports blank lines, ``#`` comment lines, an ``export `` prefix, single and double
    quotes, and inline comments after an unquoted value (a ``#`` preceded by whitespace).
    Malformed lines are skipped with a warning that gives the line number only.
    """
    values: Dict[str, str] = {}
    for number, line in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("export ", "export\t")):
            stripped = stripped[len("export"):].lstrip()
        key, sep, raw_value = stripped.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.fullmatch(key):
            logger.warning("env file line %d ignored (not KEY=VALUE)", number)
            continue
        value = _parse_value(raw_value)
        if value is None:
            logger.warning("env file line %d ignored (unclosed quote)", number)
            continue
        values[key] = value
    return values


def _in_test_run() -> bool:
    try:
        return bool(is_test_run())
    except Exception:
        return False


def load_env(path: Optional[str] = None, override: bool = False) -> Dict[str, str]:
    """Load a ``.env`` file into ``os.environ`` and report what was applied.

    ``path`` defaults to ``env_file_path()``. Without an explicit ``path`` and during a test
    run, nothing is read and ``{}`` is returned. A missing or unreadable file also gives
    ``{}``.

    Returns ``{key: "set"}`` for variables that were added and ``{key: "override"}`` for
    variables that replaced an existing value (only when ``override=True``). Keys that were
    already set, and blank values, are not included. The return value never contains the
    variables' values.
    """
    if path is None:
        if _in_test_run():
            return {}
        path = env_file_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        # Only the exception type: the message can contain bytes from the file.
        logger.warning("env file %s could not be read (%s)", path, type(exc).__name__)
        return {}

    applied: Dict[str, str] = {}
    for key, value in parse_env_text(text).items():
        if value == "":
            continue
        already = os.environ.get(key, "")
        if already.strip() and not override:
            continue
        applied[key] = "override" if already.strip() else "set"
        os.environ[key] = value

    logger.info(
        "env file %s: %d variable(s) applied%s",
        path, len(applied), (" (" + ", ".join(sorted(applied)) + ")") if applied else "",
    )
    return applied
