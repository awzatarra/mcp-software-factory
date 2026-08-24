from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI
from pydantic import ValidationError

from graph.state import SoftwareFactoryState
from graph.subgraphs.planning.models import (
    AcceptanceCriterion,
    ImplementationTask,
    PlanningOutput,
    RequirementAnalysis,
)
from graph.subgraphs.planning.prompts import PLANNER_PROMPT, REFINEMENT_PROMPT
from graph.subgraphs.planning.normalization import (
    SUPPORTED_PROJECT_TYPES,
    normalize_framework,
    normalize_project_type,
)
from tool_executor import ToolExecutor


ALLOWED_PLANNING_TOOLS = {
    "software_factory__analyze_requirement",
    "software_factory__create_tasks",
}
PROHIBITED_FEATURES = {"authentication", "database", "docker"}
SCOPE_EXPANSIONS = {"authentication", "database", "docker", "kubernetes", "redis", "terraform"}
ENDPOINT_PATTERN = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+(/[^\s,.;]+)", re.IGNORECASE)
JSON_PATTERN = re.compile(r"\{\s*[\"'][^{}\r\n]+?\}")


class PlanningDomainError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _project_type(message: str) -> str:
    lowered = message.lower()
    if "dotnet" in lowered or ".net" in lowered:
        return "dotnet"
    if "node" in lowered or "express" in lowered:
        return "node"
    return "fastapi"


def _feature_polarity(text: str, feature: str) -> tuple[bool, bool]:
    """Return positive/negative mentions using bounded deterministic clauses."""
    positive = False
    negative = False
    for match in re.finditer(rf"\b{re.escape(feature)}\b", text, re.IGNORECASE):
        clause_start = max(
            text.rfind(separator, 0, match.start())
            for separator in (".", ";", "\n", "!", "?")
        ) + 1
        prefix = text[clause_start:match.start()].casefold()
        local = " ".join(re.findall(r"[a-zÃ¡Ã©Ã­Ã³ÃºÃ±]+", prefix)[-10:])
        if re.search(
            r"\b(?:no|sin|not|without|never|prohibid[oa]s?|evitar|excluir)\b",
            local,
            re.IGNORECASE,
        ):
            negative = True
        else:
            positive = True
    return positive, negative


def _literal_contracts(message: str) -> tuple[list[str], list[str]]:
    endpoints = [match.group(0) for match in ENDPOINT_PATTERN.finditer(message)]
    json_literals = [match.group(0) for match in JSON_PATTERN.finditer(message)]
    return list(dict.fromkeys(endpoints)), list(dict.fromkeys(json_literals))


def _error(code: str, message: str) -> str:
    return f"{code}: {message}"


