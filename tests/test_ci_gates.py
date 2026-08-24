from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from api.ci_models import CIPipelineDefinition, CIPipelineStep, CIStepRun
from api.services.ci_service import (
    CIGatePolicy,
    CIPromotionPolicy,
    CIPipelinePolicy,
    CIPipelineService,
    build_ci_gate_definitions,
    classify_ci_failure_for_repair,
    evaluate_ci_gates,
    evaluate_ci_promotion_eligibility,
)
from api.services.ci_store import CIPipelineStore


def pipeline(*steps: CIPipelineStep, framework: str = "fastapi") -> CIPipelineDefinition:
    return CIPipelineDefinition(
        pipeline_id=f"{framework}-test",
        name="test",
        version="6.21.1-v1",
        framework=framework,
        steps=list(steps),
        fail_fast=True,
        timeout_seconds=60,
    )


def step(step_id: str, step_type: str, status: str, *, exit_code: int | None = 0) -> CIStepRun:
    return CIStepRun(
        step_id=step_id,
        name=step_id,
        type=step_type,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        exit_code=exit_code,
        failure_type=None if status == "passed" else f"ci_{step_type}s_failed",
    )


def service(tmp_path: Path, *, gate_policy: CIGatePolicy | None = None) -> CIPipelineService:
    return CIPipelineService(
        tmp_path,
        store=CIPipelineStore(tmp_path / "ci.sqlite"),
        policy=CIPipelinePolicy(version="6.21.1-v1", pipeline_timeout_seconds=60, step_timeout_seconds=30, output_max_chars=2000),
        gate_policy=gate_policy or CIGatePolicy(),
    )


