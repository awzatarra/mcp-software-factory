from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from graph.checkpointing import create_sqlite_checkpointer
from graph.persistence_service import (
    AmbiguousForkNodeError,
    CheckpointNotFoundError,
    CheckpointThreadMismatchError,
    InvalidForkUpdateError,
    UnsafeReplayError,
    WorkflowPersistenceService,
)
from graph.runtime import thread_config
from graph.state import SoftwareFactoryState, create_initial_state
from host import load_fork_updates, load_fork_updates_file, print_fork_preview, print_workflow_history


class TimeTravelHarness:
    def __init__(self) -> None:
        self.test_runs = 0
        self.fix_runs = 0

    async def prepare_test_request(self, state: SoftwareFactoryState) -> dict[str, Any]:
        return {
            "pending_operation": "run_tests",
            "pending_tool_name": "testing__run_tests",
            "pending_tool_arguments": {"project_name": "medical-booking"},
            "pending_approval_preview": {"expected_command": ["python", "-m", "pytest"]},
            "pending_approval_status": "waiting",
            "last_completed_node": "prepare_test_request",
        }

    async def approval(self, state: SoftwareFactoryState) -> dict[str, Any]:
        decision = interrupt(
            {
                "type": "tool_approval",
                "operation": state.get("pending_operation"),
                "public_tool_name": state.get("pending_tool_name"),
            }
        )
        return {
            "pending_approval_status": "approved" if decision.get("approved") else "rejected",
            "last_completed_node": "approval",
        }

    async def execute_tests(self, state: SoftwareFactoryState) -> dict[str, Any]:
        self.test_runs += 1
        return {
            "tests_executed": True,
            "tests_passed": True,
            "pending_operation": None,
            "pending_tool_name": None,
            "pending_tool_arguments": None,
            "pending_approval_preview": None,
            "pending_approval_status": "none",
            "last_completed_node": "run_tests",
        }

    async def execute_tests_failed(self, state: SoftwareFactoryState) -> dict[str, Any]:
        return {
            "tests_executed": True,
            "tests_passed": False,
            "repair_phase": "read_failing_test",
            "failing_test_files": ["tests/test_health.py"],
            "last_completed_node": "run_tests",
        }

    async def read_failing_test(self, state: SoftwareFactoryState) -> dict[str, Any]:
        return {"repair_phase": "read_related_source", "last_completed_node": "read_failing_test"}

    async def read_related_source(self, state: SoftwareFactoryState) -> dict[str, Any]:
        return {"repair_phase": "apply_fix", "last_completed_node": "read_related_source"}

    async def prepare_fix(self, state: SoftwareFactoryState) -> dict[str, Any]:
        decision = interrupt(
            {
                "type": "tool_approval",
                "operation": "apply_fix",
                "repair_decision": state.get("repair_decision"),
                "failing_test_files": state.get("failing_test_files"),
            }
        )
        if decision.get("approved"):
            return {"pending_approval_status": "approved", "last_completed_node": "prepare_fix"}
        return {
            "pending_approval_status": "rejected",
            "approval_reason": decision.get("reason"),
            "last_rejected_tool": "filesystem__update_project_files",
            "user_cancelled": True,
            "terminal_status": "user_cancelled",
            "last_completed_node": "prepare_fix",
        }

    async def execute_fix(self, state: SoftwareFactoryState) -> dict[str, Any]:
        self.fix_runs += 1
        return {"repair_phase": "completed", "last_completed_node": "apply_fix"}

    async def finalize(self, state: SoftwareFactoryState) -> dict[str, Any]:
        return {"terminal_status": "completed", "last_completed_node": "finalize"}


def build_test_replay_graph(checkpointer: Any, harness: TimeTravelHarness):
    builder = StateGraph(SoftwareFactoryState)
    builder.add_node("prepare_test_request", harness.prepare_test_request)
    builder.add_node("approval", harness.approval)
    builder.add_node("execute_tests", harness.execute_tests)
    builder.add_node("finalize", harness.finalize)
    builder.add_edge(START, "prepare_test_request")
    builder.add_edge("prepare_test_request", "approval")
    builder.add_edge("approval", "execute_tests")
    builder.add_edge("execute_tests", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)


