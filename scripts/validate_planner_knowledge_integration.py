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
from graph.runtime import thread_config
from graph.state import SoftwareFactoryState, create_initial_state
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


TARGET_PROJECT = "phase-7-planner-validation"
TARGET_CONTENT = (
    "The project uses a service-layer architecture. API routes must delegate "
    "business logic to services and must not access persistence directly."
)
TARGET_SOURCE = "architecture:service-layer"


class PlannerBoundaryProbeService:
    """Capture the real pre-task Planner state without a paid provider call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _plan(state: SoftwareFactoryState) -> PlanningOutput:
        project_id = str(state.get("project_name") or "planner-validation")
        return PlanningOutput(
            analysis=RequirementAnalysis(
                objective="Plan a FastAPI endpoint that cancels orders.",
                project_name=project_id,
                project_type="fastapi",
                functional_requirements=["Create an endpoint that cancels orders."],
                non_functional_requirements=["Keep route handlers maintainable."],
                constraints=["Respect current project architecture and conventions."],
                assumptions=["Only order cancellation is requested."],
            ),
            acceptance_criteria=[
                AcceptanceCriterion(
                    id="AC-1",
                    description="The endpoint cancels an order.",
                    verification_method="Automated pytest coverage.",
                )
            ],
            tasks=[
                ImplementationTask(
                    order=1,
                    role="Developer",
                    title="Implement order cancellation",
                    description="Implement the FastAPI route and service behavior.",
                ),
                ImplementationTask(
                    order=2,
                    role="QA",
                    title="Validate order cancellation",
                    description="Test the cancellation behavior with pytest.",
                    depends_on=[1],
                ),
            ],
        )

    async def analyze(self, state: SoftwareFactoryState):
        plan = self._plan(state)
        return plan.analysis, plan.acceptance_criteria

    async def create_tasks(self, state: SoftwareFactoryState):
        self.calls.append(
            {
                "project_id": state.get("project_name"),
                "knowledge_state": state.get("planner_knowledge_state"),
                "retrieval_id": state.get("planner_knowledge_retrieval_id"),
                "context": state.get("planner_knowledge_context"),
                "sources": state.get("planner_knowledge_sources", []),
            }
        )
        return self._plan(state).tasks

    async def refine(self, state: SoftwareFactoryState):
        return self._plan(state)


class DeveloperBoundaryProbeService:
    @staticmethod
    def _proposal(project_name: str) -> ProjectImplementationPlan:
        package_name = re.sub(r"[^a-zA-Z0-9_]", "_", project_name).strip("_")
        return ProjectImplementationPlan(
            project_name=project_name,
            framework="fastapi",
            package_name=package_name,
            files=[
                GeneratedFile(
                    path=f"{package_name}/main.py",
                    content=(
                        "from fastapi import FastAPI\n"
                        "app = FastAPI()\n"
                        "@app.get('/health')\n"
                        "def health():\n"
                        "    return {'status': 'ok'}\n"
                        "@app.post('/orders/{order_id}/cancel')\n"
                        "def cancel_order(order_id: str):\n"
                        "    return {'status': 'cancelled', 'order_id': order_id}\n"
                    ),
                ),
                GeneratedFile(
                    path="tests/test_health.py",
                    content=build_fastapi_health_test(
                        package_name,
                        "/health",
                        200,
                        {"status": "ok"},
                    ),
                ),
                GeneratedFile(path="requirements.txt", content="fastapi\npytest\n"),
            ],
        )

    async def generate_project(self, state: SoftwareFactoryState) -> ProjectImplementationPlan:
        return self._proposal(str(state.get("project_name") or "planner-validation"))

    async def refine_project(self, state: SoftwareFactoryState) -> ProjectImplementationPlan:
        return self._proposal(str(state.get("project_name") or "planner-validation"))


class HybridExecutor:
    def __init__(self, knowledge: HostToolExecutor) -> None:
        self.knowledge = knowledge

    def openai_tool(self, name: str) -> dict[str, Any]:
        return self.knowledge.openai_tool(name)

    async def execute(self, name, arguments, *, state_context, approval_mode="prompt"):
        if name == "knowledge__get_relevant_context":
            return await self.knowledge.execute(
                name,
                arguments,
                state_context=state_context,
                approval_mode=approval_mode,
            )
        if name == "filesystem__list_files":
            return ToolExecutionOutcome(
                name,
                arguments,
                {"success": True, "files": []},
                {"workspace_inspected": True, "project_exists": False},
            )
        raise RuntimeError(f"unexpected validation tool: {name}")


async def ensure_target_knowledge(database: Path) -> str:
    service = KnowledgeService(KnowledgeStore(database))
    await service.initialize()
    result = await service.submit_learning(
        SubmitLearningRequest(
            project_id=TARGET_PROJECT,
            workflow_id="manual-planner-knowledge-seed",
            agent_name="Planner",
            knowledge_type="architecture",
            content=TARGET_CONTENT,
            source_reference=TARGET_SOURCE,
        )
    )
    if result["status"] == "candidate":
        result = await service.approve(
            result["knowledge_id"], actor="manual_planner_validation"
        )
    if result["status"] != "indexed":
        raise RuntimeError(f"target knowledge is not indexed: {result['status']}")
    return str(result["knowledge_id"])


async def run_case(
    project_id: str,
    *,
    executor: HybridExecutor,
    observability: ObservabilityService,
    knowledge_store: KnowledgeStore,
    target_knowledge_id: str,
) -> dict[str, Any]:
    workflow_id = f"manual-planner-knowledge-{uuid4().hex[:12]}"
    root = await observability.ensure_trace(workflow_id, "original")
    planner_probe = PlannerBoundaryProbeService()
    dependencies = GraphDependencies(
        tool_executor=executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="manual-probe-no-provider",
        planning_service=planner_probe,
        implementation_service=DeveloperBoundaryProbeService(),
        observability=observability,
    )
    graph = build_software_factory_graph(dependencies, checkpointer=InMemorySaver())
    request = f"Crea un proyecto FastAPI llamado {project_id} para cancelar ordenes."
    try:
        with observability_context(root):
            await graph.ainvoke(
                create_initial_state(request),
                config=thread_config(workflow_id),
            )
        snapshot = await WorkflowPersistenceService(graph).get_snapshot(workflow_id)
        result = snapshot.values
        retrieval_id = result.get("planner_knowledge_retrieval_id")
        retrieval = (
            await knowledge_store.retrieval(str(retrieval_id))
            if retrieval_id
            else None
        )
        sources = result.get("planner_knowledge_sources", [])
        return {
            "project_id": project_id,
            "workflow_id": workflow_id,
            "knowledge_state": result.get("planner_knowledge_state"),
            "retrieval_id": retrieval_id,
            "retrieved_context_count": result.get(
                "planner_retrieved_context_count"
            ),
            "target_knowledge_retrieved": any(
                item.get("knowledge_id") == target_knowledge_id
                for item in sources
                if isinstance(item, dict)
            ),
            "planner_context_supplied": bool(
                planner_probe.calls and planner_probe.calls[0].get("context")
            ),
            "planner_generation_calls": len(planner_probe.calls),
            "persisted_workflow_id": retrieval.get("workflow_id") if retrieval else None,
            "persisted_agent": retrieval.get("agent_name") if retrieval else None,
            "trace_id": retrieval.get("trace_id") if retrieval else None,
            "span_id": retrieval.get("span_id") if retrieval else None,
        }
    finally:
        await observability.store.finalize_trace(
            root.trace_id,
            status="completed",
            ended_at=datetime.now(UTC).isoformat(),
            reason="manual_planner_knowledge_validation",
        )


async def main() -> int:
    database = workflow_event_store_path()
    target_knowledge_id = await ensure_target_knowledge(database)
    observability_store = ObservabilityStore(database)
    observability = ObservabilityService(observability_store)
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
            f"phase-7-planner-negative-{uuid4().hex[:8]}",
            executor=executor,
            observability=observability,
            knowledge_store=knowledge_store,
            target_knowledge_id=target_knowledge_id,
        )
    finally:
        await manager.disconnect_all()

    ok = (
        positive["knowledge_state"] == "available"
        and positive["target_knowledge_retrieved"] is True
        and positive["planner_context_supplied"] is True
        and positive["planner_generation_calls"] == 1
        and positive["persisted_workflow_id"] == positive["workflow_id"]
        and positive["persisted_agent"] == "Planner"
        and bool(positive["trace_id"])
        and bool(positive["span_id"])
        and negative["target_knowledge_retrieved"] is False
        and negative["planner_context_supplied"] is False
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
