# Admin panel and statistics

The admin experience is the bot's Telegram panel — there is no separate web dashboard. This page
documents what each screen shows, how every number is defined, and what the data cannot answer.

## Opening the panel

Administrators (the numeric ids in `ADMIN_IDS`) see **🛠 پنل مدیریت** in the main menu; it opens the
dashboard. Every `adm:` callback is authorised against `ADMIN_IDS` before any screen renders, and a
non-administrator who taps a stale keyboard gets an alert, never data.

Command lines that stay available (sent as ordinary messages):

| Command | Effect |
| --- | --- |
| `/grant <user id> <hours>` | Add credit hours to a user, audited |
| `/unlimited <user id> on <reason>` / `off` | Toggle unlimited (special) status, audited |
| `/ban <user id>` / `/unban <user id>` | Block or restore a user, audited |

## Dashboard (`adm:home`)

The dashboard answers three questions before anything else:

1. **How many users use the bot?** Registered total, new in the window, active in the window.
2. **Is processing working and fast?** Requests in the window, delivered vs failed, success rate,
   end-to-end response time (average / median, p95 once there are samples), and the live queue.
3. **What needs attention now?** Pending receipt reviews, failures in the window by category,
   credit expiring within 7 days, database errors, and provider timeouts / rate limits.

Under it are four drill-downs. Each one carries the same window selector and a way back.

| Screen | Callback | Shows |
| --- | --- | --- |
| نمای کلی | `adm:ov:<window>` | The three questions above |
| کاربران | `adm:ovu:<window>` | Totals, new/active/delivered users, blocked and unlimited counts, daily chart |
| پردازش | `adm:ovp:<window>` | Status and type breakdown, timing series, per-stage times, failure categories, STT engine usage, failover counts, daily chart |
| پرداخت و اعتبار | `adm:ovpay:<window>` | Pending reviews, orders created/approved/rejected in the window, approved amount, approval rate, credit expiring soon |
| سلامت سیستم | `adm:ovs` | Uptime, live jobs, peak memory, database/WAL size, last successful processing, provider state, retention settings |

Windows: **امروز** is the current UTC day; **۷ روز / ۳۰ روز** are rolling windows ending now; **کل**
is the whole history. Reporting is UTC end to end, because every timestamp is stored as UTC
ISO-8601. A Persian (Jalali) label is shown for the window start; the boundary itself is UTC.

## Metric definitions

Period figures filter on the row's `submissions.created_at` or `payments.created_at`. "Snapshot"
figures describe the current state and ignore the window. Every figure below is computed from
authoritative rows — nothing is estimated, extrapolated, or filled in.

### Users

| Metric | Definition |
| --- | --- |
| کل ثبت‌شده | `COUNT(*)` of `users` |
| جدید در بازه | Users whose `joined_at` is inside the window |
| فعال در بازه | Distinct `submissions.user_id` for rows created in the window |
| تحویل‌گرفته در بازه | Distinct user ids with at least one submission in the window in status `done` or `partial` |
| مسدود | `users.banned = 1` (snapshot) |
| ویژه | `users.is_unlimited = 1` (snapshot) |

A user who only opens the menu and never uploads is counted as registered but not active: the bot
does not persist a "last seen" timestamp.

### Processing

| Metric | Definition |
| --- | --- |
| درخواست در بازه | Submissions created in the window, all statuses |
| تحویل‌شده | `done` + `partial` — the raw transcript reached the user. `partial` means the booklet failed after the transcript was delivered |
| ناموفق/ردشده | `failed` + `rejected` — the user received nothing. `rejected` covers admission refusals (queue full, active-jobs limit, banned account) |
| نرخ موفقیت | `delivered / (delivered + failed + rejected)`; blank (`—`) when nothing reached a terminal state yet |
| انتظار در صف | `started_at − created_at` per job (average, median, p95, max) |
| پردازش خالص | `finished_at − started_at` |
| پایان به پایان | `finished_at − created_at` |
| p95 | Shown only when the sample has at least 20 rows; below that the median and the sample count are shown instead, because a p95 over a handful of jobs is noise |
| زمان مرحله‌ها | Per-stage seconds from the in-process collector, i.e. since this process started, not for the window |
| خطاهای بازه | Failed/rejected rows in the window grouped by their recorded category |
| موتور تبدیل گفتار | `submissions.stt_engine`, so the count survives transcript retention |
| جایگزینی خودکار | Provider failover counts from the in-process collector (since process start) |

