from __future__ import annotations

import os
import json
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import Request
from openai import AsyncOpenAI

from api.services.approval_service import ApprovalLockRegistry, ApprovalService
from api.services.event_broker import (
    WorkflowEventBroker,
    WorkflowEventForwarder,
)
from api.services.sse_service import WorkflowSseService
from api.services.workflow_query_service import WorkflowQueryService
from api.services.workflow_metadata_store import WorkflowMetadataStore
from api.services.workflow_registry import WorkflowRegistry
from api.services.workflow_runner import WorkflowRunner
from api.services.project_explorer_service import ProjectExplorerService
from api.services.workflow_execution_service import WorkflowExecutionService
from api.services.workflow_evaluation_service import WorkflowEvaluationService
from api.services.workflow_evaluation_store import WorkflowEvaluationStore
from api.services.workflow_learning_store import WorkflowLearningStore
from api.services.dashboard_store import DashboardMetricStore
from api.services.dashboard_service import DashboardService
from api.services.alert_store import AlertStore
from api.services.alert_service import (
    AlertEvaluationService,
    EVENT_RULES,
    PeriodicAlertEvaluator,
    periodic_evaluator_from_env,
)
from api.services.notification_store import NotificationStore
from api.services.notification_service import (
    NotificationDispatcher, NotificationDeliveryWorker, NotificationService,
    worker_from_env,
)
from api.services.observability_service import ObservabilityService
from api.services.observability_store import ObservabilityStore
from api.services.llm_observability import ObservableOpenAIClient
from api.services.llm_cost_store import LLMCostStore
from api.services.llm_cost_service import LLMCostService
from api.services.llm_finops_alert_lifecycle import LLMFinOpsAlertLifecycleService
from api.services.agent_evaluation_store import AgentEvaluationStore
from api.services.agent_evaluation_service import AgentEvaluationService
from api.services.planner_calibration_service import PlannerCalibrationService
from api.services.planner_policy_proposal_store import PlannerPolicyProposalStore
from api.services.planner_policy_runtime_store import PlannerPolicyRuntimeStore
from api.services.planner_recommendation_review_store import PlannerRecommendationReviewStore
from api.services.knowledge_providers import LLMCostKnowledgeFinOps, SQLiteVectorStore
from api.services.knowledge_service import KnowledgeService
from api.services.knowledge_store import KnowledgeStore
from api.services.git_service import GitService
from api.services.git_store import GitAuditStore
from api.services.ci_service import CIPipelineService
from api.services.ci_store import CIPipelineStore
from api.services.ci_analytics_service import CIAnalyticsService
from clients.mcp_manager import MCPClientManager
from graph.builder import build_software_factory_graph
from graph.checkpointing import create_sqlite_checkpointer
from graph.nodes import GraphDependencies
from graph.persistence_service import WorkflowPersistenceService
from host import (
    BASE_INSTRUCTIONS,
    SENSITIVE_TOOLS,
    SoftwareFactoryHost,
    build_clients,
    load_planning_resources,
)
from streaming import (
    DurableWorkflowEventEmitter,
    SQLiteWorkflowEventStore,
    WorkflowEventFactory,
)
from streaming.store import WorkflowEventStore
from tool_executor import HostToolExecutor
from servers.filesystem_server import get_workspace_root


@dataclass(frozen=True)
class ApiServices:
    registry: WorkflowRegistry
    broker: WorkflowEventBroker
    runner: WorkflowRunner
    query: WorkflowQueryService
    approvals: ApprovalService
    sse: WorkflowSseService
    persistence: WorkflowPersistenceService
    forwarder: WorkflowEventForwarder
    event_store: WorkflowEventStore
    project_explorer: ProjectExplorerService
    execution: WorkflowExecutionService
    evaluation: WorkflowEvaluationService
    dashboard: DashboardService | None = None
    alerts: AlertEvaluationService | None = None
    notifications: NotificationService | None = None
    observability: ObservabilityService | None = None
    llm_costs: LLMCostService | None = None
    agent_evaluations: AgentEvaluationService | None = None
    planner_calibration: PlannerCalibrationService | None = None
    knowledge: KnowledgeService | None = None
    git: GitService | None = None
    ci: CIPipelineService | None = None
    ci_analytics: CIAnalyticsService | None = None


def get_services(request: Request) -> ApiServices:
    return request.app.state.services