def _basic_normalize(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _task_identity(task: ImplementationTask) -> tuple[Any, ...]:
    return (
        _basic_normalize(task.role),
        _basic_normalize(task.title),
        _basic_normalize(task.description),
        tuple(sorted(task.depends_on)),
    )


def _analysis_from_state(state: SoftwareFactoryState) -> RequirementAnalysis | None:
    raw = state.get("requirement_analysis")
    if raw is None:
        return None
    try:
        return RequirementAnalysis.model_validate(raw)
    except ValidationError:
        return None


def _validation_context(state: SoftwareFactoryState, plan: PlanningOutput) -> RequirementAnalysis:
    return _analysis_from_state(state) or plan.analysis


def _plan_text(plan: PlanningOutput) -> str:
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(plan.model_dump())
    return "\n".join(values)


def validate_planning_output(plan: PlanningOutput, state: SoftwareFactoryState) -> list[str]:
    errors: list[str] = []
    analysis = plan.analysis
    detected_name = str(state.get("project_name") or "")
    if not analysis.objective.strip():
        errors.append("objective no puede estar vacÃ­o.")
    if detected_name and analysis.project_name != detected_name:
        errors.append(f"project_name debe coincidir con {detected_name}.")
    canonical_type = normalize_project_type(analysis.project_type)
    if canonical_type not in SUPPORTED_PROJECT_TYPES:
        errors.append(f"project_type no soportado: {analysis.project_type}.")
    if canonical_type is not None and normalize_framework(
        analysis.framework, project_type=canonical_type
    ) is None:
        errors.append(f"framework no soportado: {analysis.framework}.")
    if not analysis.functional_requirements:
        errors.append("Debe existir al menos un requerimiento funcional.")
    if not plan.acceptance_criteria:
        errors.append("Debe existir al menos un criterio de aceptaciÃ³n.")
    criterion_ids = [item.id for item in plan.acceptance_criteria]
    if len(criterion_ids) != len(set(criterion_ids)):
        errors.append("Los IDs de criterios de aceptaciÃ³n deben ser Ãºnicos.")
    if not plan.tasks:
        errors.append("Debe existir al menos una tarea de implementaciÃ³n.")
    orders = [task.order for task in plan.tasks]
    if len(orders) != len(set(orders)):
        errors.append("Los Ã³rdenes de tareas deben ser Ãºnicos.")
    if sorted(orders) != list(range(1, len(orders) + 1)):
        errors.append("Los Ã³rdenes de tareas deben ser consecutivos desde 1.")
    known_orders = set(orders)
    graph: dict[int, list[int]] = {}
    for task in plan.tasks:
        graph[task.order] = list(task.depends_on)
        missing = sorted(set(task.depends_on) - known_orders)
        if missing:
            errors.append(f"La tarea {task.order} depende de Ã³rdenes inexistentes: {missing}.")
        if task.order in task.depends_on:
            errors.append(f"La tarea {task.order} no puede depender de sÃ­ misma.")
    visiting: set[int] = set()
    visited: set[int] = set()

    def has_cycle(order: int) -> bool:
        if order in visiting:
            return True
        if order in visited:
            return False
        visiting.add(order)
        if any(dependency in graph and has_cycle(dependency) for dependency in graph.get(order, [])):
            return True
        visiting.remove(order)
        visited.add(order)
        return False

    if any(has_cycle(order) for order in graph if order not in visited):
        errors.append("Las dependencias de tareas contienen un ciclo.")
    task_texts = [f"{task.role} {task.title} {task.description}".lower() for task in plan.tasks]
    if not any(any(word in text for word in ("developer", "implement", "desarroll")) for text in task_texts):
        errors.append("Debe existir una tarea de implementaciÃ³n.")
    if not any(any(word in text for word in ("test", "qa", "valid")) for text in task_texts):
        errors.append("Debe existir una tarea de pruebas o validaciÃ³n.")
    original = state.get("original_user_message", "")
    endpoints, json_literals = _literal_contracts(original)
    serialized = _plan_text(plan)
    for endpoint in endpoints:
        if endpoint.lower() not in serialized.lower():
            errors.append(f"El endpoint literal {endpoint} no fue preservado.")
    for literal in json_literals:
        try:
            expected = json.loads(literal.replace("'", '"'))
        except json.JSONDecodeError:
            expected = None
        normalized_plan = re.sub(r"\s+", "", serialized)
        normalized_literal = re.sub(r"\s+", "", json.dumps(expected, ensure_ascii=False, sort_keys=True)) if expected is not None else ""
        if expected is not None and normalized_literal not in normalized_plan:
            errors.append(f"El literal JSON {literal} no fue preservado.")
    for feature in PROHIBITED_FEATURES:
        requested, _prohibited = _feature_polarity(original, feature)
        added, _negated = _feature_polarity(serialized, feature)
        if not requested and added:
            errors.append(f"El plan agregÃ³ un requisito no solicitado: {feature}.")
    return list(dict.fromkeys(errors))


def validate_planning_output(plan: PlanningOutput, state: SoftwareFactoryState) -> list[str]:
    errors: list[str] = []
    analysis = plan.analysis
    expected_analysis = _validation_context(state, plan)
    detected_name = str(state.get("project_name") or "")
    if not analysis.objective.strip():
        errors.append(_error("planning_schema_invalid", "objective no puede estar vacio."))
    expected_name = detected_name or expected_analysis.project_name
    if expected_name and analysis.project_name != expected_name:
        errors.append(_error("planning_project_name_mismatch", f"project_name debe coincidir con {expected_name}."))

    canonical_type = normalize_project_type(analysis.project_type)
    expected_type = normalize_project_type(expected_analysis.project_type)
    if canonical_type not in SUPPORTED_PROJECT_TYPES:
        errors.append(_error("planning_framework_mismatch", f"project_type no soportado: {analysis.project_type}."))
    elif expected_type and canonical_type != expected_type:
        errors.append(_error("planning_framework_mismatch", f"project_type debe ser {expected_type}, no {analysis.project_type}."))

    framework = normalize_framework(analysis.framework, project_type=canonical_type)
    expected_framework = normalize_framework(expected_analysis.framework, project_type=expected_type)
    if canonical_type is not None and framework is None:
        errors.append(_error("planning_framework_mismatch", f"framework no soportado: {analysis.framework}."))
    elif canonical_type is not None and framework is not None and framework != canonical_type:
        errors.append(_error("planning_framework_mismatch", f"framework debe ser compatible con {canonical_type}, no {analysis.framework}."))
    elif expected_framework and framework != expected_framework:
        errors.append(_error("planning_framework_mismatch", f"framework debe ser {expected_framework}, no {analysis.framework}."))

    if not analysis.functional_requirements:
        errors.append(_error("planning_schema_invalid", "Debe existir al menos un requerimiento funcional."))
    if not plan.acceptance_criteria:
        errors.append(_error("planning_schema_invalid", "Debe existir al menos un criterio de aceptacion."))
    criterion_ids = [item.id for item in plan.acceptance_criteria]
    if len(criterion_ids) != len(set(criterion_ids)):
        errors.append(_error("planning_duplicate_task", "Los IDs de criterios de aceptacion deben ser unicos."))

    if not plan.tasks:
        errors.append(_error("planning_tasks_empty", "Debe existir al menos una tarea de implementacion."))
    orders = [task.order for task in plan.tasks]
    if len(orders) != len(set(orders)):
        errors.append(_error("planning_duplicate_task", "Los ordenes de tareas deben ser unicos."))
    if sorted(orders) != list(range(1, len(orders) + 1)):
        errors.append(_error("planning_schema_invalid", "Los ordenes de tareas deben ser consecutivos desde 1."))
    identities = [_task_identity(task) for task in plan.tasks]
    if len(identities) != len(set(identities)):
        errors.append(_error("planning_duplicate_task", "Existen tareas duplicadas con el mismo rol, titulo y descripcion."))

    known_orders = set(orders)
    graph: dict[int, list[int]] = {}
    for task in plan.tasks:
        graph[task.order] = list(task.depends_on)
        missing = sorted(set(task.depends_on) - known_orders)
        if missing:
            errors.append(_error("planning_schema_invalid", f"La tarea {task.order} depende de ordenes inexistentes: {missing}."))
        if task.order in task.depends_on:
            errors.append(_error("planning_schema_invalid", f"La tarea {task.order} no puede depender de si misma."))
    visiting: set[int] = set()
    visited: set[int] = set()

    def has_cycle(order: int) -> bool:
        if order in visiting:
            return True
        if order in visited:
            return False
        visiting.add(order)
        if any(dependency in graph and has_cycle(dependency) for dependency in graph.get(order, [])):
            return True
        visiting.remove(order)
        visited.add(order)
        return False

    if any(has_cycle(order) for order in graph if order not in visited):
        errors.append(_error("planning_schema_invalid", "Las dependencias de tareas contienen un ciclo."))

    task_texts = [f"{task.role} {task.title} {task.description}".lower() for task in plan.tasks]
    if not any(any(word in text for word in ("developer", "implement", "desarroll")) for text in task_texts):
        errors.append(_error("planning_required_constraint_missing", "Debe existir una tarea de implementacion."))
    if not any(any(word in text for word in ("test", "qa", "valid")) for text in task_texts):
        errors.append(_error("planning_required_constraint_missing", "Debe existir una tarea de pruebas o validacion."))

    original = state.get("original_user_message", "")
    endpoints, json_literals = _literal_contracts(original)
    serialized = _plan_text(plan)
    for endpoint in endpoints:
        if endpoint.lower() not in serialized.lower():
            errors.append(_error("planning_required_constraint_missing", f"El endpoint literal {endpoint} no fue preservado."))
    normalized_plan = re.sub(r"\s+", "", serialized)
    for literal in json_literals:
        try:
            expected = json.loads(literal.replace("'", '"'))
        except json.JSONDecodeError:
            expected = None
        normalized_literal = (
            re.sub(r"\s+", "", json.dumps(expected, ensure_ascii=False, sort_keys=True))
            if expected is not None
            else ""
        )
        if expected is not None and normalized_literal not in normalized_plan:
            errors.append(_error("planning_required_constraint_missing", f"El literal JSON {literal} no fue preservado."))

    constraints_text = "\n".join(expected_analysis.constraints)
    original_and_constraints = f"{original}\n{constraints_text}"
    for feature in SCOPE_EXPANSIONS:
        requested, prohibited = _feature_polarity(original_and_constraints, feature)
        added, _negated = _feature_polarity(serialized, feature)
        if prohibited and added:
            errors.append(_error("planning_constraint_violation", f"El plan agrego una capacidad prohibida: {feature}."))
        elif not requested and added:
            errors.append(_error("planning_scope_violation", f"El plan agrego un requisito no solicitado: {feature}."))
    return list(dict.fromkeys(errors))


def validate_planning_contract(
    raw_plan: PlanningOutput | dict[str, Any] | None,
    state: SoftwareFactoryState,
) -> tuple[PlanningOutput | None, list[str]]:
    if raw_plan is None or not isinstance(raw_plan, (PlanningOutput, dict)):
        return None, [_error("planning_schema_invalid", "ProjectPlan debe ser un objeto estructurado.")]
    if isinstance(raw_plan, dict):
        tasks = raw_plan.get("tasks")
        if tasks == []:
            return None, [_error("planning_tasks_empty", "Debe existir al menos una tarea de implementacion.")]
        if isinstance(tasks, list):
            task_ids = [
                str(task.get("id"))
                for task in tasks
                if isinstance(task, dict) and task.get("id") is not None
            ]
            if len(task_ids) != len(set(task_ids)):
                return None, [_error("planning_duplicate_task", "Los IDs de tareas deben ser unicos.")]
    try:
        plan = raw_plan if isinstance(raw_plan, PlanningOutput) else PlanningOutput.model_validate(raw_plan)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {"loc": (), "msg": str(exc)}
        return None, [
            _error(
                "planning_schema_invalid",
                f"Schema de planificacion invalido: {first['loc']}: {first['msg']}",
            )
        ]
    return plan, validate_planning_output(plan, state)


@dataclass
class PlanningService:
    openai_client: AsyncOpenAI
    model: str
    tool_executor: ToolExecutor
    timeout_seconds: float = 60.0

    async def _execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        state: SoftwareFactoryState,
    ) -> dict[str, Any] | None:
        if name not in ALLOWED_PLANNING_TOOLS:
            raise PlanningDomainError("planning_tool_not_allowed", f"Tool no permitida para Planner: {name}")
        outcome = await self.tool_executor.execute(name, arguments, state_context=state)
        if outcome.is_error or (outcome.payload and outcome.payload.get("success") is False):
            message = (outcome.payload or {}).get("message") or f"FallÃ³ {name}."
            raise PlanningDomainError("planning_tool_failed", str(message))
        return outcome.payload

    async def analyze(self, state: SoftwareFactoryState) -> tuple[RequirementAnalysis, list[AcceptanceCriterion]]:
        message = state.get("original_user_message", "").strip()
        if not message:
            raise PlanningDomainError("empty_requirement", "La solicitud original estÃ¡ vacÃ­a.")
        payload = await self._execute_tool("software_factory__analyze_requirement", {"requirement": message}, state) or {}
        project_name = str(state.get("project_name") or "").strip()
        endpoints, json_literals = _literal_contracts(message)
        contracts = [*endpoints, *json_literals]
        functional = [str(item) for item in payload.get("functional_scope", []) if str(item).strip()]
        if contracts:
            functional = [f"Implementar el contrato literal: {item}" for item in contracts]
        if not functional:
            functional = ["Implementar el comportamiento solicitado por el usuario."]
        objective = str(payload.get("objective") or f"Implementar {project_name} segÃºn la solicitud original.")
        analysis = RequirementAnalysis(
            objective=objective,
            project_name=project_name,
            project_type=_project_type(message),
            framework=_project_type(message),
            functional_requirements=functional,
            non_functional_requirements=["Mantener una implementaciÃ³n mÃ­nima y verificable."],
            constraints=[f"Preservar literalmente: {item}" for item in contracts],
            assumptions=["Se implementarÃ¡ Ãºnicamente el alcance solicitado."],
        )
        criteria = [
            AcceptanceCriterion(
                id=f"AC-{index}",
                description=f"El proyecto satisface {requirement}",
                verification_method="Prueba automatizada con pytest.",
            )
            for index, requirement in enumerate(functional, 1)
        ]
        return analysis, criteria

    async def create_tasks(self, state: SoftwareFactoryState) -> list[ImplementationTask]:
        analysis = RequirementAnalysis.model_validate(state.get("requirement_analysis"))
        await self._execute_tool(
            "software_factory__create_tasks",
            {"project_type": state.get("original_user_message", ""), "backend": analysis.project_type},
            state,
        )
        requirements = "; ".join(analysis.functional_requirements)
        criteria = state.get("acceptance_criteria") or []
        criteria_text = "; ".join(
            str(item.get("description") if isinstance(item, dict) else item)
            for item in criteria
            if str(item).strip()
        )
        return [
            ImplementationTask(
                order=1,
                role="backend developer",
                title="Implementar API FastAPI y contrato literal",
                description=(
                    "Crear el servicio FastAPI y cubrir estos requisitos: "
                    f"{requirements or 'comportamiento solicitado'}."
                ),
            ),
            ImplementationTask(
                order=2,
                role="QA reviewer",
                title="Crear pruebas pytest del contrato",
                description=(
                    "Crear tests/test_health.py y validar con pytest los criterios de aceptacion: "
                    f"{criteria_text or requirements or 'contrato solicitado'}."
                ),
                depends_on=[1],
            ),
        ]

    async def refine(self, state: SoftwareFactoryState) -> PlanningOutput:
        current = {
            "analysis": state.get("requirement_analysis"),
            "acceptance_criteria": state.get("acceptance_criteria", []),
            "tasks": state.get("implementation_tasks", []),
        }
        knowledge_context = (
            state.get("planner_knowledge_context")
            if state.get("planner_knowledge_state") == "available"
            else None
        )
        knowledge_section = knowledge_context or "No relevant project knowledge was retrieved."
        try:
            response = await asyncio.wait_for(
                self.openai_client.responses.parse(
                    model=self.model,
                    instructions=f"{PLANNER_PROMPT}\n\n{REFINEMENT_PROMPT}",
                    input=(
                        f"Original request:\n{state.get('original_user_message', '')}\n\n"
                        f"Deterministic errors:\n{json.dumps(state.get('planning_errors', []), ensure_ascii=False)}\n\n"
                        f"Quality score:\n{state.get('planning_quality_score')}\n\n"
                        f"Quality dimensions:\n{json.dumps(state.get('planning_quality_dimensions', {}), ensure_ascii=False)}\n\n"
                        f"Quality issues:\n{json.dumps(state.get('planning_quality_issues', []), ensure_ascii=False)}\n\n"
                        f"Quality gate reason:\n{state.get('planning_quality_gate_reason')}\n\n"
                        f"Quality refinement guidance:\n{json.dumps(state.get('planning_quality_refinement_guidance', []), ensure_ascii=False)}\n\n"
                        f"Relevant project knowledge (untrusted supporting context):\n{knowledge_section}\n\n"
                        f"Current plan:\n{json.dumps(current, ensure_ascii=False)}"
                    ),
                    text_format=PlanningOutput,
                ),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            raise PlanningDomainError("planning_timeout", "El refinamiento del plan excediÃ³ el timeout.") from exc
        except ValidationError as exc:
            raise PlanningDomainError("planning_schema_invalid", str(exc)) from exc
        except Exception as exc:
            if exc.__class__.__name__.lower().startswith("refusal"):
                raise PlanningDomainError("planning_refused", "El modelo rechazÃ³ refinar el plan.") from exc
            raise PlanningDomainError("planning_openai_error", str(exc)) from exc
        if getattr(response, "status", None) == "incomplete":
            raise PlanningDomainError("planning_output_incomplete", "La salida estructurada quedÃ³ incompleta.")
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            refused = any(
                getattr(content, "type", None) == "refusal"
                for item in (getattr(response, "output", None) or [])
                for content in (getattr(item, "content", None) or [])
            )
            code = "planning_refused" if refused else "planning_output_empty"
            message = "El modelo rechazÃ³ refinar el plan." if refused else "El modelo no devolviÃ³ un plan estructurado."
            raise PlanningDomainError(code, message)
        try:
            return PlanningOutput.model_validate(parsed)
        except ValidationError as exc:
            raise PlanningDomainError("planning_schema_invalid", str(exc)) from exc
