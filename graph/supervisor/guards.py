from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from graph.state import SoftwareFactoryState
from graph.supervisor.models import SupervisorTarget


GIT_WORKSPACE_PREPARED_STATES = frozenset(
    {"workspace_prepared", "ready", "awaiting_approval", "committed", "no_changes", "unavailable"}
)
GIT_FINAL_STATES = frozenset({"committed", "no_changes", "unavailable"})
GIT_PROMOTION_TERMINAL_STATES = frozenset(
    {"completed", "rejected", "conflicts", "unavailable", "already_promoted"}
)


@dataclass(frozen=True)
class SupervisorProgressFingerprint:
    current_stage: str
    planning_valid: bool
    planning_attempts: int
    workspace_inspected: bool
    implementation_valid: bool
    implementation_attempts: int
    project_created: bool
    environment_prepared: bool
    tests_executed: bool
    tests_passed: bool
    repair_phase: str
    repair_attempts: int
    git_workflow_state: str
    git_commit_status: str | None
    ci_state: str
    ci_validated_commit: str | None

    def as_serializable(self) -> dict[str, Any]:
        return asdict(self)


def supervisor_progress_fingerprint(
    state: SoftwareFactoryState,
) -> SupervisorProgressFingerprint:
    planning = state.get("planning_result") or {}
    implementation = state.get("implementation_result") or {}
    testing = state.get("testing_result") or {}
    current_stage = str(
        state.get("last_completed_stage")
        or state.get("last_completed_node")
        or "intent"
    )
    return SupervisorProgressFingerprint(
        current_stage=current_stage,
        planning_valid=bool(planning.get("valid", state.get("planning_valid", False))),
        planning_attempts=int(planning.get("attempts", state.get("planning_attempts", 0))),
        workspace_inspected=bool(state.get("workspace_inspected")),
        implementation_valid=bool(
            implementation.get("valid", state.get("implementation_valid", False))
        ),
        implementation_attempts=int(
            implementation.get("attempts", state.get("implementation_attempts", 0))
        ),
        project_created=bool(
            implementation.get("project_created", state.get("project_created", False))
        ),
        environment_prepared=bool(
            implementation.get(
                "environment_prepared",
                state.get("environment_prepared", False),
            )
        ),
        tests_executed=bool(
            testing.get("tests_executed", state.get("tests_executed", False))
        ),
        tests_passed=bool(testing.get("tests_passed", state.get("tests_passed", False))),
        repair_phase=str(testing.get("repair_phase", state.get("repair_phase", "not_started"))),
        repair_attempts=int(
            testing.get("repair_attempts", state.get("repair_attempts", 0))
        ),
            git_workflow_state=str(state.get("git_workflow_state", "not_initialized")),
            git_commit_status=state.get("git_commit_status"),
            ci_state=str(state.get("ci_state", "not_started")),
            ci_validated_commit=state.get("ci_validated_commit"),
        )


def approval_blocks_supervisor(state: SoftwareFactoryState) -> bool:
    return state.get("pending_approval_status") == "waiting"


def _planning_valid(state: SoftwareFactoryState) -> bool:
    planning = state.get("planning_result") or {}
    return bool(planning.get("valid", state.get("planning_valid", False)))


def _implementation_valid(state: SoftwareFactoryState) -> bool:
    implementation = state.get("implementation_result") or {}
    return bool(implementation.get("valid", state.get("implementation_valid", False)))


def _project_available(state: SoftwareFactoryState) -> bool:
    implementation = state.get("implementation_result") or {}
    return bool(
        implementation.get(
            "project_created",
            state.get("project_created", False),
        )
        or state.get("project_exists")
    )


def _environment_prepared(state: SoftwareFactoryState) -> bool:
    implementation = state.get("implementation_result") or {}
    return bool(
        implementation.get(
            "environment_prepared",
            state.get("environment_prepared", False),
        )
    )


def _tests_executed(state: SoftwareFactoryState) -> bool:
    testing = state.get("testing_result") or {}
    return bool(testing.get("tests_executed", state.get("tests_executed", False)))


def _tests_passed(state: SoftwareFactoryState) -> bool:
    testing = state.get("testing_result") or {}
    return bool(testing.get("tests_passed", state.get("tests_passed", False)))


def _git_workspace_prepared(state: SoftwareFactoryState) -> bool:
    if not state.get("git_integration_enabled"):
        return True
    git_state = state.get("git_workflow_state")
    return (
        git_state in GIT_WORKSPACE_PREPARED_STATES
        and bool(state.get("git_workflow_branch"))
    )


def _git_commit_complete(state: SoftwareFactoryState) -> bool:
    if not state.get("git_integration_enabled"):
        return True
    repair_commit_pending = bool(
        state.get("git_developer_commit_sha")
        and state.get("files_updated_during_repair")
        and not state.get("git_repair_commit_sha")
    )
    return state.get("git_workflow_state") in GIT_FINAL_STATES and not repair_commit_pending


def _promotion_complete(state: SoftwareFactoryState) -> bool:
    if not state.get("git_promotion_required"):
        return True
    return state.get("git_promotion_state") in {"completed", "already_promoted"}


def _ci_required(state: SoftwareFactoryState) -> bool:
    return bool(state.get("git_integration_enabled") and state.get("ci_promotion_required"))


def _git_target_commit(state: SoftwareFactoryState) -> str | None:
    return (
        state.get("git_repair_commit_sha")
        or state.get("git_developer_commit_sha")
        or state.get("git_head_commit")
    )


