from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
import json
import os
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.alert_models import AlertEvidence, WorkflowAlert
from api.dependencies import get_services
from api.notification_models import (
    EscalationPolicyUpdate, NotificationChannel, NotificationChannelUpdate,
    NotificationMessage, NotificationPolicyUpdate, NotificationSendResult,
    QuietHoursUpdate,
)
from api.routes.notifications import router
from api.services.alert_store import AlertStore
from api.services.notification_adapters import (
    GenericWebhookAdapter, SecretResolutionError, SecretResolver,
    TargetValidator, sanitize_delivery_error,
)
from api.services.notification_service import (
    NotificationDeliveryWorker, NotificationDispatcher, NotificationService,
    quiet_hours_active,
)
from api.services.notification_store import NotificationStore


def alert(**updates):
    now=datetime.now(UTC)
    values=dict(alert_id="alert-1",rule_id="builtin:tests_failed",rule_code="TESTS_FAILED",thread_id="thread-1",branch_id="original",project_name="checkout-api",category="testing",severity="error",status="open",title="Tests failed",message="Tests did not pass",fingerprint="alert-fingerprint",first_detected_at=now-timedelta(minutes=5),last_detected_at=now,occurrence_count=1,current_evidence=AlertEvidence(metric="tests_failed",actual_value=False,threshold_value=True,related_file="tests/test_health.py"),created_at=now-timedelta(minutes=5),updated_at=now)
    values.update(updates);return WorkflowAlert(**values)


def message(delivery_id="delivery-1"):
    now=datetime.now(UTC)
    return NotificationMessage(notification_id=delivery_id,alert_event="alert_opened",alert_id="alert-1",rule_code="TESTS_FAILED",severity="error",status="open",title="Tests failed",message="Tests did not pass",thread_id="thread-1",branch_id="original",occurrence_count=1,first_detected_at=now,last_detected_at=now,evidence={})


@pytest.fixture
async def notification_system(tmp_path):
    path=tmp_path/"notifications.db";alerts=AlertStore(path);await alerts.initialize()
    store=NotificationStore(path);await store.initialize()
    dispatcher=NotificationDispatcher(store);worker=NotificationDeliveryWorker(store,interval_seconds=60,lease_seconds=5)
    return store,dispatcher,worker,NotificationService(store,dispatcher,worker),alerts


async def create_channel(store,**updates):
    values=dict(name="Operations",channel_type="internal",enabled=True,configuration={},timeout_seconds=5,max_attempts=3,initial_backoff_seconds=1,max_backoff_seconds=8,rate_limit_per_minute=None,verify_tls=True)
    values.update(updates);return await store.create_channel(values)


async def queue(store,channel,*,fingerprint="delivery-fingerprint",delivery_id="delivery-1",policy_id="builtin:internal"):
    return (await store.create_delivery(delivery_id=delivery_id,alert_id="alert-1",policy_id=policy_id,channel_id=channel.channel_id,alert_event="alert_opened",fingerprint=fingerprint,max_attempts=channel.max_attempts,payload=message(delivery_id).model_dump(mode="json")))[0]


