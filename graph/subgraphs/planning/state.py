from __future__ import annotations

from typing import Any, TypedDict

from graph.state import PlannerKnowledgeState, TerminalStatus, WorkflowIntent


class PlanningState(TypedDict, total=False):
    original_user_message: str
    project_name: str | None
    created_project_name: str | None
    detected_test_framework: str | None
    workflow_intent: WorkflowIntent
    requirement_analysis: dict[str, Any] | None
    acceptance_criteria: list[dict[str, Any]]
    implementation_tasks: list[dict[str, Any]]
    planning_valid: bool
    planning_errors: list[str]
    planning_attempts: int
    max_planning_attempts: int
    planning_failure_reason: str | None
    planning_execution_order: list[int]
    planning_dependency_edges: list[dict[str, int]]
    planning_risk_score: int | None
    planning_risk_level: str | None
    planning_risk_reasons: list[str]
    planning_sensitive_tasks: list[dict[str, Any]]
    planning_impact_areas: list[str]
    planning_approval_required: bool
    planning_approval_reason: str | None
    planning_approval_status: str | None
    planning_approval_id: str | None
    planning_policy_decision: str | None
    planning_approval_fingerprint: str | None
    planning_quality_score: int | None
    planning_quality_level: str | None
    planning_quality_dimensions: dict[str, int]
    planning_quality_issues: list[str]
    planning_decision_confidence: float | None
    planning_quality_version: str | None
    planning_quality_gate_decision: str | None
    planning_quality_gate_reason: str | None
    planning_quality_refinement_required: bool
    planning_quality_refinement_attempts: int
    planning_quality_previous_score: int | None
    planning_quality_score_delta: int | None
    planning_quality_refinement_guidance: list[str]
    planner_knowledge_query: str | None
    planner_knowledge_retrieval_id: str | None
    planner_knowledge_context: str | None
    planner_knowledge_sources: list[dict[str, Any]]
    planner_knowledge_state: PlannerKnowledgeState
    planner_knowledge_retrieval_used: bool
    planner_retrieved_context_count: int
    planner_knowledge_context_tokens: int
    terminal_status: TerminalStatus | None
    failure_type: str | None
    failure_stage: str | None
    failure_message: str | None


PLANNING_STATE_FIELDS = frozenset(PlanningState.__annotations__)

PLANNING_OUTPUT_KEYS = frozenset(
    {
        "requirement_analysis",
        "acceptance_criteria",
        "implementation_tasks",
        "planning_valid",
        "planning_errors",
        "planning_attempts",
        "planning_failure_reason",
        "planning_execution_order",
        "planning_dependency_edges",
        "planning_risk_score",
        "planning_risk_level",
        "planning_risk_reasons",
        "planning_sensitive_tasks",
        "planning_impact_areas",
        "planning_approval_required",
        "planning_approval_reason",
        "planning_approval_status",
        "planning_approval_id",
        "planning_policy_decision",
        "planning_approval_fingerprint",
        "planning_quality_score",
        "planning_quality_level",
        "planning_quality_dimensions",
        "planning_quality_issues",
        "planning_decision_confidence",
        "planning_quality_version",
        "planning_quality_gate_decision",
        "planning_quality_gate_reason",
        "planning_quality_refinement_required",
        "planning_quality_refinement_attempts",
        "planning_quality_previous_score",
        "planning_quality_score_delta",
        "planning_quality_refinement_guidance",
        "planner_knowledge_query",
        "planner_knowledge_retrieval_id",
        "planner_knowledge_context",
        "planner_knowledge_sources",
        "planner_knowledge_state",
        "planner_knowledge_retrieval_used",
        "planner_retrieved_context_count",
        "planner_knowledge_context_tokens",
        "terminal_status",
        "failure_type",
        "failure_stage",
        "failure_message",
    }
)
