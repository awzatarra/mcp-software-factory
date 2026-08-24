from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Mapping

from dotenv import load_dotenv
from jsonschema import Draft202012Validator
from openai import AsyncOpenAI

from policies.dependencies import (
    BLOCKED_FASTAPI_REQUIREMENTS,
    DEFAULT_FASTAPI_REQUIREMENT,
    DEFAULT_PYTEST_REQUIREMENT,
)

from clients.mcp_client import MCPServerClient, MCPToolTimeout
from clients.mcp_manager import MCPClientManager
from clients.tool_registry import RegisteredTool
from clients.tool_registry import ToolRegistry
from graph.persistence_service import (
    AmbiguousForkNodeError,
    CheckpointNotFoundError,
    CheckpointThreadMismatchError,
    InvalidForkUpdateError,
    UnsafeReplayError,
    WorkflowAlreadyCompletedError,
    WorkflowNotFoundError,
    WorkflowNotInterruptedError,
)


ROOT = Path(__file__).resolve().parent
MAX_OPENAI_ROUNDS = 15
MAX_TEST_ATTEMPTS = 3
SENSITIVE_TOOLS = {
    ("filesystem", "create_directory"),
    ("filesystem", "write_file"),
    ("filesystem", "create_project_structure"),
    ("filesystem", "update_project_files"),
    ("testing", "prepare_test_environment"),
    ("testing", "run_tests"),
    ("git", "commit"),
            ("git", "approve_commit"),
            ("git", "merge_workflow_branch"),
            ("git", "prepare_promotion"),
            ("git", "approve_promotion"),
}


PROJECT_NAME_PATTERNS = [
    re.compile(r"\b(?:llamado|llamada)\s+([A-Za-z0-9_-]{1,64})\b", re.IGNORECASE),
    re.compile(r"(?<!sin )\bnombre\s+([A-Za-z0-9_-]{1,64})\b", re.IGNORECASE),
    re.compile(r"\bproyecto\s+([A-Za-z0-9_-]{1,64})\b", re.IGNORECASE),
]
PROJECT_FRAMEWORK_WORDS = {"fastapi", "dotnet", "node", "python"}
PROJECT_EXISTS_MARKERS = ("already exists", "ya existe", "fileexistserror")
CREATE_INTENT_RE = re.compile(r"\b(crea|crear|genera|generar|construye|construir)\b", re.IGNORECASE)
REVIEW_INTENT_RE = re.compile(r"\b(revisa|revisar|valida|validar|corrige|corregir)\b|ejecuta sus pruebas", re.IGNORECASE)
PYTEST_FILE_RE = re.compile(r"(?P<path>(?:[A-Za-z0-9_.-]+[\\/])+test_[A-Za-z0-9_.-]+\.py):(?P<line>\d+)")
MAX_REPAIR_ATTEMPTS = 2


class FailureType(StrEnum):
    NONE = "none"
    INVALID_TOOL_ARGUMENTS = "invalid_tool_arguments"
    DEPENDENCY_POLICY_VIOLATION = "dependency_policy_violation"
    ENVIRONMENT_CREATION_FAILED = "environment_creation_failed"
    DEPENDENCY_INSTALLATION_FAILED = "dependency_installation_failed"
    TEST_TIMEOUT = "test_timeout"
    TEST_FAILURE = "test_failure"
    NO_TESTS_COLLECTED = "no_tests_collected"
    USER_REJECTED = "user_rejected"


@dataclass
class ExecutionState:
    original_user_message: str
    requested_project_name: str | None = None
    workflow_intent: Literal["create_project", "review_existing_project"] = "create_project"
    analysis_completed: bool = False
    tasks_created: bool = False
    workspace_inspected: bool = False
    project_created: bool = False
    project_exists: bool = False
    created_project_name: str | None = None
    tests_executed: bool = False
    tests_passed: bool = False
    test_infrastructure_failed: bool = False
    retry_limit_reached: bool = False
    user_cancelled: bool = False
    terminal_status: str | None = None
    failure_type: str | None = None
    failure_stage: str | None = None
    failure_message: str | None = None
    detected_test_framework: str | None = None
    expected_test_command: list[str] | None = None
    actual_test_command: list[str] | None = None
    environment_prepared: bool = False
    dependencies_installed: bool = False
    environment_python: str | None = None
    installed_fastapi_version: str | None = None
    installed_starlette_version: str | None = None
    test_warning_count: int | None = None
    test_stdout: str | None = None
    test_stderr: str | None = None
    test_failure_summary: str | None = None
    failing_test_files: list[str] = field(default_factory=list)
    failing_test_content: str | None = None
    related_source_content: str | None = None
    related_source_file: str | None = None
    related_source_candidates: list[str] = field(default_factory=list)
    related_source_read_attempts: int = 0
    repair_phase: Literal["not_started", "read_failing_test", "read_related_source", "apply_fix", "rerun_tests", "completed"] = "not_started"
    files_read_during_repair: set[str] = field(default_factory=set)
    files_updated_during_repair: set[str] = field(default_factory=set)
    repair_attempts: int = 0
    repair_decision: str | None = None
    repair_before: str | None = None
    repair_after: str | None = None
    first_test_result_summary: str | None = None
    final_test_result_summary: str | None = None
    sensitive_tools_executed: list[str] = field(default_factory=list)
    rejected_tools: set[str] = field(default_factory=set)
    last_tool_executed: str | None = None


def is_terminal_failure(state: ExecutionState) -> bool:
    return state.test_infrastructure_failed or state.retry_limit_reached or state.user_cancelled or state.terminal_status == "project_not_found"


def is_terminal_state(state: ExecutionState) -> bool:
    return is_terminal_failure(state) or state.tests_passed


def terminal_status_for(state: ExecutionState) -> str | None:
    if state.tests_passed:
        return "completed"
    if state.test_infrastructure_failed:
        return "infrastructure_failed"
    if state.user_cancelled:
        return "rejected_by_user"
    if state.retry_limit_reached:
        return "retry_limit_reached"
    return state.terminal_status


def requirement_lines(content: str) -> list[str]:
    lines: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def validate_fastapi_dependency_policy(requirements_content: str) -> dict[str, Any] | None:
    lines = requirement_lines(requirements_content)
    lower_lines = {line.lower() for line in lines}
    fastapi_lines = [line for line in lines if line.lower().startswith("fastapi")]
    if not fastapi_lines:
        return None
    expected_lower = DEFAULT_FASTAPI_REQUIREMENT.lower()
    received = fastapi_lines[0]
    if expected_lower in lower_lines and DEFAULT_PYTEST_REQUIREMENT.lower() in lower_lines:
        return None
    if received.lower() in BLOCKED_FASTAPI_REQUIREMENTS or received != DEFAULT_FASTAPI_REQUIREMENT:
        return {
            "success": False,
            "status": "dependency_policy_violation",
            "expected": DEFAULT_FASTAPI_REQUIREMENT,
            "received": received,
            "message": "Usa las dependencias configuradas por el Host.",
        }
    if DEFAULT_PYTEST_REQUIREMENT.lower() not in lower_lines:
        return {
            "success": False,
            "status": "dependency_policy_violation",
            "expected": DEFAULT_PYTEST_REQUIREMENT,
            "received": "",
            "message": "Usa las dependencias configuradas por el Host.",
        }
    return None


def dependency_policy_for_project_files(files: Any) -> dict[str, Any] | None:
    if not isinstance(files, list):
        return None
    for file in files:
        if not isinstance(file, dict):
            continue
        if Path(str(file.get("path", ""))).name.lower() != "requirements.txt":
            continue
        return validate_fastapi_dependency_policy(str(file.get("content", "")))
    return None


def build_terminal_summary(state: ExecutionState) -> dict[str, Any]:
    return {
        "project_created": state.project_created,
        "project_exists": state.project_exists,
        "project_name": state.created_project_name,
        "workflow_intent": state.workflow_intent,
        "environment_prepared": state.environment_prepared,
        "dependencies_installed": state.dependencies_installed,
        "environment_python": state.environment_python,
        "installed_fastapi_version": state.installed_fastapi_version,
        "installed_starlette_version": state.installed_starlette_version,
        "detected_test_framework": state.detected_test_framework,
        "expected_test_command": state.expected_test_command,
        "actual_test_command": state.actual_test_command,
        "tests_executed": state.tests_executed,
        "tests_passed": state.tests_passed,
        "test_warning_count": state.test_warning_count,
        "test_failure_summary": state.test_failure_summary,
        "failing_test_files": state.failing_test_files,
        "original_requirement": state.original_user_message,
        "repair_phase": state.repair_phase,
        "repair_decision": state.repair_decision,
        "repair_before": state.repair_before,
        "repair_after": state.repair_after,
        "files_read_during_repair": sorted(state.files_read_during_repair),
        "files_updated_during_repair": sorted(state.files_updated_during_repair),
        "repair_attempts": state.repair_attempts,
        "first_test_result_summary": state.first_test_result_summary,
        "final_test_result_summary": state.final_test_result_summary,
        "failure_type": state.failure_type,
        "failure_stage": state.failure_stage,
        "failure_message": state.failure_message,
    }


def determine_next_action(state: ExecutionState) -> str:
    if is_terminal_state(state):
        return "finalize"
    if state.workflow_intent == "review_existing_project":
        if not state.workspace_inspected:
            return "filesystem__list_files"
        if not state.project_exists and not state.project_created:
            return "finalize"
        if state.detected_test_framework is None:
            return "testing__detect_test_framework"
        if not state.environment_prepared:
            return "testing__prepare_test_environment"
        if state.tests_passed:
            return "finalize"
        if not state.tests_executed or state.repair_phase == "rerun_tests":
            return "testing__run_tests"
        if state.tests_executed and not state.tests_passed and not state.test_infrastructure_failed:
            return "fix_failed_tests"
        return "finalize"
    if not state.analysis_completed:
        return "software_factory__analyze_requirement"
    if not state.tasks_created:
        return "software_factory__create_tasks"
    if not state.workspace_inspected:
        return "filesystem__list_files"
    if not state.project_created and not state.project_exists:
        return "filesystem__create_project_structure"
    if state.detected_test_framework is None:
        return "testing__detect_test_framework"
    if not state.environment_prepared:
        return "testing__prepare_test_environment"
    if state.tests_passed:
        return "finalize"
    if not state.tests_executed:
        return "testing__run_tests"
    if state.tests_executed and not state.tests_passed and not state.test_infrastructure_failed:
        return "fix_failed_tests"
    return "finalize"


def get_allowed_tools(state: ExecutionState, registry: ToolRegistry) -> list[RegisteredTool]:
    next_action = determine_next_action(state)
    if next_action == "finalize":
        return []
    allowed_names = {repair_phase_allowed_tool_name(state)} if next_action == "fix_failed_tests" else {next_action}
    allowed_names.discard("")
    tools = []
    for public_name in allowed_names:
        try:
            tools.append(registry.get(public_name))
        except KeyError:
            continue
    return tools


