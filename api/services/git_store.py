from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import re
from typing import Any
from uuid import uuid4


SCHEMA = """
CREATE TABLE IF NOT EXISTS git_operations (
 operation_id TEXT PRIMARY KEY,
 workflow_id TEXT,
 project_id TEXT NOT NULL,
 agent_name TEXT,
 operation TEXT NOT NULL,
 repository_ref TEXT NOT NULL,
 success INTEGER NOT NULL,
 duration_ms REAL NOT NULL,
 error_code TEXT,
 provider TEXT NOT NULL DEFAULT 'local',
 cost REAL NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS git_workflow_state (
 workflow_id TEXT PRIMARY KEY,
 project_id TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'not_initialized',
 base_branch TEXT,
 base_commit TEXT,
 workflow_branch TEXT,
 head_commit TEXT,
 staged_files_json TEXT NOT NULL DEFAULT '[]',
 commit_preview_json TEXT,
 approval_id TEXT,
 approval_status TEXT,
 commit_sha TEXT,
 commit_message TEXT,
 commit_created_at TEXT,
 fork_origin_workflow TEXT,
 fork_origin_commit TEXT,
 working_tree_clean INTEGER,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS git_promotions (
 promotion_id TEXT PRIMARY KEY,
 workflow_id TEXT NOT NULL,
 project_id TEXT NOT NULL,
 base_branch TEXT NOT NULL,
 workflow_branch TEXT NOT NULL,
 base_commit TEXT,
 current_base_commit TEXT,
 workflow_head TEXT NOT NULL,
 strategy TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 approval_id TEXT,
 actor TEXT,
 status TEXT NOT NULL,
 preview_json TEXT NOT NULL,
 result_json TEXT,
 result_commit TEXT,
 conflict_count INTEGER NOT NULL DEFAULT 0,
 rejection_reason TEXT,
 created_at TEXT NOT NULL,
 completed_at TEXT,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_git_operations_workflow
 ON git_operations(workflow_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_git_operations_project
 ON git_operations(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_git_promotions_workflow
 ON git_promotions(workflow_id, created_at DESC);
"""

COMMIT_COLUMNS = {
    "base_commit", "head_commit", "commit_sha", "fork_origin_commit",
    "current_base_commit", "workflow_head", "result_commit", "parent_commit",
}
SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


def normalize_commit_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if SHA_RE.fullmatch(text) else None


def normalize_commit_fields(record: dict[str, Any]) -> dict[str, Any]:
    values = dict(record)
    for column in COMMIT_COLUMNS:
        if column in values:
            values[column] = normalize_commit_id(values.get(column))
    return values


