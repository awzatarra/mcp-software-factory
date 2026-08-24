from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
import logging
import os
import random
from time import monotonic
from typing import Any
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from api.alert_models import WorkflowAlert
from api.notification_models import (
    DeliveryBatchResult, DeliveryDetailResponse, InboxResponse,
    NotificationChannelCreate, NotificationDelivery, NotificationDispatchResult,
    NotificationMessage, NotificationPolicyCreate, TestChannelResponse,
)
from api.services.notification_adapters import (
    default_adapters, sanitize_delivery_error, sanitize_notification_data,
)
from api.services.notification_store import NotificationStore, NotificationStoreReferenceError


logger = logging.getLogger(__name__)


class NotificationNotFoundError(LookupError): pass
class NotificationConflictError(RuntimeError): pass
class NotificationReferenceError(ValueError): pass


def validate_channel_reference(channel_id: str, existing_channel_ids: set[str]) -> None:
    if channel_id not in existing_channel_ids:
        raise NotificationReferenceError(f"Notification channel not found: {channel_id}")


def quiet_hours_active(schedule, severity: str, now: datetime | None = None) -> bool:
    if schedule is None or not schedule.enabled or severity not in schedule.suppress_severities:
        return False
    if severity == "critical" and schedule.allow_critical:
        return False
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(schedule.timezone))
    day = local.strftime("%A").casefold(); current = local.time().replace(tzinfo=None)
    if schedule.start_time <= schedule.end_time:
        return day in schedule.days_of_week and schedule.start_time <= current < schedule.end_time
    previous = (local - timedelta(days=1)).strftime("%A").casefold()
    return (day in schedule.days_of_week and current >= schedule.start_time) or (previous in schedule.days_of_week and current < schedule.end_time)


def _application_url() -> str | None:
    value = os.getenv("APPLICATION_URL", "").strip().rstrip("/")
    return value or None


def build_notification_message(alert: WorkflowAlert, alert_event: str, notification_id: str) -> NotificationMessage:
    app = _application_url(); root = f"{app}/workflows/{quote(alert.thread_id)}" if app else None
    branch = "" if alert.branch_id == "original" else f"&branch_id={quote(alert.branch_id)}"
    evidence = sanitize_notification_data(alert.current_evidence.model_dump(mode="json"))
    return NotificationMessage(
        notification_id=notification_id, alert_event=alert_event, alert_id=alert.alert_id,
        rule_code=alert.rule_code, severity=alert.severity, status=alert.status,
        title=sanitize_delivery_error(alert.title, 300), message=sanitize_delivery_error(alert.message, 2000),
        project_name=sanitize_delivery_error(alert.project_name, 200) or None,
        thread_id=alert.thread_id, branch_id=alert.branch_id,
        occurrence_count=alert.occurrence_count, first_detected_at=alert.first_detected_at,
        last_detected_at=alert.last_detected_at, application_url=app, workflow_url=root,
        evaluation_url=f"{root}?tab=evaluation{branch}" if root and alert.category in {"testing", "repair", "evaluation"} else None,
        timeline_url=f"{root}?tab=timeline&event={quote(alert.primary_event_id)}{branch}" if root and alert.primary_event_id else None,
        execution_url=f"{root}?tab=execution&task={quote(alert.related_task_id)}{branch}" if root and alert.related_task_id else None,
        project_file_url=f"{root}?tab=project&file={quote(alert.related_file)}{branch}" if root and alert.related_file else None,
        evidence=evidence,
    )


