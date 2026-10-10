# Changelog

## Unreleased — Nara-first routing, five-panel admin IA, bounded lists

### Changed

* Three deferred imports in `admin_ai.py` were hoisted into the module's existing
  import block — `suggest_replacement`, `probe_provider_quota` and, worst of the
  three, `chunk_char_budget` which sat **inside** the plan panel's per-leg loop and
  was therefore looked up once per route on every redraw. Each of the three was
  provably safe (the module already imported those submodules at top level, so the
  laziness bought nothing). The deferred `from .stt import …` calls in `admin_stt.py`
  were left alone **after testing the reason**: hoisting them would *not* create an
  import cycle — `gamas_bot.stt` and everything it pulls in never reach
  `gamas_bot.bot`, and `admin_stt` imports on its own — but the STT runtime drags in
  aiohttp, `stt_platform` and `structuring`, and the offline consumers that import
  the panel alone (the benchmark harness, `tests/test_admin_stt.py`) currently do not
  pay for that, while `bot.py` imports `.stt` at top level anyway. No upside, so no
  change. None of this is a measured speed-up either: an import is a dictionary
  lookup, and no number is claimed for it.
* **The note route is now Nara first, OpenRouter second.** `DEFAULT_NOTE_ORDER`
  is `nara → openrouter → gemini → groq → mistral → sambanova → zai → nvidia →
  cloudflare`, seeded for all four note tasks plus the health and benchmark
  routes. A leftover `NOTE_API_PROVIDER` only keeps its provider in position
  zero in legacy mode (`AI_ROUTING_ENABLED=false`) or when it is a custom
  gateway the reviewed default does not know; in that second case it is seeded
  first so an existing integration is never stranded. Order is a preference:
  `plan()` still filters every leg through the real eligibility gates and
  reports each skipped provider with a machine code.
* **The admin panel has five destinations instead of a wall of buttons**:
  overview and statistics, users and billing, AI note generation, speech
  recognition, operations and security. Route management exists in exactly one
  place per platform (inside its own panel) — the root menu used to offer a
  second, competing entry to the AI routes. Nothing was dropped: every callback
  that the old flat menu had is reachable, and
  `tests/test_provider_health.py` now guards that reachability structurally.
* **The user menu leads with the one action that matters.** "📎 ساخت جزوه" is
  the primary button; help and formats are one screen; balance and subscription
  are one account screen (purchasing stays inside it); privacy is unchanged and
  Back/Cancel/Home behave the same everywhere.
