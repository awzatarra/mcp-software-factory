from __future__ import annotations

import ast
from collections.abc import Mapping
import json
import keyword
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Awaitable, Callable, Literal

from langgraph.types import interrupt
from openai import AsyncOpenAI

from graph.state import SoftwareFactoryState, TerminalStatus
from tool_executor import ToolExecutionOutcome, ToolExecutor
from streaming import EventStatus, WorkflowEventType, emit_workflow_event


ArgumentResolver = Callable[[str, SoftwareFactoryState], Awaitable[dict[str, Any]]]
NodeObserver = Callable[[str, str], None]

_NODE_TOOL_STAGES = {
    "analyze_requirement": "planning",
    "prepare_planner_knowledge": "planning",
    "create_tasks": "planning",
    "inspect_workspace": "inspect_workspace",
    "prepare_developer_knowledge": "implementation",
    "create_project": "implementation",
    "execute_create_project": "implementation",
    "detect_test_framework": "implementation",
    "prepare_environment": "implementation",
    "execute_prepare_environment": "implementation",
    "prepare_qa_knowledge": "testing_repair",
    "prepare_repair_knowledge": "testing_repair",
    "run_tests": "testing_repair",
    "execute_tests": "testing_repair",
    "read_failing_test": "testing_repair",
    "read_related_source": "testing_repair",
    "apply_fix": "testing_repair",
    "execute_fix": "testing_repair",
}
_OPERATION_TOOL_STAGES = {
    "create_project": "implementation",
    "prepare_environment": "implementation",
    "run_tests": "testing_repair",
    "apply_fix": "testing_repair",
}
_TOOL_STAGES = {
    "software_factory__analyze_requirement": "planning",
    "software_factory__create_tasks": "planning",
    "filesystem__list_files": "inspect_workspace",
    "knowledge__get_relevant_context": "implementation",
    "filesystem__create_project_structure": "implementation",
    "testing__detect_test_framework": "implementation",
    "testing__prepare_test_environment": "implementation",
    "testing__run_tests": "testing_repair",
    "filesystem__read_file": "testing_repair",
    "filesystem__update_project_files": "testing_repair",
}


def resolve_tool_stage(
    *,
    node_name: str | None,
    operation: str | None,
    tool_name: str,
    state: Mapping[str, Any],
) -> str | None:
    if node_name and node_name in _NODE_TOOL_STAGES:
        return _NODE_TOOL_STAGES[node_name]
    namespace = state.get("active_subgraph") or state.get("checkpoint_namespace")
    if isinstance(namespace, str):
        root_namespace = namespace.split(":", 1)[0]
        if root_namespace in {"planning", "implementation", "testing_repair"}:
            return root_namespace
    effective_operation = operation or state.get("pending_operation")
    if isinstance(effective_operation, str) and effective_operation in _OPERATION_TOOL_STAGES:
        return _OPERATION_TOOL_STAGES[effective_operation]
    return _TOOL_STAGES.get(tool_name)


@dataclass(frozen=True)
class GraphDependencies:
    tool_executor: ToolExecutor
    openai_client: AsyncOpenAI
    model: str
    instructions: str = ""
    argument_resolver: ArgumentResolver | None = None
    node_observer: NodeObserver | None = None
    planning_service: Any | None = None
    implementation_service: Any | None = None
    supervisor_service: Any | None = None
    ci_service: Any | None = None
    planner_judge_service: Any | None = None
    observability: Any | None = None
    workflow_learning_store: Any | None = None


def _project_name(state: SoftwareFactoryState) -> str:
    return str(state.get("created_project_name") or state.get("project_name") or "")


def _state_for_model(state: SoftwareFactoryState) -> dict[str, Any]:
    excluded = {"test_stdout", "test_stderr"}
    return {key: value for key, value in state.items() if key not in excluded}


