# Changelog

## Unreleased

- Performance defaults now ship on (Phase 1 order change, Phase 2 note knobs, and the
  regression guard; measurements in [PERFORMANCE_PLAN.md](PERFORMANCE_PLAN.md)):
  - **STT provider order:** `STT_PROVIDERS` now defaults to
    `deepgram,speechmatics,openai_compatible`. The list *is* the failover policy — the first
    provider with a key serves the job — so leading with Speechmatics made the slowest
    reachable engine the default for every upload. Speechmatics stays second, so a deployment
    whose only key is `SPEECHMATICS_API_KEY` behaves exactly as before and a Deepgram outage
    degrades to the previous path. `SPEECHMATICS_MODEL` and `SPEECHMATICS_BASE_URL` are
    deliberately unchanged: switching to `melia-1` is a Persian-accuracy decision, not a
    speed one.
  - **`--check` guard:** a new advisory warning fires when the provider that will actually
    run is Speechmatics with `enhanced`/`standard` while Deepgram is also configured, naming
    the fix. It is computed by `config.first_configured_stt_provider`, which `stt` now also
    uses for connection pre-warming so the warning and the failover chain cannot disagree.
  - **Note-phase guards on by default:** `NOTE_TTFB_TIMEOUT_SECONDS=25` (a silent route is
    abandoned after 25 s instead of waiting out the whole request timeout) and
    `NOTE_HEDGE_AFTER_SECONDS=60` (the second provider starts if the first has not finished;
    at most one extra in-flight POST, so it can spend extra quota). Both are below the
    request deadline on purpose, and `0` still disables either one.
  - **Note knobs on by default:** `NOTE_CONCURRENCY=6` (still bounded by chunks and a cap of
    8, so a short lecture is unaffected), `NOTE_STRUCTURED_OUTPUT=1` (schema-constrained
    output; the field drops itself and the request is retried once if a provider rejects it —
    pinned by a test that also covers the schema + streaming combination),
    `NOTE_OPENROUTER_THROUGHPUT=1` (OpenRouter route only), `NOTE_PROGRESSIVE_BOOKLET=1`
    (draft booklet before the editorial compile). The draft is now gated by
    `notes.compile_will_run`, the same predicate `_compile_pass` uses: if the compile is
    disabled, the merge is below the compile floor, or the context window leaves no budget,
    no draft is sent — it would be byte-identical to the final booklet. Turning that switch
    on by default would otherwise have delivered two identical Word files for every short
    lecture.
  - **Docs/tests:** `.env.example`, `CONFIGURATION.md`, `STT_PROVIDERS.md` and `PROVIDERS.md`
    show the new defaults, and `test_config_consistency` now fails if `.env.example`
    documents a value the code does not ship.

- Performance plan phases 2–4 (measurements and trade-offs in [PERFORMANCE_PLAN.md](PERFORMANCE_PLAN.md)):
  - **I/O:** Opus now uses `application=audio, compression_level=0` (~3x faster encode than the shipped level for ~7 % more bytes, and the hybrid path keeps more of the speech band than `voip`). `AUDIO_FORMAT_POLICY=auto` picks PCM WAV or Opus from *encoder CPU + transfer time* using the rate the job's own Telegram download measured, instead of from size alone. `TELEGRAM_DOWNLOAD_CONNECTIONS` (default `1`) can fetch one file as concurrent 512 KiB byte ranges on the same client, with automatic single-stream fallback on any failure.
  - **STT call shape:** `STT_STATUS_LONG_POLL` (default `25` s) sends `wait` on the Speechmatics submit and status reads; `POST /jobs?format=json-v2` returns the finished transcript inline, so a job that finishes inside the window costs one request instead of submit + poll + transcript download. `created` still polls, and `done` without the embedded document still fetches the transcript once.
  - **Note layer:** the per-chunk orientation block moved from the system prompt to the user message, so the 4 200-token rule block is byte-identical across chunks and can be served from a provider prefix cache. Map concurrency is now bounded by `min(NOTE_CONCURRENCY, chunks, 8)`. New opt-in flags: `NOTE_STRUCTURED_OUTPUT` (response schema / `json_schema`, dropped automatically if a provider rejects it), `NOTE_OPENROUTER_THROUGHPUT` (`provider.sort=throughput` on the OpenRouter route only), `NOTE_COMPILE_PROVIDER` (which route serves the editorial compile first), `NOTE_PROGRESSIVE_BOOKLET` (deliver the audited merge as a draft while the compile runs).

