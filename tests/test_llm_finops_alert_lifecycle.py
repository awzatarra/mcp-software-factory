from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from api.services.alert_service import AlertEvaluationService
from api.services.alert_store import AlertStore
from api.services.llm_cost_service import LLMCostService
from api.services.llm_cost_store import LLMCostStore
from api.services.llm_finops_alert_lifecycle import LLMFinOpsAlertLifecycleService
from api.services.observability_store import ObservabilityStore
from scripts.reconcile_llm_costs import reconcile
from scripts.reconcile_llm_usage import reconcile_usage


class RecordingDispatcher:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def dispatch_alert_event(self, *, alert, alert_event, **_kwargs):
        self.events.append((alert.alert_id, alert_event))

    async def cancel_for_alert_transition(self, _alert_id, _event):
        return None


@pytest.fixture
async def finops(tmp_path):
    database = tmp_path / "finops.sqlite"
    observability = ObservabilityStore(database)
    await observability.initialize()
    costs = LLMCostStore(database)
    await costs.initialize()
    alert_store = AlertStore(database)
    await alert_store.initialize()
    dispatcher = RecordingDispatcher()
    alerts = AlertEvaluationService(alert_store, notification_dispatcher=dispatcher)
    lifecycle = LLMFinOpsAlertLifecycleService(costs, observability, alerts)
    service = LLMCostService(costs, observability)
    service.set_alert_service(alerts)
    service.set_finops_lifecycle(lifecycle)
    return service, lifecycle, alert_store, observability, dispatcher


async def create_budget(service, *, mode="hard_limit", workflow="workflow-1", warning="80", limit="1"):
    now = datetime.now(UTC)
    return await service.store.create_budget({
        "name": f"{mode}-{workflow}", "scope_type": "workflow", "scope_value": workflow,
        "currency": "USD", "limit_amount": Decimal(limit),
        "warning_percent": Decimal(warning), "enforcement_mode": mode,
        "period_type": "custom", "period_start": now - timedelta(days=1),
        "period_end": now + timedelta(days=1), "enabled": True,
    })


async def open_legacy_budget_alerts(finops):
    service, _lifecycle, alert_store, _observability, _dispatcher = finops
    budget = await create_budget(service, workflow="manual-concurrency-budget-check", limit="0.0015")
    reservation = await asyncio.to_thread(
        service.store.reserve_sync, budget, call_id="held", workflow_id="manual-concurrency-budget-check",
        branch_id="original", agent_name="Developer", amount=Decimal("0.001"),
        currency="USD", ttl_seconds=300,
    )
    await asyncio.to_thread(service.store.add_budget_event_sync, {
        "budget_id": budget["budget_id"], "workflow_id": "manual-concurrency-budget-check",
        "branch_id": "original", "llm_call_id": "blocked", "event_type": "budget_call_blocked",
        "amount": Decimal("0.001"), "decision": "block", "reason_code": "budget_exceeded",
    })
    ids = []
    for code in ("LLM_BUDGET_EXCEEDED", "LLM_CALL_BLOCKED"):
        await service.alerts.record_external_condition(
            rule_code=code, thread_id="manual-concurrency-budget-check", branch_id="original",
            severity="error", title=code, message="legacy", actual_value="0.001",
            threshold_value="remaining budget", dimension="Developer",
        )
        rule = await alert_store.get_rule(f"builtin:{code.casefold()}")
        fingerprint = alert_store.fingerprint(rule.rule_id, "manual-concurrency-budget-check", "original", "Developer")
        ids.append((await alert_store.get_alert_by_fingerprint(fingerprint)).alert_id)
    return budget, reservation, ids


