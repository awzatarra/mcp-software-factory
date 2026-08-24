from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api.services.observability_context import observability_context
from api.services.observability_hierarchy import validate_span_hierarchy
from api.services.observability_service import ObservabilityService
from api.services.observability_store import ObservabilityStore
from graph.observability import observed_node, observed_subgraph
from scripts.reconcile_observability_spans import reconcile
from streaming import EventStatus, WorkflowEvent, WorkflowEventType
from streaming.sqlite_store import SQLiteWorkflowEventStore


def event(
    event_type: WorkflowEventType,
    sequence: int,
    *,
    thread_id: str = "hierarchy-workflow",
    stage: str | None = None,
    status: EventStatus = EventStatus.COMPLETED,
    data: dict | None = None,
    timestamp: datetime | None = None,
) -> WorkflowEvent:
    return WorkflowEvent(
        event_id=uuid4(),
        thread_id=thread_id,
        sequence=sequence,
        type=event_type,
        timestamp=timestamp or datetime.now(UTC),
        source="hierarchy-test",
        stage=stage,
        status=status,
        message="test event",
        data=data or {},
    )


@pytest.fixture
async def hierarchy_service(tmp_path):
    store = ObservabilityStore(tmp_path / "hierarchy.sqlite")
    service = ObservabilityService(store)
    await store.initialize()
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "node", "agent"), [
    ("planning", "analyze_requirement", "Planner"),
    ("implementation", "prepare_create_project", "Developer"),
    ("testing_repair", "execute_tests", "QA"),
    ("testing_repair", "prepare_fix", "Repair"),
])
async def test_runtime_hierarchy_for_each_agent(hierarchy_service, stage, node, agent):
    context = await hierarchy_service.ensure_trace(f"workflow-{agent}")
    dependencies = SimpleNamespace(observability=hierarchy_service)
    with observability_context(context):
        async with observed_subgraph(dependencies, stage):
            async with observed_node(dependencies, stage, node, agent=agent):
                async with hierarchy_service.span("openai.responses.parse", category="llm"):
                    pass
    ended = datetime.now(UTC)
    await hierarchy_service.store.end_span(
        context.span_id, status="completed", ended_at=ended.isoformat(), duration_ms=1
    )
    await hierarchy_service.store.end_trace(
        context.trace_id, status="completed", ended_at=ended.isoformat(), duration_ms=1
    )
    detail = await hierarchy_service.store.trace_detail(context.trace_id)
    assert detail["hierarchy_validation"]["valid"] is True
    assert detail["hierarchy_validation"]["max_depth"] >= 4
    by_id = {span["span_id"]: span for span in detail["spans"]}
    llm = next(span for span in detail["spans"] if span["category"] == "llm")
    agent_span = by_id[llm["parent_span_id"]]
    node_span = by_id[agent_span["parent_span_id"]]
    subgraph_span = by_id[node_span["parent_span_id"]]
    assert (agent_span["category"], node_span["category"], subgraph_span["category"]) == (
        "agent", "node", "subgraph"
    )


@pytest.mark.asyncio
async def test_failed_agent_span_closes_with_sanitized_error(hierarchy_service):
    context = await hierarchy_service.ensure_trace("failed-agent")
    with pytest.raises(RuntimeError), observability_context(context):
        async with hierarchy_service.span("agent.Planner", category="agent"):
            raise RuntimeError("Bearer private-token")
    span = await hierarchy_service.store.fetch_one(
        "SELECT * FROM observability_spans WHERE name='agent.Planner'"
    )
    assert span["status"] == "failed"
    assert "private-token" not in span["error_message"]


