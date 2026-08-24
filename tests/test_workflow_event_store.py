from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import sqlite3
from uuid import uuid4

import pytest

from api.services.event_broker import WorkflowEventBroker
from streaming import (
    DurableWorkflowEventEmitter,
    EventStatus,
    WorkflowEvent,
    WorkflowEventFactory,
    WorkflowEventType,
)
from streaming.errors import (
    ConflictingTerminalEventError,
    WorkflowEventPersistenceError,
    WorkflowEventStoreClosedError,
)
from streaming.sqlite_store import SQLiteWorkflowEventStore


def make_event(
    sequence: int,
    *,
    thread_id: str = "durable-thread",
    branch_id: str = "original",
    event_type: WorkflowEventType = WorkflowEventType.STAGE_STARTED,
    stage: str = "planning",
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
        status=(
            EventStatus.COMPLETED
            if event_type == WorkflowEventType.WORKFLOW_COMPLETED
            else EventStatus.FAILED
            if event_type == WorkflowEventType.WORKFLOW_FAILED
            else EventStatus.RUNNING
        ),
        message=None,
        data={
            "branch_id": branch_id,
            "lineage": "original" if branch_id == "original" else "fork",
            **(data or {}),
        },
    )


@pytest.fixture
async def store(tmp_path):
    selected = SQLiteWorkflowEventStore(tmp_path / "workflow-events.sqlite")
    await selected.initialize()
    try:
        yield selected
    finally:
        await selected.close()


@pytest.mark.asyncio
async def test_initialize_creates_tables_and_is_idempotent(tmp_path) -> None:
    selected = SQLiteWorkflowEventStore(tmp_path / "initialize.sqlite")
    await selected.initialize()
    await selected.initialize()
    with sqlite3.connect(selected.database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"workflow_events", "workflow_event_streams"} <= tables
    await selected.close()


