from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

import clients.mcp_client as mcp_client
from clients.mcp_client import (
    MCPClientTimeoutConfig,
    MCPConnectionTimeout,
    MCPServerClient,
    MCPToolTimeout,
)


class FakeExitStack:
    def __init__(self) -> None:
        self.closed = False
        self._contexts: list[Any] = []

    async def enter_async_context(self, context: Any) -> Any:
        value = await context.__aenter__()
        self._contexts.append(context)
        return value

    async def aclose(self) -> None:
        for context in reversed(self._contexts):
            await context.__aexit__(None, None, None)
        self.closed = True


class SlowSession:
    def __init__(self, delay: float = 1.0) -> None:
        self.delay = delay

    async def call_tool(self, _tool_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(self.delay)
        return {"success": True}


class FastSession:
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "tool": tool_name, "arguments": arguments}


class Observability:
    def __init__(self) -> None:
        self.spans: list[tuple[str, dict[str, Any]]] = []

    @asynccontextmanager
    async def span(self, name: str, **kwargs: Any):
        self.spans.append((name, kwargs.get("attributes") or {}))
        yield None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MCP_CONNECT_TIMEOUT_SECONDS", "0"),
        ("MCP_TOOL_TIMEOUT_SECONDS", "-1"),
        ("MCP_DISCONNECT_TIMEOUT_SECONDS", "abc"),
    ],
)
def test_timeout_config_values_are_validated(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        MCPClientTimeoutConfig.from_environment()


def test_timeout_config_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_CONNECT_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("MCP_DISCONNECT_TIMEOUT_SECONDS", "3.5")

    config = MCPClientTimeoutConfig.from_environment()

    assert config.connect_timeout_seconds == 1.5
    assert config.tool_timeout_seconds == 2.5
    assert config.disconnect_timeout_seconds == 3.5


def test_global_timeout_is_fallback() -> None:
    config = MCPClientTimeoutConfig(tool_timeout_seconds=30)

    assert config.tool_timeout_for("git", "init") == 30


def test_server_specific_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_TESTING_SECONDS", "120")
    config = MCPClientTimeoutConfig(tool_timeout_seconds=30)

    assert config.tool_timeout_for("testing", "detect_test_framework") == 120
    assert config.tool_timeout_for("git", "init") == 30


def test_tool_specific_override_wins_over_server_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_TESTING_SECONDS", "120")
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_TESTING_PREPARE_TEST_ENVIRONMENT_SECONDS", "180")
    config = MCPClientTimeoutConfig(tool_timeout_seconds=30)

    assert config.tool_timeout_for("testing", "prepare_test_environment") == 180
    assert config.tool_timeout_for("testing", "run_tests") == 120


@pytest.mark.asyncio
async def test_connect_timeout_closes_exit_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "server.py"
    script.write_text("pass\n", encoding="utf-8")
    fake_stack = FakeExitStack()

    class StackFactory:
        def __call__(self) -> FakeExitStack:
            return fake_stack

    @asynccontextmanager
    async def slow_stdio(_params: Any):
        await asyncio.sleep(1)
        yield object(), object()

    monkeypatch.setattr(mcp_client, "AsyncExitStack", StackFactory())
    monkeypatch.setattr(mcp_client, "stdio_client", slow_stdio)
    client = MCPServerClient(
        "slow",
        str(script),
        timeouts=MCPClientTimeoutConfig(
            connect_timeout_seconds=0.01,
            tool_timeout_seconds=1,
            disconnect_timeout_seconds=1,
        ),
    )

    with pytest.raises(MCPConnectionTimeout):
        await client.connect()

    assert fake_stack.closed is True
    assert client.is_connected is False


@pytest.mark.asyncio
async def test_tool_call_timeout_closes_session_and_exit_stack() -> None:
    client = MCPServerClient(
        "slow",
        __file__,
        timeouts=MCPClientTimeoutConfig(
            connect_timeout_seconds=1,
            tool_timeout_seconds=0.01,
            disconnect_timeout_seconds=1,
        ),
    )
    stack = FakeExitStack()
    client._session = SlowSession()  # type: ignore[assignment]
    client._exit_stack = stack  # type: ignore[assignment]

    with pytest.raises(MCPToolTimeout) as captured:
        await client.call_tool("sleep", {"secret": "not logged"})

    assert captured.value.server_name == "slow"
    assert captured.value.tool_name == "sleep"
    assert stack.closed is True
    assert client.is_connected is False
    assert client._exit_stack is None


@pytest.mark.asyncio
async def test_testing_prepare_environment_over_global_timeout_can_complete_with_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_TESTING_PREPARE_TEST_ENVIRONMENT_SECONDS", "0.2")
    client = MCPServerClient("testing", __file__)
    client._session = SlowSession(delay=0.05)  # type: ignore[assignment]

    result = await client.call_tool("prepare_test_environment", {})

    assert result == {"success": True}


@pytest.mark.asyncio
async def test_git_keeps_short_timeout_when_testing_override_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_TESTING_SECONDS", "0.2")
    client = MCPServerClient("git", __file__)
    client._session = SlowSession(delay=0.05)  # type: ignore[assignment]
    client._exit_stack = FakeExitStack()  # type: ignore[assignment]

    with pytest.raises(MCPToolTimeout) as captured:
        await client.call_tool("init", {})

    assert captured.value.timeout_seconds == 0.01


@pytest.mark.asyncio
async def test_next_call_after_timeout_requires_reconnect() -> None:
    client = MCPServerClient(
        "slow",
        __file__,
        timeouts=MCPClientTimeoutConfig(tool_timeout_seconds=0.01),
    )
    client._session = SlowSession()  # type: ignore[assignment]
    client._exit_stack = FakeExitStack()  # type: ignore[assignment]

    with pytest.raises(MCPToolTimeout):
        await client.call_tool("sleep", {})
    with pytest.raises(RuntimeError, match="not connected"):
        await client.call_tool("sleep", {})


@pytest.mark.asyncio
async def test_successful_tool_calls_are_unchanged() -> None:
    client = MCPServerClient("ok", __file__)
    client._session = FastSession()  # type: ignore[assignment]

    result = await client.call_tool("echo", {"value": 1})

    assert result == {"success": True, "tool": "echo", "arguments": {"value": 1}}


@pytest.mark.asyncio
async def test_observability_records_tool_timeout_without_arguments() -> None:
    observability = Observability()
    client = MCPServerClient(
        "slow",
        __file__,
        observability=observability,
        timeouts=MCPClientTimeoutConfig(tool_timeout_seconds=0.01),
    )
    client._session = SlowSession()  # type: ignore[assignment]
    client._exit_stack = FakeExitStack()  # type: ignore[assignment]

    with pytest.raises(MCPToolTimeout):
        await client.call_tool("sleep", {"api_key": "secret"})

    assert observability.spans
    name, attributes = observability.spans[-1]
    assert name == "mcp.client.call_tool"
    assert attributes["server_name"] == "slow"
    assert attributes["tool_name"] == "sleep"
    assert attributes["status"] == "timeout"
    assert attributes["timeout_seconds"] == 0.01
    assert "api_key" not in attributes
