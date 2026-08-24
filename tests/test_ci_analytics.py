from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.ci_analytics_service import CIAnalyticsService
from api.services.ci_store import CIPipelineStore


def pipeline(framework: str = "fastapi") -> dict:
    return {
        "pipeline_id": f"{framework}-ci",
        "name": f"{framework} CI",
        "version": "6.21.1-v1",
        "framework": framework,
        "steps": [{"step_id": "test", "name": "Tests", "type": "test", "command": ["python", "-m", "pytest"]}],
        "fail_fast": True,
        "timeout_seconds": 600,
    }


def step(kind: str, status: str = "passed", duration: float = 1.0, failure_type: str | None = None) -> dict:
    return {
        "step_id": kind,
        "name": kind,
        "type": kind,
        "status": status,
        "duration_seconds": duration,
        "failure_type": failure_type,
        "failure_message": "safe summary",
    }


def gate(kind: str, status: str = "passed", failure_type: str | None = None, blocking: bool = True) -> dict:
    return {
        "gate_id": f"{kind}-gate",
        "type": kind,
        "status": status,
        "required": True,
        "blocking": blocking,
        "reason": "evaluated",
        "source_steps": [kind],
        "failure_type": failure_type,
    }


def run(
    index: int,
    *,
    workflow_id: str = "workflow-1",
    framework: str = "fastapi",
    status: str = "passed",
    decision: str | None = "accepted",
    duration: float | None = 10,
    steps: list[dict] | None = None,
    gates: list[dict] | None = None,
    failure_type: str | None = None,
    repairability: dict | None = None,
    source_commit: str | None = None,
    created_at: datetime | None = None,
) -> dict:
    stamp = (created_at or datetime(2026, 8, 10, tzinfo=UTC) + timedelta(seconds=index)).isoformat()
    return {
        "ci_run_id": f"ci-{index:03d}",
        "workflow_id": workflow_id,
        "project_id": f"project-{framework}",
        "pipeline_fingerprint": f"fp-{index}",
        "pipeline": pipeline(framework),
        "source": {"source_mode": "commit" if source_commit else "working_tree", "source_commit": source_commit},
        "status": status,
        "started_at": stamp,
        "completed_at": stamp if status in {"passed", "failed"} else None,
        "duration_seconds": duration,
        "steps": steps if steps is not None else [step("test", "passed", duration or 0)],
        "failed_step": None,
        "failure_type": failure_type,
        "failure_message": "safe summary with no secrets",
        "warnings": [],
        "gate_policy_version": "6.21.2-v1",
        "gates": gates if gates is not None else [gate("test", "passed")],
        "decision": decision,
        "failed_gates": [item["gate_id"] for item in gates or [] if item.get("status") == "failed"],
        "warning_gates": [item["gate_id"] for item in gates or [] if item.get("status") == "warning"],
        "blocking_gate": next((item["gate_id"] for item in gates or [] if item.get("status") == "failed"), None),
        "gate_summary": {},
        "ci_validated_commit": source_commit if decision in {"accepted", "accepted_with_warnings"} else None,
        "repairability": repairability,
    }


async def populated_store(tmp_path: Path, runs: list[dict]) -> CIPipelineStore:
    store = CIPipelineStore(tmp_path / "ci.sqlite")
    await store.initialize()
    for item in runs:
        await store.create_run(item)
    return store


@pytest.mark.asyncio
async def test_empty_dataset_metrics_are_zero(tmp_path: Path) -> None:
    service = CIAnalyticsService(await populated_store(tmp_path, []))
    metrics = await service.metrics()
    assert metrics.summary.total_runs == 0
    assert metrics.summary.acceptance_rate is None
    assert metrics.steps["test"].run_count == 0


@pytest.mark.asyncio
async def test_basic_rates_and_durations_are_deterministic(tmp_path: Path) -> None:
    runs = [run(i, decision="accepted", duration=i) for i in range(1, 9)]
    runs.append(run(9, decision="accepted_with_warnings", duration=9, gates=[gate("lint", "warning", blocking=False)]))
    runs.append(run(10, status="failed", decision="rejected", duration=10, gates=[gate("test", "failed", "ci_test_gate_failed")], failure_type="ci_tests_failed"))
    service = CIAnalyticsService(await populated_store(tmp_path, runs))
    metrics = await service.metrics()
    assert metrics.summary.total_runs == 10
    assert metrics.summary.acceptance_rate == pytest.approx(0.8)
    assert metrics.summary.warning_rate == pytest.approx(0.1)
    assert metrics.summary.rejection_rate == pytest.approx(0.1)
    assert metrics.summary.average_pipeline_duration_seconds == pytest.approx(5.5)
    assert metrics.summary.p50_pipeline_duration_seconds == 5
    assert metrics.summary.p95_pipeline_duration_seconds == 10
    assert metrics.gates["test"].failure_rate == pytest.approx(1 / 9)
    assert metrics.gates["lint"].warning_rate == pytest.approx(1)


