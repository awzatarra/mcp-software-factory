from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
from time import perf_counter
from typing import Any, TypeVar
from uuid import uuid4

from git_dirty_paths import classify_dirty_paths
from api.git_models import (
    GitBranch,
    GitBranchesResponse,
    GitDiffFile,
    GitDiffResponse,
    GitHeadResponse,
    GitLogEntry,
    GitRepositoryInfo,
    GitStatusResponse,
    GitWorkflowSummary,
    GitCommitPreview,
    GitCommitResult,
    GitPromotionPreview,
    GitPromotionResult,
    GitPromotionStatus,
    GitInitResponse,
    GitStageResponse,
    GitWorkflowBranchResponse,
)
from api.services.git_store import GitAuditStore, normalize_commit_id
from api.services.observability_context import (
    ObservabilityContext,
    get_observability_context,
    observability_context,
)


PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ALLOWED_COMMANDS = frozenset({
    "rev-parse", "status", "diff", "log", "branch", "show",
    "init", "switch", "add", "commit", "config", "merge-base",
    "rev-list", "merge-tree", "ls-tree", "--version",
})
T = TypeVar("T")
EMPTY_TREE_SHA1 = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
GIT_STDERR_SUMMARY_MAX_CHARS = 1_000
_GIT_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*)(?:bearer|basic)?\s*\S+"),
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(
        r"(?i)((?:token|password|passwd|secret|credential|api[_-]?key)\s*[=:]\s*)\S+"
    ),
    re.compile(r"(?i)\b(?:sk-[a-z0-9_-]{8,}|gh[opusr]_[a-z0-9_]{8,}|github_pat_[a-z0-9_]+|glpat-[a-z0-9_-]+)\b"),
    re.compile(r"(?i)\b(https?://)[^/\s:@]+:[^@\s/]+@"),
)


class GitError(RuntimeError):
    code = "git_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details) if details else None


class GitRepositoryNotFound(GitError):
    code = "git_repository_not_found"


class GitCommandFailed(GitError):
    code = "git_command_failed"


class GitCommandTimeout(GitError):
    code = "git_command_timeout"


class GitPathViolation(GitError):
    code = "git_path_violation"


class GitOutputLimitExceeded(GitError):
    code = "git_output_limit_exceeded"


class GitOperationNotAllowed(GitError):
    code = "git_operation_not_allowed"


class GitProtectedBranchViolation(GitError):
    code = "git_protected_branch_violation"


class GitApprovalRequired(GitError):
    code = "git_approval_required"


class GitApprovalStale(GitError):
    code = "git_approval_stale"


class GitIdentityMissing(GitError):
    code = "git_identity_missing"


class GitNothingStaged(GitError):
    code = "git_nothing_staged"


class GitTestsNotPassed(GitError):
    code = "git_tests_not_passed"


class GitWorkspaceDirtyConflict(GitError):
    code = "git_workspace_dirty_conflict"


class GitPromotionUnavailable(GitError):
    code = "git_promotion_unavailable"


class GitPromotionConflict(GitError):
    code = "git_promotion_conflicts"


class GitPromotionStale(GitError):
    code = "git_promotion_stale"


class GitCIRequiredForPromotion(GitError):
    code = "ci_required_for_promotion"


class GitCIPromotionBlocked(GitError):
    code = "ci_promotion_blocked"


class GitCICommitMismatch(GitError):
    code = "ci_commit_mismatch"


class GitPostApprovalRecoveryUnavailable(GitError):
    code = "git_post_approval_recovery_not_available"


@dataclass(frozen=True)
class GitPostApprovalRecovery:
    operation: str | None
    status: str
    approval_id: str | None = None
    project_id: str | None = None
    result_commit: str | None = None

    @property
    def recoverable(self) -> bool:
        return self.status == "recoverable"


def detect_git_post_approval_recovery(
    pending_operation: str | None,
    commit_state: dict[str, Any] | None,
    promotion_state: dict[str, Any] | None = None,
) -> GitPostApprovalRecovery:
    """Classify durable Git state without treating approval as repeatable."""
    commit = commit_state or {}
    promotion = promotion_state or {}
    if pending_operation == "git_merge":
        if promotion.get("status") == "completed" and promotion.get("result_json"):
            return GitPostApprovalRecovery(
                "git_merge", "completed", promotion.get("approval_id"),
                promotion.get("project_id"), promotion.get("result_commit"),
            )
        if (
            promotion.get("status") == "approved"
            and promotion.get("approval_id")
            and not promotion.get("result_json")
        ):
            return GitPostApprovalRecovery(
                "git_merge", "recoverable", promotion.get("approval_id"),
                promotion.get("project_id"), None,
            )
        return GitPostApprovalRecovery("git_merge", "not_recoverable")
    if pending_operation == "git_commit":
        if commit.get("commit_sha") and commit.get("approval_id"):
            return GitPostApprovalRecovery(
                "git_commit", "completed", commit.get("approval_id"),
                commit.get("project_id"), commit.get("commit_sha"),
            )
        if (
            commit.get("approval_status") == "approved"
            and commit.get("approval_id")
            and not commit.get("commit_sha")
        ):
            return GitPostApprovalRecovery(
                "git_commit", "recoverable", commit.get("approval_id"),
                commit.get("project_id"), None,
            )
        return GitPostApprovalRecovery("git_commit", "not_recoverable")
    if promotion.get("status") == "completed" and promotion.get("result_json"):
        return GitPostApprovalRecovery(
            "git_merge", "completed", promotion.get("approval_id"),
            promotion.get("project_id"), promotion.get("result_commit"),
        )
    if commit.get("commit_sha") and commit.get("approval_id"):
        return GitPostApprovalRecovery(
            "git_commit", "completed", commit.get("approval_id"),
            commit.get("project_id"), commit.get("commit_sha"),
        )
    return GitPostApprovalRecovery(None, "not_recoverable")


@dataclass(frozen=True)
class GitPolicy:
    command_timeout_seconds: float = 10.0
    output_max_bytes: int = 500_000
    diff_max_bytes: int = 200_000
    log_max_entries: int = 100
    commit_message_max_length: int = 200
    protected_branches: tuple[str, ...] = ("main", "master", "develop")
    promotion_allow_merge_commit: bool = True

    @classmethod
    def from_environment(cls) -> "GitPolicy":
        return cls(
            command_timeout_seconds=float(os.getenv("GIT_COMMAND_TIMEOUT_SECONDS", "10")),
            output_max_bytes=int(os.getenv("GIT_OUTPUT_MAX_BYTES", "500000")),
            diff_max_bytes=int(os.getenv("GIT_DIFF_MAX_BYTES", "200000")),
            log_max_entries=int(os.getenv("GIT_LOG_MAX_ENTRIES", "100")),
            commit_message_max_length=int(os.getenv("GIT_COMMIT_MESSAGE_MAX_LENGTH", "200")),
            protected_branches=tuple(
                item.strip() for item in os.getenv(
                    "GIT_PROTECTED_BRANCHES", "main,master,develop"
                ).split(",") if item.strip()
            ),
            promotion_allow_merge_commit=os.getenv(
                "GIT_PROMOTION_ALLOW_MERGE_COMMIT", "true"
            ).casefold() == "true",
        )


def validate_project_id(project_id: str) -> str:
    value = str(project_id or "").strip()
    if not PROJECT_ID_RE.fullmatch(value):
        raise GitPathViolation("Invalid project reference.")
    return value


def validate_repository_relative_path(path: str) -> str:
    raw = str(path or "").replace("\\", "/").strip()
    windows = PureWindowsPath(raw)
    if not raw or raw.startswith(("/", "//")) or windows.is_absolute() or windows.drive:
        raise GitPathViolation("Absolute paths are not allowed.")
    parts = PurePosixPath(raw).parts
    if ".." in parts or any(part.casefold() == ".git" for part in parts):
        raise GitPathViolation("Path is outside the authorized project.")
    return PurePosixPath(*parts).as_posix()


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def summarize_git_stderr(
    data: bytes, *, limit: int = GIT_STDERR_SUMMARY_MAX_CHARS,
) -> str:
    """Return bounded Git diagnostics without retaining credential-like values."""
    if limit <= 0:
        return ""
    value = _decode(data).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(character for character in value if character in "\n\t" or ord(character) >= 32)
    for pattern in _GIT_SECRET_PATTERNS:
        if pattern.groups:
            value = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
        else:
            value = pattern.sub("[REDACTED]", value)
    value = value.strip()
    suffix = "...[TRUNCATED]"
    if len(value) > limit:
        return value[: max(0, limit - len(suffix))] + suffix
    return value


