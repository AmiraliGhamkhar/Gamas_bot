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

## The notes provider says "no suitable response"

Everything failed, or nothing was eligible. Open **AI → 📈 نمای مسیر و بودجه**:
every provider that was skipped is listed with the reason.

| Reason | Meaning | What to do |
| --- | --- | --- |
| `account_entitlement_unverified` | The provider publishes no free tier, so Gamas will not assert one (Groq today) | Attest the account under **AI → provider → 🧾 تأیید استحقاق حساب** if you have verified the account is no-charge, or remove it from the route |
| `model free eligibility unknown (FREE_ONLY)` | The model's free status is not documented | Sync the catalog; if the account really includes it, attest the key `free`. Gamas will not spend quota to find out |
| `not_free_eligible` / `billing_blocked` | The provider is paid-only or trial-only | Remove it from the free route, or enable paid fallback deliberately |
| `terms_of_use_blocked` | Provider terms prohibit this use (Z.AI today) | Do not route; the price or a key attestation does not override terms |
| `quota_exhausted` | The local daily ledger says the free budget is spent | Wait for the UTC reset, or add another free provider to the route |
| `no eligible credential` | No key is attested for this billing lane | Attest the key `free` (or `paid` on a paid leg) under **AI → provider → کلیدها** |

Run `python -m scripts.validate_provider_platform` to confirm the routing,
failover and secret-hygiene behaviour offline before changing production
configuration.

## Cloudflare fails immediately even though it is enabled

Check the estimated Neuron figure under **AI → provider → Cloudflare**. Workers
AI meters Neurons, not tokens, and does not return a neuron count, so Gamas
guards the daily inclusion from a pessimistic estimate. If the estimate has
reached the configured budget, `AI_FREE_ONLY` stops routing there and uses the
next free provider. Lower `neuron_budget_daily` only if you have measured your
account's real allocation.

## Where to look first

| Symptom | Start here |
| --- | --- |
| No reply at all | `instance_lock`, logs, `--check` |
| Job fails | tracking reference in the log, provider health panel |
| Wrong billing | `📜 گزارش مدیر` ledger, `docs/BILLING.md` |
| Document layout | `docs/DOCX.md`, `python -m scripts.validate_docx --render` |
| Deployment | `docs/DEPLOY_CPANEL.md`, `scripts/cpanel_preflight.py` |
