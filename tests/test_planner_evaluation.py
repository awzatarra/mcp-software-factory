from __future__ import annotations

from copy import deepcopy

from graph.planner_evaluation import (
    PLANNING_EVALUATION_VERSION,
    aggregate_planner_evaluations,
    evaluate_planner_outcome,
    planner_evaluation_updates,
)
from graph.state import create_initial_state


def evaluation_state(**updates):
    state = create_initial_state("Crea un proyecto FastAPI phase-7-planner-evaluation con GET /health y tests.")
    state.update(
        planning_valid=True,
        planning_attempts=0,
        planning_risk_score=12,
        planning_risk_level="low",
        planning_sensitive_tasks=[],
        planning_impact_areas=["application", "tests"],
        planning_quality_score=95,
        planning_quality_level="excellent",
        planning_quality_dimensions={"requirement_coverage": 100},
        planning_quality_issues=[],
        planning_decision_confidence=0.95,
        planning_quality_refinement_attempts=0,
        planning_quality_score_delta=None,
        planning_quality_gate_decision="continue",
        planning_policy_decision="allow",
        planning_approval_required=False,
        planning_approval_status="not_required",
        implementation_valid=True,
        implementation_attempts=0,
        implementation_errors=[],
        tests_executed=True,
        tests_passed=True,
        repair_phase="not_started",
        repair_attempts=0,
        terminal_status="completed",
        failure_type=None,
        failure_stage=None,
        git_commit_status="committed",
        planning_result={"valid": True, "quality": {"score": 95}},
    )
    state.update(updates)
    return state


def test_planner_evaluation_perfect_prediction_is_well_calibrated() -> None:
    evaluation = evaluate_planner_outcome(evaluation_state())

    assert evaluation["version"] == PLANNING_EVALUATION_VERSION
    assert evaluation["outcome"] == "successful"
    assert evaluation["outcome_score"] == 100
    assert evaluation["quality_prediction"]["absolute_error"] == 5
    assert evaluation["confidence_calibration"]["label"] == "well_calibrated"
    assert evaluation["risk_calibration"]["label"] == "aligned"


def test_planner_evaluation_overconfident_when_repair_was_needed() -> None:
    evaluation = evaluate_planner_outcome(
        evaluation_state(
            repair_attempts=2,
            repair_phase="completed",
            planning_quality_score=95,
            planning_decision_confidence=0.95,
        )
    )

    assert evaluation["outcome"] == "successful_with_repair"
    assert evaluation["outcome_score"] < 80
    assert evaluation["confidence_calibration"]["label"] == "overconfident"


def test_planner_evaluation_underconfident_when_low_confidence_succeeds_cleanly() -> None:
    evaluation = evaluate_planner_outcome(
        evaluation_state(planning_quality_score=65, planning_decision_confidence=0.60)
    )

    assert evaluation["outcome"] == "successful"
    assert evaluation["confidence_calibration"]["label"] == "underconfident"
    assert evaluation["quality_prediction"]["signed_error"] < 0


def test_planner_evaluation_risk_underestimated_for_plan_related_failure() -> None:
    evaluation = evaluate_planner_outcome(
        evaluation_state(
            tests_passed=False,
            repair_attempts=2,
            terminal_status="repair_limit_reached",
            failure_type="missing_planned_artifact",
            failure_stage="implementation",
            planning_risk_level="low",
        )
    )

    assert evaluation["outcome"] == "testing_failed"
    assert evaluation["risk_calibration"]["observed"] == "critical"
    assert evaluation["risk_calibration"]["label"] == "underestimated"
    assert evaluation["plan_related_failure"] is True


def test_planner_evaluation_infra_failure_is_not_planner_false_negative() -> None:
    evaluation = evaluate_planner_outcome(
        evaluation_state(
            tests_executed=False,
            tests_passed=False,
            terminal_status="infrastructure_failed",
            failure_type="mcp_timeout",
            failure_stage="prepare_environment",
            test_infrastructure_failed=True,
            planning_risk_level="low",
        )
    )

    assert evaluation["outcome"] == "infrastructure_failed"
    assert evaluation["risk_calibration"]["observed"] == "none"
    assert evaluation["quality_gate"]["false_negative"] is False
    assert evaluation["plan_related_failure"] is False


def test_planner_evaluation_refinement_effective_and_ineffective() -> None:
    effective = evaluate_planner_outcome(
        evaluation_state(planning_quality_refinement_attempts=1, planning_quality_score_delta=20)
    )
    ineffective = evaluate_planner_outcome(
        evaluation_state(
            planning_quality_refinement_attempts=2,
            planning_quality_score_delta=0,
            tests_passed=False,
            terminal_status="implementation_failed",
            failure_type="implementation_schema_invalid",
            failure_stage="implementation",
        )
    )

    assert effective["refinement"]["effective"] is True
    assert ineffective["refinement"]["effective"] is False


def test_planner_evaluation_quality_gate_false_positive_and_false_negative() -> None:
    false_positive = evaluate_planner_outcome(
        evaluation_state(
            planning_quality_gate_decision="refine",
            planning_quality_refinement_attempts=1,
            planning_quality_score_delta=0,
        )
    )
    false_negative = evaluate_planner_outcome(
        evaluation_state(
            planning_quality_gate_decision="continue",
            tests_passed=False,
            terminal_status="implementation_failed",
            failure_type="implementation_schema_invalid",
            failure_stage="implementation",
        )
    )

    assert false_positive["quality_gate"]["false_positive"] is True
    assert false_negative["quality_gate"]["false_negative"] is True


def test_planner_evaluation_updates_are_idempotent_and_grouped() -> None:
    state = evaluation_state()
    first = planner_evaluation_updates(state)
    second_state = deepcopy(state)
    second_state.update(first)
    second = planner_evaluation_updates(second_state)

    assert first["planning_evaluation"] == second["planning_evaluation"]
    assert first["planning_evaluation_outcome"] == "successful"
    assert first["planning_result"]["evaluation"] == first["planning_evaluation"]


def test_aggregate_planner_evaluation_metrics() -> None:
    successful = planner_evaluation_updates(evaluation_state())
    repaired = planner_evaluation_updates(evaluation_state(repair_attempts=2, repair_phase="completed"))
    failed = planner_evaluation_updates(
        evaluation_state(
            tests_passed=False,
            terminal_status="implementation_failed",
            failure_type="implementation_schema_invalid",
            failure_stage="implementation",
        )
    )

    metrics = aggregate_planner_evaluations([successful, repaired, failed])

    assert metrics["total_evaluated"] == 3
    assert metrics["successful"] == 1
    assert metrics["successful_with_repair"] == 1
    assert metrics["failed"] == 1
    assert metrics["average_quality_score"] == 95
    assert metrics["overconfident_count"] >= 1
    assert metrics["risk_underestimated_count"] >= 1
