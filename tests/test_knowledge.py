from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.knowledge_models import SubmitLearningRequest
from api.services.knowledge_providers import (
    DeterministicEmbeddingProvider, EmbeddingEstimate, EmbeddingResult, SQLiteVectorStore,
)
from api.services.knowledge_service import (
    KnowledgeConflictError, KnowledgeNotFoundError, KnowledgePolicy, KnowledgeService,
    KnowledgeValidationError,
)
from api.services.knowledge_store import KnowledgeStore
from api.services.observability_context import ObservabilityContext, observability_context
from servers import knowledge_server


class FakeTelemetry:
    def __init__(self) -> None:
        self.spans: list[str] = []; self.metrics: list[str] = []

    @asynccontextmanager
    async def span(self, name, **_):
        self.spans.append(name)
        yield

    async def record_metric(self, name, *_):
        self.metrics.append(name)


class FakeFinOps:
    def __init__(self) -> None:
        self.before: list[dict] = []; self.after: list[dict] = []

    async def before_embedding(self, **kwargs):
        self.before.append(kwargs)
        return {"decision": "allow"}

    async def after_embedding(self, **kwargs):
        self.after.append(kwargs)


class FailingEmbeddingProvider(DeterministicEmbeddingProvider):
    async def embed(self, text: str) -> EmbeddingResult:
        raise RuntimeError("embedding failed")


class FailingVectorStore(SQLiteVectorStore):
    async def upsert(self, **kwargs):
        raise RuntimeError("vector write failed")


class CountingEmbeddingProvider(DeterministicEmbeddingProvider):
    def __init__(self):
        super().__init__(); self.calls = 0

    async def embed(self, text: str) -> EmbeddingResult:
        self.calls += 1
        return await super().embed(text)


class BlockingFinOps(FakeFinOps):
    async def before_embedding(self, **kwargs):
        self.before.append(kwargs)
        raise RuntimeError("budget blocked")


async def make_service(tmp_path: Path, *, policy: KnowledgePolicy | None = None,
                       provider=None, vector=None, telemetry=None, finops=None) -> KnowledgeService:
    database = tmp_path / "knowledge.sqlite"
    store = KnowledgeStore(database)
    service = KnowledgeService(
        store, policy=policy, embedding_provider=provider,
        vector_store=vector or SQLiteVectorStore(database), observability=telemetry, finops=finops,
        chunk_size=200,
    )
    await service.initialize()
    return service


def payload(**updates):
    values = {
        "project_id": "phase-7-validation", "workflow_id": "manual-knowledge-validation",
        "agent_name": "Developer", "knowledge_type": "workflow_learning",
        "content": "FastAPI integration tests require the application environment to be prepared before execution.",
        "source_reference": "workflow:manual-knowledge-validation", "metadata": {"framework": "fastapi"},
    }
    values.update(updates)
    return SubmitLearningRequest.model_validate(values)


@pytest.mark.asyncio
async def test_candidate_approval_pipeline_and_project_isolated_retrieval(tmp_path):
    service = await make_service(tmp_path)
    submitted = await service.submit_learning(payload())
    assert submitted["status"] == "candidate" and submitted["chunk_count"] == 0
    assert await service.store.chunks(submitted["knowledge_id"]) == []

    indexed = await service.approve(submitted["knowledge_id"])
    assert indexed["status"] == "indexed"
    assert indexed["chunk_count"] > 0 and indexed["embedding_count"] == indexed["vector_count"]

    found = await service.search_knowledge(
        query="How should integration tests prepare the environment?", project_id="phase-7-validation",
    )
    assert found["retrieved_items"] >= 1
    provenance = found["results"][0]["provenance"]
    assert provenance["knowledge_id"] == submitted["knowledge_id"]
    assert provenance["project_id"] == "phase-7-validation"
    assert {"source_reference", "knowledge_type", "workflow_id", "created_at", "version", "retrieval_score"} <= provenance.keys()
    isolated = await service.search_knowledge(query="integration tests environment", project_id="another-project")
    assert isolated["results"] == []


