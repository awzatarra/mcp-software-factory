from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Callable, TextIO

from dotenv import load_dotenv

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from api.services.llm_cost_service import BudgetExceededError, LLMCostService
from api.services.llm_cost_store import LLMCostStore
from api.services.llm_observability import ObservableOpenAIClient
from api.services.observability_context import observability_context
from api.services.observability_service import ObservabilityService
from api.services.observability_store import ObservabilityStore
from streaming.sqlite_store import workflow_event_store_path


PROVIDER = "openai"
MODEL = "gpt-5.6-sol"
WORKFLOW_ID = "manual-one-dollar-validation"
BRANCH_ID = "original"
AGENT_NAME = "RealSpendValidation"
OPERATION = "real_spend_validation"
BUDGET_ID = "b9d2c5a1c7d94ffdb4a2ba934529f98a"
PRICING_ID = "5fa368b191c646e7a0fb6821d4390add"
TARGET_MIN_USD = Decimal("0.85")
TARGET_MAX_USD = Decimal("0.95")
DEFAULT_TARGET_USD = Decimal("0.90")
CONFIRMATION = "SPEND 0.95 USD"
MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class SpendSettings:
    target_usd: Decimal = DEFAULT_TARGET_USD
    max_output_tokens: int = 30_000
    max_calls: int = 20
    execute: bool = False
    confirmed: bool = False


@dataclass(slots=True)
class Runtime:
    observability: ObservabilityService
    costs: LLMCostService
    client: Any | None = None


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000000000001")), "f")


def parse_target(value: str) -> Decimal:
    try:
        target = Decimal(value)
    except Exception as exc:
        raise argparse.ArgumentTypeError("target must be a decimal USD amount") from exc
    if target <= 0 or target > TARGET_MAX_USD:
        raise argparse.ArgumentTypeError("target must be > 0 and <= 0.95")
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely validate real OpenAI usage through the existing LLM FinOps stack."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Plan only; never invoke OpenAI.")
    mode.add_argument("--execute", action="store_true", help="Permit controlled real OpenAI spend.")
    parser.add_argument("--yes-i-understand-real-cost", action="store_true")
    parser.add_argument("--target-usd", type=parse_target, default=DEFAULT_TARGET_USD)
    parser.add_argument("--max-output-tokens", type=int, default=30_000)
    parser.add_argument("--max-calls", type=int, default=20)
    parser.add_argument("--database", type=Path, default=workflow_event_store_path())
    return parser


def build_prompt() -> str:
    return (
        "Generate a long plain-text collection of fictional technical documentation records. "
        "Number every record sequentially. Each record must contain a fictional component name, "
        "purpose, inputs, outputs, invariant, routine failure mode, and deterministic verification "
        "note. Use no markdown tables, no source code, no tools, no external facts, and no unsafe "
        "content. Continue producing distinct records until the response limit is reached."
    )


async def build_runtime(database: Path, *, execute: bool) -> Runtime:
    observability_store = ObservabilityStore(database)
    observability = ObservabilityService(observability_store)
    await observability.initialize()
    cost_store = LLMCostStore(database)
    costs = LLMCostService(cost_store, observability_store)
    await costs.initialize()
    if not execute:
        return Runtime(observability=observability, costs=costs)
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required only for --execute")
    from openai import AsyncOpenAI

    raw_client = AsyncOpenAI(api_key=api_key, max_retries=0)
    return Runtime(
        observability=observability,
        costs=costs,
        client=ObservableOpenAIClient(raw_client, observability, costs),
    )


