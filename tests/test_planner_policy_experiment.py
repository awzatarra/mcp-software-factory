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
from graph.planner_policy_experiment import (
    assign_experiment_variant,
    evaluate_experiment_result,
    evaluate_promotion_readiness,
    experiment_bucket,
    estimate_required_sample_size,
    minimum_detectable_effect,
    validate_allocation,
    wilson_interval,
)
from graph.planner_policy_portfolio import evaluate_portfolio_conflicts
from graph.planner_policy_proposals import PlannerPolicyProposalError
from tests.test_planner_policy_proposals import quality_records


class ExperimentPlannerCalibrationService(PlannerCalibrationService):
    def __init__(self, review_store, proposal_store, runtime_store) -> None:
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
async def experiment_system(tmp_path):
    database = tmp_path / "experiment.sqlite"
    review_store = PlannerRecommendationReviewStore(database)
    proposal_store = PlannerPolicyProposalStore(database)
    runtime_store = PlannerPolicyRuntimeStore(database)
    await review_store.initialize()
    await proposal_store.initialize()
    await runtime_store.initialize()
    service = ExperimentPlannerCalibrationService(review_store, proposal_store, runtime_store)
    return service, runtime_store


def experiment_payload() -> dict:
    return {
        "policy_key": "planning.quality_gate.weak_threshold",
        "scope": "global_planner",
        "variants": [
            {"variant_id": "A", "name": "Lower weak threshold", "value": 55, "risk_level": "medium"},
            {"variant_id": "B", "name": "Much lower weak threshold", "value": 50, "risk_level": "medium"},
        ],
        "allocation": {"control": 50, "A": 25, "B": 25},
        "primary_metric": "successful_with_repair_rate",
        "secondary_metrics": ["planning_failure_rate"],
        "minimum_sample_size": 10,
    }


def experiment_payload_with(**updates) -> dict:
    payload = experiment_payload()
    payload.update(updates)
    return payload


def exp_record(index: int, experiment_id: str, variant_id: str, *, repair: bool = False, failed: bool = False, infra: bool = False) -> dict:
    return {
        "workflow_id": f"experiment-workflow-{index}",
        "failure_type": "mcp_timeout" if infra else None,
        "planner_policy_experiment_assignments": [
            {
                "experiment_id": experiment_id,
                "variant_id": variant_id,
                "effective_value": 60 if variant_id == "control" else 55,
                "policy_revision": 0,
            }
        ],
        "planning_evaluation": {
            "version": "7.7-v1",
            "outcome": "failed" if failed else "successful",
            "outcome_score": 0 if failed else 100,
            "successful_with_repair": repair,
            "confidence_absolute_error": 0.01,
            "risk_calibration": {"label": "aligned"},
            "quality_gate": {"false_positive": False, "false_negative": False},
            "refinement": {"effective": True},
        },
    }


def set_experiment_records(service: ExperimentPlannerCalibrationService, experiment_id: str, *, mode: str) -> None:
    records = quality_records()
    sample_count = 5000 if mode == "variant_wins" else 10
    repair_cutoff = 4
    for index in range(sample_count):
        control_repair = index % 5 < 2 if mode == "variant_wins" else index < repair_cutoff
        records.append(exp_record(index, experiment_id, "control", repair=control_repair))
        if mode == "variant_wins":
            records.append(exp_record(index + 10000, experiment_id, "A", repair=False))
            records.append(exp_record(index + 20000, experiment_id, "B", repair=control_repair))
        elif mode == "control_wins":
            records.append(exp_record(index + 20, experiment_id, "A", repair=index < 5))
            records.append(exp_record(index + 40, experiment_id, "B", repair=index < 5))
        elif mode == "equivalent":
            records.append(exp_record(index + 20, experiment_id, "A", repair=False))
            records.append(exp_record(index + 40, experiment_id, "B", repair=False))
        elif mode == "unsafe":
            records.append(exp_record(index + 20, experiment_id, "A", repair=False, failed=index < 2))
            records.append(exp_record(index + 40, experiment_id, "B", repair=index < 4))
        elif mode == "infra":
            records.append(exp_record(index + 20, experiment_id, "A", repair=False, infra=index < 3))
            records.append(exp_record(index + 40, experiment_id, "B", repair=index < 4))
        elif mode == "insufficient":
            if index < 5:
                records.append(exp_record(index + 20, experiment_id, "A", repair=False))
            records.append(exp_record(index + 40, experiment_id, "B", repair=index < 4))
    if mode == "infra":
        for index in range(3):
            records.append(exp_record(index + 100, experiment_id, "A", repair=False))
    service._records = records


