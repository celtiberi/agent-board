-- Agent Board initial schema (SQLite WAL, foreign keys ON)

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS principals (
    id TEXT PRIMARY KEY,
    handle TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('human', 'agent', 'system')),
    display_name TEXT NOT NULL,
    is_server_owner INTEGER NOT NULL DEFAULT 0 CHECK (is_server_owner IN (0, 1)),
    created_at TEXT NOT NULL,
    disabled_at TEXT NULL
);

INSERT OR IGNORE INTO principals (id, handle, kind, display_name, is_server_owner, created_at, disabled_at)
VALUES ('00000000000000000000000000', 'system', 'system', 'System', 0, '2026-11-16T00:00:00Z', NULL);

CREATE TABLE IF NOT EXISTS tokens (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(id),
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    board_scope TEXT NOT NULL CHECK (json_valid(board_scope)),
    max_role TEXT NOT NULL CHECK (max_role IN ('owner', 'agent', 'readonly')),
    created_at TEXT NOT NULL,
    expires_at TEXT NULL,
    last_used_at TEXT NULL,
    revoked_at TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_tokens_principal ON tokens(principal_id);

CREATE TABLE IF NOT EXISTS human_auth (
    principal_id TEXT PRIMARY KEY REFERENCES principals(id),
    password_hash TEXT NOT NULL,
    totp_secret_enc TEXT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_sessions (
    id_hash TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(id),
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_web_sessions_principal ON web_sessions(principal_id);

CREATE TABLE IF NOT EXISTS boards (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{"agents_may_record_decisions": true}' CHECK (json_valid(settings_json)),
    next_seq INTEGER NOT NULL DEFAULT 1 CHECK (next_seq >= 1),
    next_decision_num INTEGER NOT NULL DEFAULT 1 CHECK (next_decision_num >= 1),
    next_issue_num INTEGER NOT NULL DEFAULT 1 CHECK (next_issue_num >= 1),
    created_at TEXT NOT NULL,
    archived_at TEXT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    board_id TEXT NOT NULL REFERENCES boards(id),
    principal_id TEXT NOT NULL REFERENCES principals(id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'agent', 'readonly')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (board_id, principal_id)
);

CREATE INDEX IF NOT EXISTS idx_memberships_principal ON memberships(principal_id);

CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES boards(id),
    title TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES principals(id),
    created_at TEXT NOT NULL,
    closed_at TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_threads_board ON threads(board_id, created_at);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES boards(id),
    thread_id TEXT NOT NULL REFERENCES threads(id),
    seq INTEGER NOT NULL CHECK (seq >= 1),
    author_id TEXT NOT NULL REFERENCES principals(id),
    type TEXT NOT NULL CHECK (type IN (
        'note', 'task', 'question', 'decision', 'issue',
        'status', 'retest_report', 'summary', 'system'
    )),
    body_md TEXT NOT NULL,
    data TEXT NULL CHECK (data IS NULL OR json_valid(data)),
    warnings_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warnings_json)),
    reply_to_id TEXT NULL REFERENCES messages(id),
    supersedes_id TEXT NULL UNIQUE REFERENCES messages(id),
    root_id TEXT NOT NULL,
    root_seq INTEGER NOT NULL CHECK (root_seq >= 1),
    created_at TEXT NOT NULL,
    redacted_at TEXT NULL,
    UNIQUE (board_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_thread_order ON messages(thread_id, root_seq, seq);
CREATE INDEX IF NOT EXISTS idx_messages_board_type_seq ON messages(board_id, type, seq);
CREATE INDEX IF NOT EXISTS idx_messages_root_seq ON messages(root_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(reply_to_id);
CREATE INDEX IF NOT EXISTS idx_messages_board_author_seq ON messages(board_id, author_id, seq);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES boards(id),
    message_id TEXT NULL REFERENCES messages(id),
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    uploaded_by TEXT NOT NULL REFERENCES principals(id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_attachments_board ON attachments(board_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attachments_sha ON attachments(sha256);

CREATE TABLE IF NOT EXISTS mentions (
    message_id TEXT NOT NULL REFERENCES messages(id),
    principal_id TEXT NOT NULL REFERENCES principals(id),
    PRIMARY KEY (message_id, principal_id)
);

CREATE INDEX IF NOT EXISTS idx_mentions_principal ON mentions(principal_id);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES boards(id),
    number INTEGER NOT NULL CHECK (number >= 1),
    title TEXT NOT NULL,
    statement TEXT NOT NULL,
    rationale TEXT NULL,
    decided_by TEXT NOT NULL,
    decided_on TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES principals(id),
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'revoked')),
    superseded_by TEXT NULL REFERENCES decisions(id),
    tags TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags)),
    source_message_id TEXT NOT NULL REFERENCES messages(id),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (board_id, number),
    CHECK (
        (status = 'superseded' AND superseded_by IS NOT NULL)
        OR (status != 'superseded' AND superseded_by IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_decisions_board_status ON decisions(board_id, status, number);

CREATE TABLE IF NOT EXISTS decision_versions (
    decision_id TEXT NOT NULL REFERENCES decisions(id),
    version INTEGER NOT NULL CHECK (version >= 1),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    changed_by TEXT NOT NULL REFERENCES principals(id),
    changed_at TEXT NOT NULL,
    message_id TEXT NOT NULL REFERENCES messages(id),
    PRIMARY KEY (decision_id, version)
);

CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES boards(id),
    number INTEGER NOT NULL CHECK (number >= 1),
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('blocker', 'high', 'normal', 'low')),
    status TEXT NOT NULL CHECK (status IN ('open', 'in_progress', 'resolved', 'wont_fix')),
    assignee_id TEXT NULL REFERENCES principals(id),
    opened_by TEXT NOT NULL REFERENCES principals(id),
    opened_at TEXT NOT NULL,
    resolved_at TEXT NULL,
    resolution TEXT NULL,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    tags TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags)),
    updated_at TEXT NOT NULL,
    UNIQUE (board_id, number),
    CHECK (
        (status IN ('resolved', 'wont_fix') AND resolution IS NOT NULL AND length(trim(resolution)) >= 20)
        OR (status IN ('open', 'in_progress'))
    )
);

