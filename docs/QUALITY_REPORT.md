# Audit and quality report — lecture-note pipeline and DOCX booklet

Second audit round on `AmiraliGhamkhar/Gamas_bot`. Baseline commit `39cfdc5`,
branch `arena/01a10267-gamas-bot`. Everything below was measured on the local
repository (deterministic, provider-free) plus visual inspection of a rendered
booklet; no model provider credentials were available in this environment, so
the two *model* passes are bounded by their deterministic acceptance gates
rather than scored live.

---

## A. Root causes found in the audit

1. **A part did not know what it was about.** `build_context_block` told a chunk
   its position and the lecture's topic list, but never which topic *that part*
   owns. Every chunk therefore introduced itself as a summary of the whole
   lecture, and the merge had to repair that afterwards.
2. **A part did not know how it sat in the lecture.** Nothing distinguished a
   part that opens mid-topic (so it must link, not introduce) from one that
   starts a topic; nothing distinguished a part that ends mid-explanation from
   one that finishes a subject. The prompt's advice was therefore generic.
3. **One oversized final pass was skipped.** The single global editorial pass —
   the only step that produces a coherent document out of per-part drafts — was
   *skipped* whenever the merged notes exceeded `COMPILE_MAX_CHARS`. The longest
   lecture, exactly the one that needs it, was delivered as the raw merge.
4. **The repair pass could silently rewrite good parts.** Acceptance was decided
   on the merged document's averages, so a repair that fixed one part and
   damaged another was accepted; and `_repair_is_better` compared the *values*
   of the missing-number tuple element-wise, rejecting correct repairs whose new
   missing values sorted higher.
5. **The orientation digest silently dropped the tail of a long lecture.** A
   fixed per-part slice plus a whole-document truncation meant the last parts —
   whose topics the digest exists to provide — were cut off, and the positional
   topic map then mismatched the parts.
6. **Quality diagnostics were blind to whole-explanation loss and to structure.**
   Number/term coverage caught dosage corruption but not a deleted explanation;
   there was no measure of untitled sections, abrupt section openings, split
   topics or fragment-only prose.
7. **The DOCX's navigation layer was either missing or unhelpful.** A TOC was
   written for any document from three sections up — including short notes where
   it costs a page — and with `\o "1-2"` it was dominated by block labels
   («تعریف‌ها»، «مثال‌ها») that repeat in every section. Long documents that had
   no headings at all could still earn an empty TOC page. The cover's metadata
   was a single long `•`-joined line that wrapped and stranded the tracking
   reference.

## B. Files changed

| file | change |
| --- | --- |
| `gamas_bot/editorial.py` | per-part topic ownership, continuity flags, budget-aware outline digest, segmented compile document, section-boundary slicing |
| `gamas_bot/structuring.py` | multi-slice final compilation, per-part repair acceptance, missing-number comparison fix, context block without an outline |
| `gamas_bot/qa.py` | untitled-section and abrupt-opening diagnostics (content-free words, relation markers) |
| `gamas_bot/docx_export.py` | TOC policy (levels + two-signal threshold), cover metadata lines, adaptive cover spacing, body-section helper, logo passthrough |
| `gamas_bot/config.py` | `DOCX_TOC_LEVELS`, `DOCX_TOC_MIN_SECTIONS` |
| `.env.example`, `README.md` | document the two new settings |
| `scripts/benchmark_notes.py` | new structure fields, part-context probe, richer console summary |
| `tests/test_global_compilation.py` | part-context, outline-digest, segmented-compilation and targeted-repair tests |
| `tests/test_note_quality.py` | coherency-diagnostic tests |
| `tests/test_docx_polish.py` | TOC levels/threshold, cover metadata/spacing, raw-text TOC tests |
| `tests/test_fidelity_upgrade.py` | every documented `DOCX_*` switch reaches the design object |

## C. Architectural changes (smallest change that fixes the root cause)

**Per-part topic ownership.** `LectureContext.topic_for(index, total)` maps the
outline's positional topic list onto a part — and returns `""` when the list
length does not match the part count, because a global topic list used
positionally would label a part with someone else's subject. `build_context_block`
now states «موضوع همین بخش» and `CHUNKED_CONTENT_RULES` says that line is this
part's responsibility; parts no longer repeat their neighbours' subjects.

**Continuity flags without an extra model call.** `chunk_continuity()` derives two
facts from the part's own text: it opens mid-topic (first line is a continuation
fragment: «و …»، «بنابراین …») and it ends mid-topic (no sentence terminator).
Both flags are suppressed for punctuation-free material (raw STT), where they
would fire on every part and become noise. The context block turns each flag
into one linking instruction. No overlap was introduced: the merge and the
context block proved sufficient.

