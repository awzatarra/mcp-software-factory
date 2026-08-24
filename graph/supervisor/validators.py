from __future__ import annotations

from graph.state import SoftwareFactoryState
from graph.supervisor.guards import approval_blocks_supervisor
from graph.supervisor.models import SupervisorDecision, SupervisorTarget


def validate_supervisor_decision(
    decision: SupervisorDecision,
    allowed_handoffs: list[SupervisorTarget],
    state: SoftwareFactoryState | None = None,
) -> list[str]:
    errors: list[str] = []
    if decision.target not in allowed_handoffs:
        errors.append("target_not_allowed")
    if not decision.reason.strip():
        errors.append("supervisor_reason_missing")
    if not 0.0 <= decision.confidence <= 1.0:
        errors.append("supervisor_confidence_invalid")
    if state is None:
        return errors
    if state.get("terminal_status") is not None and decision.target != SupervisorTarget.FINALIZE:
        errors.append("supervisor_terminal_state_conflict")
    if decision.target == SupervisorTarget.TESTING_REPAIR and not state.get("environment_prepared"):
        errors.append("supervisor_testing_environment_missing")
    if (
        decision.target == SupervisorTarget.IMPLEMENTATION
        and state.get("workflow_intent") != "review_existing_project"
        and not state.get("planning_valid")
    ):
        errors.append("supervisor_implementation_without_plan")
    if (
        decision.target == SupervisorTarget.PLANNING
        and state.get("workflow_intent") == "review_existing_project"
    ):
        errors.append("supervisor_planning_not_allowed_for_review")
    if approval_blocks_supervisor(state):
        errors.append("supervisor_approval_pending")
    return list(dict.fromkeys(errors))