def normalize_tool_arguments(tool: RegisteredTool, arguments: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(arguments)
    if tool.server_name == "filesystem" and tool.original_name == "list_files":
        relative_path = normalized.get("relative_path")
        if not isinstance(relative_path, str) or not relative_path.strip():
            normalized["relative_path"] = "."
    return normalized


def validate_tool_arguments(tool: RegisteredTool, arguments: dict[str, Any]) -> list[str]:
    schema = tool.input_schema or {}
    if not schema:
        return []
    validator = Draft202012Validator(schema)
    errors = [error.message for error in sorted(validator.iter_errors(arguments), key=lambda item: list(item.path))]
    properties = schema.get("properties")
    if isinstance(properties, dict) and properties:
        allowed = set(properties)
        for key in sorted(set(arguments) - allowed):
            errors.append(f"Additional property '{key}' is not allowed")
    return errors


def invalid_tool_arguments_payload(tool: RegisteredTool, errors: list[str]) -> dict[str, Any]:
    return {
        "success": False,
        "status": FailureType.INVALID_TOOL_ARGUMENTS.value,
        "failure_type": FailureType.INVALID_TOOL_ARGUMENTS.value,
        "tool": tool.public_name,
        "errors": errors,
        "message": "Corrige los argumentos usando el schema publicado por la tool.",
    }


@dataclass(frozen=True)
class ToolExecutionResult:
    output_for_model: str
    payload: dict[str, Any] | None
    is_error: bool
    content_text: list[str] = field(default_factory=list)


def extract_requested_project_name(message: str) -> str | None:
    for index, pattern in enumerate(PROJECT_NAME_PATTERNS):
        match = pattern.search(message)
        if not match:
            continue
        name = match.group(1)
        if index == 2 and name.lower() in PROJECT_FRAMEWORK_WORDS:
            continue
        return name
    return None


def extract_workflow_intent(message: str, project_name: str | None) -> Literal["create_project", "review_existing_project"]:
    if CREATE_INTENT_RE.search(message):
        return "create_project"
    if project_name and REVIEW_INTENT_RE.search(message):
        return "review_existing_project"
    return "create_project"


def normalize_pytest_path(path: str) -> str:
    return Path(path.replace("\\", "/")).as_posix()


def normalize_project_relative_path(project_name: str, path: str) -> str:
    clean_project = normalize_pytest_path(project_name).strip("/")
    clean_path = normalize_pytest_path(path).strip()
    candidate = Path(clean_path)
    if not clean_project:
        raise ValueError("project_name must not be empty")
    if not clean_path:
        raise ValueError("path must not be empty")
    if candidate.is_absolute() or re.match(r"^[A-Za-z]:/", clean_path):
        raise ValueError("absolute paths are not allowed")
    parts = Path(clean_path).parts
    if ".." in parts:
        raise ValueError("path traversal is not allowed")
    normalized = clean_path.strip("/")
    if normalized == clean_project or normalized.startswith(f"{clean_project}/"):
        return normalized
    return f"{clean_project}/{normalized}"


def extract_failing_test_files(stdout: str, stderr: str = "") -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for match in PYTEST_FILE_RE.finditer(f"{stdout}\n{stderr}"):
        path = normalize_pytest_path(match.group("path"))
        if path not in seen:
            seen.add(path)
            files.append(path)
    return files


def extract_failure_summary(stdout: str, stderr: str = "") -> str:
    combined = f"{stdout}\n{stderr}"
    file_match = PYTEST_FILE_RE.search(combined)
    location = ""
    if file_match:
        location = f"{normalize_pytest_path(file_match.group('path'))}:{file_match.group('line')} "
    if '"status": "healthy"' in combined and '"status": "ok"' in combined:
        return f'{location}esperaba {{"status": "healthy"}}, pero el endpoint devolvió {{"status": "ok"}}.'.strip()
    if "'status': 'healthy'" in combined and "'status': 'ok'" in combined:
        return f'{location}esperaba {{"status": "healthy"}}, pero el endpoint devolvió {{"status": "ok"}}.'.strip()
    for line in combined.splitlines():
        stripped = line.strip()
        if stripped.startswith("E ") or "AssertionError" in stripped:
            return stripped
    return "Las pruebas fallaron; revisa stdout/stderr para el detalle."


def summarize_pytest_result(stdout: str, success: bool) -> str:
    matches = re.findall(r"(\d+\s+(?:passed|failed|errors?|warnings?)(?:,\s*\d+\s+(?:passed|failed|errors?|warnings?))*)", stdout)
    if matches:
        return matches[-1]
    return "passed" if success else "failed"


def expected_project_relative_path(state: ExecutionState, relative_path: str) -> str:
    project_name = state.created_project_name or state.requested_project_name or ""
    return normalize_project_relative_path(project_name, relative_path)


def infer_related_source_file(state: ExecutionState) -> str | None:
    from graph.nodes import extract_related_source_path_from_test, project_name_to_python_package

    if state.related_source_file:
        return state.related_source_file
    imported = extract_related_source_path_from_test(state.failing_test_content or "")
    if imported:
        return imported
    project_name = state.created_project_name or state.requested_project_name or ""
    try:
        return f"{project_name_to_python_package(project_name)}/main.py"
    except ValueError:
        return None


def extract_repair_evidence_from_update(files: Any) -> tuple[str | None, str | None, str | None]:
    if not isinstance(files, list):
        return None, None, None
    for file in files:
        if not isinstance(file, dict):
            continue
        content = str(file.get("content", ""))
        if '"status": "ok"' in content:
            return (
                '"status": "healthy"',
                '"status": "ok"',
                "El endpoint ya cumplía el requerimiento original. Se corrigió el test.",
            )
    return None, None, None


def repair_phase_allowed_tool_name(state: ExecutionState) -> str:
    if state.repair_phase in {"not_started", "read_failing_test", "read_related_source"}:
        return "filesystem__read_file"
    if state.repair_phase == "apply_fix":
        return "filesystem__update_project_files"
    if state.repair_phase == "rerun_tests":
        return "testing__run_tests"
    return ""


def build_base_instructions() -> str:
    return f"""You are the OpenAI planner and implementer inside an educational MCP Software Factory demo.

Use the dynamically discovered MCP tools. Tool namespaces:
- software_factory__ -> analysis and planning.
- filesystem__ -> files.
- testing__ -> tests.

Process:
1. Analyze the requirement.
2. Create tasks.
3. Inspect the workspace before creating or changing files.
4. Create a minimal executable version.
5. Include automated tests.
6. Use filesystem__create_project_structure exactly once for a new project.
7. Detect the test framework.
8. Run tests.
9. If tests fail, analyze stdout and stderr.
10. Read only the files needed for fixes.
11. Correct existing files with filesystem__update_project_files.
12. Retry tests when appropriate.
13. Do not invent tool results.
14. Do not claim tests passed unless testing__run_tests actually passed.
15. Respect user rejections and do not immediately repeat a rejected operation.
16. In the final answer, report created files, tests, fixes, and final execution status.

Clarification and approval rules:
- Do not ask again for information that is already present in the user request.
- If the request includes framework, project name, functionality, and tests, start the MCP workflow immediately.
- Do not ask about Docker, ports, exact dependency versions, environment variables, database, or authentication unless they are truly required.
- Use minimal reasonable defaults for minor unspecified details.
- Do not ask the user to type yes, y, s, confirm, approve, or equivalents in a normal model response.
- Human approvals are the exclusive responsibility of the Host code.
- Do not simulate approval forms or confirmation dialogs.
- Use tools when the request has enough information to proceed.
- Ask clarifying questions only when an essential requirement is missing and cannot be reasonably inferred.
- Complete every possible step in the same execution instead of stopping at a proposal.
- Do not answer with a proposal of what you could do; execute the workflow with tools.
- Respect literal project names, endpoints, and response bodies provided by the user.
- Never replace an explicit project name with demo_service, fastapi_minimal, or another generic name.
- Do not add endpoints, CRUD, authentication, layers, or functionality that the user did not ask for.
- For a minimal request, generate a minimal solution.
- Do not add /greet, /items, Hello World, or generic examples unless the user asks for them.
- filesystem__create_project_structure must be used at most once.
- If project creation succeeds, continue directly with test detection and test execution.
- Do not recreate a project to fix it; use filesystem__update_project_files.
- After tests pass, provide the final answer and do not request more tools.
- If there is a conflict between the user request and generated arguments, correct the arguments.
- Explicit user data has priority over any default.
- In filesystem__create_project_structure, every files[].path must be relative to the project root.
- Never include project_name as the first segment of files[].path.
- Derive the Python package directory conservatively from the actual project name by replacing hyphens with underscores; never reuse a package name from another project.
- After successful project creation, do not analyze or plan again.
- Continue directly with test framework detection and test execution.
- If a tool returns an error, read the exact error and only correct the tool arguments.
- Do not repeat a step that the Host reports as completed.
- Do not add requests unless the code really imports and uses it.
- requirements.txt documents project dependencies but does not install them automatically; the Testing MCP Server prepares and uses the project's isolated .venv.
- For new projects, use this tool order: analyze_requirement, create_tasks, list_files, create_project_structure, detect_test_framework, prepare_test_environment, run_tests, then final answer or corrections.
- After creating a project, prepare its environment before running tests.
- If testing__run_tests returns environment_not_prepared, call only testing__prepare_test_environment next.
- Immutable Host dependency policy for minimal FastAPI demo projects:
  - DEFAULT_FASTAPI_REQUIREMENT: {DEFAULT_FASTAPI_REQUIREMENT}
  - DEFAULT_PYTEST_REQUIREMENT: {DEFAULT_PYTEST_REQUIREMENT}
- Build requirements.txt from those exact Host-provided values.
- Never invent package versions. Use only package versions provided by the Host or explicitly provided by the user.
- For minimal FastAPI demo projects, create requirements.txt with exactly:
  {DEFAULT_FASTAPI_REQUIREMENT}
  {DEFAULT_PYTEST_REQUIREMENT}
- Do not add starlette directly; let FastAPI resolve a compatible Starlette.
- Do not add uvicorn, httpx, or requests separately when fastapi[standard] covers the dependencies needed for this demo.
- Keep dependency versions bounded; do not leave dependencies completely unpinned.
- Explicit user dependency requirements still take priority over these defaults.
- Generated Python projects must include a .gitignore with .venv/.
- If timed_out=true, do not assume the application code is wrong.
- Do not modify files because of a timeout.
- Do not repeat listings or file reads after an infrastructure failure.
- If stdout and stderr are empty and tests timed out, end the workflow.
- Only use filesystem__update_project_files when stdout or stderr contains a concrete functional error.
- After successful tests, answer immediately.
- After an infrastructure failure, answer immediately and report that the project was created but could not be validated.
- For review/repair requests, do not create placeholder projects.
- During repair, the original user requirement has priority over a broken test.
- If the original requirement says GET /health returns {{"status": "ok"}} and a test expects "healthy", fix the test expectation to "ok"; do not change the endpoint to "healthy".
- Do not keep listing files after the Host provides a failing test path. Read the failing test, read the related source, apply one focused fix, and rerun tests.

For minimal FastAPI projects, unless the user says otherwise, assume:
- Modern Python compatible with the project.
- pytest.
- FastAPI TestClient.
- A minimal package/file structure.
- No Docker.
- No database.
- No authentication.

Never request arbitrary command execution; only use the available testing tool.
LangGraph is intentionally not part of this demo yet.
"""


BASE_INSTRUCTIONS = build_base_instructions()


def build_request_instructions(base_instructions: str, state: ExecutionState) -> str:
    instructions = base_instructions
    if state.original_user_message:
        instructions += "\n\nOriginal user message as source of truth:\n" + state.original_user_message
    if state.requested_project_name:
        instructions += f"\n\nExplicit requested project name: {state.requested_project_name}"
    if state.workflow_intent == "review_existing_project":
        instructions += "\n\nWorkflow intent: review_existing_project. Do not create a placeholder project."
    if state.repair_phase != "not_started":
        instructions += (
            "\n\nRepair context:\n"
            f"- Requerimiento original: {state.original_user_message}\n"
            "- El requerimiento original tiene prioridad sobre un test roto.\n"
            "- Si el código cumple el requerimiento y el test lo contradice, corrige el test.\n"
            f"- failure_summary: {state.test_failure_summary}\n"
            f"- failing_test_files: {state.failing_test_files}\n"
            f"- repair_phase: {state.repair_phase}"
        )
    return instructions


def build_clients(observability: Any | None = None) -> list[MCPServerClient]:
    return [
        MCPServerClient("software_factory", str(ROOT / "servers" / "software_factory_server.py"), observability=observability),
        MCPServerClient("filesystem", str(ROOT / "servers" / "filesystem_server.py"), observability=observability),
        MCPServerClient("testing", str(ROOT / "servers" / "testing_server.py"), observability=observability),
        MCPServerClient("knowledge", str(ROOT / "servers" / "knowledge_server.py"), observability=observability),
        MCPServerClient("git", str(ROOT / "servers" / "git_server.py"), observability=observability),
    ]


def dump_response_item(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
    if isinstance(item, dict):
        return item
    raise TypeError(f"unsupported response item: {type(item)!r}")


def extract_text_from_mcp_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if hasattr(content, "text"):
        return str(content.text)
    if hasattr(content, "model_dump"):
        data = content.model_dump(exclude_none=True)
        if "text" in data:
            return str(data["text"])
        return json.dumps(data, ensure_ascii=False)
    return str(content)


def try_parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def unwrap_business_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def extract_mcp_payload(result: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if isinstance(result, dict):
        return unwrap_business_payload(result), []
    structured_content = getattr(result, "structuredContent", None)
    if structured_content is None:
        structured_content = getattr(result, "structured_content", None)
    if isinstance(structured_content, dict):
        return unwrap_business_payload(structured_content), []

    text_parts: list[str] = []
    if hasattr(result, "content"):
        text_parts = [extract_text_from_mcp_content(item) for item in result.content]
        for text in text_parts:
            parsed = try_parse_json_object(text)
            if parsed is not None:
                return unwrap_business_payload(parsed), text_parts
        return None, text_parts

    if hasattr(result, "model_dump"):
        data = result.model_dump(exclude_none=True)
        if isinstance(data, dict):
            return unwrap_business_payload(data), []
    return None, [str(result)]


def normalize_mcp_result(result: Any) -> ToolExecutionResult:
    payload, text_parts = extract_mcp_payload(result)
    is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    business_success = payload.get("success") if payload is not None else None
    output = {
        "transport_success": not is_error,
        "business_success": business_success,
        "success": not is_error,
        "data": payload if payload is not None else {},
    }
    if text_parts and payload is None:
        output["content"] = text_parts
    return ToolExecutionResult(
        output_for_model=json.dumps(output, ensure_ascii=False),
        payload=payload,
        is_error=is_error,
        content_text=text_parts,
    )


def local_execution_result(payload: dict[str, Any], is_error: bool = False) -> ToolExecutionResult:
    output = {
        "transport_success": not is_error,
        "business_success": payload.get("success"),
        "success": not is_error,
        "data": payload,
    }
    return ToolExecutionResult(
        output_for_model=json.dumps(output, ensure_ascii=False),
        payload=payload,
        is_error=is_error,
        content_text=[],
    )


def result_error_text(result: ToolExecutionResult) -> str:
    parts: list[str] = []
    if result.payload:
        for key in ("error", "message", "status"):
            value = result.payload.get(key)
            if value:
                parts.append(str(value))
    parts.extend(result.content_text)
    return "\n".join(parts)


def indicates_project_already_exists(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in PROJECT_EXISTS_MARKERS)


def is_test_timeout_payload(payload: dict[str, Any] | None) -> bool:
    return bool(payload and payload.get("timed_out") is True)


def response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) == "message":
            for content in getattr(item, "content", []) or []:
                text = getattr(content, "text", None)
                if text:
                    parts.append(text)
    return "\n".join(parts).strip()


def parse_approval(answer: str) -> bool | None:
    normalized = answer.strip().lower()
    if normalized in {"s", "si", "sí", "y", "yes"}:
        return True
    if normalized in {"n", "no"}:
        return False
    return None


def preview_text(value: str, limit: int = 500) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated preview]"


def argument_preview(arguments: dict[str, Any]) -> dict[str, Any]:
    preview = dict(arguments)
    if isinstance(preview.get("content"), str):
        content = preview["content"]
        preview["content_preview"] = preview_text(content)
        preview["content_size_bytes"] = len(content.encode("utf-8"))
        del preview["content"]
    if isinstance(preview.get("files"), list):
        files_preview = []
        total_size = 0
        for file in preview["files"]:
            content = str(file.get("content", ""))
            size = len(content.encode("utf-8"))
            total_size += size
            files_preview.append(
                {
                    "path": file.get("path"),
                    "size_bytes": size,
                    "content_preview": preview_text(content, 300),
                }
            )
        preview["files"] = files_preview
        preview["total_files"] = len(files_preview)
        preview["total_size_bytes"] = total_size
    return preview


async def ask_approval(tool: RegisteredTool, arguments: dict[str, Any]) -> bool:
    print("\nOperación sensible requiere aprobación:")
    print(f"Servidor: {tool.server_name}")
    print(f"Tool: {tool.original_name}")
    print("Argumentos:")
    print(json.dumps(argument_preview(arguments), indent=2, ensure_ascii=False))
    while True:
        answer = input("¿Aprobar? [s/n]: ")
        parsed = parse_approval(answer)
        if parsed is not None:
            return parsed
        print("Responde con s, si, sí, y, yes, n o no.")


async def load_planning_resources(manager: MCPClientManager) -> str:
    try:
        client = manager.get_client("software_factory")
    except KeyError:
        return ""
    blocks: list[str] = []
    try:
        resources_response = await client.list_resources()
        for resource in getattr(resources_response, "resources", resources_response):
            uri = str(getattr(resource, "uri", ""))
            if not uri:
                continue
            try:
                read_response = await client.read_resource(uri)
                contents = getattr(read_response, "contents", [])
                text = "\n".join(extract_text_from_mcp_content(item) for item in contents)
                blocks.append(f"Resource {uri}:\n{text}")
            except Exception as exc:
                print(f"No se pudo leer resource {uri}: {exc}")
    except Exception as exc:
        print(f"No se pudieron listar resources de planificaciÃ³n: {exc}")
    return "\n\n".join(blocks)


def find_function_calls(response: Any) -> list[Any]:
    return [item for item in getattr(response, "output", []) or [] if getattr(item, "type", None) == "function_call"]


def extract_original_user_message(initial_input: list[dict[str, Any]]) -> str:
    for item in initial_input:
        if item.get("role") != "user":
            continue
        content = item.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(str(block.get("text", "")))
                else:
                    parts.append(str(block))
            return "\n".join(part for part in parts if part)
    return ""


class SoftwareFactoryHost:
    def __init__(self, manager: MCPClientManager, openai_client: AsyncOpenAI, model: str, instructions: str) -> None:
        self.manager = manager
        self.openai_client = openai_client
        self.model = model
        self.instructions = instructions
        self.rejected_calls: set[str] = set()
        self.test_attempts: dict[str, int] = {}
        self.state: ExecutionState | None = None

    def reset_request_state(self) -> None:
        self.rejected_calls.clear()
        self.test_attempts.clear()
        self.state = None

    async def run_openai_loop(self, initial_input: list[dict[str, Any]]) -> str:
        original_message = extract_original_user_message(initial_input)
        requested_project_name = extract_requested_project_name(original_message)
        self.state = ExecutionState(
            original_user_message=original_message,
            requested_project_name=requested_project_name,
            workflow_intent=extract_workflow_intent(original_message, requested_project_name),
        )
        conversation_input = initial_input
        premature_text_corrections = 0
        for _round in range(1, MAX_OPENAI_ROUNDS + 1):
            if is_terminal_state(self.state):
                return await self._finalize_after_terminal_state(conversation_input)
            allowed_tools = get_allowed_tools(self.state, self.manager.registry)
            if not allowed_tools:
                return await self._finalize_after_terminal_state(conversation_input)
            forced_tool_choice = len(allowed_tools) == 1
            self.log_workflow_decision(self.state, allowed_tools, forced_tool_choice)
            response = await self.openai_client.responses.create(
                model=self.model,
                instructions=build_request_instructions(self.instructions, self.state),
                tools=self.openai_tools_from_registered(allowed_tools),
                tool_choice=self.tool_choice_for_allowed(allowed_tools),
                input=conversation_input,
            )
            calls = find_function_calls(response)
            if not calls:
                if allowed_tools and premature_text_corrections < 2:
                    premature_text_corrections += 1
                    conversation_input = [dump_response_item(item) for item in response.output]
                    continue
                final_text = response_text(response)
                return final_text or "(La respuesta final vino vacÃ­a.)"

            next_input = [dump_response_item(item) for item in response.output]
            sensitive_executed_this_round = False
            for call in calls:
                tool = self.lookup_call_tool(call)
                if tool is not None and tool.requires_approval and sensitive_executed_this_round:
                    print(f"Se pospone una tool sensible adicional de la misma respuesta: {getattr(call, 'name', '')}")
                    continue
                output, executed_sensitive = await self.handle_function_call(call)
                next_input.append(output)
                if self.state and is_terminal_state(self.state):
                    conversation_input = next_input
                    return await self._finalize_after_terminal_state(conversation_input)
                if executed_sensitive:
                    sensitive_executed_this_round = True
                    break
            conversation_input = next_input
        if self.state:
            self.state.terminal_status = "round_limit_reached"
        return await self._finalize_after_terminal_state(conversation_input)

    def filtered_openai_tools(self, state: ExecutionState) -> list[dict[str, Any]]:
        return self.openai_tools_from_registered(get_allowed_tools(state, self.manager.registry))

    @staticmethod
    def openai_tools_from_registered(tools: list[RegisteredTool]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool.public_name,
                "description": tool.description,
                "parameters": tool.input_schema,
            }
            for tool in tools
        ]

    @staticmethod
    def tool_choice_for_allowed(tools: list[RegisteredTool]) -> str | dict[str, str]:
        if len(tools) == 1:
            return {"type": "function", "name": tools[0].public_name}
        return "auto"

    def log_workflow_decision(self, state: ExecutionState, allowed_tools: list[RegisteredTool], forced_tool_choice: bool) -> None:
        print("Workflow decision:")
        print(f"- next_action: {determine_next_action(state)}")
        print("- allowed_tools:")
        for tool in allowed_tools:
            print(f"  - {tool.public_name}")
        print(f"- forced_tool_choice: {str(forced_tool_choice).lower()}")

    async def _finalize_after_terminal_state(self, conversation_input: list[dict[str, Any]]) -> str:
        state = self.state
        status = terminal_status_for(state) if state else "round_limit_reached"
        if state:
            state.terminal_status = status
        finalization_instructions = (
            "You are finalizing an MCP Software Factory workflow. Do not call tools. "
            f"Terminal status: {status}. "
            "The terminal summary JSON in the input is the source of truth. Do not contradict it. "
            "Never say the project is unknown when project_name is present. "
            "Do not mention cloud, virtual machines, credentials, operators, external services, or network unless they appear explicitly in failure_message. "
            "Give a brief factual answer with the project state, validation state, exact failing stage and error when present, and the next action derived only from that error. "
            "When tests passed, include Proyecto, Estado, Entorno, Dependencias, Framework de pruebas, Python usado, Comando real, Resultado, Warnings, FastAPI and Starlette. "
            "When a repair was completed, include Primera ejecución, Archivo corregido, Corrección, Segunda ejecución, and Python. "
            "If repair_decision is present, treat it as resolved evidence: do not reopen that decision, do not suggest changing the endpoint, explain which file was wrong and why it was corrected. "
            "Use actual_test_command when present; otherwise use expected_test_command. Never simplify a concrete .venv executable path to python. "
            "Do not invent missing values."
        )
        final_input = list(conversation_input)
        if state:
            final_input.append(
                {
                    "role": "system",
                    "content": "Terminal summary source of truth:\n"
                    + json.dumps(build_terminal_summary(state), ensure_ascii=False, indent=2),
                }
            )
        response = await self.openai_client.responses.create(
            model=self.model,
            instructions=finalization_instructions,
            input=final_input,
            tools=[],
        )
        final_text = response_text(response)
        return final_text or self.round_limit_diagnostic()

    def lookup_call_tool(self, call: Any) -> RegisteredTool | None:
        try:
            return self.manager.registry.get(getattr(call, "name", ""))
        except KeyError:
            return None

    async def handle_function_call(
        self,
        call: Any,
        *,
        approval_mode: Literal["prompt", "already_approved"] = "prompt",
    ) -> tuple[dict[str, Any], bool]:
        call_id = getattr(call, "call_id", "")
        name = getattr(call, "name", "")
        raw_arguments = getattr(call, "arguments", "{}") or "{}"
        try:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("function arguments must be a JSON object")
        except Exception as exc:
            result = {"success": False, "status": "invalid_json", "error": str(exc)}
            execution_result = local_execution_result(result, is_error=True)
            return self.function_output_from_execution(call_id, execution_result), False

        try:
            tool = self.manager.registry.get(name)
        except KeyError as exc:
            execution_result = local_execution_result(
                {"success": False, "status": "unknown_tool", "error": str(exc)},
                is_error=True,
            )
            return self.function_output_from_execution(call_id, execution_result), False

        arguments = normalize_tool_arguments(tool, arguments)
        schema_errors = validate_tool_arguments(tool, arguments)
        if schema_errors:
            result = invalid_tool_arguments_payload(tool, schema_errors)
            execution_result = local_execution_result(result, is_error=False)
            if self.state:
                self.state.failure_type = FailureType.INVALID_TOOL_ARGUMENTS.value
                self.state.failure_stage = "validate_arguments"
                self.state.failure_message = "; ".join(schema_errors)
            self.log_tool_result(tool, execution_result)
            return self.function_output_from_execution(call_id, execution_result), False

        if (
            self.state
            and determine_next_action(self.state) == "fix_failed_tests"
            and tool.public_name == "filesystem__read_file"
        ):
            project_name = self.state.created_project_name or self.state.requested_project_name or ""
            try:
                arguments["relative_path"] = normalize_project_relative_path(project_name, str(arguments.get("relative_path", "")))
            except ValueError as exc:
                execution_result = local_execution_result(
                    {
                        "success": False,
                        "status": FailureType.INVALID_TOOL_ARGUMENTS.value,
                        "message": str(exc),
                    },
                    is_error=False,
                )
                self.log_tool_result(tool, execution_result)
                return self.function_output_from_execution(call_id, execution_result), False

        rejection_key = f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
        if rejection_key in self.rejected_calls:
            execution_result = local_execution_result(
                {"success": False, "status": "repeated_rejected_operation"},
                is_error=False,
            )
            return self.function_output_from_execution(call_id, execution_result), False

        state_validation = self.validate_call_against_state(tool, arguments)
        if state_validation is not None:
            execution_result = local_execution_result(state_validation, is_error=False)
            self.log_tool_result(tool, execution_result)
            return self.function_output_from_execution(call_id, execution_result), False

        retry_limit = self.enforce_test_retry_limit(tool, arguments)
        if retry_limit is not None:
            execution_result = local_execution_result(retry_limit, is_error=False)
            self.log_tool_result(tool, execution_result)
            return self.function_output_from_execution(call_id, execution_result), False

        if tool.requires_approval and approval_mode == "prompt":
            if self.state and tool.public_name in self.state.rejected_tools:
                result = {
                    "success": False,
                    "status": "previously_rejected",
                    "tool": tool.public_name,
                    "message": "El usuario ya rechazó esta operación durante la solicitud actual.",
                }
                execution_result = local_execution_result(result, is_error=False)
                self.log_tool_result(tool, execution_result)
                return self.function_output_from_execution(call_id, execution_result), False
            approved = await self.request_approval(tool, arguments)
            if not approved:
                self.rejected_calls.add(rejection_key)
                if self.state:
                    self.state.rejected_tools.add(tool.public_name)
                    self.state.user_cancelled = True
                    self.state.terminal_status = "rejected_by_user"
                    self.state.failure_type = FailureType.USER_REJECTED.value
                    self.state.failure_stage = "approval"
                    self.state.failure_message = "El usuario rechazó la operación sensible."
                result = {
                    "success": False,
                    "status": "rejected_by_user",
                    "server": tool.server_name,
                    "tool": tool.original_name,
                }
                execution_result = local_execution_result(result, is_error=False)
                self.log_tool_result(tool, execution_result)
                return self.function_output_from_execution(call_id, execution_result), False

        try:
            result = await tool.client.call_tool(tool.original_name, arguments)
            execution_result = normalize_mcp_result(result)
            execution_result = self.reconcile_execution_result(tool, arguments, execution_result)
            self._update_execution_state(self.state, tool, execution_result, arguments)
            self.log_tool_result(tool, execution_result)
            return self.function_output_from_execution(call_id, execution_result), tool.requires_approval
        except MCPToolTimeout as exc:
            result = {
                "success": False,
                "status": "mcp_timeout",
                "failure_type": "mcp_timeout",
                "failure_stage": tool.original_name,
                "server": exc.server_name,
                "tool": exc.tool_name,
                "error": str(exc),
                "message": "El MCP Server no respondió dentro del timeout configurado.",
            }
            execution_result = local_execution_result(result, is_error=True)
            execution_result = self.reconcile_execution_result(tool, arguments, execution_result)
            self._update_execution_state(self.state, tool, execution_result, arguments)
            self.log_tool_result(tool, execution_result)
            return self.function_output_from_execution(call_id, execution_result), tool.requires_approval
        except Exception as exc:
            result = {"success": False, "status": "mcp_error", "error": str(exc)}
            execution_result = local_execution_result(result, is_error=True)
            execution_result = self.reconcile_execution_result(tool, arguments, execution_result)
            self._update_execution_state(self.state, tool, execution_result, arguments)
            self.log_tool_result(tool, execution_result)
            return self.function_output_from_execution(call_id, execution_result), tool.requires_approval

    async def request_approval(self, tool: RegisteredTool, arguments: dict[str, Any]) -> bool:
        if tool.public_name == "filesystem__update_project_files":
            print("\nVista previa de reparación:")
            for file in arguments.get("files", []) if isinstance(arguments.get("files"), list) else []:
                if not isinstance(file, dict):
                    continue
                path = str(file.get("path", ""))
                content = str(file.get("content", ""))
                print(f"Archivo: {path}")
                if '"status": "ok"' in content and self.state and self.state.test_failure_summary and "healthy" in self.state.test_failure_summary:
                    print('Antes: "status": "healthy"')
                    print('Después: "status": "ok"')
                else:
                    print("Contenido nuevo:")
                    print(preview_text(content, 800))
        return await ask_approval(tool, arguments)

    def validate_call_against_state(self, tool: RegisteredTool, arguments: dict[str, Any]) -> dict[str, Any] | None:
        state = self.state
        if state is None:
            return None
        completed_steps = {
            "software_factory__analyze_requirement": (
                state.analysis_completed,
                "El análisis ya fue completado. Continúa con la siguiente etapa.",
            ),
            "software_factory__create_tasks": (
                state.tasks_created,
                "Las tareas ya fueron creadas. Continúa con la siguiente etapa.",
            ),
        }
        if tool.public_name in completed_steps:
            completed, message = completed_steps[tool.public_name]
            if completed:
                return {
                    "success": False,
                    "status": "step_already_completed",
                    "tool": tool.public_name,
                    "message": message,
                }
        blocked_after_infra_failure = {
            "filesystem__list_files",
            "filesystem__read_file",
            "filesystem__update_project_files",
            "testing__detect_test_framework",
            "testing__prepare_test_environment",
            "testing__run_tests",
        }
        if state.test_infrastructure_failed and tool.public_name in blocked_after_infra_failure:
            return {
                "success": False,
                "status": "workflow_stopped_after_test_infrastructure_failure",
                "tool": tool.public_name,
                "message": "El flujo se detuvo porque las pruebas no pudieron ejecutarse por un problema de infraestructura.",
            }
        if tool.public_name == "testing__prepare_test_environment" and state.environment_prepared:
            return {
                "success": False,
                "status": "environment_already_prepared",
                "project_name": arguments.get("project_name") or state.created_project_name,
                "message": "El entorno ya fue preparado. Continúa con run_tests.",
            }
        if determine_next_action(state) == "fix_failed_tests":
            expected_tool = repair_phase_allowed_tool_name(state)
            if tool.public_name != expected_tool:
                return {
                    "success": False,
                    "status": "repair_step_not_allowed",
                    "tool": tool.public_name,
                    "expected_tool": expected_tool,
                    "repair_phase": state.repair_phase,
                    "message": "Sigue la fase de reparación indicada por el Host.",
                }
            project_name = state.created_project_name or state.requested_project_name
            if tool.public_name == "filesystem__read_file":
                if state.repair_phase in {"not_started", "read_failing_test"} and not state.failing_test_files:
                    return {
                        "success": False,
                        "status": "missing_failing_test_file",
                        "message": "No se pudo extraer de pytest un archivo de test fallido para reparar de forma segura.",
                    }
                expected_relative = state.failing_test_files[0] if state.repair_phase in {"not_started", "read_failing_test"} else infer_related_source_file(state)
                if expected_relative:
                    expected_path = expected_project_relative_path(state, expected_relative)
                    received_path = normalize_pytest_path(str(arguments.get("relative_path", "")))
                    if received_path != expected_path:
                        return {
                            "success": False,
                            "status": "repair_read_path_not_allowed",
                            "expected": expected_path,
                            "received": received_path,
                            "message": "Lee únicamente el archivo requerido por la fase de reparación.",
                        }
            if tool.public_name == "filesystem__update_project_files":
                if project_name and arguments.get("project_name") != project_name:
                    return {
                        "success": False,
                        "status": "argument_conflict",
                        "field": "project_name",
                        "expected": project_name,
                        "received": arguments.get("project_name"),
                        "message": "update_project_files debe apuntar al proyecto en revisión.",
                    }
                files = arguments.get("files")
                if not isinstance(files, list) or not files:
                    return {
                        "success": False,
                        "status": FailureType.INVALID_TOOL_ARGUMENTS.value,
                        "message": "update_project_files requiere al menos un archivo.",
                    }
                allowed = set(state.failing_test_files)
                for file in files:
                    path = normalize_pytest_path(str(file.get("path", ""))) if isinstance(file, dict) else ""
                    candidate = Path(path)
                    if not path or candidate.is_absolute() or ".." in candidate.parts:
                        return {
                            "success": False,
                            "status": FailureType.INVALID_TOOL_ARGUMENTS.value,
                            "message": "update_project_files solo acepta paths relativos dentro del proyecto.",
                        }
                    if allowed and path not in allowed:
                        return {
                            "success": False,
                            "status": "repair_update_path_not_allowed",
                            "expected": sorted(allowed),
                            "received": path,
                            "message": "La reparación debe modificar únicamente el test fallido identificado.",
                        }
            if tool.public_name == "testing__run_tests":
                if project_name and arguments.get("project_name") != project_name:
                    return {
                        "success": False,
                        "status": "argument_conflict",
                        "field": "project_name",
                        "expected": project_name,
                        "received": arguments.get("project_name"),
                    }
        if tool.public_name == "filesystem__create_project_structure":
            requested_name = state.requested_project_name
            received_name = arguments.get("project_name")
            files = arguments.get("files")
            if not isinstance(files, list) or not files:
                return {
                    "success": False,
                    "status": FailureType.INVALID_TOOL_ARGUMENTS.value,
                    "message": "create_project_structure requiere al menos un archivo.",
                }
            if requested_name and received_name != requested_name:
                return {
                    "success": False,
                    "status": "argument_conflict",
                    "tool": "filesystem__create_project_structure",
                    "field": "project_name",
                    "expected": requested_name,
                    "received": received_name,
                    "message": "El nombre del proyecto contradice la solicitud explícita del usuario.",
                }
            if state.project_created or state.project_exists:
                return {
                    "success": False,
                    "status": "project_already_created_in_current_run" if state.project_created else "project_already_exists",
                    "project_name": state.created_project_name,
                    "message": (
                        "El proyecto ya fue creado durante esta solicitud. Continúa con pruebas o correcciones."
                        if state.project_created
                        else "El proyecto ya existe. No vuelvas a crearlo; inspecciónalo y usa update_project_files si necesita correcciones."
                    ),
                }
            dependency_policy_violation = dependency_policy_for_project_files(arguments.get("files"))
            if dependency_policy_violation is not None:
                return dependency_policy_violation
        return None

    def reconcile_execution_result(
        self,
        tool: RegisteredTool,
        arguments: dict[str, Any],
        execution_result: ToolExecutionResult,
    ) -> ToolExecutionResult:
        if (
            tool.server_name == "filesystem"
            and tool.original_name == "create_project_structure"
            and execution_result.is_error
            and indicates_project_already_exists(result_error_text(execution_result))
        ):
            project_name = str(arguments.get("project_name") or (self.state.created_project_name if self.state else ""))
            payload = {
                "success": False,
                "status": "project_already_exists",
                "project_name": project_name,
                "message": "El proyecto ya existe. No vuelvas a crearlo; inspecciónalo y usa update_project_files si necesita correcciones.",
            }
            return local_execution_result(payload, is_error=False)
        if (
            tool.server_name == "testing"
            and tool.original_name == "run_tests"
            and not execution_result.is_error
            and is_test_timeout_payload(execution_result.payload)
        ):
            original_payload = execution_result.payload or {}
            payload = dict(original_payload)
            payload.update(
                {
                    "success": False,
                    "status": "test_infrastructure_failure",
                    "message": "La ejecución de pruebas excedió el timeout. No modifiques el código basándote en este resultado.",
                }
            )
            return local_execution_result(payload, is_error=False)
        if (
            tool.server_name == "testing"
            and tool.original_name == "prepare_test_environment"
            and not execution_result.is_error
            and execution_result.payload
            and execution_result.payload.get("success") is False
        ):
            payload = dict(execution_result.payload)
            failure_stage = payload.get("failure_stage")
            status_by_stage = {
                "validate_dependencies": "dependency_policy_violation",
                "create_venv": "create_venv_failure",
                "invalid_existing_environment": "invalid_existing_environment",
                "upgrade_pip": "pip_upgrade_failure",
                "install_dependencies": "dependency_installation_failure",
                "pip_check": "pip_check_failure",
                "inspect_versions": "version_inspection_failure",
            }
            if payload.get("timed_out") is True:
                payload["status"] = "installation_timeout"
            else:
                payload["status"] = status_by_stage.get(str(failure_stage), "dependency_installation_failure")
            payload.setdefault("message", "La preparación del entorno falló. No modifiques archivos automáticamente basándote en este resultado.")
            return local_execution_result(payload, is_error=False)
        return execution_result

    def _update_execution_state(
        self,
        state: ExecutionState | None,
        tool: RegisteredTool,
        execution_result: ToolExecutionResult,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        if state is None:
            return
        arguments = arguments or {}
        state.last_tool_executed = tool.public_name
        if tool.requires_approval:
            state.sensitive_tools_executed.append(tool.public_name)

        if not execution_result.is_error:
            if tool.server_name == "software_factory" and tool.original_name == "analyze_requirement":
                state.analysis_completed = True
            elif tool.server_name == "software_factory" and tool.original_name == "create_tasks":
                state.tasks_created = True
            elif tool.server_name == "filesystem" and tool.original_name == "list_files":
                state.workspace_inspected = True

        payload = unwrap_business_payload(execution_result.payload)
        business_success = execution_result.is_error is False and payload is not None and payload.get("success") is True
        if payload and payload.get("success") is False:
            state.failure_type = str(payload.get("failure_type") or payload.get("status") or "unknown_failure")
            state.failure_stage = str(payload.get("failure_stage") or payload.get("status") or tool.original_name)
            message = payload.get("message") or payload.get("error") or payload.get("stderr") or payload.get("status")
            state.failure_message = str(message) if message else None
            if payload.get("status") == "mcp_timeout":
                if tool.server_name == "git" and tool.original_name == "init":
                    state.terminal_status = "infrastructure_failed"
                    state.failure_type = "git_initialization_failed"
                    state.failure_stage = "git.init"
                    state.failure_message = str(
                        payload.get("message")
                        or payload.get("error")
                        or "Git MCP initialization timed out."
                    )
                elif tool.server_name == "testing":
                    state.terminal_status = "infrastructure_failed"
                    state.test_infrastructure_failed = True
        if tool.server_name == "filesystem" and tool.original_name == "list_files" and payload:
            requested_name = state.requested_project_name
            entries = payload.get("entries")
            if requested_name and isinstance(entries, list):
                found = any(
                    isinstance(entry, dict)
                    and entry.get("type") == "directory"
                    and normalize_pytest_path(str(entry.get("path", ""))).rstrip("/") == requested_name
                    for entry in entries
                )
                if found:
                    state.project_exists = True
                    state.created_project_name = requested_name
                elif state.workflow_intent == "review_existing_project":
                    state.terminal_status = "project_not_found"
                    state.failure_type = "project_not_found"
                    state.failure_stage = "list_files"
                    state.failure_message = f"No se encontró el proyecto existente solicitado: {requested_name}."
        if tool.server_name == "filesystem" and tool.original_name == "create_project_structure":
            if execution_result.is_error is False and payload and payload.get("status") == "project_already_exists":
                state.project_exists = True
                state.created_project_name = str(payload.get("project_name") or arguments.get("project_name", ""))
            elif business_success:
                project_name = str(payload.get("project_name") or arguments.get("project_name", ""))
                state.project_created = True
                state.created_project_name = project_name
                print(f"Proyecto creado registrado en estado: {project_name}")
            elif execution_result.is_error is False:
                print("Aviso: create_project_structure terminó sin error pero no devolvió payload de éxito esperado.")
        if tool.server_name == "filesystem" and tool.original_name == "read_file" and payload:
            path = normalize_pytest_path(str(payload.get("path") or arguments.get("relative_path", "")))
            if state.repair_phase in {"not_started", "read_failing_test", "read_related_source"}:
                state.files_read_during_repair.add(path)
                if state.repair_phase in {"not_started", "read_failing_test"}:
                    state.repair_phase = "read_related_source" if infer_related_source_file(state) else "apply_fix"
                elif state.repair_phase == "read_related_source":
                    state.repair_phase = "apply_fix"
        if tool.server_name == "filesystem" and tool.original_name == "update_project_files" and business_success:
            files = arguments.get("files")
            if isinstance(files, list):
                for file in files:
                    if isinstance(file, dict):
                        state.files_updated_during_repair.add(normalize_pytest_path(str(file.get("path", ""))))
                repair_before, repair_after, repair_decision = extract_repair_evidence_from_update(files)
                if repair_before:
                    state.repair_before = repair_before
                if repair_after:
                    state.repair_after = repair_after
                if repair_decision:
                    state.repair_decision = repair_decision
            if state.repair_phase == "apply_fix":
                state.repair_attempts += 1
                state.repair_phase = "rerun_tests"
        if tool.server_name == "testing" and tool.original_name == "detect_test_framework" and payload:
            framework = payload.get("framework")
            if framework:
                state.detected_test_framework = str(framework)
            command = payload.get("command")
            if isinstance(command, list):
                state.expected_test_command = [str(part) for part in command]
        if tool.server_name == "testing" and tool.original_name == "run_tests":
            state.tests_executed = True
            state.tests_passed = bool(payload and payload.get("success") is True)
            if payload:
                state.test_stdout = str(payload.get("stdout") or "")
                state.test_stderr = str(payload.get("stderr") or "")
                result_summary = summarize_pytest_result(state.test_stdout, state.tests_passed)
                if state.tests_passed:
                    state.final_test_result_summary = result_summary
                    if state.repair_phase == "rerun_tests":
                        state.repair_phase = "completed"
                elif state.first_test_result_summary is None:
                    state.first_test_result_summary = result_summary
                if payload.get("exit_code") == 5:
                    state.failure_type = FailureType.NO_TESTS_COLLECTED.value
                    state.failure_stage = "run_tests"
                    state.failure_message = "Pytest no encontrÃ³ pruebas ejecutables."
                    state.test_failure_summary = state.failure_message
                    state.failing_test_files = []
                    state.repair_phase = "not_started"
                    state.terminal_status = "tests_failed"
                elif payload.get("failure_type") == "test_failure":
                    state.failure_type = FailureType.TEST_FAILURE.value
                    state.failure_stage = "run_tests"
                    state.test_failure_summary = extract_failure_summary(state.test_stdout, state.test_stderr)
                    state.failure_message = state.test_failure_summary
                    state.failing_test_files = extract_failing_test_files(state.test_stdout, state.test_stderr)
                    if state.repair_attempts >= MAX_REPAIR_ATTEMPTS:
                        state.retry_limit_reached = True
                        state.terminal_status = "retry_limit_reached"
                    else:
                        state.repair_phase = "read_failing_test"
            command = payload.get("command") if payload else None
            if isinstance(command, list):
                state.actual_test_command = [str(part) for part in command]
            if payload and "test_warning_count" in payload:
                count = payload.get("test_warning_count")
                state.test_warning_count = int(count) if isinstance(count, int) else None
            if payload and payload.get("timed_out") is True:
                state.test_infrastructure_failed = True
                state.failure_type = FailureType.TEST_TIMEOUT.value
                state.failure_stage = "run_tests"
                state.failure_message = str(payload.get("stderr") or payload.get("message") or "La ejecución de pruebas excedió el timeout.")
        if tool.server_name == "testing" and tool.original_name == "prepare_test_environment":
            if business_success:
                state.environment_prepared = True
                state.dependencies_installed = True
                state.environment_python = str(payload.get("python_executable") or "")
                fastapi_version = payload.get("installed_fastapi_version")
                starlette_version = payload.get("installed_starlette_version")
                state.installed_fastapi_version = str(fastapi_version) if fastapi_version else None
                state.installed_starlette_version = str(starlette_version) if starlette_version else None
            else:
                state.environment_prepared = False
                state.dependencies_installed = False
                failure_type = str(payload.get("failure_type") or "") if payload else ""
                failure_stage = str(payload.get("failure_stage") or "") if payload else ""
                infrastructure_failures = {
                    "timeout",
                    "create_venv_failure",
                    "pip_upgrade_failure",
                    "dependency_installation_failure",
                    "pip_check_failure",
                    "version_inspection_failure",
                    "invalid_existing_environment",
                }
                if failure_type in infrastructure_failures or failure_stage in {
                    "create_venv",
                    "upgrade_pip",
                    "install_dependencies",
                    "pip_check",
                    "inspect_versions",
                    "invalid_existing_environment",
                }:
                    state.test_infrastructure_failed = True
                    state.terminal_status = "infrastructure_failed"

    def update_state_after_tool(self, tool: RegisteredTool, arguments: dict[str, Any], execution_result: ToolExecutionResult) -> None:
        self._update_execution_state(self.state, tool, execution_result, arguments)

    def log_tool_result(self, tool: RegisteredTool, execution_result: ToolExecutionResult) -> None:
        payload = execution_result.payload or {}
        print("\nResultado tool:")
        print(f"- public_tool: {tool.public_name}")
        print(f"- server: {tool.server_name}")
        print(f"- original_tool: {tool.original_name}")
        print(f"- is_error: {str(execution_result.is_error).lower()}")
        if "success" in payload:
            print(f"- business_success: {str(payload.get('success')).lower()}")
        for key in (
            "project_name",
            "project_path",
            "failure_type",
            "exit_code",
            "timed_out",
            "duration_seconds",
            "process_tree_terminated",
            "total_files",
            "environment_path",
            "python_executable",
            "dependency_file",
            "venv_created",
            "pip_upgraded",
            "dependencies_installed",
            "installed_fastapi_version",
            "installed_starlette_version",
            "framework",
            "command",
            "test_warning_count",
            "failure_stage",
            "environment_incomplete",
        ):
            if key in payload:
                print(f"- {key}: {payload.get(key)}")
        if "errors" in payload:
            print(f"- validation_errors: {payload.get('errors')}")
        if payload:
            print(f"- payload: {preview_text(json.dumps(payload, ensure_ascii=False), 2000)}")
        error = payload.get("error") or "\n".join(execution_result.content_text)
        if execution_result.is_error and error:
            print(f"- error: {preview_text(str(error), 2000)}")
        if "stdout" in payload:
            print(f"- stdout_preview: {preview_text(str(payload.get('stdout') or ''), 2000)}")
        if "stderr" in payload:
            print(f"- stderr_preview: {preview_text(str(payload.get('stderr') or ''), 2000)}")
        self.log_state()

    def log_state(self) -> None:
        if self.state is None:
            return
        print("Estado actual:")
        print(f"- analysis_completed: {str(self.state.analysis_completed).lower()}")
        print(f"- tasks_created: {str(self.state.tasks_created).lower()}")
        print(f"- workspace_inspected: {str(self.state.workspace_inspected).lower()}")
        print(f"- project_created: {str(self.state.project_created).lower()}")
        print(f"- project_exists: {str(self.state.project_exists).lower()}")
        print(f"- created_project_name: {self.state.created_project_name}")
        print(f"- tests_executed: {str(self.state.tests_executed).lower()}")
        print(f"- tests_passed: {str(self.state.tests_passed).lower()}")
        print(f"- test_infrastructure_failed: {str(self.state.test_infrastructure_failed).lower()}")
        print(f"- environment_prepared: {str(self.state.environment_prepared).lower()}")
        print(f"- dependencies_installed: {str(self.state.dependencies_installed).lower()}")
        print(f"- environment_python: {self.state.environment_python}")
        print(f"- installed_fastapi_version: {self.state.installed_fastapi_version}")
        print(f"- installed_starlette_version: {self.state.installed_starlette_version}")
        print(f"- detected_test_framework: {self.state.detected_test_framework}")
        print(f"- expected_test_command: {self.state.expected_test_command}")
        print(f"- actual_test_command: {self.state.actual_test_command}")
        print(f"- test_warning_count: {self.state.test_warning_count}")
        print(f"- workflow_intent: {self.state.workflow_intent}")
        print(f"- repair_phase: {self.state.repair_phase}")
        print(f"- repair_attempts: {self.state.repair_attempts}")
        print(f"- repair_decision: {self.state.repair_decision}")
        print(f"- repair_before: {self.state.repair_before}")
        print(f"- repair_after: {self.state.repair_after}")
        print(f"- failing_test_files: {self.state.failing_test_files}")
        print(f"- files_read_during_repair: {sorted(self.state.files_read_during_repair)}")
        print(f"- files_updated_during_repair: {sorted(self.state.files_updated_during_repair)}")

    def round_limit_diagnostic(self) -> str:
        state = self.state
        if state is None:
            return "Se alcanzó el límite general de rondas de tool calling sin estado de ejecución disponible."
        return (
            "Se alcanzó el límite general de rondas de tool calling sin respuesta final.\n"
            "Diagnóstico:\n"
            f"- last_tool_executed: {state.last_tool_executed}\n"
            f"- analysis_completed: {state.analysis_completed}\n"
            f"- tasks_created: {state.tasks_created}\n"
            f"- workspace_inspected: {state.workspace_inspected}\n"
            f"- project_created: {state.project_created}\n"
            f"- project_exists: {state.project_exists}\n"
            f"- created_project_name: {state.created_project_name}\n"
            f"- tests_executed: {state.tests_executed}\n"
            f"- tests_passed: {state.tests_passed}\n"
            f"- test_infrastructure_failed: {state.test_infrastructure_failed}"
            f"\n- environment_prepared: {state.environment_prepared}"
            f"\n- dependencies_installed: {state.dependencies_installed}"
            f"\n- environment_python: {state.environment_python}"
            f"\n- workflow_intent: {state.workflow_intent}"
            f"\n- repair_phase: {state.repair_phase}"
            f"\n- repair_decision: {state.repair_decision}"
            f"\n- repair_before: {state.repair_before}"
            f"\n- repair_after: {state.repair_after}"
            f"\n- failing_test_files: {state.failing_test_files}"
            f"\n- files_updated_during_repair: {sorted(state.files_updated_during_repair)}"
        )

    def enforce_test_retry_limit(self, tool: RegisteredTool, arguments: dict[str, Any]) -> dict[str, Any] | None:
        if tool.server_name != "testing" or tool.original_name != "run_tests":
            return None
        project_name = str(arguments.get("project_name", ""))
        attempts = self.test_attempts.get(project_name, 0)
        if attempts >= MAX_TEST_ATTEMPTS:
            if self.state:
                self.state.retry_limit_reached = True
                self.state.terminal_status = "retry_limit_reached"
                self.state.failure_type = "retry_limit_reached"
                self.state.failure_stage = "run_tests"
                self.state.failure_message = f"Se alcanzó el límite de {MAX_TEST_ATTEMPTS} intentos de prueba."
            return {
                "success": False,
                "status": "retry_limit_reached",
                "project_name": project_name,
                "attempts": attempts,
                "max_attempts": MAX_TEST_ATTEMPTS,
            }
        self.test_attempts[project_name] = attempts + 1
        return None

    @staticmethod
    def function_output(call_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(result, ensure_ascii=False),
        }

    @staticmethod
    def function_output_from_execution(call_id: str, result: ToolExecutionResult) -> dict[str, Any]:
        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": result.output_for_model,
        }


