from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from graph.builder import build_software_factory_graph
from graph.nodes import (
    GraphDependencies,
    build_final_summary,
    execute_fix_node,
    extract_related_source_path_from_test,
    finalize_node,
    inspect_workspace_node,
    prepare_fix_node,
    project_name_to_python_package,
    read_failing_test_node,
    read_related_source_node,
    run_tests_node,
    terminal_status_from_state,
)
from graph.persistence_service import WorkflowAlreadyCompletedError, WorkflowPersistenceService
from graph.routers import route_after_environment, route_after_intent, route_after_tests, route_after_workspace
from graph.runtime import WorkflowInterrupt, print_interrupt_request, run_software_factory_graph
from graph.state import SoftwareFactoryState, create_initial_state
from graph.supervisor.config import SupervisorDevelopmentConfig
from graph.supervisor.service import SupervisorService
from graph.subgraphs.implementation.test_validation import build_fastapi_health_test
from graph.subgraphs.planning.models import (
    AcceptanceCriterion,
    ImplementationTask,
    PlanningOutput,
    RequirementAnalysis,
)
from graph.subgraphs.testing_repair.builder import build_testing_repair_subgraph
from graph.subgraphs.testing_repair.routers import (
    UnknownTestingOperationError,
    route_after_execute_fix,
    route_after_prepare_fix,
    route_after_read_failing_test,
    route_after_related_source,
    route_after_tests as route_after_repair_tests,
    route_testing_approval,
)
from clients.tool_registry import ToolRegistry
from host import ExecutionState, SoftwareFactoryHost, determine_next_action, env_flag, print_workflow_snapshot
from tool_executor import HostToolExecutor, ToolExecutionOutcome


CREATE_REQUEST = (
    'Crea un proyecto FastAPI llamado medical-booking con un endpoint GET /health que devuelva {"status": "ok"}, '
    "agrega pruebas y valida que pasen."
)
REVIEW_REQUEST = "Revisa el proyecto medical-booking, ejecuta sus pruebas y corrige los errores encontrados."


class RecordingClient:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        return self.result


class RegistryManager:
    def __init__(self) -> None:
        self.registry = ToolRegistry()


class RejectingHost(SoftwareFactoryHost):
    async def request_approval(self, tool, arguments: dict[str, Any]) -> bool:
        return False


class FakeGraphToolExecutor:
    def __init__(
        self,
        *,
        project_exists: bool = False,
        fail_first_test: bool = False,
        infrastructure_failure: bool = False,
        prepare_environment_mcp_timeout: bool = False,
        tests_always_fail: bool = False,
        reject_create: bool = False,
    ) -> None:
        self.project_exists = project_exists
        self.fail_first_test = fail_first_test
        self.infrastructure_failure = infrastructure_failure
        self.prepare_environment_mcp_timeout = prepare_environment_mcp_timeout
        self.tests_always_fail = tests_always_fail
        self.reject_create = reject_create
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.test_runs = 0

    def openai_tool(self, public_tool_name: str) -> dict[str, Any]:
        return {"type": "function", "name": public_tool_name, "parameters": {"type": "object"}}

    async def execute(
        self,
        public_tool_name: str,
        arguments: dict[str, Any],
        *,
        state_context: SoftwareFactoryState,
        approval_mode: str = "prompt",
    ) -> ToolExecutionOutcome:
        self.calls.append((public_tool_name, arguments))
        updates: dict[str, Any] = {}
        payload: dict[str, Any] = {"success": True}
        if public_tool_name == "software_factory__analyze_requirement":
            updates["analysis_completed"] = True
        elif public_tool_name == "software_factory__create_tasks":
            updates["tasks_created"] = True
        elif public_tool_name == "filesystem__list_files":
            updates.update(
                workspace_inspected=True,
                project_exists=self.project_exists,
                created_project_name="medical-booking" if self.project_exists else None,
            )
        elif public_tool_name == "filesystem__create_project_structure":
            if self.reject_create:
                payload = {"success": False, "status": "rejected_by_user"}
                updates.update(
                    user_cancelled=True,
                    terminal_status="user_cancelled",
                    failure_type="user_rejected",
                    failure_stage="approval",
                    failure_message="El usuario rechazó la operación sensible.",
                )
            else:
                updates.update(project_created=True, created_project_name="medical-booking")
        elif public_tool_name == "testing__detect_test_framework":
            updates.update(detected_test_framework="pytest", expected_test_command=["python", "-m", "pytest"])
        elif public_tool_name == "testing__prepare_test_environment":
            if self.prepare_environment_mcp_timeout:
                payload = {
                    "success": False,
                    "status": "mcp_timeout",
                    "failure_type": "mcp_timeout",
                    "server": "testing",
                    "tool": "prepare_test_environment",
                    "message": "El MCP Server no respondió dentro del timeout configurado.",
                }
                updates.update(
                    environment_prepared=False,
                    test_infrastructure_failed=True,
                    terminal_status="infrastructure_failed",
                    failure_type="mcp_timeout",
                    failure_stage="prepare_test_environment",
                    failure_message="El MCP Server no respondió dentro del timeout configurado.",
                )
                return ToolExecutionOutcome(public_tool_name, arguments, payload, updates, is_error=True)
            if self.infrastructure_failure:
                payload = {"success": False, "failure_type": "dependency_installation_failure"}
                updates.update(
                    environment_prepared=False,
                    test_infrastructure_failed=True,
                    failure_type="dependency_installation_failure",
                    failure_stage="install_dependencies",
                    failure_message="dependency install failed",
                )
            else:
                updates.update(
                    environment_prepared=True,
                    dependencies_installed=True,
                    environment_python="workspace/medical-booking/.venv/Scripts/python.exe",
                    installed_fastapi_version="0.139.0",
                    installed_starlette_version="1.3.1",
                )
        elif public_tool_name == "testing__run_tests":
            self.test_runs += 1
            should_fail = self.tests_always_fail or (self.fail_first_test and self.test_runs == 1)
            if should_fail:
                payload = {"success": False, "failure_type": "test_failure"}
                updates.update(
                    tests_executed=True,
                    tests_passed=False,
                    test_failure_summary='tests/test_health.py:9 esperaba {"status": "healthy"}, pero devolvió {"status": "ok"}.',
                    failing_test_files=["tests/test_health.py"],
                    test_stdout="1 failed",
                    first_test_result_summary="1 failed",
                    repair_phase="read_failing_test",
                )
            else:
                updates.update(
                    tests_executed=True,
                    tests_passed=True,
                    test_stdout="1 passed",
                    final_test_result_summary="1 passed, 2 warnings",
                    actual_test_command=["workspace/medical-booking/.venv/Scripts/python.exe", "-m", "pytest"],
                    test_warning_count=2,
                    repair_phase="completed" if state_context.get("repair_attempts") else state_context.get("repair_phase", "not_started"),
                )
        elif public_tool_name == "filesystem__read_file":
            path = arguments["relative_path"]
            reads = list(state_context.get("files_read_during_repair", []))
            reads.append(path)
            updates["files_read_during_repair"] = reads
            if path.endswith("tests/test_health.py"):
                updates["repair_phase"] = "read_related_source"
                payload["content"] = 'assert response.json() == {"status": "healthy"}'
            else:
                updates["repair_phase"] = "apply_fix"
                payload["content"] = 'return {"status": "ok"}'
        elif public_tool_name == "filesystem__update_project_files":
            attempts = state_context.get("repair_attempts", 0) + 1
            updates.update(
                repair_attempts=attempts,
                repair_phase="rerun_tests",
                repair_decision="El endpoint ya cumplía el requerimiento original. Se corrigió el test.",
                repair_before='"status": "healthy"',
                repair_after='"status": "ok"',
                files_updated_during_repair=["tests/test_health.py"],
            )
        return ToolExecutionOutcome(public_tool_name, arguments, payload, updates)


async def fake_arguments(public_tool_name: str, state: SoftwareFactoryState) -> dict[str, Any]:
    if public_tool_name == "filesystem__create_project_structure":
        return {
            "project_name": "medical-booking",
            "files": [
                {
                    "path": "medical_booking/main.py",
                    "content": '@app.get("/health")\ndef health():\n    return {"status": "ok"}',
                },
                {
                    "path": "tests/test_health.py",
                    "content": build_fastapi_health_test(
                        "medical_booking", "/health", 200, {"status": "ok"}
                    ),
                },
                {"path": "requirements.txt", "content": "fastapi[standard]==0.139.0\npytest>=8,<9\n"},
            ],
        }
    return {
        "project_name": "medical-booking",
        "files": [{"path": "tests/test_health.py", "content": 'assert response.json() == {"status": "ok"}'}],
    }


def dependencies(
    executor: FakeGraphToolExecutor,
    sequence: list[str] | None = None,
    *,
    planning_service: Any | None = None,
) -> GraphDependencies:
    def observe(name: str, event: str) -> None:
        if sequence is not None and event == "start":
            sequence.append(name)

    return GraphDependencies(
        tool_executor=executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=fake_arguments,
        node_observer=observe,
        planning_service=planning_service,
    )


def interruptible_graph(deps: GraphDependencies):
    return build_software_factory_graph(deps, checkpointer=InMemorySaver())