def build_test_fork_graph(checkpointer: Any, harness: TimeTravelHarness):
    builder = StateGraph(SoftwareFactoryState)
    builder.add_node("execute_tests", harness.execute_tests_failed)
    builder.add_node("read_failing_test", harness.read_failing_test)
    builder.add_node("read_related_source", harness.read_related_source)
    builder.add_node("prepare_fix", harness.prepare_fix)
    builder.add_node("execute_fix", harness.execute_fix)
    builder.add_node("finalize", harness.finalize)
    builder.add_edge(START, "execute_tests")
    builder.add_edge("execute_tests", "read_failing_test")
    builder.add_edge("read_failing_test", "read_related_source")
    builder.add_edge("read_related_source", "prepare_fix")
    builder.add_conditional_edges(
        "prepare_fix",
        lambda state: "cancelled" if state.get("user_cancelled") else "approved",
        {"cancelled": END, "approved": "execute_fix"},
    )
    builder.add_edge("execute_fix", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)


def build_domain_boundary_graph(checkpointer: Any, node_name: str):
    async def boundary_node(state: SoftwareFactoryState) -> dict[str, Any]:
        return {"last_completed_node": node_name}

    builder = StateGraph(SoftwareFactoryState)
    builder.add_node(node_name, boundary_node)
    builder.add_edge(START, node_name)
    builder.add_edge(node_name, END)
    return builder.compile(checkpointer=checkpointer)


async def checkpoint_with_next(graph: Any, thread_id: str, node: str):
    async for snapshot in graph.aget_state_history(thread_config(thread_id)):
        if tuple(snapshot.next) == (node,):
            return snapshot
    raise AssertionError(f"No checkpoint before {node}")


@pytest.mark.asyncio
async def test_get_specific_checkpoint_and_ownership_errors() -> None:
    graph = build_test_replay_graph(InMemorySaver(), TimeTravelHarness())
    await graph.ainvoke(create_initial_state("first"), config=thread_config("thread-a"))
    await graph.ainvoke(create_initial_state("second"), config=thread_config("thread-b"))
    checkpoint = await checkpoint_with_next(graph, "thread-a", "prepare_test_request")
    checkpoint_id = checkpoint.config["configurable"]["checkpoint_id"]
    service = WorkflowPersistenceService(graph)

    details = await service.get_checkpoint("thread-a", checkpoint_id)

    assert details.checkpoint_id == checkpoint_id
    assert details.next_nodes == ("prepare_test_request",)
    assert details.values["original_user_message"] == "first"
    with pytest.raises(CheckpointNotFoundError):
        await service.get_checkpoint("thread-a", "missing-checkpoint")
    with pytest.raises(CheckpointThreadMismatchError):
        await service.get_checkpoint("thread-b", checkpoint_id)


@pytest.mark.asyncio
async def test_safe_replay_retriggers_test_interrupt_without_repeating_prior_effects() -> None:
    harness = TimeTravelHarness()
    graph = build_test_replay_graph(InMemorySaver(), harness)
    thread_id = "safe-replay"
    config = thread_config(thread_id)
    await graph.ainvoke(create_initial_state("run tests"), config=config)
    await graph.ainvoke(Command(resume={"approved": True}), config=config)
    assert harness.test_runs == 1
    before_tests = await checkpoint_with_next(graph, thread_id, "prepare_test_request")
    checkpoint_id = before_tests.config["configurable"]["checkpoint_id"]
    service = WorkflowPersistenceService(graph)

    plan = await service.build_replay_plan(thread_id, checkpoint_id)
    replayed = await service.replay(thread_id, checkpoint_id)

    assert plan.allowed is True
    assert "prepare_test_request" in plan.nodes_that_may_reexecute
    assert "execute_create_project" not in plan.nodes_that_may_reexecute
    assert plan.sensitive_operations == ("run_tests",)
    assert replayed.interrupted is True
    assert replayed.interrupts[0].value["operation"] == "run_tests"
    assert harness.test_runs == 1
    completed = await service.approve(thread_id)
    assert completed.final_state["tests_passed"] is True
    assert harness.test_runs == 2


