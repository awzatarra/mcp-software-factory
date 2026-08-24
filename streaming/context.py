from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from streaming.catalog import WorkflowEventType
from streaming.emitter import WorkflowEventEmitter
from streaming.errors import WorkflowEventStoreError
from streaming.factory import WorkflowEventFactory
from streaming.models import EventStatus, WorkflowEvent

_thread_id: ContextVar[str | None] = ContextVar("workflow_event_thread_id", default=None)
_emitter: ContextVar[WorkflowEventEmitter | None] = ContextVar("workflow_event_emitter", default=None)
_factory: ContextVar[WorkflowEventFactory | None] = ContextVar("workflow_event_factory", default=None)
_lineage: ContextVar[dict[str, Any]] = ContextVar("workflow_event_lineage", default={})


def get_workflow_event_context() -> tuple[str | None, dict[str, Any]]:
    return _thread_id.get(), dict(_lineage.get())


@contextmanager
def workflow_event_context(
    *,
    thread_id: str,
    emitter: WorkflowEventEmitter,
    factory: WorkflowEventFactory,
    lineage: Mapping[str, Any] | None = None,
) -> Iterator[None]:
    last_sequence = getattr(emitter, "last_sequence", None)
    if callable(last_sequence):
        try:
            factory.sequence.ensure_at_least(thread_id, int(last_sequence(thread_id)))
        except Exception:
            pass
    tokens = (
        (_thread_id, _thread_id.set(thread_id)),
        (_emitter, _emitter.set(emitter)),
        (_factory, _factory.set(factory)),
        (_lineage, _lineage.set(dict(lineage or {}))),
    )
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def emit_workflow_event(
    event_type: WorkflowEventType,
    *,
    source: str,
    stage: str | None,
    status: EventStatus,
    message: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> WorkflowEvent | None:
    thread_id = _thread_id.get()
    emitter = _emitter.get()
    factory = _factory.get()
    if thread_id is None or emitter is None or factory is None:
        return None
    try:
        event_data = {**_lineage.get(), **dict(data or {})}
        event = factory.create(
            thread_id=thread_id,
            event_type=event_type,
            source=source,
            stage=stage,
            status=status,
            message=message,
            data=event_data,
        )
        emitter.emit(event)
        try:
            from langgraph.config import get_stream_writer

            get_stream_writer()(event.model_dump(mode="json"))
        except Exception:
            pass
        return event
    except WorkflowEventStoreError:
        raise
    except Exception:
        return None
