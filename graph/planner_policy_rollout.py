from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Iterable, Mapping


POLICY_ROLLOUT_VERSION = "7.12-v1"
POLICY_ROLLOUT_ASSIGNMENT_ALGORITHM_VERSION = "stable-sha256-mod100-v1"
POLICY_ROLLOUT_GUARDRAIL_VERSION = "7.12-default-guardrails-v1"
DEFAULT_POLICY_ROLLOUT_STAGES = [10, 25, 50, 100]
DEFAULT_MINIMUM_BASELINE_SAMPLE_SIZE = 20
DEFAULT_MINIMUM_STAGE_SAMPLE_SIZE = 10

ACTIVE_ROLLOUT_STATUSES = {"prepared", "canary", "expanding", "paused", "degraded", "observing"}
TERMINAL_ROLLOUT_STATUSES = {"completed", "rolled_back", "failed"}
INFRASTRUCTURE_FAILURE_TYPES = {
    "mcp_timeout",
    "mcp_error",
    "infrastructure_failed",
    "testing_infrastructure_failed",
    "filesystem_unavailable",
    "external_provider_outage",
}


def parse_rollout_stages(value: str | None = None) -> list[int]:
    raw = value if value is not None else os.getenv("POLICY_ROLLOUT_STAGES")
    if not raw:
        return list(DEFAULT_POLICY_ROLLOUT_STAGES)
    stages: list[int] = []
    for piece in raw.split(","):
        stripped = piece.strip()
        if not stripped:
            continue
        try:
            stage = int(stripped)
        except ValueError as exc:
            raise ValueError("planner_policy_rollout_invalid_stage") from exc
        if stage <= 0 or stage > 100:
            raise ValueError("planner_policy_rollout_invalid_stage")
        stages.append(stage)
    if not stages or sorted(set(stages)) != stages or stages[-1] != 100:
        raise ValueError("planner_policy_rollout_invalid_stage")
    return stages


def stable_rollout_bucket(*, workflow_id: str, policy_key: str, rollout_id: str) -> int:
    payload = f"{workflow_id}\0{policy_key}\0{rollout_id}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return int(digest[:16], 16) % 100


def is_workflow_in_rollout(*, workflow_id: str, policy_key: str, rollout_id: str, percentage: int) -> bool:
    return stable_rollout_bucket(workflow_id=workflow_id, policy_key=policy_key, rollout_id=rollout_id) < percentage


def rollout_fingerprint(
    *,
    application_fingerprint: str,
    policy_key: str,
    scope: str,
    previous_value: Any,
    target_value: Any,
    stages: list[int],
) -> str:
    payload = {
        "application_fingerprint": application_fingerprint,
        "assignment_algorithm_version": POLICY_ROLLOUT_ASSIGNMENT_ALGORITHM_VERSION,
        "guardrail_version": POLICY_ROLLOUT_GUARDRAIL_VERSION,
        "policy_key": policy_key,
        "previous_value": previous_value,
        "rollout_version": POLICY_ROLLOUT_VERSION,
        "scope": scope,
        "stages": stages,
        "target_value": target_value,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: Iterable[Any]) -> float | None:
    numbers = [number for value in values if (number := _as_float(value)) is not None]
    return None if not numbers else round(sum(numbers) / len(numbers), 4)


def _rate(count: int, total: int) -> float:
    return 0.0 if total <= 0 else round(count / total, 4)


