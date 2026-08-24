from __future__ import annotations

from typing import Any, Literal, TypedDict


WorkflowIntent = Literal["create_project", "review_existing_project"]
RepairPhase = Literal[
    "not_started",
    "read_failing_test",
    "read_related_source",
    "apply_fix",
    "rerun_tests",
    "completed",
]
DeveloperKnowledgeState = Literal["not_started", "available", "empty", "unavailable"]
PlannerKnowledgeState = Literal["not_started", "available", "empty", "unavailable"]
QAKnowledgeState = Literal["not_started", "available", "empty", "unavailable"]
RepairKnowledgeState = Literal["not_started", "available", "empty", "unavailable"]
WorkflowLearningState = Literal["not_started", "extracted", "submitted", "partial", "unavailable"]
TerminalStatus = Literal[
    "pending",
    "completed",
    "infrastructure_failed",
    "tests_failed",
    "repair_limit_reached",
    "user_cancelled",
    "planning_failed",
    "implementation_failed",
    "ci_failed",
    "supervisor_loop_detected",
]
PendingOperation = Literal["create_project", "prepare_environment", "run_tests", "apply_fix", "git_commit", "planning_risk_approval", "git_merge"]
PendingApprovalStatus = Literal["none", "waiting", "approved", "rejected"]

# Transitional compatibility surface. New integrations should consume the
# grouped planning_result, implementation_result, and testing_result fields.
DEPRECATED_FLAT_RESULT_FIELDS = frozenset(
    {
        "requirement_analysis",
        "acceptance_criteria",
        "implementation_tasks",
        "planning_valid",
        "planning_attempts",
        "generated_package_name",
        "generated_files",
        "project_created",
        "environment_prepared",
        "detected_test_framework",
        "tests_passed",
        "final_test_result_summary",
        "repair_phase",
        "repair_attempts",
    }
)


