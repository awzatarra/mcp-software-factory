from __future__ import annotations

import json
from typing import Any

import pytest

from clients.mcp_client import MCPToolTimeout
from clients.tool_registry import RegisteredTool
from clients.tool_registry import ToolRegistry
from host import (
    DEFAULT_FASTAPI_REQUIREMENT,
    DEFAULT_PYTEST_REQUIREMENT,
    ExecutionState,
    FailureType,
    MAX_REPAIR_ATTEMPTS,
    SoftwareFactoryHost,
    build_terminal_summary,
    build_base_instructions,
    determine_next_action,
    extract_failing_test_files,
    extract_failure_summary,
    extract_requested_project_name,
    extract_workflow_intent,
    get_allowed_tools,
    normalize_mcp_result,
    normalize_project_relative_path,
    user_message,
)


class FakeFunctionCall:
    type = "function_call"

    def __init__(self, name: str, call_id: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.call_id = call_id
        self.arguments = json.dumps(arguments)

    def model_dump(self, exclude_none: bool = True) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "call_id": self.call_id,
            "arguments": self.arguments,
        }


class FakeTextContent:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeMessage:
    type = "message"

    def __init__(self, text: str) -> None:
        self.content = [FakeTextContent(text)]

    def model_dump(self, exclude_none: bool = True) -> dict[str, Any]:
        return {
            "type": self.type,
            "content": [{"type": "output_text", "text": self.content[0].text}],
        }


class FakeResponse:
    def __init__(self, output: list[Any], output_text: str | None = None) -> None:
        self.output = output
        self.output_text = output_text


class FakeResponsesApi:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("no fake response queued")
        return self._responses.pop(0)


class FakeOpenAIClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = FakeResponsesApi(responses)


class RecordingToolClient:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeCallToolResult:
    def __init__(self, structured_content: dict[str, Any] | None = None, content: list[Any] | None = None, is_error: bool = False) -> None:
        self.structuredContent = structured_content
        self.content = content or []
        self.isError = is_error


class ApprovalHost(SoftwareFactoryHost):
    def __init__(self, *args: Any, approvals: list[bool] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.approvals = approvals or [True]
        self.approval_requests: list[tuple[str, dict[str, Any]]] = []

    async def request_approval(self, tool, arguments: dict[str, Any]) -> bool:
        self.approval_requests.append((tool.public_name, arguments))
        return self.approvals.pop(0) if self.approvals else True


class FakeManager:
    def __init__(self) -> None:
        self.registry = ToolRegistry()


def decode_function_output(output: dict[str, Any]) -> dict[str, Any]:
    decoded = json.loads(output["output"])
    data = decoded.get("data")
    return data if isinstance(data, dict) else decoded


def register_tool(manager: FakeManager, server_name: str, original_name: str, client: RecordingToolClient, approval: bool):
    return manager.registry.register(
        server_name=server_name,
        original_name=original_name,
        description=original_name,
        input_schema={"type": "object", "properties": {}},
        requires_approval=approval,
        client=client,
    )


def register_workflow_tools(manager: FakeManager, client: RecordingToolClient | None = None) -> RecordingToolClient:
    shared = client or RecordingToolClient({"success": True})
    register_tool(manager, "software_factory", "analyze_requirement", shared, False)
    register_tool(manager, "software_factory", "create_tasks", shared, False)
    register_tool(manager, "filesystem", "list_files", shared, False)
    register_tool(manager, "filesystem", "read_file", shared, False)
    register_tool(manager, "filesystem", "create_project_structure", shared, True)
    register_tool(manager, "filesystem", "update_project_files", shared, True)
    register_tool(manager, "testing", "detect_test_framework", shared, False)
    register_tool(manager, "testing", "prepare_test_environment", shared, True)
    register_tool(manager, "testing", "run_tests", shared, True)
    return shared


@pytest.mark.asyncio
async def test_host_normalizes_mcp_tool_timeout() -> None:
    manager = FakeManager()
    client = RecordingToolClient(MCPToolTimeout("git", "init", 0.01))
    register_tool(manager, "git", "init", client, False)
    host = SoftwareFactoryHost(manager, FakeOpenAIClient([]), "test-model", "")
    host.state = ExecutionState(original_user_message="x")

    output, executed_sensitive = await host.handle_function_call(
        FakeFunctionCall("git__init", "call-timeout", {"project_id": "demo"})
    )
    decoded = json.loads(output["output"])
    payload = decoded["data"]

    assert executed_sensitive is False
    assert decoded["transport_success"] is False
    assert payload["success"] is False
    assert payload["status"] == "mcp_timeout"
    assert payload["server"] == "git"
    assert payload["tool"] == "init"
    assert payload["failure_type"] == "mcp_timeout"
    assert host.state.terminal_status == "infrastructure_failed"
    assert host.state.failure_type == "git_initialization_failed"
    assert host.state.failure_stage == "git.init"


@pytest.mark.asyncio
async def test_host_normalizes_testing_prepare_environment_timeout_as_mcp_timeout() -> None:
    manager = FakeManager()
    client = RecordingToolClient(
        MCPToolTimeout("testing", "prepare_test_environment", 120)
    )
    register_tool(manager, "testing", "prepare_test_environment", client, True)
    host = SoftwareFactoryHost(manager, FakeOpenAIClient([]), "test-model", "")
    host.state = ExecutionState(original_user_message="x")

    output, executed_sensitive = await host.handle_function_call(
        FakeFunctionCall(
            "testing__prepare_test_environment",
            "call-timeout",
            {"project_name": "demo"},
        ),
        approval_mode="already_approved",
    )
    decoded = json.loads(output["output"])
    payload = decoded["data"]

    assert executed_sensitive is True
    assert decoded["transport_success"] is False
    assert payload["status"] == "mcp_timeout"
    assert payload["failure_type"] == "mcp_timeout"
    assert payload["server"] == "testing"
    assert payload["tool"] == "prepare_test_environment"
    assert host.state.terminal_status == "infrastructure_failed"
    assert host.state.test_infrastructure_failed is True


def test_base_instructions_prevent_unnecessary_clarification_and_delegate_approval_to_host() -> None:
    instructions = build_base_instructions().lower()

    assert "do not ask again for information that is already present" in instructions
    assert "human approvals are the exclusive responsibility of the host code" in instructions
    assert "use minimal reasonable defaults" in instructions
    assert "use tools when the request has enough information" in instructions
    assert "do not ask the user to type yes" in instructions
    assert "never replace an explicit project name" in instructions
    assert "filesystem__create_project_structure must be used at most once" in instructions
    assert "every files[].path must be relative to the project root" in instructions
    assert "never include project_name as the first segment" in instructions


def test_base_instructions_use_configured_fastapi_dependency_policy() -> None:
    instructions = build_base_instructions()

    assert DEFAULT_FASTAPI_REQUIREMENT in instructions
    assert DEFAULT_PYTEST_REQUIREMENT in instructions
    assert "fastapi[standard]>=0.139.2,<0.140" not in instructions
    assert "Never invent package versions" in instructions


@pytest.mark.asyncio
async def test_invalid_tool_arguments_fail_before_approval_and_mcp_call() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True})
    manager.registry.register(
        server_name="testing",
        original_name="prepare_test_environment",
        description="Prepare environment",
        input_schema={
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
                "dependency_file": {"type": "string", "default": "requirements.txt"},
            },
            "required": ["project_name"],
        },
        requires_approval=True,
        client=client,
    )
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(original_user_message="prepara")

    output, executed_sensitive = await host.handle_function_call(
        FakeFunctionCall(
            "testing__prepare_test_environment",
            "call-prepare",
            {"project_name": "medical-booking", "timeout_seconds": 1200},
        )
    )
    result = decode_function_output(output)

    assert executed_sensitive is False
    assert result["status"] == FailureType.INVALID_TOOL_ARGUMENTS.value
    assert "timeout_seconds" in result["errors"][0]
    assert client.calls == []
    assert host.approval_requests == []
    assert host.state.test_infrastructure_failed is False


def test_build_terminal_summary_preserves_project_name() -> None:
    state = ExecutionState(
        original_user_message="x",
        project_created=True,
        created_project_name="medical-booking",
        failure_type=FailureType.INVALID_TOOL_ARGUMENTS.value,
        failure_stage="validate_arguments",
        failure_message="timeout_seconds must be between 1 and 900",
    )

    summary = build_terminal_summary(state)

    assert summary["project_created"] is True
    assert summary["project_name"] == "medical-booking"
    assert summary["environment_prepared"] is False
    assert summary["tests_executed"] is False


@pytest.mark.asyncio
async def test_finalization_includes_terminal_summary_and_no_infra_invention_rules() -> None:
    manager = FakeManager()
    openai_client = FakeOpenAIClient([FakeResponse([FakeMessage("Resumen factual.")], output_text="Resumen factual.")])
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions())
    host.state = ExecutionState(
        original_user_message="x",
        project_created=True,
        created_project_name="medical-booking",
        test_infrastructure_failed=True,
        failure_type="dependency_installation_failed",
        failure_stage="install_dependencies",
        failure_message="pip failed",
    )

    final_text = await host._finalize_after_terminal_state(user_message("x"))

    assert final_text == "Resumen factual."
    call = openai_client.responses.calls[-1]
    assert call["tools"] == []
    assert "Do not mention cloud, virtual machines" in call["instructions"]
    assert "Never say the project is unknown" in call["instructions"]
    summary_message = call["input"][-1]["content"]
    assert '"project_name": "medical-booking"' in summary_message
    assert '"project_created": true' in summary_message


def test_extract_requested_project_name_from_conservative_patterns() -> None:
    assert extract_requested_project_name("Crea un proyecto FastAPI llamado medical-booking") == "medical-booking"
    assert extract_requested_project_name("Crea una app llamada medical_booking") == "medical_booking"
    assert extract_requested_project_name("Usa nombre health-api") == "health-api"
    assert extract_requested_project_name("Crea un servicio sin nombre explicito") is None


def test_extract_workflow_intent_for_review_request_with_project_name() -> None:
    message = "Revisa el proyecto medical-booking, ejecuta sus pruebas y corrige los errores encontrados."

    assert extract_workflow_intent(message, "medical-booking") == "review_existing_project"
    assert extract_workflow_intent("Crea un proyecto llamado medical-booking", "medical-booking") == "create_project"


