from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError
from api.services.observability_context import get_observability_context
from graph.nodes import GraphDependencies, _execute
from graph.observability import observed_node
from graph.subgraphs.planning.executability import validate_plan_executability
from graph.subgraphs.planning.knowledge import (
    PlannerKnowledgeContextBuilder,
    PlannerKnowledgeQueryBuilder,
)
from graph.subgraphs.planning.models import PlanningOutput
from graph.subgraphs.planning.quality import (
    build_quality_refinement_guidance,
    evaluate_quality_gate,
    score_plan_quality,
)
from graph.subgraphs.planning.risk import analyze_plan_risk
from graph.subgraphs.planning.routers import route_after_plan_validation
from graph.subgraphs.planning.service import (
    PlanningDomainError,
    PlanningService,
    validate_planning_contract,
    validate_planning_output,
)
from graph.subgraphs.planning.state import PlanningState
from streaming import EventStatus, WorkflowEventType, emit_workflow_event


PlanningNode = Callable[[PlanningState], Awaitable[dict[str, Any]]]
logger = logging.getLogger(__name__)


def _development_force_invalid() -> bool:
    enabled = os.getenv("LANGGRAPH_DEVELOPMENT", "false").lower() == "true"
    forced = os.getenv("PLANNING_FORCE_INVALID_FIRST_ATTEMPT", "false").lower() == "true"
    return enabled and forced


def _service(dependencies: GraphDependencies) -> PlanningService:
    configured = getattr(dependencies, "planning_service", None)
    if configured is not None:
        return configured
    return PlanningService(dependencies.openai_client, dependencies.model, dependencies.tool_executor)


def _logged_node(name: str, node: PlanningNode, dependencies: GraphDependencies) -> PlanningNode:
    async def invoke(state: PlanningState) -> dict[str, Any]:
        print(f"Planning subgraph node start: {name}")
        if dependencies.node_observer:
            dependencies.node_observer(name, "start")
        async with observed_node(dependencies, "planning", name, agent="Planner"):
            updates = await node(state)
        summary_state = dict(state)
        summary_state.update(updates)
        print(
            f"Planning subgraph node end: {name} "
            f"task_count={len(summary_state.get('implementation_tasks', []))} "
            f"planning_attempts={summary_state.get('planning_attempts', 0)} "
            f"planning_valid={summary_state.get('planning_valid', False)} "
            f"knowledge={summary_state.get('planner_knowledge_state', 'not_started')}"
        )
        if dependencies.node_observer:
            dependencies.node_observer(name, "end")
        return updates

    return invoke


def _route(state: PlanningState) -> str:
    selected = route_after_plan_validation(state)
    print(f"Planning route: validate_plan -> {selected}")
    return selected


def _planning_error_codes(errors: list[str]) -> list[str]:
    return list(dict.fromkeys(error.split(":", 1)[0] for error in errors))


