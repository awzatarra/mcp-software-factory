from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from api.services.planner_calibration_service import PlannerCalibrationService
from api.services.planner_policy_proposal_store import PlannerPolicyProposalStore
from api.services.planner_policy_runtime_store import PlannerPolicyRuntimeStore
from api.services.planner_recommendation_review_store import PlannerRecommendationReviewStore
from graph.planner_calibration import PlannerPolicySnapshot
from graph.planner_policy_experiment import evaluate_experiment_result, evaluate_promotion_readiness
from graph.planner_policy_portfolio import evaluate_portfolio_conflicts
from graph.planner_policy_proposals import PlannerPolicyProposalError
from graph.planner_policy_registry import POLICY_METADATA, metadata_for, validate_policy_value


PHASE_7_ENV_KEYS = {
    "POLICY_EXPERIMENT_CONFIDENCE_LEVEL",
    "POLICY_EXPERIMENT_WINNER_CONFIDENCE_MIN",
    "POLICY_EXPERIMENT_EQUIVALENCE_MARGIN",
    "POLICY_EXPERIMENT_SAFETY_MIN_SAMPLE",
    "POLICY_EXPERIMENT_PROMOTION_READINESS_MIN",
    "POLICY_EXPERIMENT_STABILITY_MIN_SAMPLE",
}

FORBIDDEN_GIT_DIAGNOSTIC_TOOLS = {
    "diagnostic_ping",
    "diagnostic_service_ping",
    "diagnostic_project_root",
    "diagnostic_rev_parse",
    "diagnostic_to_thread",
    "diagnostic_subprocess",
    "diagnostic_git_environment",
}


def _quality_record(
    index: int,
    *,
    outcome_score: int = 100,
    false_positive: bool = False,
    refinement_effective: bool | None = None,
) -> dict[str, Any]:
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


def quality_records() -> list[dict[str, Any]]:
    return [
        _quality_record(index, false_positive=True, refinement_effective=False, outcome_score=57 if index < 4 else 100)
        if index < 5
        else _quality_record(index, refinement_effective=False if index < 12 else None, outcome_score=100)
        for index in range(20)
    ]


def _parse_env(path: Path) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    duplicates: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in values:
            duplicates.append(key)
        values[key] = value
    return values, duplicates


class Phase7PlannerCalibrationService(PlannerCalibrationService):
    def __init__(
        self,
        review_store: PlannerRecommendationReviewStore,
        proposal_store: PlannerPolicyProposalStore,
        runtime_store: PlannerPolicyRuntimeStore,
        records: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            query=SimpleNamespace(),
            metadata_store=None,
            review_store=review_store,
            proposal_store=proposal_store,
            policy_runtime_store=runtime_store,
        )
        self._records = records or quality_records()

    async def records(self, **kwargs):
        return list(self._records)


@pytest.fixture()
async def phase7_system(tmp_path):
    database = tmp_path / "phase7.sqlite"
    review_store = PlannerRecommendationReviewStore(database)
    proposal_store = PlannerPolicyProposalStore(database)
    runtime_store = PlannerPolicyRuntimeStore(database)
    await review_store.initialize()
    await proposal_store.initialize()
    await runtime_store.initialize()
    service = Phase7PlannerCalibrationService(review_store, proposal_store, runtime_store)
    return service, review_store, proposal_store, runtime_store


async def _approved_policy_proposal(service: Phase7PlannerCalibrationService) -> dict[str, Any]:
    analytics = await service.analyze(minimum_sample_size=20)
    recommendation = next(item for item in analytics["recommendations"] if item["policy"] == "quality_gate_threshold")
    accepted = await service.review(
        recommendation["recommendation_id"],
        action="accept",
        reviewer="phase7",
        fingerprint=recommendation["recommendation_fingerprint"],
    )
    proposal = await service.create_policy_proposal(accepted["recommendation_id"])
    await service.transition_policy_proposal(
        proposal["proposal_id"],
        action="ready",
        actor="phase7",
        fingerprint=proposal["proposal_fingerprint"],
    )
    return await service.transition_policy_proposal(
        proposal["proposal_id"],
        action="approve",
        actor="phase7",
        fingerprint=proposal["proposal_fingerprint"],
    )


def _experiment_metrics(sample_size: int = 5000) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    control = {
        "attributable_sample_size": sample_size,
        "repair_rate": 0.4,
        "failure_rate": 0,
        "risk_underestimated_rate": 0,
        "statistical_inputs": {
            "successful_with_repair_rate": {"successes": int(sample_size * 0.4), "sample_size": sample_size},
            "planning_failure_rate": {"successes": 0, "sample_size": sample_size},
            "risk_underestimated_rate": {"successes": 0, "sample_size": sample_size},
        },
    }
    winner = {
        "attributable_sample_size": sample_size,
        "repair_rate": 0.0,
        "failure_rate": 0,
        "risk_underestimated_rate": 0,
        "statistical_inputs": {
            "successful_with_repair_rate": {"successes": 0, "sample_size": sample_size},
            "planning_failure_rate": {"successes": 0, "sample_size": sample_size},
            "risk_underestimated_rate": {"successes": 0, "sample_size": sample_size},
        },
    }
    comparison = evaluate_experiment_result(
        control_metrics=control,
        variant_metrics={"A": winner},
        primary_metric="successful_with_repair_rate",
        minimum_sample_size=20,
    )
    return control, winner, comparison


def test_phase_7_example_supports_clean_clone_without_private_env(tmp_path: Path) -> None:
    copied_env = tmp_path / ".env"
    copied_env.write_bytes(Path(".env.example").read_bytes())
    env, env_duplicates = _parse_env(copied_env)
    example, example_duplicates = _parse_env(Path(".env.example"))

    assert env_duplicates == []
    assert example_duplicates == []
    assert set(env) == set(example)
    assert PHASE_7_ENV_KEYS <= set(example)
    for key in PHASE_7_ENV_KEYS:
        assert env[key] == example[key]


