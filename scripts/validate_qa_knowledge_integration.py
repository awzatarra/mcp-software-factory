from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import re
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
from graph.subgraphs.implementation.models import GeneratedFile, ProjectImplementationPlan
from graph.subgraphs.implementation.test_validation import build_fastapi_health_test
from graph.subgraphs.planning.models import (
    AcceptanceCriterion,
    ImplementationTask,
    PlanningOutput,
    RequirementAnalysis,
)
from host import SoftwareFactoryHost
from langgraph.checkpoint.memory import InMemorySaver
from streaming.sqlite_store import workflow_event_store_path
from tool_executor import HostToolExecutor, ToolExecutionOutcome


TARGET_PROJECT = "phase-7-qa-validation"
TARGET_CONTENT = (
    "FastAPI database integration tests must use an isolated test database and "
    "must verify transaction rollback between tests."
)
TARGET_SOURCE = "testing:fastapi-database-integration"


class ProbePlanningService:
    @staticmethod
    def _plan(state: SoftwareFactoryState) -> PlanningOutput:
        project = str(state.get("project_name") or "validation-project")
        return PlanningOutput(
            analysis=RequirementAnalysis(
                objective="Crear usuarios y validarlos con integration tests de base de datos.",
                project_name=project,
                project_type="fastapi",
                functional_requirements=["Crear usuarios mediante un endpoint FastAPI."],
                constraints=["Usar pytest y una base de datos aislada."],
            ),
            acceptance_criteria=[
                AcceptanceCriterion(
                    id="AC-1",
                    description="Los integration tests verifican creación y aislamiento.",
                    verification_method="Ejecutar pytest.",
                )
            ],
            tasks=[
                ImplementationTask(
                    order=1,
                    role="Developer",
                    title="Implementar creación de usuarios",
                    description="Crear el endpoint y sus archivos de soporte.",
                ),
                ImplementationTask(
                    order=2,
                    role="QA",
                    title="Validar integración con base de datos",
                    description="Preparar y ejecutar integration tests con pytest.",
                    depends_on=[1],
                ),
            ],
        )

    async def analyze(self, state: SoftwareFactoryState):
        plan = self._plan(state)
        return plan.analysis, plan.acceptance_criteria

    async def create_tasks(self, state: SoftwareFactoryState):
        return self._plan(state).tasks

    async def refine(self, state: SoftwareFactoryState):
        return self._plan(state)


class ProbeImplementationService:
    @staticmethod
    def _proposal(project_name: str) -> ProjectImplementationPlan:
        package = re.sub(r"[^a-zA-Z0-9_]", "_", project_name).strip("_")
        return ProjectImplementationPlan(
            project_name=project_name,
            framework="fastapi",
            package_name=package,
            files=[
                GeneratedFile(
                    path=f"{package}/main.py",
                    content=(
                        "from fastapi import FastAPI\n"
                        "app = FastAPI()\n"
                        "@app.get('/health')\n"
                        "def health():\n"
                        "    return {'status': 'ok'}\n"
                    ),
                ),
                GeneratedFile(
                    path="tests/test_health.py",
                    content=build_fastapi_health_test(
                        package, "/health", 200, {"status": "ok"}
                    ),
                ),
                GeneratedFile(path="requirements.txt", content="fastapi\npytest\n"),
            ],
        )

    async def generate_project(self, state: SoftwareFactoryState) -> ProjectImplementationPlan:
        return self._proposal(str(state.get("project_name") or "validation-project"))

    async def refine_project(self, state: SoftwareFactoryState) -> ProjectImplementationPlan:
        return self._proposal(str(state.get("project_name") or "validation-project"))


class HybridExecutor:
    """Routes retrieval through MCP and simulates non-Knowledge side effects."""

    def __init__(self, knowledge: HostToolExecutor) -> None:
        self.knowledge = knowledge
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
        if name == "filesystem__list_files":
            updates.update(workspace_inspected=True, project_exists=False)
        elif name == "filesystem__create_project_structure":
            updates.update(
                project_created=True,
                created_project_name=str(state_context.get("project_name")),
            )
        elif name == "testing__detect_test_framework":
            updates.update(
                detected_test_framework="pytest",
                expected_test_command=["python", "-m", "pytest"],
            )
        elif name == "testing__prepare_test_environment":
            project = str(state_context.get("project_name"))
            updates.update(
                environment_prepared=True,
                dependencies_installed=True,
                environment_python=f"workspace/{project}/.venv/Scripts/python.exe",
                installed_fastapi_version="0.139.0",
                installed_starlette_version="1.3.1",
            )
        else:
            raise RuntimeError(f"unexpected validation tool: {name}")
        return ToolExecutionOutcome(name, arguments, {"success": True}, updates)


