from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.git_service import GitPathViolation, GitService
from api.services.git_store import GitAuditStore


async def validate(workspace: Path, project_id: str, database: Path) -> dict:
    workflow_id = "phase619-workflow"
    store = GitAuditStore(database)
    await store.initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_registry("
            "thread_id TEXT PRIMARY KEY, project_name TEXT)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO workflow_registry VALUES (?,?)",
            (workflow_id, project_id),
        )
    service = GitService(workspace, audit_store=store)
    info = await service.get_repository_info(
        project_id, workflow_id=workflow_id, agent_name="ManualValidation"
    )
    status = await service.git_status(
        project_id, workflow_id=workflow_id, agent_name="ManualValidation"
    )
    diff = await service.git_diff(
        project_id, workflow_id=workflow_id, agent_name="ManualValidation"
    )
    log = await service.git_log(
        project_id, workflow_id=workflow_id, agent_name="ManualValidation"
    )
    branches = await service.git_branches(
        project_id, workflow_id=workflow_id, agent_name="ManualValidation"
    )
    blocked = False
    try:
        await service.git_status("project-b", workflow_id=workflow_id)
    except GitPathViolation:
        blocked = True
    return {
        "info": info.model_dump(mode="json"),
        "status": status.model_dump(mode="json"),
        "diff_files": [item.path for item in diff.files],
        "log_subjects": [item.subject for item in log],
        "current_branch": branches.current,
        "control_negative_blocked": blocked,
        "audit_count": len(await store.list(workflow_id=workflow_id)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("workspace"))
    parser.add_argument("--project-id", default="phase-6-19-git-validation")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/phase619-git-validation.sqlite"),
    )
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(validate(
        arguments.workspace, arguments.project_id, arguments.database
    )), indent=2))


if __name__ == "__main__":
    main()