def user_message(text: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": text}]


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "si", "sí", "on"}


def prompt_messages_to_responses_input(prompt_result: Any) -> list[dict[str, Any]]:
    messages = getattr(prompt_result, "messages", [])
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = getattr(message, "role", "user")
        content = extract_text_from_mcp_content(getattr(message, "content", ""))
        converted.append({"role": role, "content": content})
    return converted or user_message(str(prompt_result))


async def handle_plan_project_command(manager: MCPClientManager, command: str) -> list[dict[str, Any]]:
    payload = command[len("/plan-project") :].strip()
    backend = "fastapi"
    requirement = payload
    if "|" in payload:
        left, right = payload.split("|", 1)
        backend = left.strip() or "fastapi"
        requirement = right.strip()
    if not requirement:
        raise ValueError("uso: /plan-project fastapi | Crear un sistema de reservas mÃ©dicas")
    client = manager.get_client("software_factory")
    prompt_result = await client.get_prompt("plan_project", {"requirement": requirement, "backend": backend})
    return prompt_messages_to_responses_input(prompt_result)


def command_argument(command: str, prefix: str) -> str:
    value = command[len(prefix) :].strip()
    if not value:
        raise ValueError(f"uso: {prefix} <thread_id>")
    return value


def command_thread_and_reason(command: str, prefix: str) -> tuple[str, str | None]:
    value = command[len(prefix) :].strip()
    if not value:
        raise ValueError(f"uso: {prefix} <thread_id> [motivo opcional]")
    parts = value.split(maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else None


def command_thread_checkpoint(command: str, prefix: str) -> tuple[str, str]:
    value = command[len(prefix) :].strip()
    parts = value.split()
    if len(parts) != 2:
        raise ValueError(f"uso: {prefix} <thread_id> <checkpoint_id>")
    return parts[0], parts[1]


def command_fork_arguments(command: str, prefix: str) -> tuple[str, str, str]:
    value = command[len(prefix) :].strip()
    parts = value.split(maxsplit=2)
    if len(parts) != 3:
        raise ValueError(f"uso: {prefix} <thread_id> <checkpoint_id> <updates-json|json-file>")
    return parts[0], parts[1], parts[2]


def load_fork_updates(value: str, *, max_bytes: int = 32 * 1024) -> dict[str, Any]:
    stripped = value.strip()
    if stripped.startswith("{"):
        if len(stripped.encode("utf-8")) > max_bytes:
            raise ValueError(f"El JSON de fork excede {max_bytes} bytes.")
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON inválido: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Los updates del fork deben ser un objeto JSON.")
        return payload
    return load_fork_updates_file(stripped, max_bytes=max_bytes)


def load_fork_updates_file(path_value: str, *, max_bytes: int = 32 * 1024) -> dict[str, Any]:
    candidate = Path(path_value).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (ROOT / candidate).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise ValueError("El archivo JSON debe estar dentro del repositorio.") from exc
    if not resolved.is_file():
        raise ValueError(f"No existe el archivo JSON: {resolved}")
    if resolved.suffix.lower() != ".json":
        raise ValueError("El archivo de fork debe tener extensión .json.")
    if resolved.stat().st_size > max_bytes:
        raise ValueError(f"El archivo JSON excede {max_bytes} bytes.")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON inválido: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("El archivo de fork debe contener un objeto JSON.")
    return payload


def get_preferred_value(
    *,
    grouped: Mapping[str, Any] | None,
    grouped_key: str,
    state: Mapping[str, Any],
    legacy_key: str,
    default: Any = "n/a",
) -> Any:
    if grouped is not None and grouped_key in grouped:
        return grouped[grouped_key]
    if legacy_key in state:
        return state[legacy_key]
    return default


def format_bool(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "n/a"


def _format_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if value is True or value is False:
        return format_bool(value)
    return str(value)


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _count_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return value


def _nested_mapping(parent: Mapping[str, Any] | None, key: str) -> Mapping[str, Any] | None:
    if parent is None:
        return None
    value = parent.get(key)
    return value if isinstance(value, Mapping) else None


def format_planning_result(state: Mapping[str, Any]) -> list[str]:
    planning = _as_mapping(state.get("planning_result"))
    analysis = _nested_mapping(planning, "analysis") or _as_mapping(state.get("requirement_analysis"))
    project_type = get_preferred_value(
        grouped=planning,
        grouped_key="project_type",
        state={"project_type": get_preferred_value(grouped=analysis, grouped_key="project_type", state={}, legacy_key="project_type")},
        legacy_key="project_type",
    )
    functional_requirements = get_preferred_value(
        grouped=planning,
        grouped_key="functional_requirements",
        state=analysis or {},
        legacy_key="functional_requirements",
        default="n/a",
    )
    acceptance_criteria = get_preferred_value(
        grouped=planning,
        grouped_key="acceptance_criteria",
        state=state,
        legacy_key="acceptance_criteria",
        default="n/a",
    )
    tasks = get_preferred_value(
        grouped=planning,
        grouped_key="tasks",
        state=state,
        legacy_key="implementation_tasks",
        default="n/a",
    )
    return [
        "Planning result:",
        f"- valid: {format_bool(get_preferred_value(grouped=planning, grouped_key='valid', state=state, legacy_key='planning_valid'))}",
        f"- attempts: {_format_value(get_preferred_value(grouped=planning, grouped_key='attempts', state=state, legacy_key='planning_attempts'))}",
        f"- project type: {_format_value(project_type)}",
        f"- functional requirements: {_format_value(get_preferred_value(grouped=planning, grouped_key='functional_requirement_count', state={'value': _count_value(functional_requirements)}, legacy_key='value'))}",
        f"- acceptance criteria: {_format_value(get_preferred_value(grouped=planning, grouped_key='acceptance_criteria_count', state={'value': _count_value(acceptance_criteria)}, legacy_key='value'))}",
        f"- tasks: {_format_value(get_preferred_value(grouped=planning, grouped_key='task_count', state={'value': _count_value(tasks)}, legacy_key='value'))}",
    ]


def format_implementation_result(state: Mapping[str, Any]) -> list[str]:
    implementation = _as_mapping(state.get("implementation_result"))
    generated_files = get_preferred_value(
        grouped=implementation,
        grouped_key="generated_files",
        state=state,
        legacy_key="generated_files",
        default="n/a",
    )
    generated_file_count = get_preferred_value(
        grouped=implementation,
        grouped_key="generated_file_count",
        state={"generated_file_count": _count_value(generated_files)},
        legacy_key="generated_file_count",
    )
    return [
        "Implementation result:",
        f"- valid: {format_bool(get_preferred_value(grouped=implementation, grouped_key='valid', state=state, legacy_key='implementation_valid'))}",
        f"- attempts: {_format_value(get_preferred_value(grouped=implementation, grouped_key='attempts', state=state, legacy_key='implementation_attempts'))}",
        f"- package: {_format_value(get_preferred_value(grouped=implementation, grouped_key='package_name', state=state, legacy_key='generated_package_name'))}",
        f"- generated files: {_format_value(generated_file_count)}",
        f"- project created: {format_bool(get_preferred_value(grouped=implementation, grouped_key='project_created', state=state, legacy_key='project_created'))}",
        f"- environment prepared: {format_bool(get_preferred_value(grouped=implementation, grouped_key='environment_prepared', state=state, legacy_key='environment_prepared'))}",
        f"- dependencies installed: {format_bool(get_preferred_value(grouped=implementation, grouped_key='dependencies_installed', state=state, legacy_key='dependencies_installed'))}",
        f"- framework: {_format_value(get_preferred_value(grouped=implementation, grouped_key='framework', state=state, legacy_key='detected_test_framework'))}",
    ]


def format_testing_result(state: Mapping[str, Any]) -> list[str]:
    testing = _as_mapping(state.get("testing_result"))
    return [
        "Testing result:",
        f"- executed: {format_bool(get_preferred_value(grouped=testing, grouped_key='tests_executed', state=state, legacy_key='tests_executed'))}",
        f"- passed: {format_bool(get_preferred_value(grouped=testing, grouped_key='tests_passed', state=state, legacy_key='tests_passed'))}",
        f"- summary: {_format_value(get_preferred_value(grouped=testing, grouped_key='summary', state=state, legacy_key='final_test_result_summary'))}",
        f"- warnings: {_format_value(get_preferred_value(grouped=testing, grouped_key='warning_count', state=state, legacy_key='test_warning_count'))}",
        f"- repair phase: {_format_value(get_preferred_value(grouped=testing, grouped_key='repair_phase', state=state, legacy_key='repair_phase'))}",
        f"- repair attempts: {_format_value(get_preferred_value(grouped=testing, grouped_key='repair_attempts', state=state, legacy_key='repair_attempts'))}",
    ]


def format_supervisor_result(state: Mapping[str, Any]) -> list[str]:
    history_value = state.get("handoff_history")
    history = history_value if isinstance(history_value, list) else []
    last = history[-1] if history and isinstance(history[-1], Mapping) else {}
    previous = history[-2] if len(history) > 1 and isinstance(history[-2], Mapping) else {}
    current_stage = (
        previous.get("to")
        if last.get("to") == "finalize" and previous
        else last.get("to") or state.get("last_completed_node") or "n/a"
    )
    lines = [
        "Supervisor:",
        f"- current stage: {current_stage}",
        f"- last decision: {state.get('supervisor_decision') or 'n/a'}",
        f"- attempted target: {last.get('attempted_to') or last.get('to') or 'n/a'}",
        f"- executed target: {last.get('executed_to') or last.get('to') or 'n/a'}",
        f"- reason: {state.get('supervisor_reason') or 'n/a'}",
        f"- confidence: {_format_value(state.get('supervisor_confidence', 'n/a'))}",
        f"- source: {state.get('supervisor_decision_source') or last.get('source') or 'n/a'}",
        f"- attempts: {_format_value(state.get('supervisor_attempts', 0))}",
        (
            "- invalid decisions: "
            f"{_format_value(state.get('supervisor_invalid_decision_count', 0))}"
        ),
        (
            "- stagnant repetitions: "
            f"{_format_value(state.get('supervisor_stagnant_loop_probe_count', 0))}"
        ),
        f"- errors: {_format_value(state.get('supervisor_errors', []))}",
        (
            "- loop detected: true"
            if state.get("terminal_status") == "supervisor_loop_detected"
            or "supervisor_loop_detected" in (state.get("supervisor_errors") or [])
            else "- loop detected: false"
        ),
        f"- handoffs: {len(history)}",
        "",
        "Handoff history:",
    ]
    if not history:
        lines.append("n/a")
        return lines
    for index, item in enumerate(history, 1):
        if isinstance(item, Mapping):
            attempted = item.get("attempted_to")
            executed = item.get("executed_to") or item.get("to", "n/a")
            source = item.get("source", "n/a")
            if attempted and attempted != executed:
                lines.append(
                    f"{index}. attempted {attempted} -> executed {executed} ({source})"
                )
            else:
                lines.append(
                    f"{index}. {item.get('from', 'supervisor')} -> {executed} ({source})"
                )
    return lines


def print_workflow_snapshot(snapshot: Any) -> None:
    values = snapshot.values
    print(f"Thread: {snapshot.thread_id}")
    print(f"Checkpoint: {snapshot.checkpoint_id or 'n/a'}")
    print(f"Project: {values.get('created_project_name') or values.get('project_name') or 'n/a'}")
    print(f"Intent: {values.get('workflow_intent') or 'n/a'}")
    print(f"Terminal status: {values.get('terminal_status') or 'pending'}")
    print(f"Current/next nodes: {list(snapshot.next_nodes)}")
    print()
    for line in format_planning_result(values):
        print(line)
    print()
    for line in format_implementation_result(values):
        print(line)
    print()
    for line in format_testing_result(values):
        print(line)
    print()
    for line in format_supervisor_result(values):
        print(line)
    print()
    print(f"Interrupted: {str(bool(snapshot.interrupts)).lower()}")
    if values.get("terminal_status") == "user_cancelled":
        print("Pending tool: None")
        print("Pending operation: None")
        print(f"Last rejected tool: {values.get('last_rejected_tool') or 'None'}")
        print(f"Reason: {values.get('approval_reason') or 'None'}")
    else:
        print(f"Pending tool: {values.get('pending_tool_name') or 'n/a'}")
        print(f"Pending operation: {values.get('pending_operation') or 'n/a'}")
        print(f"Preview: {values.get('pending_approval_preview') or {}}")


def print_workflow_history(thread_id: str, history: list[Any]) -> None:
    print(f"Thread: {thread_id}")
    print(f"Checkpoints: {len(history)}")
    for item in history:
        print(
            f"- step={item.step} checkpoint={item.checkpoint_id or 'n/a'} "
            f"ns={item.checkpoint_namespace or 'root'} source={item.source or 'n/a'} "
            f"lineage={item.lineage} next={list(item.next_nodes)} "
            f"terminal={item.terminal_status or 'pending'} project={item.project_name or 'n/a'} "
            f"tests_passed={str(item.tests_passed).lower()} repair={item.repair_phase or 'n/a'} "
            f"origin={item.fork_origin_checkpoint_id or 'n/a'} updated={list(item.fork_updated_fields)} "
            f"result={item.result or 'n/a'} reason={item.approval_reason or 'n/a'} "
            f"last_rejected_tool={item.last_rejected_tool or 'n/a'} "
            f"writes={item.node_writes}"
        )


def print_checkpoint_details(details: Any) -> None:
    values = details.values
    print(f"Thread: {details.thread_id}")
    print(f"Checkpoint: {details.checkpoint_id}")
    print(f"Step: {details.step if details.step is not None else 'n/a'}")
    print(f"Source: {details.source or 'n/a'}")
    print(f"Next nodes: {list(details.next_nodes)}")
    print(f"Interrupted: {str(details.interrupted).lower()}")
    print(f"Project: {values.get('created_project_name') or values.get('project_name') or 'n/a'}")
    print(f"Terminal status: {values.get('terminal_status') or 'pending'}")
    print(f"Repair phase: {values.get('repair_phase') or 'n/a'}")


def print_replay_plan(plan: Any) -> None:
    print(f"Checkpoint: {plan.checkpoint_id}")
    print(f"Next nodes: {list(plan.next_nodes)}")
    print(f"Nodes that may reexecute: {list(plan.nodes_that_may_reexecute)}")
    print(f"Sensitive operations: {list(plan.sensitive_operations)}")
    print(f"Allowed: {str(plan.allowed).lower()}")
    if plan.rejection_reason:
        print(f"Reason: {plan.rejection_reason}")


def print_fork_preview(preview: Any) -> None:
    print(f"Origin checkpoint: {preview.origin_checkpoint_id}")
    print(f"Current values: {preview.current_values}")
    print(f"New values: {preview.new_values}")
    print(f"Allowed fields: {list(preview.allowed_fields)}")
    print(f"Rejected fields: {list(preview.rejected_fields)}")
    print(f"as_node: {preview.as_node or 'ambiguous'}")
    print(f"Expected next nodes: {list(preview.next_nodes)}")
    print(f"allowed={str(preview.allowed).lower()}")
    print(f"reason={preview.reason or 'n/a'}")
    print(f"disallowed_fields={list(preview.disallowed_fields)}")
    print(f"Domain: {preview.domain or 'unknown'}")


def print_fork_result(result: Any) -> None:
    print(f"Origin checkpoint: {result.origin_checkpoint_id}")
    print(f"Fork checkpoint: {result.fork_checkpoint_id}")
    print(f"Updated fields: {list(result.updated_fields)}")
    print(f"Next nodes: {list(result.next_nodes)}")
    if result.run_result is not None:
        print_workflow_run_result(result.run_result)


def print_workflow_run_result(result: Any) -> None:
    from graph.runtime import print_interrupt_request

    print(f"Workflow thread: {result.thread_id}")
    if result.interrupted:
        for workflow_interrupt in result.interrupts:
            print_interrupt_request(result.thread_id, workflow_interrupt)
    elif result.final_state.get("final_response"):
        print(result.final_state["final_response"])
    print("Workflow guardado.")
    print(f"Consultar: /workflow {result.thread_id}")


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    load_dotenv()
    import os

    api_key = os.getenv("OPENAI_API_KEY")
    model_name = os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY no estÃ¡ configurada. Copia .env.example a .env y completa la clave.")

    manager = MCPClientManager(build_clients(), SENSITIVE_TOOLS)
    async with AsyncExitStack() as exit_stack:
      try:
        await manager.connect_all()
        resource_context = await load_planning_resources(manager)
        instructions = BASE_INSTRUCTIONS
        if resource_context:
            instructions += "\n\nAvailable MCP resources:\n" + resource_context
        openai_client = AsyncOpenAI(api_key=api_key)
        host = SoftwareFactoryHost(manager, openai_client, model_name, instructions)
        use_langgraph = env_flag("USE_LANGGRAPH")
        langgraph_runtime = None
        persistence_service = None
        workflow_streaming_enabled = env_flag("WORKFLOW_STREAMING_ENABLED")
        stream_subscriptions: set[str] = set()
        stream_emitter = None
        stream_consumer = None
        if use_langgraph:
            from graph.builder import build_software_factory_graph
            from graph.checkpointing import create_sqlite_checkpointer
            from graph.nodes import GraphDependencies
            from graph.persistence_service import WorkflowPersistenceService
            from graph.runtime import run_software_factory_graph
            from streaming import default_event_emitter
            from streaming.consumer import TerminalWorkflowEventConsumer
            from tool_executor import HostToolExecutor

            dependencies = GraphDependencies(
                tool_executor=HostToolExecutor(host),
                openai_client=openai_client,
                model=model_name,
                instructions=instructions,
            )
            checkpointer = await exit_stack.enter_async_context(create_sqlite_checkpointer())
            graph = build_software_factory_graph(dependencies, checkpointer=checkpointer)
            langgraph_runtime = (graph, run_software_factory_graph)
            stream_emitter = default_event_emitter
            stream_consumer = TerminalWorkflowEventConsumer(debug=env_flag("MCP_FACTORY_DEBUG"))
            persistence_service = WorkflowPersistenceService(
                graph,
                streaming_enabled=workflow_streaming_enabled,
                event_emitter=stream_emitter,
            )
            print("Runtime de orquestación: LangGraph con persistence SQLite e interrupts durables.")
        else:
            print("Runtime de orquestación: manual.")

        def ensure_stream_subscription(thread_id: str) -> None:
            if (
                workflow_streaming_enabled
                and stream_emitter is not None
                and stream_consumer is not None
                and thread_id not in stream_subscriptions
            ):
                stream_emitter.subscribe(thread_id, stream_consumer)
                stream_subscriptions.add(thread_id)

        print("\nMCP Software Factory lista. Escribe una solicitud, /prompts, /plan-project ..., o salir.")
        while True:
            raw = input("\n> ").strip()
            if raw.lower() in {"salir", "exit", "quit"}:
                break
            if not raw:
                continue
            try:
                if raw.lower() in {"/stream on", "/stream off"}:
                    workflow_streaming_enabled = raw.lower().endswith("on")
                    if persistence_service is not None:
                        persistence_service.streaming_enabled = workflow_streaming_enabled
                    print(f"Workflow streaming: {'on' if workflow_streaming_enabled else 'off'}")
                    continue
                if raw == "/prompts":
                    prompts = await manager.list_all_prompts()
                    print(json.dumps(prompts, indent=2, ensure_ascii=False))
                    continue
                if raw.startswith("/workflow-history"):
                    if persistence_service is None:
                        print("Los workflows persistidos requieren USE_LANGGRAPH=true.")
                        continue
                    thread_id = command_argument(raw, "/workflow-history")
                    print_workflow_history(thread_id, await persistence_service.get_history(thread_id))
                    continue
                if raw.startswith("/workflow-checkpoint"):
                    if persistence_service is None:
                        print("La inspección de checkpoints requiere USE_LANGGRAPH=true.")
                        continue
                    thread_id, checkpoint_id = command_thread_checkpoint(raw, "/workflow-checkpoint")
                    print_checkpoint_details(await persistence_service.get_checkpoint(thread_id, checkpoint_id))
                    continue
                if raw.startswith("/workflow-replay-plan"):
                    if persistence_service is None:
                        print("El replay requiere USE_LANGGRAPH=true.")
                        continue
                    thread_id, checkpoint_id = command_thread_checkpoint(raw, "/workflow-replay-plan")
                    print_replay_plan(await persistence_service.build_replay_plan(thread_id, checkpoint_id))
                    continue
                if raw.startswith("/workflow-replay"):
                    if persistence_service is None:
                        print("El replay requiere USE_LANGGRAPH=true.")
                        continue
                    thread_id, checkpoint_id = command_thread_checkpoint(raw, "/workflow-replay")
                    ensure_stream_subscription(thread_id)
                    plan = await persistence_service.build_replay_plan(thread_id, checkpoint_id)
                    print_replay_plan(plan)
                    host.reset_request_state()
                    print_workflow_run_result(await persistence_service.replay(thread_id, checkpoint_id))
                    continue
                if raw.startswith("/workflow-fork-plan"):
                    if persistence_service is None:
                        print("Los forks requieren USE_LANGGRAPH=true.")
                        continue
                    thread_id, checkpoint_id, updates_value = command_fork_arguments(raw, "/workflow-fork-plan")
                    updates = load_fork_updates(updates_value)
                    print_fork_preview(await persistence_service.preview_fork(thread_id, checkpoint_id, updates))
                    continue
                if raw.startswith("/workflow-fork-preview"):
                    if persistence_service is None:
                        print("Los forks requieren USE_LANGGRAPH=true.")
                        continue
                    thread_id, checkpoint_id, updates_value = command_fork_arguments(raw, "/workflow-fork-preview")
                    updates = load_fork_updates(updates_value)
                    print_fork_preview(await persistence_service.preview_fork(thread_id, checkpoint_id, updates))
                    continue
                if raw.startswith("/workflow-fork"):
                    if persistence_service is None:
                        print("Los forks requieren USE_LANGGRAPH=true.")
                        continue
                    thread_id, checkpoint_id, updates_value = command_fork_arguments(raw, "/workflow-fork")
                    ensure_stream_subscription(thread_id)
                    updates = load_fork_updates(updates_value)
                    preview = await persistence_service.preview_fork(thread_id, checkpoint_id, updates)
                    if not preview.allowed:
                        print_fork_preview(preview)
                        continue
                    host.reset_request_state()
                    print_fork_result(await persistence_service.fork(thread_id, checkpoint_id, updates))
                    continue
                if raw.startswith("/workflow-approve"):
                    if persistence_service is None:
                        print("La aprobación durable requiere USE_LANGGRAPH=true.")
                        continue
                    thread_id, reason = command_thread_and_reason(raw, "/workflow-approve")
                    ensure_stream_subscription(thread_id)
                    host.reset_request_state()
                    print_workflow_run_result(await persistence_service.approve(thread_id, reason))
                    continue
                if raw.startswith("/workflow-reject"):
                    if persistence_service is None:
                        print("El rechazo durable requiere USE_LANGGRAPH=true.")
                        continue
                    thread_id, reason = command_thread_and_reason(raw, "/workflow-reject")
                    ensure_stream_subscription(thread_id)
                    host.reset_request_state()
                    print_workflow_run_result(await persistence_service.reject(thread_id, reason))
                    continue
                if raw.startswith("/workflow-pending"):
                    if persistence_service is None:
                        print("La consulta de aprobaciones requiere USE_LANGGRAPH=true.")
                        continue
                    thread_id = command_argument(raw, "/workflow-pending")
                    pending = await persistence_service.get_pending_interrupts(thread_id)
                    if not pending:
                        print("El workflow no tiene aprobaciones pendientes.")
                    else:
                        from graph.runtime import print_interrupt_request

                        for workflow_interrupt in pending:
                            print_interrupt_request(thread_id, workflow_interrupt)
                    continue
                if raw.startswith("/workflow-resume"):
                    if persistence_service is None:
                        print("La reanudación requiere USE_LANGGRAPH=true.")
                        continue
                    thread_id = command_argument(raw, "/workflow-resume")
                    ensure_stream_subscription(thread_id)
                    host.reset_request_state()
                    result = await persistence_service.resume(thread_id)
                    if result.interrupted:
                        print("El workflow espera una decisión humana.")
                        print("Usa /workflow-approve o /workflow-reject.")
                    elif result.already_completed:
                        print("Workflow ya completado; no se volvió a ejecutar.")
                    else:
                        print("Workflow reanudado desde el último checkpoint.")
                    print_workflow_run_result(result)
                    continue
                if raw.startswith("/workflow"):
                    if persistence_service is None:
                        print("La consulta de workflows requiere USE_LANGGRAPH=true.")
                        continue
                    thread_id = command_argument(raw, "/workflow")
                    print_workflow_snapshot(await persistence_service.get_snapshot(thread_id))
                    continue
                if raw.startswith("/plan-project"):
                    initial_input = await handle_plan_project_command(manager, raw)
                else:
                    initial_input = user_message(raw)
                host.reset_request_state()
                if langgraph_runtime is not None and not raw.startswith("/plan-project"):
                    graph, run_graph = langgraph_runtime
                    from graph.runtime import create_thread_id

                    workflow_thread_id = create_thread_id()
                    if (
                        workflow_streaming_enabled
                        and stream_emitter is not None
                        and stream_consumer is not None
                    ):
                        stream_emitter.subscribe(workflow_thread_id, stream_consumer)
                        stream_subscriptions.add(workflow_thread_id)
                    await run_graph(
                        graph,
                        raw,
                        thread_id=workflow_thread_id,
                        streaming_enabled=workflow_streaming_enabled,
                        event_emitter=stream_emitter,
                    )
                else:
                    print(await host.run_openai_loop(initial_input))
            except Exception as exc:
                if isinstance(
                    exc,
                    (
                        WorkflowNotFoundError,
                        WorkflowAlreadyCompletedError,
                        WorkflowNotInterruptedError,
                        CheckpointNotFoundError,
                        CheckpointThreadMismatchError,
                        UnsafeReplayError,
                        InvalidForkUpdateError,
                        AmbiguousForkNodeError,
                    ),
                ):
                    print(str(exc))
                else:
                    print(f"Error procesando solicitud: {exc}")
      finally:
        await manager.disconnect_all()


if __name__ == "__main__":
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 o superior es requerido.")
    asyncio.run(main())
