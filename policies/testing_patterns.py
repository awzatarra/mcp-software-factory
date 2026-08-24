from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TestingPatternPolicy:
    framework: str
    preferred_sync_client: str
    allowed_sync_imports: tuple[str, ...]
    allowed_async_imports: tuple[str, ...]
    forbidden_patterns: tuple[str, ...]


FASTAPI_TESTING_POLICY = TestingPatternPolicy(
    framework="fastapi",
    preferred_sync_client="fastapi.testclient.TestClient",
    allowed_sync_imports=(
        "from fastapi.testclient import TestClient",
    ),
    allowed_async_imports=(
        "from httpx import ASGITransport, AsyncClient",
        "from httpx import AsyncClient, ASGITransport",
    ),
    forbidden_patterns=(
        "Client(app=",
        "AsyncClient(app=",
    ),
)