async def _model_arguments(
    public_tool_name: str,
    state: SoftwareFactoryState,
    dependencies: GraphDependencies,
) -> dict[str, Any]:
    if dependencies.argument_resolver is not None:
        return await dependencies.argument_resolver(public_tool_name, state)
    tool = dependencies.tool_executor.openai_tool(public_tool_name)
    model_state = _state_for_model(state)
    developer_context = ""
    if public_tool_name == "filesystem__create_project_structure":
        developer_context = (
            "\nDeveloper context sections are present in state: original_user_message (source of truth), "
            "requirement_analysis, acceptance_criteria, and implementation_tasks. Consume this plan; do not re-plan."
        )
    response = await dependencies.openai_client.responses.create(
        model=dependencies.model,
        instructions=(
            dependencies.instructions
            + "\nReturn exactly one call to the provided tool. Use the original requirement as source of truth. "
            "Do not invent a different project name. During repair, update only the failing test when the implementation already satisfies the requirement."
            + developer_context
        ),
        input=[
            {
                "role": "user",
                "content": "Produce arguments for this workflow step from the state:\n"
                + json.dumps(model_state, ensure_ascii=False, indent=2),
            }
        ],
        tools=[tool],
        tool_choice={"type": "function", "name": public_tool_name},
    )
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        raw = getattr(item, "arguments", "{}") or "{}"
        arguments = json.loads(raw)
        if isinstance(arguments, dict):
            return arguments
    raise RuntimeError(f"OpenAI did not return arguments for {public_tool_name}")


async def _execute(
    public_tool_name: str,
    arguments: dict[str, Any],
    state: SoftwareFactoryState,
    dependencies: GraphDependencies,
    approval_mode: Literal["prompt", "already_approved"] = "prompt",
    *,
    node_name: str | None = None,
    operation: str | None = None,
) -> ToolExecutionOutcome:
    server, _, tool = public_tool_name.partition("__")
    started = perf_counter()
    stage = resolve_tool_stage(
        node_name=node_name,
        operation=operation,
        tool_name=public_tool_name,
        state=state,
    )
    emit_workflow_event(
        WorkflowEventType.TOOL_STARTED,
        source="graph.nodes",
        stage=stage,
        status=EventStatus.RUNNING,
        data={
            "server": server,
            "tool": tool or public_tool_name,
            "attempt": state.get("repair_attempts", 0),
        },
    )
    try:
        outcome = await dependencies.tool_executor.execute(
            public_tool_name,
            arguments,
            state_context=state,
            approval_mode=approval_mode,
        )
    except Exception as exc:
        emit_workflow_event(
            WorkflowEventType.TOOL_FAILED,
            source="graph.nodes",
            stage=stage,
            status=EventStatus.FAILED,
            data={
                "server": server,
                "tool": tool or public_tool_name,
                "duration_seconds": perf_counter() - started,
                "business_success": False,
                "failure_type": type(exc).__name__,
                "attempt": state.get("repair_attempts", 0),
            },
        )
        raise
    payload = outcome.payload or {}
    succeeded = not outcome.is_error and payload.get("success", True) is not False
    failure_type = (
        None
        if succeeded
        else payload.get("failure_type") or payload.get("error_code") or payload.get("status")
    )
    emit_workflow_event(
        WorkflowEventType.TOOL_COMPLETED if succeeded else WorkflowEventType.TOOL_FAILED,
        source="graph.nodes",
        stage=stage,
        status=EventStatus.COMPLETED if succeeded else EventStatus.FAILED,
        data={
            "server": server,
            "tool": tool or public_tool_name,
            "duration_seconds": perf_counter() - started,
            "business_success": succeeded,
            "failure_type": failure_type,
            "attempt": state.get("repair_attempts", 0),
        },
    )
    return outcome


async def detect_intent_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    from host import extract_requested_project_name, extract_workflow_intent

    message = state.get("original_user_message", "")
    project_name = extract_requested_project_name(message)
    return {
        "project_name": project_name,
        "workflow_intent": extract_workflow_intent(message, project_name),
    }


async def analyze_requirement_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    outcome = await _execute(
        "software_factory__analyze_requirement",
        {"requirement": state.get("original_user_message", "")},
        state,
        dependencies,
        node_name="analyze_requirement",
    )
    return outcome.state_updates


