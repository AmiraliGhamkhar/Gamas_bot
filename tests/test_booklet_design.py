"""Contracts for the Gamas document design system.

These tests guard the layer that makes every booklet look like Gamas without
any renderer hard-coding a colour, a size or a filename:

* the semantic palette and type scale (:mod:`gamas_bot.document.theme`),
* the asset manifest and topic-aware selection
  (:mod:`gamas_bot.document.assets`),
* the component vocabulary (:mod:`gamas_bot.document.components`),
* the BiDi line preparation shared by both renderers
  (:mod:`gamas_bot.document.rtl`).

They are deliberately renderer-free: a failure here means the design system
itself regressed, not that one PDF happened to shift by a point.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from gamas_bot.document import assets, components, qa
from gamas_bot.document.rtl import visual, visual_ltr, wrap_logical
from gamas_bot.document.theme import THEME

REQUIRED_PALETTE = (
    "navy_950", "navy_900", "navy_800", "navy_700",
    "teal_700", "teal_600", "teal_500", "teal_400",
    "cyan_500", "cyan_300",
    "cream_50", "cream_100", "paper",
    "ink", "body", "muted", "on_dark", "on_accent",
    "line", "border", "zebra",
    "success", "warning", "danger", "info",
)


class PaletteTests(unittest.TestCase):
    def test_every_semantic_colour_is_defined_as_a_hex_value(self):
        palette = THEME.palette
        for name in REQUIRED_PALETTE:
            with self.subTest(colour=name):
                value = getattr(palette, name)
                self.assertRegex(value, r"^#[0-9A-Fa-f]{6}$")

    def test_theme_validation_passes(self):
        self.assertIsNone(THEME.validate())

    def test_brand_core_matches_the_gamas_identity(self):
        palette = THEME.palette
        self.assertEqual(palette.navy_900.upper(), "#062C3D")
        self.assertEqual(palette.teal_700.upper(), "#087F8C")
        self.assertEqual(palette.cyan_500.upper(), "#25C6D6")
        self.assertEqual(palette.paper.upper(), "#F2EFE7")
        self.assertEqual(palette.ink.upper(), "#102B36")

    def test_red_is_reserved_for_warnings_and_errors(self):
        # The cautionary tone is the only red family in the system; no callout
        # other than the warning may borrow it.
        styles = {
            kind: components.callout_style(kind) for kind in components.CALLOUTS
        }
        warning = styles["هشدار"]
        for kind, style in styles.items():
            if kind == "هشدار":
                continue
            with self.subTest(callout=kind):
                self.assertNotEqual(style.accent.upper(), warning.accent.upper())
                self.assertNotEqual(style.fill.upper(), warning.fill.upper())


class TypeTests(unittest.TestCase):
    def test_body_text_stays_in_the_readable_band(self):
        # Printed at A4 and read on phones: below 9.5 pt a Persian body line
        # with diacritics stops being comfortable.
        self.assertGreaterEqual(THEME.type.body, 9.5)
        self.assertLessEqual(THEME.type.body, 11.5)

    def test_heading_scale_descends(self):
        types = THEME.type
        # The banner sits inside a navy bar and is set slightly smaller than a
        # page-level heading; the rest of the scale is strictly descending.
        self.assertGreater(types.h1, types.h2)
        self.assertGreater(types.h2, types.h3)
        self.assertGreater(types.h3, types.caption)
        self.assertGreaterEqual(types.banner, types.h3)


class LanguageTests(unittest.TestCase):
    def test_chapter_numbers_are_persian_digits(self):
        self.assertEqual(components.chapter_number(1), "۰۱")
        self.assertEqual(components.chapter_number(12), "۱۲")
        self.assertEqual(components.chapter_number(100), "۱۰۰")

    def test_a_persian_sentence_keeps_its_rtl_base_with_a_leading_latin_term(self):
        # The clause must end with the Latin token on the right-hand (start)
        # edge. An auto-detected LTR base mirrors it and pushes "را" to the far
        # left, which is how the booklet used to render "eGFR را …".
        rendered = visual("eGFR را پیش از افزایش دوز بررسی کنید")
        self.assertTrue(rendered.endswith("eGFR"), rendered)
        self.assertNotIn(" ", rendered.strip()[-4:])

    def test_latin_formulas_stay_left_to_right(self):
        self.assertEqual(visual_ltr("eGFR = 175 × (SCr)^-1.154"), "eGFR = 175 × (SCr)^-1.154")

    def test_a_persian_word_is_never_broken_by_the_wrapper(self):
        # A column too narrow for "سیتاگلیپتین" must let it overflow, not split
        # it into "سیتاگلیپتی" + "ن".
        lines = wrap_logical("سیتاگلیپتین", lambda text: len(text) * 40.0, 40.0)
        self.assertEqual(lines, ["سیتاگلیپتین"])

    def test_an_overlong_latin_token_is_broken_to_protect_the_frame(self):
        lines = wrap_logical("https://example.org/a/very/long/path", len, 12)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines), "https://example.org/a/very/long/path")


class AssetTests(unittest.TestCase):
    def test_the_manifest_validates_and_every_file_exists(self):
        self.assertEqual(assets.validate_manifest(), [])
        for asset in assets.all_assets():
            with self.subTest(asset=asset.key):
                self.assertTrue(Path(asset.path).is_file(), asset.path)
                self.assertGreater(asset.width, 0)
                self.assertGreater(asset.height, 0)
                self.assertTrue(asset.role)

    def test_resolution_is_semantic_and_strict(self):
        with self.assertRaises(assets.MissingAsset):
            assets.resolve("decorative/does-not-exist")
        self.assertIsNone(assets.optional("decorative/does-not-exist"))
        self.assertIsNotNone(assets.resolve("brand/monogram"))

    def test_topic_selection_is_deterministic_and_relevant(self):
        medical = assets.topic_for("فیزیولوژی قلب و عروق، فشار خون و نارسایی کلیوی")
        pharma = assets.topic_for("داروشناسی و مکانیسم داروهای دیابت")
        data = assets.topic_for("الگوریتم و ساختار داده، پایگاه داده و شبکهٔ عصبی")
        for text, expected in (("پزشکی قلب", "medical"), ("دارو", "pharma"),
                               ("پایگاه داده و شبکهٔ عصبی", "data")):
            with self.subTest(text=text):
                self.assertEqual(assets.topic_for(text).key, expected)
        self.assertEqual(medical.key, "medical")
        self.assertEqual(pharma.key, "pharma")
        self.assertEqual(data.key, "data")
        self.assertEqual(assets.topic_for("موضوع"), assets.topic_for("موضوع"))

    def test_an_unknown_or_empty_subject_falls_back_to_the_general_cover(self):
        self.assertEqual(assets.topic_for("").key, "general")
        self.assertEqual(assets.topic_for("تاریخ هنر").key, "general")
        self.assertIsNotNone(assets.cover_for(""))

    def test_an_explicit_topic_override_wins(self):
        self.assertEqual(assets.topic_for("داروشناسی", override="data").key, "data")

    def test_bundled_artwork_passes_the_asset_audit(self):
        self.assertEqual(qa.validate_assets(), ())


class ComponentTests(unittest.TestCase):
    def test_every_callout_kind_has_a_label_and_a_style(self):
        for kind in components.CALLOUTS:
            with self.subTest(callout=kind):
                self.assertTrue(components.callout_style(kind).label)
                self.assertTrue(components.callout_style(kind).accent)

    def test_an_unknown_callout_falls_back_instead_of_crashing(self):
        style = components.callout_style("چیز ناموجود")
        self.assertTrue(style.label)

    def test_every_section_label_is_present(self):
        for key in ("guide", "glance", "objectives", "summary", "key_points",
                    "tips", "review", "common_mistakes", "glossary", "final_review"):
            with self.subTest(label=key):
                self.assertTrue(components.LABELS[key].strip())

    def test_the_design_system_does_not_hard_code_colours_in_renderers(self):
        # The renderers may use the palette and the OOXML bridge, but a raw
        # hex colour in either file means a colour escaped the token layer.
        import re

        root = Path(__file__).resolve().parent.parent / "gamas_bot"
        for name in ("notes_pdf.py", "docx_export.py"):
            source = (root / name).read_text(encoding="utf-8")
            literals = re.findall(r'"#[0-9A-Fa-f]{6}"', source)
            with self.subTest(file=name):
                self.assertEqual(literals, [], f"raw colour literals in {name}")


if __name__ == "__main__":
    unittest.main()
