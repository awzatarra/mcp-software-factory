from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
import math
import sqlite3
import os
from pathlib import Path
from typing import Any

from api.llm_cost_models import CostSource, CostStatus, EnforcementMode, UsageSource
from api.services.llm_cost_store import LLMCostStore
from api.services.llm_usage_normalizer import normalize_llm_usage
from api.services.observability_store import ObservabilityStore


logger = logging.getLogger(__name__)
CALCULATION_VERSION = "1.0"
ENFORCEMENT_PRECEDENCE = {
    EnforcementMode.OBSERVE_ONLY.value: 0,
    EnforcementMode.WARN.value: 1,
    EnforcementMode.SOFT_LIMIT.value: 2,
    EnforcementMode.HARD_LIMIT.value: 3,
}
DECISION_PRECEDENCE = {"allow": 0, "warn": 1, "block": 2}


class BudgetExceededError(RuntimeError):
    code = "budget_exceeded"


class BudgetUnavailableError(RuntimeError):
    code = "budget_unavailable"


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _money(value: Decimal | None, quantum: Decimal) -> Decimal | None:
    return None if value is None else value.quantize(quantum, rounding=ROUND_HALF_UP)


def _sum(values: list[Decimal | None]) -> Decimal:
    return sum((value for value in values if value is not None), Decimal(0))


def _stronger_decision(current: str, candidate: str) -> str:
    return candidate if DECISION_PRECEDENCE[candidate] > DECISION_PRECEDENCE[current] else current


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _budget_period_active(budget: dict[str, Any], now: datetime) -> bool:
    def parse(value: Any) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    try:
        start, end = parse(budget.get("period_start")), parse(budget.get("period_end"))
    except (TypeError, ValueError):
        return False
    return not ((start and now < start) or (end and now >= end))


