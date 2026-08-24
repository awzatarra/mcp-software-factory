from __future__ import annotations

import re
import unicodedata


SUPPORTED_PROJECT_TYPES = frozenset({"fastapi", "dotnet", "node"})


def _searchable(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9.+]+", " ", ascii_value.casefold()).strip()


def normalize_project_type(value: str | None) -> str | None:
    """Resolve supported narrative aliases without expanding the supported type set."""
    if not value or not value.strip():
        return None
    normalized = _searchable(value)
    compact = normalized.replace(" ", "")
    if "fastapi" in compact:
        return "fastapi"
    if any(alias in compact for alias in ("dotnet", ".net", "aspnet")):
        return "dotnet"
    if any(alias in compact for alias in ("nodejs", "node.js", "expressjs")) or re.search(r"\b(?:node|express)\b", normalized):
        return "node"
    return normalized if normalized in SUPPORTED_PROJECT_TYPES else None


def normalize_framework(value: str | None, *, project_type: str | None = None) -> str | None:
    if value and value.strip():
        return normalize_project_type(value)
    return normalize_project_type(project_type)