async def test_notification_migrations_defaults_and_indexes_are_idempotent(notification_system):
    store,*_=notification_system;await store.initialize()
    channels=await store.list_channels();policies=await store.list_policies()
    assert {item.channel_id for item in channels}=={"builtin:internal","builtin:log"}
    assert [item.policy_id for item in policies]==["builtin:internal"]
    with sqlite3.connect(store.database_path) as db:
        tables={x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes={x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"notification_channels","notification_policies","quiet_hours_schedules","escalation_policies","notification_deliveries","notification_delivery_attempts","notification_channel_state"}<=tables
    assert {"idx_notification_delivery_status","idx_notification_attempt_delivery","idx_notification_channel_enabled"}<=indexes


async def test_channel_crud_secret_reference_and_sanitization(notification_system,monkeypatch):
    store,*_=notification_system;monkeypatch.setenv("OPS_URL","https://example.com/hook?token=secret")
    channel=await create_channel(store,channel_type="webhook",configuration={"url":"https://example.com/hook?token=value","headers":{"X-Environment":"local","Authorization":"secret"}},secret_reference="env:OPS_URL")
    assert channel.secret_reference=="env:OPS_URL" and channel.secret_configured
    assert "token=value" not in str(channel.configuration) and "Authorization" not in str(channel.configuration)
    updated=await store.update_channel(channel.channel_id,{"enabled":False,"name":"Paused"});assert not updated.enabled and updated.health=="disabled"
    assert await store.delete_channel(channel.channel_id)=="deleted"


def test_secret_resolver_missing_and_invalid(monkeypatch):
    resolver=SecretResolver();monkeypatch.setenv("PRESENT_SECRET","value")
    assert resolver.resolve("env:PRESENT_SECRET")=="value"
    with pytest.raises(SecretResolutionError):resolver.resolve("env:MISSING_SECRET")
    with pytest.raises(SecretResolutionError):resolver.resolve("vault:fake")


async def test_policy_reference_dispatch_dedup_and_branch_scope(notification_system):
    store,dispatcher,*_=notification_system
    first=await dispatcher.dispatch_alert_event(alert=alert(),alert_event="alert_opened",occurrence_id="occurrence-1")
    second=await dispatcher.dispatch_alert_event(alert=alert(),alert_event="alert_opened",occurrence_id="occurrence-1")
    fork=await dispatcher.dispatch_alert_event(alert=alert(branch_id="fork-1"),alert_event="alert_opened",occurrence_id="occurrence-fork")
    assert first.created==1 and second.deduplicated==1 and fork.created==1
    items=await store.list_deliveries(limit=10,offset=0);assert items.total==2
    assert items.items[0].fingerprint!=items.items[1].fingerprint


async def test_original_branch_policy_does_not_notify_forks(notification_system):
    store,dispatcher,*_=notification_system
    await store.update_policy("builtin:internal",{"enabled":False})
    await store.create_policy(dict(
        name="Original only",channel_ids=["builtin:internal"],rule_codes=["TESTS_FAILED"],
        severities=["error"],alert_events=["alert_opened"],statuses=["open"],
        branch_scope="original",send_resolved=False,send_acknowledged=False,
    ))
    original=await dispatcher.dispatch_alert_event(alert=alert(),alert_event="alert_opened",occurrence_id="original-occurrence")
    fork=await dispatcher.dispatch_alert_event(alert=alert(branch_id="fork-1"),alert_event="alert_opened",occurrence_id="fork-occurrence")
    assert original.created==1 and fork.created==0


async def test_delivery_survives_store_restart(notification_system):
    store,*_=notification_system
    channel=await create_channel(store)
    await queue(store,channel,delivery_id="restart-delivery",fingerprint="restart-fingerprint")
    reopened=NotificationStore(store.database_path)
    await reopened.initialize()
    delivery=await reopened.get_delivery("restart-delivery")
    assert delivery is not None and delivery.status=="pending" and delivery.fingerprint=="restart-fingerprint"


async def test_internal_worker_inbox_read_does_not_acknowledge_alert(notification_system):
    store,dispatcher,worker,service,alerts=notification_system
    await dispatcher.dispatch_alert_event(alert=alert(),alert_event="alert_opened",occurrence_id="occurrence-1")
    batch=await worker.run_once();assert batch.delivered==1
    inbox=await service.inbox();assert inbox.unread==1 and inbox.items[0].status=="delivered"
    await store.mark_read(inbox.items[0].delivery_id);assert (await service.inbox()).unread==0


async def test_custom_internal_channel_is_included_in_inbox(notification_system):
    store,_,worker,service,_=notification_system
    channel=await create_channel(store,name="Secondary inbox")
    await queue(store,channel,delivery_id="custom-internal",fingerprint="custom-internal")
    await worker.run_once()
    inbox=await service.inbox()
    assert any(item.delivery_id=="custom-internal" for item in inbox.items)


async def test_policy_cooldown_suppresses_new_identity(notification_system):
    store,dispatcher,*_=notification_system
    await store.update_policy("builtin:internal",{"cooldown_seconds":300})
    first=await dispatcher.dispatch_alert_event(alert=alert(),alert_event="alert_opened",occurrence_id="first")
    second=await dispatcher.dispatch_alert_event(alert=alert(),alert_event="alert_opened",occurrence_id="second")
    assert first.created==1 and second.created==0 and second.deduplicated==1


def test_update_models_enforce_creation_contracts():
    with pytest.raises(ValueError):NotificationChannelUpdate(secret_reference="inline:secret")
    with pytest.raises(ValueError):QuietHoursUpdate(timezone="Mars/Olympus")
    with pytest.raises(ValueError):QuietHoursUpdate(days_of_week=["monday","monday"])
    with pytest.raises(ValueError):NotificationPolicyUpdate(project_name_pattern="[unsafe]")
    with pytest.raises(ValueError):EscalationPolicyUpdate(steps=[
        {"step":2,"delay_seconds":0,"channel_ids":["one"]},
        {"step":1,"delay_seconds":0,"channel_ids":["one"]},
    ])


@pytest.mark.parametrize(("status","success","retryable","code"),[(200,True,False,None),(400,False,False,"http_4xx"),(429,False,True,"rate_limited"),(500,False,True,"http_5xx")])
async def test_generic_webhook_classification(status,success,retryable,code):
    async def handler(request):
        headers={"Retry-After":"5"} if status==429 else {}
        assert request.headers["x-idempotency-key"]=="key" and "authorization" not in request.headers
        return httpx.Response(status,text="safe response",headers=headers)
    adapter=GenericWebhookAdapter(target_validator=TargetValidator(allow_private=True),transport=httpx.MockTransport(handler))
    channel=NotificationChannel(channel_id="c",name="Webhook",channel_type="webhook",enabled=True,configuration={"url":"http://127.0.0.1:8011/webhook"},timeout_seconds=5,max_attempts=3,initial_backoff_seconds=1,max_backoff_seconds=8,verify_tls=True,created_at=datetime.now(UTC))
    result=await adapter.send(channel=channel,message=message(),idempotency_key="key")
    assert (result.success,result.retryable,result.error_code)==(success,retryable,code)
    if status==429:assert result.retry_after_seconds==5


async def test_ssrf_blocks_local_private_metadata_and_invalid_schemes():
    validator=TargetValidator(allow_private=False)
    for target in ("http://127.0.0.1/x","http://169.254.169.254/latest","file:///etc/passwd"):
        with pytest.raises(ValueError):await validator.validate(target)


class SequenceAdapter:
    def __init__(self,*results):self.results=list(results);self.calls=0
    async def send(self,**_):
        self.calls+=1;return self.results[min(self.calls-1,len(self.results)-1)]


async def test_retry_backoff_dead_letter_manual_retry_and_audit(notification_system):
    store,_,_,_,_=notification_system;channel=await create_channel(store,max_attempts=2)
    await queue(store,channel)
    adapter=SequenceAdapter(NotificationSendResult(success=False,retryable=True,error_code="http_5xx",error_message="failed"))
    worker=NotificationDeliveryWorker(store,adapters={"internal":adapter},lease_seconds=5)
    first=await worker.run_once();delivery=await store.get_delivery("delivery-1")
    assert first.retried==1 and delivery.status=="retry_scheduled" and delivery.next_attempt_at>delivery.last_attempt_at
    with sqlite3.connect(store.database_path) as db:db.execute("UPDATE notification_deliveries SET next_attempt_at=? WHERE delivery_id='delivery-1'",((datetime.now(UTC)-timedelta(seconds=1)).isoformat(),))
    second=await worker.run_once();delivery=await store.get_delivery("delivery-1")
    assert second.dead_lettered==1 and delivery.status=="dead_letter" and delivery.attempt_count==2
    assert len(await store.attempts("delivery-1"))==2
    retried,state=await store.transition_delivery("delivery-1","retry");assert state=="ok" and retried.attempt_count==2


async def test_permanent_failure_goes_directly_to_dead_letter(notification_system):
    store,*_=notification_system;channel=await create_channel(store);await queue(store,channel)
    adapter=SequenceAdapter(NotificationSendResult(success=False,retryable=False,error_code="http_4xx",error_message="bad request"))
    batch=await NotificationDeliveryWorker(store,adapters={"internal":adapter}).run_once()
    assert batch.dead_lettered==1 and (await store.get_delivery("delivery-1")).attempt_count==1


async def test_rate_limit_defers_without_attempt(notification_system):
    store,*_=notification_system;channel=await create_channel(store,rate_limit_per_minute=1)
    adapter=SequenceAdapter(NotificationSendResult(success=True,retryable=False))
    await queue(store,channel,delivery_id="delivery-1",fingerprint="f1");worker=NotificationDeliveryWorker(store,adapters={"internal":adapter});await worker.run_once()
    await queue(store,channel,delivery_id="delivery-2",fingerprint="f2");batch=await worker.run_once();second=await store.get_delivery("delivery-2")
    assert batch.deferred==1 and second.attempt_count==0 and second.last_error_code=="rate_limited"


async def test_processing_lease_prevents_duplicate_and_expiry_recovers(notification_system):
    store,*_=notification_system;channel=await create_channel(store);await queue(store,channel)
    first=await store.claim("worker-1",10,60);second=await store.claim("worker-2",10,60)
    assert len(first)==1 and second==[]
    with sqlite3.connect(store.database_path) as db:db.execute("UPDATE notification_deliveries SET processing_expires_at=?",((datetime.now(UTC)-timedelta(seconds=1)).isoformat(),))
    recovered=await store.claim("worker-2",10,60);assert len(recovered)==1 and recovered[0].processing_owner=="worker-2"


async def test_quiet_hours_cross_midnight_suppresses_warning_but_not_critical(notification_system):
    store,*_=notification_system
    schedule=await store.create_quiet_hours(dict(name="Night",enabled=True,timezone="UTC",days_of_week=[datetime.now(UTC).strftime("%A").casefold()],start_time=(datetime.now(UTC)-timedelta(minutes=1)).time(),end_time=(datetime.now(UTC)+timedelta(minutes=1)).time(),suppress_severities=["warning","critical"],allow_critical=True))
    assert quiet_hours_active(schedule,"warning") and not quiet_hours_active(schedule,"critical")
    cross=schedule.model_copy(update={"start_time":time(22),"end_time":time(6),"days_of_week":[(datetime.now(UTC)-timedelta(days=1)).strftime("%A").casefold()]})
    at_one=datetime.now(UTC).replace(hour=1,minute=0,second=0,microsecond=0);assert quiet_hours_active(cross,"warning",at_one)


async def test_escalation_schedules_steps_and_acknowledge_cancels(notification_system):
    store,dispatcher,*_=notification_system
    escalation=await store.create_escalation(dict(name="Ops escalation",steps=[{"step":1,"delay_seconds":0,"channel_ids":["builtin:internal"],"require_unacknowledged":True,"repeat":False},{"step":2,"delay_seconds":60,"channel_ids":["builtin:internal"],"require_unacknowledged":True,"repeat":False}],stop_on_acknowledge=True,stop_on_resolve=True))
    await store.create_policy(dict(name="Escalated",channel_ids=["builtin:internal"],rule_codes=["TESTS_FAILED"],severities=["error"],alert_events=["alert_opened"],statuses=["open"],branch_scope="all",escalation_policy_id=escalation.escalation_policy_id,send_resolved=True,send_acknowledged=False))
    result=await dispatcher.dispatch_alert_event(alert=alert(),alert_event="alert_opened",occurrence_id="occ-esc")
    assert result.created==4  # default delivery + policy delivery + two escalation steps
    cancelled=await dispatcher.cancel_for_alert_transition("alert-1","alert_acknowledged");assert cancelled==2


async def test_circuit_breaker_opens_and_does_not_consume_http_attempt(notification_system):
    store,*_=notification_system;channel=await create_channel(store,max_attempts=1)
    adapter=SequenceAdapter(NotificationSendResult(success=False,retryable=True,error_code="connection_error",error_message="failed"))
    worker=NotificationDeliveryWorker(store,adapters={"internal":adapter},circuit_threshold=2,circuit_cooldown=60)
    for index in range(2):await queue(store,channel,delivery_id=f"d{index}",fingerprint=f"f{index}");await worker.run_once()
    await queue(store,channel,delivery_id="d-open",fingerprint="f-open");batch=await worker.run_once();delivery=await store.get_delivery("d-open")
    assert batch.deferred==1 and delivery.attempt_count==0 and delivery.last_error_code=="circuit_open"


async def test_summary_and_delivery_transitions(notification_system):
    store,*_=notification_system;channel=await create_channel(store);delivery=await queue(store,channel)
    cancelled,state=await store.transition_delivery(delivery.delivery_id,"cancel");assert state=="ok" and cancelled.status=="cancelled"
    retry,state=await store.transition_delivery(delivery.delivery_id,"retry");assert state=="ok" and retry.status=="retry_scheduled"
    summary=await store.summary();assert summary.retry_scheduled==1 and summary.channels_enabled>=3


async def test_notification_api_contract_validation_and_no_secrets(notification_system):
    store,dispatcher,worker,service,_=notification_system
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_services]=lambda:SimpleNamespace(notifications=service)
    with TestClient(app) as client:
        channels=client.get("/api/notifications/channels");assert channels.status_code==200 and len(channels.json())==2
        created=client.post("/api/notifications/channels",json={"name":"Webhook","channel_type":"webhook","configuration":{"url":"https://example.com/hook?token=secret"},"secret_reference":"env:MISSING"});assert created.status_code==201 and "token=secret" not in created.text
        channel_id=created.json()["channel_id"]
        invalid=client.post("/api/notifications/policies",json={"name":"Bad","channel_ids":["missing"]});assert invalid.status_code==422
        policy=client.post("/api/notifications/policies",json={"name":"Ops","channel_ids":[channel_id],"rule_codes":["TESTS_FAILED"]});assert policy.status_code==201
        assert client.delete(f"/api/notifications/channels/{channel_id}").status_code==409
        test=client.post("/api/notifications/channels/builtin:internal/test");assert test.status_code==202 and test.json()["status"]=="pending"
        delivery_id=test.json()["delivery_id"]
        assert client.get(f"/api/notifications/deliveries/{delivery_id}").status_code==200
        assert client.get("/api/notifications/deliveries",params={"status":"invalid"}).status_code==422
        assert client.get("/api/notifications/deliveries/missing").status_code==404
        email=client.post("/api/notifications/channels",json={"name":"Email","channel_type":"email","enabled":True})
        assert email.status_code==201 and email.json()["enabled"] is False


