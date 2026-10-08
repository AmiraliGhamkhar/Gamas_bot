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

    def test_every_runtime_dependency_is_self_checked_or_explicitly_excluded(self):
        """``gamas_bot --check`` must notice a broken runtime dependency.

        A new dependency added to ``pyproject.toml`` has to land in one of two
        conscious places: the self-check's import list, or the documented
        exclusion list below (packages only reachable from optional paths).
        """
        from gamas_bot.__main__ import CHECK_REQUIRED_DISTRIBUTIONS

        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        declared = {
            # ``python-socks[asyncio]`` carries extras; compare distributions.
            _parse_requirement(item)[0].split("[", 1)[0].lower().replace("_", "-")
            for item in data["project"]["dependencies"]
        }
        # The self-check verifies importability, so its names are import names;
        # map the few that differ from the distribution name.
        import_to_distribution = {
            "dotenv": "python-dotenv",
            "pptx": "python-pptx",
            "docx": "python-docx",
        }
        checked = {
            import_to_distribution.get(name, name).lower().replace("_", "-")
            for name in CHECK_REQUIRED_DISTRIBUTIONS
        }
        # python-socks: only needed with TELEGRAM_PROXY (covered by settings
        # tests). Pillow/arabic-reshaper/python-bidi: only the offline page
        # renderer paints pixels (covered by test_page_renderer).
        excluded = {"python-socks", "pillow", "arabic-reshaper", "python-bidi"}
        self.assertEqual(
            declared - excluded,
            checked,
            "runtime dependency not covered by `gamas_bot --check` and not in "
            "the documented exclusion list (update CHECK_REQUIRED_DISTRIBUTIONS "
            "or the exclusions in both places)",
        )

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
