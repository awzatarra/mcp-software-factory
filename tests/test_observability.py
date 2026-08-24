from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from api.app import create_app
from api.observability_models import SpanCategory, SpanKind, SpanStatus
from api.services.observability_context import ObservabilityContext, get_observability_context, observability_context
from api.services.observability_sanitizer import error_fingerprint, sanitize_mapping, safe_path
from api.services.observability_service import ObservabilityService
from api.services.observability_store import ObservabilityStore
from streaming import EventStatus, WorkflowEvent, WorkflowEventType


@pytest.fixture
async def telemetry(tmp_path):
    store = ObservabilityStore(tmp_path / "telemetry.sqlite")
    service = ObservabilityService(store, slow_span_threshold_ms=1)
    await store.initialize()
    return service


def workflow_event(event_type, sequence, *, thread_id="wf-1", data=None, status=None, timestamp=None):
    return WorkflowEvent(
        event_id=uuid4(), thread_id=thread_id, sequence=sequence, type=event_type,
        timestamp=timestamp or datetime.now(UTC), source="test", stage="planning",
        status=status or (EventStatus.RUNNING if event_type.value.endswith("started") else EventStatus.COMPLETED),
        message="safe summary", data=data or {},
    )


def test_context_child_preserves_parent_fields():
    parent = ObservabilityContext(trace_id="a" * 32, span_id="b" * 16, workflow_id="wf")
    assert parent.child(span_id="c" * 16).trace_id == parent.trace_id


def test_context_serialize_restore_round_trip():
    context = ObservabilityContext(trace_id="a" * 32, agent="Planner", node="plan")
    assert ObservabilityContext.restore(context.serialize()) == context


def test_context_resets_after_scope():
    original = get_observability_context()
    with observability_context(ObservabilityContext(trace_id="a" * 32)):
        assert get_observability_context().trace_id == "a" * 32
    assert get_observability_context() == original


@pytest.mark.asyncio
async def test_context_isolated_between_concurrent_tasks():
    async def inspect(value):
        with observability_context(ObservabilityContext(trace_id=value)):
            await asyncio.sleep(0)
            return get_observability_context().trace_id
    assert await asyncio.gather(inspect("a" * 32), inspect("b" * 32)) == ["a" * 32, "b" * 32]


@pytest.mark.parametrize("key", ["authorization", "cookie", "api_key", "password", "token", "prompt", "content", "request_body"])
def test_sanitizer_redacts_sensitive_keys(key):
    assert sanitize_mapping({key: "private"})[key] == "[REDACTED]"


def test_sanitizer_truncates_long_strings():
    assert sanitize_mapping({"message": "x" * 2000})["message"].endswith("[TRUNCATED]")


@pytest.mark.parametrize(("path", "expected"), [
    (r"C:\workspace\project\main.py", "workspace/project/main.py"),
    ("/srv/workspace/project/tests/test_api.py", "workspace/project/tests/test_api.py"),
    (None, None),
])
def test_safe_path_keeps_only_relative_tail(path, expected):
    assert safe_path(path) == expected


def test_error_fingerprint_is_stable_and_normalizes_ids():
    assert error_fingerprint("Timeout", "call 123 failed", "tool") == error_fingerprint("Timeout", "call 456 failed", "tool")


@pytest.mark.asyncio
async def test_schema_creates_all_required_tables(telemetry):
    rows = await telemetry.store.fetch_all("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'observability_%'")
    assert {row["name"] for row in rows} >= {
        "observability_traces", "observability_spans", "observability_span_events",
        "observability_metrics", "observability_logs", "observability_llm_calls",
        "observability_tool_calls", "observability_artifacts",
    }


@pytest.mark.asyncio
async def test_ensure_trace_is_idempotent(telemetry):
    first = await telemetry.ensure_trace("wf")
    second = await telemetry.ensure_trace("wf")
    assert first.trace_id == second.trace_id
    row = await telemetry.store.fetch_one("SELECT COUNT(*) AS count FROM observability_traces")
    assert row["count"] == 1


@pytest.mark.asyncio
async def test_span_records_success_and_slow_flag(telemetry):
    context = await telemetry.ensure_trace("wf")
    with observability_context(context):
        async with telemetry.span("agent.plan", category="agent"):
            await asyncio.sleep(.003)
    span = await telemetry.store.fetch_one("SELECT * FROM observability_spans WHERE name='agent.plan'")
    assert span["status"] == "completed" and span["is_slow"] is True