def workflow_plan(tasks: list[ImplementationTask], project_name: str = "medical-booking") -> PlanningOutput:
    return PlanningOutput(
        analysis=RequirementAnalysis(
            objective="Crear API verificable.",
            project_name=project_name,
            project_type="fastapi",
            framework="fastapi",
            functional_requirements=['GET /health devuelve {"status": "ok"}.'],
            constraints=['Preservar {"status": "ok"}.'],
        ),
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1",
                description='GET /health responde {"status": "ok"}.',
                verification_method="pytest",
            )
        ],
        tasks=tasks,
    )


class RiskPlanningService:
    def __init__(self, plan: PlanningOutput) -> None:
        self.plan = plan
        self.refine_calls = 0

    async def analyze(self, state):
        return self.plan.analysis, self.plan.acceptance_criteria

    async def create_tasks(self, state):
        return self.plan.tasks

    async def refine(self, state):
        self.refine_calls += 1
        return self.plan


def low_risk_plan() -> PlanningOutput:
    return workflow_plan(
        [
            ImplementationTask(order=1, role="backend developer", title="Crear app FastAPI", description="Crear aplicacion FastAPI."),
            ImplementationTask(order=2, role="backend developer", title="Implementar GET /health", description='Implementar GET /health con {"status": "ok"}.', depends_on=[1]),
            ImplementationTask(order=3, role="QA", title="Crear tests", description="Crear tests/test_health.py.", depends_on=[2]),
        ]
    )


def medium_dependency_plan() -> PlanningOutput:
    plan = low_risk_plan()
    plan.tasks.append(
        ImplementationTask(order=4, role="developer", title="Crear requirements.txt", description="Crear requirements.txt para dependencias.", depends_on=[1])
    )
    return plan


def high_risk_plan() -> PlanningOutput:
    plan = low_risk_plan()
    plan.tasks.extend(
        [
            ImplementationTask(order=4, role="developer", title="Modificar authentication", description="Modificar authentication permissions.", depends_on=[2]),
            ImplementationTask(order=5, role="developer", title="Agregar database migration", description="Agregar database migration para schema change.", depends_on=[2]),
        ]
    )
    return plan


async def run_with_all_approvals(graph, message: str):
    result = await run_software_factory_graph(graph, message)
    approval_count = 0
    service = WorkflowPersistenceService(graph)
    while result.interrupted:
        approval_count += 1
        result = await service.approve(result.thread_id)
    return result, approval_count


@pytest.mark.asyncio
async def test_low_risk_planning_reaches_implementation_without_extra_approval() -> None:
    sequence: list[str] = []
    graph = interruptible_graph(
        dependencies(
            FakeGraphToolExecutor(),
            sequence,
            planning_service=RiskPlanningService(low_risk_plan()),
        )
    )

    result = await run_software_factory_graph(graph, CREATE_REQUEST, thread_id="risk-low")

    assert result.interrupted is True
    assert result.interrupts[0].value["operation"] == "create_project"
    assert result.final_state["planning_policy_decision"] == "allow"
    assert result.final_state["planning_approval_required"] is False
    assert result.final_state["planning_approval_status"] == "not_required"
    assert result.final_state["planning_quality_score"] >= 80
    assert result.final_state["planning_quality_level"] in {"good", "excellent"}
    assert result.final_state["planning_decision_confidence"] >= 0.80
    assert result.final_state["planning_result"]["quality"]["score"] == result.final_state["planning_quality_score"]
    assert "implementation" in sequence


@pytest.mark.asyncio
async def test_medium_sensitive_planning_interrupts_before_implementation() -> None:
    sequence: list[str] = []
    graph = interruptible_graph(
        dependencies(
            FakeGraphToolExecutor(),
            sequence,
            planning_service=RiskPlanningService(medium_dependency_plan()),
        )
    )

    result = await run_software_factory_graph(graph, CREATE_REQUEST, thread_id="risk-medium")

    assert result.interrupted is True
    assert result.interrupts[0].value["operation"] == "planning_risk_approval"
    assert result.final_state["planning_policy_decision"] == "require_approval"
    assert result.final_state["planning_approval_required"] is True
    assert result.final_state["planning_approval_status"] == "awaiting_approval"
    assert result.final_state["terminal_status"] == "pending"
    assert result.final_state["planning_decision_confidence"] <= 0.60
    assert "implementation" not in sequence


@pytest.mark.asyncio
async def test_high_and_critical_planning_interrupt_before_implementation() -> None:
    for plan_factory, thread_id in ((high_risk_plan, "risk-high"),):
        sequence: list[str] = []
        graph = interruptible_graph(
            dependencies(
                FakeGraphToolExecutor(),
                sequence,
                planning_service=RiskPlanningService(plan_factory()),
            )
        )

        result = await run_software_factory_graph(
            graph,
            CREATE_REQUEST + " Incluye cambios de authentication permissions y database migration.",
            thread_id=thread_id,
        )

        assert result.interrupted is True
        assert result.interrupts[0].value["operation"] == "planning_risk_approval"
        assert result.final_state["planning_risk_level"] in {"high", "critical"}
        assert "implementation" not in sequence


@pytest.mark.asyncio
async def test_planning_risk_approval_resumes_into_implementation() -> None:
    sequence: list[str] = []
    graph = interruptible_graph(
        dependencies(
            FakeGraphToolExecutor(),
            sequence,
            planning_service=RiskPlanningService(medium_dependency_plan()),
        )
    )
    initial = await run_software_factory_graph(graph, CREATE_REQUEST, thread_id="risk-approve")
    pending_confidence = initial.final_state["planning_decision_confidence"]

    result = await WorkflowPersistenceService(graph).approve(initial.thread_id, "Risk accepted")

    assert result.interrupted is True
    assert result.interrupts[0].value["operation"] == "create_project"
    assert result.final_state["planning_approval_status"] == "approved"
    assert result.final_state["planning_quality_score"] == initial.final_state["planning_quality_score"]
    assert result.final_state["planning_decision_confidence"] > pending_confidence
    assert result.final_state["pending_operation"] == "create_project"
    assert "implementation" in sequence


@pytest.mark.asyncio
async def test_planning_risk_reject_never_reaches_implementation() -> None:
    sequence: list[str] = []
    graph = interruptible_graph(
        dependencies(
            FakeGraphToolExecutor(),
            sequence,
            planning_service=RiskPlanningService(medium_dependency_plan()),
        )
    )
    initial = await run_software_factory_graph(graph, CREATE_REQUEST, thread_id="risk-reject")

    result = await WorkflowPersistenceService(graph).reject(initial.thread_id, "Too risky")

    assert result.interrupted is False
    assert result.final_state["planning_approval_status"] == "rejected"
    assert result.final_state["terminal_status"] == "user_cancelled"
    assert result.final_state["failure_type"] == "user_rejected"
    assert result.final_state["failure_stage"] == "planning_risk_approval"
    assert "implementation" not in sequence


@pytest.mark.asyncio
async def test_structured_streaming_covers_durable_approval_workflow() -> None:
    from streaming import (
        InMemoryWorkflowEventEmitter,
        WorkflowEventFactory,
        WorkflowEventType,
    )

    executor = FakeGraphToolExecutor()
    graph = interruptible_graph(dependencies(executor))
    emitter = InMemoryWorkflowEventEmitter()
    factory = WorkflowEventFactory()
    thread_id = "structured-streaming-workflow"
    result = await run_software_factory_graph(
        graph,
        CREATE_REQUEST,
        thread_id=thread_id,
        streaming_enabled=True,
        event_emitter=emitter,
        event_factory=factory,
    )
    persistence = WorkflowPersistenceService(
        graph,
        streaming_enabled=True,
        event_emitter=emitter,
        event_factory=factory,
    )
    approvals = 0
    approval_references = []
    while result.interrupted:
        approvals += 1
        reference = await persistence.load_current_pending_approval(thread_id)
        approval_references.append(reference)
        result = await persistence.approve(
            thread_id,
            "streaming test",
            expected_reference=reference,
        )

    events = emitter.get_events(thread_id)
    event_types = [event.type for event in events]
    assert approvals == 3
    assert len(set(approval_references)) == 3
    assert result.final_state["tests_passed"] is True
    assert event_types.count(WorkflowEventType.WORKFLOW_STARTED) == 1
    assert event_types.count(WorkflowEventType.WORKFLOW_RESUMED) == 3
    assert event_types.count(WorkflowEventType.APPROVAL_REQUIRED) == 3
    assert event_types.count(WorkflowEventType.APPROVAL_GRANTED) == 3
    assert event_types.count(WorkflowEventType.WORKFLOW_COMPLETED) == 1
    assert WorkflowEventType.WORKFLOW_FAILED not in event_types
    assert WorkflowEventType.TEST_RUN_STARTED in event_types
    assert WorkflowEventType.TEST_RUN_COMPLETED in event_types
    assert all(first.sequence < second.sequence for first, second in zip(events, events[1:]))
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert '"content"' not in "".join(event.model_dump_json() for event in events)
    call_names = [name for name, _arguments in executor.calls]
    assert call_names.count("filesystem__create_project_structure") == 1
    assert call_names.count("testing__prepare_test_environment") == 1
    assert call_names.count("testing__run_tests") == 1


