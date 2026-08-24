from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.workflow_execution_service import (
    ExecutionBranchNotFoundError,
    WorkflowExecutionService,
    event_matches_task_attempt,
    repair_legacy_mojibake,
)
from graph.persistence_service import (
    CheckpointDetails,
    WorkflowNotFoundError,
    WorkflowSnapshot,
)
from streaming import EventStatus, WorkflowEvent, WorkflowEventType


NOW = datetime(2026, 7, 29, tzinfo=UTC)


def event(
    sequence: int,
    event_type: WorkflowEventType,
    stage: str,
    *,
    branch_id: str = "original",
    data: dict | None = None,
    timestamp: datetime | None = None,
) -> WorkflowEvent:
    return WorkflowEvent(
        event_id=uuid4(),
        thread_id="execution-thread",
        sequence=sequence,
        type=event_type,
        timestamp=timestamp or NOW,
        source=f"graph.{stage}",
        stage=stage,
        status=EventStatus.COMPLETED,
        message=None,
        data={"branch_id": branch_id, **(data or {})},
    )


def complete_values(**updates):
    values = {
        "original_user_message": "Build a FastAPI health API",
        "workflow_intent": "create_project",
        "created_project_name": "execution-api",
        "project_name": "execution-api",
        "planning_result": {
            "valid": True,
            "attempts": 1,
            "analysis": {
                "objective": "Build a minimal executable FastAPI project",
                "project_name": "execution-api",
                "project_type": "FastAPI",
                "functional_requirements": ["GET /health", "Add tests"],
                "non_functional_requirements": ["Keep dependencies minimal"],
                "constraints": ["Python 3.11"],
                "assumptions": ["Local execution"],
            },
            "acceptance_criteria": [
                {"id": "AC1", "description": "Returns 200", "verification_method": "pytest"},
                {"id": "AC2", "description": "Returns status ok", "verification_method": "pytest"},
            ],
            "tasks": [
                {
                    "order": 1,
                    "role": "Backend Developer",
                    "title": "Create API",
                    "description": "Implement the FastAPI endpoint",
                },
                {
                    "order": 2,
                    "role": "QA Reviewer",
                    "title": "Validate API",
                    "description": "Run the generated tests",
                },
            ],
            "hybrid_evaluation": {
                "status": "completed",
                "source": "deterministic_and_judge",
                "score": 88.8,
                "confidence": 0.82,
                "agreement": "aligned",
                "dimensions": {},
                "flags": [],
                "recommendation": "continue",
                "version": "8.2-v1",
                "weights": {"deterministic_quality": 0.7, "judge_overall": 0.3},
            },
        },
        "requirement_analysis": {"objective": "legacy"},
        "planning_valid": True,
        "planning_attempts": 1,
        "planning_errors": [],
        "implementation_result": {
            "valid": True,
            "attempts": 2,
            "package_name": "execution_api",
            "generated_files": [
                "execution_api/main.py",
                "tests/test_health.py",
            ],
            "project_created": True,
            "environment_prepared": True,
        },
        "implementation_valid": True,
        "implementation_attempts": 2,
        "project_implementation": {
            "project_name": "execution-api",
            "framework": "fastapi",
            "package_name": "execution_api",
            "files": [
                {"path": "execution_api/main.py", "content": "source"},
                {"path": "tests/test_health.py", "content": "test source"},
            ],
        },
        "implementation_failure_reason": "Dependencies required normalization",
        "resolved_validation_errors": ["dependencies normalized"],
        "generated_package_name": "execution_api",
        "generated_files": [
            "execution_api/main.py",
            "tests/test_health.py",
        ],
        "project_created": True,
        "environment_prepared": True,
        "dependencies_installed": True,
        "dependency_policy_applied": True,
        "dependency_normalization_attempts": 1,
        "installed_fastapi_version": "fastapi 0.116",
        "installed_starlette_version": "starlette 0.47",
        "testing_result": {
            "tests_executed": True,
            "tests_passed": True,
            "summary": "1 passed, 2 warnings",
            "repair_phase": "completed",
            "repair_attempts": 1,
        },
        "agent_performance_evaluations": {
            "developer": {
                "agent": "developer",
                "status": "completed",
                "score": 75,
                "level": "good",
                "confidence": 0.95,
                "metrics": {"first_pass_success": False, "required_repair": True},
                "strengths": [],
                "issues": ["Implementation required downstream repair."],
                "reason_codes": ["repair_required_after_implementation"],
                "version": "8.3-v1",
            },
            "repair": {
                "agent": "repair",
                "status": "completed",
                "score": 100,
                "level": "excellent",
                "confidence": 0.95,
                "metrics": {"repair_success": True},
                "strengths": ["Repair recovered the failing tests."],
                "issues": [],
                "reason_codes": ["repair_success"],
                "version": "8.3-v1",
            },
        },
        "failure_attribution": {
            "status": "completed",
            "failure_class": "implementation_defect",
            "root_cause": "developer",
            "primary_attribution": "developer",
            "contributors": [
                {"source": "developer", "contribution": "caused", "confidence": 0.9, "reason_codes": ["root_cause_test_failure_repaired"]},
                {"source": "qa", "contribution": "detected", "confidence": 0.9, "reason_codes": ["qa_detected_failure"]},
                {"source": "repair", "contribution": "resolved", "confidence": 0.95, "reason_codes": ["repair_resolved_failure"]},
            ],
            "excluded_attributions": [{"source": "planner", "reason": "planning_valid"}],
            "confidence": 0.9,
            "evidence": {"repair_attempts": 1, "tests_passed_after_repair": True},
            "reason_codes": ["root_cause_test_failure_repaired"],
            "recovered": True,
            "recovery_source": "repair",
            "causal_chain": ["planner_valid", "developer_output_created", "tests_failed", "repair_resolved"],
            "version": "8.4-v1",
        },
        "tests_executed": True,
        "tests_passed": True,
        "detected_test_framework": "pytest",
        "expected_test_command": [
            r"C:\workspace\execution-api\.venv\Scripts\python.exe",
            "-m",
            "pytest",
            "tests",
            "-q",
        ],
        "actual_test_command": [
            r"C:\workspace\execution-api\.venv\Scripts\python.exe",
            "-m",
            "pytest",
            "tests",
            "-q",
        ],
        "test_warning_count": 2,
        "final_test_result_summary": "1 passed, 2 warnings",
        "repair_phase": "completed",
        "repair_attempts": 1,
        "repair_decision": "The test was corrected.",
        "repair_before": '{"status": "healthy"}',
        "repair_after": '{"status": "ok"}',
        "failing_test_files": ["tests/test_health.py"],
        "files_read_during_repair": ["tests/test_health.py", "execution_api/main.py"],
        "files_updated_during_repair": ["tests/test_health.py"],
        "supervisor_decision": "finalize",
        "supervisor_decision_source": "model",
        "supervisor_confidence": 0.95,
        "supervisor_attempts": 4,
        "supervisor_errors": [],
        "handoff_history": [
            {
                "sequence": 1,
                "from": "supervisor",
                "attempted_to": "planning",
                "selected_to": "planning",
                "executed_to": "planning",
                "reason": "Requirement needs a plan",
                "source": "model",
                "confidence": 0.9,
                "progress_fingerprint": {"secret": "hidden"},
            }
        ],
        "terminal_status": "completed",
        "final_response": "Project completed with passing tests.",
    }
    values.update(updates)
    return values


