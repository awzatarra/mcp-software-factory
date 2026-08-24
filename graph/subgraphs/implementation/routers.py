from __future__ import annotations

from typing import Literal

from graph.subgraphs.implementation.state import ImplementationState


CORRECTABLE_DEPENDENCY_ERRORS = (
    "incompatible_dependency_version",
    "missing_required_dependency",
    "duplicate_dependency",
)
MAX_DEPENDENCY_NORMALIZATION_ATTEMPTS = 2


def has_only_correctable_dependency_errors(state: ImplementationState) -> bool:
    errors = state.get("implementation_errors", [])
    return bool(errors) and all(error.startswith(CORRECTABLE_DEPENDENCY_ERRORS) for error in errors)


def route_implementation_entry(state: ImplementationState) -> Literal["create", "existing", "failed"]:
    intent = state.get("workflow_intent")
    exists = bool(state.get("project_exists") or state.get("project_created"))
    if intent == "review_existing_project":
        return "existing" if exists else "failed"
    return "existing" if exists else "create"


def route_after_implementation_validation(
    state: ImplementationState,
) -> Literal["valid", "normalize", "refine", "failed"]:
    if state.get("terminal_status"):
        return "failed"
    if state.get("implementation_valid"):
        return "valid"
    if has_only_correctable_dependency_errors(state):
        if state.get("dependency_normalization_attempts", 0) < MAX_DEPENDENCY_NORMALIZATION_ATTEMPTS:
            return "normalize"
        return "failed"
    if state.get("implementation_attempts", 0) < state.get("max_implementation_attempts", 2):
        return "refine"
    return "failed"


def route_implementation_approval(
    state: ImplementationState,
) -> Literal["execute_create_project", "execute_prepare_environment", "rejected"]:
    if state.get("pending_approval_status") == "rejected" or state.get("user_cancelled"):
        return "rejected"
    if state.get("pending_approval_status") != "approved":
        raise ValueError("La aprobación de implementación debe estar resuelta antes de enrutar.")
    operation = state.get("pending_operation")
    if operation == "create_project":
        return "execute_create_project"
    if operation == "prepare_environment":
        return "execute_prepare_environment"
    raise ValueError(f"Operación no permitida dentro de ImplementationSubgraph: {operation}")


def route_after_create_execution(state: ImplementationState) -> Literal["continue", "failed"]:
    return "continue" if state.get("project_created") or state.get("project_exists") else "failed"
