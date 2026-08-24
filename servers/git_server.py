from __future__ import annotations

from typing import Any, Awaitable, Callable

from mcp.server.fastmcp import FastMCP

from api.services.git_service import GitError, GitService
from api.services.git_store import GitAuditStore
from api.services.observability_service import ObservabilityService
from api.services.observability_store import ObservabilityStore
from servers.filesystem_server import get_workspace_root
from streaming.sqlite_store import workflow_event_store_path

mcp = FastMCP("software-factory-git")
_service: GitService | None = None

async def get_service() -> GitService:
    global _service
    if _service is None:
        database_path = workflow_event_store_path()
        observability_store = ObservabilityStore(database_path)
        observability = ObservabilityService(observability_store)
        await observability.initialize()
        _service = GitService(
            get_workspace_root(),
            audit_store=GitAuditStore(database_path),
            observability=observability,
        )
        await _service.initialize()
    return _service


async def _run(action: Callable[[GitService], Awaitable[Any]]) -> dict[str, Any] | list[dict[str, Any]]:
    try:
        result = await action(await get_service())
        if isinstance(result, list):
            return [item.model_dump(mode="json") for item in result]
        return result.model_dump(mode="json")
    except GitError as exc:
        return {
            "success": False,
            "error_code": exc.code,
            "message": str(exc),
        }


