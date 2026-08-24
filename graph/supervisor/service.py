from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI
from pydantic import ValidationError

from graph.state import SoftwareFactoryState
from graph.supervisor.adapters import from_supervisor_output, to_supervisor_input
from graph.supervisor.config import SupervisorDevelopmentConfig
from graph.supervisor.guards import (
    approval_blocks_supervisor,
    detect_supervisor_loop,
    determine_allowed_handoffs,
    resolve_mandatory_handoff,
    safe_fallback_target,
    supervisor_progress_fingerprint,
)
from graph.supervisor.models import SupervisorDecision, SupervisorTarget
from graph.supervisor.prompts import SUPERVISOR_PROMPT
from graph.supervisor.state import SupervisorState
from graph.supervisor.validators import validate_supervisor_decision


MAX_HANDOFF_HISTORY = 50


@dataclass
class SupervisorService:
    openai_client: AsyncOpenAI
    model: str
    timeout_seconds: float = 30.0
    config: SupervisorDevelopmentConfig = SupervisorDevelopmentConfig()

    async def decide(
        self,
        state: SupervisorState,
        allowed_handoffs: list[SupervisorTarget],
    ) -> SupervisorDecision:
        if self.config.development and self.config.force_timeout:
            print("Supervisor forced development timeout")
            raise TimeoutError("Forced supervisor timeout for development")
        if self.config.development and self.config.force_invalid_target:
            print("Supervisor forced invalid target: deployment")
            return SupervisorDecision.model_construct(
                target="deployment",
                reason="Forced invalid target for development.",
                confidence=0.9,
            )
        if self.config.development and self.config.force_target:
            return SupervisorDecision.model_construct(
                target=self.config.force_target,
                reason=f"Forced target {self.config.force_target} for development.",
                confidence=0.9,
            )
        payload = {
            "objective": state.get("original_user_message"),
            "intent": state.get("workflow_intent"),
            "current_stage": state.get("current_stage"),
            "planning": state.get("planning_summary"),
            "implementation": state.get("implementation_summary"),
            "testing": state.get("testing_summary"),
            "allowed_handoffs": [target.value for target in allowed_handoffs],
            "errors": state.get("supervisor_errors", []),
        }
        response = await asyncio.wait_for(
            self.openai_client.responses.parse(
                model=self.model,
                instructions=SUPERVISOR_PROMPT,
                input=json.dumps(payload, ensure_ascii=False),
                text_format=SupervisorDecision,
            ),
            timeout=self.timeout_seconds,
        )
        if getattr(response, "status", None) == "incomplete":
            raise RuntimeError("supervisor_output_incomplete")
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            refused = any(
                getattr(content, "type", None) == "refusal"
                for item in (getattr(response, "output", None) or [])
                for content in (getattr(item, "content", None) or [])
            )
            raise RuntimeError("supervisor_refused" if refused else "supervisor_output_empty")
        try:
            return SupervisorDecision.model_validate(parsed)
        except ValidationError as exc:
            raise RuntimeError("supervisor_output_invalid") from exc


def _handoff_entry(
    state: SoftwareFactoryState,
    target: SupervisorTarget,
    reason: str,
    source: str,
    confidence: float,
    attempted_target: str | None = None,
    selected_target: str | None = None,
    progress_fingerprint: dict[str, Any] | None = None,
    progress_detected: bool | None = None,
) -> dict[str, Any]:
    history = state.get("handoff_history") or []
    last_sequence = history[-1].get("sequence", len(history)) if history else 0
    sequence = int(last_sequence) + 1 if isinstance(last_sequence, int) else len(history) + 1
    effective_target = target.value
    attempted = attempted_target or effective_target
    selected = selected_target or effective_target
    branch_id = (
        f"fork:{state.get('fork_origin_checkpoint_id')}"
        if state.get("fork_origin_checkpoint_id")
        else "original"
    )
    return {
        "sequence": sequence,
        "from": "supervisor",
        "to": effective_target,
        "attempted_to": attempted,
        "selected_to": selected,
        "executed_to": effective_target,
        "reason": reason,
        "source": source,
        "confidence": confidence,
        "progress_fingerprint": progress_fingerprint
        or supervisor_progress_fingerprint(state).as_serializable(),
        **(
            {"progress_detected": progress_detected}
            if progress_detected is not None
            else {}
        ),
        "branch_id": branch_id,
        "planning_attempts": int(state.get("planning_attempts", 0)),
        "implementation_attempts": int(state.get("implementation_attempts", 0)),
        "tests_executed": bool(state.get("tests_executed")),
        "tests_passed": bool(state.get("tests_passed")),
        "repair_phase": state.get("repair_phase"),
        "repair_attempts": int(state.get("repair_attempts", 0)),
        "lineage": "fork" if state.get("fork_origin_checkpoint_id") else "original",
    }


