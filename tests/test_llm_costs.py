from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.llm_cost_models import PricingCreate, PricingPatch, UsageSource
from api.services.llm_cost_service import (
    BudgetExceededError, DECISION_PRECEDENCE, ENFORCEMENT_PRECEDENCE, LLMCostService,
)
from api.services.llm_cost_store import LLMCostStore, PricingOverlapError
from api.services.llm_observability import ObservableOpenAIClient
from api.services.llm_usage_normalizer import normalize_llm_usage
from api.services.alert_service import AlertEvaluationService
from api.services.alert_store import AlertStore
from api.services.llm_finops_alert_lifecycle import LLMFinOpsAlertLifecycleService
from api.services.observability_context import observability_context
from api.services.observability_service import ObservabilityService
from api.services.observability_store import ObservabilityStore
from scripts.reconcile_llm_costs import reconcile as reconcile_costs
from scripts.reconcile_llm_usage import reconcile_usage


@pytest.fixture
async def costs(tmp_path):
    database = tmp_path / "costs.sqlite"
    observability_store = ObservabilityStore(database)
    await observability_store.initialize()
    store = LLMCostStore(database)
    service = LLMCostService(store, observability_store)
    await service.initialize()
    return service


def pricing(**updates):
    values = {
        "provider": "test", "model_pattern": "test-model", "currency": "USD",
        "input_price_per_million": Decimal("1.00"),
        "cached_input_price_per_million": Decimal("0.25"),
        "output_price_per_million": Decimal("4.00"),
        "reasoning_price_per_million": Decimal("2.00"),
        "effective_from": datetime(2020, 1, 1, tzinfo=UTC),
        "source_type": "test_fixture", "enabled": True, "priority": 0,
        "reasoning_in_completion": True, "metadata": {"tests_only": True},
    }
    values.update(updates)
    return values


async def add_call(service, call_id="call-1", **updates):
    values = {
        "call_id": call_id, "trace_id": "trace-1", "span_id": "span-1",
        "workflow_id": "workflow-1", "branch_id": "original", "agent": "Planner",
        "provider": "test", "model": "test-model", "operation": "parse", "status": "completed",
        "input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500,
        "cached_tokens": 200, "reasoning_tokens": 100,
        "usage_source": "provider_reported", "usage_available": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    values.update(updates)
    await service.observability_store.add_llm_call(values)


async def add_budget(service, mode, *, name=None, limit="1", currency="USD", enabled=True,
                     period_start=None, period_end=None):
    return await service.store.create_budget({
        "name": name or mode, "scope_type": "global", "scope_value": None,
        "currency": currency, "limit_amount": Decimal(limit), "warning_percent": Decimal("50"),
        "enforcement_mode": mode, "enabled": enabled,
        "period_type": "custom" if period_start or period_end else None,
        "period_start": period_start, "period_end": period_end,
    })


def test_usage_normalizer_preserves_nulls_and_provider_usage():
    missing = normalize_llm_usage(None)
    assert missing.prompt_tokens is None and missing.usage_available is False
    usage = normalize_llm_usage({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cached_tokens": 2})
    assert usage.usage_source.value == "imported"
    assert usage.uncached_input_tokens == 8 and usage.usage_available is True


def test_openai_response_usage_is_provider_reported():
    response = SimpleNamespace(
        id="resp-1",
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=7, total_tokens=17,
            input_tokens_details=SimpleNamespace(cached_tokens=3, audio_tokens=0),
            output_tokens_details=SimpleNamespace(reasoning_tokens=2, audio_tokens=0),
        ),
    )
    usage = normalize_llm_usage(response, provider="openai", total_includes_input_output=True)
    assert usage.usage_source.value == "provider_reported"
    assert usage.usage_available is True
    assert usage.prompt_tokens == 10 and usage.completion_tokens == 7
    assert usage.cached_input_tokens == 3 and usage.reasoning_tokens == 2
    assert usage.provider_request_id == "resp-1"
    assert usage.raw_usage_version == "openai-responses-v1"


def test_explicit_unavailable_source_is_not_promoted_by_numbers():
    usage = normalize_llm_usage(
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        usage_source=UsageSource.UNAVAILABLE,
        provider="openai", total_includes_input_output=True,
    )
    assert usage.usage_source.value == "unavailable"
    assert usage.usage_available is False


@pytest.mark.parametrize("raw,error", [
    ({"input_tokens": -1}, "tokens_must_be_non_negative"),
    ({"input_tokens": 2, "cached_tokens": 3}, "cached_tokens_exceed_prompt_tokens"),
    ({"input_tokens": 2, "output_tokens": 3, "total_tokens": 4}, "total_tokens_inconsistent"),
])
def test_usage_validation_is_non_throwing(raw, error):
    usage = normalize_llm_usage(raw, total_includes_input_output=True)
    assert usage.usage_invalid is True and usage.validation_error == error


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "parse"])
async def test_openai_wrappers_persist_real_usage_without_sensitive_input(costs, operation):
    class Responses:
        async def create(self, **kwargs): return self.response()
        async def parse(self, **kwargs): return self.response()
        @staticmethod
        def response():
            return SimpleNamespace(
                id=f"resp-{operation}",
                usage=SimpleNamespace(
                    input_tokens=12, output_tokens=8, total_tokens=20,
                    input_tokens_details=SimpleNamespace(cached_tokens=4),
                    output_tokens_details=SimpleNamespace(reasoning_tokens=3),
                ),
            )
    observable = ObservabilityService(costs.observability_store)
    context = await observable.ensure_trace(f"wrapper-{operation}")
    client = ObservableOpenAIClient(SimpleNamespace(responses=Responses()), observable, costs)
    with observability_context(context):
        await getattr(client.responses, operation)(
            model="test-model", input="private prompt", api_key="sk-not-stored",
        )
    row = await costs.observability_store.fetch_one(
        "SELECT * FROM observability_llm_calls WHERE workflow_id=?", (f"wrapper-{operation}",)
    )
    assert row["usage_source"] == "provider_reported" and row["usage_available"] == 1
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (12, 8, 20)
    assert row["cached_tokens"] == 4 and row["reasoning_tokens"] == 3
    assert row["provider_request_id"] == f"resp-{operation}"
    assert "private prompt" not in str(row["attributes"]) and "sk-not-stored" not in str(row["attributes"])
    calculation = await costs.store.get_calculation(row["call_id"])
    assert calculation["usage_source"] == "provider_reported"
    assert calculation["cost_source"] == "unavailable"
    assert calculation["warnings"] == ["pricing_not_found"]


