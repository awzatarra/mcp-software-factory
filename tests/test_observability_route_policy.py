from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from api.services.observability_context import get_observability_context
from api.services.observability_middleware import ObservabilityMiddleware
from api.services.observability_route_policy import ObservabilityRoutePolicy
from api.services.observability_service import ObservabilityService
from api.services.observability_store import ObservabilityStore
from scripts.cleanup_observability_noise import cleanup_noise


def _count(store: ObservabilityStore, table: str) -> int:
    row = asyncio.run(store.fetch_one(f"SELECT COUNT(*) AS count FROM {table}"))
    return int(row["count"])


def _test_app(service: ObservabilityService, policy: ObservabilityRoutePolicy | None = None):
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.services = SimpleNamespace(observability=service)
        yield

    application = FastAPI(lifespan=lifespan)
    application.add_middleware(ObservabilityMiddleware, route_policy=policy)

    async def ok():
        return {"status": "ok"}

    for path in (
        "/api/observability/summary",
        "/api/observability/spans",
        "/api/observability/traces",
        "/api/alerts/summary",
        "/api/notifications/summary",
        "/api/dashboard/summary",
        "/api/dashboard/activity",
        "/api/dashboard/attention",
        "/api/dashboard/agents",
        "/api/dashboard/timeseries",
        "/api/custom/ignored",
    ):
        application.add_api_route(path, ok, methods=["GET"])

    @application.get("/api/test/normal")
    async def normal():
        return {"status": "ok"}

    @application.get("/api/test/error")
    async def error():
        return JSONResponse(status_code=500, content={"detail": "failed"})

    @application.get("/api/observability/error")
    async def ignored_error():
        return JSONResponse(status_code=500, content={"detail": "failed"})

    @application.post("/api/observability/retention/run")
    async def retention_run():
        return {"status": "completed"}

    @application.get("/api/items/{item_id}")
    async def item(item_id: str):
        return {"item_id": item_id}

    @application.get("/api/observability/context")
    async def ignored_context():
        return get_observability_context().serialize()

    return application


@pytest.fixture
def middleware_store(tmp_path):
    store = ObservabilityStore(tmp_path / "route-policy.sqlite")
    asyncio.run(store.initialize())
    return store


@pytest.mark.parametrize("path", [
    "/api/observability/summary",
    "/api/observability/spans",
    "/api/observability/traces",
])
def test_observability_queries_do_not_observe_themselves(middleware_store, path):
    with TestClient(_test_app(ObservabilityService(middleware_store))) as client:
        assert client.get(path).status_code == 200
    assert _count(middleware_store, "observability_traces") == 0
    assert _count(middleware_store, "observability_spans") == 0


@pytest.mark.parametrize("path", [
    "/api/alerts/summary",
    "/api/notifications/summary",
    "/api/dashboard/summary",
    "/api/dashboard/activity",
    "/api/dashboard/attention",
    "/api/dashboard/agents",
    "/api/dashboard/timeseries",
])
def test_default_polling_routes_do_not_create_traces(middleware_store, path):
    with TestClient(_test_app(ObservabilityService(middleware_store))) as client:
        assert client.get(f"{path}?window=30m").status_code == 200
    assert _count(middleware_store, "observability_traces") == 0


def test_normal_route_creates_trace_and_uses_route_template(middleware_store):
    with TestClient(_test_app(ObservabilityService(middleware_store))) as client:
        response = client.get("/api/test/normal?view=compact")
    assert response.headers["traceparent"].startswith("00-")
    span = asyncio.run(middleware_store.fetch_one("SELECT * FROM observability_spans"))
    assert span["attributes"]["http.route"] == "/api/test/normal"


def test_ignored_500_is_captured_when_enabled(middleware_store):
    policy = ObservabilityRoutePolicy.from_environment(
        {"OBSERVABILITY_CAPTURE_IGNORED_ERRORS": "true"}
    )
    with TestClient(_test_app(ObservabilityService(middleware_store), policy)) as client:
        response = client.get("/api/observability/error")
    assert response.status_code == 500
    trace = asyncio.run(middleware_store.fetch_one("SELECT * FROM observability_traces"))
    assert trace["status"] == "failed" and trace["source"] == "api_ignored_error"


def test_query_params_do_not_change_exclusion(middleware_store):
    with TestClient(_test_app(ObservabilityService(middleware_store))) as client:
        client.get("/api/observability/summary?refresh=true&window=30")
    assert _count(middleware_store, "observability_traces") == 0


def test_ignored_route_does_not_contaminate_contextvars(middleware_store):
    original = get_observability_context()
    with TestClient(_test_app(ObservabilityService(middleware_store))) as client:
        response = client.get("/api/observability/context")
    assert response.json()["trace_id"] is None
    assert get_observability_context() == original


