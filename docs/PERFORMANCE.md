# Performance

The shipped defaults come from measurements on this code; this page keeps the facts an operator
needs and drops the old planning document.

## What dominates a job

| Stage | Practical cost | Lever |
| --- | --- | --- |
| Telegram download | Network-bound | `TELEGRAM_DOWNLOAD_CONNECTIONS` (measure first; default 1) |
| Audio preparation | Encoder CPU | `AUDIO_FORMAT_POLICY=auto` |
| Speech-to-text | Provider speed | `STT_PROVIDERS` order and model |
| Note generation | Provider output speed | `NOTE_PROVIDERS`, `NOTE_CONCURRENCY`, `NOTE_*` guards |

Measured on a 2-vCPU host with `python -m scripts.perf_bench audio`:

- WAV 16 kHz mono encodes at ~824× real time but is ~115 MB per audio hour.
- Opus 32 kbit/s (`application=audio`, `compression_level=0`) encodes at ~355× and is ~13.5 MB per
  hour — about 3× faster and 8× smaller than WAV.
- The previous Opus settings (`voip`, `compression_level=5`) encoded at ~106×, the slowest tested
  combination.

`AUDIO_FORMAT_POLICY=auto` therefore compares `duration / encode_speed + bytes / measured_rate`
for the job's own download rate. A PCM upload only wins on a very fast link.

## Recommended defaults

These are already shipped in `.env.example`:

```dotenv
STT_PROVIDERS=speechmatics,deepgram,openai_compatible
STT_FALLBACK_ENABLED=true
SPEECHMATICS_MAX_IN_FLIGHT=2
DEEPGRAM_MAX_IN_FLIGHT=4
LLM_PRIMARY_MAX_IN_FLIGHT=4
LLM_FALLBACK_MAX_IN_FLIGHT=4
NOTE_CONCURRENCY=6
NOTE_TTFB_TIMEOUT_SECONDS=25
NOTE_HEDGE_AFTER_SECONDS=60
NOTE_STRUCTURED_OUTPUT=1
NOTE_PROGRESSIVE_BOOKLET=1
NOTE_OPENROUTER_THROUGHPUT=1
```

- Speechmatics runs first up to `SPEECHMATICS_MAX_IN_FLIGHT`, and excess concurrent jobs overflow
  immediately (`0 ms` wait) to Deepgram Nova-3 (`DEEPGRAM_MAX_IN_FLIGHT`) without waiting or triggering `429`s.
- Primary LLM routes likewise overflow immediately (`0 ms` wait) to secondary `NOTE_PROVIDERS` routes
  when `LLM_PRIMARY_MAX_IN_FLIGHT` is full or when upstream returns `429`/quota errors.
- Benchmark multi-user concurrency & failover directly offline with:
  `python -m scripts.concurrency_bench`
- The first-byte guard abandons a silent route after 25 s instead of waiting out the deadline.
- Hedging starts a second provider after 60 s; it can spend extra quota.
- Progressive delivery sends the audited merge while the editorial compile runs. It is skipped
  when the compile would not run (otherwise the two files would be identical).

## Re-measure on your host

```bash
python -m scripts.perf_bench audio --minutes 10
python -m scripts.perf_bench frames --minutes 10
python -m scripts.perf_bench cpu --chars 108000
python -m scripts.perf_bench json
python -m scripts.perf_bench prompts --chars 108000
```

The script synthesizes its own audio and Persian text, so it runs offline. Treat provider speed
figures as dependent on your keys, region and plan.

## Rules of thumb

- Never cut `NOTE_MAX_OUTPUT_TOKENS` below the observed p95 of real output; a truncated booklet is
  worse than a slower one.
- Keep `NOTE_REQUEST_TIMEOUT_SECONDS` longer than the time the output budget needs on the slowest
  configured model.
- Lower `NOTE_CONCURRENCY` for low-RPM providers (free Groq, shared gateways).
- On cPanel, keep `MEDIA_WORKER_CONCURRENCY=1` and prefer a small queue over a large one.
