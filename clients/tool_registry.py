from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ToolClient(Protocol):
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any: ...


def normalize_server_name(server_name: str) -> str:
    normalized = server_name.strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized


@dataclass(frozen=True)
class RegisteredTool:
    public_name: str
    original_name: str
    server_name: str
    description: str
    input_schema: dict[str, Any]
    requires_approval: bool
    client: ToolClient


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        *,
        server_name: str,
        original_name: str,
        description: str,
        input_schema: dict[str, Any] | None,
        requires_approval: bool,
        client: ToolClient,
    ) -> RegisteredTool:
        prefix = normalize_server_name(server_name)
        public_name = f"{prefix}__{original_name}"
        if public_name in self._tools:
            raise ValueError(f"duplicate tool public name: {public_name}")
        tool = RegisteredTool(
            public_name=public_name,
            original_name=original_name,
            server_name=prefix,
            description=description or "",
            input_schema=input_schema or {"type": "object", "properties": {}},
            requires_approval=requires_approval,
            client=client,
        )
        self._tools[public_name] = tool
        return tool

    def get(self, public_name: str) -> RegisteredTool:
        try:
            return self._tools[public_name]
        except KeyError as exc:
            raise KeyError(f"tool not registered: {public_name}") from exc

    def all_tools(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool.public_name,
                "description": tool.description,
                "parameters": tool.input_schema,
            }
            for tool in self.all_tools()
        ]
