from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dashboard_models import (
    DashboardActivityResponse,
    DashboardAgentsResponse,
    DashboardAttentionResponse,
    DashboardFilters,
    DashboardSummaryResponse,
    DashboardTimeSeriesResponse,
)
from api.dependencies import ApiServices, get_services
from api.services.dashboard_service import DashboardService
from api.services.workflow_evaluation_service import SCORING_VERSION


router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _service(services: ApiServices = Depends(get_services)) -> DashboardService:
    if services.dashboard is None:
        raise HTTPException(status_code=503, detail="Dashboard service unavailable")
    return services.dashboard


def dashboard_filters(
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    status: Annotated[Literal["all", "completed", "failed", "running", "waiting", "pending", "cancelled"], Query()] = "all",
    project_name: Annotated[str | None, Query(max_length=120)] = None,
    workflow_intent: Annotated[str | None, Query(max_length=100)] = None,
    framework: Annotated[str | None, Query(max_length=100)] = None,
    grade: Annotated[Literal["all", "excellent", "good", "acceptable", "poor", "critical"], Query()] = "all",
    branch_scope: Annotated[Literal["original", "all"], Query()] = "original",
    scoring_version: Annotated[str, Query(pattern=r"^(latest|\d+\.\d+)$")] = "latest",
    timezone: Annotated[str, Query(max_length=100)] = "UTC",
) -> DashboardFilters:
    now = datetime.now(UTC)
    end = date_to or now
    start = date_from or (end - timedelta(days=30))
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if start > end:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail="Unknown timezone")
    return DashboardFilters(
        date_from=start.astimezone(UTC), date_to=end.astimezone(UTC), status=status,
        project_name=project_name.strip() if project_name else None,
        workflow_intent=workflow_intent, framework=framework, grade=grade,
        branch_scope=branch_scope,
        scoring_version=SCORING_VERSION if scoring_version == "latest" else scoring_version,
        timezone=timezone,
    )


Filters = Annotated[DashboardFilters, Depends(dashboard_filters)]


@router.get("/summary", response_model=DashboardSummaryResponse)
async def get_dashboard_summary(filters: Filters, service: DashboardService = Depends(_service)):
    return await service.summary(filters)


@router.get("/timeseries", response_model=DashboardTimeSeriesResponse)
async def get_dashboard_timeseries(
    filters: Filters,
    interval: Annotated[Literal["hour", "day", "week", "month"], Query()] = "day",
    metric: Annotated[Literal[
        "workflow_count", "completed_count", "failed_count", "success_rate",
        "average_score", "average_duration", "average_active_duration",
        "average_approval_wait", "tests_passed", "tests_failed", "repair_count",
        "warning_count", "workflows_created", "test_pass_rate", "repair_rate",
        "approval_rejections",
    ], Query()] = "workflow_count",
    service: DashboardService = Depends(_service),
):
    return await service.timeseries(filters, interval=interval, metric=metric)


@router.get("/agents", response_model=DashboardAgentsResponse)
async def get_dashboard_agents(
    filters: Filters,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort_by: Annotated[Literal["agent", "total_tasks", "completed_tasks", "failed_tasks", "waiting_tasks", "completion_rate_percent", "average_attempts", "average_duration_seconds", "related_workflows", "related_files", "findings_count", "average_score"], Query()] = "total_tasks",
    sort_order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    service: DashboardService = Depends(_service),
):
    return await service.agents(filters, limit=limit, offset=offset, sort_by=sort_by, sort_order=sort_order)


@router.get("/attention", response_model=DashboardAttentionResponse)
async def get_dashboard_attention(
    filters: Filters,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    service: DashboardService = Depends(_service),
):
    return await service.attention(filters, limit=limit, offset=offset)


@router.get("/activity", response_model=DashboardActivityResponse)
async def get_dashboard_activity(
    filters: Filters,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_technical: Annotated[bool, Query()] = False,
    service: DashboardService = Depends(_service),
):
    return await service.activity(
        filters, limit=limit, offset=offset,
        include_technical=include_technical,
    )
