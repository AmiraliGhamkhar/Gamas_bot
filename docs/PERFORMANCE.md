# Performance and hosting limits

Start with defaults; measure on the actual account before raising them. Application
ceilings do not guarantee that a cPanel plan can sustain the workload.

| Resource | Default |
| --- | --- |
| Active / queued / per-user jobs | 3 / 8 / 2 |
| Downloads / media / uploads | 2 / 1 / 2 |
| Speechmatics / Deepgram / OpenAI-compatible slots | 2 / 4 / 2 |
| Note-map concurrency / primary LLM / fallback LLM slots | 3 / 4 / 4 |
| Media upload / duration | 128 MiB / 2 hours |
| Temporary reserve | 64 MiB plus working-copy checks |
| Extracted text / document note-export source | 5000000 / 250000 characters |
| Office builds | 2 |

Independent stages overlap network work without running many heavy media jobs.
Available fallback capacity receives overflow immediately; waiting is bounded when
all eligible routes are full/cooling down.

## If the host overloads

1. Inspect cPanel CPU, RAM, processes, disk/I/O quotas and bot logs.
2. Keep media concurrency at 1; lower `MAX_ACTIVE_JOBS` to 1–2 if needed.
3. Lower file/presentation/Office limits to fit resources; never remove caps.
4. Reduce note/Office concurrency for low quotas. Set `NOTE_HEDGE_AFTER_SECONDS=0`
   if extra parallel provider requests are undesirable.
5. Change hosting if the account kills permitted work; cron cannot bypass quotas.

Document parsing uses threads, not a separate CPU/memory sandbox. Pathological
files may still consume substantial resources. The document cache has no automatic
retention cap; monitor it separately from library references.

## Measure offline

On an isolated development checkout, inspect options first:

```powershell
.\.venv\Scripts\python.exe -m scripts.concurrency_bench --help
.\.venv\Scripts\python.exe -m scripts.perf_bench --help
```

```bash
python -m scripts.concurrency_bench --help
python -m scripts.perf_bench --help
```

Other tools: `scripts.admin_report_bench`, `scripts.document_chunk_bench`.
Simulated latencies/429s are test inputs, not live measurements. Record settings,
Python version, workload and host quotas with results; do not promise fixed throughput.

Document uploads default to 20 MiB (`DOC_MAX_BYTES`); Office archive entries
are also limited to 100 MiB in total (`DOC_UNPACKED_MAX_BYTES`). Operators may
raise these limits, but doing so increases peak memory on the 2 GiB host.

Learning generations are limited by `LEARNING_GLOBAL_CONCURRENCY` (default 2),
with `LEARNING_CHUNK_CONCURRENCY` workers per generation and a separate
`LEARNING_MAX_OUTPUT_TOKENS` budget (default 4096). Static system prompts improve
prefix caching; source data and retry instructions are sent in the user prompt.

A learning generation holds its global admission permit while running; provider
capacity is checked without waiting, so it never waits for a second slot while
holding that permit. If all routes are saturated, the normal learning retry and
refund path applies rather than queueing behind a provider slot.
