from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _checkpoint_database_path() -> Path:
    configured = os.getenv("LANGGRAPH_CHECKPOINT_DB", "").strip()
    if not configured:
        return ROOT / "data" / "langgraph-checkpoints.sqlite"
    candidate = Path(configured).expanduser()
    return candidate if candidate.is_absolute() else ROOT / candidate


def event_sequence_database_path() -> Path:
    configured = os.getenv("WORKFLOW_EVENT_DB", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_absolute() else _checkpoint_database_path().parent / candidate
    checkpoint_path = _checkpoint_database_path()
    return checkpoint_path.with_name(f"{checkpoint_path.stem}-events.sqlite")


class SQLiteWorkflowEventSequenceStore:
    """Legacy sequence-only store used by the CLI streaming adapter."""

    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path or event_sequence_database_path()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_event_sequences (
                    thread_id TEXT PRIMARY KEY,
                    last_sequence INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_event_logical_keys (
                    thread_id TEXT NOT NULL,
                    logical_key TEXT NOT NULL,
                    PRIMARY KEY(thread_id, logical_key)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path, timeout=5)

    def last_sequence(self, thread_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_sequence FROM workflow_event_sequences WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def has_logical_key(self, thread_id: str, logical_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM workflow_event_logical_keys
                WHERE thread_id = ? AND logical_key = ?
                """,
                (thread_id, logical_key),
            ).fetchone()
        return row is not None

    def record(
        self,
        thread_id: str,
        sequence: int,
        logical_key: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workflow_event_sequences(thread_id, last_sequence)
                VALUES (?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    last_sequence = MAX(last_sequence, excluded.last_sequence)
                """,
                (thread_id, sequence),
            )
            if logical_key is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO workflow_event_logical_keys(thread_id, logical_key)
                    VALUES (?, ?)
                    """,
                    (thread_id, logical_key),
                )
