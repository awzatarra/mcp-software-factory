from __future__ import annotations

from typing import Any, TypedDict

from graph.state import (
    PendingApprovalStatus,
    PendingOperation,
    QAKnowledgeState,
    RepairKnowledgeState,
    RepairPhase,
    TerminalStatus,
    WorkflowIntent,
)


class TestingRepairState(TypedDict, total=False):
    original_user_message: str
    project_name: str | None
    created_project_name: str | None
    workflow_intent: WorkflowIntent
    requirement_analysis: dict[str, Any] | None
    acceptance_criteria: list[dict[str, Any]]
    implementation_result: dict[str, Any]
    generated_files: list[str]
    implementation_errors: list[str]
    remaining_validation_errors: list[str]
    detected_test_framework: str | None
    expected_test_command: list[str] | None
    environment_python: str | None
    environment_prepared: bool
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
    actual_test_command: list[str] | None
    test_warning_count: int | None
    test_stdout: str | None
    test_stderr: str | None
    test_failure_summary: str | None
    first_test_result_summary: str | None
    final_test_result_summary: str | None
    test_infrastructure_failed: bool
    retry_limit_reached: bool
    repair_phase: RepairPhase
    repair_attempts: int
    repair_decision: str | None
    repair_before: str | None
    repair_after: str | None
    failing_test_files: list[str]
    failing_test_content: str | None
    files_read_during_repair: list[str]
    files_updated_during_repair: list[str]
    related_source_file: str | None
    related_source_content: str | None
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
    user_cancelled: bool
    terminal_status: TerminalStatus | None
    failure_type: str | None
    failure_stage: str | None
    failure_message: str | None
    fork_origin_checkpoint_id: str | None
    fork_reason: str | None
    fork_updated_fields: list[str]
    fork_lineage: str | None
    last_completed_node: str | None


TESTING_REPAIR_STATE_FIELDS = frozenset(TestingRepairState.__annotations__)

TESTING_REPAIR_INPUT_KEYS = frozenset(
    {
        "project_name",
        "created_project_name",
        "workflow_intent",
        "original_user_message",
        "requirement_analysis",
        "acceptance_criteria",
        "implementation_result",
        "generated_files",
        "implementation_errors",
        "remaining_validation_errors",
        "detected_test_framework",
        "expected_test_command",
        "environment_python",
        "tests_executed",
        "tests_passed",
        "qa_knowledge_query",
        "qa_knowledge_retrieval_id",
        "qa_knowledge_context",
        "qa_knowledge_sources",
        "qa_knowledge_state",
        "qa_knowledge_retrieval_used",
        "qa_retrieved_context_count",
        "qa_knowledge_context_tokens",
        "repair_knowledge_query",
        "repair_knowledge_retrieval_id",
        "repair_knowledge_context",
        "repair_knowledge_sources",
        "repair_knowledge_state",
        "repair_knowledge_retrieval_used",
        "repair_retrieved_context_count",
        "repair_knowledge_context_tokens",
        "actual_test_command",
        "test_warning_count",
        "test_stdout",
        "test_stderr",
        "test_failure_summary",
        "first_test_result_summary",
        "final_test_result_summary",
        "test_infrastructure_failed",
        "retry_limit_reached",
        "repair_phase",
        "repair_attempts",
        "repair_decision",
        "repair_before",
        "repair_after",
        "failing_test_files",
        "failing_test_content",
        "files_read_during_repair",
        "files_updated_during_repair",
        "related_source_file",
        "related_source_content",
        "related_source_candidates",
        "related_source_read_attempts",
        "pending_operation",
        "pending_tool_name",
        "pending_tool_arguments",
        "pending_approval_preview",
        "pending_approval_status",
        "approval_reason",
        "last_approved_tool",
        "last_rejected_tool",
        "user_cancelled",
        "terminal_status",
        "failure_type",
        "failure_stage",
        "failure_message",
        "fork_origin_checkpoint_id",
        "fork_reason",
        "fork_updated_fields",
        "fork_lineage",
        "last_completed_node",
    }
)

TESTING_REPAIR_OUTPUT_KEYS = frozenset(
    {
        "tests_executed",
        "tests_passed",
        "qa_knowledge_query",
        "qa_knowledge_retrieval_id",
        "qa_knowledge_context",
        "qa_knowledge_sources",
        "qa_knowledge_state",
        "qa_knowledge_retrieval_used",
        "qa_retrieved_context_count",
        "qa_knowledge_context_tokens",
        "repair_knowledge_query",
        "repair_knowledge_retrieval_id",
        "repair_knowledge_context",
        "repair_knowledge_sources",
        "repair_knowledge_state",
        "repair_knowledge_retrieval_used",
        "repair_retrieved_context_count",
        "repair_knowledge_context_tokens",
        "actual_test_command",
        "test_warning_count",
        "first_test_result_summary",
        "final_test_result_summary",
        "test_failure_summary",
        "test_infrastructure_failed",
        "retry_limit_reached",
        "repair_phase",
        "repair_attempts",
        "repair_decision",
        "repair_before",
        "repair_after",
        "failing_test_files",
        "files_read_during_repair",
        "files_updated_during_repair",
        "related_source_file",
        "failure_type",
        "failure_stage",
        "failure_message",
        "terminal_status",
        "pending_operation",
        "pending_tool_name",
        "pending_tool_arguments",
        "pending_approval_preview",
        "pending_approval_status",
        "approval_reason",
        "last_approved_tool",
        "last_rejected_tool",
        "user_cancelled",
        "fork_origin_checkpoint_id",
        "fork_reason",
        "fork_updated_fields",
        "fork_lineage",
    }
)
