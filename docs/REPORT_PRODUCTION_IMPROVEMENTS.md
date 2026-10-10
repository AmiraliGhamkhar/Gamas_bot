# گزارش کار / Work report — production improvements (routing, panels, performance)

Branch `arena/e4af84af-gamas-bot`, on top of `e64ca86`. The work was built up in
twelve per-round commits; the Arena sandbox was re-cloned from the remote partway
through, which discarded the unpushed commit objects while keeping the file
snapshot, so the history is now `376a49e` (everything up to the credit-screen fix)
plus the commits after it. No code was lost and no history was rewritten — the
squash is an environment accident, recorded here rather than hidden.
Everything below is what was actually changed, run and measured in this workspace;
nothing here is a projection. Defects 1-24 were merged into `main` through PR #42
(merge commit `ffff87d`, whose head was `bbb7b49`); defect 25 arrived after that
merge and travels in the follow-up PR #43 as `09d5193`, so the section counts below
match the branch rather than the merged head.

---

## 1. Defects found in the original code

| # | Defect (verified in source before the change) | Consequence |
|---|---|---|
| 1 | `DEFAULT_NOTE_ORDER` started with `gemini`, and any `NOTE_API_PROVIDER` env value was hoisted to the front unconditionally | The deployment's "free-first" intent was not the actual default order |
| 2 | `ensure_seeded()` returned early as soon as a task had *any* stored rows | Existing installs were never upgraded to a corrected default order |
| 3 | `route()` folded the `enabled` column into `free_only` | A route row the admin disabled silently became a **paid** leg |
| 4 | `generate()` truncated `plan.legs[:max_failovers+1]` *before* credential eligibility was checked | A configured tail-only deployment failed with "no credentials" without ever trying the usable provider |
| 5 | No per-leg wall-clock budget; provider-wide 5xx/transport faults rotated through every key | One slow or dead provider consumed the whole job timeout and burned the key pool |
| 6 | A *failed* route read was treated as "nothing seeded" → re-inserted defaults | An administrator's route configuration could be deleted by a transient DB error |
| 7 | The AI overview ran two queries over **different** windows and sorted all latencies in Python | Numbers were not comparable and the query cost grew with history |
| 8 | `_show_provider_health(force=False)` probed providers when the cache was cold | Opening an admin panel made live provider requests |
| 9 | Silent caps: catalog `[:14]`, route-add `[:4]`, STT models per message, logs `[:40]`, users at 50, credential keyboards unbounded, STT quota screen unbounded | Panels hid rows, and two admin screens became **unsendable** past Telegram's 100-button / 4096-character limits |
| 10 | The admin root menu duplicated the route-management destination | Two entries for one authoritative screen |
| 11 | Callback answers could be sent twice / after the 5 s window; long reads before the ack | "Button not responding" and duplicate taps |
| 12 | A route row's status glyph came from the provider's billing class only | A saved row blocked by a gate, or with no ready credential, read as healthy (the legend half of defect 11) |
| 13 | The combined STT quota screen rendered every budget and every snapshot of all 13 providers in one message | Past 4 096 characters (measured 5 677 in a fixture) the panel was unsendable |
| 14 | Three callbacks embedded a *name* in `callback_data`: `admin:ai:wiz:mdl:stt:<provider>:<model>`, `admin:stt:mdep:<provider>:<model>` and `admin:stt:qdel:<provider>:<scope>:<quota_type>` | Model ids arrive from provider sync and quota fields are admin-typed free text (60 characters each), so the tokens measured 65 / 90 / 141 bytes against Telegram's 64-byte cap — and an over-long payload stops the **whole screen** from being sent, not just one button |
| 15 | The finance screens printed the length of a capped list as the backlog (`len(list_pending_payments(limit=5))` in the overview, `limit=20` in the panel and `/payments`); the special-user list showed 30 rows but 12 remove buttons; the plan list rendered every plan into one message and one keyboard | An admin saw «۵ پرداخت در انتظار بررسی» for a queue of 34, could not act on special users 13-30 from the panel, and past ~34 plans the plan screen passed both Telegram caps and stopped opening |
| 16 | The monitoring digests had the same bounded-read-as-total problem: `show_usage` fetched `ai_quota_latest_all(limit=12)` and rendered `quotas[:4]`; `show_benchmarks` printed `ai_model_benchmark_scores(limit=10)` as a ranking without saying it was a cut; `/users` printed `user_summaries(limit=50)` under a header that implied the whole user base | An admin concluded that four providers had quota data when eight did, read a top-ten board as the full ranking, and read 50 as the user count. The quota bound is now the provider registry (one line per provider, nothing dropped), the benchmark board labels its cut, and `/users` states «۵۰ کاربر اخیر از ۶۰ کاربر» from `stats()["users"]`; all three labels use `to_persian_digits` and `USERS_EXPORT_ROWS` replaces the duplicated literal |
| 17 | Three read-only digests sliced a list that was not yet the thing they described: `show_provider_detail` printed `quotas[:3]` of a 20-row read of `ai_quota_latest()` (one row per key/model/window, so it dropped quotas entirely); the health panel took `results[:12]` from the **mixed AI+STT** cache and filtered by service *afterwards*, so STT probes could evict every AI row from the AI panel; `show_settings` sliced `list(overrides.items())[:8]` before the flag filter, so a ninth provider disappeared and a heading could print over an empty list | An admin read three quotas as the provider's whole quota picture, saw a nearly empty health panel that actually had data, and missed an admin-imposed block in the settings digest — all silent, none of them labelled |
| 18 | `admin_user_credit_overview()` computed the admin's view of a user's balance as a **Python sum over the newest 50 entitlement rows**, and the screen then sliced that list again (`entitlements[:10]`, `usage[:8]`) under headings that promised the history. The docstring claimed the split "comes from the same entitlement rows the reservation engine consumes, so the screen can never disagree with billing" — it could: `user_balance()` aggregates every spendable row with no window | On a user with 57 entitlement rows and one old 900-second grant still spendable, the panel showed **0** paid seconds while the reservation engine would happily spend 900: the admin judging a refund saw a different number than the one billing used. Not-yet-started grants were counted as spendable, the opposite disagreement | **Fixed** |
| 19 | The latency percentile is deliberately sampled — `ai_usage_overview()` reads at most `LATENCY_PERCENTILE_SAMPLE` (5 000) newest rows instead of materialising every latency — and the database exposed `latency_samples`, but **no panel consumed it**: three screens printed «تأخیر p95: X ms» next to «درخواست‌های امروز: N», so on a day with more than 5 000 requests an admin compared a *sample* percentile against a full-window counter and nothing marked the difference | The p95 an operator uses to judge a provider's tail latency was silently the p95 of whatever 5 000 requests happened to be newest — the same "bounded read presented as the whole" class as defects 9, 15, 16 and 17, on a number people make routing decisions with. Note the subtlety: `latency_samples < requests` is **not** the signal (rows can legitimately have no latency recorded); the honest signal is that the read saturated |
| 20 | The STT quota screen stated a total it had invented: `stt_quota_latest()` ended in a hard `LIMIT 20`, the overview sliced that to `[:3]`, and the label printed `len(snapshots)` — so a provider with more than 20 quota rows read «آخرین 3 از 20» — while the advice in the same line, «برای دیدن همه، صفحهٔ همین ارائه‌دهنده را باز کنید», pointed at a screen that applied the very same 20-row read and so could not show them either | An admin debugging a quota problem was told a number that was the size of the read, and was sent to a screen that would silently repeat the cut; on a provider with per-model quotas (the table is unique per account_scope/model/quota_type, so 25 rows is ordinary) the history looked finite and reachable when it was neither |
| 21 | The two **user** screens wrote a whole table into one message: `_billing_plans()` emitted one text line *and* one inline button per enabled paid plan, and `_billing_balance_text()` one line per active entitlement row — neither read had a `LIMIT` | Both grow without bound (a fresh database already ships 6 paid plans, and every approved payment adds an entitlement row). Past roughly 24 plans the message passes Telegram's 4 096-character and 100-button caps and the purchase screen stops sending at all; a long-lived account gets an unscrollable wall. The admin equivalents were bounded rounds ago — this was the same class on the side nobody had audited |
| 22 | `/history` printed `payment_history()`'s ten newest rows under a bare «سوابق پرداخت:» heading with no total | The read is capped at ten by design (and clamped to 50 in SQL), so this is defect 9's family on the user's side rather than defect 21's: after an eleventh payment the list reads as the complete record of what you ever bought, with nothing marking it as a window. The heading now says «۱۰ پرداخت اخیر از N پرداخت» from a new `payment_count()` |
| 23 | Two panels built a **button label** out of stored free text with no length promise: the purchase button interpolated `plan['name']` (the schema keeps 60 characters) next to the price, and the provider-health button interpolated a credential `label` (whitespace-normalised, never length-checked). Telegram caps a button's text at 64 UTF-16 code units and rejects the *whole* message when one button is over | One long enough plan name or key label made the purchase screen and the health panel undeliverable for every user, not just ugly — the per-button twin of defect 21, invisible with the 35–37-unit labels the seeded catalog ships |
| 24 | The STT route-step model picker still put the provider's model id *in the callback*: `admin:stt:rset:{provider}:{model}` — after the catalog rows and the budget rows had already been converted to address-by-position, and with `.encode("ascii")` on top | Telegram's `callback_data` budget is 64 bytes for the whole token and the longest shipped STT id needed 59, so one longer id or provider slug made the picker message undeliverable (and a non-ASCII id from a sync raised instead of degrading). The admin could not set a route model at all — the failure mode of defect 9's callback-sizing family, in the one panel that had been missed |
| 25 | `_deliver_result_documents` carries a comment promising that the raw transcript "is the one deliverable that never depends on the Word document, so it is always sent" — but only the document *build* was guarded; the document *upload* was not, and the fallback text send had no guard at all | A Telegram refusal or a dropped connection on a finished `.docx` unwound the rest of the delivery: the user received neither file nor text, the job was recorded failed and its usage reservation was released — after the model work had already been spent and the note had already been written. A build failure degraded gracefully while a delivery failure lost everything, which is backwards |