def _ci_valid_for_commit(state: SoftwareFactoryState) -> bool:
    if not _ci_required(state):
        return True
    target = _git_target_commit(state)
    return bool(
        target
        and state.get("ci_validated_commit") == target
        and state.get("ci_promotion_eligible") is True
        and state.get("ci_decision") in {"accepted", "accepted_with_warnings"}
    )


def determine_allowed_handoffs(state: SoftwareFactoryState) -> list[SupervisorTarget]:
    if state.get("terminal_status") is not None:
        return [SupervisorTarget.FINALIZE]
    if approval_blocks_supervisor(state):
        return []
    if _tests_passed(state):
        if state.get("git_integration_enabled") and not _git_commit_complete(state):
            return [SupervisorTarget.GIT_WORKFLOW]
        if state.get("git_integration_enabled") and _git_commit_complete(state) and not _ci_valid_for_commit(state):
            return [SupervisorTarget.CI_PIPELINE]
        if (
            state.get("git_auto_prepare_promotion")
            and state.get("git_workflow_state") == "committed"
            and _ci_valid_for_commit(state)
            and state.get("git_promotion_state") not in GIT_PROMOTION_TERMINAL_STATES
        ):
            return [SupervisorTarget.GIT_PROMOTION]
        if _git_workspace_prepared(state) and _git_commit_complete(state) and _promotion_complete(state):
            return [SupervisorTarget.FINALIZE]
        if state.get("git_integration_enabled"):
            return [SupervisorTarget.GIT_WORKFLOW]
        return [SupervisorTarget.FINALIZE]
    if state.get("test_infrastructure_failed"):
        return [SupervisorTarget.FINALIZE]

    intent = state.get("workflow_intent")
    if intent == "review_existing_project" and not state.get("workspace_inspected"):
        return [SupervisorTarget.INSPECT_WORKSPACE]

    planning_started = bool(
        state.get("planning_result")
        or state.get("planning_attempts", 0)
        or state.get("planning_valid")
    )
    if intent != "review_existing_project" and not planning_started:
        return [SupervisorTarget.PLANNING]
    if intent != "review_existing_project" and not _planning_valid(state):
        if int(state.get("planning_attempts", 0)) < int(state.get("max_planning_attempts", 2)):
            return [SupervisorTarget.PLANNING]
        return [SupervisorTarget.FINALIZE]
    if not state.get("workspace_inspected"):
        return [SupervisorTarget.INSPECT_WORKSPACE]

    project_available = _project_available(state)
    if state.get("git_integration_enabled") and project_available and not _git_workspace_prepared(state):
        return [SupervisorTarget.GIT_WORKFLOW]

    if _environment_prepared(state) and not _tests_executed(state):
        return [SupervisorTarget.TESTING_REPAIR]
    if _tests_executed(state) and not _tests_passed(state):
        if (
            not state.get("retry_limit_reached")
            and not state.get("test_infrastructure_failed")
            and int(state.get("repair_attempts", 0)) < 2
        ):
            return [SupervisorTarget.TESTING_REPAIR]
        return [SupervisorTarget.FINALIZE]

    implementation_succeeded = bool(
        _environment_prepared(state)
        and state.get("detected_test_framework")
        and project_available
    )
    if implementation_succeeded:
        return [SupervisorTarget.TESTING_REPAIR]

    if int(state.get("implementation_attempts", 0)) < int(
        state.get("max_implementation_attempts", 2)
    ):
        return [SupervisorTarget.IMPLEMENTATION]
    return [SupervisorTarget.FINALIZE]


def resolve_mandatory_handoff(
    state: SoftwareFactoryState,
    allowed_handoffs: list[SupervisorTarget],
) -> SupervisorTarget | None:
    if approval_blocks_supervisor(state):
        return None
    if state.get("terminal_status") is not None:
        return SupervisorTarget.FINALIZE
    if not allowed_handoffs:
        return None
    non_terminal = [target for target in allowed_handoffs if target != SupervisorTarget.FINALIZE]
    if len(non_terminal) == 1:
        return non_terminal[0]
    if allowed_handoffs == [SupervisorTarget.FINALIZE]:
        return SupervisorTarget.FINALIZE
    return None


def safe_fallback_target(allowed_handoffs: list[SupervisorTarget]) -> SupervisorTarget:
    for target in allowed_handoffs:
        if target != SupervisorTarget.FINALIZE:
            return target
    return SupervisorTarget.FINALIZE


def detect_supervisor_loop(
    handoff_history: list[dict[str, Any]],
    state: SoftwareFactoryState,
) -> bool:
    if not handoff_history:
        return False
    latest_branch = handoff_history[-1].get("branch_id")
    active_history = [
        item
        for item in handoff_history
        if latest_branch is None
        or item.get("branch_id", "original") == latest_branch
    ]
    if len(active_history) < 3:
        return False
    recent = active_history[-3:]
    targets = [item.get("executed_to") or item.get("to") for item in recent]
    if len(set(targets)) != 1:
        return False
    fingerprints = [item.get("progress_fingerprint") for item in recent]
    if all(fingerprint is not None for fingerprint in fingerprints):
        return fingerprints[0] == fingerprints[1] == fingerprints[2]

    # Compatibility with checkpoints written before progress fingerprints.
    target = targets[0]
    if target == SupervisorTarget.PLANNING.value:
        return len({item.get("planning_attempts") for item in recent}) == 1
    if target == SupervisorTarget.IMPLEMENTATION.value:
        return len({item.get("implementation_attempts") for item in recent}) == 1
    if target == SupervisorTarget.TESTING_REPAIR.value:
        return len(
            {
                (
                    item.get("tests_executed"),
                    item.get("tests_passed"),
                    item.get("repair_phase"),
                    item.get("repair_attempts"),
                )
                for item in recent
            }
        ) == 1
    return True
