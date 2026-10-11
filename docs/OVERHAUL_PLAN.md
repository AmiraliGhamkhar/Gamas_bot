# Gamas Bot v3 — Complete Overhaul Plan

**Priorities, in order: (1) simplicity, (2) performance, (3) best-in-class raw text handling.**

This is a plan for a ground-up rebuild of the bot core while keeping the small set of
modules that are already excellent. Nothing is executed yet — this document is the
contract for the work.

---

## 1. Where the codebase is today

Baseline verified on this branch: **1,271 tests pass in 2m29s** on Python 3.11.

| Fact | Value |
| --- | --- |
| Source lines (47 modules) | **38,581** |
| Test lines (36 files) | 23,472 |
| SQLite tables (12 migrations) | 28 |
| Environment settings in `.env.example` | ~194 |
| Docs files | 21 |
| Runtime dependencies | 15 |

### The product (small, and worth keeping)

```
Telegram upload (audio/video/PPTX) → PyAV media worker → STT → normalize
    → LLM structuring → RTL Word booklet + raw transcript .txt → delivery
```

### The mass (large, and mostly accidental complexity)

| Subsystem | Lines | What it is | Verdict |
| --- | --- | --- | --- |
| `ai/` package | 4,782 | Model registries, capability inference, catalog sync, route "legs", failover graphs, usage tracking, per-job sessions | **Delete** — replace with one thin LLM client |
| `stt_platform/` package | 4,405 | A second, parallel provider platform just for STT (registry, router, policy, quota, quality, benchmarks) | **Delete** — fold into one `stt.py` |
| `admin_ai.py` + `admin_stt.py` | 4,872 | In-Telegram admin UIs to manage the two platforms above | **Delete** — tiny status panel only |
| `bot.py` | 4,050 | Handlers + billing UI + admin UI + payment flows + delivery, in one class | **Rebuild** at ~1/4 the size |
| `database.py` | 3,591 | 28 tables, 12 migrations, ledger, credentials, audit, attestations | **Rebuild** at 6–8 tables |
| `structuring.py` + `editorial.py` | 3,612 | Chunk → context → compile → repair passes → truncation re-asks | **Rebuild** as map-reduce, ~1/5 the size |
| `qa.py` + `units.py` | 1,460 | Deterministic note-quality gates, semantic coverage scoring | Reduce to a cheap sanity check |
| `docx_export.py` | 2,997 | RTL booklet + optional LibreOffice TOC page-number pass | **Simplify** (drop the soffice/pypdf path) |
| `config.py` | 1,190 | Parses and validates ~194 settings | **Rebuild** at ~30 settings |

### What is genuinely good and survives as-is

`textnorm.py` (160), `bidi.py` (162), `telegram_text.py` (267), `media.py` (467),
`media_worker.py` (334), `presentations.py` (490), `progress.py` (305),
`instance_lock.py` (93), `logging_config.py` (102) — **~2,400 lines** of careful,
documented primitives (Persian normalization, mixed-script runs, UTF-16 pagination,
PyAV subprocess isolation, zip-bomb guards, flood-safe progress).

These are the foundation the rebuild stands on. They move into v3 unchanged.

---

## 2. Target architecture

### 2.1 Module map with line budgets

```
gamas_bot/
  __main__.py          ~60    entrypoint + self-check
  config.py            ~280   ~30 env settings, validated once at boot
  bot.py               ~900   handlers, menus, delivery (user-facing only)
  jobs.py              ~200   bounded queue, worker pool, clean shutdown/recovery
  db.py                ~450   aiosqlite, WAL, 6–8 tables, one migration file
  stt.py               ~600   Speechmatics / Deepgram / OpenAI-compatible,
                             ordered failover, word timings, retries
  transcript.py        ~450   ★ THE RAW TEXT ENGINE (see §4)
  llm.py               ~350   one client: OpenAI-compatible + Gemini (+Anthropic)
  notes.py             ~500   map-reduce structuring, no repair loops
  docx_export.py      ~1400   RTL booklet, link-only TOC, no soffice
  billing.py           ~150   (only if billing is kept — Decision B)
  admin.py             ~400   small: stats, users, broadcast, health
  ── kept unchanged ──
  textnorm.py, bidi.py, telegram_text.py, media.py, media_worker.py,
  presentations.py, progress.py, instance_lock.py, logging_config.py
```

**Target: ~7,500 source lines — an ~80% reduction** — with the same user-facing
product and better raw text output.

### 2.2 Database: 28 tables → 6–8

Keep (with billing): `users`, `submissions`, `transcriptions`, `plans`,
`payment_requests`, `ledger` (one append-only events table).
Without billing: `users`, `submissions`, `transcriptions` (3 tables).

The reserve → consume/release guarantee for credit survives (it is the one billing
invariant worth keeping); the other 20+ tables (provider registries, model catalogs,
quota snapshots, routes, attestations, snapshots, events) exist only to support the
deleted platforms and go away. One fresh migration set; the v2 database is not
carried over (see §7 Risks).

