# Mandatory channel subscription — engineering report

Status: implemented on branch `arena/d3fca9e8-gamas-bot`, full suite green
(`1224 passed, 62 subtests passed`). This report separates what is verified
automatically from what needs a live/manual check.

## What was built

A reusable `SubscriptionService` (`gamas_bot/subscription.py`) plus a
centralized gate in `GamasBot`. The service exposes:

- `check_membership(user_id, *, force=False, allow_cache=True) -> MembershipResult`
- `require_subscription(user_id, *, force=False) -> bool` (decision-only gate)
- `require_access(event, *, force=False) -> bool` (the event gate, renders the
  join prompt / unavailable screen)
- `handle_callback(event, user_id, data)` / `handle_check(event, user_id)`
- `startup_self_check() -> bool` and `close()`

Membership is verified against the Telegram **Bot API `getChatMember`** method,
so a plain `user_id` and the channel reference are all that is needed. The gate
runs on every message and every non-subscription callback, before the intake
pipeline, and administrators (`ADMIN_IDS`) are exempt.

## Transport decision

MTProto `channels.getParticipant` (Telethon `GetParticipantRequest`) was
considered first and rejected: its constructor requires an `InputPeer` and a
resolved access hash, and arbitrary user ids do not resolve from the entity
cache, so it would need extra MTProto plumbing (or the bot's own MTProto
session) for no accuracy gain. Bot API `getChatMember` is a single HTTP call
that takes `chat_id` + `user_id`, is accurate when the bot is a channel
administrator, and reuses the existing bot token. It is the transport used.

## Status mapping

| Telegram status | Result |
| --- | --- |
| `creator`, `administrator`, `member`, `restricted` | member |
| `left`, `kicked` | not a member |
| malformed / unknown status | unknown |
| `400 … not a participant` / `not a member` / `user not found` | not a member (confirmed denial) |
| `429`, network error, timeout, other `ok:false`, `403 bot-not-admin`, malformed response | unknown (fail open) |

## Fail-open rationale

The only state that blocks a user is a *confirmed* non-membership
(`left`/`kicked`, or a `400 not a participant`). Any ambiguity — Telegram
outage, network error, `403` because the bot is not a channel administrator, a
malformed response — yields `unknown`, the interaction is allowed, and the
administrators are alerted (throttled to one alert per 5 minutes). A Telegram
incident can therefore never lock every customer out of the bot.

## Implemented and tested (offline, automated)

`tests/test_subscription.py` (41 tests):

- Status mapping for every Telegram status, `restricted` → member, malformed →
  unknown.
- `check_membership`: member / `left` / `kicked` / infra-error / `403` /
  malformed / confirmed `400 not a participant`.
- Cache is used only for confirmed members; failures are never cached.
- In-flight dedup shares one request per user; `429` and network errors are
  retried once.
- Gate: non-member denied + join prompt; member allowed; admin exempt;
  unknown fails open and alerts admins (throttled); disabled/unconfigured gate
  allows. `require_subscription(user_id)` returns the same decision without
  rendering.
- `sub:check` callback is routed before the gate; the handler prompts / confirms /
  reports unavailable; other prefixes are ignored.
- Message gate integration: non-member blocked, member allowed, admin allowed.
- Migration columns, `set_subscription_state` round-trip, `identities_for`
  exposing the columns.
- Config: `from_env` reads the new keys, `subscription_channel_url` for public
  and numeric channels, `check()` validates the channel reference.
- `.env.example` coverage test updated; existing suite went from 1184 to 1225
  passing.

## Implemented but not automatically tested (live / manual verification)

These touch the real Telegram API and cannot run in the offline suite:

- `getChatMember` against the production Bot API, including the `left`/`kicked`
  and `restricted` cases for real accounts.
- `startup_self_check()` against the real bot identity (it calls
  `client.get_me()` then `getChatMember(channel, bot_id)`).
- The bot's administrator role in `@gamas_AI` (Telegram returns `403` otherwise).
- Persian message rendering and button layout in the actual Telegram client.
- Real `429` flood-wait timing (the code caps the sleep at 60 s and retries once).

Manual smoke test (from `docs/TELEGRAM_SUBSCRIPTION.md`): with a second account
that is not a member, send `/start` → join prompt; join the channel; press
**🔍 بررسی عضویت** → confirmed; without joining, `/start` again → join prompt.

## Manual configuration required

1. Make the bot an **administrator** of `@gamas_AI` (Telegram side).
2. Add the env keys (or rely on the defaults `SUBSCRIPTION_CHANNEL=@gamas_AI`,
   `SUBSCRIPTION_REQUIRED=true`, `SUBSCRIPTION_CACHE_SECONDS=0`,
   `SUBSCRIPTION_TIMEOUT_SECONDS=20`,
   `TELEGRAM_API_BASE_URL=https://api.telegram.org`).
3. Let the migration apply at startup (`migrations/v3/016_subscription_enforcement.sql`
   is idempotent; it only adds two observability columns to `users`).
4. Optionally raise `SUBSCRIPTION_CACHE_SECONDS` if Telegram rate-limits the
   per-interaction checks; the default `0` is the strict, fail-safe setting.

## Not implemented (by design or out of scope)

- **Force-adding users to the channel.** Telegram forbids it; the bot only
  reports membership.
- **MTProto `getParticipant` transport.** Rejected (see above).
- **Multiple mandatory channels.** One channel per deployment.
- **Subscription as a billing/payment gate.** Billing, bans and admin
  authorization are unchanged and sit on top of this gate.
- **Private-channel subscriptions.** `getChatMember` needs the bot to be an
  administrator; an unresolvable channel surfaces as the "unavailable" screen,
  not a denial.
- **OCR** (unrelated to this feature; not part of the project).

## Risks and limits

- The gate is only as accurate as the bot's channel-administrator rights. The
  startup self-check and the throttled admin alert surface a misconfiguration;
  they do not block startup.
- `SUBSCRIPTION_CACHE_SECONDS>0` trades strictness for Telegram request volume.
  The default `0` re-checks every interaction, so a leaver is restricted on
  their next message but each message costs one `getChatMember` call.
- The subscription state written to `users` is observability only; the gate
  never trusts it, by design.
