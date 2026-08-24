from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


_PASSED_TESTS = re.compile(r"\b([1-9]\d*)\s+passed\b", re.IGNORECASE)
_FAILED_TESTS = re.compile(
    r"\b(?:[1-9]\d*)\s+(?:failed|error|errors)\b|\bcollection\s+error\b",
    re.IGNORECASE,
)
_ACCEPTED_DECISIONS = frozenset({"accepted", "accepted_with_warnings"})


@dataclass(frozen=True)
class CISuccessReconciliation:
    applicable: bool
    clears_ci_repair_interrupt: bool
    updates: dict[str, Any]
    reason: str


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _durable_local_tests_passed(state: Mapping[str, Any]) -> bool:
    summary = state.get("final_test_result_summary")
    if not isinstance(summary, str):
        return False
    return bool(_PASSED_TESTS.search(summary)) and not _FAILED_TESTS.search(summary)


def _is_stale_ci_repair_interrupt(state: Mapping[str, Any]) -> bool:
    return bool(
        state.get("pending_operation") == "run_tests"
        and state.get("pending_tool_name") == "testing__run_tests"
        and state.get("pending_approval_status") == "waiting"
        and state.get("ci_repair_state") in {"pending", "running"}
        and state.get("ci_repair_source_run_id")
        and (
            state.get("supervisor_decision") == "testing_repair"
            or state.get("failure_stage") == "ci"
        )
    )


def build_ci_success_reconciliation(
    state: Mapping[str, Any],
    run: Mapping[str, Any],
    eligibility: Mapping[str, Any],
    *,
    head_commit: str | None,
) -> CISuccessReconciliation:
    validated_commit = run.get("ci_validated_commit")
    decision = run.get("decision")
    exact_commit = bool(
        head_commit
        and validated_commit == head_commit
        and eligibility.get("source_commit") == head_commit
        and eligibility.get("target_commit") == head_commit
        and eligibility.get("commit_match") is True
    )
    accepted = bool(
        run.get("status") == "passed"
        and decision in _ACCEPTED_DECISIONS
        and eligibility.get("eligible") is True
    )
    if not accepted or not exact_commit:
        return CISuccessReconciliation(
            applicable=False,
            clears_ci_repair_interrupt=False,
            updates={},
            reason="ci_run_not_accepted_for_current_head",
        )

    stale_repair = _is_stale_ci_repair_interrupt(state)
    updates: dict[str, Any] = {
        "ci_state": "passed",
        "ci_run_id": run.get("ci_run_id"),
        "ci_status": run.get("status"),
        "ci_decision": decision,
        "ci_validated_commit": validated_commit,
        "ci_failure_type": None,
        "ci_failure_message": None,
        "ci_gate_summary": dict(_mapping(run.get("gate_summary"))),
        "ci_promotion_eligible": True,
        "ci_promotion_eligibility": dict(eligibility),
    }
    if stale_repair:
        updates.update(
            {
                "ci_repair_state": "not_required",
                "ci_repair_failure_type": None,
                "ci_repair_failure_message": None,
                "ci_repair_source_run_id": None,
                "ci_repair_source_commit": None,
                "ci_repair_target_commit": None,
                "ci_repair_repairability": None,
                "pending_operation": None,
                "pending_tool_name": None,
                "pending_tool_arguments": None,
                "pending_approval_preview": None,
                "pending_approval_status": "none",
                "supervisor_decision": None,
                "supervisor_reason": None,
                "supervisor_confidence": None,
                "supervisor_decision_source": None,
                "allowed_handoffs": [],
                "terminal_status": None,
                "last_completed_stage": "ci_pipeline",
                "last_completed_node": "ci_pipeline",
            }
        )
        if state.get("failure_stage") == "ci":
            updates.update(
                failure_type=None,
                failure_stage=None,
                failure_message=None,
                test_failure_summary=None,
            )
        if _durable_local_tests_passed(state):
            testing_result = dict(_mapping(state.get("testing_result")))
            testing_result.update(
                tests_executed=True,
                tests_passed=True,
                summary=state.get("final_test_result_summary"),
            )
            updates.update(
                tests_executed=True,
                tests_passed=True,
                testing_result=testing_result,
            )

    return CISuccessReconciliation(
        applicable=True,
        clears_ci_repair_interrupt=stale_repair,
        updates=updates,
        reason="accepted_ci_for_current_head",
    )
