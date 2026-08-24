from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace

import pytest

from api.services.llm_cost_service import LLMCostService
from api.services.llm_cost_store import LLMCostStore
from api.services.llm_observability import ObservableOpenAIClient
from api.services.observability_service import ObservabilityService
from api.services.observability_store import ObservabilityStore
from scripts.validate_real_llm_spend import (
    AGENT_NAME,
    BRANCH_ID,
    BUDGET_ID,
    CONFIRMATION,
    MODEL,
    OPERATION,
    PRICING_ID,
    Runtime,
    SpendSettings,
    WORKFLOW_ID,
    build_parser,
    build_prompt,
    parse_target,
    require_confirmation,
    safe_output_tokens,
    validate_spend,
    worst_case_cost,
)


class MockResponses:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("mock provider failure")
        return SimpleNamespace(
            id=f"resp-{len(self.calls)}",
            output_text="fictional technical records",
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
                input_tokens_details=SimpleNamespace(cached_tokens=20),
                output_tokens_details=SimpleNamespace(reasoning_tokens=10),
            ),
        )


@pytest.fixture
async def validation_runtime(tmp_path):
    database = tmp_path / "validation.sqlite"
    observability_store = ObservabilityStore(database)
    observability = ObservabilityService(observability_store)
    await observability.initialize()
    store = LLMCostStore(database)
    costs = LLMCostService(store, observability_store)
    await costs.initialize()
    await store.create_pricing({
        "pricing_id": PRICING_ID,
        "provider": "openai",
        "model_pattern": MODEL,
        "model_canonical_name": MODEL,
        "currency": "USD",
        "input_price_per_million": Decimal("5.00"),
        "cached_input_price_per_million": Decimal("0.50"),
        "output_price_per_million": Decimal("30.00"),
        "effective_from": datetime(2020, 1, 1, tzinfo=UTC),
        "source_type": "test_fixture",
        "enabled": True,
        "reasoning_in_completion": True,
    })
    await store.create_budget({
        "budget_id": BUDGET_ID,
        "name": "Controlled real spend",
        "scope_type": "global",
        "scope_value": None,
        "currency": "USD",
        "limit_amount": Decimal("0.95"),
        "warning_percent": Decimal("80"),
        "enforcement_mode": "hard_limit",
        "enabled": True,
    })
    responses = MockResponses()
    client = ObservableOpenAIClient(
        SimpleNamespace(responses=responses), observability, costs
    )
    return Runtime(observability, costs, client), responses


def test_cli_defaults_to_non_spending_mode_and_validates_target():
    args = build_parser().parse_args([])
    assert args.execute is False
    assert args.target_usd == Decimal("0.90")
    assert args.max_output_tokens == 30_000
    assert MODEL == "gpt-5.6-sol"
    assert parse_target("0.95") == Decimal("0.95")
    with pytest.raises(Exception):
        parse_target("0")
    with pytest.raises(Exception):
        parse_target("0.951")


