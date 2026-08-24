from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from graph.planner_evaluation import INFRASTRUCTURE_FAILURE_TYPES, is_plan_related_failure
from graph.state import SoftwareFactoryState
from streaming import EventStatus, WorkflowEventType, emit_workflow_event


AGENT_PERFORMANCE_VERSION = "8.3-v1"
AGENTS = ("planner", "developer", "repair", "qa")
INFRASTRUCTURE_STAGES = {
    "prepare_environment",
    "test_environment",
    "testing_environment",
    "execute_prepare_environment",
    "execute_tests",
    "testing",
}


def _clamp(value: float, minimum: float = 0, maximum: float = 100) -> float:
    return max(minimum, min(maximum, value))


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _bool(value: Any) -> bool:
    return bool(value)


def performance_level(score: float | None) -> str | None:
    if score is None:
        return None
    if score < 40:
        return "poor"
    if score < 60:
        return "weak"
    if score < 75:
        return "acceptable"
    if score < 90:
        return "good"
    return "excellent"


def failure_attribution(state: Mapping[str, Any]) -> str:
    terminal = str(state.get("terminal_status") or "")
    failure_type = str(state.get("failure_type") or "")
    failure_stage = str(state.get("failure_stage") or "")
    if terminal == "user_cancelled" or state.get("user_cancelled"):
        return "user_related"
    if (
        terminal == "infrastructure_failed"
        or state.get("test_infrastructure_failed")
        or failure_type in INFRASTRUCTURE_FAILURE_TYPES
        or failure_type.casefold().startswith("mcp_")
    ):
        return "infrastructure_related"
    if failure_stage in {"planning_risk_approval", "git_approval", "git_promotion_approval"}:
        return "policy_related"
    return "agent_related"


def _evaluation(
    *,
    agent: str,
    status: str,
    score: float | None,
    confidence: float,
    metrics: dict[str, Any],
    strengths: Iterable[str] = (),
    issues: Iterable[str] = (),
    reason_codes: Iterable[str] = (),
) -> dict[str, Any]:
    normalized_score = None if score is None else round(_clamp(score), 4)
    return {
        "agent": agent,
        "status": status,
        "score": normalized_score,
        "level": performance_level(normalized_score),
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "metrics": metrics,
        "strengths": list(strengths),
        "issues": list(issues),
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "version": AGENT_PERFORMANCE_VERSION,
    }


def evaluate_planner_performance(state: Mapping[str, Any]) -> dict[str, Any]:
    attempts = _int(state.get("planning_attempts"))
    valid = _bool(state.get("planning_valid"))
    quality = _float(state.get("planning_quality_score"))
    outcome = _float(state.get("planning_outcome_score"))
    confidence = _float(state.get("planning_decision_confidence"))
    refinement_attempts = _int(state.get("planning_quality_refinement_attempts"))
    prediction_abs = _float(state.get("planning_quality_prediction_absolute_error"))
    hybrid = state.get("planning_hybrid_evaluation") if isinstance(state.get("planning_hybrid_evaluation"), Mapping) else {}

    score = quality if quality is not None else (outcome if outcome is not None else 50)
    if outcome is not None and failure_attribution(state) != "infrastructure_related":
        score = score * 0.65 + outcome * 0.35
    if not valid:
        score -= 40
    score -= max(attempts - 1, 0) * 10
    score -= refinement_attempts * 5
    if prediction_abs is not None and prediction_abs >= 25:
        score -= 10
    if is_plan_related_failure(state):
        score -= 15
    strengths = []
    issues = []
    reasons = []
    if valid:
        strengths.append("Planning contract validated.")
    else:
        issues.append("Planning contract did not validate.")
        reasons.append("planning_invalid")
    if refinement_attempts:
        issues.append("Planning required quality refinement.")
        reasons.append("planning_refinement_required")
    if hybrid.get("recommendation") == "review_recommended":
        issues.append("Hybrid evaluation recommended human review.")
        reasons.append("hybrid_review_recommended")
    return _evaluation(
        agent="planner",
        status="completed" if valid or attempts else "insufficient_data",
        score=score if valid or attempts else None,
        confidence=0.9 if outcome is not None else (confidence or 0.6),
        metrics={
            "attempt_count": attempts,
            "validation_success": valid,
            "retry_count": max(attempts - 1, 0),
            "downstream_success": state.get("tests_passed") is True,
            "introduced_rework": refinement_attempts > 0 or is_plan_related_failure(state),
            "resolved_rework": state.get("planning_refinement_effective"),
            "terminal_contribution": state.get("planning_evaluation_outcome"),
            "planning_quality_score": quality,
            "planning_outcome_score": outcome,
            "planning_hybrid_score": hybrid.get("score"),
        },
        strengths=strengths,
        issues=issues,
        reason_codes=reasons or ["planner_evaluated"],
    )


