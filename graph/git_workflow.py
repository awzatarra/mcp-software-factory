from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from git_dirty_paths import classify_dirty_paths, normalize_repository_path
from graph.nodes import GraphDependencies, _execute
from graph.state import SoftwareFactoryState
from servers.filesystem_server import get_workspace_root


GIT_TOOLS = (
    "git__init", "git__create_workflow_branch", "git__git_status",
    "git__stage", "git__prepare_commit", "git__approve_commit",
    "git__reject_commit", "git__commit",
)


def git_tools_available(dependencies: GraphDependencies) -> bool:
    executor = dependencies.tool_executor
    explicitly_enabled = getattr(executor, "git_integration_enabled", False)
    if executor.__class__.__name__ != "HostToolExecutor" and not explicitly_enabled:
        return False
    try:
        for name in GIT_TOOLS:
            dependencies.tool_executor.openai_tool(name)
    except (KeyError, ValueError):
        return False
    return True


def _project(state: SoftwareFactoryState) -> str:
    return str(state.get("created_project_name") or state.get("project_name") or "")


def _workflow(state: SoftwareFactoryState) -> str:
    return str(state.get("workflow_id") or "")


def _payload(outcome: Any, operation: str) -> dict[str, Any]:
    payload = outcome.payload
    if outcome.is_error or not isinstance(payload, dict) or payload.get("success") is False:
        code = payload.get("error_code") if isinstance(payload, dict) else None
        raise RuntimeError(str(code or f"{operation}_failed"))
    return payload


def _relative_owned_path(project: str, path: str) -> str | None:
    normalized = normalize_repository_path(path)
    if normalized is None:
        return None
    prefix = f"{project}/"
    if normalized.startswith(prefix):
        normalized = normalized[len(prefix):]
    return normalize_repository_path(normalized)


def _project_root_for_state(state: SoftwareFactoryState, project: str) -> Path:
    configured = state.get("project_root") or state.get("_project_root")
    if configured:
        return Path(str(configured)).resolve()
    return (get_workspace_root() / project).resolve()


def attributed_paths(state: SoftwareFactoryState, status: dict[str, Any]) -> tuple[list[str], list[str]]:
    project = _project(state)
    repair_files = state.get("files_updated_during_repair") or []
    phase = "repair" if state.get("repair_attempts", 0) and repair_files else "implementation"
    raw_owned = repair_files if phase == "repair" else [
        *(state.get("generated_files") or []),
        *(state.get("files_updated_during_repair") or []),
    ]
    owned = {
        safe for item in raw_owned
        if (safe := _relative_owned_path(project, str(item))) is not None
    }
    classification = classify_dirty_paths(_project_root_for_state(state, project), status, owned)
    return classification.allowed_files, classification.unrelated_files


def commit_message(state: SoftwareFactoryState, phase: str) -> str:
    if phase == "repair":
        detail = str(state.get("test_failure_summary") or state.get("failure_message") or "validated tests")
        detail = re.sub(r"\s+", " ", detail).strip()[:150]
        return f"fix: repair {detail}"
    project = _project(state).replace("-", " ").strip() or "project"
    return f"feat: implement {project}"[:200]