@pytest.mark.asyncio
async def test_retrieval_detail_persists_rank_score_provenance_filters_and_correlation(tmp_path):
    finops = FakeFinOps()
    service = await make_service(tmp_path, finops=finops)
    first = await service.submit_learning(payload(
        knowledge_type="documentation", source_reference="docs:migrations",
        content="Integration tests should run database migrations before assertions.",
    ))
    second = await service.submit_learning(payload(
        knowledge_type="documentation", source_reference="docs:test-database",
        content="Database integration tests prepare migrations and isolate the test database.",
    ))
    context = ObservabilityContext(
        trace_id="a" * 32, span_id="b" * 16, workflow_id="workflow-retrieval",
        agent="QA",
    )
    with observability_context(context):
        response = await service.search_knowledge(
            query="How should integration tests handle database migrations?",
            project_id="phase-7-validation", knowledge_types=["documentation"], limit=2,
        )

    assert response["retrieval_id"]
    detail = await service.store.retrieval(response["retrieval_id"])
    assert detail is not None
    assert detail["detail_state"] == "complete"
    assert detail["query"] == "How should integration tests handle database migrations?"
    assert detail["top_k"] == 2
    assert detail["filters"] == {"knowledge_types": ["documentation"]}
    assert detail["trace_id"] == context.trace_id and detail["span_id"] == context.span_id
    assert detail["workflow_id"] == context.workflow_id and detail["agent_name"] == "QA"
    assert detail["finops_call_ids"] == [finops.before[-1]["call_id"]]
    assert detail["result_count"] == len(detail["results"]) == 2
    assert [item["rank"] for item in detail["results"]] == [1, 2]
    assert {item["knowledge_id"] for item in detail["results"]} == {
        first["knowledge_id"], second["knowledge_id"],
    }
    for item, returned in zip(detail["results"], response["results"], strict=True):
        assert item["chunk_id"] == returned["chunk_id"]
        assert item["project_id"] == detail["project_id"]
        assert item["source_reference"].startswith("docs:")
        assert item["knowledge_type"] == "documentation" and item["version"] == 1
        assert item["retrieval_score"] == pytest.approx(returned["provenance"]["retrieval_score"], abs=1e-8)
    serialized = str(detail).casefold()
    assert "vector_json" not in serialized and "raw_embedding" not in serialized


@pytest.mark.asyncio
async def test_empty_retrieval_is_complete_and_persisted_without_results(tmp_path):
    service = await make_service(tmp_path)
    response = await service.search_knowledge(
        query="How should integration tests handle database migrations?",
        project_id="phase-7-other-project",
    )
    detail = await service.store.retrieval(response["retrieval_id"])
    assert response["results"] == []
    assert detail is not None and detail["result_count"] == 0
    assert detail["results"] == [] and detail["detail_state"] == "complete"
    assert any(item["retrieval_id"] == response["retrieval_id"] for item in await service.store.retrievals())


@pytest.mark.asyncio
async def test_retrieval_project_invariant_rolls_back_cross_project_results(tmp_path):
    service = await make_service(tmp_path)
    foreign = await service.submit_learning(payload(
        project_id="foreign-project", workflow_id="foreign-workflow",
        knowledge_type="documentation", source_reference="docs:foreign",
    ))
    chunks = await service.store.chunks(foreign["knowledge_id"])
    retrieval_id = "cross-project-retrieval"
    with pytest.raises(ValueError, match="retrieval_project_mismatch"):
        await service.store.add_retrieval({
            "retrieval_id": retrieval_id, "project_id": "phase-7-validation",
            "operation": "knowledge.search", "query_hash": "hash", "query": "query",
            "result_count": 1, "latency_ms": 1, "detail_state": "complete",
        }, [{
            "rank": 1, "knowledge_id": foreign["knowledge_id"],
            "chunk_id": chunks[0]["chunk_id"], "project_id": "phase-7-validation",
            "retrieval_score": .5,
        }])
    assert await service.store.retrieval(retrieval_id) is None


