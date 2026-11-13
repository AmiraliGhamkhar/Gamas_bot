# Gamas Studio repository audit (baseline)

**Audit date:** 2026-11-13 (UTC)  
**Checkout:** `arena/a38d187b-gamas-bot`, starting at `8115bdd`  
**Working tree at start:** clean  
**Scope:** repository evidence only. No production deployment, cPanel account, domain, or physical mobile device was accessible from this checkout.

## Executive findings

The Mini App is a small, framework-free WSGI application with a vanilla JS client and a SQLite store shared with the independently managed bot. The product functionality is already extensive: Home, three leaderboard windows, Studio submissions, speaker naming/roles, structured and legacy booklet editing, revision restore, PDF and DOCX exports. Replacing the architecture is not justified.

The strongest code-supported startup issue is that `mini_app/app.js` requests Home, leaderboard, and submissions simultaneously during boot even though Home alone is the initial view. This causes three authenticated HTTP requests and multiple database opens before secondary views are used. The Telegram SDK is a parser-blocking external script in the document head; on a slow/unreachable `telegram.org` path it can delay parsing and does not have an explicit timeout/retry UI. API calls have no timeout, duplicate-request guard, explicit screen-level error/retry state, or request correlation shown to the client. These are verified implementation properties; their contribution to any particular phone's observed delay remains unmeasured.

The reported intermittent 32 KB JavaScript transfer and mobile-only failure cannot be reproduced without the live host/network or the affected devices. The WSGI static responder reads the small file completely, sets its `Content-Length` and JavaScript MIME type, but currently does not implement validators and returns a body for a `HEAD` request at application level. The latter will be covered by a regression test before/after any change. Hosting-level compression, HTTP/2, persistent connections, and Apache static routing are not established by repository evidence.

## Verified repository facts

- Vanilla frontend bundle: `mini_app/index.html`, `mini_app/app.css`, and `mini_app/app.js`; measured at baseline as 6,543 / 14,590 / 58,296 bytes respectively (CSS and JS source are not minified or built).
- WSGI entry point: `scripts/mini_app_wsgi.py`, callable `application`; it constructs `MiniAppStore` against `DB_PATH` and delegates to `gamas_bot.mini_app.create_app`.
- Separate root `passenger_wsgi.py` delegates to the bot launcher. It is not the Mini App entry point and is not to be overwritten.
- Default DB path in application configuration is `data/gamas_v3.db`; WSGI does not run migrations. `MiniAppStore` opens a new SQLite connection per call, uses a busy timeout and `query_only=ON` for reads; bot storage uses SQLite WAL and owns migrations.
- Telegram authentication uses raw `initData`, HMAC-SHA256 with the bot token, constant-time comparison, a default 300-second age window, and a future-date limit. API writes and reads use owner-scoped store operations. Existing tests cover signatures, expiry, future timestamps, ownership, limits, API behavior, structured editing and exports.
- Migration files 027 and 028 exist in the repository and define persisted booklet artifacts and Studio structured editing/speaker roles. The repository has migrations through 031. This does **not** prove which migrations have run on the live cPanel database.
- The CSP is set by the WSGI static responder and permits same-origin assets plus the Telegram SDK and Telegram frame ancestors. It should be retained unless an observed requirement demonstrates otherwise.
- `/health` currently reports `database` from a path-exists check; that is not a database query/readiness check.
- `MiniAppStore.profile()` currently performs five separately opened read operations for the Home profile, entitlement, balance, counts and artifact count. The initial Home request therefore has avoidable SQLite connection/query setup.
- The cPanel documentation is internally inconsistent with the stated deployment baseline: `docs/MINIAPP_CPANEL.md` directs operators to create a new subdomain, while the requested deployment uses the root domain. The repo itself cannot confirm the live cPanel Python version, application root, `.htaccess`, Passenger version/restart behavior, permissions, `.env`, cron schedule, BotFather setting, or migration state.

## Reproduced / directly verified issues

