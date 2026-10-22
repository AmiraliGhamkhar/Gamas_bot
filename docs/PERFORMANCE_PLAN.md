# Performance plan, second pass: intake, transcode, transcription, notes

This replaces the first pass with numbers from a deeper code read and a second benchmark
round. Every figure is either produced by `python -m scripts.perf_bench` on this code, or
quoted from a provider document or published benchmark (linked inline). Pipeline totals are
modelled from those measured throughputs; the assumption is stated next to every table,
because provider latency depends on your keys, region and plan.

**Headline:** a 2-hour lecture takes **~7 minutes** today (405–534 s depending on which
published speed factor you trust for the default model). **~75 % of that can be removed**,
but the ranking of levers changed completely in this pass: the largest win is a
**configuration choice** (STT model and provider order), not new code, and the
segmentation work the first pass called "the big one" is now *not* worth building.
The note layer, which the first pass dismissed, becomes the frontier for short jobs.

---

## 0. What changed from the first pass

| First pass said | Second pass found | Why it matters |
| --- | --- | --- |
| STT compute ≈ 176 s; fix it by segmenting into K≤6 parallel requests | The default (`SPEECHMATICS_MODEL=enhanced`, first in `STT_PROVIDERS`) is the **slowest** route available. Speechmatics publishes speed factor **24.1× for enhanced vs 43.9× for standard vs 206× for melia-1**; Deepgram Nova-3 measures 351×. Compute is 171–300 s and **model/order choice removes 50–90 % of it with one env var** | The biggest lever was configuration. Segmentation added stitching risk to fix a problem that a config change fixes better and cheaper |
| "Add `wait=5` to the Speechmatics status GET" | The GET endpoints **already apply a 5 s default wait**. The real feature is `wait` **on `POST /jobs`**, which returns the finished transcript inside the submission response — the status and transcript GETs disappear for short jobs | Correct mechanism, different endpoint: it removes a whole round trip instead of tightening a poll |
| Prefix caching: "−10–25 % TTFT, −50–90 % input cost" | Measured prompt sizes: the stable rule block is **4 200 tokens** against a **21–32 k token** chunk, so it is ~10–15 % of map input. The compile prompt (1 200 tokens) is **below the 2 048-token cache floor** and can never be cached | Real but small: ~10–13 % of input cost and well under a second. Demoted from a latency lever to cost hygiene |
| Default transcode target should be AAC 48 kbit/s | With the right libopus flags, **Opus is now both faster and smaller than AAC**: `application=audio, compression_level=0` encodes at **355×** (vs 338× AAC-fast, 106× for the shipped Opus settings) and produces **13.5 MB/h vs 21.9 MB/h** | AAC is demoted to a fallback. The shipped Opus flags are the worst tested combination on *both* axes |
| Parallel download needs "N sockets" | Telethon pipelines concurrent requests: `MTProtoSender` keeps `_pending_state` per message id and `MessagePacker` batches them, so **4 concurrent `iter_download` iterators on one client** should already break the round-trip serialisation | Start with one connection (safer for flood waits), add borrowed senders only if the spike shows a ceiling |
| `orjson` / response reader are micro-optimisation candidates | Measured: 3.3 MB transcript body → `orjson` 12 ms, stdlib 47 ms; bounded 256 KiB read 1 ms | Confirmed irrelevant. Do not touch |

---

## 1. What was measured, and how

`scripts/perf_bench.py` synthesises its own audio and Persian text, so it runs offline on
the cPanel host before and after a change:

```bash
python -m scripts.perf_bench audio   --minutes 10   # format / encoder matrix
python -m scripts.perf_bench frames  --minutes 10   # decode vs resample vs encode
python -m scripts.perf_bench cpu     --chars 108000 # note layer deterministic work
python -m scripts.perf_bench json                   # STT response parsing
python -m scripts.perf_bench prompts --chars 108000 # prompt size and chunk count
```

### 1.1 Format matrix, per audio hour (measured, 2-core sandbox)

