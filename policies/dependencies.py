from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from graph.subgraphs.implementation.models import GeneratedFile


@dataclass(frozen=True)
class DependencyPolicy:
    framework: str
    required_dependencies: Mapping[str, str]
    optional_dependencies: Mapping[str, str]


FASTAPI_DEPENDENCY_POLICY = DependencyPolicy(
    framework="fastapi",
    required_dependencies=MappingProxyType(
        {
            "fastapi": "fastapi[standard]==0.139.0",
            "pytest": "pytest>=8,<9",
        }
    ),
    optional_dependencies=MappingProxyType(
        {
            "httpx": "httpx>=0.23,<1",
            "uvicorn": "uvicorn>=0.17,<1",
        }
    ),
)

DEPENDENCY_POLICIES = {FASTAPI_DEPENDENCY_POLICY.framework: FASTAPI_DEPENDENCY_POLICY}
DEFAULT_FASTAPI_REQUIREMENT = FASTAPI_DEPENDENCY_POLICY.required_dependencies["fastapi"]
DEFAULT_PYTEST_REQUIREMENT = FASTAPI_DEPENDENCY_POLICY.required_dependencies["pytest"]
BLOCKED_FASTAPI_REQUIREMENTS = frozenset({"fastapi[standard]>=0.139.2,<0.140"})
REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?")


def dependency_policy_for(framework: str) -> DependencyPolicy | None:
    return DEPENDENCY_POLICIES.get(framework.strip().lower())


def requirement_lines(content: str) -> list[str]:
    lines: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def extract_dependency_name(dependency_line: str) -> str:
    normalized = dependency_line.split("#", 1)[0].strip()
    match = REQUIREMENT_NAME.match(normalized)
    return match.group(1).lower().replace("_", "-") if match else ""


def _file_value(file: Any, field: str) -> Any:
    return file.get(field) if isinstance(file, dict) else getattr(file, field, None)


def _requirements_file(files: list[Any]) -> Any | None:
    return next((file for file in files if str(_file_value(file, "path")).casefold() == "requirements.txt"), None)


def dependency_normalization_errors(framework: str, files: list[Any]) -> list[str]:
    policy = dependency_policy_for(framework)
    if policy is None:
        return []
    requirements = _requirements_file(files)
    if requirements is None:
        return []
    controlled_names = {*policy.required_dependencies, *policy.optional_dependencies}
    errors: list[str] = []
    for line in requirement_lines(str(_file_value(requirements, "content") or "")):
        name = extract_dependency_name(line)
        if name not in controlled_names:
            errors.append(f"unsupported_dependency: {line}")
    return list(dict.fromkeys(errors))


def normalize_dependency_file(
    framework: str,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    policy = dependency_policy_for(framework)
    if policy is None:
        return list(files)
    requirements = _requirements_file(files)
    if requirements is None:
        from graph.subgraphs.implementation.models import GeneratedFile

        content = "\n".join(policy.required_dependencies.values()) + "\n"
        return [*files, GeneratedFile(path="requirements.txt", content=content)]
    optional_names_present: set[str] = set()
    for line in requirement_lines(str(_file_value(requirements, "content") or "")):
        name = extract_dependency_name(line)
        if name in policy.optional_dependencies:
            optional_names_present.add(name)
    required = tuple(policy.required_dependencies.values())
    optional = tuple(
        canonical
        for name, canonical in policy.optional_dependencies.items()
        if name in optional_names_present
    )
    content = "\n".join((*required, *optional)) + "\n"
    return [
        file.model_copy(update={"content": content}) if file is requirements else file
        for file in files
    ]


def validate_dependency_file(framework: str, files: list[Any]) -> list[str]:
    policy = dependency_policy_for(framework)
    if policy is None:
        return []
    requirements = _requirements_file(files)
    if requirements is None:
        return ["missing_required_dependency: requirements.txt"]
    lines = requirement_lines(str(_file_value(requirements, "content") or ""))
    folded = [line.casefold() for line in lines]
    errors: list[str] = []
    for name, required in policy.required_dependencies.items():
        if required.casefold() not in folded:
            variants = [line for line in lines if extract_dependency_name(line) == name]
            code = "incompatible_dependency_version" if variants else "missing_required_dependency"
            errors.append(f"{code}: {required}")
    controlled = {**policy.required_dependencies, **policy.optional_dependencies}
    allowed = {line.casefold() for line in controlled.values()}
    for line in lines:
        if line.casefold() not in allowed:
            name = extract_dependency_name(line)
            code = "incompatible_dependency_version" if name in controlled else "unsupported_dependency"
            errors.append(f"{code}: {line}")
    seen: set[str] = set()
    for line in folded:
        if line in seen:
            errors.append(f"duplicate_dependency: {line}")
        seen.add(line)
    return list(dict.fromkeys(errors))
