from __future__ import annotations

from collections.abc import Collection
from copy import deepcopy
from typing import Any, Mapping

from graph.state import SoftwareFactoryState
from graph.subgraphs.implementation.state import (
    IMPLEMENTATION_OUTPUT_KEYS,
    IMPLEMENTATION_STATE_FIELDS,
    ImplementationState,
)
from graph.subgraphs.planning.state import PLANNING_OUTPUT_KEYS, PLANNING_STATE_FIELDS, PlanningState
from graph.subgraphs.testing_repair.state import (
    TESTING_REPAIR_INPUT_KEYS,
    TESTING_REPAIR_OUTPUT_KEYS,
    TESTING_REPAIR_STATE_FIELDS,
    TestingRepairState,
)


IDENTITY_FIELDS = {"original_user_message", "project_name", "workflow_intent"}
STRICT_SUBGRAPH_CONTRACTS = True


class SubgraphContractViolation(ValueError):
    def __init__(self, subgraph_name: str, keys: Collection[str]) -> None:
        self.subgraph_name = subgraph_name
        self.keys = tuple(sorted(keys))
        super().__init__(
            f"{subgraph_name} produced fields outside its output contract: {', '.join(self.keys)}"
        )


def _select(source: Mapping[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in source.items() if key in allowed}


def select_allowed_fields(
    result: Mapping[str, Any],
    allowed_keys: Collection[str],
) -> dict[str, Any]:
    return {
        key: deepcopy(result[key])
        for key in allowed_keys
        if key in result
    }


def _dropped_output_keys(
    result: Mapping[str, Any],
    allowed_keys: Collection[str],
    ignored_keys: Collection[str] = (),
) -> list[str]:
    allowed = set(allowed_keys)
    ignored = set(ignored_keys)
    return sorted(key for key in result if key not in allowed and key not in ignored)


def _handle_dropped_keys(
    subgraph_name: str,
    dropped: list[str],
    *,
    strict: bool,
    debug: bool,
) -> None:
    if not dropped:
        return
    if strict:
        raise SubgraphContractViolation(subgraph_name, dropped)
    if debug:
        print(f"Dropped disallowed {subgraph_name} output keys:")
        for key in dropped:
            print(f"- {key}")


def to_planning_input(parent_state: SoftwareFactoryState) -> PlanningState:
    return PlanningState(**_select(parent_state, PLANNING_STATE_FIELDS))


def from_planning_output(
    result: Mapping[str, Any],
    *,
    strict: bool = False,
    debug: bool = False,
) -> dict[str, Any]:
    ignored = PLANNING_STATE_FIELDS - PLANNING_OUTPUT_KEYS
    dropped = _dropped_output_keys(result, PLANNING_OUTPUT_KEYS, ignored)
    _handle_dropped_keys("Planning", dropped, strict=strict, debug=debug)
    updates = select_allowed_fields(result, PLANNING_OUTPUT_KEYS)
    updates["analysis_completed"] = bool(updates.get("requirement_analysis"))
    updates["tasks_created"] = bool(updates.get("implementation_tasks"))
    updates["planning_result"] = {
        "analysis": deepcopy(updates.get("requirement_analysis")),
        "acceptance_criteria": deepcopy(updates.get("acceptance_criteria", [])),
        "tasks": deepcopy(updates.get("implementation_tasks", [])),
        "valid": bool(updates.get("planning_valid")),
        "attempts": int(updates.get("planning_attempts", 0)),
        "execution_order": deepcopy(updates.get("planning_execution_order", [])),
        "dependency_edges": deepcopy(updates.get("planning_dependency_edges", [])),
        "risk": {
            "score": updates.get("planning_risk_score"),
            "level": updates.get("planning_risk_level"),
            "reasons": deepcopy(updates.get("planning_risk_reasons", [])),
            "sensitive_tasks": deepcopy(updates.get("planning_sensitive_tasks", [])),
            "impact_areas": deepcopy(updates.get("planning_impact_areas", [])),
        },
        "approval": {
            "required": bool(updates.get("planning_approval_required", False)),
            "status": updates.get("planning_approval_status", "not_required"),
            "approval_id": updates.get("planning_approval_id"),
            "fingerprint": updates.get("planning_approval_fingerprint"),
            "reason": updates.get("planning_approval_reason"),
            "policy_decision": updates.get("planning_policy_decision"),
        },
        "quality": {
            "score": updates.get("planning_quality_score"),
            "level": updates.get("planning_quality_level"),
            "dimensions": deepcopy(updates.get("planning_quality_dimensions", {})),
            "issues": deepcopy(updates.get("planning_quality_issues", [])),
            "decision_confidence": updates.get("planning_decision_confidence"),
            "version": updates.get("planning_quality_version"),
            "gate": {
                "decision": updates.get("planning_quality_gate_decision"),
                "reason": updates.get("planning_quality_gate_reason"),
                "refinement_required": bool(updates.get("planning_quality_refinement_required", False)),
                "refinement_attempts": int(updates.get("planning_quality_refinement_attempts", 0)),
                "previous_score": updates.get("planning_quality_previous_score"),
                "score_delta": updates.get("planning_quality_score_delta"),
            },
        },
        "knowledge": {
            "state": updates.get("planner_knowledge_state", "not_started"),
            "query": updates.get("planner_knowledge_query"),
            "retrieval_id": updates.get("planner_knowledge_retrieval_id"),
            "sources": deepcopy(updates.get("planner_knowledge_sources", [])),
            "used": bool(updates.get("planner_knowledge_retrieval_used")),
            "retrieved_context_count": int(
                updates.get("planner_retrieved_context_count", 0)
            ),
            "context_tokens": int(updates.get("planner_knowledge_context_tokens", 0)),
        },
    }
    return updates


def to_implementation_input(parent_state: SoftwareFactoryState) -> ImplementationState:
    values = _select(parent_state, IMPLEMENTATION_STATE_FIELDS)
    planning = parent_state.get("planning_result") or {}
    values.setdefault("requirement_analysis", deepcopy(planning.get("analysis")))
    values.setdefault("acceptance_criteria", deepcopy(planning.get("acceptance_criteria", [])))
    values.setdefault("implementation_tasks", deepcopy(planning.get("tasks", [])))
    return ImplementationState(**values)


def from_implementation_output(
    result: Mapping[str, Any],
    *,
    strict: bool = False,
    debug: bool = False,
) -> dict[str, Any]:
    ignored = IMPLEMENTATION_STATE_FIELDS - IMPLEMENTATION_OUTPUT_KEYS
    dropped = _dropped_output_keys(result, IMPLEMENTATION_OUTPUT_KEYS, ignored)
    _handle_dropped_keys("Implementation", dropped, strict=strict, debug=debug)
    updates = select_allowed_fields(result, IMPLEMENTATION_OUTPUT_KEYS)
    updates["implementation_result"] = {
        "package_name": updates.get("generated_package_name"),
        "project_implementation": deepcopy(updates.get("project_implementation")),
        "generated_files": deepcopy(updates.get("generated_files", [])),
        "project_created": bool(updates.get("project_created")),
        "environment_prepared": bool(updates.get("environment_prepared")),
        "framework": updates.get("detected_test_framework"),
        "valid": bool(updates.get("implementation_valid")),
        "attempts": int(updates.get("implementation_attempts", 0)),
        "knowledge": {
            "state": updates.get("developer_knowledge_state", "not_started"),
            "query": updates.get("developer_knowledge_query"),
            "retrieval_id": updates.get("developer_knowledge_retrieval_id"),
            "sources": deepcopy(updates.get("developer_knowledge_sources", [])),
            "used": bool(updates.get("knowledge_retrieval_used")),
            "retrieved_context_count": int(updates.get("retrieved_context_count", 0)),
            "context_tokens": int(updates.get("knowledge_context_tokens", 0)),
        },
    }
    return updates


def to_testing_input(parent_state: SoftwareFactoryState) -> TestingRepairState:
    values = _select(parent_state, TESTING_REPAIR_INPUT_KEYS)
    planning = parent_state.get("planning_result") or {}
    implementation = parent_state.get("implementation_result") or {}
    values.setdefault("requirement_analysis", deepcopy(planning.get("analysis")))
    values.setdefault(
        "acceptance_criteria", deepcopy(planning.get("acceptance_criteria", []))
    )
    values.setdefault("implementation_result", deepcopy(implementation))
    values.setdefault(
        "generated_files", deepcopy(implementation.get("generated_files", []))
    )
    values.setdefault("detected_test_framework", implementation.get("framework"))
    return TestingRepairState(**values)


def from_testing_output(
    result: Mapping[str, Any],
    *,
    strict: bool = False,
    debug: bool = False,
) -> dict[str, Any]:
    ignored = TESTING_REPAIR_INPUT_KEYS | IDENTITY_FIELDS | {"last_completed_node"}
    dropped = _dropped_output_keys(result, TESTING_REPAIR_OUTPUT_KEYS, ignored)
    _handle_dropped_keys("TestingRepair", dropped, strict=strict, debug=debug)
    updates = select_allowed_fields(result, TESTING_REPAIR_OUTPUT_KEYS)
    updates["testing_result"] = {
        "tests_executed": result.get("tests_executed", False),
        "tests_passed": result.get("tests_passed", False),
        "summary": result.get("final_test_result_summary"),
        "repair_phase": result.get("repair_phase", "not_started"),
        "repair_attempts": result.get("repair_attempts", 0),
        "knowledge": {
            "state": updates.get("qa_knowledge_state", "not_started"),
            "query": updates.get("qa_knowledge_query"),
            "retrieval_id": updates.get("qa_knowledge_retrieval_id"),
            "sources": deepcopy(updates.get("qa_knowledge_sources", [])),
            "used": bool(updates.get("qa_knowledge_retrieval_used")),
            "retrieved_context_count": int(
                updates.get("qa_retrieved_context_count", 0)
            ),
            "context_tokens": int(updates.get("qa_knowledge_context_tokens", 0)),
        },
        "repair_knowledge": {
            "state": updates.get("repair_knowledge_state", "not_started"),
            "query": updates.get("repair_knowledge_query"),
            "retrieval_id": updates.get("repair_knowledge_retrieval_id"),
            "sources": deepcopy(updates.get("repair_knowledge_sources", [])),
            "used": bool(updates.get("repair_knowledge_retrieval_used")),
            "retrieved_context_count": int(
                updates.get("repair_retrieved_context_count", 0)
            ),
            "context_tokens": int(
                updates.get("repair_knowledge_context_tokens", 0)
            ),
        },
    }
    return updates
