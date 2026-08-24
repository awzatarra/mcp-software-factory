from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

from graph.state import SoftwareFactoryState
from streaming import EventStatus, WorkflowEventType, emit_workflow_event


PLANNING_HYBRID_EVALUATION_VERSION = "8.2-v1"
DEFAULT_DETERMINISTIC_WEIGHT = 0.70
DEFAULT_JUDGE_WEIGHT = 0.30
DEFAULT_MAJOR_DISAGREEMENT_THRESHOLD = 25

DIMENSION_MAP = {
    "requirement_coverage": "requirement_alignment",
    "plan_completeness": "completeness",
    "task_clarity": "task_clarity",
    "dependency_coherence": "technical_coherence",
}


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name.lower()}_invalid") from exc


def planner_hybrid_version() -> str:
    return os.getenv("PLANNER_HYBRID_VERSION", PLANNING_HYBRID_EVALUATION_VERSION).strip() or PLANNING_HYBRID_EVALUATION_VERSION


def planner_hybrid_weights() -> tuple[float, float]:
    deterministic = _env_float("PLANNER_HYBRID_DETERMINISTIC_WEIGHT", DEFAULT_DETERMINISTIC_WEIGHT)
    judge = _env_float("PLANNER_HYBRID_JUDGE_WEIGHT", DEFAULT_JUDGE_WEIGHT)
    if deterministic < 0 or judge < 0 or deterministic + judge <= 0:
        raise ValueError("planner_hybrid_weights_invalid")
    total = deterministic + judge
    return deterministic / total, judge / total


def planner_hybrid_major_threshold() -> float:
    threshold = _env_float("PLANNER_HYBRID_MAJOR_DISAGREEMENT_THRESHOLD", DEFAULT_MAJOR_DISAGREEMENT_THRESHOLD)
    if threshold <= 0:
        raise ValueError("planner_hybrid_threshold_invalid")
    return threshold


def agreement_label(delta: float | None, *, judge_available: bool, major_threshold: float | None = None) -> str:
    if not judge_available or delta is None:
        return "judge_unavailable"
    absolute = abs(delta)
    if absolute <= 10:
        return "aligned"
    if absolute < (major_threshold or DEFAULT_MAJOR_DISAGREEMENT_THRESHOLD):
        return "minor_disagreement"
    return "major_disagreement"


def calibration_label(error: float) -> str:
    absolute = abs(error)
    if absolute <= 0.10:
        return "well_calibrated"
    if absolute <= 0.20:
        return "slightly_overconfident" if error > 0 else "slightly_underconfident"
    return "overconfident" if error > 0 else "underconfident"


def _deterministic_dimensions(state: Mapping[str, Any]) -> Mapping[str, Any]:
    value = state.get("planning_quality_dimensions")
    return value if isinstance(value, Mapping) else {}