@pytest.mark.asyncio
async def test_retrieval_restart_persistence_and_independent_ids(tmp_path):
    service = await make_service(tmp_path)
    await service.submit_learning(payload(knowledge_type="documentation", source_reference="docs:restart"))
    first = await service.search_knowledge(query="integration environment", project_id="phase-7-validation")
    second = await service.search_knowledge(query="integration environment", project_id="phase-7-validation")
    assert first["retrieval_id"] != second["retrieval_id"]

    restarted = KnowledgeStore(service.store.database_path)
    await restarted.initialize()
    restored = await restarted.retrieval(first["retrieval_id"])
    assert restored is not None and restored["detail_state"] == "complete"
    assert restored["results"][0]["knowledge_id"] == first["results"][0]["provenance"]["knowledge_id"]


@pytest.mark.asyncio
async def test_historical_retrievals_are_backfilled_only_with_durable_evidence(tmp_path):
    database = tmp_path / "historical.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE knowledge_retrievals (
            retrieval_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, operation TEXT NOT NULL,
            query_hash TEXT NOT NULL, query_preview TEXT, result_count INTEGER NOT NULL,
            latency_ms REAL NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        )""")
        connection.execute("INSERT INTO knowledge_retrievals VALUES(?,?,?,?,?,?,?,?,?)",
                           ("historical-hit", "project-a", "knowledge.search", "h1", "query", 1, 3.2, "{}", "2026-01-01T00:00:00Z"))
        connection.execute("INSERT INTO knowledge_retrievals VALUES(?,?,?,?,?,?,?,?,?)",
                           ("historical-miss", "project-b", "knowledge.search", "h2", "query", 0, 1.2, "{}", "2026-01-01T00:00:01Z"))
    store = KnowledgeStore(database)
    await store.initialize()
    hit = await store.retrieval("historical-hit")
    miss = await store.retrieval("historical-miss")
    assert hit is not None and hit["detail_state"] == "summary_only" and hit["results"] == []
    assert miss is not None and miss["detail_state"] == "complete" and miss["results"] == []


@pytest.mark.asyncio
async def test_low_risk_learning_auto_validates_and_exact_submission_is_idempotent(tmp_path):
    service = await make_service(tmp_path)
    request = payload(knowledge_type="documentation", source_reference="docs:testing")
    first = await service.submit_learning(request); second = await service.submit_learning(request)
    assert first["status"] == "indexed"
    assert second["knowledge_id"] == first["knowledge_id"] and second["duplicate"] is True
    assert len(await service.store.list(project_id="phase-7-validation")) == 1


@pytest.mark.asyncio
async def test_concurrent_exact_deduplication_creates_one_canonical_record(tmp_path):
    service = await make_service(tmp_path)
    results = await asyncio.gather(*(service.submit_learning(payload()) for _ in range(8)))
    assert len({item["knowledge_id"] for item in results}) == 1
    assert sum(item["created"] for item in results) == 1
    assert len(await service.store.list()) == 1


@pytest.mark.asyncio
async def test_near_duplicate_is_candidate_and_not_indexed_automatically(tmp_path):
    service = await make_service(tmp_path, policy=KnowledgePolicy({"documentation"}))
    first = await service.submit_learning(payload(knowledge_type="documentation", source_reference="docs:a"))
    second = await service.submit_learning(payload(
        knowledge_type="documentation", source_reference="docs:b",
        content="FastAPI integration tests require the application environment to be prepared before execution!",
    ))
    assert first["status"] == "indexed"
    assert second["status"] == "candidate" and second["duplicate_candidate"] is True
    assert second["vector_count"] == 0
    with pytest.raises(KnowledgeConflictError, match="duplicate candidate"):
        await service.approve(second["knowledge_id"])


@pytest.mark.asyncio
async def test_secrets_are_rejected_without_persisting_secret_material(tmp_path):
    service = await make_service(tmp_path)
    secret = "sk-super-secret-value-1234567890"
    result = await service.submit_learning(payload(
        content=f"Authorization: Bearer {secret}", metadata={"password": "do-not-store"},
    ))
    assert result["status"] == "rejected" and result["chunk_count"] == 0
    database_bytes = service.store.database_path.read_bytes()
    assert secret.encode() not in database_bytes and b"do-not-store" not in database_bytes
    with sqlite3.connect(service.store.database_path) as connection:
        audit = connection.execute("SELECT reason,metadata_json FROM knowledge_audit").fetchone()
    assert audit[0] == "sensitive_content_detected" and secret not in audit[1]


@pytest.mark.asyncio
async def test_validation_rejects_oversize_and_non_serializable_metadata(tmp_path):
    service = await make_service(tmp_path)
    service.max_content_size = 10
    with pytest.raises(KnowledgeValidationError, match="maximum size"):
        await service.submit_learning(payload(content="content longer than ten bytes"))
    service.max_content_size = 1000
    with pytest.raises(KnowledgeValidationError, match="JSON serializable"):
        await service.submit_learning(payload(metadata={"bad": object()}))
    assert await service.store.list() == []


@pytest.mark.asyncio
async def test_classification_is_durable_and_not_blindly_agent_controlled(tmp_path):
    service = await make_service(tmp_path)
    result = await service.submit_learning(payload(
        knowledge_type="documentation", content="Decision: use a service boundary for persistence.",
        source_reference="adr:1",
    ))
    assert result["requested_type"] == "documentation"
    assert result["knowledge_type"] == "decision"
    assert result["classification_confidence"] > .9 and result["status"] == "candidate"


@pytest.mark.asyncio
async def test_rejection_preserves_audit_and_never_indexes(tmp_path):
    service = await make_service(tmp_path)
    candidate = await service.submit_learning(payload())
    rejected = await service.reject(candidate["knowledge_id"], "Not reusable")
    assert rejected["status"] == "rejected" and rejected["rejection_reason"] == "Not reusable"
    assert rejected["chunk_count"] == rejected["vector_count"] == 0
    with pytest.raises(KnowledgeConflictError):
        await service.approve(candidate["knowledge_id"])


@pytest.mark.asyncio
async def test_failed_embedding_does_not_mark_knowledge_indexed(tmp_path):
    service = await make_service(tmp_path, provider=FailingEmbeddingProvider())
    candidate = await service.submit_learning(payload())
    with pytest.raises(RuntimeError, match="embedding failed"):
        await service.approve(candidate["knowledge_id"])
    current = await service.store.detail(candidate["knowledge_id"])
    assert current["status"] == "validated" and current["vector_count"] == 0


@pytest.mark.asyncio
async def test_failed_vector_write_does_not_mark_knowledge_indexed(tmp_path):
    database = tmp_path / "vector-failure.sqlite"
    service = KnowledgeService(KnowledgeStore(database), vector_store=FailingVectorStore(database))
    await service.initialize()
    candidate = await service.submit_learning(payload())
    with pytest.raises(RuntimeError, match="vector write failed"):
        await service.approve(candidate["knowledge_id"])
    current = await service.store.detail(candidate["knowledge_id"])
    assert current["status"] == "validated" and current["vector_count"] == 0


@pytest.mark.asyncio
async def test_reindex_is_idempotent(tmp_path):
    service = await make_service(tmp_path)
    candidate = await service.submit_learning(payload()); first = await service.approve(candidate["knowledge_id"])
    second = await service.reindex(candidate["knowledge_id"]); third = await service.reindex(candidate["knowledge_id"])
    assert first["chunk_count"] == second["chunk_count"] == third["chunk_count"]
    assert second["embedding_count"] == second["vector_count"] == second["chunk_count"]
    assert third["embedding_count"] == third["vector_count"] == third["chunk_count"]


@pytest.mark.asyncio
async def test_retrieval_audit_does_not_block_reindex_or_lose_historical_chunk_id(tmp_path):
    service = await make_service(tmp_path)
    learning = await service.submit_learning(payload(
        knowledge_type="documentation", source_reference="docs:reindex-after-retrieval",
    ))
    searched = await service.search_knowledge(
        query="integration tests environment", project_id="phase-7-validation",
    )
    before = await service.store.retrieval(searched["retrieval_id"])
    historical_chunk_id = before["results"][0]["chunk_id"]

    reindexed = await service.reindex(learning["knowledge_id"])
    after = await service.store.retrieval(searched["retrieval_id"])

    assert reindexed["status"] == "indexed" and reindexed["vector_count"] > 0
    assert after["results"][0]["chunk_id"] == historical_chunk_id
    assert after["results"][0]["knowledge_id"] == learning["knowledge_id"]


@pytest.mark.asyncio
async def test_observability_and_finops_wrap_pipeline_without_content(tmp_path):
    telemetry = FakeTelemetry(); finops = FakeFinOps()
    service = await make_service(tmp_path, telemetry=telemetry, finops=finops)
    candidate = await service.submit_learning(payload()); await service.approve(candidate["knowledge_id"])
    await service.search_knowledge(query="prepare environment", project_id="phase-7-validation")
    assert {"knowledge.submit", "knowledge.validate", "knowledge.deduplicate", "knowledge.chunk",
            "knowledge.embed", "knowledge.index", "knowledge.search"} <= set(telemetry.spans)
    assert {"write_pipeline_duration", "chunks_created", "indexed_learnings",
            "retrieval_latency", "retrieved_items"} <= set(telemetry.metrics)
    assert len(finops.before) == len(finops.after) >= 2
    assert all(call["estimate"].provider == "local" for call in finops.before)
    assert all(call["operation"] == "knowledge_embedding" for call in finops.before)
    assert not any("content" in call for call in finops.before)


@pytest.mark.asyncio
async def test_finops_block_happens_before_embedding_provider_call(tmp_path):
    provider = CountingEmbeddingProvider(); finops = BlockingFinOps()
    service = await make_service(tmp_path, provider=provider, finops=finops)
    candidate = await service.submit_learning(payload())
    with pytest.raises(RuntimeError, match="budget blocked"):
        await service.approve(candidate["knowledge_id"])
    assert provider.calls == 0 and len(finops.before) == 1
    current = await service.get_learning_status(project_id="phase-7-validation", knowledge_id=candidate["knowledge_id"])
    assert current["status"] == "validated" and current["vector_count"] == 0


@pytest.mark.asyncio
async def test_read_helpers_and_source_enforce_project_boundary(tmp_path):
    service = await make_service(tmp_path)
    decision = await service.submit_learning(payload(
        content="Decision: prepare the environment before integration tests.", source_reference="adr:test-env",
    ))
    await service.approve(decision["knowledge_id"])
    assert (await service.get_project_decisions(project_id="phase-7-validation"))["retrieved_items"] == 1
    context = await service.get_relevant_context(query="integration environment", project_id="phase-7-validation")
    assert "prepare the environment" in context["context"] and context["sources"]
    source = await service.get_knowledge_source(project_id="phase-7-validation", knowledge_id=decision["knowledge_id"])
    assert source["provenance"]["retrieval_score"] == 1.0
    with pytest.raises(KnowledgeNotFoundError):
        await service.get_knowledge_source(project_id="other", knowledge_id=decision["knowledge_id"])


@pytest.mark.asyncio
async def test_mcp_contract_exposes_only_controlled_read_write_tools():
    tools = await knowledge_server.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert names == {"search_knowledge", "get_relevant_context", "get_project_decisions",
                     "get_similar_implementations", "get_knowledge_source", "submit_learning",
                     "get_learning_status"}
    assert not names & {"insert_embedding", "insert_vector", "delete_vector", "raw_vector_search",
                        "save_embedding", "write_chunk"}
    submit = next(tool for tool in tools if tool.name == "submit_learning")
    assert "embeddings" not in submit.inputSchema.get("properties", {})
    relevant = next(tool for tool in tools if tool.name == "get_relevant_context")
    assert {
        "query",
        "project_id",
        "knowledge_types",
        "limit",
        "workflow_id",
        "agent_name",
        "trace_id",
        "parent_span_id",
    } <= relevant.inputSchema.get("properties", {}).keys()


@pytest.mark.asyncio
async def test_read_policy_is_fail_open_and_configurably_fail_closed(tmp_path, monkeypatch):
    service = await make_service(tmp_path)

    async def unavailable():
        raise RuntimeError("vector backend unavailable")

    monkeypatch.setenv("KNOWLEDGE_READ_FAIL_MODE", "open")
    degraded = await knowledge_server.run_read(
        service, "search_knowledge", "project-a", unavailable,
        {"results": [], "retrieved_items": 0},
    )
    assert degraded["degraded"] is True and degraded["results"] == []
    assert degraded["failure_type"] == "knowledge_unavailable"

    monkeypatch.setenv("KNOWLEDGE_READ_FAIL_MODE", "closed")
    with pytest.raises(RuntimeError, match="vector backend unavailable"):
        await knowledge_server.run_read(
            service, "search_knowledge", "project-a", unavailable,
            {"results": [], "retrieved_items": 0},
        )


def test_agents_do_not_import_embedding_or_vector_store_implementations():
    root = Path(__file__).resolve().parents[1]
    agent_sources = [root / "host.py", *sorted((root / "graph").glob("*.py"))]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in agent_sources)
    assert "knowledge_providers" not in combined
    assert "SQLiteVectorStore" not in combined
    assert "DeterministicEmbeddingProvider" not in combined


@pytest.mark.asyncio
async def test_admin_api_lists_candidates_approves_and_reads_chunks(tmp_path):
    service = await make_service(tmp_path)
    candidate = await service.submit_learning(payload())

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(knowledge=service, observability=None)

    with TestClient(create_app(factory)) as client:
        listed = client.get("/api/knowledge/candidates")
        assert listed.status_code == 200 and listed.json()["items"][0]["knowledge_id"] == candidate["knowledge_id"]
        approved = client.post(f"/api/knowledge/{candidate['knowledge_id']}/approve")
        assert approved.status_code == 200 and approved.json()["status"] == "indexed"
        detail = client.get(f"/api/knowledge/{candidate['knowledge_id']}")
        chunks = client.get(f"/api/knowledge/{candidate['knowledge_id']}/chunks")
        assert detail.json()["vector_count"] > 0 and chunks.json()["count"] > 0
        assert client.post("/api/knowledge/missing/reindex").status_code == 404


@pytest.mark.asyncio
async def test_retrieval_detail_api_is_read_only_and_returns_provenance(tmp_path):
    service = await make_service(tmp_path)
    learning = await service.submit_learning(payload(
        knowledge_type="documentation", source_reference="docs:api-retrieval",
    ))
    searched = await service.search_knowledge(
        query="integration tests environment", project_id="phase-7-validation",
    )

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(knowledge=service, observability=None)

    with TestClient(create_app(factory)) as client:
        before = len(await service.store.retrievals())
        first = client.get(f"/api/knowledge/retrievals/{searched['retrieval_id']}")
        second = client.get(f"/api/knowledge/retrievals/{searched['retrieval_id']}")
        missing = client.get("/api/knowledge/retrievals/missing")
        after = len(await service.store.retrievals())

    assert first.status_code == second.status_code == 200 and missing.status_code == 404
    assert before == after == 1
    body = first.json()
    assert body["results"][0]["knowledge_id"] == learning["knowledge_id"]
    assert body["results"][0]["rank"] == 1 and body["results"][0]["retrieval_score"] > 0
    assert body["detail_state"] == "complete"
    assert "vector" not in body["results"][0] and "embedding" not in body["results"][0]