def test_concurrent_requests_keep_independent_context_and_persistence(middleware_store):
    with TestClient(_test_app(ObservabilityService(middleware_store))) as client:
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(lambda _: client.get("/api/test/normal"), range(16)))
    traceparents = {response.headers["traceparent"] for response in responses}
    assert len(traceparents) == 16
    assert _count(middleware_store, "observability_traces") == 16


def test_custom_exact_route_and_prefix_are_ignored(middleware_store):
    policy = ObservabilityRoutePolicy.from_environment({
        "OBSERVABILITY_IGNORED_ROUTES": "/api/test/normal",
        "OBSERVABILITY_IGNORED_PREFIXES": "/api/items",
    })
    with TestClient(_test_app(ObservabilityService(middleware_store), policy)) as client:
        client.get("/api/test/normal")
        client.get("/api/items/123")
    assert _count(middleware_store, "observability_traces") == 0


def test_parameterized_route_configuration_uses_fastapi_template(middleware_store):
    policy = ObservabilityRoutePolicy.from_environment({
        "OBSERVABILITY_IGNORED_ROUTES": "/api/items/{item_id}",
    })
    with TestClient(_test_app(ObservabilityService(middleware_store), policy)) as client:
        client.get("/api/items/abc?include=details")
    assert _count(middleware_store, "observability_traces") == 0


def test_polling_sampling_can_be_enabled(middleware_store):
    policy = ObservabilityRoutePolicy.from_environment(
        {"OBSERVABILITY_POLLING_SAMPLE_RATE": "1"}, sampler=lambda: 0.5
    )
    with TestClient(_test_app(ObservabilityService(middleware_store), policy)) as client:
        client.get("/api/dashboard/summary")
    assert _count(middleware_store, "observability_traces") == 1


def test_critical_write_under_ignored_prefix_is_still_observed(middleware_store):
    with TestClient(_test_app(ObservabilityService(middleware_store))) as client:
        response = client.post("/api/observability/retention/run")
    assert response.status_code == 200
    assert response.headers["traceparent"].startswith("00-")
    assert _count(middleware_store, "observability_traces") == 1


def test_observability_data_remains_durable_across_app_restart(tmp_path):
    database = tmp_path / "durable.sqlite"
    first_store = ObservabilityStore(database)
    asyncio.run(first_store.initialize())
    with TestClient(_test_app(ObservabilityService(first_store))) as client:
        client.get("/api/test/normal")

    second_store = ObservabilityStore(database)
    asyncio.run(second_store.initialize())
    with TestClient(_test_app(ObservabilityService(second_store))) as client:
        client.get("/api/observability/summary")
    assert _count(second_store, "observability_traces") == 1


def _insert_api_trace(
    store: ObservabilityStore,
    route: str,
    *,
    workflow_id: str | None = None,
    status: str = "completed",
) -> str:
    trace_id = uuid4().hex
    span_id = uuid4().hex[:16]
    asyncio.run(store.create_trace({
        "trace_id": trace_id,
        "workflow_id": workflow_id,
        "name": "api.request",
        "status": "running",
        "source": "api",
        "root_span_id": span_id,
    }))
    asyncio.run(store.start_span({
        "span_id": span_id,
        "trace_id": trace_id,
        "workflow_id": workflow_id,
        "name": f"GET {route}",
        "category": "api",
        "kind": "server",
        "status": "running",
        "operation": "GET",
        "attributes": {"http.method": "GET", "http.route": route},
    }))
    asyncio.run(store.end_span(
        span_id, status=status, ended_at="2026-01-01T00:00:01+00:00", duration_ms=1
    ))
    asyncio.run(store.end_trace(
        trace_id, status=status, ended_at="2026-01-01T00:00:01+00:00", duration_ms=1
    ))
    return trace_id


def test_cleanup_is_safe_transactional_and_idempotent(tmp_path):
    database = Path(tmp_path / "cleanup.sqlite")
    store = ObservabilityStore(database)
    asyncio.run(store.initialize())
    noise_id = _insert_api_trace(store, "/api/observability/summary")
    workflow_id = _insert_api_trace(
        store, "/api/observability/summary", workflow_id="workflow-1"
    )
    error_id = _insert_api_trace(store, "/api/dashboard/summary", status="failed")

    dry_run = cleanup_noise(database)
    assert dry_run["candidate_traces"] == 1 and dry_run["candidate_spans"] == 1
    assert _count(store, "observability_traces") == 3

    executed = cleanup_noise(database, dry_run=False)
    assert executed["deleted_traces"] == 1 and executed["deleted_spans"] == 1
    remaining = asyncio.run(store.fetch_all("SELECT trace_id FROM observability_traces"))
    assert {row["trace_id"] for row in remaining} == {workflow_id, error_id}
    assert noise_id not in {row["trace_id"] for row in remaining}

    second = cleanup_noise(database, dry_run=False)
    assert second["candidate_traces"] == 0 and second["deleted_traces"] == 0
