from __future__ import annotations

import pytest

from graph.planner_hybrid_evaluation import (
    PLANNING_HYBRID_EVALUATION_VERSION,
    aggregate_hybrid_judge_metrics,
    build_planning_hybrid_evaluation,
    build_planning_judge_calibration,
    planner_hybrid_evaluation_node,
)
from graph.state import create_initial_state


def hybrid_state(**updates):
    state = create_initial_state("Crea un proyecto FastAPI phase-8-2-hybrid-evaluation con GET /health y tests.")
    state.update(
        planning_valid=True,
        planning_quality_score=90,
        planning_decision_confidence=0.9,
        planning_quality_dimensions={
            "requirement_coverage": 90,
            "plan_completeness": 90,
            "task_clarity": 90,
            "dependency_coherence": 90,
        },
        planning_risk_level="low",
        planning_judge_status="completed",
        planning_judge_result={
            "overall_score": 86,
            "confidence": 0.86,
            "dimensions": {
                "requirement_alignment": 86,
                "completeness": 86,
                "technical_coherence": 86,
                "task_clarity": 86,
                "complexity_control": 86,
            },
            "recommendation": "accept",
            "issues": [],
            "strengths": [],
            "reason_codes": [],
        },
        planning_judge_disagreement={
            "detected": False,
            "type": "aligned",
            "quality_score": 90,
            "judge_score": 86,
            "delta": -4,
            "threshold": 25,
        },
        planning_result={"valid": True},
        terminal_status="completed",
        tests_passed=True,
        tests_executed=True,
    )
    state.update(updates)
    return state


def test_hybrid_aligned_combines_deterministic_and_judge() -> None:
    result = build_planning_hybrid_evaluation(hybrid_state())

    assert result["version"] == PLANNING_HYBRID_EVALUATION_VERSION
    assert result["source"] == "deterministic_and_judge"
    assert result["agreement"] == "aligned"
    assert result["score"] == pytest.approx(88.8)
    assert result["recommendation"] == "continue"


def test_hybrid_major_disagreement_is_advisory_review_only() -> None:
    result = build_planning_hybrid_evaluation(
        hybrid_state(
            planning_quality_score=92,
            planning_judge_result={
                "overall_score": 45,
                "confidence": 0.9,
                "dimensions": {
                    "requirement_alignment": 70,
                    "completeness": 70,
                    "technical_coherence": 70,
                    "task_clarity": 70,
                    "complexity_control": 70,
                },
            },
            planning_judge_disagreement={"delta": -47, "threshold": 25},
        )
    )

    assert result["agreement"] == "major_disagreement"
    assert "hybrid_major_disagreement" in result["flags"]
    assert result["recommendation"] == "review_recommended"


def test_hybrid_judge_unavailable_uses_deterministic_only() -> None:
    result = build_planning_hybrid_evaluation(
        hybrid_state(planning_judge_status="unavailable", planning_judge_result=None)
    )

    assert result["source"] == "deterministic_only"
    assert result["score"] == 90
    assert result["agreement"] == "judge_unavailable"
    assert "judge_unavailable" in result["flags"]


def test_hybrid_overengineering_flag_does_not_fail_planning() -> None:
    state = hybrid_state()
    state["planning_judge_result"]["dimensions"]["complexity_control"] = 35

    result = build_planning_hybrid_evaluation(state)

    assert "semantic_overengineering_detected" in result["flags"]
    assert state["planning_valid"] is True


def test_hybrid_requirement_alignment_concern_recommends_review() -> None:
    state = hybrid_state()
    state["planning_judge_result"]["dimensions"]["requirement_alignment"] = 50

    result = build_planning_hybrid_evaluation(state)

    assert "semantic_requirement_alignment_concern" in result["flags"]
    assert result["recommendation"] == "review_recommended"


def test_judge_calibration_well_calibrated() -> None:
    status, calibration = build_planning_judge_calibration(
        hybrid_state(
            planning_judge_result={
                "overall_score": 90,
                "confidence": 0.9,
                "dimensions": {},
            }
        ),
        {"outcome": "successful", "outcome_score": 92},
    )

    assert status == "completed"
    assert calibration is not None
    assert calibration["score_error"] == -2
    assert calibration["label"] == "well_calibrated"


def test_judge_calibration_overconfident() -> None:
    status, calibration = build_planning_judge_calibration(
        hybrid_state(planning_judge_result={"overall_score": 90, "confidence": 0.95, "dimensions": {}}),
        {"outcome": "successful_with_repair", "outcome_score": 55},
    )

    assert status == "completed"
    assert calibration is not None
    assert calibration["label"] == "overconfident"


def test_judge_calibration_excludes_infrastructure_failure() -> None:
    status, calibration = build_planning_judge_calibration(
        hybrid_state(terminal_status="infrastructure_failed", failure_type="mcp_timeout"),
        {"outcome": "infrastructure_failed", "outcome_score": 65},
    )

    assert status == "excluded_infrastructure_failure"
    assert calibration is None


def test_hybrid_absolute_error_can_be_better_than_deterministic() -> None:
    state = hybrid_state(planning_quality_score=70)
    state["planning_judge_result"]["overall_score"] = 90

    hybrid = build_planning_hybrid_evaluation(state)

    assert abs(hybrid["score"] - 82) < abs(70 - 82)


@pytest.mark.asyncio
async def test_hybrid_node_is_idempotent_and_has_no_provider_dependency() -> None:
    state = hybrid_state()

    first = await planner_hybrid_evaluation_node(state, object())
    second_state = {**state, **first}
    second = await planner_hybrid_evaluation_node(second_state, object())

    assert first["planning_hybrid_evaluation"] == second["planning_hybrid_evaluation"]
    assert first["planning_result"]["hybrid_evaluation"] == first["planning_hybrid_evaluation"]


def test_aggregate_hybrid_judge_metrics_has_correct_denominators() -> None:
    state = hybrid_state()
    hybrid = build_planning_hybrid_evaluation(state)
    state.update(
        planning_hybrid_evaluation=hybrid,
        planning_judge_score=90,
        planning_judge_score_absolute_error=2,
        planning_judge_confidence=0.9,
        planning_judge_confidence_absolute_error=0.02,
        planning_judge_calibration={"label": "well_calibrated"},
        planning_deterministic_absolute_error=10,
        planning_hybrid_absolute_error=4,
    )
    unavailable = hybrid_state(planning_judge_status="unavailable", planning_judge_result=None)
    unavailable.update(planning_deterministic_absolute_error=5)

    metrics = aggregate_hybrid_judge_metrics([state, unavailable])

    assert metrics["judge_evaluated_count"] == 1
    assert metrics["average_judge_score_absolute_error"] == 2
    assert metrics["average_deterministic_absolute_error"] == 7.5
    assert metrics["average_hybrid_absolute_error"] == 4
    assert metrics["judge_well_calibrated_count"] == 1