class GitAuditStore:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(git_operations)")
            }
            additions = {
                "branch": "TEXT", "commit_sha": "TEXT", "staged_file_count": "INTEGER",
                "diff_fingerprint": "TEXT", "approval_id": "TEXT", "actor": "TEXT",
                "commit_message": "TEXT", "phase": "TEXT", "files_json": "TEXT",
                "base_commit": "TEXT", "parent_commit": "TEXT", "trace_id": "TEXT",
                "command_operation": "TEXT", "returncode": "INTEGER", "stderr_summary": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE git_operations ADD COLUMN {name} {definition}")
            state_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(git_workflow_state)")
            }
            if "working_tree_clean" not in state_columns:
                connection.execute("ALTER TABLE git_workflow_state ADD COLUMN working_tree_clean INTEGER")

    async def initialize(self) -> None:
        await asyncio.to_thread(self.initialize_sync)

    def resolve_workflow_project_sync(self, workflow_id: str) -> str | None:
        with self._connect() as connection:
            try:
                row = connection.execute(
                    "SELECT project_name FROM workflow_registry WHERE thread_id=?",
                    (workflow_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        return str(row["project_name"]) if row and row["project_name"] else None

    async def resolve_workflow_project(self, workflow_id: str) -> str | None:
        return await asyncio.to_thread(self.resolve_workflow_project_sync, workflow_id)

    def bind_workflow_project_sync(self, workflow_id: str, project_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT project_name FROM workflow_registry WHERE thread_id=?", (workflow_id,)
            ).fetchone()
            if row is None:
                return False
            existing = row["project_name"]
            if existing and str(existing) != project_id:
                return False
            if not existing:
                connection.execute(
                    "UPDATE workflow_registry SET project_name=? WHERE thread_id=? AND project_name IS NULL",
                    (project_id, workflow_id),
                )
            return True

    async def bind_workflow_project(self, workflow_id: str, project_id: str) -> bool:
        return await asyncio.to_thread(self.bind_workflow_project_sync, workflow_id, project_id)

    def workflow_tests_passed_sync(self, workflow_id: str) -> bool | None:
        with self._connect() as connection:
            try:
                row = connection.execute(
                    "SELECT tests_passed FROM workflow_registry WHERE thread_id=?",
                    (workflow_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        return bool(row["tests_passed"]) if row else None

    async def workflow_tests_passed(self, workflow_id: str) -> bool | None:
        return await asyncio.to_thread(self.workflow_tests_passed_sync, workflow_id)

    def workflow_gate_sync(self, workflow_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            try:
                row = connection.execute(
                    "SELECT terminal_status,tests_passed FROM workflow_registry WHERE thread_id=?",
                    (workflow_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        return dict(row) if row else None

    async def workflow_gate(self, workflow_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.workflow_gate_sync, workflow_id)

    def record_sync(self, record: dict[str, Any]) -> str:
        operation_id = str(record.get("operation_id") or uuid4().hex)
        values = normalize_commit_fields(record)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO git_operations
                (operation_id,workflow_id,project_id,agent_name,operation,
                 repository_ref,success,duration_ms,error_code,provider,cost,created_at,
                 branch,commit_sha,staged_file_count,diff_fingerprint,approval_id,actor,commit_message,
                 phase,files_json,base_commit,parent_commit,trace_id,command_operation,returncode,
                 stderr_summary)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    operation_id,
                    values.get("workflow_id"),
                    values["project_id"],
                    values.get("agent_name"),
                    values["operation"],
                    values["repository_ref"],
                    int(bool(values["success"])),
                    float(values["duration_ms"]),
                    values.get("error_code"),
                    "local",
                    0.0,
                    values.get("created_at") or datetime.now(UTC).isoformat(),
                    values.get("branch"), values.get("commit_sha"),
                    values.get("staged_file_count"), values.get("diff_fingerprint"),
                    values.get("approval_id"), values.get("actor"),
                    values.get("commit_message"),
                    values.get("phase"), values.get("files_json"),
                    values.get("base_commit"), values.get("parent_commit"),
                    values.get("trace_id"),
                    values.get("command_operation"), values.get("returncode"),
                    values.get("stderr_summary"),
                ),
            )
        return operation_id

    async def record(self, record: dict[str, Any]) -> str:
        return await asyncio.to_thread(self.record_sync, record)

    def list_sync(self, *, workflow_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM git_operations"
        parameters: tuple[Any, ...] = ()
        if workflow_id:
            sql += " WHERE workflow_id=?"
            parameters = (workflow_id,)
        sql += " ORDER BY created_at"
        with self._connect() as connection:
            return [
                normalize_commit_fields(dict(row))
                for row in connection.execute(sql, parameters)
            ]

    async def list(self, *, workflow_id: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_sync, workflow_id=workflow_id)

    def get_workflow_state_sync(self, workflow_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM git_workflow_state WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
        return normalize_commit_fields(dict(row)) if row else None

    async def get_workflow_state(self, workflow_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_workflow_state_sync, workflow_id)

    def upsert_workflow_state_sync(self, workflow_id: str, project_id: str, updates: dict[str, Any]) -> None:
        current = self.get_workflow_state_sync(workflow_id) or {
            "workflow_id": workflow_id, "project_id": project_id,
            "state": "not_initialized", "staged_files_json": "[]",
        }
        current.update(updates)
        current["workflow_id"] = workflow_id
        current["project_id"] = project_id
        current["updated_at"] = datetime.now(UTC).isoformat()
        current = normalize_commit_fields(current)
        columns = [
            "workflow_id", "project_id", "state", "base_branch", "base_commit",
            "workflow_branch", "head_commit", "staged_files_json", "commit_preview_json",
            "approval_id", "approval_status", "commit_sha", "commit_message",
            "commit_created_at", "fork_origin_workflow", "fork_origin_commit", "updated_at",
            "working_tree_clean",
        ]
        with self._connect() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO git_workflow_state ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(current.get(column) for column in columns),
            )

    async def upsert_workflow_state(self, workflow_id: str, project_id: str, updates: dict[str, Any]) -> None:
        await asyncio.to_thread(self.upsert_workflow_state_sync, workflow_id, project_id, updates)

    def save_promotion_sync(self, record: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        values = dict(record)
        values.setdefault("created_at", now)
        values["updated_at"] = now
        values = normalize_commit_fields(values)
        columns = (
            "promotion_id", "workflow_id", "project_id", "base_branch",
            "workflow_branch", "base_commit", "current_base_commit", "workflow_head",
            "strategy", "fingerprint", "approval_id", "actor", "status",
            "preview_json", "result_json", "result_commit", "conflict_count",
            "rejection_reason", "created_at", "completed_at", "updated_at",
        )
        with self._connect() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO git_promotions ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(values.get(column) for column in columns),
            )

    async def save_promotion(self, record: dict[str, Any]) -> None:
        await asyncio.to_thread(self.save_promotion_sync, record)

    def get_promotion_sync(self, workflow_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM git_promotions WHERE workflow_id=? ORDER BY created_at DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
        return normalize_commit_fields(dict(row)) if row else None

    async def get_promotion(self, workflow_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_promotion_sync, workflow_id)
