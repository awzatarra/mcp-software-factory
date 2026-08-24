from __future__ import annotations

import math
import os
from datetime import UTC, datetime
from typing import Any, Mapping

from api.ci_models import (
    CIAnalyticsResponse,
    CIAuditEntry,
    CIAuditTrail,
    CIFailureMetrics,
    CIGateMetrics,
    CIOperationalSummary,
    CIPromotionMetrics,
    CIRepairMetrics,
    CIStepMetrics,
)
from api.services.ci_store import CIPipelineStore


STEP_TYPES = ("build", "test", "lint", "package", "custom")
GATE_TYPES = ("build", "test", "lint", "package")
REPAIR_CATEGORIES = (
    "repairable_code",
    "repairable_tests",
    "repairable_lint",
    "repairable_build",
    "infrastructure",
    "configuration",
    "non_repairable",
    "unknown",
)
INFRASTRUCTURE_FAILURE_TYPES = {
    "ci_step_timeout",
    "ci_pipeline_timeout",
    "ci_environment_unavailable",
    "ci_execution_interrupted",
}
CONFIGURATION_FAILURE_TYPES = {
    "ci_command_not_allowed",
    "ci_path_violation",
    "ci_pipeline_stale",
}
SECRET_MARKERS = ("secret", "token", "password", "credential", "authorization", "api_key")


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator <= 0 else numerator / denominator


def _average(values: list[float]) -> float | None:
    return None if not values else sum(values) / len(values)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile / 100 * len(ordered)) - 1))
    return ordered[index]


def _duration_stats(values: list[float]) -> tuple[float | None, float | None, float | None]:
    return _average(values), _percentile(values, 50), _percentile(values, 95)


