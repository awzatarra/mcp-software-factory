from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Protocol, runtime_checkable


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+", re.UNICODE)


@dataclass(frozen=True)
class EmbeddingEstimate:
    provider: str
    model: str
    input_tokens: int
    estimated_cost: Decimal


@dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    provider: str
    model: str
    input_tokens: int
    cost: Decimal


@runtime_checkable
class EmbeddingProvider(Protocol):
    def estimate(self, text: str) -> EmbeddingEstimate: ...
    async def embed(self, text: str) -> EmbeddingResult: ...


@dataclass(frozen=True)
class VectorSearchResult:
    knowledge_id: str
    chunk_id: str
    score: float


@runtime_checkable
class VectorStore(Protocol):
    async def initialize(self) -> None: ...
    async def upsert(
        self, *, vector_id: str, knowledge_id: str, chunk_id: str,
        project_id: str, vector: list[float], metadata: dict[str, Any],
    ) -> str: ...
    async def delete_knowledge(self, knowledge_id: str) -> int: ...
    async def search(
        self, *, project_id: str, vector: list[float], limit: int,
        knowledge_types: list[str] | None = None,
    ) -> list[VectorSearchResult]: ...


class DeterministicEmbeddingProvider:
    """Local lexical embedding used by default; no provider credentials or paid calls."""

    provider = "local"
    model = "hash-embedding-v1"

    def __init__(self, dimensions: int = 128) -> None:
        self.dimensions = dimensions

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return [token.casefold() for token in TOKEN_PATTERN.findall(text)]

    def estimate(self, text: str) -> EmbeddingEstimate:
        return EmbeddingEstimate(self.provider, self.model, len(self._tokens(text)), Decimal(0))

    async def embed(self, text: str) -> EmbeddingResult:
        tokens = self._tokens(text)
        vector = [0.0] * self.dimensions
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 1.0
        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude:
            vector = [value / magnitude for value in vector]
        return EmbeddingResult(vector, self.provider, self.model, len(tokens), Decimal(0))


class SQLiteVectorStore:
    """Replaceable vector representation. Canonical knowledge lives elsewhere."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS knowledge_vector_index (
                vector_id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL, chunk_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL, vector_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(knowledge_id) REFERENCES knowledge_records(knowledge_id) ON DELETE CASCADE,
                FOREIGN KEY(chunk_id) REFERENCES knowledge_chunks(chunk_id) ON DELETE CASCADE
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_vector_project ON knowledge_vector_index(project_id)")

    async def upsert(self, *, vector_id: str, knowledge_id: str, chunk_id: str,
                     project_id: str, vector: list[float], metadata: dict[str, Any]) -> str:
        from datetime import UTC, datetime
        await asyncio.to_thread(self._upsert_sync, vector_id, knowledge_id, chunk_id, project_id, vector, metadata, datetime.now(UTC).isoformat())
        return vector_id

    def _upsert_sync(self, vector_id: str, knowledge_id: str, chunk_id: str,
                     project_id: str, vector: list[float], metadata: dict[str, Any], created_at: str) -> None:
        with self._connection() as connection:
            connection.execute("""INSERT INTO knowledge_vector_index
                (vector_id,knowledge_id,chunk_id,project_id,vector_json,metadata_json,created_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(chunk_id) DO UPDATE SET
                vector_id=excluded.vector_id,vector_json=excluded.vector_json,
                metadata_json=excluded.metadata_json,created_at=excluded.created_at""",
                (vector_id, knowledge_id, chunk_id, project_id,
                 json.dumps(vector, separators=(",", ":")), json.dumps(metadata, sort_keys=True), created_at))

    async def delete_knowledge(self, knowledge_id: str) -> int:
        return await asyncio.to_thread(self._delete_sync, knowledge_id)

    def _delete_sync(self, knowledge_id: str) -> int:
        with self._connection() as connection:
            return connection.execute("DELETE FROM knowledge_vector_index WHERE knowledge_id=?", (knowledge_id,)).rowcount

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=False))

    async def search(self, *, project_id: str, vector: list[float], limit: int,
                     knowledge_types: list[str] | None = None) -> list[VectorSearchResult]:
        return await asyncio.to_thread(self._search_sync, project_id, vector, limit, knowledge_types)

    def _search_sync(self, project_id: str, vector: list[float], limit: int,
                     knowledge_types: list[str] | None) -> list[VectorSearchResult]:
        parameters: list[Any] = [project_id]
        type_clause = ""
        if knowledge_types:
            placeholders = ",".join("?" for _ in knowledge_types)
            type_clause = f" AND r.knowledge_type IN ({placeholders})"
            parameters.extend(knowledge_types)
        with self._connection() as connection:
            rows = connection.execute(f"""SELECT v.knowledge_id,v.chunk_id,v.vector_json
                FROM knowledge_vector_index v JOIN knowledge_records r ON r.knowledge_id=v.knowledge_id
                WHERE v.project_id=? AND r.project_id=v.project_id AND r.status='indexed'{type_clause}""", parameters).fetchall()
        scored = (VectorSearchResult(row["knowledge_id"], row["chunk_id"],
                                     self._cosine(vector, json.loads(row["vector_json"]))) for row in rows)
        ranked = sorted((item for item in scored if item.score > 0), key=lambda item: item.score, reverse=True)
        return ranked[: max(1, min(limit, 100))]


class KnowledgeFinOps(Protocol):
    async def before_embedding(self, *, call_id: str, project_id: str, workflow_id: str | None,
                               agent_name: str, operation: str,
                               estimate: EmbeddingEstimate) -> dict[str, Any]: ...
    async def after_embedding(self, *, call_id: str, result: EmbeddingResult,
                              succeeded: bool) -> None: ...


class NoopKnowledgeFinOps:
    async def before_embedding(self, **_: Any) -> dict[str, Any]:
        return {"decision": "allow", "reason_codes": ["finops_not_configured"]}

    async def after_embedding(self, **_: Any) -> None:
        return None


class LLMCostKnowledgeFinOps:
    def __init__(self, llm_costs: Any) -> None:
        self.llm_costs = llm_costs

    async def before_embedding(self, *, call_id: str, project_id: str, workflow_id: str | None,
                               agent_name: str, operation: str,
                               estimate: EmbeddingEstimate) -> dict[str, Any]:
        return await self.llm_costs.preflight(
            call_id=call_id, workflow_id=workflow_id, branch_id="original",
            agent_name=agent_name, provider=estimate.provider, model=estimate.model,
            kwargs={"operation": operation}, estimated_amount=estimate.estimated_cost,
            project_name=project_id,
        )

    async def after_embedding(self, *, call_id: str, result: EmbeddingResult,
                              succeeded: bool) -> None:
        consumed = result.cost if succeeded else None
        await asyncio.to_thread(self.llm_costs.store.finalize_reservations_sync, call_id, consumed)
