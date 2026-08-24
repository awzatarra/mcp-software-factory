from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.agent_evaluation_store import AgentEvaluationStore
from streaming.sqlite_store import workflow_event_store_path


async def reconcile(database: Path, *, dry_run: bool = True, run_ids: list[str] | None = None) -> dict:
    store = AgentEvaluationStore(database)
    await store.initialize()
    return await store.reconcile_run_rubrics(dry_run=dry_run, run_ids=run_ids)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile durable evaluation run to rubric-version bindings.")
    parser.add_argument("--database", type=Path, default=workflow_event_store_path())
    parser.add_argument("--run-id", action="append", dest="run_ids")
    parser.add_argument("--execute", action="store_true", help="Persist safe bindings; dry-run is the default.")
    args = parser.parse_args()
    result = asyncio.run(reconcile(args.database, dry_run=not args.execute, run_ids=args.run_ids))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
