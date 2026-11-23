# Changelog

Short, operator-relevant history. Detailed patches remain in Git history.

## Unreleased

**The Mini App is a product, not a dashboard (phase 7).** The four tabs are fixed —
حساب من، کتابخانه، مرور، اکسپلور — and each one is built from the same token layer
(one ink scale, one amber accent, Vazirmatn bundled locally, 20px cards, 44px
touch targets, Persian numerals and Jalali dates). **حساب من** shows the Telegram
identity, the credit balance split into hours and minutes, the active plan window,
purchase options, referral count and invite link, the reward balance with the
catalogue, and the payment history — every figure comes from `/api/v1/me`,
`/me/plan`, `/plans`, `/me/referrals`, `/me/payments` and `/rewards/*`, and buying
still happens in the bot (deep link `?start=buy`, which the bot now honours for an
existing user instead of showing the file request; `?start=support` likewise).
**کتابخانه** lists one row per Study Pack with its processing state, and the detail
screen downloads the pack's real assets, offers Review only when the pack owns
questions, and re-reads itself quietly while the pipeline is still working.
**مرور** groups the user's own questions by the pack that produced them, records
answers through `/review/answers`, shows the stored verdict and explanation, and
files a publication request through `/review/share`. **اکسپلور** is one card at a
time: topic and difficulty chips, large options, the server's verdict and
explanation, the points the server says were awarded (never a computed number),
engagement recorded as dwell time, and a fresh round when the queue runs out. No
counts of the seed bank, no internal scores, no fake links: no screen shows a
number the backend did not send. The app ships with five jsdom integration tests
(`npm test`) that walk all four tabs against the real JSON shapes.

