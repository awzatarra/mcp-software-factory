from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from graph.builder import build_software_factory_graph
from graph.nodes import GraphDependencies
from graph.persistence_service import WorkflowPersistenceService
from graph.runtime import (
    active_snapshot,
    active_snapshot_values,
    run_software_factory_graph,
    thread_config,
)
from graph.state import SoftwareFactoryState, create_initial_state
from graph.subgraphs.implementation.builder import (
    MISSING_PROJECT_IMPLEMENTATION,
    build_implementation_subgraph,
    canonicalize_project_implementation,
    require_project_implementation,
)
from graph.subgraphs.implementation.models import GeneratedFile, ProjectImplementationPlan
from graph.subgraphs.implementation.test_validation import build_fastapi_health_test
from graph.subgraphs.implementation.routers import (
    route_after_implementation_validation,
    route_implementation_approval,
    route_implementation_entry,
)
from graph.subgraphs.implementation.service import ImplementationDomainError, ImplementationService
from graph.subgraphs.implementation.validators import validate_project_implementation
from tool_executor import ToolExecutionOutcome


REQUEST = 'Crea un proyecto FastAPI llamado implementation-booking con GET /health que devuelva {"status": "ok"}, agrega pruebas.'


def valid_proposal() -> ProjectImplementationPlan:
    return ProjectImplementationPlan(
        project_name="implementation-booking",
        framework="fastapi",
        package_name="implementation_booking",
        files=[
            GeneratedFile(
                path="implementation_booking/main.py",
                content='@app.get("/health")\ndef health():\n    return {"status": "ok"}',
            ),
            GeneratedFile(
                path="tests/test_health.py",
                content=build_fastapi_health_test(
                    "implementation_booking", "/health", 200, {"status": "ok"}
                ),
            ),
            GeneratedFile(
                path="requirements.txt",
                content="fastapi[standard]==0.139.0\npytest>=8,<9\n",
            ),
        ],
    )


def implementation_state(*, existing: bool = False, review: bool = False) -> SoftwareFactoryState:
    state = create_initial_state(REQUEST)
    state.update(
        project_name="implementation-booking",
        workflow_intent="review_existing_project" if review else "create_project",
        project_exists=existing,
        created_project_name="implementation-booking" if existing else None,
        planning_valid=not review,
        requirement_analysis={
            "objective": "Crear API.",
            "project_name": "implementation-booking",
            "project_type": "fastapi",
            "functional_requirements": ['GET /health devuelve {"status": "ok"}.'],
            "non_functional_requirements": [],
            "constraints": [],
            "assumptions": [],
        }
        if not review
        else None,
        acceptance_criteria=[],
        implementation_tasks=[],
    )
    return state


class ImplementationExecutor:
    def __init__(
        self,
        *,
        existing: bool = False,
        infrastructure_failure: bool = False,
        create_policy_failure: bool = False,
    ) -> None:
        self.existing = existing
        self.infrastructure_failure = infrastructure_failure
        self.create_policy_failure = create_policy_failure
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def openai_tool(self, name: str) -> dict[str, Any]:
        return {"type": "function", "name": name, "parameters": {"type": "object"}}

    async def execute(self, name, arguments, *, state_context, approval_mode="prompt"):
        self.calls.append((name, arguments))
        payload: dict[str, Any] = {"success": True}
        updates: dict[str, Any] = {}
        if name == "software_factory__analyze_requirement":
            updates["analysis_completed"] = True
        elif name == "software_factory__create_tasks":
            updates["tasks_created"] = True
        elif name == "filesystem__list_files":
            updates.update(workspace_inspected=True, project_exists=self.existing)
            if self.existing:
                updates["created_project_name"] = "implementation-booking"
        elif name == "filesystem__create_project_structure":
            if self.create_policy_failure:
                payload = {
                    "success": False,
                    "status": "dependency_policy_violation",
                    "message": "Dependencias rechazadas.",
                }
            else:
                updates.update(project_created=True, created_project_name="implementation-booking")
                payload["project_name"] = "implementation-booking"
        elif name == "testing__detect_test_framework":
            updates.update(detected_test_framework="pytest", expected_test_command=["python", "-m", "pytest"])
        elif name == "testing__prepare_test_environment":
            if self.infrastructure_failure:
                payload = {"success": False, "status": "dependency_installation_failure"}
                updates.update(
                    test_infrastructure_failed=True,
                    terminal_status="infrastructure_failed",
                    failure_type="dependency_installation_failure",
                )
            else:
                updates.update(environment_prepared=True, dependencies_installed=True, environment_python=".venv/python")
        elif name == "testing__run_tests":
            updates.update(tests_executed=True, tests_passed=True, final_test_result_summary="1 passed")
        return ToolExecutionOutcome(name, arguments, payload, updates)


