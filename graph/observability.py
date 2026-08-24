from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


@asynccontextmanager
async def observed_subgraph(
    dependencies: Any,
    stage: str,
) -> AsyncIterator[None]:
    service = getattr(dependencies, "observability", None)
    if service is None:
        yield
        return
    async with service.span(
        f"subgraph.{stage}.runtime",
        category="subgraph",
        attributes={"stage": stage, "source": "runtime"},
        subgraph=stage,
    ):
        yield


@asynccontextmanager
async def observed_node(
    dependencies: Any,
    stage: str,
    node: str,
    *,
    agent: str | None = None,
) -> AsyncIterator[None]:
    service = getattr(dependencies, "observability", None)
    if service is None:
        yield
        return
    async with service.span(
        f"node.{node}",
        category="node",
        attributes={"stage": stage, "node": node, "source": "runtime"},
        node=node,
    ):
        if agent is None:
            yield
            return
        async with service.span(
            f"agent.{agent}",
            category="agent",
            attributes={"stage": stage, "node": node, "agent": agent, "source": "runtime"},
            agent=agent,
            node=node,
        ):
            yield
