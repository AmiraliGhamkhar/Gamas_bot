# Security and privacy

## Checklist

- Keep `.env`, the database, session, receipts, logs and backups **outside** every
  web root (`public_html`, `www`, `htdocs`, `httpdocs`, `public_ftp`).
- cPanel: `chmod 600 .env`, `chmod 700 data`. Windows: restrict the project folder
  to the operator account and keep it out of shared/cloud-synced folders.
- Never commit keys or user files. Treat the Telegram session like a password;
  rotate keys that were exposed.
- Run one process per token/session and never bypass an active lock.
- Back up before updates and use audited credit operations, never ledger edits.

## What goes where

| Data | Where |
| --- | --- |
| Audio/video | The configured STT chain (Speechmatics, Deepgram and/or an OpenAI-compatible transcription API) |
| Transcript text for booklets, AI quizzes and AI flashcards | Approved note-generation routes only: Gemini and AvalAI; OpenRouter is a fallback only when sensitivity policy permits it |
| Scan and photo images | i2ocr.com — only when `OCR_ENABLED=true` |
| Transcripts, notes, billing, tickets | local SQLite (`data/gamas_v3.db`) |
| Generated files | Telegram; the library stores message references |
| Mini App requests | your host; signed `initData` is verified on every call |

Which third-party service receives a request depends on the deployment's
private `.env` and process environment. A recoverable provider failure can send
the same audio or text to the next configured route. Model IDs and the code-
default chains are listed in [Providers](PROVIDERS.md); they do not identify a
live server's actual configuration.

Unclassified (`UNKNOWN`) text never goes to OpenRouter. Explicit `INTERNAL`,
`SENSITIVE` and `PHI_OR_PII` labels require per-provider approval; all three
opt-ins default to false. `STANDARD_EDUCATIONAL` is an explicit caller assertion,
not an automatic classification of uploaded content. Gemini/AvalAI eligibility
for `UNKNOWN` is not proof of consent or suitability for patient data. Legacy
provider variables do not activate additional routes.

Review each provider's current terms and get the consent your jurisdiction
requires before processing sensitive material. No third-party deletion or
confidentiality guarantee is made by this project.

## Controls that are already in place

- Admin authorization fails closed; private callbacks verify ownership.
- Channel membership infrastructure errors fail open (a Telegram outage must not
  lock everyone out); confirmed non-membership always blocks. Billing, bans and
  admin checks still apply.
- Receipts are signature-checked, pixel-bounded and re-encoded before private
  storage; paths are validated before reads and deletions.
- Media runs with fixed arguments, no shell and bounded child processes; document
  parsing uses bounded threads (no OS-level memory sandbox).
- Uploads, source text, drafts, response bodies, timeouts and outputs are all
  bounded; overflow is refused, never truncated.
- Logs contain metadata and references only, and are redacted against configured
  secrets. Never add transcript text or receipts to a log.
- The Mini App verifies Telegram's `initData` HMAC (constant-time), enforces
  `auth_date` freshness and scopes every query to the signed user id.

## Mini App specifics

- The reported production URL is `https://gamasrobot.ir/` on the root domain;
  verify it in cPanel and BotFather before changing the menu URL. HTTPS is
  required. The cPanel WSGI/Passenger route is the production target; the
  Cloudflare Tunnel guide is a separate optional deployment.
- Authentication sends raw `Telegram.WebApp.initData` in the JSON request body.
  The WSGI app parses Telegram's signed fields, excludes `hash` (and Telegram's
  newer non-HMAC `signature` field), computes
  `HMAC-SHA256(HMAC-SHA256("WebAppData", bot_token), data_check_string)`, uses
  constant-time comparison, and enforces the configured freshness window
  (300 seconds by default, bounded by configuration) and a future-date limit.
  The user id comes only from the verified signed `user` payload. Never use
  `initDataUnsafe` for server identity, log the raw string, persist it in browser
  storage, or relax freshness as a troubleshooting shortcut.
- Store operations scope each user-specific read/write to the authenticated
  Telegram id. Reads use SQLite `query_only`; writes use short explicit
  transactions. The Passenger process shares the bot's SQLite WAL database but
  does not run migrations. Keep the bot as schema owner and never point either
  process at a copied/secondary database.
- The WSGI static responder applies the existing restrictive CSP, `nosniff` and
  `no-referrer`; it allows same-origin assets plus the Telegram SDK origin and
  Telegram WebApp frame ancestors. The SDK is an external runtime dependency;
  its failure is surfaced without weakening the CSP or bypassing authentication.
- Request bodies are capped at 1 MB and structured/text booklet inputs have
  explicit bounds. PDF/DOCX downloads are owner-scoped and served as binary
  responses with `no-store`; stored artifact bytes are returned as-is.
- Request logs contain only a correlation id, method, route, HTTP status and
  elapsed WSGI time. Never log tokens, full initData, request bodies, private
  transcript/booklet content or unnecessary user identifiers. Opt-in client
  diagnostics show SDK state and request metadata only; see
  [Mini App operations](MINIAPP_CPANEL.md#8-diagnostics-and-troubleshooting).
- `/health` checks that the configured database file exists; it is a liveness
  check, not proof of SQL readiness or authenticated functionality. `/ready`
  performs a read-only `SELECT 1` and core schema-table check and does not
  migrate or mutate the database.

## Reporting

Report vulnerabilities privately to the maintainer. Never publish credentials,
user data or working exploit details in a public issue, and never send `.env`,
sessions, receipts or private transcripts in a support ticket.