async def resolver(name: str, state: SoftwareFactoryState) -> dict[str, Any]:
    proposal = valid_proposal()
    proposal.files[-1] = GeneratedFile(
        path="requirements.txt",
        content="fastapi\npytest\nhttpx\nuvicorn==0.30\n",
    )
    return {"project_name": proposal.project_name, "files": [file.model_dump() for file in proposal.files]}


def dependencies(executor: ImplementationExecutor, *, service: Any = None) -> GraphDependencies:
    return GraphDependencies(
        tool_executor=executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
        argument_resolver=resolver,
        implementation_service=service,
    )


def test_implementation_subgraph_compiles_and_parent_is_simplified() -> None:
    deps = dependencies(ImplementationExecutor())
    subgraph = build_implementation_subgraph(deps)
    parent = build_software_factory_graph(deps, checkpointer=InMemorySaver())
    internal = {
        "prepare_developer_knowledge",
        "prepare_create_project",
        "normalize_dependencies",
        "validate_implementation",
        "refine_implementation",
        "approval",
        "execute_create_project",
        "detect_test_framework",
        "prepare_environment_request",
        "execute_prepare_environment",
    }

    assert subgraph.checkpointer is None
    assert internal <= set(subgraph.get_graph().nodes)
    assert "implementation" in parent.get_graph().nodes
    assert internal.isdisjoint(parent.get_graph().nodes)


def test_entry_router_handles_create_existing_review_and_missing() -> None:
    assert route_implementation_entry(implementation_state()) == "create"
    assert route_implementation_entry(implementation_state(existing=True)) == "existing"
    assert route_implementation_entry(implementation_state(existing=True, review=True)) == "existing"
    assert route_implementation_entry(implementation_state(review=True)) == "failed"


def test_project_implementation_guard_preserves_valid_dictionary() -> None:
    proposal = valid_proposal().model_dump()
    state = implementation_state()
    state["project_implementation"] = proposal

    raw_proposal = require_project_implementation(state)

    assert raw_proposal is proposal
    assert ProjectImplementationPlan.model_validate(raw_proposal) == valid_proposal()


@pytest.mark.parametrize("invalid", [None, (), [], "invalid"])
def test_project_implementation_guard_rejects_missing_or_invalid_state(invalid: Any) -> None:
    state = implementation_state()
    state["project_implementation"] = invalid  # type: ignore[assignment]

    with pytest.raises(ImplementationDomainError) as exc:
        require_project_implementation(state)

    assert exc.value.code == MISSING_PROJECT_IMPLEMENTATION


@pytest.mark.asyncio
async def test_missing_generated_proposal_fails_with_explicit_state_error() -> None:
    class MissingProposalService:
        async def generate_project(self, state):
            raise ImplementationDomainError("provider_output_missing", "No proposal returned")

    result = await build_implementation_subgraph(
        dependencies(ImplementationExecutor(), service=MissingProposalService())
    ).ainvoke(implementation_state())

    assert result["terminal_status"] == "implementation_failed"
    assert result["failure_type"] == MISSING_PROJECT_IMPLEMENTATION
    assert result["implementation_errors"] == [MISSING_PROJECT_IMPLEMENTATION]


@pytest.mark.parametrize("path", ["../main.py", "/tmp/main.py", r"C:\\temp\\main.py"])
def test_generated_file_rejects_traversal_and_absolute_paths(path: str) -> None:
    with pytest.raises(ValueError):
        GeneratedFile(path=path, content="x")


def test_project_model_rejects_duplicate_paths() -> None:
    with pytest.raises(ValueError, match="unique"):
        ProjectImplementationPlan(
            project_name="implementation-booking",
            framework="fastapi",
            package_name="implementation_booking",
            files=[GeneratedFile(path="main.py", content="a"), GeneratedFile(path="MAIN.py", content="b")],
        )


def test_package_identifier_is_python_specific() -> None:
    with pytest.raises(ValueError, match="Python identifier"):
        ProjectImplementationPlan(
            project_name="implementation-booking",
            framework="fastapi",
            package_name="implementation-booking",
            files=[GeneratedFile(path="main.py", content="x")],
        )

    node_plan = ProjectImplementationPlan(
        project_name="implementation-booking",
        framework="node",
        package_name="implementation-booking",
        files=[GeneratedFile(path="index.js", content="module.exports = {}")],
    )
    assert node_plan.package_name == "implementation-booking"


