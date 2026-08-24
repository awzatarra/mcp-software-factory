from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from graph.checkpointing import create_sqlite_checkpointer
from graph.builder import build_software_factory_graph
from graph.failure_injection import raise_if_failure_injected
from graph.nodes import GraphDependencies
from graph.persistence_service import (
    WorkflowAlreadyCompletedError,
    WorkflowNotFoundError,
    WorkflowNotInterruptedError,
    WorkflowPersistenceService,
)
from graph.runtime import create_thread_id, run_software_factory_graph, thread_config
from graph.state import SoftwareFactoryState, create_initial_state
from graph.subgraphs.implementation.test_validation import build_fastapi_health_test
from streaming import (
    EventStatus,
    InMemoryWorkflowEventEmitter,
    WorkflowEventFactory,
    WorkflowEventType,
    emit_workflow_event,
)
from tool_executor import ToolExecutionOutcome


class DurableGraphHarness:
    def __init__(self, *, fail_second: bool = False) -> None:
        self.fail_second = fail_second
        self.calls: list[str] = []

    async def first(self, state: SoftwareFactoryState) -> dict[str, Any]:
        self.calls.append("first")
        return {
            "project_name": "medical-booking",
            "created_project_name": "medical-booking",
            "project_exists": True,
            "workspace_inspected": True,
            "last_completed_node": "first",
        }

    async def second(self, state: SoftwareFactoryState) -> dict[str, Any]:
        self.calls.append("second")
        if self.fail_second:
            raise RuntimeError("injected test failure")
        return {
            "tests_executed": True,
            "tests_passed": True,
            "terminal_status": "completed",
            "final_response": "Proyecto: medical-booking\nResultado: 1 passed",
            "last_completed_node": "second",
        }


class RuntimeGraph:
    def __init__(self) -> None:
        self.configs: list[dict[str, Any]] = []

    async def ainvoke(self, state: SoftwareFactoryState, config: dict[str, Any]) -> SoftwareFactoryState:
        self.configs.append(config)
        result = representative_creation_state()
        result["original_user_message"] = state["original_user_message"]
        return result


