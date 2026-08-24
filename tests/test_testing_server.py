from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from servers import testing_server as ts


@pytest.fixture()
def isolated_workspace(tmp_path: Path):
    original = ts.WORKSPACE_ROOT
    ts.set_workspace_root(tmp_path / "workspace")
    ts.get_workspace_root()
    yield ts.get_workspace_root()
    ts.set_workspace_root(original)


def test_rejects_path_traversal(isolated_workspace: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        ts.resolve_project_path("../outside")


def test_testing_workspace_root_uses_deployment_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "persistent-workspace"
    monkeypatch.setenv("WORKSPACE_ROOT", str(configured))

    assert ts.workspace_root_from_environment() == configured.resolve()


def test_detects_pytest_project(isolated_workspace: Path) -> None:
    project = isolated_workspace / "demo"
    (project / "tests").mkdir(parents=True)

    result = ts.detect_test_framework_impl("demo")

    assert result["framework"] == "pytest"
    assert result["command"] == ts.command_for_framework("pytest", ts.venv_python_path(project))
    assert "tests" in result["command"]
    assert "--disable-warnings" in result["command"]


def test_resolves_venv_python_for_windows_and_posix() -> None:
    project = Path("project")

    assert ts.venv_python_path(project, "win32") == project / ".venv" / "Scripts" / "python.exe"
    assert ts.venv_python_path(project, "linux") == project / ".venv" / "bin" / "python"


def test_resolves_versioned_windows_pip_launcher(tmp_path: Path) -> None:
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pip3.exe").write_text("", encoding="utf-8")

    assert ts.venv_pip_path(tmp_path, "win32") == scripts / "pip3.exe"


def test_rejects_absolute_dependency_file(isolated_workspace: Path) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    with pytest.raises(ValueError, match="absolute"):
        ts.resolve_project_relative_file(project, str((project / "requirements.txt").resolve()))


def test_rejects_dependency_file_traversal(isolated_workspace: Path) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        ts.resolve_project_relative_file(project, "../requirements.txt")


@pytest.mark.asyncio
async def test_prepare_environment_requires_dependency_file(isolated_workspace: Path) -> None:
    (isolated_workspace / "demo").mkdir()

    with pytest.raises(FileNotFoundError, match="dependency_file"):
        await ts.prepare_test_environment_impl("demo")


@pytest.mark.asyncio
async def test_rejects_invalid_framework(isolated_workspace: Path) -> None:
    (isolated_workspace / "demo").mkdir()
    with pytest.raises(ValueError, match="unsupported"):
        await ts.run_tests_impl("demo", "bad")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_rejects_invalid_timeout(isolated_workspace: Path) -> None:
    (isolated_workspace / "demo").mkdir()
    with pytest.raises(ValueError, match="timeout"):
        await ts.run_tests_impl("demo", "pytest", 0)


def test_truncates_output() -> None:
    output = ts.truncate_output("x" * 20_010)

    assert len(output) < 20_050
    assert "[truncated]" in output


def test_build_subprocess_environment_removes_python_state_and_keeps_path() -> None:
    environment = ts.build_subprocess_environment(
        {
            "PYTHONHOME": "home",
            "PYTHONPATH": "path",
            "VIRTUAL_ENV": "venv",
            "PATH": "keep",
            "SYSTEMROOT": "root",
            "TEMP": "temp",
            "TMP": "tmp",
        }
    )

    assert "PYTHONHOME" not in environment
    assert "PYTHONPATH" not in environment
    assert "VIRTUAL_ENV" not in environment
    assert environment["PATH"] == "keep"
    assert environment["SYSTEMROOT"] == "root"
    assert environment["TEMP"] == "temp"
    assert environment["TMP"] == "tmp"


def test_environment_for_project_adds_pytest_state_after_sanitizing(tmp_path: Path) -> None:
    environment = ts.environment_for_project(tmp_path)

    assert environment["PYTHONPATH"] == str(tmp_path)
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_process_creation_options_for_windows_and_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts.os, "name", "nt")
    windows_options = ts.get_process_creation_options()
    assert windows_options == {"creationflags": ts.subprocess.CREATE_NEW_PROCESS_GROUP}
    assert "start_new_session" not in windows_options

    monkeypatch.setattr(ts.os, "name", "posix")
    posix_options = ts.get_process_creation_options()
    assert posix_options == {"start_new_session": True}
    assert "creationflags" not in posix_options