def _valid_duration(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None


def _sort_timestamp(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_metadata(values: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in values.items():
        lowered = key.casefold()
        if any(marker in lowered for marker in SECRET_MARKERS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            text = str(value) if isinstance(value, str) else value
            if isinstance(text, str) and any(marker in text.casefold() for marker in SECRET_MARKERS):
                continue
            safe[key] = text[:500] if isinstance(text, str) else text
    return safe


def _framework(run: Mapping[str, Any]) -> str | None:
    value = (run.get("pipeline") or {}).get("framework")
    return str(value) if value else None


def _source_mode(run: Mapping[str, Any]) -> str:
    source = run.get("source") or {}
    return "commit" if source.get("source_commit") else str(source.get("source_mode") or "working_tree")


def _commit(run: Mapping[str, Any]) -> str | None:
    source = run.get("source") or {}
    return source.get("source_commit") or run.get("ci_validated_commit")


def _failure_bucket(failure_type: str | None, category: str | None) -> str:
    if category in {"infrastructure"} or failure_type in INFRASTRUCTURE_FAILURE_TYPES:
        return "infrastructure"
    if category == "configuration" or failure_type in CONFIGURATION_FAILURE_TYPES:
        return "configuration"
    if category in {"repairable_code", "repairable_tests", "repairable_lint", "repairable_build"}:
        return "code"
    if failure_type in {
        "ci_build_failed",
        "ci_build_gate_failed",
        "ci_tests_failed",
        "ci_test_gate_failed",
        "ci_lint_failed",
        "ci_lint_gate_failed",
        "ci_package_failed",
        "ci_package_gate_failed",
    }:
        return "code"
    return "unknown"


class CIAnalyticsService:
    def __init__(self, store: CIPipelineStore) -> None:
        self.store = store
        self.version = os.getenv("CI_OBSERVABILITY_VERSION", "6.21.5-v1")

    @property
    def default_limit(self) -> int:
        return max(1, int(os.getenv("CI_ANALYTICS_DEFAULT_LIMIT", "500")))

    @property
    def max_limit(self) -> int:
        return max(1, int(os.getenv("CI_ANALYTICS_MAX_LIMIT", "5000")))

    def clamp_limit(self, limit: int | None) -> int:
        requested = limit or self.default_limit
        return min(max(1, requested), self.max_limit)

    async def metrics(
        self,
        *,
        limit: int | None = None,
        framework: str | None = None,
        from_timestamp: datetime | None = None,
        to_timestamp: datetime | None = None,
    ) -> CIAnalyticsResponse:
        effective_limit = self.clamp_limit(limit)
        runs = await self.store.list_runs_for_analytics(
            limit=effective_limit,
            framework=framework,
            from_timestamp=from_timestamp.isoformat() if from_timestamp else None,
            to_timestamp=to_timestamp.isoformat() if to_timestamp else None,
        )
        summary = self._summary(runs)
        gates = self._gates(runs)
        return CIAnalyticsResponse(
            version=self.version,
            limit=effective_limit,
            framework=framework,
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            summary=summary,
            steps=self._steps(runs),
            gates=gates,
            failures=self._failures(runs),
            repair=self._repair(runs),
            promotion=self._promotion(runs),
            flat_gates={
                "build_gate_failure_rate": gates["build"].failure_rate,
                "test_gate_failure_rate": gates["test"].failure_rate,
                "lint_gate_warning_rate": gates["lint"].warning_rate,
                "lint_gate_failure_rate": gates["lint"].failure_rate,
                "package_gate_failure_rate": gates["package"].failure_rate,
            },
        )

    async def audit_trail(
        self,
        workflow_id: str,
        *,
        repair_state: Mapping[str, Any] | None = None,
        promotion_eligible: bool | None = None,
    ) -> CIAuditTrail:
        runs = await self.store.list_runs(workflow_id, limit=self.max_limit, status=None)
        entries: list[CIAuditEntry] = []
        for run in sorted(runs, key=lambda item: (str(item.get("started_at") or item.get("created_at") or ""), str(item.get("ci_run_id") or ""))):
            entries.extend(self._audit_entries_for_run(run, promotion_eligible=promotion_eligible))
        if repair_state:
            entries.extend(self._audit_entries_for_repair(workflow_id, repair_state))
        entries = sorted(
            entries,
            key=lambda item: (
                _sort_timestamp(item.timestamp),
                item.ci_run_id or "",
                item.event_type,
                item.step_id or "",
                item.gate or "",
            ),
        )
        return CIAuditTrail(workflow_id=workflow_id, total=len(entries), entries=entries)

    def _summary(self, runs: list[Mapping[str, Any]]) -> CIOperationalSummary:
        decision_runs = [run for run in runs if run.get("decision")]
        completed_with_decision = [run for run in decision_runs if run.get("status") in {"passed", "failed"}]
        durations = [
            value for run in runs
            if run.get("status") in {"passed", "failed"}
            if (value := _valid_duration(run.get("duration_seconds"))) is not None
        ]
        avg, p50, p95 = _duration_stats(durations)
        accepted = sum(1 for run in completed_with_decision if run.get("decision") == "accepted")
        warnings = sum(1 for run in completed_with_decision if run.get("decision") == "accepted_with_warnings")
        rejected = sum(1 for run in completed_with_decision if run.get("decision") == "rejected")
        denominator = len(completed_with_decision)
        return CIOperationalSummary(
            total_runs=len(runs),
            completed_runs=sum(1 for run in runs if run.get("status") in {"passed", "failed"}),
            runs_with_decision=denominator,
            accepted_runs=accepted,
            accepted_with_warnings_runs=warnings,
            rejected_runs=rejected,
            acceptance_rate=_rate(accepted, denominator),
            warning_rate=_rate(warnings, denominator),
            rejection_rate=_rate(rejected, denominator),
            average_pipeline_duration_seconds=avg,
            p50_pipeline_duration_seconds=p50,
            p95_pipeline_duration_seconds=p95,
            commit_bound_runs=sum(1 for run in runs if _source_mode(run) == "commit"),
            working_tree_runs=sum(1 for run in runs if _source_mode(run) != "commit"),
        )

    def _steps(self, runs: list[Mapping[str, Any]]) -> dict[str, CIStepMetrics]:
        result: dict[str, CIStepMetrics] = {kind: CIStepMetrics() for kind in STEP_TYPES}
        durations: dict[str, list[float]] = {kind: [] for kind in STEP_TYPES}
        for run in runs:
            for step in run.get("steps") or []:
                kind = str(step.get("type") or "custom")
                if kind not in result:
                    kind = "custom"
                metrics = result[kind]
                metrics.run_count += 1
                status = step.get("status")
                if status == "passed":
                    metrics.passed_count += 1
                elif status == "failed":
                    metrics.failed_count += 1
                elif status == "timed_out":
                    metrics.timed_out_count += 1
                elif status == "skipped":
                    metrics.skipped_count += 1
                if (duration := _valid_duration(step.get("duration_seconds"))) is not None:
                    durations[kind].append(duration)
        for kind, metrics in result.items():
            avg, p50, p95 = _duration_stats(durations[kind])
            metrics.average_duration_seconds = avg
            metrics.p50_duration_seconds = p50
            metrics.p95_duration_seconds = p95
            metrics.failure_rate = _rate(metrics.failed_count + metrics.timed_out_count, metrics.run_count)
        return result

    def _gates(self, runs: list[Mapping[str, Any]]) -> dict[str, CIGateMetrics]:
        result: dict[str, CIGateMetrics] = {kind: CIGateMetrics() for kind in GATE_TYPES}
        for run in runs:
            for gate in run.get("gates") or []:
                kind = str(gate.get("type") or "")
                if kind not in result:
                    continue
                metrics = result[kind]
                metrics.evaluated_count += 1
                status = gate.get("status")
                if status == "passed":
                    metrics.passed_count += 1
                elif status == "failed":
                    metrics.failed_count += 1
                elif status == "warning":
                    metrics.warning_count += 1
                elif status == "skipped":
                    metrics.skipped_count += 1
                elif status == "not_applicable":
                    metrics.not_applicable_count += 1
        for metrics in result.values():
            metrics.failure_rate = _rate(metrics.failed_count, metrics.evaluated_count)
            metrics.warning_rate = _rate(metrics.warning_count, metrics.evaluated_count)
        return result

    def _failures(self, runs: list[Mapping[str, Any]]) -> CIFailureMetrics:
        metrics = CIFailureMetrics(by_repair_category={category: 0 for category in REPAIR_CATEGORIES})
        failure_denominator = 0
        for run in runs:
            failure_type = run.get("failure_type")
            repairability = run.get("repairability") or {}
            category = repairability.get("category")
            if failure_type:
                metrics.by_failure_type[failure_type] = metrics.by_failure_type.get(failure_type, 0) + 1
                failure_denominator += 1
            if category:
                metrics.by_repair_category[category] = metrics.by_repair_category.get(category, 0) + 1
            bucket = _failure_bucket(failure_type, category)
            if failure_type or category:
                if bucket == "code":
                    metrics.code_related_failure_count += 1
                elif bucket == "infrastructure":
                    metrics.infrastructure_failure_count += 1
                elif bucket == "configuration":
                    metrics.configuration_failure_count += 1
                else:
                    metrics.unknown_failure_count += 1
        denominator = max(failure_denominator, sum(metrics.by_repair_category.values()))
        metrics.code_related_failure_rate = _rate(metrics.code_related_failure_count, denominator)
        metrics.infrastructure_failure_rate = _rate(metrics.infrastructure_failure_count, denominator)
        metrics.configuration_failure_rate = _rate(metrics.configuration_failure_count, denominator)
        metrics.unknown_failure_rate = _rate(metrics.unknown_failure_count, denominator)
        metrics.by_failure_type = dict(sorted(metrics.by_failure_type.items()))
        metrics.by_repair_category = dict(sorted(metrics.by_repair_category.items()))
        return metrics

    def _repair(self, runs: list[Mapping[str, Any]]) -> CIRepairMetrics:
        required = [run for run in runs if (run.get("repairability") or {}).get("repairable")]
        by_workflow: dict[str, list[Mapping[str, Any]]] = {}
        for run in runs:
            by_workflow.setdefault(str(run.get("workflow_id")), []).append(run)
        recovered = 0
        failed_after_repair = 0
        attempts: list[int] = []
        for chain in by_workflow.values():
            ordered = sorted(chain, key=lambda item: str(item.get("started_at") or item.get("completed_at") or ""))
            pending_attempts = 0
            for run in ordered:
                if (run.get("repairability") or {}).get("repairable"):
                    pending_attempts += 1
                elif pending_attempts and run.get("decision") == "accepted":
                    recovered += 1
                    attempts.append(pending_attempts)
                    pending_attempts = 0
                elif pending_attempts and run.get("decision") == "rejected":
                    failed_after_repair += 1
                    attempts.append(pending_attempts)
            if pending_attempts:
                attempts.append(pending_attempts)
        terminal = recovered + failed_after_repair
        exhausted = sum(1 for run in runs if run.get("failure_type") == "ci_repair_exhausted")
        return CIRepairMetrics(
            ci_repair_required_count=len(required),
            ci_repair_success_count=recovered,
            ci_repair_failed_count=failed_after_repair,
            ci_repair_exhausted_count=exhausted,
            ci_repair_success_rate=_rate(recovered, terminal),
            ci_repair_exhaustion_rate=_rate(exhausted, terminal + exhausted),
            average_ci_repair_attempts=_average([float(value) for value in attempts]),
            maximum_ci_repair_attempts_observed=max(attempts or [0]),
            commits_repaired_count=recovered,
            average_commits_per_repair_chain=_average([1.0 for _ in range(recovered)]),
            runs_recovered_after_repair=recovered,
            runs_failed_after_repair=failed_after_repair,
        )

    def _promotion(self, runs: list[Mapping[str, Any]]) -> CIPromotionMetrics:
        eligible = sum(1 for run in runs if run.get("decision") in {"accepted", "accepted_with_warnings"} and run.get("ci_validated_commit"))
        rejected = sum(1 for run in runs if run.get("decision") == "rejected")
        no_ci = sum(1 for run in runs if not run.get("decision") and run.get("status") in {"passed", "failed"})
        mismatch = sum(1 for run in runs if run.get("failure_type") in {"ci_commit_missing", "ci_promotion_commit_mismatch"})
        stale = sum(1 for run in runs if run.get("failure_type") == "ci_pipeline_stale")
        blocked = rejected + no_ci + mismatch + stale
        return CIPromotionMetrics(
            promotion_eligible_count=eligible,
            promotion_blocked_count=blocked,
            promotion_blocked_no_ci_count=no_ci,
            promotion_blocked_rejected_ci_count=rejected,
            promotion_blocked_commit_mismatch_count=mismatch,
            promotion_blocked_stale_ci_count=stale,
            promotion_eligibility_rate=_rate(eligible, eligible + blocked),
        )

    def _audit_entries_for_run(self, run: Mapping[str, Any], *, promotion_eligible: bool | None) -> list[CIAuditEntry]:
        workflow_id = str(run.get("workflow_id"))
        ci_run_id = str(run.get("ci_run_id"))
        commit = _commit(run)
        entries = [
            CIAuditEntry(
                event_type="ci_pipeline_started",
                timestamp=_timestamp(run.get("started_at")),
                workflow_id=workflow_id,
                ci_run_id=ci_run_id,
                commit=commit,
                metadata=_safe_metadata({"framework": _framework(run), "source_mode": _source_mode(run)}),
            )
        ]
        for step in run.get("steps") or []:
            status = step.get("status")
            event_type = "ci_step_failed" if status in {"failed", "timed_out", "interrupted"} else "ci_step_completed"
            entries.append(CIAuditEntry(
                event_type=event_type,
                timestamp=_timestamp(step.get("completed_at") or step.get("started_at")),
                workflow_id=workflow_id,
                ci_run_id=ci_run_id,
                commit=commit,
                step_id=step.get("step_id"),
                failure_type=step.get("failure_type"),
                metadata=_safe_metadata({"type": step.get("type"), "status": status, "duration_seconds": step.get("duration_seconds")}),
            ))
        for gate in run.get("gates") or []:
            status = gate.get("status")
            event_type = "ci_gate_failed" if status == "failed" else "ci_gate_warning" if status == "warning" else "ci_gate_evaluated"
            entries.append(CIAuditEntry(
                event_type=event_type,
                timestamp=_timestamp(run.get("completed_at")),
                workflow_id=workflow_id,
                ci_run_id=ci_run_id,
                commit=commit,
                gate=gate.get("type"),
                failure_type=gate.get("failure_type"),
                metadata=_safe_metadata({"status": status, "blocking": gate.get("blocking"), "required": gate.get("required")}),
            ))
        entries.append(CIAuditEntry(
            event_type="ci_decision_completed",
            timestamp=_timestamp(run.get("completed_at")),
            workflow_id=workflow_id,
            ci_run_id=ci_run_id,
            commit=commit,
            decision=run.get("decision"),
            failure_type=run.get("failure_type"),
            metadata=_safe_metadata({"status": run.get("status"), "blocking_gate": run.get("blocking_gate")}),
        ))
        if commit:
            entries.append(CIAuditEntry(
                event_type="ci_git_binding_created",
                timestamp=_timestamp(run.get("completed_at")),
                workflow_id=workflow_id,
                ci_run_id=ci_run_id,
                commit=commit,
                decision=run.get("decision"),
                metadata=_safe_metadata({"ci_validated_commit": run.get("ci_validated_commit")}),
            ))
        repairability = run.get("repairability") or {}
        if repairability:
            entries.append(CIAuditEntry(
                event_type="ci_failure_classified",
                timestamp=_timestamp(run.get("completed_at")),
                workflow_id=workflow_id,
                ci_run_id=ci_run_id,
                commit=commit,
                failure_type=repairability.get("failure_type") or run.get("failure_type"),
                metadata=_safe_metadata({
                    "category": repairability.get("category"),
                    "repairable": repairability.get("repairable"),
                    "failed_gate": repairability.get("failed_gate"),
                    "failed_step": repairability.get("failed_step"),
                }),
            ))
        if run.get("decision"):
            entries.append(CIAuditEntry(
                event_type="ci_promotion_eligibility_evaluated",
                timestamp=_timestamp(run.get("completed_at")),
                workflow_id=workflow_id,
                ci_run_id=ci_run_id,
                commit=commit,
                promotion_eligible=promotion_eligible if promotion_eligible is not None else run.get("decision") in {"accepted", "accepted_with_warnings"},
                metadata=_safe_metadata({"decision": run.get("decision")}),
            ))
            if run.get("decision") == "rejected":
                entries.append(CIAuditEntry(
                    event_type="ci_promotion_blocked",
                    timestamp=_timestamp(run.get("completed_at")),
                    workflow_id=workflow_id,
                    ci_run_id=ci_run_id,
                    commit=commit,
                    promotion_eligible=False,
                    failure_type=run.get("failure_type"),
                    metadata=_safe_metadata({"blocking_gate": run.get("blocking_gate")}),
                ))
        return entries

    def _audit_entries_for_repair(self, workflow_id: str, repair_state: Mapping[str, Any]) -> list[CIAuditEntry]:
        entries: list[CIAuditEntry] = []
        lineage = repair_state.get("lineage") or []
        for item in lineage:
            attempt = item.get("attempt")
            source_run = item.get("source_run_id")
            source_commit = item.get("source_commit")
            repair_commit = item.get("repair_commit")
            decision = item.get("result_decision")
            entries.append(CIAuditEntry(
                event_type="ci_repair_started",
                workflow_id=workflow_id,
                ci_run_id=source_run,
                commit=source_commit,
                repair_attempt=attempt,
                repair_commit=repair_commit,
                metadata=_safe_metadata({"source_run_id": source_run}),
            ))
            entries.append(CIAuditEntry(
                event_type="ci_repair_commit_created",
                workflow_id=workflow_id,
                ci_run_id=source_run,
                commit=repair_commit,
                repair_attempt=attempt,
                repair_commit=repair_commit,
            ))
            entries.append(CIAuditEntry(
                event_type="ci_repair_completed" if decision in {"accepted", "accepted_with_warnings"} else "ci_repair_failed",
                workflow_id=workflow_id,
                ci_run_id=item.get("result_run_id"),
                commit=repair_commit,
                decision=decision,
                repair_attempt=attempt,
                repair_commit=repair_commit,
            ))
        if repair_state.get("state") == "exhausted":
            entries.append(CIAuditEntry(
                event_type="ci_repair_exhausted",
                workflow_id=workflow_id,
                ci_run_id=repair_state.get("source_run_id"),
                commit=repair_state.get("source_commit"),
                repair_attempt=repair_state.get("attempts"),
            ))
        return entries
