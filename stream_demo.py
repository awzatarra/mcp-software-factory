from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import AsyncExitStack

from dotenv import load_dotenv
from openai import AsyncOpenAI

from clients.mcp_manager import MCPClientManager
from graph.builder import build_software_factory_graph
from graph.checkpointing import create_sqlite_checkpointer
from graph.nodes import GraphDependencies
from graph.persistence_service import (
    PendingApprovalReference,
    WorkflowAlreadyCompletedError,
    WorkflowNotFoundError,
    WorkflowNotInterruptedError,
    WorkflowPersistenceService,
)
from graph.runtime import create_thread_id, run_software_factory_graph
from host import (
    BASE_INSTRUCTIONS,
    SENSITIVE_TOOLS,
    SoftwareFactoryHost,
    build_clients,
    load_planning_resources,
)
from streaming import (
    EventStatus,
    InMemoryWorkflowEventEmitter,
    SQLiteWorkflowEventSequenceStore,
    WorkflowEventFactory,
    WorkflowEventType,
    emit_workflow_event,
    workflow_event_context,
)
from streaming.consumer import TerminalWorkflowEventConsumer
from tool_executor import HostToolExecutor

DEFAULT_REQUEST = (
    'Crea un proyecto FastAPI llamado streaming-health-api con GET /health que '
    'devuelva {"status": "ok"}, agrega pruebas y valida que pasen.'
)
STOP_ON_APPROVAL = "stop_on_approval"
INTERACTIVE_APPROVALS = "interactive_approvals"
AUTO_APPROVE = "auto_approve"


