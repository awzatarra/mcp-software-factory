from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4

from api.alert_models import (
    AlertAction, AlertEvidence, AlertOccurrence, AlertRule,
    AlertRuleThreshold, AlertStatusCounts, WorkflowAlert,
)


ALERT_RULE_VERSION = "1.1"
SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
_SECRET = re.compile(r"(?i)(sk-[a-z0-9_-]{12,}|bearer\s+\S+|(token|password|secret|api[_-]?key)\s*[:=]\s*\S+)")
_ABSOLUTE_PATH = re.compile(r"(?i)(?:[a-z]:[\\/][^\s,;\"']+|/(?:home|users|tmp|var|opt|workspace|mnt)/[^\s,;\"']+)")


def safe_alert_text(value: Any, limit: int = 2_000) -> str:
    text = "".join(character for character in str(value or "") if character.isprintable() or character in "\n\t")
    return _ABSOLUTE_PATH.sub("[path]", _SECRET.sub("[redacted]", text))[:limit]


BUILTIN_RULES: tuple[dict[str, Any], ...] = (
    {"code": "APPROVAL_WAIT_TOO_LONG", "name": "Approval wait too long", "description": "A workflow has waited too long for approval.", "category": "approval", "severity": "warning", "threshold": {"warning": 1800, "error": 3600, "critical": 14400}, "cooldown": 900, "auto": True},
    {"code": "WORKFLOW_FAILED", "name": "Workflow failed", "description": "The workflow reached a failed terminal state.", "category": "workflow", "severity": "error", "threshold": None, "cooldown": 3600, "auto": False},
    {"code": "TESTS_FAILED", "name": "Tests failed", "description": "The latest test execution failed.", "category": "testing", "severity": "error", "threshold": None, "cooldown": 1800, "auto": True},
    {"code": "REPAIR_FAILED", "name": "Repair failed", "description": "Automated repair did not restore passing tests.", "category": "repair", "severity": "critical", "threshold": None, "cooldown": 3600, "auto": True},
    {"code": "LOW_EVALUATION_SCORE", "name": "Low evaluation score", "description": "The final evaluation score is below the configured threshold.", "category": "evaluation", "severity": "warning", "threshold": {"warning": 60, "error": 40, "critical": 20}, "cooldown": 3600, "auto": True},
    {"code": "SUPERVISOR_LOOP_DETECTED", "name": "Supervisor loop detected", "description": "The supervisor stopped a loop without progress.", "category": "supervisor", "severity": "critical", "threshold": None, "cooldown": 3600, "auto": False},
    {"code": "SUPERVISOR_FALLBACK", "name": "Supervisor fallback", "description": "The supervisor used its deterministic fallback.", "category": "supervisor", "severity": "warning", "threshold": None, "cooldown": 3600, "auto": True},
    {"code": "HIGH_WARNING_COUNT", "name": "High warning count", "description": "Testing produced too many warnings.", "category": "testing", "severity": "warning", "threshold": {"warning": 5}, "cooldown": 3600, "auto": True},
    {"code": "EXCESSIVE_RETRIES", "name": "Excessive retries", "description": "The workflow required too many retries.", "category": "workflow", "severity": "warning", "threshold": {"warning": 3}, "cooldown": 3600, "auto": True},
    {"code": "WORKFLOW_DURATION_ANOMALY", "name": "Workflow duration anomaly", "description": "Wall clock duration exceeds the configured or historical P95 threshold.", "category": "workflow", "severity": "warning", "threshold": {"warning": 3600}, "cooldown": 1800, "auto": True},
    {"code": "EVENT_SEQUENCE_GAP", "name": "Event sequence gap", "description": "The durable event stream contains an unexplained sequence gap.", "category": "events", "severity": "error", "threshold": None, "cooldown": 3600, "auto": True},
    {"code": "DASHBOARD_METRIC_STALE", "name": "Dashboard metric stale", "description": "The dashboard projection trails the workflow event stream.", "category": "dashboard", "severity": "warning", "threshold": {"warning": 300}, "cooldown": 600, "auto": True},
    {"code": "LLM_BUDGET_WARNING", "name": "LLM budget warning", "description": "LLM spend reached the configured warning threshold.", "category": "llm_cost", "severity": "warning", "threshold": {"warning": 80}, "cooldown": 900, "auto": True},
    {"code": "LLM_BUDGET_EXCEEDED", "name": "LLM budget exceeded", "description": "LLM spend exhausted an applicable budget.", "category": "llm_cost", "severity": "error", "threshold": {"warning": 100}, "cooldown": 900, "auto": True},
    {"code": "LLM_CALL_BLOCKED", "name": "LLM call blocked", "description": "A hard budget limit blocked an LLM call before provider invocation.", "category": "llm_cost", "severity": "error", "threshold": None, "cooldown": 900, "auto": True},
    {"code": "LLM_PRICING_MISSING", "name": "LLM pricing missing", "description": "Provider usage exists but no effective pricing version was found.", "category": "llm_cost", "severity": "warning", "threshold": None, "cooldown": 3600, "auto": True},
    {"code": "LLM_USAGE_UNAVAILABLE", "name": "LLM usage unavailable", "description": "The provider response did not include usable token data.", "category": "llm_cost", "severity": "warning", "threshold": None, "cooldown": 3600, "auto": True},
    {"code": "LLM_COST_SPIKE", "name": "LLM cost spike", "description": "Deterministic cost analysis exceeded the configured historical multiplier.", "category": "llm_cost", "severity": "warning", "threshold": None, "cooldown": 3600, "auto": True},
    {"code": "HIGH_RETRY_COST", "name": "High LLM retry cost", "description": "Retry cost represents an elevated share of workflow LLM spend.", "category": "llm_cost", "severity": "warning", "threshold": None, "cooldown": 3600, "auto": True},
)


ALERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_rules (
 rule_id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 description TEXT NOT NULL, category TEXT NOT NULL, enabled INTEGER NOT NULL,
 default_severity TEXT NOT NULL, threshold_json TEXT NULL,
 cooldown_seconds INTEGER NOT NULL, auto_resolve INTEGER NOT NULL,
 branch_scope TEXT NOT NULL, rule_version TEXT NOT NULL DEFAULT '1.0',
 created_at TEXT NOT NULL, updated_at TEXT NULL
);
CREATE TABLE IF NOT EXISTS workflow_alerts (
 alert_id TEXT PRIMARY KEY, rule_id TEXT NOT NULL, rule_code TEXT NOT NULL,
 thread_id TEXT NOT NULL, branch_id TEXT NOT NULL, project_name TEXT NULL,
 category TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL,
 title TEXT NOT NULL, message TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
 first_detected_at TEXT NOT NULL, last_detected_at TEXT NOT NULL,
 occurrence_count INTEGER NOT NULL, acknowledged_at TEXT NULL,
 acknowledged_by TEXT NULL, resolved_at TEXT NULL, resolved_by TEXT NULL,
 resolution_note TEXT NULL, muted_until TEXT NULL,
 current_evidence_json TEXT NOT NULL, primary_event_id TEXT NULL,
 related_task_id TEXT NULL, related_file TEXT NULL, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, FOREIGN KEY(rule_id) REFERENCES alert_rules(rule_id)
);
CREATE TABLE IF NOT EXISTS alert_occurrences (
 occurrence_id TEXT PRIMARY KEY, alert_id TEXT NOT NULL, detected_at TEXT NOT NULL,
 severity TEXT NOT NULL, message TEXT NOT NULL, evidence_json TEXT NOT NULL,
 source_event_id TEXT NULL, FOREIGN KEY(alert_id) REFERENCES workflow_alerts(alert_id)
);
CREATE TABLE IF NOT EXISTS alert_actions (
 action_id TEXT PRIMARY KEY, alert_id TEXT NOT NULL, action TEXT NOT NULL,
 actor TEXT NULL, note TEXT NULL, previous_status TEXT NULL, new_status TEXT NULL,
 created_at TEXT NOT NULL, FOREIGN KEY(alert_id) REFERENCES workflow_alerts(alert_id)
);
CREATE TABLE IF NOT EXISTS alert_evaluator_locks (
 lock_name TEXT PRIMARY KEY, owner_id TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_status ON workflow_alerts(status);
CREATE INDEX IF NOT EXISTS idx_alert_severity ON workflow_alerts(severity);
CREATE INDEX IF NOT EXISTS idx_alert_rule ON workflow_alerts(rule_id);
CREATE INDEX IF NOT EXISTS idx_alert_rule_code ON workflow_alerts(rule_code);
CREATE INDEX IF NOT EXISTS idx_alert_thread ON workflow_alerts(thread_id);
CREATE INDEX IF NOT EXISTS idx_alert_branch ON workflow_alerts(branch_id);
CREATE INDEX IF NOT EXISTS idx_alert_project ON workflow_alerts(project_name);
CREATE INDEX IF NOT EXISTS idx_alert_last_detected ON workflow_alerts(last_detected_at);
CREATE INDEX IF NOT EXISTS idx_alert_created ON workflow_alerts(created_at);
CREATE INDEX IF NOT EXISTS idx_occurrence_alert ON alert_occurrences(alert_id);
CREATE INDEX IF NOT EXISTS idx_occurrence_detected ON alert_occurrences(detected_at);
CREATE INDEX IF NOT EXISTS idx_action_alert ON alert_actions(alert_id);
CREATE INDEX IF NOT EXISTS idx_action_created ON alert_actions(created_at);
"""


class AlertStore:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(ALERT_SCHEMA)
            for definition in BUILTIN_RULES:
                rule_id = f"builtin:{definition['code'].casefold()}"
                connection.execute(
                    """INSERT INTO alert_rules(rule_id,code,name,description,category,enabled,
                       default_severity,threshold_json,cooldown_seconds,auto_resolve,
                       branch_scope,rule_version,created_at,updated_at)
                       VALUES(?,?,?,?,?,1,?,?,?,?,?,'1.0',?,NULL)
                       ON CONFLICT(rule_id) DO UPDATE SET name=excluded.name,
                       description=excluded.description, rule_version=excluded.rule_version""",
                    (rule_id, definition["code"], definition["name"], definition["description"],
                     definition["category"], definition["severity"], json.dumps(definition["threshold"]),
                     definition["cooldown"], int(definition["auto"]), "all", now),
                )
            connection.execute(
                "UPDATE alert_rules SET auto_resolve=1,rule_version=?,updated_at=? "
                "WHERE rule_id='builtin:llm_call_blocked' AND rule_version<'1.1'",
                (ALERT_RULE_VERSION, now),
            )

    @staticmethod
    def _rule(row: sqlite3.Row | dict[str, Any]) -> AlertRule:
        item = dict(row)
        threshold = json.loads(item.pop("threshold_json")) if item.get("threshold_json") else None
        item.pop("rule_version", None)
        item["enabled"] = bool(item["enabled"])
        item["auto_resolve"] = bool(item["auto_resolve"])
        item["threshold"] = AlertRuleThreshold(**threshold) if threshold else None
        return AlertRule(**item)

    @staticmethod
    def _alert(row: sqlite3.Row | dict[str, Any]) -> WorkflowAlert:
        item = dict(row)
        item["current_evidence"] = AlertEvidence(**json.loads(item.pop("current_evidence_json")))
        return WorkflowAlert(**item)

    async def list_rules(self, *, enabled: bool | None = None, category: str | None = None, search: str | None = None, limit: int = 100, offset: int = 0) -> tuple[list[AlertRule], int]:
        return await asyncio.to_thread(self._list_rules_sync, enabled, category, search, limit, offset)

    def _list_rules_sync(self, enabled, category, search, limit, offset):
        clauses, params = [], []
        if enabled is not None: clauses.append("enabled=?"); params.append(int(enabled))
        if category: clauses.append("category=?"); params.append(category)
        if search: clauses.append("(code LIKE ? OR name LIKE ?)"); params.extend([f"%{safe_alert_text(search, 100)}%"] * 2)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM alert_rules{where}", params).fetchone()[0]
            rows = connection.execute(f"SELECT * FROM alert_rules{where} ORDER BY category,code LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        return [self._rule(row) for row in rows], int(total)

    async def get_rule(self, rule_id: str) -> AlertRule | None:
        return await asyncio.to_thread(self._get_rule_sync, rule_id)

    def _get_rule_sync(self, rule_id):
        with self._connect() as connection: row = connection.execute("SELECT * FROM alert_rules WHERE rule_id=?", (rule_id,)).fetchone()
        return self._rule(row) if row else None

    async def update_rule(self, rule_id: str, updates: dict[str, Any]) -> AlertRule | None:
        return await asyncio.to_thread(self._update_rule_sync, rule_id, updates)

    def _update_rule_sync(self, rule_id, updates):
        allowed = {"enabled", "threshold", "cooldown_seconds", "auto_resolve", "default_severity", "branch_scope"}
        values = {key: value for key, value in updates.items() if key in allowed}
        if "threshold" in values:
            threshold = values["threshold"]
            values["threshold_json"] = json.dumps(threshold.model_dump() if hasattr(threshold, "model_dump") else threshold)
            del values["threshold"]
        for field in ("enabled", "auto_resolve"):
            if field in values: values[field] = int(values[field])
        values["updated_at"] = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(f"UPDATE alert_rules SET {','.join(f'{key}=?' for key in values)} WHERE rule_id=?", [*values.values(), rule_id])
            if not cursor.rowcount: return None
            row = connection.execute("SELECT * FROM alert_rules WHERE rule_id=?", (rule_id,)).fetchone()
        return self._rule(row)

    async def reset_rule(self, rule_id: str) -> AlertRule | None:
        definition = next((item for item in BUILTIN_RULES if f"builtin:{item['code'].casefold()}" == rule_id), None)
        if definition is None: return None
        return await self.update_rule(rule_id, {"enabled": True, "threshold": definition["threshold"], "cooldown_seconds": definition["cooldown"], "auto_resolve": definition["auto"], "default_severity": definition["severity"], "branch_scope": "all"})

    async def workflow_context(self, thread_id: str, branch_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._workflow_context_sync, thread_id, branch_id)

    def _workflow_context_sync(self, thread_id, branch_id):
        with self._connect() as connection:
            metric = connection.execute("SELECT * FROM dashboard_workflow_metrics WHERE thread_id=? AND branch_id=?", (thread_id, branch_id)).fetchone()
            if metric is None: return None
            events = connection.execute("SELECT event_id,sequence,event_type,timestamp,stage,status,data_json FROM workflow_events WHERE thread_id=? AND branch_id=? ORDER BY sequence", (thread_id, branch_id)).fetchall()
            durations = [row[0] for row in connection.execute("SELECT total_seconds FROM dashboard_workflow_metrics WHERE total_seconds IS NOT NULL AND total_seconds>=0 ORDER BY total_seconds")]
        item = dict(metric)
        for field in ("approval_operations", "evaluations", "findings", "recommendations", "agents", "related_files"):
            item[field] = json.loads(item.pop(f"{field}_json"))
        item["events"] = [{**dict(row), "data": json.loads(row["data_json"] or "{}")} for row in events]
        item["duration_p95"] = durations[max(0, round((len(durations) - 1) * .95))] if durations else None
        return item

    async def workflow_keys(self, limit: int = 100, offset: int = 0) -> list[tuple[str, str]]:
        return await asyncio.to_thread(self._workflow_keys_sync, limit, offset)

    def _workflow_keys_sync(self, limit, offset):
        with self._connect() as connection:
            rows = connection.execute("SELECT thread_id,branch_id FROM dashboard_workflow_metrics ORDER BY updated_at LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [(row["thread_id"], row["branch_id"]) for row in rows]

    @staticmethod
    def fingerprint(rule_id: str, thread_id: str, branch_id: str, dimension: str) -> str:
        normalized = "|".join((rule_id, thread_id, branch_id, dimension.strip().casefold()))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    async def detect(self, *, rule: AlertRule, thread_id: str, branch_id: str, project_name: str | None, severity: str, title: str, message: str, evidence: AlertEvidence, dimension: str, source_event_id: str | None) -> str:
        return await asyncio.to_thread(self._detect_sync, rule, thread_id, branch_id, project_name, severity, title, message, evidence, dimension, source_event_id)

    def _detect_sync(self, rule, thread_id, branch_id, project_name, severity, title, message, evidence, dimension, source_event_id):
        now = datetime.now(UTC); now_text = now.isoformat(); fingerprint = self.fingerprint(rule.rule_id, thread_id, branch_id, dimension)
        evidence = evidence.model_copy(update={
            "actual_value": safe_alert_text(evidence.actual_value) if isinstance(evidence.actual_value, str) else evidence.actual_value,
            "threshold_value": safe_alert_text(evidence.threshold_value) if isinstance(evidence.threshold_value, str) else evidence.threshold_value,
        })
        evidence_json = json.dumps(evidence.model_dump(mode="json"), ensure_ascii=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM workflow_alerts WHERE fingerprint=?", (fingerprint,)).fetchone()
            if existing is None:
                alert_id = str(uuid4())
                connection.execute("""INSERT INTO workflow_alerts(
                    alert_id,rule_id,rule_code,thread_id,branch_id,project_name,
                    category,severity,status,title,message,fingerprint,
                    first_detected_at,last_detected_at,occurrence_count,
                    acknowledged_at,acknowledged_by,resolved_at,resolved_by,
                    resolution_note,muted_until,current_evidence_json,
                    primary_event_id,related_task_id,related_file,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    alert_id, rule.rule_id, rule.code, thread_id, branch_id, safe_alert_text(project_name, 200) or None,
                    rule.category, severity, "open", safe_alert_text(title, 300), safe_alert_text(message), fingerprint,
                    now_text, now_text, 1, None, None, None, None, None, None, evidence_json,
                    source_event_id, evidence.related_task_id, evidence.related_file, now_text, now_text,
                ))
                self._insert_occurrence(connection, alert_id, now_text, severity, message, evidence_json, source_event_id)
                return "created"
            alert_id = existing["alert_id"]; previous_severity = existing["severity"]
            reopened = existing["status"] == "resolved"
            escalated = SEVERITY_RANK[severity] > SEVERITY_RANK[previous_severity]
            elapsed = (now - datetime.fromisoformat(existing["last_detected_at"].replace("Z", "+00:00"))).total_seconds()
            add_occurrence = reopened or escalated or elapsed >= rule.cooldown_seconds
            previous_evidence = json.loads(existing["current_evidence_json"] or "{}")
            current_evidence = evidence.model_dump(mode="json")
            navigation_changed = any(
                previous_evidence.get(field) != current_evidence.get(field)
                for field in ("related_task_id", "related_event_id", "related_file", "pending_operation")
            ) or existing["primary_event_id"] != source_event_id
            status = "open" if reopened else existing["status"]
            occurrence_count = existing["occurrence_count"] + int(add_occurrence)
            connection.execute("""UPDATE workflow_alerts SET severity=?,status=?,title=?,message=?,last_detected_at=?,occurrence_count=?,resolved_at=NULL,resolved_by=NULL,resolution_note=NULL,current_evidence_json=?,primary_event_id=?,related_task_id=?,related_file=?,updated_at=? WHERE alert_id=?""", (
                severity if escalated else previous_severity, status, safe_alert_text(title, 300), safe_alert_text(message), now_text, occurrence_count,
                evidence_json, source_event_id, evidence.related_task_id, evidence.related_file, now_text, alert_id,
            ))
            if add_occurrence: self._insert_occurrence(connection, alert_id, now_text, severity, message, evidence_json, source_event_id)
            if reopened: self._insert_action(connection, alert_id, "reopened", "system", "Condition detected again", "resolved", "open", now_text)
            if reopened: return "reopened"
            if escalated or add_occurrence: return "updated"
            if navigation_changed: return "updated"
            return "unchanged"

    @staticmethod
    def _insert_occurrence(connection, alert_id, detected_at, severity, message, evidence_json, source_event_id):
        connection.execute("INSERT INTO alert_occurrences VALUES(?,?,?,?,?,?,?)", (str(uuid4()), alert_id, detected_at, severity, safe_alert_text(message), evidence_json, source_event_id))

    @staticmethod
    def _insert_action(connection, alert_id, action, actor, note, previous, new, created_at):
        connection.execute("INSERT INTO alert_actions VALUES(?,?,?,?,?,?,?,?)", (str(uuid4()), alert_id, action, actor, safe_alert_text(note), previous, new, created_at))

    async def auto_resolve(self, *, thread_id: str, branch_id: str, active_fingerprints: set[str], rules: list[AlertRule]) -> int:
        return await asyncio.to_thread(self._auto_resolve_sync, thread_id, branch_id, active_fingerprints, rules)

    def _auto_resolve_sync(self, thread_id, branch_id, active_fingerprints, rules):
        auto = {rule.rule_id for rule in rules if rule.auto_resolve}
        if not auto: return 0
        placeholders = ",".join("?" for _ in auto); now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(f"SELECT alert_id,status,fingerprint,rule_code FROM workflow_alerts WHERE thread_id=? AND branch_id=? AND rule_id IN ({placeholders}) AND status!='resolved'", (thread_id, branch_id, *auto)).fetchall()
            rows = [row for row in rows if row["fingerprint"] not in active_fingerprints]
            for row in rows:
                note = "The approval operation is no longer pending." if row["rule_code"] == "APPROVAL_WAIT_TOO_LONG" else "Condition cleared"
                connection.execute("UPDATE workflow_alerts SET status='resolved',resolved_at=?,resolved_by='system',resolution_note=?,muted_until=NULL,updated_at=? WHERE alert_id=?", (now, note, now, row["alert_id"]))
                self._insert_action(connection, row["alert_id"], "auto_resolve", "system", note, row["status"], "resolved", now)
        return len(rows)

    async def auto_resolve_alert(self, alert_id: str, *, actor: str, note: str) -> tuple[WorkflowAlert | None, bool]:
        return await asyncio.to_thread(self._auto_resolve_alert_sync, alert_id, actor, note)

    def _auto_resolve_alert_sync(self, alert_id: str, actor: str, note: str):
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM workflow_alerts WHERE alert_id=?", (alert_id,)).fetchone()
            if row is None:
                return None, False
            if row["status"] == "resolved":
                return self._alert(row), False
            connection.execute(
                """UPDATE workflow_alerts SET status='resolved',resolved_at=?,resolved_by=?,
                   resolution_note=?,muted_until=NULL,updated_at=? WHERE alert_id=? AND status!='resolved'""",
                (now, safe_alert_text(actor, 120), safe_alert_text(note), now, alert_id),
            )
            self._insert_action(connection, alert_id, "auto_resolve", actor, note, row["status"], "resolved", now)
            updated = connection.execute("SELECT * FROM workflow_alerts WHERE alert_id=?", (alert_id,)).fetchone()
        return self._alert(updated), True

    async def list_alerts(self, **filters) -> tuple[list[WorkflowAlert], int, AlertStatusCounts]:
        return await asyncio.to_thread(self._list_alerts_sync, filters)

    def _list_alerts_sync(self, filters):
        self._expire_mutes_sync()
        clauses, params = [], []
        for field in ("severity", "rule_code", "category", "thread_id", "branch_id"):
            if filters.get(field): clauses.append(f"{field}=?"); params.append(filters[field])
        statuses = filters.get("statuses")
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})"); params.extend(statuses)
        if filters.get("project_name"): clauses.append("project_name LIKE ?"); params.append(f"%{safe_alert_text(filters['project_name'], 100)}%")
        if filters.get("search"): clauses.append("(title LIKE ? OR message LIKE ?)"); params.extend([f"%{safe_alert_text(filters['search'], 100)}%"] * 2)
        if filters.get("date_from"): clauses.append("last_detected_at>=?"); params.append(filters["date_from"])
        if filters.get("date_to"): clauses.append("last_detected_at<=?"); params.append(filters["date_to"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order_field = filters.get("sort_by", "severity"); direction = filters.get("sort_order", "desc").upper()
        order = f"CASE severity WHEN 'critical' THEN 4 WHEN 'error' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END {direction}, last_detected_at DESC" if order_field == "severity" else f"{order_field} {direction}"
        limit, offset = filters.get("limit", 50), filters.get("offset", 0)
        with self._connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM workflow_alerts{where}", params).fetchone()[0])
            rows = connection.execute(f"SELECT * FROM workflow_alerts{where} ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            count_rows = connection.execute("SELECT status,severity,COUNT(*) count FROM workflow_alerts GROUP BY status,severity").fetchall()
        counts = Counter(); [counts.update({row["status"]: row["count"], row["severity"]: row["count"]}) for row in count_rows]
        return [self._alert(row) for row in rows], total, AlertStatusCounts(**{field: counts[field] for field in AlertStatusCounts.model_fields})

    async def get_alert(self, alert_id: str) -> WorkflowAlert | None:
        return await asyncio.to_thread(self._get_alert_sync, alert_id)

    async def get_alert_by_fingerprint(self, fingerprint: str) -> WorkflowAlert | None:
        return await asyncio.to_thread(self._get_alert_by_fingerprint_sync, fingerprint)

    def _get_alert_by_fingerprint_sync(self, fingerprint):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM workflow_alerts WHERE fingerprint=?", (fingerprint,)).fetchone()
        return self._alert(row) if row else None

    def _get_alert_sync(self, alert_id):
        self._expire_mutes_sync()
        with self._connect() as connection: row = connection.execute("SELECT * FROM workflow_alerts WHERE alert_id=?", (alert_id,)).fetchone()
        return self._alert(row) if row else None

    async def history(self, alert_id: str, limit: int = 100) -> tuple[list[AlertOccurrence], list[AlertAction]]:
        return await asyncio.to_thread(self._history_sync, alert_id, limit)

    def _history_sync(self, alert_id, limit):
        with self._connect() as connection:
            occurrences = connection.execute("SELECT * FROM alert_occurrences WHERE alert_id=? ORDER BY detected_at DESC LIMIT ?", (alert_id, limit)).fetchall()
            actions = connection.execute("SELECT * FROM alert_actions WHERE alert_id=? ORDER BY created_at DESC LIMIT ?", (alert_id, limit)).fetchall()
        occurrence_models = []
        for row in occurrences:
            item = dict(row)
            item.pop("alert_id")
            item["evidence"] = AlertEvidence(**json.loads(item.pop("evidence_json")))
            occurrence_models.append(AlertOccurrence(**item))
        return (occurrence_models, [AlertAction(**{key: value for key, value in dict(row).items() if key != "alert_id"}) for row in actions])

    async def transition(self, alert_id: str, *, action: str, actor: str, note: str | None = None, duration_seconds: int | None = None) -> tuple[WorkflowAlert | None, bool]:
        return await asyncio.to_thread(self._transition_sync, alert_id, action, actor, note, duration_seconds)

    def _transition_sync(self, alert_id, action, actor, note, duration_seconds):
        now = datetime.now(UTC); now_text = now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM workflow_alerts WHERE alert_id=?", (alert_id,)).fetchone()
            if row is None: return None, False
            previous = row["status"]
            valid = {"acknowledge": {"open", "acknowledged"}, "resolve": {"open", "acknowledged", "muted", "resolved"}, "reopen": {"resolved"}, "mute": {"open", "acknowledged", "muted"}, "unmute": {"muted"}}
            if previous not in valid[action]: return self._alert(row), False
            target = {"acknowledge": "acknowledged", "resolve": "resolved", "reopen": "open", "mute": "muted", "unmute": "acknowledged" if self._was_acknowledged(connection, alert_id) else "open"}[action]
            idempotent = previous == target
            muted_until = (now + timedelta(seconds=duration_seconds)).isoformat() if action == "mute" and duration_seconds else (row["muted_until"] if target == "muted" else None)
            acknowledged_at = row["acknowledged_at"] or (now_text if action == "acknowledge" else None)
            acknowledged_by = row["acknowledged_by"] or (safe_alert_text(actor, 120) if action == "acknowledge" else None)
            resolved_at = now_text if target == "resolved" else None; resolved_by = safe_alert_text(actor, 120) if target == "resolved" else None
            connection.execute("UPDATE workflow_alerts SET status=?,acknowledged_at=?,acknowledged_by=?,resolved_at=?,resolved_by=?,resolution_note=?,muted_until=?,updated_at=? WHERE alert_id=?", (target, acknowledged_at, acknowledged_by, resolved_at, resolved_by, safe_alert_text(note) if target == "resolved" else None, muted_until, now_text, alert_id))
            if not idempotent: self._insert_action(connection, alert_id, action, safe_alert_text(actor, 120), note, previous, target, now_text)
            updated = connection.execute("SELECT * FROM workflow_alerts WHERE alert_id=?", (alert_id,)).fetchone()
        return self._alert(updated), True

    @staticmethod
    def _was_acknowledged(connection, alert_id):
        return connection.execute("SELECT 1 FROM alert_actions WHERE alert_id=? AND action='acknowledge' LIMIT 1", (alert_id,)).fetchone() is not None

    def _expire_mutes_sync(self):
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            rows = connection.execute("SELECT alert_id FROM workflow_alerts WHERE status='muted' AND muted_until IS NOT NULL AND muted_until<=?", (now,)).fetchall()
            for row in rows:
                target = "acknowledged" if self._was_acknowledged(connection, row["alert_id"]) else "open"
                connection.execute("UPDATE workflow_alerts SET status=?,muted_until=NULL,updated_at=? WHERE alert_id=?", (target, now, row["alert_id"]))
                self._insert_action(connection, row["alert_id"], "mute_expired", "system", None, "muted", target, now)

    async def acquire_lock(self, owner_id: str, ttl_seconds: int) -> bool:
        return await asyncio.to_thread(self._acquire_lock_sync, owner_id, ttl_seconds)

    def _acquire_lock_sync(self, owner_id, ttl_seconds):
        now = datetime.now(UTC); expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM alert_evaluator_locks WHERE lock_name='periodic' AND expires_at<=?", (now.isoformat(),))
            try: connection.execute("INSERT INTO alert_evaluator_locks VALUES('periodic',?,?)", (owner_id, expires))
            except sqlite3.IntegrityError: return False
        return True

    async def release_lock(self, owner_id: str) -> None:
        await asyncio.to_thread(self._release_lock_sync, owner_id)

    def _release_lock_sync(self, owner_id):
        with self._connect() as connection: connection.execute("DELETE FROM alert_evaluator_locks WHERE lock_name='periodic' AND owner_id=?", (owner_id,))
