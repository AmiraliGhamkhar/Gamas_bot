# Mini App — Gamas Studio (ranking and booklet editing)

A Telegram Mini App — **Gamas Studio** — that shows the leaderboard and lets a
student edit their own booklet (structured editor) and manage speaker names.
It is a plain WSGI application with no JavaScript build
step, so it runs on the same cPanel host as the bot or behind Cloudflare.

| Piece | File |
| --- | --- |
| WSGI entry point | `scripts/mini_app_wsgi.py` |
| HTTP layer, signature verification | `gamas_bot/mini_app.py` |
| SQLite access | `gamas_bot/mini_app_store.py` |
| Front end (no build) | `mini_app/index.html`, `app.css`, `app.js` |

## Two deployment routes

### 1. cPanel "Setup Python App" (Passenger)

1. Point the application's **Application startup file** at
   `scripts/mini_app_wsgi.py` and the **WSGI callable** at `application`.
   Passenger imports the module and calls `application(environ, start_response)`;
   there is no ASGI server, no event loop and no websocket anywhere.
2. Put the domain's document root outside the repository, or map the app to a
   subpath such as `/miniapp`. The bundle is served by the WSGI app itself, so the
   document root is irrelevant to it.
3. Set `MINI_APP_URL=https://<your-domain>/miniapp` in `.env` and restart the bot.
   The leaderboard screen then grows a WebView button. With `MINI_APP_URL` empty
   the button is not drawn at all — a deployment without the app never shows a
   button that cannot open.

The bot keeps running as a separate long-lived process (cron watchdog,
`scripts/ensure_running.py`). The Passenger worker only serves this app; it never
starts a Telegram client, so the two never fight over the session file.

### 2. Cloudflare-fronted container

Run any WSGI server on a local port and let Cloudflare proxy to it:

```bash
gunicorn --workers 2 --threads 4 --bind 127.0.0.1:8080 scripts.mini_app_wsgi:application
```

Telegram requires **HTTPS** for a Mini App URL, so the proxied hostname must have
Cloudflare's TLS on. Nothing else is needed: no websockets, no `X-Forwarded-*`
handling, no sticky sessions. `gunicorn`/`uwsgi` are deployment dependencies, not
application ones — the app itself imports only the standard library.

### Local smoke test

```bash
python -m wsgiref.simple_server 2>/dev/null   # not needed; the deploy runs it
MINI_APP_STATIC_DIR=$PWD/mini_app python - <<'PY'
from wsgiref.simple_server import make_server
from scripts.mini_app_wsgi import application
make_server("0.0.0.0", 8080, application).serve_forever()
PY
```

`GET /health` answers `{"service": "gamas-mini-app", "ok": true}` so an uptime
monitor can poll it. It exposes no configuration and no user data.

## How a request is trusted

Every API call carries Telegram's `initData` string. The server recomputes the
HMAC that Telegram signed with the bot token and compares it in constant time:

```
secret   = HMAC_SHA256(key="WebAppData", msg=bot_token)
check    = "\n".join(f"{name}={value}" for name in sorted(fields) if name != "hash")
expected = hex(HMAC_SHA256(key=secret, msg=check))
```

`signature` (the newer Ed25519 field) is excluded, because Telegram does not
include it in the HMAC it computes for bots. `auth_date` must be within
`MINI_APP_AUTH_MAX_AGE_SECONDS` (default 300) in the past and at most a minute in
the future; anything else is a 401 with a message that deliberately does not say
which check failed.

The `user.id` inside the signed payload — never a query parameter, never the
request body — is the only identity the app acts on. Every SQL statement is
scoped to that id, so guessing a booklet's number cannot read or edit somebody
else's booklet.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` , `/static/*` | the bundle (CSP-restricted, `nosniff`, no referrer) |
| `GET` | `/health` | liveness JSON, no credentials |
| `POST` | `/api/leaderboard` | `{initData, limit}` → top rows + the caller's rank |
| `POST` | `/api/booklets` | `{initData}` → the caller's booklets |
| `POST` | `/api/booklet` | `{initData, id}` → one booklet (text + structured) |
| `PUT` | `/api/booklet` | `{initData, id, text}` → save legacy plain text |

(The full Studio surface — structured saves, exports, revisions, speakers — is
listed under “Gamas Studio 3.0” below.)

Limits: request bodies are capped at 1 MB, edited text at 200 000 characters
(the same budget the quiz and flashcard builders read), and `limit` is clamped to
50 rows.

## What an edit changes, and what it does not

There are two editing paths, and both are deliberate:

