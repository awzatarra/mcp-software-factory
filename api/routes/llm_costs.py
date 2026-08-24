from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import ApiServices, get_services
from api.llm_cost_models import (
    BudgetCheckRequest, BudgetCreate, BudgetPatch, BudgetResetRequest,
    PricingCreate, PricingPatch, PricingResolveRequest, RecalculateRequest,
)
from api.services.llm_cost_store import PricingOverlapError


router = APIRouter(prefix="/api/llm-costs", tags=["llm-costs"])


def _service(services: ApiServices):
    if services.llm_costs is None:
        raise HTTPException(status_code=503, detail="LLM cost service is not available")
    return services.llm_costs


def _aggregate_filters(
    workflow_id: str | None = None, branch_id: str | None = None,
    project_name: str | None = None, date_from: datetime | None = None,
    date_to: datetime | None = None, agent_name: str | None = None,
    provider: str | None = None, model: str | None = None,
    status: str | None = None,
) -> dict[str, str | None]:
    return {
        "workflow_id": workflow_id, "branch_id": branch_id,
        "project_name": project_name,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "agent_name": agent_name, "provider": provider, "model": model,
        "status": status,
    }


@router.get("/summary")
async def summary(
    workflow_id: str | None = None, branch_id: str | None = None,
    project_name: str | None = None, date_from: datetime | None = None,
    date_to: datetime | None = None, currency: str | None = None,
    agent_name: str | None = None, provider: str | None = None,
    model: str | None = None, status: str | None = None,
    services: ApiServices = Depends(get_services),
):
    return await _service(services).summary(
        workflow_id=workflow_id, branch_id=branch_id, project_name=project_name,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        agent_name=agent_name, provider=provider, model=model, status=status,
        currency=currency,
    )


