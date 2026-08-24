from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fnmatch import fnmatchcase
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from api.services.observability_sanitizer import safe_json, sanitize_value


LLM_COST_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_pricing_catalog (
 pricing_id TEXT PRIMARY KEY, provider TEXT NOT NULL, model_pattern TEXT NOT NULL,
 model_canonical_name TEXT NULL, currency TEXT NOT NULL,
 input_price_per_million TEXT NULL, cached_input_price_per_million TEXT NULL,
 output_price_per_million TEXT NULL, reasoning_price_per_million TEXT NULL,
 audio_input_price_per_million TEXT NULL, audio_output_price_per_million TEXT NULL,
 image_pricing_json TEXT NULL, effective_from TEXT NOT NULL, effective_to TEXT NULL,
 source_type TEXT NOT NULL, source_reference TEXT NULL, source_verified_at TEXT NULL,
 enabled INTEGER NOT NULL, priority INTEGER NOT NULL, reasoning_in_completion INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS llm_cost_calculations (
 cost_calculation_id TEXT PRIMARY KEY, llm_call_id TEXT NOT NULL,
 trace_id TEXT NULL, workflow_id TEXT NULL, branch_id TEXT NOT NULL DEFAULT 'original',
 agent_name TEXT NULL, provider TEXT NULL, model TEXT NULL, operation TEXT NULL,
 pricing_id TEXT NULL, currency TEXT NULL, usage_source TEXT NOT NULL,
 cost_source TEXT NOT NULL, cost_status TEXT NOT NULL,
 prompt_tokens INTEGER NULL, cached_input_tokens INTEGER NULL,
 uncached_input_tokens INTEGER NULL, completion_tokens INTEGER NULL,
 reasoning_tokens INTEGER NULL, input_cost TEXT NULL, cached_input_cost TEXT NULL,
 output_cost TEXT NULL, reasoning_cost TEXT NULL, other_cost TEXT NULL,
 total_cost TEXT NULL, estimated_total_cost TEXT NULL, cache_savings TEXT NULL,
 calculation_version TEXT NOT NULL, calculated_at TEXT NOT NULL,
 pricing_effective_from TEXT NULL, pricing_snapshot_json TEXT NOT NULL DEFAULT '{}',
 warnings_json TEXT NOT NULL DEFAULT '[]', metadata_json TEXT NOT NULL DEFAULT '{}',
 superseded INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_cost_current_call
 ON llm_cost_calculations(llm_call_id) WHERE superseded=0;
CREATE TABLE IF NOT EXISTS llm_cost_recalculations (
 recalculation_id TEXT PRIMARY KEY, llm_call_id TEXT NOT NULL,
 previous_calculation_id TEXT NULL, new_calculation_id TEXT NOT NULL,
 reason TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_budgets (
 budget_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NULL,
 scope_type TEXT NOT NULL, scope_value TEXT NULL, currency TEXT NOT NULL,
 limit_amount TEXT NOT NULL, warning_percent TEXT NOT NULL,
 enforcement_mode TEXT NOT NULL, period_type TEXT NULL, period_start TEXT NULL,
 period_end TEXT NULL, reset_timezone TEXT NOT NULL, enabled INTEGER NOT NULL,
 include_estimated INTEGER NOT NULL, include_failed_calls INTEGER NOT NULL,
 include_retries INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NULL,
 metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS llm_budget_reservations (
 reservation_id TEXT PRIMARY KEY, budget_id TEXT NOT NULL, workflow_id TEXT NULL,
 branch_id TEXT NOT NULL DEFAULT 'original', llm_call_id TEXT NULL, agent_name TEXT NULL,
 estimated_amount TEXT NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, released_at TEXT NULL,
 consumed_amount TEXT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
 UNIQUE(budget_id,llm_call_id)
);
CREATE TABLE IF NOT EXISTS llm_budget_events (
 budget_event_id TEXT PRIMARY KEY, budget_id TEXT NOT NULL, workflow_id TEXT NULL,
 branch_id TEXT NOT NULL DEFAULT 'original', llm_call_id TEXT NULL,
 event_type TEXT NOT NULL, amount TEXT NULL, remaining_amount TEXT NULL,
 decision TEXT NOT NULL, reason_code TEXT NOT NULL, actor TEXT NULL,
 created_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_pricing_resolve ON llm_pricing_catalog(provider,currency,enabled,effective_from,effective_to,priority);
CREATE INDEX IF NOT EXISTS idx_cost_call ON llm_cost_calculations(llm_call_id,superseded);
CREATE INDEX IF NOT EXISTS idx_cost_workflow ON llm_cost_calculations(workflow_id,branch_id,calculated_at);
CREATE INDEX IF NOT EXISTS idx_cost_agent ON llm_cost_calculations(agent_name,calculated_at);
CREATE INDEX IF NOT EXISTS idx_cost_model ON llm_cost_calculations(provider,model,calculated_at);
CREATE INDEX IF NOT EXISTS idx_cost_source ON llm_cost_calculations(cost_source,cost_status);
CREATE INDEX IF NOT EXISTS idx_budget_scope ON llm_budgets(scope_type,scope_value,enabled);
CREATE INDEX IF NOT EXISTS idx_reservation_budget ON llm_budget_reservations(budget_id,status,expires_at);
CREATE INDEX IF NOT EXISTS idx_budget_event ON llm_budget_events(budget_id,created_at);
"""


MONEY_FIELDS = {
    "input_price_per_million", "cached_input_price_per_million",
    "output_price_per_million", "reasoning_price_per_million",
    "audio_input_price_per_million", "audio_output_price_per_million",
    "input_cost", "cached_input_cost", "output_cost", "reasoning_cost",
    "other_cost", "total_cost", "estimated_total_cost", "cache_savings",
    "limit_amount", "warning_percent", "estimated_amount", "consumed_amount",
    "amount", "remaining_amount",
}
JSON_FIELDS = {"metadata_json", "pricing_snapshot_json", "warnings_json", "image_pricing_json"}


class PricingOverlapError(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text_decimal(value: Any) -> str | None:
    return None if value is None else format(Decimal(str(value)), "f")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for field in JSON_FIELDS:
        if field in result:
            result[field.removesuffix("_json")] = json.loads(result.pop(field) or ("[]" if field == "warnings_json" else "{}"))
    for field in MONEY_FIELDS:
        if field in result and result[field] is not None:
            result[field] = str(result[field])
    for field in ("enabled", "include_estimated", "include_failed_calls", "include_retries", "reasoning_in_completion", "superseded"):
        if field in result:
            result[field] = bool(result[field])
    return result


class LLMCostStore:
    def __init__(self, database_path: Path | str, *, busy_timeout_ms: int = 5_000) -> None:
        self.database_path = Path(database_path)
        self.busy_timeout_ms = busy_timeout_ms

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(LLM_COST_SCHEMA)

    async def initialize(self) -> None:
        await asyncio.to_thread(self.initialize_sync)

    def create_pricing_sync(self, data: dict[str, Any]) -> dict[str, Any]:
        now = _now(); pricing_id = data.get("pricing_id") or uuid4().hex
        effective_from = str(data["effective_from"])
        effective_to = str(data["effective_to"]) if data.get("effective_to") else None
        with self._connect() as connection:
            overlap = connection.execute(
                """SELECT pricing_id FROM llm_pricing_catalog
                   WHERE provider=? AND model_pattern=? AND currency=? AND enabled=1
                   AND effective_from < COALESCE(?, '9999-12-31T23:59:59+00:00')
                   AND COALESCE(effective_to,'9999-12-31T23:59:59+00:00') > ?""",
                (data["provider"], data["model_pattern"], data["currency"], effective_to, effective_from),
            ).fetchone()
            if overlap:
                raise PricingOverlapError("Ambiguous pricing date range overlap")
            connection.execute(
                """INSERT INTO llm_pricing_catalog(
                   pricing_id,provider,model_pattern,model_canonical_name,currency,
                   input_price_per_million,cached_input_price_per_million,output_price_per_million,
                   reasoning_price_per_million,audio_input_price_per_million,audio_output_price_per_million,
                   image_pricing_json,effective_from,effective_to,source_type,
                   source_reference,source_verified_at,enabled,priority,reasoning_in_completion,
                   created_at,updated_at,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                (pricing_id, data["provider"], data["model_pattern"], data.get("model_canonical_name"),
                 data["currency"], *(_text_decimal(data.get(key)) for key in (
                    "input_price_per_million", "cached_input_price_per_million",
                    "output_price_per_million", "reasoning_price_per_million",
                    "audio_input_price_per_million", "audio_output_price_per_million")),
                 safe_json(data.get("image_pricing")),
                 effective_from, effective_to, data.get("source_type", "manual"),
                 sanitize_value(data.get("source_reference")), str(data["source_verified_at"]) if data.get("source_verified_at") else None,
                 int(data.get("enabled", True)), int(data.get("priority", 0)), int(data.get("reasoning_in_completion", True)),
                 now, safe_json(data.get("metadata"))),
            )
        return self.get_pricing_sync(pricing_id) or {}

    async def create_pricing(self, data: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.create_pricing_sync, data)

    def get_pricing_sync(self, pricing_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return _row(connection.execute("SELECT * FROM llm_pricing_catalog WHERE pricing_id=?", (pricing_id,)).fetchone())

    async def get_pricing(self, pricing_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_pricing_sync, pricing_id)

    def list_pricing_sync(self, *, provider=None, model=None, currency=None, enabled=None, limit=100, offset=0):
        clauses: list[str] = []; params: list[Any] = []
        for column, value in (("provider", provider), ("currency", currency)):
            if value is not None: clauses.append(f"{column}=?"); params.append(value)
        if enabled is not None: clauses.append("enabled=?"); params.append(int(enabled))
        if model: clauses.append("(model_pattern=? OR model_canonical_name=?)"); params.extend([model, model])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM llm_pricing_catalog{where}", params).fetchone()[0])
            rows = connection.execute(f"SELECT * FROM llm_pricing_catalog{where} ORDER BY provider,model_pattern,effective_from DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return [_row(row) for row in rows], total

    async def list_pricing(self, **kwargs):
        return await asyncio.to_thread(self.list_pricing_sync, **kwargs)

    def update_pricing_sync(self, pricing_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"effective_to", "source_reference", "source_verified_at", "enabled", "priority", "metadata"}
        nullable = {"effective_to", "source_reference", "source_verified_at", "metadata"}
        values = {
            key: value for key, value in updates.items()
            if key in allowed and (value is not None or key in nullable)
        }
        if not values: return self.get_pricing_sync(pricing_id)
        columns=[]; params=[]
        for key,value in values.items():
            column="metadata_json" if key=="metadata" else key
            if key=="metadata": value="null" if value is None else safe_json(value)
            elif key=="enabled": value=int(value)
            elif key in {"effective_to","source_verified_at"}: value=None if value is None else str(value)
            elif key=="source_reference": value=sanitize_value(value)
            columns.append(f"{column}=?"); params.append(value)
        columns.append("updated_at=?"); params.append(_now()); params.append(pricing_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM llm_pricing_catalog WHERE pricing_id=?", (pricing_id,)
            ).fetchone()
            if current is None:
                connection.rollback()
                return None
            effective_to = values.get("effective_to", current["effective_to"])
            effective_to = None if effective_to is None else str(effective_to)
            enabled = values.get("enabled", bool(current["enabled"]))
            if effective_to is not None and str(effective_to) <= str(current["effective_from"]):
                connection.rollback()
                raise ValueError("effective_to must be after effective_from")
            if enabled:
                overlap = connection.execute(
                    """SELECT pricing_id FROM llm_pricing_catalog
                       WHERE pricing_id<>? AND provider=? AND model_pattern=?
                       AND currency=? AND enabled=1
                       AND effective_from < COALESCE(?, '9999-12-31T23:59:59+00:00')
                       AND COALESCE(effective_to,'9999-12-31T23:59:59+00:00') > ?""",
                    (
                        pricing_id, current["provider"], current["model_pattern"],
                        current["currency"], effective_to, current["effective_from"],
                    ),
                ).fetchone()
                if overlap:
                    connection.rollback()
                    raise PricingOverlapError("Ambiguous pricing date range overlap")
            connection.execute(f"UPDATE llm_pricing_catalog SET {','.join(columns)} WHERE pricing_id=?", params)
            connection.commit()
        return self.get_pricing_sync(pricing_id)

    async def update_pricing(self, pricing_id: str, updates: dict[str, Any]):
        return await asyncio.to_thread(self.update_pricing_sync, pricing_id, updates)

    def resolve_pricing_sync(self, provider: str, model: str, timestamp: str, currency: str = "USD") -> dict[str, Any] | None:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM llm_pricing_catalog WHERE provider=? AND currency=? AND enabled=1
                   AND effective_from<=? AND (effective_to IS NULL OR effective_to>?)
                   ORDER BY priority DESC,effective_from DESC""",
                (provider, currency, timestamp, timestamp),
            ).fetchall()
        matches=[]
        for raw in rows:
            item=_row(raw); pattern=str(item["model_pattern"])
            if pattern == model:
                item["match_type"]="exact"; matches.append((0, -int(item["priority"]), item)); continue
            if fnmatchcase(model, pattern):
                item["match_type"]="pattern"; matches.append((1, -int(item["priority"]), item))
        return sorted(matches, key=lambda value: (value[0], value[1]))[0][2] if matches else None

    async def resolve_pricing(self, provider: str, model: str, timestamp: str, currency: str = "USD"):
        return await asyncio.to_thread(self.resolve_pricing_sync, provider, model, timestamp, currency)

    def save_calculation_sync(self, data: dict[str, Any], *, recalculate=False, reason=None, actor=None) -> dict[str, Any]:
        now=_now(); calculation_id=uuid4().hex
        money=("input_cost","cached_input_cost","output_cost","reasoning_cost","other_cost","total_cost","estimated_total_cost","cache_savings")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous=connection.execute("SELECT * FROM llm_cost_calculations WHERE llm_call_id=? AND superseded=0",(data["llm_call_id"],)).fetchone()
            if previous and not recalculate:
                connection.rollback(); return _row(previous) or {}
            if previous:
                connection.execute("UPDATE llm_cost_calculations SET superseded=1 WHERE cost_calculation_id=?",(previous["cost_calculation_id"],))
            connection.execute(
                """INSERT INTO llm_cost_calculations(
                cost_calculation_id,llm_call_id,trace_id,workflow_id,branch_id,agent_name,provider,model,operation,
                pricing_id,currency,usage_source,cost_source,cost_status,prompt_tokens,cached_input_tokens,
                uncached_input_tokens,completion_tokens,reasoning_tokens,input_cost,cached_input_cost,output_cost,
                reasoning_cost,other_cost,total_cost,estimated_total_cost,cache_savings,calculation_version,
                calculated_at,pricing_effective_from,pricing_snapshot_json,warnings_json,metadata_json,superseded)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (calculation_id, data["llm_call_id"], data.get("trace_id"), data.get("workflow_id"), data.get("branch_id","original"),
                 data.get("agent_name"),data.get("provider"),data.get("model"),data.get("operation"),data.get("pricing_id"),
                 data.get("currency"),data["usage_source"],data["cost_source"],data["cost_status"],data.get("prompt_tokens"),
                 data.get("cached_input_tokens"),data.get("uncached_input_tokens"),data.get("completion_tokens"),data.get("reasoning_tokens"),
                 *(_text_decimal(data.get(key)) for key in money),data.get("calculation_version","1.0"),now,data.get("pricing_effective_from"),
                 safe_json(data.get("pricing_snapshot")), safe_json(data.get("warnings") or []),
                 safe_json(data.get("metadata"))),
            )
            if previous:
                connection.execute("INSERT INTO llm_cost_recalculations VALUES(?,?,?,?,?,?,?)",(uuid4().hex,data["llm_call_id"],previous["cost_calculation_id"],calculation_id,sanitize_value(reason),sanitize_value(actor),now))
            connection.commit()
        return self.get_calculation_sync(data["llm_call_id"]) or {}

    async def save_calculation(self, data: dict[str, Any], **kwargs):
        return await asyncio.to_thread(self.save_calculation_sync, data, **kwargs)

    def get_calculation_sync(self, llm_call_id: str):
        with self._connect() as connection:
            return _row(connection.execute("SELECT * FROM llm_cost_calculations WHERE llm_call_id=? AND superseded=0",(llm_call_id,)).fetchone())

    async def get_calculation(self, llm_call_id: str):
        return await asyncio.to_thread(self.get_calculation_sync, llm_call_id)

    def create_budget_sync(self, data: dict[str, Any]) -> dict[str, Any]:
        budget_id=data.get("budget_id") or uuid4().hex; now=_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO llm_budgets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (budget_id,data["name"],sanitize_value(data.get("description")),str(data["scope_type"]),data.get("scope_value"),
                 data["currency"],_text_decimal(data["limit_amount"]),_text_decimal(data["warning_percent"]),str(data["enforcement_mode"]),
                 data.get("period_type"),str(data["period_start"]) if data.get("period_start") else None,str(data["period_end"]) if data.get("period_end") else None,
                 data.get("reset_timezone","UTC"),int(data.get("enabled",True)),int(data.get("include_estimated",False)),
                 int(data.get("include_failed_calls",True)), int(data.get("include_retries",True)),
                 now, None, safe_json(data.get("metadata"))),
            )
        return self.get_budget_sync(budget_id) or {}

    async def create_budget(self,data): return await asyncio.to_thread(self.create_budget_sync,data)

    def get_budget_sync(self,budget_id):
        with self._connect() as connection: return _row(connection.execute("SELECT * FROM llm_budgets WHERE budget_id=?",(budget_id,)).fetchone())
    async def get_budget(self,budget_id): return await asyncio.to_thread(self.get_budget_sync,budget_id)

    def list_budgets_sync(self,enabled=None):
        query="SELECT * FROM llm_budgets"; params=()
        if enabled is not None: query+=" WHERE enabled=?"; params=(int(enabled),)
        with self._connect() as connection: return [_row(row) for row in connection.execute(query+" ORDER BY created_at DESC",params)]
    async def list_budgets(self,enabled=None): return await asyncio.to_thread(self.list_budgets_sync,enabled)

    def update_budget_sync(self,budget_id,updates):
        allowed={"name","description","limit_amount","warning_percent","enforcement_mode","enabled","include_estimated","include_failed_calls","include_retries","metadata"}
        values={k:v for k,v in updates.items() if k in allowed and v is not None}
        if not values:return self.get_budget_sync(budget_id)
        columns=[];params=[]
        for key,value in values.items():
            column="metadata_json" if key=="metadata" else key
            if key in {"limit_amount","warning_percent"}:value=_text_decimal(value)
            elif key in {"enabled","include_estimated","include_failed_calls","include_retries"}:value=int(value)
            elif key=="metadata":value=safe_json(value)
            elif key=="description":value=sanitize_value(value)
            else:value=str(value)
            columns.append(f"{column}=?");params.append(value)
        columns.append("updated_at=?");params.extend([_now(),budget_id])
        with self._connect() as connection:connection.execute(f"UPDATE llm_budgets SET {','.join(columns)} WHERE budget_id=?",params)
        return self.get_budget_sync(budget_id)
    async def update_budget(self,budget_id,updates):return await asyncio.to_thread(self.update_budget_sync,budget_id,updates)

    def reset_budget_sync(self, budget_id: str, *, reason: str, actor: str):
        budget = self.get_budget_sync(budget_id)
        if budget is None:
            return None
        now = datetime.now(UTC)
        period_end = None
        if budget.get("period_start") and budget.get("period_end"):
            start = datetime.fromisoformat(str(budget["period_start"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(budget["period_end"]).replace("Z", "+00:00"))
            period_end = (now + (end - start)).isoformat()
        metadata = dict(budget.get("metadata") or {})
        metadata.update({"last_reset_reason": sanitize_value(reason), "last_reset_actor": sanitize_value(actor)})
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE llm_budgets SET enabled=1,period_start=?,period_end=?,metadata_json=?,updated_at=?
                   WHERE budget_id=?""",
                (now.isoformat(), period_end, safe_json(metadata), now.isoformat(), budget_id),
            )
            connection.execute(
                """UPDATE llm_budget_reservations SET status='released',released_at=?
                   WHERE budget_id=? AND status='reserved'""",
                (now.isoformat(), budget_id),
            )
        return self.get_budget_sync(budget_id)

    async def reset_budget(self, budget_id: str, *, reason: str, actor: str):
        return await asyncio.to_thread(self.reset_budget_sync, budget_id, reason=reason, actor=actor)

    def budget_usage_sync(self,budget_id):
        budget=self.get_budget_sync(budget_id)
        if not budget:return None
        period=[];period_params=[]
        if budget.get("period_start"):
            period.append("julianday(created_at)>=julianday(?)");period_params.append(budget["period_start"])
        if budget.get("period_end"):
            period.append("julianday(created_at)<julianday(?)");period_params.append(budget["period_end"])
        period_sql=(" AND "+" AND ".join(period)) if period else ""
        with self._connect() as connection:
            consumed_rows=connection.execute(
                f"""SELECT consumed_amount FROM llm_budget_reservations
                    WHERE budget_id=? AND currency=? AND status='consumed'
                    AND consumed_amount IS NOT NULL{period_sql}""",
                (budget_id,budget["currency"],*period_params),
            ).fetchall()
            reserved_rows=connection.execute(
                f"""SELECT estimated_amount FROM llm_budget_reservations
                    WHERE budget_id=? AND currency=? AND status='reserved'
                    AND expires_at>?{period_sql}""",
                (budget_id,budget["currency"],_now(),*period_params),
            ).fetchall()
        consumed=sum((Decimal(row[0]) for row in consumed_rows),Decimal(0));reserved_amount=sum((Decimal(row[0]) for row in reserved_rows),Decimal(0));limit=Decimal(budget["limit_amount"])
        return {"budget":budget,"consumed":format(consumed,"f"),"reserved":format(reserved_amount,"f"),"remaining":format(max(limit-consumed-reserved_amount,Decimal(0)),"f"),"percent":format((consumed/limit*100) if limit else Decimal(0),"f")}
    async def budget_usage(self,budget_id):return await asyncio.to_thread(self.budget_usage_sync,budget_id)

    def reserve_sync(self,budget,*,call_id,workflow_id,branch_id,agent_name,amount,currency,ttl_seconds):
        amount=Decimal(amount);now=datetime.now(UTC);expires=(now+timedelta(seconds=ttl_seconds)).isoformat()
        connection=self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing=connection.execute("SELECT * FROM llm_budget_reservations WHERE budget_id=? AND llm_call_id=?",(budget["budget_id"],call_id)).fetchone()
            if existing:connection.commit();return _row(existing)
            connection.execute("UPDATE llm_budget_reservations SET status='expired',released_at=? WHERE budget_id=? AND status='reserved' AND expires_at<=?",(_now(),budget["budget_id"],_now()))
            usage=self.budget_usage_sync(budget["budget_id"]);remaining=Decimal(usage["remaining"])
            status="reserved" if amount<=remaining else "rejected";reservation_id=uuid4().hex
            connection.execute("INSERT INTO llm_budget_reservations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(reservation_id,budget["budget_id"],workflow_id,branch_id,call_id,agent_name,_text_decimal(amount),currency,status,now.isoformat(),expires,None,None,"{}"))
            connection.commit();return _row(connection.execute("SELECT * FROM llm_budget_reservations WHERE reservation_id=?",(reservation_id,)).fetchone())
        except Exception:connection.rollback();raise
        finally:connection.close()

    def finalize_reservations_sync(self,call_id:str,consumed:Decimal|None):
        actual=None if consumed is None else Decimal(consumed);now=_now();finalized=[]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows=connection.execute(
                "SELECT * FROM llm_budget_reservations WHERE llm_call_id=? AND status='reserved'",
                (call_id,),
            ).fetchall()
            for row in rows:
                reservation=dict(row);estimated=Decimal(reservation["estimated_amount"])
                status="consumed" if actual is not None else "released"
                released_amount=max(estimated-actual,Decimal(0)) if actual is not None else estimated
                overage_amount=max(actual-estimated,Decimal(0)) if actual is not None else Decimal(0)
                metadata=json.loads(reservation.get("metadata_json") or "{}")
                metadata["finalization"]={
                    "actual_amount":_text_decimal(actual),
                    "estimated_amount":_text_decimal(estimated),
                    "released_amount":_text_decimal(released_amount),
                    "overage_amount":_text_decimal(overage_amount),
                    "finalized_at":now,
                }
                changed=connection.execute(
                    """UPDATE llm_budget_reservations
                       SET status=?,consumed_amount=?,released_at=?,metadata_json=?
                       WHERE reservation_id=? AND status='reserved'""",
                    (status,_text_decimal(actual),now,safe_json(metadata),reservation["reservation_id"]),
                ).rowcount
                if not changed:
                    continue
                if actual is not None:
                    connection.execute(
                        """INSERT INTO llm_budget_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (uuid4().hex,reservation["budget_id"],reservation.get("workflow_id"),
                         reservation.get("branch_id") or "original",call_id,
                         "budget_reservation_consumed",_text_decimal(actual),None,"allow",
                         "reservation_consumed",None,now,safe_json({
                             "reservation_id":reservation["reservation_id"],
                             "actual_amount":_text_decimal(actual),
                             "estimated_amount":_text_decimal(estimated),
                             "released_amount":_text_decimal(released_amount),
                             "overage_amount":_text_decimal(overage_amount),
                             "workflow_id":reservation.get("workflow_id"),
                         })),
                    )
                finalized.append({
                    "reservation_id":reservation["reservation_id"],
                    "budget_id":reservation["budget_id"],"status":status,
                    "actual_amount":_text_decimal(actual),
                    "estimated_amount":_text_decimal(estimated),
                    "released_amount":_text_decimal(released_amount),
                    "overage_amount":_text_decimal(overage_amount),
                })
            connection.commit()
        return {"llm_call_id":call_id,"finalized":finalized,"count":len(finalized)}

    def reconcile_reservation_consumption_sync(self, *, dry_run: bool = True):
        with self._connect() as connection:
            rows=connection.execute(
                """SELECT r.*,x.total_cost,x.cost_status
                   FROM llm_budget_reservations r
                   JOIN llm_cost_calculations x ON x.llm_call_id=r.llm_call_id
                    AND x.superseded=0
                   JOIN llm_budgets b ON b.budget_id=r.budget_id
                   WHERE x.cost_status='calculated' AND x.total_cost IS NOT NULL
                    AND CAST(x.total_cost AS NUMERIC)>0 AND x.currency=r.currency
                    AND r.status IN ('reserved','consumed')
                   ORDER BY r.created_at,r.reservation_id"""
            ).fetchall()
            candidates=[]
            for raw in rows:
                row=dict(raw)
                event=connection.execute(
                    """SELECT 1 FROM llm_budget_events WHERE budget_id=? AND llm_call_id=?
                       AND event_type='budget_reservation_consumed' LIMIT 1""",
                    (row["budget_id"],row["llm_call_id"]),
                ).fetchone()
                needs_state=row["status"]!="consumed" or row.get("consumed_amount") is None
                needs_event=event is None
                if needs_state or needs_event:
                    candidates.append({**row,"needs_state":needs_state,"needs_event":needs_event})
        result={"candidate_reservations":len(candidates),"state_repairs":sum(bool(row["needs_state"]) for row in candidates),"audit_repairs":sum(bool(row["needs_event"]) for row in candidates),"new_consumed":0,"audit_events_created":0,"reservation_ids":[row["reservation_id"] for row in candidates]}
        if dry_run:return result
        for row in candidates:
            if row["needs_state"]:
                applied=self.finalize_reservations_sync(row["llm_call_id"],Decimal(row["total_cost"]))
                result["new_consumed"]+=applied["count"]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in candidates:
                exists=connection.execute(
                    """SELECT 1 FROM llm_budget_events WHERE budget_id=? AND llm_call_id=?
                       AND event_type='budget_reservation_consumed' LIMIT 1""",
                    (row["budget_id"],row["llm_call_id"]),
                ).fetchone()
                if exists:continue
                actual=Decimal(row["total_cost"]);estimated=Decimal(row["estimated_amount"])
                released=max(estimated-actual,Decimal(0));overage=max(actual-estimated,Decimal(0))
                now=_now()
                connection.execute(
                    "INSERT INTO llm_budget_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid4().hex,row["budget_id"],row.get("workflow_id"),row.get("branch_id") or "original",
                     row["llm_call_id"],"budget_reservation_consumed",_text_decimal(actual),None,
                     "allow","reservation_consumed","reconciliation",now,safe_json({
                         "reservation_id":row["reservation_id"],"actual_amount":_text_decimal(actual),
                         "estimated_amount":_text_decimal(estimated),"released_amount":_text_decimal(released),
                         "overage_amount":_text_decimal(overage),"workflow_id":row.get("workflow_id"),
                         "reconciliation_reason":"durable_cost_with_correlated_reservation",
                     })),
                )
                result["audit_events_created"]+=1
            connection.commit()
        return result

    def expire_reservations_sync(self) -> dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT reservation_id,budget_id FROM llm_budget_reservations WHERE status='reserved' AND expires_at<=?",
                (now,),
            ).fetchall()
            connection.execute(
                "UPDATE llm_budget_reservations SET status='expired',released_at=? WHERE status='reserved' AND expires_at<=?",
                (now, now),
            )
        return {
            "expired_reservations": len(rows),
            "budget_ids": sorted({str(row["budget_id"]) for row in rows}),
        }

    def release_reservations_sync(self,call_id:str,reason:str)->int:
        with self._connect() as connection:
            rows=connection.execute("SELECT reservation_id,metadata_json FROM llm_budget_reservations WHERE llm_call_id=? AND status='reserved'",(call_id,)).fetchall()
            for row in rows:
                metadata=json.loads(row["metadata_json"] or "{}")
                metadata["release_reason"]=sanitize_value(reason)
                connection.execute("UPDATE llm_budget_reservations SET status='released',released_at=?,metadata_json=? WHERE reservation_id=? AND status='reserved'",(_now(),safe_json(metadata),row["reservation_id"]))
        return len(rows)

    def add_budget_event_sync(self,record):
        with self._connect() as connection:
            connection.execute("INSERT INTO llm_budget_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(uuid4().hex,record["budget_id"],record.get("workflow_id"),record.get("branch_id","original"),record.get("llm_call_id"),record["event_type"],_text_decimal(record.get("amount")),_text_decimal(record.get("remaining_amount")),record["decision"],record["reason_code"],sanitize_value(record.get("actor")),_now(),safe_json(record.get("metadata"))))