class SoftwareFactoryState(TypedDict, total=False):
    workflow_id: str
    original_user_message: str
    workflow_intent: WorkflowIntent
    project_name: str | None
    planning_result: dict[str, Any]
    implementation_result: dict[str, Any]
    testing_result: dict[str, Any]
    analysis_completed: bool
    tasks_created: bool
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
    planning_approval_status: Literal["not_required", "awaiting_approval", "approved", "rejected"] | None
    planning_approval_id: str | None
    planning_policy_decision: Literal["allow", "require_approval"] | None
    planning_approval_fingerprint: str | None
    planning_quality_score: int | None
    planning_quality_level: str | None
    planning_quality_dimensions: dict[str, int]
    planning_quality_issues: list[str]
    planning_decision_confidence: float | None
    planning_quality_version: str | None
    planning_quality_gate_decision: Literal["continue", "refine", "fail"] | None
    planning_quality_gate_reason: str | None
    planning_quality_refinement_required: bool
    planning_quality_refinement_attempts: int
    planning_quality_previous_score: int | None
    planning_quality_score_delta: int | None
    planning_quality_refinement_guidance: list[str]
    planning_evaluation: dict[str, Any]
    planning_evaluation_version: str | None
    planning_evaluation_outcome: str | None
    planning_outcome_score: int | None
    planning_quality_prediction_error: float | None
    planning_quality_prediction_absolute_error: float | None
    planning_confidence_error: float | None
    planning_confidence_absolute_error: float | None
    planning_confidence_calibration: str | None
    planning_risk_observed_severity: str | None
    planning_risk_calibration: str | None
    planning_refinement_effective: bool | None
    planning_quality_gate_false_positive: bool
    planning_quality_gate_false_negative: bool
    planning_judge_status: Literal["disabled", "completed", "unavailable"] | None
    planning_judge_result: dict[str, Any] | None
    planning_judge_model: str | None
    planning_judge_version: str | None
    planning_judge_evaluated_at: str | None
    planning_judge_plan_fingerprint: str | None
    planning_judge_disagreement: dict[str, Any] | None
    planning_hybrid_evaluation: dict[str, Any]
    planning_hybrid_evaluation_version: str | None
    planning_judge_calibration_status: str | None
    planning_judge_calibration: dict[str, Any] | None
    planning_judge_calibration_version: str | None
    planning_judge_score: float | None
    planning_judge_score_error: float | None
    planning_judge_score_absolute_error: float | None
    planning_judge_confidence: float | None
    planning_judge_confidence_error: float | None
    planning_judge_confidence_absolute_error: float | None
    planning_hybrid_calibration_status: str | None
    planning_hybrid_calibration: dict[str, Any] | None
    planning_hybrid_prediction_error: float | None
    planning_hybrid_absolute_error: float | None
    planning_deterministic_prediction_error: float | None
    planning_deterministic_absolute_error: float | None
    agent_performance_evaluations: dict[str, Any]
    agent_performance_version: str | None
    failure_attribution: dict[str, Any]
    failure_attribution_version: str | None
    planner_knowledge_query: str | None
    planner_knowledge_retrieval_id: str | None
    planner_knowledge_context: str | None
    planner_knowledge_sources: list[dict[str, Any]]
    planner_knowledge_state: PlannerKnowledgeState
    planner_knowledge_retrieval_used: bool
    planner_retrieved_context_count: int
    planner_knowledge_context_tokens: int
    implementation_valid: bool
    implementation_errors: list[str]
    resolved_validation_errors: list[str]
    remaining_validation_errors: list[str]
    refinement_input_errors: list[str]
    implementation_attempts: int
    max_implementation_attempts: int
    generated_package_name: str | None
    generated_files: list[str]
    collectable_test_count: int
    test_functions: list[str]
    implementation_failure_reason: str | None
    project_implementation: dict[str, Any] | None
    developer_knowledge_query: str | None
    developer_knowledge_retrieval_id: str | None
    developer_knowledge_context: str | None
    developer_knowledge_sources: list[dict[str, Any]]
    developer_knowledge_state: DeveloperKnowledgeState
    knowledge_context_state: DeveloperKnowledgeState
    knowledge_retrieval_used: bool
    retrieved_context_count: int
    knowledge_context_tokens: int
    retrieval_id: str | None
    dependency_policy_applied: bool
    normalized_dependency_file: str | None
    dependency_policy_errors: list[str]
    dependency_normalization_attempts: int
    workspace_inspected: bool
    project_created: bool
    project_exists: bool
    created_project_name: str | None
    detected_test_framework: str | None
    expected_test_command: list[str] | None
    environment_prepared: bool
    dependencies_installed: bool
    environment_python: str | None
    installed_fastapi_version: str | None
    installed_starlette_version: str | None
    tests_executed: bool
    tests_passed: bool
    qa_knowledge_query: str | None
    qa_knowledge_retrieval_id: str | None
    qa_knowledge_context: str | None
    qa_knowledge_sources: list[dict[str, Any]]
    qa_knowledge_state: QAKnowledgeState
    qa_knowledge_retrieval_used: bool
    qa_retrieved_context_count: int
    qa_knowledge_context_tokens: int
    repair_knowledge_query: str | None
    repair_knowledge_retrieval_id: str | None
    repair_knowledge_context: str | None
    repair_knowledge_sources: list[dict[str, Any]]
    repair_knowledge_state: RepairKnowledgeState
    repair_knowledge_retrieval_used: bool
    repair_retrieved_context_count: int
    repair_knowledge_context_tokens: int
    test_infrastructure_failed: bool
    retry_limit_reached: bool
    user_cancelled: bool
    terminal_status: TerminalStatus | None
    test_warning_count: int | None
    actual_test_command: list[str] | None
    failure_type: str | None
    failure_stage: str | None
    failure_message: str | None
    test_stdout: str | None
    test_stderr: str | None
    test_failure_summary: str | None
    repair_phase: RepairPhase
    repair_attempts: int
    repair_decision: str | None
    repair_before: str | None
    repair_after: str | None
    failing_test_files: list[str]
    files_read_during_repair: list[str]
    files_updated_during_repair: list[str]
    first_test_result_summary: str | None
    final_test_result_summary: str | None
    failing_test_content: str | None
    related_source_content: str | None
    related_source_file: str | None
    related_source_candidates: list[str]
    related_source_read_attempts: int
    pending_operation: PendingOperation | None
    pending_tool_name: str | None
    pending_tool_arguments: dict[str, Any] | None
    pending_approval_preview: dict[str, Any] | None
    pending_approval_status: PendingApprovalStatus
    approval_reason: str | None
    last_approved_tool: str | None
    last_rejected_tool: str | None
    fork_origin_checkpoint_id: str | None
    fork_reason: str | None
    fork_updated_fields: list[str]
    fork_lineage: Literal["fork"] | None
    supervisor_decision: str | None
    supervisor_reason: str | None
    supervisor_confidence: float | None
    supervisor_decision_source: str | None
    supervisor_attempts: int
    max_supervisor_attempts: int
    allowed_handoffs: list[str]
    handoff_history: list[dict[str, Any]]
    supervisor_errors: list[str]
    supervisor_invalid_decision_count: int
    consecutive_invalid_decisions: int
    supervisor_stagnant_loop_probe_count: int
    supervisor_stagnant_loop_fingerprint: dict[str, Any] | None
    supervisor_stagnant_loop_active: bool
    last_completed_stage: str | None
    last_completed_node: str | None
    workflow_learning_candidates: list[dict[str, Any]]
    workflow_learning_submission_results: list[dict[str, Any]]
    workflow_learning_state: WorkflowLearningState
    final_response: str
    git_state: Literal[
        "not_initialized", "ready", "changes_staged", "awaiting_approval",
        "committed", "rejected", "unavailable",
    ]
    git_repository: bool
    git_base_branch: str | None
    git_base_commit: str | None
    git_branch: str | None
    git_head_commit: str | None
    git_staged_files: list[str]
    git_commit_preview: dict[str, Any] | None
    git_commit_approval_id: str | None
    git_commit_status: str | None
    git_workflow_state: str
    git_workflow_branch: str | None
    git_developer_commit_sha: str | None
    git_repair_commit_sha: str | None
    git_commit_history: list[dict[str, Any]]
    git_commit_phase: Literal["implementation", "repair"] | None
    git_integration_enabled: bool
    git_promotion_state: str
    git_promotion_id: str | None
    git_promotion_preview: dict[str, Any] | None
    git_promotion_approval_id: str | None
    git_promotion_result_commit: str | None
    git_promotion_strategy: str | None
    git_promotion_conflicts: list[str]
    git_auto_prepare_promotion: bool
    git_promotion_required: bool
    ci_state: str
    ci_run_id: str | None
    ci_status: str | None
    ci_decision: str | None
    ci_validated_commit: str | None
    ci_failure_type: str | None
    ci_failure_message: str | None
    ci_gate_summary: dict[str, Any]
    ci_promotion_eligible: bool | None
    ci_promotion_eligibility: dict[str, Any] | None
    ci_promotion_required: bool
    ci_promotion_allow_warnings: bool
    ci_repair_state: str
    ci_repair_attempts: int
    ci_repair_failure_type: str | None
    ci_repair_failure_message: str | None
    ci_repair_source_run_id: str | None
    ci_repair_source_commit: str | None
    ci_repair_target_commit: str | None
    ci_repair_repairability: dict[str, Any] | None
    ci_repair_lineage: list[dict[str, Any]]
    ci_repair_enabled: bool
    ci_repair_max_attempts: int


