from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from api.services.event_broker import WorkflowEventForwarder
from api.services.workflow_registry import WorkflowExecutionStatus, WorkflowRegistry
from api.services.observability_context import observability_context
from api.services.observability_service import ObservabilityService
from graph.persistence_service import (
    PendingApprovalReference,
    StaleApprovalReference,
    WorkflowNotInterruptedError,
    WorkflowPersistenceService,
)
from graph.runtime import run_software_factory_graph
from streaming import (
    EventStatus,
    InMemoryWorkflowEventEmitter,
    WorkflowEventFactory,
    WorkflowEventType,
    emit_workflow_event,
    workflow_event_context,
)

logger = logging.getLogger(__name__)


class WorkflowRunner:
    def __init__(
        self,
        *,
        graph,
        persistence: WorkflowPersistenceService,
        registry: WorkflowRegistry,
        emitter: InMemoryWorkflowEventEmitter,
        event_factory: WorkflowEventFactory,
        forwarder: WorkflowEventForwarder,
        on_transition: Callable[[str], Awaitable[None]] | None = None,
        on_resume: Callable[[str], Awaitable[None]] | None = None,
        on_failure: Callable[[str], Awaitable[None]] | None = None,
        observability: ObservabilityService | None = None,
    ) -> None:
        self.graph = graph
        self.persistence = persistence
        self.registry = registry
        self.emitter = emitter
        self.event_factory = event_factory
        self.forwarder = forwarder
        self.on_transition = on_transition
        self.on_resume = on_resume
        self.on_failure = on_failure
        self.observability = observability

    async def schedule_start(self, *, thread_id: str, request: str) -> None:
        await self.registry.register(thread_id)
        self.forwarder.attach(thread_id)
        if self.observability is not None:
            context = await self.observability.ensure_trace(thread_id)
            with observability_context(context):
                task = asyncio.create_task(self.start(thread_id=thread_id, request=request))
        else:
            task = asyncio.create_task(self.start(thread_id=thread_id, request=request))
        await self.registry.attach_task(thread_id, task)

    async def schedule_resume(
        self,
        *,
        reference: PendingApprovalReference,
        approved: bool,
        reason: str | None,
    ) -> None:
        if await self.registry.get(reference.thread_id) is None:
            await self.registry.register(reference.thread_id)
        self.forwarder.attach(reference.thread_id)
        if self.on_resume is not None:
            await self.on_resume(reference.thread_id)
        coroutine = self.resume_approval(
            thread_id=reference.thread_id, approved=approved, reason=reason,
            expected_reference=reference,
        )
        if self.observability is not None:
            context = await self.observability.ensure_trace(reference.thread_id)
            with observability_context(context):
                task = asyncio.create_task(coroutine)
        else:
            task = asyncio.create_task(coroutine)
        await self.registry.attach_task(reference.thread_id, task)

    async def start(self, *, thread_id: str, request: str) -> None:
        try:
            result = await run_software_factory_graph(
                self.graph,
                request,
                thread_id=thread_id,
                streaming_enabled=True,
                event_emitter=self.emitter,
                event_factory=self.event_factory,
            )
            await self._record_result(thread_id, result)
        except asyncio.CancelledError:
            await self._finalize_observability(
                thread_id, "cancelled", reason="workflow_task_cancelled"
            )
            raise
        except Exception as exc:
            await self._record_failure(thread_id, exc)

    async def resume_approval(
        self,
        *,
        thread_id: str,
        approved: bool,
        reason: str | None,
        expected_reference: PendingApprovalReference | None = None,
    ) -> None:
        try:
            if approved:
                result = await self.persistence.approve(
                    thread_id,
                    reason,
                    expected_reference=expected_reference,
                )
            else:
                result = await self.persistence.reject(
                    thread_id,
                    reason,
                    expected_reference=expected_reference,
                )
            await self._record_result(thread_id, result)
        except (StaleApprovalReference, WorkflowNotInterruptedError):
            await self.registry.mark_waiting(thread_id)
            logger.info("approval conflict thread=%s", thread_id)
        except asyncio.CancelledError:
            await self._finalize_observability(
                thread_id, "cancelled", reason="workflow_resume_cancelled"
            )
            raise
        except Exception as exc:
            await self._record_failure(thread_id, exc)

    async def record_recovery_result(self, thread_id: str, result) -> None:
        if await self.registry.get(thread_id) is None:
            await self.registry.register(thread_id)
        await self._record_result(thread_id, result)

    async def _record_result(self, thread_id: str, result) -> None:
        if result.interrupted:
            await self.registry.mark_waiting(thread_id)
            await self._sync_transition(thread_id)
            return
        if result.final_state.get("terminal_status") == "completed":
            await self._finalize_observability(
                thread_id, "completed", reason="workflow_runner_completed"
            )
            await self.registry.mark_completed(thread_id)
            await self._sync_transition(thread_id)
            logger.info("workflow completed thread=%s", thread_id)
            return
        terminal_status = result.final_state.get("terminal_status")
        observability_status = "cancelled" if terminal_status == "user_cancelled" else "failed"
        await self._finalize_observability(
            thread_id,
            observability_status,
            reason="workflow_runner_cancelled" if observability_status == "cancelled" else "workflow_runner_failed",
        )
        await self.registry.mark_failed(
            thread_id,
            str(result.final_state.get("failure_message") or result.final_state.get("terminal_status")),
        )
        await self._sync_transition(thread_id)

    async def _record_failure(self, thread_id: str, exc: Exception) -> None:
        terminal_exists = any(
            event.type in {
                WorkflowEventType.WORKFLOW_COMPLETED,
                WorkflowEventType.WORKFLOW_FAILED,
            }
            for event in self.emitter.get_events(thread_id)
        )
        if not terminal_exists:
            with workflow_event_context(
                thread_id=thread_id,
                emitter=self.emitter,
                factory=self.event_factory,
                lineage={"lineage": "original", "branch_id": "original"},
            ):
                emit_workflow_event(
                    WorkflowEventType.WORKFLOW_FAILED,
                    source="api.workflow_runner",
                    stage="runtime",
                    status=EventStatus.FAILED,
                    data={"failure_type": type(exc).__name__},
                )
        await self._finalize_observability(
            thread_id,
            "failed",
            reason="workflow_runner_exception",
            failure=exc,
        )
        await self.registry.mark_failed(thread_id, type(exc).__name__)
        if self.on_failure is not None:
            await self.on_failure(thread_id)
        else:
            await self._sync_transition(thread_id)
        logger.exception("workflow failed thread=%s", thread_id)

    async def _finalize_observability(
        self,
        thread_id: str,
        status: str,
        *,
        reason: str,
        failure: BaseException | None = None,
    ) -> None:
        if self.observability is None:
            return
        await self.observability.finalize_workflow_trace(
            thread_id,
            status,
            reason=reason,
            failure=failure,
        )

    async def _sync_transition(self, thread_id: str) -> None:
        if self.on_transition is not None:
            await self.on_transition(thread_id)
