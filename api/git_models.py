from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GitModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GitRepositoryInfo(GitModel):
    state: Literal["available", "not_repository"]
    is_repository: bool
    inherited_parent_repository: bool = False
    repository_root: str | None = None
    current_branch: str | None = None
    head_commit: str | None = None
    detached_head: bool = False
    clean: bool | None = None
    effective_clean: bool | None = None
    base_branch: str | None = None
    base_commit: str | None = None
    workflow_branch: str | None = None
    commits: list[dict[str, Any]] = Field(default_factory=list)
    commit_preview: dict[str, Any] | None = None
    commit_status: str | None = None
    promotion: dict[str, Any] | None = None
    ci_eligibility: dict[str, Any] | None = None


class GitStatusResponse(GitModel):
    branch: str | None
    clean: bool
    effective_clean: bool | None = None
    staged: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)


class GitDiffFile(GitModel):
    path: str
    status: Literal["added", "modified", "deleted", "renamed", "unknown"]
    additions: int = 0
    deletions: int = 0
    patch: str = ""


class GitDiffResponse(GitModel):
    staged: bool
    files: list[GitDiffFile] = Field(default_factory=list)
    truncated: bool = False
    total_bytes: int = 0


class GitLogEntry(GitModel):
    commit: str
    short_commit: str
    author_name: str
    author_email: str
    timestamp: datetime
    subject: str


class GitBranch(GitModel):
    name: str
    current: bool
    commit: str


class GitBranchesResponse(GitModel):
    current: str | None
    branches: list[GitBranch] = Field(default_factory=list)


class GitHeadResponse(GitModel):
    commit: str
    short_commit: str
    branch: str | None
    detached: bool


class GitWorkflowSummary(GitModel):
    state: Literal["available", "not_repository", "project_unavailable"]
    repository: bool = False
    branch: str | None = None
    head_commit: str | None = None
    clean: bool | None = None
    effective_clean: bool | None = None
    changed_files_count: int = 0
    base_branch: str | None = None
    base_commit: str | None = None
    workflow_branch: str | None = None
    commits: list[dict[str, Any]] = Field(default_factory=list)
    commit_preview: dict[str, Any] | None = None
    commit_status: str | None = None
    developer_commit: dict[str, Any] | None = None
    repair_commit: dict[str, Any] | None = None
    promotion: dict[str, Any] | None = None
    ci_eligibility: dict[str, Any] | None = None


class GitInitResponse(GitModel):
    initialized: bool
    repository: bool = True
    already_repository: bool = False
    branch: str | None = None
    head_commit: str | None = None


class GitWorkflowBranchResponse(GitModel):
    branch: str
    created: bool
    switched: bool
    base_branch: str | None = None
    base_commit: str | None = None


class GitStageResponse(GitModel):
    branch: str
    staged_files: list[str]
    staged_file_count: int


class GitCommitPreview(GitModel):
    approval_id: str
    branch: str
    staged_files: list[str]
    additions: int
    deletions: int
    diff_fingerprint: str
    proposed_message: str
    ready: bool
    status: Literal["awaiting_approval", "approved", "rejected", "committed"] = "awaiting_approval"


class GitCommitResult(GitModel):
    commit: str
    short_commit: str
    branch: str
    message: str
    files: list[str]
    created_at: datetime
    existing: bool = False


class GitPromotionPreview(GitModel):
    promotion_id: str
    approval_id: str | None = None
    state: str
    base_branch: str
    base_commit_at_branch_creation: str | None = None
    current_base_commit: str | None = None
    base_advanced: bool = False
    workflow_branch: str
    workflow_head: str
    commits_ahead: int = 0
    commits_behind: int = 0
    commits: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    conflict_state: Literal["clean", "conflicts"] = "clean"
    conflicting_files: list[str] = Field(default_factory=list)
    merge_strategy_candidate: Literal["fast_forward", "merge_commit", "blocked"]
    promotion_fingerprint: str
    ready: bool
    ci: dict[str, Any] | None = None


class GitPromotionResult(GitModel):
    promotion_id: str
    strategy: Literal["fast_forward", "merge_commit"]
    base_branch: str
    workflow_branch: str
    previous_base_commit: str | None = None
    workflow_head: str
    result_commit: str
    merged_commits: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    completed_at: datetime
    existing: bool = False


class GitPromotionStatus(GitModel):
    state: str = "not_started"
    promotion_id: str | None = None
    approval_id: str | None = None
    preview: GitPromotionPreview | None = None
    result: GitPromotionResult | None = None
    rejection_reason: str | None = None


class GitPendingOperationRecoveryResponse(GitModel):
    thread_id: str
    accepted: bool
    operation: Literal["git_commit", "git_merge"]
    tool_name: str
    status: Literal["completed", "running"]
    existing: bool = False
    result_commit: str | None = None


class GitStageRequest(GitModel):
    paths: list[str] = Field(min_length=1, max_length=500)
    actor: Literal["Developer", "Repair", "user"] = "Developer"


class GitCommitPrepareRequest(GitModel):
    message: str = Field(min_length=1, max_length=200)
    actor: Literal["Developer", "Repair", "user"] = "Developer"
