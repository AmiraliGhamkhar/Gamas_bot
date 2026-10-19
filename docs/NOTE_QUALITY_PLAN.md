# Note quality plan — complete, grounded, professional

> **Status: implemented (2026-10-19).** Phases P0–P4 landed on
> `arena/03f2f86e-gamas-bot`; the operator-facing description lives in
> [NOTE_QUALITY.md](NOTE_QUALITY.md) and the change is recorded in
> [CHANGELOG.md](CHANGELOG.md). The decisions in §12 were taken as recommended:
> strict tips (D1), standard equivalents allowed in bounded positions (D2), and
> gated objectives/review questions (D3). D4 was implemented as a
> coverage-comparison gate rather than a raised character floor; D5 (a
> completeness appendix) was not needed — the bounded repair plus the
> fabricated-number scrub close the gap without appending raw text. This
> document is kept as the design record: it explains *why* the contract looks
> the way it does.

---

## 1. Executive summary

The pipeline today already *asks* for completeness, but it cannot *verify* it.
The prompt says "you are a compiler, not a summarizer"; the only post-generation
gate counts sections and characters. A model can delete every dosage, swap
`12.5` for `12.3`, or invent a definition and still pass every check.

The recommended change is not "a stronger summary prompt". It is a change of
contract:

> **Note generation = lossless editorial restructuring with evidence checks.**
> The model may clean, group, order, format, tabulate and surface; it may not
> add a fact, remove an information unit, or change a number, unit, name or
> term. Tables and practical tips are *extractions and arrangements* of what the
> lecturer said, never new material.

That contract is enforced in three layers:

1. **Prompt layer** — a rewritten v4 prompt that defines exactly what "clean",
   "organize", "table" and "practical tip" mean, and what is forbidden.
2. **Deterministic evidence layer** (new, no network, no model) — extracts the
   source's *signals* (numbers with units, dates, Latin terms, URLs, code
   identifiers), audits the generated notes for **omissions** and
   **fabrications**, validates every table cell and every tip against the
   source, and computes a sentence-level coverage proxy.
3. **Bounded repair layer** — one extra pass (folded into the existing compile
   call where possible) that receives the *missing signals with their source
   sentences* and may only restore them; a failed repair falls back, never
   silently ships a fabrication.

Two new output capabilities are added **with provenance**:

* `tables` (multiple per section, captioned) — every cell must trace to the
  source; a failed table is dropped.
* `practical_tips` — only the lecturer's explicit practical advice, each with a
  verbatim `basis` quote; a tip whose basis cannot be found is dropped.

---

## 2. Scope and non-goals

**In scope**

* Completeness and fidelity of the generated booklet (lecture, presentation and
  Office-free paths are separate; this plan targets the note path).
* New table support and a practical-tips field.
* Academic/professional register rules.
* Deterministic QA, bounded repair, logging and a restored benchmark.

**Not in scope**

* Changing STT, billing, delivery or the DOCX design language.
* Adding a heavyweight local model (no `torch`, no NLI model). The repo's
  dependency list is deliberately small; the evidence layer is pure Python.
* Live web grounding: the transcript is the only source of truth. No external
  retrieval, no "enrichment" from the model's own knowledge.
