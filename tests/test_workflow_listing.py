from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.models import WorkflowSnapshotResponse
from api.services.workflow_metadata_store import WorkflowMetadataStore
from streaming.sqlite_store import SQLiteWorkflowEventStore


def snapshot(
    thread_id: str,
    *,
    project_name: str | None = None,
    intent: str | None = "create FastAPI",
    terminal_status: str = "pending",
    interrupted: bool = False,
    pending_operation: str | None = None,
    tests_executed: bool = False,
    tests_passed: bool = False,
    test_summary: str | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> WorkflowSnapshotResponse:
    return WorkflowSnapshotResponse(
        thread_id=thread_id,
        checkpoint_id=f"checkpoint-{thread_id}",
        project_name=project_name,
        workflow_intent=intent,
        terminal_status=terminal_status,
        interrupted=interrupted,
        pending_operation=pending_operation,
        pending_tool=(
            "testing__run_tests" if pending_operation is not None else None
        ),
        planning={"attempts": 1},
        implementation={"attempts": 2},
        testing={
            "executed": tests_executed,
            "passed": tests_passed,
            "final_test_result_summary": test_summary,
            "repair_phase": "completed" if tests_passed else "not_started",
            "repair_attempts": 1 if tests_passed else 0,
        },
        supervisor={"decision": "testing_repair"},
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=updated_at or datetime(2026, 1, 2, tzinfo=UTC),
    )


@pytest.fixture
async def metadata_store(tmp_path):
    event_store = SQLiteWorkflowEventStore(tmp_path / "listing.sqlite")
    await event_store.initialize()
    yield WorkflowMetadataStore(event_store.database_path)
    await event_store.close()


async def seed(store: WorkflowMetadataStore) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await store.upsert(
        snapshot(
            "thread-completed",
            project_name="Zulu API",
            terminal_status="completed",
            tests_executed=True,
            tests_passed=True,
            test_summary="1 passed, 2 warnings",
            created_at=base,
            updated_at=base + timedelta(hours=4),
        )
    )
    await store.upsert(
        snapshot(
            "thread-waiting",
            project_name="Alpha API",
            intent="Prepare HEALTH endpoint",
            interrupted=True,
            pending_operation="prepare_environment",
            created_at=base + timedelta(hours=1),
            updated_at=base + timedelta(hours=3),
        )
    )
    await store.upsert(
        snapshot(
            "thread-running",
            project_name="Middle API",
            terminal_status="running",
            created_at=base + timedelta(hours=2),
            updated_at=base + timedelta(hours=2),
        )
    )
    await store.upsert(
        snapshot(
            "thread-failed",
            project_name="Failed API",
            terminal_status="infrastructure_failed",
            created_at=base + timedelta(hours=3),
            updated_at=base + timedelta(hours=1),
        )
    )


@pytest.mark.asyncio
async def test_list_is_empty(metadata_store: WorkflowMetadataStore) -> None:
    result = await metadata_store.list(
        status=None, search=None, limit=20, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    assert result.total == 0
    assert result.items == []
    assert result.has_more is False


@pytest.mark.asyncio
async def test_list_has_typed_workflows(metadata_store: WorkflowMetadataStore) -> None:
    await seed(metadata_store)
    result = await metadata_store.list(
        status=None, search=None, limit=20, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    assert result.total == 4
    assert result.items[0].thread_id == "thread-completed"
    assert result.items[0].test_summary == "1 passed, 2 warnings"
    assert result.items[0].planning_attempts == 1
    assert result.items[0].implementation_attempts == 2


@pytest.mark.asyncio
async def test_completed_workflow_with_pending_git_merge_is_listed_as_waiting(
    metadata_store: WorkflowMetadataStore,
) -> None:
    await metadata_store.upsert(
        snapshot(
            "thread-promotion",
            project_name="Promotion API",
            terminal_status="completed",
            tests_executed=True,
            tests_passed=True,
        )
    )
    await metadata_store.upsert(
        WorkflowSnapshotResponse(
            thread_id="thread-promotion",
            checkpoint_id="checkpoint-promotion",
            project_name="Promotion API",
            workflow_intent="create FastAPI",
            terminal_status="completed",
            interrupted=True,
            pending_operation="git_merge",
            pending_tool="git__merge_workflow_branch",
            planning={"attempts": 1},
            implementation={"attempts": 2},
            testing={"executed": True, "passed": True, "repair_phase": "not_started"},
            supervisor={"decision": "git_promotion"},
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
    )

    result = await metadata_store.list(
        status=None, search="Promotion", limit=20, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    item = result.items[0]

    assert item.terminal_status == "pending"
    assert item.interrupted is True
    assert item.pending_operation == "git_merge"
    assert item.pending_tool == "git__merge_workflow_branch"
    assert item.tests_passed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort_by", "sort_order", "expected"),
    [
        ("updated_at", "desc", "thread-completed"),
        ("created_at", "asc", "thread-completed"),
        ("project_name", "asc", "thread-waiting"),
        ("project_name", "desc", "thread-completed"),
        ("terminal_status", "asc", "thread-completed"),
    ],
)
async def test_allowed_sorting(
    metadata_store: WorkflowMetadataStore,
    sort_by: str,
    sort_order: str,
    expected: str,
) -> None:
    await seed(metadata_store)
    result = await metadata_store.list(
        status=None, search=None, limit=20, offset=0,
        sort_by=sort_by, sort_order=sort_order,
    )
    assert result.items[0].thread_id == expected


@pytest.mark.asyncio
async def test_pagination_total_offset_and_has_more(
    metadata_store: WorkflowMetadataStore,
) -> None:
    await seed(metadata_store)
    first = await metadata_store.list(
        status=None, search=None, limit=2, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    second = await metadata_store.list(
        status=None, search=None, limit=2, offset=2,
        sort_by="updated_at", sort_order="desc",
    )
    assert first.total == 4
    assert len(first.items) == 2
    assert first.has_more is True
    assert second.offset == 2
    assert len(second.items) == 2
    assert second.has_more is False
    assert {item.thread_id for item in first.items}.isdisjoint(
        item.thread_id for item in second.items
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("completed", "thread-completed"),
        ("waiting", "thread-waiting"),
        ("running", "thread-running"),
        ("failed", "thread-failed"),
    ],
)
async def test_status_filters(
    metadata_store: WorkflowMetadataStore,
    status: str,
    expected: str,
) -> None:
    await seed(metadata_store)
    result = await metadata_store.list(
        status=status, search=None, limit=20, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    assert [item.thread_id for item in result.items] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("search", "expected"),
    [
        ("alpha", "thread-waiting"),
        ("THREAD-COMPLETED", "thread-completed"),
        ("health", "thread-waiting"),
        ("  alpha  ", "thread-waiting"),
    ],
)
async def test_search_fields_are_case_insensitive_and_trimmed(
    metadata_store: WorkflowMetadataStore,
    search: str,
    expected: str,
) -> None:
    await seed(metadata_store)
    result = await metadata_store.list(
        status=None, search=search, limit=20, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    assert [item.thread_id for item in result.items] == [expected]


@pytest.mark.asyncio
async def test_empty_search_does_not_filter(metadata_store: WorkflowMetadataStore) -> None:
    await seed(metadata_store)
    result = await metadata_store.list(
        status=None, search="   ", limit=20, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    assert result.total == 4


@pytest.mark.asyncio
async def test_created_at_is_immutable(metadata_store: WorkflowMetadataStore) -> None:
    original = datetime(2025, 1, 1, tzinfo=UTC)
    await metadata_store.upsert(snapshot("stable", created_at=original))
    await metadata_store.upsert(
        snapshot("stable", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    result = await metadata_store.list(
        status=None, search=None, limit=20, offset=0,
        sort_by="created_at", sort_order="asc",
    )
    assert result.items[0].created_at == original


@pytest.mark.asyncio
async def test_terminal_workflow_does_not_regress_and_pending_is_cleared(
    metadata_store: WorkflowMetadataStore,
) -> None:
    await metadata_store.upsert(
        snapshot("terminal", terminal_status="completed", tests_passed=True)
    )
    await metadata_store.upsert(
        snapshot(
            "terminal",
            interrupted=True,
            pending_operation="run_tests",
            tests_passed=False,
        )
    )
    result = await metadata_store.list(
        status=None, search=None, limit=20, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    item = result.items[0]
    assert item.terminal_status == "completed"
    assert item.interrupted is False
    assert item.pending_operation is None
    assert item.pending_tool is None
    assert item.tests_passed is True


@pytest.mark.asyncio
async def test_durable_completion_reconciles_failed_registry_projection(
    metadata_store: WorkflowMetadataStore,
) -> None:
    await metadata_store.upsert(
        snapshot("ci-recovered", terminal_status="failed", tests_passed=False)
    )
    await metadata_store.upsert(
        snapshot(
            "ci-recovered",
            terminal_status="completed",
            tests_executed=True,
            tests_passed=True,
            test_summary="1 passed, 2 warnings",
        )
    )

    result = await metadata_store.list(
        status=None,
        search=None,
        limit=20,
        offset=0,
        sort_by="updated_at",
        sort_order="desc",
    )

    item = result.items[0]
    assert item.terminal_status == "completed"
    assert item.tests_passed is True


@pytest.mark.asyncio
async def test_test_summary_is_preserved(metadata_store: WorkflowMetadataStore) -> None:
    await metadata_store.upsert(
        snapshot("summary", test_summary="3 passed", tests_executed=True)
    )
    await metadata_store.upsert(snapshot("summary", test_summary=None))
    result = await metadata_store.list(
        status=None, search=None, limit=20, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    assert result.items[0].test_summary == "3 passed"


@pytest.mark.asyncio
async def test_registry_survives_restart_and_does_not_duplicate(tmp_path) -> None:
    event_store = SQLiteWorkflowEventStore(tmp_path / "restart.sqlite")
    await event_store.initialize()
    first = WorkflowMetadataStore(event_store.database_path)
    await first.upsert(snapshot("durable", project_name="Durable API"))
    await first.upsert(snapshot("durable", project_name="Durable API"))
    await event_store.close()
    restarted_store = SQLiteWorkflowEventStore(tmp_path / "restart.sqlite")
    await restarted_store.initialize()
    second = WorkflowMetadataStore(restarted_store.database_path)
    result = await second.list(
        status=None, search=None, limit=20, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    assert result.total == 1
    assert result.items[0].project_name == "Durable API"
    await restarted_store.close()


@pytest.mark.asyncio
async def test_register_created_is_immediately_listable(
    metadata_store: WorkflowMetadataStore,
) -> None:
    await metadata_store.register_created("new-thread", " Create a durable API ")
    result = await metadata_store.list(
        status="running", search="durable", limit=20, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    assert result.total == 1
    assert result.items[0].thread_id == "new-thread"
    assert result.items[0].workflow_intent == "Create a durable API"


@pytest.mark.asyncio
async def test_registry_indexes_are_created(metadata_store: WorkflowMetadataStore) -> None:
    with sqlite3.connect(metadata_store.database_path) as connection:
        names = {
            row[1]
            for row in connection.execute("PRAGMA index_list(workflow_registry)")
        }
    assert {
        "idx_workflow_registry_updated_at",
        "idx_workflow_registry_created_at",
        "idx_workflow_registry_terminal_status",
        "idx_workflow_registry_project_name",
    }.issubset(names)


@pytest.mark.asyncio
async def test_missing_threads_only_returns_original_unregistered_streams(
    metadata_store: WorkflowMetadataStore,
) -> None:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(metadata_store.database_path) as connection:
        connection.executemany(
            """
            INSERT INTO workflow_event_streams(
                thread_id, branch_id, last_sequence, created_at, updated_at
            ) VALUES (?, ?, 1, ?, ?)
            """,
            [
                ("original-missing", "original", now, now),
                ("fork-missing", "fork-1", now, now),
            ],
        )
    assert await metadata_store.missing_original_threads() == ["original-missing"]
    await metadata_store.register_created("original-missing", "known")
    assert await metadata_store.missing_original_threads() == []


@pytest.mark.asyncio
async def test_tied_sort_uses_thread_id_as_stable_secondary_key(
    metadata_store: WorkflowMetadataStore,
) -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    await metadata_store.upsert(snapshot("thread-b", updated_at=moment))
    await metadata_store.upsert(snapshot("thread-a", updated_at=moment))
    result = await metadata_store.list(
        status=None, search=None, limit=20, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    assert [item.thread_id for item in result.items] == ["thread-a", "thread-b"]


@pytest.mark.asyncio
async def test_list_model_does_not_expose_snapshot_secrets(
    metadata_store: WorkflowMetadataStore,
) -> None:
    await metadata_store.upsert(snapshot("public-fields"))
    result = await metadata_store.list(
        status=None, search=None, limit=20, offset=0,
        sort_by="updated_at", sort_order="desc",
    )
    keys = result.items[0].model_dump().keys()
    assert "pending_tool_arguments" not in keys
    assert "generated_files" not in keys
    assert "original_user_message" not in keys


class ListingQuery:
    async def list_workflows(self, **kwargs):
        return SimpleNamespace(
            model_dump=lambda: {
                "items": [],
                "total": 0,
                "limit": kwargs["limit"],
                "offset": kwargs["offset"],
                "has_more": False,
            }
        )


def listing_client() -> TestClient:
    services = SimpleNamespace(query=ListingQuery())

    @asynccontextmanager
    async def factory():
        yield services

    return TestClient(create_app(factory))


@pytest.mark.parametrize(
    "query",
    [
        "?limit=0",
        "?limit=101",
        "?offset=-1",
        "?status=unknown",
        "?sort_by=updated_at%3BDROP%20TABLE%20workflow_registry",
        "?sort_order=sideways",
    ],
)
def test_invalid_list_filters_return_422(query: str) -> None:
    with listing_client() as client:
        assert client.get(f"/api/workflows{query}").status_code == 422
