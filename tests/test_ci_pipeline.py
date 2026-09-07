from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.ci_models import CIPipelineDefinition, CIPipelineStep
from api.models import WorkflowSnapshotResponse
from api.services.ci_service import (
    CICommandNotAllowed,
    CIPathViolation,
    CIPipelinePolicy,
    CIPipelineService,
    validate_ci_command,
    validate_ci_relative_path,
)
from api.services.ci_store import CIPipelineStore


def make_python_project(root: Path, name: str = "health-api") -> Path:
    project = root / name
    (project / "tests").mkdir(parents=True)
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (project / "tests" / "test_health.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return project


def make_service(tmp_path: Path, *, output_max_chars: int = 20_000, pipeline_timeout: float = 600, step_timeout: float = 180) -> CIPipelineService:
    return CIPipelineService(
        tmp_path,
        store=CIPipelineStore(tmp_path / "ci.sqlite"),
        policy=CIPipelinePolicy(
            version="6.21.1-v1",
            pipeline_timeout_seconds=pipeline_timeout,
            step_timeout_seconds=step_timeout,
            output_max_chars=output_max_chars,
        ),
    )


@pytest.mark.asyncio
async def test_prepare_fastapi_pipeline_has_test_step_and_stable_fingerprint(tmp_path: Path) -> None:
    make_python_project(tmp_path)
    service = make_service(tmp_path)
    await service.initialize()

    first = await service.prepare("workflow-1", "health-api")
    second = await service.prepare("workflow-1", "health-api")

    assert first.pipeline.framework == "fastapi"
    assert [step.step_id for step in first.pipeline.steps] == ["test"]
    assert first.pipeline.steps[0].command == ["python", "-m", "pytest", "--rootdir=."]
    assert first.pipeline_fingerprint == second.pipeline_fingerprint


@pytest.mark.asyncio
async def test_python_pipeline_prefers_project_virtual_environment(tmp_path: Path) -> None:
    project = make_python_project(tmp_path)
    interpreter = project / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"")
    service = make_service(tmp_path)
    await service.initialize()

    preview = await service.prepare("workflow-1", "health-api")

    assert preview.pipeline.steps[0].command == [
        ".venv/Scripts/python.exe", "--version",
    ]
    assert preview.pipeline.steps[1].command == [
        ".venv/Scripts/python.exe", "-m", "pytest", "--rootdir=.",
    ]


@pytest.mark.asyncio
async def test_prepare_node_uses_only_existing_scripts(tmp_path: Path) -> None:
    project = tmp_path / "node-app"
    project.mkdir()
    (project / "package.json").write_text(
        '{"scripts":{"build":"vite build","test":"vitest run"}}',
        encoding="utf-8",
    )
    service = make_service(tmp_path)
    await service.initialize()

    preview = await service.prepare("workflow-1", "node-app")

    assert [(step.step_id, step.command) for step in preview.pipeline.steps] == [
        ("build", ["npm", "run", "build"]),
        ("test", ["npm", "test"]),
    ]
    assert "lint" not in [step.step_id for step in preview.pipeline.steps]


@pytest.mark.asyncio
async def test_prepare_dotnet_pipeline(tmp_path: Path) -> None:
    project = tmp_path / "dotnet-app"
    project.mkdir()
    (project / "dotnet-app.csproj").write_text("<Project />", encoding="utf-8")
    service = make_service(tmp_path)
    await service.initialize()

    preview = await service.prepare("workflow-1", "dotnet-app")

    assert [step.command for step in preview.pipeline.steps] == [
        ["dotnet", "restore"],
        ["dotnet", "build", "--no-restore"],
        ["dotnet", "test", "--no-build"],
    ]


@pytest.mark.asyncio
async def test_successful_pipeline_persists_passed_run(tmp_path: Path) -> None:
    make_python_project(tmp_path)
    service = make_service(tmp_path)
    await service.initialize()
    preview = await service.prepare("workflow-1", "health-api")

    run = await service.run("workflow-1", "health-api", expected_fingerprint=preview.pipeline_fingerprint)

    assert run.status == "passed"
    assert run.steps[0].status == "passed"
    assert run.steps[0].exit_code == 0
    assert (await service.get_run("workflow-1", run.ci_run_id)) == run


