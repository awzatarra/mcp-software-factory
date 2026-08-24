from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from graph.planner_calibration import PlannerPolicySnapshot


POLICY_REGISTRY_VERSION = "7.11-v1"


class PlannerPolicyRegistryError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PlannerPolicyMetadata:
    policy_key: str
    value_type: str
    allowed_min: float | None
    allowed_max: float | None
    scope_supported: bool
    risk_classification: str
    rollback_supported: bool
    verification_strategy: str
    default_getter: Callable[[PlannerPolicySnapshot], Any]


POLICY_METADATA: dict[str, PlannerPolicyMetadata] = {
    "planning.quality_gate.weak_threshold": PlannerPolicyMetadata(
        policy_key="planning.quality_gate.weak_threshold",
        value_type="score",
        allowed_min=0,
        allowed_max=100,
        scope_supported=False,
        risk_classification="medium",
        rollback_supported=True,
        verification_strategy="read_back_and_boundary_check",
        default_getter=lambda snapshot: snapshot.quality_gate_weak_threshold,
    ),
    "planning.confidence.adjustment": PlannerPolicyMetadata(
        policy_key="planning.confidence.adjustment",
        value_type="ratio",
        allowed_min=0,
        allowed_max=1,
        scope_supported=False,
        risk_classification="medium",
        rollback_supported=True,
        verification_strategy="read_back",
        default_getter=lambda snapshot: snapshot.confidence_low_threshold,
    ),
}


def metadata_for(policy_key: str) -> PlannerPolicyMetadata:
    try:
        return POLICY_METADATA[policy_key]
    except KeyError as exc:
        raise PlannerPolicyRegistryError("planner_policy_application_not_applicable") from exc


def validate_policy_value(policy_key: str, value: Any) -> None:
    metadata = metadata_for(policy_key)
    if metadata.value_type in {"score", "ratio"}:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PlannerPolicyRegistryError("planner_policy_application_invalid_value")
        number = float(value)
        if metadata.allowed_min is not None and number < metadata.allowed_min:
            raise PlannerPolicyRegistryError("planner_policy_application_invalid_value")
        if metadata.allowed_max is not None and number > metadata.allowed_max:
            raise PlannerPolicyRegistryError("planner_policy_application_invalid_value")


def semantic_verify(policy_key: str, value: Any) -> bool:
    metadata = metadata_for(policy_key)
    if metadata.verification_strategy == "read_back_and_boundary_check":
        threshold = float(value)
        decisions = [score < threshold for score in (threshold - 1, threshold, threshold + 1)]
        return decisions == [True, False, False]
    return True