def test_policy_registry_is_the_mutation_allowlist_and_has_complete_metadata() -> None:
    assert POLICY_METADATA
    for policy_key, metadata in POLICY_METADATA.items():
        assert metadata.policy_key == policy_key
        assert metadata.value_type in {"score", "ratio"}
        assert metadata.risk_classification in {"low", "medium", "high", "critical"}
        assert metadata.verification_strategy
        assert metadata.rollback_supported is True
        validate_policy_value(policy_key, metadata.default_getter(PlannerPolicySnapshot()))

    with pytest.raises(Exception):
        metadata_for("mcp.transport.timeout")


def test_temporary_git_diagnostic_tools_are_not_exposed() -> None:
    import servers.git_server as git_server

    assert FORBIDDEN_GIT_DIAGNOSTIC_TOOLS.isdisjoint(set(dir(git_server)))


@pytest.mark.asyncio
async def test_phase_7_governed_planning_lifecycle_stops_before_policy_application(phase7_system) -> None:
    service, review_store, proposal_store, runtime_store = phase7_system
    before = await runtime_store.get_policy("planning.quality_gate.weak_threshold", "global_planner")

    proposal = await _approved_policy_proposal(service)
    after = await runtime_store.get_policy("planning.quality_gate.weak_threshold", "global_planner")
    history = await proposal_store.history(proposal["proposal_id"])
    review_history = await review_store.history(
        proposal["source_recommendation_id"],
        proposal["source_recommendation_fingerprint"],
    )

    assert proposal["status"] == "approved"
    assert proposal["application_status"] == "not_applied"
    assert after == before
    assert [item["to_status"] for item in review_history] == ["accepted"]
    assert [item["to_status"] for item in history] == ["draft", "ready_for_review", "approved"]
    assert (await service.policy_applications())["items"] == []


@pytest.mark.asyncio
async def test_application_requires_explicit_apply_and_rollback_is_manual(phase7_system) -> None:
    service, _, _, runtime_store = phase7_system
    proposal = await _approved_policy_proposal(service)

    prepared = await service.prepare_policy_application(proposal["proposal_id"], actor="phase7")
    still_base = await runtime_store.get_policy("planning.quality_gate.weak_threshold", "global_planner")
    applied = await service.apply_policy_application(
        prepared["application_id"],
        actor="phase7",
        fingerprint=prepared["application_fingerprint"],
    )
    rolled_back = await service.rollback_policy_application(
        prepared["application_id"],
        actor="phase7",
        fingerprint=prepared["application_fingerprint"],
    )
    restored = await runtime_store.get_policy("planning.quality_gate.weak_threshold", "global_planner")

    assert prepared["status"] == "prepared"
    assert still_base["value"] == 60 and still_base["revision"] == 0
    assert applied["status"] == "applied"
    assert rolled_back["status"] == "rolled_back"
    assert restored["value"] == 60
    assert restored["revision"] == 2


def test_experiment_winner_and_promotion_readiness_are_recommendation_only() -> None:
    control, winner, comparison = _experiment_metrics()
    experiment = {
        "result": comparison["result"],
        "winner_variant_id": comparison["winner_variant_id"],
        "minimum_sample_size": 20,
        "primary_metric": "successful_with_repair_rate",
        "secondary_metrics": ["planning_failure_rate"],
        "variants": [{"variant_id": "A", "value": 55, "risk_level": "medium"}],
        "metrics": {"comparison": comparison},
    }

    readiness = evaluate_promotion_readiness(
        experiment=experiment,
        control_metrics=control,
        variant_metrics={"A": winner},
        groups=None,
        policy_baseline_current=True,
        stability_sample=1,
    )

    assert comparison["result"] == "variant_preferred"
    assert readiness["status"] == "ready"
    assert readiness["recommended_action"] == "recommend_promotion"


@pytest.mark.asyncio
async def test_rollout_experiment_portfolio_blocks_competing_policy_scope(phase7_system) -> None:
    service, _, _, runtime_store = phase7_system
    proposal = await _approved_policy_proposal(service)
    application = await service.prepare_policy_application(proposal["proposal_id"], actor="phase7")
    applied = await service.apply_policy_application(
        application["application_id"],
        actor="phase7",
        fingerprint=application["application_fingerprint"],
    )
    rollout = await service.prepare_policy_rollout(applied["application_id"], actor="phase7")
    await service.start_policy_rollout(rollout["rollout_id"], actor="phase7")

    with pytest.raises(PlannerPolicyProposalError, match="planner_policy_experiment_rollout_conflict"):
        await service.create_policy_experiment(
            policy_key="planning.quality_gate.weak_threshold",
            scope="global_planner",
            variants=[{"variant_id": "A", "name": "Variant A", "value": 50, "risk_level": "medium"}],
            allocation={"control": 50, "A": 50},
            primary_metric="successful_with_repair_rate",
            actor="phase7",
        )

    portfolio = evaluate_portfolio_conflicts(
        experiments=[
            {
                "experiment_id": "candidate",
                "policy_key": "planning.quality_gate.weak_threshold",
                "scope": "global_planner",
                "status": "running",
                "primary_metric": "successful_with_repair_rate",
            }
        ],
        rollouts=await runtime_store.list_rollouts(status=None, policy_key=None, limit=100),
    )
    assert portfolio["blocking_conflict_count"] == 1
    assert portfolio["conflicts"][0]["type"] == "rollout_experiment_conflict"