def test_sanitize_delivery_error_removes_secrets_urls_and_absolute_paths():
    value=sanitize_delivery_error("token=super-secret https://example.com/hook?q=secret C:\\Users\\name\\file.txt")
    assert "super-secret" not in value and "example.com" not in value and "C:\\Users" not in value


async def test_escalation_policy_exact_regression_round_trip_and_restart(notification_system):
    store,dispatcher,worker,service,_=notification_system
    webhook=await create_channel(store,channel_type="webhook",configuration={"url":"https://example.com/webhook"})
    payload={
        "name":"Manual acknowledge cancellation test",
        "description":"Step 1 internal; step 2 webhook after 60 seconds",
        "enabled":True,
        "steps":[
            {"step":1,"delay_seconds":0,"channel_ids":["builtin:internal"],"severities":["warning","error","critical"],"require_unacknowledged":False,"repeat":False,"repeat_interval_seconds":None,"max_repeats":None},
            {"step":2,"delay_seconds":60,"channel_ids":[webhook.channel_id],"severities":["warning","error","critical"],"require_unacknowledged":True,"repeat":False,"repeat_interval_seconds":None,"max_repeats":None},
        ],
        "stop_on_acknowledge":True,"stop_on_resolve":True,
    }
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_services]=lambda:SimpleNamespace(notifications=service)
    with TestClient(app,raise_server_exceptions=False) as client:
        response=client.post("/api/notifications/escalation-policies",json=payload)
        assert response.status_code==201,response.text
        body=response.json();policy_id=body["escalation_policy_id"]
        assert body["name"]==payload["name"] and body["created_at"] and body["updated_at"] is None
        assert len(body["steps"])==2
        assert body["steps"][0]["channel_ids"]==["builtin:internal"]
        assert body["steps"][1]["channel_ids"]==[webhook.channel_id]
        assert body["steps"][0]["repeat_interval_seconds"] is None and body["steps"][0]["max_repeats"] is None
        assert body["stop_on_acknowledge"] is True and body["stop_on_resolve"] is True
        assert any(item["escalation_policy_id"]==policy_id for item in client.get("/api/notifications/escalation-policies").json())
        assert client.get(f"/api/notifications/escalation-policies/{policy_id}").json()==body
        updated=client.patch(f"/api/notifications/escalation-policies/{policy_id}",json={"name":"Updated escalation","steps":payload["steps"]})
        assert updated.status_code==200 and updated.json()["name"]=="Updated escalation"
    with sqlite3.connect(store.database_path) as database:
        raw=json.loads(database.execute("SELECT steps_json FROM escalation_policies WHERE escalation_policy_id=?",(policy_id,)).fetchone()[0])
    assert raw==payload["steps"]
    reopened=NotificationStore(store.database_path);await reopened.initialize()
    persisted=await reopened.get_escalation(policy_id)
    assert persisted is not None and len(persisted.steps)==2 and persisted.steps[1].channel_ids==[webhook.channel_id]