@pytest.mark.asyncio
async def test_cancelled_span_closes_cancelled(hierarchy_service):
    context = await hierarchy_service.ensure_trace("cancelled-agent")

    async def cancel_inside_span():
        with observability_context(context):
            async with hierarchy_service.span("agent.QA", category="agent"):
                raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cancel_inside_span()
    span = await hierarchy_service.store.fetch_one(
        "SELECT * FROM observability_spans WHERE name='agent.QA'"
    )
    assert span["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(("terminal_event", "data", "expected"), [
    (WorkflowEventType.WORKFLOW_COMPLETED, {}, "completed"),
    (WorkflowEventType.WORKFLOW_FAILED, {}, "failed"),
    (WorkflowEventType.WORKFLOW_COMPLETED, {"terminal_status": "user_cancelled"}, "cancelled"),
])
async def test_terminal_event_immediately_reconciles_active_spans(
    hierarchy_service, terminal_event, data, expected
):
    started = datetime.now(UTC) - timedelta(seconds=1)
    context = await hierarchy_service.ensure_trace("terminal-invariant", started_at=started)
    await hierarchy_service.store.start_span({
        "span_id": uuid4().hex[:16],
        "trace_id": context.trace_id,
        "parent_span_id": context.span_id,
        "name": "test.leaked",
        "category": "test",
        "status": "running",
        "started_at": started.isoformat(),
        "attributes": {"evidence": "durable"},
    })
    await hierarchy_service.ingest_workflow_event(
        event(terminal_event, 1, status=EventStatus.FAILED if expected == "failed" else EventStatus.COMPLETED, data=data)
        .model_copy(update={"thread_id": "terminal-invariant"})
    )
    trace = await hierarchy_service.store.find_trace("terminal-invariant")
    active = await hierarchy_service.store.fetch_one(
        "SELECT COUNT(*) AS count FROM observability_spans WHERE trace_id=? AND status IN ('running','waiting')",
        (context.trace_id,),
    )
    reconciled = await hierarchy_service.store.fetch_one(
        "SELECT * FROM observability_spans WHERE name='test.leaked'"
    )
    assert trace["status"] == expected and active["count"] == 0
    assert reconciled["status"] == expected
    assert reconciled["ended_at"] and reconciled["duration_ms"] >= 0
    assert reconciled["attributes"]["evidence"] == "durable"
    assert reconciled["attributes"]["reconciliation_reason"] == "workflow_terminal_event"


@pytest.mark.asyncio
async def test_subgraph_start_and_end_use_same_span_name(hierarchy_service):
    now = datetime.now(UTC)
    await hierarchy_service.ingest_workflow_event(event(
        WorkflowEventType.PLANNING_STARTED, 1, stage="planning",
        status=EventStatus.RUNNING, timestamp=now,
    ))
    await hierarchy_service.ingest_workflow_event(event(
        WorkflowEventType.PLANNING_COMPLETED, 2, stage="planning",
        timestamp=now + timedelta(milliseconds=10),
    ))
    span = await hierarchy_service.store.fetch_one(
        "SELECT * FROM observability_spans WHERE name='subgraph.planning'"
    )
    assert span["status"] == "completed" and span["duration_ms"] == 10


@pytest.mark.asyncio
async def test_live_stage_events_do_not_duplicate_runtime_node_spans(hierarchy_service):
    await hierarchy_service.ingest_workflow_event(event(
        WorkflowEventType.STAGE_STARTED, 1, stage="detect_intent",
        status=EventStatus.RUNNING, data={"node": "detect_intent"},
    ))
    row = await hierarchy_service.store.fetch_one(
        "SELECT COUNT(*) AS count FROM observability_spans WHERE category='node'"
    )
    assert row["count"] == 0


@pytest.mark.asyncio
async def test_live_workflow_events_do_not_create_redundant_workflow_child(hierarchy_service):
    await hierarchy_service.ingest_workflow_event(event(
        WorkflowEventType.WORKFLOW_STARTED, 1, status=EventStatus.RUNNING
    ))
    spans = await hierarchy_service.store.fetch_all(
        "SELECT name,parent_span_id FROM observability_spans WHERE category='workflow'"
    )
    assert len(spans) == 1
    assert spans[0]["name"] == "workflow" and spans[0]["parent_span_id"] is None


@pytest.mark.asyncio
async def test_backfill_stage_event_keeps_partial_node_evidence(hierarchy_service):
    await hierarchy_service.ingest_workflow_event(
        event(
            WorkflowEventType.STAGE_STARTED, 1, stage="detect_intent",
            status=EventStatus.RUNNING, data={"node": "detect_intent"},
        ),
        source="backfill",
    )
    span = await hierarchy_service.store.fetch_one(
        "SELECT * FROM observability_spans WHERE category='node'"
    )
    assert span is not None and span["attributes"]["parent_fallback"] == "root"


