from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import ApiServices, get_services
from api.observability_models import (
    ObservabilitySortOrder, RetentionRequest, SpanCategory, SpanSortBy,
    SpanStatus, TraceSortBy,
)

router = APIRouter(prefix="/api/observability", tags=["observability"])


def _service(services: ApiServices):
    if services.observability is None:
        raise HTTPException(status_code=503, detail="Observability is not available")
    return services.observability


@router.get("/summary")
async def summary(services: ApiServices = Depends(get_services)):
    return await _service(services).summary()


@router.get("/traces")
async def traces(
    workflow_id: str | None = None,
    status: SpanStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
    sort_by: TraceSortBy = "started_at", sort_order: ObservabilitySortOrder = "desc",
    services: ApiServices = Depends(get_services),
):
    clauses, values = [], []
    if workflow_id: clauses.append("workflow_id=?"); values.append(workflow_id)
    if status: clauses.append("status=?"); values.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    store = _service(services).store
    items = await store.fetch_all(f"SELECT * FROM observability_traces{where} ORDER BY {sort_by} {sort_order.upper()} LIMIT ? OFFSET ?", (*values, limit, offset))
    total = await store.fetch_one(f"SELECT COUNT(*) AS total FROM observability_traces{where}", tuple(values))
    return {"items": items, "total": int(total["total"] if total else 0), "limit": limit, "offset": offset}


@router.get("/traces/{trace_id}")
async def trace_detail(trace_id: str, services: ApiServices = Depends(get_services)):
    result = await _service(services).store.trace_detail(trace_id)
    if result is None: raise HTTPException(status_code=404, detail="Trace not found")
    llm_costs = getattr(services, "llm_costs", None)
    if llm_costs is not None:
        calls = (await llm_costs.list_calls(trace_id=trace_id, limit=500, offset=0))["items"]
        by_span: dict[str, list[dict]] = {}
        for call in calls:
            by_span.setdefault(str(call.get("span_id")), []).append(call)
        for span in result["spans"]:
            if span["category"] == "llm":
                span["llm_calls"] = by_span.get(str(span["span_id"]), [])
        result["llm_cost_summary"] = await llm_costs.summary(trace_id=trace_id)
    return result


@router.get("/workflows/{thread_id}")
async def workflow_observability(thread_id: str, services: ApiServices = Depends(get_services)):
    service = _service(services)
    traces = await service.store.fetch_all("SELECT * FROM observability_traces WHERE workflow_id=? ORDER BY started_at", (thread_id,))
    return {"thread_id": thread_id, "summary": await service.workflow_summary(thread_id), "traces": traces}


@router.get("/spans")
async def spans(
    trace_id: str | None = None, category: SpanCategory | None = None, status: SpanStatus | None = None,
    agent: str | None = None, slow_only: bool = False,
    limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0),
    sort_by: SpanSortBy = "started_at", sort_order: ObservabilitySortOrder = "desc",
    services: ApiServices = Depends(get_services),
):
    clauses, values = [], []
    for column, value in (("trace_id", trace_id), ("category", category), ("status", status), ("agent", agent)):
        if value is not None: clauses.append(f"{column}=?"); values.append(value)
    if slow_only: clauses.append("is_slow=1")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    store = _service(services).store
    items = await store.fetch_all(f"SELECT * FROM observability_spans{where} ORDER BY {sort_by} {sort_order.upper()} LIMIT ? OFFSET ?", (*values, limit, offset))
    total = await store.fetch_one(f"SELECT COUNT(*) AS total FROM observability_spans{where}", tuple(values))
    return {"items": items, "total": int(total["total"] if total else 0), "limit": limit, "offset": offset}


@router.get("/spans/{span_id}")
async def span_detail(span_id: str, services: ApiServices = Depends(get_services)):
    store = _service(services).store
    span = await store.fetch_one("SELECT * FROM observability_spans WHERE span_id=?", (span_id,))
    if span is None: raise HTTPException(status_code=404, detail="Span not found")
    span["events"] = await store.fetch_all("SELECT * FROM observability_span_events WHERE span_id=? ORDER BY timestamp", (span_id,))
    span["logs"] = await store.fetch_all("SELECT * FROM observability_logs WHERE span_id=? ORDER BY timestamp", (span_id,))
    return span


@router.get("/errors")
async def errors(limit: int = Query(default=100, ge=1, le=500), services: ApiServices = Depends(get_services)):
    items = await _service(services).store.fetch_all(
        """SELECT error_fingerprint,error_type,operation,COUNT(*) AS occurrences,COUNT(DISTINCT workflow_id) AS workflows,
        MIN(started_at) AS first_seen,MAX(started_at) AS last_seen FROM observability_spans
        WHERE error_fingerprint IS NOT NULL GROUP BY error_fingerprint,error_type,operation ORDER BY occurrences DESC,last_seen DESC LIMIT ?""", (limit,))
    return {"items": items}


@router.get("/agents")
async def agents(services: ApiServices = Depends(get_services)):
    items = await _service(services).store.fetch_all(
        "SELECT agent,COUNT(*) AS calls,AVG(duration_ms) AS average_duration_ms,SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failures FROM observability_spans WHERE agent IS NOT NULL GROUP BY agent ORDER BY calls DESC")
    return {"items": items}


@router.get("/tools")
async def tools(services: ApiServices = Depends(get_services)):
    items = await _service(services).store.fetch_all(
        "SELECT tool_name,server_name,COUNT(*) AS calls,AVG(duration_ms) AS average_duration_ms,SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failures FROM observability_tool_calls GROUP BY tool_name,server_name ORDER BY calls DESC")
    return {"items": items}


@router.get("/llm")
async def llm(services: ApiServices = Depends(get_services)):
    items = await _service(services).store.fetch_all(
        "SELECT agent,provider,model,COUNT(*) AS calls,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,SUM(total_tokens) AS total_tokens,AVG(duration_ms) AS average_duration_ms FROM observability_llm_calls GROUP BY agent,provider,model ORDER BY calls DESC")
    return {"items": items}


@router.get("/slow-spans")
async def slow_spans(limit: int = Query(default=100, ge=1, le=500), services: ApiServices = Depends(get_services)):
    return {"items": await _service(services).store.fetch_all("SELECT * FROM observability_spans WHERE is_slow=1 ORDER BY duration_ms DESC LIMIT ?", (limit,))}


@router.get("/logs")
async def logs(trace_id: str | None = None, limit: int = Query(default=200, ge=1, le=1000), services: ApiServices = Depends(get_services)):
    if trace_id:
        items = await _service(services).store.fetch_all("SELECT * FROM observability_logs WHERE trace_id=? ORDER BY timestamp DESC LIMIT ?", (trace_id, limit))
    else:
        items = await _service(services).store.fetch_all("SELECT * FROM observability_logs ORDER BY timestamp DESC LIMIT ?", (limit,))
    return {"items": items}


@router.get("/artifacts")
async def artifacts(trace_id: str | None = None, limit: int = Query(default=200, ge=1, le=1000), services: ApiServices = Depends(get_services)):
    query = "SELECT * FROM observability_artifacts" + (" WHERE trace_id=?" if trace_id else "") + " ORDER BY created_at DESC LIMIT ?"
    params = (trace_id, limit) if trace_id else (limit,)
    return {"items": await _service(services).store.fetch_all(query, params)}


@router.post("/retention/run")
async def run_retention(body: RetentionRequest, services: ApiServices = Depends(get_services)):
    return await _service(services).store.retention(days=body.days, dry_run=body.dry_run, batch_size=body.batch_size)
