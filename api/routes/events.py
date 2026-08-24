from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from api.dependencies import ApiServices, get_services
from api.models import WorkflowEventHistoryResponse
from graph.persistence_service import WorkflowNotFoundError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workflows", tags=["events"])


@router.get(
    "/{thread_id}/events/history",
    response_model=WorkflowEventHistoryResponse,
)
async def workflow_event_history(
    thread_id: str,
    branch_id: Annotated[str, Query(min_length=1, max_length=200)] = "original",
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    event_type: Annotated[list[str] | None, Query()] = None,
    services: ApiServices = Depends(get_services),
) -> WorkflowEventHistoryResponse:
    events = await services.event_store.get_events(
        thread_id,
        branch_id=branch_id,
        after_sequence=after_sequence,
        limit=limit + 1,
        event_types=event_type,
    )
    if not events:
        execution = await services.registry.get(thread_id)
        if execution is None:
            try:
                await services.query.get_snapshot(thread_id)
            except WorkflowNotFoundError as exc:
                raise HTTPException(status_code=404, detail="Thread not found") from exc
    has_more = len(events) > limit
    return WorkflowEventHistoryResponse(
        thread_id=thread_id,
        branch_id=branch_id,
        events=events[:limit],
        last_sequence=await services.event_store.get_last_sequence(
            thread_id,
            branch_id,
        ),
        has_more=has_more,
    )


@router.get("/{thread_id}/events")
async def workflow_events(
    thread_id: str,
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after_sequence: Annotated[int | None, Query(ge=0)] = None,
    branch_id: Annotated[str, Query(min_length=1, max_length=200)] = "original",
    services: ApiServices = Depends(get_services),
):
    execution = await services.registry.get(thread_id)
    if execution is None:
        try:
            await services.query.get_snapshot(thread_id)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc
    logger.info("SSE connected thread=%s", thread_id)
    response = services.sse.response(
        request,
        thread_id,
        branch_id=branch_id,
        last_event_id=last_event_id,
        after_sequence=after_sequence,
    )
    return response