@pytest.mark.asyncio
async def test_api_runner_resumes_all_approvals_without_repeating_sensitive_tools(
    tmp_path,
) -> None:
    import asyncio

    from api.services.approval_service import ApprovalLockRegistry, ApprovalService
    from api.services.event_broker import WorkflowEventBroker, WorkflowEventForwarder
    from api.services.workflow_registry import WorkflowExecutionStatus, WorkflowRegistry
    from api.services.workflow_runner import WorkflowRunner
    from streaming import (
        DurableWorkflowEventEmitter,
        SQLiteWorkflowEventStore,
        WorkflowEventFactory,
        WorkflowEventType,
    )

    async def wait_for_status(
        registry: WorkflowRegistry,
        thread_id: str,
        expected: WorkflowExecutionStatus,
    ) -> None:
        for _ in range(200):
            execution = await registry.get(thread_id)
            if execution is not None and execution.status == expected:
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"Workflow did not reach {expected}.")

    executor = FakeGraphToolExecutor()
    graph = interruptible_graph(dependencies(executor))
    event_store = SQLiteWorkflowEventStore(tmp_path / "api-runner-events.sqlite")
    await event_store.initialize()
    emitter = DurableWorkflowEventEmitter(event_store)
    factory = WorkflowEventFactory()
    broker = WorkflowEventBroker(store=event_store)
    forwarder = WorkflowEventForwarder(
        emitter,
        broker,
        events_are_persisted=True,
    )
    await forwarder.start()
    registry = WorkflowRegistry()
    persistence = WorkflowPersistenceService(
        graph,
        streaming_enabled=True,
        event_emitter=emitter,
        event_factory=factory,
    )
    runner = WorkflowRunner(
        graph=graph,
        persistence=persistence,
        registry=registry,
        emitter=emitter,
        event_factory=factory,
        forwarder=forwarder,
    )
    approvals = ApprovalService(
        persistence=persistence,
        registry=registry,
        runner=runner,
        locks=ApprovalLockRegistry(),
    )
    thread_id = "api-runner-all-approvals"

    try:
        await runner.schedule_start(thread_id=thread_id, request=CREATE_REQUEST)
        await wait_for_status(registry, thread_id, WorkflowExecutionStatus.WAITING)
        for index in range(3):
            await approvals.resolve(
                thread_id=thread_id,
                approved=True,
                reason="API test",
            )
            expected = (
                WorkflowExecutionStatus.COMPLETED
                if index == 2
                else WorkflowExecutionStatus.WAITING
            )
            await wait_for_status(registry, thread_id, expected)
        await forwarder.flush()

        call_names = [name for name, _arguments in executor.calls]
        assert call_names.count("filesystem__create_project_structure") == 1
        assert call_names.count("testing__prepare_test_environment") == 1
        assert call_names.count("testing__run_tests") == 1
        event_types = [item.type for item in await broker.get_history(thread_id)]
        assert event_types.count(WorkflowEventType.WORKFLOW_COMPLETED) == 1
        assert WorkflowEventType.WORKFLOW_FAILED not in event_types
        stored_events = await event_store.get_events(thread_id)
        assert [event.sequence for event in stored_events] == list(
            range(1, len(stored_events) + 1)
        )
    finally:
        await registry.cancel_all()
        await forwarder.close()
        await event_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_field", ["checkpoint", "operation"])
async def test_stale_approval_reference_is_rejected_without_executing_tool(
    stale_field: str,
) -> None:
    from graph.persistence_service import PendingApprovalReference, StaleApprovalReference

    executor = FakeGraphToolExecutor()
    graph = interruptible_graph(dependencies(executor))
    result = await run_software_factory_graph(
        graph,
        CREATE_REQUEST,
        thread_id="stale-approval-workflow",
        streaming_enabled=False,
    )
    assert result.interrupted is True
    persistence = WorkflowPersistenceService(graph)
    current = await persistence.load_current_pending_approval(result.thread_id)
    stale = PendingApprovalReference(
        thread_id=current.thread_id,
        checkpoint_id="old-checkpoint" if stale_field == "checkpoint" else current.checkpoint_id,
        operation="run_tests" if stale_field == "operation" else current.operation,
        tool_name=current.tool_name,
    )

    with pytest.raises(StaleApprovalReference):
        await persistence.approve(
            result.thread_id,
            expected_reference=stale,
        )

    call_names = [name for name, _arguments in executor.calls]
    assert "filesystem__create_project_structure" not in call_names


@pytest.mark.asyncio
async def test_rejected_approval_never_emits_granted_or_executes_tool() -> None:
    from streaming import (
        InMemoryWorkflowEventEmitter,
        WorkflowEventFactory,
        WorkflowEventType,
    )

    executor = FakeGraphToolExecutor()
    graph = interruptible_graph(dependencies(executor))
    emitter = InMemoryWorkflowEventEmitter()
    factory = WorkflowEventFactory()
    thread_id = "rejected-streaming-approval"
    result = await run_software_factory_graph(
        graph,
        CREATE_REQUEST,
        thread_id=thread_id,
        streaming_enabled=True,
        event_emitter=emitter,
        event_factory=factory,
    )
    persistence = WorkflowPersistenceService(
        graph,
        streaming_enabled=True,
        event_emitter=emitter,
        event_factory=factory,
    )
    reference = await persistence.load_current_pending_approval(thread_id)
    result = await persistence.reject(
        thread_id,
        "No aprobar",
        expected_reference=reference,
    )

    event_types = [event.type for event in emitter.get_events(thread_id)]
    assert result.final_state["terminal_status"] == "user_cancelled"
    assert WorkflowEventType.APPROVAL_REJECTED in event_types
    assert WorkflowEventType.APPROVAL_GRANTED not in event_types
    assert "filesystem__create_project_structure" not in [
        name for name, _arguments in executor.calls
    ]


def successful_creation_state() -> SoftwareFactoryState:
    state = create_initial_state(CREATE_REQUEST)
    state.update(
        project_name="medical-booking",
        created_project_name="medical-booking",
        project_created=True,
        environment_prepared=True,
        dependencies_installed=True,
        detected_test_framework="pytest",
        environment_python=r"C:\workspace\medical-booking\.venv\Scripts\python.exe",
        tests_executed=True,
        tests_passed=True,
        final_test_result_summary="1 passed, 2 warnings",
        test_warning_count=2,
    )
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_path", "content", "repair_before", "repair_after", "expected_after"),
    [
        (
            "medical_booking/main.py",
            'return {"status": "healthy"}',
            '"status": "ok"',
            '"status": "healthy"',
            '"status": "healthy"',
        ),
        (
            "tests/test_health.py",
            'assert response.json() == {"status": "ok"}',
            '"status": "healthy"',
            None,
            '"status": "ok"',
        ),
    ],
)
async def test_prepare_fix_preview_shows_complete_before_and_after(
    file_path: str,
    content: str,
    repair_before: str,
    repair_after: str | None,
    expected_after: str,
) -> None:
    async def resolver(public_tool_name: str, state: SoftwareFactoryState) -> dict[str, Any]:
        return {"project_name": "medical-booking", "files": [{"path": file_path, "content": content}]}

    deps = GraphDependencies(
        tool_executor=FakeGraphToolExecutor(),
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=resolver,
    )
    state = create_initial_state(REVIEW_REQUEST)
    state.update(
        project_name="medical-booking",
        repair_phase="apply_fix",
        failing_test_content='assert response.json() == {"status": "healthy"}',
        related_source_content='return {"status": "ok"}',
        test_failure_summary="assertion failed",
        repair_before=repair_before,
        repair_after=repair_after,
        fork_origin_checkpoint_id="origin" if file_path.endswith("main.py") else None,
    )

    updates = await prepare_fix_node(state, deps)
    preview = updates["pending_approval_preview"]

    assert preview and preview["before"] == repair_before
    assert preview["after"] == expected_after


