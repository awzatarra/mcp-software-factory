from __future__ import annotations

import argparse
from datetime import UTC, datetime
from fnmatch import fnmatchcase
import json
from pathlib import Path
import sqlite3
import asyncio
from concurrent.futures import ThreadPoolExecutor
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.observability_sanitizer import safe_json
from api.services.observability_store import ObservabilityStore
from streaming.sqlite_store import workflow_event_store_path


RECONCILIATION_REASON = "legacy_usage_columns_added_with_unavailable_defaults"
MIGRATION_SOURCE = "legacy_openai_responses_observability"
RAW_USAGE_VERSION = "legacy-openai-responses-usage-v1"


def _metadata(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _evidence(row: sqlite3.Row) -> tuple[bool, str]:
    tokens = (row["input_tokens"], row["output_tokens"], row["total_tokens"])
    if any(value is None for value in tokens):
        return False, "token_fields_incomplete"
    input_tokens, output_tokens, total_tokens = (int(value) for value in tokens)
    if min(input_tokens, output_tokens, total_tokens) < 0:
        return False, "negative_tokens"
    if total_tokens != input_tokens + output_tokens:
        return False, "total_tokens_inconsistent"
    cached = row["cached_tokens"]
    if cached is not None and (int(cached) < 0 or int(cached) > input_tokens):
        return False, "cached_tokens_inconsistent"
    reasoning = row["reasoning_tokens"]
    if reasoning is not None and int(reasoning) < 0:
        return False, "reasoning_tokens_inconsistent"
    operation = str(row["operation"] or "").casefold()
    expected_span = f"openai.responses.{operation}"
    if not (
        str(row["provider"] or "").casefold() == "openai"
        and operation in {"create", "parse"}
        and row["status"] == "completed"
        and row["span_name"] == expected_span
        and row["span_operation"] == expected_span
        and row["span_category"] == "llm"
        and row["span_kind"] == "client"
        and row["span_status"] == "completed"
    ):
        return False, "provider_span_evidence_missing"
    span_attributes = _metadata(row["span_attributes_json"])
    if str(span_attributes.get("provider") or "").casefold() != "openai":
        return False, "provider_span_attribute_missing"
    if row["model"] and span_attributes.get("model") != row["model"]:
        return False, "provider_span_model_mismatch"
    return True, "legacy_openai_response_usage_correlated"


def _pricing_exists(connection: sqlite3.Connection, row: sqlite3.Row, currency: str) -> bool:
    has_catalog = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_pricing_catalog'"
    ).fetchone()
    if not has_catalog:
        return False
    prices = connection.execute(
        """SELECT model_pattern FROM llm_pricing_catalog
           WHERE provider=? AND currency=? AND enabled=1
             AND effective_from<=? AND (effective_to IS NULL OR effective_to>?)""",
        (row["provider"], currency, row["timestamp"], row["timestamp"]),
    ).fetchall()
    return any(fnmatchcase(str(row["model"] or ""), item["model_pattern"]) for item in prices)