def test_project_prefixed_paths_are_canonicalized_relative_to_project_root() -> None:
    plan = ProjectImplementationPlan.model_validate({
        "project_name": "phase-6-19-git-e2e-5",
        "framework": "fastapi",
        "package_name": "phase_6_19_git_e2e_5",
        "files": [
            {"path": "phase-6-19-git-e2e-5/README.md", "content": "# demo\n"},
            {"path": "phase-6-19-git-e2e-5/requirements.txt", "content": "fastapi[standard]==0.139.0\npytest>=8,<9\n"},
            {"path": "phase-6-19-git-e2e-5/phase_6_19_git_e2e_5/main.py", "content": "app = 1\n"},
            {"path": "phase-6-19-git-e2e-5/tests/test_health.py", "content": "def test_health():\n    assert True\n"},
        ],
    })

    assert [file.path for file in plan.files] == [
        "README.md",
        "requirements.txt",
        "phase_6_19_git_e2e_5/main.py",
        "tests/test_health.py",
    ]


def test_mixed_prefixed_and_unprefixed_duplicate_paths_collapse() -> None:
    plan = ProjectImplementationPlan.model_validate({
        "project_name": "phase-6-19-git-e2e-5",
        "framework": "fastapi",
        "package_name": "phase_6_19_git_e2e_5",
        "files": [
            {"path": "phase-6-19-git-e2e-5/requirements.txt", "content": "fastapi\n"},
            {"path": "requirements.txt", "content": "fastapi\n"},
            {"path": "phase-6-19-git-e2e-5/tests/test_health.py", "content": "def test_health(): pass\n"},
            {"path": "tests/test_health.py", "content": "def test_health(): pass\n"},
        ],
    })

    assert [file.path for file in plan.files] == ["requirements.txt", "tests/test_health.py"]


def test_conflicting_duplicate_content_is_rejected() -> None:
    with pytest.raises(ValueError, match="implementation_path_conflict: requirements.txt"):
        ProjectImplementationPlan.model_validate({
            "project_name": "phase-6-19-git-e2e-5",
            "framework": "fastapi",
            "package_name": "phase_6_19_git_e2e_5",
            "files": [
                {"path": "phase-6-19-git-e2e-5/requirements.txt", "content": "fastapi\n"},
                {"path": "requirements.txt", "content": "pytest\n"},
            ],
        })


def test_windows_separators_are_normalized_before_project_prefix_removal() -> None:
    plan = ProjectImplementationPlan.model_validate({
        "project_name": "phase-6-19-git-e2e-5",
        "framework": "fastapi",
        "package_name": "phase_6_19_git_e2e_5",
        "files": [
            {"path": r"phase-6-19-git-e2e-5\phase_6_19_git_e2e_5\main.py", "content": "app = 1\n"},
        ],
    })

    assert plan.files[0].path == "phase_6_19_git_e2e_5/main.py"


@pytest.mark.parametrize("path", ["/tmp/main.py", r"C:\temp\main.py", "../main.py", ".git/config"])
def test_project_implementation_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError):
        ProjectImplementationPlan.model_validate({
            "project_name": "phase-6-19-git-e2e-5",
            "framework": "fastapi",
            "package_name": "phase_6_19_git_e2e_5",
            "files": [{"path": path, "content": "x"}],
        })


def test_validate_implementation_sees_canonical_main_path_for_real_workflow_shape() -> None:
    state = create_initial_state(
        'Crea un proyecto FastAPI llamado phase-6-19-git-e2e-5 con GET /health que devuelva {"status": "ok"}, agrega pruebas.'
    )
    state.update(
        project_name="phase-6-19-git-e2e-5",
        requirement_analysis={
            "project_name": "phase-6-19-git-e2e-5",
            "project_type": "fastapi",
        },
    )
    plan = ProjectImplementationPlan.model_validate({
        "project_name": "phase-6-19-git-e2e-5",
        "framework": "fastapi",
        "package_name": "phase_6_19_git_e2e_5",
        "files": [
            {
                "path": "phase-6-19-git-e2e-5/phase_6_19_git_e2e_5/main.py",
                "content": 'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/health")\ndef health():\n    return {"status": "ok"}\n',
            },
            {
                "path": "phase-6-19-git-e2e-5/tests/test_health.py",
                "content": build_fastapi_health_test("phase_6_19_git_e2e_5", "/health", 200, {"status": "ok"}),
            },
            {
                "path": "phase-6-19-git-e2e-5/requirements.txt",
                "content": "fastapi[standard]==0.139.0\npytest>=8,<9\n",
            },
        ],
    })

    assert "phase_6_19_git_e2e_5/main.py" in [file.path for file in plan.files]
    assert validate_project_implementation(state, plan) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda plan: setattr(plan, "project_name", "other"), "project_name"),
        (lambda plan: plan.files.pop(0), "archivo principal"),
        (lambda plan: setattr(plan, "files", [file for file in plan.files if not file.path.startswith("tests/")]), "archivo de test"),
        (lambda plan: setattr(plan, "files", [file for file in plan.files if file.path != "requirements.txt"]), "dependencias"),
        (
            lambda plan: setattr(
                plan.files[1],
                "content",
                build_fastapi_health_test("wrong", "/health", 200, {"status": "ok"}),
            ),
            "debe importar",
        ),
        (lambda plan: setattr(plan.files[0], "content", 'return {"status": "ok"}'), "GET /health"),
        (
            lambda plan: (
                setattr(plan.files[0], "content", '@app.get("/health")\nreturn {"status": "healthy"}'),
                setattr(
                    plan.files[1],
                    "content",
                    build_fastapi_health_test(
                        "implementation_booking", "/health", 200, {"status": "healthy"}
                    ),
                ),
            ),
            "literal JSON",
        ),
    ],
)
def test_deterministic_implementation_validation(mutation, expected: str) -> None:
    proposal = valid_proposal()
    mutation(proposal)

    assert expected in " ".join(validate_project_implementation(implementation_state(), proposal))


