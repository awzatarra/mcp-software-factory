from __future__ import annotations

import pytest

from clients.tool_registry import ToolRegistry


class DummyClient:
    async def call_tool(self, tool_name: str, arguments: dict):
        return {"tool": tool_name, "arguments": arguments}


def test_registers_public_prefixed_names_and_lookup() -> None:
    registry = ToolRegistry()
    client = DummyClient()
    tool = registry.register(
        server_name="software-factory",
        original_name="analyze_requirement",
        description="Analyze",
        input_schema={"type": "object", "properties": {"requirement": {"type": "string"}}},
        requires_approval=False,
        client=client,
    )

    assert tool.public_name == "software_factory__analyze_requirement"
    assert registry.get("software_factory__analyze_requirement") is tool
    assert registry.all_tools() == [tool]


def test_detects_duplicate_public_names() -> None:
    registry = ToolRegistry()
    client = DummyClient()
    kwargs = {
        "server_name": "filesystem",
        "original_name": "list_files",
        "description": "",
        "input_schema": {},
        "requires_approval": False,
        "client": client,
    }
    registry.register(**kwargs)
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(**kwargs)


def test_converts_to_openai_function_tool_format_and_keeps_approval() -> None:
    registry = ToolRegistry()
    tool = registry.register(
        server_name="testing",
        original_name="run_tests",
        description="Run tests",
        input_schema={"type": "object", "properties": {"project_name": {"type": "string"}}},
        requires_approval=True,
        client=DummyClient(),
    )

    assert tool.requires_approval is True
    assert registry.to_openai_tools() == [
        {
            "type": "function",
            "name": "testing__run_tests",
            "description": "Run tests",
            "parameters": {"type": "object", "properties": {"project_name": {"type": "string"}}},
        }
    ]