def test_experiment_assignment_and_allocation() -> None:
    allocation = {"control": 50, "A": 25, "B": 25}
    variants = [{"variant_id": "A"}, {"variant_id": "B"}]
    validate_allocation(allocation, variants)
    with pytest.raises(ValueError, match="planner_policy_experiment_invalid_allocation"):
        validate_allocation({"control": 50, "A": 30, "B": 30}, variants)
    workflow_id = "workflow-stable"
    first = assign_experiment_variant(experiment_id="exp", workflow_id=workflow_id, policy_key="planning.quality_gate.weak_threshold", allocation=allocation)
    second = assign_experiment_variant(experiment_id="exp", workflow_id=workflow_id, policy_key="planning.quality_gate.weak_threshold", allocation=allocation)
    assert first == second
    sample = [assign_experiment_variant(experiment_id="exp", workflow_id=f"wf-{index}", policy_key="planning.quality_gate.weak_threshold", allocation=allocation) for index in range(1000)]
    assert 430 <= sample.count("control") <= 570
    assert 190 <= sample.count("A") <= 310
    assert 190 <= sample.count("B") <= 310
    assert 0 <= experiment_bucket(experiment_id="exp", workflow_id=workflow_id, policy_key="planning.quality_gate.weak_threshold") < 10000


def test_portfolio_conflicts_are_deterministic_and_classified() -> None:
    running_quality = {"experiment_id": "quality", "policy_key": "planning.quality_gate.weak_threshold", "scope": "global", "status": "running", "primary_metric": "successful_with_repair_rate"}
    same_policy = {"experiment_id": "quality-2", "policy_key": "planning.quality_gate.weak_threshold", "scope": "global", "status": "ready", "primary_metric": "successful_with_repair_rate"}
    isolated_scope = {"experiment_id": "quality-other", "policy_key": "planning.quality_gate.weak_threshold", "scope": "framework:fastapi", "status": "ready", "primary_metric": "successful_with_repair_rate"}
    independent = {"experiment_id": "git", "policy_key": "git.workflow.promotion", "scope": "global", "status": "ready", "primary_metric": "planning_failure_rate"}
    related = {"experiment_id": "refinement", "policy_key": "planning.refinement.strategy", "scope": "global", "status": "ready", "primary_metric": "refinement_effectiveness_rate"}
    conflicting = {"experiment_id": "approval", "policy_key": "planning.approval_policy", "scope": "global", "status": "ready", "primary_metric": "risk_underestimated_rate"}
    risk = {"experiment_id": "risk", "policy_key": "planning.risk.thresholds", "scope": "global", "status": "running", "primary_metric": "risk_underestimated_rate"}
    paused = {"experiment_id": "paused", "policy_key": "planning.quality_gate.weak_threshold", "scope": "global", "status": "paused", "primary_metric": "successful_with_repair_rate"}

    same = evaluate_portfolio_conflicts(experiments=[running_quality, same_policy], rollouts=[])
    isolated = evaluate_portfolio_conflicts(experiments=[running_quality, isolated_scope], rollouts=[])
    independent_result = evaluate_portfolio_conflicts(experiments=[running_quality, independent], rollouts=[])
    related_result = evaluate_portfolio_conflicts(experiments=[running_quality, related], rollouts=[])
    blocked = evaluate_portfolio_conflicts(experiments=[risk, conflicting], rollouts=[])
    paused_result = evaluate_portfolio_conflicts(experiments=[paused, same_policy], rollouts=[])
    rollout = evaluate_portfolio_conflicts(experiments=[running_quality], rollouts=[{"rollout_id": "rollout", "policy_key": "planning.quality_gate.weak_threshold", "scope": "global", "status": "canary"}])

    assert same == evaluate_portfolio_conflicts(experiments=[running_quality, same_policy], rollouts=[])
    assert same["conflicts"][0]["type"] == "same_policy_conflict"
    assert isolated["blocking_conflict_count"] == 0
    assert independent_result["isolation_status"] == "isolated"
    assert related_result["warning_count"] >= 1
    assert any(item["type"] == "metric_interference" for item in related_result["warnings"])
    assert blocked["blocking_conflict_count"] == 1
    assert blocked["conflicts"][0]["type"] == "policy_dependency_conflict"
    assert paused_result["blocking_conflict_count"] == 0
    assert rollout["conflicts"][0]["type"] == "rollout_experiment_conflict"


