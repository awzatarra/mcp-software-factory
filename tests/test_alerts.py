from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.alert_models import AlertEvidence
from api.services.alert_service import (
    AlertEvaluationService, AlertTransitionError, PeriodicAlertEvaluator,
    build_testing_alert_navigation, find_current_approval_wait_context,
)
from api.services.alert_store import AlertStore, BUILTIN_RULES
from api.services.dashboard_store import DashboardMetricStore
from api.dependencies import get_services
from api.routes.alerts import router as alerts_router
from streaming import EventStatus, WorkflowEvent, WorkflowEventType
from streaming.sqlite_store import SQLiteWorkflowEventStore


def metric(thread_id: str = "alert-thread", **updates):
    now = datetime.now(UTC)
    base = {
        "thread_id": thread_id, "branch_id": "original", "lineage": "original",
        "project_name": "alert-api", "workflow_intent": "create_project",
        "framework": "FastAPI", "test_framework": "pytest", "terminal_status": "pending",
        "status_group": "waiting", "interrupted": 1, "data_complete": 1,
        "tests_executed": 0, "tests_passed": 0, "test_warnings": 0,
        "repair_phase": "not_started", "repair_attempts": 0,
        "overall_score": None, "grade": None, "evaluation_status": None, "scoring_version": "1.2",
        "total_seconds": 100, "active_seconds": 80, "approval_wait_seconds": 0,
        "longest_approval_wait_seconds": 0, "planning_seconds": 10,
        "implementation_seconds": 40, "testing_seconds": 0, "repair_seconds": 0,
        "approvals_requested": 1, "approvals_granted": 0, "approvals_rejected": 0,
        "approval_operations": {"create_project": 1}, "evaluations": {},
        "findings": [], "recommendations": [],
        "agents": [{"agent": "QA", "status": "waiting", "attempt": 1, "duration_seconds": 0, "related_files": 1}],
        "related_files": ["tests/test_health.py"], "pending_operation": "create_project",
        "created_at": (now - timedelta(hours=1)).isoformat(),
        "source_updated_at": now.isoformat(), "calculated_at": now.isoformat(),
    }
    base.update(updates)
    return base


def workflow_event(sequence: int, event_type: WorkflowEventType, *, age_seconds: int = 0, data=None):
    return WorkflowEvent(
        event_id=uuid4(), thread_id="alert-thread", sequence=sequence,
        type=event_type, timestamp=datetime.now(UTC) - timedelta(seconds=age_seconds),
        source="test", stage="testing", status=EventStatus.COMPLETED,
        message=None, data={"branch_id": "original", **(data or {})},
    )


def qa_execution(branch_id: str = "original"):
    task = SimpleNamespace(
        task_id=f"qa-task-{branch_id}",
        agent="QA Reviewer",
        related_files=["README.md", "tests/test_health.py", "requirements.txt"],
    )
    return SimpleNamespace(
        branch_id=branch_id,
        planning=SimpleNamespace(tasks=[task]),
        testing=SimpleNamespace(
            failing_test_files=[], files_read_during_repair=[],
            files_updated_during_repair=[],
        ),
        implementation=SimpleNamespace(
            generated_files=["README.md", "requirements.txt", "tests/test_health.py"],
        ),
    )


class StubExecutionService:
    def __init__(self, execution):
        self.execution = execution
        self.calls = 0

    async def get_execution(self, thread_id, *, branch_id):
        self.calls += 1
        return self.execution


@pytest.fixture
async def alert_system(tmp_path):
    path = tmp_path / "alerts.db"
    events = SQLiteWorkflowEventStore(path); await events.initialize()
    metrics = DashboardMetricStore(path); await metrics.initialize()
    store = AlertStore(path); await store.initialize()
    await metrics.upsert(metric())
    await events.append(workflow_event(1, WorkflowEventType.APPROVAL_REQUIRED, age_seconds=1900, data={"operation": "create_project"}))
    yield store, AlertEvaluationService(store), metrics, events
    await events.close()


