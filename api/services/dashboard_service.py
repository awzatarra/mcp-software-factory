from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
import json
import math
import time
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from api.dashboard_models import (
    ApprovalSummary, DashboardActivityItem, DashboardActivityResponse,
    DashboardAgentMetric, DashboardAgentsResponse, DashboardAttentionItem,
    DashboardAttentionResponse, DashboardCountItem, DashboardFilters,
    DashboardSummaryResponse, DashboardTimeSeriesPoint,
    DashboardTimeSeriesResponse, DurationSummary, ScoreSummary, TestingSummary,
    WorkflowCounts,
)
from api.services.dashboard_store import DashboardMetricStore
from api.services.workflow_evaluation_service import (
    SCORING_VERSION, TERMINAL_STATUSES, WorkflowEvaluationService,
    calculate_workflow_evaluation,
)
from api.services.workflow_execution_service import WorkflowExecutionService


COUNT_METRICS = {
    "workflow_count", "completed_count", "failed_count", "tests_passed",
    "tests_failed", "repair_count", "warning_count", "approval_rejections",
}
AVERAGE_METRICS = {
    "average_score", "average_duration", "average_active_duration",
    "average_approval_wait",
}
METRIC_ALIASES = {
    "workflows_created": "workflow_count",
    "test_pass_rate": "test_pass_rate",
    "repair_rate": "repair_rate",
    "approval_rejections": "approval_rejections",
}
CANONICAL_AGENTS = {
    "business analyst": "business analyst",
    "software architect": "software architect",
    "backend developer": "backend developer",
    "developer": "backend developer",
    "qa": "qa reviewer",
    "qa reviewer": "qa reviewer",
    "repair": "repair agent",
    "repair agent": "repair agent",
    "supervisor": "supervisor",
}


def build_timeseries_point(
    *,
    bucket_start: datetime,
    bucket_end: datetime,
    value: float | int | None,
    count: int,
    numerator: float | int | None = None,
    denominator: float | int | None = None,
) -> DashboardTimeSeriesPoint:
    if bucket_end.timestamp() <= bucket_start.timestamp():
        raise ValueError("bucket_end must be greater than bucket_start")
    return DashboardTimeSeriesPoint(
        bucket_start=bucket_start,
        bucket_end=bucket_end,
        value=value,
        count=count,
        numerator=numerator,
        denominator=denominator,
    )


def _status_group(
    status: str,
    interrupted: bool = False,
    pending_operation: str | None = None,
) -> str:
    normalized = (status or "pending").casefold()
    if normalized in {"user_cancelled", "cancelled", "canceled"}:
        return "cancelled"
    if normalized in {"completed", "success", "succeeded"}:
        return "completed"
    if interrupted and pending_operation:
        return "waiting"
    if normalized in {"waiting", "interrupted"}:
        return "waiting"
    if normalized in {"running", "started"}:
        return "running"
    if normalized in {"pending", "not_started"}:
        return "pending"
    return "failed"


def _mean(values: Iterable[float | int | None]) -> float | None:
    present = [float(value) for value in values if value is not None and float(value) >= 0]
    return round(sum(present) / len(present), 2) if present else None


def _percentile(values: Iterable[float | int | None], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None and float(value) >= 0)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percentile
    lower, upper = math.floor(position), math.ceil(position)
    value = ordered[lower] if lower == upper else ordered[lower] + (
        ordered[upper] - ordered[lower]
    ) * (position - lower)
    return round(value, 2)


def _items(counter: Counter[str], limit: int | None = None) -> list[DashboardCountItem]:
    total = sum(counter.values())
    values = sorted(counter.items(), key=lambda item: (-item[1], item[0].casefold()))
    if limit is not None:
        values = values[:limit]
    return [DashboardCountItem(
        key=key,
        label=key.replace("_", " ").strip().capitalize(),
        count=count,
        percentage=round(count * 100 / total, 2) if total else None,
    ) for key, count in values]


