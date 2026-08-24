from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from graph.runtime import run_software_factory_graph
from graph.nodes import (
    GraphDependencies,
    inspect_workspace_node,
    resolve_tool_stage,
)
from streaming import (
    EventStatus,
    InMemoryWorkflowEventEmitter,
    WorkflowEvent,
    WorkflowEventFactory,
    WorkflowEventSequence,
    WorkflowEventType,
    emit_workflow_event,
)
from streaming.consumer import TerminalWorkflowEventConsumer
from streaming.formatting import format_terminal_event
from streaming.mapper import LangGraphEventMapper
from streaming.sanitizer import sanitize_event_data
from streaming.context import workflow_event_context
from tool_executor import ToolExecutionOutcome


def make_event(
    event_type: WorkflowEventType = WorkflowEventType.WORKFLOW_STARTED,
    *,
    thread_id: str = "thread-1",
    sequence: int = 1,
    stage: str | None = None,
    data: dict | None = None,
) -> WorkflowEvent:
    return WorkflowEvent(
        event_id=uuid4(),
        thread_id=thread_id,
        sequence=sequence,
        type=event_type,
        timestamp=datetime.now(UTC),
        source="test",
        stage=stage,
        status=EventStatus.RUNNING,
        message=None,
        data=data or {},
    )


def test_workflow_event_serializes_to_json() -> None:
    payload = make_event().model_dump(mode="json")
    assert payload["type"] == "workflow_started"
    assert isinstance(payload["event_id"], str)
    assert payload["timestamp"].endswith("Z")


def test_workflow_event_rejects_extra_fields() -> None:
    payload = make_event().model_dump()
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        WorkflowEvent.model_validate(payload)


def test_workflow_event_rejects_unknown_type() -> None:
    payload = make_event().model_dump()
    payload["type"] = "arbitrary_event"
    with pytest.raises(ValidationError):
        WorkflowEvent.model_validate(payload)


def test_sequence_starts_at_one_and_increases() -> None:
    sequence = WorkflowEventSequence()
    assert sequence.next("a") == 1
    assert sequence.next("a") == 2


def test_sequence_is_independent_per_thread() -> None:
    sequence = WorkflowEventSequence()
    assert sequence.next("a") == 1
    assert sequence.next("b") == 1


def test_sequence_can_be_reconstructed() -> None:
    sequence = WorkflowEventSequence({"a": 8})
    assert sequence.next("a") == 9
    assert sequence.snapshot() == {"a": 9}


def test_sequence_ensure_at_least_continues_after_resume() -> None:
    sequence = WorkflowEventSequence()
    sequence.ensure_at_least("a", 52)
    assert sequence.next("a") == 53


@pytest.mark.parametrize(
    "key",
    ["api_key", "authorization", "token", "secret", "password", "cookie", "openai_api_key"],
)
def test_sanitizer_redacts_secrets(key: str) -> None:
    assert sanitize_event_data({key: "sensitive"})[key] == "[redacted]"


def test_sanitizer_redacts_nested_secret() -> None:
    assert sanitize_event_data({"headers": {"Authorization": "Bearer abc"}})["headers"]["Authorization"] == "[redacted]"


def test_sanitizer_summarizes_generated_files() -> None:
    sanitized = sanitize_event_data(
        {"generated_files": [{"path": "app.py", "content": "secret source"}]}
    )
    assert sanitized == {"generated_file_count": 1, "generated_file_paths": ["app.py"]}


def test_sanitizer_truncates_long_strings_and_output() -> None:
    sanitized = sanitize_event_data({"value": "a" * 1_100, "stdout": "b" * 2_100})
    assert len(sanitized["value"]) < 1_100
    assert len(sanitized["stdout"]) < 2_100
    assert "truncated" in sanitized["stdout"]


def test_sanitizer_converts_common_json_types() -> None:
    class Choice(StrEnum):
        A = "a"

    now = datetime.now(UTC)
    identifier = uuid4()
    sanitized = sanitize_event_data(
        {"path": Path("a/b"), "choice": Choice.A, "id": identifier, "time": now}
    )
    assert sanitized["path"] == str(Path("a/b"))
    assert sanitized["choice"] == "a"
    assert sanitized["id"] == str(identifier)
    assert sanitized["time"] == now.isoformat()


