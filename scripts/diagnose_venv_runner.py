from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers import testing_server as ts


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mcp-venv-diagnostic-") as temp_dir:
        cwd = Path(temp_dir)
        command = [sys.executable, "-m", "venv", ".venv", "--without-pip"]
        environment = ts.build_subprocess_environment()
        result = await ts.run_process(command, cwd=cwd, environment=environment, timeout_seconds=60)
        payload = {
            "command": result.command,
            "working_directory": result.working_directory,
            "host_python_executable": result.host_python_executable,
            "pid": result.pid,
            "duration_seconds": result.duration_seconds,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "process_completed": result.process_completed,
            "process_tree_terminated": result.process_tree_terminated,
            "venv_exists": (cwd / ".venv").is_dir(),
            "python_executable_exists": ts.venv_python_path(cwd).is_file(),
            "stdin": "DEVNULL",
            "creation_options": ts.get_process_creation_options(),
            "python_environment": {
                "PYTHONHOME": "present" if "PYTHONHOME" in environment else "removed",
                "PYTHONPATH": "present" if "PYTHONPATH" in environment else "removed",
                "VIRTUAL_ENV": "present" if "VIRTUAL_ENV" in environment else "removed",
            },
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
