from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any


AGENT_RECOMMENDATION_VERSION = os.getenv("AGENT_RECOMMENDATION_VERSION", "8.5-v1")
DEFAULT_MIN_SAMPLE_SIZE = int(os.getenv("AGENT_RECOMMENDATION_MIN_SAMPLE_SIZE", "20"))
DEFAULT_SEGMENT_MIN_SAMPLE_SIZE = int(os.getenv("AGENT_RECOMMENDATION_SEGMENT_MIN_SAMPLE_SIZE", "10"))

AGENTS = ("planner", "developer", "repair", "qa")
EXCLUDED_ROOT_CAUSES = {"infrastructure", "external", "user", "policy"}
RECOMMENDATION_TYPES = {
    "improve_prompt_guidance",
    "improve_validation",
    "improve_context_grounding",
    "improve_task_specificity",
    "improve_failure_classification",
    "improve_repair_strategy",
    "reduce_retry_dependency",
    "review_model_configuration",
    "review_agent_policy",
}


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _rate(count: int, total: int) -> float:
    return 0.0 if total <= 0 else round(count / total, 4)


def _avg(values: Iterable[Any]) -> float | None:
    numbers = [number for value in values if (number := _as_float(value)) is not None]
    return None if not numbers else round(sum(numbers) / len(numbers), 4)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _framework(record: Mapping[str, Any]) -> str:
    explicit = record.get("framework")
    if explicit:
        return str(explicit).strip().casefold()
    planning = _mapping(record.get("planning"))
    analysis = _mapping(planning.get("analysis"))
    return str(planning.get("framework") or analysis.get("framework") or "unknown").strip().casefold() or "unknown"