def create_initial_state(user_message: str) -> SoftwareFactoryState:
    return {
        "original_user_message": user_message,
        "workflow_intent": "create_project",
        "project_name": None,
        "planning_result": {},
        "implementation_result": {},
        "testing_result": {},
        "analysis_completed": False,
        "tasks_created": False,
        "requirement_analysis": None,
        "acceptance_criteria": [],
        "implementation_tasks": [],
        "planning_valid": False,
        "planning_errors": [],
        "planning_attempts": 0,
        "max_planning_attempts": 2,
        "planning_failure_reason": None,
        "planning_execution_order": [],
        "planning_dependency_edges": [],
        "planning_risk_score": None,
        "planning_risk_level": None,
        "planning_risk_reasons": [],
        "planning_sensitive_tasks": [],
        "planning_impact_areas": [],
        "planning_approval_required": False,
        "planning_approval_reason": None,
        "planning_approval_status": "not_required",
        "planning_approval_id": None,
        "planning_policy_decision": None,
        "planning_approval_fingerprint": None,
        "planning_quality_score": None,
        "planning_quality_level": None,
        "planning_quality_dimensions": {},
        "planning_quality_issues": [],
        "planning_decision_confidence": None,
        "planning_quality_version": None,
        "planning_quality_gate_decision": None,
        "planning_quality_gate_reason": None,
        "planning_quality_refinement_required": False,
        "planning_quality_refinement_attempts": 0,
        "planning_quality_previous_score": None,
        "planning_quality_score_delta": None,
        "planning_quality_refinement_guidance": [],
        "planning_evaluation": {},
        "planning_evaluation_version": None,
        "planning_evaluation_outcome": None,
        "planning_outcome_score": None,
        "planning_quality_prediction_error": None,
        "planning_quality_prediction_absolute_error": None,
        "planning_confidence_error": None,
        "planning_confidence_absolute_error": None,
        "planning_confidence_calibration": None,
        "planning_risk_observed_severity": None,
        "planning_risk_calibration": None,
        "planning_refinement_effective": None,
        "planning_quality_gate_false_positive": False,
        "planning_quality_gate_false_negative": False,
        "planning_judge_status": None,
        "planning_judge_result": None,
        "planning_judge_model": None,
        "planning_judge_version": None,
        "planning_judge_evaluated_at": None,
        "planning_judge_plan_fingerprint": None,
        "planning_judge_disagreement": None,
        "planning_hybrid_evaluation": {},
        "planning_hybrid_evaluation_version": None,
        "planning_judge_calibration_status": None,
        "planning_judge_calibration": None,
        "planning_judge_calibration_version": None,
        "planning_judge_score": None,
        "planning_judge_score_error": None,
        "planning_judge_score_absolute_error": None,
        "planning_judge_confidence": None,
        "planning_judge_confidence_error": None,
        "planning_judge_confidence_absolute_error": None,
        "planning_hybrid_calibration_status": None,
        "planning_hybrid_calibration": None,
        "planning_hybrid_prediction_error": None,
        "planning_hybrid_absolute_error": None,
        "planning_deterministic_prediction_error": None,
        "planning_deterministic_absolute_error": None,
        "agent_performance_evaluations": {},
        "agent_performance_version": None,
        "failure_attribution": {},
        "failure_attribution_version": None,
        "planner_knowledge_query": None,
        "planner_knowledge_retrieval_id": None,
        "planner_knowledge_context": None,
        "planner_knowledge_sources": [],
        "planner_knowledge_state": "not_started",
        "planner_knowledge_retrieval_used": False,
        "planner_retrieved_context_count": 0,
        "planner_knowledge_context_tokens": 0,
        "implementation_valid": False,
        "implementation_errors": [],
        "resolved_validation_errors": [],
        "remaining_validation_errors": [],
        "refinement_input_errors": [],
        "implementation_attempts": 0,
        "max_implementation_attempts": 2,
        "generated_package_name": None,
        "generated_files": [],
        "collectable_test_count": 0,
        "test_functions": [],
        "implementation_failure_reason": None,
        "project_implementation": None,
        "developer_knowledge_query": None,
        "developer_knowledge_retrieval_id": None,
        "developer_knowledge_context": None,
        "developer_knowledge_sources": [],
        "developer_knowledge_state": "not_started",
        "knowledge_context_state": "not_started",
        "knowledge_retrieval_used": False,
        "retrieved_context_count": 0,
        "knowledge_context_tokens": 0,
        "retrieval_id": None,
        "dependency_policy_applied": False,
        "normalized_dependency_file": None,
        "dependency_policy_errors": [],
        "dependency_normalization_attempts": 0,
        "workspace_inspected": False,
        "project_created": False,
        "project_exists": False,
        "created_project_name": None,
        "detected_test_framework": None,
        "expected_test_command": None,
        "environment_prepared": False,
        "dependencies_installed": False,
        "environment_python": None,
        "installed_fastapi_version": None,
        "installed_starlette_version": None,
        "tests_executed": False,
        "tests_passed": False,
        "qa_knowledge_query": None,
        "qa_knowledge_retrieval_id": None,
        "qa_knowledge_context": None,
        "qa_knowledge_sources": [],
        "qa_knowledge_state": "not_started",
        "qa_knowledge_retrieval_used": False,
        "qa_retrieved_context_count": 0,
        "qa_knowledge_context_tokens": 0,
        "repair_knowledge_query": None,
        "repair_knowledge_retrieval_id": None,
        "repair_knowledge_context": None,
        "repair_knowledge_sources": [],
        "repair_knowledge_state": "not_started",
        "repair_knowledge_retrieval_used": False,
        "repair_retrieved_context_count": 0,
        "repair_knowledge_context_tokens": 0,
        "test_infrastructure_failed": False,
        "retry_limit_reached": False,
        "user_cancelled": False,
        "terminal_status": None,
        "test_warning_count": None,
        "actual_test_command": None,
        "failure_type": None,
        "failure_stage": None,
        "failure_message": None,
        "test_stdout": None,
        "test_stderr": None,
        "test_failure_summary": None,
        "repair_phase": "not_started",
        "repair_attempts": 0,
        "repair_decision": None,
        "repair_before": None,
        "repair_after": None,
        "failing_test_files": [],
        "files_read_during_repair": [],
        "files_updated_during_repair": [],
        "first_test_result_summary": None,
        "final_test_result_summary": None,
        "failing_test_content": None,
        "related_source_content": None,
        "related_source_file": None,
        "related_source_candidates": [],
        "related_source_read_attempts": 0,
        "pending_operation": None,
        "pending_tool_name": None,
        "pending_tool_arguments": None,
        "pending_approval_preview": None,
        "pending_approval_status": "none",
        "approval_reason": None,
        "last_approved_tool": None,
        "last_rejected_tool": None,
        "fork_origin_checkpoint_id": None,
        "fork_reason": None,
        "fork_updated_fields": [],
        "fork_lineage": None,
        "supervisor_decision": None,
        "supervisor_reason": None,
        "supervisor_confidence": None,
        "supervisor_decision_source": None,
        "supervisor_attempts": 0,
        "max_supervisor_attempts": 3,
        "allowed_handoffs": [],
        "handoff_history": [],
        "supervisor_errors": [],
        "supervisor_invalid_decision_count": 0,
        "consecutive_invalid_decisions": 0,
        "supervisor_stagnant_loop_probe_count": 0,
        "supervisor_stagnant_loop_fingerprint": None,
        "supervisor_stagnant_loop_active": False,
        "last_completed_stage": None,
        "last_completed_node": None,
        "workflow_learning_candidates": [],
        "workflow_learning_submission_results": [],
        "workflow_learning_state": "not_started",
        "final_response": "",
        "git_state": "not_initialized",
        "git_repository": False,
        "git_base_branch": None,
        "git_base_commit": None,
        "git_branch": None,
        "git_head_commit": None,
        "git_staged_files": [],
        "git_commit_preview": None,
        "git_commit_approval_id": None,
        "git_commit_status": None,
        "git_workflow_state": "not_initialized",
        "git_workflow_branch": None,
        "git_developer_commit_sha": None,
        "git_repair_commit_sha": None,
        "git_commit_history": [],
        "git_commit_phase": None,
        "git_integration_enabled": False,
        "git_promotion_state": "not_started",
        "git_promotion_id": None,
        "git_promotion_preview": None,
        "git_promotion_approval_id": None,
        "git_promotion_result_commit": None,
        "git_promotion_strategy": None,
        "git_promotion_conflicts": [],
        "git_auto_prepare_promotion": False,
        "git_promotion_required": False,
        "ci_state": "not_started",
        "ci_run_id": None,
        "ci_status": None,
        "ci_decision": None,
        "ci_validated_commit": None,
        "ci_failure_type": None,
        "ci_failure_message": None,
        "ci_gate_summary": {},
        "ci_promotion_eligible": None,
        "ci_promotion_eligibility": None,
        "ci_promotion_required": False,
        "ci_promotion_allow_warnings": True,
        "ci_repair_state": "not_required",
        "ci_repair_attempts": 0,
        "ci_repair_failure_type": None,
        "ci_repair_failure_message": None,
        "ci_repair_source_run_id": None,
        "ci_repair_source_commit": None,
        "ci_repair_target_commit": None,
        "ci_repair_repairability": None,
        "ci_repair_lineage": [],
        "ci_repair_enabled": False,
        "ci_repair_max_attempts": 2,
    }
