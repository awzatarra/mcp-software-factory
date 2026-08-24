from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from graph.state import SoftwareFactoryState
from graph.supervisor.state import SUPERVISOR_OUTPUT_KEYS, SupervisorState


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: Any) -> int:
    return len(value) if isinstance(value, (list, tuple, dict, set)) else 0


def _current_stage(state: SoftwareFactoryState) -> str:
    stage = state.get("last_completed_stage")
    if stage:
        return str(stage)
    last = str(state.get("last_completed_node") or "")
    if last in {"planning", "inspect_workspace", "implementation", "testing_repair", "finalize"}:
        return last
    if state.get("tests_executed"):
        return "testing_repair"
    if state.get("environment_prepared") or state.get("implementation_valid"):
        return "implementation"
    if state.get("workspace_inspected"):
        return "inspect_workspace"
    if state.get("planning_valid") or state.get("planning_attempts", 0):
        return "planning"
    return "intent"


def to_supervisor_input(parent_state: SoftwareFactoryState) -> SupervisorState:
    planning = _mapping(parent_state.get("planning_result"))
    planning_analysis = _mapping(planning.get("analysis"))
    legacy_analysis = _mapping(parent_state.get("requirement_analysis"))
    functional = planning_analysis.get(
        "functional_requirements",
        legacy_analysis.get("functional_requirements", []),
    )
    acceptance = planning.get("acceptance_criteria", parent_state.get("acceptance_criteria", []))
    tasks = planning.get("tasks", parent_state.get("implementation_tasks", []))

    implementation = _mapping(parent_state.get("implementation_result"))
    testing = _mapping(parent_state.get("testing_result"))
    return SupervisorState(
        original_user_message=str(parent_state.get("original_user_message") or ""),
        project_name=str(parent_state.get("created_project_name") or parent_state.get("project_name") or ""),
        workflow_intent=str(parent_state.get("workflow_intent") or ""),
        planning_summary={
            "valid": planning.get("valid", parent_state.get("planning_valid", False)),
            "attempts": planning.get("attempts", parent_state.get("planning_attempts", 0)),
            "project_type": planning_analysis.get("project_type", legacy_analysis.get("project_type")),
            "functional_requirement_count": _count(functional),
            "acceptance_criteria_count": _count(acceptance),
            "task_count": _count(tasks),
            "failure": parent_state.get("planning_failure_reason")
            or (
                parent_state.get("failure_message")
                if parent_state.get("failure_stage") == "planning"
                else None
            ),
        },
        implementation_summary={
            "valid": implementation.get("valid", parent_state.get("implementation_valid", False)),
            "attempts": implementation.get("attempts", parent_state.get("implementation_attempts", 0)),
            "project_created": implementation.get(
                "project_created",
                parent_state.get("project_created", False),
            ),
            "project_exists": parent_state.get("project_exists", False),
            "environment_prepared": implementation.get(
                "environment_prepared",
                parent_state.get("environment_prepared", False),
            ),
            "dependencies_installed": parent_state.get("dependencies_installed", False),
            "framework": implementation.get(
                "framework",
                parent_state.get("detected_test_framework"),
            ),
            "failure": parent_state.get("implementation_failure_reason")
            or (
                parent_state.get("failure_message")
                if parent_state.get("failure_stage") == "implementation"
                else None
            ),
        },
        testing_summary={
            "executed": testing.get("tests_executed", parent_state.get("tests_executed", False)),
            "passed": testing.get("tests_passed", parent_state.get("tests_passed", False)),
            "summary": testing.get("summary", parent_state.get("final_test_result_summary")),
            "repair_phase": testing.get("repair_phase", parent_state.get("repair_phase", "not_started")),
            "repair_attempts": testing.get("repair_attempts", parent_state.get("repair_attempts", 0)),
            "failure": parent_state.get("test_failure_summary")
            or (
                parent_state.get("failure_message")
                if parent_state.get("failure_stage") in {"testing", "testing_repair"}
                else None
            ),
        },
        current_stage=_current_stage(parent_state),
        last_completed_stage=parent_state.get("last_completed_stage")
        or parent_state.get("last_completed_node"),
        allowed_handoffs=list(parent_state.get("allowed_handoffs") or []),
        supervisor_decision=parent_state.get("supervisor_decision"),
        supervisor_reason=parent_state.get("supervisor_reason"),
        supervisor_confidence=parent_state.get("supervisor_confidence"),
        supervisor_decision_source=parent_state.get("supervisor_decision_source"),
        supervisor_attempts=int(parent_state.get("supervisor_attempts", 0)),
        max_supervisor_attempts=int(parent_state.get("max_supervisor_attempts", 3)),
        supervisor_errors=list(parent_state.get("supervisor_errors") or []),
        supervisor_invalid_decision_count=int(
            parent_state.get("supervisor_invalid_decision_count", 0)
        ),
        consecutive_invalid_decisions=int(
            parent_state.get("consecutive_invalid_decisions", 0)
        ),
        supervisor_stagnant_loop_probe_count=int(
            parent_state.get("supervisor_stagnant_loop_probe_count", 0)
        ),
        supervisor_stagnant_loop_fingerprint=deepcopy(
            parent_state.get("supervisor_stagnant_loop_fingerprint")
        ),
        supervisor_stagnant_loop_active=bool(
            parent_state.get("supervisor_stagnant_loop_active", False)
        ),
        handoff_history=deepcopy(parent_state.get("handoff_history") or []),
    )


def from_supervisor_output(result: SupervisorState) -> dict[str, Any]:
    return {
        key: deepcopy(result[key])
        for key in SUPERVISOR_OUTPUT_KEYS
        if key in result
    }