**Budget-aware outline digest.** `build_outline_document` now lowers the per-part
slice as the part count grows, keeps the total inside the budget whenever the
floor allows, and drops only the optional tails — never a part — if the digest
would still overflow. The orientation answer therefore always covers every part
positionally.

**Segmented final compilation.** `split_for_compilation()` cuts the merged notes
at section boundaries into consecutive slices that each fit one request
(`COMPILE_MAX_CHARS` minus the measured system prompt and a reserved overhead).
`build_compile_document(..., part=, parts=)` tells each slice it is a slice, so a
slice writes the summary, key points and glossary for *its own* sections instead
of a whole-lecture summary from a fragment. Each slice is accepted on its own
deterministic QA gate; accepted slices are merged with the existing conservative
merge, which unions summaries, key points, glossary, objectives and questions and
keeps the lecture's order. The slice count is bounded (`COMPILE_MAX_SLICES = 6`);
beyond it the merged notes are delivered unchanged, exactly as before.

**Targeted repair.** `_choose_repaired_draft()` judges every repaired part against
its own draft, with the same ordering as the document gate and "no measurable
improvement → keep what exists" ties. A repair that fixes one part can no longer
overwrite a part that was already right.

**Diagnostics that detect but never rewrite.** `qa.py` now counts untitled
sections (placeholder headings like «بخش ۲») and abrupt section openings: a
non-first section that neither opens with a relation marker
(چون/بنابراین/اما/برای مثال/…) nor shares a content word with the previous
section's body. Word matching strips ZWNJ and uses a Unicode letter tokenizer, so
Persian is measured correctly.

## D. DOCX improvements

* **TOC policy with two independent signals.** A TOC is written when the document
  has enough peer sections *or* enough body text
  (`TOC_MIN_SECTIONS = 4`, `TOC_MIN_BODY_CHARS = 2400`), and never when the notes
  path has no sections — and never for a raw-text document with fewer than two
  Markdown headings, which would have rendered an empty TOC page. Default
  `\o "1-1"`: block labels are real Heading 2s that repeat in every section, so a
  `1-2` TOC buried the lecture's own topics. `DOCX_TOC_LEVELS` (validated against
  `1`, `1-1`, `1-2`, `1-3`) and `DOCX_TOC_MIN_SECTIONS` expose both.
* **Cover metadata as labelled lines.** `cover_meta_lines()` splits the date,
  source, engine and tracking reference into two or three short self-contained
  lines, so every label stays next to its value; the raw-text companion file
  keeps the single-line form.
* **Adaptive cover balance.** The flexible gap above the quotation now shrinks by
  one line for each extra title line (`_wrap_estimate`) and for a longer metadata
  block, bounded at five lines, so the quotation keeps its place on the page for
  any job metadata.
* **One body-section helper.** `_add_body_section()` creates the A4 body section
  and applies the section-level `w:pgBorders`; `build_notes_docx`,
  `build_plain_docx` and the cover-less title block all use it, and a configured
  local logo now also appears in the cover-less document.

Unchanged by design: real Word `PAGE` field footer, subtle running header, real
Heading 1–3 styles, the directional-run (RTL/LTR) strategy, table header
repetition, keep-with-next headings, no remote assets, no dynamic font download,
no failure on a missing logo.

## E. Prompt and quality improvements

* The chunk prompt now names the part's own topic and marks it as that part's
  responsibility, and — through the context block — says whether the part starts
  mid-topic or ends unfinished, with the linking instruction for each case.
* `CHUNKED_CONTENT_RULES` states that the outline topics are a map, not text to
  rewrite, and that a part must not repeat its neighbours' subjects.
* The compilation instruction now covers slices coherently (summary/key points
  scoped to the slice, glossary optional) and keeps the "no content deletion,
  only verbatim duplicate repetition" contract intact.
* Full-mode prompt measured at 6 256 characters with preservation, no-invention,
  number protection and transition-keeping all present, and no compression ask.

## F. Tests executed and results

```
BEFORE  python -m unittest discover -s tests     ->  Ran 471 tests in 24.275s   OK
AFTER   python -m unittest discover -s tests -v  ->  Ran 488 tests in 25.613s   OK
```

New coverage: part context states the part's own topic and ignores a mis-sized
topic list; the outline digest labels every part under a tight budget; an
oversized booklet is compiled in labelled slices and too many slices fall back to
the merge; a damaged repair slice never replaces its own draft; untitled and
abrupt sections are reported (and a linked section is not); TOC levels and
threshold are configurable; the cover's metadata is split into labelled lines and
its quotation keeps its place with a long title; a raw-text document gets a TOC
only when it has headings; every documented `DOCX_*` switch reaches the design.

