from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any


INFRASTRUCTURE_IGNORED_DIRECTORIES = frozenset({".venv", "__pycache__", ".pytest_cache"})
INFRASTRUCTURE_IGNORED_FILES = frozenset({".coverage"})


@dataclass(frozen=True)
class DirtyPathClassification:
    allowed_files: list[str]
    ignored_infrastructure: list[str]
    unrelated_files: list[str]


def normalize_repository_path(path: str) -> str | None:
    raw = str(path or "").replace("\\", "/").strip()
    while raw.startswith("./"):
        raw = raw[2:]
    raw = raw.strip("/")
    if not raw or raw.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", raw):
        return None
    normalized = PurePosixPath(raw).as_posix()
    return None if unsafe_repository_path(normalized) else normalized


def unsafe_repository_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        not path
        or ".." in parts
        or any(part.casefold() == ".git" for part in parts)
    )


def is_infrastructure_ignored(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    if parts[-1] in INFRASTRUCTURE_IGNORED_FILES:
        return True
    return any(part in INFRASTRUCTURE_IGNORED_DIRECTORIES for part in parts)


def classify_dirty_paths(
    project_root: Path,
    status: Any,
    allowed_dirty_paths: Iterable[str],
) -> DirtyPathClassification:
    root = project_root.resolve()
    allowed = {
        normalized
        for path in allowed_dirty_paths
        if (normalized := normalize_repository_path(str(path))) is not None
    }
    allowed_files: set[str] = set()
    ignored: set[str] = set()
    unrelated: set[str] = set()

    for key in ("staged", "modified", "deleted"):
        for raw_path in _status_values(status, key):
            changed = normalize_repository_path(str(raw_path))
            if changed is None:
                unrelated.add(str(raw_path).replace("\\", "/"))
            elif is_infrastructure_ignored(changed):
                ignored.add(changed)
            elif changed in allowed:
                allowed_files.add(changed)
            else:
                unrelated.add(changed)

    for raw_path in _status_values(status, "untracked"):
        raw = str(raw_path or "").replace("\\", "/").strip()
        changed = normalize_repository_path(raw)
        if changed is None:
            unrelated.add(str(raw_path).replace("\\", "/"))
            continue
        if is_infrastructure_ignored(changed):
            ignored.add(changed)
            continue
        if changed in allowed:
            allowed_files.add(changed)
            continue
        directory_like = raw.endswith(("/", "\\")) or (root / changed).is_dir()
        if directory_like:
            expanded = expand_directory_files(root, changed)
            if expanded is not None and expanded <= allowed:
                allowed_files.update(expanded)
                continue
        unrelated.add(changed)

    return DirtyPathClassification(
        allowed_files=sorted(allowed_files),
        ignored_infrastructure=sorted(ignored),
        unrelated_files=sorted(unrelated),
    )


def expand_directory_files(project_root: Path, directory: str) -> set[str] | None:
    normalized = normalize_repository_path(directory)
    if normalized is None:
        return None
    root = project_root.resolve()
    candidate = (root / normalized).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_dir():
        return None

    files: set[str] = set()
    for path in candidate.rglob("*"):
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            return None
        normalized_file = relative.as_posix()
        if unsafe_repository_path(normalized_file):
            return None
        if path.is_file() and not is_infrastructure_ignored(normalized_file):
            files.add(normalized_file)
    return files


def _status_values(status: Any, key: str) -> Iterable[Any]:
    if isinstance(status, Mapping):
        return status.get(key) or []
    return getattr(status, key, None) or []
