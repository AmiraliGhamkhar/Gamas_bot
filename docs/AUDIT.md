# Repository review — 2026-09-29 (Telegram rendering + Word booklet polish)

## Scope and result

A focused pass over the two user-visible deliverables — the Telegram message
renderer and the generated `.docx` booklet — after the cPanel audit below. Every
tracked module, both migrations, the tests and the docs were re-read for
consistency. **265 tests pass** (249 existing + 16 new). No live Telegram login
or paid provider call was made.

## Bugs found and fixed

| # | Area | Problem (reproduced) | Fix |
|---|---|---|---|
| 1 | Frontend (Telegram) | A heading or table header that was **itself bold** (`# **عنوان**`) was wrapped in a second `<b>`, emitting `<b><b>…</b></b>`. Telegram rejects a same-nested tag by failing the **entire message** ("can't parse entities"), so the whole booklet was lost — not just the heading. Reproduced on 5 distinct inputs. | New `strong()` / `strong_label()` helpers add a bold wrapper only when the already-rendered inline HTML contains no `<b>`, so a self-bold heading stays `<b>عنوان</b>`. Plain headers keep the previous `<b>a:</b>` output byte-for-byte. |
| 2 | Frontend (Telegram) | `progress_bar()` rounded 96–99% up to a **full** fill, then appended the emoji past the fixed width: the bar became 13 visible cells instead of 12 and visibly jumped in width mid-animation. | Clamp the fill to `width - 1` when a head emoji is used. The bar is now exactly `width` cells for every percentage, width and head combination (regression-tested across all of them). |
| 3 | Word document | `w:bidiVisual` was **appended** to `w:tblPr`, landing after `w:tblLook`. `CT_TblPr` is order-sensitive, so the generated file was schema-invalid and Word could report it as needing repair. | Insert `w:bidiVisual` before the first following element (`w:tblW`/`w:tblLook`/…), matching the schema. A test asserts the resulting order. |
| 4 | Word document | Truncating a long note title at 50 characters could leave a **trailing space or dot**, producing filenames like `جزوه - عنوان  - GMS-000001.docx` with a doubled separator and a near-empty extension. | `sanitize_filename_part()` re-strips ` ` and `.` after truncation. |

## Word booklet improvements

Beyond the fixes above, the booklet now reads properly across page breaks:

* **Repeating table headers** — `w:tblHeader` on the first row plus
  `w:cantSplit`, so a long comparison table keeps its column headings on every
  page instead of losing them after the first break.
* **Keep-with-next on headings** — every section heading, the summary heading
  and each "key points" box label carry `w:keepNext` + `w:keepLines`, so a
  heading can no longer be stranded alone at the foot of a page.

A new `WordLayoutTests` suite parses the produced `word/*.xml` parts and asserts
that `w:pPr`, `w:rPr`, `w:tblPr` and `w:trPr` children all follow the
`CT_PPrBase` / `CT_TblPrBase` / `CT_TrPrBase` sequences — the ordering class of
bug above cannot regress unnoticed.

## Not changed (deliberate)

* No table of contents or page-number field rework beyond what already exists;
  the footer PAGE field is correct and adding a TOC would require Word to
  refresh fields on open.
* Document structure, shading palette, fonts and the raw-text companion file
  are unchanged, so existing output stays comparable.

## Validation

`python -m unittest discover -s tests`: **265 passed** (0 failures, 0 skips).
`ruff check gamas_bot scripts tests passenger_wsgi.py --select E9,F`,
`compileall` and `pip check` pass. The generated document reopens cleanly in
python-docx and every `word/*.xml` part was verified against the OOXML element
sequences.

---

# Repository review — 2026-09-29 (full-stack audit + cPanel deployability)

## Scope and headline

Every tracked file was reviewed: all `gamas_bot/` modules, both SQL migrations,
`scripts/`, the tests, CI, the systemd unit, `.env.example`, README and the
Persian deployment guide. Baseline on entry: 220 tests green, `ruff` (E9,F) and
`pip check` clean.

