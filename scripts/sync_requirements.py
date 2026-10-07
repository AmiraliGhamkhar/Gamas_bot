"""Regenerate ``requirements.txt`` from ``pyproject.toml``.

The canonical runtime dependency list lives in ``[project].dependencies`` of
``pyproject.toml``. Deployment (cPanel/Passenger) and CI install from
``requirements.txt``, so the two must never drift: this script rewrites the
deployment file from the canonical one, and
``tests/test_dependency_consistency.py`` fails when it is stale.

Usage::

    python -m scripts.sync_requirements          # rewrite in place
    python -m scripts.sync_requirements --check   # exit 1 when stale
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"

HEADER = (
    "# Generated from pyproject.toml [project].dependencies.\n"
    "# Do not edit by hand: run `python -m scripts.sync_requirements` after\n"
    "# changing the canonical dependency list. This file exists because a\n"
    "# cPanel/Passenger deployment installs with `pip install -r requirements.txt`.\n"
)


def canonical_dependencies() -> list[str]:
    """Runtime dependency specifiers, in declaration order."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def render() -> str:
    return HEADER + "\n".join(canonical_dependencies()) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when requirements.txt is out of date instead of rewriting it",
    )
    args = parser.parse_args(argv)
    expected = render()
    current = REQUIREMENTS.read_text(encoding="utf-8") if REQUIREMENTS.is_file() else ""
    if expected == current:
        print("requirements.txt is up to date")
        return 0
    if args.check:
        print(
            "requirements.txt is out of date with pyproject.toml; "
            "run `python -m scripts.sync_requirements`",
            file=sys.stderr,
        )
        return 1
    REQUIREMENTS.write_text(expected, encoding="utf-8")
    print(f"requirements.txt regenerated ({len(canonical_dependencies())} dependencies)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
