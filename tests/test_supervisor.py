from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from graph.checkpointing import create_sqlite_checkpointer
from graph.runtime import thread_config
from graph.state import SoftwareFactoryState, create_initial_state
from graph.nodes import terminal_status_from_state
from graph.supervisor.adapters import from_supervisor_output, to_supervisor_input
from graph.supervisor.config import (
    SupervisorDevelopmentConfig,
    SupervisorDevelopmentConfigError,
)
from graph.supervisor.guards import (
    approval_blocks_supervisor,
    detect_supervisor_loop,
    determine_allowed_handoffs,
    resolve_mandatory_handoff,
    supervisor_progress_fingerprint,
)
from graph.supervisor.models import SupervisorDecision, SupervisorTarget
from graph.supervisor.routers import route_after_supervisor
from graph.supervisor.service import MAX_HANDOFF_HISTORY, SupervisorService, supervisor_node
from graph.supervisor.state import SupervisorState
from graph.supervisor.validators import validate_supervisor_decision
from host import format_supervisor_result


class RecordingSupervisorService:
    def __init__(
        self,
        result: SupervisorDecision | Exception,
        config: SupervisorDevelopmentConfig | None = None,
    ) -> None:
        self.result = result
        self.calls = 0
        self.config = config or SupervisorDevelopmentConfig()

    async def decide(self, state, allowed_handoffs):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def state_with(**updates: Any) -> SoftwareFactoryState:
    state = create_initial_state("Crea una API FastAPI llamada health-api")
    state.update(project_name="health-api", **updates)
    return state


def test_supervisor_state_is_private_and_excludes_sensitive_payloads() -> None:
    fields = set(SupervisorState.__annotations__)
    assert "planning_summary" in fields
    assert "implementation_summary" in fields
    assert "testing_summary" in fields
    assert "generated_files" not in fields
    assert "test_stdout" not in fields
    assert "test_stderr" not in fields
    assert "pending_tool_arguments" not in fields


def test_supervisor_models_are_strict() -> None:
    decision = SupervisorDecision(target="planning", reason="Continue planning.", confidence=0.8)
    assert decision.target is SupervisorTarget.PLANNING
    with pytest.raises(ValidationError):
        SupervisorDecision(target="planning", reason="", confidence=0.8)
    with pytest.raises(ValidationError):
        SupervisorDecision(target="planning", reason="ok", confidence=1.1)
    with pytest.raises(ValidationError):
        SupervisorDecision(target="planning", reason="ok", confidence=1.0, extra=True)


def test_development_hooks_are_ignored_outside_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGGRAPH_DEVELOPMENT", "false")
    monkeypatch.setenv("SUPERVISOR_FORCE_MODEL_DECISION", "true")
    monkeypatch.setenv("SUPERVISOR_FORCE_TIMEOUT", "true")
    monkeypatch.setenv("SUPERVISOR_FORCE_INVALID_TARGET", "true")
    monkeypatch.setenv("SUPERVISOR_FORCE_TARGET", "planning")
    monkeypatch.setenv("SUPERVISOR_FORCE_STAGNANT_LOOP", "true")

    config = SupervisorDevelopmentConfig.from_env()

    assert config == SupervisorDevelopmentConfig()


@pytest.mark.parametrize(
    "first,second",
    [
        ("SUPERVISOR_FORCE_TIMEOUT", "SUPERVISOR_FORCE_INVALID_TARGET"),
        ("SUPERVISOR_FORCE_TIMEOUT", "SUPERVISOR_FORCE_STAGNANT_LOOP"),
        ("SUPERVISOR_FORCE_INVALID_TARGET", "SUPERVISOR_FORCE_TARGET"),
        ("SUPERVISOR_FORCE_TARGET", "SUPERVISOR_FORCE_STAGNANT_LOOP"),
    ],
)
def test_conflicting_development_hooks_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    second: str,
) -> None:
    monkeypatch.setenv("LANGGRAPH_DEVELOPMENT", "true")
    monkeypatch.setenv(first, "planning" if first == "SUPERVISOR_FORCE_TARGET" else "true")
    monkeypatch.setenv(second, "planning" if second == "SUPERVISOR_FORCE_TARGET" else "true")
    with pytest.raises(
        SupervisorDevelopmentConfigError,
        match="supervisor_development_config_conflict",
    ):
        SupervisorDevelopmentConfig.from_env()