* **There is no website and no frontend.** The project is a Telethon (MTProto)
  Telegram bot: one long-lived outbound connection, no HTTP server, no HTML/JS.
  The "frontend" is the Telegram chat UI (inline-button menus, progress
  messages, the generated `.docx`/`.txt` files). Those were audited as such.
* **cPanel is a poor natural fit** (it launches web requests, not permanent
  workers) and the previous docs declared shared hosting unsupported. It can
  work on plans that allow long-running processes, so this review added the
  missing pieces (below) and an honest checklist rather than pretending it is a
  standard web deploy.
* The **database layer is sound**: idempotent, transactional migrations with
  bookkeeping, `foreign_keys=ON`, WAL, serialised writes, crash-orphaned
  `pending/processing` rows are marked `failed` at start-up. No schema defect
  was found.

## Bugs found and fixed

| # | Area | Problem (reproduced) | Fix |
|---|---|---|---|
| 1 | Deploy / backend | `.env`, `data/bot.sqlite3`, the Telegram session and `data/tmp` were resolved from the **current working directory**. Cron, Passenger and `su -c` start in `$HOME` or `/`, silently creating a second empty database and a new Telegram login. | Relative paths and the default `.env` are anchored to the project directory (`config.PROJECT_ROOT`); `~` is expanded. |
| 2 | Deploy / backend | **No single-instance protection**, although the docs warn that two copies corrupt the session and the start-up cleanup deletes the other copy's live job folders. Any watchdog makes duplicates likely. | `instance_lock.py`: kernel `flock` on `<session>.lock`, released by the OS on any exit (tested with `kill -9`). A second process exits with code 3. |
| 3 | Backend | `SIGTERM` (what cPanel, `kill` and `pkill` send) killed the process **without** cleanup: no job status update, no Telegram disconnect. Only `SIGINT` was graceful. | `SIGTERM` now cancels the main task → same graceful shutdown as Ctrl+C. |
| 4 | Backend / output | Any XML-illegal character (NUL, `\x01`…`\x08`, VT `\x0b` from PowerPoint soft line breaks, lone surrogates) in a note, slide or transcript made python-docx raise `ValueError`, so the user **lost the Word deliverable** and got only a chat fallback. Filenames could contain control characters too. | `xml_safe()` applied at every text/run/property write; filename sanitiser strips all control characters. |
| 5 | Frontend (Telegram) | Crossed emphasis such as `**a *b** c*` rendered `<b><i></b></i>`; Telegram rejects the **entire** message ("can't parse entities"). Reproduced in 191 of 30 000 fuzzed inputs. | Improperly nested emphasis falls back to literal text; fuzz now 0/30 000. |
| 6 | Frontend (Telegram) | Admin `/ban ²` (superscript digit) passed `str.isdigit()` but crashed `int()`; absurdly long numbers overflowed SQLite. | `_parse_user_id()` (decimal only, ≤15 digits, Persian digits accepted). |
| 7 | Deploy | Hosts that block Telegram's MTProto ports had no workaround. | Optional `TELEGRAM_PROXY` (`socks5://`, `socks4://`, `http://`); `python-socks[asyncio]` added to requirements. |

## Added for cPanel

* `docs/DEPLOY_CPANEL.md` — host checklist, Python-App/venv setup, cron,
  operations and troubleshooting.
* `scripts/cpanel_preflight.py` — checks Python version, glibc (PyAV/lxml wheels
  need ≥ 2.28), packages, project-inside-`public_html`, `.env` permissions,
  writable dirs and free disk, `flock` support, outbound reachability of
  Telegram and each configured provider, and the media worker.
* `gamas_bot/launcher.py` + `scripts/ensure_running.py` — cron watchdog: cheap
  lock check, spawn throttling (45 s), detached start, default rotating log file.
* `passenger_wsgi.py` — optional "Setup Python App" entry point: JSON status
  only (`running` / `starting` / `error`), serves only `/` and `/health`, and
  doubles as a second watchdog when polled by an uptime monitor.

## Not changed / remaining risks (need the maintainer's decision or a real host)