@pytest.mark.parametrize(
    ("preview", "expected_lines"),
    [
        (
            {"before": '"status": "ok"', "after": '"status": "healthy"'},
            ['Antes: "status": "ok"', 'Después: "status": "healthy"'],
        ),
        ({"before": None, "after": None}, ["Antes: n/a", "Después: n/a"]),
    ],
)
def test_interrupt_presentation_shows_before_after_or_na(
    preview: dict[str, Any],
    expected_lines: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_interrupt_request(
        "fork-thread",
        WorkflowInterrupt(
            interrupt_id="approval",
            value={
                "public_tool_name": "filesystem__update_project_files",
                "project_name": "medical-booking",
                "preview": preview,
            },
        ),
    )

    output = capsys.readouterr().out
    assert all(line in output for line in expected_lines)


def test_interrupt_presentation_shows_normalized_dependencies(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_interrupt_request(
        "dependency-thread",
        WorkflowInterrupt(
            interrupt_id="dependency-interrupt",
            value={
                "public_tool_name": "filesystem__create_project_structure",
                "project_name": "medical-booking",
                "preview": {
                    "normalized_dependencies": [
                        "fastapi[standard]==0.139.0",
                        "pytest>=8,<9",
                    ]
                },
            },
        ),
    )

    output = capsys.readouterr().out
    assert "Dependencias normalizadas:" in output
    assert "- fastapi[standard]==0.139.0" in output
    assert "- pytest>=8,<9" in output


class StaticGraph:
    def __init__(self, state: SoftwareFactoryState) -> None:
        self.state = state

    async def ainvoke(self, initial_state: SoftwareFactoryState, config: dict[str, Any] | None = None) -> SoftwareFactoryState:
        return self.state


async def run_manual_repair_runtime(executor: FakeGraphToolExecutor) -> ExecutionState:
    state = ExecutionState(
        original_user_message=REVIEW_REQUEST,
        requested_project_name="medical-booking",
        workflow_intent="review_existing_project",
    )
    while True:
        action = determine_next_action(state)
        if action == "finalize":
            return state
        if action == "fix_failed_tests":
            action = {
                "read_failing_test": "filesystem__read_file",
                "read_related_source": "filesystem__read_file",
                "apply_fix": "filesystem__update_project_files",
            }[state.repair_phase]
        if action == "filesystem__list_files":
            arguments = {"relative_path": "."}
        elif action == "filesystem__read_file":
            relative = "tests/test_health.py" if state.repair_phase == "read_failing_test" else "medical_booking/main.py"
            arguments = {"relative_path": f"medical-booking/{relative}"}
        elif action == "filesystem__update_project_files":
            arguments = await fake_arguments(action, {})
        else:
            arguments = {"project_name": "medical-booking"}
        graph_state: SoftwareFactoryState = {
            key: sorted(value) if isinstance(value, set) else value
            for key, value in vars(state).items()
            if key in SoftwareFactoryState.__annotations__
        }  # type: ignore[assignment]
        graph_state["project_name"] = state.requested_project_name
        outcome = await executor.execute(action, arguments, state_context=graph_state)
        for key, value in outcome.state_updates.items():
            if hasattr(state, key):
                if key in {"files_read_during_repair", "files_updated_during_repair"}:
                    value = set(value)
                setattr(state, key, value)


def test_initial_state_has_explicit_serializable_defaults() -> None:
    state = create_initial_state(CREATE_REQUEST)

    assert state["original_user_message"] == CREATE_REQUEST
    assert state["workflow_intent"] == "create_project"
    assert state["repair_phase"] == "not_started"
    assert state["repair_attempts"] == 0
    assert state["failing_test_files"] == []
    assert state["final_response"] == ""


@pytest.mark.parametrize(
    ("project_name", "expected"),
    [
        ("subgraph-booking", "subgraph_booking"),
        ("medical-booking", "medical_booking"),
        ("my-api", "my_api"),
        ("123-api", "_123_api"),
    ],
)
def test_project_name_to_python_package_returns_valid_identifier(project_name: str, expected: str) -> None:
    package = project_name_to_python_package(project_name)

    assert package == expected
    assert package.isidentifier()


@pytest.mark.parametrize("project_name", ["", "---", "!!!"])
def test_project_name_to_python_package_rejects_empty_result(project_name: str) -> None:
    with pytest.raises(ValueError):
        project_name_to_python_package(project_name)


def test_extract_related_source_path_from_test_uses_import_without_execution() -> None:
    content = "from subgraph_booking.main import app\nassert app is not None\n"

    assert extract_related_source_path_from_test(content) == "subgraph_booking/main.py"
    assert extract_related_source_path_from_test("from app.main import app") == "app/main.py"
    assert extract_related_source_path_from_test("this is not valid python") is None


class StaticOutcomeExecutor:
    def __init__(self, outcome: ToolExecutionOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def openai_tool(self, public_tool_name: str) -> dict[str, Any]:
        return {"type": "function", "name": public_tool_name, "parameters": {"type": "object"}}

    async def execute(self, public_tool_name, arguments, *, state_context, approval_mode="prompt"):
        self.calls.append((public_tool_name, arguments))
        return self.outcome


def node_dependencies(executor: Any, resolver=fake_arguments) -> GraphDependencies:
    return GraphDependencies(
        tool_executor=executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=resolver,
    )


@pytest.mark.asyncio
async def test_related_source_uses_import_and_normalizes_project_prefix() -> None:
    outcome = ToolExecutionOutcome(
        "filesystem__read_file",
        {},
        {"content": 'return {"status": "ok"}'},
        {},
    )
    executor = StaticOutcomeExecutor(outcome)
    state = create_initial_state("review")
    state.update(
        project_name="subgraph-booking",
        created_project_name="subgraph-booking",
        repair_phase="read_related_source",
        failing_test_content="from subgraph_booking.main import app",
        test_failure_summary="assertion failed",
    )

    updates = await read_related_source_node(state, node_dependencies(executor))

    assert executor.calls == [
        ("filesystem__read_file", {"relative_path": "subgraph-booking/subgraph_booking/main.py"})
    ]
    assert updates["related_source_file"] == "subgraph-booking/subgraph_booking/main.py"
    assert updates["repair_phase"] == "apply_fix"
    assert route_after_related_source({**state, **updates}) == "prepare_fix"


@pytest.mark.asyncio
async def test_failing_test_read_accepts_content_without_business_success_key() -> None:
    outcome = ToolExecutionOutcome(
        "filesystem__read_file",
        {},
        {"path": "subgraph-booking/tests/test_health.py", "content": "from subgraph_booking.main import app"},
        {"repair_phase": "read_related_source"},
    )
    state = create_initial_state("review")
    state.update(
        project_name="subgraph-booking",
        repair_phase="read_failing_test",
        failing_test_files=["tests/test_health.py"],
    )

    updates = await read_failing_test_node(state, node_dependencies(StaticOutcomeExecutor(outcome)))

    assert updates["failing_test_content"] == "from subgraph_booking.main import app"
    assert updates["repair_phase"] == "read_related_source"


@pytest.mark.asyncio
async def test_related_source_read_failure_stays_in_phase_and_never_prepares_fix() -> None:
    outcome = ToolExecutionOutcome(
        "filesystem__read_file",
        {},
        {"success": False, "status": "not_found", "message": "file does not exist"},
        {"repair_phase": "apply_fix"},
    )
    executor = StaticOutcomeExecutor(outcome)
    state = create_initial_state("review")
    state.update(
        project_name="subgraph-booking",
        repair_phase="read_related_source",
        failing_test_content="from subgraph_booking.main import app",
    )

    updates = await read_related_source_node(state, node_dependencies(executor))
    merged = {**state, **updates}

    assert updates["failure_type"] == "related_source_read_failed"
    assert updates["repair_phase"] == "read_related_source"
    assert route_after_related_source(merged) == "finalize"
    assert route_after_prepare_fix(merged) == "failed"
    assert "pending_tool_name" not in updates


@pytest.mark.asyncio
async def test_related_source_retries_once_with_project_name_fallback() -> None:
    failed = ToolExecutionOutcome(
        "filesystem__read_file",
        {},
        {"success": False, "message": "missing imported module"},
        {},
    )
    state = create_initial_state("review")
    state.update(
        project_name="subgraph-booking",
        repair_phase="read_related_source",
        failing_test_content="from app.main import app",
    )
    first = await read_related_source_node(state, node_dependencies(StaticOutcomeExecutor(failed)))

    assert first["related_source_candidates"] == ["app/main.py", "subgraph_booking/main.py"]
    assert route_after_related_source({**state, **first}) == "retry_read"

    success = ToolExecutionOutcome(
        "filesystem__read_file",
        {},
        {"success": True, "content": "app = FastAPI()"},
        {},
    )
    executor = StaticOutcomeExecutor(success)
    second = await read_related_source_node({**state, **first}, node_dependencies(executor))

    assert executor.calls[0][1]["relative_path"] == "subgraph-booking/subgraph_booking/main.py"
    assert second["repair_phase"] == "apply_fix"
    assert second["related_source_read_attempts"] == 2


@pytest.mark.asyncio
async def test_prepare_fix_rejects_incomplete_context_without_resolving_arguments() -> None:
    resolver_called = False

    async def resolver(public_tool_name: str, state: SoftwareFactoryState) -> dict[str, Any]:
        nonlocal resolver_called
        resolver_called = True
        return {}

    state = create_initial_state("review")
    state["repair_phase"] = "read_related_source"
    updates = await prepare_fix_node(state, node_dependencies(FakeGraphToolExecutor(), resolver))

    assert updates["failure_type"] == "repair_context_incomplete"
    assert route_after_prepare_fix({**state, **updates}) == "failed"
    assert resolver_called is False


@pytest.mark.asyncio
async def test_execute_fix_guard_rejection_is_not_success_or_rerun() -> None:
    outcome = ToolExecutionOutcome(
        "filesystem__update_project_files",
        {},
        {"success": False, "status": "repair_step_not_allowed", "message": "wrong phase"},
        {"repair_phase": "completed"},
    )
    state = create_initial_state("review")
    state.update(
        repair_phase="apply_fix",
        pending_operation="apply_fix",
        pending_tool_name="filesystem__update_project_files",
        pending_tool_arguments={"project_name": "subgraph-booking", "files": []},
        pending_approval_status="approved",
    )

    updates = await execute_fix_node(state, node_dependencies(StaticOutcomeExecutor(outcome)))
    merged = {**state, **updates}

    assert updates["failure_type"] == "repair_state_guard_rejected"
    assert updates["failure_stage"] == "execute_fix"
    assert route_after_execute_fix(merged) == "failed"
    assert merged["repair_phase"] == "apply_fix"


@pytest.mark.asyncio
async def test_execute_fix_success_enters_rerun_and_increments_attempts() -> None:
    outcome = ToolExecutionOutcome(
        "filesystem__update_project_files",
        {},
        {"success": True},
        {"repair_phase": "rerun_tests", "repair_attempts": 1, "files_updated_during_repair": ["tests/test_health.py"]},
    )
    state = create_initial_state("review")
    state.update(
        repair_phase="apply_fix",
        pending_operation="apply_fix",
        pending_tool_name="filesystem__update_project_files",
        pending_tool_arguments={"project_name": "subgraph-booking", "files": []},
        pending_approval_status="approved",
    )

    updates = await execute_fix_node(state, node_dependencies(StaticOutcomeExecutor(outcome)))

    assert updates["repair_phase"] == "rerun_tests"
    assert updates["repair_attempts"] == 1
    assert route_after_execute_fix({**state, **updates}) == "rerun_tests"


def test_langgraph_feature_flag_defaults_off_and_accepts_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_LANGGRAPH", raising=False)
    assert env_flag("USE_LANGGRAPH") is False
    monkeypatch.setenv("USE_LANGGRAPH", "true")
    assert env_flag("USE_LANGGRAPH") is True


def test_graph_compiles_without_checkpointer() -> None:
    graph = build_software_factory_graph(dependencies(FakeGraphToolExecutor()))

    assert graph.checkpointer is None


def test_testing_repair_subgraph_and_parent_have_expected_boundaries() -> None:
    deps = dependencies(FakeGraphToolExecutor())
    subgraph = build_testing_repair_subgraph(deps)
    parent = build_software_factory_graph(deps, checkpointer=InMemorySaver())
    subgraph_nodes = set(subgraph.get_graph().nodes)
    parent_nodes = set(parent.get_graph().nodes)
    internal_nodes = {
        "prepare_test_request",
        "execute_tests",
        "read_failing_test",
        "read_related_source",
        "prepare_repair_knowledge",
        "prepare_fix",
        "execute_fix",
    }

    assert subgraph.checkpointer is None
    assert internal_nodes | {"approval"} <= subgraph_nodes
    assert "testing_repair" in parent_nodes
    assert internal_nodes.isdisjoint(parent_nodes)
    assert "approval" not in parent_nodes
    assert "planning" in parent_nodes
    assert "analyze_requirement" not in parent_nodes
    assert "create_tasks" not in parent_nodes


def test_testing_approval_router_rejects_unknown_operation() -> None:
    state = create_initial_state("unknown")
    state.update(pending_approval_status="approved", pending_operation="create_project")

    with pytest.raises(UnknownTestingOperationError, match="desconocida"):
        route_testing_approval(state)


@pytest.mark.asyncio
async def test_missing_testing_preconditions_finalize_without_running_tests() -> None:
    class MissingFrameworkExecutor(FakeGraphToolExecutor):
        async def execute(self, public_tool_name, arguments, *, state_context, approval_mode="prompt"):
            outcome = await super().execute(
                public_tool_name,
                arguments,
                state_context=state_context,
                approval_mode=approval_mode,
            )
            if public_tool_name != "testing__detect_test_framework":
                return outcome
            updates = dict(outcome.state_updates)
            updates.update(detected_test_framework=None, expected_test_command=None)
            return ToolExecutionOutcome(
                outcome.public_tool_name,
                outcome.arguments,
                outcome.payload,
                updates,
                outcome.executed_sensitive,
            )

    executor = MissingFrameworkExecutor(project_exists=True)
    graph = interruptible_graph(dependencies(executor))

    result, approval_count = await run_with_all_approvals(graph, REVIEW_REQUEST)

    assert approval_count == 0
    assert result.final_state["terminal_status"] == "infrastructure_failed"
    assert result.final_state["failure_type"] == "framework_detection_failed"
    assert "testing__run_tests" not in [name for name, _ in executor.calls]


def test_final_summary_is_human_readable_without_full_state_or_null_repair_fields() -> None:
    summary = build_final_summary(successful_creation_state())

    assert "medical-booking" in summary
    assert "1 passed, 2 warnings" in summary
    assert "original_user_message" not in summary
    assert '"project_created"' not in summary
    assert "repair_decision" not in summary
    assert "repair_before" not in summary
    assert "repair_after" not in summary


@pytest.mark.asyncio
async def test_finalize_returns_only_summary_and_completed_terminal_status() -> None:
    state = successful_creation_state()

    updates = await finalize_node(state, dependencies(FakeGraphToolExecutor()))

    assert set(updates) == {"final_response", "terminal_status"}
    assert updates["terminal_status"] == "completed"
    assert updates["final_response"] == build_final_summary(state)


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"test_infrastructure_failed": True}, "infrastructure_failed"),
        ({"retry_limit_reached": True}, "repair_limit_reached"),
        ({"user_cancelled": True}, "user_cancelled"),
        ({"tests_executed": True, "tests_passed": False}, "tests_failed"),
    ],
)
def test_terminal_status_is_never_none_at_finalization(updates: dict[str, Any], expected: str) -> None:
    state = create_initial_state(REVIEW_REQUEST)
    state.update(updates)  # type: ignore[typeddict-item]

    assert terminal_status_from_state(state) == expected


