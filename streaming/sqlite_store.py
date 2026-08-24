from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any

from pydantic import ValidationError

from streaming.catalog import WorkflowEventType
from streaming.errors import (
    ConflictingTerminalEventError,
    WorkflowEventPersistenceError,
    WorkflowEventStoreClosedError,
)
from streaming.migrations import WORKFLOW_EVENT_INDEXES, WORKFLOW_EVENT_TABLES
from streaming.models import WorkflowEvent

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
TERMINAL_EVENT_TYPES = {
    WorkflowEventType.WORKFLOW_COMPLETED.value,
    WorkflowEventType.WORKFLOW_FAILED.value,
    "supervisor_loop_detected",
}


def workflow_event_store_path() -> Path:
    configured = os.getenv("WORKFLOW_EVENT_STORE_PATH", "").strip()
    candidate = Path(configured).expanduser() if configured else Path("data/workflow-events.sqlite")
    return candidate if candidate.is_absolute() else ROOT / candidate


def event_branch_id(event: WorkflowEvent) -> str:
    return str(event.data.get("branch_id") or "original")


def event_lineage(event: WorkflowEvent) -> str:
    return str(event.data.get("lineage") or event_branch_id(event))


def logical_event_key(event: WorkflowEvent) -> str | None:
    data = event.data
    checkpoint_id = (
        None
        if event.type == WorkflowEventType.APPROVAL_REQUIRED
        else data.get("checkpoint_id")
    )
    identity = (
        checkpoint_id,
        data.get("node"),
        data.get("operation"),
        data.get("tool_name") or data.get("tool"),
        data.get("attempt"),
        data.get("handoff_sequence"),
    )
    if not any(value is not None for value in identity):
        if event.type not in {
            WorkflowEventType.WORKFLOW_STARTED,
            WorkflowEventType.WORKFLOW_COMPLETED,
            WorkflowEventType.WORKFLOW_FAILED,
        }:
            return None
    parts = (event.type.value, event.stage, *identity)
    return json.dumps(parts, ensure_ascii=True, separators=(",", ":"), default=str)


