# Note quality: the completeness and grounding contract

A booklet is a **restructuring of the lecture, not a rewrite of it**. The note
model may clean, group, order, format and tabulate what the lecturer said; it
may not add a fact, remove a teaching unit, or change a number, unit, name or
term. This document describes what that means in the code, what the evidence
gate checks after the model answers, and what an operator can observe.

The design rationale, the research it is based on and the phased rollout are in
[NOTE_QUALITY_PLAN.md](NOTE_QUALITY_PLAN.md).

## 1. The contract

### 1.1 Information units that must survive

A definition; an explanation, mechanism or reason; a worked example, analogy or
counter-example; a step in a procedure; a comparison or classification; a number
with its unit, dose, range or threshold; a date, name, acronym, English term,
formula, code identifier or URL; a condition, exception or warning; a result or
conclusion the lecturer stated.

### 1.2 Allowed edits

Remove filler, false starts, ASR noise and off-topic chatter; remove verbatim
repetition once; fix punctuation, spacing and digit shape; split spoken
sentences and join fragments; create headings from the lecture's own topics;
group same-topic content in first-appearance order; normalise a unit's *spelling*
(`میلی‌گرم` ↔ `mg`) without converting its value; put definitions, examples,
steps and formulas in their own fields; build tables and tips from source
content.

### 1.3 Forbidden edits

Any new fact, statistic, reference, guideline, recommendation, dose, example,
analogy or comparison; any number, unit, date, name or acronym not in the
source; translating a technical term or expanding an acronym the source did not
expand; unit conversion, rounding, recalculation or a derived value (BMI, eGFR,
percentages); deleting a definition, explanation, exception, warning or worked
example; collapsing a multi-sentence explanation into one generic sentence;
turning a hedged statement into a confident one; inventing study advice.

### 1.4 Tables and practical tips

A **table** is allowed when the source compares at least two items on at least
two attributes. Every cell must be a source value (number + unit) or a short
phrase whose content words occur in the source. Headers are the model's own. A
row anchored in the source may carry a short Persian gloss of its own English
term (`INSERT` → «درج»). No computed cells, no inferred rankings, no invented
empty cells.

A **practical tip** is the lecturer's own advice or warning, surfaced — never
the model's advice. Each tip carries `basis`: a verbatim source phrase of 5–30
words. A tip whose basis cannot be found in the transcript is dropped. If the
lecture contains no advice, the correct output has no tips.

### 1.5 Standard equivalents

An English term the source does not contain is allowed only in a bounded
equivalence position: a definition's `term_en`, a glossary term, a short
parenthetical expansion, a table cell, or a code example. A small auditable
table (`notes_evidence.STANDARD_EQUIVALENTS`) also allows the English form when
the lecturer said the Persian form (`پایتون` → `Python`). An English sentence in
paragraph prose is still a fabrication. Numbers are never exempt.

## 2. The pipeline

```
transcript / slide text
        │
        ▼  map per chunk (prompt v4: schema includes tables + tips with basis)
        ▼  deterministic merge (dedupe, same-topic sections, table rows)
        ▼  gate:        tables validated, tips validated, objectives/questions gated
        ▼  audit:       numbers/terms → omissions and fabrications, sentence coverage
        ├── deficits and NOTE_QUALITY_GATE=repair
        ▼  repair:      ONE request with the missing facts + their source windows
        ▼  scrub:       sentences still asserting an unsupported number are removed
        ▼  compile:     lossless editorial merge, re-gated and re-audited
        ▼  log:         one "note quality …" line
```

`NOTE_STRICT_GROUNDING=false` skips every step below the merge (the rollback
switch). `NOTE_QUALITY_GATE=warn` runs the audit and the quality log but changes
nothing at all — not even a table or tip the gate could prove unsupported (its
counts still appear in the log, so the operator sees exactly what `repair` would
have dropped).

## 3. What is measured

| Metric | Meaning | Gate |
| --- | --- | --- |
| number recall | source number+unit signals present in the booklet | ≥ 0.95 `full`, 0.90 `standard`, 0.70 `summary` |
| term recall | source Latin terms, acronyms and URLs present | ≥ 0.95 / 0.85 / 0.60 |
| precision | booklet signals absent from the source | numbers: 0 tolerated (scrubbed); terms: allowed only in equivalence positions |
| sentence coverage | source sentences whose content words appear in the booklet | soft: below 0.75 asks for a repair |
| table cells | cells traceable to the source | any numeric failure rejects the table |
| tip basis | `basis` found in the source | required |

Units are compared through an alias table, so `۵۰۰ میلی‌گرم` and `500 mg` are the
same fact. Digits of either script fold before comparison. Structural numbering
(`### اسلاید ۳`, `[بخش ۱ از ۳ …]`, list ordinals) is masked, while a semester
like `۱۴۰۵` stays a content number. A URL is one atomic signal, so the `3` in
`https://docs.python.org/3/tutorial` is not a number.

## 4. Repairs and their limits

* One request per note generation, only when something is actually wrong.
* It carries the booklet, the missing facts and the source sentence around
  each — never the whole transcript — so restoring a fact is copying.
* It is skipped when the request would not fit the configured context budget.
* Its answer is accepted only if it is grounded and covers at least as much as
  the booklet it replaces; otherwise the previous state is kept.
* A number that survives the repair is removed with the sentence that states
  it. That can cost a real fact, which the audit then reports as a missing
  signal; an omission is visible and recoverable, an invented dosage is not.

## 5. What is deliberately not automated

Prose fabrication that contains no number or term (a plausible sentence that the
lecturer never said) cannot be detected by a deterministic rule. It is
constrained by the prompt, measured by sentence coverage, and reviewed by
sampling. The evidence layer does not pretend to solve it, and no LLM judge runs
in the delivery path.

## 6. Operator view

Every note generation logs one line:

```
note quality signals=23 numbers=16 terms=7 coverage=1.00 number_coverage=1.00
term_coverage=1.00 missing=0 fabricated=0 equivalents=13
sentence_coverage=0.95 tables=1/1 tips=2/2 objectives_dropped=0 review_questions_dropped=0
```

The line contains counts only — never transcript or booklet text. A rise in
`missing` after a model change means the model became terser; a non-zero
`fabricated` means it invented something and the scrub ran. Both are expected to
be rare in `full` mode, and both are bounded by the repair budget rather than by
an unbounded retry loop.

## 7. Tests

`tests/test_note_evidence.py` is the restored deterministic benchmark over
`tests/fixtures/notes/`: six real transcripts with hand-written Persian
reference documents. It asserts that every reference meets its declared coverage
floor, that a truncated reference fails, that a changed digit is reported as a
fabrication, that the Persian-only lecture yields zero signals rather than
invented gaps, and that slide numbers are masked while a semester is not.

The reference fixtures were calibrated against the strict policy: two of them
originally dropped a real value (`۱.۷۳` in the medical lecture, `۱۴۰۵` in the
presentation), and one table carried clinical verdicts the lecture never gave
(«طبیعی»). Those are now source-grounded, because a benchmark that encodes an
addition cannot be used to test "nothing is added".
