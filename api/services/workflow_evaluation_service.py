from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import os
from typing import Iterable, Sequence

from api.evaluation_models import (
    EvaluationCategory,
    EvaluationFinding,
    EvaluationPenalty,
    EvaluationPositiveSignal,
    RequirementCoverage,
    RequirementCoverageItem,
    StageDuration,
    WorkflowDurationBreakdown,
    WorkflowEvaluationResponse,
)
from api.execution_models import WorkflowAgentExecutionResponse, WorkflowTask
from api.services.workflow_evaluation_store import WorkflowEvaluationStore
from api.services.workflow_execution_service import WorkflowExecutionService
from streaming.models import WorkflowEvent


SCORING_VERSION = "1.2"
PLANNING_MAX = 20
IMPLEMENTATION_MAX = 25
TESTING_MAX = 25
EFFICIENCY_MAX = 15
RELIABILITY_MAX = 15
MAX_SCORE = (
    PLANNING_MAX
    + IMPLEMENTATION_MAX
    + TESTING_MAX
    + EFFICIENCY_MAX
    + RELIABILITY_MAX
)
NORMAL_APPROVAL_COUNT = 3
HIGH_DURATION_SECONDS = max(
    int(os.getenv("WORKFLOW_EVALUATION_HIGH_DURATION_SECONDS", "600")),
    60,
)
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "tests_failed",
    "planning_failed",
    "implementation_failed",
    "infrastructure_failed",
    "repair_limit_reached",
    "user_cancelled",
    "supervisor_loop_detected",
}
SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}


def grade_for_score(score: int) -> str:
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "good"
    if score >= 60:
        return "acceptable"
    if score >= 40:
        return "poor"
    return "critical"


def _retry_count(total_attempts: int) -> int:
    return max(total_attempts - 1, 0)


def _attempt_message(count: int, stage: str) -> str:
    noun = "intento" if count == 1 else "intentos"
    adjective = "adicional" if count == 1 else "adicionales"
    return f"{count} {noun} {adjective} de {stage}."


@dataclass
class _Category:
    name: str
    maximum: int
    evaluation_state: str = "evaluated"
    available_points: int | None = None
    score: int = field(init=False)
    penalties: list[EvaluationPenalty] = field(default_factory=list)
    positive_signals: list[EvaluationPositiveSignal] = field(default_factory=list)
    findings: list[EvaluationFinding] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.available_points is None:
            self.available_points = (
                0 if self.evaluation_state == "not_started" else self.maximum
            )
        self.available_points = min(max(self.available_points, 0), self.maximum)
        self.score = self.available_points

    def penalty(
        self,
        code: str,
        points: int,
        message: str,
        severity: str = "warning",
        **links: str | None,
    ) -> None:
        applied = min(max(points, 0), self.score)
        self.score -= applied
        self.penalties.append(EvaluationPenalty(
            code=code,
            category=self.name,
            points=-applied,
            message=message,
            severity=severity,
            **links,
        ))

    def signal(
        self,
        code: str,
        message: str,
        **links: str | None,
    ) -> None:
        self.positive_signals.append(EvaluationPositiveSignal(
            code=code,
            category=self.name,
            message=message,
            **links,
        ))

    def finding(
        self,
        code: str,
        title: str,
        description: str,
        severity: str,
        recommendation: str | None = None,
        **links: str | None,
    ) -> None:
        self.findings.append(EvaluationFinding(
            code=code,
            category=self.name,
            title=title,
            description=description,
            severity=severity,
            recommendation=recommendation,
            **links,
        ))

    def build(self) -> EvaluationCategory:
        available = self.available_points or 0
        return EvaluationCategory(
            score=self.score,
            max_score=self.maximum,
            percentage=(
                round(self.score * 100 / available, 2) if available else None
            ),
            evaluation_state=self.evaluation_state,
            available_points=available,
            penalties=self.penalties,
            bonuses=[],
            positive_signals=self.positive_signals,
            findings=self.findings,
        )


def _event_type(event: WorkflowEvent) -> str:
    return event.type.value


def _event_operation(event: WorkflowEvent) -> str | None:
    value = event.data.get("operation")
    return str(value) if value is not None else None


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds(), 3)


def _paired_interval(
    events: Sequence[WorkflowEvent],
    starts: set[str],
    ends: set[str],
) -> tuple[datetime, datetime] | None:
    start = next((event.timestamp for event in events if _event_type(event) in starts), None)
    end = next(
        (event.timestamp for event in reversed(events) if _event_type(event) in ends),
        None,
    )
    return (start, end) if start is not None and end is not None and end >= start else None


def _paired_duration(
    events: Sequence[WorkflowEvent],
    starts: set[str],
    ends: set[str],
) -> float | None:
    interval = _paired_interval(events, starts, ends)
    return _seconds(*interval) if interval else None