@pytest.mark.parametrize("payload",[
    {"name":"Empty","steps":[]},
    {"name":"Duplicate","steps":[{"step":1,"delay_seconds":0,"channel_ids":["builtin:internal"]},{"step":1,"delay_seconds":1,"channel_ids":["builtin:internal"]}]},
    {"name":"Negative","steps":[{"step":1,"delay_seconds":-1,"channel_ids":["builtin:internal"]}]},
    {"name":"No channels","steps":[{"step":1,"delay_seconds":0,"channel_ids":[]}]},
    {"name":"Bad repeat","steps":[{"step":1,"delay_seconds":0,"channel_ids":["builtin:internal"],"repeat":True}]},
    {"name":"Short repeat","steps":[{"step":1,"delay_seconds":0,"channel_ids":["builtin:internal"],"repeat":True,"repeat_interval_seconds":5,"max_repeats":1}]},
    {"name":"Bad severity","steps":[{"step":1,"delay_seconds":0,"channel_ids":["builtin:internal"],"severities":["fatal"]}]},
])
async def test_invalid_escalation_steps_return_422_without_partial_rows(notification_system,payload):
    store,dispatcher,worker,service,_=notification_system
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_services]=lambda:SimpleNamespace(notifications=service)
    before=len(await store.list_escalations())
    with TestClient(app,raise_server_exceptions=False) as client:
        response=client.post("/api/notifications/escalation-policies",json=payload)
    assert response.status_code==422
    assert "traceback" not in response.text.casefold() and "c:\\" not in response.text.casefold() and "secret" not in response.text.casefold()
    assert len(await store.list_escalations())==before


