from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from graph.planner_calibration import PlannerPolicySnapshot
from graph.planner_policy_registry import (
    POLICY_REGISTRY_VERSION,
    metadata_for,
    validate_policy_value,
)
from graph.planner_policy_rollout import ACTIVE_ROLLOUT_STATUSES, POLICY_ROLLOUT_VERSION
from graph.planner_policy_experiment import ACTIVE_EXPERIMENT_STATUSES, POLICY_EXPERIMENT_VERSION


RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS planner_runtime_policies (
 policy_key TEXT NOT NULL,
 scope TEXT NOT NULL,
 value_json TEXT NOT NULL,
 revision INTEGER NOT NULL,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(policy_key,scope)
);
CREATE TABLE IF NOT EXISTS planner_policy_applications (
 application_id TEXT PRIMARY KEY,
 proposal_id TEXT NOT NULL,
 proposal_fingerprint TEXT NOT NULL,
 application_fingerprint TEXT NOT NULL,
 policy_key TEXT NOT NULL,
 scope TEXT NOT NULL,
 previous_value_json TEXT NOT NULL,
 proposed_value_json TEXT NOT NULL,
 status TEXT NOT NULL,
 actor TEXT,
 notes TEXT,
 error_code TEXT,
 error_message TEXT,
 baseline_revision INTEGER NOT NULL,
 applied_revision INTEGER,
 rollback_revision INTEGER,
 rollback_supported INTEGER NOT NULL,
 verification_strategy TEXT NOT NULL,
 application_risk_level TEXT NOT NULL,
 created_at TEXT NOT NULL,
 started_at TEXT,
 completed_at TEXT,
 rolled_back_at TEXT,
 UNIQUE(proposal_id,proposal_fingerprint,application_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_policy_applications_status
 ON planner_policy_applications(status,policy_key,proposal_id,created_at DESC);
CREATE TABLE IF NOT EXISTS planner_policy_application_history (
 event_id TEXT PRIMARY KEY,
 application_id TEXT NOT NULL,
 event_type TEXT NOT NULL,
 from_status TEXT,
 to_status TEXT NOT NULL,
 actor TEXT,
 policy_key TEXT NOT NULL,
 scope TEXT NOT NULL,
 proposal_id TEXT NOT NULL,
 application_fingerprint TEXT NOT NULL,
 revision_before INTEGER,
 revision_after INTEGER,
 error_code TEXT,
 timestamp TEXT NOT NULL,
 FOREIGN KEY(application_id) REFERENCES planner_policy_applications(application_id)
);
CREATE TABLE IF NOT EXISTS planner_policy_rollouts (
 rollout_id TEXT PRIMARY KEY,
 application_id TEXT NOT NULL,
 proposal_id TEXT NOT NULL,
 policy_key TEXT NOT NULL,
 scope TEXT NOT NULL,
 baseline_revision INTEGER NOT NULL,
 target_revision INTEGER NOT NULL,
 previous_value_json TEXT NOT NULL,
 target_value_json TEXT NOT NULL,
 status TEXT NOT NULL,
 current_percentage INTEGER NOT NULL,
 target_percentage INTEGER NOT NULL,
 strategy TEXT NOT NULL,
 stages_json TEXT NOT NULL,
 baseline_window_json TEXT NOT NULL,
 observation_window_json TEXT NOT NULL,
 baseline_metrics_json TEXT NOT NULL,
 treatment_metrics_json TEXT,
 control_metrics_json TEXT,
 delta_metrics_json TEXT,
 health_status TEXT NOT NULL,
 health_score INTEGER NOT NULL,
 rollout_fingerprint TEXT NOT NULL,
 application_fingerprint TEXT NOT NULL,
 version TEXT NOT NULL,
 error_code TEXT,
 created_at TEXT NOT NULL,
 started_at TEXT,
 completed_at TEXT,
 paused_at TEXT,
 rolled_back_at TEXT,
 UNIQUE(application_id,rollout_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_policy_rollouts_status
 ON planner_policy_rollouts(status,policy_key,scope,created_at DESC);
CREATE TABLE IF NOT EXISTS planner_policy_rollout_history (
 event_id TEXT PRIMARY KEY,
 rollout_id TEXT NOT NULL,
 event_type TEXT NOT NULL,
 from_status TEXT,
 to_status TEXT NOT NULL,
 actor TEXT,
 policy_key TEXT NOT NULL,
 scope TEXT NOT NULL,
 percentage INTEGER NOT NULL,
 health_status TEXT,
 health_score INTEGER,
 revision INTEGER,
 timestamp TEXT NOT NULL,
 FOREIGN KEY(rollout_id) REFERENCES planner_policy_rollouts(rollout_id)
);
CREATE TABLE IF NOT EXISTS planner_policy_workflow_snapshots (
 snapshot_id TEXT PRIMARY KEY,
 workflow_id TEXT NOT NULL,
 policy_key TEXT NOT NULL,
 scope TEXT NOT NULL,
 revision INTEGER NOT NULL,
 rollout_id TEXT,
 treatment INTEGER NOT NULL,
 effective_value_json TEXT NOT NULL,
 assignment_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(workflow_id,policy_key,scope)
);
CREATE INDEX IF NOT EXISTS idx_policy_workflow_snapshots_workflow
 ON planner_policy_workflow_snapshots(workflow_id,created_at DESC);
CREATE TABLE IF NOT EXISTS planner_policy_experiments (
 experiment_id TEXT PRIMARY KEY,
 experiment_version TEXT NOT NULL,
 policy_key TEXT NOT NULL,
 scope TEXT NOT NULL,
 baseline_revision INTEGER NOT NULL,
 control_value_json TEXT NOT NULL,
 variants_json TEXT NOT NULL,
 allocation_json TEXT NOT NULL,
 status TEXT NOT NULL,
 minimum_sample_size INTEGER NOT NULL,
 observation_window_json TEXT NOT NULL,
 primary_metric TEXT NOT NULL,
 secondary_metrics_json TEXT NOT NULL,
 guardrails_json TEXT NOT NULL,
 metrics_json TEXT,
 result TEXT,
 winner_variant_id TEXT,
 experiment_fingerprint TEXT NOT NULL,
 created_at TEXT NOT NULL,
 started_at TEXT,
 completed_at TEXT,
 UNIQUE(policy_key,scope,experiment_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_policy_experiments_status
 ON planner_policy_experiments(status,policy_key,scope,created_at DESC);
CREATE TABLE IF NOT EXISTS planner_policy_experiment_variants (
 experiment_id TEXT NOT NULL,
 variant_id TEXT NOT NULL,
 name TEXT NOT NULL,
 value_json TEXT NOT NULL,
 proposal_id TEXT,
 application_id TEXT,
 allocation_percentage INTEGER NOT NULL,
 risk_level TEXT NOT NULL,
 status TEXT NOT NULL,
 PRIMARY KEY(experiment_id,variant_id),
 FOREIGN KEY(experiment_id) REFERENCES planner_policy_experiments(experiment_id)
);
CREATE TABLE IF NOT EXISTS planner_policy_experiment_history (
 event_id TEXT PRIMARY KEY,
 experiment_id TEXT NOT NULL,
 event_type TEXT NOT NULL,
 from_status TEXT,
 to_status TEXT NOT NULL,
 actor TEXT,
 policy_key TEXT NOT NULL,
 scope TEXT NOT NULL,
 result TEXT,
 winner_variant_id TEXT,
 timestamp TEXT NOT NULL,
 FOREIGN KEY(experiment_id) REFERENCES planner_policy_experiments(experiment_id)
);
CREATE TABLE IF NOT EXISTS planner_policy_experiment_snapshots (
 snapshot_id TEXT PRIMARY KEY,
 workflow_id TEXT NOT NULL,
 policy_key TEXT NOT NULL,
 scope TEXT NOT NULL,
 revision INTEGER NOT NULL,
 experiment_id TEXT,
 variant_id TEXT NOT NULL,
 effective_value_json TEXT NOT NULL,
 assignment_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(workflow_id,policy_key,scope)
);
CREATE INDEX IF NOT EXISTS idx_policy_experiment_snapshots_workflow
 ON planner_policy_experiment_snapshots(workflow_id,created_at DESC);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for field in (
        "previous_value",
        "proposed_value",
        "value",
        "target_value",
        "stages",
        "baseline_window",
        "observation_window",
        "baseline_metrics",
        "treatment_metrics",
        "control_metrics",
        "delta_metrics",
        "effective_value",
        "assignment",
        "control_value",
        "variants",
        "allocation",
        "observation_window",
        "secondary_metrics",
        "guardrails",
        "metrics",
    ):
        raw = f"{field}_json"
        if raw in item:
            item[field] = _loads(item.pop(raw))
    if "rollback_supported" in item:
        item["rollback_supported"] = bool(item["rollback_supported"])
    return item


class PlannerPolicyRuntimeStore:
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
            connection.executescript(RUNTIME_SCHEMA)

    def default_value(self, policy_key: str) -> Any:
        return metadata_for(policy_key).default_getter(PlannerPolicySnapshot())

    async def get_policy(self, policy_key: str, scope: str = "global_planner") -> dict[str, Any]:
        return await asyncio.to_thread(self.get_policy_sync, policy_key, scope)

    def get_policy_sync(self, policy_key: str, scope: str = "global_planner") -> dict[str, Any]:
        metadata_for(policy_key)
        with self.connect() as connection:
            return self._get_policy_with_connection(connection, policy_key, scope)

    def _get_policy_with_connection(
        self,
        connection: sqlite3.Connection,
        policy_key: str,
        scope: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT policy_key,scope,value_json,revision,updated_at FROM planner_runtime_policies WHERE policy_key=? AND scope=?",
            (policy_key, scope),
        ).fetchone()
        if row is not None:
            item = _row(row) or {}
            item["registry_version"] = POLICY_REGISTRY_VERSION
            return item
        return {
            "policy_key": policy_key,
            "scope": scope,
            "value": self.default_value(policy_key),
            "revision": 0,
            "updated_at": None,
            "registry_version": POLICY_REGISTRY_VERSION,
        }

    async def set_policy(
        self,
        policy_key: str,
        scope: str,
        value: Any,
        *,
        expected_value: Any,
        expected_revision: int,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.set_policy_sync, policy_key, scope, value, expected_value, expected_revision)

    def set_policy_sync(
        self,
        policy_key: str,
        scope: str,
        value: Any,
        expected_value: Any,
        expected_revision: int,
    ) -> dict[str, Any]:
        validate_policy_value(policy_key, value)
        timestamp = now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_policy_with_connection(connection, policy_key, scope)
            if current["value"] != expected_value or int(current["revision"]) != int(expected_revision):
                connection.commit()
                raise ValueError("planner_policy_application_stale")
            next_revision = int(current["revision"]) + 1
            connection.execute(
                """INSERT INTO planner_runtime_policies(policy_key,scope,value_json,revision,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(policy_key,scope) DO UPDATE SET
                value_json=excluded.value_json,revision=excluded.revision,updated_at=excluded.updated_at""",
                (policy_key, scope, _json(value), next_revision, timestamp),
            )
            updated = self._get_policy_with_connection(connection, policy_key, scope)
            connection.commit()
            return updated

    async def create_application(self, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        return await asyncio.to_thread(self.create_application_sync, dict(data))

    def create_application_sync(self, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        timestamp = now()
        metadata = metadata_for(data["policy_key"])
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT * FROM planner_policy_applications
                WHERE proposal_id=? AND proposal_fingerprint=? AND application_fingerprint=?""",
                (data["proposal_id"], data["proposal_fingerprint"], data["application_fingerprint"]),
            ).fetchone()
            if existing:
                connection.commit()
                return _row(existing) or {}, False
            application_id = uuid4().hex
            connection.execute(
                """INSERT INTO planner_policy_applications(
                application_id,proposal_id,proposal_fingerprint,application_fingerprint,policy_key,scope,
                previous_value_json,proposed_value_json,status,actor,notes,baseline_revision,rollback_supported,
                verification_strategy,application_risk_level,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    application_id,
                    data["proposal_id"],
                    data["proposal_fingerprint"],
                    data["application_fingerprint"],
                    data["policy_key"],
                    data["scope"],
                    _json(data["previous_value"]),
                    _json(data["proposed_value"]),
                    "prepared",
                    data.get("actor"),
                    data.get("notes"),
                    data["baseline_revision"],
                    int(metadata.rollback_supported),
                    metadata.verification_strategy,
                    data["application_risk_level"],
                    timestamp,
                ),
            )
            self._append_history(connection, application_id, "application_prepared", None, "prepared", data, None, None)
            row = connection.execute("SELECT * FROM planner_policy_applications WHERE application_id=?", (application_id,)).fetchone()
            connection.commit()
            return _row(row) or {}, True

    async def get_application(self, application_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_application_sync, application_id)

    def get_application_sync(self, application_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return _row(connection.execute("SELECT * FROM planner_policy_applications WHERE application_id=?", (application_id,)).fetchone())

    async def list_applications(
        self,
        *,
        status: str | None = None,
        policy_key: str | None = None,
        proposal_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_applications_sync, status, policy_key, proposal_id, limit)

    def list_applications_sync(self, status: str | None, policy_key: str | None, proposal_id: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if policy_key:
            clauses.append("policy_key=?")
            params.append(policy_key)
        if proposal_id:
            clauses.append("proposal_id=?")
            params.append(proposal_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as connection:
            return [_row(row) or {} for row in connection.execute(
                f"SELECT * FROM planner_policy_applications {where} ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()]

    async def history(self, application_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.history_sync, application_id)

    def history_sync(self, application_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM planner_policy_application_history WHERE application_id=? ORDER BY timestamp",
                (application_id,),
            ).fetchall()]

    def _append_history(
        self,
        connection: sqlite3.Connection,
        application_id: str,
        event_type: str,
        from_status: str | None,
        to_status: str,
        application: dict[str, Any],
        revision_before: int | None,
        revision_after: int | None,
        error_code: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO planner_policy_application_history(
            event_id,application_id,event_type,from_status,to_status,actor,policy_key,scope,proposal_id,
            application_fingerprint,revision_before,revision_after,error_code,timestamp)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                uuid4().hex,
                application_id,
                event_type,
                from_status,
                to_status,
                application.get("actor"),
                application["policy_key"],
                application["scope"],
                application["proposal_id"],
                application["application_fingerprint"],
                revision_before,
                revision_after,
                error_code,
                now(),
            ),
        )

    async def update_application(self, application_id: str, updates: dict[str, Any], event_type: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.update_application_sync, application_id, dict(updates), event_type)

    def update_application_sync(self, application_id: str, updates: dict[str, Any], event_type: str) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _row(connection.execute("SELECT * FROM planner_policy_applications WHERE application_id=?", (application_id,)).fetchone())
            if current is None:
                connection.commit()
                raise ValueError("planner_policy_application_not_found")
            from_status = current["status"]
            assignments = ",".join(f"{key}=?" for key in updates)
            connection.execute(
                f"UPDATE planner_policy_applications SET {assignments} WHERE application_id=?",
                (*updates.values(), application_id),
            )
            updated = _row(connection.execute("SELECT * FROM planner_policy_applications WHERE application_id=?", (application_id,)).fetchone()) or {}
            self._append_history(
                connection,
                application_id,
                event_type,
                from_status,
                updated["status"],
                updated,
                current.get("baseline_revision"),
                updated.get("applied_revision") or updated.get("rollback_revision"),
                updated.get("error_code"),
            )
            connection.commit()
            return updated

    async def create_rollout(self, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        return await asyncio.to_thread(self.create_rollout_sync, dict(data))

    def create_rollout_sync(self, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        timestamp = now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                f"""SELECT * FROM planner_policy_rollouts
                WHERE policy_key=? AND scope=? AND status IN ({",".join("?" for _ in ACTIVE_ROLLOUT_STATUSES)})
                ORDER BY created_at DESC LIMIT 1""",
                (data["policy_key"], data["scope"], *sorted(ACTIVE_ROLLOUT_STATUSES)),
            ).fetchone()
            if active is not None:
                connection.commit()
                raise ValueError("planner_policy_rollout_conflict")
            existing = connection.execute(
                """SELECT * FROM planner_policy_rollouts
                WHERE application_id=? AND rollout_fingerprint=?""",
                (data["application_id"], data["rollout_fingerprint"]),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return _row(existing) or {}, False
            rollout_id = uuid4().hex
            connection.execute(
                """INSERT INTO planner_policy_rollouts(
                rollout_id,application_id,proposal_id,policy_key,scope,baseline_revision,target_revision,
                previous_value_json,target_value_json,status,current_percentage,target_percentage,strategy,stages_json,
                baseline_window_json,observation_window_json,baseline_metrics_json,health_status,health_score,
                rollout_fingerprint,application_fingerprint,version,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rollout_id,
                    data["application_id"],
                    data["proposal_id"],
                    data["policy_key"],
                    data["scope"],
                    data["baseline_revision"],
                    data["target_revision"],
                    _json(data["previous_value"]),
                    _json(data["target_value"]),
                    "prepared",
                    0,
                    data["stages"][0],
                    data.get("strategy", "manual_staged"),
                    _json(data["stages"]),
                    _json(data.get("baseline_window") or {}),
                    _json(data.get("observation_window") or {}),
                    _json(data["baseline_metrics"]),
                    "not_started",
                    0,
                    data["rollout_fingerprint"],
                    data["application_fingerprint"],
                    POLICY_ROLLOUT_VERSION,
                    timestamp,
                ),
            )
            row = connection.execute("SELECT * FROM planner_policy_rollouts WHERE rollout_id=?", (rollout_id,)).fetchone()
            created = _row(row) or {}
            self._append_rollout_history(connection, created, "rollout_prepared", None, "prepared", data.get("actor"), None)
            connection.commit()
            return created, True

    async def get_rollout(self, rollout_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_rollout_sync, rollout_id)

    def get_rollout_sync(self, rollout_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return _row(connection.execute("SELECT * FROM planner_policy_rollouts WHERE rollout_id=?", (rollout_id,)).fetchone())

    async def list_rollouts(
        self,
        *,
        status: str | None = None,
        policy_key: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_rollouts_sync, status, policy_key, limit)

    def list_rollouts_sync(self, status: str | None, policy_key: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if policy_key:
            clauses.append("policy_key=?")
            params.append(policy_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as connection:
            return [_row(row) or {} for row in connection.execute(
                f"SELECT * FROM planner_policy_rollouts {where} ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()]

    async def active_rollout(self, policy_key: str, scope: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.active_rollout_sync, policy_key, scope)

    def active_rollout_sync(self, policy_key: str, scope: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                f"""SELECT * FROM planner_policy_rollouts
                WHERE policy_key=? AND scope=? AND status IN ({",".join("?" for _ in ACTIVE_ROLLOUT_STATUSES)})
                ORDER BY created_at DESC LIMIT 1""",
                (policy_key, scope, *sorted(ACTIVE_ROLLOUT_STATUSES)),
            ).fetchone()
            return _row(row)

    async def update_rollout(self, rollout_id: str, updates: dict[str, Any], event_type: str, *, actor: str | None = None, revision: int | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self.update_rollout_sync, rollout_id, dict(updates), event_type, actor, revision)

    def update_rollout_sync(self, rollout_id: str, updates: dict[str, Any], event_type: str, actor: str | None, revision: int | None) -> dict[str, Any]:
        encoded: dict[str, Any] = {}
        for key, value in updates.items():
            if key in {"baseline_metrics", "treatment_metrics", "control_metrics", "delta_metrics", "baseline_window", "observation_window", "stages", "previous_value", "target_value"}:
                encoded[f"{key}_json"] = _json(value)
            else:
                encoded[key] = value
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _row(connection.execute("SELECT * FROM planner_policy_rollouts WHERE rollout_id=?", (rollout_id,)).fetchone())
            if current is None:
                connection.commit()
                raise ValueError("planner_policy_rollout_not_found")
            from_status = current["status"]
            assignments = ",".join(f"{key}=?" for key in encoded)
            connection.execute(f"UPDATE planner_policy_rollouts SET {assignments} WHERE rollout_id=?", (*encoded.values(), rollout_id))
            updated = _row(connection.execute("SELECT * FROM planner_policy_rollouts WHERE rollout_id=?", (rollout_id,)).fetchone()) or {}
            self._append_rollout_history(connection, updated, event_type, from_status, updated["status"], actor, revision)
            connection.commit()
            return updated

    def _append_rollout_history(
        self,
        connection: sqlite3.Connection,
        rollout: dict[str, Any],
        event_type: str,
        from_status: str | None,
        to_status: str,
        actor: str | None,
        revision: int | None,
    ) -> None:
        connection.execute(
            """INSERT INTO planner_policy_rollout_history(
            event_id,rollout_id,event_type,from_status,to_status,actor,policy_key,scope,percentage,
            health_status,health_score,revision,timestamp)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                uuid4().hex,
                rollout["rollout_id"],
                event_type,
                from_status,
                to_status,
                actor,
                rollout["policy_key"],
                rollout["scope"],
                int(rollout.get("current_percentage") or 0),
                rollout.get("health_status"),
                rollout.get("health_score"),
                revision,
                now(),
            ),
        )

    async def rollout_history(self, rollout_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.rollout_history_sync, rollout_id)

    def rollout_history_sync(self, rollout_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM planner_policy_rollout_history WHERE rollout_id=? ORDER BY timestamp",
                (rollout_id,),
            ).fetchall()]

    async def get_workflow_policy_snapshot(self, workflow_id: str, policy_key: str, scope: str = "global_planner") -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_workflow_policy_snapshot_sync, workflow_id, policy_key, scope)

    def get_workflow_policy_snapshot_sync(self, workflow_id: str, policy_key: str, scope: str = "global_planner") -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM planner_policy_workflow_snapshots WHERE workflow_id=? AND policy_key=? AND scope=?",
                (workflow_id, policy_key, scope),
            ).fetchone()
            item = _row(row)
            if item is not None:
                item["treatment"] = bool(item["treatment"])
            return item

    async def create_workflow_policy_snapshot(self, data: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.create_workflow_policy_snapshot_sync, dict(data))

    def create_workflow_policy_snapshot_sync(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = _row(connection.execute(
                "SELECT * FROM planner_policy_workflow_snapshots WHERE workflow_id=? AND policy_key=? AND scope=?",
                (data["workflow_id"], data["policy_key"], data["scope"]),
            ).fetchone())
            if existing is not None:
                connection.commit()
                existing["treatment"] = bool(existing["treatment"])
                return existing
            snapshot_id = uuid4().hex
            timestamp = now()
            connection.execute(
                """INSERT INTO planner_policy_workflow_snapshots(
                snapshot_id,workflow_id,policy_key,scope,revision,rollout_id,treatment,effective_value_json,assignment_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    data["workflow_id"],
                    data["policy_key"],
                    data["scope"],
                    data["revision"],
                    data.get("rollout_id"),
                    int(bool(data.get("treatment"))),
                    _json(data["effective_value"]),
                    _json(data["assignment"]),
                    timestamp,
                ),
            )
            row = connection.execute("SELECT * FROM planner_policy_workflow_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            connection.commit()
            item = _row(row) or {}
            item["treatment"] = bool(item["treatment"])
            return item

    async def create_experiment(self, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        return await asyncio.to_thread(self.create_experiment_sync, dict(data))

    def create_experiment_sync(self, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        timestamp = now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                f"""SELECT * FROM planner_policy_experiments
                WHERE policy_key=? AND scope=? AND status IN ({",".join("?" for _ in ACTIVE_EXPERIMENT_STATUSES)})
                ORDER BY created_at DESC LIMIT 1""",
                (data["policy_key"], data["scope"], *sorted(ACTIVE_EXPERIMENT_STATUSES)),
            ).fetchone()
            if active is not None:
                connection.commit()
                raise ValueError("planner_policy_experiment_conflict")
            existing = connection.execute(
                "SELECT * FROM planner_policy_experiments WHERE policy_key=? AND scope=? AND experiment_fingerprint=?",
                (data["policy_key"], data["scope"], data["experiment_fingerprint"]),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return _row(existing) or {}, False
            experiment_id = uuid4().hex
            connection.execute(
                """INSERT INTO planner_policy_experiments(
                experiment_id,experiment_version,policy_key,scope,baseline_revision,control_value_json,
                variants_json,allocation_json,status,minimum_sample_size,observation_window_json,primary_metric,
                secondary_metrics_json,guardrails_json,experiment_fingerprint,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    experiment_id,
                    POLICY_EXPERIMENT_VERSION,
                    data["policy_key"],
                    data["scope"],
                    data["baseline_revision"],
                    _json(data["control_value"]),
                    _json(data["variants"]),
                    _json(data["allocation"]),
                    "draft",
                    data["minimum_sample_size"],
                    _json(data.get("observation_window") or {}),
                    data["primary_metric"],
                    _json(data.get("secondary_metrics") or []),
                    _json(data.get("guardrails") or {}),
                    data["experiment_fingerprint"],
                    timestamp,
                ),
            )
            for variant in data["variants"]:
                connection.execute(
                    """INSERT INTO planner_policy_experiment_variants(
                    experiment_id,variant_id,name,value_json,proposal_id,application_id,allocation_percentage,risk_level,status)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        experiment_id,
                        variant["variant_id"],
                        variant.get("name") or variant["variant_id"],
                        _json(variant["value"]),
                        variant.get("proposal_id"),
                        variant.get("application_id"),
                        int(data["allocation"][variant["variant_id"]]),
                        variant.get("risk_level") or "medium",
                        variant.get("status") or "active",
                    ),
                )
            row = connection.execute("SELECT * FROM planner_policy_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
            created = _row(row) or {}
            self._append_experiment_history(connection, created, "experiment_created", None, "draft", data.get("actor"))
            connection.commit()
            return created, True

    async def get_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_experiment_sync, experiment_id)

    def get_experiment_sync(self, experiment_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            experiment = _row(connection.execute("SELECT * FROM planner_policy_experiments WHERE experiment_id=?", (experiment_id,)).fetchone())
            if experiment is not None:
                experiment["variant_rows"] = [_row(row) or {} for row in connection.execute(
                    "SELECT * FROM planner_policy_experiment_variants WHERE experiment_id=? ORDER BY variant_id",
                    (experiment_id,),
                ).fetchall()]
            return experiment

    async def list_experiments(self, *, status: str | None = None, policy_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_experiments_sync, status, policy_key, limit)

    def list_experiments_sync(self, status: str | None, policy_key: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if policy_key:
            clauses.append("policy_key=?")
            params.append(policy_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as connection:
            return [_row(row) or {} for row in connection.execute(
                f"SELECT * FROM planner_policy_experiments {where} ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()]

    async def active_experiment(self, policy_key: str, scope: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.active_experiment_sync, policy_key, scope)

    def active_experiment_sync(self, policy_key: str, scope: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                f"""SELECT * FROM planner_policy_experiments
                WHERE policy_key=? AND scope=? AND status IN ({",".join("?" for _ in ACTIVE_EXPERIMENT_STATUSES)})
                ORDER BY created_at DESC LIMIT 1""",
                (policy_key, scope, *sorted(ACTIVE_EXPERIMENT_STATUSES)),
            ).fetchone()
            return _row(row)

    async def update_experiment(self, experiment_id: str, updates: dict[str, Any], event_type: str, *, actor: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self.update_experiment_sync, experiment_id, dict(updates), event_type, actor)

    def update_experiment_sync(self, experiment_id: str, updates: dict[str, Any], event_type: str, actor: str | None) -> dict[str, Any]:
        json_fields = {"control_value", "variants", "allocation", "observation_window", "secondary_metrics", "guardrails", "metrics"}
        encoded = {f"{key}_json" if key in json_fields else key: _json(value) if key in json_fields else value for key, value in updates.items()}
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _row(connection.execute("SELECT * FROM planner_policy_experiments WHERE experiment_id=?", (experiment_id,)).fetchone())
            if current is None:
                connection.commit()
                raise ValueError("planner_policy_experiment_not_found")
            from_status = current["status"]
            assignments = ",".join(f"{key}=?" for key in encoded)
            connection.execute(f"UPDATE planner_policy_experiments SET {assignments} WHERE experiment_id=?", (*encoded.values(), experiment_id))
            updated = _row(connection.execute("SELECT * FROM planner_policy_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()) or {}
            self._append_experiment_history(connection, updated, event_type, from_status, updated["status"], actor)
            connection.commit()
            return updated

    def _append_experiment_history(self, connection: sqlite3.Connection, experiment: dict[str, Any], event_type: str, from_status: str | None, to_status: str, actor: str | None) -> None:
        connection.execute(
            """INSERT INTO planner_policy_experiment_history(
            event_id,experiment_id,event_type,from_status,to_status,actor,policy_key,scope,result,winner_variant_id,timestamp)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                uuid4().hex,
                experiment["experiment_id"],
                event_type,
                from_status,
                to_status,
                actor,
                experiment["policy_key"],
                experiment["scope"],
                experiment.get("result"),
                experiment.get("winner_variant_id"),
                now(),
            ),
        )

    async def experiment_history(self, experiment_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.experiment_history_sync, experiment_id)

    def experiment_history_sync(self, experiment_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM planner_policy_experiment_history WHERE experiment_id=? ORDER BY timestamp",
                (experiment_id,),
            ).fetchall()]

    async def get_workflow_experiment_snapshot(self, workflow_id: str, policy_key: str, scope: str = "global_planner") -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_workflow_experiment_snapshot_sync, workflow_id, policy_key, scope)

    def get_workflow_experiment_snapshot_sync(self, workflow_id: str, policy_key: str, scope: str = "global_planner") -> dict[str, Any] | None:
        with self.connect() as connection:
            return _row(connection.execute(
                "SELECT * FROM planner_policy_experiment_snapshots WHERE workflow_id=? AND policy_key=? AND scope=?",
                (workflow_id, policy_key, scope),
            ).fetchone())

    async def create_workflow_experiment_snapshot(self, data: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.create_workflow_experiment_snapshot_sync, dict(data))

    def create_workflow_experiment_snapshot_sync(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = _row(connection.execute(
                "SELECT * FROM planner_policy_experiment_snapshots WHERE workflow_id=? AND policy_key=? AND scope=?",
                (data["workflow_id"], data["policy_key"], data["scope"]),
            ).fetchone())
            if existing is not None:
                connection.commit()
                return existing
            snapshot_id = uuid4().hex
            connection.execute(
                """INSERT INTO planner_policy_experiment_snapshots(
                snapshot_id,workflow_id,policy_key,scope,revision,experiment_id,variant_id,effective_value_json,assignment_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    data["workflow_id"],
                    data["policy_key"],
                    data["scope"],
                    data["revision"],
                    data.get("experiment_id"),
                    data["variant_id"],
                    _json(data["effective_value"]),
                    _json(data["assignment"]),
                    now(),
                ),
            )
            row = connection.execute("SELECT * FROM planner_policy_experiment_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            connection.commit()
            return _row(row) or {}
