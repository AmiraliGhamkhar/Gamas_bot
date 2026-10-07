# Troubleshooting

## The bot does not answer

1. `python -m gamas_bot --check` — configuration and dependency self-check.
2. Look for `Persian study assistant is online` in the log.
3. If the log says the session or lock is unavailable, another process is
   already polling this bot account. Stop it; the advisory lock is intentional.
4. `TELEGRAM_PROXY` must be a full URL (`socks5://host:port`); a partial value
   is rejected at startup.

## Every upload fails with a tracking reference

The reference (`GMS-000123`) is in the message and the log. Search the log for
it; the surrounding exception is the cause. Common cases:

* all configured STT engines failed → check `🩺 وضعیت سرویس‌ها`; a key can be in
  cooldown (429) or quarantine (401/403).
* the file has no audio stream → the error is explicit and user-visible.
* the file is larger than the direct-upload limit of every configured engine.

## The booklet arrives, but the table of contents has no page numbers

Expected when no renderer is installed and `DOCX_TOC_PAGE_NUMBERS=auto`: the
topic list keeps its internal links and the number column stays empty. Install
LibreOffice (`soffice`) and `pypdf` for exact numbers, or set `required` if a
deployment must refuse to deliver without them. Numbers are never guessed.

## Word offers to "repair" the document

That would be a real bug — the OOXML element order is maintained explicitly.
Report it with the generated file; OOXML ordering rules (CT_PPr, CT_RPr,
CT_SectPr, CT_TblPr) are the first thing to check.

## Persian text renders as disconnected letters in a *rendered image*

That only affects the offline inspection tool (`scripts/render_docx_pages.py`),
which paints glyphs itself and therefore needs `arabic-reshaper` and
`python-bidi`. In Word the shaping is done by the font and the document.

## Credit was not returned after a failure

It should be, in every failure and cancellation path, and there is a test per
path. Check the `📜 گزارش مدیر` ledger for a `release` event for that
submission. If the reserve exists without a matching release or consume, that is
a bug worth reporting with the submission id.

## Media processing fails after a dependency change

`python -m gamas_bot --check` verifies that the media worker imports and runs.
The worker is a child process that uses PyAV; a broken `av` wheel is reported
there rather than surfacing later as a mysterious media error.

## Where to look first

| Symptom | Start here |
| --- | --- |
| No reply at all | `instance_lock`, logs, `--check` |
| Job fails | tracking reference in the log, provider health panel |
| Wrong billing | `📜 گزارش مدیر` ledger, `docs/BILLING.md` |
| Document layout | `docs/DOCX.md`, `python -m scripts.validate_docx --render` |
| Deployment | `docs/DEPLOY_CPANEL.md`, `scripts/cpanel_preflight.py` |
