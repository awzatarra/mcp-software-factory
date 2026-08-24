from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.evaluation_models import WorkflowEvaluationResponse
from api.execution_models import WorkflowAgentExecutionResponse
from api.services.workflow_evaluation_service import (
    MAX_SCORE,
    SCORING_VERSION,
    WorkflowEvaluationService,
    attach_finding_navigation,
    calculate_workflow_evaluation,
    duration_breakdown,
    grade_for_score,
)
from api.services.workflow_evaluation_store import WorkflowEvaluationStore
from graph.persistence_service import WorkflowNotFoundError
from streaming.models import WorkflowEvent


NOW = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)


def workflow_event(
    sequence: int,
    event_type: str,
    seconds: int,
    *,
    branch_id: str = "original",
    data: dict | None = None,
) -> WorkflowEvent:
    return WorkflowEvent.model_validate({
        "event_id": uuid4(),
        "thread_id": "evaluation-thread",
        "sequence": sequence,
        "type": event_type,
        "timestamp": NOW + timedelta(seconds=seconds),
        "source": "test",
        "stage": (data or {}).get("stage"),
        "status": (
            "failed" if "failed" in event_type else
            "waiting" if event_type == "approval_required" else
            "completed"
        ),
        "message": None,
        "data": {"branch_id": branch_id, **(data or {})},
    })


def evaluation_events(branch_id: str = "original") -> list[WorkflowEvent]:
    return [
        workflow_event(1, "workflow_started", 0, branch_id=branch_id),
        workflow_event(2, "planning_started", 1, branch_id=branch_id),
        workflow_event(3, "planning_completed", 2, branch_id=branch_id),
        workflow_event(4, "implementation_started", 3, branch_id=branch_id),
        workflow_event(
            5, "approval_required", 4, branch_id=branch_id,
            data={"operation": "create_project"},
        ),
        workflow_event(
            6, "approval_granted", 9, branch_id=branch_id,
            data={"operation": "create_project"},
        ),
        workflow_event(7, "implementation_completed", 20, branch_id=branch_id),
        workflow_event(8, "testing_started", 21, branch_id=branch_id),
        workflow_event(
            9, "approval_required", 22, branch_id=branch_id,
            data={"operation": "run_tests"},
        ),
        workflow_event(
            10, "approval_granted", 24, branch_id=branch_id,
            data={"operation": "run_tests"},
        ),
        workflow_event(11, "test_run_started", 25, branch_id=branch_id),
        workflow_event(12, "test_run_completed", 27, branch_id=branch_id),
        workflow_event(13, "testing_completed", 28, branch_id=branch_id),
        workflow_event(14, "workflow_completed", 30, branch_id=branch_id),
    ]


