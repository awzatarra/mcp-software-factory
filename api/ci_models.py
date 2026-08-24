from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from api.models import HttpModel


CIPipelineStepType = Literal["build", "test", "lint", "package", "custom"]
CIStepStatus = Literal["pending", "running", "passed", "failed", "skipped", "timed_out", "cancelled", "interrupted"]
CIPipelineStatus = Literal["pending", "running", "passed", "failed", "timed_out", "cancelled", "interrupted"]
CIGateType = Literal["build", "test", "lint", "package"]
CIGateStatus = Literal["passed", "failed", "warning", "skipped", "not_applicable"]
CIDecision = Literal["accepted", "accepted_with_warnings", "rejected"]
CIRepairCategory = Literal[
    "repairable_code",
    "repairable_tests",
    "repairable_lint",
    "repairable_build",
    "infrastructure",
    "configuration",
    "non_repairable",
    "unknown",
]
CIRepairState = Literal[
    "not_required",
    "pending",
    "running",
    "repaired",
    "failed",
    "exhausted",
    "not_repairable",
]


class CIPipelineStep(HttpModel):
    step_id: str
    name: str
    type: CIPipelineStepType
    command: list[str] = Field(min_length=1)
    working_directory: str | None = None
    timeout_seconds: float | None = None
    required: bool = True
    continue_on_error: bool = False


class CIPipelineDefinition(HttpModel):
    pipeline_id: str
    name: str
    version: str
    framework: str
    steps: list[CIPipelineStep]
    fail_fast: bool = True
    timeout_seconds: float


class CISourceRevision(HttpModel):
    source_mode: Literal["working_tree", "commit"]
    source_commit: str | None = None
    source_branch: str | None = None
    workflow_branch: str | None = None
    repository_root: str | None = None
    dirty: bool | None = None
    effective_clean: bool | None = None


class CIStepRun(HttpModel):
    step_id: str
    name: str
    type: CIPipelineStepType
    status: CIStepStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    stdout_summary: str | None = None
    stderr_summary: str | None = None
    output_truncated: bool = False
    failure_type: str | None = None
    failure_message: str | None = None


class CIGateDefinition(HttpModel):
    gate_id: str
    type: CIGateType
    required: bool
    blocking: bool
    source_step_types: list[CIGateType]
    minimum_success_count: int | None = None
    allow_skipped: bool = False
    policy_version: str


class CIGateResult(HttpModel):
    gate_id: str
    type: CIGateType
    status: CIGateStatus
    required: bool
    blocking: bool
    reason: str
    source_steps: list[str]
    failure_type: str | None = None


class CIGateEvaluation(HttpModel):
    policy_version: str
    gates: list[CIGateResult] = Field(default_factory=list)
    decision: CIDecision
    failed_gates: list[str] = Field(default_factory=list)
    warning_gates: list[str] = Field(default_factory=list)
    blocking_gate: str | None = None
    failure_type: str | None = None
    summary: dict[str, int] = Field(default_factory=dict)


class CIRepairability(HttpModel):
    repairable: bool
    category: CIRepairCategory
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)
    failed_gate: str | None = None
    failed_step: str | None = None
    failure_type: str | None = None
    summary: str | None = None


class CIRepairStatus(HttpModel):
    state: CIRepairState = "not_required"
    attempts: int = 0
    max_attempts: int = 2
    source_run_id: str | None = None
    source_commit: str | None = None
    target_commit: str | None = None
    category: CIRepairCategory | None = None
    reason_codes: list[str] = Field(default_factory=list)
    repairability: CIRepairability | None = None
    lineage: list[dict[str, Any]] = Field(default_factory=list)


class CIPipelineRun(HttpModel):
    ci_run_id: str
    workflow_id: str
    project_id: str
    pipeline: CIPipelineDefinition
    pipeline_fingerprint: str
    status: CIPipelineStatus
    source: CISourceRevision
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    steps: list[CIStepRun]
    failed_step: str | None = None
    failure_type: str | None = None
    failure_message: str | None = None
    warnings: list[str] = Field(default_factory=list)
    gate_policy_version: str | None = None
    gates: list[CIGateResult] = Field(default_factory=list)
    decision: CIDecision | None = None
    failed_gates: list[str] = Field(default_factory=list)
    warning_gates: list[str] = Field(default_factory=list)
    blocking_gate: str | None = None
    gate_summary: dict[str, int] = Field(default_factory=dict)
    ci_validated_commit: str | None = None
    repairability: CIRepairability | None = None


class CIPipelinePreview(HttpModel):
    workflow_id: str
    project_id: str
    state: Literal["available"]
    pipeline: CIPipelineDefinition
    pipeline_fingerprint: str
    source: CISourceRevision
    expected_gates: list[CIGateDefinition] = Field(default_factory=list)
    gate_policy_version: str | None = None