class FactoryExecutor:
    def __init__(self, *, fail_before_repair: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_before_repair = fail_before_repair

    def openai_tool(self, public_tool_name: str) -> dict[str, Any]:
        return {"type": "function", "name": public_tool_name, "parameters": {"type": "object"}}

    async def execute(
        self,
        public_tool_name: str,
        arguments: dict[str, Any],
        *,
        state_context: SoftwareFactoryState,
        approval_mode: str = "prompt",
    ) -> ToolExecutionOutcome:
        self.calls.append(public_tool_name)
        updates: dict[str, Any] = {}
        payload: dict[str, Any] = {"success": True}
        if public_tool_name == "software_factory__analyze_requirement":
            updates["analysis_completed"] = True
        elif public_tool_name == "software_factory__create_tasks":
            updates["tasks_created"] = True
        elif public_tool_name == "filesystem__list_files":
            updates["workspace_inspected"] = True
        elif public_tool_name == "filesystem__create_project_structure":
            updates.update(project_created=True, created_project_name="recoverable-project")
        elif public_tool_name == "testing__detect_test_framework":
            updates.update(detected_test_framework="pytest", expected_test_command=["python", "-m", "pytest"])
        elif public_tool_name == "testing__prepare_test_environment":
            updates.update(environment_prepared=True, dependencies_installed=True, environment_python=".venv/python")
        elif public_tool_name == "testing__run_tests":
            if self.fail_before_repair and state_context.get("repair_attempts", 0) == 0:
                updates.update(
                    tests_executed=True,
                    tests_passed=False,
                    first_test_result_summary="1 failed",
                    test_failure_summary='tests/test_health.py esperaba {"status": "healthy"}',
                    failing_test_files=["tests/test_health.py"],
                    repair_phase="read_failing_test",
                )
            else:
                updates.update(
                    tests_executed=True,
                    tests_passed=True,
                    final_test_result_summary="1 passed",
                    actual_test_command=[".venv/python", "-m", "pytest"],
                    repair_phase="completed" if state_context.get("repair_attempts") else "not_started",
                )
        elif public_tool_name == "filesystem__read_file":
            reads = list(state_context.get("files_read_during_repair", []))
            reads.append(arguments["relative_path"])
            updates["files_read_during_repair"] = reads
            if arguments["relative_path"].endswith("tests/test_health.py"):
                updates["repair_phase"] = "read_related_source"
                payload["content"] = (
                    "from recoverable_project.main import app\n"
                    'assert response.json() == {"status": "healthy"}'
                )
            else:
                updates["repair_phase"] = "apply_fix"
                payload["content"] = 'return {"status": "ok"}'
        elif public_tool_name == "filesystem__update_project_files":
            updates.update(
                repair_attempts=state_context.get("repair_attempts", 0) + 1,
                repair_phase="rerun_tests",
                repair_decision="Se corrigió el test en el subgrafo.",
                repair_before='"status": "healthy"',
                repair_after='"status": "ok"',
                files_updated_during_repair=["tests/test_health.py"],
            )
        return ToolExecutionOutcome(public_tool_name, arguments, payload, updates)


async def factory_arguments(public_tool_name: str, state: SoftwareFactoryState) -> dict[str, Any]:
    if public_tool_name == "filesystem__update_project_files":
        return {
            "project_name": "recoverable-project",
            "files": [{"path": "tests/test_health.py", "content": 'assert response.json() == {"status": "ok"}'}],
        }
    return {
        "project_name": "recoverable-project",
        "files": [
            {
                "path": "recoverable_project/main.py",
                "content": '@app.get("/health")\ndef health():\n    return {"status": "ok"}',
            },
            {
                "path": "tests/test_health.py",
                "content": build_fastapi_health_test(
                    "recoverable_project", "/health", 200, {"status": "ok"}
                ),
            },
            {"path": "requirements.txt", "content": "fastapi\npytest\n"},
        ],
    }


def build_durable_graph(checkpointer: Any, harness: DurableGraphHarness):
    builder = StateGraph(SoftwareFactoryState)
    builder.add_node("first", harness.first)
    builder.add_node("second", harness.second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    return builder.compile(checkpointer=checkpointer)


class PostCompletionPromotionHarness:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete_with_pending_promotion(self, state: SoftwareFactoryState) -> dict[str, Any]:
        self.calls.append("complete")
        return {
            "workflow_id": state.get("workflow_id"),
            "project_name": "promotion-project",
            "created_project_name": "promotion-project",
            "terminal_status": "completed",
            "tests_passed": True,
            "pending_operation": "git_merge",
            "pending_tool_name": "git__merge_workflow_branch",
            "pending_tool_arguments": {
                "project_id": "promotion-project",
                "workflow_id": state.get("workflow_id"),
                "approval_id": "approval-1",
                "actor": "user",
            },
            "pending_approval_status": "waiting",
            "git_promotion_state": "awaiting_approval",
        }

    async def git_promotion_approval(self, state: SoftwareFactoryState) -> dict[str, Any]:
        decision = interrupt({
            "operation": "git_merge",
            "public_tool_name": "git__merge_workflow_branch",
            "arguments": state.get("pending_tool_arguments"),
        })
        if not isinstance(decision, dict) or decision.get("approved") is not True:
            return {"pending_approval_status": "rejected"}
        emit_workflow_event(
            WorkflowEventType.APPROVAL_GRANTED,
            source="graph.approval",
            stage="git_merge",
            status=EventStatus.COMPLETED,
            data={"operation": "git_merge", "tool_name": "git__merge_workflow_branch"},
        )
        return {"pending_approval_status": "approved"}

    async def execute_git_promotion(self, state: SoftwareFactoryState) -> dict[str, Any]:
        self.calls.append("git__approve_promotion")
        emit_workflow_event(
            WorkflowEventType.TOOL_STARTED,
            source="graph.git",
            stage="git_merge",
            status=EventStatus.RUNNING,
            data={"tool": "approve_promotion"},
        )
        emit_workflow_event(
            WorkflowEventType.TOOL_COMPLETED,
            source="graph.git",
            stage="git_merge",
            status=EventStatus.COMPLETED,
            data={"tool": "approve_promotion"},
        )
        self.calls.append("git__merge_workflow_branch")
        emit_workflow_event(
            WorkflowEventType.TOOL_STARTED,
            source="graph.git",
            stage="git_merge",
            status=EventStatus.RUNNING,
            data={"tool": "merge_workflow_branch"},
        )
        emit_workflow_event(
            WorkflowEventType.TOOL_COMPLETED,
            source="graph.git",
            stage="git_merge",
            status=EventStatus.COMPLETED,
            data={"tool": "merge_workflow_branch"},
        )
        return {
            "git_promotion_state": "completed",
            "git_promotion_result_commit": "393800e2a01bda7eb3b1fcb6416f5dbbe84cc7bc",
            "pending_operation": None,
            "pending_tool_name": None,
            "pending_tool_arguments": None,
            "pending_approval_status": "none",
        }


def build_post_completion_promotion_graph(
    checkpointer: Any,
    harness: PostCompletionPromotionHarness,
):
    builder = StateGraph(SoftwareFactoryState)
    builder.add_node("complete", harness.complete_with_pending_promotion)
    builder.add_node("git_promotion_approval", harness.git_promotion_approval)
    builder.add_node("execute_git_promotion", harness.execute_git_promotion)
    builder.add_edge(START, "complete")
    builder.add_edge("complete", "git_promotion_approval")
    builder.add_edge("git_promotion_approval", "execute_git_promotion")
    builder.add_edge("execute_git_promotion", END)
    return builder.compile(checkpointer=checkpointer)


def representative_creation_state() -> SoftwareFactoryState:
    state = create_initial_state("create medical-booking")
    state.update(
        project_name="medical-booking",
        created_project_name="medical-booking",
        project_created=True,
        environment_prepared=True,
        dependencies_installed=True,
        environment_python=r"workspace\medical-booking\.venv\Scripts\python.exe",
        tests_executed=True,
        tests_passed=True,
        terminal_status="completed",
        final_response="Proyecto: medical-booking\nResultado: 1 passed",
        final_test_result_summary="1 passed",
    )
    return state


def representative_repair_state() -> SoftwareFactoryState:
    state = representative_creation_state()
    state.update(
        project_created=False,
        project_exists=True,
        repair_phase="completed",
        repair_attempts=1,
        repair_decision="Se corrigió el test.",
        repair_before='"status": "healthy"',
        repair_after='"status": "ok"',
        failing_test_files=["tests/test_health.py"],
        files_read_during_repair=[
            "medical-booking/tests/test_health.py",
            "medical-booking/medical_booking/main.py",
        ],
        files_updated_during_repair=["tests/test_health.py"],
    )
    return state


def test_create_thread_id_is_uuid_and_unique() -> None:
    first = create_thread_id()
    second = create_thread_id()

    assert str(UUID(first)) == first
    assert str(UUID(second)) == second
    assert first != second


def test_thread_config_uses_received_thread_id() -> None:
    assert thread_config("received-id") == {"configurable": {"thread_id": "received-id"}}


@pytest.mark.asyncio
async def test_runtime_generates_distinct_thread_ids_and_preserves_received_id(capsys: pytest.CaptureFixture[str]) -> None:
    graph = RuntimeGraph()

    first = await run_software_factory_graph(graph, "first")
    second = await run_software_factory_graph(graph, "second")
    received = await run_software_factory_graph(graph, "third", thread_id="received-thread")
    capsys.readouterr()

    assert first.thread_id != second.thread_id
    assert received.thread_id == "received-thread"
    assert graph.configs[-1] == thread_config("received-thread")


def test_failure_injection_rejects_unknown_or_non_development_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGGRAPH_FAIL_AFTER_NODE", "arbitrary")
    with pytest.raises(ValueError, match="Valores permitidos"):
        raise_if_failure_injected("arbitrary")

    monkeypatch.setenv("LANGGRAPH_FAIL_AFTER_NODE", "prepare_environment")
    monkeypatch.setenv("LANGGRAPH_DEVELOPMENT", "false")
    with pytest.raises(RuntimeError, match="LANGGRAPH_DEVELOPMENT=true"):
        raise_if_failure_injected("prepare_environment")


@pytest.mark.asyncio
async def test_checkpointer_creates_database_and_persists_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "checkpoints.sqlite"
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(database))
    thread_id = create_thread_id()
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_durable_graph(checkpointer, DurableGraphHarness())
        await graph.ainvoke(create_initial_state("persist me"), config=thread_config(thread_id))
        snapshot = await WorkflowPersistenceService(graph).get_snapshot(thread_id)

    assert database.is_file()
    assert snapshot.thread_id == thread_id
    assert snapshot.checkpoint_id
    assert snapshot.values["tests_passed"] is True
    assert snapshot.next_nodes == ()
    assert snapshot.values["terminal_status"] == "completed"


@pytest.mark.asyncio
async def test_snapshot_exposes_pending_next_node_after_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "pending.sqlite"))
    harness = DurableGraphHarness(fail_second=True)
    thread_id = create_thread_id()
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_durable_graph(checkpointer, harness)
        with pytest.raises(RuntimeError, match="injected test failure"):
            await graph.ainvoke(create_initial_state("pending"), config=thread_config(thread_id))
        snapshot = await WorkflowPersistenceService(graph).get_snapshot(thread_id)

    assert snapshot.next_nodes == ("second",)
    assert snapshot.values["workspace_inspected"] is True
    assert snapshot.values["tests_executed"] is False


