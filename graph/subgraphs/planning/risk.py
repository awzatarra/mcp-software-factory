from __future__ import annotations

import re
import time
from dataclasses import dataclass

from graph.subgraphs.planning.models import ImplementationTask, PlanningOutput


RiskLevel = str

DEPENDENCY_TARGETS = {
    "directory.packages.props",
    "package-lock.json",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
}
LOW_DOC_TARGETS = {"readme.md", "docs", "documentation"}
CONFIG_TERMS = {"config", "configuration", "settings", "env", "runtime", "appsettings", "yaml", "yml", "toml"}
INFRA_TERMS = {"docker", "kubernetes", "terraform", "helm", "compose"}
SECURITY_TERMS = {"auth", "authentication", "authorization", "credential", "credentials", "permission", "permissions", "secret", "secrets", "token", "tokens"}
DATABASE_TERMS = {"database", "db", "migration", "migrations", "schema", "sql"}
DESTRUCTIVE_TERMS = {"delete", "drop", "remove", "reset", "truncate", "wipe", "borrar", "eliminar", "sobrescribir", "overwrite", "irreversible"}
CREATE_TERMS = {"add", "create", "crear", "agregar", "generate", "generar", "implement", "implementar"}
MODIFY_TERMS = {"edit", "modify", "modificar", "update", "actualizar", "change", "cambiar"}


@dataclass(frozen=True)
class RiskAnalysisResult:
    risk_score: int
    risk_level: RiskLevel
    risk_reasons: list[str]
    sensitive_tasks: list[dict[str, object]]
    impact_areas: list[str]
    duration_ms: float


def _task_text(task: ImplementationTask) -> str:
    return f"{task.role} {task.title} {task.description}".casefold()


def _contains(text: str, terms: set[str]) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms)


def _paths(text: str) -> list[str]:
    return list(dict.fromkeys(path.replace("\\", "/") for path in re.findall(r"\b[\w.-]+(?:/[\w.-]+)*\.[A-Za-z0-9]+\b", text)))


def _risk_level(score: int) -> RiskLevel:
    if score >= 75:
        return "critical"
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"


def _impact_for_task(text: str, paths: list[str]) -> set[str]:
    impacts: set[str] = set()
    if "test" in text or "pytest" in text or any("/test" in path or path.startswith("test") for path in paths):
        impacts.add("tests")
    if "readme" in text or "documentation" in text or "documentacion" in text:
        impacts.add("documentation")
    if any(path.lower() in DEPENDENCY_TARGETS or path.lower().endswith(".csproj") for path in paths):
        impacts.add("dependencies")
    if _contains(text, CONFIG_TERMS):
        impacts.add("configuration")
    if _contains(text, DATABASE_TERMS):
        impacts.add("database")
    if _contains(text, SECURITY_TERMS):
        impacts.add("security")
    if _contains(text, INFRA_TERMS):
        impacts.add("infrastructure")
    if "runtime" in text or "entrypoint" in text or "main.py" in text:
        impacts.add("runtime")
    if paths:
        impacts.add("filesystem")
    if not impacts or any(token in text for token in ("api", "app", "endpoint", "fastapi", "main.py")):
        impacts.add("application")
    return impacts


def _task_risk(task: ImplementationTask) -> tuple[int, list[str], set[str]]:
    text = _task_text(task)
    paths = _paths(text)
    impacts = _impact_for_task(text, paths)
    score = 4
    reasons: list[str] = []

    if impacts == {"documentation", "filesystem"} or "documentation" in impacts and not (impacts - {"documentation", "filesystem"}):
        score = 2
        reasons.append("documentation_change")
    elif "tests" in impacts and not (impacts - {"tests", "filesystem"}):
        score = 3
        reasons.append("test_change")
    elif "application" in impacts:
        score = max(score, 8)
        reasons.append("application_change")

    if _contains(text, MODIFY_TERMS):
        score += 10
        reasons.append("modifies_existing_files")
    elif _contains(text, CREATE_TERMS):
        score += 2
        reasons.append("creates_files")

    if "dependencies" in impacts:
        score = max(score, 28)
        reasons.append("dependency_change")
    if "configuration" in impacts:
        score = max(score, 30)
        reasons.append("configuration_change")
    if "runtime" in impacts:
        score = max(score, 25)
        reasons.append("runtime_change")
    if "infrastructure" in impacts:
        score = max(score, 55)
        reasons.append("infrastructure_change")
    if "database" in impacts:
        score = max(score, 58 if "migration" in text else 45)
        reasons.append("database_change")
    if "security" in impacts:
        score = max(score, 60)
        reasons.append("security_change")
    if _contains(text, DESTRUCTIVE_TERMS):
        score = max(score, 80 if ("data" in text or "schema" in text or "secret" in text or "credential" in text) else 65)
        reasons.append("destructive_operation")
    if "secret" in text or "credential" in text:
        score = max(score, 80)
        reasons.append("sensitive_credential_change")

    return min(score, 100), list(dict.fromkeys(reasons)), impacts


def analyze_plan_risk(plan: PlanningOutput) -> RiskAnalysisResult:
    """Risk formula: max task score + breadth modifier + destructive modifier, clamped to 0..100."""
    started = time.perf_counter()
    task_scores: list[int] = []
    all_reasons: list[str] = []
    impact_areas: set[str] = set()
    sensitive_tasks: list[dict[str, object]] = []
    target_count = 0

    for task in plan.tasks:
        score, reasons, impacts = _task_risk(task)
        task_scores.append(score)
        all_reasons.extend(reasons)
        impact_areas.update(impacts)
        target_count += len(_paths(_task_text(task)))
        if score >= 50:
            sensitive_tasks.append(
                {
                    "task_id": str(task.order),
                    "risk_level": _risk_level(score),
                    "reasons": reasons,
                }
            )

    base = max(task_scores, default=0)
    breadth = len(impact_areas)
    breadth_modifier = 0
    if target_count >= 5 or breadth >= 5:
        breadth_modifier = 10
        all_reasons.append("broad_multi_area_impact")
    elif target_count >= 2 or breadth >= 3:
        breadth_modifier = 6
        all_reasons.append("multi_area_impact")
    destructive_modifier = 10 if "destructive_operation" in all_reasons else 0
    sensitive_volume_modifier = 10 if len(sensitive_tasks) >= 5 else 0
    score = max(0, min(100, base + breadth_modifier + destructive_modifier + sensitive_volume_modifier))
    return RiskAnalysisResult(
        risk_score=score,
        risk_level=_risk_level(score),
        risk_reasons=list(dict.fromkeys(all_reasons)),
        sensitive_tasks=sensitive_tasks,
        impact_areas=sorted(impact_areas),
        duration_ms=(time.perf_counter() - started) * 1000,
    )