def test_development_configuration_is_a_single_loaded_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGGRAPH_DEVELOPMENT", "true")
    monkeypatch.setenv("SUPERVISOR_FORCE_MODEL_DECISION", "true")
    monkeypatch.setenv("SUPERVISOR_FORCE_TIMEOUT", "true")
    config = SupervisorDevelopmentConfig.from_env()
    monkeypatch.setenv("SUPERVISOR_FORCE_TIMEOUT", "false")

    assert config.force_model_decision is True
    assert config.force_timeout is True


def test_supervisor_adapter_uses_only_summaries_without_file_or_process_content() -> None:
    state = state_with(
        planning_result={
            "valid": True,
            "attempts": 1,
            "analysis": {
                "project_type": "fastapi",
                "functional_requirements": ["health"],
            },
            "acceptance_criteria": [{"id": "AC-1"}],
            "tasks": [{"order": 1}],
        },
        implementation_result={
            "valid": True,
            "generated_files": [{"path": "main.py", "content": "SECRET-CONTENT"}],
            "project_created": True,
            "environment_prepared": True,
            "framework": "pytest",
        },
        testing_result={
            "tests_executed": True,
            "tests_passed": False,
            "summary": "1 failed",
        },
        test_stdout="FULL-STDOUT",
        test_stderr="FULL-STDERR",
        pending_tool_arguments={"files": [{"content": "PENDING-SECRET"}]},
    )

    private = to_supervisor_input(state)
    serialized = json.dumps(private)

    assert private["planning_summary"]["functional_requirement_count"] == 1
    assert private["implementation_summary"]["project_created"] is True
    assert private["testing_summary"]["summary"] == "1 failed"
    assert "SECRET-CONTENT" not in serialized
    assert "FULL-STDOUT" not in serialized
    assert "FULL-STDERR" not in serialized
    assert "PENDING-SECRET" not in serialized


def test_supervisor_output_cannot_modify_subgraph_results() -> None:
    output = from_supervisor_output(
        {
            "supervisor_decision": "finalize",
            "supervisor_reason": "done",
            "supervisor_confidence": 1.0,
            "planning_result": {"valid": False},
            "implementation_result": {"valid": False},
            "testing_result": {"tests_passed": False},
        }  # type: ignore[arg-type]
    )
    assert output == {
        "supervisor_decision": "finalize",
        "supervisor_reason": "done",
        "supervisor_confidence": 1.0,
    }


@pytest.mark.parametrize(
    "state,expected",
    [
        (state_with(), ["planning"]),
        (
            state_with(workflow_intent="review_existing_project"),
            ["inspect_workspace"],
        ),
        (
            state_with(planning_valid=True, planning_result={"valid": True}),
            ["inspect_workspace"],
        ),
        (
            state_with(
                planning_valid=True,
                planning_result={"valid": True},
                workspace_inspected=True,
            ),
            ["implementation"],
        ),
        (
            state_with(
                planning_valid=True,
                workspace_inspected=True,
                environment_prepared=True,
                project_created=True,
                detected_test_framework="pytest",
            ),
            ["testing_repair"],
        ),
        (
            state_with(tests_executed=True, tests_passed=True),
            ["finalize"],
        ),
    ],
)
def test_allowed_handoffs_follow_workflow_guards(
    state: SoftwareFactoryState,
    expected: list[str],
) -> None:
    assert [target.value for target in determine_allowed_handoffs(state)] == expected