| Target | Encode speed | Size per hour | 2 h output | 2 h encode CPU |
| --- | ---: | ---: | ---: | ---: |
| WAV 16 kHz mono PCM (today's default) | **824×** | 115 MB | 230 MB | 9 s |
| **Opus 32k, `application=audio`, `compression_level=0`** | **355×** | **13.5 MB** | **27 MB** | **20 s** |
| AAC 48k in MP4 (`aac_coder=fast`) | 338× | 21.9 MB | 44 MB | 21 s |
| AAC 48k in MP4 (default `twoloop`) | 243× | 21.9 MB | 44 MB | 30 s |
| Opus 16k, voip, cl=0 | 251× | 6.9 MB | 14 MB | 29 s |
| Opus 24k, voip, cl=0 | 210× | 10.2 MB | 20 MB | 34 s |
| Opus 32k, voip, cl=0 | 207× | 13.5 MB | 27 MB | 35 s |
| **Opus 32k, voip, cl=5 — shipped today** | **106×** | 12.6 MB | 25 MB | **68 s** |
| FLAC 16 kHz | 564× | 112 MB | 224 MB | 13 s |

Three facts drive §3.3:

* The shipped `OPUS_CODEC_OPTIONS` (`voip`, `compression_level=5`) is **3.3× slower than
  necessary**; `application=audio, compression_level=0` is faster *and* better for speech
  recognition (libopus `voip` uses the SILK-only path; `audio` keeps the hybrid
  SILK+CELT path, so more of the 4–8 kHz band that carries fricatives survives).
* WAV is still the fastest to produce and 8–10× larger. It is a bandwidth trade, not a CPU
  trade — worth ~0 s on a fast pipe, ~70 s on a 10 Mbit/s one.
* `frame_duration=60` and `compression_level` between 0 and 5 buy nothing measurable; the
  only flag that matters is `application`.

### 1.2 Where transcode time actually goes (null muxer, 4 min source)

| Stage | Speed |
| --- | ---: |
| decode only (AAC → frames) | 1 985× |
| decode + resample 48 kHz → 16 kHz mono | 947× |
| … + PCM encode | 821× |
| … + AAC 48k (`aac_coder=fast`) | 345× |
| … + Opus 32k (`audio`, cl=0) | 353× |
| … + Opus 32k (`voip`, cl=5) | 104× |

Decoding is free (0.6 s per hour); the sample-rate converter adds ~50 %; **the encoder is
the whole cost**, and only for lossy targets. So the choice of target format is the only
lever that matters, and it must be made from *cardinality of bytes on the wire*, not from
CPU alone.

### 1.3 Note layer, deterministic work (measured)

| Operation | 2 h lecture (102 k chars) | 6 h lecture (307 k chars) |
| --- | ---: | ---: |
| `build_source_index` (first call) | 0.11 s | 0.37 s |
| `build_source_index` (cached) | 0.000 s | 0.000 s |
| `split_transcript(64k)` | 0.001 s | 0.001 s |
| `build_document` (timed words → paragraphs) | 0.02 s | 0.06 s |
| `gate_notes` (validate + audit + coverage) | **0.014 s** | 0.015 s |
| `build_notes_docx` (30 sections) | **0.19 s** | 0.15 s |

All linear, all noise next to one LLM round trip. `gate_notes` is synchronous on the event
loop and could be moved to a thread, but 14 ms does not justify the change.

### 1.4 STT response handling (measured)

| Operation | 2 h transcript (3.3 MB, 18 k words) |
| --- | ---: |
| `_read_bounded` (256 KiB chunk loop + join) | 1 ms |
| `json.loads` | 47 ms |
| `orjson.loads` (shipped) | **12 ms** |
| `_deepgram_transcript` (build `Word` list) | 11 ms |

### 1.5 Prompt sizes and rounds

| Prompt | Size |
| --- | ---: |
| `build_system_prompt("full")` — the stable rule block | 8 385 chars ≈ 4 200 tokens |
| same + one chunk's context block (today: inside the system prompt) | 17 840 chars ≈ 8 900 tokens |
| `COMPILE_SYSTEM_PROMPT` | 2 405 chars ≈ **1 200 tokens** (below the 2 048-token implicit-cache floor) |

With `NOTE_CHUNK_CHARS=64 000` (measured): a 2 h lecture is **2 chunks → 1 map round** at
any concurrency ≥2; a 6 h lecture is **6 chunks → 2 rounds at `NOTE_CONCURRENCY=3`, 1 round at
6**. So the map phase is already one or two rounds, and the other round trip a real job pays
is the **compile pass** — which is why Phase 4 attacks the compile rather than the map.

---

## 2. Where the time goes (revised)

Assumptions: 2 h lecture; bot→provider egress 10 Mbit/s (1.25 MB/s); Telegram ingress
1.5 MB/s on one MTProto connection; note output 3 000 tokens per request at 92–887 tok/s
(the published spread for Gemini 2.5 Flash-Lite). STT compute uses **published speed
factors**: enhanced 24.1×, standard 43.9×, melia-1 206× (Speechmatics/Artificial Analysis),
Deepgram Nova-3 351× (independent benchmark).

### Scenario A — 2 h audio, 115 MB `.m4a`, default providers, `enhanced`

| Stage | Today | Revised target | How |
| --- | ---: | ---: | --- |
| Telegram download | 77 s | 24 s | 4 pipelined range downloads |
| Upload (115 MB passthrough today; 27 MB Opus) | 92 s | 22 s | Opus 32k `audio`/cl0 |
| Encode (only when not passthrough) | 0 s | 20 s, hidden | encode segment *i+1* while *i* uploads |
| **STT compute** | **171–300 s** | **~35 s** | `melia-1` (206×) — or Nova-3 first (351×) |
| Status polling + transcript GET | 21 s | ~2 s | `wait` on `POST /jobs` (transcript embedded) |
| Raw `.txt` delivery | 4 s | 4 s | — |
| Notes (map + compile) | 40 s | 25 s | progressive booklet, fast compile route |
| **Total** | **405–534 s** | **≈ 112 s** | **−72…−79 %** |

The first pass reached −66 % by building segmentation. This reaches −75 % mostly by
configuring the chain correctly, and it keeps one contiguous transcription (no seams).

### Scenario B — 2 h video, 1 GB `.mp4` (extraction required)

| Stage | Today | Revised target |
| --- | ---: | ---: |
| Telegram download (1 GB at 1.5 MB/s, one stream) | 683 s | 208 s |
| Extract 2 h → WAV 230 MB → upload | 11 s + 184 s | — |
| Extract 2 h → Opus 27 MB → upload | — | 20 s (hidden) + 22 s |
| STT compute | 171–300 s | 35 s |
| Fetch + notes + delivery | 65 s | 31 s |
| **Total** | **1 114–1 243 s** | **≈ 291 s** (**−74…−77 %**) |

### Scenario C — 40 min audio, Deepgram-class provider first

| Stage | Today | Revised target |
| --- | ---: | ---: |
| Download + upload + compute + fetch | 66 s | 24 s |
| Notes + delivery | 45 s | 24 s |
| **Total** | **111 s** | **≈ 50 s** (**−55 %**) |

**The lesson of the second pass:** once the STT side is fixed, the note layer is **about half
of a 40-minute job** and a fifth of a 2-hour job. That is where the remaining latency lives.

---

## 3. Layer analysis: the best available method for each

### 3.1 The largest lever is configuration — model and provider order

`stt.transcribe` walks `STT_PROVIDERS` in order, and `config.DEFAULT_STT_PROVIDERS` is
`speechmatics,deepgram,openai_compatible` with `SPEECHMATICS_MODEL=enhanced`. The module
docstring is explicit: *"The provider order in `STT_PROVIDERS` is the failover policy."*
That means the default deployment routes every job to the **slowest** configured engine.

Speaking of the models, Speechmatics' own comparison table (batch: `enhanced`, `standard`,
`melia-1`, `oak-1`):

