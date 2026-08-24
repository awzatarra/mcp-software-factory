from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SupervisorTarget(StrEnum):
    PLANNING = "planning"
    INSPECT_WORKSPACE = "inspect_workspace"
    IMPLEMENTATION = "implementation"
    TESTING_REPAIR = "testing_repair"
    GIT_WORKFLOW = "git_workflow"
    CI_PIPELINE = "ci_pipeline"
    GIT_PROMOTION = "git_promotion"
    FINALIZE = "finalize"


class SupervisorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target: SupervisorTarget
    reason: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
