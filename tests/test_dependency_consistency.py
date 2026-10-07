"""The canonical dependency list must not drift from the deployment list.

``pyproject.toml`` is the single source of truth; ``requirements.txt`` is the
generated artifact a cPanel/Passenger deployment installs. A silent difference
between them is exactly the kind of dependency drift that makes "works on my
machine" deployments, so it fails the build here instead.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS = ROOT / "requirements.txt"


def _parse_requirement(line: str) -> tuple[str, str]:
    """(normalised name, specifier) for one requirement line."""
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]+\])?)\s*(.*)$", line.strip())
    if not match:
        raise AssertionError(f"unparsable requirement: {line!r}")
    return match.group(1).lower().replace("_", "-"), match.group(2).strip()


class DependencyConsistencyTests(unittest.TestCase):
    def test_requirements_txt_matches_pyproject(self):
        from scripts import sync_requirements

        expected = sync_requirements.render()
        self.assertTrue(REQUIREMENTS.is_file(), "requirements.txt is missing")
        self.assertEqual(
            REQUIREMENTS.read_text(encoding="utf-8"),
            expected,
            "requirements.txt is stale; run `python -m scripts.sync_requirements`",
        )

    def test_runtime_dependencies_are_bounded_and_complete(self):
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        dependencies = data["project"]["dependencies"]
        self.assertTrue(dependencies)
        names = [_parse_requirement(item)[0] for item in dependencies]
        self.assertEqual(len(names), len(set(names)), "duplicate dependency in pyproject.toml")
        for item in dependencies:
            _name, specifier = _parse_requirement(item)
            self.assertTrue(specifier, f"{item!r} is unpinned")
            # Every dependency is pinned with a range (>= lower, < upper) so a
            # deployment cannot silently jump a major version.
            self.assertIn(">=", specifier, item)
            self.assertIn("<", specifier, item)

    def test_python_requirement_matches_the_ci_matrix(self):
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        requires = data["project"]["requires-python"]
        self.assertEqual(requires.replace(" ", ""), ">=3.11")
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        for version in ("3.11", "3.12", "3.13"):
            self.assertIn(f"'{version}'", workflow, f"CI matrix is missing {version}")

    def test_banned_system_binaries_are_not_declared_as_dependencies(self):
        """PyAV/ppt2pptx replaced the ffmpeg/ffprobe/soffice binaries."""
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        declared = " ".join(data["project"]["dependencies"]).lower()
        for banned in ("ffmpeg", "ffprobe", "libreoffice", "soffice"):
            self.assertNotIn(banned, declared)


if __name__ == "__main__":
    unittest.main()
