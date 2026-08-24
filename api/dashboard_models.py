from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


BranchScope = Literal["original", "all"]
DashboardStatus = Literal["all", "completed", "failed", "running", "waiting", "pending", "cancelled"]
DashboardGrade = Literal["all", "excellent", "good", "acceptable", "poor", "critical"]


class DashboardModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DashboardFilters(DashboardModel):
    date_from: datetime
    date_to: datetime
    status: DashboardStatus = "all"
    project_name: str | None = None
    workflow_intent: str | None = None
    framework: str | None = None
    grade: DashboardGrade = "all"
    branch_scope: BranchScope = "original"
    scoring_version: str = "1.2"
    timezone: str = "UTC"


class DashboardCountItem(DashboardModel):
    key: str
    label: str
    count: int
    percentage: float | None = None


class WorkflowCounts(DashboardModel):
    total: int = 0
    completed: int = 0
    failed: int = 0
    running: int = 0
    waiting: int = 0
    pending: int = 0
    cancelled: int = 0
    success_rate_percent: float | None = None
    failure_rate_percent: float | None = None


class ScoreSummary(DashboardModel):
    evaluated_workflows: int = 0
    unevaluated_workflows: int = 0
    average_score: float | None = None
    median_score: float | None = None
    min_score: float | None = None
    max_score: float | None = None
    excellent: int = 0
    good: int = 0
    acceptable: int = 0
    poor: int = 0
    critical: int = 0
    provisional: int = 0
    scoring_versions: list[str] = Field(default_factory=list)


class DurationSummary(DashboardModel):
    workflows_with_duration: int = 0
    discarded_workflows: int = 0
    average_wall_clock_seconds: float | None = None
    median_wall_clock_seconds: float | None = None
    p50_wall_clock_seconds: float | None = None
    p90_wall_clock_seconds: float | None = None
    p95_wall_clock_seconds: float | None = None
    average_active_seconds: float | None = None
    average_approval_wait_seconds: float | None = None
    approval_wait_percent: float | None = None
    average_planning_seconds: float | None = None
    average_implementation_seconds: float | None = None
    average_testing_seconds: float | None = None
    average_repair_seconds: float | None = None


class TestingSummary(DashboardModel):
    executed: int = 0
    passed: int = 0
    failed: int = 0
    not_executed: int = 0
    pass_rate_percent: float | None = None
    workflows_with_warnings: int = 0
    total_warnings: int = 0
    average_warnings: float | None = None
    repair_required: int = 0
    repair_successful: int = 0
    repair_failed: int = 0
    repair_success_rate_percent: float | None = None
    average_repair_attempts: float | None = None


class ApprovalSummary(DashboardModel):
    total_approvals_requested: int = 0
    total_approvals_granted: int = 0
    total_approvals_rejected: int = 0
    workflows_with_pending_approval: int = 0
    average_approval_wait_seconds: float | None = None
    longest_approval_wait_seconds: float | None = None
    most_requested_operations: list[DashboardCountItem] = Field(default_factory=list)


class DashboardSummaryResponse(DashboardModel):
    date_from: datetime
    date_to: datetime
    timezone: str
    branch_scope: BranchScope
    workflow_counts: WorkflowCounts
    scores: ScoreSummary
    durations: DurationSummary
    testing: TestingSummary
    approvals: ApprovalSummary
    frameworks: list[DashboardCountItem] = Field(default_factory=list)
    intents: list[DashboardCountItem] = Field(default_factory=list)
    top_findings: list[DashboardCountItem] = Field(default_factory=list)
    top_recommendations: list[DashboardCountItem] = Field(default_factory=list)
    calculated_at: datetime
    source_updated_at: datetime | None = None
    data_complete: bool = True


class DashboardTimeSeriesPoint(DashboardModel):
    bucket_start: datetime
    bucket_end: datetime
    value: float | int | None
    count: int
    numerator: float | int | None = None
    denominator: float | int | None = None


class DashboardTimeSeriesResponse(DashboardModel):
    metric: str
    interval: str
    timezone: str
    points: list[DashboardTimeSeriesPoint]
    source_updated_at: datetime | None = None


class DashboardAgentMetric(DashboardModel):
    agent: str
    total_tasks: int
    completed_tasks: int
    failed_tasks: int
    waiting_tasks: int
    skipped_tasks: int
    completion_rate_percent: float | None
    average_attempts: float | None
    average_duration_seconds: float | None
    related_workflows: int
    related_files: int
    findings_count: int
    average_score: float | None = None


class DashboardAgentsResponse(DashboardModel):
    items: list[DashboardAgentMetric]
    total: int
    limit: int
    offset: int
    has_more: bool


class DashboardAttentionItem(DashboardModel):
    thread_id: str
    branch_id: str
    project_name: str | None = None
    terminal_status: str
    score: float | None = None
    grade: str | None = None
    severity: Literal["critical", "error", "warning"]
    reasons: list[str]
    finding_codes: list[str]
    pending_operation: str | None = None
    age_seconds: float
    updated_at: datetime | None = None


class DashboardAttentionResponse(DashboardModel):
    items: list[DashboardAttentionItem]
    total: int
    limit: int
    offset: int
    has_more: bool


class DashboardActivityItem(DashboardModel):
    event_id: str
    thread_id: str
    branch_id: str
    project_name: str | None = None
    type: str
    status: str
    message: str
    timestamp: datetime
    related_event_id: str | None = None


class DashboardActivityResponse(DashboardModel):
    items: list[DashboardActivityItem]
    total: int
    limit: int
    offset: int
    has_more: bool
