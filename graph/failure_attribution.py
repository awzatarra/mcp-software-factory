from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from graph.agent_performance import failure_attribution as broad_failure_attribution
from graph.planner_evaluation import INFRASTRUCTURE_FAILURE_TYPES, is_plan_related_failure
from graph.state import SoftwareFactoryState
from streaming import EventStatus, WorkflowEventType, emit_workflow_event


FAILURE_ATTRIBUTION_VERSION = "8.4-v1"
ROOT_CAUSES = {"planner", "developer", "repair", "qa", "infrastructure", "user", "policy", "external", "unknown"}
POLICY_FAILURE_TYPES = {
    "planning_approval_stale",
    "policy_conflict",
    "portfolio_conflict",
    "rollout_governance_conflict",
    "experiment_governance_conflict",
    "git_promotion_required_not_completed",
}
EXTERNAL_FAILURE_TYPES = {
    "provider_error",
    "provider_timeout",
    "openai_error",
    "external_api_unavailable",
    "external_dependency_unavailable",
    "llm_provider_error",
}
QA_FAILURE_TYPES = {
    "false_positive",
    "test_harness_defect",
    "test_configuration_defect",
    "testing_output_invalid",
}
IMPLEMENTATION_FAILURE_TYPES = {
    "implementation_schema_invalid",
    "implementation_state_missing_project_implementation",
    "implementation_path_conflict",
    "implementation_validation_failed",
    "implementation_dependency_inconsistency",
    "implementation_missing_required_file",
    "implementation_missing_artifact",
    "dependency_policy_violation",
    "dependency_policy_normalization_failed",
    "implementation_refinement_made_no_progress",
}


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if item))