def test_extract_failing_test_files_normalizes_windows_and_posix_paths() -> None:
    assert extract_failing_test_files("tests\\test_health.py:9: AssertionError") == ["tests/test_health.py"]
    assert extract_failing_test_files("tests/test_health.py:9: AssertionError") == ["tests/test_health.py"]


def test_normalize_project_relative_path_prefixes_project_for_posix_and_windows() -> None:
    assert normalize_project_relative_path("medical-booking", "tests/test_health.py") == "medical-booking/tests/test_health.py"
    assert normalize_project_relative_path("medical-booking", "tests\\test_health.py") == "medical-booking/tests/test_health.py"
    assert normalize_project_relative_path("medical-booking", "medical_booking/main.py") == "medical-booking/medical_booking/main.py"


def test_normalize_project_relative_path_does_not_duplicate_prefix() -> None:
    assert normalize_project_relative_path("medical-booking", "medical-booking/README.md") == "medical-booking/README.md"


def test_normalize_project_relative_path_rejects_traversal_and_absolute_paths() -> None:
    with pytest.raises(ValueError, match="traversal"):
        normalize_project_relative_path("medical-booking", "../tests/test_health.py")
    with pytest.raises(ValueError, match="absolute"):
        normalize_project_relative_path("medical-booking", "C:/tmp/test_health.py")


def test_extract_failure_summary_for_health_status_mismatch() -> None:
    stdout = """
tests/test_health.py:9: AssertionError
E       AssertionError: assert {'status': 'ok'} == {'status': 'healthy'}
"""

    summary = extract_failure_summary(stdout)

    assert 'tests/test_health.py:9 esperaba {"status": "healthy"}, pero el endpoint devolvió {"status": "ok"}.' == summary


def test_determine_next_action_base_workflow() -> None:
    assert determine_next_action(ExecutionState(original_user_message="x")) == "software_factory__analyze_requirement"
    assert determine_next_action(ExecutionState(original_user_message="x", analysis_completed=True)) == "software_factory__create_tasks"
    assert (
        determine_next_action(ExecutionState(original_user_message="x", analysis_completed=True, tasks_created=True))
        == "filesystem__list_files"
    )
    assert (
        determine_next_action(
            ExecutionState(original_user_message="x", analysis_completed=True, tasks_created=True, workspace_inspected=True)
        )
        == "filesystem__create_project_structure"
    )
    assert (
        determine_next_action(
            ExecutionState(
                original_user_message="x",
                analysis_completed=True,
                tasks_created=True,
                workspace_inspected=True,
                project_created=True,
            )
        )
        == "testing__detect_test_framework"
    )
    assert (
        determine_next_action(
            ExecutionState(
                original_user_message="x",
                analysis_completed=True,
                tasks_created=True,
                workspace_inspected=True,
                project_created=True,
                detected_test_framework="pytest",
            )
        )
        == "testing__prepare_test_environment"
    )
    assert (
        determine_next_action(
            ExecutionState(
                original_user_message="x",
                analysis_completed=True,
                tasks_created=True,
                workspace_inspected=True,
                project_created=True,
                detected_test_framework="pytest",
                environment_prepared=True,
            )
        )
        == "testing__run_tests"
    )
    assert determine_next_action(ExecutionState(original_user_message="x", tests_passed=True)) == "finalize"


def test_review_existing_project_starts_with_list_files_and_skips_creation_after_found() -> None:
    state = ExecutionState(
        original_user_message="Revisa el proyecto medical-booking",
        requested_project_name="medical-booking",
        workflow_intent="review_existing_project",
    )
    assert determine_next_action(state) == "filesystem__list_files"

    state.workspace_inspected = True
    state.project_exists = True
    state.created_project_name = "medical-booking"

    assert determine_next_action(state) == "testing__detect_test_framework"


@pytest.mark.asyncio
async def test_project_existing_is_registered_from_list_files() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"entries": [{"path": "medical-booking", "type": "directory"}]})
    register_tool(manager, "filesystem", "list_files", client, False)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(
        original_user_message="Revisa el proyecto medical-booking",
        requested_project_name="medical-booking",
        workflow_intent="review_existing_project",
    )

    await host.handle_function_call(FakeFunctionCall("filesystem__list_files", "call-list", {"relative_path": "."}))

    assert host.state.project_exists is True
    assert host.state.created_project_name == "medical-booking"
    assert determine_next_action(host.state) == "testing__detect_test_framework"


@pytest.mark.asyncio
async def test_review_absent_project_does_not_offer_create_project_structure() -> None:
    manager = FakeManager()
    register_workflow_tools(manager)
    client = manager.registry.get("filesystem__list_files").client
    client.result = {"entries": []}
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(
        original_user_message="Revisa el proyecto medical-booking",
        requested_project_name="medical-booking",
        workflow_intent="review_existing_project",
    )

    await host.handle_function_call(FakeFunctionCall("filesystem__list_files", "call-list", {"relative_path": "."}))

    assert host.state.project_exists is False
    assert host.state.terminal_status == "project_not_found"
    assert determine_next_action(host.state) == "finalize"
    assert "filesystem__create_project_structure" not in {tool["name"] for tool in host.filtered_openai_tools(host.state)}


def test_get_allowed_tools_for_functional_failure_only_allows_fix_tools() -> None:
    manager = FakeManager()
    register_workflow_tools(manager)
    state = ExecutionState(
        original_user_message="x",
        analysis_completed=True,
        tasks_created=True,
        workspace_inspected=True,
        project_created=True,
        detected_test_framework="pytest",
        environment_prepared=True,
        tests_executed=True,
        tests_passed=False,
        failing_test_files=["tests/test_health.py"],
        repair_phase="read_failing_test",
    )

    names = {tool.public_name for tool in get_allowed_tools(state, manager.registry)}

    assert determine_next_action(state) == "fix_failed_tests"
    assert names == {"filesystem__read_file"}


def test_infrastructure_failure_allows_no_tools() -> None:
    manager = FakeManager()
    register_workflow_tools(manager)
    state = ExecutionState(original_user_message="x", test_infrastructure_failed=True)

    assert determine_next_action(state) == "finalize"
    assert get_allowed_tools(state, manager.registry) == []


@pytest.mark.asyncio
async def test_single_allowed_tool_uses_forced_tool_choice() -> None:
    manager = FakeManager()
    register_workflow_tools(manager)
    openai_client = FakeOpenAIClient([FakeResponse([FakeFunctionCall("software_factory__analyze_requirement", "call-analyze", {"requirement": "x"})])])
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions())

    with pytest.raises(AssertionError, match="no fake response queued"):
        await host.run_openai_loop(user_message("x"))

    assert openai_client.responses.calls[0]["tool_choice"] == {
        "type": "function",
        "name": "software_factory__analyze_requirement",
    }


@pytest.mark.asyncio
async def test_fix_failed_tests_uses_forced_repair_tool_choice() -> None:
    manager = FakeManager()
    register_workflow_tools(manager)
    openai_client = FakeOpenAIClient([FakeResponse([FakeMessage("Final.")], output_text="Final.")])
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions())
    host.state = ExecutionState(
        original_user_message="x",
        analysis_completed=True,
        tasks_created=True,
        workspace_inspected=True,
        project_created=True,
        detected_test_framework="pytest",
        environment_prepared=True,
        tests_executed=True,
        tests_passed=False,
        failing_test_files=["tests/test_health.py"],
        repair_phase="read_failing_test",
    )

    allowed = get_allowed_tools(host.state, manager.registry)

    assert [tool.public_name for tool in allowed] == ["filesystem__read_file"]
    assert host.tool_choice_for_allowed(allowed) == {
        "type": "function",
        "name": "filesystem__read_file",
    }
    host.log_workflow_decision(host.state, allowed, forced_tool_choice=True)
    assert host.openai_tools_from_registered(allowed)


@pytest.mark.asyncio
async def test_premature_text_response_does_not_finalize_when_tool_is_pending() -> None:
    manager = FakeManager()
    analyze_client = RecordingToolClient({"requirement": "x"})
    register_tool(manager, "software_factory", "analyze_requirement", analyze_client, False)
    openai_client = FakeOpenAIClient(
        [
            FakeResponse([FakeMessage("I can do that.")], output_text="I can do that."),
            FakeResponse([FakeFunctionCall("software_factory__analyze_requirement", "call-analyze", {"requirement": "x"})]),
            FakeResponse([FakeMessage("Final.")], output_text="Final."),
        ]
    )
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions())

    final_text = await host.run_openai_loop(user_message("x"))

    assert final_text == "Final."
    assert analyze_client.calls == [("analyze_requirement", {"requirement": "x"})]
    assert len(openai_client.responses.calls) == 3


@pytest.mark.asyncio
async def test_list_files_empty_relative_path_is_normalized_to_dot() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"entries": []})
    register_tool(manager, "filesystem", "list_files", client, False)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(original_user_message="x")

    await host.handle_function_call(FakeFunctionCall("filesystem__list_files", "call-list", {"relative_path": ""}))

    assert client.calls == [("list_files", {"relative_path": "."})]


