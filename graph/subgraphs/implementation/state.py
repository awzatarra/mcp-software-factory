from __future__ import annotations

from typing import Any, Literal, TypedDict

from graph.state import PendingApprovalStatus, PendingOperation, TerminalStatus, WorkflowIntent


DeveloperKnowledgeState = Literal["not_started", "available", "empty", "unavailable"]


class ImplementationState(TypedDict, total=False):
    original_user_message: str
    project_name: str | None
    workflow_intent: WorkflowIntent
    project_exists: bool
    requirement_analysis: dict[str, Any] | None
    acceptance_criteria: list[dict[str, Any]]
    implementation_tasks: list[dict[str, Any]]
    generated_package_name: str | None
    generated_files: list[str]
    collectable_test_count: int
    test_functions: list[str]
    implementation_valid: bool
    implementation_errors: list[str]
    resolved_validation_errors: list[str]
    remaining_validation_errors: list[str]
    refinement_input_errors: list[str]
    implementation_attempts: int
    max_implementation_attempts: int
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
    project_created: bool
    created_project_name: str | None
    detected_test_framework: str | None
    expected_test_command: list[str] | None
    environment_prepared: bool
    dependencies_installed: bool
    environment_python: str | None
    installed_fastapi_version: str | None
    installed_starlette_version: str | None
    test_infrastructure_failed: bool
    pending_operation: PendingOperation | None
    pending_tool_name: str | None
    pending_tool_arguments: dict[str, Any] | None
    pending_approval_preview: dict[str, Any] | None
    pending_approval_status: PendingApprovalStatus
    approval_reason: str | None
    last_approved_tool: str | None
    last_rejected_tool: str | None
    user_cancelled: bool
    terminal_status: TerminalStatus | None
    failure_type: str | None
    failure_stage: str | None
    failure_message: str | None
    last_completed_node: str | None


IMPLEMENTATION_STATE_FIELDS = frozenset(ImplementationState.__annotations__)

IMPLEMENTATION_OUTPUT_KEYS = frozenset(
    {
        "generated_package_name",
        "generated_files",
        "collectable_test_count",
        "test_functions",
        "implementation_valid",
        "implementation_errors",
        "resolved_validation_errors",
        "remaining_validation_errors",
        "implementation_attempts",
        "implementation_failure_reason",
        "project_implementation",
        "developer_knowledge_query",
        "developer_knowledge_retrieval_id",
        "developer_knowledge_context",
        "developer_knowledge_sources",
        "developer_knowledge_state",
        "knowledge_context_state",
        "knowledge_retrieval_used",
        "retrieved_context_count",
        "knowledge_context_tokens",
        "retrieval_id",
        "dependency_policy_applied",
        "normalized_dependency_file",
        "dependency_policy_errors",
        "dependency_normalization_attempts",
        "project_created",
        "created_project_name",
        "project_exists",
        "detected_test_framework",
        "expected_test_command",
        "environment_prepared",
        "dependencies_installed",
        "environment_python",
        "installed_fastapi_version",
        "installed_starlette_version",
        "test_infrastructure_failed",
        "pending_operation",
        "pending_tool_name",
        "pending_tool_arguments",
        "pending_approval_preview",
        "pending_approval_status",
        "approval_reason",
        "last_approved_tool",
        "last_rejected_tool",
        "user_cancelled",
        "failure_type",
        "failure_stage",
        "failure_message",
        "terminal_status",
        "last_completed_node",
    }
)