Failure categories are recorded once, when the job fails, and never overwritten:

`input` (rejected file/request), `download`, `media`, `stt`, `notes`, `delivery`, `billing`,
`database`, `cancelled`, `capacity` (admission refusal), `banned`, `internal`, `unclassified`
(rows written before migration 009).

Rows created before migration 009 have no `started_at`, so they are counted in volume but excluded
from queue-wait and processing-time figures. The screens show the sample count next to every timing
so that is visible; nothing is reported as zero for those rows.

### Payments and credit

| Metric | Definition |
| --- | --- |
| در انتظار بررسی | Receipt-backed orders in `pending`/`pending_review` (snapshot) |
| ایجادشده در بازه | Orders created in the window, by status |
| تأییدشده / ردشده در بازه | Orders decided in the window, by `status` |
| مبلغ تأییدشده | Sum of `purchased_price` (the price frozen at purchase) over approved orders in the window — never the plan's current price, never a pending or rejected order |
| نرخ تأیید | Approved / (approved + rejected) in the window |
| اعتبار در حال انقضا | Credit ledger lots with an `expires_at` within the next 7 days (count and total seconds). Lots without an expiry never appear |

### System health

| Metric | Definition |
| --- | --- |
| زمان اجرا | Seconds since this process started |
| کارها | Live active/queued job counts and their peaks since start |
| حافظهٔ اوج | Process high-water RSS from the OS |
| پایگاه‌داده / WAL | Size of the SQLite file and its `-wal` sidecar |
| خطاهای پایگاه‌داده | Failed SQLite transactions since start |
| آخرین پردازش موفق | `MAX(finished_at)` over `done`/`partial`, or «ثبت نشده» |
| ارائه‌دهنده‌ها | Per-provider in-flight/peak, success rate, timeouts, rate limits, server errors since start |
| نگهداری | Configured `RECEIPT_RETENTION_DAYS` and `TRANSCRIPT_RETENTION_DAYS` |

## What the panel deliberately does not show

- **Per-user balances and quota consumption by plan.** Balances are derived per user from the
  ledger; aggregating them for every user on a dashboard tap is a full ledger scan for a number
  that changes with every job. The user's own balance is on «📊 اعتبار من», and the ledger is
  authoritative in the database.
- **LLM token usage and cost.** Provider responses include usage, and the client logs it for
  debugging, but it is not persisted per request, so no period total can be computed. Making this a
  panel metric would need a new table.
- **LLM per-request latency for a window.** Stage timings are in-process only (since start).
- **Queue wait for jobs that started before migration 009.** Not recorded, so those rows are
  excluded rather than counted as instantaneous.
- **Revenue from pending receipts.** Deliberately excluded everywhere.

## Cost and freshness

The dashboard runs one `SELECT` per figure (about 14, plus three timing series) against the
read-only WAL connection. There is no per-row query and no cached copy, so every screen shows the
state as of the moment it is opened. Two consequences worth knowing:

- A figure is internally consistent, but two figures on the same screen come from separate
  statements; a job that finishes in between can move one number and not the other.
- Picking «کل» sorts every finished job to compute median/p95. On a large database that is the most
  expensive screen in the panel. `python -m scripts.admin_report_bench` measures it on synthetic
  data (see [Performance](PERFORMANCE.md)); on 20 000 submissions the whole dashboard is ~30 ms for
  a 7-day window and ~170 ms for all history, while the user/payment screens skip the sorting
  entirely (~35 ms).

The screens that do not display timings pass `with_timings=False`, so they never pay for them.

## Retention interaction

`TRANSCRIPT_RETENTION_DAYS` deletes transcript rows; it does not delete submissions, payments, the
ledger or the recorded STT engine, so processing history and payment totals remain complete. What
does expire: the transcript text itself, and reviewed receipt images after `RECEIPT_RETENTION_DAYS`
(pending receipts are never deleted).
