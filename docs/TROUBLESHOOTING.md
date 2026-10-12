# Troubleshooting

## The bot does not answer

1. `python -m gamas_bot --check` — configuration and dependency self-check.
2. Look for the startup line in the log (`Gamas Bot v3 started`).
3. If the log says the session or lock is unavailable, another process is
   already polling this bot account. Stop it; the advisory lock is intentional
   and a second instance exits with code `3`.
4. Network problems reaching Telegram (`FloodWait`, connection errors) are
   handled by Telethon; check outbound TCP 443.

## Every upload fails with a tracking reference

The reference (`GMS-000123`) is in the chat message and in the log. Search the
log for it (`grep GMS-000123`) — the surrounding lines are the cause. The three
shapes you will see:

* `❌ تبدیل گفتار به متن نشد: …` — the speech-to-text chain failed. Continue
  with [STT failures](#stt-failures) below.
* `❌ این فایل شاخهٔ صوتی ندارد؛ …` — the file (or the extracted track) carries
  no audio stream; the message is explicit and no credit is consumed.
* `❌ این فایل از سقف حجم ربات بزرگ‌تر است.` — larger than `MAX_FILE_SIZE`.

In every failure path the reserved credit is released exactly once
(`holds.released_at`), the submission is marked `failed` with the reason, and no
`.txt`/`.docx` is delivered. If the reserve exists without a release, that is a
bug worth reporting with the submission id.

## STT failures

Search the log for the submission id and read these lines:

```
STT failover chain providers=speechmatics,deepgram,openai_compatible bytes=… language=fa
STT provider speechmatics has no credentials; skipped
STT provider deepgram in cooldown for 812s more; skipped
STT provider openai_compatible skipped: file is 41000000 bytes, over its 25000000 byte ceiling
STT attempt provider=deepgram bytes=… model=nova-3 language=fa
STT done provider=deepgram bytes=… elapsed_seconds=12.4
STT exhausted every eligible provider (2 attempted/skipped): …
```

Classification a message falls into and what to do:

| Message fragment | Meaning | Action |
| --- | --- | --- |
| `هیچ سرویس تبدیل گفتاری پیکربندی نشده است` | no key for any provider in `STT_PROVIDERS` | set a key, or add the configured provider to `STT_PROVIDERS` |
| `اعتبارنامه را نپذیرفت (HTTP 401/403)` | the key is invalid or disabled | replace it; the provider is skipped for 15 minutes (`AUTH_COOLDOWN_SECONDS`) |
| `محدودیت نرخ درخواست را اعلام کرد (HTTP 429)` | rate limited | transient; up to 3 attempts, `Retry-After` honoured up to 60 s, then the next provider |
| `درخواست را رد کرد (HTTP 4xx)` | the provider refused the request — unsupported model, language or media | read the provider's own detail after the colon; for Persian check the model pairing below |
| `موقتاً در دسترس نیست (HTTP 5xx)` / `پس از چند تلاش در دسترس نبود` | provider or network outage | transient, then failover; check outbound TCP 443 and DNS |
| `سقف حجم …MB دارد` | the file is over that provider's direct-upload ceiling | expected; the file goes to the next provider |
| `در حال استراحت (…s)` | the provider is in cooldown | wait, or fix the credential that caused it |
| `پاسخ JSON معتبری برنگرداند` / `متن قابل استفاده‌ای تولید نکرد` | unusable or empty provider payload | provider-side problem or an incompatible gateway; the next provider runs |

### Persian check

* Deepgram needs `DEEPGRAM_MODEL=nova-3` (`nova-3-general` also works). Nova-2
  has no Persian language pack and rejects `language=fa`. `--check` warns about
  this pairing and the bot logs the warning at startup.
* Speechmatics `enhanced`/`standard` accept `fa` directly; `melia-1`/`oak-1`
  require `STT_LANGUAGE=multi` and reject `auto`.
* OpenAI-compatible gateways need a model that supports the language. Segment
  timings are requested with `response_format=verbose_json` and retried once
  with `json` when a gateway explicitly refuses that format.

A `--check` run is the fastest way to see all of this at once; it never contacts
a provider and is safe to run while the bot is serving traffic.

## Credit was not returned after a failure

It should be, in every failure and cancellation path, and there is a test per
path. Check the ledger for a release event for that submission:
`release_hold` sets `holds.released_at` and is a no-op on a second call, so a
double release cannot double-refund. If a reserve has neither a release nor a
debit, report it with the submission id.

## Media processing fails after a dependency change

`python -m gamas_bot --check` runs `python -m gamas_bot.media_worker check`, so a
broken `av`/`ppt2pptx` install is reported there instead of surfacing later as a
mysterious media error. `ffmpeg`, `ffprobe` and `soffice` are **not** used:
media runs in a dedicated worker process through PyAV.

## The booklet arrives, but the table of contents has no page numbers

There is no renderer in v3: the table of contents links to the headings and the
page-number column stays empty. Numbers are never guessed.

## Word offers to "repair" the document

That would be a real bug — the OOXML element order is maintained explicitly.
Report it with the generated file; OOXML ordering rules (CT_PPr, CT_RPr,
CT_SectPr, CT_TblPr) are the first thing to check.

## Where to look first

| Symptom | Start here |
| --- | --- |
| No reply at all | `instance_lock`, log, `python -m gamas_bot --check` |
| Job fails | tracking reference in the log, `docs/STT_PROVIDERS.md` |
| Wrong billing | ledger report in the admin panel, `docs/BILLING.md` |
| Document layout | `docs/DOCX.md` |
| Deployment | `docs/DEPLOY_CPANEL.md`, `docs/DEPLOY_FA.md` |
