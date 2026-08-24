from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.planner_calibration_service import PlannerCalibrationService
from api.services.planner_policy_proposal_store import PlannerPolicyProposalStore
from api.services.planner_policy_runtime_store import PlannerPolicyRuntimeStore
from api.services.planner_recommendation_review_store import PlannerRecommendationReviewStore
from graph.planner_policy_proposals import PlannerPolicyProposalError
from graph.planner_policy_rollout import (
    evaluate_rollout_health,
    is_workflow_in_rollout,
    stable_rollout_bucket,
)
from tests.test_planner_policy_proposals import quality_records


class RolloutPlannerCalibrationService(PlannerCalibrationService):
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
async def rollout_system(tmp_path):
    database = tmp_path / "rollout.sqlite"
    review_store = PlannerRecommendationReviewStore(database)
    proposal_store = PlannerPolicyProposalStore(database)
    runtime_store = PlannerPolicyRuntimeStore(database)
    await review_store.initialize()
    await proposal_store.initialize()
    await runtime_store.initialize()
    service = RolloutPlannerCalibrationService(review_store, proposal_store, runtime_store)
    analytics = await service.analyze(minimum_sample_size=20)
    recommendation = next(item for item in analytics["recommendations"] if item["policy"] == "quality_gate_threshold")
    await service.review(
        recommendation["recommendation_id"],
        action="accept",
        reviewer="qa-lead",
        fingerprint=recommendation["recommendation_fingerprint"],
    )
    proposal = await service.create_policy_proposal(recommendation["recommendation_id"])
    await service.transition_policy_proposal(proposal["proposal_id"], action="ready", actor="architect", fingerprint=proposal["proposal_fingerprint"])
    approved = await service.transition_policy_proposal(proposal["proposal_id"], action="approve", actor="architect", fingerprint=proposal["proposal_fingerprint"])
    application = await service.prepare_policy_application(approved["proposal_id"], actor="operator")
    applied = await service.apply_policy_application(application["application_id"], actor="operator", fingerprint=application["application_fingerprint"])
    return service, runtime_store, approved, applied


def rollout_record(index: int, rollout_id: str, *, treatment: bool, outcome: str = "successful", repair: bool = False, infra: bool = False) -> dict:
    return {
        "workflow_id": f"rollout-workflow-{index}",
        "planner_policy_rollout_assignments": [
            {
                "policy_key": "planning.quality_gate.weak_threshold",
                "rollout_id": rollout_id,
                "treatment": treatment,
                "effective_value": 55 if treatment else 60,
                "revision": 1 if treatment else 0,
            }
        ],
        "failure_type": "mcp_timeout" if infra else None,
        "planning_evaluation": {
            "version": "7.7-v1",
            "outcome": outcome,
            "outcome_score": 100 if outcome == "successful" else 0,
            "successful_with_repair": repair,
            "confidence_absolute_error": 0.01,
            "risk_calibration": "aligned",
            "quality_gate_error": None,
            "refinement": {"effective": True},
        },
    }


def add_stage_records(service: RolloutPlannerCalibrationService, rollout_id: str, *, treatment_failure_count: int = 0, treatment_repair_count: int = 0, infra_count: int = 0) -> None:
    records = quality_records()
    for index in range(10):
        records.append(rollout_record(index, rollout_id, treatment=True, outcome="failed" if index < treatment_failure_count else "successful", repair=index < treatment_repair_count))
    for index in range(10, 20):
        records.append(rollout_record(index, rollout_id, treatment=False))
    for index in range(20, 20 + infra_count):
        records.append(rollout_record(index, rollout_id, treatment=True, outcome="failed", infra=True))
    service._records = records


def treatment_workflow_id(rollout_id: str, percentage: int) -> str:
    for index in range(1000):
        workflow_id = f"candidate-{index}"
        if is_workflow_in_rollout(workflow_id=workflow_id, policy_key="planning.quality_gate.weak_threshold", rollout_id=rollout_id, percentage=percentage):
            return workflow_id
    raise AssertionError("no deterministic treatment candidate found")