async def ensure_target_knowledge(database: Path) -> str:
    service = KnowledgeService(KnowledgeStore(database))
    await service.initialize()
    result = await service.submit_learning(
        SubmitLearningRequest(
            project_id=TARGET_PROJECT,
            workflow_id="manual-qa-knowledge-seed",
            agent_name="QA",
            knowledge_type="test_pattern",
            content=TARGET_CONTENT,
            source_reference=TARGET_SOURCE,
        )
    )
    if result["status"] != "indexed":
        raise RuntimeError(f"test_pattern was not auto-indexed: {result['status']}")
    return str(result["knowledge_id"])


async def run_case(
    project_id: str,
    *,
    executor: HybridExecutor,
    observability: ObservabilityService,
    knowledge_store: KnowledgeStore,
    target_knowledge_id: str,
) -> dict[str, Any]:
    workflow_id = f"manual-qa-knowledge-{uuid4().hex[:12]}"
    root = await observability.ensure_trace(workflow_id, "original")
    dependencies = GraphDependencies(
        tool_executor=executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="manual-probe-no-provider",
        planning_service=ProbePlanningService(),
        implementation_service=ProbeImplementationService(),
        observability=observability,
    )
    graph = build_software_factory_graph(dependencies, checkpointer=InMemorySaver())
    request = (
        f"Crea un proyecto FastAPI llamado {project_id} con un endpoint para crear "
        "usuarios y agrega integration tests usando base de datos."
    )
    try:
        with observability_context(root):
            result = await run_software_factory_graph(
                graph, request, thread_id=workflow_id
            )
            service = WorkflowPersistenceService(graph)
            result = await service.approve(result.thread_id)
            result = await service.approve(result.thread_id)
        snapshot = await service.get_snapshot(workflow_id)
        state = snapshot.values
        retrieval_id = state.get("qa_knowledge_retrieval_id")
        retrieval = (
            await knowledge_store.retrieval(str(retrieval_id)) if retrieval_id else None
        )
        sources = state.get("qa_knowledge_sources", [])
        return {
            "project_id": project_id,
            "workflow_id": workflow_id,
            "pending_operation": state.get("pending_operation"),
            "knowledge_state": state.get("qa_knowledge_state"),
            "retrieval_id": retrieval_id,
            "retrieval_used": state.get("qa_knowledge_retrieval_used"),
            "retrieved_context_count": state.get("qa_retrieved_context_count"),
            "context_tokens": state.get("qa_knowledge_context_tokens"),
            "context_available_before_tests": bool(state.get("qa_knowledge_context")),
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
            "tests_executed": state.get("tests_executed"),
        }
    finally:
        await observability.store.finalize_trace(
            root.trace_id,
            status="completed",
            ended_at=datetime.now(UTC).isoformat(),
            reason="manual_qa_knowledge_validation",
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
    executor = HybridExecutor(HostToolExecutor(host, observability))
    try:
        positive = await run_case(
            TARGET_PROJECT,
            executor=executor,
            observability=observability,
            knowledge_store=knowledge_store,
            target_knowledge_id=target_knowledge_id,
        )
        negative = await run_case(
            f"phase-7-qa-negative-{uuid4().hex[:8]}",
            executor=executor,
            observability=observability,
            knowledge_store=knowledge_store,
            target_knowledge_id=target_knowledge_id,
        )
    finally:
        await manager.disconnect_all()

    ok = (
        positive["pending_operation"] == "run_tests"
        and positive["knowledge_state"] == "available"
        and positive["retrieval_used"] is True
        and positive["retrieved_context_count"] >= 1
        and positive["context_available_before_tests"] is True
        and positive["target_knowledge_retrieved"] is True
        and positive["target_source_retrieved"] is True
        and positive["persisted_workflow_id"] == positive["workflow_id"]
        and positive["persisted_agent"] == "QA"
        and bool(positive["trace_id"])
        and bool(positive["span_id"])
        and positive["tests_executed"] is False
        and negative["pending_operation"] == "run_tests"
        and negative["target_knowledge_retrieved"] is False
        and negative["target_source_retrieved"] is False
    )
    print(
        json.dumps(
            {
                "ok": ok,
                "provider_calls": 0,
                "target_knowledge_id": target_knowledge_id,
                "positive": positive,
                "negative": negative,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