@pytest.mark.asyncio
async def test_failed_openai_request_without_usage_stays_unavailable(costs):
    class Responses:
        async def parse(self, **kwargs):
            raise RuntimeError("provider failed")
    observable = ObservabilityService(costs.observability_store)
    context = await observable.ensure_trace("failed-provider-call")
    client = ObservableOpenAIClient(SimpleNamespace(responses=Responses()), observable, costs)
    with observability_context(context), pytest.raises(RuntimeError, match="provider failed"):
        await client.responses.parse(model="test-model", input="private failed prompt")
    row = await costs.observability_store.fetch_one(
        "SELECT * FROM observability_llm_calls WHERE workflow_id='failed-provider-call'"
    )
    assert row["status"] == "failed"
    assert row["usage_source"] == "unavailable" and row["usage_available"] == 0
    assert row["input_tokens"] is None and row["output_tokens"] is None
    assert "private failed prompt" not in str(row["attributes"])


@pytest.mark.asyncio
async def test_pricing_resolves_exact_before_pattern_and_by_date(costs):
    await costs.store.create_pricing(pricing(model_pattern="test-*", priority=100))
    exact = await costs.store.create_pricing(pricing(model_pattern="test-model", priority=0))
    resolved = await costs.store.resolve_pricing("test", "test-model", datetime.now(UTC).isoformat())
    assert resolved["pricing_id"] == exact["pricing_id"] and resolved["match_type"] == "exact"


@pytest.mark.asyncio
async def test_pricing_historical_version_and_overlap(costs):
    first = await costs.store.create_pricing(pricing(effective_to=datetime(2024, 1, 1, tzinfo=UTC)))
    second = await costs.store.create_pricing(pricing(effective_from=datetime(2024, 1, 1, tzinfo=UTC), input_price_per_million=Decimal("2")))
    old = await costs.store.resolve_pricing("test", "test-model", "2023-01-01T00:00:00+00:00")
    new = await costs.store.resolve_pricing("test", "test-model", "2025-01-01T00:00:00+00:00")
    assert old["pricing_id"] == first["pricing_id"] and new["pricing_id"] == second["pricing_id"]
    with pytest.raises(PricingOverlapError):
        await costs.store.create_pricing(pricing(effective_from=datetime(2024, 6, 1, tzinfo=UTC)))


def test_pricing_patch_tracks_explicit_null_separately_from_absent_field():
    absent = PricingPatch.model_validate({})
    explicit = PricingPatch.model_validate({"effective_to": None})
    assert "effective_to" not in absent.model_fields_set
    assert "effective_to" in explicit.model_fields_set


@pytest.mark.asyncio
async def test_pricing_patch_without_effective_to_leaves_range_unchanged(costs):
    closed_at = datetime(2026, 1, 1, tzinfo=UTC)
    item = await costs.store.create_pricing(pricing(effective_to=closed_at))
    updated = await costs.store.update_pricing(item["pricing_id"], {})
    assert updated["effective_to"] == str(closed_at)


@pytest.mark.asyncio
async def test_pricing_patch_with_date_closes_range(costs):
    item = await costs.store.create_pricing(pricing())
    closed_at = datetime(2026, 1, 1, tzinfo=UTC)
    updated = await costs.store.update_pricing(
        item["pricing_id"], {"effective_to": closed_at}
    )
    assert updated["effective_to"] == str(closed_at)


@pytest.mark.asyncio
async def test_pricing_patch_explicit_null_reopens_range(costs):
    item = await costs.store.create_pricing(
        pricing(effective_to=datetime(2026, 1, 1, tzinfo=UTC))
    )
    updated = await costs.store.update_pricing(
        item["pricing_id"], {"effective_to": None}
    )
    assert updated["effective_to"] is None


@pytest.mark.asyncio
async def test_pricing_patch_rejects_overlap_with_enabled_version(costs):
    first = await costs.store.create_pricing(
        pricing(effective_to=datetime(2024, 1, 1, tzinfo=UTC))
    )
    await costs.store.create_pricing(
        pricing(effective_from=datetime(2024, 1, 1, tzinfo=UTC))
    )
    with pytest.raises(PricingOverlapError):
        await costs.store.update_pricing(first["pricing_id"], {"effective_to": None})
    unchanged = await costs.store.get_pricing(first["pricing_id"])
    assert unchanged["effective_to"] == str(datetime(2024, 1, 1, tzinfo=UTC))


@pytest.mark.asyncio
async def test_pricing_patch_allows_overlap_with_disabled_version(costs):
    first = await costs.store.create_pricing(
        pricing(effective_to=datetime(2024, 1, 1, tzinfo=UTC))
    )
    await costs.store.create_pricing(
        pricing(effective_from=datetime(2024, 1, 1, tzinfo=UTC), enabled=False)
    )
    updated = await costs.store.update_pricing(first["pricing_id"], {"effective_to": None})
    assert updated["effective_to"] is None


@pytest.mark.asyncio
async def test_pricing_patch_null_is_idempotent(costs):
    item = await costs.store.create_pricing(
        pricing(effective_to=datetime(2026, 1, 1, tzinfo=UTC))
    )
    first = await costs.store.update_pricing(item["pricing_id"], {"effective_to": None})
    second = await costs.store.update_pricing(item["pricing_id"], {"effective_to": None})
    assert first["effective_to"] is None and second["effective_to"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "initial"),
    [
        ("source_reference", "https://pricing.example/reference"),
        ("source_verified_at", datetime(2025, 1, 1, tzinfo=UTC)),
        ("metadata", {"origin": "official"}),
    ],
)
async def test_pricing_patch_clears_nullable_fields(costs, field, initial):
    item = await costs.store.create_pricing(pricing(**{field: initial}))
    updated = await costs.store.update_pricing(item["pricing_id"], {field: None})
    assert updated[field] is None


@pytest.mark.asyncio
async def test_pricing_patch_enabled_null_is_noop(costs):
    item = await costs.store.create_pricing(pricing(enabled=True))
    updated = await costs.store.update_pricing(item["pricing_id"], {"enabled": None})
    assert updated["enabled"] is True


