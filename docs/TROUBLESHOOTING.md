# Troubleshooting

## The bot does not answer

1. `python -m gamas_bot --check` — configuration and dependency self-check.
2. Look for the startup line in the log.
3. If the log says the session or lock is unavailable, another process is already
   polling this bot account. Stop it — the advisory lock is intentional and a
   second instance exits with code `3`.
4. `FloodWait` and connection errors are handled by Telethon; check outbound
   TCP 443.

## Every upload fails with a tracking reference

The reference (`GMS-000123`) is in the chat message and in the log. `grep` for
it — the surrounding lines are the cause.

| Message | Meaning |
| --- | --- |
| `❌ تبدیل گفتار به متن نشد: …` | the STT chain failed — see [STT failures](#stt-failures) |
| `❌ این فایل شاخهٔ صوتی ندارد؛ …` | no audio stream; no credit consumed |
| `❌ این فایل از سقف حجم ربات بزرگ‌تر است.` | larger than `MAX_FILE_SIZE` |

In every failure path the reserved credit is released exactly once
(`holds.released_at`) and the submission is marked `failed` with the reason. A
reserve with neither a release nor a debit is a bug — report it with the
submission id.

## Only the raw transcript arrives

This is a **partial success**, not a failure: the raw `.txt` is delivered and
persisted before note generation starts.

* `📝 متن خام — GMS-000123` is the normal raw delivery; `📖 جزوهٔ Word — …`
  follows it. If the `.txt` upload is refused, the same text goes out as
  paginated chat messages and the job still completes — look for
  `raw .txt delivery failed; falling back to text`.
* `ℹ️ این بار نتوانستم متن را به جزوهٔ ساختارمند دربیاورم` means every note
  provider failed. The log line `note provider failed; trying the next one`
  names each provider, its error class and the reason. A missing key shows up as
  a skip, not as a request.

Two note failures have specific fixes:

| Log line | Cause | Fix |
| --- | --- | --- |
| `LLMOutputBudgetError` (`به سقف توکن خروجی رسید`) | the answer was cut off by the output budget. Expected on a reasoning model, which spends part of the same budget thinking | raise `NOTE_MAX_OUTPUT_TOKENS` |
| `LLMContextOverflowError` after several `splitting into N part(s)` | the input was too large even after the bounded halving | shorten the recording, or raise `NOTE_CONTEXT_TOKENS` if the model's window really is larger |

**A timeout is not a context problem.** `LLM transient failure provider=… (attempt
1/3; TimeoutError)` means the provider did not answer inside the request
deadline — the request was *not* rejected for being too large (that is
`LLMContextOverflowError`, and it never appears as a `TimeoutError`). The
deadline is filled by the **output** budget, not by the input: the model has to
emit `NOTE_MAX_OUTPUT_TOKENS` tokens, and on a free or shared-pool gateway that
takes minutes. The progress bar then sits at one percentage for a long time,
which is the symptom users report.

| Log line | Cause | Fix |
| --- | --- | --- |
| `LLM request timed out provider=…` / `TimeoutError` | the provider needed longer than `NOTE_REQUEST_TIMEOUT_SECONDS` to produce the answer | raise `NOTE_REQUEST_TIMEOUT_SECONDS`, lower `NOTE_MAX_OUTPUT_TOKENS`, or lower `NOTE_REASONING_EFFORT` (thinking tokens are spent out of the same output budget) |
| `HTTP 429 … UPSTREAM_PROVIDER_SHARED_POOL` | the fallback provider is a free OpenRouter model and its shared pool is rate-limited — the failover has nowhere to go | use a non-free fallback model, or add another provider (for example `gemini`) to `NOTE_PROVIDERS` |

The chain does not repeat an identical request after a timeout: a provider that
missed the deadline is left behind and the next provider in the chain is tried
at once. A timeout on the *last* provider in the chain is still retried within
its bounded budget, so a single-provider deployment keeps its resilience.

The startup self-check (`--check`) warns when the configured output budget
cannot plausibly be produced inside the configured deadline
("the note request will time out on a slow gateway").

## STT failures

Search the log for the submission id:

```
STT failover chain providers=speechmatics,deepgram,openai_compatible bytes=… language=fa
STT provider speechmatics has no credentials; skipped
STT provider deepgram in cooldown for 812s more; skipped
STT attempt provider=deepgram bytes=… model=nova-3 language=fa
STT done provider=deepgram bytes=… elapsed_seconds=12.4
STT exhausted every eligible provider (2 attempted/skipped): …
```

| Message fragment | Meaning | Action |
| --- | --- | --- |
| `هیچ سرویس تبدیل گفتاری پیکربندی نشده است` | no key for any provider in `STT_PROVIDERS` | set a key, or list the configured provider |
| `اعتبارنامه را نپذیرفت (HTTP 401/403)` | invalid or disabled key | replace it; the provider cools down 15 minutes |
| `محدودیت نرخ درخواست را اعلام کرد (HTTP 429)` | rate limited | transient: 3 attempts, `Retry-After` honoured to 60 s, then failover |
| `درخواست را رد کرد (HTTP 4xx)` | unsupported model, language or media | read the detail after the colon; check the pairing below |
| `موقتاً در دسترس نیست` | provider or network outage | transient, then failover; check TCP 443 and DNS |
| `سقف حجم …MB دارد` | over that provider's upload ceiling | expected; the file goes to the next provider |
| `در حال استراحت (…s)` | cooldown | wait, or fix the credential that caused it |
| `متن قابل استفاده‌ای تولید نکرد` | unusable or empty payload | provider-side; the next provider runs |

### Persian check

* **Deepgram** needs `DEEPGRAM_MODEL=nova-3` (`nova-3-general` also works).
  Nova-2 has no Persian pack and rejects `language=fa`.
* **Speechmatics** `enhanced`/`standard` accept `fa` directly; `melia-1`/`oak-1`
  always use the multilingual pack — `STT_LANGUAGE=multi` is the clearest
  setting, and `auto` is mapped to `multi` for those models rather than
  producing a configuration error.
* **OpenAI-compatible** gateways need a model that supports the language.
  `verbose_json` is retried once as `json` when a gateway refuses it.

`--check` reports all of this at once, never contacts a provider, and is safe to
run while the bot is serving traffic.

## Media fails after a dependency change

`python -m gamas_bot.media_worker check` reports a broken `av` or `ppt2pptx`
install directly. `ffmpeg`, `ffprobe` and `soffice` are **not** used.

## The topic list has no page numbers

That is the design: v3 has no layout renderer, the topic list links to the
headings, and a number that was not measured is never printed.

## Word offers to "repair" the document

That is a real bug — OOXML element order is maintained explicitly. Report it with
the generated file; `CT_PPr`, `CT_RPr`, `CT_SectPr` and `CT_TblPr` ordering are
the first things to check.

## Where to look first

| Symptom | Start here |
| --- | --- |
| No reply at all | `--check`, the log, `instance_lock` |
| Job fails | tracking reference in the log, [STT_PROVIDERS.md](STT_PROVIDERS.md) |
| Only raw text arrives | `note provider failed` lines, [PROVIDERS.md](PROVIDERS.md) |
| Wrong billing | the ledger in the admin panel, [BILLING.md](BILLING.md) |
| Document layout | [DOCX.md](DOCX.md) |
| Deployment | [DEPLOY_CPANEL.md](DEPLOY_CPANEL.md), [DEPLOY_FA.md](DEPLOY_FA.md) |
