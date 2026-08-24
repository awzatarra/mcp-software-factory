from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS ci_pipeline_runs (
 ci_run_id TEXT PRIMARY KEY,
 workflow_id TEXT NOT NULL,
 project_id TEXT NOT NULL,
 pipeline_fingerprint TEXT NOT NULL,
 status TEXT NOT NULL,
 pipeline_json TEXT NOT NULL,
 source_json TEXT NOT NULL,
 failed_step TEXT,
 failure_type TEXT,
 failure_message TEXT,
 warnings_json TEXT NOT NULL DEFAULT '[]',
 gate_policy_version TEXT,
 gates_json TEXT NOT NULL DEFAULT '[]',
 decision TEXT,
 failed_gates_json TEXT NOT NULL DEFAULT '[]',
 warning_gates_json TEXT NOT NULL DEFAULT '[]',
 blocking_gate TEXT,
 gate_summary_json TEXT NOT NULL DEFAULT '{}',
 ci_validated_commit TEXT,
 repairability_json TEXT,
 started_at TEXT,
 completed_at TEXT,
 duration_seconds REAL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ci_step_runs (
 ci_run_id TEXT NOT NULL,
 step_id TEXT NOT NULL,
 name TEXT NOT NULL,
 type TEXT NOT NULL,
 status TEXT NOT NULL,
 started_at TEXT,
 completed_at TEXT,
 duration_seconds REAL,
 exit_code INTEGER,
 stdout_summary TEXT,
 stderr_summary TEXT,
 output_truncated INTEGER NOT NULL DEFAULT 0,
 failure_type TEXT,
 failure_message TEXT,
 PRIMARY KEY(ci_run_id, step_id),
 FOREIGN KEY(ci_run_id) REFERENCES ci_pipeline_runs(ci_run_id)
);
CREATE INDEX IF NOT EXISTS idx_ci_runs_workflow
 ON ci_pipeline_runs(workflow_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ci_runs_status
 ON ci_pipeline_runs(status, created_at DESC);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _text(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


class CIPipelineStore:
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
            columns = {row[1] for row in connection.execute("PRAGMA table_info(ci_pipeline_runs)")}
            additions = {
                "gate_policy_version": "TEXT",
                "gates_json": "TEXT NOT NULL DEFAULT '[]'",
                "decision": "TEXT",
                "failed_gates_json": "TEXT NOT NULL DEFAULT '[]'",
                "warning_gates_json": "TEXT NOT NULL DEFAULT '[]'",
                "blocking_gate": "TEXT",
                "gate_summary_json": "TEXT NOT NULL DEFAULT '{}'",
                "ci_validated_commit": "TEXT",
                "repairability_json": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE ci_pipeline_runs ADD COLUMN {name} {definition}")

    async def initialize(self) -> None:
        await asyncio.to_thread(self.initialize_sync)

    async def reconcile_interrupted(self) -> None:
        await asyncio.to_thread(self._reconcile_interrupted_sync)

    def _reconcile_interrupted_sync(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT ci_run_id FROM ci_pipeline_runs WHERE status='running'"
            ).fetchall()
            for row in rows:
                ci_run_id = row["ci_run_id"]
                connection.execute(
                    """UPDATE ci_pipeline_runs
                    SET status='interrupted', failure_type='ci_execution_interrupted',
                        failure_message='CI execution was interrupted before completion.',
                        completed_at=COALESCE(completed_at, ?), updated_at=?
                    WHERE ci_run_id=?""",
                    (now, now, ci_run_id),
                )
                connection.execute(
                    """UPDATE ci_step_runs
                    SET status='interrupted', completed_at=COALESCE(completed_at, ?),
                        failure_type='ci_execution_interrupted',
                        failure_message='CI step was interrupted before completion.'
                    WHERE ci_run_id=? AND status='running'""",
                    (now, ci_run_id),
                )

    async def create_run(self, run: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._create_run_sync, run)

    def _create_run_sync(self, run: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT ci_run_id FROM ci_pipeline_runs WHERE ci_run_id=?",
                (run["ci_run_id"],),
            ).fetchone()
            if existing:
                return self._get_run_sync(run["ci_run_id"], connection=connection)
            connection.execute(
                """INSERT INTO ci_pipeline_runs(
                    ci_run_id, workflow_id, project_id, pipeline_fingerprint, status,
                    pipeline_json, source_json, failed_step, failure_type, failure_message,
                    warnings_json, gate_policy_version, gates_json, decision, failed_gates_json,
                    warning_gates_json, blocking_gate, gate_summary_json, ci_validated_commit,
                    repairability_json,
                    started_at, completed_at, duration_seconds, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run["ci_run_id"], run["workflow_id"], run["project_id"],
                    run["pipeline_fingerprint"], run["status"], _json(run["pipeline"]),
                    _json(run["source"]), run.get("failed_step"), run.get("failure_type"),
                    run.get("failure_message"), _json(run.get("warnings") or []),
                    run.get("gate_policy_version"), _json(run.get("gates") or []),
                    run.get("decision"), _json(run.get("failed_gates") or []),
                    _json(run.get("warning_gates") or []), run.get("blocking_gate"),
                    _json(run.get("gate_summary") or {}), run.get("ci_validated_commit"),
                    _json(run.get("repairability")) if run.get("repairability") else None,
                    _text(run.get("started_at")), _text(run.get("completed_at")), run.get("duration_seconds"),
                    now, now,
                ),
            )
            for step in run.get("steps") or []:
                self._upsert_step_sync(connection, run["ci_run_id"], step)
            return self._get_run_sync(run["ci_run_id"], connection=connection)

    async def update_run(self, ci_run_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._update_run_sync, ci_run_id, updates)

    def _update_run_sync(self, ci_run_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "status", "failed_step", "failure_type", "failure_message",
            "warnings", "started_at", "completed_at", "duration_seconds",
            "gate_policy_version", "gates", "decision", "failed_gates",
            "warning_gates", "blocking_gate", "gate_summary", "ci_validated_commit",
            "repairability",
        }
        fields: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                continue
            json_columns = {
                "warnings": "warnings_json",
                "gates": "gates_json",
                "failed_gates": "failed_gates_json",
                "warning_gates": "warning_gates_json",
                "gate_summary": "gate_summary_json",
                "repairability": "repairability_json",
            }
            column = json_columns.get(key, key)
            fields.append(f"{column}=?")
            values.append(_json(value) if key in json_columns else _text(value))
        fields.append("updated_at=?")
        values.append(datetime.now(UTC).isoformat())
        values.append(ci_run_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE ci_pipeline_runs SET {', '.join(fields)} WHERE ci_run_id=?",
                tuple(values),
            )
            return self._get_run_sync(ci_run_id, connection=connection)

    async def upsert_step(self, ci_run_id: str, step: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._upsert_step_public_sync, ci_run_id, step)

    def _upsert_step_public_sync(self, ci_run_id: str, step: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            self._upsert_step_sync(connection, ci_run_id, step)
            connection.execute(
                "UPDATE ci_pipeline_runs SET updated_at=? WHERE ci_run_id=?",
                (datetime.now(UTC).isoformat(), ci_run_id),
            )
            return self._get_run_sync(ci_run_id, connection=connection)

    @staticmethod
    def _upsert_step_sync(connection: sqlite3.Connection, ci_run_id: str, step: dict[str, Any]) -> None:
        connection.execute(
            """INSERT INTO ci_step_runs(
                ci_run_id, step_id, name, type, status, started_at, completed_at,
                duration_seconds, exit_code, stdout_summary, stderr_summary,
                output_truncated, failure_type, failure_message
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ci_run_id, step_id) DO UPDATE SET
                name=excluded.name, type=excluded.type, status=excluded.status,
                started_at=excluded.started_at, completed_at=excluded.completed_at,
                duration_seconds=excluded.duration_seconds, exit_code=excluded.exit_code,
                stdout_summary=excluded.stdout_summary, stderr_summary=excluded.stderr_summary,
                output_truncated=excluded.output_truncated, failure_type=excluded.failure_type,
                failure_message=excluded.failure_message""",
            (
                ci_run_id, step["step_id"], step["name"], step["type"], step["status"],
                _text(step.get("started_at")), _text(step.get("completed_at")), step.get("duration_seconds"),
                step.get("exit_code"), step.get("stdout_summary"), step.get("stderr_summary"),
                int(bool(step.get("output_truncated"))), step.get("failure_type"),
                step.get("failure_message"),
            ),
        )

    async def get_run(self, ci_run_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_run_sync, ci_run_id)

    def _get_run_sync(self, ci_run_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        owns = connection is None
        conn = connection or self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM ci_pipeline_runs WHERE ci_run_id=?",
                (ci_run_id,),
            ).fetchone()
            if row is None:
                return None
            steps = conn.execute(
                "SELECT * FROM ci_step_runs WHERE ci_run_id=? ORDER BY rowid",
                (ci_run_id,),
            ).fetchall()
            return self._row(row, steps)
        finally:
            if owns:
                conn.close()

    async def latest_run(self, workflow_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._latest_run_sync, workflow_id)

    def _latest_run_sync(self, workflow_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT ci_run_id FROM ci_pipeline_runs WHERE workflow_id=? ORDER BY created_at DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
            return self._get_run_sync(row["ci_run_id"], connection=connection) if row else None

    async def latest_completed_run_for_commit(
        self, workflow_id: str, source_commit: str,
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._latest_completed_run_for_commit_sync,
            workflow_id,
            source_commit,
        )

    def _latest_completed_run_for_commit_sync(
        self, workflow_id: str, source_commit: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT ci_run_id FROM ci_pipeline_runs
                WHERE workflow_id=? AND status IN ('passed','failed')
                ORDER BY completed_at DESC, created_at DESC""",
                (workflow_id,),
            ).fetchall()
            for row in rows:
                run = self._get_run_sync(row["ci_run_id"], connection=connection)
                if not run:
                    continue
                source = run.get("source") or {}
                if source.get("source_commit") == source_commit and run.get("decision"):
                    return run
        return None

    async def list_runs(self, workflow_id: str, *, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_runs_sync, workflow_id, limit, status)

    def _list_runs_sync(self, workflow_id: str, limit: int, status: str | None) -> list[dict[str, Any]]:
        where = "workflow_id=?"
        values: list[Any] = [workflow_id]
        if status:
            where += " AND status=?"
            values.append(status)
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT ci_run_id FROM ci_pipeline_runs WHERE {where} ORDER BY created_at DESC LIMIT ?",
                tuple(values),
            ).fetchall()
            return [
                item for row in rows
                if (item := self._get_run_sync(row["ci_run_id"], connection=connection)) is not None
            ]

    async def list_runs_for_analytics(
        self,
        *,
        limit: int,
        framework: str | None = None,
        from_timestamp: str | None = None,
        to_timestamp: str | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_runs_for_analytics_sync,
            limit,
            framework,
            from_timestamp,
            to_timestamp,
        )

    def _list_runs_for_analytics_sync(
        self,
        limit: int,
        framework: str | None,
        from_timestamp: str | None,
        to_timestamp: str | None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        values: list[Any] = []
        if from_timestamp:
            where.append("COALESCE(completed_at, created_at) >= ?")
            values.append(from_timestamp)
        if to_timestamp:
            where.append("COALESCE(completed_at, created_at) <= ?")
            values.append(to_timestamp)
        values.append(limit)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT ci_run_id FROM ci_pipeline_runs
                {clause}
                ORDER BY COALESCE(completed_at, created_at) DESC, ci_run_id DESC
                LIMIT ?""",
                tuple(values),
            ).fetchall()
            runs = [
                item for row in rows
                if (item := self._get_run_sync(row["ci_run_id"], connection=connection)) is not None
            ]
        if framework:
            target = framework.casefold()
            runs = [
                run for run in runs
                if str((run.get("pipeline") or {}).get("framework") or "").casefold() == target
            ]
        return sorted(
            runs,
            key=lambda item: (
                str(item.get("completed_at") or item.get("started_at") or ""),
                str(item.get("ci_run_id") or ""),
            ),
        )

    @staticmethod
    def _row(row: sqlite3.Row, steps: list[sqlite3.Row]) -> dict[str, Any]:
        return {
            "ci_run_id": row["ci_run_id"],
            "workflow_id": row["workflow_id"],
            "project_id": row["project_id"],
            "pipeline": _loads(row["pipeline_json"], {}),
            "pipeline_fingerprint": row["pipeline_fingerprint"],
            "status": row["status"],
            "source": _loads(row["source_json"], {}),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "duration_seconds": row["duration_seconds"],
            "steps": [
                {
                    "step_id": step["step_id"], "name": step["name"], "type": step["type"],
                    "status": step["status"], "started_at": step["started_at"],
                    "completed_at": step["completed_at"],
                    "duration_seconds": step["duration_seconds"], "exit_code": step["exit_code"],
                    "stdout_summary": step["stdout_summary"], "stderr_summary": step["stderr_summary"],
                    "output_truncated": bool(step["output_truncated"]),
                    "failure_type": step["failure_type"], "failure_message": step["failure_message"],
                }
                for step in steps
            ],
            "failed_step": row["failed_step"],
            "failure_type": row["failure_type"],
            "failure_message": row["failure_message"],
            "warnings": _loads(row["warnings_json"], []),
            "gate_policy_version": row["gate_policy_version"],
            "gates": _loads(row["gates_json"], []),
            "decision": row["decision"],
            "failed_gates": _loads(row["failed_gates_json"], []),
            "warning_gates": _loads(row["warning_gates_json"], []),
            "blocking_gate": row["blocking_gate"],
            "gate_summary": _loads(row["gate_summary_json"], {}),
            "ci_validated_commit": row["ci_validated_commit"],
            "repairability": _loads(row["repairability_json"], None),
        }
