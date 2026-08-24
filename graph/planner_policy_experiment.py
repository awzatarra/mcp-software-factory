from __future__ import annotations

import hashlib
import json
import math
import os
from statistics import NormalDist
from typing import Any, Iterable, Mapping

from graph.planner_policy_rollout import (
    POLICY_ROLLOUT_ASSIGNMENT_ALGORITHM_VERSION,
    is_infrastructure_failure,
    rollout_metrics,
)


POLICY_EXPERIMENT_VERSION = "7.13-v1"
STATISTICAL_ANALYSIS_VERSION = "7.14-v1"
PROMOTION_READINESS_VERSION = "7.15-v1"
POLICY_EXPERIMENT_ASSIGNMENT_ALGORITHM_VERSION = "stable-sha256-mod10000-v1"
ACTIVE_EXPERIMENT_STATUSES = {"ready", "running", "observing"}
TERMINAL_EXPERIMENT_STATUSES = {"completed", "cancelled", "failed", "insufficient_data"}

METRIC_DEFINITIONS: dict[str, dict[str, Any]] = {
    "planning_failure_rate": {"metric_type": "rate", "direction": "lower_is_better", "minimum_effect_size": 0.03},
    "failure_rate": {"metric_type": "rate", "direction": "lower_is_better", "minimum_effect_size": 0.03},
    "successful_with_repair_rate": {"metric_type": "rate", "direction": "lower_is_better", "minimum_effect_size": 0.03},
    "repair_rate": {"metric_type": "rate", "direction": "lower_is_better", "minimum_effect_size": 0.03},
    "quality_gate_false_positive_rate": {"metric_type": "rate", "direction": "lower_is_better", "minimum_effect_size": 0.03},
    "quality_gate_false_negative_rate": {"metric_type": "rate", "direction": "lower_is_better", "minimum_effect_size": 0.03},
    "risk_underestimated_rate": {"metric_type": "rate", "direction": "lower_is_better", "minimum_effect_size": 0.03},
    "refinement_effectiveness_rate": {"metric_type": "rate", "direction": "higher_is_better", "minimum_effect_size": 0.05},
    "confidence_absolute_error": {"metric_type": "mean", "direction": "lower_is_better", "minimum_effect_size": 0.03},
    "average_confidence_absolute_error": {"metric_type": "mean", "direction": "lower_is_better", "minimum_effect_size": 0.03},
    "average_quality_score": {"metric_type": "mean", "direction": "higher_is_better", "minimum_effect_size": 5.0},
    "outcome_score": {"metric_type": "mean", "direction": "higher_is_better", "minimum_effect_size": 5.0},
    "average_outcome_score": {"metric_type": "mean", "direction": "higher_is_better", "minimum_effect_size": 5.0},
}

METRIC_DIRECTIONS: dict[str, str] = {key: str(value["direction"]) for key, value in METRIC_DEFINITIONS.items()}

MINIMUM_EFFECT_SIZE: dict[str, float] = {key: float(value["minimum_effect_size"]) for key, value in METRIC_DEFINITIONS.items()}

DEFAULT_GUARDRAILS = {
    "planning_failure_rate": {"max_delta": 0.05},
    "risk_underestimated_rate": {"max_delta": 0.05},
}


def experiment_confidence_level(value: str | None = None) -> float:
    raw = value if value is not None else os.getenv("POLICY_EXPERIMENT_CONFIDENCE_LEVEL", "0.95")
    try:
        confidence = float(raw)
    except ValueError as exc:
        raise ValueError("planner_policy_experiment_invalid_confidence_level") from exc
    if confidence < 0.80 or confidence >= 1.0:
        raise ValueError("planner_policy_experiment_invalid_confidence_level")
    return confidence


def winner_confidence_min(value: str | None = None) -> float:
    raw = value if value is not None else os.getenv("POLICY_EXPERIMENT_WINNER_CONFIDENCE_MIN", "0.75")
    try:
        threshold = float(raw)
    except ValueError as exc:
        raise ValueError("planner_policy_experiment_invalid_confidence_level") from exc
    if threshold < 0 or threshold > 1:
        raise ValueError("planner_policy_experiment_invalid_confidence_level")
    return threshold


def safety_min_sample(value: str | None = None) -> int:
    raw = value if value is not None else os.getenv("POLICY_EXPERIMENT_SAFETY_MIN_SAMPLE", "5")
    try:
        sample = int(raw)
    except ValueError as exc:
        raise ValueError("planner_policy_experiment_invalid_sample_size") from exc
    if sample < 1:
        raise ValueError("planner_policy_experiment_invalid_sample_size")
    return sample


def promotion_readiness_min(value: str | None = None) -> int:
    raw = value if value is not None else os.getenv("POLICY_EXPERIMENT_PROMOTION_READINESS_MIN", "80")
    try:
        threshold = int(raw)
    except ValueError as exc:
        raise ValueError("planner_policy_experiment_invalid_promotion_readiness_min") from exc
    if threshold < 0 or threshold > 100:
        raise ValueError("planner_policy_experiment_invalid_promotion_readiness_min")
    return threshold


def stability_min_sample(value: str | None = None) -> int:
    raw = value if value is not None else os.getenv("POLICY_EXPERIMENT_STABILITY_MIN_SAMPLE", "10")
    try:
        sample = int(raw)
    except ValueError as exc:
        raise ValueError("planner_policy_experiment_invalid_stability_sample") from exc
    if sample < 1:
        raise ValueError("planner_policy_experiment_invalid_stability_sample")
    return sample


