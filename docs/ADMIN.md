# Admin panel

Set numeric IDs in `ADMIN_IDS`; open **🛠 پنل مدیریت** in the bot's private chat.
There is no web dashboard. Every admin action is authorized; stale keyboards
do not grant access.

## Daily workflow

1. Check the dashboard: queue, delivered/failed work and pending receipts.
2. Open **Payments**, inspect the receipt and frozen order. Verify the transfer
   outside the bot, then approve explicitly or reject with a reason.
3. Open **Users** to search by numeric ID/username and inspect status.
4. Use **System** for provider errors, health and audit history.
5. Handle tickets in support management: [Support](SUPPORT.md).

Changes are audited/cooldown-limited. Protected actions cannot target yourself or
another admin. Gift reset restores the quiz and AI-deck gifts, not ledger history.

## Message commands

| Command | Purpose |
| --- | --- |
| `/grant <user_id> <hours>` | Add credit |
| `/unlimited <user_id> on <reason>` | Enable unlimited |
| `/unlimited <user_id> off` | Disable unlimited |
| `/ban <user_id>` / `/unban <user_id>` | Block/restore new work |

Double-check IDs and amounts. Prefer panel details/confirmations to guessed callbacks.

## Reading statistics

- Reporting windows use **UTC**, not the operator's local timezone.
- Delivered means `done` + `partial`. Partial delivered a transcript but not a
  complete booklet; it is not a pre-delivery failure.
- Request windows use submission creation time; `started_at` records worker pickup.
- Missing samples are unavailable, not proof of instant work or zero failures.
- Provider history uses the recorded STT engine. Text/receipt retention does not
  erase submissions, payments or ledger totals.
- Reports are read-only and must not mutate billing state.

Telemetry is not a provider invoice. Investigate with job references and redacted
logs. Never broadcast private content or keys.
[Billing](BILLING.md) · [Host operations](OPERATIONS.md)
