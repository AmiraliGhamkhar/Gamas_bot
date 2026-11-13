# Gamas Studio — cPanel / Passenger deployment and operations

This guide describes the repository implementation and the **reported** Gamas Studio deployment target. It is not evidence that the live cPanel configuration or current production release has been checked. Verify every path and value in cPanel before executing deployment commands. This repository change does not deploy to production.

## 1. Target configuration and verification gate

The reported production target is:

| Setting | Reported value | Verify before changing anything |
| --- | --- | --- |
| Public Mini App URL | `https://gamasrobot.ir/` (root domain; do not move to a subdomain) | cPanel domain list, TLS, current BotFather Menu Button |
| cPanel user / home | `gamasrob` / `/home/gamasrob` | Terminal: `id -un; printf '%s\n' "$HOME"` |
| Repository / application root | `/home/gamasrob/gamas_bot` / `gamas_bot` | `git -C /home/gamasrob/gamas_bot rev-parse --show-toplevel` and Setup Python App |
| Python / environment | Python 3.12 / `/home/gamasrob/virtualenv/gamas_bot/3.12` | cPanel Setup Python App screen and `python --version` after activation |
| WSGI startup / callable | `scripts/mini_app_wsgi.py` / `application` | Setup Python App screen; import preflight below |
| Passenger | 6.2.1 was reported | Verify current cPanel app details; do not edit generated directives |
| Database | `data/gamas_v3.db` | Read `DB_PATH` without printing secrets; inspect migration markers read-only |
| Bot process | Separate cron/watchdog managed bot | Confirm existing cron command and `ensure_running.py`; do not launch the bot from Passenger |
| Static bundle | `mini_app/` | WSGI serves `/`, `/static/*`, `/api/*`; keep repository and data outside public web roots |

`passenger_wsgi.py` at the repository root belongs to the separate bot-health/launcher integration. It is **not** the Mini App WSGI entry point and must not be overwritten. No `.htaccess`, Apache virtual-host, Passenger pool, or direct-static configuration is committed here; the actual files and directives must be read on the account, not inferred from this guide.

If any value differs from the table, stop and map the deployed app to the repository before proceeding. Keep `.env`, the SQLite DB, session, logs, and backups outside `public_html` and all other public document roots.

## 2. Architecture and request lifecycle

```text
Telegram WebView
  ├─ GET /                    → WSGI serves index.html (HTML revalidates)
  ├─ GET /static/app.css/js   → WSGI serves files with MIME, length, ETag and Last-Modified
  └─ POST /api/*              → WSGI validates raw signed initData
                                  → MiniAppStore opens read-only SQLite connections
                                  → short owner-scoped data response

Independent bot process → same SQLite file (WAL); bot alone applies migrations
```

- `scripts/mini_app_wsgi.py` builds the `application` callable using `gamas_bot.mini_app.create_app` and `MiniAppStore`.
- `gamas_bot/mini_app.py` owns WSGI routing, request validation, signed Telegram authentication, CSP/static responses and JSON/binary response headers.
- `gamas_bot/mini_app_store.py` performs owner-scoped SQLite reads and short writes. Read connections use `PRAGMA query_only=ON`; the app never applies migrations.
- `mini_app/` is a plain HTML/CSS/JavaScript bundle. There is no Node build or frontend framework in production.
- The Mini App and bot are separate processes. Passenger restart reloads the web worker only; it must not start or stop the bot.

Home loads first. Leaderboard and submission summaries load only when their tabs are opened; booklet content, revisions, speaker transcript segments and stored binary files remain user-triggered. The browser caches only small response objects in page memory. Telegram credentials are not stored in local/session storage, cookies or IndexedDB. Mutating operations are never automatically retried.

## 3. First-time verification in cPanel

Open **cPanel → Setup Python App** and confirm the existing application is configured as reported: Python 3.12; application root `gamas_bot`; application URL the root domain `gamasrobot.ir`; startup file `scripts/mini_app_wsgi.py`; callable `application`; the existing Python environment path. Do not create a second app, subdomain, Passenger mapping, or `.htaccess` rule if the existing root-domain app is active.

In **cPanel Terminal**, first run these read-only checks:

