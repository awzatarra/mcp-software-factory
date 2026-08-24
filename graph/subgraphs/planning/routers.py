from __future__ import annotations

from typing import Literal

from graph.subgraphs.planning.state import PlanningState


def route_after_plan_validation(state: PlanningState) -> Literal["valid", "refine", "failed"]:
    if state.get("planning_valid"):
        return "valid"
    if state.get("planning_attempts", 0) < state.get("max_planning_attempts", 2):
        return "refine"
    return "failed"
