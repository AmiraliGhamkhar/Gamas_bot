# Support tickets

Users open support requests from the main menu («🎧 پشتیبانی»). Every request is a **ticket** with a
human-readable ID (`T-000001`), one of four categories, and its own message history. The ticket and
its initial message are inserted atomically, so a failed first-message write cannot leave an empty
ticket. Administrators work the tickets from «🎧 مدیریت پشتیبانی» in the admin panel.

## Statuses and transitions

All transitions are defined once, in `Database.SUPPORT_TRANSITIONS` (`gamas_bot/db.py`). No handler
writes a status directly; an event that the current status does not allow raises `TicketStateError`.

| Event | From | To |
| --- | --- | --- |
| create | — | `open` |
| user message | `open`, `awaiting_admin`, `awaiting_user` | `awaiting_admin` |
| admin message | `open`, `awaiting_admin`, `awaiting_user` | `awaiting_user` |
| close (user or admin) | any open status | `closed` |
| reopen (user or admin) | `closed` | `open` |

A closed ticket accepts no messages until it is reopened, so nothing is written into a conversation
that no one is watching. Reopening keeps all earlier messages.

## Access rules

- A user can list, read, reply to, close and reopen only tickets they own. Ownership is checked in
  the database layer (`add_ticket_message`, `close_ticket`, `reopen_ticket`) and again in the bot.
- Administrators are checked with the existing `ADMIN_IDS` mechanism on every `adm:` callback.
- An admin reply is routed by the ticket ID carried by the admin's own verified callback, never by
  a username or a free-text lookup.

## Notifications

- Administrators are notified when a ticket is created, when a user replies, and when a user closes
  or reopens a ticket.
- A user is notified when an administrator replies or closes/reopens the ticket.
- The message is written to the database **before** any notification is sent. A failed or blocked
  notification is logged (ticket ID only, never message text) and does not undo the stored message.

## Data

Tables (migration `005_payments_and_support.sql`): `support_tickets`, `support_messages`,
`support_ticket_seq`. Timestamps are UTC ISO-8601 strings, as in the rest of the schema.

## History and limitations

- History pages preserve message order and contain complete text. When a page exceeds Telegram's
  message limit, the bot sends multiple bounded messages and puts navigation on the last one;
  stored messages are never cut to fit a screen.
- Attachments in support messages are not stored; only text is accepted.
- Tickets are not purged automatically.