class NotificationDispatcher:
    def __init__(self, store: NotificationStore) -> None:
        self.store = store

    @staticmethod
    def _matches(policy, alert: WorkflowAlert, alert_event: str) -> bool:
        if not policy.enabled or alert_event not in policy.alert_events:
            return False
        if alert_event in {"alert_resolved", "alert_auto_resolved"} and not policy.send_resolved:
            return False
        if alert_event == "alert_acknowledged" and not policy.send_acknowledged:
            return False
        if policy.rule_codes and alert.rule_code not in policy.rule_codes: return False
        if policy.severities and alert.severity not in policy.severities: return False
        if policy.statuses and alert.status not in policy.statuses: return False
        if policy.branch_scope == "original" and alert.branch_id != "original": return False
        if policy.project_name_pattern and not fnmatchcase((alert.project_name or "").casefold(), policy.project_name_pattern.casefold()): return False
        return True

    async def dispatch_alert_event(self, *, alert: WorkflowAlert, alert_event: str, occurrence_id: str | None = None, action_id: str | None = None) -> NotificationDispatchResult:
        result = NotificationDispatchResult(); identity = occurrence_id or action_id
        policies = await self.store.list_policies()
        channels = {item.channel_id: item for item in await self.store.list_channels()}
        escalations = {item.escalation_policy_id: item for item in await self.store.list_escalations()}
        for policy in policies:
            if not self._matches(policy, alert, alert_event): continue
            for channel_id in policy.channel_ids:
                channel = channels.get(channel_id)
                if channel is None or not channel.enabled: continue
                if await self.store.delivery_in_cooldown(alert.alert_id, policy.policy_id, channel_id, policy.cooldown_seconds):
                    result.deduplicated += 1
                    continue
                delivery_id = str(uuid4())
                fingerprint = self.store.delivery_fingerprint(alert.alert_id, alert_event, policy.policy_id, channel_id, identity, None)
                message = build_notification_message(alert, alert_event, delivery_id)
                delivery, created = await self.store.create_delivery(
                    delivery_id=delivery_id, alert_id=alert.alert_id, occurrence_id=occurrence_id,
                    action_id=action_id, policy_id=policy.policy_id, channel_id=channel_id,
                    alert_event=alert_event, fingerprint=fingerprint, max_attempts=channel.max_attempts,
                    payload=message.model_dump(mode="json"),
                )
                if created: result.created += 1; result.delivery_ids.append(delivery.delivery_id)
                else: result.deduplicated += 1
            if policy.escalation_policy_id and alert_event in {"alert_opened", "alert_reopened", "alert_severity_escalated"}:
                escalation = escalations.get(policy.escalation_policy_id)
                if escalation and escalation.enabled:
                    for step in escalation.steps:
                        if step.severities and alert.severity not in step.severities: continue
                        repeats = step.max_repeats if step.repeat and step.max_repeats else 0
                        for repeat_index in range(repeats + 1):
                            delay = step.delay_seconds + repeat_index * (step.repeat_interval_seconds or 0)
                            for channel_id in step.channel_ids:
                                channel = channels.get(channel_id)
                                if channel is None or not channel.enabled: continue
                                token = f"{identity or alert.alert_id}:repeat:{repeat_index}"
                                fingerprint = self.store.delivery_fingerprint(alert.alert_id, "escalation_triggered", policy.policy_id, channel_id, token, step.step)
                                delivery_id = str(uuid4()); message = build_notification_message(alert, "escalation_triggered", delivery_id)
                                delivery, created = await self.store.create_delivery(
                                    delivery_id=delivery_id, alert_id=alert.alert_id, occurrence_id=occurrence_id,
                                    action_id=action_id, policy_id=policy.policy_id, channel_id=channel_id,
                                    alert_event="escalation_triggered", escalation_step=step.step,
                                    fingerprint=fingerprint, max_attempts=channel.max_attempts,
                                    scheduled_at=datetime.now(UTC) + timedelta(seconds=delay), payload={**message.model_dump(mode="json"), "require_unacknowledged": step.require_unacknowledged},
                                )
                                if created: result.created += 1; result.delivery_ids.append(delivery.delivery_id)
                                else: result.deduplicated += 1
        return result

    async def cancel_for_alert_transition(self, alert_id: str, event: str) -> int:
        selected = []
        escalations = {item.escalation_policy_id: item for item in await self.store.list_escalations()}
        for policy in await self.store.list_policies():
            if not policy.escalation_policy_id: continue
            escalation = escalations.get(policy.escalation_policy_id)
            if escalation and ((event == "alert_acknowledged" and escalation.stop_on_acknowledge) or (event in {"alert_resolved", "alert_auto_resolved"} and escalation.stop_on_resolve)):
                selected.append(policy.policy_id)
        return await self.store.cancel_escalations(alert_id, selected)


