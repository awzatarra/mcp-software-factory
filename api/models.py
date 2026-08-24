from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from streaming.models import WorkflowEvent


class HttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateWorkflowRequest(HttpModel):
    request: str = Field(min_length=1, max_length=20_000)


class CreateWorkflowResponse(HttpModel):
    thread_id: str
    status: str
    workflow_url: str
    events_url: str


class ApprovalRequest(HttpModel):
    reason: str | None = Field(default=None, max_length=1_000)


class ApprovalResponse(HttpModel):
    thread_id: str
    accepted: bool
    operation: str
    tool_name: str
    status: str


class WorkflowSnapshotResponse(HttpModel):
    thread_id: str
    checkpoint_id: str | None
    project_name: str | None
    workflow_intent: str | None
    terminal_status: str
    interrupted: bool
    pending_operation: str | None
    pending_tool: str | None
    planning: dict[str, Any]
    planner_knowledge: dict[str, Any] = Field(
        default_factory=lambda: {
            "state": "not_started",
            "retrieval_id": None,
            "retrieval_used": False,
            "retrieved_context_count": 0,
            "context_tokens": 0,
            "sources": [],
        }
    )
    implementation: dict[str, Any]
    testing: dict[str, Any]
    agent_performance: dict[str, Any] = Field(default_factory=dict)
    failure_attribution: dict[str, Any] = Field(default_factory=dict)
    qa_knowledge: dict[str, Any] = Field(
        default_factory=lambda: {
            "state": "not_started",
            "retrieval_id": None,
            "retrieval_used": False,
            "retrieved_context_count": 0,
            "context_tokens": 0,
            "sources": [],
        }
    )
    repair_knowledge: dict[str, Any] = Field(
        default_factory=lambda: {
            "state": "not_started",
            "retrieval_id": None,
            "retrieval_used": False,
            "retrieved_context_count": 0,
            "context_tokens": 0,
            "sources": [],
        }
    )
    workflow_learning: dict[str, Any] = Field(
        default_factory=lambda: {
            "state": "not_started", "extracted_count": 0, "submitted_count": 0,
            "duplicate_count": 0, "rejected_count": 0, "candidates": [],
        }
    )
    supervisor: dict[str, Any]
    created_at: datetime | None
    updated_at: datetime | None
    observability_summary: dict[str, Any] = Field(
        default_factory=lambda: {"state": "not_available"}
    )
    llm_cost_summary: dict[str, Any] = Field(
        default_factory=lambda: {"state": "unavailable"}
    )
    git: dict[str, Any] = Field(
        default_factory=lambda: {
            "state": "project_unavailable",
            "repository": False,
            "branch": None,
            "head_commit": None,
            "clean": None,
            "changed_files_count": 0,
        }
    )


WorkflowListStatus = Literal["pending", "running", "completed", "failed", "waiting"]
WorkflowSortBy = Literal[
    "created_at",
    "updated_at",
    "project_name",
    "terminal_status",
]
WorkflowSortOrder = Literal["asc", "desc"]


class WorkflowListItem(HttpModel):
    thread_id: str
    project_name: str | None
    workflow_intent: str | None
    terminal_status: str
    interrupted: bool
    pending_operation: str | None
    pending_tool: str | None
    tests_executed: bool
    tests_passed: bool
    test_summary: str | None
    planning_attempts: int
    implementation_attempts: int
    repair_phase: str
    repair_attempts: int
    supervisor_decision: str | None
    created_at: datetime
    updated_at: datetime | None


class WorkflowListResponse(HttpModel):
    items: list[WorkflowListItem]
    total: int
    limit: int
    offset: int
    has_more: bool


class WorkflowProjectSummary(HttpModel):
    thread_id: str
    project_name: str | None
    project_exists: bool
    relative_project_path: str | None
    total_files: int
    total_directories: int
    total_size_bytes: int
    generated_files: list[str]
    updated_files: list[str]
    detected_framework: str | None
    detected_test_framework: str | None
    created_at: datetime | None
    updated_at: datetime | None


class ProjectFileNode(HttpModel):
    name: str
    path: str
    type: Literal["file", "directory"]
    size_bytes: int | None
    extension: str | None
    language: str | None
    content_type: Literal["text", "binary", "image", "unknown"] | None
    content_available: bool
    is_generated: bool
    is_updated: bool
    children: list["ProjectFileNode"] | None = None


class ProjectFileTreeResponse(HttpModel):
    thread_id: str
    project_name: str
    root: ProjectFileNode
    truncated: bool
    total_entries: int


class ProjectFileContentResponse(HttpModel):
    thread_id: str
    project_name: str
    path: str
    name: str
    extension: str | None
    language: str | None
    content_type: Literal["text"]
    encoding: str
    size_bytes: int
    content: str
    truncated: bool
    line_count: int
    is_generated: bool
    is_updated: bool


class WorkflowEventHistoryResponse(HttpModel):
    thread_id: str
    branch_id: str
    events: list[WorkflowEvent]
    last_sequence: int
    has_more: bool
