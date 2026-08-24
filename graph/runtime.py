from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from graph.state import SoftwareFactoryState, create_initial_state
from graph.debug import state_debug_summary
from streaming import (
    EventStatus,
    WorkflowEventEmitter,
    WorkflowEventFactory,
    WorkflowEventType,
    default_event_emitter,
    default_event_factory,
    emit_workflow_event,
    workflow_event_context,
)
from streaming.mapper import LangGraphEventMapper


def _debug_enabled() -> bool:
    return os.getenv("MCP_FACTORY_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}


def _streaming_enabled() -> bool:
    return os.getenv("WORKFLOW_STREAMING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class WorkflowRunResult:
    thread_id: str
    final_state: SoftwareFactoryState
    already_completed: bool = False
    interrupted: bool = False
    interrupts: tuple[WorkflowInterrupt, ...] = ()


@dataclass(frozen=True)
class WorkflowInterrupt:
    interrupt_id: str
    value: dict[str, Any]


def create_thread_id() -> str:
    return str(uuid4())


def thread_config(thread_id: str) -> dict[str, dict[str, Any]]:
    return {"configurable": {"thread_id": thread_id}}


def normalize_interrupts(raw_interrupts: Any) -> tuple[WorkflowInterrupt, ...]:
    if not raw_interrupts:
        return ()
    normalized: list[WorkflowInterrupt] = []
    for item in raw_interrupts:
        value = getattr(item, "value", None)
        if not isinstance(value, dict):
            value = {"value": value}
        interrupt_id = getattr(item, "id", None) or getattr(item, "interrupt_id", None) or "unknown"
        normalized.append(WorkflowInterrupt(interrupt_id=str(interrupt_id), value=value))
    return tuple(normalized)


def invocation_interrupts(result: Any) -> tuple[WorkflowInterrupt, ...]:
    if isinstance(result, dict):
        return normalize_interrupts(result.get("__interrupt__"))
    return normalize_interrupts(getattr(result, "interrupts", ()))


async def active_snapshot(graph: Any, config: dict[str, Any]) -> Any:
    try:
        return await graph.aget_state(config, subgraphs=True)
    except TypeError:
        return await graph.aget_state(config)


def snapshot_interrupts(snapshot: Any) -> tuple[WorkflowInterrupt, ...]:
    found: list[WorkflowInterrupt] = []
    found.extend(normalize_interrupts(getattr(snapshot, "interrupts", ())))
    for task in getattr(snapshot, "tasks", ()) or ():
        found.extend(normalize_interrupts(getattr(task, "interrupts", ())))
        child = getattr(task, "state", None)
        if child is not None and not isinstance(child, dict) and hasattr(child, "values"):
            found.extend(snapshot_interrupts(child))
    unique: dict[str, WorkflowInterrupt] = {item.interrupt_id: item for item in found}
    return tuple(unique.values())


def active_snapshot_values(snapshot: Any) -> SoftwareFactoryState:
    values: SoftwareFactoryState = dict(snapshot.values)
    for task in getattr(snapshot, "tasks", ()) or ():
        child = getattr(task, "state", None)
        if child is None or isinstance(child, dict) or not hasattr(child, "values"):
            continue
        values.update(active_snapshot_values(child))
    return values


async def workflow_result_from_invocation(
    graph: Any,
    thread_id: str,
    invocation_result: Any,
    *,
    already_completed: bool = False,
    snapshot_config: dict[str, Any] | None = None,
) -> WorkflowRunResult:
    interrupts = invocation_interrupts(invocation_result)
    if interrupts:
        snapshot = await active_snapshot(graph, snapshot_config or thread_config(thread_id))
        final_state = active_snapshot_values(snapshot)
    else:
        final_state = dict(invocation_result) if isinstance(invocation_result, dict) else dict(invocation_result.value)
        final_state.pop("__interrupt__", None)
    return WorkflowRunResult(
        thread_id=thread_id,
        final_state=final_state,
        already_completed=already_completed,
        interrupted=bool(interrupts),
        interrupts=interrupts,
    )


def print_interrupt_request(thread_id: str, workflow_interrupt: WorkflowInterrupt) -> None:
    value = workflow_interrupt.value
    preview = value.get("preview") if isinstance(value.get("preview"), dict) else {}
    print("Workflow pausado para aprobación")
    print(f"Thread: {thread_id}")
    print(f"Tool: {value.get('public_tool_name') or 'n/a'}")
    print(f"Proyecto: {value.get('project_name') or 'n/a'}")
    files = preview.get("files") if isinstance(preview, dict) else None
    if isinstance(files, list) and files:
        print("Archivos:")
        for file in files:
            if isinstance(file, dict):
                print(f"- {file.get('path')}")
    normalized_dependencies = preview.get("normalized_dependencies")
    if isinstance(normalized_dependencies, list) and normalized_dependencies:
        print("Dependencias normalizadas:")
        for dependency in normalized_dependencies:
            print(f"- {dependency}")
    if preview.get("test_framework"):
        print(f"Test framework: {preview['test_framework']}")
    if "collectable_test_count" in preview:
        print(f"Collectable tests: {preview['collectable_test_count']}")
    test_functions = preview.get("test_functions")
    if isinstance(test_functions, list) and test_functions:
        print("Test functions:")
        for function_name in test_functions:
            print(f"- {function_name}")
    if "before" in preview or "after" in preview:
        print(f"Antes: {preview.get('before') or 'n/a'}")
        print(f"Después: {preview.get('after') or 'n/a'}")
    if preview.get("expected_command"):
        print(f"Comando esperado: {preview['expected_command']}")


async def run_software_factory_graph(
    graph: Any,
    user_message: str,
    thread_id: str | None = None,
    *,
    streaming_enabled: bool | None = None,
    event_emitter: WorkflowEventEmitter | None = None,
    event_factory: WorkflowEventFactory | None = None,
    lineage: dict[str, Any] | None = None,
) -> WorkflowRunResult:
    workflow_thread_id = thread_id or create_thread_id()
    print(f"Workflow thread: {workflow_thread_id}")
    initial_state = create_initial_state(user_message)
    initial_state["workflow_id"] = workflow_thread_id
    use_streaming = _streaming_enabled() if streaming_enabled is None else streaming_enabled
    if use_streaming:
        emitter = event_emitter or default_event_emitter
        factory = event_factory or default_event_factory
        event_lineage = lineage or {"lineage": "original", "branch_id": "original"}
        mapper = LangGraphEventMapper(factory, base_data=event_lineage)
        terminal_event_emitted = False
        with workflow_event_context(
            thread_id=workflow_thread_id,
            emitter=emitter,
            factory=factory,
            lineage=event_lineage,
        ):
            try:
                from host import extract_requested_project_name, extract_workflow_intent

                requested_project = extract_requested_project_name(user_message)
                workflow_intent = extract_workflow_intent(user_message, requested_project)
            except Exception:
                requested_project = None
                workflow_intent = None
            emit_workflow_event(
                WorkflowEventType.WORKFLOW_STARTED,
                source="graph.runtime",
                stage=None,
                status=EventStatus.RUNNING,
                data={
                    "project_name": requested_project,
                    "workflow_intent": workflow_intent,
                },
            )
            async for chunk in graph.astream(
                initial_state,
                config=thread_config(workflow_thread_id),
                stream_mode=["updates", "custom"],
                subgraphs=True,
            ):
                if not isinstance(chunk, tuple) or len(chunk) != 3:
                    continue
                namespace, mode, payload = chunk
                if mode != "updates" or not isinstance(payload, dict):
                    continue
                for node_name, update in payload.items():
                    if not isinstance(update, dict):
                        continue
                    events = mapper.map_update(
                        thread_id=workflow_thread_id,
                        namespace=tuple(str(item).split(":", 1)[0] for item in namespace),
                        node_name=str(node_name),
                        update=update,
                    )
                    for event in events:
                        emitter.emit(event)
                        if event.type in {
                            WorkflowEventType.WORKFLOW_COMPLETED,
                            WorkflowEventType.WORKFLOW_FAILED,
                        }:
                            terminal_event_emitted = True
            snapshot = await active_snapshot(graph, thread_config(workflow_thread_id))
            interrupts = snapshot_interrupts(snapshot)
            final_state = active_snapshot_values(snapshot)
            result = WorkflowRunResult(
                thread_id=workflow_thread_id,
                final_state=final_state,
                interrupted=bool(interrupts),
                interrupts=interrupts,
            )
            if not result.interrupted and not terminal_event_emitted:
                completed = result.final_state.get("terminal_status") == "completed"
                emit_workflow_event(
                    WorkflowEventType.WORKFLOW_COMPLETED if completed else WorkflowEventType.WORKFLOW_FAILED,
                    source="graph.runtime",
                    stage="finalize",
                    status=EventStatus.COMPLETED if completed else EventStatus.FAILED,
                    data={"terminal_status": result.final_state.get("terminal_status")},
                )
    else:
        invocation_result = await graph.ainvoke(
            initial_state,
            config=thread_config(workflow_thread_id),
        )
        result = await workflow_result_from_invocation(graph, workflow_thread_id, invocation_result)
    if result.interrupted:
        for workflow_interrupt in result.interrupts:
            print_interrupt_request(workflow_thread_id, workflow_interrupt)
    elif result.final_state.get("final_response"):
        print(result.final_state["final_response"])
    if _debug_enabled():
        print("LangGraph final state (debug):")
        print(json.dumps(state_debug_summary(result.final_state), ensure_ascii=False, indent=2))
    print("Workflow guardado.")
    print(f"Consultar: /workflow {workflow_thread_id}")
    return result