def test_final_summary_includes_corrected_file_only_after_completed_repair() -> None:
    state = successful_creation_state()
    state.update(
        repair_phase="completed",
        files_updated_during_repair=["tests/test_health.py"],
        repair_decision="Se corrigió el test.",
        repair_before='"status": "healthy"',
        repair_after='"status": "ok"',
    )

    summary = build_final_summary(state)

    assert "Archivo corregido: tests/test_health.py" in summary
    assert "Se corrigió el test." in summary


@pytest.mark.asyncio
async def test_runtime_prints_only_final_response_in_normal_mode(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    state = successful_creation_state()
    state.update(final_response=build_final_summary(state), terminal_status="completed")
    monkeypatch.setenv("MCP_FACTORY_DEBUG", "false")

    result = await run_software_factory_graph(StaticGraph(state), CREATE_REQUEST, thread_id="thread-normal")

    assert result.final_state == state
    assert result.thread_id == "thread-normal"
    output = capsys.readouterr().out
    assert "Workflow thread: thread-normal" in output
    assert state["final_response"] in output
    assert "Workflow guardado." in output


@pytest.mark.asyncio
async def test_runtime_prints_full_state_in_debug(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    state = successful_creation_state()
    state.update(final_response=build_final_summary(state), terminal_status="completed")
    monkeypatch.setenv("MCP_FACTORY_DEBUG", "true")

    await run_software_factory_graph(StaticGraph(state), CREATE_REQUEST, thread_id="thread-debug")
    output = capsys.readouterr().out

    assert state["final_response"] in output
    assert "LangGraph final state (debug):" in output
    assert '"original_user_message"' in output


@pytest.mark.asyncio
async def test_finalize_log_uses_status_and_length_without_embedding_response(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("MCP_FACTORY_DEBUG", "false")
    graph = interruptible_graph(dependencies(FakeGraphToolExecutor()))

    result, _ = await run_with_all_approvals(graph, CREATE_REQUEST)
    state = result.final_state
    output = capsys.readouterr().out

    assert "Parent graph node end: finalize\n- terminal_status: completed\n- final_response_length:" in output
    final_log = output.split("Parent graph node end: finalize", 1)[1].split("Proyecto: medical-booking", 1)[0]
    assert state["final_response"] not in final_log


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"workflow_intent": "create_project"}, "create"),
        ({"workflow_intent": "review_existing_project"}, "review"),
    ],
)
def test_route_after_intent(state: SoftwareFactoryState, expected: str) -> None:
    assert route_after_intent(state) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"project_exists": True}, "existing"),
        ({"project_exists": False, "project_created": False}, "create"),
    ],
)
def test_route_after_workspace(state: SoftwareFactoryState, expected: str) -> None:
    assert route_after_workspace(state) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"environment_prepared": True}, "tests"),
        ({"environment_prepared": False, "test_infrastructure_failed": True}, "finalize"),
    ],
)
def test_route_after_environment(state: SoftwareFactoryState, expected: str) -> None:
    assert route_after_environment(state) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"tests_passed": True}, "passed"),
        ({"tests_passed": False, "repair_attempts": 0}, "repair"),
        ({"tests_passed": False, "test_infrastructure_failed": True}, "infrastructure_failed"),
        ({"tests_passed": False, "repair_attempts": 2}, "repair_limit_reached"),
    ],
)
def test_route_after_tests(state: SoftwareFactoryState, expected: str) -> None:
    assert route_after_tests(state) == expected


@pytest.mark.asyncio
async def test_nodes_return_partial_updates_without_clients_in_state() -> None:
    executor = FakeGraphToolExecutor(project_exists=True)
    deps = dependencies(executor)
    state = create_initial_state(REVIEW_REQUEST)
    state.update(project_name="medical-booking", workflow_intent="review_existing_project")

    workspace_update = await inspect_workspace_node(state, deps)
    state.update(workspace_update)
    test_update = await run_tests_node(state, deps)

    assert workspace_update == {
        "workspace_inspected": True,
        "project_exists": True,
        "created_project_name": "medical-booking",
    }
    assert test_update["tests_passed"] is True
    assert "tool_executor" not in state
    assert "openai_client" not in state


