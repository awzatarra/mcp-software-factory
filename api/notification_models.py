from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.dashboard_models import DashboardCountItem


ChannelType = Literal["internal", "log", "webhook", "slack_webhook", "teams_webhook", "email"]
DeliveryStatus = Literal["pending", "processing", "delivered", "failed", "retry_scheduled", "dead_letter", "suppressed", "cancelled"]
AlertEvent = Literal[
    "alert_opened", "alert_reopened", "alert_severity_escalated",
    "alert_acknowledged", "alert_resolved", "alert_muted",
    "alert_unmuted", "alert_auto_resolved", "escalation_triggered",
    "channel_test",
]
Severity = Literal["info", "warning", "error", "critical"]
DAY_NAMES = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


class NotificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotificationChannel(NotificationModel):
    channel_id: str
    name: str
    description: str | None = None
    channel_type: ChannelType
    enabled: bool
    configuration: dict[str, Any] = Field(default_factory=dict)
    secret_reference: str | None = None
    secret_configured: bool = False
    health: Literal["healthy", "unknown", "misconfigured", "disabled", "circuit_open"] = "unknown"
    timeout_seconds: int
    max_attempts: int
    initial_backoff_seconds: int
    max_backoff_seconds: int
    rate_limit_per_minute: int | None = None
    verify_tls: bool
    created_at: datetime
    updated_at: datetime | None = None


