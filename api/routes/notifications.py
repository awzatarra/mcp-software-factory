from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from api.dependencies import ApiServices, get_services
from api.notification_models import (
    DeliveryDetailResponse, DeliveryListResponse, EscalationPolicy,
    EscalationPolicyCreate, EscalationPolicyUpdate, InboxResponse,
    NotificationChannel, NotificationChannelCreate, NotificationChannelUpdate,
    NotificationDelivery, NotificationPolicy, NotificationPolicyCreate,
    NotificationPolicyUpdate, NotificationSummaryResponse, QuietHoursCreate,
    QuietHoursSchedule, QuietHoursUpdate, TestChannelResponse,
)
from api.services.notification_service import (
    NotificationConflictError, NotificationNotFoundError,
    NotificationReferenceError, NotificationService,
)


router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _service(services: ApiServices = Depends(get_services)) -> NotificationService:
    if services.notifications is None: raise HTTPException(503, "Notification service unavailable")
    return services.notifications


def _not_found(exc): raise HTTPException(404, "Notification resource not found") from exc
def _reference(exc): raise HTTPException(422, str(exc)) from exc


@router.get("/channels", response_model=list[NotificationChannel])
async def list_channels(service=Depends(_service)): return await service.store.list_channels()

@router.get("/channels/{channel_id}", response_model=NotificationChannel)
async def get_channel(channel_id:str,service=Depends(_service)):
    item=await service.store.get_channel(channel_id)
    if item is None:raise HTTPException(404,"Notification channel not found")
    return item

@router.post("/channels",response_model=NotificationChannel,status_code=201)
async def create_channel(body:NotificationChannelCreate,service=Depends(_service)):
    values = body.model_dump()
    if body.channel_type == "email":
        values["enabled"] = False
    return await service.store.create_channel(values)

@router.patch("/channels/{channel_id}",response_model=NotificationChannel)
async def update_channel(channel_id:str,body:NotificationChannelUpdate,service=Depends(_service)):
    values=body.model_dump(exclude_unset=True)
    current=await service.store.get_channel(channel_id)
    if current is None:raise HTTPException(404,"Notification channel not found")
    if current.channel_type=="email":values["enabled"]=False
    initial=values.get("initial_backoff_seconds",current.initial_backoff_seconds)
    maximum=values.get("max_backoff_seconds",current.max_backoff_seconds)
    if maximum<initial:raise HTTPException(422,"max_backoff_seconds must be >= initial_backoff_seconds")
    item=await service.store.update_channel(channel_id,values)
    if item is None:raise HTTPException(404,"Notification channel not found")
    return item

@router.delete("/channels/{channel_id}",status_code=204)
async def delete_channel(channel_id:str,service=Depends(_service)):
    result=await service.store.delete_channel(channel_id)
    if result=="missing":raise HTTPException(404,"Notification channel not found")
    if result=="referenced":raise HTTPException(409,"Channel is referenced by a notification policy")
    return Response(status_code=204)

@router.post("/channels/{channel_id}/test",response_model=TestChannelResponse,status_code=202)
async def test_channel(channel_id:str,service=Depends(_service)):
    try:return await service.test_channel(channel_id)
    except NotificationNotFoundError as exc:_not_found(exc)


@router.get("/policies",response_model=list[NotificationPolicy])
async def list_policies(enabled:bool|None=None,channel_id:str|None=None,rule_code:str|None=None,service=Depends(_service)):
    items=await service.store.list_policies()
    if enabled is not None:items=[x for x in items if x.enabled==enabled]
    if channel_id:items=[x for x in items if channel_id in x.channel_ids]
    if rule_code:items=[x for x in items if not x.rule_codes or rule_code in x.rule_codes]
    return items

@router.get("/policies/{policy_id}",response_model=NotificationPolicy)
async def get_policy(policy_id:str,service=Depends(_service)):
    item=await service.store.get_policy(policy_id)
    if item is None:raise HTTPException(404,"Notification policy not found")
    return item

@router.post("/policies",response_model=NotificationPolicy,status_code=201)
async def create_policy(body:NotificationPolicyCreate,service=Depends(_service)):
    values=body.model_dump()
    try:await service.validate_references(values)
    except NotificationReferenceError as exc:_reference(exc)
    return await service.store.create_policy(values)

@router.patch("/policies/{policy_id}",response_model=NotificationPolicy)
async def update_policy(policy_id:str,body:NotificationPolicyUpdate,service=Depends(_service)):
    values=body.model_dump(exclude_unset=True)
    try:await service.validate_references(values)
    except NotificationReferenceError as exc:_reference(exc)
    item=await service.store.update_policy(policy_id,values)
    if item is None:raise HTTPException(404,"Notification policy not found")
    return item

@router.delete("/policies/{policy_id}",status_code=204)
async def delete_policy(policy_id:str,service=Depends(_service)):
    if not await service.store.delete_policy(policy_id):raise HTTPException(404,"Notification policy not found")
    return Response(status_code=204)


@router.get("/quiet-hours",response_model=list[QuietHoursSchedule])
async def list_quiet_hours(service=Depends(_service)):return await service.store.list_quiet_hours()
@router.post("/quiet-hours",response_model=QuietHoursSchedule,status_code=201)
async def create_quiet_hours(body:QuietHoursCreate,service=Depends(_service)):return await service.store.create_quiet_hours(body.model_dump())
@router.patch("/quiet-hours/{quiet_id}",response_model=QuietHoursSchedule)
async def update_quiet_hours(quiet_id:str,body:QuietHoursUpdate,service=Depends(_service)):
    item=await service.store.update_quiet_hours(quiet_id,body.model_dump(exclude_unset=True))
    if item is None:raise HTTPException(404,"Quiet hours schedule not found")
    return item