@pytest.mark.asyncio
async def test_host_tool_executor_reuses_host_normalization_and_state_updates() -> None:
    manager = RegistryManager()
    client = RecordingClient(
        {
            "success": True,
            "entries": [{"path": "medical-booking", "type": "directory", "size_bytes": 0}],
        }
    )
    manager.registry.register(
        server_name="filesystem",
        original_name="list_files",
        description="list",
        input_schema={"type": "object", "properties": {"relative_path": {"type": "string"}}},
        requires_approval=False,
        client=client,
    )
    host = SoftwareFactoryHost(manager, object(), "test-model", "")  # type: ignore[arg-type]
    state = create_initial_state(REVIEW_REQUEST)
    state.update(project_name="medical-booking", workflow_intent="review_existing_project")

    outcome = await HostToolExecutor(host).execute(
        "filesystem__list_files",
        {"relative_path": "."},
        state_context=state,
    )

    assert client.calls == [("list_files", {"relative_path": "."})]
    assert outcome.payload and outcome.payload["success"] is True
    assert outcome.state_updates["workspace_inspected"] is True
    assert outcome.state_updates["project_exists"] is True


@pytest.mark.asyncio
async def test_host_adapter_classifies_pytest_exit_code_five() -> None:
    manager = RegistryManager()
    client = RecordingClient(
        {
            "success": False,
            "exit_code": 5,
            "failure_type": "test_failure",
            "stdout": "no tests ran",
            "stderr": "",
        }
    )
    manager.registry.register(
        server_name="testing",
        original_name="run_tests",
        description="run",
        input_schema={
            "type": "object",
            "properties": {"project_name": {"type": "string"}},
            "required": ["project_name"],
        },
        requires_approval=True,
        client=client,
    )
    host = SoftwareFactoryHost(manager, object(), "test-model", "")  # type: ignore[arg-type]
    state = create_initial_state(REVIEW_REQUEST)
    state.update(project_name="medical-booking")

    outcome = await HostToolExecutor(host).execute(
        "testing__run_tests",
        {"project_name": "medical-booking"},
        state_context=state,
        approval_mode="already_approved",
    )

    assert outcome.state_updates["failure_type"] == "no_tests_collected"
    assert outcome.state_updates["failure_stage"] == "run_tests"
    assert outcome.state_updates["failure_message"] == "Pytest no encontrÃ³ pruebas ejecutables."
    assert outcome.state_updates["terminal_status"] == "tests_failed"
    merged = {**state, **outcome.state_updates}
    assert merged["failing_test_files"] == []
    assert route_after_repair_tests(merged) == "no_tests_collected"


@pytest.mark.asyncio
async def test_missing_failing_test_stops_before_related_source() -> None:
    sequence: list[str] = []
    graph = build_testing_repair_subgraph(dependencies(FakeGraphToolExecutor(), sequence))
    state = create_initial_state(REVIEW_REQUEST)
    state.update(
        project_name="medical-booking",
        fork_origin_checkpoint_id="before-read",
        repair_phase="read_failing_test",
        failing_test_files=[],
    )

    result = await graph.ainvoke(state)

    assert result["failure_type"] == "missing_failing_test_file"
    assert route_after_read_failing_test(result) == "failed"
    assert "read_failing_test" in sequence
    assert "read_related_source" not in sequence


@pytest.mark.asyncio
async def test_host_tool_executor_preserves_approval_rejection() -> None:
    manager = RegistryManager()
    client = RecordingClient({"success": True, "project_name": "medical-booking"})
    manager.registry.register(
        server_name="filesystem",
        original_name="create_project_structure",
        description="create",
        input_schema={
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
                "files": {"type": "array"},
            },
            "required": ["project_name", "files"],
        },
        requires_approval=True,
        client=client,
    )
    host = RejectingHost(manager, object(), "test-model", "")  # type: ignore[arg-type]
    state = create_initial_state(CREATE_REQUEST)
    state.update(
        project_name="medical-booking",
        analysis_completed=True,
        tasks_created=True,
        workspace_inspected=True,
    )

    outcome = await HostToolExecutor(host).execute(
        "filesystem__create_project_structure",
        {
            "project_name": "medical-booking",
            "files": [{"path": "README.md", "content": "demo"}],
        },
        state_context=state,
    )

    assert client.calls == []
    assert outcome.state_updates["user_cancelled"] is True
    assert outcome.state_updates["failure_stage"] == "approval"


@pytest.mark.asyncio
async def test_creation_graph_exact_sequence_and_final_state() -> None:
    sequence: list[str] = []
    executor = FakeGraphToolExecutor()
    graph = interruptible_graph(dependencies(executor, sequence))

    result, approval_count = await run_with_all_approvals(graph, CREATE_REQUEST)
    state = result.final_state

    assert sequence == [
        "detect_intent",
        "supervisor",
        "planning",
        "analyze_requirement",
        "prepare_planner_knowledge",
        "create_tasks",
        "validate_plan",
        "planning_risk_policy",
        "planning_judge",
        "planning_hybrid_evaluation",
        "supervisor",
        "inspect_workspace",
        "supervisor",
        "implementation",
        "prepare_developer_knowledge",
        "prepare_create_project",
        "normalize_dependencies",
        "validate_implementation",
        "approval",
        "approval",
        "execute_create_project",
        "detect_test_framework",
        "prepare_environment_request",
        "approval",
        "approval",
        "execute_prepare_environment",
        "supervisor",
        "testing_repair",
        "prepare_qa_knowledge",
        "prepare_test_request",
        "approval",
        "approval",
        "execute_tests",
        "supervisor",
        "finalize",
        "planner_evaluation",
        "agent_performance_evaluation",
        "failure_attribution",
        "extract_workflow_learnings",
        "submit_workflow_learnings",
    ]
    assert approval_count == 3
    assert state["project_created"] is True
    assert state["tests_passed"] is True
    assert "Proyecto: medical-booking" in state["final_response"]
    assert "Resultado: 1 passed, 2 warnings" in state["final_response"]
    assert state["terminal_status"] == "completed"
    assert state["planning_judge_status"] == "disabled"
    assert state["planning_result"]["judge"]["status"] == "disabled"
    assert state["planning_hybrid_evaluation"]["source"] == "deterministic_only"
    assert state["planning_result"]["hybrid_evaluation"]["agreement"] == "judge_unavailable"
    evaluation = state["planning_result"]["evaluation"]
    assert evaluation["outcome"] == "successful"
    assert evaluation["version"] == "7.7-v1"
    assert evaluation["quality_prediction"]["absolute_error"] is not None
    assert evaluation["confidence_calibration"]["label"]
    assert evaluation["risk_calibration"]["label"]
    assert state["planning_evaluation"] == evaluation
    assert state["agent_performance_evaluations"]["developer"]["metrics"]["first_pass_success"] is True
    assert state["agent_performance_evaluations"]["repair"]["status"] == "not_applicable"
    assert state["failure_attribution"]["status"] == "not_applicable"


@pytest.mark.asyncio
async def test_mandatory_supervisor_routing_progresses_without_model_or_false_loop() -> None:
    executor = FakeGraphToolExecutor()
    base_dependencies = dependencies(executor)
    supervisor_service = SupervisorService(
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        config=SupervisorDevelopmentConfig(
            development=True,
            force_model_decision=True,
            force_invalid_target=True,
        ),
    )
    graph = interruptible_graph(
        replace(base_dependencies, supervisor_service=supervisor_service)
    )

    result, _ = await run_with_all_approvals(graph, CREATE_REQUEST)
    state = result.final_state
    handoffs = state["handoff_history"]

    assert state["terminal_status"] == "completed"
    assert state["project_created"] is True
    assert state["environment_prepared"] is True
    assert state["tests_passed"] is True
    assert state["supervisor_invalid_decision_count"] == 0
    assert state["consecutive_invalid_decisions"] == 0
    assert "supervisor_invalid_decision" not in state["supervisor_errors"]
    assert "target_not_allowed" not in state["supervisor_errors"]
    assert "supervisor_loop_detected" not in state["supervisor_errors"]
    assert [
        (item["attempted_to"], item["selected_to"], item["executed_to"])
        for item in handoffs[:3]
    ] == [
        ("planning", "planning", "planning"),
        ("inspect_workspace", "inspect_workspace", "inspect_workspace"),
        ("implementation", "implementation", "implementation"),
    ]
    assert all(item["source"] == "deterministic" for item in handoffs)
    assert handoffs[3]["executed_to"] == "testing_repair"
    assert handoffs[4]["executed_to"] == "finalize"


@pytest.mark.asyncio
async def test_stagnant_loop_probe_finishes_before_any_subgraph_or_tool() -> None:
    sequence: list[str] = []
    executor = FakeGraphToolExecutor()
    base_dependencies = dependencies(executor, sequence)
    supervisor_service = SupervisorService(
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        config=SupervisorDevelopmentConfig(
            development=True,
            force_stagnant_loop=True,
        ),
    )
    graph = interruptible_graph(
        replace(base_dependencies, supervisor_service=supervisor_service)
    )

    result = await run_software_factory_graph(graph, CREATE_REQUEST)
    snapshot = await graph.aget_state(
        {"configurable": {"thread_id": result.thread_id}}
    )

    assert result.interrupted is False
    assert result.final_state["terminal_status"] == "supervisor_loop_detected"
    assert result.final_state["supervisor_stagnant_loop_probe_count"] == 3
    assert len(result.final_state["handoff_history"]) == 3
    assert tuple(snapshot.next) == ()
    assert not executor.calls
    assert "planning" not in sequence
    assert "implementation" not in sequence
    assert "testing_repair" not in sequence
    assert sequence == [
        "detect_intent",
        "supervisor",
        "supervisor",
        "supervisor",
        "finalize",
        "planner_evaluation",
        "agent_performance_evaluation",
        "failure_attribution",
        "extract_workflow_learnings",
        "submit_workflow_learnings",
    ]