async def prepare_git_workflow_node(
    state: SoftwareFactoryState, dependencies: GraphDependencies,
) -> dict[str, Any]:
    project, workflow = _project(state), _workflow(state)
    if not project or not workflow:
        return {"git_workflow_state": "unavailable", "git_commit_status": "unavailable"}
    if state.get("git_workflow_state") in {None, "not_initialized"}:
        init_outcome = await _execute(
            "git__init", {"project_id": project, "workflow_id": workflow, "agent_name": "Developer"},
            state, dependencies, approval_mode="already_approved", node_name="prepare_git_workspace",
        )
        init_payload = init_outcome.payload if isinstance(init_outcome.payload, dict) else {}
        if init_outcome.is_error or init_payload.get("success") is False:
            status = str(init_payload.get("status") or init_payload.get("failure_type") or "git_initialization_failed")
            return {
                "git_state": "git_unavailable",
                "git_workflow_state": "git_initialization_failed",
                "git_commit_status": "unavailable",
                "terminal_status": "infrastructure_failed",
                "failure_type": "git_initialization_failed" if status == "mcp_timeout" else status,
                "failure_stage": "git.init",
                "failure_message": str(
                    init_payload.get("message")
                    or init_payload.get("error")
                    or "Git initialization failed."
                ),
            }
        init = _payload(init_outcome, "git_init")
        status = _payload(await _execute(
            "git__git_status", {"project_id": project, "workflow_id": workflow, "agent_name": "Developer"},
            state, dependencies, node_name="prepare_git_workspace",
        ), "git_status")
        owned, unrelated = attributed_paths(state, status)
        if unrelated:
            return {
                "git_workflow_state": "unavailable", "git_commit_status": "unavailable",
                "terminal_status": "implementation_failed",
                "failure_type": "git_workspace_dirty_conflict",
                "failure_stage": "prepare_git_workspace",
                "failure_message": "Existing working tree contains changes not attributed to this workflow.",
            }
        branch_arguments = {
            "project_id": project, "workflow_id": workflow, "agent_name": "Developer",
            "allowed_dirty_paths": owned,
        }
        if state.get("fork_origin_checkpoint_id"):
            branch_arguments.update({
                "fork_origin_workflow": workflow,
                "fork_origin_commit": state.get("git_head_commit"),
                "branch_identity": f"{workflow}:fork:{state['fork_origin_checkpoint_id']}",
            })
        branch_outcome = await _execute(
            "git__create_workflow_branch",
            branch_arguments,
            state, dependencies, approval_mode="already_approved", node_name="prepare_git_workspace",
        )
        branch_payload = branch_outcome.payload if isinstance(branch_outcome.payload, dict) else {}
        if branch_outcome.is_error or branch_payload.get("success") is False:
            failure_type = str(
                branch_payload.get("failure_type")
                or branch_payload.get("error_code")
                or branch_payload.get("status")
                or "git_branch_failed"
            )
            return {
                "git_workflow_state": "unavailable",
                "git_commit_status": "unavailable",
                "terminal_status": "implementation_failed",
                "failure_type": failure_type,
                "failure_stage": "prepare_git_workspace",
                "failure_message": str(
                    branch_payload.get("message")
                    or branch_payload.get("error")
                    or "Git workflow branch could not be prepared."
                ),
            }
        branch = _payload(branch_outcome, "git_branch")
        return {
            "git_state": "ready", "git_workflow_state": "workspace_prepared",
            "git_repository": True, "git_base_branch": branch.get("base_branch"),
            "git_base_commit": branch.get("base_commit"),
            "git_branch": branch.get("branch"), "git_workflow_branch": branch.get("branch"),
            "git_head_commit": init.get("head_commit"), "git_commit_status": None,
        }

    if state.get("tests_passed") is not True:
        return {}
    status = _payload(await _execute(
        "git__git_status", {"project_id": project, "workflow_id": workflow, "agent_name": "Developer"},
        state, dependencies, node_name="prepare_git_commit",
    ), "git_status")
    owned, unrelated = attributed_paths(state, status)
    phase = "repair" if state.get("repair_attempts", 0) and state.get("files_updated_during_repair") else "implementation"
    if not owned:
        if phase == "repair":
            return {
                "git_workflow_state": "no_changes",
                "git_state": "ready",
                "git_commit_status": "no_changes",
                "ci_repair_state": "failed",
                "terminal_status": "ci_failed",
                "failure_type": "ci_repair_no_changes",
                "failure_stage": "prepare_git_commit",
                "failure_message": "Repair validation passed locally but did not produce a new attributed change for commit.",
            }
        return {
            "git_workflow_state": "no_changes", "git_state": "ready",
            "git_commit_status": "no_changes",
        }
    actor = "Repair" if phase == "repair" else "Developer"
    _payload(await _execute(
        "git__stage", {"project_id": project, "workflow_id": workflow, "paths": owned, "agent_name": actor},
        state, dependencies, approval_mode="already_approved", node_name=f"prepare_{phase}_commit",
    ), "git_stage")
    preview = _payload(await _execute(
        "git__prepare_commit", {
            "project_id": project, "workflow_id": workflow,
            "proposed_message": commit_message(state, phase), "agent_name": actor,
        }, state, dependencies, approval_mode="already_approved", node_name=f"prepare_{phase}_commit",
    ), "git_prepare_commit")
    return {
        "git_state": "awaiting_approval", "git_workflow_state": "awaiting_approval",
        "git_staged_files": owned, "git_commit_preview": preview,
        "git_commit_approval_id": preview.get("approval_id"),
        "git_commit_status": "awaiting_approval", "git_commit_phase": phase,
        "pending_operation": "git_commit", "pending_tool_name": "git__commit",
        "pending_tool_arguments": {
            "project_id": project, "workflow_id": workflow,
            "approval_id": preview.get("approval_id"), "agent_name": actor,
            "phase": phase,
        },
        "pending_approval_preview": {**preview, "phase": phase, "unrelated_files": unrelated},
        "pending_approval_status": "waiting", "approval_reason": None,
    }