def _stage_duration(
    events: Sequence[WorkflowEvent],
    stage: str,
) -> float | None:
    selected = [
        event
        for event in events
        if str(event.stage or event.data.get("node") or "").casefold() == stage
    ]
    start = next(
        (event.timestamp for event in selected if _event_type(event) == "stage_started"),
        None,
    )
    end = next(
        (
            event.timestamp
            for event in reversed(selected)
            if _event_type(event) == "stage_completed"
        ),
        None,
    )
    return _seconds(start, end)


def _approval_intervals(
    events: Sequence[WorkflowEvent],
    now: datetime,
) -> list[tuple[datetime, datetime, str]]:
    pending: list[WorkflowEvent] = []
    intervals: list[tuple[datetime, datetime, str]] = []
    for event in events:
        event_type = _event_type(event)
        if event_type == "approval_required":
            pending.append(event)
            continue
        if event_type not in {"approval_granted", "approval_rejected"}:
            continue
        operation = _event_operation(event)
        match = next(
            (
                candidate
                for candidate in pending
                if _event_operation(candidate) == operation
            ),
            None,
        )
        if match is not None:
            pending.remove(match)
            intervals.append((
                match.timestamp,
                max(event.timestamp, match.timestamp),
                _approval_stage(match),
            ))
    for event in pending:
        intervals.append((event.timestamp, max(now, event.timestamp), _approval_stage(event)))
    return intervals


def _approval_stage(event: WorkflowEvent) -> str:
    stage = str(event.stage or event.data.get("stage") or "").casefold()
    if stage in {"planning", "implementation", "testing", "repair", "finalize"}:
        return stage
    operation = (_event_operation(event) or "").casefold()
    if operation in {"create_project", "prepare_environment"}:
        return "implementation"
    if operation in {"apply_fix", "update_project_files"}:
        return "repair"
    return "testing" if operation == "run_tests" else ""


def _merged_seconds(intervals: Sequence[tuple[datetime, datetime]]) -> float:
    if not intervals:
        return 0.0
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return round(sum((end - start).total_seconds() for start, end in merged), 3)


def _stage_breakdown(
    interval: tuple[datetime, datetime] | None,
    approvals: Sequence[tuple[datetime, datetime, str]],
    stage: str,
) -> StageDuration:
    if interval is None:
        return StageDuration()
    start, end = interval
    clipped = [
        (max(start, wait_start), min(end, wait_end))
        for wait_start, wait_end, wait_stage in approvals
        if wait_stage == stage and wait_end > start and wait_start < end
    ]
    elapsed = (end - start).total_seconds()
    waiting = _merged_seconds(clipped)
    return StageDuration(
        elapsed_seconds=round(elapsed, 3),
        active_seconds=round(max(elapsed - waiting, 0), 3),
        waiting_seconds=waiting,
    )


def duration_breakdown(
    events: Sequence[WorkflowEvent],
    *,
    terminal: bool,
    now: datetime,
) -> WorkflowDurationBreakdown:
    total_interval = _paired_interval(
        events,
        {"workflow_started"},
        {"workflow_completed", "workflow_failed"},
    )
    if total_interval is None and not terminal:
        started = next(
            (event.timestamp for event in events if _event_type(event) == "workflow_started"),
            None,
        )
        if started is not None:
            total_interval = (started, max(started, now))
    total = _seconds(*total_interval) if total_interval else None
    approvals = _approval_intervals(events, now)
    approval_wait = _merged_seconds([(start, end) for start, end, _ in approvals])
    stage_intervals = {
        "planning": _paired_interval(events, {"planning_started"}, {"planning_completed"}),
        "implementation": _paired_interval(
            events, {"implementation_started"}, {"implementation_completed"}
        ),
        "testing": _paired_interval(events, {"testing_started"}, {"testing_completed"}),
        "repair": _paired_interval(events, {"repair_started"}, {"repair_completed"}),
        "finalize": (
            _paired_interval(
                events,
                {"finalization_started", "finalize_started"},
                {"finalization_completed", "finalize_completed"},
            )
        ),
    }
    stages = {
        name: _stage_breakdown(interval, approvals, name)
        for name, interval in stage_intervals.items()
    }
    finalize_fallback = _stage_duration(events, "finalize")
    if stages["finalize"].elapsed_seconds is None and finalize_fallback is not None:
        stages["finalize"] = StageDuration(
            elapsed_seconds=finalize_fallback,
            active_seconds=finalize_fallback,
            waiting_seconds=0,
        )
    return WorkflowDurationBreakdown(
        wall_clock_duration_seconds=total,
        active_execution_seconds=(
            round(max(total - approval_wait, 0), 3) if total is not None else None
        ),
        total_duration_seconds=total,
        planning_seconds=stages["planning"].elapsed_seconds,
        implementation_seconds=stages["implementation"].elapsed_seconds,
        testing_seconds=stages["testing"].elapsed_seconds,
        repair_seconds=stages["repair"].elapsed_seconds,
        approval_wait_seconds=approval_wait or None,
        finalize_seconds=stages["finalize"].elapsed_seconds,
        planning=stages["planning"],
        implementation=stages["implementation"],
        testing=stages["testing"],
        repair=stages["repair"],
        finalize=stages["finalize"],
    )


