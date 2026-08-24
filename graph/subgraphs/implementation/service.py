from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI
from pydantic import ValidationError

from graph.state import SoftwareFactoryState
from graph.subgraphs.implementation.models import GeneratedFile, ProjectImplementationPlan
from graph.subgraphs.implementation.prompts import (
    DEVELOPER_PROMPT,
    REFINEMENT_PROMPT,
    build_refinement_guidance,
)
from graph.subgraphs.implementation.test_validation import build_fastapi_health_test
from graph.subgraphs.implementation.validators import extract_fastapi_test_contract


ArgumentResolver = Callable[[str, SoftwareFactoryState], Awaitable[dict[str, Any]]]


class ImplementationDomainError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass
class ImplementationService:
    openai_client: AsyncOpenAI
    model: str
    argument_resolver: ArgumentResolver | None = None
    timeout_seconds: float = 60.0

    def _apply_deterministic_health_test(
        self,
        state: SoftwareFactoryState,
        proposal: ProjectImplementationPlan,
    ) -> ProjectImplementationPlan:
        contract = extract_fastapi_test_contract(state)
        if proposal.framework != "fastapi" or contract is None or contract[0] != "/health":
            return proposal
        endpoint, expected_json = contract
        deterministic_test = GeneratedFile(
            path="tests/test_health.py",
            content=build_fastapi_health_test(
                proposal.package_name,
                endpoint,
                200,
                expected_json,
            ),
        )
        files: list[GeneratedFile] = []
        test_inserted = False
        for file in proposal.files:
            if file.path.startswith("tests/") and file.path.endswith(".py"):
                if not test_inserted:
                    files.append(deterministic_test)
                    test_inserted = True
                continue
            files.append(file)
        if not test_inserted:
            files.append(deterministic_test)
        return proposal.model_copy(update={"files": files})

    def _context(self, state: SoftwareFactoryState) -> dict[str, Any]:
        context: dict[str, Any] = {
            "original_request": state.get("original_user_message", ""),
            "requirement_analysis": state.get("requirement_analysis"),
            "acceptance_criteria": state.get("acceptance_criteria", []),
            "implementation_tasks": state.get("implementation_tasks", []),
        }
        retrieved = state.get("developer_knowledge_context")
        if state.get("developer_knowledge_state") == "available" and isinstance(retrieved, str):
            context["relevant_project_knowledge"] = retrieved
            context["relevant_project_knowledge_sources"] = state.get(
                "developer_knowledge_sources", []
            )
        return context

    async def _structured(self, state: SoftwareFactoryState, *, refinement: bool) -> ProjectImplementationPlan:
        if self.argument_resolver is not None:
            arguments = await self.argument_resolver("filesystem__create_project_structure", state)
            analysis = state.get("requirement_analysis") or {}
            package_name = str(analysis.get("project_name") or arguments.get("project_name") or "").replace("-", "_")
            candidate = {
                "project_name": arguments.get("project_name"),
                "framework": analysis.get("project_type") or "fastapi",
                "package_name": package_name,
                "files": arguments.get("files", []),
            }
            try:
                proposal = ProjectImplementationPlan.model_validate(candidate)
                return self._apply_deterministic_health_test(state, proposal)
            except ValidationError as exc:
                raise ImplementationDomainError("implementation_schema_invalid", str(exc)) from exc
        context = self._context(state)
        if refinement:
            context["current_proposal"] = state.get("project_implementation")
            context["deterministic_errors"] = state.get("implementation_errors", [])
            context["refinement_guidance"] = build_refinement_guidance(
                state.get("implementation_errors", [])
            )
        try:
            response = await asyncio.wait_for(
                self.openai_client.responses.parse(
                    model=self.model,
                    instructions=DEVELOPER_PROMPT + ("\n\n" + REFINEMENT_PROMPT if refinement else ""),
                    input=json.dumps(context, ensure_ascii=False),
                    text_format=ProjectImplementationPlan,
                ),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            raise ImplementationDomainError("implementation_timeout", "La generación excedió el timeout.") from exc
        except ValidationError as exc:
            raise ImplementationDomainError("implementation_schema_invalid", str(exc)) from exc
        except Exception as exc:
            if exc.__class__.__name__.lower().startswith("refusal"):
                raise ImplementationDomainError("implementation_refused", "El modelo rechazó generar la implementación.") from exc
            raise ImplementationDomainError("implementation_openai_error", str(exc)) from exc
        if getattr(response, "status", None) == "incomplete":
            raise ImplementationDomainError("implementation_output_incomplete", "La salida estructurada quedó incompleta.")
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            refused = any(
                getattr(content, "type", None) == "refusal"
                for item in (getattr(response, "output", None) or [])
                for content in (getattr(item, "content", None) or [])
            )
            code = "implementation_refused" if refused else "implementation_output_empty"
            raise ImplementationDomainError(code, "El modelo no devolvió una implementación estructurada.")
        try:
            proposal = ProjectImplementationPlan.model_validate(parsed)
            return self._apply_deterministic_health_test(state, proposal)
        except ValidationError as exc:
            raise ImplementationDomainError("implementation_schema_invalid", str(exc)) from exc

    async def generate_project(self, state: SoftwareFactoryState) -> ProjectImplementationPlan:
        return await self._structured(state, refinement=False)

    async def refine_project(self, state: SoftwareFactoryState) -> ProjectImplementationPlan:
        return await self._structured(state, refinement=True)