def execution(**updates) -> WorkflowAgentExecutionResponse:
    payload = {
        "thread_id": "evaluation-thread",
        "branch_id": "original",
        "lineage": "original",
        "inherited_from": None,
        "inherited_from_branch": None,
        "origin_checkpoint": None,
        "data_complete": True,
        "workflow_intent": "create_project",
        "project_name": "evaluation-api",
        "terminal_status": "completed",
        "planning": {
            "valid": True,
            "attempts": 0,
            "project_type": "fastapi",
            "framework": "fastapi",
            "analysis": {
                "requirement": "Create API",
                "objective": "Create a health endpoint",
                "functional_requirements": ["GET /health returns ok"],
                "non_functional_requirements": ["Keep it minimal"],
                "acceptance_criteria": ["Health test passes"],
                "assumptions": [],
                "constraints": [],
                "risks": [],
                "completed": True,
                "source": "planning_result",
                "updated_at": NOW,
            },
            "tasks": [
                {
                    "task_id": "developer-task",
                    "order": 1,
                    "agent": "backend developer",
                    "title": "Implement",
                    "description": "Implement endpoint",
                    "status": "completed",
                    "attempt": 0,
                    "result_summary": "2 files",
                    "primary_event_id": None,
                    "related_event_ids": ["implementation-event"],
                    "related_files": ["app/main.py"],
                    "started_at": NOW,
                    "completed_at": NOW + timedelta(seconds=20),
                },
                {
                    "task_id": "qa-task",
                    "order": 2,
                    "agent": "QA reviewer",
                    "title": "Test",
                    "description": "Run tests",
                    "status": "completed",
                    "attempt": 1,
                    "result_summary": "1 passed",
                    "primary_event_id": "test-event",
                    "related_event_ids": ["test-event"],
                    "related_files": ["tests/test_health.py"],
                    "started_at": NOW + timedelta(seconds=25),
                    "completed_at": NOW + timedelta(seconds=27),
                },
            ],
            "validation_errors": [],
            "refinements": [],
            "started_at": NOW,
            "completed_at": NOW + timedelta(seconds=2),
        },
        "implementation": {
            "valid": True,
            "attempts": 0,
            "project_name": "evaluation-api",
            "package_name": "evaluation_api",
            "framework": "fastapi",
            "generated_files": ["app/main.py", "tests/test_health.py"],
            "updated_files": [],
            "dependency_policy_applied": True,
            "dependency_normalization_attempts": 0,
            "environment_prepared": True,
            "dependencies_installed": True,
            "installed_dependencies": ["fastapi==1.0"],
            "validation_errors": [],
            "refinements": [],
            "started_at": NOW,
            "completed_at": NOW + timedelta(seconds=20),
        },
        "testing": {
            "executed": True,
            "passed": True,
            "framework": "pytest",
            "expected_command": ["python", "-m", "pytest"],
            "actual_command": ["python", "-m", "pytest"],
            "summary": "1 passed",
            "warnings": 0,
            "failure_type": None,
            "failure_stage": None,
            "failure_message": None,
            "failing_test_files": [],
            "repair_phase": "not_started",
            "repair_attempts": 0,
            "repair_decision": None,
            "repair_before": None,
            "repair_after": None,
            "files_read_during_repair": [],
            "files_updated_during_repair": [],
            "started_at": NOW,
            "completed_at": NOW + timedelta(seconds=28),
        },
        "supervisor": {
            "decision": "finalize",
            "decision_source": "deterministic",
            "confidence": 1.0,
            "attempts": 0,
            "errors": [],
            "loop_detected": False,
            "handoff_history": [],
        },
        "final_result": {
            "terminal_status": "completed",
            "summary": "Done",
            "project_created": True,
            "tests_passed": True,
            "failure_type": None,
            "failure_message": None,
        },
        "events": None,
        "created_at": NOW,
        "updated_at": NOW + timedelta(seconds=30),
    }
    for path, value in updates.items():
        target = payload
        parts = path.split("__")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return WorkflowAgentExecutionResponse.model_validate(payload)


def evaluate(
    selected: WorkflowAgentExecutionResponse | None = None,
    events: list[WorkflowEvent] | None = None,
    **updates,
) -> WorkflowEvaluationResponse:
    return calculate_workflow_evaluation(
        selected or execution(**updates),
        events if events is not None else evaluation_events(),
        calculated_at=NOW + timedelta(seconds=31),
    )


@pytest.mark.parametrize(
    ("score", "grade"),
    [(100, "excellent"), (90, "excellent"), (89, "good"), (75, "good"),
     (74, "acceptable"), (60, "acceptable"), (59, "poor"), (40, "poor"),
     (39, "critical"), (0, "critical")],
)
def test_grade_boundaries(score: int, grade: str) -> None:
    assert grade_for_score(score) == grade


def test_successful_evaluation_is_final_and_bounded() -> None:
    result = evaluate()
    assert result.evaluation_status == "final"
    assert 0 <= result.overall_score <= MAX_SCORE
    assert result.overall_score == sum([
        result.planning.score,
        result.implementation.score,
        result.testing.score,
        result.efficiency.score,
        result.reliability.score,
    ])


