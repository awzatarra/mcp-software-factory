from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.knowledge_providers import SQLiteVectorStore
from api.services.knowledge_service import KnowledgeService
from api.services.knowledge_store import KnowledgeStore
from clients.mcp_client import MCPServerClient


LEARNING = {
    "project_id": "phase-7-validation",
    "workflow_id": "manual-knowledge-validation",
    "agent_name": "Developer",
    "knowledge_type": "workflow_learning",
    "content": "FastAPI integration tests require the application environment to be prepared before execution.",
    "source_reference": "workflow:manual-knowledge-validation",
    "metadata": {"validation": "phase-7.0"},
}


def result_payload(result) -> dict:
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if text:
            parsed = json.loads(text)
            return parsed.get("result", parsed) if isinstance(parsed, dict) else parsed
    raise RuntimeError("Knowledge MCP returned no structured payload")


async def validate(database: Path) -> dict:
    previous_path = os.environ.get("WORKFLOW_EVENT_STORE_PATH")
    os.environ["WORKFLOW_EVENT_STORE_PATH"] = str(database)
    client = MCPServerClient("knowledge", str(ROOT / "servers" / "knowledge_server.py"))
    try:
        await client.connect()
        first = result_payload(await client.call_tool("submit_learning", LEARNING))
        initial_status = first["status"]
        admin = KnowledgeService(KnowledgeStore(database), vector_store=SQLiteVectorStore(database))
        await admin.initialize()
        indexed = first if initial_status == "indexed" else await admin.approve(first["knowledge_id"])
        search = result_payload(await client.call_tool("search_knowledge", {
            "query": "How should integration tests prepare the environment?",
            "project_id": "phase-7-validation",
        }))
        duplicate = result_payload(await client.call_tool("submit_learning", LEARNING))
        isolated = result_payload(await client.call_tool("search_knowledge", {
            "query": "How should integration tests prepare the environment?",
            "project_id": "another-project",
        }))
        search_detail = await admin.store.retrieval(search["retrieval_id"])
        isolated_detail = await admin.store.retrieval(isolated["retrieval_id"])
    finally:
        await client.disconnect()
        if previous_path is None:
            os.environ.pop("WORKFLOW_EVENT_STORE_PATH", None)
        else:
            os.environ["WORKFLOW_EVENT_STORE_PATH"] = previous_path
    connection = sqlite3.connect(database)
    try:
        knowledge_traces = connection.execute("SELECT COUNT(*) FROM observability_traces WHERE source='knowledge_mcp'").fetchone()[0]
        knowledge_spans = connection.execute("SELECT COUNT(*) FROM observability_spans WHERE category='knowledge'").fetchone()[0]
        knowledge_metrics = connection.execute("SELECT COUNT(*) FROM observability_metrics WHERE name IN ('retrieval_latency','retrieved_items','write_pipeline_duration')").fetchone()[0]
        embedding_operations = connection.execute("SELECT COUNT(*) FROM knowledge_embeddings WHERE operation='knowledge_embedding'").fetchone()[0]
        retrieval_result_rows = connection.execute("SELECT COUNT(*) FROM knowledge_retrieval_results").fetchone()[0]
        correlated_span = connection.execute(
            "SELECT COUNT(*) FROM observability_spans WHERE trace_id=? AND span_id=?",
            (search_detail["trace_id"], search_detail["span_id"]),
        ).fetchone()[0]
    finally:
        connection.close()
    checks = {
        "initial_status_allowed": initial_status in {"candidate", "validated", "indexed"},
        "indexed": indexed["status"] == "indexed",
        "chunks_created": indexed["chunk_count"] > 0,
        "embedding_reference_created": indexed["embedding_count"] > 0,
        "vector_reference_created": indexed["vector_count"] > 0,
        "retrieved_original": any(item["provenance"]["knowledge_id"] == indexed["knowledge_id"] for item in search["results"]),
        "duplicate_reused": duplicate["knowledge_id"] == indexed["knowledge_id"] and duplicate["created"] is False,
        "cross_project_isolated": isolated["results"] == [],
        "retrieval_detail_complete": search_detail["detail_state"] == "complete",
        "retrieval_provenance_persisted": (
            search_detail["result_count"] == len(search_detail["results"])
            and search_detail["results"][0]["knowledge_id"] == indexed["knowledge_id"]
        ),
        "empty_retrieval_persisted": (
            isolated_detail["detail_state"] == "complete"
            and isolated_detail["result_count"] == 0 and isolated_detail["results"] == []
        ),
        "retrieval_span_correlated": correlated_span == 1,
        "observability_traced": knowledge_traces > 0 and knowledge_spans > 0 and knowledge_metrics > 0,
        "finops_operation_recorded": embedding_operations > 0,
    }
    return {
        "database": str(database), "knowledge_id": indexed["knowledge_id"],
        "initial_status": initial_status, "final_status": indexed["status"],
        "chunks": indexed["chunk_count"], "embeddings": indexed["embedding_count"],
        "vectors": indexed["vector_count"], "retrieved_items": search["retrieved_items"],
        "retrieval_id": search["retrieval_id"], "retrieval_result_rows": retrieval_result_rows,
        "empty_retrieval_id": isolated["retrieval_id"],
        "duplicate_created": duplicate["created"], "cross_project_results": isolated["retrieved_items"],
        "knowledge_traces": knowledge_traces, "knowledge_spans": knowledge_spans,
        "knowledge_metrics": knowledge_metrics, "embedding_operations": embedding_operations,
        "checks": checks, "valid": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the Phase 7.0 Knowledge lifecycle without paid calls.")
    parser.add_argument("--database", type=Path, help="Optional durable SQLite path. A temporary database is used by default.")
    args = parser.parse_args()
    if args.database:
        result = asyncio.run(validate(args.database.resolve()))
    else:
        with tempfile.TemporaryDirectory(prefix="knowledge-phase7-") as directory:
            result = asyncio.run(validate(Path(directory) / "knowledge.sqlite"))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
