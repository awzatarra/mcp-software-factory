from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain_core.runnables import RunnableConfig

from graph.failure_injection import raise_if_failure_injected
from graph.debug import state_debug_summary
from graph.agent_performance import agent_performance_evaluation_node
from graph.failure_attribution import failure_attribution_node
from graph.nodes import (
    GraphDependencies,
    approval_node,
    detect_intent_node,
    finalize_node,
    inspect_workspace_node,
)
from graph.git_workflow import (
    execute_git_commit_node,
    git_tools_available,
    prepare_git_workflow_node,
    reject_git_commit_node,
    prepare_git_promotion_node,
    execute_git_promotion_node,
    reject_git_promotion_node,
)
from graph.observability import observed_node, observed_subgraph
from graph.planner_evaluation import planner_evaluation_node
from graph.planner_hybrid_evaluation import planner_hybrid_evaluation_node
from graph.planner_judge import planner_judge_node
from graph.planning_approval_policy import (
    evaluate_planning_approval_policy,
    planning_approval_fingerprint,
)
from graph.state import SoftwareFactoryState
from graph.workflow_learning import (
    extract_workflow_learnings_node,
    submit_workflow_learnings_node,
)
from graph.supervisor.routers import route_after_supervisor
from graph.supervisor.config import SupervisorDevelopmentConfig
from graph.supervisor.service import SupervisorService, supervisor_node
from graph.subgraph_adapters import (
    from_implementation_output,
    from_planning_output,
    from_testing_output,
    to_implementation_input,
    to_planning_input,
    to_testing_input,
)
from graph.subgraphs.implementation.builder import build_implementation_subgraph
from graph.subgraphs.planning.builder import build_planning_subgraph
from graph.subgraphs.planning.models import PlanningOutput
from graph.subgraphs.planning.quality import score_plan_quality
from graph.subgraphs.testing_repair.builder import build_testing_repair_subgraph
from streaming import EventStatus, WorkflowEventType, emit_workflow_event
from api.services.ci_service import classify_ci_failure_for_repair


Node = Callable[[SoftwareFactoryState, GraphDependencies], Awaitable[dict[str, Any]]]
FAILURE_NODE_ALIASES: dict[str, str] = {}


def _debug_enabled() -> bool:
    return os.getenv("MCP_FACTORY_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}


def _logged_node(name: str, node: Node, dependencies: GraphDependencies) -> Callable[[SoftwareFactoryState], Awaitable[dict[str, Any]]]:
    async def invoke(state: SoftwareFactoryState) -> dict[str, Any]:
        previous_node = state.get("last_completed_node")
        if previous_node:
            raise_if_failure_injected(previous_node)
        print(f"Parent graph node start: {name}")
        emit_workflow_event(
            WorkflowEventType.STAGE_STARTED,
            source="parent_graph",
            stage=name,
            status=EventStatus.RUNNING,
            data={"node": name, "namespace": ["parent", name]},
        )
        if name == "inspect_workspace":
            emit_workflow_event(
                WorkflowEventType.WORKSPACE_INSPECTION_STARTED,
                source="parent_graph",
                stage="inspect_workspace",
                status=EventStatus.RUNNING,
                data={"node": name, "namespace": ["parent", name]},
            )
        if dependencies.node_observer:
            dependencies.node_observer(name, "start")
        try:
            async with observed_subgraph(dependencies, name):
                async with observed_node(
                    dependencies,
                    name,
                    name,
                    agent="Supervisor" if name == "supervisor" else None,
                ):
                    updates = await node(state, dependencies)
        except Exception as exc:
            emit_workflow_event(
                WorkflowEventType.STAGE_FAILED,
                source="parent_graph",
                stage=name,
                status=EventStatus.FAILED,
                data={"node": name, "namespace": ["parent", name], "failure_type": type(exc).__name__},
            )
            raise
        updates["last_completed_node"] = FAILURE_NODE_ALIASES.get(name, name)
        if name == "finalize":
            print("Parent graph node end: finalize")
            print(f"- terminal_status: {updates.get('terminal_status')}")
            print(f"- final_response_length: {len(str(updates.get('final_response', '')))}")
            if _debug_enabled():
                debug_state = dict(state)
                debug_state.update(updates)
                print(json.dumps(state_debug_summary(debug_state), ensure_ascii=False, indent=2))
        elif name in {"extract_workflow_learnings", "submit_workflow_learnings"}:
            print(
                f"Parent graph node end: {name} "
                f"state={updates.get('workflow_learning_state')} "
                f"count={len(updates.get('workflow_learning_candidates') or updates.get('workflow_learning_submission_results') or [])}"
            )
        else:
            summary = {key: value for key, value in updates.items() if key not in {"test_stdout", "test_stderr", "failing_test_content", "related_source_content"}}
            print(f"Parent graph node end: {name} updates={summary}")
        if dependencies.node_observer:
            dependencies.node_observer(name, "end")
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="parent_graph",
            stage=name,
            status=EventStatus.COMPLETED,
            data={"node": name, "namespace": ["parent", name]},
        )
        if name == "inspect_workspace":
            emit_workflow_event(
                WorkflowEventType.WORKSPACE_INSPECTION_COMPLETED,
                source="parent_graph",
                stage="inspect_workspace",
                status=EventStatus.COMPLETED,
                data={"node": name, "namespace": ["parent", name]},
            )
        return updates

    return invoke