@pytest.mark.parametrize(
    ("updates", "category", "code"),
    [
        ({"planning__valid": False}, "planning", "PLAN_INVALID"),
        ({"planning__analysis__objective": None}, "planning", "PLANNING_OBJECTIVE_MISSING"),
        ({"planning__analysis__functional_requirements": []}, "planning", "FUNCTIONAL_REQUIREMENTS_MISSING"),
        ({"planning__analysis__acceptance_criteria": []}, "planning", "ACCEPTANCE_CRITERIA_MISSING"),
        ({"planning__tasks": []}, "planning", "PLANNING_TASKS_MISSING"),
        ({"planning__attempts": 2}, "planning", "PLANNING_RETRIES"),
        ({"planning__validation_errors": ["bad"]}, "planning", "PLANNING_VALIDATION_ERRORS"),
        ({"implementation__valid": False}, "implementation", "IMPLEMENTATION_INVALID"),
        ({"implementation__generated_files": []}, "implementation", "GENERATED_FILES_MISSING"),
        ({"implementation__environment_prepared": False}, "implementation", "ENVIRONMENT_NOT_PREPARED"),
        ({"implementation__dependencies_installed": False}, "implementation", "DEPENDENCIES_NOT_INSTALLED"),
        ({"implementation__attempts": 2}, "implementation", "IMPLEMENTATION_RETRIES"),
        ({"implementation__validation_errors": ["bad"]}, "implementation", "IMPLEMENTATION_VALIDATION_ERRORS"),
        ({"testing__executed": False}, "testing", "TESTS_NOT_EXECUTED"),
        ({"testing__passed": False}, "testing", "TESTS_FAILED"),
        ({"testing__warnings": 2}, "testing", "TEST_WARNINGS"),
        ({"testing__failure_type": "test_failure"}, "testing", "TEST_FAILURE_TYPE"),
        ({"testing__repair_attempts": 2}, "testing", "REPAIR_ATTEMPTS"),
        ({"testing__repair_phase": "failed"}, "testing", "REPAIR_FAILED"),
        ({"testing__actual_command": ["pytest"]}, "testing", "TEST_COMMAND_MISMATCH"),
        ({"supervisor__decision_source": "fallback"}, "reliability", "SUPERVISOR_FALLBACK"),
        ({"supervisor__loop_detected": True}, "reliability", "SUPERVISOR_LOOP"),
        ({"supervisor__errors": ["bad"]}, "reliability", "SUPERVISOR_ERRORS"),
        ({"supervisor__confidence": 0.2}, "reliability", "LOW_SUPERVISOR_CONFIDENCE"),
    ],
)
def test_penalty_rules(updates: dict, category: str, code: str) -> None:
    result = evaluate(**updates)
    selected = getattr(result, category)
    assert code in {penalty.code for penalty in selected.penalties}


def test_partial_workflow_does_not_penalize_unstarted_testing() -> None:
    result = evaluate(
        terminal_status="running",
        testing__executed=False,
        testing__passed=False,
        implementation__valid=False,
        implementation__environment_prepared=False,
        implementation__dependencies_installed=False,
    )
    assert result.evaluation_status == "partial"
    assert "TESTS_NOT_EXECUTED" not in {item.code for item in result.penalties}
    assert "TESTING_PENDING" in {item.code for item in result.findings}


@pytest.mark.parametrize("terminal_status", ["failed", "tests_failed"])
def test_failed_workflow_is_final_and_penalized(terminal_status: str) -> None:
    result = evaluate(terminal_status=terminal_status, testing__passed=False)
    assert result.evaluation_status == "final"
    assert "TERMINAL_FAILURE" in {item.code for item in result.penalties}


def test_normal_approval_count_is_not_penalized() -> None:
    events = evaluation_events()
    events.insert(-1, workflow_event(
        14, "approval_required", 28, data={"operation": "prepare_environment"}
    ))
    events[-1] = events[-1].model_copy(update={"sequence": 15})
    result = evaluate(events=events)
    assert "APPROVAL_OVERHEAD" not in {item.code for item in result.penalties}


def test_extra_approval_is_penalized() -> None:
    events = evaluation_events()
    for offset, operation in enumerate(("prepare_environment", "apply_fix"), start=14):
        events.insert(-1, workflow_event(
            offset, "approval_required", offset + 15, data={"operation": operation}
        ))
    events[-1] = events[-1].model_copy(update={"sequence": 16})
    result = evaluate(events=events)
    assert "APPROVAL_OVERHEAD" in {item.code for item in result.penalties}


def test_duplicate_tool_execution_is_penalized() -> None:
    events = evaluation_events()[:-1]
    data = {"tool": "run_tests", "checkpoint_id": "cp", "attempt": 0}
    events.extend([
        workflow_event(14, "tool_started", 28, data=data),
        workflow_event(15, "tool_started", 29, data=data),
        workflow_event(16, "workflow_completed", 30),
    ])
    assert "DUPLICATE_TOOL_EXECUTION" in {
        item.code for item in evaluate(events=events).penalties
    }


def test_high_duration_is_penalized() -> None:
    events = evaluation_events()
    events[-1] = events[-1].model_copy(
        update={"timestamp": NOW + timedelta(seconds=1_201)}
    )
    assert "HIGH_DURATION" in {item.code for item in evaluate(events=events).penalties}


def test_critical_sequence_gap_is_penalized() -> None:
    events = evaluation_events()
    events[-1] = events[-1].model_copy(update={"sequence": 30})
    assert "CRITICAL_EVENT_SEQUENCE_GAP" in {
        item.code for item in evaluate(events=events).penalties
    }