@pytest.mark.asyncio
async def test_span_records_failure_fingerprint(telemetry):
    context = await telemetry.ensure_trace("wf")
    with pytest.raises(RuntimeError), observability_context(context):
        async with telemetry.span("node.fail", category="node"):
            raise RuntimeError("boom 123")
    span = await telemetry.store.fetch_one("SELECT * FROM observability_spans WHERE name='node.fail'")
    assert span["status"] == "failed" and span["error_fingerprint"]


@pytest.mark.asyncio
async def test_event_ingestion_builds_and_closes_trace(telemetry):
    start = datetime.now(UTC)
    await telemetry.ingest_workflow_event(workflow_event(WorkflowEventType.WORKFLOW_STARTED, 1, timestamp=start))
    await telemetry.ingest_workflow_event(workflow_event(WorkflowEventType.PLANNING_STARTED, 2, timestamp=start + timedelta(milliseconds=5)))
    await telemetry.ingest_workflow_event(workflow_event(WorkflowEventType.PLANNING_COMPLETED, 3, timestamp=start + timedelta(milliseconds=10)))
    await telemetry.ingest_workflow_event(workflow_event(WorkflowEventType.WORKFLOW_COMPLETED, 4, timestamp=start + timedelta(milliseconds=20)))
    trace = await telemetry.store.find_trace("wf-1")
    assert trace["status"] == "completed" and trace["duration_ms"] == 20
    assert (await telemetry.workflow_summary("wf-1"))["span_count"] >= 2


@pytest.mark.asyncio
async def test_event_ingestion_is_idempotent(telemetry):
    event = workflow_event(WorkflowEventType.PLANNING_STARTED, 1)
    await telemetry.ingest_workflow_event(event); await telemetry.ingest_workflow_event(event)
    row = await telemetry.store.fetch_one("SELECT COUNT(*) AS count FROM observability_span_events")
    assert row["count"] == 1
    logs = await telemetry.store.fetch_one("SELECT COUNT(*) AS count FROM observability_logs")
    assert logs["count"] == 1


@pytest.mark.asyncio
async def test_fork_gets_distinct_trace_and_lineage(telemetry):
    await telemetry.ingest_workflow_event(workflow_event(WorkflowEventType.WORKFLOW_STARTED, 1))
    await telemetry.ingest_workflow_event(workflow_event(WorkflowEventType.WORKFLOW_FORKED, 1, data={"branch_id": "fork-1", "origin_checkpoint": "cp-1"}))
    traces = await telemetry.store.fetch_all("SELECT * FROM observability_traces ORDER BY branch_id")
    assert len(traces) == 2 and traces[0]["trace_id"] != traces[1]["trace_id"]
    assert traces[0 if traces[0]["branch_id"] == "fork-1" else 1]["origin_checkpoint"] == "cp-1"


@pytest.mark.asyncio
async def test_missing_workflow_summary_is_explicit(telemetry):
    assert await telemetry.workflow_summary("missing") == {"state": "not_available"}


@pytest.mark.asyncio
async def test_reconcile_preserves_waiting_approval(telemetry):
    await telemetry.ingest_workflow_event(workflow_event(WorkflowEventType.APPROVAL_REQUIRED, 1, data={"operation": "create_project"}))
    await telemetry.store.reconcile_running()
    span = await telemetry.store.fetch_one("SELECT * FROM observability_spans WHERE category='approval'")
    assert span["status"] == "waiting"


@pytest.mark.asyncio
async def test_retention_dry_run_does_not_delete(telemetry):
    context = await telemetry.ensure_trace("old", started_at=datetime.now(UTC) - timedelta(days=60))
    await telemetry.store.end_trace(context.trace_id, status="completed", ended_at=datetime.now(UTC).isoformat(), duration_ms=1)
    result = await telemetry.store.retention(days=30, dry_run=True, batch_size=100)
    assert result["eligible"] == 1 and result["deleted"] == 0
    assert await telemetry.store.find_trace("old") is not None


@pytest.mark.asyncio
async def test_retention_execute_deletes_only_terminal_old_trace(telemetry):
    old = await telemetry.ensure_trace("old", started_at=datetime.now(UTC) - timedelta(days=60))
    await telemetry.store.end_trace(old.trace_id, status="completed", ended_at=datetime.now(UTC).isoformat(), duration_ms=1)
    await telemetry.ensure_trace("active", started_at=datetime.now(UTC) - timedelta(days=60))
    result = await telemetry.store.retention(days=30, dry_run=False, batch_size=100)
    assert result["deleted"] == 1 and await telemetry.store.find_trace("active") is not None


