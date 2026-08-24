from __future__ import annotations

from typing import Literal

from graph.subgraphs.testing_repair.state import TestingRepairState


MAX_REPAIR_ATTEMPTS = 2


class UnknownTestingOperationError(ValueError):
    pass


def route_testing_entry(
    state: TestingRepairState,
) -> Literal["tests", "read_failing_test", "read_related_source", "prepare_fix"]:
    if not state.get("fork_origin_checkpoint_id"):
        return "tests"
    phase = state.get("repair_phase")
    if phase == "read_failing_test":
        return "read_failing_test"
    if phase == "read_related_source":
        return "read_related_source"
    if phase == "apply_fix":
        return "prepare_fix"
    return "tests"


def route_testing_approval(
    state: TestingRepairState,
) -> Literal["execute_tests", "execute_fix", "rejected"]:
    if state.get("pending_approval_status") == "rejected" or state.get("user_cancelled"):
        return "rejected"
    if state.get("pending_approval_status") != "approved":
        raise UnknownTestingOperationError("La aprobación interna debe estar resuelta antes de enrutar.")
    operation = state.get("pending_operation")
    if operation == "run_tests":
        return "execute_tests"
    if operation == "apply_fix":
        return "execute_fix"
    raise UnknownTestingOperationError(f"Operación de testing/reparación desconocida: {operation}")


def route_after_tests(
    state: TestingRepairState,
) -> Literal["passed", "repair", "no_tests_collected", "infrastructure_failed", "repair_limit_reached"]:
    if state.get("tests_passed"):
        return "passed"
    if state.get("failure_type") == "no_tests_collected":
        return "no_tests_collected"
    if state.get("test_infrastructure_failed"):
        return "infrastructure_failed"
    if state.get("retry_limit_reached") or state.get("repair_attempts", 0) >= MAX_REPAIR_ATTEMPTS:
        return "repair_limit_reached"
    return "repair"


def route_after_read_failing_test(
    state: TestingRepairState,
) -> Literal["continue", "failed"]:
    if state.get("failing_test_content") and state.get("repair_phase") == "read_related_source":
        return "continue"
    return "failed"


def route_after_fix(state: TestingRepairState) -> Literal["tests", "finished"]:
    if state.get("repair_phase") == "rerun_tests" and not state.get("user_cancelled"):
        return "tests"
    return "finished"


def route_after_related_source(
    state: TestingRepairState,
) -> Literal["prepare_fix", "retry_read", "finalize"]:
    if state.get("repair_phase") == "apply_fix" and state.get("related_source_content"):
        return "prepare_fix"
    attempts = state.get("related_source_read_attempts", 0)
    candidates = state.get("related_source_candidates", [])
    if attempts < min(len(candidates), MAX_REPAIR_ATTEMPTS):
        return "retry_read"
    return "finalize"


def route_after_prepare_fix(state: TestingRepairState) -> Literal["approval", "failed"]:
    if state.get("pending_operation") == "apply_fix" and state.get("pending_approval_status") == "waiting":
        return "approval"
    return "failed"


def route_after_execute_fix(state: TestingRepairState) -> Literal["rerun_tests", "cancelled", "failed"]:
    if state.get("user_cancelled"):
        return "cancelled"
    if state.get("repair_phase") == "rerun_tests" and not state.get("failure_type"):
        return "rerun_tests"
    return "failed"