Visual validation (mandatory, and repeated after the last edit):

```
PYTHONPATH=. python out/mkbooklet.py            # 31 fixture sections -> 50 901 B DOCX
python -m scripts.render_docx_pages out/booklet_after.docx \
       --out out/pages_final --json out/facts_final.json
-> pages=13 sections=2 warnings=0, ok=true, page 826x1169 px
```

All 13 pages were inspected (cover, TOC, 11 body pages): «به نام خدا»، GAMAS /
Gamas Bot, title, mode, date/source/engine/tracking lines, the verbatim quotation
panel; running header «Gamas Bot — جزوه درسی | …», footer «Gamas Bot — صفحه n»
(cover unnumbered, body restarts at ۱), section-level page frame on every page,
sections ۱…۳۱ with shaded key-point panels, ◆ definition lists, warning/tip
callouts, formulas, numbered steps and tables whose shaded header row repeats on
the continuation pages; Latin terms inside Persian sentences stay LTR-correct
(`HbA1c`, `eGFR`, `SELECT`, `O(n log n)`); no blank page, no clipping, no orphan
heading, no bottom-margin overflow.

## G. Benchmark — BEFORE vs AFTER (deterministic, provider-free)

| measure | BEFORE (`39cfdc5`) | AFTER |
| --- | --- | --- |
| unit tests | 471 pass | **488 pass** |
| full-mode prompt chars | 6 256 | 6 256 (preservation/no-invention true) |
| fixture compression / signal / semantic | 1.007 / 1.0 / 1.0 | 1.007 / 1.0 / 1.0 |
| fixtures needing repair | 0 | 0 |
| merge probe (60 → 30 sections, 128 blocks) | 0.0 duplicate rate | 0.0 duplicate rate |
| continuation probe joins | 3/3 correct | 3/3 correct, 0 wrong, 0 prose losses |
| long lecture (61 660 chars) | 3 chunks, 83 → 81 sections, 0 split topics | 3 chunks, 83 → 81 sections, 0 split topics, 0 duplicate blocks |
| long-document DOCX facts | cover, TOC, PAGE field, page borders, Heading 1–3 | same, TOC now `1-1` |
| document structure diagnostics | not measured | 30 sections, 0 split topics, 0 untitled, 0 duplicate paragraphs, 1 repeated heading, 2 abrupt openings (real: two fixtures share «جمع‌بندی») |
| part context (new) | not measured | own topic known, context 526 chars, continuation/unfinished hints emitted, silent on punctuation-free STT |
| renderer | 11 pages, 0 warnings | 13 pages, 0 warnings, every page inspected |

A shorter document is not treated as an improvement anywhere: compression is read
together with semantic and signal coverage, and the booklet is longer than the
earlier one because the segmented compilation now always runs.

## H. Remaining limitations

* The two *model* passes (orientation, compilation) cannot be scored live without
  provider credentials; their downside is bounded by the deterministic
  acceptance gates, but a readability gain from real output still needs a human
  reviewer (`--human-out`).
* Continuity flags depend on the punctuation the STT engine produced; on
  punctuation-free transcripts they stay silent by design rather than guessing a
  boundary.
* The compile slice count is bounded (6). A pathological booklet above that bound
  is delivered as the deterministic merge, which is the previous behaviour, not a
  regression.
* The offline renderer does not evaluate Word fields, so the TOC page renders as
  the heading plus the update hint; the real `TOC \o` field is asserted on the
  document XML instead.
* `Sections that open without a link to the previous one` can fire on a genuinely
  new subject; it is a diagnostic only, never a repair trigger.

## I. Changes intentionally NOT made

* **No automatic chunk overlap.** The global context (topic, neighbours, previous
  headings, continuity flags) plus the merge and the compilation were sufficient;
  overlap would duplicate boundary material into the merge.
* **No vector store, embeddings, RAG or agent framework**, no new provider, no new
  dependency.
* **No destructive QA filters.** The new diagnostics detect; only strong evidence
  of information loss (coverage) or a measurable structural defect (acceptance
  gates) can trigger a targeted repair.
* **No full regeneration on repair**, and no second rewrite pass: the repair is
  still one optional pass, now judged part by part.
* **No schema expansion** beyond what the renderer consumes: `learning_objectives`
  and `review_questions` already existed and remain the only optional fields.
* **No change to Telegram, STT, media, database or deployment code** — the audit
  confirmed the delivery path builds `DocumentMeta` and falls back to a plain
  document on any renderer failure, which is the required behaviour.
