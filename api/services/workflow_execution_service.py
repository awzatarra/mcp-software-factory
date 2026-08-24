from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
import re
from collections.abc import Sequence
from typing import Any, Iterable

from api.execution_models import (
    WorkflowAgentExecutionResponse,
    WorkflowFinalResult,
    WorkflowGitSummary,
    WorkflowImplementationResult,
    WorkflowLearningCandidateSummary,
    WorkflowLearningSummary,
    WorkflowPlanningResult,
    WorkflowRefinement,
    WorkflowRequirementAnalysis,
    WorkflowSupervisorResult,
    WorkflowTask,
    WorkflowTestingResult,
)
from api.services.workflow_metadata_store import WorkflowMetadataStore
from api.services.workflow_query_service import normalize_terminal_status
from api.services.workflow_knowledge import (
    planner_knowledge_summary,
    qa_knowledge_summary,
    repair_knowledge_summary,
)
from graph.persistence_service import (
    CheckpointNotFoundError,
    WorkflowNotFoundError,
    WorkflowPersistenceService,
)
from streaming.models import WorkflowEvent
from streaming.store import WorkflowEventStore


class ExecutionBranchNotFoundError(LookupError):
    pass


_SENSITIVE_KEYS = {
    "api_key", "authorization", "chain_of_thought", "connection_string",
    "content", "credential", "credentials", "environment", "env",
    "internal_prompt", "private_key", "prompt", "scratchpad", "system_prompt",
    "test_stderr", "test_stdout", "progress_fingerprint",
}
_SECRET_PATTERN = re.compile(
    r"(?i)(sk-[a-z0-9_-]{12,}|bearer\s+[a-z0-9._-]+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(api[_-]?key|token|password|secret|credential|connection[_ -]?string)"
    r"\s*[:=]\s*\S+)"
)
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:[\\/][^\s,;\"']+|"
    r"/(?:home|users|tmp|var|opt|workspace|mnt)/[^\s,;\"']+)"
)
_EXCLUDED_FILE_NAMES = {".env", ".venv", ".git", "node_modules"}
_STAGE_BY_AGENT = {
    "business analyst": "planning",
    "software architect": "planning",
    "backend developer": "implementation",
    "developer": "implementation",
    "qa reviewer": "testing",
    "qa": "testing",
}
_QA_AGENT_NAMES = {
    "qa",
    "qa reviewer",
    "quality assurance",
    "test engineer",
    "tester",
    "testing agent",
}
_QA_EVENT_TYPES = {
    "testing_started",
    "testing_completed",
    "testing_failed",
    "test_run_started",
    "test_run_completed",
    "test_run_failed",
    "repair_started",
    "repair_completed",
    "repair_failed",
}
_QA_STAGES = {"testing", "testing_repair", "run_tests", "repair"}
_QA_PRIMARY_PRECEDENCE = (
    "test_run_completed",
    "testing_completed",
    "tool_completed",
    "test_run_started",
    "approval_required",
    "testing_started",
    "test_run_failed",
    "testing_failed",
)
_MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "â€™", "â€œ", "â€\u009d", "ðŸ")


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _strings(value: Any, *, limit: int = 100) -> list[str]:
    return [_safe_text(item) for item in _as_list(value)[:limit] if isinstance(item, str)]


def repair_legacy_mojibake(value: str) -> str:
    text = value
    for _ in range(2):
        before_score = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)
        if before_score == 0:
            break
        repaired = None
        for encoding in ("latin1", "cp1252"):
            try:
                candidate = text.encode(encoding).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            after_score = sum(candidate.count(marker) for marker in _MOJIBAKE_MARKERS)
            if after_score < before_score:
                repaired = candidate
                break
        if repaired is None:
            break
        text = repaired
    return text


def _safe_text(value: Any, limit: int = 4_000) -> str:
    text = repair_legacy_mojibake(str(value or ""))
    text = _SECRET_PATTERN.sub("[redacted]", text)
    text = _ABSOLUTE_PATH_PATTERN.sub("[path]", text)
    return "".join(character for character in text if character.isprintable() or character in "\n\t")[:limit]