async def validate_configuration(costs: LLMCostService) -> tuple[dict[str, Any], dict[str, Any]]:
    pricing = await costs.store.get_pricing(PRICING_ID)
    budget = await costs.store.get_budget(BUDGET_ID)
    if pricing is None:
        raise RuntimeError(f"Required pricing not found: {PRICING_ID}")
    if budget is None:
        raise RuntimeError(f"Required budget not found: {BUDGET_ID}")
    expected_pricing = {
        "provider": PROVIDER,
        "input_price_per_million": "5.00",
        "cached_input_price_per_million": "0.50",
        "output_price_per_million": "30.00",
        "currency": "USD",
    }
    numeric_pricing_fields = {
        "input_price_per_million", "cached_input_price_per_million",
        "output_price_per_million",
    }
    mismatches = [
        key for key, expected in expected_pricing.items()
        if (
            _decimal(pricing.get(key)) if key in numeric_pricing_fields
            else str(pricing.get(key)).casefold()
        ) != (
            _decimal(expected) if key in numeric_pricing_fields
            else str(expected).casefold()
        )
    ]
    resolved = await costs.store.resolve_pricing(
        PROVIDER, MODEL, datetime.now(UTC).isoformat(), "USD"
    )
    if mismatches or not pricing.get("enabled") or not resolved or resolved.get("pricing_id") != PRICING_ID:
        raise RuntimeError(
            f"Pricing configuration mismatch: fields={mismatches}, resolved_id="
            f"{resolved.get('pricing_id') if resolved else None}"
        )
    expected_budget = {
        "scope_type": "global",
        "currency": "USD",
        "limit_amount": Decimal("0.95"),
        "enforcement_mode": "hard_limit",
    }
    budget_mismatches = [
        key for key, expected in expected_budget.items()
        if (_decimal(budget.get(key)) if key == "limit_amount" else budget.get(key)) != expected
    ]
    if budget_mismatches or not budget.get("enabled"):
        raise RuntimeError(f"Budget configuration mismatch: fields={budget_mismatches}")
    return pricing, budget


async def snapshot(costs: LLMCostService) -> dict[str, Any]:
    return {
        "summary": await costs.summary(),
        "workflow_summary": await costs.summary(
            workflow_id=WORKFLOW_ID, branch_id=BRANCH_ID
        ),
        "budget_usage": await costs.store.budget_usage(BUDGET_ID),
    }


def estimate_input_tokens(prompt: str) -> int:
    return max(math.ceil(len(json.dumps(prompt, ensure_ascii=True)) / 4), 1)


def worst_case_cost(pricing: dict[str, Any], prompt: str, output_tokens: int) -> Decimal:
    return (
        Decimal(estimate_input_tokens(prompt)) * _decimal(pricing["input_price_per_million"])
        + Decimal(output_tokens) * _decimal(pricing["output_price_per_million"])
    ) / MILLION


def safe_output_tokens(
    pricing: dict[str, Any], prompt: str, configured_max: int, available_usd: Decimal
) -> int:
    input_cost = (
        Decimal(estimate_input_tokens(prompt))
        * _decimal(pricing["input_price_per_million"])
        / MILLION
    )
    output_price = _decimal(pricing["output_price_per_million"])
    if output_price <= 0 or available_usd <= input_cost:
        return 0
    affordable = ((available_usd - input_cost) * MILLION / output_price).to_integral_value(
        rounding=ROUND_DOWN
    )
    return max(0, min(configured_max, int(affordable)))


def local_cost(call: dict[str, Any], pricing: dict[str, Any]) -> dict[str, Decimal]:
    input_tokens = Decimal(int(call.get("input_tokens") or 0))
    cached_tokens = Decimal(int(call.get("cached_tokens") or 0))
    output_tokens = Decimal(int(call.get("output_tokens") or 0))
    uncached_tokens = max(input_tokens - cached_tokens, Decimal(0))
    uncached_cost = uncached_tokens * _decimal(pricing["input_price_per_million"]) / MILLION
    cached_cost = cached_tokens * _decimal(pricing["cached_input_price_per_million"]) / MILLION
    output_cost = output_tokens * _decimal(pricing["output_price_per_million"]) / MILLION
    return {
        "input": uncached_cost + cached_cost,
        "output": output_cost,
        "total": uncached_cost + cached_cost + output_cost,
    }


async def _new_call(costs: LLMCostService, previous_ids: set[str]) -> dict[str, Any] | None:
    result = await costs.list_calls(
        workflow_id=WORKFLOW_ID, branch_id=BRANCH_ID, limit=1000, sort_order="desc"
    )
    return next((item for item in result["items"] if item["call_id"] not in previous_ids), None)


def _emit_call(output: TextIO, number: int, call: dict[str, Any], costs: dict[str, Decimal],
               accumulated: Decimal, usage: dict[str, Any], decision: str) -> None:
    fields = {
        "CallNumber": number,
        "CallId": call["call_id"],
        "InputTokens": int(call.get("input_tokens") or 0),
        "OutputTokens": int(call.get("output_tokens") or 0),
        "TotalTokens": int(call.get("total_tokens") or 0),
        "InputCost": _money(costs["input"]),
        "OutputCost": _money(costs["output"]),
        "CallCost": _money(costs["total"]),
        "AccumulatedCost": _money(accumulated),
        "BudgetConsumed": usage["consumed"],
        "BudgetReserved": usage["reserved"],
        "BudgetRemaining": usage["remaining"],
        "Decision": decision,
    }
    print(json.dumps(fields, indent=2), file=output)


