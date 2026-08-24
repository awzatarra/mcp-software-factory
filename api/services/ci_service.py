from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import tempfile
from time import perf_counter
from typing import Any, Iterable, Mapping
from uuid import uuid4

from git_dirty_paths import classify_dirty_paths

from api.ci_models import (
    CIGateDefinition,
    CIGateEvaluation,
    CIGateResult,
    CIRepairability,
    CIPromotionEligibility,
    CIPipelineDefinition,
    CIPipelinePreview,
    CIPipelineRun,
    CIPipelineStep,
    CIRunListResponse,
    CISourceRevision,
    CIStepRun,
    CIWorkflowStatus,
    CIRepairStatus,
)
from api.services.ci_store import CIPipelineStore
from api.services.git_store import normalize_commit_id
from api.services.observability_sanitizer import sanitize_value
from streaming import EventStatus, WorkflowEventType, emit_workflow_event


CI_FAILURE_TYPES = {
    "ci_pipeline_stale",
    "ci_pipeline_timeout",
    "ci_step_timeout",
    "ci_command_not_allowed",
    "ci_environment_unavailable",
    "ci_execution_context_invalid",
    "ci_build_failed",
    "ci_tests_failed",
    "ci_lint_failed",
    "ci_package_failed",
    "ci_execution_interrupted",
    "ci_repair_not_applicable",
    "ci_repair_no_changes",
    "ci_repair_validation_failed",
    "ci_repair_commit_not_advanced",
    "ci_repair_exhausted",
    "ci_repair_failed",
}
ALLOWED_EXECUTABLES = {"python", "pytest", "pip", "uv", "dotnet", "npm", "npx"}
FORBIDDEN_TOKENS = {"&&", "||", "|", ";", ">", "<", "`", "$(", "rm", "del", "erase", "curl", "eval"}
SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
PYTEST_ROOTDIR_RE = re.compile(r"(?im)^rootdir:\s*(.+?)\s*$")


class CIError(RuntimeError):
    code = "ci_error"


class CIProjectUnavailable(CIError):
    code = "ci_project_unavailable"


class CICommandNotAllowed(CIError):
    code = "ci_command_not_allowed"


class CIPipelineStale(CIError):
    code = "ci_pipeline_stale"


class CIEnvironmentUnavailable(CIError):
    code = "ci_environment_unavailable"


class CIPathViolation(CIError):
    code = "ci_path_violation"


@dataclass(frozen=True)
class CIPipelinePolicy:
    version: str = "6.21.1-v1"
    pipeline_timeout_seconds: float = 600.0
    step_timeout_seconds: float = 180.0
    output_max_chars: int = 20_000

    @classmethod
    def from_environment(cls) -> "CIPipelinePolicy":
        return cls(
            version=os.getenv("CI_PIPELINE_VERSION", "6.21.1-v1"),
            pipeline_timeout_seconds=float(os.getenv("CI_PIPELINE_TIMEOUT_SECONDS", "600")),
            step_timeout_seconds=float(os.getenv("CI_STEP_TIMEOUT_SECONDS", "180")),
            output_max_chars=int(os.getenv("CI_STEP_OUTPUT_MAX_CHARS", "20000")),
        )


@dataclass(frozen=True)
class CIGatePolicy:
    version: str = "6.21.2-v1"
    build_blocking: bool = True
    test_blocking: bool = True
    lint_blocking: bool = False
    package_blocking: bool = False

    @classmethod
    def from_environment(cls) -> "CIGatePolicy":
        def enabled(name: str, default: str) -> bool:
            return os.getenv(name, default).strip().casefold() in {"1", "true", "yes", "on"}

        return cls(
            version=os.getenv("CI_GATE_POLICY_VERSION", "6.21.2-v1"),
            build_blocking=enabled("CI_BUILD_GATE_BLOCKING", "true"),
            test_blocking=enabled("CI_TEST_GATE_BLOCKING", "true"),
            lint_blocking=enabled("CI_LINT_GATE_BLOCKING", "false"),
            package_blocking=enabled("CI_PACKAGE_GATE_BLOCKING", "false"),
        )


@dataclass(frozen=True)
class CIPromotionPolicy:
    version: str = "6.21.3-v1"
    required: bool = True
    allow_warnings: bool = True

    @classmethod
    def from_environment(cls) -> "CIPromotionPolicy":
        def enabled(name: str, default: str) -> bool:
            return os.getenv(name, default).strip().casefold() in {"1", "true", "yes", "on"}

        return cls(
            version=os.getenv("CI_GIT_INTEGRATION_VERSION", "6.21.3-v1"),
            required=enabled("CI_PROMOTION_REQUIRED", "true"),
            allow_warnings=enabled("CI_PROMOTION_ALLOW_WARNINGS", "true"),
        )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_executable(value: str) -> str:
    base = Path(str(value or "")).name.casefold()
    if base.endswith(".exe") or base.endswith(".cmd") or base.endswith(".bat"):
        base = base.rsplit(".", 1)[0]
    return base


def validate_ci_command(command: Iterable[str]) -> list[str]:
    arguments = [str(item) for item in command]
    if not arguments:
        raise CICommandNotAllowed("ci_command_not_allowed")
    executable = _normalize_executable(arguments[0])
    if executable not in ALLOWED_EXECUTABLES:
        raise CICommandNotAllowed("ci_command_not_allowed")
    lowered = [item.casefold() for item in arguments]
    if executable in {"powershell", "pwsh", "cmd", "bash", "sh"}:
        raise CICommandNotAllowed("ci_command_not_allowed")
    if any(item in FORBIDDEN_TOKENS for item in lowered):
        raise CICommandNotAllowed("ci_command_not_allowed")
    if any("-c" == item and executable in {"python", "bash", "sh"} for item in lowered):
        raise CICommandNotAllowed("ci_command_not_allowed")
    if executable == "pip" and len(arguments) >= 2 and arguments[1] in {"install", "uninstall"}:
        raise CICommandNotAllowed("ci_command_not_allowed")
    if executable == "uv" and any(item in {"pip", "add", "remove"} for item in lowered):
        raise CICommandNotAllowed("ci_command_not_allowed")
    return arguments


