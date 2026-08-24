from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any

from api.services.observability_sanitizer import safe_json, sanitize_value
from api.services.observability_hierarchy import validate_span_hierarchy

SCHEMA = """
CREATE TABLE IF NOT EXISTS observability_traces (
 trace_id TEXT PRIMARY KEY, workflow_id TEXT, branch_id TEXT NOT NULL DEFAULT 'original',
 execution_id TEXT, name TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'live',
 parent_trace_id TEXT, origin_checkpoint TEXT, started_at TEXT NOT NULL, ended_at TEXT,
 duration_ms REAL, root_span_id TEXT, attributes_json TEXT NOT NULL DEFAULT '{}', retained INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT
);
CREATE TABLE IF NOT EXISTS observability_spans (
 span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_span_id TEXT, name TEXT NOT NULL,
 category TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, workflow_id TEXT,
 branch_id TEXT, task_id TEXT, agent TEXT, node TEXT, operation TEXT, execution_id TEXT,
 started_at TEXT NOT NULL, ended_at TEXT, duration_ms REAL, error_type TEXT,
 error_message TEXT, error_fingerprint TEXT, is_slow INTEGER NOT NULL DEFAULT 0,
 attributes_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT,
 FOREIGN KEY(trace_id) REFERENCES observability_traces(trace_id)
);
CREATE TABLE IF NOT EXISTS observability_span_events (
 event_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, span_id TEXT, name TEXT NOT NULL,
 timestamp TEXT NOT NULL, attributes_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS observability_metrics (
 metric_id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, workflow_id TEXT, name TEXT NOT NULL,
 value REAL NOT NULL, unit TEXT, timestamp TEXT NOT NULL, attributes_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS observability_logs (
 log_id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, span_id TEXT, workflow_id TEXT,
 level TEXT NOT NULL, message TEXT NOT NULL, timestamp TEXT NOT NULL, attributes_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS observability_llm_calls (
 call_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, span_id TEXT NOT NULL, workflow_id TEXT,
 branch_id TEXT NOT NULL DEFAULT 'original', agent TEXT, node TEXT, subgraph TEXT, task_id TEXT,
 retry_attempt INTEGER NOT NULL DEFAULT 0, provider TEXT, model TEXT, operation TEXT, status TEXT NOT NULL, duration_ms REAL,
 input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, cached_tokens INTEGER,
 reasoning_tokens INTEGER, audio_input_tokens INTEGER, audio_output_tokens INTEGER,
 usage_source TEXT NOT NULL DEFAULT 'unavailable', usage_available INTEGER NOT NULL DEFAULT 0,
 usage_invalid INTEGER NOT NULL DEFAULT 0, usage_error TEXT, provider_request_id TEXT,
 raw_usage_version TEXT, error_fingerprint TEXT, timestamp TEXT NOT NULL,
 completed_at TEXT, attributes_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS observability_tool_calls (
 call_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, span_id TEXT NOT NULL, workflow_id TEXT,
 tool_name TEXT NOT NULL, server_name TEXT, operation TEXT, status TEXT NOT NULL, duration_ms REAL,
 error_fingerprint TEXT, timestamp TEXT NOT NULL, arguments_summary_json TEXT NOT NULL DEFAULT '{}',
 result_summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS observability_artifacts (
 artifact_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, span_id TEXT, workflow_id TEXT,
 artifact_type TEXT NOT NULL, relative_path TEXT, content_hash TEXT, size_bytes INTEGER,
 source TEXT NOT NULL DEFAULT 'live', created_at TEXT NOT NULL, attributes_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_observability_traces_workflow ON observability_traces(workflow_id, branch_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_observability_traces_status ON observability_traces(status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_observability_spans_trace ON observability_spans(trace_id, started_at);
CREATE INDEX IF NOT EXISTS idx_observability_spans_filter ON observability_spans(category, status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_observability_spans_error ON observability_spans(error_fingerprint, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_observability_logs_trace ON observability_logs(trace_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_observability_llm_workflow ON observability_llm_calls(workflow_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_observability_tools_name ON observability_tool_calls(tool_name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_observability_artifacts_trace ON observability_artifacts(trace_id, created_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_datetime(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json"):
            result[key[:-5]] = json.loads(result.pop(key) or "{}")
    for key in ("is_slow", "retained"):
        if key in result:
            result[key] = bool(result[key])
    return result


class ObservabilityStore:
    def __init__(self, database_path: Path | str, *, busy_timeout_ms: int = 5_000) -> None:
        self.database_path = Path(database_path)
        self.busy_timeout_ms = busy_timeout_ms

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            for table in ("observability_traces", "observability_spans"):
                columns = {
                    row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if "updated_at" not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN updated_at TEXT")
            llm_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(observability_llm_calls)")
            }
            llm_migrations = {
                "branch_id": "TEXT NOT NULL DEFAULT 'original'", "node": "TEXT", "subgraph": "TEXT",
                "task_id": "TEXT", "retry_attempt": "INTEGER NOT NULL DEFAULT 0", "reasoning_tokens": "INTEGER",
                "audio_input_tokens": "INTEGER", "audio_output_tokens": "INTEGER",
                "usage_source": "TEXT NOT NULL DEFAULT 'unavailable'", "usage_available": "INTEGER NOT NULL DEFAULT 0",
                "usage_invalid": "INTEGER NOT NULL DEFAULT 0", "usage_error": "TEXT",
                "provider_request_id": "TEXT", "raw_usage_version": "TEXT", "completed_at": "TEXT",
            }
            for column, definition in llm_migrations.items():
                if column not in llm_columns:
                    connection.execute(f"ALTER TABLE observability_llm_calls ADD COLUMN {column} {definition}")

    async def initialize(self) -> None:
        await asyncio.to_thread(self.initialize_sync)

    def execute_sync(self, sql: str, parameters: tuple[Any, ...] = ()) -> None:
        with self._connect() as connection:
            connection.execute(sql, parameters)

    async def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> None:
        await asyncio.to_thread(self.execute_sync, sql, parameters)

    async def create_trace(self, record: dict[str, Any]) -> None:
        await self.execute(
            """INSERT OR IGNORE INTO observability_traces
            (trace_id,workflow_id,branch_id,execution_id,name,status,source,parent_trace_id,origin_checkpoint,started_at,root_span_id,attributes_json,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record["trace_id"], record.get("workflow_id"), record.get("branch_id", "original"),
             record.get("execution_id"), record["name"], record.get("status", "running"),
             record.get("source", "live"), record.get("parent_trace_id"), record.get("origin_checkpoint"),
             record.get("started_at", _now()), record.get("root_span_id"), safe_json(record.get("attributes")), _now()),
        )

    async def start_span(self, record: dict[str, Any]) -> None:
        await self.execute(
            """INSERT OR IGNORE INTO observability_spans
            (span_id,trace_id,parent_span_id,name,category,kind,status,workflow_id,branch_id,task_id,agent,node,operation,execution_id,started_at,attributes_json,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record["span_id"], record["trace_id"], record.get("parent_span_id"), record["name"],
             record["category"], record.get("kind", "internal"), record.get("status", "running"),
             record.get("workflow_id"), record.get("branch_id"), record.get("task_id"), record.get("agent"),
             record.get("node"), record.get("operation"), record.get("execution_id"),
             record.get("started_at", _now()), safe_json(record.get("attributes")), _now()),
        )

    async def end_span(self, span_id: str, *, status: str, ended_at: str, duration_ms: float,
                       error_type: str | None = None, error_message: str | None = None,
                       error_fingerprint: str | None = None, is_slow: bool = False) -> None:
        await self.execute(
            """UPDATE observability_spans SET status=?,ended_at=?,duration_ms=?,error_type=?,error_message=?,error_fingerprint=?,is_slow=?,updated_at=?
            WHERE span_id=? AND status IN ('running','waiting')""",
            (status, ended_at, duration_ms, error_type, sanitize_value(error_message), error_fingerprint, int(is_slow), _now(), span_id),
        )

    async def end_trace(self, trace_id: str, *, status: str, ended_at: str, duration_ms: float) -> None:
        await self.execute(
            "UPDATE observability_traces SET status=?,ended_at=?,duration_ms=?,updated_at=? WHERE trace_id=? AND status IN ('running','waiting','interrupted')",
            (status, ended_at, duration_ms, _now(), trace_id),
        )

    def finalize_trace_sync(
        self,
        trace_id: str,
        *,
        status: str,
        ended_at: str,
        reason: str,
        error_type: str | None = None,
        error_message: str | None = None,
        error_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"Unsupported terminal trace status: {status}")
        requested_end = _parse_datetime(ended_at, datetime.now(UTC))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            trace = connection.execute(
                "SELECT * FROM observability_traces WHERE trace_id=?", (trace_id,)
            ).fetchone()
            if trace is None:
                connection.rollback()
                return {"trace_found": False, "closed_spans": 0, "changed": False}
            already_terminal = trace["status"] in {"completed", "failed", "cancelled"}
            effective_status = trace["status"] if already_terminal else status
            effective_end = _parse_datetime(trace["ended_at"], requested_end) if already_terminal else requested_end
            active = connection.execute(
                """SELECT * FROM observability_spans
                   WHERE trace_id=? AND (
                       status IN ('running','waiting')
                       OR (status='interrupted' AND ended_at IS NULL)
                   )""",
                (trace_id,),
            ).fetchall()
            sanitized_error = sanitize_value(error_message)
            for span in active:
                started = _parse_datetime(span["started_at"], effective_end)
                attributes = json.loads(span["attributes_json"] or "{}")
                attributes["reconciliation_reason"] = reason
                connection.execute(
                    """UPDATE observability_spans
                       SET status=?,ended_at=?,duration_ms=?,updated_at=?,attributes_json=?,
                           error_type=CASE WHEN ?='failed' THEN COALESCE(error_type,?) ELSE error_type END,
                           error_message=CASE WHEN ?='failed' THEN COALESCE(error_message,?) ELSE error_message END,
                           error_fingerprint=CASE WHEN ?='failed' THEN COALESCE(error_fingerprint,?) ELSE error_fingerprint END
                       WHERE span_id=? AND (
                           status IN ('running','waiting')
                           OR (status='interrupted' AND ended_at IS NULL)
                       )""",
                    (
                        effective_status,
                        effective_end.isoformat(),
                        max(0.0, (effective_end - started).total_seconds() * 1000),
                        requested_end.isoformat(),
                        safe_json(attributes),
                        effective_status, error_type,
                        effective_status, sanitized_error,
                        effective_status, error_fingerprint,
                        span["span_id"],
                    ),
                )
            trace_started = _parse_datetime(trace["started_at"], effective_end)
            trace_attributes = json.loads(trace["attributes_json"] or "{}")
            if not already_terminal:
                trace_attributes["finalization_reason"] = reason
                connection.execute(
                    """UPDATE observability_traces
                       SET status=?,ended_at=?,duration_ms=?,updated_at=?,attributes_json=?
                       WHERE trace_id=?""",
                    (
                        effective_status,
                        effective_end.isoformat(),
                        max(0.0, (effective_end - trace_started).total_seconds() * 1000),
                        requested_end.isoformat(),
                        safe_json(trace_attributes),
                        trace_id,
                    ),
                )
            invariant = connection.execute(
                """SELECT t.status,t.ended_at,t.duration_ms,t.root_span_id,
                          SUM(CASE WHEN s.status IN ('running','waiting') OR (s.status='interrupted' AND s.ended_at IS NULL) THEN 1 ELSE 0 END) AS active_spans,
                          MAX(CASE WHEN s.span_id=t.root_span_id THEN s.status END) AS root_status
                   FROM observability_traces AS t
                   LEFT JOIN observability_spans AS s ON s.trace_id=t.trace_id
                   WHERE t.trace_id=? GROUP BY t.trace_id""",
                (trace_id,),
            ).fetchone()
            if (
                invariant is None
                or invariant["status"] not in {"completed", "failed", "cancelled"}
                or invariant["ended_at"] is None
                or invariant["duration_ms"] is None
                or int(invariant["active_spans"] or 0) != 0
                or invariant["root_status"] not in {"completed", "failed", "cancelled"}
            ):
                raise sqlite3.IntegrityError("terminal_trace_invariant_failed")
            connection.commit()
            return {
                "trace_found": True,
                "closed_spans": len(active),
                "changed": bool(active) or not already_terminal,
                "status": effective_status,
                "ended_at": invariant["ended_at"],
                "duration_ms": invariant["duration_ms"],
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def finalize_trace(self, trace_id: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.finalize_trace_sync, trace_id, **kwargs)

    async def add_span_event(self, record: dict[str, Any]) -> None:
        await self.execute(
            "INSERT OR IGNORE INTO observability_span_events(event_id,trace_id,span_id,name,timestamp,attributes_json) VALUES(?,?,?,?,?,?)",
            (record["event_id"], record["trace_id"], record.get("span_id"), record["name"], record.get("timestamp", _now()), safe_json(record.get("attributes"))),
        )

    async def add_log(self, record: dict[str, Any]) -> None:
        await self.execute(
            "INSERT INTO observability_logs(trace_id,span_id,workflow_id,level,message,timestamp,attributes_json) VALUES(?,?,?,?,?,?,?)",
            (record.get("trace_id"), record.get("span_id"), record.get("workflow_id"), record.get("level", "info"), sanitize_value(record["message"]), record.get("timestamp", _now()), safe_json(record.get("attributes"))),
        )

    async def add_metric(self, record: dict[str, Any]) -> None:
        await self.execute(
            "INSERT INTO observability_metrics(trace_id,workflow_id,name,value,unit,timestamp,attributes_json) VALUES(?,?,?,?,?,?,?)",
            (record.get("trace_id"), record.get("workflow_id"), record["name"], record["value"], record.get("unit"), record.get("timestamp", _now()), safe_json(record.get("attributes"))),
        )

    async def add_llm_call(self, record: dict[str, Any]) -> None:
        await self.execute(
            """INSERT OR IGNORE INTO observability_llm_calls
            (call_id,trace_id,span_id,workflow_id,branch_id,agent,node,subgraph,task_id,retry_attempt,
             provider,model,operation,status,duration_ms,input_tokens,output_tokens,total_tokens,cached_tokens,
             reasoning_tokens,audio_input_tokens,audio_output_tokens,usage_source,usage_available,usage_invalid,
             usage_error,provider_request_id,raw_usage_version,error_fingerprint,timestamp,completed_at,attributes_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record["call_id"], record["trace_id"], record["span_id"], record.get("workflow_id"),
             record.get("branch_id", "original"), record.get("agent"), record.get("node"), record.get("subgraph"),
             record.get("task_id"), record.get("retry_attempt", 0), record.get("provider"), record.get("model"), record.get("operation"),
             record.get("status", "completed"), record.get("duration_ms"), record.get("input_tokens"),
             record.get("output_tokens"), record.get("total_tokens"), record.get("cached_tokens"),
             record.get("reasoning_tokens"), record.get("audio_input_tokens"), record.get("audio_output_tokens"),
             record.get("usage_source", "unavailable"), int(record.get("usage_available", False)), int(record.get("usage_invalid", False)),
             sanitize_value(record.get("usage_error")), record.get("provider_request_id"), record.get("raw_usage_version"),
             record.get("error_fingerprint"), record.get("timestamp", _now()), record.get("completed_at", _now()),
             safe_json(record.get("attributes"))),
        )

    async def add_tool_call(self, record: dict[str, Any]) -> None:
        await self.execute(
            """INSERT OR IGNORE INTO observability_tool_calls
            (call_id,trace_id,span_id,workflow_id,tool_name,server_name,operation,status,duration_ms,error_fingerprint,timestamp,arguments_summary_json,result_summary_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record["call_id"], record["trace_id"], record["span_id"], record.get("workflow_id"),
             record["tool_name"], record.get("server_name"), record.get("operation"), record.get("status", "completed"),
             record.get("duration_ms"), record.get("error_fingerprint"), record.get("timestamp", _now()),
             safe_json(record.get("arguments_summary")), safe_json(record.get("result_summary"))),
        )

    async def add_artifact(self, record: dict[str, Any]) -> None:
        await self.execute(
            """INSERT OR IGNORE INTO observability_artifacts
            (artifact_id,trace_id,span_id,workflow_id,artifact_type,relative_path,content_hash,size_bytes,source,created_at,attributes_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (record["artifact_id"], record["trace_id"], record.get("span_id"), record.get("workflow_id"),
             record["artifact_type"], record.get("relative_path"), record.get("content_hash"), record.get("size_bytes"),
             record.get("source", "live"), record.get("created_at", _now()), safe_json(record.get("attributes"))),
        )

    def fetch_all_sync(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [_dict(row) for row in connection.execute(sql, parameters).fetchall()]

    async def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.fetch_all_sync, sql, parameters)

    async def fetch_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, parameters)
        return rows[0] if rows else None

    async def find_trace(self, workflow_id: str, branch_id: str = "original") -> dict[str, Any] | None:
        return await self.fetch_one(
            "SELECT * FROM observability_traces WHERE workflow_id=? AND branch_id=? ORDER BY started_at DESC LIMIT 1",
            (workflow_id, branch_id),
        )

    async def find_open_span(self, trace_id: str, name: str) -> dict[str, Any] | None:
        return await self.fetch_one(
            "SELECT * FROM observability_spans WHERE trace_id=? AND name=? AND status IN ('running','waiting') ORDER BY started_at DESC LIMIT 1",
            (trace_id, name),
        )

    async def trace_detail(self, trace_id: str) -> dict[str, Any] | None:
        trace = await self.fetch_one("SELECT * FROM observability_traces WHERE trace_id=?", (trace_id,))
        if trace is None:
            return None
        trace["spans"] = await self.fetch_all("SELECT * FROM observability_spans WHERE trace_id=? ORDER BY started_at", (trace_id,))
        nodes = {span["span_id"]: {**span, "children": []} for span in trace["spans"]}
        roots = []
        for node in nodes.values():
            parent = nodes.get(node.get("parent_span_id"))
            if parent is None:
                roots.append(node)
            else:
                parent["children"].append(node)
        trace["span_tree"] = roots
        trace["events"] = await self.fetch_all("SELECT * FROM observability_span_events WHERE trace_id=? ORDER BY timestamp", (trace_id,))
        trace["logs"] = await self.fetch_all("SELECT * FROM observability_logs WHERE trace_id=? ORDER BY timestamp", (trace_id,))
        trace["artifacts"] = await self.fetch_all("SELECT * FROM observability_artifacts WHERE trace_id=? ORDER BY created_at", (trace_id,))
        trace["hierarchy_validation"] = validate_span_hierarchy(trace, trace["spans"])
        return trace

    def reconcile_terminal_spans_sync(
        self,
        *,
        trace_id: str | None = None,
        dry_run: bool = False,
        reason: str = "terminal_trace_active_span",
    ) -> dict[str, int | bool]:
        now = datetime.now(UTC)
        clauses = [
            "t.status IN ('completed','failed','cancelled')",
            "(s.status IN ('running','waiting') OR (s.status='interrupted' AND s.ended_at IS NULL))",
        ]
        parameters: list[Any] = []
        if trace_id is not None:
            clauses.append("t.trace_id=?")
            parameters.append(trace_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT s.span_id,s.started_at,s.attributes_json,t.status AS trace_status,
                           COALESCE(t.ended_at, ?) AS terminal_ended_at
                    FROM observability_spans AS s
                    JOIN observability_traces AS t ON t.trace_id=s.trace_id
                    WHERE {' AND '.join(clauses)}""",
                (now.isoformat(), *parameters),
            ).fetchall()
            result: dict[str, int | bool] = {
                "dry_run": dry_run,
                "candidate_spans": len(rows),
                "reconciled_spans": 0,
            }
            if dry_run or not rows:
                return result
            connection.execute("BEGIN IMMEDIATE")
            for row in rows:
                ended_at = _parse_datetime(row["terminal_ended_at"], now)
                started_at = _parse_datetime(row["started_at"], ended_at)
                attributes = json.loads(row["attributes_json"] or "{}")
                attributes["reconciliation_reason"] = reason
                connection.execute(
                    """UPDATE observability_spans
                       SET status=?,ended_at=?,duration_ms=?,attributes_json=?,updated_at=?
                       WHERE span_id=? AND (
                           status IN ('running','waiting')
                           OR (status='interrupted' AND ended_at IS NULL)
                       )""",
                    (
                        row["trace_status"],
                        ended_at.isoformat(),
                        max(0.0, (ended_at - started_at).total_seconds() * 1000),
                        safe_json(attributes),
                        now.isoformat(),
                        row["span_id"],
                    ),
                )
            connection.commit()
            result["reconciled_spans"] = len(rows)
            return result

    async def reconcile_terminal_spans(
        self,
        *,
        trace_id: str | None = None,
        dry_run: bool = False,
        reason: str = "terminal_trace_active_span",
    ) -> dict[str, int | bool]:
        return await asyncio.to_thread(
            self.reconcile_terminal_spans_sync,
            trace_id=trace_id,
            dry_run=dry_run,
            reason=reason,
        )

    async def reconcile_running(self) -> int:
        now = _now()
        with_count = await self.fetch_one("SELECT COUNT(*) AS count FROM observability_spans WHERE status='running'")
        await self.execute(
            "UPDATE observability_spans SET status='interrupted',ended_at=?,updated_at=?,error_type='process_interrupted',error_message='Span reconciled after process restart' WHERE status='running' AND category!='workflow'",
            (now, now),
        )
        await self.execute(
            """UPDATE observability_traces SET status='interrupted',ended_at=?,updated_at=?
            WHERE status='running' AND trace_id NOT IN
            (SELECT trace_id FROM observability_spans WHERE status='waiting')""", (now, now))
        return int(with_count["count"] if with_count else 0)

    async def retention(self, *, days: int, dry_run: bool, batch_size: int) -> dict[str, Any]:
        cutoff = (datetime.now(UTC) - timedelta(days=max(days, 1))).isoformat()
        rows = await self.fetch_all(
            "SELECT trace_id FROM observability_traces WHERE started_at<? AND retained=0 AND status NOT IN ('running','waiting') LIMIT ?",
            (cutoff, max(1, min(batch_size, 10_000))),
        )
        ids = [row["trace_id"] for row in rows]
        if not dry_run and ids:
            placeholders = ",".join("?" for _ in ids)
            with self._connect() as connection:
                for table in ("observability_span_events", "observability_metrics", "observability_logs", "observability_llm_calls", "observability_tool_calls", "observability_artifacts", "observability_spans"):
                    connection.execute(f"DELETE FROM {table} WHERE trace_id IN ({placeholders})", ids)
                connection.execute(f"DELETE FROM observability_traces WHERE trace_id IN ({placeholders})", ids)
        return {"dry_run": dry_run, "cutoff": cutoff, "eligible": len(ids), "deleted": 0 if dry_run else len(ids)}
