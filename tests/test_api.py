from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.app import create_app, parse_cors_origins
from api.git_models import GitCommitResult
from api.errors import ApprovalConflictError
from api.models import (
    ApprovalResponse,
    CreateWorkflowRequest,
    WorkflowSnapshotResponse,
)
from api.services.event_broker import WorkflowEventBroker
from api.services.sse_service import parse_last_event_id
from api.services.workflow_query_service import (
    WorkflowQueryService,
    normalize_terminal_status,
)
from api.services.workflow_runner import WorkflowRunner
from api.services.git_service import GitPostApprovalRecovery
from api.services.workflow_registry import (
    WorkflowExecutionStatus,
    WorkflowRegistry,
)
from graph.persistence_service import WorkflowNotFoundError
from graph.persistence_service import WorkflowSnapshot
from graph.runtime import WorkflowInterrupt, WorkflowRunResult
from streaming import EventStatus, WorkflowEvent, WorkflowEventType
from streaming import InMemoryWorkflowEventEmitter, WorkflowEventFactory


def event(
    sequence: int,
    event_type: WorkflowEventType = WorkflowEventType.STAGE_STARTED,
    *,
    thread_id: str = "api-thread",
    data: dict | None = None,
) -> WorkflowEvent:
    return WorkflowEvent(
        event_id=uuid4(),
        thread_id=thread_id,
        sequence=sequence,
        type=event_type,
        timestamp=datetime.now(UTC),
        source="test",
        stage="test",
        status=EventStatus.COMPLETED,
        message=None,
        data=data or {},
    )


class FakeRunner:
    def __init__(self) -> None:
        self.starts: list[tuple[str, str]] = []

    async def schedule_start(self, *, thread_id: str, request: str) -> None:
        self.starts.append((thread_id, request))


class FakeQuery:
    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing
        self.calls = 0

    async def get_snapshot(self, thread_id: str) -> WorkflowSnapshotResponse:
        self.calls += 1
        if self.missing:
            raise WorkflowNotFoundError(thread_id)
        return WorkflowSnapshotResponse(
            thread_id=thread_id,
            checkpoint_id="checkpoint-1",
            project_name="api-project",
            workflow_intent="create_project",
            terminal_status="pending",
            interrupted=True,
            pending_operation="create_project",
            pending_tool="filesystem__create_project_structure",
            planning={"valid": True},
            implementation={},
            testing={},
            supervisor={},
            created_at=None,
            updated_at=None,
        )


class PlanningEvaluationQuery(FakeQuery):
    async def get_snapshot(self, thread_id: str) -> WorkflowSnapshotResponse:
        response = await super().get_snapshot(thread_id)
        response.planning["evaluation"] = {
            "version": "7.7-v1",
            "outcome": "successful",
            "outcome_score": 100,
        }
        return response


class FakeApprovals:
    def __init__(self, *, conflict: bool = False) -> None:
        self.conflict = conflict
        self.calls: list[tuple[str, bool, str | None]] = []

    async def resolve(self, *, thread_id: str, approved: bool, reason: str | None):
        if self.conflict:
            raise ApprovalConflictError("Workflow is not waiting for approval.")
        self.calls.append((thread_id, approved, reason))
        return ApprovalResponse(
            thread_id=thread_id,
            accepted=True,
            operation="create_project",
            tool_name="filesystem__create_project_structure",
            status="running",
        )


class GitMergeApprovals(FakeApprovals):
    async def resolve(self, *, thread_id: str, approved: bool, reason: str | None):
        self.calls.append((thread_id, approved, reason))
        return ApprovalResponse(
            thread_id=thread_id,
            accepted=True,
            operation="git_merge",
            tool_name="git__merge_workflow_branch",
            status="completed",
        )


class CompletedGitMergeQuery:
    persistence: "CompletedGitMergeQuery"

    def __init__(self) -> None:
        self.persistence = self

    async def get_snapshot(self, thread_id: str) -> WorkflowSnapshot:
        return WorkflowSnapshot(
            thread_id=thread_id,
            checkpoint_id="checkpoint-git-merge",
            next_nodes=("git_promotion_approval",),
            values={
                "terminal_status": "completed",
                "pending_operation": "git_merge",
                "pending_tool_name": "git__merge_workflow_branch",
            },
            metadata={},
            created_at=None,
            interrupts=(WorkflowInterrupt("interrupt-1", {"operation": "git_merge"}),),
        )


