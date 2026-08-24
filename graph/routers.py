from __future__ import annotations

from typing import Literal

from graph.state import SoftwareFactoryState
from graph.subgraphs.testing_repair.routers import route_after_tests


def route_after_intent(state: SoftwareFactoryState) -> Literal["create", "review"]:
    return "review" if state.get("workflow_intent") == "review_existing_project" else "create"


def route_after_planning(state: SoftwareFactoryState) -> Literal["continue", "failed"]:
    return "continue" if state.get("planning_valid") else "failed"


def route_after_implementation(state: SoftwareFactoryState) -> Literal["testing", "finalize"]:
    if state.get("terminal_status") is not None:
        return "finalize"
    if state.get("environment_prepared") and state.get("detected_test_framework"):
        return "testing"
    return "finalize"


def route_after_workspace(state: SoftwareFactoryState) -> Literal["create", "existing"]:
    return "existing" if state.get("project_exists") or state.get("project_created") else "create"


def route_after_environment(state: SoftwareFactoryState) -> Literal["tests", "finalize"]:
    if state.get("test_infrastructure_failed") or not state.get("environment_prepared"):
        return "finalize"
    return "tests"


def route_after_approval(state: SoftwareFactoryState) -> str:
    if state.get("pending_approval_status") == "rejected" or state.get("user_cancelled"):
        return "finalize"
    if state.get("pending_approval_status") != "approved":
        raise ValueError("La aprobación debe estar resuelta antes de enrutar.")
    routes = {
        "create_project": "execute_create_project",
        "prepare_environment": "execute_prepare_environment",
        "run_tests": "execute_tests",
        "apply_fix": "execute_fix",
    }
    operation = state.get("pending_operation")
    if operation not in routes:
        raise ValueError(f"Operación pendiente desconocida: {operation}")
    return routes[operation]