async def test_alert_migration_and_builtin_rules_are_idempotent(alert_system):
    store, _, _, _ = alert_system
    await store.initialize()
    rules, total = await store.list_rules(limit=100)
    assert total == len(BUILTIN_RULES)
    assert len({rule.rule_id for rule in rules}) == len(BUILTIN_RULES)
    with sqlite3.connect(store.database_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"alert_rules", "workflow_alerts", "alert_occurrences", "alert_actions", "alert_evaluator_locks"} <= tables
    assert {"idx_alert_status", "idx_alert_severity", "idx_occurrence_alert", "idx_action_alert"} <= indexes


async def test_rule_update_disable_and_reset(alert_system):
    store, _, _, _ = alert_system
    rule_id = "builtin:approval_wait_too_long"
    updated = await store.update_rule(rule_id, {"enabled": False, "threshold": {"warning": 60}, "cooldown_seconds": 10})
    assert updated and not updated.enabled and updated.threshold.warning == 60 and updated.cooldown_seconds == 10
    reset = await store.reset_rule(rule_id)
    assert reset and reset.enabled and reset.threshold.warning == 1800 and reset.cooldown_seconds == 900


async def test_approval_wait_creates_deduplicated_alert_and_cooldown(alert_system):
    _, service, _, _ = alert_system
    first = await service.evaluate_workflow(thread_id="alert-thread")
    second = await service.evaluate_workflow(thread_id="alert-thread")
    alerts = await service.list_alerts(statuses=["open"], limit=50, offset=0)
    approval = next(item for item in alerts.items if item.rule_code == "APPROVAL_WAIT_TOO_LONG")
    assert first.created_alerts >= 1
    assert second.created_alerts == 0
    assert approval.severity == "warning"
    assert approval.occurrence_count == 1


@pytest.mark.parametrize(("age", "expected"), [(1900, "warning"), (3700, "error"), (14500, "critical")])
async def test_approval_wait_severity_escalation(tmp_path, age, expected):
    path = tmp_path / f"alert-{age}.db"; events = SQLiteWorkflowEventStore(path); await events.initialize()
    metrics = DashboardMetricStore(path); await metrics.initialize(); await metrics.upsert(metric())
    store = AlertStore(path); await store.initialize(); service = AlertEvaluationService(store)
    await events.append(workflow_event(1, WorkflowEventType.APPROVAL_REQUIRED, age_seconds=age, data={"operation": "create_project"}))
    await service.evaluate_workflow(thread_id="alert-thread")
    item = next(item for item in (await service.list_alerts(statuses=["open"], limit=50, offset=0)).items if item.rule_code == "APPROVAL_WAIT_TOO_LONG")
    assert item.severity == expected
    await events.close()


@pytest.mark.parametrize(("updates", "rule_code", "severity"), [
    ({"status_group": "failed", "terminal_status": "tests_failed"}, "WORKFLOW_FAILED", "error"),
    ({"tests_executed": 1, "tests_passed": 0}, "TESTS_FAILED", "error"),
    ({"tests_executed": 1, "tests_passed": 0, "repair_attempts": 1}, "REPAIR_FAILED", "critical"),
    ({"evaluation_status": "final", "overall_score": 50}, "LOW_EVALUATION_SCORE", "error"),
    ({"test_warnings": 5}, "HIGH_WARNING_COUNT", "warning"),
    ({"repair_attempts": 3}, "EXCESSIVE_RETRIES", "warning"),
])
async def test_metric_rules_create_expected_alert(alert_system, updates, rule_code, severity):
    _, service, metrics, _ = alert_system
    await metrics.upsert(metric(**updates))
    await service.evaluate_workflow(thread_id="alert-thread")
    item = next(item for item in (await service.list_alerts(statuses=["open", "acknowledged"], limit=100, offset=0)).items if item.rule_code == rule_code)
    assert item.severity == severity


async def test_passing_tests_auto_resolve_failed_test_alert(alert_system):
    _, service, metrics, _ = alert_system
    await metrics.upsert(metric(tests_executed=1, tests_passed=0))
    await service.evaluate_workflow(thread_id="alert-thread")
    await metrics.upsert(metric(tests_executed=1, tests_passed=1, status_group="completed", terminal_status="completed", pending_operation=None))
    result = await service.evaluate_workflow(thread_id="alert-thread")
    resolved = await service.list_alerts(statuses=["resolved"], rule_code="TESTS_FAILED", limit=10, offset=0)
    assert result.resolved_alerts >= 1
    assert resolved.items[0].resolved_by == "system"