def reconcile_usage(database: Path, *, dry_run: bool = True) -> dict[str, Any]:
    ObservabilityStore(database).initialize_sync()
    connection = sqlite3.connect(database, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    rows = connection.execute(
        """SELECT c.*,s.name AS span_name,s.operation AS span_operation,
                  s.category AS span_category,s.kind AS span_kind,
                  s.status AS span_status,s.attributes_json AS span_attributes_json
           FROM observability_llm_calls c
           LEFT JOIN observability_spans s ON s.span_id=c.span_id
           WHERE c.usage_source='unavailable' AND c.usage_available=0
             AND (c.input_tokens IS NOT NULL OR c.output_tokens IS NOT NULL OR c.total_tokens IS NOT NULL)
           ORDER BY c.timestamp,c.call_id"""
    ).fetchall()
    repairable: list[tuple[sqlite3.Row, str]] = []
    rejected: list[dict[str, str]] = []
    for row in rows:
        safe, reason = _evidence(row)
        if safe:
            repairable.append((row, reason))
        else:
            rejected.append({"call_id": row["call_id"], "reason": reason})

    result: dict[str, Any] = {
        "dry_run": dry_run,
        "calls_with_tokens_but_unavailable": len(rows),
        "repairable": len(repairable),
        "repaired": 0,
        "not_repairable": len(rejected),
        "rejected": rejected,
    }
    if dry_run or not repairable:
        connection.close()
        return result

    now = datetime.now(UTC).isoformat()
    has_costs = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_cost_calculations'"
    ).fetchone() is not None
    try:
        connection.execute("BEGIN IMMEDIATE")
        for row, evidence_reason in repairable:
            repair = {
                "migration_source": MIGRATION_SOURCE,
                "reconciliation_reason": RECONCILIATION_REASON,
                "evidence": evidence_reason,
                "reconciled_at": now,
            }
            attributes = _metadata(row["attributes_json"])
            attributes["normalization_repair"] = repair
            cursor = connection.execute(
                """UPDATE observability_llm_calls
                   SET usage_source='provider_reported',usage_available=1,
                       raw_usage_version=?,attributes_json=?
                   WHERE call_id=? AND usage_source='unavailable' AND usage_available=0""",
                (RAW_USAGE_VERSION, safe_json(attributes), row["call_id"]),
            )
            if cursor.rowcount != 1:
                continue
            if has_costs:
                calculation = connection.execute(
                    """SELECT cost_calculation_id,metadata_json,warnings_json,currency,cost_source
                       FROM llm_cost_calculations
                       WHERE llm_call_id=? AND superseded=0""",
                    (row["call_id"],),
                ).fetchone()
                if calculation:
                    metadata = _metadata(calculation["metadata_json"])
                    metadata["normalization_repair"] = repair
                    warnings = json.loads(calculation["warnings_json"] or "[]")
                    cost_status_update = None
                    if calculation["cost_source"] == "unavailable" and "usage_not_available" in warnings:
                        if _pricing_exists(connection, row, calculation["currency"] or "USD"):
                            warnings = ["usage_repaired_recalculation_required"]
                            cost_status_update = "stale"
                        else:
                            warnings = ["pricing_not_found"]
                            metadata["reason"] = "pricing_not_found"
                    connection.execute(
                        """UPDATE llm_cost_calculations
                           SET usage_source='provider_reported',metadata_json=?,warnings_json=?,
                               cost_status=COALESCE(?,cost_status)
                           WHERE cost_calculation_id=?""",
                        (safe_json(metadata), safe_json(warnings), cost_status_update,
                         calculation["cost_calculation_id"]),
                    )
            result["repaired"] += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if result["repaired"]:
        async def resolve_usage_alerts() -> int:
            from api.services.alert_service import AlertEvaluationService
            from api.services.alert_store import AlertStore
            from api.services.llm_cost_store import LLMCostStore
            from api.services.llm_finops_alert_lifecycle import LLMFinOpsAlertLifecycleService

            alert_store = AlertStore(database)
            await alert_store.initialize()
            observability = ObservabilityStore(database)
            await observability.initialize()
            costs = LLMCostStore(database)
            await costs.initialize()
            lifecycle = LLMFinOpsAlertLifecycleService(
                costs, observability, AlertEvaluationService(alert_store),
            )
            return await lifecycle.reevaluate_usage_alerts(reason="Usage reconciliation made provider usage available")

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            result["alerts_resolved"] = asyncio.run(resolve_usage_alerts())
        else:
            with ThreadPoolExecutor(max_workers=1) as executor:
                result["alerts_resolved"] = executor.submit(lambda: asyncio.run(resolve_usage_alerts())).result()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair legacy LLM usage classification only when durable provider evidence exists."
    )
    parser.add_argument("--database", type=Path, default=workflow_event_store_path())
    parser.add_argument("--execute", action="store_true", help="Apply safe repairs; dry-run is the default.")
    args = parser.parse_args()
    print(json.dumps(reconcile_usage(args.database, dry_run=not args.execute), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
