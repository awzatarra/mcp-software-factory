from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import ipaddress
import json
import logging
import os
import re
import socket
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from api.notification_models import (
    NotificationChannel, NotificationMessage, NotificationSendResult,
)


logger = logging.getLogger("software_factory.notifications")
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "cookie", "authorization"}
SECRET_PATTERN = re.compile(r"(?i)(sk-[a-z0-9_-]{12,}|bearer\s+\S+|(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+)")
URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
ABSOLUTE_PATH = re.compile(r"(?i)(?:[a-z]:[\\/][^\s,;\"']+|/(?:home|users|tmp|var|opt|workspace|mnt)/[^\s,;\"']+)")


class SecretResolutionError(RuntimeError):
    pass


class InvalidNotificationTarget(ValueError):
    pass


class SecretResolver:
    def resolve(self, reference: str) -> str:
        if not reference.startswith("env:") or not reference[4:]:
            raise SecretResolutionError("invalid_secret_reference")
        value = os.getenv(reference[4:])
        if not value:
            raise SecretResolutionError("secret_missing")
        return value


def sanitize_delivery_error(value: Any, limit: int = 500) -> str:
    text = "".join(character for character in str(value or "") if character.isprintable() or character in "\n\t")
    text = URL_PATTERN.sub("[target]", text)
    text = ABSOLUTE_PATH.sub("[path]", text)
    return SECRET_PATTERN.sub("[redacted]", text)[:limit]