### 2.3 Configuration: ~194 settings → ~30

```
Telegram:    TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, ADMIN_IDS
STT:         STT_PROVIDERS (ordered list, e.g. "speechmatics,deepgram"),
             per-provider KEY/BASE_URL/MODEL/LANGUAGE (≤10 vars)
LLM:         NOTE_PROVIDER, NOTE_BASE_URL, NOTE_API_KEY, NOTE_MODEL
Limits:      MAX_FILE_SIZE, MAX_ACTIVE_JOBS, MAX_QUEUED_JOBS, JOB_TIMEOUT
Raw text:    RAW_TEXT_PROFILE (plain | timestamped), TIMESTAMP_PARAGRAPHS
Misc:        DB_PATH, TEMP_DIR, LOG_LEVEL, (billing vars if kept)
```

Provider order *is* the failover policy — no route tables, no admin route editors.
First configured-and-healthy provider wins; on failure, next in the list.

### 2.4 Dependencies: 15 → 9

```
Telethon, aiohttp, aiosqlite, python-dotenv, av, python-pptx, python-docx,
ppt2pptx, lxml
```

Dropped: `Pillow`, `arabic-reshaper`, `python-bidi`, `pypdf` (all four exist only
for the offline page renderer / soffice TOC page-number path), and `cryptography`
(unless the encrypted admin credential store is kept — Decision B).

---

## 3. Performance plan

The bottleneck today is not SQLite or Telegram I/O — it is **provider round-trips**
and **everything waiting for everything else**.

1. **Progressive delivery.** The raw transcript `.txt` is sent **immediately after
   STT completes**, before note generation starts. Today the user waits for
   structuring + QA + compile + DOCX before seeing anything. This makes the most
   important artifact (the raw text) arrive minutes earlier and means an LLM outage
   can never delay it.
2. **LLM call count: ~5–15 per job → N+1.** Current pipeline per chunk: structure
   call + repair pass (if QA fails) + possible truncation re-asks, plus a compile
   pass and context/outline calls. v3: exactly one map call per chunk plus one
   reduce call. For a 1-hour lecture: ~7 calls instead of up to ~15+.
3. **Parallel map calls.** Chunk calls run concurrently (bounded, default 2–3), so
   wall-clock latency ≈ 3–4 sequential call times regardless of lecture length.
4. **Punctuation at the source.** Ask the STT engine for punctuation + smart
   formatting (Speechmatics enhanced, Deepgram `smart_format`) instead of repairing
   text afterwards. No post-hoc LLM cleanup pass exists in v3.
5. **Word timings kept from the provider response** and used locally for
   paragraphing (§4) — free structure, zero extra calls.
6. **Faster boot & tests.** Config parsing goes from 1,190 lines to ~280; the test
   suite targets **< 45 s** (from 2m29s) by dropping the platform tests and keeping
   focused contract tests.

---

## 4. The raw text engine — `transcript.py` (priority #3)

Today "raw text" is whatever the engine returns, lightly normalized, written to a
`.txt`. v3 makes it a first-class pipeline stage with a spec and tests.

### 4.1 Input contract

```python
@dataclass
class Word:
    text: str
    start: float | None   # seconds from provider, when available
    end:   float | None

@dataclass
class RawTranscript:
    words: list[Word]     # empty when the provider gives plain text only
    text: str             # always present (fallback path)
    confidence: float | None
```

### 4.2 Paragraphing (deterministic, from timings when present)

- **Sentence break:** terminal punctuation (from the engine), or an inter-word gap
  > 700 ms when unpunctuated.
- **Paragraph break:** a gap > 1.6 s, a Persian discourse/topic-shift marker
  («حالا», «خب», «فصل بعدی», «بعد از این», …) at a sentence start, or a soft cap
  of ~600 characters per paragraph.
- No timings → fall back to engine punctuation + the 600-char cap. Never invented.

### 4.3 Cleanup rules — typography only, never content

- Persian punctuation spacing: `، ؟ ؛ .` attached to the preceding word, one space
  after; quote normalization to «…»; collapse repeated terminators (`。。。` → `…`).
- Persian letter folding and whitespace collapse: reuse `textnorm.normalize_display`
  (kept module — Arabic YEH/KAF/TEH MARBUTA → Persian, ZWNJ preserved).
- **Never** removed or altered: words, digits, Latin tokens (`HbA1c`, `mg/dL`),
  formulas, ZWNJ. Fillers stay — cleaning content is the STT engine's job.

### 4.4 The fidelity gate (hard invariant)

Before/after every transform, count: digit groups, Latin tokens, ZWNJ occurrences,
and total non-space characters. If any count changes, the transform is rejected and
the pre-transform text is delivered. The raw text can be *reformatted*, never
*lost*. (This generalizes the invariant already proven in `qa.py`/`units.py` into
the raw path.) Property tests fuzz this with mixed-script fixtures.

### 4.5 Output profiles

