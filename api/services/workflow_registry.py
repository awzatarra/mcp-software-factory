from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class WorkflowExecutionStatus(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class WorkflowExecution:
    thread_id: str
    task: asyncio.Task[None] | None
    status: WorkflowExecutionStatus
    started_at: datetime
    completed_at: datetime | None = None
    error: str | None = None


class WorkflowRegistry:
    def __init__(self) -> None:
        self._executions: dict[str, WorkflowExecution] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        thread_id: str,
        *,
        started_at: datetime | None = None,
    ) -> WorkflowExecution:
        async with self._lock:
            existing = self._executions.get(thread_id)
            if existing is not None:
                return existing
            execution = WorkflowExecution(
                thread_id=thread_id,
                task=None,
                status=WorkflowExecutionStatus.RUNNING,
                started_at=started_at or datetime.now(UTC),
            )
            self._executions[thread_id] = execution
        return execution

    async def attach_task(self, thread_id: str, task: asyncio.Task[None]) -> None:
        async with self._lock:
            execution = self._executions[thread_id]
            execution.task = task
            execution.status = WorkflowExecutionStatus.RUNNING
            execution.completed_at = None
            execution.error = None

    async def get(self, thread_id: str) -> WorkflowExecution | None:
        async with self._lock:
            return self._executions.get(thread_id)

    async def mark_waiting(self, thread_id: str) -> None:
        async with self._lock:
            execution = self._executions[thread_id]
            execution.status = WorkflowExecutionStatus.WAITING
            execution.task = None

    async def mark_completed(self, thread_id: str) -> None:
        async with self._lock:
            execution = self._executions[thread_id]
            execution.status = WorkflowExecutionStatus.COMPLETED
            execution.completed_at = datetime.now(UTC)
            execution.task = None

    async def mark_failed(self, thread_id: str, error: str) -> None:
        async with self._lock:
            execution = self._executions[thread_id]
            execution.status = WorkflowExecutionStatus.FAILED
            execution.completed_at = datetime.now(UTC)
            execution.error = error[:1_000]
            execution.task = None

    async def is_running(self, thread_id: str) -> bool:
        execution = await self.get(thread_id)
        return bool(
            execution
            and execution.status == WorkflowExecutionStatus.RUNNING
            and execution.task is not None
            and not execution.task.done()
        )

    async def cancel_all(self) -> None:
        async with self._lock:
            tasks = [
                execution.task
                for execution in self._executions.values()
                if execution.task is not None and not execution.task.done()
            ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