def test_deterministic_assignment_and_subset_property() -> None:
    rollout_id = "rollout-stable"
    workflow_id = "workflow-stable"
    first = stable_rollout_bucket(workflow_id=workflow_id, policy_key="planning.quality_gate.weak_threshold", rollout_id=rollout_id)
    second = stable_rollout_bucket(workflow_id=workflow_id, policy_key="planning.quality_gate.weak_threshold", rollout_id=rollout_id)
    assert first == second
    sample = [f"workflow-{index}" for index in range(250)]
    ten = {item for item in sample if is_workflow_in_rollout(workflow_id=item, policy_key="planning.quality_gate.weak_threshold", rollout_id=rollout_id, percentage=10)}
    twenty_five = {item for item in sample if is_workflow_in_rollout(workflow_id=item, policy_key="planning.quality_gate.weak_threshold", rollout_id=rollout_id, percentage=25)}
    fifty = {item for item in sample if is_workflow_in_rollout(workflow_id=item, policy_key="planning.quality_gate.weak_threshold", rollout_id=rollout_id, percentage=50)}
    assert ten <= twenty_five <= fifty


def test_rollout_health_states() -> None:
    baseline = {"failure_rate": 0, "repair_rate": 0, "quality_gate_false_negative_rate": 0, "risk_underestimated_rate": 0, "average_confidence_absolute_error": 0, "policy_sample_size": 20}
    healthy = {"failure_rate": 0, "repair_rate": 0, "quality_gate_false_negative_rate": 0, "risk_underestimated_rate": 0, "average_confidence_absolute_error": 0.01, "policy_sample_size": 10}
    degraded = {**healthy, "repair_rate": 0.3}
    critical = {**healthy, "failure_rate": 0.2}
    observing = {**healthy, "policy_sample_size": 3}
    assert evaluate_rollout_health(baseline_metrics=baseline, treatment_metrics=healthy)["health_status"] == "healthy"
    assert evaluate_rollout_health(baseline_metrics=baseline, treatment_metrics=degraded)["health_status"] == "degraded"
    assert evaluate_rollout_health(baseline_metrics=baseline, treatment_metrics=critical)["health_status"] == "critical"
    assert evaluate_rollout_health(baseline_metrics=baseline, treatment_metrics=observing)["health_status"] == "observing"


@pytest.mark.asyncio
async def test_prepare_start_evaluate_and_advance_one_stage(rollout_system) -> None:
    service, _, _, application = rollout_system
    rollout = await service.prepare_policy_rollout(application["application_id"], actor="operator")
    started = await service.start_policy_rollout(rollout["rollout_id"], actor="operator")
    add_stage_records(service, rollout["rollout_id"])
    evaluated = await service.evaluate_policy_rollout(rollout["rollout_id"], actor="operator")
    advanced = await service.advance_policy_rollout(rollout["rollout_id"], actor="operator")

    assert rollout["status"] == "prepared"
    assert started["status"] == "canary"
    assert started["current_percentage"] == 10
    assert evaluated["health_status"] == "healthy"
    assert advanced["status"] == "expanding"
    assert advanced["current_percentage"] == 25


@pytest.mark.asyncio
async def test_degraded_blocks_advance(rollout_system) -> None:
    service, _, _, application = rollout_system
    rollout = await service.prepare_policy_rollout(application["application_id"], actor="operator")
    await service.start_policy_rollout(rollout["rollout_id"], actor="operator")
    add_stage_records(service, rollout["rollout_id"], treatment_repair_count=3)
    evaluated = await service.evaluate_policy_rollout(rollout["rollout_id"], actor="operator")

    assert evaluated["status"] == "degraded"
    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_rollout_conflict|planner_policy_rollout_not_healthy"):
        await service.advance_policy_rollout(rollout["rollout_id"], actor="operator")


@pytest.mark.asyncio
async def test_critical_auto_rolls_back_when_supported_and_ignores_infra(rollout_system) -> None:
    service, runtime_store, _, application = rollout_system
    rollout = await service.prepare_policy_rollout(application["application_id"], actor="operator")
    await service.start_policy_rollout(rollout["rollout_id"], actor="operator")
    add_stage_records(service, rollout["rollout_id"], treatment_failure_count=2, infra_count=3)
    evaluated = await service.evaluate_policy_rollout(rollout["rollout_id"], actor="operator")
    runtime = await runtime_store.get_policy("planning.quality_gate.weak_threshold", "global_planner")

    assert evaluated["status"] == "rolled_back"
    assert evaluated["health_status"] == "rolled_back"
    assert runtime["value"] == 60
    assert runtime["revision"] == 2


