from __future__ import annotations

from dataclasses import dataclass
import os
import random
from typing import Callable, Mapping
from urllib.parse import urlsplit


DEFAULT_IGNORED_ROUTES = frozenset({
    "/docs",
    "/redoc",
    "/openapi.json",
    "/health",
    "/favicon.ico",
})
DEFAULT_IGNORED_PREFIXES = ("/api/observability", "/docs")
DEFAULT_POLLING_ROUTES = frozenset({
    "/api/alerts/summary",
    "/api/notifications/summary",
    "/api/dashboard/summary",
    "/api/dashboard/activity",
    "/api/dashboard/attention",
    "/api/dashboard/agents",
    "/api/dashboard/timeseries",
})


def normalize_route_path(path: str) -> str:
    value = urlsplit(path.strip()).path.replace("\\", "/") or "/"
    if not value.startswith("/"):
        value = f"/{value}"
    while "//" in value:
        value = value.replace("//", "/")
    return value.rstrip("/") or "/"


def _configured_paths(value: str | None) -> set[str]:
    return {normalize_route_path(item) for item in (value or "").split(",") if item.strip()}


def _bool_setting(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class RouteObservationDecision:
    route: str
    observe: bool
    reason: str
    capture_ignored_errors: bool


@dataclass(frozen=True, slots=True)
class ObservabilityRoutePolicy:
    ignored_routes: frozenset[str]
    ignored_prefixes: tuple[str, ...]
    polling_routes: frozenset[str]
    polling_sample_rate: float
    capture_ignored_errors: bool
    sampler: Callable[[], float] = random.random

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        sampler: Callable[[], float] = random.random,
    ) -> "ObservabilityRoutePolicy":
        env = os.environ if environment is None else environment
        legacy_routes = _configured_paths(env.get("OBSERVABILITY_IGNORED_PATHS"))
        ignored_routes = DEFAULT_IGNORED_ROUTES | legacy_routes | _configured_paths(
            env.get("OBSERVABILITY_IGNORED_ROUTES")
        )
        prefixes = set(DEFAULT_IGNORED_PREFIXES)
        prefixes.update(_configured_paths(env.get("OBSERVABILITY_IGNORED_PREFIXES")))
        try:
            sample_rate = float(env.get("OBSERVABILITY_POLLING_SAMPLE_RATE", "0"))
        except ValueError:
            sample_rate = 0.0
        return cls(
            ignored_routes=frozenset(ignored_routes),
            ignored_prefixes=tuple(sorted(prefixes)),
            polling_routes=DEFAULT_POLLING_ROUTES,
            polling_sample_rate=max(0.0, min(1.0, sample_rate)),
            capture_ignored_errors=_bool_setting(
                env.get("OBSERVABILITY_CAPTURE_IGNORED_ERRORS"), True
            ),
            sampler=sampler,
        )

    def is_ignored_route(self, route: str) -> bool:
        normalized = normalize_route_path(route)
        if normalized in self.ignored_routes:
            return True
        return any(
            normalized == prefix or normalized.startswith(f"{prefix}/")
            for prefix in self.ignored_prefixes
        )

    def is_polling_route(self, route: str, method: str = "GET") -> bool:
        return method.upper() in {"GET", "HEAD"} and normalize_route_path(route) in self.polling_routes

    def is_noise_route(self, route: str, method: str = "GET") -> bool:
        normalized_method = method.upper()
        if normalized_method not in {"GET", "HEAD", "OPTIONS"}:
            return False
        return self.is_ignored_route(route) or self.is_polling_route(route, normalized_method)

    def decide(self, route: str, method: str) -> RouteObservationDecision:
        normalized = normalize_route_path(route)
        normalized_method = method.upper()
        if normalized_method == "OPTIONS":
            return RouteObservationDecision(normalized, False, "preflight", False)
        if normalized_method not in {"GET", "HEAD"}:
            return RouteObservationDecision(normalized, True, "critical_write", False)
        if self.is_ignored_route(normalized):
            return RouteObservationDecision(
                normalized, False, "ignored_route", self.capture_ignored_errors
            )
        if self.is_polling_route(normalized, normalized_method):
            observe = self.polling_sample_rate > 0 and self.sampler() < self.polling_sample_rate
            return RouteObservationDecision(
                normalized,
                observe,
                "polling_sampled" if observe else "polling_not_sampled",
                self.capture_ignored_errors,
            )
        return RouteObservationDecision(normalized, True, "observed", False)
