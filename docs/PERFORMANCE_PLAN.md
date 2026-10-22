# Performance plan: intake, transcode, transcription, notes

This is a measured plan, not a wish list. Every number below is either produced by
`python -m scripts.perf_bench` on the project's own code, or quoted from a published
benchmark/provider document (linked). Pipeline times are modelled from those measured
throughputs with the assumption stated next to each table, because provider latency
depends on your keys, region and plan.

**Headline:** for a 2-hour lecture the wall-clock today is ≈7 minutes; ≈65–70 % of it can
be removed without changing what the user receives (same transcript, same booklet), and
another ~5 % is available through optional quality/latency trades. The three levers that
matter are (1) Telegram has a single-connection download ceiling, (2) the STT *upload* is
10× bigger than it needs to be and the STT *job* is processed serially, (3) the note layer
sends a per-chunk-varying system prompt that defeats provider prefix caching.

---

## 1. What was measured, and how

`scripts/perf_bench.py` (added with this plan) synthesises its own audio and Persian text,
so it runs offline on the cPanel host before and after a change:

```bash
python -m scripts.perf_bench audio --minutes 10   # decode/encode cost + output size
python -m scripts.perf_bench cpu   --chars 108000 # note layer deterministic work
python -m scripts.perf_bench prompts --chars 108000
```

### 1.1 Media layer (10 min stereo AAC source, 9.7 MB)

| Output | Time | Speed | Size (10 min) | Size (2 h) |
| --- | ---: | ---: | ---: | ---: |
| WAV 16 kHz mono PCM (shipped default) | 1.04 s | 574× | 19.2 MB | **230 MB** |
| Opus 32k VoIP `compression_level=5` (shipped) | 5.85 s | 103× | 1.94 MB | **29 MB** |
| Opus 32k VoIP `compression_level=0` | 3.72 s | 161× | 2.05 MB | 30 MB |
| Opus 24k VoIP `compression_level=5` | 5.56 s | 108× | 1.43 MB | 21 MB |
| AAC 48k in MP4 (16 kHz mono) | 2.78 s | 216× | 3.65 MB | **44 MB** |
| AAC 32k in MP4 | 2.85 s | 210× | 2.45 MB | 29 MB |
| FLAC 16 kHz | 1.15 s | 520× | 13.45 MB | 161 MB |
| probe (separate worker process) | 0.08 s | — | — | — |
| worker `check` (interpreter + PyAV import) | 0.09 s | — | — | — |

Three facts follow and they drive §3.2:

* WAV is 8× faster to produce than Opus but **8–10× larger**, so the choice is a pure
  bandwidth trade, not a CPU trade.
* **AAC is ~2× faster to encode than Opus** at one third of the WAV size. The shipped
  Opus default is the most expensive of the three options per second of audio.
* Extraction is never the bottleneck by itself (12 s of CPU for 2 h); it only matters when
  it sits on the critical path in front of a slow upload.

### 1.2 Note layer CPU (108 000 characters ≈ 2 h of Persian speech)

| Operation | Time |
| --- | ---: |
| `build_source_index` (first call) | 0.11 s |
| `build_source_index` (cached, `lru_cache(maxsize=16)`) | 0.000 s |
| `split_transcript(64k)` | 0.001 s |
| `gate_notes` (validate + audit + coverage) | **0.023 s** |
| `build_notes_docx` (30 sections, 48 KB output) | **0.17 s** |
| `json.dumps` of a compact payload | <0.001 s |

The note layer's *local* work is noise next to one LLM round-trip (tens of seconds).
`gate_notes` is synchronous on the event loop, but 23 ms is not worth moving to a thread —
it is listed here so nobody "optimises" it instead of the network.

### 1.3 Prompt sizes (default settings)

| Prompt | Size |
| --- | ---: |
| `build_system_prompt("full")` (stable rules) | 8 385 chars ≈ 4 200 tokens |
| same + one chunk's context block | 17 840 chars ≈ 8 900 tokens |