@pytest.mark.asyncio
async def test_host_tool_loop_executes_function_calls_and_preserves_call_ids() -> None:
    manager = FakeManager()
    analyze_client = RecordingToolClient({"requirement": "medical-booking"})
    tasks_client = RecordingToolClient({"tasks": [{"order": 1, "agent": "business analyst", "task": "Analyze"}]})
    manager.registry.register(
        server_name="software_factory",
        original_name="analyze_requirement",
        description="Analyze requirement",
        input_schema={"type": "object", "properties": {"requirement": {"type": "string"}}},
        requires_approval=False,
        client=analyze_client,
    )
    manager.registry.register(
        server_name="software_factory",
        original_name="create_tasks",
        description="Create tasks",
        input_schema={"type": "object", "properties": {"project_type": {"type": "string"}, "backend": {"type": "string"}}},
        requires_approval=False,
        client=tasks_client,
    )
    openai_client = FakeOpenAIClient(
        [
            FakeResponse(
                [
                    FakeFunctionCall(
                        "software_factory__analyze_requirement",
                        "call-analyze",
                        {"requirement": "Crea medical-booking"},
                    )
                ]
            ),
            FakeResponse(
                [
                    FakeFunctionCall(
                        "software_factory__create_tasks",
                        "call-tasks",
                        {"project_type": "medical-booking", "backend": "fastapi"},
                    )
                ]
            ),
            FakeResponse([FakeMessage("Flujo completado.")], output_text="Flujo completado."),
        ]
    )
    host = SoftwareFactoryHost(manager, openai_client, "test-model", build_base_instructions())

    final_text = await host.run_openai_loop(user_message("Crea medical-booking"))

    assert final_text == "Flujo completado."
    assert analyze_client.calls == [("analyze_requirement", {"requirement": "Crea medical-booking"})]
    assert tasks_client.calls == [("create_tasks", {"project_type": "medical-booking", "backend": "fastapi"})]
    assert openai_client.responses.calls[0]["tool_choice"] == {
        "type": "function",
        "name": "software_factory__analyze_requirement",
    }
    assert openai_client.responses.calls[1]["input"][-1]["type"] == "function_call_output"
    assert openai_client.responses.calls[1]["input"][-1]["call_id"] == "call-analyze"
    assert openai_client.responses.calls[2]["input"][-2]["type"] == "function_call_output"
    assert openai_client.responses.calls[2]["input"][-2]["call_id"] == "call-tasks"


def test_structured_content_normalization_updates_project_state() -> None:
    result = normalize_mcp_result(
        FakeCallToolResult(
            {
                "success": True,
                "project_name": "medical-booking",
                "project_path": "medical-booking",
                "total_files": 6,
            }
        )
    )
    manager = FakeManager()
    client = RecordingToolClient({})
    tool = register_tool(manager, "filesystem", "create_project_structure", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(original_user_message="Crea un proyecto llamado medical-booking")

    host.update_state_after_tool(tool, {"project_name": "medical-booking"}, result)

    assert host.state.project_created is True
    assert host.state.created_project_name == "medical-booking"


def test_wrapped_data_normalization_updates_project_state() -> None:
    result = normalize_mcp_result(
        {
            "success": True,
            "data": {
                "success": True,
                "project_name": "medical-booking",
            },
        }
    )
    manager = FakeManager()
    client = RecordingToolClient({})
    tool = register_tool(manager, "filesystem", "create_project_structure", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(original_user_message="Crea un proyecto llamado medical-booking")

    host.update_state_after_tool(tool, {"project_name": "fallback"}, result)

    assert host.state.project_created is True
    assert host.state.created_project_name == "medical-booking"


def test_update_state_uses_server_name_and_original_name() -> None:
    manager = FakeManager()
    tool = RegisteredTool(
        public_name="unexpected_public_name",
        original_name="analyze_requirement",
        server_name="software_factory",
        description="",
        input_schema={},
        requires_approval=False,
        client=RecordingToolClient({}),
    )
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(original_user_message="analiza")

    host.update_state_after_tool(tool, {}, normalize_mcp_result({"requirement": "x"}))

    assert host.state.analysis_completed is True


@pytest.mark.asyncio
async def test_analyze_create_tasks_and_list_files_update_stage_flags_without_success_field() -> None:
    manager = FakeManager()
    analyze_client = RecordingToolClient({"requirement": "x"})
    tasks_client = RecordingToolClient([{"order": 1}])
    list_client = RecordingToolClient({"entries": []})
    register_tool(manager, "software_factory", "analyze_requirement", analyze_client, False)
    register_tool(manager, "software_factory", "create_tasks", tasks_client, False)
    register_tool(manager, "filesystem", "list_files", list_client, False)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(original_user_message="x")

    await host.handle_function_call(FakeFunctionCall("software_factory__analyze_requirement", "call-1", {"requirement": "x"}))
    await host.handle_function_call(FakeFunctionCall("software_factory__create_tasks", "call-2", {"project_type": "x"}))
    await host.handle_function_call(FakeFunctionCall("filesystem__list_files", "call-3", {"relative_path": "."}))

    assert host.state.analysis_completed is True
    assert host.state.tasks_created is True
    assert host.state.workspace_inspected is True


@pytest.mark.asyncio
async def test_create_project_name_conflict_blocks_mcp_and_approval() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True})
    register_tool(manager, "filesystem", "create_project_structure", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(
        original_user_message="Crea un proyecto FastAPI llamado medical-booking",
        requested_project_name="medical-booking",
    )

    output, executed_sensitive = await host.handle_function_call(
        FakeFunctionCall(
            "filesystem__create_project_structure",
            "call-create",
            {"project_name": "fastapi_minimal", "files": [{"path": "README.md", "content": ""}]},
        )
    )

    result = decode_function_output(output)
    assert executed_sensitive is False
    assert result["status"] == "argument_conflict"
    assert result["expected"] == "medical-booking"
    assert result["received"] == "fastapi_minimal"
    assert client.calls == []
    assert host.approval_requests == []


@pytest.mark.asyncio
async def test_create_project_structure_rejects_unconfigured_fastapi_version_before_approval() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True})
    register_tool(manager, "filesystem", "create_project_structure", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(original_user_message="Crea un proyecto llamado medical-booking", requested_project_name="medical-booking")

    output, executed_sensitive = await host.handle_function_call(
        FakeFunctionCall(
            "filesystem__create_project_structure",
            "call-create",
            {
                "project_name": "medical-booking",
                "files": [{"path": "requirements.txt", "content": "fastapi[standard]>=0.139.2,<0.140\npytest>=8,<9\n"}],
            },
        )
    )
    result = decode_function_output(output)

    assert executed_sensitive is False
    assert result["status"] == "dependency_policy_violation"
    assert result["expected"] == DEFAULT_FASTAPI_REQUIREMENT
    assert result["received"] == "fastapi[standard]>=0.139.2,<0.140"
    assert client.calls == []
    assert host.approval_requests == []


@pytest.mark.asyncio
async def test_create_project_guard_accepts_canonical_optional_dependencies() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True, "project_name": "medical-booking"})
    register_tool(manager, "filesystem", "create_project_structure", client, True)
    host = ApprovalHost(
        manager,
        FakeOpenAIClient([]),
        "test-model",
        build_base_instructions(),
        approvals=[True],
    )
    host.state = ExecutionState(
        original_user_message="Crea un proyecto llamado medical-booking",
        requested_project_name="medical-booking",
    )
    requirements = (
        "fastapi[standard]==0.139.0\n"
        "pytest>=8,<9\n"
        "httpx>=0.23,<1\n"
        "uvicorn>=0.17,<1\n"
    )

    output, executed_sensitive = await host.handle_function_call(
        FakeFunctionCall(
            "filesystem__create_project_structure",
            "call-create",
            {
                "project_name": "medical-booking",
                "files": [{"path": "requirements.txt", "content": requirements}],
            },
        )
    )

    assert executed_sensitive is True
    assert decode_function_output(output)["success"] is True
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_create_project_structure_rejects_empty_files_before_approval() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True})
    register_tool(manager, "filesystem", "create_project_structure", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(original_user_message="Crea un proyecto llamado medical-booking", requested_project_name="medical-booking")

    output, executed_sensitive = await host.handle_function_call(
        FakeFunctionCall("filesystem__create_project_structure", "call-create", {"project_name": "medical-booking", "files": []})
    )
    result = decode_function_output(output)

    assert executed_sensitive is False
    assert result["status"] == FailureType.INVALID_TOOL_ARGUMENTS.value
    assert result["message"] == "create_project_structure requiere al menos un archivo."
    assert client.calls == []
    assert host.approval_requests == []


@pytest.mark.asyncio
async def test_create_project_structure_blocks_second_creation_in_same_run() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True, "project_name": "medical-booking"})
    register_tool(manager, "filesystem", "create_project_structure", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(
        original_user_message="Crea un proyecto FastAPI llamado medical-booking",
        requested_project_name="medical-booking",
    )

    first, first_sensitive = await host.handle_function_call(
        FakeFunctionCall(
            "filesystem__create_project_structure",
            "call-create-1",
            {"project_name": "medical-booking", "files": [{"path": "README.md", "content": ""}]},
        )
    )
    second, second_sensitive = await host.handle_function_call(
        FakeFunctionCall(
            "filesystem__create_project_structure",
            "call-create-2",
            {"project_name": "medical-booking", "files": [{"path": "README.md", "content": ""}]},
        )
    )

    assert decode_function_output(first)["success"] is True
    assert first_sensitive is True
    assert decode_function_output(second)["status"] == "project_already_created_in_current_run"
    assert second_sensitive is False
    assert client.calls == [("create_project_structure", {"project_name": "medical-booking", "files": [{"path": "README.md", "content": ""}]})]
    assert len(host.approval_requests) == 1


@pytest.mark.asyncio
async def test_project_already_exists_error_is_reconciled_and_blocks_second_creation() -> None:
    manager = FakeManager()
    client = RecordingToolClient(FileExistsError("project already exists: medical-booking"))
    register_tool(manager, "filesystem", "create_project_structure", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(
        original_user_message="Crea un proyecto FastAPI llamado medical-booking",
        requested_project_name="medical-booking",
    )

    first, _ = await host.handle_function_call(
        FakeFunctionCall("filesystem__create_project_structure", "call-1", {"project_name": "medical-booking", "files": [{"path": "README.md", "content": ""}]})
    )
    second, _ = await host.handle_function_call(
        FakeFunctionCall("filesystem__create_project_structure", "call-2", {"project_name": "medical-booking", "files": [{"path": "README.md", "content": ""}]})
    )

    assert decode_function_output(first)["status"] == "project_already_exists"
    assert host.state.project_exists is True
    assert host.state.project_created is False
    assert host.state.created_project_name == "medical-booking"
    assert decode_function_output(second)["status"] == "project_already_exists"
    assert len(host.approval_requests) == 1


@pytest.mark.asyncio
async def test_only_one_sensitive_tool_executes_per_model_response_before_next_round() -> None:
    manager = FakeManager()
    analyze_client = RecordingToolClient({"requirement": "medical-booking"})
    tasks_client = RecordingToolClient({"tasks": []})
    list_client = RecordingToolClient({"entries": []})
    create_client = RecordingToolClient({"success": True, "project_name": "medical-booking"})
    register_tool(manager, "software_factory", "analyze_requirement", analyze_client, False)
    register_tool(manager, "software_factory", "create_tasks", tasks_client, False)
    register_tool(manager, "filesystem", "list_files", list_client, False)
    register_tool(manager, "filesystem", "create_project_structure", create_client, True)
    openai_client = FakeOpenAIClient(
        [
            FakeResponse([FakeFunctionCall("software_factory__analyze_requirement", "call-analyze", {"requirement": "x"})]),
            FakeResponse([FakeFunctionCall("software_factory__create_tasks", "call-tasks", {"project_type": "x"})]),
            FakeResponse([FakeFunctionCall("filesystem__list_files", "call-list", {"relative_path": "."})]),
            FakeResponse(
                [
                    FakeFunctionCall(
                        "filesystem__create_project_structure",
                        "call-create-1",
                        {"project_name": "medical-booking", "files": [{"path": "README.md", "content": ""}]},
                    ),
                    FakeFunctionCall(
                        "filesystem__create_project_structure",
                        "call-create-2",
                        {"project_name": "medical-booking", "files": [{"path": "README.md", "content": ""}]},
                    ),
                ]
            ),
            FakeResponse([FakeMessage("Final.")], output_text="Final."),
        ]
    )
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions(), approvals=[True])

    final_text = await host.run_openai_loop(user_message("Crea un proyecto FastAPI llamado medical-booking"))

    assert final_text == "Final."
    assert create_client.calls == [("create_project_structure", {"project_name": "medical-booking", "files": [{"path": "README.md", "content": ""}]})]
    assert len(host.approval_requests) == 1
    assert openai_client.responses.calls[-1]["tools"] == []


@pytest.mark.asyncio
async def test_repeated_rejected_sensitive_tool_does_not_request_approval_again() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True})
    register_tool(manager, "filesystem", "write_file", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[False])
    host.state = ExecutionState(original_user_message="actualiza archivo")
    call = FakeFunctionCall("filesystem__write_file", "call-write-1", {"relative_path": "a.py", "content": "x"})

    first, _ = await host.handle_function_call(call)
    second, _ = await host.handle_function_call(
        FakeFunctionCall("filesystem__write_file", "call-write-2", {"relative_path": "b.py", "content": "y"})
    )

    assert decode_function_output(first)["status"] == "rejected_by_user"
    assert decode_function_output(second)["status"] == "previously_rejected"
    assert client.calls == []
    assert len(host.approval_requests) == 1


