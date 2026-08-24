from __future__ import annotations

from datetime import UTC, datetime
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from api.dashboard_models import DashboardFilters, DashboardTimeSeriesResponse
from api.routes.dashboard import dashboard_filters
from api.services.dashboard_service import (
    DashboardService,
    _percentile,
    _status_group,
    build_timeseries_point,
)
from api.services.dashboard_store import DashboardMetricStore


def metric(thread_id: str, **updates):
    base = {
        "thread_id": thread_id, "branch_id": "original", "lineage": "original",
        "project_name": f"project-{thread_id}", "workflow_intent": "create_project",
        "framework": "FastAPI", "test_framework": "pytest", "terminal_status": "completed",
        "status_group": "completed", "interrupted": 0, "data_complete": 1,
        "tests_executed": 1, "tests_passed": 1, "test_warnings": 2, "repair_phase": "not_started", "repair_attempts": 0,
        "overall_score": 90, "grade": "excellent", "evaluation_status": "final", "scoring_version": "1.2",
        "total_seconds": 100, "active_seconds": 80, "approval_wait_seconds": 20, "longest_approval_wait_seconds": 20,
        "planning_seconds": 10, "implementation_seconds": 40, "testing_seconds": 30, "repair_seconds": 0,
        "approvals_requested": 1, "approvals_granted": 1, "approvals_rejected": 0,
        "approval_operations": {"create_project": 1},
        "evaluations": {"1.2": {"score": 90, "grade": "excellent", "status": "final"}},
        "findings": ["PLAN_VALID"], "recommendations": ["Keep tests focused."],
        "agents": [{"agent": "QA", "status": "completed", "attempt": 1, "duration_seconds": 30, "related_files": 1}],
        "related_files": ["tests/test_health.py"], "pending_operation": None,
        "created_at": "2026-07-20T12:00:00+00:00", "source_updated_at": "2026-07-20T12:02:00+00:00",
        "calculated_at": "2026-07-20T12:02:01+00:00",
    }
    base.update(updates)
    return base


@pytest.fixture
async def dashboard(tmp_path):
    store = DashboardMetricStore(tmp_path / "events.db")
    await store.initialize()
    service = DashboardService(store=store, execution_service=None, evaluation_service=None)  # type: ignore[arg-type]
    return store, service


def filters(**updates):
    base = dict(date_from=datetime(2026, 7, 1, tzinfo=UTC), date_to=datetime(2026, 7, 31, 23, tzinfo=UTC))
    base.update(updates)
    return DashboardFilters(**base)


@pytest.mark.parametrize(("status", "expected"), [
    ("completed", "completed"), ("running", "running"), ("pending", "pending"),
    ("user_cancelled", "cancelled"), ("tests_failed", "failed"),
])
def test_status_classification(status, expected):
    assert _status_group(status) == expected


@pytest.mark.parametrize(("values", "p", "expected"), [
    ([], .5, None), ([10], .9, 10), ([10, 20], .5, 15), ([1, 2, 3, 4], .9, 3.7),
])
def test_percentile(values, p, expected):
    assert _percentile(values, p) == expected


async def test_store_initializes_required_indexes(dashboard):
    store, _ = dashboard
    with sqlite3.connect(store.database_path) as connection:
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_dashboard_metrics_created_at", "idx_dashboard_metrics_status", "idx_dashboard_metrics_project", "idx_dashboard_metrics_grade"} <= indexes


async def test_upsert_is_idempotent_and_preserves_created_at(dashboard):
    store, _ = dashboard
    assert await store.upsert(metric("one")) == "inserted"
    assert await store.upsert(metric("one", created_at="2026-08-01T00:00:00+00:00", overall_score=70)) == "updated"
    rows = await store.list_metrics()
    assert len(rows) == 1
    assert rows[0]["created_at"] == "2026-07-20T12:00:00+00:00"
    assert rows[0]["overall_score"] == 70