def _evaluation(record: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = record.get("planning_evaluation")
    if not isinstance(raw, Mapping):
        raw = record.get("evaluation")
    return raw if isinstance(raw, Mapping) else {}


def is_infrastructure_failure(record: Mapping[str, Any]) -> bool:
    evaluation = _evaluation(record)
    failure_type = str(
        record.get("failure_type")
        or evaluation.get("failure_type")
        or evaluation.get("failure_category")
        or ""
    )
    terminal_status = str(record.get("terminal_status") or evaluation.get("terminal_status") or "")
    return failure_type in INFRASTRUCTURE_FAILURE_TYPES or terminal_status == "infrastructure_failed"


def rollout_assignment_for(record: Mapping[str, Any], rollout_id: str) -> dict[str, Any] | None:
    raw = record.get("planner_policy_rollout_assignments") or record.get("policy_assignments")
    assignments: Iterable[Any]
    if isinstance(raw, Mapping):
        assignments = raw.values()
    elif isinstance(raw, list):
        assignments = raw
    else:
        return None
    for assignment in assignments:
        if isinstance(assignment, Mapping) and assignment.get("rollout_id") == rollout_id:
            return dict(assignment)
    return None


def rollout_metrics(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    items = [dict(record) for record in records]
    policy_records = [record for record in items if not is_infrastructure_failure(record)]
    infrastructure_count = len(items) - len(policy_records)
    evaluations = [_evaluation(record) for record in policy_records]
    sample = len(policy_records)
    failed = sum(1 for evaluation in evaluations if evaluation.get("outcome") == "failed")
    successful = sum(1 for evaluation in evaluations if evaluation.get("outcome") == "successful")
    repair = sum(1 for evaluation in evaluations if evaluation.get("successful_with_repair") is True or evaluation.get("repair_used") is True)
    refinement_used = [evaluation for evaluation in evaluations if isinstance(evaluation.get("refinement"), Mapping)]
    refinement_effective = sum(1 for evaluation in refinement_used if (evaluation.get("refinement") or {}).get("effective") is True)
    quality_gate = [evaluation.get("quality_gate") if isinstance(evaluation.get("quality_gate"), Mapping) else {} for evaluation in evaluations]
    risk_calibration = [evaluation.get("risk_calibration") if isinstance(evaluation.get("risk_calibration"), Mapping) else {} for evaluation in evaluations]
    confidence = [evaluation.get("confidence_calibration") if isinstance(evaluation.get("confidence_calibration"), Mapping) else {} for evaluation in evaluations]
    return {
        "sample_size": len(items),
        "policy_sample_size": sample,
        "infrastructure_sample_size": infrastructure_count,
        "success_rate": _rate(successful, sample),
        "failure_rate": _rate(failed, sample),
        "repair_rate": _rate(repair, sample),
        "average_quality_score": _avg(evaluation.get("quality_score") or ((evaluation.get("quality_prediction") or {}).get("observed") if isinstance(evaluation.get("quality_prediction"), Mapping) else None) for evaluation in evaluations),
        "average_outcome_score": _avg(evaluation.get("outcome_score") for evaluation in evaluations),
        "average_confidence_absolute_error": _avg(evaluation.get("confidence_absolute_error") or item.get("absolute_error") for evaluation, item in zip(evaluations, confidence, strict=False)),
        "risk_underestimated_rate": _rate(sum(1 for evaluation, item in zip(evaluations, risk_calibration, strict=False) if evaluation.get("risk_calibration") == "underestimated" or item.get("label") == "underestimated"), sample),
        "quality_gate_false_positive_rate": _rate(sum(1 for evaluation, item in zip(evaluations, quality_gate, strict=False) if evaluation.get("quality_gate_error") == "false_positive" or item.get("false_positive") is True), sample),
        "quality_gate_false_negative_rate": _rate(sum(1 for evaluation, item in zip(evaluations, quality_gate, strict=False) if evaluation.get("quality_gate_error") == "false_negative" or item.get("false_negative") is True), sample),
        "refinement_effectiveness_rate": _rate(refinement_effective, len(refinement_used)),
    }


def split_rollout_records(records: Iterable[Mapping[str, Any]], rollout_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    treatment: list[dict[str, Any]] = []
    control: list[dict[str, Any]] = []
    for record in records:
        assignment = rollout_assignment_for(record, rollout_id)
        if assignment is None:
            continue
        if assignment.get("treatment") is True:
            treatment.append(dict(record))
        else:
            control.append(dict(record))
    return treatment, control


def _delta(current: Mapping[str, Any], baseline: Mapping[str, Any], key: str) -> float:
    left = _as_float(current.get(key)) or 0.0
    right = _as_float(baseline.get(key)) or 0.0
    return round(left - right, 4)


def evaluate_rollout_health(
    *,
    baseline_metrics: Mapping[str, Any],
    treatment_metrics: Mapping[str, Any],
    control_metrics: Mapping[str, Any] | None = None,
    minimum_stage_sample_size: int = DEFAULT_MINIMUM_STAGE_SAMPLE_SIZE,
) -> dict[str, Any]:
    policy_sample_size = int(treatment_metrics.get("policy_sample_size") or 0)
    deltas = {
        "failure_rate_delta": _delta(treatment_metrics, baseline_metrics, "failure_rate"),
        "repair_rate_delta": _delta(treatment_metrics, baseline_metrics, "repair_rate"),
        "quality_gate_false_negative_delta": _delta(treatment_metrics, baseline_metrics, "quality_gate_false_negative_rate"),
        "risk_underestimated_delta": _delta(treatment_metrics, baseline_metrics, "risk_underestimated_rate"),
        "confidence_absolute_error_delta": _delta(treatment_metrics, baseline_metrics, "average_confidence_absolute_error"),
    }
    if policy_sample_size < minimum_stage_sample_size:
        return {
            "health_status": "observing",
            "health_score": 50,
            "sample_sufficient": False,
            "delta_metrics": deltas,
            "reason_codes": ["insufficient_stage_sample"],
        }
    reason_codes: list[str] = []
    health_status = "healthy"
    score = 100
    if deltas["failure_rate_delta"] > 0.10:
        health_status = "critical"
        score = 0
        reason_codes.append("failure_rate_guardrail")
    elif (
        deltas["repair_rate_delta"] > 0.15
        or deltas["quality_gate_false_negative_delta"] > 0.10
        or deltas["risk_underestimated_delta"] > 0.10
        or deltas["confidence_absolute_error_delta"] > 0.15
    ):
        health_status = "degraded"
        score = 25
        reason_codes.append("degradation_guardrail")
    elif deltas["confidence_absolute_error_delta"] > 0.10:
        health_status = "warning"
        score = 60
        reason_codes.append("confidence_warning_guardrail")
    if control_metrics:
        reason_codes.append("control_compared")
    return {
        "health_status": health_status,
        "health_score": score,
        "sample_sufficient": True,
        "delta_metrics": deltas,
        "reason_codes": reason_codes or ["within_guardrails"],
    }