@pytest.mark.asyncio
async def test_run_tests_updates_execution_state_for_success_and_failure() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True, "project_name": "medical-booking"})
    register_tool(manager, "testing", "run_tests", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True, True])
    host.state = ExecutionState(original_user_message="valida tests")

    await host.handle_function_call(FakeFunctionCall("testing__run_tests", "call-tests-1", {"project_name": "medical-booking"}))
    assert host.state.tests_executed is True
    assert host.state.tests_passed is True

    client.result = {"success": False, "project_name": "medical-booking"}
    await host.handle_function_call(FakeFunctionCall("testing__run_tests", "call-tests-2", {"project_name": "medical-booking"}))
    assert host.state.tests_executed is True
    assert host.state.tests_passed is False


@pytest.mark.asyncio
async def test_run_tests_exit_code_zero_and_one_update_state() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True, "exit_code": 0, "stdout": "ok", "stderr": ""})
    register_tool(manager, "testing", "run_tests", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True, True])
    host.state = ExecutionState(original_user_message="tests")

    await host.handle_function_call(FakeFunctionCall("testing__run_tests", "call-ok", {"project_name": "medical-booking"}))
    assert host.state.tests_executed is True
    assert host.state.tests_passed is True

    client.result = {"success": False, "exit_code": 1, "stdout": "failed output", "stderr": "traceback"}
    await host.handle_function_call(FakeFunctionCall("testing__run_tests", "call-fail", {"project_name": "medical-booking"}))
    assert host.state.tests_executed is True
    assert host.state.tests_passed is False


@pytest.mark.asyncio
async def test_run_tests_functional_failure_stores_stdout_summary_and_failing_file() -> None:
    manager = FakeManager()
    stdout = """
tests\\test_health.py:9: AssertionError
E       AssertionError: assert {'status': 'ok'} == {'status': 'healthy'}
"""
    client = RecordingToolClient(
        {
            "success": False,
            "failure_type": "test_failure",
            "exit_code": 1,
            "timed_out": False,
            "project_name": "medical-booking",
            "stdout": stdout,
            "stderr": "",
        }
    )
    register_tool(manager, "testing", "run_tests", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(
        original_user_message='Revisa el proyecto medical-booking; GET /health debe devolver {"status": "ok"}.',
        requested_project_name="medical-booking",
        created_project_name="medical-booking",
        workflow_intent="review_existing_project",
        workspace_inspected=True,
        project_exists=True,
        environment_prepared=True,
        detected_test_framework="pytest",
    )

    await host.handle_function_call(FakeFunctionCall("testing__run_tests", "call-tests", {"project_name": "medical-booking"}))

    assert host.state.test_stdout == stdout
    assert host.state.test_stderr == ""
    assert host.state.failure_type == FailureType.TEST_FAILURE.value
    assert host.state.failure_stage == "run_tests"
    assert host.state.failure_message == 'tests/test_health.py:9 esperaba {"status": "healthy"}, pero el endpoint devolvió {"status": "ok"}.'
    assert host.state.failing_test_files == ["tests/test_health.py"]
    assert host.state.repair_phase == "read_failing_test"


def test_repair_phases_force_single_next_tool() -> None:
    manager = FakeManager()
    register_workflow_tools(manager)
    state = ExecutionState(
        original_user_message='Revisa medical-booking; GET /health debe devolver {"status": "ok"}.',
        requested_project_name="medical-booking",
        created_project_name="medical-booking",
        workflow_intent="review_existing_project",
        workspace_inspected=True,
        project_exists=True,
        detected_test_framework="pytest",
        environment_prepared=True,
        tests_executed=True,
        tests_passed=False,
        failing_test_files=["tests/test_health.py"],
        repair_phase="read_failing_test",
    )
    assert [tool.public_name for tool in get_allowed_tools(state, manager.registry)] == ["filesystem__read_file"]

    state.repair_phase = "read_related_source"
    assert [tool.public_name for tool in get_allowed_tools(state, manager.registry)] == ["filesystem__read_file"]

    state.repair_phase = "apply_fix"
    assert [tool.public_name for tool in get_allowed_tools(state, manager.registry)] == ["filesystem__update_project_files"]

    state.repair_phase = "rerun_tests"
    assert determine_next_action(state) == "testing__run_tests"
    assert [tool.public_name for tool in get_allowed_tools(state, manager.registry)] == ["testing__run_tests"]


@pytest.mark.asyncio
async def test_repair_read_update_and_rerun_phase_transitions() -> None:
    manager = FakeManager()
    read_client = RecordingToolClient({"success": True, "path": "medical-booking/tests/test_health.py", "content": "healthy"})
    source_client = RecordingToolClient({"success": True, "path": "medical-booking/medical_booking/main.py", "content": '{"status": "ok"}'})
    update_client = RecordingToolClient({"success": True, "project_name": "medical-booking", "files_updated": ["tests/test_health.py"]})
    run_client = RecordingToolClient({"success": True, "project_name": "medical-booking", "stdout": "1 passed in 0.46s", "command": ["venv", "-m", "pytest"]})
    register_tool(manager, "filesystem", "read_file", read_client, False)
    register_tool(manager, "filesystem", "update_project_files", update_client, True)
    register_tool(manager, "testing", "run_tests", run_client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True, True])
    host.state = ExecutionState(
        original_user_message='Revisa el proyecto medical-booking; GET /health debe devolver {"status": "ok"}.',
        requested_project_name="medical-booking",
        created_project_name="medical-booking",
        workflow_intent="review_existing_project",
        workspace_inspected=True,
        project_exists=True,
        environment_prepared=True,
        detected_test_framework="pytest",
        tests_executed=True,
        tests_passed=False,
        test_failure_summary='tests/test_health.py:9 esperaba {"status": "healthy"}, pero el endpoint devolvió {"status": "ok"}.',
        failing_test_files=["tests/test_health.py"],
        repair_phase="read_failing_test",
    )

    await host.handle_function_call(FakeFunctionCall("filesystem__read_file", "call-test", {"relative_path": "tests/test_health.py"}))
    assert host.state.repair_phase == "read_related_source"
    assert read_client.calls[-1] == ("read_file", {"relative_path": "medical-booking/tests/test_health.py"})
    read_client.result = source_client.result
    await host.handle_function_call(FakeFunctionCall("filesystem__read_file", "call-source", {"relative_path": "medical_booking/main.py"}))
    assert host.state.repair_phase == "apply_fix"
    assert read_client.calls[-1] == ("read_file", {"relative_path": "medical-booking/medical_booking/main.py"})
    await host.handle_function_call(
        FakeFunctionCall(
            "filesystem__update_project_files",
            "call-update",
            {"project_name": "medical-booking", "files": [{"path": "tests/test_health.py", "content": '{"status": "ok"}'}]},
        )
    )
    assert host.state.repair_phase == "rerun_tests"
    assert host.state.repair_attempts == 1
    assert host.state.repair_before == '"status": "healthy"'
    assert host.state.repair_after == '"status": "ok"'
    assert host.state.repair_decision == "El endpoint ya cumplía el requerimiento original. Se corrigió el test."
    await host.handle_function_call(FakeFunctionCall("testing__run_tests", "call-rerun", {"project_name": "medical-booking"}))

    assert host.state.repair_phase == "completed"
    assert host.state.tests_passed is True
    assert update_client.calls == [
        ("update_project_files", {"project_name": "medical-booking", "files": [{"path": "tests/test_health.py", "content": '{"status": "ok"}'}]})
    ]


