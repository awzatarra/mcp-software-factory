from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from graph.planner_hybrid_evaluation import calibration_updates, aggregate_hybrid_judge_metrics
from graph.state import SoftwareFactoryState
from streaming import EventStatus, WorkflowEventType, emit_workflow_event


PLANNING_EVALUATION_VERSION = "7.7-v1"
PLAN_RELATED_FAILURE_TYPES = {
    "implementation_schema_invalid",
    "implementation_state_missing_project_implementation",
    "implementation_path_conflict",
    "implementation_validation_failed",
    "implementation_dependency_inconsistency",
    "implementation_missing_required_file",
    "implementation_missing_artifact",
    "missing_planned_artifact",
    "target_mismatch",
    "dependency_inconsistency",
}
INFRASTRUCTURE_FAILURE_TYPES = {
    "mcp_timeout",
    "mcp_error",
    "testing_environment_failed",
    "test_environment_failed",
    "prepare_environment_failed",
    "MCPToolTimeout",
}
RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
RISK_FROM_SCORE = {0: "none", 1: "low", 2: "medium", 3: "high", 4: "critical"}


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _terminal_status(state: Mapping[str, Any]) -> str | None:
    status = state.get("terminal_status")
    return str(status) if status is not None else None


def is_plan_related_failure(state: Mapping[str, Any]) -> bool:
    failure_type = str(state.get("failure_type") or "")
    failure_stage = str(state.get("failure_stage") or "")
    if failure_type in INFRASTRUCTURE_FAILURE_TYPES or failure_type.casefold().startswith("mcp_"):
        return False
    if failure_type in PLAN_RELATED_FAILURE_TYPES:
        return True
    if failure_stage == "implementation" and failure_type in {
        "validation_failed",
        "schema_invalid",
        "missing_artifact",
    }:
        return True
    return False


def classify_planning_evaluation_outcome(state: Mapping[str, Any]) -> str:
    terminal = _terminal_status(state)
    repair_attempts = _as_int(state.get("repair_attempts"))
    tests_passed = bool(state.get("tests_passed"))
    tests_executed = bool(state.get("tests_executed"))
    implementation_valid = bool(state.get("implementation_valid"))
    failure_type = str(state.get("failure_type") or "")
    failure_stage = str(state.get("failure_stage") or "")

    if tests_passed and repair_attempts > 0:
        return "successful_with_repair"
    if tests_passed:
        return "successful"
    if terminal == "user_cancelled" or state.get("user_cancelled"):
        return "user_cancelled"
    if (
        terminal == "infrastructure_failed"
        or state.get("test_infrastructure_failed")
        or failure_type in INFRASTRUCTURE_FAILURE_TYPES
        or failure_type.casefold().startswith("mcp_")
    ):
        return "infrastructure_failed"
    if terminal == "implementation_failed" or (
        not implementation_valid and failure_stage in {"implementation", "implementation_validation"}
    ):
        return "implementation_failed"
    if tests_executed and state.get("tests_passed") is False:
        return "testing_failed"
    if terminal in {"tests_failed", "repair_limit_reached"}:
        return "testing_failed"
    if terminal in {"completed", "planning_failed"}:
        return "implementation_failed" if terminal == "planning_failed" else "incomplete"
    return "incomplete"


def compute_planning_outcome_score(state: Mapping[str, Any], outcome: str) -> int:
    score = 100
    implementation_attempts = _as_int(state.get("implementation_attempts"))
    repair_attempts = _as_int(state.get("repair_attempts"))
    if implementation_attempts > 0:
        score -= max(implementation_attempts - 1, 0) * 15
    if repair_attempts > 0:
        score -= 20
    if repair_attempts > 1:
        score -= (repair_attempts - 1) * 10
    if outcome == "testing_failed":
        score -= 40
    elif outcome == "implementation_failed":
        score -= 50
    elif outcome == "infrastructure_failed":
        score -= 35
    elif outcome == "user_cancelled":
        score -= 30
    elif outcome == "incomplete":
        score -= 25
    if _terminal_status(state) == "repair_limit_reached":
        score -= 15
    if is_plan_related_failure(state):
        score -= 15
    return int(_clamp(score, 0, 100))


def confidence_calibration_label(error: float) -> str:
    absolute = abs(error)
    if absolute <= 0.10:
        return "well_calibrated"
    if absolute <= 0.20:
        return "slightly_overconfident" if error > 0 else "slightly_underconfident"
    return "overconfident" if error > 0 else "underconfident"


