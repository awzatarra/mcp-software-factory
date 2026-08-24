from collections import defaultdict, deque
from collections.abc import Callable
import json
import logging
from threading import Lock
from time import sleep
from typing import Protocol

from streaming.catalog import WorkflowEventType
from streaming.errors import WorkflowEventPersistenceError
from streaming.models import WorkflowEvent
from streaming.sqlite_store import (
    SQLiteWorkflowEventStore,
    event_branch_id,
    logical_event_key,
)

logger = logging.getLogger(__name__)


class WorkflowEventEmitter(Protocol):
    def emit(self, event: WorkflowEvent) -> None: ...


class NoOpWorkflowEventEmitter:
    def emit(self, event: WorkflowEvent) -> None:
        return None


class InMemoryWorkflowEventEmitter:
    _CRITICAL = {
        WorkflowEventType.APPROVAL_REQUIRED,
        WorkflowEventType.APPROVAL_GRANTED,
        WorkflowEventType.APPROVAL_REJECTED,
        WorkflowEventType.WORKFLOW_COMPLETED,
        WorkflowEventType.WORKFLOW_FAILED,
    }

    def __init__(
        self,
        *,
        max_events_per_thread: int = 1_000,
        sequence_store: object | None = None,
    ) -> None:
        self.max_events_per_thread = max_events_per_thread
        self.sequence_store = sequence_store
        self._events: dict[str, deque[WorkflowEvent]] = defaultdict(deque)
        self._subscribers: dict[str, list[Callable[[WorkflowEvent], None]]] = defaultdict(list)
        self._logical_keys: dict[str, set[tuple[object, ...]]] = defaultdict(set)
        self._dropped: dict[str, int] = defaultdict(int)
        self._last_sequence: dict[str, int] = defaultdict(int)
        self._lock = Lock()

    @staticmethod
    def _logical_key(event: WorkflowEvent) -> tuple[object, ...]:
        data = event.data
        checkpoint_id = (
            None
            if event.type == WorkflowEventType.APPROVAL_REQUIRED
            else data.get("checkpoint_id")
        )
        return (
            data.get("branch_id"),
            event.type,
            event.stage,
            checkpoint_id,
            data.get("operation"),
            data.get("tool_name") or data.get("tool"),
            data.get("attempt"),
            data.get("handoff_sequence"),
        )

    def emit(self, event: WorkflowEvent) -> None:
        subscribers: list[Callable[[WorkflowEvent], None]]
        with self._lock:
            logical_key = self._logical_key(event)
            if logical_key in self._logical_keys[event.thread_id]:
                return
            serialized_key = json.dumps(
                list(logical_key),
                ensure_ascii=True,
                separators=(",", ":"),
                default=str,
            )
            if self.sequence_store is not None:
                try:
                    if self.sequence_store.has_logical_key(event.thread_id, serialized_key):
                        return
                except Exception:
                    pass
            last_sequence = self._last_sequence[event.thread_id]
            if not last_sequence and self.sequence_store is not None:
                try:
                    last_sequence = int(self.sequence_store.last_sequence(event.thread_id))
                except Exception:
                    last_sequence = 0
            if last_sequence and event.sequence != last_sequence + 1:
                event.sequence = last_sequence + 1
            queue = self._events[event.thread_id]
            if len(queue) >= self.max_events_per_thread and event.type not in self._CRITICAL:
                self._dropped[event.thread_id] += 1
                return
            if len(queue) >= self.max_events_per_thread:
                removable = next(
                    (item for item in queue if item.type not in self._CRITICAL),
                    None,
                )
                if removable is not None:
                    queue.remove(removable)
                    self._dropped[event.thread_id] += 1
            queue.append(event)
            self._last_sequence[event.thread_id] = event.sequence
            if self.sequence_store is not None:
                try:
                    self.sequence_store.record(
                        event.thread_id,
                        event.sequence,
                        serialized_key,
                    )
                except Exception:
                    pass
            self._logical_keys[event.thread_id].add(logical_key)
            subscribers = list(self._subscribers[event.thread_id])
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:
                continue

    def subscribe(
        self,
        thread_id: str,
        consumer: Callable[[WorkflowEvent], None] | None = None,
    ) -> Callable[[WorkflowEvent], None]:
        if consumer is None:
            return lambda event: self.emit(event)
        with self._lock:
            self._subscribers[thread_id].append(consumer)
        return consumer

    def get_events(self, thread_id: str) -> list[WorkflowEvent]:
        with self._lock:
            return list(self._events.get(thread_id, ()))

    def clear(self, thread_id: str) -> None:
        with self._lock:
            self._events.pop(thread_id, None)
            self._logical_keys.pop(thread_id, None)
            self._dropped.pop(thread_id, None)
            self._last_sequence.pop(thread_id, None)

    def dropped_events(self, thread_id: str) -> int:
        with self._lock:
            return self._dropped.get(thread_id, 0)

    def last_sequence(self, thread_id: str) -> int:
        with self._lock:
            current = self._last_sequence.get(thread_id, 0)
        if current or self.sequence_store is None:
            return current
        try:
            return int(self.sequence_store.last_sequence(thread_id))
        except Exception:
            return 0


