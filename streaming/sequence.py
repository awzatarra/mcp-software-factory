from collections.abc import Mapping
from threading import Lock


class WorkflowEventSequence:
    def __init__(self, initial: Mapping[str, int] | None = None) -> None:
        self._values = dict(initial or {})
        self._lock = Lock()

    def next(self, thread_id: str) -> int:
        with self._lock:
            value = self._values.get(thread_id, 0) + 1
            self._values[thread_id] = value
            return value

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)

    def ensure_at_least(self, thread_id: str, value: int) -> int:
        with self._lock:
            current = self._values.get(thread_id, 0)
            if value > current:
                self._values[thread_id] = value
                return value
            return current

    def clear(self, thread_id: str) -> None:
        with self._lock:
            self._values.pop(thread_id, None)
