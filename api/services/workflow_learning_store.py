from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_learning_outbox (
 candidate_id TEXT PRIMARY KEY,
 workflow_id TEXT NOT NULL,
 branch_id TEXT NOT NULL,
 candidate_json TEXT NOT NULL,
 submission_json TEXT,
 submission_status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_learning_pending
ON workflow_learning_outbox(submission_status, workflow_id, branch_id);
"""


class WorkflowLearningStore:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    async def save_candidates(self, candidates: list[dict[str, Any]]) -> None:
        await asyncio.to_thread(self._save_candidates, candidates)

    def _save_candidates(self, candidates: list[dict[str, Any]]) -> None:
        with self._connect() as connection:
            for item in candidates:
                connection.execute(
                    """INSERT INTO workflow_learning_outbox
                    (candidate_id,workflow_id,branch_id,candidate_json,updated_at)
                    VALUES (?,?,?,?,?) ON CONFLICT(candidate_id) DO NOTHING""",
                    (item["candidate_id"], item["workflow_id"], item["branch_id"],
                     json.dumps(item, ensure_ascii=False, sort_keys=True), item["created_at"]),
                )

    async def record_submission(self, candidate_id: str, status: str,
                                result: dict[str, Any]) -> None:
        await asyncio.to_thread(self._record_submission, candidate_id, status, result)

    def _record_submission(self, candidate_id: str, status: str,
                           result: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE workflow_learning_outbox SET submission_json=?, submission_status=?,
                attempts=attempts+1, updated_at=datetime('now') WHERE candidate_id=?""",
                (json.dumps(result, ensure_ascii=False, sort_keys=True), status, candidate_id),
            )

    async def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._pending, limit)

    def _pending(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT candidate_json,submission_json,submission_status,attempts
                FROM workflow_learning_outbox
                WHERE submission_status IN ('pending','submission_failed')
                ORDER BY updated_at,candidate_id LIMIT ?""", (limit,),
            ).fetchall()
        return [{**json.loads(row["candidate_json"]), "submission_status": row["submission_status"],
                 "attempts": row["attempts"]} for row in rows]
