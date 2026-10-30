# Mandatory channel subscription

Every user must be a member of the official Telegram channel `@gamas_AI` before
they can use protected bot features. Membership is verified **per interaction**
against Telegram — the bot never trusts a value written to the database, so a
user who leaves the channel, is banned from it, or rejoins later is checked
against the live channel state the next time they act.

This page is the setup guide and the cPanel deployment checklist for that gate.

## How it works

The bot calls the Telegram **Bot API `getChatMember`** method:

```
POST https://api.telegram.org/bot<TOKEN>/getChatMember
{"chat_id": "@gamas_AI", "user_id": <telegram_id>}
```

The returned `ChatMember.status` is mapped to one of three states:

| Telegram status | Result | User experience |
| --- | --- | --- |
| `creator`, `administrator`, `member`, `restricted` | member | allowed |
| `left`, `kicked` | not a member | join prompt, interaction blocked |
| malformed response / infrastructure error | unknown | interaction allowed (fail open) + admin alert |

Key properties:

- **The bot must be an administrator of the channel.** `getChatMember` returns
  `403 Forbidden: bot is not an administrator` otherwise, and the check cannot
  be performed.
- **A network or Telegram infrastructure error never denies access.** When the
  status cannot be determined the interaction is allowed and the administrators
  are alerted (throttled to one alert per 5 minutes), so a Telegram outage cannot
  lock everyone out.
- **Only a confirmed non-membership blocks a user.** A `400 … not a participant`
  response is treated as a confirmed denial.
- **`restricted` counts as a member.** A restricted member can still read the
  channel, so they are not locked out.
- **Administrators are always exempt** (`ADMIN_IDS`). The gate is for customers,
  not operators.
- **`SUBSCRIPTION_CACHE_SECONDS=0` (default) re-checks on every interaction.**
  A positive value caches only *confirmed members* for that many seconds;
  failures are never cached.

## Configuration

Add these keys to `.env` (see `.env.example`):

```
# Channel every user must join. A public @username or a numeric channel id
# (e.g. -1001234567890).
SUBSCRIPTION_CHANNEL=@gamas_AI

# true = verify membership on every protected interaction; false disables the
# gate (administration/testing only).
SUBSCRIPTION_REQUIRED=true

# Reuse a confirmed membership for N seconds. 0 = verify on every interaction.
SUBSCRIPTION_CACHE_SECONDS=0

# Timeout of each getChatMember call, in seconds.
SUBSCRIPTION_TIMEOUT_SECONDS=20

# Telegram Bot API base URL; override only behind a proxy.
TELEGRAM_API_BASE_URL=https://api.telegram.org
```

The bot token used for the check is the existing `TELEGRAM_BOT_TOKEN`.

## Setup on Telegram

1. In the channel `@gamas_AI`, open **Administrators** and add the bot
   (`@userbot` / the bot username), granting it the standard administrator
   rights. `getChatMember` is the only right the gate needs, but a normal
   administrator role is required.
2. Confirm the bot can resolve itself in the channel: the startup self-check
   runs `getChatMember(channel, bot_id)` and logs whether the bot is an
   administrator. It never blocks startup; failures are logged and alerted to
   the administrators.

## cPanel deployment checklist

Run these in **cPanel Terminal**.

1. **Add the env keys** to the project `.env` (in the project directory, outside
   `public_html`):

   ```bash
   cat >> .env <<'EOF'
   SUBSCRIPTION_CHANNEL=@gamas_AI
   SUBSCRIPTION_REQUIRED=true
   SUBSCRIPTION_CACHE_SECONDS=0
   SUBSCRIPTION_TIMEOUT_SECONDS=20
   TELEGRAM_API_BASE_URL=https://api.telegram.org
   EOF
   ```

   If the file already contains these keys, edit them instead of duplicating.

2. **Run the schema migration.** It is idempotent and only adds two
   observability columns to `users`:

   ```bash
   python manage.py migrate
   ```

   (Use the project's migration entry point; `migrations/v3/016_subscription_enforcement.sql`
   is applied automatically by `Database.connect()` on startup.)

3. **Make the bot an administrator of `@gamas_AI`** (see *Setup on Telegram*).

4. **Start the bot** and watch the log for the self-check line:

   ```bash
   python -m gamas_bot
   ```

   Expected: `subscription self-check passed channel=@gamas_AI`. If instead you
   see `subscription self-check failed`, the bot is not an administrator of the
   channel.

5. **Test with a second account** that is *not* a member of the channel:

   - Send `/start` to the bot → the reply is the join prompt with the channel
     link and the **🔍 بررسی عضویت** (check membership) button.
   - Join the channel, then press **🔍 بررسی عضویت** → the bot replies
     "✅ عضویت شما در کانال گاماس تأیید شد" and shows the main menu.
   - Send another `/start` without joining → the join prompt is shown again.

6. **Verify the admin path is unaffected**: an `ADMIN_IDS` user gets the normal
   admin menu even if they are not a channel member.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Every user gets the "check unavailable" screen | The bot is not an administrator of the channel, or the channel reference is wrong. Make the bot an administrator and check `SUBSCRIPTION_CHANNEL`. |
| `subscription self-check failed` in the log | The bot cannot confirm its own administrator status. Same fix as above. |
| A user who just joined is still blocked | Membership is verified per interaction; press **🔍 بررسی عضویت** (or send `/start` again) to force a fresh check. If `SUBSCRIPTION_CACHE_SECONDS` is positive and the user was previously confirmed as a member, the cached value may be reused — lower it to `0` for per-interaction checks. |
| `429 Too Many Requests` in the log | Telegram rate limiting. The bot waits `retry_after` (capped at 60s) and retries once; reduce request frequency or raise `SUBSCRIPTION_CACHE_SECONDS`. |
| A user who left the channel can still use the bot | They should be checked as `left` on their next interaction. Confirm the bot is an administrator and that the log shows a denied check for them. |

## What is not enforced

- The bot does **not** try to add users to the channel. Telegram forbids it;
  the bot only reports whether the user is a member.
- The gate is not a payment or billing gate. Billing, bans and admin
  authorization are unchanged and still apply on top of this gate.
- Public channels only. `getChatMember` needs the bot to be an administrator;
  private channels the bot cannot resolve will surface as the "unavailable"
  screen rather than a denial.