| Profile | Behavior |
| --- | --- |
| `plain` (default) | Paragraphed, cleaned text; UTF-8, LF, no BOM, logical order |
| `timestamped` | `[mm:ss]` at each paragraph start (only when timings exist) |

Both written with the existing `متن خام - {reference}.txt` naming, plus an inline
chat preview (~800 chars) when delivered. (Markdown profile and plain-text *input*
handling are Decision D below.)

---

## 5. Notes pipeline (simplified, keeps graceful degradation)

- **Map:** transcript → chunks (~22k chars, boundary-aware, reusing the kept
  splitting logic) → one strict-JSON structured-notes call per chunk, run
  concurrently.
- **Reduce:** one editorial call merges chunk notes (dedupe, transitions), with the
  deterministic merge from `editorial.py` as fallback if the call fails.
- **No repair loops, no QA-driven re-asks, no truncation re-sends.** A bad chunk is
  accepted, or the whole notes step degrades to the raw transcript — which by then
  the user already has (progressive delivery), so degradation costs nothing.
- Prompt, JSON schema and note dataclasses shrink to one screen each; the DOCX
  renderer consumes the same structures as today.

---

## 6. Execution phases

Each phase ends with the suite green, `ruff` clean, and a commit on this branch.

| Phase | Deliverable | Test focus |
| --- | --- | --- |
| **0. Baseline** | ✅ Done — suite green (1,271 tests / 2m29s), venv working | — |
| **1. Skeleton** | New `config`, `db`, `jobs`, `bot` (intake + menus), media modules moved in; bot boots and accepts uploads | queue bounds, back-pressure, shutdown/recovery |
| **2. STT + raw text** ★ | `stt.py` (3 providers, failover), `transcript.py` engine, `.txt` + preview delivery | timing fixtures, fidelity gate property tests, failover |
| **3. Notes** | `llm.py`, `notes.py` map-reduce, degradation path | chunking, JSON contract, concurrency bound |
| **4. DOCX** | Simplified `docx_export.py`, link-only TOC | ported RTL contract tests |
| **5. Billing/admin** | Per Decision B/C below | ledger reserve/release invariant |
| **6. Demolition** | Delete `ai/`, `stt_platform/`, `admin_ai/stt`, 20+ DB tables, ~160 env vars, prune docs to README + ARCHITECTURE + DEPLOY + this plan's successor | full suite re-run |
| **7. Verification** | `compileall`, `ruff`, `mypy` (core), full suite, offline benchmarks, CHANGELOG, PR to `main` | — |

Ordering note: Phase 2 lands *before* notes because raw text is the stated priority
and is independently shippable (STT → transcript file already helps a user).

---

## 7. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| **Data migration** — v2 DB has 28 tables; v3 has 6–8 | v3 starts a fresh database. If needed, a one-shot import script maps `users`/`entitlements`/`ledger` into the new schema; nothing else carries over. Decision B confirms whether billing data must migrate. |
| **Behavior regressions** the old 23k-line suite pinned | Port the ~25 genuinely behavioral test modules (textnorm, bidi, pagination, docx RTL, media intake, zip-bomb, queue, billing-ledger); drop tests that pin deleted platforms. Every kept invariant is re-pinned against v3 code. |
| **Failover becomes too simple** (no health cache, no cooldowns) | Ordered provider list + per-provider cooldown (a provider that just failed is skipped for N minutes, in memory). No registry, no persistence. |
| **JSON fragility from LLM** | Strict schema + one deterministic retry with a "return valid JSON only" prepend; then degrade to raw transcript (already delivered). |
| **Single-process SQLite under load** | Unchanged from v2 (WAL, single writer); not the bottleneck at this scale. |

---

## 8. Decisions (confirmed 2026-10-12)

| # | Decision | Answer |
| --- | --- | --- |
| **A** | Scope | **Rebuild in place** on this branch. |
| **B** | Billing | **Keep, simplified.** Plans, card-to-card payments with receipts, and the append-only ledger with the reserve → consume/release invariant. ~8 tables total; a one-shot `scripts/import_v2.py` maps users/credit/ledger from a v2 database. |
| **C** | Provider credentials | **Environment only.** Ordered failover via `STT_PROVIDERS` / `NOTE_PROVIDER`; in-memory cooldowns; `cryptography` dependency dropped. |
| **D** | "Best raw text handling" | **The text that is passed to the LLM.** The transcript engine's primary output is the LLM input document: typographically cleaned, deterministically paragraphed (from word timings), fidelity-gated, then split boundary-aware with neighbour context so nothing is lost at chunk edges. The `.txt` deliverable is the same artifact (§4). |
| **E** | DOCX | **Polished RTL document, no LibreOffice.** RTL + right-aligned, mixed Persian/English runs via bidi splitting, real tables, page numbers in the footer (native Word field), proper margins, cover page, heading styles, link-only TOC. `pypdf`/`Pillow`/`arabic-reshaper`/`python-bidi` dropped. |