def _task_for(execution: WorkflowAgentExecutionResponse, role: str) -> WorkflowTask | None:
    normalized = role.casefold()
    return next(
        (
            task
            for task in execution.planning.tasks
            if normalized in task.agent.casefold()
        ),
        None,
    )


def _safe_relative_path(path: str | None) -> str | None:
    if not path:
        return None
    normalized = path.replace("\\", "/").strip()
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    lowered = [part.casefold() for part in parts]
    if (
        normalized.startswith("/")
        or (len(normalized) > 1 and normalized[1] == ":")
        or any(part == ".." for part in parts)
        or any(
            part in {".git", ".venv", "secrets", "credentials"}
            or part.startswith(".env")
            for part in lowered
        )
        or any(part.endswith((".pem", ".key", ".p12")) for part in lowered)
    ):
        return None
    return "/".join(parts) or None


def _event_for_navigation(
    events: Sequence[WorkflowEvent],
    branch_id: str,
    *,
    event_types: Sequence[str],
    operation: str | None = None,
) -> WorkflowEvent | None:
    selected = sorted(
        (
            event for event in events
            if str(event.data.get("branch_id") or "original") == branch_id
        ),
        key=lambda event: (event.sequence, event.timestamp, str(event.event_id)),
    )
    for event_type in event_types:
        for event in reversed(selected):
            if _event_type(event) != event_type:
                continue
            if (
                operation
                and event_type in {"approval_required", "tool_completed"}
                and _event_operation(event) != operation
            ):
                tool = str(event.data.get("tool") or event.data.get("tool_name") or "")
                if operation not in tool:
                    continue
            return event
    return None


def _task_by_roles(
    execution: WorkflowAgentExecutionResponse,
    roles: Sequence[str],
) -> WorkflowTask | None:
    for role in roles:
        task = _task_for(execution, role)
        if task is not None:
            return task
    return None


def _preferred_file(
    execution: WorkflowAgentExecutionResponse,
    *,
    tests: bool = False,
) -> str | None:
    candidates = [
        *execution.testing.failing_test_files,
        *execution.testing.files_read_during_repair,
        *execution.testing.files_updated_during_repair,
        *execution.implementation.generated_files,
        *execution.implementation.updated_files,
    ]
    safe = [path for item in candidates if (path := _safe_relative_path(item))]
    if tests:
        return next(
            (path for path in safe if path.startswith("tests/") or "/tests/" in path),
            None,
        )
    package = execution.implementation.package_name
    if package:
        preferred = f"{package}/main.py"
        match = next((path for path in safe if path.endswith(preferred)), None)
        if match:
            return match
    return next(
        (path for path in safe if path.endswith("/main.py") or path == "main.py"),
        next((path for path in safe if path.endswith(".py") and "test" not in path), None),
    )


def attach_finding_navigation(
    *,
    finding: EvaluationFinding,
    execution: WorkflowAgentExecutionResponse,
    events: Sequence[WorkflowEvent],
    branch_id: str,
) -> EvaluationFinding:
    """Attach only observable navigation evidence from the selected branch."""
    task: WorkflowTask | None = None
    event: WorkflowEvent | None = None
    related_file: str | None = None
    if finding.code == "TEST_WARNINGS":
        task = _task_by_roles(execution, ("qa",))
        event = _event_for_navigation(
            events,
            branch_id,
            event_types=(
                "test_run_completed",
                "testing_completed",
                "tool_completed",
                "test_run_started",
            ),
            operation="run_tests",
        )
        related_file = _preferred_file(execution, tests=True)
    elif finding.code == "IMPLEMENTATION_VALID":
        task = _task_by_roles(execution, ("backend developer", "developer"))
        event = _event_for_navigation(
            events, branch_id, event_types=("implementation_completed",)
        )
        related_file = _preferred_file(execution)
    elif finding.code == "IMPLEMENTATION_PENDING":
        task = _task_by_roles(execution, ("backend developer", "developer"))
        event = _event_for_navigation(
            events,
            branch_id,
            event_types=("approval_required",),
            operation="create_project",
        )
    elif finding.code == "IMPLEMENTATION_IN_PROGRESS":
        task = _task_by_roles(execution, ("backend developer", "developer"))
        event = _event_for_navigation(
            events, branch_id, event_types=("implementation_completed",)
        )
        related_file = _preferred_file(execution)
    elif finding.code == "PLAN_VALID":
        task = _task_by_roles(execution, ("software architect", "architect"))
        event = _event_for_navigation(
            events, branch_id, event_types=("planning_completed",)
        )
    elif finding.code == "REQUIREMENT_UNKNOWN":
        task = _task_by_roles(
            execution, ("business analyst", "backend developer", "developer")
        )
        event = _event_for_navigation(
            events, branch_id, event_types=("planning_completed",)
        )
        related_file = _preferred_file(execution)
    elif finding.code == "ACCEPTANCE_UNKNOWN":
        task = _task_by_roles(execution, ("qa",))
        event = _event_for_navigation(
            events,
            branch_id,
            event_types=("approval_required", "testing_started"),
            operation="run_tests",
        )
        related_file = _preferred_file(execution, tests=True)
    else:
        return finding
    return finding.model_copy(update={
        "related_task_id": task.task_id if task else finding.related_task_id,
        "related_event_id": str(event.event_id) if event else finding.related_event_id,
        "related_file": related_file or _safe_relative_path(finding.related_file),
    })