@pytest.mark.asyncio
async def test_review_repair_graph_exact_sequence() -> None:
    sequence: list[str] = []
    executor = FakeGraphToolExecutor(project_exists=True, fail_first_test=True)
    graph = interruptible_graph(dependencies(executor, sequence))

    result, approval_count = await run_with_all_approvals(graph, REVIEW_REQUEST)
    state = result.final_state

    assert sequence == [
        "detect_intent",
        "supervisor",
        "inspect_workspace",
        "supervisor",
        "implementation",
        "detect_test_framework",
        "prepare_environment_request",
        "approval",
        "approval",
        "execute_prepare_environment",
        "supervisor",
        "testing_repair",
        "prepare_qa_knowledge",
        "prepare_test_request",
        "approval",
        "approval",
        "execute_tests",
        "read_failing_test",
        "read_related_source",
        "prepare_repair_knowledge",
        "prepare_fix",
        "approval",
        "approval",
        "execute_fix",
        "prepare_qa_knowledge",
        "prepare_test_request",
        "approval",
        "approval",
        "execute_tests",
        "supervisor",
        "finalize",
        "planner_evaluation",
        "agent_performance_evaluation",
        "failure_attribution",
        "extract_workflow_learnings",
        "submit_workflow_learnings",
    ]
    assert approval_count == 4
    tool_names = [name for name, _ in executor.calls]
    assert tool_names.count("testing__run_tests") == 2
    assert tool_names.count("filesystem__update_project_files") == 1
    assert "filesystem__create_project_structure" not in tool_names
    assert state["tests_passed"] is True
    assert state["repair_attempts"] == 1
    assert state["repair_before"] == '"status": "healthy"'
    assert state["repair_after"] == '"status": "ok"'


@pytest.mark.asyncio
async def test_subgraph_booking_repair_resolves_real_package_and_updates_only_test() -> None:
    class SubgraphBookingExecutor(FakeGraphToolExecutor):
        async def execute(self, public_tool_name, arguments, *, state_context, approval_mode="prompt"):
            outcome = await super().execute(
                public_tool_name,
                arguments,
                state_context=state_context,
                approval_mode=approval_mode,
            )
            if public_tool_name == "filesystem__list_files":
                outcome.state_updates["created_project_name"] = "subgraph-booking"
            elif public_tool_name == "filesystem__read_file" and arguments["relative_path"].endswith("tests/test_health.py"):
                outcome.payload["content"] = (
                    "from fastapi.testclient import TestClient\n"
                    "from subgraph_booking.main import app\n"
                    'assert client.get("/health").json() == {"status": "healthy"}\n'
                )
            return outcome

    async def resolver(public_tool_name: str, state: SoftwareFactoryState) -> dict[str, Any]:
        return {
            "project_name": "subgraph-booking",
            "files": [{"path": "tests/test_health.py", "content": 'assert response.json() == {"status": "ok"}'}],
        }

    sequence: list[str] = []
    executor = SubgraphBookingExecutor(project_exists=True, fail_first_test=True)
    deps = GraphDependencies(
        tool_executor=executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=resolver,
        node_observer=lambda name, event: sequence.append(name) if event == "start" else None,
    )
    graph = build_software_factory_graph(deps, checkpointer=InMemorySaver())

    result, _ = await run_with_all_approvals(
        graph,
        "Revisa el proyecto subgraph-booking, ejecuta sus pruebas y corrige los errores encontrados.",
    )
    calls = executor.calls
    read_paths = [arguments["relative_path"] for name, arguments in calls if name == "filesystem__read_file"]
    updates = [arguments for name, arguments in calls if name == "filesystem__update_project_files"]

    assert read_paths == [
        "subgraph-booking/tests/test_health.py",
        "subgraph-booking/subgraph_booking/main.py",
    ]
    assert len(updates) == 1
    assert [file["path"] for file in updates[0]["files"]] == ["tests/test_health.py"]
    assert executor.test_runs == 2
    assert result.final_state["repair_attempts"] == 1
    assert result.final_state["repair_phase"] == "completed"
    assert result.final_state["tests_passed"] is True
    assert sequence[-8:] == ["execute_tests", "supervisor", "finalize", "planner_evaluation", "agent_performance_evaluation", "failure_attribution", "extract_workflow_learnings", "submit_workflow_learnings"]


@pytest.mark.asyncio
async def test_infrastructure_failure_routes_directly_to_finalize() -> None:
    sequence: list[str] = []
    executor = FakeGraphToolExecutor(project_exists=True, infrastructure_failure=True)
    graph = interruptible_graph(dependencies(executor, sequence))

    result, _ = await run_with_all_approvals(graph, REVIEW_REQUEST)
    state = result.final_state

    assert sequence[-8:] == ["execute_prepare_environment", "supervisor", "finalize", "planner_evaluation", "agent_performance_evaluation", "failure_attribution", "extract_workflow_learnings", "submit_workflow_learnings"]
    assert "execute_tests" not in sequence
    assert state["test_infrastructure_failed"] is True


@pytest.mark.asyncio
async def test_prepare_environment_mcp_timeout_finishes_as_infrastructure_failure() -> None:
    sequence: list[str] = []
    executor = FakeGraphToolExecutor(prepare_environment_mcp_timeout=True)
    graph = interruptible_graph(dependencies(executor, sequence))

    result, _ = await run_with_all_approvals(graph, CREATE_REQUEST)
    state = result.final_state

    assert state["terminal_status"] == "infrastructure_failed"
    assert state["failure_type"] == "mcp_timeout"
    assert state["failure_stage"] == "prepare_test_environment"
    assert state["test_infrastructure_failed"] is True
    assert "execute_tests" not in sequence
    assert sequence[-8:] == [
        "execute_prepare_environment",
        "supervisor",
        "finalize",
        "planner_evaluation",
        "agent_performance_evaluation",
        "failure_attribution",
        "extract_workflow_learnings",
        "submit_workflow_learnings",
    ]


