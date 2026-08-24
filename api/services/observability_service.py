from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from datetime import UTC, datetime
import logging
import os
from time import perf_counter
from typing import Any, AsyncIterator
from uuid import uuid4

from api.services.observability_context import (
    ObservabilityContext,
    get_observability_context,
    observability_context,
)
from api.services.observability_sanitizer import error_fingerprint, sanitize_mapping, stable_hash
from api.services.observability_store import ObservabilityStore
from api.services.otlp_exporter import OptionalOtlpExporter
from streaming.models import WorkflowEvent

logger = logging.getLogger(__name__)

START_SUFFIXES = ("_started", "_required")
END_SUFFIXES = ("_completed", "_failed", "_granted", "_rejected")
TERMINAL_EVENTS = {"workflow_completed": "completed", "workflow_failed": "failed"}
SUBGRAPH_EVENTS = {
    "planning_started", "planning_completed", "planning_failed",
    "workspace_inspection_started", "workspace_inspection_completed",
    "implementation_started", "implementation_completed", "implementation_failed",
    "testing_started", "testing_completed", "testing_failed",
    "supervisor_decision_started", "supervisor_decision_completed",
}
SUBGRAPH_STAGE_BY_EVENT = {
    "workspace_inspection_started": "inspect_workspace",
    "workspace_inspection_completed": "inspect_workspace",
    "supervisor_decision_started": "supervisor",
    "supervisor_decision_completed": "supervisor",
}
APPROVAL_STAGE_BY_OPERATION = {
    "create_project": "implementation",
    "prepare_environment": "implementation",
    "run_tests": "testing_repair",
    "apply_fix": "testing_repair",
}


def _trace_id(workflow_id: str, branch_id: str) -> str:
    return stable_hash(f"workflow:{workflow_id}:{branch_id}")[:32]


def _span_id(seed: str | None = None) -> str:
    return stable_hash(seed or uuid4().hex)[:16]


