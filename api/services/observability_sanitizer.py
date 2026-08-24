from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any

SENSITIVE_KEY = re.compile(
    r"(authorization|cookie|token|secret|password|api[_-]?key|credential|prompt|request_body|response_body|content)",
    re.IGNORECASE,
)
MAX_STRING = 1_000
MAX_ITEMS = 50
SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+)[^\s,;]+|\bsk-[a-z0-9_-]{8,}\b|((?:api[_-]?key|password|token)\s*[=:]\s*)[^\s,;]+"
)


def sanitize_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if depth > 5:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        redacted = SECRET_VALUE.sub(lambda match: (match.group(1) or match.group(2) or "") + "[REDACTED]", value)
        return redacted if len(redacted) <= MAX_STRING else redacted[:MAX_STRING] + "...[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_value(item, key=str(item_key), depth=depth + 1)
            for item_key, item in list(value.items())[:MAX_ITEMS]
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize_value(item, depth=depth + 1) for item in list(value)[:MAX_ITEMS]]
    return sanitize_value(str(value), key=key, depth=depth + 1)


def sanitize_mapping(value: dict[str, Any] | None) -> dict[str, Any]:
    return sanitize_value(value or {})


def safe_json(value: dict[str, Any] | None) -> str:
    return json.dumps(sanitize_mapping(value), ensure_ascii=True, separators=(",", ":"), default=str)


def safe_path(path: str | None) -> str | None:
    if not path:
        return None
    parts = [part for part in PurePosixPath(path.replace("\\", "/")).parts if part not in {"/", "\\"}]
    if parts and parts[0].endswith(":"):
        parts = parts[1:]
    return "/".join(parts[-4:])


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def error_fingerprint(error_type: str | None, message: str | None, operation: str | None) -> str:
    normalized = re.sub(r"\b[0-9a-f]{8,}\b|\b\d+\b", "?", (message or "").casefold())
    return stable_hash("|".join((error_type or "error", normalized[:500], operation or "")))[:24]