def test_experiment_result_classifications() -> None:
    control = {"attributable_sample_size": 3000, "repair_rate": 0.4, "failure_rate": 0}
    winner = {"attributable_sample_size": 3000, "repair_rate": 0.1, "failure_rate": 0}
    tie = {"attributable_sample_size": 3000, "repair_rate": 0.11, "failure_rate": 0}
    unsafe = {"attributable_sample_size": 3000, "repair_rate": 0.0, "failure_rate": 0.2}
    assert evaluate_experiment_result(control_metrics=control, variant_metrics={"A": winner}, primary_metric="successful_with_repair_rate", minimum_sample_size=10)["result"] == "variant_preferred"
    assert evaluate_experiment_result(control_metrics=control, variant_metrics={"A": {"attributable_sample_size": 20, "repair_rate": 0.39, "failure_rate": 0}}, primary_metric="successful_with_repair_rate", minimum_sample_size=10)["result"] == "inconclusive"
    assert evaluate_experiment_result(control_metrics=control, variant_metrics={"A": winner, "B": tie}, primary_metric="successful_with_repair_rate", minimum_sample_size=10)["result"] == "multiple_variants_equivalent"
    assert evaluate_experiment_result(control_metrics=control, variant_metrics={"A": unsafe}, primary_metric="successful_with_repair_rate", minimum_sample_size=10)["result"] == "unsafe_variant"
    assert evaluate_experiment_result(control_metrics=control, variant_metrics={"A": {"attributable_sample_size": 5, "repair_rate": 0}}, primary_metric="successful_with_repair_rate", minimum_sample_size=10)["result"] == "insufficient_data"


def test_statistical_confidence_for_rates_noise_and_practical_failure() -> None:
    significant = evaluate_experiment_result(
        control_metrics={"attributable_sample_size": 3000, "failure_rate": .20, "statistical_inputs": {"planning_failure_rate": {"successes": 600, "sample_size": 3000}}},
        variant_metrics={"A": {"attributable_sample_size": 3000, "failure_rate": .12, "statistical_inputs": {"planning_failure_rate": {"successes": 360, "sample_size": 3000}}}},
        primary_metric="planning_failure_rate",
        minimum_sample_size=20,
    )
    noisy = evaluate_experiment_result(
        control_metrics={"attributable_sample_size": 20, "failure_rate": .20, "statistical_inputs": {"planning_failure_rate": {"successes": 4, "sample_size": 20}}},
        variant_metrics={"A": {"attributable_sample_size": 20, "failure_rate": .15, "statistical_inputs": {"planning_failure_rate": {"successes": 3, "sample_size": 20}}}},
        primary_metric="planning_failure_rate",
        minimum_sample_size=20,
    )
    trivial = evaluate_experiment_result(
        control_metrics={"attributable_sample_size": 5000, "failure_rate": .20, "statistical_inputs": {"planning_failure_rate": {"successes": 1000, "sample_size": 5000}}},
        variant_metrics={"A": {"attributable_sample_size": 5000, "failure_rate": .19, "statistical_inputs": {"planning_failure_rate": {"successes": 950, "sample_size": 5000}}}},
        primary_metric="planning_failure_rate",
        minimum_sample_size=20,
    )

    assert significant["result"] == "variant_preferred"
    assert significant["statistical_summary"]["comparisons"]["A"]["statistically_significant"] is True
    assert significant["statistical_summary"]["comparisons"]["A"]["practically_significant"] is True
    assert noisy["result"] == "inconclusive"
    assert noisy["statistical_summary"]["decision_confidence_label"] in {"low", "medium"}
    assert trivial["result"] == "inconclusive"
    assert trivial["statistical_summary"]["comparisons"]["A"]["practically_significant"] is False