def qa_complete_values(**updates):
    values = complete_values(
        repair_phase="not_started",
        repair_attempts=0,
        repair_before=None,
        repair_after=None,
        files_read_during_repair=[],
        files_updated_during_repair=[],
        testing_result={
            "tests_executed": True,
            "tests_passed": True,
            "summary": "1 passed, 2 warnings",
            "repair_phase": "not_started",
            "repair_attempts": 0,
        },
    )
    values.update(updates)
    return values


class FakePersistence:
    def __init__(self, values=None, *, missing=False, branch_values=None):
        self.values = complete_values() if values is None else values
        self.missing = missing
        self.branch_values = branch_values
        self.snapshot_calls = 0
        self.checkpoint_calls = 0

    async def get_snapshot(self, thread_id: str):
        self.snapshot_calls += 1
        if self.missing:
            raise WorkflowNotFoundError(thread_id)
        return WorkflowSnapshot(
            thread_id=thread_id,
            checkpoint_id="head",
            next_nodes=(),
            values=self.values,
            metadata={},
            created_at=NOW.isoformat(),
            interrupts=(),
        )

    async def get_checkpoint(self, thread_id: str, checkpoint_id: str):
        self.checkpoint_calls += 1
        if self.branch_values is None:
            from graph.persistence_service import CheckpointNotFoundError
            raise CheckpointNotFoundError(checkpoint_id)
        return CheckpointDetails(
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            step=1,
            source="fork",
            next_nodes=(),
            values=self.branch_values,
            metadata={},
            interrupted=False,
        )


