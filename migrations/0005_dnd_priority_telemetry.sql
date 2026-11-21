ALTER TABLE principals ADD COLUMN dnd_until TEXT NULL;
ALTER TABLE principals ADD COLUMN dnd_reason TEXT NULL;
ALTER TABLE messages ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('normal', 'urgent'));

CREATE TABLE IF NOT EXISTS telemetry_snapshots (
    host_id TEXT PRIMARY KEY,
    reported_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);
