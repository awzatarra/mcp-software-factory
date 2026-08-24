from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from api.services.ci_analytics_service import CIAnalyticsService
from api.services.ci_service import (
    CIPromotionPolicy,
    classify_ci_failure_for_repair,
    evaluate_ci_promotion_eligibility,
)
from api.services.ci_store import CIPipelineStore
from graph.git_workflow import execute_git_commit_node, prepare_git_workflow_node
from graph.nodes import GraphDependencies
from graph.state import SoftwareFactoryState
from tool_executor import ToolExecutionOutcome


def completed_run(
    *,
    commit: str,
    decision: str,
    status: str = "passed",
    run_id: str = "run-1",
    source_mode: str = "commit",
    failed_gates: list[str] | None = None,
    warning_gates: list[str] | None = None,
) -> dict[str, object]:
    return {
        "ci_run_id": run_id,
        "status": status,
        "decision": decision,
        "source": {"source_mode": source_mode, "source_commit": commit},
        "pipeline": {"version": "6.21.1-v1", "framework": "fastapi"},
        "pipeline_fingerprint": f"fp-{run_id}",
        "gate_policy_version": "6.21.2-v1",
        "failed_gates": failed_gates or [],
        "warning_gates": warning_gates or [],
        "steps": [],
        "gates": [],
    }


