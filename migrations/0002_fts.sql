-- Contentful FTS5 search indexes (kept in sync by services/ via explicit root/entity rowids)

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    message_id UNINDEXED,
    root_id UNINDEXED,
    board_id UNINDEXED,
    thread_id UNINDEXED,
    thread_title,
    body_md,
    tokenize = 'porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
    decision_id UNINDEXED,
    board_id UNINDEXED,
    title,
    statement,
    rationale,
    tokenize = 'porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS issues_fts USING fts5(
    issue_id UNINDEXED,
    board_id UNINDEXED,
    title,
    body_md,
    resolution,
    tokenize = 'porter unicode61'
);