def evaluate_developer_performance(state: Mapping[str, Any]) -> dict[str, Any]:
    attribution = failure_attribution(state)
    failure_stage = str(state.get("failure_stage") or "")
    if attribution == "infrastructure_related" and failure_stage in {"implementation", "prepare_environment", "execute_prepare_environment", "testing", "execute_tests"}:
        return _evaluation(
            agent="developer",
            status="excluded_infrastructure_failure",
            score=None,
            confidence=0.9,
            metrics={"attempt_count": _int(state.get("implementation_attempts")), "infrastructure_failure": True},
            issues=["Infrastructure failure is not attributed to Developer."],
            reason_codes=["infrastructure_failure_excluded"],
        )
    attempts = _int(state.get("implementation_attempts"))
    valid = _bool(state.get("implementation_valid"))
    repair_attempts = _int(state.get("repair_attempts"))
    errors = state.get("implementation_errors") if isinstance(state.get("implementation_errors"), list) else []
    generated = state.get("generated_files") if isinstance(state.get("generated_files"), list) else []
    updated = state.get("files_updated_during_repair") if isinstance(state.get("files_updated_during_repair"), list) else []
    first_pass_success = attempts <= 1 and valid and state.get("tests_passed") is True and repair_attempts == 0
    required_repair = repair_attempts > 0 and attribution != "infrastructure_related"

    if attempts == 0 and not valid and not generated:
        return _evaluation(
            agent="developer",
            status="insufficient_data",
            score=None,
            confidence=0.2,
            metrics={"attempt_count": 0, "validation_success": False},
            reason_codes=["developer_not_executed"],
        )
    score = 100
    score -= max(attempts - 1, 0) * 15
    if not valid:
        score -= 25
    if errors:
        score -= min(len(errors) * 5, 20)
    if required_repair:
        score -= 25
    if state.get("terminal_status") == "implementation_failed":
        score -= 30
    if first_pass_success:
        score += 5
    strengths = ["Implementation validated."] if valid else []
    if first_pass_success:
        strengths.append("Implementation passed tests without repair.")
    issues = []
    reasons = []
    if attempts > 1:
        issues.append("Implementation required retries.")
        reasons.append("implementation_retries")
    if required_repair:
        issues.append("Implementation required downstream repair.")
        reasons.append("repair_required_after_implementation")
    if not valid:
        issues.append("Implementation validation failed.")
        reasons.append("implementation_invalid")
    return _evaluation(
        agent="developer",
        status="completed",
        score=score,
        confidence=0.95 if state.get("tests_executed") else 0.75,
        metrics={
            "attempt_count": attempts,
            "implementation_attempts": attempts,
            "validation_success": valid,
            "retry_count": max(attempts - 1, 0),
            "downstream_success": state.get("tests_passed") is True,
            "introduced_rework": required_repair or attempts > 1,
            "resolved_rework": False,
            "terminal_contribution": state.get("terminal_status"),
            "first_pass_success": first_pass_success,
            "validation_error_count": len(errors),
            "files_generated": len(generated),
            "files_updated": len(updated),
            "required_repair": required_repair,
            "tests_passed_after_implementation": state.get("tests_passed") is True,
        },
        strengths=strengths,
        issues=issues,
        reason_codes=reasons or ["developer_evaluated"],
    )