@mcp.tool()
async def get_repository_info(
    project_id: str,
    workflow_id: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Inspect the authorized project's local repository without modifying it."""
    return await _run(lambda service: service.get_repository_info(
        project_id, workflow_id=workflow_id, agent_name=agent_name
    ))  # type: ignore[return-value]


@mcp.tool()
async def git_status(
    project_id: str,
    workflow_id: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Return structured working-tree status for one authorized project."""
    return await _run(lambda service: service.git_status(
        project_id, workflow_id=workflow_id, agent_name=agent_name
    ))  # type: ignore[return-value]


@mcp.tool()
async def git_diff(
    project_id: str,
    staged: bool = False,
    workflow_id: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Return a bounded structured working-tree or staged diff."""
    return await _run(lambda service: service.git_diff(
        project_id, staged=staged, workflow_id=workflow_id, agent_name=agent_name
    ))  # type: ignore[return-value]


@mcp.tool()
async def git_log(
    project_id: str,
    limit: int = 20,
    workflow_id: str | None = None,
    agent_name: str | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return bounded structured local commit history."""
    return await _run(lambda service: service.git_log(
        project_id, limit=limit, workflow_id=workflow_id, agent_name=agent_name
    ))


@mcp.tool()
async def git_branches(
    project_id: str,
    workflow_id: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """List local branches only."""
    return await _run(lambda service: service.git_branches(
        project_id, workflow_id=workflow_id, agent_name=agent_name
    ))  # type: ignore[return-value]


@mcp.tool()
async def git_head(
    project_id: str,
    workflow_id: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Return the current HEAD without changing it."""
    return await _run(lambda service: service.git_head(
        project_id, workflow_id=workflow_id, agent_name=agent_name
    ))  # type: ignore[return-value]


@mcp.tool()
async def git_file_history(
    project_id: str,
    path: str,
    limit: int = 20,
    workflow_id: str | None = None,
    agent_name: str | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return commit history for a safe project-relative path."""
    return await _run(lambda service: service.git_file_history(
        project_id, path, limit=limit, workflow_id=workflow_id, agent_name=agent_name
    ))


@mcp.tool(name="init")
async def git_init(project_id: str, workflow_id: str, agent_name: str | None = None) -> dict[str, Any]:
    """Initialize the authorized project repository without accepting a path."""
    return await _run(lambda service: service.git_init(
        project_id, workflow_id=workflow_id, agent_name=agent_name
    ))  # type: ignore[return-value]


@mcp.tool(name="create_workflow_branch")
async def git_create_workflow_branch(
    project_id: str, workflow_id: str, agent_name: str | None = None,
    fork_origin_workflow: str | None = None, fork_origin_commit: str | None = None,
    allowed_dirty_paths: list[str] | None = None,
    branch_identity: str | None = None,
) -> dict[str, Any]:
    """Create or switch to the branch derived from the workflow identity."""
    return await _run(lambda service: service.git_create_workflow_branch(
        project_id, workflow_id=workflow_id, agent_name=agent_name,
        fork_origin_workflow=fork_origin_workflow, fork_origin_commit=fork_origin_commit,
        allowed_dirty_paths=allowed_dirty_paths, branch_identity=branch_identity,
    ))  # type: ignore[return-value]


@mcp.tool(name="stage")
async def git_stage(
    project_id: str, workflow_id: str, paths: list[str], agent_name: str | None = None,
) -> dict[str, Any]:
    """Stage only validated explicit project-relative paths."""
    return await _run(lambda service: service.git_stage(
        project_id, paths, workflow_id=workflow_id, agent_name=agent_name
    ))  # type: ignore[return-value]


@mcp.tool(name="prepare_commit")
async def prepare_git_commit(
    project_id: str, workflow_id: str, proposed_message: str,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Create a durable, fingerprint-bound commit approval preview."""
    return await _run(lambda service: service.prepare_git_commit(
        project_id, proposed_message, workflow_id=workflow_id, agent_name=agent_name
    ))  # type: ignore[return-value]


@mcp.tool(name="commit")
async def git_commit(
    project_id: str, workflow_id: str, approval_id: str,
    agent_name: str | None = None, phase: str = "implementation",
) -> dict[str, Any]:
    """Commit an unchanged staged diff after durable human approval."""
    return await _run(lambda service: service.git_commit(
        project_id, approval_id=approval_id, workflow_id=workflow_id,
        agent_name=agent_name, phase=phase,
    ))  # type: ignore[return-value]


@mcp.tool(name="approve_commit")
async def approve_git_commit(workflow_id: str, actor: str = "user") -> dict[str, Any]:
    """Record the durable human approval consumed by the commit tool."""
    try:
        approval_id = await (await get_service()).approve_git_commit(workflow_id, actor=actor)
        return {"success": True, "approval_id": approval_id}
    except GitError as exc:
        return {"success": False, "error_code": exc.code, "message": str(exc)}


@mcp.tool(name="reject_commit")
async def reject_git_commit(
    workflow_id: str, reason: str | None = None, actor: str = "user",
) -> dict[str, Any]:
    """Record a rejected commit approval without changing files or staging."""
    try:
        approval_id = await (await get_service()).reject_git_commit(
            workflow_id, reason=reason, actor=actor
        )
        return {"success": True, "approval_id": approval_id, "status": "rejected"}
    except GitError as exc:
        return {"success": False, "error_code": exc.code, "message": str(exc)}


@mcp.tool(name="prepare_promotion")
async def prepare_git_promotion(
    project_id: str, workflow_id: str, actor: str = "Orchestrator",
) -> dict[str, Any]:
    """Prepare a durable promotion preview without modifying repository refs."""
    return await _run(lambda service: service.prepare_git_promotion(
        project_id, workflow_id=workflow_id, actor=actor
    ))  # type: ignore[return-value]


@mcp.tool(name="merge_workflow_branch")
async def merge_git_workflow_branch(
    project_id: str, workflow_id: str, approval_id: str,
    actor: str = "user",
) -> dict[str, Any]:
    """Execute a fingerprint-bound workflow promotion after durable approval."""
    return await _run(lambda service: service.merge_git_workflow_branch(
        project_id, workflow_id=workflow_id, approval_id=approval_id, actor=actor
    ))  # type: ignore[return-value]


@mcp.tool(name="approve_promotion")
async def approve_git_promotion(
    workflow_id: str, actor: str = "user",
) -> dict[str, Any]:
    """Record approval for the current durable promotion preview."""
    try:
        approval_id = await (await get_service()).approve_git_promotion(workflow_id, actor=actor)
        return {"success": True, "approval_id": approval_id}
    except GitError as exc:
        return {"success": False, "error_code": exc.code, "message": str(exc)}


@mcp.tool(name="reject_promotion")
async def reject_git_promotion(
    workflow_id: str, reason: str | None = None, actor: str = "user",
) -> dict[str, Any]:
    """Reject a promotion while preserving workflow branch and commits."""
    try:
        approval_id = await (await get_service()).reject_git_promotion(
            workflow_id, reason=reason, actor=actor
        )
        return {"success": True, "approval_id": approval_id, "status": "rejected"}
    except GitError as exc:
        return {"success": False, "error_code": exc.code, "message": str(exc)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
