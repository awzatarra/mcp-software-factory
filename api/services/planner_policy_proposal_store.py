from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import uuid4


PROPOSAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS planner_policy_proposals (
 proposal_id TEXT PRIMARY KEY,
 proposal_version TEXT NOT NULL,
 source_recommendation_id TEXT NOT NULL,
 source_recommendation_fingerprint TEXT NOT NULL,
 source_review_id TEXT,
 source_policy TEXT NOT NULL,
 source_segment TEXT,
 source_direction TEXT NOT NULL,
 policy_key TEXT NOT NULL,
 policy_scope TEXT NOT NULL,
 segment TEXT,
 current_value_json TEXT NOT NULL,
 proposed_value_json TEXT,
 change_type TEXT NOT NULL,
 rationale TEXT NOT NULL,
 evidence_summary_json TEXT NOT NULL,
 reason_codes_json TEXT NOT NULL,
 status TEXT NOT NULL,
 application_status TEXT NOT NULL,
 proposal_fingerprint TEXT NOT NULL,
 proposal_risk_level TEXT NOT NULL,
 affected_policy_area TEXT NOT NULL,
 affected_workflows_scope TEXT NOT NULL,
 simulation_json TEXT,
 safety_flags_json TEXT NOT NULL,
 absolute_change REAL,
 relative_change_pct REAL,
 reviewer TEXT,
 review_notes TEXT,
 reviewed_at TEXT,
 decision_reason TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(source_recommendation_id,source_recommendation_fingerprint,proposal_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_policy_proposals_status
 ON planner_policy_proposals(status,policy_key,proposal_risk_level,updated_at DESC);
CREATE TABLE IF NOT EXISTS planner_policy_proposal_history (
 event_id TEXT PRIMARY KEY,
 proposal_id TEXT NOT NULL,
 event_type TEXT NOT NULL,
 from_status TEXT,
 to_status TEXT NOT NULL,
 actor TEXT,
 notes TEXT,
 reason TEXT,
 proposal_fingerprint TEXT NOT NULL,
 timestamp TEXT NOT NULL,
 FOREIGN KEY(proposal_id) REFERENCES planner_policy_proposals(proposal_id)
);
CREATE INDEX IF NOT EXISTS idx_policy_proposal_history
 ON planner_policy_proposal_history(proposal_id,timestamp);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for key in ("current_value", "proposed_value", "evidence_summary", "reason_codes", "simulation", "safety_flags"):
        raw_key = f"{key}_json"
        if raw_key in item:
            item[key] = _loads(item.pop(raw_key))
    return item


class PlannerPolicyProposalStore:
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
            connection.executescript(PROPOSAL_SCHEMA)

    async def create(self, proposal: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        return await asyncio.to_thread(self.create_sync, dict(proposal))

    def create_sync(self, proposal: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        timestamp = now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT * FROM planner_policy_proposals
                WHERE source_recommendation_id=? AND source_recommendation_fingerprint=? AND proposal_fingerprint=?""",
                (
                    proposal["source_recommendation_id"],
                    proposal["source_recommendation_fingerprint"],
                    proposal["proposal_fingerprint"],
                ),
            ).fetchone()
            if existing:
                connection.commit()
                return _row(existing) or {}, False
            proposal_id = uuid4().hex
            values = (
                proposal_id,
                proposal["proposal_version"],
                proposal["source_recommendation_id"],
                proposal["source_recommendation_fingerprint"],
                proposal.get("source_review_id"),
                proposal["source_policy"],
                proposal.get("source_segment"),
                proposal["source_direction"],
                proposal["policy_key"],
                proposal["policy_scope"],
                proposal.get("segment"),
                _json(proposal["current_value"]),
                _json(proposal.get("proposed_value")),
                proposal["change_type"],
                proposal["rationale"],
                _json(proposal["evidence_summary"]),
                _json(proposal["reason_codes"]),
                "draft",
                "not_applied",
                proposal["proposal_fingerprint"],
                proposal["proposal_risk_level"],
                proposal["affected_policy_area"],
                proposal["affected_workflows_scope"],
                _json(proposal.get("simulation")),
                _json(proposal["safety_flags"]),
                proposal.get("absolute_change"),
                proposal.get("relative_change_pct"),
                timestamp,
                timestamp,
            )
            connection.execute(
                """INSERT INTO planner_policy_proposals(
                proposal_id,proposal_version,source_recommendation_id,source_recommendation_fingerprint,
                source_review_id,source_policy,source_segment,source_direction,policy_key,policy_scope,segment,
                current_value_json,proposed_value_json,change_type,rationale,evidence_summary_json,
                reason_codes_json,status,application_status,proposal_fingerprint,proposal_risk_level,
                affected_policy_area,affected_workflows_scope,simulation_json,safety_flags_json,
                absolute_change,relative_change_pct,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            connection.execute(
                """INSERT INTO planner_policy_proposal_history(
                event_id,proposal_id,event_type,from_status,to_status,actor,notes,reason,proposal_fingerprint,timestamp)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid4().hex,
                    proposal_id,
                    "proposal_created",
                    None,
                    "draft",
                    None,
                    None,
                    None,
                    proposal["proposal_fingerprint"],
                    timestamp,
                ),
            )
            row = connection.execute("SELECT * FROM planner_policy_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            connection.commit()
            return _row(row) or {}, True

    async def get(self, proposal_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_sync, proposal_id)

    def get_sync(self, proposal_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return _row(connection.execute("SELECT * FROM planner_policy_proposals WHERE proposal_id=?", (proposal_id,)).fetchone())

    async def list(
        self,
        *,
        status: str | None = None,
        policy_key: str | None = None,
        risk_level: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_sync, status, policy_key, risk_level, limit)

    def list_sync(self, status: str | None, policy_key: str | None, risk_level: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if policy_key:
            clauses.append("policy_key=?")
            params.append(policy_key)
        if risk_level:
            clauses.append("proposal_risk_level=?")
            params.append(risk_level)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as connection:
            return [_row(row) or {} for row in connection.execute(
                f"SELECT * FROM planner_policy_proposals {where} ORDER BY updated_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()]

    async def history(self, proposal_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.history_sync, proposal_id)

    def history_sync(self, proposal_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM planner_policy_proposal_history WHERE proposal_id=? ORDER BY timestamp ASC",
                (proposal_id,),
            ).fetchall()]

    async def transition(
        self,
        proposal_id: str,
        *,
        to_status: str,
        actor: str,
        fingerprint: str,
        notes: str | None = None,
        reason: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        return await asyncio.to_thread(self.transition_sync, proposal_id, to_status, actor, fingerprint, notes, reason)

    def transition_sync(
        self,
        proposal_id: str,
        to_status: str,
        actor: str,
        fingerprint: str,
        notes: str | None,
        reason: str | None,
    ) -> tuple[dict[str, Any], bool]:
        timestamp = now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM planner_policy_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None:
                connection.commit()
                raise ValueError("planner_policy_proposal_not_found")
            if row["proposal_fingerprint"] != fingerprint:
                connection.commit()
                raise ValueError("planner_policy_proposal_stale")
            from_status = str(row["status"])
            if from_status == to_status:
                connection.commit()
                return _row(row) or {}, False
            allowed = {
                "draft": {"ready_for_review", "cancelled"},
                "ready_for_review": {"approved", "rejected", "cancelled"},
            }
            if to_status not in allowed.get(from_status, set()):
                connection.commit()
                raise ValueError("planner_policy_proposal_transition_not_allowed")
            application_status = "not_applied" if to_status in {"ready_for_review", "approved"} else "not_applicable"
            event_type = {
                "ready_for_review": "ready_for_review",
                "approved": "approved",
                "rejected": "rejected",
                "cancelled": "cancelled",
            }[to_status]
            connection.execute(
                """UPDATE planner_policy_proposals SET status=?,application_status=?,reviewer=?,
                review_notes=?,reviewed_at=?,decision_reason=?,updated_at=? WHERE proposal_id=?""",
                (to_status, application_status, actor, notes, timestamp, reason, timestamp, proposal_id),
            )
            connection.execute(
                """INSERT INTO planner_policy_proposal_history(
                event_id,proposal_id,event_type,from_status,to_status,actor,notes,reason,proposal_fingerprint,timestamp)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid4().hex,
                    proposal_id,
                    event_type,
                    from_status,
                    to_status,
                    actor,
                    notes,
                    reason,
                    fingerprint,
                    timestamp,
                ),
            )
            updated = connection.execute("SELECT * FROM planner_policy_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            connection.commit()
            return _row(updated) or {}, True

    async def set_application_status(self, proposal_id: str, application_status: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.set_application_status_sync, proposal_id, application_status)

    def set_application_status_sync(self, proposal_id: str, application_status: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE planner_policy_proposals SET application_status=?,updated_at=? WHERE proposal_id=?",
                (application_status, now(), proposal_id),
            )
            return _row(connection.execute("SELECT * FROM planner_policy_proposals WHERE proposal_id=?", (proposal_id,)).fetchone())
