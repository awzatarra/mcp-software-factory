from __future__ import annotations

import ast
import json
from contextlib import asynccontextmanager
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.git_models import (
    GitBranch,
    GitBranchesResponse,
    GitDiffFile,
    GitDiffResponse,
    GitHeadResponse,
    GitLogEntry,
    GitRepositoryInfo,
    GitStatusResponse,
)
from api.models import ApprovalResponse, WorkflowSnapshotResponse
from api.services.git_service import (
    GitApprovalRequired,
    GitApprovalStale,
    GitCommandFailed,
    GitCommandTimeout,
    GitOperationNotAllowed,
    GitPathViolation,
    GitPolicy,
    GitOutputLimitExceeded,
    GitIdentityMissing,
    GitProtectedBranchViolation,
    GitRepositoryNotFound,
    GitTestsNotPassed,
    GitWorkspaceDirtyConflict,
    GitPostApprovalRecovery,
    GitService,
    GIT_STDERR_SUMMARY_MAX_CHARS,
    detect_git_post_approval_recovery,
    validate_repository_relative_path,
)
from api.services.git_store import GitAuditStore
from api.services.observability_context import ObservabilityContext, observability_context
from clients.mcp_client import MCPClientTimeoutConfig, MCPServerClient
from servers.filesystem_server import get_workspace_root
from servers import git_server


SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


def run_git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, shell=False, check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    project = workspace / "project-a"
    project.mkdir(parents=True)
    run_git(project, "init")
    run_git(project, "config", "user.name", "Test User")
    run_git(project, "config", "user.email", "test@example.com")
    (project / "README.md").write_text("initial\n", encoding="utf-8")
    run_git(project, "add", "README.md")
    run_git(project, "commit", "-m", "Initial commit")
    return workspace, project


@pytest.fixture
def nested_parent_repository(tmp_path: Path) -> tuple[Path, Path, Path]:
    parent = tmp_path / "parent"
    workspace = parent / "workspace"
    project = workspace / "project-a"
    project.mkdir(parents=True)
    run_git(parent, "init")
    run_git(parent, "config", "user.name", "Parent User")
    run_git(parent, "config", "user.email", "parent@example.com")
    (parent / "PARENT.md").write_text("parent\n", encoding="utf-8")
    run_git(parent, "add", "PARENT.md")
    run_git(parent, "commit", "-m", "Parent commit")
    (project / "README.md").write_text("child\n", encoding="utf-8")
    return parent, workspace, project


@pytest.fixture
async def git_service(repository: tuple[Path, Path], tmp_path: Path) -> GitService:
    workspace, _ = repository
    store = GitAuditStore(tmp_path / "events.sqlite")
    service = GitService(workspace, audit_store=store)
    await service.initialize()
    return service


@pytest.mark.asyncio
async def test_repository_detection_clean_status_log_branches_and_head(
    git_service: GitService,
) -> None:
    info = await git_service.get_repository_info("project-a")
    status = await git_service.git_status("project-a")
    log = await git_service.git_log("project-a")
    branches = await git_service.git_branches("project-a")
    head = await git_service.git_head("project-a")

    assert info.is_repository is True
    assert info.repository_root == "project-a"
    assert info.clean is True
    assert status.clean is True
    assert log[0].subject == "Initial commit"
    assert any(branch.current for branch in branches.branches)
    assert head.commit == info.head_commit
    assert head.detached is False


@pytest.mark.asyncio
async def test_effective_clean_ignores_only_infrastructure_artifacts(
    repository: tuple[Path, Path],
) -> None:
    workspace, project = repository
    service = GitService(workspace)
    (project / ".venv").mkdir()
    (project / ".venv" / "pyvenv.cfg").write_text("home = python\n", encoding="utf-8")

    status = await service.git_status("project-a")
    info = await service.get_repository_info("project-a")

    assert status.clean is False
    assert status.effective_clean is True
    assert info.clean is False
    assert info.effective_clean is True

    (project / "secret.txt").write_text("real change\n", encoding="utf-8")
    status = await service.git_status("project-a")

    assert status.clean is False
    assert status.effective_clean is False


@pytest.mark.asyncio
async def test_non_repository_is_reported_without_initializing_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "plain-project"
    project.mkdir(parents=True)
    service = GitService(workspace)
    info = await service.get_repository_info("plain-project")
    assert info.state == "not_repository"
    assert info.is_repository is False
    assert not (project / ".git").exists()


@pytest.mark.asyncio
async def test_project_nested_inside_parent_repo_is_not_considered_repository(
    nested_parent_repository: tuple[Path, Path, Path],
) -> None:
    _, workspace, _ = nested_parent_repository
    service = GitService(workspace)

    info = await service.get_repository_info("project-a")

    assert info.state == "not_repository"
    assert info.is_repository is False
    assert info.inherited_parent_repository is True
    assert info.repository_root is None