class FakeEventStore:
    def __init__(self, events=None):
        self.events = list(events or [])
        self.calls = 0

    async def get_events(
        self,
        thread_id,
        *,
        branch_id="original",
        limit=None,
        **_kwargs,
    ):
        self.calls += 1
        selected = [
            item for item in self.events
            if item.data.get("branch_id", "original") == branch_id
        ]
        return selected[:limit] if limit else selected


class FakeMetadata:
    def __init__(self):
        self.calls = []

    async def event_timestamps(self, thread_id, branch_id="original"):
        self.calls.append((thread_id, branch_id))
        return NOW, NOW


def standard_events(branch_id="original"):
    return [
        event(1, WorkflowEventType.PLANNING_STARTED, "planning", branch_id=branch_id),
        event(2, WorkflowEventType.PLANNING_COMPLETED, "planning", branch_id=branch_id),
        event(3, WorkflowEventType.IMPLEMENTATION_STARTED, "implementation", branch_id=branch_id),
        event(4, WorkflowEventType.IMPLEMENTATION_COMPLETED, "implementation", branch_id=branch_id),
        event(5, WorkflowEventType.TESTING_STARTED, "testing", branch_id=branch_id),
        event(6, WorkflowEventType.TESTING_COMPLETED, "testing", branch_id=branch_id),
    ]


def qa_events(
    branch_id: str = "original",
    *,
    attempt: int = 0,
) -> list[WorkflowEvent]:
    return [
        event(
            40,
            WorkflowEventType.IMPLEMENTATION_COMPLETED,
            "implementation",
            branch_id=branch_id,
            timestamp=NOW + timedelta(seconds=0),
        ),
        event(
            41,
            WorkflowEventType.TESTING_STARTED,
            "testing_repair",
            branch_id=branch_id,
            timestamp=NOW + timedelta(seconds=1),
        ),
        event(
            42,
            WorkflowEventType.APPROVAL_REQUIRED,
            "run_tests",
            branch_id=branch_id,
            data={
                "operation": "run_tests",
                "tool_name": "testing__run_tests",
                "attempt": attempt,
            },
            timestamp=NOW + timedelta(seconds=2),
        ),
        event(
            43,
            WorkflowEventType.WORKFLOW_RESUMED,
            "run_tests",
            branch_id=branch_id,
            data={
                "operation": "run_tests",
                "tool_name": "testing__run_tests",
            },
            timestamp=NOW + timedelta(seconds=3),
        ),
        event(
            44,
            WorkflowEventType.APPROVAL_GRANTED,
            "run_tests",
            branch_id=branch_id,
            data={
                "operation": "run_tests",
                "tool_name": "testing__run_tests",
                "attempt": attempt,
            },
            timestamp=NOW + timedelta(seconds=4),
        ),
        event(
            45,
            WorkflowEventType.TEST_RUN_STARTED,
            "testing_repair",
            branch_id=branch_id,
            data={"attempt": attempt},
            timestamp=NOW + timedelta(seconds=5),
        ),
        event(
            46,
            WorkflowEventType.TOOL_STARTED,
            "testing_repair",
            branch_id=branch_id,
            data={"tool": "run_tests", "server": "testing", "attempt": attempt},
            timestamp=NOW + timedelta(seconds=6),
        ),
        event(
            47,
            WorkflowEventType.TOOL_COMPLETED,
            "testing_repair",
            branch_id=branch_id,
            data={"tool": "run_tests", "server": "testing", "attempt": attempt},
            timestamp=NOW + timedelta(seconds=7),
        ),
        event(
            48,
            WorkflowEventType.TEST_RUN_COMPLETED,
            "testing_repair",
            branch_id=branch_id,
            data={"attempt": attempt},
            timestamp=NOW + timedelta(seconds=8),
        ),
        event(
            49,
            WorkflowEventType.TESTING_COMPLETED,
            "testing_repair",
            branch_id=branch_id,
            timestamp=NOW + timedelta(seconds=9),
        ),
    ]


def service(values=None, *, missing=False, events=None, branch_values=None):
    persistence = FakePersistence(
        values, missing=missing, branch_values=branch_values
    )
    event_store = FakeEventStore(events if events is not None else standard_events())
    metadata = FakeMetadata()
    return (
        WorkflowExecutionService(
            persistence=persistence,
            event_store=event_store,
            metadata_store=metadata,
        ),
        persistence,
        event_store,
        metadata,
    )


@pytest.mark.asyncio
async def test_execution_thread_not_found():
    selected, *_ = service(missing=True)
    with pytest.raises(WorkflowNotFoundError):
        await selected.get_execution("missing")