def sanitize_notification_data(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "[truncated]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:100]:
            normalized = str(key).casefold()
            if normalized in {"prompt", "chain_of_thought", "content", "stdout", "stderr", "authorization", "token", "password", "secret", "api_key"}:
                continue
            result[str(key)[:100]] = sanitize_notification_data(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [sanitize_notification_data(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return sanitize_delivery_error(value, 1000)
    return value if isinstance(value, (int, float, bool)) or value is None else sanitize_delivery_error(value)


class TargetValidator:
    def __init__(self, *, allow_private: bool | None = None) -> None:
        development = os.getenv("APP_ENV", "development").casefold() in {"development", "dev", "local", "test"}
        configured = os.getenv("NOTIFICATION_ALLOW_PRIVATE_TARGETS", "false").casefold() == "true"
        self.allow_private = (configured and development) if allow_private is None else allow_private

    async def validate(self, url: str) -> str:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise InvalidNotificationTarget("invalid_target") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise InvalidNotificationTarget("invalid_target")
        host = parsed.hostname.casefold()
        if host in {"localhost", "localhost.localdomain", "metadata.google.internal"} and not self.allow_private:
            raise InvalidNotificationTarget("invalid_target")
        try:
            addresses = await asyncio.to_thread(socket.getaddrinfo, host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise InvalidNotificationTarget("dns_error") from exc
        for address in {item[4][0] for item in addresses}:
            ip = ipaddress.ip_address(address)
            blocked = ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            if blocked and not self.allow_private:
                raise InvalidNotificationTarget("invalid_target")
            if str(ip) == "169.254.169.254" and not self.allow_private:
                raise InvalidNotificationTarget("invalid_target")
        return host


class NotificationAdapter(Protocol):
    async def send(self, *, channel: NotificationChannel, message: NotificationMessage, idempotency_key: str) -> NotificationSendResult: ...


class InternalNotificationAdapter:
    async def send(self, *, channel, message, idempotency_key):
        return NotificationSendResult(success=True, retryable=False, response_summary="Stored in internal inbox")


class LogNotificationAdapter:
    async def send(self, *, channel, message, idempotency_key):
        logger.info("notification event=%s alert=%s severity=%s project=%s", message.alert_event, message.alert_id, message.severity, sanitize_delivery_error(message.project_name, 120))
        return NotificationSendResult(success=True, retryable=False, response_summary="Logged")


class DisabledEmailAdapter:
    async def send(self, *, channel, message, idempotency_key):
        return NotificationSendResult(success=False, retryable=False, error_code="channel_disabled", error_message="Email transport is not configured")


class GenericWebhookAdapter:
    def __init__(self, secret_resolver: SecretResolver | None = None, target_validator: TargetValidator | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.secret_resolver = secret_resolver or SecretResolver()
        self.target_validator = target_validator or TargetValidator()
        self.transport = transport

    def payload(self, message: NotificationMessage) -> dict[str, Any]:
        return {"event": message.alert_event, "notification": message.model_dump(mode="json"), "sent_at": datetime.now(UTC).isoformat()}

    async def send(self, *, channel, message, idempotency_key):
        configuration = getattr(channel, "_raw_configuration", None) or channel.configuration
        url = configuration.get("url") if isinstance(configuration, dict) else None
        authorization = None
        try:
            if channel.secret_reference:
                secret = self.secret_resolver.resolve(channel.secret_reference)
                if secret.startswith(("http://", "https://")):
                    url = secret
                else:
                    authorization = f"Bearer {secret}"
            if not isinstance(url, str):
                raise SecretResolutionError("secret_missing")
            host = await self.target_validator.validate(url)
        except SecretResolutionError as exc:
            return NotificationSendResult(success=False, retryable=False, error_code=str(exc), error_message="Channel secret is not configured")
        except InvalidNotificationTarget as exc:
            code = str(exc) if str(exc) in {"invalid_target", "dns_error"} else "invalid_target"
            return NotificationSendResult(success=False, retryable=code == "dns_error", error_code=code, error_message="Notification target is not allowed")

        payload = sanitize_notification_data(self.payload(message))
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_PAYLOAD_BYTES:
            return NotificationSendResult(success=False, retryable=False, error_code="payload_too_large", error_message="Notification payload exceeds the size limit", target_host=host, request_method="POST")
        headers = {
            "Content-Type": "application/json", "X-Notification-Id": message.notification_id,
            "X-Idempotency-Key": idempotency_key, "User-Agent": "mcp-software-factory/notifications",
        }
        configured_headers = configuration.get("headers", {}) if isinstance(configuration, dict) else {}
        for name, value in configured_headers.items():
            normalized = str(name).casefold()
            if normalized.startswith("x-") and normalized not in FORBIDDEN_HEADERS:
                headers[str(name)[:100]] = sanitize_delivery_error(value, 500)
        if authorization:
            headers["Authorization"] = authorization
        try:
            async with httpx.AsyncClient(
                timeout=channel.timeout_seconds, verify=channel.verify_tls,
                follow_redirects=False, cookies=None, transport=self.transport,
            ) as client:
                response = await client.post(url, content=body, headers=headers)
            summary = sanitize_delivery_error(response.text[:MAX_RESPONSE_BYTES], 500)
            retry_after = None
            if response.status_code in {429, 503}:
                try: retry_after = max(int(response.headers.get("Retry-After", "0")), 0)
                except ValueError: retry_after = None
            if 200 <= response.status_code < 300:
                return NotificationSendResult(success=True, retryable=False, status_code=response.status_code, response_summary=summary or "Delivered", request_method="POST", target_host=host)
            retryable = response.status_code in {408, 425, 429} or response.status_code >= 500 or (response.status_code == 409 and bool(configuration.get("retry_conflict")))
            code = "rate_limited" if response.status_code == 429 else "http_5xx" if response.status_code >= 500 else "http_4xx"
            return NotificationSendResult(success=False, retryable=retryable, status_code=response.status_code, response_summary=summary, error_code=code, error_message=f"Remote endpoint returned HTTP {response.status_code}", retry_after_seconds=retry_after, request_method="POST", target_host=host)
        except httpx.TimeoutException:
            return NotificationSendResult(success=False, retryable=True, error_code="timeout", error_message="Notification request timed out", request_method="POST", target_host=host)
        except httpx.ConnectError as exc:
            text = str(exc).casefold(); code = "dns_error" if "name" in text or "dns" in text else "tls_error" if "ssl" in text or "certificate" in text else "connection_error"
            return NotificationSendResult(success=False, retryable=True, error_code=code, error_message="Notification connection failed", request_method="POST", target_host=host)
        except httpx.HTTPError:
            return NotificationSendResult(success=False, retryable=True, error_code="connection_error", error_message="Notification request failed", request_method="POST", target_host=host)


class SlackWebhookAdapter(GenericWebhookAdapter):
    def payload(self, message):
        text = f"[{message.severity.upper()}] {sanitize_delivery_error(message.title, 160)} · {sanitize_delivery_error(message.project_name or message.thread_id, 120)}"
        return {"text": text, "blocks": [{"type": "section", "text": {"type": "plain_text", "text": text[:2900]}}]}


class TeamsWebhookAdapter(GenericWebhookAdapter):
    def payload(self, message):
        title = f"[{message.severity.upper()}] {sanitize_delivery_error(message.title, 160)}"
        return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {"type": "AdaptiveCard", "version": "1.4", "body": [{"type": "TextBlock", "weight": "Bolder", "text": title}, {"type": "TextBlock", "wrap": True, "text": sanitize_delivery_error(message.message, 1000)}]}}]}


def default_adapters() -> dict[str, NotificationAdapter]:
    webhook = GenericWebhookAdapter()
    return {
        "internal": InternalNotificationAdapter(), "log": LogNotificationAdapter(),
        "webhook": webhook, "slack_webhook": SlackWebhookAdapter(),
        "teams_webhook": TeamsWebhookAdapter(), "email": DisabledEmailAdapter(),
    }
