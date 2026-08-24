from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from graph.state import SoftwareFactoryState


PlanningPolicyDecisionValue = Literal["allow", "require_approval"]
PlanningApprovalStatusValue = Literal["not_required", "awaiting_approval", "approved", "rejected"]

MEDIUM_SENSITIVE_AREAS = {
    "configuration",
    "database",
    "dependencies",
    "infrastructure",
    "runtime",
    "security",
}


@dataclass(frozen=True)
class PlanningApprovalDecision:
    decision: PlanningPolicyDecisionValue
    required: bool
    reason: str


def evaluate_planning_approval_policy(
    risk_level: str | None,
    risk_score: int | None,
    impact_areas: list[str],
    sensitive_tasks: list[dict[str, Any]],
) -> PlanningApprovalDecision:
    level = str(risk_level or "low").casefold()
    areas = {str(area).casefold() for area in impact_areas}
    has_high_sensitive_task = any(
        str(task.get("risk_level") or "").casefold() in {"high", "critical"}
        for task in sensitive_tasks
    )
    if level in {"high", "critical"}:
        return PlanningApprovalDecision(
            "require_approval",
            True,
            f"planning risk level is {level}",
        )
    if has_high_sensitive_task:
        return PlanningApprovalDecision(
            "require_approval",
            True,
            "planning contains high-risk sensitive tasks",
        )
    if level == "medium" and areas.intersection(MEDIUM_SENSITIVE_AREAS):
        return PlanningApprovalDecision(
            "require_approval",
            True,
            "medium planning risk touches sensitive impact areas",
        )
    return PlanningApprovalDecision(
        "allow",
        False,
        f"planning risk level {level} does not require additional approval",
    )


def planning_approval_fingerprint(state: SoftwareFactoryState) -> str:
    planning = state.get("planning_result") or {}
    payload = {
        "project_name": state.get("project_name") or state.get("created_project_name"),
        "risk_score": state.get("planning_risk_score"),
        "risk_level": state.get("planning_risk_level"),
        "impact_areas": sorted(str(area) for area in state.get("planning_impact_areas", [])),
        "sensitive_tasks": state.get("planning_sensitive_tasks", []),
        "analysis": planning.get("analysis") or state.get("requirement_analysis"),
        "acceptance_criteria": planning.get("acceptance_criteria") or state.get("acceptance_criteria", []),
        "tasks": planning.get("tasks") or state.get("implementation_tasks", []),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