@pytest.mark.asyncio
async def test_initialize_migrates_table_without_logical_key(tmp_path) -> None:
    path = tmp_path / "legacy-schema.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE workflow_events (
                event_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                stage TEXT NULL,
                status TEXT NOT NULL,
                message TEXT NULL,
                data_json TEXT NOT NULL,
                checkpoint_id TEXT NULL,
                lineage TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(thread_id, branch_id, sequence)
            )
            """
        )
    selected = SQLiteWorkflowEventStore(path)
    await selected.initialize()
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(workflow_events)")
        }
    assert "logical_key" in columns
    await selected.close()


@pytest.mark.asyncio
async def test_append_inserts_and_duplicate_returns_false(store) -> None:
    event = make_event(1)
    assert await store.append(event) is True
    assert await store.append(event) is False
    assert await store.get_events("durable-thread") == [event]


@pytest.mark.asyncio
async def test_sequence_starts_at_one_and_increments(store) -> None:
    assert await store.reserve_next_sequence("new-thread", "original") == 1
    assert await store.reserve_next_sequence("new-thread", "original") == 2


@pytest.mark.asyncio
async def test_sequence_survives_store_restart(tmp_path) -> None:
    path = tmp_path / "restart.sqlite"
    first = SQLiteWorkflowEventStore(path)
    await first.initialize()
    assert await first.reserve_next_sequence("restart-thread", "original") == 1
    await first.close()
    second = SQLiteWorkflowEventStore(path)
    await second.initialize()
    assert await second.reserve_next_sequence("restart-thread", "original") == 2
    await second.close()


@pytest.mark.asyncio
async def test_concurrent_sequence_reservations_are_unique(store) -> None:
    sequences = await asyncio.gather(
        *(
            store.reserve_next_sequence("concurrent-thread", "original")
            for _ in range(25)
        )
    )
    assert sorted(sequences) == list(range(1, 26))


@pytest.mark.asyncio
async def test_get_events_orders_and_filters_after_sequence(store) -> None:
    await store.append_many([make_event(3), make_event(1), make_event(2)])
    events = await store.get_events("durable-thread", after_sequence=1)
    assert [event.sequence for event in events] == [2, 3]


@pytest.mark.asyncio
async def test_get_events_applies_limit_and_event_type(store) -> None:
    await store.append_many(
        [
            make_event(1),
            make_event(2, event_type=WorkflowEventType.TOOL_STARTED),
            make_event(3, event_type=WorkflowEventType.TOOL_STARTED),
        ]
    )
    events = await store.get_events(
        "durable-thread",
        limit=1,
        event_types=[WorkflowEventType.TOOL_STARTED.value],
    )
    assert [event.sequence for event in events] == [2]


@pytest.mark.asyncio
async def test_terminal_metadata_is_updated(store) -> None:
    terminal = make_event(
        4,
        event_type=WorkflowEventType.WORKFLOW_COMPLETED,
        stage="finalize",
    )
    await store.append(terminal)
    assert await store.get_terminal_event("durable-thread") == terminal
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            """
            SELECT terminal_event_type, terminal_sequence
            FROM workflow_event_streams
            WHERE thread_id = ? AND branch_id = ?
            """,
            ("durable-thread", "original"),
        ).fetchone()
    assert row == ("workflow_completed", 4)


@pytest.mark.asyncio
async def test_incompatible_terminal_event_is_rejected(store) -> None:
    await store.append(
        make_event(1, event_type=WorkflowEventType.WORKFLOW_COMPLETED)
    )
    with pytest.raises(ConflictingTerminalEventError):
        await store.append(
            make_event(2, event_type=WorkflowEventType.WORKFLOW_FAILED)
        )


@pytest.mark.asyncio
async def test_append_many_returns_inserted_count(store) -> None:
    first = make_event(1)
    second = make_event(2)
    assert await store.append_many([first, second, first]) == 2


@pytest.mark.asyncio
async def test_corrupt_row_is_skipped_and_counted(store) -> None:
    event = make_event(1)
    await store.append(event)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE workflow_events SET data_json = ? WHERE event_id = ?",
            ("{broken", str(event.event_id)),
        )
    assert await store.get_events("durable-thread") == []
    assert store.corrupt_rows() == 1


@pytest.mark.asyncio
async def test_limited_read_continues_after_corrupt_row(store) -> None:
    first = make_event(1)
    second = make_event(2)
    await store.append_many([first, second])
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE workflow_events SET data_json = ? WHERE event_id = ?",
            ("{broken", str(first.event_id)),
        )
    events = await store.get_events("durable-thread", limit=1)
    assert events == [second]


@pytest.mark.asyncio
async def test_wal_busy_timeout_and_foreign_keys_are_configured(store) -> None:
    assert str(store.pragma_sync("journal_mode")).lower() == "wal"
    assert store.pragma_sync("busy_timeout") == 5_000
    assert store.pragma_sync("foreign_keys") == 1


@pytest.mark.asyncio
async def test_closed_store_rejects_new_operations(tmp_path) -> None:
    selected = SQLiteWorkflowEventStore(tmp_path / "closed.sqlite")
    await selected.initialize()
    await selected.close()
    with pytest.raises(WorkflowEventStoreClosedError):
        await selected.get_events("thread")


@pytest.mark.asyncio
async def test_prune_removes_old_events_and_preserves_terminal(store) -> None:
    old = make_event(1)
    terminal = make_event(
        2,
        event_type=WorkflowEventType.WORKFLOW_COMPLETED,
        stage="finalize",
    )
    await store.append_many([old, terminal])
    old_timestamp = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE workflow_events SET created_at = ?",
            (old_timestamp,),
        )
    assert await store.prune(retention_days=30, max_per_thread=100) == 1
    assert await store.get_events("durable-thread") == [terminal]


@pytest.mark.asyncio
async def test_prune_enforces_maximum_without_removing_terminal(store) -> None:
    await store.append_many(
        [
            make_event(1),
            make_event(2),
            make_event(3),
            make_event(4, event_type=WorkflowEventType.WORKFLOW_COMPLETED),
        ]
    )
    assert await store.prune(retention_days=365, max_per_thread=1) == 2
    assert [
        event.sequence for event in await store.get_events("durable-thread")
    ] == [3, 4]


@pytest.mark.asyncio
async def test_branches_have_independent_sequences_and_history(store) -> None:
    assert await store.reserve_next_sequence("branch-thread", "original") == 1
    assert await store.reserve_next_sequence("branch-thread", "fork-1") == 1
    original = make_event(1, thread_id="branch-thread")
    fork = make_event(1, thread_id="branch-thread", branch_id="fork-1")
    await store.append_many([original, fork])
    assert await store.get_events("branch-thread") == [original]
    assert await store.get_events(
        "branch-thread",
        branch_id="fork-1",
    ) == [fork]


def test_durable_emitter_persists_before_live_delivery(tmp_path) -> None:
    selected = SQLiteWorkflowEventStore(tmp_path / "emitter.sqlite")
    selected.initialize_sync()
    emitter = DurableWorkflowEventEmitter(selected)
    delivered: list[WorkflowEvent] = []
    emitter.subscribe("emitter-thread", delivered.append)
    event = WorkflowEventFactory().create(
        thread_id="emitter-thread",
        event_type=WorkflowEventType.STAGE_STARTED,
        source="test",
        stage="planning",
        status=EventStatus.RUNNING,
    )
    emitter.emit(event)
    assert selected.get_events_sync("emitter-thread") == delivered == [event]


def test_durable_emitter_deduplicates_logical_replay(tmp_path) -> None:
    selected = SQLiteWorkflowEventStore(tmp_path / "dedupe.sqlite")
    selected.initialize_sync()
    emitter = DurableWorkflowEventEmitter(selected)
    delivered: list[WorkflowEvent] = []
    emitter.subscribe("dedupe-thread", delivered.append)
    first = make_event(
        1,
        thread_id="dedupe-thread",
        data={"node": "inspect_workspace"},
    )
    duplicate = make_event(
        1,
        thread_id="dedupe-thread",
        data={"node": "inspect_workspace"},
    )
    emitter.emit(first)
    emitter.emit(duplicate)
    assert len(delivered) == 1
    assert len(selected.get_events_sync("dedupe-thread")) == 1
    assert emitter.deduplicated_events() == 1
    assert selected.get_last_sequence_sync("dedupe-thread") == 1


def test_durable_emitter_keeps_branch_sequences_independent(tmp_path) -> None:
    selected = SQLiteWorkflowEventStore(tmp_path / "emitter-branches.sqlite")
    selected.initialize_sync()
    emitter = DurableWorkflowEventEmitter(selected)
    original = make_event(99, thread_id="emitter-branches")
    fork = make_event(
        99,
        thread_id="emitter-branches",
        branch_id="fork-1",
    )
    emitter.emit(original)
    emitter.emit(fork)
    assert original.sequence == 1
    assert fork.sequence == 1
    assert selected.get_events_sync("emitter-branches") == [original]
    assert selected.get_events_sync(
        "emitter-branches",
        branch_id="fork-1",
    ) == [fork]


def test_persistence_failure_does_not_publish_live() -> None:
    class FailingStore:
        def has_logical_event_sync(self, thread_id, branch_id, logical_key):
            return False

        def reserve_next_sequence_sync(self, thread_id, branch_id):
            return 1

        def append_sync(self, event):
            raise WorkflowEventPersistenceError("disk unavailable")

        def get_last_sequence_sync(self, thread_id, branch_id):
            return 0

    emitter = DurableWorkflowEventEmitter(FailingStore(), persistence_retries=2)
    delivered: list[WorkflowEvent] = []
    emitter.subscribe("failed-thread", delivered.append)
    with pytest.raises(WorkflowEventPersistenceError):
        emitter.emit(make_event(1, thread_id="failed-thread"))
    assert delivered == []


@pytest.mark.asyncio
async def test_broker_delivers_durable_history(store) -> None:
    event = make_event(1)
    await store.append(event)
    broker = WorkflowEventBroker(store=store)
    terminal = make_event(
        2,
        event_type=WorkflowEventType.WORKFLOW_COMPLETED,
    )
    await store.append(terminal)
    received = [item async for item in broker.subscribe("durable-thread")]
    assert received == [event, terminal]


@pytest.mark.asyncio
async def test_broker_delivers_live_after_durable_history(store) -> None:
    historical = make_event(1)
    await store.append(historical)
    broker = WorkflowEventBroker(store=store)
    stream = broker.subscribe("durable-thread")
    first = await anext(stream)
    live = make_event(2)
    await store.append(live)
    waiting = asyncio.create_task(anext(stream))
    await broker.publish(live, already_persisted=True)
    assert first == historical
    assert await waiting == live
    await stream.aclose()


@pytest.mark.asyncio
async def test_history_live_race_neither_loses_nor_duplicates(store) -> None:
    historical = make_event(1)
    await store.append(historical)
    history_started = asyncio.Event()
    release_history = asyncio.Event()

    class SlowStore:
        async def get_events(self, *args, **kwargs):
            history_started.set()
            await release_history.wait()
            return await store.get_events(*args, **kwargs)

        async def append(self, event):
            return await store.append(event)

    broker = WorkflowEventBroker(store=SlowStore())
    stream = broker.subscribe("durable-thread")
    first_task = asyncio.create_task(anext(stream))
    await history_started.wait()
    raced = make_event(2)
    await store.append(raced)
    await broker.publish(raced, already_persisted=True)
    release_history.set()
    first = await first_task
    second = await anext(stream)
    assert [first.sequence, second.sequence] == [1, 2]
    await stream.aclose()


@pytest.mark.asyncio
async def test_broker_restart_reads_after_last_event_id(store) -> None:
    await store.append_many([make_event(1), make_event(2), make_event(3)])
    restarted_broker = WorkflowEventBroker(store=store)
    events = await restarted_broker.get_history(
        "durable-thread",
        after_sequence=2,
    )
    assert [event.sequence for event in events] == [3]