def _stagnant_loop_probe_updates(
    state: SoftwareFactoryState,
    allowed: list[SupervisorTarget],
) -> dict[str, Any]:
    target = SupervisorTarget.PLANNING
    fingerprint = state.get("supervisor_stagnant_loop_fingerprint")
    if not isinstance(fingerprint, dict):
        fingerprint = supervisor_progress_fingerprint(state).as_serializable()
    probe_count = int(state.get("supervisor_stagnant_loop_probe_count", 0)) + 1
    history = list(state.get("handoff_history") or [])
    history.append(
        _handoff_entry(
            state,
            target,
            "Forced stagnant loop probe for development.",
            "development_loop_probe",
            1.0,
            attempted_target=target.value,
            selected_target=target.value,
            progress_fingerprint=fingerprint,
            progress_detected=False,
        )
    )
    history = history[-MAX_HANDOFF_HISTORY:]
    loop_detected = detect_supervisor_loop(history, state)
    errors = list(state.get("supervisor_errors") or [])
    if loop_detected:
        print("Supervisor repeated handoff detected")
        print(f"- target: {target.value}")
        print("- repetitions: 3")
        print("- progress: false")
        print("terminal_status=supervisor_loop_detected")
        errors.append("supervisor_loop_detected")
        return {
            **from_supervisor_output(
                SupervisorState(
                    supervisor_decision=SupervisorTarget.FINALIZE.value,
                    supervisor_reason="Supervisor loop detected; finalizing safely.",
                    supervisor_confidence=1.0,
                    supervisor_decision_source="development_loop_probe",
                    supervisor_attempts=int(state.get("supervisor_attempts", 0)),
                    allowed_handoffs=[target.value for target in allowed],
                    handoff_history=history,
                    supervisor_errors=list(dict.fromkeys(errors)),
                    supervisor_invalid_decision_count=int(
                        state.get("supervisor_invalid_decision_count", 0)
                    ),
                    consecutive_invalid_decisions=int(
                        state.get("consecutive_invalid_decisions", 0)
                    ),
                    supervisor_stagnant_loop_probe_count=probe_count,
                    supervisor_stagnant_loop_fingerprint=fingerprint,
                    supervisor_stagnant_loop_active=False,
                )
            ),
            "terminal_status": "supervisor_loop_detected",
            "failure_type": "supervisor_loop_detected",
            "failure_stage": "supervisor",
            "failure_message": "El Supervisor detectó un ciclo de handoffs sin progreso.",
        }

    print("Supervisor stagnant loop probe:")
    print(f"- target: {target.value}")
    print(f"- repetition: {probe_count}")
    print("- progress: false")
    return from_supervisor_output(
        SupervisorState(
            supervisor_decision="supervisor_loop_probe",
            supervisor_reason="Continue controlled stagnant-loop probe.",
            supervisor_confidence=1.0,
            supervisor_decision_source="development_loop_probe",
            supervisor_attempts=int(state.get("supervisor_attempts", 0)),
            allowed_handoffs=[target.value for target in allowed],
            handoff_history=history,
            supervisor_errors=list(dict.fromkeys(errors)),
            supervisor_invalid_decision_count=int(
                state.get("supervisor_invalid_decision_count", 0)
            ),
            consecutive_invalid_decisions=int(
                state.get("consecutive_invalid_decisions", 0)
            ),
            supervisor_stagnant_loop_probe_count=probe_count,
            supervisor_stagnant_loop_fingerprint=fingerprint,
            supervisor_stagnant_loop_active=True,
        )
    )