def test_invalid_supervisor_decisions_are_penalized() -> None:
    result = evaluate(
        supervisor__handoff_history=[{"invalid_decision_count": 2}]
    )
    penalty = next(
        item for item in result.penalties
        if item.code == "INVALID_SUPERVISOR_DECISIONS"
    )
    assert penalty.points == -6


def test_requirement_and_acceptance_coverage_are_satisfied() -> None:
    result = evaluate()
    assert result.requirement_coverage.satisfied == 1
    assert result.acceptance_coverage.satisfied == 1


def test_failed_tests_make_coverage_unsatisfied() -> None:
    result = evaluate(testing__passed=False)
    assert result.requirement_coverage.unsatisfied == 1
    assert result.acceptance_coverage.unsatisfied == 1


def test_partial_coverage_is_unknown() -> None:
    result = evaluate(
        terminal_status="running",
        testing__executed=False,
        testing__passed=False,
        implementation__valid=False,
    )
    assert result.requirement_coverage.unknown == 1
    assert result.acceptance_coverage.unknown == 1


def test_recommendations_are_deterministic_and_deduplicated() -> None:
    first = evaluate(testing__warnings=2, testing__repair_attempts=1)
    second = evaluate(testing__warnings=2, testing__repair_attempts=1)
    assert first.recommendations == second.recommendations
    assert len(first.recommendations) == len(set(first.recommendations))


def test_duration_breakdown_and_approval_wait() -> None:
    duration = duration_breakdown(
        evaluation_events(),
        terminal=True,
        now=NOW + timedelta(seconds=31),
    )
    assert duration.total_duration_seconds == 30
    assert duration.planning_seconds == 1
    assert duration.implementation_seconds == 17
    assert duration.testing_seconds == 7
    assert duration.approval_wait_seconds == 7


def test_open_approval_uses_now_without_double_counting() -> None:
    events = evaluation_events()[:5]
    duration = duration_breakdown(
        events,
        terminal=False,
        now=NOW + timedelta(seconds=10),
    )
    assert duration.approval_wait_seconds == 6
    assert duration.total_duration_seconds == 10


def test_scoring_version_and_evidence_policy() -> None:
    hidden = evaluate()
    visible = calculate_workflow_evaluation(
        execution(),
        evaluation_events(),
        include_evidence=True,
        calculated_at=NOW,
    )
    assert hidden.scoring_version == SCORING_VERSION
    assert hidden.evidence is None
    assert visible.evidence
    encoded = visible.model_dump_json()
    assert "chain_of_thought" not in encoded
    assert "system_prompt" not in encoded
    assert "C:\\\\Users\\\\" not in encoded


def test_success_without_repair_has_explicit_bonus() -> None:
    result = evaluate()
    assert "NO_REPAIR_REQUIRED" in {
        item.code for item in result.testing.positive_signals
    }
    assert result.testing.bonuses == []


def test_repaired_workflow_keeps_passed_result_with_visible_penalty() -> None:
    result = evaluate(
        testing__repair_phase="completed",
        testing__repair_attempts=1,
    )
    assert result.testing.score < evaluate().testing.score
    assert "REPAIR_REQUIRED" in {item.code for item in result.findings}
    assert result.terminal_status == "completed"


def test_branch_isolation_penalizes_inconsistent_evidence() -> None:
    selected = execution(branch_id="fork-1", lineage="fork")
    result = evaluate(selected, evaluation_events("original"))
    assert "BRANCH_INCONSISTENT" in {item.code for item in result.penalties}


def test_pending_testing_is_not_scored_as_completed() -> None:
    result = evaluate(
        terminal_status="running",
        testing__executed=False,
        testing__passed=False,
    )
    assert result.testing.score == 0
    assert result.testing.evaluation_state == "not_started"
    assert result.testing.available_points == 0
    assert result.testing.percentage is None
    assert result.available_points == 75
    assert result.earned_points == sum(
        category.score for category in (
            result.planning,
            result.implementation,
            result.efficiency,
            result.reliability,
        )
    )
    assert result.provisional_percentage == round(
        result.earned_points * 100 / 75, 2
    )
    assert result.grade is None
    assert result.provisional_grade is not None
    assert result.is_provisional is True
    assert result.final_grade_available is False