@pytest.mark.asyncio
async def test_pricing_patch_api_persists_null_and_returns_overlap_conflict(costs):
    first = await costs.store.create_pricing(
        pricing(effective_to=datetime(2024, 1, 1, tzinfo=UTC))
    )
    second = await costs.store.create_pricing(
        pricing(effective_from=datetime(2024, 1, 1, tzinfo=UTC))
    )

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(llm_costs=costs)

    with TestClient(create_app(factory)) as client:
        conflict = client.patch(
            f"/api/llm-costs/pricing/{first['pricing_id']}",
            json={"effective_to": None},
        )
        disabled = client.post(
            f"/api/llm-costs/pricing/{second['pricing_id']}/disable"
        )
        reopened = client.patch(
            f"/api/llm-costs/pricing/{first['pricing_id']}",
            json={"effective_to": None},
        )
        invalid_range = client.patch(
            f"/api/llm-costs/pricing/{first['pricing_id']}",
            json={"effective_to": "2019-01-01T00:00:00Z"},
        )

    assert conflict.status_code == 409
    assert disabled.status_code == 200
    assert reopened.status_code == 200
    assert reopened.json()["effective_to"] is None
    assert invalid_range.status_code == 422


@pytest.mark.asyncio
async def test_decimal_cost_breakdown_cache_savings_and_snapshot(costs):
    selected = await costs.store.create_pricing(pricing())
    await add_call(costs)
    result = await costs.calculate_call("call-1")
    assert result["input_cost"] == "0.000800000000"
    assert result["cached_input_cost"] == "0.000050000000"
    assert result["output_cost"] == "0.002000000000"
    assert result["total_cost"] == "0.002850000000"
    assert result["cache_savings"] == "0.000150000000"
    assert result["pricing_snapshot"]["pricing_id"] == selected["pricing_id"]
    await costs.store.update_pricing(selected["pricing_id"], {"enabled": False})
    unchanged = await costs.calculate_call("call-1")
    assert unchanged["total_cost"] == result["total_cost"]


@pytest.mark.asyncio
async def test_reasoning_separate_avoids_double_charge(costs):
    await costs.store.create_pricing(pricing(reasoning_in_completion=False))
    await add_call(costs)
    result = await costs.calculate_call("call-1")
    assert result["output_cost"] == "0.001600000000"
    assert result["reasoning_cost"] == "0.000200000000"
    assert result["total_cost"] == "0.002650000000"


@pytest.mark.asyncio
async def test_optional_audio_and_image_pricing_is_persisted(costs):
    item = await costs.store.create_pricing(pricing(
        audio_input_price_per_million=Decimal("3"),
        audio_output_price_per_million=Decimal("6"),
        image_pricing={"unit": "image"},
    ))
    assert item["audio_input_price_per_million"] == "3"
    assert item["audio_output_price_per_million"] == "6"
    assert item["image_pricing"] == {"unit": "image"}


@pytest.mark.asyncio
async def test_missing_required_component_price_never_understates_real_total(costs):
    await costs.store.create_pricing(pricing(output_price_per_million=None))
    await add_call(costs)
    result = await costs.calculate_call("call-1")
    assert result["cost_source"] == "unavailable"
    assert result["cost_status"] == "invalid_pricing"
    assert result["total_cost"] is None
    assert "output_price_unavailable" in result["warnings"]


@pytest.mark.asyncio
async def test_missing_pricing_opens_one_durable_alert(costs):
    alert_store = AlertStore(costs.store.database_path)
    await alert_store.initialize()
    costs.set_alert_service(AlertEvaluationService(alert_store))
    await add_call(costs)
    await costs.calculate_call("call-1")
    await costs.calculate_call("call-1")
    alerts, total, _ = await alert_store.list_alerts(rule_code="LLM_PRICING_MISSING")
    assert total == 1
    assert alerts[0].thread_id == "workflow-1"
    assert alerts[0].branch_id == "original"
    await costs.store.create_pricing(pricing())
    await costs.calculate_call("call-1", recalculate=True, reason="pricing loaded", actor="test")
    resolved, _, _ = await alert_store.list_alerts(rule_code="LLM_PRICING_MISSING")
    assert resolved[0].status == "resolved"


@pytest.mark.asyncio
async def test_usage_reconciliation_requires_provider_span_and_is_idempotent(costs):
    await costs.observability_store.create_trace({
        "trace_id": "trace-1", "workflow_id": "workflow-1",
        "name": "workflow", "status": "completed",
    })
    await costs.observability_store.start_span({
        "span_id": "span-1", "trace_id": "trace-1", "name": "openai.responses.parse",
        "category": "llm", "kind": "client", "status": "completed",
        "operation": "openai.responses.parse",
        "attributes": {"provider": "openai", "model": "test-model"},
    })
    await add_call(
        costs, usage_source="unavailable", usage_available=False,
        provider="openai", cached_tokens=0, reasoning_tokens=10,
    )
    await add_call(
        costs, call_id="unsafe-call", span_id="missing-span",
        usage_source="unavailable", usage_available=False,
        provider="openai", cached_tokens=0, reasoning_tokens=0,
    )
    calculation = await costs.calculate_call("call-1")
    assert calculation["cost_source"] == "unavailable"
    assert calculation["warnings"] == ["usage_not_available"]

    dry = reconcile_usage(costs.store.database_path)
    assert dry["repairable"] == 1 and dry["not_repairable"] == 1 and dry["repaired"] == 0
    applied = reconcile_usage(costs.store.database_path, dry_run=False)
    assert applied["repaired"] == 1 and applied["not_repairable"] == 1
    second = reconcile_usage(costs.store.database_path, dry_run=False)
    assert second["repaired"] == 0 and second["not_repairable"] == 1

    repaired = await costs.observability_store.fetch_one(
        "SELECT * FROM observability_llm_calls WHERE call_id='call-1'"
    )
    untouched = await costs.observability_store.fetch_one(
        "SELECT * FROM observability_llm_calls WHERE call_id='unsafe-call'"
    )
    snapshot = await costs.store.get_calculation("call-1")
    assert repaired["usage_source"] == "provider_reported" and repaired["usage_available"] == 1
    assert repaired["attributes"]["normalization_repair"]["migration_source"] == "legacy_openai_responses_observability"
    assert untouched["usage_source"] == "unavailable" and untouched["usage_available"] == 0
    assert snapshot["usage_source"] == "provider_reported"
    assert snapshot["cost_source"] == "unavailable"
    assert snapshot["cost_status"] == "unavailable"
    assert snapshot["warnings"] == ["pricing_not_found"]
    assert "prompt" not in str(repaired["attributes"]).casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize(("usage_available", "expected"), [(False, "usage_not_available"), (True, "pricing_not_found")])
