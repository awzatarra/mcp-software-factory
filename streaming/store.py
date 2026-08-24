from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from streaming.models import WorkflowEvent


class WorkflowEventStore(Protocol):
    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def reserve_next_sequence(
        self,
        thread_id: str,
        branch_id: str,
    ) -> int: ...

    async def append(self, event: WorkflowEvent) -> bool: ...

    async def append_many(self, events: Sequence[WorkflowEvent]) -> int: ...

    async def get_events(
        self,
        thread_id: str,
        *,
        branch_id: str = "original",
        after_sequence: int | None = None,
        limit: int | None = None,
        event_types: Sequence[str] | None = None,
    ) -> list[WorkflowEvent]: ...

    async def get_last_sequence(
        self,
        thread_id: str,
        branch_id: str = "original",
    ) -> int: ...

    async def get_terminal_event(
        self,
        thread_id: str,
        branch_id: str = "original",
    ) -> WorkflowEvent | None: ...

    async def prune(
        self,
        *,
        retention_days: int | None = None,
        max_per_thread: int | None = None,
    ) -> int: ...
