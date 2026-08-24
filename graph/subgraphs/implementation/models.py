from __future__ import annotations

import re
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SUPPORTED_FRAMEWORKS = {"fastapi", "dotnet", "node"}
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")


class StrictImplementationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneratedFile(StrictImplementationModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=1_048_576)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or WINDOWS_ABSOLUTE.match(normalized):
            raise ValueError("file path must be relative")
        if ".." in path.parts or not path.parts:
            raise ValueError("file path must not contain traversal")
        if any(part.casefold() == ".git" for part in path.parts):
            raise ValueError("file path must not target .git")
        return path.as_posix()


class ProjectImplementationPlan(StrictImplementationModel):
    project_name: str = Field(min_length=1, max_length=120)
    framework: str = Field(min_length=1, max_length=50)
    package_name: str = Field(min_length=1, max_length=120)
    files: list[GeneratedFile] = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def normalize_project_relative_file_paths(cls, data):
        if not isinstance(data, dict):
            return data
        project_name = str(data.get("project_name") or "").strip().replace("\\", "/").strip("/")
        files = data.get("files")
        if not project_name or not isinstance(files, list):
            return data
        normalized_files: list[object] = []
        seen: dict[str, dict[str, str]] = {}
        prefix = f"{project_name}/"
        for item in files:
            if not isinstance(item, dict):
                normalized_files.append(item)
                continue
            raw_path = str(item.get("path") or "").strip().replace("\\", "/")
            normalized_path = raw_path[len(prefix):] if raw_path.startswith(prefix) else raw_path
            normalized_item = {**item, "path": normalized_path}
            key = normalized_path.casefold()
            content = str(normalized_item.get("content") or "")
            if key in seen:
                previous = seen[key]
                if previous["content"] != content:
                    raise ValueError(f"implementation_path_conflict: {normalized_path}")
                continue
            seen[key] = {"path": normalized_path, "content": content}
            normalized_files.append(normalized_item)
        return {**data, "files": normalized_files}

    @field_validator("framework")
    @classmethod
    def validate_framework(cls, value: str) -> str:
        lowered = value.strip().lower()
        if lowered not in SUPPORTED_FRAMEWORKS:
            raise ValueError(f"unsupported framework: {value}")
        return lowered

    @field_validator("project_name", "package_name")
    @classmethod
    def normalize_names(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_unique_paths(self) -> "ProjectImplementationPlan":
        if self.framework == "fastapi" and not self.package_name.isidentifier():
            raise ValueError("package_name must be a valid Python identifier for FastAPI")
        paths = [file.path.casefold() for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("generated file paths must be unique")
        return self
