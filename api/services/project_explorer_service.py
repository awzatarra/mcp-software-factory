from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
import io
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from tempfile import SpooledTemporaryFile
from urllib.parse import unquote
import zipfile

from api.models import (
    ProjectFileContentResponse,
    ProjectFileNode,
    ProjectFileTreeResponse,
    WorkflowProjectSummary,
)
from api.services.workflow_metadata_store import WorkflowMetadataStore
from api.services.workflow_query_service import WorkflowQueryService
from graph.persistence_service import WorkflowNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_EXCLUDED_NAMES = {
    ".venv",
    "venv",
    "node_modules",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "coverage",
    "dist",
    "build",
    ".env",
}
DEFAULT_EXCLUDED_PATTERNS = {
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "secrets.*",
    "credentials.*",
}
TEXT_NAMES = {"Dockerfile", ".gitignore"}
TEXT_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".txt",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".html", ".css",
    ".scss", ".sql", ".xml", ".cs", ".csproj", ".sln", ".sh", ".ps1",
    ".bat", ".cmd",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}
LANGUAGES = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".json": "json",
    ".md": "markdown", ".yml": "yaml", ".yaml": "yaml", ".html": "html",
    ".css": "css", ".scss": "scss", ".sql": "sql", ".cs": "csharp",
    ".ps1": "powershell", ".sh": "bash", ".toml": "toml", ".xml": "xml",
}


class ProjectExplorerError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _decode_path(value: str) -> str:
    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded.replace("\\", "/")


def resolve_safe_project_path(
    workspace_root: Path,
    project_name: str,
    relative_path: str | None = None,
) -> Path:
    root = workspace_root.resolve()
    if (
        not project_name
        or PurePosixPath(project_name).name != project_name
        or PureWindowsPath(project_name).name != project_name
    ):
        raise ProjectExplorerError("Invalid project reference.", 403)
    project_root = (root / project_name).resolve()
    try:
        project_root.relative_to(root)
    except ValueError as exc:
        raise ProjectExplorerError("Project is outside the workspace.", 403) from exc
    raw = _decode_path(relative_path or "")
    windows_path = PureWindowsPath(raw)
    if raw.startswith("/") or raw.startswith("//") or windows_path.is_absolute() or windows_path.drive:
        raise ProjectExplorerError("Absolute paths are not allowed.", 400)
    parts = PurePosixPath(raw).parts
    if ".." in parts:
        raise ProjectExplorerError("Path traversal is not allowed.", 403)
    resolved = (project_root / Path(*parts)).resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ProjectExplorerError("Path traversal is not allowed.", 403) from exc
    return resolved


def _configured_set(name: str, defaults: set[str]) -> set[str]:
    configured = os.getenv(name, "")
    return defaults | {item.strip() for item in configured.split(",") if item.strip()}


@dataclass(frozen=True)
class ExplorerPolicy:
    max_file_size: int = 1_048_576
    max_tree_entries: int = 5_000
    max_archive_size: int = 52_428_800
    excluded_names: frozenset[str] = frozenset(DEFAULT_EXCLUDED_NAMES)
    excluded_patterns: frozenset[str] = frozenset(DEFAULT_EXCLUDED_PATTERNS)

    @classmethod
    def from_environment(cls) -> "ExplorerPolicy":
        return cls(
            max_file_size=int(os.getenv("PROJECT_EXPLORER_MAX_FILE_SIZE_BYTES", "1048576")),
            max_tree_entries=int(os.getenv("PROJECT_EXPLORER_MAX_TREE_ENTRIES", "5000")),
            max_archive_size=int(os.getenv("PROJECT_EXPLORER_MAX_ARCHIVE_SIZE_BYTES", "52428800")),
            excluded_names=frozenset(
                _configured_set("PROJECT_EXPLORER_EXCLUDED_NAMES", DEFAULT_EXCLUDED_NAMES)
            ),
            excluded_patterns=frozenset(
                _configured_set("PROJECT_EXPLORER_EXCLUDED_PATTERNS", DEFAULT_EXCLUDED_PATTERNS)
            ),
        )