def evaluate_repair_performance(state: Mapping[str, Any]) -> dict[str, Any]:
    attempts = _int(state.get("repair_attempts"))
    phase = str(state.get("repair_phase") or "not_started")
    attribution = failure_attribution(state)
    if attempts == 0 and phase == "not_started":
        return _evaluation(
            agent="repair",
            status="not_applicable",
            score=None,
            confidence=1,
            metrics={"repair_required": False, "attempt_count": 0, "repair_attempts": 0},
            reason_codes=["repair_not_required"],
        )
    if attribution == "infrastructure_related" and str(state.get("failure_stage") or "") in INFRASTRUCTURE_STAGES:
        return _evaluation(
            agent="repair",
            status="excluded_infrastructure_failure",
            score=None,
            confidence=0.9,
            metrics={"repair_required": attempts > 0, "attempt_count": attempts, "infrastructure_failure": True},
            reason_codes=["infrastructure_failure_excluded"],
        )
    repair_success = attempts > 0 and phase == "completed" and state.get("tests_passed") is True
    tests_recovered = attempts > 0 and state.get("tests_passed") is True
    score = 100
    score -= max(attempts - 1, 0) * 15
    if not repair_success:
        score -= 35
    if state.get("terminal_status") == "repair_limit_reached":
        score -= 25
    strengths = ["Repair recovered the failing tests."] if tests_recovered else []
    issues = [] if repair_success else ["Repair did not recover tests."]
    return _evaluation(
        agent="repair",
        status="completed",
        score=score,
        confidence=0.95 if attempts > 0 else 0.4,
        metrics={
            "repair_required": True,
            "attempt_count": attempts,
            "repair_attempts": attempts,
            "validation_success": repair_success,
            "retry_count": max(attempts - 1, 0),
            "downstream_success": state.get("tests_passed") is True,
            "introduced_rework": False,
            "resolved_rework": tests_recovered,
            "terminal_contribution": state.get("terminal_status"),
            "repair_success": repair_success,
            "tests_recovered": tests_recovered,
            "repair_efficiency": None if attempts == 0 else round(1 / attempts, 4),
        },
        strengths=strengths,
        issues=issues,
        reason_codes=["repair_success"] if repair_success else ["repair_incomplete"],
    )


def evaluate_qa_performance(state: Mapping[str, Any]) -> dict[str, Any]:
    attribution = failure_attribution(state)
    if attribution == "infrastructure_related" and (
        state.get("test_infrastructure_failed") or str(state.get("failure_stage") or "") in INFRASTRUCTURE_STAGES
    ):
        return _evaluation(
            agent="qa",
            status="excluded_infrastructure_failure",
            score=None,
            confidence=0.95,
            metrics={"tests_executed": _bool(state.get("tests_executed")), "infrastructure_failure": True},
            issues=["Testing infrastructure failure is excluded from QA scoring."],
            reason_codes=["testing_infrastructure_failure_excluded"],
        )
    tests_executed = _bool(state.get("tests_executed"))
    tests_passed = state.get("tests_passed") is True
    repair_triggered = _int(state.get("repair_attempts")) > 0
    failure_detected = tests_executed and state.get("tests_passed") is False or repair_triggered
    failure_actionable = bool(state.get("test_failure_summary") or state.get("failing_test_files") or repair_triggered)
    if not tests_executed:
        return _evaluation(
            agent="qa",
            status="insufficient_data",
            score=None,
            confidence=0.3,
            metrics={"tests_executed": False, "tests_passed": False},
            reason_codes=["tests_not_executed"],
        )
    score = 100
    if not tests_passed and not repair_triggered:
        score -= 20
    if failure_detected and not failure_actionable:
        score -= 20
    if state.get("failure_type") == "false_positive":
        score -= 30
    strengths = ["Tests executed and produced an interpretable result."]
    if failure_detected:
        strengths.append("QA surfaced a failure signal for repair.")
    return _evaluation(
        agent="qa",
        status="completed",
        score=score,
        confidence=0.95,
        metrics={
            "attempt_count": 1,
            "validation_success": tests_executed,
            "retry_count": 0,
            "downstream_success": tests_passed,
            "introduced_rework": False,
            "resolved_rework": repair_triggered,
            "terminal_contribution": state.get("terminal_status"),
            "tests_executed": tests_executed,
            "tests_passed": tests_passed,
            "test_failure_detected": failure_detected,
            "failure_actionable": failure_actionable,
            "false_positive_signal": state.get("failure_type") == "false_positive",
            "infrastructure_failure": False,
            "repair_triggered": repair_triggered,
        },
        strengths=strengths,
        issues=[],
        reason_codes=["qa_evaluated"],
    )