@pytest.mark.asyncio
async def test_three_approvals_preserve_hierarchy_across_restart(tmp_path):
    database = Path(tmp_path / "approvals.sqlite")
    first = ObservabilityService(ObservabilityStore(database))
    await first.store.initialize()
    now = datetime.now(UTC)
    await first.ingest_workflow_event(event(
        WorkflowEventType.IMPLEMENTATION_STARTED, 1, stage="implementation",
        status=EventStatus.RUNNING, timestamp=now,
    ))
    for sequence, operation in enumerate(("create_project", "prepare_environment"), start=2):
        await first.ingest_workflow_event(event(
            WorkflowEventType.APPROVAL_REQUIRED, sequence * 2, stage=operation,
            status=EventStatus.WAITING, data={"operation": operation},
        ))
        await first.ingest_workflow_event(event(
            WorkflowEventType.APPROVAL_GRANTED, sequence * 2 + 1, stage=operation,
            data={"operation": operation},
        ))

    restarted = ObservabilityService(ObservabilityStore(database))
    await restarted.store.initialize()
    await restarted.ingest_workflow_event(event(
        WorkflowEventType.IMPLEMENTATION_COMPLETED, 10, stage="implementation"
    ))
    await restarted.ingest_workflow_event(event(
        WorkflowEventType.TESTING_STARTED, 11, stage="testing_repair", status=EventStatus.RUNNING
    ))
    await restarted.ingest_workflow_event(event(
        WorkflowEventType.APPROVAL_REQUIRED, 12, stage="run_tests",
        status=EventStatus.WAITING, data={"operation": "run_tests"},
    ))
    await restarted.ingest_workflow_event(event(
        WorkflowEventType.APPROVAL_GRANTED, 13, stage="run_tests", data={"operation": "run_tests"}
    ))
    await restarted.ingest_workflow_event(event(
        WorkflowEventType.TESTING_COMPLETED, 14, stage="testing_repair"
    ))
    await restarted.ingest_workflow_event(event(WorkflowEventType.WORKFLOW_COMPLETED, 15))
    trace = await restarted.store.find_trace("hierarchy-workflow")
    detail = await restarted.store.trace_detail(trace["trace_id"])
    approvals = [span for span in detail["spans"] if span["category"] == "approval"]
    by_id = {span["span_id"]: span for span in detail["spans"]}
    assert len(approvals) == 3
    assert all(by_id[span["parent_span_id"]]["category"] in {"node", "subgraph"} for span in approvals)
    assert all(span["attributes"]["context_restored"] is True for span in approvals)
    assert detail["hierarchy_validation"]["terminal_active_spans"] == 0


@pytest.mark.asyncio
async def test_fork_and_replay_keep_independent_hierarchy_roots(hierarchy_service):
    await hierarchy_service.ingest_workflow_event(event(
        WorkflowEventType.WORKFLOW_STARTED, 1, data={"branch_id": "original"}
    ))
    await hierarchy_service.ingest_workflow_event(event(
        WorkflowEventType.WORKFLOW_FORKED, 1,
        data={"branch_id": "fork-1", "origin_checkpoint": "cp-1"},
    ))
    await hierarchy_service.ingest_workflow_event(event(
        WorkflowEventType.WORKFLOW_RESUMED, 1,
        data={"branch_id": "replay-1", "lineage": "replay"},
    ))
    traces = await hierarchy_service.store.fetch_all(
        "SELECT * FROM observability_traces ORDER BY branch_id"
    )
    assert {trace["branch_id"] for trace in traces} == {"original", "fork-1", "replay-1"}
    assert len({trace["root_span_id"] for trace in traces}) == 3
    assert next(trace for trace in traces if trace["branch_id"] == "fork-1")["source"] == "fork"
    assert next(trace for trace in traces if trace["branch_id"] == "replay-1")["source"] == "replay"


@pytest.mark.asyncio
async def test_two_concurrent_workflows_do_not_cross_parent_spans(hierarchy_service):
    dependencies = SimpleNamespace(observability=hierarchy_service)

    async def execute(workflow_id: str):
        context = await hierarchy_service.ensure_trace(workflow_id)
        with observability_context(context):
            async with observed_subgraph(dependencies, "planning"):
                async with observed_node(
                    dependencies, "planning", "analyze_requirement", agent="Planner"
                ):
                    await asyncio.sleep(0)
        return context

    contexts = await asyncio.gather(execute("concurrent-a"), execute("concurrent-b"))
    for context in contexts:
        spans = await hierarchy_service.store.fetch_all(
            "SELECT * FROM observability_spans WHERE trace_id=?", (context.trace_id,)
        )
        assert {span["trace_id"] for span in spans} == {context.trace_id}
        by_id = {span["span_id"] for span in spans}
        assert all(
            span["parent_span_id"] is None or span["parent_span_id"] in by_id for span in spans
        )


