from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from graph.state import SoftwareFactoryState
from graph.subgraphs.planning.models import ImplementationTask, PlanningOutput
from graph.subgraphs.planning.normalization import normalize_framework, normalize_project_type


PATH_PATTERN = re.compile(r"\b[\w.-]+(?:/[\w.-]+)+\b")
CREATE_WORDS = {"add", "agregar", "create", "crear", "generate", "generar", "implement", "implementar"}
CONSUME_WORDS = {"edit", "modificar", "modify", "update", "actualizar"}
TEST_WORDS = {"pytest", "qa", "test", "tests", "prueba", "pruebas", "validar", "validacion"}
IMPLEMENT_WORDS = {
    "api",
    "app",
    "backend",
    "developer",
    "endpoint",
    "fastapi",
    "health",
    "implement",
    "implementar",
    "main.py",
}


@dataclass(frozen=True)
class ExecutabilityValidationResult:
    errors: list[str]
    execution_order: list[int]
    dependency_edges: list[dict[str, int]]
    duration_ms: float


def _error(code: str, message: str) -> str:
    return f"{code}: {message}"


def _task_text(task: ImplementationTask) -> str:
    return f"{task.role} {task.title} {task.description}".casefold()


def _task_label(task: ImplementationTask) -> str:
    return str(task.order)


def _contains_any(text: str, words: set[str]) -> bool:
    return any(word in text for word in words)


def _contains_word(text: str, words: set[str]) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words)


def _is_test_task(task: ImplementationTask) -> bool:
    return _contains_any(_task_text(task), TEST_WORDS)


def _is_implementation_task(task: ImplementationTask) -> bool:
    text = _task_text(task)
    role = task.role.casefold()
    if _is_test_task(task) and not any(word in role for word in ("backend", "developer", "desarrollador")):
        return False
    return _contains_any(text, IMPLEMENT_WORDS) and not (
        _is_test_task(task) and not any(word in text for word in ("implement", "implementar", "developer"))
    )


def _extract_paths(task: ImplementationTask) -> list[str]:
    return list(dict.fromkeys(path.replace("\\", "/") for path in PATH_PATTERN.findall(_task_text(task))))


def _is_created(task: ImplementationTask, path: str) -> bool:
    text = _task_text(task)
    if path.casefold() not in text:
        return False
    if _contains_word(text, CONSUME_WORDS) and not _contains_word(text, {"create", "crear", "generate", "generar"}):
        return False
    return _contains_word(text, CREATE_WORDS)


def _is_consumed_without_create(task: ImplementationTask, path: str) -> bool:
    text = _task_text(task)
    if path.casefold() not in text:
        return False
    return _contains_word(text, CONSUME_WORDS) and not _is_created(task, path)


def _topological_sort(tasks: list[ImplementationTask]) -> tuple[list[int], bool]:
    graph = {task.order: set(task.depends_on) for task in tasks}
    ready = sorted(order for order, deps in graph.items() if not deps)
    result: list[int] = []
    while ready:
        order = ready.pop(0)
        result.append(order)
        for other in sorted(graph):
            if order in graph[other]:
                graph[other].remove(order)
                if not graph[other] and other not in result and other not in ready:
                    ready.append(other)
        ready.sort()
    return result, len(result) == len(tasks)


def _entrypoint_targets(tasks: list[ImplementationTask]) -> list[str]:
    targets: list[str] = []
    for task in tasks:
        if _is_test_task(task):
            continue
        for path in _extract_paths(task):
            if path.endswith("/main.py") and "test" not in path:
                targets.append(path)
    return list(dict.fromkeys(targets))


def _has_fastapi_executable(plan: PlanningOutput) -> bool:
    tasks = plan.tasks
    requirement_text = " ".join(plan.analysis.functional_requirements).casefold()
    for task in tasks:
        text = _task_text(task)
        if _is_test_task(task) and not _is_implementation_task(task):
            continue
        if _is_implementation_task(task) and any(token in text for token in ("fastapi", "endpoint", "main.py", "/health", "api")):
            return True
        if _is_implementation_task(task) and any(token in text for token in ("requisito", "funcional", "proyecto", "ejecutable")):
            return True
        if _is_implementation_task(task) and any(token in requirement_text for token in ("get ", "post ", "endpoint", "/health", "api")):
            return True
    return False