def test_sanitizer_rejects_unsupported_objects() -> None:
    with pytest.raises(TypeError, match="Unsupported"):
        sanitize_event_data({"bad": object()})


def test_factory_assigns_sequence_and_sanitizes() -> None:
    factory = WorkflowEventFactory()
    event = factory.create(
        thread_id="a",
        event_type=WorkflowEventType.TOOL_STARTED,
        source="test",
        stage="implementation",
        status=EventStatus.RUNNING,
        data={"password": "nope"},
    )
    assert event.sequence == 1
    assert event.data["password"] == "[redacted]"


@pytest.mark.parametrize(
    ("node_name", "operation", "tool_name", "expected"),
    [
        ("inspect_workspace", None, "filesystem__list_files", "inspect_workspace"),
        (None, None, "software_factory__analyze_requirement", "planning"),
        (None, None, "software_factory__create_tasks", "planning"),
        (None, None, "filesystem__create_project_structure", "implementation"),
        (None, None, "testing__detect_test_framework", "implementation"),
        (None, None, "testing__prepare_test_environment", "implementation"),
        (None, None, "testing__run_tests", "testing_repair"),
        ("read_failing_test", None, "filesystem__read_file", "testing_repair"),
        ("apply_fix", None, "filesystem__update_project_files", "testing_repair"),
        (None, None, "unknown__tool", None),
    ],
)
def test_tool_stage_resolution(
    node_name: str | None,
    operation: str | None,
    tool_name: str,
    expected: str | None,
) -> None:
    assert resolve_tool_stage(
        node_name=node_name,
        operation=operation,
        tool_name=tool_name,
        state={"last_completed_stage": "planning"},
    ) == expected


@pytest.mark.asyncio
async def test_list_files_events_use_inspect_workspace_stage() -> None:
    class Executor:
        def openai_tool(self, public_tool_name: str) -> dict:
            return {"type": "function", "name": public_tool_name}

        async def execute(self, public_tool_name, arguments, **kwargs):
            return ToolExecutionOutcome(
                public_tool_name,
                arguments,
                {"success": True},
                {"workspace_inspected": True},
            )

    emitter = InMemoryWorkflowEventEmitter()
    with workflow_event_context(
        thread_id="inspect-stage",
        emitter=emitter,
        factory=WorkflowEventFactory(),
    ):
        await inspect_workspace_node(
            {"last_completed_stage": "planning"},
            GraphDependencies(
                tool_executor=Executor(),
                openai_client=object(),
                model="test",
            ),
        )

    tool_events = [
        event
        for event in emitter.get_events("inspect-stage")
        if event.type
        in {
            WorkflowEventType.TOOL_STARTED,
            WorkflowEventType.TOOL_COMPLETED,
        }
    ]
    assert [event.stage for event in tool_events] == [
        "inspect_workspace",
        "inspect_workspace",
    ]


def test_emitter_publishes_and_filters_by_thread() -> None:
    emitter = InMemoryWorkflowEventEmitter()
    emitter.emit(make_event(thread_id="a"))
    emitter.emit(make_event(thread_id="b"))
    assert [event.thread_id for event in emitter.get_events("a")] == ["a"]


def test_emitter_notifies_subscriber() -> None:
    emitter = InMemoryWorkflowEventEmitter()
    received: list[WorkflowEvent] = []
    emitter.subscribe("a", received.append)
    emitter.emit(make_event(thread_id="a"))
    assert len(received) == 1


def test_broken_consumer_does_not_break_emitter() -> None:
    emitter = InMemoryWorkflowEventEmitter()
    emitter.subscribe("a", lambda _event: (_ for _ in ()).throw(RuntimeError("broken")))
    emitter.emit(make_event(thread_id="a"))
    assert len(emitter.get_events("a")) == 1


def test_emitter_deduplicates_logical_event() -> None:
    emitter = InMemoryWorkflowEventEmitter()
    emitter.emit(make_event(thread_id="a"))
    emitter.emit(make_event(thread_id="a", sequence=2))
    assert len(emitter.get_events("a")) == 1


