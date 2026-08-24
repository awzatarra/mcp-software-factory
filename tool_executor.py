from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal, Protocol
from uuid import uuid4

from api.services.observability_context import get_observability_context
from api.services.observability_sanitizer import error_fingerprint, safe_path, stable_hash

from graph.state import SoftwareFactoryState


@dataclass(frozen=True)
class ToolExecutionOutcome:
    public_tool_name: str
    arguments: dict[str, Any]
    payload: dict[str, Any] | None
    state_updates: dict[str, Any]
    executed_sensitive: bool = False
    is_error: bool = False


class ToolExecutor(Protocol):
    def openai_tool(self, public_tool_name: str) -> dict[str, Any]: ...

    async def execute(
        self,
        public_tool_name: str,
        arguments: dict[str, Any],
        *,
        state_context: SoftwareFactoryState,
        approval_mode: Literal["prompt", "already_approved"] = "prompt",
    ) -> ToolExecutionOutcome: ...


class _FunctionCall:
    type = "function_call"

    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.call_id = f"langgraph-{name}"
        self.arguments = json.dumps(arguments, ensure_ascii=False)


def _execution_state_from_graph(state: SoftwareFactoryState) -> Any:
    from host import ExecutionState

    allowed = {item.name for item in fields(ExecutionState)}
    values = {key: value for key, value in state.items() if key in allowed}
    values["requested_project_name"] = state.get("project_name")
    values["files_read_during_repair"] = set(state.get("files_read_during_repair", []))
    values["files_updated_during_repair"] = set(state.get("files_updated_during_repair", []))
    return ExecutionState(**values)


def _graph_values_from_execution(state: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    graph_keys = SoftwareFactoryState.__annotations__
    for key in graph_keys:
        if hasattr(state, key):
            value = getattr(state, key)
            values[key] = sorted(value) if isinstance(value, set) else value
    values["project_name"] = state.requested_project_name
    if state.user_cancelled:
        values["terminal_status"] = "user_cancelled"
    return values


def _decode_payload(function_output: dict[str, Any]) -> dict[str, Any] | None:
    raw = function_output.get("output")
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    data = decoded.get("data")
    return data if isinstance(data, dict) else decoded


def _transport_failed(function_output: dict[str, Any]) -> bool:
    raw = function_output.get("output")
    if not isinstance(raw, str):
        return False
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(decoded, dict) and decoded.get("transport_success") is False


def _with_knowledge_correlation(
    public_tool_name: str,
    arguments: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    effective = dict(arguments)
    if public_tool_name != "knowledge__get_relevant_context":
        return effective
    correlation = {
        "workflow_id": context.workflow_id,
        "branch_id": context.branch_id,
        "agent_name": context.agent or "Developer",
        "trace_id": context.trace_id,
        "parent_span_id": context.span_id,
    }
    effective.update({key: value for key, value in correlation.items() if value})
    return effective


class HostToolExecutor:
    """LangGraph adapter that preserves the Host's validation and approval path."""

    def __init__(self, host: Any, observability: Any | None = None) -> None:
        self.host = host
        self.observability = observability

    def openai_tool(self, public_tool_name: str) -> dict[str, Any]:
        tool = self.host.manager.registry.get(public_tool_name)
        return self.host.openai_tools_from_registered([tool])[0]

    async def execute(
        self,
        public_tool_name: str,
        arguments: dict[str, Any],
        *,
        state_context: SoftwareFactoryState,
        approval_mode: Literal["prompt", "already_approved"] = "prompt",
    ) -> ToolExecutionOutcome:
        before = dict(state_context)
        self.host.state = _execution_state_from_graph(state_context)
        context = get_observability_context()
        effective_arguments = _with_knowledge_correlation(
            public_tool_name, arguments, context
        )
        started = perf_counter()
        failure: Exception | None = None
        output: dict[str, Any] = {}
        try:
            if self.observability is not None:
                async with self.observability.span(
                    public_tool_name,
                    category="mcp",
                    kind="client",
                    attributes={"tool_name": public_tool_name, "approval_mode": approval_mode},
                ) as tool_context:
                    effective_arguments = _with_knowledge_correlation(
                        public_tool_name, effective_arguments, tool_context
                    )
                    output, executed_sensitive = await self.host.handle_function_call(
                        _FunctionCall(public_tool_name, effective_arguments), approval_mode=approval_mode,
                    )
            else:
                output, executed_sensitive = await self.host.handle_function_call(
                    _FunctionCall(public_tool_name, effective_arguments), approval_mode=approval_mode,
                )
        except Exception as exc:
            failure = exc
            raise
        finally:
            if self.observability is not None and context.trace_id:
                child = locals().get("tool_context", context)
                server_name = public_tool_name.partition("__")[0] or None
                await self.observability._safe(self.observability.store.add_tool_call, {
                    "call_id": uuid4().hex, "trace_id": context.trace_id,
                    "span_id": child.span_id or context.span_id, "workflow_id": context.workflow_id,
                    "tool_name": public_tool_name, "server_name": server_name,
                    "operation": approval_mode, "status": "failed" if failure else "completed",
                    "duration_ms": (perf_counter() - started) * 1000,
                    "error_fingerprint": error_fingerprint(type(failure).__name__, str(failure), public_tool_name) if failure else None,
                    "timestamp": datetime.now(UTC).isoformat(), "arguments_summary": effective_arguments,
                    "result_summary": {"transport_success": not failure, "executed_sensitive": locals().get("executed_sensitive", False)},
                })
                files = effective_arguments.get("files")
                if failure is None and isinstance(files, list):
                    for item in files:
                        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                            continue
                        content = item.get("content") if isinstance(item.get("content"), str) else ""
                        relative_path = safe_path(item["path"])
                        await self.observability._safe(self.observability.store.add_artifact, {
                            "artifact_id": stable_hash(f"{context.trace_id}:{public_tool_name}:{relative_path}:{stable_hash(content)}")[:32],
                            "trace_id": context.trace_id, "span_id": child.span_id or context.span_id,
                            "workflow_id": context.workflow_id, "artifact_type": "project_file",
                            "relative_path": relative_path, "content_hash": stable_hash(content),
                            "size_bytes": len(content.encode("utf-8")), "source": "live",
                            "created_at": datetime.now(UTC).isoformat(),
                        })
        after = _graph_values_from_execution(self.host.state)
        updates = {key: value for key, value in after.items() if before.get(key) != value}
        return ToolExecutionOutcome(
            public_tool_name=public_tool_name,
            arguments=effective_arguments,
            payload=_decode_payload(output),
            state_updates=updates,
            executed_sensitive=executed_sensitive,
            is_error=_transport_failed(output),
        )