@pytest.mark.asyncio
async def test_relative_repair_read_path_is_normalized_without_rejection_round() -> None:
    manager = FakeManager()
    read_client = RecordingToolClient({"success": True, "path": "medical-booking/tests/test_health.py", "content": "healthy"})
    register_tool(manager, "filesystem", "read_file", read_client, False)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(
        original_user_message='Revisa el proyecto medical-booking; GET /health debe devolver {"status": "ok"}.',
        requested_project_name="medical-booking",
        created_project_name="medical-booking",
        workflow_intent="review_existing_project",
        workspace_inspected=True,
        project_exists=True,
        detected_test_framework="pytest",
        environment_prepared=True,
        tests_executed=True,
        tests_passed=False,
        failing_test_files=["tests/test_health.py"],
        repair_phase="read_failing_test",
    )

    output, executed_sensitive = await host.handle_function_call(
        FakeFunctionCall("filesystem__read_file", "call-read", {"relative_path": "tests/test_health.py"})
    )
    result = decode_function_output(output)

    assert executed_sensitive is False
    assert result["success"] is True
    assert result.get("status") != "repair_read_path_not_allowed"
    assert read_client.calls == [("read_file", {"relative_path": "medical-booking/tests/test_health.py"})]


@pytest.mark.asyncio
async def test_repair_policy_blocks_changing_endpoint_when_test_contradicts_requirement() -> None:
    manager = FakeManager()
    update_client = RecordingToolClient({"success": True})
    register_tool(manager, "filesystem", "update_project_files", update_client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(
        original_user_message='Revisa el proyecto medical-booking; GET /health debe devolver {"status": "ok"}.',
        requested_project_name="medical-booking",
        created_project_name="medical-booking",
        workflow_intent="review_existing_project",
        workspace_inspected=True,
        project_exists=True,
        detected_test_framework="pytest",
        environment_prepared=True,
        tests_executed=True,
        tests_passed=False,
        test_failure_summary='tests/test_health.py:9 esperaba {"status": "healthy"}, pero el endpoint devolvió {"status": "ok"}.',
        failing_test_files=["tests/test_health.py"],
        repair_phase="apply_fix",
    )

    output, executed_sensitive = await host.handle_function_call(
        FakeFunctionCall(
            "filesystem__update_project_files",
            "call-update",
            {"project_name": "medical-booking", "files": [{"path": "medical_booking/main.py", "content": '{"status": "healthy"}'}]},
        )
    )
    result = decode_function_output(output)

    assert executed_sensitive is False
    assert result["status"] == "repair_update_path_not_allowed"
    assert update_client.calls == []
    assert host.approval_requests == []


@pytest.mark.asyncio
async def test_repair_attempt_limit_is_respected() -> None:
    manager = FakeManager()
    run_client = RecordingToolClient(
        {
            "success": False,
            "failure_type": "test_failure",
            "stdout": "tests/test_health.py:9: AssertionError\nE assert {'status': 'ok'} == {'status': 'healthy'}",
            "stderr": "",
        }
    )
    register_tool(manager, "testing", "run_tests", run_client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(
        original_user_message='Revisa el proyecto medical-booking; GET /health debe devolver {"status": "ok"}.',
        requested_project_name="medical-booking",
        created_project_name="medical-booking",
        workflow_intent="review_existing_project",
        repair_phase="rerun_tests",
        repair_attempts=MAX_REPAIR_ATTEMPTS,
    )

    await host.handle_function_call(FakeFunctionCall("testing__run_tests", "call-tests", {"project_name": "medical-booking"}))

    assert host.state.retry_limit_reached is True
    assert host.state.terminal_status == "retry_limit_reached"


@pytest.mark.asyncio
async def test_run_tests_timeout_marks_infrastructure_failure_and_normalizes_output() -> None:
    manager = FakeManager()
    client = RecordingToolClient(
        {
            "success": False,
            "failure_type": "timeout",
            "exit_code": None,
            "timed_out": True,
            "project_name": "medical-booking",
            "stdout": "",
            "stderr": "La ejecución de pruebas superó el timeout de 30 segundos.",
        }
    )
    register_tool(manager, "testing", "run_tests", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(original_user_message="tests")

    output, _ = await host.handle_function_call(
        FakeFunctionCall("testing__run_tests", "call-timeout", {"project_name": "medical-booking"})
    )
    result = decode_function_output(output)

    assert host.state.tests_executed is True
    assert host.state.tests_passed is False
    assert host.state.test_infrastructure_failed is True
    assert result["status"] == "test_infrastructure_failure"
    assert result["timed_out"] is True
    assert "No modifiques el código" in result["message"]


@pytest.mark.asyncio
async def test_after_timeout_blocks_inspection_correction_and_retesting_without_approval() -> None:
    manager = FakeManager()
    list_client = RecordingToolClient({"success": True})
    read_client = RecordingToolClient({"success": True})
    update_client = RecordingToolClient({"success": True})
    detect_client = RecordingToolClient({"success": True})
    run_client = RecordingToolClient({"success": True})
    register_tool(manager, "filesystem", "list_files", list_client, False)
    register_tool(manager, "filesystem", "read_file", read_client, False)
    register_tool(manager, "filesystem", "update_project_files", update_client, True)
    register_tool(manager, "testing", "detect_test_framework", detect_client, False)
    register_tool(manager, "testing", "run_tests", run_client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(original_user_message="tests", test_infrastructure_failed=True)

    for name, args in [
        ("filesystem__list_files", {"relative_path": "."}),
        ("filesystem__read_file", {"relative_path": "app.py"}),
        ("filesystem__update_project_files", {"project_name": "medical-booking", "files": []}),
        ("testing__detect_test_framework", {"project_name": "medical-booking"}),
        ("testing__run_tests", {"project_name": "medical-booking"}),
    ]:
        output, executed_sensitive = await host.handle_function_call(FakeFunctionCall(name, f"call-{name}", args))
        result = decode_function_output(output)
        assert executed_sensitive is False
        assert result["status"] == "workflow_stopped_after_test_infrastructure_failure"

    assert list_client.calls == []
    assert read_client.calls == []
    assert update_client.calls == []
    assert detect_client.calls == []
    assert run_client.calls == []
    assert host.approval_requests == []


@pytest.mark.asyncio
async def test_functional_test_failure_allows_read_and_update() -> None:
    manager = FakeManager()
    read_client = RecordingToolClient({"success": True, "content": "code"})
    update_client = RecordingToolClient({"success": True})
    register_tool(manager, "filesystem", "read_file", read_client, False)
    register_tool(manager, "filesystem", "update_project_files", update_client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(original_user_message="tests", tests_executed=True, tests_passed=False)

    await host.handle_function_call(FakeFunctionCall("filesystem__read_file", "call-read", {"relative_path": "medical-booking/app.py"}))
    await host.handle_function_call(
        FakeFunctionCall("filesystem__update_project_files", "call-update", {"project_name": "medical-booking", "files": []})
    )

    assert read_client.calls == [("read_file", {"relative_path": "medical-booking/app.py"})]
    assert update_client.calls == [("update_project_files", {"project_name": "medical-booking", "files": []})]
    assert len(host.approval_requests) == 1


@pytest.mark.asyncio
async def test_timeout_allows_final_response_without_round_limit() -> None:
    manager = FakeManager()
    run_client = RecordingToolClient(
        {
            "success": False,
            "failure_type": "timeout",
            "exit_code": None,
            "timed_out": True,
            "project_name": "medical-booking",
            "stdout": "",
            "stderr": "",
        }
    )
    register_tool(manager, "testing", "run_tests", run_client, True)
    openai_client = FakeOpenAIClient([FakeResponse([FakeMessage("Proyecto creado, pero las pruebas no pudieron validarse por timeout.")], output_text="Proyecto creado, pero las pruebas no pudieron validarse por timeout.")])
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(original_user_message="tests", environment_prepared=True, detected_test_framework="pytest")

    await host.handle_function_call(FakeFunctionCall("testing__run_tests", "call-tests", {"project_name": "medical-booking"}))
    final_text = await host._finalize_after_terminal_state(user_message("Valida las pruebas del proyecto medical-booking."))

    assert "timeout" in final_text
    assert "Se alcanzó el límite general" not in final_text
    assert host.state.test_infrastructure_failed is True
    assert openai_client.responses.calls[-1]["tools"] == []


@pytest.mark.asyncio
async def test_prepare_environment_updates_host_state() -> None:
    manager = FakeManager()
    client = RecordingToolClient(
        {
            "success": True,
            "project_name": "medical-booking",
            "python_executable": "workspace/medical-booking/.venv/Scripts/python.exe",
            "dependencies_installed": True,
            "failure_stage": "none",
        }
    )
    register_tool(manager, "testing", "prepare_test_environment", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(original_user_message="prepara")

    await host.handle_function_call(
        FakeFunctionCall("testing__prepare_test_environment", "call-prepare", {"project_name": "medical-booking"})
    )

    assert host.state.environment_prepared is True
    assert host.state.dependencies_installed is True
    assert host.state.environment_python == "workspace/medical-booking/.venv/Scripts/python.exe"


@pytest.mark.asyncio
async def test_dependency_policy_failure_is_not_infrastructure_terminal() -> None:
    manager = FakeManager()
    prepare_client = RecordingToolClient(
        {
            "success": False,
            "failure_stage": "validate_dependencies",
            "failure_type": "dependency_policy_violation",
            "timed_out": False,
            "message": "El archivo requirements.txt no cumple la politica de dependencias.",
        }
    )
    run_client = RecordingToolClient({"success": True})
    register_tool(manager, "testing", "prepare_test_environment", prepare_client, True)
    register_tool(manager, "testing", "run_tests", run_client, True)
    openai_client = FakeOpenAIClient([FakeResponse([FakeMessage("Entorno no preparado; pruebas no validadas.")], output_text="Entorno no preparado; pruebas no validadas.")])
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(original_user_message="prepara", project_created=True, detected_test_framework="pytest")

    await host.handle_function_call(FakeFunctionCall("testing__prepare_test_environment", "call-prepare", {"project_name": "medical-booking"}))
    final_text = await host._finalize_after_terminal_state(user_message("Prepara el entorno y valida medical-booking"))

    assert "Entorno no preparado" in final_text
    assert host.state.test_infrastructure_failed is False
    assert host.state.failure_type == "dependency_policy_violation"
    assert prepare_client.calls == [("prepare_test_environment", {"project_name": "medical-booking"})]
    assert run_client.calls == []


@pytest.mark.asyncio
async def test_real_pip_failure_marks_infrastructure_failed_and_finalizes_without_tools() -> None:
    manager = FakeManager()
    prepare_client = RecordingToolClient(
        {
            "success": False,
            "failure_stage": "install_dependencies",
            "failure_type": "dependency_installation_failure",
            "timed_out": False,
            "stderr": "pip failed",
            "message": "La instalación de dependencias falló.",
        }
    )
    run_client = RecordingToolClient({"success": True})
    register_tool(manager, "testing", "prepare_test_environment", prepare_client, True)
    register_tool(manager, "testing", "run_tests", run_client, True)
    openai_client = FakeOpenAIClient([FakeResponse([FakeMessage("Instalacion fallida.")], output_text="Instalacion fallida.")])
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(original_user_message="prepara", project_created=True, detected_test_framework="pytest")

    await host.handle_function_call(FakeFunctionCall("testing__prepare_test_environment", "call-prepare", {"project_name": "medical-booking"}))
    final_text = await host._finalize_after_terminal_state(user_message("Prepara el entorno y valida medical-booking"))

    assert "fallida" in final_text
    assert host.state.test_infrastructure_failed is True
    assert host.state.failure_type == "dependency_installation_failure"
    assert host.state.failure_stage == "install_dependencies"
    assert run_client.calls == []
    assert openai_client.responses.calls[-1]["tools"] == []


@pytest.mark.asyncio
async def test_invalid_prepare_arguments_can_be_retried_with_valid_arguments_and_finish() -> None:
    manager = FakeManager()
    prepare_client = RecordingToolClient(
        {
            "success": True,
            "project_name": "medical-booking",
            "python_executable": "workspace/medical-booking/.venv/Scripts/python.exe",
            "dependencies_installed": True,
            "failure_stage": "none",
        }
    )
    tests_client = RecordingToolClient({"success": True, "project_name": "medical-booking"})
    analyze_client = RecordingToolClient({"requirement": "medical-booking"})
    tasks_client = RecordingToolClient({"tasks": []})
    list_client = RecordingToolClient({"entries": []})
    create_client = RecordingToolClient({"success": True, "project_name": "medical-booking"})
    detect_client = RecordingToolClient({"success": True, "framework": "pytest", "command": ["python", "-m", "pytest"]})
    register_tool(manager, "software_factory", "analyze_requirement", analyze_client, False)
    register_tool(manager, "software_factory", "create_tasks", tasks_client, False)
    register_tool(manager, "filesystem", "list_files", list_client, False)
    register_tool(manager, "filesystem", "create_project_structure", create_client, True)
    register_tool(manager, "testing", "detect_test_framework", detect_client, False)
    manager.registry.register(
        server_name="testing",
        original_name="prepare_test_environment",
        description="Prepare environment",
        input_schema={
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
                "dependency_file": {"type": "string", "default": "requirements.txt"},
            },
            "required": ["project_name"],
        },
        requires_approval=True,
        client=prepare_client,
    )
    register_tool(manager, "testing", "run_tests", tests_client, True)
    openai_client = FakeOpenAIClient(
        [
            FakeResponse([FakeFunctionCall("software_factory__analyze_requirement", "call-analyze", {"requirement": "x"})]),
            FakeResponse([FakeFunctionCall("software_factory__create_tasks", "call-tasks", {"project_type": "x"})]),
            FakeResponse([FakeFunctionCall("filesystem__list_files", "call-list", {"relative_path": "."})]),
            FakeResponse(
                [
                    FakeFunctionCall(
                        "filesystem__create_project_structure",
                        "call-create",
                        {"project_name": "medical-booking", "files": [{"path": "README.md", "content": ""}]},
                    )
                ]
            ),
            FakeResponse([FakeFunctionCall("testing__detect_test_framework", "call-detect", {"project_name": "medical-booking"})]),
            FakeResponse(
                [
                    FakeFunctionCall(
                        "testing__prepare_test_environment",
                        "call-invalid",
                        {"project_name": "medical-booking", "timeout_seconds": 1200},
                    )
                ]
            ),
            FakeResponse([FakeFunctionCall("testing__prepare_test_environment", "call-valid", {"project_name": "medical-booking"})]),
            FakeResponse([FakeFunctionCall("testing__run_tests", "call-tests", {"project_name": "medical-booking"})]),
            FakeResponse([FakeMessage("Proyecto medical-booking validado.")], output_text="Proyecto medical-booking validado."),
        ]
    )
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions(), approvals=[True, True, True])

    final_text = await host.run_openai_loop(user_message("Prepara y valida medical-booking"))

    assert final_text == "Proyecto medical-booking validado."
    assert host.approval_requests == [
            ("filesystem__create_project_structure", {"project_name": "medical-booking", "files": [{"path": "README.md", "content": ""}]}),
        ("testing__prepare_test_environment", {"project_name": "medical-booking"}),
        ("testing__run_tests", {"project_name": "medical-booking"}),
    ]
    assert prepare_client.calls == [("prepare_test_environment", {"project_name": "medical-booking"})]
    assert tests_client.calls == [("run_tests", {"project_name": "medical-booking"})]
    assert host.state.test_infrastructure_failed is False
    assert host.state.tests_passed is True
    assert openai_client.responses.calls[-1]["tools"] == []

@pytest.mark.asyncio
async def test_repeated_prepare_environment_is_blocked_without_approval() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True})
    register_tool(manager, "testing", "prepare_test_environment", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(original_user_message="prepara", environment_prepared=True, created_project_name="medical-booking")

    output, executed_sensitive = await host.handle_function_call(
        FakeFunctionCall("testing__prepare_test_environment", "call-prepare", {"project_name": "medical-booking"})
    )
    result = decode_function_output(output)

    assert executed_sensitive is False
    assert result["status"] == "environment_already_prepared"
    assert client.calls == []
    assert host.approval_requests == []


def test_completed_stage_tools_are_filtered_from_openai_tools() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True})
    register_tool(manager, "software_factory", "analyze_requirement", client, False)
    register_tool(manager, "software_factory", "create_tasks", client, False)
    register_tool(manager, "filesystem", "create_project_structure", client, True)
    register_tool(manager, "testing", "detect_test_framework", client, False)
    register_tool(manager, "testing", "prepare_test_environment", client, True)
    register_tool(manager, "testing", "run_tests", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    state = ExecutionState(
        original_user_message="x",
        analysis_completed=True,
        tasks_created=True,
        workspace_inspected=True,
        project_created=True,
        environment_prepared=True,
        detected_test_framework="pytest",
    )

    names = {tool["name"] for tool in host.filtered_openai_tools(state)}

    assert "software_factory__analyze_requirement" not in names
    assert "software_factory__create_tasks" not in names
    assert "filesystem__create_project_structure" not in names
    assert "testing__prepare_test_environment" not in names
    assert "testing__detect_test_framework" not in names
    assert "testing__run_tests" in names


@pytest.mark.asyncio
async def test_detect_test_framework_updates_state_and_filters_tool() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True, "framework": "pytest"})
    register_tool(manager, "testing", "detect_test_framework", client, False)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(original_user_message="detecta")

    await host.handle_function_call(FakeFunctionCall("testing__detect_test_framework", "call-detect", {"project_name": "medical-booking"}))
    names = {tool["name"] for tool in host.filtered_openai_tools(host.state)}

    assert host.state.detected_test_framework == "pytest"
    assert "testing__detect_test_framework" not in names