@pytest.mark.asyncio
async def test_fastapi_ci_executes_pytest_from_resolved_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_python_project(tmp_path)
    service = make_service(tmp_path)
    await service.initialize()
    captured: dict[str, object] = {}

    def fake_run(command: list[str], cwd: Path, timeout: float):
        captured.update(command=list(command), cwd=cwd, timeout=timeout)
        config_index = command.index("-c") + 1
        assert Path(command[config_index]).is_file()
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"rootdir: {project.resolve()}\n1 passed\n".encode(),
            stderr=b"",
        )

    monkeypatch.setattr(service, "_run_command", fake_run)
    preview = await service.prepare(
        "workflow-1", "health-api", repository_root="health-api"
    )
    run = await service.run(
        "workflow-1",
        "health-api",
        expected_fingerprint=preview.pipeline_fingerprint,
        repository_root="health-api",
    )

    assert captured["cwd"] == project.resolve()
    assert "--rootdir=." in captured["command"]
    assert run.status == "passed"
    assert run.decision == "accepted"
    assert run.source.repository_root == "health-api"


@pytest.mark.asyncio
async def test_pytest_does_not_inherit_parent_repository_configuration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = make_python_project(workspace)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = ['--parent-option-must-not-load']\n",
        encoding="utf-8",
    )
    service = CIPipelineService(
        workspace,
        store=CIPipelineStore(tmp_path / "isolated-ci.sqlite"),
    )
    await service.initialize()
    preview = await service.prepare("workflow-1", "health-api")

    run = await service.run(
        "workflow-1", "health-api", expected_fingerprint=preview.pipeline_fingerprint
    )

    assert run.status == "passed"
    assert str(project.resolve()) in (run.steps[-1].stdout_summary or "")
    assert "parent-option-must-not-load" not in (run.steps[-1].stderr_summary or "")


@pytest.mark.asyncio
async def test_repository_root_traversal_and_cross_project_context_are_rejected(
    tmp_path: Path,
) -> None:
    make_python_project(tmp_path)
    make_python_project(tmp_path, "other-api")
    service = make_service(tmp_path)
    await service.initialize()

    with pytest.raises(CIPathViolation, match="ci_path_violation"):
        await service.prepare(
            "workflow-1", "health-api", repository_root="../health-api"
        )
    with pytest.raises(CIPathViolation, match="ci_path_violation"):
        await service.prepare(
            "workflow-1", "health-api", repository_root="other-api"
        )


@pytest.mark.asyncio
async def test_wrong_reported_pytest_root_is_configuration_failure_not_repairable_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_python_project(tmp_path)
    service = make_service(tmp_path)
    await service.initialize()

    def wrong_root(command: list[str], cwd: Path, timeout: float):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=f"rootdir: {tmp_path.resolve()}\ncollection error\n".encode(),
            stderr=b"",
        )

    monkeypatch.setattr(service, "_run_command", wrong_root)
    preview = await service.prepare("workflow-1", "health-api")
    run = await service.run(
        "workflow-1", "health-api", expected_fingerprint=preview.pipeline_fingerprint
    )

    assert run.failure_type == "ci_execution_context_invalid"
    assert run.repairability is not None
    assert run.repairability.repairable is False
    assert run.repairability.category == "configuration"


@pytest.mark.asyncio
async def test_correct_execution_root_preserves_exact_source_commit_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_python_project(tmp_path)
    service = make_service(tmp_path)
    await service.initialize()
    commit = "2bd7367063e051796f0e17b26d1ffd27c46277f7"

    def passed(command: list[str], cwd: Path, timeout: float):
        assert cwd == project.resolve()
        return subprocess.CompletedProcess(
            command, 0, stdout=f"rootdir: {cwd}\n1 passed\n".encode(), stderr=b""
        )

    monkeypatch.setattr(service, "_run_command", passed)
    preview = await service.prepare(
        "workflow-1", "health-api", source_commit=commit,
        repository_root="health-api",
    )
    run = await service.run(
        "workflow-1", "health-api",
        expected_fingerprint=preview.pipeline_fingerprint,
        source_commit=commit,
        repository_root="health-api",
    )

    assert run.ci_validated_commit == commit
    assert run.source.source_commit == commit
    assert run.decision == "accepted"


@pytest.mark.asyncio
async def test_fail_fast_skips_required_following_steps(tmp_path: Path) -> None:
    make_python_project(tmp_path)
    service = make_service(tmp_path)
    await service.initialize()
    service.default_pipeline = lambda _root, _framework: CIPipelineDefinition(  # type: ignore[method-assign]
        pipeline_id="test",
        name="test",
        version="6.21.1-v1",
        framework="fastapi",
        fail_fast=True,
        timeout_seconds=60,
        steps=[
            CIPipelineStep(step_id="build", name="Build", type="build", command=["python", "--bad-option"]),
            CIPipelineStep(step_id="test", name="Test", type="test", command=["python", "-m", "pytest"]),
        ],
    )
    preview = await service.prepare("workflow-1", "health-api")

    run = await service.run("workflow-1", "health-api", expected_fingerprint=preview.pipeline_fingerprint)

    assert run.status == "failed"
    assert run.failed_step == "build"
    assert run.failure_type == "ci_build_failed"
    assert run.steps[0].status == "failed"
    assert run.steps[1].status == "skipped"


