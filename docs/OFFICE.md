# Retired Free Office bot UI

The Free Office Telegram menu and job wiring were retired in v4. The bot no longer accepts bare
text or text-file uploads as requests to create Word, Excel, PowerPoint or PDF documents; the user
surface is reserved for lecture processing, the personal library, wallet, support and help.

The pure export utilities remain in `gamas_bot/office.py`, `office_ai.py` and `pdf_export.py` for
reuse by other code. Their package-level behavior is covered by `tests/test_office.py`; this page is
kept as a migration note, not as a user-facing feature guide.

`OFFICE_ENABLED` defaults to `false`. The legacy `OFFICE_*` settings remain readable for
configuration compatibility but are not wired to a Telegram screen or job lane.
