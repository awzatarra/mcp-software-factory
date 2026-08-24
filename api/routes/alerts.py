from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from api.alert_models import (
    AlertActionRequest, AlertDetailResponse, AlertEvaluationRequest,
    AlertEvaluationResult, AlertListResponse, AlertMuteRequest, AlertRule,
    AlertRuleListResponse, AlertRuleUpdate, AlertSummaryResponse, WorkflowAlert,
)
from api.dependencies import ApiServices, get_services
from api.services.alert_service import (
    AlertEvaluationService, AlertNotFoundError, AlertTransitionError,
    AlertWorkflowNotFoundError,
)


router = APIRouter(prefix="/api/alerts", tags=["alerts"])
RuleCode = Literal[
    "APPROVAL_WAIT_TOO_LONG", "WORKFLOW_FAILED", "TESTS_FAILED",
    "REPAIR_FAILED", "LOW_EVALUATION_SCORE", "SUPERVISOR_LOOP_DETECTED",
    "SUPERVISOR_FALLBACK", "HIGH_WARNING_COUNT", "EXCESSIVE_RETRIES",
    "WORKFLOW_DURATION_ANOMALY", "EVENT_SEQUENCE_GAP",
    "DASHBOARD_METRIC_STALE",
]


def _service(services: ApiServices = Depends(get_services)) -> AlertEvaluationService:
    if services.alerts is None:
        raise HTTPException(status_code=503, detail="Alert service unavailable")
    return services.alerts


@router.get("/rules", response_model=AlertRuleListResponse)
async def list_alert_rules(
    enabled: bool | None = None,
    category: Literal["workflow", "approval", "testing", "repair", "evaluation", "supervisor", "events", "dashboard", "llm_cost"] | None = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    service: AlertEvaluationService = Depends(_service),
):
    return await service.list_rules(enabled=enabled, category=category, search=search, limit=limit, offset=offset)


@router.get("/rules/{rule_id}", response_model=AlertRule)
async def get_alert_rule(rule_id: str, service: AlertEvaluationService = Depends(_service)):
    rule = await service.store.get_rule(rule_id)
    if rule is None: raise HTTPException(status_code=404, detail="Alert rule not found")
    return rule


@router.patch("/rules/{rule_id}", response_model=AlertRule)
async def update_alert_rule(rule_id: str, update: AlertRuleUpdate, service: AlertEvaluationService = Depends(_service)):
    rule = await service.store.update_rule(rule_id, update.model_dump(exclude_unset=True))
    if rule is None: raise HTTPException(status_code=404, detail="Alert rule not found")
    return rule


@router.post("/rules/{rule_id}/reset", response_model=AlertRule)
async def reset_alert_rule(rule_id: str, service: AlertEvaluationService = Depends(_service)):
    rule = await service.store.reset_rule(rule_id)
    if rule is None: raise HTTPException(status_code=404, detail="Alert rule not found")
    return rule


@router.get("/summary", response_model=AlertSummaryResponse)
async def get_alert_summary(
    date_from: datetime | None = None, date_to: datetime | None = None,
    branch_scope: Literal["original", "all"] = "original",
    project_name: Annotated[str | None, Query(max_length=120)] = None,
    service: AlertEvaluationService = Depends(_service),
):
    if date_from and date_to and date_from > date_to: raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    return await service.summary(date_from=date_from, date_to=date_to, branch_scope=branch_scope, project_name=project_name)


@router.post("/evaluate", response_model=AlertEvaluationResult)
async def evaluate_alerts(request: AlertEvaluationRequest, service: AlertEvaluationService = Depends(_service)):
    try: return await service.evaluate_workflow(thread_id=request.thread_id, branch_id=request.branch_id)
    except AlertWorkflowNotFoundError as exc: raise HTTPException(status_code=404, detail="Workflow not found") from exc


@router.get("", response_model=AlertListResponse)
async def list_alerts(
    status: Annotated[str, Query(pattern=r"^(all|(open|acknowledged|resolved|muted)(,(open|acknowledged|resolved|muted))*)$")] = "open,acknowledged",
    severity: Literal["info", "warning", "error", "critical"] | None = None,
    rule_code: RuleCode | None = None,
    category: Literal["workflow", "approval", "testing", "repair", "evaluation", "supervisor", "events", "dashboard", "llm_cost"] | None = None,
    thread_id: Annotated[str | None, Query(max_length=200)] = None,
    branch_id: Annotated[str | None, Query(max_length=200)] = None,
    project_name: Annotated[str | None, Query(max_length=120)] = None,
    date_from: datetime | None = None, date_to: datetime | None = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    sort_by: Literal["severity", "last_detected_at", "first_detected_at", "occurrence_count"] = "severity",
    sort_order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    service: AlertEvaluationService = Depends(_service),
):
    if date_from and date_to and date_from > date_to: raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    statuses = None if status == "all" else status.split(",")
    return await service.list_alerts(statuses=statuses, severity=severity, rule_code=rule_code, category=category, thread_id=thread_id, branch_id=branch_id, project_name=project_name, date_from=date_from.isoformat() if date_from else None, date_to=date_to.isoformat() if date_to else None, search=search, sort_by=sort_by, sort_order=sort_order, limit=limit, offset=offset)


@router.get("/{alert_id}", response_model=AlertDetailResponse)
async def get_alert_detail(alert_id: str, service: AlertEvaluationService = Depends(_service)):
    try: return await service.detail(alert_id)
    except AlertNotFoundError as exc: raise HTTPException(status_code=404, detail="Alert not found") from exc


async def _transition(alert_id: str, action: str, request: AlertActionRequest, service: AlertEvaluationService, duration_seconds: int | None = None) -> WorkflowAlert:
    try: return await service.transition(alert_id, action, request.actor, request.note, duration_seconds)
    except AlertNotFoundError as exc: raise HTTPException(status_code=404, detail="Alert not found") from exc
    except AlertTransitionError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{alert_id}/acknowledge", response_model=WorkflowAlert)
async def acknowledge_alert(alert_id: str, request: AlertActionRequest, service: AlertEvaluationService = Depends(_service)):
    return await _transition(alert_id, "acknowledge", request, service)


@router.post("/{alert_id}/resolve", response_model=WorkflowAlert)
async def resolve_alert(alert_id: str, request: AlertActionRequest, service: AlertEvaluationService = Depends(_service)):
    return await _transition(alert_id, "resolve", request, service)


@router.post("/{alert_id}/reopen", response_model=WorkflowAlert)
async def reopen_alert(alert_id: str, request: AlertActionRequest, service: AlertEvaluationService = Depends(_service)):
    return await _transition(alert_id, "reopen", request, service)


@router.post("/{alert_id}/mute", response_model=WorkflowAlert)
async def mute_alert(alert_id: str, request: AlertMuteRequest, service: AlertEvaluationService = Depends(_service)):
    return await _transition(alert_id, "mute", request, service, request.duration_seconds)


@router.post("/{alert_id}/unmute", response_model=WorkflowAlert)
async def unmute_alert(alert_id: str, request: AlertActionRequest, service: AlertEvaluationService = Depends(_service)):
    return await _transition(alert_id, "unmute", request, service)