async def create_tasks_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    message = state.get("original_user_message", "")
    backend = "dotnet" if "dotnet" in message.lower() else "node" if "node" in message.lower() else "fastapi"
    outcome = await _execute(
        "software_factory__create_tasks",
        {"project_type": message, "backend": backend},
        state,
        dependencies,
        node_name="create_tasks",
    )
    return outcome.state_updates


async def inspect_workspace_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    outcome = await _execute(
        "filesystem__list_files",
        {"relative_path": "."},
        state,
        dependencies,
        node_name="inspect_workspace",
    )
    return outcome.state_updates


async def create_project_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    arguments = await _model_arguments("filesystem__create_project_structure", state, dependencies)
    outcome = await _execute(
        "filesystem__create_project_structure",
        arguments,
        state,
        dependencies,
        node_name="create_project",
    )
    return outcome.state_updates


def _file_preview(arguments: dict[str, Any]) -> dict[str, Any]:
    files = arguments.get("files")
    if not isinstance(files, list):
        return {}
    previews: list[dict[str, Any]] = []
    for file in files:
        if not isinstance(file, dict):
            continue
        content = str(file.get("content", ""))
        previews.append({"path": str(file.get("path", "")), "size_bytes": len(content.encode("utf-8"))})
    return {"files": previews, "total_files": len(previews)}


def _prepared_status_change(arguments: dict[str, Any]) -> str | None:
    files = arguments.get("files")
    if not isinstance(files, list):
        return None
    pattern = re.compile(r'["\']status["\']\s*:\s*["\'][^"\']+["\']')
    for file in files:
        if not isinstance(file, dict) or not isinstance(file.get("content"), str):
            continue
        match = pattern.search(file["content"])
        if match:
            return match.group(0)
    return None


def _pending_approval(
    operation: Literal["create_project", "prepare_environment", "run_tests", "apply_fix"],
    tool_name: str,
    arguments: dict[str, Any],
    preview: dict[str, Any],
) -> dict[str, Any]:
    return {
        "pending_operation": operation,
        "pending_tool_name": tool_name,
        "pending_tool_arguments": arguments,
        "pending_approval_preview": preview,
        "pending_approval_status": "waiting",
        "approval_reason": None,
    }


def _cleared_approval() -> dict[str, Any]:
    return {
        "pending_operation": None,
        "pending_tool_name": None,
        "pending_tool_arguments": None,
        "pending_approval_preview": None,
        "pending_approval_status": "none",
    }


def _approved_pending_tool(state: SoftwareFactoryState) -> tuple[str, dict[str, Any]]:
    tool_name = state.get("pending_tool_name")
    arguments = state.get("pending_tool_arguments")
    if state.get("pending_approval_status") != "approved" or not tool_name or not isinstance(arguments, dict):
        raise RuntimeError("No existe una operación aprobada lista para ejecutar.")
    return tool_name, arguments


async def prepare_create_project_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    arguments = await _model_arguments("filesystem__create_project_structure", state, dependencies)
    preview = {"project_name": arguments.get("project_name"), **_file_preview(arguments)}
    return _pending_approval("create_project", "filesystem__create_project_structure", arguments, preview)


async def prepare_environment_request_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    arguments = {"project_name": _project_name(state)}
    return _pending_approval("prepare_environment", "testing__prepare_test_environment", arguments, dict(arguments))


async def prepare_test_request_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    arguments = {"project_name": _project_name(state)}
    preview = {
        "project_name": _project_name(state),
        "expected_command": state.get("expected_test_command"),
    }
    return _pending_approval("run_tests", "testing__run_tests", arguments, preview)


