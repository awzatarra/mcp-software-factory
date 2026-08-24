from __future__ import annotations

from typing import Any


TERMINAL_TRACE_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_SPAN_STATUSES = {"running", "waiting"}


def validate_span_hierarchy(
    trace: dict[str, Any],
    spans: list[dict[str, Any]],
) -> dict[str, Any]:
    by_id = {str(span["span_id"]): span for span in spans}
    roots = [span for span in spans if span.get("parent_span_id") is None]
    orphans = [
        span for span in spans
        if span.get("parent_span_id") is not None
        and str(span["parent_span_id"]) not in by_id
    ]
    cycle_nodes: set[str] = set()
    max_depth = 0
    for span_id in by_id:
        visited: dict[str, int] = {}
        current: str | None = span_id
        depth = 0
        while current is not None and current in by_id:
            if current in visited:
                cycle_nodes.update(list(visited)[visited[current]:])
                break
            visited[current] = depth
            depth += 1
            parent = by_id[current].get("parent_span_id")
            current = str(parent) if parent is not None else None
        max_depth = max(max_depth, depth)

    allowed_parents = {
        "subgraph": {"workflow"},
        "node": {"subgraph"},
        "agent": {"node"},
        "llm": {"agent"},
        "mcp": {"agent", "node", "tool"},
    }
    relationship_violations: list[dict[str, str | None]] = []
    for span in spans:
        allowed = allowed_parents.get(str(span.get("category")))
        if allowed is None:
            continue
        parent = by_id.get(str(span.get("parent_span_id")))
        parent_category = str(parent.get("category")) if parent else None
        if parent_category not in allowed:
            relationship_violations.append({
                "span_id": str(span["span_id"]),
                "category": str(span.get("category")),
                "parent_category": parent_category,
            })

    terminal_active = sum(
        1 for span in spans if span.get("status") in ACTIVE_SPAN_STATUSES
    )
    active_span_count = sum(
        1
        for span in spans
        if span.get("status") in ACTIVE_SPAN_STATUSES
        or (span.get("status") == "interrupted" and span.get("ended_at") is None)
    )
    attributes = trace.get("attributes") or {}
    precision = str(attributes.get("precision") or "full")
    hierarchy_degraded = bool(
        attributes.get("hierarchy_degraded")
        or precision == "partial"
        or orphans
        or cycle_nodes
        or relationship_violations
    )
    valid = (
        len(roots) == 1
        and not orphans
        and not cycle_nodes
        and not (
            trace.get("status") in TERMINAL_TRACE_STATUSES
            and terminal_active > 0
        )
        and not relationship_violations
    )
    return {
        "valid": valid,
        "root_count": len(roots),
        "orphan_count": len(orphans),
        "cycle_count": len(cycle_nodes),
        "max_depth": max_depth,
        "active_span_count": active_span_count,
        "terminal_active_spans": terminal_active,
        "relationship_violation_count": len(relationship_violations),
        "relationship_violations": relationship_violations,
        "precision": precision,
        "hierarchy_degraded": hierarchy_degraded,
    }
