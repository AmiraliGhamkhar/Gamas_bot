# Support tickets

## Users

1. Open **🎧 پشتیبانی**, choose a category and describe the issue.
2. Include a job/order reference when relevant. Ticket IDs look like `T-000001`.
3. Keep replies in the ticket history; close when resolved or reopen if needed.

Do not send keys, `.env`, sessions or sensitive lecture/receipt content as diagnostics.
Payment receipts belong in the purchase workflow, not support replies.

## Admins

Open support management, filter history, select the ticket and reply. Use the
controls to close/reopen. Payment decisions belong in the payment panel;
a support reply never activates credit.

States: `open`, `awaiting_admin`, `awaiting_user`, `closed`. Messages are stored
before notifications; Telegram delivery failure does not delete a ticket/reply.
Users can access only their own history. Creation and first message are atomic;
timestamps use UTC and messages retain order.

Use [Troubleshooting](TROUBLESHOOTING.md) and share redacted metadata only.
Maintainers: preserve `Database.SUPPORT_TRANSITIONS` in the database layer.
