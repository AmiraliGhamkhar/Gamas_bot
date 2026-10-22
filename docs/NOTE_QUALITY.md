# Note quality

The booklet reorganizes the lecture; it must not invent facts or change names, terms, numbers or
units.

## Checks

With `NOTE_STRICT_GROUNDING=true` (default), a deterministic evidence gate indexes the source once
and compares source and output signals (numbers, terms, table cells, practical tips). At most one
bounded repair request can restore missing material; unsupported numeric claims are removed.

- `NOTE_QUALITY_GATE=warn` audits and logs without changing the notes (so unvalidated tables and
  tips still reach the document).
- `NOTE_STRICT_GROUNDING=false` skips the gate and the repair.
- Practical tips keep the model's order and are capped at eight.
- Identical paragraphs, bullets and table rows are de-duplicated across the whole booklet.
- A failed chunk fails the map step by design: the user keeps the raw transcript and no partial
  booklet is delivered.

## Large booklets

A multi-hour lecture produces a merge larger than one request:

- **Editorial compile** runs in contiguous groups of whole sections under `NOTE_CONCURRENCY`,
  preserving order, and attempts one final merge when the text shrank enough
  (`MAX_COMPILE_LEVELS = 2`). A section too large for the budget is kept verbatim with a
  `compile skipped` warning. A compile that invents facts or loses coverage is rejected group by
  group.
- **Repair** sends only the sections holding the lost facts (`MAX_REPAIR_SECTIONS = 6`) and merges
  the answer back by heading. Other sections are delivered exactly as audited.

The raw transcript is always delivered first and remains the reference. If every note provider
fails, the user keeps the transcript.

## Limits

These checks catch missing or invented numbers, terms and some structural content. They cannot
prove every ordinary sentence was said. Review output when accuracy matters, especially for
medical, legal or safety-critical content. The job log contains a `note quality` summary with
counts, not transcript text. Regression fixtures live in `tests/fixtures/notes/`.