@pytest.mark.asyncio
async def test_history_is_recent_first_limited_and_summarized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "history.sqlite"))
    thread_id = create_thread_id()
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_durable_graph(checkpointer, DurableGraphHarness())
        initial = create_initial_state("history")
        initial["test_stdout"] = "x" * 100_000
        await graph.ainvoke(initial, config=thread_config(thread_id))
        history = await WorkflowPersistenceService(graph).get_history(thread_id, limit=2)

    assert len(history) == 2
    assert history[0].step is not None
    assert all("test_stdout" not in keys for item in history for keys in item.node_writes.values())
    assert all("x" * 100 not in repr(item) for item in history)


@pytest.mark.asyncio
async def test_incomplete_workflow_resumes_same_thread_without_rebuilding_initial_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "resume.sqlite"))
    harness = DurableGraphHarness(fail_second=True)
    thread_id = create_thread_id()
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_durable_graph(checkpointer, harness)
        with pytest.raises(RuntimeError):
            await graph.ainvoke(create_initial_state("original input"), config=thread_config(thread_id))
        assert harness.calls == ["first", "second"]
        harness.fail_second = False

        result = await WorkflowPersistenceService(graph).resume(thread_id)

    assert result.thread_id == thread_id
    assert result.already_completed is False
    assert result.final_state["original_user_message"] == "original input"
    assert result.final_state["tests_passed"] is True
    assert harness.calls == ["first", "second", "second"]