* **Not verified on a live cPanel account.** Whether the host kills long-running
  processes, exposes Python 3.11+, or allows outbound 443 to Telegram can only be
  proven with the preflight on the target plan.
* STT and note-API calls (`aiohttp`) do **not** use `TELEGRAM_PROXY` or
  `HTTP(S)_PROXY`; those hosts must be directly reachable. `trust_env` was left
  off deliberately (it would also route a local Ollama/vLLM gateway via a proxy).
* No per-user rate limit or queue bound; pending jobs live in memory and are lost
  on restart (users must resend). Public bots need admission control.
* SQLite keeps transcripts and notes indefinitely (no retention policy).
* `ruff --select ASYNC240` flags a few blocking `Path.stat()/open()` calls inside
  async STT functions; they are short local-disk calls and were left alone.
* Prior-review limitations below (Windows unverified, remote Speechmatics jobs not
  cancelled on local timeout, no license file) still apply.

## Validation

`python -m unittest discover -s tests`: **249 passed** (220 existing + 29 new in
`tests/test_cpanel_deploy.py`). `ruff check gamas_bot scripts tests passenger_wsgi.py
--select E9,F` and `pip check` pass. Live checks: launcher started a detached
bot from a foreign working directory and created all state under the project
directory; a second bot exited with code 3; the preflight ran green on Linux
(glibc 2.36). No live Telegram login or paid provider call was made.

---

# Repository review — 2026-09-28 (accuracy-first pipeline hardening)

## Follow-up verification — 2026-09-28

A post-review pass over the current checkout found and fixed one issue in the
Speechmatics result path: after downloading and logging the complete `json-v2`
transcript, `_speechmatics()` made a second identical request and parsed that
response instead. It now parses the already-downloaded payload once, uses that
same payload for confidence metrics, and rejects malformed top-level response
shapes with a sanitized provider error. The regression test asserts exactly one
transcript download per completed job.

Validation on this checkout: `python -m unittest discover -s tests -v` — **220
passed**; `ruff check gamas_bot scripts tests --select E9,F`,
`python -m pip check`, `compileall`, and `git diff --check` pass. These counts
are current for this follow-up; the historical result figures in the sections
below describe their original review snapshots.

## Scope and result

Focused re-audit of the **transcription → note-generation** path (`stt.py`,
`structuring.py`, `config.py`, `bot.py` orchestration, `benchmark_stt.py`) with
accuracy as the primary constraint and no behaviour changes to the working
media/presentation/database pipeline. **Result: 190 tests pass** (up from
164); no paid provider calls were made.

## Root-cause findings and fixes

| Problem | Root cause | Fix | Files |
|---|---|---|---|
| Gemini `HTTP 400` was undiagnosable in production. | Google answers bad/restricted keys and bad request fields with `400 INVALID_ARGUMENT` (e.g. `details[].reason: API_KEY_INVALID`), never 401, while `_structure_chunk` read and **discarded** the error body and logged only `status=400 request_id=unknown` (Google does not send `x-request-id`). | Parse the error body defensively and log only the provider's **structured metadata** (`status/type/code`, bounded `message`, `details[].reason`) with the configured key redacted and control characters stripped; attach the same sanitized detail to the raised `StructuringError`. Raw bodies are still never logged. | `structuring.py` |
| `NOTE_API_MODEL=models/gemini-…` produced `…/models/models%2F…:generateContent` → guaranteed `400`. | Model name was URL-quoted verbatim into the path. | Strip a leading `models/` prefix before quoting. | `structuring.py` |
| Gemini safety-blocked responses surfaced as "empty/invalid response". | `candidates[0]` indexing raised `IndexError` before `promptFeedback.blockReason` was read — a real possibility for medical lecture content. | Missing candidates now raise with the sanitized `blockReason`; the raw-material fallback still applies. | `structuring.py` |
| Speechmatics ran the **throughput** tier (`"model": "standard"`) for an accuracy-first Persian bot. | Hardcoded tier; Speechmatics documents `enhanced` as its highest-accuracy model and supports a native 20k-term custom dictionary. | `SPEECHMATICS_MODEL` (default `enhanced`) and `SPEECHMATICS_ADDITIONAL_VOCAB` (native `additional_vocab`; no LLM layer). A contract without the enhanced tier rejects submission and the existing fallback keeps jobs recoverable. | `stt.py`, `config.py` |
| STT catalogue not extensible (two hardcoded names, ad-hoc 1 GB special case). | Provider list, key checks, size routing inline in `transcribe()`/`_provider_key()`/validation/benchmark. | Small frozen `STTProvider` registry `(availability, attempt, max_upload)`; new optional `openai_compatible` STT engine (OpenAI/Groq `whisper`, local vLLM/Ollama gateways via `POST /audio/transcriptions`); per-engine size caps generalize the 1 GB rule. Default two-provider behaviour, fallback chain and guard rails are unchanged. | `stt.py`, `config.py`, `scripts/benchmark_stt.py` |

