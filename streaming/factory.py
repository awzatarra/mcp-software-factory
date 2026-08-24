from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from streaming.catalog import WorkflowEventType
from streaming.models import EventStatus, WorkflowEvent
from streaming.sanitizer import sanitize_event_data
from streaming.sequence import WorkflowEventSequence


class WorkflowEventFactory:
    def __init__(self, sequence: WorkflowEventSequence | None = None) -> None:
        self.sequence = sequence or WorkflowEventSequence()

    def create(
        self,
        *,
        thread_id: str,
        event_type: WorkflowEventType,
        source: str,
        stage: str | None,
        status: EventStatus,
        message: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> WorkflowEvent:
        return WorkflowEvent(
            event_id=uuid4(),
            thread_id=thread_id,
            sequence=self.sequence.next(thread_id),
            type=event_type,
            timestamp=datetime.now(UTC),
            source=source,
            stage=stage,
            status=status,
            message=message,
            data=sanitize_event_data(data),
        )


default_event_factory = WorkflowEventFactory()