@pytest.mark.asyncio
async def test_factory_failure_after_prepare_resumes_without_repeating_completed_nodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "factory-resume.sqlite"))
    monkeypatch.setenv("LANGGRAPH_DEVELOPMENT", "true")
    monkeypatch.setenv("LANGGRAPH_FAIL_AFTER_NODE", "prepare_environment")
    executor = FactoryExecutor()
    dependencies = GraphDependencies(
        tool_executor=executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=factory_arguments,
    )
    thread_id = create_thread_id()

    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_software_factory_graph(dependencies, checkpointer=checkpointer)
        initial = await graph.ainvoke(
            create_initial_state("Crea un proyecto llamado recoverable-project"),
            config=thread_config(thread_id),
        )
        assert initial.get("__interrupt__")
        service = WorkflowPersistenceService(graph)
        create_approved = await service.approve(thread_id)
        assert create_approved.interrupted is True
        with pytest.raises(RuntimeError, match="después de prepare_environment"):
            await service.approve(thread_id)
        snapshot = await WorkflowPersistenceService(graph).get_snapshot(thread_id)
        assert snapshot.values["environment_prepared"] is True
        assert snapshot.next_nodes == ("supervisor",)

        monkeypatch.delenv("LANGGRAPH_FAIL_AFTER_NODE")
        result = await WorkflowPersistenceService(graph).resume(thread_id)
        assert result.interrupted is True
        result = await WorkflowPersistenceService(graph).approve(thread_id)

    assert result.thread_id == thread_id
    assert result.final_state["terminal_status"] == "completed"
    assert executor.calls.count("filesystem__create_project_structure") == 1
    assert executor.calls.count("testing__prepare_test_environment") == 1
    assert executor.calls.count("testing__run_tests") == 1