def _confidence(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    return bool(value)


def _contributor(source: str, contribution: str, confidence: float, reason_codes: Iterable[str]) -> dict[str, Any]:
    return {
        "source": source,
        "contribution": contribution,
        "confidence": _confidence(confidence),
        "reason_codes": _dedupe(reason_codes),
    }


def _excluded(source: str, reason: str) -> dict[str, str]:
    return {"source": source, "reason": reason}


def _base_evidence(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "failure_type": state.get("failure_type"),
        "failure_stage": state.get("failure_stage"),
        "terminal_status": state.get("terminal_status"),
        "planning_valid": state.get("planning_valid"),
        "implementation_valid": state.get("implementation_valid"),
        "tests_executed": state.get("tests_executed"),
        "tests_passed": state.get("tests_passed"),
        "tests_passed_before_repair": bool(state.get("first_test_result_summary")) or (_int(state.get("repair_attempts")) > 0),
        "tests_passed_after_repair": state.get("tests_passed") is True and _int(state.get("repair_attempts")) > 0,
        "repair_attempts": _int(state.get("repair_attempts")),
        "repair_phase": state.get("repair_phase"),
        "planning_evaluation_outcome": state.get("planning_evaluation_outcome"),
        "plan_related_failure": is_plan_related_failure(state),
    }


def _chain(state: Mapping[str, Any], *items: str) -> list[str]:
    chain: list[str] = []
    if state.get("planning_valid"):
        chain.append("planner_valid")
    if state.get("project_created") or state.get("implementation_valid"):
        chain.append("developer_output_created")
    if state.get("first_test_result_summary") or (_int(state.get("repair_attempts")) > 0):
        chain.append("tests_failed")
    if _int(state.get("repair_attempts")) > 0:
        chain.append("repair_attempted")
    if state.get("tests_passed") is True:
        chain.append("tests_passed")
    chain.extend(items)
    return _dedupe(chain)[:12]


def _result(
    *,
    status: str,
    failure_class: str | None,
    root_cause: str | None,
    primary_attribution: str | None,
    contributors: list[dict[str, Any]] | None = None,
    excluded_attributions: list[dict[str, str]] | None = None,
    confidence: float | None,
    evidence: dict[str, Any],
    reason_codes: Iterable[str],
    recovered: bool = False,
    recovery_source: str | None = None,
    causal_chain: list[str] | None = None,
) -> dict[str, Any]:
    if root_cause is not None and root_cause not in ROOT_CAUSES:
        root_cause = "unknown"
    return {
        "status": status,
        "failure_class": failure_class,
        "root_cause": root_cause,
        "primary_attribution": primary_attribution,
        "contributors": contributors or [],
        "excluded_attributions": excluded_attributions or [],
        "confidence": None if confidence is None else _confidence(confidence),
        "evidence": evidence,
        "reason_codes": _dedupe(reason_codes),
        "recovered": recovered,
        "recovery_source": recovery_source,
        "causal_chain": causal_chain or [],
        "version": FAILURE_ATTRIBUTION_VERSION,
    }


def build_failure_attribution(state: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _base_evidence(state)
    failure_type = str(state.get("failure_type") or "")
    failure_stage = str(state.get("failure_stage") or "")
    terminal = str(state.get("terminal_status") or "")
    repair_attempts = _int(state.get("repair_attempts"))
    recovered = repair_attempts > 0 and state.get("tests_passed") is True
    broad = broad_failure_attribution(state)

    if recovered:
        contributors = [
            _contributor("qa", "detected", 0.9, ["qa_detected_failure"]),
            _contributor("repair", "resolved", 0.95, ["repair_resolved_failure"]),
        ]
        root = "planner" if is_plan_related_failure(state) else "developer"
        if root == "planner":
            contributors.insert(0, _contributor("planner", "caused", 0.85, ["root_cause_plan_related_recovered_failure"]))
            excluded = [_excluded("developer", "followed_valid_plan_or_not_primary")]
            failure_class = "planning_defect"
            reasons = ["root_cause_plan_target_mismatch", "root_cause_recovered_by_repair"]
        else:
            contributors.insert(0, _contributor("developer", "caused", 0.9, ["root_cause_test_failure_repaired"]))
            excluded = [_excluded("planner", "planning_valid")]
            failure_class = "implementation_defect"
            reasons = ["root_cause_test_failure_repaired"]
        return _result(
            status="completed",
            failure_class=failure_class,
            root_cause=root,
            primary_attribution=root,
            contributors=contributors,
            excluded_attributions=excluded,
            confidence=0.9,
            evidence=evidence,
            reason_codes=reasons,
            recovered=True,
            recovery_source="repair",
            causal_chain=_chain(state, "repair_resolved"),
        )

    if terminal == "completed" and not failure_type and not state.get("test_failure_summary"):
        return _result(
            status="not_applicable",
            failure_class=None,
            root_cause=None,
            primary_attribution=None,
            confidence=None,
            evidence=evidence,
            reason_codes=["no_failure_detected"],
            causal_chain=_chain(state),
        )

    if terminal == "user_cancelled" or state.get("user_cancelled") or failure_type == "user_rejected":
        return _result(
            status="completed",
            failure_class="user_rejection",
            root_cause="user",
            primary_attribution="user",
            contributors=[_contributor("user", "caused", 0.95, ["root_cause_user_rejected"])],
            excluded_attributions=[_excluded(agent, "user_rejection") for agent in ("planner", "developer", "repair", "qa")],
            confidence=0.95,
            evidence=evidence,
            reason_codes=["root_cause_user_rejected"],
            causal_chain=_chain(state, "user_rejected"),
        )

    if failure_type in EXTERNAL_FAILURE_TYPES or "provider" in failure_type or "external" in failure_type:
        return _result(
            status="completed",
            failure_class="external_provider_failure",
            root_cause="external",
            primary_attribution="external",
            contributors=[_contributor("external", "caused", 0.9, ["root_cause_external_provider"])],
            excluded_attributions=[_excluded(agent, "external_provider_failure") for agent in ("planner", "developer", "repair", "qa")],
            confidence=0.9,
            evidence=evidence,
            reason_codes=["root_cause_external_provider"],
            causal_chain=_chain(state, "external_provider_failure"),
        )

    if broad == "infrastructure_related":
        reason = "root_cause_mcp_timeout" if failure_type == "mcp_timeout" or failure_type.casefold().startswith("mcp_") else "root_cause_testing_infrastructure"
        return _result(
            status="completed",
            failure_class="infrastructure_failure",
            root_cause="infrastructure",
            primary_attribution="infrastructure",
            contributors=[_contributor("infrastructure", "caused", 0.95, [reason])],
            excluded_attributions=[_excluded(agent, "infrastructure_failure") for agent in ("planner", "developer", "repair", "qa")],
            confidence=0.95,
            evidence=evidence,
            reason_codes=[reason],
            causal_chain=_chain(state, "infrastructure_failure"),
        )

    if failure_type in POLICY_FAILURE_TYPES or failure_stage in {"planning_risk_approval", "policy_governance", "rollout_governance", "experiment_governance"}:
        return _result(
            status="completed",
            failure_class="policy_governance_failure",
            root_cause="policy",
            primary_attribution="policy",
            contributors=[_contributor("policy", "caused", 0.9, ["root_cause_policy_conflict"])],
            excluded_attributions=[_excluded(agent, "policy_governance_failure") for agent in ("planner", "developer", "repair", "qa")],
            confidence=0.9,
            evidence=evidence,
            reason_codes=["root_cause_policy_conflict"],
            causal_chain=_chain(state, "policy_governance_failure"),
        )

    if terminal == "planning_failed" or failure_type == "planning_validation_failed" or failure_stage == "planning":
        return _result(
            status="completed",
            failure_class="planning_defect",
            root_cause="planner",
            primary_attribution="planner",
            contributors=[_contributor("planner", "caused", 0.95, ["root_cause_planning_failed"])],
            excluded_attributions=[_excluded("developer", "implementation_not_primary"), _excluded("qa", "testing_not_primary")],
            confidence=0.95,
            evidence=evidence,
            reason_codes=["root_cause_planning_failed"],
            causal_chain=_chain(state, "planning_failed"),
        )

    if is_plan_related_failure(state):
        return _result(
            status="completed",
            failure_class="planning_defect",
            root_cause="planner",
            primary_attribution="planner",
            contributors=[_contributor("planner", "caused", 0.85, ["root_cause_plan_target_mismatch"])],
            excluded_attributions=[_excluded("developer", "plan_related_failure")],
            confidence=0.85,
            evidence=evidence,
            reason_codes=["root_cause_plan_target_mismatch"],
            causal_chain=_chain(state, "plan_related_failure"),
        )

    if failure_type in QA_FAILURE_TYPES or failure_stage in {"test_harness", "test_configuration"}:
        return _result(
            status="completed",
            failure_class="testing_defect",
            root_cause="qa",
            primary_attribution="qa",
            contributors=[_contributor("qa", "caused", 0.9, ["root_cause_testing_defect"])],
            excluded_attributions=[_excluded("developer", "testing_defect"), _excluded("planner", "testing_defect")],
            confidence=0.9,
            evidence=evidence,
            reason_codes=["root_cause_testing_defect"],
            causal_chain=_chain(state, "testing_defect"),
        )

    if repair_attempts > 0 and state.get("tests_passed") is not True:
        root = "developer"
        contributors = [
            _contributor("developer", "caused", 0.75, ["root_cause_implementation_defect"]),
            _contributor("qa", "detected", 0.85, ["qa_detected_failure"]),
            _contributor("repair", "failed_to_recover", 0.8, ["repair_failed_to_recover"]),
        ]
        if failure_stage == "execute_fix" or failure_type == "repair_state_guard_rejected":
            contributors.append(_contributor("repair", "amplified", 0.7, ["root_cause_repair_introduced_failure"]))
        return _result(
            status="completed",
            failure_class="implementation_defect",
            root_cause=root,
            primary_attribution=root,
            contributors=contributors,
            excluded_attributions=[_excluded("planner", "planning_valid")],
            confidence=0.75,
            evidence=evidence,
            reason_codes=["root_cause_implementation_validation", "repair_failed_to_recover"],
            causal_chain=_chain(state, "repair_failed_to_recover"),
        )

    if failure_type in IMPLEMENTATION_FAILURE_TYPES or terminal == "implementation_failed" or failure_stage in {"implementation", "implementation_validation", "execute_create_project"}:
        return _result(
            status="completed",
            failure_class="implementation_defect",
            root_cause="developer",
            primary_attribution="developer",
            contributors=[_contributor("developer", "caused", 0.9, ["root_cause_implementation_validation"])],
            excluded_attributions=[_excluded("planner", "planning_valid") if state.get("planning_valid") else _excluded("planner", "not_primary")],
            confidence=0.9,
            evidence=evidence,
            reason_codes=["root_cause_implementation_validation"],
            causal_chain=_chain(state, "implementation_defect"),
        )

    if state.get("tests_executed") and state.get("tests_passed") is False:
        return _result(
            status="completed",
            failure_class="implementation_defect",
            root_cause="developer",
            primary_attribution="developer",
            contributors=[
                _contributor("developer", "caused", 0.7, ["root_cause_unrecovered_test_failure"]),
                _contributor("qa", "detected", 0.8, ["qa_detected_failure"]),
            ],
            excluded_attributions=[_excluded("planner", "planning_valid")],
            confidence=0.7,
            evidence=evidence,
            reason_codes=["root_cause_unrecovered_test_failure"],
            causal_chain=_chain(state, "tests_failed"),
        )

    return _result(
        status="insufficient_evidence",
        failure_class="unknown_failure",
        root_cause="unknown",
        primary_attribution="unknown",
        confidence=0.35,
        evidence=evidence,
        reason_codes=["root_cause_unknown"],
        causal_chain=_chain(state, "unknown_failure"),
    )


async def failure_attribution_node(state: SoftwareFactoryState, _dependencies: Any) -> dict[str, Any]:
    emit_workflow_event(
        WorkflowEventType.STAGE_STARTED,
        source="failure_attribution",
        stage="failure_attribution",
        status=EventStatus.RUNNING,
        data={"event": "failure_attribution_started", "version": FAILURE_ATTRIBUTION_VERSION},
    )
    result = build_failure_attribution(state)
    emit_workflow_event(
        WorkflowEventType.STAGE_COMPLETED,
        source="failure_attribution",
        stage="failure_attribution",
        status=EventStatus.COMPLETED,
        data={
            "event": "failure_attribution_completed",
            "status": result["status"],
            "failure_class": result["failure_class"],
            "root_cause": result["root_cause"],
            "confidence": result["confidence"],
            "contributor_count": len(result["contributors"]),
            "reason_code_count": len(result["reason_codes"]),
            "version": FAILURE_ATTRIBUTION_VERSION,
        },
    )
    return {
        "failure_attribution": result,
        "failure_attribution_version": FAILURE_ATTRIBUTION_VERSION,
    }


def aggregate_failure_attribution(states: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    items = [
        state.get("failure_attribution")
        for state in states
        if isinstance(state.get("failure_attribution"), Mapping)
        and state["failure_attribution"].get("status") == "completed"
    ]
    root_causes = [str(item.get("root_cause") or "unknown") for item in items]
    confidences = [float(item["confidence"]) for item in items if item.get("confidence") is not None]
    contributors = [contributor for item in items for contributor in item.get("contributors", []) if isinstance(contributor, Mapping)]

    def count_root(name: str) -> int:
        return root_causes.count(name)

    def count_contributor(source: str, contribution: str) -> int:
        return sum(
            1
            for item in contributors
            if item.get("source") == source and item.get("contribution") == contribution
        )

    return {
        "total_failures_attributed": len(items),
        "planner_root_cause_count": count_root("planner"),
        "developer_root_cause_count": count_root("developer"),
        "repair_root_cause_count": count_root("repair"),
        "qa_root_cause_count": count_root("qa"),
        "infrastructure_root_cause_count": count_root("infrastructure"),
        "user_root_cause_count": count_root("user"),
        "policy_root_cause_count": count_root("policy"),
        "external_root_cause_count": count_root("external"),
        "unknown_root_cause_count": count_root("unknown"),
        "average_attribution_confidence": None if not confidences else round(sum(confidences) / len(confidences), 4),
        "developer_caused_count": count_contributor("developer", "caused"),
        "repair_resolved_count": count_contributor("repair", "resolved"),
        "qa_detected_count": count_contributor("qa", "detected"),
        "repair_failed_to_recover_count": count_contributor("repair", "failed_to_recover"),
    }