def _safe_relative_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    windows = PureWindowsPath(value)
    if (
        not normalized
        or normalized.startswith("/")
        or windows.is_absolute()
        or windows.drive
        or ".." in path.parts
        or any(part.lower() in _EXCLUDED_FILE_NAMES for part in path.parts)
        or any(part.lower().startswith(".env") for part in path.parts)
    ):
        return None
    return path.as_posix()


def _safe_paths(value: Any) -> list[str]:
    result: list[str] = []
    for item in _as_list(value):
        normalized = _safe_relative_path(item)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _safe_command(value: Any) -> list[str]:
    command: list[str] = []
    for item in _as_list(value)[:100]:
        raw_text = str(item or "")
        windows = PureWindowsPath(raw_text)
        if windows.is_absolute() or windows.drive:
            text = windows.name
        elif PurePosixPath(raw_text).is_absolute():
            text = PurePosixPath(raw_text).name
        else:
            text = _safe_text(raw_text, 500)
        command.append(text)
    return command


def _safe_mapping(value: Any, *, depth: int = 0) -> dict[str, Any]:
    if not isinstance(value, dict) or depth > 3:
        return {}
    output: dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:100]:
        key = str(raw_key)
        if key.lower() in _SENSITIVE_KEYS:
            continue
        if isinstance(raw_value, dict):
            output[key] = _safe_mapping(raw_value, depth=depth + 1)
        elif isinstance(raw_value, list):
            output[key] = [
                _safe_mapping(item, depth=depth + 1)
                if isinstance(item, dict)
                else _safe_text(item, 1_000)
                for item in raw_value[:50]
            ]
        elif isinstance(raw_value, str):
            output[key] = _safe_text(raw_value)
        elif raw_value is None or isinstance(raw_value, (bool, int, float)):
            output[key] = raw_value
    return output


def _safe_event(event: WorkflowEvent) -> WorkflowEvent:
    return event.model_copy(update={
        "message": _safe_text(event.message) or None,
        "data": _safe_mapping(event.data),
    })


def _parse_comparison(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return _safe_mapping(value)
    text = _safe_text(value, 2_000)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"summary": text} if text else None
    return _safe_mapping(parsed) if isinstance(parsed, dict) else {"summary": text}


def _event_time(
    events: list[WorkflowEvent],
    stage: str,
    *,
    first: bool,
) -> datetime | None:
    matches = [event.timestamp for event in events if event.stage == stage]
    if not matches:
        return None
    return min(matches) if first else max(matches)


def _event_ids(events: Iterable[WorkflowEvent], stage: str) -> list[str]:
    related = [event for event in events if event.stage == stage]
    related.sort(
        key=lambda event: (
            not (
                event.type.value.endswith("_completed")
                or event.type.value == "workflow_completed"
            ),
            -event.sequence,
        )
    )
    return [str(event.event_id) for event in related]