Every item above is fixed. Defects 12-21 were found after the first pass and
closed round by round: the quota screen was bounded, the route annotation added,
the log page given its total, the three name-carrying tokens replaced by positions
plus a fingerprint, the billing screens counted and paged, the AI monitoring
digests labelled, the digest filters reordered before their caps, the credit
screen's balance made to agree with billing, the sampled percentile labelled, and
the STT quota totals re-sourced from the table, and the user's own purchase and balance screens
bounded by the sendability pass (§6 records what defect 12's fix
costs). The remedy for 12 is an explicit annotation next to the row rather than a
new colour glyph — the honest caveat is in §8.

**Sweep closure.** The bounded-read family was swept three ways and each pass is
recorded: `limit=` arguments and hard `LIMIT`s in the read itself (defects 9, 13, 15,
18, 20), Python slices taken from an already-bounded read (9, 16, 17, 18, 20), and
filters applied *after* a cap
or aggregates computed *over* a windowed read (17, 18, 19). The remaining bounded
displays were read and are labelled by design, not overlooked:
`QUOTA_ROWS` (probe row + «فقط ۱۲ تصویر سهمیه…» next to it), `BENCHMARK_ROWS`
(same shape), `HEALTH_ROWS` (states how many results the cache holds),
`WIZARD_MODEL_CHOICES` on both wizard branches («N مدل شناخته‌شدهٔ دیگر در این فهرست
نیامده» plus the custom-model escape hatch), `QUOTA_OVERVIEW_BUDGETS` /
`QUOTA_PROVIDERS_PAGE_SIZE` (folded counts stated, drill-in button offered),
`USERS_EXPORT_ROWS` («۵۰ کاربر اخیر از N کاربر»), the finance pages (real `COUNT`s),
`payment_history`'s ten-row window (now «۱۰ پرداخت اخیر از N پرداخت»),
`LATENCY_PERCENTILE_SAMPLE` (sampled note), `admin_audit`/`admin_usage_ledger`
(labelled «آخرین» digests, DB-capped at 200 and sent through the long-message pager),
and the credential lists whose "N از M" totals come from uncapped
`list_summaries()` rather than from a page.