* Reworking the Free Office section (`office_ai.py` already validates cells
  against the user's text; it is cited below as precedent, not changed).

---

## 3. Baseline: what the code does today

Measured on branch `arena/03f2f86e-gamas-bot` at `a15b524`; test baseline:
**544 passed in 14.45 s** (`python -m pytest -q`).

### 3.1 The note model and prompts

| Area | Current behaviour | Consequence |
| --- | --- | --- |
| Role | "گردآورندهٔ جزوهٔ درس، نه خلاصه‌ساز" (`notes.py:768`) | Correct intent, unenforced |
| Content rules | `_SEMANTIC_RULES`, `_CONTENT_RULES`, `_STYLE_RULES` (`notes.py:637–693`) | Strong prose, no operational definition of an "information unit" |
| Schema | `StructuredNotes` (`notes.py:226`), one `table` per section (`NoteTable`, `notes.py:115`) | Tables are rare; no tips field |
| Extras | `learning_objectives`, `review_questions`, `glossary`, `term_en` | All are invitation points for outside knowledge |
| Compile | Free rewriting: "روان کنید … گذارها را بازسازی کنید" (`COMPILE_SYSTEM_PROMPT`, `notes.py:812`) | Paraphrase risk; the editorial pass can quietly compress |
| Acceptance | `compile_accepts` (`notes.py:1363`) checks section ratio ≥ 0.5, structured-item ratio ≥ 0.5, body-char ratio ≥ 0.35 | A fact-gutted answer with balanced characters passes |
| Merge | Deterministic dedupe/merge (`notes.py:1077`) | Conservative, good — but it merges whatever the map calls produced |
| Call budget | 1 request/chunk + 1 optional compile; no repair, no QA (`notes.py:1541`) | Cheap and resilient; must stay cheap |

### 3.2 There is already a fidelity precedent in the repo

`transcript.py:147–190` implements a **content fingerprint** and
`guarded()` — every typographic transform is rejected if digit groups, Latin
tokens, ZWNJ or letter counts change. That is exactly the philosophy this plan
extends to notes, with one important correction:

> A **count-based** fingerprint catches loss, not **substitution**. Replacing
> `12.5` with `12.3` keeps every count identical. The note layer therefore needs
> **set-based signals with unit-aware canonicalisation**, not counts.

`office_ai.py:362` already validates every proposed table cell with
`normalize_for_compare(cell) in source` — the same pattern, applied to notes.

### 3.3 The benchmark exists but is unused

`tests/fixtures/notes/README.md` states the corpus is unused and that the old
deterministic scorer (`gamas_bot.qa.run_note_qa`, removed in v3) is gone. Six
hand-written Persian reference documents and their transcripts remain. Restoring
that scorer is the first measurable step of this plan.

### 3.4 Calibration of the fixtures (measured today)

A naive "every source number must appear in the notes" check fails on the
hand-written references themselves:

| Fixture | Numbers src → ref | What the reference does |
| --- | --- | --- |
| `01_medical_endocrinology` | 17 → 16 | Drops `1.73` (body-surface-area denominator); rewrites `میلی‌گرم` → `mg`, `میلی‌لیتر` → `ml`, `میلی‌متر جیوه` → `mmHg` |
| `02_hit_university` | 0 → 0 | Adds English `term_en` expansions (`experience`, `user`) that are not in the source |
| `03_cs_algorithms` | 3 → 3 | Adds `notation` |
| `04_persian_only_literature` | 0 → 0 | Clean |
| `05_code_switching_tech` | 1 → 0 | The only `3` is inside the URL `https://docs.python.org/3/tutorial`; the reference drops the URL |
| `06_pptx_slide_outline` | 9 → 0 | Slide numbers 1–8 are structural; `1405` (spring semester) is **content** and the reference drops it |

**Conclusions for the design**

1. Signal extraction must be **unit-aware and digit-form-aware**, or it will
   flag legitimate unit spelling (`میلی‌گرم` → `mg`) as a fabrication.
2. URLs must be atomic tokens; a number inside a URL is not a standalone signal.
3. Presentation slide numbers and chunk markers must be **masked before
   extraction**.
4. `term_en` expansion is outside knowledge. Under the strict contract it must
   be **disabled by default** (see §12, decision D2).
5. The fixtures themselves need a calibration pass (documented, not silently
   changed) before `min_number_coverage: 1.0` can be used as a hard floor.

---

## 4. Research findings and what they change here

The literature separates **faithfulness** (nothing invented) from
**completeness/coverage** (nothing important lost) and **conciseness**. They
must be measured separately; a single "quality score" hides the trade-off.

| Finding | Source | Design consequence |
| --- | --- | --- |
| Fine-grained evaluation scores faithfulness, completeness and conciseness independently, using key-fact alignment | FineSurE, arXiv 2407.00908 | Three separate metrics, three separate gates; never trade one off silently |
| Atomic-fact support is the standard precision measure (decompose → verify) | FActScore, Min et al. 2023 | Numbers/terms are our deterministic atom proxy; no model needed for the hard cases |
| Hallucination in long generations is concentrated **towards the end** | "Hallucinate at the Last", arXiv 2505.15291 | Put required content first in the JSON; cap tips; the tail must be optional |
| Multi-document summarizers hallucinate up to 75% of content; common errors are instruction-failure and **overly generic** insights | NAACL 2025 Findings 293 | Explicit forbid-list and "no generic filler" rule; schema constraints are not enough |
| Grounding triad: restrict to the document, quote first, allow abstention; require retraction of unsupported claims | Anthropic prompting guidance; production grounding guides | Tips carry a verbatim `basis`; unclear content stays unclear; no best-guess rewriting |
| Text-to-table generation needs data-completeness, relevance and structure checks, with **cell-level** verification | arXiv 2508.08653 | Table cells are validated individually; numeric cells are the hard gate |
| Structural table faithfulness fails on numerical values, entity-to-row attribution, invented rankings | TabFaith / STAF, ACL 2026 workshop | Reject a table if any numeric cell or any row/column cannot be traced |
| Map-reduce loses information at chunk compression; larger chunks help recall but not without cost; seams need overlap | LLM×MapReduce, ACL 2025; book-length coherence study (ICLR 2024) | Keep neighbour windows, audit per chunk before merge, do not shrink chunks blindly |
| Clinical documentation frameworks report ~1.5% hallucination and ~3.5% omission even after prompt refinement | npj Digital Medicine 2025 (`s41746-025-01670-7`) | Set realistic targets: zero *numeric* fabrication is achievable deterministically; prose fidelity needs sampling |
| Automated faithfulness metrics correlate only moderately with humans and are prompt-sensitive | "Verify with Caution", 2025; metric surveys | Do not replace deterministic gates with an LLM judge; use a judge only for sampled evaluation |

The repo already reflects two of these: a "raw transcript before notes"
invariant, and a deterministic fidelity gate for typography. This plan applies
the same engineering to the note layer.

---

## 5. The contract: what "complete", "clean", "organize", "add", "remove" mean

These definitions go into the code and the prompt; they are the acceptance
criteria.

### 5.1 Information units (must all survive)

A unit is one of:

1. a definition or a term introduced with its meaning;
2. an explanation, mechanism, or causal "why";
3. a worked example, counter-example, or analogy the lecturer used;
4. a step in a procedure or algorithm;
5. a comparison, classification or attribute list;
6. a number with its unit, dose, range, ratio or threshold;
7. a date, name, acronym, English term, formula, code identifier or URL;
8. a condition, exception, warning, or explicitly stated uncertainty;
9. a result or conclusion the lecturer stated.

### 5.2 Allowed edits ("clean" and "organize")

* Remove filler, false starts, verbal tics, ASR noise, and off-topic chatter.
* Remove verbatim repetition (same wording, same meaning) — once only.
* Fix punctuation, spacing, digit shape, and obvious ASR word boundaries.
* Split long spoken sentences; join fragments that belong to one sentence.
* Create headings from the lecturer's actual topics; group same-topic content.
* Order sections by first appearance; never move a topic across another topic.
* Normalize unit *spelling* (`میلی‌گرم` ↔ `mg`) but never convert values.
* Put definitions/examples/steps/formulas into their fields.
* Build tables and practical tips **from** source content.

### 5.3 Forbidden edits ("do not add or remove")

* Any new fact, statistic, reference, guideline, recommendation, diagnosis,
  dose, example, analogy, comparison or "common knowledge".
* Any number, unit, date, name or acronym not present in the source.
* Any translation of a technical term, or expansion of an acronym, unless the
  source itself provides it.
* Any unit conversion (`mg` → `mmol/L`), rounding, recalculation, or derived
  value (BMI, eGFR, percentages).
* Deleting a definition, explanation, exception, warning, or worked example.
* Collapsing a multi-sentence explanation into one generic sentence.
* "Improving" an ambiguous statement into a confident one; hedges stay hedges.
* Study advice invented by the model ("این بخش را دو بار بخوانید") — a tip is
  only what the lecturer said.

### 5.4 What a "practical tip" is

A tip is a **surfacing** of advice already in the source, in one of these forms:

* an explicit recommendation or imperative ("باید …", "نباید …");
* a warning or safety rule;
* an exam emphasis ("این نکته در آزمون می‌آید");
* a practical/professional rule of thumb the lecturer stated.

Each tip must carry `basis`: a **verbatim** source phrase (5–30 words) from which
the tip is derived. No basis, no tip. If the lecture contains no such sentence,
the list is empty — that is a correct answer, not a failure.

### 5.5 What a table is

A table is allowed only when the source itself compares or enumerates **≥2
items on ≥2 attributes**. Every cell must be:

* a value (number + unit) present in the source, or
* a short phrase whose content words all occur in one source sentence/window.

Headers are the only free text (chosen by the model). No computed cells, no
inferred rankings, no empty invented cells, no merged cells.

---

## 6. Proposed architecture

```
transcript / outline
        │
        ▼
[0] structural mask + signal index          (new, deterministic, free)
        │  numbers+units, dates, Latin terms, URLs, code ids,
        │  advice-sentence candidates
        ▼
[1] map per chunk (prompt v4)                (existing calls, schema v4)
        │  sections + tables + tips(with basis)
        ▼
[2] merge (deterministic, existing) + near-duplicate cleanup (new)
        │
        ▼
[3] signal audit (new)                       (deterministic, free)
        │  omission list with source windows, fabrication list
        ├── pass ──────────────────────────────┐
        ▼                                      │
[4] compile / repair (one existing call)       │
        │  lossless edit + restore missing     │
        ▼                                      │
[5] acceptance gate (extended compile_accepts) │
        │  no fabrication, no loss, structure  │
        ├── fail → deterministic merge ────────┤
        ▼                                      ▼
[6] DOCX renderer (tips, multiple tables)  + quality log
```

### 6.1 New module: `gamas_bot/notes_evidence.py`

Pure Python, no dependencies, no network. Public surface:

```python
@dataclass(frozen=True)
class Signal:
    kind: str          # "number" | "latin" | "url" | "date"
    raw: str
    value: str         # canonical digits, lower-cased token, URL
    unit: str = ""     # canonical unit when attached ("mg", "mmHg", "%", ...)

@dataclass(frozen=True)
class SignalAudit:
    total: int
    covered: int
    missing: tuple[Signal, ...]
    fabricated: tuple[Signal, ...]
    coverage: float
    precision: float

def mask_structural(text: str) -> str            # slide numbers, chunk markers, list ordinals
def extract_signals(text: str) -> frozenset[Signal]
def audit_notes(source: str, notes: StructuredNotes) -> SignalAudit
def validate_table(table: NoteTable, source: str) -> TableVerdict
def validate_tip(tip: PracticalTip, source: str) -> bool
def sentence_coverage(source: str, notes: StructuredNotes) -> CoverageReport
def advice_sentences(source: str) -> tuple[str, ...]   # candidates for tips
```

**Unit canonicalisation** (small, auditable table; extend from real fixtures):

| Source spelling | Canonical |
| --- | --- |
| میلی‌گرم، میلی گرم | `mg` |
| میلی‌لیتر، میلی لیتر | `ml` |
| کیلوگرم | `kg` |
| میلی‌متر جیوه | `mmHg` |
| درصد | `%` |
| گرم بر دسی‌لیتر | `g/dL` |
| میلی‌گرم بر دسی‌لیتر | `mg/dL` |
| میلی‌لیتر بر دقیقه | `ml/min` |
| بار در دقیقه | `bpm` |

Rules:

* Fold Persian/Arabic digits; normalize `٫`, `,` (thousands), and decimal forms.
* Treat `120/80` as one signal (a ratio), not two.
* Keep the unit attached to the number when both sides have one; a value with a
  **different** unit is a fabrication, not a match.
* Mask before extracting: `### اسلاید ۳`, `[بخش ۱ از ۳ …]`, leading list ordinals
  (`۱.`, `- ۲)`), and table-of-contents-style numbering.
* URLs and code identifiers are atomic; numbers inside them are not signals.

### 6.2 Data model changes (`notes.py`)

```python
@dataclass(frozen=True)
class NoteTable:
    headers: list[str]
    rows: list[list[str]]
    caption: str = ""          # new, optional, rendered as "جدول ۱ — ..."

@dataclass(frozen=True)
class PracticalTip:
    text: str
    basis: str = ""            # verbatim source phrase; never rendered
    kind: str = "کاربردی"      # "کاربردی" | "آزمونی" | "هشدار"

@dataclass(frozen=True)
class NoteSection:
    ...
    tables: tuple[NoteTable, ...] = ()   # preferred
    table: NoteTable | None = None       # legacy input, normalised into tables

@dataclass(frozen=True)
class StructuredNotes:
    ...
    practical_tips: tuple[PracticalTip, ...] = ()
```

Compatibility: `from_payload` accepts both `table` (old) and `tables` (new) and
normalises to `tables`; `to_payload` emits `tables`. A legacy `table` is never
lost. Existing tests keep passing with a deprecation path.

### 6.3 JSON schema v4 (model output)

```json
{
  "title": "…",
  "summary": "…",
  "sections": [
    {
      "heading": "…",
      "paragraphs": ["…"],
      "bullets": ["…"],
      "definitions": [{"term": "…", "term_en": "…", "definition": "…"}],
      "examples": ["…"],
      "steps": ["…"],
      "formulas": ["…"],
      "key_points": ["…"],
      "tables": [
        {"caption": "…", "headers": ["…"], "rows": [["…"]]}
      ],
      "callouts": [{"kind": "نکته", "text": "…"}]
    }
  ],
  "practical_tips": [{"text": "…", "basis": "…", "kind": "کاربردی"}],
  "key_points": ["…"],
  "glossary": [{"term": "…", "definition": "…"}]
}
```

`learning_objectives` and `review_questions` remain accepted but are gated (see
§12, decision D3): under strict mode they are emitted only when the source
explicitly supports them, and are dropped by the validator otherwise.

---

## 7. Prompt v4 (draft rules)

The prompt is rewritten around a small number of hard rules instead of
accumulated prose. Full text will live in `notes.py`; the substance:

### 7.1 Role and governing principle

```text
شما ویراستار امین جزوهٔ درسی هستید، نه نویسنده و نه خلاصه‌ساز.
قانون بنیادی: هر جمله، عدد، تعریف، مثال و نکته‌ای که می‌نویسید باید در متن
منبع اثری داشته باشد. کار شما «تمیز کردن، مرتب کردن و قالب‌بندی» است؛
افزودن دانش بیرونی و حذف محتوای آموزشی ممنوع است.
```

### 7.2 Completeness ledger (before writing)

```text
پیش از نوشتن، بی‌آنکه در خروجی بیاورید، این فهرست را از متن منبع مرور کنید و
مطمئن شوید هر مورد جایی در جزوه آمده است:
تعریف‌ها، توضیح مکانیزم و چرایی، مثال‌ها، مراحل، مقایسه‌ها، اعداد و واحدها،
تاریخ‌ها، نام‌ها، اصطلاح‌های انگلیسی، فرمول‌ها، استثناها، هشدارها و نتیجه‌ها.
```

### 7.3 Additions forbidden (explicit list)

```text
این موارد را هرگز اضافه نکنید:
- آمار، مرجع، راهنما، توصیهٔ درمانی، دوز، مثال یا تشبیه تازه؛
- عدد، واحد، تاریخ یا نامی که در متن نیست؛
- ترجمهٔ اصطلاح تخصصی یا بازکردن مخفف، مگر خود متن آن را گفته باشد؛
- تبدیل واحد، گرد کردن، محاسبه یا مقدار مشتق‌شده (BMI، eGFR، درصد)؛
- جملهٔ کلیشه‌ای، نصیحت مطالعه، یا لحن تبلیغاتی؛
- «بهتر کردن» یک جملهٔ مبهم؛ ابهام را همان‌گونه که هست نگه دارید.
```

### 7.4 Cleaning and organization (operational)

```text
تمیز کردن مجاز: حذف پرکننده‌ها («خب»، «یعنی»، «اِ…»)، شروع‌های ناتمام، تکرار
لفظی، نویز پیاده‌سازی و حاشیه‌روی بی‌ربط؛ اصلاح نقطه‌گذاری و فاصله؛ شکستن
جمله‌های بلند گفتاری.
مرتب کردن مجاز: عنوان از موضوع واقعی بخش؛ حفظ ترتیب ظهور موضوع‌ها؛ ادغام
بخش‌های هم‌موضوع؛ آوردن تعریف/مثال/مرحله/فرمول در فیلد خودش.
هر توضیح چندجمله‌ای باید چندجمله‌ای بماند؛ آن را به یک جملهٔ کلی تبدیل نکنید.
```

### 7.5 Tables

```text
جدول فقط وقتی که متن حداقل دو مورد قابل‌مقایسه با حداقل دو ویژگی داشته باشد.
- هر خانه باید عیناً از متن منبع آمده باشد (یا عدد+واحد موجود در متن).
- عنوان ستون‌ها را خودتان انتخاب کنید؛ محتوای خانه‌ها را نسازید.
- محاسبه، میانگین، رتبه‌بندی و خانهٔ استنتاجی ممنوع است.
- حداکثر دو جدول در هر بخش، حداکثر ۸ سطر و ۶ ستون.
- اگر مطمئن نیستید، به‌جای جدول از فهرست استفاده کنید.
```

### 7.6 Practical tips

```text
فقط توصیه‌ها، هشدارها و تأکیدهای عملیِ خود گوینده را بیاورید.
- برای هر نکته، «basis» را عیناً از متن منبع کپی کنید (۵ تا ۳۰ واژه).
- نکته‌ای که پشتوانهٔ لفظی در متن ندارد، نوشته نمی‌شود.
- نصیحت مطالعه، انگیزه‌بخشی و توصیهٔ عمومی ممنوع است.
- حداکثر ۸ نکته؛ اگر متن توصیهٔ عملی ندارد، آرایه خالی بماند.
```

### 7.7 Academic register

```text
لحن: سوم‌شخص، بی‌طرف، فشرده و دانشگاهی. «شما» و «بیایید» و «در این جزوه
خواهیم دید» ننویسید. اصطلاح تخصصی را یکدست به‌کار ببرید. جملهٔ declarative
بنویسید؛ هیجان، تعجب و ایموجی ممنوع است. اگر گوینده احتمال داده، همان احتمال
را نگه دارید (ممکن است، احتمالاً).
```

### 7.8 Compile prompt becomes lossless editing

Replace "rewrite fluently / rebuild transitions" with:

```text
شما ویراستار نهایی هستید و اجازهٔ بازنویسی سبکی ندارید.
- جمله‌ها را تا حد امکان عیناً نگه دارید؛ فقط نقطه‌گذاری، فاصله و پیوندهای
  لازم را اصلاح کنید.
- بخش‌های هم‌موضوع را ادغام کنید و عنوان تکراری را یک‌بار بنویسید.
- تکرار لفظ‌به‌لفظ را حذف کنید؛ دو جمله با کلمات متفاوت و معنای یکسان هر دو
  می‌مانند.
- فهرست «موارد جامانده» را فقط با جمله‌های منبعِ همان فهرست به جای درست
  برگردانید؛ چیز دیگری اضافه نکنید.
- هیچ عدد، واحد، تعریف، مثال، استثنا یا نکتهٔ کاربردی را حذف نکنید.
```

---

## 8. Deterministic verification and acceptance

### 8.1 Metrics

| Metric | Definition | Gate |
| --- | --- | --- |
| `number_recall` | source number+unit signals present in notes | `full` ≥ 0.95, `standard` ≥ 0.85, `summary` ≥ 0.60 |
| `term_recall` | source Latin/acronym/URL signals present | same floors |
| `signal_precision` | notes signals not in source | **1.0 in every mode** |
| `sentence_coverage` | source content sentences with a matching note sentence (content-word overlap) | soft: triggers repair below 0.75 (calibrate) |
| `table_cell_pass` | cells traceable to source | **1.0 for numeric cells; ≥ 0.8 overall, else drop table** |
| `tip_basis_pass` | tips whose `basis` is found in source | 1.0 (invalid tips dropped) |
| `compression_ratio` | note chars / source chars | logged, not gated; a collapse (< 0.15) is a warning |

Notes:

* `signal_precision = 1.0` is achievable because numbers and Latin terms are
  finite, normalization is explicit, and structural masks remove the noise.
* Prose fabrication (a plausible sentence with no number) is **not** reliably
  detectable by a deterministic rule. It is mitigated by prompt constraints,
  sentence coverage, and sampled human/LLM review — not by pretending a regex
  solved it.
* Coverage is a *proxy*. It may under-count legitimate paraphrase; that is why
  it triggers a repair rather than a hard rejection.

### 8.2 Table validation (per table)

1. Normalize headers and cells.
2. Headers: free text, but length-bounded; reject columns with no data.
3. Each cell:
   * empty → allowed;
   * numeric/unit → must exist as a `(value, unit)` signal in the source
     (unit-aware; `mg` and `میلی‌گرم` are equal);
   * text → content words must appear in a single source window (≤ 2 adjacent
     sentences) with ≥ 0.8 containment, **or** the cell is a short label in the
     header allowlist.
4. Reject the whole table if any numeric cell fails, or > 20% of text cells
   fail, or the table has a computed/derived column.
5. Rejected table → not rendered; its values become "missing signals" for the
   repair pass. If repair restores them in prose, completeness is preserved.

### 8.3 Tip validation (per tip)

1. `basis` non-empty and ≥ 5 words.
2. `basis` normalized is a substring of the source, or shares ≥ 0.85 of its
   content words with one source sentence (ASR-tolerant).
3. `text` contains no signal absent from the source.
4. `text` is ≤ 240 chars; `kind` is in the enum.
5. Invalid → dropped (logged), never rewritten by the verifier.

### 8.4 Acceptance gate

Extend `compile_accepts(compiled, merged, source_signals)`:

* existing section/structured/char checks (raise the char floor; see §12 D4);
* `compiled` introduces **no** new signals vs source;
* `compiled` signal recall ≥ `merged` recall (never lose ground);
* if `compiled` fails, deliver `merged` — but only after `merged` itself passed
  the same audit, otherwise log `note_quality=failed` and deliver the best
  audited state.

### 8.5 Repair policy

* Trigger: any hard-gate failure after merge, or `sentence_coverage` below the
  soft floor.
* Where: folded into the compile call for multi-chunk notes; a single dedicated
  call for single-chunk notes (only when failing, so the common path stays at
  one call).
* Input: compact notes JSON + ordered `missing signals` with a ±160-char source
  window + a list of failed checks. No raw transcript beyond those windows.
* Output: full JSON, same schema.
* Re-audit. Accept only if recall improves and precision stays 1.0. One attempt,
  no loops (the repo's "fixed call budget" invariant is preserved).
* Optional (decision D5): a deterministic "remaining evidence" appendix that
  appends the exact source sentences for any still-missing signals. Lossless,
  no model, but stylistically a last resort.

### 8.6 What is never done

* No "quality-driven re-ask" loops.
* No LLM-as-judge in the delivery path.
* No silent deletion of a failing paragraph: fail loudly in logs, degrade at
  the *structured item* level (table/tip), and keep the transcript invariant.
* No change to the user-facing promise: the raw transcript is still delivered
  before notes.

---

## 9. Renderer and delivery changes

| File | Change |
| --- | --- |
| `docx_export.py` | render `practical_tips` as a boxed "نکته‌های کاربردی" section after the key points; render all `tables` with "جدول ۱ — …" captions; include tips/tables/captions in `_sections_chars` and the TOC length heuristic |
| `notes.py::to_markdown` | tips and multiple tables in the Telegram Markdown subset |
| `notes.py::compact_notes_payload` | emit `tables`, `practical_tips` (text + basis) for the compile pass |
| `notes.py` merge | merge tables with identical headers; dedupe tips by normalized text; never merge two tips with different bases into one |
| `bot.py` | no flow change; log the quality report at the existing note log point |

The DOCX design language does not change: same fonts, same RTL rules, same table
style, same callout boxes. New content reuses existing primitives.

---

## 10. Test and evaluation plan

### 10.1 Restore the deterministic corpus benchmark

New `tests/test_note_quality.py`, no network, no provider:

1. **Signal extraction units** — Persian/Arabic digits, decimals, ratios,
   ranges, unit aliases, URLs, code identifiers, slide-number masking.
2. **Reference pass** — each fixture's reference document reaches the declared
   coverage floor after structural masking and unit normalization.
3. **Truncation fails** — a deliberately truncated reference **fails** the same
   check, proving the benchmark detects loss.
4. **Fabrication fails** — a single changed digit, an invented unit, an invented
   date or an expanded acronym is reported as `signal_precision < 1`.
5. **Table validation** — a table with one invented numeric cell is rejected; a
   table of source facts passes; a computed column is rejected.
6. **Tip validation** — a tip with a real basis passes; a tip with a fabricated
   basis or a new number is dropped.
7. **Presentation** — slide numbers are masked; `1405` remains a content signal.
8. **Non-Latin fixture** — `04_persian_only_literature` reports zero signals and
   no fabricated findings (no invented gaps).

Fixture calibration (documented separately): the reference for `01` drops
`1.73` and `06` drops `1405`; either annotate them as intentional omissions or
fix the reference. Do not lower a global floor to hide it.

### 10.2 Extend existing tests

* `tests/test_notes.py` — schema v4 parsing/back-compat, merge of tables/tips,
  compile gate with signals, no-fabrication fallback.
* `tests/test_docx_export.py` — tips box, table captions, multiple tables,
  RTL, heading/TOC accounting.
* Prompt tests — assert the hard rules exist (forbid-list, basis, cell rule),
  without brittle full-text hashing.
* Regression: all 544 current tests must stay green.

### 10.3 New adversarial fixtures (optional but recommended)

| Fixture | Trap |
| --- | --- |
| `07_grounding_traps` | `12.5` vs `12.3`, `mg` vs `mcg`, a URL with a digit, a Persian unit, a slide marker, and **no** practical advice (expect zero tips) |
| `08_practical_tips` | explicit advice, warnings and exam emphasis; one ambiguous sentence that must not become a tip |

### 10.4 Live A/B (needs provider keys)

Add `scripts/eval_notes.py` (or extend `probe_note_providers.py`) to run the six
fixtures through the configured chain and emit a JSON report with the metrics
above. Compare v3 vs v4 prompts on:

* number/term recall and precision;
* sentence coverage and compression ratio;
* table count, table pass rate, tip count, tip pass rate;
* JSON validity, truncation/split count, latency, tokens;
* a blind human rubric on 20 sampled notes: completeness, fidelity,
  professionalism, table usefulness, tip groundedness (1–5 each).

Target: precision 1.0 on numbers, recall ≥ 0.95 in `full`, no increase in
provider failures, latency within the existing 900 s deadline.

---

## 11. Rollout phases (each phase is independently shippable)

| Phase | Deliverable | Status |
| --- | --- | --- |
| **P0 — measurement** | `notes_evidence.py` (signals, masks, audit) + restored corpus benchmark + fixture calibration | **done** — 40 tests in `test_note_evidence.py` |
| **P1 — schema v4** | `PracticalTip`, `tables`, back-compat parsing, merge, markdown/payload | **done** — legacy `table` still read, `table` still rendered first |
| **P2 — prompt v4** | forbid-list, operational cleaning rules, table/tip/academic rules, lossless compile prompt | **done** |
| **P3 — gates + repair** | gate in every path, compile acceptance compares coverage, one repair, number scrub, quality log | **done** — `warn` mode for observation |
| **P4 — renderer** | DOCX tips box + every table with its caption | **done** — 3 new DOCX tests |
| **P5 — evaluation** | adversarial fixtures, live A/B report, threshold tuning | **deferred** — needs provider quota; deterministic benchmark is live |

Total: ~6–7 working days. P0 can land alone without touching generation; P3 is
the only phase that can change failure behaviour, and it is guarded by flags.

### Feature flags (as implemented)

* `NOTE_STRICT_GROUNDING` (default `true`): enables the evidence gate, table and
  tip validation, the repair and the scrub. `false` is the rollback to the
  prompt-only path.
* `NOTE_QUALITY_GATE` (`repair` | `warn`; default `repair`): `warn` runs the gate
  and the audit but neither repairs nor scrubs — the first-week observation mode.
* No `NOTE_PROMPT_VERSION`: the prompt is code, so a half-rollback (old prompt,
  new schema) would be a configuration nobody can reason about. The two flags
  above are the whole surface.
* Thresholds are constants (`notes_evidence.NUMBER_COVERAGE_FLOORS` etc.), not
  environment sprawl.

---

## 12. Decisions (resolved)

| # | Decision | Resolution |
| --- | --- | --- |
| **D1** | **Definition of practical tips.** | **Strict.** Only the lecturer's advice, with a verbatim 5–30-word `basis`; no basis, no tip. A lecture with no advice yields no tips. |
| **D2** | **`term_en` / acronym expansion.** | **Standard equivalents allowed, bounded.** `term_en`, glossary terms, short parentheticals, table cells and code examples may name an English equivalent; `STANDARD_EQUIVALENTS` also allows it when the lecturer said the Persian form (پایتون → Python). Prose Latin is still a fabrication. |
| **D3** | **`learning_objectives` / `review_questions`.** | **Gated.** Emitted only when the lecture's vocabulary supports them (≥0.7 / ≥0.6 content-word support); otherwise removed. |
| **D4** | **Compile strictness.** | Implemented as a **coverage comparison** instead of a raised character floor: the compile is rejected if it invents a signal or covers less of the source than the merge. That is stricter about facts and fairer to legitimate dedupe. |
| **D5** | **Completeness appendix.** | **Not implemented.** The bounded repair plus the fabricated-number scrub make it unnecessary; appending raw transcript sentences under a heading was a stylistic last resort. |
| **D6** | **Live provider access.** | Deterministic benchmark is live; the live A/B (P5) still needs provider quota. |

---

## 13. Risks and mitigations

| Risk | Why it matters | Mitigation |
| --- | --- | --- |
| Over-strict checks flag legitimate transformations | Unit spelling, plurals, computed counts | Unit aliases, digit folding, structural masks, repair instead of hard reject for prose |
| Tables/tips increase output and hit `max_tokens` | Truncation → split → duplication | Caps (2 tables/section, 8 rows, 8 tips), tips last in the schema, existing split path retained |
| End-of-output truncation loses tips | "Hallucinate at the last" and tail cuts | Core sections first; tips are optional tail; cap and log truncation |
| Repair introduces new errors | A second model pass is another chance to hallucinate | Repair input is only the missing signals + source windows; re-audit; precision must stay 1.0; one attempt |
| Coverage proxy penalises paraphrase | False "missing" reports | Soft metric; trigger repair only; log, do not reject |
| Strict mode removes useful extras (review questions, translations) | Product feels thinner | Decisions D2/D3; enrichment is an explicit opt-in, not a silent default |
| Fixture references are themselves lossy | A hard floor would make the benchmark red | Calibrate/annotate the two known omissions first; document it |
| Prompt growth increases cost | Longer system prompt per chunk | Keep rules compact; prompt budget already reserved in `effective_chunk_chars`; measure tokens in P5 |

---

## 14. Appendix — evidence from the repository

* `notes.py:768` — system prompt builder ("compiler, not summarizer").
* `notes.py:622` — mode rules (`full` / `standard` / `summary`).
* `notes.py:637–693` — content, semantic, style and transition rules.
* `notes.py:695` — JSON rules (schema is the only structural constraint).
* `notes.py:812` — compile prompt (free rewriting).
* `notes.py:1363` — `compile_accepts` (section/structured/char ratios only).
* `notes.py:1541` — `structure_document` map/reduce, fixed call budget.
* `notes.py:1477` — `_structure_piece` bounded split recovery.
* `notes.py:115` — `NoteTable`; `notes.py:226` — `StructuredNotes`.
* `docx_export.py:2585` — section rendering; `:2612` — table heading.
* `transcript.py:147` — content fingerprint; `:166` — `guarded` fidelity gate.
* `office_ai.py:362` — substring validation of every proposed table cell.
* `tests/fixtures/notes/README.md` — corpus unused; scorer removed in v3.
* Baseline: 544 tests pass in 14.45 s on this branch.

### External sources

* FineSurE — fine-grained faithfulness/completeness/conciseness:
  <https://arxiv.org/html/2407.00908v1>
* FActScore — atomic-fact precision: <https://arxiv.org/abs/2305.07982>
* AlignScore — factual-consistency metric (context for why we do not use an
  NLI model in-process): <https://github.com/yuh-zha/alignscore>
* "Hallucinate at the Last in Long Response Generation":
  <https://arxiv.org/html/2505.15291v2>
* "From Single to Multi: How LLMs Hallucinate in Multi-Document
  Summarization": <https://aclanthology.org/2025.findings-naacl.293/>
* HALoGEN — atomic-unit verification across domains:
  <https://aclanthology.org/2025.acl-long.71.pdf>
* LLM×MapReduce — chunking/long-range information loss:
  <https://aclanthology.org/2025.acl-long.1341/>
* Book-length summarization coherence (chunk size, hierarchy):
  <https://arxiv.org/pdf/2310.00785>
* Text-to-table with cell-level verification:
  <https://arxiv.org/html/2508.08653>
* TabFaith / STAF — structural table faithfulness:
  <https://aclanthology.org/2026.surgellm-1.21/>
* Clinical documentation safety framework (hallucination/omission rates):
  <https://www.nature.com/articles/s41746-025-01670-7>
* Anthropic prompting guidance — quote first, restrict to documents, allow
  abstention: <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices>
* "Verify with Caution" — limits of automated factuality metrics:
  <https://pith.science/paper/2501.14883>