def test_pending_approval_blocks_supervisor() -> None:
    state = state_with(pending_approval_status="waiting")
    assert approval_blocks_supervisor(state) is True
    assert determine_allowed_handoffs(state) == []
    assert resolve_mandatory_handoff(state, []) is None


def test_supervisor_validator_rejects_unknown_and_incompatible_targets() -> None:
    state = state_with()
    unknown = SupervisorDecision.model_construct(
        target="unknown",
        reason="invalid",
        confidence=1.0,
    )
    incompatible = SupervisorDecision(
        target=SupervisorTarget.IMPLEMENTATION,
        reason="skip planning",
        confidence=0.5,
    )
    allowed = [SupervisorTarget.PLANNING, SupervisorTarget.FINALIZE]
    assert "target_not_allowed" in validate_supervisor_decision(unknown, allowed, state)
    errors = validate_supervisor_decision(incompatible, allowed, state)
    assert "target_not_allowed" in errors
    assert "supervisor_implementation_without_plan" in errors
    assert route_after_supervisor({"supervisor_decision": "unknown"}) == "finalize"


def test_valid_implementation_cannot_finalize_before_tests() -> None:
    state = state_with(
        planning_valid=True,
        planning_result={"valid": True},
        workspace_inspected=True,
        implementation_valid=True,
        implementation_result={"valid": True, "project_created": True, "environment_prepared": True},
        project_created=True,
        environment_prepared=True,
        detected_test_framework="pytest",
        tests_executed=False,
        tests_passed=False,
    )

    allowed = determine_allowed_handoffs(state)

    assert allowed == [SupervisorTarget.TESTING_REPAIR]
    assert resolve_mandatory_handoff(state, allowed) is SupervisorTarget.TESTING_REPAIR


def test_git_init_cannot_finalize_before_workflow_branch() -> None:
    state = state_with(
        planning_valid=True,
        planning_result={"valid": True},
        workspace_inspected=True,
        implementation_valid=True,
        implementation_result={"valid": True, "project_created": True, "environment_prepared": True},
        project_created=True,
        environment_prepared=True,
        detected_test_framework="pytest",
        git_integration_enabled=True,
        git_workflow_state="workspace_prepared",
        git_workflow_branch=None,
    )

    allowed = determine_allowed_handoffs(state)

    assert allowed == [SupervisorTarget.GIT_WORKFLOW]
    assert SupervisorTarget.FINALIZE not in allowed


def test_workspace_prepared_routes_to_testing_without_finalize() -> None:
    state = state_with(
        planning_valid=True,
        planning_result={"valid": True},
        workspace_inspected=True,
        implementation_valid=True,
        implementation_result={"valid": True, "project_created": True, "environment_prepared": True},
        project_created=True,
        environment_prepared=True,
        detected_test_framework="pytest",
        git_integration_enabled=True,
        git_workflow_state="workspace_prepared",
        git_workflow_branch="workflow/b0932234",
        tests_executed=False,
        tests_passed=False,
    )

    allowed = determine_allowed_handoffs(state)

    assert allowed == [SupervisorTarget.TESTING_REPAIR]
    assert SupervisorTarget.FINALIZE not in allowed


def test_tests_passed_with_uncommitted_changes_routes_only_to_git_workflow() -> None:
    state = state_with(
        tests_executed=True,
        tests_passed=True,
        testing_result={"tests_executed": True, "tests_passed": True},
        git_integration_enabled=True,
        git_workflow_state="workspace_prepared",
        git_workflow_branch="workflow/b0932234",
    )

    allowed = determine_allowed_handoffs(state)

    assert allowed == [SupervisorTarget.GIT_WORKFLOW]
    assert SupervisorTarget.FINALIZE not in allowed


def test_git_committed_and_tests_passed_allow_finalize() -> None:
    state = state_with(
        tests_executed=True,
        tests_passed=True,
        testing_result={"tests_executed": True, "tests_passed": True},
        git_integration_enabled=True,
        git_workflow_state="committed",
        git_workflow_branch="workflow/b0932234",
    )

    assert determine_allowed_handoffs(state) == [SupervisorTarget.FINALIZE]


