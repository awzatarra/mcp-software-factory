from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from langgraph.types import Command

from graph.ci_reconciliation import build_ci_success_reconciliation
from graph.runtime import (
    WorkflowInterrupt,
    WorkflowRunResult,
    active_snapshot,
    active_snapshot_values,
    normalize_interrupts,
    thread_config,
    workflow_result_from_invocation,
)
from graph.state import SoftwareFactoryState
from streaming import (
    EventStatus,
    WorkflowEventEmitter,
    WorkflowEventFactory,
    WorkflowEventType,
    default_event_emitter,
    default_event_factory,
    emit_workflow_event,
    workflow_event_context,
)


@dataclass(frozen=True)
class PendingApprovalReference:
    thread_id: str
    checkpoint_id: str
    operation: str
    tool_name: str


class WorkflowNotFoundError(LookupError):
    def __init__(self, thread_id: str) -> None:
        super().__init__(f"No existe un workflow con thread ID: {thread_id}")
        self.thread_id = thread_id


class WorkflowAlreadyCompletedError(RuntimeError):
    pass


class WorkflowNotInterruptedError(RuntimeError):
    pass


TERMINAL_STATUSES = {
    "completed",
    "infrastructure_failed",
    "tests_failed",
    "repair_limit_reached",
    "user_cancelled",
    "planning_failed",
    "implementation_failed",
    "supervisor_loop_detected",
}


def _has_pending_approval(values: dict[str, Any], interrupts: tuple[WorkflowInterrupt, ...]) -> bool:
    return bool(
        interrupts
        and values.get("pending_operation") is not None
        and values.get("pending_tool_name") is not None
    )


def _is_effectively_terminal(
    values: dict[str, Any],
    interrupts: tuple[WorkflowInterrupt, ...],
) -> bool:
    return (
        values.get("terminal_status") in TERMINAL_STATUSES
        and not _has_pending_approval(values, interrupts)
    )


class StaleApprovalReference(RuntimeError):
    def __init__(
        self,
        expected: PendingApprovalReference,
        current: PendingApprovalReference,
    ) -> None:
        self.expected = expected
        self.current = current
        super().__init__(
            "La referencia de aprobación ya no corresponde al head actual: "
            f"expected={expected}, current={current}."
        )


class CheckpointNotFoundError(LookupError):
    pass


class CheckpointThreadMismatchError(LookupError):
    pass


class UnsafeReplayError(RuntimeError):
    pass


class InvalidForkUpdateError(ValueError):
    def __init__(self, rejected_fields: dict[str, str]) -> None:
        self.rejected_fields = dict(rejected_fields)
        details = "; ".join(f"{field}: {reason}" for field, reason in sorted(rejected_fields.items()))
        super().__init__(f"El fork contiene campos inválidos: {details}")


class AmbiguousForkNodeError(RuntimeError):
    pass


FORK_EDITABLE_FIELDS_BY_SUBGRAPH = {
    "planning": frozenset(),
    "implementation": frozenset(),
    "testing_repair": frozenset(
        {
            "tests_executed",
            "tests_passed",
            "repair_decision",
            "repair_before",
            "repair_after",
            "repair_phase",
            "repair_attempts",
            "failure_type",
            "failure_stage",
            "failure_message",
            "failing_test_files",
        }
    ),
}
FORK_EDITABLE_FIELDS = set().union(*FORK_EDITABLE_FIELDS_BY_SUBGRAPH.values())
REPAIR_PHASES = {
    "not_started",
    "read_failing_test",
    "read_related_source",
    "apply_fix",
    "rerun_tests",
    "completed",
}
MAX_FORK_UPDATE_BYTES = 32 * 1024
SAFE_REPLAY_NEXT_NODES = {
    "supervisor",
    "supervisor_loop_probe",
    "prepare_planner_knowledge",
    "implementation",
    "prepare_developer_knowledge",
    "testing_repair",
    "prepare_qa_knowledge",
    "prepare_repair_knowledge",
    "prepare_test_request",
    "approval",
    "execute_tests",
    "run_tests",
    "read_failing_test",
    "read_related_source",
    "prepare_fix",
    "finalize",
}
UNSAFE_REPLAY_NEXT_NODES = {
    "prepare_create_project",
    "execute_create_project",
    "prepare_environment_request",
    "execute_prepare_environment",
    "execute_fix",
    "git_workflow",
    "git_approval",
    "execute_git_commit",
    "reject_git_commit",
    "git_promotion",
    "git_promotion_approval",
    "execute_git_promotion",
    "reject_git_promotion",
}


@dataclass(frozen=True)
class CheckpointDetails:
    thread_id: str
    checkpoint_id: str
    step: int | None
    source: str | None
    next_nodes: tuple[str, ...]
    values: dict[str, Any]
    metadata: dict[str, Any]
    interrupted: bool


@dataclass(frozen=True)
class ReplayPlan:
    thread_id: str
    checkpoint_id: str
    next_nodes: tuple[str, ...]
    nodes_that_may_reexecute: tuple[str, ...]
    sensitive_operations: tuple[str, ...]
    allowed: bool
    rejection_reason: str | None


@dataclass(frozen=True)
class ForkPreview:
    thread_id: str
    origin_checkpoint_id: str
    current_values: dict[str, Any]
    new_values: dict[str, Any]
    allowed_fields: tuple[str, ...]
    rejected_fields: tuple[str, ...]
    rejection_reasons: dict[str, str]
    as_node: str | None
    next_nodes: tuple[str, ...]
    allowed: bool
    reason: str | None = None
    domain: str | None = None

    @property
    def disallowed_fields(self) -> tuple[str, ...]:
        return tuple(field for field in self.rejected_fields if not field.startswith("$"))


@dataclass(frozen=True)
class WorkflowForkResult:
    thread_id: str
    origin_checkpoint_id: str
    fork_checkpoint_id: str
    updated_fields: tuple[str, ...]
    next_nodes: tuple[str, ...]
    run_result: WorkflowRunResult | None


