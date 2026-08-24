from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.services.observability_context import ObservabilityContext, observability_context
from api.services.workflow_learning_store import WorkflowLearningStore
from api.services.workflow_knowledge import workflow_learning_summary
from graph.state import create_initial_state
from graph.workflow_learning import (
    WorkflowLearningExtractor,
    deterministic_candidate_id,
    extract_workflow_learnings_node,
    submit_workflow_learnings_node,
)
from streaming import NoOpWorkflowEventEmitter, WorkflowEventFactory, workflow_event_context
from tool_executor import ToolExecutionOutcome


def evidence(**updates):
    value = {
        "terminal_status": "completed",
        "planning_valid": True,
        "implementation_valid": True,
        "generated_files": ["app/main.py", "tests/test_api.py"],
        "environment_prepared": True,
        "tests_executed": True,
        "tests_passed": True,
        "detected_test_framework": "pytest",
        "actual_test_command": ["python", "-m", "pytest"],
        "final_test_result_summary": "2 passed",
        "repair_phase": "completed",
        "repair_attempts": 1,
        "failure_message": "Database state leaked between integration tests.",
        "repair_before": "shared transaction",
        "repair_after": "rollback per test",
        "repair_decision": "Isolate every test transaction and roll it back.",
        "files_updated_during_repair": ["tests/conftest.py"],
        "repair_knowledge_sources": [{"knowledge_id": "knowledge-prior"}],
    }
    value.update(updates)
    return value


def test_repair_success_extracts_incident_and_solution_deterministically():
    extractor = WorkflowLearningExtractor()
    first = extractor.extract(workflow_id="wf", branch_id="original", project_id="project", evidence=evidence(), created_at="2026-01-01T00:00:00Z")
    second = extractor.extract(workflow_id="wf", branch_id="original", project_id="project", evidence=evidence(), created_at="2026-01-01T00:00:01Z")
    assert {item.knowledge_type for item in first} >= {"incident", "solution", "test_pattern", "code_pattern"}
    assert [item.candidate_id for item in first] == [item.candidate_id for item in second]
    solution = next(item for item in first if item.knowledge_type == "solution")
    assert solution.confidence == .90
    assert solution.knowledge_provenance_ids == ["knowledge-prior"]
    assert "rollback per test" in solution.content


def test_failed_repair_never_creates_solution():
    candidates = WorkflowLearningExtractor().extract(
        workflow_id="wf", branch_id="original", project_id="project",
        evidence=evidence(terminal_status="tests_failed", tests_passed=False, repair_phase="rerun_tests"),
    )
    assert "incident" in {item.knowledge_type for item in candidates}
    assert "solution" not in {item.knowledge_type for item in candidates}


def test_implementation_failure_with_evidence_creates_only_incident_learning():
    candidates = WorkflowLearningExtractor().extract(
        workflow_id="wf", branch_id="original", project_id="project",
        evidence=evidence(terminal_status="implementation_failed", tests_executed=False,
                          tests_passed=False, implementation_valid=False, repair_attempts=0,
                          repair_phase="not_started", repair_before=None, repair_after=None),
    )
    types = {item.knowledge_type for item in candidates}
    assert "incident" in types
    assert "solution" not in types


def test_qa_developer_and_planner_require_validated_evidence():
    candidates = WorkflowLearningExtractor().extract(
        workflow_id="wf", branch_id="original", project_id="project",
        evidence=evidence(tests_executed=False, tests_passed=False, implementation_valid=False,
                          repair_attempts=0, repair_phase="not_started", failure_message=None,
                          repair_before=None, repair_after=None,
                          requirement_analysis={"constraints": ["Use service layer"]},
                          last_approved_tool=None),
    )
    types = {item.knowledge_type for item in candidates}
    assert "test_pattern" not in types
    assert "code_pattern" not in types
    assert "decision" not in types


def test_candidate_id_includes_branch_and_evidence():
    refs = [{"kind": "file_path", "value": "a.py"}]
    first = deterministic_candidate_id("wf", "original", "solution", " Fixed  it ", refs)
    assert first == deterministic_candidate_id("wf", "original", "solution", "fixed it", refs)
    assert first != deterministic_candidate_id("wf", "fork", "solution", "fixed it", refs)


def test_snapshot_summary_is_safe_and_counts_statuses():
    summary = workflow_learning_summary({
        "workflow_learning_state": "submitted",
        "workflow_learning_candidates": [{
            "candidate_id": "candidate-1", "knowledge_type": "solution", "confidence": .9,
            "source_reference": "workflow:wf:solution:candidate-1", "created_at": "2026-01-01T00:00:00Z",
            "content": "must not leak",
        }],
        "workflow_learning_submission_results": [{
            "candidate_id": "candidate-1", "submission_status": "duplicate", "knowledge_id": "knowledge-1",
        }],
    })
    assert summary["extracted_count"] == summary["submitted_count"] == summary["duplicate_count"] == 1
    assert "content" not in summary["candidates"][0]


