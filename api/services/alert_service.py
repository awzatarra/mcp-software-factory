from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import os
import re
import logging
from typing import Any, Sequence
from urllib.parse import quote
from uuid import uuid4

from api.alert_models import (
    AlertDetailResponse, AlertEvaluationResult, AlertEvidence, AlertListResponse,
    AlertNavigation, AlertRule, AlertRuleListResponse, AlertSummaryResponse,
    WorkflowAlert,
)
from api.dashboard_models import DashboardCountItem
from api.execution_models import WorkflowAgentExecutionResponse
from api.services.alert_store import AlertStore, SEVERITY_RANK
from api.services.workflow_execution_service import WorkflowExecutionService


EVENT_RULES = {
    "approval_required": {"APPROVAL_WAIT_TOO_LONG", "WORKFLOW_DURATION_ANOMALY", "DASHBOARD_METRIC_STALE"},
    "approval_granted": {"APPROVAL_WAIT_TOO_LONG"},
    "approval_rejected": {"APPROVAL_WAIT_TOO_LONG", "WORKFLOW_FAILED"},
    "test_run_completed": {"TESTS_FAILED", "REPAIR_FAILED", "HIGH_WARNING_COUNT"},
    "testing_completed": {"TESTS_FAILED", "REPAIR_FAILED", "HIGH_WARNING_COUNT"},
    "repair_completed": {"TESTS_FAILED", "REPAIR_FAILED", "EXCESSIVE_RETRIES"},
    "supervisor_decision_completed": {"SUPERVISOR_FALLBACK", "EXCESSIVE_RETRIES"},
    "supervisor_fallback_used": {"SUPERVISOR_FALLBACK"},
    "supervisor_loop_detected": {"SUPERVISOR_LOOP_DETECTED", "WORKFLOW_FAILED"},
    "workflow_completed": {"WORKFLOW_DURATION_ANOMALY", "EVENT_SEQUENCE_GAP", "DASHBOARD_METRIC_STALE"},
    "workflow_failed": {"WORKFLOW_FAILED", "TESTS_FAILED", "REPAIR_FAILED", "WORKFLOW_DURATION_ANOMALY"},
    "evaluation_completed": {"LOW_EVALUATION_SCORE"},
    "dashboard_metric_updated": {"DASHBOARD_METRIC_STALE"},
}
EXTERNAL_ALERT_RULES = {
    "LLM_BUDGET_WARNING", "LLM_BUDGET_EXCEEDED", "LLM_CALL_BLOCKED",
    "LLM_PRICING_MISSING", "LLM_USAGE_UNAVAILABLE", "LLM_COST_SPIKE", "HIGH_RETRY_COST",
}
logger = logging.getLogger(__name__)


class AlertNotFoundError(LookupError):
    pass


class AlertTransitionError(RuntimeError):
    pass


class AlertWorkflowNotFoundError(LookupError):
    pass


def _threshold(rule: AlertRule, name: str, fallback: float) -> float:
    if rule.threshold is None:
        return fallback
    value = getattr(rule.threshold, name)
    return fallback if value is None else float(value)


def _last_event(context: dict[str, Any], *types: str) -> dict[str, Any] | None:
    return next((event for event in reversed(context["events"]) if event["event_type"] in types), None)


def _event_file(event: dict[str, Any] | None, context: dict[str, Any]) -> str | None:
    candidates = []
    if event:
        candidates.append(event["data"].get("related_file") or event["data"].get("path"))
    candidates.extend(context.get("related_files") or [])
    for candidate in candidates:
        if isinstance(candidate, str) and not candidate.startswith(("/", "\\")) and ":" not in candidate and ".." not in candidate.replace("\\", "/").split("/"):
            return candidate.replace("\\", "/")
    return None


@dataclass(frozen=True)
class ApprovalWaitContext:
    operation: str
    approval_required_event_id: str
    approval_required_at: datetime
    wait_seconds: float
    related_task_id: str | None


@dataclass(frozen=True)
class AlertNavigationEvidence:
    related_task_id: str | None = None
    related_event_id: str | None = None
    related_file: str | None = None


