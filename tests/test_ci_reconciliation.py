from __future__ import annotations

from types import SimpleNamespace
from typing import Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from api.ci_models import CIRunRequest
from api.routes.ci import run_workflow_ci
from graph.checkpointing import create_sqlite_checkpointer
from graph.ci_reconciliation import build_ci_success_reconciliation
from graph.persistence_service import WorkflowPersistenceService
from graph.runtime import thread_config


COMMIT = "2bd7367063e051796f0e17b26d1ffd27c46277f7"


def accepted_run(*, commit: str = COMMIT, run_id: str = "ci-accepted") -> dict[str, Any]:
    return {
        "ci_run_id": run_id,
        "status": "passed",
        "decision": "accepted",
        "ci_validated_commit": commit,
        "gate_summary": {"passed": 3},
    }


def eligible(*, commit: str = COMMIT) -> dict[str, Any]:
    return {
        "eligible": True,
        "commit_match": True,
        "source_commit": commit,
        "target_commit": commit,
    }


def stale_ci_repair_state() -> dict[str, Any]:
    return {
        "ci_state": "failed",
        "ci_repair_state": "pending",
        "ci_repair_source_run_id": "ci-rejected",
        "ci_repair_source_commit": COMMIT,
        "pending_operation": "run_tests",
        "pending_tool_name": "testing__run_tests",
        "pending_tool_arguments": {"project_name": "health-api"},
        "pending_approval_preview": {"operation": "run_tests"},
        "pending_approval_status": "waiting",
        "supervisor_decision": "testing_repair",
        "terminal_status": None,
        "failure_stage": "ci",
        "failure_type": "ci_test_gate_failed",
        "failure_message": "Previous CI rejected the commit.",
        "tests_executed": True,
        "tests_passed": False,
        "final_test_result_summary": "1 passed, 2 warnings",
        "testing_result": {
            "tests_executed": True,
            "tests_passed": False,
            "summary": "CI rejected the commit.",
            "repair_phase": "not_started",
            "repair_attempts": 0,
        },
    }


def test_exact_commit_accepted_ci_clears_only_stale_ci_repair_state() -> None:
    plan = build_ci_success_reconciliation(
        stale_ci_repair_state(), accepted_run(), eligible(), head_commit=COMMIT,
    )

    assert plan.applicable is True
    assert plan.clears_ci_repair_interrupt is True
    assert plan.updates["ci_repair_state"] == "not_required"
    assert plan.updates["pending_operation"] is None
    assert plan.updates["pending_tool_name"] is None
    assert plan.updates["supervisor_decision"] is None
    assert plan.updates["tests_passed"] is True
    assert plan.updates["testing_result"]["tests_passed"] is True
    assert plan.updates["ci_promotion_eligible"] is True


def test_local_test_success_is_not_invented_without_durable_evidence() -> None:
    state = stale_ci_repair_state()
    state["final_test_result_summary"] = "1 failed, 1 passed"

    plan = build_ci_success_reconciliation(
        state, accepted_run(), eligible(), head_commit=COMMIT,
    )

    assert "tests_passed" not in plan.updates
    assert "testing_result" not in plan.updates


def test_ci_for_another_commit_does_not_reconcile() -> None:
    plan = build_ci_success_reconciliation(
        stale_ci_repair_state(),
        accepted_run(commit="a" * 40),
        eligible(commit="a" * 40),
        head_commit=COMMIT,
    )

    assert plan.applicable is False
    assert plan.updates == {}


def test_rejected_ci_does_not_reconcile_pending_repair() -> None:
    run = accepted_run()
    run.update(status="failed", decision="rejected")

    plan = build_ci_success_reconciliation(
        stale_ci_repair_state(), run, eligible(), head_commit=COMMIT,
    )

    assert plan.applicable is False


