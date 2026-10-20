# Note quality

The booklet should reorganize the lecture, not replace it. The model may clean, group and format the source; it must not invent facts or change names, terms, numbers or units.

## Checks

When `NOTE_STRICT_GROUNDING=true` (default), the deterministic evidence gate compares source and output signals, checks table cells and requires source text for practical tips. If enabled, at most one bounded repair request can restore missing material; unsupported numeric claims are removed. `NOTE_QUALITY_GATE=warn` audits and logs without changing the generated notes.

The raw transcript remains the reference and is delivered before notes. If all note providers fail, the user keeps the transcript.

## Limits

These checks catch missing or invented numbers, terms and some structural content. They cannot prove every ordinary prose sentence was said in the lecture. Provider output still needs human review when accuracy is important, especially for medical, legal or safety-critical content.

To diagnose a change, check the `note quality` summary in the job log. It contains counts, not transcript text. Settings are in `.env.example`; regression tests use the deterministic corpus under `tests/fixtures/notes/`.
