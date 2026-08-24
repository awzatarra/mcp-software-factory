from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from graph.subgraphs.planning.normalization import normalize_framework, normalize_project_type


class StrictPlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RequirementAnalysis(StrictPlanningModel):
    objective: str = Field(min_length=1, max_length=2000)
    project_name: str = Field(min_length=1, max_length=120)
    project_type: str = Field(min_length=1, max_length=50)
    framework: str | None = Field(default=None, min_length=1, max_length=50)
    functional_requirements: list[str] = Field(min_length=1, max_length=30)
    non_functional_requirements: list[str] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def normalize_supported_type_and_framework(self) -> "RequirementAnalysis":
        canonical_type = normalize_project_type(self.project_type)
        if canonical_type is not None:
            self.project_type = canonical_type
        canonical_framework = normalize_framework(self.framework, project_type=self.project_type)
        if canonical_framework is not None:
            self.framework = canonical_framework
        return self


class AcceptanceCriterion(StrictPlanningModel):
    id: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=1000)
    verification_method: str = Field(min_length=1, max_length=1000)


class ImplementationTask(StrictPlanningModel):
    order: int = Field(ge=1, le=100)
    role: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=2000)
    depends_on: list[int] = Field(default_factory=list, max_length=20)


class PlanningOutput(StrictPlanningModel):
    analysis: RequirementAnalysis
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1, max_length=50)
    tasks: list[ImplementationTask] = Field(min_length=1, max_length=100)
