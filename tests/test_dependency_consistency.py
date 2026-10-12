"""``pyproject.toml`` and ``requirements.txt`` must never drift.

The canonical runtime dependency list is ``[project].dependencies`` in
``pyproject.toml``; ``requirements.txt`` is generated from it by
``python -m scripts.sync_requirements`` (cPanel/Passenger installs from the
generated file). This test fails when the generated file is stale.
"""

from __future__ import annotations

import unittest

from scripts.sync_requirements import REQUIREMENTS, canonical_dependencies, render


class DependencyConsistencyTests(unittest.TestCase):
    def test_requirements_matches_pyproject(self) -> None:
        current = REQUIREMENTS.read_text(encoding="utf-8") if REQUIREMENTS.is_file() else ""
        self.assertEqual(
            render(),
            current,
            "requirements.txt is out of date with pyproject.toml; "
            "run `python -m scripts.sync_requirements`",
        )

    def test_every_runtime_dependency_has_a_version_constraint(self) -> None:
        for spec in canonical_dependencies():
            self.assertRegex(
                spec,
                r"[<>=~!]",
                f"dependency {spec!r} floats without a version constraint",
            )


if __name__ == "__main__":
    unittest.main()
