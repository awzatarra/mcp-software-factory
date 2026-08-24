from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.knowledge_models import SubmitLearningRequest
from api.services.knowledge_service import KnowledgeService
from api.services.knowledge_store import KnowledgeStore
from api.services.observability_context import observability_context
from api.services.observability_service import ObservabilityService
from api.services.observability_store import ObservabilityStore
from clients.mcp_client import MCPServerClient
from clients.mcp_manager import MCPClientManager
from graph.builder import build_software_factory_graph
from graph.nodes import GraphDependencies
from graph.persistence_service import WorkflowPersistenceService
from graph.runtime import run_software_factory_graph
from graph.state import SoftwareFactoryState
from host import SoftwareFactoryHost
from langgraph.checkpoint.memory import InMemorySaver
from scripts.validate_qa_knowledge_integration import (
    ProbeImplementationService,
    ProbePlanningService,
)
from streaming.sqlite_store import workflow_event_store_path
from tool_executor import HostToolExecutor, ToolExecutionOutcome


TARGET_PROJECT = "phase-7-repair-validation"
TARGET_CONTENT = (
    "When FastAPI integration tests fail because database state leaks between "
    "tests, isolate each test with a transaction and roll it back after the test "
    "completes."
)
TARGET_SOURCE = "solution:pytest-db-rollback"


class RepairHybridExecutor:
    def __init__(self, knowledge: HostToolExecutor, *, tests_pass: bool = False) -> None:
        self.knowledge = knowledge
        self.tests_pass = tests_pass
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def openai_tool(self, name: str) -> dict[str, Any]:
        return self.knowledge.openai_tool(name)

    async def execute(self, name, arguments, *, state_context, approval_mode="prompt"):
        self.calls.append((name, dict(arguments)))
        if name == "knowledge__get_relevant_context":
            return await self.knowledge.execute(
                name,
                arguments,
                state_context=state_context,
                approval_mode=approval_mode,
            )
        updates: dict[str, Any] = {}
        payload: dict[str, Any] = {"success": True}
        project = str(state_context.get("project_name") or state_context.get("created_project_name"))
        if name == "filesystem__list_files":
            updates.update(workspace_inspected=True, project_exists=False)
        elif name == "filesystem__create_project_structure":
            updates.update(project_created=True, created_project_name=project)
        elif name == "testing__detect_test_framework":
            updates.update(
                detected_test_framework="pytest",
                expected_test_command=["python", "-m", "pytest"],
            )
        elif name == "testing__prepare_test_environment":
            updates.update(
                environment_prepared=True,
                dependencies_installed=True,
                environment_python=f"workspace/{project}/.venv/Scripts/python.exe",
                installed_fastapi_version="0.139.0",
                installed_starlette_version="1.3.1",
            )
        elif name == "testing__run_tests":
            if self.tests_pass:
                updates.update(
                    tests_executed=True,
                    tests_passed=True,
                    final_test_result_summary="1 passed",
                    test_stdout="1 passed",
                )
            else:
                payload = {"success": False, "failure_type": "test_failure"}
                updates.update(
                    tests_executed=True,
                    tests_passed=False,
                    failure_type="test_failure",
                    failure_stage="run_tests",
                    failure_message="Database state leaked between integration tests.",
                    test_failure_summary=(
                        "tests/test_database.py failed because transaction rollback "
                        "did not isolate database state"
                    ),
                    test_stderr="AssertionError: user from previous test still exists",
                    failing_test_files=["tests/test_database.py"],
                    first_test_result_summary="1 failed",
                    repair_phase="read_failing_test",
                )
        elif name == "filesystem__read_file":
            path = str(arguments["relative_path"])
            reads = [*state_context.get("files_read_during_repair", []), path]
            updates["files_read_during_repair"] = reads
            if path.endswith("tests/test_database.py"):
                payload["content"] = "def test_isolation(): assert users == []"
                updates["repair_phase"] = "read_related_source"
            else:
                payload["content"] = "def create_user(): return repository.save(user)"
                updates["repair_phase"] = "apply_fix"
        else:
            raise RuntimeError(f"unexpected validation tool: {name}")
        return ToolExecutionOutcome(name, arguments, payload, updates)


async def arguments_for(name: str, state: SoftwareFactoryState) -> dict[str, Any]:
    project = str(state.get("project_name") or state.get("created_project_name"))
    package = project.replace("-", "_")
    if name == "filesystem__create_project_structure":
        return {
            "project_name": project,
            "files": [
                {
                    "path": f"{package}/main.py",
                    "content": "from fastapi import FastAPI\napp = FastAPI()\n",
                },
                {
                    "path": "tests/test_database.py",
                    "content": "def test_isolation(): assert True\n",
                },
                {"path": "requirements.txt", "content": "fastapi\npytest\n"},
            ],
        }
    if name == "filesystem__update_project_files":
        return {
            "project_name": project,
            "files": [
                {
                    "path": "tests/test_database.py",
                    "content": "def test_isolation(transaction): assert users == []\n",
                }
            ],
        }
    raise RuntimeError(f"unexpected argument request: {name}")


async def ensure_target_knowledge(database: Path) -> str:
    service = KnowledgeService(KnowledgeStore(database))
    await service.initialize()
    result = await service.submit_learning(
        SubmitLearningRequest(
            project_id=TARGET_PROJECT,
            workflow_id="manual-repair-knowledge-seed",
            agent_name="Repair",
            knowledge_type="solution",
            content=TARGET_CONTENT,
            source_reference=TARGET_SOURCE,
        )
    )
    if result["status"] == "candidate":
        result = await service.approve(
            result["knowledge_id"], actor="manual_repair_validation"
        )
    if result["status"] != "indexed":
        raise RuntimeError(f"solution was not indexed: {result['status']}")
    return str(result["knowledge_id"])