def test_valid_implementation_has_no_errors() -> None:
    assert validate_project_implementation(implementation_state(), valid_proposal()) == []


@pytest.mark.asyncio
async def test_implementation_service_owns_minimal_health_test_template() -> None:
    async def non_collectable_resolver(name, state):
        proposal = valid_proposal()
        proposal.files[1] = proposal.files[1].model_copy(
            update={
                "path": "tests/health.py",
                "content": "def health_test():\n    assert True\n",
            }
        )
        return {
            "project_name": proposal.project_name,
            "files": [file.model_dump() for file in proposal.files],
        }

    service = ImplementationService(
        object(),  # type: ignore[arg-type]
        "test-model",
        argument_resolver=non_collectable_resolver,
    )

    proposal = await service.generate_project(implementation_state())
    test_file = next(file for file in proposal.files if file.path.startswith("tests/"))

    assert test_file.path == "tests/test_health.py"
    assert "def test_health():" in test_file.content
    assert "client = TestClient(app)" in test_file.content
    assert validate_project_implementation(implementation_state(), proposal) == []


class RefiningService:
    def __init__(self, *, always_invalid: bool = False) -> None:
        self.always_invalid = always_invalid
        self.refine_calls = 0

    async def generate_project(self, state):
        proposal = valid_proposal()
        proposal.files = [file for file in proposal.files if not file.path.startswith("tests/")]
        return proposal

    async def refine_project(self, state):
        self.refine_calls += 1
        if self.always_invalid:
            return await self.generate_project(state)
        return valid_proposal()


class OptionalDependencyService:
    def __init__(self) -> None:
        self.refine_calls = 0

    async def generate_project(self, state):
        proposal = valid_proposal()
        proposal.files[-1] = GeneratedFile(
            path="requirements.txt",
            content="fastapi\npytest\nhttpx==0.20\nuvicorn\n",
        )
        return proposal

    async def refine_project(self, state):
        self.refine_calls += 1
        return await self.generate_project(state)


class IncompatibleClientService:
    def __init__(self) -> None:
        self.refinement_errors: list[str] = []

    async def generate_project(self, state):
        proposal = valid_proposal()
        proposal.files[1] = GeneratedFile(
            path="tests/test_health.py",
            content=(
                "from httpx import Client\n"
                "from implementation_booking.main import app\n\n"
                "def test_health():\n"
                "    with Client(app=app, base_url='http://test') as client:\n"
                "        response = client.get('/health')\n"
                "    assert response.status_code == 200\n"
                "    assert response.json() == {'status': 'ok'}\n"
            ),
        )
        return proposal

    async def refine_project(self, state):
        self.refinement_errors = list(state.get("implementation_errors", []))
        proposal = valid_proposal()
        proposal.files[-1] = GeneratedFile(
            path="requirements.txt", content="fastapi\npytest\nhttpx\n"
        )
        return proposal


class NonCollectableTestService:
    def __init__(self) -> None:
        self.refinement_errors: list[str] = []

    async def generate_project(self, state):
        proposal = valid_proposal()
        proposal.files[1] = GeneratedFile(
            path="tests/test_health.py",
            content=build_fastapi_health_test(
                "implementation_booking", "/health", 200, {"status": "ok"}
            ).replace("def test_health():", "def health_test():"),
        )
        return proposal

    async def refine_project(self, state):
        self.refinement_errors = list(state.get("implementation_errors", []))
        return valid_proposal()


