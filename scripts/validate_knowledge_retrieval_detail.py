from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.knowledge_store import KnowledgeStore
from clients.mcp_client import MCPServerClient
from streaming.sqlite_store import workflow_event_store_path


PROJECT = "phase-7-candidate-validation"
OTHER_PROJECT = "phase-7-other-project"
QUERY = "How should integration tests handle database migrations?"
EXPECTED_KNOWLEDGE = "89dbbaec329e49338732331788e9649c"
EXPECTED_CHUNK = "860f1bc9754d41858e3df6c542c27962"


def payload(result) -> dict:
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
    previous = os.environ.get("WORKFLOW_EVENT_STORE_PATH")
    os.environ["WORKFLOW_EVENT_STORE_PATH"] = str(database)
    client = MCPServerClient("knowledge", str(ROOT / "servers" / "knowledge_server.py"))
    try:
        await client.connect()
        hit = payload(await client.call_tool("search_knowledge", {
            "query": QUERY, "project_id": PROJECT,
        }))
        miss = payload(await client.call_tool("search_knowledge", {
            "query": QUERY, "project_id": OTHER_PROJECT,
        }))
    finally:
        await client.disconnect()
        if previous is None:
            os.environ.pop("WORKFLOW_EVENT_STORE_PATH", None)
        else:
            os.environ["WORKFLOW_EVENT_STORE_PATH"] = previous

    store = KnowledgeStore(database)
    await store.initialize()
    hit_detail = await store.retrieval(hit["retrieval_id"])
    miss_detail = await store.retrieval(miss["retrieval_id"])
    result = hit_detail["results"][0] if hit_detail and hit_detail["results"] else None
    with sqlite3.connect(database) as connection:
        correlated_span = connection.execute(
            "SELECT COUNT(*) FROM observability_spans WHERE trace_id=? AND span_id=?",
            (hit_detail["trace_id"], hit_detail["span_id"]),
        ).fetchone()[0] if hit_detail else 0
    checks = {
        "hit_complete": bool(hit_detail and hit_detail["detail_state"] == "complete"),
        "expected_knowledge": bool(result and result["knowledge_id"] == EXPECTED_KNOWLEDGE),
        "expected_chunk": bool(result and result["chunk_id"] == EXPECTED_CHUNK),
        "rank_preserved": bool(result and result["rank"] == 1),
        "score_preserved": bool(result and abs(result["retrieval_score"] - 0.50395263) < 1e-8),
        "project_isolated": bool(
            miss_detail and miss_detail["project_id"] == OTHER_PROJECT
            and miss_detail["result_count"] == 0 and miss_detail["results"] == []
        ),
        "observability_correlated": correlated_span == 1,
        "finops_correlated": bool(hit_detail and hit_detail["finops_call_ids"]),
        "no_raw_embeddings": bool(result and not ({"vector", "embedding"} & set(result))),
    }
    return {
        "database": str(database), "hit_retrieval_id": hit["retrieval_id"],
        "miss_retrieval_id": miss["retrieval_id"], "knowledge_id": result["knowledge_id"] if result else None,
        "chunk_id": result["chunk_id"] if result else None,
        "source_reference": result["source_reference"] if result else None,
        "retrieval_score": result["retrieval_score"] if result else None,
        "trace_id": hit_detail["trace_id"] if hit_detail else None,
        "span_id": hit_detail["span_id"] if hit_detail else None,
        "finops_call_ids": hit_detail["finops_call_ids"] if hit_detail else [],
        "miss_result_count": miss_detail["result_count"] if miss_detail else None,
        "checks": checks, "valid": all(checks.values()),
    }


def main() -> int:
    result = asyncio.run(validate(workflow_event_store_path().resolve()))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
