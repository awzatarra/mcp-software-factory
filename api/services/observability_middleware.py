from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.routing import Match

from api.services.observability_context import ObservabilityContext, observability_context
from api.services.observability_route_policy import ObservabilityRoutePolicy, normalize_route_path
from api.services.observability_sanitizer import error_fingerprint, stable_hash


class ObservabilityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, route_policy: ObservabilityRoutePolicy | None = None) -> None:
        super().__init__(app)
        self.route_policy = route_policy or ObservabilityRoutePolicy.from_environment()

    async def dispatch(self, request: Request, call_next):
        service = getattr(getattr(request.app.state, "services", None), "observability", None)
        if service is None:
            return await call_next(request)
        route = self._resolve_route(request)
        decision = self.route_policy.decide(route, request.method)
        if not decision.observe:
            return await self._handle_ignored_request(
                request, call_next, service, decision.route, decision.capture_ignored_errors
            )
        return await self._handle_observed_request(request, call_next, service, decision.route)

    @staticmethod
    def _resolve_route(request: Request) -> str:
        resolved = request.scope.get("route")
        route_path = getattr(resolved, "path", None)
        if route_path:
            return normalize_route_path(route_path)
        for candidate in request.app.routes:
            match, _ = candidate.matches(request.scope)
            if match is Match.FULL:
                return normalize_route_path(getattr(candidate, "path", request.url.path))
        return normalize_route_path(request.url.path)

    async def _handle_ignored_request(self, request, call_next, service, route, capture_errors):
        started = datetime.now(UTC)
        try:
            response = await call_next(request)
        except Exception as exc:
            if capture_errors:
                await self._persist_ignored_error(service, request, route, started, exc=exc)
            raise
        if capture_errors and response.status_code >= 500:
            trace_id, span_id = await self._persist_ignored_error(
                service, request, route, started, status_code=response.status_code
            )
            if trace_id and span_id:
                response.headers["traceparent"] = f"00-{trace_id}-{span_id}-01"
        return response

    async def _handle_observed_request(self, request, call_next, service, route):
        incoming = request.headers.get("traceparent", "").split("-")
        trace_id = incoming[1] if len(incoming) == 4 and len(incoming[1]) == 32 else uuid4().hex
        parent_id = incoming[2] if len(incoming) == 4 and len(incoming[2]) == 16 else None
        span_id = stable_hash(uuid4().hex)[:16]
        started = datetime.now(UTC)
        try:
            await service.store.create_trace({"trace_id": trace_id, "name": "api.request", "status": "running", "source": "api", "started_at": started.isoformat(), "root_span_id": span_id})
            await service.store.start_span({"span_id": span_id, "trace_id": trace_id, "parent_span_id": parent_id, "name": f"{request.method} {route}", "category": "api", "kind": "server", "status": "running", "operation": request.method, "started_at": started.isoformat(), "attributes": {"http.method": request.method, "http.route": route}})
        except Exception:
            return await call_next(request)
        context = ObservabilityContext(trace_id=trace_id, span_id=span_id, operation=request.method)
        failure = None
        try:
            with observability_context(context): response = await call_next(request)
            status = "failed" if response.status_code >= 500 else "completed"
            response.headers["traceparent"] = f"00-{trace_id}-{span_id}-01"
            return response
        except Exception as exc:
            failure, status = exc, "failed"
            raise
        finally:
            ended = datetime.now(UTC); duration = (ended - started).total_seconds() * 1000
            await service._safe(service.store.end_span, span_id, status=status, ended_at=ended.isoformat(), duration_ms=duration, error_type=type(failure).__name__ if failure else None, error_message=str(failure)[:1000] if failure else None, error_fingerprint=error_fingerprint(type(failure).__name__, str(failure), request.method) if failure else None, is_slow=duration >= service.slow_span_threshold_ms)
            await service._safe(service.store.end_trace, trace_id, status=status, ended_at=ended.isoformat(), duration_ms=duration)

    async def _persist_ignored_error(
        self, service, request, route, started, *, exc=None, status_code=None
    ) -> tuple[str | None, str | None]:
        trace_id = uuid4().hex
        span_id = stable_hash(uuid4().hex)[:16]
        ended = datetime.now(UTC)
        duration = (ended - started).total_seconds() * 1000
        error_type = type(exc).__name__ if exc else f"HTTP{status_code}"
        error_message = str(exc)[:1000] if exc else f"Ignored route returned HTTP {status_code}"
        try:
            await service.store.create_trace({
                "trace_id": trace_id,
                "name": "api.ignored_error",
                "status": "running",
                "source": "api_ignored_error",
                "started_at": started.isoformat(),
                "root_span_id": span_id,
                "attributes": {"observability.capture_reason": "ignored_route_error"},
            })
            await service.store.start_span({
                "span_id": span_id,
                "trace_id": trace_id,
                "name": f"{request.method} {route}",
                "category": "api",
                "kind": "server",
                "status": "running",
                "operation": request.method,
                "started_at": started.isoformat(),
                "attributes": {
                    "http.method": request.method,
                    "http.route": route,
                    "http.status_code": status_code,
                    "observability.capture_reason": "ignored_route_error",
                },
            })
            await service.store.end_span(
                span_id,
                status="failed",
                ended_at=ended.isoformat(),
                duration_ms=duration,
                error_type=error_type,
                error_message=error_message,
                error_fingerprint=error_fingerprint(error_type, error_message, request.method),
                is_slow=duration >= service.slow_span_threshold_ms,
            )
            await service.store.end_trace(
                trace_id, status="failed", ended_at=ended.isoformat(), duration_ms=duration
            )
            return trace_id, span_id
        except Exception:
            return None, None
