-- Cumulative runtime/provider counters that survive process restarts.
--
-- Shared hosting (cPanel/Passenger) restarts the bot far more often than an
-- operator would like; in-memory-only counters made the admin «سامانه» screen
-- look healthy after every restart and erased incident history. This table
-- stores absolute per-run totals (never increments), written once per counter
-- at orderly shutdown and read once at startup, so a crash can lose at most
-- the delta of the interrupted run and can never double-count it.
--
-- Only aggregate integers are stored — no prompts, transcripts, identifiers,
-- or message text. Rows are overwritten with absolute totals on every flush.
CREATE TABLE IF NOT EXISTS runtime_metric_totals (
    metric_key   TEXT PRIMARY KEY,
    metric_value INTEGER NOT NULL DEFAULT 0 CHECK (metric_value >= 0),
    updated_at   TEXT NOT NULL
);