@pytest.mark.asyncio
async def test_git_init_nested_project_creates_isolated_repository(
    nested_parent_repository: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    parent, workspace, project = nested_parent_repository
    service = await mutation_service(workspace, tmp_path / "nested-init.sqlite", "project-a", "nested-init")
    parent_head_before = run_git_output(parent, "rev-parse", "HEAD")
    parent_index_before = run_git_output(parent, "diff", "--cached", "--name-only")

    initialized = await service.git_init("project-a", workflow_id="nested-init")
    info = await service.get_repository_info("project-a", workflow_id="nested-init")

    assert initialized.initialized is True
    assert initialized.already_repository is False
    assert (project / ".git").exists()
    assert Path(run_git_output(project, "rev-parse", "--show-toplevel")).resolve() == project.resolve()
    assert info.is_repository is True
    assert info.inherited_parent_repository is False
    assert info.repository_root == "project-a"
    assert run_git_output(parent, "rev-parse", "HEAD") == parent_head_before
    assert run_git_output(parent, "diff", "--cached", "--name-only") == parent_index_before


@pytest.mark.asyncio
async def test_existing_isolated_nested_project_repo_remains_idempotent(
    nested_parent_repository: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    _, workspace, project = nested_parent_repository
    service = await mutation_service(workspace, tmp_path / "nested-idempotent.sqlite", "project-a", "nested-idempotent")

    first = await service.git_init("project-a", workflow_id="nested-idempotent")
    second = await service.git_init("project-a", workflow_id="nested-idempotent")

    assert first.initialized is True
    assert second.initialized is False
    assert second.already_repository is True
    assert Path(run_git_output(project, "rev-parse", "--show-toplevel")).resolve() == project.resolve()


@pytest.mark.asyncio
async def test_parent_git_is_never_used_before_isolated_init(
    nested_parent_repository: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    parent, workspace, project = nested_parent_repository
    workflow_id = "nested-guard"
    service = await mutation_service(workspace, tmp_path / "nested-guard.sqlite", "project-a", workflow_id)
    parent_cached_before = run_git_output(parent, "diff", "--cached", "--name-only")

    with pytest.raises(GitRepositoryNotFound):
        await service.git_status("project-a", workflow_id=workflow_id)
    with pytest.raises(GitRepositoryNotFound):
        await service.git_diff("project-a", workflow_id=workflow_id)
    with pytest.raises(GitRepositoryNotFound):
        await service.git_stage("project-a", ["README.md"], workflow_id=workflow_id)
    with pytest.raises(GitRepositoryNotFound):
        await service.prepare_git_commit("project-a", "should not use parent", workflow_id=workflow_id)
    with pytest.raises(GitRepositoryNotFound):
        await service.git_create_workflow_branch("project-a", workflow_id=workflow_id)

    assert run_git_output(parent, "diff", "--cached", "--name-only") == parent_cached_before
    assert "workspace/project-a/README.md" not in run_git_output(parent, "diff", "--cached", "--name-only")
    assert not (project / ".git").exists()


@pytest.mark.asyncio
async def test_status_modified_untracked_staged_and_diff(
    git_service: GitService,
    repository: tuple[Path, Path],
) -> None:
    _, project = repository
    (project / "README.md").write_text("initial\nchanged\n", encoding="utf-8")
    (project / "test.txt").write_text("new\n", encoding="utf-8")
    status = await git_service.git_status("project-a")
    diff = await git_service.git_diff("project-a")

    assert status.clean is False
    assert status.modified == ["README.md"]
    assert status.untracked == ["test.txt"]
    assert diff.files[0].path == "README.md"
    assert diff.files[0].additions == 1
    assert "changed" in diff.files[0].patch

    run_git(project, "add", "README.md")
    staged = await git_service.git_status("project-a")
    staged_diff = await git_service.git_diff("project-a", staged=True)
    assert staged.staged == ["README.md"]
    assert staged_diff.files[0].path == "README.md"


@pytest.mark.asyncio
async def test_status_parses_zero_delimited_rename_without_phantom_path(
    git_service: GitService,
    repository: tuple[Path, Path],
) -> None:
    _, project = repository
    run_git(project, "mv", "README.md", "RENAMED.md")

    status = await git_service.git_status("project-a")

    assert status.staged == ["RENAMED.md"]
    assert "README.md" not in status.modified


@pytest.mark.asyncio
async def test_diff_is_bounded_and_marked_truncated(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    (project / "README.md").write_text("x" * 500, encoding="utf-8")
    service = GitService(
        workspace,
        audit_store=GitAuditStore(tmp_path / "audit.sqlite"),
        policy=GitPolicy(diff_max_bytes=80, output_max_bytes=10_000),
    )
    await service.initialize()
    diff = await service.git_diff("project-a")
    assert diff.truncated is True
    assert sum(len(file.patch.encode()) for file in diff.files) <= 80


@pytest.mark.asyncio
async def test_detached_head(git_service: GitService, repository: tuple[Path, Path]) -> None:
    _, project = repository
    run_git(project, "checkout", "--detach")
    head = await git_service.git_head("project-a")
    assert head.detached is True
    assert head.branch is None


@pytest.mark.parametrize("path", ["../project-b/file.py", "/tmp/file.py", r"C:\repo\file.py", ".git/config"])
def test_file_history_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(GitPathViolation):
        validate_repository_relative_path(path)


@pytest.mark.asyncio
async def test_workflow_project_isolation(repository: tuple[Path, Path], tmp_path: Path) -> None:
    workspace, _ = repository
    database = tmp_path / "isolated.sqlite"
    store = GitAuditStore(database)
    await store.initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE workflow_registry(thread_id TEXT PRIMARY KEY, project_name TEXT)")
        connection.execute("INSERT INTO workflow_registry VALUES('workflow-a','project-a')")
    service = GitService(workspace, audit_store=store)
    await service.git_status("project-a", workflow_id="workflow-a")
    with pytest.raises(GitPathViolation):
        await service.git_status("project-b", workflow_id="workflow-a")


@pytest.mark.asyncio
async def test_invalid_project_id_is_not_masked_by_audit(repository: tuple[Path, Path], tmp_path: Path) -> None:
    workspace, _ = repository
    service = GitService(workspace, audit_store=GitAuditStore(tmp_path / "audit.sqlite"))
    await service.initialize()

    with pytest.raises(GitPathViolation):
        await service.git_status("../project-a")


def test_command_allowlist_rejects_mutations(repository: tuple[Path, Path]) -> None:
    workspace, project = repository
    service = GitService(workspace)
    for operation in ("add", "commit", "checkout", "switch", "reset", "clean", "merge", "rebase", "push", "pull", "fetch"):
        with pytest.raises(GitOperationNotAllowed):
            service._run(project, [operation])


def test_subprocess_never_uses_shell(monkeypatch: pytest.MonkeyPatch, repository: tuple[Path, Path]) -> None:
    workspace, project = repository
    captured: dict = {}
    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"ok", stderr=b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    GitService(workspace)._run(project, ["rev-parse", "HEAD"])
    assert captured["shell"] is False
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE
    assert isinstance(captured["cwd"], Path)


def test_subprocess_uses_create_no_window_when_available(
    monkeypatch: pytest.MonkeyPatch, repository: tuple[Path, Path],
) -> None:
    workspace, project = repository
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "run", fake_run)
    GitService(workspace)._run(project, ["rev-parse", "HEAD"])

    assert captured["creationflags"] == 0x08000000


def test_promotion_subprocess_handles_are_isolated(
    monkeypatch: pytest.MonkeyPatch, repository: tuple[Path, Path],
) -> None:
    workspace, project = repository
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    GitService(workspace)._run_promotion_command(
        project, ["switch", "workflow/demo"], base_branch="main", workflow_branch="workflow/demo"
    )

    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE
    assert captured["shell"] is False


def test_command_timeout_is_explicit(monkeypatch: pytest.MonkeyPatch, repository: tuple[Path, Path]) -> None:
    workspace, project = repository
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["git", "status"], 1)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(GitCommandTimeout):
        GitService(workspace)._run(project, ["status"])


def test_failed_git_command_captures_safe_stderr_summary(
    monkeypatch: pytest.MonkeyPatch, repository: tuple[Path, Path],
) -> None:
    workspace, project = repository
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 128, stdout=b"", stderr=b"fatal: unable to create commit\n"
        ),
    )

    with pytest.raises(GitCommandFailed) as raised:
        GitService(workspace)._run(project, ["commit", "-m", "safe message"])

    assert str(raised.value) == "Git command failed."
    assert raised.value.code == "git_command_failed"
    assert raised.value.details == {
        "command_operation": "commit",
        "returncode": 128,
        "stderr_summary": "fatal: unable to create commit",
    }