@pytest.mark.asyncio
async def test_completed_workflow_is_not_invoked_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "completed.sqlite"))
    harness = DurableGraphHarness()
    thread_id = create_thread_id()
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_durable_graph(checkpointer, harness)
        await graph.ainvoke(create_initial_state("complete"), config=thread_config(thread_id))
        calls_before = list(harness.calls)

        result = await WorkflowPersistenceService(graph).resume(thread_id)

    assert result.already_completed is True
    assert harness.calls == calls_before


@pytest.mark.asyncio
async def test_unknown_thread_raises_domain_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "missing.sqlite"))
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_durable_graph(checkpointer, DurableGraphHarness())

        with pytest.raises(WorkflowNotFoundError, match="missing-thread"):
            await WorkflowPersistenceService(graph).get_snapshot("missing-thread")

        with pytest.raises(WorkflowNotFoundError, match="missing-thread"):
            await WorkflowPersistenceService(graph).approve("missing-thread")


@pytest.mark.asyncio
async def test_approval_rejects_completed_and_non_interrupted_workflows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "approval-errors.sqlite"))
    completed_thread = create_thread_id()
    pending_thread = create_thread_id()
    completed_harness = DurableGraphHarness()
    pending_harness = DurableGraphHarness(fail_second=True)
    async with create_sqlite_checkpointer() as checkpointer:
        completed_graph = build_durable_graph(checkpointer, completed_harness)
        await completed_graph.ainvoke(create_initial_state("completed"), config=thread_config(completed_thread))
        with pytest.raises(WorkflowAlreadyCompletedError, match="ya termin"):
            await WorkflowPersistenceService(completed_graph).approve(completed_thread)

        pending_graph = build_durable_graph(checkpointer, pending_harness)
        with pytest.raises(RuntimeError, match="injected test failure"):
            await pending_graph.ainvoke(create_initial_state("pending"), config=thread_config(pending_thread))
        with pytest.raises(WorkflowNotInterruptedError, match="no espera una aprobaci"):
            await WorkflowPersistenceService(pending_graph).approve(pending_thread)


