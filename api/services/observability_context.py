from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, replace
from typing import Iterator


@dataclass(frozen=True, slots=True)
class ObservabilityContext:
    trace_id: str | None = None
    span_id: str | None = None
    workflow_id: str | None = None
    branch_id: str = "original"
    task_id: str | None = None
    agent: str | None = None
    node: str | None = None
    subgraph: str | None = None
    operation: str | None = None
    execution_id: str | None = None

    def child(self, **updates: str | None) -> "ObservabilityContext":
        return replace(self, **updates)

    def serialize(self) -> dict[str, str | None]:
        return asdict(self)

    @classmethod
    def restore(cls, value: dict[str, object] | None) -> "ObservabilityContext":
        if not value:
            return cls()
        fields = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in fields if key in value})


_current_context: ContextVar[ObservabilityContext] = ContextVar(
    "software_factory_observability_context",
    default=ObservabilityContext(),
)


def get_observability_context() -> ObservabilityContext:
    return _current_context.get()


def set_observability_context(context: ObservabilityContext) -> Token:
    return _current_context.set(context)


def reset_observability_context(token: Token) -> None:
    _current_context.reset(token)


@contextmanager
def observability_context(context: ObservabilityContext) -> Iterator[ObservabilityContext]:
    token = set_observability_context(context)
    try:
        yield context
    finally:
        reset_observability_context(token)