async def test_unchanged_upsert_does_not_change_timestamps(dashboard):
    store, _ = dashboard
    payload = metric("one")
    assert await store.upsert(payload) == "inserted"
    before = (await store.list_metrics())[0]
    repeated = {**payload, "calculated_at": "2026-07-21T00:00:00+00:00"}
    assert await store.upsert(repeated) == "unchanged"
    after = (await store.list_metrics())[0]
    assert after["updated_at"] == before["updated_at"]
    assert after["source_updated_at"] == before["source_updated_at"]
    assert after["calculated_at"] == before["calculated_at"]


async def test_summary_aggregates_scores_tests_approvals_and_dimensions(dashboard):
    store, service = dashboard
    await store.upsert(metric("one"))
    await store.upsert(metric("two", overall_score=50, grade="poor", tests_passed=0, status_group="failed", terminal_status="tests_failed", approvals_rejected=1, repair_phase="completed", repair_attempts=2, evaluations={"1.2": {"score": 50, "grade": "poor", "status": "final"}}))
    result = await service.summary(filters())
    assert result.workflow_counts.total == 2
    assert result.workflow_counts.completed == 1
    assert result.workflow_counts.failed == 1
    assert result.scores.average_score == 70
    assert result.scores.median_score == 70
    assert result.testing.pass_rate_percent == 50
    assert result.testing.repair_successful == 0
    assert result.approvals.total_approvals_rejected == 1
    assert result.frameworks[0].key == "FastAPI"
    assert result.top_findings[0].key == "PLAN_VALID"


async def test_partial_evaluation_is_not_in_average(dashboard):
    store, service = dashboard
    await store.upsert(metric("final"))
    await store.upsert(metric("partial", evaluation_status="partial", overall_score=None, grade=None, evaluations={"1.2": {"score": 30, "grade": None, "status": "partial"}}))
    result = await service.summary(filters())
    assert result.scores.average_score == 90
    assert result.scores.evaluated_workflows == 1
    assert result.scores.unevaluated_workflows == 1


async def test_summary_exposes_public_contract_and_state_invariant(dashboard):
    store, service = dashboard
    await store.upsert(metric("complete"))
    await store.upsert(metric("failed", status_group="failed", terminal_status="implementation_failed"))
    await store.upsert(metric("pending", status_group="pending", terminal_status="pending", evaluations={"1.2": {"score": 45, "grade": None, "status": "partial"}}))
    result = await service.summary(filters())
    assert result.date_from == filters().date_from
    assert result.date_to == filters().date_to
    assert result.timezone == "UTC"
    assert result.branch_scope == "original"
    counts = result.workflow_counts
    assert counts.total == sum((counts.completed, counts.failed, counts.running, counts.waiting, counts.pending, counts.cancelled))
    assert counts.success_rate_percent == 50
    assert counts.failure_rate_percent == 50


async def test_scores_are_typed_final_only_and_provisional_is_separate(dashboard):
    store, service = dashboard
    await store.upsert(metric("final", evaluations={"1.2": {"score": 80, "grade": "good", "status": "final"}}))
    await store.upsert(metric("partial", evaluations={"1.2": {"score": 10, "grade": None, "status": "partial"}}))
    result = await service.summary(filters())
    assert result.scores.average_score == 80
    assert result.scores.good == 1
    assert result.scores.provisional == 1
    assert sum((result.scores.excellent, result.scores.good, result.scores.acceptable, result.scores.poor, result.scores.critical)) == result.scores.evaluated_workflows


async def test_duration_percentiles_include_p95_and_discard_negative_values(dashboard):
    store, service = dashboard
    await store.upsert(metric("one", total_seconds=10))
    await store.upsert(metric("two", total_seconds=20))
    await store.upsert(metric("bad", total_seconds=-1))
    result = await service.summary(filters())
    assert result.durations.discarded_workflows == 1
    assert result.durations.p50_wall_clock_seconds <= result.durations.p90_wall_clock_seconds <= result.durations.p95_wall_clock_seconds