## Deliberately not changed (accuracy constraints)

- **No audio chunking for STT.** Files are attempted whole per engine and, above
  a provider's direct-upload cap, routed to another configured engine.
  Chunking would lose word context at every boundary; Speechmatics/Deepgram
  batch APIs accept multi-hour files natively. This is now enforced in one
  place (`STTProvider.max_upload`) instead of an inline 1 GB branch.
- Note-generation chunking stays **sequential** at sentence boundaries with
  chronological `بخش N` joining; long decks keep the complete outline
  behaviours from the previous review.
- Raw-transcript safety net in `bot.py` (structuring failure ⇒ full raw
  transcript/outline delivered and stored) is untouched and re-verified by a
  new bot-level regression test covering a Gemini `HTTP 400`.
- No embeddings/RAG/normalization layer; prompts forbid inventing facts.

## Tests added (`tests/test_provider_robustness.py`, 26 tests)

Gemini/`400` handling (sanitized `API_KEY_INVALID` visible, key redaction,
HTML-body suppression, non-retry), OpenAI-compatible note errors + `429`
retry, `models/` normalization, safety `blockReason`, STT provider
switching/fallback chain incl. `openai_compatible`, sparse-file size routing
(1 GB skip, >all-caps fail-fast), OpenAI STT request shape/privacy, Speechmatics
enhanced + vocab config, config validation, chunk ordering/completeness on
mixed Persian-English medical text (numbers, `Metformin`, `HbA1c`),
benchmark three-engine coverage, and raw-transcript delivery/storage after a
note-generation `HTTP 400`.

## Validation

`python -m unittest discover -s tests`: **190 passed** (0 failures, 0 skips).
`ruff check gamas_bot scripts tests --select E9,F`: passed.
`python -m pip check`, `compileall`: passed.

---

# Repository review — 2026-09-28: system media/office dependencies removed

## Scope and result

Full re-audit of every tracked file (application modules, SQL migrations,
tests, benchmark script, `.env.example`, systemd unit, GitHub workflow, README
and Persian deployment guide), followed by replacement of the external
**FFmpeg / FFprobe / LibreOffice** runtime dependencies with Python packages.

Goal: the bot must run on hosts that have no `ffmpeg`, `ffprobe` or `soffice`
binary installed (cPanel, locked-down containers, minimal VPS images), while
preserving formats, security limits, timeouts, cleanup and error handling.

**Result: 164 tests pass** (up from 141) with no system media binaries present
on the machine, `pip check` and `ruff check gamas_bot scripts tests --select E9,F`
are clean. No live Telegram or paid provider requests were made.

## What replaced the binaries

