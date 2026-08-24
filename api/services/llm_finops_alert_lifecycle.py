from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from api.services.alert_store import AlertStore
from api.services.llm_cost_service import _budget_period_active


BUDGET_RULES = {
    "LLM_BUDGET_WARNING",
    "LLM_BUDGET_EXCEEDED",
    "LLM_CALL_BLOCKED",
}


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def budget_period_key(budget: dict[str, Any]) -> str:
    return "|".join((
        str(budget.get("period_type") or "unbounded"),
        str(budget.get("period_start") or ""),
        str(budget.get("period_end") or ""),
        str(budget.get("reset_timezone") or "UTC"),
    ))


def budget_alert_dimension(
    budget: dict[str, Any], *, agent_name: str | None, model: str | None,
) -> str:
    return "|".join((
        f"budget:{budget['budget_id']}",
        f"period:{budget_period_key(budget)}",
        f"agent:{agent_name or 'unknown'}",
        f"model:{model or 'unknown'}",
    ))


class LLMFinOpsAlertLifecycleService:
    def __init__(self, cost_store, observability_store, alerts) -> None:
        self.cost_store = cost_store
        self.observability_store = observability_store
        self.alerts = alerts

    async def _budget_workflow_keys(
        self, budget: dict[str, Any], workflow_id: str | None, branch_id: str | None,
    ) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        if workflow_id:
            keys.add((workflow_id, branch_id or "original"))
        if budget.get("scope_type") == "workflow" and budget.get("scope_value"):
            keys.add((str(budget["scope_value"]), branch_id or "original"))
        rows = await self.observability_store.fetch_all(
            """SELECT DISTINCT workflow_id,branch_id FROM llm_budget_events
               WHERE budget_id=? AND workflow_id IS NOT NULL""",
            (budget["budget_id"],),
        )
        keys.update((str(row["workflow_id"]), str(row.get("branch_id") or "original")) for row in rows)
        return keys

    async def _legacy_fingerprints(
        self, budget_id: str, rule_id: str, workflow_id: str, branch_id: str,
    ) -> set[str]:
        rows = await self.observability_store.fetch_all(
            """SELECT DISTINCT agent_name FROM llm_budget_reservations
               WHERE budget_id=? AND workflow_id=? AND branch_id=? AND agent_name IS NOT NULL""",
            (budget_id, workflow_id, branch_id),
        )
        return {
            AlertStore.fingerprint(rule_id, workflow_id, branch_id, str(row["agent_name"]))
            for row in rows
        }

    async def _budget_condition_active(self, budget: dict[str, Any], alert) -> bool:
        if not budget.get("enabled") or not _budget_period_active(budget, datetime.now(UTC)):
            return False
        if alert.current_evidence.period_key and alert.current_evidence.period_key != budget_period_key(budget):
            return False
        usage = await self.cost_store.budget_usage(budget["budget_id"])
        if usage is None:
            return False
        consumed = Decimal(usage["consumed"])
        reserved = Decimal(usage["reserved"])
        total = consumed + reserved
        limit = Decimal(budget["limit_amount"])
        if alert.rule_code == "LLM_BUDGET_WARNING":
            warning = limit * Decimal(budget["warning_percent"]) / Decimal(100)
            return total >= warning
        if alert.rule_code == "LLM_BUDGET_EXCEEDED":
            return total > limit
        if alert.rule_code == "LLM_CALL_BLOCKED":
            if budget.get("enforcement_mode") != "hard_limit":
                return False
            estimated = _decimal(alert.current_evidence.estimated_amount)
            if estimated is None:
                estimated = _decimal(alert.current_evidence.actual_value) or Decimal(0)
            return total + estimated > limit
        return False

    async def _legacy_condition_active(self, alert) -> bool:
        rows = await self.observability_store.fetch_all(
            """SELECT DISTINCT b.* FROM llm_budgets b
               JOIN llm_budget_events e ON e.budget_id=b.budget_id
               WHERE e.workflow_id=? AND e.branch_id=?""",
            (alert.thread_id, alert.branch_id),
        )
        for raw in rows:
            budget = dict(raw)
            for field in ("enabled", "include_estimated", "include_failed_calls", "include_retries"):
                if field in budget:
                    budget[field] = bool(budget[field])
            fingerprints = await self._legacy_fingerprints(
                budget["budget_id"], alert.rule_id, alert.thread_id, alert.branch_id,
            )
            if alert.fingerprint in fingerprints and await self._budget_condition_active(budget, alert):
                return True
        return False

    async def reevaluate_budget_alerts(
        self, budget_id: str, *, workflow_id: str | None = None,
        branch_id: str | None = None, reason: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        budget = await self.cost_store.get_budget(budget_id)
        result = {"budget_id": budget_id, "alerts_to_resolve": [], "alerts_resolved": 0}
        if budget is None:
            return result
        keys = await self._budget_workflow_keys(budget, workflow_id, branch_id)
        for thread_id, current_branch in keys:
            listed = await self.alerts.list_alerts(
                statuses=["open", "acknowledged", "muted"], thread_id=thread_id,
                branch_id=current_branch, limit=500, offset=0,
            )
            for alert in listed.items:
                if alert.rule_code not in BUDGET_RULES:
                    continue
                direct = alert.current_evidence.budget_id == budget_id
                legacy = alert.current_evidence.budget_id is None and alert.fingerprint in await self._legacy_fingerprints(
                    budget_id, alert.rule_id, thread_id, current_branch,
                )
                if not direct and not legacy:
                    continue
                active = (
                    await self._budget_condition_active(budget, alert)
                    if direct else await self._legacy_condition_active(alert)
                )
                if active:
                    continue
                result["alerts_to_resolve"].append(alert.alert_id)
                if not dry_run and await self.alerts.auto_resolve_external_alert(
                    alert.alert_id,
                    reason=reason or f"FinOps budget condition cleared for {budget_id}",
                ):
                    result["alerts_resolved"] += 1
        return result

    async def reevaluate_all_budget_alerts(self, *, reason: str, dry_run: bool = False) -> dict[str, Any]:
        alert_ids: list[str] = []
        resolved = 0
        for budget in await self.cost_store.list_budgets(enabled=None):
            result = await self.reevaluate_budget_alerts(
                budget["budget_id"], reason=reason, dry_run=dry_run,
            )
            for alert_id in result["alerts_to_resolve"]:
                if alert_id not in alert_ids:
                    alert_ids.append(alert_id)
            resolved += result["alerts_resolved"]
        return {"alerts_to_resolve": alert_ids, "alerts_resolved": resolved}

    async def reevaluate_pricing_alerts(self, *, reason: str) -> int:
        listed = await self.alerts.list_alerts(
            statuses=["open", "acknowledged", "muted"],
            rule_code="LLM_PRICING_MISSING", limit=10_000, offset=0,
        )
        resolved = 0
        for alert in listed.items:
            provider = alert.current_evidence.provider
            model = alert.current_evidence.model
            if not provider or not model:
                raw = str(alert.current_evidence.actual_value or "")
                provider, separator, model = raw.partition("/")
                if not separator:
                    continue
            calls = await self.observability_store.fetch_all(
                """SELECT timestamp FROM observability_llm_calls
                   WHERE workflow_id=? AND branch_id=? AND provider=? AND model=?
                   AND usage_available=1""",
                (alert.thread_id, alert.branch_id, provider, model),
            )
            pricing_matches = [
                await self.cost_store.resolve_pricing(provider, model, str(call["timestamp"]))
                for call in calls
            ]
            if calls and all(item is not None for item in pricing_matches):
                resolved += int(await self.alerts.auto_resolve_external_alert(alert.alert_id, reason=reason))
        return resolved

    async def reevaluate_usage_alerts(self, *, reason: str) -> int:
        listed = await self.alerts.list_alerts(
            statuses=["open", "acknowledged", "muted"],
            rule_code="LLM_USAGE_UNAVAILABLE", limit=10_000, offset=0,
        )
        resolved = 0
        for alert in listed.items:
            evidence = alert.current_evidence
            if evidence.llm_call_id:
                row = await self.observability_store.fetch_one(
                    "SELECT usage_available FROM observability_llm_calls WHERE call_id=?",
                    (evidence.llm_call_id,),
                )
                active = not row or not bool(row["usage_available"])
            else:
                rows = await self.observability_store.fetch_all(
                    """SELECT agent,usage_available FROM observability_llm_calls
                       WHERE workflow_id=? AND branch_id=?""",
                    (alert.thread_id, alert.branch_id),
                )
                matched = [
                    row for row in rows
                    if AlertStore.fingerprint(
                        alert.rule_id, alert.thread_id, alert.branch_id,
                        str(row.get("agent") or "unknown"),
                    ) == alert.fingerprint
                ]
                active = True if not matched else any(not bool(row["usage_available"]) for row in matched)
            if not active:
                resolved += int(await self.alerts.auto_resolve_external_alert(alert.alert_id, reason=reason))
        return resolved