class ReconciliationState(TypedDict, total=False):
    ci_state: str
    ci_run_id: str | None
    ci_status: str | None
    ci_decision: str | None
    ci_validated_commit: str | None
    ci_failure_type: str | None
    ci_failure_message: str | None
    ci_gate_summary: dict[str, Any]
    ci_promotion_eligible: bool
    ci_promotion_eligibility: dict[str, Any]
    ci_repair_state: str
    ci_repair_source_run_id: str | None
    ci_repair_source_commit: str | None
    ci_repair_target_commit: str | None
    ci_repair_failure_type: str | None
    ci_repair_failure_message: str | None
    ci_repair_repairability: dict[str, Any] | None
    pending_operation: str | None
    pending_tool_name: str | None
    pending_tool_arguments: dict[str, Any] | None
    pending_approval_preview: dict[str, Any] | None
    pending_approval_status: str
    supervisor_decision: str | None
    supervisor_reason: str | None
    supervisor_confidence: float | None
    supervisor_decision_source: str | None
    allowed_handoffs: list[str]
    terminal_status: str | None
    failure_stage: str | None
    failure_type: str | None
    failure_message: str | None
    test_failure_summary: str | None
    tests_executed: bool
    tests_passed: bool
    final_test_result_summary: str | None
    testing_result: dict[str, Any]
    last_completed_stage: str | None
    last_completed_node: str | None


def build_interrupted_repair_graph(checkpointer: Any, calls: list[str]):
    async def approval(state: ReconciliationState) -> dict[str, Any]:
        interrupt({"operation": "run_tests", "public_tool_name": "testing__run_tests"})
        return {}

    child = StateGraph(ReconciliationState)
    child.add_node("approval", approval)
    child.add_edge(START, "approval")
    child.add_edge("approval", END)
    child_graph = child.compile()

    async def testing_repair(state: ReconciliationState) -> dict[str, Any]:
        return await child_graph.ainvoke(state)

    async def supervisor(state: ReconciliationState) -> dict[str, Any]:
        calls.append("supervisor")
        return {"terminal_status": "completed"}

    parent = StateGraph(ReconciliationState)
    parent.add_node("testing_repair", testing_repair)
    parent.add_node("supervisor", supervisor)
    parent.add_edge(START, "testing_repair")
    parent.add_edge("testing_repair", "supervisor")
    parent.add_edge("supervisor", END)
    return parent.compile(checkpointer=checkpointer)


@pytest.mark.asyncio
async def test_reconciliation_consumes_stale_interrupt_without_running_tests_or_promotion(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    calls: list[str] = []
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_interrupted_repair_graph(checkpointer, calls)
        await graph.ainvoke(stale_ci_repair_state(), thread_config("workflow-1"))
        service = WorkflowPersistenceService(graph)

        result = await service.reconcile_successful_ci_run(
            "workflow-1", run=accepted_run(), eligibility=eligible(), head_commit=COMMIT,
        )
        snapshot = await service.get_snapshot("workflow-1")

        assert result is not None
        assert result.final_state["terminal_status"] == "completed"
        assert snapshot.interrupts == ()
        assert snapshot.next_nodes == ()
        assert snapshot.values["pending_operation"] is None
        assert snapshot.values["tests_passed"] is True
        assert snapshot.values["ci_promotion_eligible"] is True
        assert calls == ["supervisor"]

        unchanged = await service.reconcile_successful_ci_run(
            "workflow-1",
            run=accepted_run(run_id="ci-accepted-again"),
            eligibility=eligible(),
            head_commit=COMMIT,
        )
        assert unchanged is None
        assert calls == ["supervisor"]

    async with create_sqlite_checkpointer() as restarted_checkpointer:
        restarted_graph = build_interrupted_repair_graph(restarted_checkpointer, calls)
        restarted = WorkflowPersistenceService(restarted_graph)
        snapshot = await restarted.get_snapshot("workflow-1")
        assert snapshot.values["pending_operation"] is None
        assert snapshot.values["tests_passed"] is True
        assert snapshot.values["ci_validated_commit"] == COMMIT


@pytest.mark.asyncio
async def test_unrelated_human_interrupt_is_preserved(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "unrelated.sqlite"))

    async def approval(state: ReconciliationState) -> dict[str, Any]:
        interrupt({"operation": "git_merge", "public_tool_name": "git__merge_workflow_branch"})
        return {}

    graph_builder = StateGraph(ReconciliationState)
    graph_builder.add_node("approval", approval)
    graph_builder.add_edge(START, "approval")
    graph_builder.add_edge("approval", END)
    state = stale_ci_repair_state()
    state.update(
        pending_operation="git_merge",
        pending_tool_name="git__merge_workflow_branch",
    )
    async with create_sqlite_checkpointer() as checkpointer:
        graph = graph_builder.compile(checkpointer=checkpointer)
        await graph.ainvoke(state, thread_config("workflow-2"))
        service = WorkflowPersistenceService(graph)

        result = await service.reconcile_successful_ci_run(
            "workflow-2", run=accepted_run(), eligibility=eligible(), head_commit=COMMIT,
        )
        snapshot = await service.get_snapshot("workflow-2")

        assert result is None
        assert snapshot.interrupts
        assert snapshot.values["pending_operation"] == "git_merge"