@pytest.mark.asyncio
async def test_replay_blocks_post_approval_and_unsafe_creation_boundaries() -> None:
    harness = TimeTravelHarness()
    graph = build_test_replay_graph(InMemorySaver(), harness)
    thread_id = "unsafe-replay"
    config = thread_config(thread_id)
    await graph.ainvoke(create_initial_state("run tests"), config=config)
    await graph.ainvoke(Command(resume={"approved": True}), config=config)
    post_approval = await checkpoint_with_next(graph, thread_id, "execute_tests")
    service = WorkflowPersistenceService(graph)
    plan = await service.build_replay_plan(
        thread_id,
        post_approval.config["configurable"]["checkpoint_id"],
    )

    assert plan.allowed is False
    assert "saltaría un nuevo interrupt" in (plan.rejection_reason or "")
    with pytest.raises(UnsafeReplayError):
        await service.replay(thread_id, post_approval.config["configurable"]["checkpoint_id"])

    class UnsafeGraph:
        async def aget_state(self, requested_config):
            class Snapshot:
                values = {"project_name": "medical-booking"}
                metadata = {"source": "loop", "step": 1}
                created_at = "now"
                next = ("execute_create_project",)
                tasks = ()
                interrupts = ()
                config = requested_config

            return Snapshot()

    unsafe = WorkflowPersistenceService(UnsafeGraph())
    unsafe_plan = await unsafe.build_replay_plan("thread", "checkpoint")
    assert unsafe_plan.allowed is False
    assert "efectos externos" in (unsafe_plan.rejection_reason or "")


@pytest.mark.asyncio
async def test_replay_at_implementation_boundary_preserves_sensitive_approvals() -> None:
    class ImplementationBoundaryGraph:
        async def aget_state(self, requested_config):
            class Snapshot:
                values = {"project_name": "medical-booking"}
                metadata = {"source": "loop", "step": 1}
                created_at = "now"
                next = ("implementation",)
                tasks = ()
                interrupts = ()
                config = requested_config

            return Snapshot()

    plan = await WorkflowPersistenceService(ImplementationBoundaryGraph()).build_replay_plan(
        "thread",
        "checkpoint",
    )

    assert plan.allowed is True
    assert "approval" in plan.nodes_that_may_reexecute
    assert plan.sensitive_operations == ("create_project", "prepare_environment", "run_tests")


@pytest.mark.asyncio
async def test_replay_from_final_checkpoint_is_noop() -> None:
    harness = TimeTravelHarness()
    graph = build_test_replay_graph(InMemorySaver(), harness)
    thread_id = "final-replay"
    await graph.ainvoke(create_initial_state("run tests"), config=thread_config(thread_id))
    await graph.ainvoke(Command(resume={"approved": True}), config=thread_config(thread_id))
    final_snapshot = await graph.aget_state(thread_config(thread_id))

    result = await WorkflowPersistenceService(graph).replay(
        thread_id,
        final_snapshot.config["configurable"]["checkpoint_id"],
    )

    assert result.already_completed is True
    assert harness.test_runs == 1


@pytest.mark.asyncio
async def test_fork_preview_validation_and_as_node_policy() -> None:
    graph = build_test_fork_graph(InMemorySaver(), TimeTravelHarness())
    thread_id = "fork-preview"
    await graph.ainvoke(create_initial_state("repair"), config=thread_config(thread_id))
    before_fix = await checkpoint_with_next(graph, thread_id, "prepare_fix")
    checkpoint_id = before_fix.config["configurable"]["checkpoint_id"]
    service = WorkflowPersistenceService(graph)

    preview = await service.preview_fork(
        thread_id,
        checkpoint_id,
        {"repair_decision": "Corregir otra prueba", "failing_test_files": [r"tests\test_other.py"]},
    )
    rejected = await service.preview_fork(
        thread_id,
        checkpoint_id,
        {"planning_result": {}, "pending_tool_arguments": {}, "repair_phase": 7},
    )

    assert preview.allowed is True
    assert preview.as_node == "read_related_source"
    assert preview.new_values["failing_test_files"] == ["tests/test_other.py"]
    assert set(rejected.rejected_fields) == {"pending_tool_arguments", "planning_result", "repair_phase"}
    assert rejected.reason == "fork_field_not_allowed"
    with pytest.raises(InvalidForkUpdateError) as invalid:
        await service.fork(thread_id, checkpoint_id, {"planning_result": {}}, continue_execution=False)
    assert invalid.value.rejected_fields == {"planning_result": "campo no editable"}


