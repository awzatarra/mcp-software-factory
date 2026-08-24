from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.planner_calibration_service import PlannerCalibrationService
from api.services.planner_policy_proposal_store import PlannerPolicyProposalStore
from api.services.planner_policy_runtime_store import PlannerPolicyRuntimeStore
from api.services.planner_recommendation_review_store import PlannerRecommendationReviewStore
from graph.planner_calibration import PlannerPolicySnapshot
from graph.planner_policy_proposals import PlannerPolicyProposalError
from tests.test_planner_policy_proposals import quality_records


class DirectPlannerCalibrationService(PlannerCalibrationService):
    def __init__(
        self,
        review_store: PlannerRecommendationReviewStore,
        proposal_store: PlannerPolicyProposalStore,
        runtime_store: PlannerPolicyRuntimeStore,
    ) -> None:
        super().__init__(
            query=SimpleNamespace(),
            metadata_store=None,
            review_store=review_store,
            proposal_store=proposal_store,
            policy_runtime_store=runtime_store,
        )
        self._records = quality_records()

    async def records(self, **kwargs):
        return list(self._records)


@pytest.fixture()
async def application_system(tmp_path):
    database = tmp_path / "application.sqlite"
    review_store = PlannerRecommendationReviewStore(database)
    proposal_store = PlannerPolicyProposalStore(database)
    runtime_store = PlannerPolicyRuntimeStore(database)
    await review_store.initialize()
    await proposal_store.initialize()
    await runtime_store.initialize()
    service = DirectPlannerCalibrationService(review_store, proposal_store, runtime_store)
    analytics = await service.analyze(minimum_sample_size=20)
    recommendation = next(item for item in analytics["recommendations"] if item["policy"] == "quality_gate_threshold")
    await service.review(
        recommendation["recommendation_id"],
        action="accept",
        reviewer="qa-lead",
        fingerprint=recommendation["recommendation_fingerprint"],
    )
    proposal = await service.create_policy_proposal(recommendation["recommendation_id"])
    await service.transition_policy_proposal(
        proposal["proposal_id"],
        action="ready",
        actor="architect",
        fingerprint=proposal["proposal_fingerprint"],
    )
    approved = await service.transition_policy_proposal(
        proposal["proposal_id"],
        action="approve",
        actor="architect",
        fingerprint=proposal["proposal_fingerprint"],
    )
    return service, runtime_store, approved


@pytest.mark.asyncio
async def test_valid_apply_updates_runtime_and_revision(application_system) -> None:
    service, runtime_store, proposal = application_system

    application = await service.prepare_policy_application(proposal["proposal_id"], actor="operator")
    applied = await service.apply_policy_application(
        application["application_id"],
        actor="operator",
        fingerprint=application["application_fingerprint"],
    )
    runtime = await runtime_store.get_policy("planning.quality_gate.weak_threshold", "global_planner")
    refreshed = await service.policy_proposal(proposal["proposal_id"])

    assert application["status"] == "prepared"
    assert application["previous_value"] == 60
    assert application["proposed_value"] == 55
    assert applied["status"] == "applied"
    assert applied["applied_revision"] == 1
    assert runtime["value"] == 55
    assert runtime["revision"] == 1
    assert refreshed["application_status"] == "applied"


@pytest.mark.asyncio
async def test_apply_requires_approved_proposal(application_system) -> None:
    service, _, proposal = application_system
    assert service.proposal_store is not None
    with service.proposal_store.connect() as connection:
        connection.execute(
            "UPDATE planner_policy_proposals SET status='rejected',application_status='not_applicable' WHERE proposal_id=?",
            (proposal["proposal_id"],),
        )

    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_application_not_approved"):
        await service.prepare_policy_application(proposal["proposal_id"], actor="operator")


@pytest.mark.asyncio
async def test_review_only_proposal_is_not_applicable(tmp_path) -> None:
    database = tmp_path / "review-only.sqlite"
    review_store = PlannerRecommendationReviewStore(database)
    proposal_store = PlannerPolicyProposalStore(database)
    runtime_store = PlannerPolicyRuntimeStore(database)
    await review_store.initialize()
    await proposal_store.initialize()
    await runtime_store.initialize()
    proposal, _ = await proposal_store.create({
        "proposal_version": "7.10-v1",
        "source_recommendation_id": "rec",
        "source_recommendation_fingerprint": "rfp",
        "source_policy": "quality_refinement_strategy",
        "source_segment": None,
        "source_direction": "review",
        "policy_key": "planning.refinement.strategy",
        "policy_scope": "global_planner",
        "segment": None,
        "current_value": "current_guidance",
        "proposed_value": None,
        "change_type": "review_only",
        "rationale": "review",
        "evidence_summary": {},
        "reason_codes": ["review"],
        "proposal_fingerprint": "pfp",
        "proposal_risk_level": "medium",
        "affected_policy_area": "refinement",
        "affected_workflows_scope": "global_planner",
        "simulation": None,
        "safety_flags": {},
        "absolute_change": None,
        "relative_change_pct": None,
    })
    await proposal_store.transition(proposal["proposal_id"], to_status="ready_for_review", actor="a", fingerprint="pfp")
    approved, _ = await proposal_store.transition(proposal["proposal_id"], to_status="approved", actor="a", fingerprint="pfp")
    service = DirectPlannerCalibrationService(review_store, proposal_store, runtime_store)

    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_application_not_applicable"):
        await service.prepare_policy_application(approved["proposal_id"], actor="operator")


