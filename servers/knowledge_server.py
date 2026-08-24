from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import os
from time import perf_counter
from typing import Any, Awaitable, Callable
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

from api.knowledge_models import KnowledgeType, SubmitLearningRequest
from api.services.knowledge_providers import LLMCostKnowledgeFinOps, SQLiteVectorStore
from api.services.knowledge_service import KnowledgeError, KnowledgeService
from api.services.knowledge_store import KnowledgeStore
from api.services.llm_cost_service import LLMCostService
from api.services.llm_cost_store import LLMCostStore
from api.services.observability_service import ObservabilityService
from api.services.observability_context import ObservabilityContext, get_observability_context, observability_context
from api.services.observability_sanitizer import error_fingerprint, stable_hash
from api.services.observability_store import ObservabilityStore
from streaming.sqlite_store import workflow_event_store_path


mcp = FastMCP("software-factory-knowledge")
_service: KnowledgeService | None = None


async def get_service() -> KnowledgeService:
    global _service
    if _service is None:
        database_path = workflow_event_store_path()
        observability_store = ObservabilityStore(database_path)
        await observability_store.initialize()
        observability = ObservabilityService(observability_store)
        llm_costs = LLMCostService(LLMCostStore(database_path), observability_store)
        await llm_costs.initialize()
        store = KnowledgeStore(database_path)
        _service = KnowledgeService(
            store, vector_store=SQLiteVectorStore(database_path), observability=observability,
            finops=LLMCostKnowledgeFinOps(llm_costs),
        )
        await _service.initialize()
    return _service


@asynccontextmanager
async def knowledge_trace(
    service: KnowledgeService,
    operation: str,
    project_id: str,
    *,
    workflow_id: str | None = None,
    branch_id: str = "original",
    agent_name: str | None = None,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
):
    observability = service.observability
    current = get_observability_context()
    if observability is None or current.trace_id:
        yield
        return

    requested_trace_id = str(trace_id or "").strip()[:128] or None
    owns_trace = True
    effective_trace_id = uuid4().hex
    effective_parent_span_id: str | None = None
    if requested_trace_id:
        existing_trace = await observability.store.fetch_one(
            "SELECT trace_id FROM observability_traces WHERE trace_id=?",
            (requested_trace_id,),
        )
        if existing_trace is not None:
            owns_trace = False
            effective_trace_id = requested_trace_id
            requested_parent = str(parent_span_id or "").strip()[:128] or None
            if requested_parent:
                parent = await observability.store.fetch_one(
                    "SELECT trace_id FROM observability_spans WHERE span_id=?",
                    (requested_parent,),
                )
                if parent and parent.get("trace_id") == effective_trace_id:
                    effective_parent_span_id = requested_parent

    span_id = stable_hash(uuid4().hex)[:16]
    started = datetime.now(UTC)
    started_clock = perf_counter()
    failure: Exception | None = None
    safe_attributes = {"operation": operation, "project_id_hash": stable_hash(project_id)}
    if owns_trace:
        await observability._safe(observability.store.create_trace, {
            "trace_id": effective_trace_id,
            "workflow_id": workflow_id,
            "branch_id": branch_id,
            "name": "knowledge.mcp",
            "status": "running",
            "source": "knowledge_mcp",
            "started_at": started.isoformat(),
            "root_span_id": span_id,
            "attributes": safe_attributes,
        })
    await observability._safe(observability.store.start_span, {
        "span_id": span_id,
        "trace_id": effective_trace_id,
        "parent_span_id": effective_parent_span_id,
        "name": f"knowledge.mcp.{operation}",
        "category": "knowledge",
        "kind": "server",
        "status": "running",
        "workflow_id": workflow_id,
        "branch_id": branch_id,
        "agent": agent_name,
        "operation": operation,
        "started_at": started.isoformat(),
        "attributes": safe_attributes,
    })
    try:
        with observability_context(ObservabilityContext(
            trace_id=effective_trace_id,
            span_id=span_id,
            workflow_id=workflow_id,
            branch_id=branch_id,
            agent=agent_name,
            operation=operation,
        )):
            yield
    except Exception as exc:
        failure = exc
        raise
    finally:
        ended = datetime.now(UTC)
        duration = (perf_counter() - started_clock) * 1000
        status = "failed" if failure else "completed"
        await observability._safe(
            observability.store.end_span,
            span_id,
            status=status,
            ended_at=ended.isoformat(),
            duration_ms=duration,
            error_type=type(failure).__name__ if failure else None,
            error_message=type(failure).__name__ if failure else None,
            error_fingerprint=(
                error_fingerprint(
                    type(failure).__name__, "knowledge operation failed", operation
                )
                if failure
                else None
            ),
            is_slow=duration >= observability.slow_span_threshold_ms,
        )
        if owns_trace:
            await observability._safe(
                observability.store.end_trace,
                effective_trace_id,
                status=status,
                ended_at=ended.isoformat(),
                duration_ms=duration,
            )


