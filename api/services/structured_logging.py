from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from api.services.observability_context import get_observability_context


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = get_observability_context()
        payload = {
            "timestamp": datetime.now(UTC).isoformat(), "level": record.levelname.casefold(),
            "logger": record.name, "message": record.getMessage(), "trace_id": context.trace_id,
            "span_id": context.span_id, "workflow_id": context.workflow_id,
            "branch_id": context.branch_id, "agent": context.agent, "node": context.node,
            "operation": context.operation,
        }
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "error"
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_structured_logging() -> None:
    if os.getenv("OBSERVABILITY_JSON_LOGS", "false").casefold() != "true":
        return
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.setFormatter(JsonLogFormatter())
