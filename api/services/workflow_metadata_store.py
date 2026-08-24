from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Any

from api.models import WorkflowListItem, WorkflowListResponse, WorkflowSnapshotResponse


SORT_COLUMNS = {
    "created_at": "created_at",
    "updated_at": "updated_at",
    "project_name": "project_name",
    "terminal_status": "terminal_status",
}
SORT_ORDERS = {"asc": "ASC", "desc": "DESC"}
TERMINAL_STATUSES = {"completed", "failed"}


def _value(group: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if group.get(name) is not None:
            return group[name]
    return default


def _registry_status(snapshot: WorkflowSnapshotResponse) -> str:
    raw = snapshot.terminal_status.strip().lower()
    if snapshot.interrupted and snapshot.pending_operation == "git_merge":
        return "pending"
    if raw == "completed":
        return "completed"
    if raw not in {"", "pending", "running"}:
        return "failed"
    return raw or "pending"


class WorkflowMetadataStore:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def upsert(self, snapshot: WorkflowSnapshotResponse) -> None:
        await asyncio.to_thread(self._upsert_sync, snapshot)

    async def register_created(self, thread_id: str, request: str) -> None:
        await asyncio.to_thread(self._register_created_sync, thread_id, request)

    def _register_created_sync(self, thread_id: str, request: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO workflow_registry(
                    thread_id, project_name, workflow_intent, terminal_status,
                    created_at, updated_at
                ) VALUES (?, NULL, ?, 'running', ?, ?)
                """,
                (thread_id, request.strip(), now, now),
            )

    async def mark_running(self, thread_id: str) -> None:
        await asyncio.to_thread(self._mark_running_sync, thread_id)

    def _mark_running_sync(self, thread_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE workflow_registry
                SET terminal_status = 'running',
                    interrupted = 0,
                    pending_operation = NULL,
                    pending_tool = NULL,
                    updated_at = ?
                WHERE thread_id = ?
                  AND terminal_status NOT IN ('completed', 'failed')
                """,
                (now, thread_id),
            )

    async def mark_failed(self, thread_id: str) -> None:
        await asyncio.to_thread(self._mark_failed_sync, thread_id)

    def _mark_failed_sync(self, thread_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE workflow_registry
                SET terminal_status = 'failed',
                    interrupted = 0,
                    pending_operation = NULL,
                    pending_tool = NULL,
                    updated_at = ?
                WHERE thread_id = ?
                  AND terminal_status != 'completed'
                """,
                (now, thread_id),
            )

    async def event_timestamps(
        self,
        thread_id: str,
        branch_id: str = "original",
    ) -> tuple[datetime | None, datetime | None]:
        return await asyncio.to_thread(
            self._event_timestamps_sync, thread_id, branch_id
        )

    def _event_timestamps_sync(
        self,
        thread_id: str,
        branch_id: str = "original",
    ) -> tuple[datetime | None, datetime | None]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(timestamp) AS created_at, MAX(timestamp) AS updated_at
                FROM workflow_events
                WHERE thread_id = ? AND branch_id = ?
                """,
                (thread_id, branch_id),
            ).fetchone()
        def parse(value: str | None) -> datetime | None:
            return (
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                if value
                else None
            )

        return parse(row["created_at"]), parse(row["updated_at"])

    async def sync_project_files(
        self,
        thread_id: str,
        *,
        generated_files: list[str],
        updated_files: list[str],
        branch_id: str = "original",
    ) -> None:
        await asyncio.to_thread(
            self._sync_project_files_sync,
            thread_id,
            generated_files,
            updated_files,
            branch_id,
        )

    def _sync_project_files_sync(
        self,
        thread_id: str,
        generated_files: list[str],
        updated_files: list[str],
        branch_id: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        changes = {
            path.replace("\\", "/").lstrip("/"): ("generated", "create_project_structure")
            for path in generated_files
            if path
        }
        changes.update(
            {
                path.replace("\\", "/").lstrip("/"): (
                    "updated",
                    "update_project_files",
                )
                for path in updated_files
                if path
            }
        )
        if not changes:
            return
        with self._connect() as connection:
            for path, (change_type, source_operation) in changes.items():
                connection.execute(
                    """
                    INSERT INTO workflow_project_files(
                        thread_id, branch_id, path, change_type,
                        source_operation, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id, branch_id, path) DO UPDATE SET
                        change_type = excluded.change_type,
                        source_operation = excluded.source_operation,
                        updated_at = excluded.updated_at
                    """,
                    (
                        thread_id,
                        branch_id,
                        path,
                        change_type,
                        source_operation,
                        now,
                        now,
                    ),
                )

    async def get_project_files(
        self,
        thread_id: str,
        branch_id: str = "original",
    ) -> dict[str, str]:
        return await asyncio.to_thread(
            self._get_project_files_sync,
            thread_id,
            branch_id,
        )

    def _get_project_files_sync(
        self,
        thread_id: str,
        branch_id: str,
    ) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT path, change_type
                FROM workflow_project_files
                WHERE thread_id = ? AND branch_id = ?
                  AND change_type != 'deleted'
                """,
                (thread_id, branch_id),
            ).fetchall()
        return {str(row["path"]): str(row["change_type"]) for row in rows}

    def _upsert_sync(self, snapshot: WorkflowSnapshotResponse) -> None:
        now = datetime.now(UTC).isoformat()
        created_at = (snapshot.created_at or datetime.now(UTC)).isoformat()
        updated_at = (snapshot.updated_at or datetime.now(UTC)).isoformat()
        planning = snapshot.planning
        implementation = snapshot.implementation
        testing = snapshot.testing
        supervisor = snapshot.supervisor
        status = _registry_status(snapshot)
        has_pending_interrupt = bool(
            snapshot.interrupted
            and snapshot.pending_operation == "git_merge"
            and snapshot.pending_tool is not None
        )
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT terminal_status, tests_passed FROM workflow_registry WHERE thread_id = ?",
                (snapshot.thread_id,),
            ).fetchone()
            if (
                existing is not None
                and existing["terminal_status"] in TERMINAL_STATUSES
                and not has_pending_interrupt
                and not (
                    status == "completed"
                    and existing["terminal_status"] == "failed"
                )
            ):
                status = str(existing["terminal_status"])
            tests_passed = bool(_value(testing, "passed", "tests_passed", default=False))
            if existing is not None and bool(existing["tests_passed"]):
                tests_passed = True
            pending_operation = (
                snapshot.pending_operation
                if status not in TERMINAL_STATUSES or has_pending_interrupt
                else None
            )
            pending_tool = (
                snapshot.pending_tool
                if status not in TERMINAL_STATUSES or has_pending_interrupt
                else None
            )
            connection.execute(
                """
                INSERT INTO workflow_registry(
                    thread_id, project_name, workflow_intent, terminal_status,
                    interrupted, pending_operation, pending_tool, tests_executed,
                    tests_passed, test_summary, planning_attempts,
                    implementation_attempts, repair_phase, repair_attempts,
                    supervisor_decision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    project_name = excluded.project_name,
                    workflow_intent = excluded.workflow_intent,
                    terminal_status = excluded.terminal_status,
                    interrupted = excluded.interrupted,
                    pending_operation = excluded.pending_operation,
                    pending_tool = excluded.pending_tool,
                    tests_executed = excluded.tests_executed,
                    tests_passed = excluded.tests_passed,
                    test_summary = COALESCE(excluded.test_summary, workflow_registry.test_summary),
                    planning_attempts = excluded.planning_attempts,
                    implementation_attempts = excluded.implementation_attempts,
                    repair_phase = excluded.repair_phase,
                    repair_attempts = excluded.repair_attempts,
                    supervisor_decision = excluded.supervisor_decision,
                    updated_at = excluded.updated_at
                """,
                (
                    snapshot.thread_id,
                    snapshot.project_name,
                    snapshot.workflow_intent,
                    status,
                    int(snapshot.interrupted and (status not in TERMINAL_STATUSES or has_pending_interrupt)),
                    pending_operation,
                    pending_tool,
                    int(bool(_value(testing, "executed", "tests_executed", default=False))),
                    int(tests_passed),
                    _value(testing, "final_test_result_summary", "test_summary"),
                    int(_value(planning, "attempts", "planning_attempts", default=0)),
                    int(_value(implementation, "attempts", "implementation_attempts", default=0)),
                    str(_value(testing, "repair_phase", default="not_started")),
                    int(_value(testing, "repair_attempts", default=0)),
                    _value(supervisor, "decision", "supervisor_decision"),
                    created_at,
                    updated_at or now,
                ),
            )

    async def missing_original_threads(self) -> list[str]:
        return await asyncio.to_thread(self._missing_original_threads_sync)

    def _missing_original_threads_sync(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT streams.thread_id
                FROM workflow_event_streams AS streams
                LEFT JOIN workflow_registry AS registry
                  ON registry.thread_id = streams.thread_id
                WHERE streams.branch_id = 'original'
                  AND registry.thread_id IS NULL
                ORDER BY streams.thread_id
                """
            ).fetchall()
        return [str(row["thread_id"]) for row in rows]

    async def list(
        self,
        *,
        status: str | None,
        search: str | None,
        limit: int,
        offset: int,
        sort_by: str,
        sort_order: str,
    ) -> WorkflowListResponse:
        return await asyncio.to_thread(
            self._list_sync,
            status=status,
            search=search,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_order=sort_order,
        )

    def _list_sync(
        self,
        *,
        status: str | None,
        search: str | None,
        limit: int,
        offset: int,
        sort_by: str,
        sort_order: str,
    ) -> WorkflowListResponse:
        clauses: list[str] = []
        parameters: list[Any] = []
        if status == "waiting":
            clauses.append("interrupted = 1 AND pending_operation IS NOT NULL")
        elif status == "running":
            clauses.append("terminal_status = 'running' AND interrupted = 0")
        elif status == "pending":
            clauses.append(
                "terminal_status = 'pending' AND NOT (interrupted = 1 AND pending_operation IS NOT NULL)"
            )
        elif status:
            clauses.append("terminal_status = ?")
            parameters.append(status)
        normalized_search = (search or "").strip().lower()
        if normalized_search:
            clauses.append(
                "(LOWER(COALESCE(project_name, '')) LIKE ? "
                "OR LOWER(thread_id) LIKE ? "
                "OR LOWER(COALESCE(workflow_intent, '')) LIKE ?)"
            )
            pattern = f"%{normalized_search}%"
            parameters.extend((pattern, pattern, pattern))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        column = SORT_COLUMNS[sort_by]
        direction = SORT_ORDERS[sort_order]
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) AS total FROM workflow_registry{where}",
                    parameters,
                ).fetchone()["total"]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM workflow_registry{where}
                ORDER BY {column} {direction}, thread_id ASC
                LIMIT ? OFFSET ?
                """,
                [*parameters, limit, offset],
            ).fetchall()
        items = [
            WorkflowListItem(
                thread_id=row["thread_id"],
                project_name=row["project_name"],
                workflow_intent=row["workflow_intent"],
                terminal_status=row["terminal_status"],
                interrupted=bool(row["interrupted"]),
                pending_operation=row["pending_operation"],
                pending_tool=row["pending_tool"],
                tests_executed=bool(row["tests_executed"]),
                tests_passed=bool(row["tests_passed"]),
                test_summary=row["test_summary"],
                planning_attempts=row["planning_attempts"],
                implementation_attempts=row["implementation_attempts"],
                repair_phase=row["repair_phase"],
                repair_attempts=row["repair_attempts"],
                supervisor_decision=row["supervisor_decision"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]
        return WorkflowListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        )