async def run_read(service: KnowledgeService, operation: str, project_id: str,
                   action: Callable[[], Awaitable[dict[str, Any]]],
                   fallback: dict[str, Any],
                   trace_context: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        async with knowledge_trace(service, operation, project_id, **(trace_context or {})):
            return await action()
    except KnowledgeError:
        raise
    except Exception as exc:
        if os.getenv("KNOWLEDGE_READ_FAIL_MODE", "open").casefold() != "open":
            raise
        return {**fallback, "project_id": project_id, "degraded": True,
                "failure_type": "knowledge_unavailable", "failure_reason": type(exc).__name__}


@mcp.tool()
async def search_knowledge(query: str, project_id: str, knowledge_types: list[str] | None = None,
                           limit: int = 5) -> dict[str, Any]:
    """Search indexed knowledge inside one mandatory project boundary."""
    service = await get_service()
    return await run_read(service, "search_knowledge", project_id,
                          lambda: service.search_knowledge(query=query, project_id=project_id,
                                                           knowledge_types=knowledge_types, limit=limit),
                          {"results": [], "retrieved_items": 0, "retrieval_latency_ms": None})


@mcp.tool()
async def get_relevant_context(
    query: str,
    project_id: str,
    knowledge_types: list[str] | None = None,
    limit: int = 5,
    workflow_id: str | None = None,
    branch_id: str = "original",
    agent_name: str | None = None,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
) -> dict[str, Any]:
    """Build grounded context from indexed project knowledge and return provenance."""
    service = await get_service()
    return await run_read(
        service,
        "get_relevant_context",
        project_id,
        lambda: service.get_relevant_context(
            query=query,
            project_id=project_id,
            knowledge_types=knowledge_types,
            limit=limit,
        ),
        {"results": [], "retrieved_items": 0, "retrieval_latency_ms": None,
         "context": "", "sources": []},
        {
            "workflow_id": workflow_id,
            "branch_id": branch_id,
            "agent_name": agent_name,
            "trace_id": trace_id,
            "parent_span_id": parent_span_id,
        },
    )


@mcp.tool()
async def get_project_decisions(project_id: str, query: str = "decision",
                                limit: int = 10) -> dict[str, Any]:
    """Retrieve indexed architectural and implementation decisions for a project."""
    service = await get_service()
    return await run_read(service, "get_project_decisions", project_id,
                          lambda: service.get_project_decisions(project_id=project_id, query=query, limit=limit),
                          {"results": [], "retrieved_items": 0, "retrieval_latency_ms": None})


@mcp.tool()
async def get_similar_implementations(query: str, project_id: str, limit: int = 5) -> dict[str, Any]:
    """Retrieve project-local code, test, and solution patterns similar to a query."""
    service = await get_service()
    return await run_read(service, "get_similar_implementations", project_id,
                          lambda: service.get_similar_implementations(project_id=project_id, query=query, limit=limit),
                          {"results": [], "retrieved_items": 0, "retrieval_latency_ms": None})


@mcp.tool()
async def get_knowledge_source(knowledge_id: str, project_id: str) -> dict[str, Any]:
    """Read one indexed source with full provenance inside its project boundary."""
    service = await get_service()
    return await run_read(service, "get_knowledge_source", project_id,
                          lambda: service.get_knowledge_source(project_id=project_id, knowledge_id=knowledge_id),
                          {"content": "", "provenance": None})


@mcp.tool()
async def submit_learning(project_id: str, workflow_id: str | None, agent_name: str,
                          knowledge_type: KnowledgeType, content: str,
                          source_reference: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Submit canonical learning through sanitization, policy, deduplication, embedding, and indexing."""
    request = SubmitLearningRequest(
        project_id=project_id, workflow_id=workflow_id, agent_name=agent_name,
        knowledge_type=knowledge_type, content=content, source_reference=source_reference,
        metadata=metadata or {},
    )
    service = await get_service()
    async with knowledge_trace(service, "submit_learning", project_id):
        return await service.submit_learning(request)


@mcp.tool()
async def get_learning_status(knowledge_id: str, project_id: str) -> dict[str, Any]:
    """Read the durable lifecycle status for one project-local learning."""
    service = await get_service()
    async with knowledge_trace(service, "get_learning_status", project_id):
        return await service.get_learning_status(project_id=project_id, knowledge_id=knowledge_id)


if __name__ == "__main__":
    mcp.run(transport="stdio")
