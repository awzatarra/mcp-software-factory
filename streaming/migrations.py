WORKFLOW_EVENT_TABLES = """
CREATE TABLE IF NOT EXISTS workflow_events (
    event_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    stage TEXT NULL,
    status TEXT NOT NULL,
    message TEXT NULL,
    data_json TEXT NOT NULL,
    checkpoint_id TEXT NULL,
    lineage TEXT NOT NULL,
    logical_key TEXT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(thread_id, branch_id, sequence)
);

CREATE TABLE IF NOT EXISTS workflow_event_streams (
    thread_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    last_sequence INTEGER NOT NULL DEFAULT 0,
    terminal_event_type TEXT NULL,
    terminal_sequence INTEGER NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(thread_id, branch_id)
);

CREATE TABLE IF NOT EXISTS workflow_registry (
    thread_id TEXT PRIMARY KEY,
    project_name TEXT NULL,
    workflow_intent TEXT NULL,
    terminal_status TEXT NOT NULL,
    interrupted INTEGER NOT NULL DEFAULT 0,
    pending_operation TEXT NULL,
    pending_tool TEXT NULL,
    tests_executed INTEGER NOT NULL DEFAULT 0,
    tests_passed INTEGER NOT NULL DEFAULT 0,
    test_summary TEXT NULL,
    planning_attempts INTEGER NOT NULL DEFAULT 0,
    implementation_attempts INTEGER NOT NULL DEFAULT 0,
    repair_phase TEXT NOT NULL DEFAULT 'not_started',
    repair_attempts INTEGER NOT NULL DEFAULT 0,
    supervisor_decision TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NULL
);

CREATE TABLE IF NOT EXISTS workflow_project_files (
    thread_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    path TEXT NOT NULL,
    change_type TEXT NOT NULL,
    source_operation TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(thread_id, branch_id, path)
);
"""

WORKFLOW_EVENT_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_workflow_events_stream_sequence
ON workflow_events(thread_id, branch_id, sequence);

CREATE INDEX IF NOT EXISTS idx_workflow_events_thread_type
ON workflow_events(thread_id, event_type);

CREATE INDEX IF NOT EXISTS idx_workflow_events_created_at
ON workflow_events(created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_events_logical_key
ON workflow_events(thread_id, branch_id, logical_key)
WHERE logical_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_workflow_registry_updated_at
ON workflow_registry(updated_at);

CREATE INDEX IF NOT EXISTS idx_workflow_registry_created_at
ON workflow_registry(created_at);

CREATE INDEX IF NOT EXISTS idx_workflow_registry_terminal_status
ON workflow_registry(terminal_status);

CREATE INDEX IF NOT EXISTS idx_workflow_registry_project_name
ON workflow_registry(project_name);

CREATE INDEX IF NOT EXISTS idx_workflow_project_files_thread_branch
ON workflow_project_files(thread_id, branch_id);
"""