def observed_risk_severity(state: Mapping[str, Any], outcome: str) -> str:
    if outcome == "infrastructure_failed":
        return "none"
    if outcome == "successful":
        return "low"
    severity = 0
    repair_attempts = _as_int(state.get("repair_attempts"))
    if repair_attempts == 1:
        severity = max(severity, 1)
    elif repair_attempts >= 2:
        severity = max(severity, 3)
    if outcome == "successful_with_repair":
        severity = max(severity, 2 if repair_attempts >= 2 else 1)
    if outcome == "testing_failed":
        severity = max(severity, 3)
    if outcome == "implementation_failed":
        severity = max(severity, 3 if is_plan_related_failure(state) else 2)
    if is_plan_related_failure(state):
        severity = max(severity, 3)
    if _terminal_status(state) == "repair_limit_reached":
        severity = max(severity, 4)
    return RISK_FROM_SCORE[severity]


def risk_calibration_label(predicted: str | None, observed: str) -> str:
    predicted_value = RISK_ORDER.get(str(predicted or "none").casefold(), 0)
    observed_value = RISK_ORDER.get(str(observed or "none").casefold(), 0)
    if predicted_value == observed_value:
        return "aligned"
    return "underestimated" if predicted_value < observed_value else "overestimated"


def evaluate_planner_outcome(state: SoftwareFactoryState) -> dict[str, Any]:
    outcome = classify_planning_evaluation_outcome(state)
    outcome_score = compute_planning_outcome_score(state, outcome)
    quality_score = _as_float(state.get("planning_quality_score"))
    confidence = _as_float(state.get("planning_decision_confidence"))
    observed_confidence = round(outcome_score / 100, 4)
    quality_signed_error = None if quality_score is None else round(quality_score - outcome_score, 4)
    quality_absolute_error = None if quality_signed_error is None else round(abs(quality_signed_error), 4)
    confidence_signed_error = None if confidence is None else round(confidence - observed_confidence, 4)
    confidence_absolute_error = (
        None if confidence_signed_error is None else round(abs(confidence_signed_error), 4)
    )
    confidence_label = (
        "well_calibrated"
        if confidence_signed_error is None
        else confidence_calibration_label(confidence_signed_error)
    )
    observed_risk = observed_risk_severity(state, outcome)
    risk_label = risk_calibration_label(state.get("planning_risk_level"), observed_risk)
    refinement_attempts = _as_int(state.get("planning_quality_refinement_attempts"))
    score_delta = state.get("planning_quality_score_delta")
    score_delta_int = _as_int(score_delta) if score_delta is not None else None
    refinement_effective = None
    if refinement_attempts > 0:
        refinement_effective = bool(
            score_delta_int is not None
            and score_delta_int > 0
            and state.get("tests_passed") is True
        )
    plan_related = is_plan_related_failure(state)
    false_positive = bool(
        state.get("planning_quality_gate_decision") == "refine"
        and (score_delta_int is not None and score_delta_int <= 0)
        and outcome == "successful"
        and _as_int(state.get("repair_attempts")) == 0
    )
    false_negative = bool(
        state.get("planning_quality_gate_decision") == "continue"
        and plan_related
        and outcome in {"implementation_failed", "testing_failed", "successful_with_repair"}
    )
    return {
        "version": PLANNING_EVALUATION_VERSION,
        "outcome": outcome,
        "outcome_score": outcome_score,
        "quality_prediction": {
            "predicted": None if quality_score is None else int(quality_score),
            "observed": outcome_score,
            "signed_error": quality_signed_error,
            "absolute_error": quality_absolute_error,
        },
        "confidence_calibration": {
            "predicted": confidence,
            "observed": observed_confidence,
            "signed_error": confidence_signed_error,
            "absolute_error": confidence_absolute_error,
            "label": confidence_label,
        },
        "risk_calibration": {
            "predicted": state.get("planning_risk_level"),
            "observed": observed_risk,
            "label": risk_label,
        },
        "refinement": {
            "attempts": refinement_attempts,
            "effective": refinement_effective,
        },
        "quality_gate": {
            "false_positive": false_positive,
            "false_negative": false_negative,
        },
        "plan_related_failure": plan_related,
    }