def build_planning_subgraph(dependencies: GraphDependencies):
    service = _service(dependencies)
    knowledge_query_builder = PlannerKnowledgeQueryBuilder()
    knowledge_context_builder = PlannerKnowledgeContextBuilder()

    async def analyze_requirement(state: PlanningState) -> dict[str, Any]:
        try:
            analysis, criteria = await service.analyze(state)
            analysis_data = analysis.model_dump()
            if _development_force_invalid() and state.get("planning_attempts", 0) == 0:
                analysis_data["project_name"] = "forced-invalid-project"
            return {
                "requirement_analysis": analysis_data,
                "acceptance_criteria": [item.model_dump() for item in criteria],
            }
        except Exception as exc:
            code = getattr(exc, "code", "planning_schema_invalid")
            return {"planning_errors": [str(exc)], "planning_failure_reason": code}

    async def create_tasks(state: PlanningState) -> dict[str, Any]:
        if not state.get("requirement_analysis"):
            return {"planning_errors": ["No existe análisis estructurado."]}
        try:
            tasks = await service.create_tasks(state)
            emit_workflow_event(
                WorkflowEventType.STAGE_COMPLETED,
                source="planning_subgraph",
                stage="planning",
                status=EventStatus.COMPLETED,
                data={
                    "namespace": ["planning"],
                    "subgraph": "planning",
                    "node": "create_tasks",
                    "event": "plan_generated",
                    "task_count": len(tasks),
                },
            )
            return {"implementation_tasks": [item.model_dump() for item in tasks]}
        except Exception as exc:
            code = getattr(exc, "code", "planning_schema_invalid")
            return {"planning_errors": [str(exc)], "planning_failure_reason": code}

    async def retrieve_planner_knowledge(state: PlanningState) -> dict[str, Any]:
        request = knowledge_query_builder.build(state)
        previous_state = state.get("planner_knowledge_state", "not_started")
        if (
            state.get("planner_knowledge_query") == request.query
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
            "planner_knowledge_query": request.query,
            "planner_knowledge_retrieval_id": None,
            "planner_knowledge_context": None,
            "planner_knowledge_sources": [],
            "planner_knowledge_retrieval_used": False,
            "planner_retrieved_context_count": 0,
            "planner_knowledge_context_tokens": 0,
        }
        if not project_id:
            logger.warning("Planner knowledge retrieval skipped: project scope is unavailable")
            return {**base_updates, "planner_knowledge_state": "unavailable"}

        context = get_observability_context()
        arguments: dict[str, Any] = {
            "query": request.query,
            "project_id": project_id,
            "knowledge_types": list(request.knowledge_types),
            "limit": request.top_k,
            "agent_name": "Planner",
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
                node_name="prepare_planner_knowledge",
                operation="knowledge_retrieval",
            )
            payload = outcome.payload
            if outcome.is_error or not isinstance(payload, dict) or payload.get("degraded"):
                reason = payload.get("failure_type") if isinstance(payload, dict) else "invalid_response"
                logger.warning(
                    "Planner knowledge retrieval unavailable project=%s reason=%s",
                    project_id,
                    reason,
                )
                return {**base_updates, "planner_knowledge_state": "unavailable"}
            raw_results = payload.get("results", [])
            results = (
                [item for item in raw_results if isinstance(item, dict)]
                if isinstance(raw_results, list)
                else []
            )
            built = knowledge_context_builder.build(results, top_k=request.top_k)
            retrieval_id = payload.get("retrieval_id")
            retrieval_id = str(retrieval_id) if retrieval_id else None
            if not built.sources:
                return {
                    **base_updates,
                    "planner_knowledge_retrieval_id": retrieval_id,
                    "planner_knowledge_state": "empty",
                }
            return {
                **base_updates,
                "planner_knowledge_retrieval_id": retrieval_id,
                "planner_knowledge_context": built.context,
                "planner_knowledge_sources": [dict(source) for source in built.sources],
                "planner_knowledge_state": "available",
                "planner_knowledge_retrieval_used": True,
                "planner_retrieved_context_count": len(built.sources),
                "planner_knowledge_context_tokens": built.token_count,
            }
        except Exception as exc:
            logger.warning(
                "Planner knowledge retrieval failed open project=%s error=%s",
                project_id,
                type(exc).__name__,
            )
            return {**base_updates, "planner_knowledge_state": "unavailable"}

    async def prepare_planner_knowledge(state: PlanningState) -> dict[str, Any]:
        return await retrieve_planner_knowledge(state)

    async def validate_plan(state: PlanningState) -> dict[str, Any]:
        try:
            plan = PlanningOutput.model_validate(
                {
                    "analysis": state.get("requirement_analysis"),
                    "acceptance_criteria": state.get("acceptance_criteria", []),
                    "tasks": state.get("implementation_tasks", []),
                }
            )
            errors = validate_planning_output(plan, state)
        except ValidationError as exc:
            errors = [f"Schema de planificación inválido: {error['loc']}: {error['msg']}" for error in exc.errors()]
        updates: dict[str, Any] = {"planning_valid": not errors, "planning_errors": errors}
        if "plan" in locals():
            updates["requirement_analysis"] = plan.analysis.model_dump()
        if errors and state.get("planning_attempts", 0) >= state.get("max_planning_attempts", 2):
            updates.update(
                terminal_status="planning_failed",
                failure_type="planning_validation_failed",
                failure_stage="planning",
                failure_message="; ".join(errors),
                planning_failure_reason="; ".join(errors),
            )
        return updates

    async def refine_plan(state: PlanningState) -> dict[str, Any]:
        attempts = state.get("planning_attempts", 0) + 1
        knowledge_updates: dict[str, Any] = {}
        try:
            knowledge_updates = await retrieve_planner_knowledge(state)
            refinement_state = dict(state)
            refinement_state.update(knowledge_updates)
            plan = await service.refine(refinement_state)  # type: ignore[arg-type]
            return {
                **knowledge_updates,
                "requirement_analysis": plan.analysis.model_dump(),
                "acceptance_criteria": [item.model_dump() for item in plan.acceptance_criteria],
                "implementation_tasks": [item.model_dump() for item in plan.tasks],
                "planning_attempts": attempts,
                "planning_failure_reason": None,
                "planning_approval_required": False,
                "planning_approval_reason": None,
                "planning_approval_status": "not_required",
                "planning_approval_id": None,
                "planning_policy_decision": None,
                "planning_approval_fingerprint": None,
                "pending_operation": None,
                "pending_tool_name": None,
                "pending_tool_arguments": None,
                "pending_approval_preview": None,
                "pending_approval_status": "none",
            }
        except PlanningDomainError as exc:
            return {
                **knowledge_updates,
                "planning_attempts": attempts,
                "planning_errors": [str(exc)],
                "planning_failure_reason": exc.code,
            }

    async def validate_plan(state: PlanningState) -> dict[str, Any]:
        plan, errors = validate_planning_contract(
            {
                "analysis": state.get("requirement_analysis"),
                "acceptance_criteria": state.get("acceptance_criteria", []),
                "tasks": state.get("implementation_tasks", []),
            },
            state,  # type: ignore[arg-type]
        )
        execution_order: list[int] = []
        dependency_edges: list[dict[str, int]] = []
        risk_updates: dict[str, Any] = {
            "planning_risk_score": None,
            "planning_risk_level": None,
            "planning_risk_reasons": [],
            "planning_sensitive_tasks": [],
            "planning_impact_areas": [],
        }
        quality_updates: dict[str, Any] = {
            "planning_quality_score": None,
            "planning_quality_level": None,
            "planning_quality_dimensions": {},
            "planning_quality_issues": [],
            "planning_decision_confidence": None,
            "planning_quality_version": None,
            "planning_quality_gate_decision": None,
            "planning_quality_gate_reason": None,
            "planning_quality_refinement_required": False,
            "planning_quality_refinement_attempts": state.get("planning_quality_refinement_attempts", 0),
            "planning_quality_previous_score": None,
            "planning_quality_score_delta": None,
            "planning_quality_refinement_guidance": [],
        }
        if plan is not None and not errors:
            emit_workflow_event(
                WorkflowEventType.STAGE_STARTED,
                source="planning_subgraph",
                stage="planning",
                status=EventStatus.RUNNING,
                data={
                    "namespace": ["planning"],
                    "subgraph": "planning",
                    "node": "validate_plan",
                    "event": "executability_validation_started",
                    "attempt": state.get("planning_attempts", 0),
                    "task_count": len(plan.tasks),
                    "dependency_count": sum(len(task.depends_on) for task in plan.tasks),
                },
            )
            executable = validate_plan_executability(plan, state)  # type: ignore[arg-type]
            errors = executable.errors
            execution_order = executable.execution_order
            dependency_edges = executable.dependency_edges
            emit_workflow_event(
                WorkflowEventType.STAGE_COMPLETED if not errors else WorkflowEventType.STAGE_FAILED,
                source="planning_subgraph",
                stage="planning",
                status=EventStatus.COMPLETED if not errors else EventStatus.FAILED,
                data={
                    "namespace": ["planning"],
                    "subgraph": "planning",
                    "node": "validate_plan",
                    "event": "executability_validation_completed" if not errors else "executability_validation_failed",
                    "attempt": state.get("planning_attempts", 0),
                    "task_count": len(plan.tasks),
                    "dependency_count": len(dependency_edges),
                    "error_codes": _planning_error_codes(errors),
                    "duration_ms": executable.duration_ms,
                },
            )
            if not errors:
                emit_workflow_event(
                    WorkflowEventType.STAGE_STARTED,
                    source="planning_subgraph",
                    stage="planning",
                    status=EventStatus.RUNNING,
                    data={
                        "namespace": ["planning"],
                        "subgraph": "planning",
                        "node": "validate_plan",
                        "event": "risk_analysis_started",
                        "attempt": state.get("planning_attempts", 0),
                        "task_count": len(plan.tasks),
                    },
                )
                risk = analyze_plan_risk(plan)
                risk_updates = {
                    "planning_risk_score": risk.risk_score,
                    "planning_risk_level": risk.risk_level,
                    "planning_risk_reasons": risk.risk_reasons,
                    "planning_sensitive_tasks": risk.sensitive_tasks,
                    "planning_impact_areas": risk.impact_areas,
                }
                emit_workflow_event(
                    WorkflowEventType.STAGE_COMPLETED,
                    source="planning_subgraph",
                    stage="planning",
                    status=EventStatus.COMPLETED,
                    data={
                        "namespace": ["planning"],
                        "subgraph": "planning",
                        "node": "validate_plan",
                        "event": "risk_analysis_completed",
                        "attempt": state.get("planning_attempts", 0),
                        "task_count": len(plan.tasks),
                        "risk_score": risk.risk_score,
                        "risk_level": risk.risk_level,
                        "impact_area_count": len(risk.impact_areas),
                        "sensitive_task_count": len(risk.sensitive_tasks),
                        "duration_ms": risk.duration_ms,
                    },
                )
                emit_workflow_event(
                    WorkflowEventType.STAGE_STARTED,
                    source="planning_subgraph",
                    stage="planning",
                    status=EventStatus.RUNNING,
                    data={
                        "namespace": ["planning"],
                        "subgraph": "planning",
                        "node": "validate_plan",
                        "event": "plan_quality_scoring_started",
                        "attempt": state.get("planning_attempts", 0),
                    },
                )
                quality_state = dict(state)
                quality_state.update(risk_updates)
                quality_state.update(
                    planning_execution_order=execution_order,
                    planning_dependency_edges=dependency_edges,
                )
                quality = score_plan_quality(plan, quality_state)  # type: ignore[arg-type]
                quality_updates = {
                    "planning_quality_score": quality.score,
                    "planning_quality_level": quality.level,
                    "planning_quality_dimensions": quality.dimensions,
                    "planning_quality_issues": quality.issues,
                    "planning_decision_confidence": quality.decision_confidence,
                    "planning_quality_version": quality.version,
                }
                previous_score = state.get("planning_quality_score")
                score_delta = (
                    quality.score - int(previous_score)
                    if isinstance(previous_score, int)
                    else None
                )
                gate = evaluate_quality_gate(
                    quality_score=quality.score,
                    quality_level=quality.level,
                    quality_dimensions=quality.dimensions,
                    quality_issues=quality.issues,
                    decision_confidence=quality.decision_confidence,
                    attempts=state.get("planning_attempts", 0),
                    max_attempts=state.get("max_planning_attempts", 2),
                )
                guidance = build_quality_refinement_guidance(
                    quality_score=quality.score,
                    quality_dimensions=quality.dimensions,
                    quality_issues=quality.issues,
                    gate_reason=gate.reason,
                )
                quality_updates.update(
                    planning_quality_gate_decision=gate.decision,
                    planning_quality_gate_reason=gate.reason,
                    planning_quality_refinement_required=gate.refinement_required,
                    planning_quality_refinement_attempts=state.get("planning_quality_refinement_attempts", 0)
                    + (1 if gate.decision == "refine" else 0),
                    planning_quality_previous_score=previous_score,
                    planning_quality_score_delta=score_delta,
                    planning_quality_refinement_guidance=guidance if gate.refinement_required else [],
                )
                emit_workflow_event(
                    WorkflowEventType.STAGE_COMPLETED,
                    source="planning_subgraph",
                    stage="planning",
                    status=EventStatus.COMPLETED,
                    data={
                        "namespace": ["planning"],
                        "subgraph": "planning",
                        "node": "validate_plan",
                        "event": "plan_quality_scoring_completed",
                        "quality_score": quality.score,
                        "quality_level": quality.level,
                        "decision_confidence": quality.decision_confidence,
                        "issue_count": len(quality.issues),
                        "dimension_count": len(quality.dimensions),
                        "attempt": state.get("planning_attempts", 0),
                    },
                )
                emit_workflow_event(
                    WorkflowEventType.STAGE_COMPLETED if gate.decision == "continue" else WorkflowEventType.STAGE_FAILED,
                    source="planning_subgraph",
                    stage="planning",
                    status=EventStatus.COMPLETED if gate.decision == "continue" else EventStatus.FAILED,
                    data={
                        "namespace": ["planning"],
                        "subgraph": "planning",
                        "node": "validate_plan",
                        "event": "quality_gate_passed" if gate.decision == "continue" else "quality_refinement_required" if gate.decision == "refine" else "quality_gate_evaluated",
                        "attempt": state.get("planning_attempts", 0),
                        "quality_score": quality.score,
                        "quality_level": quality.level,
                        "decision_confidence": quality.decision_confidence,
                        "decision": gate.decision,
                        "reason_codes": gate.reason_codes,
                        "score_delta": score_delta,
                    },
                )
                if gate.decision in {"refine", "fail"}:
                    errors = [gate.reason]
        updates: dict[str, Any] = {
            "planning_valid": not errors,
            "planning_errors": errors,
            "planning_failure_reason": _planning_error_codes(errors)[0] if errors else None,
            "planning_execution_order": execution_order,
            "planning_dependency_edges": dependency_edges,
            **risk_updates,
            **quality_updates,
        }
        if plan is not None:
            updates.update(
                requirement_analysis=plan.analysis.model_dump(),
                acceptance_criteria=[item.model_dump() for item in plan.acceptance_criteria],
                implementation_tasks=[item.model_dump() for item in plan.tasks],
            )
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED if not errors else WorkflowEventType.STAGE_FAILED,
            source="planning_subgraph",
            stage="planning",
            status=EventStatus.COMPLETED if not errors else EventStatus.FAILED,
            data={
                "namespace": ["planning"],
                "subgraph": "planning",
                "node": "validate_plan",
                "event": "plan_validated" if not errors else "plan_validation_failed",
                "valid": not errors,
                "error_codes": _planning_error_codes(errors),
            },
        )
        if errors and state.get("planning_attempts", 0) >= state.get("max_planning_attempts", 2):
            updates.update(
                terminal_status="planning_failed",
                failure_type="planning_validation_failed",
                failure_stage="planning",
                failure_message="; ".join(errors),
                planning_failure_reason=_planning_error_codes(errors)[0],
            )
        return updates

    async def refine_plan(state: PlanningState) -> dict[str, Any]:
        attempts = state.get("planning_attempts", 0) + 1
        knowledge_updates: dict[str, Any] = {}
        try:
            emit_workflow_event(
                WorkflowEventType.STAGE_STARTED,
                source="planning_subgraph",
                stage="planning",
                status=EventStatus.RUNNING,
                data={
                    "namespace": ["planning"],
                    "subgraph": "planning",
                    "node": "refine_plan",
                    "event": "plan_refinement_started",
                    "attempt": attempts,
                    "error_codes": _planning_error_codes(state.get("planning_errors", [])),
                },
            )
            knowledge_updates = await retrieve_planner_knowledge(state)
            refinement_state = dict(state)
            refinement_state.update(knowledge_updates)
            plan = await service.refine(refinement_state)  # type: ignore[arg-type]
            emit_workflow_event(
                WorkflowEventType.STAGE_COMPLETED,
                source="planning_subgraph",
                stage="planning",
                status=EventStatus.COMPLETED,
                data={
                    "namespace": ["planning"],
                    "subgraph": "planning",
                    "node": "refine_plan",
                    "event": "plan_refinement_completed",
                    "attempt": attempts,
                },
            )
            return {
                **knowledge_updates,
                "requirement_analysis": plan.analysis.model_dump(),
                "acceptance_criteria": [item.model_dump() for item in plan.acceptance_criteria],
                "implementation_tasks": [item.model_dump() for item in plan.tasks],
                "planning_attempts": attempts,
                "planning_failure_reason": None,
                "planning_approval_required": False,
                "planning_approval_reason": None,
                "planning_approval_status": "not_required",
                "planning_approval_id": None,
                "planning_policy_decision": None,
                "planning_approval_fingerprint": None,
            }
        except PlanningDomainError as exc:
            emit_workflow_event(
                WorkflowEventType.STAGE_FAILED,
                source="planning_subgraph",
                stage="planning",
                status=EventStatus.FAILED,
                data={
                    "namespace": ["planning"],
                    "subgraph": "planning",
                    "node": "refine_plan",
                    "event": "plan_refinement_failed",
                    "attempt": attempts,
                    "error_codes": [exc.code],
                },
            )
            return {
                **knowledge_updates,
                "planning_attempts": attempts,
                "planning_errors": [f"{exc.code}: {exc}"],
                "planning_failure_reason": exc.code,
            }

    async def enter(state: PlanningState) -> dict[str, Any]:
        print("Parent graph node start: planning")
        if dependencies.node_observer:
            dependencies.node_observer("planning", "start")
        emit_workflow_event(
            WorkflowEventType.PLANNING_STARTED,
            source="planning_subgraph",
            stage="planning",
            status=EventStatus.RUNNING,
            data={"namespace": ["planning"], "subgraph": "planning", "node": "_entry"},
        )
        return {}

    async def leave(state: PlanningState) -> dict[str, Any]:
        print("Parent graph node end: planning")
        if dependencies.node_observer:
            dependencies.node_observer("planning", "end")
        valid = state.get("planning_valid") is True
        emit_workflow_event(
            WorkflowEventType.PLANNING_COMPLETED if valid else WorkflowEventType.PLANNING_FAILED,
            source="planning_subgraph",
            stage="planning",
            status=EventStatus.COMPLETED if valid else EventStatus.FAILED,
            data={
                "namespace": ["planning"],
                "subgraph": "planning",
                "node": "_exit",
                "valid": valid,
                "knowledge_retrieval_used": state.get(
                    "planner_knowledge_retrieval_used", False
                ),
                "retrieved_context_count": state.get(
                    "planner_retrieved_context_count", 0
                ),
                "retrieval_id": state.get("planner_knowledge_retrieval_id"),
            },
        )
        return {}

    builder = StateGraph(PlanningState)
    builder.add_node("_entry", enter)
    builder.add_node("analyze_requirement", _logged_node("analyze_requirement", analyze_requirement, dependencies))
    builder.add_node(
        "prepare_planner_knowledge",
        _logged_node(
            "prepare_planner_knowledge", prepare_planner_knowledge, dependencies
        ),
    )
    builder.add_node("create_tasks", _logged_node("create_tasks", create_tasks, dependencies))
    builder.add_node("validate_plan", _logged_node("validate_plan", validate_plan, dependencies))
    builder.add_node("refine_plan", _logged_node("refine_plan", refine_plan, dependencies))
    builder.add_node("_exit", leave)
    builder.add_edge(START, "_entry")
    builder.add_edge("_entry", "analyze_requirement")
    builder.add_edge("analyze_requirement", "prepare_planner_knowledge")
    builder.add_edge("prepare_planner_knowledge", "create_tasks")
    builder.add_edge("create_tasks", "validate_plan")
    builder.add_conditional_edges(
        "validate_plan",
        _route,
        {"valid": "_exit", "refine": "refine_plan", "failed": "_exit"},
    )
    builder.add_edge("refine_plan", "validate_plan")
    builder.add_edge("_exit", END)
    return builder.compile()