async def test_unavailable_cost_reasons(costs, usage_available, expected):
    await add_call(costs, usage_available=usage_available, input_tokens=1 if usage_available else None,
                   output_tokens=1 if usage_available else None, total_tokens=2 if usage_available else None,
                   cached_tokens=0 if usage_available else None,
                   reasoning_tokens=0 if usage_available else None)
    result = await costs.calculate_call("call-1")
    assert result["cost_source"] == "unavailable" and expected in result["warnings"]
    assert result["total_cost"] is None


@pytest.mark.asyncio
async def test_recalculation_is_audited_and_keeps_history(costs):
    await costs.store.create_pricing(pricing())
    await add_call(costs)
    first = await costs.calculate_call("call-1")
    second = await costs.calculate_call("call-1", recalculate=True, reason="verified", actor="tester")
    assert first["cost_calculation_id"] != second["cost_calculation_id"]
    rows = await costs.observability_store.fetch_all("SELECT * FROM llm_cost_recalculations")
    history = await costs.observability_store.fetch_all("SELECT * FROM llm_cost_calculations WHERE llm_call_id='call-1'")
    assert len(rows) == 1 and len(history) == 2 and sum(not row["superseded"] for row in history) == 1


@pytest.mark.asyncio
async def test_budget_reservation_idempotent_and_hard_limit(costs):
    budget = await costs.store.create_budget({
        "name": "Hard", "scope_type": "global", "scope_value": None, "currency": "USD",
        "limit_amount": Decimal("0.000001"), "warning_percent": Decimal("80"),
        "enforcement_mode": "hard_limit", "enabled": True,
    })
    with pytest.raises(BudgetExceededError):
        await costs.preflight(call_id="blocked", workflow_id="workflow-1", branch_id="original",
                              agent_name="Planner", provider="test", model="test-model", kwargs={},
                              estimated_amount=Decimal("1"))
    rows = await costs.observability_store.fetch_all("SELECT * FROM llm_budget_reservations WHERE budget_id=?", (budget["budget_id"],))
    assert len(rows) == 1 and rows[0]["status"] == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(("estimated", "actual", "released", "overage"), [
    ("0.50", "0.41195", "0.08805", "0"),
    ("0.50", "0.50", "0.00", "0"),
    ("0.50", "0.60", "0", "0.10"),
])
async def test_success_finalizes_reservation_with_actual_decimal_cost(
    costs, estimated, actual, released, overage,
):
    budget = await add_budget(costs, "hard_limit", limit="1")
    reservation = await asyncio.to_thread(
        costs.store.reserve_sync, budget, call_id="actual-call", workflow_id="workflow-1",
        branch_id="original", agent_name="Developer", amount=Decimal(estimated),
        currency="USD", ttl_seconds=300,
    )
    result = await asyncio.to_thread(
        costs.store.finalize_reservations_sync, "actual-call", Decimal(actual)
    )
    row = await costs.observability_store.fetch_one(
        "SELECT * FROM llm_budget_reservations WHERE reservation_id=?",
        (reservation["reservation_id"],),
    )
    usage = await costs.store.budget_usage(budget["budget_id"])
    events = await costs.observability_store.fetch_all(
        "SELECT * FROM llm_budget_events WHERE llm_call_id='actual-call'"
    )
    assert result["count"] == 1
    assert row["status"] == "consumed" and Decimal(row["consumed_amount"]) == Decimal(actual)
    assert Decimal(row["metadata"]["finalization"]["released_amount"]) == Decimal(released)
    assert Decimal(row["metadata"]["finalization"]["overage_amount"]) == Decimal(overage)
    assert Decimal(usage["consumed"]) == Decimal(actual)
    assert usage["reserved"] == "0"
    assert Decimal(usage["remaining"]) == max(Decimal("1") - Decimal(actual), Decimal(0))
    assert len(events) == 1 and events[0]["event_type"] == "budget_reservation_consumed"
    assert events[0]["metadata"]["reservation_id"] == reservation["reservation_id"]


@pytest.mark.asyncio
async def test_failed_or_unavailable_call_releases_reservation_without_consumption(costs):
    budget = await add_budget(costs, "hard_limit", limit="1")
    await asyncio.to_thread(
        costs.store.reserve_sync, budget, call_id="failed-call", workflow_id="workflow-1",
        branch_id="original", agent_name="Developer", amount=Decimal("0.4"),
        currency="USD", ttl_seconds=300,
    )
    result = await asyncio.to_thread(costs.store.finalize_reservations_sync, "failed-call", None)
    row = await costs.observability_store.fetch_one(
        "SELECT * FROM llm_budget_reservations WHERE llm_call_id='failed-call'"
    )
    usage = await costs.store.budget_usage(budget["budget_id"])
    assert result["count"] == 1 and row["status"] == "released"
    assert row["consumed_amount"] is None
    assert usage["consumed"] == "0" and usage["reserved"] == "0"


