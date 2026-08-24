from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPClientError(RuntimeError):
    """Base error for safe MCP client failures."""


class MCPConnectionTimeout(MCPClientError):
    def __init__(self, server_name: str, timeout_seconds: float) -> None:
        super().__init__(
            f"MCP server '{server_name}' did not connect within {timeout_seconds:g} seconds."
        )
        self.server_name = server_name
        self.timeout_seconds = timeout_seconds


class MCPToolTimeout(MCPClientError):
    def __init__(self, server_name: str, tool_name: str, timeout_seconds: float) -> None:
        super().__init__(
            f"MCP tool '{server_name}__{tool_name}' did not respond within {timeout_seconds:g} seconds."
        )
        self.server_name = server_name
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds


class MCPTransportError(MCPClientError):
    def __init__(self, server_name: str, message: str) -> None:
        super().__init__(f"MCP transport error for '{server_name}': {message}")
        self.server_name = server_name


def _positive_timeout_from_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


@dataclass(frozen=True)
class MCPClientTimeoutConfig:
    connect_timeout_seconds: float = 15.0
    tool_timeout_seconds: float = 30.0
    disconnect_timeout_seconds: float = 10.0
    server_tool_timeout_seconds: dict[str, float] | None = None
    tool_timeout_overrides_seconds: dict[tuple[str, str], float] | None = None

    @classmethod
    def from_environment(cls) -> "MCPClientTimeoutConfig":
        server_overrides: dict[str, float] = {}
        tool_overrides: dict[tuple[str, str], float] = {}
        for key, raw_value in os.environ.items():
            if not key.startswith("MCP_TOOL_TIMEOUT_") or not key.endswith("_SECONDS"):
                continue
            middle = key.removeprefix("MCP_TOOL_TIMEOUT_").removesuffix("_SECONDS")
            if middle in {"", "SECONDS"}:
                continue
            if middle in {"CONNECT", "DISCONNECT"}:
                continue
            value = _positive_timeout_from_env(key, 0)
            normalized = _normalize_timeout_key(middle)
            if "_" in normalized:
                server, tool = normalized.split("_", 1)
                tool_overrides[(server, tool)] = value
            else:
                server_overrides[normalized] = value
        return cls(
            connect_timeout_seconds=_positive_timeout_from_env(
                "MCP_CONNECT_TIMEOUT_SECONDS", 15.0
            ),
            tool_timeout_seconds=_positive_timeout_from_env(
                "MCP_TOOL_TIMEOUT_SECONDS", 30.0
            ),
            disconnect_timeout_seconds=_positive_timeout_from_env(
                "MCP_DISCONNECT_TIMEOUT_SECONDS", 10.0
            ),
            server_tool_timeout_seconds=server_overrides,
            tool_timeout_overrides_seconds=tool_overrides,
        )

    def tool_timeout_for(self, server_name: str, tool_name: str) -> float:
        server = _normalize_timeout_key(server_name)
        tool = _normalize_timeout_key(tool_name)
        exact_tool_env = f"MCP_TOOL_TIMEOUT_{server}_{tool}_SECONDS".upper()
        exact_server_env = f"MCP_TOOL_TIMEOUT_{server}_SECONDS".upper()
        tool_overrides = self.tool_timeout_overrides_seconds or {}
        server_overrides = self.server_tool_timeout_seconds or {}
        return (
            _optional_positive_timeout_from_env(exact_tool_env)
            or _optional_positive_timeout_from_env(exact_server_env)
            or tool_overrides.get((server, tool))
            or server_overrides.get(server)
            or self.tool_timeout_seconds
        )


def _optional_positive_timeout_from_env(name: str) -> float | None:
    return _positive_timeout_from_env(name, 0) if os.getenv(name) else None


def _normalize_timeout_key(value: str) -> str:
    return "_".join(
        part for part in "".join(
            character if character.isalnum() else "_" for character in str(value)
        ).casefold().split("_") if part
    )