class PrefixedPathService:
    async def generate_project(self, state):
        proposal = valid_proposal()
        return proposal.model_copy(
            update={
                "files": [
                    GeneratedFile(path=f"{proposal.project_name}/README.md", content="# demo\n"),
                    GeneratedFile(
                        path=f"{proposal.project_name}/requirements.txt",
                        content="fastapi[standard]==0.139.0\npytest>=8,<9\n",
                    ),
                    GeneratedFile(
                        path=f"{proposal.project_name}/{proposal.package_name}/main.py",
                        content=proposal.files[0].content,
                    ),
                    GeneratedFile(
                        path=f"{proposal.project_name}/tests/test_health.py",
                        content=proposal.files[1].content,
                    ),
                ],
            }
        )

    async def refine_project(self, state):
        return await self.generate_project(state)


@pytest.mark.asyncio
async def test_invalid_proposal_refines_then_requests_approval() -> None:
    service = RefiningService()
    graph = build_implementation_subgraph(dependencies(ImplementationExecutor(), service=service))
    result = await graph.ainvoke(implementation_state())

    assert result["implementation_attempts"] == 1
    assert result["implementation_valid"] is True
    assert result["__interrupt__"]
    assert result["__interrupt__"][0].value["operation"] == "create_project"


def test_non_pytest_file_name_is_rejected() -> None:
    proposal = valid_proposal()
    proposal.files[1] = proposal.files[1].model_copy(update={"path": "tests/health.py"})

    errors = validate_project_implementation(implementation_state(), proposal)

    assert "invalid_pytest_test_file_name: tests/health.py" in errors


@pytest.mark.asyncio
async def test_non_collectable_test_refines_before_approval() -> None:
    service = NonCollectableTestService()
    executor = ImplementationExecutor()
    graph = build_implementation_subgraph(dependencies(executor, service=service))

    result = await graph.ainvoke(implementation_state())

    assert "pytest_no_collectable_tests" in service.refinement_errors
    assert result["implementation_attempts"] == 1
    assert result["implementation_valid"] is True
    assert result["collectable_test_count"] == 1
    assert result["test_functions"] == ["test_health"]
    assert result["__interrupt__"][0].value["operation"] == "create_project"
    preview = result["__interrupt__"][0].value["preview"]
    assert preview["test_framework"] == "pytest"
    assert preview["collectable_test_count"] == 1
    assert preview["test_functions"] == ["test_health"]
    assert "filesystem__create_project_structure" not in [name for name, _ in executor.calls]


@pytest.mark.asyncio
async def test_incompatible_fastapi_client_refines_before_approval() -> None:
    service = IncompatibleClientService()
    executor = ImplementationExecutor()
    graph = build_implementation_subgraph(dependencies(executor, service=service))

    result = await graph.ainvoke(implementation_state())

    assert "unsupported_fastapi_sync_test_client" in service.refinement_errors
    assert "httpx_client_app_argument_not_supported" in service.refinement_errors
    assert result["implementation_attempts"] == 1
    assert result["implementation_valid"] is True
    assert "unsupported_fastapi_sync_test_client" in result["resolved_validation_errors"]
    assert result["remaining_validation_errors"] == []
    assert result["__interrupt__"][0].value["operation"] == "create_project"
    assert "filesystem__create_project_structure" not in [name for name, _ in executor.calls]
    test_file = next(
        file for file in result["project_implementation"]["files"] if file["path"] == "tests/test_health.py"
    )
    assert "from fastapi.testclient import TestClient" in test_file["content"]
    assert "client = TestClient(app)" in test_file["content"]


@pytest.mark.asyncio
async def test_refinement_preserves_normalized_dependencies() -> None:
    service = IncompatibleClientService()
    graph = build_implementation_subgraph(
        dependencies(ImplementationExecutor(), service=service)
    )

    result = await graph.ainvoke(implementation_state())

    requirements = next(
        file
        for file in result["project_implementation"]["files"]
        if file["path"] == "requirements.txt"
    )
    assert requirements["content"] == result["normalized_dependency_file"]


