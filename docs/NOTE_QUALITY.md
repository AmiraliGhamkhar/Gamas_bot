# Note quality

The raw transcript is delivered first and remains the reference. Booklets must
preserve source names, numbers, terms and units rather than invent facts.

## Current behavior

- Audio uses full mode: title, contents, objective, source-ordered sections, recap
  and source-based glossary. `NOTE_MODE` is a legacy no-op.
- Compact document revision notes are a separate action, not an audio mode.
- Structure/topic coverage checks can trigger up to 6 bounded fill requests with
  `NOTE_COMPLETENESS_FILL=true`.
- Defaults `NOTE_STRICT_GROUNDING=true` and `NOTE_QUALITY_GATE=repair` check numbers,
  terms, table cells and tips. Unsupported numeric claims are removed; repair is bounded.
- Editorial compilation groups whole sections and rejects fact/coverage loss.
  Oversized sections may stay uncompiled.
- Failed map chunks do not produce partial booklets. The user keeps the delivered
  transcript; transcription billing is unchanged.
- One static prompt covers every pass: `gamas_bot/_prompt_block.py` holds the
  booklet-agent contract (the eight principles, the JSON shape and the booklet
  skeleton the exporter renders). It is byte-identical for every chunk, source
  kind and job, and every per-job datum — position, topic map, neighbours, the
  transcript — travels in the user message. That is what makes a provider's
  prefix cache hit, and `tests/test_notes.py::PromptTests` asserts both halves.

`NOTE_QUALITY_GATE=warn` only audits. Disabling strict grounding skips evidence
protection. Keep production defaults. Repair/hedging can use additional provider quota.

## Learning output

Quizzes target application/analysis, roughly 40% medium and 60% hard, with four
options, one answer and a short explanation. Items are anchored to source chunks;
coverage/count shortfalls are reported. Presets request 10/20/40 cards or 5/10/20
questions. Review previews before saving.

## Accuracy limit

Checks cannot prove every sentence is supported or every concept covered. Review
critical material manually, especially medical, legal or safety content. Regression
fixtures in `tests/fixtures/notes/` use no live AI calls.