def _normalized_agent(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def _is_qa_task(task: WorkflowTask) -> bool:
    return _normalized_agent(task.agent) in _QA_AGENT_NAMES


def event_matches_task_attempt(
    event_attempt: int | None,
    task_attempt: int,
) -> bool:
    if event_attempt is None:
        return True
    if task_attempt < 1 or event_attempt < 0:
        return False
    return event_attempt in {task_attempt, task_attempt - 1}


def _event_attempt(event: WorkflowEvent) -> int | None:
    for key in ("attempt", "repair_attempt", "repair_attempts"):
        value = event.data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _event_branch(event: WorkflowEvent) -> str:
    return str(event.data.get("branch_id") or "original")


def _run_tests_signal(event: WorkflowEvent) -> bool:
    data = event.data
    tool = str(data.get("tool") or "").casefold()
    tool_name = str(data.get("tool_name") or "").casefold()
    operation = str(data.get("operation") or "").casefold()
    return (
        operation == "run_tests"
        or tool == "run_tests"
        or tool_name == "testing__run_tests"
    )


def _qa_event_matches(event: WorkflowEvent) -> bool:
    event_type = event.type.value
    stage = str(event.stage or "").casefold()
    source = event.source.casefold()
    if event_type in _QA_EVENT_TYPES:
        return True
    if _run_tests_signal(event):
        return True
    if stage in _QA_STAGES and source in {
        "testing_repair_subgraph",
        "testing_repair",
        "graph.approval",
        "graph.nodes",
        "graph.persistence",
        "parent_graph",
    }:
        return True
    return False


def collect_task_related_events(
    *,
    task: WorkflowTask,
    events: Sequence[WorkflowEvent],
    branch_id: str,
) -> list[WorkflowEvent]:
    if _is_qa_task(task):
        selected = [
            event
            for event in events
            if _event_branch(event) == branch_id
            and _qa_event_matches(event)
            and event_matches_task_attempt(_event_attempt(event), task.attempt)
        ]
    else:
        stage = _task_stage(task.agent)
        selected = [
            event
            for event in events
            if _event_branch(event) == branch_id and event.stage == stage
        ]
    selected.sort(key=lambda event: event.sequence)
    seen: set[str] = set()
    ordered: list[WorkflowEvent] = []
    for event in selected:
        event_id = str(event.event_id)
        if event_id in seen:
            continue
        seen.add(event_id)
        ordered.append(event)
    return ordered


def collect_task_related_event_ids(
    *,
    task: WorkflowTask,
    events: Sequence[WorkflowEvent],
    branch_id: str,
) -> list[str]:
    return [
        str(event.event_id)
        for event in collect_task_related_events(
            task=task,
            events=events,
            branch_id=branch_id,
        )
    ]


def _event_is_run_tests_tool(event: WorkflowEvent, event_type: str) -> bool:
    return event.type.value == event_type and _run_tests_signal(event)


def _preferred_task_event_id(
    task: WorkflowTask,
    related: Sequence[WorkflowEvent],
) -> str | None:
    if not related:
        return None
    if _is_qa_task(task):
        for event_type in _QA_PRIMARY_PRECEDENCE:
            for event in related:
                if event.type.value != event_type:
                    continue
                if event_type in {"tool_completed", "approval_required"} and not _run_tests_signal(event):
                    continue
                return str(event.event_id)
    role = _normalized_agent(task.agent)
    preferred = (
        ("planning_completed",)
        if "architect" in role
        else ("planning_started", "planning_completed")
        if "analyst" in role
        else ("implementation_completed", "tool_completed")
    )
    for event_type in preferred:
        for event in related:
            if event.type.value == event_type:
                return str(event.event_id)
    return str(related[0].event_id)


def _first_timestamp(
    related: Sequence[WorkflowEvent],
    predicates: Sequence,
) -> datetime | None:
    for predicate in predicates:
        matches = [event for event in related if predicate(event)]
        if matches:
            return min(event.timestamp for event in matches)
    return None


def _task_timestamps(
    task: WorkflowTask,
    related: Sequence[WorkflowEvent],
) -> tuple[datetime | None, datetime | None]:
    if not _is_qa_task(task):
        timestamps = [event.timestamp for event in related]
        return (
            min(timestamps) if timestamps else None,
            max(timestamps) if timestamps and task.status in {"completed", "failed", "skipped"} else None,
        )
    started = _first_timestamp(related, (
        lambda event: event.type.value == "test_run_started",
        lambda event: _event_is_run_tests_tool(event, "tool_started"),
        lambda event: event.type.value == "approval_granted" and _run_tests_signal(event),
        lambda event: event.type.value == "testing_started",
    ))
    completed = _first_timestamp(related, (
        lambda event: event.type.value == "test_run_completed",
        lambda event: event.type.value == "testing_completed",
        lambda event: _event_is_run_tests_tool(event, "tool_completed"),
        lambda event: event.type.value in {"test_run_failed", "testing_failed"},
    ))
    if started and completed and started > completed:
        completed = None
    return started, completed


def _qa_related_files(values: dict[str, Any], generated: Sequence[str]) -> list[str]:
    generated_tests = [
        path
        for path in generated
        if "tests" in PurePosixPath(path).parts
    ]
    candidates = [
        *generated_tests,
        *_safe_paths(values.get("failing_test_files")),
        *_safe_paths(values.get("files_read_during_repair")),
        *_safe_paths(values.get("files_updated_during_repair")),
    ]
    return sorted(dict.fromkeys(candidates))


def _application_framework(
    analysis: dict[str, Any],
    planning: dict[str, Any],
    implementation: dict[str, Any],
    values: dict[str, Any],
) -> str | None:
    testing_frameworks = {"pytest", "unittest", "jest", "vitest", "mocha"}
    candidates = (
        analysis.get("project_type"),
        planning.get("framework"),
        analysis.get("framework"),
        values.get("detected_framework"),
        implementation.get("framework"),
    )
    for candidate in candidates:
        framework = _safe_text(candidate, 100)
        if framework and framework.casefold() not in testing_frameworks:
            return framework
    return None


def _named_dependency(name: str, version: Any) -> str | None:
    text = _safe_text(version, 100).strip()
    if not text:
        return None
    lowered = text.casefold()
    if lowered.startswith(f"{name.casefold()}=="):
        return text
    if lowered.startswith(f"{name.casefold()} "):
        text = text[len(name):].strip()
    return f"{name}=={text}"


def _task_id(
    thread_id: str,
    branch_id: str,
    order: int,
    agent: str,
    description: str,
) -> str:
    normalized = " ".join(description.lower().split())
    digest = hashlib.sha256(
        f"{thread_id}|{branch_id}|{order}|{agent.lower()}|{normalized}".encode()
    ).hexdigest()[:16]
    return f"task-{digest}"


def _task_status(agent: str, values: dict[str, Any]) -> str:
    role = _normalized_agent(agent)
    terminal = values.get("terminal_status")
    pending = values.get("pending_operation")
    if "analyst" in role:
        if values.get("requirement_analysis"):
            return "completed"
        return "failed" if terminal == "planning_failed" else (
            "running" if int(values.get("planning_attempts") or 0) > 0 else "pending"
        )
    if "architect" in role:
        if values.get("planning_valid"):
            return "completed"
        return "failed" if terminal == "planning_failed" else (
            "running" if values.get("requirement_analysis") else "pending"
        )
    if role in _QA_AGENT_NAMES:
        if pending in {"run_tests", "apply_fix"}:
            return "waiting"
        if values.get("tests_passed"):
            return "completed"
        if values.get("tests_executed") and terminal in {
            "tests_failed", "repair_limit_reached", "infrastructure_failed"
        }:
            return "failed"
        if values.get("tests_executed") or values.get("last_completed_stage") == "implementation":
            return "running"
        return "pending"
    if pending in {"create_project", "prepare_environment"}:
        return "waiting"
    if values.get("project_created") and values.get("environment_prepared"):
        return "completed"
    if terminal in {"implementation_failed", "infrastructure_failed", "user_cancelled"}:
        return "failed"
    if int(values.get("implementation_attempts") or 0) > 0 or values.get("project_created"):
        return "running"
    return "pending"


def _task_stage(agent: str) -> str:
    role = agent.lower()
    for candidate, stage in _STAGE_BY_AGENT.items():
        if candidate in role:
            return stage
    return "implementation"


def _ensure_process_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not tasks:
        return []
    agents = " ".join(str(task.get("role") or task.get("agent") or "").lower() for task in tasks)
    additions: list[dict[str, Any]] = []
    if "analyst" not in agents:
        additions.append({
            "order": 0, "role": "Business Analyst", "title": "Requirement analysis",
            "description": "Analyze the original requirement.",
        })
    if "architect" not in agents:
        additions.append({
            "order": 0, "role": "Software Architect", "title": "Validated plan",
            "description": "Validate the implementation plan.",
        })
    if "qa" not in agents and "test" not in agents:
        additions.append({
            "order": 10_000, "role": "QA Reviewer", "title": "Test validation",
            "description": "Validate the generated project and repairs.",
        })
    combined = additions[:2] + tasks + additions[2:]
    return [
        {**task, "order": index}
        for index, task in enumerate(combined, start=1)
    ]


def _refinements(
    events: list[WorkflowEvent],
    stage: str,
    values: dict[str, Any],
) -> list[WorkflowRefinement]:
    result: list[WorkflowRefinement] = []
    for event in events:
        attempt = event.data.get("attempt")
        if event.stage != stage or not isinstance(attempt, int) or attempt < 2:
            continue
        result.append(WorkflowRefinement(
            sequence=event.sequence,
            stage=stage,
            reason=_safe_text(event.data.get("reason") or event.message) or None,
            before_summary=_safe_text(event.data.get("before")) or None,
            after_summary=_safe_text(event.data.get("after")) or None,
            changed_fields=_strings(event.data.get("changed_fields")),
            attempt=attempt,
            event_id=str(event.event_id),
            created_at=event.timestamp,
        ))
    if stage == "implementation" and int(values.get("implementation_attempts") or 0) > 1 and not result:
        result.append(WorkflowRefinement(
            sequence=1,
            stage=stage,
            reason=_safe_text(values.get("implementation_failure_reason")) or None,
            changed_fields=_strings(values.get("resolved_validation_errors")),
            attempt=int(values.get("implementation_attempts") or 0),
        ))
    return result


class WorkflowExecutionService:
    def __init__(
        self,
        *,
        persistence: WorkflowPersistenceService,
        event_store: WorkflowEventStore,
        metadata_store: WorkflowMetadataStore,
    ) -> None:
        self.persistence = persistence
        self.event_store = event_store
        self.metadata_store = metadata_store

    async def _state(
        self,
        thread_id: str,
        branch_id: str,
    ) -> tuple[dict[str, Any], bool, str | None, str, datetime | None]:
        snapshot = await self.persistence.get_snapshot(thread_id)
        if branch_id == "original":
            return (
                dict(snapshot.values),
                bool(snapshot.interrupts),
                snapshot.created_at,
                "original",
                None,
            )
        events = await self.event_store.get_events(
            thread_id, branch_id=branch_id, limit=2_000
        )
        if not events:
            raise ExecutionBranchNotFoundError(branch_id)
        candidates = [
            str(event.data.get("checkpoint_id"))
            for event in reversed(events)
            if event.data.get("checkpoint_id")
        ]
        candidates.append(branch_id)
        for checkpoint_id in dict.fromkeys(candidates):
            try:
                details = await self.persistence.get_checkpoint(thread_id, checkpoint_id)
            except CheckpointNotFoundError:
                continue
            lineage = str(events[-1].data.get("lineage") or "fork")
            inherited = events[-1].data.get("origin_checkpoint")
            return (
                dict(details.values),
                details.interrupted,
                details.metadata.get("created_at"),
                lineage,
                str(inherited) if inherited else None,
            )
        raise ExecutionBranchNotFoundError(branch_id)

    async def get_execution(
        self,
        thread_id: str,
        *,
        branch_id: str = "original",
        include_events: bool = False,
    ) -> WorkflowAgentExecutionResponse:
        values, interrupted, checkpoint_time, lineage, inherited = await self._state(
            thread_id, branch_id
        )
        events = await self.event_store.get_events(
            thread_id, branch_id=branch_id, limit=2_000
        )
        planning_raw = _as_dict(values.get("planning_result"))
        implementation_raw = _as_dict(values.get("implementation_result"))
        testing_raw = _as_dict(values.get("testing_result"))
        analysis_raw = _as_dict(
            planning_raw.get("analysis") or values.get("requirement_analysis")
        )
        criteria_raw = _as_list(
            planning_raw.get("acceptance_criteria") or values.get("acceptance_criteria")
        )
        criteria = [
            _safe_text(item.get("description") if isinstance(item, dict) else item)
            for item in criteria_raw
        ]
        requirement = _safe_text(values.get("original_user_message"))
        analysis = WorkflowRequirementAnalysis(
            requirement=requirement,
            objective=_safe_text(analysis_raw.get("objective")) or None,
            functional_requirements=_strings(analysis_raw.get("functional_requirements")),
            non_functional_requirements=_strings(analysis_raw.get("non_functional_requirements")),
            acceptance_criteria=criteria,
            assumptions=_strings(analysis_raw.get("assumptions")),
            constraints=_strings(analysis_raw.get("constraints")),
            risks=_strings(analysis_raw.get("risks")),
            completed=bool(analysis_raw),
            source="planning_result" if planning_raw else ("legacy_snapshot" if analysis_raw else None),
            updated_at=_event_time(events, "planning", first=False),
        )

        task_raw = _as_list(
            planning_raw.get("tasks") or values.get("implementation_tasks")
        )
        tasks: list[WorkflowTask] = []
        generated = _safe_paths(
            implementation_raw.get("generated_files") or values.get("generated_files")
        )
        repaired = _safe_paths(values.get("files_updated_during_repair"))
        qa_files = _qa_related_files(values, generated)
        for task in _ensure_process_tasks(
            [dict(item) for item in task_raw if isinstance(item, dict)]
        ):
            agent = _safe_text(task.get("role") or task.get("agent") or "Developer", 100)
            description = _safe_text(task.get("description"), 2_000)
            order = int(task.get("order") or len(tasks) + 1)
            stage = _task_stage(agent)
            related_files = qa_files if _normalized_agent(agent) in _QA_AGENT_NAMES else (
                generated if stage == "implementation" else []
            )
            status = _task_status(agent, values)
            summary = None
            if stage == "implementation" and values.get("project_created"):
                summary = f"{len(generated)} generated files"
            elif stage == "testing":
                summary = _safe_text(
                    testing_raw.get("summary") or values.get("final_test_result_summary")
                ) or None
            task_attempt = int(
                values.get("planning_attempts")
                if stage == "planning"
                else values.get("implementation_attempts")
                if stage == "implementation"
                else int(values.get("repair_attempts") or 0) + 1
            )
            projected_task = WorkflowTask(
                task_id=_task_id(thread_id, branch_id, order, agent, description),
                order=order,
                agent=agent,
                title=_safe_text(task.get("title"), 300) or None,
                description=description,
                status=status,
                attempt=max(task_attempt, 0),
                result_summary=summary,
                related_files=related_files,
            )
            related_events = collect_task_related_events(
                task=projected_task,
                events=events,
                branch_id=branch_id,
            )
            started_at, completed_at = _task_timestamps(
                projected_task,
                related_events,
            )
            tasks.append(projected_task.model_copy(update={
                "primary_event_id": _preferred_task_event_id(
                    projected_task,
                    related_events,
                ),
                "related_event_ids": [
                    str(event.event_id) for event in related_events
                ],
                "started_at": started_at,
                "completed_at": completed_at,
            }))

        project_type = _safe_text(analysis_raw.get("project_type"), 100) or None
        application_framework = _application_framework(
            analysis_raw,
            planning_raw,
            implementation_raw,
            values,
        )
        planning = WorkflowPlanningResult(
            valid=bool(planning_raw.get("valid", values.get("planning_valid", False))),
            attempts=int(planning_raw.get("attempts", values.get("planning_attempts", 0)) or 0),
            project_type=project_type,
            framework=application_framework,
            analysis=analysis,
            tasks=tasks,
            validation_errors=_strings(values.get("planning_errors")),
            refinements=_refinements(events, "planning", values),
            judge=dict(planning_raw.get("judge") or {}) if isinstance(planning_raw.get("judge"), dict) else {},
            hybrid_evaluation=(
                dict(planning_raw.get("hybrid_evaluation") or values.get("planning_hybrid_evaluation") or {})
                if isinstance(planning_raw.get("hybrid_evaluation") or values.get("planning_hybrid_evaluation"), dict)
                else {}
            ),
            started_at=_event_time(events, "planning", first=True),
            completed_at=_event_time(events, "planning", first=False) if values.get("planning_valid") else None,
        )

        installed = [
            dependency
            for dependency in (
                _named_dependency(
                    "fastapi",
                    values.get("installed_fastapi_version"),
                ),
                _named_dependency(
                    "starlette",
                    values.get("installed_starlette_version"),
                ),
            )
            if dependency
        ]
        implementation = WorkflowImplementationResult(
            valid=bool(implementation_raw.get("valid", values.get("implementation_valid", False))),
            attempts=int(implementation_raw.get("attempts", values.get("implementation_attempts", 0)) or 0),
            project_name=_safe_text(values.get("created_project_name") or values.get("project_name"), 120) or None,
            package_name=_safe_text(implementation_raw.get("package_name") or values.get("generated_package_name"), 120) or None,
            framework=application_framework,
            project_implementation=(
                dict(values["project_implementation"])
                if isinstance(values.get("project_implementation"), dict)
                else None
            ),
            generated_files=generated,
            updated_files=repaired,
            dependency_policy_applied=bool(values.get("dependency_policy_applied")),
            dependency_normalization_attempts=int(values.get("dependency_normalization_attempts") or 0),
            environment_prepared=bool(implementation_raw.get("environment_prepared", values.get("environment_prepared", False))),
            dependencies_installed=bool(values.get("dependencies_installed")),
            installed_dependencies=installed,
            validation_errors=_strings(values.get("remaining_validation_errors") or values.get("implementation_errors")),
            refinements=_refinements(events, "implementation", values),
            started_at=_event_time(events, "implementation", first=True),
            completed_at=_event_time(events, "implementation", first=False) if values.get("implementation_valid") else None,
        )

        testing = WorkflowTestingResult(
            executed=bool(testing_raw.get("tests_executed", values.get("tests_executed", False))),
            passed=bool(testing_raw.get("tests_passed", values.get("tests_passed", False))),
            framework=_safe_text(values.get("detected_test_framework"), 100) or None,
            expected_command=_safe_command(values.get("expected_test_command")),
            actual_command=_safe_command(values.get("actual_test_command")),
            summary=_safe_text(testing_raw.get("summary") or values.get("final_test_result_summary")) or None,
            warnings=int(values.get("test_warning_count") or 0),
            failure_type=_safe_text(values.get("failure_type"), 200) or None,
            failure_stage=_safe_text(values.get("failure_stage"), 200) or None,
            failure_message=_safe_text(values.get("failure_message")) or None,
            failing_test_files=_safe_paths(values.get("failing_test_files")),
            repair_phase=_safe_text(testing_raw.get("repair_phase") or values.get("repair_phase") or "not_started", 100),
            repair_attempts=int(testing_raw.get("repair_attempts", values.get("repair_attempts", 0)) or 0),
            repair_decision=_safe_text(values.get("repair_decision")) or None,
            repair_before=_parse_comparison(values.get("repair_before")),
            repair_after=_parse_comparison(values.get("repair_after")),
            files_read_during_repair=_safe_paths(values.get("files_read_during_repair")),
            files_updated_during_repair=repaired,
            started_at=_event_time(events, "testing", first=True),
            completed_at=_event_time(events, "testing", first=False) if values.get("tests_executed") else None,
        )

        git_commits = [
            _safe_mapping(item)
            for item in _as_list(values.get("git_commit_history"))
            if isinstance(item, dict)
        ]
        git = WorkflowGitSummary(
            state=_safe_text(values.get("git_workflow_state") or "not_initialized", 100),
            base_branch=_safe_text(values.get("git_base_branch"), 200) or None,
            base_commit=_safe_text(values.get("git_base_commit"), 100) or None,
            workflow_branch=_safe_text(values.get("git_workflow_branch") or values.get("git_branch"), 200) or None,
            head_commit=_safe_text(values.get("git_head_commit"), 100) or None,
            commit_status=_safe_text(values.get("git_commit_status"), 100) or None,
            approval_state=_safe_text(values.get("pending_approval_status"), 100) or None,
            developer_commit=next((item for item in reversed(git_commits) if item.get("phase") == "implementation"), None),
            repair_commit=next((item for item in reversed(git_commits) if item.get("phase") == "repair"), None),
            commits=git_commits,
        )

        handoffs = [
            _safe_mapping(item)
            for item in _as_list(values.get("handoff_history"))
            if isinstance(item, dict)
        ]
        supervisor = WorkflowSupervisorResult(
            decision=_safe_text(values.get("supervisor_decision"), 100) or None,
            decision_source=_safe_text(values.get("supervisor_decision_source"), 100) or None,
            confidence=float(values["supervisor_confidence"]) if isinstance(values.get("supervisor_confidence"), (int, float)) else None,
            attempts=int(values.get("supervisor_attempts") or 0),
            errors=_strings(values.get("supervisor_errors")),
            loop_detected=(
                values.get("terminal_status") == "supervisor_loop_detected"
                or "supervisor_loop_detected" in _as_list(values.get("supervisor_errors"))
            ),
            handoff_history=handoffs,
        )
        terminal_status = normalize_terminal_status(
            values.get("terminal_status"),
            interrupted=interrupted,
            next_nodes=(),
        )
        created_at, updated_at = await self.metadata_store.event_timestamps(
            thread_id, branch_id
        )
        submissions = {
            str(item.get("candidate_id")): item
            for item in _as_list(values.get("workflow_learning_submission_results"))
            if isinstance(item, dict) and item.get("candidate_id")
        }
        learning_candidates: list[WorkflowLearningCandidateSummary] = []
        for item in _as_list(values.get("workflow_learning_candidates")):
            if not isinstance(item, dict) or not item.get("candidate_id"):
                continue
            submission = submissions.get(str(item["candidate_id"]), {})
            created_value = item.get("created_at")
            try:
                candidate_created = datetime.fromisoformat(str(created_value).replace("Z", "+00:00")) if created_value else None
            except ValueError:
                candidate_created = None
            learning_candidates.append(WorkflowLearningCandidateSummary(
                candidate_id=_safe_text(item.get("candidate_id"), 100),
                knowledge_type=_safe_text(item.get("knowledge_type"), 100),
                confidence=float(item.get("confidence") or 0),
                submission_status=_safe_text(submission.get("submission_status") or "pending", 100),
                knowledge_id=_safe_text(submission.get("knowledge_id"), 100) or None,
                source_reference=_safe_text(item.get("source_reference"), 1000),
                created_at=candidate_created,
            ))
        submitted_statuses = {"candidate", "indexed", "duplicate", "rejected"}
        workflow_learning = WorkflowLearningSummary(
            state=_safe_text(values.get("workflow_learning_state") or "not_started", 100),
            extracted_count=len(learning_candidates),
            submitted_count=sum(item.submission_status in submitted_statuses for item in learning_candidates),
            duplicate_count=sum(item.submission_status == "duplicate" for item in learning_candidates),
            rejected_count=sum(item.submission_status == "rejected" for item in learning_candidates),
            candidates=learning_candidates,
        )
        return WorkflowAgentExecutionResponse(
            thread_id=thread_id,
            branch_id=branch_id,
            lineage=lineage,
            inherited_from=inherited,
            inherited_from_branch="original" if inherited else None,
            origin_checkpoint=inherited,
            data_complete=bool(analysis_raw and task_raw),
            workflow_intent=_safe_text(values.get("workflow_intent"), 100) or None,
            project_name=implementation.project_name,
            terminal_status=terminal_status,
            planning=planning,
            planner_knowledge=planner_knowledge_summary(values),
            implementation=implementation,
            testing=testing,
            agent_performance=(
                dict(values.get("agent_performance_evaluations") or {})
                if isinstance(values.get("agent_performance_evaluations"), dict)
                else {}
            ),
            failure_attribution=(
                dict(values.get("failure_attribution") or {})
                if isinstance(values.get("failure_attribution"), dict)
                else {}
            ),
            git=git,
            qa_knowledge=qa_knowledge_summary(values),
            repair_knowledge=repair_knowledge_summary(values),
            workflow_learning=workflow_learning,
            supervisor=supervisor,
            final_result=WorkflowFinalResult(
                terminal_status=terminal_status,
                summary=_safe_text(values.get("final_response")) or None,
                project_created=bool(values.get("project_created")),
                tests_passed=bool(values.get("tests_passed")),
                failure_type=testing.failure_type,
                failure_message=testing.failure_message,
            ),
            events=[_safe_event(event) for event in events] if include_events else None,
            created_at=created_at,
            updated_at=updated_at or (
                datetime.fromisoformat(checkpoint_time.replace("Z", "+00:00"))
                if checkpoint_time else None
            ),
        )
