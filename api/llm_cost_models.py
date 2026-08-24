from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class UsageSource(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    TOKENIZER_ESTIMATED = "tokenizer_estimated"
    UNAVAILABLE = "unavailable"
    IMPORTED = "imported"
    BACKFILL_PARTIAL = "backfill_partial"


class CostSource(StrEnum):
    CALCULATED = "calculated"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"
    MANUAL_OVERRIDE = "manual_override"


class CostStatus(StrEnum):
    PENDING = "pending"
    CALCULATED = "calculated"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    INVALID_PRICING = "invalid_pricing"
    EXCLUDED = "excluded"


class BudgetStatus(StrEnum):
    WITHIN_BUDGET = "within_budget"
    WARNING = "warning"
    EXCEEDED = "exceeded"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class BudgetScope(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"
    WORKFLOW = "workflow"
    BRANCH = "branch"
    AGENT = "agent"
    USER = "user"
    MODEL = "model"
    DAILY = "daily"
    MONTHLY = "monthly"


class EnforcementMode(StrEnum):
    OBSERVE_ONLY = "observe_only"
    WARN = "warn"
    SOFT_LIMIT = "soft_limit"
    HARD_LIMIT = "hard_limit"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PricingCreate(StrictModel):
    provider: str = Field(min_length=1, max_length=100)
    model_pattern: str = Field(min_length=1, max_length=200)
    model_canonical_name: str | None = Field(default=None, max_length=200)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    input_price_per_million: Decimal | None = None
    cached_input_price_per_million: Decimal | None = None
    output_price_per_million: Decimal | None = None
    reasoning_price_per_million: Decimal | None = None
    audio_input_price_per_million: Decimal | None = None
    audio_output_price_per_million: Decimal | None = None
    image_pricing: dict[str, Any] | None = None
    effective_from: datetime
    effective_to: datetime | None = None
    source_type: str = Field(default="manual", max_length=50)
    source_reference: str | None = Field(default=None, max_length=500)
    source_verified_at: datetime | None = None
    enabled: bool = True
    priority: int = Field(default=0, ge=-10_000, le=10_000)
    reasoning_in_completion: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "input_price_per_million", "cached_input_price_per_million",
        "output_price_per_million", "reasoning_price_per_million",
        "audio_input_price_per_million", "audio_output_price_per_million",
    )
    @classmethod
    def non_negative_price(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("Prices cannot be negative")
        return value

    @model_validator(mode="after")
    def valid_range(self):
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class PricingPatch(StrictModel):
    effective_to: datetime | None = None
    source_reference: str | None = Field(default=None, max_length=500)
    source_verified_at: datetime | None = None
    enabled: bool | None = Field(
        default=None,
        description="Omit or send null to leave enabled unchanged.",
    )
    priority: int | None = Field(
        default=None, ge=-10_000, le=10_000,
        description="Omit or send null to leave priority unchanged.",
    )
    metadata: dict[str, Any] | None = None


class PricingResolveRequest(StrictModel):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    timestamp: datetime
    currency: str = Field(default="USD", min_length=3, max_length=3)


class BudgetCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    scope_type: BudgetScope
    scope_value: str | None = Field(default=None, max_length=300)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    limit_amount: Decimal = Field(gt=0)
    warning_percent: Decimal = Field(default=Decimal("80"), ge=0, le=100)
    enforcement_mode: EnforcementMode = EnforcementMode.OBSERVE_ONLY
    period_type: str | None = Field(default=None, pattern="^(daily|monthly|custom)$")
    period_start: datetime | None = None
    period_end: datetime | None = None
    reset_timezone: str = Field(default="UTC", max_length=100)
    enabled: bool = True
    include_estimated: bool = False
    include_failed_calls: bool = True
    include_retries: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def scope_requires_value(self):
        if self.scope_type not in {BudgetScope.GLOBAL, BudgetScope.DAILY, BudgetScope.MONTHLY} and not self.scope_value:
            raise ValueError("scope_value is required for this scope")
        if self.period_end and self.period_start and self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        return self


class BudgetPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    limit_amount: Decimal | None = Field(default=None, gt=0)
    warning_percent: Decimal | None = Field(default=None, ge=0, le=100)
    enforcement_mode: EnforcementMode | None = None
    enabled: bool | None = None
    include_estimated: bool | None = None
    include_failed_calls: bool | None = None
    include_retries: bool | None = None
    metadata: dict[str, Any] | None = None


class BudgetCheckRequest(StrictModel):
    workflow_id: str | None = Field(default=None, max_length=200)
    branch_id: str = Field(default="original", max_length=200)
    project_name: str | None = Field(default=None, max_length=300)
    agent_name: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    estimated_amount: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class RecalculateRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)
    actor: str = Field(min_length=1, max_length=200)


class BudgetResetRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)
    actor: str = Field(min_length=1, max_length=200)