def equivalence_margin(value: str | None = None) -> float:
    raw = value if value is not None else os.getenv("POLICY_EXPERIMENT_EQUIVALENCE_MARGIN", "0.01")
    try:
        margin = float(raw)
    except ValueError as exc:
        raise ValueError("planner_policy_experiment_invalid_equivalence_margin") from exc
    if margin < 0:
        raise ValueError("planner_policy_experiment_invalid_equivalence_margin")
    return margin


def metric_direction(metric: str) -> str:
    try:
        return METRIC_DIRECTIONS[metric]
    except KeyError as exc:
        raise ValueError("planner_policy_experiment_invalid_metric") from exc


def metric_definition(metric: str) -> dict[str, Any]:
    canonical = metric if metric in METRIC_DEFINITIONS else canonical_metric_name(metric)
    try:
        return METRIC_DEFINITIONS[canonical]
    except KeyError as exc:
        raise ValueError("planner_policy_experiment_invalid_metric") from exc


def canonical_metric_name(metric: str) -> str:
    if metric == "planning_failure_rate":
        return "failure_rate"
    if metric == "confidence_absolute_error":
        return "average_confidence_absolute_error"
    if metric == "outcome_score":
        return "average_outcome_score"
    if metric == "successful_with_repair_rate":
        return "repair_rate"
    return metric


def validate_allocation(allocation: Mapping[str, Any], variants: Iterable[Mapping[str, Any]]) -> None:
    variant_ids = {str(variant["variant_id"]) for variant in variants}
    expected = {"control", *variant_ids}
    found = set(allocation)
    if found != expected:
        raise ValueError("planner_policy_experiment_invalid_allocation")
    total = 0
    for value in allocation.values():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValueError("planner_policy_experiment_invalid_allocation")
        total += int(value)
    if total != 100:
        raise ValueError("planner_policy_experiment_invalid_allocation")


def experiment_bucket(*, experiment_id: str, workflow_id: str, policy_key: str) -> int:
    payload = f"{experiment_id}\0{workflow_id}\0{policy_key}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return int(digest[:16], 16) % 10000


def assign_experiment_variant(
    *,
    experiment_id: str,
    workflow_id: str,
    policy_key: str,
    allocation: Mapping[str, int],
) -> str:
    bucket = experiment_bucket(experiment_id=experiment_id, workflow_id=workflow_id, policy_key=policy_key)
    cursor = 0
    for variant_id, percentage in allocation.items():
        cursor += int(percentage) * 100
        if bucket < cursor:
            return variant_id
    return "control"


