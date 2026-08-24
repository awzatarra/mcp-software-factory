from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from api.services.observability_context import get_observability_context
from graph.failure_injection import raise_if_failure_injected
from graph.nodes import (
    GraphDependencies,
    _execute,
    approval_node,
    execute_fix_node,
    execute_tests_node,
    prepare_fix_node,
    prepare_test_request_node,
    read_failing_test_node,
    read_related_source_node,
)
from graph.observability import observed_node
from graph.subgraphs.testing_repair.knowledge import (
    QAKnowledgeContextBuilder,
    QAKnowledgeQueryBuilder,
)
from graph.subgraphs.testing_repair.repair_knowledge import (
    RepairKnowledgeContextBuilder,
    RepairKnowledgeQueryBuilder,
    repair_should_retrieve,
)
from graph.subgraphs.testing_repair.routers import (
    route_after_execute_fix,
    route_after_prepare_fix,
    route_after_read_failing_test,
    route_after_related_source,
    route_after_tests,
    route_testing_approval,
    route_testing_entry,
)
from graph.subgraphs.testing_repair.state import TestingRepairState
from streaming import EventStatus, WorkflowEventType, emit_workflow_event


Node = Callable[[TestingRepairState, GraphDependencies], Awaitable[dict[str, Any]]]
FAILURE_NODE_ALIASES = {"execute_tests": "run_tests", "execute_fix": "apply_fix"}
logger = logging.getLogger(__name__)


def _logged_node(
    name: str,
    node: Node,
    dependencies: GraphDependencies,
) -> Callable[[TestingRepairState], Awaitable[dict[str, Any]]]:
    async def invoke(state: TestingRepairState) -> dict[str, Any]:
        previous_node = state.get("last_completed_node")
        if previous_node:
            raise_if_failure_injected(previous_node)
        print(f"Subgraph node start: {name}")
        if dependencies.node_observer:
            dependencies.node_observer(name, "start")
        agent = None
        if name in {
            "prepare_qa_knowledge",
            "prepare_test_request",
            "execute_tests",
            "read_failing_test",
            "read_related_source",
        }:
            agent = "QA"
        elif name in {"prepare_repair_knowledge", "prepare_fix", "execute_fix"}:
            agent = "Repair"
        async with observed_node(dependencies, "testing_repair", name, agent=agent):
            updates = await node(state, dependencies)
        updates["last_completed_node"] = FAILURE_NODE_ALIASES.get(name, name)
        merged = dict(state)
        merged.update(updates)
        print(
            f"Subgraph node end: {name} "
            f"tests_executed={merged.get('tests_executed', False)} "
            f"tests_passed={merged.get('tests_passed', False)} "
            f"knowledge={merged.get('qa_knowledge_state', 'not_started')} "
            f"repair_phase={merged.get('repair_phase', 'not_started')} "
            f"repair_attempts={merged.get('repair_attempts', 0)}"
        )
        if dependencies.node_observer:
            dependencies.node_observer(name, "end")
        return updates

    return invoke


def _logged_router(source: str, router: Callable[[TestingRepairState], str]):
    def route(state: TestingRepairState) -> str:
        selected = router(state)
        print(f"Subgraph route: {source} -> {selected}")
        return selected

    return route


def _entry_logger(dependencies: GraphDependencies):
    async def enter(state: TestingRepairState) -> dict[str, Any]:
        print("Parent graph node start: testing_repair")
        if dependencies.node_observer:
            dependencies.node_observer("testing_repair", "start")
        emit_workflow_event(
            WorkflowEventType.TESTING_STARTED,
            source="testing_repair_subgraph",
            stage="testing_repair",
            status=EventStatus.RUNNING,
            data={"namespace": ["testing_repair"], "subgraph": "testing_repair", "node": "_entry"},
        )
        return {}

    return enter


def _exit_logger(dependencies: GraphDependencies):
    async def leave(state: TestingRepairState) -> dict[str, Any]:
        print("Parent graph node end: testing_repair")
        if dependencies.node_observer:
            dependencies.node_observer("testing_repair", "end")
        passed = state.get("tests_passed") is True
        emit_workflow_event(
            WorkflowEventType.TESTING_COMPLETED if passed else WorkflowEventType.TESTING_FAILED,
            source="testing_repair_subgraph",
            stage="testing_repair",
            status=EventStatus.COMPLETED if passed else EventStatus.FAILED,
            data={
                "namespace": ["testing_repair"],
                "subgraph": "testing_repair",
                "node": "_exit",
                "tests_passed": passed,
                "knowledge_retrieval_used": state.get(
                    "qa_knowledge_retrieval_used", False
                ),
                "retrieved_context_count": state.get(
                    "qa_retrieved_context_count", 0
                ),
                "retrieval_id": state.get("qa_knowledge_retrieval_id"),
            },
        )
        return {}

    return leave