def waiting_create_evaluation(
    *,
    branch_id: str = "original",
) -> tuple[WorkflowEvaluationResponse, list[WorkflowEvent]]:
    events = evaluation_events(branch_id)[:5]
    selected = execution(
        branch_id=branch_id,
        lineage="fork" if branch_id != "original" else "original",
        terminal_status="running",
        final_result__terminal_status="running",
        final_result__project_created=False,
        final_result__tests_passed=False,
        implementation__environment_prepared=False,
        implementation__dependencies_installed=False,
        testing__executed=False,
        testing__passed=False,
    )
    return evaluate(selected, events), events


def test_waiting_create_project_leaves_implementation_not_started() -> None:
    result, _ = waiting_create_evaluation()
    assert result.implementation.evaluation_state == "not_started"
    assert result.implementation.score == 0
    assert result.implementation.available_points == 0
    assert result.implementation.percentage is None
    assert "IMPLEMENTATION_VALID" not in {
        item.code for item in result.implementation.findings
    }
    assert "IMPLEMENTATION_VALID" not in {
        item.code for item in result.implementation.positive_signals
    }
    assert "IMPLEMENTATION_PENDING" in {
        item.code for item in result.implementation.findings
    }


def test_waiting_create_project_uses_only_available_categories() -> None:
    result, _ = waiting_create_evaluation()
    assert result.earned_points == 50
    assert result.available_points == 50
    assert result.provisional_percentage == 100
    assert result.grade is None
    assert result.testing.evaluation_state == "not_started"
    assert result.scoring_version == "1.2"


def test_implementation_pending_links_backend_task_and_create_approval() -> None:
    result, events = waiting_create_evaluation()
    finding = next(
        item for item in result.findings if item.code == "IMPLEMENTATION_PENDING"
    )
    assert finding.related_task_id == "developer-task"
    assert finding.related_event_id == str(events[-1].event_id)
    assert finding.related_file is None


def test_valid_implementation_plan_is_not_executed_implementation() -> None:
    result, _ = waiting_create_evaluation()
    assert result.implementation.evaluation_state == "not_started"
    assert "IMPLEMENTATION_PLAN_VALID" in {
        item.code for item in result.planning.positive_signals
    }


def test_created_project_enables_only_first_nineteen_points() -> None:
    result = evaluate(
        terminal_status="running",
        final_result__terminal_status="running",
        final_result__project_created=True,
        implementation__environment_prepared=False,
        implementation__dependencies_installed=False,
        testing__executed=False,
        testing__passed=False,
    )
    assert result.implementation.evaluation_state == "partial"
    assert result.implementation.available_points == 19
    assert result.implementation.score == 19
    assert result.implementation.percentage == 100
    assert "IMPLEMENTATION_IN_PROGRESS" in {
        item.code for item in result.implementation.findings
    }
    assert "PROJECT_CREATED" in {
        item.code for item in result.implementation.positive_signals
    }
    assert "ENVIRONMENT_PREPARED" not in {
        item.code for item in result.implementation.positive_signals
    }
    assert "DEPENDENCIES_INSTALLED" not in {
        item.code for item in result.implementation.positive_signals
    }


@pytest.mark.parametrize(
    ("environment_prepared", "dependencies_installed", "expected_score", "missing_code"),
    [
        (True, False, 22, "DEPENDENCIES_NOT_INSTALLED"),
        (False, True, 22, "ENVIRONMENT_NOT_PREPARED"),
    ],
)
def test_environment_components_contribute_independently(
    environment_prepared: bool,
    dependencies_installed: bool,
    expected_score: int,
    missing_code: str,
) -> None:
    result = evaluate(
        terminal_status="running",
        final_result__terminal_status="running",
        final_result__project_created=True,
        implementation__environment_prepared=environment_prepared,
        implementation__dependencies_installed=dependencies_installed,
        testing__executed=False,
        testing__passed=False,
    )
    assert result.implementation.evaluation_state == "partial"
    assert result.implementation.available_points == 25
    assert result.implementation.score == expected_score
    assert missing_code in {
        item.code for item in result.implementation.penalties
    }


def test_completed_implementation_exposes_all_points_and_navigation() -> None:
    events = evaluation_events()
    result = evaluate(events=events)
    assert result.implementation.evaluation_state == "evaluated"
    assert result.implementation.available_points == 25
    assert result.implementation.score == 25
    finding = next(
        item for item in result.findings if item.code == "IMPLEMENTATION_VALID"
    )
    assert finding.related_task_id == "developer-task"
    assert finding.related_event_id == str(events[6].event_id)
    assert finding.related_file == "app/main.py"