async def test_acknowledge_resolve_reopen_mute_and_audit(alert_system):
    _, service, _, _ = alert_system
    await service.evaluate_workflow(thread_id="alert-thread")
    alert = (await service.list_alerts(statuses=["open"], rule_code="APPROVAL_WAIT_TOO_LONG", limit=10, offset=0)).items[0]
    acknowledged = await service.transition(alert.alert_id, "acknowledge", "local-user", "Investigating")
    assert acknowledged.status == "acknowledged" and acknowledged.acknowledged_by == "local-user"
    assert (await service.transition(alert.alert_id, "acknowledge", "local-user")).status == "acknowledged"
    muted = await service.transition(alert.alert_id, "mute", "local-user", duration_seconds=60)
    assert muted.status == "muted" and muted.muted_until
    unmuted = await service.transition(alert.alert_id, "unmute", "local-user")
    assert unmuted.status == "acknowledged"
    resolved = await service.transition(alert.alert_id, "resolve", "local-user", "Fixed")
    assert resolved.status == "resolved" and resolved.resolution_note == "Fixed"
    with pytest.raises(AlertTransitionError): await service.transition(alert.alert_id, "acknowledge", "local-user")
    reopened = await service.transition(alert.alert_id, "reopen", "local-user")
    assert reopened.status == "open"
    detail = await service.detail(alert.alert_id)
    assert {action.action for action in detail.actions} >= {"acknowledge", "mute", "unmute", "resolve", "reopen"}
    assert all(action.actor in {"local-user", "system"} for action in detail.actions)


async def test_filters_summary_navigation_and_branch_isolation(alert_system):
    _, service, _, _ = alert_system
    await service.evaluate_workflow(thread_id="alert-thread")
    listed = await service.list_alerts(statuses=["open"], severity="warning", project_name="alert", limit=1, offset=0)
    assert listed.total >= 1 and listed.items[0].branch_id == "original"
    summary = await service.summary(branch_scope="original")
    assert summary.open_total >= 1 and summary.affected_workflows == 1 and summary.top_rules
    detail = await service.detail(listed.items[0].alert_id)
    assert detail.navigation.workflow == "/workflows/alert-thread"
    assert detail.navigation.timeline and "event=" in detail.navigation.timeline


async def test_stable_fingerprint_and_sanitized_evidence(alert_system):
    store, _, _, _ = alert_system
    assert store.fingerprint("rule", "thread", "original", "x") == store.fingerprint("rule", "thread", "original", "x")
    rule = await store.get_rule("builtin:high_warning_count")
    evidence = AlertEvidence(metric="warnings", actual_value="token=secret-value", threshold_value=5, related_file="tests/test.py")
    await store.detect(rule=rule, thread_id="safe", branch_id="original", project_name="C:\\Users\\secret\\project", severity="warning", title="token=secret-value", message="C:\\Users\\secret\\file.py", evidence=evidence, dimension="safe", source_event_id=None)
    alert = (await store.list_alerts(statuses=["open"], thread_id="safe", limit=10, offset=0))[0][0]
    assert "secret-value" not in alert.title
    assert "C:\\Users" not in alert.message
    assert "C:\\Users" not in (alert.project_name or "")


async def test_periodic_evaluator_lock_prevents_two_workers(alert_system):
    _, service, _, _ = alert_system
    first = PeriodicAlertEvaluator(service, interval_seconds=60)
    second = PeriodicAlertEvaluator(service, interval_seconds=60)
    assert await service.store.acquire_lock(first.owner_id, 60)
    assert not await second.run_once()
    await service.store.release_lock(first.owner_id)
    assert await second.run_once()


async def test_backfill_is_idempotent(alert_system):
    _, service, _, _ = alert_system
    first = await service.backfill()
    second = await service.backfill()
    assert first["processed"] == second["processed"] == 1
    assert first["created"] >= 1
    assert second["created"] == 0 and second["updated"] == 0 and second["failed"] == 0


