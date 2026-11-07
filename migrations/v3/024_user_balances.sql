-- A cache, never the authority: dirty users are recomputed from FIFO lots.
CREATE TABLE IF NOT EXISTS user_balances (
    user_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
    available_seconds INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    next_transition TEXT
);
CREATE TABLE IF NOT EXISTS balance_dirty (user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS balance_backfill_state (
    id INTEGER PRIMARY KEY CHECK(id=1), last_user_id INTEGER, done INTEGER NOT NULL DEFAULT 0
);
INSERT INTO balance_backfill_state(id,done) VALUES (1,0) ON CONFLICT(id) DO NOTHING;

CREATE TRIGGER IF NOT EXISTS balance_ledger_insert AFTER INSERT ON ledger BEGIN
    INSERT INTO balance_dirty(user_id) VALUES(NEW.user_id) ON CONFLICT(user_id) DO NOTHING;
END;
CREATE TRIGGER IF NOT EXISTS balance_ledger_update AFTER UPDATE ON ledger BEGIN
    INSERT INTO balance_dirty(user_id) VALUES(NEW.user_id) ON CONFLICT(user_id) DO NOTHING;
    INSERT INTO balance_dirty(user_id) VALUES(OLD.user_id) ON CONFLICT(user_id) DO NOTHING;
END;
CREATE TRIGGER IF NOT EXISTS balance_ledger_delete AFTER DELETE ON ledger BEGIN
    INSERT INTO balance_dirty(user_id) VALUES(OLD.user_id) ON CONFLICT(user_id) DO NOTHING;
END;
CREATE TRIGGER IF NOT EXISTS balance_holds_insert AFTER INSERT ON holds BEGIN
    INSERT INTO balance_dirty(user_id) VALUES(NEW.user_id) ON CONFLICT(user_id) DO NOTHING;
END;
CREATE TRIGGER IF NOT EXISTS balance_holds_update AFTER UPDATE ON holds BEGIN
    INSERT INTO balance_dirty(user_id) VALUES(NEW.user_id) ON CONFLICT(user_id) DO NOTHING;
    INSERT INTO balance_dirty(user_id) VALUES(OLD.user_id) ON CONFLICT(user_id) DO NOTHING;
END;
CREATE TRIGGER IF NOT EXISTS balance_holds_delete AFTER DELETE ON holds BEGIN
    INSERT INTO balance_dirty(user_id) VALUES(OLD.user_id) ON CONFLICT(user_id) DO NOTHING;
END;
CREATE TRIGGER IF NOT EXISTS balance_debit_allocation_insert AFTER INSERT ON debit_allocations BEGIN
    INSERT INTO balance_dirty(user_id) SELECT user_id FROM ledger WHERE id=NEW.debit_id ON CONFLICT(user_id) DO NOTHING;
END;
CREATE TRIGGER IF NOT EXISTS balance_debit_allocation_delete AFTER DELETE ON debit_allocations BEGIN
    INSERT INTO balance_dirty(user_id) SELECT user_id FROM ledger WHERE id=OLD.debit_id ON CONFLICT(user_id) DO NOTHING;
END;
CREATE TRIGGER IF NOT EXISTS balance_debit_allocation_update AFTER UPDATE ON debit_allocations BEGIN
    INSERT INTO balance_dirty(user_id) SELECT user_id FROM ledger WHERE id=NEW.debit_id ON CONFLICT(user_id) DO NOTHING;
    INSERT INTO balance_dirty(user_id) SELECT user_id FROM ledger WHERE id=OLD.debit_id ON CONFLICT(user_id) DO NOTHING;
END;
CREATE TRIGGER IF NOT EXISTS balance_hold_allocation_insert AFTER INSERT ON hold_allocations BEGIN
    INSERT INTO balance_dirty(user_id) SELECT user_id FROM holds WHERE submission_id=NEW.submission_id ON CONFLICT(user_id) DO NOTHING;
END;
CREATE TRIGGER IF NOT EXISTS balance_hold_allocation_delete AFTER DELETE ON hold_allocations BEGIN
    INSERT INTO balance_dirty(user_id) SELECT user_id FROM holds WHERE submission_id=OLD.submission_id ON CONFLICT(user_id) DO NOTHING;
END;
CREATE TRIGGER IF NOT EXISTS balance_hold_allocation_update AFTER UPDATE ON hold_allocations BEGIN
    INSERT INTO balance_dirty(user_id) SELECT user_id FROM holds WHERE submission_id=NEW.submission_id ON CONFLICT(user_id) DO NOTHING;
    INSERT INTO balance_dirty(user_id) SELECT user_id FROM holds WHERE submission_id=OLD.submission_id ON CONFLICT(user_id) DO NOTHING;
END;
