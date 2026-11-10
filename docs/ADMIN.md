# Admin, support and membership

Everything is administered inside Telegram: there is no web dashboard. Set
numeric IDs in `ADMIN_IDS`, then open the bot's private chat and use
**🛠 پنل مدیریت**. Every action is authorized on the server; a stale keyboard
grants nothing.

## Daily routine

1. **Dashboard** — queue, delivered/failed work, pending receipts.
2. **Payments** — inspect the frozen order and the receipt, verify the transfer
   outside the bot, then approve explicitly or reject with a reason.
3. **Users** — search by numeric ID or username, inspect status and balance.
4. **System** — provider errors, health, audit history.
5. **Support** — reply, close or reopen tickets.

Grants, unlimited status, bans and gift resets are audited and rate-limited, and
cannot target yourself or another admin.

## Commands

| Command | Effect |
| --- | --- |
| `/grant <user_id> <hours>` | Add credit |
| `/unlimited <user_id> on <reason>` | Unlimited access |
| `/unlimited <user_id> off` | Remove unlimited access |
| `/ban <user_id>` / `/unban <user_id>` | Block/restore **new** work (running jobs finish) |

Double-check IDs and amounts; prefer the panel's confirmations over guessed
callbacks. Unlimited releases holds; it does not rewrite ledger history.

## Payments review

1. A user picks a plan; the order's price, duration and validity are frozen.
2. The receipt upload moves the order `awaiting_payment → pending_review`. **No
   credit is added.**
3. You verify the transfer, then approve or reject with a reason. Approval,
   credit and audit commit in one transaction; a failed notification never
   repeats the credit. Final decisions are immutable.
4. Receipt reuse is flagged in the admin alert by a SHA-256 fingerprint of the
   normalized image. It does not block the upload by itself.

Users may cancel before uploading a receipt; one open order per user. Reviewed
receipt images expire after `RECEIPT_RETENTION_DAYS=90`; pending-review receipts
do not expire automatically.

## Support tickets

Users open **🎧 پشتیبانی**, choose a category and describe the issue. Ticket IDs
look like `T-000001`. States: `open`, `awaiting_admin`, `awaiting_user`,
`closed`.

- Messages are stored before notifications, so a Telegram delivery failure never
  deletes a ticket.
- Users can read only their own history; replies keep their order; timestamps are
  UTC.
- Admission limits: `SUPPORT_MAX_OPEN_TICKETS=3` and
  `SUPPORT_TICKET_COOLDOWN_SECONDS=600` (set `0` to disable) are enforced
  atomically.
- Payment decisions belong in the payment panel — a support reply never
  activates credit. Never ask a user for `.env`, keys, sessions or receipt text.

## Channel membership gate

On by default: `SUBSCRIPTION_REQUIRED=true`, channel `@gamas_AI`.

1. Make the bot an **administrator** of the channel; `getChatMember` needs that
   to resolve membership reliably. A private channel needs its numeric ID
   (`-100…`) and a manual link test.
2. Set the existing keys instead of appending duplicates:

```dotenv
SUBSCRIPTION_CHANNEL=@gamas_AI
SUBSCRIPTION_REQUIRED=true
SUBSCRIPTION_CACHE_SECONDS=300
SUBSCRIPTION_TIMEOUT_SECONDS=20
```

3. Restart, then test with a **non-admin** account.

Behavior to remember:

- `creator`, `administrator`, `member` and `restricted` count as members;
  `left`, `kicked` and "not a participant" block access.
- The gate is deferred: `/start`, help, wallet, purchases, support, library
  browsing and receipt upload stay open, so a non-member can pay and ask for
  help. Files, booklets, quizzes and flashcard exports are gated when work would
  start.
- A blocked file is remembered for up to seven days and resumes automatically
  after **✅ بررسی عضویت** succeeds — the user does not resend it.
- `ADMIN_IDS` are exempt. `SUBSCRIPTION_REQUIRED=false` disables the gate for
  testing.
- A confirmed membership is cached for `SUBSCRIPTION_CACHE_SECONDS`; failures are
  never cached. A bounded sweep re-checks ~20 recently active users every six
  hours.
- Telegram infrastructure errors **fail open** and alert admins (throttled);
  confirmed non-membership always blocks. Fix "bot is not an admin" warnings
  rather than relying on that fallback.

## Broadcasts

Administrator preview and confirmation are required, and one broadcast runs at a
time. Banned users and recipients with three permanent Telegram failures are
skipped; a successful send resets the counter. A stopped run can be resumed by
the same admin after re-entering the same text (verified by SHA-256). Cursors are
checkpointed per batch, so a crash can repeat delivery within the last
uncheckpointed batch — Telegram has no exactly-once send. The message text itself
is not stored.

## Reading reports

- Windows use **UTC**, not your local timezone.
- "Delivered" = `done` + `partial`. Partial means a transcript was delivered but
  the booklet was not; it is not a pre-delivery failure.
- Request windows use submission time; `started_at` is worker pickup.
- Missing samples mean "no data", not "zero failures".
- Reports are read-only and must never mutate billing state.

Telemetry is not a provider invoice. Investigate with the `GMS-#####` job
reference and redacted logs, and never broadcast private content.