@pytest.mark.asyncio
async def test_execution_branch_not_found():
    selected, *_ = service(events=[])
    with pytest.raises(ExecutionBranchNotFoundError):
        await selected.get_execution("execution-thread", branch_id="fork")


@pytest.mark.asyncio
async def test_new_workflow_returns_partial_execution():
    selected, *_ = service(values={}, events=[])
    result = await selected.get_execution("execution-thread")
    assert result.data_complete is False
    assert result.planning.tasks == []
    assert result.testing.executed is False


@pytest.mark.asyncio
async def test_planning_partial_does_not_invent_tasks():
    selected, *_ = service(
        values={"planning_attempts": 1, "terminal_status": None},
        events=standard_events()[:1],
    )
    result = await selected.get_execution("execution-thread")
    assert result.planning.valid is False
    assert result.planning.tasks == []


@pytest.mark.asyncio
async def test_complete_execution_projects_grouped_results():
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert result.planning.valid is True
    assert result.planning.hybrid_evaluation.score == 88.8
    assert result.planning.hybrid_evaluation.recommendation == "continue"
    assert result.implementation.valid is True
    assert result.implementation.project_implementation is not None
    assert result.implementation.project_implementation["package_name"] == "execution_api"
    assert result.testing.passed is True
    assert result.agent_performance["developer"].metrics["required_repair"] is True
    assert result.agent_performance["repair"].level == "excellent"
    assert result.failure_attribution.root_cause == "developer"
    assert result.failure_attribution.recovered is True


@pytest.mark.asyncio
async def test_execution_exposes_safe_workflow_learning_summary():
    candidate = {
        "candidate_id": "candidate-1", "knowledge_type": "solution", "confidence": .9,
        "source_reference": "workflow:execution-thread:solution:candidate-1",
        "created_at": NOW.isoformat(), "content": "large private content",
    }
    selected, *_ = service(values=complete_values(
        workflow_learning_state="submitted",
        workflow_learning_candidates=[candidate],
        workflow_learning_submission_results=[{
            "candidate_id": "candidate-1", "submission_status": "candidate",
            "knowledge_id": "knowledge-1",
        }],
    ))
    result = await selected.get_execution("execution-thread")
    assert result.workflow_learning.state == "submitted"
    assert result.workflow_learning.extracted_count == 1
    assert result.workflow_learning.submitted_count == 1
    assert result.workflow_learning.candidates[0].knowledge_id == "knowledge-1"
    assert "content" not in result.workflow_learning.candidates[0].model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("objective", "Build a minimal executable FastAPI project"),
        ("functional_requirements", ["GET /health", "Add tests"]),
        ("non_functional_requirements", ["Keep dependencies minimal"]),
        ("acceptance_criteria", ["Returns 200", "Returns status ok"]),
        ("assumptions", ["Local execution"]),
        ("constraints", ["Python 3.11"]),
        ("completed", True),
        ("source", "planning_result"),
    ],
)
async def test_analysis_fields(field, expected):
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert getattr(result.planning.analysis, field) == expected


@pytest.mark.asyncio
async def test_tasks_are_ordered():
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert [task.order for task in result.planning.tasks] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_task_ids_are_stable_across_reads():
    selected, *_ = service()
    first = await selected.get_execution("execution-thread")
    second = await selected.get_execution("execution-thread")
    assert [task.task_id for task in first.planning.tasks] == [
        task.task_id for task in second.planning.tasks
    ]


@pytest.mark.asyncio
async def test_task_ids_are_branch_specific():
    branch_events = standard_events("fork-1")
    branch_events[-1].data["checkpoint_id"] = "fork-head"
    selected, *_ = service(
        events=branch_events,
        branch_values=complete_values(),
    )
    original_service, *_ = service()
    original = await original_service.get_execution("execution-thread")
    fork = await selected.get_execution("execution-thread", branch_id="fork-1")
    assert original.planning.tasks[0].task_id != fork.planning.tasks[0].task_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_fragment", "expected"),
    [
        ("Business Analyst", "completed"),
        ("Software Architect", "completed"),
        ("Backend Developer", "completed"),
        ("QA Reviewer", "completed"),
    ],
)
async def test_completed_task_statuses(agent_fragment, expected):
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    task = next(task for task in result.planning.tasks if agent_fragment in task.agent)
    assert task.status == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pending_operation", "agent", "expected"),
    [
        ("create_project", "Backend Developer", "waiting"),
        ("prepare_environment", "Backend Developer", "waiting"),
        ("run_tests", "QA Reviewer", "waiting"),
        ("apply_fix", "QA Reviewer", "waiting"),
    ],
)
async def test_waiting_task_statuses(pending_operation, agent, expected):
    values = complete_values(
        pending_operation=pending_operation,
        tests_passed=False,
        environment_prepared=False,
        terminal_status=None,
    )
    selected, *_ = service(values=values)
    result = await selected.get_execution("execution-thread")
    task = next(task for task in result.planning.tasks if agent in task.agent)
    assert task.status == expected


