# API Contract

The JSON API is served by `gamas_bot/webapp_api.py` (stdlib WSGI, no framework)
from the same origin as the bot's Passenger app, and is consumed by the Mini App
in `miniapp/`. This page lists exactly the routes the code answers; anything not
listed returns `404`.

## Authentication

Every `/api/*` route except `/api/health` and `/api/v1/health` requires
`X-Telegram-Init-Data`, validated server-side with HMAC-SHA256 against
`TELEGRAM_BOT_TOKEN` (`gamas_bot/telegram_auth.py`): sorted payload, constant-time
compare, `auth_date` freshness, `user.id` required. Missing, expired or tampered
data is `401 Unauthorized`. The browser never sends a `user_id`; ownership always
comes from the validated payload, and a foreign id resolves to `404`.

Admin routes additionally require the authenticated id to be in `ADMIN_IDS`;
otherwise `403 Forbidden`.

## Health

| Route | Response |
| --- | --- |
| `GET`/`HEAD /api/health` | `200 {"status": "ok"}` |
| `GET`/`HEAD /api/v1/health` | `200 {"status": "ok"}` |

## Account

| Route | Response |
| --- | --- |
| `GET /api/me`, `GET /api/v1/me` | `{"user_id", "credit_seconds", "reward_points", "free_uses", "banned", "bot_username"}`. `credit_seconds` is `available_seconds()` from the ledger, never a stored counter. `free_uses` is `{"quiz": bool}`. |
| `GET /api/v1/me/credits` | `{"credit_seconds", "free_uses"}` |
| `GET /api/v1/me/subscription` | `{"required_channel", "channel_url", "required"}` — `required_channel` is `null` when the gate is off |
| `GET /api/v1/me/plan` | current plan code/title, active window (`expires_at`), and `unlimited` |
| `GET /api/v1/plans` | `{"plans": [{"code", "hours", "price", "original_price", "validity_days"}]}` — read from the `plans` table, priced > 0 only |
| `GET /api/v1/me/payments` | `{"payments": [{"payment_id", "plan_code", "status", "created_at", "amount_toman", "purchased_seconds"}]}` (latest 50, price is the order-time snapshot) |
| `GET /api/v1/me/referrals` | `{"invited_count": int}` |

## Library (Study Packs)

| Route | Response |
| --- | --- |
| `GET /api/packs`, `GET /api/v1/library` | `{"packs": [...]}` |
| `POST /api/packs` (`{"title", "source_type"}`) | `201 {"pack": {...}}` |
| `GET /api/packs/{pack_id}`, `GET /api/v1/library/{pack_id}` | `{"pack", "assets", "question_count"}` |
| `DELETE /api/v1/library/{pack_id}` | `{"deleted": true, "pack_id": ...}` |
| `GET /api/v1/library/{pack_id}/assets` | `{"assets": [...]}` |
| `GET /api/v1/library/{pack_id}/download/{asset_id}` | binary stream, `Content-Disposition: attachment`, `Cache-Control: private, no-store` |
| `GET /api/assets/{asset_id}/download` | the same stream for an asset the caller owns |

Downloads resolve the asset inside the caller's own pack, re-check the owning row
of every reference (artifact/transcription/document blob) and refuse path escapes.

## Review (private, per Study Pack)

| Route | Response |
| --- | --- |
| `GET /api/v1/review/groups` | `{"groups": [...], "limit", "offset", "total"}` — one group per Study Pack, plus explicit legacy groups for questions that predate packs |
| `GET /api/v1/review/questions` | `{"questions", "limit", "offset", "total"}`; filter with `?pack_id=`, `?group_key=pack:{id}` or `?group_key=legacy:{item_id}` |
| `GET /api/v1/review/questions/{question_id}` | `{"question": {...}}` |
| `POST /api/v1/review/answers` (`{"question_id", "chosen_index"}`) | `{"is_correct", "correct_index", "explanation"}` — answering twice is idempotent, never a `500` |
| `POST /api/v1/review/share` (`{"question_id"}`) | `201` on a new publication request, `200 {"already_shared": true, ...}` on a repeat |

`correct_index`/`explanation` are only returned once the question has been
answered; an unanswered question never exposes the key.

## Explore (public curated question bank)

| Route | Response |
| --- | --- |
| `GET /api/v1/explore/feed` | `{"items": [{"question_id", "topic", "difficulty", "question_text", "options"}]}` — never a bank count or index |
| `POST /api/v1/explore/answers` (`{"question_id", "chosen_index", "dwell_ms"}`) | `{"is_correct", "correct_index", "explanation", "points_awarded"}`; only a first-time correct answer to an approved question earns points |
| `POST /api/v1/explore/engagement`, `POST /api/v1/explore/impressions` (`{"question_id", "dwell_ms", "action"}`) | `{"recorded": true}` — stores signal `2` (dwell > 8 s), `1` (skip < 3 s) or `0`; engagement never grants points |

## Admin moderation

| Route | Response |
| --- | --- |
| `GET /api/v1/admin/explore/pending` | `{"items": [...]}` (latest 50 `pending` questions), `403` for non-admins |
| `POST /api/v1/admin/explore/moderate` (`{"question_id", "action": "approve"\|"reject"}`) | `{"question_id", "status"}`, `404` for an unknown id |

## Rewards

| Route | Response |
| --- | --- |
| `GET /api/v1/rewards/balance` | `{"balance": int, "history": [...]}` (latest 50 ledger entries) |
| `GET /api/v1/rewards/catalog` | `{"catalog": [...]}` — enabled rewards only |
| `POST /api/v1/rewards/redeem` (`{"reward_code", "idempotency_key"?}`) | `{"balance", "credit_seconds_granted", "available_seconds", ...}`. Redemption writes the points debit, the credit lot and the redemption row in one `BEGIN IMMEDIATE` transaction, so an `idempotency_key` replay grants nothing twice and the response reports only what the ledger holds. |

## Errors

`400` malformed/invalid JSON or payload (body capped at 1 MiB), `401` failed
`initData` validation, `403` non-admin on an admin route, `404` unknown id or a
row owned by somebody else, `500` unexpected failure with no internal detail.