@pytest.mark.asyncio
async def test_expired_legacy_reservation_resolves_historical_finops_alerts(finops):
    service, _lifecycle, alert_store, observability, _dispatcher = finops
    budget, reservation, alert_ids = await open_legacy_budget_alerts(finops)
    await observability.execute(
        "UPDATE llm_budget_reservations SET expires_at=? WHERE reservation_id=?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), reservation["reservation_id"]),
    )

    dry = await reconcile(service.store.database_path)
    assert set(dry["alerts_to_resolve"]) == set(alert_ids)
    before = [await alert_store.get_alert(alert_id) for alert_id in alert_ids]
    assert all(alert.status == "open" for alert in before)

    applied = await reconcile(service.store.database_path, dry_run=False)
    assert applied["expired_reservations"] == 1
    assert applied["alerts_resolved"] == 2
    for alert_id in alert_ids:
        alert = await alert_store.get_alert(alert_id)
        assert alert.status == "resolved"
        assert alert.resolved_at is not None
        assert alert.resolved_by == "finops-auto-resolve"
    usage = await service.store.budget_usage(budget["budget_id"])
    assert usage["reserved"] == "0" and usage["remaining"] == "0.0015"
    second = await reconcile(service.store.database_path, dry_run=False)
    assert second["alerts_resolved"] == 0 and second["alerts_to_resolve"] == []


@pytest.mark.asyncio
async def test_warning_release_resolves_and_later_detection_reopens(finops):
    service, lifecycle, alert_store, _observability, _dispatcher = finops
    budget = await create_budget(service, mode="warn", warning="50")
    await service.preflight(
        call_id="warning-1", workflow_id="workflow-1", branch_id="original",
        agent_name="Developer", provider="openai", model="gpt-5-mini", kwargs={},
        estimated_amount=Decimal("0.6"), raise_on_block=False,
    )
    alert = (await service.alerts.list_alerts(rule_code="LLM_BUDGET_WARNING", limit=10, offset=0)).items[0]
    assert alert.status == "open" and alert.current_evidence.budget_id == budget["budget_id"]
    occurrence_count = alert.occurrence_count

    assert await service.release_reservations("warning-1", reason="test release") == 1
    resolved = await alert_store.get_alert(alert.alert_id)
    assert resolved.status == "resolved"

    await service.preflight(
        call_id="warning-2", workflow_id="workflow-1", branch_id="original",
        agent_name="Developer", provider="openai", model="gpt-5-mini", kwargs={},
        estimated_amount=Decimal("0.6"), raise_on_block=False,
    )
    reopened = await alert_store.get_alert(alert.alert_id)
    assert reopened.status == "open" and reopened.occurrence_count == occurrence_count + 1
    assert (await lifecycle.reevaluate_budget_alerts(budget["budget_id"], reason="still active"))["alerts_resolved"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["disable", "reset", "period_rollover"])
async def test_budget_mutations_resolve_block_alerts(finops, mutation):
    service, lifecycle, alert_store, observability, _dispatcher = finops
    budget, _reservation, alert_ids = await open_legacy_budget_alerts(finops)
    if mutation == "disable":
        await service.store.update_budget(budget["budget_id"], {"enabled": False})
    elif mutation == "reset":
        await service.store.reset_budget(budget["budget_id"], reason="test", actor="tester")
    else:
        now = datetime.now(UTC)
        await observability.execute(
            "UPDATE llm_budgets SET period_start=?,period_end=? WHERE budget_id=?",
            ((now + timedelta(days=1)).isoformat(), (now + timedelta(days=2)).isoformat(), budget["budget_id"]),
        )
    result = await lifecycle.reevaluate_budget_alerts(budget["budget_id"], reason=mutation)
    assert result["alerts_resolved"] == 2
    after = [await alert_store.get_alert(alert_id) for alert_id in alert_ids]
    assert all(alert.status == "resolved" for alert in after)


