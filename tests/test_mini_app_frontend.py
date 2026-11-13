"""Behavior smoke tests for the vanilla Mini App boot path (no browser required)."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is optional for local frontend smoke tests")
class MiniAppFrontendSmokeTests(unittest.TestCase):
    def test_boot_and_network_resilience_contracts(self):
        result = subprocess.run(
            [shutil.which("node") or "node", str(ROOT / "tests" / "mini_app_frontend_smoke.cjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Mini App frontend smoke tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
