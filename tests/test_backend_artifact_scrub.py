"""Regression tests for backend-artifact scrubbing (spec §46).

The scrubber is the only barrier between provider plumbing and the student's
document, so it has to be complete *and* conservative. Two confirmed defects are
pinned here:

* a Google API key (``AIza…``) has no separator after its prefix, so the pattern
  that required one redacted every "sk-shaped" key except Google's — the one key
  the default Gemini deployment actually uses;
* the "as an AI assistant"-class disclaimer pattern was case-sensitive and
  required a lowercase ``as`` before a capitalised ``AI``, which meant the form a
  model actually writes (sentence-initial ``As an AI assistant …``) was never
  removed from the notes, the title or the Word booklet.

Both are checked on the *delivered* artifacts — the serialized payload the DOCX
and the JSON export are built from, and the generated document text — not only
on the scrubber function, because that is where a leak would end up.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from support import docx_text

from gamas_bot.docx_export import (
    DocumentMeta,
    build_notes_docx,
    resolve_design,
    resolve_fonts,
)
from gamas_bot.structuring import (
    count_backend_artifacts,
    parse_structured_notes,
    scrub_backend_artifacts,
)

#: Live shapes of the registered providers' keys. Each is a value that must
#: never be readable in a student's booklet.
GEMINI_KEY = "AIzaSyC9dKq3WvBn7XyZ1lMnOpQrStUvWxYz123"
GROQ_KEY = "gsk_AbCdEfGhIjKlMnOpQrStUvWxYz012345"
NVIDIA_KEY = "nvapi-9aQ2wErTyUiOpAsDfGhJkLzXcVbNm"
NARA_KEY = "sk-nry-abc123456789"
OPENAI_KEY = "sk-proj-9aQ2wErTyUiOpAsDfGhJkLzXcVbNm0123456789"
ANTHROPIC_KEY = "sk-ant-api03-abcDEF1234567890_-xyzXYZ"
HF_KEY = "hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345"
BEARER = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"

SECRETS = (
    GEMINI_KEY, GROQ_KEY, NVIDIA_KEY, NARA_KEY, OPENAI_KEY, ANTHROPIC_KEY,
    HF_KEY, BEARER,
)

#: Fragments that must be absent from any delivered artifact. The literal
#: secrets are the strongest form of the check and are asserted as well.
SECRET_FRAGMENTS = (
    "AIzaSyC9dKq3WvBn7XyZ1lMnOpQrStUvWxYz123",
    "gsk_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
    "nvapi-9aQ2wErTyUiOpAsDfGhJkLzXcVbNm",
    "sk-nry-abc123456789",
    "sk-proj-9aQ2wErTyUiOpAsDfGhJkLzXcVbNm0123456789",
    "sk-ant-api03",
    "hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
)

#: One legitimate Persian academic sentence per field, so the scrubbed artifact
#: still has to prove it kept the lecture.
LECTURE_SENTENCE = "فشار خون سیستولیک نیروی خون بر دیوارهٔ سرخرگ است و مقدار طبیعی آن ۱۲۰ میلی‌متر جیوه است."

DISCLAIMERS = (
    "As an AI assistant, I cannot provide medical advice.",
    "As an AI language model, I must note that this is not a diagnosis.",
    "As an AI, I cannot browse the web.",
    "I am an AI model and I cannot verify this.",
    "I'm just an AI assistant, so please confirm with a doctor.",
    "به عنوان یک هوش مصنوعی، نمی‌توانم توصیهٔ پزشکی بدهم.",
)

#: Sentences a lecture may legitimately contain. Every one of them mentions the
#: same vocabulary the scrubber looks for, and every one of them must survive
#: byte-for-byte: over-filtering deletes course content.
LEGITIMATE_SENTENCES = (
    "کد وضعیت ۴۲۹ یعنی Too Many Requests و ۵۰۳ یعنی Service Unavailable.",
    "شرکت Cloudflare سرویس CDN ارائه می‌دهد و در برابر حملهٔ DDoS محافظت می‌کند.",
    "یک مدل زبانی، به‌عنوان یک مدل زبانی، توکن بعدی را پیش‌بینی می‌کند.",
    "A chatbot acts as an AI assistant to the user, which is the interface layer.",
    "As an AI tutor, the system explains the concept step by step.",
    "plasma concentration پس از ۳۰ دقیقه به ۵ میلی‌گرم در لیتر می‌رسد.",
)


def _contaminated_payload() -> str:
    """A model answer that leaked a secret and a disclaimer into every field."""
    return json.dumps(
        {
            "title": f"As an AI assistant I cannot help — جزوهٔ فیزیولوژی ({GEMINI_KEY})",
            "summary": f"این خلاصه با Gemini API ساخته شد. کلید {GROQ_KEY} و {BEARER}",
            "learning_objectives": ["As an AI language model, I must note: اهداف"],
            "sections": [
                {
                    "heading": f"Anthropic API — {NVIDIA_KEY}",
                    "paragraphs": [
                        f"{LECTURE_SENTENCE} شناسهٔ کلید {NARA_KEY} و {OPENAI_KEY}",
                        "As an AI assistant, I cannot browse the web.",
                    ],
                    "bullets": [f"hf: {HF_KEY}"],
                    "definitions": [
                        {"term": f"کلید {ANTHROPIC_KEY}", "definition": "کلید سرویس"},
                    ],
                    "examples": ["I am an AI model and I cannot verify this."],
                    "steps": ["گام یک", "گام دو"],
                    "formulas": ["BP = CO × SVR"],
                    "key_points": ["As an AI, I cannot provide medical advice."],
                    "table": {
                        "headers": ["رسانه", "کد"],
                        "rows": [["HTTP 502 Bad Gateway retry-after quota", "chatcmpl-AbCdEf123456"]],
                    },
                    "callouts": [
                        {"kind": "نکته", "text": "generated by Groq"},
                        {"kind": "هشدار", "text": "به عنوان یک هوش مصنوعی، نمی‌توانم توصیهٔ پزشکی بدهم."},
                    ],
                }
            ],
            "key_points": ["As an AI assistant, I cannot diagnose."],
            "review_questions": ["As an AI language model, I cannot answer this."],
            "glossary": [
                {"term": "API", "definition": f"As an AI assistant: کلید {GEMINI_KEY}"},
            ],
        },
        ensure_ascii=False,
    )


class SecretRedactionTests(unittest.TestCase):
    """A provider key echoed into an answer must never reach a student."""

    def test_no_registered_provider_key_shape_survives_the_scrubber(self):
        for secret in SECRETS:
            with self.subTest(secret=secret[:12]):
                cleaned = scrub_backend_artifacts(f"کلید سرویس {secret} است.")
                self.assertNotIn(secret, cleaned)

    def test_every_field_and_the_serialized_payload_are_redacted(self):
        notes = parse_structured_notes(_contaminated_payload())
        payload = notes.to_json()
        for fragment in SECRET_FRAGMENTS:
            with self.subTest(fragment=fragment[:16]):
                self.assertNotIn(fragment, payload)

    def test_the_delivered_word_document_carries_no_secret(self):
        notes = parse_structured_notes(_contaminated_payload())
        data = build_notes_docx(
            notes,
            meta=DocumentMeta(reference="GMS-1", created_at=datetime(2026, 1, 1)),
            fonts=resolve_fonts({}),
            design=resolve_design({"toc_enabled": False}),
        )
        text = docx_text(data)
        # The lecture itself survives...
        self.assertIn("فشار خون سیستولیک", text)
        # ...and nothing about the backend does.
        for fragment in SECRET_FRAGMENTS:
            with self.subTest(fragment=fragment[:16]):
                self.assertNotIn(fragment, text)


class DisclaimerTests(unittest.TestCase):
    """The "as an AI assistant"-class disclaimer, in the forms models write."""

    def test_sentence_initial_disclaimers_are_removed(self):
        for disclaimer in DISCLAIMERS:
            with self.subTest(disclaimer=disclaimer[:24]):
                self.assertNotEqual(scrub_backend_artifacts(disclaimer), disclaimer)

    def test_disclaimers_are_absent_from_the_delivered_document(self):
        notes = parse_structured_notes(_contaminated_payload())
        text = docx_text(
            build_notes_docx(
                notes,
                meta=DocumentMeta(reference="GMS-2", created_at=datetime(2026, 1, 1)),
                fonts=resolve_fonts({}),
                design=resolve_design({"toc_enabled": False}),
            )
        )
        self.assertNotIn("As an AI", text)
        self.assertNotIn("I am an AI", text)
        self.assertNotIn("I'm just an AI", text)
        self.assertNotIn("به عنوان یک هوش مصنوعی", text)
        self.assertIn("فشار خون سیستولیک", text)

    def test_the_attempt_is_counted_so_the_pipeline_can_report_it(self):
        self.assertGreater(count_backend_artifacts(_contaminated_payload()), 0)


class ConservativeFilteringTests(unittest.TestCase):
    """Academic content that merely mentions this vocabulary must survive."""

    def test_legitimate_lecture_sentences_are_untouched(self):
        for sentence in LEGITIMATE_SENTENCES:
            with self.subTest(sentence=sentence[:32]):
                self.assertEqual(scrub_backend_artifacts(sentence), sentence)

    def test_legitimate_content_survives_a_contaminated_document(self):
        notes = parse_structured_notes(_contaminated_payload())
        payload = json.dumps(notes.to_payload(), ensure_ascii=False)
        self.assertIn("فشار خون سیستولیک", payload)
        self.assertIn("BP = CO × SVR", payload)
        self.assertIn("گام یک", payload)

    def test_a_lecture_about_http_and_cloud_providers_is_not_deleted(self):
        body = " ".join(LEGITIMATE_SENTENCES[:2])
        notes = parse_structured_notes(
            json.dumps(
                {
                    "title": "شبکهٔ کامپیوتری",
                    "sections": [{"heading": "لایهٔ کاربرد", "paragraphs": [body]}],
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual(notes.sections[0].paragraphs, (body,))


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
