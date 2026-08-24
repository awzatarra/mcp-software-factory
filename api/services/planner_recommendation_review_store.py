from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import uuid4


REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS planner_recommendation_reviews (
 recommendation_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 analytics_version TEXT NOT NULL,
 policy TEXT NOT NULL,
 segment TEXT,
 direction TEXT NOT NULL,
 review_status TEXT NOT NULL,
 reviewer TEXT,
 review_notes TEXT,
 reviewed_at TEXT,
 decision_reason TEXT,
 deferred_until TEXT,
 recommendation_json TEXT NOT NULL,
 review_version INTEGER NOT NULL DEFAULT 1,
 application_status TEXT NOT NULL DEFAULT 'not_applicable',
 updated_at TEXT NOT NULL,
 PRIMARY KEY(recommendation_id,fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_planner_review_status
 ON planner_recommendation_reviews(review_status,policy,updated_at DESC);
CREATE TABLE IF NOT EXISTS planner_recommendation_review_history (
 review_id TEXT PRIMARY KEY,
 recommendation_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 analytics_version TEXT NOT NULL,
 policy TEXT NOT NULL,
 segment TEXT,
 direction TEXT NOT NULL,
 from_status TEXT NOT NULL,
 to_status TEXT NOT NULL,
 reviewer TEXT,
 notes TEXT,
 reason TEXT,
 deferred_until TEXT,
 timestamp TEXT NOT NULL,
 review_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_planner_review_history_recommendation
 ON planner_recommendation_review_history(recommendation_id,fingerprint,timestamp);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    if "recommendation_json" in item:
        try:
            item["recommendation"] = json.loads(item.pop("recommendation_json") or "{}")
        except json.JSONDecodeError:
            item["recommendation"] = {}
    return item


class PlannerRecommendationReviewStore:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self.initialize_sync)

    def initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(REVIEW_SCHEMA)

    async def get(self, recommendation_id: str, fingerprint: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_sync, recommendation_id, fingerprint)

    def get_sync(self, recommendation_id: str, fingerprint: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return _row(
                connection.execute(
                    "SELECT * FROM planner_recommendation_reviews WHERE recommendation_id=? AND fingerprint=?",
                    (recommendation_id, fingerprint),
                ).fetchone()
            )

    async def history(self, recommendation_id: str, fingerprint: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.history_sync, recommendation_id, fingerprint)

    def history_sync(self, recommendation_id: str, fingerprint: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM planner_recommendation_review_history WHERE recommendation_id=?"
        params: tuple[Any, ...] = (recommendation_id,)
        if fingerprint:
            sql += " AND fingerprint=?"
            params = (recommendation_id, fingerprint)
        sql += " ORDER BY timestamp ASC"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    async def list(
        self,
        *,
        status: str | None = None,
        policy: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_sync, status, policy, limit)

    def list_sync(self, status: str | None, policy: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("review_status=?")
            params.append(status)
        if policy:
            clauses.append("policy=?")
            params.append(policy)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as connection:
            return [
                _row(row) or {}
                for row in connection.execute(
                    f"SELECT * FROM planner_recommendation_reviews {where} ORDER BY updated_at DESC LIMIT ?",
                    tuple(params),
                ).fetchall()
            ]

    async def transition(
        self,
        recommendation: Mapping[str, Any],
        *,
        to_status: str,
        reviewer: str,
        notes: str | None = None,
        reason: str | None = None,
        deferred_until: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        return await asyncio.to_thread(
            self.transition_sync,
            dict(recommendation),
            to_status,
            reviewer,
            notes,
            reason,
            deferred_until,
        )

    def transition_sync(
        self,
        recommendation: dict[str, Any],
        to_status: str,
        reviewer: str,
        notes: str | None,
        reason: str | None,
        deferred_until: str | None,
    ) -> tuple[dict[str, Any], bool]:
        recommendation_id = str(recommendation["recommendation_id"])
        fingerprint = str(recommendation["recommendation_fingerprint"])
        analytics_version = str(recommendation["analytics_version"])
        policy = str(recommendation["policy"])
        segment = recommendation.get("segment")
        direction = str(recommendation["direction"])
        timestamp = now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM planner_recommendation_reviews WHERE recommendation_id=? AND fingerprint=?",
                (recommendation_id, fingerprint),
            ).fetchone()
            from_status = str(existing["review_status"]) if existing else "recommendation_only"
            if existing and from_status == to_status:
                connection.commit()
                return _row(existing) or {}, False
            if from_status in {"accepted", "rejected"}:
                raise ValueError("planner_recommendation_review_terminal")
            allowed = {
                "recommendation_only": {"under_review", "accepted", "rejected", "deferred"},
                "under_review": {"accepted", "rejected", "deferred"},
                "deferred": {"under_review", "accepted", "rejected", "deferred"},
            }
            if to_status not in allowed.get(from_status, set()):
                raise ValueError("planner_recommendation_review_transition_not_allowed")
            review_version = int(existing["review_version"]) + 1 if existing else 1
            application_status = "not_applied" if to_status == "accepted" else "not_applicable"
            values = (
                recommendation_id,
                fingerprint,
                analytics_version,
                policy,
                segment,
                direction,
                to_status,
                reviewer,
                notes,
                timestamp,
                reason,
                deferred_until,
                _json(recommendation),
                review_version,
                application_status,
                timestamp,
            )
            connection.execute(
                """INSERT INTO planner_recommendation_reviews(
                recommendation_id,fingerprint,analytics_version,policy,segment,direction,review_status,
                reviewer,review_notes,reviewed_at,decision_reason,deferred_until,recommendation_json,
                review_version,application_status,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(recommendation_id,fingerprint) DO UPDATE SET
                analytics_version=excluded.analytics_version,policy=excluded.policy,segment=excluded.segment,
                direction=excluded.direction,review_status=excluded.review_status,reviewer=excluded.reviewer,
                review_notes=excluded.review_notes,reviewed_at=excluded.reviewed_at,
                decision_reason=excluded.decision_reason,deferred_until=excluded.deferred_until,
                recommendation_json=excluded.recommendation_json,review_version=excluded.review_version,
                application_status=excluded.application_status,updated_at=excluded.updated_at""",
                values,
            )
            connection.execute(
                """INSERT INTO planner_recommendation_review_history(
                review_id,recommendation_id,fingerprint,analytics_version,policy,segment,direction,
                from_status,to_status,reviewer,notes,reason,deferred_until,timestamp,review_version)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid4().hex,
                    recommendation_id,
                    fingerprint,
                    analytics_version,
                    policy,
                    segment,
                    direction,
                    from_status,
                    to_status,
                    reviewer,
                    notes,
                    reason,
                    deferred_until,
                    timestamp,
                    review_version,
                ),
            )
            row = connection.execute(
                "SELECT * FROM planner_recommendation_reviews WHERE recommendation_id=? AND fingerprint=?",
                (recommendation_id, fingerprint),
            ).fetchone()
            connection.commit()
            return _row(row) or {}, True
