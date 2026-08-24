from __future__ import annotations

from typing import Any, TypedDict


class SupervisorState(TypedDict, total=False):
    original_user_message: str
    project_name: str
    workflow_intent: str
    planning_summary: dict[str, Any]
    implementation_summary: dict[str, Any]
    testing_summary: dict[str, Any]
    current_stage: str
    last_completed_stage: str | None
    allowed_handoffs: list[str]
    supervisor_decision: str | None
    supervisor_reason: str | None
    supervisor_confidence: float | None
    supervisor_decision_source: str | None
    supervisor_attempts: int
    max_supervisor_attempts: int
    supervisor_errors: list[str]
    supervisor_invalid_decision_count: int
    consecutive_invalid_decisions: int
    supervisor_stagnant_loop_probe_count: int
    supervisor_stagnant_loop_fingerprint: dict[str, Any] | None
    supervisor_stagnant_loop_active: bool
    handoff_history: list[dict[str, Any]]


SUPERVISOR_STATE_FIELDS = frozenset(SupervisorState.__annotations__)
SUPERVISOR_OUTPUT_KEYS = frozenset(
    {
        "supervisor_decision",
        "supervisor_reason",
        "supervisor_confidence",
        "supervisor_decision_source",
        "supervisor_attempts",
        "allowed_handoffs",
        "handoff_history",
        "supervisor_errors",
        "supervisor_invalid_decision_count",
        "consecutive_invalid_decisions",
        "supervisor_stagnant_loop_probe_count",
        "supervisor_stagnant_loop_fingerprint",
        "supervisor_stagnant_loop_active",
    }
)
