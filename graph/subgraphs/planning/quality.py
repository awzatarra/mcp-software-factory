from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from graph.state import SoftwareFactoryState
from graph.subgraphs.planning.models import ImplementationTask, PlanningOutput


QUALITY_VERSION = "planning-quality-v1"
QUALITY_WEIGHTS = {
    "requirement_coverage": 0.30,
    "dependency_coherence": 0.20,
    "task_clarity": 0.15,
    "plan_completeness": 0.25,
    "risk_residual": 0.10,
}
GENERIC_TASK_PHRASES = {
    "hacer backend",
    "implementar",
    "configurar cosas",
    "cosas de tests",
    "do backend",
    "implement",
}
TEST_TERMS = {"pytest", "qa", "test", "tests", "prueba", "pruebas", "validar"}
IMPLEMENT_TERMS = {"api", "app", "backend", "endpoint", "fastapi", "health", "implement", "implementar"}
DEPENDENCY_TARGETS = {"requirements.txt", "pyproject.toml", "package.json", "package-lock.json"}


@dataclass(frozen=True)
class PlanQualityResult:
    score: int
    level: str
    dimensions: dict[str, int]
    issues: list[str]
    decision_confidence: float
    version: str = QUALITY_VERSION


@dataclass(frozen=True)
class QualityGateDecision:
    decision: str
    reason: str
    reason_codes: list[str]
    refinement_required: bool


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _level(score: int) -> str:
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "good"
    if score >= 60:
        return "acceptable"
    if score >= 40:
        return "weak"
    return "poor"


def _task_text(task: ImplementationTask) -> str:
    return f"{task.role} {task.title} {task.description}".casefold()


def _plan_text(plan: PlanningOutput) -> str:
    return json.dumps(plan.model_dump(), ensure_ascii=False, sort_keys=True).casefold()


def _json_literals(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\{\s*[\"'][^{}\r\n]+?\}", text)))