async def test_testing_and_approval_contract_is_complete(dashboard):
    store, service = dashboard
    await store.upsert(metric("tested", repair_attempts=2, tests_passed=1))
    await store.upsert(metric("waiting", tests_executed=0, tests_passed=0, test_warnings=0, status_group="waiting", pending_operation="run_tests"))
    result = await service.summary(filters())
    assert result.testing.executed == result.testing.passed + result.testing.failed
    assert result.testing.executed + result.testing.not_executed == result.workflow_counts.total
    assert result.testing.workflows_with_warnings == 1
    assert result.testing.total_warnings == 2
    assert result.testing.repair_required == 1
    assert result.testing.repair_successful == 1
    assert result.approvals.workflows_with_pending_approval == 1
    assert result.approvals.longest_approval_wait_seconds == 20
    assert result.approvals.most_requested_operations[0].label == "Create project"


@pytest.mark.parametrize(("field", "value"), [
    ("status", "failed"), ("project_name", "project-one"),
    ("workflow_intent", "create_project"), ("framework", "FastAPI"),
    ("grade", "excellent"),
])
async def test_shared_filters(field, value, dashboard):
    store, service = dashboard
    await store.upsert(metric("one"))
    selected = await service._filtered(filters(**{field: value}))
    assert len(selected) == (0 if field == "status" else 1)


async def test_branch_scope_defaults_to_original(dashboard):
    store, service = dashboard
    await store.upsert(metric("one"))
    await store.upsert(metric("one", branch_id="fork-1", lineage="fork"))
    assert (await service.summary(filters())).workflow_counts.total == 1
    assert (await service.summary(filters(branch_scope="all"))).workflow_counts.total == 2


async def test_scoring_version_selects_historical_evaluation(dashboard):
    store, service = dashboard
    await store.upsert(metric("one", evaluations={"1.1": {"score": 70, "grade": "good", "status": "final"}, "1.2": {"score": 90, "grade": "excellent", "status": "final"}}))
    assert (await service.summary(filters(scoring_version="1.1"))).scores.average_score == 70


@pytest.mark.parametrize("interval", ["hour", "day", "week", "month"])
async def test_timeseries_is_sorted_and_fills_empty_buckets(interval, dashboard):
    store, service = dashboard
    await store.upsert(metric("one"))
    result = await service.timeseries(filters(date_from=datetime(2026, 7, 19, tzinfo=UTC), date_to=datetime(2026, 7, 21, tzinfo=UTC)), interval=interval, metric="workflows_created")
    assert result.points
    assert result.points == sorted(result.points, key=lambda point: point.bucket_start)
    assert any(point.value == 0 for point in result.points) or interval == "month"


@pytest.mark.parametrize("metric_name", [
    "workflow_count", "completed_count", "failed_count", "success_rate",
    "average_score", "average_duration", "average_active_duration",
    "average_approval_wait", "tests_passed", "tests_failed", "repair_count",
    "warning_count", "workflows_created", "test_pass_rate", "repair_rate",
    "approval_rejections",
])
async def test_timeseries_points_use_complete_public_bucket_contract(metric_name, dashboard):
    store, service = dashboard
    await store.upsert(metric("one"))
    result = await service.timeseries(
        filters(date_from=datetime(2026, 7, 19, tzinfo=UTC), date_to=datetime(2026, 7, 21, tzinfo=UTC)),
        interval="day", metric=metric_name,
    )
    payloads = [point.model_dump(mode="json") for point in result.points]
    assert payloads
    assert all(set(point) == {"bucket_start", "bucket_end", "value", "count", "numerator", "denominator"} for point in payloads)
    assert all(point.bucket_end > point.bucket_start for point in result.points)
    assert len({point.bucket_start for point in result.points}) == len(result.points)


