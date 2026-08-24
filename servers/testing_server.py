from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from mcp.server.fastmcp import FastMCP

from policies.dependencies import (
    BLOCKED_FASTAPI_REQUIREMENTS,
    DEFAULT_FASTAPI_REQUIREMENT,
    DEFAULT_PYTEST_REQUIREMENT,
)


mcp = FastMCP("software-factory-testing")

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE_ROOT = REPOSITORY_ROOT / "workspace"
ALLOWED_FRAMEWORKS = {"pytest", "npm", "dotnet", "auto"}
MAX_OUTPUT_CHARS = 20_000
TOTAL_TIMEOUT_SECONDS = 600
DEFAULT_VENV_TIMEOUT_SECONDS = 60
DEFAULT_PIP_UPGRADE_TIMEOUT_SECONDS = 120
DEFAULT_DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 300
DEFAULT_PIP_CHECK_TIMEOUT_SECONDS = 60
DEFAULT_INSPECT_VERSIONS_TIMEOUT_SECONDS = 30
MAX_PREPARE_TOTAL_TIMEOUT_SECONDS = 900
DEBUG_ENV_VAR = "MCP_FACTORY_DEBUG"


def workspace_root_from_environment() -> Path:
    configured = os.getenv("WORKSPACE_ROOT", "").strip()
    if not configured:
        return DEFAULT_WORKSPACE_ROOT.resolve()
    candidate = Path(configured).expanduser()
    return (
        candidate.resolve()
        if candidate.is_absolute()
        else (REPOSITORY_ROOT / candidate).resolve()
    )


WORKSPACE_ROOT = workspace_root_from_environment()


def debug_logging_enabled() -> bool:
    return os.getenv(DEBUG_ENV_VAR, "false").strip().lower() in {"1", "true", "yes", "y", "si", "sí"}


def debug_log(*values: Any) -> None:
    if debug_logging_enabled():
        print(*values)


def normal_log(*values: Any) -> None:
    print(*values)


@dataclass(frozen=True)
class ProcessExecutionResult:
    command: list[str]
    working_directory: str
    host_python_executable: str
    pid: int | None
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    stdout: str
    stderr: str
    process_tree_terminated: bool
    process_completed: bool


def set_workspace_root(path: Path) -> None:
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = path.resolve()


def get_workspace_root() -> Path:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_ROOT.resolve()


def resolve_project_path(project_name: str) -> Path:
    if not project_name or not project_name.strip():
        raise ValueError("project_name must not be empty")
    candidate = Path(project_name)
    if candidate.is_absolute():
        raise ValueError("absolute project paths are not allowed")
    root = get_workspace_root()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("project path escapes the workspace") from exc
    if not resolved.is_dir():
        raise FileNotFoundError(f"project does not exist: {project_name}")
    return resolved


def venv_python_path(project: Path, platform: str | None = None) -> Path:
    platform_name = platform or sys.platform
    if platform_name == "win32":
        return project / ".venv" / "Scripts" / "python.exe"
    return project / ".venv" / "bin" / "python"


def venv_pip_path(project: Path, platform: str | None = None) -> Path:
    platform_name = platform or sys.platform
    if platform_name == "win32":
        scripts_dir = project / ".venv" / "Scripts"
        for name in ("pip.exe", "pip3.exe"):
            candidate = scripts_dir / name
            if candidate.is_file():
                return candidate
        versioned = sorted(scripts_dir.glob("pip*.exe"))
        if versioned:
            return versioned[0]
        return scripts_dir / "pip.exe"
    return project / ".venv" / "bin" / "pip"


def resolve_project_relative_file(project: Path, relative_path: str) -> Path:
    if relative_path is None or not str(relative_path).strip():
        raise ValueError("dependency_file must not be empty")
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError("absolute dependency_file paths are not allowed")
    resolved = (project / candidate).resolve()
    try:
        resolved.relative_to(project.resolve())
    except ValueError as exc:
        raise ValueError("dependency_file escapes the project") from exc
    return resolved


def command_for_framework(framework: str, project_python: Path | None = None) -> list[str]:
    if framework == "pytest":
        python = str(project_python) if project_python is not None else sys.executable
        return [python, "-m", "pytest", "tests", "-q", "--disable-warnings"]
    if framework == "npm":
        return ["npm", "test", "--", "--runInBand"]
    if framework == "dotnet":
        return ["dotnet", "test", "--nologo", "--verbosity", "minimal"]
    raise ValueError(f"unsupported framework: {framework}")