@pytest.mark.asyncio
async def test_failed_provider_wrapper_releases_correlated_reservation(costs):
    await costs.store.create_pricing(pricing(provider="openai"))
    budget = await add_budget(costs, "hard_limit", limit="1")
    class Responses:
        async def create(self, **kwargs):
            raise RuntimeError("provider failed without usage")
    observable = ObservabilityService(costs.observability_store)
    context = (await observable.ensure_trace("failed-budget-call")).child(agent="Developer")
    client = ObservableOpenAIClient(SimpleNamespace(responses=Responses()), observable, costs)
    with observability_context(context), pytest.raises(RuntimeError, match="without usage"):
        await client.responses.create(
            model="test-model", input="safe", max_output_tokens=100,
        )
    rows = await costs.observability_store.fetch_all(
        "SELECT * FROM llm_budget_reservations WHERE workflow_id='failed-budget-call'"
    )
    usage = await costs.store.budget_usage(budget["budget_id"])
    assert len(rows) == 1 and rows[0]["status"] == "released"
    assert rows[0]["consumed_amount"] is None
    assert usage["consumed"] == "0" and usage["reserved"] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize(("output_tokens", "expected_codes"), [
    (1500, {"LLM_BUDGET_WARNING"}),
    (3000, {"LLM_BUDGET_WARNING", "LLM_BUDGET_EXCEEDED"}),
])
async def test_actual_consumption_creates_warning_and_exceeded_alerts(
    costs, output_tokens, expected_codes,
):
    await costs.store.create_pricing(pricing())
    budget = await add_budget(costs, "hard_limit", limit="0.01")
    alert_store = AlertStore(costs.store.database_path)
    await alert_store.initialize()
    alerts = AlertEvaluationService(alert_store)
    lifecycle = LLMFinOpsAlertLifecycleService(costs.store, costs.observability_store, alerts)
    costs.set_alert_service(alerts)
    costs.set_finops_lifecycle(lifecycle)
    await costs.preflight(
        call_id="threshold-call", workflow_id="workflow-1", branch_id="original",
        agent_name="Developer", provider="test", model="test-model", kwargs={},
        estimated_amount=Decimal("0.001"),
    )
    await add_call(
        costs, call_id="threshold-call", input_tokens=0, output_tokens=output_tokens,
        total_tokens=output_tokens, cached_tokens=0, reasoning_tokens=0,
    )
    await costs.calculate_call("threshold-call")
    listed = await alerts.list_alerts(thread_id="workflow-1", limit=20, offset=0)
    codes = {item.rule_code for item in listed.items}
    usage = await costs.store.budget_usage(budget["budget_id"])
    assert expected_codes <= codes
    assert Decimal(usage["consumed"]) == Decimal(output_tokens) * Decimal("4") / Decimal(1_000_000)


@pytest.mark.asyncio
async def test_retry_reservations_consume_once_per_distinct_llm_call(costs):
    budget = await add_budget(costs, "hard_limit", limit="1")
    for call_id in ("attempt-0", "attempt-1"):
        await asyncio.to_thread(
            costs.store.reserve_sync, budget, call_id=call_id, workflow_id="workflow-1",
            branch_id="original", agent_name="Developer", amount=Decimal("0.2"),
            currency="USD", ttl_seconds=300,
        )
        await asyncio.to_thread(
            costs.store.finalize_reservations_sync, call_id, Decimal("0.1")
        )
    await asyncio.to_thread(
        costs.store.finalize_reservations_sync, "attempt-1", Decimal("0.1")
    )
    usage = await costs.store.budget_usage(budget["budget_id"])
    assert usage["consumed"] == "0.2" and usage["reserved"] == "0"


@pytest.mark.asyncio
async def test_duplicate_and_concurrent_finalization_are_idempotent(costs):
    budget = await add_budget(costs, "hard_limit", limit="1")
    await asyncio.to_thread(
        costs.store.reserve_sync, budget, call_id="concurrent-finalize", workflow_id="workflow-1",
        branch_id="original", agent_name="Developer", amount=Decimal("0.5"),
        currency="USD", ttl_seconds=300,
    )
    results = await asyncio.gather(*[
        asyncio.to_thread(
            costs.store.finalize_reservations_sync,
            "concurrent-finalize", Decimal("0.4"),
        ) for _ in range(8)
    ])
    usage = await costs.store.budget_usage(budget["budget_id"])
    events = await costs.observability_store.fetch_all(
        "SELECT * FROM llm_budget_events WHERE llm_call_id='concurrent-finalize'"
    )
    assert sum(result["count"] for result in results) == 1
    assert usage["consumed"] == "0.4" and len(events) == 1


@pytest.mark.asyncio
async def test_existing_durable_calculation_retries_missing_finalization(costs):
    budget = await add_budget(costs, "hard_limit", limit="1")
    await asyncio.to_thread(
        costs.store.reserve_sync, budget, call_id="saved-before-finalize",
        workflow_id="workflow-1", branch_id="original", agent_name="Developer",
        amount=Decimal("0.5"), currency="USD", ttl_seconds=300,
    )
    await costs.store.save_calculation({
        "llm_call_id":"saved-before-finalize","workflow_id":"workflow-1",
        "branch_id":"original","agent_name":"Developer","provider":"openai",
        "model":"gpt-5-mini","operation":"create","currency":"USD",
        "usage_source":"provider_reported","cost_source":"calculated",
        "cost_status":"calculated","total_cost":Decimal("0.4"),
        "calculation_version":"test","warnings":[],
    })
    await costs.calculate_call("saved-before-finalize")
    await costs.calculate_call("saved-before-finalize")
    usage = await costs.store.budget_usage(budget["budget_id"])
    events = await costs.observability_store.fetch_all(
        "SELECT * FROM llm_budget_events WHERE llm_call_id='saved-before-finalize'"
    )
    assert usage["consumed"] == "0.4" and len(events) == 1


@pytest.mark.asyncio
async def test_multiple_global_and_workflow_budgets_consume_same_correlated_call(costs):
    global_budget = await add_budget(costs, "hard_limit", name="global", limit="2")
    workflow_budget = await costs.store.create_budget({
        "name":"workflow","scope_type":"workflow","scope_value":"workflow-1",
        "currency":"USD","limit_amount":Decimal("2"),"warning_percent":Decimal("80"),
        "enforcement_mode":"hard_limit","enabled":True,
    })
    decision = await costs.preflight(
        call_id="multi-budget-call", workflow_id="workflow-1", branch_id="original",
        agent_name="Developer", provider="test", model="test-model", kwargs={},
        estimated_amount=Decimal("0.5"),
    )
    assert set(decision["applicable_budgets"]) == {
        global_budget["budget_id"], workflow_budget["budget_id"],
    }
    await asyncio.to_thread(
        costs.store.finalize_reservations_sync, "multi-budget-call", Decimal("0.4")
    )
    for budget in (global_budget, workflow_budget):
        usage = await costs.store.budget_usage(budget["budget_id"])
        assert usage["consumed"] == "0.4" and usage["reserved"] == "0"