class DurableWorkflowEventEmitter(InMemoryWorkflowEventEmitter):
    def __init__(
        self,
        store: SQLiteWorkflowEventStore,
        *,
        max_events_per_thread: int = 1_000,
        persistence_retries: int = 3,
    ) -> None:
        super().__init__(max_events_per_thread=max_events_per_thread)
        self.store = store
        self.persistence_retries = max(persistence_retries, 1)
        self._persistence_lock = Lock()
        self._deduplicated = 0

    def emit(self, event: WorkflowEvent) -> None:
        event.data.setdefault("branch_id", "original")
        event.data.setdefault("lineage", event.data["branch_id"])
        event.data.setdefault("origin_checkpoint", None)
        event.data.setdefault("origin_branch_id", None)
        branch_id = event_branch_id(event)
        with self._persistence_lock:
            logical_key = logical_event_key(event)
            if self.store.has_logical_event_sync(
                event.thread_id,
                branch_id,
                logical_key,
            ):
                self._deduplicated += 1
                logger.info(
                    "workflow event deduplicated thread=%s branch=%s type=%s",
                    event.thread_id,
                    branch_id,
                    event.type.value,
                )
                return
            event.sequence = self.store.reserve_next_sequence_sync(
                event.thread_id,
                branch_id,
            )
            last_error: WorkflowEventPersistenceError | None = None
            for attempt in range(self.persistence_retries):
                try:
                    inserted = self.store.append_sync(event)
                    if not inserted:
                        self._deduplicated += 1
                        logger.info(
                            "workflow event deduplicated thread=%s branch=%s type=%s",
                            event.thread_id,
                            branch_id,
                            event.type.value,
                        )
                        return
                    break
                except WorkflowEventPersistenceError as exc:
                    last_error = exc
                    if attempt + 1 < self.persistence_retries:
                        logger.warning(
                            "workflow event persistence retry thread=%s attempt=%s",
                            event.thread_id,
                            attempt + 1,
                        )
                        sleep(0.01 * (attempt + 1))
            else:
                raise last_error or WorkflowEventPersistenceError(
                    "Could not persist workflow event."
                )
        with self._lock:
            queue = self._events[event.thread_id]
            if len(queue) >= self.max_events_per_thread and event.type not in self._CRITICAL:
                self._dropped[event.thread_id] += 1
                return
            if len(queue) >= self.max_events_per_thread:
                removable = next(
                    (item for item in queue if item.type not in self._CRITICAL),
                    None,
                )
                if removable is not None:
                    queue.remove(removable)
                    self._dropped[event.thread_id] += 1
            queue.append(event)
            self._last_sequence[event.thread_id] = max(
                self._last_sequence[event.thread_id],
                event.sequence,
            )
            subscribers = list(self._subscribers[event.thread_id])
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:
                continue

    def last_sequence(self, thread_id: str, branch_id: str = "original") -> int:
        in_memory = super().last_sequence(thread_id)
        return max(
            in_memory,
            self.store.get_last_sequence_sync(thread_id, branch_id),
        )

    def deduplicated_events(self) -> int:
        return self._deduplicated


default_event_emitter = InMemoryWorkflowEventEmitter()