def _event_branch(event: dict[str, Any]) -> str:
    return str(event.get("data", {}).get("branch_id") or "original")


def _event_operation_matches(event: dict[str, Any], operation: str) -> bool:
    data = event.get("data", {})
    candidate = data.get("operation") or data.get("pending_operation")
    if candidate is not None:
        return str(candidate).casefold() == operation.casefold()
    tool = str(data.get("tool_name") or data.get("tool") or "").casefold()
    normalized = operation.casefold()
    if normalized == "prepare_environment":
        return "prepare" in tool and "environment" in tool
    return normalized in tool


def find_current_approval_wait_context(
    *,
    events: Sequence[dict[str, Any]],
    branch_id: str,
    pending_operation: str | None,
    now: datetime | None = None,
) -> ApprovalWaitContext | None:
    if not pending_operation:
        return None
    approval = next((
        event for event in reversed(events)
        if event.get("event_type") == "approval_required"
        and _event_branch(event) == branch_id
        and _event_operation_matches(event, pending_operation)
    ), None)
    if approval is None:
        return None
    approval_at = datetime.fromisoformat(str(approval["timestamp"]).replace("Z", "+00:00"))
    current = now or datetime.now(UTC)
    task_id = approval.get("data", {}).get("task_id")
    return ApprovalWaitContext(
        operation=pending_operation,
        approval_required_event_id=str(approval["event_id"]),
        approval_required_at=approval_at,
        wait_seconds=max((current - approval_at).total_seconds(), 0),
        related_task_id=str(task_id) if task_id else None,
    )


def _normalized_agent(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _safe_test_path(path: str | None) -> str | None:
    if not path:
        return None
    normalized = path.replace("\\", "/").strip()
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":") or ".." in parts:
        return None
    return "/".join(parts) or None


def _is_run_tests_event(event: dict[str, Any]) -> bool:
    data = event.get("data", {})
    tool = str(data.get("tool_name") or data.get("tool") or "").casefold()
    operation = str(data.get("operation") or data.get("pending_operation") or "").casefold()
    return "run_tests" in tool or operation == "run_tests"


def build_testing_alert_navigation(
    *,
    execution: WorkflowAgentExecutionResponse | None,
    events: Sequence[dict[str, Any]],
    branch_id: str,
) -> AlertNavigationEvidence:
    if execution is None or execution.branch_id != branch_id:
        return AlertNavigationEvidence()
    qa_task = next((
        task for task in execution.planning.tasks
        if _normalized_agent(task.agent) == "qa reviewer"
    ), None)
    branch_events = [event for event in events if _event_branch(event) == branch_id]
    event = None
    for event_type in ("test_run_completed", "testing_completed", "tool_completed", "test_run_started"):
        event = next((
            item for item in reversed(branch_events)
            if item.get("event_type") == event_type
            and (event_type != "tool_completed" or _is_run_tests_event(item))
        ), None)
        if event is not None:
            break

    qa_paths = [
        path for raw in (qa_task.related_files if qa_task else [])
        if (path := _safe_test_path(raw)) and path.startswith("tests/")
    ]
    execution_paths = [
        *execution.testing.failing_test_files,
        *execution.testing.files_read_during_repair,
        *execution.testing.files_updated_during_repair,
        *execution.implementation.generated_files,
    ]
    safe_paths = [path for raw in execution_paths if (path := _safe_test_path(raw))]
    related_file = next(iter(qa_paths), None)
    if related_file is None:
        related_file = next((path for path in safe_paths if path == "tests/test_health.py"), None)
    if related_file is None:
        related_file = next((
            path for path in safe_paths
            if path.startswith("tests/") and path.rsplit("/", 1)[-1].startswith("test_") and path.endswith(".py")
        ), None)
    return AlertNavigationEvidence(
        related_task_id=qa_task.task_id if qa_task else None,
        related_event_id=str(event["event_id"]) if event else None,
        related_file=related_file,
    )