def experiment_fingerprint(
    *,
    policy_key: str,
    scope: str,
    baseline_revision: int,
    control_value: Any,
    variants: list[dict[str, Any]],
    allocation: Mapping[str, int],
    primary_metric: str,
    secondary_metrics: list[str],
    guardrails: Mapping[str, Any],
) -> str:
    payload = {
        "allocation": allocation,
        "assignment_algorithm_version": POLICY_EXPERIMENT_ASSIGNMENT_ALGORITHM_VERSION,
        "baseline_revision": baseline_revision,
        "control_value": control_value,
        "evaluation_version": POLICY_EXPERIMENT_VERSION,
        "guardrails": guardrails,
        "policy_key": policy_key,
        "primary_metric": primary_metric,
        "scope": scope,
        "secondary_metrics": sorted(secondary_metrics),
        "variants": sorted(variants, key=lambda item: str(item["variant_id"])),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def experiment_assignment_for(record: Mapping[str, Any], experiment_id: str) -> dict[str, Any] | None:
    raw = record.get("planner_policy_experiment_assignments")
    assignments: Iterable[Any]
    if isinstance(raw, Mapping):
        assignments = raw.values()
    elif isinstance(raw, list):
        assignments = raw
    else:
        return None
    for assignment in assignments:
        if isinstance(assignment, Mapping) and assignment.get("experiment_id") == experiment_id:
            return dict(assignment)
    return None


def split_experiment_records(records: Iterable[Mapping[str, Any]], experiment_id: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        assignment = experiment_assignment_for(record, experiment_id)
        if assignment is None:
            continue
        variant_id = str(assignment.get("variant_id") or "control")
        groups.setdefault(variant_id, []).append(dict(record))
    return groups


def experiment_group_metrics(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    items = [dict(record) for record in records]
    metrics = rollout_metrics(items)
    metrics["excluded_infrastructure_count"] = int(metrics.get("infrastructure_sample_size") or 0)
    metrics["attributable_sample_size"] = int(metrics.get("policy_sample_size") or 0)
    policy_items = [record for record in items if not is_infrastructure_failure(record)]

    def evaluation(record: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = record.get("planning_evaluation")
        return raw if isinstance(raw, Mapping) else {}

    evaluations = [evaluation(record) for record in policy_items]

    def count_rate(predicate) -> dict[str, int]:
        denominator = len(evaluations)
        return {"successes": sum(1 for item in evaluations if predicate(item)), "sample_size": denominator}

    def mean_stats(getter) -> dict[str, Any]:
        values = [number for item in evaluations if (number := _number(getter(item))) is not None]
        sample = len(values)
        total = sum(values)
        squared = sum(value * value for value in values)
        if sample <= 1:
            variance = 0.0
        else:
            variance = max((squared - (total * total / sample)) / (sample - 1), 0.0)
        return {
            "sample_size": sample,
            "sum": round(total, 6),
            "sum_squared": round(squared, 6),
            "mean": round(total / sample, 6) if sample else None,
            "stddev": round(math.sqrt(variance), 6),
        }

    metrics["statistical_inputs"] = {
        "planning_failure_rate": count_rate(lambda item: item.get("outcome") == "failed"),
        "successful_with_repair_rate": count_rate(lambda item: item.get("successful_with_repair") is True or item.get("repair_used") is True),
        "quality_gate_false_positive_rate": count_rate(lambda item: item.get("quality_gate_error") == "false_positive" or ((item.get("quality_gate") if isinstance(item.get("quality_gate"), Mapping) else {}) or {}).get("false_positive") is True),
        "quality_gate_false_negative_rate": count_rate(lambda item: item.get("quality_gate_error") == "false_negative" or ((item.get("quality_gate") if isinstance(item.get("quality_gate"), Mapping) else {}) or {}).get("false_negative") is True),
        "risk_underestimated_rate": count_rate(lambda item: item.get("risk_calibration") == "underestimated" or ((item.get("risk_calibration") if isinstance(item.get("risk_calibration"), Mapping) else {}) or {}).get("label") == "underestimated"),
        "refinement_effectiveness_rate": count_rate(lambda item: ((item.get("refinement") if isinstance(item.get("refinement"), Mapping) else {}) or {}).get("effective") is True),
        "average_confidence_absolute_error": mean_stats(lambda item: item.get("confidence_absolute_error") or ((item.get("confidence_calibration") if isinstance(item.get("confidence_calibration"), Mapping) else {}) or {}).get("absolute_error")),
        "average_outcome_score": mean_stats(lambda item: item.get("outcome_score")),
        "average_quality_score": mean_stats(lambda item: item.get("quality_score") or ((item.get("quality_prediction") if isinstance(item.get("quality_prediction"), Mapping) else {}) or {}).get("observed")),
    }
    return metrics


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _improvement(*, metric: str, control_value: float, variant_value: float) -> float:
    direction = metric_direction(metric)
    return round(control_value - variant_value, 6) if direction == "lower_is_better" else round(variant_value - control_value, 6)


def _z_score(confidence_level: float) -> float:
    return NormalDist().inv_cdf(0.5 + confidence_level / 2)


def wilson_interval(successes: int, sample_size: int, confidence_level: float = 0.95) -> dict[str, float | None]:
    if sample_size <= 0:
        return {"estimate": None, "ci_lower": None, "ci_upper": None, "confidence_level": confidence_level}
    z = _z_score(confidence_level)
    p = successes / sample_size
    denominator = 1 + z * z / sample_size
    center = (p + z * z / (2 * sample_size)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * sample_size)) / sample_size) / denominator
    return {
        "estimate": round(p, 6),
        "ci_lower": round(max(0.0, center - margin), 6),
        "ci_upper": round(min(1.0, center + margin), 6),
        "confidence_level": confidence_level,
    }


def mean_interval(stats: Mapping[str, Any], confidence_level: float = 0.95) -> dict[str, float | None]:
    sample = int(stats.get("sample_size") or 0)
    mean = _number(stats.get("mean"))
    if sample <= 0 or mean is None:
        return {"estimate": None, "ci_lower": None, "ci_upper": None, "confidence_level": confidence_level}
    stddev = _number(stats.get("stddev")) or 0.0
    z = _z_score(confidence_level)
    stderr = stddev / math.sqrt(sample) if sample else 0.0
    return {
        "estimate": round(mean, 6),
        "ci_lower": round(mean - z * stderr, 6),
        "ci_upper": round(mean + z * stderr, 6),
        "confidence_level": confidence_level,
    }


def _metric_stats(metrics: Mapping[str, Any], metric: str) -> dict[str, Any]:
    inputs = metrics.get("statistical_inputs")
    if isinstance(inputs, Mapping) and isinstance(inputs.get(metric), Mapping):
        return dict(inputs[metric])
    sample = int(metrics.get("attributable_sample_size") or metrics.get("policy_sample_size") or 0)
    estimate = _number(metrics.get(metric)) or 0.0
    definition = metric_definition(metric)
    if definition["metric_type"] == "rate":
        return {"successes": round(estimate * sample), "sample_size": sample}
    return {"sample_size": sample, "mean": estimate, "stddev": 0.0, "sum": estimate * sample, "sum_squared": estimate * estimate * sample}


def _estimate_and_ci(metrics: Mapping[str, Any], metric: str, confidence_level: float) -> dict[str, Any]:
    stats = _metric_stats(metrics, metric)
    definition = metric_definition(metric)
    if definition["metric_type"] == "rate":
        return wilson_interval(int(stats.get("successes") or 0), int(stats.get("sample_size") or 0), confidence_level)
    return mean_interval(stats, confidence_level)


def _delta_ci(
    *,
    control_metrics: Mapping[str, Any],
    candidate_metrics: Mapping[str, Any],
    metric: str,
    confidence_level: float,
) -> tuple[float | None, float | None]:
    definition = metric_definition(metric)
    z = _z_score(confidence_level)
    control_stats = _metric_stats(control_metrics, metric)
    candidate_stats = _metric_stats(candidate_metrics, metric)
    control_n = int(control_stats.get("sample_size") or 0)
    candidate_n = int(candidate_stats.get("sample_size") or 0)
    if control_n <= 0 or candidate_n <= 0:
        return None, None
    if definition["metric_type"] == "rate":
        control_p = (int(control_stats.get("successes") or 0)) / control_n
        candidate_p = (int(candidate_stats.get("successes") or 0)) / candidate_n
        stderr = math.sqrt((control_p * (1 - control_p) / control_n) + (candidate_p * (1 - candidate_p) / candidate_n))
        delta = candidate_p - control_p
    else:
        control_mean = _number(control_stats.get("mean")) or 0.0
        candidate_mean = _number(candidate_stats.get("mean")) or 0.0
        control_sd = _number(control_stats.get("stddev")) or 0.0
        candidate_sd = _number(candidate_stats.get("stddev")) or 0.0
        stderr = math.sqrt((control_sd * control_sd / control_n) + (candidate_sd * candidate_sd / candidate_n))
        delta = candidate_mean - control_mean
    return round(delta - z * stderr, 6), round(delta + z * stderr, 6)


def minimum_detectable_effect(
    *,
    metric: str,
    control_metrics: Mapping[str, Any],
    candidate_metrics: Mapping[str, Any],
    confidence_level: float = 0.95,
) -> float | None:
    lower, upper = _delta_ci(control_metrics=control_metrics, candidate_metrics=candidate_metrics, metric=metric, confidence_level=confidence_level)
    if lower is None or upper is None:
        return None
    return round((upper - lower) / 2, 6)


def estimate_required_sample_size(*, metric: str, baseline_value: float, effect_size: float, confidence_level: float = 0.95, variance: float | None = None) -> int | None:
    if effect_size <= 0:
        return None
    z = _z_score(confidence_level)
    definition = metric_definition(metric)
    if definition["metric_type"] == "rate":
        p1 = min(max(baseline_value, 0.0), 1.0)
        p2 = min(max(baseline_value + effect_size, 0.0), 1.0)
        pooled_variance = p1 * (1 - p1) + p2 * (1 - p2)
        return max(1, math.ceil(2 * (z * z) * pooled_variance / (effect_size * effect_size)))
    used_variance = 1.0 if variance is None else max(variance, 0.0)
    return max(1, math.ceil(2 * (z * z) * used_variance / (effect_size * effect_size)))


def _sample_adequacy(sample_size: int, required_sample: int | None, minimum_sample_size: int) -> str:
    if sample_size < minimum_sample_size:
        return "insufficient"
    if required_sample is None:
        return "minimum_met"
    if sample_size >= required_sample * 2:
        return "strong"
    if sample_size >= required_sample:
        return "adequate"
    return "minimum_met"


def _confidence_label(value: float) -> str:
    if value < 0.50:
        return "low"
    if value < 0.70:
        return "medium"
    if value < 0.90:
        return "high"
    return "very_high"


def _decision_confidence(
    *,
    sample_adequacy: str,
    improvement_delta: float,
    minimum_effect_size: float,
    delta_ci_lower: float | None,
    delta_ci_upper: float | None,
    guardrail_status: str,
) -> float:
    sample_factor = {"insufficient": 0.15, "minimum_met": 0.45, "adequate": 0.75, "strong": 0.95}.get(sample_adequacy, 0.3)
    effect_factor = min(abs(improvement_delta) / max(minimum_effect_size, 1e-9), 2.0) / 2.0
    if delta_ci_lower is None or delta_ci_upper is None:
        separation_factor = 0.0
    elif delta_ci_lower > 0:
        separation_factor = min(delta_ci_lower / max(minimum_effect_size, 1e-9), 1.0)
    else:
        separation_factor = 0.0
    guardrail_factor = 0.0 if guardrail_status == "unsafe" else 1.0
    return round(max(0.0, min(1.0, 0.30 * sample_factor + 0.25 * effect_factor + 0.35 * separation_factor + 0.10 * guardrail_factor)), 4)


def evaluate_experiment_result(
    *,
    control_metrics: Mapping[str, Any],
    variant_metrics: Mapping[str, Mapping[str, Any]],
    primary_metric: str,
    guardrails: Mapping[str, Any] | None = None,
    minimum_sample_size: int = 20,
    confidence_level: float | None = None,
    winner_confidence_threshold: float | None = None,
) -> dict[str, Any]:
    confidence_level = experiment_confidence_level(str(confidence_level) if confidence_level is not None else None)
    winner_confidence_threshold = winner_confidence_min(str(winner_confidence_threshold) if winner_confidence_threshold is not None else None)
    minimum_sample_size = max(int(minimum_sample_size), safety_min_sample())
    equivalence = equivalence_margin()
    metric = canonical_metric_name(primary_metric)
    primary_definition = metric_definition(primary_metric)
    control_sample = int(control_metrics.get("attributable_sample_size") or control_metrics.get("policy_sample_size") or 0)
    insufficient = [
        variant_id
        for variant_id, metrics in {"control": control_metrics, **variant_metrics}.items()
        if int(metrics.get("attributable_sample_size") or metrics.get("policy_sample_size") or 0) < minimum_sample_size
    ]
    if insufficient:
        return {
            "result": "insufficient_data",
            "winner_variant_id": None,
            "reason_codes": ["insufficient_sample"],
            "insufficient_groups": insufficient,
            "comparisons": {},
            "statistical_summary": {
                "confidence_level": confidence_level,
                "analysis_version": STATISTICAL_ANALYSIS_VERSION,
                "primary_metric": primary_metric,
                "comparisons": {},
                "multiple_comparison_method": "holm_bonferroni_confidence",
                "decision_confidence": 0.0,
                "decision_confidence_label": "low",
                "estimated_required_sample_size": None,
            },
            "control_sample_size": control_sample,
        }
    control_value = _number(control_metrics.get(metric))
    if control_value is None:
        return {"result": "inconclusive", "winner_variant_id": None, "reason_codes": ["primary_metric_unavailable"], "comparisons": {}, "statistical_summary": {"confidence_level": confidence_level, "analysis_version": STATISTICAL_ANALYSIS_VERSION, "primary_metric": primary_metric, "comparisons": {}, "multiple_comparison_method": "holm_bonferroni_confidence", "decision_confidence": 0.0, "decision_confidence_label": "low", "estimated_required_sample_size": None}}
    threshold = MINIMUM_EFFECT_SIZE.get(primary_metric, MINIMUM_EFFECT_SIZE.get(metric, 0.03))
    guardrail_config = dict(DEFAULT_GUARDRAILS)
    guardrail_config.update(guardrails or {})
    comparisons: dict[str, dict[str, Any]] = {}
    statistical_comparisons: dict[str, dict[str, Any]] = {}
    unsafe: list[str] = []
    adjusted_confidence = 1 - ((1 - confidence_level) / max(len(variant_metrics), 1))
    control_ci = _estimate_and_ci(control_metrics, metric, confidence_level)
    for variant_id, metrics in variant_metrics.items():
        variant_value = _number(metrics.get(metric))
        if variant_value is None:
            comparisons[variant_id] = {"status": "unavailable", "reason": "metric_not_available"}
            continue
        improvement = _improvement(metric=primary_metric, control_value=control_value, variant_value=variant_value)
        absolute_delta = round(variant_value - control_value, 6)
        relative_delta_pct = round((absolute_delta / abs(control_value)) * 100, 6) if control_value else None
        guardrail_status = "ok"
        guardrail_violations: list[str] = []
        for guardrail_metric, config in guardrail_config.items():
            key = canonical_metric_name(guardrail_metric)
            limit = _number((config or {}).get("max_delta") if isinstance(config, Mapping) else None)
            if limit is None:
                continue
            control_guardrail = _number(control_metrics.get(key)) or 0.0
            variant_guardrail = _number(metrics.get(key)) or 0.0
            if variant_guardrail - control_guardrail > limit:
                guardrail_status = "unsafe"
                guardrail_violations.append(guardrail_metric)
        if guardrail_status == "unsafe":
            unsafe.append(variant_id)
        candidate_ci = _estimate_and_ci(metrics, metric, confidence_level)
        delta_lower, delta_upper = _delta_ci(control_metrics=control_metrics, candidate_metrics=metrics, metric=metric, confidence_level=confidence_level)
        adjusted_delta_lower, adjusted_delta_upper = _delta_ci(control_metrics=control_metrics, candidate_metrics=metrics, metric=metric, confidence_level=adjusted_confidence)
        if metric_direction(primary_metric) == "lower_is_better":
            improvement_ci_lower = round(-(adjusted_delta_upper or 0), 6) if adjusted_delta_upper is not None else None
            improvement_ci_upper = round(-(adjusted_delta_lower or 0), 6) if adjusted_delta_lower is not None else None
            raw_improvement_ci_lower = round(-(delta_upper or 0), 6) if delta_upper is not None else None
            raw_improvement_ci_upper = round(-(delta_lower or 0), 6) if delta_lower is not None else None
        else:
            improvement_ci_lower = adjusted_delta_lower
            improvement_ci_upper = adjusted_delta_upper
            raw_improvement_ci_lower = delta_lower
            raw_improvement_ci_upper = delta_upper
        statistically_significant = raw_improvement_ci_lower is not None and raw_improvement_ci_lower > 0
        adjusted_significant = improvement_ci_lower is not None and improvement_ci_lower > 0
        practically_significant = abs(improvement) >= threshold
        mde = minimum_detectable_effect(metric=metric, control_metrics=control_metrics, candidate_metrics=metrics, confidence_level=confidence_level)
        variance = None
        stats = _metric_stats(control_metrics, metric)
        if primary_definition["metric_type"] == "mean":
            variance = (_number(stats.get("stddev")) or 0.0) ** 2
        required_sample = estimate_required_sample_size(metric=metric, baseline_value=control_value, effect_size=threshold, confidence_level=confidence_level, variance=variance)
        sample_size = int(metrics.get("attributable_sample_size") or metrics.get("policy_sample_size") or 0)
        adequacy = _sample_adequacy(sample_size, required_sample, minimum_sample_size)
        confidence = _decision_confidence(
            sample_adequacy=adequacy,
            improvement_delta=improvement,
            minimum_effect_size=threshold,
            delta_ci_lower=improvement_ci_lower,
            delta_ci_upper=improvement_ci_upper,
            guardrail_status=guardrail_status,
        )
        comparisons[variant_id] = {
            "control_value": control_value,
            "variant_value": variant_value,
            "delta": absolute_delta,
            "improvement": improvement,
            "minimum_effect_size": threshold,
            "guardrail_status": guardrail_status,
            "guardrail_violations": guardrail_violations,
            "sample_size": sample_size,
            "statistically_significant": statistically_significant,
            "practically_significant": practically_significant,
            "adjusted_significant": adjusted_significant,
            "decision_confidence": confidence,
        }
        statistical_comparisons[variant_id] = {
            "control_variant": "control",
            "candidate_variant": variant_id,
            "metric": primary_metric,
            "metric_type": primary_definition["metric_type"],
            "control_estimate": control_ci["estimate"],
            "candidate_estimate": candidate_ci["estimate"],
            "absolute_delta": absolute_delta,
            "relative_delta_pct": relative_delta_pct,
            "improvement_delta": improvement,
            "control_ci": [control_ci["ci_lower"], control_ci["ci_upper"]],
            "candidate_ci": [candidate_ci["ci_lower"], candidate_ci["ci_upper"]],
            "delta_ci": [raw_improvement_ci_lower, raw_improvement_ci_upper],
            "adjusted_delta_ci": [improvement_ci_lower, improvement_ci_upper],
            "minimum_effect_size": threshold,
            "minimum_detectable_effect": mde,
            "estimated_required_sample_size": required_sample,
            "statistically_significant": statistically_significant,
            "practically_significant": practically_significant,
            "adjusted_significant": adjusted_significant,
            "sample_adequacy": adequacy,
            "decision_confidence": confidence,
            "decision_confidence_label": _confidence_label(confidence),
        }
    if unsafe:
        safe_comparisons = {key: value for key, value in comparisons.items() if key not in unsafe and value.get("guardrail_status") != "unsafe"}
    else:
        safe_comparisons = comparisons
    candidates = [
        (variant_id, comparison)
        for variant_id, comparison in safe_comparisons.items()
        if _number(comparison.get("improvement")) is not None
        and float(comparison["improvement"]) >= threshold
        and comparison.get("statistically_significant") is True
        and comparison.get("practically_significant") is True
        and comparison.get("adjusted_significant") is True
        and float(comparison.get("decision_confidence") or 0) >= winner_confidence_threshold
    ]
    best_confidence = max([float(item.get("decision_confidence") or 0) for item in comparisons.values()] or [0.0])
    statistical_summary = {
        "confidence_level": confidence_level,
        "analysis_version": STATISTICAL_ANALYSIS_VERSION,
        "primary_metric": primary_metric,
        "comparisons": statistical_comparisons,
        "multiple_comparison_method": "holm_bonferroni_confidence",
        "adjusted_significance": adjusted_confidence,
        "equivalence_margin": equivalence,
        "decision_confidence": round(best_confidence, 4),
        "decision_confidence_label": _confidence_label(best_confidence),
        "estimated_required_sample_size": next((item.get("estimated_required_sample_size") for item in statistical_comparisons.values() if item.get("estimated_required_sample_size") is not None), None),
    }
    if not candidates:
        noisy_positive = any(float(item.get("improvement") or 0) > 0 for item in safe_comparisons.values())
        return {
            "result": "unsafe_variant" if unsafe and len(unsafe) == len(variant_metrics) else "inconclusive" if noisy_positive else "control_preferred",
            "winner_variant_id": None,
            "reason_codes": ["guardrail_violated"] if unsafe else ["statistical_criteria_not_met"] if noisy_positive else ["no_significant_variant_improvement"],
            "unsafe_variants": unsafe,
            "comparisons": comparisons,
            "statistical_summary": statistical_summary,
        }
    candidates.sort(key=lambda item: float(item[1]["improvement"]), reverse=True)
    best_id, best = candidates[0]
    equivalent = [
        variant_id
        for variant_id, comparison in candidates[1:]
        if abs(float(best["improvement"]) - float(comparison["improvement"])) < threshold
    ]
    if equivalent:
        return {
            "result": "multiple_variants_equivalent",
            "winner_variant_id": None,
            "reason_codes": ["multiple_equivalent_variants"],
            "equivalent_variants": [best_id, *equivalent],
            "unsafe_variants": unsafe,
            "comparisons": comparisons,
            "statistical_summary": statistical_summary,
        }
    return {
        "result": "variant_preferred",
        "winner_variant_id": best_id,
        "reason_codes": ["variant_significant_improvement"],
        "unsafe_variants": unsafe,
        "comparisons": comparisons,
        "statistical_summary": statistical_summary,
    }


def _score_average(values: Iterable[float]) -> int:
    items = [max(0.0, min(100.0, float(value))) for value in values]
    return round(sum(items) / len(items)) if items else 0


def _readiness_status_for_non_winner(result: str | None) -> tuple[str, str, list[str]]:
    if result == "unsafe_variant":
        return "blocked_by_guardrail", "reject_promotion", ["promotion_guardrail_violation"]
    if result == "insufficient_data":
        return "needs_more_data", "continue_experiment", ["promotion_sample_insufficient"]
    if result == "multiple_variants_equivalent":
        return "inconclusive", "review_equivalent_variants", ["promotion_experiment_inconclusive"]
    if result == "control_preferred":
        return "no_winner", "keep_control", ["promotion_no_winner"]
    return "inconclusive", "continue_observation", ["promotion_experiment_inconclusive"]


def _secondary_metric_consistency(
    *,
    secondary_metrics: Iterable[str],
    control_metrics: Mapping[str, Any],
    winner_metrics: Mapping[str, Any],
) -> dict[str, Any]:
    supportive = 0
    neutral = 0
    mixed = 0
    contradictory = 0
    details: dict[str, dict[str, Any]] = {}
    for raw_metric in secondary_metrics:
        metric = canonical_metric_name(str(raw_metric))
        control = _number(control_metrics.get(metric))
        winner = _number(winner_metrics.get(metric))
        if control is None or winner is None:
            continue
        improvement = _improvement(metric=metric, control_value=control, variant_value=winner)
        threshold = MINIMUM_EFFECT_SIZE.get(str(raw_metric), MINIMUM_EFFECT_SIZE.get(metric, 0.03))
        if improvement >= threshold:
            classification = "supportive"
            supportive += 1
        elif improvement <= -threshold:
            classification = "contradictory"
            contradictory += 1
        elif improvement < 0:
            classification = "mixed"
            mixed += 1
        else:
            classification = "neutral"
            neutral += 1
        details[str(raw_metric)] = {
            "control_value": control,
            "winner_value": winner,
            "improvement": round(improvement, 6),
            "classification": classification,
        }
    overall = "contradictory" if contradictory else "mixed" if mixed else "supportive" if supportive else "neutral"
    return {
        "status": overall,
        "supportive": supportive,
        "neutral": neutral,
        "mixed": mixed,
        "contradictory": contradictory,
        "details": details,
    }


def _temporal_stability(
    *,
    groups: Mapping[str, list[dict[str, Any]]] | None,
    winner_variant_id: str,
    primary_metric: str,
    minimum_per_window: int,
) -> dict[str, Any]:
    if not groups:
        return {"status": "insufficient_data", "reason": "records_unavailable"}
    control_records = list(groups.get("control") or [])
    winner_records = list(groups.get(winner_variant_id) or [])
    if len(control_records) < minimum_per_window * 2 or len(winner_records) < minimum_per_window * 2:
        return {"status": "insufficient_data", "reason": "window_sample_insufficient"}

    def split(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        midpoint = len(records) // 2
        return records[:midpoint], records[midpoint:]

    control_early, control_recent = split(control_records)
    winner_early, winner_recent = split(winner_records)
    metric = canonical_metric_name(primary_metric)
    early_control = experiment_group_metrics(control_early)
    early_winner = experiment_group_metrics(winner_early)
    recent_control = experiment_group_metrics(control_recent)
    recent_winner = experiment_group_metrics(winner_recent)
    early_control_value = _number(early_control.get(metric))
    early_winner_value = _number(early_winner.get(metric))
    recent_control_value = _number(recent_control.get(metric))
    recent_winner_value = _number(recent_winner.get(metric))
    if None in {early_control_value, early_winner_value, recent_control_value, recent_winner_value}:
        return {"status": "insufficient_data", "reason": "window_metric_unavailable"}
    early_effect = _improvement(metric=primary_metric, control_value=float(early_control_value), variant_value=float(early_winner_value))
    recent_effect = _improvement(metric=primary_metric, control_value=float(recent_control_value), variant_value=float(recent_winner_value))
    threshold = MINIMUM_EFFECT_SIZE.get(primary_metric, MINIMUM_EFFECT_SIZE.get(metric, 0.03))
    if recent_effect <= -threshold:
        status = "degrading"
    elif recent_effect >= threshold and early_effect >= threshold:
        status = "improving" if recent_effect > early_effect else "stable"
    elif recent_effect >= 0:
        status = "stable"
    else:
        status = "degrading"
    return {
        "status": status,
        "early_effect": round(early_effect, 6),
        "recent_effect": round(recent_effect, 6),
        "minimum_effect_size": threshold,
        "window_sample_size": min(len(control_early), len(control_recent), len(winner_early), len(winner_recent)),
    }


def evaluate_promotion_readiness(
    *,
    experiment: Mapping[str, Any],
    control_metrics: Mapping[str, Any],
    variant_metrics: Mapping[str, Mapping[str, Any]],
    groups: Mapping[str, list[dict[str, Any]]] | None = None,
    policy_baseline_current: bool = True,
    readiness_threshold: int | None = None,
    stability_sample: int | None = None,
) -> dict[str, Any]:
    readiness_threshold = promotion_readiness_min(str(readiness_threshold) if readiness_threshold is not None else None)
    stability_sample = stability_min_sample(str(stability_sample) if stability_sample is not None else None)
    result = str(experiment.get("result") or "")
    winner_variant_id = experiment.get("winner_variant_id")
    metrics = experiment.get("metrics") if isinstance(experiment.get("metrics"), Mapping) else {}
    comparison = metrics.get("comparison") if isinstance(metrics.get("comparison"), Mapping) else {}
    statistical_summary = experiment.get("statistical_summary")
    if not isinstance(statistical_summary, Mapping):
        statistical_summary = comparison.get("statistical_summary") if isinstance(comparison.get("statistical_summary"), Mapping) else {}
    checks = {
        "winner_present": bool(winner_variant_id),
        "sample_sufficient": False,
        "statistical_significance": False,
        "practical_significance": False,
        "guardrails_clear": False,
        "secondary_metrics_consistent": True,
        "temporal_stability_ok": True,
        "policy_baseline_current": bool(policy_baseline_current),
    }
    reason_codes: list[str] = []
    if result != "variant_preferred" or not winner_variant_id:
        status, action, codes = _readiness_status_for_non_winner(result)
        reason_codes.extend(codes)
        checks["policy_baseline_current"] = bool(policy_baseline_current)
        return {
            "status": status,
            "score": 0,
            "confidence": 0.0,
            "reason_codes": reason_codes,
            "checks": checks,
            "winner_variant_id": winner_variant_id,
            "recommended_action": action,
            "version": PROMOTION_READINESS_VERSION,
        }

    winner_key = str(winner_variant_id)
    winner_metrics = variant_metrics.get(winner_key) or {}
    comparisons = comparison.get("comparisons") if isinstance(comparison.get("comparisons"), Mapping) else {}
    winner_comparison = comparisons.get(winner_key) if isinstance(comparisons.get(winner_key), Mapping) else {}
    statistical_comparisons = statistical_summary.get("comparisons") if isinstance(statistical_summary.get("comparisons"), Mapping) else {}
    winner_stats = statistical_comparisons.get(winner_key) if isinstance(statistical_comparisons.get(winner_key), Mapping) else {}

    sample_size = int(winner_comparison.get("sample_size") or winner_metrics.get("attributable_sample_size") or winner_metrics.get("policy_sample_size") or 0)
    required_sample = _number(winner_stats.get("estimated_required_sample_size"))
    mde = _number(winner_stats.get("minimum_detectable_effect"))
    effect = abs(_number(winner_stats.get("improvement_delta")) or _number(winner_comparison.get("improvement")) or 0.0)
    minimum_effect = _number(winner_stats.get("minimum_effect_size")) or _number(winner_comparison.get("minimum_effect_size")) or 0.03
    decision_confidence = _number(statistical_summary.get("decision_confidence")) or _number(winner_stats.get("decision_confidence")) or 0.0

    checks["sample_sufficient"] = sample_size >= int(experiment.get("minimum_sample_size") or 0) and (required_sample is None or sample_size >= required_sample)
    checks["statistical_significance"] = bool(winner_comparison.get("statistically_significant") and winner_comparison.get("adjusted_significant"))
    checks["practical_significance"] = bool(winner_comparison.get("practically_significant"))
    checks["guardrails_clear"] = winner_comparison.get("guardrail_status") != "unsafe"

    if not checks["sample_sufficient"]:
        reason_codes.append("promotion_sample_insufficient")
    if mde is not None and effect < mde:
        reason_codes.append("promotion_mde_too_large")
    if decision_confidence < winner_confidence_min():
        reason_codes.append("promotion_confidence_insufficient")
    if not checks["statistical_significance"]:
        reason_codes.append("promotion_confidence_insufficient")
    if not checks["practical_significance"]:
        reason_codes.append("promotion_experiment_inconclusive")
    if not checks["guardrails_clear"]:
        reason_codes.append("promotion_guardrail_violation")

    secondary = _secondary_metric_consistency(
        secondary_metrics=experiment.get("secondary_metrics") or [],
        control_metrics=control_metrics,
        winner_metrics=winner_metrics,
    )
    checks["secondary_metrics_consistent"] = secondary["status"] != "contradictory"
    if secondary["status"] == "mixed":
        reason_codes.append("promotion_secondary_metrics_mixed")
    elif secondary["status"] == "contradictory":
        reason_codes.append("promotion_secondary_metrics_contradictory")

    temporal = _temporal_stability(
        groups=groups,
        winner_variant_id=winner_key,
        primary_metric=str(experiment.get("primary_metric")),
        minimum_per_window=stability_sample,
    )
    checks["temporal_stability_ok"] = temporal["status"] not in {"degrading"}
    if temporal["status"] == "degrading":
        reason_codes.append("promotion_temporal_instability")

    variants = {str(item.get("variant_id")): item for item in experiment.get("variants") or [] if isinstance(item, Mapping)}
    winner = variants.get(winner_key) or {}
    risk_level = str(winner.get("risk_level") or "medium")
    safety_flags = winner.get("safety_flags") if isinstance(winner.get("safety_flags"), Mapping) else {}
    critical_safety = any(bool(safety_flags.get(key)) for key in ("reduces_safety", "reduces_approval_requirements", "increases_failure_tolerance"))
    if risk_level in {"high", "critical"}:
        reason_codes.append("promotion_policy_risk_high")
    if critical_safety:
        reason_codes.append("promotion_guardrail_violation")

    delta_ci = winner_stats.get("adjusted_delta_ci") if isinstance(winner_stats.get("adjusted_delta_ci"), list) else []
    ci_lower = _number(delta_ci[0]) if len(delta_ci) > 0 else None
    guardrail_margin = None
    if winner_comparison.get("guardrail_status") == "ok":
        guardrail_margin = 1.0
    statistical_strength = 35
    if checks["statistical_significance"]:
        statistical_strength = 70 + min(30, decision_confidence * 30)
        if ci_lower is not None and ci_lower > 0:
            statistical_strength = min(100, statistical_strength + min(10, ci_lower / max(minimum_effect, 1e-9) * 5))
    sample_strength = 55 if checks["sample_sufficient"] else 35
    if required_sample and required_sample > 0:
        sample_strength = min(100, max(20, (sample_size / required_sample) * 80))
    effect_strength = min(100, (effect / max(minimum_effect, 1e-9)) * 35)
    guardrail_safety = 0 if not checks["guardrails_clear"] or critical_safety else 100
    metric_consistency = {"supportive": 95, "neutral": 85, "mixed": 60, "contradictory": 20}.get(str(secondary["status"]), 75)
    temporal_strength = {"stable": 95, "improving": 100, "insufficient_data": 70, "degrading": 20}.get(str(temporal["status"]), 70)
    policy_risk = {"low": 100, "medium": 85, "high": 65, "critical": 35}.get(risk_level, 80)
    dimensions = {
        "statistical_strength": round(statistical_strength),
        "sample_strength": round(sample_strength),
        "effect_strength": round(effect_strength),
        "guardrail_safety": round(guardrail_safety),
        "metric_consistency": round(metric_consistency),
        "temporal_stability": round(temporal_strength),
        "policy_risk": round(policy_risk),
    }
    score = _score_average(dimensions.values())
    confidence = round(_score_average([dimensions["statistical_strength"], dimensions["sample_strength"], dimensions["guardrail_safety"], dimensions["metric_consistency"], dimensions["temporal_stability"]]) / 100, 4)

    if not checks["guardrails_clear"] or critical_safety:
        status = "blocked_by_guardrail"
        action = "reject_promotion"
    elif not checks["sample_sufficient"] or "promotion_mde_too_large" in reason_codes or "promotion_confidence_insufficient" in reason_codes:
        status = "needs_more_data"
        action = "continue_experiment"
    elif not checks["secondary_metrics_consistent"] or not checks["temporal_stability_ok"]:
        status = "unstable"
        action = "continue_observation"
    elif not checks["policy_baseline_current"]:
        status = "inconclusive"
        action = "refresh_policy_baseline"
        reason_codes.append("promotion_experiment_inconclusive")
    elif score >= readiness_threshold:
        status = "ready"
        action = "recommend_promotion"
        reason_codes.append("promotion_ready")
    else:
        status = "needs_more_data"
        action = "continue_experiment"
        reason_codes.append("promotion_sample_insufficient")

    deduped_codes = list(dict.fromkeys(reason_codes))
    return {
        "status": status,
        "score": score,
        "confidence": confidence,
        "reason_codes": deduped_codes,
        "checks": checks,
        "winner_variant_id": winner_key,
        "recommended_action": action,
        "version": PROMOTION_READINESS_VERSION,
        "readiness_threshold": readiness_threshold,
        "dimensions": dimensions,
        "sample_size": sample_size,
        "estimated_required_sample_size": required_sample,
        "minimum_detectable_effect": mde,
        "effect_size": round(effect, 6),
        "minimum_effect_size": minimum_effect,
        "decision_confidence": round(decision_confidence, 4),
        "guardrail_margin": guardrail_margin,
        "secondary_metric_consistency": secondary,
        "temporal_stability": temporal,
        "policy_risk": {"risk_level": risk_level, "critical_safety_flags": critical_safety, "safety_flags": dict(safety_flags)},
        "statistical_analysis_version": statistical_summary.get("analysis_version"),
    }
