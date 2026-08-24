from __future__ import annotations

import os


FAILURE_INJECTION_NODES = {
    "detect_intent",
    "analyze_requirement",
    "create_tasks",
    "inspect_workspace",
    "create_project",
    "detect_test_framework",
    "prepare_environment",
    "run_tests",
    "apply_fix",
}


def raise_if_failure_injected(node_name: str) -> None:
    configured = os.getenv("LANGGRAPH_FAIL_AFTER_NODE", "").strip()
    if not configured:
        return
    if configured not in FAILURE_INJECTION_NODES:
        allowed = ", ".join(sorted(FAILURE_INJECTION_NODES))
        raise ValueError(f"LANGGRAPH_FAIL_AFTER_NODE inválido. Valores permitidos: {allowed}")
    development = os.getenv("LANGGRAPH_DEVELOPMENT", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not development:
        raise RuntimeError("LANGGRAPH_FAIL_AFTER_NODE solo está permitido con LANGGRAPH_DEVELOPMENT=true.")
    if configured == node_name:
        raise RuntimeError(f"Fallo de desarrollo inyectado después de {node_name}.")
