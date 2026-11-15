"""Regression checks for dependency security floors."""

from __future__ import annotations

from pathlib import Path
import tomllib
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class DependencySecurityTests(unittest.TestCase):
    def test_pillow_range_excludes_known_vulnerable_releases(self) -> None:
        """Image parsing handles untrusted uploads, so Pillow must be >= 13.0.0."""
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = project["project"]["dependencies"]
        self.assertIn("Pillow>=13.0,<14.0", dependencies)


if __name__ == "__main__":
    unittest.main()