def evaluate_agent_performance(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "planner": evaluate_planner_performance(state),
        "developer": evaluate_developer_performance(state),
        "repair": evaluate_repair_performance(state),
        "qa": evaluate_qa_performance(state),
    }


async def agent_performance_evaluation_node(state: SoftwareFactoryState, _dependencies: Any) -> dict[str, Any]:
    emit_workflow_event(
        WorkflowEventType.STAGE_STARTED,
        source="agent_performance",
        stage="agent_performance_evaluation",
        status=EventStatus.RUNNING,
        data={"event": "agent_performance_evaluation_started", "version": AGENT_PERFORMANCE_VERSION},
    )
    evaluations = evaluate_agent_performance(state)
    for item in evaluations.values():
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="agent_performance",
            stage="agent_performance_evaluation",
            status=EventStatus.COMPLETED,
            data={
                "event": "agent_performance_evaluation_completed",
                "agent": item["agent"],
                "score": item["score"],
                "level": item["level"],
                "confidence": item["confidence"],
                "status": item["status"],
                "reason_code_count": len(item["reason_codes"]),
                "version": AGENT_PERFORMANCE_VERSION,
            },
        )
    return {
        "agent_performance_evaluations": evaluations,
        "agent_performance_version": AGENT_PERFORMANCE_VERSION,
    }


def aggregate_agent_performance(states: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    records = [
        item.get("agent_performance_evaluations")
        for item in states
        if isinstance(item.get("agent_performance_evaluations"), Mapping)
    ]

    def agent_items(agent: str) -> list[Mapping[str, Any]]:
        return [
            value
            for record in records
            if isinstance((value := record.get(agent)), Mapping)
            and value.get("status") not in {"not_applicable", "excluded_infrastructure_failure", "insufficient_data"}
        ]

    def avg(agent: str) -> float | None:
        values = [_float(item.get("score")) for item in agent_items(agent)]
        numbers = [value for value in values if value is not None]
        return None if not numbers else round(sum(numbers) / len(numbers), 4)

    def rate(agent: str, predicate) -> float | None:
        items = agent_items(agent)
        if not items:
            return None
        return round(sum(1 for item in items if predicate(item)) / len(items), 4)

    repair_all = [
        record.get("repair")
        for record in records
        if isinstance(record.get("repair"), Mapping)
    ]
    qa_all = [
        record.get("qa")
        for record in records
        if isinstance(record.get("qa"), Mapping)
    ]
    return {
        "average_planner_score": avg("planner"),
        "average_developer_score": avg("developer"),
        "average_repair_score": avg("repair"),
        "average_qa_score": avg("qa"),
        "first_pass_developer_rate": rate("developer", lambda item: bool((item.get("metrics") or {}).get("first_pass_success"))),
        "repair_success_rate": rate("repair", lambda item: bool((item.get("metrics") or {}).get("repair_success"))),
        "qa_infrastructure_exclusion_rate": None if not qa_all else round(sum(1 for item in qa_all if item.get("status") == "excluded_infrastructure_failure") / len(qa_all), 4),
        "agent_evaluation_counts": {agent: len(agent_items(agent)) for agent in AGENTS},
        "planner_good_or_better_rate": rate("planner", lambda item: item.get("level") in {"good", "excellent"}),
        "developer_good_or_better_rate": rate("developer", lambda item: item.get("level") in {"good", "excellent"}),
        "repair_good_or_better_rate": rate("repair", lambda item: item.get("level") in {"good", "excellent"}),
        "qa_good_or_better_rate": rate("qa", lambda item: item.get("level") in {"good", "excellent"}),
        "repair_not_applicable_count": sum(1 for item in repair_all if item.get("status") == "not_applicable"),
    }