async def validate_spend(
    runtime: Runtime,
    settings: SpendSettings,
    *,
    output: TextIO = sys.stdout,
) -> dict[str, Any]:
    if settings.max_output_tokens <= 0 or settings.max_calls <= 0:
        raise ValueError("max-output-tokens and max-calls must be positive")
    pricing, budget = await validate_configuration(runtime.costs)
    before = await snapshot(runtime.costs)
    prompt = build_prompt()
    existing = await runtime.costs.list_calls(
        workflow_id=WORKFLOW_ID, branch_id=BRANCH_ID, limit=1000
    )
    known_ids = {item["call_id"] for item in existing["items"]}
    accumulated = _decimal(before["workflow_summary"]["real_cost"])
    budget_usage = before["budget_usage"]
    available = min(
        max(settings.target_usd - accumulated, Decimal(0)),
        _decimal(budget_usage["remaining"]),
        max(TARGET_MAX_USD - accumulated, Decimal(0)),
    )
    planned_max = safe_output_tokens(pricing, prompt, settings.max_output_tokens, available)
    estimated_call_cost = worst_case_cost(pricing, prompt, max(planned_max, 0))
    estimated_calls = (
        math.ceil(float(available / estimated_call_cost))
        if planned_max > 0 and estimated_call_cost > 0 else 0
    )
    print(json.dumps({
        "mode": "execute" if settings.execute else "dry-run",
        "provider": PROVIDER,
        "model": MODEL,
        "workflow_id": WORKFLOW_ID,
        "operation": OPERATION,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_length": len(prompt),
        "pricing_id": pricing["pricing_id"],
        "budget_id": budget["budget_id"],
        "budget": budget_usage,
        "target_usd": str(settings.target_usd),
        "hard_cap_usd": str(TARGET_MAX_USD),
        "configured_initial_max_output_tokens": settings.max_output_tokens,
        "planned_first_max_output_tokens": planned_max,
        "estimated_calls": min(estimated_calls, settings.max_calls),
        "before": before,
    }, indent=2, default=str), file=output)
    if not settings.execute:
        return {
            "provider": PROVIDER, "model": MODEL, "workflow_id": WORKFLOW_ID,
            "provider_calls": 0, "provider_failed_calls": 0, "input_tokens": 0,
            "output_tokens": 0, "total_tokens": 0, "real_cost_usd": _money(accumulated),
            "budget_consumed": budget_usage["consumed"],
            "budget_remaining": budget_usage["remaining"], "blocked_calls": 0,
            "target_reached": accumulated >= TARGET_MIN_USD,
            "stop_reason": "dry_run", "pricing_id": PRICING_ID,
            "usage_source": "unavailable",
        }
    if not settings.confirmed:
        raise RuntimeError("Real spend was not explicitly confirmed")
    if runtime.client is None:
        raise RuntimeError("Observable OpenAI client is required for --execute")

    counters = {"calls": 0, "failed": 0, "blocked": 0, "input": 0, "output": 0, "total": 0}
    stop_reason = "max_calls_reached"
    context = await runtime.observability.ensure_trace(WORKFLOW_ID, BRANCH_ID, source="manual_validation")
    context = context.child(agent=AGENT_NAME, node=OPERATION, operation=OPERATION)
    try:
        for call_number in range(1, settings.max_calls + 1):
            current = await snapshot(runtime.costs)
            accumulated = _decimal(current["workflow_summary"]["real_cost"])
            budget_usage = current["budget_usage"]
            if accumulated >= settings.target_usd:
                stop_reason = "target_reached"
                break
            if accumulated >= TARGET_MAX_USD:
                stop_reason = "hard_cap_reached"
                break
            available = min(
                settings.target_usd - accumulated,
                TARGET_MAX_USD - accumulated,
                _decimal(budget_usage["remaining"]),
            )
            max_tokens = safe_output_tokens(pricing, prompt, settings.max_output_tokens, available)
            if max_tokens <= 0:
                stop_reason = "insufficient_safe_budget"
                break
            try:
                with observability_context(context):
                    response = await runtime.client.responses.create(
                        model=MODEL,
                        input=prompt,
                        max_output_tokens=max_tokens,
                        _observability_operation=OPERATION,
                    )
                counters["calls"] += 1
                call = await _new_call(runtime.costs, known_ids)
                if call is None:
                    raise RuntimeError("Provider returned but no durable LLM call was found")
                known_ids.add(call["call_id"])
                if call.get("usage_source") != "provider_reported" or not call.get("usage_available"):
                    raise RuntimeError("Provider usage was not persisted as provider_reported")
                if call.get("pricing_id") != PRICING_ID:
                    raise RuntimeError(f"Unexpected pricing resolved: {call.get('pricing_id')}")
                calculated = local_cost(call, pricing)
                stored = _decimal(call.get("total_cost"))
                if abs(calculated["total"] - stored) > runtime.costs.quantum:
                    raise RuntimeError(
                        f"Local/stored cost mismatch for call {call['call_id']}: "
                        f"local={_money(calculated['total'])} stored={_money(stored)}"
                    )
                counters["input"] += int(call.get("input_tokens") or 0)
                counters["output"] += int(call.get("output_tokens") or 0)
                counters["total"] += int(call.get("total_tokens") or 0)
                after_call = await snapshot(runtime.costs)
                accumulated = _decimal(after_call["workflow_summary"]["real_cost"])
                budget_usage = after_call["budget_usage"]
                decision = str((call.get("attributes") or {}).get("budget_decision") or "allow")
                _emit_call(output, call_number, call, calculated, accumulated, budget_usage, decision)
                output_text = str(getattr(response, "output_text", "") or "")
                print(json.dumps({
                    "OutputLength": len(output_text),
                    "OutputSha256": hashlib.sha256(output_text.encode()).hexdigest(),
                }), file=output)
            except BudgetExceededError:
                counters["blocked"] += 1
                stop_reason = "budget_blocked"
                print("ProviderCalled = false\nStopReason = budget_blocked", file=output)
                break
            except Exception as exc:
                counters["failed"] += 1
                stop_reason = "provider_failed"
                print(f"ProviderCalled = true\nStopReason = provider_failed\nErrorType = {type(exc).__name__}", file=output)
                break
    finally:
        await runtime.observability.finalize_workflow_trace(
            WORKFLOW_ID, "completed" if not counters["failed"] else "failed",
            branch_id=BRANCH_ID, reason=f"{OPERATION}:{stop_reason}",
        )

    after = await snapshot(runtime.costs)
    accumulated = _decimal(after["workflow_summary"]["real_cost"])
    budget_usage = after["budget_usage"]
    summary = {
        "provider": PROVIDER,
        "model": MODEL,
        "workflow_id": WORKFLOW_ID,
        "provider_calls": counters["calls"],
        "provider_failed_calls": counters["failed"],
        "input_tokens": counters["input"],
        "output_tokens": counters["output"],
        "total_tokens": counters["total"],
        "real_cost_usd": _money(accumulated),
        "budget_consumed": budget_usage["consumed"],
        "budget_remaining": budget_usage["remaining"],
        "blocked_calls": counters["blocked"],
        "target_reached": TARGET_MIN_USD <= accumulated <= TARGET_MAX_USD,
        "stop_reason": stop_reason,
        "pricing_id": PRICING_ID,
        "usage_source": "provider_reported" if counters["calls"] else "unavailable",
    }
    print(json.dumps({
        "after": after,
        "delta": {
            "real_cost_usd": _money(
                accumulated - _decimal(before["workflow_summary"]["real_cost"])
            ),
            "budget_consumed": _money(
                _decimal(budget_usage["consumed"]) - _decimal(before["budget_usage"]["consumed"])
            ),
        },
    }, indent=2, default=str), file=output)
    print(json.dumps(summary, indent=2), file=output)
    return summary


def require_confirmation(args: argparse.Namespace, *, input_fn: Callable[[str], str] = input) -> bool:
    if not args.execute:
        return False
    if args.yes_i_understand_real_cost:
        return True
    if not sys.stdin.isatty():
        raise RuntimeError("--execute in non-interactive mode requires --yes-i-understand-real-cost")
    print("About to spend up to USD 0.95 using real OpenAI API usage.")
    entered = input_fn(f"Type EXACTLY:\n\n{CONFIRMATION}\n\n> ")
    if entered != CONFIRMATION:
        raise RuntimeError("Confirmation did not match; no provider call was made")
    return True


async def async_main(args: argparse.Namespace) -> int:
    confirmed = require_confirmation(args)
    runtime = await build_runtime(args.database, execute=args.execute)
    settings = SpendSettings(
        target_usd=args.target_usd,
        max_output_tokens=args.max_output_tokens,
        max_calls=args.max_calls,
        execute=args.execute,
        confirmed=confirmed,
    )
    await validate_spend(runtime, settings)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(async_main(args))
    except (RuntimeError, ValueError) as exc:
        print(f"Validation aborted: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