def test_extract_pytest_warning_count_only_when_present() -> None:
    assert ts.extract_pytest_warning_count(". 1 passed, 2 warnings in 0.46s") == 2
    assert ts.extract_pytest_warning_count(". 1 passed, 1 warning in 0.46s") == 1
    assert ts.extract_pytest_warning_count(". 1 passed in 0.46s") is None


def test_parse_installed_versions_from_inspect_stdout() -> None:
    assert ts.parse_installed_versions("fastapi==0.139.0\nstarlette==1.3.1\n") == ("0.139.0", "1.3.1")


def test_diagnostic_script_uses_real_helper() -> None:
    script = Path("scripts/diagnose_venv_runner.py").read_text(encoding="utf-8")

    assert "ts.run_process" in script
    assert "sys.executable, \"-m\", \"venv\", \".venv\", \"--without-pip\"" in script


def process_result(
    command: list[str],
    cwd: Path,
    *,
    exit_code: int | None = 0,
    timed_out: bool = False,
    stdout: str = "ok",
    stderr: str = "",
    duration_seconds: float = 0.01,
    process_tree_terminated: bool = False,
    process_completed: bool = True,
) -> ts.ProcessExecutionResult:
    return ts.ProcessExecutionResult(
        command=command,
        working_directory=str(cwd),
        host_python_executable=sys.executable,
        pid=123,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=duration_seconds,
        stdout=stdout,
        stderr=stderr,
        process_tree_terminated=process_tree_terminated,
        process_completed=process_completed,
    )


