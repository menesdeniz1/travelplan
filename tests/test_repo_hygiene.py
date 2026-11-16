"""Repo hygiene (GUV-3): no secrets or databases among the files git tracks."""
import os
import re
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_RE = re.compile(rb"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")  # Telegram bot token shape
DB_RE = re.compile(r"\.(db|sqlite|sqlite3)(-(wal|shm|journal))?$")
MAX_SCAN_BYTES = 2_000_000


def _tracked_files():
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
                             check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [path for path in out.decode("utf-8", "replace").split("\0") if path]


class RepoHygieneTests(unittest.TestCase):
    def setUp(self):
        self.files = _tracked_files()
        if self.files is None:
            self.skipTest("git is not available")

    def test_no_env_file_is_tracked(self):
        bad = [p for p in self.files
               if os.path.basename(p) == ".env"
               or (os.path.basename(p).startswith(".env.") and os.path.basename(p) != ".env.example")]
        self.assertEqual(bad, [])

    def test_no_database_file_is_tracked(self):
        self.assertEqual([p for p in self.files if DB_RE.search(p.lower())], [])

    def test_no_telegram_token_in_tracked_files(self):
        hits = []
        for p in self.files:
            path = os.path.join(REPO, p)
            try:
                if os.path.getsize(path) > MAX_SCAN_BYTES:
                    continue
                with open(path, "rb") as handle:
                    if TOKEN_RE.search(handle.read()):
                        hits.append(p)  # the path only, never the matched text
            except OSError:
                continue
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