@pytest.mark.asyncio
async def test_budget_period_accepts_mixed_space_and_iso_timestamp_formats(costs):
    now = datetime.now(UTC)
    budget = await add_budget(
        costs, "hard_limit", limit="1",
        period_start=(now - timedelta(hours=1)).isoformat().replace("T", " "),
        period_end=(now + timedelta(hours=1)).isoformat().replace("T", " "),
    )
    await asyncio.to_thread(
        costs.store.reserve_sync, budget, call_id="mixed-date", workflow_id="workflow-1",
        branch_id="original", agent_name="Developer", amount=Decimal("0.5"),
        currency="USD", ttl_seconds=300,
    )
    await asyncio.to_thread(costs.store.finalize_reservations_sync, "mixed-date", Decimal("0.4"))
    assert (await costs.store.budget_usage(budget["budget_id"]))["consumed"] == "0.4"


def test_budget_precedence_is_canonical():
    assert ENFORCEMENT_PRECEDENCE == {"observe_only": 0, "warn": 1, "soft_limit": 2, "hard_limit": 3}
    assert DECISION_PRECEDENCE == {"allow": 0, "warn": 1, "block": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "expected", "blocking"), [
    ("observe_only", "warn", False),
    ("warn", "warn", False),
    ("soft_limit", "warn", False),
    ("hard_limit", "block", True),
])
async def test_single_budget_enforcement_semantics(costs, mode, expected, blocking):
    budget = await add_budget(costs, mode)
    result = await costs.preflight(
        call_id=f"single-{mode}", workflow_id="w", branch_id="original",
        agent_name="Developer", provider="openai", model="gpt-5-mini", kwargs={},
        estimated_amount=Decimal("2"), raise_on_block=False,
    )
    assert result["decision"] == expected
    assert (budget["budget_id"] in result["blocking_budgets"]) is blocking
    assert len(result["reason_codes"]) == len(set(result["reason_codes"]))


@pytest.mark.asyncio
@pytest.mark.parametrize(("modes", "expected"), [
    (("observe_only", "warn"), "warn"),
    (("observe_only", "hard_limit"), "block"),
    (("warn", "hard_limit"), "block"),
    (("soft_limit", "hard_limit"), "block"),
    (("observe_only", "warn", "hard_limit"), "block"),
    (("hard_limit", "hard_limit"), "block"),
])
async def test_multiple_budgets_keep_most_restrictive_decision(costs, modes, expected):
    budgets = [await add_budget(costs, mode, name=f"{mode}-{index}") for index, mode in enumerate(modes)]
    result = await costs.preflight(
        call_id="combined", workflow_id="w", branch_id="original", agent_name="Developer",
        provider="openai", model="gpt-5-mini", kwargs={}, estimated_amount=Decimal("2"),
        raise_on_block=False,
    )
    assert result["decision"] == expected
    assert set(result["applicable_budgets"]) == {budget["budget_id"] for budget in budgets}
    expected_blocking = {budget["budget_id"] for budget in budgets if budget["enforcement_mode"] == "hard_limit"}
    assert set(result["blocking_budgets"]) == expected_blocking
    assert len(result["reason_codes"]) == len(set(result["reason_codes"]))
    if expected == "block":
        assert {"budget_exceeded", "reservation_exceeds_budget", "hard_limit"} <= set(result["reason_codes"])


@pytest.mark.asyncio
async def test_block_releases_permissive_reservation_and_keeps_hard_rejected(costs):
    permissive = await add_budget(costs, "observe_only", limit="10")
    hard = await add_budget(costs, "hard_limit", limit="1")
    result = await costs.preflight(
        call_id="mixed-release", workflow_id="w", branch_id="original", agent_name="Developer",
        provider="openai", model="gpt-5-mini", kwargs={}, estimated_amount=Decimal("2"),
        raise_on_block=False,
    )
    rows = await costs.observability_store.fetch_all(
        "SELECT budget_id,status,metadata_json FROM llm_budget_reservations WHERE llm_call_id='mixed-release'"
    )
    statuses = {row["budget_id"]: row["status"] for row in rows}
    assert result["decision"] == "block" and result["blocking_budgets"] == [hard["budget_id"]]
    assert statuses[hard["budget_id"]] == "rejected"
    assert statuses[permissive["budget_id"]] == "released"


@pytest.mark.asyncio
async def test_reconciliation_releases_legacy_active_reservation_after_block(costs):
    budget = await add_budget(costs, "observe_only", limit="10")
    reservation = await asyncio.to_thread(
        costs.store.reserve_sync, budget, call_id="legacy-blocked", workflow_id="w",
        branch_id="original", agent_name="Developer", amount=Decimal("1"),
        currency="USD", ttl_seconds=300,
    )
    assert reservation["status"] == "reserved"
    await asyncio.to_thread(costs.store.add_budget_event_sync, {
        "budget_id": budget["budget_id"], "llm_call_id": "legacy-blocked",
        "workflow_id": "w", "branch_id": "original",
        "event_type": "budget_call_blocked", "decision": "block",
        "reason_code": "hard_limit", "amount": Decimal("1"),
    })

    dry = await reconcile_costs(costs.store.database_path)
    applied = await reconcile_costs(costs.store.database_path, dry_run=False)
    second = await reconcile_costs(costs.store.database_path)
    row = await costs.observability_store.fetch_one(
        "SELECT status FROM llm_budget_reservations WHERE reservation_id=?",
        (reservation["reservation_id"],),
    )

    assert dry["blocked_active_reservations"] == 1
    assert dry["released_blocked_reservations"] == 0
    assert applied["released_blocked_reservations"] == 1
    assert second["blocked_active_reservations"] == 0
    assert row["status"] == "released"