def _coverage(
    texts: Sequence[str],
    execution: WorkflowAgentExecutionResponse,
    *,
    acceptance: bool,
) -> RequirementCoverage:
    qa = _task_for(execution, "qa")
    implementation_task = next(
        (
            task
            for task in execution.planning.tasks
            if "developer" in task.agent.casefold()
        ),
        None,
    )
    items: list[RequirementCoverageItem] = []
    for index, text in enumerate(texts, start=1):
        related_tasks = [
            task.task_id
            for task in (qa, implementation_task)
            if task is not None
        ]
        related_events = sorted({
            event_id
            for task in (qa, implementation_task)
            if task is not None
            for event_id in task.related_event_ids
        })
        related_files = sorted({
            path
            for task in (qa, implementation_task)
            if task is not None
            for path in task.related_files
        })
        evidence: list[str] = []
        if acceptance:
            enough = bool(
                execution.planning.valid
                and execution.testing.passed
                and qa
                and qa.status == "completed"
                and qa.related_files
            )
        else:
            enough = bool(
                execution.planning.valid
                and execution.implementation.valid
                and execution.testing.passed
                and implementation_task
                and implementation_task.status == "completed"
                and related_files
            )
        if enough:
            status = "satisfied"
            evidence = [
                "planning_valid",
                "implementation_valid",
                "tests_passed",
            ]
        elif execution.testing.executed and not execution.testing.passed:
            status = "unsatisfied"
            evidence = ["tests_failed"]
        elif execution.terminal_status in TERMINAL_STATUSES and (
            not execution.planning.valid or not execution.implementation.valid
        ):
            status = "unsatisfied"
            evidence = ["terminal_validation_failed"]
        else:
            status = "unknown"
        items.append(RequirementCoverageItem(
            index=index,
            text=text,
            status=status,
            evidence=evidence,
            related_task_ids=related_tasks,
            related_event_ids=related_events,
            related_files=related_files,
        ))
    total = len(items)
    satisfied = sum(item.status == "satisfied" for item in items)
    unsatisfied = sum(item.status == "unsatisfied" for item in items)
    unknown = total - satisfied - unsatisfied
    return RequirementCoverage(
        total=total,
        satisfied=satisfied,
        unsatisfied=unsatisfied,
        unknown=unknown,
        coverage_percent=round(satisfied * 100 / total, 2) if total else 0.0,
        items=items,
    )


def _duplicate_tools(events: Sequence[WorkflowEvent]) -> int:
    seen: set[tuple[str, str, int | None]] = set()
    duplicates = 0
    for event in events:
        if _event_type(event) != "tool_started":
            continue
        tool = str(event.data.get("tool") or event.data.get("tool_name") or "")
        checkpoint = str(event.data.get("checkpoint_id") or "")
        attempt = event.data.get("attempt")
        key = (tool, checkpoint, attempt if isinstance(attempt, int) else None)
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def _event_sequence_has_critical_gap(events: Sequence[WorkflowEvent]) -> bool:
    sequences = sorted({event.sequence for event in events})
    return any(right - left > 2 for left, right in zip(sequences, sequences[1:]))


def _recommendations(findings: Iterable[EvaluationFinding]) -> list[str]:
    ordered = sorted(
        (finding for finding in findings if finding.recommendation),
        key=lambda finding: (
            SEVERITY_ORDER[finding.severity],
            finding.code,
        ),
    )
    return list(dict.fromkeys(
        finding.recommendation
        for finding in ordered
        if finding.recommendation
    ))


def _safe_evidence(
    execution: WorkflowAgentExecutionResponse,
    events: Sequence[WorkflowEvent],
) -> dict[str, object]:
    return {
        "event_count": len(events),
        "last_sequence": max((event.sequence for event in events), default=0),
        "task_ids": [task.task_id for task in execution.planning.tasks],
        "generated_files": execution.implementation.generated_files,
        "updated_files": execution.implementation.updated_files,
        "test_files": sorted({
            *execution.testing.failing_test_files,
            *execution.testing.files_read_during_repair,
            *execution.testing.files_updated_during_repair,
        }),
        "terminal_status": execution.terminal_status,
        "attempt_counter_semantics": "total_attempts_first_is_normal",
        "implementation_retry_count": _retry_count(execution.implementation.attempts),
    }


def _completed_operation_observed(
    events: Sequence[WorkflowEvent],
    branch_id: str,
    operation: str,
) -> bool:
    return _event_for_navigation(
        events,
        branch_id,
        event_types=("tool_completed",),
        operation=operation,
    ) is not None