def build_testing_repair_subgraph(dependencies: GraphDependencies):
    knowledge_query_builder = QAKnowledgeQueryBuilder()
    knowledge_context_builder = QAKnowledgeContextBuilder()
    repair_query_builder = RepairKnowledgeQueryBuilder()
    repair_context_builder = RepairKnowledgeContextBuilder()

    async def _retrieve_knowledge(
        *,
        state: TestingRepairState,
        graph_dependencies: GraphDependencies,
        request: Any,
        project_id: str,
        agent_name: str,
        node_name: str,
        context_builder: Any,
        prefix: str,
    ) -> dict[str, Any]:
        base_updates: dict[str, Any] = {
            f"{prefix}_knowledge_query": request.query,
            f"{prefix}_knowledge_retrieval_id": None,
            f"{prefix}_knowledge_context": None,
            f"{prefix}_knowledge_sources": [],
            f"{prefix}_knowledge_retrieval_used": False,
            f"{prefix}_retrieved_context_count": 0,
            f"{prefix}_knowledge_context_tokens": 0,
        }
        if not project_id:
            logger.warning("%s knowledge retrieval skipped: project scope is unavailable", agent_name)
            return {**base_updates, f"{prefix}_knowledge_state": "unavailable"}
        context = get_observability_context()
        arguments: dict[str, Any] = {
            "query": request.query,
            "project_id": project_id,
            "knowledge_types": list(request.knowledge_types),
            "limit": request.top_k,
            "agent_name": agent_name,
            "branch_id": context.branch_id,
        }
        correlation = {
            "workflow_id": context.workflow_id,
            "trace_id": context.trace_id,
            "parent_span_id": context.span_id,
        }
        arguments.update({key: value for key, value in correlation.items() if value})
        try:
            outcome = await _execute(
                "knowledge__get_relevant_context",
                arguments,
                state,  # type: ignore[arg-type]
                graph_dependencies,
                node_name=node_name,
                operation="knowledge_retrieval",
            )
            payload = outcome.payload
            if outcome.is_error or not isinstance(payload, dict) or payload.get("degraded"):
                reason = payload.get("failure_type") if isinstance(payload, dict) else "invalid_response"
                logger.warning(
                    "%s knowledge retrieval unavailable project=%s reason=%s",
                    agent_name,
                    project_id,
                    reason,
                )
                return {**base_updates, f"{prefix}_knowledge_state": "unavailable"}
            raw_results = payload.get("results", [])
            results = (
                [item for item in raw_results if isinstance(item, dict)]
                if isinstance(raw_results, list)
                else []
            )
            built = context_builder.build(results, top_k=request.top_k)
            retrieval_id = payload.get("retrieval_id")
            retrieval_id = str(retrieval_id) if retrieval_id else None
            if not built.sources:
                return {
                    **base_updates,
                    f"{prefix}_knowledge_retrieval_id": retrieval_id,
                    f"{prefix}_knowledge_state": "empty",
                }
            return {
                **base_updates,
                f"{prefix}_knowledge_retrieval_id": retrieval_id,
                f"{prefix}_knowledge_context": built.context,
                f"{prefix}_knowledge_sources": [dict(source) for source in built.sources],
                f"{prefix}_knowledge_state": "available",
                f"{prefix}_knowledge_retrieval_used": True,
                f"{prefix}_retrieved_context_count": len(built.sources),
                f"{prefix}_knowledge_context_tokens": built.token_count,
            }
        except Exception as exc:
            logger.warning(
                "%s knowledge retrieval failed open project=%s error=%s",
                agent_name,
                project_id,
                type(exc).__name__,
            )
            return {**base_updates, f"{prefix}_knowledge_state": "unavailable"}

    def _project_id(state: TestingRepairState) -> str:
        analysis = state.get("requirement_analysis") or {}
        analysis_project = analysis.get("project_name") if isinstance(analysis, dict) else None
        return str(
            state.get("project_name")
            or analysis_project
            or state.get("created_project_name")
            or ""
        ).strip()

    async def prepare_qa_knowledge(
        state: TestingRepairState,
        graph_dependencies: GraphDependencies,
    ) -> dict[str, Any]:
        request = knowledge_query_builder.build(state)
        if (
            state.get("qa_knowledge_query") == request.query
            and state.get("qa_knowledge_state", "not_started")
            in {"available", "empty", "unavailable"}
        ):
            return {}
        return await _retrieve_knowledge(
            state=state,
            graph_dependencies=graph_dependencies,
            request=request,
            project_id=_project_id(state),
            agent_name="QA",
            node_name="prepare_qa_knowledge",
            context_builder=knowledge_context_builder,
            prefix="qa",
        )

    async def prepare_repair_knowledge(
        state: TestingRepairState,
        graph_dependencies: GraphDependencies,
    ) -> dict[str, Any]:
        if not repair_should_retrieve(state):
            return {}
        request = repair_query_builder.build(state)
        if (
            state.get("repair_knowledge_query") == request.query
            and state.get("repair_knowledge_state", "not_started")
            in {"available", "empty", "unavailable"}
        ):
            return {}
        return await _retrieve_knowledge(
            state=state,
            graph_dependencies=graph_dependencies,
            request=request,
            project_id=_project_id(state),
            agent_name="Repair",
            node_name="prepare_repair_knowledge",
            context_builder=repair_context_builder,
            prefix="repair",
        )

    builder = StateGraph(TestingRepairState)
    builder.add_node("_entry", _entry_logger(dependencies))
    builder.add_node(
        "prepare_qa_knowledge",
        _logged_node("prepare_qa_knowledge", prepare_qa_knowledge, dependencies),
    )
    builder.add_node(
        "prepare_repair_knowledge",
        _logged_node(
            "prepare_repair_knowledge", prepare_repair_knowledge, dependencies
        ),
    )
    builder.add_node("prepare_test_request", _logged_node("prepare_test_request", prepare_test_request_node, dependencies))
    builder.add_node("approval", _logged_node("approval", approval_node, dependencies))
    builder.add_node("execute_tests", _logged_node("execute_tests", execute_tests_node, dependencies))
    builder.add_node("read_failing_test", _logged_node("read_failing_test", read_failing_test_node, dependencies))
    builder.add_node("read_related_source", _logged_node("read_related_source", read_related_source_node, dependencies))
    builder.add_node("prepare_fix", _logged_node("prepare_fix", prepare_fix_node, dependencies))
    builder.add_node("execute_fix", _logged_node("execute_fix", execute_fix_node, dependencies))
    builder.add_node("_exit", _exit_logger(dependencies))

    builder.add_edge(START, "_entry")
    builder.add_conditional_edges(
        "_entry",
        _logged_router("_entry", route_testing_entry),
        {
            "tests": "prepare_qa_knowledge",
            "read_failing_test": "read_failing_test",
            "read_related_source": "read_related_source",
            "prepare_fix": "prepare_repair_knowledge",
        },
    )
    builder.add_edge("prepare_qa_knowledge", "prepare_test_request")
    builder.add_edge("prepare_repair_knowledge", "prepare_fix")
    builder.add_edge("prepare_test_request", "approval")
    builder.add_conditional_edges(
        "approval",
        _logged_router("approval", route_testing_approval),
        {"execute_tests": "execute_tests", "execute_fix": "execute_fix", "rejected": "_exit"},
    )
    builder.add_conditional_edges(
        "execute_tests",
        _logged_router("execute_tests", route_after_tests),
        {
            "passed": "_exit",
            "repair": "read_failing_test",
            "no_tests_collected": "_exit",
            "infrastructure_failed": "_exit",
            "repair_limit_reached": "_exit",
        },
    )
    builder.add_conditional_edges(
        "read_failing_test",
        _logged_router("read_failing_test", route_after_read_failing_test),
        {"continue": "read_related_source", "failed": "_exit"},
    )
    builder.add_conditional_edges(
        "read_related_source",
        _logged_router("read_related_source", route_after_related_source),
        {
            "prepare_fix": "prepare_repair_knowledge",
            "retry_read": "read_related_source",
            "finalize": "_exit",
        },
    )
    builder.add_conditional_edges(
        "prepare_fix",
        _logged_router("prepare_fix", route_after_prepare_fix),
        {"approval": "approval", "failed": "_exit"},
    )
    builder.add_conditional_edges(
        "execute_fix",
        _logged_router("execute_fix", route_after_execute_fix),
        {"rerun_tests": "prepare_qa_knowledge", "cancelled": "_exit", "failed": "_exit"},
    )
    builder.add_edge("_exit", END)
    return builder.compile()
