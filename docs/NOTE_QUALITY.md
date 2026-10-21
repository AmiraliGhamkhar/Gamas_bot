# Note quality

The booklet should reorganize the lecture, not replace it. The model may clean, group and format the source; it must not invent facts or change names, terms, numbers or units.

## Checks

When `NOTE_STRICT_GROUNDING=true` (default), the deterministic evidence gate compares source and output signals, checks table cells and requires source text for practical tips. If enabled, at most one bounded repair request can restore missing material; unsupported numeric claims are removed. `NOTE_QUALITY_GATE=warn` audits and logs (at WARNING) without changing the generated notes, so **unvalidated tables and tips reach the delivered document**; `NOTE_STRICT_GROUNDING=false` skips the gate and the repair entirely. Both are diagnostic settings, not a way to improve quality.

Practical tips keep the model's own order and are capped at eight; a longer justification no longer promotes a tip. When the transcript is split into several chunks, identical paragraphs, bullets and table rows are de-duplicated across the whole booklet — a sentence repeated in two different topics is therefore emitted once. A failed chunk fails the whole map step by design: the user keeps the raw transcript and the request is not partially merged.

## Large booklets

A multi-hour lecture produces a merged booklet larger than one request, so the passes scale instead of switching off:

- **Editorial compile.** When the merged booklet fits the compile budget it is sent once, as before. When it does not, it is compiled in contiguous groups of whole sections (`compile pass grouped` is logged), each group under the budget; sections are never split, and a single section too large for the budget is delivered verbatim with a `compile skipped` warning. A second level — one final merge of the compiled groups — is attempted only when the compiled text actually shrank enough to fit, so the number of extra requests is bounded (`MAX_COMPILE_LEVELS = 2`). The whole assembled booklet is still audited against the merge it replaced: a compile that invents a fact or loses coverage is rejected group by group.
- **Repair.** If the whole booklet does not fit the repair budget, the request carries only the sections that hold the lost facts (`MAX_REPAIR_SECTIONS = 6`), chosen by the overlap between each section and the source window of the missing/fabricated signal. The answer is merged back by heading; a section the model invents is ignored, and the rest of the booklet is delivered exactly as audited. A size-driven skip is now logged (`repair skipped`) instead of silently dropping the pass.

Only a `compile pass disabled` warning remains a misconfiguration signal: it means `NOTE_CONTEXT_TOKENS` leaves no room for even the compile prompt, so nothing can be sent at that size.

The raw transcript remains the reference and is delivered before notes. If all note providers fail, the user keeps the transcript.

## Limits

These checks catch missing or invented numbers, terms and some structural content. They cannot prove every ordinary prose sentence was said in the lecture. Provider output still needs human review when accuracy is important, especially for medical, legal or safety-critical content.

To diagnose a change, check the `note quality` summary in the job log. It contains counts, not transcript text. Settings are in `.env.example`; regression tests use the deterministic corpus under `tests/fixtures/notes/`.