class NotificationDeliveryWorker:
    def __init__(self, store: NotificationStore, *, adapters=None, interval_seconds: float = 5, batch_size: int = 50, lease_seconds: int = 60, circuit_threshold: int = 5, circuit_cooldown: int = 60) -> None:
        self.store=store;self.adapters=adapters or default_adapters();self.interval_seconds=interval_seconds;self.batch_size=batch_size;self.lease_seconds=lease_seconds
        self.circuit_threshold=circuit_threshold;self.circuit_cooldown=circuit_cooldown;self.owner_id=str(uuid4());self._task=None;self._closed=False

    async def start(self):
        if self._task is None and not self._closed:self._task=asyncio.create_task(self._run())
    async def close(self):
        self._closed=True
        if self._task:
            self._task.cancel()
            try:await self._task
            except asyncio.CancelledError:pass
            self._task=None
    async def _run(self):
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:await self.run_once(self.batch_size)
            except Exception:logger.exception("notification worker batch failed")

    async def run_once(self,batch_size:int|None=None)->DeliveryBatchResult:
        claimed=await self.store.claim(self.owner_id,batch_size or self.batch_size,self.lease_seconds);batch=DeliveryBatchResult(claimed=len(claimed))
        for delivery in claimed:
            outcome=await self._process(delivery)
            setattr(batch,outcome,getattr(batch,outcome)+1)
        return batch

    async def _process(self,delivery:NotificationDelivery)->str:
        raw=await self.store.get_channel(delivery.channel_id,raw=True)
        if raw is None:
            await self.store.defer(delivery.delivery_id,self.owner_id,datetime.now(UTC)+timedelta(seconds=60),"channel_missing");return "deferred"
        channel,configuration=raw;channel=channel.model_copy(update={"configuration":configuration})
        if not channel.enabled:
            await self.store.defer(delivery.delivery_id,self.owner_id,datetime.now(UTC)+timedelta(seconds=60),"channel_disabled");return "deferred"
        payload=await self.store.delivery_payload(delivery.delivery_id)
        message=NotificationMessage.model_validate({key:value for key,value in payload.items() if key!="require_unacknowledged"})
        if delivery.alert_event=="escalation_triggered" and payload.get("require_unacknowledged"):
            alert_status = await self.store.alert_status(delivery.alert_id)
            if alert_status in {"acknowledged", "resolved", "muted"}:
                await self.store.defer(delivery.delivery_id,self.owner_id,datetime.now(UTC)+timedelta(days=3650),"escalation_stopped")
                await self.store.transition_delivery(delivery.delivery_id,"cancel")
                return "deferred"
        policy=await self.store.get_policy(delivery.policy_id)
        quiet=await self.store.get_quiet_hours(policy.quiet_hours_id) if policy and policy.quiet_hours_id else None
        if quiet_hours_active(quiet,message.severity):
            await self.store.suppress(delivery.delivery_id,self.owner_id,"Suppressed by quiet hours");return "suppressed"
        limited=await self.store.rate_limited_until(channel.channel_id,channel.rate_limit_per_minute)
        if limited:
            await self.store.defer(delivery.delivery_id,self.owner_id,limited,"rate_limited");return "deferred"
        circuit=await self.store.circuit_permission(channel.channel_id,self.circuit_threshold,self.circuit_cooldown)
        if circuit=="open":
            await self.store.defer(delivery.delivery_id,self.owner_id,datetime.now(UTC)+timedelta(seconds=self.circuit_cooldown),"circuit_open");return "deferred"
        adapter=self.adapters.get(channel.channel_type)
        if adapter is None:
            await self.store.defer(delivery.delivery_id,self.owner_id,datetime.now(UTC)+timedelta(seconds=60),"channel_disabled");return "deferred"
        started=datetime.now(UTC);clock=monotonic();result=await adapter.send(channel=channel,message=message,idempotency_key=delivery.fingerprint)
        result=result.model_copy(update={"error_message":sanitize_delivery_error(result.error_message),"response_summary":sanitize_delivery_error(result.response_summary)})
        updated=await self.store.complete_attempt(delivery.delivery_id,self.owner_id,result,started,round((monotonic()-clock)*1000))
        await self.store.record_circuit(channel.channel_id,result.success,self.circuit_threshold)
        if updated and updated.status=="delivered":return "delivered"
        if updated and updated.status=="dead_letter":return "dead_lettered"
        return "retried"


