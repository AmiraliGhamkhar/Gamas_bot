# Note-quality benchmark corpus

> **Status: live again.** `corpus.json` is loaded by
> `tests/test_note_evidence.py`, the deterministic scorer that replaced the
> removed v2 `gamas_bot.qa.run_note_qa` / `tests/test_note_evaluation.py`. It
> measures coverage, precision (fabrication), truncation, structural masking and
> the table/tip checks of `gamas_bot/notes_evidence.py`. The `description` field
> of `corpus.json` still names the v2 scorer and is kept as a historical note.

Six realistic lecture transcripts paired with hand-written *reference documents*
— the shape a good model answer should take — plus the coverage floor each pair
must reach.

| Fixture | Domain | Why it is here |
| --- | --- | --- |
| `01_medical_endocrinology` | medical (Persian + English) | dosages, `mg`/`mL`/`kg`/`mmHg`, `HbA1c`, a `120/80` pair, `eGFR`, `AKI` — the hardest case for numeric preservation |
| `02_hit_university` | HCI / university | Persian-dominant prose with English terminology (`WIMP`, `Fitts`, Nielsen heuristics) |
| `03_cs_algorithms` | computer science | big-O notation, `merge`, merge sort, hashing, a comparison at `n = 1000` |
| `04_persian_only_literature` | Persian only | no Latin characters at all — a scorer must report **zero** signals instead of inventing gaps |
| `05_code_switching_tech` | Persian + English code-switching | spoken identifiers (`greet`, `total_sum`, `ValueError`) and a documentation URL |
| `06_pptx_slide_outline` | PowerPoint | real `slides_outline()` text: slide headings, bullets, speaker notes |

`slides_outline()` still exists, in `gamas_bot/presentations.py`.

## Layout

* `corpus.json` — the index: transcript file, reference file, and
  `min_number_coverage` / `min_term_coverage` per fixture.
* `*.txt` — the source transcripts.
* `references/*.json` — the reference notes, in the schema the model is asked to
  produce.

## What a restored benchmark needs

The original scorer was deterministic — no network, no provider, no model call —
and asserted that:

* `split_transcript` is lossless and ordered for every fixture at several chunk
  budgets;
* a scorer observes at least the declared coverage between a fixture's reference
  document and its transcript;
* a deliberately truncated document *fails* that same check, proving the
  benchmark can detect loss;
* merge and DOCX rendering keep every block and both text directions.

The first of those is live today in `tests/test_transcript.py`. Coverage floors
were 100% for numbers everywhere, and 100% for terms except
`05_code_switching_tech` (0.8), where spoken code produces phonetic tokens such
as "equals" and "string" that a good note legitimately rewrites into real syntax.

The scorer now lives in `tests/test_note_evidence.py`. Two reference documents
were corrected when the strict grounding policy landed: `01` had dropped the
`۱.۷۳` body-surface denominator and asserted clinical verdicts («طبیعی») the
lecture never gave, and `06` had dropped the semester `۱۴۰۵`; both are content
under the "nothing added, nothing removed" contract.