def _app(service):
    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(observability=service)
    application = create_app(factory)

    @application.get("/api/test/normal")
    async def normal_route():
        return {"status": "ok"}

    return application


def test_observability_api_summary_and_trace_detail(tmp_path):
    store = ObservabilityStore(tmp_path / "api.sqlite"); asyncio.run(store.initialize())
    service = ObservabilityService(store)
    asyncio.run(service.ensure_trace("medical-booking"))
    with TestClient(_app(service)) as client:
        summary = client.get("/api/observability/summary")
        listing = client.get("/api/observability/traces?workflow_id=medical-booking")
        trace_id = listing.json()["items"][0]["trace_id"]
        detail = client.get(f"/api/observability/traces/{trace_id}")
    assert summary.status_code == 200 and summary.json()["traces"] >= 1
    assert detail.status_code == 200 and detail.json()["workflow_id"] == "medical-booking"
    assert detail.json()["hierarchy_validation"]["root_count"] == 1


@pytest.mark.parametrize("query", ["category=invalid", "status=unknown", "sort_by=drop_table", "sort_order=sideways"])
def test_observability_api_rejects_invalid_literal_filters(tmp_path, query):
    store = ObservabilityStore(tmp_path / f"api-{uuid4().hex}.sqlite"); asyncio.run(store.initialize())
    with TestClient(_app(ObservabilityService(store))) as client:
        endpoint = "/api/observability/spans" if "category" in query or "sort_by" in query else "/api/observability/traces"
        response = client.get(f"{endpoint}?{query}")
    assert response.status_code == 422


def test_middleware_emits_traceparent_and_does_not_store_bodies(tmp_path):
    store = ObservabilityStore(tmp_path / "middleware.sqlite"); asyncio.run(store.initialize())
    with TestClient(_app(ObservabilityService(store))) as client:
        response = client.get("/api/test/normal", headers={"Authorization": "Bearer private"})
    assert response.status_code == 200 and response.headers["traceparent"].startswith("00-")
    raw = (tmp_path / "middleware.sqlite").read_bytes()
    assert b"Bearer private" not in raw


def test_health_docs_and_openapi_are_ignored_by_middleware(tmp_path):
    store = ObservabilityStore(tmp_path / "ignored.sqlite"); asyncio.run(store.initialize())
    with TestClient(_app(ObservabilityService(store))) as client:
        client.get("/docs"); client.get("/openapi.json")
    rows = asyncio.run(store.fetch_one("SELECT COUNT(*) AS count FROM observability_traces"))
    assert rows["count"] == 0


@pytest.mark.parametrize("category", [
    "workflow", "subgraph", "node", "agent", "llm", "tool", "mcp", "approval",
    "test", "repair", "retry", "persistence", "notification", "alert", "api",
    "worker", "artifact", "validation",
])
def test_canonical_span_categories_are_contract_values(category):
    assert TypeAdapter(SpanCategory).validate_python(category) == category


@pytest.mark.parametrize("status", [
    "running", "completed", "failed", "cancelled", "waiting", "interrupted", "timeout", "skipped",
])
def test_canonical_span_statuses_are_contract_values(status):
    assert TypeAdapter(SpanStatus).validate_python(status) == status


@pytest.mark.parametrize("kind", ["internal", "server", "client", "producer", "consumer"])
def test_canonical_span_kinds_are_contract_values(kind):
    assert TypeAdapter(SpanKind).validate_python(kind) == kind


@pytest.mark.parametrize("invalid", ["workflow_state", "unknown_status", "remote_kind"])
def test_literal_contracts_reject_noncanonical_values(invalid):
    adapters = (TypeAdapter(SpanCategory), TypeAdapter(SpanStatus), TypeAdapter(SpanKind))
    assert all(_rejects(adapter, invalid) for adapter in adapters)


def test_secret_values_are_redacted_inside_messages():
    result = sanitize_mapping({"message": "failed with Bearer private-token"})
    assert result["message"] == "failed with Bearer [REDACTED]"


def _rejects(adapter, value):
    try:
        adapter.validate_python(value)
    except ValueError:
        return True
    return False
