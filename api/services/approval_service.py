from __future__ import annotations

import asyncio

from api.errors import ApprovalConflictError
from api.models import ApprovalResponse
from api.services.workflow_registry import WorkflowRegistry
from api.services.workflow_runner import WorkflowRunner
from graph.persistence_service import (
    StaleApprovalReference,
    WorkflowAlreadyCompletedError,
    WorkflowNotInterruptedError,
    WorkflowPersistenceService,
)


class ApprovalLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get_lock(self, thread_id: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(thread_id, asyncio.Lock())


class ApprovalService:
    def __init__(
        self,
        *,
        persistence: WorkflowPersistenceService,
        registry: WorkflowRegistry,
        runner: WorkflowRunner,
        locks: ApprovalLockRegistry,
    ) -> None:
        self.persistence = persistence
        self.registry = registry
        self.runner = runner
        self.locks = locks

    async def resolve(
        self,
        *,
        thread_id: str,
        approved: bool,
        reason: str | None,
    ) -> ApprovalResponse:
        lock = await self.locks.get_lock(thread_id)
        async with lock:
            if await self.registry.is_running(thread_id):
                raise ApprovalConflictError("Approval is already being resolved.")
            try:
                reference = await self.persistence.load_current_pending_approval(thread_id)
            except (
                WorkflowAlreadyCompletedError,
                WorkflowNotInterruptedError,
                StaleApprovalReference,
            ) as exc:
                raise ApprovalConflictError("Workflow is not waiting for approval.") from exc
            await self.runner.schedule_resume(
                reference=reference,
                approved=approved,
                reason=reason,
            )
            return ApprovalResponse(
                thread_id=thread_id,
                accepted=True,
                operation=reference.operation,
                tool_name=reference.tool_name,
                status="running",
            )
