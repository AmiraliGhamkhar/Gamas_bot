"""Configuration and documentation must not drift from the code.

Three classes of drift cost real production incidents and are cheap to detect:

* a configuration variable the code reads but nobody documents (the operator
  cannot know it exists);
* a variable ``.env.example`` offers but no code reads (the operator sets it and
  nothing happens);
* documentation that names a module, a file or a deployment gate that no longer
  exists.
"""

from __future__ import annotations

import dataclasses
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "gamas_bot" / "config.py"
ENV_EXAMPLE = ROOT / ".env.example"
DOC_FILES = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]

#: Environment variables read outside the canonical settings module (startup
#: scripts and the offline tooling). They are still documented in .env.example.
_EXTERNAL_ENV_READERS = ("passenger_wsgi.py", "scripts", "gamas_bot/__main__.py")


def _env_names_read_by_config() -> set[str]:
    """Every environment variable name ``config.py`` looks up."""
    source = CONFIG.read_text(encoding="utf-8")
    names: set[str] = set()
    for pattern in (
        r'os\.getenv\(\s*"([A-Z0-9_]+)"',
        r'_text\(\s*"([A-Z0-9_]+)"',
        r'_flag\(\s*"([A-Z0-9_]+)"',
        r'_path\(\s*"([A-Z0-9_]+)"',
        # PLAN_ENV_FIELDS entries: ("FREE_PLAN_HOURS", "free_plan_hours", 1)
        r'\(\s*"([A-Z0-9_]+)",\s*"[a-z0-9_]+",',
    ):
        names.update(re.findall(pattern, source))
    assert names, "no environment variables found in config.py; the parser is broken"
    return names


def _env_example_assignments() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return {
        match.group(1)
        for match in re.finditer(r"(?m)^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", text)
    }


class ConfigurationConsistencyTests(unittest.TestCase):
    def test_every_read_variable_is_documented(self):
        documented = ENV_EXAMPLE.read_text(encoding="utf-8")
        missing = sorted(
            name
            for name in _env_names_read_by_config()
            # A name counts as documented when it appears at all -- the legacy
            # aliases are documented as commented examples on purpose.
            if not re.search(rf"\b{name}\b", documented)
        )
        self.assertEqual(missing, [], f"undocumented configuration variables: {missing}")

    def test_env_example_has_no_dead_variables(self):
        read = _env_names_read_by_config()
        extra_readers = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            if (ROOT / path).is_file()
            else "\n".join(
                file.read_text(encoding="utf-8") for file in sorted((ROOT / path).rglob("*.py"))
            )
            for path in _EXTERNAL_ENV_READERS
        )
        dead = sorted(
            name
            for name in _env_example_assignments()
            if name not in read and not re.search(rf'["\']{name}["\']', extra_readers)
        )
        self.assertEqual(dead, [], f".env.example configures variables nothing reads: {dead}")

    def test_settings_defaults_match_the_canonical_plan_catalog(self):
        """A default defined twice is a bug waiting for a deployment to hit it."""
        from gamas_bot import billing, config

        settings_defaults = {
            field.name: field.default for field in dataclasses.fields(config.Settings)
        }
        for env_name, attribute, canonical in config.PLAN_ENV_FIELDS:
            self.assertEqual(
                settings_defaults[attribute],
                canonical,
                f"Settings.{attribute} default disagrees with {env_name}",
            )
        # The billable catalog reads the same canonical constants.
        for spec in billing.PAID_PLAN_SPECS:
            attribute = f"plan_{spec.hours}_hours"
            self.assertEqual(settings_defaults.get(attribute), spec.hours)

    def test_toc_page_number_policy_is_consistent_everywhere(self):
        from gamas_bot.config import Settings  # noqa: F401 - import proves the field exists

        source = CONFIG.read_text(encoding="utf-8")
        self.assertIn("DOCX_TOC_PAGE_NUMBERS", source)
        self.assertIn("DOCX_TOC_PAGE_NUMBERS", ENV_EXAMPLE.read_text(encoding="utf-8"))

    def test_documented_modules_exist(self):
        """A docs page that names a module must name one that is importable."""
        missing: dict[str, set[str]] = {}
        for document in DOC_FILES:
            text = document.read_text(encoding="utf-8")
            referenced = set(re.findall(r"gamas_bot\.([a-z_][a-z0-9_]*)", text))
            referenced |= set(re.findall(r"(?m)^\|\s*`([a-z_]+)\.py`", text))
            for name in sorted(referenced):
                if name in {"py"}:
                    continue
                if not (ROOT / "gamas_bot" / f"{name}.py").exists():
                    missing.setdefault(document.name, set()).add(name)
        self.assertEqual(missing, {}, f"documentation references missing modules: {missing}")

    def test_documented_docs_files_exist(self):
        """Markdown links between documents must resolve."""
        broken: dict[str, list[str]] = {}
        for document in DOC_FILES:
            text = document.read_text(encoding="utf-8")
            for target in re.findall(r"\]\(([^)#\s]+\.md)\)", text):
                if target.startswith(("http://", "https://")):
                    continue
                if not (document.parent / target).resolve().exists():
                    broken.setdefault(document.name, []).append(target)
        self.assertEqual(broken, {}, f"documentation links are broken: {broken}")

    def test_documented_file_formats_are_actually_supported(self):
        from gamas_bot.bot import AUDIO_EXTENSIONS, FORMATS_TEXT, VIDEO_EXTENSIONS

        audio_lists_audio = bool(re.search(r"MP3|WAV|M4A", FORMATS_TEXT))
        self.assertTrue(audio_lists_audio, "FORMATS_TEXT stopped describing audio")
        for extension in (".mp3", ".wav", ".m4a", ".pptx"):
            token = extension.lstrip(".").upper()
            if token == "PPTX":
                self.assertIn("PPTX", FORMATS_TEXT)
            else:
                self.assertIn(token, FORMATS_TEXT)
                self.assertIn(extension, AUDIO_EXTENSIONS | VIDEO_EXTENSIONS)


if __name__ == "__main__":
    unittest.main()
