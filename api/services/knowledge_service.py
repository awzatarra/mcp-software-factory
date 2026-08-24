from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
import re
from time import perf_counter
from typing import Any, AsyncIterator
from uuid import uuid4

from api.knowledge_models import KnowledgeStatus, KnowledgeType, SubmitLearningRequest
from api.services.knowledge_providers import (
    DeterministicEmbeddingProvider, EmbeddingProvider, EmbeddingResult,
    KnowledgeFinOps, NoopKnowledgeFinOps, SQLiteVectorStore, VectorStore,
)
from api.services.knowledge_store import KnowledgeStore
from api.services.observability_context import get_observability_context


class KnowledgeError(RuntimeError):
    code = "knowledge_error"


class KnowledgeNotFoundError(KnowledgeError):
    code = "knowledge_not_found"


class KnowledgeConflictError(KnowledgeError):
    code = "knowledge_state_conflict"


class KnowledgeValidationError(KnowledgeError):
    code = "knowledge_validation_failed"


SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    "authorization_header": re.compile(r"authorization\s*:\s*(?:bearer|basic)\s+\S+", re.I),
    "api_key": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b", re.I),
    "credential_assignment": re.compile(r"\b(?:api[_-]?key|access[_-]?token|secret(?:[_-]?key)?|password|passwd|client[_-]?secret)\s*[=:]\s*[^\s,;]+", re.I),
    "sensitive_environment": re.compile(r"\b[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|SECRET|PASSWORD|PRIVATE_KEY)[A-Z0-9_]*\s*=\s*\S+", re.I),
    "credential_connection_string": re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/]+:[^\s@]+@[^\s]+", re.I),
}
SENSITIVE_METADATA_KEYS = re.compile(r"(?:api[_-]?key|access[_-]?token|authorization|secret|password|private[_-]?key|connection[_-]?string)", re.I)


def normalized_content(content: str) -> str:
    return " ".join(content.split()).casefold()


def content_hash(content: str) -> str:
    return hashlib.sha256(normalized_content(content).encode("utf-8")).hexdigest()