def test_failed_git_stderr_is_truncated_and_secrets_are_redacted(
    monkeypatch: pytest.MonkeyPatch, repository: tuple[Path, Path],
) -> None:
    workspace, project = repository
    secret = "sk-supersecretvalue12345"
    stderr = (
        f"Authorization: Bearer auth-value token={secret} password=hunter2 "
        f"https://user:credential@example.test/repo {'x' * 2_000}"
    ).encode()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, stdout=b"", stderr=stderr
        ),
    )

    with pytest.raises(GitCommandFailed) as raised:
        GitService(workspace)._run(project, ["status"])

    summary = raised.value.details["stderr_summary"]
    assert len(summary) <= GIT_STDERR_SUMMARY_MAX_CHARS
    assert summary.endswith("...[TRUNCATED]")
    assert "auth-value" not in summary
    assert secret not in summary
    assert "hunter2" not in summary
    assert "user:credential" not in summary
    assert "[REDACTED]" in summary


def test_successful_command_and_allow_failure_behavior_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, repository: tuple[Path, Path],
) -> None:
    workspace, project = repository
    results = iter((
        subprocess.CompletedProcess(["git", "status"], 0, stdout=b"success\n", stderr=b""),
        subprocess.CompletedProcess(["git", "status"], 1, stdout=b"allowed\n", stderr=b"failure"),
    ))
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: next(results))
    service = GitService(workspace)

    assert service._run(project, ["status"]) == "success\n"
    assert service._run(project, ["status"], allow_failure=True) == "allowed\n"


@pytest.mark.asyncio
async def test_failed_command_diagnostics_are_persisted_in_git_audit(
    repository: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _ = repository
    store = GitAuditStore(tmp_path / "failed-command-audit.sqlite")
    service = GitService(workspace, audit_store=store)
    await service.initialize()
    original_run_result = service._run_result

    def fail_status(root: Path, arguments: list[str]):
        if arguments[0] == "status":
            return subprocess.CompletedProcess(
                ["git", *arguments], 7, stdout=b"", stderr=b"fatal: token=private-value"
            )
        return original_run_result(root, arguments)

    monkeypatch.setattr(service, "_run_result", fail_status)

    with pytest.raises(GitCommandFailed):
        await service.git_status("project-a", workflow_id=None, agent_name="QA")

    row = (await store.list())[-1]
    assert row["error_code"] == "git_command_failed"
    assert row["command_operation"] == "status"
    assert row["returncode"] == 7
    assert row["stderr_summary"] == "fatal: token=[REDACTED]"


def test_output_limit_is_explicit(monkeypatch: pytest.MonkeyPatch, repository: tuple[Path, Path]) -> None:
    workspace, project = repository
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=b"x" * 20, stderr=b""),
    )
    with pytest.raises(GitOutputLimitExceeded):
        GitService(workspace, policy=GitPolicy(output_max_bytes=10))._run(project, ["status"])


@pytest.mark.parametrize("approval_status", ["awaiting_approval", "rejected", "stale"])
def test_git_post_approval_recovery_rejects_non_approved_commit_states(
    approval_status: str,
) -> None:
    recovery = detect_git_post_approval_recovery(
        "git_commit",
        {
            "approval_status": approval_status,
            "approval_id": "approval-1",
            "project_id": "project-a",
            "commit_sha": None,
        },
    )

    assert recovery.status == "not_recoverable"
    assert recovery.recoverable is False


def test_approved_commit_without_sha_is_recoverable_and_completed_is_idempotent() -> None:
    approved = detect_git_post_approval_recovery(
        "git_commit",
        {
            "approval_status": "approved",
            "approval_id": "approval-1",
            "project_id": "project-a",
            "commit_sha": None,
        },
    )
    completed = detect_git_post_approval_recovery(
        "git_commit",
        {
            "approval_status": "committed",
            "approval_id": "approval-1",
            "project_id": "project-a",
            "commit_sha": "a" * 40,
        },
    )

    assert approved == GitPostApprovalRecovery(
        "git_commit", "recoverable", "approval-1", "project-a", None
    )
    assert completed.status == "completed"
    assert completed.result_commit == "a" * 40


def test_approved_promotion_without_result_is_recoverable() -> None:
    recovery = detect_git_post_approval_recovery(
        "git_merge",
        None,
        {
            "status": "approved",
            "approval_id": "promotion-approval",
            "project_id": "project-a",
            "result_json": None,
        },
    )

    assert recovery.recoverable is True
    assert recovery.operation == "git_merge"


