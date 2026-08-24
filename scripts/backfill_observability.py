from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.observability_service import ObservabilityService
from api.services.observability_store import ObservabilityStore
from streaming.models import WorkflowEvent
from streaming.sqlite_store import workflow_event_store_path


def load_events(database_path: Path) -> list[WorkflowEvent]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM workflow_events ORDER BY timestamp, sequence").fetchall()
    finally:
        connection.close()
    events = []
    for row in rows:
        events.append(WorkflowEvent.model_validate({
            "event_id": row["event_id"], "thread_id": row["thread_id"], "sequence": row["sequence"],
            "type": row["event_type"], "timestamp": row["timestamp"], "source": row["source"],
            "stage": row["stage"], "status": row["status"], "message": row["message"],
            "data": json.loads(row["data_json"] or "{}"),
        }))
    return events


async def run(*, database_path: Path, dry_run: bool) -> dict[str, int | bool]:
    events = load_events(database_path)
    if dry_run:
        return {"dry_run": True, "events_scanned": len(events), "events_processed": 0}
    os.environ["OBSERVABILITY_RECONCILE_ON_STARTUP"] = "false"
    service = ObservabilityService(ObservabilityStore(database_path))
    await service.initialize()
    processed = 0
    for event in events:
        processed += int(await service.ingest_workflow_event(event, source="backfill"))
    return {"dry_run": False, "events_scanned": len(events), "events_processed": processed}


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill durable observability from workflow events.")
    parser.add_argument("--database", type=Path, default=workflow_event_store_path())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.database.exists():
        parser.error(f"Database not found: {args.database}")
    print(json.dumps(asyncio.run(run(database_path=args.database, dry_run=args.dry_run)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
