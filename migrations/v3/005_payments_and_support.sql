-- Payment states and persistent support tickets.
--
-- Payment states: awaiting_payment -> pending_review -> approved | rejected,
-- and cancelled (owner, before a receipt). Legacy 'pending' rows are mapped
-- to the explicit state that matches whether a receipt exists. Approval still
-- credits only the purchase snapshot, exactly once (see db.review_payment).
UPDATE payments SET status = 'awaiting_payment'
 WHERE status = 'pending' AND receipt_path IS NULL;
UPDATE payments SET status = 'pending_review'
 WHERE status = 'pending' AND receipt_path IS NOT NULL;

-- At most one open request per user, whatever its open state.
DROP INDEX IF EXISTS idx_payments_one_pending;
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_one_open
    ON payments(user_id) WHERE status IN ('pending', 'awaiting_payment', 'pending_review');
CREATE INDEX IF NOT EXISTS idx_payments_status_id ON payments(status, payment_id);

-- Support tickets. Each ticket is its own conversation; messages are append-only.
CREATE TABLE IF NOT EXISTS support_ticket_seq (
    id INTEGER PRIMARY KEY AUTOINCREMENT
);

CREATE TABLE IF NOT EXISTS support_tickets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    public_ticket_id TEXT    NOT NULL UNIQUE,
    user_id          INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    category         TEXT    NOT NULL CHECK (category IN ('payment','technical','subscription','other')),
    subject          TEXT    NOT NULL CHECK (length(subject) >= 2 AND length(subject) <= 400),
    status           TEXT    NOT NULL CHECK (status IN ('open','awaiting_admin','awaiting_user','closed')),
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    closed_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_support_tickets_user ON support_tickets(user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_support_tickets_category ON support_tickets(category, status);

CREATE TABLE IF NOT EXISTS support_messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id           INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    sender_id           INTEGER NOT NULL,
    sender_role         TEXT    NOT NULL CHECK (sender_role IN ('user','admin')),
    message_text        TEXT    NOT NULL CHECK (length(message_text) >= 1 AND length(message_text) <= 4000),
    telegram_message_id INTEGER,
    created_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_support_messages_ticket ON support_messages(ticket_id, id);