class CIWorkflowStatus(HttpModel):
    workflow_id: str
    project_id: str | None
    state: Literal["not_started", "available", "project_unavailable"]
    latest_run: CIPipelineRun | None = None
    pipeline: CIPipelineDefinition | None = None
    pipeline_fingerprint: str | None = None
    source: CISourceRevision | None = None
    promotion_eligible: bool | None = None
    promotion_eligibility: dict[str, Any] | None = None
    repair: CIRepairStatus = Field(default_factory=CIRepairStatus)


class CIPromotionEligibility(HttpModel):
    required: bool
    eligible: bool
    reason: str
    commit_match: bool
    source_commit: str | None = None
    target_commit: str | None = None
    run_id: str | None = None
    ci_status: CIPipelineStatus | None = None
    ci_decision: CIDecision | None = None
    blocking_gates: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    gate_policy_version: str | None = None
    pipeline_version: str | None = None
    pipeline_fingerprint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CIRunRequest(HttpModel):
    pipeline_fingerprint: str
    ci_run_id: str | None = None


class CIRunListResponse(HttpModel):
    workflow_id: str
    runs: list[CIPipelineRun]
    total: int


class CIDurationMetrics(HttpModel):
    average_seconds: float | None = None
    p50_seconds: float | None = None
    p95_seconds: float | None = None


class CIOperationalSummary(HttpModel):
    total_runs: int = 0
    completed_runs: int = 0
    runs_with_decision: int = 0
    accepted_runs: int = 0
    accepted_with_warnings_runs: int = 0
    rejected_runs: int = 0
    acceptance_rate: float | None = None
    warning_rate: float | None = None
    rejection_rate: float | None = None
    average_pipeline_duration_seconds: float | None = None
    p50_pipeline_duration_seconds: float | None = None
    p95_pipeline_duration_seconds: float | None = None
    commit_bound_runs: int = 0
    working_tree_runs: int = 0


class CIStepMetrics(HttpModel):
    run_count: int = 0
    passed_count: int = 0
    failed_count: int = 0
    timed_out_count: int = 0
    skipped_count: int = 0
    average_duration_seconds: float | None = None
    p50_duration_seconds: float | None = None
    p95_duration_seconds: float | None = None
    failure_rate: float | None = None


class CIGateMetrics(HttpModel):
    evaluated_count: int = 0
    passed_count: int = 0
    failed_count: int = 0
    warning_count: int = 0
    skipped_count: int = 0
    not_applicable_count: int = 0
    failure_rate: float | None = None
    warning_rate: float | None = None


class CIFailureMetrics(HttpModel):
    by_failure_type: dict[str, int] = Field(default_factory=dict)
    by_repair_category: dict[str, int] = Field(default_factory=dict)
    code_related_failure_count: int = 0
    infrastructure_failure_count: int = 0
    configuration_failure_count: int = 0
    unknown_failure_count: int = 0
    code_related_failure_rate: float | None = None
    infrastructure_failure_rate: float | None = None
    configuration_failure_rate: float | None = None
    unknown_failure_rate: float | None = None


class CIRepairMetrics(HttpModel):
    ci_repair_required_count: int = 0
    ci_repair_success_count: int = 0
    ci_repair_failed_count: int = 0
    ci_repair_exhausted_count: int = 0
    ci_repair_success_rate: float | None = None
    ci_repair_exhaustion_rate: float | None = None
    average_ci_repair_attempts: float | None = None
    maximum_ci_repair_attempts_observed: int = 0
    commits_repaired_count: int = 0
    average_commits_per_repair_chain: float | None = None
    runs_recovered_after_repair: int = 0
    runs_failed_after_repair: int = 0


class CIPromotionMetrics(HttpModel):
    promotion_eligible_count: int = 0
    promotion_blocked_count: int = 0
    promotion_blocked_no_ci_count: int = 0
    promotion_blocked_rejected_ci_count: int = 0
    promotion_blocked_commit_mismatch_count: int = 0
    promotion_blocked_stale_ci_count: int = 0
    promotion_eligibility_rate: float | None = None


class CIAnalyticsResponse(HttpModel):
    version: str
    limit: int
    framework: str | None = None
    from_timestamp: datetime | None = None
    to_timestamp: datetime | None = None
    summary: CIOperationalSummary
    steps: dict[str, CIStepMetrics] = Field(default_factory=dict)
    gates: dict[str, CIGateMetrics] = Field(default_factory=dict)
    failures: CIFailureMetrics = Field(default_factory=CIFailureMetrics)
    repair: CIRepairMetrics = Field(default_factory=CIRepairMetrics)
    promotion: CIPromotionMetrics = Field(default_factory=CIPromotionMetrics)
    flat_gates: dict[str, float | None] = Field(default_factory=dict)
    percentile_method: str = "nearest_rank"


class CIAuditEntry(HttpModel):
    event_type: str
    timestamp: datetime | None = None
    workflow_id: str
    ci_run_id: str | None = None
    commit: str | None = None
    step_id: str | None = None
    gate: str | None = None
    decision: str | None = None
    failure_type: str | None = None
    repair_attempt: int | None = None
    repair_commit: str | None = None
    promotion_eligible: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CIAuditTrail(HttpModel):
    workflow_id: str
    total: int
    entries: list[CIAuditEntry] = Field(default_factory=list)
