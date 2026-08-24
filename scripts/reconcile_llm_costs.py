from __future__ import annotations

import argparse
import asyncio
from datetime import UTC,datetime
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from api.services.llm_cost_service import LLMCostService
from api.services.llm_cost_store import LLMCostStore
from api.services.llm_finops_alert_lifecycle import LLMFinOpsAlertLifecycleService
from api.services.alert_service import AlertEvaluationService
from api.services.alert_store import AlertStore
from api.services.observability_store import ObservabilityStore
from streaming.sqlite_store import workflow_event_store_path


async def reconcile(database:Path,*,dry_run:bool=True,recalculate:bool=False)->dict:
    obs=ObservabilityStore(database);await obs.initialize();store=LLMCostStore(database);service=LLMCostService(store,obs);await service.initialize()
    alert_store=AlertStore(database);await alert_store.initialize();alerts=AlertEvaluationService(alert_store)
    lifecycle=LLMFinOpsAlertLifecycleService(store,obs,alerts);service.set_alert_service(alerts);service.set_finops_lifecycle(lifecycle)
    missing=await obs.fetch_all("""SELECT c.call_id FROM observability_llm_calls c LEFT JOIN llm_cost_calculations x ON x.llm_call_id=c.call_id AND x.superseded=0 WHERE x.cost_calculation_id IS NULL""")
    incomplete=await obs.fetch_all("SELECT llm_call_id FROM llm_cost_calculations WHERE superseded=0 AND (cost_source IS NULL OR pricing_snapshot_json IS NULL)")
    missing_pricing=await obs.fetch_all("""SELECT x.llm_call_id FROM llm_cost_calculations x
        LEFT JOIN llm_pricing_catalog p ON p.pricing_id=x.pricing_id
        WHERE x.superseded=0 AND x.pricing_id IS NOT NULL AND p.pricing_id IS NULL""")
    stale=await obs.fetch_all("SELECT llm_call_id FROM llm_cost_calculations WHERE superseded=0 AND cost_status='stale'")
    duplicate_calls=await obs.fetch_all("SELECT llm_call_id FROM llm_cost_calculations WHERE superseded=0 GROUP BY llm_call_id HAVING COUNT(*)>1")
    expired=await obs.fetch_all("SELECT reservation_id FROM llm_budget_reservations WHERE status='reserved' AND expires_at<=?",(datetime.now(UTC).isoformat(),))
    blocked_active=await obs.fetch_all("""SELECT r.llm_call_id,COUNT(*) AS active_reservation_count
        FROM llm_budget_reservations r
        WHERE r.status='reserved' AND EXISTS(
            SELECT 1 FROM llm_budget_events e
            WHERE e.llm_call_id=r.llm_call_id AND e.decision='block'
        ) GROUP BY r.llm_call_id""")
    consumption=await asyncio.to_thread(store.reconcile_reservation_consumption_sync,dry_run=True)
    unreconciled=await obs.fetch_all("""SELECT x.llm_call_id,x.total_cost,x.workflow_id
        FROM llm_cost_calculations x
        WHERE x.superseded=0 AND x.cost_status='calculated' AND x.total_cost IS NOT NULL
        AND CAST(x.total_cost AS NUMERIC)>0 AND NOT EXISTS(
            SELECT 1 FROM llm_budget_reservations r WHERE r.llm_call_id=x.llm_call_id
        ) ORDER BY x.calculated_at""")
    result={"dry_run":dry_run,"missing_calculations":len(missing),"missing_pricing_records":len(missing_pricing),
            "incomplete_snapshots":len(incomplete),"stale_calculations":len(stale),
            "duplicate_calls":len(duplicate_calls),"expired_reservations":len(expired),
            "blocked_calls":len(blocked_active),
            "blocked_active_reservations":sum(int(row["active_reservation_count"]) for row in blocked_active),
            "released_blocked_reservations":0,
            "consumption_candidates":consumption["candidate_reservations"],
            "consumption_state_repairs":consumption["state_repairs"],
            "consumption_audit_repairs":consumption["audit_repairs"],
            "new_consumed":0,"consumption_audit_events_created":0,
            "unreconciled":len(unreconciled),
            "unreconciled_call_ids":[row["llm_call_id"] for row in unreconciled],
            "alerts_to_resolve":[],"alerts_resolved":0,"reconciled":0,"failed":0}
    preview=await lifecycle.reevaluate_all_budget_alerts(reason="FinOps reconciliation",dry_run=True)
    result["alerts_to_resolve"]=preview["alerts_to_resolve"]
    if dry_run:return result
    for row in missing:
        try:await service.calculate_call(row["call_id"]);result["reconciled"]+=1
        except Exception:result["failed"]+=1
    if recalculate:
        for row in incomplete:
            try:await service.calculate_call(row["llm_call_id"],recalculate=True,reason="reconciliation",actor="script");result["reconciled"]+=1
            except Exception:result["failed"]+=1
    if expired:
        await asyncio.to_thread(store.expire_reservations_sync)
    for row in blocked_active:
        result["released_blocked_reservations"]+=await asyncio.to_thread(store.release_reservations_sync,row["llm_call_id"],"Reconciliation released reservation after aggregate block")
    repaired=await asyncio.to_thread(store.reconcile_reservation_consumption_sync,dry_run=False)
    result["new_consumed"]=repaired["new_consumed"]
    result["consumption_audit_events_created"]=repaired["audit_events_created"]
    resolved=await lifecycle.reevaluate_all_budget_alerts(reason="Reservation expired or released during reconciliation")
    result["alerts_resolved"]=resolved["alerts_resolved"]
    return result


def main()->int:
    parser=argparse.ArgumentParser(description="Reconcile LLM calculations and reservations.");parser.add_argument("--database",type=Path,default=workflow_event_store_path());parser.add_argument("--execute",action="store_true");parser.add_argument("--recalculate",action="store_true")
    args=parser.parse_args();print(json.dumps(asyncio.run(reconcile(args.database,dry_run=not args.execute,recalculate=args.recalculate)),indent=2));return 0
if __name__=="__main__":raise SystemExit(main())