@pytest.mark.asyncio
async def test_test_failure_gets_ci_tests_failed(tmp_path: Path) -> None:
    project = make_python_project(tmp_path)
    (project / "tests" / "test_health.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    service = make_service(tmp_path)
    await service.initialize()
    preview = await service.prepare("workflow-1", "health-api")

    run = await service.run("workflow-1", "health-api", expected_fingerprint=preview.pipeline_fingerprint)

    assert run.status == "failed"
    assert run.failed_step == "test"
    assert run.failure_type == "ci_tests_failed"


@pytest.mark.asyncio
async def test_optional_lint_continue_on_error_records_warning(tmp_path: Path) -> None:
    make_python_project(tmp_path)
    service = make_service(tmp_path)
    await service.initialize()
    service.default_pipeline = lambda _root, _framework: CIPipelineDefinition(  # type: ignore[method-assign]
        pipeline_id="test",
        name="test",
        version="6.21.1-v1",
        framework="fastapi",
        fail_fast=True,
        timeout_seconds=60,
        steps=[
            CIPipelineStep(step_id="lint", name="Lint", type="lint", command=["python", "--bad-option"], required=False, continue_on_error=True),
            CIPipelineStep(step_id="test", name="Test", type="test", command=["python", "-m", "pytest"]),
        ],
    )
    preview = await service.prepare("workflow-1", "health-api")

    run = await service.run("workflow-1", "health-api", expected_fingerprint=preview.pipeline_fingerprint)

    assert run.status == "passed"
    assert run.steps[0].status == "failed"
    assert run.steps[1].status == "passed"
    assert run.warnings == ["lint:ci_lint_failed"]


@pytest.mark.asyncio
async def test_step_timeout_marks_pipeline_failed_and_kills_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_python_project(tmp_path)
    service = make_service(tmp_path, step_timeout=0.1)
    await service.initialize()

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["python", "--version"], timeout=0.1)

    monkeypatch.setattr(service, "_run_command", timeout)
    preview = await service.prepare("workflow-1", "health-api")

    run = await service.run("workflow-1", "health-api", expected_fingerprint=preview.pipeline_fingerprint)

    assert run.status == "failed"
    assert run.steps[0].status == "timed_out"
    assert run.failure_type == "ci_step_timeout"


@pytest.mark.asyncio
async def test_pipeline_timeout_uses_pipeline_failure_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_python_project(tmp_path)
    service = make_service(tmp_path, pipeline_timeout=0.1, step_timeout=10)
    await service.initialize()

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["python", "--version"], timeout=0.1)

    monkeypatch.setattr(service, "_run_command", timeout)
    preview = await service.prepare("workflow-1", "health-api")

    run = await service.run("workflow-1", "health-api", expected_fingerprint=preview.pipeline_fingerprint)

    assert run.status == "timed_out"
    assert run.failure_type == "ci_pipeline_timeout"


@pytest.mark.parametrize(
    "command",
    [
        ["powershell", "-Command", "Write-Host hi"],
        ["cmd", "/c", "dir"],
        ["bash", "-c", "echo hi"],
        ["rm", "-rf", "."],
        ["python", "-c", "print('hi')"],
        ["pip", "install", "requests"],
    ],
)
def test_command_safety_rejects_shell_and_install_commands(command: list[str]) -> None:
    with pytest.raises(CICommandNotAllowed):
        validate_ci_command(command)


@pytest.mark.parametrize("path", ["../../", "a/../b", ".git/hooks", "C:/tmp"])
def test_working_directory_path_safety(path: str) -> None:
    with pytest.raises(Exception):
        validate_ci_relative_path(path)


@pytest.mark.asyncio
async def test_output_is_truncated_and_secrets_are_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_python_project(tmp_path)
    service = make_service(tmp_path, output_max_chars=20)
    await service.initialize()

    def output(*_args, **_kwargs):
        return subprocess.CompletedProcess(["python", "--version"], 0, b"token=dummy very long output", b"Bearer dummy")

    monkeypatch.setattr(service, "_run_command", output)
    preview = await service.prepare("workflow-1", "health-api")

    run = await service.run("workflow-1", "health-api", expected_fingerprint=preview.pipeline_fingerprint)

    assert run.steps[0].output_truncated is True
    assert "dummy" not in (run.steps[0].stdout_summary or "")
    assert "token=[REDACTED]" in (run.steps[0].stdout_summary or "")
    assert run.steps[0].stderr_summary == "Bearer [REDACTED]"


