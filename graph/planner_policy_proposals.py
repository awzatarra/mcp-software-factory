from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from graph.planner_calibration import PlannerPolicySnapshot


POLICY_PROPOSAL_VERSION = "7.10-v1"
SIMULATION_VERSION = "7.10-simulation-v1"


class PlannerPolicyProposalError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _as_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _validate_planner_policy_recommendation(recommendation: Mapping[str, Any]) -> None:
    target = recommendation.get("target")
    if isinstance(target, Mapping) and target.get("type") not in {None, "policy", "planner_policy"}:
        raise PlannerPolicyProposalError("planner_policy_proposal_invalid_source")
    policy = str(recommendation.get("policy") or "")
    if policy.startswith("agent:"):
        raise PlannerPolicyProposalError("planner_policy_proposal_invalid_source")


def _current_policy_value(policy_key: str, policy_snapshot: PlannerPolicySnapshot) -> Any:
    snapshot = policy_snapshot.model_dump()
    if policy_key == "planning.quality_gate.weak_threshold":
        return snapshot["quality_gate_weak_threshold"]
    if policy_key == "planning.confidence.adjustment":
        return snapshot["confidence_low_threshold"]
    if policy_key == "planning.risk.thresholds":
        return snapshot["risk_level_thresholds"]
    if policy_key == "planning.approval_policy":
        return snapshot["risk_approval_sensitive_areas"]
    if policy_key == "planning.refinement.strategy":
        return "current_guidance"
    if policy_key == "planning.quality.dimension_minimums":
        return snapshot["quality_gate_dimension_minimums"]
    raise PlannerPolicyProposalError("planner_policy_key_unresolved")


def _resolve_policy_key(recommendation: Mapping[str, Any]) -> str:
    policy = str(recommendation.get("policy") or "")
    if policy == "quality_gate_threshold":
        return "planning.quality_gate.weak_threshold"
    if policy == "quality_dimension_threshold":
        return "planning.quality.dimension_minimums"
    if policy == "planning_decision_confidence":
        return "planning.confidence.adjustment"
    if policy == "planning_risk_policy":
        return "planning.risk.thresholds"
    if policy == "approval_policy_review":
        return "planning.approval_policy"
    if policy == "quality_refinement_strategy":
        return "planning.refinement.strategy"
    if policy == "policy_experiment_promotion":
        evidence = recommendation.get("evidence") if isinstance(recommendation.get("evidence"), Mapping) else {}
        policy_key = str(evidence.get("policy_key") or "")
        if policy_key:
            return policy_key
    raise PlannerPolicyProposalError("planner_policy_key_unresolved")


def _proposed_value(
    recommendation: Mapping[str, Any],
    *,
    current_value: Any,
    policy_key: str,
) -> tuple[Any, str]:
    direction = str(recommendation.get("direction") or "review")
    suggested = recommendation.get("suggested_value")
    if direction == "review" or not isinstance(suggested, dict):
        return None, "review_only"
    if "suggested_value" in suggested:
        return suggested["suggested_value"], "replace"
    if "value" in suggested:
        return suggested["value"], "replace"
    if "suggested_adjustment" in suggested:
        adjustment = _as_number(suggested.get("suggested_adjustment"))
        base = _as_number(current_value)
        if adjustment is None or base is None:
            return None, "review_only"
        proposed = round(base + adjustment, 6)
        change_type = "increase" if proposed > base else "decrease" if proposed < base else "replace"
        if policy_key == "planning.quality_gate.weak_threshold":
            proposed = int(proposed) if proposed.is_integer() else proposed
        return proposed, change_type
    return None, "review_only"


def _validate_value(policy_key: str, proposed_value: Any, change_type: str) -> None:
    if change_type == "review_only":
        return
    number = _as_number(proposed_value)
    if policy_key in {"planning.quality_gate.weak_threshold", "planning.quality.dimension_minimums"}:
        if number is None or not 0 <= number <= 100:
            raise PlannerPolicyProposalError("planner_policy_proposal_invalid_value")
    elif policy_key == "planning.confidence.adjustment":
        if number is None or not 0 <= number <= 1:
            raise PlannerPolicyProposalError("planner_policy_proposal_invalid_value")
    elif policy_key in {"planning.risk.thresholds", "planning.approval_policy", "planning.refinement.strategy"}:
        raise PlannerPolicyProposalError("planner_policy_key_unresolved")


def _change_metadata(current_value: Any, proposed_value: Any, change_type: str) -> dict[str, Any]:
    if change_type == "review_only":
        return {"absolute_change": None, "relative_change_pct": None}
    current = _as_number(current_value)
    proposed = _as_number(proposed_value)
    if current is None or proposed is None:
        return {"absolute_change": None, "relative_change_pct": None}
    absolute = round(proposed - current, 6)
    relative = None if current == 0 else round((absolute / current) * 100, 2)
    return {"absolute_change": absolute, "relative_change_pct": relative}


def _safety_flags(policy_key: str, change_type: str, absolute_change: float | None) -> dict[str, bool]:
    decreases = (absolute_change or 0) < 0
    approval = policy_key == "planning.approval_policy"
    quality_gate = policy_key.startswith("planning.quality")
    return {
        "reduces_safety": bool((quality_gate and decreases) or approval),
        "increases_automation": bool((quality_gate and decreases) or approval),
        "reduces_approval_requirements": bool(approval and change_type in {"decrease", "replace", "review_only"}),
        "increases_failure_tolerance": bool(quality_gate and decreases),
    }