class PendingPromotionGit:
    def __init__(self) -> None:
        self.approved = False
        self.merged = False

    async def get_git_promotion(self, thread_id: str):
        return SimpleNamespace(
            state="awaiting_approval",
            approval_id="approval-1",
        )

    async def approve_git_promotion(self, thread_id: str, *, actor: str = "user") -> str:
        self.approved = True
        return "approval-1"

    async def merge_git_workflow_branch(
        self,
        project_id: str,
        *,
        workflow_id: str,
        approval_id: str,
        actor: str = "user",
    ):
        self.merged = True
        return SimpleNamespace(result_commit="393800e2a01bda7eb3b1fcb6416f5dbbe84cc7bc")


class FakeEventStore:
    def __init__(self, events: list[WorkflowEvent] | None = None) -> None:
        self.events = list(events or [])
        self.calls: list[dict] = []
        self.closed = False

    async def get_events(
        self,
        thread_id: str,
        *,
        branch_id: str = "original",
        after_sequence: int | None = None,
        limit: int | None = None,
        event_types=None,
    ) -> list[WorkflowEvent]:
        self.calls.append(
            {
                "thread_id": thread_id,
                "branch_id": branch_id,
                "after_sequence": after_sequence,
                "limit": limit,
                "event_types": event_types,
            }
        )
        selected = [
            item
            for item in self.events
            if item.thread_id == thread_id
            and item.data.get("branch_id", "original") == branch_id
            and item.sequence > (after_sequence or 0)
            and (not event_types or item.type.value in event_types)
        ]
        return selected[:limit] if limit is not None else selected

    async def get_last_sequence(
        self,
        thread_id: str,
        branch_id: str = "original",
    ) -> int:
        sequences = [
            item.sequence
            for item in self.events
            if item.thread_id == thread_id
            and item.data.get("branch_id", "original") == branch_id
        ]
        return max(sequences, default=0)

    async def close(self) -> None:
        self.closed = True


def snapshot_response(thread_id: str = "api-thread") -> WorkflowSnapshotResponse:
    return WorkflowSnapshotResponse(
        thread_id=thread_id,
        checkpoint_id="checkpoint-1",
        project_name="api-project",
        workflow_intent="create_project",
        terminal_status="pending",
        interrupted=True,
        pending_operation="create_project",
        pending_tool="filesystem__create_project_structure",
        planning={},
        implementation={},
        testing={},
        supervisor={},
        created_at=None,
        updated_at=None,
    )


def make_services(
    *,
    broker: WorkflowEventBroker | None = None,
    query: FakeQuery | None = None,
    approvals: FakeApprovals | None = None,
    event_store: FakeEventStore | None = None,
):
    from api.services.sse_service import WorkflowSseService

    selected_broker = broker or WorkflowEventBroker()
    return SimpleNamespace(
        registry=WorkflowRegistry(),
        broker=selected_broker,
        runner=FakeRunner(),
        query=query or FakeQuery(),
        approvals=approvals or FakeApprovals(),
        sse=WorkflowSseService(selected_broker),
        event_store=event_store or FakeEventStore(),
    )


def app_with_services(services, *, closed: list[bool] | None = None):
    @asynccontextmanager
    async def factory():
        try:
            yield services
        finally:
            if closed is not None:
                closed.append(True)

    return create_app(factory)


def test_create_request_accepts_non_empty_value() -> None:
    assert CreateWorkflowRequest(request="build it").request == "build it"


def test_health_endpoint_reports_backend_readiness() -> None:
    with TestClient(app_with_services(make_services())) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("value", ["", " " * 20_001])
def test_create_request_rejects_invalid_length(value: str) -> None:
    with pytest.raises(ValidationError):
        CreateWorkflowRequest(request=value)


def test_http_models_reject_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CreateWorkflowRequest(request="ok", extra=True)


def test_create_returns_202_and_schedules_runner() -> None:
    services = make_services()
    with TestClient(app_with_services(services)) as client:
        response = client.post("/api/workflows", json={"request": "create api"})
    assert response.status_code == 202
    payload = response.json()
    assert payload["thread_id"]
    assert payload["events_url"].endswith("/events")
    assert services.runner.starts == [(payload["thread_id"], "create api")]


def test_create_returns_uuid_thread_id() -> None:
    services = make_services()
    with TestClient(app_with_services(services)) as client:
        response = client.post("/api/workflows", json={"request": "create api"})
    assert str(uuid4().__class__(response.json()["thread_id"])) == response.json()["thread_id"]


def test_empty_create_body_returns_422() -> None:
    with TestClient(app_with_services(make_services())) as client:
        response = client.post("/api/workflows", json={"request": ""})
    assert response.status_code == 422