def test_statistical_helpers_are_deterministic_and_sample_sensitive() -> None:
    interval = wilson_interval(12, 100, .95)
    required = estimate_required_sample_size(metric="planning_failure_rate", baseline_value=.2, effect_size=.03, confidence_level=.95)
    low_sample_mde = minimum_detectable_effect(
        metric="planning_failure_rate",
        control_metrics={"attributable_sample_size": 20, "failure_rate": .2, "statistical_inputs": {"planning_failure_rate": {"successes": 4, "sample_size": 20}}},
        candidate_metrics={"attributable_sample_size": 20, "failure_rate": .1, "statistical_inputs": {"planning_failure_rate": {"successes": 2, "sample_size": 20}}},
    )
    high_sample_mde = minimum_detectable_effect(
        metric="planning_failure_rate",
        control_metrics={"attributable_sample_size": 200, "failure_rate": .2, "statistical_inputs": {"planning_failure_rate": {"successes": 40, "sample_size": 200}}},
        candidate_metrics={"attributable_sample_size": 200, "failure_rate": .1, "statistical_inputs": {"planning_failure_rate": {"successes": 20, "sample_size": 200}}},
    )

    assert interval == wilson_interval(12, 100, .95)
    assert required is not None and required > 0
    assert low_sample_mde is not None and high_sample_mde is not None
    assert high_sample_mde < low_sample_mde


def test_mean_metric_confidence_and_guardrail_block_winner() -> None:
    mean_winner = evaluate_experiment_result(
        control_metrics={
            "attributable_sample_size": 200,
            "average_confidence_absolute_error": .10,
            "failure_rate": 0,
            "statistical_inputs": {"average_confidence_absolute_error": {"sample_size": 200, "mean": .10, "stddev": .02}},
        },
        variant_metrics={
            "A": {
                "attributable_sample_size": 200,
                "average_confidence_absolute_error": .05,
                "failure_rate": 0,
                "statistical_inputs": {"average_confidence_absolute_error": {"sample_size": 200, "mean": .05, "stddev": .02}},
            }
        },
        primary_metric="average_confidence_absolute_error",
        minimum_sample_size=20,
    )
    unsafe = evaluate_experiment_result(
        control_metrics={"attributable_sample_size": 3000, "repair_rate": .4, "failure_rate": 0},
        variant_metrics={"A": {"attributable_sample_size": 3000, "repair_rate": .1, "failure_rate": .2}},
        primary_metric="successful_with_repair_rate",
        guardrails={"planning_failure_rate": {"max_delta": .05}},
        minimum_sample_size=20,
    )

    assert mean_winner["result"] == "variant_preferred"
    assert mean_winner["statistical_summary"]["comparisons"]["A"]["metric_type"] == "mean"
    assert unsafe["result"] == "unsafe_variant"
    assert unsafe["winner_variant_id"] is None


def readiness_case(
    *,
    control_metrics: dict,
    variant_metrics: dict[str, dict],
    primary_metric: str = "successful_with_repair_rate",
    secondary_metrics: list[str] | None = None,
    variants: list[dict] | None = None,
    result_override: str | None = None,
    winner_override: str | None = None,
    groups: dict[str, list[dict]] | None = None,
) -> dict:
    comparison = evaluate_experiment_result(
        control_metrics=control_metrics,
        variant_metrics=variant_metrics,
        primary_metric=primary_metric,
        minimum_sample_size=20,
    )
    if result_override is not None:
        comparison["result"] = result_override
    if winner_override is not None:
        comparison["winner_variant_id"] = winner_override
    experiment = {
        "experiment_id": "experiment-readiness",
        "experiment_fingerprint": "fingerprint",
        "status": "running",
        "policy_key": "planning.quality_gate.weak_threshold",
        "scope": "global_planner",
        "control_value": 60,
        "winner_variant_id": comparison.get("winner_variant_id"),
        "result": comparison["result"],
        "primary_metric": primary_metric,
        "secondary_metrics": secondary_metrics or ["planning_failure_rate"],
        "minimum_sample_size": 20,
        "variants": variants or [{"variant_id": "A", "value": 55, "risk_level": "medium"}],
        "metrics": {"control": control_metrics, "variants": variant_metrics, "comparison": comparison},
        "statistical_summary": comparison.get("statistical_summary"),
    }
    return evaluate_promotion_readiness(
        experiment=experiment,
        control_metrics=control_metrics,
        variant_metrics=variant_metrics,
        groups=groups,
    )


