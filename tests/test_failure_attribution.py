from __future__ import annotations

import pytest

from graph.failure_attribution import (
    FAILURE_ATTRIBUTION_VERSION,
    aggregate_failure_attribution,
    build_failure_attribution,
    failure_attribution_node,
)
from graph.state import create_initial_state


def rca_state(**updates):
    state = create_initial_state("Crea un proyecto FastAPI phase-8-4-rca-success con GET /health y tests.")
    state.update(
        planning_valid=True,
        planning_attempts=1,
        planning_quality_score=92,
        implementation_valid=True,
        implementation_attempts=1,
        project_created=True,
        tests_executed=True,
        tests_passed=True,
        repair_phase="not_started",
        repair_attempts=0,
        terminal_status="completed",
        failure_type=None,
        failure_stage=None,
        test_infrastructure_failed=False,
        agent_performance_evaluations={},
    )
    state.update(updates)
    return state


def test_success_is_not_applicable() -> None:
    result = build_failure_attribution(rca_state())

    assert result["status"] == "not_applicable"
    assert result["root_cause"] is None
    assert result["failure_class"] is None


def test_mcp_timeout_is_infrastructure_and_excludes_agents() -> None:
    result = build_failure_attribution(
        rca_state(
            terminal_status="infrastructure_failed",
            failure_type="mcp_timeout",
            failure_stage="testing",
            tests_executed=False,
            tests_passed=False,
            test_infrastructure_failed=True,
        )
    )

    assert result["root_cause"] == "infrastructure"
    assert result["failure_class"] == "infrastructure_failure"
    assert result["confidence"] >= 0.9
    assert {item["source"] for item in result["excluded_attributions"]} >= {"developer", "qa"}


def test_user_rejection_is_user_root_cause() -> None:
    result = build_failure_attribution(
        rca_state(
            terminal_status="user_cancelled",
            failure_type="user_rejected",
            failure_stage="planning_risk_approval",
            user_cancelled=True,
        )
    )

    assert result["root_cause"] == "user"
    assert result["failure_class"] == "user_rejection"
    assert result["reason_codes"] == ["root_cause_user_rejected"]


def test_planner_defect_uses_plan_related_classifier() -> None:
    result = build_failure_attribution(
        rca_state(
            terminal_status="tests_failed",
            tests_passed=False,
            failure_type="target_mismatch",
            failure_stage="implementation",
        )
    )

    assert result["root_cause"] == "planner"
    assert result["failure_class"] == "planning_defect"
    assert result["excluded_attributions"][0]["source"] == "developer"


def test_developer_defect_recovered_by_repair() -> None:
    result = build_failure_attribution(
        rca_state(
            repair_attempts=1,
            repair_phase="completed",
            first_test_result_summary="1 failed",
            test_failure_summary="wrong health response",
            files_updated_during_repair=["app/main.py"],
            tests_passed=True,
        )
    )

    assert result["status"] == "completed"
    assert result["recovered"] is True
    assert result["root_cause"] == "developer"
    assert result["failure_class"] == "implementation_defect"
    assert any(item["source"] == "qa" and item["contribution"] == "detected" for item in result["contributors"])
    assert any(item["source"] == "repair" and item["contribution"] == "resolved" for item in result["contributors"])


def test_repair_failure_keeps_original_developer_root_cause() -> None:
    result = build_failure_attribution(
        rca_state(
            terminal_status="repair_limit_reached",
            tests_passed=False,
            repair_attempts=2,
            repair_phase="rerun_tests",
            failure_type="test_failure",
            failure_stage="run_tests",
        )
    )

    assert result["root_cause"] == "developer"
    assert any(item["source"] == "repair" and item["contribution"] == "failed_to_recover" for item in result["contributors"])


def test_qa_detected_bug_is_not_qa_root_cause() -> None:
    result = build_failure_attribution(
        rca_state(tests_passed=False, terminal_status="tests_failed", failure_type="test_failure", failure_stage="run_tests")
    )

    assert result["root_cause"] == "developer"
    assert any(item["source"] == "qa" and item["contribution"] == "detected" for item in result["contributors"])


def test_qa_structured_defect_can_be_root_cause() -> None:
    result = build_failure_attribution(
        rca_state(terminal_status="tests_failed", tests_passed=False, failure_type="test_harness_defect", failure_stage="test_harness")
    )

    assert result["root_cause"] == "qa"
    assert result["failure_class"] == "testing_defect"


def test_policy_and_external_classification() -> None:
    policy = build_failure_attribution(rca_state(terminal_status="implementation_failed", failure_type="policy_conflict", failure_stage="policy_governance"))
    external = build_failure_attribution(rca_state(terminal_status="infrastructure_failed", failure_type="provider_timeout", failure_stage="llm"))

    assert policy["root_cause"] == "policy"
    assert external["root_cause"] == "external"


def test_low_developer_score_does_not_override_mcp_timeout() -> None:
    result = build_failure_attribution(
        rca_state(
            terminal_status="infrastructure_failed",
            failure_type="mcp_timeout",
            failure_stage="testing",
            tests_passed=False,
            agent_performance_evaluations={"developer": {"score": 40}},
        )
    )

    assert result["root_cause"] == "infrastructure"


def test_ambiguous_failure_is_insufficient_evidence() -> None:
    result = build_failure_attribution(
        rca_state(terminal_status="failed", tests_executed=False, tests_passed=False, failure_type="mystery", failure_stage="unknown")
    )

    assert result["status"] == "insufficient_evidence"
    assert result["root_cause"] == "unknown"


@pytest.mark.asyncio
async def test_failure_attribution_node_is_deterministic() -> None:
    state = rca_state(repair_attempts=1, repair_phase="completed", first_test_result_summary="1 failed")
    first = await failure_attribution_node(state, object())
    second = await failure_attribution_node({**state, **first}, object())

    assert first == second
    assert first["failure_attribution"]["version"] == FAILURE_ATTRIBUTION_VERSION


def test_failure_attribution_aggregation() -> None:
    developer = {"failure_attribution": build_failure_attribution(rca_state(repair_attempts=1, repair_phase="completed", first_test_result_summary="1 failed"))}
    infra = {"failure_attribution": build_failure_attribution(rca_state(terminal_status="infrastructure_failed", failure_type="mcp_timeout", failure_stage="testing", tests_passed=False))}

    aggregate = aggregate_failure_attribution([developer, infra])

    assert aggregate["total_failures_attributed"] == 2
    assert aggregate["developer_root_cause_count"] == 1
    assert aggregate["infrastructure_root_cause_count"] == 1
    assert aggregate["repair_resolved_count"] == 1
    assert aggregate["qa_detected_count"] == 1
