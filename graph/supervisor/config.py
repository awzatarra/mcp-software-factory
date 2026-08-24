from __future__ import annotations

import os
from dataclasses import dataclass


class SupervisorDevelopmentConfigError(ValueError):
    pass


def _env_flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SupervisorDevelopmentConfig:
    development: bool = False
    force_model_decision: bool = False
    force_timeout: bool = False
    force_invalid_target: bool = False
    force_target: str | None = None
    force_stagnant_loop: bool = False

    @classmethod
    def from_env(cls) -> "SupervisorDevelopmentConfig":
        development = _env_flag("LANGGRAPH_DEVELOPMENT")
        if not development:
            return cls()
        forced_target = os.getenv("SUPERVISOR_FORCE_TARGET", "").strip() or None
        config = cls(
            development=True,
            force_model_decision=_env_flag("SUPERVISOR_FORCE_MODEL_DECISION"),
            force_timeout=_env_flag("SUPERVISOR_FORCE_TIMEOUT"),
            force_invalid_target=_env_flag("SUPERVISOR_FORCE_INVALID_TARGET"),
            force_target=forced_target,
            force_stagnant_loop=_env_flag("SUPERVISOR_FORCE_STAGNANT_LOOP"),
        )
        active_hooks = [
            config.force_timeout,
            config.force_invalid_target,
            config.force_target is not None,
            config.force_stagnant_loop,
        ]
        if sum(bool(active) for active in active_hooks) > 1:
            raise SupervisorDevelopmentConfigError(
                "supervisor_development_config_conflict"
            )
        return config