@dataclass(frozen=True)
class WorkflowSnapshot:
    thread_id: str
    checkpoint_id: str | None
    next_nodes: tuple[str, ...]
    values: SoftwareFactoryState
    metadata: dict[str, Any]
    created_at: str | None
    interrupts: tuple[WorkflowInterrupt, ...] = ()


@dataclass(frozen=True)
class WorkflowHistoryItem:
    checkpoint_id: str | None
    next_nodes: tuple[str, ...]
    source: str | None
    step: int | None
    node_writes: dict[str, tuple[str, ...]]
    terminal_status: str | None
    project_name: str | None
    tests_passed: bool
    repair_phase: str | None
    lineage: str = "original"
    fork_origin_checkpoint_id: str | None = None
    fork_updated_fields: tuple[str, ...] = ()
    approval_reason: str | None = None
    last_rejected_tool: str | None = None
    result: str | None = None
    checkpoint_namespace: str = ""


def _checkpoint_id(config: dict[str, Any] | None) -> str | None:
    configurable = (config or {}).get("configurable", {})
    value = configurable.get("checkpoint_id") if isinstance(configurable, dict) else None
    return str(value) if value else None


def checkpoint_config(
    thread_id: str,
    checkpoint_id: str,
    checkpoint_ns: str = "",
) -> dict[str, dict[str, Any]]:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }
    }


def _snapshot_exists(snapshot: Any) -> bool:
    return bool(snapshot.values or snapshot.metadata is not None or snapshot.created_at is not None or _checkpoint_id(snapshot.config))


def _active_interrupt_checkpoint_id(snapshot: Any) -> str | None:
    for task in getattr(snapshot, "tasks", ()) or ():
        child = getattr(task, "state", None)
        if child is None or isinstance(child, dict) or not hasattr(child, "values"):
            continue
        child_checkpoint = _active_interrupt_checkpoint_id(child)
        if child_checkpoint is not None:
            return child_checkpoint
    if normalize_interrupts(getattr(snapshot, "interrupts", ())):
        return _checkpoint_id(getattr(snapshot, "config", None))
    return None


def _snapshot_interrupts(snapshot: Any) -> tuple[WorkflowInterrupt, ...]:
    direct = normalize_interrupts(getattr(snapshot, "interrupts", ()))
    if direct:
        return direct
    collected: list[Any] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        collected.extend(getattr(task, "interrupts", ()) or ())
        child = getattr(task, "state", None)
        if child is not None and not isinstance(child, dict) and hasattr(child, "values"):
            collected.extend(_snapshot_interrupts(child))
    return normalize_interrupts(collected)


def _summarize_writes(metadata: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    writes = metadata.get("writes")
    if not isinstance(writes, dict):
        return {}
    summary: dict[str, tuple[str, ...]] = {}
    for node, value in writes.items():
        if isinstance(value, dict):
            keys = tuple(sorted(key for key in value if key not in {"test_stdout", "test_stderr"}))
        else:
            keys = (type(value).__name__,)
        summary[str(node)] = keys
    return summary


def _step(metadata: dict[str, Any]) -> int | None:
    value = metadata.get("step")
    return value if isinstance(value, int) else None


def _lineage(metadata: dict[str, Any], values: dict[str, Any]) -> str:
    source = metadata.get("source")
    if values.get("fork_lineage") == "fork":
        return "fork"
    if source == "update" or values.get("fork_origin_checkpoint_id"):
        return "fork/update"
    if source == "fork":
        return "replay"
    return "original"


def _presented_source(metadata: dict[str, Any], values: dict[str, Any]) -> str | None:
    if values.get("fork_lineage") == "fork":
        return "fork"
    source = metadata.get("source")
    return str(source) if source is not None else None


def _normalize_test_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized == "."
        or normalized.startswith("/")
        or not path.parts
        or ":" in path.parts[0]
        or ".." in path.parts
    ):
        raise ValueError("debe ser una ruta relativa sin traversal")
    return path.as_posix()