@pytest.mark.asyncio
async def test_failed_qa_is_not_marked_completed():
    selected, *_ = service(values=complete_values(
        tests_passed=False,
        terminal_status="tests_failed",
    ))
    result = await selected.get_execution("execution-thread")
    task = next(task for task in result.planning.tasks if "QA" in task.agent)
    assert task.status == "failed"


@pytest.mark.asyncio
async def test_related_events_are_attached_by_stage():
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert all(task.related_event_ids for task in result.planning.tasks)


@pytest.mark.asyncio
async def test_related_completion_event_is_the_navigation_target():
    events = standard_events()
    selected, *_ = service(events=events)
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    assert qa.primary_event_id == str(events[-1].event_id)


@pytest.mark.asyncio
async def test_implementation_task_has_generated_files():
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    developer = next(task for task in result.planning.tasks if "Developer" in task.agent)
    assert developer.related_files == [
        "execution_api/main.py",
        "tests/test_health.py",
    ]


@pytest.mark.asyncio
async def test_qa_task_has_updated_files():
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    assert qa.related_files == [
        "execution_api/main.py",
        "tests/test_health.py",
    ]


@pytest.mark.asyncio
async def test_qa_completed_has_real_testing_repair_events():
    events = qa_events()
    selected, *_ = service(values=qa_complete_values(), events=events)
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    assert qa.related_event_ids == [str(item.event_id) for item in events[1:]]
    assert qa.primary_event_id == str(events[-2].event_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    [
        WorkflowEventType.TESTING_STARTED,
        WorkflowEventType.TEST_RUN_STARTED,
        WorkflowEventType.TOOL_STARTED,
        WorkflowEventType.TOOL_COMPLETED,
        WorkflowEventType.TEST_RUN_COMPLETED,
        WorkflowEventType.TESTING_COMPLETED,
    ],
)
async def test_qa_relates_each_testing_event_type(event_type):
    events = qa_events()
    expected = next(item for item in events if item.type == event_type)
    selected, *_ = service(values=qa_complete_values(), events=events)
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    assert str(expected.event_id) in qa.related_event_ids


@pytest.mark.asyncio
async def test_qa_does_not_relate_implementation_events():
    events = qa_events()
    selected, *_ = service(values=qa_complete_values(), events=events)
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    assert str(events[0].event_id) not in qa.related_event_ids


@pytest.mark.asyncio
async def test_qa_does_not_relate_another_branch():
    events = [*qa_events(), *qa_events("fork-1")]
    selected, *_ = service(values=qa_complete_values(), events=events)
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    fork_ids = {str(item.event_id) for item in events if item.data["branch_id"] == "fork-1"}
    assert not fork_ids.intersection(qa.related_event_ids)


@pytest.mark.asyncio
async def test_qa_does_not_relate_another_attempt():
    events = [*qa_events(attempt=0), *qa_events(attempt=4)]
    selected, *_ = service(values=qa_complete_values(), events=events)
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    other_attempt_ids = {
        str(item.event_id)
        for item in events
        if item.data.get("attempt") == 4
    }
    assert not other_attempt_ids.intersection(qa.related_event_ids)


@pytest.mark.parametrize(
    ("event_attempt", "task_attempt", "expected"),
    [
        (0, 1, True),
        (1, 1, True),
        (2, 1, False),
        (1, 2, True),
        (0, 2, False),
        (None, 2, True),
    ],
)
def test_event_attempt_matching(event_attempt, task_attempt, expected):
    assert event_matches_task_attempt(event_attempt, task_attempt) is expected


@pytest.mark.asyncio
async def test_qa_related_event_ids_are_ordered_and_deduplicated():
    events = qa_events()
    duplicate = events[2].model_copy(update={"sequence": 99})
    selected, *_ = service(
        values=qa_complete_values(),
        events=[*reversed(events), duplicate],
    )
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    sequence_by_id = {str(item.event_id): item.sequence for item in events}
    assert qa.related_event_ids == list(dict.fromkeys(qa.related_event_ids))
    assert [
        sequence_by_id[event_id] for event_id in qa.related_event_ids
    ] == sorted(sequence_by_id[event_id] for event_id in qa.related_event_ids)