@pytest.mark.asyncio
async def test_fork_before_failing_test_infers_execute_tests_from_checkpoint_evidence() -> None:
    graph = build_test_fork_graph(InMemorySaver(), TimeTravelHarness())
    thread_id = "fork-before-read"
    await graph.ainvoke(create_initial_state("repair"), config=thread_config(thread_id))
    before_read = await checkpoint_with_next(graph, thread_id, "read_failing_test")

    preview = await WorkflowPersistenceService(graph).preview_fork(
        thread_id,
        before_read.config["configurable"]["checkpoint_id"],
        {"failing_test_files": ["tests/test_alternative.py"]},
    )

    assert preview.allowed is True
    assert preview.as_node == "execute_tests"
    assert preview.next_nodes == ("read_failing_test",)


@pytest.mark.parametrize("bad_path", ["../test.py", "/tmp/test.py", r"C:\temp\test.py"])
def test_fork_validation_rejects_unsafe_test_paths(bad_path: str) -> None:
    from graph.persistence_service import validate_fork_updates

    validated, rejected = validate_fork_updates({"failing_test_files": [bad_path]})

    assert validated == {}
    assert "failing_test_files" in rejected


def test_fork_json_file_is_confined_to_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import host

    repository = tmp_path / "repository"
    repository.mkdir()
    valid_file = repository / "fork-state.json"
    valid_file.write_text('{"repair_decision": "alternativa"}', encoding="utf-8")
    outside_file = tmp_path / "outside.json"
    outside_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(host, "ROOT", repository)

    assert load_fork_updates_file("fork-state.json") == {"repair_decision": "alternativa"}
    with pytest.raises(ValueError, match="dentro del repositorio"):
        load_fork_updates_file(str(outside_file))


def test_fork_updates_accept_inline_json() -> None:
    assert load_fork_updates('{"tests_executed": false, "repair_attempts": 2}') == {
        "tests_executed": False,
        "repair_attempts": 2,
    }


@pytest.mark.asyncio
async def test_testing_fork_reapproves_once_and_preserves_grouped_results_and_origin() -> None:
    harness = TimeTravelHarness()
    graph = build_test_replay_graph(InMemorySaver(), harness)
    thread_id = "testing-contract-fork"
    initial = create_initial_state("run tests")
    initial["planning_result"] = {
        "analysis": {"project_type": "fastapi"},
        "tasks": [{"id": "task-1"}],
        "valid": True,
    }
    initial["implementation_result"] = {
        "package_name": "medical_booking",
        "generated_files": ["medical_booking/main.py", "tests/test_health.py"],
        "project_created": True,
        "environment_prepared": True,
        "valid": True,
    }
    await graph.ainvoke(initial, config=thread_config(thread_id))
    origin_snapshot = await checkpoint_with_next(graph, thread_id, "prepare_test_request")
    origin_checkpoint_id = origin_snapshot.config["configurable"]["checkpoint_id"]
    service = WorkflowPersistenceService(graph)

    preview = await service.preview_fork(
        thread_id,
        origin_checkpoint_id,
        {
            "tests_executed": False,
            "tests_passed": False,
            "repair_phase": "not_started",
            "repair_attempts": 0,
            "failure_type": None,
            "failure_stage": None,
            "failure_message": None,
        },
    )
    plan = await service.build_replay_plan(thread_id, origin_checkpoint_id)
    forked = await service.fork(
        thread_id,
        origin_checkpoint_id,
        {"tests_executed": False, "tests_passed": False},
        continue_execution=True,
    )

    assert preview.allowed is True
    assert preview.domain == "testing_repair"
    assert preview.as_node == "__start__"
    assert plan.nodes_that_may_reexecute == (
        "prepare_test_request",
        "approval",
        "execute_tests",
        "finalize",
        "planner_evaluation",
        "agent_performance_evaluation",
        "failure_attribution",
    )
    assert plan.sensitive_operations == ("run_tests",)
    assert forked.run_result is not None
    assert forked.run_result.interrupted is True
    assert forked.run_result.interrupts[0].value["operation"] == "run_tests"
    assert harness.test_runs == 0

    fork_details = await service.get_checkpoint(thread_id, forked.fork_checkpoint_id)
    origin_details = await service.get_checkpoint(thread_id, origin_checkpoint_id)
    assert fork_details.source == "fork"
    assert fork_details.values["planning_result"] == initial["planning_result"]
    assert fork_details.values["implementation_result"] == initial["implementation_result"]
    assert origin_details.values["planning_result"] == initial["planning_result"]
    assert origin_details.values["implementation_result"] == initial["implementation_result"]
    assert origin_details.values.get("fork_origin_checkpoint_id") is None
    assert origin_details.values["tests_executed"] is False

    completed = await service.approve(thread_id, "Ejecutar tests de la rama")
    assert completed.final_state["tests_passed"] is True
    assert harness.test_runs == 1
    assert completed.final_state["planning_result"] == initial["planning_result"]
    assert completed.final_state["implementation_result"] == initial["implementation_result"]
    assert "prepare_create_project" not in graph.nodes
    assert "prepare_developer_knowledge" not in graph.nodes
    assert "prepare_environment_request" not in graph.nodes

    history = await service.get_history(thread_id, limit=50)
    fork_items = [item for item in history if item.lineage == "fork"]
    assert fork_items
    assert all(item.source == "fork" for item in fork_items)
    assert any(item.fork_origin_checkpoint_id == origin_checkpoint_id for item in fork_items)
    assert any(set(item.fork_updated_fields) == {"tests_executed", "tests_passed"} for item in fork_items)