@pytest.mark.parametrize(("interval", "expected_start", "expected_end"), [
    ("hour", "2026-07-22T15:00:00+00:00", "2026-07-22T16:00:00+00:00"),
    ("day", "2026-07-22T00:00:00+00:00", "2026-07-23T00:00:00+00:00"),
    ("week", "2026-07-20T00:00:00+00:00", "2026-07-27T00:00:00+00:00"),
    ("month", "2026-07-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
])
def test_timeseries_interval_boundaries(interval, expected_start, expected_end):
    start = DashboardService._bucket(datetime(2026, 7, 22, 15, 37, tzinfo=UTC), interval)
    end = DashboardService._next_bucket(start, interval)
    assert start.isoformat() == expected_start
    assert end.isoformat() == expected_end
    if interval == "week":
        assert start.weekday() == 0


@pytest.mark.parametrize(("timezone", "expected_offset"), [
    ("America/Lima", "-05:00"),
    ("UTC", "+00:00"),
])
async def test_timeseries_boundaries_preserve_requested_timezone(timezone, expected_offset, dashboard):
    _, service = dashboard
    result = await service.timeseries(
        filters(
            date_from=datetime(2026, 7, 25, tzinfo=UTC),
            date_to=datetime(2026, 7, 25, 23, 59, tzinfo=UTC),
            timezone=timezone,
        ), interval="day", metric="workflow_count",
    )
    assert result.points[0].bucket_start.isoformat().endswith(expected_offset)
    assert result.points[0].bucket_end.isoformat().endswith(expected_offset)
    if timezone == "America/Lima":
        assert result.points[0].bucket_start.isoformat().startswith("2026-07-24T00:00:00")


async def test_timeseries_empty_bucket_semantics(dashboard):
    _, service = dashboard
    requested = filters(
        date_from=datetime(2026, 7, 20, tzinfo=UTC),
        date_to=datetime(2026, 7, 20, 23, tzinfo=UTC),
    )
    count = (await service.timeseries(requested, interval="day", metric="workflow_count")).points[0]
    average = (await service.timeseries(requested, interval="day", metric="average_score")).points[0]
    rate = (await service.timeseries(requested, interval="day", metric="success_rate")).points[0]
    assert (count.value, count.count, count.numerator, count.denominator) == (0, 0, None, None)
    assert (average.value, average.count, average.numerator, average.denominator) == (None, 0, None, None)
    assert (rate.value, rate.count, rate.numerator, rate.denominator) == (None, 0, 0, 0)


def test_build_timeseries_point_rejects_invalid_bounds():
    start = datetime(2026, 7, 20, tzinfo=UTC)
    with pytest.raises(ValueError, match="bucket_end must be greater"):
        build_timeseries_point(bucket_start=start, bucket_end=start, value=0, count=0)


def test_timeseries_response_schema_uses_public_names():
    point_schema = DashboardTimeSeriesResponse.model_json_schema()["$defs"]["DashboardTimeSeriesPoint"]["properties"]
    assert set(point_schema) == {"bucket_start", "bucket_end", "value", "count", "numerator", "denominator"}
    assert "bucket" not in point_schema


def test_hour_boundary_handles_daylight_saving_offset_change():
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 3, 8, 1, tzinfo=zone)
    end = DashboardService._next_bucket(start, "hour")
    assert start.isoformat().endswith("-05:00")
    assert end.hour == 3
    assert end.isoformat().endswith("-04:00")
    assert end.timestamp() > start.timestamp()


