# Note quality

The booklet reorganizes the lecture; it must not invent facts or change names, terms, numbers or
units.

Every booklet is produced in a single **FULL** mode — a complete, standalone replacement for
attending the class. The former `standard`/`summary` modes were removed; `NOTE_MODE` is now a
no-op and any value (including values on archived booklets) is normalised to `full`.

## Completeness (class-replacement booklets)

The generation prompts mandate a full skeleton: cover/lesson title → table of contents → overall
lesson objective → logical sections in source order (definitions, examples, steps, formulas,
tables, callouts, key points) → a one-page final «جمع‌بندی» → an English-term glossary drawn
only from the source. A coverage rule requires every instructional unit of the transcript to appear
at least once. The Word exporter auto-builds the TOC from section headings, appends the final
«جمع‌بندی» recap and a glossary (derived from section definitions when the model supplies
none), and numbers the headings.

After the map-reduce draft a deterministic completeness validator (`assess_completeness`) checks
both the skeleton and the share of source units represented (a topic-overlap proxy). With
`NOTE_COMPLETENESS_FILL=true` (default), any gap triggers one bounded, strictly grounded fill
request per missing unit (max 6), then re-validates. A fill failure keeps the audited draft rather
than discarding the booklet; fills only re-organise source text and can never invent facts.

## Quiz questions

Quizzes generated from a booklet are deliberately **mid-to-hard**, not recall tests. The quiz
system prompt asks for roughly 40% `medium` and 60% `hard` questions (computed per requested
count), each at the Apply / Analyze / Evaluate level and grounded strictly in the booklet text.
Every item is a short scenario followed by a question ending in «کدام گزینه …؟»; direct recall
stems («X چیست؟», «X را تعریف کن», «چه کسی/چه سالی …؟», name/number/date lookups) are forbidden.

Each question has exactly four options with one correct answer: distractors come from plausible
student misconceptions, all four options are similar in style and length, and giveaway absolutes
(«همیشه», «هرگز», «هیچ‌کدام», «همهٔ موارد فوق») plus the «همهٔ موارد فوق» option are banned.
A one-line Persian explanation accompanies each answer. The output schema adds an **optional**
`difficulty` tag (`medium` | `hard`); `parse_quiz_payload()` records it when present but never
rejects a question that omits it, so the `MIN_VALID_QUESTIONS = 3` refund path is unchanged.
Question stems may now use up to `MAX_QUESTION_CHARS = 400` characters to fit a scenario; option
shuffling is unchanged. Billing (debit before generation, automatic refund on failure) is untouched.

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