@pytest.mark.asyncio
async def test_stale_baseline_blocks_apply(application_system) -> None:
    service, runtime_store, proposal = application_system
    await runtime_store.set_policy(
        "planning.quality_gate.weak_threshold",
        "global_planner",
        58,
        expected_value=60,
        expected_revision=0,
    )

    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_application_stale"):
        await service.prepare_policy_application(proposal["proposal_id"], actor="operator")

    runtime = await runtime_store.get_policy("planning.quality_gate.weak_threshold", "global_planner")
    assert runtime["value"] == 58


@pytest.mark.asyncio
async def test_apply_is_idempotent(application_system) -> None:
    service, runtime_store, proposal = application_system
    application = await service.prepare_policy_application(proposal["proposal_id"], actor="operator")

    first = await service.apply_policy_application(application["application_id"], actor="operator", fingerprint=application["application_fingerprint"])
    second = await service.apply_policy_application(application["application_id"], actor="operator", fingerprint=application["application_fingerprint"])

    assert first["status"] == second["status"] == "applied"
    assert (await runtime_store.get_policy("planning.quality_gate.weak_threshold", "global_planner"))["revision"] == 1


@pytest.mark.asyncio
async def test_manual_rollback_restores_previous_value_and_increments_revision(application_system) -> None:
    service, runtime_store, proposal = application_system
    application = await service.prepare_policy_application(proposal["proposal_id"], actor="operator")
    applied = await service.apply_policy_application(application["application_id"], actor="operator", fingerprint=application["application_fingerprint"])

    rolled = await service.rollback_policy_application(applied["application_id"], actor="operator", fingerprint=applied["application_fingerprint"])
    runtime = await runtime_store.get_policy("planning.quality_gate.weak_threshold", "global_planner")
    refreshed = await service.policy_proposal(proposal["proposal_id"])

    assert rolled["status"] == "rolled_back"
    assert runtime["value"] == 60
    assert runtime["revision"] == 2
    assert refreshed["application_status"] == "rolled_back"


@pytest.mark.asyncio
async def test_rollback_stale_does_not_overwrite_newer_policy(application_system) -> None:
    service, runtime_store, proposal = application_system
    application = await service.prepare_policy_application(proposal["proposal_id"], actor="operator")
    applied = await service.apply_policy_application(application["application_id"], actor="operator", fingerprint=application["application_fingerprint"])
    await runtime_store.set_policy(
        "planning.quality_gate.weak_threshold",
        "global_planner",
        50,
        expected_value=55,
        expected_revision=1,
    )

    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_rollback_stale"):
        await service.rollback_policy_application(applied["application_id"], actor="operator", fingerprint=applied["application_fingerprint"])

    assert (await runtime_store.get_policy("planning.quality_gate.weak_threshold", "global_planner"))["value"] == 50


@pytest.mark.asyncio
async def test_no_file_mutation_during_apply(application_system) -> None:
    service, _, proposal = application_system
    env_path = Path(".env")
    before = env_path.read_bytes() if env_path.exists() else b""
    application = await service.prepare_policy_application(proposal["proposal_id"], actor="operator")
    await service.apply_policy_application(application["application_id"], actor="operator", fingerprint=application["application_fingerprint"])
    after = env_path.read_bytes() if env_path.exists() else b""
    assert after == before


def test_policy_application_api_flow(tmp_path) -> None:
    database = tmp_path / "api-application.sqlite"
    review_store = PlannerRecommendationReviewStore(database)
    proposal_store = PlannerPolicyProposalStore(database)
    runtime_store = PlannerPolicyRuntimeStore(database)
    review_store.initialize_sync()
    proposal_store.initialize_sync()
    runtime_store.initialize_sync()
    service = DirectPlannerCalibrationService(review_store, proposal_store, runtime_store)

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(planner_calibration=service)

    with TestClient(create_app(factory)) as client:
        recommendation = next(item for item in client.get("/api/evaluations/planner/recommendations").json()["items"] if item["policy"] == "quality_gate_threshold")
        client.post(f"/api/evaluations/planner/recommendations/{recommendation['recommendation_id']}/accept", json={"reviewer": "qa", "fingerprint": recommendation["recommendation_fingerprint"]})
        proposal = client.post(f"/api/evaluations/planner/recommendations/{recommendation['recommendation_id']}/policy-proposal").json()
        client.post(f"/api/evaluations/planner/policy-proposals/{proposal['proposal_id']}/ready", json={"actor": "architect", "proposal_fingerprint": proposal["proposal_fingerprint"]})
        proposal = client.post(f"/api/evaluations/planner/policy-proposals/{proposal['proposal_id']}/approve", json={"actor": "architect", "proposal_fingerprint": proposal["proposal_fingerprint"]}).json()
        prepared = client.post(f"/api/evaluations/planner/policy-proposals/{proposal['proposal_id']}/application/prepare", json={"actor": "operator"}).json()
        applied = client.post(f"/api/evaluations/planner/policy-applications/{prepared['application_id']}/apply", json={"actor": "operator", "application_fingerprint": prepared["application_fingerprint"]}).json()
        rolled = client.post(f"/api/evaluations/planner/policy-applications/{prepared['application_id']}/rollback", json={"actor": "operator", "application_fingerprint": prepared["application_fingerprint"]}).json()
        listed = client.get("/api/evaluations/planner/policy-applications").json()

    assert applied["status"] == "applied"
    assert rolled["status"] == "rolled_back"
    assert listed["items"][0]["application_id"] == prepared["application_id"]
    assert PlannerPolicySnapshot().quality_gate_weak_threshold == 60