async def execute_git_commit_node(
    state: SoftwareFactoryState, dependencies: GraphDependencies,
) -> dict[str, Any]:
    project, workflow = _project(state), _workflow(state)
    phase = str(state.get("git_commit_phase") or "implementation")
    actor = "Repair" if phase == "repair" else "Developer"
    approval_id = str(state.get("git_commit_approval_id") or "")
    _payload(await _execute(
        "git__approve_commit", {"workflow_id": workflow, "actor": "user"},
        state, dependencies, approval_mode="already_approved", node_name="git_commit_approval",
    ), "git_approve_commit")
    result = _payload(await _execute(
        "git__commit", {"project_id": project, "workflow_id": workflow,
                         "approval_id": approval_id, "agent_name": actor,
                         "phase": phase},
        state, dependencies, approval_mode="already_approved", node_name="execute_git_commit",
    ), "git_commit")

    return git_commit_state_updates(state, result)


def git_commit_state_updates(
    state: SoftwareFactoryState,
    result: dict[str, Any],
    *,
    recovery: bool = False,
) -> dict[str, Any]:
    """Build the canonical graph update after a durable commit side effect."""
    phase = str(state.get("git_commit_phase") or "implementation")
    actor = "Repair" if phase == "repair" else "Developer"
    history = list(state.get("git_commit_history") or [])
    if not any(item.get("sha") == result.get("commit") for item in history):
        history.append({
            "phase": phase, "agent": actor, "sha": result.get("commit"),
            "message": result.get("message"), "files": result.get("files") or [],
        })
    repair_updates: dict[str, Any] = {}
    if phase == "repair":
        if result.get("commit") == state.get("ci_repair_source_commit"):
            repair_updates.update({
                "ci_repair_state": "failed",
                "terminal_status": "ci_failed",
                "failure_type": "ci_repair_commit_not_advanced",
                "failure_stage": "git_repair_commit",
                "failure_message": "Repair commit did not advance the CI source commit.",
            })
        else:
            repair_updates.update({
                "ci_repair_state": "running",
                "ci_repair_target_commit": result.get("commit"),
                "ci_validated_commit": None,
                "ci_promotion_eligible": False,
                "ci_promotion_eligibility": None,
                "git_promotion_state": "not_started",
                "git_promotion_preview": None,
                "git_promotion_approval_id": None,
            })
    updates: dict[str, Any] = {
        "git_state": "committed", "git_workflow_state": "committed",
        "git_head_commit": result.get("commit"), "git_commit_status": "committed",
        "git_developer_commit_sha": result.get("commit") if phase == "implementation" else state.get("git_developer_commit_sha"),
        "git_repair_commit_sha": result.get("commit") if phase == "repair" else state.get("git_repair_commit_sha"),
        "git_commit_history": history,
        "pending_operation": None, "pending_tool_name": None,
        "pending_tool_arguments": None, "pending_approval_preview": None,
        "pending_approval_status": "none",
        **repair_updates,
    }
    if recovery:
        updates.update({
            "terminal_status": None,
            "failure_type": None,
            "failure_stage": None,
            "failure_message": None,
        })
        updates.update(repair_updates)
    return updates


