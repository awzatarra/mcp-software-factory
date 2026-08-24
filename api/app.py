from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.dependencies import ApiServices, build_api_services
from api.routes.events import router as events_router
from api.routes.workflows import router as workflows_router
from api.routes.dashboard import router as dashboard_router
from api.routes.alerts import router as alerts_router
from api.routes.notifications import router as notifications_router
from api.routes.observability import router as observability_router
from api.routes.llm_costs import router as llm_costs_router
from api.routes.agent_evaluations import router as agent_evaluations_router
from api.routes.knowledge import router as knowledge_router
from api.routes.git import router as git_router
from api.routes.ci import router as ci_router
from api.services.observability_middleware import ObservabilityMiddleware
from api.services.structured_logging import configure_structured_logging

logger = logging.getLogger(__name__)
ServicesFactory = Callable[[], AsyncIterator[ApiServices]]
DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173,"
    "http://127.0.0.1:5173"
)


def parse_cors_origins(configured: str) -> list[str]:
    origins: list[str] = []
    seen: set[str] = set()
    for raw_origin in configured.split(","):
        candidate = raw_origin.strip()
        if not candidate or candidate == "*":
            continue
        try:
            parsed = urlsplit(candidate)
            _ = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            continue
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}".rstrip("/")
        if origin not in seen:
            seen.add(origin)
            origins.append(origin)
    return origins


def _cors_origins() -> list[str]:
    configured = os.getenv("API_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    return parse_cors_origins(configured)


def create_app(
    services_factory=build_api_services,
) -> FastAPI:
    configure_structured_logging()
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with services_factory() as services:
            application.state.services = services
            yield

    application = FastAPI(
        title="MCP Software Factory API",
        version="0.2.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )
    application.add_middleware(ObservabilityMiddleware)
    application.include_router(workflows_router)
    application.include_router(events_router)
    application.include_router(dashboard_router)
    application.include_router(alerts_router)
    application.include_router(notifications_router)
    application.include_router(observability_router)
    application.include_router(llm_costs_router)
    application.include_router(agent_evaluations_router)
    application.include_router(knowledge_router)
    application.include_router(git_router)
    application.include_router(ci_router)

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unexpected API failure", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return application


app = create_app()