- Pipeline-wide speed and performance improvements across all four stages:
  - **Telegram transfers and delivery:** added `cryptg` for C-accelerated MTProto AES-IGE transfers; configured Telethon `get_appropriated_part_size` to use 512 KiB chunks for all positive file sizes; offloaded `.txt`, `.docx` and Office file writes via `asyncio.to_thread`; overlapped transcript database persistence with file writes and raw preview sending with credit settlement.
  - **STT (`POST` and `GET`):** buffered audio upload streams to 1 MiB and response reads to 256 KiB; switched response and job-config JSON handling to `orjson`; staged Speechmatics word/punctuation merging in mutable entries before building `Word` objects; added adaptive Speechmatics status polling (`0.25x` → `0.5x` → `0.75x` → `1.0x` of `STT_POLL_INTERVAL`); pre-warmed both the primary LLM and primary STT endpoints concurrently when creating the shared HTTP session.
  - **Audio extraction, probing and presentations:** eliminated the redundant second `media_worker probe` subprocess call in `_process_media` when the source probe already reported duration; enabled multi-threaded PyAV decoding (`thread_type="AUTO"`) with safe `flush_buffers()` cleanup; tuned VoIP Opus encoding (`compression_level="5"`) and reused pre-zeroed silence frames; kept the `.pptx` `ZipFile` archive open across slide extraction with per-slide `.rels` caching; added binary-search ($O(W \log C)$) windowing in `align_narration_to_slides`.
  - **LLM, transcript and note generation:** added an LRU-cached `SourceEvidenceIndex` (`build_source_index`) with $O(1)$ signal lookup and pre-normalized `STANDARD_EQUIVALENTS` across `gate_notes`, `audit_notes`, `validate_table`, `validate_tip`, `sentence_coverage` and `evidence_windows`; parallelized grouped compile passes (`_compile_in_groups`) under `NOTE_CONCURRENCY` while preserving section order; extended `NOTE_TTFB_TIMEOUT_SECONDS` streaming to Gemini routes (`:streamGenerateContent?alt=sse`); parsed SSE lines directly from `bytes` via `orjson`; pre-compiled Persian translation tables in `textnorm.py` and regex token counters in `transcript.fingerprint`.

- Large-audio hardening (a 3–6 hour lecture is now the sized-for case): the editorial compile and the repair pass no longer switch off when the merged booklet exceeds one request — they run in bounded whole-section groups and merge back by heading (`NOTE_QUALITY.md`); a failed raw `.txt` upload of a long transcript is re-sent as bounded parts instead of being refused (`RAW_FALLBACK_FILE_CHARS`, `RAW_FALLBACK_MAX_FILES`); the STT chain shares one deadline and never re-uploads the file on a transient error (only idempotent status reads retry); unknown-duration uploads are charged against the conservative floor at intake; downloads have a stall watchdog and a temp-disk floor (`DOWNLOAD_STALL_SECONDS`, `TEMP_MIN_FREE_BYTES`); every provider transfer has its own deadline and response-body ceiling (`STT_TRANSFER_TIMEOUT_SECONDS`, `MAX_STT_RESPONSE_BYTES`); queue dispatch is round-robin across users; finished transcripts can expire (`TRANSCRIPT_RETENTION_DAYS`, audit action `transcript_expired`); `--check` warns about a `MEDIA_TIMEOUT` too short for a multi-hour video. Regression suite: `tests/test_hardening_large.py`.

- LLM request-layer performance work (quality gate untouched): per-request TTFB/elapsed timing in the provider usage log; tuned shared HTTP connector, orjson response parsing with a stdlib fallback, and quota-free connection pre-warm; opt-in (default-off) streaming TTFB failover guard and two-provider hedged racing; concurrent (order-preserving) Office deck planning. New flags: `LLM_CONNECTOR_*`, `LLM_PREWARM`, `NOTE_TTFB_TIMEOUT_SECONDS`, `NOTE_HEDGE_AFTER_SECONDS`, `OFFICE_AI_CONCURRENCY` — unset, behaviour is exactly as before.

- Hardened callback authorization, file-download limits, receipt validation, ZIP extraction, admin cooldowns, broadcasts and cPanel launcher locking.
- Added regression tests for the security fixes.
- Rewrote setup and operations guides for Windows PowerShell and cPanel shared hosting.

## 3.0.0 — 2026-10-11

- Rebuilt the bot around a bounded job queue, SQLite billing ledger and recoverable reservations.
- Added ordered speech-to-text and note-provider failover, raw-transcript-first delivery and RTL Word booklets.
- Added PowerPoint narration support and the free text-to-Office menu.
- Added cPanel cron/Passenger launch support. The repository is a single-process app; it is not horizontally scalable without a job broker.

For older implementation history, use Git history.
