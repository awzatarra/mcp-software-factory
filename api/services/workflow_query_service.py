from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from api.models import WorkflowListResponse, WorkflowSnapshotResponse
from api.services.workflow_metadata_store import WorkflowMetadataStore
from api.services.workflow_knowledge import (
    planner_knowledge_summary,
    qa_knowledge_summary,
    repair_knowledge_summary,
    workflow_learning_summary,
)
from api.services.workflow_registry import WorkflowRegistry
from api.services.observability_service import ObservabilityService
from api.services.llm_cost_service import LLMCostService
from api.services.git_service import GitError, GitService
from graph.persistence_service import WorkflowPersistenceService
from streaming.store import WorkflowEventStore


def normalize_terminal_status(
    raw_status: str | None,
    *,
    interrupted: bool,
    next_nodes: Sequence[str],
    active: bool = False,
) -> str:
    if raw_status and raw_status.strip():
        return raw_status
    if interrupted or next_nodes:
        return "pending"
    if active:
        return "running"
    return "pending"


def _group(values: dict[str, Any], explicit: str, fields: tuple[str, ...]) -> dict[str, Any]:
    grouped = values.get(explicit)
    result: dict[str, Any] = {}
    if isinstance(grouped, dict):
        aliases = {
            "planning_valid": "valid",
            "planning_attempts": "attempts",
            "planning_errors": "errors",
            "planning_evaluation": "evaluation",
            "implementation_valid": "valid",
            "implementation_attempts": "attempts",
            "tests_passed": "passed",
            "tests_executed": "executed",
            "supervisor_decision": "decision",
            "supervisor_decision_source": "decision_source",
            "supervisor_confidence": "confidence",
            "supervisor_attempts": "attempts",
        }
        for field in fields:
            grouped_key = aliases.get(field, field)
            if grouped.get(grouped_key) is not None:
                result[grouped_key] = grouped[grouped_key]
    for field in fields:
        grouped_key = aliases.get(field, field) if isinstance(grouped, dict) else field
        if grouped_key not in result and values.get(field) is not None:
            result[grouped_key] = values[field]
    return result


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class WorkflowQueryService:
    def __init__(
        self,
        persistence: WorkflowPersistenceService,
        registry: WorkflowRegistry,
        event_store: WorkflowEventStore | None = None,
        metadata_store: WorkflowMetadataStore | None = None,
        observability: ObservabilityService | None = None,
        llm_costs: LLMCostService | None = None,
        git_service: GitService | None = None,
    ) -> None:
        self.persistence = persistence
        self.registry = registry
        self.event_store = event_store
        self.metadata_store = metadata_store
        self.observability = observability
        self.llm_costs = llm_costs
        self.git_service = git_service

    async def _durable_created_at(self, thread_id: str) -> datetime | None:
        if self.metadata_store is not None:
            created_at, _ = await self.metadata_store.event_timestamps(thread_id)
            return created_at
        if self.event_store is None:
            return None
        events = await self.event_store.get_events(
            thread_id,
            branch_id="original",
            limit=1,
        )
        if not events:
            return None
        return _parse_datetime(events[0].timestamp)

    async def get_snapshot(self, thread_id: str) -> WorkflowSnapshotResponse:
        snapshot = await self.persistence.get_snapshot(thread_id)
        values = snapshot.values
        execution = await self.registry.get(thread_id)
        project_id = values.get("created_project_name") or values.get("project_name")
        git_summary = await self._git_summary(thread_id, project_id)
        git_approval_pending = git_summary.get("commit_status") == "awaiting_approval"
        promotion = git_summary.get("promotion") or {}
        promotion_approval_pending = promotion.get("state") == "awaiting_approval"
        interrupted = bool(snapshot.interrupts) or git_approval_pending or promotion_approval_pending
        active = await self.registry.is_running(thread_id)
        if self.metadata_store is not None:
            durable_created_at, durable_updated_at = (
                await self.metadata_store.event_timestamps(thread_id)
            )
        else:
            durable_created_at = await self._durable_created_at(thread_id)
            durable_updated_at = None
        checkpoint_updated_at = _parse_datetime(snapshot.created_at)
        response = WorkflowSnapshotResponse(
            thread_id=thread_id,
            checkpoint_id=snapshot.checkpoint_id,
            project_name=project_id,
            workflow_intent=values.get("workflow_intent"),
            terminal_status=normalize_terminal_status(
                values.get("terminal_status"),
                interrupted=interrupted,
                next_nodes=snapshot.next_nodes,
                active=active,
            ),
            interrupted=interrupted,
            pending_operation=(
                "git_merge" if promotion_approval_pending else
                "git_commit" if git_approval_pending else values.get("pending_operation")
            ),
            pending_tool=(
                "git__merge_workflow_branch" if promotion_approval_pending else
                "git__commit" if git_approval_pending else values.get("pending_tool_name")
            ),
            planning=_group(
                values,
                "planning_result",
                ("planning_valid", "planning_attempts", "planning_errors", "planning_evaluation"),
            ),
            planner_knowledge=planner_knowledge_summary(values),
            implementation=_group(
                values,
                "implementation_result",
                (
                    "implementation_valid",
                    "implementation_attempts",
                    "project_created",
                    "environment_prepared",
                    "dependencies_installed",
                    "detected_framework",
                ),
            ),
            testing=_group(
                values,
                "testing_result",
                (
                    "tests_executed",
                    "tests_passed",
                    "repair_phase",
                    "repair_attempts",
                    "final_test_result_summary",
                    "detected_test_framework",
                ),
            ),
            agent_performance=(
                dict(values.get("agent_performance_evaluations") or {})
                if isinstance(values.get("agent_performance_evaluations"), dict)
                else {}
            ),
            failure_attribution=(
                dict(values.get("failure_attribution") or {})
                if isinstance(values.get("failure_attribution"), dict)
                else {}
            ),
            qa_knowledge=qa_knowledge_summary(values),
            repair_knowledge=repair_knowledge_summary(values),
            workflow_learning=workflow_learning_summary(values),
            supervisor=_group(
                values,
                "supervisor_result",
                (
                    "supervisor_decision",
                    "supervisor_decision_source",
                    "supervisor_confidence",
                    "supervisor_attempts",
                ),
            ),
            created_at=(
                durable_created_at
                or (execution.started_at if execution is not None else None)
                or checkpoint_updated_at
            ),
            updated_at=(
                execution.completed_at
                if execution is not None and execution.completed_at is not None
                else durable_updated_at or checkpoint_updated_at
            ),
            observability_summary=(
                await self.observability.workflow_summary(thread_id)
                if self.observability is not None
                else {"state": "not_available"}
            ),
            llm_cost_summary=(
                {"state": "available", **await self.llm_costs.summary(workflow_id=thread_id)}
                if self.llm_costs is not None
                else {"state": "unavailable"}
            ),
            git=git_summary,
        )
        if self.metadata_store is not None:
            await self.metadata_store.upsert(response)
            generated = values.get("generated_files")
            updated = values.get("files_updated_during_repair")
            await self.metadata_store.sync_project_files(
                thread_id,
                generated_files=(
                    [str(path) for path in generated]
                    if isinstance(generated, list)
                    else []
                ),
                updated_files=(
                    [str(path) for path in updated]
                    if isinstance(updated, list)
                    else []
                ),
            )
        return response

    async def _git_summary(
        self,
        thread_id: str,
        project_id: str | None,
    ) -> dict[str, Any]:
        if self.git_service is None:
            return {"state": "project_unavailable", "repository": False,
                    "branch": None, "head_commit": None, "clean": None,
                    "changed_files_count": 0}
        try:
            return (await self.git_service.workflow_summary(project_id, thread_id)).model_dump()
        except GitError:
            return {"state": "project_unavailable", "repository": False,
                    "branch": None, "head_commit": None, "clean": None,
                    "changed_files_count": 0}

    async def sync_snapshot(self, thread_id: str) -> None:
        try:
            await self.get_snapshot(thread_id)
        except Exception:
            # Registry synchronization must not turn a completed graph run into a failure.
            return

    async def register_workflow(self, thread_id: str, request: str) -> None:
        if self.metadata_store is not None:
            await self.metadata_store.register_created(thread_id, request)

    async def mark_running(self, thread_id: str) -> None:
        if self.metadata_store is not None:
            await self.metadata_store.mark_running(thread_id)

    async def mark_failed(self, thread_id: str) -> None:
        if self.metadata_store is not None:
            await self.metadata_store.mark_failed(thread_id)

    async def backfill_registry(self) -> None:
        if self.metadata_store is None:
            return
        for thread_id in await self.metadata_store.missing_original_threads():
            await self.sync_snapshot(thread_id)

    async def list_workflows(
        self,
        *,
        status: str | None,
        search: str | None,
        limit: int,
        offset: int,
        sort_by: str,
        sort_order: str,
    ) -> WorkflowListResponse:
        if self.metadata_store is None:
            return WorkflowListResponse(
                items=[], total=0, limit=limit, offset=offset, has_more=False
            )
        return await self.metadata_store.list(
            status=status,
            search=search,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_order=sort_order,
        )
