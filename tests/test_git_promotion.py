from __future__ import annotations

from pathlib import Path
from contextlib import asynccontextmanager
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.models import WorkflowSnapshotResponse
from api.services.git_service import (
    GitApprovalRequired,
    GitCIPromotionBlocked,
    GitCIRequiredForPromotion,
    GitOperationNotAllowed,
    GitPromotionStale,
    GitWorkspaceDirtyConflict,
    GitService,
)
from api.services.git_store import GitAuditStore
from api.ci_models import CIPromotionEligibility


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, shell=False, check=True,
        capture_output=True, text=True,
    )
    return result.stdout.strip()


class FakeCIService:
    def __init__(self, eligibility: CIPromotionEligibility) -> None:
        self.eligibility = eligibility

    async def promotion_eligibility(self, workflow_id: str, target_commit: str | None) -> CIPromotionEligibility:
        return self.eligibility.model_copy(update={"target_commit": target_commit})


def ci_eligibility(
    *,
    eligible: bool,
    reason: str,
    commit: str | None,
    run_id: str | None = "ci-run-1",
    decision: str | None = "accepted",
) -> CIPromotionEligibility:
    return CIPromotionEligibility(
        required=True,
        eligible=eligible,
        reason=reason,
        commit_match=eligible,
        source_commit=commit,
        target_commit=commit,
        run_id=run_id,
        ci_status="passed" if decision != "rejected" else "failed",
        ci_decision=decision,  # type: ignore[arg-type]
        blocking_gates=[] if eligible else ["test-gate"],
        warnings=[],
        gate_policy_version="6.21.2-v1",
        pipeline_version="6.21.1-v1",
        pipeline_fingerprint=f"fp-{run_id}",
    )


@pytest.fixture
async def promotion_repo(tmp_path: Path):
    workspace = tmp_path / "workspace"
    project = workspace / "project-a"
    project.mkdir(parents=True)
    git(project, "init")
    git(project, "config", "user.name", "Promotion Test")
    git(project, "config", "user.email", "promotion@example.com")
    (project / "README.md").write_text("line one\n", encoding="utf-8")
    git(project, "add", "README.md")
    git(project, "commit", "-m", "base")
    database = tmp_path / "promotion.sqlite"
    store = GitAuditStore(database)
    await store.initialize()
    workflow = "11111111-1111-1111-1111-111111111111"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE workflow_registry("
            "thread_id TEXT PRIMARY KEY,project_name TEXT,terminal_status TEXT,tests_passed INTEGER)"
        )
        connection.execute(
            "INSERT INTO workflow_registry VALUES(?,?,?,?)",
            (workflow, "project-a", "completed", 1),
        )
    service = GitService(workspace, audit_store=store)
    await service.git_init("project-a", workflow_id=workflow)
    branch = await service.git_create_workflow_branch("project-a", workflow_id=workflow)
    (project / "feature.py").write_text("value = 1\n", encoding="utf-8")
    await service.git_stage("project-a", ["feature.py"], workflow_id=workflow)
    commit_preview = await service.prepare_git_commit(
        "project-a", "feat: add feature", workflow_id=workflow,
    )
    await service.approve_git_commit(workflow)
    commit = await service.git_commit(
        "project-a", workflow_id=workflow, approval_id=commit_preview.approval_id,
    )
    return service, store, project, workflow, branch.base_branch, commit.commit


@pytest.mark.asyncio
async def test_fast_forward_promotion_is_approved_audited_and_idempotent(promotion_repo):
    service, store, project, workflow, base_branch, workflow_head = promotion_repo
    preview = await service.prepare_git_promotion("project-a", workflow_id=workflow)

    assert preview.ready is True
    assert preview.merge_strategy_candidate == "fast_forward"
    assert preview.commits_ahead == 1 and preview.commits_behind == 0
    assert preview.conflict_state == "clean"
    assert preview.base_branch == base_branch
    await service.approve_git_promotion(workflow)
    result = await service.merge_git_workflow_branch(
        "project-a", workflow_id=workflow, approval_id=preview.approval_id,
    )
    repeated = await service.merge_git_workflow_branch(
        "project-a", workflow_id=workflow, approval_id=preview.approval_id,
    )

    assert result.strategy == "fast_forward"
    assert result.result_commit == workflow_head
    assert repeated.existing is True and repeated.result_commit == result.result_commit
    assert git(project, "branch", "--show-current") == base_branch
    assert git(project, "status", "--porcelain") == ""
    audit = await store.list(workflow_id=workflow)
    assert any(item["operation"] == "git.promotion.merge" for item in audit)