@pytest.mark.asyncio
async def test_refined_fastapi_project_passes_without_repair() -> None:
    service = IncompatibleClientService()
    executor = ImplementationExecutor()
    graph = build_software_factory_graph(
        dependencies(executor, service=service), checkpointer=InMemorySaver()
    )
    result = await run_software_factory_graph(
        graph, REQUEST, thread_id="implementation-client-refinement"
    )
    persistence = WorkflowPersistenceService(graph)

    assert result.interrupts[0].value["operation"] == "create_project"
    result = await persistence.approve(result.thread_id)
    assert result.interrupts[0].value["operation"] == "prepare_environment"
    result = await persistence.approve(result.thread_id)
    assert result.interrupts[0].value["operation"] == "run_tests"
    result = await persistence.approve(result.thread_id)

    assert result.final_state["tests_passed"] is True
    assert result.final_state["repair_attempts"] == 0
    assert result.final_state["repair_phase"] == "not_started"
    assert result.final_state["planning_result"]["valid"] is True
    assert result.final_state["implementation_result"]["project_created"] is True
    assert result.final_state["implementation_result"]["environment_prepared"] is True
    assert result.final_state["testing_result"]["tests_passed"] is True
    assert result.final_state["testing_result"]["repair_attempts"] == 0
    assert [name for name, _ in executor.calls].count("testing__run_tests") == 1
    assert not any(name == "filesystem__read_file" for name, _ in executor.calls)


@pytest.mark.asyncio
async def test_known_dependency_versions_do_not_call_refine() -> None:
    service = OptionalDependencyService()
    graph = build_implementation_subgraph(
        dependencies(ImplementationExecutor(), service=service)
    )
    result = await graph.ainvoke(implementation_state())

    assert service.refine_calls == 0
    assert result["implementation_attempts"] == 0
    assert result["dependency_normalization_attempts"] == 1
    assert result["implementation_valid"] is True
    assert result["normalized_dependency_file"] == (
        "fastapi[standard]==0.139.0\n"
        "pytest>=8,<9\n"
        "httpx>=0.23,<1\n"
        "uvicorn>=0.17,<1\n"
    )
    assert result["__interrupt__"][0].value["operation"] == "create_project"


@pytest.mark.asyncio
async def test_dependency_normalization_loop_stops_without_refine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graph.subgraphs.implementation.builder as implementation_builder

    service = OptionalDependencyService()
    monkeypatch.setattr(
        implementation_builder,
        "normalize_dependency_file",
        lambda framework, files: list(files),
    )
    graph = build_implementation_subgraph(
        dependencies(ImplementationExecutor(), service=service)
    )
    result = await graph.ainvoke(implementation_state())

    assert service.refine_calls == 0
    assert result["implementation_attempts"] == 0
    assert result["dependency_normalization_attempts"] == 2
    assert result["terminal_status"] == "implementation_failed"
    assert result["failure_type"] == "dependency_policy_normalization_failed"
    assert "__interrupt__" not in result


@pytest.mark.asyncio
async def test_development_flag_forces_one_refinement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGGRAPH_DEVELOPMENT", "true")
    monkeypatch.setenv("IMPLEMENTATION_FORCE_INVALID_FIRST_ATTEMPT", "true")
    service = RefiningService()
    graph = build_implementation_subgraph(dependencies(ImplementationExecutor(), service=service))
    result = await graph.ainvoke(implementation_state())

    assert service.refine_calls == 1
    assert result["implementation_attempts"] == 1
    assert result["implementation_valid"] is True
    assert result["__interrupt__"][0].value["operation"] == "create_project"


@pytest.mark.asyncio
async def test_refinement_limit_sets_implementation_failed_without_approval() -> None:
    service = RefiningService(always_invalid=True)
    state = implementation_state()
    state["max_implementation_attempts"] = 2
    result = await build_implementation_subgraph(dependencies(ImplementationExecutor(), service=service)).ainvoke(state)

    assert service.refine_calls == 1
    assert result["implementation_attempts"] == 1
    assert result["terminal_status"] == "implementation_failed"
    assert result["failure_type"] == "implementation_refinement_made_no_progress"
    assert result["remaining_validation_errors"] == result["refinement_input_errors"]
    assert "__interrupt__" not in result


def test_approval_router_rejects_testing_operations() -> None:
    state = implementation_state()
    state.update(pending_approval_status="approved", pending_operation="run_tests")
    with pytest.raises(ValueError, match="no permitida"):
        route_implementation_approval(state)