@pytest.mark.asyncio
async def test_testing_fork_rejects_planning_and_implementation_fields_without_checkpoint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    graph = build_test_replay_graph(InMemorySaver(), TimeTravelHarness())
    thread_id = "testing-forbidden-fields"
    await graph.ainvoke(create_initial_state("run tests"), config=thread_config(thread_id))
    origin = await checkpoint_with_next(graph, thread_id, "prepare_test_request")
    checkpoint_id = origin.config["configurable"]["checkpoint_id"]
    service = WorkflowPersistenceService(graph)
    history_before = await service.get_history(thread_id, limit=50)

    planning_preview = await service.preview_fork(
        thread_id,
        checkpoint_id,
        {
            "planning_result": {},
            "requirement_analysis": {},
            "implementation_tasks": [],
            "supervisor_decision": "finalize",
            "handoff_history": [],
        },
    )
    implementation_preview = await service.preview_fork(
        thread_id,
        checkpoint_id,
        {
            "implementation_result": {},
            "generated_package_name": "other",
            "generated_files": [],
            "project_created": False,
            "environment_prepared": False,
            "dependencies_installed": False,
        },
    )
    print_fork_preview(planning_preview)
    output = capsys.readouterr().out
    history_after = await service.get_history(thread_id, limit=50)

    assert planning_preview.allowed is False
    assert planning_preview.reason == "fork_field_not_allowed"
    assert set(planning_preview.disallowed_fields) == {
        "handoff_history",
        "implementation_tasks",
        "planning_result",
        "requirement_analysis",
        "supervisor_decision",
    }
    assert implementation_preview.allowed is False
    assert set(implementation_preview.disallowed_fields) == {
        "dependencies_installed",
        "environment_prepared",
        "generated_files",
        "generated_package_name",
        "implementation_result",
        "project_created",
    }
    assert "allowed=false" in output
    assert "reason=fork_field_not_allowed" in output
    assert "disallowed_fields=" in output
    assert len(history_after) == len(history_before)


@pytest.mark.parametrize("domain_node,expected_domain", [("planning", "planning"), ("implementation", "implementation")])
@pytest.mark.asyncio
async def test_fork_domain_allowlists_reject_testing_updates_outside_testing_repair(
    domain_node: str,
    expected_domain: str,
) -> None:
    graph = build_domain_boundary_graph(InMemorySaver(), domain_node)
    thread_id = f"{expected_domain}-domain"
    await graph.ainvoke(create_initial_state("domain boundary"), config=thread_config(thread_id))
    origin = await checkpoint_with_next(graph, thread_id, domain_node)
    service = WorkflowPersistenceService(graph)
    history_before = await service.get_history(thread_id, limit=20)

    preview = await service.preview_fork(
        thread_id,
        origin.config["configurable"]["checkpoint_id"],
        {"tests_executed": False, "repair_phase": "not_started"},
    )
    history_after = await service.get_history(thread_id, limit=20)

    assert preview.domain == expected_domain
    assert preview.allowed is False
    assert preview.reason == "fork_field_not_allowed"
    assert set(preview.disallowed_fields) == {"repair_phase", "tests_executed"}
    assert len(history_after) == len(history_before)