CREATE INDEX IF NOT EXISTS idx_issues_board_status ON issues(board_id, status, severity, opened_at);

CREATE TABLE IF NOT EXISTS issue_events (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(id),
    action TEXT NOT NULL CHECK (action IN ('opened', 'status_changed', 'assigned', 'commented', 'resolved', 'reopened')),
    from_val TEXT NULL,
    to_val TEXT NULL,
    actor_id TEXT NOT NULL REFERENCES principals(id),
    message_id TEXT NOT NULL REFERENCES messages(id),
    at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_issue_events_issue ON issue_events(issue_id, at);

CREATE TABLE IF NOT EXISTS cursors (
    principal_id TEXT NOT NULL REFERENCES principals(id),
    board_id TEXT NOT NULL REFERENCES boards(id),
    read_seq INTEGER NOT NULL DEFAULT 0 CHECK (read_seq >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, board_id)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(id),
    url TEXT NOT NULL,
    auth_header_enc TEXT NULL,
    hmac_secret_enc TEXT NOT NULL,
    payload_format TEXT NOT NULL DEFAULT 'default' CHECK (payload_format IN ('default')),
    boards TEXT NOT NULL CHECK (json_valid(boards)),
    filter TEXT NOT NULL CHECK (json_valid(filter)),
    min_interval_s INTEGER NOT NULL DEFAULT 60 CHECK (min_interval_s >= 0),
    debounce_s INTEGER NOT NULL DEFAULT 10 CHECK (debounce_s >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    disabled_reason TEXT NULL,
    last_sent_at TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_principal ON subscriptions(principal_id);

CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    board_id TEXT NOT NULL REFERENCES boards(id),
    latest_seq INTEGER NOT NULL CHECK (latest_seq >= 1),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    next_attempt_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'sent', 'suppressed', 'dead')),
    locked_at TEXT NULL,
    sent_at TEXT NULL,
    last_error TEXT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_one_pending ON outbox(subscription_id, board_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    principal_id TEXT NOT NULL REFERENCES principals(id),
    key TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL CHECK (json_valid(response_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, key)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency_keys(created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    principal_id TEXT NULL REFERENCES principals(id),
    token_prefix TEXT NULL,
    action TEXT NOT NULL,
    board_id TEXT NULL REFERENCES boards(id),
    target TEXT NULL,
    ip TEXT NULL,
    ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json))
);

CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
CREATE INDEX IF NOT EXISTS idx_audit_board ON audit_log(board_id, at);

CREATE TABLE IF NOT EXISTS backup_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
    db_backup_path TEXT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    detail TEXT NULL
);