@pytest.mark.asyncio
async def test_completed_workflow_with_pending_git_merge_interrupt_can_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "post-completion-promotion.sqlite"))
    thread_id = create_thread_id()
    harness = PostCompletionPromotionHarness()
    emitter = InMemoryWorkflowEventEmitter()
    factory = WorkflowEventFactory()
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_post_completion_promotion_graph(checkpointer, harness)
        initial = create_initial_state("promote after completion")
        initial["workflow_id"] = thread_id
        interrupted = await graph.ainvoke(initial, config=thread_config(thread_id))
        assert interrupted.get("__interrupt__")
        service = WorkflowPersistenceService(
            graph,
            streaming_enabled=True,
            event_emitter=emitter,
            event_factory=factory,
        )
        snapshot = await service.get_snapshot(thread_id)

        assert snapshot.values["terminal_status"] == "completed"
        assert snapshot.values["pending_operation"] == "git_merge"
        assert snapshot.values["pending_tool_name"] == "git__merge_workflow_branch"
        assert snapshot.interrupts

        result = await service.approve(thread_id, "Promote")

    events = emitter.get_events(thread_id)
    event_types = [event.type for event in events]
    tool_names = [event.data.get("tool") or event.data.get("tool_name") for event in events]

    assert result.interrupted is False
    assert result.final_state["terminal_status"] == "completed"
    assert result.final_state["pending_operation"] is None
    assert result.final_state["pending_tool_name"] is None
    assert result.final_state["git_promotion_state"] == "completed"
    assert harness.calls == [
        "complete",
        "git__approve_promotion",
        "git__merge_workflow_branch",
    ]
    assert WorkflowEventType.WORKFLOW_RESUMED in event_types
    assert WorkflowEventType.APPROVAL_GRANTED in event_types
    assert "approve_promotion" in tool_names
    assert "merge_workflow_branch" in tool_names


@pytest.mark.asyncio
async def test_threads_are_isolated_even_with_same_project_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "isolated.sqlite"))
    first_thread = create_thread_id()
    second_thread = create_thread_id()
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_durable_graph(checkpointer, DurableGraphHarness())
        await graph.ainvoke(create_initial_state("first request"), config=thread_config(first_thread))
        await graph.ainvoke(create_initial_state("second request"), config=thread_config(second_thread))
        service = WorkflowPersistenceService(graph)
        first = await service.get_snapshot(first_thread)
        second = await service.get_snapshot(second_thread)

    assert first.values["created_project_name"] == second.values["created_project_name"] == "medical-booking"
    assert first.values["original_user_message"] == "first request"
    assert second.values["original_user_message"] == "second request"


