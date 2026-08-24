from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from graph.builder import build_software_factory_graph
from graph.nodes import GraphDependencies
from graph.planning_approval_policy import evaluate_planning_approval_policy
from graph.routers import route_after_planning
from graph.runtime import run_software_factory_graph
from graph.state import SoftwareFactoryState, create_initial_state
from graph.subgraphs.implementation.test_validation import build_fastapi_health_test
from graph.subgraphs.planning.builder import build_planning_subgraph
from graph.subgraphs.planning.executability import validate_plan_executability
from graph.subgraphs.planning.models import (
    AcceptanceCriterion,
    ImplementationTask,
    PlanningOutput,
    RequirementAnalysis,
)
from graph.subgraphs.planning.quality import (
    build_quality_refinement_guidance,
    evaluate_quality_gate,
    score_plan_quality,
)
from graph.subgraphs.planning.risk import analyze_plan_risk
from graph.subgraphs.planning.routers import route_after_plan_validation
from graph.subgraphs.planning.service import (
    PlanningDomainError,
    PlanningService,
    validate_planning_contract,
    validate_planning_output,
)
from graph.subgraphs.planning.normalization import (
    normalize_framework,
    normalize_project_type,
)
from tool_executor import ToolExecutionOutcome


REQUEST = 'Crea un proyecto FastAPI llamado planning-booking con un endpoint GET /health que devuelva {"status": "ok"}, agrega pruebas.'


def valid_plan(project_name: str = "planning-booking") -> PlanningOutput:
    return PlanningOutput(
        analysis=RequirementAnalysis(
            objective="Crear una API mínima verificable.",
            project_name=project_name,
            project_type="fastapi",
            functional_requirements=['GET /health devuelve {"status": "ok"}.'],
            non_functional_requirements=["Implementación mantenible."],
            constraints=['Preservar {"status": "ok"}.'],
            assumptions=["Sin alcance adicional."],
        ),
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1",
                description='GET /health responde {"status": "ok"}.',
                verification_method="pytest",
            )
        ],
        tasks=[
            ImplementationTask(order=1, role="backend developer", title="Implementar API", description="Implementar GET /health."),
            ImplementationTask(order=2, role="QA", title="Crear tests", description="Validar el JSON con pytest.", depends_on=[1]),
        ],
    )