def test_git_committed_and_ci_required_routes_to_ci_pipeline() -> None:
    state = state_with(
        tests_executed=True,
        tests_passed=True,
        testing_result={"tests_executed": True, "tests_passed": True},
        git_integration_enabled=True,
        git_workflow_state="committed",
        git_workflow_branch="workflow/b0932234",
        git_developer_commit_sha="a" * 40,
        ci_promotion_required=True,
        ci_promotion_eligible=False,
    )

    assert determine_allowed_handoffs(state) == [SupervisorTarget.CI_PIPELINE]


def test_git_committed_and_ci_accepted_does_not_route_ci_again() -> None:
    state = state_with(
        tests_executed=True,
        tests_passed=True,
        testing_result={"tests_executed": True, "tests_passed": True},
        git_integration_enabled=True,
        git_workflow_state="committed",
        git_workflow_branch="workflow/b0932234",
        git_developer_commit_sha="a" * 40,
        ci_promotion_required=True,
        ci_promotion_eligible=True,
        ci_validated_commit="a" * 40,
        ci_decision="accepted",
    )

    assert determine_allowed_handoffs(state) == [SupervisorTarget.FINALIZE]


def test_real_regression_state_routes_to_testing_repair_only() -> None:
    state = state_with(
        planning_valid=True,
        planning_result={"valid": True},
        workspace_inspected=True,
        implementation_valid=True,
        implementation_result={"valid": True, "project_created": True, "environment_prepared": True},
        project_created=True,
        environment_prepared=True,
        detected_test_framework="pytest",
        tests_executed=False,
        tests_passed=False,
        git_integration_enabled=True,
        git_workflow_state="workspace_prepared",
        git_workflow_branch="workflow/b0932234",
    )

    assert determine_allowed_handoffs(state) == [SupervisorTarget.TESTING_REPAIR]


def test_implementation_valid_true_never_becomes_implementation_failed_without_failure() -> None:
    state = state_with(
        terminal_status="implementation_failed",
        failure_type=None,
        failure_message=None,
        implementation_valid=True,
        implementation_result={"valid": True, "validation_errors": []},
        tests_executed=False,
        tests_passed=False,
    )

    assert terminal_status_from_state(state) != "implementation_failed"


@pytest.mark.asyncio
async def test_mandatory_decision_does_not_call_model_or_mcp() -> None:
    service = RecordingSupervisorService(AssertionError("model must not be called"))
    result = await supervisor_node(state_with(), service)  # type: ignore[arg-type]
    assert service.calls == 0
    assert result["supervisor_decision"] == "planning"
    assert result["handoff_history"][0]["source"] == "deterministic"


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("timeout"), RuntimeError("supervisor_refused")],
)
@pytest.mark.asyncio
async def test_supervisor_failure_uses_safe_fallback(
    failure: Exception,
) -> None:
    service = RecordingSupervisorService(
        failure,
        SupervisorDevelopmentConfig(
            development=True,
            force_model_decision=True,
        ),
    )

    result = await supervisor_node(state_with(), service)  # type: ignore[arg-type]

    assert service.calls == 0
    assert result["supervisor_decision"] == "planning"
    assert result["supervisor_attempts"] == 0
    assert result["supervisor_decision_source"] == "deterministic"
    assert result["handoff_history"][-1]["source"] == "deterministic"
    assert result["supervisor_errors"] == []


@pytest.mark.asyncio
async def test_invalid_model_output_uses_fallback(
) -> None:
    service = RecordingSupervisorService(
        SupervisorDecision(
            target=SupervisorTarget.IMPLEMENTATION,
            reason="Skip required planning.",
            confidence=0.7,
        ),
        SupervisorDevelopmentConfig(
            development=True,
            force_model_decision=True,
        ),
    )

    result = await supervisor_node(state_with(), service)  # type: ignore[arg-type]

    assert result["supervisor_decision"] == "planning"
    assert result["supervisor_attempts"] == 0
    assert result["supervisor_decision_source"] == "deterministic"
    assert result["supervisor_errors"] == []