class AlertEvaluationService:
    def __init__(self, store: AlertStore, execution_service: WorkflowExecutionService | None = None, notification_dispatcher=None) -> None:
        self.store = store
        self.execution_service = execution_service
        self.notification_dispatcher = notification_dispatcher

    async def _dispatch(self, alert: WorkflowAlert, event: str, *, occurrence_id=None, action_id=None) -> None:
        if self.notification_dispatcher is None: return
        try:
            await self.notification_dispatcher.dispatch_alert_event(alert=alert, alert_event=event, occurrence_id=occurrence_id, action_id=action_id)
            if event in {"alert_acknowledged", "alert_resolved", "alert_auto_resolved"}:
                await self.notification_dispatcher.cancel_for_alert_transition(alert.alert_id, event)
        except Exception:
            logger.exception("notification dispatch failed alert=%s event=%s", alert.alert_id, event)

    async def record_external_condition(
        self, *, rule_code: str, thread_id: str, branch_id: str,
        severity: str, title: str, message: str, actual_value: Any,
        threshold_value: Any, dimension: str, project_name: str | None = None,
        evidence_context: dict[str, Any] | None = None,
    ) -> str:
        rule = await self.store.get_rule(f"builtin:{rule_code.casefold()}")
        if rule is None or not rule.enabled:
            return "disabled"
        fingerprint = self.store.fingerprint(rule.rule_id, thread_id, branch_id, dimension)
        previous = await self.store.get_alert_by_fingerprint(fingerprint)
        outcome = await self.store.detect(
            rule=rule, thread_id=thread_id, branch_id=branch_id, project_name=project_name,
            severity=severity, title=title, message=message,
            evidence=AlertEvidence(
                metric=rule_code.casefold(), actual_value=actual_value,
                threshold_value=threshold_value, **(evidence_context or {}),
            ),
            dimension=dimension, source_event_id=None,
        )
        current = await self.store.get_alert_by_fingerprint(fingerprint)
        if current and outcome in {"created", "reopened"}:
            occurrences, actions = await self.store.history(current.alert_id, 1)
            await self._dispatch(
                current, "alert_opened" if outcome == "created" else "alert_reopened",
                occurrence_id=occurrences[0].occurrence_id if occurrences else None,
                action_id=actions[0].action_id if actions else None,
            )
        elif current and previous and SEVERITY_RANK[current.severity] > SEVERITY_RANK[previous.severity]:
            occurrences, _ = await self.store.history(current.alert_id, 1)
            await self._dispatch(current, "alert_severity_escalated", occurrence_id=occurrences[0].occurrence_id if occurrences else None)
        return outcome

    async def clear_external_condition(self, *, rule_code: str, thread_id: str,
                                       branch_id: str, dimension: str) -> bool:
        rule = await self.store.get_rule(f"builtin:{rule_code.casefold()}")
        if rule is None or not rule.auto_resolve:
            return False
        fingerprint = self.store.fingerprint(rule.rule_id, thread_id, branch_id, dimension)
        alert = await self.store.get_alert_by_fingerprint(fingerprint)
        if alert is None or alert.status == "resolved":
            return False
        await self.transition(alert.alert_id, "resolve", "system", "Condition cleared")
        return True

    async def auto_resolve_external_alert(self, alert_id: str, *, reason: str) -> bool:
        current, changed = await self.store.auto_resolve_alert(
            alert_id, actor="finops-auto-resolve", note=reason,
        )
        if changed and current is not None:
            _, actions = await self.store.history(alert_id, 1)
            await self._dispatch(
                current, "alert_auto_resolved",
                action_id=actions[0].action_id if actions else None,
            )
        return changed

    async def evaluate_workflow(self, *, thread_id: str, branch_id: str = "original", trigger_event=None) -> AlertEvaluationResult:
        context = await self.store.workflow_context(thread_id, branch_id)
        if context is None:
            raise AlertWorkflowNotFoundError(thread_id)
        context["branch_id"] = branch_id
        context["execution"] = None
        if self.execution_service is not None:
            try:
                context["execution"] = await self.execution_service.get_execution(
                    thread_id, branch_id=branch_id
                )
            except LookupError:
                pass
        rules, _ = await self.store.list_rules(limit=500)
        event_type = getattr(getattr(trigger_event, "type", None), "value", None) or getattr(trigger_event, "event_type", None)
        relevant = EVENT_RULES.get(event_type) if event_type else None
        selected = [rule for rule in rules if rule.enabled and rule.code not in EXTERNAL_ALERT_RULES and (rule.branch_scope == "all" or branch_id == "original") and (relevant is None or rule.code in relevant)]
        result = AlertEvaluationResult(evaluated_rules=len(selected)); active: set[str] = set()
        selected_rule_ids = {rule.rule_id for rule in selected if rule.auto_resolve}
        unresolved_before = {
            item.alert_id: item for item in (await self.list_alerts(statuses=["open", "acknowledged", "muted"], thread_id=thread_id, branch_id=branch_id, limit=500, offset=0)).items
            if item.rule_id in selected_rule_ids
        }
        for rule in selected:
            try:
                detection = self._evaluate_rule(rule, context)
                if detection is None:
                    continue
                severity, title, message, evidence, dimension, source_event_id = detection
                fingerprint = self.store.fingerprint(rule.rule_id, thread_id, branch_id, dimension)
                active.add(fingerprint)
                previous = await self.store.get_alert_by_fingerprint(fingerprint)
                outcome = await self.store.detect(
                    rule=rule, thread_id=thread_id, branch_id=branch_id,
                    project_name=context.get("project_name"), severity=severity,
                    title=title, message=message, evidence=evidence,
                    dimension=dimension, source_event_id=source_event_id,
                )
                field = {"created": "created_alerts", "updated": "updated_alerts", "reopened": "reopened_alerts", "unchanged": "unchanged_alerts"}[outcome]
                setattr(result, field, getattr(result, field) + 1)
                current = await self.store.get_alert_by_fingerprint(fingerprint)
                if current and outcome in {"created", "reopened"}:
                    occurrences, actions = await self.store.history(current.alert_id, 1)
                    await self._dispatch(current, "alert_opened" if outcome == "created" else "alert_reopened", occurrence_id=occurrences[0].occurrence_id if occurrences else None, action_id=actions[0].action_id if actions else None)
                elif current and previous and SEVERITY_RANK[current.severity] > SEVERITY_RANK[previous.severity]:
                    occurrences, _ = await self.store.history(current.alert_id, 1)
                    await self._dispatch(current, "alert_severity_escalated", occurrence_id=occurrences[0].occurrence_id if occurrences else None)
            except Exception as exc:
                result.errors.append(f"{rule.code}:{type(exc).__name__}")
        result.resolved_alerts = await self.store.auto_resolve(thread_id=thread_id, branch_id=branch_id, active_fingerprints=active, rules=selected)
        for alert_id, previous in unresolved_before.items():
            if previous.fingerprint in active: continue
            current = await self.store.get_alert(alert_id)
            if current and current.status == "resolved":
                _, actions = await self.store.history(alert_id, 1)
                await self._dispatch(current, "alert_auto_resolved", action_id=actions[0].action_id if actions else None)
        return result

    def _evaluate_rule(self, rule: AlertRule, context: dict[str, Any]):
        code = rule.code; events = context["events"]
        event = _last_event(context, *EVENT_RULES.keys())
        source_event_id = str(event["event_id"]) if event else None
        task_id = str(event["data"].get("task_id")) if event and event["data"].get("task_id") else None
        related_file = _event_file(event, context)
        actual: Any = None; threshold: Any = None; dimension = code.casefold(); severity = rule.default_severity
        if code == "APPROVAL_WAIT_TOO_LONG":
            operation = context.get("pending_operation")
            approval = find_current_approval_wait_context(
                events=events,
                branch_id=str(context.get("branch_id") or "original"),
                pending_operation=operation,
            )
            if approval is None: return None
            actual = approval.wait_seconds
            threshold = _threshold(rule, "warning", 1800)
            if actual < threshold: return None
            if actual >= _threshold(rule, "critical", 14400): severity = "critical"
            elif actual >= _threshold(rule, "error", 3600): severity = "error"
            dimension = approval.operation; source_event_id = approval.approval_required_event_id
            task_id = approval.related_task_id; related_file = None
            title = "Approval wait too long"; message = f"{context.get('project_name') or context['thread_id']} has waited {round(actual)} seconds for {operation}."
        elif code == "WORKFLOW_FAILED":
            if context.get("status_group") != "failed": return None
            actual = context.get("terminal_status"); threshold = "failed"
            if context.get("repair_attempts", 0) > 0 or any(e["event_type"] == "supervisor_loop_detected" for e in events): severity = "critical"
            title = "Workflow failed"; message = "The workflow reached a failed terminal state."
        elif code == "TESTS_FAILED":
            if not context.get("tests_executed") or context.get("tests_passed"): return None
            actual = False; threshold = True; dimension = f"attempt:{context.get('repair_attempts', 0)}"
            title = "Tests failed"; message = "The latest test execution did not pass."
        elif code == "REPAIR_FAILED":
            if context.get("repair_attempts", 0) <= 0 or context.get("tests_passed"): return None
            actual = context.get("repair_attempts"); threshold = 0
            title = "Repair failed"; message = "Automated repair did not restore passing tests."
        elif code == "LOW_EVALUATION_SCORE":
            if context.get("evaluation_status") != "final" or context.get("overall_score") is None: return None
            actual = float(context["overall_score"]); threshold = _threshold(rule, "warning", 60)
            if actual >= threshold: return None
            severity = "critical" if actual < 40 else "error" if actual < 60 else "warning"
            dimension = str(context.get("scoring_version") or "latest")
            title = "Low evaluation score"; message = f"The final workflow score is {actual:g}."
        elif code == "SUPERVISOR_LOOP_DETECTED":
            loop = _last_event(context, "supervisor_loop_detected")
            if loop is None and context.get("terminal_status") != "supervisor_loop_detected": return None
            actual = True; threshold = False; source_event_id = str(loop["event_id"]) if loop else None
            title = "Supervisor loop detected"; message = "The supervisor detected a loop without progress."
        elif code == "SUPERVISOR_FALLBACK":
            fallback = next((item for item in reversed(events) if item["event_type"] == "supervisor_fallback_used" or item["data"].get("decision_source") == "fallback"), None)
            if fallback is None: return None
            actual = "fallback"; threshold = "model"; source_event_id = str(fallback["event_id"])
            title = "Supervisor fallback"; message = "The supervisor used its deterministic fallback."
        elif code == "HIGH_WARNING_COUNT":
            actual = int(context.get("test_warnings") or 0); threshold = _threshold(rule, "warning", 5)
            if actual < threshold: return None
            title = "High warning count"; message = f"Testing completed with {actual} warnings."
        elif code == "EXCESSIVE_RETRIES":
            agent_retries = sum(max(int(agent.get("attempt") or 1) - 1, 0) for agent in context.get("agents", []))
            actual = agent_retries + int(context.get("repair_attempts") or 0); threshold = _threshold(rule, "warning", 3)
            if actual < threshold: return None
            title = "Excessive retries"; message = f"The workflow accumulated {actual} retries."
        elif code == "WORKFLOW_DURATION_ANOMALY":
            if context.get("total_seconds") is None or float(context["total_seconds"]) < 0: return None
            actual = float(context["total_seconds"]); threshold = max(_threshold(rule, "warning", 3600), float(context.get("duration_p95") or 0))
            if actual <= threshold: return None
            title = "Workflow duration anomaly"; message = f"Workflow duration reached {actual:g} seconds."
        elif code == "EVENT_SEQUENCE_GAP":
            sequences = [int(item["sequence"]) for item in events]
            gaps = [right for left, right in zip(sequences, sequences[1:]) if right != left + 1]
            if not gaps: return None
            actual = gaps[0]; threshold = "contiguous"; dimension = f"gap:{gaps[0]}"
            title = "Event sequence gap"; message = f"Durable sequence {gaps[0]} has no preceding event."
        elif code == "DASHBOARD_METRIC_STALE":
            if not events or not context.get("source_updated_at"): return None
            latest = datetime.fromisoformat(events[-1]["timestamp"].replace("Z", "+00:00")); projected = datetime.fromisoformat(context["source_updated_at"].replace("Z", "+00:00"))
            actual = max((latest - projected).total_seconds(), 0); threshold = _threshold(rule, "warning", 300)
            if actual <= threshold: return None
            title = "Dashboard metric stale"; message = f"The dashboard projection trails the event stream by {round(actual)} seconds."
        else:
            return None
        if code in {"HIGH_WARNING_COUNT", "TESTS_FAILED", "REPAIR_FAILED"}:
            navigation = build_testing_alert_navigation(
                execution=context.get("execution"),
                events=events,
                branch_id=str(context.get("branch_id") or "original"),
            )
            task_id = navigation.related_task_id
            source_event_id = navigation.related_event_id
            related_file = navigation.related_file
        evidence = AlertEvidence(metric=code.casefold(), actual_value=actual, threshold_value=threshold, related_task_id=task_id, related_event_id=source_event_id, related_file=related_file, pending_operation=context.get("pending_operation"))
        return severity, title, message, evidence, dimension, source_event_id

    async def list_rules(self, **filters) -> AlertRuleListResponse:
        items, total = await self.store.list_rules(**filters)
        return AlertRuleListResponse(items=items, total=total, limit=filters.get("limit", 100), offset=filters.get("offset", 0), has_more=filters.get("offset", 0) + len(items) < total)

    async def list_alerts(self, **filters) -> AlertListResponse:
        items, total, counts = await self.store.list_alerts(**filters)
        return AlertListResponse(items=items, total=total, limit=filters.get("limit", 50), offset=filters.get("offset", 0), has_more=filters.get("offset", 0) + len(items) < total, counts=counts)

    async def detail(self, alert_id: str) -> AlertDetailResponse:
        alert = await self.store.get_alert(alert_id)
        if alert is None: raise AlertNotFoundError(alert_id)
        rule = await self.store.get_rule(alert.rule_id)
        occurrences, actions = await self.store.history(alert_id)
        deliveries = []
        if self.notification_dispatcher is not None:
            listed = await self.notification_dispatcher.store.list_deliveries(alert_id=alert_id, limit=100, offset=0)
            deliveries = [item.model_dump(mode="json") for item in listed.items]
        return AlertDetailResponse(alert=alert, occurrences=occurrences, actions=actions, rule=rule, navigation=self._navigation(alert), notification_deliveries=deliveries)

    @staticmethod
    def _navigation(alert: WorkflowAlert) -> AlertNavigation:
        branch = "" if alert.branch_id == "original" else f"&branch_id={quote(alert.branch_id)}"
        root = f"/workflows/{quote(alert.thread_id)}"
        return AlertNavigation(
            workflow=root + (f"?branch_id={quote(alert.branch_id)}" if branch else ""),
            evaluation=f"{root}?tab=evaluation{branch}" if alert.category in {"testing", "repair", "evaluation"} else None,
            timeline=f"{root}?tab=timeline&event={quote(alert.primary_event_id)}{branch}" if alert.primary_event_id else None,
            execution=f"{root}?tab=execution&task={quote(alert.related_task_id)}{branch}" if alert.related_task_id else None,
            project=f"{root}?tab=project&file={quote(alert.related_file)}{branch}" if alert.related_file else None,
        )

    async def transition(self, alert_id: str, action: str, actor: str, note: str | None = None, duration_seconds: int | None = None) -> WorkflowAlert:
        alert, valid = await self.store.transition(alert_id, action=action, actor=actor, note=note, duration_seconds=duration_seconds)
        if alert is None: raise AlertNotFoundError(alert_id)
        if not valid: raise AlertTransitionError(f"Cannot {action} alert in status {alert.status}")
        event = {"acknowledge": "alert_acknowledged", "resolve": "alert_resolved", "reopen": "alert_reopened", "mute": "alert_muted", "unmute": "alert_unmuted"}[action]
        _, actions = await self.store.history(alert_id, 1)
        await self._dispatch(alert, event, action_id=actions[0].action_id if actions else None)
        return alert

    async def summary(self, *, date_from: datetime | None = None, date_to: datetime | None = None, branch_scope: str = "original", project_name: str | None = None) -> AlertSummaryResponse:
        items, _, _ = await self.store.list_alerts(statuses=None, date_from=date_from.isoformat() if date_from else None, date_to=date_to.isoformat() if date_to else None, branch_id="original" if branch_scope == "original" else None, project_name=project_name, limit=100_000, offset=0)
        counts = Counter(item.status for item in items)
        now = datetime.now(UTC); rules = Counter(item.rule_code for item in items)
        ack = [(item.acknowledged_at - item.first_detected_at).total_seconds() for item in items if item.acknowledged_at]
        resolved = [(item.resolved_at - item.first_detected_at).total_seconds() for item in items if item.resolved_at]
        active = [item for item in items if item.status in {"open", "acknowledged"}]
        return AlertSummaryResponse(
            open_total=counts["open"], acknowledged_total=counts["acknowledged"],
            resolved_total=counts["resolved"], muted_total=counts["muted"],
            critical_open=sum(item.severity == "critical" for item in active),
            error_open=sum(item.severity == "error" for item in active),
            warning_open=sum(item.severity == "warning" for item in active),
            info_open=sum(item.severity == "info" for item in active),
            affected_workflows=len({(item.thread_id, item.branch_id) for item in active}),
            top_rules=[DashboardCountItem(key=code, label=code.replace("_", " ").title(), count=count) for code, count in rules.most_common(8)],
            average_time_to_acknowledge_seconds=sum(ack) / len(ack) if ack else None,
            average_time_to_resolve_seconds=sum(resolved) / len(resolved) if resolved else None,
            oldest_open_alert_seconds=max(((now - item.first_detected_at).total_seconds() for item in active), default=None),
            calculated_at=now,
        )

    async def backfill(self, batch_size: int = 100) -> dict[str, int]:
        counters = Counter(processed=0, created=0, updated=0, resolved=0, unchanged=0, failed=0); offset = 0
        while True:
            keys = await self.store.workflow_keys(batch_size, offset)
            if not keys: break
            for thread_id, branch_id in keys:
                counters["processed"] += 1
                try:
                    result = await self.evaluate_workflow(thread_id=thread_id, branch_id=branch_id)
                    counters["created"] += result.created_alerts; counters["updated"] += result.updated_alerts + result.reopened_alerts; counters["resolved"] += result.resolved_alerts
                    if not result.created_alerts and not result.updated_alerts and not result.reopened_alerts and not result.resolved_alerts: counters["unchanged"] += 1
                    if result.errors: counters["failed"] += 1
                except Exception: counters["failed"] += 1
            offset += len(keys)
        return dict(counters)