@pytest.mark.asyncio
async def test_rejected_approval_finishes_without_calling_later_tools(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sequence: list[str] = []
    executor = FakeGraphToolExecutor()
    graph = interruptible_graph(dependencies(executor, sequence))

    interrupted = await run_software_factory_graph(graph, CREATE_REQUEST)
    service = WorkflowPersistenceService(graph)
    result = await service.reject(interrupted.thread_id, "No deseo crear el proyecto")
    state = result.final_state
    snapshot = await service.get_snapshot(interrupted.thread_id)
    capsys.readouterr()
    print_workflow_snapshot(snapshot)
    snapshot_output = capsys.readouterr().out

    assert sequence[-8:] == ["approval", "supervisor", "finalize", "planner_evaluation", "agent_performance_evaluation", "failure_attribution", "extract_workflow_learnings", "submit_workflow_learnings"]
    assert "detect_test_framework" not in sequence
    assert result.interrupted is False
    assert state["user_cancelled"] is True
    assert state["terminal_status"] == "user_cancelled"
    assert state["pending_operation"] is None
    assert state["pending_tool_name"] is None
    assert state["pending_tool_arguments"] is None
    assert state["pending_approval_preview"] is None
    assert state["pending_approval_status"] == "rejected"
    assert state["approval_reason"] == "No deseo crear el proyecto"
    assert state["last_rejected_tool"] == "filesystem__create_project_structure"
    assert "Operación rechazada" in state["final_response"]
    assert "No deseo crear el proyecto" in state["final_response"]
    assert "Proyecto no modificado" in state["final_response"]
    assert "filesystem__create_project_structure" not in [name for name, _ in executor.calls]
    assert snapshot.interrupts == ()
    assert "Interrupted: false" in snapshot_output
    assert "Pending tool: None" in snapshot_output
    assert "Pending operation: None" in snapshot_output
    assert "Last rejected tool: filesystem__create_project_structure" in snapshot_output
    assert "Reason: No deseo crear el proyecto" in snapshot_output
    assert "Preview:" not in snapshot_output
    with pytest.raises(WorkflowAlreadyCompletedError):
        await service.approve(interrupted.thread_id)


@pytest.mark.asyncio
async def test_rejected_repair_does_not_update_files_and_finishes() -> None:
    executor = FakeGraphToolExecutor(project_exists=True, fail_first_test=True)
    graph = interruptible_graph(dependencies(executor))
    service = WorkflowPersistenceService(graph)
    result = await run_software_factory_graph(graph, REVIEW_REQUEST, thread_id="reject-repair")
    result = await service.approve(result.thread_id)
    result = await service.approve(result.thread_id)

    assert result.interrupted is True
    assert result.interrupts[0].value["operation"] == "apply_fix"
    result = await service.reject(result.thread_id, "No aplicar cambios automáticos")

    tool_names = [name for name, _ in executor.calls]
    assert "filesystem__update_project_files" not in tool_names
    assert result.interrupted is False
    assert result.final_state["terminal_status"] == "user_cancelled"
    assert result.final_state["approval_reason"] == "No aplicar cambios automáticos"
    assert result.final_state["last_rejected_tool"] == "filesystem__update_project_files"
    assert result.final_state["files_updated_during_repair"] == []


@pytest.mark.asyncio
async def test_initial_interrupt_persists_serializable_request_without_executing_sensitive_tool() -> None:
    executor = FakeGraphToolExecutor()
    graph = interruptible_graph(dependencies(executor))

    result = await run_software_factory_graph(graph, CREATE_REQUEST, thread_id="interrupt-initial")
    snapshot = await WorkflowPersistenceService(graph).get_snapshot(result.thread_id)

    assert result.interrupted is True
    assert len(result.interrupts) == 1
    request = result.interrupts[0].value
    assert json.loads(json.dumps(request))["type"] == "tool_approval"
    assert request["operation"] == "create_project"
    assert request["public_tool_name"] == "filesystem__create_project_structure"
    assert request["arguments"] == snapshot.values["pending_tool_arguments"]
    assert snapshot.values["pending_approval_status"] == "waiting"
    assert snapshot.next_nodes == ("implementation",)
    assert snapshot.interrupts == result.interrupts
    assert "filesystem__create_project_structure" not in [name for name, _ in executor.calls]


@pytest.mark.asyncio
async def test_approval_uses_persisted_arguments_once_and_reaches_next_interrupt() -> None:
    executor = FakeGraphToolExecutor()
    graph = interruptible_graph(dependencies(executor))
    initial = await run_software_factory_graph(graph, CREATE_REQUEST, thread_id="approve-persisted")
    service = WorkflowPersistenceService(graph)
    before = await service.get_snapshot(initial.thread_id)
    persisted_arguments = before.values["pending_tool_arguments"]

    result = await service.approve(initial.thread_id, "Crear la estructura solicitada")

    create_calls = [args for name, args in executor.calls if name == "filesystem__create_project_structure"]
    assert create_calls == [persisted_arguments]
    assert result.thread_id == initial.thread_id
    assert result.interrupted is True
    assert result.interrupts[0].value["operation"] == "prepare_environment"
    assert result.final_state["pending_operation"] == "prepare_environment"
    assert result.final_state["pending_tool_name"] == "testing__prepare_test_environment"
    assert result.final_state["pending_approval_status"] == "waiting"
    assert result.final_state["last_approved_tool"] == "filesystem__create_project_structure"


@pytest.mark.asyncio
async def test_invalid_approval_response_creates_stable_validation_interrupt() -> None:
    executor = FakeGraphToolExecutor()
    graph = interruptible_graph(dependencies(executor))
    initial = await run_software_factory_graph(graph, CREATE_REQUEST, thread_id="invalid-decision")

    invalid = await graph.ainvoke(
        Command(resume={"approved": "yes"}),
        config={"configurable": {"thread_id": initial.thread_id}},
    )

    assert len(invalid["__interrupt__"]) == 1
    assert "validation_error" in invalid["__interrupt__"][0].value
    assert "filesystem__create_project_structure" not in [name for name, _ in executor.calls]


@pytest.mark.asyncio
async def test_langgraph_approved_execution_does_not_request_blocking_approval() -> None:
    manager = RegistryManager()
    client = RecordingClient({"success": True, "project_name": "medical-booking"})
    manager.registry.register(
        server_name="filesystem",
        original_name="create_project_structure",
        description="create",
        input_schema={
            "type": "object",
            "properties": {"project_name": {"type": "string"}, "files": {"type": "array"}},
            "required": ["project_name", "files"],
        },
        requires_approval=True,
        client=client,
    )
    host = RejectingHost(manager, object(), "test-model", "")  # type: ignore[arg-type]
    state = create_initial_state(CREATE_REQUEST)
    state.update(project_name="medical-booking", analysis_completed=True, tasks_created=True, workspace_inspected=True)

    outcome = await HostToolExecutor(host).execute(
        "filesystem__create_project_structure",
        {"project_name": "medical-booking", "files": [{"path": "README.md", "content": "demo"}]},
        state_context=state,
        approval_mode="already_approved",
    )

    assert outcome.payload and outcome.payload["success"] is True
    assert client.calls == [
        ("create_project_structure", {"project_name": "medical-booking", "files": [{"path": "README.md", "content": "demo"}]})
    ]


@pytest.mark.asyncio
async def test_parent_replay_reenters_testing_subgraph_and_requests_new_approval() -> None:
    executor = FakeGraphToolExecutor()
    graph = interruptible_graph(dependencies(executor))
    completed, _ = await run_with_all_approvals(graph, CREATE_REQUEST)
    before_subgraph = None
    async for snapshot in graph.aget_state_history({"configurable": {"thread_id": completed.thread_id}}):
        if tuple(snapshot.next) == ("testing_repair",):
            before_subgraph = snapshot
            break
    assert before_subgraph is not None
    previous_handoffs = list(before_subgraph.values["handoff_history"])
    create_calls = [call for call in executor.calls if call[0] == "filesystem__create_project_structure"]
    environment_calls = [call for call in executor.calls if call[0] == "testing__prepare_test_environment"]

    replayed = await WorkflowPersistenceService(graph).replay(
        completed.thread_id,
        before_subgraph.config["configurable"]["checkpoint_id"],
    )

    assert replayed.interrupted is True
    assert replayed.interrupts[0].value["operation"] == "run_tests"
    assert [call for call in executor.calls if call[0] == "filesystem__create_project_structure"] == create_calls
    assert [call for call in executor.calls if call[0] == "testing__prepare_test_environment"] == environment_calls
    replayed = await WorkflowPersistenceService(graph).approve(replayed.thread_id)
    assert replayed.final_state["testing_result"]["tests_passed"] is True
    assert replayed.final_state["testing_result"]["repair_attempts"] == 0
    assert replayed.final_state["handoff_history"][: len(previous_handoffs)] == previous_handoffs


@pytest.mark.asyncio
async def test_fork_before_nested_prepare_fix_retriggers_approval_without_writing() -> None:
    executor = FakeGraphToolExecutor(project_exists=True, fail_first_test=True)
    graph = interruptible_graph(dependencies(executor))
    result = await run_software_factory_graph(graph, REVIEW_REQUEST, thread_id="nested-fork")
    service = WorkflowPersistenceService(graph)
    result = await service.approve(result.thread_id)
    result = await service.approve(result.thread_id)
    assert result.interrupted is True
    assert result.interrupts[0].value["operation"] == "apply_fix"
    nested_checkpoint_id = None
    async for checkpoint_tuple in graph.checkpointer.alist(None):
        snapshot = await graph.aget_state(checkpoint_tuple.config)
        if tuple(snapshot.next) == ("prepare_fix",):
            nested_checkpoint_id = checkpoint_tuple.config["configurable"]["checkpoint_id"]
            break
    assert nested_checkpoint_id is not None
    writes_before = [call for call in executor.calls if call[0] == "filesystem__update_project_files"]

    forked = await service.fork(
        result.thread_id,
        nested_checkpoint_id,
        {"repair_decision": "Propuesta alternativa dentro del subgrafo"},
        continue_execution=True,
    )

    assert forked.run_result is not None
    assert forked.run_result.interrupted is True
    assert forked.run_result.interrupts[0].value["operation"] == "apply_fix"
    assert [call for call in executor.calls if call[0] == "filesystem__update_project_files"] == writes_before


@pytest.mark.asyncio
async def test_repair_limit_finishes_without_infinite_loop() -> None:
    executor = FakeGraphToolExecutor(project_exists=True, tests_always_fail=True)
    graph = interruptible_graph(dependencies(executor))

    result, _ = await run_with_all_approvals(graph, REVIEW_REQUEST)
    state = result.final_state

    assert executor.test_runs == 3
    assert state["repair_attempts"] == 2
    assert state["tests_passed"] is False
    assert state["final_response"]


@pytest.mark.asyncio
async def test_manual_and_langgraph_baselines_have_equivalent_repair_outcome() -> None:
    manual_executor = FakeGraphToolExecutor(project_exists=True, fail_first_test=True)
    manual_state = await run_manual_repair_runtime(manual_executor)
    graph_executor = FakeGraphToolExecutor(project_exists=True, fail_first_test=True)
    graph = interruptible_graph(dependencies(graph_executor))

    result, _ = await run_with_all_approvals(graph, REVIEW_REQUEST)
    state = result.final_state
    manual_tool_sequence = [name for name, _ in manual_executor.calls]
    graph_tool_sequence = [name for name, _ in graph_executor.calls]
    graph_operational_tool_sequence = [
        name for name in graph_tool_sequence
        if name not in {"knowledge__get_relevant_context", "knowledge__submit_learning"}
    ]

    assert graph_operational_tool_sequence == manual_tool_sequence
    knowledge_agents = [
        arguments.get("agent_name")
        for name, arguments in graph_executor.calls
        if name == "knowledge__get_relevant_context"
    ]
    assert knowledge_agents == ["QA", "Repair", "QA"]
    assert state["project_exists"] is manual_state.project_exists is True
    assert state["tests_passed"] is manual_state.tests_passed is True
    assert state["repair_attempts"] == manual_state.repair_attempts == 1
    assert state["repair_decision"] == manual_state.repair_decision
