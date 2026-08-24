from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from host import (
    format_bool,
    format_implementation_result,
    format_planning_result,
    format_testing_result,
    get_preferred_value,
    print_workflow_snapshot,
)


def snapshot(values: dict[str, Any], *, checkpoint_id: str = "checkpoint-1") -> SimpleNamespace:
    return SimpleNamespace(
        thread_id="thread-1",
        checkpoint_id=checkpoint_id,
        values=values,
        next_nodes=[],
        interrupts=(),
    )


def completed_state() -> dict[str, Any]:
    return {
        "project_name": "state-contract-pool-api",
        "workflow_intent": "create_project",
        "terminal_status": "completed",
        "planning_valid": False,
        "planning_attempts": 99,
        "planning_result": {
            "valid": True,
            "attempts": 0,
            "analysis": {
                "project_type": "fastapi",
                "functional_requirements": ["health", "tests"],
            },
            "acceptance_criteria": [{"id": "AC-1"}, {"id": "AC-2"}],
            "tasks": [{"id": "T-1"}, {"id": "T-2"}],
        },
        "implementation_result": {
            "valid": True,
            "attempts": 0,
            "package_name": "state_contract_pool_api",
            "generated_files": [
                "state_contract_pool_api/main.py",
                "tests/test_health.py",
                "requirements.txt",
            ],
            "project_created": True,
            "environment_prepared": True,
            "dependencies_installed": True,
            "framework": "pytest",
        },
        "testing_result": {
            "tests_executed": True,
            "tests_passed": True,
            "summary": "1 passed, 2 warnings",
            "warning_count": 2,
            "repair_phase": "not_started",
            "repair_attempts": 0,
        },
        "pending_tool_name": None,
        "pending_operation": None,
        "pending_approval_preview": {},
    }


def test_get_preferred_value_prioritizes_grouped_over_legacy() -> None:
    assert (
        get_preferred_value(
            grouped={"valid": True},
            grouped_key="valid",
            state={"planning_valid": False},
            legacy_key="planning_valid",
        )
        is True
    )


def test_get_preferred_value_uses_legacy_when_grouped_missing() -> None:
    assert get_preferred_value(grouped=None, grouped_key="valid", state={"planning_valid": False}, legacy_key="planning_valid") is False


def test_get_preferred_value_preserves_false_zero_empty_list_and_none() -> None:
    assert get_preferred_value(grouped={"value": False}, grouped_key="value", state={"legacy": True}, legacy_key="legacy") is False
    assert get_preferred_value(grouped={"value": 0}, grouped_key="value", state={"legacy": 3}, legacy_key="legacy") == 0
    assert get_preferred_value(grouped={"value": []}, grouped_key="value", state={"legacy": [1]}, legacy_key="legacy") == []
    assert get_preferred_value(grouped={"value": None}, grouped_key="value", state={"legacy": "fallback"}, legacy_key="legacy") is None


def test_format_bool_uses_lowercase_or_na() -> None:
    assert format_bool(True) == "true"
    assert format_bool(False) == "false"
    assert format_bool(None) == "n/a"


def test_format_planning_result_shows_grouped_values() -> None:
    lines = format_planning_result(completed_state())

    assert "- valid: true" in lines
    assert "- attempts: 0" in lines
    assert "- project type: fastapi" in lines
    assert "- functional requirements: 2" in lines
    assert "- acceptance criteria: 2" in lines
    assert "- tasks: 2" in lines


def test_format_implementation_result_shows_grouped_values_and_file_count() -> None:
    lines = format_implementation_result(completed_state())

    assert "- valid: true" in lines
    assert "- package: state_contract_pool_api" in lines
    assert "- generated files: 3" in lines
    assert "- project created: true" in lines
    assert "- environment prepared: true" in lines
    assert "- dependencies installed: true" in lines
    assert "- framework: pytest" in lines


def test_generated_files_integer_is_shown_directly() -> None:
    state = completed_state()
    state["implementation_result"]["generated_files"] = 5

    assert "- generated files: 5" in format_implementation_result(state)


def test_format_testing_result_shows_grouped_values() -> None:
    lines = format_testing_result(completed_state())

    assert "- executed: true" in lines
    assert "- passed: true" in lines
    assert "- summary: 1 passed, 2 warnings" in lines
    assert "- warnings: 2" in lines
    assert "- repair phase: not_started" in lines
    assert "- repair attempts: 0" in lines


def test_old_workflow_without_grouped_results_uses_legacy_fields(capsys) -> None:
    values = {
        "project_name": "old-api",
        "workflow_intent": "create_project",
        "terminal_status": "completed",
        "planning_valid": True,
        "planning_attempts": 0,
        "requirement_analysis": {"project_type": "fastapi", "functional_requirements": ["health"]},
        "acceptance_criteria": [{"id": "AC-1"}],
        "implementation_tasks": [{"id": "T-1"}],
        "implementation_valid": True,
        "implementation_attempts": 0,
        "generated_package_name": "old_api",
        "generated_files": ["old_api/main.py"],
        "project_created": True,
        "environment_prepared": True,
        "dependencies_installed": True,
        "detected_test_framework": "pytest",
        "tests_executed": True,
        "tests_passed": True,
        "final_test_result_summary": "1 passed",
        "test_warning_count": 0,
        "repair_phase": "not_started",
        "repair_attempts": 0,
    }

    print_workflow_snapshot(snapshot(values))

    output = capsys.readouterr().out
    assert "Planning result:" in output
    assert "Implementation result:" in output
    assert "Testing result:" in output
    assert "- package: old_api" in output
    assert "- warnings: 0" in output


def test_partial_workflow_snapshot_uses_na_without_crashing(capsys) -> None:
    print_workflow_snapshot(
        snapshot(
            {
                "project_name": "partial-api",
                "workflow_intent": "create_project",
                "planning_result": {"valid": False, "attempts": 0},
                "tests_executed": False,
            }
        )
    )

    output = capsys.readouterr().out
    assert "- valid: false" in output
    assert "- attempts: 0" in output
    assert "Implementation result:\n- valid: n/a" in output
    assert "Testing result:\n- executed: false" in output


def test_repair_workflow_snapshot_shows_repair_phase(capsys) -> None:
    values = completed_state()
    values["testing_result"] = {
        "tests_executed": True,
        "tests_passed": False,
        "summary": "1 failed",
        "repair_phase": "apply_fix",
        "repair_attempts": 0,
    }

    print_workflow_snapshot(snapshot(values))

    output = capsys.readouterr().out
    assert "- passed: false" in output
    assert "- repair phase: apply_fix" in output
    assert "- repair attempts: 0" in output


def test_snapshot_presentation_does_not_print_generated_file_contents(capsys) -> None:
    values = completed_state()
    values["implementation_result"]["generated_files"] = [
        {"path": "state_contract_pool_api/main.py", "content": "SECRET_SOURCE_CONTENT"}
    ]

    print_workflow_snapshot(snapshot(values))

    output = capsys.readouterr().out
    assert "SECRET_SOURCE_CONTENT" not in output
    assert "- generated files: 1" in output


def test_print_workflow_snapshot_does_not_modify_state(capsys) -> None:
    values = completed_state()
    before = deepcopy(values)

    print_workflow_snapshot(snapshot(values, checkpoint_id="before-after"))

    assert values == before
    assert "Checkpoint: before-after" in capsys.readouterr().out

