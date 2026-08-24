from __future__ import annotations

import os
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from graph.nodes import (
    GraphDependencies,
    _execute,
    approval_node,
    detect_test_framework_node,
    execute_create_project_node,
    execute_prepare_environment_node,
    prepare_environment_request_node,
)
from graph.observability import observed_node
from api.services.observability_context import get_observability_context
from graph.subgraphs.implementation.knowledge import (
    DeveloperKnowledgeContextBuilder,
    DeveloperKnowledgeQueryBuilder,
)
from graph.subgraphs.implementation.models import ProjectImplementationPlan
from graph.subgraphs.implementation.routers import (
    MAX_DEPENDENCY_NORMALIZATION_ATTEMPTS,
    has_only_correctable_dependency_errors,
    route_after_create_execution,
    route_after_implementation_validation,
    route_implementation_approval,
    route_implementation_entry,
)
from graph.subgraphs.implementation.service import ImplementationDomainError, ImplementationService
from graph.subgraphs.implementation.state import ImplementationState
from graph.subgraphs.implementation.validators import collectable_tests, validate_project_implementation
from policies.dependencies import (
    dependency_normalization_errors,
    dependency_policy_for,
    normalize_dependency_file,
    requirement_lines,
    validate_dependency_file,
)
from streaming import EventStatus, WorkflowEventType, emit_workflow_event


Node = Callable[[ImplementationState], Awaitable[dict[str, Any]]]
logger = logging.getLogger(__name__)
MISSING_PROJECT_IMPLEMENTATION = "implementation_state_missing_project_implementation"
FAILURE_NODE_ALIASES = {
    "execute_create_project": "create_project",
    "execute_prepare_environment": "prepare_environment",
}


def _force_invalid() -> bool:
    development = os.getenv("LANGGRAPH_DEVELOPMENT", "false").lower() == "true"
    forced = os.getenv("IMPLEMENTATION_FORCE_INVALID_FIRST_ATTEMPT", "false").lower() == "true"
    return development and forced


def _service(dependencies: GraphDependencies) -> ImplementationService:
    configured = dependencies.implementation_service
    if configured is not None:
        return configured
    return ImplementationService(dependencies.openai_client, dependencies.model, dependencies.argument_resolver)


def require_project_implementation(state: ImplementationState) -> dict[str, Any]:
    raw_proposal = state.get("project_implementation")
    if not isinstance(raw_proposal, dict):
        raise ImplementationDomainError(
            MISSING_PROJECT_IMPLEMENTATION,
            MISSING_PROJECT_IMPLEMENTATION,
        )
    return raw_proposal


def canonicalize_project_implementation(
    proposal: ProjectImplementationPlan,
) -> ProjectImplementationPlan:
    return ProjectImplementationPlan.model_validate(proposal.model_dump())


def _summary(state: ImplementationState, updates: dict[str, Any]) -> str:
    merged = dict(state)
    merged.update(updates)
    return (
        f"implementation_attempts={merged.get('implementation_attempts', 0)} "
        f"implementation_valid={merged.get('implementation_valid', False)} "
        f"project_created={merged.get('project_created', False)} "
        f"environment_prepared={merged.get('environment_prepared', False)} "
        f"knowledge={merged.get('developer_knowledge_state', 'not_started')}"
    )


def _logged(name: str, node: Node, dependencies: GraphDependencies) -> Node:
    async def invoke(state: ImplementationState) -> dict[str, Any]:
        print(f"Implementation subgraph node start: {name}")
        if dependencies.node_observer:
            dependencies.node_observer(name, "start")
        agent = "Developer" if name not in {"approval", "entry_failure", "framework_failure"} else None
        async with observed_node(dependencies, "implementation", name, agent=agent):
            updates = await node(state)
        updates["last_completed_node"] = FAILURE_NODE_ALIASES.get(name, name)
        print(f"Implementation subgraph node end: {name} {_summary(state, updates)}")
        if dependencies.node_observer:
            dependencies.node_observer(name, "end")
        return updates

    return invoke


def _logged_router(source: str, router: Callable[[ImplementationState], str]):
    def route(state: ImplementationState) -> str:
        selected = router(state)
        prefix = "Implementation subgraph route" if source == "entry" else "Implementation route"
        print(f"{prefix}: {source} -> {selected}")
        return selected

    return route


