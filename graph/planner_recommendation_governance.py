from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


REVIEW_STATUSES = {
    "recommendation_only",
    "under_review",
    "accepted",
    "rejected",
    "deferred",
    "superseded",
}


def recommendation_fingerprint(
    recommendation: Mapping[str, Any],
    *,
    analytics_version: str,
) -> str:
    payload = {
        "analytics_version": analytics_version,
        "policy": recommendation.get("policy"),
        "segment": recommendation.get("segment"),
        "direction": recommendation.get("direction"),
        "current_value": recommendation.get("current_value"),
        "suggested_value": recommendation.get("suggested_value"),
        "severity": recommendation.get("severity"),
        "confidence": recommendation.get("confidence"),
        "reason_codes": recommendation.get("reason_codes") or [],
        "evidence": recommendation.get("evidence") or {},
    }
    for optional_key in ("target", "type", "recommended_action", "created_from_version"):
        if optional_key in recommendation:
            payload[optional_key] = recommendation.get(optional_key)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def governance_recommendation(
    recommendation: Mapping[str, Any],
    *,
    analytics_version: str,
) -> dict[str, Any]:
    item = dict(recommendation)
    fingerprint = recommendation_fingerprint(item, analytics_version=analytics_version)
    item["analytics_version"] = analytics_version
    item["recommendation_fingerprint"] = fingerprint
    item["fingerprint"] = fingerprint
    return item
