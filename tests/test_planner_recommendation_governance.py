from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.planner_calibration_service import PlannerCalibrationService, PlannerRecommendationError
from api.services.planner_recommendation_review_store import PlannerRecommendationReviewStore
from graph.planner_calibration import PlannerPolicySnapshot, analyze_planner_calibration
from graph.planner_recommendation_governance import governance_recommendation, recommendation_fingerprint


def record(index: int, *, risk_label: str = "underestimated", outcome_score: int = 35) -> dict:
    return {
        "workflow_id": f"workflow-{index}",
        "framework": "fastapi",
        "risk_level": "low",
        "quality_level": "weak",
        "approval_required": False,
        "created_at": (datetime(2026, 8, 1, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
        "planning_evaluation": {
            "version": "7.7-v1",
            "outcome": "testing_failed" if risk_label == "underestimated" else "successful",
            "outcome_score": outcome_score,
            "quality_prediction": {"predicted": 95, "observed": outcome_score, "absolute_error": abs(95 - outcome_score)},
            "confidence_calibration": {
                "predicted": 0.95,
                "observed": outcome_score / 100,
                "signed_error": 0.95 - outcome_score / 100,
                "absolute_error": abs(0.95 - outcome_score / 100),
                "label": "overconfident",
            },
            "risk_calibration": {"predicted": "low", "observed": "high", "label": risk_label},
            "refinement": {"attempts": 0, "effective": None},
            "quality_gate": {"false_positive": False, "false_negative": False},
        },
    }


def records(count: int = 20) -> list[dict]:
    return [record(index) for index in range(count)]


class DirectPlannerCalibrationService(PlannerCalibrationService):
    def __init__(self, review_store: PlannerRecommendationReviewStore, initial_records: list[dict]) -> None:
        super().__init__(query=SimpleNamespace(), metadata_store=None, review_store=review_store)
        self._records = initial_records

    async def records(self, **kwargs):
        return list(self._records)


@pytest.fixture()
async def governance(tmp_path):
    store = PlannerRecommendationReviewStore(tmp_path / "reviews.sqlite")
    await store.initialize()
    service = DirectPlannerCalibrationService(store, records())
    analytics = await service.analyze(minimum_sample_size=20)
    recommendation = next(item for item in analytics["recommendations"] if item["policy"] == "planning_risk_policy")
    return service, store, recommendation


def test_recommendation_fingerprint_is_content_bound() -> None:
    analytics = analyze_planner_calibration(records(), minimum_sample_size=20)
    recommendation = analytics["recommendations"][0]
    first = recommendation_fingerprint(recommendation, analytics_version=analytics["version"])
    changed = {**recommendation, "evidence": {**recommendation["evidence"], "sample_size": 21}}
    second = recommendation_fingerprint(changed, analytics_version=analytics["version"])

    assert first == recommendation_fingerprint(recommendation, analytics_version=analytics["version"])
    assert first != second


@pytest.mark.asyncio
async def test_accept_persists_review_and_does_not_apply_policy(governance) -> None:
    service, store, recommendation = governance
    before = PlannerPolicySnapshot().model_dump()

    accepted = await service.review(
        recommendation["recommendation_id"],
        action="accept",
        reviewer="qa-lead",
        notes="Conceptually approved.",
        reason="Calibration evidence is strong.",
        fingerprint=recommendation["recommendation_fingerprint"],
    )

    assert accepted["review"]["status"] == "accepted"
    assert accepted["review"]["reviewer"] == "qa-lead"
    assert accepted["review"]["reviewed_at"]
    assert accepted["application_status"] == "not_applied"
    assert PlannerPolicySnapshot().model_dump() == before
    history = await store.history(recommendation["recommendation_id"], recommendation["recommendation_fingerprint"])
    assert len(history) == 1
    assert history[0]["from_status"] == "recommendation_only"
    assert history[0]["to_status"] == "accepted"


@pytest.mark.asyncio
async def test_reject_preserves_original_recommendation_evidence(governance) -> None:
    service, store, recommendation = governance

    rejected = await service.review(
        recommendation["recommendation_id"],
        action="reject",
        reviewer="architect",
        reason="Dataset is not representative.",
        notes="Collect more FastAPI samples.",
        fingerprint=recommendation["recommendation_fingerprint"],
    )

    row = await store.get(recommendation["recommendation_id"], recommendation["recommendation_fingerprint"])
    assert rejected["review"]["status"] == "rejected"
    assert row is not None
    assert row["recommendation"]["evidence"] == recommendation["evidence"]
    assert row["decision_reason"] == "Dataset is not representative."


@pytest.mark.asyncio
async def test_defer_persists_deferred_until(governance) -> None:
    service, _, recommendation = governance

    deferred = await service.review(
        recommendation["recommendation_id"],
        action="defer",
        reviewer="planner-owner",
        reason="Review after more workflows.",
        deferred_until="after 50 workflows",
        fingerprint=recommendation["recommendation_fingerprint"],
    )

    assert deferred["review"]["status"] == "deferred"
    assert deferred["review"]["deferred_until"] == "after 50 workflows"


@pytest.mark.asyncio
async def test_stale_fingerprint_is_rejected_without_state_change(governance) -> None:
    service, store, recommendation = governance

    with pytest.raises(PlannerRecommendationError, match="planner_recommendation_stale"):
        await service.review(
            recommendation["recommendation_id"],
            action="accept",
            reviewer="qa-lead",
            fingerprint="stale",
        )

    assert await store.get(recommendation["recommendation_id"], recommendation["recommendation_fingerprint"]) is None


@pytest.mark.asyncio
async def test_accept_is_idempotent_for_same_fingerprint(governance) -> None:
    service, store, recommendation = governance
    payload = {
        "action": "accept",
        "reviewer": "qa-lead",
        "fingerprint": recommendation["recommendation_fingerprint"],
    }

    first = await service.review(recommendation["recommendation_id"], **payload)
    second = await service.review(recommendation["recommendation_id"], **payload)

    assert first["review"]["status"] == second["review"]["status"] == "accepted"
    history = await store.history(recommendation["recommendation_id"], recommendation["recommendation_fingerprint"])
    assert len(history) == 1


@pytest.mark.asyncio
async def test_new_fingerprint_does_not_inherit_accepted_review(governance) -> None:
    service, _, recommendation = governance
    await service.review(
        recommendation["recommendation_id"],
        action="accept",
        reviewer="qa-lead",
        fingerprint=recommendation["recommendation_fingerprint"],
    )
    service._records = records(21)

    refreshed = await service.recommendation(recommendation["recommendation_id"])

    assert refreshed is not None
    assert refreshed["recommendation_fingerprint"] != recommendation["recommendation_fingerprint"]
    assert refreshed["review"]["status"] == "recommendation_only"


@pytest.mark.asyncio
async def test_review_history_records_start_then_defer(governance) -> None:
    service, store, recommendation = governance

    await service.review(
        recommendation["recommendation_id"],
        action="start",
        reviewer="owner",
        notes="Taking review.",
        fingerprint=recommendation["recommendation_fingerprint"],
    )
    await service.review(
        recommendation["recommendation_id"],
        action="defer",
        reviewer="owner",
        reason="Needs more data.",
        fingerprint=recommendation["recommendation_fingerprint"],
    )

    history = await store.history(recommendation["recommendation_id"], recommendation["recommendation_fingerprint"])
    assert [(item["from_status"], item["to_status"]) for item in history] == [
        ("recommendation_only", "under_review"),
        ("under_review", "deferred"),
    ]


@pytest.mark.asyncio
async def test_calibration_enriches_accepted_recommendation(governance) -> None:
    service, _, recommendation = governance
    await service.review(
        recommendation["recommendation_id"],
        action="accept",
        reviewer="qa-lead",
        fingerprint=recommendation["recommendation_fingerprint"],
    )

    analytics = await service.analyze(minimum_sample_size=20)
    accepted = next(item for item in analytics["recommendations"] if item["recommendation_id"] == recommendation["recommendation_id"])

    assert accepted["review"]["status"] == "accepted"
    assert analytics["review_metrics"]["recommendations_accepted"] == 1


@pytest.mark.asyncio
async def test_recommendation_list_filters_by_status_and_policy(governance) -> None:
    service, _, recommendation = governance
    await service.review(
        recommendation["recommendation_id"],
        action="accept",
        reviewer="qa-lead",
        fingerprint=recommendation["recommendation_fingerprint"],
    )

    result = await service.recommendations(status="accepted", policy="planning_risk_policy")

    assert [item["recommendation_id"] for item in result["items"]] == [recommendation["recommendation_id"]]


def test_governance_does_not_change_analytics_core() -> None:
    analytics = analyze_planner_calibration(records(), minimum_sample_size=20)

    assert "review_metrics" not in analytics
    assert "review" not in analytics["recommendations"][0]


def test_api_review_endpoints_accept_and_enrich(tmp_path) -> None:
    store = PlannerRecommendationReviewStore(tmp_path / "api-reviews.sqlite")
    store.initialize_sync()
    service = DirectPlannerCalibrationService(store, records())

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(planner_calibration=service)

    with TestClient(create_app(factory)) as client:
        listed = client.get("/api/evaluations/planner/recommendations").json()
        recommendation = next(item for item in listed["items"] if item["policy"] == "planning_risk_policy")
        response = client.post(
            f"/api/evaluations/planner/recommendations/{recommendation['recommendation_id']}/accept",
            json={
                "reviewer": "qa-lead",
                "notes": "Approved conceptually.",
                "decision_reason": "Evidence is enough.",
                "fingerprint": recommendation["recommendation_fingerprint"],
            },
        )
        refreshed = client.get("/api/evaluations/planner/calibration").json()

    assert response.status_code == 200
    assert response.json()["review"]["status"] == "accepted"
    accepted = next(item for item in refreshed["recommendations"] if item["recommendation_id"] == recommendation["recommendation_id"])
    assert accepted["review"]["status"] == "accepted"


def test_governance_recommendation_exposes_fingerprint_alias() -> None:
    analytics = analyze_planner_calibration(records(), minimum_sample_size=20)
    item = governance_recommendation(analytics["recommendations"][0], analytics_version=analytics["version"])

    assert item["fingerprint"] == item["recommendation_fingerprint"]
    assert len(item["fingerprint"]) == 64