def test_hierarchy_validation_detects_orphan_cycle_and_invalid_relationship():
    trace = {"status": "completed", "attributes": {"precision": "full"}}
    spans = [
        {"span_id": "root", "parent_span_id": None, "category": "workflow", "status": "completed"},
        {"span_id": "orphan", "parent_span_id": "missing", "category": "node", "status": "completed"},
        {"span_id": "a", "parent_span_id": "b", "category": "node", "status": "completed"},
        {"span_id": "b", "parent_span_id": "a", "category": "agent", "status": "completed"},
    ]
    result = validate_span_hierarchy(trace, spans)
    assert result["valid"] is False
    assert result["root_count"] == 1 and result["orphan_count"] == 1
    assert result["cycle_count"] == 2 and result["relationship_violation_count"] > 0


@pytest.mark.asyncio
async def test_backfill_marks_partial_degraded_hierarchy(hierarchy_service):
    context = await hierarchy_service.ensure_trace("backfill", source="backfill")
    trace = await hierarchy_service.store.fetch_one(
        "SELECT * FROM observability_traces WHERE trace_id=?", (context.trace_id,)
    )
    assert trace["attributes"] == {"precision": "partial", "hierarchy_degraded": True}


@pytest.mark.asyncio
async def test_historical_reconciliation_is_idempotent(hierarchy_service):
    context = await hierarchy_service.ensure_trace("historical")
    ended = datetime.now(UTC)
    await hierarchy_service.store.end_trace(
        context.trace_id, status="completed", ended_at=ended.isoformat(), duration_ms=1
    )
    dry_run = await hierarchy_service.store.reconcile_terminal_spans(dry_run=True)
    first = await hierarchy_service.store.reconcile_terminal_spans()
    second = await hierarchy_service.store.reconcile_terminal_spans()
    assert dry_run["candidate_spans"] == 1
    assert first["reconciled_spans"] == 1
    assert second["candidate_spans"] == 0 and second["reconciled_spans"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_finalize_trace_closes_all_active_spans_transactionally(
    hierarchy_service, status
):
    context = await hierarchy_service.ensure_trace(f"finalize-{status}")
    spans = (
        ("workflow.workflow", "workflow", "running"),
        ("subgraph.implementation", "subgraph", "running"),
        ("subgraph.testing_repair", "subgraph", "waiting"),
        ("node.finalize", "node", "interrupted"),
    )
    for index, (name, category, span_status) in enumerate(spans):
        await hierarchy_service.store.start_span({
            "span_id": f"{status}-{index}",
            "trace_id": context.trace_id,
            "parent_span_id": f"{status}-1" if category == "node" else context.span_id,
            "name": name,
            "category": category,
            "status": span_status,
            "started_at": datetime.now(UTC).isoformat(),
        })
    before = await hierarchy_service.store.trace_detail(context.trace_id)
    assert before["hierarchy_validation"]["active_span_count"] == 5
    assert before["hierarchy_validation"]["terminal_active_spans"] == 4
    assert before["hierarchy_validation"]["valid"] is True
    result = await hierarchy_service.finalize_workflow_trace(
        f"finalize-{status}", status, reason="unit_terminal"
    )
    trace = await hierarchy_service.store.find_trace(f"finalize-{status}")
    active = await hierarchy_service.store.fetch_one(
        """SELECT COUNT(*) AS count FROM observability_spans
           WHERE trace_id=? AND status IN ('running','waiting','interrupted')""",
        (context.trace_id,),
    )
    rows = await hierarchy_service.store.fetch_all(
        "SELECT status,ended_at,duration_ms,updated_at FROM observability_spans WHERE trace_id=?",
        (context.trace_id,),
    )
    assert result["closed_spans"] == 5 and active["count"] == 0
    assert trace["status"] == status and trace["ended_at"] and trace["duration_ms"] >= 0
    assert trace["updated_at"]
    assert all(row["status"] == status for row in rows)
    assert all(row["ended_at"] and row["duration_ms"] >= 0 and row["updated_at"] for row in rows)
    after = await hierarchy_service.store.trace_detail(context.trace_id)
    assert after["hierarchy_validation"]["active_span_count"] == 0
    assert after["hierarchy_validation"]["terminal_active_spans"] == 0
    assert after["hierarchy_validation"]["valid"] is True


@pytest.mark.asyncio
async def test_finalize_trace_is_idempotent_and_concurrency_safe(hierarchy_service):
    context = await hierarchy_service.ensure_trace("idempotent-terminal")
    first, second = await asyncio.gather(
        hierarchy_service.finalize_workflow_trace(
            "idempotent-terminal", "completed", reason="first"
        ),
        hierarchy_service.finalize_workflow_trace(
            "idempotent-terminal", "completed", reason="second"
        ),
    )
    trace_before = await hierarchy_service.store.find_trace("idempotent-terminal")
    third = await hierarchy_service.finalize_workflow_trace(
        "idempotent-terminal", "failed", reason="late_duplicate"
    )
    trace_after = await hierarchy_service.store.find_trace("idempotent-terminal")
    assert sorted((first["changed"], second["changed"])) == [False, True]
    assert third["changed"] is False
    assert trace_after["status"] == "completed"
    assert trace_after["ended_at"] == trace_before["ended_at"]
    assert trace_after["duration_ms"] == trace_before["duration_ms"]
    detail = await hierarchy_service.store.trace_detail(context.trace_id)
    assert detail["hierarchy_validation"]["terminal_active_spans"] == 0


@pytest.mark.asyncio
async def test_restart_before_terminal_finalize_closes_interrupted_spans(tmp_path):
    database = tmp_path / "restart-terminal.sqlite"
    first = ObservabilityService(ObservabilityStore(database))
    await first.initialize()
    context = await first.ensure_trace("restart-terminal")
    await first.store.start_span({
        "span_id": "restart-subgraph",
        "trace_id": context.trace_id,
        "parent_span_id": context.span_id,
        "name": "subgraph.implementation",
        "category": "subgraph",
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
    })
    restarted = ObservabilityService(ObservabilityStore(database))
    await restarted.initialize()
    await restarted.finalize_workflow_trace(
        "restart-terminal", "completed", reason="restart_terminal"
    )
    detail = await restarted.store.trace_detail(context.trace_id)
    assert detail["status"] == "completed"
    assert detail["hierarchy_validation"]["terminal_active_spans"] == 0


def test_reconcile_uses_durable_terminal_workflow_and_is_idempotent(tmp_path):
    database = tmp_path / "durable-reconcile.sqlite"
    event_store = SQLiteWorkflowEventStore(database)
    event_store.initialize_sync()
    store = ObservabilityStore(database)
    store.initialize_sync()
    service = ObservabilityService(store)
    context = asyncio.run(service.ensure_trace("durable-terminal"))
    terminal = event(WorkflowEventType.WORKFLOW_COMPLETED, 1).model_copy(
        update={"thread_id": "durable-terminal"}
    )
    event_store.append_sync(terminal)

    missing_checkpoints = tmp_path / "missing-checkpoints.sqlite"
    dry_run = reconcile(database, checkpoint_database=missing_checkpoints)
    applied = reconcile(
        database, dry_run=False, checkpoint_database=missing_checkpoints
    )
    second = reconcile(
        database, dry_run=False, checkpoint_database=missing_checkpoints
    )
    trace = asyncio.run(store.find_trace("durable-terminal"))
    active = asyncio.run(store.fetch_one(
        """SELECT COUNT(*) AS count FROM observability_spans
           WHERE trace_id=? AND status IN ('running','waiting','interrupted')""",
        (context.trace_id,),
    ))
    assert dry_run["candidate_traces"] == 1 and dry_run["candidate_spans"] == 1
    assert applied["reconciled_traces"] == 1 and applied["reconciled_spans"] == 1
    assert second["candidate_traces"] == 0 and second["reconciled_spans"] == 0
    assert trace["status"] == "completed" and active["count"] == 0


def test_explicit_reconciliation_closes_root_workflow_child_and_waiting_span(tmp_path):
    database = tmp_path / "explicit-reconcile.sqlite"
    store = ObservabilityStore(database)
    store.initialize_sync()
    service = ObservabilityService(store)
    context = asyncio.run(service.ensure_trace("explicit-terminal"))
    for span_id, name, category, status in (
        ("legacy-workflow", "workflow.workflow", "workflow", "running"),
        ("waiting-approval", "approval.create_project", "approval", "waiting"),
        ("unfinished-interrupt", "approval.interrupted", "approval", "interrupted"),
    ):
        asyncio.run(store.start_span({
            "span_id": span_id,
            "trace_id": context.trace_id,
            "parent_span_id": context.span_id,
            "name": name,
            "category": category,
            "status": status,
            "started_at": datetime.now(UTC).isoformat(),
        }))
    before = asyncio.run(store.trace_detail(context.trace_id))
    assert before["hierarchy_validation"]["active_span_count"] == 4
    assert before["hierarchy_validation"]["terminal_active_spans"] == 3

    first = reconcile(
        database,
        dry_run=False,
        checkpoint_database=tmp_path / "missing.sqlite",
        trace_id=context.trace_id,
        terminal_status="completed",
    )
    trace_after_first = asyncio.run(store.trace_detail(context.trace_id))
    second = reconcile(
        database,
        dry_run=False,
        checkpoint_database=tmp_path / "missing.sqlite",
        trace_id=context.trace_id,
        terminal_status="completed",
    )
    trace_after_second = asyncio.run(store.trace_detail(context.trace_id))
    statuses = {span["name"]: span["status"] for span in trace_after_first["spans"]}
    validation = trace_after_first["hierarchy_validation"]
    assert first["reconciled_traces"] == 1 and first["reconciled_spans"] == 4
    assert statuses["workflow"] == "completed"
    assert statuses["workflow.workflow"] == "completed"
    assert statuses["approval.create_project"] == "completed"
    assert statuses["approval.interrupted"] == "completed"
    assert trace_after_first["status"] == "completed"
    assert trace_after_first["ended_at"] and trace_after_first["duration_ms"] >= 0
    assert validation["active_span_count"] == 0
    assert validation["terminal_active_spans"] == 0
    assert validation["valid"] is True
    assert validation["relationship_violation_count"] == 0
    assert second["candidate_traces"] == 0 and second["reconciled_spans"] == 0
    assert trace_after_second["ended_at"] == trace_after_first["ended_at"]
    assert trace_after_second["duration_ms"] == trace_after_first["duration_ms"]


@pytest.mark.asyncio
async def test_observability_store_failure_is_best_effort(monkeypatch, hierarchy_service):
    context = await hierarchy_service.ensure_trace("store-failure")

    async def fail_finalize(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(hierarchy_service.store, "finalize_trace", fail_finalize)
    result = await hierarchy_service.finalize_workflow_trace(
        "store-failure", "completed", reason="store_failure_test"
    )
    assert context.trace_id
    assert result == {"trace_found": True, "changed": False, "store_failed": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_status", "trace_status", "registry_action"),
    [
        ("completed", "completed", "completed"),
        ("tests_failed", "failed", "failed"),
        ("user_cancelled", "cancelled", "failed"),
    ],
)
async def test_runner_finalizes_observability_before_registry_terminal_state(
    terminal_status, trace_status, registry_action
):
    from api.services.workflow_runner import WorkflowRunner
    from api.services.workflow_registry import WorkflowRegistry

    calls: list[str] = []

    class RecordingRegistry(WorkflowRegistry):
        async def mark_completed(self, thread_id):
            calls.append("registry:completed")
            await super().mark_completed(thread_id)

        async def mark_failed(self, thread_id, error):
            calls.append("registry:failed")
            await super().mark_failed(thread_id, error)

    class RecordingObservability:
        async def finalize_workflow_trace(self, thread_id, status, **kwargs):
            calls.append(f"trace:{status}")

    registry = RecordingRegistry()
    await registry.register("runner-terminal")
    runner = WorkflowRunner(
        graph=object(),
        persistence=object(),
        registry=registry,
        emitter=object(),
        event_factory=object(),
        forwarder=object(),
        observability=RecordingObservability(),
    )
    result = SimpleNamespace(
        interrupted=False,
        final_state={"terminal_status": terminal_status, "failure_message": "failure"},
    )
    await runner._record_result("runner-terminal", result)
    assert calls == [f"trace:{trace_status}", f"registry:{registry_action}"]