def test_snapshot_found_is_safe_projection() -> None:
    services = make_services()
    with TestClient(app_with_services(services)) as client:
        response = client.get("/api/workflows/api-thread")
    assert response.status_code == 200
    assert response.json()["project_name"] == "api-project"
    assert response.json()["terminal_status"] == "pending"
    assert "original_user_message" not in response.json()


def test_snapshot_exposes_planner_evaluation() -> None:
    services = make_services(query=PlanningEvaluationQuery())
    with TestClient(app_with_services(services)) as client:
        response = client.get("/api/workflows/api-thread")

    assert response.status_code == 200
    assert response.json()["planning"]["evaluation"]["version"] == "7.7-v1"
    assert response.json()["planning"]["evaluation"]["outcome"] == "successful"


@pytest.mark.parametrize(
    ("raw_status", "interrupted", "next_nodes", "active", "expected"),
    [
        ("completed", False, (), False, "completed"),
        (None, True, ("approval",), False, "pending"),
        (None, False, ("inspect_workspace",), False, "pending"),
        (None, False, (), True, "running"),
        (None, False, (), False, "pending"),
        ("supervisor_loop_detected", False, (), False, "supervisor_loop_detected"),
    ],
)
def test_terminal_status_is_normalized_for_http_projection(
    raw_status: str | None,
    interrupted: bool,
    next_nodes: tuple[str, ...],
    active: bool,
    expected: str,
) -> None:
    assert normalize_terminal_status(
        raw_status,
        interrupted=interrupted,
        next_nodes=next_nodes,
        active=active,
    ) == expected


def test_snapshot_missing_returns_404() -> None:
    services = make_services(query=FakeQuery(missing=True))
    with TestClient(app_with_services(services)) as client:
        response = client.get("/api/workflows/missing")
    assert response.status_code == 404
    assert response.json()["detail"] == "Thread not found"


@pytest.mark.asyncio
async def test_broker_history_filters_and_orders_sequences() -> None:
    broker = WorkflowEventBroker()
    await broker.publish(event(2))
    await broker.publish(event(1))
    history = await broker.get_history("api-thread", after_sequence=1)
    assert [item.sequence for item in history] == [2]


@pytest.mark.asyncio
async def test_broker_deduplicates_sequence_per_thread() -> None:
    broker = WorkflowEventBroker()
    await broker.publish(event(1))
    await broker.publish(event(1, WorkflowEventType.APPROVAL_REQUIRED))
    history = await broker.get_history("api-thread")
    assert [item.sequence for item in history] == [1]


@pytest.mark.asyncio
async def test_broker_delivers_new_event_to_two_subscribers() -> None:
    broker = WorkflowEventBroker()
    first = broker.subscribe("api-thread")
    second = broker.subscribe("api-thread")
    first_next = asyncio.create_task(anext(first))
    second_next = asyncio.create_task(anext(second))
    await asyncio.sleep(0)
    await broker.publish(event(1))
    assert (await first_next).sequence == 1
    assert (await second_next).sequence == 1
    await first.aclose()
    await second.aclose()
    assert await broker.subscriber_count("api-thread") == 0


@pytest.mark.asyncio
async def test_broker_delivers_event_published_after_subscription() -> None:
    broker = WorkflowEventBroker()
    stream = broker.subscribe("api-thread", after_sequence=7)
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await broker.publish(event(8))
    assert (await waiting).sequence == 8
    await stream.aclose()


@pytest.mark.asyncio
async def test_broker_terminal_closes_stream() -> None:
    broker = WorkflowEventBroker()
    await broker.publish(event(1, WorkflowEventType.WORKFLOW_COMPLETED))
    received = [item async for item in broker.subscribe("api-thread")]
    assert [item.type for item in received] == [WorkflowEventType.WORKFLOW_COMPLETED]


@pytest.mark.asyncio
async def test_approval_event_does_not_close_stream() -> None:
    broker = WorkflowEventBroker()
    stream = broker.subscribe("api-thread")
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await broker.publish(event(1, WorkflowEventType.APPROVAL_REQUIRED))
    assert (await pending).type == WorkflowEventType.APPROVAL_REQUIRED
    next_item = asyncio.create_task(anext(stream))
    await broker.publish(event(2, WorkflowEventType.WORKFLOW_COMPLETED))
    assert (await next_item).type == WorkflowEventType.WORKFLOW_COMPLETED


@pytest.mark.asyncio
async def test_broker_full_queue_preserves_critical_events() -> None:
    broker = WorkflowEventBroker(subscriber_queue_size=1)
    stream = broker.subscribe("api-thread")
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await broker.publish(event(1))
    await broker.publish(event(2, WorkflowEventType.APPROVAL_REQUIRED))
    assert (await waiting).type == WorkflowEventType.APPROVAL_REQUIRED
    await stream.aclose()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 0), ("", 0), ("0", 0), ("25", 25)],
)
def test_last_event_id_parsing(value: str | None, expected: int) -> None:
    assert parse_last_event_id(value) == expected