class GitService:
    def __init__(
        self,
        workspace_root: Path,
        *,
        audit_store: GitAuditStore | None = None,
        observability: Any | None = None,
        policy: GitPolicy | None = None,
        ci_service: Any | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.audit_store = audit_store
        self.observability = observability
        self.policy = policy or GitPolicy.from_environment()
        self.ci_service = ci_service
        self._approval_locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        if self.audit_store is not None:
            await self.audit_store.initialize()

    async def _project_root(
        self, project_id: str, workflow_id: str | None, *, allow_initial_bind: bool = False,
    ) -> Path:
        project_id = validate_project_id(project_id)
        if workflow_id and self.audit_store is not None:
            authorized = await self.audit_store.resolve_workflow_project(workflow_id)
            if authorized is None and allow_initial_bind:
                bound = await self.audit_store.bind_workflow_project(workflow_id, project_id)
                authorized = project_id if bound else None
            if authorized is None or authorized != project_id:
                raise GitPathViolation("Workflow is not authorized for this project.")
        root = (self.workspace_root / project_id).resolve()
        try:
            root.relative_to(self.workspace_root)
        except ValueError as exc:
            raise GitPathViolation("Project is outside the workspace.") from exc
        if not root.is_dir():
            raise GitRepositoryNotFound("Project does not exist.")
        return root

    @staticmethod
    def _validate_command(arguments: list[str]) -> None:
        if not arguments or arguments[0] not in ALLOWED_COMMANDS:
            raise GitOperationNotAllowed("Git operation is not allowed.")
        operation, tail = arguments[0], arguments[1:]
        if operation == "--version" and not tail:
            return
        if operation == "--version":
            raise GitOperationNotAllowed("Git version arguments are not allowed.")
        forbidden = {"--amend", "--all", "-A", "-a", "--force", "-f", "--hard"}
        if any(item in forbidden or item.startswith("--output") for item in tail):
            raise GitOperationNotAllowed("Git option is not allowed.")
        if operation == "init" and tail:
            raise GitOperationNotAllowed("Git init arguments are not allowed.")
        if operation == "add":
            if not tail or tail[0] != "--" or not tail[1:]:
                raise GitOperationNotAllowed("Git staging requires explicit paths.")
            if any(item in {".", "*"} or item.startswith("-") for item in tail[1:]):
                raise GitOperationNotAllowed("Git staging path is not allowed.")
        if operation == "commit" and (
            len(tail) != 2 or tail[0] != "-m" or tail[1].startswith("-")
        ):
            raise GitOperationNotAllowed("Git commit arguments are not allowed.")
        if operation == "switch":
            branch = tail[1] if len(tail) >= 2 and tail[0] == "-c" else (tail[0] if tail else "")
            valid_start = len(tail) != 3 or bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", tail[2]))
            if (
                len(tail) not in {1, 2, 3}
                or (len(tail) >= 2 and tail[0] != "-c")
                or not branch.startswith("workflow/")
                or not valid_start
            ):
                raise GitOperationNotAllowed("Only workflow branches may be switched.")
        if operation == "config" and tail not in (["--get", "user.name"], ["--get", "user.email"]):
            raise GitOperationNotAllowed("Only Git identity reads are allowed.")
        if operation == "branch" and tail:
            valid = (
                tail == ["--show-current"]
                or (len(tail) == 1 and tail[0].startswith("--format="))
                or (len(tail) == 2 and tail[0] == "--list" and tail[1].startswith("workflow/"))
            )
            if not valid:
                raise GitOperationNotAllowed("Only local branch reads are allowed.")
        if operation == "diff" and "--no-index" in tail:
            raise GitOperationNotAllowed("External path diffs are not allowed.")
        if "--" in tail:
            path_index = tail.index("--") + 1
            for path in tail[path_index:]:
                validate_repository_relative_path(path)

    def _run(self, root: Path, arguments: list[str], *, allow_failure: bool = False) -> str:
        result = self._run_result(root, arguments)
        if result.returncode and not allow_failure:
            raise GitCommandFailed(
                "Git command failed.",
                details={
                    "command_operation": arguments[0],
                    "returncode": int(result.returncode),
                    "stderr_summary": summarize_git_stderr(result.stderr),
                },
            )
        return _decode(result.stdout)

    def _run_result(self, root: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        self._validate_command(arguments)
        command = ["git", *arguments]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                command,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creationflags,
                timeout=self.policy.command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCommandTimeout("Git command timed out.") from exc
        output_size = len(result.stdout) + len(result.stderr)
        if output_size > self.policy.output_max_bytes:
            raise GitOutputLimitExceeded("Git output exceeded the configured limit.")
        return result

    def _run_promotion_command(
        self, root: Path, arguments: list[str], *, base_branch: str,
        workflow_branch: str,
    ) -> str:
        valid = (
            arguments == ["switch", base_branch]
            or arguments == ["switch", workflow_branch]
            or arguments == ["switch", "-c", base_branch, workflow_branch]
            or arguments == ["merge", "--ff-only", workflow_branch]
            or arguments == ["merge", "--abort"]
            or (
                len(arguments) == 5
                and arguments[:2] == ["merge", "--no-ff"]
                and arguments[2] == workflow_branch
                and arguments[3] == "-m"
                and arguments[4].startswith("merge: promote workflow ")
            )
        )
        if not valid or not base_branch or base_branch == workflow_branch:
            raise GitOperationNotAllowed("Promotion command is not allowed.")
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(
                ["git", *arguments], cwd=root, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, creationflags=creationflags,
                timeout=self.policy.command_timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCommandTimeout("Git promotion command timed out.") from exc
        if len(result.stdout) + len(result.stderr) > self.policy.output_max_bytes:
            raise GitOutputLimitExceeded("Git output exceeded the configured limit.")
        if result.returncode:
            raise GitCommandFailed("Git promotion command failed.")
        return _decode(result.stdout)

    async def _read(
        self,
        operation: str,
        project_id: str,
        action: Callable[[Path], Awaitable[T]],
        *,
        workflow_id: str | None,
        agent_name: str | None,
        audit_details: dict[str, Any] | None = None,
        allow_initial_bind: bool = False,
    ) -> T:
        started = perf_counter()
        success = False
        failure: GitError | None = None
        root: Path | None = None
        audit_project_id: str | None = None
        try:
            audit_project_id = validate_project_id(project_id)
            root = await self._project_root(
                project_id, workflow_id, allow_initial_bind=allow_initial_bind,
            )
            if self.observability is not None:
                current = get_observability_context()
                if current.trace_id and audit_details is not None:
                    audit_details.setdefault("trace_id", current.trace_id)
                synthetic_workflow: str | None = None
                if current.trace_id:
                    context = current
                else:
                    trace = (
                        await self.observability.store.find_trace(workflow_id)
                        if workflow_id
                        else None
                    )
                    if trace:
                        context = ObservabilityContext(
                            trace_id=trace["trace_id"],
                            span_id=trace.get("root_span_id"),
                            workflow_id=workflow_id,
                            branch_id=str(trace.get("branch_id") or "original"),
                        )
                    else:
                        synthetic_workflow = workflow_id or f"git-read-{uuid4().hex}"
                        context = await self.observability.ensure_trace(
                            synthetic_workflow,
                            source="git_mcp",
                        )
                observed_success = False
                try:
                    with observability_context(context):
                        async with self.observability.span(
                            f"git.{operation}",
                            category="git",
                            kind="internal",
                            agent=agent_name,
                            attributes={
                                "workflow_id": workflow_id,
                                "project_id": project_id,
                                "agent_name": agent_name,
                                "operation": operation,
                                "provider": "local",
                                "cost": 0,
                            },
                        ):
                            value = await action(root)
                    observed_success = True
                finally:
                    if synthetic_workflow and workflow_id is None:
                        await self.observability.finalize_workflow_trace(
                            synthetic_workflow,
                            "completed" if observed_success else "failed",
                            reason="git_mcp_read_completed" if observed_success else "git_mcp_read_failed",
                        )
            else:
                value = await action(root)
            success = True
            return value
        except GitError as exc:
            failure = exc
            if exc.details:
                if audit_details is None:
                    audit_details = {}
                for key in ("command_operation", "returncode", "stderr_summary"):
                    if key in exc.details:
                        audit_details[key] = exc.details[key]
            raise
        finally:
            duration = (perf_counter() - started) * 1000
            if self.audit_store is not None and audit_project_id is not None:
                await self.audit_store.record(
                    {
                        "workflow_id": workflow_id,
                        "project_id": audit_project_id,
                        "agent_name": agent_name,
                        "operation": operation,
                        "repository_ref": project_id,
                        "success": success,
                        "duration_ms": duration,
                        "error_code": failure.code if failure else None,
                        **(audit_details or {}),
                    }
                )

    async def _repository_root(self, project_root: Path) -> Path:
        raw = await asyncio.to_thread(self._run, project_root, ["rev-parse", "--show-toplevel"])
        repository_root = Path(raw.strip()).resolve()
        if repository_root != project_root:
            raise GitRepositoryNotFound("Project is not an isolated Git repository.")
        return repository_root

    async def _detected_repository_root(self, project_root: Path) -> Path | None:
        raw = await asyncio.to_thread(
            self._run, project_root, ["rev-parse", "--show-toplevel"], allow_failure=True
        )
        detected = raw.strip()
        return Path(detected).resolve() if detected else None

    async def get_repository_info(
        self, project_id: str, *, workflow_id: str | None = None,
        agent_name: str | None = None, allow_initial_bind: bool = False,
    ) -> GitRepositoryInfo:
        async def action(root: Path) -> GitRepositoryInfo:
            detected_root = await self._detected_repository_root(root)
            if detected_root != root:
                return GitRepositoryInfo(
                    state="not_repository",
                    is_repository=False,
                    inherited_parent_repository=detected_root is not None,
                )
            try:
                head = await self._head(root)
                current_branch = head.branch
                head_commit = head.commit
                detached_head = head.detached
            except GitCommandFailed:
                current_branch = await self._current_branch(root)
                head_commit = None
                detached_head = False
            status = await self._status(root)
            return GitRepositoryInfo(
                state="available",
                is_repository=True,
                repository_root=project_id,
                current_branch=current_branch,
                head_commit=head_commit,
                detached_head=detached_head,
                clean=status.clean,
                effective_clean=self._effective_clean(root, status),
            )
        return await self._read(
            "repository_info", project_id, action, workflow_id=workflow_id,
            agent_name=agent_name, allow_initial_bind=allow_initial_bind,
        )

    async def _ensure_repository(self, root: Path) -> None:
        await self._repository_root(root)

    async def _status(self, root: Path) -> GitStatusResponse:
        await self._ensure_repository(root)
        output = await asyncio.to_thread(
            self._run, root, ["status", "--porcelain=v1", "--branch", "-z"]
        )
        entries = output.split("\0")
        branch: str | None = None
        staged: set[str] = set()
        modified: set[str] = set()
        untracked: set[str] = set()
        deleted: set[str] = set()
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            if entry.startswith("## "):
                value = entry[3:].split("...", 1)[0]
                branch = None if value.startswith("HEAD ") else value
                continue
            code = entry[:2]
            path = entry[3:].split(" -> ")[-1].replace("\\", "/")
            if code[0] in {"R", "C"} or code[1] in {"R", "C"}:
                # Porcelain v1 -z emits the original path as a second record.
                index += 1
            if code == "??":
                untracked.add(path)
                continue
            if code[0] not in {" ", "?"}:
                staged.add(path)
            if "D" in code:
                deleted.add(path)
            elif code[1] not in {" ", "?"}:
                modified.add(path)
        return GitStatusResponse(
            branch=branch,
            clean=not (staged or modified or untracked or deleted),
            staged=sorted(staged), modified=sorted(modified),
            untracked=sorted(untracked), deleted=sorted(deleted),
        )

    @staticmethod
    def _effective_clean(root: Path, status: GitStatusResponse) -> bool:
        dirty = classify_dirty_paths(root, status, [])
        return not dirty.allowed_files and not dirty.unrelated_files

    async def git_status(self, project_id: str, *, workflow_id: str | None = None,
                         agent_name: str | None = None) -> GitStatusResponse:
        async def action(root: Path) -> GitStatusResponse:
            status = await self._status(root)
            return status.model_copy(update={"effective_clean": self._effective_clean(root, status)})

        return await self._read("status", project_id, action, workflow_id=workflow_id, agent_name=agent_name)

    @staticmethod
    def _diff_status(code: str) -> str:
        return {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}.get(code[:1], "unknown")

    async def _diff(self, root: Path, staged: bool) -> GitDiffResponse:
        await self._ensure_repository(root)
        base = ["diff", "--no-ext-diff"] + (["--cached"] if staged else [])
        names, stats, patch = await asyncio.gather(
            asyncio.to_thread(self._run, root, [*base, "--name-status"]),
            asyncio.to_thread(self._run, root, [*base, "--numstat"]),
            asyncio.to_thread(self._run, root, [*base, "--unified=3"]),
        )
        status_by_path: dict[str, str] = {}
        for line in names.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                status_by_path[parts[-1]] = self._diff_status(parts[0])
        counts: dict[str, tuple[int, int]] = {}
        for line in stats.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                counts[parts[-1]] = (
                    int(parts[0]) if parts[0].isdigit() else 0,
                    int(parts[1]) if parts[1].isdigit() else 0,
                )
        patches: dict[str, str] = {}
        current: str | None = None
        chunks: list[str] = []
        for line in patch.splitlines(keepends=True):
            if line.startswith("diff --git "):
                if current is not None:
                    patches[current] = "".join(chunks)
                match = re.match(r"diff --git a/(.+?) b/(.+)$", line.rstrip("\n"))
                current = match.group(2) if match else None
                chunks = [line]
            elif current is not None:
                chunks.append(line)
        if current is not None:
            patches[current] = "".join(chunks)
        total_bytes = len(patch.encode("utf-8"))
        remaining = self.policy.diff_max_bytes
        truncated = total_bytes > remaining
        files: list[GitDiffFile] = []
        for path in sorted(set(status_by_path) | set(counts) | set(patches)):
            raw_patch = patches.get(path, "")
            encoded = raw_patch.encode("utf-8")
            if len(encoded) > remaining:
                raw_patch = encoded[: max(remaining, 0)].decode("utf-8", errors="ignore")
            remaining = max(0, remaining - len(raw_patch.encode("utf-8")))
            additions, deletions = counts.get(path, (0, 0))
            files.append(GitDiffFile(
                path=path,
                status=status_by_path.get(path, "unknown"),
                additions=additions,
                deletions=deletions,
                patch=raw_patch,
            ))
        return GitDiffResponse(staged=staged, files=files, truncated=truncated, total_bytes=total_bytes)

    async def git_diff(self, project_id: str, *, staged: bool = False,
                       workflow_id: str | None = None, agent_name: str | None = None) -> GitDiffResponse:
        return await self._read("diff", project_id, lambda root: self._diff(root, staged), workflow_id=workflow_id, agent_name=agent_name)

    async def _log(self, root: Path, limit: int, path: str | None = None) -> list[GitLogEntry]:
        await self._ensure_repository(root)
        safe_limit = max(1, min(int(limit), self.policy.log_max_entries))
        arguments = ["log", f"-{safe_limit}", "--format=%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s%x1e"]
        if path is not None:
            arguments.extend(["--", validate_repository_relative_path(path)])
        output = await asyncio.to_thread(self._run, root, arguments)
        entries: list[GitLogEntry] = []
        for record in output.split("\x1e"):
            fields = record.strip().split("\x1f")
            if len(fields) == 6:
                entries.append(GitLogEntry(
                    commit=fields[0], short_commit=fields[1], author_name=fields[2],
                    author_email=fields[3], timestamp=datetime.fromisoformat(fields[4]),
                    subject=fields[5],
                ))
        return entries

    async def git_log(self, project_id: str, *, limit: int = 20,
                      workflow_id: str | None = None, agent_name: str | None = None) -> list[GitLogEntry]:
        return await self._read("log", project_id, lambda root: self._log(root, limit), workflow_id=workflow_id, agent_name=agent_name)

    async def git_file_history(self, project_id: str, path: str, *, limit: int = 20,
                               workflow_id: str | None = None, agent_name: str | None = None) -> list[GitLogEntry]:
        safe_path = validate_repository_relative_path(path)
        return await self._read("file_history", project_id, lambda root: self._log(root, limit, safe_path), workflow_id=workflow_id, agent_name=agent_name)

    async def _head(self, root: Path) -> GitHeadResponse:
        await self._ensure_repository(root)
        commit, short_commit, branch = await asyncio.gather(
            asyncio.to_thread(self._run, root, ["rev-parse", "HEAD"]),
            asyncio.to_thread(self._run, root, ["rev-parse", "--short", "HEAD"]),
            asyncio.to_thread(self._run, root, ["rev-parse", "--abbrev-ref", "HEAD"]),
        )
        branch_name = branch.strip()
        detached = branch_name == "HEAD"
        return GitHeadResponse(
            commit=commit.strip(), short_commit=short_commit.strip(),
            branch=None if detached else branch_name, detached=detached,
        )

    async def git_head(self, project_id: str, *, workflow_id: str | None = None,
                       agent_name: str | None = None) -> GitHeadResponse:
        return await self._read("head", project_id, self._head, workflow_id=workflow_id, agent_name=agent_name)

    async def _branches(self, root: Path) -> GitBranchesResponse:
        head = await self._head(root)
        output = await asyncio.to_thread(
            self._run,
            root,
            ["branch", "--format=%(refname:short)\t%(objectname)"],
        )
        branches: list[GitBranch] = []
        for line in output.splitlines():
            if "\t" not in line:
                continue
            name, commit = line.split("\t", 1)
            branches.append(GitBranch(name=name, current=name == head.branch, commit=commit))
        return GitBranchesResponse(current=head.branch, branches=branches)

    async def git_branches(self, project_id: str, *, workflow_id: str | None = None,
                           agent_name: str | None = None) -> GitBranchesResponse:
        return await self._read("branches", project_id, self._branches, workflow_id=workflow_id, agent_name=agent_name)

    async def workflow_summary(self, project_id: str | None, workflow_id: str) -> GitWorkflowSummary:
        if not project_id:
            return GitWorkflowSummary(state="project_unavailable")
        info = await self.get_repository_info(project_id, workflow_id=workflow_id, agent_name="API")
        if not info.is_repository:
            return GitWorkflowSummary(state="not_repository")
        status = await self.git_status(project_id, workflow_id=workflow_id, agent_name="API")
        changed = set(status.staged + status.modified + status.untracked + status.deleted)
        durable = await self.audit_store.get_workflow_state(workflow_id) if self.audit_store else None
        commits: list[dict[str, Any]] = []
        if self.audit_store:
            operations = await self.audit_store.list(workflow_id=workflow_id)
            commits = [
                {
                    "commit": item["commit_sha"],
                    "message": item.get("commit_message"),
                    "created_at": item.get("created_at"),
                    "phase": item.get("phase"),
                    "agent": item.get("actor") or item.get("agent_name"),
                    "files": json.loads(item.get("files_json") or "[]"),
                    "base_commit": item.get("base_commit"),
                    "parent_commit": item.get("parent_commit"),
                    "approval_id": item.get("approval_id"),
                    "diff_fingerprint": item.get("diff_fingerprint"),
                    "trace_id": item.get("trace_id"),
                }
                for item in reversed(operations)
                if item.get("operation") == "git.commit" and item.get("success")
                and item.get("commit_sha")
            ]
        developer_commit = next((item for item in reversed(commits) if item.get("phase") == "implementation"), None)
        repair_commit = next((item for item in reversed(commits) if item.get("phase") == "repair"), None)
        promotion = await self.get_git_promotion(workflow_id) if self.audit_store else None
        commit_status = durable.get("approval_status") if durable else None
        commit_preview = (
            json.loads(durable["commit_preview_json"])
            if durable and durable.get("commit_preview_json") else None
        )
        if isinstance(commit_preview, dict) and commit_status == "committed":
            commit_preview = {**commit_preview, "ready": False, "status": "committed"}
        ci_eligibility = await self._ci_eligibility_for_commit(
            workflow_id,
            durable.get("commit_sha") if durable else None,
        )
        return GitWorkflowSummary(
            state="available", repository=True, branch=info.current_branch,
            head_commit=info.head_commit, clean=status.clean,
            effective_clean=status.effective_clean,
            changed_files_count=len(changed),
            base_branch=durable.get("base_branch") if durable else None,
            base_commit=durable.get("base_commit") if durable else None,
            workflow_branch=durable.get("workflow_branch") if durable else None,
            commits=commits,
            commit_preview=commit_preview,
            commit_status=commit_status,
            developer_commit=developer_commit, repair_commit=repair_commit,
            promotion=promotion.model_dump(mode="json") if promotion else None,
            ci_eligibility=ci_eligibility,
        )

    @staticmethod
    def workflow_branch_name(workflow_id: str) -> str:
        raw = str(workflow_id)
        compact = raw.replace("-", "")
        normalized = compact if re.fullmatch(r"[0-9a-fA-F]{32}", compact) else hashlib.sha256(raw.encode()).hexdigest()
        return f"workflow/{normalized[:8].lower()}"

    def _validate_commit_message(self, message: str) -> str:
        value = str(message or "").strip()
        if not value or len(value) > self.policy.commit_message_max_length:
            raise GitOperationNotAllowed("Commit message is invalid.")
        if value.startswith("-") or any(ord(character) < 32 for character in value):
            raise GitOperationNotAllowed("Commit message is invalid.")
        return value

    async def _current_branch(self, root: Path) -> str | None:
        output = await asyncio.to_thread(
            self._run, root, ["branch", "--show-current"], allow_failure=True
        )
        branch = output.strip()
        return None if not branch or branch == "HEAD" else branch

    async def _current_commit(self, root: Path) -> str | None:
        output = await asyncio.to_thread(
            self._run, root, ["rev-parse", "HEAD"], allow_failure=True
        )
        return normalize_commit_id(output)

    def _require_store(self) -> GitAuditStore:
        if self.audit_store is None:
            raise GitPathViolation("Durable workflow authorization is required.")
        return self.audit_store

    async def _ci_eligibility_for_commit(
        self,
        workflow_id: str,
        workflow_head: str | None,
    ) -> dict[str, Any] | None:
        if self.ci_service is None:
            return None
        eligibility = await self.ci_service.promotion_eligibility(workflow_id, workflow_head)
        return eligibility.model_dump(mode="json")

    async def ci_eligibility_for_workflow(
        self,
        workflow_id: str,
    ) -> dict[str, Any] | None:
        durable = await self.audit_store.get_workflow_state(workflow_id) if self.audit_store else None
        commit = durable.get("commit_sha") if durable else None
        return await self._ci_eligibility_for_commit(workflow_id, commit)

    @staticmethod
    def _assert_ci_promotion_eligible(ci: dict[str, Any] | None) -> None:
        if not ci or not ci.get("required"):
            return
        if ci.get("eligible"):
            return
        reason = str(ci.get("reason") or "ci_required_for_promotion")
        if reason == "ci_commit_mismatch":
            raise GitCICommitMismatch("CI run does not match the promoted commit.")
        if reason == "ci_required_for_promotion":
            raise GitCIRequiredForPromotion("A valid CI run is required before promotion.")
        raise GitCIPromotionBlocked("CI gates do not allow promotion.")

    async def git_init(
        self, project_id: str, *, workflow_id: str, agent_name: str | None = None,
    ) -> GitInitResponse:
        store = self._require_store()
        details: dict[str, Any] = {"operation": "git.init", "actor": agent_name}

        async def action(root: Path) -> GitInitResponse:
            detected_root = await self._detected_repository_root(root)
            if detected_root == root:
                branch = await self._current_branch(root)
                head = await self._current_commit(root)
                await store.upsert_workflow_state(workflow_id, project_id, {
                    "state": "ready", "head_commit": head,
                })
                return GitInitResponse(
                    initialized=False, already_repository=True, branch=branch, head_commit=head
                )
            await asyncio.to_thread(self._run, root, ["init"])
            await self._repository_root(root)
            branch = await self._current_branch(root)
            await store.upsert_workflow_state(workflow_id, project_id, {"state": "ready"})
            return GitInitResponse(initialized=True, branch=branch, head_commit=None)

        return await self._read(
            "init", project_id, action, workflow_id=workflow_id,
            agent_name=agent_name, audit_details=details, allow_initial_bind=True,
        )

    async def git_create_workflow_branch(
        self, project_id: str, *, workflow_id: str, agent_name: str | None = None,
        fork_origin_workflow: str | None = None, fork_origin_commit: str | None = None,
        allowed_dirty_paths: list[str] | None = None, branch_identity: str | None = None,
    ) -> GitWorkflowBranchResponse:
        store = self._require_store()
        branch_name = self.workflow_branch_name(branch_identity or workflow_id)
        if fork_origin_commit is not None and not re.fullmatch(r"[0-9a-fA-F]{40,64}", fork_origin_commit):
            raise GitOperationNotAllowed("Fork origin commit is invalid.")
        allowed_dirty = {
            validate_repository_relative_path(path) for path in (allowed_dirty_paths or [])
        }
        details: dict[str, Any] = {"operation": "git.branch.create", "branch": branch_name, "actor": agent_name}

        async def action(root: Path) -> GitWorkflowBranchResponse:
            await self._ensure_repository(root)
            status = await self._status(root)
            dirty = classify_dirty_paths(root, status, allowed_dirty)
            if dirty.unrelated_files:
                raise GitWorkspaceDirtyConflict(
                    "Existing working tree contains changes not attributed to this workflow."
                )
            durable = await store.get_workflow_state(workflow_id)
            base_branch = (durable or {}).get("base_branch") or await self._current_branch(root)
            if durable and durable.get("base_branch"):
                base_commit = normalize_commit_id(durable.get("base_commit"))
            else:
                base_commit = await self._current_commit(root)
            existing = await asyncio.to_thread(
                self._run, root, ["branch", "--list", branch_name]
            )
            current = await self._current_branch(root)
            created = not bool(existing.strip())
            switched = current != branch_name
            if switched:
                switch_arguments = ["switch", "-c", branch_name]
                if created and fork_origin_commit:
                    switch_arguments.append(fork_origin_commit)
                await asyncio.to_thread(
                    self._run, root, switch_arguments if created else ["switch", branch_name]
                )
            details["operation"] = "git.branch.create" if created else (
                "git.branch.switch" if switched else "git.branch.create"
            )
            await store.upsert_workflow_state(workflow_id, project_id, {
                "state": "ready", "base_branch": base_branch, "base_commit": base_commit,
                "workflow_branch": branch_name, "head_commit": await self._current_commit(root),
                "fork_origin_workflow": fork_origin_workflow,
                "fork_origin_commit": fork_origin_commit,
            })
            return GitWorkflowBranchResponse(
                branch=branch_name, created=created, switched=switched,
                base_branch=base_branch, base_commit=base_commit,
            )

        return await self._read(
            "branch.create", project_id, action, workflow_id=workflow_id,
            agent_name=agent_name, audit_details=details,
        )

    async def _require_workflow_branch(self, root: Path, workflow_id: str) -> str:
        branch = await self._current_branch(root)
        durable = await self._require_store().get_workflow_state(workflow_id)
        expected = durable.get("workflow_branch") if durable else self.workflow_branch_name(workflow_id)
        if branch in self.policy.protected_branches or branch != expected:
            raise GitProtectedBranchViolation("Mutation requires the workflow branch.")
        return branch

    async def git_stage(
        self, project_id: str, paths: list[str], *, workflow_id: str,
        agent_name: str | None = None,
    ) -> GitStageResponse:
        store = self._require_store()
        if not isinstance(paths, list) or not paths:
            raise GitPathViolation("At least one explicit path is required.")
        safe_paths = [validate_repository_relative_path(path) for path in paths]
        if len(set(safe_paths)) != len(safe_paths) or any(path in {".", "*"} for path in safe_paths):
            raise GitPathViolation("Staging paths must be explicit and unique.")
        details: dict[str, Any] = {"operation": "git.stage", "staged_file_count": len(safe_paths), "actor": agent_name}

        async def action(root: Path) -> GitStageResponse:
            await self._ensure_repository(root)
            branch = await self._require_workflow_branch(root, workflow_id)
            await asyncio.to_thread(self._run, root, ["add", "--", *safe_paths])
            status = await self._status(root)
            durable = await store.get_workflow_state(workflow_id)
            updates: dict[str, Any] = {
                "state": "changes_staged", "workflow_branch": branch,
                "staged_files_json": json.dumps(status.staged),
            }
            if not durable or durable.get("approval_status") != "approved":
                updates.update({
                    "commit_preview_json": None, "approval_id": None,
                    "approval_status": None,
                })
            await store.upsert_workflow_state(workflow_id, project_id, updates)
            details["branch"] = branch
            details["staged_file_count"] = len(status.staged)
            return GitStageResponse(
                branch=branch, staged_files=status.staged, staged_file_count=len(status.staged)
            )

        return await self._read(
            "stage", project_id, action, workflow_id=workflow_id,
            agent_name=agent_name, audit_details=details,
        )

    async def _staged_evidence(self, root: Path) -> tuple[list[str], int, int, str]:
        diff = await self._diff(root, True)
        if not diff.files:
            raise GitNothingStaged("No staged files are available for commit.")
        raw = await asyncio.to_thread(
            self._run, root, ["diff", "--cached", "--no-ext-diff", "--binary"]
        )
        return (
            [item.path for item in diff.files],
            sum(item.additions for item in diff.files),
            sum(item.deletions for item in diff.files),
            hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )

    async def prepare_git_commit(
        self, project_id: str, proposed_message: str, *, workflow_id: str,
        agent_name: str | None = None,
    ) -> GitCommitPreview:
        store = self._require_store()
        if await store.workflow_tests_passed(workflow_id) is False:
            raise GitTestsNotPassed("A Git commit cannot be prepared before tests pass.")
        message = self._validate_commit_message(proposed_message)
        details: dict[str, Any] = {"operation": "git.commit.prepare", "actor": agent_name}

        async def action(root: Path) -> GitCommitPreview:
            await self._ensure_repository(root)
            branch = await self._require_workflow_branch(root, workflow_id)
            files, additions, deletions, fingerprint = await self._staged_evidence(root)
            approval_id = uuid4().hex
            preview = GitCommitPreview(
                approval_id=approval_id, branch=branch, staged_files=files,
                additions=additions, deletions=deletions, diff_fingerprint=fingerprint,
                proposed_message=message, ready=True,
            )
            await store.upsert_workflow_state(workflow_id, project_id, {
                "state": "awaiting_approval", "workflow_branch": branch,
                "staged_files_json": json.dumps(files),
                "commit_preview_json": json.dumps(preview.model_dump(mode="json")),
                "approval_id": approval_id, "approval_status": "awaiting_approval",
                "commit_sha": None, "commit_message": None, "commit_created_at": None,
            })
            details.update({"branch": branch, "staged_file_count": len(files),
                            "diff_fingerprint": fingerprint, "approval_id": approval_id})
            return preview

        return await self._read(
            "commit.prepare", project_id, action, workflow_id=workflow_id,
            agent_name=agent_name, audit_details=details,
        )

    async def pending_commit_preview(self, workflow_id: str) -> dict[str, Any] | None:
        store = self._require_store()
        durable = await store.get_workflow_state(workflow_id)
        if not durable or durable.get("approval_status") != "awaiting_approval":
            return None
        raw = durable.get("commit_preview_json")
        return json.loads(raw) if raw else None

    async def detect_post_approval_recovery(
        self, workflow_id: str, pending_operation: str | None,
    ) -> GitPostApprovalRecovery:
        store = self._require_store()
        commit = await store.get_workflow_state(workflow_id)
        promotion = await store.get_promotion(workflow_id)
        return detect_git_post_approval_recovery(pending_operation, commit, promotion)

    async def recover_post_approval_operation(
        self,
        workflow_id: str,
        pending_operation: str | None,
        *,
        phase: str = "implementation",
        actor: str | None = None,
    ) -> tuple[GitPostApprovalRecovery, GitCommitResult | GitPromotionResult]:
        lock_key = workflow_id if pending_operation == "git_commit" else f"recovery:{workflow_id}"
        lock = self._approval_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            recovery = await self.detect_post_approval_recovery(
                workflow_id, pending_operation
            )
            if recovery.status not in {"recoverable", "completed"}:
                raise GitPostApprovalRecoveryUnavailable(
                    "No approved Git operation is available for recovery."
                )
            if not recovery.operation or not recovery.approval_id or not recovery.project_id:
                raise GitPostApprovalRecoveryUnavailable(
                    "Durable Git recovery evidence is incomplete."
                )
            if recovery.operation == "git_commit":
                result = await self.git_commit(
                    recovery.project_id,
                    approval_id=recovery.approval_id,
                    workflow_id=workflow_id,
                    agent_name=actor or ("Repair" if phase == "repair" else "Developer"),
                    phase=phase,
                )
            else:
                result = await self.merge_git_workflow_branch(
                    recovery.project_id,
                    workflow_id=workflow_id,
                    approval_id=recovery.approval_id,
                    actor=actor or "user",
                )
            return recovery, result

    async def resolve_commit_approval(
        self, workflow_id: str, *, approved: bool, reason: str | None = None,
        actor: str = "user",
    ) -> GitCommitResult | None:
        lock = self._approval_locks.setdefault(workflow_id, asyncio.Lock())
        async with lock:
            return await self._resolve_commit_approval_locked(
                workflow_id, approved=approved, reason=reason, actor=actor,
            )

    async def _resolve_commit_approval_locked(
        self, workflow_id: str, *, approved: bool, reason: str | None,
        actor: str,
    ) -> GitCommitResult | None:
        store = self._require_store()
        durable = await store.get_workflow_state(workflow_id)
        if not durable or durable.get("approval_status") != "awaiting_approval":
            raise GitApprovalRequired("No Git commit is awaiting approval.")
        project_id = str(durable["project_id"])
        preview = json.loads(str(durable["commit_preview_json"]))
        approval_id = str(durable["approval_id"])
        if not approved:
            await self.reject_git_commit(workflow_id, reason=reason, actor=actor)
            return None
        await self.approve_git_commit(workflow_id, actor=actor)
        return await self.git_commit(
            project_id, approval_id=approval_id, workflow_id=workflow_id, agent_name=actor
        )

    async def approve_git_commit(self, workflow_id: str, *, actor: str = "user") -> str:
        store = self._require_store()
        durable = await store.get_workflow_state(workflow_id)
        if not durable or durable.get("approval_status") != "awaiting_approval":
            raise GitApprovalRequired("No Git commit is awaiting approval.")
        project_id = str(durable["project_id"])
        preview = json.loads(str(durable["commit_preview_json"]))
        approval_id = str(durable["approval_id"])
        await store.upsert_workflow_state(workflow_id, project_id, {
            "state": "awaiting_approval", "approval_status": "approved",
        })
        await store.record({
            "workflow_id": workflow_id, "project_id": project_id, "agent_name": None,
            "operation": "git.commit.approve", "repository_ref": project_id,
            "success": True, "duration_ms": 0, "error_code": None,
            "branch": preview.get("branch"), "approval_id": approval_id, "actor": actor,
        })
        return approval_id

    async def reject_git_commit(
        self, workflow_id: str, *, reason: str | None = None, actor: str = "user",
    ) -> str:
        store = self._require_store()
        durable = await store.get_workflow_state(workflow_id)
        if not durable or durable.get("approval_status") != "awaiting_approval":
            raise GitApprovalRequired("No Git commit is awaiting approval.")
        project_id = str(durable["project_id"])
        preview = json.loads(str(durable["commit_preview_json"]))
        approval_id = str(durable["approval_id"])
        await store.upsert_workflow_state(workflow_id, project_id, {
            "state": "rejected", "approval_status": "rejected",
        })
        await store.record({
            "workflow_id": workflow_id, "project_id": project_id, "agent_name": None,
            "operation": "git.commit.reject", "repository_ref": project_id,
            "success": True, "duration_ms": 0, "error_code": None,
            "branch": preview.get("branch"), "approval_id": approval_id,
            "actor": actor, "commit_message": reason,
        })
        return approval_id

    async def git_commit(
        self, project_id: str, *, approval_id: str, workflow_id: str,
        agent_name: str | None = None, phase: str = "implementation",
    ) -> GitCommitResult:
        store = self._require_store()
        if await store.workflow_tests_passed(workflow_id) is False:
            raise GitTestsNotPassed("A Git commit cannot run before tests pass.")
        durable = await store.get_workflow_state(workflow_id)
        if durable and durable.get("approval_id") == approval_id and durable.get("commit_sha"):
            return GitCommitResult(
                commit=durable["commit_sha"], short_commit=str(durable["commit_sha"])[:7],
                branch=durable["workflow_branch"], message=durable["commit_message"],
                files=json.loads(durable.get("staged_files_json") or "[]"),
                created_at=durable["commit_created_at"], existing=True,
            )
        if durable and durable.get("approval_status") == "stale":
            raise GitApprovalStale("The Git commit approval is stale.")
        if not durable or durable.get("approval_status") != "approved" or durable.get("approval_id") != approval_id:
            raise GitApprovalRequired("A current approval is required for Git commit.")
        preview = json.loads(str(durable["commit_preview_json"]))
        message = self._validate_commit_message(str(preview["proposed_message"]))
        details = {
            "operation": "git.commit", "approval_id": approval_id,
            "actor": agent_name, "commit_message": message, "phase": phase,
            "base_commit": durable.get("base_commit"),
        }

        async def action(root: Path) -> GitCommitResult:
            await self._ensure_repository(root)
            branch = await self._require_workflow_branch(root, workflow_id)
            parent_commit = await self._current_commit(root)
            files, _additions, _deletions, fingerprint = await self._staged_evidence(root)
            if branch != preview["branch"] or files != preview["staged_files"] or fingerprint != preview["diff_fingerprint"]:
                await store.upsert_workflow_state(workflow_id, project_id, {
                    "state": "changes_staged", "approval_status": "stale",
                })
                raise GitApprovalStale("Staged changes no longer match the approved preview.")
            name = await asyncio.to_thread(
                self._run, root, ["config", "--get", "user.name"], allow_failure=True
            )
            email = await asyncio.to_thread(
                self._run, root, ["config", "--get", "user.email"], allow_failure=True
            )
            if not name.strip() or not email.strip():
                raise GitIdentityMissing("Git user.name and user.email are required.")
            await asyncio.to_thread(self._run, root, ["commit", "-m", message])
            commit = await self._current_commit(root)
            if not commit:
                raise GitCommandFailed("Git commit did not produce a commit SHA.")
            created_at = datetime.now().astimezone()
            clean = (await self._status(root)).clean
            committed_preview = {
                **preview,
                "ready": False,
                "status": "committed",
            }
            await store.upsert_workflow_state(workflow_id, project_id, {
                "state": "committed", "approval_status": "committed",
                "head_commit": commit, "commit_sha": commit, "commit_message": message,
                "commit_created_at": created_at.isoformat(),
                "commit_preview_json": json.dumps(committed_preview),
                "working_tree_clean": clean,
            })
            details.update({"branch": branch, "commit_sha": commit,
                            "staged_file_count": len(files), "diff_fingerprint": fingerprint,
                            "files_json": json.dumps(files), "parent_commit": parent_commit})
            return GitCommitResult(
                commit=commit, short_commit=commit[:7], branch=branch,
                message=message, files=files, created_at=created_at,
            )

        return await self._read(
            "commit", project_id, action, workflow_id=workflow_id,
            agent_name=agent_name, audit_details=details,
        )

    @staticmethod
    def _promotion_fingerprint(data: dict[str, Any]) -> str:
        material = {
            key: data.get(key)
            for key in (
                "workflow_id", "base_branch", "current_base_commit",
                "workflow_branch", "workflow_head", "commits",
                "files_changed", "merge_strategy_candidate", "ci",
            )
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    async def _ref_commit(self, root: Path, ref: str) -> str | None:
        result = await asyncio.to_thread(self._run_result, root, ["rev-parse", "--verify", ref])
        return normalize_commit_id(_decode(result.stdout)) if result.returncode == 0 else None

    async def _is_ancestor(self, root: Path, ancestor: str, descendant: str) -> bool:
        result = await asyncio.to_thread(
            self._run_result, root, ["merge-base", "--is-ancestor", ancestor, descendant]
        )
        return result.returncode == 0

    async def _has_merge_base(self, root: Path, first: str, second: str) -> bool:
        result = await asyncio.to_thread(self._run_result, root, ["merge-base", first, second])
        return result.returncode == 0

    async def _promotion_conflicts(
        self, root: Path, base_branch: str, workflow_branch: str,
    ) -> list[str]:
        async def check() -> list[str]:
            result = await asyncio.to_thread(
                self._run_result, root,
                ["merge-tree", "--write-tree", "--name-only", base_branch, workflow_branch],
            )
            if result.returncode == 0:
                return []
            output = _decode(result.stdout) + "\n" + _decode(result.stderr)
            conflicts = {
                match.group(1).strip()
                for match in re.finditer(r"CONFLICT .*? in (.+?)(?:\r?$)", output, re.MULTILINE)
            }
            if not conflicts:
                lines = [line.strip() for line in output.splitlines() if line.strip()]
                conflicts = {
                    line for line in lines[1:]
                    if not line.startswith(("Auto-merging", "CONFLICT", "warning:", "fatal:"))
                    and not re.fullmatch(r"[0-9a-fA-F]{40,64}", line)
                }
            return sorted(conflicts)

        if self.observability is None:
            return await check()
        async with self.observability.span(
            "git.promotion.conflict_check", category="git", kind="internal",
            attributes={"base_branch": base_branch, "workflow_branch": workflow_branch},
        ):
            return await check()

    async def _build_promotion_preview(
        self, root: Path, workflow_id: str, durable: dict[str, Any],
        *, promotion_id: str, approval_id: str | None,
        ci_evidence: dict[str, Any] | None = None,
    ) -> GitPromotionPreview:
        base_branch = str(durable.get("base_branch") or "")
        workflow_branch = str(durable.get("workflow_branch") or "")
        if not base_branch or not workflow_branch or base_branch == workflow_branch:
            raise GitPromotionUnavailable("Persisted Git branch provenance is unavailable.")
        workflow_head = await self._ref_commit(root, workflow_branch)
        if not workflow_head:
            raise GitPromotionUnavailable("Workflow branch does not have a commit.")
        current_base = await self._ref_commit(root, base_branch)
        base_at_creation = normalize_commit_id(durable.get("base_commit"))
        already_promoted = bool(
            current_base and await self._is_ancestor(root, workflow_head, current_base)
        )
        base_is_ancestor = bool(
            current_base and await self._is_ancestor(root, current_base, workflow_head)
        )
        has_merge_base = bool(
            current_base and await self._has_merge_base(root, current_base, workflow_head)
        )
        if current_base and has_merge_base:
            counts = await asyncio.to_thread(
                self._run, root,
                ["rev-list", "--left-right", "--count", f"{current_base}...{workflow_head}"],
            )
            behind, ahead = (int(value) for value in counts.split())
            commits_raw = await asyncio.to_thread(
                self._run, root, ["rev-list", "--reverse", f"{current_base}..{workflow_head}"],
            )
            files_raw = await asyncio.to_thread(
                self._run, root, ["diff", "--name-only", f"{current_base}...{workflow_head}"],
            )
            stats_raw = await asyncio.to_thread(
                self._run, root, ["diff", "--numstat", f"{current_base}...{workflow_head}"],
            )
        elif current_base:
            ahead_raw = await asyncio.to_thread(
                self._run, root, ["rev-list", "--count", workflow_head],
            )
            behind_raw = await asyncio.to_thread(
                self._run, root, ["rev-list", "--count", current_base],
            )
            ahead, behind = int(ahead_raw.strip() or "0"), int(behind_raw.strip() or "0")
            commits_raw = await asyncio.to_thread(
                self._run, root, ["rev-list", "--reverse", workflow_head],
            )
            files_raw = await asyncio.to_thread(
                self._run, root, ["diff", "--name-only", EMPTY_TREE_SHA1, workflow_head],
            )
            stats_raw = await asyncio.to_thread(
                self._run, root, ["diff", "--numstat", EMPTY_TREE_SHA1, workflow_head],
            )
        else:
            ahead_raw = await asyncio.to_thread(
                self._run, root, ["rev-list", "--count", workflow_head],
            )
            ahead, behind = int(ahead_raw.strip() or "0"), 0
            commits_raw = await asyncio.to_thread(
                self._run, root, ["rev-list", "--reverse", workflow_head],
            )
            files_raw = await asyncio.to_thread(
                self._run, root, ["diff", "--name-only", EMPTY_TREE_SHA1, workflow_head],
            )
            stats_raw = await asyncio.to_thread(
                self._run, root, ["diff", "--numstat", EMPTY_TREE_SHA1, workflow_head],
            )
        files = sorted(filter(None, files_raw.splitlines()))
        commits = list(filter(None, commits_raw.splitlines()))
        additions = deletions = 0
        for line in stats_raw.splitlines():
            parts = line.split("\t", 2)
            if len(parts) >= 2:
                additions += int(parts[0]) if parts[0].isdigit() else 0
                deletions += int(parts[1]) if parts[1].isdigit() else 0
        conflicts = []
        if current_base and not already_promoted and not base_is_ancestor:
            conflicts = await self._promotion_conflicts(root, base_branch, workflow_branch)
        strategy = (
            "fast_forward" if not current_base or base_is_ancestor
            else "blocked" if conflicts or not self.policy.promotion_allow_merge_commit
            else "merge_commit"
        )
        data = {
            "workflow_id": workflow_id, "base_branch": base_branch,
            "current_base_commit": current_base, "workflow_branch": workflow_branch,
            "workflow_head": workflow_head, "commits": commits,
            "files_changed": files, "merge_strategy_candidate": strategy,
        }
        ci = ci_evidence
        if ci is None:
            ci = await self._ci_eligibility_for_commit(workflow_id, workflow_head)
        self._assert_ci_promotion_eligible(ci)
        if ci:
            data["ci"] = {
                "run_id": ci.get("run_id"),
                "source_commit": ci.get("source_commit"),
                "decision": ci.get("ci_decision"),
                "pipeline_fingerprint": ci.get("pipeline_fingerprint"),
                "gate_policy_version": ci.get("gate_policy_version"),
            }
        fingerprint = self._promotion_fingerprint(data)
        state = (
            "already_promoted" if already_promoted else
            "conflicts" if conflicts else
            "unavailable" if strategy == "blocked" else
            "awaiting_approval"
        )
        return GitPromotionPreview(
            promotion_id=promotion_id, approval_id=approval_id, state=state,
            base_branch=base_branch, base_commit_at_branch_creation=base_at_creation,
            current_base_commit=current_base,
            base_advanced=base_at_creation != current_base,
            workflow_branch=workflow_branch, workflow_head=workflow_head,
            commits_ahead=ahead, commits_behind=behind, commits=commits,
            files_changed=files, additions=additions, deletions=deletions,
            conflict_state="conflicts" if conflicts else "clean",
            conflicting_files=conflicts, merge_strategy_candidate=strategy,
            promotion_fingerprint=fingerprint,
            ready=state == "awaiting_approval",
            ci=ci,
        )

    async def prepare_git_promotion(
        self, project_id: str, *, workflow_id: str, actor: str = "user",
    ) -> GitPromotionPreview:
        store = self._require_store()
        gate = await store.workflow_gate(workflow_id)
        orchestrated_ready = bool(
            actor == "Orchestrator" and gate
            and gate.get("terminal_status") in {None, "pending", "running"}
        )
        if not gate or (gate.get("terminal_status") != "completed" and not orchestrated_ready):
            raise GitPromotionUnavailable("Workflow must be completed before promotion.")
        if not bool(gate.get("tests_passed")):
            raise GitTestsNotPassed("Tests must pass before promotion.")
        durable = await store.get_workflow_state(workflow_id)
        if not durable or not durable.get("commit_sha"):
            raise GitPromotionUnavailable("A durable workflow commit is required.")
        promotion_id, approval_id = uuid4().hex, uuid4().hex
        details: dict[str, Any] = {"operation": "git.promotion.prepare", "actor": actor}

        async def action(root: Path) -> GitPromotionPreview:
            await self._ensure_promotion_worktree_clean(
                root, "Working tree must be clean for promotion."
            )
            preview = await self._build_promotion_preview(
                root, workflow_id, durable,
                promotion_id=promotion_id, approval_id=approval_id,
            )
            await store.save_promotion({
                "promotion_id": promotion_id, "workflow_id": workflow_id,
                "project_id": project_id, "base_branch": preview.base_branch,
                "workflow_branch": preview.workflow_branch,
                "base_commit": preview.base_commit_at_branch_creation,
                "current_base_commit": preview.current_base_commit,
                "workflow_head": preview.workflow_head,
                "strategy": preview.merge_strategy_candidate,
                "fingerprint": preview.promotion_fingerprint,
                "approval_id": approval_id if preview.ready else None,
                "actor": actor, "status": preview.state,
                "preview_json": preview.model_dump_json(),
                "conflict_count": len(preview.conflicting_files),
            })
            details.update({
                "branch": preview.workflow_branch,
                "approval_id": approval_id if preview.ready else None,
                "diff_fingerprint": preview.promotion_fingerprint,
            })
            return preview

        return await self._read(
            "promotion.prepare", project_id, action, workflow_id=workflow_id,
            agent_name="Orchestrator", audit_details=details,
        )

    async def _ensure_promotion_worktree_clean(self, root: Path, message: str) -> None:
        status = await self._status(root)
        dirty = classify_dirty_paths(root, status, [])
        if dirty.unrelated_files:
            raise GitWorkspaceDirtyConflict(message)

    async def get_git_promotion(self, workflow_id: str) -> GitPromotionStatus:
        record = await self._require_store().get_promotion(workflow_id)
        if not record:
            return GitPromotionStatus()
        preview = GitPromotionPreview.model_validate_json(record["preview_json"])
        result = (
            GitPromotionResult.model_validate_json(record["result_json"])
            if record.get("result_json") else None
        )
        return GitPromotionStatus(
            state=str(record["status"]), promotion_id=record["promotion_id"],
            approval_id=record.get("approval_id"), preview=preview, result=result,
            rejection_reason=record.get("rejection_reason"),
        )

    async def approve_git_promotion(self, workflow_id: str, *, actor: str = "user") -> str:
        store = self._require_store()
        record = await store.get_promotion(workflow_id)
        if not record or record.get("status") != "awaiting_approval":
            raise GitApprovalRequired("No Git promotion is awaiting approval.")
        record.update(status="approved", actor=actor)
        await store.save_promotion(record)
        return str(record["approval_id"])

    async def reject_git_promotion(
        self, workflow_id: str, *, reason: str | None = None, actor: str = "user",
    ) -> str:
        store = self._require_store()
        record = await store.get_promotion(workflow_id)
        if not record or record.get("status") != "awaiting_approval":
            raise GitApprovalRequired("No Git promotion is awaiting approval.")
        record.update(status="rejected", actor=actor, rejection_reason=reason)
        await store.save_promotion(record)
        return str(record["approval_id"])

    async def merge_git_workflow_branch(
        self, project_id: str, *, workflow_id: str, approval_id: str,
        actor: str = "user",
    ) -> GitPromotionResult:
        store = self._require_store()
        lock = self._approval_locks.setdefault(f"promotion:{workflow_id}", asyncio.Lock())
        async with lock:
            record = await store.get_promotion(workflow_id)
            if record and record.get("status") == "completed" and record.get("result_json"):
                return GitPromotionResult.model_validate_json(record["result_json"]).model_copy(
                    update={"existing": True}
                )
            if not record or record.get("status") != "approved" or record.get("approval_id") != approval_id:
                raise GitApprovalRequired("An approved current promotion is required.")
            durable = await store.get_workflow_state(workflow_id)
            if not durable:
                raise GitPromotionUnavailable("Git workflow state is unavailable.")
            details: dict[str, Any] = {
                "operation": "git.promotion.merge", "approval_id": approval_id,
                "actor": actor, "diff_fingerprint": record["fingerprint"],
            }

            async def action(root: Path) -> GitPromotionResult:
                await self._ensure_promotion_worktree_clean(
                    root, "Working tree changed before promotion."
                )
                approved_preview = GitPromotionPreview.model_validate_json(record["preview_json"])
                current = await self._build_promotion_preview(
                    root, workflow_id, durable,
                    promotion_id=str(record["promotion_id"]), approval_id=approval_id,
                    ci_evidence=approved_preview.ci,
                )
                if current.promotion_fingerprint != record["fingerprint"]:
                    record.update(status="stale")
                    await store.save_promotion(record)
                    raise GitPromotionStale("Promotion references changed after approval.")
                if not current.ready:
                    raise GitPromotionUnavailable("Promotion is no longer ready.")
                previous_base = current.current_base_commit
                if previous_base:
                    self._run_promotion_command(
                        root, ["switch", current.base_branch],
                        base_branch=current.base_branch,
                        workflow_branch=current.workflow_branch,
                    )
                else:
                    self._run_promotion_command(
                        root, ["switch", "-c", current.base_branch, current.workflow_branch],
                        base_branch=current.base_branch,
                        workflow_branch=current.workflow_branch,
                    )
                if previous_base:
                    arguments = (
                        ["merge", "--ff-only", current.workflow_branch]
                        if current.merge_strategy_candidate == "fast_forward"
                        else ["merge", "--no-ff", current.workflow_branch, "-m",
                              f"merge: promote workflow {workflow_id.replace('-', '')[:8]}"]
                    )
                    try:
                        self._run_promotion_command(
                            root, arguments, base_branch=current.base_branch,
                            workflow_branch=current.workflow_branch,
                        )
                    except GitCommandFailed:
                        merge_head = await self._ref_commit(root, "MERGE_HEAD")
                        if merge_head:
                            self._run_promotion_command(
                                root, ["merge", "--abort"],
                                base_branch=current.base_branch,
                                workflow_branch=current.workflow_branch,
                            )
                        self._run_promotion_command(
                            root, ["switch", current.workflow_branch],
                            base_branch=current.base_branch,
                            workflow_branch=current.workflow_branch,
                        )
                        record.update(status="failed")
                        await store.save_promotion(record)
                        raise
                result_commit = await self._current_commit(root)
                if not result_commit:
                    raise GitCommandFailed("Promotion did not produce a base commit.")
                completed_at = datetime.now().astimezone()
                result = GitPromotionResult(
                    promotion_id=str(record["promotion_id"]),
                    strategy=current.merge_strategy_candidate,
                    base_branch=current.base_branch,
                    workflow_branch=current.workflow_branch,
                    previous_base_commit=previous_base,
                    workflow_head=current.workflow_head,
                    result_commit=result_commit,
                    merged_commits=current.commits, files=current.files_changed,
                    completed_at=completed_at,
                )
                record.update(
                    status="completed", result_json=result.model_dump_json(),
                    result_commit=result_commit, completed_at=completed_at.isoformat(),
                    actor=actor,
                )
                await store.save_promotion(record)
                await store.upsert_workflow_state(workflow_id, project_id, {
                    "state": "promoted", "head_commit": result_commit,
                })
                details.update({
                    "branch": current.base_branch, "commit_sha": result_commit,
                    "base_commit": previous_base,
                })
                return result

            return await self._read(
                "promotion.merge", project_id, action, workflow_id=workflow_id,
                agent_name="Orchestrator", audit_details=details,
            )
