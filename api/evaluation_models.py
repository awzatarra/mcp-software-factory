from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


Severity = Literal["info", "warning", "error", "critical"]
CoverageStatus = Literal["satisfied", "unsatisfied", "unknown"]
EvaluationState = Literal["not_started", "partial", "evaluated"]
EvaluationGrade = Literal["excellent", "good", "acceptable", "poor", "critical"]


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluationPenalty(EvaluationModel):
    code: str
    category: str
    points: int
    message: str
    severity: Severity
    related_task_id: str | None = None
    related_event_id: str | None = None
    related_file: str | None = None


class EvaluationBonus(EvaluationModel):
    code: str
    category: str
    points: int
    message: str
    related_task_id: str | None = None
    related_event_id: str | None = None
    related_file: str | None = None


class EvaluationPositiveSignal(EvaluationModel):
    code: str
    category: str
    message: str
    related_task_id: str | None = None
    related_event_id: str | None = None
    related_file: str | None = None


class EvaluationFinding(EvaluationModel):
    code: str
    category: str
    title: str
    description: str
    severity: Severity
    recommendation: str | None = None
    related_task_id: str | None = None
    related_event_id: str | None = None
    related_file: str | None = None


class RequirementCoverageItem(EvaluationModel):
    index: int
    text: str
    status: CoverageStatus
    evidence: list[str] = Field(default_factory=list)
    related_task_ids: list[str] = Field(default_factory=list)
    related_event_ids: list[str] = Field(default_factory=list)
    related_files: list[str] = Field(default_factory=list)


class RequirementCoverage(EvaluationModel):
    total: int
    satisfied: int
    unsatisfied: int
    unknown: int
    coverage_percent: float
    items: list[RequirementCoverageItem] = Field(default_factory=list)


class EvaluationCategory(EvaluationModel):
    score: int
    max_score: int
    percentage: float | None
    evaluation_state: EvaluationState = "evaluated"
    available_points: int = 0
    penalties: list[EvaluationPenalty] = Field(default_factory=list)
    bonuses: list[EvaluationBonus] = Field(default_factory=list)
    positive_signals: list[EvaluationPositiveSignal] = Field(default_factory=list)
    findings: list[EvaluationFinding] = Field(default_factory=list)


class StageDuration(EvaluationModel):
    elapsed_seconds: float | None = None
    active_seconds: float | None = None
    waiting_seconds: float | None = None


class WorkflowDurationBreakdown(EvaluationModel):
    wall_clock_duration_seconds: float | None = None
    active_execution_seconds: float | None = None
    total_duration_seconds: float | None = None
    planning_seconds: float | None = None
    implementation_seconds: float | None = None
    testing_seconds: float | None = None
    repair_seconds: float | None = None
    approval_wait_seconds: float | None = None
    finalize_seconds: float | None = None
    planning: StageDuration = Field(default_factory=StageDuration)
    implementation: StageDuration = Field(default_factory=StageDuration)
    testing: StageDuration = Field(default_factory=StageDuration)
    repair: StageDuration = Field(default_factory=StageDuration)
    finalize: StageDuration = Field(default_factory=StageDuration)


class WorkflowEvaluationResponse(EvaluationModel):
    thread_id: str
    branch_id: str
    lineage: str
    inherited_from_branch: str | None = None
    origin_checkpoint: str | None = None
    terminal_status: str
    data_complete: bool
    evaluation_status: Literal["partial", "final"]
    overall_score: int
    max_score: int
    percentage: float
    earned_points: int = 0
    available_points: int = 0
    provisional_percentage: float = 0
    projected_max_score: int = 100
    is_provisional: bool = False
    final_grade_available: bool = True
    grade: EvaluationGrade | None
    provisional_grade: EvaluationGrade | None = None
    planning: EvaluationCategory
    implementation: EvaluationCategory
    testing: EvaluationCategory
    efficiency: EvaluationCategory
    reliability: EvaluationCategory
    requirement_coverage: RequirementCoverage
    acceptance_coverage: RequirementCoverage
    duration: WorkflowDurationBreakdown
    penalties: list[EvaluationPenalty] = Field(default_factory=list)
    bonuses: list[EvaluationBonus] = Field(default_factory=list)
    positive_signals: list[EvaluationPositiveSignal] = Field(default_factory=list)
    findings: list[EvaluationFinding] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    calculated_at: datetime
    source_updated_at: datetime | None = None
    scoring_version: str
    evidence: dict[str, Any] | None = None