async def supervisor_node(
    state: SoftwareFactoryState,
    service: SupervisorService,
) -> dict[str, Any]:
    if approval_blocks_supervisor(state):
        return {}

    private_state = to_supervisor_input(state)
    allowed = determine_allowed_handoffs(state)
    print("Supervisor input:")
    print(f"- current_stage: {private_state.get('current_stage')}")
    print(f"- allowed_handoffs: {[target.value for target in allowed]}")
    print(f"- planning_valid: {private_state.get('planning_summary', {}).get('valid')}")
    print(f"- implementation_valid: {private_state.get('implementation_summary', {}).get('valid')}")
    print(f"- tests_passed: {private_state.get('testing_summary', {}).get('passed')}")

    config = getattr(service, "config", SupervisorDevelopmentConfig())
    if config.development and config.force_stagnant_loop:
        return _stagnant_loop_probe_updates(state, allowed)

    mandatory = resolve_mandatory_handoff(state, allowed)
    force_model = (
        config.development
        and config.force_model_decision
        and len(allowed) >= 2
        and mandatory not in {SupervisorTarget.TESTING_REPAIR, SupervisorTarget.FINALIZE}
    )
    errors = list(state.get("supervisor_errors") or [])
    attempts = int(state.get("supervisor_attempts", 0))
    max_attempts = int(state.get("max_supervisor_attempts", 3))
    source = "deterministic"
    attempted_target: str | None = None
    selected_target: str | None = None
    invalid_decision = False
    if mandatory is not None and (not force_model or attempts >= max_attempts):
        decision = SupervisorDecision(
            target=mandatory,
            reason=f"Deterministic policy selected {mandatory.value}.",
            confidence=1.0,
        )
        attempted_target = mandatory.value
        selected_target = mandatory.value
    else:
        try:
            decision = await service.decide(private_state, allowed)
            attempted_target = str(decision.target)
            decision_errors = validate_supervisor_decision(decision, allowed, state)
            if decision_errors:
                invalid_decision = True
                attempts += 1
                errors.extend(["supervisor_invalid_decision", *decision_errors])
                print(f"Supervisor validation failed: {', '.join(decision_errors)}")
                target = safe_fallback_target(allowed)
                decision = SupervisorDecision(
                    target=target,
                    reason="Safe deterministic fallback after an invalid supervisor decision.",
                    confidence=1.0,
                )
                selected_target = target.value
                source = "fallback"
                print("Supervisor fallback selected")
            else:
                decision = SupervisorDecision(
                    target=SupervisorTarget(str(decision.target)),
                    reason=decision.reason,
                    confidence=decision.confidence,
                )
                selected_target = decision.target.value
                source = "model"
        except TimeoutError:
            attempts += 1
            errors.append("supervisor_timeout")
            target = safe_fallback_target(allowed)
            decision = SupervisorDecision(
                target=target,
                reason="Safe deterministic fallback after supervisor timeout.",
                confidence=1.0,
            )
            selected_target = target.value
            source = "fallback"
            print("Supervisor fallback:")
            print(f"- target: {target.value}")
            print("- source: fallback")
        except Exception as exc:
            attempts += 1
            errors.extend(["supervisor_decision_failed", str(exc)[:500]])
            target = safe_fallback_target(allowed)
            decision = SupervisorDecision(
                target=target,
                reason="Safe deterministic fallback after supervisor failure.",
                confidence=1.0,
            )
            selected_target = target.value
            source = "fallback"
            print("Supervisor fallback:")
            print(f"- target: {target.value}")
            print("- source: fallback")

    history = list(state.get("handoff_history") or [])
    history.append(
        _handoff_entry(
            state,
            decision.target,
            decision.reason,
            source,
            decision.confidence,
            attempted_target=attempted_target,
            selected_target=selected_target,
        )
    )
    history = history[-MAX_HANDOFF_HISTORY:]
    loop_detected = detect_supervisor_loop(history, state)
    terminal_updates: dict[str, Any] = {}
    if loop_detected:
        repeated_target = attempted_target or decision.target.value
        print("Supervisor repeated handoff detected")
        print(f"- target: {repeated_target}")
        print("- repetitions: 3")
        print("- progress: false")
        print("terminal_status=supervisor_loop_detected")
        decision = SupervisorDecision(
            target=SupervisorTarget.FINALIZE,
            reason="Supervisor loop detected; finalizing safely.",
            confidence=1.0,
        )
        source = "deterministic"
        history[-1] = _handoff_entry(
            state,
            decision.target,
            decision.reason,
            source,
            decision.confidence,
            attempted_target=repeated_target,
            selected_target=decision.target.value,
        )
        history[-1]["sequence"] = len(state.get("handoff_history") or []) + 1
        errors.append("supervisor_loop_detected")
        terminal_updates = {
            "terminal_status": "supervisor_loop_detected",
            "failure_type": "supervisor_loop_detected",
            "failure_stage": "supervisor",
            "failure_message": "El Supervisor detectó un ciclo de handoffs sin progreso.",
        }

    private_output = SupervisorState(
        supervisor_decision=decision.target.value,
        supervisor_reason=decision.reason,
        supervisor_confidence=decision.confidence,
        supervisor_decision_source=source,
        supervisor_attempts=attempts,
        allowed_handoffs=[target.value for target in allowed],
        handoff_history=history,
        supervisor_errors=list(dict.fromkeys(errors)),
        supervisor_invalid_decision_count=int(
            state.get("supervisor_invalid_decision_count", 0)
        )
        + (1 if invalid_decision else 0),
        consecutive_invalid_decisions=(
            int(state.get("consecutive_invalid_decisions", 0)) + 1
            if invalid_decision
            else 0
        ),
    )
    print("Supervisor decision:")
    print(f"- target: {decision.target.value}")
    print(f"- reason: {decision.reason}")
    print(f"- confidence: {decision.confidence}")
    print(f"- source: {source}")
    return {**from_supervisor_output(private_output), **terminal_updates}