def planner_evaluation_updates(state: SoftwareFactoryState) -> dict[str, Any]:
    existing = state.get("planning_evaluation")
    if (
        isinstance(existing, dict)
        and existing.get("version") == PLANNING_EVALUATION_VERSION
        and existing.get("outcome") != "incomplete"
        and _terminal_status(state) not in {None, "pending"}
    ):
        evaluation = existing
    else:
        evaluation = evaluate_planner_outcome(state)
    planning_result = dict(state.get("planning_result") or {})
    planning_result["evaluation"] = evaluation
    quality_prediction = evaluation["quality_prediction"]
    confidence = evaluation["confidence_calibration"]
    risk = evaluation["risk_calibration"]
    updates = {
        "planning_evaluation": evaluation,
        "planning_evaluation_version": evaluation["version"],
        "planning_evaluation_outcome": evaluation["outcome"],
        "planning_outcome_score": evaluation["outcome_score"],
        "planning_quality_prediction_error": quality_prediction["signed_error"],
        "planning_quality_prediction_absolute_error": quality_prediction["absolute_error"],
        "planning_confidence_error": confidence["signed_error"],
        "planning_confidence_absolute_error": confidence["absolute_error"],
        "planning_confidence_calibration": confidence["label"],
        "planning_risk_observed_severity": risk["observed"],
        "planning_risk_calibration": risk["label"],
        "planning_refinement_effective": evaluation["refinement"]["effective"],
        "planning_quality_gate_false_positive": evaluation["quality_gate"]["false_positive"],
        "planning_quality_gate_false_negative": evaluation["quality_gate"]["false_negative"],
        "planning_result": planning_result,
    }
    updates.update(calibration_updates({**state, **updates}, evaluation))
    return updates


async def planner_evaluation_node(
    state: SoftwareFactoryState,
    _dependencies: Any,
) -> dict[str, Any]:
    emit_workflow_event(
        WorkflowEventType.STAGE_STARTED,
        source="planner_evaluation",
        stage="planner_evaluation",
        status=EventStatus.RUNNING,
        data={"event": "planner_evaluation_started"},
    )
    updates = planner_evaluation_updates(state)
    evaluation = updates["planning_evaluation"]
    emit_workflow_event(
        WorkflowEventType.STAGE_COMPLETED,
        source="planner_evaluation",
        stage="planner_evaluation",
        status=EventStatus.COMPLETED,
        data={
            "event": "planner_evaluation_completed",
            "outcome": evaluation["outcome"],
            "outcome_score": evaluation["outcome_score"],
            "quality_absolute_error": evaluation["quality_prediction"]["absolute_error"],
            "confidence_absolute_error": evaluation["confidence_calibration"]["absolute_error"],
            "confidence_calibration": evaluation["confidence_calibration"]["label"],
            "risk_calibration": evaluation["risk_calibration"]["label"],
            "refinement_effective": evaluation["refinement"]["effective"],
        },
    )
    return updates


def aggregate_planner_evaluations(states: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    records = list(states)
    evaluations = [
        state.get("planning_evaluation")
        for state in records
        if isinstance(state.get("planning_evaluation"), dict)
    ]
    total = len(evaluations)

    def average(path: tuple[str, ...]) -> float | None:
        values: list[float] = []
        for evaluation in evaluations:
            current: Any = evaluation
            for key in path:
                current = current.get(key) if isinstance(current, dict) else None
            number = _as_float(current)
            if number is not None:
                values.append(number)
        return None if not values else round(sum(values) / len(values), 4)

    outcomes = [str(item.get("outcome")) for item in evaluations]
    confidence_labels = [
        str((item.get("confidence_calibration") or {}).get("label")) for item in evaluations
    ]
    risk_labels = [str((item.get("risk_calibration") or {}).get("label")) for item in evaluations]
    refinements = [item.get("refinement") or {} for item in evaluations]
    quality_gates = [item.get("quality_gate") or {} for item in evaluations]
    failed = total - outcomes.count("successful") - outcomes.count("successful_with_repair")
    aggregate = {
        "total_evaluated": total,
        "successful": outcomes.count("successful"),
        "successful_with_repair": outcomes.count("successful_with_repair"),
        "failed": failed,
        "average_quality_score": average(("quality_prediction", "predicted")),
        "average_outcome_score": average(("outcome_score",)),
        "average_quality_absolute_error": average(("quality_prediction", "absolute_error")),
        "average_confidence": average(("confidence_calibration", "predicted")),
        "average_confidence_absolute_error": average(("confidence_calibration", "absolute_error")),
        "overconfident_count": confidence_labels.count("overconfident")
        + confidence_labels.count("slightly_overconfident"),
        "underconfident_count": confidence_labels.count("underconfident")
        + confidence_labels.count("slightly_underconfident"),
        "well_calibrated_count": confidence_labels.count("well_calibrated"),
        "risk_underestimated_count": risk_labels.count("underestimated"),
        "risk_overestimated_count": risk_labels.count("overestimated"),
        "risk_aligned_count": risk_labels.count("aligned"),
        "quality_gate_false_positive_count": sum(
            1 for item in quality_gates if item.get("false_positive") is True
        ),
        "quality_gate_false_negative_count": sum(
            1 for item in quality_gates if item.get("false_negative") is True
        ),
        "refinement_effective_count": sum(
            1 for item in refinements if item.get("effective") is True
        ),
        "refinement_ineffective_count": sum(
            1 for item in refinements if item.get("effective") is False
        ),
    }
    aggregate.update(aggregate_hybrid_judge_metrics(records))
    return aggregate