class Executor:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    async def execute(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        if self.fail:
            raise RuntimeError("knowledge unavailable")
        return ToolExecutionOutcome(name, arguments, {"knowledge_id": "knowledge-1", "status": "candidate"}, {}, False, False)


@pytest.mark.asyncio
async def test_nodes_submit_only_via_mcp_and_are_idempotent(tmp_path: Path):
    state = create_initial_state("create project")
    state.update(evidence(), project_name="project")
    store = WorkflowLearningStore(tmp_path / "events.sqlite")
    await store.initialize()
    executor = Executor()
    dependencies = SimpleNamespace(tool_executor=executor, workflow_learning_store=store, observability=None)
    with observability_context(ObservabilityContext(workflow_id="wf", branch_id="original")):
        extracted = await extract_workflow_learnings_node(state, dependencies)
        state.update(extracted)
        submitted = await submit_workflow_learnings_node(state, dependencies)
        state.update(submitted)
        again = await submit_workflow_learnings_node(state, dependencies)
    assert executor.calls
    assert all(name == "knowledge__submit_learning" for name, _, _ in executor.calls)
    assert all(call[1]["metadata"]["origin"] == "workflow_learning_extractor" for call in executor.calls)
    assert len(executor.calls) == len(state["workflow_learning_candidates"])
    assert again["workflow_learning_state"] == "submitted"
    assert not any("embedding" in item for item in state)


@pytest.mark.asyncio
async def test_knowledge_failure_is_fail_open_and_retryable(tmp_path: Path):
    state = create_initial_state("create project")
    state.update(evidence(), project_name="project")
    store = WorkflowLearningStore(tmp_path / "events.sqlite")
    await store.initialize()
    dependencies = SimpleNamespace(tool_executor=Executor(fail=True), workflow_learning_store=store, observability=None)
    with observability_context(ObservabilityContext(workflow_id="wf", branch_id="original")):
        state.update(await extract_workflow_learnings_node(state, dependencies))
        result = await submit_workflow_learnings_node(state, dependencies)
    assert result["workflow_learning_state"] == "unavailable"
    assert all(item["retryable"] for item in result["workflow_learning_submission_results"])
    assert state["terminal_status"] == "completed"
    assert len(await store.pending()) == len(state["workflow_learning_candidates"])


@pytest.mark.asyncio
async def test_replay_context_does_not_extract_or_submit():
    state = create_initial_state("create project")
    state.update(evidence(), project_name="project")
    executor = Executor()
    dependencies = SimpleNamespace(tool_executor=executor, workflow_learning_store=None, observability=None)
    with workflow_event_context(thread_id="wf", emitter=NoOpWorkflowEventEmitter(), factory=WorkflowEventFactory(),
                                lineage={"lineage": "replay", "branch_id": "checkpoint-1"}):
        result = await extract_workflow_learnings_node(state, dependencies)
    assert result == {}
    assert executor.calls == []


@pytest.mark.asyncio
async def test_fork_candidate_uses_branch_provenance():
    state = create_initial_state("create project")
    state.update(evidence(), project_name="project", fork_origin_checkpoint_id="origin")
    dependencies = SimpleNamespace(tool_executor=Executor(), workflow_learning_store=None, observability=None)
    with workflow_event_context(thread_id="wf", emitter=NoOpWorkflowEventEmitter(), factory=WorkflowEventFactory(),
                                lineage={"lineage": "fork", "branch_id": "fork-1"}):
        result = await extract_workflow_learnings_node(state, dependencies)
    assert result["workflow_learning_candidates"]
    assert {item["branch_id"] for item in result["workflow_learning_candidates"]} == {"fork-1"}


@pytest.mark.asyncio
async def test_outbox_survives_restart_and_terminal_submission_is_idempotent(tmp_path: Path):
    database = tmp_path / "events.sqlite"
    candidate = WorkflowLearningExtractor().extract(
        workflow_id="wf", branch_id="original", project_id="project",
        evidence=evidence(), created_at="2026-01-01T00:00:00Z",
    )[0].to_dict()
    first = WorkflowLearningStore(database)
    await first.initialize()
    await first.save_candidates([candidate, candidate])
    assert len(await first.pending()) == 1
    await first.record_submission(candidate["candidate_id"], "candidate", {
        "candidate_id": candidate["candidate_id"], "submission_status": "candidate",
        "knowledge_id": "knowledge-1",
    })
    restarted = WorkflowLearningStore(database)
    await restarted.initialize()
    assert await restarted.pending() == []
