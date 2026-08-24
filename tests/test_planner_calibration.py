from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api.app import create_app
from graph.planner_calibration import analyze_planner_calibration


def record(
    index: int,
    *,
    outcome: str = "successful",
    outcome_score: int = 100,
    confidence_label: str = "well_calibrated",
    confidence_error: float = 0.04,
    risk_label: str = "aligned",
    false_positive: bool = False,
    false_negative: bool = False,
    refinement_effective=None,
    framework: str = "fastapi",
    risk_level: str = "low",
    quality_level: str = "excellent",
    approval_required: bool = False,
    version: str = "7.7-v1",
):
    confidence = 0.95
    observed_confidence = outcome_score / 100
    if confidence_label in {"overconfident", "slightly_overconfident"}:
        confidence = min(1.0, observed_confidence + abs(confidence_error))
    elif confidence_label in {"underconfident", "slightly_underconfident"}:
        confidence = max(0.0, observed_confidence - abs(confidence_error))
    return {
        "workflow_id": f"workflow-{index:03d}",
        "framework": framework,
        "risk_level": risk_level,
        "quality_level": quality_level,
        "approval_required": approval_required,
        "created_at": (datetime(2026, 8, 1, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
        "planning_evaluation": {
            "version": version,
            "outcome": outcome,
            "outcome_score": outcome_score,
            "quality_prediction": {
                "predicted": 95,
                "observed": outcome_score,
                "signed_error": 95 - outcome_score,
                "absolute_error": abs(95 - outcome_score),
            },
            "confidence_calibration": {
                "predicted": confidence,
                "observed": observed_confidence,
                "signed_error": confidence - observed_confidence,
                "absolute_error": abs(confidence - observed_confidence),
                "label": confidence_label,
            },
            "risk_calibration": {
                "predicted": risk_level,
                "observed": "high" if risk_label == "underestimated" else "low",
                "label": risk_label,
            },
            "refinement": {
                "attempts": 1 if refinement_effective is not None else 0,
                "effective": refinement_effective,
            },
            "quality_gate": {
                "false_positive": false_positive,
                "false_negative": false_negative,
            },
        },
    }


def test_planner_calibration_insufficient_data_has_metrics_but_no_recommendations() -> None:
    analytics = analyze_planner_calibration([record(i) for i in range(10)], minimum_sample_size=20)

    assert analytics["status"] == "insufficient_data"
    assert analytics["sample_size"] == 10
    assert analytics["metrics"]["average_quality_score"] == 95
    assert analytics["recommendations"] == []


def test_planner_calibration_overconfident_recommends_confidence_decrease() -> None:
    records = [
        record(i, confidence_label="overconfident", confidence_error=0.35, outcome_score=60)
        if i < 8
        else record(i)
        for i in range(20)
    ]

    analytics = analyze_planner_calibration(records, minimum_sample_size=20)

    recommendation = next(item for item in analytics["recommendations"] if item["policy"] == "planning_decision_confidence")
    assert recommendation["direction"] == "decrease"
    assert "confidence_overestimated" in recommendation["reason_codes"]
    assert recommendation["status"] == "recommendation_only"


def test_planner_calibration_risk_underestimated_recommends_policy_increase() -> None:
    records = [
        record(i, risk_label="underestimated", outcome="testing_failed", outcome_score=35)
        if i < 7
        else record(i)
        for i in range(20)
    ]

    analytics = analyze_planner_calibration(records, minimum_sample_size=20)

    recommendation = next(item for item in analytics["recommendations"] if item["policy"] == "planning_risk_policy")
    assert recommendation["direction"] == "increase"
    assert recommendation["severity"] in {"medium", "high"}


def test_planner_calibration_false_positives_recommend_gate_decrease() -> None:
    records = [
        record(i, false_positive=True, refinement_effective=False)
        if i < 5
        else record(i, refinement_effective=False if i < 12 else None)
        for i in range(20)
    ]

    analytics = analyze_planner_calibration(records, minimum_sample_size=20)

    recommendation = next(item for item in analytics["recommendations"] if item["policy"] == "quality_gate_threshold")
    assert recommendation["direction"] == "decrease"
    assert "quality_gate_false_positives_high" in recommendation["reason_codes"]


def test_planner_calibration_false_negatives_recommend_gate_increase() -> None:
    records = [
        record(i, false_negative=True, outcome="successful_with_repair", outcome_score=70)
        if i < 4
        else record(i)
        for i in range(20)
    ]

    analytics = analyze_planner_calibration(records, minimum_sample_size=20)

    recommendation = next(item for item in analytics["recommendations"] if item["policy"] == "quality_gate_threshold")
    assert recommendation["direction"] == "increase"
    assert "quality_gate_false_negatives_high" in recommendation["reason_codes"]


def test_planner_calibration_good_dataset_has_no_recommendations() -> None:
    analytics = analyze_planner_calibration([record(i) for i in range(25)], minimum_sample_size=20)

    assert analytics["status"] == "ready"
    assert analytics["metrics"]["well_calibrated_rate"] == 1
    assert analytics["recommendations"] == []


def test_planner_calibration_segment_specific_recommendation() -> None:
    records = []
    records.extend(
        record(i, framework="dotnet", confidence_label="overconfident", confidence_error=0.25, outcome_score=70)
        for i in range(10)
    )
    records.extend(record(i + 10, framework="fastapi") for i in range(30))

    analytics = analyze_planner_calibration(records, minimum_sample_size=20, segment_min_sample_size=10)

    assert analytics["metrics"]["overconfident_rate"] < 0.30
    recommendation = next(item for item in analytics["recommendations"] if item.get("segment") == "framework:dotnet")
    assert recommendation["policy"] == "planning_decision_confidence"
    assert recommendation["direction"] == "decrease"


def test_planner_calibration_is_deterministic_and_deduplicated() -> None:
    records = [
        record(i, risk_label="underestimated", outcome="testing_failed", outcome_score=35)
        if i < 7
        else record(i)
        for i in range(20)
    ]

    first = analyze_planner_calibration(records, minimum_sample_size=20)
    second = analyze_planner_calibration(records, minimum_sample_size=20)

    assert first == second
    ids = [item["recommendation_id"] for item in first["recommendations"]]
    assert len(ids) == len(set(ids))


def test_planner_calibration_mixed_versions_uses_current_and_reports_breakdown() -> None:
    records = [record(i) for i in range(20)] + [record(100 + i, version="7.7-v2") for i in range(5)]

    analytics = analyze_planner_calibration(records, minimum_sample_size=20)

    assert analytics["sample_size"] == 20
    assert analytics["version_breakdown"] == {"7.7-v1": 20, "7.7-v2": 5}


class FakePlannerCalibration:
    async def analyze(self, **kwargs):
        return {
            "version": "7.8-v1",
            "sample_size": 0,
            "status": "insufficient_data",
            "metrics": {},
            "segments": {},
            "recommendations": [],
            "query": kwargs,
        }


def test_planner_calibration_endpoint_is_read_only() -> None:
    services = SimpleNamespace(planner_calibration=FakePlannerCalibration())

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory():
        yield services

    with TestClient(create_app(factory)) as client:
        response = client.get("/api/evaluations/planner/calibration?limit=50&framework=fastapi")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "insufficient_data"
    assert payload["recommendations"] == []
    assert payload["query"]["framework"] == "fastapi"