async def prepare_fix_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    required = {
        "failing test": state.get("failing_test_content"),
        "related source": state.get("related_source_content"),
        "failure message": state.get("failure_message") or state.get("test_failure_summary"),
        "original requirement": state.get("original_user_message"),
    }
    missing = [name for name, value in required.items() if not value]
    if state.get("repair_phase") != "apply_fix":
        missing.append("repair_phase=apply_fix")
    if missing:
        return {
            "failure_type": "repair_context_incomplete",
            "failure_stage": "prepare_fix",
            "failure_message": f"Contexto de reparación incompleto: {', '.join(missing)}.",
        }
    arguments = await _model_arguments("filesystem__update_project_files", state, dependencies)
    preview = {
        "project_name": arguments.get("project_name"),
        **_file_preview(arguments),
        "before": state.get("repair_before")
        or ('"status": "healthy"' if "healthy" in (state.get("test_failure_summary") or "") else None),
        "after": state.get("repair_after") or _prepared_status_change(arguments),
    }
    return _pending_approval("apply_fix", "filesystem__update_project_files", arguments, preview)


async def approval_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    request = {
        "type": "tool_approval",
        "operation": state.get("pending_operation"),
        "public_tool_name": state.get("pending_tool_name"),
        "arguments": state.get("pending_tool_arguments"),
        "preview": state.get("pending_approval_preview"),
        "project_name": _project_name(state),
        "message": "Aprueba o rechaza la operación sensible para continuar el workflow.",
    }
    emit_workflow_event(
        WorkflowEventType.APPROVAL_REQUIRED,
        source="graph.approval",
        stage=str(state.get("pending_operation") or "approval"),
        status=EventStatus.WAITING,
        data={
            "operation": state.get("pending_operation"),
            "tool_name": state.get("pending_tool_name"),
            "project_name": _project_name(state),
            "preview": state.get("pending_approval_preview"),
            "attempt": state.get("repair_attempts", 0),
        },
    )
    # This node restarts from the beginning after resume. Nothing before interrupt may have side effects.
    decision = interrupt(request)
    if not isinstance(decision, dict) or not isinstance(decision.get("approved"), bool) or not (
        decision.get("reason") is None or isinstance(decision.get("reason"), str)
    ):
        decision = interrupt({**request, "validation_error": "Responde con {'approved': bool, 'reason': str | null}."})
    if not isinstance(decision, dict) or not isinstance(decision.get("approved"), bool):
        return {
            "pending_approval_status": "rejected",
            "user_cancelled": True,
            "terminal_status": "user_cancelled",
            "failure_type": "invalid_approval_response",
            "failure_message": "La respuesta de aprobación no fue válida.",
        }
    reason = decision.get("reason") if isinstance(decision.get("reason"), str) else None
    if decision["approved"]:
        emit_workflow_event(
            WorkflowEventType.APPROVAL_GRANTED,
            source="graph.approval",
            stage=str(state.get("pending_operation") or "approval"),
            status=EventStatus.COMPLETED,
            data={
                "operation": state.get("pending_operation"),
                "tool_name": state.get("pending_tool_name"),
                "project_name": _project_name(state),
                "attempt": state.get("repair_attempts", 0),
            },
        )
        return {
            "pending_approval_status": "approved",
            "approval_reason": reason,
            "last_approved_tool": state.get("pending_tool_name"),
        }
    emit_workflow_event(
        WorkflowEventType.APPROVAL_REJECTED,
        source="graph.approval",
        stage=str(state.get("pending_operation") or "approval"),
        status=EventStatus.FAILED,
        data={
            "operation": state.get("pending_operation"),
            "tool_name": state.get("pending_tool_name"),
            "project_name": _project_name(state),
            "reason": reason,
            "attempt": state.get("repair_attempts", 0),
        },
    )
    return {
        "pending_operation": None,
        "pending_tool_name": None,
        "pending_tool_arguments": None,
        "pending_approval_preview": None,
        "pending_approval_status": "rejected",
        "approval_reason": reason,
        "last_rejected_tool": state.get("pending_tool_name"),
        "user_cancelled": True,
        "terminal_status": "user_cancelled",
        "failure_type": "user_rejected",
        "failure_message": reason or "El usuario rechazó la operación sensible.",
    }