@pytest.mark.asyncio
async def test_service_forced_timeout_raises_without_calling_openai() -> None:
    service = SupervisorService(
        openai_client=object(),  # type: ignore[arg-type]
        model="test",
        config=SupervisorDevelopmentConfig(
            development=True,
            force_model_decision=True,
            force_timeout=True,
        ),
    )
    with pytest.raises(TimeoutError, match="Forced supervisor timeout for development"):
        await service.decide(to_supervisor_input(state_with()), [SupervisorTarget.PLANNING])


@pytest.mark.asyncio
async def test_service_forced_invalid_target_reaches_validator() -> None:
    service = SupervisorService(
        openai_client=object(),  # type: ignore[arg-type]
        model="test",
        config=SupervisorDevelopmentConfig(
            development=True,
            force_model_decision=True,
            force_invalid_target=True,
        ),
    )
    state = state_with()
    decision = await service.decide(
        to_supervisor_input(state),
        [SupervisorTarget.PLANNING, SupervisorTarget.FINALIZE],
    )
    errors = validate_supervisor_decision(
        decision,
        [SupervisorTarget.PLANNING, SupervisorTarget.FINALIZE],
        state,
    )
    assert decision.target == "deployment"
    assert errors == ["target_not_allowed"]


@pytest.mark.asyncio
async def test_forced_repeated_invalid_target_uses_progressing_fallbacks_without_loop() -> None:
    service = SupervisorService(
        openai_client=object(),  # type: ignore[arg-type]
        model="test",
        config=SupervisorDevelopmentConfig(
            development=True,
            force_model_decision=True,
            force_target="planning",
        ),
    )
    state = state_with()

    first = await supervisor_node(state, service)
    state.update(first)
    state.update(planning_valid=True, planning_result={"valid": True})
    second = await supervisor_node(state, service)
    state.update(second)
    state.update(workspace_inspected=True)
    third = await supervisor_node(state, service)

    assert first["supervisor_decision"] == "planning"
    assert second["supervisor_decision"] == "inspect_workspace"
    assert third["supervisor_decision"] == "implementation"
    assert third.get("terminal_status") is None
    assert third["supervisor_invalid_decision_count"] == 0
    assert "supervisor_loop_detected" not in third["supervisor_errors"]
    assert [item.get("attempted_to") for item in third["handoff_history"][-3:]] == [
        "planning",
        "inspect_workspace",
        "implementation",
    ]
    assert [item["executed_to"] for item in third["handoff_history"][-3:]] == [
        "planning",
        "inspect_workspace",
        "implementation",
    ]
    assert route_after_supervisor(third) == "implementation"


@pytest.mark.asyncio
async def test_forced_repeated_effective_target_without_progress_detects_loop() -> None:
    service = SupervisorService(
        openai_client=object(),  # type: ignore[arg-type]
        model="test",
        config=SupervisorDevelopmentConfig(
            development=True,
            force_model_decision=True,
            force_target="planning",
        ),
    )
    state = state_with()
    for _ in range(3):
        state.update(await supervisor_node(state, service))

    assert state["supervisor_decision"] == "finalize"
    assert state["terminal_status"] == "supervisor_loop_detected"
    assert "supervisor_loop_detected" in state["supervisor_errors"]