class PeriodicAlertEvaluator:
    def __init__(self, service: AlertEvaluationService, *, interval_seconds: float = 60, batch_size: int = 100) -> None:
        self.service = service; self.interval_seconds = interval_seconds; self.batch_size = batch_size
        self.owner_id = str(uuid4()); self._task: asyncio.Task | None = None; self._closed = False

    async def start(self) -> None:
        if self._task is None and not self._closed: self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
            self._task = None
        await self.service.store.release_lock(self.owner_id)

    async def run_once(self) -> bool:
        acquired = await self.service.store.acquire_lock(self.owner_id, max(round(self.interval_seconds * 2), 10))
        if not acquired: return False
        try:
            for thread_id, branch_id in await self.service.store.workflow_keys(self.batch_size):
                try: await self.service.evaluate_workflow(thread_id=thread_id, branch_id=branch_id)
                except AlertWorkflowNotFoundError: pass
        finally:
            await self.service.store.release_lock(self.owner_id)
        return True

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.run_once()


def periodic_evaluator_from_env(service: AlertEvaluationService) -> PeriodicAlertEvaluator | None:
    if os.getenv("ALERT_EVALUATION_ENABLED", "true").casefold() != "true": return None
    return PeriodicAlertEvaluator(service, interval_seconds=max(float(os.getenv("ALERT_EVALUATION_INTERVAL_SECONDS", "60")), 1), batch_size=max(int(os.getenv("ALERT_EVALUATION_BATCH_SIZE", "100")), 1))