def validate_ci_relative_path(path: str | None) -> str | None:
    if path is None or str(path).strip() == "":
        return None
    raw = str(path).replace("\\", "/").strip()
    windows = PureWindowsPath(raw)
    if raw.startswith(("/", "//")) or windows.is_absolute() or windows.drive:
        raise CIPathViolation("ci_path_violation")
    parts = PurePosixPath(raw).parts
    if ".." in parts or any(part.casefold() == ".git" for part in parts):
        raise CIPathViolation("ci_path_violation")
    return PurePosixPath(*parts).as_posix()


def pipeline_fingerprint(
    *,
    project_id: str,
    source: Mapping[str, Any],
    pipeline: Mapping[str, Any],
    gates: Iterable[Mapping[str, Any]] = (),
) -> str:
    payload = {
        "project_id": project_id,
        "source": source,
        "pipeline_version": pipeline.get("version"),
        "framework": pipeline.get("framework"),
        "steps": pipeline.get("steps") or [],
        "fail_fast": pipeline.get("fail_fast"),
        "timeout_seconds": pipeline.get("timeout_seconds"),
        "gates": list(gates),
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def build_ci_gate_definitions(
    pipeline: CIPipelineDefinition,
    gate_policy: CIGatePolicy,
) -> list[CIGateDefinition]:
    steps_by_type = {gate_type: [step for step in pipeline.steps if step.type == gate_type] for gate_type in ("build", "test", "lint", "package")}

    def gate(gate_type: str, *, blocking: bool) -> CIGateDefinition:
        steps = steps_by_type[gate_type]
        required = bool(steps) and (gate_type in {"build", "test"} or any(step.required for step in steps))
        minimum = len(steps) if steps and required else None
        return CIGateDefinition(
            gate_id=f"{gate_type}-gate",
            type=gate_type,  # type: ignore[arg-type]
            required=required,
            blocking=blocking and required,
            source_step_types=[gate_type],  # type: ignore[list-item]
            minimum_success_count=minimum,
            allow_skipped=False,
            policy_version=gate_policy.version,
        )

    return [
        gate("build", blocking=gate_policy.build_blocking),
        gate("test", blocking=gate_policy.test_blocking),
        gate("lint", blocking=gate_policy.lint_blocking),
        gate("package", blocking=gate_policy.package_blocking),
    ]


def evaluate_ci_gates(
    pipeline_definition: CIPipelineDefinition,
    step_results: Iterable[CIStepRun],
    gate_policy: CIGatePolicy,
    gate_definitions: Iterable[CIGateDefinition] | None = None,
) -> CIGateEvaluation:
    definitions = list(gate_definitions or build_ci_gate_definitions(pipeline_definition, gate_policy))
    results = list(step_results)
    gates: list[CIGateResult] = []
    for definition in definitions:
        sources = [step for step in results if step.type in definition.source_step_types]
        source_ids = [step.step_id for step in sources]
        if not sources:
            gates.append(CIGateResult(
                gate_id=definition.gate_id, type=definition.type, status="not_applicable",
                required=definition.required, blocking=definition.blocking,
                reason="no_source_steps", source_steps=[],
            ))
            continue
        if any(step.status == "skipped" for step in sources) and not definition.allow_skipped:
            gates.append(CIGateResult(
                gate_id=definition.gate_id, type=definition.type, status="skipped",
                required=definition.required, blocking=definition.blocking,
                reason="source_step_skipped", source_steps=source_ids,
            ))
            continue
        success_count = sum(1 for step in sources if step.status == "passed")
        minimum = definition.minimum_success_count or len(sources)
        failed = [step for step in sources if step.status in {"failed", "timed_out", "cancelled", "interrupted"}]
        if success_count >= minimum:
            status = "passed"
            reason = "minimum_success_count_met"
            failure_type = None
        elif failed:
            status = "failed" if definition.blocking else "warning"
            reason = "blocking_source_step_failed" if definition.blocking else "non_blocking_source_step_failed"
            failure_type = f"ci_{definition.type}_gate_failed"
        else:
            status = "failed" if definition.required and definition.blocking else "warning" if definition.required else "not_applicable"
            reason = "minimum_success_count_not_met"
            failure_type = f"ci_{definition.type}_gate_failed" if status in {"failed", "warning"} else None
        gates.append(CIGateResult(
            gate_id=definition.gate_id,
            type=definition.type,
            status=status,  # type: ignore[arg-type]
            required=definition.required,
            blocking=definition.blocking,
            reason=reason,
            source_steps=source_ids,
            failure_type=failure_type,
        ))
    failed_gates = [gate.gate_id for gate in gates if gate.status == "failed"]
    warning_gates = [gate.gate_id for gate in gates if gate.status == "warning"]
    blocking_gate = next((gate.gate_id for gate in gates if gate.status == "failed" and gate.blocking), None)
    if blocking_gate:
        decision = "rejected"
    elif warning_gates:
        decision = "accepted_with_warnings"
    else:
        decision = "accepted"
    summary = {
        "total": len(gates),
        "passed": sum(1 for gate in gates if gate.status == "passed"),
        "failed": sum(1 for gate in gates if gate.status == "failed"),
        "warning": sum(1 for gate in gates if gate.status == "warning"),
        "skipped": sum(1 for gate in gates if gate.status == "skipped"),
        "not_applicable": sum(1 for gate in gates if gate.status == "not_applicable"),
        "blocking_failed_count": sum(1 for gate in gates if gate.status == "failed" and gate.blocking),
    }
    failure_type = next((gate.failure_type for gate in gates if gate.gate_id == blocking_gate), None)
    return CIGateEvaluation(
        policy_version=gate_policy.version,
        gates=gates,
        decision=decision,  # type: ignore[arg-type]
        failed_gates=failed_gates,
        warning_gates=warning_gates,
        blocking_gate=blocking_gate,
        failure_type=failure_type,
        summary=summary,
    )


def evaluate_ci_promotion_eligibility(
    git_state: Mapping[str, Any],
    ci_run: Mapping[str, Any] | CIPipelineRun | None,
    policy: CIPromotionPolicy,
) -> CIPromotionEligibility:
    target_commit = normalize_commit_id(
        git_state.get("workflow_head")
        or git_state.get("commit_sha")
        or git_state.get("head_commit")
        or None
    )
    if not policy.required:
        return CIPromotionEligibility(
            required=False,
            eligible=True,
            reason="ci_not_required",
            commit_match=True,
            target_commit=target_commit,
            metadata={"policy_version": policy.version},
        )
    if not target_commit:
        return CIPromotionEligibility(
            required=True,
            eligible=False,
            reason="ci_target_commit_missing",
            commit_match=False,
            target_commit=None,
            metadata={"policy_version": policy.version},
        )
    if ci_run is None:
        return CIPromotionEligibility(
            required=True,
            eligible=False,
            reason="ci_required_for_promotion",
            commit_match=False,
            target_commit=target_commit,
            metadata={"policy_version": policy.version},
        )
    run = ci_run.model_dump() if isinstance(ci_run, CIPipelineRun) else dict(ci_run)
    source = run.get("source") or {}
    source_mode = str(source.get("source_mode") or "").strip() or None
    source_commit = normalize_commit_id(source.get("source_commit") or run.get("ci_validated_commit") or None)
    commit_match = source_commit == target_commit
    pipeline = run.get("pipeline") or {}
    decision = run.get("decision")
    warnings = list(run.get("warning_gates") or run.get("warnings") or [])
    blocking = list(run.get("failed_gates") or ([] if not run.get("blocking_gate") else [run.get("blocking_gate")]))
    common = {
        "required": True,
        "commit_match": commit_match,
        "source_commit": source_commit,
        "target_commit": target_commit,
        "run_id": run.get("ci_run_id"),
        "ci_status": run.get("status"),
        "ci_decision": decision,
        "blocking_gates": blocking,
        "warnings": warnings,
        "gate_policy_version": run.get("gate_policy_version"),
        "pipeline_version": pipeline.get("version"),
        "pipeline_fingerprint": run.get("pipeline_fingerprint"),
        "metadata": {
            "policy_version": policy.version,
            "failure_type": run.get("failure_type"),
            "failed_step": run.get("failed_step"),
            "source_mode": source_mode,
            "source_commit_valid": source_commit is not None,
        },
    }
    if source_mode != "commit" or source_commit is None:
        return CIPromotionEligibility(eligible=False, reason="ci_required_for_promotion", **common)
    if not commit_match:
        return CIPromotionEligibility(eligible=False, reason="ci_commit_mismatch", **common)
    if run.get("status") not in {"passed", "failed"} or not decision:
        return CIPromotionEligibility(eligible=False, reason="ci_run_not_completed", **common)
    if decision == "rejected":
        return CIPromotionEligibility(eligible=False, reason="ci_promotion_blocked", **common)
    if decision == "accepted_with_warnings" and not policy.allow_warnings:
        return CIPromotionEligibility(eligible=False, reason="ci_warnings_blocked", **common)
    reason = "ci_accepted_with_warnings" if decision == "accepted_with_warnings" else "ci_accepted"
    return CIPromotionEligibility(eligible=True, reason=reason, **common)


INFRASTRUCTURE_CI_FAILURES = frozenset({
    "ci_step_timeout",
    "ci_pipeline_timeout",
    "ci_environment_unavailable",
    "ci_execution_interrupted",
})
CONFIGURATION_CI_FAILURES = frozenset({
    "ci_command_not_allowed",
    "ci_execution_context_invalid",
    "ci_path_violation",
    "ci_pipeline_stale",
})


def _bounded_failure_summary(step: CIStepRun | None, run: Mapping[str, Any] | CIPipelineRun) -> str | None:
    if step is None:
        message = run.failure_message if isinstance(run, CIPipelineRun) else run.get("failure_message")
        return str(message)[:1000] if message else None
    text = "\n".join(
        item for item in (step.failure_message, step.stdout_summary, step.stderr_summary) if item
    ).strip()
    return text[:1000] if text else None


def classify_ci_failure_for_repair(
    ci_run: Mapping[str, Any] | CIPipelineRun,
    gate_evaluation: CIGateEvaluation | None = None,
    workflow_state: Mapping[str, Any] | None = None,
) -> CIRepairability:
    if isinstance(ci_run, CIPipelineRun):
        run = ci_run
        raw_run: Mapping[str, Any] = run.model_dump(mode="json")
    else:
        raw_run = ci_run
        if raw_run.get("decision") != "rejected":
            return CIRepairability(
                repairable=False,
                category="non_repairable",
                confidence=1.0,
                reason_codes=["ci_decision_not_rejected"],
                failure_type=raw_run.get("failure_type"),
                summary=_bounded_failure_summary(None, raw_run),
            )
        run = CIPipelineRun.model_validate(raw_run)
    if run.decision != "rejected":
        return CIRepairability(
            repairable=False,
            category="non_repairable",
            confidence=1.0,
            reason_codes=["ci_decision_not_rejected"],
            failure_type=run.failure_type,
            summary=_bounded_failure_summary(None, run),
        )
    failure_type = run.failure_type
    if failure_type in INFRASTRUCTURE_CI_FAILURES or run.status in {"timed_out", "interrupted"}:
        return CIRepairability(
            repairable=False,
            category="infrastructure",
            confidence=0.95,
            reason_codes=["ci_infrastructure_failure"],
            failed_gate=run.blocking_gate,
            failed_step=run.failed_step,
            failure_type=failure_type,
            summary=_bounded_failure_summary(None, run),
        )
    if failure_type in CONFIGURATION_CI_FAILURES:
        return CIRepairability(
            repairable=False,
            category="configuration",
            confidence=0.9,
            reason_codes=["ci_configuration_failure"],
            failed_gate=run.blocking_gate,
            failed_step=run.failed_step,
            failure_type=failure_type,
            summary=_bounded_failure_summary(None, run),
        )
    failed_gate = run.blocking_gate or next(iter(run.failed_gates), None)
    failed_step = next((step for step in run.steps if step.step_id == run.failed_step), None)
    if failed_step is None and failed_gate:
        gate_type = failed_gate.replace("-gate", "")
        failed_step = next((step for step in run.steps if step.type == gate_type and step.status in {"failed", "timed_out", "interrupted"}), None)
    gate_type = (
        str(failed_step.type)
        if failed_step is not None
        else str(failed_gate or "").replace("-gate", "")
    )
    if gate_type == "test" or failure_type in {"ci_tests_failed", "ci_test_gate_failed"}:
        return CIRepairability(
            repairable=True,
            category="repairable_tests",
            confidence=0.85,
            reason_codes=["ci_test_gate_failed", "ci_environment_executed"],
            failed_gate=failed_gate,
            failed_step=failed_step.step_id if failed_step else run.failed_step,
            failure_type=failure_type,
            summary=_bounded_failure_summary(failed_step, run),
        )
    if gate_type == "build" or failure_type in {"ci_build_failed", "ci_build_gate_failed"}:
        summary = (_bounded_failure_summary(failed_step, run) or "").casefold()
        infra_signal = any(token in summary for token in ("timeout", "unavailable", "network", "registry"))
        return CIRepairability(
            repairable=not infra_signal,
            category="infrastructure" if infra_signal else "repairable_build",
            confidence=0.75 if not infra_signal else 0.8,
            reason_codes=["ci_build_gate_failed"] if not infra_signal else ["ci_build_infrastructure_signal"],
            failed_gate=failed_gate,
            failed_step=failed_step.step_id if failed_step else run.failed_step,
            failure_type=failure_type,
            summary=_bounded_failure_summary(failed_step, run),
        )
    if gate_type == "lint" or failure_type in {"ci_lint_failed", "ci_lint_gate_failed"}:
        return CIRepairability(
            repairable=True,
            category="repairable_lint",
            confidence=0.8,
            reason_codes=["ci_lint_gate_failed"],
            failed_gate=failed_gate,
            failed_step=failed_step.step_id if failed_step else run.failed_step,
            failure_type=failure_type,
            summary=_bounded_failure_summary(failed_step, run),
        )
    return CIRepairability(
        repairable=False,
        category="unknown",
        confidence=0.3,
        reason_codes=["ci_failure_unknown"],
        failed_gate=failed_gate,
        failed_step=run.failed_step,
        failure_type=failure_type,
        summary=_bounded_failure_summary(failed_step, run),
    )


class CIPipelineService:
    def __init__(
        self,
        workspace_root: Path,
        *,
        store: CIPipelineStore,
        policy: CIPipelinePolicy | None = None,
        gate_policy: CIGatePolicy | None = None,
        promotion_policy: CIPromotionPolicy | None = None,
        observability: Any | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.store = store
        self.policy = policy or CIPipelinePolicy.from_environment()
        self.gate_policy = gate_policy or CIGatePolicy.from_environment()
        self.promotion_policy = promotion_policy or CIPromotionPolicy.from_environment()
        self.observability = observability

    async def initialize(self) -> None:
        await self.store.initialize()
        await self.store.reconcile_interrupted()

    def _project_root(self, project_id: str) -> Path:
        root = (self.workspace_root / project_id).resolve()
        try:
            root.relative_to(self.workspace_root)
        except ValueError as exc:
            raise CIPathViolation("ci_path_violation") from exc
        if not root.is_dir():
            raise CIProjectUnavailable("ci_project_unavailable")
        return root

    def _execution_repository_root(
        self, project_id: str, repository_root: str | None,
    ) -> tuple[Path, str]:
        project_root = self._project_root(project_id)
        relative = validate_ci_relative_path(repository_root or project_id)
        if not relative:
            raise CIPathViolation("ci_path_violation")
        resolved = (self.workspace_root / relative).resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError as exc:
            raise CIPathViolation("ci_path_violation") from exc
        if resolved != project_root:
            raise CIPathViolation("ci_path_violation")
        return resolved, relative

    async def workflow_status(
        self,
        workflow_id: str,
        project_id: str | None,
        *,
        target_commit: str | None = None,
    ) -> CIWorkflowStatus:
        latest = await self.store.latest_run(workflow_id)
        if not project_id:
            return CIWorkflowStatus(workflow_id=workflow_id, project_id=None, state="project_unavailable")
        try:
            preview = await self.prepare(workflow_id, project_id, source_commit=target_commit)
        except CIProjectUnavailable:
            return CIWorkflowStatus(workflow_id=workflow_id, project_id=project_id, state="project_unavailable")
        eligibility = None
        if target_commit:
            eligibility = await self.promotion_eligibility(workflow_id, target_commit)
        latest_repairability = (latest or {}).get("repairability") or {}
        return CIWorkflowStatus(
            workflow_id=workflow_id,
            project_id=project_id,
            state="available" if latest else "not_started",
            latest_run=CIPipelineRun.model_validate(latest) if latest else None,
            pipeline=preview.pipeline,
            pipeline_fingerprint=preview.pipeline_fingerprint,
            source=preview.source,
            promotion_eligible=eligibility.eligible if eligibility else None,
            promotion_eligibility=eligibility.model_dump() if eligibility else None,
            repair=CIRepairStatus(
                state="pending" if latest_repairability.get("repairable") else "not_required",
                attempts=0,
                max_attempts=int(os.getenv("CI_REPAIR_MAX_ATTEMPTS", "2")),
                source_run_id=latest.get("ci_run_id") if latest else None,
                source_commit=(latest.get("source") or {}).get("source_commit") if latest else None,
                category=latest_repairability.get("category") or None,
                reason_codes=latest_repairability.get("reason_codes") or [],
                repairability=(CIRepairability.model_validate(latest_repairability) if latest_repairability else None),
            ),
        )

    async def prepare(
        self,
        workflow_id: str,
        project_id: str,
        *,
        source_commit: str | None = None,
        source_branch: str | None = None,
        workflow_branch: str | None = None,
        repository_root: str | None = None,
    ) -> CIPipelinePreview:
        root, normalized_repository_root = self._execution_repository_root(
            project_id, repository_root
        )
        framework = self.detect_framework(root)
        pipeline = self.default_pipeline(root, framework)
        source = self.source_revision(
            root,
            source_commit=source_commit,
            source_branch=source_branch,
            workflow_branch=workflow_branch,
            repository_root=normalized_repository_root,
        )
        expected_gates = build_ci_gate_definitions(pipeline, self.gate_policy)
        fingerprint = pipeline_fingerprint(
            project_id=project_id,
            source=source.model_dump(),
            pipeline=pipeline.model_dump(),
            gates=[gate.model_dump() for gate in expected_gates],
        )
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="ci",
            stage="ci_pipeline",
            status=EventStatus.COMPLETED,
            data={"event": "ci_pipeline_prepared", "workflow_id": workflow_id, "pipeline_fingerprint": fingerprint, "framework": framework},
        )
        return CIPipelinePreview(
            workflow_id=workflow_id,
            project_id=project_id,
            state="available",
            pipeline=pipeline,
            pipeline_fingerprint=fingerprint,
            source=source,
            expected_gates=expected_gates,
            gate_policy_version=self.gate_policy.version,
        )

    async def run(
        self,
        workflow_id: str,
        project_id: str,
        *,
        expected_fingerprint: str,
        ci_run_id: str | None = None,
        source_commit: str | None = None,
        source_branch: str | None = None,
        workflow_branch: str | None = None,
        repository_root: str | None = None,
    ) -> CIPipelineRun:
        existing = await self.store.get_run(ci_run_id) if ci_run_id else None
        if existing and existing.get("status") in {"passed", "failed", "timed_out", "cancelled", "interrupted"}:
            return CIPipelineRun.model_validate(existing)
        preview = await self.prepare(
            workflow_id,
            project_id,
            source_commit=source_commit,
            source_branch=source_branch,
            workflow_branch=workflow_branch,
            repository_root=repository_root,
        )
        if preview.pipeline_fingerprint != expected_fingerprint:
            raise CIPipelineStale("ci_pipeline_stale")
        run_id = ci_run_id or uuid4().hex
        run_data = {
            "ci_run_id": run_id,
            "workflow_id": workflow_id,
            "project_id": project_id,
            "pipeline": preview.pipeline.model_dump(),
            "pipeline_fingerprint": preview.pipeline_fingerprint,
            "status": "running",
            "source": preview.source.model_dump(),
            "started_at": _utc(),
            "completed_at": None,
            "duration_seconds": None,
            "steps": [
                CIStepRun(step_id=step.step_id, name=step.name, type=step.type, status="pending").model_dump()
                for step in preview.pipeline.steps
            ],
            "warnings": [],
            "gate_policy_version": preview.gate_policy_version,
            "gates": [],
            "decision": None,
            "failed_gates": [],
            "warning_gates": [],
            "blocking_gate": None,
            "gate_summary": {},
            "ci_validated_commit": preview.source.source_commit if preview.source.source_mode == "commit" else None,
        }
        created = await self.store.create_run(run_data)
        if created.get("status") != "running":
            return CIPipelineRun.model_validate(created)
        root, _normalized_repository_root = self._execution_repository_root(
            project_id, preview.source.repository_root
        )
        started = perf_counter()
        emit_workflow_event(
            WorkflowEventType.STAGE_STARTED,
            source="ci",
            stage="ci_pipeline",
            status=EventStatus.RUNNING,
            data={"event": "ci_pipeline_started", "workflow_id": workflow_id, "ci_run_id": run_id, "source_commit": preview.source.source_commit},
        )
        failed_step: str | None = None
        failure_type: str | None = None
        failure_message: str | None = None
        warnings: list[str] = []
        final_status = "passed"
        skip_remaining = False

        for step in preview.pipeline.steps:
            elapsed = perf_counter() - started
            if elapsed >= preview.pipeline.timeout_seconds:
                final_status = "timed_out"
                failed_step = step.step_id
                failure_type = "ci_pipeline_timeout"
                failure_message = "CI pipeline exceeded its timeout."
                await self._skip_pending(run_id, preview.pipeline.steps, step.step_id)
                break
            if skip_remaining:
                await self.store.upsert_step(run_id, CIStepRun(
                    step_id=step.step_id, name=step.name, type=step.type, status="skipped",
                    failure_type=failure_type, failure_message="Skipped because a required previous step failed.",
                ).model_dump())
                continue
            step_result = await self._execute_step(
                run_id=run_id,
                root=root,
                step=step,
                remaining_seconds=preview.pipeline.timeout_seconds - elapsed,
                workflow_id=workflow_id,
            )
            if step_result.status in {"failed", "timed_out"}:
                if step.continue_on_error and not step.required:
                    warnings.append(f"{step.step_id}:{step_result.failure_type or step_result.status}")
                    continue
                failed_step = step.step_id
                failure_type = step_result.failure_type
                failure_message = step_result.failure_message
                final_status = "timed_out" if step_result.status == "timed_out" and failure_type == "ci_pipeline_timeout" else "failed"
                if preview.pipeline.fail_fast and step.required:
                    skip_remaining = True
        completed_at = _utc()
        latest = await self.store.get_run(run_id)
        step_results = [CIStepRun.model_validate(step) for step in (latest or {}).get("steps", [])]
        gate_evaluation = evaluate_ci_gates(
            preview.pipeline,
            step_results,
            self.gate_policy,
            preview.expected_gates,
        )
        if gate_evaluation.decision == "rejected" and not failure_type:
            final_status = "failed"
            failed_step = gate_evaluation.blocking_gate
            failure_type = gate_evaluation.failure_type
            failure_message = f"Blocking CI gate failed: {gate_evaluation.blocking_gate}."
        repairability = None
        if gate_evaluation.decision == "rejected":
            repairability = classify_ci_failure_for_repair(
                {
                    **(latest or {}),
                    "status": final_status,
                    "failed_step": failed_step,
                    "failure_type": failure_type,
                    "failure_message": failure_message,
                    "gate_policy_version": gate_evaluation.policy_version,
                    "gates": [gate.model_dump() for gate in gate_evaluation.gates],
                    "decision": gate_evaluation.decision,
                    "failed_gates": gate_evaluation.failed_gates,
                    "warning_gates": gate_evaluation.warning_gates,
                    "blocking_gate": gate_evaluation.blocking_gate,
                    "gate_summary": gate_evaluation.summary,
                    "ci_validated_commit": preview.source.source_commit if preview.source.source_mode == "commit" else None,
                },
                gate_evaluation,
            )
        final = await self.store.update_run(
            run_id,
            {
                "status": final_status,
                "failed_step": failed_step,
                "failure_type": failure_type,
                "failure_message": failure_message,
                "warnings": warnings,
                "gate_policy_version": gate_evaluation.policy_version,
                "gates": [gate.model_dump() for gate in gate_evaluation.gates],
                "decision": gate_evaluation.decision,
                "failed_gates": gate_evaluation.failed_gates,
                "warning_gates": gate_evaluation.warning_gates,
                "blocking_gate": gate_evaluation.blocking_gate,
                "gate_summary": gate_evaluation.summary,
                "ci_validated_commit": preview.source.source_commit if preview.source.source_mode == "commit" else None,
                "repairability": repairability.model_dump() if repairability else None,
                "completed_at": completed_at,
                "duration_seconds": round(perf_counter() - started, 4),
            },
        )
        for gate in gate_evaluation.gates:
            emit_workflow_event(
                WorkflowEventType.STAGE_COMPLETED if gate.status in {"passed", "not_applicable", "skipped"} else WorkflowEventType.STAGE_FAILED,
                source="ci",
                stage="ci_gate",
                status=EventStatus.COMPLETED if gate.status in {"passed", "not_applicable", "skipped"} else EventStatus.FAILED,
                data={
                    "event": "ci_gate_evaluated" if gate.status not in {"failed", "warning"} else "ci_gate_failed" if gate.status == "failed" else "ci_gate_warning",
                    "workflow_id": workflow_id,
                    "ci_run_id": run_id,
                    "gate_id": gate.gate_id,
                    "gate_type": gate.type,
                    "status": gate.status,
                    "blocking": gate.blocking,
                    "policy_version": gate_evaluation.policy_version,
                },
            )
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED if gate_evaluation.decision != "rejected" else WorkflowEventType.STAGE_FAILED,
            source="ci",
            stage="ci_decision",
            status=EventStatus.COMPLETED if gate_evaluation.decision != "rejected" else EventStatus.FAILED,
            data={
                "event": "ci_decision_completed",
                "workflow_id": workflow_id,
                "ci_run_id": run_id,
                "decision": gate_evaluation.decision,
                "blocking_gate": gate_evaluation.blocking_gate,
                "policy_version": gate_evaluation.policy_version,
            },
        )
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED if final_status == "passed" else WorkflowEventType.STAGE_FAILED,
            source="ci",
            stage="ci_pipeline",
            status=EventStatus.COMPLETED if final_status == "passed" else EventStatus.FAILED,
            data={
                "event": "ci_pipeline_completed" if final_status == "passed" else "ci_pipeline_failed",
                "workflow_id": workflow_id,
                "ci_run_id": run_id,
                "status": final_status,
                "duration_seconds": final.get("duration_seconds"),
                "failure_type": failure_type,
                "source_commit": preview.source.source_commit,
            },
        )
        return CIPipelineRun.model_validate(final)

    async def _skip_pending(self, run_id: str, steps: list[CIPipelineStep], current_step_id: str) -> None:
        found = False
        for step in steps:
            if step.step_id == current_step_id:
                found = True
                continue
            if found:
                await self.store.upsert_step(run_id, CIStepRun(
                    step_id=step.step_id, name=step.name, type=step.type, status="skipped",
                    failure_type="ci_pipeline_timeout",
                    failure_message="Skipped because the pipeline timed out.",
                ).model_dump())

    async def _execute_step(
        self,
        *,
        run_id: str,
        root: Path,
        step: CIPipelineStep,
        remaining_seconds: float,
        workflow_id: str,
    ) -> CIStepRun:
        command = validate_ci_command(step.command)
        workdir = root / (validate_ci_relative_path(step.working_directory) or "")
        workdir = workdir.resolve()
        try:
            workdir.relative_to(root)
        except ValueError as exc:
            raise CIPathViolation("ci_path_violation") from exc
        timeout = min(float(step.timeout_seconds or self.policy.step_timeout_seconds), max(0.1, remaining_seconds))
        started = _utc()
        await self.store.upsert_step(run_id, CIStepRun(
            step_id=step.step_id, name=step.name, type=step.type, status="running", started_at=started,
        ).model_dump())
        emit_workflow_event(
            WorkflowEventType.STAGE_STARTED,
            source="ci",
            stage="ci_step",
            status=EventStatus.RUNNING,
            data={"event": "ci_step_started", "workflow_id": workflow_id, "ci_run_id": run_id, "step_id": step.step_id, "step_type": step.type},
        )
        started_perf = perf_counter()
        try:
            with self._isolated_execution_command(command) as execution_command:
                completed = await asyncio.to_thread(
                    self._run_command, execution_command, workdir, timeout
                )
            duration = round(perf_counter() - started_perf, 4)
            stdout, stdout_truncated = self._summarize_output(completed.stdout)
            stderr, stderr_truncated = self._summarize_output(completed.stderr)
            context_invalid = self._execution_context_invalid(
                execution_command, workdir, completed.stdout, completed.stderr
            )
            failure = (
                "ci_execution_context_invalid"
                if context_invalid
                else None if completed.returncode == 0 else self._failure_type(step)
            )
            result = CIStepRun(
                step_id=step.step_id,
                name=step.name,
                type=step.type,
                status="passed" if completed.returncode == 0 and not context_invalid else "failed",
                started_at=started,
                completed_at=_utc(),
                duration_seconds=duration,
                exit_code=completed.returncode,
                stdout_summary=stdout,
                stderr_summary=stderr,
                output_truncated=stdout_truncated or stderr_truncated,
                failure_type=failure,
                failure_message=(
                    "CI test execution root did not match the project repository root."
                    if context_invalid
                    else None if completed.returncode == 0
                    else f"{step.name} exited with code {completed.returncode}."
                ),
            )
        except subprocess.TimeoutExpired:
            duration = round(perf_counter() - started_perf, 4)
            pipeline_timeout = timeout >= remaining_seconds - 0.001
            result = CIStepRun(
                step_id=step.step_id,
                name=step.name,
                type=step.type,
                status="timed_out",
                started_at=started,
                completed_at=_utc(),
                duration_seconds=duration,
                failure_type="ci_pipeline_timeout" if pipeline_timeout else "ci_step_timeout",
                failure_message=f"{step.name} exceeded its timeout.",
            )
        await self.store.upsert_step(run_id, result.model_dump())
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED if result.status == "passed" else WorkflowEventType.STAGE_FAILED,
            source="ci",
            stage="ci_step",
            status=EventStatus.COMPLETED if result.status == "passed" else EventStatus.FAILED,
            data={
                "event": "ci_step_completed" if result.status == "passed" else "ci_step_failed",
                "workflow_id": workflow_id,
                "ci_run_id": run_id,
                "step_id": step.step_id,
                "step_type": step.type,
                "duration_seconds": result.duration_seconds,
                "exit_code": result.exit_code,
                "status": result.status,
                "failure_type": result.failure_type,
            },
        )
        return result

    @staticmethod
    def _run_command(command: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[bytes]:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return subprocess.run(
            command,
            cwd=str(cwd),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=creationflags,
        )

    @staticmethod
    def _is_pytest_command(command: list[str]) -> bool:
        executable = _normalize_executable(command[0]) if command else ""
        lowered = [item.casefold() for item in command]
        return executable == "pytest" or (
            executable == "python" and len(lowered) >= 3
            and lowered[1:3] == ["-m", "pytest"]
        )

    @contextmanager
    def _isolated_execution_command(self, command: list[str]):
        execution_command = list(command)
        if not self._is_pytest_command(execution_command):
            yield execution_command
            return
        with tempfile.TemporaryDirectory(prefix="mcp-ci-pytest-") as directory:
            config = Path(directory) / "pytest.ini"
            config.write_text("[pytest]\n", encoding="ascii")
            if not any(item == "-c" or item.startswith("--config-file") for item in execution_command):
                execution_command.extend(["-c", str(config)])
            if not any(item == "--rootdir" or item.startswith("--rootdir=") for item in execution_command):
                execution_command.append("--rootdir=.")
            yield execution_command

    @classmethod
    def _execution_context_invalid(
        cls,
        command: list[str],
        expected_root: Path,
        stdout: bytes,
        stderr: bytes,
    ) -> bool:
        if not cls._is_pytest_command(command):
            return False
        output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
        match = PYTEST_ROOTDIR_RE.search(output)
        if not match:
            return False
        reported = Path(match.group(1).strip()).resolve()
        return reported != expected_root.resolve()

    def _summarize_output(self, data: bytes) -> tuple[str, bool]:
        text = data.decode("utf-8", errors="replace")
        sanitized = str(sanitize_value(text))
        truncated = len(sanitized) > self.policy.output_max_chars
        if truncated:
            sanitized = sanitized[: self.policy.output_max_chars] + "...[TRUNCATED]"
        return sanitized, truncated

    @staticmethod
    def _failure_type(step: CIPipelineStep) -> str:
        if step.type == "build":
            return "ci_build_failed"
        if step.type == "test":
            return "ci_tests_failed"
        if step.type == "lint":
            return "ci_lint_failed"
        if step.type == "package":
            return "ci_package_failed"
        return "ci_step_failed"

    def detect_framework(self, root: Path) -> str:
        if (root / "package.json").is_file():
            return "node"
        if any(root.glob("*.sln")) or any(root.rglob("*.csproj")):
            return "dotnet"
        if (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file() or any(root.glob("pytest.ini")):
            return "fastapi"
        return "python"

    def default_pipeline(self, root: Path, framework: str) -> CIPipelineDefinition:
        if framework == "node":
            steps = self._node_steps(root)
        elif framework == "dotnet":
            steps = [
                CIPipelineStep(step_id="restore", name="Restore", type="build", command=["dotnet", "restore"]),
                CIPipelineStep(step_id="build", name="Build", type="build", command=["dotnet", "build", "--no-restore"]),
                CIPipelineStep(step_id="test", name="Test", type="test", command=["dotnet", "test", "--no-build"]),
            ]
        else:
            steps = self._python_steps(root)
        for step in steps:
            validate_ci_command(step.command)
            validate_ci_relative_path(step.working_directory)
        return CIPipelineDefinition(
            pipeline_id=f"{framework}-default",
            name=f"{framework.title()} default CI",
            version=self.policy.version,
            framework=framework,
            steps=steps,
            fail_fast=True,
            timeout_seconds=self.policy.pipeline_timeout_seconds,
        )

    def _python_steps(self, root: Path) -> list[CIPipelineStep]:
        steps: list[CIPipelineStep] = []
        python = "python"
        for candidate in (
            Path(".venv/Scripts/python.exe"),
            Path(".venv/bin/python"),
            Path("venv/Scripts/python.exe"),
            Path("venv/bin/python"),
        ):
            if (root / candidate).is_file():
                python = candidate.as_posix()
                break
        if (root / ".venv").exists() or (root / "venv").exists():
            steps.append(CIPipelineStep(
                step_id="environment",
                name="Validate environment",
                type="build",
                command=[python, "--version"],
                required=True,
            ))
        elif not ((root / "requirements.txt").exists() or (root / "pyproject.toml").exists()):
            raise CIEnvironmentUnavailable("ci_environment_unavailable")
        steps.append(CIPipelineStep(
            step_id="test",
            name="Run tests",
            type="test",
            command=[python, "-m", "pytest", "--rootdir=."],
        ))
        return steps

    def _node_steps(self, root: Path) -> list[CIPipelineStep]:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts") if isinstance(package, dict) else {}
        if not isinstance(scripts, dict):
            scripts = {}
        steps: list[CIPipelineStep] = []
        if "build" in scripts:
            steps.append(CIPipelineStep(step_id="build", name="Build", type="build", command=["npm", "run", "build"]))
        if "test" in scripts:
            steps.append(CIPipelineStep(step_id="test", name="Test", type="test", command=["npm", "test"]))
        if "lint" in scripts:
            steps.append(CIPipelineStep(step_id="lint", name="Lint", type="lint", command=["npm", "run", "lint"], required=False, continue_on_error=True))
        if not steps:
            raise CIEnvironmentUnavailable("ci_environment_unavailable")
        return steps

    def source_revision(
        self,
        root: Path,
        *,
        source_commit: str | None = None,
        source_branch: str | None = None,
        workflow_branch: str | None = None,
        repository_root: str | None = None,
    ) -> CISourceRevision:
        if source_commit:
            if not SHA_RE.fullmatch(source_commit):
                raise CIPipelineStale("ci_pipeline_stale")
            return CISourceRevision(
                source_mode="commit",
                source_commit=source_commit,
                source_branch=source_branch,
                workflow_branch=workflow_branch,
                repository_root=repository_root,
                dirty=False,
                effective_clean=True,
            )
        if not (root / ".git").exists():
            return CISourceRevision(
                source_mode="working_tree",
                repository_root=repository_root,
                dirty=None,
                effective_clean=None,
            )
        branch = self._git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
        commit = self._git(root, ["rev-parse", "HEAD"])
        status = self._git(root, ["status", "--porcelain"])
        dirty = bool(status.strip())
        lines = [line for line in status.splitlines() if line.strip()]
        parsed = {
            "staged": [line[3:] for line in lines if line[:2].strip() and not line.startswith("??")],
            "modified": [line[3:] for line in lines if line.startswith(" M")],
            "deleted": [line[3:] for line in lines if line.startswith(" D")],
            "untracked": [line[3:] for line in lines if line.startswith("??")],
        }
        classified = classify_dirty_paths(root, parsed, [])
        effective_clean = not classified.unrelated_files and not parsed["staged"] and not parsed["modified"] and not parsed["deleted"]
        return CISourceRevision(
            source_mode="commit" if commit and effective_clean else "working_tree",
            source_commit=commit or None,
            source_branch=None if branch == "HEAD" else branch,
            workflow_branch=workflow_branch,
            repository_root=repository_root,
            dirty=dirty,
            effective_clean=effective_clean,
        )

    @staticmethod
    def _git(root: Path, args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(root),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except Exception:
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", errors="replace").strip()

    async def get_run(self, workflow_id: str, ci_run_id: str) -> CIPipelineRun | None:
        run = await self.store.get_run(ci_run_id)
        if run is None or run.get("workflow_id") != workflow_id:
            return None
        return CIPipelineRun.model_validate(run)

    async def latest_completed_run_for_commit(
        self,
        workflow_id: str,
        source_commit: str,
    ) -> CIPipelineRun | None:
        run = await self.store.latest_completed_run_for_commit(workflow_id, source_commit)
        return CIPipelineRun.model_validate(run) if run else None

    async def promotion_eligibility(
        self,
        workflow_id: str,
        target_commit: str | None,
    ) -> CIPromotionEligibility:
        run = (
            await self.latest_completed_run_for_commit(workflow_id, target_commit)
            if target_commit
            else None
        )
        return evaluate_ci_promotion_eligibility(
            {"workflow_head": target_commit},
            run,
            self.promotion_policy,
        )

    async def list_runs(self, workflow_id: str, *, limit: int = 20, status: str | None = None) -> CIRunListResponse:
        runs = await self.store.list_runs(workflow_id, limit=limit, status=status)
        return CIRunListResponse(
            workflow_id=workflow_id,
            runs=[CIPipelineRun.model_validate(run) for run in runs],
            total=len(runs),
        )