def test_emitter_repairs_regressive_sequence_from_recreated_factory() -> None:
    emitter = InMemoryWorkflowEventEmitter()
    emitter.emit(make_event(thread_id="a", sequence=52, stage="first"))
    regressive = make_event(thread_id="a", sequence=1, stage="second")
    emitter.emit(regressive)
    assert [event.sequence for event in emitter.get_events("a")] == [52, 53]


def test_reconnect_subscription_does_not_replay_historical_events() -> None:
    emitter = InMemoryWorkflowEventEmitter()
    emitter.emit(make_event(thread_id="a", stage="historical"))
    received: list[WorkflowEvent] = []
    emitter.subscribe("a", received.append)
    emitter.emit(make_event(thread_id="a", sequence=2, stage="current"))
    assert [event.stage for event in received] == ["current"]


def test_emitter_preserves_critical_event_under_backpressure() -> None:
    emitter = InMemoryWorkflowEventEmitter(max_events_per_thread=1)
    emitter.emit(make_event(WorkflowEventType.STAGE_STARTED, thread_id="a", stage="one"))
    emitter.emit(make_event(WorkflowEventType.APPROVAL_REQUIRED, thread_id="a", stage="approval"))
    assert emitter.get_events("a")[-1].type == WorkflowEventType.APPROVAL_REQUIRED
    assert emitter.dropped_events("a") == 1


def test_mapper_maps_planning_result() -> None:
    mapper = LangGraphEventMapper(WorkflowEventFactory())
    events = mapper.map_update(
        thread_id="a",
        namespace=("planning",),
        node_name="validate_plan",
        update={"planning_result": {"valid": True}},
    )
    assert events[0].type == WorkflowEventType.PLANNING_COMPLETED
    assert events[0].data["subgraph"] == "planning"


def test_mapper_maps_implementation_result() -> None:
    events = LangGraphEventMapper(WorkflowEventFactory()).map_update(
        thread_id="a",
        namespace=("implementation",),
        node_name="_exit",
        update={"implementation_result": {"project_created": True}},
    )
    assert events[0].type == WorkflowEventType.IMPLEMENTATION_COMPLETED


def test_mapper_maps_testing_result() -> None:
    events = LangGraphEventMapper(WorkflowEventFactory()).map_update(
        thread_id="a",
        namespace=("testing_repair",),
        node_name="_exit",
        update={"testing_result": {"tests_passed": True}},
    )
    assert events[0].type == WorkflowEventType.TESTING_COMPLETED


def test_mapper_preserves_subgraph_namespace() -> None:
    event = LangGraphEventMapper(WorkflowEventFactory()).map_update(
        thread_id="a",
        namespace=("implementation", "validate_implementation"),
        node_name="validate_implementation",
        update={"implementation_result": {"project_created": True}},
    )[0]
    assert event.data["namespace"] == ["implementation", "validate_implementation"]
    assert event.data["parent_stage"] == "implementation"


def test_mapper_deduplicates_terminal_event() -> None:
    mapper = LangGraphEventMapper(WorkflowEventFactory())
    first = mapper.map_update(
        thread_id="a",
        namespace=(),
        node_name="finalize",
        update={"terminal_status": "completed"},
    )
    second = mapper.map_update(
        thread_id="a",
        namespace=(),
        node_name="finalize",
        update={"terminal_status": "completed"},
    )
    assert first[0].type == WorkflowEventType.WORKFLOW_COMPLETED
    assert second == []


def test_mapper_does_not_close_workflow_before_finalize() -> None:
    events = LangGraphEventMapper(WorkflowEventFactory()).map_update(
        thread_id="a",
        namespace=("implementation",),
        node_name="implementation",
        update={"terminal_status": "implementation_failed"},
    )
    assert all(
        event.type not in {WorkflowEventType.WORKFLOW_COMPLETED, WorkflowEventType.WORKFLOW_FAILED}
        for event in events
    )