def build_implementation_subgraph(dependencies: GraphDependencies):
    service = _service(dependencies)
    knowledge_query_builder = DeveloperKnowledgeQueryBuilder()
    knowledge_context_builder = DeveloperKnowledgeContextBuilder()

    async def enter(state: ImplementationState) -> dict[str, Any]:
        print("Parent graph node start: implementation")
        if dependencies.node_observer:
            dependencies.node_observer("implementation", "start")
        emit_workflow_event(
            WorkflowEventType.IMPLEMENTATION_STARTED,
            source="implementation_subgraph",
            stage="implementation",
            status=EventStatus.RUNNING,
            data={"namespace": ["implementation"], "subgraph": "implementation", "node": "_entry"},
        )
        return {}

    async def leave(state: ImplementationState) -> dict[str, Any]:
        print("Parent graph node end: implementation")
        if dependencies.node_observer:
            dependencies.node_observer("implementation", "end")
        failed = bool(state.get("implementation_failure_reason"))
        emit_workflow_event(
            WorkflowEventType.IMPLEMENTATION_FAILED if failed else WorkflowEventType.IMPLEMENTATION_COMPLETED,
            source="implementation_subgraph",
            stage="implementation",
            status=EventStatus.FAILED if failed else EventStatus.COMPLETED,
            data={
                "namespace": ["implementation"],
                "subgraph": "implementation",
                "node": "_exit",
                "project_created": state.get("project_created"),
                "knowledge_retrieval_used": state.get("knowledge_retrieval_used", False),
                "retrieved_context_count": state.get("retrieved_context_count", 0),
                "retrieval_id": state.get("developer_knowledge_retrieval_id"),
            },
        )
        return {}

    async def entry_failure(state: ImplementationState) -> dict[str, Any]:
        message = f"No se encontró el proyecto existente solicitado: {state.get('project_name')}."
        return {
            "terminal_status": "implementation_failed",
            "failure_type": "project_not_found",
            "failure_stage": "implementation",
            "failure_message": message,
            "implementation_failure_reason": message,
        }

    async def prepare_developer_knowledge(state: ImplementationState) -> dict[str, Any]:
        request = knowledge_query_builder.build(state)
        previous_state = state.get("developer_knowledge_state", "not_started")
        if (
            state.get("developer_knowledge_query") == request.query
            and previous_state in {"available", "empty", "unavailable"}
        ):
            return {}

        analysis = state.get("requirement_analysis") or {}
        analysis_project = analysis.get("project_name") if isinstance(analysis, dict) else None
        project_id = str(
            state.get("project_name")
            or analysis_project
            or state.get("created_project_name")
            or ""
        ).strip()
        base_updates: dict[str, Any] = {
            "developer_knowledge_query": request.query,
            "developer_knowledge_retrieval_id": None,
            "developer_knowledge_context": None,
            "developer_knowledge_sources": [],
            "knowledge_retrieval_used": False,
            "retrieved_context_count": 0,
            "knowledge_context_tokens": 0,
            "retrieval_id": None,
        }
        if not project_id:
            logger.warning("Developer knowledge retrieval skipped: project scope is unavailable")
            return {
                **base_updates,
                "developer_knowledge_state": "unavailable",
                "knowledge_context_state": "unavailable",
            }

        context = get_observability_context()
        arguments: dict[str, Any] = {
            "query": request.query,
            "project_id": project_id,
            "knowledge_types": list(request.knowledge_types),
            "limit": request.top_k,
            "agent_name": "Developer",
            "branch_id": context.branch_id,
        }
        correlation = {
            "workflow_id": context.workflow_id,
            "trace_id": context.trace_id,
            "parent_span_id": context.span_id,
        }
        arguments.update({key: value for key, value in correlation.items() if value})
        try:
            outcome = await _execute(
                "knowledge__get_relevant_context",
                arguments,
                state,  # type: ignore[arg-type]
                dependencies,
                node_name="prepare_developer_knowledge",
                operation="knowledge_retrieval",
            )
            payload = outcome.payload
            if outcome.is_error or not isinstance(payload, dict) or payload.get("degraded"):
                reason = payload.get("failure_type") if isinstance(payload, dict) else "invalid_response"
                logger.warning(
                    "Developer knowledge retrieval unavailable project=%s reason=%s",
                    project_id,
                    reason,
                )
                return {
                    **base_updates,
                    "developer_knowledge_state": "unavailable",
                    "knowledge_context_state": "unavailable",
                }
            raw_results = payload.get("results", [])
            results = [item for item in raw_results if isinstance(item, dict)] if isinstance(raw_results, list) else []
            built = knowledge_context_builder.build(results, top_k=request.top_k)
            retrieval_id = payload.get("retrieval_id")
            retrieval_id = str(retrieval_id) if retrieval_id else None
            if not built.sources:
                return {
                    **base_updates,
                    "developer_knowledge_retrieval_id": retrieval_id,
                    "retrieval_id": retrieval_id,
                    "developer_knowledge_state": "empty",
                    "knowledge_context_state": "empty",
                }
            return {
                **base_updates,
                "developer_knowledge_retrieval_id": retrieval_id,
                "developer_knowledge_context": built.context,
                "developer_knowledge_sources": [dict(source) for source in built.sources],
                "developer_knowledge_state": "available",
                "knowledge_context_state": "available",
                "knowledge_retrieval_used": True,
                "retrieved_context_count": len(built.sources),
                "knowledge_context_tokens": built.token_count,
                "retrieval_id": retrieval_id,
            }
        except Exception as exc:
            logger.warning(
                "Developer knowledge retrieval failed open project=%s error=%s",
                project_id,
                type(exc).__name__,
            )
            return {
                **base_updates,
                "developer_knowledge_state": "unavailable",
                "knowledge_context_state": "unavailable",
            }

    async def prepare_create_project(state: ImplementationState) -> dict[str, Any]:
        try:
            proposal = await service.generate_project(state)
            proposal = canonicalize_project_implementation(proposal)
            proposal_data = proposal.model_dump()
            if _force_invalid() and state.get("implementation_attempts", 0) == 0:
                proposal_data["files"] = [
                    file for file in proposal_data["files"] if not str(file["path"]).startswith("tests/")
                ]
            return {
                "project_implementation": proposal_data,
                "generated_package_name": proposal.package_name,
                "implementation_valid": False,
                "implementation_errors": [],
            }
        except ImplementationDomainError as exc:
            return {
                "implementation_errors": [str(exc)],
                "implementation_failure_reason": exc.code,
                "implementation_valid": False,
            }

    async def normalize_dependencies(state: ImplementationState) -> dict[str, Any]:
        attempts = state.get("dependency_normalization_attempts", 0) + 1
        try:
            raw_proposal = require_project_implementation(state)
            proposal = ProjectImplementationPlan.model_validate(raw_proposal)
        except ImplementationDomainError as exc:
            return {
                "dependency_policy_applied": False,
                "normalized_dependency_file": None,
                "dependency_policy_errors": [exc.code],
                "dependency_normalization_attempts": attempts,
                "implementation_errors": [exc.code],
                "remaining_validation_errors": [exc.code],
                "implementation_failure_reason": exc.code,
                "terminal_status": "implementation_failed",
                "failure_type": exc.code,
                "failure_stage": "implementation",
                "failure_message": str(exc),
            }
        except ValidationError as exc:
            return {
                "dependency_policy_applied": False,
                "normalized_dependency_file": None,
                "dependency_policy_errors": [f"implementation_schema_invalid: {exc}"],
                "dependency_normalization_attempts": attempts,
            }
        pre_normalization_errors = dependency_normalization_errors(proposal.framework, proposal.files)
        normalized_files = normalize_dependency_file(proposal.framework, proposal.files)
        normalized = proposal.model_copy(update={"files": normalized_files})
        requirements = next((file for file in normalized_files if file.path.casefold() == "requirements.txt"), None)
        policy = dependency_policy_for(proposal.framework)
        errors = [
            *pre_normalization_errors,
            *validate_dependency_file(proposal.framework, normalized_files),
        ]
        return {
            "project_implementation": normalized.model_dump(),
            "dependency_policy_applied": policy is not None and requirements is not None,
            "normalized_dependency_file": requirements.content if requirements is not None else None,
            "dependency_policy_errors": list(dict.fromkeys(errors)),
            "dependency_normalization_attempts": attempts,
        }

    async def validate_implementation(state: ImplementationState) -> dict[str, Any]:
        try:
            raw_proposal = require_project_implementation(state)
            proposal = ProjectImplementationPlan.model_validate(raw_proposal)
            errors = [
                *state.get("dependency_policy_errors", []),
                *validate_project_implementation(state, proposal),
            ]
            errors = list(dict.fromkeys(errors))
        except ImplementationDomainError as exc:
            errors = [exc.code]
            proposal = None
        except ValidationError as exc:
            errors = [f"Schema de implementación inválido: {error['loc']}: {error['msg']}" for error in exc.errors()]
            proposal = None
        refinement_input_errors = state.get("refinement_input_errors", [])
        resolved_errors = [error for error in refinement_input_errors if error not in errors]
        updates: dict[str, Any] = {
            "implementation_valid": not errors,
            "implementation_errors": errors,
            "resolved_validation_errors": resolved_errors,
            "remaining_validation_errors": errors,
        }
        if proposal is None and errors == [MISSING_PROJECT_IMPLEMENTATION]:
            updates.update(
                terminal_status="implementation_failed",
                failure_type=MISSING_PROJECT_IMPLEMENTATION,
                failure_stage="implementation",
                failure_message=MISSING_PROJECT_IMPLEMENTATION,
                implementation_failure_reason=MISSING_PROJECT_IMPLEMENTATION,
            )
        collected = collectable_tests(proposal) if proposal is not None else []
        updates.update(
            collectable_test_count=len(collected),
            test_functions=collected,
        )
        if not errors and proposal is not None:
            arguments = {
                "project_name": proposal.project_name,
                "files": [file.model_dump() for file in proposal.files],
            }
            updates.update(
                pending_operation="create_project",
                pending_tool_name="filesystem__create_project_structure",
                pending_tool_arguments=arguments,
                pending_approval_preview={
                    "project_name": proposal.project_name,
                    "files": [
                        {"path": file.path, "size_bytes": len(file.content.encode("utf-8"))}
                        for file in proposal.files
                    ],
                    "total_files": len(proposal.files),
                    "normalized_dependencies": requirement_lines(
                        state.get("normalized_dependency_file") or ""
                    ),
                    "test_framework": "pytest",
                    "collectable_test_count": len(collected),
                    "test_functions": collected,
                },
                pending_approval_status="waiting",
                approval_reason=None,
            )
        elif refinement_input_errors and set(errors) == set(refinement_input_errors):
            message = "El refinamiento no resolvio ningun error de validacion."
            updates.update(
                terminal_status="implementation_failed",
                failure_type="implementation_refinement_made_no_progress",
                failure_stage="implementation",
                failure_message=message,
                implementation_failure_reason=message,
            )
        elif (
            has_only_correctable_dependency_errors({**state, "implementation_errors": errors})
            and state.get("dependency_normalization_attempts", 0) >= MAX_DEPENDENCY_NORMALIZATION_ATTEMPTS
        ):
            message = "; ".join(errors)
            updates.update(
                terminal_status="implementation_failed",
                failure_type="dependency_policy_normalization_failed",
                failure_stage="implementation",
                failure_message=message,
                implementation_failure_reason=message,
            )
        elif state.get("implementation_attempts", 0) >= state.get("max_implementation_attempts", 2):
            message = "; ".join(errors)
            updates.update(
                terminal_status="implementation_failed",
                failure_type="implementation_validation_failed",
                failure_stage="implementation",
                failure_message=message,
                implementation_failure_reason=message,
            )
        emit_workflow_event(
            WorkflowEventType.IMPLEMENTATION_VALIDATION_COMPLETED,
            source="implementation_subgraph",
            stage="implementation",
            status=EventStatus.COMPLETED if not errors else EventStatus.FAILED,
            data={
                "namespace": ["implementation", "validate_implementation"],
                "subgraph": "implementation",
                "node": "validate_implementation",
                "valid": not errors,
                "error_count": len(errors),
            },
        )
        return updates

    async def refine_implementation(state: ImplementationState) -> dict[str, Any]:
        attempts = state.get("implementation_attempts", 0) + 1
        try:
            require_project_implementation(state)
            proposal = await service.refine_project(state)
            proposal = canonicalize_project_implementation(proposal)
            normalized_dependencies = state.get("normalized_dependency_file")
            if normalized_dependencies:
                proposal = proposal.model_copy(
                    update={
                        "files": [
                            file.model_copy(update={"content": normalized_dependencies})
                            if file.path.casefold() == "requirements.txt"
                            else file
                            for file in proposal.files
                        ]
                    }
                )
            return {
                "project_implementation": proposal.model_dump(),
                "generated_package_name": proposal.package_name,
                "implementation_attempts": attempts,
                "refinement_input_errors": list(state.get("implementation_errors", [])),
                "dependency_normalization_attempts": 0,
                "implementation_failure_reason": None,
            }
        except ImplementationDomainError as exc:
            terminal_updates = (
                {
                    "terminal_status": "implementation_failed",
                    "failure_type": exc.code,
                    "failure_stage": "implementation",
                    "failure_message": str(exc),
                }
                if exc.code == MISSING_PROJECT_IMPLEMENTATION
                else {}
            )
            return {
                **terminal_updates,
                "implementation_attempts": attempts,
                "refinement_input_errors": list(state.get("implementation_errors", [])),
                "dependency_normalization_attempts": 0,
                "implementation_errors": [str(exc)],
                "implementation_failure_reason": exc.code,
            }

    async def detect_framework(state: ImplementationState) -> dict[str, Any]:
        return await detect_test_framework_node(state, dependencies)

    async def framework_failure(state: ImplementationState) -> dict[str, Any]:
        message = "No se pudo detectar un framework de pruebas para preparar el entorno."
        return {
            "test_infrastructure_failed": True,
            "terminal_status": "infrastructure_failed",
            "failure_type": "framework_detection_failed",
            "failure_stage": "implementation",
            "failure_message": message,
            "implementation_failure_reason": message,
        }

    async def prepare_environment(state: ImplementationState) -> dict[str, Any]:
        return await prepare_environment_request_node(state, dependencies)

    async def approve(state: ImplementationState) -> dict[str, Any]:
        return await approval_node(state, dependencies)

    async def execute_create(state: ImplementationState) -> dict[str, Any]:
        return await execute_create_project_node(state, dependencies)

    async def execute_environment(state: ImplementationState) -> dict[str, Any]:
        return await execute_prepare_environment_node(state, dependencies)

    def route_after_detect(state: ImplementationState) -> str:
        return "continue" if state.get("detected_test_framework") else "failed"

    builder = StateGraph(ImplementationState)
    builder.add_node("_entry", enter)
    builder.add_node("entry_failure", _logged("entry_failure", entry_failure, dependencies))
    builder.add_node(
        "prepare_developer_knowledge",
        _logged("prepare_developer_knowledge", prepare_developer_knowledge, dependencies),
    )
    builder.add_node("prepare_create_project", _logged("prepare_create_project", prepare_create_project, dependencies))
    builder.add_node("normalize_dependencies", _logged("normalize_dependencies", normalize_dependencies, dependencies))
    builder.add_node("validate_implementation", _logged("validate_implementation", validate_implementation, dependencies))
    builder.add_node("refine_implementation", _logged("refine_implementation", refine_implementation, dependencies))
    builder.add_node("approval", _logged("approval", approve, dependencies))
    builder.add_node("execute_create_project", _logged("execute_create_project", execute_create, dependencies))
    builder.add_node("detect_test_framework", _logged("detect_test_framework", detect_framework, dependencies))
    builder.add_node("framework_failure", _logged("framework_failure", framework_failure, dependencies))
    builder.add_node("prepare_environment_request", _logged("prepare_environment_request", prepare_environment, dependencies))
    builder.add_node("execute_prepare_environment", _logged("execute_prepare_environment", execute_environment, dependencies))
    builder.add_node("_exit", leave)
    builder.add_edge(START, "_entry")
    builder.add_conditional_edges(
        "_entry",
        _logged_router("entry", route_implementation_entry),
        {"create": "prepare_developer_knowledge", "existing": "detect_test_framework", "failed": "entry_failure"},
    )
    builder.add_edge("entry_failure", "_exit")
    builder.add_edge("prepare_developer_knowledge", "prepare_create_project")
    builder.add_edge("prepare_create_project", "normalize_dependencies")
    builder.add_edge("normalize_dependencies", "validate_implementation")
    builder.add_conditional_edges(
        "validate_implementation",
        _logged_router("validate_implementation", route_after_implementation_validation),
        {
            "valid": "approval",
            "normalize": "normalize_dependencies",
            "refine": "refine_implementation",
            "failed": "_exit",
        },
    )
    builder.add_edge("refine_implementation", "normalize_dependencies")
    builder.add_conditional_edges(
        "approval",
        _logged_router("approval", route_implementation_approval),
        {
            "execute_create_project": "execute_create_project",
            "execute_prepare_environment": "execute_prepare_environment",
            "rejected": "_exit",
        },
    )
    builder.add_conditional_edges(
        "execute_create_project",
        _logged_router("execute_create_project", route_after_create_execution),
        {"continue": "detect_test_framework", "failed": "_exit"},
    )
    builder.add_conditional_edges(
        "detect_test_framework",
        _logged_router("detect_test_framework", route_after_detect),
        {"continue": "prepare_environment_request", "failed": "framework_failure"},
    )
    builder.add_edge("framework_failure", "_exit")
    builder.add_edge("prepare_environment_request", "approval")
    builder.add_edge("execute_prepare_environment", "_exit")
    builder.add_edge("_exit", END)
    return builder.compile()