def test_promotion_readiness_ready_needs_more_data_guardrail_and_inconclusive() -> None:
    ready = readiness_case(
        control_metrics={"attributable_sample_size": 5000, "repair_rate": .4, "failure_rate": 0},
        variant_metrics={"A": {"attributable_sample_size": 5000, "repair_rate": 0, "failure_rate": 0}},
    )
    needs_more = readiness_case(
        control_metrics={"attributable_sample_size": 200, "repair_rate": .4, "failure_rate": 0},
        variant_metrics={"A": {"attributable_sample_size": 200, "repair_rate": 0, "failure_rate": 0}},
    )
    blocked = readiness_case(
        control_metrics={"attributable_sample_size": 5000, "repair_rate": .4, "failure_rate": 0},
        variant_metrics={"A": {"attributable_sample_size": 5000, "repair_rate": 0, "failure_rate": .2}},
    )
    control = readiness_case(
        control_metrics={"attributable_sample_size": 5000, "repair_rate": .1, "failure_rate": 0},
        variant_metrics={"A": {"attributable_sample_size": 5000, "repair_rate": .4, "failure_rate": 0}},
    )
    equivalent = readiness_case(
        control_metrics={"attributable_sample_size": 5000, "repair_rate": .4, "failure_rate": 0},
        variant_metrics={"A": {"attributable_sample_size": 5000, "repair_rate": 0, "failure_rate": 0}, "B": {"attributable_sample_size": 5000, "repair_rate": .01, "failure_rate": 0}},
    )

    assert ready["status"] == "ready"
    assert ready["score"] >= 80
    assert ready["recommended_action"] == "recommend_promotion"
    assert needs_more["status"] == "needs_more_data"
    assert "promotion_sample_insufficient" in needs_more["reason_codes"]
    assert blocked["status"] == "blocked_by_guardrail"
    assert "promotion_guardrail_violation" in blocked["reason_codes"]
    assert control["status"] == "no_winner"
    assert equivalent["status"] == "inconclusive"
    assert equivalent["recommended_action"] == "review_equivalent_variants"


def test_promotion_readiness_temporal_secondary_risk_and_determinism() -> None:
    control_metrics = {"attributable_sample_size": 5000, "repair_rate": .4, "failure_rate": 0}
    variant_metrics = {"A": {"attributable_sample_size": 5000, "repair_rate": 0, "failure_rate": 0}}
    unstable_groups = {
        "control": [exp_record(i, "exp", "control", repair=(i < 2000)) for i in range(5000)],
        "A": [exp_record(i + 10000, "exp", "A", repair=False) for i in range(2500)]
        + [exp_record(i + 20000, "exp", "A", repair=True) for i in range(2500)],
    }
    unstable = readiness_case(control_metrics=control_metrics, variant_metrics=variant_metrics, groups=unstable_groups)
    contradictory = readiness_case(
        control_metrics={"attributable_sample_size": 5000, "repair_rate": .4, "failure_rate": 0, "average_confidence_absolute_error": .1},
        variant_metrics={"A": {"attributable_sample_size": 5000, "repair_rate": 0, "failure_rate": 0, "average_confidence_absolute_error": .2}},
        secondary_metrics=["average_confidence_absolute_error"],
    )
    high_risk = readiness_case(
        control_metrics=control_metrics,
        variant_metrics=variant_metrics,
        variants=[{"variant_id": "A", "value": 55, "risk_level": "high"}],
    )
    first = readiness_case(control_metrics=control_metrics, variant_metrics=variant_metrics)
    second = readiness_case(control_metrics=control_metrics, variant_metrics=variant_metrics)

    assert unstable["status"] == "unstable"
    assert "promotion_temporal_instability" in unstable["reason_codes"]
    assert contradictory["status"] in {"unstable", "blocked_by_guardrail"}
    assert "promotion_secondary_metrics_contradictory" in contradictory["reason_codes"]
    assert high_risk["status"] == "ready"
    assert high_risk["score"] < first["score"]
    assert "promotion_policy_risk_high" in high_risk["reason_codes"]
    assert first == second


