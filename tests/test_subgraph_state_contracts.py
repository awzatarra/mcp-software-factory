from __future__ import annotations

import json

import pytest

from graph.state import create_initial_state
from graph.debug import state_debug_summary
from graph.subgraph_adapters import (
    STRICT_SUBGRAPH_CONTRACTS,
    SubgraphContractViolation,
    from_implementation_output,
    from_planning_output,
    from_testing_output,
    select_allowed_fields,
    to_implementation_input,
    to_planning_input,
    to_testing_input,
)
from graph.nodes import build_final_summary
from graph.subgraphs.implementation.state import (
    IMPLEMENTATION_OUTPUT_KEYS,
    IMPLEMENTATION_STATE_FIELDS,
)
from graph.subgraphs.planning.state import PLANNING_STATE_FIELDS
from graph.subgraphs.testing_repair.state import TESTING_REPAIR_STATE_FIELDS


def populated_parent_state():
    state = create_initial_state("Crea contract-api")
    state.update(
        project_name="contract-api",
        requirement_analysis={"project_name": "contract-api"},
        acceptance_criteria=[{"id": "AC-1"}],
        implementation_tasks=[{"id": "TASK-1"}],
        planning_valid=True,
        generated_files=["contract_api/main.py"],
        project_implementation={
            "files": [{"path": "contract_api/main.py", "content": "private source"}],
        },
        analysis_completed=True,
        tasks_created=True,
        workspace_inspected=True,
        project_created=True,
        dependencies_installed=True,
        detected_test_framework="pytest",
        expected_test_command=["python", "-m", "pytest"],
        tests_passed=True,
        test_stdout="sensitive output",
        repair_phase="completed",
        repair_attempts=1,
    )
    return state


def test_private_states_have_disjoint_domain_boundaries() -> None:
    assert "tests_passed" not in PLANNING_STATE_FIELDS
    assert "repair_phase" not in IMPLEMENTATION_STATE_FIELDS
    assert "implementation_tasks" not in TESTING_REPAIR_STATE_FIELDS
    assert "generated_files" in TESTING_REPAIR_STATE_FIELDS
    assert "project_implementation" not in TESTING_REPAIR_STATE_FIELDS
    assert "test_stdout" not in IMPLEMENTATION_STATE_FIELDS


def test_planning_input_excludes_testing_and_repair_data() -> None:
    result = to_planning_input(populated_parent_state())
    assert set(result) <= PLANNING_STATE_FIELDS
    assert "tests_passed" not in result
    assert "repair_phase" not in result
    assert "repair_before" not in result
    assert "repair_after" not in result


def test_implementation_input_contains_plan_but_excludes_test_logs() -> None:
    result = to_implementation_input(populated_parent_state())
    assert set(result) <= IMPLEMENTATION_STATE_FIELDS
    assert result["implementation_tasks"] == [{"id": "TASK-1"}]
    assert "test_stdout" not in result
    assert "repair_phase" not in result


def test_testing_input_contains_safe_qa_evidence_without_developer_contents() -> None:
    result = to_testing_input(populated_parent_state())
    assert set(result) <= TESTING_REPAIR_STATE_FIELDS
    assert "implementation_tasks" not in result
    assert result["generated_files"] == ["contract_api/main.py"]
    assert result["requirement_analysis"] == {"project_name": "contract-api"}
    assert result["acceptance_criteria"] == [{"id": "AC-1"}]
    assert "project_implementation" not in result
    assert "planning_valid" not in result


def test_unknown_subgraph_outputs_cannot_modify_parent() -> None:
    assert "tests_passed" not in from_planning_output(
        {"planning_valid": True, "tests_passed": True}
    )
    assert "repair_phase" not in from_implementation_output(
        {"implementation_valid": True, "repair_phase": "completed"}
    )
    assert "implementation_tasks" not in from_testing_output(
        {"tests_passed": True, "implementation_tasks": [{"id": "injected"}]}
    )
    assert "project_created" not in from_testing_output(
        {"tests_passed": True, "project_created": False}
    )
    assert "dependencies_installed" not in from_testing_output(
        {"tests_passed": True, "dependencies_installed": False}
    )
    assert "analysis_completed" not in from_testing_output(
        {"tests_passed": True, "analysis_completed": False}
    )