def detect_test_framework_impl(project_name: str) -> dict[str, Any]:
    project = resolve_project_path(project_name)
    framework = "pytest"
    if (project / "package.json").exists():
        framework = "npm"
    elif any(project.glob("*.sln")) or any(project.rglob("*.csproj")):
        framework = "dotnet"
    elif (project / "pytest.ini").exists() or (project / "pyproject.toml").exists() or (project / "tests").is_dir():
        framework = "pytest"
    return {
        "framework": framework,
        "command": command_for_framework(framework, venv_python_path(project) if framework == "pytest" else None),
        "project_path": str(project),
    }


def truncate_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def preview_output(text: str, limit: int = 1_000) -> str:
    return truncate_output(text, limit)


def stage_result(name: str, success: bool, exit_code: int | None, duration_seconds: float, stdout: str = "", stderr: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "success": success,
        "exit_code": exit_code,
        "duration_seconds": round(duration_seconds, 3),
        "stdout_preview": preview_output(stdout),
        "stderr_preview": preview_output(stderr),
    }


def process_result_dict(result: ProcessExecutionResult | dict[str, Any]) -> dict[str, Any]:
    return asdict(result) if isinstance(result, ProcessExecutionResult) else result


def stage_from_process(name: str, result: ProcessExecutionResult | dict[str, Any]) -> dict[str, Any]:
    data = process_result_dict(result)
    stage = stage_result(
        name=name,
        success=data.get("exit_code") == 0 and data.get("timed_out") is False and data.get("process_completed") is not False,
        exit_code=data.get("exit_code"),
        duration_seconds=float(data.get("duration_seconds") or 0),
        stdout=str(data.get("stdout") or ""),
        stderr=str(data.get("stderr") or ""),
    )
    for key in ("command", "working_directory", "host_python_executable", "pid", "process_completed"):
        if key in data:
            stage[key] = data[key]
    return stage


def requirement_lines(content: str) -> list[str]:
    lines: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def validate_dependency_policy(requirements_content: str) -> dict[str, Any] | None:
    lines = requirement_lines(requirements_content)
    lower_lines = {line.lower() for line in lines}
    fastapi_lines = [line for line in lines if line.lower().startswith("fastapi")]
    if not fastapi_lines:
        return None
    expected_fastapi = DEFAULT_FASTAPI_REQUIREMENT.lower()
    expected_pytest = DEFAULT_PYTEST_REQUIREMENT.lower()
    received = fastapi_lines[0]
    if expected_fastapi in lower_lines and expected_pytest in lower_lines:
        return None
    if received.lower() in BLOCKED_FASTAPI_REQUIREMENTS or received != DEFAULT_FASTAPI_REQUIREMENT:
        return {
            "success": False,
            "failure_stage": "validate_dependencies",
            "failure_type": "dependency_policy_violation",
            "expected": DEFAULT_FASTAPI_REQUIREMENT,
            "received": received,
            "timed_out": False,
            "message": "El archivo requirements.txt no cumple la política de dependencias.",
        }
    return {
        "success": False,
        "failure_stage": "validate_dependencies",
        "failure_type": "dependency_policy_violation",
        "expected": DEFAULT_PYTEST_REQUIREMENT,
        "received": "",
        "timed_out": False,
        "message": "El archivo requirements.txt no cumple la política de dependencias.",
    }