async def test_missing_escalation_channel_returns_422_and_delete_reference_conflicts(notification_system):
    store,dispatcher,worker,service,_=notification_system
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_services]=lambda:SimpleNamespace(notifications=service)
    with TestClient(app,raise_server_exceptions=False) as client:
        missing=client.post("/api/notifications/escalation-policies",json={"name":"Missing","steps":[{"step":1,"delay_seconds":0,"channel_ids":["missing-channel"]}]})
        assert missing.status_code==422 and "Notification channel not found" in missing.text and "traceback" not in missing.text.casefold()
        created=client.post("/api/notifications/escalation-policies",json={"name":"Referenced","steps":[{"step":1,"delay_seconds":0,"channel_ids":["builtin:log"]}]})
        assert created.status_code==201
        escalation_id=created.json()["escalation_policy_id"]
        policy=client.post("/api/notifications/policies",json={"name":"Uses escalation","channel_ids":["builtin:internal"],"escalation_policy_id":escalation_id})
        assert policy.status_code==201
        assert client.delete(f"/api/notifications/escalation-policies/{escalation_id}").status_code==409
        assert client.delete(f"/api/notifications/policies/{policy.json()['policy_id']}").status_code==204
        assert client.delete(f"/api/notifications/escalation-policies/{escalation_id}").status_code==204
