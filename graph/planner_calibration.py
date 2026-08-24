from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from graph.planner_evaluation import PLANNING_EVALUATION_VERSION, aggregate_planner_evaluations
from graph.subgraphs.planning.quality import QUALITY_WEIGHTS


PLANNER_CALIBRATION_ANALYTICS_VERSION = "7.8-v1"


@dataclass(frozen=True)
class PlannerPolicySnapshot:
    quality_weights: dict[str, float] = field(default_factory=lambda: dict(QUALITY_WEIGHTS))
    quality_levels: dict[str, int] = field(
        default_factory=lambda: {
            "excellent": 90,
            "good": 75,
            "acceptable": 60,
            "weak": 40,
        }
    )
    quality_gate_dimension_minimums: dict[str, int] = field(
        default_factory=lambda: {
            "requirement_coverage": 70,
            "plan_completeness": 70,
            "task_clarity": 50,
            "dependency_coherence": 60,
        }
    )
    confidence_low_threshold: float = 0.55
    risk_level_thresholds: dict[str, int] = field(
        default_factory=lambda: {"medium": 25, "high": 50, "critical": 75}
    )
    risk_approval_sensitive_areas: list[str] = field(
        default_factory=lambda: [
            "configuration",
            "database",
            "dependencies",
            "infrastructure",
            "runtime",
            "security",
        ]
    )
    quality_gate_weak_threshold: int = 60

    def model_dump(self) -> dict[str, Any]:
        return {
            "quality_weights": dict(self.quality_weights),
            "quality_levels": dict(self.quality_levels),
            "quality_gate_dimension_minimums": dict(self.quality_gate_dimension_minimums),
            "confidence_low_threshold": self.confidence_low_threshold,
            "risk_level_thresholds": dict(self.risk_level_thresholds),
            "risk_approval_sensitive_areas": list(self.risk_approval_sensitive_areas),
            "quality_gate_weak_threshold": self.quality_gate_weak_threshold,
        }


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rate(count: int, total: int) -> float:
    return 0.0 if total <= 0 else round(count / total, 4)


def _avg(values: Iterable[Any]) -> float | None:
    numbers = [number for value in values if (number := _as_float(value)) is not None]
    return None if not numbers else round(sum(numbers) / len(numbers), 4)