def parse_approval_answer(value: str) -> bool:
    return value.strip().casefold() in {"y", "yes", "s", "si", "sí"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Demo de streaming estructurado de la Software Factory.")
    approval_modes = parser.add_mutually_exclusive_group()
    approval_modes.add_argument(
        "--stop-on-approval",
        dest="approval_mode",
        action="store_const",
        const=STOP_ON_APPROVAL,
        help="Detiene el demo en el primer interrupt sin resolverlo (predeterminado).",
    )
    approval_modes.add_argument(
        "--interactive-approvals",
        dest="approval_mode",
        action="store_const",
        const=INTERACTIVE_APPROVALS,
        help="Solicita una aceptación explícita antes de cada operación.",
    )
    approval_modes.add_argument(
        "--auto-approve",
        dest="approval_mode",
        action="store_const",
        const=AUTO_APPROVE,
        help="Aprueba automáticamente cada operación; úsalo solo en entornos controlados.",
    )
    parser.set_defaults(approval_mode=STOP_ON_APPROVAL)
    parser.add_argument(
        "--show-all-events",
        action="store_true",
        help="Muestra la secuencia técnica en lugar de la numeración compacta.",
    )
    parser.add_argument(
        "--resume-thread",
        metavar="THREAD_ID",
        help="Reanuda un workflow interrumpido desde su checkpoint SQLite actual.",
    )
    parser.add_argument("request", nargs="?", default=None)
    return parser


def validate_cli_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if args.resume_thread and args.request is not None:
        parser.error("Request is not allowed with --resume-thread")


def print_pending_reference(reference: PendingApprovalReference) -> None:
    print("Aprobación pendiente:")
    print(f"- Thread: {reference.thread_id}")
    print(f"- Checkpoint: {reference.checkpoint_id}")
    print(f"- Operación: {reference.operation}")
    print(f"- Tool: {reference.tool_name}")


def publish_resume_event(
    reference: PendingApprovalReference,
    *,
    event_emitter: InMemoryWorkflowEventEmitter,
    event_factory: WorkflowEventFactory,
) -> None:
    with workflow_event_context(
        thread_id=reference.thread_id,
        emitter=event_emitter,
        factory=event_factory,
        lineage={
            "lineage": "original",
            "branch_id": "original",
            "checkpoint_id": reference.checkpoint_id,
        },
    ):
        emit_workflow_event(
            WorkflowEventType.WORKFLOW_RESUMED,
            source="stream_demo",
            stage=None,
            status=EventStatus.RUNNING,
            data={
                "operation": reference.operation,
                "tool_name": reference.tool_name,
            },
        )


async def run_demo(
    request: str | None,
    *,
    resume_thread: str | None = None,
    approval_mode: str = STOP_ON_APPROVAL,
    show_all_events: bool = False,
) -> None:
    if resume_thread and request is not None:
        raise ValueError("Request is not allowed with --resume-thread")
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY no está configurada.")
    model = os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
    manager = MCPClientManager(build_clients(), SENSITIVE_TOOLS)
    async with AsyncExitStack() as stack:
        await manager.connect_all()
        stack.push_async_callback(manager.disconnect_all)
        resources = await load_planning_resources(manager)
        instructions = BASE_INSTRUCTIONS
        if resources:
            instructions += "\n\nAvailable MCP resources:\n" + resources
        client = AsyncOpenAI(api_key=api_key)
        host = SoftwareFactoryHost(manager, client, model, instructions)
        dependencies = GraphDependencies(
            tool_executor=HostToolExecutor(host),
            openai_client=client,
            model=model,
            instructions=instructions,
        )
        checkpointer = await stack.enter_async_context(create_sqlite_checkpointer())
        graph = build_software_factory_graph(dependencies, checkpointer=checkpointer)
        sequence_store = SQLiteWorkflowEventSequenceStore()
        event_emitter = InMemoryWorkflowEventEmitter(sequence_store=sequence_store)
        event_factory = WorkflowEventFactory()
        persistence = WorkflowPersistenceService(
            graph,
            streaming_enabled=True,
            event_emitter=event_emitter,
            event_factory=event_factory,
        )
        thread_id = resume_thread or create_thread_id()
        last_sequence = event_emitter.last_sequence(thread_id)
        event_emitter.subscribe(
            thread_id,
            TerminalWorkflowEventConsumer(
                compact_sequence=not show_all_events,
                start_sequence=last_sequence,
            ),
        )

        current: PendingApprovalReference | None = None
        result = None
        if resume_thread:
            current = await persistence.load_current_pending_approval(thread_id)
            publish_resume_event(
                current,
                event_emitter=event_emitter,
                event_factory=event_factory,
            )
        else:
            result = await run_software_factory_graph(
                graph,
                request or DEFAULT_REQUEST,
                thread_id=thread_id,
                streaming_enabled=True,
                event_emitter=event_emitter,
                event_factory=event_factory,
            )

        while current is not None or (result is not None and result.interrupted):
            current = current or await persistence.load_current_pending_approval(thread_id)
            print_pending_reference(current)
            if approval_mode == STOP_ON_APPROVAL:
                print("Demo detenido sin aprobar. El checkpoint permanece pendiente.")
                return
            if approval_mode == INTERACTIVE_APPROVALS:
                answer = input("Aprobar operación pendiente? [y/N]: ")
                if not parse_approval_answer(answer):
                    print("No hubo aceptación explícita. El checkpoint permanece pendiente.")
                    return
                reason = input("Razón opcional: ").strip() or None
            elif approval_mode == AUTO_APPROVE:
                reason = "Aprobación automática solicitada explícitamente por --auto-approve."
            else:
                raise ValueError(f"Modo de aprobación desconocido: {approval_mode}")
            host.reset_request_state()
            result = await persistence.approve(
                thread_id,
                reason,
                expected_reference=current,
            )
            current = None
        if result is not None and result.final_state.get("final_response"):
            print(result.final_state["final_response"])


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    validate_cli_args(parser, args)
    try:
        asyncio.run(
            run_demo(
                args.request,
                resume_thread=args.resume_thread,
                approval_mode=args.approval_mode,
                show_all_events=args.show_all_events,
            )
        )
    except WorkflowNotFoundError:
        parser.exit(2, "Thread not found\n")
    except (WorkflowAlreadyCompletedError, WorkflowNotInterruptedError):
        parser.exit(2, "Workflow is not waiting for approval\n")


if __name__ == "__main__":
    main()
