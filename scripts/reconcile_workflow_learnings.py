from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.workflow_learning_store import WorkflowLearningStore
from clients.mcp_client import MCPServerClient
from streaming.sqlite_store import workflow_event_store_path


def _payload(result: object) -> dict:
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        value = structured.get("result", structured)
        return value if isinstance(value, dict) else {}
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if text:
            parsed = json.loads(text)
            value = parsed.get("result", parsed) if isinstance(parsed, dict) else {}
            return value if isinstance(value, dict) else {}
    raise RuntimeError("Knowledge MCP returned no structured payload")


def _status(payload: dict) -> str:
    if payload.get("duplicate") or payload.get("duplicate_of"):
        return "duplicate"
    status = str(payload.get("status") or "candidate")
    return status if status in {"candidate", "indexed", "rejected"} else "candidate"


async def reconcile(*, execute: bool, limit: int) -> dict[str, int | bool]:
    store = WorkflowLearningStore(workflow_event_store_path())
    await store.initialize()
    pending = await store.pending(limit)
    summary: dict[str, int | bool] = {
        "dry_run": not execute, "pending": len(pending), "submitted": 0,
        "failed": 0, "duplicates": 0,
    }
    if not execute or not pending:
        return summary
    client = MCPServerClient("knowledge", str(ROOT / "servers" / "knowledge_server.py"))
    await client.connect()
    try:
        for candidate in pending:
            arguments = {
                key: candidate[key]
                for key in ("project_id", "workflow_id", "agent_name", "knowledge_type", "content", "source_reference")
            }
            arguments["metadata"] = {
                "origin": "workflow_learning_extractor",
                "candidate_id": candidate["candidate_id"],
                "confidence": candidate["confidence"],
                "extraction_method": candidate["extraction_method"],
                "evidence_refs": candidate["evidence_refs"],
                "branch_id": candidate["branch_id"],
                "knowledge_provenance_ids": candidate.get("knowledge_provenance_ids", []),
                "reconciled": True,
            }
            try:
                payload = _payload(await client.call_tool("submit_learning", arguments))
                status = _status(payload)
                result = {"candidate_id": candidate["candidate_id"], "submission_status": status,
                          "knowledge_id": payload.get("knowledge_id"), "retryable": False}
                await store.record_submission(candidate["candidate_id"], status, result)
                summary["submitted"] = int(summary["submitted"]) + 1
                if status == "duplicate":
                    summary["duplicates"] = int(summary["duplicates"]) + 1
            except Exception as exc:
                result = {"candidate_id": candidate["candidate_id"], "submission_status": "submission_failed",
                          "retryable": True, "failure_type": type(exc).__name__, "failure_message": str(exc)[:500]}
                await store.record_submission(candidate["candidate_id"], "submission_failed", result)
                summary["failed"] = int(summary["failed"]) + 1
    finally:
        await client.disconnect()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry durable workflow learning submissions without re-extraction.")
    parser.add_argument("--execute", action="store_true", help="Submit pending candidates; default is dry-run.")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    summary = asyncio.run(reconcile(execute=args.execute, limit=max(1, min(args.limit, 10_000))))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if int(summary["failed"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
