"""``.env.example`` and ``config.py`` must document the same environment keys.

``.env.example`` is the operator-facing reference; ``config.py`` is the only
module that reads ``os.environ``. A setting that exists in one and not the
other is drift: an undocumented knob, or documentation of a knob that does
nothing. This test pins the two sets to exact equality.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
CONFIG_PY = PROJECT_ROOT / "gamas_bot" / "config.py"

#: ``_env("NAME")``, ``_env_int("NAME", …)`` and friends.
_ENV_CALL = re.compile(r'_env(?:_int|_float|_bool)?\(\s*"([A-Z0-9_]+)"')
#: Plan overrides declared as ``("PLAN_X", "plan_x", default)``.
_PLAN_FIELD = re.compile(r'\(\s*"([A-Z0-9_]+)"\s*,\s*"[a-z0-9_]+"\s*,')
#: Documented keys: ``KEY=value`` or commented ``#KEY=value`` samples.
_ENV_DOC = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=")


def config_env_keys() -> set[str]:
    source = CONFIG_PY.read_text(encoding="utf-8")
    keys = set(_ENV_CALL.findall(source))
    keys.update(_PLAN_FIELD.findall(source))
    return keys


def documented_env_keys() -> set[str]:
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if match := _ENV_DOC.match(line):
            keys.add(match.group(1))
    return keys


class ConfigConsistencyTests(unittest.TestCase):
    def test_env_example_and_config_read_the_same_keys(self) -> None:
        self.assertEqual(
            documented_env_keys(),
            config_env_keys(),
            "`.env.example` and `config.py` disagree on the environment surface",
        )

    def test_no_duplicate_documented_keys(self) -> None:
        seen: list[str] = []
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            if match := _ENV_DOC.match(line):
                seen.append(match.group(1))
        duplicates = {key for key in seen if seen.count(key) > 1}
        self.assertFalse(duplicates, f"duplicate keys in `.env.example`: {sorted(duplicates)}")


if __name__ == "__main__":
    unittest.main()