def test_pending_implementation_navigation_is_branch_isolated() -> None:
    result, _ = waiting_create_evaluation(branch_id="fork-1")
    foreign_events = evaluation_events("original")[:5]
    finding = next(
        item for item in result.findings if item.code == "IMPLEMENTATION_PENDING"
    )
    detached = attach_finding_navigation(
        finding=finding.model_copy(update={"related_event_id": None}),
        execution=execution(
            branch_id="fork-1",
            lineage="fork",
            final_result__project_created=False,
        ),
        events=foreign_events,
        branch_id="fork-1",
    )
    assert detached.related_event_id is None


def test_completed_workflow_exposes_all_points_and_final_grade() -> None:
    result = evaluate()
    assert result.available_points == 100
    assert result.testing.evaluation_state == "evaluated"
    assert result.grade == grade_for_score(result.earned_points)
    assert result.provisional_grade is None
    assert result.final_grade_available is True


def test_first_implementation_attempt_is_not_a_retry() -> None:
    result = evaluate(implementation__attempts=1)
    assert "IMPLEMENTATION_RETRIES" not in {
        item.code for item in result.implementation.penalties
    }
    assert result.evidence is None


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [(2, "1 intento adicional de implementación."), (3, "2 intentos adicionales de implementación.")],
)
def test_real_implementation_retries_have_correct_grammar(
    attempts: int,
    expected: str,
) -> None:
    result = evaluate(implementation__attempts=attempts)
    penalty = next(
        item for item in result.implementation.penalties
        if item.code == "IMPLEMENTATION_RETRIES"
    )
    assert penalty.message == expected


def test_retry_semantics_are_available_only_in_safe_evidence() -> None:
    result = calculate_workflow_evaluation(
        execution(implementation__attempts=2),
        evaluation_events(),
        include_evidence=True,
        calculated_at=NOW,
    )
    assert result.evidence
    assert result.evidence["attempt_counter_semantics"] == "total_attempts_first_is_normal"
    assert result.evidence["implementation_retry_count"] == 1


def test_positive_signals_have_no_points_and_bonuses_are_empty() -> None:
    result = evaluate()
    assert result.positive_signals
    assert result.bonuses == []
    encoded = result.model_dump(mode="json")
    assert all("points" not in signal for signal in encoded["positive_signals"])


def test_duration_separates_active_time_from_approval_wait() -> None:
    duration = duration_breakdown(
        evaluation_events(), terminal=True, now=NOW + timedelta(seconds=31)
    )
    assert duration.wall_clock_duration_seconds == 30
    assert duration.approval_wait_seconds == 7
    assert duration.active_execution_seconds == 23
    assert duration.implementation.elapsed_seconds == 17
    assert duration.implementation.active_seconds == 12
    assert duration.implementation.waiting_seconds == 5
    assert duration.testing.elapsed_seconds == 7
    assert duration.testing.active_seconds == 5
    assert duration.testing.waiting_seconds == 2
    assert duration.wall_clock_duration_seconds != sum(
        value for value in (
            duration.implementation.elapsed_seconds,
            duration.testing.elapsed_seconds,
            duration.approval_wait_seconds,
        )
        if value is not None
    )


def test_generic_finalize_stage_populates_structured_duration() -> None:
    events = [
        *evaluation_events()[:-1],
        workflow_event(14, "stage_started", 28, data={"stage": "finalize"}),
        workflow_event(15, "stage_completed", 29, data={"stage": "finalize"}),
        workflow_event(16, "workflow_completed", 30),
    ]
    duration = duration_breakdown(
        events, terminal=True, now=NOW + timedelta(seconds=31)
    )
    assert duration.finalize_seconds == 1
    assert duration.finalize.elapsed_seconds == 1
    assert duration.finalize.active_seconds == 1
    assert duration.finalize.waiting_seconds == 0


def test_warning_finding_has_existing_qa_event_and_test_file() -> None:
    events = evaluation_events()
    result = evaluate(events=events, testing__warnings=2)
    finding = next(item for item in result.findings if item.code == "TEST_WARNINGS")
    assert finding.related_task_id == "qa-task"
    assert finding.related_event_id in {str(event.event_id) for event in events}
    related_event = next(
        event for event in events if str(event.event_id) == finding.related_event_id
    )
    assert related_event.type.value == "test_run_completed"
    assert finding.related_file == "tests/test_health.py"