def analytics_run(
    index: int,
    *,
    workflow_id: str = "workflow-1",
    status: str = "passed",
    decision: str | None = "accepted",
    duration: float | None = 10.0,
    source_mode: str = "commit",
    source_commit: str | None = None,
    gates: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
    failure_type: str | None = None,
    repairability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stamp = (datetime(2026, 8, 10, tzinfo=UTC) + timedelta(seconds=index)).isoformat()
    return {
        "ci_run_id": f"ci-{index:03d}",
        "workflow_id": workflow_id,
        "project_id": "project-fastapi",
        "pipeline_fingerprint": f"fp-{index}",
        "pipeline": {
            "pipeline_id": "fastapi-ci",
            "name": "FastAPI CI",
            "version": "6.21.1-v1",
            "framework": "fastapi",
            "steps": [{"step_id": "test", "name": "Tests", "type": "test", "command": ["python", "-m", "pytest"]}],
            "fail_fast": True,
            "timeout_seconds": 600,
        },
        "source": {"source_mode": source_mode, "source_commit": source_commit},
        "status": status,
        "started_at": stamp,
        "completed_at": stamp if status in {"passed", "failed"} else None,
        "duration_seconds": duration,
        "steps": steps if steps is not None else [{
            "step_id": "test",
            "name": "test",
            "type": "test",
            "status": "passed",
            "duration_seconds": duration or 0,
            "failure_message": "safe summary",
        }],
        "failed_step": None,
        "failure_type": failure_type,
        "failure_message": "safe summary with no secrets",
        "warnings": [],
        "gate_policy_version": "6.21.2-v1",
        "gates": gates if gates is not None else [{
            "gate_id": "test-gate",
            "type": "test",
            "status": "passed",
            "required": True,
            "blocking": True,
            "reason": "evaluated",
            "source_steps": ["test"],
        }],
        "decision": decision,
        "failed_gates": [item["gate_id"] for item in gates or [] if item.get("status") == "failed"],
        "warning_gates": [item["gate_id"] for item in gates or [] if item.get("status") == "warning"],
        "blocking_gate": next((item["gate_id"] for item in gates or [] if item.get("status") == "failed"), None),
        "gate_summary": {},
        "ci_validated_commit": source_commit if decision in {"accepted", "accepted_with_warnings"} else None,
        "repairability": repairability,
    }


async def populated_store(tmp_path: Path, runs: list[dict[str, Any]]) -> CIPipelineStore:
    store = CIPipelineStore(tmp_path / "ci.sqlite")
    await store.initialize()
    for item in runs:
        await store.create_run(item)
    return store


def graph_dependencies(executor: Any) -> GraphDependencies:
    return GraphDependencies(
        tool_executor=executor,
        openai_client=object(),  # type: ignore[arg-type]
        model="test-model",
    )


class MinimalGitRepairExecutor:
    git_integration_enabled = True

    def __init__(self, *, commit_sha: str = f"{1:040x}", status_files: list[str] | None = None) -> None:
        self.commit_sha = commit_sha
        self.status_files = list(status_files or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def openai_tool(self, public_tool_name: str) -> dict[str, Any]:
        return {"type": "function", "name": public_tool_name, "parameters": {"type": "object"}}

    async def execute(
        self,
        public_tool_name: str,
        arguments: dict[str, Any],
        *,
        state_context: SoftwareFactoryState,
        approval_mode: str = "prompt",
    ) -> ToolExecutionOutcome:
        del approval_mode
        self.calls.append((public_tool_name, arguments))
        if public_tool_name == "git__git_status":
            payload = {
                "success": True,
                "branch": "workflow/12345678",
                "clean": not self.status_files,
                "staged": [],
                "modified": [],
                "untracked": list(self.status_files),
                "deleted": [],
            }
            return ToolExecutionOutcome(public_tool_name, arguments, payload, {})
        if public_tool_name == "git__approve_commit":
            return ToolExecutionOutcome(public_tool_name, arguments, {"success": True, "approval_id": "approval-git"}, {})
        if public_tool_name == "git__commit":
            payload = {
                "success": True,
                "commit": self.commit_sha,
                "short_commit": self.commit_sha[:7],
                "branch": "workflow/12345678",
                "message": (state_context.get("git_commit_preview") or {}).get("proposed_message", "repair"),
                "files": list(state_context.get("git_staged_files") or []),
                "existing": False,
                "created_at": "2026-08-14T00:00:00Z",
                "phase": state_context.get("git_commit_phase") or "repair",
            }
            return ToolExecutionOutcome(public_tool_name, arguments, payload, {})
        raise AssertionError(f"Unexpected tool call: {public_tool_name}")


def repair_commit_state(*, source_commit: str) -> SoftwareFactoryState:
    return {
        "workflow_id": "workflow-ci-repair",
        "project_name": "health-api",
        "created_project_name": "health-api",
        "git_commit_phase": "repair",
        "git_commit_approval_id": "approval-git",
        "git_commit_preview": {"proposed_message": "Repair CI failure"},
        "git_staged_files": ["tests/test_health.py"],
        "git_commit_history": [],
        "ci_repair_state": "pending",
        "ci_repair_attempts": 1,
        "ci_repair_source_run_id": "ci-run-1",
        "ci_repair_source_commit": source_commit,
        "ci_validated_commit": source_commit,
        "ci_promotion_eligible": True,
        "ci_promotion_eligibility": {"eligible": True},
        "git_promotion_state": "awaiting_approval",
        "git_promotion_preview": {"promotion_id": "promotion-1"},
        "git_promotion_approval_id": "promotion-approval-1",
    }


def repair_validation_state() -> SoftwareFactoryState:
    return {
        "workflow_id": "workflow-ci-no-changes",
        "project_name": "health-api",
        "created_project_name": "health-api",
        "git_workflow_state": "workspace_prepared",
        "git_workflow_branch": "workflow/12345678",
        "git_state": "ready",
        "tests_passed": True,
        "repair_attempts": 1,
        "files_updated_during_repair": ["health-api/tests/test_health.py"],
    }


def test_ci_promotion_accepts_exact_sha_for_commit_bound_run() -> None:
    eligibility = evaluate_ci_promotion_eligibility(
        {"workflow_head": "a" * 40},
        completed_run(commit="a" * 40, decision="accepted"),
        CIPromotionPolicy(required=True),
    )

    assert eligibility.eligible is True
    assert eligibility.commit_match is True
    assert eligibility.reason == "ci_accepted"


def test_ci_promotion_requires_commit_bound_evidence_even_if_sha_matches() -> None:
    eligibility = evaluate_ci_promotion_eligibility(
        {"workflow_head": "a" * 40},
        completed_run(commit="a" * 40, decision="accepted", source_mode="working_tree"),
        CIPromotionPolicy(required=True),
    )

    assert eligibility.eligible is False
    assert eligibility.reason == "ci_required_for_promotion"
    assert eligibility.metadata["source_mode"] == "working_tree"


def test_required_gate_failure_blocks_promotion_decision() -> None:
    eligibility = evaluate_ci_promotion_eligibility(
        {"workflow_head": "a" * 40},
        completed_run(
            commit="a" * 40,
            decision="rejected",
            status="failed",
            failed_gates=["test-gate"],
        ),
        CIPromotionPolicy(required=True),
    )

    assert eligibility.eligible is False
    assert eligibility.reason == "ci_promotion_blocked"
    assert eligibility.blocking_gates == ["test-gate"]


def test_warning_policy_stays_configurable() -> None:
    run = completed_run(
        commit="a" * 40,
        decision="accepted_with_warnings",
        warning_gates=["lint-gate"],
    )

    allowed = evaluate_ci_promotion_eligibility(
        {"workflow_head": "a" * 40},
        run,
        CIPromotionPolicy(required=True, allow_warnings=True),
    )
    blocked = evaluate_ci_promotion_eligibility(
        {"workflow_head": "a" * 40},
        run,
        CIPromotionPolicy(required=True, allow_warnings=False),
    )

    assert allowed.eligible is True
    assert allowed.reason == "ci_accepted_with_warnings"
    assert blocked.eligible is False
    assert blocked.reason == "ci_warnings_blocked"


@pytest.mark.asyncio
async def test_ci_repair_commit_advances_sha_and_invalidates_previous_evidence() -> None:
    executor = MinimalGitRepairExecutor(commit_sha=f"{1:040x}")
    result = await execute_git_commit_node(
        repair_commit_state(source_commit=f"{0:040x}"),
        graph_dependencies(executor),
    )

    assert result["git_repair_commit_sha"] == f"{1:040x}"
    assert result["ci_repair_state"] == "running"
    assert result["ci_repair_target_commit"] == f"{1:040x}"
    assert result["ci_validated_commit"] is None
    assert result["ci_promotion_eligible"] is False


@pytest.mark.asyncio
async def test_ci_repair_without_changes_terminates_explicitly_without_loop() -> None:
    executor = MinimalGitRepairExecutor(status_files=[])
    result = await prepare_git_workflow_node(
        repair_validation_state(),
        graph_dependencies(executor),
    )

    assert result["git_commit_status"] == "no_changes"
    assert result["ci_repair_state"] == "failed"
    assert result["terminal_status"] == "ci_failed"
    assert result["failure_type"] == "ci_repair_no_changes"
    assert result["failure_stage"] == "prepare_git_commit"
    assert [name for name, _arguments in executor.calls] == ["git__git_status"]


@pytest.mark.parametrize(
    ("failure_type", "category"),
    [
        ("ci_step_timeout", "infrastructure"),
        ("ci_pipeline_timeout", "infrastructure"),
        ("ci_environment_unavailable", "infrastructure"),
        ("ci_execution_interrupted", "infrastructure"),
        ("ci_command_not_allowed", "configuration"),
    ],
)
def test_failure_to_repair_classification_matrix_is_stable(
    failure_type: str,
    category: str,
) -> None:
    repairability = classify_ci_failure_for_repair(
        analytics_run(
            1,
            status="failed",
            decision="rejected",
            source_commit="a" * 40,
            failure_type=failure_type,
        )
    )

    assert repairability.category == category
    assert repairability.repairable is False


def test_promotion_is_blocked_without_ci_when_required() -> None:
    eligibility = evaluate_ci_promotion_eligibility(
        {"workflow_head": "a" * 40},
        None,
        CIPromotionPolicy(required=True),
    )

    assert eligibility.eligible is False
    assert eligibility.reason == "ci_required_for_promotion"


@pytest.mark.asyncio
async def test_ci_audit_trail_is_safe_and_reconstructs_repair_lineage(tmp_path: Path) -> None:
    repairability = {
        "repairable": True,
        "category": "repairable_tests",
        "confidence": 0.8,
        "reason_codes": [],
        "summary": "secret token should not leak",
    }
    store = await populated_store(tmp_path, [
        analytics_run(
            1,
            workflow_id="audit",
            status="failed",
            decision="rejected",
            failure_type="ci_tests_failed",
            repairability=repairability,
            source_commit="a" * 40,
        ),
        analytics_run(
            2,
            workflow_id="audit",
            decision="accepted",
            source_commit="b" * 40,
        ),
    ])

    trail = await CIAnalyticsService(store).audit_trail(
        "audit",
        repair_state={
            "lineage": [{
                "attempt": 1,
                "source_run_id": "ci-001",
                "source_commit": "a" * 40,
                "repair_commit": "b" * 40,
                "result_run_id": "ci-002",
                "result_decision": "accepted",
            }]
        },
    )

    event_types = [entry.event_type for entry in trail.entries]
    assert event_types.count("ci_repair_started") == 1
    assert "ci_repair_commit_created" in event_types
    assert "ci_repair_completed" in event_types
    assert "secret" not in trail.model_dump_json().casefold()


def test_ci_env_defaults_are_in_sync() -> None:
    root = Path(__file__).resolve().parents[1]

    def ci_lines(path: Path) -> list[str]:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("CI_")]

    assert ci_lines(root / ".env") == ci_lines(root / ".env.example")


@pytest.mark.asyncio
async def test_legacy_ci_run_without_gates_or_decision_remains_compatible(tmp_path: Path) -> None:
    store = await populated_store(tmp_path, [
        analytics_run(
            1,
            workflow_id="legacy",
            decision=None,
            duration=None,
            source_mode="working_tree",
            source_commit=None,
            gates=[],
            failure_type=None,
        )
    ])
    service = CIAnalyticsService(store)

    metrics = await service.metrics()
    trail = await service.audit_trail("legacy")
    loaded = await store.get_run("ci-001")

    assert metrics.summary.total_runs == 1
    assert metrics.summary.runs_with_decision == 0
    assert trail.total >= 2
    assert loaded is not None
    assert loaded["decision"] is None
    assert loaded["gates"] == []