1. **Unnecessary initial requests (code-path verified).** `boot()` invokes `loadHome()`, `loadRank()` and `loadSubmissions()` after authentication, although the user first sees only Home. The handlers load the leaderboard and Studio again when those views are selected.
2. **External SDK can gate document parsing (HTML verified).** The script tag for `https://telegram.org/js/telegram-web-app.js` has no `async`/`defer` and occurs before the stylesheet and page markup.
3. **Weak network failure UX (code verified).** `call()` uses `fetch()` without a timeout or abort signal. Startup failures only display a transient toast; Home has no persistent retry state. Repeated view selection issues requests even if no data changed.
4. **Static cache validation absent (code verified).** Static responses use a fixed five-minute cache policy and do not send `ETag` / `Last-Modified` or honor conditional requests. `HEAD` is routed but `_static()` returns the file bytes from the application.
5. **Home query overhead (code verified).** `profile()` opens five SQLite connections for the one Home request.
6. **Initial automated test environment incomplete.** `python -m unittest tests.test_mini_app -q` was executed: 57 tests ran, with 15 errors because runtime dependencies (`telethon`, `aiohttp`, and other project dependencies) were not installed in the initial checkout environment. The expected crash-path test logs its synthetic exception. The declared runtime and development dependencies were subsequently installed in an ignored local virtual environment, and the focused/full tests are reported below.

## Potential defects / unresolved hypotheses

- The affected mobile network might be unable to retrieve the Telegram SDK, but mobile reachability was not tested. Defer/failure UX will make this observable and recoverable; it will not establish the network root cause.
- The 32 KB reset could originate in the client network, TLS/CDN/proxy/Apache/Passenger path, or a one-off connection interruption. No live repeated GET/HEAD/range/conditional tests were possible; byte-range support is not required for these small assets.
- The delay may involve cPanel cold starts, DNS/TLS, Telegram WebView behavior, or slow database/provider-independent app requests. No live timing/correlation data is available.
- The large transcript JSON and structured editor content are fetched only when their views are opened by the current code, but there is no browser-level response-size/load test in the repo.
- `MINI_APP_STATIC_DIR` is read from the process environment in the WSGI entry point while other settings use `from_env()`. Validate cPanel environment loading before relying on a custom value.
- Current Home summary issues multiple SQL reads and is the highest-value database path to consolidate. Other data-heavy requests (speaker transcript and booklet/editor/revisions) are user initiated and should remain lazy.

## Security and deployment constraints

- Preserve signed `initData`; never use `initDataUnsafe` for authentication, persist raw credentials, log signed payloads, or relax freshness.
- Preserve per-user ownership checks, `query_only` read connections, SQLite WAL/concurrent bot access, existing CSP, and the separate bot process.
- Do not change `passenger_wsgi.py`, the live `.htaccess`, virtual-host config, Python application settings, database, cron job, or BotFather button without explicit authorization and live verification.
- Root-domain routing, compression, HTTP/2, asset direct-serving permissions and exact Passenger restart behavior require inspection in the cPanel account; no administrator-level Apache assumptions are safe.

## Files in scope for focused work

- Frontend: `mini_app/index.html`, `mini_app/app.css`, `mini_app/app.js`.
- HTTP/WSGI: `gamas_bot/mini_app.py`, `scripts/mini_app_wsgi.py` (only compatible readiness/diagnostic wiring if needed).
- SQLite: `gamas_bot/mini_app_store.py`.
- Validation: `tests/test_mini_app.py` and focused frontend smoke tests where the available toolchain permits.
- Documentation: `README.md`, `docs/README.md`, `docs/MINIAPP_CPANEL.md`, `docs/CONFIGURATION.md`, `docs/TESTING.md`, `docs/TROUBLESHOOTING.md`, `docs/SECURITY.md`, `docs/DEVELOPMENT.md`, `docs/CHANGELOG.md`, and this audit.

## Implementation plan (small, evidence-led)

