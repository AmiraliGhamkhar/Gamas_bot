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

Environment: Linux, Python **3.11.2**, FFmpeg **7.0.2-static**.

- `python -m unittest discover -s tests -v`: **141 passed**, no skips when
  `FFMPEG_TEST_BIN` points to the smoke-test binary.
- Real FFmpeg checks: concatenate/resample clips and inserted silence; WAV/Opus
  conversion; reject a network playlist without fetching its URL.
- Real subprocess checks: timeout with output pipes, cancellation, and POSIX
  launcher-child cleanup.
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

FFmpeg was supplied locally by an optional `imageio-ffmpeg` development install
because OS package installation was unavailable in this sandbox. It is not a
new application dependency. Normally install FFmpeg/FFprobe from your OS packages;
CI installs them that way. Real-media tests skip explicitly if no FFmpeg binary
is available. The new cloud CI matrix has not yet run in GitHub; only Python
3.11 was executed locally.

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

## Remaining limitations and unverified behavior

- Live Telegram authentication/upload/delivery, provider API compatibility and
  billing, real LibreOffice conversion, Windows execution, and actual systemd
  startup require validation on the target host. FFprobe was exercised through
  stubs/parser tests, not a real local FFprobe executable.
- `MAX_CONCURRENT_JOBS` limits active work, not queue length or per-user usage.
  Pending tasks live in memory. Public deployments need admission/rate limits
  and resource controls; interrupted jobs require resubmission.
- Large presentation parsing still loads substantial package data. Unknown media
  durations cannot be completely bounded in advance, and output sizes are
  checked after conversion. Set host disk, memory and CPU limits.
- Protocol restrictions and the service unit are not a full parser sandbox.
  Untrusted documents may still access local files readable by the service
  account; isolate workers and keep native tools patched. The unit needs network
  access for Telegram/STT, so it does not isolate LibreOffice from the network.
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