A **fourth pass** was needed after that closure was written, because the three sweeps above all
asked "was a bounded read presented as the whole?" and never asked "is this message *sendable*?".
A list with no read limit at all is not merely slow — it fails to deliver. So every user-facing
screen was re-read for unbounded renders: `_billing_plans()` and `_billing_balance_text()` had
none (defect 21) and are bounded now. The rest were checked and are bounded for a stated reason:
the help, privacy and receipt texts are constant-size, and long documents already go through
`_send_long_message`'s pager.

## 2. Files changed and why

| File | Why |
|---|---|
| `gamas_bot/ai/routing.py` | Authoritative router. New `DEFAULT_NOTE_ORDER`; `ROUTE_SEED_REVISION`; `ensure_seeded()` → `_seed_order` / `_upgrade_seeded_order`; `plan()` gate order with `skipped: list[tuple[slug, code]]`; `RouteLeg.enabled`; `NoteJobSession._runnable_legs` / `_attempted_legs` / `_leg_budget_seconds()`; Retry-After clamp; provider-wide-fault short circuit; `default_route_order()` for the panel |
| `gamas_bot/database.py` | `pending_payment_count()` and `unlimited_user_count()` (a real total for panels that used `len()` of a capped list); `list_pending_payments` carries `receipt_path`, which is what removed the per-row `payment_detail` read; `ai_routes_reorder()` (route_index-only, transactional, under the UNIQUE constraint); `ai_usage_overview()` (one window for counters + bounded p95 sample); `ai_models_page(offset,limit)`; `ai_event_count()` extended to mirror every page filter (error_class, job_id, http_status class) so the log panel's total and its page cannot disagree; `stt_events_count`; offsets for event-log and user queries; `admin_user_credit_overview()` now takes its balance from one SQL aggregate over the reservation engine's own predicate (never a sum over the display window) and returns `entitlement_total` / `usage_total` beside the two display lists, whose sizes became parameters (`recent_rows=10`, `usage_rows=8`); `ai_quota_latest_for_provider()` — every quota snapshot a provider holds, ordered by quota identity instead of observation time so a stated cut is a stable prefix (`EXPLAIN QUERY PLAN`: `SEARCH ai_quota_snapshots USING INDEX sqlite_autoindex_ai_quota_snapshots_1 (provider=?)` + a temp B-tree over one provider's rows, the same access path the previous read used, so no new index); `ai_usage_overview()` now returns `latency_sample_capped` next to `latency_samples`, because a short sample is not proof of a cap (rows may have no latency recorded) while a saturated read is ; `payment_count(user_id)` was added so `/history` can state its window's total — a `COUNT(*)` on `user_id`, which the existing `idx_payment_requests_user_created` already covers (no new index) |
| `gamas_bot/telegram_text.py` | `BUTTON_LABEL_LIMIT = 64` and `button_label()`: measures a button label in the same UTF-16 units the message pager already uses (so a label of emoji is shortened too) and marks the cut with `…`; the walk is backwards from the budget rather than a fixed character slice | | `gamas_bot/provider_health.py` | `snapshot()` / `cached()` / `invalidate()` so redraws need no network |
| `gamas_bot/bot.py` | Ack-before-read, `_arm_answer_once` + idempotent `_edit_callback`; user menu (ساخت جزوه primary, merged help/formats, merged balance/subscription, privacy kept); five admin sections; users, credential, payment, plan and special-user screens paged with counted totals (one `_admin_page` parser, `_page_arg` accepts digit-only page tokens so `admin:special:add` still routes to its own branch); network-free health panel; a page of receipts no longer re-reads each row through `payment_detail`; the `/users` export titled against `stats()["users"]` when it is only a prefix, with `USERS_EXPORT_ROWS` as the single source for the row count; the credit screen's two history headings now state «۱۰ مورد از N سابقه» via `_credit_history_heading`, the redundant `[:10]`/`[:8]` slices are gone, and `admin:credits:` joins the ack-first set; the overview AI line appends `latency_sample_note()` so a sampled p95 is labelled; the purchase screen is paged (`USER_PLAN_PAGE = 8`, `billing:plans:{page}`, the bare legacy token still page 0, «صفحهٔ X از Y» only when a second page exists) and the balance screen prints at most `ACCOUNT_ENTITLEMENT_ROWS = 8` rows under its all-rows totals with a counted remainder; `_page_arg(data, prefix)` now takes the whole token prefix so every paged screen shares one parser; `/history` labels its ten-row window against `payment_count()`; the Word upload and the text fallback are each best-effort now, so one Telegram failure can no longer swallow the other (defect 25); both data-driven button labels (`خرید …` on the purchase screen, `🧪 تست …` on the health panel) now go through `button_label()`, with the price reserved outside the clamp so shortening a name never hides what a plan costs |
| `gamas_bot/admin_ai.py` | `MODELS_PAGE_SIZE=10`, `LOGS_PAGE_SIZE=14`, `KEYS_PAGE_SIZE=12`; merged overview metrics; `show_routes(task_type)` via `_route_callback`; uncut addable list; `stale_rows` gate for the live catalog read; `_SKIP_REASON_FA`; log pages with an authoritative total and a clamped stale page; paged key screens with service filter preserved; unusable route rows annotated from `plan()` / `credentials_for()`; quota digest bounded by `len(PROVIDER_REGISTRY)` instead of a silent `[:4]`; `BENCHMARK_ROWS` plus a probe row that labels the cut; counts rendered with `to_persian_digits`; `QUOTA_ROWS` for the provider-detail quota block (the OpenRouter local ledger moved out of the counted group so the statements about it stay true); `HEALTH_ROWS` with the service filter applied *before* the cap; admin overrides built line-by-line and the heading suppressed when no provider carries a flag; both p95 lines carry `latency_sample_note()` — «(نمونه: 5/000 درخواست اخیر)» whenever the sample was capped |
| `gamas_bot/admin_stt.py` | Paged model catalog and log list (`_page_token`/`_split_page`/`_logs_button`), `mark_model` phantom-row guard, paged quota overview (`admin:stt:qtp:{page}`, 3 providers × 4 budgets) with the folded counts stated in the text; the snapshot note now counts the table (`stt_quota_count`) instead of the page, `QUOTA_SNAPSHOT_ROWS` bounds the provider's own screen and says so when it cuts; `show_route_models` and `route_set_model` now share one `_route_model_choices()` list and address a choice by `index` + an 8-hex `_model_fingerprint()`, so every picker payload fits by shape (`_route_model_token`); a stale or out-of-range position is refused with an alert and the panel re-rendered, a malformed one is refused without writing, and the name-carrying token is still accepted for buttons on older messages |
| `scripts/benchmark_admin_panels.py` | New, committed, offline benchmark that measures BEFORE and AFTER query shapes, every panel render and `router.plan()` in one process |
| `tests/test_route_seed_upgrade.py` (new, 20 tests + 19 subtests) | Seeding/migration contract |
| `tests/test_button_labels.py` (new, 9 tests + 5 subtests) | The label helper (exact budget, astral-plane counting, whitespace at the cut, empty-label floor, impossible budget) and the two screens that use it: a 58-character plan name stays sendable and keeps the price while the full name stays in the message body, the seeded catalog needs no clamp at all, and an 87-unit credential label is clamped on the button while the panel text keeps the whole story |
| `tests/test_user_account_screen.py` (new, 12 tests + 3 subtests) | The purchase screen's per-page cap, every plan reachable across pages, a stale or malformed page token falling back to the first page, buy payloads unchanged by paging, the balance screen's counted remainder — and the same screens through `_handle_callback`, so `billing:plans:1` is proved to reach page 2 and not to be read as a plan code, and `/history`'s heading through `_handle_message` at 12, 3 and 0 payments |
| `tests/test_route_failover_eligibility.py` (new, 11 tests) | Eligibility, 429/5xx, cooldown, invalid-credential rotation, per-key quota rotation, attribution, reservation release, leg deadline, FREE_ONLY / unknown-billing blocking |
| `tests/test_admin_panel_presentation.py` (new, 81 tests + 22 subtests) | Paging, ack order, stale/malformed callbacks, callback-size and keyboard limits, label and masking contracts for both platforms, both credential screens, the quota overview, the route-availability annotation, the log-total/page-query agreement and the "callback carries an address" rule for the STT catalog, the quota rows, the key wizard and the route-step model picker (payload fits with a pathological id, position round-trips, auto slot clears, stale/malformed position refuses without writing, legacy token still honoured) |
| `tests/test_media_intake.py` (2 more tests, 31 total) | Both delivery failures of defect 25, driven through the real `_process_submission` with the real Word builder and the real caption text: a refused `.docx` upload still sends the note as a chat message plus the raw transcript and leaves the usage reservation consumed rather than released, and a failing text send still lets the transcript go out |
| `.env.example`, `README.md`, `docs/{AI_PROVIDERS,AI_PLATFORM_AUDIT,ARCHITECTURE,CONFIGURATION,TESTING,CHANGELOG}.md` | Contracts an operator needs: default order, gate semantics, upgrade + rollback, panel structure, measured performance, test counts |

No dependency was added (`pip check` clean, `sync_requirements --check` up to
date); no migration file was added; no new cache, background task or index.

## 3. Exact default route order

```
nara → openrouter → gemini → groq → mistral → sambanova → zai → nvidia → cloudflare
```

* `AI_ROUTING_ENABLED=false` restores the legacy single-provider behaviour
  (`LEGACY_NOTE_ORDER` + `NOTE_API_PROVIDER` hoisting).
* `AI_DEFAULT_NOTE_ROUTE` overrides the whole order for a task.
* An explicit admin route for a task wins over all of the above and is never
  rewritten.
* Seeded rows are ordered; **eligibility gates do not reorder them** — they remove
  legs from a *plan* at request time. `plan()` skips, in order:
  `route_disabled` → `trial_only_blocked` → `terms_of_use_blocked` →
  `experimental_locked` → `region_restricted_locked` (+ region codes) →
  `disabled` / `unavailable` → free-only block (`billing_blocked`,
  `account_entitlement_unverified`, `not_free_eligible`, `quota_exhausted`) →
  `paid_fallback_disabled`. Each skip is `(slug, code)` and the panel renders
  `_SKIP_REASON_FA` beside it. "Preferred but unusable" is therefore visible and
  auto-skipped, never silently paid for.
* A model list GET is not treated as proof of entitlement: without a billing
  attestation an unknown-billing provider stays blocked (`AI_FREE_ONLY=true` /
  `AI_ALLOW_PAID_FALLBACK=false` semantics preserved and tested).

## 4. Migration strategy and admin-route preservation

* Forward-only, computed in Python, no schema change and no SQL `UPDATE … JOIN`.
* `_upgrade_seeded_order()` reorders **only** when every row for the task is
  provably automatic: `updated_by_admin_id IS NULL`, no `model`, `enabled`,
  `free_only`, and every slug a member of `DEFAULT_NOTE_ORDER ∪
  LEGACY_NOTE_ORDER`. Desired order = `_seed_order ∩ current` + trailing extras;
  rows are never added or removed.
* One transaction writes `route_index` under the UNIQUE constraint
  (`ai_routes_reorder`), logs `ai_routes_reorder` + `provider_route_changed`, and
  is idempotent — a steady-state boot writes nothing (measured 4.5 ms).
* A failed route read → `_safe_routes()` returns `None` → seeding is skipped
  entirely, so a DB error can no longer overwrite configuration.
* Usage rows, credentials, route metadata, audit history and model history are
  never deleted; encrypted credentials are never rewritten.
* Rollback: revert the code; the stored order stays valid for the old build.
  Before deploying, `cp "$DATABASE_PATH" "$DATABASE_PATH.bak-$(date +%F)"`.
  An admin can also press "🧭 بازنشانی ترتیب پیش‌فرض" to realign, or edit a row —
  after which the automatic upgrade stops touching that task.

## 5. New navigation

**User:** `📎 ساخت جزوه` (primary) · `📚 راهنما و قالب‌ها` (help + formats merged) ·
`💳 حساب و اعتبار` (balance + subscription merged, purchase inside it) ·
`🔐 حریم خصوصی` · `⚙️ پنل مدیریت` for admins. Back/Cancel/Home behave identically
on every screen; no redundant confirmation steps; errors carry the next action.

**Admin root:** `📊 نمای کلی و آمار` · `👥 کاربران و مالی` · `🤖 تولید جزوه (AI)` ·
`🎙 گفتار (STT)` · `🛡 عملیات و امنیت` · `↩️ منوی اصلی`. `admin:ai:rt` (route
management) exists **only** inside the AI panel and the STT panel keeps its own
single route entry — no duplicates at the root. Keys, billing attestations, task
routes, model catalogs, usage analytics, dry-run, provider tests, quotas,
benchmarks, provider policies and region restrictions are all preserved, with the
advanced items nested one level down.

Paged screens: `admin:ai:md:{slug}:{page}` (10), `admin:ai:lg`/`lgp:{page}:{filter}`
(14), `admin:ai:keys[:{scope}[:{page}]]` (12), `admin:credentials[:{page}]` (12),
`admin:stt:md[:{slug}[:{page}]]` (10), `admin:stt:mdep:{slug}:{page}:{index}` and
`admin:stt:qdx:{provider}:{position}:{fingerprint8}` (addresses, never names; the
legacy literal `qdel` token keeps working),
`admin:stt:qt`/`qtp:{page}` (3 providers,
4 budgets each), `admin:users[:{page}]` (25), `admin:payments[:{page}]` (20
receipts), `admin:plans[:{page}]` (10) and `admin:special[:{page}]` (12 buttons
over the 30 listed rows). Provider/service/task context
survives every redraw, row actions return to the page they were clicked from,
stale or malformed tokens degrade to a valid page, and a page indicator
(`صفحهٔ X از Y`) appears only when the list is actually paged.

## 6. Performance: methodology and before/after numbers

`python -m scripts.benchmark_admin_panels --rows 20000 --events 2000 --models 600
--users 800 --repeat 20` — offline, temporary SQLite, both the old and the new
query shape in the **same** process, whole panel renders included, no provider
network (all figures in ms/call after one warm-up, mean of 20).
Full output: `/home/user/perf/benchmark_admin_panels_20k.txt`.

| Measurement | Before | After |
|---|---|---|
| `metrics + p95` for one day | 40.5 | **22.4** (`ai_usage_overview(days=1)`) |
| Catalog read for one provider | 5.9 (whole table) | **1.0** first page / 1.8 last page |
| Whole AI catalog redraw (earlier session) | 13.8 | 3.8–5.5 (`stale_rows` gate) |
| Admin home | — | 30.7 |
| AI platform home / providers / plan / routes | — | 24.3 / 3.2 / 43.0 / 2.9 |
| Health panel | live probe on cold cache | **2.2 with zero requests** |
| AI logs page 0 | flat 14 rows, no paging | 6.7 with an authoritative `COUNT`; a stale "page 41" token costs 4.1 and clamps to the last real page instead of showing an empty screen |
| Users (p0 / p10) | 1.2 for a 50-row query + long-message send | 3.7 / 4.2 per page of 25, inline and complete |
| `router.plan("chunk_structuring")` | 41.2 | 41.2 (unchanged path) |
| Pending-receipt page (20 rows) | 21 queries: the list + one `payment_detail` per row | **1 query**; `payment_detail` called 0 times (a spy in the suite pins it) |
| AI routes panel | 2.9 | **73.2** — deliberately: one `plan()` + a per-leg credential lookup per redraw, database-only |
| `ensure_seeded()` steady state | 15.9 cold | 4.5, writes nothing once aligned |

`EXPLAIN QUERY PLAN`: overview metrics use the covering `idx_ai_usage_created`;
the p95 sample adds a temp B-tree bounded by `LIMIT 5000`; the model page uses
`idx_ai_models_provider`; the event page scans a ring-pruned table; the user page
scans plus a covering `idx_submissions_user_id` and a temp B-tree. **No new index
was added** — at these sizes each candidate was ≤ 5 ms, so the benefit was
undemonstrated and the write cost was not worth paying.

Run-to-run caveat, stated plainly: repeating this command after the last two
code changes measured *every* screen up to ~1.5× slower than the run above
(`router.plan` 41.2 → 62.6 → 46.1 ms, admin home 30.7 → 43.7 → 43.1 ms) on the
same machine with the same arguments. Absolute numbers here therefore drift with
sandbox load; the BEFORE/AFTER pairs measured inside a single run are the
trustworthy figures (in the latest run: 47.3 → 28.5 ms overview, 6.8 → 0.9 ms
catalog page), and cross-run comparisons are indicative only. That is also why
defect 12's cost is reported as "2.9 → ~58-73 ms in the same 20 k-row database"
rather than as a percentage on a healthy machine.

The deliberate costs, both inside one run: the user page is ~1.4× the old
single query (1.42 → 2.00 ms) and the AI log page gained one `COUNT`
(~1.5 ms). Both bought completeness — 800 users and the whole filtered event
history reachable — not speed. The routes panel pays a `plan()` per redraw.

The admin credit screen is the same trade, measured at 600 entitlement rows and
a 3 000-row ledger (mean of 20 calls, same process, temporary SQLite, no
network): **1.46 → 2.84 ms** for two covering `COUNT`s and one aggregate that
replaces a Python sum, and it is the difference between the panel reporting
`paid_seconds = 0` and reporting the 900 seconds the reservation engine will
actually spend. `EXPLAIN QUERY PLAN` for the new reads: `SEARCH entitlements
USING COVERING INDEX idx_entitlements_balance_order (user_id=?)`, `SEARCH
usage_ledger USING COVERING INDEX idx_usage_ledger_user_created (user_id=?)`
and `SEARCH e USING INDEX idx_entitlements_balance_order (user_id=? AND
status=?)`. No new index.

The STT quota screen's honest total costs one covering-index `COUNT` per provider on
the page: measured at 3 providers × 25 quota rows, **1.02 → 1.56 ms** per overview
render, while the row read itself got *smaller* (three rows displayed instead of twenty
fetched and then sliced). `EXPLAIN QUERY PLAN` for the count: `SEARCH stt_quota_snapshots
USING COVERING INDEX idx_stt_quota_latest (provider=?)`. No new index.

Re-measured after the last five rounds (same command, same arguments, one run — read
the in-run BEFORE/AFTER pairs, not the absolute values): overview reads
41.3 → 20.7 ms, catalog 5.4 → 0.7 ms first page and 1.2 ms last page, user page
0.80 → 1.07 ms, health panel from cache 2.2 ms, AI log page 6.1 ms (3.1 ms for an
out-of-range page, which clamps), `ensure_seeded()` 4.4 ms in steady state. The
shape the table above describes is unchanged; nothing in these rounds moved a
number materially.

Two screens are the slowest in the panel and both are slow on purpose:
📈 نمای مسیر و بودجه (52 ms) and 🧭 مسیرها (46 ms) each pay one authoritative
`router.plan()` eligibility read (40.7 ms on the same database) rather than a
cached summary, because a route panel that disagreed with the router would be worse
than one that takes 50 ms to open.

The sampled-percentile label costs nothing: `latency_sample_capped` is a
comparison on rows the read already fetched, and the note is one suffix on three
existing lines. The cap itself stays — removing it would mean sorting every
latency of a busy day in Python to render one number — but the number can no
longer be read as a window statistic when it is not one.

* The two user screens from defect 21 cost nothing measurable to bound: the plan page and the
  entitlement window are slices of reads that already happened, and `list_plans()` still has no
  `LIMIT` on purpose — `ORDER BY sort_order, id` is what keeps a paging window stable. The
  balance screen's *totals* keep summing every active row, so printing fewer rows never caps the
  number that matters.

* `/history`'s new total costs one `COUNT(*)` per view: 0.351 ms for a user with 5 000 payment
  rows (mean of 100, temporary SQLite, covering index per `EXPLAIN QUERY PLAN`) against 2.892 ms
  for the ten-row join the screen was already doing — about 12 % on top of the read it already
  paid, and no migration.

## 7. Exact test commands and actual results

```
/home/user/venv/bin/python -m pytest tests/ -q      → 1219 passed, 1 skipped, 1842 subtests in 173.94 s
/home/user/venv/bin/python -m unittest discover -s tests → Ran 1220 tests, OK (skipped=1), 144.7 s
/home/user/venv/bin/python -m scripts.validate_docx → PASSED: 0 structural failure(s)
/home/user/venv/bin/python -m ruff check .          → All checks passed!
/home/user/venv/bin/python -m compileall -q gamas_bot scripts tests passenger_wsgi.py → OK
/home/user/venv/bin/python -m scripts.validate_provider_platform → 25 passed, 0 failed, exit 0.
    Note the `-m`: invoked as a bare file path the script cannot import `gamas_bot`
    unless the repo root is on `PYTHONPATH`, and an earlier checklist line was read as
    a pass through a pipe that hid exactly that. The exit code is now checked.
/home/user/venv/bin/python -m scripts.benchmark_notes --mode standard --json --router → ran offline,
    reports account_entitlement_unverified / terms_of_use_blocked / trial_only_blocked skips
/home/user/venv/bin/python -m pip check             → No broken requirements found.
/home/user/venv/bin/python -m scripts.sync_requirements --check → requirements.txt is up to date
/home/user/venv/bin/python -m scripts.benchmark_admin_panels --rows 4000 --events 500 --models 120
    --users 200 --repeat 5                            → 5 s ; the 20 k-row run takes 24-58 s
    depending on sandbox load
```

Baseline before the work was `1076 passed, 1 skipped, 1793 subtests`. **143 tests
were added and the pre-existing failure count stayed zero** (1076 + 143 = 1219
passing, the same one skip throughout, and 49 more subtests). No live or billable provider call was made and no real
credential was used anywhere: the credential tests run against Fernet-encrypted
rows in temporary SQLite files with fake key material.

Behaviour coverage requested in the brief, by test file: seeding/migration →
`test_route_seed_upgrade.py`; eligibility, 429/5xx failover, cooldown,
invalid-credential rotation, per-key quota rotation, attribution, reservation
release, leg deadline, FREE_ONLY and unknown-billing blocking →
`test_route_failover_eligibility.py`; paging, ack, stale callbacks and label
contracts including both credential screens, plus the quota-screen bounds and the
route-availability annotation → `test_admin_panel_presentation.py`; a Telegram refusal on a finished
Word file and a failing text fallback, both proving the raw transcript and the note text still go
out and the job is recorded as delivered → `test_media_intake.py` (both cases fail against the code
before defect 25 and pass against this).

## 8. Limits, residual issues, honest caveats

* Correction to an earlier draft of this report: the AI log panel was never
  fetching a bounded superset — it fetched `size + 1` rows at a real `OFFSET` and
  used the extra row as a "there is a next page" probe. It now asks
  `ai_event_count()` for the total (same WHERE clause as the page) and clamps a
  stale page token to the last real page, which is what the STT log panel already
  did. `ai_usage_recent()` / `stt_usage_recent()` still have no offset, but both
  are only used for fixed-size digests (the overview, and a key's detail screen
  reading 8 rows), so no list became unreachable because of that.
* The admin users list total still comes from `stats()["users"]` (a second cheap
  query), not from the page query.
* p95 is approximate by design: at most 5 000 most-recent samples are sorted.
* A blocked route row reads `🟢 … ⚠️ <دلیل>` rather than `⛔`: the 🟢 stays a
  billing-class mark and the ⚠️ carries the planner's verdict. Splitting the glyph
  itself would conflate "what this provider costs" with "whether it runs now",
  which is why the annotation was added next to the row instead.
* That annotation costs one `plan()` per route-panel redraw (~73 ms measured,
  database-only; 46 ms in the latest run), and the plan/budget panel pays the same
  single read (52 ms). Caching either was excluded by the no-new-caches rule.
* The quota overview shows 4 budgets and 3 snapshots per provider and says how many
  were folded away, with a link to the provider's own screen — which itself reads a
  20-row window, now labelled, with the rest left in `stt_quota_snapshots`. Bounded
  and explicit at both levels, but not the full list in one message.
* The admin audit screen (`admin:audit`, `/audit`) still renders the latest 30
  entries of each ledger with no paging. It is labelled «آخرین», it goes through
  the long-message splitter so it can never be too long to send, and the DB caps
  the read at 200 — a digest by design rather than a silent cut. Left as it is
  instead of adding a fourth paged list for its own sake.
* Three read-only digests (benchmarks, provider-detail quotas, health) state their
  display bound instead of being paged: the lists are capped because one Telegram
  message is capped, not because the data is large, and each one now names what was
  left out. Paging them would add navigation to a screen whose whole job is a glance.
* The credit screen's «موجودی قابل استفاده» deliberately became the reservation
  engine's predicate: a grant whose `starts_at` is in the future is no longer shown
  as spendable (it is still listed in the history), which is the number that matches
  what a job can actually consume rather than the number a sum over recent rows
  happened to produce.
* The route-step model picker keeps accepting the old `admin:stt:rset:<provider>:<model>` token. Those buttons exist on administrators' screens already, and a message that rendered was by definition within the byte budget — refusing them would break a working click to protect against a case that cannot occur. A model id containing `:` was truncated by the old parser and still is on that legacy path; the new position path is exact.
* Three other button labels are already short enough by hand — `label[:16]` on the key rows, `model.model_id[:32]` in the model list, `f"{free_mark} {m.model_id[:26]}"` in both model pickers. They cut silently, which is crude, but they cannot overflow, so they were left alone rather than restyled for uniformity. Replacing them with `button_label()` is a cosmetic change with a test-churn cost and no defect behind it.
* Digit style inside a panel is still mixed: the counters that predate this work
  (`کلیدهای ثبت‌شده: 0`, log page headers) render Latin digits, while the new digest
  labels go through `to_persian_digits`. Normalising every counter in both panels is
  presentational and was deliberately left out of this sweep.
* The purchase screen pages at 8 plans. The point was sendability, not browsing UX, and a catalog
  that long is unusual — so a plan can now be one press away, and the heading only says
  «صفحهٔ X از Y» when a second page really exists. Same trade for the balance screen: 8 rows
  printed, all rows counted, remainder named.
* Per-provider billing prices, model identifiers and free-quota numbers are
  provider-side facts; the docs record them as dated observations, not
  guarantees, and entitlement is always re-checked from live signals.
* Note-quality pipeline untouched: schemas, chunking, outline, semantic coverage,
  deterministic QA, repair/compile gates, scrubbing, Persian normalisation, RTL
  Word and PPTX generation. A later round changed how those finished files are
  *handed to Telegram* (defect 25) — nothing about how they are produced, and the
  two tests that pin it drive `_process_submission` itself, not a copy of its logic.
  QA, validation, provider accounting and billing safeguards were not weakened to
  obtain a green run.
* Deliberately *not* "fixed" after reading the code: reservation release on
  cancellation (already in `finally`), payment idempotency, banned-user checks in
  both layers, health-probe concurrency, masked-only key screens, DB usability
  after a mid-flight cancellation.
* The Telegram text pager was audited in the round that found defect 25 and found
  sound, so nothing was changed there: `render_pages` / `split_message` were driven
  through fourteen adversarial inputs (a 9 000-character token, a 600-word table
  cell, a 400-row table, an 800-line fence, a 9 000-character line inside a fence,
  unclosed and crossed emphasis, 3 000 astral-plane emoji, a list item longer than
  the budget, `limit=1`) and every emitted page came back ≤ 4 032 UTF-16 units with
  balanced tags, in order and without a lost character — including the suspicious
  cases of `.strip()` removing indentation at a cut, fence markers dropped from an
  over-eager split, and whitespace-only pages being discarded. `PaginationTests` in
  `tests/test_regressions.py` already pins those invariants, so no pager tests were
  duplicated.
* Two other sendability bounds were measured rather than assumed, and both are safe
  as they stand: the 1 000-character caption clamp on a delivered document is
  character-based where Telegram counts UTF-16 units, but `MAX_TITLE_CHARS = 200`
  and `MAX_FILENAME_TITLE_CHARS = 50` keep the worst legal caption far below 1 024
  units; and the same bounds keep every generated filename under the 255-byte
  per-component limit (`PC_NAME_MAX`), inside a write that already falls back to a
  plain text file. A clamp written for a length that cannot be reached would have
  been dead code, so it was left out.
* `/broadcast` splits through the same pager, so any length is *sendable*; what is
  uncapped is the number of pages and the serial one-by-one send over every user. It
  was left alone for that reason — the failure mode would be slow, not corrupt.

## 9. Deployment, migration and backup

1. Backup first: `cp "$DATABASE_PATH" "$DATABASE_PATH.bak-$(date +%F)"`. Nothing
   in this change set migrates the schema, so a restore is just the reverse copy.
2. Deploy the code and restart the worker. On boot, `ensure_seeded()` walks all six seeded route
   sets — the four note tasks (`chunk_structuring`, `outline`, `repair`, `final_compilation`) plus
   `health_test` and `benchmark` — and realigns only a group that is still provably automatic; the
   log will contain `provider_route_changed` only for tasks it actually reordered. The
   `AI_DEFAULT_NOTE_ROUTE` override is applied to the four note tasks alone.
3. Verify with `python -m scripts.validate_provider_platform` and the panel:
   `پنل مدیریت → 🤖 تولید جزوه (AI) → مسیرها` — expected nara first, openrouter
   second, and a Persian reason for anything greyed out.
4. Only then add or rotate credentials per provider. Do not enable a paid route
   unless `AI_ALLOW_PAID_FALLBACK=true` and a billing attestation exist.
5. Optional: run `python -m scripts.benchmark_admin_panels …` after deploying to
   confirm the panel timings on real hardware.
6. Rollback = redeploy the previous build. Stored route rows remain valid for it;
   no data written by the new router needs unwinding.
