from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from api.services.planner_calibration_service import PlannerCalibrationService
from api.services.planner_recommendation_review_store import PlannerRecommendationReviewStore
from graph.agent_recommendations import (
    AGENT_RECOMMENDATION_VERSION,
    analyze_agent_recommendations,
)


def perf(
    *,
    developer_score: float = 90,
    first_pass: bool = True,
    validation_errors: int = 0,
    repair_required: bool = False,
    repair_success: bool | None = None,
    qa_level: str = "excellent",
) -> dict:
    repair_status = "not_applicable" if repair_success is None else "completed"
    return {
        "planner": {"score": 90, "level": "excellent", "metrics": {}, "reason_codes": ["planner_evaluated"]},
        "developer": {
            "score": developer_score,
            "level": "excellent" if developer_score >= 90 else "weak",
            "metrics": {
                "first_pass_success": first_pass,
                "required_repair": repair_required,
                "validation_error_count": validation_errors,
            },
            "reason_codes": ["implementation_validation_failed"] if validation_errors else ["developer_evaluated"],
        },
        "repair": {
            "score": 90 if repair_success else 35 if repair_success is False else None,
            "level": "excellent" if repair_success else "weak" if repair_success is False else None,
            "status": repair_status,
            "metrics": {"repair_success": repair_success, "repair_attempts": 1 if repair_success is not None else 0},
            "reason_codes": ["repair_success"] if repair_success else ["repair_failed"] if repair_success is False else ["repair_not_required"],
        },
        "qa": {"score": 90, "level": qa_level, "metrics": {}, "reason_codes": ["qa_evaluated"]},
    }


def rca(
    root_cause: str | None = None,
    *,
    reason_codes: list[str] | None = None,
    contributors: list[dict] | None = None,
    status: str = "completed",
) -> dict:
    if root_cause is None:
        return {
            "status": "not_applicable",
            "root_cause": None,
            "reason_codes": ["no_failure_detected"],
            "contributors": [],
            "excluded_attributions": [],
            "version": "8.4-v1",
        }
    return {
        "status": status,
        "root_cause": root_cause,
        "failure_class": f"{root_cause}_failure",
        "reason_codes": reason_codes or [f"root_cause_{root_cause}"],
        "contributors": contributors or [],
        "excluded_attributions": [],
        "version": "8.4-v1",
    }