@pytest.mark.asyncio
async def test_tests_passed_finalizes_immediately_without_tools() -> None:
    manager = FakeManager()
    openai_client = FakeOpenAIClient([FakeResponse([FakeMessage("Pruebas validadas.")], output_text="Pruebas validadas.")])
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(original_user_message="valida", tests_executed=True, tests_passed=True)

    final_text = await host._finalize_after_terminal_state(user_message("Valida medical-booking"))

    assert final_text == "Pruebas validadas."
    assert host.state.tests_passed is True
    assert openai_client.responses.calls[-1]["tools"] == []


@pytest.mark.asyncio
async def test_finalization_keeps_original_requirement_and_repair_decision() -> None:
    manager = FakeManager()
    final_text = (
        'El endpoint ya devolvía {"status": "ok"}, tal como exigía el requerimiento original. '
        'La prueba esperaba incorrectamente {"status": "healthy"}, por lo que se actualizó tests/test_health.py.'
    )
    openai_client = FakeOpenAIClient([FakeResponse([FakeMessage(final_text)], output_text=final_text)])
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions())
    host.state = ExecutionState(
        original_user_message='Revisa medical-booking; GET /health debe devolver {"status": "ok"}.',
        requested_project_name="medical-booking",
        created_project_name="medical-booking",
        workflow_intent="review_existing_project",
        tests_executed=True,
        tests_passed=True,
        repair_phase="completed",
        repair_decision="El endpoint ya cumplía el requerimiento original. Se corrigió el test.",
        repair_before='"status": "healthy"',
        repair_after='"status": "ok"',
        files_updated_during_repair={"tests/test_health.py"},
    )

    response = await host._finalize_after_terminal_state(user_message(host.state.original_user_message))
    summary_message = openai_client.responses.calls[-1]["input"][-1]["content"]

    assert response == final_text
    assert '"original_requirement": "Revisa medical-booking; GET /health debe devolver {\\"status\\": \\"ok\\"}."' in summary_message
    assert '"repair_decision": "El endpoint ya cumplía el requerimiento original. Se corrigió el test."' in summary_message
    assert '"repair_before": "\\"status\\": \\"healthy\\""' in summary_message
    assert '"repair_after": "\\"status\\": \\"ok\\""' in summary_message
    assert "tests/test_health.py" in response
    assert "cambiar el endpoint" not in response.lower()
    assert "main.py" not in response