| Model | Turnaround (vendor words) | Speed factor | Batch features this bot consumes |
| --- | --- | --- | --- |
| `enhanced` (today's default) | "Fast" | **24.1×** | all |
| `standard` | "Fastest" | **43.9×** | all |
| `melia-1` | **"Fastest on Batch"** | **206×** | word timings ✔, punctuation ✔, notifications ✔; **no confidence scores, no custom dictionary** — this bot uses neither |
| `oak-1` (medical) | "Fastest" | — | batch only, healthcare |

Additional facts that make `melia-1` the natural default here:

* It is **cheaper**: $0.12–0.24/h against $0.38–0.75/h for `enhanced`.
* It requires `language=multi` (not `auto`) — **already implemented**: `stt._normalize_language`
  returns `multi` for `melia-1`/`oak-1`.
* The request body this bot sends is exactly
  `{"type": "transcription", "transcription_config": {"language": …, "model": …}}`
  (`stt._speechmatics`) — no diarization, custom dictionary, entities, partials or
  notifications — and `_speechmatics_poll` sets `confidence = None` rather than reading a
  score. Every field the bot actually uses is supported by Melia-1 on Batch, so switching
  models changes no request or response handling.
* It is multilingual with code-switching, which fits Persian lectures peppered with English
  terms — the exact content this bot transcribes.
* Vendor claim: a 60-minute recording in **under 20 s**, 10 minutes in under 5 s. An
  independent benchmark put Melia-1 at 12.1 s average per file against 22.6 s for enhanced.
* Batch endpoints are regional: `eu1.asr.api.speechmatics.com`, `us1...`, `au1...`. The
  shipped default `https://asr.api.speechmatics.com/v2` is not listed for Melia batch, so
  `SPEECHMATICS_BASE_URL` must be set to the nearest regional host when switching.

**Method, in order of preference (all zero-code):**

1. If Deepgram is configured: `STT_PROVIDERS=deepgram,speechmatics` — Nova-3 measured 351×
   by an independent benchmark, i.e. ~2 h in ~20 s, and Persian is supported by `nova-3`.
   Keep Speechmatics as the validated fallback.
2. Otherwise: `SPEECHMATICS_MODEL=melia-1` + `SPEECHMATICS_BASE_URL=https://eu1.asr.api.speechmatics.com/v2`.
3. If Melia's Persian quality fails the A/B (§4, gate E1): `SPEECHMATICS_MODEL=standard`
   (43.9× vs 24.1×, i.e. −44 % compute, same features, ~40 % cheaper).

**Guard rails:** add a `--check` warning when `speechmatics` is first *and* `enhanced` is
selected *and* another configured provider is known-fast, so the default cannot silently be
the slow one. Also log the chosen model plus the observed seconds-per-audio-minute at the
end of `transcribe` — the ratio is the only provider-independent speed signal the bot can
collect itself.

### 3.2 Telegram ingress — serialised 512 KiB round trips

`bot._download_media_limited` uses `message.download_media(...)`, which exposes no
`request_size`, `offset` or worker count. Reading the installed Telethon 1.45:

* `_DirectDownloadIter._load_next_chunk` issues **one** `GetFileRequest` and awaits it
  before the next; `request_size` is clamped to
  `MAX_CHUNK_SIZE = 512 KiB` (`MIN_CHUNK_SIZE = 4 KiB`).
* Throughput is therefore `512 KiB / RTT` in theory (~8.5 MB/s at 60 ms), but **1.5 MB/s in
  measured practice** — Telegram throttles per connection and the client serialises one
  request at a time. `cryptg` is installed, so AES-IGE is not the limit; round trips are.
* `MTProtoSender` keeps `_pending_state` keyed by message id and `MessagePacker` batches
  messages into containers, so **several concurrent RPCs on one client are pipelined over
  the same connection**. Concurrent `iter_download` iterators at different offsets should
  therefore multiply throughput without opening sockets.

Measured community results for range-splitting one file:

| Configuration | Throughput |
| --- | ---: |
| sequential (Telethon default) | 1.50 MB/s |
| 4 sockets × 2 workers | 4.67–5.56 MB/s |
| 1 stream → 4 streams, 251 MB file | 481 s → 145 s (**3.33×**) |

**Method:**

1. **Spike first (half a day):** four concurrent iterators on **one** client, each owning a
   contiguous byte range:

   ```python
   chunks = range_bytes // request_size          # limit counts chunks, not bytes
   async for chunk in client.iter_download(
       message.media,
       offset=start,                              # byte offset, multiple of 4 KiB
       limit=chunks,                              # chunk count (Telethon's `limit`)
       request_size=512 * 1024,                   # == MAX_CHUNK_SIZE, keep == chunk_size
       file_size=total,                           # avoids an extra probe request
   ):
       await asyncio.to_thread(os.pwrite, fd, chunk, written)
   ```

   Keep `chunk_size == request_size` (otherwise Telethon re-slices and does more work) and
   keep offsets on 4 KiB boundaries. Measure MB/s against the current single-stream path on
   the real host; if it reaches ≥2.5×, ship it, if not, try borrowed exported senders
   (`_borrow_exported_sender`) as step 2.
2. Keep every existing guarantee: byte ceiling, stall watchdog, partial-file cleanup.
3. Handle `FloodWaitError` explicitly — the current download path does not, and parallel
   transfer makes floods arrive sooner, per Telethon's own FAQ. On the first flood wait:
   sleep the advertised time, drop to one stream for the rest of the job.
4. Move the transfer to intake (bounded by a download semaphore) so it overlaps other jobs'
   CPU/LLM work instead of holding one of the three workers.

### 3.3 Transcode — one constant is 3.3× too slow

`media_worker.OPUS_CODEC_OPTIONS = {"application": "voip", "compression_level": "5"}` is the
most expensive of everything measured (§1.1): 106× while `{"application": "audio",
"compression_level": "0"}` is 355× at the same bitrate and slightly *better* spectrum for
ASR. `media.choose_merge_format` then decides WAV-vs-Opus from a *size* estimate only.

**Method:**

1. Change the constant (one line, no behaviour change beyond speed and quality).
2. Replace the size-only decision with a time model, using the job's own measured Telegram
   download rate as the egress estimate (already observed by the progress callback — no new
   probe):

```
cost(format) = encode_seconds(format, duration) + bytes(format, duration) / egress
```

   For a 2 h lecture and `E` = egress in MB/s: `wav = 9 + 230/E`, `opus32 = 20 + 27/E`,
   `opus24 = 34 + 20/E`. Break-even WAV→Opus is `E = 203 MB / 11 s ≈ 18 MB/s` — i.e. **WAV
   is only worth it above ~145 Mbit/s**, which no shared host has. Make Opus the default and
   expose `AUDIO_FORMAT_POLICY=auto|wav|opus|aac` for the exceptions (a provider that
   prefers PCM, a link that is genuinely local).
3. Keep AAC 48k as the fallback where a provider rejects Ogg or where per-request transfer
   overhead dominates (AAC in MP4 is also 2× faster than the *shipped* Opus settings).
4. Encode segment *i+1* while segment *i* uploads, so encoder CPU leaves the critical path.
5. Note the one counter-argument and keep it inside the policy, not the default:
   Speechmatics recommends 16 kHz mono PCM for *server-side* speed, so a deployment that
   measures a slow upload but unusually fast processing may legitimately prefer WAV. That is
   what `AUDIO_FORMAT_POLICY=wav` is for; it should not be the default for a 10 Mbit/s host.
6. Optional, off by default: VAD-based silence trimming. Lecture audio commonly contains
   10–25 % silence; cutting it shortens upload *and* provider compute by the same share, but
   it changes inter-paragraph gaps.

### 3.4 STT POST/GET — `wait` on the submit, not a tighter poll

`_speechmatics` submits and returns; `_speechmatics_poll` then sleeps 0.5 → 2.5 → 5 → 7.5 s and then a flat
10 s between `GET /jobs/{id}` calls. Two corrections to the first pass:

* **The GETs already long-poll for 5 s** by default (`wait=0` disables it), so tightening our
  own cadence buys little; the remaining cost is the detection granularity of the sleep.
* **`wait` works on `POST /jobs` too**: `POST /jobs/?wait=60&format=json-v2` holds the
  request until the job reaches a terminal state and returns the finished transcript nested
  under `json-v2`, with `status` reporting the outcome. If `wait` elapses first, `status` is
  `created` and the normal polling path continues — the documented fallback, and providers
  cap the wait server-side.

**Method:** submit with `params={"wait": "120", "format": "json-v2"}`. On `status == done`
and a `json-v2` payload, parse the words straight out of the POST response and **skip both
the status loop and the transcript GET** (removes ~20 s and one 3.3 MB transfer for a 2 h
job). Otherwise fall back to the current loop unchanged. Bound the response with the
existing `max_stt_response_bytes`. Do not build a webhook receiver: it needs public HTTPS,
which cPanel shared hosting does not provide, and `wait` already removes the polling.

The upload itself needs no protocol work: verified locally that `aiohttp.FormData` with a
file object emits `Content-Length` (3 000 312 for a 3 MB file) and that the client sends
`Accept-Encoding: gzip, deflate`. What is left is *volume* (§3.3) and the fact that a
failover re-uploads the whole file (§3.8).

### 3.5 Segmentation — demoted, and only for the pathological case

With a 200×-class model, a 20-minute segment finishes in ~6 s, so segmenting a 2 h lecture
into 6 requests saves **under 10 s** over a single 35 s request, while introducing seam
deduplication, per-segment failover and a stitching test matrix. The economics only work
when the fastest reachable engine is slow — e.g. a self-hosted endpoint, `enhanced`-only
access, or a queue-heavy region.

**Method (only if the timing line shows upload ≫ compute):** segment at 20 minutes — the
same window Speechmatics reports as its accelerated path for `standard`/`enhanced` — with
2–5 s overlap, submit with `wait` as above, offset word timings per segment, dedupe the
overlap on `(normalised text, offset)`. Keep `media` on AAC/Opus; never materialise WAV per
segment. Ship behind `STT_SEGMENT_MINUTES=0` (off) and enable only where it measures faster.

### 3.6 Notes — now the frontier

Measured: local work is negligible, and a 2 h lecture is one map round plus one compile
round. So latency is `(output tokens) / (tokens per second)`, twice over.

1. **Route for throughput, per provider.** The strongest zero-code lever: on an OpenRouter
   route, add `"provider": {"sort": "throughput"}` (or the `:nitro` model suffix) — the
   default is *price-weighted* load balancing, which can pick a much slower upstream of the
   same model. On Groq/Cerebras routes, `NOTE_REASONING_EFFORT=low` keeps reasoning tokens
   out of the output budget (already implemented). On Gemini, `NOTE_GEMINI_THINKING_BUDGET=0`
   (the default) is correct — thinking would be pure latency.
2. **Progressive booklet.** Deliver the deterministic merge immediately, then the compiled
   edition as a second file ("نسخهٔ ویرایششده"). The compile is a full extra round trip;
   this removes it from the user's critical path instead of guessing whether it is worth it.
3. **Adaptive concurrency and chunking.** `NOTE_CONCURRENCY=6` (cap 8, and
   `min(8, chunks)` so it never over-subscribes) takes a 6 h lecture from 2 map rounds to 1
   (measured, §1.5).
   For very long lectures, raising `NOTE_CHUNK_CHARS` toward the context budget (≈96 k for a
   131 k-token window) turns 5 chunks into 3 and 2 rounds into 1 — verify the
   `finish_reason=length` rate (already surfaced as `LLMOutputBudgetError`) before and after.
4. **Enable the tail-latency machinery that already exists.** `NOTE_TTFB_TIMEOUT_SECONDS`
   and `NOTE_HEDGE_AFTER_SECONDS` (streaming first-byte guard + one hedged attempt) are
   implemented in `llm.py` and default to `0`. Setting 25 s / 60 s cuts P95 for at most one
   extra request.
5. **Schema-constrained output.** `responseMimeType: application/json` is a request, not a
   guarantee; a provider that answers with prose costs a **full second request** with
   `JSON_ONLY_REMINDER`. Gemini's `responseSchema` and OpenAI-compatible `json_schema` make
   the answer structurally valid at <10 % per-call overhead.
6. **Prefix hygiene (cost, not speed).** Move the per-chunk context block out of the system
   prompt into the user message so the 4 200-token rule block is byte-identical across every
   chunk and job: it exceeds the 2 048-token implicit-cache floor on Gemini 2.5 Flash, so
   cached reads bill at ~0.1× — about 10–13 % of map input. Never insert a timestamp, job id
   or reordered keys into that block.
7. **Meta:** `llm._log_usage` already prints `input_tokens`, `output_tokens`, `ttfb_ms` and
   `elapsed_ms` per request. Use the collected distribution — not this document — to decide
   between (2) and (3) on your own traffic, and `python -m scripts.probe_note_providers
   --generate lecture.txt` to rank your configured routes.

### 3.7 Delivery, export and the rest — nothing to win

`build_notes_docx` is 0.15–0.19 s for a 30-section booklet and already runs in
`asyncio.to_thread`; the `.docx` is ~50 KB and the raw `.txt` 100–200 KB, so Telegram
uploads are sub-second. Leave them.

The one thing worth a small change is not throughput but *Telegram API pressure*:
`progress.JobProgress._animate` publishes an edit every `ANIMATION_INTERVAL_SECONDS` for the
whole job, with a flood streak backoff. On a job that now spends minutes in download, encode
and provider compute, that ticker — not the pipeline — is the largest source of API calls,
and a flood wait there stalls the update path. Throttle to a slower cadence during the long
stages (and on the first `FloodWaitError`, drop to a 30 s cadence rather than per-frame
backoff). Cheap, and it protects the speedups above from being masked by rate limiting.

### 3.8 Three traps the arithmetic exposes

These are not optimisations; they are ways the current defaults can *cost* minutes.

1. **The transfer deadline cannot fit the file.** `STT_TRANSFER_TIMEOUT_SECONDS` is 600 s,
   and a 2 h lecture encoded as WAV is 230 MB, which needs ~3 Mbit/s of egress to clear
   inside ten minutes. On a slower host the upload is killed at 600 s, the chain fails over,
   and the next provider spends another 600 s on the same bytes. Fix: derive the transfer
   deadline from `bytes / measured egress` with a 2× margin (the egress estimate already
   exists, §3.3), and keep the default payload small enough that it is rarely reached.
2. **The failover chain re-uploads.** `_attempt_provider` deliberately never re-POSTs, so a
   transient submit failure costs a **full second upload**. Segmenting would bound that to one
   segment, which is one more reason segmentation matters for slow/pathological links — but
   it is not a reason to build it for links that work.
3. **One media slot, three workers.** `MAX_ACTIVE_JOBS=3` and `MEDIA_WORKER_CONCURRENCY=1`
   mean a single `ffmpeg` at a time. On a 2-vCPU host that is the correct cap, but during the
   encode the other two jobs sit in `_media_semaphore`. Two consequences: encoding must be
   *overlapped with upload* (it is network-idle, CPU-busy), and the download must happen at
   intake so it never consumes one of the three job slots.

---

## 4. The plan

### Phase 0 — Instrument and decide (1 day, no risk)

1. One structured `pipeline timing` line per stage (`download`, `probe`, `encode`, `upload`,
   `stt`, `fetch`, `notes_map`, `notes_compile`, `docx`, `send`) with `submission_id`,
   bytes, seconds, engine and model. Every percentage below is a prediction until this
   exists, and each phase's gate is read from it.
2. Run the two probes that already exist:
   `python -m scripts.perf_bench all` and
   `python -m scripts.probe_note_providers --generate lecture.txt`.
3. **Gate E1 — Persian quality A/B:** transcribe two real lectures with `melia-1` and
   `enhanced`; compare word error spot checks *and* the note gate metrics
   (`coverage`, fabrications, dropped tables/tips). Melia ships only if it is not worse.
   Same for `standard` as the safety fallback.

### Phase 1 — Configuration (same day as E1, −40…−53 % e2e)

| Change | Where | Expected |
| --- | --- | --- |
| `STT_PROVIDERS=deepgram,speechmatics` if Deepgram is configured | `.env` | compute 171–300 s → ~20 s |
| else `SPEECHMATICS_MODEL=melia-1` + regional `SPEECHMATICS_BASE_URL` | `.env` | compute → ~35 s |
| else `SPEECHMATICS_MODEL=standard` | `.env` | compute −44 % |
| `NOTE_TTFB_TIMEOUT_SECONDS=25`, `NOTE_HEDGE_AFTER_SECONDS=60` | `.env` | P95 −20…40 % of the note phase |
| `NOTE_CONCURRENCY=6` | `.env` | map rounds halved for ≥3 h |
| `--check` warning when the slow default combination is detected | `config` | prevents regression |

Scenario A after Phase 1: ≈ 405–534 s → **≈ 250 s (−40…−53 %)**. All of it is
configuration; nothing in this phase can regress correctness.

### Phase 2 — I/O (2–3 days, −64…−73 % cumulative)

| Change | Where | Expected |
| --- | --- | --- |
| Opus `application=audio`, `compression_level=0` | `media_worker.OPUS_CODEC_OPTIONS` | encode 106× → 355× |
| Time-based format choice (`encode + bytes/egress`) | `media.choose_merge_format` | upload 92 s → 19 s |
| 4 pipelined `iter_download` ranges + FloodWait fallback | `bot._download_media_limited` | download 77 s → 24 s |
| Download at intake, bounded by a semaphore | `bot` handler | queue wait off the user's clock |
| Encode segment *i+1* while *i* uploads | `bot._process_media` | encoder off the critical path |

Scenario A after Phase 2: ≈ 250 s → **≈ 146 s (−64…−73 %)**. The download spike is the
gate: if it does not reach 2.5×, keep the single stream and the phase still lands at
**≈ 200 s**.

### Phase 3 — STT call shape (1–2 days, −70…−77 % cumulative, +robustness)

| Change | Where | Expected |
| --- | --- | --- |
| `wait=120&format=json-v2` on `POST /jobs`, parse embedded transcript | `stt._speechmatics` | removes 21 s and one 3.3 MB GET |
| Keep the polling fallback and the existing deadline budget | `stt` | no regression |
| Adaptive poll cadence 0.5 → 3 s with jitter **only** for the fallback path | `stt._speechmatics_poll` | −5 s when `wait` times out |

Scenario A after Phase 3: ≈ **121 s (−70…−77 %)**.

### Phase 4 — Notes (2–3 days, −74…−80 % cumulative; the whole win on short jobs)

| Change | Where | Expected |
| --- | --- | --- |
| Progressive booklet (merge first, compiled edition second) | `bot._deliver_booklet` | perceived −15…25 s |
| Compile routed to the fastest configured route | `config`, `llm` | −10…20 s |
| `provider.sort=throughput` / `:nitro` on OpenRouter routes | `llm._build_request_for` | upstream-dependent, often 2–5× output speed |
| `responseSchema` / `json_schema` structured output | `llm._build_request_for` | removes the retry path |
| Context block moved to the user message (cache-stable prefix) | `notes.build_system_prompt` | ~10–13 % input cost |
| Adaptive concurrency `min(8, chunks)` | `notes.structure_document` | no over-subscription |

Scenario C after Phase 4: ≈ 111 s → **≈ 35 s (−68 %)**. That is the point of the second
pass: on a short job almost everything left is the note layer.

### Phase 5 — Only if measured (not planned work)

| Candidate | Trigger to consider it |
| --- | --- |
| Segmented parallel transcription | timing line shows upload ≫ compute (e.g. self-hosted engine, `enhanced`-only access) |
| VAD silence trim | timing line shows a large silence share in the source |
| Webhook/callback instead of polling | the host gets public HTTPS and `wait` proves insufficient |
| Second engine in the `openai_compatible` slot (Groq `whisper-large-v3-turbo`, 216×, 25 MB free-tier cap, 7 200 audio-s/h free-tier ceiling) | a cheaper/faster fallback is worth a quality A/B |

### Rejected, with the reason

| Candidate | Why not |
| --- | --- |
| Self-hosted ASR (faster-whisper, Nemotron, transcribe.cpp) | measured at **0.5× real time** for faster-whisper large-v3 int8 on 2 vCPUs, and this deployment is 2-core shared hosting with no GPU; a 2 h lecture would take 4 h. Nemotron 3.5 needs a GPU to reach sub-real-time |
| Streaming WebSocket STT for batch files | total time is provider compute, not first-token latency |
| Response gzip / `Content-Length` work | already emitted/negotiated (verified) |
| `orjson`, `_read_bounded`, `build_notes_docx`, `gate_notes` rewrites | measured at 1–160 ms |
| Shrinking `NOTE_MAX_OUTPUT_TOKENS` | caps truncation, not generation time; risks losing content |
| Segmentation as a default | saves <10 s with a 200×-class model, costs seam risk |
| Extra connection pooling / pre-warm work | `LLM_PREWARM`, keep-alive and DNS caching already exist |

---

## 5. Expected outcome

| Scenario | Today | After Phase 1 | After 1–2 | After 1–3 | After 1–4 |
| --- | ---: | ---: | ---: | ---: | ---: |
| A — 2 h audio, defaults | 405–534 s | ~250 s (−40…−53 %) | ~146 s (−64…−73 %) | ~121 s (−70…−77 %) | ~106 s (**−74…−80 %**) |
| B — 2 h video, 1 GB | 1 114–1 243 s | ~978 s (−12…−21 %) | ~350 s (−69…−72 %) | ~306 s (−73…−75 %) | ~291 s (**−74…−77 %**) |
| C — 40 min, fast provider | 111 s | ~90 s (−19 %) | ~50 s (−55 %) | ~47 s (−58 %) | ~35 s (**−68 %**) |
| 6 h lecture | ~1 170 s | ~696 s (−41 %) | ~324 s (−72 %) | ~300 s (−74 %) | ~285 s (**−76 %**) |

Per-stage improvement, with confidence:

| Stage | Improvement | Confidence |
| --- | ---: | --- |
| STT compute (`enhanced` → `melia-1`/Nova-3) | **−80…−93 %** | High — vendor-published and independently benchmarked; verify Persian quality (E1) |
| STT upload bytes | −88 % (230 → 27 MB per 2 h) | High — arithmetic on measured sizes |
| Encode CPU | −70 % (106× → 355×) | High — measured here |
| Telegram download | −69 % | Medium — needs the Phase 0 spike and FloodWait care |
| Status/transcript fetch | −90 % (`wait` on POST) | Medium — depends on the server-side cap |
| Notes | −35…−45 % (progressive + throughput routing) | Medium — provider/model dependent |

---

## 6. Risks and guardrails

| Risk | Mitigation |
| --- | --- |
| Melia-1 Persian quality is worse than `enhanced` | Gate E1 compares WER spot checks *and* the note gate metrics; fall back to `standard` (still −44 % compute) |
| Melia lacks custom dictionary / confidence | This bot uses neither; it does use word timings and punctuation, which Melia supports on Batch |
| Melia batch needs a regional endpoint | Set `SPEECHMATICS_BASE_URL` to `eu1`/`us1`/`au1`; add it to `--check` |
| `wait` on POST holds a socket | Explicit, bounded `wait` (≤120 s) inside the existing `STT_TRANSFER_TIMEOUT_SECONDS`; treat `status=created` as the normal fallback |
| FloodWait from parallel download | Pipelined ranges on one client first, cap at 4, back off to 1 on the first flood, keep the stall watchdog |
| Lower bitrate hurts accuracy | 32 kbit/s Opus `audio` is essentially transparent for speech; keep `AUDIO_FORMAT_POLICY=wav` available; A/B two lectures |
| Opus `application=audio` behaves differently | It is the hybrid speech+music path; for lecture speech it preserves more high band than `voip`. Verify one transcript |
| `provider.sort=throughput` costs more | It can pick a pricier upstream; run it for the compile call (the long one) and keep price sorting where cost dominates |
| Progressive booklet confuses users | Deliver the merge as the booklet and the compiled edition as an explicit, labelled second file |
| Longer chunk budget truncates output | Watch `LLMOutputBudgetError`; raise `NOTE_MAX_OUTPUT_TOKENS` or revert the chunk size |
| More moving parts | Each change ships behind a setting, with the old path as the default until the timing line proves the win |

Tests to add: `wait`-path parsing and its fallback; format-choice costing table; parallel
download abort/limit/FloodWait paths; prompt-prefix stability (hash the rule block across
chunks); and a golden test that the same lecture still produces the same paragraph count.

---

## 7. Settings this plan touches

| Setting | Now | Recommended | Purpose |
| --- | --- | --- | --- |
| `STT_PROVIDERS` | `speechmatics,deepgram,…` | `deepgram,speechmatics` when Deepgram is configured | order *is* the latency policy |
| `SPEECHMATICS_MODEL` | `enhanced` | `melia-1` (or `standard`) | 24.1× → 206× / 43.9× |
| `SPEECHMATICS_BASE_URL` | global host | nearest regional host | Melia batch endpoints |
| `OPUS_CODEC_OPTIONS` (constant) | `audio`, `cl=0` (changed) | `audio`, `cl=0` | 106× → 355× |
| `AUDIO_FORMAT_POLICY` | `auto` | `auto` | choose from the measured rate |
| `TELEGRAM_DOWNLOAD_CONNECTIONS` | `1` | `1` → raise after the host measurement | pipelined range download |
| `NOTE_CONCURRENCY` | `3` | `6`, adaptive | halve map rounds |
| `NOTE_TTFB_TIMEOUT_SECONDS` | `0` | `25` | first-byte guard |
| `NOTE_HEDGE_AFTER_SECONDS` | `0` | `60` | one hedged attempt |
| `NOTE_CHUNK_CHARS` | `64 000` | `64 000`…`96 000` | rounds vs output budget |
| `NOTE_PROGRESSIVE_BOOKLET` | `0` | `1` after a look at the draft | perceived latency |
| `NOTE_STRUCTURED_OUTPUT` | `0` | `1` when the provider documents it | removes the retry path |
| `NOTE_OPENROUTER_THROUGHPUT` | `0` | `1` on an OpenRouter route | fastest upstream |
| `NOTE_COMPILE_PROVIDER` | empty | the fastest configured slug | compile off the slow route |

---

## 8. Implementation status

**Applied in code: phases 2, 3 and 4.** Phase 1 stays an operator decision (it changes
provider and model selection, which needs the E1 quality A/B); phase 0 is measurement, and
phase 5 is the measured-only fallback list.

| Phase | Change | Where | Default |
| --- | --- | --- | --- |
| 2 | `application=audio, compression_level=0` for Opus | `media_worker.OPUS_CODEC_OPTIONS` | on |
| 2 | Time-based container choice (`encode + bytes/egress`) | `media.choose_merge_format`, fed by the job's measured download rate | `AUDIO_FORMAT_POLICY=auto` |
| 2 | Concurrent 512 KiB byte-range download on one client | `bot.plan_download_ranges`, `bot._download_media_ranges` | `TELEGRAM_DOWNLOAD_CONNECTIONS=1` (opt-in) |
| 3 | `wait` long-poll on submit + inline transcript | `stt._speechmatics_wait_params`, `_speechmatics`, `_speechmatics_poll` | `STT_STATUS_LONG_POLL=25` |
| 4 | Cache-stable system prompt (context moved to the user message) | `notes.build_chunk_message`, `structure_document.map_chunk` | on |
| 4 | Map concurrency `min(NOTE_CONCURRENCY, chunks, 8)` | `notes._note_semaphore` | on |
| 4 | Response schema / `json_schema` | `notes.notes_response_schema`, `llm._build_request_for` | `NOTE_STRUCTURED_OUTPUT=0` |
| 4 | OpenRouter throughput routing | `llm._build_request_for` | `NOTE_OPENROUTER_THROUGHPUT=0` |
| 4 | Compile routed to a chosen provider first | `llm._ordered_routes`, `notes._compile_once` | `NOTE_COMPILE_PROVIDER=` (empty) |
| 4 | Progressive booklet (merge delivered before the compile) | `notes.structure_document(on_merged=…)`, `bot._progressive_booklet_sender` | `NOTE_PROGRESSIVE_BOOKLET=0` |

Why the two download/encode flags ship off or at `1`: neither can be verified in a sandbox
without Telegram, so the proven path stays the default. Turn them on after one measurement
on the host — `python -m scripts.perf_bench audio` plus the download timing line.

**Deliberately not implemented, with the reason:**

| Phase 2 item | Why it did not ship |
| --- | --- |
| *Encode segment i+1 while segment i uploads* | The upload is one `POST` of one file; there is nothing to overlap it with until the file itself is segmented, and segmenting is Phase 5 (measured-only). Implementing an overlap here would mean building the segmentation seam logic to hide a 20 s encode behind a 22 s upload — a 20 s win bought with a new failure mode |
| *Download at intake, bounded by a semaphore* | It moves the transfer ahead of the job slot, which changes the queue's admission and billing order (a download can then run for a job that is later rejected for credit), and it doubles peak disk use for queued jobs. The measured benefit is queue wait, not per-job latency; it is a scheduling change, not an I/O change |

Everything else in phase 2 is in place. The cumulative expectation is unchanged for the
parts that shipped; a deployment that turns the download flag on after measuring ≥2.5x
reaches the numbers in §5, and one that leaves it at `1` lands ~54 s higher on the 2 h
scenario (download 77 s instead of 24 s).

---

## 9. References

* Speechmatics models (turnaround, features, `melia-1` needs `language=multi`):
  <https://docs.speechmatics.com/speech-to-text/models>
* Speechmatics synchronous transcription (`wait` on POST/GET/transcript, 5 s default on GETs):
  <https://docs.speechmatics.com/speech-to-text/batch/synchronous>
* Speechmatics speed factors (standard 43.9×, enhanced 24.1×):
  <https://www.speechmatics.com/company/articles-and-news/speed-you-can-trust-the-stt-metrics-that-matter-for-voice-agents>
* Melia 1 at 206× and "a 60-minute recording in under 20 seconds":
  <https://www.speechmatics.com/company/articles-and-news/melia-fastest-speech-to-text-artificial-analysis>
* Melia-1 vs enhanced per-file wall clock (12.1 s vs 22.6 s):
  <https://novascribe.ai/how-accurate-is-speechmatics>
* Speechmatics batch limits (10 POST/s, 50 GET/s, 20 000 concurrent) and microbatching:
  <https://docs.speechmatics.com/speech-to-text/batch/limits> ·
  <https://www.speechmatics.com/company/articles-and-news/how-to-build-a-microbatching-workflow-with-the-speechmatics-api>
* Independent STT speed benchmark (Deepgram Nova-3 351×, Speechmatics enhanced 42×):
  <https://echosaw.com/curated-lists/best-speech-to-text-apis/>
* Deepgram callback (retries 10× / 30 s) — why a webhook needs public HTTPS:
  <https://developers.deepgram.com/docs/callback>
* Groq Whisper limits (216×, 25 MB free / 100 MB dev, 7 200 audio-s/h free tier):
  <https://console.groq.com/docs/speech-to-text>
* OpenRouter throughput routing (`provider.sort`, `:nitro`):
  <https://openrouter.ai/docs/guides/routing/provider-selection>
* Gemini implicit caching (2 048-token minimum on 2.5 Flash, cached reads ~0.1×):
  <https://ai.google.dev/gemini-api/docs/caching>
* Telethon downloads (512 KiB clamp, `iter_download` offsets) and MTProto pipelining:
  <https://docs.telethon.dev/en/stable/modules/client.html> ·
  parallel-download measurements: <https://github.com/julienfroidefond/stripstream-librarian/pull/85>
* Opus/AAC bitrate vs ASR accuracy: <https://team-connect.co.uk/info/audio-codecs-explained> ·
  <https://github.com/ibm-cloud-docs/speech-to-text/blob/master/audio-formats.md>
* Self-hosted ASR throughput on 2 CPU vCPUs (faster-whisper large-v3 int8 at 0.5× real time):
  <https://echosaw.com/curated-lists/best-speech-to-text-apis/>
* Nemotron 3.5 ASR (GPU-class streaming model, for context only):
  <https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b>