def _evaluation(record: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = record.get("planning_evaluation")
    if not isinstance(raw, dict):
        raw = record.get("evaluation")
    return raw if isinstance(raw, dict) else None


def _compatible_records(
    records: Iterable[Mapping[str, Any]],
    version: str,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    compatible: list[dict[str, Any]] = []
    versions: Counter[str] = Counter()
    for record in records:
        evaluation = _evaluation(record)
        if not evaluation:
            continue
        found_version = str(evaluation.get("version") or record.get("planning_evaluation_version") or "unknown")
        versions[found_version] += 1
        if found_version != version:
            continue
        item = dict(record)
        item["planning_evaluation"] = dict(evaluation)
        compatible.append(item)
    return compatible, versions


def _metric_bundle(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    aggregate = aggregate_planner_evaluations(records)
    total = int(aggregate["total_evaluated"])
    over = int(aggregate["overconfident_count"])
    under = int(aggregate["underconfident_count"])
    well = int(aggregate["well_calibrated_count"])
    risk_under = int(aggregate["risk_underestimated_count"])
    risk_over = int(aggregate["risk_overestimated_count"])
    risk_aligned = int(aggregate["risk_aligned_count"])
    fp = int(aggregate["quality_gate_false_positive_count"])
    fn = int(aggregate["quality_gate_false_negative_count"])
    successful_with_repair = int(aggregate["successful_with_repair"])
    failed = int(aggregate["failed"])
    refinement_used = [
        record
        for record in records
        if ((record.get("planning_evaluation") or {}).get("refinement") or {}).get("effective") is not None
    ]
    refinement_effective = sum(
        1
        for record in refinement_used
        if ((record.get("planning_evaluation") or {}).get("refinement") or {}).get("effective") is True
    )
    approval_required = sum(1 for record in records if record.get("approval_required") is True)
    clean_success = sum(
        1
        for record in records
        if (record.get("planning_evaluation") or {}).get("outcome") == "successful"
    )
    metrics = {
        **aggregate,
        "sample_size": total,
        "overconfident_rate": _rate(over, total),
        "underconfident_rate": _rate(under, total),
        "well_calibrated_rate": _rate(well, total),
        "risk_underestimated_rate": _rate(risk_under, total),
        "risk_overestimated_rate": _rate(risk_over, total),
        "risk_aligned_rate": _rate(risk_aligned, total),
        "quality_gate_false_positive_rate": _rate(fp, total),
        "quality_gate_false_negative_rate": _rate(fn, total),
        "refinement_effectiveness_rate": _rate(refinement_effective, len(refinement_used)),
        "refinement_used_count": len(refinement_used),
        "planning_failure_rate": _rate(failed, total),
        "successful_with_repair_rate": _rate(successful_with_repair, total),
        "approval_required_rate": _rate(approval_required, total),
        "clean_success_rate": _rate(clean_success, total),
    }
    return metrics


def _segment_key(value: Any) -> str:
    text = str(value if value is not None else "unknown").strip().casefold()
    return text or "unknown"


def _segments(records: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    definitions = {
        "framework": lambda item: item.get("framework"),
        "risk_level": lambda item: item.get("risk_level"),
        "quality_level": lambda item: item.get("quality_level"),
        "approval_required": lambda item: "true" if item.get("approval_required") is True else "false",
        "refinement_used": lambda item: (
            "true"
            if ((item.get("planning_evaluation") or {}).get("refinement") or {}).get("attempts", 0)
            else "false"
        ),
    }
    output: dict[str, dict[str, Any]] = {}
    for name, getter in definitions.items():
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for record in records:
            buckets.setdefault(_segment_key(getter(record)), []).append(record)
        output[name] = {
            key: _metric_bundle(list(items))
            for key, items in sorted(buckets.items())
        }
    return output


def _recommendation_id(
    *,
    policy: str,
    segment: str | None,
    direction: str,
    reason_codes: list[str],
) -> str:
    payload = {
        "version": PLANNER_CALIBRATION_ANALYTICS_VERSION,
        "policy": policy,
        "segment": segment,
        "direction": direction,
        "reason_codes": sorted(reason_codes),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _confidence(sample_size: int, observed_rate: float, threshold: float, error: float | None) -> float:
    sample_factor = min(sample_size / 100, 1.0)
    effect_factor = min(max(observed_rate - threshold, 0) / max(1 - threshold, 0.01), 1.0)
    error_factor = min((error or 0) / 0.30, 1.0)
    return round(max(0.1, 0.35 * sample_factor + 0.40 * effect_factor + 0.25 * error_factor), 4)


def _severity(rate: float, medium: float, high: float) -> str:
    if rate >= high:
        return "high"
    if rate >= medium:
        return "medium"
    if rate > 0:
        return "low"
    return "info"


def _recommendation(
    *,
    policy: str,
    current_value: Any,
    suggested_value: Any,
    direction: str,
    severity: str,
    confidence: float,
    evidence: dict[str, Any],
    reason_codes: list[str],
    segment: str | None = None,
) -> dict[str, Any]:
    return {
        "recommendation_id": _recommendation_id(
            policy=policy,
            segment=segment,
            direction=direction,
            reason_codes=reason_codes,
        ),
        "policy": policy,
        "segment": segment,
        "current_value": current_value,
        "suggested_value": suggested_value,
        "direction": direction,
        "severity": severity,
        "confidence": confidence,
        "evidence": evidence,
        "reason_codes": reason_codes,
        "status": "recommendation_only",
    }


def _global_recommendations(
    metrics: Mapping[str, Any],
    policy_snapshot: PlannerPolicySnapshot,
    minimum_sample_size: int,
) -> list[dict[str, Any]]:
    sample = int(metrics.get("sample_size") or 0)
    recommendations: list[dict[str, Any]] = []
    confidence_error = _as_float(metrics.get("average_confidence_absolute_error"))
    if metrics.get("overconfident_rate", 0) >= 0.30 and (confidence_error or 0) >= 0.15:
        rate = float(metrics["overconfident_rate"])
        recommendations.append(
            _recommendation(
                policy="planning_decision_confidence",
                current_value="current_formula",
                suggested_value={"suggested_adjustment": -0.05},
                direction="decrease",
                severity=_severity(rate, 0.30, 0.50),
                confidence=_confidence(sample, rate, 0.30, confidence_error),
                evidence={
                    "sample_size": sample,
                    "observed_rate": rate,
                    "threshold": 0.30,
                    "average_error": confidence_error,
                },
                reason_codes=["confidence_overestimated", "confidence_absolute_error_high"],
            )
        )
    if (
        metrics.get("underconfident_rate", 0) >= 0.30
        and (metrics.get("average_outcome_score") or 0) >= 80
    ):
        rate = float(metrics["underconfident_rate"])
        recommendations.append(
            _recommendation(
                policy="planning_decision_confidence",
                current_value="current_formula",
                suggested_value={"review": "confidence penalties"},
                direction="increase",
                severity=_severity(rate, 0.30, 0.50),
                confidence=_confidence(sample, rate, 0.30, confidence_error),
                evidence={
                    "sample_size": sample,
                    "observed_rate": rate,
                    "threshold": 0.30,
                    "average_outcome_score": metrics.get("average_outcome_score"),
                },
                reason_codes=["confidence_underestimated", "clean_outcomes_high"],
            )
        )
    if metrics.get("risk_underestimated_rate", 0) >= 0.25:
        rate = float(metrics["risk_underestimated_rate"])
        recommendations.append(
            _recommendation(
                policy="planning_risk_policy",
                current_value=policy_snapshot.risk_level_thresholds,
                suggested_value={"review": ["risk weights", "sensitive area weights", "destructive operation weights"]},
                direction="increase",
                severity=_severity(rate, 0.25, 0.40),
                confidence=_confidence(sample, rate, 0.25, metrics.get("planning_failure_rate")),
                evidence={"sample_size": sample, "observed_rate": rate, "threshold": 0.25},
                reason_codes=["risk_underestimated"],
            )
        )
    if metrics.get("risk_overestimated_rate", 0) >= 0.35:
        rate = float(metrics["risk_overestimated_rate"])
        recommendations.append(
            _recommendation(
                policy="planning_risk_policy",
                current_value=policy_snapshot.risk_level_thresholds,
                suggested_value={"review": "risk thresholds and sensitive area weights"},
                direction="review",
                severity=_severity(rate, 0.35, 0.55),
                confidence=_confidence(sample, rate, 0.35, None),
                evidence={"sample_size": sample, "observed_rate": rate, "threshold": 0.35},
                reason_codes=["risk_overestimated"],
            )
        )
    if (
        metrics.get("approval_required_rate", 0) >= 0.35
        and metrics.get("risk_overestimated_rate", 0) >= 0.30
        and metrics.get("clean_success_rate", 0) >= 0.60
    ):
        rate = float(metrics["approval_required_rate"])
        recommendations.append(
            _recommendation(
                policy="approval_policy_review",
                current_value=policy_snapshot.risk_approval_sensitive_areas,
                suggested_value={"review": "approval triggers; do not remove approvals automatically"},
                direction="review",
                severity="medium",
                confidence=_confidence(sample, rate, 0.35, None),
                evidence={
                    "sample_size": sample,
                    "approval_required_rate": rate,
                    "risk_overestimated_rate": metrics.get("risk_overestimated_rate"),
                    "clean_success_rate": metrics.get("clean_success_rate"),
                },
                reason_codes=["approvals_frequent", "risk_overestimated", "clean_outcomes_high"],
            )
        )
    if (
        metrics.get("quality_gate_false_positive_rate", 0) >= 0.20
        and metrics.get("refinement_effectiveness_rate", 1.0) < 0.50
    ):
        rate = float(metrics["quality_gate_false_positive_rate"])
        recommendations.append(
            _recommendation(
                policy="quality_gate_threshold",
                current_value={"weak_threshold": policy_snapshot.quality_gate_weak_threshold},
                suggested_value={"suggested_adjustment": -5},
                direction="decrease",
                severity=_severity(rate, 0.20, 0.35),
                confidence=_confidence(sample, rate, 0.20, None),
                evidence={
                    "sample_size": sample,
                    "observed_rate": rate,
                    "threshold": 0.20,
                    "refinement_effectiveness_rate": metrics.get("refinement_effectiveness_rate"),
                },
                reason_codes=["quality_gate_false_positives_high", "refinement_effectiveness_low"],
            )
        )
    if metrics.get("quality_gate_false_negative_rate", 0) >= 0.15:
        rate = float(metrics["quality_gate_false_negative_rate"])
        recommendations.append(
            _recommendation(
                policy="quality_gate_threshold",
                current_value={
                    "weak_threshold": policy_snapshot.quality_gate_weak_threshold,
                    "dimension_minimums": policy_snapshot.quality_gate_dimension_minimums,
                },
                suggested_value={"review": "increase threshold or dimension minimums"},
                direction="increase",
                severity=_severity(rate, 0.15, 0.30),
                confidence=_confidence(sample, rate, 0.15, None),
                evidence={"sample_size": sample, "observed_rate": rate, "threshold": 0.15},
                reason_codes=["quality_gate_false_negatives_high"],
            )
        )
    if (
        int(metrics.get("refinement_used_count") or 0) >= max(5, minimum_sample_size // 4)
        and metrics.get("refinement_effectiveness_rate", 1.0) < 0.50
    ):
        rate = float(metrics.get("refinement_effectiveness_rate") or 0)
        recommendations.append(
            _recommendation(
                policy="quality_refinement_strategy",
                current_value="current_guidance",
                suggested_value={"review": "quality refinement guidance and stopping criteria"},
                direction="review",
                severity="medium",
                confidence=_confidence(sample, 1 - rate, 0.50, None),
                evidence={
                    "sample_size": sample,
                    "refinement_used_count": metrics.get("refinement_used_count"),
                    "refinement_effectiveness_rate": rate,
                    "threshold": 0.50,
                },
                reason_codes=["refinement_effectiveness_low"],
            )
        )
    return recommendations


def _segment_recommendations(
    segments: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    policy_snapshot: PlannerPolicySnapshot,
    segment_min_sample_size: int,
    global_metrics: Mapping[str, Any],
) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    for segment_name, buckets in segments.items():
        for segment_value, metrics in buckets.items():
            sample = int(metrics.get("sample_size") or 0)
            if sample < segment_min_sample_size:
                continue
            segment = f"{segment_name}:{segment_value}"
            if (
                metrics.get("overconfident_rate", 0) >= 0.30
                and (metrics.get("average_confidence_absolute_error") or 0) >= 0.15
                and global_metrics.get("overconfident_rate", 0) < 0.30
            ):
                rate = float(metrics["overconfident_rate"])
                recommendations.append(
                    _recommendation(
                        policy="planning_decision_confidence",
                        current_value="current_formula",
                        suggested_value={"review": f"confidence penalties for {segment}"},
                        direction="decrease",
                        severity=_severity(rate, 0.30, 0.50),
                        confidence=_confidence(sample, rate, 0.30, metrics.get("average_confidence_absolute_error")),
                        evidence={
                            "segment": segment,
                            "sample_size": sample,
                            "observed_rate": rate,
                            "threshold": 0.30,
                            "global_rate": global_metrics.get("overconfident_rate"),
                        },
                        reason_codes=["segment_confidence_overestimated"],
                        segment=segment,
                    )
                )
            if (
                metrics.get("quality_gate_false_negative_rate", 0) >= 0.20
                and global_metrics.get("quality_gate_false_negative_rate", 0) < 0.15
            ):
                rate = float(metrics["quality_gate_false_negative_rate"])
                recommendations.append(
                    _recommendation(
                        policy="quality_dimension_threshold",
                        current_value=policy_snapshot.quality_gate_dimension_minimums,
                        suggested_value={"review": f"dimension thresholds for {segment}"},
                        direction="increase",
                        severity=_severity(rate, 0.20, 0.35),
                        confidence=_confidence(sample, rate, 0.20, None),
                        evidence={
                            "segment": segment,
                            "sample_size": sample,
                            "observed_rate": rate,
                            "threshold": 0.20,
                            "global_rate": global_metrics.get("quality_gate_false_negative_rate"),
                        },
                        reason_codes=["segment_quality_gate_false_negatives_high"],
                        segment=segment,
                    )
                )
    return recommendations


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _trend(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = [
        (timestamp, record)
        for record in records
        if (timestamp := _parse_time(record.get("created_at") or record.get("updated_at"))) is not None
    ]
    if len(ordered) < 4:
        return {"status": "insufficient_data"}
    ordered.sort(key=lambda item: item[0])
    midpoint = len(ordered) // 2
    previous = [item[1] for item in ordered[:midpoint]]
    recent = [item[1] for item in ordered[midpoint:]]

    def classify(previous_value: float | None, recent_value: float | None, *, lower_is_better: bool) -> str:
        if previous_value is None or recent_value is None:
            return "unknown"
        delta = recent_value - previous_value
        if abs(delta) < 0.03:
            return "stable"
        improving = delta < 0 if lower_is_better else delta > 0
        return "improving" if improving else "degrading"

    previous_metrics = _metric_bundle(previous)
    recent_metrics = _metric_bundle(recent)
    return {
        "status": "ready",
        "previous_sample_size": len(previous),
        "recent_sample_size": len(recent),
        "quality_absolute_error": classify(
            previous_metrics.get("average_quality_absolute_error"),
            recent_metrics.get("average_quality_absolute_error"),
            lower_is_better=True,
        ),
        "confidence_absolute_error": classify(
            previous_metrics.get("average_confidence_absolute_error"),
            recent_metrics.get("average_confidence_absolute_error"),
            lower_is_better=True,
        ),
        "risk_alignment": classify(
            previous_metrics.get("risk_aligned_rate"),
            recent_metrics.get("risk_aligned_rate"),
            lower_is_better=False,
        ),
        "refinement_effectiveness": classify(
            previous_metrics.get("refinement_effectiveness_rate"),
            recent_metrics.get("refinement_effectiveness_rate"),
            lower_is_better=False,
        ),
    }


def analyze_planner_calibration(
    records: Iterable[Mapping[str, Any]],
    policy_snapshot: PlannerPolicySnapshot | None = None,
    *,
    compatible_version: str = PLANNING_EVALUATION_VERSION,
    minimum_sample_size: int = 20,
    segment_min_sample_size: int = 10,
) -> dict[str, Any]:
    policy = policy_snapshot or PlannerPolicySnapshot()
    compatible, version_counts = _compatible_records(records, compatible_version)
    metrics = _metric_bundle(compatible)
    segments = _segments(compatible)
    status = "ready" if metrics["sample_size"] >= minimum_sample_size else "insufficient_data"
    recommendations: list[dict[str, Any]] = []
    if status == "ready":
        recommendations.extend(_global_recommendations(metrics, policy, minimum_sample_size))
        recommendations.extend(
            _segment_recommendations(
                segments,
                policy_snapshot=policy,
                segment_min_sample_size=segment_min_sample_size,
                global_metrics=metrics,
            )
        )
    deduped: dict[str, dict[str, Any]] = {}
    for recommendation in recommendations:
        deduped.setdefault(recommendation["recommendation_id"], recommendation)
    return {
        "version": PLANNER_CALIBRATION_ANALYTICS_VERSION,
        "compatible_evaluation_version": compatible_version,
        "sample_size": metrics["sample_size"],
        "minimum_sample_size": minimum_sample_size,
        "segment_min_sample_size": segment_min_sample_size,
        "status": status,
        "metrics": metrics,
        "segments": segments,
        "trend": _trend(compatible),
        "policy_snapshot": policy.model_dump(),
        "version_breakdown": dict(sorted(version_counts.items())),
        "recommendations": list(deduped.values()),
    }
