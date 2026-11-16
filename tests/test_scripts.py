import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
PS1 = os.path.join(SCRIPTS, "autostart_windows.ps1")
BAT = os.path.join(SCRIPTS, "run_server.bat")


class ScriptFilesTests(unittest.TestCase):
    def test_expected_files_exist(self):
        for name in ("autostart_windows.ps1", "run_server.bat", "README.md"):
            with self.subTest(name=name):
                self.assertTrue(os.path.isfile(os.path.join(SCRIPTS, name)))

    def test_batch_file_starts_server_from_repo_root(self):
        with open(BAT, "r", encoding="ascii") as f:
            content = f.read()
        self.assertIn("%~dp0..", content)  # repo root relative to the script
        self.assertIn("python server_v2.py", content)

    def test_powershell_script_has_expected_switches(self):
        with open(PS1, "r", encoding="utf-8-sig") as f:
            content = f.read()
        for switch in ("[switch]$Install", "[switch]$Uninstall", "[switch]$Status", "SupportsShouldProcess"):
            with self.subTest(switch=switch):
                self.assertIn(switch, content)
        self.assertIn('"TravelPlanServer"', content)


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class AutostartDryRunTests(unittest.TestCase):
    def run_ps1(self, *args):
        # Output bytes are decoded leniently: the console codepage may differ from UTF-8
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PS1, *args],
            capture_output=True, timeout=120,
        )
        completed.stdout = completed.stdout.decode("utf-8", errors="replace")
        completed.stderr = completed.stderr.decode("utf-8", errors="replace")
        return completed

    def test_install_whatif_prints_command_and_registers_nothing(self):
        result = self.run_ps1("-Install", "-WhatIf")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TravelPlanServer", result.stdout)
        self.assertIn("server_v2.py", result.stdout)
        self.assertIn("server.log", result.stdout)
        self.assertIn("WhatIf", result.stdout)

    def test_status_runs(self):
        result = self.run_ps1("-Status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TravelPlanServer", result.stdout)

    def test_no_switch_prints_usage(self):
        result = self.run_ps1()
        self.assertEqual(result.returncode, 1)
        self.assertIn("-Install", result.stdout)


if __name__ == "__main__":
    unittest.main()