@router.get("/calls")
async def calls(
    workflow_id: str | None = None, branch_id: str | None = None,
    project_name: str | None = None, date_from: datetime | None = None,
    date_to: datetime | None = None,
    agent_name: str | None = None, provider: str | None = None,
    model: str | None = None, status: str | None = None, cost_source: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort_by: str = Query(default="timestamp", pattern="^(timestamp|duration_ms|total_tokens|model|status|total_cost)$"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    services: ApiServices = Depends(get_services),
):
    # Sort fields are validated even though the durable default ordering is used in v1.
    return await _service(services).list_calls(
        workflow_id=workflow_id, branch_id=branch_id, project_name=project_name,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None, agent_name=agent_name,
        provider=provider, model=model, status=status, cost_source=cost_source,
        sort_by=sort_by, sort_order=sort_order, limit=limit, offset=offset,
    )


@router.get("/calls/{llm_call_id}")
async def call_detail(llm_call_id: str, services: ApiServices = Depends(get_services)):
    result = await _service(services).call_detail(llm_call_id)
    if result is None:
        raise HTTPException(status_code=404, detail="LLM call not found")
    return result


@router.get("/workflows/{thread_id}")
async def workflow_costs(
    thread_id: str, branch_id: str = "original",
    services: ApiServices = Depends(get_services),
):
    service = _service(services)
    return {
        "thread_id": thread_id, "branch_id": branch_id,
        "summary": await service.summary(workflow_id=thread_id, branch_id=branch_id),
        **await service.list_calls(workflow_id=thread_id, branch_id=branch_id, limit=500, offset=0),
    }


async def _aggregate(dimension: str, services: ApiServices, filters: dict[str, str | None]):
    return await _service(services).aggregate(dimension, **filters)


@router.get("/agents")
async def agents(
    filters: Annotated[dict[str, str | None], Depends(_aggregate_filters)],
    services: ApiServices = Depends(get_services),
):
    return await _aggregate("agents", services, filters)


@router.get("/models")
async def models(
    filters: Annotated[dict[str, str | None], Depends(_aggregate_filters)],
    services: ApiServices = Depends(get_services),
):
    return await _aggregate("models", services, filters)


@router.get("/providers")
async def providers(
    filters: Annotated[dict[str, str | None], Depends(_aggregate_filters)],
    services: ApiServices = Depends(get_services),
):
    return await _aggregate("providers", services, filters)


@router.get("/operations")
async def operations(
    filters: Annotated[dict[str, str | None], Depends(_aggregate_filters)],
    services: ApiServices = Depends(get_services),
):
    return await _aggregate("operations", services, filters)


@router.get("/timeseries")
async def timeseries(
    filters: Annotated[dict[str, str | None], Depends(_aggregate_filters)],
    services: ApiServices = Depends(get_services),
):
    calls_result = await _service(services).list_calls(limit=10_000, offset=0, **filters)
    groups: dict[str, dict[str, object]] = {}
    for call in calls_result["items"]:
        day = str(call.get("timestamp") or "")[:10] or "unknown"
        item = groups.setdefault(day, {"period": day, "calls": 0, "tokens": 0, "real_cost": Decimal(0), "estimated_cost": Decimal(0)})
        item["calls"] = int(item["calls"]) + 1
        item["tokens"] = int(item["tokens"]) + int(call.get("total_tokens") or 0)
        item["real_cost"] = Decimal(item["real_cost"]) + Decimal(str(call.get("total_cost") or 0))
        item["estimated_cost"] = Decimal(item["estimated_cost"]) + Decimal(str(call.get("estimated_total_cost") or 0))
    return {"items": [{**item, "real_cost": format(Decimal(item["real_cost"]), "f"), "estimated_cost": format(Decimal(item["estimated_cost"]), "f")} for item in sorted(groups.values(), key=lambda value: str(value["period"]))]}


@router.get("/unpriced")
async def unpriced(services: ApiServices = Depends(get_services)):
    result = await _service(services).list_calls(limit=10_000, offset=0)
    return {"items": [item for item in result["items"] if "pricing_not_found" in str(item.get("warnings"))]}


@router.get("/unavailable-usage")
async def unavailable_usage(services: ApiServices = Depends(get_services)):
    result = await _service(services).list_calls(limit=10_000, offset=0)
    return {"items": [item for item in result["items"] if not item.get("usage_available")]}


@router.post("/calculate/{llm_call_id}")
async def calculate(llm_call_id: str, services: ApiServices = Depends(get_services)):
    result = await _service(services).calculate_call(llm_call_id)
    if result is None: raise HTTPException(status_code=404, detail="LLM call not found")
    return result


@router.post("/recalculate/{llm_call_id}")
async def recalculate(llm_call_id: str, body: RecalculateRequest, services: ApiServices = Depends(get_services)):
    result = await _service(services).calculate_call(llm_call_id, recalculate=True, reason=body.reason, actor=body.actor)
    if result is None: raise HTTPException(status_code=404, detail="LLM call not found")
    return result


@router.post("/backfill")
async def backfill(services: ApiServices = Depends(get_services)):
    service = _service(services); calls_result = await service.list_calls(limit=10_000, offset=0)
    counters = {"processed": 0, "calculated": 0, "estimated": 0, "usage_unavailable": 0, "pricing_unavailable": 0, "unchanged": 0, "failed": 0}
    for call in calls_result["items"]:
        counters["processed"] += 1
        before = await service.store.get_calculation(call["call_id"])
        try:
            result = await service.calculate_call(call["call_id"])
            if before: counters["unchanged"] += 1
            elif result and result["cost_source"] == "calculated": counters["calculated"] += 1
            elif result and result["cost_source"] == "estimated": counters["estimated"] += 1
            elif result and "usage_not_available" in result.get("warnings", []): counters["usage_unavailable"] += 1
            else: counters["pricing_unavailable"] += 1
        except Exception: counters["failed"] += 1
    return counters


@router.get("/pricing")
async def pricing_list(
    provider: str | None = None, model: str | None = None, currency: str | None = None,
    enabled: bool | None = None, limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0),
    services: ApiServices = Depends(get_services),
):
    items,total=await _service(services).store.list_pricing(provider=provider,model=model,currency=currency,enabled=enabled,limit=limit,offset=offset)
    return {"items":items,"total":total,"limit":limit,"offset":offset}


@router.post("/pricing", status_code=201)
async def pricing_create(body: PricingCreate, services: ApiServices = Depends(get_services)):
    service = _service(services)
    try:
        item = await service.store.create_pricing(body.model_dump())
        if service.finops_lifecycle is not None:
            await service.finops_lifecycle.reevaluate_pricing_alerts(reason="Valid pricing was created")
        return item
    except PricingOverlapError as exc:raise HTTPException(status_code=409,detail=str(exc)) from exc


@router.post("/pricing/resolve")
async def pricing_resolve(body: PricingResolveRequest, services: ApiServices = Depends(get_services)):
    item=await _service(services).store.resolve_pricing(body.provider,body.model,body.timestamp.isoformat(),body.currency)
    if item is None:return {"pricing":None,"match_type":None,"warnings":["pricing_not_found"]}
    return {"pricing":item,"match_type":item.get("match_type"),"priority":item["priority"],"warnings":[]}


@router.get("/pricing/{pricing_id}")
async def pricing_detail(pricing_id: str, services: ApiServices = Depends(get_services)):
    item=await _service(services).store.get_pricing(pricing_id)
    if item is None:raise HTTPException(status_code=404,detail="Pricing not found")
    return item


