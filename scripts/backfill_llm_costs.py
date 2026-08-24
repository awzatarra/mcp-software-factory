from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.llm_cost_service import LLMCostService
from api.services.llm_cost_store import LLMCostStore
from api.services.observability_store import ObservabilityStore
from streaming.sqlite_store import workflow_event_store_path


async def backfill(database: Path, *, dry_run: bool, batch_size: int = 500) -> dict[str, int | bool]:
    observability = ObservabilityStore(database); await observability.initialize()
    store = LLMCostStore(database); service = LLMCostService(store, observability); await service.initialize()
    counters: dict[str, int | bool] = {
        "dry_run": dry_run, "processed": 0, "calculated": 0, "estimated": 0,
        "usage_unavailable": 0, "pricing_unavailable": 0, "unchanged": 0, "failed": 0,
    }
    offset = 0
    while True:
        rows = await observability.fetch_all(
            """SELECT c.call_id,c.usage_available,c.input_tokens,c.output_tokens,c.total_tokens,
                      x.cost_calculation_id
               FROM observability_llm_calls c
               LEFT JOIN llm_cost_calculations x
                 ON x.llm_call_id=c.call_id AND x.superseded=0
               ORDER BY c.timestamp LIMIT ? OFFSET ?""",
            (batch_size, offset),
        )
        if not rows: break
        for row in rows:
            counters["processed"] += 1
            if row.get("cost_calculation_id"):
                counters["unchanged"] += 1; continue
            if dry_run:
                has_usage = bool(row.get("usage_available")) or any(
                    row.get(field) is not None
                    for field in ("input_tokens", "output_tokens", "total_tokens")
                )
                counters["pricing_unavailable" if has_usage else "usage_unavailable"] += 1
                continue
            try:
                result = await service.calculate_call(row["call_id"])
                source = result.get("cost_source") if result else "unavailable"
                if source == "calculated": counters["calculated"] += 1
                elif source == "estimated": counters["estimated"] += 1
                elif result and "usage_not_available" in result.get("warnings", []): counters["usage_unavailable"] += 1
                else: counters["pricing_unavailable"] += 1
            except Exception:
                counters["failed"] += 1
        offset += len(rows)
    return counters


def main() -> int:
    parser=argparse.ArgumentParser(description="Backfill durable LLM costs without inventing usage or pricing.")
    parser.add_argument("--database",type=Path,default=workflow_event_store_path())
    parser.add_argument("--batch-size",type=int,default=500)
    parser.add_argument("--dry-run",action="store_true")
    args=parser.parse_args()
    print(json.dumps(asyncio.run(backfill(args.database,dry_run=args.dry_run,batch_size=max(1,min(args.batch_size,5000)))),indent=2))
    return 0


if __name__ == "__main__": raise SystemExit(main())
