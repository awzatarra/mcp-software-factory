from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4


SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_records (
 knowledge_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workflow_id TEXT,
 agent_name TEXT NOT NULL, requested_type TEXT NOT NULL, knowledge_type TEXT NOT NULL,
 classification_confidence REAL NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
 source_reference TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL, rejection_reason TEXT, duplicate_of TEXT,
 duplicate_candidate INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, validated_at TEXT, indexed_at TEXT,
 UNIQUE(project_id,source_reference,content_hash)
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
 chunk_id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL, project_id TEXT NOT NULL,
 knowledge_type TEXT NOT NULL, source_reference TEXT NOT NULL, chunk_index INTEGER NOT NULL,
 content TEXT NOT NULL, content_hash TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, UNIQUE(knowledge_id,chunk_index),
 FOREIGN KEY(knowledge_id) REFERENCES knowledge_records(knowledge_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS knowledge_embeddings (
 embedding_id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
 provider TEXT NOT NULL, model TEXT NOT NULL, dimensions INTEGER NOT NULL,
 input_tokens INTEGER, cost TEXT, operation TEXT NOT NULL,
 vector_reference TEXT NOT NULL, vector_hash TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(chunk_id,provider,model),
 FOREIGN KEY(knowledge_id) REFERENCES knowledge_records(knowledge_id) ON DELETE CASCADE,
 FOREIGN KEY(chunk_id) REFERENCES knowledge_chunks(chunk_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS knowledge_audit (
 event_id TEXT PRIMARY KEY, knowledge_id TEXT, project_id TEXT,
 workflow_id TEXT, action TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT,
 metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_retrievals (
 retrieval_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, operation TEXT NOT NULL,
 query_hash TEXT NOT NULL, query_preview TEXT, query_text TEXT,
 result_count INTEGER NOT NULL, latency_ms REAL NOT NULL,
 workflow_id TEXT, agent_name TEXT, trace_id TEXT, span_id TEXT, top_k INTEGER,
 filters_json TEXT NOT NULL DEFAULT '{}', reranker_used INTEGER NOT NULL DEFAULT 0,
 reranker_model TEXT, finops_call_ids_json TEXT NOT NULL DEFAULT '[]',
 detail_state TEXT NOT NULL DEFAULT 'summary_only',
 metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_retrieval_results (
 retrieval_id TEXT NOT NULL, rank INTEGER NOT NULL, knowledge_id TEXT NOT NULL,
 chunk_id TEXT NOT NULL, project_id TEXT NOT NULL, source_reference TEXT NOT NULL,
 knowledge_type TEXT NOT NULL, knowledge_version INTEGER NOT NULL, workflow_id TEXT,
 retrieval_score REAL NOT NULL, semantic_score REAL, rerank_score REAL, final_score REAL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(retrieval_id,rank), UNIQUE(retrieval_id,chunk_id),
 CHECK(rank > 0),
 FOREIGN KEY(retrieval_id) REFERENCES knowledge_retrievals(retrieval_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_knowledge_records_project ON knowledge_records(project_id,status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_records_source ON knowledge_records(project_id,source_reference);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_record ON knowledge_chunks(knowledge_id,chunk_index);
CREATE INDEX IF NOT EXISTS idx_knowledge_retrievals_project ON knowledge_retrievals(project_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_retrieval_results_record ON knowledge_retrieval_results(knowledge_id,chunk_id);
CREATE TRIGGER IF NOT EXISTS knowledge_retrieval_result_project_guard
BEFORE INSERT ON knowledge_retrieval_results
BEGIN
 SELECT CASE WHEN (SELECT project_id FROM knowledge_retrievals WHERE retrieval_id=NEW.retrieval_id) != NEW.project_id
  THEN RAISE(ABORT, 'retrieval_project_mismatch') END;
 SELECT CASE WHEN (SELECT project_id FROM knowledge_records WHERE knowledge_id=NEW.knowledge_id) != NEW.project_id
  THEN RAISE(ABORT, 'knowledge_project_mismatch') END;
 SELECT CASE WHEN (SELECT project_id FROM knowledge_chunks WHERE chunk_id=NEW.chunk_id) != NEW.project_id
  THEN RAISE(ABORT, 'chunk_project_mismatch') END;
END;
"""


RETRIEVAL_MIGRATIONS = (
    ("query_text", "TEXT"),
    ("workflow_id", "TEXT"),
    ("agent_name", "TEXT"),
    ("trace_id", "TEXT"),
    ("span_id", "TEXT"),
    ("top_k", "INTEGER"),
    ("filters_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("reranker_used", "INTEGER NOT NULL DEFAULT 0"),
    ("reranker_model", "TEXT"),
    ("finops_call_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("detail_state", "TEXT NOT NULL DEFAULT 'summary_only'"),
)


def now() -> str:
    return datetime.now(UTC).isoformat()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json"):
            result[key[:-5]] = json.loads(result.pop(key) or "{}")
    for key in ("duplicate_candidate", "reranker_used"):
        if key in result:
            result[key] = bool(result[key])
    if "query_text" in result:
        result["query"] = result.pop("query_text") or result.get("query_preview")
    if "knowledge_version" in result:
        result["version"] = result.pop("knowledge_version")
    return result


class KnowledgeStore:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def connection(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def initialize(self) -> None:
        await asyncio.to_thread(self.initialize_sync)

    def initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(knowledge_retrievals)")}
            for column, definition in RETRIEVAL_MIGRATIONS:
                if column not in columns:
                    connection.execute(f"ALTER TABLE knowledge_retrievals ADD COLUMN {column} {definition}")
            self._remove_mutable_result_foreign_keys(connection)
            self._reconcile_retrieval_detail_states(connection)

    @staticmethod
    def _remove_mutable_result_foreign_keys(connection: sqlite3.Connection) -> None:
        foreign_tables = {
            row["table"] for row in connection.execute(
                "PRAGMA foreign_key_list(knowledge_retrieval_results)"
            )
        }
        if not ({"knowledge_records", "knowledge_chunks"} & foreign_tables):
            return
        connection.execute("DROP TRIGGER IF EXISTS knowledge_retrieval_result_project_guard")
        connection.execute("DROP INDEX IF EXISTS idx_knowledge_retrieval_results_record")
        connection.execute(
            "ALTER TABLE knowledge_retrieval_results RENAME TO knowledge_retrieval_results_legacy"
        )
        connection.execute("""CREATE TABLE knowledge_retrieval_results (
            retrieval_id TEXT NOT NULL, rank INTEGER NOT NULL, knowledge_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL, project_id TEXT NOT NULL, source_reference TEXT NOT NULL,
            knowledge_type TEXT NOT NULL, knowledge_version INTEGER NOT NULL, workflow_id TEXT,
            retrieval_score REAL NOT NULL, semantic_score REAL, rerank_score REAL, final_score REAL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(retrieval_id,rank), UNIQUE(retrieval_id,chunk_id), CHECK(rank > 0),
            FOREIGN KEY(retrieval_id) REFERENCES knowledge_retrievals(retrieval_id) ON DELETE CASCADE
        )""")
        connection.execute("""INSERT INTO knowledge_retrieval_results
            SELECT * FROM knowledge_retrieval_results_legacy""")
        connection.execute("DROP TABLE knowledge_retrieval_results_legacy")
        connection.execute("""CREATE INDEX idx_knowledge_retrieval_results_record
            ON knowledge_retrieval_results(knowledge_id,chunk_id)""")
        connection.execute("""CREATE TRIGGER knowledge_retrieval_result_project_guard
            BEFORE INSERT ON knowledge_retrieval_results
            BEGIN
             SELECT CASE WHEN (SELECT project_id FROM knowledge_retrievals WHERE retrieval_id=NEW.retrieval_id) != NEW.project_id
              THEN RAISE(ABORT, 'retrieval_project_mismatch') END;
             SELECT CASE WHEN (SELECT project_id FROM knowledge_records WHERE knowledge_id=NEW.knowledge_id) != NEW.project_id
              THEN RAISE(ABORT, 'knowledge_project_mismatch') END;
             SELECT CASE WHEN (SELECT project_id FROM knowledge_chunks WHERE chunk_id=NEW.chunk_id) != NEW.project_id
              THEN RAISE(ABORT, 'chunk_project_mismatch') END;
            END""")

    @staticmethod
    def _reconcile_retrieval_detail_states(connection: sqlite3.Connection) -> None:
        connection.execute("""UPDATE knowledge_retrievals
            SET detail_state=CASE
                WHEN result_count=0 THEN 'complete'
                WHEN result_count=(SELECT COUNT(*) FROM knowledge_retrieval_results rr
                                   WHERE rr.retrieval_id=knowledge_retrievals.retrieval_id)
                THEN 'complete'
                ELSE 'summary_only'
            END""")

    async def create_or_get(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        return await asyncio.to_thread(self._create_or_get_sync, record)

    def _create_or_get_sync(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM knowledge_records WHERE project_id=? AND source_reference=? AND content_hash=?",
                (record["project_id"], record["source_reference"], record["content_hash"]),
            ).fetchone()
            if existing:
                connection.commit()
                return _row(existing) or {}, False
            timestamp = now()
            knowledge_id = record.get("knowledge_id") or uuid4().hex
            connection.execute("""INSERT INTO knowledge_records (
                knowledge_id,project_id,workflow_id,agent_name,requested_type,knowledge_type,
                classification_confidence,content,content_hash,source_reference,metadata_json,
                status,rejection_reason,duplicate_of,duplicate_candidate,version,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                knowledge_id, record["project_id"], record.get("workflow_id"), record["agent_name"],
                record["requested_type"], record["knowledge_type"], record["classification_confidence"],
                record["content"], record["content_hash"], record["source_reference"],
                json.dumps(record.get("metadata") or {}, sort_keys=True, default=str), record["status"],
                record.get("rejection_reason"), record.get("duplicate_of"),
                int(bool(record.get("duplicate_candidate"))), int(record.get("version", 1)), timestamp, timestamp,
            ))
            created = connection.execute("SELECT * FROM knowledge_records WHERE knowledge_id=?", (knowledge_id,)).fetchone()
            connection.commit()
            return _row(created) or {}, True

    async def get(self, knowledge_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, knowledge_id)

    def _get_sync(self, knowledge_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return _row(connection.execute("SELECT * FROM knowledge_records WHERE knowledge_id=?", (knowledge_id,)).fetchone())

    async def detail(self, knowledge_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._detail_sync, knowledge_id)

    def _detail_sync(self, knowledge_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("""SELECT r.*,
                (SELECT COUNT(*) FROM knowledge_chunks c WHERE c.knowledge_id=r.knowledge_id) AS chunk_count,
                (SELECT COUNT(*) FROM knowledge_embeddings e WHERE e.knowledge_id=r.knowledge_id) AS embedding_count,
                (SELECT COUNT(*) FROM knowledge_vector_index v WHERE v.knowledge_id=r.knowledge_id) AS vector_count
                FROM knowledge_records r WHERE r.knowledge_id=?""", (knowledge_id,)).fetchone()
            return _row(row)

    async def list(self, *, status: str | None = None, project_id: str | None = None,
                   knowledge_type: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_sync, status, project_id, knowledge_type, limit, offset)

    def _list_sync(self, status: str | None, project_id: str | None, knowledge_type: str | None,
                   limit: int, offset: int) -> list[dict[str, Any]]:
        where: list[str] = []; params: list[Any] = []
        for column, value in (("status", status), ("project_id", project_id), ("knowledge_type", knowledge_type)):
            if value:
                where.append(f"r.{column}=?"); params.append(value)
        clause = " WHERE " + " AND ".join(where) if where else ""
        params.extend((max(1, min(limit, 500)), max(0, offset)))
        with self.connection() as connection:
            rows = connection.execute(f"""SELECT r.*,
                (SELECT COUNT(*) FROM knowledge_chunks c WHERE c.knowledge_id=r.knowledge_id) AS chunk_count,
                (SELECT COUNT(*) FROM knowledge_vector_index v WHERE v.knowledge_id=r.knowledge_id) AS vector_count
                FROM knowledge_records r{clause} ORDER BY r.created_at DESC LIMIT ? OFFSET ?""", params).fetchall()
            return [_row(row) or {} for row in rows]

    async def possible_duplicates(self, project_id: str, knowledge_type: str, exclude_hash: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._possible_duplicates_sync, project_id, knowledge_type, exclude_hash)

    def _possible_duplicates_sync(self, project_id: str, knowledge_type: str, exclude_hash: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("""SELECT knowledge_id,content,content_hash FROM knowledge_records
                WHERE project_id=? AND knowledge_type=? AND content_hash!=? AND status NOT IN ('rejected','archived')
                ORDER BY created_at DESC LIMIT 100""", (project_id, knowledge_type, exclude_hash)).fetchall()
            return [_row(row) or {} for row in rows]

    async def update_status(self, knowledge_id: str, status: str, *, reason: str | None = None) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._update_status_sync, knowledge_id, status, reason)

    def _update_status_sync(self, knowledge_id: str, status: str, reason: str | None) -> dict[str, Any] | None:
        timestamp = now()
        validated = timestamp if status == "validated" else None
        indexed = timestamp if status == "indexed" else None
        with self.connection() as connection:
            connection.execute("""UPDATE knowledge_records SET status=?,rejection_reason=COALESCE(?,rejection_reason),
                updated_at=?,validated_at=COALESCE(?,validated_at),indexed_at=COALESCE(?,indexed_at)
                WHERE knowledge_id=?""", (status, reason, timestamp, validated, indexed, knowledge_id))
            return _row(connection.execute("SELECT * FROM knowledge_records WHERE knowledge_id=?", (knowledge_id,)).fetchone())

    async def replace_chunks(self, knowledge_id: str, chunks: list[dict[str, Any]]) -> None:
        await asyncio.to_thread(self._replace_chunks_sync, knowledge_id, chunks)

    def _replace_chunks_sync(self, knowledge_id: str, chunks: list[dict[str, Any]]) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM knowledge_embeddings WHERE knowledge_id=?", (knowledge_id,))
            connection.execute("DELETE FROM knowledge_chunks WHERE knowledge_id=?", (knowledge_id,))
            for chunk in chunks:
                connection.execute("""INSERT INTO knowledge_chunks VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                    chunk["chunk_id"], knowledge_id, chunk["project_id"], chunk["knowledge_type"],
                    chunk["source_reference"], chunk["chunk_index"], chunk["content"],
                    chunk["content_hash"], json.dumps(chunk.get("metadata") or {}, sort_keys=True, default=str),
                    chunk["created_at"],
                ))
            connection.commit()

    async def chunks(self, knowledge_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._chunks_sync, knowledge_id)

    def _chunks_sync(self, knowledge_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM knowledge_chunks WHERE knowledge_id=? ORDER BY chunk_index", (knowledge_id,)).fetchall()
            return [_row(row) or {} for row in rows]

    async def clear_representations(self, knowledge_id: str) -> None:
        await asyncio.to_thread(self._clear_representations_sync, knowledge_id)

    def _clear_representations_sync(self, knowledge_id: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM knowledge_embeddings WHERE knowledge_id=?", (knowledge_id,))
            connection.execute("DELETE FROM knowledge_chunks WHERE knowledge_id=?", (knowledge_id,))

    async def add_embedding(self, record: dict[str, Any]) -> None:
        await asyncio.to_thread(self._add_embedding_sync, record)

    def _add_embedding_sync(self, record: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute("""INSERT INTO knowledge_embeddings
                (embedding_id,knowledge_id,chunk_id,provider,model,dimensions,input_tokens,cost,
                 operation,vector_reference,vector_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(chunk_id,provider,model) DO UPDATE SET
                 embedding_id=excluded.embedding_id,dimensions=excluded.dimensions,input_tokens=excluded.input_tokens,
                 cost=excluded.cost,vector_reference=excluded.vector_reference,vector_hash=excluded.vector_hash,
                 created_at=excluded.created_at""", (
                record["embedding_id"], record["knowledge_id"], record["chunk_id"], record["provider"],
                record["model"], record["dimensions"], record.get("input_tokens"), str(record.get("cost", "0")),
                "knowledge_embedding", record["vector_reference"], record["vector_hash"], record["created_at"],
            ))

    async def add_audit(self, *, knowledge_id: str | None, project_id: str | None,
                        workflow_id: str | None, action: str, outcome: str,
                        reason: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        await asyncio.to_thread(self._add_audit_sync, knowledge_id, project_id, workflow_id, action, outcome, reason, metadata or {})

    def _add_audit_sync(self, knowledge_id: str | None, project_id: str | None,
                        workflow_id: str | None, action: str, outcome: str,
                        reason: str | None, metadata: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute("INSERT INTO knowledge_audit VALUES(?,?,?,?,?,?,?,?,?)", (
                uuid4().hex, knowledge_id, project_id, workflow_id, action, outcome, reason,
                json.dumps(metadata, sort_keys=True, default=str), now(),
            ))

    async def add_retrieval(self, record: dict[str, Any],
                            results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._add_retrieval_sync, record, results)

    def _add_retrieval_sync(self, record: dict[str, Any],
                            results: list[dict[str, Any]] | None) -> dict[str, Any]:
        retrieval_id = record.get("retrieval_id") or uuid4().hex
        timestamp = record.get("created_at") or now()
        detail_state = record.get("detail_state") or ("complete" if results is not None else "summary_only")
        if results is not None and int(record["result_count"]) != len(results):
            raise ValueError("retrieval_result_count_mismatch")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""INSERT INTO knowledge_retrievals (
                retrieval_id,project_id,operation,query_hash,query_preview,query_text,
                result_count,latency_ms,workflow_id,agent_name,trace_id,span_id,top_k,
                filters_json,reranker_used,reranker_model,finops_call_ids_json,
                detail_state,metadata_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                retrieval_id, record["project_id"], record["operation"], record["query_hash"],
                record.get("query_preview"), record.get("query"), int(record["result_count"]),
                float(record["latency_ms"]), record.get("workflow_id"), record.get("agent_name"),
                record.get("trace_id"), record.get("span_id"), record.get("top_k"),
                json.dumps(record.get("filters") or {}, sort_keys=True, default=str),
                int(bool(record.get("reranker_used"))), record.get("reranker_model"),
                json.dumps(record.get("finops_call_ids") or [], sort_keys=True, default=str),
                detail_state, json.dumps(record.get("metadata") or {}, sort_keys=True, default=str), timestamp,
            ))
            for position, result in enumerate(results or [], start=1):
                rank = int(result.get("rank") or position)
                if result.get("project_id") != record["project_id"]:
                    raise ValueError("retrieval_project_mismatch")
                canonical = connection.execute("""SELECT r.project_id,r.source_reference,r.knowledge_type,
                        r.version,r.workflow_id,c.knowledge_id AS chunk_knowledge_id,c.project_id AS chunk_project_id
                    FROM knowledge_records r JOIN knowledge_chunks c ON c.knowledge_id=r.knowledge_id
                    WHERE r.knowledge_id=? AND c.chunk_id=?""",
                    (result["knowledge_id"], result["chunk_id"])).fetchone()
                if canonical is None:
                    raise ValueError("retrieval_provenance_not_found")
                if (canonical["project_id"] != record["project_id"]
                        or canonical["chunk_project_id"] != record["project_id"]
                        or canonical["chunk_knowledge_id"] != result["knowledge_id"]):
                    raise ValueError("retrieval_project_mismatch")
                connection.execute("""INSERT INTO knowledge_retrieval_results (
                    retrieval_id,rank,knowledge_id,chunk_id,project_id,source_reference,
                    knowledge_type,knowledge_version,workflow_id,retrieval_score,
                    semantic_score,rerank_score,final_score,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    retrieval_id, rank, result["knowledge_id"], result["chunk_id"],
                    record["project_id"], canonical["source_reference"], canonical["knowledge_type"],
                    canonical["version"], canonical["workflow_id"], float(result["retrieval_score"]),
                    result.get("semantic_score"), result.get("rerank_score"), result.get("final_score"), timestamp,
                ))
            created = connection.execute(
                "SELECT * FROM knowledge_retrievals WHERE retrieval_id=?", (retrieval_id,),
            ).fetchone()
            connection.commit()
            return _row(created) or {}

    async def retrievals(self, limit: int = 100) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._retrievals_sync, limit)

    def _retrievals_sync(self, limit: int) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM knowledge_retrievals ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
            return [_row(row) or {} for row in rows]

    async def retrieval(self, retrieval_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._retrieval_sync, retrieval_id)

    def _retrieval_sync(self, retrieval_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_retrievals WHERE retrieval_id=?", (retrieval_id,),
            ).fetchone()
            if row is None:
                return None
            result = _row(row) or {}
            rows = connection.execute("""SELECT * FROM knowledge_retrieval_results
                WHERE retrieval_id=? ORDER BY rank""", (retrieval_id,)).fetchall()
            result["results"] = [_row(item) or {} for item in rows]
            return result

    async def sources(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._sources_sync)

    def _sources_sync(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("""SELECT project_id,source_reference,COUNT(*) AS knowledge_count,
                SUM(CASE WHEN status='indexed' THEN 1 ELSE 0 END) AS indexed_count,MAX(updated_at) AS updated_at
                FROM knowledge_records GROUP BY project_id,source_reference ORDER BY updated_at DESC""").fetchall()
            return [_row(row) or {} for row in rows]