@router.patch("/pricing/{pricing_id}")
async def pricing_patch(pricing_id: str, body: PricingPatch, services: ApiServices = Depends(get_services)):
    service = _service(services)
    updates = {field: getattr(body, field) for field in body.model_fields_set}
    try:
        item = await service.store.update_pricing(pricing_id, updates)
    except PricingOverlapError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if item is None:raise HTTPException(status_code=404,detail="Pricing not found")
    if service.finops_lifecycle is not None:
        await service.finops_lifecycle.reevaluate_pricing_alerts(reason="Pricing configuration changed")
    return item


@router.post("/pricing/{pricing_id}/disable")
async def pricing_disable(pricing_id: str, services: ApiServices = Depends(get_services)):
    item=await _service(services).store.update_pricing(pricing_id,{"enabled":False})
    if item is None:raise HTTPException(status_code=404,detail="Pricing not found")
    return item


@router.get("/budgets")
async def budgets(enabled: bool | None = None, services: ApiServices = Depends(get_services)):
    return {"items":await _service(services).store.list_budgets(enabled)}


@router.post("/budgets", status_code=201)
async def budget_create(body: BudgetCreate, services: ApiServices = Depends(get_services)):
    return await _service(services).store.create_budget(body.model_dump())


@router.get("/budgets/{budget_id}")
async def budget_detail(budget_id: str, services: ApiServices = Depends(get_services)):
    item=await _service(services).store.get_budget(budget_id)
    if item is None:raise HTTPException(status_code=404,detail="Budget not found")
    return item


@router.patch("/budgets/{budget_id}")
async def budget_patch(budget_id: str, body: BudgetPatch, services: ApiServices = Depends(get_services)):
    service = _service(services)
    item=await service.store.update_budget(budget_id,body.model_dump(exclude_unset=True))
    if item is None:raise HTTPException(status_code=404,detail="Budget not found")
    if service.finops_lifecycle is not None:
        await service.finops_lifecycle.reevaluate_budget_alerts(budget_id,reason="Budget configuration changed")
    return item


@router.delete("/budgets/{budget_id}")
async def budget_disable(budget_id: str, services: ApiServices = Depends(get_services)):
    service = _service(services)
    item=await service.store.update_budget(budget_id,{"enabled":False})
    if item is None:raise HTTPException(status_code=404,detail="Budget not found")
    if service.finops_lifecycle is not None:
        await service.finops_lifecycle.reevaluate_budget_alerts(budget_id,reason="Budget disabled")
    return item


@router.get("/budgets/{budget_id}/usage")
async def budget_usage(budget_id: str, services: ApiServices = Depends(get_services)):
    item=await _service(services).store.budget_usage(budget_id)
    if item is None:raise HTTPException(status_code=404,detail="Budget not found")
    return item


@router.get("/budgets/{budget_id}/events")
async def budget_events(budget_id: str, services: ApiServices = Depends(get_services)):
    return {"items":await _service(services).observability_store.fetch_all("SELECT * FROM llm_budget_events WHERE budget_id=? ORDER BY created_at DESC",(budget_id,))}


@router.get("/budgets/{budget_id}/reservations")
async def budget_reservations(budget_id: str, services: ApiServices = Depends(get_services)):
    service = _service(services)
    if await service.store.get_budget(budget_id) is None:
        raise HTTPException(status_code=404, detail="Budget not found")
    return {"items":await service.observability_store.fetch_all(
        """SELECT reservation_id,budget_id,workflow_id,branch_id,llm_call_id,agent_name,
           estimated_amount,currency,status,created_at,expires_at,released_at,consumed_amount
           FROM llm_budget_reservations WHERE budget_id=? ORDER BY created_at DESC""",
        (budget_id,),
    )}


@router.post("/budgets/check")
async def budget_check(body: BudgetCheckRequest, services: ApiServices = Depends(get_services)):
    service=_service(services)
    return await service.preflight(
        call_id=f"check:{uuid4().hex}",workflow_id=body.workflow_id,
        branch_id=body.branch_id,project_name=body.project_name,
        agent_name=body.agent_name,provider="unknown",model=body.model or "unknown",
        kwargs={},estimated_amount=body.estimated_amount,currency=body.currency,
        raise_on_block=False,
    )


@router.post("/budgets/{budget_id}/reset")
async def budget_reset(budget_id: str, body: BudgetResetRequest, services: ApiServices = Depends(get_services)):
    service=_service(services);item=await service.store.reset_budget(budget_id,reason=body.reason,actor=body.actor)
    if item is None:raise HTTPException(status_code=404,detail="Budget not found")
    await __import__("asyncio").to_thread(service.store.add_budget_event_sync,{"budget_id":budget_id,"event_type":"budget_reset","decision":"within_budget","reason_code":"manual_reset","actor":body.actor,"metadata":{"reason":body.reason}})
    if service.finops_lifecycle is not None:
        await service.finops_lifecycle.reevaluate_budget_alerts(budget_id,reason="Budget reset")
    return item