@pytest.mark.asyncio
async def test_create_ready_start_evaluate_complete_and_recommendation(experiment_system) -> None:
    service, _ = experiment_system
    experiment = await service.create_policy_experiment(**experiment_payload(), actor="operator")
    ready = await service.ready_policy_experiment(experiment["experiment_id"], actor="operator")
    started = await service.start_policy_experiment(experiment["experiment_id"], actor="operator")
    set_experiment_records(service, experiment["experiment_id"], mode="variant_wins")
    evaluated = await service.evaluate_policy_experiment(experiment["experiment_id"], actor="operator")
    completed = await service.complete_policy_experiment(experiment["experiment_id"], actor="operator")
    recommendations = await service.recommendations(limit=200)

    assert experiment["status"] == "draft"
    assert ready["status"] == "ready"
    assert started["status"] == "running"
    assert evaluated["result"] == "variant_preferred"
    assert evaluated["winner_variant_id"] == "A"
    assert evaluated["statistical_summary"]["analysis_version"] == "7.14-v1"
    assert evaluated["statistical_summary"]["decision_confidence"] >= .75
    assert evaluated["promotion_readiness"]["status"] == "ready"
    assert evaluated["promotion_readiness"]["score"] >= 80
    assert completed["status"] == "completed"
    recommendation = next(item for item in recommendations["items"] if item["policy"] == "policy_experiment_promotion")
    assert recommendation["status"] == "recommendation_only"
    reviewed = await service.review(
        recommendation["recommendation_id"],
        action="accept",
        reviewer="operator",
        fingerprint=recommendation["recommendation_fingerprint"],
        reason="Promotion evidence is ready.",
    )
    proposal = await service.create_policy_proposal(recommendation["recommendation_id"])
    assert reviewed["review"]["status"] == "accepted"
    assert proposal["policy_key"] == "planning.quality_gate.weak_threshold"
    assert proposal["proposed_value"] == 55


@pytest.mark.asyncio
async def test_snapshot_pinning_pause_cancel_and_base_resolution(experiment_system) -> None:
    service, _ = experiment_system
    experiment = await service.create_policy_experiment(**experiment_payload(), actor="operator")
    await service.ready_policy_experiment(experiment["experiment_id"], actor="operator")
    await service.start_policy_experiment(experiment["experiment_id"], actor="operator")
    workflow_id = next(
        f"candidate-{index}"
        for index in range(1000)
        if assign_experiment_variant(experiment_id=experiment["experiment_id"], workflow_id=f"candidate-{index}", policy_key="planning.quality_gate.weak_threshold", allocation=experiment_payload()["allocation"]) == "A"
    )
    pinned = await service.resolve_policy_for_workflow("planning.quality_gate.weak_threshold", workflow_id)
    await service.pause_policy_experiment(experiment["experiment_id"], actor="operator")
    still_pinned = await service.resolve_policy_for_workflow("planning.quality_gate.weak_threshold", workflow_id)
    await service.cancel_policy_experiment(experiment["experiment_id"], actor="operator")
    fresh = await service.resolve_policy_for_workflow("planning.quality_gate.weak_threshold", "after-cancel")

    assert pinned["variant_id"] == "A"
    assert pinned["effective_value"] == 55
    assert still_pinned["effective_value"] == 55
    assert fresh["effective_value"] == 60


@pytest.mark.asyncio
async def test_portfolio_blocks_same_policy_start_and_endpoint_lists_conflict(experiment_system) -> None:
    service, _ = experiment_system
    first = await service.create_policy_experiment(**experiment_payload(), actor="operator")
    second = await service.create_policy_experiment(
        **experiment_payload_with(
            variants=[
                {"variant_id": "A", "name": "Lower weak threshold", "value": 50, "risk_level": "medium"},
                {"variant_id": "B", "name": "Much lower weak threshold", "value": 45, "risk_level": "medium"},
            ],
            allocation={"control": 50, "A": 25, "B": 25},
        ),
        actor="operator",
    )
    await service.ready_policy_experiment(first["experiment_id"], actor="operator")
    await service.start_policy_experiment(first["experiment_id"], actor="operator")
    await service.ready_policy_experiment(second["experiment_id"], actor="operator")

    with pytest.raises(PlannerPolicyProposalError, match="planner_experiment_portfolio_conflict"):
        await service.start_policy_experiment(second["experiment_id"], actor="operator")

    portfolio = await service.policy_experiment_portfolio()
    assert portfolio["blocking_conflict_count"] == 1
    assert portfolio["conflicts"][0]["type"] == "same_policy_conflict"
    await service.pause_policy_experiment(first["experiment_id"], actor="operator")
    started = await service.start_policy_experiment(second["experiment_id"], actor="operator")
    assert started["status"] == "running"


