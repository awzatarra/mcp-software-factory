from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from api.dashboard_models import BranchScope, DashboardCountItem


AlertSeverity = Literal["info", "warning", "error", "critical"]
AlertStatus = Literal["open", "acknowledged", "resolved", "muted"]
AlertCategory = Literal[
    "workflow", "approval", "testing", "repair", "evaluation",
    "supervisor", "events", "dashboard", "llm_cost",
]


class AlertModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AlertRuleCondition(AlertModel):
    metric: str
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "in"]
    value: str | int | float | bool | list[str]


class AlertRuleThreshold(AlertModel):
    warning: float | None = Field(default=None, ge=0)
    error: float | None = Field(default=None, ge=0)
    critical: float | None = Field(default=None, ge=0)


class AlertRule(AlertModel):
    rule_id: str
    code: str
    name: str
    description: str
    category: AlertCategory
    enabled: bool
    default_severity: AlertSeverity
    threshold: AlertRuleThreshold | None = None
    cooldown_seconds: int
    auto_resolve: bool
    branch_scope: BranchScope
    created_at: datetime
    updated_at: datetime | None = None


class AlertRuleUpdate(AlertModel):
    enabled: bool | None = None
    threshold: AlertRuleThreshold | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0, le=2_592_000)
    auto_resolve: bool | None = None
    default_severity: AlertSeverity | None = None
    branch_scope: BranchScope | None = None


class AlertEvidence(AlertModel):
    metric: str
    actual_value: str | int | float | bool | None
    threshold_value: str | int | float | bool | None
    related_task_id: str | None = None
    related_event_id: str | None = None
    related_file: str | None = None
    pending_operation: str | None = None
    budget_id: str | None = None
    period_key: str | None = None
    agent_name: str | None = None
    provider: str | None = None
    model: str | None = None
    llm_call_id: str | None = None
    estimated_amount: str | None = None


class AlertOccurrence(AlertModel):
    occurrence_id: str
    detected_at: datetime
    severity: AlertSeverity
    message: str
    evidence: AlertEvidence
    source_event_id: str | None = None


class AlertAction(AlertModel):
    action_id: str
    action: str
    actor: str | None = None
    note: str | None = None
    previous_status: AlertStatus | None = None
    new_status: AlertStatus | None = None
    created_at: datetime


class AlertNavigation(AlertModel):
    workflow: str
    evaluation: str | None = None
    timeline: str | None = None
    execution: str | None = None
    project: str | None = None


class WorkflowAlert(AlertModel):
    alert_id: str
    rule_id: str
    rule_code: str
    thread_id: str
    branch_id: str
    project_name: str | None = None
    category: str
    severity: AlertSeverity
    status: AlertStatus
    title: str
    message: str
    fingerprint: str
    first_detected_at: datetime
    last_detected_at: datetime
    occurrence_count: int
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None
    muted_until: datetime | None = None
    current_evidence: AlertEvidence
    primary_event_id: str | None = None
    related_task_id: str | None = None
    related_file: str | None = None
    created_at: datetime
    updated_at: datetime


class AlertStatusCounts(AlertModel):
    open: int = 0
    acknowledged: int = 0
    resolved: int = 0
    muted: int = 0
    critical: int = 0
    error: int = 0
    warning: int = 0
    info: int = 0


class AlertListResponse(AlertModel):
    items: list[WorkflowAlert]
    total: int
    limit: int
    offset: int
    has_more: bool
    counts: AlertStatusCounts


class AlertRuleListResponse(AlertModel):
    items: list[AlertRule]
    total: int
    limit: int
    offset: int
    has_more: bool


class AlertDetailResponse(AlertModel):
    alert: WorkflowAlert
    occurrences: list[AlertOccurrence]
    actions: list[AlertAction]
    rule: AlertRule
    navigation: AlertNavigation
    notification_deliveries: list[dict[str, Any]] = Field(default_factory=list)


class AlertActionRequest(AlertModel):
    actor: str = Field(default="local-user", min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=2_000)


class AlertMuteRequest(AlertActionRequest):
    duration_seconds: int = Field(ge=60, le=2_592_000)


class AlertEvaluationRequest(AlertModel):
    thread_id: str = Field(min_length=1, max_length=200)
    branch_id: str = Field(default="original", min_length=1, max_length=200)


class AlertEvaluationResult(AlertModel):
    evaluated_rules: int = 0
    created_alerts: int = 0
    updated_alerts: int = 0
    resolved_alerts: int = 0
    reopened_alerts: int = 0
    unchanged_alerts: int = 0
    errors: list[str] = Field(default_factory=list)


class AlertSummaryResponse(AlertModel):
    open_total: int
    acknowledged_total: int
    resolved_total: int
    muted_total: int
    critical_open: int
    error_open: int
    warning_open: int
    info_open: int
    affected_workflows: int
    top_rules: list[DashboardCountItem]
    average_time_to_acknowledge_seconds: float | None
    average_time_to_resolve_seconds: float | None
    oldest_open_alert_seconds: float | None
    calculated_at: datetime
