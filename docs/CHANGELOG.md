# Changelog

## Unreleased

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