def _risk_level(policy_key: str, change_type: str, absolute_change: float | None, flags: Mapping[str, bool]) -> str:
    magnitude = abs(absolute_change or 0)
    if flags.get("reduces_approval_requirements"):
        return "critical"
    if policy_key == "planning.approval_policy":
        return "high"
    if flags.get("reduces_safety") and magnitude >= 10:
        return "high"
    if flags.get("reduces_safety"):
        return "medium"
    if change_type == "review_only":
        return "medium"
    return "low"


def _quality_score(record: Mapping[str, Any]) -> float | None:
    evaluation = record.get("planning_evaluation") if isinstance(record.get("planning_evaluation"), dict) else {}
    prediction = evaluation.get("quality_prediction") if isinstance(evaluation.get("quality_prediction"), dict) else {}
    return _as_number(prediction.get("observed") or evaluation.get("outcome_score"))


def simulate_policy_change(
    proposal: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if proposal.get("change_type") == "review_only":
        return None
    if proposal.get("policy_key") not in {"planning.quality_gate.weak_threshold", "planning.quality.dimension_minimums"}:
        return None
    current = _as_number(proposal.get("current_value"))
    proposed = _as_number(proposal.get("proposed_value"))
    if current is None or proposed is None:
        return None
    before: dict[str, int] = {"refinement_triggered": 0, "continue": 0}
    after: dict[str, int] = {"refinement_triggered": 0, "continue": 0}
    changed = 0
    sample = 0
    for record in records:
        score = _quality_score(record)
        if score is None:
            continue
        sample += 1
        before_decision = "refinement_triggered" if score < current else "continue"
        after_decision = "refinement_triggered" if score < proposed else "continue"
        before[before_decision] += 1
        after[after_decision] += 1
        changed += int(before_decision != after_decision)
    return {
        "version": SIMULATION_VERSION,
        "sample_size": sample,
        "affected_count": changed,
        "before_decisions": before,
        "after_decisions": after,
        "changed_decision_count": changed,
    }


def proposal_fingerprint(proposal: Mapping[str, Any]) -> str:
    payload = {
        "version": POLICY_PROPOSAL_VERSION,
        "source_recommendation_fingerprint": proposal.get("source_recommendation_fingerprint"),
        "policy_key": proposal.get("policy_key"),
        "policy_scope": proposal.get("policy_scope"),
        "current_value": proposal.get("current_value"),
        "proposed_value": proposal.get("proposed_value"),
        "change_type": proposal.get("change_type"),
        "reason_codes": proposal.get("reason_codes") or [],
        "simulation_version": SIMULATION_VERSION,
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def build_policy_change_proposal(
    recommendation: Mapping[str, Any],
    policy_snapshot: PlannerPolicySnapshot,
    *,
    records: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    _validate_planner_policy_recommendation(recommendation)
    policy_key = _resolve_policy_key(recommendation)
    current = _current_policy_value(policy_key, policy_snapshot)
    proposed, change_type = _proposed_value(recommendation, current_value=current, policy_key=policy_key)
    _validate_value(policy_key, proposed, change_type)
    metadata = _change_metadata(current, proposed, change_type)
    flags = _safety_flags(policy_key, change_type, metadata["absolute_change"])
    risk = _risk_level(policy_key, change_type, metadata["absolute_change"], flags)
    proposal = {
        "proposal_version": POLICY_PROPOSAL_VERSION,
        "source_recommendation_id": recommendation.get("recommendation_id"),
        "source_recommendation_fingerprint": recommendation.get("recommendation_fingerprint"),
        "source_policy": recommendation.get("policy"),
        "source_segment": recommendation.get("segment"),
        "source_direction": recommendation.get("direction"),
        "policy_key": policy_key,
        "policy_scope": recommendation.get("segment") or "global_planner",
        "segment": recommendation.get("segment"),
        "current_value": current,
        "proposed_value": proposed,
        "change_type": change_type,
        "rationale": f"Deterministic proposal from accepted recommendation {recommendation.get('recommendation_id')}.",
        "evidence_summary": {
            key: value
            for key, value in (recommendation.get("evidence") or {}).items()
            if key in {"sample_size", "observed_rate", "threshold", "average_error", "refinement_effectiveness_rate"}
        },
        "reason_codes": list(recommendation.get("reason_codes") or []),
        "proposal_risk_level": risk,
        "affected_policy_area": policy_key.split(".")[1] if "." in policy_key else policy_key,
        "affected_workflows_scope": recommendation.get("segment") or "global_planner",
        "safety_flags": flags,
        **metadata,
    }
    proposal["simulation"] = simulate_policy_change(proposal, records)
    proposal["proposal_fingerprint"] = proposal_fingerprint(proposal)
    return proposal


def validate_proposal_against_snapshot(
    proposal: Mapping[str, Any],
    policy_snapshot: PlannerPolicySnapshot,
) -> None:
    current = _current_policy_value(str(proposal.get("policy_key")), policy_snapshot)
    if current != proposal.get("current_value"):
        raise PlannerPolicyProposalError("planner_policy_proposal_stale")
    _validate_value(str(proposal.get("policy_key")), proposal.get("proposed_value"), str(proposal.get("change_type")))
    if proposal_fingerprint(proposal) != proposal.get("proposal_fingerprint"):
        raise PlannerPolicyProposalError("planner_policy_proposal_stale")