def test_select_allowed_fields_returns_only_contract_keys() -> None:
    result = select_allowed_fields({"tests_passed": True, "project_created": False}, {"tests_passed"})

    assert result == {"tests_passed": True}


def test_implementation_output_filter_preserves_project_implementation() -> None:
    proposal = {
        "project_name": "contract-api",
        "framework": "fastapi",
        "package_name": "contract_api",
        "files": [{"path": "contract_api/main.py", "content": "source"}],
    }

    result = from_implementation_output(
        {
            "project_implementation": proposal,
            "generated_package_name": "contract_api",
            "implementation_valid": True,
        },
        strict=True,
    )

    assert "project_implementation" in IMPLEMENTATION_OUTPUT_KEYS
    assert result["project_implementation"] == proposal
    assert result["implementation_result"]["project_implementation"] == proposal


def test_strict_contract_mode_detects_disallowed_outputs() -> None:
    assert STRICT_SUBGRAPH_CONTRACTS is True
    with pytest.raises(SubgraphContractViolation) as exc:
        from_testing_output(
            {"tests_passed": True, "project_created": False},
            strict=STRICT_SUBGRAPH_CONTRACTS,
        )

    assert exc.value.subgraph_name == "TestingRepair"
    assert exc.value.keys == ("project_created",)


def test_debug_logs_dropped_disallowed_testing_keys(capsys: pytest.CaptureFixture[str]) -> None:
    updates = from_testing_output(
        {
            "tests_passed": True,
            "project_created": False,
            "implementation_tasks": [],
        },
        debug=True,
    )

    output = capsys.readouterr().out
    assert "Dropped disallowed TestingRepair output keys:" in output
    assert "- project_created" in output
    assert "- implementation_tasks" in output
    assert updates["tests_passed"] is True
    assert "project_created" not in updates
    assert "implementation_tasks" not in updates


def test_testing_output_preserves_existing_parent_fields_by_not_publishing_them() -> None:
    parent = populated_parent_state()
    malicious = {
        "analysis_completed": False,
        "project_created": False,
        "dependencies_installed": False,
        "tests_passed": True,
        "repair_phase": "not_started",
    }

    parent.update(from_testing_output(malicious))

    assert parent["analysis_completed"] is True
    assert parent["tasks_created"] is True
    assert parent["workspace_inspected"] is True
    assert parent["project_created"] is True
    assert parent["dependencies_installed"] is True
    assert parent["tests_passed"] is True
    assert parent["repair_phase"] == "not_started"