@pytest.mark.parametrize("representative_state", [representative_creation_state, representative_repair_state])
@pytest.mark.asyncio
async def test_representative_software_factory_state_is_serializable(
    representative_state,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / f"{representative_state.__name__}.sqlite"))
    thread_id = create_thread_id()
    async with create_sqlite_checkpointer() as checkpointer:
        builder = StateGraph(SoftwareFactoryState)

        async def identity(state: SoftwareFactoryState) -> dict[str, Any]:
            return {}

        builder.add_node("identity", identity)
        builder.add_edge(START, "identity")
        builder.add_edge("identity", END)
        graph = builder.compile(checkpointer=checkpointer)
        await graph.ainvoke(representative_state(), config=thread_config(thread_id))
        snapshot = await WorkflowPersistenceService(graph).get_snapshot(thread_id)

    assert snapshot.values["terminal_status"] == "completed"
    assert snapshot.values["created_project_name"] == "medical-booking"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sqlite_checkpoint_survives_checkpointer_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "restart.sqlite"
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(database))
    thread_id = create_thread_id()

    async with create_sqlite_checkpointer() as first_checkpointer:
        graph = build_durable_graph(first_checkpointer, DurableGraphHarness())
        await graph.ainvoke(create_initial_state("survive restart"), config=thread_config(thread_id))

    async with create_sqlite_checkpointer() as second_checkpointer:
        graph = build_durable_graph(second_checkpointer, DurableGraphHarness())
        snapshot = await WorkflowPersistenceService(graph).get_snapshot(thread_id)

    assert snapshot.values["original_user_message"] == "survive restart"
    assert snapshot.values["tests_passed"] is True
    assert snapshot.next_nodes == ()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_approval_survives_sqlite_restart_and_resumes_same_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stream_demo import publish_resume_event
    from streaming import (
        InMemoryWorkflowEventEmitter,
        SQLiteWorkflowEventSequenceStore,
        WorkflowEventFactory,
        WorkflowEventType,
    )

    database = tmp_path / "interrupt-restart.sqlite"
    event_database = tmp_path / "interrupt-restart-events.sqlite"
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(database))
    thread_id = "durable-approval-thread"
    first_executor = FactoryExecutor()
    dependencies = GraphDependencies(
        tool_executor=first_executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=factory_arguments,
    )

    sequence_store = SQLiteWorkflowEventSequenceStore(event_database)
    first_emitter = InMemoryWorkflowEventEmitter(sequence_store=sequence_store)
    first_factory = WorkflowEventFactory()
    async with create_sqlite_checkpointer() as first_checkpointer:
        first_graph = build_software_factory_graph(dependencies, checkpointer=first_checkpointer)
        first_result = await run_software_factory_graph(
            first_graph,
            "Crea un proyecto llamado recoverable-project",
            thread_id=thread_id,
            streaming_enabled=True,
            event_emitter=first_emitter,
            event_factory=first_factory,
        )
        assert first_result.interrupted is True
        assert first_result.interrupts[0].value["operation"] == "create_project"
        assert "filesystem__create_project_structure" not in first_executor.calls
        original_last_sequence = sequence_store.last_sequence(thread_id)

    restarted_executor = FactoryExecutor()
    restarted_dependencies = GraphDependencies(
        tool_executor=restarted_executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=factory_arguments,
    )
    resumed_emitter = InMemoryWorkflowEventEmitter(sequence_store=sequence_store)
    resumed_factory = WorkflowEventFactory()
    async with create_sqlite_checkpointer() as second_checkpointer:
        second_graph = build_software_factory_graph(restarted_dependencies, checkpointer=second_checkpointer)
        service = WorkflowPersistenceService(
            second_graph,
            streaming_enabled=True,
            event_emitter=resumed_emitter,
            event_factory=resumed_factory,
        )
        pending = await service.get_pending_interrupts(thread_id)
        reference = await service.load_current_pending_approval(thread_id)
        publish_resume_event(
            reference,
            event_emitter=resumed_emitter,
            event_factory=resumed_factory,
        )
        resumed = await service.approve(
            thread_id,
            "Aprobado tras reiniciar",
            expected_reference=reference,
        )

    assert len(pending) == 1
    assert pending[0].value["public_tool_name"] == "filesystem__create_project_structure"
    assert resumed.thread_id == thread_id
    assert resumed.interrupted is True
    assert resumed.interrupts[0].value["operation"] == "prepare_environment"
    assert restarted_executor.calls.count("filesystem__create_project_structure") == 1
    resumed_events = resumed_emitter.get_events(thread_id)
    resumed_types = [event.type for event in resumed_events]
    assert resumed_events[0].sequence == original_last_sequence + 1
    assert resumed_types[0] == WorkflowEventType.WORKFLOW_RESUMED
    assert WorkflowEventType.WORKFLOW_STARTED not in resumed_types
    assert WorkflowEventType.PLANNING_STARTED not in resumed_types
    assert WorkflowEventType.WORKSPACE_INSPECTION_STARTED not in resumed_types
    assert WorkflowEventType.IMPLEMENTATION_STARTED not in resumed_types