@pytest.mark.asyncio
async def test_approved_commit_recovery_executes_once_and_is_idempotent(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    workflow_id = "recovery-commit-1234"
    service = await mutation_service(
        workspace, tmp_path / "recovery-commit.sqlite", "project-a", workflow_id
    )
    await service.git_create_workflow_branch("project-a", workflow_id=workflow_id)
    (project / "README.md").write_text("recover me\n", encoding="utf-8")
    await service.git_stage("project-a", ["README.md"], workflow_id=workflow_id)
    preview = await service.prepare_git_commit(
        "project-a", "fix: recover approved commit", workflow_id=workflow_id
    )
    await service.approve_git_commit(workflow_id)

    before = run_git_output(project, "rev-list", "--count", "HEAD")
    detected, first = await service.recover_post_approval_operation(
        workflow_id, "git_commit"
    )
    completed, second = await service.recover_post_approval_operation(
        workflow_id, "git_commit"
    )
    after = run_git_output(project, "rev-list", "--count", "HEAD")

    assert detected.recoverable is True
    assert completed.status == "completed"
    assert first.commit == second.commit
    assert second.existing is True
    assert int(after) == int(before) + 1
    durable = await service.audit_store.get_workflow_state(workflow_id)  # type: ignore[union-attr]
    assert durable["approval_id"] == preview.approval_id
    assert durable["approval_status"] == "committed"
    assert durable["commit_sha"] == first.commit


@pytest.mark.asyncio
async def test_failed_commit_recovery_preserves_approved_state_for_retry(
    repository: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, project = repository
    workflow_id = "recovery-failure-1234"
    service = await mutation_service(
        workspace, tmp_path / "recovery-failure.sqlite", "project-a", workflow_id
    )
    await service.git_create_workflow_branch("project-a", workflow_id=workflow_id)
    (project / "README.md").write_text("retry me\n", encoding="utf-8")
    await service.git_stage("project-a", ["README.md"], workflow_id=workflow_id)
    preview = await service.prepare_git_commit(
        "project-a", "fix: retry durable approval", workflow_id=workflow_id
    )
    await service.approve_git_commit(workflow_id)
    original = service.git_commit

    async def fail_once(*args, **kwargs):
        raise GitCommandFailed(
            "Git command failed.",
            details={
                "command_operation": "commit",
                "returncode": 1,
                "stderr_summary": "safe",
            },
        )

    monkeypatch.setattr(service, "git_commit", fail_once)
    with pytest.raises(GitCommandFailed):
        await service.recover_post_approval_operation(workflow_id, "git_commit")
    durable = await service.audit_store.get_workflow_state(workflow_id)  # type: ignore[union-attr]
    assert durable["approval_status"] == "approved"
    assert durable["approval_id"] == preview.approval_id
    assert durable["commit_sha"] is None
    assert (await service.detect_post_approval_recovery(workflow_id, "git_commit")).recoverable

    monkeypatch.setattr(service, "git_commit", original)
    _recovery, result = await service.recover_post_approval_operation(
        workflow_id, "git_commit"
    )
    assert result.commit


@pytest.mark.asyncio
async def test_git_operation_creates_sanitized_observability_span(repository: tuple[Path, Path]) -> None:
    workspace, _ = repository
    calls: list[tuple[str, dict]] = []
    class Observability:
        @asynccontextmanager
        async def span(self, name, **kwargs):
            calls.append((name, kwargs))
            yield None
    service = GitService(workspace, observability=Observability())
    with observability_context(ObservabilityContext(trace_id="trace", span_id="root")):
        await service.git_status("project-a", workflow_id="workflow-a", agent_name="QA")
    name, details = calls[0]
    assert name == "git.status"
    assert details["attributes"] == {
        "workflow_id": "workflow-a", "project_id": "project-a",
        "agent_name": "QA", "operation": "status", "provider": "local", "cost": 0,
    }
    assert "patch" not in details["attributes"]


@pytest.mark.asyncio
async def test_audit_is_durable_idempotent_and_contains_no_patch(
    git_service: GitService, repository: tuple[Path, Path],
) -> None:
    _, project = repository
    (project / "README.md").write_text("changed\n", encoding="utf-8")
    await git_service.git_diff("project-a", workflow_id=None, agent_name="Developer")
    restarted = GitAuditStore(git_service.audit_store.database_path)  # type: ignore[union-attr]
    await restarted.initialize()
    rows = await restarted.list()
    assert rows[-1]["operation"] == "diff"
    assert rows[-1]["provider"] == "local"
    assert rows[-1]["cost"] == 0
    assert "patch" not in rows[-1]


@pytest.mark.asyncio
async def test_git_mcp_exposes_read_only_tools() -> None:
    tools = await git_server.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert names == {
        "get_repository_info", "git_status", "git_diff", "git_log",
        "git_branches", "git_head", "git_file_history",
        "init", "create_workflow_branch", "stage", "prepare_commit", "commit",
        "approve_commit", "reject_commit", "prepare_promotion",
        "merge_workflow_branch", "approve_promotion", "reject_promotion",
    }
    assert not names & {
        "diagnostic_ping", "diagnostic_service_ping", "diagnostic_project_root",
        "diagnostic_rev_parse", "diagnostic_to_thread", "diagnostic_subprocess",
        "diagnostic_git_environment",
    }
    assert not names & {"merge", "reset", "clean", "push", "pull", "fetch", "rebase"}


async def mutation_service(
    workspace: Path, database: Path, project_id: str, workflow_id: str,
) -> GitService:
    store = GitAuditStore(database)
    await store.initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_registry(thread_id TEXT PRIMARY KEY, project_name TEXT)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO workflow_registry VALUES(?,?)", (workflow_id, project_id)
        )
    return GitService(workspace, audit_store=store)


@pytest.mark.asyncio
async def test_git_mcp_stdio_repository_info_returns_without_timeout() -> None:
    project_id = f"git-stdio-repository-{uuid4().hex[:8]}"
    project = get_workspace_root() / project_id
    project.mkdir(parents=True, exist_ok=False)
    client = MCPServerClient(
        "git",
        str(Path(__file__).resolve().parents[1] / "servers" / "git_server.py"),
        timeouts=MCPClientTimeoutConfig(
            connect_timeout_seconds=15,
            tool_timeout_seconds=10,
            disconnect_timeout_seconds=10,
        ),
    )
    try:
        await client.connect()
        result = await client.call_tool("get_repository_info", {"project_id": project_id})
    finally:
        await client.disconnect()
        shutil.rmtree(project, ignore_errors=True)

    payload = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    assert payload["state"] == "not_repository"
    assert payload["is_repository"] is False


@pytest.mark.asyncio
async def test_git_init_new_repository_is_idempotent_and_does_not_configure_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "new-project"
    project.mkdir(parents=True)
    service = await mutation_service(workspace, tmp_path / "git.sqlite", "new-project", "workflow-init")

    first = await service.git_init("new-project", workflow_id="workflow-init")
    second = await service.git_init("new-project", workflow_id="workflow-init")

    assert first.initialized is True
    assert second.initialized is False and second.already_repository is True
    assert not run_git_output(project, "config", "--local", "--get", "user.name")


@pytest.mark.asyncio
async def test_unborn_repository_current_commit_is_none(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "unborn-project"
    project.mkdir(parents=True)
    service = await mutation_service(workspace, tmp_path / "unborn.sqlite", "unborn-project", "unborn-1234")
    await service.git_init("unborn-project", workflow_id="unborn-1234")

    assert await service._current_commit(project) is None


def run_git_output(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, shell=False, check=False, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.asyncio
async def test_workflow_branch_stage_preview_approval_and_commit_are_safe_and_idempotent(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    service = await mutation_service(workspace, tmp_path / "mutations.sqlite", "project-a", "workflow-12345678")
    initialized = await service.git_init("project-a", workflow_id="workflow-12345678")
    branch = await service.git_create_workflow_branch("project-a", workflow_id="workflow-12345678")
    repeated = await service.git_create_workflow_branch("project-a", workflow_id="workflow-12345678")
    (project / "README.md").write_text("approved change\n", encoding="utf-8")
    staged = await service.git_stage("project-a", ["README.md"], workflow_id="workflow-12345678")
    preview = await service.prepare_git_commit(
        "project-a", "feat: add workflow Git validation", workflow_id="workflow-12345678"
    )

    with pytest.raises(GitApprovalRequired):
        await service.git_commit(
            "project-a", approval_id=preview.approval_id, workflow_id="workflow-12345678"
        )
    result = await service.resolve_commit_approval("workflow-12345678", approved=True)
    retried = await service.git_commit(
        "project-a", approval_id=preview.approval_id, workflow_id="workflow-12345678"
    )

    assert initialized.already_repository is True
    assert branch.branch == service.workflow_branch_name("workflow-12345678") and branch.created is True
    assert repeated.created is False and repeated.switched is False
    assert staged.staged_files == ["README.md"]
    assert preview.diff_fingerprint and preview.ready is True
    assert result is not None and result.commit == run_git_output(project, "rev-parse", "HEAD")
    assert retried.existing is True and retried.commit == result.commit
    assert run_git_output(project, "status", "--porcelain") == ""
    durable = await service.audit_store.get_workflow_state("workflow-12345678")  # type: ignore[union-attr]
    consumed_preview = json.loads(durable["commit_preview_json"])
    assert durable["approval_status"] == "committed"
    assert consumed_preview["status"] == "committed"
    assert consumed_preview["ready"] is False
    assert consumed_preview["approval_id"] == preview.approval_id
    assert consumed_preview["diff_fingerprint"] == preview.diff_fingerprint
    summary = await service.workflow_summary("project-a", "workflow-12345678")
    assert summary.commit_status == "committed"
    assert summary.commit_preview is not None
    assert summary.commit_preview["status"] == "committed"

    (project / "README.md").write_text("repair change\n", encoding="utf-8")
    await service.git_stage(
        "project-a", ["README.md"], workflow_id="workflow-12345678", agent_name="Repair"
    )
    repair_preview = await service.prepare_git_commit(
        "project-a", "fix: repair validated output",
        workflow_id="workflow-12345678", agent_name="Repair",
    )
    repair = await service.resolve_commit_approval(
        "workflow-12345678", approved=True, actor="user"
    )
    assert repair is not None and repair.commit != result.commit
    assert repair_preview.approval_id != preview.approval_id
    assert run_git_output(project, "log", "--format=%s", "-2").splitlines() == [
        "fix: repair validated output", "feat: add workflow Git validation",
    ]


@pytest.mark.asyncio
async def test_unborn_workflow_branch_and_first_commit_store_null_provenance(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "unborn-project"
    project.mkdir(parents=True)
    workflow = "431bc1de-8761-4c00-a784-7b95c7171880"
    service = await mutation_service(workspace, tmp_path / "unborn-provenance.sqlite", "unborn-project", workflow)
    await service.git_init("unborn-project", workflow_id=workflow)
    run_git(project, "config", "user.name", "Unborn Test")
    run_git(project, "config", "user.email", "unborn@example.com")

    branch = await service.git_create_workflow_branch("unborn-project", workflow_id=workflow)
    durable = await service.audit_store.get_workflow_state(workflow)  # type: ignore[union-attr]

    assert branch.base_branch == "master"
    assert branch.base_commit is None
    assert durable["base_commit"] is None
    assert durable["head_commit"] is None

    (project / "README.md").write_text("first\n", encoding="utf-8")
    await service.git_stage("unborn-project", ["README.md"], workflow_id=workflow)
    preview = await service.prepare_git_commit("unborn-project", "feat: first commit", workflow_id=workflow)
    await service.approve_git_commit(workflow)
    result = await service.git_commit("unborn-project", workflow_id=workflow, approval_id=preview.approval_id)
    durable = await service.audit_store.get_workflow_state(workflow)  # type: ignore[union-attr]
    operations = await service.audit_store.list(workflow_id=workflow)  # type: ignore[union-attr]
    commit_operation = [item for item in operations if item["operation"] == "git.commit"][-1]

    assert result.commit == run_git_output(project, "rev-parse", "HEAD")
    assert durable["commit_sha"] == result.commit
    assert durable["head_commit"] == result.commit
    assert durable["base_commit"] is None
    assert commit_operation["base_commit"] is None
    assert commit_operation["parent_commit"] is None
    assert commit_operation["commit_sha"] == result.commit
    assert result.commit is not None and SHA_RE.fullmatch(result.commit)


@pytest.mark.asyncio
async def test_legacy_head_commit_fields_are_normalized_on_read(tmp_path: Path) -> None:
    database = tmp_path / "legacy-head.sqlite"
    store = GitAuditStore(database)
    await store.initialize()
    valid_sha = "a" * 40
    now = "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO git_workflow_state(
                workflow_id, project_id, state, base_commit, head_commit,
                staged_files_json, updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            ("legacy-workflow", "project-a", "committed", "HEAD", "", "[]", now),
        )
        connection.execute(
            """
            INSERT INTO git_operations(
                operation_id, workflow_id, project_id, operation, repository_ref,
                success, duration_ms, provider, cost, created_at,
                commit_sha, base_commit, parent_commit
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "op-1", "legacy-workflow", "project-a", "git.commit", "project-a",
                1, 0, "local", 0, now, valid_sha, "HEAD", "",
            ),
        )

    durable = await store.get_workflow_state("legacy-workflow")
    operations = await store.list(workflow_id="legacy-workflow")

    assert durable is not None
    assert durable["base_commit"] is None
    assert durable["head_commit"] is None
    assert operations[0]["commit_sha"] == valid_sha
    assert operations[0]["base_commit"] is None
    assert operations[0]["parent_commit"] is None


@pytest.mark.asyncio
async def test_protected_branch_and_non_explicit_staging_are_rejected(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, _ = repository
    service = await mutation_service(workspace, tmp_path / "protected.sqlite", "project-a", "protected-1234")
    with pytest.raises(GitProtectedBranchViolation):
        await service.git_stage("project-a", ["README.md"], workflow_id="protected-1234")
    with pytest.raises(GitPathViolation):
        await service.git_stage("project-a", ["."], workflow_id="protected-1234")
    with pytest.raises(GitOperationNotAllowed):
        service._run(workspace / "project-a", ["add", "--", "."])
    with pytest.raises(GitOperationNotAllowed):
        service._run(workspace / "project-a", ["commit", "--amend"])
    with pytest.raises(GitOperationNotAllowed):
        service._run(workspace / "project-a", ["branch", "-D", "main"])
    with pytest.raises(GitOperationNotAllowed):
        service._run(workspace / "project-a", ["diff", "--no-index", "README.md", "../secret"])


@pytest.mark.asyncio
async def test_changed_staging_invalidates_approved_fingerprint(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    service = await mutation_service(workspace, tmp_path / "stale.sqlite", "project-a", "stale-12345678")
    await service.git_init("project-a", workflow_id="stale-12345678")
    await service.git_create_workflow_branch("project-a", workflow_id="stale-12345678")
    (project / "README.md").write_text("diff A\n", encoding="utf-8")
    await service.git_stage("project-a", ["README.md"], workflow_id="stale-12345678")
    preview = await service.prepare_git_commit("project-a", "change A", workflow_id="stale-12345678")
    await service.approve_git_commit("stale-12345678")
    (project / "README.md").write_text("diff B\n", encoding="utf-8")
    await service.git_stage("project-a", ["README.md"], workflow_id="stale-12345678")

    with pytest.raises(GitApprovalStale):
        await service.git_commit(
            "project-a", approval_id=preview.approval_id, workflow_id="stale-12345678"
        )
    assert run_git_output(project, "log", "--oneline").count("Initial commit") == 1


@pytest.mark.asyncio
async def test_rejected_preview_survives_restart_and_creates_no_commit(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    database = tmp_path / "restart.sqlite"
    service = await mutation_service(workspace, database, "project-a", "reject-12345678")
    await service.git_init("project-a", workflow_id="reject-12345678")
    await service.git_create_workflow_branch("project-a", workflow_id="reject-12345678")
    (project / "README.md").write_text("rejected\n", encoding="utf-8")
    await service.git_stage("project-a", ["README.md"], workflow_id="reject-12345678")
    preview = await service.prepare_git_commit("project-a", "rejected commit", workflow_id="reject-12345678")
    restarted = GitService(workspace, audit_store=GitAuditStore(database))

    assert (await restarted.pending_commit_preview("reject-12345678"))["approval_id"] == preview.approval_id
    assert await restarted.resolve_commit_approval("reject-12345678", approved=False, reason="No") is None
    assert run_git_output(project, "diff", "--cached", "--name-only") == "README.md"
    assert "rejected commit" not in run_git_output(project, "log", "--oneline")


@pytest.mark.asyncio
async def test_missing_git_identity_blocks_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "NUL")
    workspace = tmp_path / "workspace"
    project = workspace / "identity-project"
    project.mkdir(parents=True)
    service = await mutation_service(workspace, tmp_path / "identity.sqlite", "identity-project", "identity-12345678")
    await service.git_init("identity-project", workflow_id="identity-12345678")
    await service.git_create_workflow_branch("identity-project", workflow_id="identity-12345678")
    (project / "README.md").write_text("content\n", encoding="utf-8")
    await service.git_stage("identity-project", ["README.md"], workflow_id="identity-12345678")
    preview = await service.prepare_git_commit("identity-project", "initial", workflow_id="identity-12345678")
    await service.approve_git_commit("identity-12345678")
    with pytest.raises(GitIdentityMissing):
        await service.git_commit(
            "identity-project", approval_id=preview.approval_id, workflow_id="identity-12345678"
        )


@pytest.mark.asyncio
async def test_fork_workflow_gets_its_own_branch_and_provenance(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, _ = repository
    database = tmp_path / "fork.sqlite"
    original = "11111111-1111-1111-1111-111111111111"
    fork = "22222222-2222-2222-2222-222222222222"
    service = await mutation_service(workspace, database, "project-a", original)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO workflow_registry VALUES(?,?)", (fork, "project-a"))
    await service.git_init("project-a", workflow_id=original)
    original_branch = await service.git_create_workflow_branch("project-a", workflow_id=original)
    origin_commit = run_git_output(workspace / "project-a", "rev-parse", "HEAD")
    fork_branch = await service.git_create_workflow_branch(
        "project-a", workflow_id=fork, fork_origin_workflow=original,
        fork_origin_commit=origin_commit,
    )
    durable = await service.audit_store.get_workflow_state(fork)  # type: ignore[union-attr]

    assert original_branch.branch == "workflow/11111111"
    assert fork_branch.branch == "workflow/22222222"
    assert durable["fork_origin_workflow"] == original
    assert durable["fork_origin_commit"] == origin_commit


@pytest.mark.asyncio
async def test_same_workflow_fork_uses_distinct_branch_at_origin_commit(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    workflow = "11111111-1111-1111-1111-111111111111"
    service = await mutation_service(workspace, tmp_path / "same-thread-fork.sqlite", "project-a", workflow)
    await service.git_init("project-a", workflow_id=workflow)
    original = await service.git_create_workflow_branch("project-a", workflow_id=workflow)
    origin_commit = run_git_output(project, "rev-parse", "HEAD")
    fork = await service.git_create_workflow_branch(
        "project-a", workflow_id=workflow, branch_identity=f"{workflow}:fork:checkpoint-7",
        fork_origin_workflow=workflow, fork_origin_commit=origin_commit,
    )

    assert fork.branch != original.branch
    assert run_git_output(project, "rev-parse", "HEAD") == origin_commit
    assert run_git_output(project, "branch", "--show-current") == fork.branch


@pytest.mark.asyncio
async def test_branch_creation_rejects_unattributed_dirty_files(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    workflow = "dirty-workflow"
    service = await mutation_service(workspace, tmp_path / "dirty.sqlite", "project-a", workflow)
    await service.git_init("project-a", workflow_id=workflow)
    original_branch = run_git_output(project, "branch", "--show-current")
    (project / "notes.txt").write_text("unrelated\n", encoding="utf-8")

    with pytest.raises(GitWorkspaceDirtyConflict):
        await service.git_create_workflow_branch("project-a", workflow_id=workflow)

    assert run_git_output(project, "branch", "--show-current") == original_branch


@pytest.mark.asyncio
async def test_branch_creation_ignores_venv_infrastructure(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    workflow = "venv-workflow"
    service = await mutation_service(workspace, tmp_path / "venv.sqlite", "project-a", workflow)
    await service.git_init("project-a", workflow_id=workflow)
    (project / ".venv").mkdir()
    (project / ".venv" / "pyvenv.cfg").write_text("home = python\n", encoding="utf-8")

    branch = await service.git_create_workflow_branch("project-a", workflow_id=workflow)

    assert branch.branch == service.workflow_branch_name(workflow)
    assert run_git_output(project, "diff", "--cached", "--name-only") == ""


@pytest.mark.asyncio
async def test_branch_creation_allows_untracked_directory_with_only_allowed_files(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    workflow = "tests-dir-workflow"
    service = await mutation_service(workspace, tmp_path / "tests-dir.sqlite", "project-a", workflow)
    await service.git_init("project-a", workflow_id=workflow)
    (project / "tests").mkdir()
    (project / "tests" / "test_health.py").write_text("def test_health(): pass\n", encoding="utf-8")

    branch = await service.git_create_workflow_branch(
        "project-a", workflow_id=workflow, allowed_dirty_paths=["tests/test_health.py"]
    )

    assert branch.branch == service.workflow_branch_name(workflow)


@pytest.mark.asyncio
async def test_branch_creation_rejects_untracked_directory_with_foreign_file(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    workflow = "tests-dir-conflict"
    service = await mutation_service(workspace, tmp_path / "tests-dir-conflict.sqlite", "project-a", workflow)
    await service.git_init("project-a", workflow_id=workflow)
    (project / "tests").mkdir()
    (project / "tests" / "test_health.py").write_text("def test_health(): pass\n", encoding="utf-8")
    (project / "tests" / "foreign.txt").write_text("manual\n", encoding="utf-8")

    with pytest.raises(GitWorkspaceDirtyConflict):
        await service.git_create_workflow_branch(
            "project-a", workflow_id=workflow, allowed_dirty_paths=["tests/test_health.py"]
        )


@pytest.mark.asyncio
async def test_branch_creation_allows_package_directory_when_all_files_allowed(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    workflow = "package-dir-workflow"
    service = await mutation_service(workspace, tmp_path / "package-dir.sqlite", "project-a", workflow)
    await service.git_init("project-a", workflow_id=workflow)
    package = project / "project_a"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "main.py").write_text("app = None\n", encoding="utf-8")

    branch = await service.git_create_workflow_branch(
        "project-a",
        workflow_id=workflow,
        allowed_dirty_paths=["project_a/__init__.py", "project_a/main.py"],
    )

    assert branch.branch == service.workflow_branch_name(workflow)


@pytest.mark.asyncio
async def test_branch_creation_allows_exact_readme_and_rejects_exact_foreign(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    allowed_workflow = "exact-readme"
    service = await mutation_service(workspace, tmp_path / "exact.sqlite", "project-a", allowed_workflow)
    await service.git_init("project-a", workflow_id=allowed_workflow)
    (project / "README.md").write_text("workflow change\n", encoding="utf-8")
    allowed = await service.git_create_workflow_branch(
        "project-a", workflow_id=allowed_workflow, allowed_dirty_paths=["README.md"]
    )
    assert allowed.branch == service.workflow_branch_name(allowed_workflow)
    run_git(project, "add", "README.md")
    run_git(project, "commit", "-m", "Allowed readme")

    foreign_workflow = "exact-foreign"
    with sqlite3.connect(tmp_path / "exact.sqlite") as connection:
        connection.execute(
            "INSERT OR REPLACE INTO workflow_registry VALUES(?,?)", (foreign_workflow, "project-a")
        )
    (project / "foreign.txt").write_text("manual\n", encoding="utf-8")
    with pytest.raises(GitWorkspaceDirtyConflict):
        await service.git_create_workflow_branch("project-a", workflow_id=foreign_workflow)


@pytest.mark.asyncio
async def test_branch_creation_normalizes_windows_allowed_paths(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    workflow = "windows-paths"
    service = await mutation_service(workspace, tmp_path / "windows.sqlite", "project-a", workflow)
    await service.git_init("project-a", workflow_id=workflow)
    (project / "tests").mkdir()
    (project / "tests" / "test_health.py").write_text("def test_health(): pass\n", encoding="utf-8")

    branch = await service.git_create_workflow_branch(
        "project-a", workflow_id=workflow, allowed_dirty_paths=[r"tests\test_health.py"]
    )

    assert branch.branch == service.workflow_branch_name(workflow)


@pytest.mark.asyncio
async def test_branch_creation_rejects_allowed_path_traversal(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, _project = repository
    workflow = "traversal-paths"
    service = await mutation_service(workspace, tmp_path / "traversal.sqlite", "project-a", workflow)
    await service.git_init("project-a", workflow_id=workflow)

    with pytest.raises(GitPathViolation):
        await service.git_create_workflow_branch(
            "project-a", workflow_id=workflow, allowed_dirty_paths=["../secret.txt"]
        )


@pytest.mark.asyncio
async def test_commit_cannot_be_prepared_before_durable_tests_pass(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    database = tmp_path / "tests-gate.sqlite"
    workflow_id = "tests-gate-12345678"
    service = await mutation_service(workspace, database, "project-a", workflow_id)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE workflow_registry ADD COLUMN tests_passed INTEGER DEFAULT 0")
    await service.git_init("project-a", workflow_id=workflow_id)
    await service.git_create_workflow_branch("project-a", workflow_id=workflow_id)
    (project / "README.md").write_text("not tested\n", encoding="utf-8")
    await service.git_stage("project-a", ["README.md"], workflow_id=workflow_id)
    with pytest.raises(GitTestsNotPassed):
        await service.prepare_git_commit("project-a", "not allowed", workflow_id=workflow_id)


def test_planner_qa_and_replay_have_no_git_mutation_coupling() -> None:
    root = Path(__file__).parents[1]
    for path in (
        root / "graph" / "subgraphs" / "planning",
        root / "graph" / "persistence_service.py",
    ):
        sources = [path] if path.is_file() else list(path.rglob("*.py"))
        for source in sources:
            content = source.read_text(encoding="utf-8")
            assert "git__stage" not in content
            assert "git__commit" not in content


def test_agent_subgraphs_have_no_direct_git_dependency() -> None:
    forbidden_modules = {"subprocess", "git", "pygit2", "dulwich", "api.services.git_service"}
    root = Path(__file__).parents[1] / "graph" / "subgraphs"
    for source_path in root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        assert not any(name in forbidden_modules or name.startswith("api.services.git") for name in imports), source_path


class FakeGitService:
    async def get_repository_info(self, *_args, **_kwargs):
        return GitRepositoryInfo(state="available", is_repository=True, repository_root="api-project", current_branch="main", head_commit="a" * 40, clean=False)
    async def git_status(self, *_args, **_kwargs):
        return GitStatusResponse(branch="main", clean=False, modified=["README.md"])
    async def git_diff(self, *_args, **_kwargs):
        return GitDiffResponse(staged=False, files=[GitDiffFile(path="README.md", status="modified", additions=1)])
    async def git_log(self, *_args, **_kwargs):
        return [GitLogEntry(commit="a" * 40, short_commit="aaaaaaa", author_name="Test", author_email="test@example.com", timestamp="2026-08-10T00:00:00Z", subject="Initial")]
    async def git_branches(self, *_args, **_kwargs):
        return GitBranchesResponse(current="main", branches=[GitBranch(name="main", current=True, commit="a" * 40)])
    async def workflow_summary(self, *_args, **_kwargs):
        return SimpleNamespace(
            base_branch=None, base_commit=None, workflow_branch=None,
            commits=[], commit_preview=None, commit_status=None,
        )


def test_git_api_endpoints_are_structured() -> None:
    class Query:
        async def get_snapshot(self, thread_id):
            return WorkflowSnapshotResponse(thread_id=thread_id, checkpoint_id="cp", project_name="api-project", workflow_intent="create_project", terminal_status="completed", interrupted=False, pending_operation=None, pending_tool=None, planning={}, implementation={}, testing={}, supervisor={}, created_at=None, updated_at=None)
    services = SimpleNamespace(query=Query(), git=FakeGitService())
    @asynccontextmanager
    async def factory():
        yield services
    with TestClient(create_app(factory)) as client:
        assert client.get("/api/workflows/thread/git").json()["current_branch"] == "main"
        assert client.get("/api/workflows/thread/git/status").json()["modified"] == ["README.md"]
        assert client.get("/api/workflows/thread/git/diff").json()["files"][0]["path"] == "README.md"
        assert client.get("/api/workflows/thread/git/log").json()[0]["subject"] == "Initial"
        assert client.get("/api/workflows/thread/git/branches").json()["current"] == "main"


def test_git_api_returns_not_repository_after_infrastructure_failure_before_git(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "api-project"
    project.mkdir(parents=True)
    service = GitService(workspace, audit_store=GitAuditStore(tmp_path / "git-api.sqlite"))
    service.audit_store.initialize_sync()  # type: ignore[union-attr]
    with sqlite3.connect(service.audit_store.database_path) as connection:  # type: ignore[union-attr]
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_registry(thread_id TEXT PRIMARY KEY, project_name TEXT)"
        )
        connection.execute(
            "INSERT INTO workflow_registry VALUES(?,?)", ("thread", "api-project")
        )

    class Query:
        async def get_snapshot(self, thread_id):
            return WorkflowSnapshotResponse(
                thread_id=thread_id, checkpoint_id="cp", project_name="api-project",
                workflow_intent="create_project", terminal_status="infrastructure_failed",
                interrupted=False, pending_operation=None, pending_tool=None,
                planning={}, implementation={}, testing={}, supervisor={},
                created_at=None, updated_at=None,
            )

    services = SimpleNamespace(query=Query(), git=service, approvals=None)

    @asynccontextmanager
    async def factory():
        yield services

    with TestClient(create_app(factory)) as client:
        response = client.get("/api/workflows/thread/git")

    assert response.status_code == 200
    assert response.json()["state"] == "not_repository"
    assert response.json()["is_repository"] is False


@pytest.mark.asyncio
async def test_mutation_api_requires_existing_approval_endpoint_for_commit(
    repository: tuple[Path, Path], tmp_path: Path,
) -> None:
    workspace, project = repository
    workflow_id = "api-mutation-12345678"
    service = await mutation_service(
        workspace, tmp_path / "api-mutations.sqlite", "project-a", workflow_id
    )

    class Query:
        async def get_snapshot(self, thread_id):
            return WorkflowSnapshotResponse(
                thread_id=thread_id, checkpoint_id="cp", project_name="project-a",
                workflow_intent="create_project", terminal_status="completed", interrupted=False,
                pending_operation=None, pending_tool=None, planning={}, implementation={},
                testing={}, supervisor={}, created_at=None, updated_at=None,
            )

    services = SimpleNamespace(query=Query(), git=service, approvals=None)

    @asynccontextmanager
    async def factory():
        yield services

    with TestClient(create_app(factory)) as client:
        assert client.post(f"/api/workflows/{workflow_id}/git/init").status_code == 200
        branch = client.post(f"/api/workflows/{workflow_id}/git/branch")
        assert branch.status_code == 200
        (project / "README.md").write_text("API mutation\n", encoding="utf-8")
        staged = client.post(
            f"/api/workflows/{workflow_id}/git/stage",
            json={"paths": ["README.md"], "actor": "Developer"},
        )
        assert staged.json()["staged_files"] == ["README.md"]
        preview = client.post(
            f"/api/workflows/{workflow_id}/git/commit/prepare",
            json={"message": "feat: API approved commit", "actor": "Developer"},
        )
        assert preview.json()["status"] == "awaiting_approval"
        assert client.post(
            f"/api/workflows/{workflow_id}/git/commit", json={}
        ).status_code in {404, 405}
        approval = client.post(
            f"/api/workflows/{workflow_id}/approve", json={"reason": "Reviewed"}
        )
        assert approval.status_code == 202
        assert approval.json() == {
            "thread_id": workflow_id, "accepted": True, "operation": "git_commit",
            "tool_name": "git__commit", "status": "completed",
        }
        git_summary = client.get(f"/api/workflows/{workflow_id}/git")
        assert git_summary.status_code == 200
        assert git_summary.json()["commit_status"] == "committed"
        assert git_summary.json()["commit_preview"]["status"] == "committed"
        assert git_summary.json()["commit_preview"]["approval_id"] == preview.json()["approval_id"]
        assert "feat: API approved commit" in run_git_output(project, "log", "--oneline", "-1")


def test_graph_git_approval_resumes_checkpoint_instead_of_committing_directly() -> None:
    class Query:
        async def get_snapshot(self, thread_id):
            return WorkflowSnapshotResponse(
                thread_id=thread_id, checkpoint_id="cp-git", project_name="api-project",
                workflow_intent="create_project", terminal_status="pending", interrupted=True,
                pending_operation="git_commit", pending_tool="git__commit", planning={},
                implementation={}, testing={}, supervisor={}, created_at=None, updated_at=None,
            )

    class Git:
        async def pending_commit_preview(self, _thread_id):
            raise AssertionError("Graph approvals must not use the direct Git approval path")

    class Approvals:
        async def resolve(self, *, thread_id, approved, reason):
            assert approved is True and reason == "Reviewed"
            return ApprovalResponse(
                thread_id=thread_id, accepted=True, operation="git_commit",
                tool_name="git__commit", status="running",
            )

    services = SimpleNamespace(query=Query(), git=Git(), approvals=Approvals())

    @asynccontextmanager
    async def factory():
        yield services

    with TestClient(create_app(factory)) as client:
        response = client.post("/api/workflows/graph-git/approve", json={"reason": "Reviewed"})
    assert response.status_code == 202
    assert response.json()["status"] == "running"
