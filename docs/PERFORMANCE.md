# Performance, concurrency, and capacity

This page records the resource assumptions, failover behavior, and reproducible measurements. The
concurrency benchmark is fully offline: Telegram, Speechmatics, Deepgram, NaraRouter, and OpenRouter
are simulated. Its provider latencies and 429s are test inputs, not live-service measurements.

## Resource ceilings

The shipped profile is deliberately modest for shared cPanel hosting:

| Resource | Default | Guard |
| --- | ---: | --- |
| Active jobs / queued jobs | 3 / 8 | Fixed workers, bounded pending queue, fair per-user dispatch |
| Active jobs per user | 2 | Admission check before enqueue |
| Telegram downloads / uploads | 2 / 2 | Independent semaphores and stage deadlines |
| Media worker concurrency | 1 | Heavy extraction and conversion do not fan out across jobs |
| Ranges per Telegram download | 1 | `TELEGRAM_DOWNLOAD_CONNECTIONS`; increase only after measuring the host |
| General upload size / duration | 128 MiB / 2 hours | Metadata checks plus post-download and media-probe checks |
| Minimum free temporary space | 64 MiB | Headroom check reserves room for working copies before transfer |
| Presentation clips / unpacked / temporary | 50 / 128 MiB / 256 MiB | Extraction and media caps are enforced before expansion |
| Speechmatics / Deepgram slots | 2 / 4 | Immediate overflow to configured Deepgram slots when Speechmatics is full |
| Note map concurrency | 3 | Bounded map requests per job |
| LLM primary / fallback slots | 4 / 4 | Local slot reservation; saturation overflows to an available secondary route |
| STT / LLM response bodies | 64 MiB / 16 MiB | Incremental reads stop at the configured limit plus one byte |

Generated Telegram downloads and uploads use asynchronous file-like adapters backed by the shared
thread pool; STT file uploads stream from disk in bounded chunks. Temporary files, extraction, media
subprocesses, provider attempts, and Telegram transfers all have explicit limits. A cPanel plan can
still impose tighter disk, CPU, process, network, or runtime ceilings than the application can
observe; lower `MAX_FILE_SIZE` and presentation limits to fit the actual account.

## Provider routing policy

### Speech-to-text

`STT_PROVIDERS` is an ordered chain and defaults to
`speechmatics,deepgram,openai_compatible`. Speechmatics is primary. Providers lacking their own
credentials are skipped; fallback credentials are optional and do not prevent startup. Keep
`STT_FALLBACK_ENABLED=true` (default) to use the configured chain.

- If Speechmatics has a free local slot, the job uses it. If its slots are full and a configured,
  eligible Deepgram slot is available, the capacity pool reserves Deepgram immediately without
  waiting for the primary. If every eligible provider is full, the chain waits at most
  `STT_SLOT_WAIT_TIMEOUT_SECONDS` (15 seconds by default), then returns a bounded capacity error.
- A provider is skipped if it is cooling down or the prepared audio exceeds its upload ceiling.
- `HTTP 429`, `408`, `425`, `5xx`, network errors, and per-provider timeouts fail over without
  repeating the upload. `Retry-After` is bounded; transient failures use a 60-second local
  cooldown. Authentication failures cool down for 900 seconds; quota failures for 300 seconds.
- Empty/corrupt input fails without sending the same bad audio to every provider. Other definitive
  provider request rejections are recorded and the next configured route is tried.
- The submit `POST` is attempted once per provider. Only idempotent Speechmatics status/transcript
  `GET`s get up to two bounded retries. `STT_JOB_TIMEOUT` (6 hours) is the whole chain deadline,
  `STT_PROVIDER_TIMEOUT_SECONDS` (30 minutes) caps one attempt, and
  `STT_TRANSFER_TIMEOUT_SECONDS` (10 minutes) caps one network transfer.
- Leases are released in `finally` on success, provider errors, timeouts, and cancellation. A
  Speechmatics job cancellation is best-effort and itself capped at five seconds.

### Note generation (LLM)

`NOTE_PROVIDERS` controls route order; providers without their own credentials are skipped. If the
primary route is saturated, a configured secondary with capacity is selected immediately. The
bounded all-full wait is `LLM_SLOT_WAIT_TIMEOUT_SECONDS` (30 seconds by default).

- `HTTP 429` and qualifying provider-limit responses fail over without sleeping or replaying the
  primary request; the route enters a short local cooldown. Authentication/quota failures also
  cool down and proceed to the next eligible provider.
- Transient network/server errors receive at most two retries on that route. Request timeouts
  normally fail over instead of repeating a potentially expensive completion; the last configured
  route can use its bounded retry path.
