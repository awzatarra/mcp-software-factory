from __future__ import annotations

from collections.abc import AsyncIterator
import logging

from fastapi import HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from api.services.event_broker import WorkflowEventBroker
from streaming import WorkflowEvent

logger = logging.getLogger(__name__)


def parse_last_event_id(value: str | None) -> int:
    if value is None or value.strip() == "":
        return 0
    try:
        parsed = int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a non-negative integer.") from exc
    if parsed < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a non-negative integer.")
    return parsed


class WorkflowSseService:
    def __init__(self, broker: WorkflowEventBroker) -> None:
        self.broker = broker

    async def _events(
        self,
        request: Request,
        thread_id: str,
        branch_id: str,
        after_sequence: int,
    ) -> AsyncIterator[dict[str, str]]:
        try:
            async for event in self.broker.subscribe(
                thread_id,
                branch_id=branch_id,
                after_sequence=after_sequence,
            ):
                if await request.is_disconnected():
                    return
                yield self.format_event(event)
        finally:
            logger.info("SSE disconnected thread=%s", thread_id)

    @staticmethod
    def format_event(event: WorkflowEvent) -> dict[str, str]:
        return {
            "id": str(event.sequence),
            "event": event.type.value,
            "data": event.model_dump_json(),
        }

    def response(
        self,
        request: Request,
        thread_id: str,
        *,
        branch_id: str = "original",
        last_event_id: str | None,
        after_sequence: int | None,
    ) -> EventSourceResponse:
        threshold = parse_last_event_id(last_event_id)
        if last_event_id is None and after_sequence is not None:
            if after_sequence < 0:
                raise HTTPException(status_code=400, detail="after_sequence must be non-negative.")
            threshold = after_sequence
        return EventSourceResponse(
            self._events(request, thread_id, branch_id, threshold),
            ping=15,
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
