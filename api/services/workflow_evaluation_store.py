from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from api.evaluation_models import WorkflowEvaluationResponse


EVALUATION_TABLE = """
CREATE TABLE IF NOT EXISTS workflow_evaluations (
    thread_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    scoring_version TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    source_updated_at TEXT NULL,
    calculated_at TEXT NOT NULL,
    PRIMARY KEY(thread_id, branch_id, scoring_version)
);
"""


class WorkflowEvaluationStore:
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
            connection.execute(EVALUATION_TABLE)

    async def get(
        self,
        thread_id: str,
        branch_id: str,
        scoring_version: str,
        source_updated_at: datetime | None,
    ) -> WorkflowEvaluationResponse | None:
        return await asyncio.to_thread(
            self._get_sync,
            thread_id,
            branch_id,
            scoring_version,
            source_updated_at,
        )

    def _get_sync(
        self,
        thread_id: str,
        branch_id: str,
        scoring_version: str,
        source_updated_at: datetime | None,
    ) -> WorkflowEvaluationResponse | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT evaluation_json, source_updated_at
                FROM workflow_evaluations
                WHERE thread_id = ? AND branch_id = ? AND scoring_version = ?
                """,
                (thread_id, branch_id, scoring_version),
            ).fetchone()
        if row is None:
            return None
        expected = source_updated_at.isoformat() if source_updated_at else None
        if row["source_updated_at"] != expected:
            return None
        try:
            return WorkflowEvaluationResponse.model_validate_json(
                row["evaluation_json"]
            )
        except (ValueError, TypeError):
            return None

    async def put(self, evaluation: WorkflowEvaluationResponse) -> None:
        await asyncio.to_thread(self._put_sync, evaluation)

    def _put_sync(self, evaluation: WorkflowEvaluationResponse) -> None:
        payload = json.dumps(
            evaluation.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        source_updated_at = (
            evaluation.source_updated_at.isoformat()
            if evaluation.source_updated_at
            else None
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workflow_evaluations(
                    thread_id, branch_id, scoring_version, evaluation_json,
                    source_updated_at, calculated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id, branch_id, scoring_version) DO UPDATE SET
                    evaluation_json = excluded.evaluation_json,
                    source_updated_at = excluded.source_updated_at,
                    calculated_at = excluded.calculated_at
                """,
                (
                    evaluation.thread_id,
                    evaluation.branch_id,
                    evaluation.scoring_version,
                    payload,
                    source_updated_at,
                    evaluation.calculated_at.isoformat(),
                ),
            )