@pytest.mark.asyncio
async def test_experiment_conflicts_with_rollout_and_stale_base(experiment_system) -> None:
    service, runtime_store = experiment_system
    experiment = await service.create_policy_experiment(**experiment_payload(), actor="operator")
    await runtime_store.set_policy("planning.quality_gate.weak_threshold", "global_planner", 58, expected_value=60, expected_revision=0)
    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_experiment_stale"):
        await service.ready_policy_experiment(experiment["experiment_id"], actor="operator")

    service2, runtime_store2 = experiment_system
    rollout_like = await runtime_store2.create_rollout(
        {
            "application_id": "application",
            "proposal_id": "proposal",
            "policy_key": "planning.quality_gate.weak_threshold",
            "scope": "global_planner",
            "baseline_revision": 0,
            "target_revision": 1,
            "previous_value": 60,
            "target_value": 55,
            "stages": [10, 25, 50, 100],
            "baseline_metrics": {"policy_sample_size": 20},
            "rollout_fingerprint": "fingerprint",
            "application_fingerprint": "app-fingerprint",
        }
    )
    assert rollout_like[0]["status"] == "prepared"
    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_experiment_rollout_conflict"):
        await service2.create_policy_experiment(**experiment_payload(), actor="operator")


@pytest.mark.asyncio
async def test_evaluate_is_idempotent_and_infra_excluded(experiment_system) -> None:
    service, _ = experiment_system
    experiment = await service.create_policy_experiment(**experiment_payload(), actor="operator")
    await service.ready_policy_experiment(experiment["experiment_id"], actor="operator")
    await service.start_policy_experiment(experiment["experiment_id"], actor="operator")
    set_experiment_records(service, experiment["experiment_id"], mode="infra")
    first = await service.evaluate_policy_experiment(experiment["experiment_id"], actor="operator")
    second = await service.evaluate_policy_experiment(experiment["experiment_id"], actor="operator")
    metrics = second["metrics"]["variants"]["A"]
    assert first["metrics"] == second["metrics"]
    assert metrics["excluded_infrastructure_count"] == 3
    assert second["result"] == "variant_preferred"
    assert second["statistical_summary"] == second["metrics"]["comparison"]["statistical_summary"]


def test_policy_experiment_api_flow(tmp_path) -> None:
    database = tmp_path / "api-experiment.sqlite"
    review_store = PlannerRecommendationReviewStore(database)
    proposal_store = PlannerPolicyProposalStore(database)
    runtime_store = PlannerPolicyRuntimeStore(database)
    review_store.initialize_sync()
    proposal_store.initialize_sync()
    runtime_store.initialize_sync()
    service = ExperimentPlannerCalibrationService(review_store, proposal_store, runtime_store)

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(planner_calibration=service)

    with TestClient(create_app(factory)) as client:
        created = client.post("/api/evaluations/planner/policy-experiments", json={**experiment_payload(), "actor": "operator"}).json()
        ready = client.post(f"/api/evaluations/planner/policy-experiments/{created['experiment_id']}/ready", json={"actor": "operator"}).json()
        started = client.post(f"/api/evaluations/planner/policy-experiments/{created['experiment_id']}/start", json={"actor": "operator"}).json()
        listed = client.get("/api/evaluations/planner/policy-experiments").json()
        portfolio = client.get("/api/evaluations/planner/policy-experiments/portfolio").json()

    assert created["status"] == "draft"
    assert ready["status"] == "ready"
    assert started["status"] == "running"
    assert listed["items"][0]["experiment_id"] == created["experiment_id"]
    assert portfolio["active_experiments"][0]["id"] == created["experiment_id"]
    assert portfolio["isolation_status"] in {"isolated", "warnings"}