class SQLiteWorkflowEventStore:
    def __init__(
        self,
        database_path: Path | str | None = None,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.database_path = Path(database_path) if database_path is not None else workflow_event_store_path()
        self.busy_timeout_ms = busy_timeout_ms
        self._closed = False
        self._corrupt_rows = 0
        self._metric_lock = Lock()

    def _ensure_open(self) -> None:
        if self._closed:
            raise WorkflowEventStoreClosedError("Workflow event store is closed.")

    def _connect(self) -> sqlite3.Connection:
        self._ensure_open()
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize_sync(self) -> None:
        self._closed = False
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(WORKFLOW_EVENT_TABLES)
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(workflow_events)"
                    )
                }
                if "logical_key" not in columns:
                    connection.execute(
                        "ALTER TABLE workflow_events ADD COLUMN logical_key TEXT NULL"
                    )
                connection.executescript(WORKFLOW_EVENT_INDEXES)
        except sqlite3.Error as exc:
            raise WorkflowEventPersistenceError("Could not initialize workflow event store.") from exc

    async def initialize(self) -> None:
        await asyncio.to_thread(self.initialize_sync)

    async def close(self) -> None:
        self._closed = True

    def reserve_next_sequence_sync(self, thread_id: str, branch_id: str) -> int:
        now = datetime.now(UTC).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT last_sequence
                FROM workflow_event_streams
                WHERE thread_id = ? AND branch_id = ?
                """,
                (thread_id, branch_id),
            ).fetchone()
            next_sequence = (int(row["last_sequence"]) if row is not None else 0) + 1
            connection.execute(
                """
                INSERT INTO workflow_event_streams(
                    thread_id, branch_id, last_sequence, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(thread_id, branch_id) DO UPDATE SET
                    last_sequence = excluded.last_sequence,
                    updated_at = excluded.updated_at
                """,
                (thread_id, branch_id, next_sequence, now, now),
            )
            connection.commit()
            return next_sequence
        except sqlite3.Error as exc:
            connection.rollback()
            raise WorkflowEventPersistenceError("Could not reserve event sequence.") from exc
        finally:
            connection.close()

    async def reserve_next_sequence(self, thread_id: str, branch_id: str) -> int:
        return await asyncio.to_thread(
            self.reserve_next_sequence_sync,
            thread_id,
            branch_id,
        )

    def has_logical_event_sync(
        self,
        thread_id: str,
        branch_id: str,
        logical_key: str | None,
    ) -> bool:
        if logical_key is None:
            return False
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM workflow_events
                WHERE thread_id = ? AND branch_id = ? AND logical_key = ?
                """,
                (thread_id, branch_id, logical_key),
            ).fetchone()
        return row is not None

    @staticmethod
    def _prepare_event(event: WorkflowEvent) -> tuple[Any, ...]:
        branch_id = event_branch_id(event)
        lineage = event_lineage(event)
        checkpoint_id = event.data.get("checkpoint_id")
        return (
            str(event.event_id),
            event.thread_id,
            branch_id,
            event.sequence,
            event.type.value,
            event.timestamp.isoformat(),
            event.source,
            event.stage,
            event.status.value,
            event.message,
            json.dumps(event.data, ensure_ascii=True, separators=(",", ":"), default=str),
            str(checkpoint_id) if checkpoint_id is not None else None,
            lineage,
            logical_event_key(event),
            datetime.now(UTC).isoformat(),
        )

    def _append_on_connection(
        self,
        connection: sqlite3.Connection,
        event: WorkflowEvent,
    ) -> bool:
        branch_id = event_branch_id(event)
        terminal_type = event.type.value if event.type.value in TERMINAL_EVENT_TYPES else None
        if terminal_type is not None:
            terminal = connection.execute(
                """
                SELECT terminal_event_type
                FROM workflow_event_streams
                WHERE thread_id = ? AND branch_id = ?
                """,
                (event.thread_id, branch_id),
            ).fetchone()
            existing = terminal["terminal_event_type"] if terminal is not None else None
            if existing is not None:
                duplicate = connection.execute(
                    """
                    SELECT 1 FROM workflow_events
                    WHERE event_id = ?
                       OR (
                            thread_id = ? AND branch_id = ?
                            AND logical_key = ?
                       )
                    """,
                    (
                        str(event.event_id),
                        event.thread_id,
                        branch_id,
                        logical_event_key(event),
                    ),
                ).fetchone()
                if duplicate is not None:
                    return False
                raise ConflictingTerminalEventError(
                    f"Branch already ended with {existing}; cannot append {terminal_type}."
                )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO workflow_events(
                event_id, thread_id, branch_id, sequence, event_type,
                timestamp, source, stage, status, message, data_json,
                checkpoint_id, lineage, logical_key, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._prepare_event(event),
        )
        if cursor.rowcount == 0:
            return False
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """
            INSERT INTO workflow_event_streams(
                thread_id, branch_id, last_sequence, terminal_event_type,
                terminal_sequence, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id, branch_id) DO UPDATE SET
                last_sequence = MAX(last_sequence, excluded.last_sequence),
                terminal_event_type = COALESCE(
                    workflow_event_streams.terminal_event_type,
                    excluded.terminal_event_type
                ),
                terminal_sequence = COALESCE(
                    workflow_event_streams.terminal_sequence,
                    excluded.terminal_sequence
                ),
                updated_at = excluded.updated_at
            """,
            (
                event.thread_id,
                branch_id,
                event.sequence,
                terminal_type,
                event.sequence if terminal_type is not None else None,
                now,
                now,
            ),
        )
        return True

    def append_sync(self, event: WorkflowEvent) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            inserted = self._append_on_connection(connection, event)
            connection.commit()
            return inserted
        except ConflictingTerminalEventError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise WorkflowEventPersistenceError("Could not append workflow event.") from exc
        finally:
            connection.close()

    async def append(self, event: WorkflowEvent) -> bool:
        return await asyncio.to_thread(self.append_sync, event)

    def append_many_sync(self, events: Sequence[WorkflowEvent]) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            inserted = sum(
                1 for event in events if self._append_on_connection(connection, event)
            )
            connection.commit()
            return inserted
        except (ConflictingTerminalEventError, sqlite3.Error) as exc:
            connection.rollback()
            if isinstance(exc, ConflictingTerminalEventError):
                raise
            raise WorkflowEventPersistenceError("Could not append workflow events.") from exc
        finally:
            connection.close()

    async def append_many(self, events: Sequence[WorkflowEvent]) -> int:
        return await asyncio.to_thread(self.append_many_sync, events)

    def _row_to_event(self, row: sqlite3.Row) -> WorkflowEvent | None:
        try:
            data = json.loads(row["data_json"])
            return WorkflowEvent.model_validate(
                {
                    "event_id": row["event_id"],
                    "thread_id": row["thread_id"],
                    "sequence": row["sequence"],
                    "type": row["event_type"],
                    "timestamp": row["timestamp"],
                    "source": row["source"],
                    "stage": row["stage"],
                    "status": row["status"],
                    "message": row["message"],
                    "data": data,
                }
            )
        except (json.JSONDecodeError, TypeError, ValidationError, ValueError):
            with self._metric_lock:
                self._corrupt_rows += 1
            logger.error(
                "Skipping corrupt workflow event row event_id=%s",
                row["event_id"],
            )
            return None

    def get_events_sync(
        self,
        thread_id: str,
        *,
        branch_id: str = "original",
        after_sequence: int | None = None,
        limit: int | None = None,
        event_types: Sequence[str] | None = None,
    ) -> list[WorkflowEvent]:
        clauses = ["thread_id = ?", "branch_id = ?", "sequence > ?"]
        type_parameters: list[Any] = []
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({placeholders})")
            type_parameters.extend(event_types)
        base_query = (
            "SELECT * FROM workflow_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY sequence ASC"
        )
        try:
            with self._connect() as connection:
                if limit is None:
                    rows = connection.execute(
                        base_query,
                        [thread_id, branch_id, after_sequence or 0, *type_parameters],
                    ).fetchall()
                    return [
                        event
                        for row in rows
                        if (event := self._row_to_event(row)) is not None
                    ]
                events: list[WorkflowEvent] = []
                cursor = after_sequence or 0
                batch_size = max(limit, 25)
                while len(events) < limit:
                    rows = connection.execute(
                        base_query + " LIMIT ?",
                        [thread_id, branch_id, cursor, *type_parameters, batch_size],
                    ).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        cursor = max(cursor, int(row["sequence"]))
                        parsed = self._row_to_event(row)
                        if parsed is not None:
                            events.append(parsed)
                            if len(events) == limit:
                                break
                    if len(rows) < batch_size:
                        break
                return events
        except sqlite3.Error as exc:
            raise WorkflowEventPersistenceError("Could not read workflow events.") from exc

    async def get_events(
        self,
        thread_id: str,
        *,
        branch_id: str = "original",
        after_sequence: int | None = None,
        limit: int | None = None,
        event_types: Sequence[str] | None = None,
    ) -> list[WorkflowEvent]:
        return await asyncio.to_thread(
            self.get_events_sync,
            thread_id,
            branch_id=branch_id,
            after_sequence=after_sequence,
            limit=limit,
            event_types=event_types,
        )

    def get_last_sequence_sync(
        self,
        thread_id: str,
        branch_id: str = "original",
    ) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT last_sequence FROM workflow_event_streams
                WHERE thread_id = ? AND branch_id = ?
                """,
                (thread_id, branch_id),
            ).fetchone()
        return int(row["last_sequence"]) if row is not None else 0

    async def get_last_sequence(
        self,
        thread_id: str,
        branch_id: str = "original",
    ) -> int:
        return await asyncio.to_thread(
            self.get_last_sequence_sync,
            thread_id,
            branch_id,
        )

    async def get_terminal_event(
        self,
        thread_id: str,
        branch_id: str = "original",
    ) -> WorkflowEvent | None:
        events = await self.get_events(
            thread_id,
            branch_id=branch_id,
            event_types=tuple(TERMINAL_EVENT_TYPES),
        )
        return events[-1] if events else None

    def corrupt_rows(self) -> int:
        with self._metric_lock:
            return self._corrupt_rows

    def pragma_sync(self, name: str) -> Any:
        if name not in {"journal_mode", "busy_timeout", "foreign_keys"}:
            raise ValueError("Unsupported pragma.")
        with self._connect() as connection:
            return connection.execute(f"PRAGMA {name}").fetchone()[0]

    def prune_sync(
        self,
        *,
        retention_days: int | None = None,
        max_per_thread: int | None = None,
    ) -> int:
        days = retention_days if retention_days is not None else int(
            os.getenv("WORKFLOW_EVENT_RETENTION_DAYS", "30")
        )
        maximum = max_per_thread if max_per_thread is not None else int(
            os.getenv("WORKFLOW_EVENT_MAX_PER_THREAD", "10000")
        )
        cutoff = (datetime.now(UTC) - timedelta(days=max(days, 0))).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            removed = connection.execute(
                """
                DELETE FROM workflow_events
                WHERE created_at < ?
                  AND event_type NOT IN (?, ?, ?)
                """,
                (cutoff, *sorted(TERMINAL_EVENT_TYPES)),
            ).rowcount
            streams = connection.execute(
                "SELECT thread_id, branch_id FROM workflow_event_streams"
            ).fetchall()
            for stream in streams:
                excess = connection.execute(
                    """
                    SELECT event_id FROM workflow_events
                    WHERE thread_id = ? AND branch_id = ?
                      AND event_type NOT IN (?, ?, ?)
                    ORDER BY sequence DESC
                    LIMIT -1 OFFSET ?
                    """,
                    (
                        stream["thread_id"],
                        stream["branch_id"],
                        *sorted(TERMINAL_EVENT_TYPES),
                        max(maximum, 0),
                    ),
                ).fetchall()
                if excess:
                    placeholders = ",".join("?" for _ in excess)
                    removed += connection.execute(
                        f"DELETE FROM workflow_events WHERE event_id IN ({placeholders})",
                        [row["event_id"] for row in excess],
                    ).rowcount
            connection.commit()
            return removed
        except sqlite3.Error as exc:
            connection.rollback()
            raise WorkflowEventPersistenceError("Could not prune workflow events.") from exc
        finally:
            connection.close()

    async def prune(
        self,
        *,
        retention_days: int | None = None,
        max_per_thread: int | None = None,
    ) -> int:
        return await asyncio.to_thread(
            self.prune_sync,
            retention_days=retention_days,
            max_per_thread=max_per_thread,
        )
