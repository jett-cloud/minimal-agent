PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta (version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

INSERT OR IGNORE INTO schema_meta (version, applied_at)
VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

INSERT OR IGNORE INTO schema_meta (version, applied_at)
VALUES (3, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    summary TEXT,
    summary_from_seq INTEGER,
    summary_through_seq INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (summary_from_seq IS NULL AND summary_through_seq IS NULL)
        OR (
            summary_from_seq IS NOT NULL
            AND summary_through_seq IS NOT NULL
            AND summary_from_seq >= 1
            AND summary_from_seq <= summary_through_seq
        )
    )
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    kind TEXT NOT NULL CHECK (
        kind IN ('message', 'tool_call', 'tool_result', 'system_error')
    ),
    content_json TEXT NOT NULL CHECK (json_valid(content_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL
        DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_session_seq
ON messages(session_id, seq);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'completed', 'failed', 'cancelled')
    ),
    loop_count INTEGER NOT NULL DEFAULT 0 CHECK (loop_count >= 0),
    user_message_id TEXT NOT NULL,
    final_message_id TEXT,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (user_message_id) REFERENCES messages(id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (final_message_id) REFERENCES messages(id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (status = 'running' AND finished_at IS NULL)
        OR (status <> 'running' AND finished_at IS NOT NULL)
    ),
    CHECK (status <> 'completed' OR final_message_id IS NOT NULL),
    CHECK (status <> 'failed' OR error_code IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_one_running_per_session
ON runs(session_id)
WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_runs_session_started
ON runs(session_id, started_at);

CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_no INTEGER NOT NULL CHECK (step_no >= 0),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_traces_run_id
ON traces(run_id, id);

CREATE TABLE IF NOT EXISTS stream_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    source_seq INTEGER CHECK (source_seq IS NULL OR source_seq >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    UNIQUE (run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_stream_events_run_id
ON stream_events(run_id, id);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    customer_name TEXT NOT NULL,
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 1 AND quantity <= 100000),
    unit_price TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'confirmed', 'shipped', 'completed', 'cancelled')
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_created_at
ON orders(created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_orders_status_created_at
ON orders(status, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS order_operations (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL CHECK (operation IN ('create', 'update', 'delete')),
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL CHECK (json_valid(response_json)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_invocations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    step_no INTEGER NOT NULL CHECK (step_no >= 1),
    provider_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL CHECK (json_valid(arguments_json)),
    arguments_hash TEXT NOT NULL,
    idempotency_key TEXT,
    retry_semantics TEXT NOT NULL CHECK (
        retry_semantics IN ('safe', 'with_idempotency_key', 'never')
    ),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'succeeded', 'failed', 'unknown')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    tool_call_message_id TEXT NOT NULL,
    result_message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (tool_call_message_id) REFERENCES messages(id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (result_message_id) REFERENCES messages(id) ON DELETE SET NULL
        DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (run_id, provider_call_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_invocations_run_step
ON tool_invocations(run_id, step_no DESC, created_at DESC);