def test_grouped_results_and_private_inputs_are_json_serializable() -> None:
    planning = from_planning_output(
        {
            "requirement_analysis": {"project_name": "contract-api"},
            "acceptance_criteria": [],
            "implementation_tasks": [],
            "planning_valid": True,
            "planning_attempts": 0,
            "planner_knowledge_query": "service layer architecture",
            "planner_knowledge_retrieval_id": "planner-retrieval-1",
            "planner_knowledge_sources": [
                {
                    "knowledge_id": "planner-knowledge-1",
                    "chunk_id": "planner-chunk-1",
                    "source_reference": "architecture:service-layer",
                    "version": 1,
                }
            ],
            "planner_knowledge_state": "available",
            "planner_knowledge_retrieval_used": True,
            "planner_retrieved_context_count": 1,
            "planner_knowledge_context_tokens": 37,
        }
    )
    implementation = from_implementation_output(
        {
            "generated_package_name": "contract_api",
            "generated_files": ["contract_api/main.py"],
            "project_created": True,
            "environment_prepared": True,
            "detected_test_framework": "pytest",
            "developer_knowledge_query": "database migration integration tests",
            "developer_knowledge_retrieval_id": "retrieval-1",
            "developer_knowledge_sources": [
                {
                    "knowledge_id": "knowledge-1",
                    "chunk_id": "chunk-1",
                    "source_reference": "docs:migrations",
                    "version": 1,
                }
            ],
            "developer_knowledge_state": "available",
            "knowledge_retrieval_used": True,
            "retrieved_context_count": 1,
            "knowledge_context_tokens": 42,
        }
    )
    testing = from_testing_output(
        {
            "tests_executed": True,
            "tests_passed": True,
            "final_test_result_summary": "1 passed",
            "repair_phase": "not_started",
            "repair_attempts": 0,
            "qa_knowledge_retrieval_id": "qa-retrieval-1",
            "qa_knowledge_sources": [
                {
                    "knowledge_id": "qa-knowledge-1",
                    "chunk_id": "qa-chunk-1",
                    "source_reference": "testing:integration",
                    "version": 1,
                }
            ],
            "qa_knowledge_state": "available",
            "qa_knowledge_retrieval_used": True,
            "qa_retrieved_context_count": 1,
            "qa_knowledge_context_tokens": 31,
        }
    )

    encoded = json.dumps(
        {
            "planning": planning,
            "implementation": implementation,
            "testing": testing,
            "private_input": to_testing_input(populated_parent_state()),
        }
    )
    assert "planning_result" in encoded
    assert "implementation_result" in encoded
    assert "testing_result" in encoded
    assert testing["testing_result"] == {
        "tests_executed": True,
        "tests_passed": True,
        "summary": "1 passed",
        "repair_phase": "not_started",
        "repair_attempts": 0,
        "knowledge": {
            "state": "available",
            "query": None,
            "retrieval_id": "qa-retrieval-1",
            "sources": [
                {
                    "knowledge_id": "qa-knowledge-1",
                    "chunk_id": "qa-chunk-1",
                    "source_reference": "testing:integration",
                    "version": 1,
                }
            ],
            "used": True,
            "retrieved_context_count": 1,
            "context_tokens": 31,
        },
        "repair_knowledge": {
            "state": "not_started",
            "query": None,
            "retrieval_id": None,
            "sources": [],
            "used": False,
            "retrieved_context_count": 0,
            "context_tokens": 0,
        },
    }
    assert implementation["implementation_result"]["knowledge"] == {
        "state": "available",
        "query": "database migration integration tests",
        "retrieval_id": "retrieval-1",
        "sources": [
            {
                "knowledge_id": "knowledge-1",
                "chunk_id": "chunk-1",
                "source_reference": "docs:migrations",
                "version": 1,
            }
        ],
        "used": True,
        "retrieved_context_count": 1,
        "context_tokens": 42,
    }
    assert planning["planning_result"]["knowledge"] == {
        "state": "available",
        "query": "service layer architecture",
        "retrieval_id": "planner-retrieval-1",
        "sources": [
            {
                "knowledge_id": "planner-knowledge-1",
                "chunk_id": "planner-chunk-1",
                "source_reference": "architecture:service-layer",
                "version": 1,
            }
        ],
        "used": True,
        "retrieved_context_count": 1,
        "context_tokens": 37,
    }


def test_finalize_prefers_grouped_results_over_legacy_flat_fields() -> None:
    state = populated_parent_state()
    state.update(
        testing_result={
            "tests_executed": True,
            "tests_passed": True,
            "summary": "1 passed from grouped",
            "repair_phase": "not_started",
            "repair_attempts": 0,
        },
        implementation_result={
            "project_created": True,
            "environment_prepared": True,
            "framework": "pytest",
        },
        tests_executed=True,
        final_test_result_summary="legacy should not win",
    )

    summary = build_final_summary(state)

    assert "1 passed from grouped" in summary
    assert "legacy should not win" not in summary


def test_debug_summary_excludes_messages_logs_and_file_contents() -> None:
    state = populated_parent_state()
    state.update(
        original_user_message="private requirement",
        test_stdout="private test log",
        project_implementation={
            "files": [{"path": "secret.py", "content": "private source"}]
        },
    )

    serialized = json.dumps(state_debug_summary(state))

    assert "private requirement" not in serialized
    assert "private test log" not in serialized
    assert "private source" not in serialized