```bash
id -un
printf 'HOME=%s\n' "$HOME"
PROJECT=/home/gamasrob/gamas_bot
VENV=/home/gamasrob/virtualenv/gamas_bot/3.12
printf 'project=%s\n' "$(git -C "$PROJECT" rev-parse --show-toplevel)"
printf 'commit=%s\n' "$(git -C "$PROJECT" rev-parse --short HEAD)"
git -C "$PROJECT" status --short --branch
"$VENV/bin/python" --version
ls -ld "$PROJECT" "$PROJECT/mini_app" "$PROJECT/data" "$VENV"
```

Expected: account `gamasrob`, home `/home/gamasrob`, repository root `/home/gamasrob/gamas_bot`, Python `3.12.x`, a present Mini App bundle, and an existing data directory. A non-empty `git status --short` needs operator review; do not overwrite local changes. These paths are reported values and must be corrected to the cPanel screen if the live account differs.

Check file permissions without printing `.env` contents:

```bash
stat -c '%a %U:%G %n' "$PROJECT/.env" "$PROJECT/data" "$PROJECT/data/gamas_v3.db"
```

Expected policy: `.env` is readable only by its owner (normally `600`), `data/` is owner-only (normally `700`), and the database is not under any web root. Do not change ownership or permissions blindly; if they are too broad, correct them as the cPanel account owner and recheck bot access.

## 4. Check the environment, WSGI import and migration state

Run from the verified project root, in the existing Python 3.12 environment. The commands print presence and paths, never the token value:

```bash
cd /home/gamasrob/gamas_bot
source /home/gamasrob/virtualenv/gamas_bot/3.12/bin/activate
python - <<'PY'
from pathlib import Path
from gamas_bot.config import from_env

settings = from_env()
print('python:', __import__('sys').version.split()[0])
print('TELEGRAM_BOT_TOKEN configured:', bool(settings.bot_token))
print('database file exists:', Path(settings.db_path).is_file())
print('database path:', Path(settings.db_path).resolve())
from scripts.mini_app_wsgi import application
print('WSGI application callable:', callable(application))
PY
```

Expected: Python 3.12.x, token configured `True`, DB file exists `True`, resolved DB path matches the single DB used by the bot, and WSGI callable `True`. The token is not output. If the app needs packages, install only after checking whether the release changed `requirements.txt` or `pyproject.toml`:

```bash
python -m pip check
# Only when a reviewed release changed runtime dependencies:
python -m pip install -r requirements.txt
python -m pip check
```

Inspect migration markers read-only. This command never applies a migration and must not be replaced with manual SQL:

```bash
python - <<'PY'
from gamas_bot.config import from_env
import sqlite3
from pathlib import Path

path = Path(from_env().db_path).resolve()
connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
try:
    names = {row[0] for row in connection.execute(
        "SELECT name FROM schema_migrations WHERE name IN (?, ?, ?)",
        ('027_persisted_booklet_files.sql',
         '028_studio_structured_editing.sql',
         '031_v2_payment_linkage.sql'),
    )}
finally:
    connection.close()
for name in ('027_persisted_booklet_files.sql',
             '028_studio_structured_editing.sql',
             '031_v2_payment_linkage.sql'):
    print(name, 'APPLIED' if name in names else 'NOT PRESENT')
PY
```

The repository contains migrations 027–031. The historical claim that 027/028 were applied is not proof of the current live state. If they are absent, **do not** run SQL manually, rerun migrations, replace the database, or deploy the Studio schema independently. Verify the bot release and its normal migration runner with the maintainer first. The bot owns migrations; the Passenger app is read-only with respect to schema changes.

## 5. Safe release sequence

### 5.1 Record and back up before updating

Use the cPanel Terminal and the verified project/environment paths. Create a private backup directory outside every web root. The SQLite online backup API captures a consistent snapshot while WAL is in use; it does not stop or restart the bot.

```bash
set -eu
PROJECT=/home/gamasrob/gamas_bot
VENV=/home/gamasrob/virtualenv/gamas_bot/3.12
umask 077
BACKUP="$HOME/backups/gamas-studio-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
printf 'code commit: ' > "$BACKUP/release.txt"
git -C "$PROJECT" rev-parse HEAD >> "$BACKUP/release.txt"
cp -p "$PROJECT/.env" "$BACKUP/.env"
cd "$PROJECT"
source "$VENV/bin/activate"
python - "$PROJECT/data/gamas_v3.db" "$BACKUP/gamas_v3.db" <<'PY'
import sqlite3
import sys
source, destination = sys.argv[1:]
with sqlite3.connect(source, timeout=10) as src, sqlite3.connect(destination) as dst:
    src.backup(dst)
    result = dst.execute('PRAGMA quick_check').fetchone()[0]
    print('snapshot quick_check:', result)
    if result != 'ok':
        raise SystemExit('database snapshot validation failed')
PY
chmod 600 "$BACKUP/.env" "$BACKUP/gamas_v3.db"
printf 'backup=%s\n' "$BACKUP"
```