def _judge_dimensions(judge_result: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(judge_result, Mapping):
        return {}
    value = judge_result.get("dimensions")
    return value if isinstance(value, Mapping) else {}


def _dimension_combined(deterministic: float | None, judge: float | None, det_weight: float, judge_weight: float) -> float | None:
    if deterministic is None and judge is None:
        return None
    if judge is None:
        return round(_clamp(float(deterministic or 0), 0, 100), 4)
    if deterministic is None:
        return round(_clamp(judge, 0, 100), 4)
    return round(_clamp(deterministic * det_weight + judge * judge_weight, 0, 100), 4)


def build_planning_hybrid_evaluation(state: Mapping[str, Any]) -> dict[str, Any]:
    version = planner_hybrid_version()
    det_weight, judge_weight = planner_hybrid_weights()
    major_threshold = planner_hybrid_major_threshold()
    deterministic_score = _float(state.get("planning_quality_score"))
    deterministic_confidence = _float(state.get("planning_decision_confidence"))
    judge_completed = state.get("planning_judge_status") == "completed" and isinstance(state.get("planning_judge_result"), Mapping)
    judge_result = state.get("planning_judge_result") if judge_completed else None
    judge_score = _float(judge_result.get("overall_score")) if isinstance(judge_result, Mapping) else None
    judge_confidence = _float(judge_result.get("confidence")) if isinstance(judge_result, Mapping) else None
    source = "deterministic_and_judge" if judge_score is not None else "deterministic_only"

    if deterministic_score is None:
        score = judge_score if judge_score is not None else 0.0
    elif judge_score is None:
        score = deterministic_score
    else:
        score = deterministic_score * det_weight + judge_score * judge_weight
    score = round(_clamp(float(score), 0, 100), 4)

    if deterministic_confidence is None and judge_confidence is None:
        confidence = None
    elif judge_confidence is None:
        confidence = _clamp(float(deterministic_confidence or 0), 0, 1)
    elif deterministic_confidence is None:
        confidence = _clamp(judge_confidence, 0, 1)
    else:
        confidence = _clamp(deterministic_confidence * det_weight + judge_confidence * judge_weight, 0, 1)

    disagreement = state.get("planning_judge_disagreement") if isinstance(state.get("planning_judge_disagreement"), Mapping) else {}
    delta = _float(disagreement.get("delta"))
    agreement = agreement_label(delta, judge_available=judge_score is not None, major_threshold=major_threshold)
    if confidence is not None and delta is not None:
        confidence = _clamp(confidence - min(abs(delta) / 100, 0.4), 0, 1)

    det_dimensions = _deterministic_dimensions(state)
    judge_dimensions = _judge_dimensions(judge_result if isinstance(judge_result, Mapping) else None)
    dimensions: dict[str, Any] = {}
    for deterministic_key, judge_key in DIMENSION_MAP.items():
        det_value = _float(det_dimensions.get(deterministic_key))
        judge_value = _float(judge_dimensions.get(judge_key))
        dimensions[deterministic_key] = {
            "deterministic": det_value,
            "judge": judge_value,
            "judge_dimension": judge_key,
            "combined": _dimension_combined(det_value, judge_value, det_weight, judge_weight),
        }
    if "complexity_control" in judge_dimensions:
        dimensions["complexity_control"] = {
            "deterministic": None,
            "judge": _float(judge_dimensions.get("complexity_control")),
            "combined": _float(judge_dimensions.get("complexity_control")),
            "source": "semantic_only",
        }

    flags: list[str] = []
    if judge_score is None:
        flags.append("judge_unavailable")
    if agreement == "major_disagreement":
        flags.append("hybrid_major_disagreement")
    if _float(judge_dimensions.get("complexity_control")) is not None and float(judge_dimensions["complexity_control"]) < 50:
        flags.append("semantic_overengineering_detected")
    if _float(judge_dimensions.get("completeness")) is not None and float(judge_dimensions["completeness"]) < 60:
        flags.append("semantic_completeness_concern")
    if _float(judge_dimensions.get("requirement_alignment")) is not None and float(judge_dimensions["requirement_alignment"]) < 60:
        flags.append("semantic_requirement_alignment_concern")
    if judge_confidence is not None and judge_confidence < 0.5:
        flags.append("judge_low_confidence")

    recommendation = "continue"
    if agreement == "major_disagreement":
        recommendation = "review_recommended"
    if judge_score is not None and judge_confidence is not None and judge_score < 50 and judge_confidence >= 0.75:
        recommendation = "review_recommended"
    if "semantic_requirement_alignment_concern" in flags:
        recommendation = "review_recommended"

    return {
        "status": "completed",
        "source": source,
        "score": score,
        "confidence": None if confidence is None else round(confidence, 4),
        "agreement": agreement,
        "dimensions": dimensions,
        "flags": flags,
        "recommendation": recommendation,
        "version": version,
        "weights": {
            "deterministic_quality": round(det_weight, 4),
            "judge_overall": round(judge_weight, 4) if judge_score is not None else 0,
        },
    }


def planning_result_with_hybrid(state: Mapping[str, Any], hybrid: Mapping[str, Any]) -> dict[str, Any]:
    planning = dict(state.get("planning_result") or {})
    planning["hybrid_evaluation"] = dict(hybrid)
    return planning


async def planner_hybrid_evaluation_node(state: SoftwareFactoryState, _dependencies: Any) -> dict[str, Any]:
    emit_workflow_event(
        WorkflowEventType.STAGE_STARTED,
        source="planner_hybrid_evaluation",
        stage="planning_hybrid_evaluation",
        status=EventStatus.RUNNING,
        data={"event": "planner_hybrid_evaluation_started", "version": planner_hybrid_version()},
    )
    hybrid = build_planning_hybrid_evaluation(state)
    emit_workflow_event(
        WorkflowEventType.STAGE_COMPLETED,
        source="planner_hybrid_evaluation",
        stage="planning_hybrid_evaluation",
        status=EventStatus.COMPLETED,
        data={
            "event": "planner_hybrid_evaluation_completed",
            "source": hybrid["source"],
            "score": hybrid["score"],
            "confidence": hybrid["confidence"],
            "agreement": hybrid["agreement"],
            "flag_count": len(hybrid["flags"]),
            "recommendation": hybrid["recommendation"],
            "version": hybrid["version"],
        },
    )
    return {
        "planning_hybrid_evaluation": hybrid,
        "planning_hybrid_evaluation_version": hybrid["version"],
        "planning_result": planning_result_with_hybrid(state, hybrid),
    }


def build_planning_judge_calibration(state: Mapping[str, Any], evaluation: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
    version = planner_hybrid_version()
    outcome = str(evaluation.get("outcome") or "")
    if outcome == "infrastructure_failed":
        return "excluded_infrastructure_failure", None
    if outcome == "user_cancelled":
        return "excluded_user_cancelled", None
    observed_score = _float(evaluation.get("outcome_score"))
    if observed_score is None or outcome == "incomplete":
        return "insufficient_outcome", None
    judge_result = state.get("planning_judge_result")
    if state.get("planning_judge_status") != "completed" or not isinstance(judge_result, Mapping):
        return "judge_unavailable", None
    judge_score = _float(judge_result.get("overall_score"))
    judge_confidence = _float(judge_result.get("confidence"))
    if judge_score is None or judge_confidence is None:
        return "judge_unavailable", None
    observed = round(observed_score / 100, 4)
    score_error = round(judge_score - observed_score, 4)
    confidence_error = round(judge_confidence - observed, 4)
    return "completed", {
        "judge_score": judge_score,
        "observed_outcome_score": observed_score,
        "score_error": score_error,
        "score_absolute_error": round(abs(score_error), 4),
        "judge_confidence": judge_confidence,
        "observed_outcome": observed,
        "confidence_error": confidence_error,
        "confidence_absolute_error": round(abs(confidence_error), 4),
        "label": calibration_label(confidence_error),
        "version": version,
    }


def build_planning_hybrid_calibration(state: Mapping[str, Any], evaluation: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
    version = planner_hybrid_version()
    outcome = str(evaluation.get("outcome") or "")
    if outcome == "infrastructure_failed":
        return "excluded_infrastructure_failure", None
    if outcome == "user_cancelled":
        return "excluded_user_cancelled", None
    observed_score = _float(evaluation.get("outcome_score"))
    hybrid = state.get("planning_hybrid_evaluation")
    if observed_score is None or outcome == "incomplete" or not isinstance(hybrid, Mapping):
        return "insufficient_outcome", None
    hybrid_score = _float(hybrid.get("score"))
    hybrid_confidence = _float(hybrid.get("confidence"))
    if hybrid_score is None:
        return "insufficient_outcome", None
    observed = round(observed_score / 100, 4)
    score_error = round(hybrid_score - observed_score, 4)
    confidence_error = None if hybrid_confidence is None else round(hybrid_confidence - observed, 4)
    return "completed", {
        "hybrid_score": hybrid_score,
        "observed_outcome_score": observed_score,
        "score_error": score_error,
        "score_absolute_error": round(abs(score_error), 4),
        "hybrid_confidence": hybrid_confidence,
        "observed_outcome": observed,
        "confidence_error": confidence_error,
        "confidence_absolute_error": None if confidence_error is None else round(abs(confidence_error), 4),
        "label": None if confidence_error is None else calibration_label(confidence_error),
        "version": version,
    }


def calibration_updates(state: Mapping[str, Any], evaluation: Mapping[str, Any]) -> dict[str, Any]:
    judge_status, judge_calibration = build_planning_judge_calibration(state, evaluation)
    hybrid_status, hybrid_calibration = build_planning_hybrid_calibration(state, evaluation)
    observed = _float(evaluation.get("outcome_score"))
    deterministic_error = (evaluation.get("quality_prediction") or {}).get("signed_error") if isinstance(evaluation.get("quality_prediction"), Mapping) else None
    deterministic_absolute = _float((evaluation.get("quality_prediction") or {}).get("absolute_error")) if isinstance(evaluation.get("quality_prediction"), Mapping) else None
    judge_absolute = None if judge_calibration is None else judge_calibration["score_absolute_error"]
    hybrid_absolute = None if hybrid_calibration is None else hybrid_calibration["score_absolute_error"]
    updates = {
        "planning_judge_calibration_status": judge_status,
        "planning_judge_calibration": judge_calibration,
        "planning_judge_calibration_version": planner_hybrid_version(),
        "planning_judge_score": None if judge_calibration is None else judge_calibration["judge_score"],
        "planning_judge_score_error": None if judge_calibration is None else judge_calibration["score_error"],
        "planning_judge_score_absolute_error": None if judge_calibration is None else judge_calibration["score_absolute_error"],
        "planning_judge_confidence": None if judge_calibration is None else judge_calibration["judge_confidence"],
        "planning_judge_confidence_error": None if judge_calibration is None else judge_calibration["confidence_error"],
        "planning_judge_confidence_absolute_error": None if judge_calibration is None else judge_calibration["confidence_absolute_error"],
        "planning_hybrid_calibration_status": hybrid_status,
        "planning_hybrid_calibration": hybrid_calibration,
        "planning_hybrid_prediction_error": None if hybrid_calibration is None else hybrid_calibration["score_error"],
        "planning_hybrid_absolute_error": hybrid_absolute,
        "planning_deterministic_absolute_error": deterministic_absolute,
        "planning_deterministic_prediction_error": deterministic_error,
    }
    planning_result = dict(state.get("planning_result") or {})
    evaluation_with_calibration = dict(evaluation)
    evaluation_with_calibration["judge_calibration_status"] = judge_status
    evaluation_with_calibration["judge_calibration"] = judge_calibration
    evaluation_with_calibration["hybrid_calibration_status"] = hybrid_status
    evaluation_with_calibration["hybrid_calibration"] = hybrid_calibration
    evaluation_with_calibration["prediction_comparison"] = {
        "deterministic_absolute_error": deterministic_absolute,
        "judge_absolute_error": judge_absolute,
        "hybrid_absolute_error": hybrid_absolute,
    }
    planning_result["evaluation"] = evaluation_with_calibration
    updates["planning_evaluation"] = evaluation_with_calibration
    updates["planning_result"] = planning_result
    return updates


def aggregate_hybrid_judge_metrics(states: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    records = [state for state in states if isinstance(state, Mapping)]

    def avg(field: str) -> float | None:
        values = [_float(item.get(field)) for item in records]
        numbers = [value for value in values if value is not None]
        return None if not numbers else round(sum(numbers) / len(numbers), 4)

    judge_calibrations = [
        item.get("planning_judge_calibration")
        for item in records
        if isinstance(item.get("planning_judge_calibration"), Mapping)
    ]
    labels = [str(item.get("label")) for item in judge_calibrations]
    disagreements = [
        item.get("planning_hybrid_evaluation")
        for item in records
        if isinstance(item.get("planning_hybrid_evaluation"), Mapping)
    ]
    return {
        "judge_evaluated_count": sum(1 for item in records if item.get("planning_judge_status") == "completed"),
        "average_judge_score": avg("planning_judge_score"),
        "average_judge_score_absolute_error": avg("planning_judge_score_absolute_error"),
        "average_judge_confidence": avg("planning_judge_confidence"),
        "average_judge_confidence_absolute_error": avg("planning_judge_confidence_absolute_error"),
        "judge_overconfident_count": labels.count("overconfident") + labels.count("slightly_overconfident"),
        "judge_underconfident_count": labels.count("underconfident") + labels.count("slightly_underconfident"),
        "judge_well_calibrated_count": labels.count("well_calibrated"),
        "judge_major_disagreement_count": sum(1 for item in disagreements if item.get("agreement") == "major_disagreement"),
        "average_deterministic_absolute_error": avg("planning_deterministic_absolute_error"),
        "average_judge_absolute_error": avg("planning_judge_score_absolute_error"),
        "average_hybrid_absolute_error": avg("planning_hybrid_absolute_error"),
    }