def _operation(event: Any) -> str:
    return str(event.data.get("operation") or event.data.get("pending_operation") or event.data.get("tool_name") or event.data.get("tool") or "unknown")


def _pending_approval(
    events: list[Any],
    reference_time: datetime,
) -> tuple[str | None, list[float]]:
    pending: tuple[str, datetime] | None = None
    waits: list[float] = []
    for event in events:
        event_type = event.type.value
        if event_type == "approval_required":
            pending = (_operation(event), event.timestamp)
        elif event_type in {"approval_granted", "approval_rejected"} and pending:
            waits.append(max((event.timestamp - pending[1]).total_seconds(), 0))
            pending = None
    if pending:
        waits.append(max((reference_time - pending[1]).total_seconds(), 0))
    return (pending[0] if pending else None), waits


def _canonical_agent(value: str) -> str:
    normalized = " ".join(value.casefold().replace("_", " ").split())
    for candidate, canonical in CANONICAL_AGENTS.items():
        if candidate in normalized:
            return canonical
    return normalized or "backend developer"


class DashboardService:
    def __init__(self, *, store: DashboardMetricStore,
                 execution_service: WorkflowExecutionService,
                 evaluation_service: WorkflowEvaluationService,
                 cache_ttl_seconds: int = 30) -> None:
        self.store = store
        self.execution_service = execution_service
        self.evaluation_service = evaluation_service
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}

    def invalidate_cache(self) -> None:
        self._cache.clear()

    async def upsert_workflow(self, thread_id: str, branch_id: str = "original") -> str:
        try:
            execution = await self.execution_service.get_execution(
                thread_id, branch_id=branch_id, include_events=True
            )
            events = execution.events or []
            reference_time = execution.updated_at or datetime.now(UTC)
            evaluation = calculate_workflow_evaluation(
                execution, events, include_evidence=False,
                calculated_at=reference_time,
            )
            if execution.terminal_status in TERMINAL_STATUSES:
                await self.evaluation_service.store.put(evaluation)
        except Exception:
            return "failed"

        pending_operation, approval_waits = _pending_approval(events, reference_time)
        operations: Counter[str] = Counter()
        requested = granted = rejected = 0
        for event in events:
            if event.type.value == "approval_required":
                requested += 1
                operations[_operation(event)] += 1
            elif event.type.value == "approval_granted":
                granted += 1
            elif event.type.value == "approval_rejected":
                rejected += 1
        agents = []
        related_files: set[str] = set()
        for task in execution.planning.tasks:
            duration = None
            if task.started_at and task.completed_at:
                duration = max((task.completed_at - task.started_at).total_seconds(), 0)
            related_files.update(task.related_files)
            agents.append({
                "agent": _canonical_agent(task.agent), "status": task.status,
                "attempt": max(task.attempt, 1), "duration_seconds": duration,
                "related_files": len(set(task.related_files)),
            })
        if execution.testing.repair_attempts:
            agents.append({"agent": "repair agent", "status": "completed" if execution.testing.passed else "failed", "attempt": execution.testing.repair_attempts, "duration_seconds": evaluation.duration.repair_seconds, "related_files": len(execution.testing.files_updated_during_repair)})
        if execution.supervisor.decision:
            agents.append({"agent": "supervisor", "status": "completed", "attempt": max(execution.supervisor.attempts, 1), "duration_seconds": None, "related_files": 0})
        versions = await self.store.evaluation_versions(thread_id, branch_id)
        versions[evaluation.scoring_version] = {"score": evaluation.overall_score, "grade": evaluation.grade, "status": evaluation.evaluation_status}
        created_at = execution.created_at or execution.updated_at or datetime.now(UTC)
        source_updated = execution.updated_at or created_at
        status_group = _status_group(execution.terminal_status, bool(pending_operation), pending_operation)
        result = await self.store.upsert({
            "thread_id": thread_id, "branch_id": branch_id, "lineage": execution.lineage,
            "project_name": execution.project_name, "workflow_intent": execution.workflow_intent,
            "framework": execution.implementation.framework, "test_framework": execution.testing.framework,
            "terminal_status": execution.terminal_status, "status_group": status_group,
            "interrupted": int(bool(pending_operation)), "pending_operation": pending_operation,
            "data_complete": int(execution.data_complete), "tests_executed": int(execution.testing.executed),
            "tests_passed": int(execution.testing.passed), "test_warnings": execution.testing.warnings,
            "repair_phase": execution.testing.repair_phase, "repair_attempts": execution.testing.repair_attempts,
            "overall_score": evaluation.overall_score if evaluation.evaluation_status == "final" else None,
            "grade": evaluation.grade if evaluation.evaluation_status == "final" else None,
            "evaluation_status": evaluation.evaluation_status, "scoring_version": evaluation.scoring_version,
            "total_seconds": evaluation.duration.wall_clock_duration_seconds,
            "active_seconds": evaluation.duration.active_execution_seconds,
            "approval_wait_seconds": evaluation.duration.approval_wait_seconds,
            "longest_approval_wait_seconds": max(approval_waits, default=None),
            "planning_seconds": evaluation.duration.planning_seconds,
            "implementation_seconds": evaluation.duration.implementation_seconds,
            "testing_seconds": evaluation.duration.testing_seconds,
            "repair_seconds": evaluation.duration.repair_seconds,
            "approvals_requested": requested, "approvals_granted": min(granted, requested),
            "approvals_rejected": min(rejected, max(requested - min(granted, requested), 0)),
            "approval_operations": dict(operations),
            "evaluations": versions, "findings": [finding.code for finding in evaluation.findings],
            "recommendations": evaluation.recommendations, "agents": agents,
            "related_files": sorted(related_files), "created_at": created_at.isoformat(),
            "source_updated_at": source_updated.isoformat(), "calculated_at": datetime.now(UTC).isoformat(),
        })
        if result in {"inserted", "updated"}:
            self.invalidate_cache()
        return result

    async def backfill(self, *, batch_size: int = 50) -> dict[str, int]:
        counts = {"inserted": 0, "updated": 0, "unchanged": 0, "failed": 0}
        keys = await self.store.stream_keys()
        for start in range(0, len(keys), max(batch_size, 1)):
            for thread_id, branch_id in keys[start:start + max(batch_size, 1)]:
                result = await self.upsert_workflow(thread_id, branch_id)
                counts[result if result in counts else "failed"] += 1
        return counts

    async def _filtered(self, filters: DashboardFilters) -> list[dict[str, Any]]:
        key = filters.model_dump_json()
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self.cache_ttl_seconds:
            return cached[1]
        selected = []
        for source in await self.store.list_metrics():
            created = datetime.fromisoformat(source["created_at"].replace("Z", "+00:00"))
            if not filters.date_from <= created <= filters.date_to:
                continue
            if filters.branch_scope == "original" and source["branch_id"] != "original":
                continue
            if filters.status != "all" and source["status_group"] != filters.status:
                continue
            if filters.project_name and filters.project_name.casefold() not in str(source["project_name"] or "").casefold():
                continue
            if filters.workflow_intent and source["workflow_intent"] != filters.workflow_intent:
                continue
            if filters.framework and source["framework"] != filters.framework:
                continue
            version = source["evaluations"].get(filters.scoring_version, {})
            row = {**source, "selected_score": version.get("score"), "selected_grade": version.get("grade"), "selected_evaluation_status": version.get("status")}
            if filters.grade != "all" and row["selected_grade"] != filters.grade:
                continue
            selected.append(row)
        self._cache[key] = (now, selected)
        return selected

    async def summary(self, filters: DashboardFilters) -> DashboardSummaryResponse:
        rows = await self._filtered(filters)
        statuses = Counter(row["status_group"] for row in rows)
        terminal = statuses["completed"] + statuses["failed"]
        evaluated = [row for row in rows if row["selected_evaluation_status"] == "final" and row["selected_score"] is not None]
        provisional = sum(row["selected_evaluation_status"] == "partial" for row in rows)
        scores = [row["selected_score"] for row in evaluated]
        grades = Counter(row["selected_grade"] for row in evaluated if row["selected_grade"])
        raw_durations = [row["total_seconds"] for row in rows if row["total_seconds"] is not None]
        durations = [float(value) for value in raw_durations if float(value) >= 0]
        executed = [row for row in rows if row["tests_executed"]]
        repairs = [row for row in rows if row["repair_attempts"] > 0]
        warnings = [row for row in rows if row["test_warnings"] > 0]
        operations: Counter[str] = Counter()
        for row in rows:
            operations.update(row["approval_operations"])
        wait_total = sum(float(row["approval_wait_seconds"] or 0) for row in rows)
        wall_total = sum(float(row["total_seconds"] or 0) for row in rows if float(row["total_seconds"] or 0) >= 0)
        sources = [row["source_updated_at"] for row in rows if row["source_updated_at"]]
        return DashboardSummaryResponse(
            date_from=filters.date_from, date_to=filters.date_to, timezone=filters.timezone,
            branch_scope=filters.branch_scope,
            workflow_counts=WorkflowCounts(
                total=len(rows), completed=statuses["completed"], failed=statuses["failed"],
                running=statuses["running"], waiting=statuses["waiting"], pending=statuses["pending"],
                cancelled=statuses["cancelled"],
                success_rate_percent=round(statuses["completed"] * 100 / terminal, 2) if terminal else None,
                failure_rate_percent=round(statuses["failed"] * 100 / terminal, 2) if terminal else None,
            ),
            scores=ScoreSummary(
                evaluated_workflows=len(evaluated), unevaluated_workflows=len(rows) - len(evaluated),
                average_score=_mean(scores), median_score=_percentile(scores, .5),
                min_score=min(scores) if scores else None, max_score=max(scores) if scores else None,
                excellent=grades["excellent"], good=grades["good"], acceptable=grades["acceptable"],
                poor=grades["poor"], critical=grades["critical"], provisional=provisional,
                scoring_versions=sorted({filters.scoring_version} if any(filters.scoring_version in row["evaluations"] for row in rows) else set()),
            ),
            durations=DurationSummary(
                workflows_with_duration=len(durations), discarded_workflows=len(raw_durations) - len(durations),
                average_wall_clock_seconds=_mean(durations), median_wall_clock_seconds=_percentile(durations, .5),
                p50_wall_clock_seconds=_percentile(durations, .5), p90_wall_clock_seconds=_percentile(durations, .9),
                p95_wall_clock_seconds=_percentile(durations, .95),
                average_active_seconds=_mean(row["active_seconds"] for row in rows),
                average_approval_wait_seconds=_mean(row["approval_wait_seconds"] for row in rows),
                approval_wait_percent=round(wait_total * 100 / wall_total, 2) if wall_total else None,
                average_planning_seconds=_mean(row["planning_seconds"] for row in rows),
                average_implementation_seconds=_mean(row["implementation_seconds"] for row in rows),
                average_testing_seconds=_mean(row["testing_seconds"] for row in rows),
                average_repair_seconds=_mean(row["repair_seconds"] for row in repairs),
            ),
            testing=TestingSummary(
                executed=len(executed), passed=sum(row["tests_passed"] for row in executed),
                failed=sum(not row["tests_passed"] for row in executed), not_executed=len(rows) - len(executed),
                pass_rate_percent=round(sum(row["tests_passed"] for row in executed) * 100 / len(executed), 2) if executed else None,
                workflows_with_warnings=len(warnings), total_warnings=sum(row["test_warnings"] for row in rows),
                average_warnings=_mean(row["test_warnings"] for row in warnings),
                repair_required=len(repairs), repair_successful=sum(bool(row["tests_passed"]) for row in repairs),
                repair_failed=sum(not row["tests_passed"] for row in repairs),
                repair_success_rate_percent=round(sum(bool(row["tests_passed"]) for row in repairs) * 100 / len(repairs), 2) if repairs else None,
                average_repair_attempts=_mean(row["repair_attempts"] for row in repairs),
            ),
            approvals=ApprovalSummary(
                total_approvals_requested=sum(row["approvals_requested"] for row in rows),
                total_approvals_granted=sum(row["approvals_granted"] for row in rows),
                total_approvals_rejected=sum(row["approvals_rejected"] for row in rows),
                workflows_with_pending_approval=sum(bool(row["pending_operation"]) for row in rows),
                average_approval_wait_seconds=_mean(row["approval_wait_seconds"] for row in rows if row["approvals_requested"]),
                longest_approval_wait_seconds=max((float(row["longest_approval_wait_seconds"]) for row in rows if row["longest_approval_wait_seconds"] is not None), default=None),
                most_requested_operations=_items(operations, 8),
            ),
            frameworks=_items(Counter(row["framework"] for row in rows if row["framework"])),
            intents=_items(Counter(row["workflow_intent"] for row in rows if row["workflow_intent"])),
            top_findings=_items(Counter(item for row in rows for item in row["findings"]), 8),
            top_recommendations=_items(Counter(item for row in rows for item in row["recommendations"]), 8),
            calculated_at=datetime.now(UTC), source_updated_at=datetime.fromisoformat(max(sources).replace("Z", "+00:00")) if sources else None,
            data_complete=all(bool(row["data_complete"]) for row in rows),
        )

    async def timeseries(self, filters: DashboardFilters, *, interval: str, metric: str) -> DashboardTimeSeriesResponse:
        canonical = METRIC_ALIASES.get(metric, metric)
        rows = await self._filtered(filters)
        zone = ZoneInfo(filters.timezone)
        buckets: defaultdict[float, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            local = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).astimezone(zone)
            buckets[self._bucket(local, interval).timestamp()].append(row)

        def point_value(items: list[dict[str, Any]]) -> tuple[float | None, float | None, float | None]:
            if canonical == "workflow_count": return float(len(items)), None, None
            if canonical == "completed_count": return float(sum(x["status_group"] == "completed" for x in items)), None, None
            if canonical == "failed_count": return float(sum(x["status_group"] == "failed" for x in items)), None, None
            if canonical == "success_rate":
                numerator = sum(x["status_group"] == "completed" for x in items); denominator = numerator + sum(x["status_group"] == "failed" for x in items)
                return (round(numerator * 100 / denominator, 2) if denominator else None, float(numerator), float(denominator))
            if canonical == "average_score":
                values = [x["selected_score"] for x in items if x["selected_evaluation_status"] == "final"]
                return _mean(values), None, None
            if canonical == "average_duration": return _mean(x["total_seconds"] for x in items), None, None
            if canonical == "average_active_duration": return _mean(x["active_seconds"] for x in items), None, None
            if canonical == "average_approval_wait": return _mean(x["approval_wait_seconds"] for x in items), None, None
            if canonical == "tests_passed": return float(sum(x["tests_executed"] and x["tests_passed"] for x in items)), None, None
            if canonical == "tests_failed": return float(sum(x["tests_executed"] and not x["tests_passed"] for x in items)), None, None
            if canonical == "repair_count": return float(sum(x["repair_attempts"] > 0 for x in items)), None, None
            if canonical == "warning_count": return float(sum(x["test_warnings"] for x in items)), None, None
            if canonical == "approval_rejections": return float(sum(x["approvals_rejected"] for x in items)), None, None
            if canonical == "test_pass_rate":
                tested = [x for x in items if x["tests_executed"]]; numerator = sum(x["tests_passed"] for x in tested)
                return (round(numerator * 100 / len(tested), 2) if tested else None, float(numerator), float(len(tested)))
            repaired = [x for x in items if x["repair_attempts"] > 0]
            return (round(len(repaired) * 100 / len(items), 2) if items else None, float(len(repaired)), float(len(items)))

        cursor = self._bucket(filters.date_from.astimezone(zone), interval); end = self._bucket(filters.date_to.astimezone(zone), interval)
        points = []
        while cursor.timestamp() <= end.timestamp():
            bucket_end = self._next_bucket(cursor, interval)
            bucket_rows = buckets.get(cursor.timestamp(), [])
            value, numerator, denominator = point_value(bucket_rows)
            if not bucket_rows and canonical in COUNT_METRICS:
                value = 0.0
            points.append(build_timeseries_point(
                bucket_start=cursor, bucket_end=bucket_end, value=value,
                count=len(bucket_rows), numerator=numerator,
                denominator=denominator,
            ))
            cursor = bucket_end
        source = max((row["source_updated_at"] for row in rows if row["source_updated_at"]), default=None)
        return DashboardTimeSeriesResponse(metric=canonical, interval=interval, timezone=filters.timezone, points=points, source_updated_at=datetime.fromisoformat(source.replace("Z", "+00:00")) if source else None)

    @staticmethod
    def _bucket(value: datetime, interval: str) -> datetime:
        if interval == "hour": return value.replace(minute=0, second=0, microsecond=0)
        if interval == "day": return value.replace(hour=0, minute=0, second=0, microsecond=0)
        if interval == "week": return (value - timedelta(days=value.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _next_bucket(value: datetime, interval: str) -> datetime:
        if interval == "hour":
            return (value.astimezone(UTC) + timedelta(hours=1)).astimezone(value.tzinfo)
        if interval == "day": return (value + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if interval == "week": return (value + timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        return value.replace(year=value.year + (value.month == 12), month=1 if value.month == 12 else value.month + 1)

    async def agents(self, filters: DashboardFilters, *, limit: int, offset: int, sort_by: str, sort_order: str) -> DashboardAgentsResponse:
        rows = await self._filtered(filters)
        grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        workflows: defaultdict[str, set[str]] = defaultdict(set)
        for row in rows:
            for agent in row["agents"]:
                name = _canonical_agent(agent["agent"]); grouped[name].append({**agent, "score": row["selected_score"], "findings": len(row["findings"])}); workflows[name].add(row["thread_id"])
        items = []
        for agent, values in grouped.items():
            total = len(values); completed = sum(x["status"] == "completed" for x in values)
            items.append(DashboardAgentMetric(
                agent=agent, total_tasks=total, completed_tasks=completed,
                failed_tasks=sum(x["status"] == "failed" for x in values), waiting_tasks=sum(x["status"] == "waiting" for x in values),
                skipped_tasks=sum(x["status"] == "skipped" for x in values), completion_rate_percent=round(completed * 100 / total, 2) if total else None,
                average_attempts=_mean(x["attempt"] for x in values), average_duration_seconds=_mean(x["duration_seconds"] for x in values),
                related_workflows=len(workflows[agent]), related_files=sum(int(x.get("related_files", 0)) for x in values),
                findings_count=sum(x["findings"] for x in values), average_score=_mean(x["score"] for x in values),
            ))
        allowed = set(DashboardAgentMetric.model_fields)
        key = sort_by if sort_by in allowed else "total_tasks"
        items.sort(key=lambda item: str(getattr(item, key)) if key == "agent" else (getattr(item, key) is not None, getattr(item, key) or 0), reverse=sort_order == "desc")
        total = len(items)
        return DashboardAgentsResponse(items=items[offset:offset + limit], total=total, limit=limit, offset=offset, has_more=offset + limit < total)

    async def attention(self, filters: DashboardFilters, *, limit: int, offset: int) -> DashboardAttentionResponse:
        now = datetime.now(UTC); items = []
        for row in await self._filtered(filters):
            reasons: list[str] = []; severity = "warning"
            findings = list(row["findings"])
            if row["terminal_status"] == "supervisor_loop_detected" or row["selected_grade"] == "critical" or (row["repair_attempts"] and not row["tests_passed"]): severity = "critical"; reasons.append("critical_failure")
            elif row["status_group"] == "failed" or row["selected_grade"] == "poor" or (row["tests_executed"] and not row["tests_passed"]): severity = "error"; reasons.append("workflow_failed")
            if row["pending_operation"]: reasons.append("approval_pending")
            if row["selected_score"] is not None and row["selected_score"] < 70: reasons.append("low_score")
            if row["test_warnings"] >= 2: reasons.append("elevated_warnings")
            if not row["data_complete"]: reasons.append("incomplete_data")
            if not reasons: continue
            updated = datetime.fromisoformat(row["source_updated_at"].replace("Z", "+00:00")) if row["source_updated_at"] else None
            items.append(DashboardAttentionItem(
                thread_id=row["thread_id"], branch_id=row["branch_id"], project_name=row["project_name"], terminal_status=row["terminal_status"],
                score=row["selected_score"], grade=row["selected_grade"], severity=severity, reasons=list(dict.fromkeys(reasons)),
                finding_codes=findings, pending_operation=row["pending_operation"], age_seconds=max((now - updated).total_seconds(), 0) if updated else 0, updated_at=updated,
            ))
        priority = {"critical": 0, "error": 1, "warning": 2}
        items.sort(key=lambda item: (priority[item.severity], -(item.updated_at.timestamp() if item.updated_at else 0)))
        total = len(items)
        return DashboardAttentionResponse(items=items[offset:offset + limit], total=total, limit=limit, offset=offset, has_more=offset + limit < total)

    async def activity(self, filters: DashboardFilters, *, limit: int, offset: int, include_technical: bool = False) -> DashboardActivityResponse:
        selected = await self._filtered(filters)
        rows, total = await self.store.activity_rows(
            date_from=filters.date_from.isoformat(), date_to=filters.date_to.isoformat(), branch_scope=filters.branch_scope,
            limit=limit, offset=offset, allowed_keys=[(row["thread_id"], row["branch_id"]) for row in selected], include_technical=include_technical,
        )
        names = {"workflow_started": ("workflow_created", "Workflow creado"), "workflow_completed": ("workflow_completed", "Workflow completado"), "workflow_failed": ("workflow_failed", "Workflow fallido"), "approval_required": ("approval_required", "Aprobación requerida"), "repair_started": ("repair_started", "Reparación iniciada"), "repair_completed": ("repair_completed", "Reparación completada"), "supervisor_loop_detected": ("supervisor_loop_detected", "Loop del supervisor detectado"), "approval_granted": ("approval_granted", "Aprobación concedida"), "approval_rejected": ("approval_rejected", "Aprobación rechazada"), "tool_started": ("tool_started", "Operación iniciada"), "tool_completed": ("tool_completed", "Operación completada"), "tool_failed": ("tool_failed", "Operación fallida")}
        items = []
        for row in rows:
            try: data = json.loads(row["data_json"])
            except (TypeError, json.JSONDecodeError): data = {}
            event_type, message = names.get(row["event_type"], (row["event_type"], "Actividad del workflow"))
            operation = data.get("operation") or data.get("tool_name")
            if operation: message = f"{message}: {str(operation)[:80]}"
            items.append(DashboardActivityItem(
                event_id=row["event_id"], thread_id=row["thread_id"], branch_id=row["branch_id"], project_name=row.get("project_name"),
                type=event_type, status=row["status"], message=message, timestamp=datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                related_event_id=str(data.get("related_event_id")) if data.get("related_event_id") else None,
            ))
        return DashboardActivityResponse(items=items, total=total, limit=limit, offset=offset, has_more=offset + limit < total)