async def _execute_approved(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    tool_name, arguments = _approved_pending_tool(state)
    outcome = await _execute(
        tool_name,
        arguments,
        state,
        dependencies,
        approval_mode="already_approved",
        operation=state.get("pending_operation"),
    )
    return {**outcome.state_updates, **_cleared_approval()}


async def execute_create_project_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    tool_name, arguments = _approved_pending_tool(state)
    outcome = await _execute(
        tool_name,
        arguments,
        state,
        dependencies,
        approval_mode="already_approved",
        node_name="execute_create_project",
        operation="create_project",
    )
    payload = outcome.payload or {}
    if outcome.is_error or payload.get("success") is not True:
        message = payload.get("message") or payload.get("error") or payload.get("status") or "create_project_failed"
        return {
            **_cleared_approval(),
            "terminal_status": "implementation_failed",
            "failure_type": str(payload.get("status") or "create_project_failed"),
            "failure_stage": "execute_create_project",
            "failure_message": str(message),
            "implementation_failure_reason": str(message),
        }
    files = arguments.get("files", [])
    generated_files = [str(file.get("path")) for file in files if isinstance(file, dict) and file.get("path")]
    return {
        **outcome.state_updates,
        **_cleared_approval(),
        "project_created": True,
        "created_project_name": str(payload.get("project_name") or arguments.get("project_name") or ""),
        "generated_files": generated_files,
    }


async def execute_prepare_environment_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    tool_name, arguments = _approved_pending_tool(state)
    outcome = await _execute(
        tool_name,
        arguments,
        state,
        dependencies,
        approval_mode="already_approved",
        node_name="execute_prepare_environment",
        operation="prepare_environment",
    )
    updates = {**outcome.state_updates, **_cleared_approval()}
    payload = outcome.payload or {}
    if outcome.is_error or payload.get("success") is False:
        failure_type = str(payload.get("failure_type") or payload.get("status") or "prepare_environment_failed")
        message = str(
            payload.get("message")
            or payload.get("error")
            or payload.get("status")
            or "La preparación del entorno falló."
        )
        updates.update(
            {
                "test_infrastructure_failed": True,
                "terminal_status": "infrastructure_failed",
                "failure_type": failure_type,
                "failure_stage": str(payload.get("failure_stage") or "prepare_test_environment"),
                "failure_message": message,
                "implementation_failure_reason": message,
            }
        )
    return updates


async def execute_tests_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    started = perf_counter()
    emit_workflow_event(
        WorkflowEventType.TEST_RUN_STARTED,
        source="testing_repair",
        stage="testing_repair",
        status=EventStatus.RUNNING,
        data={
            "framework": state.get("detected_test_framework"),
            "attempt": state.get("repair_attempts", 0),
        },
    )
    updates = await _execute_approved(state, dependencies)
    passed = updates.get("tests_passed") is True
    emit_workflow_event(
        WorkflowEventType.TEST_RUN_COMPLETED if passed else WorkflowEventType.TEST_RUN_FAILED,
        source="testing_repair",
        stage="testing_repair",
        status=EventStatus.COMPLETED if passed else EventStatus.FAILED,
        data={
            "framework": updates.get("detected_test_framework") or state.get("detected_test_framework"),
            "passed": passed,
            "summary": updates.get("final_test_result_summary") or updates.get("test_failure_summary"),
            "warning_count": updates.get("test_warning_count"),
            "duration_seconds": perf_counter() - started,
            "exit_code": updates.get("test_exit_code"),
            "stderr": updates.get("test_stderr") if not passed else None,
            "attempt": state.get("repair_attempts", 0),
        },
    )
    if updates.get("tests_passed") is False and updates.get("repair_phase") == "read_failing_test":
        updates.update(
            related_source_content=None,
            related_source_file=None,
            related_source_candidates=[],
            related_source_read_attempts=0,
        )
    return updates


async def execute_fix_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    emit_workflow_event(
        WorkflowEventType.REPAIR_STARTED,
        source="testing_repair",
        stage="repair",
        status=EventStatus.RUNNING,
        data={"operation": "apply_fix", "repair_attempts": state.get("repair_attempts")},
    )
    tool_name, arguments = _approved_pending_tool(state)
    outcome = await _execute(
        tool_name,
        arguments,
        state,
        dependencies,
        approval_mode="already_approved",
        node_name="execute_fix",
        operation="apply_fix",
    )
    payload = outcome.payload or {}
    if outcome.is_error or payload.get("success") is not True:
        status = str(payload.get("status") or payload.get("failure_type") or "update_project_files_failed")
        message = payload.get("message") or payload.get("error") or status
        failure_type = "repair_state_guard_rejected" if status == "repair_step_not_allowed" else status
        emit_workflow_event(
            WorkflowEventType.REPAIR_FAILED,
            source="testing_repair",
            stage="repair",
            status=EventStatus.FAILED,
            data={"operation": "apply_fix", "failure_type": failure_type},
        )
        return {
            **_cleared_approval(),
            "failure_type": failure_type,
            "failure_stage": "execute_fix",
            "failure_message": str(message),
        }
    emit_workflow_event(
        WorkflowEventType.REPAIR_COMPLETED,
        source="testing_repair",
        stage="repair",
        status=EventStatus.COMPLETED,
        data={
            "operation": "apply_fix",
            "repair_before": state.get("repair_before"),
            "repair_after": state.get("repair_after"),
        },
    )
    return {**outcome.state_updates, **_cleared_approval()}


async def detect_test_framework_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    outcome = await _execute(
        "testing__detect_test_framework",
        {"project_name": _project_name(state)},
        state,
        dependencies,
        node_name="detect_test_framework",
    )
    return outcome.state_updates


async def prepare_environment_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    outcome = await _execute(
        "testing__prepare_test_environment",
        {"project_name": _project_name(state)},
        state,
        dependencies,
        node_name="prepare_environment",
    )
    return outcome.state_updates


async def run_tests_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    outcome = await _execute(
        "testing__run_tests",
        {"project_name": _project_name(state)},
        state,
        dependencies,
        node_name="run_tests",
    )
    return outcome.state_updates


async def read_failing_test_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    from host import normalize_project_relative_path

    files = state.get("failing_test_files", [])
    if not files:
        return {
            "retry_limit_reached": True,
            "failure_type": "missing_failing_test_file",
            "failure_stage": "read_failing_test",
            "failure_message": "No se pudo identificar el test fallido.",
        }
    relative_path = normalize_project_relative_path(_project_name(state), files[0])
    outcome = await _execute(
        "filesystem__read_file",
        {"relative_path": relative_path},
        state,
        dependencies,
        node_name="read_failing_test",
    )
    payload = outcome.payload or {}
    content = payload.get("content")
    if outcome.is_error or payload.get("success") is False or not isinstance(content, str):
        message = payload.get("message") or payload.get("error") or "No se pudo leer el test fallido."
        return {
            "failure_type": "failing_test_read_failed",
            "failure_stage": "read_failing_test",
            "failure_message": str(message),
            "repair_phase": "read_failing_test",
        }
    return {
        **outcome.state_updates,
        "failing_test_content": content,
        "repair_phase": "read_related_source",
    }


def project_name_to_python_package(project_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", project_name.strip().replace("-", "_"))
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        raise ValueError("El nombre del proyecto no puede estar vacío ni carecer de caracteres válidos.")
    if normalized[0].isdigit():
        normalized = f"_{normalized}"
    if keyword.iskeyword(normalized):
        normalized = f"{normalized}_"
    if not normalized.isidentifier():
        raise ValueError("El nombre del proyecto no puede convertirse en un package Python válido.")
    return normalized


def extract_related_source_path_from_test(test_content: str) -> str | None:
    try:
        tree = ast.parse(test_content)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        parts = node.module.split(".")
        if len(parts) >= 2 and parts[-1] == "main" and all(part.isidentifier() for part in parts):
            return "/".join(parts) + ".py"
    return None


def _related_source_candidates(state: SoftwareFactoryState) -> list[str]:
    stored = state.get("related_source_candidates")
    if stored:
        return list(stored)
    project_name = _project_name(state)
    fallback = f"{project_name_to_python_package(project_name)}/main.py"
    imported = extract_related_source_path_from_test(state.get("failing_test_content") or "")
    return list(dict.fromkeys(path for path in (imported, fallback) if path))


async def read_related_source_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    from host import normalize_project_relative_path

    try:
        candidates = _related_source_candidates(state)
    except ValueError as exc:
        return {
            "failure_type": "related_source_resolution_failed",
            "failure_stage": "read_related_source",
            "failure_message": str(exc),
            "repair_phase": "read_related_source",
        }
    attempt = state.get("related_source_read_attempts", 0)
    if attempt >= len(candidates):
        return {
            "failure_type": "related_source_read_failed",
            "failure_stage": "read_related_source",
            "failure_message": "No queda una ruta segura para leer la fuente relacionada.",
            "repair_phase": "read_related_source",
            "related_source_candidates": candidates,
        }
    source = candidates[attempt]
    relative_path = normalize_project_relative_path(_project_name(state), source)
    resolution = "from_test_import" if attempt == 0 and extract_related_source_path_from_test(state.get("failing_test_content") or "") == source else "fallback_from_project_name"
    print(f"Resolved related source: {relative_path}")
    print(f"Related source resolution: {resolution}")
    outcome = await _execute(
        "filesystem__read_file",
        {"relative_path": relative_path},
        state,
        dependencies,
        node_name="read_related_source",
    )
    payload = outcome.payload or {}
    content = payload.get("content")
    common = {
        "related_source_file": relative_path,
        "related_source_candidates": candidates,
        "related_source_read_attempts": attempt + 1,
    }
    if outcome.is_error or payload.get("success") is False or not isinstance(content, str):
        message = payload.get("message") or payload.get("error") or "No se pudo leer la fuente relacionada."
        print("Related source read failed")
        print(f"path: {relative_path}")
        return {
            **common,
            "failure_type": "related_source_read_failed",
            "failure_stage": "read_related_source",
            "failure_message": str(message),
            "repair_phase": "read_related_source",
        }
    return {
        **outcome.state_updates,
        **common,
        "related_source_content": content,
        "repair_phase": "apply_fix",
        "failure_type": None,
        "failure_stage": None,
        "failure_message": state.get("test_failure_summary"),
    }


async def apply_fix_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    arguments = await _model_arguments("filesystem__update_project_files", state, dependencies)
    outcome = await _execute(
        "filesystem__update_project_files",
        arguments,
        state,
        dependencies,
        node_name="apply_fix",
    )
    return outcome.state_updates


def terminal_status_from_state(state: SoftwareFactoryState) -> TerminalStatus:
    if state.get("terminal_status") == "supervisor_loop_detected":
        return "supervisor_loop_detected"
    if state.get("terminal_status") == "infrastructure_failed":
        return "infrastructure_failed"
    if state.get("terminal_status") == "ci_failed":
        return "ci_failed"
    implementation = state.get("implementation_result") or {}
    implementation_valid = bool(
        implementation.get("valid", state.get("implementation_valid", False))
    )
    implementation_errors = (
        implementation.get("validation_errors")
        or implementation.get("errors")
        or state.get("implementation_errors")
        or state.get("remaining_validation_errors")
        or []
    )
    actual_implementation_failure = (
        state.get("failure_type") == "implementation_validation_failed"
        or (
            state.get("terminal_status") == "implementation_failed"
            and bool(
                state.get("failure_type")
                or state.get("failure_message")
                or implementation_errors
            )
        )
    )
    if actual_implementation_failure:
        return "implementation_failed"
    if state.get("terminal_status") == "planning_failed" or state.get("failure_type") == "planning_validation_failed":
        return "planning_failed"
    testing = state.get("testing_result") or {}
    tests_passed = testing.get("tests_passed") if "tests_passed" in testing else state.get("tests_passed")
    if tests_passed:
        return "completed"
    if state.get("test_infrastructure_failed"):
        return "infrastructure_failed"
    if state.get("retry_limit_reached") or state.get("repair_attempts", 0) >= 2:
        return "repair_limit_reached"
    if state.get("user_cancelled"):
        return "user_cancelled"
    return "tests_failed"


def _test_result(state: SoftwareFactoryState) -> str:
    testing = state.get("testing_result") or {}
    result = testing.get("summary") or state.get("final_test_result_summary") or state.get("first_test_result_summary")
    if not result:
        tests_passed = testing.get("tests_passed") if "tests_passed" in testing else state.get("tests_passed")
        result = "pruebas aprobadas" if tests_passed else "pruebas no aprobadas"
    warning_count = state.get("test_warning_count")
    if warning_count and "warning" not in result.lower():
        suffix = "warning" if warning_count == 1 else "warnings"
        result = f"{result}, {warning_count} {suffix}"
    return result


def build_final_summary(state: SoftwareFactoryState) -> str:
    implementation = state.get("implementation_result") or {}
    testing = state.get("testing_result") or {}
    project_name = _project_name(state) or "desconocido"
    project_created = (
        implementation.get("project_created")
        if "project_created" in implementation
        else state.get("project_created")
    )
    project_exists = state.get("project_exists")
    environment_prepared = (
        implementation.get("environment_prepared")
        if "environment_prepared" in implementation
        else state.get("environment_prepared")
    )
    framework = implementation.get("framework") or state.get("detected_test_framework")
    tests_executed = (
        testing.get("tests_executed")
        if "tests_executed" in testing
        else state.get("tests_executed")
    )
    repair_phase = (
        testing.get("repair_phase")
        if "repair_phase" in testing
        else state.get("repair_phase")
    )
    if project_created:
        project_status = "creado correctamente"
    elif project_exists:
        project_status = "existente revisado"
    elif state.get("user_cancelled"):
        project_status = "operación cancelada"
    else:
        project_status = "no creado"

    lines = [
        f"Proyecto: {project_name}",
        f"Estado: {project_status}",
        f"Entorno: {'preparado' if environment_prepared else 'no preparado'}",
        f"Dependencias: {'instaladas' if state.get('dependencies_installed') else 'no instaladas'}",
    ]
    if framework:
        lines.append(f"Framework de pruebas: {framework}")
    if state.get("environment_python"):
        lines.append(f"Python usado: {state['environment_python']}")
    if tests_executed:
        lines.append(f"Resultado: {_test_result(state)}")
    elif state.get("failure_message"):
        lines.append(f"Resultado: {state['failure_message']}")
    if state.get("ci_run_id") or state.get("ci_decision"):
        lines.append(f"CI: {state.get('ci_decision') or state.get('ci_status') or 'n/a'}")
        if state.get("ci_validated_commit"):
            lines.append(f"CI commit: {str(state['ci_validated_commit'])[:12]}")

    if repair_phase == "completed":
        updated_files = state.get("files_updated_during_repair", [])
        if updated_files:
            lines.append(f"Archivo corregido: {', '.join(updated_files)}")
        if state.get("repair_decision"):
            lines.append(f"Reparación: {state['repair_decision']}")
        if state.get("repair_before"):
            lines.append(f"Antes: {state['repair_before']}")
        if state.get("repair_after"):
            lines.append(f"Después: {state['repair_after']}")
    if state.get("user_cancelled"):
        lines.append("Operación rechazada")
        if state.get("last_rejected_tool"):
            lines.append(f"Tool: {state['last_rejected_tool']}")
        lines.append(f"Motivo: {state.get('approval_reason') or state.get('failure_message') or 'Sin motivo indicado'}")
        lines.append("Proyecto no modificado")
    return "\n".join(lines)


async def finalize_node(state: SoftwareFactoryState, dependencies: GraphDependencies) -> dict[str, Any]:
    return {
        "final_response": build_final_summary(state),
        "terminal_status": terminal_status_from_state(state),
    }