* **Structured edit (the Studio editor).** Saving validates the document
  through the canonical `StructuredNotes` model, appends a revision, and
  re-renders **both** the PDF and the DOCX from that model with the same
  engines the pipeline uses. The rendered bytes are stored per revision as
  binary artifacts, so the Exports tab and Telegram download buttons always
  serve a file that matches a saved revision. The raw transcript and the
  original pipeline output are never overwritten.
* **Plain-text edit (the legacy fallback).** Items without structured content
  (older submissions) still save `library_items.content_text` — the cache the
  quiz, flashcard and booklet-learning paths read — and append a text
  revision. The Word file already delivered to Telegram stays a snapshot.

Rendering happens synchronously inside the save transaction boundary on
purpose: a failed render aborts the save, so a revision can never point at
missing or half-written artifacts.

## Why SQLite directly instead of the bot's API

The bot and the Mini App are separate processes. The bot owns an `aiosqlite`
connection; a Passenger worker cannot borrow it. Both open the same file instead:

* WAL mode (already on, see `docs/ARCHITECTURE.md`) lets the app read while the
  bot writes;
* reads set `PRAGMA query_only`, writes use one short `BEGIN IMMEDIATE` with a
  3-second busy timeout;
* a connection is opened per request, so no worker holds a snapshot open and the
  `-wal` file cannot grow without bound on a shared host;
* the app never migrates the schema — that stays the bot's job.


---

## Gamas Studio 3.0 — structured editing, speakers, dual-format exports

The Mini App is now **Gamas Studio**, a Persian-first RTL single-page app with
five areas: **Home** (credits, points, counts, working shortcuts),
**Leaderboard** (all-time / weekly / monthly), **Submissions** (per-submission
detail with speakers and available artifacts), **Booklet Editor** and its
**Preview**, **Exports**, and **Revision History**.

The booklet editor is **structured**: it edits the canonical `StructuredNotes`
model (title, summary, sections, paragraphs, lists, definitions, examples, key
points, callouts, process steps, tables, formulas, glossary) rather than a
plain-text blob. The data flow is one-directional and never lossy:

```
StructuredNotes  →  Mini App editor  →  validated StructuredNotes
                                                 ↓
                                     PDF renderer + DOCX renderer
                                                 ↓
                                   binary artifacts per revision
```

There is no structured→text→reconstruct round trip anywhere. Editing support is
add/delete/reorder (move up/down) at the section, subsection and block level;
tables are editable cell-by-cell with row/column add and remove.

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/home` | POST | the caller's own, non-private summary |
| `/api/leaderboard` | POST | `window` = `all` / `weekly` / `monthly` |
| `/api/submissions` | POST | the caller's submissions with artifact availability |
| `/api/booklets`, `/api/booklet` | POST | list booklets / read one (text **and** structured) |
| `/api/booklet` | PUT | save — `document` (structured) or `text` (legacy) |
| `/api/booklet/export` | POST | rendered bytes: `format` = `pdf` / `docx` |
| `/api/revisions`, `/api/revision` | POST / PUT | history and restore |
| `/api/speakers` | POST / PUT | anonymous turns, owner-chosen names and roles |

Save-time validation is the canonical model's, not a hand-rolled copy: block
types, required fields, table shapes, ordering and per-field size bounds are
enforced by `StructuredNotes.from_payload`; control characters are stripped
first (they would make Word refuse the file), and unknown block types are
**rejected** (backward compatibility is carried by the stored JSON, not by
accepting arbitrary input). Limits: 1 MB request body, 400 000 serialized
characters, 200 sections, 200 000 characters of plain text.

Every structured save re-renders both formats and stores them against the new
revision. Exports are served from the stored artifacts — downloading is free,
idempotent, and never re-renders on the hot path.

### Speakers

`/api/speakers` groups the stored diarization turns per submission. Labels are
the stable speaker ids from the pipeline (`SPEAKER_00` style) — never a guessed
identity. The owner may set a display name and an optional role per label; both
are stored in `speaker_names` (migration 028) scoped to the submission and user.
Renaming never touches the audio, timestamps or segments; names and roles are
refreshed into the saved structured JSON so transcript views show them, and the
original label is kept whenever no name was confirmed.

### Migration 028

`migrations/v3/028_studio_structured_editing.sql` is additive (new table
columns and one index). The bot applies it on start; the WSGI worker never
migrates. Until the bot has run once, structured saves fail with 503 rather
than half-writing.

Every endpoint re-verifies raw `initData` with the bot token. Weekly/monthly
boards read the append-only `quiz_point_events` table; all-time still reads the
authoritative `users.quiz_points`. `scripts/mini_app_wsgi.py` builds the full
`MiniAppData` facade per request — see `docs/GAMAS2.md` for deployment order.