def _performance(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("agent_performance_evaluations")
    if not isinstance(raw, Mapping):
        raw = record.get("agent_performance")
    return _mapping(raw)


def _rca(record: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(record.get("failure_attribution"))


def _planning_eval(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("planning_evaluation")
    if isinstance(raw, Mapping):
        return dict(raw)
    planning = _mapping(record.get("planning"))
    return _mapping(planning.get("evaluation"))


def _hybrid_eval(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("planning_hybrid_evaluation")
    if isinstance(raw, Mapping):
        return dict(raw)
    planning = _mapping(record.get("planning"))
    return _mapping(planning.get("hybrid_evaluation"))


def _judge_result(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("planning_judge_result")
    if isinstance(raw, Mapping):
        return dict(raw)
    planning = _mapping(record.get("planning"))
    judge = _mapping(planning.get("judge"))
    return _mapping(judge.get("result"))


def _created_at(record: Mapping[str, Any]) -> datetime | None:
    value = record.get("created_at") or record.get("updated_at")
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _root_cause(record: Mapping[str, Any]) -> str | None:
    cause = _rca(record).get("root_cause")
    return str(cause) if cause else None


def _attributable_record(record: Mapping[str, Any]) -> bool:
    cause = _root_cause(record)
    return cause not in EXCLUDED_ROOT_CAUSES


def _reason_counter(records: Iterable[Mapping[str, Any]], agent: str) -> Counter[str]:
    counter: Counter[str] = Counter()
    for record in records:
        rca = _rca(record)
        if rca.get("root_cause") == agent:
            counter.update(str(code) for code in _sequence(rca.get("reason_codes")))
    return counter


def _contribution_count(records: Iterable[Mapping[str, Any]], agent: str, contribution: str) -> int:
    count = 0
    for record in records:
        for item in _sequence(_rca(record).get("contributors")):
            if isinstance(item, Mapping) and item.get("source") == agent and item.get("contribution") == contribution:
                count += 1
    return count


def _agent_metric(records: list[Mapping[str, Any]], agent: str, metric: str) -> list[Any]:
    values: list[Any] = []
    for record in records:
        item = _mapping(_performance(record).get(agent))
        metrics = _mapping(item.get("metrics"))
        if metric in metrics:
            values.append(metrics.get(metric))
    return values


def _agent_scores(records: list[Mapping[str, Any]], agent: str) -> list[Any]:
    return [_mapping(_performance(record).get(agent)).get("score") for record in records]


def _specific_metrics(records: list[Mapping[str, Any]], agent: str, root_cause_count: int, attributable: int) -> dict[str, Any]:
    if agent == "developer":
        first_pass_values = [value for value in _agent_metric(records, agent, "first_pass_success") if isinstance(value, bool)]
        required_repair = [value for value in _agent_metric(records, agent, "required_repair") if isinstance(value, bool)]
        validation_failures = sum(
            1
            for record in records
            if _as_int(_mapping(_mapping(_performance(record).get(agent)).get("metrics")).get("validation_error_count")) > 0
            or "validation" in " ".join(map(str, _mapping(_performance(record).get(agent)).get("reason_codes") or [])).casefold()
        )
        return {
            "first_pass_success_rate": _rate(sum(1 for value in first_pass_values if value), len(first_pass_values)),
            "repair_required_rate": _rate(sum(1 for value in required_repair if value), len(required_repair)),
            "implementation_validation_failure_rate": _rate(validation_failures, max(1, attributable)),
            "repair_resolved_developer_defects": _contribution_count(records, "repair", "resolved"),
        }
    if agent == "repair":
        repair_records = [
            record
            for record in records
            if _mapping(_performance(record).get("repair")).get("status") != "not_applicable"
            or _as_int(record.get("repair_attempts") or _mapping(record.get("testing")).get("repair_attempts")) > 0
        ]
        repair_success_values = [value for value in _agent_metric(repair_records, "repair", "repair_success") if isinstance(value, bool)]
        attempts = [
            _as_int(_mapping(_mapping(_performance(record).get("repair")).get("metrics")).get("repair_attempts") or record.get("repair_attempts") or _mapping(record.get("testing")).get("repair_attempts"))
            for record in repair_records
        ]
        failed = _contribution_count(records, "repair", "failed_to_recover")
        introduced = _contribution_count(records, "repair", "introduced_failure")
        return {
            "repair_execution_count": len(repair_records),
            "repair_success_rate": _rate(sum(1 for value in repair_success_values if value), len(repair_success_values)),
            "average_repair_attempts": _avg(attempts),
            "failed_to_recover_rate": _rate(failed, max(1, len(repair_records))),
            "repair_introduced_failure_count": introduced,
        }
    if agent == "qa":
        detected = _contribution_count(records, "qa", "detected")
        qa_failures = sum(1 for record in records if _root_cause(record) == "qa")
        unclassified = sum(
            1
            for record in records
            if _root_cause(record) == "qa"
            and any("classification" in str(code) or "unknown" in str(code) for code in _sequence(_rca(record).get("reason_codes")))
        )
        infra_excluded = sum(1 for record in records if _root_cause(record) in {"infrastructure", "external"})
        return {
            "qa_detected_count": detected,
            "attributable_failure_rate": _rate(qa_failures, max(1, attributable)),
            "failure_classification_missing_rate": _rate(unclassified, max(1, qa_failures)),
            "infrastructure_exclusion_rate": _rate(infra_excluded, len(records)),
        }
    refinement = sum(1 for record in records if _mapping(_planning_eval(record).get("refinement")).get("attempts", 0))
    gate_failures = sum(1 for record in records if _mapping(_planning_eval(record).get("quality_gate")).get("false_negative") or _mapping(_planning_eval(record).get("quality_gate")).get("false_positive"))
    judge_alignment = sum(
        1
        for record in records
        if "requirement_alignment" in json.dumps(_judge_result(record), sort_keys=True).casefold()
        or "requirement_alignment" in json.dumps(_mapping(_hybrid_eval(record).get("flags")), sort_keys=True).casefold()
    )
    hybrid_review = sum(1 for record in records if _hybrid_eval(record).get("recommendation") == "review_recommended")
    return {
        "planning_failure_rate": _rate(root_cause_count, max(1, attributable)),
        "quality_refinement_rate": _rate(refinement, len(records)),
        "quality_gate_failure_rate": _rate(gate_failures, len(records)),
        "judge_alignment_concern_rate": _rate(judge_alignment, len(records)),
        "hybrid_review_recommended_rate": _rate(hybrid_review, len(records)),
    }


def _metrics(records: list[Mapping[str, Any]], agent: str) -> dict[str, Any]:
    sample = len(records)
    attributable = sum(1 for record in records if _attributable_record(record))
    root_causes = sum(1 for record in records if _root_cause(record) == agent)
    scores = _agent_scores(records, agent)
    statuses = [_mapping(_performance(record).get(agent)).get("level") for record in records]
    retry_values = [
        _as_int(_mapping(_mapping(_performance(record).get(agent)).get("metrics")).get("retry_count") or _mapping(_mapping(_performance(record).get(agent)).get("metrics")).get("attempt_count"))
        for record in records
    ]
    metrics = {
        "sample_size": sample,
        "attributable_workflows": attributable,
        "average_score": _avg(scores),
        "good_or_better_rate": _rate(sum(1 for value in statuses if value in {"good", "excellent"}), len(statuses)),
        "root_cause_count": root_causes,
        "root_cause_rate": _rate(root_causes, attributable),
        "retry_rate": _rate(sum(1 for value in retry_values if value > 1), len(retry_values)),
        "excluded_infrastructure_count": sum(1 for record in records if _root_cause(record) in {"infrastructure", "external"}),
        "excluded_user_count": sum(1 for record in records if _root_cause(record) == "user"),
        "excluded_policy_count": sum(1 for record in records if _root_cause(record) == "policy"),
    }
    metrics.update(_specific_metrics(records, agent, root_causes, attributable))
    return metrics


def _trend(records: list[Mapping[str, Any]], agent: str) -> dict[str, Any]:
    ordered = [(timestamp, record) for record in records if (timestamp := _created_at(record)) is not None]
    if len(ordered) < 8:
        return {"status": "insufficient_data"}
    ordered.sort(key=lambda item: item[0])
    midpoint = len(ordered) // 2
    previous = _metrics([item[1] for item in ordered[:midpoint]], agent)
    recent = _metrics([item[1] for item in ordered[midpoint:]], agent)
    previous_rate = float(previous.get("root_cause_rate") or 0)
    recent_rate = float(recent.get("root_cause_rate") or 0)
    delta = recent_rate - previous_rate
    status = "stable" if abs(delta) < 0.03 else "improving" if delta < 0 else "degrading"
    return {
        "status": status,
        "previous_sample_size": previous["sample_size"],
        "recent_sample_size": recent["sample_size"],
        "previous_root_cause_rate": previous_rate,
        "recent_root_cause_rate": recent_rate,
    }


def _confidence(sample_size: int, magnitude: float, *, rca_support: float, corroboration: float = 0.0) -> float:
    sample_factor = min(sample_size / 80, 1.0)
    magnitude_factor = min(max(magnitude, 0), 1.0)
    value = 0.20 + 0.25 * sample_factor + 0.35 * rca_support + 0.15 * magnitude_factor + 0.05 * corroboration
    return round(min(max(value, 0.0), 0.95), 4)


def _severity(magnitude: float, sample_size: int, root_cause_rate: float) -> str:
    if sample_size >= 50 and root_cause_rate >= 0.30 and magnitude >= 0.30:
        return "high"
    if root_cause_rate >= 0.15 and magnitude >= 0.15:
        return "medium"
    if magnitude > 0:
        return "low"
    return "info"


def _fingerprint_payload(recommendation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": recommendation.get("created_from_version"),
        "target": recommendation.get("target"),
        "type": recommendation.get("type"),
        "severity": recommendation.get("severity"),
        "confidence": recommendation.get("confidence"),
        "reason_codes": recommendation.get("reason_codes") or [],
        "evidence": recommendation.get("evidence") or {},
    }


def recommendation_fingerprint(recommendation: Mapping[str, Any]) -> str:
    encoded = json.dumps(_fingerprint_payload(recommendation), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _recommendation_id(agent: str, rec_type: str, segment: str | None, reason_codes: list[str]) -> str:
    payload = {
        "version": AGENT_RECOMMENDATION_VERSION,
        "agent": agent,
        "type": rec_type,
        "segment": segment,
        "reason_codes": sorted(reason_codes),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _recommendation(
    *,
    agent: str,
    rec_type: str,
    severity: str,
    confidence: float,
    evidence: dict[str, Any],
    reason_codes: list[str],
    recommended_action: str,
    segment: str | None = None,
    summary: str | None = None,
    trend: str | None = None,
) -> dict[str, Any]:
    reason_codes = _dedupe(reason_codes)
    item = {
        "recommendation_id": _recommendation_id(agent, rec_type, segment, reason_codes),
        "agent": agent,
        "type": rec_type,
        "policy": f"agent:{agent}:{rec_type}",
        "segment": segment,
        "direction": "review",
        "severity": severity,
        "confidence": confidence,
        "summary": summary or f"Review {agent} {rec_type.replace('_', ' ')}.",
        "reason_codes": reason_codes,
        "evidence": evidence,
        "status": "recommendation_only",
        "recommended_action": recommended_action,
        "created_from_version": AGENT_RECOMMENDATION_VERSION,
        "target": {"type": "agent", "agent": agent, "segment": segment},
        "application_status": "not_applied",
        "current_value": "current_agent_configuration",
        "suggested_value": {"review": recommended_action},
        "trend": trend or "insufficient_data",
    }
    item["fingerprint"] = recommendation_fingerprint(item)
    item["recommendation_fingerprint"] = item["fingerprint"]
    item["analytics_version"] = AGENT_RECOMMENDATION_VERSION
    return item


def _planner_recommendations(metrics: Mapping[str, Any], reasons: Counter[str], trend: Mapping[str, Any]) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    sample = int(metrics["sample_size"])
    root_rate = float(metrics.get("root_cause_rate") or 0)
    mismatch_count = sum(count for code, count in reasons.items() if "target" in code or "artifact" in code or "dependency" in code or "mismatch" in code)
    mismatch_rate = _rate(mismatch_count, max(1, int(metrics.get("root_cause_count") or 0)))
    judge_rate = float(metrics.get("judge_alignment_concern_rate") or 0)
    if root_rate >= 0.15 and mismatch_rate >= 0.40:
        confidence = _confidence(sample, root_rate, rca_support=root_rate, corroboration=judge_rate)
        if judge_rate >= 0.20:
            confidence = min(0.95, round(confidence + 0.08, 4))
        recommendations.append(_recommendation(
            agent="planner",
            rec_type="improve_context_grounding",
            severity=_severity(root_rate, sample, root_rate),
            confidence=confidence,
            evidence={
                "sample_size": sample,
                "root_cause_rate": root_rate,
                "target_or_artifact_mismatch_rate": mismatch_rate,
                "judge_alignment_concern_rate": judge_rate,
            },
            reason_codes=["agent_high_root_cause_rate", "agent_context_grounding_failures"] + (["judge_corroborated_alignment_concern"] if judge_rate >= 0.20 else []),
            recommended_action="Review Planner guidance around grounding tasks in the explicit user target and requested artifacts.",
            trend=str(trend.get("status") or "insufficient_data"),
        ))
    elif root_rate >= 0.15 and float(metrics.get("quality_refinement_rate") or 0) >= 0.30:
        recommendations.append(_recommendation(
            agent="planner",
            rec_type="improve_task_specificity",
            severity=_severity(float(metrics.get("quality_refinement_rate") or 0), sample, root_rate),
            confidence=_confidence(sample, float(metrics.get("quality_refinement_rate") or 0), rca_support=root_rate),
            evidence={"sample_size": sample, "root_cause_rate": root_rate, "quality_refinement_rate": metrics.get("quality_refinement_rate")},
            reason_codes=["agent_high_root_cause_rate", "agent_quality_refinement_dependency"],
            recommended_action="Review Planner task specificity and acceptance criteria decomposition.",
            trend=str(trend.get("status") or "insufficient_data"),
        ))
    return recommendations


def _developer_recommendations(metrics: Mapping[str, Any], trend: Mapping[str, Any]) -> list[dict[str, Any]]:
    sample = int(metrics["sample_size"])
    first_pass = float(metrics.get("first_pass_success_rate") or 0)
    validation = float(metrics.get("implementation_validation_failure_rate") or 0)
    root_rate = float(metrics.get("root_cause_rate") or 0)
    repair_required = float(metrics.get("repair_required_rate") or 0)
    if first_pass < 0.65 and validation > 0.20 and root_rate > 0.15:
        return [_recommendation(
            agent="developer",
            rec_type="improve_validation",
            severity=_severity(max(1 - first_pass, validation), sample, root_rate),
            confidence=_confidence(sample, max(1 - first_pass, validation), rca_support=root_rate, corroboration=repair_required),
            evidence={
                "sample_size": sample,
                "average_score": metrics.get("average_score"),
                "root_cause_rate": root_rate,
                "first_pass_success_rate": first_pass,
                "validation_failure_rate": validation,
                "repair_required_rate": repair_required,
            },
            reason_codes=["agent_low_first_pass_rate", "agent_validation_failures", "agent_high_root_cause_rate"],
            recommended_action="Review Developer validation guidance around generated files, executable tests, and explicit target checks.",
            trend=str(trend.get("status") or "insufficient_data"),
        )]
    if root_rate > 0.15 and repair_required > 0.30:
        return [_recommendation(
            agent="developer",
            rec_type="reduce_retry_dependency",
            severity=_severity(repair_required, sample, root_rate),
            confidence=_confidence(sample, repair_required, rca_support=root_rate),
            evidence={"sample_size": sample, "root_cause_rate": root_rate, "repair_required_rate": repair_required},
            reason_codes=["agent_high_root_cause_rate", "agent_high_retry_rate"],
            recommended_action="Review Developer output checks to reduce dependence on downstream repair.",
            trend=str(trend.get("status") or "insufficient_data"),
        )]
    return []


def _repair_recommendations(metrics: Mapping[str, Any], trend: Mapping[str, Any]) -> list[dict[str, Any]]:
    sample = int(metrics["sample_size"])
    execution_count = int(metrics.get("repair_execution_count") or 0)
    if execution_count < max(3, sample // 10):
        return []
    success = float(metrics.get("repair_success_rate") or 0)
    failed = float(metrics.get("failed_to_recover_rate") or 0)
    if success < 0.60 and failed > 0.25:
        return [_recommendation(
            agent="repair",
            rec_type="improve_repair_strategy",
            severity=_severity(max(1 - success, failed), sample, float(metrics.get("root_cause_rate") or failed)),
            confidence=_confidence(sample, max(1 - success, failed), rca_support=failed),
            evidence={
                "sample_size": sample,
                "repair_execution_count": execution_count,
                "repair_success_rate": success,
                "failed_to_recover_rate": failed,
                "average_repair_attempts": metrics.get("average_repair_attempts"),
            },
            reason_codes=["agent_low_repair_success", "agent_failed_to_recover"],
            recommended_action="Review Repair strategy for diagnosis, patch scope, and stopping criteria.",
            trend=str(trend.get("status") or "insufficient_data"),
        )]
    if int(metrics.get("repair_introduced_failure_count") or 0) > 0:
        return [_recommendation(
            agent="repair",
            rec_type="improve_validation",
            severity="medium",
            confidence=_confidence(sample, _rate(int(metrics.get("repair_introduced_failure_count") or 0), execution_count), rca_support=float(metrics.get("root_cause_rate") or 0)),
            evidence={"sample_size": sample, "repair_introduced_failure_count": metrics.get("repair_introduced_failure_count"), "repair_execution_count": execution_count},
            reason_codes=["agent_validation_failures"],
            recommended_action="Review Repair validation before accepting patch output.",
            trend=str(trend.get("status") or "insufficient_data"),
        )]
    return []


def _qa_recommendations(metrics: Mapping[str, Any], trend: Mapping[str, Any]) -> list[dict[str, Any]]:
    sample = int(metrics["sample_size"])
    root_rate = float(metrics.get("root_cause_rate") or 0)
    missing = float(metrics.get("failure_classification_missing_rate") or 0)
    if root_rate > 0.10 and missing >= 0.30:
        return [_recommendation(
            agent="qa",
            rec_type="improve_failure_classification",
            severity=_severity(missing, sample, root_rate),
            confidence=_confidence(sample, missing, rca_support=root_rate),
            evidence={
                "sample_size": sample,
                "root_cause_rate": root_rate,
                "failure_classification_missing_rate": missing,
                "qa_detected_count": metrics.get("qa_detected_count"),
            },
            reason_codes=["agent_failure_classification_missing", "agent_high_root_cause_rate"],
            recommended_action="Review QA failure classification rules for actionable test defects and unknown outcomes.",
            trend=str(trend.get("status") or "insufficient_data"),
        )]
    return []


def _rules(agent: str, metrics: Mapping[str, Any], records: list[Mapping[str, Any]], trend: Mapping[str, Any]) -> list[dict[str, Any]]:
    if agent == "planner":
        return _planner_recommendations(metrics, _reason_counter(records, agent), trend)
    if agent == "developer":
        return _developer_recommendations(metrics, trend)
    if agent == "repair":
        return _repair_recommendations(metrics, trend)
    if agent == "qa":
        return _qa_recommendations(metrics, trend)
    return []


def _with_segment(item: dict[str, Any], segment: str) -> dict[str, Any]:
    cloned = dict(item)
    cloned["segment"] = segment
    cloned["target"] = {"type": "agent", "agent": item["agent"], "segment": segment}
    cloned["recommendation_id"] = _recommendation_id(
        str(item["agent"]),
        str(item["type"]),
        segment,
        list(item.get("reason_codes") or []),
    )
    cloned["evidence"] = {**dict(item.get("evidence") or {}), "segment": segment}
    cloned["fingerprint"] = recommendation_fingerprint(cloned)
    cloned["recommendation_fingerprint"] = cloned["fingerprint"]
    return cloned


def _segments(records: list[Mapping[str, Any]], agent: str, segment_min_sample_size: int) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        buckets.setdefault(_framework(record), []).append(record)
    output: dict[str, dict[str, Any]] = {}
    for framework, items in sorted(buckets.items()):
        if len(items) < segment_min_sample_size:
            continue
        segment = f"framework:{framework}"
        metrics = _metrics(items, agent)
        trend = _trend(items, agent)
        recommendations = [_with_segment(item, segment) for item in _rules(agent, metrics, items, trend)]
        output[segment] = {"metrics": metrics, "trend": trend, "recommendations": recommendations}
    return output


def analyze_agent_recommendations(
    records: Iterable[Mapping[str, Any]],
    *,
    minimum_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
    segment_min_sample_size: int = DEFAULT_SEGMENT_MIN_SAMPLE_SIZE,
    agent_filter: str | None = None,
    framework: str | None = None,
) -> dict[str, Any]:
    all_records = [dict(record) for record in records]
    if framework:
        all_records = [record for record in all_records if _framework(record) == framework.casefold()]
    agents = [agent_filter] if agent_filter else list(AGENTS)
    agent_results: dict[str, Any] = {}
    recommendations: list[dict[str, Any]] = []
    for agent in agents:
        if agent not in AGENTS:
            continue
        metrics = _metrics(all_records, agent)
        trend = _trend(all_records, agent)
        status = "ready" if metrics["sample_size"] >= minimum_sample_size else "insufficient_data"
        agent_recommendations: list[dict[str, Any]] = []
        segments = _segments(all_records, agent, segment_min_sample_size)
        if status == "ready":
            global_recommendations = _rules(agent, metrics, all_records, trend)
            segmented = [item for segment in segments.values() for item in segment["recommendations"]]
            if segmented and not global_recommendations:
                agent_recommendations.extend(segmented)
            elif segmented and global_recommendations:
                segment_root_rates = [
                    float(segment["metrics"].get("root_cause_rate") or 0)
                    for segment in segments.values()
                ]
                if segment_root_rates and max(segment_root_rates) >= 2 * max(0.01, float(metrics.get("root_cause_rate") or 0)):
                    agent_recommendations.extend(segmented)
                else:
                    agent_recommendations.extend(global_recommendations)
                    agent_recommendations.extend(segmented)
            else:
                agent_recommendations.extend(global_recommendations)
        if trend.get("status") == "improving":
            for recommendation in agent_recommendations:
                if recommendation["severity"] == "high":
                    recommendation["severity"] = "medium"
                elif recommendation["severity"] == "medium":
                    recommendation["severity"] = "low"
                recommendation["confidence"] = round(max(0.1, float(recommendation["confidence"]) - 0.10), 4)
                recommendation["fingerprint"] = recommendation_fingerprint(recommendation)
                recommendation["recommendation_fingerprint"] = recommendation["fingerprint"]
        deduped: dict[str, dict[str, Any]] = {}
        for recommendation in agent_recommendations:
            deduped.setdefault(recommendation["recommendation_id"], recommendation)
        agent_results[agent] = {
            "status": status,
            "sample_size": metrics["sample_size"],
            "metrics": metrics,
            "trend": trend,
            "segments": segments,
            "recommendations": list(deduped.values()),
        }
        recommendations.extend(agent_results[agent]["recommendations"])
    recommendations.sort(key=lambda item: (str(item["agent"]), str(item.get("segment") or ""), str(item["type"])))
    return {
        "version": AGENT_RECOMMENDATION_VERSION,
        "status": "ready" if any(agent_results[agent]["status"] == "ready" for agent in agent_results) else "insufficient_data",
        "sample_size": len(all_records),
        "minimum_sample_size": minimum_sample_size,
        "segment_min_sample_size": segment_min_sample_size,
        "agents": agent_results,
        "recommendations": recommendations,
    }
