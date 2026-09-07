"""Shared entry-point configuration; domain policies keep their own settings."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import load_dotenv


REPOSITORY_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class RuntimeSettings:
    openai_api_key: str | None = field(repr=False)
    openai_model: str
    workspace_root: Path
    data_root: Path


def load_settings(root: Path = REPOSITORY_ROOT) -> RuntimeSettings:
    root = root.resolve()
    # Never search a parent directory for credentials or override process env.
    load_dotenv(root / ".env", override=False)

    def configured_path(name: str, default: str) -> Path:
        value = Path(os.getenv(name, "").strip() or default).expanduser()
        return (value if value.is_absolute() else root / value).resolve()

    return RuntimeSettings(
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL") or "gpt-4.1-mini",
        workspace_root=configured_path("WORKSPACE_ROOT", "workspace"),
        data_root=configured_path("DATA_ROOT", "data"),
    )
