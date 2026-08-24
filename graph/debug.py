from __future__ import annotations

from typing import Any, Mapping


def state_debug_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    planning = state.get("planning_result") or {}
    implementation = state.get("implementation_result") or {}
    testing = state.get("testing_result") or {}
    return {
        "state_keys": sorted(state),
        "project_name": state.get("project_name"),
        "workflow_intent": state.get("workflow_intent"),
        "terminal_status": state.get("terminal_status"),
        "failure_type": state.get("failure_type"),
        "failure_stage": state.get("failure_stage"),
        "planning_result": {
            "valid": planning.get("valid"),
            "attempts": planning.get("attempts"),
        },
        "implementation_result": {
            "package_name": implementation.get("package_name"),
            "generated_file_count": len(implementation.get("generated_files") or []),
            "project_created": implementation.get("project_created"),
            "environment_prepared": implementation.get("environment_prepared"),
            "framework": implementation.get("framework"),
        },
        "testing_result": {
            "tests_passed": testing.get("tests_passed"),
            "repair_phase": testing.get("repair_phase"),
            "repair_attempts": testing.get("repair_attempts"),
        },
        "pending_operation": state.get("pending_operation"),
        "pending_tool_name": state.get("pending_tool_name"),
    }