@pytest.mark.asyncio
async def test_full_simulated_flow_prepares_environment_before_tests() -> None:
    manager = FakeManager()
    analyze_client = RecordingToolClient({"requirement": "medical-booking"})
    tasks_client = RecordingToolClient({"tasks": []})
    list_client = RecordingToolClient({"entries": []})
    create_client = RecordingToolClient({"success": True, "project_name": "medical-booking"})
    detect_client = RecordingToolClient({"success": True, "framework": "pytest"})
    prepare_client = RecordingToolClient(
        {
            "success": True,
            "project_name": "medical-booking",
            "python_executable": "workspace/medical-booking/.venv/Scripts/python.exe",
            "dependencies_installed": True,
            "failure_stage": "none",
        }
    )
    tests_client = RecordingToolClient({"success": True, "project_name": "medical-booking"})
    register_tool(manager, "software_factory", "analyze_requirement", analyze_client, False)
    register_tool(manager, "software_factory", "create_tasks", tasks_client, False)
    register_tool(manager, "filesystem", "list_files", list_client, False)
    register_tool(manager, "filesystem", "create_project_structure", create_client, True)
    register_tool(manager, "testing", "detect_test_framework", detect_client, False)
    register_tool(manager, "testing", "prepare_test_environment", prepare_client, True)
    register_tool(manager, "testing", "run_tests", tests_client, True)
    openai_client = FakeOpenAIClient(
        [
            FakeResponse([FakeFunctionCall("software_factory__analyze_requirement", "call-analyze", {"requirement": "x"})]),
            FakeResponse([FakeFunctionCall("software_factory__create_tasks", "call-tasks", {"project_type": "x"})]),
            FakeResponse([FakeFunctionCall("filesystem__list_files", "call-list", {"relative_path": "."})]),
            FakeResponse(
                [
                    FakeFunctionCall(
                        "filesystem__create_project_structure",
                        "call-create",
                        {"project_name": "medical-booking", "files": [{"path": "requirements.txt", "content": f"{DEFAULT_FASTAPI_REQUIREMENT}\n{DEFAULT_PYTEST_REQUIREMENT}\n"}]},
                    )
                ]
            ),
            FakeResponse(
                [
                    FakeFunctionCall("testing__detect_test_framework", "call-detect", {"project_name": "medical-booking"}),
                    FakeFunctionCall("testing__prepare_test_environment", "call-prepare", {"project_name": "medical-booking"}),
                ]
            ),
            FakeResponse(
                [
                    FakeFunctionCall("testing__run_tests", "call-tests", {"project_name": "medical-booking"}),
                ]
            ),
            FakeResponse([FakeMessage("Listo.")], output_text="Listo."),
        ]
    )
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions(), approvals=[True, True, True])

    final_text = await host.run_openai_loop(user_message("Crea y valida medical-booking"))

    assert final_text == "Listo."
    assert prepare_client.calls == [("prepare_test_environment", {"project_name": "medical-booking"})]
    assert tests_client.calls == [("run_tests", {"project_name": "medical-booking"})]
    assert host.state.environment_prepared is True
    assert host.state.dependencies_installed is True
    assert host.state.tests_passed is True


@pytest.mark.asyncio
async def test_complete_medical_booking_flow_regression_uses_venv_python_and_warnings() -> None:
    manager = FakeManager()
    calls: list[tuple[str, dict[str, Any]]] = []

    class OrderedToolClient:
        def __init__(self, public_name: str, result: dict[str, Any]) -> None:
            self.public_name = public_name
            self.result = result
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((tool_name, arguments))
            calls.append((self.public_name, arguments))
            return self.result

    venv_python = "workspace/medical-booking/.venv/Scripts/python.exe"
    pytest_command = [venv_python, "-m", "pytest", "tests", "-q", "--disable-warnings"]
    clients = {
        "software_factory__analyze_requirement": OrderedToolClient(
            "software_factory__analyze_requirement",
            {"success": True, "requirement": "medical-booking"},
        ),
        "software_factory__create_tasks": OrderedToolClient(
            "software_factory__create_tasks",
            {"success": True, "project_name": "medical-booking", "tasks": []},
        ),
        "filesystem__list_files": OrderedToolClient(
            "filesystem__list_files",
            {"success": True, "entries": []},
        ),
        "filesystem__create_project_structure": OrderedToolClient(
            "filesystem__create_project_structure",
            {"success": True, "project_name": "medical-booking"},
        ),
        "testing__detect_test_framework": OrderedToolClient(
            "testing__detect_test_framework",
            {"success": True, "project_name": "medical-booking", "framework": "pytest", "command": pytest_command},
        ),
        "testing__prepare_test_environment": OrderedToolClient(
            "testing__prepare_test_environment",
            {
                "success": True,
                "project_name": "medical-booking",
                "python_executable": venv_python,
                "dependencies_installed": True,
                "installed_fastapi_version": "0.139.0",
                "installed_starlette_version": "1.3.1",
                "failure_stage": "none",
            },
        ),
        "testing__run_tests": OrderedToolClient(
            "testing__run_tests",
            {
                "success": True,
                "project_name": "medical-booking",
                "framework": "pytest",
                "command": pytest_command,
                "stdout": ". 1 passed, 2 warnings in 0.46s",
                "test_warning_count": 2,
            },
        ),
    }
    register_tool(manager, "software_factory", "analyze_requirement", clients["software_factory__analyze_requirement"], False)
    register_tool(manager, "software_factory", "create_tasks", clients["software_factory__create_tasks"], False)
    register_tool(manager, "filesystem", "list_files", clients["filesystem__list_files"], False)
    register_tool(manager, "filesystem", "create_project_structure", clients["filesystem__create_project_structure"], True)
    register_tool(manager, "testing", "detect_test_framework", clients["testing__detect_test_framework"], False)
    register_tool(manager, "testing", "prepare_test_environment", clients["testing__prepare_test_environment"], True)
    register_tool(manager, "testing", "run_tests", clients["testing__run_tests"], True)
    final_answer = (
        "Proyecto: medical-booking\n"
        "Estado: creado\n"
        "Entorno: preparado\n"
        "Dependencias: instaladas\n"
        "Framework de pruebas: pytest\n"
        f"Python usado: {venv_python}\n"
        f"Comando real: {' '.join(pytest_command)}\n"
        "Resultado: 1 passed\n"
        "Warnings: 2"
    )
    openai_client = FakeOpenAIClient(
        [
            FakeResponse([FakeFunctionCall("software_factory__analyze_requirement", "call-analyze", {"requirement": "medical-booking"})]),
            FakeResponse([FakeFunctionCall("software_factory__create_tasks", "call-tasks", {"project_type": "medical-booking", "backend": "fastapi"})]),
            FakeResponse([FakeFunctionCall("filesystem__list_files", "call-list", {"relative_path": "."})]),
            FakeResponse(
                [
                    FakeFunctionCall(
                        "filesystem__create_project_structure",
                        "call-create",
                        {
                            "project_name": "medical-booking",
                            "files": [
                                {
                                    "path": "requirements.txt",
                                    "content": f"{DEFAULT_FASTAPI_REQUIREMENT}\n{DEFAULT_PYTEST_REQUIREMENT}\n",
                                }
                            ],
                        },
                    )
                ]
            ),
            FakeResponse([FakeFunctionCall("testing__detect_test_framework", "call-detect", {"project_name": "medical-booking"})]),
            FakeResponse([FakeFunctionCall("testing__prepare_test_environment", "call-prepare", {"project_name": "medical-booking"})]),
            FakeResponse([FakeFunctionCall("testing__run_tests", "call-tests", {"project_name": "medical-booking"})]),
            FakeResponse([FakeMessage(final_answer)], output_text=final_answer),
        ]
    )
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions(), approvals=[True, True, True])

    final_text = await host.run_openai_loop(
        user_message(
            'Crea un proyecto FastAPI llamado medical-booking con un endpoint GET /health que devuelva {"status": "ok"}, agrega pruebas y valida que pasen.'
        )
    )

    assert [name for name, _ in calls] == [
        "software_factory__analyze_requirement",
        "software_factory__create_tasks",
        "filesystem__list_files",
        "filesystem__create_project_structure",
        "testing__detect_test_framework",
        "testing__prepare_test_environment",
        "testing__run_tests",
    ]
    assert len(calls) == 7
    for public_name, client in clients.items():
        assert len(client.calls) == 1, public_name
    assert calls[0][1]["requirement"] == "medical-booking"
    assert calls[1][1]["project_type"] == "medical-booking"
    assert calls[2][1] == {"relative_path": "."}
    assert all(arguments.get("project_name") == "medical-booking" for _, arguments in calls[3:])
    assert final_text == final_answer
    assert venv_python in final_text
    assert host.state.tests_passed is True
    assert host.state.test_warning_count == 2
    assert host.state.environment_python == venv_python
    assert host.state.actual_test_command == pytest_command
    assert host.state.installed_fastapi_version == "0.139.0"
    assert host.state.installed_starlette_version == "1.3.1"
    assert host.state.terminal_status == "completed"
    assert "Se alcanzó el límite general" not in final_text
    assert openai_client.responses.calls[-1]["tools"] == []