def project(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    (root / "tests").mkdir(parents=True)
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return root


def test_fastapi_test_pass_has_not_applicable_build_and_accepted_decision() -> None:
    definition = pipeline(CIPipelineStep(step_id="test", name="Test", type="test", command=["python", "-m", "pytest"]))

    evaluation = evaluate_ci_gates(definition, [step("test", "test", "passed")], CIGatePolicy())

    assert {gate.type: gate.status for gate in evaluation.gates} == {
        "build": "not_applicable",
        "test": "passed",
        "lint": "not_applicable",
        "package": "not_applicable",
    }
    assert evaluation.decision == "accepted"
    assert evaluation.summary["blocking_failed_count"] == 0


def test_node_build_test_lint_success_is_accepted() -> None:
    definition = pipeline(
        CIPipelineStep(step_id="build", name="Build", type="build", command=["npm", "run", "build"]),
        CIPipelineStep(step_id="test", name="Test", type="test", command=["npm", "test"]),
        CIPipelineStep(step_id="lint", name="Lint", type="lint", command=["npm", "run", "lint"], required=False, continue_on_error=True),
        framework="node",
    )

    evaluation = evaluate_ci_gates(
        definition,
        [step("build", "build", "passed"), step("test", "test", "passed"), step("lint", "lint", "passed")],
        CIGatePolicy(),
    )

    assert evaluation.decision == "accepted"
    assert all(gate.status in {"passed", "not_applicable"} for gate in evaluation.gates)


def test_dotnet_restore_build_test_maps_restore_into_build_gate() -> None:
    definition = pipeline(
        CIPipelineStep(step_id="restore", name="Restore", type="build", command=["dotnet", "restore"]),
        CIPipelineStep(step_id="build", name="Build", type="build", command=["dotnet", "build", "--no-restore"]),
        CIPipelineStep(step_id="test", name="Test", type="test", command=["dotnet", "test", "--no-build"]),
        framework="dotnet",
    )

    evaluation = evaluate_ci_gates(
        definition,
        [step("restore", "build", "passed"), step("build", "build", "passed"), step("test", "test", "passed")],
        CIGatePolicy(),
    )

    assert next(gate for gate in evaluation.gates if gate.type == "build").status == "passed"
    assert next(gate for gate in evaluation.gates if gate.type == "test").status == "passed"
    assert evaluation.decision == "accepted"


def test_build_failure_rejects_and_test_skipped_is_not_second_root_cause() -> None:
    definition = pipeline(
        CIPipelineStep(step_id="build", name="Build", type="build", command=["python", "--version"]),
        CIPipelineStep(step_id="test", name="Test", type="test", command=["python", "-m", "pytest"]),
    )

    evaluation = evaluate_ci_gates(
        definition,
        [step("build", "build", "failed", exit_code=1), step("test", "test", "skipped", exit_code=None)],
        CIGatePolicy(),
    )

    assert evaluation.decision == "rejected"
    assert evaluation.blocking_gate == "build-gate"
    assert next(gate for gate in evaluation.gates if gate.type == "test").status == "skipped"
    assert next(gate for gate in evaluation.gates if gate.type == "test").failure_type is None


def test_test_failure_rejects() -> None:
    definition = pipeline(CIPipelineStep(step_id="test", name="Test", type="test", command=["python", "-m", "pytest"]))

    evaluation = evaluate_ci_gates(definition, [step("test", "test", "failed", exit_code=1)], CIGatePolicy())

    assert evaluation.decision == "rejected"
    assert evaluation.blocking_gate == "test-gate"
    assert evaluation.failure_type == "ci_test_gate_failed"


def test_lint_failure_non_blocking_warns() -> None:
    definition = pipeline(
        CIPipelineStep(step_id="test", name="Test", type="test", command=["python", "-m", "pytest"]),
        CIPipelineStep(step_id="lint", name="Lint", type="lint", command=["python", "--version"], required=False, continue_on_error=True),
    )

    evaluation = evaluate_ci_gates(
        definition,
        [step("test", "test", "passed"), step("lint", "lint", "failed", exit_code=1)],
        CIGatePolicy(lint_blocking=False),
    )

    lint = next(gate for gate in evaluation.gates if gate.type == "lint")
    assert lint.status == "warning"
    assert lint.blocking is False
    assert evaluation.decision == "accepted_with_warnings"


def test_lint_failure_blocking_rejects() -> None:
    definition = pipeline(
        CIPipelineStep(step_id="test", name="Test", type="test", command=["python", "-m", "pytest"]),
        CIPipelineStep(step_id="lint", name="Lint", type="lint", command=["python", "--version"], required=True),
    )

    evaluation = evaluate_ci_gates(
        definition,
        [step("test", "test", "passed"), step("lint", "lint", "failed", exit_code=1)],
        CIGatePolicy(lint_blocking=True),
    )

    assert evaluation.decision == "rejected"
    assert evaluation.blocking_gate == "lint-gate"


def test_package_failure_non_blocking_warns() -> None:
    definition = pipeline(
        CIPipelineStep(step_id="test", name="Test", type="test", command=["python", "-m", "pytest"]),
        CIPipelineStep(step_id="package", name="Package", type="package", command=["python", "--version"], required=True),
    )

    evaluation = evaluate_ci_gates(
        definition,
        [step("test", "test", "passed"), step("package", "package", "failed", exit_code=1)],
        CIGatePolicy(package_blocking=False),
    )

    assert next(gate for gate in evaluation.gates if gate.type == "package").status == "warning"
    assert evaluation.decision == "accepted_with_warnings"


def test_multi_step_test_gate_requires_all_by_default() -> None:
    definition = pipeline(
        CIPipelineStep(step_id="unit", name="Unit", type="test", command=["python", "-m", "pytest", "tests/unit"]),
        CIPipelineStep(step_id="integration", name="Integration", type="test", command=["python", "-m", "pytest", "tests/integration"]),
    )

    evaluation = evaluate_ci_gates(
        definition,
        [step("unit", "test", "passed"), step("integration", "test", "failed", exit_code=1)],
        CIGatePolicy(),
    )

    assert next(gate for gate in evaluation.gates if gate.type == "test").status == "failed"
    assert evaluation.decision == "rejected"


def test_minimum_success_count_can_accept_partial_suite() -> None:
    definition = pipeline(
        CIPipelineStep(step_id="unit", name="Unit", type="test", command=["python", "-m", "pytest", "tests/unit"]),
        CIPipelineStep(step_id="integration", name="Integration", type="test", command=["python", "-m", "pytest", "tests/integration"]),
        CIPipelineStep(step_id="contract", name="Contract", type="test", command=["python", "-m", "pytest", "tests/contract"]),
    )
    gates = build_ci_gate_definitions(definition, CIGatePolicy())
    gates = [gate.model_copy(update={"minimum_success_count": 2}) if gate.type == "test" else gate for gate in gates]

    evaluation = evaluate_ci_gates(
        definition,
        [step("unit", "test", "passed"), step("integration", "test", "passed"), step("contract", "test", "failed", exit_code=1)],
        CIGatePolicy(),
        gates,
    )

    assert next(gate for gate in evaluation.gates if gate.type == "test").status == "passed"
    assert evaluation.decision == "accepted"


def completed_run(
    *,
    commit: str,
    decision: str,
    status: str = "passed",
    run_id: str = "run-1",
    failed_gates: list[str] | None = None,
    warning_gates: list[str] | None = None,
) -> dict[str, object]:
    return {
        "ci_run_id": run_id,
        "status": status,
        "decision": decision,
        "source": {"source_mode": "commit", "source_commit": commit},
        "pipeline": {"version": "6.21.1-v1"},
        "pipeline_fingerprint": f"fp-{run_id}",
        "gate_policy_version": "6.21.2-v1",
        "failed_gates": failed_gates or [],
        "warning_gates": warning_gates or [],
    }


def rejected_pipeline_run(*, gate: str, step_type: str, failure_type: str, status: str = "failed") -> dict[str, object]:
    return {
        "ci_run_id": "run-failed",
        "workflow_id": "workflow-1",
        "project_id": "app",
        "pipeline": pipeline(CIPipelineStep(step_id=step_type, name=step_type, type=step_type, command=["python", "-m", "pytest"])).model_dump(),
        "pipeline_fingerprint": "f" * 64,
        "status": status,
        "source": {"source_mode": "commit", "source_commit": "a" * 40},
        "steps": [step(step_type, step_type, "timed_out" if status == "timed_out" else "failed", exit_code=1).model_dump()],
        "failed_step": step_type,
        "failure_type": failure_type,
        "failure_message": "failure summary",
        "warnings": [],
        "gate_policy_version": "6.21.2-v1",
        "gates": [],
        "decision": "rejected",
        "failed_gates": [gate],
        "warning_gates": [],
        "blocking_gate": gate,
        "gate_summary": {},
        "ci_validated_commit": "a" * 40,
    }


def test_classify_ci_test_failure_as_repairable_tests() -> None:
    repairability = classify_ci_failure_for_repair(
        rejected_pipeline_run(gate="test-gate", step_type="test", failure_type="ci_tests_failed")
    )

    assert repairability.repairable is True
    assert repairability.category == "repairable_tests"
    assert "ci_test_gate_failed" in repairability.reason_codes


def test_classify_ci_build_failure_as_repairable_build() -> None:
    repairability = classify_ci_failure_for_repair(
        rejected_pipeline_run(gate="build-gate", step_type="build", failure_type="ci_build_failed")
    )

    assert repairability.repairable is True
    assert repairability.category == "repairable_build"


def test_classify_ci_timeout_as_infrastructure() -> None:
    repairability = classify_ci_failure_for_repair(
        rejected_pipeline_run(gate="test-gate", step_type="test", failure_type="ci_step_timeout", status="timed_out")
    )

    assert repairability.repairable is False
    assert repairability.category == "infrastructure"


def test_execution_context_failure_is_configuration_and_never_enters_repair() -> None:
    repairability = classify_ci_failure_for_repair(
        rejected_pipeline_run(
            gate="test-gate",
            step_type="test",
            failure_type="ci_execution_context_invalid",
        )
    )

    assert repairability.repairable is False
    assert repairability.category == "configuration"
    assert repairability.reason_codes == ["ci_configuration_failure"]


def test_lint_warning_decision_does_not_enter_repair() -> None:
    run = completed_run(commit="a" * 40, decision="accepted_with_warnings", warning_gates=["lint-gate"])
    repairability = classify_ci_failure_for_repair(run)

    assert repairability.repairable is False
    assert repairability.category == "non_repairable"
    assert repairability.reason_codes == ["ci_decision_not_rejected"]


def test_ci_promotion_requires_run_for_commit() -> None:
    eligibility = evaluate_ci_promotion_eligibility(
        {"workflow_head": "a" * 40},
        None,
        CIPromotionPolicy(required=True),
    )

    assert eligibility.eligible is False
    assert eligibility.reason == "ci_required_for_promotion"


def test_ci_promotion_accepts_exact_commit() -> None:
    eligibility = evaluate_ci_promotion_eligibility(
        {"workflow_head": "a" * 40},
        completed_run(commit="a" * 40, decision="accepted"),
        CIPromotionPolicy(required=True),
    )

    assert eligibility.eligible is True
    assert eligibility.commit_match is True
    assert eligibility.run_id == "run-1"


def test_ci_promotion_blocks_rejected_decision() -> None:
    eligibility = evaluate_ci_promotion_eligibility(
        {"workflow_head": "a" * 40},
        completed_run(commit="a" * 40, decision="rejected", status="failed", failed_gates=["test-gate"]),
        CIPromotionPolicy(required=True),
    )

    assert eligibility.eligible is False
    assert eligibility.reason == "ci_promotion_blocked"
    assert eligibility.blocking_gates == ["test-gate"]


def test_ci_promotion_warning_policy_is_configurable() -> None:
    run = completed_run(commit="a" * 40, decision="accepted_with_warnings", warning_gates=["lint-gate"])

    allowed = evaluate_ci_promotion_eligibility({"workflow_head": "a" * 40}, run, CIPromotionPolicy(allow_warnings=True))
    blocked = evaluate_ci_promotion_eligibility({"workflow_head": "a" * 40}, run, CIPromotionPolicy(allow_warnings=False))

    assert allowed.eligible is True
    assert blocked.eligible is False
    assert blocked.reason == "ci_warnings_blocked"


def test_ci_promotion_rejects_commit_mismatch() -> None:
    eligibility = evaluate_ci_promotion_eligibility(
        {"workflow_head": "b" * 40},
        completed_run(commit="a" * 40, decision="accepted"),
        CIPromotionPolicy(required=True),
    )

    assert eligibility.eligible is False
    assert eligibility.reason == "ci_commit_mismatch"
    assert eligibility.commit_match is False


@pytest.mark.asyncio
async def test_run_persists_policy_snapshot_and_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project(tmp_path)
    ci = service(tmp_path, gate_policy=CIGatePolicy(version="policy-a", lint_blocking=False))
    await ci.initialize()
    ci.default_pipeline = lambda _root, _framework: pipeline(  # type: ignore[method-assign]
        CIPipelineStep(step_id="test", name="Test", type="test", command=["python", "-m", "pytest"]),
        CIPipelineStep(step_id="lint", name="Lint", type="lint", command=["python", "--version"], required=False, continue_on_error=True),
    )

    def fake_run(command, *_args, **_kwargs):
        return subprocess.CompletedProcess(command, 1 if "--version" in command else 0, b"", b"")

    monkeypatch.setattr(ci, "_run_command", fake_run)
    preview = await ci.prepare("workflow-1", "app")
    run = await ci.run("workflow-1", "app", expected_fingerprint=preview.pipeline_fingerprint)
    ci.gate_policy = CIGatePolicy(version="policy-b", lint_blocking=True)
    loaded = await ci.get_run("workflow-1", run.ci_run_id)

    assert loaded is not None
    assert loaded.gate_policy_version == "policy-a"
    assert loaded.decision == "accepted_with_warnings"
    assert loaded.gates == run.gates


@pytest.mark.asyncio
async def test_latest_completed_exact_commit_run_wins_over_failed_history(tmp_path: Path) -> None:
    project(tmp_path)
    ci = service(tmp_path)
    await ci.initialize()
    preview = await ci.prepare("workflow-1", "app", source_commit="a" * 40)
    base = {
        "workflow_id": "workflow-1",
        "project_id": "app",
        "pipeline": preview.pipeline.model_dump(),
        "pipeline_fingerprint": preview.pipeline_fingerprint,
        "source": preview.source.model_dump(),
        "steps": [{"step_id": "test", "name": "Test", "type": "test", "status": "passed"}],
        "gate_policy_version": "6.21.2-v1",
        "gates": [],
        "failed_gates": [],
        "warning_gates": [],
        "gate_summary": {},
        "ci_validated_commit": "a" * 40,
    }
    await ci.store.create_run({**base, "ci_run_id": "run-1", "status": "failed", "decision": "rejected"})
    await ci.store.create_run({**base, "ci_run_id": "run-2", "status": "passed", "decision": "accepted"})
    await ci.store.create_run({**base, "ci_run_id": "run-3", "status": "running", "decision": None})

    run = await ci.latest_completed_run_for_commit("workflow-1", "a" * 40)
    eligibility = await ci.promotion_eligibility("workflow-1", "a" * 40)

    assert run is not None
    assert run.ci_run_id == "run-2"
    assert eligibility.eligible is True
    assert eligibility.run_id == "run-2"


@pytest.mark.asyncio
async def test_policy_change_between_prepare_and_run_is_stale(tmp_path: Path) -> None:
    project(tmp_path)
    ci = service(tmp_path, gate_policy=CIGatePolicy(version="policy-a"))
    await ci.initialize()
    preview = await ci.prepare("workflow-1", "app")
    ci.gate_policy = CIGatePolicy(version="policy-b")

    with pytest.raises(Exception, match="ci_pipeline_stale"):
        await ci.run("workflow-1", "app", expected_fingerprint=preview.pipeline_fingerprint)


@pytest.mark.asyncio
async def test_legacy_run_without_gates_is_readable(tmp_path: Path) -> None:
    project(tmp_path)
    ci = service(tmp_path)
    await ci.initialize()
    preview = await ci.prepare("workflow-1", "app")
    await ci.store.create_run({
        "ci_run_id": "legacy",
        "workflow_id": "workflow-1",
        "project_id": "app",
        "pipeline": preview.pipeline.model_dump(),
        "pipeline_fingerprint": preview.pipeline_fingerprint,
        "status": "passed",
        "source": preview.source.model_dump(),
        "steps": [{"step_id": "test", "name": "Test", "type": "test", "status": "passed"}],
    })

    run = await ci.get_run("workflow-1", "legacy")

    assert run is not None
    assert run.decision is None
    assert run.gates == []