@pytest.mark.asyncio
async def test_snapshot_pinning_survives_rollout_advance_and_rollback(rollout_system) -> None:
    service, _, _, application = rollout_system
    rollout = await service.prepare_policy_rollout(application["application_id"], actor="operator")
    await service.start_policy_rollout(rollout["rollout_id"], actor="operator")
    workflow_id = treatment_workflow_id(rollout["rollout_id"], 10)
    pinned = await service.resolve_policy_for_workflow("planning.quality_gate.weak_threshold", workflow_id)
    add_stage_records(service, rollout["rollout_id"])
    await service.advance_policy_rollout(rollout["rollout_id"], actor="operator")
    still_pinned = await service.resolve_policy_for_workflow("planning.quality_gate.weak_threshold", workflow_id)
    await service.rollback_policy_rollout(rollout["rollout_id"], actor="operator")
    fresh = await service.resolve_policy_for_workflow("planning.quality_gate.weak_threshold", "new-after-rollback")

    assert pinned["effective_value"] == 55
    assert still_pinned["effective_value"] == 55
    assert fresh["effective_value"] == 60


@pytest.mark.asyncio
async def test_complete_promotes_base_and_removes_active_rollout(rollout_system) -> None:
    service, runtime_store, _, application = rollout_system
    rollout = await service.prepare_policy_rollout(application["application_id"], actor="operator")
    await service.start_policy_rollout(rollout["rollout_id"], actor="operator")
    for expected in (25, 50, 100):
        add_stage_records(service, rollout["rollout_id"])
        current = await service.advance_policy_rollout(rollout["rollout_id"], actor="operator")
        assert current["current_percentage"] == expected
    add_stage_records(service, rollout["rollout_id"])
    completed = await service.advance_policy_rollout(rollout["rollout_id"], actor="operator")

    runtime = await runtime_store.get_policy("planning.quality_gate.weak_threshold", "global_planner")
    active = await runtime_store.active_rollout("planning.quality_gate.weak_threshold", "global_planner")
    assert completed["status"] == "completed"
    assert runtime["value"] == 55
    assert runtime["revision"] == 2
    assert active is None


@pytest.mark.asyncio
async def test_pause_resume_conflict_stale_and_idempotent_start(rollout_system) -> None:
    service, runtime_store, _, application = rollout_system
    rollout = await service.prepare_policy_rollout(application["application_id"], actor="operator")
    start_one = await service.start_policy_rollout(rollout["rollout_id"], actor="operator")
    start_two = await service.start_policy_rollout(rollout["rollout_id"], actor="operator")
    paused = await service.pause_policy_rollout(rollout["rollout_id"], actor="operator")
    resumed = await service.resume_policy_rollout(rollout["rollout_id"], actor="operator")
    assert start_one["rollout_id"] == start_two["rollout_id"]
    assert paused["status"] == "paused"
    assert resumed["current_percentage"] == 10

    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_rollout_conflict"):
        await service.prepare_policy_rollout(application["application_id"], actor="operator")

    await runtime_store.set_policy("planning.quality_gate.weak_threshold", "global_planner", 50, expected_value=55, expected_revision=1)
    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_rollout_stale"):
        await service.advance_policy_rollout(rollout["rollout_id"], actor="operator")


def test_policy_rollout_api_flow(tmp_path) -> None:
    database = tmp_path / "api-rollout.sqlite"
    review_store = PlannerRecommendationReviewStore(database)
    proposal_store = PlannerPolicyProposalStore(database)
    runtime_store = PlannerPolicyRuntimeStore(database)
    review_store.initialize_sync()
    proposal_store.initialize_sync()
    runtime_store.initialize_sync()
    service = RolloutPlannerCalibrationService(review_store, proposal_store, runtime_store)

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
        rollout = client.post(f"/api/evaluations/planner/policy-applications/{applied['application_id']}/rollout/prepare", json={"actor": "operator"}).json()
        started = client.post(f"/api/evaluations/planner/policy-rollouts/{rollout['rollout_id']}/start", json={"actor": "operator"}).json()
        listed = client.get("/api/evaluations/planner/policy-rollouts").json()

    assert rollout["status"] == "prepared"
    assert started["current_percentage"] == 10
    assert listed["items"][0]["rollout_id"] == rollout["rollout_id"]