@pytest.mark.asyncio
async def test_restart_continues_checkpoint_already_waiting_at_supervisor(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "resume-supervisor.sqlite"))
    calls: list[str] = []
    async with create_sqlite_checkpointer() as checkpointer:
        graph = build_interrupted_repair_graph(checkpointer, calls)
        await graph.ainvoke(stale_ci_repair_state(), thread_config("workflow-restart"))
        plan = build_ci_success_reconciliation(
            stale_ci_repair_state(), accepted_run(), eligible(), head_commit=COMMIT,
        )
        await graph.aupdate_state(
            thread_config("workflow-restart"),
            plan.updates,
            as_node="testing_repair",
        )

    async with create_sqlite_checkpointer() as restarted_checkpointer:
        restarted_graph = build_interrupted_repair_graph(restarted_checkpointer, calls)
        service = WorkflowPersistenceService(restarted_graph)
        waiting = await service.get_snapshot("workflow-restart")
        assert waiting.next_nodes == ("supervisor",)

        result = await service.reconcile_successful_ci_run(
            "workflow-restart",
            run=accepted_run(run_id="ci-after-restart"),
            eligibility=eligible(),
            head_commit=COMMIT,
        )

        assert result is not None
        assert result.final_state["terminal_status"] == "completed"
        assert calls == ["supervisor"]


@pytest.mark.asyncio
async def test_manual_ci_route_reconciles_the_current_head() -> None:
    class Query:
        async def get_snapshot(self, thread_id: str):
            return SimpleNamespace(project_name="health-api")

    class Git:
        async def workflow_summary(self, project_id: str, thread_id: str):
            return SimpleNamespace(
                developer_commit={"commit": COMMIT},
                repair_commit=None,
                head_commit=COMMIT,
                workflow_branch="workflow/1",
                branch="workflow/1",
            )

    class Model:
        def __init__(self, value: dict[str, Any]) -> None:
            self.value = value

        def model_dump(self, **kwargs):
            return dict(self.value)

    class CI:
        async def run(self, *args, **kwargs):
            return Model(accepted_run())

        async def promotion_eligibility(self, workflow_id: str, commit: str):
            return Model(eligible())

    class Persistence:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def reconcile_successful_ci_run(self, thread_id: str, **kwargs):
            self.calls.append({"thread_id": thread_id, **kwargs})
            return SimpleNamespace(interrupted=False, final_state={"terminal_status": "completed"})

    class Runner:
        def __init__(self) -> None:
            self.results: list[Any] = []

        async def record_recovery_result(self, thread_id: str, result: Any) -> None:
            self.results.append((thread_id, result))

    persistence = Persistence()
    runner = Runner()
    services = SimpleNamespace(
        query=Query(), git=Git(), ci=CI(), persistence=persistence, runner=runner,
    )

    await run_workflow_ci("workflow-1", CIRunRequest(pipeline_fingerprint="fingerprint"), services)

    assert len(persistence.calls) == 1
    assert persistence.calls[0]["head_commit"] == COMMIT
    assert persistence.calls[0]["run"]["ci_run_id"] == "ci-accepted"
    assert len(runner.results) == 1