def _logged_router(source: str, router: Callable[[SoftwareFactoryState], str]) -> Callable[[SoftwareFactoryState], str]:
    def route(state: SoftwareFactoryState) -> str:
        selected = router(state)
        print(f"Parent graph route: {source} -> {selected}")
        return selected

    return route


async def _testing_repair_precondition_failure(
    state: SoftwareFactoryState,
    dependencies: GraphDependencies,
) -> dict[str, Any]:
    missing: list[str] = []
    if not (state.get("created_project_name") or state.get("project_name")):
        missing.append("project_name")
    if not state.get("environment_prepared"):
        missing.append("environment_prepared")
    if not state.get("detected_test_framework"):
        missing.append("detected_test_framework")
    if not state.get("expected_test_command"):
        missing.append("expected_test_command")
    return {
        "test_infrastructure_failed": True,
        "failure_type": "testing_repair_precondition_failed",
        "failure_stage": "testing_repair_entry",
        "failure_message": f"No se puede iniciar testing/reparación; faltan: {', '.join(missing)}.",
    }


def build_software_factory_graph(
    dependencies: GraphDependencies,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
):
    builder = StateGraph(SoftwareFactoryState)
    testing_repair_subgraph = build_testing_repair_subgraph(dependencies)
    planning_subgraph = build_planning_subgraph(dependencies)
    implementation_subgraph = build_implementation_subgraph(dependencies)
    supervisor_service = dependencies.supervisor_service or SupervisorService(
        openai_client=dependencies.openai_client,
        model=dependencies.model,
        config=SupervisorDevelopmentConfig.from_env(),
    )
    git_enabled = git_tools_available(dependencies)
    promotion_required = os.getenv("GIT_PROMOTION_REQUIRED", "false").casefold() == "true"
    ci_promotion_required = (
        dependencies.ci_service is not None
        and os.getenv("CI_PROMOTION_REQUIRED", "true").casefold() == "true"
    )
    ci_promotion_allow_warnings = os.getenv("CI_PROMOTION_ALLOW_WARNINGS", "true").casefold() == "true"
    ci_repair_enabled = os.getenv("CI_REPAIR_ENABLED", "true").casefold() == "true"
    ci_repair_max_attempts = int(os.getenv("CI_REPAIR_MAX_ATTEMPTS", "2"))
    auto_prepare_promotion = (
        os.getenv("GIT_AUTO_PREPARE_PROMOTION", "false").casefold() == "true"
        or promotion_required
    )

    async def run_planning_subgraph(
        state: SoftwareFactoryState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        subgraph_input = to_planning_input(state)
        if _debug_enabled():
            print(f"Parent -> Planning input keys: {sorted(subgraph_input)}")
        async with observed_subgraph(dependencies, "planning"):
            result = await planning_subgraph.ainvoke(subgraph_input, config=config)
        updates = from_planning_output(result, debug=_debug_enabled())
        updates["last_completed_stage"] = "planning"
        if _debug_enabled():
            print(f"Planning -> Parent output keys: {sorted(updates)}")
        return updates

    async def run_implementation_subgraph(
        state: SoftwareFactoryState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        subgraph_input = to_implementation_input(state)
        if _debug_enabled():
            print(f"Parent -> Implementation input keys: {sorted(subgraph_input)}")
        async with observed_subgraph(dependencies, "implementation"):
            result = await implementation_subgraph.ainvoke(subgraph_input, config=config)
        updates = from_implementation_output(result, debug=_debug_enabled())
        updates["last_completed_stage"] = "implementation"
        if _debug_enabled():
            print(f"Implementation -> Parent output keys: {sorted(updates)}")
        return updates

    async def run_testing_subgraph(
        state: SoftwareFactoryState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        subgraph_input = to_testing_input(state)
        if _debug_enabled():
            print(f"Parent -> TestingRepair input keys: {sorted(subgraph_input)}")
        async with observed_subgraph(dependencies, "testing_repair"):
            result = await testing_repair_subgraph.ainvoke(subgraph_input, config=config)
        updates = from_testing_output(result, debug=_debug_enabled())
        updates["last_completed_stage"] = "testing_repair"
        if _debug_enabled():
            print(f"TestingRepair -> Parent output keys: {sorted(updates)}")
        return updates

    async def run_supervisor(
        state: SoftwareFactoryState,
        _dependencies: GraphDependencies,
    ) -> dict[str, Any]:
        next_handoff_sequence = len(state.get("handoff_history") or []) + 1
        emit_workflow_event(
            WorkflowEventType.SUPERVISOR_DECISION_STARTED,
            source="supervisor",
            stage="supervisor",
            status=EventStatus.RUNNING,
            data={"handoff_sequence": next_handoff_sequence},
        )
        effective_state = dict(state)
        effective_state["git_integration_enabled"] = git_enabled
        effective_state["git_auto_prepare_promotion"] = auto_prepare_promotion
        effective_state["git_promotion_required"] = promotion_required
        effective_state["ci_promotion_required"] = ci_promotion_required
        effective_state["ci_promotion_allow_warnings"] = ci_promotion_allow_warnings
        effective_state["ci_repair_enabled"] = ci_repair_enabled
        effective_state["ci_repair_max_attempts"] = ci_repair_max_attempts
        updates = await supervisor_node(effective_state, supervisor_service)  # type: ignore[arg-type]
        updates["git_integration_enabled"] = git_enabled
        updates["git_auto_prepare_promotion"] = auto_prepare_promotion
        updates["git_promotion_required"] = promotion_required
        updates["ci_promotion_required"] = ci_promotion_required
        updates["ci_promotion_allow_warnings"] = ci_promotion_allow_warnings
        updates["ci_repair_enabled"] = ci_repair_enabled
        updates["ci_repair_max_attempts"] = ci_repair_max_attempts
        handoff_sequence = len(updates.get("handoff_history") or state.get("handoff_history") or [])
        event_data = {
            "attempted_target": (updates.get("handoff_history") or [{}])[-1].get("attempted_target"),
            "executed_target": updates.get("supervisor_decision"),
            "decision_source": updates.get("supervisor_decision_source"),
            "confidence": updates.get("supervisor_confidence"),
            "invalid_decision_count": updates.get("supervisor_invalid_decision_count"),
            "loop_detected": updates.get("failure_type") == "supervisor_loop_detected",
            "handoff_sequence": handoff_sequence,
        }
        emit_workflow_event(
            WorkflowEventType.SUPERVISOR_DECISION_COMPLETED,
            source="supervisor",
            stage="supervisor",
            status=EventStatus.COMPLETED,
            data=event_data,
        )
        if updates.get("supervisor_decision_source") == "fallback":
            emit_workflow_event(
                WorkflowEventType.SUPERVISOR_FALLBACK_USED,
                source="supervisor",
                stage="supervisor",
                status=EventStatus.COMPLETED,
                data=event_data,
            )
        if event_data["loop_detected"]:
            emit_workflow_event(
                WorkflowEventType.SUPERVISOR_LOOP_DETECTED,
                source="supervisor",
                stage="supervisor",
                status=EventStatus.FAILED,
                data=event_data,
            )
        emit_workflow_event(
            WorkflowEventType.HANDOFF_COMPLETED,
            source="supervisor",
            stage="supervisor",
            status=EventStatus.COMPLETED,
            data=event_data,
        )
        return updates

    async def run_ci_pipeline(
        state: SoftwareFactoryState,
        _dependencies: GraphDependencies,
    ) -> dict[str, Any]:
        ci_service = _dependencies.ci_service
        if ci_service is None:
            return {
                "ci_state": "unavailable",
                "terminal_status": "infrastructure_failed",
                "failure_stage": "ci",
                "failure_type": "ci_service_unavailable",
                "failure_message": "CI service is unavailable.",
            }
        project_id = str(state.get("created_project_name") or state.get("project_name") or "")
        commit = (
            state.get("git_repair_commit_sha")
            or state.get("git_developer_commit_sha")
            or state.get("git_head_commit")
        )
        if not project_id or not commit:
            return {
                "ci_state": "failed",
                "terminal_status": "ci_failed",
                "failure_stage": "ci",
                "failure_type": "ci_commit_missing",
                "failure_message": "CI requires a durable Git commit.",
            }
        existing = await ci_service.promotion_eligibility(str(state.get("workflow_id") or ""), commit)
        if existing.eligible:
            lineage = list(state.get("ci_repair_lineage") or [])
            if state.get("ci_repair_source_run_id") and state.get("ci_repair_state") in {"pending", "running"}:
                lineage = [
                    *lineage,
                    {
                        "attempt": state.get("ci_repair_attempts", 0),
                        "source_run_id": state.get("ci_repair_source_run_id"),
                        "source_commit": state.get("ci_repair_source_commit"),
                        "repair_commit": existing.source_commit,
                        "result_run_id": existing.run_id,
                        "result_decision": existing.ci_decision,
                    },
                ][-ci_repair_max_attempts:]
            return {
                "ci_state": "passed",
                "ci_run_id": existing.run_id,
                "ci_status": existing.ci_status,
                "ci_decision": existing.ci_decision,
                "ci_validated_commit": existing.source_commit,
                "ci_gate_summary": {},
                "ci_promotion_eligible": True,
                "ci_promotion_eligibility": existing.model_dump(mode="json"),
                "ci_repair_state": "repaired" if state.get("ci_repair_source_run_id") else state.get("ci_repair_state", "not_required"),
                "ci_repair_target_commit": existing.source_commit if state.get("ci_repair_source_run_id") else state.get("ci_repair_target_commit"),
                "ci_repair_lineage": lineage,
                "terminal_status": None,
            }
        try:
            preview = await ci_service.prepare(
                str(state.get("workflow_id") or ""),
                project_id,
                source_commit=commit,
                source_branch=state.get("git_workflow_branch"),
                workflow_branch=state.get("git_workflow_branch"),
                repository_root=project_id,
            )
            run = await ci_service.run(
                str(state.get("workflow_id") or ""),
                project_id,
                expected_fingerprint=preview.pipeline_fingerprint,
                source_commit=commit,
                source_branch=state.get("git_workflow_branch"),
                workflow_branch=state.get("git_workflow_branch"),
                repository_root=project_id,
            )
        except Exception as exc:
            failure_type = getattr(exc, "code", type(exc).__name__)
            return {
                "ci_state": "infrastructure_failed",
                "terminal_status": "infrastructure_failed",
                "failure_stage": "ci",
                "failure_type": str(failure_type),
                "failure_message": str(exc),
            }
        eligibility = await ci_service.promotion_eligibility(str(state.get("workflow_id") or ""), commit)
        if run.decision == "rejected":
            repairability = run.repairability or classify_ci_failure_for_repair(run)
            common_failure = {
                "ci_state": "failed",
                "ci_run_id": run.ci_run_id,
                "ci_status": run.status,
                "ci_decision": run.decision,
                "ci_validated_commit": run.ci_validated_commit,
                "ci_failure_type": run.failure_type,
                "ci_failure_message": run.failure_message,
                "ci_gate_summary": run.gate_summary,
                "ci_promotion_eligible": False,
                "ci_promotion_eligibility": eligibility.model_dump(mode="json"),
                "ci_repair_repairability": repairability.model_dump(mode="json"),
                "ci_repair_failure_type": run.failure_type,
                "ci_repair_failure_message": run.failure_message,
                "ci_repair_source_run_id": run.ci_run_id,
                "ci_repair_source_commit": run.ci_validated_commit,
            }
            if repairability.category == "infrastructure":
                return {
                    **common_failure,
                    "ci_repair_state": "not_repairable",
                    "terminal_status": "infrastructure_failed",
                    "failure_stage": "ci",
                    "failure_type": run.failure_type or "ci_infrastructure_failure",
                    "failure_message": run.failure_message or repairability.summary or "CI infrastructure failed.",
                }
            if (
                not ci_repair_enabled
                or not repairability.repairable
            ):
                return {
                    **common_failure,
                    "ci_repair_state": "not_repairable",
                    "terminal_status": "ci_failed",
                    "failure_stage": "ci",
                    "failure_type": run.failure_type or "ci_repair_not_applicable",
                    "failure_message": run.failure_message or repairability.summary or "CI failure is not repairable.",
                }
            attempts = int(state.get("ci_repair_attempts", 0))
            if attempts >= ci_repair_max_attempts:
                lineage = [
                    *list(state.get("ci_repair_lineage") or []),
                    {
                        "attempt": attempts,
                        "source_run_id": state.get("ci_repair_source_run_id"),
                        "source_commit": state.get("ci_repair_source_commit"),
                        "repair_commit": run.ci_validated_commit,
                        "result_run_id": run.ci_run_id,
                        "result_decision": run.decision,
                    },
                ][-ci_repair_max_attempts:]
                return {
                    **common_failure,
                    "ci_repair_state": "exhausted",
                    "ci_repair_lineage": lineage,
                    "terminal_status": "ci_failed",
                    "failure_stage": "ci",
                    "failure_type": "ci_repair_exhausted",
                    "failure_message": "CI repair attempts were exhausted.",
                }
            emit_workflow_event(
                WorkflowEventType.STAGE_COMPLETED,
                source="ci_repair",
                stage="ci_repair",
                status=EventStatus.COMPLETED,
                data={
                    "event": "ci_failure_classified",
                    "workflow_id": state.get("workflow_id"),
                    "ci_run_id": run.ci_run_id,
                    "source_commit": run.ci_validated_commit,
                    "category": repairability.category,
                    "repairable": repairability.repairable,
                    "attempt": attempts + 1,
                },
            )
            return {
                **common_failure,
                "ci_repair_state": "pending",
                "ci_repair_attempts": attempts + 1,
                "terminal_status": None,
                "tests_executed": True,
                "tests_passed": False,
                "testing_result": {
                    **(state.get("testing_result") or {}),
                    "tests_executed": True,
                    "tests_passed": False,
                    "summary": repairability.summary,
                    "repair_phase": state.get("repair_phase", "not_started"),
                    "repair_attempts": state.get("repair_attempts", 0),
                },
                "failure_stage": "ci",
                "failure_type": run.failure_type or repairability.failure_type or "ci_promotion_blocked",
                "failure_message": run.failure_message or repairability.summary or "CI gates rejected the commit.",
            }
        return {
            "ci_state": "passed" if eligibility.eligible else "failed",
            "ci_run_id": run.ci_run_id,
            "ci_status": run.status,
            "ci_decision": run.decision,
            "ci_validated_commit": run.ci_validated_commit,
            "ci_failure_type": run.failure_type,
            "ci_failure_message": run.failure_message,
            "ci_gate_summary": run.gate_summary,
            "ci_promotion_eligible": eligibility.eligible,
            "ci_promotion_eligibility": eligibility.model_dump(mode="json"),
            "ci_repair_state": "repaired" if state.get("ci_repair_source_run_id") else state.get("ci_repair_state", "not_required"),
            "terminal_status": None if eligibility.eligible else "ci_failed",
        }

    async def run_supervisor_loop_probe(
        state: SoftwareFactoryState,
    ) -> dict[str, Any]:
        return {}

    def _planning_result_with_approval(state: SoftwareFactoryState, updates: dict[str, Any]) -> dict[str, Any]:
        planning_result = dict(state.get("planning_result") or {})
        approval = dict(planning_result.get("approval") or {})
        approval.update(
            {
                "required": updates.get("planning_approval_required", state.get("planning_approval_required", False)),
                "status": updates.get("planning_approval_status", state.get("planning_approval_status")),
                "approval_id": updates.get("planning_approval_id", state.get("planning_approval_id")),
                "fingerprint": updates.get("planning_approval_fingerprint", state.get("planning_approval_fingerprint")),
                "reason": updates.get("planning_approval_reason", state.get("planning_approval_reason")),
                "policy_decision": updates.get("planning_policy_decision", state.get("planning_policy_decision")),
            }
        )
        planning_result["approval"] = approval
        quality = dict(planning_result.get("quality") or {})
        if "planning_quality_score" in updates or "planning_decision_confidence" in updates:
            quality.update(
                {
                    "score": updates.get("planning_quality_score", state.get("planning_quality_score")),
                    "level": updates.get("planning_quality_level", state.get("planning_quality_level")),
                    "dimensions": updates.get("planning_quality_dimensions", state.get("planning_quality_dimensions", {})),
                    "issues": updates.get("planning_quality_issues", state.get("planning_quality_issues", [])),
                    "decision_confidence": updates.get("planning_decision_confidence", state.get("planning_decision_confidence")),
                    "version": updates.get("planning_quality_version", state.get("planning_quality_version")),
                }
            )
            planning_result["quality"] = quality
        return planning_result

    def _planning_quality_updates(state: SoftwareFactoryState, updates: dict[str, Any]) -> dict[str, Any]:
        planning_result = dict(state.get("planning_result") or {})
        raw_plan = {
            "analysis": planning_result.get("analysis") or state.get("requirement_analysis"),
            "acceptance_criteria": planning_result.get("acceptance_criteria") or state.get("acceptance_criteria", []),
            "tasks": planning_result.get("tasks") or state.get("implementation_tasks", []),
        }
        try:
            plan = PlanningOutput.model_validate(raw_plan)
        except Exception:
            return {}
        quality_state = dict(state)
        quality_state.update(updates)
        quality = score_plan_quality(plan, quality_state)  # type: ignore[arg-type]
        return {
            "planning_quality_score": quality.score,
            "planning_quality_level": quality.level,
            "planning_quality_dimensions": quality.dimensions,
            "planning_quality_issues": quality.issues,
            "planning_decision_confidence": quality.decision_confidence,
            "planning_quality_version": quality.version,
        }

    async def planning_risk_policy_node(
        state: SoftwareFactoryState,
        _dependencies: GraphDependencies,
    ) -> dict[str, Any]:
        if not state.get("planning_valid"):
            return {}
        fingerprint = planning_approval_fingerprint(state)
        decision = evaluate_planning_approval_policy(
            state.get("planning_risk_level"),
            state.get("planning_risk_score"),
            state.get("planning_impact_areas", []),
            state.get("planning_sensitive_tasks", []),
        )
        approval_id = f"planning-risk-{fingerprint[:16]}"
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="planning_policy",
            stage="planning",
            status=EventStatus.COMPLETED,
            data={
                "event": "planning_policy_evaluated",
                "risk_level": state.get("planning_risk_level"),
                "risk_score": state.get("planning_risk_score"),
                "policy_decision": decision.decision,
                "impact_area_count": len(state.get("planning_impact_areas", [])),
                "sensitive_task_count": len(state.get("planning_sensitive_tasks", [])),
                "approval_id": approval_id if decision.required else None,
            },
        )
        if not decision.required:
            updates: dict[str, Any] = {
                "planning_approval_required": False,
                "planning_approval_reason": decision.reason,
                "planning_approval_status": "not_required",
                "planning_approval_id": None,
                "planning_policy_decision": "allow",
                "planning_approval_fingerprint": fingerprint,
                "terminal_status": None if state.get("terminal_status") == "pending" else state.get("terminal_status"),
            }
            updates.update(_planning_quality_updates(state, updates))
            updates["planning_result"] = _planning_result_with_approval(state, updates)
            return updates
        if (
            state.get("planning_approval_status") == "approved"
            and state.get("planning_approval_fingerprint") == fingerprint
        ):
            updates = {
                "planning_approval_required": True,
                "planning_approval_reason": decision.reason,
                "planning_approval_status": "approved",
                "planning_approval_id": state.get("planning_approval_id") or approval_id,
                "planning_policy_decision": "require_approval",
                "planning_approval_fingerprint": fingerprint,
                "terminal_status": None,
            }
            updates.update(_planning_quality_updates(state, updates))
            updates["planning_result"] = _planning_result_with_approval(state, updates)
            return updates
        preview = {
            "project_name": state.get("project_name") or state.get("created_project_name"),
            "risk_level": state.get("planning_risk_level"),
            "risk_score": state.get("planning_risk_score"),
            "risk_reasons": state.get("planning_risk_reasons", []),
            "impact_areas": state.get("planning_impact_areas", []),
            "sensitive_tasks": state.get("planning_sensitive_tasks", []),
            "task_count": len(state.get("implementation_tasks", [])),
            "approval_id": approval_id,
            "fingerprint": fingerprint,
        }
        updates = {
            "planning_approval_required": True,
            "planning_approval_reason": decision.reason,
            "planning_approval_status": "awaiting_approval",
            "planning_approval_id": approval_id,
            "planning_policy_decision": "require_approval",
            "planning_approval_fingerprint": fingerprint,
            "pending_operation": "planning_risk_approval",
            "pending_tool_name": "planning__approve_risk",
            "pending_tool_arguments": {"approval_id": approval_id, "fingerprint": fingerprint},
            "pending_approval_preview": preview,
            "pending_approval_status": "waiting",
            "approval_reason": None,
            "terminal_status": "pending",
        }
        updates.update(_planning_quality_updates(state, updates))
        updates["planning_result"] = _planning_result_with_approval(state, updates)
        emit_workflow_event(
            WorkflowEventType.APPROVAL_REQUIRED,
            source="planning_policy",
            stage="planning_risk_approval",
            status=EventStatus.WAITING,
            data={
                "event": "planning_approval_required",
                "risk_level": state.get("planning_risk_level"),
                "risk_score": state.get("planning_risk_score"),
                "policy_decision": "require_approval",
                "impact_area_count": len(state.get("planning_impact_areas", [])),
                "sensitive_task_count": len(state.get("planning_sensitive_tasks", [])),
                "approval_id": approval_id,
            },
        )
        return updates

    async def accept_planning_risk_node(
        state: SoftwareFactoryState,
        _dependencies: GraphDependencies,
    ) -> dict[str, Any]:
        expected = (state.get("pending_tool_arguments") or {}).get("fingerprint")
        current = planning_approval_fingerprint(state)
        if expected != current:
            updates = {
                "planning_approval_status": "awaiting_approval",
                "failure_type": "planning_approval_stale",
                "failure_stage": "planning_risk_approval",
                "failure_message": "Planning risk approval is stale for the current plan.",
                "pending_approval_status": "waiting",
            }
            emit_workflow_event(
                WorkflowEventType.STAGE_FAILED,
                source="planning_policy",
                stage="planning_risk_approval",
                status=EventStatus.FAILED,
                data={"event": "planning_approval_stale", "approval_id": state.get("planning_approval_id")},
            )
            updates["planning_result"] = _planning_result_with_approval(state, updates)
            return updates
        updates = {
            "planning_approval_status": "approved",
            "planning_approval_required": True,
            "planning_policy_decision": "require_approval",
            "pending_operation": None,
            "pending_tool_name": None,
            "pending_tool_arguments": None,
            "pending_approval_preview": None,
            "pending_approval_status": "none",
            "terminal_status": None,
        }
        updates.update(_planning_quality_updates(state, updates))
        updates["planning_result"] = _planning_result_with_approval(state, updates)
        emit_workflow_event(
            WorkflowEventType.APPROVAL_GRANTED,
            source="planning_policy",
            stage="planning_risk_approval",
            status=EventStatus.COMPLETED,
            data={"event": "planning_approval_granted", "approval_id": state.get("planning_approval_id")},
        )
        return updates

    async def reject_planning_risk_node(
        state: SoftwareFactoryState,
        _dependencies: GraphDependencies,
    ) -> dict[str, Any]:
        updates = {
            "planning_approval_status": "rejected",
            "planning_approval_required": True,
            "planning_policy_decision": "require_approval",
            "failure_type": "user_rejected",
            "failure_stage": "planning_risk_approval",
            "failure_message": state.get("approval_reason") or "El usuario rechazo el riesgo del plan.",
            "terminal_status": "user_cancelled",
            "user_cancelled": True,
        }
        updates.update(_planning_quality_updates(state, updates))
        updates["planning_result"] = _planning_result_with_approval(state, updates)
        emit_workflow_event(
            WorkflowEventType.APPROVAL_REJECTED,
            source="planning_policy",
            stage="planning_risk_approval",
            status=EventStatus.FAILED,
            data={"event": "planning_approval_rejected", "approval_id": state.get("planning_approval_id")},
        )
        return updates

    nodes: dict[str, Node] = {
        "detect_intent": detect_intent_node,
        "inspect_workspace": inspect_workspace_node,
        "testing_repair_precondition_failure": _testing_repair_precondition_failure,
        "finalize": finalize_node,
        "planner_evaluation": planner_evaluation_node,
        "agent_performance_evaluation": agent_performance_evaluation_node,
        "failure_attribution": failure_attribution_node,
        "planning_judge": planner_judge_node,
        "planning_hybrid_evaluation": planner_hybrid_evaluation_node,
        "extract_workflow_learnings": extract_workflow_learnings_node,
        "submit_workflow_learnings": submit_workflow_learnings_node,
        "planning_risk_policy": planning_risk_policy_node,
        "planning_risk_approval": approval_node,
        "accept_planning_risk": accept_planning_risk_node,
        "reject_planning_risk": reject_planning_risk_node,
        "git_approval": approval_node,
        "git_workflow": prepare_git_workflow_node,
        "ci_pipeline": run_ci_pipeline,
        "execute_git_commit": execute_git_commit_node,
        "reject_git_commit": reject_git_commit_node,
        "git_promotion_approval": approval_node,
        "git_promotion": prepare_git_promotion_node,
        "execute_git_promotion": execute_git_promotion_node,
        "reject_git_promotion": reject_git_promotion_node,
    }
    for name, node in nodes.items():
        builder.add_node(name, _logged_node(name, node, dependencies))
    builder.add_node("testing_repair", run_testing_subgraph)
    builder.add_node("planning", run_planning_subgraph)
    builder.add_node("implementation", run_implementation_subgraph)
    builder.add_node("supervisor", _logged_node("supervisor", run_supervisor, dependencies))
    builder.add_node("supervisor_loop_probe", run_supervisor_loop_probe)

    builder.add_edge(START, "detect_intent")
    builder.add_edge("detect_intent", "supervisor")
    builder.add_edge("planning", "planning_risk_policy")
    builder.add_conditional_edges(
        "planning_risk_policy",
        lambda state: (
            "approval"
            if state.get("pending_operation") == "planning_risk_approval" and state.get("pending_approval_status") == "waiting"
            else "judge"
            if state.get("planning_valid")
            else "supervisor"
        ),
        {"approval": "planning_risk_approval", "judge": "planning_judge", "supervisor": "supervisor"},
    )
    builder.add_conditional_edges(
        "planning_risk_approval",
        lambda state: "approved" if state.get("pending_approval_status") == "approved" else "rejected",
        {"approved": "accept_planning_risk", "rejected": "reject_planning_risk"},
    )
    builder.add_edge("accept_planning_risk", "planning_judge")
    builder.add_edge("planning_judge", "planning_hybrid_evaluation")
    builder.add_edge("planning_hybrid_evaluation", "supervisor")
    builder.add_edge("reject_planning_risk", "finalize")
    builder.add_edge("inspect_workspace", "supervisor")
    builder.add_edge("implementation", "supervisor")
    builder.add_edge("testing_repair", "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        _logged_router("supervisor", route_after_supervisor),
        {
            "supervisor_loop_probe": "supervisor_loop_probe",
            "planning": "planning",
            "inspect_workspace": "inspect_workspace",
            "implementation": "implementation",
            "testing_repair": "testing_repair",
            "git_workflow": "git_workflow",
            "ci_pipeline": "ci_pipeline",
            "git_promotion": "git_promotion",
            "finalize": "finalize",
        },
    )
    builder.add_conditional_edges(
        "git_workflow",
        lambda state: "approval" if state.get("pending_operation") == "git_commit" and state.get("pending_approval_status") == "waiting" else "supervisor",
        {"approval": "git_approval", "supervisor": "supervisor"},
    )
    builder.add_conditional_edges(
        "git_approval",
        lambda state: "execute" if state.get("pending_approval_status") == "approved" else "reject",
        {"execute": "execute_git_commit", "reject": "reject_git_commit"},
    )
    builder.add_edge("execute_git_commit", "supervisor")
    builder.add_edge("ci_pipeline", "supervisor")
    builder.add_edge("reject_git_commit", END)
    builder.add_conditional_edges(
        "git_promotion",
        lambda state: "approval" if state.get("pending_operation") == "git_merge" and state.get("pending_approval_status") == "waiting" else "supervisor",
        {"approval": "git_promotion_approval", "supervisor": "supervisor"},
    )
    builder.add_conditional_edges(
        "git_promotion_approval",
        lambda state: "execute" if state.get("pending_approval_status") == "approved" else "reject",
        {"execute": "execute_git_promotion", "reject": "reject_git_promotion"},
    )
    builder.add_edge("execute_git_promotion", "supervisor")
    builder.add_edge("reject_git_promotion", "supervisor")
    builder.add_edge("supervisor_loop_probe", "supervisor")
    builder.add_edge("testing_repair_precondition_failure", "finalize")
    builder.add_edge("finalize", "planner_evaluation")
    builder.add_edge("planner_evaluation", "agent_performance_evaluation")
    builder.add_edge("agent_performance_evaluation", "failure_attribution")
    builder.add_edge("failure_attribution", "extract_workflow_learnings")
    builder.add_edge("extract_workflow_learnings", "submit_workflow_learnings")
    builder.add_edge("submit_workflow_learnings", END)
    return builder.compile(checkpointer=checkpointer)
