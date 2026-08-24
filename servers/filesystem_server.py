from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel


mcp = FastMCP("software-factory-filesystem")

MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_PROJECT_BYTES = 5 * 1024 * 1024
MAX_FILES_PER_OPERATION = 50
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE_ROOT = REPOSITORY_ROOT / "workspace"
IGNORED_DIRECTORIES = {".venv", "__pycache__", ".pytest_cache", ".git", "node_modules", "bin", "obj"}


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


class ProjectFile(BaseModel):
    path: str
    content: str


class FileUpdate(BaseModel):
    path: str
    content: str


def set_workspace_root(path: Path) -> None:
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = path.resolve()


def get_workspace_root() -> Path:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_ROOT.resolve()


def resolve_workspace_path(relative_path: str) -> Path:
    if relative_path is None or not str(relative_path).strip():
        raise ValueError("relative_path must not be empty")
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError("absolute paths are not allowed")
    root = get_workspace_root()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes the workspace") from exc
    return resolved


def relative_to_workspace(path: Path) -> str:
    return path.resolve().relative_to(get_workspace_root()).as_posix()


def content_size(content: str) -> int:
    return len(content.encode("utf-8"))


def validate_project_name(project_name: str) -> str:
    if not PROJECT_NAME_RE.fullmatch(project_name or ""):
        raise ValueError("project_name may contain only letters, numbers, hyphens and underscores, max 64 chars")
    return project_name


def validate_file_batch(files: list[ProjectFile | FileUpdate]) -> tuple[set[str], int]:
    if not files:
        raise ValueError("files must not be empty")
    if len(files) > MAX_FILES_PER_OPERATION:
        raise ValueError(f"too many files; maximum is {MAX_FILES_PER_OPERATION}")
    seen: set[str] = set()
    total_size = 0
    for file in files:
        normalized = Path(file.path).as_posix().lower()
        if normalized in seen:
            raise ValueError(f"duplicate file path: {file.path}")
        seen.add(normalized)
        size = content_size(file.content)
        if size > MAX_FILE_BYTES:
            raise ValueError(f"file too large: {file.path}")
        total_size += size
    if total_size > MAX_PROJECT_BYTES:
        raise ValueError("project is too large")
    return seen, total_size


def validate_project_file_paths_do_not_include_project_name(project_name: str, files: list[ProjectFile]) -> None:
    for file in files:
        first_segment = Path(file.path).parts[0] if Path(file.path).parts else ""
        if first_segment.lower() == project_name.lower():
            raise ValueError(
                "Las rutas deben ser relativas a la raíz del proyecto y no deben incluir project_name."
            )


def is_ignored_path(path: Path) -> bool:
    return any(part in IGNORED_DIRECTORIES for part in path.parts)


def list_files_impl(relative_path: str = ".", recursive: bool = False, include_ignored: bool = False) -> dict[str, Any]:
    target = resolve_workspace_path(relative_path)
    if not target.exists():
        raise FileNotFoundError(f"path does not exist: {relative_path}")
    if not target.is_dir():
        raise NotADirectoryError(f"path is not a directory: {relative_path}")
    iterator = target.rglob("*") if recursive else target.iterdir()
    entries = []
    for entry in sorted(iterator, key=lambda p: p.as_posix().lower()):
        relative_entry = entry.resolve().relative_to(target.resolve())
        if not include_ignored and is_ignored_path(relative_entry):
            continue
        entries.append(
            {
                "path": relative_to_workspace(entry),
                "type": "directory" if entry.is_dir() else "file",
                "size_bytes": 0 if entry.is_dir() else entry.stat().st_size,
            }
        )
    return {
        "workspace": str(get_workspace_root()),
        "relative_path": relative_path,
        "recursive": recursive,
        "include_ignored": include_ignored,
        "entries": entries,
    }


def read_file_impl(relative_path: str) -> dict[str, Any]:
    target = resolve_workspace_path(relative_path)
    if not target.exists():
        raise FileNotFoundError(f"file does not exist: {relative_path}")
    if not target.is_file():
        raise IsADirectoryError(f"path is not a file: {relative_path}")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError("file is too large")
    return {"path": relative_to_workspace(target), "size_bytes": size, "content": target.read_text(encoding="utf-8")}