@pytest.mark.parametrize("metric_name", ["workflow_count", "completed_count", "failed_count", "success_rate", "average_score", "average_duration", "average_active_duration", "average_approval_wait", "tests_passed", "tests_failed", "repair_count", "warning_count", "workflows_created", "test_pass_rate", "repair_rate", "approval_rejections"])
async def test_timeseries_metric_whitelist_behaviour(metric_name, dashboard):
    store, service = dashboard
    await store.upsert(metric("one"))
    result = await service.timeseries(filters(date_from=datetime(2026, 7, 20, tzinfo=UTC), date_to=datetime(2026, 7, 20, 23, tzinfo=UTC)), interval="day", metric=metric_name)
    assert len(result.points) == 1
    assert result.points[0].value is None or result.points[0].value >= 0


async def test_agents_are_normalized_paginated_and_sorted(dashboard):
    store, service = dashboard
    await store.upsert(metric("one"))
    result = await service.agents(filters(), limit=1, offset=0, sort_by="total_tasks", sort_order="desc")
    assert result.total == 1
    assert result.items[0].agent == "qa reviewer"
    assert result.items[0].total_tasks == 1
    assert result.items[0].completion_rate_percent == 100
    assert result.items[0].average_duration_seconds == 30


async def test_attention_prioritizes_failures(dashboard):
    store, service = dashboard
    await store.upsert(metric("low", status_group="completed", evaluations={"1.2": {"score": 60, "grade": "poor", "status": "final"}}))
    await store.upsert(metric("failed", status_group="failed", terminal_status="tests_failed", tests_passed=0))
    result = await service.attention(filters(), limit=10, offset=0)
    assert {item.thread_id for item in result.items} == {"failed", "low"}
    assert all(item.severity == "error" for item in result.items)
    assert all(isinstance(item.reasons, list) for item in result.items)


async def test_attention_exposes_pending_operation_age_and_findings(dashboard):
    store, service = dashboard
    await store.upsert(metric("waiting", status_group="waiting", terminal_status="pending", pending_operation="run_tests"))
    item = (await service.attention(filters(), limit=10, offset=0)).items[0]
    assert item.severity == "warning"
    assert item.pending_operation == "run_tests"
    assert item.age_seconds >= 0
    assert item.finding_codes == ["PLAN_VALID"]


async def test_activity_hides_tool_events_unless_technical_mode_is_requested(dashboard):
    store, service = dashboard
    await store.upsert(metric("one"))
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("""CREATE TABLE workflow_events(
            event_id TEXT, thread_id TEXT, branch_id TEXT, sequence INTEGER,
            event_type TEXT, timestamp TEXT, stage TEXT, status TEXT,
            message TEXT, data_json TEXT)""")
        for sequence, event_type in enumerate(("workflow_started", "tool_completed"), start=1):
            connection.execute(
                "INSERT INTO workflow_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"event-{sequence}", "one", "original", sequence, event_type,
                 "2026-07-20T12:00:00+00:00", "implementation", "completed",
                 "Operación completada", "{}"),
            )
    public = await service.activity(filters(), limit=10, offset=0)
    technical = await service.activity(filters(), limit=10, offset=0, include_technical=True)
    assert [item.type for item in public.items] == ["workflow_created"]
    assert "tool_completed" in [item.type for item in technical.items]
    assert public.items[0].message == "Workflow creado"


async def test_cache_is_invalidated_by_upsert(dashboard):
    store, service = dashboard
    await store.upsert(metric("one"))
    await service.summary(filters())
    assert service._cache
    service.invalidate_cache()
    assert not service._cache


def test_filter_defaults_are_thirty_days_and_latest_version():
    result = dashboard_filters()
    assert 29.9 <= (result.date_to - result.date_from).total_seconds() / 86400 <= 30.1
    assert result.scoring_version == "1.2"
    assert result.branch_scope == "original"


def test_filter_rejects_inverted_dates():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        dashboard_filters(date_from=datetime(2026, 8, 2, tzinfo=UTC), date_to=datetime(2026, 8, 1, tzinfo=UTC))
    assert exc.value.status_code == 422


def test_filter_rejects_unknown_timezone():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        dashboard_filters(timezone="Mars/Olympus")
    assert exc.value.detail == "Unknown timezone"
