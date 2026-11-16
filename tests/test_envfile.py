import logging
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from travel_engine import envfile
from travel_engine.envfile import env_file_path, load_env, parse_env_text

SECRET = "123456:SECRET-TOKEN-DO-NOT-LOG"


class EnvFileTestBase(unittest.TestCase):
    def setUp(self):
        # Every test starts from a copy of the real environment; patch.dict restores it.
        env_patch = patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.tmp_dir = tempfile.mkdtemp(prefix="envfile_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)

    def write_env(self, content, name=".env", encoding="utf-8"):
        path = os.path.join(self.tmp_dir, name)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode, **({} if mode == "wb" else {"encoding": encoding, "newline": ""})) as f:
            f.write(content)
        return path

    def unset(self, *keys):
        for key in keys:
            os.environ.pop(key, None)


class TestParsing(EnvFileTestBase):
    def test_basic_pairs_blank_lines_and_comments(self):
        text = "\n# a comment\nTRAVELPLAN_PORT=8090\n\n   # indented comment\nTRAVELPLAN_HOST=0.0.0.0\n"
        self.assertEqual(parse_env_text(text), {"TRAVELPLAN_PORT": "8090", "TRAVELPLAN_HOST": "0.0.0.0"})

    def test_export_prefix(self):
        self.assertEqual(parse_env_text("export TELEGRAM_CHAT_ID=42\nexport\tX_Y=1\n"),
                         {"TELEGRAM_CHAT_ID": "42", "X_Y": "1"})

    def test_single_quotes_are_literal(self):
        self.assertEqual(parse_env_text(r"A='x \n y \" z'"), {"A": r"x \n y \" z"})

    def test_double_quotes_handle_escapes_and_keep_unknown_ones(self):
        self.assertEqual(parse_env_text(r'A="say \"hi\" \\ done"'), {"A": 'say "hi" \\ done'})
        self.assertEqual(parse_env_text(r'P="C:\Users\user"'), {"P": "C:\\Users\\user"})

    def test_inline_comment_outside_quotes_is_removed(self):
        self.assertEqual(parse_env_text("A=value # note"), {"A": "value"})
        self.assertEqual(parse_env_text("A=value\t# note"), {"A": "value"})

    def test_hash_inside_quotes_or_without_space_is_kept(self):
        self.assertEqual(parse_env_text('A="pa # ss"'), {"A": "pa # ss"})
        self.assertEqual(parse_env_text("A='x#y'"), {"A": "x#y"})
        self.assertEqual(parse_env_text("A=x#y"), {"A": "x#y"})

    def test_text_after_closing_quote_is_ignored(self):
        self.assertEqual(parse_env_text('A="v"  # comment'), {"A": "v"})

    def test_value_starting_with_hash_is_blank(self):
        self.assertEqual(parse_env_text("A=#only a comment"), {"A": ""})

    def test_empty_value_and_spaces_around_equals(self):
        self.assertEqual(parse_env_text("A=\nB =  spaced  \nC= \"\""), {"A": "", "B": "spaced", "C": ""})

    def test_malformed_lines_are_skipped(self):
        text = "no_equals_here\n1BAD=1\nBAD-NAME=2\n=novalue\nGOOD=ok\nOPEN=\"never closed\n"
        with self.assertLogs("travel_engine.envfile", level="WARNING") as cm:
            values = parse_env_text(text)
        self.assertEqual(values, {"GOOD": "ok"})
        self.assertTrue(any("line 6" in line for line in cm.output))

    def test_duplicate_key_last_one_wins(self):
        self.assertEqual(parse_env_text("A=1\nA=2\n"), {"A": "2"})

    def test_crlf_line_endings(self):
        self.assertEqual(parse_env_text("A=1\r\nB=2\r\n"), {"A": "1", "B": "2"})

    def test_bom_is_ignored_in_text(self):
        self.assertEqual(parse_env_text("\ufeffA=1\n"), {"A": "1"})