**The bot is a file interface again (phase 6).** A student's whole journey is
`/start` → send a file → watch it process → press the outputs. `/start` is one
line and one input list ("فایل درس را بفرستید تا متن خام، جزوهٔ PDF و Word و ۲۰
سؤال نمونه بگیرید"), identical on the first and every later visit: no balance,
no greeting paragraph, no streak, and no "returning panel" to re-read. It is the
only screen with a button — `WEBAPP_URL` (new setting, `{origin}/app`) — and that
button is the Mini App, which now owns every account screen. The persistent
reply keyboard is gone (`reply_menu_rows()` returns nothing and
`_reply_keyboard` no longer exists), the second-level `menu:more` menu is gone
with its `_more_menu`/`_show_more`, and the completed-file keyboard is only what
the file produced: PDF booklet, transcript/key points/TXT, Word booklet, the
interactive review sheet, and «🧪 ۲۰ سؤال نمونه» (`quiz:new:<item>`, which opens
the size/price screen — the tap itself spends nothing). The referral row and the
"back to main menu" row are gone from that keyboard, and
`LEARNING_DELIVERY_CTA` no longer points at a label that was never drawn.

Labels from keyboards already on screen keep answering, once each:
`/start`-like `🎓 شروع` returns to the file request; `خرید اشتراک` /
`افزایش اعتبار` / `آفیس رایگان` / `پشتیبانی` / `راهنما` / `🛠 پنل مدیریت` still open
their screens, because the Mini App cannot sell credits or run a support
conversation; `📦 خروجی‌های من`, `نتایج پردازش`, `📚 کتابخانه من`, `🤝 دعوت دوستان` and
`بیشتر` answer with one line and the Mini App button (`texts.MINI_APP_REDIRECT`)
instead of re-opening a chat-side copy of the account. `/help`, `/buy` and
`/support` are the same screens by command, and `/office` opens the free Office
draft whose keyboard entry retired with the others, so nothing needs a keyboard
to be reachable. `help_screen` is a short guide again (what to send, what you
get, the commands, the billing and input notices) whose single button is «📤
ارسال فایل» → the start screen, and the raw-only deployment still never promises
a booklet it cannot build.

Nothing was deleted from the backend: billing, credit deduction, subscriptions,
payment confirmation and receipts, referrals, support tickets, admin commands,
the database, the API, background jobs, STT and booklet generation are untouched
and still reachable through their callbacks and the Mini App. Flashcards stay
absent from every user-facing surface. `WEBAPP_URL` is documented in
`.env.example` and `docs/DEPLOY_MINIAPP.md` (empty = no menu button, never a dead
link).

**The Explore bank is 200 distinct questions (migration 042).** The seed "bank"
was ten questions emitted twenty times with a `(پرسش N)` suffix, and nothing
seeded it in production, so Explore had either 190 near-duplicates or no content
at all. `gamas_bot/explore_bank.py` now holds 200 hand-written Persian questions at
`easy` difficulty across 12 topics (زیست‌شناسی ۱۸، بدن انسان ۱۸،
سلامت و پزشکی ۱۴، شیمی ۱۸، فیزیک ۱۸، ریاضی ۱۶، فناوری ۱۴، رایانه ۱۴، تاریخ ۲۰،
جغرافیا ۲۰، محیط زیست ۱۴، دانش روزمره ۱۶), each with four distinct options, one
unambiguous correct answer (position spread 45/62/45/48 across the four slots)
and a one-line explanation; the correct answer is never in a predictable slot.
`explore_questions.seed_key` (new nullable column, unique when present) gives
every seed question a stable identity, so `seed_explore_questions()` is
idempotent: it inserts only missing keys, never rewrites a row, and skips a
question whose normalized text already exists — a seed question can therefore
never become a twin of something a user published. Text normalization folds
Arabic/Persian letter variants, diacritics, ZWNJ, punctuation and spacing before
comparing, and a Jaccard check over content words rejects conceptual duplicates
(the bank's worst pair scores 0.50, against a 0.60 gate). The numbered
placeholders written by the old generator are demoted to `status='rejected'`
rather than deleted, so their impressions and rewards survive and an admin can
restore one (`--keep-legacy` keeps them). Nothing seeds at `connect()`, so tests
and scripts that insert a fixture question with an explicit id keep working; the
bot's `run()` seeds once at startup (best-effort, logged), and
`python3 scripts/validate_explore_bank.py --db gamas_bot.db` does the same from
the shell. Explore still exposes no count, progress or seed concept: the feed
returns `{"items": [...]}` and the recommender's topic affinity, diversity cap,
freshness and seen-question exclusion are untouched.

**Reward redemption grants real credits, atomically (migration 041).**
Redeeming reward points used to debit `reward_points_ledger` and then report a
`credit_seconds_granted` that no `ledger` row backed, so points were spent and
nothing was delivered (audit D-05), while repeating the request spent again. A
redemption is now one `BEGIN IMMEDIATE` transaction that creates a
`reward_redemptions` row, debits the points against it
(`ref_id = 'redemption:<id>'`), inserts the credit lot through the ordinary
billing grant (`ledger`, `kind='credit'`, FIFO-consumable like a paid one, no
separate wallet) and points the redemption at that lot; any failure rolls the
whole thing back and nothing is reported as granted. `POST
/api/v1/rewards/redeem` reads the price and the grant from the server-owned
catalog (`gamas_bot/rewards.py`), enforces `max_per_user` (pre-041 debits still
count, so an upgrade hands out no extra uses), refuses an insufficient balance
and never lets a balance go negative; it accepts an optional
`idempotency_key` (body or `Idempotency-Key` header, unique per user) so a
retried tap or request returns the original redemption instead of spending
again, and the response reports `credit_seconds_granted`, `credited`,
`ledger_id`, `available_seconds` and `idempotent` from what was committed.
`/api/v1/me` and `/api/v1/me/credits` used to read a `credit_seconds` key
`user_identity` never returned and therefore always answered 0 (D-04); they now
answer from the ledger, so the Mini App can refresh credits after a redemption.
Catalog entries that grant no processing credit (`theme`, `badge`, discount)
still debit their points and record the redemption, but report
`credited: false` — no credit is ever claimed for them. Prices, plans, reward
earning and the billing system are unchanged; the migration is additive and
touches no existing row.

**Review is a Study Pack view (migration 040).** The Review API
(`/api/v1/review/groups`, `/questions`, `/questions/{id}`, `/answers`, `/share`)
now reads questions through the pack that owns them and groups them by it, so a
group is titled with its pack instead of `بسته آزمون #<session_id>`. Questions
generated before packs existed are returned as explicit **legacy** groups built
from their real library row — never attached to a pack they did not come from.
Both halves of ownership are checked on every read (session owner and pack
owner), a deleted pack hides its questions, and the answer key is only revealed
for questions the user has already answered. Answering is once-only and
idempotent (a repeated tap returns the stored answer instead of an
`IntegrityError`), and offering a question to Explore is idempotent per question
and its moderation status is visible on the question. Retention no longer
deletes an item that owns questions or is a pack's source row, which is what
previously erased finished quizzes; a pruned delivered file also drops its pack
asset so no manifest points at a missing file. To give pre-pack uploads their
pack and link their questions, run `Database(...).study_packs.
backfill_from_library(dry_run=False)` once after deploying (it is idempotent);
until then those questions are shown as legacy groups. The Mini App builds again
(`npm run build`): the Review tab's missing API bindings and the Library → Review
button are wired to the endpoints above.

**Study Packs come from the pipeline (migration 039).** Every accepted upload now
creates exactly one Study Pack: media, presentation and pasted text by submission,
PDF/DOCX/PPTX uploads by document. The pack is opened at admission with status
`processing` and is finalized by the delivery step from the submission's own
outcome (`ready`, `partial` or `failed`, with `error`/`error_kind`), so a crashed
or failed job can never leave a pack looking ready. Packs own the outputs the
pipeline produced — raw transcript, PDF, Word, review HTML and the delivered
question file — by *reference* into the storage that already holds them
(`transcriptions`, `submission_binary_artifacts`, `documents`,
`library_file_blobs`); no bytes are copied and nothing is duplicated on a retry.
Generated questions record their pack in `quiz_sessions.pack_id`. Historical
library rows are mapped by an idempotent backfill, and the private-asset download
endpoints resolve references only for the owning account. Prices, plans, STT
provider settings and booklet generation are unchanged.

**Office navigation.** The standalone Office button is no longer shown in the
more menu. Existing callbacks, text-file drafts and document exports remain
available; this navigation change does not merge or reprice Office workflows.

**Booklet completion and wallet display.** Successful booklets show a short
completion message and an invite button without replacing the result. Partial
results retain important warnings. Wallet summaries round credit down for display;
exact plan credits and balances remain available in wallet details. Hours, prices
and payment processing are unchanged. Stored list prices are struck through only
when a real discount exists.

**Admin system health and provider accounting.** Elapsed times use duration units,
unknown/partial-delivery errors have Persian labels, and partial issues are
reported separately from failed jobs. The live system screen no longer offers a
misleading period selector. It exposes database probe/schema information and
connection, quota and authentication failures; providers without completed
attempts no longer show 100% success. Quota-denied LLM dispatches and Speechmatics
poll retries now use consistent provider accounting. Logical database read
failures are counted once, including retried reads. Migration 036 persists
aggregate runtime counters across orderly restarts (not abrupt termination);
last-error timestamps and latency samples remain process-local. Windows peak
RSS reporting and multi-day uptime formatting are supported.

**Booklet orchestration and delivery.** Optional synthesis follows a local
preflight and precedes final verification and repair. Model request attempts
record stage, duration, retries and exact/estimated usage without changing
user-credit settlement. PDF, Word and interactive review HTML render offline
from the retained booklet. A failed PDF or Word render is not marked complete;
raw and successful artifacts stay available for retrieval.

**Bounded booklet verification.** Reports identify remaining issues by topic,
section and source reference. Deterministic checks come first; strict repair mode
uses small semantic checks only for ambiguous claims. Each defective node gets
at most one targeted edit. If it fails, the verifier retains healthy content and
reports omissions instead of replacing a safe booklet wholesale.

**Deterministic merge and opt-in synthesis.** Merge keeps source order,
unique educational details, conflicting alternatives, and source references.
`NOTE_COMPILE` now asks for bounded edits rather than a replacement booklet.
Large drafts use whole-topic groups, chapter outlines, and a final complete
outline; rejected or over-budget edits leave the approved content intact.

**Parallel Section Writer.** Planned sections use topic-local evidence, a
source-supported expansion matrix, and the existing note schema and provider
chain. The bounded map preserves plan order and records source block IDs and
failed ranges. Local fallback covers a failed range without regenerating other
sections; integrations can checkpoint and resume unfinished work explicitly.

**Topic-first booklet segmentation.** Planned topics, pages, and slides are
mapped as source ranges. Only oversized topics are subdivided; context and
output failures are retried on the affected range without using truncated JSON.
The model registry and the request's real overhead now determine token budgets,
with Gemini native counting and configurable unknown-model ceilings.

**Booklet content planning.** The deterministic plan groups normalized blocks
into ordered topics and records exact ranges, source facts, terminology, and
repeat references without dropping coverage. Chunk orientation uses the plan
without changing the stored booklet or renderer contracts. Local LLM outline
refinement is opt-in and restricted to very large, weakly structured sources.

**Booklet source normalization.** The existing document model now exposes
stable source blocks and Unicode offsets, including page/slide numbers or
transcript timings when available. Booklet chunks are checked against the
complete normalized source before generation. Partial/stale document caches
are rebuilt from stored text; presentation preambles and supported chart and
SmartArt tails are retained. The old 250 000-character note-generation limit
is gone. The paid map-request ceiling remains, with source-only recovery when
enabled. Credential redaction, provider routing, and the stored booklet and
renderer contracts are unchanged.

**Fix: the «دعوت دوستان» referral screen never rendered.** The share button's
URL embedded the Persian invite text raw — spaces included — which is not a
valid URL; Telegram rejected the whole inline keyboard with
`BUTTON_URL_INVALID`, the buttoned fallback respond failed for the same
reason, and both failures were swallowed, so pressing «🤝 دعوت دوستان» did
nothing. The share URL is now fully percent-encoded (`urlencode`), and the
edit-fallback chain ends with a text-only respond so an invalid button can
never blank a screen again. Also fixed the doubled «اعتبار اعتبار» unit in
the referral rules and join-reward templates (the formatter already appends
the unit), and the share text moved to `texts.py`. Regression tests cover the
rendered screen with a real bot username — the fake client's missing
`get_me` is what hid this.

**Document booklets: same visible quality pipeline as lecture notes.** The
📘 جزوهٔ کامل / ⚡ مرور سریع actions on an uploaded PDF/DOCX/PPTX now wire the
note pipeline's stage callbacks through to the chat progress bar, so the user
sees «اعتبارسنجی جزوه با متن سند» and «ویرایش نهایی جزوه» exactly like the
audio flow — including on the deterministic no-provider path, where the
extractive booklet is the analogue of those passes. Compact revision notes
report per-chunk extraction progress and the final whole-document validation.
An accepted booklet whose coverage is imperfect (the pipeline's
`on_incomplete` signal) is still delivered, but the completion message now
says so explicitly instead of claiming a verified result.

**Free Office: unlimited, categorized PowerPoint decks with a real progress
bar.** The slide cap is gone by default (`OFFICE_MAX_SLIDES=0` = unlimited; a
positive value restores the old hard cap, and an explicit `max_slides=`
argument is still enforced by the builders). Each content section is now
classified from its own lines — grid, statistics, list, prose or mixed — and
rendered with a category ribbon, a Persian badge, colour-coded agenda dots
and a per-category body layout (real RTL tables for grids, tinted cards for
statistics, quote panels for prose). Over-full sections split into «(ادامه)»
continuation slides with the table header repeated, so unlimited decks stay
readable; the user's lines are still never rewritten. The build reports a
multi-stage progress bar (text analysis → deck planning → per-slide graphic
design → delivery) for both session texts and extracted documents.

**Documentation alignment.** Clarified that a transcript is stored and its
charge settled before optional note generation, with downloads offered on the
final results screen. Corrected SQLite artifact storage, test dependencies,
import guidance and historical audit-evidence availability.

**Leaderboard and remaining Mini App/Studio schema remnants removed.** The
user leaderboard (`board` / «لیدربورد»), post-quiz rank display, admin top-users
ranking and leaderboard-gift screens (`adm:board`, `adm:bg:*`, `adm:bgok:*`),
and their repository/protocol methods (`leaderboard_top`, `leaderboard_rank`,
`leaderboard_admin_top`) are removed; quizzes and flashcards remain intact.
Migration `034_remove_leaderboard_and_studio_remnants.sql` drops the unused
`booklet_revisions` and `quiz_point_events` tables together with
`idx_users_quiz_points` while leaving historical migrations untouched so
existing databases upgrade cleanly.

**OCR removed; document ingestion is text-layer only.** The optional i2ocr.com
integration is gone: `gamas_bot/ocr.py`, `scripts/ocr_smoke.py`, the `ocr` extra
(`curl-cffi`), the `OCR_ENABLED`/`OCR_LANGUAGE`/`OCR_MAX_PAGES`/
`OCR_TIMEOUT_SECONDS` settings, their `.env.example` section, the OCR tests and
the feature/troubleshooting documentation. A text-poor PDF and an uploaded photo
are refused with an explicit Persian message instead of being sent to a
third-party recognition service; photos no longer enter the document pipeline.
The stored `documents.source_type='image'` value and migration `017` are left in
place so existing libraries stay readable.

**Booklet prompt and map/reduce corrections.** The Persian generation contract no
longer asks each chunk to suppress its summary, key points, glossary and
source-backed objectives while the default pipeline skips the optional compile;
chunk-local metadata is now merged in source order. The compile prompt now labels
the topic map as orientation only, not full source evidence, and does not ask the
editor to reconstruct missing fields from it. Removed prompt rules that
encouraged decorative tables/callouts and fabricated transitions, and clarified
that instructions found inside source material are data, not commands. A
multi-chunk job with an unrecovered map failure now fails as a whole (or uses a
full-source deterministic fallback), rather than silently merging a subset.
Topic digests preserve their original chunk positions when a chunk has no usable
topic label.

**Gamas document design system for booklets.** DOCX and PDF booklets now share
one art-directed identity instead of two unrelated templates: a central token
layer (`gamas_bot/document/theme.py`), a semantic component vocabulary, and a
manifest-backed artwork set with deterministic, topic-aware cover selection. The
cover is cream paper with a navy frame, teal/cyan curves, an ECG trace and a
layered academic illustration; all Persian text on it is drawn by the renderer,
never baked into an image. Chapters get navy banners, front matter and closing
material a lighter teal register, cards carry an RTL accent edge, tables size
their columns from measured text and repeat a titled header, and the Word footer
keeps a real page field. Rendering now has post-render QA (`inspect_pdf`,
`inspect_docx`, `validate_assets`) plus three representative rendering tests;
content generation, validation, billing, jobs and delivery are untouched.

**Interactive HTML review sheet (third booklet output).** A completed AI booklet
(verified or repaired; never the deterministic source fallback or a degraded
booklet) can now also produce a self-contained, offline, Persian RTL HTML
review sheet with a 30-minute study plan, collapsible questions, exam traps,
comparison pairs and a final one-minute self-test. The planner is one
note-provider call per booklet that emits structured JSON only; the HTML is
rendered deterministically and checked by a hard-error/warning validator before
it is stored or advertised. It is stored as the `review_html` binary artifact
(migration `033_review_html_artifacts.sql` rebuilds the artifact table once,
preserving every row, id and delivery counter), served from the results screen
and Results Hub as a `.html` document, and never regenerated on download. PDF
and DOCX delivery is unaffected when the sheet fails. Full-document Office
workflows send the sheet with their Word/PDF outputs.

**Telegram-only release: Gamas Studio removed.** The Telegram Mini App is
permanently decommissioned: its frontend bundle, WSGI backend and SQLite store,
the `initData` authentication layer, the WSGI/benchmark scripts, its
configuration variables, tests and deployment guides are gone. The bot exposes
no WebView button in the reply keyboard, leaderboard or delivery messages;
PDF/DOCX booklets, transcripts, key points, quizzes, flashcards, the library,
wallet, admin panel and the native leaderboard are unchanged. No database,
migration, table or stored record was modified — the historical
`migrations/v3/026`–`030` objects remain in place so existing databases stay
usable.

**Safe booklet recovery and delivery status.** Numeric/term evidence retains
original text spans so the final scrub removes only disproved occurrences and
unsupported clauses; high-risk medical claims are prioritized and removed as a
unit when a value cannot be safely isolated. A scrubbed booklet is re-audited,
with a source-only structured outline as the deterministic fallback. Structural
validity is checked before rendering. Safe content loss (including after compile)
is reported as `partial` rather than discarding the booklet solely for a
coverage ratio; notices are aggregated. Final `done`/progress completion now
requires a successful results reply, while failed notifications remain `partial`.
Failed/restarted submissions with a saved non-empty transcript recover as
`partial`; the one-time status backfill covers older rows too.

**Billing catalog and Gemini 3.5 request contract.** The current commercial
catalog is six explicit 30-day plans (3, 6, 10, 20, 26 and 30 hours), each with
separate list and sale prices; the unconfirmed 50-hour tier is not offered.
Wallets distinguish the two amounts, while purchase snapshots and revenue use
the final sale price. Full startup catalog sync deactivates omitted built-in
plans only; custom plans, payment records and frozen snapshots remain intact.
The old global credit-package price is retired: `100,000 credits = 3 hours =
10,800 seconds` is only a usage-unit conversion.

Gemini 3.x requests now use `responseFormat.text` and `thinkingConfig.thinkingLevel`
without sampling parameters; Gemini 2.x keeps its legacy request dialect. Quiz
task policy follows the original requested count, and each learning job resolves
source sensitivity once and carries it through all chunk requests. Quiz
validation and duplicate rejection emit count-only diagnostics.

**Follow-up: compile policy, prose claims, incompleteness.** The editorial pass
received the booklet's label but did not forward it to `_compile_once`, so
`NOTE_COMPILE=true` silently dropped OpenRouter from the compile only; the
label now reaches the single, grouped and second-level compile requests.
`auto` classification gained case-narrative markers (`UNKNOWN`) and honorific
names (`SENSITIVE`), with the explicit caveat that it is an identifier
heuristic, not de-identification.

The final gate now also verifies prose: a section sentence that shares less
than 20 % of its content words with any source sentence is reported as
`unsupported_claims` and rejects the booklet, while a sentence with source
vocabulary but no shared signal still passes. Persistent omissions are no
longer silently presented as verified: an accepted booklet that still omits
source numbers or terms is delivered as `partial` with
`error_kind=notes_incomplete` and an explicit notice. `compiling_notes` is only
reported when the compile will run, the presentation failure notice no longer
promises a Word file that this path does not produce, and log details are
reduced to printable ASCII so an echoed lecture sentence cannot reach `bot.log`.
No database, migration or note-budget change.

**Booklet acceptance, sensitivity propagation and diagnostics.** Booklet
generation, repair, coverage fill and compilation now share one data-sensitivity
label, resolved once per submission by the new `NOTE_DATA_SENSITIVITY`
(`auto` classifies the source and fails closed to `UNKNOWN`; an explicit
`standard_educational`/`internal`/`sensitive`/`phi_or_pii` declaration overrides
it). OpenRouter stays ineligible for `UNKNOWN` material at every stage, and each
request logs a `LLM route policy` line naming eligible and blocked routes.

Repair is now a bounded loop (`MAX_NOTE_REPAIR_ATTEMPTS = 2`) that re-audits the
whole booklet after each attempt, stops early when an attempt is not accepted,
scrubs unsupported numbers and logs fixed/unresolved signals. A new final gate
(`enforce_final_acceptance`) rejects a booklet that still asserts an unsupported
number or term: the job keeps the transcript, ends `partial` with
`error_kind=notes_quality`, and tells the user the booklet was not verified, not
that it was made. `NOTE_QUALITY_GATE=warn` observes without rejecting.

Evidence matching gained canonical date signals, so `۱۴۰۲/۵/۱۲` and
`1402-05-12` are one fact while a different day is still fabricated; Persian
digits, Arabic letter variants, half-spaces and the approved equivalent term
list continue to be normalized, and a nearby number never grounds a different
one. Provider failures now log the HTTP status and a sanitized provider message
(`detail=`) while user-facing errors keep class names only, and Speechmatics
403s distinguish account access from a rejected key. Job states add
`validating_notes` and `compiling_notes` (runtime states over the existing TEXT
column; no migration). `NOTE_STRICT_GROUNDING` and the output/context budgets
were not changed.

**Provider/validator remediation.** Text generation now exposes only Gemini,
AvalAI and OpenRouter; removed settings, profiles, dialects and probes cannot
activate legacy routes. Stale credentials/slots from either the environment or
`.env` are ignored with names-only warnings, not startup errors. Task priority
wins over list order/preferences and OpenRouter stays the last fallback;
`UNKNOWN` content is still excluded from it. Gemini-only deployments respect
`GEMINI_MODEL` and explicit provider-chain restrictions. Shared OpenAI-compatible
transport/schema code for approved gateways and STT remains available.

Quiz parsing now requires a nonempty source and valid difficulty by default,
matching the schema and generation caller; flashcards require a source too.
Explicit legacy parsing flags remain available. Non-string difficulty tags
and non-finite numeric locators are rejected, with Persian/English difficulty
aliases and finite numeric locators still supported. Offline fixtures opt into
fake approved credentials only where generation is mocked; routing, no-hedging,
quota, cancellation and strict/permissive validator regressions are exercised
behaviorally. Lint cleanup and an annotation-only mypy fix do not change
billing behavior. Conservative context/chunk/output budgets and `NOTE_COMPILE=false`
are unchanged; no production environment, database, migration or deploy was changed.

**Gamas Studio mobile reliability and startup.** The Telegram SDK is loaded
asynchronously; SDK/initData failures have an explicit retry/explanation state.
Home is the only startup API request; leaderboard and Studio data are loaded on
first use and cached only in page memory. Read requests coalesce while in flight,
use bounded timeouts, and expose persistent manual retry; mutations are not
automatically retried. Telegram theme/viewport and BackButton are feature
detected. The Persian RTL UI has responsive light/dark tokens, touch targets,
safe-area padding and mobile editor controls while preserving all Studio
features.

**Mini App WSGI and SQLite.** Home summary work is now one read-only connection
and one query instead of five connection/read operations. `/ready` is a
read-only SQL/schema probe distinct from the existing `/health` file-exists
check. Static responses now support ETag/Last-Modified validation and correct
HEAD/304 behavior; request IDs and WSGI timing are recorded without request
bodies or signed credentials. No schema migration, index, Passenger directive,
bot process or live database was changed. cPanel documentation now targets the
reported root-domain setup and requires live verification; production and
physical-device results remain outstanding.

**Reporting, booklet delivery and auditability.** Admin completion rates now
separate fully completed work from partial transcript delivery; PDF booklet
success is counted only from persisted PDF bytes. PDF button send attempts are
tracked in bounded SQLite counters so user retries remain distinct from logical
submissions. Invalid renderer output is not persisted as a final artifact.
Referral payment rewards are limited to the referred user's first approved
payment, with a banned-referrer decision recorded transactionally, and the
leaderboard aggregates reward rows before joining invite counts. The v2 importer
keeps source payment IDs and links eligible paid credit lots to imported payment
history while validating balances and foreign keys.

**AvalAI hardening.** The OpenAI-compatible note route now captures AvalAI's
`avalai-request-id` for every successful normal or streaming completion and
logs it with token usage, so exact User API cost reconciliation has a
correlation id. Error messages prefer the same id and redact secrets. The LLM
transport has a short connect timeout, refuses redirects that could forward a
bearer key, and caps provider `Retry-After` delays at 60 seconds. `--check`
rejects accidentally configured AvalAI User API or Google-native API bases, and
the standalone AvalAI smoke script no longer prints URL credentials, queries or
fragments.

**Large uploads.** Default video/audio and PowerPoint/document upload ceilings are now 1 GiB; presentation extraction permits up to 1 GiB unpacked with a 2 GiB workspace budget. Operators must provision adequate temporary disk and verify Telegram/host transfer limits.

## 3.0 — Gamas Studio, reference-grade booklets, tariff at release

**Mini App (Gamas Studio).** Persian-first RTL single page with Home,
Leaderboard (all-time / weekly / monthly), Submissions, structured Booklet
Editor, Preview, Exports and Revision History — no build step. Deploy:
cPanel Passenger or Cloudflare Tunnel (this Mini App was removed later; see
Unreleased).

**Structured editing.** The editor works on the canonical `StructuredNotes`
model: sections, subsections and blocks (paragraphs, lists, definitions,
examples, key points, callouts, process steps, tables, formulas, glossary) with
add/delete/reorder and cell-level table editing. Validation is the model's own
(`from_payload`); unknown blocks are rejected. Every save re-renders both the
Persian RTL PDF and the editable DOCX and stores the bytes against the new
revision — exports serve stored bytes and never re-render.

**Speakers.** Per-submission speaker list with stable engine labels and
owner-chosen display names and roles (migration 028, additive). Renaming never
touches audio, timestamps or segments and never infers identity.

**Booklets.** Native `StructuredNotes` PDF renderer: generated Persian cover and
topic map, hierarchy, educational blocks, RTL tables, glossary, running
furniture and an explicit auto-generation disclaimer. Additive migration 027
persists owner-scoped PDF/DOCX bytes; the completion screen sends nothing
automatically and each output button returns the exact stored file.

**Historical tariff (superseded by Unreleased above).** At the 3.0 release,
the then-current tariff treated 100 000 credits = 3 hours = 10 800 seconds as
65 000 Toman, with proportional 30-day built-in plan prices. This records the
catalog at that release only; it is not a current price rule. Historical
payment snapshots and custom plans were untouched at the time.

**Quality.** PDF visual QA (`tests/test_pdf_visual_qa.py`) checks geometry,
fonts, ink coverage and text collisions, and found three real glyph/layout
defects — a missing step-connector arrow, callout icons missing from Vazirmatn,
and a cover badge drawn over the topic text — all fixed.

## 2.x — providers, hardening, billing recovery

- Note-provider failover with per-provider keys/models, bounded retries,
  structured-output compatibility ladder and a deployment-specific tested
  Gemini → OpenRouter profile. The recorded NaraRouter HTTP 502s were for the
  specific `laguna-s-2.1` model; they do not establish a failure of the shipped
  `agnes-2.5-flash` default.
- Cache-stable static note prompt for provider prefix caching.
- Durable payment/learning recovery: pending charges, one-time refunds,
  forward-only payment states, receipt fingerprinting, append-only lot ledger
  with a materialized balance cache.
- Broadcast runs with resume checkpoints and permanent-failure tracking;
  channel-membership gate with cached confirmations and a bounded sweep.
- Bounded everything: jobs, queues, uploads, document expansion, Office drafts,
  provider bodies and outputs.

## 1.x — the bot

Transcription, RTL Word booklets, PDF export, study documents, Free Office,
library, wallet, support tickets and referrals on Windows PowerShell and cPanel.

**Migration numbering.** `migrations/v3/` contains two files numbered `016` (`016_office_library_kind.sql` and `016_subscription_enforcement.sql`). This is harmless today: the loader sorts filenames and tracks each by name. New migrations must continue from `032_`.