@pytest.mark.asyncio
async def test_fork_creates_independent_checkpoints_and_preserves_origin() -> None:
    harness = TimeTravelHarness()
    graph = build_test_fork_graph(InMemorySaver(), harness)
    thread_id = "independent-forks"
    await graph.ainvoke(create_initial_state("repair"), config=thread_config(thread_id))
    before_fix = await checkpoint_with_next(graph, thread_id, "prepare_fix")
    checkpoint_id = before_fix.config["configurable"]["checkpoint_id"]
    original_decision = before_fix.values.get("repair_decision")
    service = WorkflowPersistenceService(graph)

    first = await service.fork(
        thread_id,
        checkpoint_id,
        {"repair_decision": "Primera alternativa"},
        continue_execution=False,
    )
    second = await service.fork(
        thread_id,
        checkpoint_id,
        {"repair_decision": "Segunda alternativa"},
        continue_execution=False,
    )
    origin = await service.get_checkpoint(thread_id, checkpoint_id)
    first_details = await service.get_checkpoint(thread_id, first.fork_checkpoint_id)
    second_details = await service.get_checkpoint(thread_id, second.fork_checkpoint_id)
    history = await service.get_history(thread_id, limit=50)

    assert first.fork_checkpoint_id != second.fork_checkpoint_id
    assert origin.values.get("repair_decision") == original_decision
    assert first_details.values["repair_decision"] == "Primera alternativa"
    assert second_details.values["repair_decision"] == "Segunda alternativa"
    assert first_details.source == second_details.source == "fork"
    assert first_details.values["fork_origin_checkpoint_id"] == checkpoint_id
    assert first.updated_fields == ("repair_decision",)
    assert sum(item.lineage == "fork" for item in history) >= 2


@pytest.mark.asyncio
async def test_history_keeps_legacy_update_fork_compatibility() -> None:
    graph = build_test_fork_graph(InMemorySaver(), TimeTravelHarness())
    thread_id = "legacy-fork-update"
    await graph.ainvoke(create_initial_state("repair"), config=thread_config(thread_id))
    origin = await checkpoint_with_next(graph, thread_id, "prepare_fix")
    origin_checkpoint_id = origin.config["configurable"]["checkpoint_id"]

    legacy_config = await graph.aupdate_state(
        origin.config,
        values={
            "repair_decision": "Legacy alternative",
            "fork_origin_checkpoint_id": origin_checkpoint_id,
            "fork_updated_fields": ["repair_decision"],
        },
        as_node="read_related_source",
    )
    legacy_checkpoint_id = legacy_config["configurable"]["checkpoint_id"]
    history = await WorkflowPersistenceService(graph).get_history(thread_id, limit=50)
    legacy = next(item for item in history if item.checkpoint_id == legacy_checkpoint_id)

    assert legacy.source == "update"
    assert legacy.lineage == "fork/update"
    assert legacy.fork_origin_checkpoint_id == origin_checkpoint_id


@pytest.mark.asyncio
async def test_fork_continues_from_returned_config_and_retriggers_interrupt() -> None:
    harness = TimeTravelHarness()
    graph = build_test_fork_graph(InMemorySaver(), harness)
    thread_id = "fork-continue"
    await graph.ainvoke(create_initial_state("repair"), config=thread_config(thread_id))
    before_fix = await checkpoint_with_next(graph, thread_id, "prepare_fix")
    checkpoint_id = before_fix.config["configurable"]["checkpoint_id"]
    service = WorkflowPersistenceService(graph)
    await service.fork(
        thread_id,
        checkpoint_id,
        {"repair_decision": "Rama que no continúa"},
        continue_execution=False,
    )

    continued = await service.fork(
        thread_id,
        checkpoint_id,
        {"repair_decision": "Rama ejecutada"},
        continue_execution=True,
    )

    assert continued.run_result is not None
    assert continued.run_result.interrupted is True
    assert continued.run_result.interrupts[0].value["repair_decision"] == "Rama ejecutada"
    assert continued.run_result.final_state["fork_origin_checkpoint_id"] == checkpoint_id
    assert harness.fix_runs == 0