def test_real_execution_confirmation_is_exact(monkeypatch):
    args = build_parser().parse_args(["--execute"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert require_confirmation(args, input_fn=lambda _: CONFIRMATION) is True
    with pytest.raises(RuntimeError, match="Confirmation did not match"):
        require_confirmation(args, input_fn=lambda _: "spend 0.95 usd")


def test_noninteractive_execution_requires_explicit_ack(monkeypatch):
    args = build_parser().parse_args(["--execute"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(RuntimeError, match="non-interactive"):
        require_confirmation(args)
    acknowledged = build_parser().parse_args(
        ["--execute", "--yes-i-understand-real-cost"]
    )
    assert require_confirmation(acknowledged) is True


@pytest.mark.asyncio
async def test_dry_run_never_invokes_mock_provider(validation_runtime):
    runtime, responses = validation_runtime
    output = StringIO()
    result = await validate_spend(runtime, SpendSettings(), output=output)
    assert responses.calls == []
    assert result["provider_calls"] == 0
    assert result["stop_reason"] == "dry_run"
    assert result["usage_source"] == "unavailable"
    assert PRICING_ID in output.getvalue() and BUDGET_ID in output.getvalue()


@pytest.mark.asyncio
async def test_dry_run_reports_zero_calls_when_input_margin_cannot_fit(validation_runtime):
    runtime, responses = validation_runtime
    output = StringIO()
    result = await validate_spend(
        runtime, SpendSettings(target_usd=Decimal("0.0001")), output=output
    )
    assert responses.calls == [] and result["provider_calls"] == 0
    assert '"planned_first_max_output_tokens": 0' in output.getvalue()
    assert '"estimated_calls": 0' in output.getvalue()


@pytest.mark.asyncio
async def test_execute_uses_observable_wrapper_and_persists_exact_operation(validation_runtime):
    runtime, responses = validation_runtime
    output = StringIO()
    result = await validate_spend(
        runtime,
        SpendSettings(
            target_usd=Decimal("0.01"),
            max_output_tokens=100,
            max_calls=1,
            execute=True,
            confirmed=True,
        ),
        output=output,
    )
    assert result["provider_calls"] == 1
    assert result["provider_failed_calls"] == 0
    assert len(responses.calls) == 1
    provider_kwargs = responses.calls[0]
    assert provider_kwargs["model"] == MODEL
    assert provider_kwargs["input"] == build_prompt()
    assert "_observability_operation" not in provider_kwargs
    assert not {"tools", "text", "response_format"}.intersection(provider_kwargs)
    calls = await runtime.costs.list_calls(workflow_id=WORKFLOW_ID, branch_id=BRANCH_ID)
    call = calls["items"][0]
    assert call["agent"] == AGENT_NAME
    assert call["operation"] == OPERATION
    assert call["usage_source"] == "provider_reported"
    assert call["pricing_id"] == PRICING_ID
    assert Decimal(call["total_cost"]) == Decimal("0.00191")
    assert (call["input_tokens"], call["output_tokens"], call["total_tokens"]) == (100, 50, 150)
    reservations = await runtime.costs.observability_store.fetch_all(
        "SELECT status FROM llm_budget_reservations WHERE llm_call_id=?", (call["call_id"],)
    )
    assert reservations == [{"status": "consumed"}]


@pytest.mark.asyncio
async def test_output_contains_hashes_but_not_prompt_response_or_secrets(validation_runtime):
    runtime, _ = validation_runtime
    output = StringIO()
    await validate_spend(
        runtime,
        SpendSettings(target_usd=Decimal("0.01"), max_output_tokens=100,
                      max_calls=1, execute=True, confirmed=True),
        output=output,
    )
    rendered = output.getvalue()
    assert "OutputSha256" in rendered and "prompt_sha256" in rendered
    assert build_prompt() not in rendered
    assert "fictional technical records" not in rendered
    assert "OPENAI_API_KEY" not in rendered and "sk-" not in rendered


@pytest.mark.asyncio
async def test_provider_failure_is_not_retried_and_is_persisted(validation_runtime):
    runtime, _ = validation_runtime
    responses = MockResponses(fail=True)
    runtime.client = ObservableOpenAIClient(
        SimpleNamespace(responses=responses), runtime.observability, runtime.costs
    )
    result = await validate_spend(
        runtime,
        SpendSettings(target_usd=Decimal("0.01"), max_output_tokens=100,
                      max_calls=5, execute=True, confirmed=True),
        output=StringIO(),
    )
    assert len(responses.calls) == 1
    assert result["provider_calls"] == 0
    assert result["provider_failed_calls"] == 1
    assert result["stop_reason"] == "provider_failed"
    assert result["usage_source"] == "unavailable"
    calls = await runtime.costs.list_calls(workflow_id=WORKFLOW_ID)
    assert calls["total"] == 1 and calls["items"][0]["status"] == "failed"
    assert calls["items"][0]["usage_source"] == "unavailable"


@pytest.mark.asyncio
async def test_budget_block_reports_no_provider_call(validation_runtime):
    runtime, responses = validation_runtime
    await runtime.costs.store.create_budget({
        "name": "Mock pre-provider block",
        "scope_type": "global",
        "scope_value": None,
        "currency": "USD",
        "limit_amount": Decimal("0.000001"),
        "warning_percent": Decimal("50"),
        "enforcement_mode": "hard_limit",
        "enabled": True,
    })
    output = StringIO()
    result = await validate_spend(
        runtime,
        SpendSettings(target_usd=Decimal("0.01"), max_output_tokens=100,
                      max_calls=1, execute=True, confirmed=True),
        output=output,
    )
    assert responses.calls == []
    assert result["blocked_calls"] == 1
    assert result["provider_calls"] == 0
    assert result["stop_reason"] == "budget_blocked"
    assert result["usage_source"] == "unavailable"
    assert "ProviderCalled = false" in output.getvalue()


def test_dynamic_max_output_tokens_uses_current_prices_and_available_margin():
    pricing = {
        "input_price_per_million": "5.00",
        "output_price_per_million": "30.00",
    }
    prompt = build_prompt()
    initial = safe_output_tokens(pricing, prompt, 30_000, Decimal("0.90"))
    reduced = safe_output_tokens(pricing, prompt, 30_000, Decimal("0.03"))
    assert 29_000 < initial < 30_000
    assert 0 < reduced < initial
    assert worst_case_cost(pricing, prompt, initial) <= Decimal("0.90")
    assert worst_case_cost(pricing, prompt, reduced) <= Decimal("0.03")


@pytest.mark.asyncio
async def test_real_usage_drives_next_call_and_never_overspends_hard_cap(validation_runtime):
    runtime, _ = validation_runtime

    class MaxAwareResponses:
        def __init__(self):
            self.maximums = []

        async def create(self, **kwargs):
            self.maximums.append(kwargs["max_output_tokens"])
            output_tokens = kwargs["max_output_tokens"]
            return SimpleNamespace(
                id=f"adaptive-{len(self.maximums)}",
                output_text="safe",
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=output_tokens,
                    total_tokens=100 + output_tokens,
                    input_tokens_details=SimpleNamespace(cached_tokens=0),
                    output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                ),
            )

    responses = MaxAwareResponses()
    runtime.client = ObservableOpenAIClient(
        SimpleNamespace(responses=responses), runtime.observability, runtime.costs
    )
    result = await validate_spend(
        runtime,
        SpendSettings(target_usd=Decimal("0.90"), max_output_tokens=30_000,
                      max_calls=3, execute=True, confirmed=True),
        output=StringIO(),
    )
    assert responses.maximums
    assert responses.maximums[0] < 30_000
    assert Decimal(result["real_cost_usd"]) <= Decimal("0.95")
    assert Decimal(result["budget_consumed"]) <= Decimal("0.95")
    assert result["target_reached"] is True
    assert result["usage_source"] == "provider_reported"


@pytest.mark.asyncio
async def test_missing_or_wrong_fixed_configuration_fails_before_provider(validation_runtime):
    runtime, responses = validation_runtime
    await runtime.costs.store.update_budget(BUDGET_ID, {"enabled": False})
    with pytest.raises(RuntimeError, match="Budget configuration mismatch"):
        await validate_spend(
            runtime,
            SpendSettings(execute=True, confirmed=True),
            output=StringIO(),
        )
    assert responses.calls == []