@pytest.mark.parametrize("value", ["abc", "-1", "1.5"])
def test_invalid_last_event_id_returns_400(value: str) -> None:
    with pytest.raises(Exception) as raised:
        parse_last_event_id(value)
    assert getattr(raised.value, "status_code", None) == 400


def test_sse_returns_history_and_filters_last_event_id() -> None:
    broker = WorkflowEventBroker()

    async def prepare() -> None:
        await broker.publish(event(1, WorkflowEventType.STAGE_STARTED))
        await broker.publish(event(2, WorkflowEventType.WORKFLOW_COMPLETED))

    asyncio.run(prepare())
    services = make_services(broker=broker)
    with TestClient(app_with_services(services)) as client:
        response = client.get(
            "/api/workflows/api-thread/events",
            headers={"Last-Event-ID": "1"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "id: 2" in response.text
    assert "id: 1" not in response.text
    assert "workflow_completed" in response.text


def test_sse_invalid_last_event_id_returns_400() -> None:
    services = make_services()
    with TestClient(app_with_services(services)) as client:
        response = client.get(
            "/api/workflows/api-thread/events",
            headers={"Last-Event-ID": "bad"},
        )
    assert response.status_code == 400


def test_sse_last_event_id_reads_durable_history_after_restart(tmp_path) -> None:
    from streaming.sqlite_store import SQLiteWorkflowEventStore

    selected_store = SQLiteWorkflowEventStore(tmp_path / "api-restart.sqlite")

    async def prepare() -> None:
        await selected_store.initialize()
        await selected_store.append(event(20))
        await selected_store.append(
            event(21, WorkflowEventType.WORKFLOW_COMPLETED)
        )

    asyncio.run(prepare())
    broker = WorkflowEventBroker(store=selected_store)
    services = make_services(
        broker=broker,
        event_store=selected_store,  # type: ignore[arg-type]
    )
    with TestClient(app_with_services(services)) as client:
        response = client.get(
            "/api/workflows/api-thread/events",
            headers={"Last-Event-ID": "20"},
        )
    assert response.status_code == 200
    assert "id: 21" in response.text
    assert "id: 20" not in response.text
    asyncio.run(selected_store.close())


def test_sse_after_sequence_filters_history() -> None:
    broker = WorkflowEventBroker()

    async def prepare() -> None:
        await broker.publish(event(1))
        await broker.publish(event(2, WorkflowEventType.WORKFLOW_COMPLETED))

    asyncio.run(prepare())
    with TestClient(app_with_services(make_services(broker=broker))) as client:
        response = client.get("/api/workflows/api-thread/events?after_sequence=1")
    assert "id: 2" in response.text
    assert "id: 1" not in response.text


def test_history_endpoint_returns_typed_page() -> None:
    selected_store = FakeEventStore([event(1), event(2), event(3)])
    services = make_services(event_store=selected_store)
    with TestClient(app_with_services(services)) as client:
        response = client.get(
            "/api/workflows/api-thread/events/history?after_sequence=1&limit=1"
        )
    assert response.status_code == 200
    assert response.json()["thread_id"] == "api-thread"
    assert [item["sequence"] for item in response.json()["events"]] == [2]
    assert response.json()["last_sequence"] == 3
    assert response.json()["has_more"] is True


def test_history_endpoint_filters_branch_and_event_type() -> None:
    fork_event = event(
        1,
        WorkflowEventType.TOOL_STARTED,
        data={"branch_id": "fork-1", "lineage": "fork"},
    )
    selected_store = FakeEventStore([event(1), fork_event])
    services = make_services(event_store=selected_store)
    with TestClient(app_with_services(services)) as client:
        response = client.get(
            "/api/workflows/api-thread/events/history"
            "?branch_id=fork-1&event_type=tool_started"
        )
    assert response.status_code == 200
    assert response.json()["branch_id"] == "fork-1"
    assert [item["type"] for item in response.json()["events"]] == [
        "tool_started"
    ]


def test_history_endpoint_returns_empty_for_existing_workflow() -> None:
    services = make_services(event_store=FakeEventStore())
    with TestClient(app_with_services(services)) as client:
        response = client.get("/api/workflows/api-thread/events/history")
    assert response.status_code == 200
    assert response.json()["events"] == []
    assert response.json()["last_sequence"] == 0


def test_history_endpoint_returns_404_for_unknown_workflow() -> None:
    services = make_services(
        query=FakeQuery(missing=True),
        event_store=FakeEventStore(),
    )
    with TestClient(app_with_services(services)) as client:
        response = client.get("/api/workflows/missing/events/history")
    assert response.status_code == 404


def test_sse_unknown_thread_returns_404() -> None:
    services = make_services(query=FakeQuery(missing=True))
    with TestClient(app_with_services(services)) as client:
        response = client.get("/api/workflows/missing/events")
    assert response.status_code == 404


def test_heartbeat_configuration_does_not_publish_workflow_event() -> None:
    broker = WorkflowEventBroker()
    services = make_services(broker=broker)
    with TestClient(app_with_services(services)) as client:
        response = services.sse.response(
            client,
            "api-thread",
            last_event_id=None,
            after_sequence=None,
        )
    assert response.ping_interval == 15
    assert asyncio.run(broker.get_history("api-thread")) == []


@pytest.mark.parametrize("path,approved", [("approve", True), ("reject", False)])
def test_approval_endpoints_return_202(path: str, approved: bool) -> None:
    services = make_services()
    with TestClient(app_with_services(services)) as client:
        response = client.post(
            f"/api/workflows/api-thread/{path}",
            json={"reason": "because"},
        )
    assert response.status_code == 202
    assert services.approvals.calls == [("api-thread", approved, "because")]


def test_retry_pending_git_commit_uses_durable_approval_and_continues_without_reapproval() -> None:
    thread_id = "git-recovery-thread"

    class Persistence:
        def __init__(self) -> None:
            self.continuations: list[tuple[str, dict, str]] = []

        async def get_snapshot(self, requested_thread: str) -> WorkflowSnapshot:
            assert requested_thread == thread_id
            return WorkflowSnapshot(
                thread_id=thread_id,
                checkpoint_id="checkpoint-after-approval",
                next_nodes=("execute_git_commit",),
                values={
                    "workflow_id": thread_id,
                    "project_name": "api-project",
                    "pending_operation": "git_commit",
                    "pending_tool_name": "git__commit",
                    "pending_approval_status": "approved",
                    "git_commit_phase": "implementation",
                    "git_commit_history": [],
                },
                metadata={},
                created_at=None,
                interrupts=(),
            )

        async def continue_after_recovered_git_operation(
            self, requested_thread: str, updates: dict, *, as_node: str,
        ) -> WorkflowRunResult:
            self.continuations.append((requested_thread, updates, as_node))
            return WorkflowRunResult(
                thread_id=thread_id,
                final_state={**updates, "terminal_status": "completed", "ci_pipeline_state": "passed"},
            )

    class Git:
        def __init__(self) -> None:
            self.recoveries = 0

        async def detect_post_approval_recovery(self, requested_thread, pending_operation):
            assert requested_thread == thread_id and pending_operation == "git_commit"
            return GitPostApprovalRecovery(
                "git_commit", "recoverable", "durable-approval", "api-project", None
            )

        async def recover_post_approval_operation(
            self, requested_thread, pending_operation, *, phase,
        ):
            assert requested_thread == thread_id
            assert pending_operation == "git_commit"
            assert phase == "implementation"
            self.recoveries += 1
            return (
                GitPostApprovalRecovery(
                    "git_commit", "recoverable", "durable-approval", "api-project", None
                ),
                GitCommitResult(
                    commit="a" * 40,
                    short_commit="aaaaaaa",
                    branch="workflow/git-reco",
                    message="fix: recover commit",
                    files=["README.md"],
                    created_at=datetime.now(UTC),
                ),
            )

    persistence = Persistence()
    git = Git()
    services = make_services(broker=WorkflowEventBroker())
    services.persistence = persistence
    services.git = git

    with TestClient(app_with_services(services)) as client:
        response = client.post(
            f"/api/workflows/{thread_id}/retry-pending-operation"
        )

    assert response.status_code == 202
    assert response.json()["result_commit"] == "a" * 40
    assert git.recoveries == 1
    assert persistence.continuations[0][2] == "execute_git_commit"
    updates = persistence.continuations[0][1]
    assert updates["pending_operation"] is None
    assert updates["pending_tool_name"] is None
    assert updates["git_commit_status"] == "committed"
    events = asyncio.run(services.broker.get_history(thread_id))
    event_types = [item.type for item in events]
    assert WorkflowEventType.WORKFLOW_RESUMED in event_types
    assert WorkflowEventType.TOOL_STARTED in event_types
    assert WorkflowEventType.TOOL_COMPLETED in event_types
    assert WorkflowEventType.APPROVAL_GRANTED not in event_types


def test_retry_pending_git_operation_rejects_unapproved_state() -> None:
    class Persistence:
        async def get_snapshot(self, thread_id: str) -> WorkflowSnapshot:
            return WorkflowSnapshot(
                thread_id=thread_id,
                checkpoint_id="checkpoint-awaiting",
                next_nodes=("git_approval",),
                values={"pending_operation": "git_commit"},
                metadata={},
                created_at=None,
            )

    class Git:
        async def detect_post_approval_recovery(self, thread_id, pending_operation):
            return GitPostApprovalRecovery("git_commit", "not_recoverable")

    services = make_services()
    services.persistence = Persistence()
    services.git = Git()
    with TestClient(app_with_services(services)) as client:
        response = client.post(
            "/api/workflows/unapproved/retry-pending-operation"
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "git_post_approval_recovery_not_available"


def test_approve_accepts_completed_workflow_with_pending_git_merge_interrupt() -> None:
    broker = WorkflowEventBroker()
    git = PendingPromotionGit()
    services = make_services(broker=broker)
    services.git = git
    with TestClient(app_with_services(services)) as client:
        response = client.post(
            "/api/workflows/api-thread/approve",
            json={"reason": "Promote"},
        )

    assert response.status_code == 202
    assert response.json()["operation"] == "git_merge"
    assert response.json()["tool_name"] == "git__merge_workflow_branch"
    assert git.approved is True
    assert git.merged is True
    events = asyncio.run(broker.get_history("api-thread"))
    event_types = [event.type for event in events]
    tools = [event.data.get("tool") for event in events]
    assert WorkflowEventType.WORKFLOW_RESUMED in event_types
    assert WorkflowEventType.APPROVAL_GRANTED in event_types
    assert "approve_promotion" in tools
    assert "merge_workflow_branch" in tools


def test_approval_conflict_returns_409() -> None:
    services = make_services(approvals=FakeApprovals(conflict=True))
    with TestClient(app_with_services(services)) as client:
        response = client.post("/api/workflows/api-thread/approve", json={})
    assert response.status_code == 409


def test_approval_body_rejects_extra_fields() -> None:
    with TestClient(app_with_services(make_services())) as client:
        response = client.post(
            "/api/workflows/api-thread/approve",
            json={"approved": True},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_registry_is_concurrency_safe_and_cancels_tasks() -> None:
    registry = WorkflowRegistry()
    await asyncio.gather(*(registry.register(f"thread-{index}") for index in range(20)))
    executions = await asyncio.gather(
        *(registry.get(f"thread-{index}") for index in range(20))
    )
    assert all(executions)
    blocker = asyncio.create_task(asyncio.sleep(60))
    await registry.attach_task("thread-0", blocker)
    assert await registry.is_running("thread-0") is True
    await registry.cancel_all()
    assert blocker.cancelled()


@pytest.mark.asyncio
async def test_registry_records_completed_and_failed_statuses() -> None:
    registry = WorkflowRegistry()
    await registry.register("completed")
    await registry.mark_completed("completed")
    await registry.register("failed")
    await registry.mark_failed("failed", "safe error")
    assert (await registry.get("completed")).status == WorkflowExecutionStatus.COMPLETED
    failed = await registry.get("failed")
    assert failed.status == WorkflowExecutionStatus.FAILED
    assert failed.error == "safe error"


@pytest.mark.asyncio
async def test_registry_created_at_is_defined_and_idempotent() -> None:
    registry = WorkflowRegistry()
    first = await registry.register("stable-thread")
    second = await registry.register("stable-thread")
    assert first.started_at is not None
    assert second is first
    assert second.started_at == first.started_at


class TimestampSnapshotPersistence:
    def __init__(
        self,
        *,
        created_at: str = "2026-07-28T12:30:00+00:00",
        terminal_status: str = "running",
    ) -> None:
        self.created_at = created_at
        self.terminal_status = terminal_status

    async def get_snapshot(self, thread_id: str) -> WorkflowSnapshot:
        return WorkflowSnapshot(
            thread_id=thread_id,
            checkpoint_id="checkpoint-time",
            next_nodes=(),
            values={"terminal_status": self.terminal_status},
            metadata={},
            created_at=self.created_at,
        )


def timestamped_started_event(
    timestamp: datetime,
    *,
    thread_id: str = "stable-thread",
) -> WorkflowEvent:
    return event(
        1,
        WorkflowEventType.WORKFLOW_STARTED,
        thread_id=thread_id,
    ).model_copy(update={"timestamp": timestamp})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transition",
    ["approve", "prepare_environment", "run_tests", "complete"],
)
async def test_created_at_does_not_change_across_workflow_transitions(
    transition: str,
) -> None:
    original = datetime(2026, 7, 27, 4, 9, 51, tzinfo=UTC)
    persistence = TimestampSnapshotPersistence()
    store = FakeEventStore([timestamped_started_event(original)])
    query = WorkflowQueryService(
        persistence,  # type: ignore[arg-type]
        WorkflowRegistry(),
        store,  # type: ignore[arg-type]
    )
    before = await query.get_snapshot("stable-thread")
    persistence.created_at = f"2026-07-28T1{len(transition) % 10}:45:00+00:00"
    persistence.terminal_status = (
        "completed" if transition == "complete" else "running"
    )
    after = await query.get_snapshot("stable-thread")
    assert before.created_at == original
    assert after.created_at == original


@pytest.mark.asyncio
async def test_created_at_survives_registry_restart_and_repeated_gets() -> None:
    original = datetime(2026, 7, 27, 4, 9, 51, tzinfo=UTC)
    store = FakeEventStore([timestamped_started_event(original)])
    persistence = TimestampSnapshotPersistence()
    first_query = WorkflowQueryService(
        persistence,  # type: ignore[arg-type]
        WorkflowRegistry(),
        store,  # type: ignore[arg-type]
    )
    first = await first_query.get_snapshot("stable-thread")
    restarted_query = WorkflowQueryService(
        persistence,  # type: ignore[arg-type]
        WorkflowRegistry(),
        store,  # type: ignore[arg-type]
    )
    second = await restarted_query.get_snapshot("stable-thread")
    third = await restarted_query.get_snapshot("stable-thread")
    assert first.created_at == second.created_at == third.created_at == original


@pytest.mark.asyncio
async def test_old_thread_created_at_is_migrated_from_first_durable_event() -> None:
    original = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    store = FakeEventStore(
        [
            timestamped_started_event(original, thread_id="old-thread"),
            event(2, thread_id="old-thread"),
        ]
    )
    projected = await WorkflowQueryService(
        TimestampSnapshotPersistence(),  # type: ignore[arg-type]
        WorkflowRegistry(),
        store,  # type: ignore[arg-type]
    ).get_snapshot("old-thread")
    assert projected.created_at == original
    assert store.events[0].timestamp == original


@pytest.mark.asyncio
async def test_updated_at_changes_without_mutating_created_at() -> None:
    original = datetime(2026, 7, 27, 4, 9, 51, tzinfo=UTC)
    persistence = TimestampSnapshotPersistence(
        created_at="2026-07-28T12:30:00+00:00",
    )
    query = WorkflowQueryService(
        persistence,  # type: ignore[arg-type]
        WorkflowRegistry(),
        FakeEventStore([timestamped_started_event(original)]),  # type: ignore[arg-type]
    )
    first = await query.get_snapshot("stable-thread")
    persistence.created_at = "2026-07-28T12:31:00+00:00"
    second = await query.get_snapshot("stable-thread")
    assert first.created_at == second.created_at == original
    assert first.updated_at != second.updated_at


@pytest.mark.asyncio
async def test_terminal_created_at_survives_api_registry_restart() -> None:
    original = datetime(2026, 7, 27, 4, 9, 51, tzinfo=UTC)
    persistence = TimestampSnapshotPersistence(terminal_status="completed")
    store = FakeEventStore([timestamped_started_event(original)])
    for _ in range(2):
        projected = await WorkflowQueryService(
            persistence,  # type: ignore[arg-type]
            WorkflowRegistry(),
            store,  # type: ignore[arg-type]
        ).get_snapshot("stable-thread")
        assert projected.terminal_status == "completed"
        assert projected.created_at == original


@pytest.mark.asyncio
async def test_legacy_snapshot_timestamp_remains_the_last_created_at_fallback() -> None:
    projected = await WorkflowQueryService(
        TimestampSnapshotPersistence(
            created_at="2024-03-02T01:00:00+00:00",
        ),  # type: ignore[arg-type]
        WorkflowRegistry(),
        FakeEventStore(),  # type: ignore[arg-type]
    ).get_snapshot("legacy-thread")
    assert projected.created_at == datetime(2024, 3, 2, 1, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_snapshot_projection_excludes_generated_files_and_source_content() -> None:
    class SnapshotPersistence:
        calls = 0
        values = {
            "created_project_name": "safe-api",
            "implementation_result": {
                "valid": True,
                "generated_files": [
                    {"path": "main.py", "content": "PRIVATE SOURCE"}
                ],
            },
            "generated_files": ["main.py"],
        }

        async def get_snapshot(self, thread_id: str) -> WorkflowSnapshot:
            self.calls += 1
            return WorkflowSnapshot(
                thread_id=thread_id,
                checkpoint_id="checkpoint-safe",
                next_nodes=(),
                values=self.values,
                metadata={},
                created_at=None,
            )

    persistence = SnapshotPersistence()
    query = WorkflowQueryService(persistence, WorkflowRegistry())  # type: ignore[arg-type]
    projected = await query.get_snapshot("safe-thread")
    serialized = projected.model_dump_json()
    assert persistence.calls == 1
    assert projected.terminal_status == "pending"
    assert projected.terminal_status is not None
    assert "terminal_status" not in persistence.values
    assert "generated_files" not in serialized
    assert "PRIVATE SOURCE" not in serialized


@pytest.mark.asyncio
async def test_active_snapshot_projects_running_without_mutating_state() -> None:
    class SnapshotPersistence:
        values: dict = {}

        async def get_snapshot(self, thread_id: str) -> WorkflowSnapshot:
            return WorkflowSnapshot(
                thread_id=thread_id,
                checkpoint_id="checkpoint-running",
                next_nodes=(),
                values=self.values,
                metadata={},
                created_at=None,
            )

    persistence = SnapshotPersistence()
    registry = WorkflowRegistry()
    await registry.register("running-thread")
    task = asyncio.create_task(asyncio.sleep(60))
    await registry.attach_task("running-thread", task)
    try:
        projected = await WorkflowQueryService(  # type: ignore[arg-type]
            persistence,
            registry,
        ).get_snapshot("running-thread")
        assert projected.terminal_status == "running"
        assert projected.interrupted is False
        assert persistence.values == {}
    finally:
        await registry.cancel_all()


def test_unexpected_http_failure_is_sanitized() -> None:
    class FailingRunner:
        async def schedule_start(self, *, thread_id: str, request: str) -> None:
            raise RuntimeError("SECRET INTERNAL DETAIL")

    services = make_services()
    services.runner = FailingRunner()
    with TestClient(
        app_with_services(services),
        raise_server_exceptions=False,
    ) as client:
        response = client.post("/api/workflows", json={"request": "create api"})
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "SECRET INTERNAL DETAIL" not in response.text


@pytest.mark.asyncio
async def test_runner_emits_workflow_failed_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_graph(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "api.services.workflow_runner.run_software_factory_graph",
        fail_graph,
    )
    emitter = InMemoryWorkflowEventEmitter()
    broker = WorkflowEventBroker()
    from api.services.event_broker import WorkflowEventForwarder

    forwarder = WorkflowEventForwarder(emitter, broker)
    await forwarder.start()
    registry = WorkflowRegistry()
    runner = WorkflowRunner(
        graph=object(),
        persistence=object(),  # type: ignore[arg-type]
        registry=registry,
        emitter=emitter,
        event_factory=WorkflowEventFactory(),
        forwarder=forwarder,
    )
    await runner.schedule_start(thread_id="failed-thread", request="fail")
    execution = await registry.get("failed-thread")
    await execution.task
    await forwarder.flush()
    failures = [
        item
        for item in await broker.get_history("failed-thread")
        if item.type == WorkflowEventType.WORKFLOW_FAILED
    ]
    assert len(failures) == 1
    assert (await registry.get("failed-thread")).status == WorkflowExecutionStatus.FAILED
    await forwarder.close()


def test_cors_uses_configured_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_CORS_ORIGINS", "http://localhost:5173")
    with TestClient(app_with_services(make_services())) as client:
        response = client.options(
            "/api/workflows",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:5173", "http://127.0.0.1:5173"],
)
def test_cors_default_allows_vite_development_origins(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    monkeypatch.delenv("API_CORS_ORIGINS", raising=False)
    with TestClient(app_with_services(make_services())) as client:
        response = client.options(
            "/api/workflows",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


def test_cors_parser_trims_deduplicates_and_ignores_empty_entries() -> None:
    assert parse_cors_origins(
        " http://localhost:5173, ,http://127.0.0.1:5173,"
        "http://localhost:5173/ "
    ) == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_cors_parser_keeps_only_valid_origins_and_rejects_wildcard() -> None:
    assert parse_cors_origins(
        "*,ftp://localhost:5173,http://user@localhost:5173,"
        "http://localhost:5173/path,https://example.com"
    ) == ["https://example.com"]


def test_cors_rejects_origin_not_in_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "API_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    with TestClient(app_with_services(make_services())) as client:
        response = client.options(
            "/api/workflows",
            headers={
                "Origin": "http://malicious.example",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_lifespan_runs_cleanup() -> None:
    closed: list[bool] = []
    with TestClient(app_with_services(make_services(), closed=closed)):
        pass
    assert closed == [True]