@pytest.mark.asyncio
async def test_fingerprint_changes_when_source_commit_changes(tmp_path: Path) -> None:
    project = make_python_project(tmp_path)
    subprocess.run(["git", "init"], cwd=project, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", "--", "."], cwd=project, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "first"], cwd=project, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    service = make_service(tmp_path)
    await service.initialize()
    first = await service.prepare("workflow-1", "health-api")
    (project / "README.md").write_text("change\n", encoding="utf-8")
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", "--", "."], cwd=project, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "second"], cwd=project, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    second = await service.prepare("workflow-1", "health-api")

    assert first.pipeline_fingerprint != second.pipeline_fingerprint
    assert first.source.source_commit != second.source.source_commit


@pytest.mark.asyncio
async def test_stale_preview_is_rejected(tmp_path: Path) -> None:
    project = make_python_project(tmp_path)
    subprocess.run(["git", "init"], cwd=project, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", "--", "."], cwd=project, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "first"], cwd=project, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    service = make_service(tmp_path)
    await service.initialize()
    preview = await service.prepare("workflow-1", "health-api")
    (project / "README.md").write_text("change\n", encoding="utf-8")

    with pytest.raises(Exception, match="ci_pipeline_stale"):
        await service.run("workflow-1", "health-api", expected_fingerprint=preview.pipeline_fingerprint)


@pytest.mark.asyncio
async def test_idempotent_completed_run_does_not_execute_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_python_project(tmp_path)
    service = make_service(tmp_path)
    await service.initialize()
    preview = await service.prepare("workflow-1", "health-api")
    run = await service.run("workflow-1", "health-api", expected_fingerprint=preview.pipeline_fingerprint, ci_run_id="fixed-run")

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("should not execute again")

    monkeypatch.setattr(service, "_run_command", fail_if_called)
    repeated = await service.run("workflow-1", "health-api", expected_fingerprint=preview.pipeline_fingerprint, ci_run_id="fixed-run")

    assert repeated == run


@pytest.mark.asyncio
async def test_restart_reconciles_running_runs_as_interrupted(tmp_path: Path) -> None:
    make_python_project(tmp_path)
    service = make_service(tmp_path)
    await service.initialize()
    preview = await service.prepare("workflow-1", "health-api")
    await service.store.create_run({
        "ci_run_id": "running-run",
        "workflow_id": "workflow-1",
        "project_id": "health-api",
        "pipeline": preview.pipeline.model_dump(),
        "pipeline_fingerprint": preview.pipeline_fingerprint,
        "status": "running",
        "source": preview.source.model_dump(),
        "started_at": "2026-08-01T00:00:00+00:00",
        "steps": [{"step_id": "test", "name": "Run tests", "type": "test", "status": "running"}],
    })

    restarted = make_service(tmp_path)
    await restarted.initialize()
    run = await restarted.get_run("workflow-1", "running-run")

    assert run is not None
    assert run.status == "interrupted"
    assert run.failure_type == "ci_execution_interrupted"
    assert run.steps[0].status == "interrupted"


class FakeQuery:
    async def get_snapshot(self, thread_id: str) -> WorkflowSnapshotResponse:
        return WorkflowSnapshotResponse(
            thread_id=thread_id,
            checkpoint_id="checkpoint",
            project_name="health-api",
            workflow_intent="create_project",
            terminal_status="completed",
            interrupted=False,
            pending_operation=None,
            pending_tool=None,
            planning={},
            implementation={},
            testing={},
            supervisor={},
            created_at=None,
            updated_at=None,
        )


def app_with_ci(service: CIPipelineService):
    @asynccontextmanager
    async def factory():
        await service.initialize()
        yield SimpleNamespace(query=FakeQuery(), ci=service)

    return create_app(factory)


def test_ci_api_prepare_run_detail_list_and_legacy_status(tmp_path: Path) -> None:
    make_python_project(tmp_path)
    service = make_service(tmp_path)

    with TestClient(app_with_ci(service)) as client:
        status = client.get("/api/workflows/workflow-1/ci")
        assert status.status_code == 200
        assert status.json()["state"] == "not_started"

        preview = client.post("/api/workflows/workflow-1/ci/prepare")
        assert preview.status_code == 200
        fingerprint = preview.json()["pipeline_fingerprint"]

        run = client.post("/api/workflows/workflow-1/ci/run", json={"pipeline_fingerprint": fingerprint})
        assert run.status_code == 200
        run_id = run.json()["ci_run_id"]
        assert run.json()["status"] == "passed"

        detail = client.get(f"/api/workflows/workflow-1/ci/runs/{run_id}")
        assert detail.status_code == 200
        assert detail.json()["ci_run_id"] == run_id

        listing = client.get("/api/workflows/workflow-1/ci/runs")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