@pytest.mark.asyncio
async def test_rejected_fork_history_marks_alternative_and_preserves_original(
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness = TimeTravelHarness()
    graph = build_test_fork_graph(InMemorySaver(), harness)
    thread_id = "rejected-alternative"
    config = thread_config(thread_id)
    await graph.ainvoke(create_initial_state("repair"), config=config)
    before_fix = await checkpoint_with_next(graph, thread_id, "prepare_fix")
    origin_checkpoint_id = before_fix.config["configurable"]["checkpoint_id"]
    await graph.ainvoke(Command(resume={"approved": True}), config=config)
    original_final = await graph.aget_state(config)
    original_final_id = original_final.config["configurable"]["checkpoint_id"]
    service = WorkflowPersistenceService(graph)

    forked = await service.fork(
        thread_id,
        origin_checkpoint_id,
        {
            "repair_decision": "Cambiar main.py",
            "repair_before": '"status": "ok"',
            "repair_after": '"status": "healthy"',
            "repair_phase": "apply_fix",
        },
        continue_execution=True,
    )
    assert forked.run_result and forked.run_result.interrupted is True
    rejected = await service.reject(thread_id, "No aplicar la alternativa")
    history = await service.get_history(thread_id, limit=50)
    original = await service.get_checkpoint(thread_id, original_final_id)
    fork_checkpoint = await service.get_checkpoint(thread_id, forked.fork_checkpoint_id)
    print_workflow_history(thread_id, history)
    output = capsys.readouterr().out

    cancelled = next(item for item in history if item.result == "alternative_rejected")
    assert rejected.final_state["terminal_status"] == "user_cancelled"
    assert cancelled.lineage == "fork"
    assert cancelled.approval_reason == "No aplicar la alternativa"
    assert cancelled.last_rejected_tool == "filesystem__update_project_files"
    assert cancelled.fork_origin_checkpoint_id == origin_checkpoint_id
    assert set(cancelled.fork_updated_fields) == {
        "repair_after",
        "repair_before",
        "repair_decision",
        "repair_phase",
    }
    assert original.values["terminal_status"] == "completed"
    assert fork_checkpoint.source == "fork"
    assert harness.fix_runs == 1
    assert "lineage=fork" in output
    assert "terminal=user_cancelled" in output
    assert "result=alternative_rejected" in output
    assert "reason=No aplicar la alternativa" in output
    assert "last_rejected_tool=filesystem__update_project_files" in output


@pytest.mark.asyncio
async def test_ambiguous_fork_node_is_controlled_error() -> None:
    graph = build_test_fork_graph(InMemorySaver(), TimeTravelHarness())
    thread_id = "ambiguous-fork"
    await graph.ainvoke(create_initial_state("repair"), config=thread_config(thread_id))
    ambiguous = await checkpoint_with_next(graph, thread_id, "read_related_source")

    with pytest.raises(AmbiguousForkNodeError):
        await WorkflowPersistenceService(graph).fork(
            thread_id,
            ambiguous.config["configurable"]["checkpoint_id"],
            {"repair_decision": "No inferir"},
            continue_execution=False,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sqlite_time_travel_replay_and_fork_survive_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "time-travel.sqlite"
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(database))
    thread_id = "sqlite-time-travel"
    origin_checkpoint_id = ""
    fork_checkpoint_id = ""

    async with create_sqlite_checkpointer() as checkpointer:
        harness = TimeTravelHarness()
        graph = build_test_fork_graph(checkpointer, harness)
        await graph.ainvoke(create_initial_state("repair"), config=thread_config(thread_id))
        before_fix = await checkpoint_with_next(graph, thread_id, "prepare_fix")
        origin_checkpoint_id = before_fix.config["configurable"]["checkpoint_id"]
        await graph.ainvoke(Command(resume={"approved": True}), config=thread_config(thread_id))
        service = WorkflowPersistenceService(graph)
        replayed = await service.replay(thread_id, origin_checkpoint_id)
        assert replayed.interrupted is True
        forked = await service.fork(
            thread_id,
            origin_checkpoint_id,
            {"repair_decision": "Persistir rama"},
            continue_execution=False,
        )
        fork_checkpoint_id = forked.fork_checkpoint_id

    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_test_fork_graph(checkpointer, TimeTravelHarness())
        service = WorkflowPersistenceService(graph)
        origin = await service.get_checkpoint(thread_id, origin_checkpoint_id)
        fork = await service.get_checkpoint(thread_id, fork_checkpoint_id)
        history = await service.get_history(thread_id, limit=50)

    assert origin.values.get("repair_decision") is None
    assert fork.values["repair_decision"] == "Persistir rama"
    assert fork.source == "fork"
    assert any(item.lineage == "replay" for item in history)
    assert any(item.lineage == "fork" for item in history)