1. Make initial Home rendering independent of the Telegram SDK network fetch and backend completion; add explicit SDK/auth failure and retry UI.
2. Load only Home at startup. Load leaderboard / submissions on first view entry, cache their non-sensitive response in page memory, coalesce duplicate read requests, set a bounded request timeout, and expose persistent retry/error states. Keep mutation requests non-retried.
3. Apply Telegram theme and viewport/back-button APIs with feature detection; retain the current lightweight vanilla implementation and existing product areas.
4. Consolidate Home reads to one read-only SQLite connection/query. Do not add migrations or alter data.
5. Add static validators and correct `HEAD` behavior, request IDs/timing metadata, and a readiness check that is explicitly distinct from the existing liveness/path check. Keep diagnostics metadata-only.
6. Redesign styles for a polished Persian RTL, theme-aware, narrow-screen Telegram UI; preserve all editing and export controls.
7. Add focused regression tests, run the dependency-complete test suite, measure local WSGI assets/endpoints repeatedly, and document that live-host / mobile acceptance remains outstanding.
8. Rewrite deployment guidance around the existing root-domain cPanel Passenger setup only as an operator-verified target; provide preflight checks, exact safe commands, BotFather verification, release and rollback steps. No production change is part of this work.

## Measurement baseline and limitations

At repository baseline, JS is 58,296 bytes, CSS 14,590 bytes and HTML 6,543 bytes. These are source file sizes, not compressed wire sizes. Three startup requests are visible in code. Live p50/p95, device load time, transfer completeness/repeated resets, HTTP/2/compression/cache support, SDK mobile accessibility and cPanel database latency are unavailable until measured against the actual host and devices. Before/after local timings will be explicitly labeled laboratory measurements and will not be represented as phone or production results.

## Audit follow-up — implementation completed in this worktree

These changes are local to the active development branch; **nothing was deployed**
onto the live cPanel host or database.

- The SDK is asynchronous and has visible failure/retry states. `ready()` is
  called before the first API wait; only Home is fetched at startup. Leaderboard,
  submissions, editor, revisions and speaker transcript data are loaded when
  their view is opened. Read calls have coalescing, bounded timeout, in-memory
  caching and manual retry; writes are never automatically retried.
- The RTL UI has light/dark theme tokens, Telegram theme/viewport listeners,
  safe-area padding, BackButton navigation, narrow-screen touch controls and
  persistent Home/rank/Studio/editor/speaker/revision loading/error states. All
  existing Studio features remain in the DOM and handlers.
- The Home summary uses one read-only SQLite connection and one data query,
  skipping optional fields when the corresponding additive schema/table is not
  present. No migration or index was added. `/ready` is a read-only DB/schema
  check, while `/health` remains a path-exists liveness check.
- Static routes now send ETag/Last-Modified, use HTML revalidation and a short
  static cache, honor 304, return no body for HEAD, and preserve MIME, length,
  CSP and traversal protection. Request IDs and app timing are logged/headered
  without logging request bodies or signed initData.
- Focused frontend/backend smoke tests: 65 passed on the final focused run.
  `node --check`, full `ruff check .`, byte compilation, requirements consistency,
  `pip check`, and `git diff --check` passed. Targeted mypy passed for the three
  changed Python runtime modules. Full pytest earlier in the work: 1,793 passed,
  2 skipped, 252 subtests passed; full unittest earlier: 1,795 ran, 2 skipped.
  A later compatibility-only change to the Home profile and final frontend
  retry/status refinements were rerun through the focused 65-test set. Full-repo
  mypy is currently blocked by an unrelated pre-existing type error in the
  untouched `gamas_bot/db_referrals.py:211`.
- The 500-iteration in-process benchmark is recorded in
  [`MINIAPP_CPANEL.md`](MINIAPP_CPANEL.md#repository-local-beforeafter-benchmark-2026-11-13).
  It confirms the synthetic Home connection count moved 5→1; it does not
  establish mobile, cPanel, real-network or production latency.

Outstanding evidence: live DNS/TLS/assets/SDK/API checks, cPanel settings and
migration markers, repeated mobile transfer tests, and real Android/iOS/Telegram
Desktop UI acceptance. The initial mobile failure cause remains unproven.