async def reject_git_commit_node(
    state: SoftwareFactoryState, dependencies: GraphDependencies,
) -> dict[str, Any]:
    await _execute(
        "git__reject_commit", {"workflow_id": _workflow(state),
                                "reason": state.get("approval_reason"), "actor": "user"},
        state, dependencies, approval_mode="already_approved", node_name="reject_git_commit",
    )
    return {
        "git_state": "rejected", "git_workflow_state": "rejected",
        "git_commit_status": "rejected",
        "final_response": "Git commit rejected. Staged files were preserved for review.",
    }


async def prepare_git_promotion_node(
    state: SoftwareFactoryState, dependencies: GraphDependencies,
) -> dict[str, Any]:
    preview = _payload(await _execute(
        "git__prepare_promotion", {
            "project_id": _project(state), "workflow_id": _workflow(state),
            "actor": "Orchestrator",
        }, state, dependencies, approval_mode="already_approved",
        node_name="prepare_git_promotion",
    ), "git_promotion_prepare")
    promotion_state = str(preview.get("state") or "unavailable")
    if promotion_state == "already_promoted":
        promotion_state = "completed"
    updates: dict[str, Any] = {
        "git_promotion_state": promotion_state,
        "git_promotion_id": preview.get("promotion_id"),
        "git_promotion_preview": preview,
        "git_promotion_approval_id": preview.get("approval_id"),
        "git_promotion_strategy": preview.get("merge_strategy_candidate"),
        "git_promotion_conflicts": preview.get("conflicting_files") or [],
    }
    if preview.get("ready"):
        updates.update({
            "git_promotion_state": "awaiting_approval",
            "pending_operation": "git_merge",
            "pending_tool_name": "git__merge_workflow_branch",
            "pending_tool_arguments": {
                "project_id": _project(state), "workflow_id": _workflow(state),
                "approval_id": preview.get("approval_id"), "actor": "user",
            },
            "pending_approval_preview": preview,
            "pending_approval_status": "waiting",
            "approval_reason": None,
        })
    elif state.get("git_promotion_required") and promotion_state in {"conflicts", "unavailable"}:
        updates.update({
            "terminal_status": "infrastructure_failed",
            "failure_type": "git_promotion_required_not_completed",
            "failure_stage": "prepare_git_promotion",
            "failure_message": "Required Git promotion could not be prepared safely.",
        })
    return updates


async def execute_git_promotion_node(
    state: SoftwareFactoryState, dependencies: GraphDependencies,
) -> dict[str, Any]:
    workflow = _workflow(state)
    await _execute(
        "git__approve_promotion", {"workflow_id": workflow, "actor": "user"},
        state, dependencies, approval_mode="already_approved",
        node_name="git_promotion_approval",
    )
    result = _payload(await _execute(
        "git__merge_workflow_branch", {
            "project_id": _project(state), "workflow_id": workflow,
            "approval_id": state.get("git_promotion_approval_id"), "actor": "user",
        }, state, dependencies, approval_mode="already_approved",
        node_name="execute_git_promotion",
    ), "git_promotion_merge")
    return {
        "git_promotion_state": "completed",
        "git_promotion_result_commit": result.get("result_commit"),
        "git_promotion_strategy": result.get("strategy"),
        "git_head_commit": result.get("result_commit"),
        "pending_operation": None, "pending_tool_name": None,
        "pending_tool_arguments": None, "pending_approval_preview": None,
        "pending_approval_status": "none",
    }


async def reject_git_promotion_node(
    state: SoftwareFactoryState, dependencies: GraphDependencies,
) -> dict[str, Any]:
    await _execute(
        "git__reject_promotion", {
            "workflow_id": _workflow(state), "reason": state.get("approval_reason"),
            "actor": "user",
        }, state, dependencies, approval_mode="already_approved",
        node_name="reject_git_promotion",
    )
    optional = not bool(state.get("git_promotion_required"))
    return {
        "git_promotion_state": "rejected",
        "terminal_status": None if optional else state.get("terminal_status"),
        "user_cancelled": False if optional else state.get("user_cancelled"),
        "failure_type": None if optional else state.get("failure_type"),
        "failure_message": None if optional else state.get("failure_message"),
    }