async def test_concurrent_evaluation_keeps_one_alert(alert_system):
    _, service, _, _ = alert_system
    await asyncio.gather(*(service.evaluate_workflow(thread_id="alert-thread") for _ in range(4)))
    result = await service.list_alerts(statuses=["open"], rule_code="APPROVAL_WAIT_TOO_LONG", limit=20, offset=0)
    assert result.total == 1


async def test_event_driven_supervisor_gap_and_stale_rules(alert_system):
    _, service, metrics, events = alert_system
    old = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    await metrics.upsert(metric(source_updated_at=old, calculated_at=old))
    await events.append(workflow_event(3, WorkflowEventType.SUPERVISOR_DECISION_COMPLETED, data={"decision_source": "fallback"}))
    await events.append(workflow_event(4, WorkflowEventType.SUPERVISOR_LOOP_DETECTED))
    await service.evaluate_workflow(thread_id="alert-thread")
    codes = {item.rule_code: item.severity for item in (await service.list_alerts(statuses=["open"], limit=100, offset=0)).items}
    assert codes["SUPERVISOR_FALLBACK"] == "warning"
    assert codes["SUPERVISOR_LOOP_DETECTED"] == "critical"
    assert codes["EVENT_SEQUENCE_GAP"] == "error"
    assert codes["DASHBOARD_METRIC_STALE"] == "warning"


async def test_automatic_reopen_uses_same_alert(alert_system):
    _, service, _, _ = alert_system
    await service.evaluate_workflow(thread_id="alert-thread")
    alert = (await service.list_alerts(statuses=["open"], rule_code="APPROVAL_WAIT_TOO_LONG", limit=10, offset=0)).items[0]
    await service.transition(alert.alert_id, "resolve", "local-user")
    result = await service.evaluate_workflow(thread_id="alert-thread")
    reopened = await service.store.get_alert(alert.alert_id)
    assert result.reopened_alerts >= 1
    assert reopened.status == "open" and reopened.alert_id == alert.alert_id
    assert "reopened" in {item.action for item in (await service.detail(alert.alert_id)).actions}


async def test_expired_mute_restores_active_status(alert_system):
    _, service, _, _ = alert_system
    await service.evaluate_workflow(thread_id="alert-thread")
    alert = (await service.list_alerts(statuses=["open"], rule_code="APPROVAL_WAIT_TOO_LONG", limit=10, offset=0)).items[0]
    await service.transition(alert.alert_id, "mute", "local-user", duration_seconds=-1)
    restored = await service.store.get_alert(alert.alert_id)
    assert restored.status == "open" and restored.muted_until is None


async def test_fork_alert_is_isolated_by_branch(alert_system):
    _, service, metrics, _ = alert_system
    await metrics.upsert(metric(branch_id="fork-1", lineage="fork", status_group="failed", terminal_status="tests_failed", pending_operation=None))
    await service.evaluate_workflow(thread_id="alert-thread", branch_id="fork-1")
    fork = await service.list_alerts(statuses=["open"], branch_id="fork-1", rule_code="WORKFLOW_FAILED", limit=10, offset=0)
    original = await service.list_alerts(statuses=["open"], branch_id="original", rule_code="WORKFLOW_FAILED", limit=10, offset=0)
    assert fork.total == 1 and original.total == 0
    assert fork.items[0].fingerprint != service.store.fingerprint(fork.items[0].rule_id, "alert-thread", "original", "workflow_failed")


async def test_duration_anomaly_uses_historical_p95(alert_system):
    _, service, metrics, _ = alert_system
    for index in range(25):
        await metrics.upsert(metric(f"baseline-{index}", status_group="completed", terminal_status="completed", pending_operation=None, total_seconds=100 + index))
    await metrics.upsert(metric(total_seconds=7200))
    await service.evaluate_workflow(thread_id="alert-thread")
    result = await service.list_alerts(statuses=["open"], rule_code="WORKFLOW_DURATION_ANOMALY", limit=10, offset=0)
    assert result.total == 1