def _project_creation_observed(
    execution: WorkflowAgentExecutionResponse,
    events: Sequence[WorkflowEvent],
) -> bool:
    if execution.final_result.project_created:
        return True
    if _completed_operation_observed(events, execution.branch_id, "create_project"):
        return True
    return any(
        _event_type(event) == "implementation_completed"
        and str(event.data.get("branch_id") or "original") == execution.branch_id
        and event.data.get("project_created") is True
        for event in events
    )


def calculate_workflow_evaluation(
    execution: WorkflowAgentExecutionResponse,
    events: Sequence[WorkflowEvent],
    *,
    include_evidence: bool = False,
    calculated_at: datetime | None = None,
) -> WorkflowEvaluationResponse:
    now = calculated_at or datetime.now(UTC)
    terminal = execution.terminal_status in TERMINAL_STATUSES
    planning_started = bool(
        execution.planning.valid
        or terminal
        or execution.planning.attempts
        or execution.planning.analysis.completed
    )
    project_created = _project_creation_observed(execution, events)
    environment_observed = bool(
        project_created
        and (
            execution.implementation.environment_prepared
            or execution.implementation.dependencies_installed
            or _completed_operation_observed(
                events, execution.branch_id, "prepare_environment"
            )
            or terminal
        )
    )
    implementation_complete = bool(
        project_created
        and execution.implementation.generated_files
        and execution.implementation.valid
        and execution.implementation.environment_prepared
        and execution.implementation.dependencies_installed
    )
    implementation_state = (
        "evaluated" if implementation_complete
        else "partial" if project_created
        else "not_started"
    )
    implementation_available = (
        25 if environment_observed else 19 if project_created else 0
    )
    planning = _Category(
        "planning", PLANNING_MAX,
        "evaluated" if execution.planning.valid or terminal else (
            "partial" if planning_started else "not_started"
        ),
    )
    implementation = _Category(
        "implementation", IMPLEMENTATION_MAX,
        implementation_state,
        implementation_available,
    )
    testing = _Category(
        "testing", TESTING_MAX,
        "evaluated" if execution.testing.executed or terminal else "not_started",
    )
    efficiency = _Category("efficiency", EFFICIENCY_MAX)
    reliability = _Category("reliability", RELIABILITY_MAX)

    analysis = execution.planning.analysis
    if execution.planning.valid:
        planning.signal("PLAN_VALID", "El plan superó la validación estructurada.")
        planning.finding(
            "PLAN_VALID", "Plan válido", "El plan cumple su contrato estructurado.", "info"
        )
    elif terminal or execution.planning.attempts:
        planning.penalty("PLAN_INVALID", 12, "El plan no es válido.", "error")
        planning.finding(
            "PLAN_REQUIRES_REFINEMENT",
            "El plan requiere ajustes",
            "La validación del plan no terminó correctamente.",
            "error",
            "Corregir los errores estructurados antes de implementar.",
        )
    else:
        planning.finding(
            "PLANNING_PENDING",
            "Planning pendiente",
            "La planificación todavía no produjo un resultado evaluable.",
            "info",
        )
    if not analysis.objective:
        planning.penalty("PLANNING_OBJECTIVE_MISSING", 2, "Falta un objetivo explícito.")
    if not analysis.functional_requirements:
        planning.penalty(
            "FUNCTIONAL_REQUIREMENTS_MISSING", 2, "No hay requisitos funcionales."
        )
    if not analysis.acceptance_criteria:
        planning.penalty(
            "ACCEPTANCE_CRITERIA_MISSING", 2, "No hay criterios de aceptación."
        )
    if not execution.planning.tasks:
        planning.penalty("PLANNING_TASKS_MISSING", 4, "No hay tareas asignadas.")
    planning_retry_count = _retry_count(execution.planning.attempts)
    extra_planning = min(planning_retry_count * 2, 6)
    if extra_planning:
        planning.penalty(
            "PLANNING_RETRIES", extra_planning,
            _attempt_message(planning_retry_count, "planificación")
        )
        planning.finding(
            "PLANNING_RETRIES",
            "Plan refinado",
            "La planificación necesitó intentos adicionales.",
            "warning",
            "Mejorar las validaciones tempranas del plan.",
        )
    if execution.planning.validation_errors:
        planning.penalty(
            "PLANNING_VALIDATION_ERRORS",
            min(len(execution.planning.validation_errors), 5),
            f"{len(execution.planning.validation_errors)} errores de validación del plan.",
            "error",
        )

    if execution.implementation.valid and not project_created:
        planning.signal(
            "IMPLEMENTATION_PLAN_VALID",
            "El plan de implementación superó la validación.",
        )
    if implementation_complete:
        implementation.signal(
            "IMPLEMENTATION_VALID", "La implementación superó la validación."
        )
        implementation.finding(
            "IMPLEMENTATION_VALID",
            "Implementación válida",
            "Los artefactos generados cumplen el contrato de implementación.",
            "info",
        )
        implementation.signal("PROJECT_CREATED", "Proyecto creado.")
        implementation.signal("GENERATED_FILES_AVAILABLE", "Archivos generados disponibles.")
        implementation.signal("ENVIRONMENT_PREPARED", "Entorno preparado.")
        implementation.signal("DEPENDENCIES_INSTALLED", "Dependencias instaladas.")
    elif project_created:
        implementation.signal("PROJECT_CREATED", "Proyecto creado.")
        if execution.implementation.generated_files:
            implementation.signal(
                "GENERATED_FILES_AVAILABLE", "Archivos generados disponibles."
            )
        else:
            implementation.penalty(
                "GENERATED_FILES_MISSING",
                5,
                "No se registraron archivos generados.",
                "error",
            )
        if not execution.implementation.valid:
            implementation.penalty(
                "IMPLEMENTATION_INVALID",
                4,
                "La implementación no es válida.",
                "error",
            )
        if environment_observed:
            if execution.implementation.environment_prepared:
                implementation.signal("ENVIRONMENT_PREPARED", "Entorno preparado.")
            else:
                implementation.penalty(
                    "ENVIRONMENT_NOT_PREPARED",
                    3,
                    "El entorno no quedó preparado.",
                    "error",
                )
            if execution.implementation.dependencies_installed:
                implementation.signal(
                    "DEPENDENCIES_INSTALLED", "Dependencias instaladas."
                )
            else:
                implementation.penalty(
                    "DEPENDENCIES_NOT_INSTALLED",
                    3,
                    "Las dependencias no quedaron instaladas.",
                    "error",
                )
        implementation.finding(
            "IMPLEMENTATION_IN_PROGRESS",
            "Implementación parcial",
            "El proyecto fue creado, pero su preparación todavía no está completa.",
            "info",
        )
    else:
        implementation.finding(
            "IMPLEMENTATION_PENDING",
            "Implementación pendiente",
            "La creación del proyecto todavía no fue ejecutada.",
            "info",
        )
    implementation_retry_count = _retry_count(execution.implementation.attempts)
    implementation_retries = min(implementation_retry_count * 2, 6)
    if implementation_retries:
        implementation.penalty(
            "IMPLEMENTATION_RETRIES",
            implementation_retries,
            _attempt_message(implementation_retry_count, "implementación"),
        )
    if execution.implementation.validation_errors:
        implementation.penalty(
            "IMPLEMENTATION_VALIDATION_ERRORS",
            min(len(execution.implementation.validation_errors) * 2, 8),
            f"{len(execution.implementation.validation_errors)} errores de implementación.",
            "error",
        )
    if (
        execution.implementation.updated_files
        and execution.testing.repair_phase != "completed"
    ):
        implementation.finding(
            "UNEXPECTED_UPDATED_FILES",
            "Archivos actualizados",
            "Hay archivos actualizados sin una reparación completada asociada.",
            "warning",
            "Revisar la procedencia de los archivos actualizados.",
        )

    if execution.testing.executed:
        testing.signal("TESTS_EXECUTED", "Las pruebas fueron ejecutadas.")
    elif terminal:
        testing.penalty("TESTS_NOT_EXECUTED", 20, "Las pruebas no se ejecutaron.", "error")
        testing.finding(
            "TESTS_NOT_EXECUTED",
            "Pruebas no ejecutadas",
            "No existe evidencia durable de una ejecución de pruebas.",
            "error",
            "Ejecutar la suite de pruebas antes de finalizar.",
        )
    else:
        testing.finding(
            "TESTING_PENDING",
            "Testing pendiente",
            "La suite todavía no fue ejecutada; el score es provisional.",
            "info",
        )
    if execution.testing.executed and execution.testing.passed:
        testing.signal("TESTS_PASSED", "La suite de pruebas terminó correctamente.")
    elif execution.testing.executed:
        testing.penalty("TESTS_FAILED", 20, "La suite de pruebas falló.", "critical")
        testing.finding(
            "TESTS_FAILED",
            "Pruebas fallidas",
            execution.testing.summary or "La suite no terminó correctamente.",
            "critical",
            "Corregir los fallos y volver a ejecutar la suite.",
        )
    if execution.testing.warnings:
        testing.penalty(
            "TEST_WARNINGS",
            min(execution.testing.warnings, 5),
            f"{execution.testing.warnings} warnings en la ejecución de pruebas.",
        )
        testing.finding(
            "TEST_WARNINGS",
            "Warnings de pruebas",
            f"Pytest reportó {execution.testing.warnings} warnings.",
            "warning",
            "Revisar y eliminar warnings de pytest.",
        )
    if execution.testing.failure_type:
        testing.penalty(
            "TEST_FAILURE_TYPE", 3,
            f"Se registró el fallo {execution.testing.failure_type}.", "error"
        )
    if execution.testing.repair_attempts:
        testing.penalty(
            "REPAIR_ATTEMPTS",
            min(execution.testing.repair_attempts * 2, 6),
            f"{execution.testing.repair_attempts} intentos de reparación.",
        )
        testing.finding(
            "REPAIR_REQUIRED",
            "Fue necesaria una reparación",
            "La primera implementación necesitó corrección automática.",
            "warning",
            "Analizar la causa raíz del primer fallo para reducir retrabajo.",
        )
    elif execution.testing.passed:
        testing.signal("NO_REPAIR_REQUIRED", "No fue necesaria una reparación.")
    if execution.testing.repair_phase == "failed":
        testing.penalty("REPAIR_FAILED", 10, "La reparación falló.", "critical")
        testing.finding(
            "REPAIR_FAILED",
            "Reparación fallida",
            "El flujo no logró producir una corrección válida.",
            "critical",
            "Revisar la estrategia y la evidencia usada para reparar.",
        )
    if (
        execution.testing.expected_command
        and execution.testing.actual_command
        and execution.testing.expected_command != execution.testing.actual_command
    ):
        testing.penalty(
            "TEST_COMMAND_MISMATCH",
            3,
            "El comando ejecutado difiere del comando esperado.",
        )

    attempt_cost = (
        planning_retry_count
        + implementation_retry_count
        + _retry_count(execution.supervisor.attempts)
        + max(execution.testing.repair_attempts, 0) * 2
    )
    if attempt_cost:
        efficiency.penalty(
            "EXTRA_ATTEMPTS", min(attempt_cost, EFFICIENCY_MAX),
            f"Los intentos adicionales descontaron {min(attempt_cost, EFFICIENCY_MAX)} puntos."
        )
    approval_count = sum(_event_type(event) == "approval_required" for event in events)
    if approval_count > NORMAL_APPROVAL_COUNT:
        efficiency.penalty(
            "APPROVAL_OVERHEAD",
            approval_count - NORMAL_APPROVAL_COUNT,
            f"{approval_count - NORMAL_APPROVAL_COUNT} aprobaciones adicionales.",
        )
        efficiency.finding(
            "APPROVAL_OVERHEAD",
            "Aprobaciones adicionales",
            "El flujo necesitó más aprobaciones que las tres operaciones normales.",
            "warning",
            "Revisar si las operaciones adicionales pueden agruparse de forma segura.",
        )
    duplicates = _duplicate_tools(events)
    if duplicates:
        efficiency.penalty(
            "DUPLICATE_TOOL_EXECUTION", 3, "Se detectó una ejecución duplicada de tool."
        )
    duration = duration_breakdown(events, terminal=terminal, now=now)
    if (
        duration.total_duration_seconds is not None
        and duration.total_duration_seconds > HIGH_DURATION_SECONDS
    ):
        duration_points = min(
            int(duration.total_duration_seconds // HIGH_DURATION_SECONDS),
            5,
        )
        efficiency.penalty(
            "HIGH_DURATION",
            duration_points,
            f"La duración superó {HIGH_DURATION_SECONDS} segundos.",
        )
        efficiency.finding(
            "HIGH_DURATION",
            "Duración elevada",
            "El workflow superó el umbral de duración configurado.",
            "warning",
            "Revisar preparación de entorno y cache de dependencias.",
        )

    if execution.supervisor.loop_detected:
        reliability.penalty("SUPERVISOR_LOOP", 15, "El Supervisor detectó un loop.", "critical")
        reliability.finding(
            "SUPERVISOR_LOOP",
            "Loop del Supervisor",
            "La política de handoff dejó de mostrar progreso.",
            "critical",
            "Revisar política de handoff y fingerprint de progreso.",
        )
    elif execution.supervisor.decision_source == "deterministic":
        reliability.signal(
            "SUPERVISOR_DETERMINISTIC", "El Supervisor usó decisión determinista."
        )
    if execution.supervisor.decision_source == "fallback":
        reliability.penalty(
            "SUPERVISOR_FALLBACK", 5, "El Supervisor utilizó fallback.", "error"
        )
        reliability.finding(
            "SUPERVISOR_FALLBACK",
            "Fallback del Supervisor",
            "La decisión principal no pudo utilizarse.",
            "error",
            "Revisar la salida estructurada y la política de decisión.",
        )
    if execution.supervisor.errors:
        reliability.penalty(
            "SUPERVISOR_ERRORS",
            min(len(execution.supervisor.errors) * 2, 8),
            f"{len(execution.supervisor.errors)} errores del Supervisor.",
            "error",
        )
    invalid_decisions = sum(
        int(item.get("invalid_decision_count") or 0)
        for item in execution.supervisor.handoff_history
        if isinstance(item.get("invalid_decision_count"), (int, float))
    )
    if invalid_decisions:
        reliability.penalty(
            "INVALID_SUPERVISOR_DECISIONS",
            min(invalid_decisions * 3, 9),
            f"{invalid_decisions} decisiones inválidas.",
            "error",
        )
    if (
        execution.supervisor.confidence is not None
        and execution.supervisor.confidence < 0.5
    ):
        reliability.penalty(
            "LOW_SUPERVISOR_CONFIDENCE", 4, "La confianza del Supervisor es menor a 0.5."
        )
    if terminal and execution.terminal_status != "completed":
        reliability.penalty(
            "TERMINAL_FAILURE", 10, f"El workflow terminó como {execution.terminal_status}.", "error"
        )
    branch_mismatch = any(
        str(event.data.get("branch_id") or "original") != execution.branch_id
        for event in events
    )
    if branch_mismatch:
        reliability.penalty(
            "BRANCH_INCONSISTENT", 10, "Se detectó evidencia de otra rama.", "critical"
        )
    if _event_sequence_has_critical_gap(events):
        reliability.penalty(
            "CRITICAL_EVENT_SEQUENCE_GAP", 5, "Existe un gap crítico en la secuencia."
        )

    requirement_coverage = _coverage(
        analysis.functional_requirements,
        execution,
        acceptance=False,
    )
    acceptance_coverage = _coverage(
        analysis.acceptance_criteria,
        execution,
        acceptance=True,
    )
    if requirement_coverage.unknown:
        planning.finding(
            "REQUIREMENT_UNKNOWN",
            "Requisitos sin evidencia",
            f"{requirement_coverage.unknown} requisitos no tienen evidencia suficiente.",
            "warning",
            "Agregar pruebas explícitas para los requisitos sin evidencia.",
        )
    if acceptance_coverage.unsatisfied:
        testing.finding(
            "ACCEPTANCE_UNSATISFIED",
            "Criterios incumplidos",
            f"{acceptance_coverage.unsatisfied} criterios no fueron satisfechos.",
            "error",
            "Corregir la implementación y agregar pruebas para cada criterio.",
        )
    elif acceptance_coverage.unknown:
        testing.finding(
            "ACCEPTANCE_UNKNOWN",
            "Criterios sin evidencia",
            f"{acceptance_coverage.unknown} criterios no tienen evidencia suficiente.",
            "warning",
            "Agregar pruebas explícitas para los criterios sin evidencia.",
        )

    categories = [
        planning.build(),
        implementation.build(),
        testing.build(),
        efficiency.build(),
        reliability.build(),
    ]
    categories = [
        category.model_copy(update={
            "findings": [
                attach_finding_navigation(
                    finding=finding,
                    execution=execution,
                    events=events,
                    branch_id=execution.branch_id,
                )
                for finding in category.findings
            ],
        })
        for category in categories
    ]
    penalties = [penalty for category in categories for penalty in category.penalties]
    bonuses = [bonus for category in categories for bonus in category.bonuses]
    positive_signals = [
        signal for category in categories for signal in category.positive_signals
    ]
    findings = [finding for category in categories for finding in category.findings]
    earned = sum(category.score for category in categories)
    available = sum(category.available_points for category in categories)
    provisional_percentage = round(earned * 100 / available, 2) if available else 0.0
    is_provisional = not terminal
    return WorkflowEvaluationResponse(
        thread_id=execution.thread_id,
        branch_id=execution.branch_id,
        lineage=execution.lineage,
        inherited_from_branch=execution.inherited_from_branch,
        origin_checkpoint=execution.origin_checkpoint,
        terminal_status=execution.terminal_status,
        data_complete=execution.data_complete,
        evaluation_status="final" if terminal else "partial",
        overall_score=earned,
        max_score=MAX_SCORE,
        percentage=provisional_percentage,
        earned_points=earned,
        available_points=available,
        provisional_percentage=provisional_percentage,
        projected_max_score=MAX_SCORE,
        is_provisional=is_provisional,
        final_grade_available=not is_provisional,
        grade=grade_for_score(earned) if not is_provisional else None,
        provisional_grade=grade_for_score(round(provisional_percentage)) if is_provisional else None,
        planning=categories[0],
        implementation=categories[1],
        testing=categories[2],
        efficiency=categories[3],
        reliability=categories[4],
        requirement_coverage=requirement_coverage,
        acceptance_coverage=acceptance_coverage,
        duration=duration,
        penalties=penalties,
        bonuses=bonuses,
        positive_signals=positive_signals,
        findings=findings,
        recommendations=_recommendations(findings),
        calculated_at=now,
        source_updated_at=execution.updated_at,
        scoring_version=SCORING_VERSION,
        evidence=_safe_evidence(execution, events) if include_evidence else None,
    )


class WorkflowEvaluationService:
    def __init__(
        self,
        *,
        execution_service: WorkflowExecutionService,
        store: WorkflowEvaluationStore,
    ) -> None:
        self.execution_service = execution_service
        self.store = store

    async def get_evaluation(
        self,
        thread_id: str,
        *,
        branch_id: str = "original",
        include_evidence: bool = False,
    ) -> WorkflowEvaluationResponse:
        execution = await self.execution_service.get_execution(
            thread_id,
            branch_id=branch_id,
            include_events=True,
        )
        terminal = execution.terminal_status in TERMINAL_STATUSES
        if terminal:
            cached = await self.store.get(
                thread_id,
                branch_id,
                SCORING_VERSION,
                execution.updated_at,
            )
            if cached is not None:
                return cached if include_evidence else cached.model_copy(
                    update={"evidence": None}
                )
        evaluation = calculate_workflow_evaluation(
            execution,
            execution.events or [],
            include_evidence=True,
        )
        if terminal:
            await self.store.put(evaluation)
        return evaluation if include_evidence else evaluation.model_copy(
            update={"evidence": None}
        )
