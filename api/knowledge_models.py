from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeType(StrEnum):
    ARCHITECTURE = "architecture"
    DECISION = "decision"
    CONVENTION = "convention"
    CODE_PATTERN = "code_pattern"
    TEST_PATTERN = "test_pattern"
    INCIDENT = "incident"
    SOLUTION = "solution"
    WORKFLOW_LEARNING = "workflow_learning"
    DOCUMENTATION = "documentation"


class KnowledgeStatus(StrEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    REJECTED = "rejected"
    INDEXED = "indexed"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmitLearningRequest(StrictModel):
    project_id: str = Field(min_length=1, max_length=200)
    workflow_id: str | None = Field(default=None, max_length=200)
    agent_name: str = Field(min_length=1, max_length=100)
    knowledge_type: KnowledgeType
    content: str = Field(min_length=1)
    source_reference: str = Field(min_length=1, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RejectLearningRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)

