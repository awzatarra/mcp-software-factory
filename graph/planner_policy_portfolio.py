from __future__ import annotations

from typing import Any, Iterable, Mapping


POLICY_EXPERIMENT_PORTFOLIO_VERSION = "7.16-v1"

ACTIVE_PORTFOLIO_EXPERIMENT_STATUSES = {"ready", "running", "observing"}
VISIBLE_PORTFOLIO_EXPERIMENT_STATUSES = {"ready", "running", "observing", "paused"}
ACTIVE_PORTFOLIO_ROLLOUT_STATUSES = {"prepared", "canary", "expanding", "observing", "degraded"}
VISIBLE_PORTFOLIO_ROLLOUT_STATUSES = {"prepared", "canary", "expanding", "observing", "degraded", "paused"}

POLICY_AREAS: dict[str, str] = {
    "planning.quality_gate.weak_threshold": "quality_gate",
    "planning.quality.dimension_minimums": "quality_gate",
    "planning.refinement.strategy": "refinement",
    "planning.confidence.adjustment": "confidence",
    "planning.risk.thresholds": "risk",
    "planning.approval_policy": "approval",
    "git.workflow.promotion": "git",
}

POLICY_RELATIONSHIPS: dict[tuple[str, str], str] = {
    ("quality_gate", "refinement"): "related",
    ("quality_gate", "confidence"): "related",
    ("quality_gate", "risk"): "related",
    ("risk", "approval"): "conflicting",
    ("confidence", "risk"): "related",
    ("git", "quality_gate"): "independent",
    ("git", "refinement"): "independent",
    ("git", "confidence"): "independent",
    ("git", "risk"): "independent",
    ("git", "approval"): "independent",
}

POLICY_METRIC_INFLUENCE: dict[str, set[str]] = {
    "quality_gate": {"successful_with_repair_rate", "repair_rate", "refinement_effectiveness_rate", "quality_gate_false_positive_rate", "quality_gate_false_negative_rate", "average_quality_score"},
    "refinement": {"successful_with_repair_rate", "repair_rate", "refinement_effectiveness_rate", "average_quality_score"},
    "confidence": {"average_confidence_absolute_error", "confidence_absolute_error"},
    "risk": {"risk_underestimated_rate"},
    "approval": {"risk_underestimated_rate"},
}


def policy_area(policy_key: str) -> str:
    return POLICY_AREAS.get(policy_key, policy_key)


def policy_relationship(left_policy: str, right_policy: str) -> str:
    left = policy_area(left_policy)
    right = policy_area(right_policy)
    if left == right:
        return "same_area"
    return POLICY_RELATIONSHIPS.get((left, right)) or POLICY_RELATIONSHIPS.get((right, left)) or "independent"


def _active_experiment(experiment: Mapping[str, Any]) -> bool:
    return str(experiment.get("status")) in ACTIVE_PORTFOLIO_EXPERIMENT_STATUSES


def _visible_experiment(experiment: Mapping[str, Any]) -> bool:
    return str(experiment.get("status")) in VISIBLE_PORTFOLIO_EXPERIMENT_STATUSES


def _active_rollout(rollout: Mapping[str, Any]) -> bool:
    return str(rollout.get("status")) in ACTIVE_PORTFOLIO_ROLLOUT_STATUSES


def _visible_rollout(rollout: Mapping[str, Any]) -> bool:
    return str(rollout.get("status")) in VISIBLE_PORTFOLIO_ROLLOUT_STATUSES