def test_terminal_consumer_formats_friendly_line() -> None:
    output: list[str] = []
    consumer = TerminalWorkflowEventConsumer(output=output.append)
    consumer.consume(make_event(data={"project_name": "streaming-health-api"}))
    assert output == ["[001] Workflow started: streaming-health-api"]


def test_terminal_consumer_debug_prints_json() -> None:
    output: list[str] = []
    TerminalWorkflowEventConsumer(debug=True, output=output.append).consume(make_event())
    assert '"thread_id": "thread-1"' in output[0]


def test_emit_helper_is_noop_outside_runtime_context() -> None:
    assert (
        emit_workflow_event(
            WorkflowEventType.WORKFLOW_STARTED,
            source="test",
            stage=None,
            status=EventStatus.RUNNING,
        )
        is None
    )


class FakeGraph:
    def __init__(self) -> None:
        self.ainvoke_calls = 0
        self.astream_calls = 0
        self.snapshot = SimpleNamespace(
            values={"terminal_status": "completed", "final_response": "done"},
            tasks=(),
            interrupts=(),
        )

    async def ainvoke(self, state, config):
        self.ainvoke_calls += 1
        return dict(self.snapshot.values)

    async def astream(self, state, config, stream_mode, subgraphs):
        self.astream_calls += 1
        assert stream_mode == ["updates", "custom"]
        assert subgraphs is True
        yield ((), "updates", {"finalize": {"terminal_status": "completed"}})

    async def aget_state(self, config, subgraphs=True):
        return self.snapshot


@pytest.mark.asyncio
async def test_runtime_streaming_emits_lifecycle_once(capsys) -> None:
    graph = FakeGraph()
    emitter = InMemoryWorkflowEventEmitter()
    result = await run_software_factory_graph(
        graph,
        "create project",
        thread_id="stream-thread",
        streaming_enabled=True,
        event_emitter=emitter,
        event_factory=WorkflowEventFactory(),
    )
    types = [event.type for event in emitter.get_events("stream-thread")]
    assert types.count(WorkflowEventType.WORKFLOW_STARTED) == 1
    assert types.count(WorkflowEventType.WORKFLOW_COMPLETED) == 1
    assert WorkflowEventType.WORKFLOW_FAILED not in types
    assert result.final_state["terminal_status"] == "completed"
    assert graph.astream_calls == 1
    assert graph.ainvoke_calls == 0
    capsys.readouterr()


@pytest.mark.asyncio
async def test_runtime_without_streaming_preserves_ainvoke_path(capsys) -> None:
    graph = FakeGraph()
    result = await run_software_factory_graph(
        graph,
        "create project",
        thread_id="normal-thread",
        streaming_enabled=False,
    )
    assert result.final_state["terminal_status"] == "completed"
    assert graph.ainvoke_calls == 1
    assert graph.astream_calls == 0
    capsys.readouterr()


@pytest.mark.parametrize(
    ("event_type", "lineage"),
    [
        (WorkflowEventType.WORKFLOW_RESUMED, "replay"),
        (WorkflowEventType.WORKFLOW_FORKED, "fork"),
    ],
)
def test_lineage_is_preserved_in_event_data(
    event_type: WorkflowEventType,
    lineage: str,
) -> None:
    event = make_event(event_type, data={"lineage": lineage, "origin_checkpoint": "cp-1", "branch_id": "b-1"})
    assert event.data["lineage"] == lineage
    assert event.data["origin_checkpoint"] == "cp-1"


def test_approval_preview_does_not_include_file_contents() -> None:
    event = WorkflowEventFactory().create(
        thread_id="a",
        event_type=WorkflowEventType.APPROVAL_REQUIRED,
        source="approval",
        stage="create_project",
        status=EventStatus.WAITING,
        data={"generated_files": [{"path": "main.py", "content": "full source"}]},
    )
    assert "full source" not in event.model_dump_json()
    assert event.data["generated_file_paths"] == ["main.py"]


def test_formatting_uses_operation_for_approval() -> None:
    event = make_event(
        WorkflowEventType.APPROVAL_REQUIRED,
        data={"operation": "run_tests"},
    )
    assert format_terminal_event(event) == "[001] Waiting approval: run_tests"
