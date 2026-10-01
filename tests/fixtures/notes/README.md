# Note-quality benchmark corpus

Six realistic lecture transcripts paired with hand-written *reference
documents* — the shape a good model answer should take — plus the coverage
floor each pair must reach.

| Fixture | Domain | Why it is here |
|---|---|---|
| `01_medical_endocrinology` | medical (Persian + English) | dosages, `mg` / `mL` / `kg` / `mmHg`, `HbA1c`, a `120/80` pair, `eGFR`, `AKI` — the hardest case for numeric preservation |
| `02_hit_university` | HCI / university | Persian-dominant prose with English terminology (`WIMP`, `Fitts`, Nielsen heuristics) |
| `03_cs_algorithms` | computer science | big-O notation, `merge`, merge sort, hashing, a numeric comparison at `n = 1000` |
| `04_persian_only_literature` | Persian only | no Latin characters at all — the QA layer must report **zero** signals instead of inventing gaps |
| `05_code_switching_tech` | Persian + English code-switching | spoken identifiers (`greet`, `total_sum`, `ValueError`) and a documentation URL |
| `06_pptx_slide_outline` | PowerPoint | real `slides_outline()` text: slide headings, bullets, speaker notes |

## Layout

- `corpus.json` — the index: transcript file, reference file, and
  `min_number_coverage` / `min_term_coverage` per fixture.
- `*.txt` — the source transcripts.
- `references/*.json` — the reference notes, in the same schema the model is
  asked to produce.

## How it is scored

`tests/test_note_evaluation.py` drives everything deterministically — no
network, no provider, no model call:

- `split_transcript` must be lossless and ordered for every fixture, at several
  chunk budgets;
- `run_note_qa` must observe at least the declared coverage between a fixture's
  reference document and its transcript;
- a deliberately truncated document must *fail* the same check, proving the
  benchmark can actually detect loss;
- merge and DOCX rendering must keep every block and both text directions.

Coverage floors are 100% for numbers everywhere. Terms are 100% except for
`05_code_switching_tech` (0.8), where spoken code produces phonetic tokens such
as "equals" and "string" that a good note legitimately rewrites into real
syntax.

A real model can be scored against this corpus by running the same QA report on
its own answer; what lives here is the reproducible baseline that a prompt
change must not regress.