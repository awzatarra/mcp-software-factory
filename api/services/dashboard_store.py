from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


DASHBOARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS dashboard_workflow_metrics (
    thread_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    lineage TEXT NOT NULL,
    project_name TEXT NULL,
    workflow_intent TEXT NULL,
    framework TEXT NULL,
    test_framework TEXT NULL,
    terminal_status TEXT NOT NULL,
    status_group TEXT NOT NULL,
    interrupted INTEGER NOT NULL DEFAULT 0,
    data_complete INTEGER NOT NULL DEFAULT 1,
    tests_executed INTEGER NOT NULL DEFAULT 0,
    tests_passed INTEGER NOT NULL DEFAULT 0,
    test_warnings INTEGER NOT NULL DEFAULT 0,
    repair_phase TEXT NOT NULL DEFAULT 'not_started',
    repair_attempts INTEGER NOT NULL DEFAULT 0,
    overall_score REAL NULL,
    grade TEXT NULL,
    evaluation_status TEXT NULL,
    scoring_version TEXT NULL,
    total_seconds REAL NULL,
    active_seconds REAL NULL,
    approval_wait_seconds REAL NULL,
    longest_approval_wait_seconds REAL NULL,
    planning_seconds REAL NULL,
    implementation_seconds REAL NULL,
    testing_seconds REAL NULL,
    repair_seconds REAL NULL,
    approvals_requested INTEGER NOT NULL DEFAULT 0,
    approvals_granted INTEGER NOT NULL DEFAULT 0,
    approvals_rejected INTEGER NOT NULL DEFAULT 0,
    approval_operations_json TEXT NOT NULL DEFAULT '{}',
    evaluations_json TEXT NOT NULL DEFAULT '{}',
    findings_json TEXT NOT NULL DEFAULT '[]',
    recommendations_json TEXT NOT NULL DEFAULT '[]',
    agents_json TEXT NOT NULL DEFAULT '[]',
    related_files_json TEXT NOT NULL DEFAULT '[]',
    pending_operation TEXT NULL,
    created_at TEXT NOT NULL,
    source_updated_at TEXT NULL,
    calculated_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY(thread_id, branch_id)
);
CREATE INDEX IF NOT EXISTS idx_dashboard_metrics_created_at
ON dashboard_workflow_metrics(created_at);
CREATE INDEX IF NOT EXISTS idx_dashboard_metrics_status
ON dashboard_workflow_metrics(status_group);
CREATE INDEX IF NOT EXISTS idx_dashboard_metrics_project
ON dashboard_workflow_metrics(project_name);
CREATE INDEX IF NOT EXISTS idx_dashboard_metrics_intent
ON dashboard_workflow_metrics(workflow_intent);
CREATE INDEX IF NOT EXISTS idx_dashboard_metrics_framework
ON dashboard_workflow_metrics(framework);
CREATE INDEX IF NOT EXISTS idx_dashboard_metrics_grade
ON dashboard_workflow_metrics(grade);
CREATE INDEX IF NOT EXISTS idx_dashboard_metrics_scoring
ON dashboard_workflow_metrics(scoring_version);
CREATE INDEX IF NOT EXISTS idx_dashboard_metrics_source_updated
ON dashboard_workflow_metrics(source_updated_at);
"""


JSON_FIELDS = {
    "approval_operations", "evaluations", "findings", "recommendations", "agents",
    "related_files",
}

MIGRATION_COLUMNS = {
    "test_warnings": "INTEGER NOT NULL DEFAULT 0",
    "related_files_json": "TEXT NOT NULL DEFAULT '[]'",
    "pending_operation": "TEXT NULL",
    "longest_approval_wait_seconds": "REAL NULL",
    "updated_at": "TEXT NOT NULL DEFAULT ''",
    "payload_hash": "TEXT NOT NULL DEFAULT ''",
}


class DashboardMetricStore:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(DASHBOARD_SCHEMA)
            existing = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(dashboard_workflow_metrics)"
                )
            }
            for column, definition in MIGRATION_COLUMNS.items():
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE dashboard_workflow_metrics ADD COLUMN {column} {definition}"
                    )

    async def upsert(self, metric: dict[str, Any]) -> str:
        return await asyncio.to_thread(self._upsert_sync, metric)

    def _upsert_sync(self, metric: dict[str, Any]) -> str:
        payload = dict(metric)
        for field in JSON_FIELDS:
            payload[f"{field}_json"] = json.dumps(
                payload.pop(field, [] if field != "evaluations" and field != "approval_operations" else {}),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        fingerprint_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"created_at", "calculated_at", "updated_at", "payload_hash"}
        }
        payload_hash = hashlib.sha256(json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")).hexdigest()
        payload["payload_hash"] = payload_hash
        payload["updated_at"] = payload["calculated_at"]
        columns = list(payload)
        updates = [
            f"{column}=excluded.{column}"
            for column in columns
            if column not in {"thread_id", "branch_id", "created_at"}
        ]
        sql = f"""
            INSERT INTO dashboard_workflow_metrics({','.join(columns)})
            VALUES ({','.join('?' for _ in columns)})
            ON CONFLICT(thread_id, branch_id) DO UPDATE SET {','.join(updates)}
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT payload_hash FROM dashboard_workflow_metrics
                   WHERE thread_id=? AND branch_id=?""",
                (payload["thread_id"], payload["branch_id"]),
            ).fetchone()
            if existing is not None and existing["payload_hash"] == payload_hash:
                connection.rollback()
                return "unchanged"
            connection.execute(sql, [payload[column] for column in columns])
            connection.commit()
        return "inserted" if existing is None else "updated"

    async def list_metrics(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_metrics_sync)

    def _list_metrics_sync(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM dashboard_workflow_metrics ORDER BY created_at"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for field in JSON_FIELDS:
                item[field] = json.loads(item.pop(f"{field}_json"))
            result.append(item)
        return result

    async def stream_keys(self) -> list[tuple[str, str]]:
        return await asyncio.to_thread(self._stream_keys_sync)

    def _stream_keys_sync(self) -> list[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT thread_id, branch_id FROM workflow_event_streams ORDER BY created_at"
            ).fetchall()
        return [(row["thread_id"], row["branch_id"]) for row in rows]

    async def evaluation_versions(self, thread_id: str, branch_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._evaluation_versions_sync, thread_id, branch_id
        )

    def _evaluation_versions_sync(self, thread_id: str, branch_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT scoring_version, evaluation_json FROM workflow_evaluations
                   WHERE thread_id=? AND branch_id=?""",
                (thread_id, branch_id),
            ).fetchall()
        versions: dict[str, Any] = {}
        for row in rows:
            try:
                evaluation = json.loads(row["evaluation_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            versions[row["scoring_version"]] = {
                "score": evaluation.get("overall_score"),
                "grade": evaluation.get("grade"),
                "status": evaluation.get("evaluation_status"),
            }
        return versions

    async def activity_rows(
        self, *, date_from: str, date_to: str, branch_scope: str,
        limit: int, offset: int, allowed_keys: list[tuple[str, str]] | None = None,
        include_technical: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        return await asyncio.to_thread(
            self._activity_rows_sync,
            date_from, date_to, branch_scope, limit, offset, allowed_keys,
            include_technical,
        )

    def _activity_rows_sync(
        self, date_from: str, date_to: str, branch_scope: str,
        limit: int, offset: int, allowed_keys: list[tuple[str, str]] | None,
        include_technical: bool,
    ) -> tuple[list[dict[str, Any]], int]:
        event_types = [
            "workflow_started", "workflow_completed", "workflow_failed",
            "approval_required", "repair_started", "repair_completed",
            "supervisor_loop_detected",
        ]
        if include_technical:
            event_types.extend((
                "approval_granted", "approval_rejected", "tool_started",
                "tool_completed", "tool_failed",
            ))
        placeholders = ",".join("?" for _ in event_types)
        branch_sql = " AND branch_id='original'" if branch_scope == "original" else ""
        key_sql = ""
        key_params: list[Any] = []
        if allowed_keys is not None:
            if not allowed_keys:
                return [], 0
            key_sql = " AND (" + " OR ".join(
                "(thread_id=? AND branch_id=?)" for _ in allowed_keys
            ) + ")"
            key_params = [value for key in allowed_keys for value in key]
        where = f"timestamp>=? AND timestamp<=? AND event_type IN ({placeholders}){branch_sql}{key_sql}"
        params: list[Any] = [date_from, date_to, *event_types, *key_params]
        with self._connect() as connection:
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM workflow_events WHERE {where}", params
            ).fetchone()[0])
            rows = connection.execute(
                f"""SELECT e.event_id, e.thread_id, e.branch_id, e.event_type,
                           e.stage, e.status, e.timestamp, e.message, e.data_json,
                           m.project_name
                    FROM workflow_events AS e
                    LEFT JOIN dashboard_workflow_metrics AS m
                      ON m.thread_id=e.thread_id AND m.branch_id=e.branch_id
                    WHERE {where.replace('thread_id', 'e.thread_id').replace('branch_id', 'e.branch_id').replace('timestamp', 'e.timestamp').replace('event_type', 'e.event_type')}
                    ORDER BY e.timestamp DESC, e.sequence DESC LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
        return [dict(row) for row in rows], total