@asynccontextmanager
async def build_api_services() -> AsyncIterator[ApiServices]:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY no está configurada.")
    model = os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
    async with AsyncExitStack() as stack:
        event_store = SQLiteWorkflowEventStore()
        await event_store.initialize()
        workflow_learning_store = WorkflowLearningStore(event_store.database_path)
        await workflow_learning_store.initialize()
        observability_store = ObservabilityStore(event_store.database_path)
        observability = ObservabilityService(observability_store)
        await observability.initialize()
        llm_cost_store = LLMCostStore(event_store.database_path)
        llm_costs = LLMCostService(llm_cost_store, observability_store)
        await llm_costs.initialize()
        manager = MCPClientManager(build_clients(observability), SENSITIVE_TOOLS)
        await manager.connect_all()
        stack.push_async_callback(manager.disconnect_all)
        resources = await load_planning_resources(manager)
        instructions = BASE_INSTRUCTIONS
        if resources:
            instructions += "\n\nAvailable MCP resources:\n" + resources
        client = ObservableOpenAIClient(AsyncOpenAI(api_key=api_key), observability, llm_costs)
        host = SoftwareFactoryHost(manager, client, model, instructions)
        ci = CIPipelineService(
            get_workspace_root(),
            store=CIPipelineStore(event_store.database_path),
            observability=observability,
        )
        await ci.initialize()
        ci_analytics = CIAnalyticsService(ci.store)
        graph_dependencies = GraphDependencies(
            tool_executor=HostToolExecutor(host, observability),
            openai_client=client,
            model=model,
            instructions=instructions,
            observability=observability,
            workflow_learning_store=workflow_learning_store,
            ci_service=ci,
        )
        checkpointer = await stack.enter_async_context(create_sqlite_checkpointer())
        graph = build_software_factory_graph(
            graph_dependencies,
            checkpointer=checkpointer,
        )
        emitter = DurableWorkflowEventEmitter(event_store)
        event_factory = WorkflowEventFactory()
        registry = WorkflowRegistry()
        broker = WorkflowEventBroker(store=event_store)
        forwarder = WorkflowEventForwarder(
            emitter,
            broker,
            events_are_persisted=True,
        )
        await forwarder.start()
        persistence = WorkflowPersistenceService(
            graph,
            streaming_enabled=True,
            event_emitter=emitter,
            event_factory=event_factory,
        )
        metadata_store = WorkflowMetadataStore(event_store.database_path)
        git = GitService(
            get_workspace_root(),
            audit_store=GitAuditStore(event_store.database_path),
            observability=observability,
            ci_service=ci,
        )
        await git.initialize()
        query = WorkflowQueryService(
            persistence,
            registry,
            event_store,
            metadata_store,
            observability,
            llm_costs,
            git,
        )
        await query.backfill_registry()
        project_explorer = ProjectExplorerService(
            query=query,
            metadata_store=metadata_store,
            workspace_root=get_workspace_root(),
        )
        execution = WorkflowExecutionService(
            persistence=persistence,
            event_store=event_store,
            metadata_store=metadata_store,
        )
        evaluation_store = WorkflowEvaluationStore(event_store.database_path)
        await evaluation_store.initialize()
        evaluation = WorkflowEvaluationService(
            execution_service=execution,
            store=evaluation_store,
        )
        dashboard_store = DashboardMetricStore(event_store.database_path)
        await dashboard_store.initialize()
        dashboard = DashboardService(
            store=dashboard_store,
            execution_service=execution,
            evaluation_service=evaluation,
        )
        alert_store = AlertStore(event_store.database_path)
        await alert_store.initialize()
        notification_store = NotificationStore(event_store.database_path)
        await notification_store.initialize()
        notification_dispatcher = NotificationDispatcher(notification_store)
        notification_worker = worker_from_env(notification_store)
        notifications = NotificationService(notification_store, notification_dispatcher, notification_worker)
        alerts = AlertEvaluationService(alert_store, execution, notification_dispatcher)
        llm_costs.set_alert_service(alerts)
        finops_lifecycle = LLMFinOpsAlertLifecycleService(llm_cost_store, observability_store, alerts)
        llm_costs.set_finops_lifecycle(finops_lifecycle)
        agent_evaluation_store = AgentEvaluationStore(event_store.database_path)
        heuristic_weights_raw=os.getenv("AGENT_EVALUATION_HEURISTIC_WEIGHTS")
        heuristic_weights=json.loads(heuristic_weights_raw) if heuristic_weights_raw else None
        agent_evaluations = AgentEvaluationService(
            agent_evaluation_store, observability=observability, llm_costs=llm_costs,
            openai_client=client, default_model=model, heuristic_dimension_weights=heuristic_weights,
        )
        await agent_evaluations.initialize()
        planner_calibration = PlannerCalibrationService(
            query=query,
            metadata_store=metadata_store,
            review_store=PlannerRecommendationReviewStore(event_store.database_path),
            proposal_store=PlannerPolicyProposalStore(event_store.database_path),
            policy_runtime_store=PlannerPolicyRuntimeStore(event_store.database_path),
        )
        if planner_calibration.review_store is not None:
            await planner_calibration.review_store.initialize()
        if planner_calibration.proposal_store is not None:
            await planner_calibration.proposal_store.initialize()
        if planner_calibration.policy_runtime_store is not None:
            await planner_calibration.policy_runtime_store.initialize()
        knowledge_store = KnowledgeStore(event_store.database_path)
        knowledge = KnowledgeService(
            knowledge_store, vector_store=SQLiteVectorStore(event_store.database_path),
            observability=observability, finops=LLMCostKnowledgeFinOps(llm_costs),
        )
        await knowledge.initialize()
        periodic_alerts = periodic_evaluator_from_env(alerts)

        async def evaluate_alert_event(event) -> None:
            await observability.ingest_workflow_event(event)
            if event.type.value not in EVENT_RULES:
                return
            branch_id = str(event.data.get("branch_id") or "original")
            await query.sync_snapshot(event.thread_id)
            await dashboard.upsert_workflow(event.thread_id)
            await alerts.evaluate_workflow(
                thread_id=event.thread_id,
                branch_id=branch_id,
                trigger_event=event,
            )

        forwarder.on_event = evaluate_alert_event

        async def sync_dashboard(thread_id: str) -> None:
            await query.sync_snapshot(thread_id)
            await dashboard.upsert_workflow(thread_id)
            await alerts.evaluate_workflow(thread_id=thread_id)

        async def resume_dashboard(thread_id: str) -> None:
            await query.mark_running(thread_id)
            await dashboard.upsert_workflow(thread_id)
            await alerts.evaluate_workflow(thread_id=thread_id)

        async def fail_dashboard(thread_id: str) -> None:
            await query.mark_failed(thread_id)
            await dashboard.upsert_workflow(thread_id)
            await alerts.evaluate_workflow(thread_id=thread_id)

        if os.getenv("DASHBOARD_SKIP_STARTUP_BACKFILL", "").casefold() != "true":
            await dashboard.backfill()
        if os.getenv("ALERT_SKIP_STARTUP_BACKFILL", "").casefold() != "true":
            await alerts.backfill()
        if periodic_alerts is not None:
            await periodic_alerts.start()
        if os.getenv("NOTIFICATION_WORKER_ENABLED", "true").casefold() == "true":
            await notification_worker.start()
        runner = WorkflowRunner(
            graph=graph,
            persistence=persistence,
            registry=registry,
            emitter=emitter,
            event_factory=event_factory,
            forwarder=forwarder,
            on_transition=sync_dashboard,
            on_resume=resume_dashboard,
            on_failure=fail_dashboard,
            observability=observability,
        )
        approvals = ApprovalService(
            persistence=persistence,
            registry=registry,
            runner=runner,
            locks=ApprovalLockRegistry(),
        )
        services = ApiServices(
            registry=registry,
            broker=broker,
            runner=runner,
            query=query,
            approvals=approvals,
            sse=WorkflowSseService(broker),
            persistence=persistence,
            forwarder=forwarder,
            event_store=event_store,
            project_explorer=project_explorer,
            execution=execution,
            evaluation=evaluation,
            dashboard=dashboard,
            alerts=alerts,
            notifications=notifications,
            observability=observability,
            llm_costs=llm_costs,
            agent_evaluations=agent_evaluations,
            planner_calibration=planner_calibration,
            knowledge=knowledge,
            git=git,
            ci=ci,
            ci_analytics=ci_analytics,
        )
        try:
            yield services
        finally:
            await notification_worker.close()
            if periodic_alerts is not None:
                await periodic_alerts.close()
            await registry.cancel_all()
            await forwarder.close()
            await broker.close()
            await event_store.close()
