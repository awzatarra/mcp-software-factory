from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from streaming.models import WorkflowEvent


class ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowRequirementAnalysis(ExecutionModel):
    requirement: str
    objective: str | None = None
    functional_requirements: list[str] = Field(default_factory=list)
    non_functional_requirements: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    completed: bool = False
    source: str | None = None
    updated_at: datetime | None = None


class WorkflowTask(ExecutionModel):
    task_id: str
    order: int
    agent: str
    title: str | None = None
    description: str
    status: Literal[
        "pending", "running", "waiting", "completed", "failed", "skipped"
    ]
    attempt: int = 0
    result_summary: str | None = None
    primary_event_id: str | None = None
    related_event_ids: list[str] = Field(default_factory=list)
    related_files: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class WorkflowRefinement(ExecutionModel):
    sequence: int
    stage: str
    reason: str | None = None
    before_summary: str | None = None
    after_summary: str | None = None
    changed_fields: list[str] = Field(default_factory=list)
    attempt: int = 0
    event_id: str | None = None
    created_at: datetime | None = None


class WorkflowPlannerJudge(ExecutionModel):
    status: str | None = None
    model: str | None = None
    version: str | None = None
    result: dict[str, Any] | None = None
    disagreement: dict[str, Any] | None = None
    plan_fingerprint: str | None = None
    evaluated_at: datetime | None = None


class WorkflowPlannerHybridEvaluation(ExecutionModel):
    status: str | None = None
    source: str | None = None
    score: float | None = None
    confidence: float | None = None
    agreement: str | None = None
    dimensions: dict[str, Any] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)
    recommendation: str | None = None
    version: str | None = None
    weights: dict[str, Any] = Field(default_factory=dict)


class WorkflowPlanningResult(ExecutionModel):
    valid: bool = False
    attempts: int = 0
    project_type: str | None = None
    framework: str | None = None
    analysis: WorkflowRequirementAnalysis
    tasks: list[WorkflowTask] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    refinements: list[WorkflowRefinement] = Field(default_factory=list)
    judge: WorkflowPlannerJudge = Field(default_factory=WorkflowPlannerJudge)
    hybrid_evaluation: WorkflowPlannerHybridEvaluation = Field(default_factory=WorkflowPlannerHybridEvaluation)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class WorkflowImplementationResult(ExecutionModel):
    valid: bool = False
    attempts: int = 0
    project_name: str | None = None
    package_name: str | None = None
    framework: str | None = None
    project_implementation: dict[str, Any] | None = None
    generated_files: list[str] = Field(default_factory=list)
    updated_files: list[str] = Field(default_factory=list)
    dependency_policy_applied: bool = False
    dependency_normalization_attempts: int = 0
    environment_prepared: bool = False
    dependencies_installed: bool = False
    installed_dependencies: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    refinements: list[WorkflowRefinement] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class WorkflowTestingResult(ExecutionModel):
    executed: bool = False
    passed: bool = False
    framework: str | None = None
    expected_command: list[str] = Field(default_factory=list)
    actual_command: list[str] = Field(default_factory=list)
    summary: str | None = None
    warnings: int = 0
    failure_type: str | None = None
    failure_stage: str | None = None
    failure_message: str | None = None
    failing_test_files: list[str] = Field(default_factory=list)
    repair_phase: str = "not_started"
    repair_attempts: int = 0
    repair_decision: str | None = None
    repair_before: dict[str, Any] | None = None
    repair_after: dict[str, Any] | None = None
    files_read_during_repair: list[str] = Field(default_factory=list)
    files_updated_during_repair: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class WorkflowSupervisorResult(ExecutionModel):
    decision: str | None = None
    decision_source: str | None = None
    confidence: float | None = None
    attempts: int = 0
    errors: list[str] = Field(default_factory=list)
    loop_detected: bool = False
    handoff_history: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowFinalResult(ExecutionModel):
    terminal_status: str
    summary: str | None = None
    project_created: bool = False
    tests_passed: bool = False
    failure_type: str | None = None
    failure_message: str | None = None


class WorkflowAgentPerformanceItem(ExecutionModel):
    agent: str
    status: str
    score: float | None = None
    level: str | None = None
    confidence: float
    metrics: dict[str, Any] = Field(default_factory=dict)
    strengths: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    version: str | None = None


class WorkflowFailureAttribution(ExecutionModel):
    status: str | None = None
    failure_class: str | None = None
    root_cause: str | None = None
    primary_attribution: str | None = None
    contributors: list[dict[str, Any]] = Field(default_factory=list)
    excluded_attributions: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)
    recovered: bool = False
    recovery_source: str | None = None
    causal_chain: list[str] = Field(default_factory=list)
    version: str | None = None


class WorkflowLearningCandidateSummary(ExecutionModel):
    candidate_id: str
    knowledge_type: str
    confidence: float
    submission_status: str
    knowledge_id: str | None = None
    source_reference: str
    created_at: datetime | None = None


class WorkflowLearningSummary(ExecutionModel):
    state: str = "not_started"
    extracted_count: int = 0
    submitted_count: int = 0
    duplicate_count: int = 0
    rejected_count: int = 0
    candidates: list[WorkflowLearningCandidateSummary] = Field(default_factory=list)


class WorkflowGitSummary(ExecutionModel):
    state: str = "not_initialized"
    base_branch: str | None = None
    base_commit: str | None = None
    workflow_branch: str | None = None
    head_commit: str | None = None
    commit_status: str | None = None
    approval_state: str | None = None
    developer_commit: dict[str, Any] | None = None
    repair_commit: dict[str, Any] | None = None
    commits: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowAgentExecutionResponse(ExecutionModel):
    thread_id: str
    branch_id: str
    lineage: str
    inherited_from: str | None = None
    inherited_from_branch: str | None = None
    origin_checkpoint: str | None = None
    data_complete: bool
    workflow_intent: str | None = None
    project_name: str | None = None
    terminal_status: str
    planning: WorkflowPlanningResult
    planner_knowledge: dict[str, Any] = Field(
        default_factory=lambda: {
            "state": "not_started",
            "retrieval_id": None,
            "retrieval_used": False,
            "retrieved_context_count": 0,
            "context_tokens": 0,
            "sources": [],
        }
    )
    implementation: WorkflowImplementationResult
    testing: WorkflowTestingResult
    agent_performance: dict[str, WorkflowAgentPerformanceItem] = Field(default_factory=dict)
    failure_attribution: WorkflowFailureAttribution = Field(default_factory=WorkflowFailureAttribution)
    git: WorkflowGitSummary = Field(default_factory=WorkflowGitSummary)
    qa_knowledge: dict[str, Any] = Field(
        default_factory=lambda: {
            "state": "not_started",
            "retrieval_id": None,
            "retrieval_used": False,
            "retrieved_context_count": 0,
            "context_tokens": 0,
            "sources": [],
        }
    )
    repair_knowledge: dict[str, Any] = Field(
        default_factory=lambda: {
            "state": "not_started",
            "retrieval_id": None,
            "retrieval_used": False,
            "retrieved_context_count": 0,
            "context_tokens": 0,
            "sources": [],
        }
    )
    workflow_learning: WorkflowLearningSummary = Field(default_factory=WorkflowLearningSummary)
    supervisor: WorkflowSupervisorResult
    final_result: WorkflowFinalResult
    events: list[WorkflowEvent] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