| Old dependency | Replacement | Why this option |
|---|---|---|
| `ffprobe` stream probing | **PyAV** (`av` wheel) inside `gamas_bot/media_worker.py` | Verified API: `av.open(..., options={"protocol_whitelist": "file,pipe"})`, `container.duration`/`stream.duration` reporting, `Codec.canonical_name` (equals ffprobe's `codec_name`, e.g. `mp3` not `mp3float`). |
| `ffmpeg` audio extraction/transcode | **PyAV** decode → `AudioResampler` (s16/mono, 16 kHz or 48 kHz) → `pcm_s16le` WAV or `libopus` 32 kbit/s Opus in Ogg | Verified encoders ship in the wheel (`pcm_s16le`, `libopus`, `libmp3lame`, `libx264`, …); `codec_context.options = {"application": "voip"}` reaches `avcodec_open2` exactly like the old `-application voip` flag. |
| `ffmpeg` multi-clip merging | **PyAV** per-input resamplers + zero-sample padding between clips | Mirrors the old `aresample`/`apad=pad_dur`/`concat` graph: mono, target rate, 0.5 s (`PPTX_SILENCE_SECONDS`) between clips, none after the last. |
| `soffice` legacy `.ppt → .pptx` | **ppt2pptx** (pure Python, MS-PPT/CFB parser) | Mandated equivalent; converts `.ppt`/`.pps`/`.pot` (extension-agnostic), never executes macros, respects input-size limits, reports lossy features as structured warnings. |
| ODP/OTP via LibreOffice | **Explicit rejection** with a user-facing message asking for `.pptx` | No verified pure-Python ODP→PPTX converter with acceptable fidelity exists; a clean unsupported-format error is better than a fake conversion. |

`pymediainfo` was evaluated and rejected: although recent wheels bundle the
native library, PyAV alone covers probing *and* transcoding with one
dependency, and its FFmpeg libraries are the same code family the previous
commands used, which keeps format coverage closest to the old behaviour.
`pydub`/`moviepy` were not used because they still shell out to FFmpeg.

## Architecture: a supervised Python worker

All media operations run in a dedicated child process,
`python -m gamas_bot.media_worker`, started through the existing
`media.run_command` wrapper:

- **No runtime path invokes `ffmpeg`, `ffprobe` or `soffice`** — every command
  vector starts with `sys.executable -m gamas_bot.media_worker`
  (regression-tested in `test_media_runtime.py`, `test_audit.py`).
- Timeouts (`MEDIA_TIMEOUT_SECONDS`, `PPT_CONVERT_TIMEOUT_SECONDS`),
  cancellation, POSIX process-group kill, pipe draining, non-shell execution
  and bounded stderr reporting are unchanged.
- A native crash or hang inside the media libraries takes down only the
  worker, not the bot — the isolation property the old subprocess tools gave.
- Each input is opened with the `file,pipe` protocol whitelist, so a disguised
  playlist still cannot fetch network URLs
  (`test_network_playlist_is_rejected_without_fetching_url` asserts the
  refusal message).
- FFmpeg-level error records are routed to the worker's stderr head so the
  first 300 characters the parent reports contain the real cause (e.g.
  `Protocol 'http' not on whitelist 'file,pipe'!`).

## Configuration changes

- Removed: `FFMPEG_BIN`, `FFPROBE_BIN`, `SOFFICE_BIN`, `SOFFICE_TIMEOUT_SECONDS`.
- Added: `MEDIA_TIMEOUT_SECONDS` (default 3600) and
  `PPT_CONVERT_TIMEOUT_SECONDS` (default 600).
- Backward compatible: `FFMPEG_TIMEOUT_SECONDS` and `SOFFICE_TIMEOUT_SECONDS`
  are still read as fallbacks, and leftover `*_BIN` lines in existing `.env`
  files are silently ignored (verified by tests in this review).
- Startup now runs `media_worker check` (reports installed `av`/`ppt2pptx`
  versions) instead of probing binaries on `PATH`.

## Format support and fidelity notes

- Audio/video coverage follows the FFmpeg libraries bundled in the `av` wheel:
  MP3, M4A/AAC, WAV, OGG/Opus/Vorbis, FLAC, WMA, AMR, MP4/MKV/MOV/AVI/WebM,
  etc. `PASSTHROUGH_CODECS` still matches ffprobe-era canonical names, so
  normal uploads are still not re-encoded.
- Output behaviour is unchanged: mono 16 kHz PCM WAV, or 48 kHz Opus
  (32 kbit/s, voip) when the WAV-size estimate would exceed
  `PPTX_WAV_LIMIT_BYTES`/`MAX_FILE_SIZE_BYTES`.
- Legacy PPT conversion keeps slide text, speaker notes, slide order and
  macros-not-executed behaviour, with ZIP/packaging safety enforced by
  `ppt2pptx` limits plus the bot's own `MAX_FILE_SIZE_BYTES` check.
  ppt2pptx documents lossy legacy features (embedded media playback,
  animations, some OLE objects) as structured warnings; they are logged, not
  silently dropped. Embedded narration media inside old `.ppt` files is no
  longer carried into the converted `.pptx` (LibreOffice used to copy it);
  this is an accepted, documented limitation of the mandated `ppt2pptx`
  approach, covered by `test_embedded_media_deck_converts_with_diagnostics`.
- ODP/OTP are classified as `unsupported` and rejected at submission time
  with an explanatory message; `PPTX_LEGACY_ENABLED=false` still disables
  PPT/PPS/POT conversion entirely.

## Tests added or reworked

- Real-media smoke tests no longer skip: probing (WAV/MP3/MP4 with and
  without audio), extraction to WAV/Opus, merging with resample + silence
  math, corrupt-input errors, bounded error messages, worker timeout kill,
  protocol-whitelist refusal, and the worker dependency self-check all run
  on every CI job without installing anything.
- `PPT → PPTX` coverage uses committed real PowerPoint 97–2003 fixtures
  (`tests/fixtures/*.ppt`, MIT, from the ppt2pptx corpus) for `.ppt`, `.pps`
  and `.pot`, plus garbage-input failure and explicit ODP rejection.
- Native PPTX variants, ZIP safety limits, clip limits, duration caps,
  cancellation/cleanup and security restrictions keep their previous tests,
  now backed by real generated media instead of stub binaries.
- A regression test runs a full video job with `PATH` pointing at a
  non-existent directory, proving no system binaries are needed.
- CI no longer installs `ffmpeg` via apt.

## Validation performed

Environment: Linux, Python **3.11.2**, no `ffmpeg`/`ffprobe`/`soffice`
binaries on the host, `av` **18.1.0**, `ppt2pptx` **0.4.2**.

- `python -m unittest discover -s tests`: **164 passed**, 0 skipped.
- `python -m pip check`: no broken requirements.
- `ruff check gamas_bot scripts tests --select E9,F`: passed.
- `python -m compileall -q gamas_bot scripts tests`: passed.
- Worker CLI exercised directly (`check`, `probe`, `extract`, `merge`,
  `convert`) including the HTTP-playlist whitelist refusal.

## Remaining limitations

- The media worker is still native code parsing untrusted bytes; it is
  process-isolated and protocol-restricted but not a sandbox. Keep `av`
  updated and apply host resource limits.
- ppt2pptx fidelity on exotic legacy decks is bounded by its documented
  feature set (see its README); warnings are logged for omitted content.
- Windows/macOS application execution is documented but validated only on
  Linux CI, as before.

---

# Repository review — 2026-09-27

## Scope and result

Reviewed the tracked application modules, SQL migrations, operational script,
tests, environment example, deployment unit, GitHub workflow, README and Persian
deployment guide. Changes focus on reproducible correctness, privacy and
operational issues; this is not a certification that the project is bug-free.

The original **104 tests passed**. New regression tests reproduced failures that
were not covered by that suite. After fixes, **141 tests pass**, including three
real-FFmpeg smoke tests. No live Telegram or paid provider requests were made.

## Fixes

| Area | Problem | Change |
|---|---|---|
| First startup | Telethon opened its SQLite session before the parent directory existed. | Create the session directory before constructing the client. Apply the restrictive POSIX umask before file logging as well. |
| Telegram delivery | Oversized HTML lines were split after escaping, breaking entities and changing visible text. Literal internal code placeholders could crash rendering. | Split decoded text, then escape each page; strip NUL placeholders from input. Validate nonpositive split limits. |
| Broadcasts | A flood wait on a later page resent already-delivered pages. | Retry only the failed page. |
| Admin interaction | A pending private broadcast could consume an administrator’s unrelated group message or unknown command. | Limit admin commands/callbacks to private chats; group messages do not answer prompts. Menu navigation and cancellation clear pending actions. |
| SQLite writes | An invalid clip row could leave earlier updates pending, then an unrelated write committed them. | Serialize writes in explicit transactions and roll back on errors/cancellation. |
| Migrations | DDL could remain partially applied; failed database startup leaked its connection. Comment removal could join SQL tokens. | Transactional migrations, close-on-failure, idempotent open, whitespace-preserving comment removal and trigger-aware statement boundaries. Existing duplicate-column recovery remains supported. |
| Long presentations | Outline truncation silently lost later slides, including in raw fallback. | Preserve the full extracted outline and chunk it for note generation. Short outlines are still repeated with narration chunks. |
| Presentation variants | python-pptx’s convenience loader rejects slideshow/template content types despite advertised support. | Read their presentation parts directly, with mappings for macro-enabled slideshow/template variants. Native parsing does not execute macros. |
| Presentation metadata | Disabling slide text made the recorded slide count zero. Unnamed legacy files were saved with a native extension. | Count actual slide parts independently of text extraction; choose the appropriate legacy suffix. Add missing template MIME types. |
| Extraction cleanup | Cancelling a thread-backed extraction let it keep writing after its work directory was removed. | Wait for extraction to finish before propagating cancellation and cleaning files. |
| Media commands | Timeout cleanup could leave launcher children running or hang on output pipes. Concatenation did not reset stream timestamps. | POSIX process-group termination, pipe draining, and per-input timestamp reset. Windows still only explicitly kills the direct process. |
| Media safety | A disguised playlist could ask FFmpeg/FFprobe to fetch network resources. | Whitelist local `file` and `pipe` protocols for every media input. This is not a full filesystem sandbox. |
| Media estimates | Inserted silence was absent from WAV-size estimates; one unknown clip duration disabled checks on known durations. | Include silence in size estimates and enforce the cap against the sum of known durations. Report narration duration separately. |
| Configuration/probing | Nonfinite time values and malformed probe JSON caused invalid processing or unexpected exceptions. | Reject nonfinite environment times; validate probe structure and ignore nonfinite durations. Ignore invalid confidence ranges. |
| STT timeout | Socket timeouts/poll checks did not bound the complete upload/request lifecycle. | Bound every provider attempt with the configured job timeout; fallback gets its own attempt budget. |
| Provider privacy | STT response bodies and client exceptions could leak lecture text/URLs to logs; Gemini keys were in query parameters. | Keep sanitized STT status/type errors, omit provider error bodies, suppress sensitive client exception chains and authenticate Gemini with a header. |
| Incomplete notes | Explicit output-token truncation was accepted as a finished booklet. | Detect token-limit finish reasons for supported providers and use the existing raw-material fallback. |
| Benchmark | Large files routed to Deepgram could be labeled as Speechmatics measurements. Nested output directories failed. | Reject unsupported Speechmatics samples in that row, verify the actual engine and create output parent directories. |
| Deployment | Account creation populated the intended checkout directory before `git clone`. The unit’s writable-directory comment overstated `ProtectSystem=full`. | Document `--no-create-home`; use `ProtectSystem=strict` with `data/` as the writable exception. |
| Repository hygiene | SQLite WAL/journal sidecars outside `data/` were not ignored. | Ignore database/session sidecars and the linter cache. |
| GitHub Actions | Unrelated RDP workflow contained a fixed administrator password, disabled NLA and had no bot tests. | Remove it and add read-only-permission Linux test CI for Python 3.11–3.13. |

No new SQL migration or runtime dependency was needed. Existing database schemas
remain compatible. Requirements were installed and audited rather than changed
without evidence of a dependency problem.

## Validation performed

Environment: Linux, Python **3.11.2** (historical run, before the 2026-09-28
dependency removal above).

- `python -m unittest discover -s tests -v`: **141 passed** at that time, no
  skips when the optional smoke-test FFmpeg binary was provided.
- Real FFmpeg checks (now superseded by the always-on PyAV worker tests):
  concatenate/resample clips and inserted silence; WAV/Opus conversion;
  reject a network playlist without fetching its URL.
- Real subprocess checks: timeout with output pipes, cancellation, and POSIX
  launcher-child cleanup (retained, still passing).
- Synthetic package/SQLite tests: presentation variants, complete long outlines,
  rollback after invalid data, migration rollback/replay and parser boundaries.
- Mock provider/Telegram tests: timeout fallback, private admin isolation,
  broadcast retries, truncated responses and error sanitization.
- `ruff check gamas_bot scripts tests --select E9,F`: passed.
- `python -m compileall -q gamas_bot scripts tests`: passed.
- `python -m pip check`: no broken requirements.
- `pip-audit -r requirements.txt`: no known vulnerabilities in resolved
  requirements at review time. This is not a guarantee about unknown issues.
- `git diff --check`: passed.

At that time FFmpeg was supplied locally by an optional `imageio-ffmpeg`
development install because OS package installation was unavailable in this
sandbox; it was not an application dependency. Since the 2026-09-28 review
above, **no FFmpeg/FFprobe binary is used or expected anywhere** — neither for
the application nor for the tests — and CI installs none.

## Deployment actions

1. Back up the database, session and environment with the service stopped.
2. Install updates and run the tests in your virtual environment.
3. If using the supplied systemd unit, reinstall it and run
   `systemctl daemon-reload`. Keep database, session, temp and file-log paths
   under `data/`, or add explicit writable paths to your customized unit.
4. Restart one worker and test a short audio, a video, a slides-only presentation
   and a real legacy deck against your configured accounts.
5. Verify model availability, Persian support, API quotas and upload limits in
   your provider account. Set low concurrency initially.
6. If the removed RDP workflow was ever used, stop remaining runner sessions and
   review/revoke their Tailscale access. Rotate the published password anywhere
   it was reused. Removing the workflow does not erase Git history or rotate
   external credentials.

## Remaining limitations and unverified behavior (2026-09-27 state)

- Live Telegram authentication/upload/delivery, provider API compatibility and
  billing, Windows execution, and actual systemd startup require validation on
  the target host. FFprobe was then exercised through stubs/parser tests; the
  2026-09-28 review replaced it entirely with real PyAV-backed probe tests.
- `MAX_CONCURRENT_JOBS` limits active work, not queue length or per-user usage.
  Pending tasks live in memory. Public deployments need admission/rate limits
  and resource controls; interrupted jobs require resubmission.
- Large presentation parsing still loads substantial package data. Unknown media
  durations cannot be completely bounded in advance, and output sizes are
  checked after conversion. Set host disk, memory and CPU limits.
- Protocol restrictions and the service unit are not a full parser sandbox.
  Untrusted documents may still access local files readable by the service
  account; isolate workers and keep native libraries patched. The unit needs
  network access for Telegram/STT, so it does not isolate the media worker
  from the network (the worker itself only whitelists local protocols).
- Long decks are chunked without precise narration-to-slide timestamps. Retaining
  all input does not guarantee an LLM preserves every fact or cross-chunk context.
- A local STT timeout does not cancel/delete a remote Speechmatics job. Review
  provider retention policies and possible costs for abandoned requests.
- SQLite retains user information, transcripts and notes. There is no automatic
  retention/deletion policy. Backups and logs also need access controls.
- No project license has been supplied; the maintainer must choose one.

## Repository coverage

- `gamas_bot/`: configuration, entry point, handlers, progress, logging,
  persistence, media/presentation pipeline, speech-to-text and note generation.
- `migrations/`: both existing schemas reviewed and replay/rollback tested;
  migration files did not need modification.
- `scripts/`: benchmark routing/reporting fixed; text normalization reviewed.
- `tests/`: existing suite retained; `test_audit.py` and
  `test_media_runtime.py` add regression and native-tool checks.
- `deploy/`, `.github/`, `.env.example`, `.gitignore`, `requirements.txt`:
  operational/security review and changes described above.
- `README.md`, `docs/DEPLOY_FA.md`: updated setup, timeout semantics, private
  administration, chunking/fallback behavior, testing and security limitations.