@pytest.mark.asyncio
async def test_auto_resolve_is_concurrent_idempotent_and_notified_once(finops):
    service, lifecycle, alert_store, _observability, dispatcher = finops
    budget = await create_budget(service, mode="warn", warning="50")
    await service.preflight(
        call_id="concurrent", workflow_id="workflow-1", branch_id="original",
        agent_name="Developer", provider="openai", model="gpt-5-mini", kwargs={},
        estimated_amount=Decimal("0.6"), raise_on_block=False,
    )
    alert = (await service.alerts.list_alerts(rule_code="LLM_BUDGET_WARNING", limit=10, offset=0)).items[0]
    await asyncio.to_thread(service.store.release_reservations_sync, "concurrent", "test")
    results = await asyncio.gather(*(
        lifecycle.reevaluate_budget_alerts(budget["budget_id"], reason="concurrent")
        for _ in range(8)
    ))
    assert sum(result["alerts_resolved"] for result in results) == 1
    resolved = await alert_store.get_alert(alert.alert_id)
    resolved_at = resolved.resolved_at
    await lifecycle.reevaluate_budget_alerts(budget["budget_id"], reason="second")
    assert (await alert_store.get_alert(alert.alert_id)).resolved_at == resolved_at
    _occurrences, actions = await alert_store.history(alert.alert_id)
    assert sum(action.action == "auto_resolve" for action in actions) == 1
    assert dispatcher.events.count((alert.alert_id, "alert_auto_resolved")) == 1


@pytest.mark.asyncio
async def test_budget_fingerprint_workflow_branch_and_budget_isolation(finops):
    service, lifecycle, alert_store, _observability, _dispatcher = finops
    first = await create_budget(service, mode="warn", workflow="workflow-1", warning="50")
    second = await create_budget(service, mode="warn", workflow="workflow-2", warning="50")
    for call_id, workflow in (("first", "workflow-1"), ("second", "workflow-2")):
        await service.preflight(
            call_id=call_id, workflow_id=workflow, branch_id="fork" if workflow == "workflow-2" else "original",
            agent_name="Developer", provider="openai", model="gpt-5-mini", kwargs={},
            estimated_amount=Decimal("0.6"), raise_on_block=False,
        )
    await asyncio.to_thread(service.store.release_reservations_sync, "first", "test")
    result = await lifecycle.reevaluate_budget_alerts(first["budget_id"], reason="first only")
    assert result["alerts_resolved"] == 1
    alerts = (await service.alerts.list_alerts(rule_code="LLM_BUDGET_WARNING", limit=10, offset=0)).items
    statuses = {(item.thread_id, item.branch_id, item.current_evidence.budget_id): item.status for item in alerts}
    assert statuses[("workflow-1", "original", first["budget_id"])] == "resolved"
    assert statuses[("workflow-2", "fork", second["budget_id"])] == "open"


@pytest.mark.asyncio
async def test_pricing_and_usage_conditions_resolve_only_after_durable_repair(finops):
    service, lifecycle, alert_store, observability, _dispatcher = finops
    await observability.add_llm_call({
        "call_id": "pricing-call", "trace_id": "trace", "span_id": "span",
        "workflow_id": "workflow-1", "branch_id": "original", "agent": "Developer",
        "provider": "openai", "model": "gpt-test", "operation": "parse", "status": "completed",
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
        "cached_tokens": 0, "reasoning_tokens": 0, "usage_source": "provider_reported",
        "usage_available": True,
    })
    await service.calculate_call("pricing-call")
    pricing_alert = (await service.alerts.list_alerts(rule_code="LLM_PRICING_MISSING", limit=10, offset=0)).items[0]
    await service.store.create_pricing({
        "provider": "openai", "model_pattern": "gpt-test", "currency": "USD",
        "input_price_per_million": Decimal("1"), "output_price_per_million": Decimal("2"),
        "effective_from": datetime(2020, 1, 1, tzinfo=UTC), "source_type": "test", "enabled": True,
    })
    assert await lifecycle.reevaluate_pricing_alerts(reason="pricing added") == 1
    assert (await alert_store.get_alert(pricing_alert.alert_id)).status == "resolved"

    await service.alerts.record_external_condition(
        rule_code="LLM_USAGE_UNAVAILABLE", thread_id="workflow-1", branch_id="original",
        severity="warning", title="usage", message="missing", actual_value="unavailable",
        threshold_value="provider_reported", dimension="usage-call",
        evidence_context={"llm_call_id": "usage-call", "agent_name": "Developer"},
    )
    usage_alert = (await service.alerts.list_alerts(rule_code="LLM_USAGE_UNAVAILABLE", limit=10, offset=0)).items[0]
    assert await lifecycle.reevaluate_usage_alerts(reason="not repaired") == 0
    await observability.add_llm_call({
        "call_id": "usage-call", "workflow_id": "workflow-1", "branch_id": "original",
        "trace_id": "trace-usage", "span_id": "span-usage",
        "agent": "Developer", "provider": "openai", "model": "gpt-test", "operation": "parse",
        "status": "completed", "usage_source": "provider_reported", "usage_available": True,
    })
    assert await lifecycle.reevaluate_usage_alerts(reason="usage repaired") == 1
    assert (await alert_store.get_alert(usage_alert.alert_id)).status == "resolved"