def validate_fork_updates(
    updates: dict[str, Any],
    *,
    allowed_fields: frozenset[str] | set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    if not isinstance(updates, dict):
        raise InvalidForkUpdateError({"$": "el payload debe ser un objeto JSON"})
    try:
        payload_size = len(json.dumps(updates, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise InvalidForkUpdateError({"$": "el payload debe ser JSON serializable"}) from exc
    if payload_size > MAX_FORK_UPDATE_BYTES:
        raise InvalidForkUpdateError({"$": f"el payload excede {MAX_FORK_UPDATE_BYTES} bytes"})

    validated: dict[str, Any] = {}
    rejected: dict[str, str] = {}
    effective_allowed_fields = FORK_EDITABLE_FIELDS if allowed_fields is None else set(allowed_fields)
    nullable_strings = {
        "repair_decision",
        "repair_before",
        "repair_after",
        "failure_type",
        "failure_stage",
        "failure_message",
    }
    for field, value in updates.items():
        if field not in effective_allowed_fields:
            rejected[field] = "campo no editable"
            continue
        if field in {"tests_executed", "tests_passed"}:
            if not isinstance(value, bool):
                rejected[field] = "debe ser bool"
            else:
                validated[field] = value
            continue
        if field == "repair_attempts":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                rejected[field] = "debe ser int mayor o igual que cero"
            else:
                validated[field] = value
            continue
        if field in nullable_strings:
            if value is not None and not isinstance(value, str):
                rejected[field] = "debe ser str o null"
            else:
                validated[field] = value
            continue
        if field == "repair_phase":
            if not isinstance(value, str) or value not in REPAIR_PHASES:
                rejected[field] = "fase de reparación inválida"
            else:
                validated[field] = value
            continue
        if field == "failing_test_files":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                rejected[field] = "debe ser list[str]"
                continue
            try:
                validated[field] = [_normalize_test_path(item) for item in value]
            except ValueError as exc:
                rejected[field] = str(exc)
    return validated, rejected


def _fork_domain(details: CheckpointDetails) -> str | None:
    namespace = str(details.metadata.get("_checkpoint_ns") or "")
    if namespace:
        root_name = namespace.split(":", 1)[0]
        if root_name in FORK_EDITABLE_FIELDS_BY_SUBGRAPH:
            return root_name
        if root_name == "testing_repair":
            return "testing_repair"

    if details.next_nodes == ("supervisor",):
        writes = details.metadata.get("writes")
        producers = set(writes) if isinstance(writes, dict) else set()
        last = details.values.get("last_completed_stage") or details.values.get("last_completed_node")
        if "testing_repair" in producers or last == "testing_repair":
            return "testing_repair"
        if "implementation" in producers or last == "implementation":
            return "implementation"
        if "planning" in producers or last == "planning":
            return "planning"

    domain_nodes = {
        "planning": {
            "planning",
            "analyze_requirement",
            "prepare_planner_knowledge",
            "create_tasks",
            "validate_planning",
            "refine_planning",
            "planning_hybrid_evaluation",
        },
        "implementation": {
            "implementation",
            "prepare_developer_knowledge",
            "prepare_create_project",
            "normalize_dependencies",
            "validate_implementation",
            "execute_create_project",
            "detect_test_framework",
            "prepare_environment_request",
            "execute_prepare_environment",
        },
        "testing_repair": {
            "testing_repair",
            "prepare_qa_knowledge",
            "prepare_repair_knowledge",
            "prepare_test_request",
            "approval",
            "execute_tests",
            "run_tests",
            "read_failing_test",
            "read_related_source",
            "prepare_fix",
            "execute_fix",
            "finalize",
        },
    }
    for domain, nodes in domain_nodes.items():
        if any(node in nodes for node in details.next_nodes):
            return domain
    return None


def _reexecution_nodes(next_nodes: tuple[str, ...], values: dict[str, Any]) -> tuple[str, ...]:
    if not next_nodes:
        return ()
    node = next_nodes[0]
    paths = {
        "prepare_planner_knowledge": (
            "prepare_planner_knowledge",
            "create_tasks",
            "validate_plan",
            "refine_plan",
            "planning_judge",
            "planning_hybrid_evaluation",
            "supervisor",
            "finalize",
            "planner_evaluation",
            "agent_performance_evaluation",
            "failure_attribution",
        ),
        "supervisor_loop_probe": (
            "supervisor_loop_probe",
            "supervisor",
            "finalize",
            "planner_evaluation",
            "agent_performance_evaluation",
            "failure_attribution",
        ),
        "supervisor": (
            "supervisor",
            "planning",
            "prepare_planner_knowledge",
            "planning_judge",
            "planning_hybrid_evaluation",
            "inspect_workspace",
            "implementation",
            "testing_repair",
            "prepare_qa_knowledge",
            "finalize",
            "planner_evaluation",
            "agent_performance_evaluation",
            "failure_attribution",
        ),
        "implementation": (
            "implementation",
            "prepare_developer_knowledge",
            "prepare_create_project",
            "normalize_dependencies",
            "validate_implementation",
            "approval",
            "execute_create_project",
            "detect_test_framework",
            "prepare_environment_request",
            "execute_prepare_environment",
            "testing_repair",
            "prepare_qa_knowledge",
            "finalize",
            "planner_evaluation",
            "agent_performance_evaluation",
            "failure_attribution",
        ),
        "testing_repair": (
            "testing_repair",
            "prepare_qa_knowledge",
            "prepare_test_request",
            "approval",
            "execute_tests",
            "finalize",
            "planner_evaluation",
            "agent_performance_evaluation",
            "failure_attribution",
        ),
        "prepare_qa_knowledge": (
            "prepare_qa_knowledge",
            "prepare_test_request",
            "approval",
            "execute_tests",
            "finalize",
            "planner_evaluation",
            "agent_performance_evaluation",
            "failure_attribution",
        ),
        "prepare_repair_knowledge": (
            "prepare_repair_knowledge",
            "prepare_fix",
            "approval",
            "execute_fix",
            "prepare_qa_knowledge",
            "prepare_test_request",
            "approval",
            "execute_tests",
            "finalize",
            "planner_evaluation",
            "agent_performance_evaluation",
        ),
        "prepare_test_request": ("prepare_test_request", "approval", "execute_tests", "finalize", "planner_evaluation", "agent_performance_evaluation", "failure_attribution"),
        "approval": ("approval", "execute_tests", "finalize", "planner_evaluation", "agent_performance_evaluation", "failure_attribution"),
        "execute_tests": ("execute_tests", "finalize", "planner_evaluation", "agent_performance_evaluation", "failure_attribution"),
        "run_tests": ("run_tests", "finalize", "planner_evaluation", "agent_performance_evaluation", "failure_attribution"),
        "read_failing_test": (
            "read_failing_test",
            "read_related_source",
            "prepare_repair_knowledge",
            "prepare_fix",
            "approval",
        ),
        "read_related_source": (
            "read_related_source",
            "prepare_repair_knowledge",
            "prepare_fix",
            "approval",
        ),
        "prepare_fix": ("prepare_fix", "approval"),
        "planning_hybrid_evaluation": ("planning_hybrid_evaluation", "supervisor", "finalize", "planner_evaluation", "agent_performance_evaluation", "failure_attribution"),
        "finalize": ("finalize", "planner_evaluation", "agent_performance_evaluation", "failure_attribution"),
        "planner_evaluation": ("planner_evaluation", "agent_performance_evaluation", "failure_attribution"),
        "agent_performance_evaluation": ("agent_performance_evaluation", "failure_attribution"),
        "failure_attribution": ("failure_attribution",),
    }
    return paths.get(node, next_nodes)


def _sensitive_operations(nodes: tuple[str, ...], values: dict[str, Any]) -> tuple[str, ...]:
    operations: list[str] = []
    if any(node in nodes for node in {"implementation", "prepare_developer_knowledge", "prepare_create_project", "execute_create_project"}):
        operations.append("create_project")
    if any(
        node in nodes
        for node in {"implementation", "detect_test_framework", "prepare_environment_request", "execute_prepare_environment"}
    ):
        operations.append("prepare_environment")
    if any(node in nodes for node in {"testing_repair", "prepare_qa_knowledge", "prepare_test_request", "approval", "execute_tests", "run_tests"}):
        operations.append("run_tests")
    if any(
        node in nodes
        for node in {
            "read_failing_test",
            "read_related_source",
            "prepare_repair_knowledge",
            "prepare_fix",
        }
    ):
        operations.append("apply_fix")
    pending = values.get("pending_operation")
    if isinstance(pending, str) and pending not in operations:
        operations.append(pending)
    return tuple(operations)


def _resolve_fork_as_node(details: CheckpointDetails) -> str:
    if details.next_nodes == ("supervisor",):
        writes = details.metadata.get("writes")
        if isinstance(writes, dict):
            producers = [str(name) for name in writes if name not in {"__input__", "__start__"}]
            if len(producers) == 1 and producers[0] in {
                "planning",
                "inspect_workspace",
                "implementation",
                "testing_repair",
            }:
                return producers[0]
        last = details.values.get("last_completed_stage") or details.values.get("last_completed_node")
        if last in {"planning", "inspect_workspace", "implementation", "testing_repair"}:
            return str(last)
    if details.next_nodes == ("prepare_test_request",):
        return "__start__"
    if details.next_nodes == ("prepare_fix",):
        return "read_related_source"
    if details.next_nodes == ("read_failing_test",):
        writes = details.metadata.get("writes")
        if isinstance(writes, dict):
            producers = [str(name) for name in writes if name not in {"__start__", "__input__"}]
            if len(producers) == 1 and producers[0] in {"execute_tests", "run_tests"}:
                return "execute_tests" if producers[0] == "run_tests" else producers[0]
        if details.metadata.get("source") == "loop" and details.values.get("last_completed_node") == "run_tests":
            return "execute_tests"
    raise AmbiguousForkNodeError(
        f"No se puede inferir as_node de forma segura para next={list(details.next_nodes)}."
    )


class WorkflowPersistenceService:
    def __init__(
        self,
        graph: Any,
        *,
        streaming_enabled: bool = False,
        event_emitter: WorkflowEventEmitter | None = None,
        event_factory: WorkflowEventFactory | None = None,
    ) -> None:
        self._graph = graph
        self.streaming_enabled = streaming_enabled
        self._event_emitter = event_emitter or default_event_emitter
        self._event_factory = event_factory or default_event_factory
        self._resolved_approval_keys: set[tuple[str, str, str, str, str]] = set()

    def _stream_context(self, thread_id: str, lineage: dict[str, Any]):
        if not self.streaming_enabled:
            return nullcontext()
        return workflow_event_context(
            thread_id=thread_id,
            emitter=self._event_emitter,
            factory=self._event_factory,
            lineage=lineage,
        )

    @staticmethod
    def _emit_terminal(result: WorkflowRunResult) -> None:
        if result.interrupted or result.already_completed:
            return
        completed = result.final_state.get("terminal_status") == "completed"
        emit_workflow_event(
            WorkflowEventType.WORKFLOW_COMPLETED if completed else WorkflowEventType.WORKFLOW_FAILED,
            source="graph.persistence",
            stage="finalize",
            status=EventStatus.COMPLETED if completed else EventStatus.FAILED,
            data={"terminal_status": result.final_state.get("terminal_status")},
        )

    async def get_snapshot(self, thread_id: str) -> WorkflowSnapshot:
        snapshot = await active_snapshot(self._graph, thread_config(thread_id))
        if not _snapshot_exists(snapshot):
            raise WorkflowNotFoundError(thread_id)
        values = active_snapshot_values(snapshot)
        metadata = dict(snapshot.metadata or {})
        result = WorkflowSnapshot(
            thread_id=thread_id,
            checkpoint_id=_checkpoint_id(snapshot.config),
            next_nodes=tuple(snapshot.next),
            values=values,
            metadata=metadata,
            created_at=snapshot.created_at,
            interrupts=_snapshot_interrupts(snapshot),
        )
        print(f"Checkpoint recuperado: {result.checkpoint_id or 'n/a'}")
        print(f"Next nodes: {list(result.next_nodes)}")
        return result

    async def _stored_checkpoint_config(self, checkpoint_id: str) -> dict[str, Any] | None:
        checkpointer = getattr(self._graph, "checkpointer", None)
        if checkpointer is None or not hasattr(checkpointer, "alist"):
            return None
        async for checkpoint_tuple in checkpointer.alist(None):
            config = getattr(checkpoint_tuple, "config", None)
            if _checkpoint_id(config) == checkpoint_id:
                return dict(config or {})
        return None

    async def get_checkpoint(self, thread_id: str, checkpoint_id: str) -> CheckpointDetails:
        await self.get_snapshot(thread_id)
        requested_config = checkpoint_config(thread_id, checkpoint_id)
        snapshot = await self._graph.aget_state(requested_config)
        if not (snapshot.values or snapshot.metadata is not None or snapshot.created_at is not None):
            stored_config = await self._stored_checkpoint_config(checkpoint_id)
            configurable = (stored_config or {}).get("configurable", {})
            owner = configurable.get("thread_id") if isinstance(configurable, dict) else None
            if owner is not None and owner != thread_id:
                raise CheckpointThreadMismatchError(
                    f"El checkpoint {checkpoint_id} pertenece al thread {owner}, no a {thread_id}."
                )
            if stored_config is not None:
                requested_config = stored_config
                snapshot = await self._graph.aget_state(requested_config)
        if not (snapshot.values or snapshot.metadata is not None or snapshot.created_at is not None):
            raise CheckpointNotFoundError(
                f"No existe el checkpoint {checkpoint_id} en el thread {thread_id}."
            )
        actual_id = _checkpoint_id(snapshot.config)
        if actual_id != checkpoint_id:
            raise CheckpointNotFoundError(
                f"No existe el checkpoint {checkpoint_id} en el thread {thread_id}."
            )
        metadata = dict(snapshot.metadata or {})
        configurable = (snapshot.config or {}).get("configurable", {})
        metadata["_checkpoint_ns"] = (
            str(configurable.get("checkpoint_ns", "")) if isinstance(configurable, dict) else ""
        )
        if isinstance(configurable, dict) and isinstance(configurable.get("checkpoint_map"), dict):
            metadata["_checkpoint_map"] = dict(configurable["checkpoint_map"])
        return CheckpointDetails(
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            step=_step(metadata),
            source=_presented_source(metadata, dict(snapshot.values)),
            next_nodes=tuple(snapshot.next),
            values=dict(snapshot.values),
            metadata=metadata,
            interrupted=bool(_snapshot_interrupts(snapshot)),
        )

    async def get_history(self, thread_id: str, limit: int = 20) -> list[WorkflowHistoryItem]:
        if limit < 1:
            raise ValueError("limit debe ser mayor que cero")
        await self.get_snapshot(thread_id)
        snapshots: list[Any] = []
        checkpointer = getattr(self._graph, "checkpointer", None)
        if checkpointer is not None and hasattr(checkpointer, "alist"):
            seen: set[tuple[str, str]] = set()
            configs: list[dict[str, Any]] = []
            async for checkpoint_tuple in checkpointer.alist(None):
                config = getattr(checkpoint_tuple, "config", None) or {}
                configurable = config.get("configurable", {})
                if not isinstance(configurable, dict) or str(configurable.get("thread_id")) != thread_id:
                    continue
                checkpoint_id = str(configurable.get("checkpoint_id") or "")
                namespace = str(configurable.get("checkpoint_ns") or "")
                key = (namespace, checkpoint_id)
                if not checkpoint_id or key in seen:
                    continue
                seen.add(key)
                configs.append(config)
            for config in configs:
                snapshot = await self._graph.aget_state(config)
                if snapshot.values or snapshot.metadata is not None or snapshot.created_at is not None:
                    snapshots.append(snapshot)
            snapshots.sort(key=lambda item: item.created_at or "", reverse=True)
        else:
            snapshots = [
                snapshot
                async for snapshot in self._graph.aget_state_history(thread_config(thread_id), limit=limit)
            ]
        history: list[WorkflowHistoryItem] = []
        for snapshot in snapshots[:limit]:
            values = dict(snapshot.values)
            metadata = dict(snapshot.metadata or {})
            step = metadata.get("step")
            lineage = _lineage(metadata, values)
            terminal_status = values.get("terminal_status")
            configurable = (snapshot.config or {}).get("configurable", {})
            namespace = str(configurable.get("checkpoint_ns") or "") if isinstance(configurable, dict) else ""
            history.append(
                WorkflowHistoryItem(
                    checkpoint_id=_checkpoint_id(snapshot.config),
                    next_nodes=tuple(snapshot.next),
                    source=_presented_source(metadata, values),
                    step=int(step) if isinstance(step, int) else None,
                    node_writes=_summarize_writes(metadata),
                    terminal_status=terminal_status,
                    project_name=values.get("created_project_name") or values.get("project_name"),
                    tests_passed=bool(values.get("tests_passed")),
                    repair_phase=values.get("repair_phase"),
                    lineage=lineage,
                    fork_origin_checkpoint_id=values.get("fork_origin_checkpoint_id"),
                    fork_updated_fields=tuple(values.get("fork_updated_fields") or ()),
                    approval_reason=values.get("approval_reason"),
                    last_rejected_tool=values.get("last_rejected_tool"),
                    result="alternative_rejected"
                    if lineage in {"fork", "fork/update"} and terminal_status == "user_cancelled"
                    else None,
                    checkpoint_namespace=namespace,
                )
            )
        return history

    async def build_replay_plan(self, thread_id: str, checkpoint_id: str) -> ReplayPlan:
        details = await self.get_checkpoint(thread_id, checkpoint_id)
        next_nodes = details.next_nodes
        nodes = _reexecution_nodes(next_nodes, details.values)
        sensitive = _sensitive_operations(nodes, details.values)
        rejection: str | None = None
        if len(next_nodes) > 1:
            rejection = "El checkpoint tiene múltiples nodos siguientes y no puede reproducirse de forma conservadora."
        elif next_nodes and next_nodes[0] in UNSAFE_REPLAY_NEXT_NODES:
            rejection = f"Replay bloqueado: {next_nodes[0]} puede repetir una operación sensible con efectos externos."
        elif next_nodes and next_nodes[0] not in SAFE_REPLAY_NEXT_NODES:
            rejection = f"Replay bloqueado: el nodo {next_nodes[0]} no está en la política segura."
        elif next_nodes == ("approval",) and (
            details.values.get("pending_operation") != "run_tests"
            or details.values.get("pending_approval_status") != "waiting"
        ):
            rejection = "Replay bloqueado: approval no representa una aprobación nueva y pendiente de run_tests."
        elif next_nodes and next_nodes[0] in {"execute_tests", "run_tests"}:
            if details.values.get("pending_approval_status") == "approved":
                rejection = "Replay bloqueado: el checkpoint está después de una aprobación y saltaría un nuevo interrupt."
            else:
                rejection = "Replay bloqueado: la ejecución directa de tests requiere volver a pasar por approval."
        return ReplayPlan(
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            next_nodes=next_nodes,
            nodes_that_may_reexecute=nodes,
            sensitive_operations=sensitive,
            allowed=rejection is None,
            rejection_reason=rejection,
        )

    @staticmethod
    def _details_config(details: CheckpointDetails) -> dict[str, dict[str, Any]]:
        namespace = details.metadata.get("_checkpoint_ns", "")
        config = checkpoint_config(
            details.thread_id,
            details.checkpoint_id,
            str(namespace) if namespace is not None else "",
        )
        checkpoint_map = details.metadata.get("_checkpoint_map")
        if isinstance(checkpoint_map, dict):
            config["configurable"]["checkpoint_map"] = dict(checkpoint_map)
        return config

    async def _execution_target(
        self,
        details: CheckpointDetails,
    ) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        config: dict[str, Any] = self._details_config(details)
        namespace = str(details.metadata.get("_checkpoint_ns") or "")
        if not namespace:
            return self._graph, config, thread_config(details.thread_id)
        root_name = namespace.split(":", 1)[0]
        async for name, _subgraph in self._graph.aget_subgraphs(recurse=True):
            if name != root_name:
                continue
            return self._graph, config, thread_config(details.thread_id)
        raise RuntimeError(f"No se encontró el subgrafo para el namespace {namespace}.")

    async def replay(self, thread_id: str, checkpoint_id: str) -> WorkflowRunResult:
        plan = await self.build_replay_plan(thread_id, checkpoint_id)
        if not plan.allowed:
            raise UnsafeReplayError(plan.rejection_reason or "Replay bloqueado por la política de seguridad.")
        if not plan.next_nodes:
            details = await self.get_checkpoint(thread_id, checkpoint_id)
            return WorkflowRunResult(
                thread_id=thread_id,
                final_state=details.values,  # type: ignore[arg-type]
                already_completed=True,
            )
        details = await self.get_checkpoint(thread_id, checkpoint_id)
        execution_graph, execution_config, latest_config = await self._execution_target(details)
        with self._stream_context(
            thread_id,
            {"lineage": "replay", "origin_checkpoint": checkpoint_id, "branch_id": checkpoint_id},
        ):
            emit_workflow_event(
                WorkflowEventType.WORKFLOW_RESUMED,
                source="graph.persistence",
                stage=None,
                status=EventStatus.RUNNING,
                data={"reason": "replay"},
            )
            invocation_result = await execution_graph.ainvoke(None, config=execution_config)
        result = await workflow_result_from_invocation(
            execution_graph,
            thread_id,
            invocation_result,
            snapshot_config=latest_config,
        )
        with self._stream_context(
            thread_id,
            {"lineage": "replay", "origin_checkpoint": checkpoint_id, "branch_id": checkpoint_id},
        ):
            self._emit_terminal(result)
        return result

    async def preview_fork(
        self,
        thread_id: str,
        checkpoint_id: str,
        updates: dict[str, Any],
    ) -> ForkPreview:
        details = await self.get_checkpoint(thread_id, checkpoint_id)
        domain = _fork_domain(details)
        allowed_fields = FORK_EDITABLE_FIELDS_BY_SUBGRAPH.get(domain or "", frozenset())
        validated, rejected = validate_fork_updates(updates, allowed_fields=allowed_fields)
        plan = await self.build_replay_plan(thread_id, checkpoint_id)
        if not plan.allowed:
            rejected["$replay"] = plan.rejection_reason or "checkpoint no seguro"
        as_node: str | None = None
        try:
            as_node = _resolve_fork_as_node(details)
        except AmbiguousForkNodeError as exc:
            rejected["$as_node"] = str(exc)
        current = {field: details.values.get(field) for field in updates if field in allowed_fields}
        reason = "fork_field_not_allowed" if any(not field.startswith("$") for field in rejected) else None
        return ForkPreview(
            thread_id=thread_id,
            origin_checkpoint_id=checkpoint_id,
            current_values=current,
            new_values=validated,
            allowed_fields=tuple(sorted(allowed_fields)),
            rejected_fields=tuple(sorted(rejected)),
            rejection_reasons=rejected,
            as_node=as_node,
            next_nodes=details.next_nodes,
            allowed=not rejected and bool(validated) and as_node is not None,
            reason=reason,
            domain=domain,
        )

    async def fork(
        self,
        thread_id: str,
        checkpoint_id: str,
        updates: dict[str, Any],
        *,
        continue_execution: bool = True,
    ) -> WorkflowForkResult:
        preview = await self.preview_fork(thread_id, checkpoint_id, updates)
        if preview.rejected_fields:
            if preview.disallowed_fields:
                raise InvalidForkUpdateError(
                    {
                        field: preview.rejection_reasons[field]
                        for field in preview.disallowed_fields
                    }
                )
            if "$as_node" in preview.rejection_reasons:
                raise AmbiguousForkNodeError(preview.rejection_reasons["$as_node"])
            if "$replay" in preview.rejection_reasons:
                raise UnsafeReplayError(preview.rejection_reasons["$replay"])
            raise InvalidForkUpdateError(preview.rejection_reasons)
        if not preview.new_values or preview.as_node is None:
            raise InvalidForkUpdateError({"$": "el fork requiere al menos un campo editable"})
        updated_fields = tuple(sorted(preview.new_values))
        origin_details = await self.get_checkpoint(thread_id, checkpoint_id)
        audit_values = {
            **preview.new_values,
            "fork_origin_checkpoint_id": checkpoint_id,
            "fork_reason": preview.new_values.get("repair_decision")
            or preview.new_values.get("failure_message")
            or "Fork de estado para explorar una reparación alternativa.",
            "fork_updated_fields": list(updated_fields),
            "fork_lineage": "fork",
            "git_promotion_state": "not_started",
            "git_promotion_id": None,
            "git_promotion_preview": None,
            "git_promotion_approval_id": None,
            "git_promotion_result_commit": None,
            "git_promotion_strategy": None,
            "git_promotion_conflicts": [],
        }
        if origin_details.values.get("pending_operation") == "git_merge":
            audit_values.update({
                "pending_operation": None,
                "pending_tool_name": None,
                "pending_tool_arguments": None,
                "pending_approval_preview": None,
                "pending_approval_status": "none",
                "approval_reason": None,
            })
        fork_config = await self._graph.aupdate_state(
            self._details_config(origin_details),
            values=audit_values,
            as_node=preview.as_node,
        )
        fork_checkpoint_id = _checkpoint_id(fork_config)
        if not fork_checkpoint_id:
            raise RuntimeError("LangGraph no devolvió checkpoint_id para el fork.")
        fork_lineage = {
            "lineage": "fork",
            "origin_checkpoint": checkpoint_id,
            "branch_id": fork_checkpoint_id,
        }
        with self._stream_context(thread_id, fork_lineage):
            emit_workflow_event(
                WorkflowEventType.WORKFLOW_FORKED,
                source="graph.persistence",
                stage=None,
                status=EventStatus.RUNNING,
                data={"updated_fields": list(updated_fields)},
            )
        fork_snapshot = await self._graph.aget_state(fork_config)
        next_nodes = tuple(fork_snapshot.next)
        run_result: WorkflowRunResult | None = None
        if continue_execution:
            fork_details = await self.get_checkpoint(thread_id, fork_checkpoint_id)
            namespace = str(fork_details.metadata.get("_checkpoint_ns") or "")
            if namespace:
                checkpoint_map = fork_details.metadata.get("_checkpoint_map")
                parent_checkpoint_id = checkpoint_map.get("") if isinstance(checkpoint_map, dict) else None
                if not parent_checkpoint_id:
                    raise RuntimeError("El fork interno no conserva su checkpoint padre.")
                bridge_config = await self._graph.aupdate_state(
                    checkpoint_config(thread_id, str(parent_checkpoint_id)),
                    values=dict(fork_details.values),
                    as_node="implementation",
                )
                bridge_checkpoint_id = _checkpoint_id(bridge_config)
                if not bridge_checkpoint_id:
                    raise RuntimeError("LangGraph no devolvió checkpoint_id para la rama del padre.")
                fork_config = bridge_config
                fork_checkpoint_id = bridge_checkpoint_id
                fork_lineage["branch_id"] = fork_checkpoint_id
                fork_snapshot = await self._graph.aget_state(fork_config)
                next_nodes = tuple(fork_snapshot.next)
            fork_plan = await self.build_replay_plan(thread_id, fork_checkpoint_id)
            if not fork_plan.allowed:
                raise UnsafeReplayError(fork_plan.rejection_reason or "La continuación del fork no es segura.")
            fork_details = await self.get_checkpoint(thread_id, fork_checkpoint_id)
            execution_graph, execution_config, latest_config = await self._execution_target(fork_details)
            with self._stream_context(thread_id, fork_lineage):
                invocation_result = await execution_graph.ainvoke(None, config=execution_config)
            run_result = await workflow_result_from_invocation(
                execution_graph,
                thread_id,
                invocation_result,
                snapshot_config=latest_config,
            )
            with self._stream_context(thread_id, fork_lineage):
                self._emit_terminal(run_result)
        return WorkflowForkResult(
            thread_id=thread_id,
            origin_checkpoint_id=checkpoint_id,
            fork_checkpoint_id=fork_checkpoint_id,
            updated_fields=updated_fields,
            next_nodes=next_nodes,
            run_result=run_result,
        )

    async def resume(self, thread_id: str) -> WorkflowRunResult:
        snapshot = await self.get_snapshot(thread_id)
        if not snapshot.next_nodes:
            return WorkflowRunResult(thread_id=thread_id, final_state=snapshot.values, already_completed=True)
        if snapshot.interrupts:
            return WorkflowRunResult(
                thread_id=thread_id,
                final_state=snapshot.values,
                interrupted=True,
                interrupts=snapshot.interrupts,
            )
        with self._stream_context(thread_id, {"lineage": "original"}):
            emit_workflow_event(
                WorkflowEventType.WORKFLOW_RESUMED,
                source="graph.persistence",
                stage=None,
                status=EventStatus.RUNNING,
            )
            invocation_result = await self._graph.ainvoke(None, config=thread_config(thread_id))
        result = await workflow_result_from_invocation(self._graph, thread_id, invocation_result)
        with self._stream_context(thread_id, {"lineage": "original"}):
            self._emit_terminal(result)
        return result

    async def continue_after_recovered_git_operation(
        self,
        thread_id: str,
        updates: dict[str, Any],
        *,
        as_node: str,
    ) -> WorkflowRunResult:
        if as_node not in {"execute_git_commit", "execute_git_promotion"}:
            raise ValueError("Unsupported Git recovery node.")
        snapshot = await active_snapshot(self._graph, thread_config(thread_id))
        if not _snapshot_exists(snapshot):
            raise WorkflowNotFoundError(thread_id)
        if _snapshot_interrupts(snapshot):
            raise WorkflowNotInterruptedError(
                "The workflow still has an approval interrupt and must use the approval flow."
            )
        updated_config = await self._graph.aupdate_state(
            thread_config(thread_id), values=updates, as_node=as_node,
        )
        with self._stream_context(thread_id, {"lineage": "original"}):
            invocation_result = await self._graph.ainvoke(None, config=updated_config)
        result = await workflow_result_from_invocation(
            self._graph,
            thread_id,
            invocation_result,
            snapshot_config=thread_config(thread_id),
        )
        with self._stream_context(thread_id, {"lineage": "original"}):
            self._emit_terminal(result)
        return result

    async def reconcile_successful_ci_run(
        self,
        thread_id: str,
        *,
        run: dict[str, Any],
        eligibility: dict[str, Any],
        head_commit: str | None,
    ) -> WorkflowRunResult | None:
        snapshot = await active_snapshot(self._graph, thread_config(thread_id))
        if not _snapshot_exists(snapshot):
            raise WorkflowNotFoundError(thread_id)
        values = active_snapshot_values(snapshot)
        reconciliation = build_ci_success_reconciliation(
            values,
            run,
            eligibility,
            head_commit=head_commit,
        )
        if not reconciliation.applicable:
            return None

        interrupts = _snapshot_interrupts(snapshot)
        if interrupts and not reconciliation.clears_ci_repair_interrupt:
            # A different human approval remains authoritative. Updating the root
            # checkpoint would consume its durable interrupt.
            return None

        accepted_current_head = bool(
            not reconciliation.clears_ci_repair_interrupt
            and not interrupts
            and head_commit
            and values.get("ci_validated_commit") == head_commit
            and values.get("ci_status") == "passed"
            and values.get("ci_decision") in {"accepted", "accepted_with_warnings"}
            and values.get("ci_promotion_eligible") is True
        )
        state_already_reconciled = accepted_current_head or (
            not interrupts
            and all(
                values.get(key) == value
                for key, value in reconciliation.updates.items()
            )
        )
        waiting_at_supervisor = tuple(snapshot.next) == ("supervisor",)
        if state_already_reconciled and not waiting_at_supervisor:
            return None

        if state_already_reconciled:
            continuation_config = thread_config(thread_id)
        else:
            continuation_config = await self._graph.aupdate_state(
                thread_config(thread_id),
                values=reconciliation.updates,
                as_node=(
                    "testing_repair"
                    if reconciliation.clears_ci_repair_interrupt
                    else "ci_pipeline"
                ),
            )

        with self._stream_context(thread_id, {"lineage": "original"}):
            emit_workflow_event(
                WorkflowEventType.WORKFLOW_RESUMED,
                source="ci_reconciliation",
                stage="ci_pipeline",
                status=EventStatus.RUNNING,
                data={
                    "reason": "manual_ci_rerun_accepted",
                    "ci_run_id": run.get("ci_run_id"),
                    "ci_validated_commit": run.get("ci_validated_commit"),
                },
            )
            invocation_result = await self._graph.ainvoke(
                None,
                config=continuation_config,
            )
        result = await workflow_result_from_invocation(
            self._graph,
            thread_id,
            invocation_result,
            snapshot_config=thread_config(thread_id),
        )
        with self._stream_context(thread_id, {"lineage": "original"}):
            self._emit_terminal(result)
        return result

    async def get_pending_interrupts(self, thread_id: str) -> tuple[WorkflowInterrupt, ...]:
        return (await self.get_snapshot(thread_id)).interrupts

    async def _load_current_pending_state(
        self,
        thread_id: str,
    ) -> tuple[PendingApprovalReference, SoftwareFactoryState]:
        snapshot = await active_snapshot(self._graph, thread_config(thread_id))
        if not _snapshot_exists(snapshot):
            raise WorkflowNotFoundError(thread_id)
        values = active_snapshot_values(snapshot)
        interrupts = _snapshot_interrupts(snapshot)
        if _is_effectively_terminal(values, interrupts):
            raise WorkflowAlreadyCompletedError(f"El workflow {thread_id} ya terminó.")
        if not interrupts:
            raise WorkflowNotInterruptedError(f"El workflow {thread_id} no espera una aprobación.")
        interrupt_value = interrupts[0].value
        operation = values.get("pending_operation") or interrupt_value.get("operation")
        tool_name = (
            values.get("pending_tool_name")
            or interrupt_value.get("public_tool_name")
            or {
                "create_project": "filesystem__create_project_structure",
                "prepare_environment": "testing__prepare_test_environment",
                "run_tests": "testing__run_tests",
                "apply_fix": "filesystem__update_project_files",
            }.get(str(operation))
        )
        checkpoint_id = _active_interrupt_checkpoint_id(snapshot)
        if not checkpoint_id or not operation or not tool_name:
            raise WorkflowNotInterruptedError(
                f"El workflow {thread_id} no conserva una referencia de aprobación completa."
            )
        return (
            PendingApprovalReference(
                thread_id=thread_id,
                checkpoint_id=str(checkpoint_id),
                operation=str(operation),
                tool_name=str(tool_name),
            ),
            values,
        )

    async def load_current_pending_approval(self, thread_id: str) -> PendingApprovalReference:
        reference, _values = await self._load_current_pending_state(thread_id)
        return reference

    async def _resolve_approval(
        self,
        thread_id: str,
        approved: bool,
        reason: str | None,
        *,
        expected_reference: PendingApprovalReference | None = None,
    ) -> WorkflowRunResult:
        current, _current_values = await self._load_current_pending_state(thread_id)
        if expected_reference is not None and current != expected_reference:
            raise StaleApprovalReference(expected_reference, current)
        refreshed, refreshed_values = await self._load_current_pending_state(thread_id)
        if refreshed != current:
            raise StaleApprovalReference(current, refreshed)
        branch_id = str(refreshed_values.get("fork_origin_checkpoint_id") or "original")
        lineage = "fork" if refreshed_values.get("fork_lineage") == "fork" else "original"
        idempotency_key = (
            thread_id,
            branch_id,
            current.checkpoint_id,
            current.operation,
            current.tool_name,
        )
        if idempotency_key in self._resolved_approval_keys:
            raise StaleApprovalReference(expected_reference or current, current)
        with self._stream_context(
            thread_id,
            {
                "lineage": lineage,
                "checkpoint_id": current.checkpoint_id,
                "branch_id": branch_id,
            },
        ):
            emit_workflow_event(
                WorkflowEventType.WORKFLOW_RESUMED,
                source="graph.persistence",
                stage=None,
                status=EventStatus.RUNNING,
                data={
                    "reason": "approval",
                    "operation": current.operation,
                    "tool_name": current.tool_name,
                },
            )
            invocation_result = await self._graph.ainvoke(
                Command(resume={"approved": approved, "reason": reason}),
                config=thread_config(thread_id),
            )
        self._resolved_approval_keys.add(idempotency_key)
        result = await workflow_result_from_invocation(self._graph, thread_id, invocation_result)
        with self._stream_context(thread_id, {"lineage": "original"}):
            self._emit_terminal(result)
        return result

    async def approve(
        self,
        thread_id: str,
        reason: str | None = None,
        *,
        expected_reference: PendingApprovalReference | None = None,
    ) -> WorkflowRunResult:
        return await self._resolve_approval(
            thread_id,
            True,
            reason,
            expected_reference=expected_reference,
        )

    async def reject(
        self,
        thread_id: str,
        reason: str | None = None,
        *,
        expected_reference: PendingApprovalReference | None = None,
    ) -> WorkflowRunResult:
        return await self._resolve_approval(
            thread_id,
            False,
            reason,
            expected_reference=expected_reference,
        )