@pytest.mark.asyncio
async def test_stagnant_loop_probe_uses_real_detector_and_three_identical_records() -> None:
    service = SupervisorService(
        openai_client=object(),  # type: ignore[arg-type]
        model="test",
        config=SupervisorDevelopmentConfig(
            development=True,
            force_stagnant_loop=True,
        ),
    )
    state = state_with()

    first = await supervisor_node(state, service)
    state.update(first)
    second = await supervisor_node(state, service)
    state.update(second)
    third = await supervisor_node(state, service)

    assert first["supervisor_stagnant_loop_probe_count"] == 1
    assert second["supervisor_stagnant_loop_probe_count"] == 2
    assert first.get("terminal_status") is None
    assert second.get("terminal_status") is None
    assert third["supervisor_stagnant_loop_probe_count"] == 3
    assert third["terminal_status"] == "supervisor_loop_detected"
    assert third["failure_type"] == "supervisor_loop_detected"
    assert third["failure_stage"] == "supervisor"
    probes = third["handoff_history"]
    assert len(probes) == 3
    assert {item["executed_to"] for item in probes} == {"planning"}
    assert {json.dumps(item["progress_fingerprint"], sort_keys=True) for item in probes}
    assert len(
        {json.dumps(item["progress_fingerprint"], sort_keys=True) for item in probes}
    ) == 1
    assert all(item["source"] == "development_loop_probe" for item in probes)
    assert all(item["progress_detected"] is False for item in probes)
    assert route_after_supervisor(first) == "supervisor_loop_probe"
    assert route_after_supervisor(third) == "finalize"


def test_repeated_attempt_with_distinct_effective_targets_is_not_loop() -> None:
    history = [
        {
            "attempted_to": "deployment",
            "selected_to": "planning",
            "executed_to": "planning",
            "progress_fingerprint": "A",
        },
        {
            "attempted_to": "deployment",
            "selected_to": "inspect_workspace",
            "executed_to": "inspect_workspace",
            "progress_fingerprint": "B",
        },
        {
            "attempted_to": "deployment",
            "selected_to": "implementation",
            "executed_to": "implementation",
            "progress_fingerprint": "C",
        },
    ]
    assert detect_supervisor_loop(history, state_with()) is False


def test_repeated_effective_target_with_progress_is_not_loop() -> None:
    history = [
        {"executed_to": "planning", "progress_fingerprint": value}
        for value in ("A", "B", "C")
    ]
    assert detect_supervisor_loop(history, state_with()) is False


def test_repeated_effective_target_without_progress_is_loop() -> None:
    history = [
        {"executed_to": "planning", "progress_fingerprint": "A"}
        for _ in range(3)
    ]
    assert detect_supervisor_loop(history, state_with()) is True


def test_fork_loop_detection_uses_only_active_branch() -> None:
    history = [
        {
            "executed_to": "planning",
            "progress_fingerprint": "A",
            "branch_id": "original",
        },
        {
            "executed_to": "planning",
            "progress_fingerprint": "A",
            "branch_id": "original",
        },
        {
            "executed_to": "planning",
            "progress_fingerprint": "A",
            "branch_id": "fork:checkpoint-1",
        },
    ]
    assert detect_supervisor_loop(history, state_with()) is False


@pytest.mark.parametrize(
    "updates",
    [
        {"planning_attempts": 1},
        {"implementation_attempts": 1},
        {"workspace_inspected": True},
        {"project_created": True},
        {"environment_prepared": True},
        {"tests_executed": True},
        {"repair_phase": "read_failing_test"},
    ],
)
def test_progress_fingerprint_changes_for_workflow_progress(updates: dict[str, Any]) -> None:
    before = state_with()
    after = state_with(**updates)
    assert supervisor_progress_fingerprint(before) != supervisor_progress_fingerprint(after)


@pytest.mark.asyncio
async def test_loop_detection_finalizes_with_controlled_failure() -> None:
    repeated = [
        {
            "sequence": index,
            "from": "supervisor",
            "to": "planning",
            "planning_attempts": 0,
        }
        for index in (1, 2)
    ]
    state = state_with(handoff_history=repeated)

    assert detect_supervisor_loop(repeated, state) is False
    result = await supervisor_node(
        state,
        RecordingSupervisorService(AssertionError("not called")),  # type: ignore[arg-type]
    )

    assert result["supervisor_decision"] == "finalize"
    assert result["terminal_status"] == "supervisor_loop_detected"
    assert result["failure_stage"] == "supervisor"