@pytest.mark.asyncio
async def test_full_review_existing_project_flow_repairs_test_and_finishes() -> None:
    manager = FakeManager()
    calls: list[tuple[str, dict[str, Any]]] = []

    class SequencedToolClient:
        def __init__(self, public_name: str, results: list[dict[str, Any]]) -> None:
            self.public_name = public_name
            self.results = results
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((tool_name, arguments))
            calls.append((self.public_name, arguments))
            if not self.results:
                raise AssertionError(f"no result queued for {self.public_name}")
            return self.results.pop(0)

    venv_python = "workspace/medical-booking/.venv/Scripts/python.exe"
    command = [venv_python, "-m", "pytest", "tests", "-q", "--disable-warnings"]
    failing_stdout = "tests/test_health.py:9: AssertionError\nE assert {'status': 'ok'} == {'status': 'healthy'}\n1 failed in 0.42s"
    final_answer = (
        "Proyecto: medical-booking\n"
        "Primera ejecución: 1 failed\n"
        "Archivo corregido: tests/test_health.py\n"
        'Corrección: expectativa "healthy" -> "ok"\n'
        'El endpoint ya devolvía {"status": "ok"}, tal como exigía el requerimiento original.\n'
        "Segunda ejecución: 1 passed\n"
        f"Python: {venv_python}"
    )
    list_client = SequencedToolClient("filesystem__list_files", [{"entries": [{"path": "medical-booking", "type": "directory"}]}])
    detect_client = SequencedToolClient("testing__detect_test_framework", [{"success": True, "framework": "pytest", "command": command}])
    prepare_client = SequencedToolClient(
        "testing__prepare_test_environment",
        [{"success": True, "project_name": "medical-booking", "python_executable": venv_python, "dependencies_installed": True}],
    )
    run_client = SequencedToolClient(
        "testing__run_tests",
        [
            {"success": False, "project_name": "medical-booking", "framework": "pytest", "command": command, "failure_type": "test_failure", "stdout": failing_stdout, "stderr": ""},
            {"success": True, "project_name": "medical-booking", "framework": "pytest", "command": command, "stdout": "1 passed, 2 warnings in 0.46s", "test_warning_count": 2},
        ],
    )
    read_client = SequencedToolClient(
        "filesystem__read_file",
        [
            {"success": True, "path": "medical-booking/tests/test_health.py", "content": 'assert resp.json() == {"status": "healthy"}'},
            {"success": True, "path": "medical-booking/medical_booking/main.py", "content": 'return {"status": "ok"}'},
        ],
    )
    update_client = SequencedToolClient("filesystem__update_project_files", [{"success": True, "project_name": "medical-booking", "files_updated": ["tests/test_health.py"]}])
    create_client = SequencedToolClient("filesystem__create_project_structure", [{"success": True}])
    register_tool(manager, "filesystem", "list_files", list_client, False)
    register_tool(manager, "filesystem", "read_file", read_client, False)
    register_tool(manager, "filesystem", "create_project_structure", create_client, True)
    register_tool(manager, "filesystem", "update_project_files", update_client, True)
    register_tool(manager, "testing", "detect_test_framework", detect_client, False)
    register_tool(manager, "testing", "prepare_test_environment", prepare_client, True)
    register_tool(manager, "testing", "run_tests", run_client, True)
    openai_client = FakeOpenAIClient(
        [
            FakeResponse([FakeFunctionCall("filesystem__list_files", "call-list", {"relative_path": "."})]),
            FakeResponse([FakeFunctionCall("testing__detect_test_framework", "call-detect", {"project_name": "medical-booking"})]),
            FakeResponse([FakeFunctionCall("testing__prepare_test_environment", "call-prepare", {"project_name": "medical-booking"})]),
            FakeResponse([FakeFunctionCall("testing__run_tests", "call-tests-1", {"project_name": "medical-booking"})]),
            FakeResponse([FakeFunctionCall("filesystem__read_file", "call-read-test", {"relative_path": "tests/test_health.py"})]),
            FakeResponse([FakeFunctionCall("filesystem__read_file", "call-read-source", {"relative_path": "medical_booking/main.py"})]),
            FakeResponse(
                [
                    FakeFunctionCall(
                        "filesystem__update_project_files",
                        "call-update",
                        {"project_name": "medical-booking", "files": [{"path": "tests/test_health.py", "content": 'assert resp.json() == {"status": "ok"}'}]},
                    )
                ]
            ),
            FakeResponse([FakeFunctionCall("testing__run_tests", "call-tests-2", {"project_name": "medical-booking"})]),
            FakeResponse([FakeMessage(final_answer)], output_text=final_answer),
        ]
    )
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions(), approvals=[True, True, True, True])

    final_text = await host.run_openai_loop(
        user_message("Revisa el proyecto medical-booking, ejecuta sus pruebas y corrige los errores encontrados.")
    )

    assert [name for name, _ in calls] == [
        "filesystem__list_files",
        "testing__detect_test_framework",
        "testing__prepare_test_environment",
        "testing__run_tests",
        "filesystem__read_file",
        "filesystem__read_file",
        "filesystem__update_project_files",
        "testing__run_tests",
    ]
    assert create_client.calls == []
    assert update_client.calls == [
        ("update_project_files", {"project_name": "medical-booking", "files": [{"path": "tests/test_health.py", "content": 'assert resp.json() == {"status": "ok"}'}]})
    ]
    assert len(update_client.calls) == 1
    assert len(run_client.calls) == 2
    assert host.state.workflow_intent == "review_existing_project"
    assert host.state.project_exists is True
    assert host.state.tests_passed is True
    assert host.state.repair_phase == "completed"
    assert host.state.first_test_result_summary == "1 failed"
    assert host.state.final_test_result_summary == "1 passed, 2 warnings"
    assert host.state.environment_python == venv_python
    assert host.state.repair_decision == "El endpoint ya cumplía el requerimiento original. Se corrigió el test."
    assert host.state.repair_before == '"status": "healthy"'
    assert host.state.repair_after == '"status": "ok"'
    assert read_client.calls == [
        ("read_file", {"relative_path": "medical-booking/tests/test_health.py"}),
        ("read_file", {"relative_path": "medical-booking/medical_booking/main.py"}),
    ]
    assert final_text == final_answer
    assert "Se alcanzó el límite general" not in final_text
    assert openai_client.responses.calls[-1]["tools"] == []


@pytest.mark.asyncio
async def test_logger_includes_error_exit_code_stdout_and_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    manager = FakeManager()
    client = RecordingToolClient(
        {
            "success": False,
            "exit_code": 1,
            "timed_out": False,
            "stdout": "assertion failed",
            "stderr": "ImportError: missing httpx",
        }
    )
    register_tool(manager, "testing", "run_tests", client, True)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions(), approvals=[True])
    host.state = ExecutionState(original_user_message="tests")

    await host.handle_function_call(FakeFunctionCall("testing__run_tests", "call-tests", {"project_name": "medical-booking"}))

    out = capsys.readouterr().out
    assert "public_tool: testing__run_tests" in out
    assert "server: testing" in out
    assert "original_tool: run_tests" in out
    assert "exit_code: 1" in out
    assert "timed_out: False" in out
    assert "stdout_preview: assertion failed" in out
    assert "stderr_preview: ImportError: missing httpx" in out


@pytest.mark.asyncio
async def test_repeated_analyze_requirement_is_blocked_after_completion() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True})
    register_tool(manager, "software_factory", "analyze_requirement", client, False)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(original_user_message="analiza", analysis_completed=True)

    output, executed_sensitive = await host.handle_function_call(
        FakeFunctionCall("software_factory__analyze_requirement", "call-analyze", {"requirement": "x"})
    )

    assert executed_sensitive is False
    assert decode_function_output(output)["status"] == "step_already_completed"
    assert client.calls == []


@pytest.mark.asyncio
async def test_repeated_create_tasks_is_blocked_after_completion() -> None:
    manager = FakeManager()
    client = RecordingToolClient({"success": True})
    register_tool(manager, "software_factory", "create_tasks", client, False)
    host = ApprovalHost(manager, FakeOpenAIClient([]), "test-model", build_base_instructions())
    host.state = ExecutionState(original_user_message="tareas", tasks_created=True)

    output, executed_sensitive = await host.handle_function_call(
        FakeFunctionCall("software_factory__create_tasks", "call-tasks", {"project_type": "x"})
    )

    assert executed_sensitive is False
    assert decode_function_output(output)["status"] == "step_already_completed"
    assert client.calls == []


@pytest.mark.asyncio
async def test_full_simulated_flow_creates_once_runs_tests_and_finishes() -> None:
    manager = FakeManager()
    analyze_client = RecordingToolClient({"success": True})
    tasks_client = RecordingToolClient({"success": True})
    list_client = RecordingToolClient({"success": True, "entries": []})
    create_client = RecordingToolClient({"success": True, "project_name": "medical-booking"})
    detect_client = RecordingToolClient({"success": True, "framework": "pytest"})
    tests_client = RecordingToolClient({"success": True, "project_name": "medical-booking"})
    register_tool(manager, "software_factory", "analyze_requirement", analyze_client, False)
    register_tool(manager, "software_factory", "create_tasks", tasks_client, False)
    register_tool(manager, "filesystem", "list_files", list_client, False)
    register_tool(manager, "filesystem", "create_project_structure", create_client, True)
    register_tool(manager, "testing", "detect_test_framework", detect_client, False)
    register_tool(manager, "testing", "run_tests", tests_client, True)
    openai_client = FakeOpenAIClient(
        [
            FakeResponse(
                [
                    FakeFunctionCall("software_factory__analyze_requirement", "call-1", {"requirement": "medical-booking"}),
                    FakeFunctionCall("software_factory__create_tasks", "call-2", {"project_type": "medical-booking"}),
                    FakeFunctionCall("filesystem__list_files", "call-3", {"relative_path": ".", "recursive": False}),
                    FakeFunctionCall(
                        "filesystem__create_project_structure",
                        "call-4",
                        {"project_name": "medical-booking", "files": [{"path": "app.py", "content": "ok"}]},
                    ),
                ]
            ),
            FakeResponse(
                [
                    FakeFunctionCall("testing__detect_test_framework", "call-5", {"project_name": "medical-booking"}),
                    FakeFunctionCall("testing__run_tests", "call-6", {"project_name": "medical-booking"}),
                ]
            ),
            FakeResponse([FakeMessage("Proyecto creado y pruebas exitosas.")], output_text="Proyecto creado y pruebas exitosas."),
        ]
    )
    host = ApprovalHost(manager, openai_client, "test-model", build_base_instructions(), approvals=[True, True])

    final_text = await host.run_openai_loop(
        user_message(
            'Crea un proyecto FastAPI llamado medical-booking con un endpoint GET /health que devuelva {"status": "ok"}, agrega pruebas y valida que pasen.'
        )
    )

    assert final_text == "Proyecto creado y pruebas exitosas."
    assert create_client.calls == [
        ("create_project_structure", {"project_name": "medical-booking", "files": [{"path": "app.py", "content": "ok"}]})
    ]
    assert tests_client.calls == [("run_tests", {"project_name": "medical-booking"})]
    assert host.state.created_project_name == "medical-booking"
    assert host.state.analysis_completed is True
    assert host.state.tasks_created is True
    assert host.state.workspace_inspected is True
    assert host.state.tests_executed is True
    assert host.state.tests_passed is True
    assert "Se alcanzó el límite general" not in final_text
