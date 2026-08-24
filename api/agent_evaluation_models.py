from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


EvaluationType = Literal["deterministic", "heuristic", "llm_judge", "comprehensive"]
EvaluationValidityStatus = Literal["valid", "superseded", "invalidated"]
Verdict = Literal["excellent", "good", "acceptable", "needs_improvement", "failed", "not_evaluated"]
JudgeVerdict = Literal["excellent", "good", "acceptable", "needs_improvement", "failed"]


class EvaluationApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluationRunCreate(EvaluationApiModel):
    workflow_id: str = Field(min_length=1, max_length=200)
    branch_id: str = Field(default="original", min_length=1, max_length=200)
    trace_id: str | None = Field(default=None, max_length=200)
    evaluation_type: EvaluationType = "comprehensive"
    rubric_id: str | None = None
    model: str | None = None
    force: bool = False


class RubricCreate(EvaluationApiModel):
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=40)
    agent_name: str = Field(min_length=1, max_length=80)
    dimensions: dict[str, float]
    weights: dict[str, float]
    thresholds: dict[str, float] = Field(default_factory=dict)
    enabled: bool = True


class RubricPatch(EvaluationApiModel):
    version: str | None = Field(default=None, min_length=1, max_length=40)
    dimensions: dict[str, float] | None = None
    weights: dict[str, float] | None = None
    thresholds: dict[str, float] | None = None
    enabled: bool | None = None


class RubricVersionCreate(EvaluationApiModel):
    version: str = Field(min_length=1, max_length=40)
    dimensions: dict[str, float]
    weights: dict[str, float]
    thresholds: dict[str, float] = Field(default_factory=dict)
    enabled: bool = True


class BaselineCreate(EvaluationApiModel):
    scope: dict[str, str] = Field(default_factory=dict)
    metric: str = Field(min_length=1, max_length=120)
    score: float = Field(ge=0)
    sample_count: int = Field(default=1, ge=1)


class EvaluationCompareRequest(EvaluationApiModel):
    evaluation_run_id: str
    baseline_id: str | None = None
    regression_threshold: float = Field(default=0.05, ge=0, le=1)


class EvaluationValidityPatch(EvaluationApiModel):
    validity_status: EvaluationValidityStatus
    superseded_by_run_id: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, min_length=1, max_length=1000)


class JudgeDimension(EvaluationApiModel):
    name: str = Field(min_length=1, max_length=120)
    score: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)


class JudgeOutput(EvaluationApiModel):
    agent_name: str = Field(min_length=1, max_length=80)
    score: float = Field(ge=0, le=1)
    verdict: JudgeVerdict
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=1000)
    dimensions: list[JudgeDimension] = Field(min_length=1, max_length=32)


class EvaluationRunResponse(EvaluationApiModel):
    evaluation_run_id: str
    workflow_id: str
    trace_id: str | None
    branch_id: str
    execution_id: str | None
    evaluation_type: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    duration_ms: float | None
    evaluator_version: str
    rubric_id: str | None = None
    rubric_version: str | None = None
    model: str | None = None
    overall_score: float | None = None
    verdict: str | None = None
    error: str | None = None
    validity_status: EvaluationValidityStatus = "valid"
    superseded_by_run_id: str | None = None
    invalidated_at: datetime | None = None
    invalidation_reason: str | None = None
    created_at: datetime
    results: list[dict[str, Any]] = Field(default_factory=list)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    dimension_scores: dict[str, Any] = Field(default_factory=dict)
    rubrics_used: list[dict[str, Any]] = Field(default_factory=list)
    rubric_binding_status: str | None = None
    rubric_binding_reason: str | None = None
