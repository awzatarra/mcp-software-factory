from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from api.llm_cost_models import PricingCreate
from api.services.llm_cost_store import LLMCostStore, PricingOverlapError
from streaming.sqlite_store import workflow_event_store_path


def load_records(path: Path) -> list[dict]:
    if path.suffix.casefold()==".csv":
        with path.open(encoding="utf-8",newline="") as handle:return list(csv.DictReader(handle))
    value=json.loads(path.read_text(encoding="utf-8"));return value if isinstance(value,list) else value.get("pricing",[])


async def import_pricing(database: Path,file: Path,*,dry_run:bool)->dict:
    store=LLMCostStore(database);await store.initialize();records=load_records(file)
    result={"dry_run":dry_run,"processed":len(records),"valid":0,"imported":0,"duplicates":0,"overlaps":0,"failed":0,"warnings":[]}
    seen=set()
    for raw in records:
        try:
            item=PricingCreate.model_validate(raw);key=(item.provider,item.model_pattern,item.currency,item.effective_from.isoformat())
            if key in seen:result["duplicates"]+=1;continue
            seen.add(key);result["valid"]+=1
            if not dry_run:await store.create_pricing(item.model_dump());result["imported"]+=1
        except PricingOverlapError as exc:result["overlaps"]+=1;result["warnings"].append(str(exc))
        except Exception as exc:result["failed"]+=1;result["warnings"].append(type(exc).__name__)
    return result


def main()->int:
    parser=argparse.ArgumentParser(description="Import versioned LLM pricing using Decimal validation.")
    parser.add_argument("--database",type=Path,default=workflow_event_store_path());parser.add_argument("--file",type=Path,required=True);parser.add_argument("--dry-run",action="store_true")
    args=parser.parse_args();print(json.dumps(asyncio.run(import_pricing(args.database,args.file,dry_run=args.dry_run)),indent=2));return 0
if __name__=="__main__":raise SystemExit(main())