@pytest.mark.asyncio
async def test_qa_timestamps_come_from_test_run_events():
    events = qa_events()
    selected, *_ = service(values=qa_complete_values(), events=events)
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    assert qa.started_at == events[5].timestamp
    assert qa.completed_at == events[8].timestamp
    assert qa.started_at <= qa.completed_at


@pytest.mark.asyncio
async def test_qa_files_include_tests_and_reject_unsafe_paths():
    values = complete_values(
        generated_files=[
            "tests/test_health.py",
            r"C:\workspace\tests\test_secret.py",
            ".venv/test.py",
        ],
        implementation_result={
            **complete_values()["implementation_result"],
            "generated_files": [
                "tests/test_health.py",
                r"C:\workspace\tests\test_secret.py",
                ".venv/test.py",
            ],
        },
        failing_test_files=["tests/test_health.py", "../outside.py"],
        files_read_during_repair=[],
        files_updated_during_repair=[],
    )
    selected, *_ = service(values=values, events=qa_events())
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    assert qa.related_files == ["tests/test_health.py"]


@pytest.mark.asyncio
async def test_waiting_qa_uses_run_tests_approval_as_primary():
    events = qa_events()[:3]
    values = qa_complete_values(
        pending_operation="run_tests",
        tests_executed=False,
        tests_passed=False,
        terminal_status=None,
    )
    selected, *_ = service(values=values, events=events)
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    assert qa.status == "waiting"
    assert qa.primary_event_id == str(events[-1].event_id)


@pytest.mark.asyncio
async def test_running_qa_uses_active_test_event():
    events = qa_events()[:7]
    values = qa_complete_values(
        pending_operation=None,
        tests_executed=True,
        tests_passed=False,
        terminal_status=None,
    )
    selected, *_ = service(values=values, events=events)
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    assert qa.status == "running"
    assert qa.primary_event_id == str(events[5].event_id)


@pytest.mark.asyncio
async def test_repair_events_are_related_to_qa():
    events = qa_events()
    events.extend([
        event(
            50,
            WorkflowEventType.REPAIR_STARTED,
            "repair",
            data={"repair_attempts": 1},
        ),
        event(51, WorkflowEventType.REPAIR_COMPLETED, "repair"),
    ])
    selected, *_ = service(events=events)
    result = await selected.get_execution("execution-thread")
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    assert str(events[-2].event_id) in qa.related_event_ids
    assert str(events[-1].event_id) in qa.related_event_ids


@pytest.mark.asyncio
async def test_primary_event_is_stable_across_reads():
    selected, *_ = service(values=qa_complete_values(), events=qa_events())
    first = await selected.get_execution("execution-thread")
    second = await selected.get_execution("execution-thread")
    first_qa = next(task for task in first.planning.tasks if "QA" in task.agent)
    second_qa = next(task for task in second.planning.tasks if "QA" in task.agent)
    assert first_qa.primary_event_id == second_qa.primary_event_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("dependency_policy_applied", True),
        ("dependency_normalization_attempts", 1),
        ("environment_prepared", True),
        ("dependencies_installed", True),
        ("generated_files", ["execution_api/main.py", "tests/test_health.py"]),
        ("updated_files", ["tests/test_health.py"]),
    ],
)
async def test_implementation_fields(field, expected):
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert getattr(result.implementation, field) == expected


@pytest.mark.asyncio
async def test_application_and_testing_frameworks_are_separated():
    values = complete_values()
    values["implementation_result"] = {
        **values["implementation_result"],
        "framework": "pytest",
    }
    selected, *_ = service(values=values)
    result = await selected.get_execution("execution-thread")
    assert result.implementation.framework == "FastAPI"
    assert result.testing.framework == "pytest"


@pytest.mark.asyncio
async def test_installed_dependencies_include_names_and_versions():
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert result.implementation.installed_dependencies == [
        "fastapi==0.116",
        "starlette==0.47",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("framework", "pytest"),
        ("summary", "1 passed, 2 warnings"),
        ("warnings", 2),
        ("repair_phase", "completed"),
        ("repair_attempts", 1),
        ("failing_test_files", ["tests/test_health.py"]),
        ("files_read_during_repair", ["tests/test_health.py", "execution_api/main.py"]),
        ("files_updated_during_repair", ["tests/test_health.py"]),
    ],
)
async def test_testing_fields(field, expected):
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert getattr(result.testing, field) == expected


@pytest.mark.asyncio
async def test_repair_before_after_are_structured():
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert result.testing.repair_before == {"status": "healthy"}
    assert result.testing.repair_after == {"status": "ok"}


