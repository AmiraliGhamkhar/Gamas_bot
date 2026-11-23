# API Contract

All `/api/*` endpoints (except `/api/health`) require `X-Telegram-Init-Data` validated server-side via HMAC-SHA256.

## Implemented (Phases 2–3)
- `GET /api/health` -> `200 {"status": "ok"}`
- `GET /api/me` -> `200 {"user_id", "credit_seconds", "reward_points", "free_uses", "banned"}`
- `GET /api/packs` -> `200 {"packs": [...]}`
- `POST /api/packs` (`{"title", "source_type"}`) -> `201 {"pack": {...}}`
- `GET /api/packs/{pack_id}` -> `200 {"pack": {...}, "assets": [...]}`
- `DELETE /api/packs/{pack_id}` -> `200 {"deleted": true, "pack_id": ...}`
- `GET /api/assets/{asset_id}/download` -> `200` binary stream (`Content-Disposition: attachment`, `Cache-Control: private, no-store`)

## Planned (Phases 4–5)

### Account, Subscription, Credits, Referrals, Payments
- `GET /api/account/summary` -> extends `/api/me` with subscription status, referral code/stats, recent payments.
- `GET /api/payments` -> list user's payment history.
- `GET /api/referrals` -> referral link, invited count, earned seconds.

### Review (Private Pack Questions)
- `GET /api/review/sessions` -> list user's `quiz_sessions` (optionally filtered by `pack_id`).
- `GET /api/review/sessions/{session_id}` -> questions for a session (without exposing `correct_index` until answered, or with review mode).
- `POST /api/review/sessions/{session_id}/answer` (`{"question_id", "chosen_index"}`) -> `200 {"is_correct", "correct_index", "explanation"}`.

### Explore (Public Question Feed & Engagement)
- `GET /api/explore/feed` -> `200 {"items": [{"question_id", "topic", "difficulty", "question_text", "options"}]}` (never returns total bank count or index).
- `POST /api/explore/answer` (`{"question_id", "chosen_index", "dwell_ms"}`) -> `200 {"is_correct", "correct_index", "explanation", "points_awarded"}`.
- `POST /api/explore/events` (`{"question_id", "dwell_ms", "action": "skip"|"dwell"}`) -> `204` (records `2` if `dwell_ms > 8000`, `1` if `dwell_ms < 3000`, `0` otherwise).
- `GET /api/explore/preferences` / `PUT /api/explore/preferences` -> read/update `user_topic_preferences`.

### Rewards
- `GET /api/rewards` -> `200 {"balance": int, "history": [...]}`.
- `POST /api/rewards/redeem` (`{"reward_code": str}`) -> `200 {"balance": int, "credit_seconds_granted": int}`.