@pytest.mark.asyncio
async def test_historical_consumption_reconciliation_is_safe_and_idempotent(costs):
    budget = await add_budget(costs, "hard_limit", limit="1")
    reservation = await asyncio.to_thread(
        costs.store.reserve_sync, budget, call_id="historical-cost", workflow_id="workflow-1",
        branch_id="original", agent_name="Developer", amount=Decimal("0.5"),
        currency="USD", ttl_seconds=300,
    )
    await costs.store.save_calculation({
        "llm_call_id":"historical-cost","workflow_id":"workflow-1","branch_id":"original",
        "agent_name":"Developer","provider":"openai","model":"gpt-5-mini",
        "operation":"create","currency":"USD","usage_source":"provider_reported",
        "cost_source":"calculated","cost_status":"calculated","total_cost":Decimal("0.4"),
        "calculation_version":"test","warnings":[],
    })
    dry = await reconcile_costs(costs.store.database_path)
    applied = await reconcile_costs(costs.store.database_path, dry_run=False)
    second = await reconcile_costs(costs.store.database_path, dry_run=False)
    row = await costs.observability_store.fetch_one(
        "SELECT * FROM llm_budget_reservations WHERE reservation_id=?",
        (reservation["reservation_id"],),
    )
    usage = await costs.store.budget_usage(budget["budget_id"])
    assert dry["consumption_candidates"] == 1 and dry["consumption_state_repairs"] == 1
    assert applied["new_consumed"] == 1
    assert row["status"] == "consumed" and row["consumed_amount"] == "0.4"
    assert usage["consumed"] == "0.4" and usage["remaining"] == "0.6"
    assert second["new_consumed"] == 0 and second["consumption_candidates"] == 0


@pytest.mark.asyncio
async def test_reconciliation_backfills_missing_audit_but_never_invents_reservation(costs):
    budget = await add_budget(costs, "hard_limit", limit="1")
    reservation = await asyncio.to_thread(
        costs.store.reserve_sync, budget, call_id="missing-audit", workflow_id="workflow-1",
        branch_id="original", agent_name="Developer", amount=Decimal("0.5"),
        currency="USD", ttl_seconds=300,
    )
    await costs.store.save_calculation({
        "llm_call_id":"missing-audit","workflow_id":"workflow-1","branch_id":"original",
        "agent_name":"Developer","provider":"openai","model":"gpt-5-mini",
        "operation":"create","currency":"USD","usage_source":"provider_reported",
        "cost_source":"calculated","cost_status":"calculated","total_cost":Decimal("0.4"),
        "calculation_version":"test","warnings":[],
    })
    await costs.observability_store.execute(
        """UPDATE llm_budget_reservations SET status='consumed',consumed_amount='0.4',
           released_at=? WHERE reservation_id=?""",
        (datetime.now(UTC).isoformat(), reservation["reservation_id"]),
    )
    await costs.store.save_calculation({
        "llm_call_id":"no-reservation","workflow_id":"workflow-1","branch_id":"original",
        "agent_name":"Developer","provider":"openai","model":"gpt-5-mini",
        "operation":"create","currency":"USD","usage_source":"provider_reported",
        "cost_source":"calculated","cost_status":"calculated","total_cost":Decimal("0.2"),
        "calculation_version":"test","warnings":[],
    })
    dry = await reconcile_costs(costs.store.database_path)
    applied = await reconcile_costs(costs.store.database_path, dry_run=False)
    second = await reconcile_costs(costs.store.database_path, dry_run=False)
    events = await costs.observability_store.fetch_all(
        "SELECT * FROM llm_budget_events WHERE llm_call_id='missing-audit'"
    )
    fabricated = await costs.observability_store.fetch_all(
        "SELECT * FROM llm_budget_reservations WHERE llm_call_id='no-reservation'"
    )
    assert dry["consumption_audit_repairs"] == 1
    assert dry["unreconciled"] == 1 and dry["unreconciled_call_ids"] == ["no-reservation"]
    assert applied["consumption_audit_events_created"] == 1 and len(events) == 1
    assert fabricated == []
    assert second["consumption_audit_events_created"] == 0


@pytest.mark.asyncio
async def test_incompatible_disabled_and_out_of_period_budgets_do_not_apply(costs):
    future = datetime.now(UTC) + timedelta(days=2)
    euro = await add_budget(costs, "hard_limit", currency="EUR")
    disabled = await add_budget(costs, "hard_limit", name="disabled", enabled=False)
    out_of_period = await add_budget(costs, "hard_limit", name="future", period_start=future, period_end=future + timedelta(days=1))
    result = await costs.preflight(
        call_id="filtered", workflow_id="w", branch_id="original", agent_name="Developer",
        provider="openai", model="gpt-5-mini", kwargs={}, estimated_amount=Decimal("2"),
        currency="USD", raise_on_block=False,
    )
    assert result["decision"] == "allow"
    assert not ({euro["budget_id"], disabled["budget_id"], out_of_period["budget_id"]} & set(result["applicable_budgets"]))