@pytest.mark.asyncio
async def test_promotion_requires_ci_when_ci_service_requires_it(promotion_repo) -> None:
    service, _store, _project, workflow, _base_branch, workflow_head = promotion_repo
    service.ci_service = FakeCIService(
        ci_eligibility(
            eligible=False,
            reason="ci_required_for_promotion",
            commit=workflow_head,
            run_id=None,
            decision=None,
        )
    )

    with pytest.raises(GitCIRequiredForPromotion):
        await service.prepare_git_promotion("project-a", workflow_id=workflow)


@pytest.mark.asyncio
async def test_promotion_preview_includes_ci_evidence_and_fingerprint(promotion_repo) -> None:
    service, _store, _project, workflow, _base_branch, workflow_head = promotion_repo
    service.ci_service = FakeCIService(
        ci_eligibility(eligible=True, reason="ci_accepted", commit=workflow_head, run_id="ci-run-a")
    )

    first = await service.prepare_git_promotion("project-a", workflow_id=workflow)
    service.ci_service = FakeCIService(
        ci_eligibility(eligible=True, reason="ci_accepted", commit=workflow_head, run_id="ci-run-b")
    )
    second = await service.prepare_git_promotion("project-a", workflow_id=workflow)

    assert first.ci is not None
    assert first.ci["run_id"] == "ci-run-a"
    assert first.ci["source_commit"] == workflow_head
    assert first.promotion_fingerprint != second.promotion_fingerprint


@pytest.mark.asyncio
async def test_promotion_rejected_by_ci_is_blocked(promotion_repo) -> None:
    service, _store, _project, workflow, _base_branch, workflow_head = promotion_repo
    service.ci_service = FakeCIService(
        ci_eligibility(
            eligible=False,
            reason="ci_promotion_blocked",
            commit=workflow_head,
            decision="rejected",
        )
    )

    with pytest.raises(GitCIPromotionBlocked):
        await service.prepare_git_promotion("project-a", workflow_id=workflow)


@pytest.mark.parametrize(
    "relative_path,content",
    [
        (".venv/pyvenv.cfg", "home = python\n"),
        ("project_a/__pycache__/module.cpython-312.pyc", "cache\n"),
        (".pytest_cache/README.md", "cache\n"),
        (".coverage", "coverage\n"),
    ],
)
@pytest.mark.asyncio
async def test_promotion_prepare_allows_ignored_infrastructure_artifacts(
    promotion_repo, relative_path: str, content: str,
) -> None:
    service, _store, project, workflow, _base_branch, _workflow_head = promotion_repo
    artifact = project / relative_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(content, encoding="utf-8")

    preview = await service.prepare_git_promotion("project-a", workflow_id=workflow)

    assert preview.ready is True
    assert preview.files_changed == ["feature.py"]
    assert relative_path.replace("\\", "/") not in preview.promotion_fingerprint


@pytest.mark.asyncio
async def test_promotion_prepare_blocks_untracked_real_file(promotion_repo) -> None:
    service, _store, project, workflow, _base_branch, _workflow_head = promotion_repo
    (project / "secret.txt").write_text("manual\n", encoding="utf-8")

    with pytest.raises(GitWorkspaceDirtyConflict):
        await service.prepare_git_promotion("project-a", workflow_id=workflow)


@pytest.mark.asyncio
async def test_promotion_prepare_blocks_modified_tracked_file(promotion_repo) -> None:
    service, _store, project, workflow, _base_branch, _workflow_head = promotion_repo
    (project / "README.md").write_text("modified after commit\n", encoding="utf-8")

    with pytest.raises(GitWorkspaceDirtyConflict):
        await service.prepare_git_promotion("project-a", workflow_id=workflow)