def _descriptor(item: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    identifier = item.get("experiment_id") if kind == "experiment" else item.get("rollout_id")
    return {
        "kind": kind,
        "id": identifier,
        "policy_key": item.get("policy_key"),
        "scope": item.get("scope"),
        "status": item.get("status"),
        "priority": item.get("portfolio_priority") or item.get("priority") or "normal",
    }


def _conflict(
    *,
    conflict_type: str,
    severity: str,
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "type": conflict_type,
        "severity": severity,
        "left": dict(left),
        "right": dict(right),
        "reason": reason,
    }


def metric_interference(left_policy: str, right_primary_metric: str | None) -> bool:
    if not right_primary_metric:
        return False
    return right_primary_metric in POLICY_METRIC_INFLUENCE.get(policy_area(left_policy), set())


def evaluate_portfolio_conflicts(
    *,
    experiments: Iterable[Mapping[str, Any]],
    rollouts: Iterable[Mapping[str, Any]],
    candidate: Mapping[str, Any] | None = None,
    candidate_kind: str | None = None,
) -> dict[str, Any]:
    visible_experiments = [dict(item) for item in experiments if _visible_experiment(item)]
    visible_rollouts = [dict(item) for item in rollouts if _visible_rollout(item)]
    active_experiments = [item for item in visible_experiments if _active_experiment(item)]
    active_rollouts = [item for item in visible_rollouts if _active_rollout(item)]
    if candidate is not None and candidate_kind == "experiment" and _active_experiment(candidate):
        active_experiments = [item for item in active_experiments if item.get("experiment_id") != candidate.get("experiment_id")]
        active_experiments.append(dict(candidate))
    if candidate is not None and candidate_kind == "rollout" and _active_rollout(candidate):
        active_rollouts = [item for item in active_rollouts if item.get("rollout_id") != candidate.get("rollout_id")]
        active_rollouts.append(dict(candidate))

    conflicts: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for index, left in enumerate(active_experiments):
        left_desc = _descriptor(left, kind="experiment")
        for right in active_experiments[index + 1 :]:
            right_desc = _descriptor(right, kind="experiment")
            same_scope = left.get("scope") == right.get("scope")
            if left.get("policy_key") == right.get("policy_key") and same_scope:
                conflicts.append(_conflict(conflict_type="same_policy_conflict", severity="blocking", left=left_desc, right=right_desc, reason="same_policy_and_scope"))
                continue
            relationship = policy_relationship(str(left.get("policy_key")), str(right.get("policy_key")))
            if same_scope and relationship == "conflicting":
                conflicts.append(_conflict(conflict_type="policy_dependency_conflict", severity="blocking", left=left_desc, right=right_desc, reason="conflicting_policy_relationship"))
            elif same_scope and relationship in {"related", "same_area"}:
                warnings.append(_conflict(conflict_type="same_scope_conflict", severity="warning", left=left_desc, right=right_desc, reason="related_policy_same_scope"))
            if metric_interference(str(left.get("policy_key")), str(right.get("primary_metric"))):
                warnings.append(_conflict(conflict_type="metric_interference", severity="warning", left=left_desc, right=right_desc, reason="left_policy_influences_right_primary_metric"))
            if metric_interference(str(right.get("policy_key")), str(left.get("primary_metric"))):
                warnings.append(_conflict(conflict_type="metric_interference", severity="warning", left=right_desc, right=left_desc, reason="left_policy_influences_right_primary_metric"))

    for experiment in active_experiments:
        exp_desc = _descriptor(experiment, kind="experiment")
        for rollout in active_rollouts:
            rollout_desc = _descriptor(rollout, kind="rollout")
            same_scope = experiment.get("scope") == rollout.get("scope")
            if experiment.get("policy_key") == rollout.get("policy_key") and same_scope:
                conflicts.append(_conflict(conflict_type="rollout_experiment_conflict", severity="blocking", left=exp_desc, right=rollout_desc, reason="same_policy_and_scope"))
                continue
            relationship = policy_relationship(str(experiment.get("policy_key")), str(rollout.get("policy_key")))
            if same_scope and relationship == "conflicting":
                conflicts.append(_conflict(conflict_type="policy_dependency_conflict", severity="blocking", left=exp_desc, right=rollout_desc, reason="conflicting_policy_relationship"))
            elif same_scope and relationship in {"related", "same_area"}:
                warnings.append(_conflict(conflict_type="planner_experiment_interference_warning", severity="warning", left=exp_desc, right=rollout_desc, reason="related_rollout_same_scope"))

    blocking = [item for item in conflicts if item.get("severity") == "blocking"]
    return {
        "version": POLICY_EXPERIMENT_PORTFOLIO_VERSION,
        "active_experiments": [_descriptor(item, kind="experiment") for item in visible_experiments],
        "active_rollouts": [_descriptor(item, kind="rollout") for item in visible_rollouts],
        "conflicts": conflicts,
        "warnings": warnings,
        "blocking_conflict_count": len(blocking),
        "warning_count": len(warnings),
        "isolation_status": "blocked" if blocking else "warnings" if warnings else "isolated",
    }