class MCPServerClient:
    def __init__(
        self,
        server_name: str,
        server_script: str,
        *,
        timeouts: MCPClientTimeoutConfig | None = None,
        observability: Any | None = None,
    ) -> None:
        self.server_name = server_name
        self.server_script = str(Path(server_script).resolve())
        self.timeouts = timeouts or MCPClientTimeoutConfig.from_environment()
        self.observability = observability
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    async def connect(self) -> None:
        if self._session is not None:
            return
        script = Path(self.server_script)
        if not script.exists():
            raise FileNotFoundError(f"MCP server script not found: {script}")
        stack = AsyncExitStack()
        started = asyncio.get_running_loop().time()
        status = "completed"
        try:
            async def open_session() -> ClientSession:
                environment = dict(os.environ)
                repository_root = str(script.parent.parent)
                existing_pythonpath = environment.get("PYTHONPATH")
                environment["PYTHONPATH"] = (
                    repository_root
                    if not existing_pythonpath
                    else repository_root + os.pathsep + existing_pythonpath
                )
                params = StdioServerParameters(command=sys.executable, args=[str(script)], env=environment)
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                return session

            session = await asyncio.wait_for(
                open_session(),
                timeout=self.timeouts.connect_timeout_seconds,
            )
            self._exit_stack = stack
            self._session = session
        except TimeoutError as exc:
            status = "timeout"
            with suppress(MCPTransportError):
                await self._close_stack(stack)
            raise MCPConnectionTimeout(
                self.server_name, self.timeouts.connect_timeout_seconds
            ) from exc
        except Exception:
            status = "failed"
            with suppress(MCPTransportError):
                await self._close_stack(stack)
            raise
        finally:
            await self._record_observability(
                "mcp.client.connect",
                status=status,
                timeout_seconds=self.timeouts.connect_timeout_seconds,
                duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )

    async def disconnect(self) -> None:
        stack = self._exit_stack
        self._exit_stack = None
        self._session = None
        if stack is not None:
            await self._close_stack(stack)

    async def _close_stack(self, stack: AsyncExitStack) -> None:
        try:
            await asyncio.wait_for(
                stack.aclose(),
                timeout=self.timeouts.disconnect_timeout_seconds,
            )
        except TimeoutError as exc:
            raise MCPTransportError(
                self.server_name,
                f"disconnect exceeded {self.timeouts.disconnect_timeout_seconds:g} seconds",
            ) from exc

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError(f"MCP client is not connected: {self.server_name}")
        return self._session

    async def list_tools(self) -> Any:
        return await self._call_session("list_tools")

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        started = asyncio.get_running_loop().time()
        timeout_seconds = self.timeouts.tool_timeout_for(self.server_name, tool_name)
        try:
            result = await asyncio.wait_for(
                self._require_session().call_tool(tool_name, arguments),
                timeout=timeout_seconds,
            )
            await self._record_observability(
                "mcp.client.call_tool",
                tool_name=tool_name,
                status="completed",
                timeout_seconds=timeout_seconds,
                duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )
            return result
        except TimeoutError as exc:
            await self._record_observability(
                "mcp.client.call_tool",
                tool_name=tool_name,
                status="timeout",
                timeout_seconds=timeout_seconds,
                duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )
            await self._discard_corrupt_session()
            raise MCPToolTimeout(
                self.server_name, tool_name, timeout_seconds
            ) from exc

    async def list_resources(self) -> Any:
        return await self._call_session("list_resources")

    async def read_resource(self, uri: str) -> Any:
        return await self._call_session("read_resource", uri)

    async def list_prompts(self) -> Any:
        return await self._call_session("list_prompts")

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await self._call_session("get_prompt", name, arguments or {})

    async def _call_session(self, method_name: str, *args: Any) -> Any:
        try:
            return await getattr(self._require_session(), method_name)(*args)
        except Exception as exc:
            raise MCPTransportError(self.server_name, method_name) from exc

    async def _discard_corrupt_session(self) -> None:
        stack = self._exit_stack
        self._exit_stack = None
        self._session = None
        if stack is not None:
            try:
                await self._close_stack(stack)
            except MCPTransportError:
                pass

    async def _record_observability(
        self,
        operation: str,
        *,
        status: str,
        timeout_seconds: float,
        duration_ms: float,
        tool_name: str | None = None,
    ) -> None:
        if self.observability is None:
            return
        attributes = {
            "server_name": self.server_name,
            "timeout_seconds": timeout_seconds,
            "duration_ms": duration_ms,
            "status": status,
        }
        if tool_name is not None:
            attributes["tool_name"] = tool_name
        try:
            async with self.observability.span(
                operation,
                category="mcp",
                kind="client",
                attributes=attributes,
            ):
                return
        except Exception:
            return