def test_implementation_finding_has_backend_event_and_main_file() -> None:
    events = evaluation_events()
    result = evaluate(events=events)
    finding = next(
        item for item in result.findings if item.code == "IMPLEMENTATION_VALID"
    )
    assert finding.related_task_id == "developer-task"
    assert finding.related_event_id == str(events[6].event_id)
    assert finding.related_file == "app/main.py"


def test_plan_navigation_uses_architect_when_observable() -> None:
    selected = execution()
    architect = selected.planning.tasks[0].model_copy(update={
        "task_id": "architect-task",
        "agent": "Software Architect",
    })
    selected = selected.model_copy(update={
        "planning": selected.planning.model_copy(update={
            "tasks": [architect, *selected.planning.tasks],
        }),
    })
    events = evaluation_events()
    result = evaluate(selected, events)
    finding = next(item for item in result.findings if item.code == "PLAN_VALID")
    assert finding.related_task_id == "architect-task"
    assert finding.related_event_id == str(events[2].event_id)
    assert finding.related_file is None


def test_navigation_never_uses_another_branch() -> None:
    selected = execution(branch_id="fork-1", lineage="fork")
    finding = evaluate(selected, evaluation_events("original")).findings[0]
    attached = attach_finding_navigation(
        finding=finding,
        execution=selected,
        events=evaluation_events("original"),
        branch_id="fork-1",
    )
    assert attached.related_event_id is None


def test_navigation_rejects_absolute_and_secret_paths() -> None:
    selected = execution(
        implementation__generated_files=[
            r"C:\Users\user\secret.py",
            ".env",
            "../outside.py",
        ],
        testing__failing_test_files=[r"C:\tests\test_health.py"],
        testing__warnings=1,
    )
    result = evaluate(selected, evaluation_events())
    assert all(
        finding.related_file is None
        for finding in result.findings
        if finding.code in {"IMPLEMENTATION_VALID", "TEST_WARNINGS"}
    )


def test_partial_and_final_scores_are_deterministic() -> None:
    partial_updates = {
        "terminal_status": "running",
        "testing__executed": False,
        "testing__passed": False,
    }
    events = evaluation_events()
    assert evaluate(events=events, **partial_updates) == evaluate(
        events=events, **partial_updates
    )
    assert evaluate(events=events) == evaluate(events=events)


@pytest.mark.asyncio
async def test_evaluation_store_is_idempotent_and_invalidates(tmp_path) -> None:
    store = WorkflowEvaluationStore(tmp_path / "evaluation.sqlite")
    await store.initialize()
    await store.initialize()
    result = evaluate()
    await store.put(result)
    cached = await store.get(
        result.thread_id, result.branch_id, SCORING_VERSION, result.source_updated_at
    )
    stale = await store.get(
        result.thread_id,
        result.branch_id,
        SCORING_VERSION,
        result.source_updated_at + timedelta(seconds=1),
    )
    assert cached == result
    assert stale is None


@pytest.mark.asyncio
async def test_scoring_1_0_1_1_and_1_2_materializations_coexist(tmp_path) -> None:
    store = WorkflowEvaluationStore(tmp_path / "evaluation.sqlite")
    await store.initialize()
    current = evaluate()
    await store.put(current.model_copy(update={"scoring_version": "1.0"}))
    await store.put(current.model_copy(update={"scoring_version": "1.1"}))
    await store.put(current)
    assert (
        await store.get(
            current.thread_id,
            current.branch_id,
            "1.0",
            current.source_updated_at,
        )
    ).scoring_version == "1.0"
    assert (
        await store.get(
            current.thread_id,
            current.branch_id,
            "1.1",
            current.source_updated_at,
        )
    ).scoring_version == "1.1"
    assert (
        await store.get(
            current.thread_id,
            current.branch_id,
            "1.2",
            current.source_updated_at,
        )
    ).scoring_version == "1.2"


def test_legacy_1_0_payload_remains_parseable() -> None:
    payload = evaluate().model_dump(mode="json")
    payload["scoring_version"] = "1.0"
    for field_name in (
        "earned_points",
        "available_points",
        "provisional_percentage",
        "projected_max_score",
        "is_provisional",
        "final_grade_available",
        "provisional_grade",
        "positive_signals",
    ):
        payload.pop(field_name)
    for category_name in (
        "planning", "implementation", "testing", "efficiency", "reliability"
    ):
        payload[category_name].pop("evaluation_state")
        payload[category_name].pop("available_points")
        payload[category_name].pop("positive_signals")
    parsed = WorkflowEvaluationResponse.model_validate(payload)
    assert parsed.scoring_version == "1.0"
    assert parsed.overall_score == payload["overall_score"]


