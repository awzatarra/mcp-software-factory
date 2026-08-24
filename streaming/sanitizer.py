from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

_SECRET_PARTS = (
    "api_key",
    "authorization",
    "token",
    "secret",
    "password",
    "cookie",
    "openai_api_key",
)
_MAX_DEPTH = 5
_MAX_ITEMS = 50
_MAX_STRING = 1_000
_MAX_OUTPUT = 2_000


def _is_secret(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SECRET_PARTS)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}... [truncated {len(value) - limit} chars]"


def _sanitize(value: Any, *, depth: int, key: str = "") -> Any:
    if depth > _MAX_DEPTH:
        return "[max depth]"
    if _is_secret(key):
        return "[redacted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        limit = _MAX_OUTPUT if key.lower() in {"stdout", "stderr"} else _MAX_STRING
        return _truncate(value, limit)
    if isinstance(value, (Path, UUID)):
        return str(value)
    if isinstance(value, Enum):
        return _sanitize(value.value, depth=depth + 1, key=key)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                result["_truncated_items"] = len(value) - _MAX_ITEMS
                break
            item_key = str(raw_key)
            result[item_key] = _sanitize(item, depth=depth + 1, key=item_key)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _sanitize(item, depth=depth + 1, key=key)
            for item in value[:_MAX_ITEMS]
        ]
    raise TypeError(f"Unsupported event data type: {type(value).__name__}")


def _generated_file_summary(files: Any) -> tuple[int, list[str]]:
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes, bytearray)):
        return 0, []
    paths: list[str] = []
    for item in files[:_MAX_ITEMS]:
        if isinstance(item, Mapping) and item.get("path") is not None:
            paths.append(str(item["path"]))
        elif isinstance(item, (str, Path)):
            paths.append(str(item))
    return len(files), paths


def sanitize_event_data(data: Mapping[str, Any] | None) -> dict[str, Any]:
    if data is None:
        return {}
    prepared = dict(data)
    if "generated_files" in prepared:
        count, paths = _generated_file_summary(prepared.pop("generated_files"))
        prepared["generated_file_count"] = count
        prepared["generated_file_paths"] = paths
    sanitized = _sanitize(prepared, depth=0)
    if not isinstance(sanitized, dict):
        raise TypeError("Event data must be a mapping")
    return sanitized