def _endpoints(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in re.finditer(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+(/[^\s,.;]+)", text, re.IGNORECASE)))


def _is_test_task(task: ImplementationTask) -> bool:
    text = _task_text(task)
    return any(term in text for term in TEST_TERMS)


def _is_implementation_task(task: ImplementationTask) -> bool:
    text = _task_text(task)
    return any(term in text for term in IMPLEMENT_TERMS) and not (_is_test_task(task) and "developer" not in text)


def _has_dependency_manifest(plan: PlanningOutput) -> bool:
    text = _plan_text(plan)
    return any(target in text for target in DEPENDENCY_TARGETS)


def _requirement_coverage(plan: PlanningOutput, state: SoftwareFactoryState) -> tuple[int, list[str]]:
    original = str(state.get("original_user_message") or "")
    text = _plan_text(plan)
    signals: list[tuple[str, bool]] = []
    if "fastapi" in original.casefold():
        signals.append(("fastapi", "fastapi" in text))
    for endpoint in _endpoints(original):
        signals.append((endpoint, endpoint.casefold() in text))
    for literal in _json_literals(original):
        try:
            parsed = json.loads(literal.replace("'", '"'))
            normalized = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
            json_covered = re.sub(r"\s+", "", normalized).casefold() in re.sub(r"\s+", "", text)
            if isinstance(parsed, dict):
                json_covered = json_covered or all(
                    str(key).casefold() in text and str(value).casefold() in text
                    for key, value in parsed.items()
                )
        except json.JSONDecodeError:
            normalized = literal
            json_covered = re.sub(r"\s+", "", normalized).casefold() in re.sub(r"\s+", "", text)
        signals.append((literal, json_covered))
    if any(term in original.casefold() for term in TEST_TERMS):
        signals.append(("tests", any(_is_test_task(task) for task in plan.tasks)))
    if not signals:
        return 80, []
    covered = sum(1 for _name, ok in signals if ok)
    score = round(100 * covered / len(signals))
    issues = [] if score >= 80 else ["planning_quality_low_requirement_coverage"]
    return score, issues


def _dependency_coherence(plan: PlanningOutput, state: SoftwareFactoryState) -> tuple[int, list[str]]:
    task_count = len(plan.tasks)
    edges = state.get("planning_dependency_edges", [])
    edge_count = len(edges) if isinstance(edges, list) else sum(len(task.depends_on) for task in plan.tasks)
    if task_count <= 1:
        return 95, []
    max_reasonable = max(task_count - 1, 1)
    density = edge_count / max_reasonable
    score = 100
    if density > 2.0:
        score -= 25
    elif density > 1.3:
        score -= 12
    if not state.get("planning_execution_order"):
        score -= 5
    issues = ["planning_quality_high_coupling"] if score < 80 else []
    return max(score, 0), issues


def _task_clarity(plan: PlanningOutput) -> tuple[int, list[str]]:
    penalties = 0
    identities: set[tuple[str, str, str]] = set()
    for task in plan.tasks:
        text = _task_text(task)
        identity = (task.role.casefold(), task.title.casefold(), task.description.casefold())
        if identity in identities:
            penalties += 20
        identities.add(identity)
        if len(task.description.strip()) < 20:
            penalties += 15
        if task.description.strip().casefold() in GENERIC_TASK_PHRASES or task.title.strip().casefold() in GENERIC_TASK_PHRASES:
            penalties += 25
        if not any(term in text for term in CREATE_ACTION_TERMS):
            penalties += 6
    score = int(_clamp(100 - penalties, 0, 100))
    issues = ["planning_quality_low_task_clarity"] if score < 70 else []
    return score, issues


CREATE_ACTION_TERMS = {
    "actualizar",
    "agregar",
    "cambiar",
    "crear",
    "create",
    "generate",
    "generar",
    "implement",
    "implementar",
    "modificar",
    "update",
    "validar",
    "verificar",
}


def _plan_completeness(plan: PlanningOutput, state: SoftwareFactoryState) -> tuple[int, list[str]]:
    original = str(state.get("original_user_message") or "").casefold()
    checks: list[bool] = [any(_is_implementation_task(task) for task in plan.tasks)]
    if any(term in original for term in TEST_TERMS):
        checks.append(any(_is_test_task(task) for task in plan.tasks))
    if "fastapi" in original or plan.analysis.framework == "fastapi":
        checks.append(any("fastapi" in _task_text(task) or "endpoint" in _task_text(task) or "api" in _task_text(task) for task in plan.tasks))
    if _has_dependency_manifest(plan):
        checks.append(True)
    score = round(100 * sum(checks) / len(checks)) if checks else 80
    issues = ["planning_quality_incomplete_plan"] if score < 80 else []
    return score, issues


def _risk_residual(state: SoftwareFactoryState) -> tuple[int, list[str]]:
    level = str(state.get("planning_risk_level") or "low").casefold()
    score = {"low": 100, "medium": 80, "high": 55, "critical": 25}.get(level, 80)
    issues = ["planning_quality_high_residual_risk"] if score < 60 else []
    return score, issues


def _decision_confidence(
    quality_score: int,
    issues: list[str],
    state: SoftwareFactoryState,
) -> float:
    confidence = quality_score / 100
    risk_level = str(state.get("planning_risk_level") or "low").casefold()
    if risk_level == "high":
        confidence -= 0.12
    elif risk_level == "critical":
        confidence -= 0.25
    attempts = int(state.get("planning_attempts", 0) or 0)
    confidence -= 0 if attempts <= 0 else 0.03 if attempts == 1 else 0.07 if attempts == 2 else 0.12
    confidence -= min(len(issues) * 0.03, 0.15)
    approval_status = str(state.get("planning_approval_status") or "not_required")
    approval_required = bool(state.get("planning_approval_required"))
    if approval_required and approval_status == "approved":
        confidence += 0.05
    if attempts == 0 and not issues:
        confidence += 0.02
    if approval_required and approval_status == "awaiting_approval":
        confidence = min(confidence, 0.60)
    return round(_clamp(confidence, 0.0, 1.0), 3)


def score_plan_quality(plan: PlanningOutput, state: SoftwareFactoryState) -> PlanQualityResult:
    """Weighted average: coverage 30%, DAG coherence 20%, clarity 15%, completeness 25%, residual risk 10%."""
    dimensions: dict[str, int] = {}
    issues: list[str] = []
    for name, scorer in (
        ("requirement_coverage", lambda: _requirement_coverage(plan, state)),
        ("dependency_coherence", lambda: _dependency_coherence(plan, state)),
        ("task_clarity", lambda: _task_clarity(plan)),
        ("plan_completeness", lambda: _plan_completeness(plan, state)),
        ("risk_residual", lambda: _risk_residual(state)),
    ):
        value, found = scorer()
        dimensions[name] = int(_clamp(value, 0, 100))
        issues.extend(found)
    score = round(sum(dimensions[name] * weight for name, weight in QUALITY_WEIGHTS.items()))
    deduped_issues = list(dict.fromkeys(issues))
    return PlanQualityResult(
        score=int(_clamp(score, 0, 100)),
        level=_level(score),
        dimensions=dimensions,
        issues=deduped_issues,
        decision_confidence=_decision_confidence(int(score), deduped_issues, state),
    )


STRUCTURAL_QUALITY_ISSUES = {
    "planning_quality_low_requirement_coverage",
    "planning_quality_incomplete_plan",
    "planning_quality_low_task_clarity",
    "planning_quality_high_coupling",
}


def evaluate_quality_gate(
    *,
    quality_score: int | None,
    quality_level: str | None,
    quality_dimensions: dict[str, int],
    quality_issues: list[str],
    decision_confidence: float | None,
    attempts: int,
    max_attempts: int,
) -> QualityGateDecision:
    level = str(quality_level or "poor").casefold()
    issues = set(quality_issues)
    reason_codes: list[str] = []
    if level in {"weak", "poor"}:
        reason_codes.append("planning_quality_below_threshold")
    if quality_dimensions.get("requirement_coverage", 100) < 70:
        reason_codes.append("planning_quality_low_requirement_coverage")
    if quality_dimensions.get("plan_completeness", 100) < 70:
        reason_codes.append("planning_quality_incomplete_plan")
    if quality_dimensions.get("task_clarity", 100) < 50:
        reason_codes.append("planning_quality_low_task_clarity")
    if quality_dimensions.get("dependency_coherence", 100) < 60:
        reason_codes.append("planning_quality_high_coupling")
    if level == "acceptable" and issues.intersection(
        {"planning_quality_low_requirement_coverage", "planning_quality_incomplete_plan"}
    ):
        reason_codes.append("planning_quality_acceptable_with_structural_issue")
    if (
        decision_confidence is not None
        and decision_confidence < 0.55
        and issues.intersection(STRUCTURAL_QUALITY_ISSUES)
    ):
        reason_codes.append("planning_quality_low_confidence")
    reason_codes = list(dict.fromkeys(reason_codes))
    if not reason_codes:
        return QualityGateDecision("continue", "quality_gate_passed", [], False)
    if attempts >= max_attempts:
        return QualityGateDecision(
            "fail",
            "planning_quality_below_threshold",
            reason_codes,
            False,
        )
    return QualityGateDecision(
        "refine",
        reason_codes[0],
        reason_codes,
        True,
    )


def build_quality_refinement_guidance(
    *,
    quality_score: int | None,
    quality_dimensions: dict[str, int],
    quality_issues: list[str],
    gate_reason: str | None,
) -> list[str]:
    guidance: list[str] = []
    if quality_dimensions.get("requirement_coverage", 100) < 70 or "planning_quality_low_requirement_coverage" in quality_issues:
        guidance.append("Increase explicit coverage of required behavior, endpoints, response literals, and tests.")
    if quality_dimensions.get("task_clarity", 100) < 50 or "planning_quality_low_task_clarity" in quality_issues:
        guidance.append("Replace vague tasks with concrete actions, targets, and verification steps.")
    if quality_dimensions.get("plan_completeness", 100) < 70 or "planning_quality_incomplete_plan" in quality_issues:
        guidance.append("Add missing implementation, testing, or dependency tasks required by the request.")
    if quality_dimensions.get("dependency_coherence", 100) < 60 or "planning_quality_high_coupling" in quality_issues:
        guidance.append("Simplify dependencies between tasks and keep the execution DAG understandable.")
    if quality_score is not None:
        guidance.append(f"Improve the deterministic plan quality score above the gate threshold; current score is {quality_score}.")
    if gate_reason:
        guidance.append(f"Resolve quality gate reason: {gate_reason}.")
    return list(dict.fromkeys(guidance))