class ProjectExplorerService:
    def __init__(
        self,
        *,
        query: WorkflowQueryService,
        metadata_store: WorkflowMetadataStore,
        workspace_root: Path,
        policy: ExplorerPolicy | None = None,
    ) -> None:
        self.query = query
        self.metadata_store = metadata_store
        self.workspace_root = workspace_root.resolve()
        self.policy = policy or ExplorerPolicy.from_environment()

    def _is_excluded(self, relative: PurePosixPath) -> bool:
        names = {name.lower() for name in self.policy.excluded_names}
        patterns = {pattern.lower() for pattern in self.policy.excluded_patterns}
        for part in relative.parts:
            lowered = part.lower()
            if lowered in names or any(fnmatch(lowered, pattern) for pattern in patterns):
                return True
        return False

    async def _context(self, thread_id: str):
        snapshot = await self.query.get_snapshot(thread_id)
        changes = await self.metadata_store.get_project_files(thread_id)
        project_name = snapshot.project_name
        root = (
            resolve_safe_project_path(self.workspace_root, project_name)
            if project_name
            else None
        )
        return snapshot, changes, root

    def _visible_files(self, root: Path):
        for path in root.rglob("*"):
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if self._is_excluded(relative):
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            yield path, relative

    async def summary(self, thread_id: str) -> WorkflowProjectSummary:
        snapshot, changes, root = await self._context(thread_id)
        exists = bool(root and root.is_dir())
        files = directories = total_size = 0
        if exists and root is not None:
            for path, _ in self._visible_files(root):
                if path.is_dir():
                    directories += 1
                elif path.is_file():
                    files += 1
                    total_size += path.stat().st_size
        generated = sorted(path for path, change in changes.items() if change == "generated")
        updated = sorted(path for path, change in changes.items() if change == "updated")
        return WorkflowProjectSummary(
            thread_id=thread_id,
            project_name=snapshot.project_name,
            project_exists=exists,
            relative_project_path=snapshot.project_name if exists else None,
            total_files=files,
            total_directories=directories,
            total_size_bytes=total_size,
            generated_files=generated,
            updated_files=updated,
            detected_framework=self._framework(snapshot),
            detected_test_framework=self._test_framework(snapshot),
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
        )

    @staticmethod
    def _framework(snapshot) -> str | None:
        value = snapshot.implementation.get("detected_framework")
        if value:
            return str(value)
        return "FastAPI" if snapshot.project_name and "api" in snapshot.project_name.lower() else None

    @staticmethod
    def _test_framework(snapshot) -> str | None:
        value = snapshot.testing.get("detected_test_framework")
        if value:
            return str(value)
        return "pytest" if snapshot.testing.get("executed") else None

    def _classification(self, path: Path) -> tuple[str, str | None]:
        extension = path.suffix.lower()
        if extension in IMAGE_EXTENSIONS:
            return "image", None
        try:
            with path.open("rb") as source:
                sample = source.read(8192)
        except OSError:
            return "unknown", None
        if b"\x00" in sample:
            return "binary", None
        try:
            decoded = sample.decode("utf-8")
        except UnicodeDecodeError:
            return "binary", None
        printable = sum(character.isprintable() or character in "\r\n\t" for character in decoded)
        text_likely = not decoded or printable / max(len(decoded), 1) > 0.9
        if path.name in TEXT_NAMES or extension in TEXT_EXTENSIONS or text_likely:
            language = "dockerfile" if path.name == "Dockerfile" else LANGUAGES.get(extension)
            return "text", language or "text"
        return "unknown", None

    def _node(
        self,
        path: Path,
        root: Path,
        changes: dict[str, str],
        *,
        depth: int,
        include_hidden: bool,
        counter: list[int],
    ) -> ProjectFileNode:
        relative = path.relative_to(root).as_posix() if path != root else ""
        change = changes.get(relative)
        if path.is_dir():
            children: list[ProjectFileNode] = []
            if depth > 0:
                candidates = []
                for child in path.iterdir():
                    child_relative = PurePosixPath(child.relative_to(root).as_posix())
                    if self._is_excluded(child_relative):
                        continue
                    if not include_hidden and child.name.startswith(".") and child.name not in TEXT_NAMES:
                        continue
                    try:
                        child.resolve().relative_to(root)
                    except (OSError, ValueError):
                        continue
                    candidates.append(child)
                candidates.sort(key=lambda item: (not item.is_dir(), item.name.lower()))
                for child in candidates:
                    if counter[0] >= self.policy.max_tree_entries:
                        break
                    counter[0] += 1
                    children.append(
                        self._node(
                            child, root, changes, depth=depth - 1,
                            include_hidden=include_hidden, counter=counter,
                        )
                    )
            return ProjectFileNode(
                name=path.name, path=relative, type="directory", size_bytes=None,
                extension=None, language=None, content_type=None,
                content_available=False, is_generated=False, is_updated=False,
                children=children,
            )
        content_type, language = self._classification(path)
        size = path.stat().st_size
        return ProjectFileNode(
            name=path.name,
            path=relative,
            type="file",
            size_bytes=size,
            extension=path.suffix.lower() or None,
            language=language,
            content_type=content_type,
            content_available=content_type == "text" and size <= self.policy.max_file_size,
            is_generated=change == "generated",
            is_updated=change == "updated",
        )

    async def tree(
        self,
        thread_id: str,
        *,
        relative_path: str,
        depth: int,
        include_hidden: bool,
    ) -> ProjectFileTreeResponse:
        snapshot, changes, root = await self._context(thread_id)
        if root is None or not root.is_dir() or snapshot.project_name is None:
            raise ProjectExplorerError("The project has not been created yet.", 409)
        target = resolve_safe_project_path(self.workspace_root, snapshot.project_name, relative_path)
        relative = PurePosixPath(target.relative_to(root).as_posix())
        if self._is_excluded(relative):
            raise ProjectExplorerError("The requested path is excluded.", 403)
        if not target.exists():
            raise ProjectExplorerError("The requested path does not exist.", 404)
        if not target.is_dir():
            raise ProjectExplorerError("The requested tree path is not a directory.", 400)
        counter = [0]
        node = self._node(
            target, root, changes, depth=depth,
            include_hidden=include_hidden, counter=counter,
        )
        return ProjectFileTreeResponse(
            thread_id=thread_id,
            project_name=snapshot.project_name,
            root=node,
            truncated=counter[0] >= self.policy.max_tree_entries,
            total_entries=counter[0],
        )

    async def content(self, thread_id: str, relative_path: str) -> ProjectFileContentResponse:
        snapshot, changes, root = await self._context(thread_id)
        target = self._file_target(snapshot.project_name, root, relative_path)
        size = target.stat().st_size
        content_type, language = self._classification(target)
        if content_type != "text":
            raise ProjectExplorerError("This file cannot be previewed.", 415)
        if size > self.policy.max_file_size:
            raise ProjectExplorerError("The file is too large to preview.", 413)
        content = target.read_text(encoding="utf-8")
        normalized = target.relative_to(root).as_posix()
        change = changes.get(normalized)
        logger.info(
            "project explorer thread=%s operation=content path=%s result=ok size=%s",
            thread_id, normalized, size,
        )
        return ProjectFileContentResponse(
            thread_id=thread_id,
            project_name=str(snapshot.project_name),
            path=normalized,
            name=target.name,
            extension=target.suffix.lower() or None,
            language=language,
            content_type="text",
            encoding="utf-8",
            size_bytes=size,
            content=content,
            truncated=False,
            line_count=len(content.splitlines()),
            is_generated=change == "generated",
            is_updated=change == "updated",
        )

    def _file_target(self, project_name: str | None, root: Path | None, relative_path: str) -> Path:
        if project_name is None or root is None or not root.is_dir():
            raise ProjectExplorerError("The project has not been created yet.", 409)
        target = resolve_safe_project_path(self.workspace_root, project_name, relative_path)
        relative = PurePosixPath(target.relative_to(root).as_posix())
        if self._is_excluded(relative):
            raise ProjectExplorerError("The requested file is excluded.", 403)
        if not target.exists():
            raise ProjectExplorerError("The requested file does not exist.", 404)
        if not target.is_file():
            raise ProjectExplorerError("The requested path is not a file.", 400)
        return target

    async def download_target(self, thread_id: str, relative_path: str) -> Path:
        snapshot, _, root = await self._context(thread_id)
        return self._file_target(snapshot.project_name, root, relative_path)

    async def archive(self, thread_id: str) -> tuple[SpooledTemporaryFile, str, int]:
        snapshot, _, root = await self._context(thread_id)
        if snapshot.project_name is None or root is None or not root.is_dir():
            raise ProjectExplorerError("The project has not been created yet.", 409)
        archive = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        total_size = 0
        try:
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
                for path, relative in self._visible_files(root):
                    if not path.is_file():
                        continue
                    size = path.stat().st_size
                    total_size += size
                    if total_size > self.policy.max_archive_size:
                        raise ProjectExplorerError("The project is too large to archive.", 413)
                    output.write(path, arcname=relative.as_posix())
            archive.seek(0, io.SEEK_END)
            archive_size = archive.tell()
            archive.seek(0)
            filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", snapshot.project_name).strip(".-")
            logger.info(
                "project explorer thread=%s operation=archive result=ok size=%s",
                thread_id, archive_size,
            )
            return archive, f"{filename or 'project'}.zip", archive_size
        except Exception:
            archive.close()
            raise