@pytest.mark.asyncio
async def test_promotion_prepare_blocks_staged_real_change(promotion_repo) -> None:
    service, _store, project, workflow, _base_branch, _workflow_head = promotion_repo
    (project / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
    git(project, "add", "unexpected.txt")

    with pytest.raises(GitWorkspaceDirtyConflict):
        await service.prepare_git_promotion("project-a", workflow_id=workflow)


@pytest.mark.asyncio
async def test_promotion_merge_revalidates_and_allows_ignored_infrastructure(promotion_repo) -> None:
    service, _store, project, workflow, base_branch, workflow_head = promotion_repo
    preview = await service.prepare_git_promotion("project-a", workflow_id=workflow)
    await service.approve_git_promotion(workflow)
    artifact = project / ".venv" / "pyvenv.cfg"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("home = python\n", encoding="utf-8")

    result = await service.merge_git_workflow_branch(
        "project-a", workflow_id=workflow, approval_id=preview.approval_id,
    )

    assert result.result_commit == workflow_head
    assert result.files == ["feature.py"]
    assert git(project, "branch", "--show-current") == base_branch


@pytest.mark.asyncio
async def test_promotion_merge_revalidates_and_blocks_real_dirty_file(promotion_repo) -> None:
    service, _store, project, workflow, _base_branch, _workflow_head = promotion_repo
    preview = await service.prepare_git_promotion("project-a", workflow_id=workflow)
    await service.approve_git_promotion(workflow)
    (project / "secret.txt").write_text("manual\n", encoding="utf-8")

    with pytest.raises(GitWorkspaceDirtyConflict):
        await service.merge_git_workflow_branch(
            "project-a", workflow_id=workflow, approval_id=preview.approval_id,
        )


@pytest.fixture
async def unborn_promotion_repo(tmp_path: Path):
    workspace = tmp_path / "workspace"
    project = workspace / "unborn-project"
    project.mkdir(parents=True)
    git(project, "init")
    git(project, "config", "user.name", "Unborn Promotion")
    git(project, "config", "user.email", "unborn@example.com")
    database = tmp_path / "unborn-promotion.sqlite"
    store = GitAuditStore(database)
    await store.initialize()
    workflow = "431bc1de-8761-4c00-a784-7b95c7171880"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE workflow_registry("
            "thread_id TEXT PRIMARY KEY,project_name TEXT,terminal_status TEXT,tests_passed INTEGER)"
        )
        connection.execute(
            "INSERT INTO workflow_registry VALUES(?,?,?,?)",
            (workflow, "unborn-project", "completed", 1),
        )
    service = GitService(workspace, audit_store=store)
    await service.git_init("unborn-project", workflow_id=workflow)
    branch = await service.git_create_workflow_branch("unborn-project", workflow_id=workflow)
    (project / "feature.py").write_text("value = 1\n", encoding="utf-8")
    await service.git_stage("unborn-project", ["feature.py"], workflow_id=workflow)
    commit_preview = await service.prepare_git_commit(
        "unborn-project", "feat: initial workflow commit", workflow_id=workflow,
    )
    await service.approve_git_commit(workflow)
    commit = await service.git_commit(
        "unborn-project", workflow_id=workflow, approval_id=commit_preview.approval_id,
    )
    return service, store, project, workflow, branch, commit


@pytest.mark.asyncio
async def test_unborn_base_promotion_preview_uses_null_base_and_fast_forward(unborn_promotion_repo):
    service, _store, project, workflow, branch, commit = unborn_promotion_repo

    preview = await service.prepare_git_promotion("unborn-project", workflow_id=workflow)

    assert branch.base_branch == "master"
    assert branch.base_commit is None
    assert preview.base_branch == "master"
    assert preview.base_commit_at_branch_creation is None
    assert preview.current_base_commit is None
    assert preview.base_advanced is False
    assert preview.workflow_branch == "workflow/431bc1de"
    assert preview.workflow_head == commit.commit
    assert preview.commits_ahead == 1
    assert preview.commits_behind == 0
    assert preview.conflict_state == "clean"
    assert preview.merge_strategy_candidate == "fast_forward"
    assert preview.ready is True
    assert git(project, "branch", "--show-current") == "workflow/431bc1de"


@pytest.mark.asyncio
async def test_unborn_base_promotion_merge_creates_base_branch(unborn_promotion_repo):
    service, _store, project, workflow, _branch, commit = unborn_promotion_repo
    preview = await service.prepare_git_promotion("unborn-project", workflow_id=workflow)
    await service.approve_git_promotion(workflow)

    result = await service.merge_git_workflow_branch(
        "unborn-project", workflow_id=workflow, approval_id=preview.approval_id,
    )

    assert result.strategy == "fast_forward"
    assert result.previous_base_commit is None
    assert result.result_commit == commit.commit
    assert git(project, "branch", "--show-current") == "master"
    assert git(project, "rev-parse", "master") == commit.commit


@pytest.mark.asyncio
async def test_unborn_base_promotion_stale_when_base_appears_after_prepare(unborn_promotion_repo):
    service, _store, project, workflow, _branch, _commit = unborn_promotion_repo
    preview = await service.prepare_git_promotion("unborn-project", workflow_id=workflow)
    await service.approve_git_promotion(workflow)
    git(project, "switch", "--orphan", "master")
    (project / "feature.py").unlink(missing_ok=True)
    (project / "base.py").write_text("base = 1\n", encoding="utf-8")
    git(project, "add", "base.py")
    git(project, "commit", "-m", "base appears")
    base_commit = git(project, "rev-parse", "HEAD")
    git(project, "switch", "workflow/431bc1de")

    with pytest.raises(GitPromotionStale):
        await service.merge_git_workflow_branch(
            "unborn-project", workflow_id=workflow, approval_id=preview.approval_id,
        )

    current = await service.prepare_git_promotion("unborn-project", workflow_id=workflow)
    assert current.base_commit_at_branch_creation is None
    assert current.current_base_commit == base_commit
    assert current.base_advanced is True


@pytest.mark.asyncio
async def test_base_advancement_uses_merge_commit_without_agent_destination(promotion_repo):
    service, _store, project, workflow, base_branch, workflow_head = promotion_repo
    git(project, "switch", base_branch)
    (project / "base.py").write_text("base = 2\n", encoding="utf-8")
    git(project, "add", "base.py")
    git(project, "commit", "-m", "advance base")
    advanced = git(project, "rev-parse", "HEAD")
    durable = await service.audit_store.get_workflow_state(workflow)  # type: ignore[union-attr]
    git(project, "switch", durable["workflow_branch"])

    preview = await service.prepare_git_promotion("project-a", workflow_id=workflow)
    assert preview.base_advanced is True
    assert preview.current_base_commit == advanced
    assert preview.merge_strategy_candidate == "merge_commit"
    assert preview.commits_behind == 1
    await service.approve_git_promotion(workflow)
    result = await service.merge_git_workflow_branch(
        "project-a", workflow_id=workflow, approval_id=preview.approval_id,
    )

    assert result.strategy == "merge_commit"
    assert git(project, "rev-list", "--parents", "-n", "1", result.result_commit).split() == [
        result.result_commit, advanced, workflow_head,
    ]
    with pytest.raises(GitOperationNotAllowed):
        service._run(project, ["merge", "release/prod"])


@pytest.mark.asyncio
async def test_conflicts_block_approval_without_mutating_worktree(promotion_repo):
    service, _store, project, workflow, base_branch, _head = promotion_repo
    durable = await service.audit_store.get_workflow_state(workflow)  # type: ignore[union-attr]
    git(project, "switch", base_branch)
    (project / "README.md").write_text("base changed\n", encoding="utf-8")
    git(project, "add", "README.md")
    git(project, "commit", "-m", "base conflict")
    git(project, "switch", durable["workflow_branch"])
    (project / "README.md").write_text("workflow changed\n", encoding="utf-8")
    git(project, "add", "README.md")
    git(project, "commit", "-m", "workflow conflict")
    workflow_head = git(project, "rev-parse", "HEAD")
    await service.audit_store.upsert_workflow_state(  # type: ignore[union-attr]
        workflow, "project-a", {"commit_sha": workflow_head, "head_commit": workflow_head}
    )

    preview = await service.prepare_git_promotion("project-a", workflow_id=workflow)

    assert preview.ready is False and preview.state == "conflicts"
    assert preview.conflicting_files == ["README.md"]
    assert git(project, "branch", "--show-current") == durable["workflow_branch"]
    assert git(project, "status", "--porcelain") == ""
    with pytest.raises(GitApprovalRequired, match="awaiting approval"):
        await service.approve_git_promotion(workflow)


@pytest.mark.asyncio
async def test_changed_base_makes_approved_promotion_stale(promotion_repo):
    service, _store, project, workflow, base_branch, _head = promotion_repo
    preview = await service.prepare_git_promotion("project-a", workflow_id=workflow)
    await service.approve_git_promotion(workflow)
    durable = await service.audit_store.get_workflow_state(workflow)  # type: ignore[union-attr]
    git(project, "switch", base_branch)
    (project / "late.py").write_text("late = True\n", encoding="utf-8")
    git(project, "add", "late.py")
    git(project, "commit", "-m", "late base advancement")
    git(project, "switch", durable["workflow_branch"])

    with pytest.raises(GitPromotionStale):
        await service.merge_git_workflow_branch(
            "project-a", workflow_id=workflow, approval_id=preview.approval_id,
        )
    assert (await service.get_git_promotion(workflow)).state == "stale"
    assert git(project, "branch", "--show-current") == durable["workflow_branch"]


@pytest.mark.asyncio
async def test_rejection_preserves_branch_and_allows_new_deterministic_preview(promotion_repo):
    service, _store, project, workflow, _base, workflow_head = promotion_repo
    first = await service.prepare_git_promotion("project-a", workflow_id=workflow)
    await service.reject_git_promotion(workflow, reason="Not yet")
    rejected = await service.get_git_promotion(workflow)
    second = await service.prepare_git_promotion("project-a", workflow_id=workflow)

    assert rejected.state == "rejected" and rejected.rejection_reason == "Not yet"
    assert second.promotion_id != first.promotion_id
    assert second.promotion_fingerprint == first.promotion_fingerprint
    assert git(project, "rev-parse", "HEAD") == workflow_head


@pytest.mark.asyncio
async def test_promotion_api_prepares_reads_and_merges_through_existing_approval(promotion_repo):
    service, _store, _project, workflow, _base, workflow_head = promotion_repo

    class Query:
        async def get_snapshot(self, thread_id):
            return WorkflowSnapshotResponse(
                thread_id=thread_id, checkpoint_id="cp", project_name="project-a",
                workflow_intent="create_project", terminal_status="completed",
                interrupted=False, pending_operation=None, pending_tool=None,
                planning={}, implementation={}, testing={}, supervisor={},
                created_at=None, updated_at=None,
            )

    services = SimpleNamespace(query=Query(), git=service, approvals=None)

    @asynccontextmanager
    async def factory():
        yield services

    with TestClient(create_app(factory)) as client:
        prepared = client.post(f"/api/workflows/{workflow}/git/promotion/prepare")
        assert prepared.status_code == 200 and prepared.json()["ready"] is True
        status = client.get(f"/api/workflows/{workflow}/git/promotion")
        assert status.json()["state"] == "awaiting_approval"
        approved = client.post(
            f"/api/workflows/{workflow}/approve", json={"reason": "Promote"}
        )
        assert approved.status_code == 202
        assert approved.json()["operation"] == "git_merge"
        completed = client.get(f"/api/workflows/{workflow}/git/promotion").json()
        assert completed["state"] == "completed"
        assert completed["result"]["result_commit"] == workflow_head