class PlanningExecutor:
    def __init__(self, *, project_exists: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.project_exists = project_exists

    def openai_tool(self, name: str) -> dict[str, Any]:
        return {"type": "function", "name": name, "parameters": {"type": "object"}}

    async def execute(self, name, arguments, *, state_context, approval_mode="prompt"):
        self.calls.append((name, arguments))
        payload: dict[str, Any] = {"success": True}
        updates: dict[str, Any] = {}
        if name == "software_factory__analyze_requirement":
            payload = {"objective": "Crear planning-booking.", "functional_scope": ["GET /health"]}
            updates["analysis_completed"] = True
        elif name == "software_factory__create_tasks":
            updates["tasks_created"] = True
        elif name == "filesystem__list_files":
            updates.update(workspace_inspected=True, project_exists=self.project_exists)
            if self.project_exists:
                updates["created_project_name"] = "planning-booking"
        elif name == "testing__detect_test_framework":
            updates.update(detected_test_framework="pytest", expected_test_command=["python", "-m", "pytest"])
        return ToolExecutionOutcome(name, arguments, payload, updates)


class NoResponsesClient:
    pass


def dependencies(executor: Any, *, planning_service: Any = None, resolver: Any = None) -> GraphDependencies:
    return GraphDependencies(
        tool_executor=executor,
        openai_client=NoResponsesClient(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=resolver,
        planning_service=planning_service,
    )


def planning_state() -> SoftwareFactoryState:
    state = create_initial_state(REQUEST)
    state["project_name"] = "planning-booking"
    return state


def test_planning_models_are_strict_and_serializable() -> None:
    plan = valid_plan()
    dumped = plan.model_dump()
    schema = PlanningOutput.model_json_schema()

    assert json.loads(json.dumps(dumped))["analysis"]["project_name"] == "planning-booking"
    assert schema["additionalProperties"] is False
    with pytest.raises(Exception):
        RequirementAnalysis.model_validate({**plan.analysis.model_dump(), "unexpected": True})


@pytest.mark.parametrize(
    "alias",
    [
        "FastAPI",
        "Proyecto FastAPI",
        "Proyecto de servicio HTTP (FastAPI)",
        "Proyecto de servicio HTTP (FastAPI) con pruebas",
        "fastapi-api",
        "fastapi_service",
    ],
)
def test_fastapi_descriptive_project_types_normalize_to_canonical(alias: str) -> None:
    assert normalize_project_type(alias) == "fastapi"


def test_framework_normalizes_independently_from_project_type() -> None:
    analysis = RequirementAnalysis(
        **{
            **valid_plan().analysis.model_dump(exclude={"framework"}),
            "project_type": "Proyecto de servicio HTTP (FastAPI) con pruebas",
            "framework": "Proyecto FastAPI",
        }
    )
    assert analysis.project_type == "fastapi"
    assert analysis.framework == "fastapi"
    assert normalize_framework("Express", project_type="fastapi") == "node"


@pytest.mark.parametrize("canonical", ["fastapi", "dotnet", "node"])
def test_existing_supported_project_types_remain_unchanged(canonical: str) -> None:
    assert normalize_project_type(canonical) == canonical


def test_planning_subgraph_compiles_without_own_checkpointer() -> None:
    executor = PlanningExecutor()
    subgraph = build_planning_subgraph(dependencies(executor))

    assert subgraph.checkpointer is None
    assert {
        "analyze_requirement",
        "prepare_planner_knowledge",
        "create_tasks",
        "validate_plan",
        "refine_plan",
    } <= set(subgraph.get_graph().nodes)


@pytest.mark.asyncio
async def test_valid_plan_is_stored_and_uses_only_allowed_tools() -> None:
    executor = PlanningExecutor()
    result = await build_planning_subgraph(dependencies(executor)).ainvoke(planning_state())

    assert result["requirement_analysis"]["project_name"] == "planning-booking"
    assert result["acceptance_criteria"]
    assert len(result["implementation_tasks"]) == 2
    assert result["planning_valid"] is True
    assert result["planning_attempts"] == 0
    assert {name for name, _ in executor.calls} == {
        "software_factory__analyze_requirement",
        "knowledge__get_relevant_context",
        "software_factory__create_tasks",
    }


@pytest.mark.asyncio
async def test_phase7_fastapi_request_produces_canonical_valid_planning() -> None:
    request = (
        "Crea un proyecto FastAPI llamado phase-7-cross-workflow-success "
        "con GET /health y tests automáticos."
    )
    state = create_initial_state(request)
    state["project_name"] = "phase-7-cross-workflow-success"
    result = await build_planning_subgraph(
        dependencies(PlanningExecutor())
    ).ainvoke(state)

    assert result["requirement_analysis"]["project_type"] == "fastapi"
    assert result["requirement_analysis"]["framework"] == "fastapi"
    assert result["planning_valid"] is True
    assert not any("docker" in error.casefold() for error in result["planning_errors"])


@pytest.mark.asyncio
async def test_phase72_fastapi_request_produces_executable_planning_order() -> None:
    request = (
        'Crea un proyecto FastAPI llamado phase-7-executability '
        'con GET /health que devuelva {"status":"ok"} y tests automaticos.'
    )
    state = create_initial_state(request)
    state["project_name"] = "phase-7-executability"

    class Phase72Service(ExecutabilityRefiningService):
        async def analyze(self, state):
            plan = executable_plan("phase-7-executability")
            return plan.analysis, plan.acceptance_criteria

        async def create_tasks(self, state):
            return executable_plan("phase-7-executability").tasks

    result = await build_planning_subgraph(
        dependencies(PlanningExecutor(), planning_service=Phase72Service())
    ).ainvoke(state)

    assert result["requirement_analysis"]["project_type"] == "fastapi"
    assert result["requirement_analysis"]["framework"] == "fastapi"
    assert result["planning_valid"] is True
    assert result["planning_errors"] == []
    assert result["planning_execution_order"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_planner_rejects_filesystem_and_testing_tools() -> None:
    service = PlanningService(NoResponsesClient(), "test-model", PlanningExecutor())  # type: ignore[arg-type]

    for tool_name in ("filesystem__list_files", "testing__run_tests"):
        with pytest.raises(PlanningDomainError, match="no permitida"):
            await service._execute_tool(tool_name, {}, planning_state())


class RefiningService:
    def __init__(self, refined: PlanningOutput, *, always_invalid: bool = False) -> None:
        self.refined = refined
        self.always_invalid = always_invalid
        self.refine_calls = 0

    async def analyze(self, state):
        plan = valid_plan("wrong-project")
        return plan.analysis, plan.acceptance_criteria

    async def create_tasks(self, state):
        return valid_plan().tasks

    async def refine(self, state):
        self.refine_calls += 1
        return valid_plan("wrong-project") if self.always_invalid else self.refined


class InspectingPlanningService:
    def __init__(self) -> None:
        self.create_tasks_state: dict[str, Any] | None = None

    async def analyze(self, state):
        plan = valid_plan()
        return plan.analysis, plan.acceptance_criteria

    async def create_tasks(self, state):
        self.create_tasks_state = dict(state)
        return valid_plan().tasks

    async def refine(self, state):
        return valid_plan()


class ExecutabilityRefiningService:
    def __init__(self, refined: PlanningOutput | None = None, *, always_invalid: bool = False) -> None:
        self.refined = refined or executable_plan()
        self.always_invalid = always_invalid
        self.refine_calls = 0
        self.refine_states: list[dict[str, Any]] = []

    async def analyze(self, state):
        plan = executable_plan()
        return plan.analysis, plan.acceptance_criteria

    async def create_tasks(self, state):
        return [
            ImplementationTask(
                order=1,
                role="QA",
                title="Crear tests GET /health",
                description="Crear tests/test_health.py antes de implementar.",
            ),
            ImplementationTask(
                order=2,
                role="backend developer",
                title="Implementar GET /health",
                description='Implementar endpoint GET /health que devuelve {"status": "ok"}.',
            ),
        ]

    async def refine(self, state):
        self.refine_calls += 1
        self.refine_states.append(dict(state))
        if self.always_invalid:
            plan = executable_plan()
            plan.tasks = await self.create_tasks(state)
            return plan
        return self.refined


def low_quality_executable_plan(project_name: str = "planning-booking") -> PlanningOutput:
    plan = valid_plan(project_name)
    plan.tasks = [
        ImplementationTask(
            order=1,
            role="backend developer",
            title="Implement",
            description='GET /health {"status": "ok"}.',
        ),
        ImplementationTask(
            order=2,
            role="QA",
            title="Test",
            description="pytest.",
            depends_on=[1],
        ),
    ]
    return plan


class QualityRefiningService:
    def __init__(self, *, refined: PlanningOutput | None = None, always_low_quality: bool = False) -> None:
        self.refined = refined or executable_plan()
        self.always_low_quality = always_low_quality
        self.refine_calls = 0
        self.refine_states: list[dict[str, Any]] = []

    async def analyze(self, state):
        plan = low_quality_executable_plan()
        return plan.analysis, plan.acceptance_criteria

    async def create_tasks(self, state):
        return low_quality_executable_plan().tasks

    async def refine(self, state):
        self.refine_calls += 1
        self.refine_states.append(dict(state))
        return low_quality_executable_plan() if self.always_low_quality else self.refined


@pytest.mark.asyncio
async def test_create_tasks_receives_structured_analysis_between_nodes() -> None:
    service = InspectingPlanningService()

    await build_planning_subgraph(dependencies(PlanningExecutor(), planning_service=service)).ainvoke(planning_state())

    assert service.create_tasks_state is not None
    assert service.create_tasks_state["requirement_analysis"]["project_name"] == "planning-booking"
    assert service.create_tasks_state["acceptance_criteria"]
    assert "analysis_completed" not in service.create_tasks_state


@pytest.mark.asyncio
async def test_invalid_plan_refines_once_then_becomes_valid() -> None:
    service = RefiningService(valid_plan())
    state = await build_planning_subgraph(dependencies(PlanningExecutor(), planning_service=service)).ainvoke(planning_state())

    assert service.refine_calls == 1
    assert state["planning_attempts"] == 1
    assert state["planning_valid"] is True
    assert state["terminal_status"] is None


@pytest.mark.asyncio
async def test_executability_error_reaches_refinement_and_refined_plan_is_revalidated() -> None:
    service = ExecutabilityRefiningService()

    result = await build_planning_subgraph(
        dependencies(PlanningExecutor(), planning_service=service)
    ).ainvoke(planning_state())

    assert service.refine_calls == 1
    assert "planning_task_order_invalid" in error_codes(service.refine_states[0]["planning_errors"])
    assert result["planning_valid"] is True
    assert result["planning_errors"] == []
    assert result["planning_execution_order"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_executability_refinement_limit_sets_planning_failed() -> None:
    service = ExecutabilityRefiningService(always_invalid=True)
    state = planning_state()
    state["max_planning_attempts"] = 2

    result = await build_planning_subgraph(
        dependencies(PlanningExecutor(), planning_service=service)
    ).ainvoke(state)

    assert service.refine_calls == 2
    assert result["planning_valid"] is False
    assert result["terminal_status"] == "planning_failed"
    assert result["planning_failure_reason"] == "planning_task_order_invalid"


@pytest.mark.asyncio
async def test_descriptive_fastapi_analysis_is_normalized_without_planning_retry() -> None:
    class DescriptiveFastApiService:
        refine_calls = 0

        async def analyze(self, state):
            base = valid_plan()
            analysis = RequirementAnalysis.model_construct(
                **{
                    **base.analysis.model_dump(),
                    "project_type": "Proyecto de servicio HTTP (FastAPI) con pruebas",
                    "framework": "Proyecto de servicio HTTP (FastAPI) con pruebas",
                    "constraints": ["No se añadirá soporte para Docker."],
                }
            )
            return analysis, base.acceptance_criteria

        async def create_tasks(self, state):
            return valid_plan().tasks

        async def refine(self, state):
            self.refine_calls += 1
            return valid_plan()

    service = DescriptiveFastApiService()
    result = await build_planning_subgraph(
        dependencies(PlanningExecutor(), planning_service=service)
    ).ainvoke(planning_state())

    assert result["planning_valid"] is True
    assert result["planning_attempts"] == 0
    assert service.refine_calls == 0
    assert result["requirement_analysis"]["project_type"] == "fastapi"
    assert result["requirement_analysis"]["framework"] == "fastapi"
    assert not any("docker" in error.casefold() for error in result["planning_errors"])


@pytest.mark.asyncio
async def test_development_flag_forces_invalid_first_plan_then_refines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InitiallyValidService(RefiningService):
        async def analyze(self, state):
            plan = valid_plan()
            return plan.analysis, plan.acceptance_criteria

    monkeypatch.setenv("LANGGRAPH_DEVELOPMENT", "true")
    monkeypatch.setenv("PLANNING_FORCE_INVALID_FIRST_ATTEMPT", "true")
    service = InitiallyValidService(valid_plan())

    result = await build_planning_subgraph(dependencies(PlanningExecutor(), planning_service=service)).ainvoke(planning_state())

    assert service.refine_calls == 1
    assert result["planning_attempts"] == 1
    assert result["planning_valid"] is True
    assert result["requirement_analysis"]["project_name"] == "planning-booking"


@pytest.mark.asyncio
async def test_refinement_limit_sets_planning_failed() -> None:
    service = RefiningService(valid_plan(), always_invalid=True)
    state = planning_state()
    state["max_planning_attempts"] = 2
    result = await build_planning_subgraph(dependencies(PlanningExecutor(), planning_service=service)).ainvoke(state)

    assert service.refine_calls == 2
    assert result["planning_attempts"] == 2
    assert result["planning_valid"] is False
    assert result["terminal_status"] == "planning_failed"
    assert result["failure_type"] == "planning_validation_failed"
    assert route_after_planning(result) == "failed"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda plan: setattr(plan.analysis, "project_name", "other"), "project_name"),
        (lambda plan: setattr(plan, "acceptance_criteria", [plan.acceptance_criteria[0], plan.acceptance_criteria[0]]), "únicos"),
        (lambda plan: setattr(plan.tasks[1], "order", 1), "órdenes"),
        (lambda plan: setattr(plan.tasks[1], "depends_on", [99]), "inexistentes"),
        (lambda plan: setattr(plan.tasks[0], "depends_on", [1]), "sí misma"),
        (lambda plan: (setattr(plan.tasks[0], "depends_on", [2]), setattr(plan.tasks[1], "depends_on", [1])), "ciclo"),
        (
            lambda plan: (
                setattr(plan.tasks[1], "role", "documentation writer"),
                setattr(plan.tasks[1], "title", "Documentar"),
                setattr(plan.tasks[1], "description", "Documentar el proyecto."),
            ),
            "pruebas",
        ),
    ],
)
def test_deterministic_validation_rejects_invalid_contracts(mutate, expected: str) -> None:
    plan = valid_plan()
    mutate(plan)

    normalized_expected = (
        expected.replace("Ãºnicos", "unicos")
        .replace("Ã³rdenes", "ordenes")
        .replace("sÃ­ misma", "si misma")
    )
    if "nico" in normalized_expected or "�nico" in normalized_expected:
        normalized_expected = "unicos"
    if "rdenes" in normalized_expected:
        normalized_expected = "ordenes"
    if "misma" in normalized_expected:
        normalized_expected = "si misma"
    assert normalized_expected in " ".join(validate_planning_output(plan, planning_state()))


def error_codes(errors: list[str]) -> set[str]:
    return {error.split(":", 1)[0] for error in errors}


def executable_plan(project_name: str = "planning-booking") -> PlanningOutput:
    plan = valid_plan(project_name)
    plan.tasks = [
        ImplementationTask(
            order=1,
            role="backend developer",
            title="Crear aplicacion FastAPI",
            description="Crear planning_booking/main.py con la aplicacion FastAPI.",
        ),
        ImplementationTask(
            order=2,
            role="backend developer",
            title="Implementar GET /health",
            description='Implementar endpoint GET /health que devuelve {"status": "ok"}.',
            depends_on=[1],
        ),
        ImplementationTask(
            order=3,
            role="QA",
            title="Crear tests GET /health",
            description='Crear tests/test_health.py para validar GET /health y {"status": "ok"}.',
            depends_on=[2],
        ),
    ]
    return plan


def test_contract_validation_rejects_missing_or_invalid_schema() -> None:
    _plan, errors = validate_planning_contract(None, planning_state())

    assert "planning_schema_invalid" in error_codes(errors)

    _plan, errors = validate_planning_contract({"analysis": {"bad": True}}, planning_state())

    assert "planning_schema_invalid" in error_codes(errors)


def test_contract_validation_rejects_project_name_mismatch() -> None:
    plan = valid_plan("other-project")

    errors = validate_planning_output(plan, planning_state())

    assert "planning_project_name_mismatch" in error_codes(errors)


def test_contract_validation_rejects_framework_mismatch() -> None:
    plan = valid_plan()
    plan.analysis.framework = "node"

    errors = validate_planning_output(plan, planning_state())

    assert "planning_framework_mismatch" in error_codes(errors)


def test_contract_validation_rejects_empty_tasks() -> None:
    raw = valid_plan().model_dump()
    raw["tasks"] = []

    _plan, errors = validate_planning_contract(raw, planning_state())

    assert "planning_tasks_empty" in error_codes(errors)


def test_contract_validation_rejects_duplicate_task_ids_in_raw_plan() -> None:
    raw = valid_plan().model_dump()
    raw["tasks"][0]["id"] = "T-1"
    raw["tasks"][1]["id"] = "T-1"

    _plan, errors = validate_planning_contract(raw, planning_state())

    assert "planning_duplicate_task" in error_codes(errors)


def test_contract_validation_rejects_duplicate_identical_tasks() -> None:
    plan = valid_plan()
    plan.tasks.append(
        ImplementationTask(
            order=3,
            role=plan.tasks[0].role,
            title=plan.tasks[0].title,
            description=plan.tasks[0].description,
        )
    )

    errors = validate_planning_output(plan, planning_state())

    assert "planning_duplicate_task" in error_codes(errors)


def test_prohibited_docker_task_is_constraint_violation() -> None:
    state = planning_state()
    state["original_user_message"] += " No usar Docker."
    plan = valid_plan()
    plan.analysis.constraints.append("No usar Docker.")
    plan.tasks.append(
        ImplementationTask(
            order=3,
            role="DevOps",
            title="Agregar Docker",
            description="Crear Dockerfile y configuracion de Docker.",
            depends_on=[1],
        )
    )

    errors = validate_planning_output(plan, state)

    assert "planning_constraint_violation" in error_codes(errors)


def test_missing_endpoint_or_json_contract_is_required_constraint_missing() -> None:
    plan = valid_plan()
    plan.analysis.functional_requirements = ["Implementar API saludable."]
    plan.analysis.constraints = []
    plan.acceptance_criteria[0].description = "La API responde correctamente."
    plan.tasks[0].description = "Implementar la API."

    errors = validate_planning_output(plan, planning_state())

    assert "planning_required_constraint_missing" in error_codes(errors)


def test_scope_expansion_is_rejected_when_not_requested() -> None:
    plan = valid_plan()
    plan.tasks.append(
        ImplementationTask(
            order=3,
            role="platform engineer",
            title="Agregar Kubernetes",
            description="Crear manifiestos de Kubernetes y Terraform.",
            depends_on=[1],
        )
    )

    errors = validate_planning_output(plan, planning_state())

    assert "planning_scope_violation" in error_codes(errors)


def test_executability_accepts_fastapi_endpoint_then_tests() -> None:
    result = validate_plan_executability(executable_plan(), planning_state())

    assert result.errors == []
    assert result.execution_order == [1, 2, 3]
    assert result.dependency_edges == [{"from": 1, "to": 2}, {"from": 2, "to": 3}]


def test_executability_accepts_independent_tasks_and_fanout() -> None:
    plan = executable_plan()
    plan.tasks.append(
        ImplementationTask(
            order=4,
            role="technical writer",
            title="Crear README",
            description="Crear README.md para documentar el uso.",
        )
    )
    plan.tasks.append(
        ImplementationTask(
            order=5,
            role="QA",
            title="Crear segundo test",
            description="Crear tests/test_contract.py para validar el contrato.",
            depends_on=[2],
        )
    )

    result = validate_plan_executability(plan, planning_state())

    assert result.errors == []
    assert sorted(result.execution_order) == [1, 2, 3, 4, 5]
    assert result.execution_order.index(2) < result.execution_order.index(3)
    assert result.execution_order.index(2) < result.execution_order.index(5)


def test_executability_accepts_multiple_valid_targets() -> None:
    plan = executable_plan()
    plan.tasks[0].description += " Crear requirements.txt y README.md."

    result = validate_plan_executability(plan, planning_state())

    assert result.errors == []


def test_executability_rejects_missing_dependency() -> None:
    plan = executable_plan()
    plan.tasks[2].depends_on = [99]

    result = validate_plan_executability(plan, planning_state())

    assert "planning_dependency_missing" in error_codes(result.errors)


def test_executability_rejects_self_dependency() -> None:
    plan = executable_plan()
    plan.tasks[0].depends_on = [1]

    result = validate_plan_executability(plan, planning_state())

    assert "planning_dependency_cycle" in error_codes(result.errors)


def test_executability_rejects_cycle_ab_and_abc() -> None:
    plan = executable_plan()
    plan.tasks[0].depends_on = [2]
    plan.tasks[1].depends_on = [1]
    assert "planning_dependency_cycle" in error_codes(validate_plan_executability(plan, planning_state()).errors)

    plan = executable_plan()
    plan.tasks[0].depends_on = [3]
    plan.tasks[1].depends_on = [1]
    plan.tasks[2].depends_on = [2]
    assert "planning_dependency_cycle" in error_codes(validate_plan_executability(plan, planning_state()).errors)


def test_executability_rejects_tests_before_implementation() -> None:
    plan = executable_plan()
    plan.tasks = [
        ImplementationTask(
            order=1,
            role="QA",
            title="Crear tests GET /health",
            description="Crear tests/test_health.py antes de implementar.",
        ),
        ImplementationTask(
            order=2,
            role="backend developer",
            title="Implementar GET /health",
            description="Implementar endpoint GET /health.",
        ),
    ]

    result = validate_plan_executability(plan, planning_state())

    assert "planning_task_order_invalid" in error_codes(result.errors)


def test_executability_rejects_artifact_consumed_before_produced() -> None:
    plan = executable_plan()
    plan.tasks[0].title = "Modificar aplicacion FastAPI"
    plan.tasks[0].description = "Modificar app/main.py para agregar GET /health."

    result = validate_plan_executability(plan, planning_state())

    assert "planning_artifact_missing" in error_codes(result.errors)


def test_executability_rejects_inconsistent_main_targets() -> None:
    plan = executable_plan()
    plan.tasks[0].description = "Crear app/main.py para la aplicacion FastAPI."
    plan.tasks[1].description = "Crear src/main.py para implementar GET /health."

    result = validate_plan_executability(plan, planning_state())

    assert "planning_target_inconsistent" in error_codes(result.errors)


def test_executability_rejects_fastapi_without_executable_implementation() -> None:
    plan = executable_plan()
    plan.tasks = [
        ImplementationTask(
            order=1,
            role="QA",
            title="Crear tests",
            description="Crear tests/test_health.py.",
        )
    ]

    result = validate_plan_executability(plan, planning_state())

    assert "planning_framework_structure_invalid" in error_codes(result.errors)


def test_executability_rejects_unreachable_task() -> None:
    plan = executable_plan()
    plan.tasks.append(
        ImplementationTask(
            order=4,
            role="release engineer",
            title="Deploy package",
            description="Deploy package artifact.",
            depends_on=[3],
        )
    )

    result = validate_plan_executability(plan, planning_state())

    assert "planning_unreachable_task" in error_codes(result.errors)


def test_risk_low_readme_only_simple_endpoint_and_tests() -> None:
    readme = executable_plan()
    readme.tasks = [
        ImplementationTask(
            order=1,
            role="technical writer",
            title="Crear README",
            description="Crear README.md con documentacion.",
        )
    ]
    endpoint = executable_plan()
    tests = executable_plan()
    tests.tasks = [
        ImplementationTask(
            order=1,
            role="QA",
            title="Crear tests",
            description="Crear tests/test_health.py para GET /health.",
        )
    ]

    assert analyze_plan_risk(readme).risk_level == "low"
    assert analyze_plan_risk(endpoint).risk_level in {"low", "medium"}
    assert analyze_plan_risk(tests).risk_level == "low"


def test_risk_medium_dependency_and_config_changes() -> None:
    plan = executable_plan()
    plan.tasks = [
        ImplementationTask(order=1, role="developer", title="Modificar requirements.txt", description="Modificar requirements.txt para nuevas dependencias."),
        ImplementationTask(order=2, role="developer", title="Actualizar package.json", description="Actualizar package.json."),
        ImplementationTask(order=3, role="developer", title="Cambiar app config", description="Cambiar runtime configuration en appsettings.json."),
    ]

    result = analyze_plan_risk(plan)

    assert result.risk_level == "medium"
    assert {"dependencies", "configuration", "runtime"} <= set(result.impact_areas)


def test_risk_high_security_database_and_multiple_core_modules() -> None:
    plan = executable_plan()
    plan.tasks = [
        ImplementationTask(order=1, role="developer", title="Modificar autenticacion", description="Modificar authentication y authorization permissions."),
        ImplementationTask(order=2, role="developer", title="Agregar migration", description="Agregar database migration para schema change."),
        ImplementationTask(order=3, role="developer", title="Cambiar runtime", description="Actualizar runtime configuration."),
        ImplementationTask(order=4, role="developer", title="Modificar core", description="Modificar core/user.py core/orders.py core/payments.py."),
    ]

    result = analyze_plan_risk(plan)

    assert result.risk_level == "high"
    assert {"security", "database", "configuration", "runtime"} <= set(result.impact_areas)
    assert result.sensitive_tasks


def test_risk_critical_destructive_data_schema_and_secrets() -> None:
    plan = executable_plan()
    plan.tasks = [
        ImplementationTask(order=1, role="dba", title="Delete persistent data", description="Delete persistent data and drop schema."),
        ImplementationTask(order=2, role="security", title="Overwrite credentials", description="Overwrite secret credentials in production config."),
    ]

    result = analyze_plan_risk(plan)

    assert result.risk_level == "critical"
    assert result.risk_score <= 100
    assert {"database", "security", "configuration"} <= set(result.impact_areas)
    assert result.sensitive_tasks


def test_risk_aggregation_is_deterministic_deduped_and_clamped() -> None:
    plan = executable_plan()
    plan.tasks = [
        ImplementationTask(
            order=index,
            role="security",
            title=f"Overwrite secret credential {index}",
            description="Overwrite secret credentials and delete persistent data.",
        )
        for index in range(1, 8)
    ]

    first = analyze_plan_risk(plan)
    second = analyze_plan_risk(plan)

    assert first.risk_score == 100
    assert second.risk_score == first.risk_score
    assert first.risk_reasons == list(dict.fromkeys(first.risk_reasons))
    assert first.impact_areas == sorted(set(first.impact_areas))


@pytest.mark.parametrize(
    ("level", "areas", "sensitive_tasks", "expected"),
    [
        ("low", ["application", "tests"], [], "allow"),
        ("medium", ["documentation"], [], "allow"),
        ("medium", ["tests"], [], "allow"),
        ("medium", ["dependencies"], [], "require_approval"),
        ("medium", ["configuration"], [], "require_approval"),
        ("medium", ["security"], [], "require_approval"),
        ("high", ["application"], [], "require_approval"),
        ("critical", ["application"], [], "require_approval"),
        ("medium", ["application"], [{"task_id": "1", "risk_level": "high", "reasons": ["security_change"]}], "require_approval"),
    ],
)
def test_planning_approval_policy_is_deterministic(level, areas, sensitive_tasks, expected) -> None:
    decision = evaluate_planning_approval_policy(level, 30, areas, sensitive_tasks)

    assert decision.decision == expected
    assert decision.required is (expected == "require_approval")


def quality_state_for(plan: PlanningOutput, *, risk_level: str = "low", approval_status: str = "not_required", attempts: int = 0) -> SoftwareFactoryState:
    state = planning_state()
    risk = analyze_plan_risk(plan)
    state.update(
        planning_risk_score=risk.risk_score,
        planning_risk_level=risk_level or risk.risk_level,
        planning_risk_reasons=risk.risk_reasons,
        planning_sensitive_tasks=risk.sensitive_tasks,
        planning_impact_areas=risk.impact_areas,
        planning_execution_order=[task.order for task in plan.tasks],
        planning_dependency_edges=[
            {"from": dependency, "to": task.order}
            for task in plan.tasks
            for dependency in task.depends_on
        ],
        planning_approval_required=approval_status in {"awaiting_approval", "approved"},
        planning_approval_status=approval_status,
        planning_attempts=attempts,
    )
    return state


def test_quality_excellent_fastapi_health_plan_has_high_confidence() -> None:
    plan = executable_plan()
    plan.tasks.append(
        ImplementationTask(order=4, role="developer", title="Crear requirements.txt", description="Crear requirements.txt para FastAPI y pytest.", depends_on=[1])
    )

    quality = score_plan_quality(plan, quality_state_for(plan))

    assert quality.score >= 80
    assert quality.level in {"good", "excellent"}
    assert quality.dimensions["requirement_coverage"] >= 90
    assert quality.dimensions["plan_completeness"] >= 80
    assert quality.decision_confidence >= 0.80


def test_quality_low_task_clarity_adds_issue_without_contract_failure() -> None:
    plan = executable_plan()
    plan.tasks = [
        ImplementationTask(order=1, role="backend developer", title="hacer backend", description="hacer backend"),
        ImplementationTask(order=2, role="backend developer", title="implementar", description="implementar", depends_on=[1]),
        ImplementationTask(order=3, role="QA", title="cosas de tests", description="cosas de tests", depends_on=[2]),
    ]

    quality = score_plan_quality(plan, quality_state_for(plan))

    assert quality.dimensions["task_clarity"] < 70
    assert "planning_quality_low_task_clarity" in quality.issues


def test_quality_incomplete_plan_warns_when_tests_are_missing() -> None:
    plan = executable_plan()
    plan.tasks = [
        ImplementationTask(order=1, role="backend developer", title="Crear app FastAPI", description="Crear app FastAPI."),
        ImplementationTask(order=2, role="backend developer", title="Implementar GET /health", description='Implementar GET /health con {"status": "ok"}.', depends_on=[1]),
    ]

    quality = score_plan_quality(plan, quality_state_for(plan))

    assert quality.dimensions["plan_completeness"] < 100
    assert "planning_quality_incomplete_plan" in quality.issues


def test_quality_high_risk_reduces_residual_and_confidence_but_not_structure() -> None:
    plan = executable_plan()
    state = quality_state_for(plan, risk_level="high")

    quality = score_plan_quality(plan, state)

    assert quality.dimensions["requirement_coverage"] >= 90
    assert quality.dimensions["risk_residual"] == 55
    assert quality.decision_confidence < 0.85


def test_quality_is_deterministic_for_same_input() -> None:
    plan = executable_plan()
    state = quality_state_for(plan)

    assert score_plan_quality(plan, state) == score_plan_quality(plan, state)


def test_quality_approval_pending_caps_confidence_and_approved_increases() -> None:
    plan = executable_plan()
    pending = score_plan_quality(plan, quality_state_for(plan, risk_level="medium", approval_status="awaiting_approval"))
    approved = score_plan_quality(plan, quality_state_for(plan, risk_level="medium", approval_status="approved"))

    assert pending.decision_confidence <= 0.60
    assert approved.score == pending.score
    assert approved.decision_confidence > pending.decision_confidence


def test_quality_gate_continues_for_good_structural_quality() -> None:
    decision = evaluate_quality_gate(
        quality_score=88,
        quality_level="good",
        quality_dimensions={
            "requirement_coverage": 95,
            "plan_completeness": 90,
            "task_clarity": 85,
            "dependency_coherence": 90,
            "risk_residual": 55,
        },
        quality_issues=["planning_quality_high_residual_risk"],
        decision_confidence=0.58,
        attempts=0,
        max_attempts=2,
    )

    assert decision.decision == "continue"
    assert decision.refinement_required is False


def test_quality_gate_refines_weak_or_structurally_incomplete_plan() -> None:
    decision = evaluate_quality_gate(
        quality_score=52,
        quality_level="weak",
        quality_dimensions={
            "requirement_coverage": 62,
            "plan_completeness": 65,
            "task_clarity": 75,
            "dependency_coherence": 90,
        },
        quality_issues=["planning_quality_low_requirement_coverage"],
        decision_confidence=0.50,
        attempts=0,
        max_attempts=2,
    )

    assert decision.decision == "refine"
    assert decision.reason == "planning_quality_below_threshold"
    assert "planning_quality_low_requirement_coverage" in decision.reason_codes


def test_quality_gate_does_not_refine_for_risk_residual_alone() -> None:
    decision = evaluate_quality_gate(
        quality_score=76,
        quality_level="good",
        quality_dimensions={
            "requirement_coverage": 95,
            "plan_completeness": 95,
            "task_clarity": 90,
            "dependency_coherence": 90,
            "risk_residual": 25,
        },
        quality_issues=["planning_quality_high_residual_risk"],
        decision_confidence=0.52,
        attempts=0,
        max_attempts=2,
    )

    assert decision.decision == "continue"
    assert "planning_quality_high_residual_risk" not in decision.reason_codes


def test_quality_gate_fails_when_refinement_attempts_are_exhausted() -> None:
    decision = evaluate_quality_gate(
        quality_score=39,
        quality_level="poor",
        quality_dimensions={
            "requirement_coverage": 40,
            "plan_completeness": 40,
            "task_clarity": 40,
            "dependency_coherence": 90,
        },
        quality_issues=["planning_quality_low_requirement_coverage"],
        decision_confidence=0.30,
        attempts=2,
        max_attempts=2,
    )

    assert decision.decision == "fail"
    assert decision.reason == "planning_quality_below_threshold"
    assert decision.refinement_required is False


def test_quality_refinement_guidance_is_structural_and_deterministic() -> None:
    guidance = build_quality_refinement_guidance(
        quality_score=45,
        quality_dimensions={
            "requirement_coverage": 50,
            "plan_completeness": 60,
            "task_clarity": 40,
            "dependency_coherence": 55,
        },
        quality_issues=["planning_quality_incomplete_plan"],
        gate_reason="planning_quality_below_threshold",
    )

    assert any("required behavior" in item for item in guidance)
    assert any("concrete actions" in item for item in guidance)
    assert any("execution DAG" in item for item in guidance)


@pytest.mark.asyncio
async def test_quality_gate_refines_plan_and_tracks_improvement_delta() -> None:
    service = QualityRefiningService(refined=executable_plan())

    result = await build_planning_subgraph(
        dependencies(PlanningExecutor(), planning_service=service)
    ).ainvoke(planning_state())

    assert result["planning_valid"] is True
    assert service.refine_calls == 1
    assert service.refine_states[0]["planning_quality_gate_decision"] == "refine"
    assert service.refine_states[0]["planning_quality_refinement_required"] is True
    assert service.refine_states[0]["planning_quality_refinement_guidance"]
    assert result["planning_quality_gate_decision"] == "continue"
    assert result["planning_quality_refinement_attempts"] == 1
    assert result["planning_quality_previous_score"] is not None
    assert result["planning_quality_score_delta"] is not None
    assert result["planning_quality_score_delta"] > 0
    assert result["planning_quality_gate_reason"] == "quality_gate_passed"


@pytest.mark.asyncio
async def test_quality_gate_fails_after_exhausted_refinement_attempts_and_preserves_snapshot() -> None:
    service = QualityRefiningService(always_low_quality=True)
    state = planning_state()
    state["max_planning_attempts"] = 1

    result = await build_planning_subgraph(
        dependencies(PlanningExecutor(), planning_service=service)
    ).ainvoke(state)

    assert result["planning_valid"] is False
    assert result["planning_failure_reason"] == "planning_quality_below_threshold"
    assert result["planning_quality_gate_decision"] == "fail"
    assert result["planning_quality_score"] is not None
    assert result["planning_quality_dimensions"]
    assert result["planning_quality_refinement_attempts"] == 1
    assert result["planning_quality_score_delta"] == 0
    assert result["planning_quality_gate_reason"] == "planning_quality_below_threshold"


@pytest.mark.asyncio
async def test_quality_refinement_invalidates_previous_planning_approval() -> None:
    service = QualityRefiningService(refined=executable_plan())
    state = planning_state()
    state.update(
        planning_approval_required=True,
        planning_approval_status="awaiting_approval",
        planning_approval_id="planning-risk-stale",
        planning_policy_decision="require_approval",
        planning_approval_fingerprint="stale-fingerprint",
    )

    result = await build_planning_subgraph(
        dependencies(PlanningExecutor(), planning_service=service)
    ).ainvoke(state)

    assert result["planning_valid"] is True
    assert result["planning_approval_required"] is False
    assert result["planning_approval_status"] == "not_required"
    assert result["planning_approval_id"] is None
    assert result["planning_policy_decision"] is None
    assert result["planning_approval_fingerprint"] is None


@pytest.mark.asyncio
async def test_risk_analysis_not_run_if_contract_validation_fails() -> None:
    service = RefiningService(valid_plan("wrong-project"), always_invalid=True)
    state = planning_state()
    state["max_planning_attempts"] = 0

    result = await build_planning_subgraph(
        dependencies(PlanningExecutor(), planning_service=service)
    ).ainvoke(state)

    assert result["planning_valid"] is False
    assert result["planning_risk_level"] is None
    assert result["planning_impact_areas"] == []


@pytest.mark.asyncio
async def test_risk_analysis_not_run_if_executability_validation_fails() -> None:
    service = ExecutabilityRefiningService(always_invalid=True)
    state = planning_state()
    state["max_planning_attempts"] = 0

    result = await build_planning_subgraph(
        dependencies(PlanningExecutor(), planning_service=service)
    ).ainvoke(state)

    assert result["planning_valid"] is False
    assert result["planning_risk_level"] is None
    assert result["planning_impact_areas"] == []


@pytest.mark.asyncio
async def test_valid_plan_reaches_risk_analysis_and_persists_parent_metadata() -> None:
    class RiskService(ExecutabilityRefiningService):
        async def create_tasks(self, state):
            plan = executable_plan()
            plan.tasks.append(
                ImplementationTask(order=4, role="technical writer", title="Crear README", description="Crear README.md.")
            )
            plan.tasks.append(
                ImplementationTask(order=5, role="developer", title="Crear requirements.txt", description="Crear requirements.txt.", depends_on=[1])
            )
            return plan.tasks

    result = await build_planning_subgraph(
        dependencies(PlanningExecutor(), planning_service=RiskService())
    ).ainvoke(planning_state())

    assert result["planning_valid"] is True
    assert result["planning_risk_level"] in {"low", "medium"}
    assert {"application", "tests", "dependencies", "documentation"} <= set(result["planning_impact_areas"])
    assert not any(task["risk_level"] in {"high", "critical"} for task in result["planning_sensitive_tasks"])


def test_deterministic_validation_preserves_endpoint_and_json_literals() -> None:
    plan = valid_plan()
    assert validate_planning_output(plan, planning_state()) == []

    plan.analysis.functional_requirements = ["Implementar otro endpoint."]
    plan.analysis.constraints = []
    plan.acceptance_criteria[0].description = "La API responde correctamente."
    plan.tasks[0].description = "Implementar la API."

    errors = " ".join(validate_planning_output(plan, planning_state()))
    assert "GET /health" in errors
    assert 'JSON {"status": "ok"}' in errors


@pytest.mark.parametrize("constraint", ["No usar Docker.", "Sin Docker.", "No configurar Docker."])
def test_negative_docker_constraints_are_not_unsolicited_requirements(constraint: str) -> None:
    plan = valid_plan()
    plan.analysis.constraints.append(constraint)
    errors = validate_planning_output(plan, planning_state())
    assert not any("docker" in error.casefold() for error in errors)


def test_actual_docker_task_is_still_an_unsolicited_requirement() -> None:
    plan = valid_plan()
    plan.tasks.append(ImplementationTask(
        order=3, role="DevOps", title="Agregar Docker",
        description="Crear y configurar Docker para el proyecto.", depends_on=[1],
    ))
    errors = validate_planning_output(plan, planning_state())
    assert any("docker" in error.casefold() for error in errors)


def test_router_selects_valid_refine_and_failed() -> None:
    assert route_after_plan_validation({"planning_valid": True}) == "valid"
    assert route_after_plan_validation({"planning_valid": False, "planning_attempts": 0, "max_planning_attempts": 2}) == "refine"
    assert route_after_plan_validation({"planning_valid": False, "planning_attempts": 2, "max_planning_attempts": 2}) == "failed"


class ParseClient:
    def __init__(self, result: Any = None, error: Exception | None = None, delay: float = 0) -> None:
        self.responses = self
        self.result = result
        self.error = error
        self.delay = delay
        self.kwargs: dict[str, Any] | None = None

    async def parse(self, **kwargs):
        self.kwargs = kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_structured_refine_uses_sdk_parser_and_validates_model() -> None:
    client = ParseClient(SimpleNamespace(status="completed", output_parsed=valid_plan(), output=[]))
    service = PlanningService(client, "test-model", PlanningExecutor())  # type: ignore[arg-type]
    state = planning_state()
    plan = valid_plan("wrong-project")
    state.update(
        requirement_analysis=plan.analysis.model_dump(),
        acceptance_criteria=[item.model_dump() for item in plan.acceptance_criteria],
        implementation_tasks=[item.model_dump() for item in plan.tasks],
        planning_errors=["project_name incorrecto"],
    )

    refined = await service.refine(state)

    assert refined.analysis.project_name == "planning-booking"
    assert client.kwargs and client.kwargs["text_format"] is PlanningOutput


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "code"),
    [
        (ParseClient(SimpleNamespace(status="incomplete", output_parsed=None, output=[])), "planning_output_incomplete"),
        (
            ParseClient(SimpleNamespace(status="completed", output_parsed=None, output=[SimpleNamespace(content=[SimpleNamespace(type="refusal")])])),
            "planning_refused",
        ),
        (ParseClient(SimpleNamespace(status="completed", output_parsed={"bad": True}, output=[])), "planning_schema_invalid"),
    ],
)
async def test_structured_refine_controls_invalid_outputs(client: ParseClient, code: str) -> None:
    service = PlanningService(client, "test-model", PlanningExecutor())  # type: ignore[arg-type]

    with pytest.raises(PlanningDomainError) as captured:
        await service.refine(planning_state())

    assert captured.value.code == code