def build_subprocess_environment(base_environment: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(base_environment or os.environ)
    removed = {name: name in environment for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")}
    for name in removed:
        environment.pop(name, None)
    debug_log(
        "Subprocess Python environment sanitized:",
        {name: "removed" if present else "absent" for name, present in removed.items()},
    )
    return environment


def environment_for_project(project: Path) -> dict[str, str]:
    environment = build_subprocess_environment()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONPATH"] = str(project)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def get_process_creation_options() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def subprocess_options() -> dict[str, Any]:
    return get_process_creation_options()


async def terminate_process_tree(process: asyncio.subprocess.Process) -> bool:
    if process.returncode is not None:
        return True
    if process.pid is None:
        process.kill()
        return False
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(killer.communicate(), timeout=10)
                debug_log(
                    "taskkill result:",
                    {
                        "exit_code": killer.returncode,
                        "stdout_preview": preview_output(stdout_bytes.decode("utf-8", errors="replace")),
                        "stderr_preview": preview_output(stderr_bytes.decode("utf-8", errors="replace")),
                    },
                )
            except asyncio.TimeoutError:
                killer.kill()
                debug_log("taskkill timed out after 10 seconds")
        else:
            os.killpg(process.pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True
    except Exception:
        try:
            process.kill()
        except ProcessLookupError:
            return True
        return False


def classify_failure(exit_code: int | None, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if exit_code == 0:
        return "none"
    if exit_code is None:
        return "execution_error"
    return "test_failure"


async def run_process(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
) -> ProcessExecutionResult:
    debug_log("Stage subprocess:")
    debug_log(f"- command: {' '.join(command)}")
    debug_log(f"- cwd: {cwd}")
    debug_log(f"- timeout_seconds: {timeout_seconds}")
    debug_log("- stdin: DEVNULL")
    debug_log(f"- host_python_executable: {sys.executable}")
    debug_log(f"- creation_options: {get_process_creation_options()}")
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **get_process_creation_options(),
    )
    timed_out = False
    process_tree_terminated = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        timed_out = True
        process_tree_terminated = await terminate_process_tree(process)
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=5)
        except asyncio.TimeoutError:
            stdout_bytes, stderr_bytes = b"", b""
    duration = time.monotonic() - started
    process_completed = process.returncode is not None
    exit_code = None if timed_out else process.returncode
    stdout = truncate_output(stdout_bytes.decode("utf-8", errors="replace"))
    stderr = truncate_output(stderr_bytes.decode("utf-8", errors="replace"))
    if not timed_out and not process_completed:
        stderr = append_output(stderr, "El proceso finalizó communicate() sin returncode disponible.")
    debug_log("Stage subprocess result:")
    debug_log(f"- pid: {getattr(process, 'pid', None)}")
    debug_log(f"- exit_code: {exit_code}")
    debug_log(f"- timed_out: {str(timed_out).lower()}")
    debug_log(f"- duration_seconds: {round(duration, 3)}")
    debug_log(f"- stdout_preview: {preview_output(stdout)}")
    debug_log(f"- stderr_preview: {preview_output(stderr)}")
    return ProcessExecutionResult(
        command=command,
        working_directory=str(cwd),
        host_python_executable=sys.executable,
        pid=getattr(process, "pid", None),
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=round(duration, 3),
        stdout=stdout,
        stderr=stderr,
        process_tree_terminated=process_tree_terminated,
        process_completed=process_completed,
    )


def append_output(existing: str, addition: str) -> str:
    if not addition:
        return existing
    return f"{existing}\n{addition}".strip() if existing else addition


def detect_environment_after_failure(project: Path) -> dict[str, Any]:
    venv_dir = project / ".venv"
    python_executable = venv_python_path(project)
    pip_executable = venv_pip_path(project)
    return {
        "venv_exists": venv_dir.exists(),
        "python_executable_exists": python_executable.is_file(),
        "pip_executable_exists": pip_executable.is_file(),
    }


async def detect_environment_after_failure_with_retries(project: Path, retries: int = 4, delay_seconds: float = 0.25) -> dict[str, Any]:
    detected = detect_environment_after_failure(project)
    for _ in range(retries):
        if detected["venv_exists"] and detected["python_executable_exists"]:
            return detected
        await asyncio.sleep(delay_seconds)
        detected = detect_environment_after_failure(project)
    return detected


def environment_incomplete_from_detection(detected: dict[str, Any]) -> bool:
    if not detected["venv_exists"]:
        return False
    return not (detected["python_executable_exists"] and detected["pip_executable_exists"])


def process_metadata(result: ProcessExecutionResult | dict[str, Any]) -> dict[str, Any]:
    data = process_result_dict(result)
    return {
        "command": data.get("command"),
        "working_directory": data.get("working_directory"),
        "host_python_executable": data.get("host_python_executable"),
        "pid": data.get("pid"),
        "process_completed": data.get("process_completed"),
    }


def parse_installed_versions(stdout: str) -> tuple[str | None, str | None]:
    versions: dict[str, str] = {}
    for line in stdout.splitlines():
        name, separator, version = line.strip().partition("==")
        if separator and name in {"fastapi", "starlette"} and version:
            versions[name] = version.strip()
    return versions.get("fastapi"), versions.get("starlette")


def extract_pytest_warning_count(stdout: str) -> int | None:
    matches = re.findall(r"\b(\d+)\s+warnings?\b", stdout)
    if not matches:
        return None
    return int(matches[-1])


def log_prepare_summary(result: dict[str, Any]) -> None:
    if debug_logging_enabled():
        return
    for stage in result.get("stages", []):
        name = stage.get("name")
        if name == "validate_host_python":
            continue
        success = "success" if stage.get("success") is True else "failure"
        normal_log(f"stage: {name} -> {success}")
    if "duration_seconds" in result:
        normal_log(f"duration_seconds: {result.get('duration_seconds')}")
    if "python_executable" in result:
        normal_log(f"python_executable: {result.get('python_executable')}")
    if "installed_fastapi_version" in result:
        normal_log(f"fastapi_version: {result.get('installed_fastapi_version')}")
    if "installed_starlette_version" in result:
        normal_log(f"starlette_version: {result.get('installed_starlette_version')}")


async def prepare_test_environment_impl(
    project_name: str,
    dependency_file: str = "requirements.txt",
    timeout_seconds: int | None = None,
    venv_timeout_seconds: int = DEFAULT_VENV_TIMEOUT_SECONDS,
    pip_upgrade_timeout_seconds: int = DEFAULT_PIP_UPGRADE_TIMEOUT_SECONDS,
    dependency_install_timeout_seconds: int = DEFAULT_DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
    pip_check_timeout_seconds: int = DEFAULT_PIP_CHECK_TIMEOUT_SECONDS,
    inspect_versions_timeout_seconds: int = DEFAULT_INSPECT_VERSIONS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if timeout_seconds is not None:
        if timeout_seconds <= 0 or timeout_seconds > MAX_PREPARE_TOTAL_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds must be between 1 and {MAX_PREPARE_TOTAL_TIMEOUT_SECONDS}")
        venv_timeout_seconds = timeout_seconds
        pip_upgrade_timeout_seconds = timeout_seconds
        dependency_install_timeout_seconds = timeout_seconds
    stage_timeouts = {
        "venv_timeout_seconds": venv_timeout_seconds,
        "pip_upgrade_timeout_seconds": pip_upgrade_timeout_seconds,
        "dependency_install_timeout_seconds": dependency_install_timeout_seconds,
        "pip_check_timeout_seconds": pip_check_timeout_seconds,
        "inspect_versions_timeout_seconds": inspect_versions_timeout_seconds,
    }
    for name, value in stage_timeouts.items():
        if value <= 0 or value > MAX_PREPARE_TOTAL_TIMEOUT_SECONDS:
            raise ValueError(f"{name} must be between 1 and {MAX_PREPARE_TOTAL_TIMEOUT_SECONDS}")

    project = resolve_project_path(project_name)
    dependencies = resolve_project_relative_file(project, dependency_file)
    if not dependencies.is_file():
        raise FileNotFoundError(f"dependency_file does not exist: {dependency_file}")

    venv_dir = project / ".venv"
    python_executable = venv_python_path(project)
    stdout = ""
    stderr = ""
    started = time.monotonic()
    stages: list[dict[str, Any]] = []
    venv_created = False
    pip_upgraded = False
    dependencies_installed = False
    last_exit_code: int | None = 0
    timed_out = False
    process_tree_terminated = False

    dependency_policy_error = validate_dependency_policy(dependencies.read_text(encoding="utf-8"))
    stages.append(stage_result("validate_dependencies", dependency_policy_error is None, 0 if dependency_policy_error is None else 1, 0))
    if dependency_policy_error is not None:
        return {
            **dependency_policy_error,
            "project_name": project_name,
            "environment_path": str(venv_dir),
            "python_executable": str(python_executable),
            "dependency_file": dependency_file,
            "venv_created": False,
            "pip_upgraded": False,
            "dependencies_installed": False,
            "environment_incomplete": False,
            "exit_code": 1,
            "process_tree_terminated": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": "",
            "stderr": dependency_policy_error["message"],
            "stages": stages,
        }

    host_environment = build_subprocess_environment()
    validate_host_python_result = await run_process(
        [sys.executable, "-c", "import sys; print(sys.executable)"],
        cwd=project,
        environment=host_environment,
        timeout_seconds=venv_timeout_seconds,
    )
    validate_host_python_data = process_result_dict(validate_host_python_result)
    stages.append(stage_from_process("validate_host_python", validate_host_python_result))
    stdout = append_output(stdout, validate_host_python_data["stdout"])
    stderr = append_output(stderr, validate_host_python_data["stderr"])
    if validate_host_python_data["timed_out"] or validate_host_python_data["exit_code"] != 0:
        return {
            "success": False,
            "project_name": project_name,
            "environment_path": str(venv_dir),
            "python_executable": str(python_executable),
            "dependency_file": dependency_file,
            "venv_created": False,
            "pip_upgraded": False,
            "dependencies_installed": False,
            "environment_incomplete": False,
            "environment_detected_after_failure": detect_environment_after_failure(project),
            "failure_type": "invalid_host_python",
            "exit_code": validate_host_python_data["exit_code"],
            "timed_out": validate_host_python_data["timed_out"],
            "process_tree_terminated": validate_host_python_data["process_tree_terminated"],
            "failure_stage": "validate_host_python",
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": stdout,
            "stderr": stderr,
            "stages": stages,
        }

    if venv_dir.exists():
        if not python_executable.is_file():
            stages.append(stage_result("create_venv", False, None, 0, "", "El entorno .venv existe pero no contiene un ejecutable Python valido."))
            return {
                "success": False,
                "project_name": project_name,
                "environment_path": str(venv_dir),
                "python_executable": str(python_executable),
                "dependency_file": dependency_file,
                "venv_created": False,
                "pip_upgraded": False,
                "dependencies_installed": False,
                "environment_incomplete": True,
                "failure_type": "invalid_existing_environment",
                "exit_code": None,
                "timed_out": False,
                "process_tree_terminated": False,
                "failure_stage": "invalid_existing_environment",
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": "",
                "stderr": "El entorno .venv existe pero no contiene un ejecutable Python valido.",
                "message": "El entorno .venv existente es invalido.",
                "stages": stages,
            }
        check_existing = await run_process(
            [str(python_executable), "-c", "import sys; print(sys.executable)"],
            cwd=project,
            environment=environment_for_project(project),
            timeout_seconds=venv_timeout_seconds,
        )
        check_existing_data = process_result_dict(check_existing)
        stages.append(stage_from_process("create_venv", check_existing))
        if check_existing_data["timed_out"] or check_existing_data["exit_code"] != 0:
            return {
                "success": False,
                "project_name": project_name,
                "environment_path": str(venv_dir),
                "python_executable": str(python_executable),
                "dependency_file": dependency_file,
                "venv_created": False,
                "pip_upgraded": False,
                "dependencies_installed": False,
                "environment_incomplete": True,
                "failure_type": "invalid_existing_environment",
                "exit_code": check_existing_data["exit_code"],
                "timed_out": check_existing_data["timed_out"],
                "process_tree_terminated": check_existing_data["process_tree_terminated"],
                "failure_stage": "invalid_existing_environment",
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": check_existing_data["stdout"],
                "stderr": check_existing_data["stderr"],
                "message": "El entorno .venv existente es invalido.",
                "stages": stages,
            }
        pip_existing = await run_process(
            [str(python_executable), "-m", "pip", "--version"],
            cwd=project,
            environment=environment_for_project(project),
            timeout_seconds=venv_timeout_seconds,
        )
        pip_existing_data = process_result_dict(pip_existing)
        stages.append(stage_from_process("bootstrap_pip", pip_existing))
        if pip_existing_data["timed_out"] or pip_existing_data["exit_code"] != 0:
            return {
                "success": False,
                "project_name": project_name,
                "environment_path": str(venv_dir),
                "python_executable": str(python_executable),
                "dependency_file": dependency_file,
                "venv_created": False,
                "pip_upgraded": False,
                "dependencies_installed": False,
                "environment_incomplete": True,
                "failure_type": "invalid_existing_environment",
                "exit_code": check_existing_data["exit_code"] if check_existing_data["exit_code"] != 0 else pip_existing_data["exit_code"],
                "timed_out": check_existing_data["timed_out"] or pip_existing_data["timed_out"],
                "process_tree_terminated": check_existing_data["process_tree_terminated"] or pip_existing_data["process_tree_terminated"],
                "failure_stage": "invalid_existing_environment",
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": append_output(check_existing_data["stdout"], pip_existing_data["stdout"]),
                "stderr": append_output(check_existing_data["stderr"], pip_existing_data["stderr"]),
                "message": "El entorno .venv existente es invalido.",
                "stages": stages,
            }
    else:
        create_result = await run_process(
            [sys.executable, "-m", "venv", ".venv", "--without-pip"],
            cwd=project,
            environment=host_environment,
            timeout_seconds=venv_timeout_seconds,
        )
        create_data = process_result_dict(create_result)
        stages.append(stage_from_process("create_venv", create_result))
        stdout = append_output(stdout, create_data["stdout"])
        stderr = append_output(stderr, create_data["stderr"])
        last_exit_code = create_data["exit_code"]
        timed_out = create_data["timed_out"]
        process_tree_terminated = process_tree_terminated or create_data["process_tree_terminated"]
        if timed_out or last_exit_code != 0:
            if timed_out:
                stderr = append_output(stderr, f"La creacion del entorno supero el timeout de {venv_timeout_seconds} segundos.")
            detected_after_failure = await detect_environment_after_failure_with_retries(project)
            return {
                "success": False,
                **process_metadata(create_result),
                "project_name": project_name,
                "environment_path": str(venv_dir),
                "python_executable": str(python_executable),
                "dependency_file": dependency_file,
                "venv_created": False,
                "pip_upgraded": False,
                "dependencies_installed": False,
                "environment_incomplete": environment_incomplete_from_detection(detected_after_failure),
                "environment_detected_after_failure": detected_after_failure,
                "failure_type": "timeout" if timed_out else "create_venv_failure",
                "exit_code": last_exit_code,
                "timed_out": timed_out,
                "process_tree_terminated": process_tree_terminated,
                "failure_stage": "create_venv",
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": stdout,
                "stderr": stderr,
                "stages": stages,
            }
        venv_created = True

        bootstrap_result = await run_process(
            [str(python_executable), "-m", "ensurepip", "--upgrade"],
            cwd=project,
            environment=environment_for_project(project),
            timeout_seconds=venv_timeout_seconds,
        )
        bootstrap_data = process_result_dict(bootstrap_result)
        stages.append(stage_from_process("bootstrap_pip", bootstrap_result))
        stdout = append_output(stdout, bootstrap_data["stdout"])
        stderr = append_output(stderr, bootstrap_data["stderr"])
        last_exit_code = bootstrap_data["exit_code"]
        timed_out = bootstrap_data["timed_out"]
        process_tree_terminated = process_tree_terminated or bootstrap_data["process_tree_terminated"]
        if timed_out or last_exit_code != 0:
            if timed_out:
                stderr = append_output(stderr, f"El bootstrap de pip supero el timeout de {venv_timeout_seconds} segundos.")
            detected_after_failure = detect_environment_after_failure(project)
            return {
                "success": False,
                "project_name": project_name,
                "environment_path": str(venv_dir),
                "python_executable": str(python_executable),
                "dependency_file": dependency_file,
                "venv_created": venv_created,
                "pip_upgraded": False,
                "dependencies_installed": False,
                "environment_incomplete": environment_incomplete_from_detection(detected_after_failure),
                "environment_detected_after_failure": detected_after_failure,
                "failure_type": "timeout" if timed_out else "bootstrap_pip_failure",
                "exit_code": last_exit_code,
                "timed_out": timed_out,
                "process_tree_terminated": process_tree_terminated,
                "failure_stage": "bootstrap_pip",
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": stdout,
                "stderr": stderr,
                "stages": stages,
            }

    upgrade_result = await run_process(
        [str(python_executable), "-m", "pip", "install", "--upgrade", "pip"],
        cwd=project,
        environment=environment_for_project(project),
        timeout_seconds=pip_upgrade_timeout_seconds,
    )
    upgrade_data = process_result_dict(upgrade_result)
    stages.append(stage_from_process("upgrade_pip", upgrade_result))
    stdout = append_output(stdout, upgrade_data["stdout"])
    stderr = append_output(stderr, upgrade_data["stderr"])
    last_exit_code = upgrade_data["exit_code"]
    timed_out = upgrade_data["timed_out"]
    process_tree_terminated = process_tree_terminated or upgrade_data["process_tree_terminated"]
    if timed_out or last_exit_code != 0:
        if timed_out:
            stderr = append_output(stderr, f"La actualizacion de pip supero el timeout de {pip_upgrade_timeout_seconds} segundos.")
        detected_after_failure = detect_environment_after_failure(project)
        return {
            "success": False,
            "project_name": project_name,
            "environment_path": str(venv_dir),
            "python_executable": str(python_executable),
            "dependency_file": dependency_file,
            "venv_created": venv_created,
            "pip_upgraded": False,
            "dependencies_installed": False,
            "environment_incomplete": environment_incomplete_from_detection(detected_after_failure),
            "environment_detected_after_failure": detected_after_failure,
            "failure_type": "timeout" if timed_out else "pip_upgrade_failure",
            "exit_code": last_exit_code,
            "timed_out": timed_out,
            "process_tree_terminated": process_tree_terminated,
            "failure_stage": "upgrade_pip",
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": stdout,
            "stderr": stderr,
            "stages": stages,
        }
    pip_upgraded = True

    install_result = await run_process(
        [str(python_executable), "-m", "pip", "install", "-r", str(dependencies)],
        cwd=project,
        environment=environment_for_project(project),
        timeout_seconds=dependency_install_timeout_seconds,
    )
    install_data = process_result_dict(install_result)
    stages.append(stage_from_process("install_dependencies", install_result))
    stdout = append_output(stdout, install_data["stdout"])
    stderr = append_output(stderr, install_data["stderr"])
    last_exit_code = install_data["exit_code"]
    timed_out = install_data["timed_out"]
    process_tree_terminated = process_tree_terminated or install_data["process_tree_terminated"]
    if timed_out or last_exit_code != 0:
        if timed_out:
            stderr = append_output(stderr, f"La instalacion de dependencias supero el timeout de {dependency_install_timeout_seconds} segundos.")
        detected_after_failure = detect_environment_after_failure(project)
        return {
            "success": False,
            "project_name": project_name,
            "environment_path": str(venv_dir),
            "python_executable": str(python_executable),
            "dependency_file": dependency_file,
            "venv_created": venv_created,
            "pip_upgraded": pip_upgraded,
            "dependencies_installed": False,
            "environment_incomplete": environment_incomplete_from_detection(detected_after_failure),
            "environment_detected_after_failure": detected_after_failure,
            "failure_type": "timeout" if timed_out else "dependency_installation_failure",
            "exit_code": last_exit_code,
            "timed_out": timed_out,
            "process_tree_terminated": process_tree_terminated,
            "failure_stage": "install_dependencies",
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": stdout,
            "stderr": stderr,
            "stages": stages,
        }
    dependencies_installed = True

    pip_check_result = await run_process(
        [str(python_executable), "-m", "pip", "check"],
        cwd=project,
        environment=environment_for_project(project),
        timeout_seconds=pip_check_timeout_seconds,
    )
    pip_check_data = process_result_dict(pip_check_result)
    stages.append(stage_from_process("pip_check", pip_check_result))
    stdout = append_output(stdout, pip_check_data["stdout"])
    stderr = append_output(stderr, pip_check_data["stderr"])
    last_exit_code = pip_check_data["exit_code"]
    timed_out = pip_check_data["timed_out"]
    process_tree_terminated = process_tree_terminated or pip_check_data["process_tree_terminated"]
    if timed_out or last_exit_code != 0:
        return {
            "success": False,
            "project_name": project_name,
            "environment_path": str(venv_dir),
            "python_executable": str(python_executable),
            "dependency_file": dependency_file,
            "venv_created": venv_created,
            "pip_upgraded": pip_upgraded,
            "dependencies_installed": dependencies_installed,
            "environment_incomplete": False,
            "environment_detected_after_failure": detect_environment_after_failure(project),
            "failure_type": "timeout" if timed_out else "pip_check_failure",
            "exit_code": last_exit_code,
            "timed_out": timed_out,
            "process_tree_terminated": process_tree_terminated,
            "failure_stage": "pip_check",
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": stdout,
            "stderr": stderr,
            "stages": stages,
        }

    inspect_result = await run_process(
        [
            str(python_executable),
            "-c",
            "import fastapi, starlette; print(f'fastapi=={fastapi.__version__}'); print(f'starlette=={starlette.__version__}')",
        ],
        cwd=project,
        environment=environment_for_project(project),
        timeout_seconds=inspect_versions_timeout_seconds,
    )
    inspect_data = process_result_dict(inspect_result)
    stages.append(stage_from_process("inspect_versions", inspect_result))
    stdout = append_output(stdout, inspect_data["stdout"])
    stderr = append_output(stderr, inspect_data["stderr"])
    installed_fastapi_version, installed_starlette_version = parse_installed_versions(inspect_data["stdout"])
    last_exit_code = inspect_data["exit_code"]
    timed_out = inspect_data["timed_out"]
    process_tree_terminated = process_tree_terminated or inspect_data["process_tree_terminated"]
    if timed_out or last_exit_code != 0:
        return {
            "success": False,
            "project_name": project_name,
            "environment_path": str(venv_dir),
            "python_executable": str(python_executable),
            "dependency_file": dependency_file,
            "venv_created": venv_created,
            "pip_upgraded": pip_upgraded,
            "dependencies_installed": dependencies_installed,
            "installed_fastapi_version": installed_fastapi_version,
            "installed_starlette_version": installed_starlette_version,
            "environment_incomplete": False,
            "environment_detected_after_failure": detect_environment_after_failure(project),
            "failure_type": "timeout" if timed_out else "version_inspection_failure",
            "exit_code": last_exit_code,
            "timed_out": timed_out,
            "process_tree_terminated": process_tree_terminated,
            "failure_stage": "inspect_versions",
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": stdout,
            "stderr": stderr,
            "stages": stages,
        }

    return {
        "success": True,
        **process_metadata(inspect_result),
        "project_name": project_name,
        "environment_path": str(venv_dir),
        "python_executable": str(python_executable),
        "dependency_file": dependency_file,
        "venv_created": venv_created,
        "pip_upgraded": pip_upgraded,
        "dependencies_installed": dependencies_installed,
        "installed_fastapi_version": installed_fastapi_version,
        "installed_starlette_version": installed_starlette_version,
        "environment_incomplete": False,
        "failure_type": "none",
        "exit_code": last_exit_code,
        "timed_out": False,
        "process_tree_terminated": process_tree_terminated,
        "failure_stage": "none",
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": stdout,
        "stderr": stderr,
        "stages": stages,
    }

async def run_tests_impl(
    project_name: str,
    framework: Literal["auto", "pytest", "npm", "dotnet"] = "auto",
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    if framework not in ALLOWED_FRAMEWORKS:
        raise ValueError(f"unsupported framework: {framework}")
    if timeout_seconds <= 0 or timeout_seconds > 120:
        raise ValueError("timeout_seconds must be between 1 and 120")
    project = resolve_project_path(project_name)
    selected = detect_test_framework_impl(project_name)["framework"] if framework == "auto" else framework
    project_python = None
    if selected == "pytest":
        project_python = venv_python_path(project)
        if not project_python.is_file():
            return {
                "success": False,
                "framework": selected,
                "command": command_for_framework(selected, project_python),
                "project_path": str(project),
                "exit_code": None,
                "timed_out": False,
                "failure_type": "environment_not_prepared",
                "process_tree_terminated": False,
                "duration_seconds": 0,
                "stdout": "",
                "stderr": "El entorno del proyecto no ha sido preparado. Ejecuta prepare_test_environment.",
                "message": "El entorno del proyecto no ha sido preparado. Ejecuta prepare_test_environment.",
            }
    command = command_for_framework(selected, project_python)
    execution = await run_process(
        command,
        cwd=project,
        environment=environment_for_project(project),
        timeout_seconds=timeout_seconds,
    )
    execution_data = process_result_dict(execution)
    stdout = execution_data["stdout"]
    stderr = execution_data["stderr"]
    if execution_data["timed_out"]:
        timeout_message = f"La ejecucion de pruebas supero el timeout de {timeout_seconds} segundos."
        stderr = f"{stderr}\n{timeout_message}".strip()
    failure_type = classify_failure(execution_data["exit_code"], execution_data["timed_out"])
    test_warning_count = extract_pytest_warning_count(stdout) if selected == "pytest" else None
    return {
        "success": execution_data["exit_code"] == 0,
        "framework": selected,
        "command": command,
        "project_path": str(project),
        "exit_code": execution_data["exit_code"],
        "timed_out": execution_data["timed_out"],
        "failure_type": failure_type,
        "process_tree_terminated": execution_data["process_tree_terminated"],
        "process_completed": execution_data["process_completed"],
        "duration_seconds": execution_data["duration_seconds"],
        "test_warning_count": test_warning_count,
        "stdout": stdout,
        "stderr": stderr,
    }

@mcp.tool()
def detect_test_framework(project_name: str) -> dict:
    return detect_test_framework_impl(project_name)


@mcp.tool()
async def prepare_test_environment(
    project_name: str,
    dependency_file: str = "requirements.txt",
) -> dict:
    result = await prepare_test_environment_impl(
        project_name,
        dependency_file,
        None,
        DEFAULT_VENV_TIMEOUT_SECONDS,
        DEFAULT_PIP_UPGRADE_TIMEOUT_SECONDS,
        DEFAULT_DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
        DEFAULT_PIP_CHECK_TIMEOUT_SECONDS,
        DEFAULT_INSPECT_VERSIONS_TIMEOUT_SECONDS,
    )
    log_prepare_summary(result)
    return result


@mcp.tool()
async def run_tests(
    project_name: str,
    framework: Literal["auto", "pytest", "npm", "dotnet"] = "auto",
    timeout_seconds: int = 30,
) -> dict:
    return await run_tests_impl(project_name, framework, timeout_seconds)


if __name__ == "__main__":
    mcp.run(transport="stdio")
