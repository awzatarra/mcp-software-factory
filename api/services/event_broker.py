from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
import logging

from streaming import WorkflowEvent, WorkflowEventType
from streaming.emitter import InMemoryWorkflowEventEmitter
from streaming.sqlite_store import event_branch_id
from streaming.store import WorkflowEventStore

logger = logging.getLogger(__name__)

_CRITICAL_TYPES = {
    WorkflowEventType.APPROVAL_REQUIRED,
    WorkflowEventType.APPROVAL_GRANTED,
    WorkflowEventType.APPROVAL_REJECTED,
    WorkflowEventType.WORKFLOW_COMPLETED,
    WorkflowEventType.WORKFLOW_FAILED,
}
_TERMINAL_TYPES = {
    WorkflowEventType.WORKFLOW_COMPLETED,
    WorkflowEventType.WORKFLOW_FAILED,
}


@dataclass(eq=False)
class _Subscriber:
    events: deque[WorkflowEvent] = field(default_factory=deque)
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class WorkflowEventBroker:
    def __init__(
        self,
        *,
        store: WorkflowEventStore | None = None,
        history_size: int = 2_000,
        subscriber_queue_size: int = 200,
    ) -> None:
        self.store = store
        self.history_size = history_size
        self.subscriber_queue_size = subscriber_queue_size
        self._history: dict[tuple[str, str], deque[WorkflowEvent]] = defaultdict(deque)
        self._subscribers: dict[tuple[str, str], set[_Subscriber]] = defaultdict(set)
        self._seen_sequences: dict[tuple[str, str], set[int]] = defaultdict(set)
        self._dropped: dict[tuple[str, str], int] = defaultdict(int)
        self._lock = asyncio.Lock()

    @staticmethod
    def _make_room(
        queue: deque[WorkflowEvent],
        event: WorkflowEvent,
        limit: int,
    ) -> bool:
        if len(queue) < limit:
            return True
        if event.type not in _CRITICAL_TYPES:
            return False
        removable = next((item for item in queue if item.type not in _CRITICAL_TYPES), None)
        if removable is not None:
            queue.remove(removable)
        return True

    async def publish(
        self,
        event: WorkflowEvent,
        *,
        already_persisted: bool = False,
    ) -> None:
        branch_id = event_branch_id(event)
        stream_key = (event.thread_id, branch_id)
        if self.store is not None and not already_persisted:
            if not await self.store.append(event):
                return
        async with self._lock:
            if event.sequence in self._seen_sequences[stream_key]:
                return
            self._seen_sequences[stream_key].add(event.sequence)
            if self.store is None:
                history = self._history[stream_key]
                if self._make_room(history, event, self.history_size):
                    history.append(event)
                else:
                    self._dropped[stream_key] += 1
            for subscriber in tuple(self._subscribers[stream_key]):
                if self._make_room(
                    subscriber.events,
                    event,
                    self.subscriber_queue_size,
                ):
                    subscriber.events.append(event)
                    subscriber.ready.set()
                else:
                    self._dropped[stream_key] += 1

    async def get_history(
        self,
        thread_id: str,
        *,
        branch_id: str = "original",
        after_sequence: int | None = None,
        limit: int | None = None,
        event_types: Sequence[str] | None = None,
    ) -> list[WorkflowEvent]:
        threshold = after_sequence or 0
        if self.store is not None:
            return await self.store.get_events(
                thread_id,
                branch_id=branch_id,
                after_sequence=threshold,
                limit=limit,
                event_types=event_types,
            )
        stream_key = (thread_id, branch_id)
        async with self._lock:
            history = sorted(
                [
                event
                for event in self._history.get(stream_key, ())
                if event.sequence > threshold
                and (not event_types or event.type.value in event_types)
                ],
                key=lambda event: event.sequence,
            )
            return history[:limit] if limit is not None else history

    async def subscribe(
        self,
        thread_id: str,
        *,
        branch_id: str = "original",
        after_sequence: int | None = None,
    ) -> AsyncIterator[WorkflowEvent]:
        subscriber = _Subscriber()
        threshold = after_sequence or 0
        stream_key = (thread_id, branch_id)
        async with self._lock:
            self._subscribers[stream_key].add(subscriber)
        try:
            history = await self.get_history(
                thread_id,
                branch_id=branch_id,
                after_sequence=threshold,
            )
            last_delivered = threshold
            for event in history:
                if event.sequence <= last_delivered:
                    continue
                yield event
                last_delivered = event.sequence
                if event.type in _TERMINAL_TYPES:
                    return
            while True:
                await subscriber.ready.wait()
                while True:
                    async with self._lock:
                        if not subscriber.events:
                            subscriber.ready.clear()
                            break
                        event = subscriber.events.popleft()
                    if event.sequence <= last_delivered:
                        continue
                    yield event
                    last_delivered = event.sequence
                    if event.type in _TERMINAL_TYPES:
                        return
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(stream_key)
                if subscribers is not None:
                    subscribers.discard(subscriber)
                    if not subscribers:
                        self._subscribers.pop(stream_key, None)

    async def subscriber_count(
        self,
        thread_id: str,
        branch_id: str = "original",
    ) -> int:
        async with self._lock:
            return len(self._subscribers.get((thread_id, branch_id), ()))

    async def dropped_events(
        self,
        thread_id: str,
        branch_id: str = "original",
    ) -> int:
        async with self._lock:
            return self._dropped.get((thread_id, branch_id), 0)

    async def close(self) -> None:
        async with self._lock:
            self._subscribers.clear()


class WorkflowEventForwarder:
    def __init__(
        self,
        emitter: InMemoryWorkflowEventEmitter,
        broker: WorkflowEventBroker,
        *,
        events_are_persisted: bool = False,
        on_event: Callable[[WorkflowEvent], Awaitable[None]] | None = None,
    ) -> None:
        self.emitter = emitter
        self.broker = broker
        self.events_are_persisted = events_are_persisted
        self.on_event = on_event
        self._queue: asyncio.Queue[WorkflowEvent | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._attached_threads: set[str] = set()

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    def attach(self, thread_id: str) -> None:
        if thread_id in self._attached_threads:
            return
        self._attached_threads.add(thread_id)
        self.emitter.subscribe(thread_id, self._queue.put_nowait)

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                return
            try:
                await self.broker.publish(
                    event,
                    already_persisted=self.events_are_persisted,
                )
                if self.on_event is not None:
                    try:
                        await self.on_event(event)
                    except Exception:
                        logger.exception(
                            "workflow event hook failed thread=%s type=%s",
                            event.thread_id,
                            event.type.value,
                        )
            finally:
                self._queue.task_done()

    async def flush(self) -> None:
        await self._queue.join()

    async def close(self) -> None:
        if self._worker is None:
            return
        await self._queue.put(None)
        await self._worker
        self._worker = None