def _parse(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _duration_ms(started_at: str | datetime, ended_at: str | datetime) -> float:
    return max(0.0, (_parse(ended_at) - _parse(started_at)).total_seconds() * 1000)


def _base_event_name(event_type: str) -> str:
    for suffix in (*START_SUFFIXES, *END_SUFFIXES):
        if event_type.endswith(suffix):
            return event_type[: -len(suffix)]
    return event_type


def _category(event: WorkflowEvent) -> str:
    name = event.type.value
    if name.startswith("workflow_"): return "workflow"
    if name in SUBGRAPH_EVENTS: return "subgraph"
    if name.startswith("stage_"): return "node"
    if name.startswith("approval_"): return "approval"
    if name.startswith("tool_"): return "tool"
    if name.startswith("test_run_") or name.startswith("testing_"): return "test"
    if name.startswith("repair_"): return "repair"
    if name.startswith("supervisor_") or name.startswith("planning_") or name.startswith("implementation_"): return "agent"
    if name.startswith("workspace_"): return "validation"
    return "node"


def _agent(event: WorkflowEvent) -> str | None:
    if event.data.get("agent"):
        return str(event.data["agent"])
    name = event.type.value
    if name.startswith("planning_"): return "Planner"
    if name.startswith("implementation_"): return "Developer"
    if name.startswith("testing_") or name.startswith("test_run_"): return "QA"
    if name.startswith("repair_"): return "Repair"
    if name.startswith("supervisor_") or name == "handoff_completed": return "Supervisor"
    return None


def _event_stage(event: WorkflowEvent) -> str:
    event_type = event.type.value
    if event_type in SUBGRAPH_STAGE_BY_EVENT:
        return SUBGRAPH_STAGE_BY_EVENT[event_type]
    if event_type.startswith("approval_"):
        operation = str(event.data.get("operation") or "")
        return APPROVAL_STAGE_BY_OPERATION.get(operation, str(event.stage or "approval"))
    return str(event.data.get("subgraph") or event.stage or "workflow")


def _event_operation(event: WorkflowEvent) -> str:
    event_type = event.type.value
    category = _category(event)
    if category == "subgraph":
        return _event_stage(event)
    if category == "tool":
        server = str(event.data.get("server") or "tool")
        tool = str(event.data.get("tool") or "call")
        return f"{server}__{tool}"
    if category == "approval":
        return str(event.data.get("operation") or "approval")
    if category == "node":
        return str(event.data.get("node") or event.stage or _base_event_name(event_type))
    if category == "test":
        return "run_tests" if event_type.startswith("test_run_") else _event_stage(event)
    return str(event.data.get("operation") or _base_event_name(event_type))


class ObservabilityService:
    def __init__(self, store: ObservabilityStore, *, slow_span_threshold_ms: float | None = None) -> None:
        self.store = store
        self.slow_span_threshold_ms = slow_span_threshold_ms or float(os.getenv("OBSERVABILITY_SLOW_SPAN_MS", "2000"))
        self.enabled = os.getenv("OBSERVABILITY_ENABLED", "true").casefold() == "true"
        self.otlp = OptionalOtlpExporter()

    async def initialize(self) -> None:
        await self.store.initialize()
        if os.getenv("OBSERVABILITY_RECONCILE_ON_STARTUP", "true").casefold() == "true":
            reconciled = await self.store.reconcile_running()
            if reconciled:
                logger.info("observability reconciled interrupted_spans=%s", reconciled)

    async def _safe(self, action, *args, **kwargs):
        if not self.enabled:
            return None
        try:
            return await action(*args, **kwargs)
        except Exception:
            logger.exception("observability operation failed")
            return None

    async def finalize_workflow_trace(
        self,
        workflow_id: str,
        status: str,
        *,
        branch_id: str = "original",
        reason: str = "workflow_lifecycle_terminal",
        failure: BaseException | None = None,
        ended_at: datetime | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"trace_found": False, "changed": False}
        trace = await self.store.find_trace(workflow_id, branch_id)
        if trace is None:
            context = await self.ensure_trace(workflow_id, branch_id)
            trace_id = context.trace_id
        else:
            trace_id = trace["trace_id"]
        assert trace_id is not None
        timestamp = (ended_at or datetime.now(UTC)).isoformat()
        error_type = type(failure).__name__ if failure else None
        error_message = str(failure)[:1000] if failure else None
        fingerprint = (
            error_fingerprint(error_type, error_message, "workflow") if failure else None
        )
        try:
            return await self.store.finalize_trace(
                trace_id,
                status=status,
                ended_at=timestamp,
                reason=reason,
                error_type=error_type,
                error_message=error_message,
                error_fingerprint=fingerprint,
            )
        except Exception:
            logger.exception(
                "observability terminal lifecycle failed workflow=%s status=%s",
                workflow_id,
                status,
            )
            try:
                return await self.store.finalize_trace(
                    trace_id,
                    status=status,
                    ended_at=timestamp,
                    reason=f"{reason}_best_effort_retry",
                    error_type=error_type,
                    error_message=error_message,
                    error_fingerprint=fingerprint,
                )
            except Exception:
                logger.exception(
                    "observability immediate reconciliation failed workflow=%s",
                    workflow_id,
                )
                return {"trace_found": True, "changed": False, "store_failed": True}
    async def ensure_trace(
        self,
        workflow_id: str,
        branch_id: str = "original",
        *,
        started_at: datetime | None = None,
        source: str = "live",
        parent_trace_id: str | None = None,
        origin_checkpoint: str | None = None,
    ) -> ObservabilityContext:
        trace_id = _trace_id(workflow_id, branch_id)
        root_span_id = _span_id(f"{trace_id}:root")
        timestamp = (started_at or datetime.now(UTC)).isoformat()
        await self._safe(self.store.create_trace, {
            "trace_id": trace_id, "workflow_id": workflow_id, "branch_id": branch_id,
            "name": "software_factory.workflow", "status": "running", "source": source,
            "parent_trace_id": parent_trace_id, "origin_checkpoint": origin_checkpoint,
            "started_at": timestamp, "root_span_id": root_span_id,
            "attributes": {
                "precision": "partial" if source == "backfill" else "full",
                "hierarchy_degraded": source == "backfill",
            },
        })
        await self._safe(self.store.start_span, {
            "span_id": root_span_id, "trace_id": trace_id, "name": "workflow",
            "category": "workflow", "kind": "internal", "status": "running",
            "workflow_id": workflow_id, "branch_id": branch_id, "started_at": timestamp,
        })
        return ObservabilityContext(
            trace_id=trace_id, span_id=root_span_id, workflow_id=workflow_id,
            branch_id=branch_id, execution_id=uuid4().hex,
        )

    @asynccontextmanager
    async def span(
        self, name: str, *, category: str = "internal", kind: str = "internal",
        attributes: dict[str, Any] | None = None, status: str = "running",
        agent: str | None = None, node: str | None = None, subgraph: str | None = None,
    ) -> AsyncIterator[ObservabilityContext]:
        parent = get_observability_context()
        if not parent.trace_id:
            yield parent
            return
        span_id = _span_id()
        started = datetime.now(UTC)
        child = parent.child(
            span_id=span_id,
            operation=name,
            agent=agent if agent is not None else parent.agent,
            node=node if node is not None else parent.node,
            subgraph=subgraph if subgraph is not None else parent.subgraph,
        )
        await self._safe(self.store.start_span, {
            **child.serialize(), "span_id": span_id, "parent_span_id": parent.span_id,
            "name": name, "category": category, "kind": kind, "status": status,
            "started_at": started.isoformat(), "attributes": attributes,
        })
        final_status = "completed"
        failure: Exception | None = None
        try:
            with observability_context(child):
                yield child
        except asyncio.CancelledError:
            final_status = "cancelled"
            raise
        except Exception as exc:
            final_status, failure = "failed", exc
            raise
        finally:
            ended = datetime.now(UTC)
            duration = _duration_ms(started, ended)
            await self._safe(
                self.store.end_span, span_id, status=final_status, ended_at=ended.isoformat(), duration_ms=duration,
                error_type=type(failure).__name__ if failure else None,
                error_message=str(failure)[:1000] if failure else None,
                error_fingerprint=error_fingerprint(type(failure).__name__, str(failure), name) if failure else None,
                is_slow=duration >= self.slow_span_threshold_ms,
            )
            await self._safe(self.store.add_metric, {
                "trace_id": parent.trace_id, "workflow_id": parent.workflow_id,
                "name": f"{category}.duration", "value": duration, "unit": "ms",
                "timestamp": ended.isoformat(), "attributes": {"operation": name, "status": final_status},
            })
            if failure is not None:
                await self._safe(self.store.add_metric, {
                    "trace_id": parent.trace_id, "workflow_id": parent.workflow_id,
                    "name": f"{category}.errors", "value": 1, "unit": "count",
                    "timestamp": ended.isoformat(), "attributes": {"operation": name},
                })
            self.otlp.emit_span(
                name=name, started_at=started, ended_at=ended, status=final_status,
                attributes={
                    "software_factory.trace_id": parent.trace_id,
                    "software_factory.span_id": span_id,
                    "software_factory.workflow_id": parent.workflow_id,
                    "software_factory.category": category,
                },
            )

    async def _event_parent_span(
        self,
        trace_id: str,
        root_span_id: str,
        event: WorkflowEvent,
    ) -> tuple[str, bool]:
        category = _category(event)
        if category in {"workflow", "subgraph"}:
            return root_span_id, False
        preferences = {
            "node": ("subgraph",),
            "agent": ("node",),
            "llm": ("agent",),
            "mcp": ("agent", "node", "tool"),
            "tool": ("agent", "node", "subgraph"),
            "approval": ("node", "subgraph"),
            "test": ("agent", "node", "subgraph"),
            "repair": ("agent", "node", "subgraph"),
            "validation": ("node", "subgraph"),
        }.get(category, ("node", "subgraph"))
        placeholders = ",".join("?" for _ in preferences)
        candidates = await self.store.fetch_all(
            f"""SELECT * FROM observability_spans
                WHERE trace_id=? AND category IN ({placeholders}) AND started_at<=?
                ORDER BY started_at DESC LIMIT 100""",
            (trace_id, *preferences, event.timestamp.isoformat()),
        )
        stage = _event_stage(event)
        for preferred in preferences:
            for candidate in candidates:
                attributes = candidate.get("attributes") or {}
                candidate_stage = str(attributes.get("stage") or candidate.get("node") or "")
                if candidate.get("category") == preferred and candidate_stage == stage:
                    return str(candidate["span_id"]), True
        for preferred in preferences:
            for candidate in candidates:
                if candidate.get("category") == preferred:
                    return str(candidate["span_id"]), True
        return root_span_id, False

    async def ingest_workflow_event(self, event: WorkflowEvent, *, source: str = "live") -> bool:
        if not self.enabled:
            return False
        try:
            existing = await self.store.fetch_one(
                "SELECT event_id FROM observability_span_events WHERE event_id=?",
                (str(event.event_id),),
            )
            if existing is not None:
                return False
            branch_id = str(event.data.get("branch_id") or "original")
            event_type = event.type.value
            trace_source = (
                "fork" if event_type == "workflow_forked"
                else "replay" if str(event.data.get("lineage", "")).startswith("replay")
                else source
            )
            context = await self.ensure_trace(
                event.thread_id, branch_id, started_at=event.timestamp, source=trace_source,
                parent_trace_id=event.data.get("parent_trace_id") or (_trace_id(event.thread_id, "original") if branch_id != "original" else None),
                origin_checkpoint=event.data.get("origin_checkpoint"),
            )
            trace_id = context.trace_id
            assert trace_id is not None
            trace = await self.store.find_trace(event.thread_id, branch_id)
            root_span_id = trace.get("root_span_id") if trace else context.span_id
            if event_type == "workflow_resumed":
                await self.store.execute(
                    "UPDATE observability_traces SET status='running',ended_at=NULL,updated_at=? WHERE trace_id=? AND status='interrupted'",
                    (datetime.now(UTC).isoformat(), trace_id),
                )
                await self.store.execute(
                    "UPDATE observability_spans SET status='running',ended_at=NULL,updated_at=? WHERE span_id=? AND status='interrupted'",
                    (datetime.now(UTC).isoformat(), root_span_id),
                )
            category = _category(event)
            operation = _event_operation(event)
            name = f"{category}.{operation}"
            parent_span_id, context_restored = await self._event_parent_span(
                trace_id, str(root_span_id), event
            )
            event_span_id = str(root_span_id)
            materialize_event_span = not (
                source == "live" and (
                    event_type.startswith("stage_") or category == "workflow"
                )
            )
            if materialize_event_span and event_type.endswith(START_SUFFIXES):
                status = "waiting" if event_type == "approval_required" else "running"
                event_span_id = _span_id(str(event.event_id))
                await self.store.start_span({
                    "span_id": event_span_id, "trace_id": trace_id,
                    "parent_span_id": parent_span_id, "name": name, "category": category,
                    "kind": "client" if category in {"tool", "llm", "mcp"} else "internal",
                    "status": status, "workflow_id": event.thread_id, "branch_id": branch_id,
                    "agent": _agent(event), "node": event.data.get("node") or event.stage,
                    "operation": operation, "started_at": event.timestamp.isoformat(),
                    "attributes": {
                        "source": event.source,
                        "sequence": event.sequence,
                        "stage": _event_stage(event),
                        "context_restored": context_restored,
                        "parent_fallback": None if context_restored or category == "subgraph" else "root",
                    },
                })
            elif materialize_event_span and event_type.endswith(END_SUFFIXES):
                open_span = await self.store.find_open_span(trace_id, name)
                if open_span:
                    event_span_id = str(open_span["span_id"])
                    status = "failed" if event_type.endswith("_failed") else ("cancelled" if event_type.endswith("_rejected") else "completed")
                    duration = _duration_ms(open_span["started_at"], event.timestamp)
                    message = event.message if status == "failed" else None
                    await self.store.end_span(
                        open_span["span_id"], status=status, ended_at=event.timestamp.isoformat(), duration_ms=duration,
                        error_type=str(event.data.get("failure_type") or "workflow_error") if message else None,
                        error_message=message, error_fingerprint=error_fingerprint(str(event.data.get("failure_type")), message, operation) if message else None,
                        is_slow=duration >= self.slow_span_threshold_ms,
                    )
                    await self.store.add_metric({
                        "trace_id": trace_id, "workflow_id": event.thread_id,
                        "name": f"{category}.duration", "value": duration, "unit": "ms",
                        "timestamp": event.timestamp.isoformat(), "attributes": {"operation": operation, "status": status},
                    })
                    if status == "failed":
                        await self.store.add_metric({
                            "trace_id": trace_id, "workflow_id": event.thread_id,
                            "name": f"{_category(event)}.errors", "value": 1, "unit": "count",
                            "timestamp": event.timestamp.isoformat(), "attributes": {"operation": operation},
                        })
            await self.store.add_span_event({
                "event_id": str(event.event_id), "trace_id": trace_id, "span_id": event_span_id,
                "name": event_type, "timestamp": event.timestamp.isoformat(),
                "attributes": {"sequence": event.sequence, "stage": event.stage, "status": event.status.value, "source": event.source},
            })
            await self.store.add_log({
                "trace_id": trace_id, "span_id": root_span_id, "workflow_id": event.thread_id,
                "level": "error" if event.status.value == "failed" else "info",
                "message": event.message or event_type, "timestamp": event.timestamp.isoformat(),
                "attributes": {"event_type": event_type, "sequence": event.sequence},
            })
            if event_type in TERMINAL_EVENTS:
                root = await self.store.find_open_span(trace_id, "workflow")
                terminal_status = TERMINAL_EVENTS[event_type]
                if event.data.get("terminal_status") == "user_cancelled":
                    terminal_status = "cancelled"
                if root:
                    duration = _duration_ms(root["started_at"], event.timestamp)
                    await self.store.end_span(root["span_id"], status=terminal_status, ended_at=event.timestamp.isoformat(), duration_ms=duration, is_slow=duration >= self.slow_span_threshold_ms)
                    await self.store.add_metric({"trace_id": trace_id, "workflow_id": event.thread_id, "name": "workflow.duration", "value": duration, "unit": "ms", "timestamp": event.timestamp.isoformat()})
                else:
                    trace_record = await self.store.fetch_one(
                        "SELECT started_at FROM observability_traces WHERE trace_id=?", (trace_id,)
                    )
                    duration = _duration_ms(trace_record["started_at"], event.timestamp) if trace_record else 0
                await self.finalize_workflow_trace(
                    event.thread_id,
                    terminal_status,
                    branch_id=branch_id,
                    reason="workflow_terminal_event",
                    ended_at=event.timestamp,
                )
            return True
        except Exception:
            logger.exception("could not ingest workflow event into observability")
            return False

    async def workflow_summary(self, workflow_id: str) -> dict[str, Any]:
        trace = await self.store.find_trace(workflow_id)
        if trace is None:
            return {"state": "not_available"}
        counts = await self.store.fetch_one(
            "SELECT COUNT(*) AS span_count,SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS error_count,SUM(CASE WHEN is_slow=1 THEN 1 ELSE 0 END) AS slow_span_count FROM observability_spans WHERE trace_id=?",
            (trace["trace_id"],),
        )
        return {
            "state": "available", "trace_id": trace["trace_id"], "status": trace["status"],
            "duration_ms": trace.get("duration_ms"), **(counts or {}),
        }

    async def summary(self) -> dict[str, Any]:
        totals = await self.store.fetch_one(
            """SELECT COUNT(*) AS traces,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
            AVG(duration_ms) AS average_duration_ms FROM observability_traces"""
        ) or {}
        spans = await self.store.fetch_one(
            "SELECT COUNT(*) AS spans,SUM(CASE WHEN is_slow=1 THEN 1 ELSE 0 END) AS slow_spans,SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS errors FROM observability_spans"
        ) or {}
        duration_rows = await self.store.fetch_all(
            "SELECT duration_ms FROM observability_traces WHERE duration_ms IS NOT NULL ORDER BY duration_ms"
        )
        durations = [float(row["duration_ms"]) for row in duration_rows]
        def percentile(fraction: float) -> float | None:
            if not durations:
                return None
            return durations[min(len(durations) - 1, round((len(durations) - 1) * fraction))]
        return {
            **totals, **spans, "p50_duration_ms": percentile(.5),
            "p90_duration_ms": percentile(.9), "p95_duration_ms": percentile(.95),
            "otlp_configured": self.otlp.configured,
            "otlp_available": self.otlp.available,
            "calculated_at": datetime.now(UTC).isoformat(),
        }