@pytest.mark.asyncio
async def test_parent_creation_approvals_execute_sensitive_tools_once() -> None:
    executor = ImplementationExecutor()
    graph = build_software_factory_graph(dependencies(executor), checkpointer=InMemorySaver())
    result = await run_software_factory_graph(graph, REQUEST, thread_id="implementation-create")
    service = WorkflowPersistenceService(graph)

    assert result.interrupted and result.interrupts[0].value["operation"] == "create_project"
    assert result.final_state["project_implementation"]["project_name"] == "implementation-booking"
    assert result.final_state["dependency_policy_applied"] is True
    assert result.final_state["normalized_dependency_file"] == (
        "fastapi[standard]==0.139.0\n"
        "pytest>=8,<9\n"
        "httpx>=0.23,<1\n"
        "uvicorn>=0.17,<1\n"
    )
    assert result.final_state["dependency_policy_errors"] == []
    assert result.final_state["pending_approval_preview"]["normalized_dependencies"] == [
        "fastapi[standard]==0.139.0",
        "pytest>=8,<9",
        "httpx>=0.23,<1",
        "uvicorn>=0.17,<1",
    ]
    assert "filesystem__create_project_structure" not in [name for name, _ in executor.calls]
    result = await service.approve(result.thread_id)
    assert result.interrupted and result.interrupts[0].value["operation"] == "prepare_environment"
    assert [name for name, _ in executor.calls].count("filesystem__create_project_structure") == 1
    create_arguments = next(
        arguments for name, arguments in executor.calls if name == "filesystem__create_project_structure"
    )
    requirements = next(file for file in create_arguments["files"] if file["path"] == "requirements.txt")
    assert requirements["content"] == (
        "fastapi[standard]==0.139.0\n"
        "pytest>=8,<9\n"
        "httpx>=0.23,<1\n"
        "uvicorn>=0.17,<1\n"
    )
    assert result.final_state["implementation_attempts"] == 0
    assert result.final_state["dependency_normalization_attempts"] == 1
    result = await service.approve(result.thread_id)
    assert result.interrupted and result.interrupts[0].value["operation"] == "run_tests"
    result = await service.approve(result.thread_id)

    assert result.final_state["project_created"] is True
    assert result.final_state["environment_prepared"] is True
    assert result.final_state["tests_passed"] is True
    assert result.final_state["generated_package_name"] == "implementation_booking"
    assert result.final_state["generated_files"] == [file.path for file in valid_proposal().files]


@pytest.mark.asyncio
async def test_filesystem_create_receives_only_canonical_paths_from_prefixed_proposal() -> None:
    executor = ImplementationExecutor()
    graph = build_software_factory_graph(
        dependencies(executor, service=PrefixedPathService()),
        checkpointer=InMemorySaver(),
    )
    result = await run_software_factory_graph(graph, REQUEST, thread_id="implementation-prefixed-paths")

    assert result.interrupted and result.interrupts[0].value["operation"] == "create_project"
    canonical_paths = [file["path"] for file in result.final_state["project_implementation"]["files"]]
    assert canonical_paths == [
        "README.md",
        "requirements.txt",
        "implementation_booking/main.py",
        "tests/test_health.py",
    ]

    result = await WorkflowPersistenceService(graph).approve(result.thread_id)
    create_arguments = next(
        arguments for name, arguments in executor.calls if name == "filesystem__create_project_structure"
    )

    assert [file["path"] for file in create_arguments["files"]] == canonical_paths
    assert all(not file["path"].startswith("implementation-booking/") for file in create_arguments["files"])
    assert result.interrupted and result.interrupts[0].value["operation"] == "prepare_environment"


@pytest.mark.asyncio
async def test_project_implementation_survives_parent_checkpoint_restore() -> None:
    checkpointer = InMemorySaver()
    graph = build_software_factory_graph(
        dependencies(ImplementationExecutor()),
        checkpointer=checkpointer,
    )
    thread_id = "implementation-proposal-checkpoint"

    result = await run_software_factory_graph(graph, REQUEST, thread_id=thread_id)
    restored = await active_snapshot(graph, thread_config(thread_id))
    raw_proposal = active_snapshot_values(restored).get("project_implementation")

    assert result.interrupted is True
    assert isinstance(raw_proposal, dict)
    assert raw_proposal == result.final_state["project_implementation"]
    assert ProjectImplementationPlan.model_validate(raw_proposal) == valid_proposal().model_copy(
        update={
            "files": [
                file.model_copy(
                    update={
                        "content": (
                            "fastapi[standard]==0.139.0\n"
                            "pytest>=8,<9\n"
                            "httpx>=0.23,<1\n"
                            "uvicorn>=0.17,<1\n"
                        )
                    }
                )
                if file.path == "requirements.txt"
                else file
                for file in valid_proposal().files
            ]
        }
    )


@pytest.mark.asyncio
async def test_rejected_create_does_not_execute_tool() -> None:
    executor = ImplementationExecutor()
    graph = build_software_factory_graph(dependencies(executor), checkpointer=InMemorySaver())
    result = await run_software_factory_graph(graph, REQUEST, thread_id="implementation-reject")
    result = await WorkflowPersistenceService(graph).reject(result.thread_id, "No crear")

    assert result.final_state["terminal_status"] == "user_cancelled"
    assert "filesystem__create_project_structure" not in [name for name, _ in executor.calls]