class FakeExecutionService:
    def __init__(self, result: WorkflowAgentExecutionResponse) -> None:
        self.result = result
        self.calls = 0

    async def get_execution(self, *_args, **_kwargs):
        self.calls += 1
        return self.result.model_copy(update={"events": evaluation_events()})


@pytest.mark.asyncio
async def test_terminal_evaluation_survives_service_restart(tmp_path) -> None:
    store = WorkflowEvaluationStore(tmp_path / "evaluation.sqlite")
    await store.initialize()
    source = FakeExecutionService(execution())
    first_service = WorkflowEvaluationService(
        execution_service=source,  # type: ignore[arg-type]
        store=store,
    )
    first = await first_service.get_evaluation("evaluation-thread")
    second_service = WorkflowEvaluationService(
        execution_service=source,  # type: ignore[arg-type]
        store=WorkflowEvaluationStore(tmp_path / "evaluation.sqlite"),
    )
    second = await second_service.get_evaluation("evaluation-thread")
    assert second == first


@pytest.mark.asyncio
async def test_partial_implementation_survives_refresh_and_service_restart(
    tmp_path,
) -> None:
    store = WorkflowEvaluationStore(tmp_path / "evaluation.sqlite")
    await store.initialize()
    partial = execution(
        terminal_status="running",
        final_result__terminal_status="running",
        final_result__project_created=False,
        implementation__environment_prepared=False,
        implementation__dependencies_installed=False,
        testing__executed=False,
        testing__passed=False,
    )
    source = FakeExecutionService(partial)
    first_service = WorkflowEvaluationService(
        execution_service=source,  # type: ignore[arg-type]
        store=store,
    )
    first = await first_service.get_evaluation("evaluation-thread")
    refreshed = await first_service.get_evaluation("evaluation-thread")
    restarted = await WorkflowEvaluationService(
        execution_service=source,  # type: ignore[arg-type]
        store=WorkflowEvaluationStore(tmp_path / "evaluation.sqlite"),
    ).get_evaluation("evaluation-thread")
    for result in (first, refreshed, restarted):
        assert result.implementation.evaluation_state == "not_started"
        assert result.implementation.score == 0
        assert result.available_points == 50
        assert result.scoring_version == "1.2"


@pytest.mark.asyncio
async def test_service_reads_execution_once_and_hides_evidence_by_default(tmp_path) -> None:
    store = WorkflowEvaluationStore(tmp_path / "evaluation.sqlite")
    await store.initialize()
    source = FakeExecutionService(execution())
    service = WorkflowEvaluationService(
        execution_service=source,  # type: ignore[arg-type]
        store=store,
    )
    result = await service.get_evaluation("evaluation-thread")
    assert source.calls == 1
    assert result.evidence is None


@pytest.mark.asyncio
async def test_service_can_return_sanitized_evidence(tmp_path) -> None:
    store = WorkflowEvaluationStore(tmp_path / "evaluation.sqlite")
    await store.initialize()
    source = FakeExecutionService(execution())
    result = await WorkflowEvaluationService(
        execution_service=source,  # type: ignore[arg-type]
        store=store,
    ).get_evaluation("evaluation-thread", include_evidence=True)
    assert result.evidence
    assert "event_count" in result.evidence
    assert "content" not in result.evidence


def test_evaluation_endpoint_returns_typed_response() -> None:
    expected = evaluate()

    class EvaluationApi:
        async def get_evaluation(self, *_args, **_kwargs):
            return expected

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def services():
        yield SimpleNamespace(evaluation=EvaluationApi())

    with TestClient(create_app(services)) as client:
        response = client.get(
            "/api/workflows/evaluation-thread/evaluation?branch_id=original"
        )
    assert response.status_code == 200
    assert response.json()["overall_score"] == expected.overall_score


def test_evaluation_endpoint_maps_missing_thread() -> None:
    class EvaluationApi:
        async def get_evaluation(self, *_args, **_kwargs):
            raise WorkflowNotFoundError("missing")

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def services():
        yield SimpleNamespace(evaluation=EvaluationApi())

    with TestClient(create_app(services)) as client:
        response = client.get("/api/workflows/missing/evaluation")
    assert response.status_code == 404
    assert response.json()["detail"] == "Thread not found"