`effective_chunk_chars = 64 000`, `NOTE_CONCURRENCY = 3`, `NOTE_CONTEXT_TOKENS = 131 072`.
A 2-hour lecture is **2 map requests + 1 compile request** — i.e. two serial LLM round
trips, not ten.

---

## 2. Where the time actually goes

Model assumptions: 2-hour lecture; egress 10 Mbit/s (1.25 MB/s) from the bot host to the
provider; Telegram ingress 1.5 MB/s on one MTProto connection (measured range in the
Telethon community: 0.3–1.5 MB/s single, 4.7–5.6 MB/s with four connections); STT
throughput from the September 2026 independent benchmark
([Speechmatics 42×RT](https://echosaw.com/curated-lists/best-speech-to-text-apis/),
[Deepgram Nova-3 351×RT](https://echosaw.com/curated-lists/best-speech-to-text-apis/)),
note generation at ~200 output tok/s with ~3 000 output tokens per request.

### Scenario A — 2 h audio, 115 MB `.m4a`, default providers (`speechmatics,deepgram,…`)

| Stage | Today | Optimised | Change |
| --- | ---: | ---: | ---: |
| Telegram download | 77 s | 24 s | −69 % |
| Probe | 0.1 s | 0.1 s | — |
| Normalise/encode | 0 s (passthrough) | ~20 s on the path (segmented) | +20 s |
| **STT POST (upload)** | **92 s** | **23 s** | **−75 %** |
| **STT compute + polling** | **176 s** | **31 s** | **−82 %** |
| Transcript GET | 21 s | 12 s | −43 % |
| Raw `.txt` delivery | 2 s | 2 s | — |
| Note generation (map+compile) | 40 s | 25 s | −38 % |
| Word build + upload | 2 s | 2 s | — |
| **Total** | **≈ 410 s** | **≈ 139 s** | **−66 %** |

### Scenario B — 2 h video, 1 GB `.mp4` (audio extracted, WAV today)

| Stage | Today | Optimised | Change |
| --- | ---: | ---: | ---: |
| Telegram download | 667 s | 213 s | −68 % |
| Extract + encode | 12 s CPU → 230 MB WAV | 70 s CPU → 29 MB Opus, ~25 s on the path | −87 % bytes |
| STT POST | 184 s | 23 s | −87 % |
| STT compute | 176 s | 31 s | −82 % |
| Transcript GET + notes + delivery | 65 s | 41 s | −37 % |
| **Total** | **≈ 1 104 s** | **≈ 333 s** | **−70 %** |

### Scenario C — 40 min audio with an already-fast provider (Deepgram-class)

| Stage | Today | Optimised | Change |
| --- | ---: | ---: | ---: |
| Download + upload | 27 + 32 s | 8 + 8 s | −73 % |
| STT compute | 12 s | 4 s | −67 % |
| Notes + delivery | 38 s | 30 s | −21 % |
| **Total** | **≈ 111 s** | **≈ 58 s** | **−48 %** |

The lesson: **short jobs are dominated by fixed costs** (two serial LLM calls, one
download, one upload), so the same work buys half as much. Percentages in §4 are quoted
per phase for Scenario A unless stated otherwise.

---

## 3. Layer-by-layer analysis

### 3.1 Telegram ingress — the ceiling is one socket, not the file

`bot._download_media_limited` calls `message.download_media(file=..., progress_callback=...)`.
Telethon's `download_media` exposes **no** `request_size`, `offset` or worker count, and the
iterator clamps every request to `MAX_CHUNK_SIZE = 512 KiB` and awaits it before requesting
the next range. Throughput is therefore bounded by `512 KiB / RTT`: ~8.5 MB/s at 60 ms,
~3.4 MB/s at 150 ms, and lower still on a loaded shared host. `cryptg` is already installed,
so AES-IGE is not the limiter — round-trips are.

Measured community results for splitting one file over several MTProto connections:

| Configuration | Throughput |
| --- | ---: |
| sequential (Telethon default) | 1.50 MB/s |
| 4 sockets × 2 workers (default in that project) | 4.67–5.56 MB/s |
| 1 stream vs 4 streams, 251 MB file | 481 s → 145 s (**3.33×**) |
| free account, multi-connection tooling | 7+ MB/s claimed |

**Plan:** replace `download_media` with N `iter_download(offset=…, limit=…, request_size=…)`
streams writing at their own file offsets (`aiofiles` not needed — `os.pwrite` via
`asyncio.to_thread`), default N = 4, clamped 1–8. Keep every existing guarantee: the byte
limit and the stall watchdog stay in the progress path, and the partial file is still
unlinked on abort. Risk: Telegram `FloodWaitError` appears sooner with more requests
(Telethon's own FAQ says parallel transfers only make it happen sooner); pair it with a
proactive per-account rate gate and fall back to the single stream on the first flood wait.

Second, smaller win: **download at intake, not at worker pickup.** Today the download
happens inside `_process_media`, so it occupies one of the three workers while a queue of
other jobs waits. Starting the transfer in the message handler (bounded by a download
semaphore and the existing disk-headroom check) overlaps ingress with other jobs' CPU/LLM
work and removes queue wait from the user's clock — worth 20–50 % of *perceived* latency
under load, 0 % when idle.

### 3.2 Audio extraction and normalisation — the format decision is a bandwidth equation

Today `media.choose_merge_format` returns WAV unless the estimate exceeds
`presentation_wav_limit_bytes` or the largest configured STT ceiling. For a 2-hour lecture
that means a **230 MB** upload, while 29 MB of Opus (or 44 MB of AAC) carries the same
speech: ASR accuracy is flat from ~32 kbit/s upward (Opus 24–32 kbit/s is within
0.5–1.5 pp WER of WAV, "statistically insignificant" at 32 kbit/s; IBM measured Ogg/Opus
within 2 % WER of WAV/FLAC). Speechmatics recommends 16 kHz mono PCM WAV *for server-side
speed*, which is the opposite trade — worth knowing, and the reason the choice must be
computed rather than hard-coded.

Cost model for 2 h (measured encode times, `E` = egress in MB/s):

```
T_wav  = 230/E                     (12 s of CPU)
T_opus =  58 +  29/E               (70 s of CPU)
T_aac  =  21 +  44/E               (33 s of CPU)
break-even WAV vs Opus: E = 201 MB / 58 s ≈ 3.5 MB/s  (28 Mbit/s)
break-even WAV vs AAC : E = 186 MB / 21 s ≈ 8.9 MB/s  (71 Mbit/s)
```

**Plan:**

1. Add an `AUDIO_TARGET_BPS`/`AUDIO_FORMAT_POLICY=auto|wav|aac|opus` setting and choose in
   `choose_merge_format` from `(measured encode speed × duration, expected bytes ÷ measured
   egress)`, taking the minimum. Seed the egress estimate from the actual Telegram download
   rate of the same job (already observable in the progress callback) — no new probing.
2. Make **AAC 48 kbit/s mono in MP4 the default for transcoded long audio**: 2× faster to
   encode than Opus and it moves the break-even from 28 Mbit/s to ~71 Mbit/s, so it wins on
   almost every shared host. Keep Opus 24 kbit/s as the small-pipe option (it is the best
   quality per byte and both Speechmatics and Deepgram accept Ogg/Opus).
3. Drop Opus `compression_level` from `5` to `0–2` in `media_worker.OPUS_CODEC_OPTIONS`
   (1.6× faster, +5 % size). This ships independently and changes nothing else.
4. **Overlap** instead of serialise: encode segment *i+1* while segment *i* uploads (`§3.3`),
   so the 33–70 s of encoder CPU mostly disappears from the critical path.
5. Optional, needs sign-off: VAD-based silence trimming before transcription. Lecture
   recordings commonly contain 10–25 % silence; cutting it shortens upload *and* provider
   compute by the same share. It changes inter-paragraph gaps, so it must be off by default.

### 3.3 STT POST — upload 10× more bytes than the audio contains

`stt._speechmatics` / `_deepgram` / `_openai_compatible` each stream the prepared file from
disk in 1 MiB buffered reads inside a multipart or raw-body POST. Verified locally:

* `aiohttp.FormData` with a file object **does** set `Content-Length` (3 000 312 bytes for a
  3 MB file), so providers can reject an oversized body early and no chunked-encoding
  penalty applies — nothing to fix here.
* `aiohttp` sends `Accept-Encoding: gzip, deflate` by default, so response compression is
  already negotiated wherever the provider supports it.

Two things are wrong: the volume, and what a failover costs.

| File | Bytes |
| --- | ---: |
| WAV 16 kHz mono, 2 h | 230 MB |
| AAC 48k, 2 h | 44 MB |
| Opus 32k, 2 h | 29 MB |
| Opus 24k, 2 h | 21 MB |

At 1.25 MB/s that is 184 s versus 23 s of transfer for the same speech. Second, the failover
chain re-uploads the whole file to the next provider on any transient failure
(`_attempt_provider` deliberately never re-POSTs, so a failover = a second full upload);
segmenting fixes that too — a retry costs one segment, not the lecture.

There is also a latent timeout trap that the same arithmetic exposes: `STT_TRANSFER_TIMEOUT_SECONDS`
defaults to 600 s, which a 230 MB WAV cannot finish on a host with less than ~3 Mbit/s of
egress. The upload is then killed *after ten minutes*, the chain fails over, and the next
provider burns the same ten minutes on the same file. Either derive the transfer deadline
from `bytes / measured egress` (with a 2× margin) or keep the payload small enough that the
default cannot be hit — both are Phase 2 changes.

**Plan:** (a) the adaptive format from §3.2; (b) segment uploads in flight, 2–4 concurrent,
each with its own `_transfer_timeout` and its own bounded retry; (c) keep the existing rule
that a *submit* is never retried inside one provider.

### 3.4 STT GET — 10-second polling and 25 MB of JSON

`_speechmatics` submits, then `_speechmatics_poll` waits with
`_adaptive_poll_interval(10 s)`: 2.5 s, 5 s, 7.5 s, then a flat 10 s forever. Detection
granularity therefore costs 0–10 s per job (≈5 s average) and, since a 2-hour lecture at
42×RT finishes in ~171 s, it also costs ~17–20 status requests per job — and a queued job
polls for as long as it queues. The transcript itself is then read as one body
(`stt._read_bounded` accumulates chunks and `b"".join`s them) — a second full copy of up to
25–40 MB, then parsed with `orjson`.

Provider-side capabilities that exist today and are unused:

* Speechmatics **long-polls** `GET /jobs/{id}` for up to 5 s before answering
  (`wait=0` disables it) and now offers a *synchronous* transcription mode; their release
  notes (2026-09-22) also report significantly faster processing for files up to 20 minutes.
  Their documented latency guidance is **micro-batching at 25–35 s** per request, 10 POST/s,
  50 GET/s, and "use the notifications webhook instead of polling"
  ([limits](https://docs.speechmatics.com/speech-to-text/batch/limits),
  [microbatching](https://www.speechmatics.com/company/articles-and-news/how-to-build-a-microbatching-workflow-with-the-speechmatics-api)).
* Deepgram offers a `callback` URL for pre-recorded jobs, 100+ concurrent requests on the
  standard tier, and recommends 5–10 minute chunks for large files.

**Plan:** (1) add `wait=5` to the Speechmatics status GET (one-line change, removes the
10 s blind spot); (2) make the poll cadence 0.5 → 1 → 2 → 3 s with jitter instead of a 10 s
floor; (3) optional webhook receiver (`notifications`) for deployments that already expose
HTTPS — it removes polling entirely; (4) read the transcript with a `bytearray` and parse
incrementally per `results` batch for long files.

### 3.5 Note generation — two serial requests, and a prompt that can't be cached

Measured (§1.2, §1.3): local work is negligible; the cost is **two serial LLM round trips**
(2 map chunks in one round at concurrency 3, then one compile). Published throughput for the
default model is 92–359 output tok/s depending on measurement
([Artificial Analysis/Google](https://venturebeat.com/technology/googles-gemini-2-5-flash-lite-is-now-the-fastest-proprietary-model-and);
[benchmarks](https://www.kunalganglani.com/blog/llm-api-latency-benchmarks-2026)); a
3 000-token chunk at 200 tok/s is ≈15 s of pure generation, so per-request latency is
output-token-bound, not prompt-bound.

Six concrete improvements:

1. **Stable prefix.** `build_system_prompt(mode, context_block=block)` appends the
   per-chunk context to the *end* of the 4 200-token rule block. Providers cache on an
   exact byte prefix (Gemini implicit caching: 2 048-token minimum on 2.5 Flash, hits billed
   at ~0.1×; OpenAI: 1 024 tokens, ~0.5×). Move the context block into the **user** message
   so the whole system prompt is byte-identical across every chunk *and every job*. Gain:
   10–25 % lower TTFT and 50–90 % input cost on the cached part.
2. **Concurrency.** `NOTE_CONCURRENCY` defaults to 3 and is capped at 8, but a 2-hour
   lecture is 2 chunks, so raising it does nothing there. It matters for 3 h+ lectures and
   for compile groups: at 6, a 4-chunk job drops from 2 rounds to 1 (−45 % of the map phase,
   ~−20 s). Recommend `NOTE_CONCURRENCY=6` and making it adaptive
   (`min(8, max(3, chunks))`) so it never exceeds provider limits.
3. **Schema-constrained output** instead of `responseMimeType: application/json` alone:
   Gemini `responseSchema`, OpenAI `json_schema` where the gateway documents it. Constrained
   decoding costs <10 % per call and removes the 2–10 % of calls that currently pay for a
   full second request with `JSON_ONLY_REMINDER` ([latency/retry trade-offs](https://nisai.dev/guides/structured-outputs-guide/)).
4. **Compile strategy.** The editorial compile is a third serial request over the whole
   booklet (~15–25 s at 200 tok/s). Two options, both cheap: (a) build and send the
   deterministic merge as the booklet *immediately*, then send the compiled edition as a
   second file ("نسخهٔ ویرایش‌شده") — perceived latency drops by the whole compile request;
   (b) keep one request but route it to the fastest configured provider
   (`Cerebras gpt-oss-120B ≈1 750 tok/s`, `Groq ≈480 tok/s` vs `≈200 tok/s` for Gemini
   Flash-Lite — Artificial Analysis data, 2026), since editorial work tolerates a different
   model than extraction. Rank your own routes with
   `python -m scripts.probe_note_providers --generate lecture.txt` instead of trusting a
   published number: it sends one real map-style request per configured route and reports
   elapsed time.
5. **Tail latency, already implemented, currently off.** `NOTE_TTFB_TIMEOUT_SECONDS` and
   `NOTE_HEDGE_AFTER_SECONDS` (streaming TTFB guard + one hedged second attempt) exist in
   `llm.py` but default to `0`. Setting 25 s / 60 s cuts the P95 note phase substantially for
   at most one extra request. Turn on in `.env` and document the cost.
6. **Micro:** `_deliver_raw` blocks the start of note generation for ~2 s of Telegram upload;
   start both with `asyncio.gather` (the raw text is already persisted first, so the
   ordering guarantee is preserved).

### 3.6 Delivery and export — nothing to win

`build_notes_docx` is 0.17 s for a 30-section booklet (measured) and runs in
`asyncio.to_thread`; the `.docx` is ~50 KB and the raw `.txt` ~100–200 KB, so Telegram
uploads are sub-second. Do not touch this layer.

---

## 4. The plan

### Phase 0 — Instrument before changing (0.5 day, no risk)

Add one structured `pipeline timing` log line per stage (`download`, `probe`, `encode`,
`upload`, `stt`, `fetch`, `notes_map`, `notes_compile`, `docx`, `send`) carrying
`submission_id`, bytes, seconds and engine. Everything below is verified against it; the
percentages in this document are predictions until this line exists.

Two free measurements are already in the tree and should be run before any change:

```bash
python -m scripts.probe_note_providers --generate lecture.txt   # per-route note latency
python -m scripts.perf_bench all                                # local encode/CPU baselines
```

### Phase 1 — Zero-risk configuration and polling wins (1–2 days, −15 % e2e)

| Change | Where | Expected |
| --- | --- | --- |
| `wait=5` on the Speechmatics status GET; 0.5→3 s poll cadence with jitter | `stt._speechmatics_poll` | −5 s/job |
| Start notes concurrently with raw delivery | `bot._process_media` | −2 s |
| `NOTE_CONCURRENCY=6` (adaptive to chunk count) | `config`, `notes.structure_document` | −20 s on 3 h+ |
| `NOTE_TTFB_TIMEOUT_SECONDS=25`, `NOTE_HEDGE_AFTER_SECONDS=60` | `.env.example` + docs | P95 −20–40 % |
| Region-nearest base URLs (`DEEPGRAM_BASE_URL`, `SPEECHMATICS_BASE_URL`) | ops | −RTT per request |
| Parallel Telegram download, N=4, flood-wait fallback | `bot._download_media_limited` | ingress −69 % |

Cumulative: ≈410 s → ≈350 s (**−15 %**).

### Phase 2 — Payload intelligence (2–3 days, −27 % e2e, upload −75 %)

| Change | Where | Expected |
| --- | --- | --- |
| `AUDIO_FORMAT_POLICY=auto` costing WAV/AAC/Opus from measured egress | `media.choose_merge_format` | upload 92 s → 23 s |
| AAC 48k mono as the default transcode target; Opus only for small pipes | `media_worker` | encode 70 s → 33 s |
| Opus `compression_level` 5 → 2 | `media_worker.OPUS_CODEC_OPTIONS` | encode −35 % |
| Downsample-only fast path for 8 kHz voice notes (no 16 kHz up-conversion) | `media_worker` | −bytes, free |

Cumulative: ≈350 s → ≈300 s (**−27 %**), plus a 10× smaller upload, which also makes
failover cheap.

### Phase 3 — Segmented parallel transcription (1–2 weeks, −63 % e2e: the largest single win)

This is the change that removes the dominant term. It is also the only one that needs new
tests around stitching.

1. **Segment** the prepared audio into `K` parts (default `K = min(6, max(1, ceil(minutes/20)))`,
   each ≥10 min) with a 2–5 s overlap, produced by the media worker (one process per
   segment, `--start`/`--duration`, encode straight to AAC/Opus — never through WAV).
2. **Submit in parallel** to the *same* provider (Speechmatics documents microbatching and
   allows 10 POST/s and 20 000 concurrent jobs; Deepgram's standard tier allows 100+
   concurrent requests). Keep the existing provider failover, but per segment.
3. **Stitch** word timings with a per-segment offset; drop the duplicated overlap by
   timestamp and by normalised-text match at the seam; keep the existing paragraphing engine
   unchanged (it only needs monotonic word timings).
4. **Overlap** the pipeline: download → segment → (encode ‖ upload ‖ transcribe) per segment,
   so encoder CPU and transport disappear behind provider compute.

Expected: STT wall 176 s → ~31 s at K=6; transcript GET 21 s → 12 s; pipeline down to
≈152 s (**−63 %**) before the note-layer work. Reliability improves too: a failure costs one
20-minute segment, not the lecture.

### Phase 4 — Note layer (2–3 days, −66 % e2e)

| Change | Where | Expected |
| --- | --- | --- |
| Context block moved from system prompt into the user message (byte-stable prefix) | `notes.build_system_prompt`, `map_chunk` | TTFT −10–25 %, input cost −50–90 % |
| `responseSchema` / `json_schema` structured output | `llm._build_request_for` | removes the retry path (P95) |
| Progressive booklet: deterministic merge first, compiled edition second | `bot._deliver_booklet` | perceived −15–25 s |
| Compile routed to the fastest provider, or skipped for `fast` mode | `config`, `llm` | −10–20 s |
| `asyncio.to_thread` for `gate_notes` (hygiene, not speed — 23 ms) | `notes` | 0 |

Cumulative: ≈152 s → ≈139 s (**−66 %** e2e for Scenario A).

### Phase 5 — Optional / experimental (only with measured benefit, −70 to −75 % e2e)

* Webhook (`notifications`) instead of polling; removes the GET loop entirely.
* VAD silence trimming (−10–25 % of audio ⇒ −10–25 % of upload *and* STT time).
* Additional STT provider behind the existing OpenAI-compatible slot: **Groq
  `whisper-large-v3-turbo` (216×RT, $0.04/h, word timestamps via `verbose_json`)**, or a
  native Soniox adapter ($0.10/h async, Persian support, word timings) as a cheaper/faster
  second engine than Speechmatics Enhanced (42×RT, $0.50/h).
* gzip-tolerant transcript parsing and per-result streaming (only if the provider compresses).

---

## 5. Expected outcome

| Scenario | Today | After Phase 1–4 | Improvement |
| --- | ---: | ---: | ---: |
| A — 2 h audio, 115 MB, batch provider | 410 s | ~139 s | **−66 %** |
| B — 2 h video, 1 GB | 1 104 s | ~333 s | **−70 %** |
| C — 40 min audio, fast provider | 111 s | ~58 s | **−48 %** |
| 6 h lecture, batch provider | ~1 140 s | ~345 s | **−70 %** |

Per-stage improvement, for the "which number do I quote?" question:

| Stage | Improvement | Confidence |
| --- | ---: | --- |
| Telegram download (per MB) | −69 % (−3.3×) | High — measured by third parties, needs FloodWait care |
| Audio encode | −50 % (AAC) / −35 % (Opus cl=2) | High — measured here |
| STT upload bytes | −87 % (WAV→Opus 24k) / −81 % (WAV→AAC 48k) | High — arithmetic |
| STT compute wall-clock | −80 % at K=6 | Medium — depends on provider concurrency |
| Transcript fetch | −43 % (per-segment, gzip) | Medium |
| Note generation | −30 to −45 % | Medium — depends on provider/model |

---

## 6. Risks and guardrails

| Risk | Mitigation |
| --- | --- |
| Telegram `FloodWaitError` from parallel downloads | Cap at 4 sockets, back off to 1 on the first flood wait, keep the existing stall watchdog |
| Segment seams duplicate or drop words | 2–5 s overlap, dedupe on `(normalised text, offset)` window, assert monotonic timings and total word count vs. duration in tests |
| Lower bitrate hurts Persian accuracy | Stay ≥24 kbit/s Opus / ≥48 kbit/s AAC; keep WAV for providers that require it; A/B two real lectures before switching the default |
| Provider concurrency limits (429) | Segment count ≤ provider concurrency, keep `MAX_RETRY_AFTER_SECONDS` and cooldowns; a 429 on one segment must not fail the job |
| Prefix cache misses | The cached prefix must be byte-identical: no timestamps, no per-job ids, no reordered keys in the system prompt |
| Compile skipped ⇒ quality drop | Deliver the compiled edition as a second file; never silently degrade NOTE_QUALITY |
| More moving parts | Every phase ships behind a setting with the old path as default until the timing line proves the win |

Tests to add: segment stitching (offset, overlap, single-word seam), `choose_merge_format`
costing table, parallel-download abort/limit paths, `wait=5` polling, prompt-prefix
stability (hash the system prompt across chunks), and a golden-file check that the same
lecture still produces the same paragraph count.

---

## 7. New settings this plan introduces

| Setting | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_DOWNLOAD_CONNECTIONS` | `4` (1–8) | parallel MTProto range download |
| `DOWNLOAD_AT_INTAKE` | `false` (then `true`) | overlap ingress with the queue |
| `AUDIO_FORMAT_POLICY` | `auto` | `wav` / `aac` / `opus` / `auto` costing |
| `AUDIO_TARGET_KBPS` | `48` | AAC/Opus bitrate for speech |
| `STT_SEGMENT_MINUTES` | `20` | segment length for parallel transcription |
| `STT_SEGMENT_CONCURRENCY` | `3` (1–8) | segments in flight |
| `STT_STATUS_LONG_POLL` | `5` | Speechmatics `wait` seconds |
| `NOTE_CONCURRENCY` (existing) | `3` → `6` | map/compile parallelism |
| `NOTE_PROGRESSIVE_BOOKLET` | `false` | merge first, compiled edition second |

---

## 8. What this plan deliberately does *not* do

| Candidate | Why not |
| --- | --- |
| Optimise `build_notes_docx` / PDF export | 0.17 s measured |
| Off-load `gate_notes` to a thread | 0.023 s measured |
| Replace `orjson`, batch media-worker processes, cache probes | 0.08–0.09 s per process; already batched for decks |
| Add `Content-Length` to the STT upload | Already sent by `aiohttp` (verified) |
| Add response compression | Already requested (`Accept-Encoding` verified) |
| Shrink `NOTE_MAX_OUTPUT_TOKENS` | Caps truncation, not generation time; risks losing content |
| Prefetch/pool provider connections harder | `LLM_PREWARM`, keep-alive and DNS caching already exist |
| Streaming STT over WebSocket for batch files | Total time is provider compute, not first-token latency |

---

## 9. References

* Speechmatics batch limits and microbatching:
  <https://docs.speechmatics.com/speech-to-text/batch/limits> ·
  <https://www.speechmatics.com/company/articles-and-news/how-to-build-a-microbatching-workflow-with-the-speechmatics-api> ·
  status long-poll/synchronous mode: <https://speechmatics.featurebase.app/en/changelog>
* Speechmatics input guidance (16 kHz mono WAV): <https://docs.speechmatics.com/speech-to-text/batch/input>
* Independent STT speed/WER benchmark (Sept 2026, Deepgram 351×RT, Speechmatics 42×RT):
  <https://echosaw.com/curated-lists/best-speech-to-text-apis/>
* Groq Whisper limits and 216×RT: <https://console.groq.com/docs/speech-to-text>
* Opus/AAC bitrate vs ASR accuracy: <https://team-connect.co.uk/info/audio-codecs-explained> ·
  <https://github.com/ibm-cloud-docs/speech-to-text/blob/master/audio-formats.md>
* Telethon downloads (512 KiB clamp, `iter_download` offsets):
  <https://docs.telethon.dev/en/stable/modules/client.html> ·
  parallel-download measurements: <https://github.com/julienfroidefond/stripstream-librarian/pull/85>
* Gemini implicit caching (2 048-token minimum, 0.1× reads):
  <https://ai.google.dev/gemini-api/docs/caching>
* OpenAI/Anthropic caching and the exact-prefix rule:
  <https://machinelearningmastery.com/the-complete-guide-to-inference-caching-in-llms/>
* Structured outputs vs JSON mode (retry cost): <https://nisai.dev/guides/structured-outputs-guide/>
* Output-token throughput (Cerebras ~1 750 tok/s, Groq ~480 tok/s, Gemini Flash-Lite ~92–359):
  <https://tokendyno.com/blog/llm-speed-comparison/>