@pytest.mark.asyncio
async def test_structured_refine_controls_timeout() -> None:
    service = PlanningService(ParseClient(delay=0.05), "test-model", PlanningExecutor(), timeout_seconds=0.001)  # type: ignore[arg-type]

    with pytest.raises(PlanningDomainError) as captured:
        await service.refine(planning_state())

    assert captured.value.code == "planning_timeout"


@pytest.mark.asyncio
async def test_parent_creation_runs_planning_and_developer_receives_structured_plan() -> None:
    executor = PlanningExecutor()
    received_state: dict[str, Any] = {}

    async def resolver(name: str, state: SoftwareFactoryState) -> dict[str, Any]:
        received_state.update(state)
        return {
            "project_name": "planning-booking",
            "files": [
                {
                    "path": "planning_booking/main.py",
                    "content": '@app.get("/health")\ndef health():\n    return {"status": "ok"}',
                },
                {
                    "path": "tests/test_health.py",
                    "content": build_fastapi_health_test(
                        "planning_booking", "/health", 200, {"status": "ok"}
                    ),
                },
                {"path": "requirements.txt", "content": "fastapi\npytest\n"},
            ],
        }

    graph = build_software_factory_graph(dependencies(executor, resolver=resolver), checkpointer=InMemorySaver())
    result = await run_software_factory_graph(graph, REQUEST, thread_id="planning-integration")

    assert result.interrupted is True
    assert result.interrupts[0].value["operation"] == "create_project"
    assert received_state["requirement_analysis"]["project_name"] == "planning-booking"
    assert received_state["acceptance_criteria"]
    assert len(received_state["implementation_tasks"]) >= 2
    assert "planning_valid" not in received_state
    assert "filesystem__create_project_structure" not in [name for name, _ in executor.calls]


@pytest.mark.asyncio
async def test_parent_review_bypasses_planning() -> None:
    executor = PlanningExecutor(project_exists=True)
    graph = build_software_factory_graph(dependencies(executor), checkpointer=InMemorySaver())

    result = await run_software_factory_graph(
        graph,
        "Revisa el proyecto planning-booking y ejecuta sus pruebas.",
        thread_id="review-without-planning",
    )

    assert result.interrupted is True
    assert result.interrupts[0].value["operation"] == "prepare_environment"
    assert not any(name.startswith("software_factory__") for name, _ in executor.calls)