def record(
    index: int,
    *,
    framework: str = "fastapi",
    root_cause: str | None = None,
    reason_codes: list[str] | None = None,
    developer_score: float = 90,
    first_pass: bool = True,
    validation_errors: int = 0,
    repair_required: bool = False,
    repair_success: bool | None = None,
    contributors: list[dict] | None = None,
) -> dict:
    return {
        "workflow_id": f"workflow-{index}",
        "framework": framework,
        "terminal_status": "completed" if root_cause is None else "tests_failed",
        "created_at": (datetime(2026, 8, 1, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
        "agent_performance_evaluations": perf(
            developer_score=developer_score,
            first_pass=first_pass,
            validation_errors=validation_errors,
            repair_required=repair_required,
            repair_success=repair_success,
        ),
        "failure_attribution": rca(root_cause, reason_codes=reason_codes, contributors=contributors),
        "planning_evaluation": {"version": "7.7-v1", "refinement": {"attempts": 0}, "quality_gate": {}},
    }


def test_insufficient_data_returns_no_recommendations() -> None:
    analysis = analyze_agent_recommendations([record(index) for index in range(5)], minimum_sample_size=20)

    assert analysis["status"] == "insufficient_data"
    assert analysis["agents"]["developer"]["status"] == "insufficient_data"
    assert analysis["recommendations"] == []


def test_healthy_developer_has_no_recommendation() -> None:
    analysis = analyze_agent_recommendations([record(index) for index in range(25)], minimum_sample_size=20)

    assert analysis["agents"]["developer"]["metrics"]["root_cause_rate"] == 0
    assert [item for item in analysis["recommendations"] if item["agent"] == "developer"] == []


def test_developer_validation_pattern_recommends_improve_validation() -> None:
    records = [
        record(index, root_cause="developer", developer_score=55, first_pass=False, validation_errors=2, repair_required=True, repair_success=True, contributors=[{"source": "repair", "contribution": "resolved"}])
        if index % 2 == 0
        else record(index, first_pass=True)
        for index in range(24)
    ]

    analysis = analyze_agent_recommendations(records, minimum_sample_size=20)
    recommendation = next(item for item in analysis["recommendations"] if item["agent"] == "developer")

    assert recommendation["type"] == "improve_validation"
    assert recommendation["status"] == "recommendation_only"
    assert recommendation["application_status"] == "not_applied"
    assert recommendation["target"] == {"type": "agent", "agent": "developer", "segment": None}
    assert recommendation["confidence"] >= 0.5
    assert "agent_validation_failures" in recommendation["reason_codes"]


def test_infrastructure_rca_prevents_false_developer_recommendation() -> None:
    records = [
        record(index, root_cause="infrastructure", developer_score=45, first_pass=False, validation_errors=2)
        for index in range(25)
    ]

    analysis = analyze_agent_recommendations(records, minimum_sample_size=20)

    assert analysis["agents"]["developer"]["metrics"]["excluded_infrastructure_count"] == 25
    assert [item for item in analysis["recommendations"] if item["agent"] == "developer"] == []


def test_policy_and_user_failures_are_excluded_from_agent_denominators() -> None:
    records = [record(index, root_cause="policy") for index in range(10)]
    records.extend(record(index + 10, root_cause="user") for index in range(10))

    analysis = analyze_agent_recommendations(records, minimum_sample_size=20)

    assert analysis["agents"]["developer"]["metrics"]["attributable_workflows"] == 0
    assert analysis["recommendations"] == []


def test_planner_grounding_recommendation_with_judge_corroboration_has_higher_confidence() -> None:
    plain = [
        record(index, root_cause="planner", reason_codes=["target_mismatch"])
        if index < 8 else record(index)
        for index in range(24)
    ]
    corroborated = [
        {**item, "planning_judge_result": {"dimension_scores": {"requirement_alignment": 0.4}}}
        for item in plain
    ]

    first = analyze_agent_recommendations(plain, minimum_sample_size=20)
    second = analyze_agent_recommendations(corroborated, minimum_sample_size=20)
    first_rec = next(item for item in first["recommendations"] if item["agent"] == "planner")
    second_rec = next(item for item in second["recommendations"] if item["agent"] == "planner")

    assert first_rec["type"] == "improve_context_grounding"
    assert second_rec["confidence"] > first_rec["confidence"]
    assert "judge_corroborated_alignment_concern" in second_rec["reason_codes"]


def test_judge_only_without_downstream_rca_does_not_create_strong_recommendation() -> None:
    records = [
        {**record(index), "planning_judge_result": {"dimension_scores": {"requirement_alignment": 0.2}}}
        for index in range(25)
    ]

    analysis = analyze_agent_recommendations(records, minimum_sample_size=20)

    assert [item for item in analysis["recommendations"] if item["agent"] == "planner"] == []


def test_repair_strategy_recommendation() -> None:
    records = [
        record(index, root_cause="developer", repair_required=True, repair_success=False, contributors=[{"source": "repair", "contribution": "failed_to_recover"}])
        if index < 10 else record(index)
        for index in range(25)
    ]

    analysis = analyze_agent_recommendations(records, minimum_sample_size=20)
    recommendation = next(item for item in analysis["recommendations"] if item["agent"] == "repair")

    assert recommendation["type"] == "improve_repair_strategy"
    assert "agent_low_repair_success" in recommendation["reason_codes"]


def test_qa_failure_classification_recommendation() -> None:
    records = [
        record(index, root_cause="qa", reason_codes=["unknown_failure_classification_missing"])
        if index < 5 else record(index)
        for index in range(25)
    ]

    analysis = analyze_agent_recommendations(records, minimum_sample_size=20)
    recommendation = next(item for item in analysis["recommendations"] if item["agent"] == "qa")

    assert recommendation["type"] == "improve_failure_classification"


def test_segmented_recommendation_without_global_recommendation() -> None:
    records = [record(index, framework="fastapi") for index in range(10)]
    records.extend(
        record(index + 10, framework="dotnet", root_cause="developer", developer_score=45, first_pass=False, validation_errors=2, repair_required=True)
        for index in range(10)
    )

    analysis = analyze_agent_recommendations(records, minimum_sample_size=20, segment_min_sample_size=10)
    developer = [item for item in analysis["recommendations"] if item["agent"] == "developer"]

    assert [item["segment"] for item in developer] == ["framework:dotnet"]


def test_improving_trend_suppresses_current_recommendation() -> None:
    records = [
        record(index, root_cause="developer", developer_score=45, first_pass=False, validation_errors=2)
        if index < 10 else record(index)
        for index in range(20)
    ]

    analysis = analyze_agent_recommendations(records, minimum_sample_size=20)

    assert analysis["agents"]["developer"]["trend"]["status"] == "improving"
    recommendations = [item for item in analysis["recommendations"] if item["agent"] == "developer"]
    assert all(item["severity"] in {"low", "medium"} for item in recommendations)


def test_deterministic_ids_and_fingerprints() -> None:
    records = [
        record(index, root_cause="developer", developer_score=55, first_pass=False, validation_errors=2, repair_required=True)
        if index < 8 else record(index)
        for index in range(24)
    ]

    first = analyze_agent_recommendations(records, minimum_sample_size=20)
    second = analyze_agent_recommendations(records, minimum_sample_size=20)

    assert first["agents"]["developer"]["metrics"] == second["agents"]["developer"]["metrics"]
    assert [(item["recommendation_id"], item["fingerprint"]) for item in first["recommendations"]] == [
        (item["recommendation_id"], item["fingerprint"]) for item in second["recommendations"]
    ]
    assert first["version"] == AGENT_RECOMMENDATION_VERSION


class DirectAgentRecommendationService(PlannerCalibrationService):
    def __init__(self, review_store: PlannerRecommendationReviewStore, initial_records: list[dict]) -> None:
        super().__init__(query=SimpleNamespace(), metadata_store=None, review_store=review_store)
        self._agent_records = initial_records

    async def agent_records(self, **kwargs):
        return list(self._agent_records)


@pytest.mark.asyncio
async def test_agent_recommendation_governance_accepts_without_applying(tmp_path) -> None:
    store = PlannerRecommendationReviewStore(tmp_path / "agent-reviews.sqlite")
    await store.initialize()
    records = [
        record(index, root_cause="developer", developer_score=55, first_pass=False, validation_errors=2, repair_required=True)
        if index < 8 else record(index)
        for index in range(24)
    ]
    service = DirectAgentRecommendationService(store, records)
    listed = await service.agent_recommendations(minimum_sample_size=20)
    recommendation = next(item for item in listed["items"] if item["agent"] == "developer")

    accepted = await service.review_agent_recommendation(
        recommendation["recommendation_id"],
        action="accept",
        reviewer="architect",
        notes="Accepted for future advisory review.",
        reason="Pattern is durable.",
        fingerprint=recommendation["recommendation_fingerprint"],
    )

    assert accepted["review"]["status"] == "accepted"
    assert accepted["application_status"] == "not_applied"
    assert accepted["target"]["type"] == "agent"
    row = await store.get(recommendation["recommendation_id"], recommendation["recommendation_fingerprint"])
    assert row is not None
    assert row["application_status"] == "not_applied"