class LLMCostService:
    def __init__(
        self,
        store: LLMCostStore,
        observability_store: ObservabilityStore,
    ) -> None:
        self.store = store
        self.observability_store = observability_store
        self.enabled = os.getenv("LLM_COSTS_ENABLED", "true").casefold() == "true"
        self.base_currency = os.getenv("LLM_COSTS_BASE_CURRENCY", "USD").upper()
        places = max(6, min(int(os.getenv("LLM_COSTS_DECIMAL_PLACES", "12")), 18))
        self.quantum = Decimal(1).scaleb(-places)
        self.auto_calculate = os.getenv("LLM_COSTS_AUTO_CALCULATE", "true").casefold() == "true"
        self.budgets_enabled = os.getenv("LLM_BUDGETS_ENABLED", "true").casefold() == "true"
        self.fail_mode = os.getenv("LLM_BUDGET_FAIL_MODE", "open").casefold()
        self.reservation_ttl = max(1, int(os.getenv("LLM_BUDGET_RESERVATION_TTL_SECONDS", "300")))
        self.alerts = None
        self.finops_lifecycle = None

    def set_alert_service(self, alerts: Any) -> None:
        self.alerts = alerts

    def set_finops_lifecycle(self, lifecycle: Any) -> None:
        self.finops_lifecycle = lifecycle

    async def _emit_alert(self, rule_code: str, *, workflow_id: str | None, branch_id: str,
                          severity: str, title: str, message: str, actual: Any,
                          threshold: Any, dimension: str,
                          evidence_context: dict[str, Any] | None = None,
                          project_name: str | None = None) -> None:
        enabled = os.getenv("LLM_COSTS_ALERTS_ENABLED", "true").casefold() == "true"
        if self.alerts is None or not workflow_id or not enabled:
            return
        try:
            await self.alerts.record_external_condition(
                rule_code=rule_code, thread_id=workflow_id, branch_id=branch_id,
                severity=severity, title=title, message=message,
                actual_value=actual, threshold_value=threshold, dimension=dimension,
                evidence_context=evidence_context, project_name=project_name,
            )
        except Exception:
            logger.exception("LLM cost alert emission failed rule=%s", rule_code)

    async def _clear_alert(self, rule_code: str, *, workflow_id: str | None,
                           branch_id: str, dimension: str) -> None:
        if self.alerts is None or not workflow_id:
            return
        try:
            await self.alerts.clear_external_condition(
                rule_code=rule_code, thread_id=workflow_id,
                branch_id=branch_id, dimension=dimension,
            )
        except Exception:
            logger.exception("LLM cost alert resolution failed rule=%s", rule_code)

    async def initialize(self) -> None:
        await self.store.initialize()

    async def _call(self, llm_call_id: str) -> dict[str, Any] | None:
        return await self.observability_store.fetch_one(
            "SELECT * FROM observability_llm_calls WHERE call_id=?", (llm_call_id,)
        )

    async def release_reservations(self, llm_call_id: str, *, reason: str) -> int:
        rows = await self.observability_store.fetch_all(
            "SELECT DISTINCT budget_id,workflow_id,branch_id FROM llm_budget_reservations WHERE llm_call_id=? AND status='reserved'",
            (llm_call_id,),
        )
        released = await asyncio.to_thread(self.store.release_reservations_sync, llm_call_id, reason)
        if self.finops_lifecycle is not None:
            for row in rows:
                await self.finops_lifecycle.reevaluate_budget_alerts(
                    row["budget_id"], workflow_id=row.get("workflow_id"),
                    branch_id=row.get("branch_id") or "original", reason=reason,
                )
        return released

    async def expire_reservations(self, *, reason: str) -> dict[str, Any]:
        result = await asyncio.to_thread(self.store.expire_reservations_sync)
        if self.finops_lifecycle is not None:
            for budget_id in result["budget_ids"]:
                await self.finops_lifecycle.reevaluate_budget_alerts(budget_id, reason=reason)
        return result

    async def calculate_call(
        self,
        llm_call_id: str,
        *,
        recalculate: bool = False,
        reason: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        existing = await self.store.get_calculation(llm_call_id)
        if existing and not recalculate:
            consumed = _decimal(existing.get("total_cost")) or _decimal(
                existing.get("estimated_total_cost")
            )
            await asyncio.to_thread(
                self.store.finalize_reservations_sync, llm_call_id, consumed
            )
            return existing
        call = await self._call(llm_call_id)
        if call is None:
            return None
        source_value = call.get("usage_source") or "unavailable"
        try:
            usage_source = UsageSource(source_value)
        except ValueError:
            usage_source = UsageSource.IMPORTED
        usage = normalize_llm_usage(
            call, usage_source=usage_source,
            provider_request_id=call.get("provider_request_id"),
            provider=call.get("provider"),
            total_includes_input_output=(call.get("provider") == "openai"),
        )
        common = {
            "llm_call_id": llm_call_id, "trace_id": call.get("trace_id"),
            "workflow_id": call.get("workflow_id"), "branch_id": call.get("branch_id") or "original",
            "agent_name": call.get("agent"), "provider": call.get("provider"), "model": call.get("model"),
            "operation": call.get("operation"), "usage_source": usage.usage_source.value,
            "prompt_tokens": usage.prompt_tokens, "cached_input_tokens": usage.cached_input_tokens,
            "uncached_input_tokens": usage.uncached_input_tokens, "completion_tokens": usage.completion_tokens,
            "reasoning_tokens": usage.reasoning_tokens, "calculation_version": CALCULATION_VERSION,
            "metadata": {"call_status": call.get("status"), "retry_attempt": call.get("retry_attempt", 0)},
        }
        warnings: list[str] = []
        if usage.usage_invalid:
            result = {**common, "cost_source": CostSource.UNAVAILABLE.value, "cost_status": CostStatus.UNAVAILABLE.value,
                      "currency": self.base_currency, "warnings": [usage.validation_error or "invalid_usage"],
                      "metadata": {**common["metadata"], "reason": "invalid_usage"}}
        elif not usage.usage_available:
            result = {**common, "cost_source": CostSource.UNAVAILABLE.value, "cost_status": CostStatus.UNAVAILABLE.value,
                      "currency": self.base_currency, "warnings": ["usage_not_available"],
                      "metadata": {**common["metadata"], "reason": "usage_not_available"}}
        else:
            pricing = await self.store.resolve_pricing(
                str(call.get("provider") or "unknown"), str(call.get("model") or "unknown"),
                str(call.get("timestamp")), self.base_currency,
            )
            if pricing is None:
                result = {**common, "cost_source": CostSource.UNAVAILABLE.value, "cost_status": CostStatus.UNAVAILABLE.value,
                          "currency": self.base_currency, "warnings": ["pricing_not_found"],
                          "metadata": {**common["metadata"], "reason": "pricing_not_found"}}
            else:
                million = Decimal(1_000_000)
                uncached = Decimal(usage.uncached_input_tokens or 0)
                cached = Decimal(usage.cached_input_tokens or 0)
                completion = Decimal(usage.completion_tokens or 0)
                reasoning = Decimal(usage.reasoning_tokens or 0)
                audio_input = Decimal(usage.audio_input_tokens or 0)
                audio_output = Decimal(usage.audio_output_tokens or 0)
                input_price = _decimal(pricing.get("input_price_per_million"))
                cached_price = _decimal(pricing.get("cached_input_price_per_million"))
                output_price = _decimal(pricing.get("output_price_per_million"))
                reasoning_price = _decimal(pricing.get("reasoning_price_per_million"))
                audio_input_price = _decimal(pricing.get("audio_input_price_per_million"))
                audio_output_price = _decimal(pricing.get("audio_output_price_per_million"))
                input_cost = uncached * input_price / million if input_price is not None else None
                cached_cost = cached * cached_price / million if cached_price is not None else None
                output_billable = completion
                reasoning_cost = None
                if reasoning and reasoning_price is not None:
                    if pricing.get("reasoning_in_completion", True):
                        warnings.append("reasoning_included_in_completion")
                    else:
                        output_billable = max(completion - reasoning, Decimal(0))
                        reasoning_cost = reasoning * reasoning_price / million
                output_cost = output_billable * output_price / million if output_price is not None else None
                audio_input_cost = audio_input * audio_input_price / million if audio_input_price is not None else None
                audio_output_cost = audio_output * audio_output_price / million if audio_output_price is not None else None
                if usage.prompt_tokens is not None and input_price is None:
                    warnings.append("input_price_unavailable")
                if usage.completion_tokens is not None and output_price is None:
                    warnings.append("output_price_unavailable")
                if audio_input and audio_input_price is None:
                    warnings.append("audio_input_price_unavailable")
                if audio_output and audio_output_price is None:
                    warnings.append("audio_output_price_unavailable")
                if reasoning and not pricing.get("reasoning_in_completion", True) and reasoning_price is None:
                    warnings.append("reasoning_price_unavailable")
                baseline = cached * input_price / million if cached and input_price is not None else None
                savings = max(baseline - cached_cost, Decimal(0)) if baseline is not None and cached_cost is not None else None
                other_cost = _sum([audio_input_cost, audio_output_cost])
                components = [input_cost, cached_cost, output_cost, reasoning_cost, other_cost]
                total = _sum(components)
                required_pricing_missing = any(warning.endswith("_price_unavailable") for warning in warnings)
                source = (CostSource.UNAVAILABLE if required_pricing_missing else
                          CostSource.ESTIMATED if usage.usage_source == UsageSource.TOKENIZER_ESTIMATED else
                          CostSource.CALCULATED)
                result = {
                    **common, "pricing_id": pricing["pricing_id"], "currency": pricing["currency"],
                    "cost_source": source.value,
                    "cost_status": (CostStatus.INVALID_PRICING.value if source == CostSource.UNAVAILABLE else
                                    CostStatus.ESTIMATED.value if source == CostSource.ESTIMATED else CostStatus.CALCULATED.value),
                    "input_cost": _money(input_cost, self.quantum), "cached_input_cost": _money(cached_cost, self.quantum),
                    "output_cost": _money(output_cost, self.quantum), "reasoning_cost": _money(reasoning_cost, self.quantum),
                    "other_cost": _money(other_cost, self.quantum),
                    "total_cost": _money(total, self.quantum) if source == CostSource.CALCULATED else None,
                    "estimated_total_cost": _money(total, self.quantum) if source == CostSource.ESTIMATED else None,
                    "cache_savings": _money(savings, self.quantum), "pricing_effective_from": pricing["effective_from"],
                    "pricing_snapshot": {key: pricing.get(key) for key in (
                        "pricing_id", "provider", "model_pattern", "model_canonical_name", "currency",
                        "input_price_per_million", "cached_input_price_per_million", "output_price_per_million",
                        "reasoning_price_per_million", "effective_from", "effective_to", "source_type",
                        "audio_input_price_per_million", "audio_output_price_per_million", "image_pricing",
                        "source_verified_at", "reasoning_in_completion",
                    )}, "warnings": warnings,
                }
        saved = await self.store.save_calculation(result, recalculate=recalculate, reason=reason, actor=actor)
        if "pricing_not_found" in saved.get("warnings", []):
            await self._emit_alert(
                "LLM_PRICING_MISSING", workflow_id=call.get("workflow_id"),
                branch_id=call.get("branch_id") or "original", severity="warning",
                title="LLM pricing missing",
                message="Usage was recorded but no effective pricing version was found.",
                actual=f"{call.get('provider')}/{call.get('model')}", threshold="effective pricing",
                dimension=f"{call.get('provider')}:{call.get('model')}",
                evidence_context={
                    "provider": str(call.get("provider") or "unknown"),
                    "model": str(call.get("model") or "unknown"),
                    "llm_call_id": llm_call_id,
                },
            )
        else:
            await self._clear_alert(
                "LLM_PRICING_MISSING", workflow_id=call.get("workflow_id"),
                branch_id=call.get("branch_id") or "original",
                dimension=f"{call.get('provider')}:{call.get('model')}",
            )
        if "usage_not_available" in saved.get("warnings", []):
            await self._emit_alert(
                "LLM_USAGE_UNAVAILABLE", workflow_id=call.get("workflow_id"),
                branch_id=call.get("branch_id") or "original", severity="warning",
                title="LLM usage unavailable",
                message="The provider response did not include usable token data.",
                actual="unavailable", threshold="provider_reported",
                dimension=llm_call_id,
                evidence_context={
                    "llm_call_id": llm_call_id,
                    "agent_name": str(call.get("agent") or "unknown"),
                    "provider": str(call.get("provider") or "unknown"),
                    "model": str(call.get("model") or "unknown"),
                },
            )
        else:
            await self._clear_alert(
                "LLM_USAGE_UNAVAILABLE", workflow_id=call.get("workflow_id"),
                branch_id=call.get("branch_id") or "original",
                dimension=llm_call_id,
            )
        current_cost = _decimal(saved.get("total_cost"))
        if current_cost is not None:
            minimum = max(1, int(os.getenv("LLM_COSTS_SPIKE_MIN_SAMPLES", "10")))
            multiplier = Decimal(os.getenv("LLM_COSTS_SPIKE_MULTIPLIER", "3"))
            history = await self.observability_store.fetch_all(
                """SELECT total_cost FROM llm_cost_calculations
                   WHERE superseded=0 AND total_cost IS NOT NULL AND llm_call_id!=?
                   AND provider=? AND model=? ORDER BY calculated_at DESC LIMIT ?""",
                (llm_call_id, call.get("provider"), call.get("model"), minimum),
            )
            historical = [_decimal(item.get("total_cost")) for item in history]
            historical = [item for item in historical if item is not None]
            if len(historical) >= minimum:
                average = _sum(historical) / Decimal(len(historical))
                if average > 0 and current_cost > average * multiplier:
                    await self._emit_alert(
                        "LLM_COST_SPIKE", workflow_id=call.get("workflow_id"),
                        branch_id=call.get("branch_id") or "original", severity="warning",
                        title="LLM cost spike", message="Call cost exceeded the configured historical multiplier.",
                        actual=format(current_cost,"f"), threshold=format(average * multiplier,"f"),
                        dimension=f"{call.get('provider')}:{call.get('model')}",
                    )
            if int(call.get("retry_attempt") or 0) > 0 and call.get("workflow_id"):
                workflow_costs = await self.observability_store.fetch_all(
                    """SELECT x.total_cost,c.retry_attempt FROM llm_cost_calculations x
                       JOIN observability_llm_calls c ON c.call_id=x.llm_call_id
                       WHERE x.superseded=0 AND x.total_cost IS NOT NULL
                       AND x.workflow_id=? AND x.branch_id=?""",
                    (call.get("workflow_id"), call.get("branch_id") or "original"),
                )
                total = _sum([_decimal(item.get("total_cost")) for item in workflow_costs])
                retry = _sum([_decimal(item.get("total_cost")) for item in workflow_costs if int(item.get("retry_attempt") or 0)>0])
                ratio = retry / total if total > 0 else Decimal(0)
                ratio_threshold = Decimal(os.getenv("LLM_RETRY_COST_WARNING_RATIO", "0.25"))
                if ratio >= ratio_threshold:
                    await self._emit_alert(
                        "HIGH_RETRY_COST", workflow_id=call.get("workflow_id"),
                        branch_id=call.get("branch_id") or "original", severity="warning",
                        title="High LLM retry cost", message="Retry cost reached the configured workflow ratio.",
                        actual=format(ratio,"f"), threshold=format(ratio_threshold,"f"),
                        dimension=str(call.get("agent") or "workflow"),
                    )
        reservation_rows = await self.observability_store.fetch_all(
            "SELECT DISTINCT budget_id FROM llm_budget_reservations WHERE llm_call_id=?",
            (llm_call_id,),
        )
        consumed = _decimal(saved.get("total_cost")) or _decimal(saved.get("estimated_total_cost"))
        await asyncio.to_thread(
            self.store.finalize_reservations_sync, llm_call_id, consumed
        )
        for row in reservation_rows:
            budget = await self.store.get_budget(row["budget_id"])
            usage = await self.store.budget_usage(row["budget_id"])
            if budget is None or usage is None or consumed is None:
                continue
            total_usage = Decimal(usage["consumed"]) + Decimal(usage["reserved"])
            limit = Decimal(budget["limit_amount"])
            warning = limit * Decimal(budget["warning_percent"]) / Decimal(100)
            from api.services.llm_finops_alert_lifecycle import budget_alert_dimension, budget_period_key
            dimension = budget_alert_dimension(
                budget, agent_name=call.get("agent"), model=call.get("model")
            )
            evidence = {
                "budget_id": row["budget_id"], "period_key": budget_period_key(budget),
                "agent_name": call.get("agent"), "model": call.get("model"),
                "llm_call_id": llm_call_id,
            }
            if total_usage >= warning:
                await self._emit_alert(
                    "LLM_BUDGET_WARNING", workflow_id=call.get("workflow_id"),
                    branch_id=call.get("branch_id") or "original", severity="warning",
                    title="LLM budget warning",
                    message="Actual LLM consumption reached the configured budget warning threshold.",
                    actual=format(total_usage,"f"), threshold=format(warning,"f"),
                    dimension=dimension, evidence_context=evidence,
                )
            if total_usage > limit:
                await self._emit_alert(
                    "LLM_BUDGET_EXCEEDED", workflow_id=call.get("workflow_id"),
                    branch_id=call.get("branch_id") or "original", severity="error",
                    title="LLM budget exceeded",
                    message="Actual LLM consumption exceeded the configured budget limit.",
                    actual=format(total_usage,"f"), threshold=format(limit,"f"),
                    dimension=dimension, evidence_context=evidence,
                )
        if self.finops_lifecycle is not None:
            for row in reservation_rows:
                await self.finops_lifecycle.reevaluate_budget_alerts(
                    row["budget_id"], workflow_id=call.get("workflow_id"),
                    branch_id=call.get("branch_id") or "original",
                    reason="Budget reservation consumed or released",
                )
            await self.finops_lifecycle.reevaluate_pricing_alerts(reason="Valid pricing is now available")
            await self.finops_lifecycle.reevaluate_usage_alerts(reason="Provider usage is now available")
        await self.observability_store.add_log({
            "trace_id": call.get("trace_id"), "span_id": call.get("span_id"), "workflow_id": call.get("workflow_id"),
            "level": "info", "message": "llm_cost_calculated" if consumed is not None else "llm_cost_unavailable",
            "attributes": {"llm_call_id": llm_call_id, "cost_source": saved.get("cost_source"), "reason_codes": saved.get("warnings", [])},
        })
        metric_timestamp = datetime.now(UTC).isoformat()
        metric_attributes = {
            "provider": call.get("provider"), "model": call.get("model"),
            "agent": call.get("agent"), "cost_source": saved.get("cost_source"),
        }
        metrics = [
            ("llm.calls.with_cost", "1", "call") if consumed is not None else
            ("llm.calls.without_cost", "1", "call"),
        ]
        if saved.get("total_cost") is not None:
            metrics.append(("llm.cost.total", saved["total_cost"], saved.get("currency") or self.base_currency))
        if saved.get("estimated_total_cost") is not None:
            metrics.append(("llm.estimated_cost.total", saved["estimated_total_cost"], saved.get("currency") or self.base_currency))
        if saved.get("cache_savings") is not None:
            metrics.append(("llm.cache_savings.total", saved["cache_savings"], saved.get("currency") or self.base_currency))
        for name, value, unit in metrics:
            await self.observability_store.add_metric({
                "trace_id": call.get("trace_id"), "workflow_id": call.get("workflow_id"),
                "name": name, "value": value, "unit": unit,
                "timestamp": metric_timestamp, "attributes": metric_attributes,
            })
        return saved

    async def record_and_calculate(self, llm_call_id: str) -> None:
        if self.auto_calculate:
            try:
                await self.calculate_call(llm_call_id)
            except Exception:
                logger.exception("LLM cost calculation failed call=%s", llm_call_id)

    async def _project_name(self, workflow_id: str | None) -> str | None:
        if not workflow_id:
            return None
        try:
            row = await self.observability_store.fetch_one(
                "SELECT project_name FROM workflow_registry WHERE thread_id=?", (workflow_id,)
            )
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return None
        return str(row["project_name"]) if row and row.get("project_name") else None

    @staticmethod
    def estimate_tokens(kwargs: dict[str, Any]) -> tuple[int | None, int | None]:
        payload = kwargs.get("input")
        if payload is None:
            return None, kwargs.get("max_output_tokens")
        try:
            length = len(json.dumps(payload, ensure_ascii=True, default=str))
            return max(math.ceil(length / 4), 1), kwargs.get("max_output_tokens") or 1024
        except Exception:
            return None, kwargs.get("max_output_tokens")

    async def preflight(
        self,
        *,
        call_id: str,
        workflow_id: str | None,
        branch_id: str,
        agent_name: str | None,
        provider: str,
        model: str,
        kwargs: dict[str, Any],
        estimated_amount: Decimal | None = None,
        currency: str | None = None,
        project_name: str | None = None,
        raise_on_block: bool = True,
    ) -> dict[str, Any]:
        if not self.enabled or not self.budgets_enabled:
            return {"decision": "allow", "reason_codes": ["budgets_disabled"], "reservations": [], "applicable_budgets": [], "warning_budgets": [], "blocking_budgets": []}
        try:
            now = datetime.now(UTC)
            selected_currency = (currency or self.base_currency).upper()
            input_tokens, output_tokens = self.estimate_tokens(kwargs)
            pricing = await self.store.resolve_pricing(provider, model, now.isoformat(), selected_currency)
            estimated: Decimal | None = estimated_amount
            if estimated is None and pricing and input_tokens is not None and output_tokens is not None:
                input_price = _decimal(pricing.get("input_price_per_million"))
                output_price = _decimal(pricing.get("output_price_per_million"))
                if input_price is not None and output_price is not None:
                    estimated = (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price) / Decimal(1_000_000)
                    estimated = _money(estimated, self.quantum)
            budgets = await self.store.list_budgets(enabled=True)
            resolved_project_name = project_name or await self._project_name(workflow_id)
            applicable=[]
            for budget in budgets:
                scope=budget["scope_type"];value=budget.get("scope_value")
                matches = scope in {"global","daily","monthly"} or (
                    (scope=="workflow" and value==workflow_id) or
                    (scope=="branch" and value==branch_id) or
                    (scope=="agent" and value==agent_name) or
                    (scope=="model" and value==model) or
                    (scope=="project" and value==resolved_project_name)
                )
                if matches and budget["currency"] == selected_currency and _budget_period_active(budget, now):
                    applicable.append(budget)
            applicable.sort(key=lambda item: ENFORCEMENT_PRECEDENCE.get(item["enforcement_mode"], -1), reverse=True)
            decision="allow"; reasons=[]; reservations=[]; warning_budgets=[]; blocking_budgets=[]; budget_decisions=[]
            for budget in applicable:
                usage=await self.store.budget_usage(budget["budget_id"]); remaining=Decimal(usage["remaining"])
                warning=Decimal(budget["limit_amount"])*Decimal(budget["warning_percent"])/Decimal(100)
                consumed=Decimal(usage["consumed"]); reserved=Decimal(usage["reserved"])
                projected=consumed+reserved+(estimated or Decimal(0)); limit=Decimal(budget["limit_amount"])
                mode=budget["enforcement_mode"]
                local_decision="allow"; local_reasons=[]
                if projected >= warning:
                    local_decision="warn"; _append_unique(local_reasons,"budget_warning")
                    _append_unique(warning_budgets,budget["budget_id"])
                exhausted_before_call = consumed + reserved >= limit
                if mode==EnforcementMode.SOFT_LIMIT.value and exhausted_before_call:
                    local_decision="block"; _append_unique(local_reasons,"budget_exceeded"); _append_unique(local_reasons,"soft_limit")
                    _append_unique(blocking_budgets,budget["budget_id"])
                if mode==EnforcementMode.HARD_LIMIT.value and exhausted_before_call:
                    local_decision="block"; _append_unique(local_reasons,"budget_exceeded"); _append_unique(local_reasons,"hard_limit")
                    _append_unique(blocking_budgets,budget["budget_id"])
                if estimated is not None:
                    reservation=await asyncio.to_thread(self.store.reserve_sync,budget,call_id=call_id,workflow_id=workflow_id,branch_id=branch_id,agent_name=agent_name,amount=estimated,currency=selected_currency,ttl_seconds=self.reservation_ttl)
                    reservations.append(reservation["reservation_id"])
                    if reservation["status"]=="rejected":
                        _append_unique(local_reasons,"reservation_exceeds_budget")
                        if mode==EnforcementMode.HARD_LIMIT.value:
                            local_decision="block"; _append_unique(local_reasons,"budget_exceeded"); _append_unique(local_reasons,"hard_limit")
                            _append_unique(blocking_budgets,budget["budget_id"])
                        elif mode==EnforcementMode.SOFT_LIMIT.value:
                            local_decision=_stronger_decision(local_decision,"warn"); _append_unique(local_reasons,"soft_limit_current_call_allowed")
                            _append_unique(warning_budgets,budget["budget_id"])
                        elif mode==EnforcementMode.WARN.value:
                            local_decision=_stronger_decision(local_decision,"warn"); _append_unique(local_reasons,"warn_limit")
                            _append_unique(warning_budgets,budget["budget_id"])
                        elif mode==EnforcementMode.OBSERVE_ONLY.value:
                            local_decision=_stronger_decision(local_decision,"warn"); _append_unique(local_reasons,"observe_only")
                            _append_unique(warning_budgets,budget["budget_id"])
                decision=_stronger_decision(decision,local_decision)
                for reason in local_reasons:_append_unique(reasons,reason)
                budget_decisions.append({"budget_id":budget["budget_id"],"enforcement_mode":mode,"decision":local_decision,"reason_codes":local_reasons})
                await asyncio.to_thread(self.store.add_budget_event_sync,{"budget_id":budget["budget_id"],"workflow_id":workflow_id,"branch_id":branch_id,"llm_call_id":call_id,"event_type":"budget_checked","amount":estimated,"remaining_amount":remaining,"decision":local_decision,"reason_code":local_reasons[-1] if local_reasons else "within_budget"})
            if decision=="block":
                await self.release_reservations(call_id,reason="Aggregate budget decision blocked provider invocation")
                for budget_id in blocking_budgets:
                    await asyncio.to_thread(self.store.add_budget_event_sync,{"budget_id":budget_id,"workflow_id":workflow_id,"branch_id":branch_id,"llm_call_id":call_id,"event_type":"budget_call_blocked","amount":estimated,"decision":"block","reason_code":"budget_exceeded"})
                await self.observability_store.add_log({"workflow_id":workflow_id,"level":"warning","message":"budget_call_blocked","attributes":{"branch_id":branch_id,"agent_name":agent_name,"model":model,"blocking_budgets":blocking_budgets,"reason_codes":reasons}})
                from api.services.llm_finops_alert_lifecycle import budget_alert_dimension, budget_period_key
                for budget_id in blocking_budgets:
                    budget = next(item for item in applicable if item["budget_id"] == budget_id)
                    evidence = {
                        "budget_id": budget_id, "period_key": budget_period_key(budget),
                        "agent_name": agent_name, "model": model, "llm_call_id": call_id,
                        "estimated_amount": format(estimated,"f") if estimated is not None else None,
                    }
                    dimension = budget_alert_dimension(budget, agent_name=agent_name, model=model)
                    await self._emit_alert(
                        "LLM_BUDGET_EXCEEDED", workflow_id=workflow_id, branch_id=branch_id,
                        severity="error", title="LLM budget exceeded",
                        message="An applicable LLM budget could not reserve the estimated call cost.",
                        actual=format(estimated,"f") if estimated is not None else "unavailable",
                        threshold=budget["limit_amount"], dimension=dimension,
                        evidence_context=evidence, project_name=resolved_project_name,
                    )
                    if budget["enforcement_mode"] == EnforcementMode.HARD_LIMIT.value:
                        await self._emit_alert(
                            "LLM_CALL_BLOCKED", workflow_id=workflow_id, branch_id=branch_id,
                            severity="error", title="LLM call blocked by budget",
                            message="A hard budget policy blocked the call before provider invocation.",
                            actual=format(estimated,"f") if estimated is not None else "unavailable",
                            threshold=budget["limit_amount"], dimension=dimension,
                            evidence_context=evidence, project_name=resolved_project_name,
                        )
                if raise_on_block:
                    raise BudgetExceededError("LLM call blocked by budget policy")
            if decision=="warn":
                from api.services.llm_finops_alert_lifecycle import budget_alert_dimension, budget_period_key
                for budget_id in warning_budgets:
                    budget = next(item for item in applicable if item["budget_id"] == budget_id)
                    await self._emit_alert(
                        "LLM_BUDGET_WARNING", workflow_id=workflow_id, branch_id=branch_id,
                        severity="warning", title="LLM budget warning",
                        message="An applicable LLM budget reached its warning condition.",
                        actual=format(estimated,"f") if estimated is not None else "unavailable",
                        threshold=budget["warning_percent"],
                        dimension=budget_alert_dimension(budget, agent_name=agent_name, model=model),
                        evidence_context={
                            "budget_id": budget_id, "period_key": budget_period_key(budget),
                            "agent_name": agent_name, "model": model, "llm_call_id": call_id,
                            "estimated_amount": format(estimated,"f") if estimated is not None else None,
                        }, project_name=resolved_project_name,
                    )
            elif decision=="allow":
                if self.finops_lifecycle is not None:
                    for budget in applicable:
                        await self.finops_lifecycle.reevaluate_budget_alerts(
                            budget["budget_id"], workflow_id=workflow_id, branch_id=branch_id,
                            reason="Budget check returned within policy",
                        )
            return {"decision":decision,"reason_codes":reasons or ["within_budget"],"estimated_input_tokens":input_tokens,"estimated_output_tokens":output_tokens,"estimated_max_cost":format(estimated,"f") if estimated is not None else None,"applicable_budgets":[b["budget_id"] for b in applicable],"warning_budgets":warning_budgets,"blocking_budgets":blocking_budgets,"budget_decisions":budget_decisions,"reservations":reservations,"currency":selected_currency}
        except BudgetExceededError:
            raise
        except Exception as exc:
            logger.exception("budget guard degraded")
            if self.fail_mode == "closed":
                raise BudgetUnavailableError("Budget guard is unavailable") from exc
            return {"decision":"allow","reason_codes":["budget_guard_degraded"],"reservations":[]}

    async def list_calls(self, *, limit=100, offset=0, sort_by="timestamp", sort_order="desc", **filters):
        clauses=[];params=[]
        registry = await self.observability_store.fetch_one(
            "SELECT 1 AS present FROM sqlite_master WHERE type='table' AND name='workflow_registry'"
        )
        registry_join = " LEFT JOIN workflow_registry r ON r.thread_id=c.workflow_id" if registry else ""
        allowed={"workflow_id":"c.workflow_id","branch_id":"c.branch_id","trace_id":"c.trace_id","agent_name":"c.agent","provider":"c.provider","model":"c.model","status":"c.status","cost_source":"x.cost_source","project_name":"r.project_name"}
        for key,column in allowed.items():
            value=filters.get(key)
            if value is not None:
                if key == "project_name" and not registry:
                    clauses.append("1=0")
                else:
                    clauses.append(f"{column}=?");params.append(value)
        if filters.get("date_from"): clauses.append("c.timestamp>=?"); params.append(filters["date_from"])
        if filters.get("date_to"): clauses.append("c.timestamp<?"); params.append(filters["date_to"])
        where=" WHERE "+" AND ".join(clauses) if clauses else ""
        sort_columns = {
            "timestamp": "c.timestamp", "duration_ms": "c.duration_ms",
            "total_tokens": "c.total_tokens", "model": "c.model", "status": "c.status",
            "total_cost": "CAST(COALESCE(x.total_cost,x.estimated_total_cost) AS NUMERIC)",
        }
        order_column = sort_columns.get(sort_by, "c.timestamp")
        order_direction = "ASC" if str(sort_order).casefold() == "asc" else "DESC"
        sql=f"""SELECT c.*,x.cost_calculation_id,x.currency,x.cost_source,x.cost_status,x.total_cost,
                x.estimated_total_cost,x.input_cost,x.cached_input_cost,x.output_cost,x.reasoning_cost,
                x.cache_savings,x.pricing_id,x.pricing_snapshot_json,x.warnings_json,x.calculated_at
                FROM observability_llm_calls c LEFT JOIN llm_cost_calculations x
                ON x.llm_call_id=c.call_id AND x.superseded=0
                {registry_join}{where}
                ORDER BY {order_column} {order_direction} LIMIT ? OFFSET ?"""
        items=await self.observability_store.fetch_all(sql,(*params,limit,offset))
        total=await self.observability_store.fetch_one(f"SELECT COUNT(*) AS total FROM observability_llm_calls c LEFT JOIN llm_cost_calculations x ON x.llm_call_id=c.call_id AND x.superseded=0{registry_join}{where}",tuple(params))
        return {"items":items,"total":int(total["total"] if total else 0),"limit":limit,"offset":offset}

    async def call_detail(self,llm_call_id):
        call=await self.observability_store.fetch_one("SELECT * FROM observability_llm_calls WHERE call_id=?",(llm_call_id,))
        if not call:return None
        calculation=await self.store.get_calculation(llm_call_id)
        events=await self.observability_store.fetch_all("SELECT * FROM llm_budget_events WHERE llm_call_id=? ORDER BY created_at",(llm_call_id,))
        return {"call":call,"calculation":calculation,"budget_events":events}

    async def summary(self, **filters):
        calls=(await self.list_calls(limit=10000,offset=0,**filters))["items"]
        real=[_decimal(item.get("total_cost")) for item in calls]
        estimated=[_decimal(item.get("estimated_total_cost")) for item in calls]
        savings=[_decimal(item.get("cache_savings")) for item in calls]
        real_values=[v for v in real if v is not None]
        sorted_costs=sorted(real_values)
        def percentile(p):
            if not sorted_costs:return None
            return format(sorted_costs[min(math.ceil(len(sorted_costs)*p)-1,len(sorted_costs)-1)],"f")
        total_tokens=sum(int(item.get("total_tokens") or 0) for item in calls)
        cached_tokens=sum(int(item.get("cached_tokens") or 0) for item in calls)
        state=("unavailable" if not calls else "available" if len(real_values)==len(calls) else "partial")
        budget_limit=budget_consumed=budget_reserved=budget_remaining=None;budget_status="unavailable"
        workflow_id=filters.get("workflow_id")
        if workflow_id:
            applicable=[b for b in await self.store.list_budgets(enabled=True) if b["scope_type"]=="workflow" and b.get("scope_value")==workflow_id]
            if applicable:
                usages=[await self.store.budget_usage(b["budget_id"]) for b in applicable]
                strict=min((u for u in usages if u),key=lambda u:Decimal(u["remaining"]),default=None)
                if strict:
                    budget_limit=strict["budget"]["limit_amount"];budget_consumed=strict["consumed"];budget_reserved=strict["reserved"];budget_remaining=strict["remaining"]
                    percent=Decimal(strict["percent"]);warning=Decimal(strict["budget"]["warning_percent"])
                    budget_status="exceeded" if Decimal(strict["remaining"])<=0 else "warning" if percent>=warning else "within_budget"
        active_budgets=[b for b in await self.store.list_budgets(enabled=True) if _budget_period_active(b,datetime.now(UTC))]
        global_budgets=[b for b in active_budgets if b["scope_type"]=="global"]
        if not workflow_id and global_budgets:
            usages=[await self.store.budget_usage(b["budget_id"]) for b in global_budgets]
            strict=min((u for u in usages if u),key=lambda u:Decimal(u["remaining"]),default=None)
            if strict:
                budget_limit=strict["budget"]["limit_amount"];budget_consumed=strict["consumed"];budget_reserved=strict["reserved"];budget_remaining=strict["remaining"]
                percent=Decimal(strict["percent"]);warning=Decimal(strict["budget"]["warning_percent"])
                budget_status="exceeded" if Decimal(strict["remaining"])<=0 else "warning" if percent>=warning else "within_budget"
        blocked=await self.observability_store.fetch_one("SELECT COUNT(DISTINCT llm_call_id) AS total FROM llm_budget_events WHERE event_type='budget_call_blocked'")
        workflow_ids={str(item.get("workflow_id")) for item in calls if item.get("workflow_id")}
        completed=0
        if workflow_ids:
            placeholders=",".join("?" for _ in workflow_ids)
            registry=await self.observability_store.fetch_one("SELECT 1 AS present FROM sqlite_master WHERE type='table' AND name='workflow_registry'")
            if registry:
                row=await self.observability_store.fetch_one(f"SELECT COUNT(*) AS total FROM workflow_registry WHERE terminal_status='completed' AND thread_id IN ({placeholders})",tuple(workflow_ids))
                completed=int(row["total"] if row else 0)
        return {
            "state":state,
            "calls":len(calls),"calls_with_usage":sum(bool(item.get("usage_available")) for item in calls),
            "calls_without_usage":sum(not bool(item.get("usage_available")) for item in calls),
            "calls_with_cost":len(real_values),"calls_without_pricing":sum("pricing_not_found" in str(item.get("warnings")) for item in calls),
            "real_cost":format(_sum(real),"f"),"estimated_cost":format(_sum(estimated),"f"),
            "input_cost":format(_sum([_decimal(item.get("input_cost")) for item in calls]),"f"),
            "cached_input_cost":format(_sum([_decimal(item.get("cached_input_cost")) for item in calls]),"f"),
            "output_cost":format(_sum([_decimal(item.get("output_cost")) for item in calls]),"f"),
            "reasoning_cost":format(_sum([_decimal(item.get("reasoning_cost")) for item in calls]),"f"),
            "cache_savings":format(_sum(savings),"f"),
            "retry_cost":format(_sum([_decimal(item.get("total_cost")) for item in calls if int(item.get("retry_attempt") or 0)>0]),"f"),
            "failed_call_cost":format(_sum([_decimal(item.get("total_cost")) for item in calls if item.get("status")=="failed"]),"f"),
            "average_cost_per_call":format(_sum(real)/len(real_values),"f") if real_values else None,
            "average_cost_per_completed_workflow":format(_sum(real)/completed,"f") if completed else None,
            "p50":percentile(.5),"p90":percentile(.9),"p95":percentile(.95),
            "total_tokens":total_tokens,"cached_tokens":cached_tokens,
            "budget_status":budget_status,"budget_limit":budget_limit,"budget_consumed":budget_consumed,"budget_reserved":budget_reserved,"budget_remaining":budget_remaining,
            "active_budgets":len(active_budgets),"global_budget_applicable":bool(global_budgets),
            "blocked_calls":int(blocked["total"] if blocked else 0),"completed_workflows":completed,
            "latest_calculation_at":max((str(item.get("calculated_at") or "") for item in calls),default=None),
            "currency":self.base_currency,"calculated_at":datetime.now(UTC).isoformat(),
        }

    async def aggregate(self, dimension: str, **filters):
        calls=(await self.list_calls(limit=10000,offset=0,**filters))["items"]
        mapping={"agents":"agent","models":"model","providers":"provider","operations":"operation"};key=mapping[dimension]
        groups:dict[str,dict[str,Any]]={}
        for call in calls:
            name=str(call.get(key) or "unknown");group=groups.setdefault(name,{key:name,"calls":0,"tokens":0,"calls_with_usage":0,"calls_without_usage":0,"calls_with_pricing":0,"calls_without_pricing":0,"real_cost":Decimal(0),"estimated_cost":Decimal(0),"failed_call_cost":Decimal(0),"failures":0,"retries":0})
            group["calls"]+=1;group["tokens"]+=int(call.get("total_tokens") or 0);group["calls_with_usage"]+=bool(call.get("usage_available"));group["calls_without_usage"]+=not bool(call.get("usage_available"));group["calls_with_pricing"]+=call.get("total_cost") is not None or call.get("estimated_total_cost") is not None;group["calls_without_pricing"]+="pricing_not_found" in str(call.get("warnings"));group["real_cost"]+=_decimal(call.get("total_cost")) or 0;group["estimated_cost"]+=_decimal(call.get("estimated_total_cost")) or 0;group["failures"]+=call.get("status")=="failed";group["retries"]+=int(call.get("retry_attempt") or 0)>0
            if call.get("status")=="failed":group["failed_call_cost"]+=_decimal(call.get("total_cost")) or 0
        total_real=sum((item["real_cost"] for item in groups.values()),Decimal(0))
        return {"items":[{**item,"real_cost":format(item["real_cost"],"f"),"estimated_cost":format(item["estimated_cost"],"f"),"failed_call_cost":format(item["failed_call_cost"],"f"),"average_cost_per_call":format(item["real_cost"]/item["calls"],"f") if item["calls"] else None,"percentage_of_total_cost":format(item["real_cost"]/total_real*100,"f") if total_real else None} for item in groups.values()]}
