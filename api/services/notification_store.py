from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from api.dashboard_models import DashboardCountItem
from api.notification_models import (
    DeliveryListResponse, EscalationPolicy, NotificationChannel,
    NotificationDelivery, NotificationDeliveryAttempt, NotificationPolicy,
    NotificationSummaryResponse, QuietHoursSchedule,
)


NOTIFICATION_CONFIG_VERSION = "1.0"


class NotificationStoreReferenceError(ValueError):
    def __init__(self, channel_id: str) -> None:
        super().__init__(channel_id)
        self.channel_id = channel_id
JSON_FIELDS = {
    "notification_channels": {"configuration": "configuration_json"},
    "notification_policies": {
        "channel_ids": "channel_ids_json", "rule_codes": "rule_codes_json",
        "severities": "severities_json", "alert_events": "alert_events_json",
        "statuses": "statuses_json",
    },
    "quiet_hours_schedules": {
        "days_of_week": "days_of_week_json",
        "suppress_severities": "suppress_severities_json",
    },
    "escalation_policies": {"steps": "steps_json"},
}


NOTIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_channels (
 channel_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NULL,
 channel_type TEXT NOT NULL, enabled INTEGER NOT NULL,
 configuration_json TEXT NOT NULL, secret_reference TEXT NULL,
 timeout_seconds INTEGER NOT NULL, max_attempts INTEGER NOT NULL,
 initial_backoff_seconds INTEGER NOT NULL, max_backoff_seconds INTEGER NOT NULL,
 rate_limit_per_minute INTEGER NULL, verify_tls INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NULL
);
CREATE TABLE IF NOT EXISTS notification_policies (
 policy_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NULL,
 enabled INTEGER NOT NULL, channel_ids_json TEXT NOT NULL,
 rule_codes_json TEXT NOT NULL, severities_json TEXT NOT NULL,
 alert_events_json TEXT NOT NULL, statuses_json TEXT NOT NULL,
 branch_scope TEXT NOT NULL, project_name_pattern TEXT NULL,
 cooldown_seconds INTEGER NOT NULL, send_resolved INTEGER NOT NULL,
 send_acknowledged INTEGER NOT NULL, quiet_hours_id TEXT NULL,
 escalation_policy_id TEXT NULL, created_at TEXT NOT NULL, updated_at TEXT NULL
);
CREATE TABLE IF NOT EXISTS quiet_hours_schedules (
 quiet_hours_id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL,
 timezone TEXT NOT NULL, days_of_week_json TEXT NOT NULL,
 start_time TEXT NOT NULL, end_time TEXT NOT NULL,
 suppress_severities_json TEXT NOT NULL, allow_critical INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NULL
);
CREATE TABLE IF NOT EXISTS escalation_policies (
 escalation_policy_id TEXT PRIMARY KEY, name TEXT NOT NULL,
 description TEXT NULL, enabled INTEGER NOT NULL, steps_json TEXT NOT NULL,
 stop_on_acknowledge INTEGER NOT NULL, stop_on_resolve INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NULL
);
CREATE TABLE IF NOT EXISTS notification_deliveries (
 delivery_id TEXT PRIMARY KEY, alert_id TEXT NOT NULL,
 occurrence_id TEXT NULL, action_id TEXT NULL, policy_id TEXT NOT NULL,
 channel_id TEXT NOT NULL, alert_event TEXT NOT NULL,
 escalation_step INTEGER NULL, original_delivery_id TEXT NULL,
 fingerprint TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
 scheduled_at TEXT NOT NULL, first_attempt_at TEXT NULL,
 last_attempt_at TEXT NULL, delivered_at TEXT NULL,
 next_attempt_at TEXT NULL, attempt_count INTEGER NOT NULL,
 max_attempts INTEGER NOT NULL, response_status_code INTEGER NULL,
 response_summary TEXT NULL, last_error_code TEXT NULL,
 last_error_message TEXT NULL, payload_json TEXT NOT NULL,
 processing_owner TEXT NULL, processing_started_at TEXT NULL,
 processing_expires_at TEXT NULL, read_at TEXT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
 attempt_id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL,
 attempt_number INTEGER NOT NULL, started_at TEXT NOT NULL,
 completed_at TEXT NOT NULL, duration_ms INTEGER NOT NULL,
 result TEXT NOT NULL, request_method TEXT NULL, target_host TEXT NULL,
 response_status_code INTEGER NULL, response_summary TEXT NULL,
 error_code TEXT NULL, error_message TEXT NULL,
 FOREIGN KEY(delivery_id) REFERENCES notification_deliveries(delivery_id),
 UNIQUE(delivery_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS notification_channel_state (
 channel_id TEXT PRIMARY KEY, circuit_state TEXT NOT NULL DEFAULT 'closed',
 consecutive_failures INTEGER NOT NULL DEFAULT 0, opened_at TEXT NULL,
 half_open_claimed INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(channel_id) REFERENCES notification_channels(channel_id)
);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_status ON notification_deliveries(status);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_scheduled ON notification_deliveries(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_next_attempt ON notification_deliveries(next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_alert ON notification_deliveries(alert_id);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_channel ON notification_deliveries(channel_id);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_policy ON notification_deliveries(policy_id);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_created ON notification_deliveries(created_at);
CREATE INDEX IF NOT EXISTS idx_notification_attempt_delivery ON notification_delivery_attempts(delivery_id);
CREATE INDEX IF NOT EXISTS idx_notification_attempt_started ON notification_delivery_attempts(started_at);
CREATE INDEX IF NOT EXISTS idx_notification_channel_type ON notification_channels(channel_type);
CREATE INDEX IF NOT EXISTS idx_notification_channel_enabled ON notification_channels(enabled);
CREATE INDEX IF NOT EXISTS idx_notification_policy_enabled ON notification_policies(enabled);
"""


class NotificationStore:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.executescript(NOTIFICATION_SCHEMA)
            development = os.getenv("APP_ENV", "development").casefold() in {"development", "dev", "local", "test"}
            defaults = (
                ("builtin:internal", "Internal inbox", "Durable in-application notifications.", "internal", 1, "{}"),
                ("builtin:log", "Development log", "Sanitized notification log.", "log", int(development), "{}"),
            )
            for channel_id, name, description, channel_type, enabled, configuration in defaults:
                connection.execute("""INSERT OR IGNORE INTO notification_channels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    channel_id, name, description, channel_type, enabled, configuration,
                    None, 10, 3, 2, 300, None, 1, now, None,
                ))
                connection.execute("INSERT OR IGNORE INTO notification_channel_state(channel_id) VALUES(?)", (channel_id,))
            connection.execute("""INSERT OR IGNORE INTO notification_policies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                "builtin:internal", "Internal alert notifications", "Default durable inbox policy.", 1,
                json.dumps(["builtin:internal"]), json.dumps([]), json.dumps(["warning", "error", "critical"]),
                json.dumps(["alert_opened", "alert_reopened", "alert_severity_escalated", "alert_resolved", "alert_auto_resolved"]),
                json.dumps(["open", "acknowledged", "resolved"]), "all", None, 0, 1, 0, None, None, now, None,
            ))

    @staticmethod
    def delivery_fingerprint(
        alert_id: str, alert_event: str, policy_id: str, channel_id: str,
        occurrence_or_action: str | None, escalation_step: int | None,
        redelivery_token: str | None = None,
    ) -> str:
        raw = "|".join((alert_id, alert_event, policy_id, channel_id, occurrence_or_action or "", str(escalation_step or ""), redelivery_token or ""))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _sanitized_configuration(configuration: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in configuration.items():
            lowered = key.casefold()
            if lowered in {"authorization", "token", "password", "secret", "api_key"}:
                continue
            if lowered == "url" and isinstance(value, str):
                parsed = urlsplit(value)
                result[key] = f"{parsed.scheme}://{parsed.hostname or '[redacted]'}[:port]/[redacted]"
            elif lowered == "headers" and isinstance(value, dict):
                result[key] = {str(name): "[configured]" for name in value if str(name).casefold().startswith("x-")}
            else:
                result[key] = value
        return result

    def _channel(self, row: sqlite3.Row | None) -> NotificationChannel | None:
        if row is None:
            return None
        item = dict(row); configuration = json.loads(item.pop("configuration_json") or "{}")
        state = item.pop("circuit_state", "closed")
        reference = item.get("secret_reference")
        configured = bool(reference and reference.startswith("env:") and os.getenv(reference[4:]))
        if not item["enabled"]: health = "disabled"
        elif reference and not configured: health = "misconfigured"
        elif state == "open": health = "circuit_open"
        else: health = "healthy" if item["channel_type"] in {"internal", "log"} or configured or configuration.get("url") else "unknown"
        item.update(configuration=self._sanitized_configuration(configuration), secret_configured=configured, health=health)
        for key in ("enabled", "verify_tls"): item[key] = bool(item[key])
        return NotificationChannel(**item)

    def _raw_channel(self, row: sqlite3.Row | None) -> tuple[NotificationChannel, dict[str, Any]] | None:
        if row is None: return None
        model = self._channel(row)
        return model, json.loads(row["configuration_json"] or "{}")

    @staticmethod
    def _policy(row: sqlite3.Row | None) -> NotificationPolicy | None:
        if row is None: return None
        item = dict(row)
        for name, column in JSON_FIELDS["notification_policies"].items(): item[name] = json.loads(item.pop(column) or "[]")
        for key in ("enabled", "send_resolved", "send_acknowledged"): item[key] = bool(item[key])
        return NotificationPolicy(**item)

    @staticmethod
    def _quiet(row: sqlite3.Row | None) -> QuietHoursSchedule | None:
        if row is None: return None
        item = dict(row)
        for name, column in JSON_FIELDS["quiet_hours_schedules"].items(): item[name] = json.loads(item.pop(column) or "[]")
        for key in ("enabled", "allow_critical"): item[key] = bool(item[key])
        return QuietHoursSchedule(**item)

    @staticmethod
    def _escalation(row: sqlite3.Row | None) -> EscalationPolicy | None:
        if row is None: return None
        item = dict(row); item["steps"] = json.loads(item.pop("steps_json") or "[]")
        for key in ("enabled", "stop_on_acknowledge", "stop_on_resolve"): item[key] = bool(item[key])
        return EscalationPolicy(**item)

    @staticmethod
    def _delivery(row: sqlite3.Row | None) -> NotificationDelivery | None:
        if row is None: return None
        item = {key: value for key, value in dict(row).items() if key != "payload_json"}
        return NotificationDelivery(**item)

    @staticmethod
    def _attempt(row: sqlite3.Row) -> NotificationDeliveryAttempt:
        return NotificationDeliveryAttempt(**dict(row))

    async def list_channels(self) -> list[NotificationChannel]:
        return await asyncio.to_thread(self._list_channels_sync)

    def _list_channels_sync(self):
        with self._connect() as connection:
            rows = connection.execute("""SELECT c.*,COALESCE(s.circuit_state,'closed') circuit_state FROM notification_channels c LEFT JOIN notification_channel_state s USING(channel_id) ORDER BY c.name""").fetchall()
        return [self._channel(row) for row in rows]

    async def get_channel(self, channel_id: str, *, raw: bool = False):
        return await asyncio.to_thread(self._get_channel_sync, channel_id, raw)

    def _get_channel_sync(self, channel_id, raw):
        with self._connect() as connection:
            row = connection.execute("""SELECT c.*,COALESCE(s.circuit_state,'closed') circuit_state FROM notification_channels c LEFT JOIN notification_channel_state s USING(channel_id) WHERE c.channel_id=?""", (channel_id,)).fetchone()
        return self._raw_channel(row) if raw else self._channel(row)

    async def create_channel(self, values: dict[str, Any]) -> NotificationChannel:
        return await asyncio.to_thread(self._create_channel_sync, values)

    def _create_channel_sync(self, values):
        channel_id = str(uuid4()); now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("""INSERT INTO notification_channels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                channel_id, values["name"], values.get("description"), values["channel_type"], int(values.get("enabled", True)),
                json.dumps(values.get("configuration") or {}), values.get("secret_reference"), values.get("timeout_seconds", 10),
                values.get("max_attempts", 3), values.get("initial_backoff_seconds", 2), values.get("max_backoff_seconds", 300),
                values.get("rate_limit_per_minute"), int(values.get("verify_tls", True)), now, None,
            ))
            connection.execute("INSERT INTO notification_channel_state(channel_id) VALUES(?)", (channel_id,))
        return self._get_channel_sync(channel_id, False)

    async def update_channel(self, channel_id: str, values: dict[str, Any]) -> NotificationChannel | None:
        return await asyncio.to_thread(self._update_entity_sync, "notification_channels", "channel_id", channel_id, values, self._get_channel_sync)

    async def delete_channel(self, channel_id: str) -> str:
        return await asyncio.to_thread(self._delete_channel_sync, channel_id)

    def _delete_channel_sync(self, channel_id):
        with self._connect() as connection:
            policies = connection.execute("SELECT policy_id,channel_ids_json FROM notification_policies").fetchall()
            if any(channel_id in json.loads(row["channel_ids_json"]) for row in policies): return "referenced"
            row = connection.execute("SELECT 1 FROM notification_channels WHERE channel_id=?", (channel_id,)).fetchone()
            if row is None: return "missing"
            connection.execute("DELETE FROM notification_channel_state WHERE channel_id=?", (channel_id,))
            connection.execute("DELETE FROM notification_channels WHERE channel_id=?", (channel_id,))
        return "deleted"

    def _update_entity_sync(self, table, id_field, entity_id, values, getter):
        with self._connect() as connection:
            if connection.execute(f"SELECT 1 FROM {table} WHERE {id_field}=?", (entity_id,)).fetchone() is None: return None
            mapped = []
            for key, value in values.items():
                column = JSON_FIELDS.get(table, {}).get(key, key)
                if column.endswith("_json"): value = json.dumps(value)
                elif isinstance(value, bool): value = int(value)
                elif hasattr(value, "isoformat"): value = value.isoformat()
                mapped.append((column, value))
            mapped.append(("updated_at", datetime.now(UTC).isoformat()))
            connection.execute(f"UPDATE {table} SET {','.join(f'{key}=?' for key, _ in mapped)} WHERE {id_field}=?", (*[value for _, value in mapped], entity_id))
        return getter(entity_id, False) if table == "notification_channels" else getter(entity_id)

    async def list_policies(self) -> list[NotificationPolicy]:
        return await asyncio.to_thread(self._list_policies_sync)

    def _list_policies_sync(self):
        with self._connect() as connection: rows = connection.execute("SELECT * FROM notification_policies ORDER BY name").fetchall()
        return [self._policy(row) for row in rows]

    async def get_policy(self, policy_id: str): return await asyncio.to_thread(self._get_policy_sync, policy_id)
    def _get_policy_sync(self, policy_id):
        with self._connect() as connection: row = connection.execute("SELECT * FROM notification_policies WHERE policy_id=?", (policy_id,)).fetchone()
        return self._policy(row)

    async def create_policy(self, values): return await asyncio.to_thread(self._create_policy_sync, values)
    def _create_policy_sync(self, values):
        policy_id = str(uuid4()); now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("INSERT INTO notification_policies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                policy_id, values["name"], values.get("description"), int(values.get("enabled", True)),
                *[json.dumps(values.get(key) or []) for key in ("channel_ids", "rule_codes", "severities", "alert_events", "statuses")],
                values.get("branch_scope", "original"), values.get("project_name_pattern"), values.get("cooldown_seconds", 0),
                int(values.get("send_resolved", True)), int(values.get("send_acknowledged", False)),
                values.get("quiet_hours_id"), values.get("escalation_policy_id"), now, None,
            ))
        return self._get_policy_sync(policy_id)

    async def update_policy(self, entity_id, values): return await asyncio.to_thread(self._update_entity_sync, "notification_policies", "policy_id", entity_id, values, self._get_policy_sync)
    async def delete_policy(self, entity_id): return await asyncio.to_thread(self._delete_simple_sync, "notification_policies", "policy_id", entity_id)

    async def list_quiet_hours(self): return await asyncio.to_thread(self._list_simple_sync, "quiet_hours_schedules", self._quiet)
    async def get_quiet_hours(self, entity_id): return await asyncio.to_thread(self._get_quiet_sync, entity_id)
    def _get_quiet_sync(self, entity_id):
        with self._connect() as connection: row = connection.execute("SELECT * FROM quiet_hours_schedules WHERE quiet_hours_id=?", (entity_id,)).fetchone()
        return self._quiet(row)
    async def create_quiet_hours(self, values): return await asyncio.to_thread(self._create_quiet_sync, values)
    def _create_quiet_sync(self, values):
        entity_id=str(uuid4()); now=datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("INSERT INTO quiet_hours_schedules VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                entity_id, values["name"], int(values.get("enabled", True)), values["timezone"], json.dumps(values["days_of_week"]),
                values["start_time"].isoformat(), values["end_time"].isoformat(), json.dumps(values["suppress_severities"]), int(values.get("allow_critical", True)), now, None,
            ))
        return self._get_quiet_sync(entity_id)
    async def update_quiet_hours(self, entity_id, values): return await asyncio.to_thread(self._update_entity_sync, "quiet_hours_schedules", "quiet_hours_id", entity_id, values, self._get_quiet_sync)
    async def delete_quiet_hours(self, entity_id): return await asyncio.to_thread(self._delete_simple_sync, "quiet_hours_schedules", "quiet_hours_id", entity_id)

    async def list_escalations(self): return await asyncio.to_thread(self._list_simple_sync, "escalation_policies", self._escalation)
    async def get_escalation(self, entity_id): return await asyncio.to_thread(self._get_escalation_sync, entity_id)
    def _get_escalation_sync(self, entity_id):
        with self._connect() as connection: row=connection.execute("SELECT * FROM escalation_policies WHERE escalation_policy_id=?",(entity_id,)).fetchone()
        return self._escalation(row)
    @staticmethod
    def _escalation_steps_payload(values):
        return [item.model_dump(mode="json") if hasattr(item,"model_dump") else dict(item) for item in values["steps"]]

    @staticmethod
    def _validate_escalation_channels(connection, steps):
        channel_ids = {str(channel_id) for step in steps for channel_id in step["channel_ids"]}
        if not channel_ids:
            return
        marks = ",".join("?" for _ in channel_ids)
        existing = {
            row[0] for row in connection.execute(
                f"SELECT channel_id FROM notification_channels WHERE channel_id IN ({marks})",
                tuple(sorted(channel_ids)),
            )
        }
        missing = sorted(channel_ids - existing)
        if missing:
            raise NotificationStoreReferenceError(missing[0])

    async def create_escalation(self, values): return await asyncio.to_thread(self._create_escalation_sync, values)
    def _create_escalation_sync(self, values):
        entity_id=str(uuid4());now=datetime.now(UTC).isoformat()
        steps=self._escalation_steps_payload(values)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_escalation_channels(connection, steps)
            connection.execute("""INSERT INTO escalation_policies(
                escalation_policy_id,name,description,enabled,steps_json,
                stop_on_acknowledge,stop_on_resolve,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",(
                entity_id,values["name"],values.get("description"),int(values.get("enabled",True)),
                json.dumps(steps,ensure_ascii=False),int(values.get("stop_on_acknowledge",True)),
                int(values.get("stop_on_resolve",True)),now,None,
            ))
            row=connection.execute("SELECT * FROM escalation_policies WHERE escalation_policy_id=?",(entity_id,)).fetchone()
            result=self._escalation(row)
        return result
    async def update_escalation(self, entity_id, values):
        return await asyncio.to_thread(self._update_escalation_sync,entity_id,values)
    def _update_escalation_sync(self,entity_id,values):
        selected=dict(values)
        if "steps" in selected:selected["steps"]=self._escalation_steps_payload(selected)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM escalation_policies WHERE escalation_policy_id=?",(entity_id,)).fetchone() is None:return None
            if "steps" in selected:self._validate_escalation_channels(connection,selected["steps"])
            mapped=[]
            for key,value in selected.items():
                column="steps_json" if key=="steps" else key
                if key=="steps":value=json.dumps(value,ensure_ascii=False)
                elif isinstance(value,bool):value=int(value)
                mapped.append((column,value))
            mapped.append(("updated_at",datetime.now(UTC).isoformat()))
            connection.execute(f"UPDATE escalation_policies SET {','.join(f'{key}=?' for key,_ in mapped)} WHERE escalation_policy_id=?",(*[value for _,value in mapped],entity_id))
            row=connection.execute("SELECT * FROM escalation_policies WHERE escalation_policy_id=?",(entity_id,)).fetchone()
            result=self._escalation(row)
        return result
    async def delete_escalation(self, entity_id): return await asyncio.to_thread(self._delete_escalation_sync,entity_id)
    def _delete_escalation_sync(self,entity_id):
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM escalation_policies WHERE escalation_policy_id=?",(entity_id,)).fetchone() is None:return "missing"
            if connection.execute("SELECT 1 FROM notification_policies WHERE escalation_policy_id=? LIMIT 1",(entity_id,)).fetchone():return "referenced"
            connection.execute("DELETE FROM escalation_policies WHERE escalation_policy_id=?",(entity_id,))
        return "deleted"

    def _list_simple_sync(self, table, converter):
        with self._connect() as connection: rows=connection.execute(f"SELECT * FROM {table} ORDER BY name").fetchall()
        return [converter(row) for row in rows]
    def _delete_simple_sync(self, table, id_field, entity_id):
        with self._connect() as connection:
            row=connection.execute(f"SELECT 1 FROM {table} WHERE {id_field}=?",(entity_id,)).fetchone()
            if row is None:return False
            connection.execute(f"DELETE FROM {table} WHERE {id_field}=?",(entity_id,))
        return True

    async def create_delivery(self, **values) -> tuple[NotificationDelivery, bool]:
        return await asyncio.to_thread(self._create_delivery_sync, values)

    async def delivery_in_cooldown(self, alert_id: str, policy_id: str, channel_id: str, cooldown_seconds: int) -> bool:
        if cooldown_seconds <= 0:
            return False
        return await asyncio.to_thread(
            self._delivery_in_cooldown_sync, alert_id, policy_id, channel_id, cooldown_seconds,
        )

    def _delivery_in_cooldown_sync(self, alert_id, policy_id, channel_id, cooldown_seconds):
        cutoff = (datetime.now(UTC) - timedelta(seconds=cooldown_seconds)).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM notification_deliveries
                   WHERE alert_id=? AND policy_id=? AND channel_id=? AND created_at>=?
                     AND status NOT IN ('cancelled','suppressed') LIMIT 1""",
                (alert_id, policy_id, channel_id, cutoff),
            ).fetchone()
        return row is not None

    def _create_delivery_sync(self, values):
        delivery_id=values.get("delivery_id") or str(uuid4());now=datetime.now(UTC).isoformat()
        scheduled=values.get("scheduled_at") or now
        if hasattr(scheduled,"isoformat"):scheduled=scheduled.isoformat()
        fingerprint=values["fingerprint"]
        with self._connect() as connection:
            existing=connection.execute("SELECT * FROM notification_deliveries WHERE fingerprint=?",(fingerprint,)).fetchone()
            if existing:return self._delivery(existing),False
            connection.execute("""INSERT INTO notification_deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                delivery_id,values["alert_id"],values.get("occurrence_id"),values.get("action_id"),values["policy_id"],values["channel_id"],values["alert_event"],values.get("escalation_step"),values.get("original_delivery_id"),fingerprint,values.get("status","pending"),scheduled,None,None,None,None,0,values["max_attempts"],None,None,None,None,json.dumps(values["payload"],ensure_ascii=False),None,None,None,None,now,now,
            ))
            row=connection.execute("SELECT * FROM notification_deliveries WHERE delivery_id=?",(delivery_id,)).fetchone()
        return self._delivery(row),True

    async def get_delivery(self, delivery_id): return await asyncio.to_thread(self._get_delivery_sync,delivery_id)
    def _get_delivery_sync(self, delivery_id):
        with self._connect() as connection:row=connection.execute("SELECT * FROM notification_deliveries WHERE delivery_id=?",(delivery_id,)).fetchone()
        return self._delivery(row)
    async def delivery_payload(self, delivery_id): return await asyncio.to_thread(self._delivery_payload_sync,delivery_id)
    def _delivery_payload_sync(self,delivery_id):
        with self._connect() as connection:row=connection.execute("SELECT payload_json FROM notification_deliveries WHERE delivery_id=?",(delivery_id,)).fetchone()
        return json.loads(row[0]) if row else None
    async def alert_status(self, alert_id): return await asyncio.to_thread(self._alert_status_sync, alert_id)
    def _alert_status_sync(self, alert_id):
        with self._connect() as connection:
            row = connection.execute("SELECT status FROM workflow_alerts WHERE alert_id=?", (alert_id,)).fetchone()
        return row[0] if row else None
    async def attempts(self,delivery_id,limit=100,offset=0):return await asyncio.to_thread(self._attempts_sync,delivery_id,limit,offset)
    def _attempts_sync(self,delivery_id,limit,offset):
        with self._connect() as connection:rows=connection.execute("SELECT * FROM notification_delivery_attempts WHERE delivery_id=? ORDER BY attempt_number LIMIT ? OFFSET ?",(delivery_id,limit,offset)).fetchall()
        return [self._attempt(row) for row in rows]

    async def list_deliveries(self, **filters) -> DeliveryListResponse:
        return await asyncio.to_thread(self._list_deliveries_sync,filters)
    def _list_deliveries_sync(self,filters):
        clauses=[];params=[]
        for field in ("status","channel_id","policy_id","alert_id","alert_event"):
            if filters.get(field):clauses.append(f"{field}=?");params.append(filters[field])
        if filters.get("date_from"):clauses.append("created_at>=?");params.append(filters["date_from"])
        if filters.get("date_to"):clauses.append("created_at<=?");params.append(filters["date_to"])
        if filters.get("search"):clauses.append("(alert_id LIKE ? OR last_error_message LIKE ? OR response_summary LIKE ?)");params.extend([f"%{filters['search']}%"]*3)
        where=" WHERE "+" AND ".join(clauses) if clauses else ""
        sort=filters.get("sort_by","created_at");order=filters.get("sort_order","desc").upper();limit=filters.get("limit",50);offset=filters.get("offset",0)
        with self._connect() as connection:
            total=connection.execute(f"SELECT COUNT(*) FROM notification_deliveries{where}",params).fetchone()[0]
            rows=connection.execute(f"SELECT * FROM notification_deliveries{where} ORDER BY {sort} {order} LIMIT ? OFFSET ?",(*params,limit,offset)).fetchall()
        return DeliveryListResponse(items=[self._delivery(row) for row in rows],total=total,limit=limit,offset=offset,has_more=offset+len(rows)<total)

    async def claim(self,owner,batch_size,lease_seconds):return await asyncio.to_thread(self._claim_sync,owner,batch_size,lease_seconds)
    def _claim_sync(self,owner,batch_size,lease_seconds):
        now=datetime.now(UTC);now_text=now.isoformat();expires=(now+timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows=connection.execute("""SELECT delivery_id FROM notification_deliveries WHERE ((status IN ('pending','retry_scheduled') AND COALESCE(next_attempt_at,scheduled_at)<=?) OR (status='processing' AND processing_expires_at<=?)) ORDER BY COALESCE(next_attempt_at,scheduled_at),created_at LIMIT ?""",(now_text,now_text,batch_size)).fetchall()
            ids=[]
            for row in rows:
                changed=connection.execute("""UPDATE notification_deliveries SET status='processing',processing_owner=?,processing_started_at=?,processing_expires_at=?,updated_at=? WHERE delivery_id=? AND (status IN ('pending','retry_scheduled') OR (status='processing' AND processing_expires_at<=?))""",(owner,now_text,expires,now_text,row["delivery_id"],now_text)).rowcount
                if changed:ids.append(row["delivery_id"])
            result=[self._delivery(connection.execute("SELECT * FROM notification_deliveries WHERE delivery_id=?",(item,)).fetchone()) for item in ids]
        return result

    async def defer(self,delivery_id,owner,when,code):return await asyncio.to_thread(self._defer_sync,delivery_id,owner,when,code)
    def _defer_sync(self,delivery_id,owner,when,code):
        when_text=when.isoformat() if hasattr(when,"isoformat") else when;now=datetime.now(UTC).isoformat()
        with self._connect() as connection:return bool(connection.execute("""UPDATE notification_deliveries SET status='retry_scheduled',next_attempt_at=?,last_error_code=?,last_error_message=?,processing_owner=NULL,processing_started_at=NULL,processing_expires_at=NULL,updated_at=? WHERE delivery_id=? AND status='processing' AND processing_owner=?""",(when_text,code,code.replace("_"," "),now,delivery_id,owner)).rowcount)

    async def suppress(self,delivery_id,owner,reason):return await asyncio.to_thread(self._suppress_sync,delivery_id,owner,reason)
    def _suppress_sync(self,delivery_id,owner,reason):
        now=datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row=connection.execute("SELECT attempt_count FROM notification_deliveries WHERE delivery_id=? AND processing_owner=?",(delivery_id,owner)).fetchone()
            if not row:return False
            connection.execute("UPDATE notification_deliveries SET status='suppressed',last_error_code='quiet_hours',last_error_message=?,processing_owner=NULL,processing_started_at=NULL,processing_expires_at=NULL,updated_at=? WHERE delivery_id=?",(reason,now,delivery_id))
            connection.execute("INSERT INTO notification_delivery_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(str(uuid4()),delivery_id,row[0]+1,now,now,0,"suppressed",None,None,None,None,"quiet_hours",reason))
        return True

    async def complete_attempt(self,delivery_id,owner,result,started_at,duration_ms):return await asyncio.to_thread(self._complete_attempt_sync,delivery_id,owner,result,started_at,duration_ms)
    def _complete_attempt_sync(self,delivery_id,owner,result,started_at,duration_ms):
        now=datetime.now(UTC);now_text=now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row=connection.execute("SELECT * FROM notification_deliveries WHERE delivery_id=? AND status='processing' AND processing_owner=?",(delivery_id,owner)).fetchone()
            if not row:return None
            attempt=row["attempt_count"]+1
            final="delivered" if result.success else "retry_scheduled" if result.retryable and attempt<row["max_attempts"] else "dead_letter"
            next_attempt=None
            if final=="retry_scheduled":
                channel=connection.execute("SELECT initial_backoff_seconds,max_backoff_seconds FROM notification_channels WHERE channel_id=?",(row["channel_id"],)).fetchone()
                delay=min(channel["max_backoff_seconds"],channel["initial_backoff_seconds"]*(2**(attempt-1)))
                jitter=int(hashlib.sha256(f"{delivery_id}:{attempt}".encode()).hexdigest()[:4],16)/0xFFFF*.2-.1
                delay=max(1,round(delay*(1+jitter)))
                if result.retry_after_seconds is not None:delay=max(delay,result.retry_after_seconds)
                next_attempt=(now+timedelta(seconds=delay)).isoformat()
            connection.execute("""UPDATE notification_deliveries SET status=?,first_attempt_at=COALESCE(first_attempt_at,?),last_attempt_at=?,delivered_at=?,next_attempt_at=?,attempt_count=?,response_status_code=?,response_summary=?,last_error_code=?,last_error_message=?,processing_owner=NULL,processing_started_at=NULL,processing_expires_at=NULL,updated_at=? WHERE delivery_id=?""",(final,started_at.isoformat(),now_text,now_text if final=="delivered" else None,next_attempt,attempt,result.status_code,result.response_summary,result.error_code,result.error_message,now_text,delivery_id))
            attempt_result="success" if result.success else "retryable_failure" if result.retryable else "permanent_failure"
            connection.execute("INSERT INTO notification_delivery_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(str(uuid4()),delivery_id,attempt,started_at.isoformat(),now_text,duration_ms,attempt_result,result.request_method,result.target_host,result.status_code,result.response_summary,result.error_code,result.error_message))
            updated=connection.execute("SELECT * FROM notification_deliveries WHERE delivery_id=?",(delivery_id,)).fetchone()
        return self._delivery(updated)

    async def rate_limited_until(self,channel_id,limit):return await asyncio.to_thread(self._rate_limited_until_sync,channel_id,limit)
    def _rate_limited_until_sync(self,channel_id,limit):
        if not limit:return None
        cutoff=datetime.now(UTC)-timedelta(minutes=1)
        with self._connect() as connection:rows=connection.execute("""SELECT a.started_at FROM notification_delivery_attempts a JOIN notification_deliveries d USING(delivery_id) WHERE d.channel_id=? AND a.started_at>=? AND a.result!='suppressed' ORDER BY a.started_at""",(channel_id,cutoff.isoformat())).fetchall()
        return datetime.fromisoformat(rows[0][0])+timedelta(minutes=1) if len(rows)>=limit else None

    async def circuit_permission(self,channel_id,threshold,cooldown):return await asyncio.to_thread(self._circuit_permission_sync,channel_id,threshold,cooldown)
    def _circuit_permission_sync(self,channel_id,threshold,cooldown):
        now=datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE");row=connection.execute("SELECT * FROM notification_channel_state WHERE channel_id=?",(channel_id,)).fetchone()
            if row is None:return "closed"
            if row["circuit_state"]=="open":
                opened=datetime.fromisoformat(row["opened_at"])
                if (now-opened).total_seconds()<cooldown:return "open"
                if row["half_open_claimed"]:return "open"
                connection.execute("UPDATE notification_channel_state SET circuit_state='half_open',half_open_claimed=1 WHERE channel_id=?",(channel_id,));return "half_open"
            if row["circuit_state"]=="half_open" and row["half_open_claimed"]:return "open"
            return "closed"
    async def record_circuit(self,channel_id,success,threshold):await asyncio.to_thread(self._record_circuit_sync,channel_id,success,threshold)
    def _record_circuit_sync(self,channel_id,success,threshold):
        with self._connect() as connection:
            row=connection.execute("SELECT consecutive_failures FROM notification_channel_state WHERE channel_id=?",(channel_id,)).fetchone();failures=0 if success else (row[0] if row else 0)+1
            state="closed" if success or failures<threshold else "open"
            connection.execute("""INSERT INTO notification_channel_state(channel_id,circuit_state,consecutive_failures,opened_at,half_open_claimed) VALUES(?,?,?,?,0) ON CONFLICT(channel_id) DO UPDATE SET circuit_state=excluded.circuit_state,consecutive_failures=excluded.consecutive_failures,opened_at=excluded.opened_at,half_open_claimed=0""",(channel_id,state,failures,datetime.now(UTC).isoformat() if state=="open" else None))

    async def transition_delivery(self,delivery_id,action):return await asyncio.to_thread(self._transition_delivery_sync,delivery_id,action)
    def _transition_delivery_sync(self,delivery_id,action):
        allowed={"retry":{"failed","dead_letter","cancelled"},"cancel":{"pending","retry_scheduled"}}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE");row=connection.execute("SELECT * FROM notification_deliveries WHERE delivery_id=?",(delivery_id,)).fetchone()
            if row is None:return None,"missing"
            if row["status"] not in allowed[action]:return self._delivery(row),"conflict"
            target="retry_scheduled" if action=="retry" else "cancelled";now=datetime.now(UTC).isoformat()
            connection.execute("UPDATE notification_deliveries SET status=?,next_attempt_at=?,last_error_code=NULL,last_error_message=NULL,updated_at=? WHERE delivery_id=? AND status=?",(target,now if action=="retry" else None,now,delivery_id,row["status"]))
            updated=connection.execute("SELECT * FROM notification_deliveries WHERE delivery_id=?",(delivery_id,)).fetchone()
        return self._delivery(updated),"ok"

    async def cancel_escalations(self,alert_id,policy_ids):return await asyncio.to_thread(self._cancel_escalations_sync,alert_id,policy_ids)
    def _cancel_escalations_sync(self,alert_id,policy_ids):
        if not policy_ids:return 0
        now=datetime.now(UTC).isoformat();marks=','.join('?' for _ in policy_ids)
        with self._connect() as connection:return connection.execute(f"UPDATE notification_deliveries SET status='cancelled',updated_at=? WHERE alert_id=? AND policy_id IN ({marks}) AND alert_event='escalation_triggered' AND status IN ('pending','retry_scheduled')",(now,alert_id,*policy_ids)).rowcount

    async def mark_read(self,delivery_id):return await asyncio.to_thread(self._mark_read_sync,delivery_id)
    def _mark_read_sync(self,delivery_id):
        now=datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row=connection.execute("SELECT d.* FROM notification_deliveries d JOIN notification_channels c USING(channel_id) WHERE d.delivery_id=? AND c.channel_type='internal'",(delivery_id,)).fetchone()
            if row is None:return None
            connection.execute("UPDATE notification_deliveries SET read_at=COALESCE(read_at,?),updated_at=? WHERE delivery_id=?",(now,now,delivery_id));row=connection.execute("SELECT * FROM notification_deliveries WHERE delivery_id=?",(delivery_id,)).fetchone()
        return self._delivery(row)
    async def mark_all_read(self):return await asyncio.to_thread(self._mark_all_read_sync)
    def _mark_all_read_sync(self):
        now=datetime.now(UTC).isoformat()
        with self._connect() as connection:return connection.execute("""UPDATE notification_deliveries SET read_at=COALESCE(read_at,?),updated_at=? WHERE channel_id IN (SELECT channel_id FROM notification_channels WHERE channel_type='internal') AND status='delivered' AND read_at IS NULL""",(now,now)).rowcount

    async def inbox_deliveries(self, limit=50, offset=0):
        return await asyncio.to_thread(self._inbox_deliveries_sync, limit, offset)

    def _inbox_deliveries_sync(self, limit, offset):
        with self._connect() as connection:
            total = connection.execute("""SELECT COUNT(*) FROM notification_deliveries d
                JOIN notification_channels c USING(channel_id) WHERE c.channel_type='internal'""").fetchone()[0]
            rows = connection.execute("""SELECT d.* FROM notification_deliveries d
                JOIN notification_channels c USING(channel_id) WHERE c.channel_type='internal'
                ORDER BY d.created_at DESC LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
        return DeliveryListResponse(
            items=[self._delivery(row) for row in rows], total=total, limit=limit, offset=offset,
            has_more=offset + len(rows) < total,
        )

    async def summary(self):return await asyncio.to_thread(self._summary_sync)
    def _summary_sync(self):
        now=datetime.now(UTC);cutoff=(now-timedelta(days=1)).isoformat()
        with self._connect() as connection:
            status=Counter({row[0]:row[1] for row in connection.execute("SELECT status,COUNT(*) FROM notification_deliveries GROUP BY status")})
            latencies=[row[0] for row in connection.execute("SELECT (julianday(delivered_at)-julianday(created_at))*86400 FROM notification_deliveries WHERE status='delivered' AND delivered_at IS NOT NULL")]
            attempts=connection.execute("SELECT COUNT(*),SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) FROM notification_delivery_attempts WHERE result!='suppressed'").fetchone()
            channels=connection.execute("SELECT COUNT(*),SUM(enabled) FROM notification_channels").fetchone()
            failures=connection.execute("""SELECT d.channel_id,c.name,COUNT(*) count FROM notification_deliveries d LEFT JOIN notification_channels c USING(channel_id) WHERE d.status='dead_letter' GROUP BY d.channel_id ORDER BY count DESC LIMIT 5""").fetchall()
            last24=connection.execute("SELECT COUNT(*),SUM(CASE WHEN status='dead_letter' THEN 1 ELSE 0 END) FROM notification_deliveries WHERE created_at>=?",(cutoff,)).fetchone()
            unread=connection.execute("SELECT COUNT(*) FROM notification_deliveries d JOIN notification_channels c USING(channel_id) WHERE c.channel_type='internal' AND d.status='delivered' AND d.read_at IS NULL").fetchone()[0]
        values=sorted(float(x) for x in latencies);p95=values[max(0,round((len(values)-1)*.95))] if values else None
        return NotificationSummaryResponse(
            **{key:status[key] for key in ("pending","processing","delivered","retry_scheduled","failed","dead_letter","suppressed","cancelled")},
            delivery_success_rate_percent=round((attempts[1] or 0)*100/attempts[0],2) if attempts[0] else None,
            average_delivery_latency_seconds=sum(values)/len(values) if values else None,p95_delivery_latency_seconds=p95,
            channels_enabled=channels[1] or 0,channels_misconfigured=sum(item.health=="misconfigured" for item in self._list_channels_sync()),
            deliveries_last_24h=last24[0],dead_letters_last_24h=last24[1] or 0,unread_internal=unread,
            top_failing_channels=[DashboardCountItem(key=row[0],label=row[1] or row[0],count=row[2]) for row in failures],calculated_at=now,
        )