Expected: `snapshot quick_check: ok`; `release.txt`, `.env`, and DB snapshot exist in the private backup directory. Keep backups encrypted and remove only under your retention policy. For bot-session/receipt/log recovery, follow [Operations backup guidance](OPERATIONS.md#backup-and-restore) separately; this app-only release does not alter those files.

### 5.2 Update only a clean, verified checkout

First inspect the release tracking branch and pending changes:

```bash
git -C /home/gamasrob/gamas_bot status --short --branch
git -C /home/gamasrob/gamas_bot branch --show-current
git -C /home/gamasrob/gamas_bot rev-parse --abbrev-ref --symbolic-full-name '@{u}'
git -C /home/gamasrob/gamas_bot remote -v
```

Proceed only when the working tree is clean and the configured upstream is the approved production branch. Fetch and fast-forward that configured branch; this command refuses a merge commit/divergence:

```bash
git -C /home/gamasrob/gamas_bot fetch --prune --tags origin
git -C /home/gamasrob/gamas_bot pull --ff-only
```

Expected: a fast-forward to the reviewed production release. If there is no upstream, local change, divergence, or unexpected branch, stop; do not force-pull, reset, switch branches, or overwrite files. Record the resulting commit and confirm the release diff has no unreviewed database migrations or secrets:

```bash
git -C /home/gamasrob/gamas_bot rev-parse HEAD
git -C /home/gamasrob/gamas_bot diff --name-status HEAD@{1}..HEAD
git -C /home/gamasrob/gamas_bot status --short
```

This optimization release has no new database migration. It changes Mini App code and documentation only; do not run the bot, change its cron job, or stop the independent bot process for this release.

### 5.3 Dependencies and WSGI import

```bash
cd /home/gamasrob/gamas_bot
source /home/gamasrob/virtualenv/gamas_bot/3.12/bin/activate
python --version
python -m pip check
python - <<'PY'
from scripts.mini_app_wsgi import application
assert callable(application)
print('WSGI import OK')
PY
```

If and only if dependencies changed in the approved release, install them in the existing environment using the preceding section and re-run `pip check` and the import check. Never create a replacement virtualenv as a shortcut.

### 5.4 Restart Passenger — not the bot

Use **cPanel → Setup Python App → the existing `gamasrobot.ir` application → Restart**. Confirm the application URL, Python version, startup file and callable before clicking. This is the supported procedure for this reported installation. It restarts the Passenger worker only; do not use `scripts/ensure_running.py` to restart the web app.

A Passenger `tmp/restart.txt` touch is **not** prescribed here: use it only if the current cPanel app root, Passenger documentation, directory and permissions have been checked on this account and cPanel's Restart action is unavailable. Do not add or replace Passenger directives in `.htaccess` or `passenger_wsgi.py`.

## 6. Smoke tests and static delivery measurements

Run from a trusted workstation or cPanel Terminal with outbound HTTPS. These are live-host checks and will only become verified after execution against the actual deployment.

```bash
curl --fail --silent --show-error --max-time 20 -D /tmp/gamas-home.headers -o /tmp/gamas-home.html https://gamasrobot.ir/
curl --fail --silent --show-error --max-time 20 -D /tmp/gamas-health.headers -o /tmp/gamas-health.json https://gamasrobot.ir/health
curl --fail --silent --show-error --max-time 20 -D /tmp/gamas-ready.headers -o /tmp/gamas-ready.json https://gamasrobot.ir/ready
cat /tmp/gamas-health.json
cat /tmp/gamas-ready.json
```

Expected: `/`, `/health`, `/ready` are HTTP 200; `/health` JSON reports `database` as a path-exists check, while `/ready` reports a read-only SQLite/schema probe (`ok`, `database`, `schema`). `/health` is liveness, **not** proof that authentication or every API operation works. Do not expose secrets while copying response bodies into support tickets.

Check both assets, MIME, exact body size, and cache validators:

```bash
for asset in app.css app.js; do
  curl --fail --silent --show-error --max-time 30 \
    -D "/tmp/gamas-${asset}.headers" \
    -o "/tmp/gamas-${asset}.body" "https://gamasrobot.ir/static/${asset}"
  printf '%s: ' "$asset"
  wc -c < "/tmp/gamas-${asset}.body"
  grep -iE '^(HTTP/|Content-Type:|Content-Length:|ETag:|Last-Modified:|Cache-Control:)' \
    "/tmp/gamas-${asset}.headers"
done
curl --silent --show-error --head --max-time 20 https://gamasrobot.ir/static/app.js
```

Expected: HTTP 200; CSS `text/css`, JS `application/javascript`; `Content-Length` equals the saved body's `wc -c`; HEAD has the same length and no body. GET a validator and verify a 304:

```bash
ETAG=$(awk 'BEGIN{IGNORECASE=1} /^ETag:/{sub("\r$", "", $2); print $2}' /tmp/gamas-app.js.headers)
curl --silent --show-error --max-time 20 -H "If-None-Match: $ETAG" -D - -o /dev/null https://gamasrobot.ir/static/app.js
```

Expected: `304 Not Modified`, empty response body, same ETag, and no transport error. HTML revalidates (`Cache-Control: no-cache`); CSS/JS have a five-minute revalidation window. They are deliberately not marked immutable because filenames are not content-versioned. The WSGI process reads/serves static files; direct Apache serving, gzip/Brotli, HTTP/2 and connection reuse are **not assumed** and should be confirmed in the browser Network panel or cPanel/host configuration before documenting them as enabled.

Repeat the body transfer enough times to look for intermittent resets (a single success is not an acceptance result):

```bash
for i in $(seq 1 20); do
  curl --silent --show-error --max-time 30 -o /dev/null \
    -w '%{http_code} %{size_download} %{time_total}\n' \
    https://gamasrobot.ir/static/app.js || echo 'transport-failure'
done
```

All 20 should report the same 200 and byte count, without `transport-failure`. Record all durations and compute p50/p95 rather than selecting the fastest sample. Do not treat `Range` requests that return 200/full body as proof of byte-range support; this Mini App does not need ranges for its small JS file.

Equivalent **Windows PowerShell** checks:

```powershell
$Base = 'https://gamasrobot.ir'
foreach ($Path in '/', '/health', '/ready', '/static/app.css', '/static/app.js') {
  $Watch = [System.Diagnostics.Stopwatch]::StartNew()
  try {
    $Response = Invoke-WebRequest -Uri ($Base + $Path) -TimeoutSec 30
    $Watch.Stop()
    '{0} {1} {2}ms {3} bytes' -f $Path, [int]$Response.StatusCode,
      $Watch.ElapsedMilliseconds, [Text.Encoding]::UTF8.GetByteCount([string]$Response.Content)
    $Response.Headers['Content-Type']
  } catch {
    $Watch.Stop()
    '{0} FAILED after {1}ms: {2}' -f $Path, $Watch.ElapsedMilliseconds, $_.Exception.Message
  }
}
$Head = Invoke-WebRequest -Method Head -Uri "$Base/static/app.js" -TimeoutSec 30
$Head.StatusCode; $Head.Headers['Content-Length']; $Head.Headers['Content-Type']; $Head.RawContentLength
```

For exact JS byte counts, save the body as bytes rather than converting a response string; `Invoke-WebRequest` text decoding can change the reported character count. Network tests from a workstation measure that workstation's route, not the affected phone.

## 7. Telegram configuration and device verification

The reported BotFather Menu Button is `گاماس استودیو` → `https://gamasrobot.ir`. Preserve it. Verify with **@BotFather → /mybots → Gamas bot → Bot Settings → Menu Button**; do not change it just to test a deployment. Verify the app URL is HTTPS and uses the root domain. `MINI_APP_URL` controls bot-generated WebView entry buttons; it does not replace the Menu Button setting.

Every authenticated API request sends `Telegram.WebApp.initData` (the signed raw query string) in its JSON body. The server verifies it; `initDataUnsafe` is display-only and never authenticates. The app uses `ready()`, `expand()` and feature-detects theme, viewport and BackButton APIs. The official SDK at `https://telegram.org/js/telegram-web-app.js` remains an external runtime dependency. If it cannot load, the page gives an explicit error and a retry; first verify that `telegram.org` is reachable from that device/network.

Test on actual Android Telegram and iOS Telegram: open from the bot Menu Button and a Web App button, wait for Home, switch light/dark theme if available, resize/rotate, open Studio and a booklet, edit/save/reopen, use BackButton and export a file. Also test Telegram Desktop, Android Chrome and iOS Safari as controls. An ordinary browser should show a clear “open from Telegram” message and must not be granted API access without signed initData. Mark each device/network result and timestamp; no physical-device result is implied by local tests.

## 8. Diagnostics and troubleshooting

The opt-in URL `https://gamasrobot.ir/?diagnostics=1` shows metadata-only client diagnostics: SDK availability/version/platform, theme/viewport, request status/duration and server request ID. It never displays initData. Browser DevTools should separately inspect HTML/CSS/JS status, MIME, response size, timing, console exceptions and `X-Request-ID`. Server logs record request ID, method, route, HTTP status and WSGI processing duration; they do not log request bodies, initData or user content.

| Symptom | Check and safe next action |
| --- | --- |
| Slow opening | Measure `/`, CSS, JS and `/ready` separately on desktop and phone; compare Wi-Fi vs mobile data; inspect cold vs warm load and request waterfalls. Do not guess DNS/TLS/Passenger without measurements. |
| Blank screen | Confirm HTML and both local assets are 200 with correct MIME; inspect JS console; check whether `telegram.org` SDK loaded. Clear only this site's cache after checking `ETag`. |
| SDK unavailable | On the same affected network, open `https://telegram.org/js/telegram-web-app.js` in a browser and record status/time. Retry in the app; compare Wi-Fi and mobile data. |
| SDK available, API 401 | Open from the configured bot, check system clock and freshness; confirm Passenger uses the same bot token as the bot without printing it. Reopen Mini App to get fresh signed data. |
| API network failure / timeout | Use displayed diagnostic request ID; inspect cPanel app logs and browser Network panel. Retry only read operations manually; do not auto-repeat save/restore requests. |
| API HTTP 500 | Match timestamp/request ID to the Passenger application error log; check WSGI import, environment loading and DB read access. Keep tracebacks/tokens out of user reports. |
| HTTP 502 / 503 | Check cPanel Setup Python App status and error log. 502 often means upstream/worker failure; 503 can also be the app's explicit readiness or migration-gap response. Verify before restarting. |
| HTTP 302 | Inspect the `Location` header. A normal HTTP→HTTPS or canonical-host redirect should end at the same root app. A login redirect, wrong host/path, or loop is not a successful Mini App response. |
| JS download reset | Repeat full GETs and record status/bytes/time from the same network; HEAD alone does not test body delivery. Compare desktop, phone Wi-Fi and phone mobile data. |
| `/health` ok but app fails | `/health` checks only the DB path. Query `/ready`; then use a real signed Telegram request and correlate server logs. |
| Database/schema error | Confirm the app and bot use one `DB_PATH`, database file permissions, WAL sidecars and migration markers. Do not copy only a live `.db`, run migrations manually, or replace the DB. |
| Passenger restart fails | Use cPanel app log and Setup Python App status. Verify Python version, venv, startup file and callable; do not alter `passenger_wsgi.py` or `.htaccess` speculatively. |

When collecting output, remove usernames, file content, personal identifiers and any secrets. Never send `.env` or full request bodies.

## 9. Health, caching and host limitations

- `GET /health`: liveness response with a `database` file-exists boolean. It does not execute SQL or validate auth.
- `GET /ready`: read-only SQLite `SELECT 1` and presence of the `users` / `schema_migrations` tables; 503 if not ready. It does not run migrations or prove every endpoint works.
- Static HTML uses `no-cache`; CSS/JS use `public, max-age=300, must-revalidate`, `ETag`, `Last-Modified`, correct MIME, and accurate `Content-Length`. Conditional requests can return 304. API JSON and binary responses are `no-store`.
- The WSGI static handler still reads these small files and serves bytes. cPanel may or may not enable compression, HTTP/2, caching modules or Apache aliases; no server administrator privileges are assumed.
- SQLite shared WAL reads are supported. Passenger app writes are short and owner-scoped; the bot remains the schema/migration owner. No indexes or migrations were added by this optimization.

### Repository-local before/after benchmark (2026-11-13)

The checked-in `scripts/mini_app_bench.py` was run with 500 iterations against
synthetic SQLite data and the in-process WSGI callable. Baseline source was
commit `8115bdd`; final source was the working tree after this change. The
runner excludes TCP, TLS, Apache/Passenger, cPanel cold starts, compression,
real data sizes, Telegram SDK and device rendering. Each p50/p95 below is the
500-request sample in that run; do not interpret it as production latency.

| Route | Baseline bytes | Baseline p50 / p95 (ms) | Final bytes | Final p50 / p95 (ms) |
| --- | ---: | ---: | ---: | ---: |
| `/` HTML | 6,543 | 0.057 / 0.101 | 9,094 | 0.083 / 0.202 |
| `/static/app.css` | 14,590 | 0.065 / 0.111 | 21,684 | 0.090 / 0.126 |
| `/static/app.js` | 58,296 | 0.056 / 0.106 | 73,493 | 0.132 / 0.186 |
| Signed synthetic `/api/home` | 193 | 1.260 / 1.744 | 193 | 0.485 / 1.516 |

The HTML/CSS/JS bundle grew by about 31.3% in total (79,429 → 104,271 bytes)
for the retry/error UI, Telegram feature detection and responsive redesign. The
first-screen API request count fell from 3 to 1 (verified by the startup code
and Node smoke test); the other two requests are deferred until users open those
views. Synthetic Home profile connections fell from 5 to 1 per request. The
Home timing in this run moved from 1.260/1.744 ms to 0.485/1.516 ms; this is a
local microbenchmark observation, not a promised/production improvement. Asset
read/hash timings are higher in this run and are not claimed to have improved.
Re-run on the deployed domain and actual devices before making a mobile
performance claim.

Repeat the local run on both revisions using:

```bash
python scripts/mini_app_bench.py --iterations 500
```

## 10. Rollback

This release is code-only and adds no migration. Keep the verified previous commit from `release.txt` and the DB snapshot. If smoke tests fail:

1. Stop user traffic only if necessary using the existing cPanel app controls; do not stop the independent bot unless a database safety issue is confirmed.
2. Verify the checkout is clean and locate the private backup. In Terminal:

   ```bash
   PROJECT=/home/gamasrob/gamas_bot
   git -C "$PROJECT" status --short --branch
   BACKUP=$(ls -1dt "$HOME"/backups/gamas-studio-* 2>/dev/null | head -n 1)
   test -n "$BACKUP"
   printf 'backup directory: %s\n' "$BACKUP"
   cat "$BACKUP/release.txt"
   ```

   Confirm the backup path and commit are the ones recorded for the failed
   release. Do not proceed if the working tree has local edits or the commit
   cannot be established.
3. Restore only after reviewing that output and confirming a clean tree:

   ```bash
   PROJECT=/home/gamasrob/gamas_bot
   BACKUP=$(ls -1dt "$HOME"/backups/gamas-studio-* 2>/dev/null | head -n 1)
   PREVIOUS=$(awk '/^code commit: / { print $3; exit }' "$BACKUP/release.txt")
   test -n "$PREVIOUS"
   test -z "$(git -C "$PROJECT" status --porcelain)"
   printf 'checking out previous code %s\n' "$PREVIOUS"
   git -C "$PROJECT" checkout --detach "$PREVIOUS"
   ```

   This checks out repository code; it does not touch the database or `.env`.
   Preserve the deployment's intended branch/release process when returning to
   normal updates.
4. Use cPanel **Setup Python App → Restart** for the existing root-domain app. The bot process is independent and should not be restarted as part of the Passenger rollback.
5. Repeat `/`, `/health`, `/ready`, static GET/HEAD and the Telegram device smoke tests.

Do **not** restore the DB snapshot for this code-only release. A database restore can erase newer bot transactions. If a later release includes migrations, stop and follow that release's explicit migration/rollback plan; this repository has no generic destructive downgrade. Keep `.env` protected and restore it only if it was separately changed and the backup is confirmed.

## 11. Evidence still required from production

The following cannot be established from this repository: current cPanel/Passenger/Python configuration; `.htaccess` and Apache static routing; whether compression, HTTP/2 or persistent connections are active; actual migration markers; TLS/DNS status; mobile SDK reachability; repeated real-host JS transfer behavior; API/database latency under Passenger; and Android/iOS/Telegram Desktop acceptance. Record repeated measurements and device/network details before claiming a production root cause or improvement.
