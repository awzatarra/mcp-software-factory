from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.observability_route_policy import ObservabilityRoutePolicy
from streaming.sqlite_store import workflow_event_store_path


TRACE_CHILD_TABLES = (
    "observability_span_events",
    "observability_metrics",
    "observability_logs",
    "observability_llm_calls",
    "observability_tool_calls",
    "observability_artifacts",
    "observability_spans",
)


def _bool_setting(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _route_and_method(row: sqlite3.Row) -> tuple[str, str]:
    try:
        attributes = json.loads(row["attributes_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        attributes = {}
    route = attributes.get("http.route")
    method = attributes.get("http.method") or row["operation"] or "GET"
    if not route:
        parts = str(row["name"] or "").split(" ", 1)
        route = parts[1] if len(parts) == 2 else ""
        method = parts[0] if len(parts) == 2 else method
    return str(route), str(method)


def cleanup_noise(
    database_path: Path,
    *,
    dry_run: bool = True,
    preserve_errors: bool = True,
    policy: ObservabilityRoutePolicy | None = None,
) -> dict[str, Any]:
    route_policy = policy or ObservabilityRoutePolicy.from_environment()
    connection = sqlite3.connect(database_path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "observability_traces" not in tables or "observability_spans" not in tables:
            return {
                "dry_run": dry_run,
                "candidate_traces": 0,
                "candidate_spans": 0,
                "preserved_error_traces": 0,
                "deleted_traces": 0,
                "deleted_spans": 0,
            }
        rows = connection.execute(
            """SELECT t.trace_id, t.status AS trace_status, s.status AS span_status,
                      s.name, s.operation, s.attributes_json
               FROM observability_traces AS t
               JOIN observability_spans AS s ON s.span_id = t.root_span_id
               WHERE t.workflow_id IS NULL
                 AND t.source IN ('api', 'api_ignored_error')
                 AND s.category = 'api'"""
        ).fetchall()
        candidate_ids: list[str] = []
        preserved_errors = 0
        for row in rows:
            route, method = _route_and_method(row)
            if not route_policy.is_noise_route(route, method):
                continue
            is_error = row["trace_status"] == "failed" or row["span_status"] == "failed"
            if preserve_errors and is_error:
                preserved_errors += 1
                continue
            candidate_ids.append(row["trace_id"])

        candidate_spans = 0
        if candidate_ids:
            placeholders = ",".join("?" for _ in candidate_ids)
            candidate_spans = connection.execute(
                f"SELECT COUNT(*) FROM observability_spans WHERE trace_id IN ({placeholders})",
                candidate_ids,
            ).fetchone()[0]

        result = {
            "dry_run": dry_run,
            "candidate_traces": len(candidate_ids),
            "candidate_spans": candidate_spans,
            "preserved_error_traces": preserved_errors,
            "deleted_traces": 0,
            "deleted_spans": 0,
        }
        if dry_run or not candidate_ids:
            return result

        placeholders = ",".join("?" for _ in candidate_ids)
        connection.execute("BEGIN IMMEDIATE")
        for table in TRACE_CHILD_TABLES:
            if table in tables:
                connection.execute(
                    f"DELETE FROM {table} WHERE trace_id IN ({placeholders})", candidate_ids
                )
        connection.execute(
            f"DELETE FROM observability_traces WHERE trace_id IN ({placeholders})", candidate_ids
        )
        connection.commit()
        result["deleted_traces"] = len(candidate_ids)
        result["deleted_spans"] = candidate_spans
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove historical API polling noise from durable observability."
    )
    parser.add_argument("--database", type=Path, default=workflow_event_store_path())
    parser.add_argument(
        "--execute", action="store_true", help="Delete candidates; the default is dry-run."
    )
    parser.add_argument(
        "--include-errors",
        action="store_true",
        help="Also remove failed ignored-route traces. Errors are preserved by default.",
    )
    args = parser.parse_args()
    if not args.database.exists():
        parser.error(f"Database not found: {args.database}")
    preserve_errors = _bool_setting(
        os.getenv("OBSERVABILITY_CLEANUP_PRESERVE_ERRORS"), True
    ) and not args.include_errors
    result = cleanup_noise(
        args.database,
        dry_run=not args.execute,
        preserve_errors=preserve_errors,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
