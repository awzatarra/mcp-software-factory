from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

SpanCategory = Literal[
    "workflow", "subgraph", "node", "agent", "llm", "tool", "mcp",
    "approval", "test", "repair", "retry", "persistence", "notification",
    "alert", "api", "worker", "artifact", "validation",
]
SpanStatus = Literal[
    "running", "completed", "failed", "cancelled", "waiting",
    "interrupted", "timeout", "skipped",
]
SpanKind = Literal["internal", "server", "client", "producer", "consumer"]
ObservabilitySortOrder = Literal["asc", "desc"]
TraceSortBy = Literal["started_at", "ended_at", "duration_ms", "status", "workflow_id"]
SpanSortBy = Literal["started_at", "ended_at", "duration_ms", "name", "status", "category"]
LogLevel = Literal["debug", "info", "warning", "error", "critical"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetentionRequest(StrictModel):
    days: int = 30
    dry_run: bool = True
    batch_size: int = 1_000