def sanitize_text(value: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    sanitized = value
    for reason, pattern in SECRET_PATTERNS.items():
        if pattern.search(sanitized):
            reasons.append(reason)
            sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized, reasons


def sanitize_metadata(value: Any, path: str = "metadata") -> tuple[Any, list[str]]:
    reasons: list[str] = []
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if SENSITIVE_METADATA_KEYS.search(str(key)):
                result[str(key)] = "[REDACTED]"
                reasons.append(f"sensitive_metadata_key:{path}.{key}")
            else:
                cleaned, nested = sanitize_metadata(item, f"{path}.{key}")
                result[str(key)] = cleaned; reasons.extend(nested)
        return result, reasons
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            cleaned, nested = sanitize_metadata(item, f"{path}[{index}]")
            result.append(cleaned); reasons.extend(nested)
        return result, reasons
    if isinstance(value, str):
        cleaned, nested = sanitize_text(value)
        return cleaned, [f"{reason}:{path}" for reason in nested]
    return value, reasons


def classify_knowledge(requested: str, content: str) -> tuple[str, float, str]:
    normalized = normalized_content(content)
    if normalized.startswith(("decision:", "adr:")) or "we decided to" in normalized:
        return KnowledgeType.DECISION.value, 0.93, "deterministic_decision_marker"
    if normalized.startswith("architecture:") or "architecture decision record" in normalized:
        return KnowledgeType.ARCHITECTURE.value, 0.91, "deterministic_architecture_marker"
    return requested, 0.75, "validated_requested_type"


def lexical_similarity(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[a-z0-9_]+", normalized_content(left)))
    right_tokens = set(re.findall(r"[a-z0-9_]+", normalized_content(right)))
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union) if union else 1.0


class KnowledgePolicy:
    def __init__(self, auto_validate_types: set[str] | None = None) -> None:
        configured = os.getenv("KNOWLEDGE_AUTO_VALIDATE_TYPES", "documentation,code_pattern,test_pattern")
        self.auto_validate_types = auto_validate_types if auto_validate_types is not None else {
            item.strip() for item in configured.split(",") if item.strip()
        }

    def requires_approval(self, knowledge_type: str) -> bool:
        return knowledge_type not in self.auto_validate_types


class KnowledgeService:
    def __init__(self, store: KnowledgeStore, *, embedding_provider: EmbeddingProvider | None = None,
                 vector_store: VectorStore | None = None, policy: KnowledgePolicy | None = None,
                 observability: Any | None = None, finops: KnowledgeFinOps | None = None,
                 max_content_size: int | None = None, chunk_size: int | None = None) -> None:
        self.store = store
        self.embedding_provider = embedding_provider or DeterministicEmbeddingProvider()
        self.vector_store = vector_store or SQLiteVectorStore(store.database_path)
        self.policy = policy or KnowledgePolicy()
        self.observability = observability
        self.finops = finops or NoopKnowledgeFinOps()
        self.max_content_size = max_content_size or int(os.getenv("KNOWLEDGE_MAX_CONTENT_SIZE", "100000"))
        self.chunk_size = max(200, chunk_size or int(os.getenv("KNOWLEDGE_CHUNK_SIZE", "1200")))
        self.near_duplicate_threshold = float(os.getenv("KNOWLEDGE_NEAR_DUPLICATE_THRESHOLD", "0.92"))

    async def initialize(self) -> None:
        await self.store.initialize()
        await self.vector_store.initialize()

    @asynccontextmanager
    async def _span(self, name: str, **attributes: Any) -> AsyncIterator[Any]:
        if self.observability is None or not hasattr(self.observability, "span"):
            yield get_observability_context(); return
        async with self.observability.span(name, category="knowledge", attributes=attributes) as context:
            yield context

    async def _metric(self, name: str, value: float, unit: str, **attributes: Any) -> None:
        if self.observability is None:
            return
        if hasattr(self.observability, "record_metric"):
            await self.observability.record_metric(name, value, unit, attributes)
            return
        store = getattr(self.observability, "store", None)
        if store is not None and hasattr(store, "add_metric"):
            record = {"name": name, "value": value, "unit": unit,
                      "timestamp": datetime.now(UTC).isoformat(), "attributes": attributes}
            context = get_observability_context()
            record["trace_id"] = context.trace_id
            record["workflow_id"] = context.workflow_id
            if hasattr(self.observability, "_safe"):
                await self.observability._safe(store.add_metric, record)
                return
            action = store.add_metric(record)
            if hasattr(action, "__await__"):
                await action

    @staticmethod
    def _chunk(record: dict[str, Any], chunk_size: int) -> list[dict[str, Any]]:
        words = record["content"].split()
        parts: list[str] = []; current: list[str] = []; current_size = 0
        for word in words:
            projected = current_size + len(word) + (1 if current else 0)
            if current and projected > chunk_size:
                parts.append(" ".join(current)); current = [word]; current_size = len(word)
            else:
                current.append(word); current_size = projected
        if current:
            parts.append(" ".join(current))
        timestamp = datetime.now(UTC).isoformat()
        return [{
            "chunk_id": uuid4().hex, "project_id": record["project_id"],
            "knowledge_type": record["knowledge_type"], "source_reference": record["source_reference"],
            "chunk_index": index, "content": part, "content_hash": content_hash(part),
            "metadata": record.get("metadata") or {}, "created_at": timestamp,
        } for index, part in enumerate(parts)]

    async def submit_learning(self, payload: SubmitLearningRequest | dict[str, Any]) -> dict[str, Any]:
        request = payload if isinstance(payload, SubmitLearningRequest) else SubmitLearningRequest.model_validate(payload)
        started = perf_counter()
        project_id = request.project_id.strip(); content = request.content.strip(); source = request.source_reference.strip()
        if not project_id or not content or not source:
            raise KnowledgeValidationError("project_id, content and source_reference are required")
        try:
            json.dumps(request.metadata, default=lambda _: (_ for _ in ()).throw(TypeError()))
        except (TypeError, ValueError) as exc:
            raise KnowledgeValidationError("metadata must be JSON serializable") from exc
        sanitized_project, project_reasons = sanitize_text(project_id)
        sanitized_workflow, workflow_reasons = sanitize_text(request.workflow_id or "")
        sanitized_agent, agent_reasons = sanitize_text(request.agent_name.strip())
        sanitized_content, content_reasons = sanitize_text(content)
        sanitized_source, source_reasons = sanitize_text(source)
        sanitized_metadata, metadata_reasons = sanitize_metadata(request.metadata)
        secret_reasons = sorted(set(
            [f"{reason}:project_id" for reason in project_reasons]
            + [f"{reason}:workflow_id" for reason in workflow_reasons]
            + [f"{reason}:agent_name" for reason in agent_reasons]
            + content_reasons + source_reasons + metadata_reasons
        ))
        safe_workflow = sanitized_workflow or None
        if len(content.encode("utf-8")) > self.max_content_size:
            await self.store.add_audit(knowledge_id=None, project_id=sanitized_project, workflow_id=safe_workflow,
                                       action="knowledge.submit", outcome="rejected", reason="content_too_large",
                                       metadata={"size_bytes": len(content.encode("utf-8"))})
            await self._metric("rejected_learnings", 1, "count", reason="content_too_large")
            raise KnowledgeValidationError("content exceeds configured maximum size")

        async with self._span("knowledge.submit", project_id=sanitized_project, workflow_id=safe_workflow,
                              agent_name=sanitized_agent, knowledge_type=request.knowledge_type.value):
            original_hash = content_hash(content)
            classified_type, confidence, classification_reason = classify_knowledge(request.knowledge_type.value, sanitized_content)

            async with self._span("knowledge.validate", project_id=sanitized_project, knowledge_type=classified_type):
                rejection_reason = "sensitive_content_detected" if secret_reasons else None
            async with self._span("knowledge.deduplicate", project_id=sanitized_project):
                candidates = [] if secret_reasons else await self.store.possible_duplicates(sanitized_project, classified_type, original_hash)
                duplicate_of = next((item["knowledge_id"] for item in candidates
                                     if lexical_similarity(sanitized_content, item["content"]) >= self.near_duplicate_threshold), None)
            record, created = await self.store.create_or_get({
                "project_id": sanitized_project, "workflow_id": safe_workflow,
                "agent_name": sanitized_agent, "requested_type": request.knowledge_type.value,
                "knowledge_type": classified_type, "classification_confidence": confidence,
                "content": "[REDACTED: sensitive learning rejected]" if secret_reasons else sanitized_content,
                "content_hash": original_hash, "source_reference": sanitized_source,
                "metadata": sanitized_metadata, "status": KnowledgeStatus.REJECTED.value if secret_reasons else KnowledgeStatus.CANDIDATE.value,
                "rejection_reason": rejection_reason, "duplicate_of": duplicate_of,
                "duplicate_candidate": duplicate_of is not None,
            })
            if not created:
                await self.store.add_audit(knowledge_id=record["knowledge_id"], project_id=sanitized_project,
                                           workflow_id=safe_workflow, action="knowledge.deduplicate",
                                           outcome="duplicate", reason="exact_duplicate", metadata={})
                await self._metric("duplicates_detected", 1, "count", kind="exact")
                return {**await self._detail_or_raise(record["knowledge_id"]), "created": False,
                        "duplicate": True, "duplicate_reason": "exact_duplicate"}

            await self.store.add_audit(
                knowledge_id=record["knowledge_id"], project_id=sanitized_project, workflow_id=safe_workflow,
                action="knowledge.submit", outcome=record["status"], reason=rejection_reason,
                metadata={"classification_reason": classification_reason,
                          "classification_confidence": confidence, "secret_reason_codes": secret_reasons},
            )
            if secret_reasons:
                await self._metric("rejected_learnings", 1, "count", reason="sensitive_content_detected")
            elif duplicate_of:
                await self._metric("duplicates_detected", 1, "count", kind="near_duplicate")
            elif not self.policy.requires_approval(classified_type):
                record = await self.approve(record["knowledge_id"], actor="knowledge_policy")
            else:
                record = await self._detail_or_raise(record["knowledge_id"])
            if secret_reasons or duplicate_of:
                record = await self._detail_or_raise(record["knowledge_id"])
            duration = (perf_counter() - started) * 1000
            await self._metric("write_pipeline_duration", duration, "ms", status=record["status"])
            return {**record, "created": True, "duplicate": False,
                    "requires_approval": record["status"] == KnowledgeStatus.CANDIDATE.value}

    async def _detail_or_raise(self, knowledge_id: str) -> dict[str, Any]:
        record = await self.store.detail(knowledge_id)
        if record is None:
            raise KnowledgeNotFoundError(knowledge_id)
        return record

    async def approve(self, knowledge_id: str, *, actor: str = "admin") -> dict[str, Any]:
        record = await self._detail_or_raise(knowledge_id)
        if record["status"] == KnowledgeStatus.INDEXED.value:
            return record
        if record["status"] != KnowledgeStatus.CANDIDATE.value:
            raise KnowledgeConflictError(f"cannot approve knowledge in status {record['status']}")
        if record.get("duplicate_candidate"):
            raise KnowledgeConflictError("duplicate candidate requires resolution before indexing")
        await self.store.update_status(knowledge_id, KnowledgeStatus.VALIDATED.value)
        await self.store.add_audit(knowledge_id=knowledge_id, project_id=record["project_id"],
                                   workflow_id=record.get("workflow_id"), action="knowledge.validate",
                                   outcome="validated", metadata={"actor": actor})
        return await self._index(knowledge_id)

    async def reject(self, knowledge_id: str, reason: str, *, actor: str = "admin") -> dict[str, Any]:
        record = await self._detail_or_raise(knowledge_id)
        if record["status"] in {KnowledgeStatus.INDEXED.value, KnowledgeStatus.SUPERSEDED.value,
                                KnowledgeStatus.ARCHIVED.value}:
            raise KnowledgeConflictError(f"cannot reject knowledge in status {record['status']}")
        await self.vector_store.delete_knowledge(knowledge_id)
        await self.store.clear_representations(knowledge_id)
        updated = await self.store.update_status(knowledge_id, KnowledgeStatus.REJECTED.value, reason=reason)
        await self.store.add_audit(knowledge_id=knowledge_id, project_id=record["project_id"],
                                   workflow_id=record.get("workflow_id"), action="knowledge.reject",
                                   outcome="rejected", reason=reason, metadata={"actor": actor})
        await self._metric("rejected_learnings", 1, "count", reason="administrative_rejection")
        return await self._detail_or_raise(knowledge_id) if updated else record

    async def reindex(self, knowledge_id: str) -> dict[str, Any]:
        record = await self._detail_or_raise(knowledge_id)
        if record["status"] not in {KnowledgeStatus.INDEXED.value, KnowledgeStatus.VALIDATED.value}:
            raise KnowledgeConflictError(f"cannot reindex knowledge in status {record['status']}")
        await self.store.update_status(knowledge_id, KnowledgeStatus.VALIDATED.value)
        return await self._index(knowledge_id)

    async def _index(self, knowledge_id: str) -> dict[str, Any]:
        record = await self._detail_or_raise(knowledge_id)
        if record["status"] != KnowledgeStatus.VALIDATED.value:
            raise KnowledgeConflictError("only validated knowledge can be indexed")
        try:
            async with self._span("knowledge.chunk", knowledge_id=knowledge_id, project_id=record["project_id"]):
                chunks = self._chunk(record, self.chunk_size)
                await self.vector_store.delete_knowledge(knowledge_id)
                await self.store.replace_chunks(knowledge_id, chunks)
                await self._metric("chunks_created", len(chunks), "count", knowledge_id=knowledge_id)
            for chunk in chunks:
                estimate = self.embedding_provider.estimate(chunk["content"])
                call_id = uuid4().hex
                await self.finops.before_embedding(call_id=call_id, project_id=record["project_id"],
                                                   workflow_id=record.get("workflow_id"), agent_name=record["agent_name"],
                                                   operation="knowledge_embedding",
                                                   estimate=estimate)
                result: EmbeddingResult | None = None
                try:
                    async with self._span("knowledge.embed", knowledge_id=knowledge_id,
                                          provider=estimate.provider, model=estimate.model):
                        result = await self.embedding_provider.embed(chunk["content"])
                    vector_id = uuid4().hex
                    async with self._span("knowledge.index", knowledge_id=knowledge_id,
                                          project_id=record["project_id"]):
                        reference = await self.vector_store.upsert(
                            vector_id=vector_id, knowledge_id=knowledge_id, chunk_id=chunk["chunk_id"],
                            project_id=record["project_id"], vector=result.vector,
                            metadata={"knowledge_type": record["knowledge_type"],
                                      "source_reference": record["source_reference"]},
                        )
                        await self.store.add_embedding({
                            "embedding_id": uuid4().hex, "knowledge_id": knowledge_id,
                            "chunk_id": chunk["chunk_id"], "provider": result.provider,
                            "model": result.model, "dimensions": len(result.vector),
                            "input_tokens": result.input_tokens, "cost": result.cost,
                            "vector_reference": reference,
                            "vector_hash": hashlib.sha256(json.dumps(result.vector, separators=(",", ":")).encode()).hexdigest(),
                            "created_at": datetime.now(UTC).isoformat(),
                        })
                finally:
                    fallback = result or EmbeddingResult([], estimate.provider, estimate.model, estimate.input_tokens, estimate.estimated_cost)
                    await self.finops.after_embedding(call_id=call_id, result=fallback, succeeded=result is not None)
            await self.store.update_status(knowledge_id, KnowledgeStatus.INDEXED.value)
            await self.store.add_audit(knowledge_id=knowledge_id, project_id=record["project_id"],
                                       workflow_id=record.get("workflow_id"), action="knowledge.index",
                                       outcome="indexed", metadata={"chunks": len(chunks)})
            await self._metric("indexed_learnings", 1, "count", knowledge_id=knowledge_id)
            return await self._detail_or_raise(knowledge_id)
        except Exception as exc:
            await self.store.update_status(knowledge_id, KnowledgeStatus.VALIDATED.value)
            await self.store.add_audit(knowledge_id=knowledge_id, project_id=record["project_id"],
                                       workflow_id=record.get("workflow_id"), action="knowledge.index",
                                       outcome="failed", reason=type(exc).__name__, metadata={})
            raise

    async def search_knowledge(self, *, query: str, project_id: str,
                               knowledge_types: list[str] | None = None, limit: int = 5,
                               operation: str = "knowledge.search") -> dict[str, Any]:
        query = query.strip(); project_id = project_id.strip()
        if not query or not project_id:
            raise KnowledgeValidationError("query and project_id are required")
        invalid = set(knowledge_types or []) - {item.value for item in KnowledgeType}
        if invalid:
            raise KnowledgeValidationError(f"unknown knowledge types: {sorted(invalid)}")
        started = perf_counter(); retrieval_id = uuid4().hex
        outer_context = get_observability_context()
        retrieval_context = outer_context
        finops_call_ids: list[str] = []
        async with self._span("knowledge.search", project_id=project_id, limit=limit,
                              retrieval_id=retrieval_id) as span_context:
            retrieval_context = span_context or outer_context
            estimate = self.embedding_provider.estimate(query); call_id = uuid4().hex
            finops_call_ids.append(call_id)
            await self.finops.before_embedding(
                call_id=call_id, project_id=project_id, workflow_id=retrieval_context.workflow_id,
                agent_name=retrieval_context.agent or "KnowledgeRetriever",
                operation="knowledge_embedding", estimate=estimate,
            )
            query_embedding: EmbeddingResult | None = None
            try:
                async with self._span("knowledge.embed", project_id=project_id,
                                      provider=estimate.provider, model=estimate.model, purpose="query"):
                    query_embedding = await self.embedding_provider.embed(query)
            finally:
                fallback = query_embedding or EmbeddingResult([], estimate.provider, estimate.model,
                                                               estimate.input_tokens, estimate.estimated_cost)
                await self.finops.after_embedding(call_id=call_id, result=fallback,
                                                  succeeded=query_embedding is not None)
            assert query_embedding is not None
            matches = await self.vector_store.search(project_id=project_id, vector=query_embedding.vector,
                                                     limit=limit, knowledge_types=knowledge_types)
            results = []; retrieval_results: list[dict[str, Any]] = []
            for match in matches:
                record = await self.store.detail(match.knowledge_id)
                chunks = await self.store.chunks(match.knowledge_id)
                chunk = next((item for item in chunks if item["chunk_id"] == match.chunk_id), None)
                if (record is None or chunk is None or record["status"] != KnowledgeStatus.INDEXED.value
                        or record["project_id"] != project_id or chunk["project_id"] != project_id):
                    continue
                rank = len(results) + 1
                results.append({
                    "content": chunk["content"], "chunk_id": chunk["chunk_id"], "rank": rank,
                    "provenance": {
                        "knowledge_id": record["knowledge_id"], "chunk_id": chunk["chunk_id"],
                        "source_reference": record["source_reference"], "rank": rank,
                        "knowledge_type": record["knowledge_type"], "project_id": record["project_id"],
                        "workflow_id": record.get("workflow_id"), "created_at": record["created_at"],
                        "version": record["version"], "retrieval_score": round(match.score, 8),
                    },
                })
                retrieval_results.append({
                    "rank": rank, "knowledge_id": record["knowledge_id"],
                    "chunk_id": chunk["chunk_id"], "project_id": record["project_id"],
                    "retrieval_score": float(match.score),
                })
        latency = (perf_counter() - started) * 1000
        safe_query = sanitize_text(query)[0]
        await self.store.add_retrieval({
            "retrieval_id": retrieval_id, "project_id": project_id, "operation": operation,
            "query_hash": content_hash(query), "query_preview": safe_query[:120], "query": safe_query,
            "result_count": len(results), "latency_ms": latency,
            "workflow_id": retrieval_context.workflow_id, "agent_name": retrieval_context.agent,
            "trace_id": retrieval_context.trace_id, "span_id": retrieval_context.span_id,
            "top_k": limit, "filters": {"knowledge_types": knowledge_types or []},
            "reranker_used": False, "reranker_model": None,
            "finops_call_ids": finops_call_ids, "detail_state": "complete",
            "metadata": {"embedding_provider": query_embedding.provider,
                         "embedding_model": query_embedding.model,
                         "embedding_operation": "knowledge_embedding"},
        }, retrieval_results)
        await self._metric("retrieval_latency", latency, "ms", project_id=project_id)
        await self._metric("retrieved_items", len(results), "count", project_id=project_id)
        return {"retrieval_id": retrieval_id, "project_id": project_id, "results": results,
                "retrieved_items": len(results), "retrieval_latency_ms": round(latency, 3)}

    async def get_relevant_context(self, *, query: str, project_id: str,
                                   knowledge_types: list[str] | None = None,
                                   limit: int = 5) -> dict[str, Any]:
        response = await self.search_knowledge(
            query=query, project_id=project_id, knowledge_types=knowledge_types,
            limit=limit, operation="knowledge.relevant_context",
        )
        return {**response, "context": "\n\n".join(item["content"] for item in response["results"]),
                "sources": [item["provenance"] for item in response["results"]]}

    async def get_project_decisions(self, *, project_id: str, query: str = "decision", limit: int = 10) -> dict[str, Any]:
        return await self.search_knowledge(query=query, project_id=project_id,
                                           knowledge_types=[KnowledgeType.DECISION.value], limit=limit,
                                           operation="knowledge.project_decisions")

    async def get_similar_implementations(self, *, project_id: str, query: str, limit: int = 5) -> dict[str, Any]:
        return await self.search_knowledge(
            query=query, project_id=project_id,
            knowledge_types=[KnowledgeType.CODE_PATTERN.value, KnowledgeType.TEST_PATTERN.value, KnowledgeType.SOLUTION.value],
            limit=limit, operation="knowledge.similar_implementations",
        )

    async def get_knowledge_source(self, *, project_id: str, knowledge_id: str) -> dict[str, Any]:
        record = await self._detail_or_raise(knowledge_id)
        if record["project_id"] != project_id or record["status"] != KnowledgeStatus.INDEXED.value:
            raise KnowledgeNotFoundError(knowledge_id)
        return {"content": record["content"], "provenance": {
            "knowledge_id": record["knowledge_id"], "source_reference": record["source_reference"],
            "knowledge_type": record["knowledge_type"], "project_id": record["project_id"],
            "workflow_id": record.get("workflow_id"), "created_at": record["created_at"],
            "version": record["version"], "retrieval_score": 1.0,
        }}

    async def get_learning_status(self, *, project_id: str, knowledge_id: str) -> dict[str, Any]:
        record = await self._detail_or_raise(knowledge_id)
        if record["project_id"] != project_id:
            raise KnowledgeNotFoundError(knowledge_id)
        return record