@pytest.mark.asyncio
async def test_create_dependency_policy_failure_is_terminal_and_not_retried() -> None:
    executor = ImplementationExecutor(create_policy_failure=True)
    graph = build_software_factory_graph(dependencies(executor), checkpointer=InMemorySaver())
    result = await run_software_factory_graph(graph, REQUEST, thread_id="implementation-policy-failure")
    result = await WorkflowPersistenceService(graph).approve(result.thread_id)

    assert result.interrupted is False
    assert result.final_state["terminal_status"] == "implementation_failed"
    assert result.final_state["failure_type"] == "dependency_policy_violation"
    assert result.final_state["failure_stage"] == "execute_create_project"
    assert result.final_state["project_created"] is False
    assert [name for name, _ in executor.calls].count("filesystem__create_project_structure") == 1


class UnsupportedDependencyService:
    async def generate_project(self, state):
        proposal = valid_proposal()
        proposal.files[-1] = GeneratedFile(
            path="requirements.txt",
            content="fastapi\nrequests>=2\n",
        )
        return proposal

    async def refine_project(self, state):
        return await self.generate_project(state)


@pytest.mark.asyncio
async def test_unsupported_dependency_never_reaches_approval() -> None:
    state = implementation_state()
    state["max_implementation_attempts"] = 0
    result = await build_implementation_subgraph(
        dependencies(ImplementationExecutor(), service=UnsupportedDependencyService())
    ).ainvoke(state)

    assert result["terminal_status"] == "implementation_failed"
    assert "unsupported_dependency: requests>=2" in result["dependency_policy_errors"]
    assert result.get("pending_tool_name") is None
    assert "__interrupt__" not in result


@pytest.mark.asyncio
async def test_existing_project_skips_generation_and_creation() -> None:
    executor = ImplementationExecutor(existing=True)
    graph = build_software_factory_graph(dependencies(executor), checkpointer=InMemorySaver())
    result = await run_software_factory_graph(
        graph,
        "Revisa el proyecto implementation-booking y ejecuta sus pruebas.",
        thread_id="implementation-existing",
    )

    assert result.interrupted and result.interrupts[0].value["operation"] == "prepare_environment"
    names = [name for name, _ in executor.calls]
    assert "filesystem__create_project_structure" not in names
    assert "testing__detect_test_framework" in names
    assert not any(name.startswith("software_factory__") for name in names)
    assert result.final_state["dependency_policy_applied"] is False
    assert result.final_state["normalized_dependency_file"] is None


@pytest.mark.asyncio
async def test_missing_review_project_finishes_as_implementation_failed() -> None:
    executor = ImplementationExecutor(existing=False)
    graph = build_software_factory_graph(dependencies(executor), checkpointer=InMemorySaver())
    result = await run_software_factory_graph(
        graph,
        "Revisa el proyecto implementation-booking y ejecuta sus pruebas.",
        thread_id="implementation-missing",
    )

    assert result.final_state["terminal_status"] == "implementation_failed"
    assert result.final_state["failure_type"] == "project_not_found"


@pytest.mark.asyncio
async def test_environment_infrastructure_failure_finishes_parent_workflow() -> None:
    executor = ImplementationExecutor(existing=True, infrastructure_failure=True)
    graph = build_software_factory_graph(dependencies(executor), checkpointer=InMemorySaver())
    result = await run_software_factory_graph(
        graph,
        "Revisa el proyecto implementation-booking y ejecuta sus pruebas.",
        thread_id="implementation-infrastructure-failure",
    )
    result = await WorkflowPersistenceService(graph).approve(result.thread_id)

    assert result.interrupted is False
    assert result.final_state["terminal_status"] == "infrastructure_failed"
    assert result.final_state["test_infrastructure_failed"] is True
    assert "testing__run_tests" not in [name for name, _ in executor.calls]


def test_validation_router_handles_valid_refine_and_limit() -> None:
    assert route_after_implementation_validation({"implementation_valid": True}) == "valid"
    assert route_after_implementation_validation(
        {
            "implementation_valid": False,
            "implementation_errors": ["incompatible_dependency_version: httpx"],
            "dependency_normalization_attempts": 1,
        }
    ) == "normalize"
    assert route_after_implementation_validation(
        {
            "implementation_valid": False,
            "implementation_errors": ["incompatible_dependency_version: httpx"],
            "dependency_normalization_attempts": 2,
        }
    ) == "failed"
    assert route_after_implementation_validation({"implementation_valid": False, "implementation_attempts": 0, "max_implementation_attempts": 2}) == "refine"
    assert route_after_implementation_validation({"implementation_valid": False, "implementation_attempts": 2, "max_implementation_attempts": 2}) == "failed"
