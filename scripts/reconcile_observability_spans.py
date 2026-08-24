from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.observability_store import ObservabilityStore
from graph.checkpointing import checkpoint_database_path
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from streaming.sqlite_store import workflow_event_store_path


TERMINAL_TRACE_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_SPAN_STATUSES = {"running", "waiting", "interrupted"}


def _event_terminal_status(event_type: str, data_json: str) -> str:
    data = json.loads(data_json or "{}")
    if data.get("terminal_status") == "user_cancelled":
        return "cancelled"
    return "completed" if event_type == "workflow_completed" else "failed"


async def _checkpoint_terminal_states(
    database: Path,
    workflow_ids: set[str],
) -> dict[str, dict[str, str]]:
    if not database.exists() or not workflow_ids:
        return {}
    terminals: dict[str, dict[str, str]] = {}
    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        for workflow_id in workflow_ids:
            item = await saver.aget_tuple({"configurable": {"thread_id": workflow_id}})
            if item is None:
                continue
            values = item.checkpoint.get("channel_values", {})
            terminal_status = values.get("terminal_status")
            ended_at = item.checkpoint.get("ts")
            if not terminal_status or not ended_at:
                continue
            status = (
                "completed" if terminal_status == "completed"
                else "cancelled" if terminal_status == "user_cancelled"
                else "failed"
            )
            terminals[workflow_id] = {
                "status": status,
                "ended_at": str(ended_at),
            }
    return terminals


def reconcile(
    database: Path,
    *,
    dry_run: bool = True,
    checkpoint_database: Path | None = None,
    trace_id: str | None = None,
    terminal_status: str | None = None,
) -> dict[str, Any]:
    if (trace_id is None) != (terminal_status is None):
        raise ValueError("trace_id and terminal_status must be provided together")
    if terminal_status is not None and terminal_status not in TERMINAL_TRACE_STATUSES:
        raise ValueError("terminal_status must be completed, failed, or cancelled")
    store = ObservabilityStore(database)
    store.initialize_sync()
    candidates: dict[str, dict[str, Any]] = {}

    with store._connect() as connection:
        trace_sql = "SELECT trace_id,workflow_id,branch_id,status,ended_at FROM observability_traces"
        trace_parameters: tuple[str, ...] = ()
        if trace_id is not None:
            trace_sql += " WHERE trace_id=?"
            trace_parameters = (trace_id,)
        traces = connection.execute(trace_sql, trace_parameters).fetchall()
        active_counts = {
            row["trace_id"]: int(row["span_count"])
            for row in connection.execute(
                """SELECT trace_id,COUNT(*) AS span_count FROM observability_spans
                   WHERE status IN ('running','waiting')
                      OR (status='interrupted' AND ended_at IS NULL)
                   GROUP BY trace_id"""
            )
        }
        terminal_events: dict[tuple[str, str], dict[str, str]] = {}
        has_events = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_events'"
        ).fetchone()
        if has_events:
            rows = connection.execute(
                """SELECT thread_id,branch_id,event_type,timestamp,data_json
                   FROM workflow_events
                   WHERE event_type IN ('workflow_completed','workflow_failed')
                   ORDER BY sequence,created_at"""
            ).fetchall()
            for row in rows:
                terminal_events[(row["thread_id"], row["branch_id"] or "original")] = {
                    "status": _event_terminal_status(row["event_type"], row["data_json"]),
                    "ended_at": row["timestamp"],
                }

        active_original_workflows = {
            str(trace["workflow_id"])
            for trace in traces
            if active_counts.get(trace["trace_id"], 0)
            and trace["status"] not in TERMINAL_TRACE_STATUSES
            and (trace["branch_id"] or "original") == "original"
            and trace["workflow_id"]
        }
        checkpoint_terminals = asyncio.run(_checkpoint_terminal_states(
            checkpoint_database or checkpoint_database_path(),
            active_original_workflows,
        ))

        for trace in traces:
            active_spans = active_counts.get(trace["trace_id"], 0)
            if active_spans == 0:
                continue
            status = trace["status"]
            ended_at = trace["ended_at"]
            reason = "historical_terminal_trace_reconciliation"
            if trace_id is not None:
                status = str(terminal_status)
                ended_at = datetime.now(UTC).isoformat()
                reason = "explicit_terminal_trace_reconciliation"
            elif status not in TERMINAL_TRACE_STATUSES:
                terminal = terminal_events.get(
                    (trace["workflow_id"], trace["branch_id"] or "original")
                )
                if terminal is None and (trace["branch_id"] or "original") == "original":
                    terminal = checkpoint_terminals.get(str(trace["workflow_id"]))
                if terminal is None:
                    continue
                status = terminal["status"]
                ended_at = terminal["ended_at"]
                reason = "durable_terminal_workflow_reconciliation"
            candidates[trace["trace_id"]] = {
                "trace_id": trace["trace_id"],
                "workflow_id": trace["workflow_id"],
                "branch_id": trace["branch_id"],
                "status": status,
                "ended_at": ended_at,
                "active_spans": active_spans,
                "reason": reason,
            }

    result: dict[str, Any] = {
        "dry_run": dry_run,
        "candidate_traces": len(candidates),
        "candidate_spans": sum(item["active_spans"] for item in candidates.values()),
        "reconciled_traces": 0,
        "reconciled_spans": 0,
        "candidates": list(candidates.values()),
    }
    if dry_run:
        return result

    for candidate in candidates.values():
        outcome = store.finalize_trace_sync(
            candidate["trace_id"],
            status=candidate["status"],
            ended_at=candidate["ended_at"],
            reason=candidate["reason"],
        )
        if outcome.get("changed"):
            result["reconciled_traces"] += 1
            result["reconciled_spans"] += int(outcome.get("closed_spans", 0))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Close active observability traces backed by durable terminal workflows."
    )
    parser.add_argument("--database", type=Path, default=workflow_event_store_path())
    parser.add_argument(
        "--checkpoint-database", type=Path, default=checkpoint_database_path()
    )
    parser.add_argument("--trace-id")
    parser.add_argument(
        "--terminal-status", choices=sorted(TERMINAL_TRACE_STATUSES)
    )
    parser.add_argument(
        "--execute", action="store_true", help="Apply reconciliation; default is dry-run."
    )
    args = parser.parse_args()
    if bool(args.trace_id) != bool(args.terminal_status):
        parser.error("--trace-id and --terminal-status must be provided together")
    if not args.database.exists():
        parser.error(f"Database not found: {args.database}")
    print(json.dumps(reconcile(
        args.database,
        dry_run=not args.execute,
        checkpoint_database=args.checkpoint_database,
        trace_id=args.trace_id,
        terminal_status=args.terminal_status,
    ), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