- For eligible OpenAI-style and Gemini routes, `NOTE_TTFB_TIMEOUT_SECONDS` (25 seconds; `0`
  disables it) streams the response and abandons a route that produces no first byte in time.
  Non-streaming JSON and SSE are both bounded at 16 MiB and parsed without first retaining an
  unbounded response body.
- If enabled, `NOTE_HEDGE_AFTER_SECONDS` (60 seconds by default) starts one secondary attempt only
  after the primary remains unfinished for that long. It can spend extra quota; both tasks are
  cancelled and joined on completion/caller cancellation, and their provider slots are released.

All limits are local to one bot process; they do not coordinate multiple cPanel accounts, servers,
or provider keys shared by unrelated applications.

## Reproducible measurements

Run with the project virtual environment and installed requirements:

```bash
python -m scripts.concurrency_bench cpanel
python -m scripts.concurrency_bench high_concurrency
python -m scripts.concurrency_bench burst
```

The benchmark uses six synthetic jobs for the full pipeline (and eight for the provider-only burst),
real local audio extraction/normalization and DOCX generation, a temporary WAL SQLite database, and
simulated provider limits. No external APIs or paid keys are used. Results from this checkout:

| Run | Jobs | Wall time | Throughput | Mean end-to-end | Python heap peak | RSS peak |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Before edits, cPanel profile | 6 | 4.6276 s | 1.297 jobs/s | 2.2759 s | 6.44 MiB | 112.25 MiB |
| After edits, cPanel sample A | 6 | 4.8252 s | 1.243 jobs/s | 2.3932 s | 6.00 MiB | 105.82 MiB |
| After edits, cPanel sample B (latest) | 6 | 4.6763 s | 1.283 jobs/s | 2.2888 s | 5.34 MiB | 111.07 MiB |
| After edits, one active worker | 6 | 13.9475 s | 0.430 jobs/s | 2.3246 s | 4.36 MiB | 101.52 MiB |
| After edits, stress profile (6 active) | 6 | 2.6502 s | 2.264 jobs/s | 2.5520 s | 9.77 MiB | 126.98 MiB |

The latest post-change cPanel sample measured mean download/extract/transcribe/note/upload stages of
`0.1188 / 0.3483 / 0.7526 / 0.9625 / 0.1036` seconds. The recorded before-edit run measured
`0.1193 / 0.3423 / 0.7520 / 0.9643 / 0.0952` seconds. In the latest simulated cPanel run, four
jobs used Speechmatics and two used Deepgram; Nara returned two injected HTTP 429s, after which
OpenRouter served both fallbacks. The two post-change cPanel samples varied from 4.6763 to 4.8252
seconds (mean end-to-end 2.2888 to 2.3932 seconds); sample B was about 0.6% slower end-to-end than
the recorded baseline. This small offline sample is consistent with rough parity, not statistically
conclusive evidence of a throughput gain or regression. The cPanel profile delivered about 2.98x
the throughput of the one-worker run, with similar mean end-to-end time because the jobs overlap
network stages.

The six-active-worker stress profile sustained 2.264 jobs/s with mean end-to-end latency of
2.5520 seconds; coordinated capacity used Speechmatics for two jobs and Deepgram for four, and Nara
for two while OpenRouter served four overflow jobs. No simulated 429 was needed in that profile.
The separate eight-job burst profile measured 15.524 jobs/s under 0.18–0.32 second simulated
provider latencies; those figures are not a production throughput forecast. RSS is the process
high-water mark reported by the OS; when multiple profiles are run in one Python process, later
profiles inherit earlier high-water peaks.

These are local synthetic measurements, not tests of a real cPanel account, Telegram transfer,
Speechmatics/Deepgram account, model-provider latency, or production traffic. No live-provider or
cPanel verification was performed.

## Tuning guidance

- On shared hosting, start with the defaults above, `MEDIA_WORKER_CONCURRENCY=1`, and
  `TELEGRAM_DOWNLOAD_CONNECTIONS=1`. Increase one limit at a time and repeat the benchmark on the
  target host; CPU, RAM, disk I/O, process quotas, and outbound bandwidth are host-controlled.
- Reduce `MAX_ACTIVE_JOBS`, `MAX_CONCURRENT_DOWNLOADS`, `MAX_CONCURRENT_UPLOADS`, or
  `NOTE_CONCURRENCY` if the plan has lower quotas. Keep the queue bounded rather than accepting
  work that cannot start promptly.
- Preserve enough free disk for simultaneous source and prepared files; do not disable the headroom
  check to make a too-small plan appear to fit.
- Keep `NOTE_REQUEST_TIMEOUT_SECONDS` long enough for the selected output budget. Lower
  `NOTE_MAX_OUTPUT_TOKENS` for slow or low-RPM routes rather than permitting unbounded retries.
