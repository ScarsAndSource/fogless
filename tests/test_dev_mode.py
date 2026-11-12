"""The no-database local mode (FOGLESS_DEV_MODE=1) and the fail-closed cases. Each scenario needs a fresh
process because the app reads its configuration at import time."""
import os
import subprocess
import sys
import unittest

CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dev_mode_check.py")


class TestDevMode(unittest.TestCase):
    def run_scenario(self, name):
        p = subprocess.run([sys.executable, CHECK, name], capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"ok {name}", p.stdout)

    def test_local_in_memory_accounts_and_isolation(self):
        self.run_scenario("local")

    def test_dev_mode_is_ignored_on_vercel(self):
        self.run_scenario("vercel")

    def test_unconfigured_auth_fails_closed(self):
        self.run_scenario("unconfigured")


if __name__ == "__main__":
    unittest.main()
