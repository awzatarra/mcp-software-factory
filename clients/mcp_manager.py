from __future__ import annotations

from typing import Any

from clients.mcp_client import MCPServerClient
from clients.tool_registry import ToolRegistry, normalize_server_name


class MCPClientManager:
    def __init__(
        self,
        clients: list[MCPServerClient],
        approval_required: set[tuple[str, str]] | None = None,
    ) -> None:
        self.clients = clients
        self.approval_required = {
            (normalize_server_name(server), tool) for server, tool in (approval_required or set())
        }
        self.registry = ToolRegistry()

    async def connect_all(self) -> ToolRegistry:
        connected: list[MCPServerClient] = []
        try:
            for client in self.clients:
                print(f"Conectando MCP server: {client.server_name}")
                await client.connect()
                connected.append(client)
                await self._register_client_tools(client)
            return self.registry
        except Exception:
            for client in reversed(connected):
                await client.disconnect()
            raise

    async def _register_client_tools(self, client: MCPServerClient) -> None:
        response = await client.list_tools()
        tools = getattr(response, "tools", response)
        print(f"Descubiertas {len(tools)} tools en {client.server_name}")
        for tool in tools:
            name = getattr(tool, "name", None) or tool["name"]
            description = getattr(tool, "description", None) or ""
            input_schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
            requires = (normalize_server_name(client.server_name), name) in self.approval_required
            registered = self.registry.register(
                server_name=client.server_name,
                original_name=name,
                description=description,
                input_schema=input_schema,
                requires_approval=requires,
                client=client,
            )
            marker = " requiere aprobación" if requires else ""
            print(f"  - {registered.public_name}{marker}")

    async def disconnect_all(self) -> None:
        errors: list[Exception] = []
        for client in reversed(self.clients):
            try:
                await client.disconnect()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError(f"errors while disconnecting MCP clients: {errors}")

    def get_client(self, server_name: str) -> MCPServerClient:
        normalized = normalize_server_name(server_name)
        for client in self.clients:
            if normalize_server_name(client.server_name) == normalized:
                return client
        raise KeyError(f"MCP client not found: {server_name}")

    async def list_all_prompts(self) -> list[dict[str, Any]]:
        prompts: list[dict[str, Any]] = []
        for client in self.clients:
            response = await client.list_prompts()
            for prompt in getattr(response, "prompts", response):
                prompts.append(
                    {
                        "server": normalize_server_name(client.server_name),
                        "name": getattr(prompt, "name", ""),
                        "description": getattr(prompt, "description", "") or "",
                    }
                )
        return prompts
