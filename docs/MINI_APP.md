# Mini App — ranking and booklet editing

A small Telegram Mini App that shows the leaderboard and lets a student edit
their own booklet text. It is a plain WSGI application with no JavaScript build
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
| `POST` | `/api/booklet` | `{initData, id}` → one booklet's text |
| `PUT` | `/api/booklet` | `{initData, id, text}` → save the edited text |

Limits: request bodies are capped at 1 MB, edited text at 200 000 characters
(the same budget the quiz and flashcard builders read), and `limit` is clamped to
50 rows.

## What an edit changes, and what it does not

An edit writes `library_items.content_text` — the text cache that the quiz,
flashcard and booklet-learning paths read. It does **not** re-render the Word
file that was already delivered to Telegram: that file is a snapshot, and
re-rendering it would mean loading `python-docx` inside a web worker on every
save for no benefit. The app says so in the editor.

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