async def test_alert_api_contract_and_validation(alert_system):
    _, service, _, _ = alert_system
    await service.evaluate_workflow(thread_id="alert-thread")
    app = FastAPI(); app.include_router(alerts_router)
    app.dependency_overrides[get_services] = lambda: type("Services", (), {"alerts": service})()
    with TestClient(app) as client:
        rules = client.get("/api/alerts/rules"); assert rules.status_code == 200 and rules.json()["total"] == len(BUILTIN_RULES)
        listing = client.get("/api/alerts", params={"status": "open", "severity": "warning"}); assert listing.status_code == 200
        alert_id = listing.json()["items"][0]["alert_id"]
        assert client.get(f"/api/alerts/{alert_id}").status_code == 200
        assert client.post(f"/api/alerts/{alert_id}/acknowledge", json={"actor": "local-user", "note": "Reviewing"}).json()["status"] == "acknowledged"
        assert client.get("/api/alerts", params={"status": "invalid"}).status_code == 422
        assert client.get("/api/alerts", params={"severity": "invalid"}).status_code == 422
        assert client.get("/api/alerts", params={"sort_by": "invalid"}).status_code == 422
        assert client.get("/api/alerts/missing").status_code == 404
        assert client.post("/api/alerts/evaluate", json={"thread_id": "missing"}).status_code == 404
        assert client.post(
            "/api/alerts/evaluate",
            content='{"thread_id":',
            headers={"content-type": "application/json"},
        ).status_code == 422


async def test_approval_wait_is_reconciled_by_operation_fingerprint(alert_system):
    store, service, metrics, events = alert_system
    first = await service.evaluate_workflow(thread_id="alert-thread")
    create_alert = (await service.list_alerts(
        statuses=["open"], rule_code="APPROVAL_WAIT_TOO_LONG", limit=10, offset=0,
    )).items[0]
    assert first.created_alerts >= 1
    assert create_alert.current_evidence.pending_operation == "create_project"
    assert create_alert.related_file is None
    await service.transition(create_alert.alert_id, "acknowledge", "operator")

    await events.append(workflow_event(
        2, WorkflowEventType.APPROVAL_REQUIRED, age_seconds=30,
        data={"operation": "prepare_environment"},
    ))
    await metrics.upsert(metric(
        pending_operation="prepare_environment",
        approval_operations={"create_project": 1, "prepare_environment": 1},
    ))
    changed = await service.evaluate_workflow(thread_id="alert-thread")
    resolved = await store.get_alert(create_alert.alert_id)
    assert changed.resolved_alerts >= 1
    assert resolved.status == "resolved"
    assert resolved.fingerprint == create_alert.fingerprint
    assert resolved.occurrence_count == create_alert.occurrence_count
    assert resolved.resolved_by == "system"
    detail = await service.detail(create_alert.alert_id)
    action = next(item for item in detail.actions if item.action == "auto_resolve")
    assert action.actor == "system"
    assert action.previous_status == "acknowledged"
    assert action.note == "The approval operation is no longer pending."
    assert (await service.list_alerts(
        statuses=["open", "acknowledged"], rule_code="APPROVAL_WAIT_TOO_LONG",
        limit=10, offset=0,
    )).total == 0

    prepare_event_id = (await store.workflow_context("alert-thread", "original"))["events"][-1]["event_id"]
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE workflow_events SET timestamp=? WHERE event_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1900)).isoformat(), prepare_event_id),
        )
    created = await service.evaluate_workflow(thread_id="alert-thread")
    prepare_alert = (await service.list_alerts(
        statuses=["open"], rule_code="APPROVAL_WAIT_TOO_LONG", limit=10, offset=0,
    )).items[0]
    assert created.created_alerts >= 1
    assert prepare_alert.alert_id != create_alert.alert_id
    assert prepare_alert.fingerprint != create_alert.fingerprint
    assert prepare_alert.current_evidence.pending_operation == "prepare_environment"
    assert prepare_alert.primary_event_id == prepare_alert.current_evidence.related_event_id
    assert prepare_alert.related_file is None


