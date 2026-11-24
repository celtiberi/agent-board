CREATE TABLE IF NOT EXISTS timers (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES boards(id),
    principal_id TEXT NOT NULL REFERENCES principals(id),
    thread_id TEXT NULL REFERENCES threads(id),
    due_at TEXT NOT NULL,
    message TEXT NOT NULL,
    cancel_on_reply INTEGER NOT NULL DEFAULT 0 CHECK (cancel_on_reply IN (0, 1)),
    baseline_seq INTEGER NOT NULL DEFAULT 0 CHECK (baseline_seq >= 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'fired', 'cancelled')),
    fired_message_id TEXT NULL REFERENCES messages(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_timers_due ON timers(status, due_at);
CREATE INDEX IF NOT EXISTS idx_timers_principal ON timers(principal_id, status, due_at);

ALTER TABLE threads ADD COLUMN handoff_md TEXT NULL;
ALTER TABLE threads ADD COLUMN handoff_updated_by TEXT NULL REFERENCES principals(id);
ALTER TABLE threads ADD COLUMN handoff_updated_at TEXT NULL;