@pytest.mark.asyncio
async def test_handoff_history_is_serializable_and_limited() -> None:
    history = [
        {
            "sequence": index + 1,
            "from": "supervisor",
            "to": "inspect_workspace" if index % 2 else "finalize",
        }
        for index in range(MAX_HANDOFF_HISTORY)
    ]
    state = state_with(handoff_history=history)
    result = await supervisor_node(
        state,
        RecordingSupervisorService(AssertionError("not called")),  # type: ignore[arg-type]
    )
    assert len(result["handoff_history"]) == MAX_HANDOFF_HISTORY
    json.dumps(result["handoff_history"])


def test_workflow_supervisor_presentation_is_compact() -> None:
    state = state_with(
        supervisor_decision="finalize",
        supervisor_reason="Tests passed successfully.",
        supervisor_confidence=1.0,
        supervisor_invalid_decision_count=3,
        handoff_history=[
            {"from": "supervisor", "to": "planning"},
            {"from": "supervisor", "to": "testing_repair"},
            {
                "from": "supervisor",
                "to": "finalize",
                "attempted_to": "deployment",
                "selected_to": "finalize",
                "executed_to": "finalize",
                "source": "fallback",
            },
        ],
    )
    output = "\n".join(format_supervisor_result(state))
    assert "current stage: testing_repair" in output
    assert "last decision: finalize" in output
    assert "attempted target: deployment" in output
    assert "executed target: finalize" in output
    assert "invalid decisions: 3" in output
    assert "source:" in output
    assert "errors:" in output
    assert "loop detected: false" in output
    assert "handoffs: 3" in output
    assert "1. supervisor -> planning" in output
    assert "3. attempted deployment -> executed finalize (fallback)" in output


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sqlite_persists_supervisor_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "supervisor.sqlite"
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(database))
    thread_id = "supervisor-sqlite"

    def build(checkpointer):
        async def record(state: SoftwareFactoryState) -> dict[str, Any]:
            return {
                "supervisor_decision": "finalize",
                "supervisor_reason": "persisted",
                "supervisor_confidence": 1.0,
                "supervisor_invalid_decision_count": 2,
                "consecutive_invalid_decisions": 1,
                "supervisor_stagnant_loop_probe_count": 3,
                "supervisor_stagnant_loop_fingerprint": {
                    "current_stage": "intent",
                    "planning_valid": False,
                },
                "handoff_history": [
                    {
                        "sequence": 1,
                        "from": "supervisor",
                        "to": "finalize",
                        "source": "deterministic",
                        "confidence": 1.0,
                        "attempted_to": "deployment",
                        "selected_to": "finalize",
                        "executed_to": "finalize",
                        "progress_fingerprint": {"current_stage": "testing_repair"},
                    }
                ],
            }

        builder = StateGraph(SoftwareFactoryState)
        builder.add_node("record", record)
        builder.add_edge(START, "record")
        builder.add_edge("record", END)
        return builder.compile(checkpointer=checkpointer)

    async with create_sqlite_checkpointer() as checkpointer:
        graph = build(checkpointer)
        await graph.ainvoke(create_initial_state("persist"), config=thread_config(thread_id))

    async with create_sqlite_checkpointer() as checkpointer:
        graph = build(checkpointer)
        snapshot = await graph.aget_state(thread_config(thread_id))

    assert snapshot.values["supervisor_decision"] == "finalize"
    assert snapshot.values["handoff_history"][0]["source"] == "deterministic"
    assert snapshot.values["supervisor_invalid_decision_count"] == 2
    assert snapshot.values["consecutive_invalid_decisions"] == 1
    assert snapshot.values["supervisor_stagnant_loop_probe_count"] == 3
    assert snapshot.values["supervisor_stagnant_loop_fingerprint"]["current_stage"] == "intent"
    json.dumps(snapshot.values["handoff_history"][0]["progress_fingerprint"])