@router.delete("/quiet-hours/{quiet_id}",status_code=204)
async def delete_quiet_hours(quiet_id:str,service=Depends(_service)):
    if not await service.store.delete_quiet_hours(quiet_id):raise HTTPException(404,"Quiet hours schedule not found")
    return Response(status_code=204)


@router.get("/escalation-policies",response_model=list[EscalationPolicy])
async def list_escalations(service=Depends(_service)):return await service.store.list_escalations()
@router.get("/escalation-policies/{escalation_id}",response_model=EscalationPolicy)
async def get_escalation(escalation_id:str,service=Depends(_service)):
    item=await service.store.get_escalation(escalation_id)
    if item is None:raise HTTPException(404,"Escalation policy not found")
    return item
@router.post("/escalation-policies",response_model=EscalationPolicy,status_code=201)
async def create_escalation(body:EscalationPolicyCreate,service=Depends(_service)):
    values=body.model_dump()
    try:
        await service.validate_steps(values)
        return await service.create_escalation(values)
    except NotificationReferenceError as exc:_reference(exc)
@router.patch("/escalation-policies/{escalation_id}",response_model=EscalationPolicy)
async def update_escalation(escalation_id:str,body:EscalationPolicyUpdate,service=Depends(_service)):
    values=body.model_dump(exclude_unset=True)
    try:
        await service.validate_steps(values)
        item=await service.update_escalation(escalation_id,values)
    except NotificationReferenceError as exc:_reference(exc)
    if item is None:raise HTTPException(404,"Escalation policy not found")
    return item
@router.delete("/escalation-policies/{escalation_id}",status_code=204)
async def delete_escalation(escalation_id:str,service=Depends(_service)):
    result=await service.store.delete_escalation(escalation_id)
    if result=="missing":raise HTTPException(404,"Escalation policy not found")
    if result=="referenced":raise HTTPException(409,"Escalation policy is referenced by a notification policy")
    return Response(status_code=204)


DeliveryStatus=Literal["pending","processing","delivered","failed","retry_scheduled","dead_letter","suppressed","cancelled"]
@router.get("/deliveries",response_model=DeliveryListResponse)
async def list_deliveries(
    status_filter:Annotated[DeliveryStatus|None,Query(alias="status")]=None,channel_id:str|None=None,policy_id:str|None=None,
    alert_id:str|None=None,alert_event:str|None=None,date_from:datetime|None=None,date_to:datetime|None=None,
    search:Annotated[str|None,Query(max_length=100)]=None,sort_by:Literal["created_at","scheduled_at","last_attempt_at","attempt_count","status"]="created_at",
    sort_order:Literal["asc","desc"]="desc",limit:Annotated[int,Query(ge=1,le=500)]=50,offset:Annotated[int,Query(ge=0)]=0,service=Depends(_service),
):
    if date_from and date_to and date_from>date_to:raise HTTPException(422,"date_from must not be after date_to")
    return await service.store.list_deliveries(status=status_filter,channel_id=channel_id,policy_id=policy_id,alert_id=alert_id,alert_event=alert_event,date_from=date_from.isoformat() if date_from else None,date_to=date_to.isoformat() if date_to else None,search=search,sort_by=sort_by,sort_order=sort_order,limit=limit,offset=offset)

@router.get("/deliveries/{delivery_id}",response_model=DeliveryDetailResponse)
async def get_delivery(delivery_id:str,service=Depends(_service)):
    try:return await service.detail(delivery_id)
    except NotificationNotFoundError as exc:_not_found(exc)

async def _delivery_action(delivery_id,action,service):
    try:return await service.transition(delivery_id,action)
    except NotificationNotFoundError as exc:_not_found(exc)
    except NotificationConflictError as exc:raise HTTPException(409,str(exc)) from exc
@router.post("/deliveries/{delivery_id}/retry",response_model=NotificationDelivery)
async def retry_delivery(delivery_id:str,service=Depends(_service)):return await _delivery_action(delivery_id,"retry",service)
@router.post("/deliveries/{delivery_id}/cancel",response_model=NotificationDelivery)
async def cancel_delivery(delivery_id:str,service=Depends(_service)):return await _delivery_action(delivery_id,"cancel",service)
@router.post("/deliveries/{delivery_id}/redeliver",response_model=NotificationDelivery,status_code=201)
async def redeliver(delivery_id:str,service=Depends(_service)):
    try:return await service.redeliver(delivery_id)
    except NotificationNotFoundError as exc:_not_found(exc)

@router.get("/summary",response_model=NotificationSummaryResponse)
async def summary(service=Depends(_service)):return await service.store.summary()
@router.get("/inbox",response_model=InboxResponse)
async def inbox(limit:Annotated[int,Query(ge=1,le=200)]=50,offset:Annotated[int,Query(ge=0)]=0,service=Depends(_service)):return await service.inbox(limit,offset)
@router.post("/inbox/{delivery_id}/read",response_model=NotificationDelivery)
async def mark_read(delivery_id:str,service=Depends(_service)):
    item=await service.store.mark_read(delivery_id)
    if item is None:raise HTTPException(404,"Internal notification not found")
    return item
@router.post("/inbox/read-all")
async def mark_all_read(service=Depends(_service)):return {"updated":await service.store.mark_all_read()}