class TestLoadEnv(EnvFileTestBase):
    def test_missing_file_returns_empty_dict(self):
        missing = os.path.join(self.tmp_dir, "does-not-exist.env")
        self.assertEqual(load_env(missing), {})

    def test_sets_missing_variables_and_reports_keys_only(self):
        path = self.write_env(f"TELEGRAM_BOT_TOKEN={SECRET}\nTELEGRAM_CHAT_ID=987654\n")
        self.unset("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        applied = load_env(path)
        self.assertEqual(applied, {"TELEGRAM_BOT_TOKEN": "set", "TELEGRAM_CHAT_ID": "set"})
        self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], SECRET)
        self.assertEqual(os.environ["TELEGRAM_CHAT_ID"], "987654")

    def test_existing_variable_is_not_overridden_by_default(self):
        path = self.write_env("TRAVELPLAN_PORT=9000\nTRAVELPLAN_HOST=10.0.0.1\n")
        with patch.dict(os.environ, {"TRAVELPLAN_PORT": "8100"}):
            applied = load_env(path)
            self.assertEqual(os.environ["TRAVELPLAN_PORT"], "8100")
            self.assertEqual(os.environ["TRAVELPLAN_HOST"], "10.0.0.1")
        self.assertEqual(applied, {"TRAVELPLAN_HOST": "set"})

    def test_override_true_replaces_existing_variable(self):
        path = self.write_env("TRAVELPLAN_PORT=9000\n")
        with patch.dict(os.environ, {"TRAVELPLAN_PORT": "8100"}):
            applied = load_env(path, override=True)
            self.assertEqual(os.environ["TRAVELPLAN_PORT"], "9000")
        self.assertEqual(applied, {"TRAVELPLAN_PORT": "override"})

    def test_existing_empty_variable_is_filled_from_file(self):
        path = self.write_env("TELEGRAM_CHAT_ID=55\n")
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": ""}):
            self.assertEqual(load_env(path), {"TELEGRAM_CHAT_ID": "set"})
            self.assertEqual(os.environ["TELEGRAM_CHAT_ID"], "55")

    def test_blank_values_are_not_applied(self):
        path = self.write_env("TELEGRAM_BOT_TOKEN=\nTRAVELPLAN_DB=\n")
        self.unset("TELEGRAM_BOT_TOKEN", "TRAVELPLAN_DB")
        self.assertEqual(load_env(path), {})
        self.assertNotIn("TELEGRAM_BOT_TOKEN", os.environ)
        self.assertNotIn("TRAVELPLAN_DB", os.environ)

    def test_bom_file_is_read_correctly(self):
        path = self.write_env("\ufeffTRAVELPLAN_PORT=7777\n".encode("utf-8"), name="bom.env")
        self.unset("TRAVELPLAN_PORT")
        self.assertEqual(load_env(path), {"TRAVELPLAN_PORT": "set"})
        self.assertEqual(os.environ["TRAVELPLAN_PORT"], "7777")

    def test_non_utf8_file_gives_empty_dict_without_raising(self):
        path = self.write_env(b"A=\xff\xfe\x80\n", name="bad.env")
        self.unset("A")
        with self.assertLogs("travel_engine.envfile", level="WARNING"):
            self.assertEqual(load_env(path), {})
        self.assertNotIn("A", os.environ)

    def test_never_returns_or_logs_values(self):
        path = self.write_env(f"TELEGRAM_BOT_TOKEN={SECRET}\n")
        self.unset("TELEGRAM_BOT_TOKEN")
        with self.assertLogs("travel_engine.envfile", level="DEBUG") as cm:
            applied = load_env(path)
        self.assertNotIn(SECRET, repr(applied))
        self.assertTrue(all(isinstance(v, str) and v in ("set", "override") for v in applied.values()))
        self.assertNotIn(SECRET, "\n".join(cm.output))

    def test_never_logs_value_from_malformed_line(self):
        path = self.write_env(f"OPEN=\"{SECRET}\n")
        self.unset("OPEN")
        with self.assertLogs("travel_engine.envfile", level="WARNING") as cm:
            load_env(path)
        self.assertNotIn(SECRET, "\n".join(cm.output))

    def test_telegram_settings_reach_the_notifier(self):
        from travel_engine.notifier import AlertNotifier

        path = self.write_env(f"TELEGRAM_BOT_TOKEN={SECRET}\nTELEGRAM_CHAT_ID=987654\n")
        self.unset("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        load_env(path)
        notifier = AlertNotifier(
            alerts_file=os.path.join(self.tmp_dir, "alerts.json"),
            config_file=os.path.join(self.tmp_dir, "config.json"),
        )
        self.assertEqual(notifier.config["telegram_bot_token"], SECRET)
        self.assertEqual(notifier.config["telegram_chat_id"], "987654")


class TestPathsAndTestRunGuard(EnvFileTestBase):
    def test_default_path_is_repo_root_dot_env(self):
        self.unset(envfile.ENV_FILE_VAR)
        self.assertEqual(env_file_path(), os.path.join(envfile.REPO_DIR, ".env"))
        self.assertEqual(envfile.REPO_DIR, os.path.dirname(os.path.dirname(os.path.abspath(envfile.__file__))))

    def test_env_var_overrides_default_path(self):
        target = os.path.join(self.tmp_dir, "other.env")
        with patch.dict(os.environ, {envfile.ENV_FILE_VAR: target}):
            self.assertEqual(env_file_path(), os.path.abspath(target))

    # The detector is patched here: is_test_run() reads sys.argv[0], which is not a
    # recognisable runner name under "python -m unittest" on this Windows install.
    def test_default_path_is_not_read_during_test_run(self):
        path = self.write_env("TRAVELPLAN_PORT=6543\n")
        self.unset("TRAVELPLAN_PORT")
        with patch.dict(os.environ, {envfile.ENV_FILE_VAR: path}), \
                patch.object(envfile, "is_test_run", return_value=True):
            self.assertEqual(load_env(), {})
            self.assertNotIn("TRAVELPLAN_PORT", os.environ)

    def test_default_path_is_read_when_not_a_test_run(self):
        path = self.write_env("TRAVELPLAN_PORT=6543\n")
        self.unset("TRAVELPLAN_PORT")
        with patch.dict(os.environ, {envfile.ENV_FILE_VAR: path}), \
                patch.object(envfile, "is_test_run", return_value=False):
            self.assertEqual(load_env(), {"TRAVELPLAN_PORT": "set"})
            self.assertEqual(os.environ["TRAVELPLAN_PORT"], "6543")

    def test_explicit_path_is_honoured_during_test_run(self):
        path = self.write_env("TRAVELPLAN_PORT=6544\n")
        self.unset("TRAVELPLAN_PORT")
        self.assertEqual(load_env(path), {"TRAVELPLAN_PORT": "set"})
        self.assertEqual(os.environ["TRAVELPLAN_PORT"], "6544")


if __name__ == "__main__":
    unittest.main()