* **The billing and special-user screens page instead of stopping.** Pending
  receipts page 20 at a time (`admin:payments:<page>`: a footer message with the
  total and prev/next, and only the current page's receipts are sent), the plan
  list pages 10 at a time (45 custom plans used to be rendered into one message
  and one keyboard, passing both the 4 096-character and the 100-button caps),
  and the special-user list gives every row it shows its own remove button
  instead of offering 12 buttons for 30 listed users.
* **Large lists are paginated instead of truncated**: AI and STT model
  catalogs, the AI and STT event logs, the route panel's "add a provider" list,
  both credential screens (the AI key panel and the operations digest), the STT
  quota screens and the user list. Panels show a page indicator, keep the
  provider/task/service context across redraws, and every row action
  (deprecate, paid-billing toggle) returns to the page it was clicked from. This
  is not only cosmetic: a keyboard is capped at 100 buttons and a message at
  4 096 characters, so an uncapped list stops being sendable at all once a
  deployment grows.
* **The AI event-log page states how much history it is paging.** Its total now
  comes from one `ai_event_count()` call that mirrors the page query's WHERE
  clause exactly — including the "status class below 100" convention — so the
  header can say «صفحهٔ X از Y (N رویداد)». A stale token for a page past the end
  clamps to the last real page instead of rendering an empty screen.

### Fixed

* **A disabled route row is a disabled route, not a paid leg.** `route()` folded
  `enabled` into the `free_only` flag, so switching a provider off while paid
  fallback was allowed silently turned it into a *billable* attempt. The row
  flag is now carried on the leg and skipped as `route_disabled`.
* **A failed route read could delete an administrator's configuration.**
  `ensure_seeded()` treated "could not read" as "nothing seeded" and re-inserted
  defaults over the top; it now skips the task and writes nothing.
* **Unconfigured preferred providers no longer strand a usable one.** The job
  truncated the route to `AI_MAX_PROVIDER_FAILOVERS + 1` legs *before* checking
  whether a provider had a credential at all, so on the long default route a
  deployment with only a tail provider configured failed without ever trying
  it. Legs that cannot be attempted are now pruned first, and the plain
  truncation (with its "no credentials configured" error) is kept when nothing
  is prunable.
* **A provider-specified pause is honoured instead of hot-looped.** A 429 whose
  `Retry-After` is longer than the provider's retry cap no longer burns another
  in-leg retry and the next credential; the credential goes into cooldown for
  the full `Retry-After` and the route moves on.
* **A provider-wide fault no longer walks the credential pool.** 5xx, transport
  and response-contract failures are properties of the provider, not of one key,
  so the remaining keys are no longer spent on the same leg; per-key failures
  (auth, per-key quota) still rotate.
* **One slow provider can no longer consume the whole job.** Each route leg now
  carries a wall-clock budget (`clamp(timeout × 2, 60 s, 600 s)`); an attempt
  that cannot finish inside it is not started, and the leg fails over instead of
  queueing through the rest of the pool and the retries.
* **The overview screens were slow and their two numbers were not comparable.**
  The AI overview ran two queries over *different* windows and then sorted every
  latency in Python; `ai_usage_overview()` now computes the counters and the
  bounded p95 sample in one pass over one window.
* **Opening the health panel contacted providers on a cold cache.** It now reads
  the persisted snapshot (expired rows are labelled as stale) and only the
  explicit "🔄 بررسی همه" action probes.
* **A route row that cannot run was shown as healthy.** The route panel's
  per-row mark came from the provider's billing class, so a saved row blocked
  by a gate (ToU, region, attestation, trial, disabled leg) — or with no ready
  credential at all — still read 🟢. `show_routes()` now asks the authoritative
  planner for the task and annotates those rows with `⚠️` plus the Persian
  reason and machine code, reusing the router's own credential-pool predicate
  for the "no ready key" case (an unattested key stays flagged; attesting it
  clears the warning). The redraw pays `plan()` for it: 2.9 ms → ~73 ms in the
  benchmark database, database-only, no provider request.
* **The STT quota screen invented its own total.** `stt_quota_latest()` ended in a hard
  ``LIMIT 20``, the combined overview sliced that to its three display rows, and the note
  beside them printed the length of the *read* — so a provider holding more than twenty
  quota rows was told «آخرین 3 از 20», and the advice to open the provider's own screen to
  see the rest led to a screen running the same 20-row read. `Database.stt_quota_count()`
  now supplies the real total (`stt_quota_latest()` takes an explicit `limit`, default kept
  at 20), `admin_stt.QUOTA_SNAPSHOT_ROWS` bounds the provider screen and says
  «20 مشاهدهٔ اخیر از N … ادامه در جدول stt_quota_snapshots است» when it cuts, and the
  overview states «آخرین 3 از N مشاهدهٔ سهمیهٔ این ارائه‌دهنده». Three tests in
  `SttPanelTests` pin the counted label, the row count actually rendered, and that short
  history carries no cut note at all.
* **A sampled p95 was printed as if it described the whole window.** `ai_usage_overview()`
  reads at most `Database.LATENCY_PERCENTILE_SAMPLE` (5 000) newest rows for the
  percentile — by design, so a busy day does not sort every latency in Python — and it
  already returned `latency_samples`, but no panel used it. Three screens therefore
  printed «تأخیر p95: X ms» beside a request counter for the full window, and on a
  deployment over 5 000 requests a day those two numbers described different
  populations. The overview now also returns `latency_sample_capped` (a *saturated*
  read, which is the honest signal — a short sample can also just mean rows without a
  recorded latency), and `latency_sample_note()` appends «(نمونه: 5/000 درخواست اخیر)»
  to the AI platform home, the AI usage panel and the admin overview line whenever the
  cap applied. Nothing was removed or widened: the sample stays, the label says what it is.
* **The admin credit screen could show a balance that billing did not honour.**
  `admin_user_credit_overview()` computed the free/paid split as a Python sum over
  `… ORDER BY e.id DESC LIMIT 50`, so once a user's entitlement history passed fifty rows
  a still-spendable grant silently stopped counting — measured on a synthetic user with
  57 rows: `paid_seconds = 0` while `user_balance()` said 900 was available to spend. A
  future-dated grant counted in the panel and not in billing, the opposite disagreement.
  The screen also re-sliced its own lists (`entitlements[:10]`, `usage[:8]`), presenting a
  display window as the user's history. The split is now one SQL aggregate over exactly the
  predicate `_available_seconds_in_transaction()` spends from, the two lists are returned at
  their display sizes (`recent_rows` / `usage_rows`) beside real `entitlement_total` /
  `usage_total` counts, `_credit_history_heading` states «۱۰ مورد از N سابقه» whenever the
  window is smaller than the table, and `admin:credits:` joins the ack-first set because the
  screen now reads four indexed queries instead of two. Cost: 1.46 → 2.84 ms at 600
  entitlements and a 3 000-row ledger, every new read covering-indexed, no new index.
  `tests/test_admin_credit_overview.py` (7 tests) pins the balance agreement, the expiry
  and start-date predicates, the counted labels and the window parameters, and
  `test_admin_panel_presentation.py` pins the new acknowledgement order.
* **Three more digests sliced a list before describing it.** `show_provider_detail`
  printed ``quotas[:3]`` out of `ai_quota_latest()`, whose rows are unique per
  (credential, model, window) — so a provider reporting four quotas showed three and
  said nothing about the fourth; the health panel applied its ``results[:12]`` cap to
  the *mixed* AI+STT cache and filtered by service afterwards, so enough STT probes
  could empty the AI panel; `show_settings` sliced ``list(overrides.items())[:8]``
  before dropping providers without flags, so a ninth override vanished and the
  heading could print over an empty list. The provider panel now reads every quota
  through `ai_quota_latest_for_provider()` (`QUOTA_ROWS` display bound with a probe row
  and a stated cut, ordered by quota identity so the shown prefix is stable), the
  health panel filters before capping at `HEALTH_ROWS` and says how many results were
  left out, and admin overrides render one line per flagged provider with the heading
  suppressed when there are none.
* **The monitoring digests presented a bounded read as a complete view.** `show_usage`
  fetched the latest quota snapshot for up to twelve providers and printed
  ``quotas[:4]``; `show_benchmarks` listed ten scored models as a ranking without
  saying ten was a cut; `/users` printed fifty rows under a header that implied the
  whole user base. The quota digest is now bounded by `len(PROVIDER_REGISTRY)` — the
  query returns one row per provider, so every snapshot has a line and the header says
  how many providers it covers — the benchmark board fetches one probe row and appends
  a line naming the cut while it exists, and the `/users` copy is titled
  «۵۰ کاربر اخیر از ۶۰ کاربر» whenever the table is larger than the copy — the fifty comes
  from a new `USERS_EXPORT_ROWS` constant instead of a literal duplicated between the
  query and the sentence, and every count in these three labels is rendered with
  `to_persian_digits` so a Persian sentence does not switch numeral systems mid-line.
* **Three admin screens could become unsendable because a callback carried a
  name instead of an address.** Telegram caps ``callback_data`` at 64 bytes, and
  three tokens embedded strings whose length is not ours to bound:
  ``admin:ai:wiz:mdl:stt:<provider>:<model>``, which the *seeded* Gemini-STT
  catalog already overflows at 65 bytes; ``admin:stt:mdep:<provider>:<model>``,
  59 bytes for that same id and 90 for a synced 66-character one; and
  ``admin:stt:qdel:<provider>:<scope>:<quota_type>``, measured at 141 bytes with
  the 60-character-per-field input the budget prompt itself accepts.
  Each screen stopped being sendable for exactly the rows an admin needed to act
  on. They now address a rendered row by position (plus an 8-hex identity
  fingerprint for the quota row, so a keyboard that went stale under the admin
  refuses the write instead of deleting a neighbouring row), the legacy literal
  tokens keep working, and a position that no longer exists answers in Persian
  and redraws rather than touching another row. The wizard steps also state how
  many known models they left out instead of hiding them.
* **The finance screens reported a page as if it were the backlog.** The admin
  overview said «۵ پرداخت در انتظار بررسی» while 34 receipts waited, because it
  printed `len(list_pending_payments(limit=5))`; `/payments` and the payment
  panel did the same with 20. The pending queue and the special-user list now
  take one `COUNT` each, so the number an admin decides on is the real one.
* **Silent truncation removed from the panels.** The AI catalog showed 14
  models and hid the rest, the route panel offered four addable providers,
  STT listed every model of every provider in one message, the log panels
  stopped at 14/40 rows, both credential screens grew past the keyboard limit,
  the combined STT quota screen passed the 4 096-character message limit once a few
  budgets existed (providers are now paged three at a time, with the folded-away
  counts stated), and the user list stopped at 50 users.
* **Unknown billing state was presented as free.** The catalog now renders
  `unknown` as "نامشخص — رایگان تلقی نمی‌شود ⚠️", shows a promotional free tier
  with its end date, and keeps the deprecated / withdrawn /
  needs-billing markers on every page.
* **Panel redraws no longer answer the same callback twice** (the second answer
  always failed and produced a red toast), and the read-only admin panels that
  touch several tables are acknowledged *before* the database work so the button
  stops spinning while the panel reads.
* Deprecating a model from the catalog panel recorded no date (`None`), so the
  deprecation could not be aged; and the provider legend had a typo.

### Added

* `database.py`: `ai_usage_overview()` (with `ai_usage_metrics` /
  `ai_usage_latency_p95` as wrappers), `ai_models_page()`, `ai_event_count()`,
  `stt_events_count()`, `ai_routes_reorder()` (transactional, two-phase, under
  the `UNIQUE(service, task_type, route_index)` constraint), and `offset`
  support on `ai_events_list()`, `stt_events_list()` and `user_summaries()`.
* `provider_health.snapshot()` — a network-free read of the persisted health
  rows for panel redraws; `ProviderRouter.default_route_order()` so the route
  panel asks the router what a fresh install looks like instead of keeping a
  second copy of the list; `StudyBot._arm_answer_once()` for once-only
  acknowledgements; the "🧭 بازنشانی ترتیب پیش‌فرض" route action.
* Tests: `test_route_seed_upgrade.py` (seeding, startup alignment, admin-route
  preservation, idempotence, history preservation, env/legacy modes),
  `test_route_failover_eligibility.py` (failover budget, disabled rows,
  Retry-After, credential pool, leg budget, attribution, slot release) and
  `test_admin_panel_presentation.py` (pagination and labels for AI/STT catalogs,
  logs, users; callback-size limit; malformed and stale callbacks;
  acknowledgement order; skip-reason coverage).

### Performance

Measured with the committed harness, which seeds synthetic data and times both
the pre-change and post-change query shape in the same process, so no
cross-session comparison is involved:

```bash
python -m scripts.benchmark_admin_panels --rows 20000 --events 2000 \
       --models 600 --users 800 --repeat 20
```

20 000 usage rows, 2 000 events, 600 catalog models, 800 users, SQLite in a
temporary directory, no network, mean of 20 timed calls after a warm-up
(sandbox: 4 cores, Python 3.11). Before-numbers are the old code path measured
in the same run, or the pre-change baseline session for the panel renders.

| Path | Before | After |
| --- | --- | --- |
| Overview usage reads (metrics + p95) | 40.5 ms | 22.4 ms (`ai_usage_overview`, one window) |
| AI catalog read for one provider | 5.9 ms (whole table) | 1.0 ms first page, 1.8 ms last page |
| AI catalog panel redraw (600 models) | 13.8 ms | 4.5 ms (15.5 ms on a page that offers replacements) |
| Health panel open | live probes on a cold cache | 2.2 ms, zero requests |
| Event-log page (2 000 rows) | flat 14 rows, no paging | 6.7 ms first page with an authoritative total; 4.1 ms for a stale token, which now clamps to the last real page |
| Pending-receipt page (20 rows) | 21 queries — one `payment_detail` per row | 1 query; `payment_detail` called zero times (pinned by a test) |
| STT catalog / log page | whole registry in one message | 3.5 ms / 4.0 ms |
| User page (800 users) | 1.2 ms for the 50-row query, then a long-message send | 3.7 ms per page of 25 — complete and inline |
| `router.plan()` per pipeline stage | 41 ms | unchanged (full eligibility read) |
| `ensure_seeded()` at every startup | — | 4.5 ms, and it writes nothing once aligned |

A later re-run of the same command (same arguments, after the bounded-read and
credit-screen rounds) reproduced the shape: overview 41.3 → 20.7 ms, catalog page
5.4 → 0.7 ms, user page 0.80 → 1.07 ms, health panel 2.2 ms from cache, `ensure_seeded()`
4.4 ms. The two slowest admin screens remain 📈 نمای مسیر و بودجه (52 ms) and 🧭 مسیرها
(46 ms): each pays one authoritative `router.plan()` read instead of a cached summary,
by design.

No new index was added. `EXPLAIN QUERY PLAN` still shows a `USE TEMP B-TREE`
for the bounded p95 sample, for the catalog page's `ORDER BY deprecated, id` and
for the user page's `ORDER BY first_seen`; at these sizes each of those sorts
costs well under a millisecond per page (or, for the p95 sample, is bounded by
the 5 000-row sample cap), and `ai_events` is ring-pruned to 2 000 rows, so no
index benefit has been demonstrated — an index that is not needed is write cost
and migration surface for nothing.

### Notes

* Pricing, model identifiers and free-quota limits are provider-side facts that
  change without notice; the numbers quoted in `docs/AI_PROVIDERS.md` are dated
  evidence, not guarantees, and the router treats an unverifiable billing state
  as not-free rather than as free.


## Unreleased — audit, reliability and performance pass

### Fixed

* **Word output is now explicitly right-aligned, not justified.** Direction
  (`w:bidi`) and alignment (`w:jc`) are different properties: a paragraph
  with `w:bidi` and no `w:jc` aligns to its logical *start* edge, which a
  reader resolves through its own defaults, and the body text was justified
  (`both`) rather than right-aligned. Right alignment is now declared at all
  three layers Word reads -- `w:docDefaults`' `w:pPrDefault`, every
  paragraph style (`Normal`, the headings, `Title`/`Subtitle`, the box
  styles, `Quote`, `TOC Heading`, `TOC 1..3`) and every paragraph the build
  writes -- while the centred blocks keep their explicit centring and a
  Latin-only formula stays LTR *and* right-aligned.
* **A missing LibreOffice no longer loses a user's document.** The static table
  of contents used to make the whole delivery fail when no renderer was
  present, discarding a perfectly good transcript and note set. The new
  `DOCX_TOC_PAGE_NUMBERS` policy (`auto` | `required` | `off`, default `auto`)
  delivers a link-only topic list instead, still never printing an unmeasured
  page number.
* **An empty static table of contents no longer discards the booklet.**
  `build_plain_docx` decided whether a document earned a topic list by counting
  *every* Markdown heading, but the list itself only collects headings at or
  above `DOCX_TOC_LEVELS` — so raw material whose headings were all `###` or
  deeper requested a TOC page that was then filtered down to nothing. The empty
  list was a hard `DocxPaginationError`, which the delivery path re-raised: the
  user lost the Word booklet *and* the transcript. The decision now counts only
  the headings the list will actually contain, an empty list drops the topic
  page instead of failing, and a heading with no usable bookmark is skipped
  rather than aborting the build. Documents that already built successfully are
  unchanged byte-for-byte.
* **The raw transcript is now always delivered.** `_deliver_result_documents`
  documented that the `.txt` file "is always sent", but a `DocxPaginationError`
  propagated before it was written, so a pagination failure cost the user the
  lecture as well as the booklet. The transcript is sent first and the failure
  is still raised afterwards, so the job is recorded as failed with its
  tracking reference and no content is lost.
* **MIME-less or generic-MIME `.webm` screen recordings now use video extraction.**
  `.webm` is listed as both an audio and video extension, and the old audio-first
  fallback sent an ambiguous upload to STT with its video stream still inside.
  The extension fallback now prefers video, which extracts only the audio track
  before transcription. A real VP9/Opus WebM is covered through the extraction
  and transcription-delivery path.
* **Bookmark identifiers leaked a document's position in the job sequence.**
  They came from a process-global counter that never reset, so the first heading
  of one user's booklet was named after how many documents the server had
  produced before it. Numbering is now per document, which also makes DOCX
  output byte-reproducible.
* **Two duplicate stop-word entries** (`each` in `qa.py`, `را` in `units.py`)
  removed.
* Documentation links pointing at reports that no longer exist.
* **systemd docs disagreed with the shipped unit.** `OPERATIONS.md` advised
  `Restart=on-failure` while `deploy/gamas-bot.service` sets `Restart=always`,
  and the README/OPERATIONS pointers to "the full unit" were circular. Both
  pages now describe the unit that actually ships.
* **`cryptography` was capped below its security fixes.** The declared range
  `<47.0` made every fixed release uninstallable while `pip-audit` reported 7
  advisories in the resolvable version (PYSEC-2026-3552/3553/3554 and
  GHSA-537c-gmf6-5ccf, fixed in 48.0.1/49.0.0/50.0.0). The range is now
  `>=50.0.0,<51.0` (resolves to 50.0.2, `pip-audit` reports no known
  vulnerabilities); the Fernet credential path is covered by
  `tests/test_provider_credentials.py`.
* **`python -m gamas_bot --check` existed only in the documentation.** The
  README, `OPERATIONS.md` and `TROUBLESHOOTING.md` told operators to run it as
  a configuration/dependency self-check, but the entry point had no argument
  parsing, so the flag was ignored and the bot *started* instead — the exact
  opposite of a diagnostic. It now runs an offline PASS/FAIL check
  (configuration, runtime-importable dependencies, media worker) with exit
  codes 0/2 and never touches Telegram, the database or the instance lock;
  `tests/test_cpanel_deploy.py` pins all of that, and
  `tests/test_dependency_consistency.py` fails when a new runtime dependency
  is neither checked nor explicitly excluded.

### Changed

* `bot.py` no longer owns Telegram message rendering: the Markdown → HTML
  converter and the UTF-16 pagination logic moved to `gamas_bot/telegram_text.py`
  (re-exported from `bot.py`, so existing imports and patch targets are
  unchanged).
* Dependencies have one canonical definition (`pyproject.toml`);
  `requirements.txt` is generated by `python -m scripts.sync_requirements` and a
  test fails on drift.
* Documentation consolidated: the four overlapping historical reports
  (`AUDIT.md`, `QUALITY_REPORT.md`, `PRODUCTION_REPORT.md`, `FINAL_REPORT.md`)
  became `docs/AUDIT_HISTORY.md`, which is explicitly marked superseded.
* `scripts/cpanel_preflight.py` reports a missing DOCX renderer as a warning
  under `auto` and a failure under `required`.
* Lint configuration (`ruff`) and pytest configuration moved into
  `pyproject.toml`; the selected rule set is `E4,E7,E9,F,W,B,C4` and it is clean.
* Presentation clips are probed through `media.probe_media_many` instead of one
  `probe_media` call per clip. The two `PresentationDurationTests` in
  `tests/test_audit.py` now patch that seam with the same per-clip reports;
  their assertions are unchanged.

### Performance

Measured on a 12-section document with the page-map renderer stubbed (see
`docs/TESTING.md` for how to reproduce):

Measured on a 30-section document (median of 7 runs), with the page-map
renderer stubbed so the numbers are CPU time rather than LibreOffice time:

| Metric | Before | After | Change |
| --- | --- | --- | --- |
| DOCX build, static TOC disabled | 843.9 ms | 615.8 ms | −27% |
| DOCX build, static TOC enabled | 1140.1 ms | 825.8 ms | −28% |
| Page-map renders per document | 3 | 2 | −33% |
| Full package rewrites per document | 3 | 1 | −67% |
| Worst-case renders before failure | 6 | 3 | −50% |

The render count is the one that matters most in production: each render is a
headless LibreOffice conversion of several seconds, so the document path is
shorter by roughly one conversion. The style-lookup memoization accounts for
most of the remaining saving and produces byte-identical output (verified by
hashing `word/document.xml`, `word/styles.xml` and `word/fontTable.xml` against
the previous implementation).

A second performance pass used the same methodology (median of 5 runs, same
30-section document with tables/callouts/glossary, static TOC enabled, output
compared member-by-member by SHA-256 before and after):

| Metric | Before | After | Change |
| --- | --- | --- | --- |
| DOCX build | 2727 ms | 1406 ms | −48% |
| Clip probing, 40-clip deck (`prepare_audio`, incl. merge) | 4344 ms | 390 ms | −91% |
| Worker processes per 40-clip deck probe | 40 | 1 | −97% |

* The DOCX win writes each fresh run's `w:rPr` directly instead of through six
  python-docx property setters (each performs schema-order tree scans), and
  resolves the static TOC's heading styles from the raw `w:pStyle` id instead
  of `paragraph.style` per paragraph. `tests/test_docx_run_styling.py` proves
  both fast paths emit exactly the XML the reference paths emit for every
  combination of direction, weight, style, colour, size and font role, and the
  whole golden corpus (the six note fixtures, a synthetic 30-section booklet,
  a plain document and the raw-text deliverable) hashes identically.
* The probing win is `probe_media_many`: one worker process per deck instead
  of one per clip — about 90% of a single probe's wall time was interpreter +
  PyAV import. A clip that cannot be probed becomes a per-file error entry
  (same skip reason, same order as before), and a failed batch falls back to
  isolated per-file probes so a native crash on one clip cannot lose the rest.

### Added

* **Fabrication (precision) QA and a corrective pass for invented values.**
  The deterministic QA layer only measured *loss*; a hallucinated dosage,
  lab value or statistic is the opposite failure and was invisible to it.
  `run_note_qa` now also reports values and strong terms the notes state
  that no source chunk contains, computed conservatively (as
  `NoteQAReport.unsupported_numbers` / `unsupported_terms`, with
  `has_unsupported_facts`), so a formatting difference such as `۱۲۰/۸۰` vs
  `120/80` is never mistaken for an invention. An invented value now
  triggers the existing, bounded repair pass (`NOTE_REPAIR_ENABLED`), whose
  prompt names the unsupported values to remove and whose reminder forbids
  keeping them. Acceptance is guarded both ways: a pass that *introduces*
  an invented value is rejected outright, and a pass that *removes* one is
  accepted even when recall is unchanged — per part and per document — so a
  fabricated number can never be traded for restored prose.
* **Account-entitlement attestation.** Providers that publish rate limits but no
  documented no-charge entitlement (Groq) are now explicit, opt-in states
  instead of being silently treated as free: they stay out of `AI_FREE_ONLY`
  routes until an administrator attests the deployment's account under
  **AI → provider → 🧾 تأیید استحقاق حساب**. The attestation is audited,
  revocable, and never relaxes the per-key billing attestation, terms,
  deprecation or quota gates.
* **Per-provider extra-pass overrides.** The outline, QA-repair and
  final-compilation calls each spend free-tier quota; they can now be turned on
  or off per provider from the admin panel, with the origin (profile vs admin)
  shown and reported through `PlannedRoute.pass_state()`.
* **Non-token metering for Cloudflare Workers AI.** Neurons are estimated
  locally (pessimistically, never under-counted) and guarded against the
  documented daily inclusion; the running estimate is visible in the admin
  panel, and models that cannot run on the free allocation can be flagged
  `requires_paid_billing` so `FREE_ONLY` rejects them up front.
* **NVIDIA endpoint lifecycle.** Discovery records `free_endpoint` per model,
  withdrawn models are marked unavailable rather than deleted, and the Models
  panel suggests a live replacement for any deprecated or withdrawn model.
* **🩺 سلامت and ⚙️ تنظیمات AI panels**, plus status-class, error-class, job-id
  and date filters on the logs panel. The health panel is read-only by
  construction; only the explicit test action sends a billable request.
* `scripts/validate_provider_platform.py` — offline end-to-end validation of the
  platform (legacy NaraRouter compatibility, provider-aware chunk budgets,
  provider-level failover on 429, FREE_ONLY and paid-fallback gates,
  backend-metadata scrubbing, secret hygiene). No API key required.
* `tests/test_provider_platform_gaps.py` — coverage for account entitlement,
  extra-pass overrides, Neuron metering, NVIDIA endpoint lifecycle, log filters
  and the new admin panels.
* `migrations/008_provider_entitlement_and_pass_policy.sql` — forward-only
  schema additions for all of the above.

### Fixed

* **`NoteJobSession` now exposes the extra-pass policy.** `structuring._pass_allowed`
  reads `job.outline_enabled` / `repair_enabled` / `compile_enabled`, but the
  session never defined them, so the repair and final-compilation gates raised
  `AttributeError` on any routed run that reached them. The session now proxies
  the planned route, so an administrator override reaches every call site.
* **Provider settings columns added by migration 008 were silently dropped.**
  `ai_provider_settings_upsert` filtered against a fixed allowlist, so the new
  entitlement, extra-pass, Neuron-budget and region fields could not be stored.
  The allowlist now covers them.

* `tests/test_config_consistency.py` — configuration/documentation/implementation
  drift detection.
* `tests/test_dependency_consistency.py` — `pyproject.toml` ↔ `requirements.txt`
  drift detection.
* Regression tests for the TOC page-number policy, the bounded render loop and
  byte-reproducible document generation.
* `scripts/benchmark_queue.py` — offline load benchmark for the bounded job
  queue (1/3/5/10/25/50 simultaneous uploads: acceptance vs back-pressure,
  queue-wait and latency percentiles, throughput, RSS); wired into CI as a
  smoke test.
* `tests/test_docx_run_styling.py` — byte-level equivalence of the optimized
  DOCX hot paths with the python-docx setter reference, plus the heading-level
  resolver against the public `paragraph.style.name` scan.
* Batched-probe coverage in `tests/test_media_runtime.py`: one worker process
  for a whole deck, per-file failure isolation, and both fallback paths.