async def run_case(
    project_id: str,
    *,
    host_executor: HostToolExecutor,
    observability: ObservabilityService,
    knowledge_store: KnowledgeStore,
    target_knowledge_id: str,
    tests_pass: bool,
) -> dict[str, Any]:
    workflow_id = f"manual-repair-knowledge-{uuid4().hex[:12]}"
    root = await observability.ensure_trace(workflow_id, "original")
    executor = RepairHybridExecutor(host_executor, tests_pass=tests_pass)
    graph = build_software_factory_graph(
        GraphDependencies(
            tool_executor=executor,
            openai_client=object(),  # type: ignore[arg-type]
            model="manual-probe-no-provider",
            planning_service=ProbePlanningService(),
            implementation_service=ProbeImplementationService(),
            argument_resolver=arguments_for,
            observability=observability,
        ),
        checkpointer=InMemorySaver(),
    )
    request = (
        f"Crea un proyecto FastAPI llamado {project_id} con integration tests de "
        "base de datos."
    )
    try:
        with observability_context(root):
            result = await run_software_factory_graph(
                graph, request, thread_id=workflow_id
            )
            service = WorkflowPersistenceService(graph)
            result = await service.approve(result.thread_id)
            result = await service.approve(result.thread_id)
            result = await service.approve(result.thread_id)
        snapshot = await service.get_snapshot(workflow_id)
        state = snapshot.values
        retrieval_id = state.get("repair_knowledge_retrieval_id")
        retrieval = (
            await knowledge_store.retrieval(str(retrieval_id)) if retrieval_id else None
        )
        sources = state.get("repair_knowledge_sources", [])
        repair_calls = [
            arguments
            for name, arguments in executor.calls
            if name == "knowledge__get_relevant_context"
            and arguments.get("agent_name") == "Repair"
        ]
        return {
            "project_id": project_id,
            "workflow_id": workflow_id,
            "pending_operation": state.get("pending_operation"),
            "terminal_status": state.get("terminal_status"),
            "tests_passed": state.get("tests_passed"),
            "repair_calls": len(repair_calls),
            "knowledge_state": state.get("repair_knowledge_state"),
            "retrieval_id": retrieval_id,
            "retrieval_used": state.get("repair_knowledge_retrieval_used"),
            "retrieved_context_count": state.get("repair_retrieved_context_count"),
            "context_tokens": state.get("repair_knowledge_context_tokens"),
            "target_knowledge_retrieved": any(
                isinstance(item, dict)
                and item.get("knowledge_id") == target_knowledge_id
                for item in sources
            ),
            "target_source_retrieved": any(
                isinstance(item, dict)
                and item.get("source_reference") == TARGET_SOURCE
                for item in sources
            ),
            "persisted_workflow_id": retrieval.get("workflow_id") if retrieval else None,
            "persisted_agent": retrieval.get("agent_name") if retrieval else None,
            "trace_id": retrieval.get("trace_id") if retrieval else None,
            "span_id": retrieval.get("span_id") if retrieval else None,
            "files_updated": state.get("files_updated_during_repair", []),
        }
    finally:
        await observability.store.finalize_trace(
            root.trace_id,
            status="completed",
            ended_at=datetime.now(UTC).isoformat(),
            reason="manual_repair_knowledge_validation",
        )


async def main() -> int:
    database = workflow_event_store_path()
    target_knowledge_id = await ensure_target_knowledge(database)
    observability = ObservabilityService(ObservabilityStore(database))
    await observability.initialize()
    knowledge_store = KnowledgeStore(database)
    await knowledge_store.initialize()

    client = MCPServerClient("knowledge", str(ROOT / "servers" / "knowledge_server.py"))
    manager = MCPClientManager([client])
    await manager.connect_all()
    host = SoftwareFactoryHost(
        manager, object(), "manual-probe-no-provider", ""
    )  # type: ignore[arg-type]
    host_executor = HostToolExecutor(host, observability)
    try:
        positive = await run_case(
            TARGET_PROJECT,
            host_executor=host_executor,
            observability=observability,
            knowledge_store=knowledge_store,
            target_knowledge_id=target_knowledge_id,
            tests_pass=False,
        )
        negative = await run_case(
            f"phase-7-repair-negative-{uuid4().hex[:8]}",
            host_executor=host_executor,
            observability=observability,
            knowledge_store=knowledge_store,
            target_knowledge_id=target_knowledge_id,
            tests_pass=False,
        )
        passing = await run_case(
            f"phase-7-repair-passing-{uuid4().hex[:8]}",
            host_executor=host_executor,
            observability=observability,
            knowledge_store=knowledge_store,
            target_knowledge_id=target_knowledge_id,
            tests_pass=True,
        )
    finally:
        await manager.disconnect_all()

    ok = (
        positive["pending_operation"] == "apply_fix"
        and positive["knowledge_state"] == "available"
        and positive["retrieval_used"] is True
        and positive["retrieved_context_count"] >= 1
        and positive["target_knowledge_retrieved"] is True
        and positive["target_source_retrieved"] is True
        and positive["persisted_workflow_id"] == positive["workflow_id"]
        and positive["persisted_agent"] == "Repair"
        and bool(positive["trace_id"])
        and bool(positive["span_id"])
        and positive["files_updated"] == []
        and negative["target_knowledge_retrieved"] is False
        and negative["target_source_retrieved"] is False
        and passing["tests_passed"] is True
        and passing["repair_calls"] == 0
        and passing["knowledge_state"] == "not_started"
    )
    print(
        json.dumps(
            {
                "ok": ok,
                "provider_calls": 0,
                "target_knowledge_id": target_knowledge_id,
                "positive": positive,
                "negative": negative,
                "passing": passing,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