@pytest.mark.asyncio
async def test_consuming_reservation_with_lower_real_cost_resolves_warning(finops):
    service, _lifecycle, alert_store, observability, _dispatcher = finops
    await create_budget(service, mode="warn", warning="50")
    await service.store.create_pricing({
        "provider": "openai", "model_pattern": "gpt-test", "currency": "USD",
        "input_price_per_million": Decimal("1"), "output_price_per_million": Decimal("1"),
        "effective_from": datetime(2020, 1, 1, tzinfo=UTC), "source_type": "test", "enabled": True,
    })
    await service.preflight(
        call_id="consumed-call", workflow_id="workflow-1", branch_id="original",
        agent_name="Developer", provider="openai", model="gpt-test", kwargs={},
        estimated_amount=Decimal("0.6"), raise_on_block=False,
    )
    alert = (await service.alerts.list_alerts(rule_code="LLM_BUDGET_WARNING", limit=10, offset=0)).items[0]
    await observability.add_llm_call({
        "call_id": "consumed-call", "trace_id": "trace", "span_id": "span",
        "workflow_id": "workflow-1", "branch_id": "original", "agent": "Developer",
        "provider": "openai", "model": "gpt-test", "operation": "parse", "status": "completed",
        "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
        "cached_tokens": 0, "reasoning_tokens": 0, "usage_source": "provider_reported",
        "usage_available": True,
    })
    await service.calculate_call("consumed-call")
    assert (await alert_store.get_alert(alert.alert_id)).status == "resolved"


@pytest.mark.asyncio
async def test_usage_reconciliation_triggers_finops_auto_resolve(finops):
    service, _lifecycle, alert_store, observability, _dispatcher = finops
    await observability.create_trace({"trace_id": "trace", "workflow_id": "workflow-1", "name": "workflow", "status": "completed"})
    await observability.start_span({
        "span_id": "usage-span", "trace_id": "trace", "name": "openai.responses.parse",
        "operation": "openai.responses.parse", "category": "llm", "kind": "client",
        "status": "completed", "attributes": {"provider": "openai", "model": "gpt-test"},
    })
    await observability.add_llm_call({
        "call_id": "legacy-usage", "trace_id": "trace", "span_id": "usage-span",
        "workflow_id": "workflow-1", "branch_id": "original", "agent": "Developer",
        "provider": "openai", "model": "gpt-test", "operation": "parse", "status": "completed",
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
        "cached_tokens": 0, "reasoning_tokens": 0, "usage_source": "unavailable",
        "usage_available": False,
    })
    await service.alerts.record_external_condition(
        rule_code="LLM_USAGE_UNAVAILABLE", thread_id="workflow-1", branch_id="original",
        severity="warning", title="usage", message="missing", actual_value="unavailable",
        threshold_value="provider_reported", dimension="legacy-usage",
        evidence_context={"llm_call_id": "legacy-usage", "agent_name": "Developer"},
    )
    alert = (await service.alerts.list_alerts(rule_code="LLM_USAGE_UNAVAILABLE", limit=10, offset=0)).items[0]
    result = reconcile_usage(service.store.database_path, dry_run=False)
    assert result["repaired"] == 1 and result["alerts_resolved"] == 1
    assert (await alert_store.get_alert(alert.alert_id)).status == "resolved"
