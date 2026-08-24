from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from streaming.catalog import WorkflowEventType


class EventStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    thread_id: str
    sequence: int
    type: WorkflowEventType
    timestamp: datetime
    source: str
    stage: str | None
    status: EventStatus
    message: str | None
    data: dict[str, Any]
