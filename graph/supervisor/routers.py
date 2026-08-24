from __future__ import annotations

from typing import Literal

from graph.state import SoftwareFactoryState
from graph.supervisor.models import SupervisorTarget


SupervisorRoute = Literal[
    "supervisor_loop_probe",
    "planning",
    "inspect_workspace",
    "implementation",
    "testing_repair",
    "git_workflow", "ci_pipeline", "git_promotion",
    "finalize",
]


def route_after_supervisor(state: SoftwareFactoryState) -> SupervisorRoute:
    value = state.get("supervisor_decision")
    if value == "supervisor_loop_probe" and state.get("supervisor_stagnant_loop_active"):
        return "supervisor_loop_probe"
    try:
        return SupervisorTarget(str(value)).value  # type: ignore[return-value]
    except ValueError:
        return SupervisorTarget.FINALIZE.value