@pytest.mark.asyncio
async def test_soft_limit_blocks_only_after_previous_consumption_exhausted_budget(costs):
    budget = await add_budget(costs, "soft_limit")
    first = await costs.preflight(
        call_id="soft-current", workflow_id="w", branch_id="original", agent_name="Developer",
        provider="openai", model="gpt-5-mini", kwargs={}, estimated_amount=Decimal("2"),
        raise_on_block=False,
    )
    assert first["decision"] == "warn" and budget["budget_id"] not in first["blocking_budgets"]
    await costs.store.save_calculation({
        "llm_call_id":"spent","workflow_id":"w","branch_id":"original","agent_name":"Developer",
        "provider":"openai","model":"gpt-5-mini","operation":"parse","currency":"USD",
        "usage_source":"provider_reported","cost_source":"calculated","cost_status":"calculated",
        "total_cost":Decimal("2"),"calculation_version":"test","warnings":[],
    })
    await asyncio.to_thread(
        costs.store.reserve_sync, budget, call_id="spent", workflow_id="w",
        branch_id="original", agent_name="Developer", amount=Decimal("1"),
        currency="USD", ttl_seconds=300,
    )
    await asyncio.to_thread(costs.store.finalize_reservations_sync, "spent", Decimal("2"))
    subsequent = await costs.preflight(
        call_id="soft-next", workflow_id="w", branch_id="original", agent_name="Developer",
        provider="openai", model="gpt-5-mini", kwargs={}, estimated_amount=Decimal("0.1"),
        raise_on_block=False,
    )
    assert subsequent["decision"] == "block"
    assert subsequent["blocking_budgets"] == [budget["budget_id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(("reserved", "estimated"), [("1", "0.1"), ("0.75", "0.5")])
async def test_hard_limit_accounts_for_existing_reservations(costs, reserved, estimated):
    budget = await add_budget(costs, "hard_limit")
    existing = await asyncio.to_thread(
        costs.store.reserve_sync, budget, call_id="existing", workflow_id="w",
        branch_id="original", agent_name="Developer", amount=Decimal(reserved),
        currency="USD", ttl_seconds=300,
    )
    assert existing["status"] == "reserved"
    result = await costs.preflight(
        call_id=f"next-{reserved}", workflow_id="w", branch_id="original", agent_name="Developer",
        provider="openai", model="gpt-5-mini", kwargs={}, estimated_amount=Decimal(estimated),
        raise_on_block=False,
    )
    assert result["decision"] == "block"
    assert result["blocking_budgets"] == [budget["budget_id"]]


@pytest.mark.asyncio
async def test_budget_check_api_returns_aggregate_block(costs):
    await add_budget(costs, "observe_only", name="observe")
    await add_budget(costs, "warn", name="warning")
    hard = await add_budget(costs, "hard_limit", name="hard")
    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(llm_costs=costs)
    with TestClient(create_app(factory)) as client:
        response = client.post("/api/llm-costs/budgets/check", json={
            "workflow_id":"manual-hard-limit-check","branch_id":"original",
            "project_name":"manual-validation","agent_name":"Developer",
            "model":"gpt-5-mini","estimated_amount":"2","currency":"USD",
        })
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "block"
    assert hard["budget_id"] in body["applicable_budgets"]
    assert body["blocking_budgets"] == [hard["budget_id"]]
    assert len(body["reason_codes"]) == len(set(body["reason_codes"]))


@pytest.mark.asyncio
async def test_concurrent_reservations_prevent_overspending(costs):
    budget = await costs.store.create_budget({"name":"Concurrent","scope_type":"global","currency":"USD","limit_amount":Decimal("1"),"warning_percent":Decimal("80"),"enforcement_mode":"hard_limit","enabled":True})
    async def reserve(call_id):
        try:
            return await costs.preflight(call_id=call_id,workflow_id="w",branch_id="original",agent_name="A",provider="test",model="test-model",kwargs={},estimated_amount=Decimal("0.75"))
        except BudgetExceededError:
            return {"decision":"block"}
    results = await asyncio.gather(reserve("a"), reserve("b"))
    assert sorted(item["decision"] for item in results) == ["allow", "block"]


@pytest.mark.asyncio
async def test_wrapper_hard_limit_never_calls_provider(costs):
    await costs.store.create_pricing(pricing(provider="openai"))
    await costs.store.create_budget({"name":"No provider","scope_type":"global","currency":"USD","limit_amount":Decimal("0.000001"),"warning_percent":Decimal("80"),"enforcement_mode":"hard_limit","enabled":True})
    called = 0
    class Responses:
        async def parse(self, **kwargs):
            nonlocal called; called += 1; return SimpleNamespace(id="response", usage=None)
    observable = ObservabilityService(costs.observability_store)
    context = await observable.ensure_trace("budget-workflow")
    client = ObservableOpenAIClient(SimpleNamespace(responses=Responses()), observable, costs)
    with observability_context(context), pytest.raises(BudgetExceededError):
        await client.responses.parse(model="test-model", input="x" * 100, max_output_tokens=100)
    assert called == 0
    calls = await costs.observability_store.fetch_all(
        "SELECT call_id FROM observability_llm_calls WHERE workflow_id='budget-workflow'"
    )
    events = await costs.observability_store.fetch_all(
        "SELECT event_type,decision FROM llm_budget_events WHERE workflow_id='budget-workflow'"
    )
    assert calls == []
    assert any(event["event_type"] == "budget_call_blocked" and event["decision"] == "block" for event in events)


def test_pricing_model_rejects_negative_and_invalid_dates():
    with pytest.raises(Exception): PricingCreate.model_validate(pricing(input_price_per_million=Decimal("-1")))
    with pytest.raises(Exception): PricingCreate.model_validate(pricing(effective_to=datetime(2019,1,1,tzinfo=UTC)))


def test_llm_cost_api_summary_and_pricing_empty(costs):
    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(llm_costs=costs)
    with TestClient(create_app(factory)) as client:
        summary = client.get("/api/llm-costs/summary")
        catalog = client.get("/api/llm-costs/pricing")
        invalid = client.get("/api/llm-costs/calls?sort_by=invalid")
    assert summary.status_code == 200 and summary.json()["state"] == "unavailable"
    assert catalog.status_code == 200 and catalog.json()["items"] == []
    assert invalid.status_code == 422


def test_llm_cost_aggregate_and_timeseries_routes_propagate_filters():
    class FakeCosts:
        def __init__(self):
            self.aggregate_calls = []
            self.list_calls_filters = []

        async def aggregate(self, dimension, **filters):
            self.aggregate_calls.append((dimension, filters))
            return {"items": []}

        async def list_calls(self, **filters):
            self.list_calls_filters.append(filters)
            return {"items": [], "total": 0}

    fake = FakeCosts()

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(llm_costs=fake)

    query = "provider=openai&model=gpt-5-mini&workflow_id=workflow-1"
    with TestClient(create_app(factory)) as client:
        agents = client.get(f"/api/llm-costs/agents?{query}")
        series = client.get(f"/api/llm-costs/timeseries?{query}")

    assert agents.status_code == 200
    assert series.status_code == 200
    assert fake.aggregate_calls[0][0] == "agents"
    assert fake.aggregate_calls[0][1]["provider"] == "openai"
    assert fake.aggregate_calls[0][1]["model"] == "gpt-5-mini"
    assert fake.aggregate_calls[0][1]["workflow_id"] == "workflow-1"
    assert fake.list_calls_filters[0]["provider"] == "openai"
    assert fake.list_calls_filters[0]["model"] == "gpt-5-mini"
    assert fake.list_calls_filters[0]["workflow_id"] == "workflow-1"


@pytest.mark.asyncio
async def test_budget_reservations_route_exposes_safe_fields(costs):
    budget = await add_budget(costs, "warn")
    reservation = await asyncio.to_thread(
        costs.store.reserve_sync, budget, call_id="safe-reservation",
        workflow_id="workflow-1", branch_id="original", agent_name="Planner",
        amount=Decimal("0.1"), currency="USD", ttl_seconds=300,
    )

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(llm_costs=costs)

    with TestClient(create_app(factory)) as client:
        response = client.get(
            f"/api/llm-costs/budgets/{budget['budget_id']}/reservations"
        )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["reservation_id"] == reservation["reservation_id"]
    assert item["workflow_id"] == "workflow-1"
    assert "metadata_json" not in item
