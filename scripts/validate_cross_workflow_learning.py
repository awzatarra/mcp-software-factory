from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.knowledge_providers import SQLiteVectorStore
from api.services.knowledge_service import KnowledgeService
from api.services.knowledge_store import KnowledgeStore
from clients.mcp_client import MCPServerClient
from graph.workflow_learning import WorkflowLearningExtractor


def payload(result: object) -> dict:
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if text:
            decoded = json.loads(text)
            return decoded.get("result", decoded)
    raise RuntimeError("MCP result has no payload")


async def validate(database: Path) -> dict[str, object]:
    previous = os.environ.get("WORKFLOW_EVENT_STORE_PATH")
    os.environ["WORKFLOW_EVENT_STORE_PATH"] = str(database)
    client = MCPServerClient("knowledge", str(ROOT / "servers" / "knowledge_server.py"))
    extractor = WorkflowLearningExtractor()
    evidence = {
        "terminal_status": "completed", "tests_executed": True, "tests_passed": True,
        "detected_test_framework": "pytest", "final_test_result_summary": "2 passed",
        "repair_phase": "completed", "repair_attempts": 1,
        "failure_message": "Database state leaked between integration tests.",
        "repair_before": "shared database state", "repair_after": "transaction rollback per test",
        "repair_decision": "Isolate each test with a transaction and rollback after completion.",
        "files_updated_during_repair": ["tests/conftest.py"],
    }
    candidates = extractor.extract(
        workflow_id="workflow-a", branch_id="original",
        project_id="phase-7-cross-workflow-validation", evidence=evidence,
    )
    try:
        await client.connect()
        submitted = []
        for candidate in candidates:
            if candidate.knowledge_type not in {"incident", "solution"}:
                continue
            result = payload(await client.call_tool("submit_learning", {
                "project_id": candidate.project_id, "workflow_id": candidate.workflow_id,
                "agent_name": candidate.agent_name, "knowledge_type": candidate.knowledge_type,
                "content": candidate.content, "source_reference": candidate.source_reference,
                "metadata": {"origin": "workflow_learning_extractor", "candidate_id": candidate.candidate_id,
                             "confidence": candidate.confidence, "extraction_method": candidate.extraction_method,
                             "evidence_refs": candidate.evidence_refs, "branch_id": candidate.branch_id},
            }))
            submitted.append(result)
        solution = next(item for item in submitted if item["knowledge_type"] == "solution")
        admin = KnowledgeService(KnowledgeStore(database), vector_store=SQLiteVectorStore(database))
        await admin.initialize()
        if solution["status"] != "indexed":
            solution = await admin.approve(solution["knowledge_id"], actor="manual_validation")
        same_project = payload(await client.call_tool("get_relevant_context", {
            "query": "database state leak integration tests rollback isolation",
            "project_id": "phase-7-cross-workflow-validation", "workflow_id": "workflow-b",
            "agent_name": "Repair", "knowledge_types": ["solution"], "top_k": 5,
        }))
        other_project = payload(await client.call_tool("get_relevant_context", {
            "query": "database state leak integration tests rollback isolation",
            "project_id": "another-project", "workflow_id": "workflow-c",
            "agent_name": "Repair", "knowledge_types": ["solution"], "top_k": 5,
        }))
        failed = extractor.extract(
            workflow_id="workflow-failed", branch_id="original",
            project_id="phase-7-cross-workflow-validation",
            evidence={**evidence, "terminal_status": "tests_failed", "tests_passed": False,
                      "repair_phase": "rerun_tests"},
        )
        checks = {
            "incident_submitted": any(item["knowledge_type"] == "incident" for item in submitted),
            "solution_indexed": solution["status"] == "indexed",
            "workflow_b_reused_solution": any(
                item.get("provenance", {}).get("knowledge_id") == solution["knowledge_id"]
                for item in same_project.get("results", [])
            ),
            "workflow_c_isolated": not other_project.get("results"),
            "failed_repair_has_no_solution": not any(item.knowledge_type == "solution" for item in failed),
        }
        return {"checks": checks, "solution_knowledge_id": solution["knowledge_id"],
                "provider_llm_calls": 0, "passed": all(checks.values())}
    finally:
        await client.disconnect()
        if previous is None:
            os.environ.pop("WORKFLOW_EVENT_STORE_PATH", None)
        else:
            os.environ["WORKFLOW_EVENT_STORE_PATH"] = previous


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cross-workflow-learning-") as directory:
        result = asyncio.run(validate(Path(directory) / "validation.sqlite"))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
