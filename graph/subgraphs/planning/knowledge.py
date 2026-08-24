from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from graph.subgraphs.planning.state import PlanningState


DEFAULT_PLANNER_KNOWLEDGE_TYPES = (
    "architecture",
    "decision",
    "convention",
    "documentation",
    "workflow_learning",
    "solution",
    "code_pattern",
)
TEST_TERMS = (
    "test",
    "tests",
    "testing",
    "pytest",
    "integration",
    "prueba",
    "pruebas",
)
CONTEXT_DELIMITER_PATTERN = re.compile(r"</?retrieved_project_knowledge>", re.I)


def _positive_environment_int(name: str, default: int, *, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, min(value, maximum))


def estimate_planner_knowledge_tokens(value: str) -> int:
    if not value:
        return 0
    return max(1, (len(value.encode("utf-8")) + 3) // 4)


def _compact(value: Any, *, limit: int = 1200) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        rendered = value
    elif isinstance(value, (list, tuple)):
        rendered = "; ".join(_compact(item, limit=400) for item in value)
    elif isinstance(value, Mapping):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    else:
        rendered = str(value)
    return " ".join(rendered.split())[:limit]


@dataclass(frozen=True, slots=True)
class PlannerKnowledgeQuery:
    query: str
    knowledge_types: tuple[str, ...]
    top_k: int


class PlannerKnowledgeQueryBuilder:
    """Build a scoped retrieval request from selected planning inputs."""

    def __init__(self, *, top_k: int | None = None) -> None:
        self.top_k = top_k or _positive_environment_int(
            "PLANNER_KNOWLEDGE_TOP_K", 5, maximum=20
        )

    def build(self, state: PlanningState) -> PlannerKnowledgeQuery:
        analysis = state.get("requirement_analysis") or {}
        if not isinstance(analysis, Mapping):
            analysis = {}
        project_name = _compact(
            state.get("project_name")
            or analysis.get("project_name")
            or state.get("created_project_name"),
            limit=200,
        )
        framework = _compact(
            state.get("detected_test_framework") or analysis.get("project_type"),
            limit=200,
        )
        constraints = [
            *(analysis.get("constraints") or []),
            *(analysis.get("non_functional_requirements") or []),
        ]
        parts = [
            f"Requirement: {_compact(state.get('original_user_message'), limit=1800)}"
        ]
        if project_name:
            parts.append(f"Project: {project_name}")
        workflow_intent = _compact(state.get("workflow_intent"), limit=200)
        if workflow_intent:
            parts.append(f"Workflow intent: {workflow_intent}")
        objective = _compact(analysis.get("objective"), limit=900)
        if objective:
            parts.append(f"Requirement analysis: {objective}")
        functional = _compact(analysis.get("functional_requirements"), limit=1200)
        if functional:
            parts.append(f"Functional scope: {functional}")
        if framework:
            parts.append(f"Framework: {framework}")
        if constraints:
            parts.append(
                f"Architecture constraints: {_compact(constraints, limit=1200)}"
            )
        acceptance = _compact(state.get("acceptance_criteria"), limit=1400)
        if acceptance:
            parts.append(f"Acceptance criteria: {acceptance}")
        parts.append(
            "Find project architecture, decisions, conventions, documentation, "
            "workflow learnings, solutions, and code patterns relevant to planning."
        )
        query = "\n".join(part for part in parts if not part.endswith(": "))[:6000]
        knowledge_types = list(DEFAULT_PLANNER_KNOWLEDGE_TYPES)
        testing_scope = " ".join(
            (
                _compact(state.get("original_user_message"), limit=1800),
                objective,
                functional,
            )
        ).casefold()
        if any(term in testing_scope for term in TEST_TERMS):
            knowledge_types.append("test_pattern")
        return PlannerKnowledgeQuery(
            query=query,
            knowledge_types=tuple(knowledge_types),
            top_k=self.top_k,
        )


@dataclass(frozen=True, slots=True)
class PlannerKnowledgeContext:
    context: str
    sources: tuple[dict[str, Any], ...]
    token_count: int


def _escape_context_delimiters(value: str) -> str:
    return CONTEXT_DELIMITER_PATTERN.sub(
        lambda match: match.group(0).replace("<", "&lt;").replace(">", "&gt;"),
        value,
    )


def _safe_context_metadata(value: Any) -> str:
    return _escape_context_delimiters(_compact(value, limit=500)) or "n/a"


class PlannerKnowledgeContextBuilder:
    """Render bounded untrusted context while preserving durable provenance."""

    def __init__(self, *, max_tokens: int | None = None) -> None:
        self.max_tokens = max_tokens or _positive_environment_int(
            "PLANNER_KNOWLEDGE_MAX_TOKENS", 3000, maximum=32_000
        )

    @staticmethod
    def _source(item: Mapping[str, Any]) -> dict[str, Any]:
        provenance = item.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = item
        return {
            "knowledge_id": provenance.get("knowledge_id"),
            "chunk_id": provenance.get("chunk_id") or item.get("chunk_id"),
            "source_reference": provenance.get("source_reference"),
            "knowledge_type": provenance.get("knowledge_type"),
            "retrieval_score": provenance.get("retrieval_score"),
            "version": provenance.get("version"),
        }

    @staticmethod
    def _block(index: int, source: Mapping[str, Any], content: str) -> str:
        return (
            f"[Knowledge {index}]\n"
            f"knowledge_id: {_safe_context_metadata(source.get('knowledge_id'))}\n"
            f"chunk_id: {_safe_context_metadata(source.get('chunk_id'))}\n"
            f"source_reference: {_safe_context_metadata(source.get('source_reference'))}\n"
            f"knowledge_type: {_safe_context_metadata(source.get('knowledge_type'))}\n"
            f"retrieval_score: {_safe_context_metadata(source.get('retrieval_score'))}\n"
            f"version: {_safe_context_metadata(source.get('version'))}\n"
            f"content:\n{content}"
        )

    def build(
        self,
        results: list[dict[str, Any]],
        *,
        top_k: int,
    ) -> PlannerKnowledgeContext:
        opening = "<retrieved_project_knowledge>"
        closing = "</retrieved_project_knowledge>"
        blocks: list[str] = []
        sources: list[dict[str, Any]] = []
        for item in results[:top_k]:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            source = self._source(item)
            safe_content = _escape_context_delimiters(content.strip())
            block = self._block(len(blocks) + 1, source, safe_content)
            candidate = (
                f"{opening}\n" + "\n\n".join([*blocks, block]) + f"\n{closing}"
            )
            if estimate_planner_knowledge_tokens(candidate) <= self.max_tokens:
                blocks.append(block)
                sources.append(source)
                continue

            marker = "\n[content truncated to token budget]"
            low, high = 0, len(safe_content)
            fitted = ""
            while low <= high:
                middle = (low + high) // 2
                partial = safe_content[:middle].rstrip() + marker
                partial_block = self._block(len(blocks) + 1, source, partial)
                partial_candidate = (
                    f"{opening}\n"
                    + "\n\n".join([*blocks, partial_block])
                    + f"\n{closing}"
                )
                if (
                    estimate_planner_knowledge_tokens(partial_candidate)
                    <= self.max_tokens
                ):
                    fitted = partial_block
                    low = middle + 1
                else:
                    high = middle - 1
            if fitted:
                blocks.append(fitted)
                sources.append(source)
            break

        if not blocks:
            return PlannerKnowledgeContext("", (), 0)
        context = f"{opening}\n" + "\n\n".join(blocks) + f"\n{closing}"
        return PlannerKnowledgeContext(
            context=context,
            sources=tuple(sources),
            token_count=estimate_planner_knowledge_tokens(context),
        )
