from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.planner_calibration_service import PlannerCalibrationService
from api.services.planner_policy_proposal_store import PlannerPolicyProposalStore
from api.services.planner_recommendation_review_store import PlannerRecommendationReviewStore
from graph.planner_calibration import PlannerPolicySnapshot, analyze_planner_calibration
from graph.planner_policy_proposals import (
    PlannerPolicyProposalError,
    build_policy_change_proposal,
)


def record(
    index: int,
    *,
    outcome_score: int = 100,
    false_positive: bool = False,
    refinement_effective=None,
) -> dict:
    return {
        "workflow_id": f"workflow-{index}",
        "framework": "fastapi",
        "risk_level": "low",
        "quality_level": "excellent",
        "approval_required": False,
        "created_at": (datetime(2026, 8, 1, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
        "planning_evaluation": {
            "version": "7.7-v1",
            "outcome": "successful",
            "outcome_score": outcome_score,
            "quality_prediction": {"predicted": 95, "observed": outcome_score, "absolute_error": abs(95 - outcome_score)},
            "confidence_calibration": {
                "predicted": 0.95,
                "observed": outcome_score / 100,
                "signed_error": 0.95 - outcome_score / 100,
                "absolute_error": abs(0.95 - outcome_score / 100),
                "label": "well_calibrated",
            },
            "risk_calibration": {"predicted": "low", "observed": "low", "label": "aligned"},
            "refinement": {"attempts": 1 if refinement_effective is not None else 0, "effective": refinement_effective},
            "quality_gate": {"false_positive": false_positive, "false_negative": False},
        },
    }


def quality_records() -> list[dict]:
    return [
        record(index, false_positive=True, refinement_effective=False, outcome_score=57 if index < 4 else 100)
        if index < 5
        else record(index, refinement_effective=False if index < 12 else None, outcome_score=100)
        for index in range(20)
    ]


def quality_recommendation() -> dict:
    analytics = analyze_planner_calibration(quality_records(), minimum_sample_size=20)
    return next(item for item in analytics["recommendations"] if item["policy"] == "quality_gate_threshold")


def review_only_recommendation() -> dict:
    return {
        "recommendation_id": "review-only",
        "recommendation_fingerprint": "fingerprint-review",
        "analytics_version": "7.8-v1",
        "policy": "quality_refinement_strategy",
        "segment": None,
        "direction": "review",
        "current_value": "current_guidance",
        "suggested_value": {"review": "quality refinement guidance"},
        "severity": "medium",
        "confidence": 0.8,
        "evidence": {"sample_size": 20, "refinement_effectiveness_rate": 0.2},
        "reason_codes": ["refinement_effectiveness_low"],
    }


class DirectPlannerCalibrationService(PlannerCalibrationService):
    def __init__(
        self,
        review_store: PlannerRecommendationReviewStore,
        proposal_store: PlannerPolicyProposalStore,
        initial_records: list[dict],
    ) -> None:
        super().__init__(
            query=SimpleNamespace(),
            metadata_store=None,
            review_store=review_store,
            proposal_store=proposal_store,
        )
        self._records = initial_records

    async def records(self, **kwargs):
        return list(self._records)


@pytest.fixture()
async def proposal_system(tmp_path):
    review_store = PlannerRecommendationReviewStore(tmp_path / "proposal.sqlite")
    proposal_store = PlannerPolicyProposalStore(tmp_path / "proposal.sqlite")
    await review_store.initialize()
    await proposal_store.initialize()
    service = DirectPlannerCalibrationService(review_store, proposal_store, quality_records())
    analytics = await service.analyze(minimum_sample_size=20)
    recommendation = next(item for item in analytics["recommendations"] if item["policy"] == "quality_gate_threshold")
    return service, review_store, proposal_store, recommendation


def test_valid_threshold_change_builds_draft_fields() -> None:
    recommendation = quality_recommendation()
    recommendation["recommendation_fingerprint"] = "fingerprint-quality"
    proposal = build_policy_change_proposal(recommendation, PlannerPolicySnapshot(), records=quality_records())

    assert proposal["policy_key"] == "planning.quality_gate.weak_threshold"
    assert proposal["current_value"] == 60
    assert proposal["proposed_value"] == 55
    assert proposal["change_type"] == "decrease"
    assert proposal["application_status"] if "application_status" in proposal else True
    assert proposal["proposal_fingerprint"]
    assert proposal["absolute_change"] == -5
    assert proposal["relative_change_pct"] == -8.33


def test_review_only_proposal_has_no_proposed_value_or_simulation() -> None:
    proposal = build_policy_change_proposal(review_only_recommendation(), PlannerPolicySnapshot(), records=quality_records())

    assert proposal["change_type"] == "review_only"
    assert proposal["proposed_value"] is None
    assert proposal["simulation"] is None
    assert proposal["proposal_risk_level"] == "medium"


def test_invalid_confidence_value_is_rejected() -> None:
    recommendation = {
        **review_only_recommendation(),
        "policy": "planning_decision_confidence",
        "direction": "increase",
        "suggested_value": {"suggested_value": 1.5},
    }

    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_proposal_invalid_value"):
        build_policy_change_proposal(recommendation, PlannerPolicySnapshot())


def test_threshold_simulation_is_deterministic() -> None:
    recommendation = quality_recommendation()
    recommendation["recommendation_fingerprint"] = "fingerprint-quality"

    first = build_policy_change_proposal(recommendation, PlannerPolicySnapshot(), records=quality_records())
    second = build_policy_change_proposal(recommendation, PlannerPolicySnapshot(), records=quality_records())

    assert first["proposal_fingerprint"] == second["proposal_fingerprint"]
    assert first["simulation"] == second["simulation"]
    assert first["simulation"]["sample_size"] == 20
    assert first["simulation"]["before_decisions"]["refinement_triggered"] == 4
    assert first["simulation"]["after_decisions"]["refinement_triggered"] == 0
    assert first["simulation"]["changed_decision_count"] == 4


def test_policy_change_risk_flags_reduced_quality_gate_safety() -> None:
    recommendation = quality_recommendation()
    recommendation["recommendation_fingerprint"] = "fingerprint-quality"
    proposal = build_policy_change_proposal(recommendation, PlannerPolicySnapshot(), records=quality_records())

    assert proposal["proposal_risk_level"] == "medium"
    assert proposal["safety_flags"]["reduces_safety"] is True
    assert proposal["safety_flags"]["increases_failure_tolerance"] is True


@pytest.mark.asyncio
async def test_source_not_accepted_cannot_create_proposal(proposal_system) -> None:
    service, _, _, recommendation = proposal_system

    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_proposal_source_not_accepted"):
        await service.create_policy_proposal(recommendation["recommendation_id"])


@pytest.mark.asyncio
async def test_accepted_recommendation_creates_draft_and_duplicate_returns_same_id(proposal_system) -> None:
    service, _, _, recommendation = proposal_system
    before = PlannerPolicySnapshot().model_dump()
    await service.review(
        recommendation["recommendation_id"],
        action="accept",
        reviewer="qa-lead",
        fingerprint=recommendation["recommendation_fingerprint"],
    )

    first = await service.create_policy_proposal(recommendation["recommendation_id"])
    second = await service.create_policy_proposal(recommendation["recommendation_id"])

    assert first["proposal_id"] == second["proposal_id"]
    assert first["status"] == "draft"
    assert first["application_status"] == "not_applied"
    assert first["current_value"] == 60
    assert first["proposed_value"] == 55
    assert PlannerPolicySnapshot().model_dump() == before


@pytest.mark.asyncio
async def test_ready_and_approve_keep_policy_not_applied(proposal_system) -> None:
    service, _, _, recommendation = proposal_system
    before = PlannerPolicySnapshot().model_dump()
    await service.review(recommendation["recommendation_id"], action="accept", reviewer="qa-lead", fingerprint=recommendation["recommendation_fingerprint"])
    proposal = await service.create_policy_proposal(recommendation["recommendation_id"])

    ready = await service.transition_policy_proposal(
        proposal["proposal_id"],
        action="ready",
        actor="architect",
        fingerprint=proposal["proposal_fingerprint"],
    )
    approved = await service.transition_policy_proposal(
        proposal["proposal_id"],
        action="approve",
        actor="architect",
        notes="Approved conceptually.",
        fingerprint=proposal["proposal_fingerprint"],
    )

    assert ready["status"] == "ready_for_review"
    assert approved["status"] == "approved"
    assert approved["application_status"] == "not_applied"
    assert PlannerPolicySnapshot().model_dump() == before


@pytest.mark.asyncio
async def test_reject_sets_not_applicable(proposal_system) -> None:
    service, _, _, recommendation = proposal_system
    await service.review(recommendation["recommendation_id"], action="accept", reviewer="qa-lead", fingerprint=recommendation["recommendation_fingerprint"])
    proposal = await service.create_policy_proposal(recommendation["recommendation_id"])
    await service.transition_policy_proposal(proposal["proposal_id"], action="ready", actor="architect", fingerprint=proposal["proposal_fingerprint"])

    rejected = await service.transition_policy_proposal(
        proposal["proposal_id"],
        action="reject",
        actor="architect",
        reason="Too risky.",
        fingerprint=proposal["proposal_fingerprint"],
    )

    assert rejected["status"] == "rejected"
    assert rejected["application_status"] == "not_applicable"


@pytest.mark.asyncio
async def test_stale_policy_blocks_ready(proposal_system) -> None:
    service, _, proposal_store, recommendation = proposal_system
    await service.review(recommendation["recommendation_id"], action="accept", reviewer="qa-lead", fingerprint=recommendation["recommendation_fingerprint"])
    proposal = await service.create_policy_proposal(recommendation["recommendation_id"])
    with proposal_store.connect() as connection:
        connection.execute(
            "UPDATE planner_policy_proposals SET current_value_json=? WHERE proposal_id=?",
            ("58", proposal["proposal_id"]),
        )

    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_proposal_stale"):
        await service.transition_policy_proposal(
            proposal["proposal_id"],
            action="ready",
            actor="architect",
            fingerprint=proposal["proposal_fingerprint"],
        )


def test_api_policy_proposal_flow(tmp_path) -> None:
    review_store = PlannerRecommendationReviewStore(tmp_path / "api-proposal.sqlite")
    proposal_store = PlannerPolicyProposalStore(tmp_path / "api-proposal.sqlite")
    review_store.initialize_sync()
    proposal_store.initialize_sync()
    service = DirectPlannerCalibrationService(review_store, proposal_store, quality_records())

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(planner_calibration=service)

    with TestClient(create_app(factory)) as client:
        listed = client.get("/api/evaluations/planner/recommendations").json()
        recommendation = next(item for item in listed["items"] if item["policy"] == "quality_gate_threshold")
        client.post(
            f"/api/evaluations/planner/recommendations/{recommendation['recommendation_id']}/accept",
            json={"reviewer": "qa-lead", "fingerprint": recommendation["recommendation_fingerprint"]},
        )
        created = client.post(f"/api/evaluations/planner/recommendations/{recommendation['recommendation_id']}/policy-proposal")
        proposal = created.json()
        ready = client.post(
            f"/api/evaluations/planner/policy-proposals/{proposal['proposal_id']}/ready",
            json={"actor": "architect", "proposal_fingerprint": proposal["proposal_fingerprint"]},
        )
        approved = client.post(
            f"/api/evaluations/planner/policy-proposals/{proposal['proposal_id']}/approve",
            json={"actor": "architect", "proposal_fingerprint": proposal["proposal_fingerprint"], "notes": "Approved."},
        )
        listed_proposals = client.get("/api/evaluations/planner/policy-proposals?status=approved").json()

    assert created.status_code == 201
    assert ready.status_code == 200
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["application_status"] == "not_applied"
    assert [item["proposal_id"] for item in listed_proposals["items"]] == [proposal["proposal_id"]]