def validate_plan_executability(
    plan: PlanningOutput,
    state: SoftwareFactoryState,
) -> ExecutabilityValidationResult:
    started = time.perf_counter()
    errors: list[str] = []
    tasks = sorted(plan.tasks, key=lambda task: task.order)
    by_order = {task.order: task for task in tasks}
    dependency_edges = [
        {"from": dependency, "to": task.order}
        for task in tasks
        for dependency in task.depends_on
    ]

    for task in tasks:
        if task.order in task.depends_on:
            errors.append(
                _error(
                    "planning_dependency_cycle",
                    f"task '{_task_label(task)}' depends on itself",
                )
            )
        for dependency in task.depends_on:
            if dependency not in by_order:
                errors.append(
                    _error(
                        "planning_dependency_missing",
                        f"task '{_task_label(task)}' depends on unknown task '{dependency}'",
                    )
                )
    if errors:
        return ExecutabilityValidationResult(
            list(dict.fromkeys(errors)),
            [],
            dependency_edges,
            (time.perf_counter() - started) * 1000,
        )

    execution_order, acyclic = _topological_sort(tasks)
    if not acyclic:
        errors.append(_error("planning_dependency_cycle", "task dependency graph contains a cycle"))
        return ExecutabilityValidationResult(
            list(dict.fromkeys(errors)),
            execution_order,
            dependency_edges,
            (time.perf_counter() - started) * 1000,
        )

    implementation_orders = [task.order for task in tasks if _is_implementation_task(task)]
    test_orders = [task.order for task in tasks if _is_test_task(task)]
    if state.get("workflow_intent", "create_project") == "create_project" and test_orders:
        if not implementation_orders:
            errors.append(
                _error(
                    "planning_framework_structure_invalid",
                    "testing tasks require at least one executable implementation task",
                )
            )
        elif min(test_orders) < min(implementation_orders):
            errors.append(
                _error(
                    "planning_task_order_invalid",
                    "testing tasks cannot run before the implementation they validate",
                )
            )

    produced_paths: set[str] = set()
    existing_paths: set[str] = set()
    if state.get("workflow_intent") == "review_existing_project":
        existing_paths.update(path.replace("\\", "/") for path in state.get("generated_files", []))
    for task in tasks:
        for path in _extract_paths(task):
            if _is_consumed_without_create(task, path) and path not in produced_paths and path not in existing_paths:
                errors.append(
                    _error(
                        "planning_artifact_missing",
                        f"task '{_task_label(task)}' consumes missing artifact '{path}'",
                    )
                )
        for path in _extract_paths(task):
            if _is_created(task, path):
                produced_paths.add(path)

    entrypoints = _entrypoint_targets(tasks)
    if len(entrypoints) > 1:
        roots = {path.rsplit("/main.py", 1)[0] for path in entrypoints}
        if len(roots) > 1:
            errors.append(
                _error(
                    "planning_target_inconsistent",
                    f"inconsistent executable entrypoints: {', '.join(entrypoints)}",
                )
            )

    project_type = normalize_project_type(plan.analysis.project_type)
    framework = normalize_framework(plan.analysis.framework, project_type=project_type)
    if project_type == "fastapi" or framework == "fastapi":
        if not _has_fastapi_executable(plan):
            errors.append(
                _error(
                    "planning_framework_structure_invalid",
                    "FastAPI projects require an executable app or endpoint implementation task",
                )
            )
    elif project_type == "node" or framework == "node":
        if not any("package.json" in _task_text(task) or "server" in _task_text(task) for task in tasks):
            errors.append(
                _error(
                    "planning_framework_structure_invalid",
                    "Node projects require a package manifest or server entrypoint task",
                )
            )
    elif project_type == "dotnet" or framework == "dotnet":
        if not any(".csproj" in _task_text(task) or "program.cs" in _task_text(task) for task in tasks):
            errors.append(
                _error(
                    "planning_framework_structure_invalid",
                    ".NET projects require a project or Program.cs entrypoint task",
                )
            )

    for task in tasks:
        text = _task_text(task)
        if "deploy" in text and "package" in text and not any("package" in _task_text(other) or "build" in _task_text(other) for other in tasks if other.order != task.order):
            errors.append(
                _error(
                    "planning_unreachable_task",
                    f"task '{_task_label(task)}' requires an impossible package prerequisite",
                )
            )

    return ExecutabilityValidationResult(
        list(dict.fromkeys(errors)),
        execution_order,
        dependency_edges,
        (time.perf_counter() - started) * 1000,
    )