class NotificationService:
    def __init__(self,store:NotificationStore,dispatcher:NotificationDispatcher,worker:NotificationDeliveryWorker):self.store=store;self.dispatcher=dispatcher;self.worker=worker
    async def validate_references(self,values):
        for channel_id in values.get("channel_ids") or []:
            if await self.store.get_channel(channel_id) is None:raise NotificationReferenceError(f"Unknown channel: {channel_id}")
        quiet=values.get("quiet_hours_id")
        if quiet and await self.store.get_quiet_hours(quiet) is None:raise NotificationReferenceError("Unknown quiet hours schedule")
        escalation=values.get("escalation_policy_id")
        if escalation and await self.store.get_escalation(escalation) is None:raise NotificationReferenceError("Unknown escalation policy")
    async def validate_steps(self,values):
        existing_channel_ids={item.channel_id for item in await self.store.list_channels()}
        for step in values.get("steps") or []:
            channel_ids=step.channel_ids if hasattr(step,"channel_ids") else step.get("channel_ids",[])
            for channel_id in channel_ids:validate_channel_reference(str(channel_id),existing_channel_ids)
    async def create_escalation(self,values):
        try:return await self.store.create_escalation(values)
        except NotificationStoreReferenceError as exc:
            raise NotificationReferenceError(f"Notification channel not found: {exc.channel_id}") from exc
    async def update_escalation(self,entity_id,values):
        try:return await self.store.update_escalation(entity_id,values)
        except NotificationStoreReferenceError as exc:
            raise NotificationReferenceError(f"Notification channel not found: {exc.channel_id}") from exc
    async def test_channel(self,channel_id):
        channel=await self.store.get_channel(channel_id)
        if channel is None:raise NotificationNotFoundError(channel_id)
        delivery_id=str(uuid4());now=datetime.now(UTC)
        message=NotificationMessage(notification_id=delivery_id,alert_event="channel_test",alert_id=f"channel-test:{channel_id}",rule_code="CHANNEL_TEST",severity="info",status="open",title="Notification channel test",message="Software Factory notification channel test.",thread_id="channel-test",branch_id="original",occurrence_count=1,first_detected_at=now,last_detected_at=now,evidence={})
        fingerprint=self.store.delivery_fingerprint(message.alert_id,"channel_test","builtin:channel-test",channel_id,delivery_id,None)
        delivery,_=await self.store.create_delivery(delivery_id=delivery_id,alert_id=message.alert_id,policy_id="builtin:channel-test",channel_id=channel_id,alert_event="channel_test",fingerprint=fingerprint,max_attempts=channel.max_attempts,payload=message.model_dump(mode="json"))
        return TestChannelResponse(delivery_id=delivery.delivery_id,status=delivery.status)
    async def detail(self,delivery_id):
        delivery=await self.store.get_delivery(delivery_id)
        if delivery is None:raise NotificationNotFoundError(delivery_id)
        return DeliveryDetailResponse(delivery=delivery,attempts=await self.store.attempts(delivery_id),channel=await self.store.get_channel(delivery.channel_id),policy=await self.store.get_policy(delivery.policy_id))
    async def transition(self,delivery_id,action):
        delivery,status=await self.store.transition_delivery(delivery_id,action)
        if status=="missing":raise NotificationNotFoundError(delivery_id)
        if status=="conflict":raise NotificationConflictError(f"Cannot {action} delivery in status {delivery.status}")
        return delivery
    async def redeliver(self,delivery_id):
        original=await self.store.get_delivery(delivery_id)
        if original is None:raise NotificationNotFoundError(delivery_id)
        payload=await self.store.delivery_payload(delivery_id);channel=await self.store.get_channel(original.channel_id)
        token=str(uuid4());new_id=str(uuid4());payload={**payload,"notification_id":new_id}
        fingerprint=self.store.delivery_fingerprint(original.alert_id,original.alert_event,original.policy_id,original.channel_id,original.occurrence_id or original.action_id,original.escalation_step,token)
        delivery,_=await self.store.create_delivery(delivery_id=new_id,alert_id=original.alert_id,occurrence_id=original.occurrence_id,action_id=original.action_id,policy_id=original.policy_id,channel_id=original.channel_id,alert_event=original.alert_event,escalation_step=original.escalation_step,original_delivery_id=original.delivery_id,fingerprint=fingerprint,max_attempts=channel.max_attempts if channel else original.max_attempts,payload=payload)
        return delivery
    async def inbox(self,limit=50,offset=0):
        listed=await self.store.inbox_deliveries(limit=limit,offset=offset)
        summary=await self.store.summary();return InboxResponse(items=listed.items,unread=summary.unread_internal,total=listed.total)


def worker_from_env(store):
    return NotificationDeliveryWorker(store,interval_seconds=max(float(os.getenv("NOTIFICATION_WORKER_INTERVAL_SECONDS","5")),.5),batch_size=max(int(os.getenv("NOTIFICATION_WORKER_BATCH_SIZE","50")),1),lease_seconds=max(int(os.getenv("NOTIFICATION_PROCESSING_LEASE_SECONDS","60")),5))