@pytest.mark.asyncio
async def test_failure_classification_separates_code_infrastructure_and_configuration(tmp_path: Path) -> None:
    repairable = {"repairable": True, "category": "repairable_tests", "confidence": .8, "reason_codes": []}
    infra = {"repairable": False, "category": "infrastructure", "confidence": .9, "reason_codes": []}
    config = {"repairable": False, "category": "configuration", "confidence": .9, "reason_codes": []}
    runs = [
        run(1, status="failed", decision="rejected", failure_type="ci_tests_failed", repairability=repairable),
        run(2, status="failed", decision="rejected", failure_type="ci_step_timeout", repairability=infra),
        run(3, status="failed", decision="rejected", failure_type="ci_command_not_allowed", repairability=config),
    ]
    service = CIAnalyticsService(await populated_store(tmp_path, runs))
    failures = (await service.metrics()).failures
    assert failures.code_related_failure_count == 1
    assert failures.infrastructure_failure_count == 1
    assert failures.configuration_failure_count == 1


@pytest.mark.asyncio
async def test_repair_and_promotion_metrics(tmp_path: Path) -> None:
    repairable = {"repairable": True, "category": "repairable_tests", "confidence": .8, "reason_codes": []}
    runs = [
        run(1, workflow_id="w1", status="failed", decision="rejected", failure_type="ci_tests_failed", repairability=repairable, source_commit="a" * 40),
        run(2, workflow_id="w1", decision="accepted", source_commit="b" * 40),
        run(3, workflow_id="w2", status="failed", decision="rejected", failure_type="ci_tests_failed", repairability=repairable, source_commit="c" * 40),
        run(4, workflow_id="w2", status="failed", decision="rejected", failure_type="ci_repair_exhausted", source_commit="d" * 40),
    ]
    service = CIAnalyticsService(await populated_store(tmp_path, runs))
    metrics = await service.metrics()
    assert metrics.repair.ci_repair_required_count == 2
    assert metrics.repair.ci_repair_success_count == 1
    assert metrics.repair.ci_repair_exhausted_count == 1
    assert metrics.promotion.promotion_eligible_count == 1
    assert metrics.promotion.promotion_blocked_rejected_ci_count == 3


@pytest.mark.asyncio
async def test_legacy_incomplete_framework_limit_and_determinism(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI_ANALYTICS_MAX_LIMIT", "2")
    runs = [
        run(1, framework="fastapi", decision=None, gates=[], duration=None),
        run(2, framework="node", status="running", decision=None, duration=None),
        run(3, framework="fastapi", decision="accepted", duration=3),
    ]
    service = CIAnalyticsService(await populated_store(tmp_path, runs))
    first = await service.metrics(limit=50, framework="fastapi")
    second = await service.metrics(limit=50, framework="fastapi")
    assert first.limit == 2
    assert first.summary.total_runs == second.summary.total_runs == 1
    assert first.model_dump() == second.model_dump()


@pytest.mark.asyncio
async def test_audit_trail_is_ordered_and_redacts_secret_like_metadata(tmp_path: Path) -> None:
    repairable = {"repairable": True, "category": "repairable_tests", "confidence": .8, "reason_codes": [], "summary": "secret token should not leak"}
    runs = [
        run(1, workflow_id="audit", status="failed", decision="rejected", failure_type="ci_tests_failed", repairability=repairable, source_commit="a" * 40),
        run(2, workflow_id="audit", decision="accepted", source_commit="b" * 40),
    ]
    service = CIAnalyticsService(await populated_store(tmp_path, runs))
    trail = await service.audit_trail("audit", repair_state={"lineage": [{"attempt": 1, "source_run_id": "ci-001", "source_commit": "a" * 40, "repair_commit": "b" * 40, "result_run_id": "ci-002", "result_decision": "accepted"}]})
    event_types = [entry.event_type for entry in trail.entries]
    assert "ci_pipeline_started" in event_types
    assert "ci_failure_classified" in event_types
    assert "ci_repair_commit_created" in event_types
    assert "ci_repair_completed" in event_types
    assert "secret" not in trail.model_dump_json().casefold()


def test_ci_analytics_api_endpoints(tmp_path: Path) -> None:
    class Query:
        async def get_snapshot(self, thread_id: str) -> SimpleNamespace:
            return SimpleNamespace(project_name="project-fastapi", values={})

    @asynccontextmanager
    async def factory():
        store = CIPipelineStore(tmp_path / "api-ci.sqlite")
        await store.initialize()
        await store.create_run(run(1, workflow_id="workflow-api", decision="accepted", source_commit="a" * 40))
        yield SimpleNamespace(ci_analytics=CIAnalyticsService(store), ci=SimpleNamespace(store=store), query=Query())

    with TestClient(create_app(factory)) as client:
        metrics = client.get("/api/evaluations/ci/metrics")
        audit = client.get("/api/workflows/workflow-api/ci/audit")
    assert metrics.status_code == 200
    assert metrics.json()["summary"]["total_runs"] == 1
    assert audit.status_code == 200
    assert audit.json()["entries"]
