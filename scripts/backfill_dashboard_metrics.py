from __future__ import annotations

import argparse
import asyncio
import json
import os

from api.dependencies import build_api_services


async def _run(batch_size: int) -> int:
    os.environ["DASHBOARD_SKIP_STARTUP_BACKFILL"] = "true"
    async with build_api_services() as services:
        if services.dashboard is None:
            print(json.dumps({"updated": 0, "skipped": 0, "failed": 1}))
            return 1
        result = await services.dashboard.backfill(batch_size=batch_size)
        print(json.dumps(result, sort_keys=True))
        return 1 if result["failed"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize durable dashboard metrics.")
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()
    return asyncio.run(_run(max(args.batch_size, 1)))


if __name__ == "__main__":
    raise SystemExit(main())