async def test_current_approval_context_uses_operation_clock_and_branch(alert_system):
    store, _, metrics, events = alert_system
    await events.append(workflow_event(
        2, WorkflowEventType.APPROVAL_REQUIRED, age_seconds=20,
        data={"operation": "prepare_environment"},
    ))
    await metrics.upsert(metric(pending_operation="prepare_environment"))
    context = await store.workflow_context("alert-thread", "original")
    wait = find_current_approval_wait_context(
        events=context["events"], branch_id="original",
        pending_operation="prepare_environment",
    )
    assert wait is not None
    assert 15 <= wait.wait_seconds < 60
    assert wait.approval_required_event_id == str(context["events"][-1]["event_id"])
    assert find_current_approval_wait_context(
        events=context["events"], branch_id="fork-1",
        pending_operation="prepare_environment",
    ) is None


async def test_testing_rules_share_durable_qa_navigation_without_n_plus_one(alert_system):
    _, _, metrics, events = alert_system
    execution_service = StubExecutionService(qa_execution())
    store = alert_system[0]
    service = AlertEvaluationService(store, execution_service)
    await store.update_rule("builtin:high_warning_count", {"threshold": {"warning": 2}})
    await metrics.upsert(metric(
        tests_executed=1, tests_passed=0, test_warnings=2,
        repair_attempts=1, pending_operation=None,
        related_files=["README.md", "requirements.txt"],
    ))
    completed = workflow_event(
        2, WorkflowEventType.TEST_RUN_COMPLETED,
        data={"related_file": "README.md", "task_id": "wrong-task"},
    )
    await events.append(completed)
    await service.evaluate_workflow(thread_id="alert-thread")
    assert execution_service.calls == 1
    alerts = await service.list_alerts(
        statuses=["open"], limit=100, offset=0,
    )
    by_code = {item.rule_code: item for item in alerts.items}
    for code in ("HIGH_WARNING_COUNT", "TESTS_FAILED", "REPAIR_FAILED"):
        item = by_code[code]
        assert item.related_task_id == "qa-task-original"
        assert item.primary_event_id == str(completed.event_id)
        assert item.current_evidence.related_event_id == str(completed.event_id)
        assert item.related_file == "tests/test_health.py"
        assert item.related_file not in {"README.md", "requirements.txt"}


def test_testing_navigation_rejects_cross_branch_evidence():
    events = [{
        "event_id": "original-event", "event_type": "test_run_completed",
        "sequence": 1, "timestamp": datetime.now(UTC).isoformat(),
        "data": {"branch_id": "original"},
    }, {
        "event_id": "fork-event", "event_type": "test_run_completed",
        "sequence": 2, "timestamp": datetime.now(UTC).isoformat(),
        "data": {"branch_id": "fork-1"},
    }]
    fork = build_testing_alert_navigation(
        execution=qa_execution("fork-1"), events=events, branch_id="fork-1",
    )
    assert fork.related_task_id == "qa-task-fork-1"
    assert fork.related_event_id == "fork-event"
    assert build_testing_alert_navigation(
        execution=qa_execution("original"), events=events, branch_id="fork-1",
    ) == type(fork)()


async def test_navigation_reconciliation_preserves_occurrences_and_first_detection(alert_system):
    store, _, metrics, events = alert_system
    await store.update_rule("builtin:high_warning_count", {"threshold": {"warning": 2}})
    await metrics.upsert(metric(test_warnings=2, pending_operation=None, related_files=["README.md"]))
    basic = AlertEvaluationService(store)
    await basic.evaluate_workflow(thread_id="alert-thread")
    before = (await basic.list_alerts(
        statuses=["open"], rule_code="HIGH_WARNING_COUNT", limit=10, offset=0,
    )).items[0]
    completed = workflow_event(2, WorkflowEventType.TEST_RUN_COMPLETED)
    await events.append(completed)
    corrected = AlertEvaluationService(store, StubExecutionService(qa_execution()))
    first = await corrected.evaluate_workflow(thread_id="alert-thread")
    after = await store.get_alert(before.alert_id)
    second = await corrected.evaluate_workflow(thread_id="alert-thread")
    assert first.updated_alerts >= 1
    assert after.related_task_id == "qa-task-original"
    assert after.related_file == "tests/test_health.py"
    assert after.occurrence_count == before.occurrence_count
    assert after.first_detected_at == before.first_detected_at
    assert second.updated_alerts == 0
