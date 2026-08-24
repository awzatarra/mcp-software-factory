from __future__ import annotations

from datetime import datetime
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class OptionalOtlpExporter:
    def __init__(self) -> None:
        self.endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        self.configured = bool(self.endpoint)
        self.available = False
        self.error: str | None = None
        self._tracer = None
        if not self.configured:
            return
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            headers = {}
            for item in os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "").split(","):
                key, separator, value = item.partition("=")
                if separator and key.strip():
                    headers[key.strip()] = value.strip()
            provider = TracerProvider(resource=Resource.create({
                "service.name": os.getenv("OTEL_SERVICE_NAME", "mcp-software-factory")
            }))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=self.endpoint, headers=headers)))
            self._tracer = provider.get_tracer("mcp-software-factory")
            self.available = True
        except Exception as exc:
            self.error = type(exc).__name__
            logger.warning("OTLP exporter unavailable; local telemetry remains active: %s", self.error)

    def emit_span(
        self, *, name: str, started_at: datetime, ended_at: datetime,
        status: str, attributes: dict[str, Any],
    ) -> None:
        if self._tracer is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode
            span = self._tracer.start_span(
                name, start_time=int(started_at.timestamp() * 1_000_000_000),
                attributes={key: value for key, value in attributes.items() if value is not None},
            )
            span.set_status(Status(StatusCode.ERROR if status == "failed" else StatusCode.OK))
            span.end(end_time=int(ended_at.timestamp() * 1_000_000_000))
        except Exception:
            logger.exception("OTLP span export failed; local telemetry remains active")
