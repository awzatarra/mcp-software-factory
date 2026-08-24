from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.knowledge_store import KnowledgeStore
from streaming.sqlite_store import workflow_event_store_path


def inspect(database: Path) -> dict[str, Any]:
    if not database.exists():
        return {
            "database": str(database), "retrievals": 0, "complete": 0,
            "summary_only": 0, "safe_state_updates": 0, "reconstructed_results": 0,
        }
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "knowledge_retrievals" not in tables:
            return {
                "database": str(database), "retrievals": 0, "complete": 0,
                "summary_only": 0, "safe_state_updates": 0, "reconstructed_results": 0,
            }
        columns = {row[1] for row in connection.execute("PRAGMA table_info(knowledge_retrievals)")}
        has_results = "knowledge_retrieval_results" in tables
        rows = connection.execute("SELECT retrieval_id,result_count"
                                  + (",detail_state" if "detail_state" in columns else "")
                                  + " FROM knowledge_retrievals").fetchall()
        result_counts = {}
        if has_results:
            result_counts = dict(connection.execute(
                "SELECT retrieval_id,COUNT(*) FROM knowledge_retrieval_results GROUP BY retrieval_id"
            ).fetchall())
        complete = 0
        summary_only = 0
        safe_updates = 0
        for row in rows:
            durable_count = int(result_counts.get(row["retrieval_id"], 0))
            evidence_complete = int(row["result_count"]) == durable_count
            desired = "complete" if evidence_complete else "summary_only"
            current = row["detail_state"] if "detail_state" in columns else None
            complete += int(desired == "complete")
            summary_only += int(desired == "summary_only")
            safe_updates += int(current != desired)
        return {
            "database": str(database), "retrievals": len(rows), "complete": complete,
            "summary_only": summary_only, "safe_state_updates": safe_updates,
            "reconstructed_results": 0,
            "note": "Result identities are never reconstructed without durable result rows.",
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely reconcile Knowledge retrieval detail states without inventing provenance."
    )
    parser.add_argument("--database", type=Path, default=workflow_event_store_path())
    parser.add_argument("--apply", action="store_true", help="Apply schema and safe detail-state updates.")
    args = parser.parse_args()
    database = args.database.resolve()
    before = inspect(database)
    if args.apply:
        KnowledgeStore(database).initialize_sync()
    after = inspect(database)
    print(json.dumps({"dry_run": not args.apply, "before": before, "after": after}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
