from __future__ import annotations

import json
import re
from typing import Any

from graph.state import SoftwareFactoryState
from graph.subgraphs.implementation.models import ProjectImplementationPlan
from graph.subgraphs.implementation.test_validation import (
    collect_pytest_test_functions,
    validate_fastapi_test_code,
)
from policies.dependencies import validate_dependency_file


MAX_FILES = 50
MAX_TOTAL_BYTES = 5 * 1024 * 1024
ENDPOINT_PATTERN = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+(/[^\s,.;]+)", re.IGNORECASE)
JSON_PATTERN = re.compile(r"\{\s*[\"'][^{}\r\n]+?\}")


def _literal_json(message: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in JSON_PATTERN.finditer(message)))


def _normalized_json(literal: str) -> str | None:
    try:
        value = json.loads(literal.replace("'", '"'))
    except json.JSONDecodeError:
        return None
    return re.sub(r"\s+", "", json.dumps(value, ensure_ascii=False, sort_keys=True))


def extract_fastapi_test_contract(
    state: SoftwareFactoryState,
) -> tuple[str, dict[str, Any]] | None:
    original = state.get("original_user_message", "")
    endpoints = list(dict.fromkeys(match.group(1) for match in ENDPOINT_PATTERN.finditer(original)))
    if len(endpoints) != 1:
        return None
    for literal in _literal_json(original):
        try:
            parsed = json.loads(literal.replace("'", '"'))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return endpoints[0], parsed
    return None


def validate_project_implementation(
    state: SoftwareFactoryState,
    implementation: ProjectImplementationPlan,
) -> list[str]:
    errors: list[str] = []
    project_name = str(state.get("project_name") or state.get("created_project_name") or "")
    if implementation.project_name != project_name:
        errors.append(f"project_name debe coincidir con {project_name}.")
    analysis = state.get("requirement_analysis") or {}
    expected_framework = str(analysis.get("project_type") or implementation.framework).lower()
    if implementation.framework != expected_framework:
        errors.append(f"framework debe coincidir con {expected_framework}.")
    if implementation.framework == "fastapi" and not implementation.package_name.isidentifier():
        errors.append("package_name debe ser un identificador Python válido.")
    paths = [file.path for file in implementation.files]
    folded = [path.casefold() for path in paths]
    if len(paths) != len(set(folded)):
        errors.append("Los paths generados deben ser únicos.")
    if len(paths) > MAX_FILES:
        errors.append(f"La implementación excede el máximo de {MAX_FILES} archivos.")
    total_bytes = sum(len(file.content.encode("utf-8")) for file in implementation.files)
    if total_bytes > MAX_TOTAL_BYTES:
        errors.append("La implementación excede el tamaño total permitido.")
    if implementation.framework == "fastapi":
        main_path = f"{implementation.package_name}/main.py"
        if main_path not in paths:
            errors.append(f"Falta el archivo principal {main_path}.")
    test_files = [file for file in implementation.files if file.path.startswith("tests/") and file.path.endswith(".py")]
    if not test_files:
        errors.append("Falta al menos un archivo de test.")
    for test_file in test_files:
        file_name = test_file.path.rsplit("/", 1)[-1]
        if not (file_name.startswith("test_") or file_name.endswith("_test.py")):
            errors.append(f"invalid_pytest_test_file_name: {test_file.path}")
    dependency_files = {"requirements.txt", "pyproject.toml", "package.json"}
    if not any(path in dependency_files for path in paths):
        errors.append("Falta un archivo de dependencias.")
    errors.extend(validate_dependency_file(implementation.framework, implementation.files))
    original = state.get("original_user_message", "")
    source_content = "\n".join(
        file.content for file in implementation.files if not file.path.startswith("tests/")
    )
    for match in ENDPOINT_PATTERN.finditer(original):
        endpoint = match.group(1)
        if endpoint not in source_content:
            errors.append(f"El endpoint literal {match.group(0)} no fue preservado.")
    normalized_content = re.sub(r"\s+", "", source_content)
    literals = _literal_json(original)
    for literal in literals:
        normalized = _normalized_json(literal)
        if normalized and normalized not in normalized_content:
            errors.append(f"El literal JSON {literal} no fue preservado.")
    test_content = "\n".join(file.content for file in test_files)
    normalized_tests = re.sub(r"\s+", "", test_content)
    for literal in literals:
        normalized = _normalized_json(literal)
        if normalized and normalized not in normalized_tests:
            errors.append(f"Los tests no validan el literal JSON {literal}.")
    if implementation.framework == "fastapi" and test_files:
        expected_import = f"from {implementation.package_name}.main import"
        if expected_import not in test_content:
            errors.append(f"El test debe importar desde {implementation.package_name}.main.")
        contract = extract_fastapi_test_contract(state)
        expected_endpoint, expected_json = contract or ("", {})
        planning_context = json.dumps(
            {
                "request": original,
                "analysis": state.get("requirement_analysis"),
                "tasks": state.get("implementation_tasks", []),
            },
            ensure_ascii=False,
        ).lower()
        allow_async = bool(
            re.search(r"\b(?:async|asynchronous|as[ií]ncron[oa])\b", planning_context)
        )
        for test_file in test_files:
            errors.extend(
                validate_fastapi_test_code(
                    test_file.content,
                    implementation.package_name,
                    expected_endpoint,
                    expected_json,
                    allow_async=allow_async,
                )
            )
    original_lower = original.lower()
    paths_lower = "\n".join(paths).lower()
    prohibited: dict[str, tuple[str, ...]] = {
        "authentication": ("auth", "oauth", "jwt"),
        "database": ("database", "repository", "migration", "sqlalchemy"),
        "docker": ("dockerfile", "docker-compose"),
        "ci/cd": (".github/workflows", "gitlab-ci"),
    }
    for label, markers in prohibited.items():
        if not any(marker in original_lower for marker in markers) and any(marker in paths_lower for marker in markers):
            errors.append(f"La implementación agregó un componente no solicitado: {label}.")
    return list(dict.fromkeys(errors))


def collectable_tests(implementation: ProjectImplementationPlan) -> list[str]:
    collected: list[str] = []
    for file in implementation.files:
        if not file.path.startswith("tests/") or not file.path.endswith(".py"):
            continue
        file_name = file.path.rsplit("/", 1)[-1]
        if not (file_name.startswith("test_") or file_name.endswith("_test.py")):
            continue
        collected.extend(collect_pytest_test_functions(file.content))
    return collected