@pytest.mark.integration
@pytest.mark.asyncio
async def test_implementation_approvals_survive_separate_sqlite_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "implementation-restarts.sqlite"
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(database))
    thread_id = "implementation-restarts"

    first_executor = FactoryExecutor()
    first_dependencies = GraphDependencies(
        tool_executor=first_executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=factory_arguments,
    )
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_software_factory_graph(first_dependencies, checkpointer=checkpointer)
        result = await run_software_factory_graph(
            graph,
            "Crea un proyecto llamado recoverable-project",
            thread_id=thread_id,
        )

    assert result.interrupted is True
    assert result.interrupts[0].value["operation"] == "create_project"
    assert "filesystem__create_project_structure" not in first_executor.calls

    second_executor = FactoryExecutor()
    second_dependencies = GraphDependencies(
        tool_executor=second_executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=factory_arguments,
    )
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_software_factory_graph(second_dependencies, checkpointer=checkpointer)
        result = await WorkflowPersistenceService(graph).approve(thread_id, "Crear")

    assert result.interrupted is True
    assert result.interrupts[0].value["operation"] == "prepare_environment"
    assert second_executor.calls.count("filesystem__create_project_structure") == 1
    assert "testing__prepare_test_environment" not in second_executor.calls

    third_executor = FactoryExecutor()
    third_dependencies = GraphDependencies(
        tool_executor=third_executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=factory_arguments,
    )
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_software_factory_graph(third_dependencies, checkpointer=checkpointer)
        service = WorkflowPersistenceService(graph)
        result = await service.approve(thread_id, "Preparar")
        assert result.interrupted is True
        assert result.interrupts[0].value["operation"] == "run_tests"
        result = await service.approve(thread_id, "Ejecutar tests")

    assert result.final_state["terminal_status"] == "completed"
    assert result.final_state["tests_passed"] is True
    assert third_executor.calls.count("testing__prepare_test_environment") == 1
    assert third_executor.calls.count("testing__run_tests") == 1
    assert "filesystem__create_project_structure" not in third_executor.calls


@pytest.mark.integration
@pytest.mark.asyncio
async def test_testing_repair_subgraph_resumes_after_sqlite_restart_and_completes_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "testing-repair-subgraph.sqlite"
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(database))
    thread_id = "testing-repair-restart"
    first_executor = FactoryExecutor(fail_before_repair=True)
    dependencies = GraphDependencies(
        tool_executor=first_executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=factory_arguments,
    )

    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_software_factory_graph(dependencies, checkpointer=checkpointer)
        await graph.ainvoke(
            create_initial_state("Crea un proyecto llamado recoverable-project"),
            config=thread_config(thread_id),
        )
        service = WorkflowPersistenceService(graph)
        await service.approve(thread_id)
        run_tests_interrupt = await service.approve(thread_id)
        snapshot = await service.get_snapshot(thread_id)

        assert run_tests_interrupt.interrupted is True
        assert run_tests_interrupt.interrupts[0].value["operation"] == "run_tests"
        assert len(snapshot.interrupts) == 1
        assert snapshot.values["pending_tool_name"] == "testing__run_tests"
        parent_calls_before_restart = list(first_executor.calls)

    restarted_executor = FactoryExecutor(fail_before_repair=True)
    restarted_dependencies = GraphDependencies(
        tool_executor=restarted_executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=factory_arguments,
    )
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_software_factory_graph(restarted_dependencies, checkpointer=checkpointer)
        service = WorkflowPersistenceService(graph)
        fix_interrupt = await service.approve(thread_id)
        assert fix_interrupt.interrupted is True
        assert fix_interrupt.interrupts[0].value["operation"] == "apply_fix"
        rerun_interrupt = await service.approve(thread_id)
        assert rerun_interrupt.interrupted is True
        assert rerun_interrupt.interrupts[0].value["operation"] == "run_tests"
        completed = await service.approve(thread_id)
        history = await service.get_history(thread_id, limit=100)

    assert completed.thread_id == thread_id
    assert completed.final_state["tests_passed"] is True
    assert completed.final_state["repair_phase"] == "completed"
    assert completed.final_state["repair_attempts"] == 1
    assert first_executor.calls.count("filesystem__create_project_structure") == 1
    assert first_executor.calls.count("testing__prepare_test_environment") == 1
    assert restarted_executor.calls.count("testing__run_tests") == 2
    assert restarted_executor.calls.count("filesystem__update_project_files") == 1
    assert any(item.next_nodes == ("prepare_fix",) and item.checkpoint_namespace for item in history)
    assert "software_factory__analyze_requirement" not in restarted_executor.calls
    assert "filesystem__create_project_structure" not in restarted_executor.calls
    assert parent_calls_before_restart == first_executor.calls