@pytest.mark.asyncio
async def test_run_process_uses_single_communicate_and_no_wait(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {"communicate": 0, "wait": 0}

    class FakeProcess:
        pid = 123
        returncode = None

        async def communicate(self):
            calls["communicate"] += 1
            self.returncode = 7
            return "olá".encode(), "err".encode()

        async def wait(self):
            calls["wait"] += 1
            raise AssertionError("wait must not be called")

    async def fake_create_subprocess_exec(*command, **kwargs):
        assert "shell" not in kwargs
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        assert kwargs["stderr"] == asyncio.subprocess.PIPE
        return FakeProcess()

    async def fake_terminate_process_tree(process):
        raise AssertionError("terminate_process_tree must not run on success")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(ts, "terminate_process_tree", fake_terminate_process_tree)

    result = await ts.run_process(["cmd"], cwd=tmp_path, environment={}, timeout_seconds=60)

    assert calls == {"communicate": 1, "wait": 0}
    assert result.exit_code == 7
    assert result.process_completed is True
    assert result.stdout == "olá"
    assert result.stderr == "err"
    assert result.duration_seconds >= 0


@pytest.mark.asyncio
async def test_run_process_timeout_terminates_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {"communicate": 0, "terminated": 0}

    class FakeProcess:
        pid = 123
        returncode = None

        async def communicate(self):
            calls["communicate"] += 1
            if calls["communicate"] == 1:
                raise asyncio.TimeoutError()
            return b"partial", b""

    async def fake_create_subprocess_exec(*command, **kwargs):
        return FakeProcess()

    async def fake_terminate_process_tree(process):
        calls["terminated"] += 1
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(ts, "terminate_process_tree", fake_terminate_process_tree)

    result = await ts.run_process(["cmd"], cwd=tmp_path, environment={}, timeout_seconds=1)

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.process_tree_terminated is True
    assert calls == {"communicate": 2, "terminated": 1}


@pytest.mark.asyncio
async def test_terminate_process_tree_skips_taskkill_when_process_already_done(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProcess:
        returncode = 0
        pid = 123

    async def fake_create_subprocess_exec(*command, **kwargs):
        raise AssertionError("taskkill must not run for an already completed process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    assert await ts.terminate_process_tree(FakeProcess()) is True


@pytest.mark.asyncio
async def test_windows_taskkill_has_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, int] = {}

    class FakeProcess:
        returncode = None
        pid = 123

        def kill(self):
            self.returncode = -9

    class FakeKiller:
        returncode = None

        async def communicate(self):
            return b"", b""

        def kill(self):
            self.returncode = -9

    async def fake_create_subprocess_exec(*command, **kwargs):
        return FakeKiller()

    async def fake_wait_for(awaitable, timeout):
        observed["timeout"] = timeout
        awaitable.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(ts.sys, "platform", "win32")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    assert await ts.terminate_process_tree(FakeProcess()) is True
    assert observed["timeout"] == 10


@pytest.mark.asyncio
async def test_simulated_eight_second_process_does_not_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ticks = iter([100.0, 108.0])

    class FakeProcess:
        pid = 123
        returncode = 0

        async def communicate(self):
            return b"done", b""

    async def fake_create_subprocess_exec(*command, **kwargs):
        return FakeProcess()

    async def fake_wait_for(awaitable, timeout):
        return await awaitable

    class FakeTime:
        def monotonic(self):
            return next(ticks)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(ts, "time", FakeTime())

    result = await ts.run_process(["cmd"], cwd=tmp_path, environment={}, timeout_seconds=60)

    assert result.timed_out is False
    assert result.exit_code == 0
    assert result.duration_seconds == 8


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_create_venv_without_pip_finishes_under_timeout(tmp_path: Path) -> None:
    result = await ts.run_process(
        [sys.executable, "-m", "venv", ".venv", "--without-pip"],
        cwd=tmp_path,
        environment=ts.environment_for_project(tmp_path),
        timeout_seconds=60,
    )

    assert result.timed_out is False
    assert result.exit_code == 0
    assert ts.venv_python_path(tmp_path).is_file()
    assert result.duration_seconds < 60


@pytest.mark.asyncio
async def test_run_tests_uses_closed_commands_and_no_shell(isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (isolated_workspace / "demo").mkdir()
    venv_python = ts.venv_python_path(isolated_workspace / "demo")
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    captured: dict = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

        def kill(self):
            raise AssertionError("kill should not be called")

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await ts.run_tests_impl("demo", "pytest", 5)

    assert result["success"] is True
    assert captured["command"] == ts.command_for_framework("pytest", venv_python)
    assert captured["command"][0] == str(venv_python)
    assert captured["command"][0] != sys.executable
    assert captured["kwargs"]["cwd"] == isolated_workspace / "demo"
    assert captured["kwargs"]["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert captured["kwargs"]["env"]["PYTHONPATH"] == str(isolated_workspace / "demo")
    assert captured["kwargs"]["env"]["PYTHONUNBUFFERED"] == "1"
    assert captured["kwargs"]["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "shell" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_run_tests_records_warning_count_without_failing(isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    venv_python = ts.venv_python_path(project)
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    async def fake_run_process(command, *, cwd, environment, timeout_seconds):
        return process_result(
            command=list(command),
            cwd=cwd,
            exit_code=0,
            stdout=". 1 passed, 2 warnings in 0.46s",
        )

    monkeypatch.setattr(ts, "run_process", fake_run_process)

    result = await ts.run_tests_impl("demo", "pytest", 30)

    assert result["success"] is True
    assert result["test_warning_count"] == 2
    assert result["command"][0] == str(venv_python)


@pytest.mark.asyncio
async def test_timeout_returns_none_exit_code_and_terminates_process_tree(
    isolated_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (isolated_workspace / "demo").mkdir()
    venv_python = ts.venv_python_path(isolated_workspace / "demo")
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    captured: dict = {"terminated": False}

    class FakeProcess:
        pid = 1234
        returncode = None

        def __init__(self) -> None:
            self.calls = 0

        async def communicate(self):
            self.calls += 1
            if self.calls == 1:
                raise asyncio.TimeoutError()
            return b"partial", b""

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        return FakeProcess()

    async def fake_terminate_process_tree(process):
        captured["terminated"] = True
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(ts, "terminate_process_tree", fake_terminate_process_tree)

    result = await ts.run_tests_impl("demo", "pytest", 1)

    assert result["success"] is False
    assert result["exit_code"] is None
    assert result["timed_out"] is True
    assert result["failure_type"] == "timeout"
    assert result["process_tree_terminated"] is True
    assert "supero el timeout de 1 segundos" in result["stderr"]
    assert captured["terminated"] is True
    assert "shell" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_exit_code_one_without_timeout_is_test_failure(isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (isolated_workspace / "demo").mkdir()
    venv_python = ts.venv_python_path(isolated_workspace / "demo")
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    class FakeProcess:
        returncode = 1

        async def communicate(self):
            return b"failed", b"traceback"

    async def fake_create_subprocess_exec(*command, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await ts.run_tests_impl("demo", "pytest", 5)

    assert result["success"] is False
    assert result["exit_code"] == 1
    assert result["timed_out"] is False
    assert result["failure_type"] == "test_failure"


@pytest.mark.asyncio
async def test_run_tests_without_venv_returns_environment_not_prepared(isolated_workspace: Path) -> None:
    (isolated_workspace / "demo").mkdir()

    result = await ts.run_tests_impl("demo", "pytest", 5)

    assert result["success"] is False
    assert result["failure_type"] == "environment_not_prepared"
    assert "prepare_test_environment" in result["message"]


@pytest.mark.asyncio
async def test_prepare_environment_public_schema_does_not_expose_technical_timeouts() -> None:
    tools = await ts.mcp.list_tools()
    prepare_tool = next(tool for tool in tools if tool.name == "prepare_test_environment")
    properties = prepare_tool.inputSchema["properties"]

    assert set(properties) == {"project_name", "dependency_file"}
    assert "project_name" in prepare_tool.inputSchema["required"]


@pytest.mark.asyncio
async def test_prepare_environment_creates_venv_and_installs_requirements(
    isolated_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    (project / "requirements.txt").write_text("pytest>=8,<9", encoding="utf-8")
    captured: list[list[str]] = []

    async def fake_run_process(command, *, cwd, environment, timeout_seconds):
        captured.append(list(command))
        if command[:3] == [sys.executable, "-m", "venv"]:
            venv_python = ts.venv_python_path(cwd)
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")
        return {
            "command": command,
            "exit_code": 0,
            "timed_out": False,
            "process_tree_terminated": False,
            "duration_seconds": 0.01,
            "stdout": "ok",
            "stderr": "",
        }

    monkeypatch.setattr(ts, "run_process", fake_run_process)

    result = await ts.prepare_test_environment_impl("demo")

    assert result["success"] is True
    assert result["venv_created"] is True
    assert result["pip_upgraded"] is True
    assert result["dependencies_installed"] is True
    assert captured[0] == [sys.executable, "-c", "import sys; print(sys.executable)"]
    assert captured[1] == [sys.executable, "-m", "venv", ".venv", "--without-pip"]
    assert captured[2][1:4] == ["-m", "ensurepip", "--upgrade"]
    assert captured[3][1:5] == ["-m", "pip", "install", "--upgrade"]
    assert captured[4][1:5] == ["-m", "pip", "install", "-r"]
    assert str(project / "requirements.txt") in captured[4]


@pytest.mark.asyncio
async def test_prepare_environment_tool_logs_summary_without_pip_noise(
    isolated_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    (project / "requirements.txt").write_text("pytest>=8,<9", encoding="utf-8")

    async def fake_run_process(command, *, cwd, environment, timeout_seconds):
        if command[:3] == [sys.executable, "-m", "venv"]:
            venv_python = ts.venv_python_path(cwd)
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")
        stdout = "Downloading lots of pip output" if command[1:5] == ["-m", "pip", "install", "-r"] else ""
        if command[1:2] == ["-c"] and "fastapi" in command[2]:
            stdout = "fastapi==0.139.0\nstarlette==1.3.1\n"
        return {
            "command": command,
            "exit_code": 0,
            "timed_out": False,
            "process_tree_terminated": False,
            "process_completed": True,
            "duration_seconds": 0.01,
            "stdout": stdout,
            "stderr": "",
        }

    monkeypatch.setenv("MCP_FACTORY_DEBUG", "false")
    monkeypatch.setattr(ts, "run_process", fake_run_process)

    result = await ts.prepare_test_environment("demo")

    out = capsys.readouterr().out
    assert result["success"] is True
    assert "Downloading lots of pip output" in result["stdout"]
    assert "stage: validate_dependencies -> success" in out
    assert "stage: create_venv -> success" in out
    assert "stage: bootstrap_pip -> success" in out
    assert "stage: upgrade_pip -> success" in out
    assert "stage: install_dependencies -> success" in out
    assert "stage: pip_check -> success" in out
    assert "stage: inspect_versions -> success" in out
    assert "fastapi_version: 0.139.0" in out
    assert "starlette_version: 1.3.1" in out
    assert "Downloading lots of pip output" not in out


@pytest.mark.asyncio
async def test_prepare_environment_reuses_existing_venv(isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    (project / "requirements.txt").write_text("pytest>=8,<9", encoding="utf-8")
    venv_python = ts.venv_python_path(project)
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    captured: list[list[str]] = []

    async def fake_run_process(command, *, cwd, environment, timeout_seconds):
        captured.append(list(command))
        return {
            "command": command,
            "exit_code": 0,
            "timed_out": False,
            "process_tree_terminated": False,
            "duration_seconds": 0.01,
            "stdout": "ok",
            "stderr": "",
        }

    monkeypatch.setattr(ts, "run_process", fake_run_process)

    result = await ts.prepare_test_environment_impl("demo")

    assert result["success"] is True
    assert result["venv_created"] is False
    assert len(captured) == 7
    assert captured[0] == [sys.executable, "-c", "import sys; print(sys.executable)"]
    assert captured[1][1:3] == ["-c", "import sys; print(sys.executable)"]
    assert captured[2][1:4] == ["-m", "pip", "--version"]
    assert all(command[:3] != [sys.executable, "-m", "venv"] for command in captured)
    assert [stage["name"] for stage in result["stages"]] == [
        "validate_dependencies",
        "validate_host_python",
        "create_venv",
        "bootstrap_pip",
        "upgrade_pip",
        "install_dependencies",
        "pip_check",
        "inspect_versions",
    ]


@pytest.mark.asyncio
async def test_prepare_environment_timeout_during_venv_stops_flow(isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    (project / "requirements.txt").write_text("pytest>=8,<9", encoding="utf-8")
    calls = 0

    async def fake_run_process(command, *, cwd, environment, timeout_seconds):
        nonlocal calls
        calls += 1
        timed_out = calls == 2
        return {
            "command": command,
            "working_directory": str(cwd),
            "host_python_executable": sys.executable,
            "pid": 123,
            "exit_code": None if timed_out else 0,
            "timed_out": timed_out,
            "process_tree_terminated": timed_out,
            "process_completed": not timed_out,
            "duration_seconds": timeout_seconds,
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr(ts, "run_process", fake_run_process)

    result = await ts.prepare_test_environment_impl("demo", timeout_seconds=1)

    assert result["success"] is False
    assert result["failure_stage"] == "create_venv"
    assert result["failure_type"] == "timeout"
    assert result["timed_out"] is True
    assert calls == 2


@pytest.mark.asyncio
async def test_prepare_environment_rejects_invalid_fastapi_requirement_before_venv(
    isolated_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    (project / "requirements.txt").write_text("fastapi[standard]>=0.139.2,<0.140\npytest>=8,<9\n", encoding="utf-8")
    calls = 0

    async def fake_run_process(command, *, cwd, environment, timeout_seconds):
        nonlocal calls
        calls += 1
        return {}

    monkeypatch.setattr(ts, "run_process", fake_run_process)

    result = await ts.prepare_test_environment_impl("demo")

    assert result["success"] is False
    assert result["failure_stage"] == "validate_dependencies"
    assert result["failure_type"] == "dependency_policy_violation"
    assert result["timed_out"] is False
    assert not (project / ".venv").exists()
    assert calls == 0


@pytest.mark.asyncio
async def test_prepare_environment_uses_independent_install_timeout(isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    (project / "requirements.txt").write_text("pytest>=8,<9", encoding="utf-8")
    install_timeout_seen: list[int] = []

    async def fake_run_process(command, *, cwd, environment, timeout_seconds):
        if command[:3] == [sys.executable, "-m", "venv"]:
            venv_python = ts.venv_python_path(cwd)
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")
        if command[1:5] == ["-m", "pip", "install", "-r"]:
            install_timeout_seen.append(timeout_seconds)
        return {
            "command": command,
            "exit_code": 0,
            "timed_out": False,
            "process_tree_terminated": False,
            "duration_seconds": 0.01,
            "stdout": "ok",
            "stderr": "",
        }

    monkeypatch.setattr(ts, "run_process", fake_run_process)

    result = await ts.prepare_test_environment_impl("demo", dependency_install_timeout_seconds=321)

    assert result["success"] is True
    assert install_timeout_seen == [321]


@pytest.mark.asyncio
async def test_prepare_environment_detects_partial_venv_without_python(isolated_workspace: Path) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    (project / "requirements.txt").write_text("pytest>=8,<9", encoding="utf-8")
    (project / ".venv").mkdir()

    result = await ts.prepare_test_environment_impl("demo")

    assert result["success"] is False
    assert result["environment_incomplete"] is True
    assert result["failure_type"] == "invalid_existing_environment"
    assert result["failure_stage"] == "invalid_existing_environment"


@pytest.mark.asyncio
async def test_prepare_environment_rejects_invalid_existing_python(isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    (project / "requirements.txt").write_text("pytest>=8,<9", encoding="utf-8")
    venv_python = ts.venv_python_path(project)
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    async def fake_run_process(command, *, cwd, environment, timeout_seconds):
        calls.append(list(command))
        exit_code = 0 if command[0] == sys.executable else 1
        return {
            "command": command,
            "exit_code": exit_code,
            "timed_out": False,
            "process_tree_terminated": False,
            "duration_seconds": 0.01,
            "stdout": "",
            "stderr": "invalid python",
        }

    monkeypatch.setattr(ts, "run_process", fake_run_process)

    result = await ts.prepare_test_environment_impl("demo")

    assert result["success"] is False
    assert result["failure_type"] == "invalid_existing_environment"
    assert calls == [
        [sys.executable, "-c", "import sys; print(sys.executable)"],
        [str(venv_python), "-c", "import sys; print(sys.executable)"],
    ]


@pytest.mark.asyncio
async def test_prepare_environment_pip_failure_stops_before_install(isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = isolated_workspace / "demo"
    project.mkdir()
    (project / "requirements.txt").write_text("pytest>=8,<9", encoding="utf-8")
    calls: list[list[str]] = []

    async def fake_run_process(command, *, cwd, environment, timeout_seconds):
        calls.append(list(command))
        if command == [sys.executable, "-c", "import sys; print(sys.executable)"]:
            exit_code = 0
        elif command[:3] == [sys.executable, "-m", "venv"]:
            venv_python = ts.venv_python_path(cwd)
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")
            exit_code = 0
        elif command[1:4] == ["-m", "ensurepip", "--upgrade"]:
            exit_code = 0
        else:
            exit_code = 1
        return {
            "command": command,
            "exit_code": exit_code,
            "timed_out": False,
            "process_tree_terminated": False,
            "duration_seconds": 0.01,
            "stdout": "",
            "stderr": "pip failed",
        }

    monkeypatch.setattr(ts, "run_process", fake_run_process)

    result = await ts.prepare_test_environment_impl("demo")

    assert result["success"] is False
    assert result["failure_stage"] == "upgrade_pip"
    assert len(calls) == 4
