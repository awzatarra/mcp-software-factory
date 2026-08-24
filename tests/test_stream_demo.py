from __future__ import annotations

import pytest
from datetime import UTC, datetime
from uuid import uuid4

from stream_demo import (
    AUTO_APPROVE,
    INTERACTIVE_APPROVALS,
    STOP_ON_APPROVAL,
    build_parser,
    parse_approval_answer,
    publish_resume_event,
    validate_cli_args,
)
from graph.persistence_service import PendingApprovalReference
from streaming.consumer import TerminalWorkflowEventConsumer
from streaming import (
    EventStatus,
    InMemoryWorkflowEventEmitter,
    SQLiteWorkflowEventSequenceStore,
    WorkflowEvent,
    WorkflowEventFactory,
    WorkflowEventType,
)


def make_event(sequence: int, *, stage: str | None = None) -> WorkflowEvent:
    return WorkflowEvent(
        event_id=uuid4(),
        thread_id="demo-thread",
        sequence=sequence,
        type=WorkflowEventType.STAGE_STARTED,
        timestamp=datetime.now(UTC),
        source="test",
        stage=stage,
        status=EventStatus.RUNNING,
        message=None,
        data={},
    )


@pytest.mark.parametrize("value", ["y", "yes", "s", "si", "sí", " SÍ "])
def test_parse_approval_answer_accepts_only_explicit_yes(value: str) -> None:
    assert parse_approval_answer(value) is True


@pytest.mark.parametrize("value", ["", " ", "n", "no", "continue", "unknown"])
def test_parse_approval_answer_rejects_non_explicit_values(value: str) -> None:
    assert parse_approval_answer(value) is False


def test_stop_on_approval_is_default() -> None:
    args = build_parser().parse_args([])
    assert args.approval_mode == STOP_ON_APPROVAL


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("--stop-on-approval", STOP_ON_APPROVAL),
        ("--interactive-approvals", INTERACTIVE_APPROVALS),
        ("--auto-approve", AUTO_APPROVE),
    ],
)
def test_approval_mode_flags(flag: str, expected: str) -> None:
    assert build_parser().parse_args([flag]).approval_mode == expected


def test_approval_modes_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--interactive-approvals", "--auto-approve"])


def test_resume_thread_is_parsed_without_request() -> None:
    args = build_parser().parse_args(["--resume-thread", "thread-123"])
    assert args.resume_thread == "thread-123"
    assert args.request is None


def test_resume_thread_rejects_request(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    args = parser.parse_args(["--resume-thread", "thread-123", "new request"])
    with pytest.raises(SystemExit):
        validate_cli_args(parser, args)
    assert "Request is not allowed with --resume-thread" in capsys.readouterr().err


def test_compact_consumer_uses_continuous_visual_sequence() -> None:
    output: list[str] = []
    consumer = TerminalWorkflowEventConsumer(output=output.append)
    consumer.consume(make_event(sequence=40))
    consumer.consume(make_event(sequence=52, stage="next"))
    assert output[0].startswith("[001]")
    assert output[1].startswith("[002]")


def test_compact_consumer_can_continue_persisted_sequence() -> None:
    output: list[str] = []
    consumer = TerminalWorkflowEventConsumer(start_sequence=25, output=output.append)
    consumer.consume(make_event(sequence=26))
    assert output[0].startswith("[026]")


def test_resume_event_continues_sqlite_sequence_without_history(
    tmp_path,
) -> None:
    store = SQLiteWorkflowEventSequenceStore(tmp_path / "events.sqlite")
    first_emitter = InMemoryWorkflowEventEmitter(sequence_store=store)
    first_emitter.emit(make_event(sequence=25, stage="approval"))

    resumed_emitter = InMemoryWorkflowEventEmitter(sequence_store=store)
    received: list[WorkflowEvent] = []
    resumed_emitter.subscribe("demo-thread", received.append)
    publish_resume_event(
        PendingApprovalReference(
            thread_id="demo-thread",
            checkpoint_id="checkpoint-25",
            operation="create_project",
            tool_name="filesystem__create_project_structure",
        ),
        event_emitter=resumed_emitter,
        event_factory=WorkflowEventFactory(),
    )

    assert [event.sequence for event in received] == [26]
    assert [event.type for event in received] == [WorkflowEventType.WORKFLOW_RESUMED]
    assert store.last_sequence("demo-thread") == 26
    assert resumed_emitter.get_events("demo-thread") == received


def test_reconnect_does_not_repeat_persisted_logical_event(tmp_path) -> None:
    store = SQLiteWorkflowEventSequenceStore(tmp_path / "dedup-events.sqlite")
    first = InMemoryWorkflowEventEmitter(sequence_store=store)
    approval = make_event(sequence=25, stage="prepare_environment")
    approval.type = WorkflowEventType.APPROVAL_REQUIRED
    approval.data = {
        "branch_id": "original",
        "operation": "prepare_environment",
        "tool_name": "testing__prepare_test_environment",
    }
    first.emit(approval)

    reconnected = InMemoryWorkflowEventEmitter(sequence_store=store)
    repeated = approval.model_copy(update={"event_id": uuid4(), "sequence": 1})
    reconnected.emit(repeated)

    assert reconnected.get_events("demo-thread") == []
    assert store.last_sequence("demo-thread") == 25
