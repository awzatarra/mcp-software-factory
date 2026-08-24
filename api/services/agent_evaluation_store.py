from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from api.services.observability_sanitizer import safe_json, sanitize_value


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_evaluation_runs (
 evaluation_run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, trace_id TEXT,
 branch_id TEXT NOT NULL, execution_id TEXT, evaluation_type TEXT NOT NULL,
 status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT, duration_ms REAL,
 evaluator_version TEXT NOT NULL, rubric_id TEXT, rubric_version TEXT, model TEXT,
 overall_score REAL, verdict TEXT, error TEXT, idempotency_key TEXT NOT NULL,
 validity_status TEXT NOT NULL DEFAULT 'valid', superseded_by_run_id TEXT,
 invalidated_at TEXT, invalidation_reason TEXT,
 forced INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_evaluation_results (
 evaluation_result_id TEXT PRIMARY KEY, evaluation_run_id TEXT NOT NULL,
 agent_name TEXT NOT NULL, node TEXT, subgraph TEXT, evaluator_name TEXT NOT NULL,
 score REAL, verdict TEXT NOT NULL, confidence REAL NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(evaluation_run_id) REFERENCES agent_evaluation_runs(evaluation_run_id)
);
CREATE TABLE IF NOT EXISTS agent_evaluation_metrics (
 metric_id TEXT PRIMARY KEY, evaluation_run_id TEXT NOT NULL,
 evaluation_result_id TEXT, agent_name TEXT, evaluation_scope TEXT NOT NULL DEFAULT 'workflow',
 node TEXT, subgraph TEXT, metric_name TEXT NOT NULL,
 metric_value REAL NOT NULL, metric_type TEXT NOT NULL, unit TEXT,
 threshold REAL, passed INTEGER NOT NULL, formula TEXT, raw_value REAL,
 source_state TEXT, contributes_to_score INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
 FOREIGN KEY(evaluation_run_id) REFERENCES agent_evaluation_runs(evaluation_run_id)
);
CREATE TABLE IF NOT EXISTS agent_evaluation_evidence (
 evidence_id TEXT PRIMARY KEY, evaluation_run_id TEXT NOT NULL,
 evaluation_result_id TEXT, evidence_type TEXT NOT NULL, reference_id TEXT,
 summary TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
 FOREIGN KEY(evaluation_run_id) REFERENCES agent_evaluation_runs(evaluation_run_id)
);
CREATE TABLE IF NOT EXISTS agent_evaluation_rubrics (
 rubric_id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL,
 agent_name TEXT NOT NULL, dimensions_json TEXT NOT NULL, weights_json TEXT NOT NULL,
 thresholds_json TEXT NOT NULL, enabled INTEGER NOT NULL, created_at TEXT NOT NULL,
 updated_at TEXT, version_status TEXT NOT NULL DEFAULT 'current',
 UNIQUE(name,version,agent_name)
);
CREATE TABLE IF NOT EXISTS agent_evaluation_run_rubrics (
 evaluation_run_id TEXT NOT NULL, rubric_id TEXT NOT NULL,
 rubric_version TEXT NOT NULL, agent_name TEXT NOT NULL,
 evaluation_type TEXT NOT NULL, binding_source TEXT NOT NULL DEFAULT 'runtime',
 created_at TEXT NOT NULL,
 PRIMARY KEY(evaluation_run_id,rubric_id),
 FOREIGN KEY(evaluation_run_id) REFERENCES agent_evaluation_runs(evaluation_run_id),
 FOREIGN KEY(rubric_id) REFERENCES agent_evaluation_rubrics(rubric_id)
);
CREATE TABLE IF NOT EXISTS agent_evaluation_rubric_reconciliation (
 evaluation_run_id TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT,
 reconciled_at TEXT NOT NULL,
 FOREIGN KEY(evaluation_run_id) REFERENCES agent_evaluation_runs(evaluation_run_id)
);
CREATE TABLE IF NOT EXISTS agent_evaluation_baselines (
 baseline_id TEXT PRIMARY KEY, scope_json TEXT NOT NULL, scope_key TEXT NOT NULL,
 metric TEXT NOT NULL, score REAL NOT NULL, sample_count INTEGER NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(scope_key,metric)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_eval_idempotent
 ON agent_evaluation_runs(idempotency_key) WHERE forced=0;
CREATE INDEX IF NOT EXISTS idx_agent_eval_runs_workflow
 ON agent_evaluation_runs(workflow_id,branch_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_eval_results_agent
 ON agent_evaluation_results(agent_name,score,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_eval_metrics_name
 ON agent_evaluation_metrics(metric_name,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_eval_evidence_run
 ON agent_evaluation_evidence(evaluation_run_id);
CREATE INDEX IF NOT EXISTS idx_agent_eval_run_rubrics_rubric
 ON agent_evaluation_run_rubrics(rubric_id,evaluation_run_id);
CREATE TRIGGER IF NOT EXISTS trg_agent_eval_rubric_scoring_immutable
 BEFORE UPDATE OF dimensions_json,weights_json,thresholds_json,agent_name,version ON agent_evaluation_rubrics
 WHEN EXISTS(SELECT 1 FROM agent_evaluation_run_rubrics WHERE rubric_id=OLD.rubric_id)
  AND (NEW.dimensions_json!=OLD.dimensions_json OR NEW.weights_json!=OLD.weights_json OR NEW.thresholds_json!=OLD.thresholds_json OR NEW.agent_name!=OLD.agent_name OR NEW.version!=OLD.version)
 BEGIN SELECT RAISE(ABORT,'rubric_version_is_immutable'); END;
"""

DEFAULT_RUBRICS = {
    "Planner": ({"completeness": 1, "feasibility": 1, "decomposition_quality": 1, "technical_consistency": 1}, {"completeness": .30, "feasibility": .25, "decomposition_quality": .25, "technical_consistency": .20}),
    "Developer": ({"requirement_coverage": 1, "implementation_quality": 1, "maintainability": 1, "architectural_alignment": 1}, {"requirement_coverage": .30, "implementation_quality": .30, "maintainability": .20, "architectural_alignment": .20}),
    "QA": ({"test_coverage_quality": 1, "defect_detection_quality": 1, "validation_completeness": 1}, {"test_coverage_quality": .40, "defect_detection_quality": .30, "validation_completeness": .30}),
    "Repair": ({"root_cause_quality": 1, "repair_relevance": 1, "regression_risk": 1}, {"root_cause_quality": .40, "repair_relevance": .35, "regression_risk": .25}),
    "Workflow": ({"goal_completion": 1, "overall_quality": 1, "efficiency": 1, "reliability": 1}, {"goal_completion": .35, "overall_quality": .30, "efficiency": .15, "reliability": .20}),
}
DIMENSION_AGENT_ORDER = ("Planner", "Developer", "QA", "Repair", "Supervisor", "Workflow")
KNOWN_INVALID_RUN_ID = "e1c9254e7c304063b3e4326b3c3d2269"
KNOWN_INVALID_REASON = "Agent scores were derived from workflow-global deterministic metrics before agent-scope attribution fix"


def now() -> str:
    return datetime.now(UTC).isoformat()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None: return None
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json"):
            result[key[:-5]] = json.loads(result.pop(key) or "{}")
    for key in ("forced", "enabled", "passed", "contributes_to_score"):
        if key in result: result[key] = bool(result[key])
    return result


class AgentEvaluationStore:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self.initialize_sync)

    def initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            run_columns={row["name"] for row in connection.execute("PRAGMA table_info(agent_evaluation_runs)")}
            for column,definition in (("validity_status","TEXT NOT NULL DEFAULT 'valid'"),("superseded_by_run_id","TEXT"),("invalidated_at","TEXT"),("invalidation_reason","TEXT")):
                if column not in run_columns:connection.execute(f"ALTER TABLE agent_evaluation_runs ADD COLUMN {column} {definition}")
            connection.execute("""UPDATE agent_evaluation_runs SET validity_status='invalidated',invalidated_at=COALESCE(invalidated_at,?),invalidation_reason=?
                WHERE evaluation_run_id=? AND validity_status='valid'""",(now(),KNOWN_INVALID_REASON,KNOWN_INVALID_RUN_ID))
            self._migrate_result_score_nullable(connection)
            metric_columns={row["name"] for row in connection.execute("PRAGMA table_info(agent_evaluation_metrics)")}
            for column,definition in (("evaluation_scope","TEXT NOT NULL DEFAULT 'workflow'"),("node","TEXT"),("subgraph","TEXT"),("raw_value","REAL"),("source_state","TEXT"),("contributes_to_score","INTEGER NOT NULL DEFAULT 1")):
                if column not in metric_columns:connection.execute(f"ALTER TABLE agent_evaluation_metrics ADD COLUMN {column} {definition}")
            rubric_columns={row["name"] for row in connection.execute("PRAGMA table_info(agent_evaluation_rubrics)")}
            if "version_status" not in rubric_columns:
                connection.execute("ALTER TABLE agent_evaluation_rubrics ADD COLUMN version_status TEXT NOT NULL DEFAULT 'current'")
            connection.execute("UPDATE agent_evaluation_rubrics SET version_status='disabled' WHERE enabled=0 AND version_status='current'")
            connection.execute("""UPDATE agent_evaluation_rubrics AS older SET enabled=0,version_status='superseded'
                WHERE older.version_status='current' AND EXISTS (
                    SELECT 1 FROM agent_evaluation_rubrics newer
                    WHERE newer.name=older.name AND newer.agent_name=older.agent_name AND newer.version_status='current'
                    AND (newer.created_at>older.created_at OR (newer.created_at=older.created_at AND newer.rowid>older.rowid)))""")
            connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_eval_rubric_current
                ON agent_evaluation_rubrics(name,agent_name) WHERE version_status='current'""")
            connection.execute("DROP TRIGGER IF EXISTS trg_agent_eval_rubric_scoring_immutable")
            connection.execute("""CREATE TRIGGER trg_agent_eval_rubric_scoring_immutable
                BEFORE UPDATE OF dimensions_json,weights_json,thresholds_json,agent_name,version ON agent_evaluation_rubrics
                WHEN EXISTS(SELECT 1 FROM agent_evaluation_run_rubrics WHERE rubric_id=OLD.rubric_id)
                AND (NEW.dimensions_json!=OLD.dimensions_json OR NEW.weights_json!=OLD.weights_json OR NEW.thresholds_json!=OLD.thresholds_json OR NEW.agent_name!=OLD.agent_name OR NEW.version!=OLD.version)
                BEGIN SELECT RAISE(ABORT,'rubric_version_is_immutable'); END""")
            for agent, (dimensions, weights) in DEFAULT_RUBRICS.items():
                connection.execute(
                    """INSERT OR IGNORE INTO agent_evaluation_rubrics
                    (rubric_id,name,version,agent_name,dimensions_json,weights_json,thresholds_json,enabled,created_at,updated_at,version_status)
                    VALUES(?,?,?,?,?,?,?,?,?,NULL,'current')""",
                    (uuid4().hex, f"{agent} quality", "1.0", agent, safe_json(dimensions),
                     safe_json(weights), safe_json({"pass": .7}), 1, now()),
                )

    @staticmethod
    def _migrate_result_score_nullable(connection: sqlite3.Connection) -> None:
        score_column=next((row for row in connection.execute("PRAGMA table_info(agent_evaluation_results)") if row["name"]=="score"),None)
        if score_column is None or not bool(score_column["notnull"]):return
        connection.execute("ALTER TABLE agent_evaluation_results RENAME TO agent_evaluation_results_legacy")
        connection.execute("""CREATE TABLE agent_evaluation_results (
         evaluation_result_id TEXT PRIMARY KEY, evaluation_run_id TEXT NOT NULL,
         agent_name TEXT NOT NULL, node TEXT, subgraph TEXT, evaluator_name TEXT NOT NULL,
         score REAL, verdict TEXT NOT NULL, confidence REAL NOT NULL,
         reason TEXT NOT NULL, created_at TEXT NOT NULL,
         FOREIGN KEY(evaluation_run_id) REFERENCES agent_evaluation_runs(evaluation_run_id))""")
        connection.execute("INSERT INTO agent_evaluation_results SELECT * FROM agent_evaluation_results_legacy")
        connection.execute("DROP TABLE agent_evaluation_results_legacy")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_agent_eval_results_agent ON agent_evaluation_results(agent_name,score,created_at DESC)")

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()):
        return await asyncio.to_thread(self.fetch_one_sync, sql, params)

    def fetch_one_sync(self, sql: str, params: tuple[Any, ...] = ()):
        with self.connect() as connection: return row_dict(connection.execute(sql, params).fetchone())

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()):
        return await asyncio.to_thread(self.fetch_all_sync, sql, params)

    def fetch_all_sync(self, sql: str, params: tuple[Any, ...] = ()):
        with self.connect() as connection: return [row_dict(row) for row in connection.execute(sql, params).fetchall()]

    async def create_run(self, data: dict[str, Any], *, force: bool = False) -> tuple[dict[str, Any], bool]:
        return await asyncio.to_thread(self.create_run_sync, data, force)

    def create_run_sync(self, data: dict[str, Any], force: bool):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not force:
                existing=connection.execute("SELECT * FROM agent_evaluation_runs WHERE idempotency_key=? AND forced=0",(data["idempotency_key"],)).fetchone()
                if existing: connection.commit(); return row_dict(existing),False
            run_id=uuid4().hex; timestamp=now()
            connection.execute(
                """INSERT INTO agent_evaluation_runs(evaluation_run_id,workflow_id,trace_id,branch_id,
                execution_id,evaluation_type,status,started_at,evaluator_version,rubric_id,rubric_version,
                model,idempotency_key,forced,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id,data["workflow_id"],data.get("trace_id"),data.get("branch_id","original"),
                 data.get("execution_id"),data["evaluation_type"],"running",timestamp,
                 data["evaluator_version"],data.get("rubric_id"),data.get("rubric_version"),
                 data.get("model"),data["idempotency_key"],int(force),timestamp),
            )
            connection.commit()
            return row_dict(connection.execute("SELECT * FROM agent_evaluation_runs WHERE evaluation_run_id=?",(run_id,)).fetchone()),True

    async def finish_run(self, run_id: str, *, score: float | None, verdict: str | None, error: str | None = None):
        await asyncio.to_thread(self.finish_run_sync, run_id, score, verdict, error)

    def finish_run_sync(self, run_id, score, verdict, error):
        ended=now()
        with self.connect() as connection:
            row=connection.execute("SELECT started_at FROM agent_evaluation_runs WHERE evaluation_run_id=?",(run_id,)).fetchone()
            started=datetime.fromisoformat(row["started_at"]); duration=max(0,(datetime.fromisoformat(ended)-started).total_seconds()*1000)
            connection.execute("UPDATE agent_evaluation_runs SET status=?,completed_at=?,duration_ms=?,overall_score=?,verdict=?,error=? WHERE evaluation_run_id=?",("failed" if error else "completed",ended,duration,score,verdict,sanitize_value(error),run_id))

    async def set_validity(self, run_id: str, *, validity_status: str, superseded_by_run_id: str | None, reason: str | None):
        return await asyncio.to_thread(self.set_validity_sync,run_id,validity_status,superseded_by_run_id,reason)

    def set_validity_sync(self, run_id: str, validity_status: str, superseded_by_run_id: str | None, reason: str | None):
        with self.connect() as connection:
            current=connection.execute("SELECT * FROM agent_evaluation_runs WHERE evaluation_run_id=?",(run_id,)).fetchone()
            if current is None:return None
            if validity_status=="superseded":
                if not superseded_by_run_id or superseded_by_run_id==run_id:return {"error":"superseded_run_required"}
                target=connection.execute("SELECT 1 FROM agent_evaluation_runs WHERE evaluation_run_id=?",(superseded_by_run_id,)).fetchone()
                if target is None:return {"error":"superseding_run_not_found"}
            timestamp=now() if validity_status in {"invalidated","superseded"} else None
            connection.execute("""UPDATE agent_evaluation_runs SET validity_status=?,superseded_by_run_id=?,invalidated_at=?,invalidation_reason=? WHERE evaluation_run_id=?""",
                (validity_status,superseded_by_run_id if validity_status=="superseded" else None,timestamp,sanitize_value(reason) if validity_status!="valid" else None,run_id))
        return self.fetch_one_sync("SELECT * FROM agent_evaluation_runs WHERE evaluation_run_id=?",(run_id,))

    async def add_result(self, run_id: str, data: dict[str, Any]) -> str:
        result_id=uuid4().hex
        await asyncio.to_thread(self.add_result_sync, result_id, run_id, data)
        return result_id

    def add_result_sync(self, result_id, run_id, data):
        with self.connect() as connection:
            score=float(data["score"]) if data.get("score") is not None else None
            connection.execute("INSERT INTO agent_evaluation_results VALUES(?,?,?,?,?,?,?,?,?,?,?)",(result_id,run_id,data["agent_name"],data.get("node"),data.get("subgraph"),data["evaluator_name"],score,data["verdict"],float(data.get("confidence",1)),sanitize_value(data.get("reason")) or "No reason",now()))

    async def add_metric(self, run_id: str, data: dict[str, Any], result_id: str | None = None):
        await asyncio.to_thread(self.add_metric_sync, run_id, data, result_id)

    def add_metric_sync(self, run_id, data, result_id):
        with self.connect() as connection:
            connection.execute("""INSERT INTO agent_evaluation_metrics(
            metric_id,evaluation_run_id,evaluation_result_id,agent_name,evaluation_scope,node,subgraph,
            metric_name,metric_value,metric_type,unit,threshold,passed,formula,raw_value,source_state,
            contributes_to_score,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(uuid4().hex,run_id,result_id,data.get("agent_name"),data.get("evaluation_scope") or ("agent" if data.get("agent_name") else "workflow"),data.get("node"),data.get("subgraph"),data["metric_name"],float(data["metric_value"]),data["metric_type"],data.get("unit"),data.get("threshold"),int(data["passed"]),data.get("formula"),data.get("raw_value"),data.get("source_state"),int(data.get("contributes_to_score",True)),now()))

    async def add_evidence(self, run_id: str, data: dict[str, Any], result_id: str | None = None):
        await asyncio.to_thread(self.add_evidence_sync, run_id, data, result_id)

    def add_evidence_sync(self, run_id, data, result_id):
        with self.connect() as connection:
            connection.execute("INSERT INTO agent_evaluation_evidence VALUES(?,?,?,?,?,?,?,?)",(uuid4().hex,run_id,result_id,data["evidence_type"],data.get("reference_id"),sanitize_value(data.get("summary")) or "Evidence",safe_json(data.get("metadata") or {}),now()))

    async def detail(self, run_id: str):
        run=await self.fetch_one("SELECT * FROM agent_evaluation_runs WHERE evaluation_run_id=?",(run_id,))
        if not run:return None
        run["results"]=await self.fetch_all("SELECT * FROM agent_evaluation_results WHERE evaluation_run_id=? ORDER BY created_at",(run_id,))
        run["metrics"]=await self.fetch_all("SELECT * FROM agent_evaluation_metrics WHERE evaluation_run_id=? ORDER BY metric_type,metric_name",(run_id,))
        run["evidence"]=await self.fetch_all("SELECT * FROM agent_evaluation_evidence WHERE evaluation_run_id=? ORDER BY created_at",(run_id,))
        run["rubrics_used"]=await self.fetch_all("""SELECT b.evaluation_run_id,b.rubric_id,b.rubric_version,b.agent_name,
            b.evaluation_type,b.binding_source,b.created_at,r.name,r.dimensions_json,r.weights_json,r.thresholds_json
            FROM agent_evaluation_run_rubrics b JOIN agent_evaluation_rubrics r ON r.rubric_id=b.rubric_id
            WHERE b.evaluation_run_id=? ORDER BY b.agent_name""",(run_id,))
        reconciliation=await self.fetch_one("SELECT status,reason,reconciled_at FROM agent_evaluation_rubric_reconciliation WHERE evaluation_run_id=?",(run_id,))
        run["rubric_binding_status"]=(reconciliation or {}).get("status")
        run["rubric_binding_reason"]=(reconciliation or {}).get("reason")
        run["dimension_scores"]=self._dimension_scores(run["evaluation_type"],run["metrics"],run["rubrics_used"])
        return run

    @staticmethod
    def _dimension_scores(evaluation_type: str, metrics: list[dict[str, Any]], rubrics: list[dict[str, Any]]) -> dict[str, Any]:
        rubric_order={}
        for rubric in rubrics:
            if rubric["agent_name"] not in rubric_order:
                rubric_order[rubric["agent_name"]]=list(rubric["weights"])

        def grouped(metric_types: set[str]) -> dict[str, dict[str, float]]:
            rows=[item for item in metrics if item["metric_type"] in metric_types and item.get("contributes_to_score",True)]
            result={}
            for agent in DIMENSION_AGENT_ORDER:
                agent_rows=[item for item in rows if (item.get("agent_name") or "Workflow")==agent]
                if not agent_rows:continue
                values={item["metric_name"]:item["metric_value"] for item in agent_rows}
                names=[name for name in rubric_order.get(agent,[]) if name in values]
                names.extend(sorted(set(values)-set(names)))
                result[agent]={name:values[name] for name in names}
            return result

        if evaluation_type=="llm_judge":
            return grouped({"llm_judge"})
        if evaluation_type=="comprehensive":
            sections={
                "deterministic":grouped({"deterministic"}),
                "heuristic":grouped({"heuristic","heuristic_dimension"}),
                "llm_judge":grouped({"llm_judge"}),
            }
            return {name:values for name,values in sections.items() if values}
        if evaluation_type=="heuristic":
            return {item["metric_name"]:item["metric_value"] for item in metrics if item["metric_type"]=="heuristic_dimension" and item.get("contributes_to_score",True)}
        return {}

    async def list_runs(self, *, limit=100, offset=0, **filters):
        clauses=[];params=[]
        mapping={"workflow_id":"workflow_id","branch_id":"branch_id","agent":"evaluation_run_id IN (SELECT evaluation_run_id FROM agent_evaluation_results WHERE agent_name=?)","model":"model","verdict":"verdict","evaluation_type":"evaluation_type","validity_status":"validity_status"}
        for key,column in mapping.items():
            value=filters.get(key)
            if value is not None: clauses.append(column if key=="agent" else f"{column}=?");params.append(value)
        if filters.get("min_score") is not None: clauses.append("overall_score>=?");params.append(filters["min_score"])
        if filters.get("max_score") is not None: clauses.append("overall_score<=?");params.append(filters["max_score"])
        if filters.get("date_from"):clauses.append("created_at>=?");params.append(filters["date_from"])
        if filters.get("date_to"):clauses.append("created_at<?");params.append(filters["date_to"])
        where=" WHERE "+" AND ".join(clauses) if clauses else ""
        items=await self.fetch_all(f"SELECT * FROM agent_evaluation_runs{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",(*params,limit,offset))
        total=await self.fetch_one(f"SELECT COUNT(*) AS total FROM agent_evaluation_runs{where}",tuple(params))
        return {"items":items,"total":int(total["total"]),"limit":limit,"offset":offset,"has_more":offset+len(items)<int(total["total"])}

    async def rubrics(self):
        return await self.fetch_all("""SELECT r.*,
            (SELECT COUNT(*) FROM agent_evaluation_run_rubrics b WHERE b.rubric_id=r.rubric_id) AS evaluations_total,
            (SELECT COUNT(*) FROM agent_evaluation_run_rubrics b JOIN agent_evaluation_runs x ON x.evaluation_run_id=b.evaluation_run_id WHERE b.rubric_id=r.rubric_id AND x.validity_status='valid') AS evaluations_valid,
            (SELECT COUNT(*) FROM agent_evaluation_run_rubrics b WHERE b.rubric_id=r.rubric_id) AS usage_count
            FROM agent_evaluation_rubrics r ORDER BY r.agent_name,r.version DESC""")

    async def rubric_detail(self, rubric_id):
        item=await self.fetch_one("""SELECT r.*,
            (SELECT COUNT(*) FROM agent_evaluation_run_rubrics b WHERE b.rubric_id=r.rubric_id) AS evaluations_total,
            (SELECT COUNT(*) FROM agent_evaluation_run_rubrics b JOIN agent_evaluation_runs x ON x.evaluation_run_id=b.evaluation_run_id WHERE b.rubric_id=r.rubric_id AND x.validity_status='valid') AS evaluations_valid,
            (SELECT COUNT(*) FROM agent_evaluation_run_rubrics b WHERE b.rubric_id=r.rubric_id) AS usage_count
            FROM agent_evaluation_rubrics r WHERE r.rubric_id=?""",(rubric_id,))
        if not item:return None
        item["history"]=await self.fetch_all("""SELECT r.*,
            (SELECT COUNT(*) FROM agent_evaluation_run_rubrics b WHERE b.rubric_id=r.rubric_id) AS evaluations_total,
            (SELECT COUNT(*) FROM agent_evaluation_run_rubrics b JOIN agent_evaluation_runs x ON x.evaluation_run_id=b.evaluation_run_id WHERE b.rubric_id=r.rubric_id AND x.validity_status='valid') AS evaluations_valid,
            (SELECT COUNT(*) FROM agent_evaluation_run_rubrics b WHERE b.rubric_id=r.rubric_id) AS usage_count
            FROM agent_evaluation_rubrics r WHERE r.name=? AND r.agent_name=? ORDER BY r.created_at DESC""",(item["name"],item["agent_name"]))
        return item

    async def bind_run_rubric(self, run_id: str, rubric: dict[str,Any], evaluation_type: str, *, source: str="runtime"):
        await asyncio.to_thread(self.bind_run_rubric_sync,run_id,rubric,evaluation_type,source)

    def bind_run_rubric_sync(self, run_id, rubric, evaluation_type, source="runtime", connection=None):
        owns_connection=connection is None;connection=connection or self.connect()
        try:
            connection.execute("""INSERT INTO agent_evaluation_run_rubrics
                (evaluation_run_id,rubric_id,rubric_version,agent_name,evaluation_type,binding_source,created_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(evaluation_run_id,rubric_id) DO NOTHING""",
                (run_id,rubric["rubric_id"],rubric["version"],rubric["agent_name"],evaluation_type,source,now()))
            existing=connection.execute("SELECT * FROM agent_evaluation_run_rubrics WHERE evaluation_run_id=? AND rubric_id=?",(run_id,rubric["rubric_id"])).fetchone()
            if existing and (existing["rubric_version"]!=rubric["version"] or existing["agent_name"]!=rubric["agent_name"]):
                raise ValueError("rubric_binding_conflict")
            if owns_connection:connection.commit()
        finally:
            if owns_connection:connection.close()

    async def reconcile_run_rubrics(self, *, dry_run: bool=True, run_ids: list[str] | None=None):
        return await asyncio.to_thread(self.reconcile_run_rubrics_sync,dry_run,run_ids)

    def reconcile_run_rubrics_sync(self, dry_run=True, run_ids=None):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            params=tuple(run_ids or ());where=f" WHERE evaluation_run_id IN ({','.join('?' for _ in params)})" if params else ""
            runs=connection.execute(f"SELECT * FROM agent_evaluation_runs{where} ORDER BY started_at",params).fetchall()
            rubric_agents={row["agent_name"] for row in connection.execute("SELECT DISTINCT agent_name FROM agent_evaluation_rubrics")}
            report={"dry_run":dry_run,"runs_scanned":len(runs),"bindings_candidates":0,"bindings_created":0,"reconciled_runs":0,"unreconciled_runs":0,"unreconciled":[]}
            for run in runs:
                results=connection.execute("SELECT agent_name,score,verdict FROM agent_evaluation_results WHERE evaluation_run_id=?",(run["evaluation_run_id"],)).fetchall()
                agents=sorted({row["agent_name"] for row in results if row["agent_name"] in rubric_agents and row["score"] is not None and row["verdict"]!="not_evaluated"})
                safe=[];problems=[];effective_at=run["started_at"] or run["created_at"]
                legacy=row_dict(connection.execute("SELECT * FROM agent_evaluation_rubrics WHERE rubric_id=? AND created_at<=?",(run["rubric_id"],effective_at)).fetchone()) if run["rubric_id"] else None
                for agent in agents:
                    if legacy and legacy["agent_name"]==agent:
                        safe.append(legacy);continue
                    candidates=connection.execute("SELECT * FROM agent_evaluation_rubrics WHERE agent_name=? AND created_at<=? ORDER BY created_at DESC,version DESC",(agent,effective_at)).fetchall()
                    latest_by_name={}
                    for candidate in candidates:latest_by_name.setdefault(candidate["name"],candidate)
                    if len(latest_by_name)==1:safe.append(row_dict(next(iter(latest_by_name.values()))))
                    elif len(latest_by_name)>1:problems.append(f"{agent}:ambiguous_effective_rubric")
                    else:problems.append(f"{agent}:no_effective_rubric")
                report["bindings_candidates"]+=len(safe)
                existing_ids={row["rubric_id"] for row in connection.execute("SELECT rubric_id FROM agent_evaluation_run_rubrics WHERE evaluation_run_id=?",(run["evaluation_run_id"],))}
                if not dry_run:
                    for rubric in safe:
                        if rubric["rubric_id"] not in existing_ids:
                            self.bind_run_rubric_sync(run["evaluation_run_id"],rubric,run["evaluation_type"],"backfill",connection);report["bindings_created"]+=1
                    status="reconciled" if not problems and bool(safe) else "unreconciled_rubric_binding"
                    reason=";".join(problems) if problems else None
                    connection.execute("""INSERT INTO agent_evaluation_rubric_reconciliation(evaluation_run_id,status,reason,reconciled_at)
                        VALUES(?,?,?,?) ON CONFLICT(evaluation_run_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,reconciled_at=excluded.reconciled_at""",(run["evaluation_run_id"],status,reason,now()))
                if problems or not safe:
                    report["unreconciled_runs"]+=1;report["unreconciled"].append({"evaluation_run_id":run["evaluation_run_id"],"reasons":problems or ["no_evaluated_rubric_agent"]})
                else:report["reconciled_runs"]+=1
            if dry_run:connection.rollback()
            else:connection.commit()
            return report

    async def create_rubric(self, data):
        return await asyncio.to_thread(self.create_rubric_sync,data)

    def create_rubric_sync(self, data):
        rubric_id=uuid4().hex;timestamp=now();enabled=bool(data.get("enabled",True))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if enabled:
                connection.execute("UPDATE agent_evaluation_rubrics SET enabled=0,version_status='superseded',updated_at=? WHERE name=? AND agent_name=? AND version_status='current'",(timestamp,data["name"],data["agent_name"]))
            connection.execute("""INSERT INTO agent_evaluation_rubrics
                (rubric_id,name,version,agent_name,dimensions_json,weights_json,thresholds_json,enabled,created_at,updated_at,version_status)
                VALUES(?,?,?,?,?,?,?,?,?,NULL,?)""",(rubric_id,data["name"],data["version"],data["agent_name"],safe_json(data["dimensions"]),safe_json(data["weights"]),safe_json(data.get("thresholds") or {}),int(enabled),timestamp,"current" if enabled else "disabled"))
            return row_dict(connection.execute("SELECT * FROM agent_evaluation_rubrics WHERE rubric_id=?",(rubric_id,)).fetchone())

    async def create_rubric_version(self, rubric_id, data):
        return await asyncio.to_thread(self.create_rubric_version_sync,rubric_id,data)

    def create_rubric_version_sync(self, rubric_id, data):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source=row_dict(connection.execute("SELECT * FROM agent_evaluation_rubrics WHERE rubric_id=?",(rubric_id,)).fetchone())
            if not source:return None,False,"not_found"
            values={**source,**data}; existing=row_dict(connection.execute("SELECT * FROM agent_evaluation_rubrics WHERE name=? AND agent_name=? AND version=?",(source["name"],source["agent_name"],values["version"])).fetchone())
            comparable=lambda item:(item["dimensions"],item["weights"],item["thresholds"],bool(item["enabled"]))
            if existing:
                requested={**values,"enabled":bool(values.get("enabled",True))}
                return (existing,False,None) if comparable(existing)==comparable(requested) else (existing,False,"version_exists")
            timestamp=now();new_id=uuid4().hex;enabled=bool(values.get("enabled",True))
            if enabled:
                connection.execute("UPDATE agent_evaluation_rubrics SET enabled=0,version_status='superseded',updated_at=? WHERE name=? AND agent_name=? AND version_status='current'",(timestamp,source["name"],source["agent_name"]))
            connection.execute("""INSERT INTO agent_evaluation_rubrics
                (rubric_id,name,version,agent_name,dimensions_json,weights_json,thresholds_json,enabled,created_at,updated_at,version_status)
                VALUES(?,?,?,?,?,?,?,?,?,NULL,?)""",(new_id,source["name"],values["version"],source["agent_name"],safe_json(values["dimensions"]),safe_json(values["weights"]),safe_json(values.get("thresholds") or {}),int(enabled),timestamp,"current" if enabled else "disabled"))
            created=row_dict(connection.execute("SELECT * FROM agent_evaluation_rubrics WHERE rubric_id=?",(new_id,)).fetchone())
            return created,True,None

    async def disable_rubric(self, rubric_id):
        with self.connect() as connection:
            connection.execute("UPDATE agent_evaluation_rubrics SET enabled=0,version_status='disabled',updated_at=? WHERE rubric_id=?",(now(),rubric_id))
        return await self.rubric_detail(rubric_id)

    async def create_baseline(self, data):
        scope_key=safe_json(data.get("scope") or {}); baseline_id=uuid4().hex
        with self.connect() as connection:
            connection.execute("""INSERT INTO agent_evaluation_baselines VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(scope_key,metric) DO UPDATE SET score=excluded.score,sample_count=excluded.sample_count,created_at=excluded.created_at""",(baseline_id,scope_key,scope_key,data["metric"],float(data["score"]),int(data.get("sample_count",1)),now()))
        return await self.fetch_one("SELECT * FROM agent_evaluation_baselines WHERE scope_key=? AND metric=?",(scope_key,data["metric"]))

    async def baselines(self): return await self.fetch_all("SELECT * FROM agent_evaluation_baselines ORDER BY created_at DESC")