class NotificationChannelCreate(NotificationModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    channel_type: ChannelType
    enabled: bool = True
    configuration: dict[str, Any] = Field(default_factory=dict)
    secret_reference: str | None = Field(default=None, max_length=200)
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    max_attempts: int = Field(default=3, ge=1, le=20)
    initial_backoff_seconds: int = Field(default=2, ge=1, le=3600)
    max_backoff_seconds: int = Field(default=300, ge=1, le=86400)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=10000)
    verify_tls: bool = True

    @field_validator("secret_reference")
    @classmethod
    def validate_secret_reference(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("env:"):
            raise ValueError("Only env: secret references are supported")
        return value

    @model_validator(mode="after")
    def validate_backoff(self):
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= initial_backoff_seconds")
        return self


class NotificationChannelUpdate(NotificationModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None
    configuration: dict[str, Any] | None = None
    secret_reference: str | None = Field(default=None, max_length=200)
    timeout_seconds: int | None = Field(default=None, ge=1, le=60)
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    initial_backoff_seconds: int | None = Field(default=None, ge=1, le=3600)
    max_backoff_seconds: int | None = Field(default=None, ge=1, le=86400)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=10000)
    verify_tls: bool | None = None

    @field_validator("secret_reference")
    @classmethod
    def validate_secret_reference(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("env:"):
            raise ValueError("Only env: secret references are supported")
        return value


class EscalationStep(NotificationModel):
    step: int = Field(ge=1, le=100)
    delay_seconds: int = Field(ge=0, le=2_592_000)
    channel_ids: list[str] = Field(min_length=1, max_length=20)
    severities: list[Severity] | None = None
    require_unacknowledged: bool = True
    repeat: bool = False
    repeat_interval_seconds: int | None = Field(default=None, ge=30, le=2_592_000)
    max_repeats: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def validate_repeat(self):
        if self.repeat and (self.repeat_interval_seconds is None or self.max_repeats is None):
            raise ValueError("Repeating steps require repeat_interval_seconds and max_repeats")
        return self


class EscalationPolicy(NotificationModel):
    escalation_policy_id: str
    name: str
    description: str | None = None
    enabled: bool
    steps: list[EscalationStep]
    stop_on_acknowledge: bool
    stop_on_resolve: bool
    created_at: datetime
    updated_at: datetime | None = None


class EscalationPolicyCreate(NotificationModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool = True
    steps: list[EscalationStep] = Field(min_length=1, max_length=20)
    stop_on_acknowledge: bool = True
    stop_on_resolve: bool = True

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, value: list[EscalationStep]) -> list[EscalationStep]:
        numbers = [item.step for item in value]
        if numbers != sorted(numbers) or len(numbers) != len(set(numbers)):
            raise ValueError("Escalation steps must be unique and ascending")
        return value


class EscalationPolicyUpdate(NotificationModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None
    steps: list[EscalationStep] | None = Field(default=None, min_length=1, max_length=20)
    stop_on_acknowledge: bool | None = None
    stop_on_resolve: bool | None = None

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, value: list[EscalationStep] | None) -> list[EscalationStep] | None:
        if value is None:
            return value
        numbers = [item.step for item in value]
        if numbers != sorted(numbers) or len(numbers) != len(set(numbers)):
            raise ValueError("Escalation steps must be unique and ascending")
        return value


class QuietHoursSchedule(NotificationModel):
    quiet_hours_id: str
    name: str
    enabled: bool
    timezone: str
    days_of_week: list[str]
    start_time: time
    end_time: time
    suppress_severities: list[Severity]
    allow_critical: bool
    created_at: datetime
    updated_at: datetime | None = None


class QuietHoursCreate(NotificationModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    timezone: str = Field(default="UTC", max_length=100)
    days_of_week: list[str] = Field(min_length=1, max_length=7)
    start_time: time
    end_time: time
    suppress_severities: list[Severity] = Field(default_factory=lambda: ["info", "warning"])
    allow_critical: bool = True

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value

    @field_validator("days_of_week")
    @classmethod
    def validate_days(cls, value: list[str]) -> list[str]:
        normalized = [item.casefold() for item in value]
        if any(item not in DAY_NAMES for item in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("Invalid or duplicate day of week")
        return normalized


class QuietHoursUpdate(NotificationModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    timezone: str | None = Field(default=None, max_length=100)
    days_of_week: list[str] | None = None
    start_time: time | None = None
    end_time: time | None = None
    suppress_severities: list[Severity] | None = None
    allow_critical: bool | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value

    @field_validator("days_of_week")
    @classmethod
    def validate_days(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        normalized = [item.casefold() for item in value]
        if not normalized or any(item not in DAY_NAMES for item in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("Invalid or duplicate day of week")
        return normalized


class NotificationPolicy(NotificationModel):
    policy_id: str
    name: str
    description: str | None = None
    enabled: bool
    channel_ids: list[str]
    rule_codes: list[str]
    severities: list[Severity]
    alert_events: list[str]
    statuses: list[str]
    branch_scope: Literal["original", "all"]
    project_name_pattern: str | None = None
    cooldown_seconds: int
    send_resolved: bool
    send_acknowledged: bool
    quiet_hours_id: str | None = None
    escalation_policy_id: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class NotificationPolicyCreate(NotificationModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool = True
    channel_ids: list[str] = Field(min_length=1, max_length=20)
    rule_codes: list[str] = Field(default_factory=list, max_length=100)
    severities: list[Severity] = Field(default_factory=lambda: ["warning", "error", "critical"])
    alert_events: list[str] = Field(default_factory=lambda: ["alert_opened", "alert_reopened", "alert_severity_escalated", "alert_resolved"])
    statuses: list[str] = Field(default_factory=lambda: ["open", "acknowledged", "resolved"])
    branch_scope: Literal["original", "all"] = "original"
    project_name_pattern: str | None = Field(default=None, max_length=120)
    cooldown_seconds: int = Field(default=0, ge=0, le=2_592_000)
    send_resolved: bool = True
    send_acknowledged: bool = False
    quiet_hours_id: str | None = None
    escalation_policy_id: str | None = None

    @field_validator("project_name_pattern")
    @classmethod
    def validate_pattern(cls, value: str | None) -> str | None:
        if value and any(character in value for character in "[]{}\\"):
            raise ValueError("Only simple glob patterns with * and ? are supported")
        return value


class NotificationPolicyUpdate(NotificationModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None
    channel_ids: list[str] | None = None
    rule_codes: list[str] | None = None
    severities: list[Severity] | None = None
    alert_events: list[str] | None = None
    statuses: list[str] | None = None
    branch_scope: Literal["original", "all"] | None = None
    project_name_pattern: str | None = Field(default=None, max_length=120)
    cooldown_seconds: int | None = Field(default=None, ge=0, le=2_592_000)
    send_resolved: bool | None = None
    send_acknowledged: bool | None = None
    quiet_hours_id: str | None = None
    escalation_policy_id: str | None = None

    @field_validator("project_name_pattern")
    @classmethod
    def validate_pattern(cls, value: str | None) -> str | None:
        if value and any(character in value for character in "[]{}\\"):
            raise ValueError("Only simple glob patterns with * and ? are supported")
        return value


class NotificationMessage(NotificationModel):
    notification_id: str
    alert_event: str
    alert_id: str
    rule_code: str
    severity: str
    status: str
    title: str
    message: str
    project_name: str | None = None
    thread_id: str
    branch_id: str
    occurrence_count: int
    first_detected_at: datetime
    last_detected_at: datetime
    application_url: str | None = None
    workflow_url: str | None = None
    evaluation_url: str | None = None
    timeline_url: str | None = None
    execution_url: str | None = None
    project_file_url: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class NotificationSendResult(NotificationModel):
    success: bool
    retryable: bool
    status_code: int | None = None
    response_summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_after_seconds: int | None = None
    request_method: str | None = None
    target_host: str | None = None


class NotificationDelivery(NotificationModel):
    delivery_id: str
    alert_id: str
    occurrence_id: str | None = None
    action_id: str | None = None
    policy_id: str
    channel_id: str
    alert_event: str
    escalation_step: int | None = None
    original_delivery_id: str | None = None
    fingerprint: str
    status: DeliveryStatus
    scheduled_at: datetime
    first_attempt_at: datetime | None = None
    last_attempt_at: datetime | None = None
    delivered_at: datetime | None = None
    next_attempt_at: datetime | None = None
    attempt_count: int
    max_attempts: int
    response_status_code: int | None = None
    response_summary: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    processing_owner: str | None = None
    processing_started_at: datetime | None = None
    processing_expires_at: datetime | None = None
    read_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class NotificationDeliveryAttempt(NotificationModel):
    attempt_id: str
    delivery_id: str
    attempt_number: int
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    result: Literal["success", "retryable_failure", "permanent_failure", "suppressed"]
    request_method: str | None = None
    target_host: str | None = None
    response_status_code: int | None = None
    response_summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class DeliveryDetailResponse(NotificationModel):
    delivery: NotificationDelivery
    attempts: list[NotificationDeliveryAttempt]
    channel: NotificationChannel | None = None
    policy: NotificationPolicy | None = None


class DeliveryListResponse(NotificationModel):
    items: list[NotificationDelivery]
    total: int
    limit: int
    offset: int
    has_more: bool


class NotificationDispatchResult(NotificationModel):
    created: int = 0
    deduplicated: int = 0
    suppressed: int = 0
    delivery_ids: list[str] = Field(default_factory=list)


class DeliveryBatchResult(NotificationModel):
    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    dead_lettered: int = 0
    suppressed: int = 0
    deferred: int = 0


class NotificationSummaryResponse(NotificationModel):
    pending: int = 0
    processing: int = 0
    delivered: int = 0
    retry_scheduled: int = 0
    failed: int = 0
    dead_letter: int = 0
    suppressed: int = 0
    cancelled: int = 0
    delivery_success_rate_percent: float | None = None
    average_delivery_latency_seconds: float | None = None
    p95_delivery_latency_seconds: float | None = None
    channels_enabled: int = 0
    channels_misconfigured: int = 0
    deliveries_last_24h: int = 0
    dead_letters_last_24h: int = 0
    unread_internal: int = 0
    top_failing_channels: list[DashboardCountItem] = Field(default_factory=list)
    calculated_at: datetime


class TestChannelResponse(NotificationModel):
    delivery_id: str
    status: DeliveryStatus


class InboxResponse(NotificationModel):
    items: list[NotificationDelivery]
    unread: int
    total: int