@pytest.mark.asyncio
async def test_repair_not_started_has_defaults():
    selected, *_ = service(values=complete_values(
        testing_result={},
        repair_phase="not_started",
        repair_attempts=0,
        repair_before=None,
        repair_after=None,
    ))
    result = await selected.get_execution("execution-thread")
    assert result.testing.repair_phase == "not_started"
    assert result.testing.repair_attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("decision", "finalize"),
        ("decision_source", "model"),
        ("confidence", 0.95),
        ("attempts", 4),
        ("loop_detected", False),
    ],
)
async def test_supervisor_fields(field, expected):
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert getattr(result.supervisor, field) == expected


@pytest.mark.asyncio
async def test_handoff_history_is_sanitized():
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert result.supervisor.handoff_history
    assert "progress_fingerprint" not in result.supervisor.handoff_history[0]


@pytest.mark.asyncio
async def test_supervisor_fallback_is_visible():
    selected, *_ = service(values=complete_values(
        supervisor_decision_source="fallback",
    ))
    result = await selected.get_execution("execution-thread")
    assert result.supervisor.decision_source == "fallback"


@pytest.mark.asyncio
async def test_supervisor_loop_is_visible():
    selected, *_ = service(values=complete_values(
        terminal_status="supervisor_loop_detected",
        supervisor_errors=["supervisor_loop_detected"],
    ))
    result = await selected.get_execution("execution-thread")
    assert result.supervisor.loop_detected is True


@pytest.mark.asyncio
async def test_legacy_flat_state_backfills_execution():
    values = complete_values()
    values["planning_result"] = {}
    values["implementation_result"] = {}
    values["testing_result"] = {}
    values["requirement_analysis"] = complete_values()["planning_result"]["analysis"]
    values["acceptance_criteria"] = complete_values()["planning_result"]["acceptance_criteria"]
    values["implementation_tasks"] = complete_values()["planning_result"]["tasks"]
    selected, *_ = service(values=values)
    result = await selected.get_execution("execution-thread")
    assert result.planning.analysis.source == "legacy_snapshot"
    assert result.planning.tasks


@pytest.mark.asyncio
async def test_include_events_defaults_to_none():
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert result.events is None


@pytest.mark.asyncio
async def test_include_events_is_controlled():
    selected, *_ = service()
    result = await selected.get_execution("execution-thread", include_events=True)
    assert len(result.events or []) == 6


@pytest.mark.asyncio
async def test_include_events_contains_sanitized_qa_events():
    events = qa_events()
    events[-2].message = "password=hidden"
    selected, *_ = service(events=events)
    result = await selected.get_execution(
        "execution-thread",
        include_events=True,
    )
    qa = next(task for task in result.planning.tasks if "QA" in task.agent)
    included_ids = {str(item.event_id) for item in result.events or []}
    assert set(qa.related_event_ids).issubset(included_ids)
    assert "password=hidden" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_branch_uses_branch_events_and_state():
    branch_events = standard_events("fork-1")
    branch_events[-1].data.update({
        "checkpoint_id": "fork-head",
        "lineage": "fork",
        "origin_checkpoint": "origin-head",
    })
    selected, persistence, event_store, metadata = service(
        events=branch_events,
        branch_values=complete_values(repair_decision="Fork repair"),
    )
    result = await selected.get_execution("execution-thread", branch_id="fork-1")
    assert result.branch_id == "fork-1"
    assert result.lineage == "fork"
    assert result.origin_checkpoint == "origin-head"
    assert result.inherited_from_branch == "original"
    assert persistence.checkpoint_calls == 1
    assert event_store.calls == 2
    assert metadata.calls == [("execution-thread", "fork-1")]


@pytest.mark.asyncio
async def test_commands_do_not_expose_absolute_paths():
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert result.testing.actual_command[0] == "python.exe"
    assert "C:\\" not in " ".join(result.testing.actual_command)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../secret.txt",
        r"C:\Windows\secret.txt",
        "/etc/passwd",
        ".env",
        ".venv/token",
    ],
)
async def test_unsafe_file_paths_are_removed(unsafe_path):
    selected, *_ = service(values=complete_values(
        generated_files=[unsafe_path, "safe/main.py"],
        implementation_result={
            **complete_values()["implementation_result"],
            "generated_files": [unsafe_path, "safe/main.py"],
        },
    ))
    result = await selected.get_execution("execution-thread")
    assert result.implementation.generated_files == ["safe/main.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret",
    [
        "OPENAI_API_KEY=sk-abcdefghijklmnop",
        "Bearer abc.def.secret",
        "password=hunter2",
        "secret=private-value",
    ],
)
async def test_secret_patterns_are_redacted(secret):
    selected, *_ = service(values=complete_values(
        final_response=f"Completed {secret}",
        failure_message=secret,
    ))
    result = await selected.get_execution("execution-thread")
    encoded = result.model_dump_json()
    assert secret not in encoded
    assert "[redacted]" in encoded