def create_directory_impl(relative_path: str) -> dict[str, Any]:
    target = resolve_workspace_path(relative_path)
    existed = target.exists()
    if existed and not target.is_dir():
        raise FileExistsError("path exists and is not a directory")
    target.mkdir(parents=True, exist_ok=True)
    return {"success": True, "path": relative_to_workspace(target), "existed": existed}


def write_file_impl(relative_path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    target = resolve_workspace_path(relative_path)
    size = content_size(content)
    if size > MAX_FILE_BYTES:
        raise ValueError("file is too large")
    existed = target.exists()
    if existed and not overwrite:
        raise FileExistsError("file already exists; pass overwrite=true to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"success": True, "path": relative_to_workspace(target), "size_bytes": size, "overwritten": existed}


def resolve_project_file(project_root: Path, file_path: str) -> Path:
    if not file_path or not file_path.strip():
        raise ValueError("file path must not be empty")
    candidate = Path(file_path)
    if candidate.is_absolute():
        raise ValueError("absolute file paths are not allowed")
    resolved = (project_root / candidate).resolve()
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError as exc:
        raise ValueError(f"file path escapes project: {file_path}") from exc
    return resolved


def create_project_structure_impl(project_name: str, files: list[ProjectFile]) -> dict[str, Any]:
    project_name = validate_project_name(project_name)
    _, total_size = validate_file_batch(files)
    validate_project_file_paths_do_not_include_project_name(project_name, files)
    workspace = get_workspace_root()
    destination = resolve_workspace_path(project_name)
    if destination.exists():
        raise FileExistsError(f"project already exists: {project_name}")
    response = {
        "success": True,
        "project_name": project_name,
        "project_path": relative_to_workspace(destination),
        "files_created": [file.path for file in files],
        "total_files": len(files),
        "total_size_bytes": total_size,
    }

    temp_dir = Path(tempfile.mkdtemp(prefix=f".{project_name}.", dir=workspace))
    try:
        for file in files:
            target = resolve_project_file(temp_dir, file.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file.content, encoding="utf-8")
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    try:
        shutil.move(str(temp_dir), str(destination))
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return response


def update_project_files_impl(project_name: str, files: list[FileUpdate]) -> dict[str, Any]:
    project_name = validate_project_name(project_name)
    _, total_size = validate_file_batch(files)
    project_root = resolve_workspace_path(project_name)
    if not project_root.is_dir():
        raise FileNotFoundError(f"project does not exist: {project_name}")

    targets: list[tuple[Path, str]] = []
    previous: dict[Path, str] = {}
    for file in files:
        target = resolve_project_file(project_root, file.path)
        if not target.is_file():
            raise FileNotFoundError(f"file does not exist: {file.path}")
        targets.append((target, file.content))
        previous[target] = target.read_text(encoding="utf-8")

    try:
        for target, content in targets:
            target.write_text(content, encoding="utf-8")
    except Exception:
        for target, old_content in previous.items():
            target.write_text(old_content, encoding="utf-8")
        raise

    return {
        "success": True,
        "project_name": project_name,
        "files_updated": [file.path for file in files],
        "total_files": len(files),
        "total_size_bytes": total_size,
    }


@mcp.tool()
def list_files(relative_path: str = ".", recursive: bool = False, include_ignored: bool = False) -> dict:
    """List files while excluding generated dependency/cache folders unless include_ignored is true."""
    return list_files_impl(relative_path, recursive, include_ignored)


@mcp.tool()
def read_file(relative_path: str) -> dict:
    return read_file_impl(relative_path)


@mcp.tool()
def create_directory(relative_path: str) -> dict:
    return create_directory_impl(relative_path)


@mcp.tool()
def write_file(relative_path: str, content: str, overwrite: bool = False) -> dict:
    return write_file_impl(relative_path, content, overwrite)


@mcp.tool()
def create_project_structure(project_name: str, files: list[ProjectFile] | None = None) -> dict:
    return create_project_structure_impl(project_name, files or [])


@mcp.tool()
def update_project_files(project_name: str, files: list[FileUpdate] | None = None) -> dict:
    return update_project_files_impl(project_name, files or [])


if __name__ == "__main__":
    mcp.run(transport="stdio")

