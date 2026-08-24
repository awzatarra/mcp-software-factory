from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.git_service import GitApprovalStale, GitService
from api.services.git_store import GitAuditStore


def git(project: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=project, shell=False, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


async def main() -> None:
    workspace = ROOT / "workspace"
    project_id = "phase-6-19-git-mutation-validation"
    workflow_id = "a2b27b16-1111-2222-3333-444444444444"
    project = workspace / project_id
    project.mkdir(parents=True, exist_ok=True)
    database = ROOT / "data" / "git-mutation-validation.sqlite"
    store = GitAuditStore(database)
    await store.initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_registry(thread_id TEXT PRIMARY KEY, project_name TEXT)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO workflow_registry VALUES(?,?)", (workflow_id, project_id)
        )
    service = GitService(workspace, audit_store=store)
    initialized = await service.git_init(project_id, workflow_id=workflow_id, agent_name="Developer")
    git(project, "config", "user.name", "Software Factory Validation")
    git(project, "config", "user.email", "validation@example.invalid")
    branch = await service.git_create_workflow_branch(
        project_id, workflow_id=workflow_id, agent_name="Developer"
    )
    readme = project / "README.md"
    run_marker = datetime.now(UTC).isoformat()
    readme.write_text(f"# Git mutation validation\n\nRun: {run_marker}\n", encoding="utf-8")
    await service.git_stage(project_id, ["README.md"], workflow_id=workflow_id, agent_name="Developer")
    preview = await service.prepare_git_commit(
        project_id, "feat: add workflow Git validation",
        workflow_id=workflow_id, agent_name="Developer",
    )
    result = await service.resolve_commit_approval(workflow_id, approved=True, actor="manual-validator")

    readme.write_text(f"# Git mutation validation\n\nRun: {run_marker}\nstale candidate\n", encoding="utf-8")
    await service.git_stage(project_id, ["README.md"], workflow_id=workflow_id, agent_name="Repair")
    stale_preview = await service.prepare_git_commit(
        project_id, "fix: stale approval validation", workflow_id=workflow_id, agent_name="Repair"
    )
    await service.approve_git_commit(workflow_id, actor="manual-validator")
    readme.write_text(f"# Git mutation validation\n\nRun: {run_marker}\nchanged after approval\n", encoding="utf-8")
    await service.git_stage(project_id, ["README.md"], workflow_id=workflow_id, agent_name="Repair")
    stale_blocked = False
    try:
        await service.git_commit(
            project_id, approval_id=stale_preview.approval_id,
            workflow_id=workflow_id, agent_name="Repair",
        )
    except GitApprovalStale:
        stale_blocked = True
    audits = await store.list(workflow_id=workflow_id)

    print(json.dumps({
        "initialized": initialized.initialized,
        "repository": True,
        "workflow_branch": branch.branch,
        "preview_status": preview.status,
        "commit": result.commit if result else None,
        "head": git(project, "rev-parse", "HEAD"),
        "log_contains_commit": "feat: add workflow Git validation" in git(project, "log", "--oneline"),
        "stale_approval_blocked": stale_blocked,
        "staged_after_stale": git(project, "diff", "--cached", "--name-only").splitlines(),
        "recent_audit_operations": [item["operation"] for item in audits[-8:]],
        "audit_contains_patch": any("patch" in item for item in audits),
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
