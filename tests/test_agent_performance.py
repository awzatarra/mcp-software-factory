from __future__ import annotations

import pytest

from graph.agent_performance import (
    AGENT_PERFORMANCE_VERSION,
    aggregate_agent_performance,
    agent_performance_evaluation_node,
    evaluate_agent_performance,
    failure_attribution,
)
from graph.state import create_initial_state


def performance_state(**updates):
    state = create_initial_state("Crea un proyecto FastAPI phase-8-3-agent-performance con GET /health y tests.")
    state.update(
        planning_valid=True,
        planning_attempts=1,
        planning_quality_score=94,
        planning_decision_confidence=0.92,
        planning_outcome_score=100,
        planning_evaluation_outcome="successful",
        planning_quality_prediction_absolute_error=6,
        planning_quality_refinement_attempts=0,
        implementation_valid=True,
        implementation_attempts=1,
        implementation_errors=[],
        generated_files=["app/main.py", "tests/test_health.py"],
        files_updated_during_repair=[],
        tests_executed=True,
        tests_passed=True,
        repair_phase="not_started",
        repair_attempts=0,
        terminal_status="completed",
        failure_type=None,
        failure_stage=None,
        test_infrastructure_failed=False,
    )
    state.update(updates)
    return state


def test_perfect_flow_scores_expected_agents() -> None:
    result = evaluate_agent_performance(performance_state())

    assert set(result) == {"planner", "developer", "repair", "qa"}
    assert result["planner"]["level"] in {"good", "excellent"}
    assert result["developer"]["level"] == "excellent"
    assert result["developer"]["metrics"]["first_pass_success"] is True
    assert result["repair"]["status"] == "not_applicable"
    assert result["repair"]["score"] is None
    assert result["qa"]["level"] == "excellent"
    assert result["qa"]["version"] == AGENT_PERFORMANCE_VERSION


def test_developer_retries_reduce_score_without_forcing_failure() -> None:
    result = evaluate_agent_performance(performance_state(implementation_attempts=3))

    developer = result["developer"]
    assert developer["score"] < 100
    assert developer["level"] in {"acceptable", "good"}
    assert "implementation_retries" in developer["reason_codes"]


def test_developer_requires_repair_penalizes_developer_and_rewards_repair() -> None:
    result = evaluate_agent_performance(
        performance_state(
            tests_passed=True,
            repair_attempts=1,
            repair_phase="completed",
            first_test_result_summary="1 failed",
            test_failure_summary="assertion failed",
            files_updated_during_repair=["tests/test_health.py"],
            planning_outcome_score=80,
            planning_evaluation_outcome="successful_with_repair",
        )
    )

    assert result["developer"]["metrics"]["required_repair"] is True
    assert result["developer"]["score"] < 90
    assert result["repair"]["metrics"]["repair_success"] is True
    assert result["repair"]["level"] == "excellent"


def test_repair_failure_scores_low_when_attributable() -> None:
    result = evaluate_agent_performance(
        performance_state(
            tests_passed=False,
            repair_attempts=2,
            repair_phase="rerun_tests",
            terminal_status="repair_limit_reached",
            failure_type="missing_planned_artifact",
            failure_stage="implementation",
        )
    )

    assert result["repair"]["score"] < 60
    assert result["repair"]["level"] in {"poor", "weak"}
    assert result["repair"]["metrics"]["tests_recovered"] is False


def test_qa_success_with_detected_failure_is_positive() -> None:
    result = evaluate_agent_performance(
        performance_state(
            tests_passed=True,
            repair_attempts=1,
            repair_phase="completed",
            test_failure_summary="Actionable failure",
        )
    )

    assert result["qa"]["metrics"]["test_failure_detected"] is True
    assert result["qa"]["metrics"]["failure_actionable"] is True
    assert result["qa"]["score"] >= 90


def test_qa_infrastructure_failure_is_excluded() -> None:
    result = evaluate_agent_performance(
        performance_state(
            tests_executed=False,
            tests_passed=False,
            terminal_status="infrastructure_failed",
            failure_type="mcp_timeout",
            failure_stage="testing",
            test_infrastructure_failed=True,
        )
    )

    assert failure_attribution(performance_state(terminal_status="infrastructure_failed", failure_type="mcp_timeout")) == "infrastructure_related"
    assert result["qa"]["status"] == "excluded_infrastructure_failure"
    assert result["qa"]["score"] is None
    assert result["developer"]["status"] == "excluded_infrastructure_failure"


def test_planner_bad_downstream_plan_related_failure_is_penalized() -> None:
    result = evaluate_agent_performance(
        performance_state(
            tests_executed=False,
            tests_passed=False,
            terminal_status="implementation_failed",
            failure_type="target_mismatch",
            failure_stage="implementation",
            planning_quality_score=95,
            planning_outcome_score=50,
        )
    )

    assert result["planner"]["score"] < 80
    assert "hybrid_review_recommended" not in result["planner"]["reason_codes"]


def test_infra_does_not_create_arbitrary_agent_penalty() -> None:
    result = evaluate_agent_performance(
        performance_state(
            terminal_status="infrastructure_failed",
            failure_type="mcp_timeout",
            failure_stage="prepare_environment",
            test_infrastructure_failed=True,
        )
    )

    assert result["developer"]["status"] == "excluded_infrastructure_failure"
    assert result["qa"]["status"] == "excluded_infrastructure_failure"
    assert result["planner"]["score"] >= 80


@pytest.mark.asyncio
async def test_agent_performance_node_is_idempotent() -> None:
    state = performance_state()
    first = await agent_performance_evaluation_node(state, object())
    second = await agent_performance_evaluation_node({**state, **first}, object())

    assert first == second


def test_agent_performance_aggregation_uses_correct_denominators() -> None:
    first = {"agent_performance_evaluations": evaluate_agent_performance(performance_state())}
    second = {
        "agent_performance_evaluations": evaluate_agent_performance(
            performance_state(
                tests_executed=False,
                tests_passed=False,
                terminal_status="infrastructure_failed",
                failure_type="mcp_timeout",
                failure_stage="testing",
                test_infrastructure_failed=True,
            )
        )
    }

    aggregate = aggregate_agent_performance([first, second])

    assert aggregate["average_developer_score"] == first["agent_performance_evaluations"]["developer"]["score"]
    assert aggregate["repair_not_applicable_count"] == 2
    assert aggregate["qa_infrastructure_exclusion_rate"] == 0.5
    assert aggregate["developer_good_or_better_rate"] == 1