@pytest.mark.asyncio
async def test_private_reasoning_keys_are_not_exposed():
    values = complete_values()
    values["handoff_history"] = [{
        "from": "supervisor",
        "to": "planning",
        "chain_of_thought": "private",
        "system_prompt": "hidden",
        "scratchpad": "hidden",
    }]
    selected, *_ = service(values=values)
    result = await selected.get_execution("execution-thread")
    encoded = result.model_dump_json()
    assert "chain_of_thought" not in encoded
    assert "system_prompt" not in encoded
    assert "scratchpad" not in encoded


@pytest.mark.parametrize(
    ("damaged", "expected"),
    [
        ("implementaciÃ³n", "implementación"),
        ("mÃ­nima", "mínima"),
        ("Se implementarÃ¡ Ãºnicamente", "Se implementará únicamente"),
        ("aceptaciÃ³n", "aceptación"),
        ("implementaciÃƒÂ³n", "implementación"),
    ],
)
def test_legacy_mojibake_is_repaired(damaged, expected):
    assert repair_legacy_mojibake(damaged) == expected


@pytest.mark.parametrize(
    "legitimate",
    [
        "implementación mínima",
        "João",
        "API de salud",
        "Verificar aceptación",
    ],
)
def test_legitimate_unicode_is_not_modified(legitimate):
    assert repair_legacy_mojibake(legitimate) == legitimate


def test_mojibake_repair_is_idempotent():
    repaired = repair_legacy_mojibake("implementaciÃ³n")
    assert repair_legacy_mojibake(repaired) == repaired


@pytest.mark.asyncio
async def test_execution_response_repairs_persisted_mojibake():
    values = complete_values(original_user_message="ImplementaciÃ³n mÃ­nima")
    values["planning_result"]["analysis"] = {
        **values["planning_result"]["analysis"],
        "objective": "Se implementarÃ¡ Ãºnicamente el alcance solicitado.",
        "functional_requirements": [
            "Verificar criterios de aceptaciÃ³n",
        ],
    }
    selected, *_ = service(values=values)
    result = await selected.get_execution("execution-thread")
    encoded = result.model_dump_json()
    assert "Implementación mínima" in encoded
    assert "Se implementará únicamente" in encoded
    assert "aceptación" in encoded
    assert "Ã" not in encoded


@pytest.mark.asyncio
async def test_corrupt_repair_json_is_handled():
    selected, *_ = service(values=complete_values(
        repair_before="{not-json",
    ))
    result = await selected.get_execution("execution-thread")
    assert result.testing.repair_before == {"summary": "{not-json"}


@pytest.mark.asyncio
async def test_event_queries_are_bounded():
    selected, _, event_store, _ = service()
    await selected.get_execution("execution-thread")
    assert event_store.calls == 1


@pytest.mark.asyncio
async def test_timestamps_are_durable():
    selected, *_ = service()
    result = await selected.get_execution("execution-thread")
    assert result.created_at == NOW
    assert result.updated_at == NOW


class EndpointExecution:
    def __init__(self, error=None):
        self.error = error

    async def get_execution(self, thread_id, **_kwargs):
        if self.error:
            raise self.error
        selected, *_ = service()
        return await selected.get_execution(thread_id)


def execution_client(execution):
    services = SimpleNamespace(execution=execution)

    @asynccontextmanager
    async def factory():
        yield services

    app = create_app(factory)
    app.state.services = services
    return TestClient(app)


def test_execution_endpoint_maps_missing_thread_to_404():
    client = execution_client(
        EndpointExecution(WorkflowNotFoundError("missing"))
    )
    assert client.get("/api/workflows/missing/execution").status_code == 404


def test_execution_endpoint_maps_missing_branch_to_404():
    client = execution_client(
        EndpointExecution(ExecutionBranchNotFoundError("missing"))
    )
    assert client.get(
        "/api/workflows/execution-thread/execution?branch_id=missing"
    ).status_code == 404


def test_execution_endpoint_returns_typed_payload():
    client = execution_client(EndpointExecution())
    response = client.get("/api/workflows/execution-thread/execution")
    assert response.status_code == 200
    assert response.json()["project_name"] == "execution-api"
