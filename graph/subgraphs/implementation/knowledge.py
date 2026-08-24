from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from graph.subgraphs.implementation.state import ImplementationState


DEFAULT_DEVELOPER_KNOWLEDGE_TYPES = (
    "architecture",
    "decision",
    "convention",
    "code_pattern",
    "solution",
    "workflow_learning",
    "documentation",
)
TEST_TERMS = ("test", "tests", "testing", "pytest", "prueba", "pruebas", "integration")
INCIDENT_TERMS = (
    "bug",
    "error",
    "failure",
    "incident",
    "outage",
    "repair",
    "correg",
    "fallo",
    "incidente",
)
PATH_PATTERN = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")
CONTEXT_DELIMITER_PATTERN = re.compile(r"</?retrieved_project_knowledge>", re.I)


def _positive_environment_int(name: str, default: int, *, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, min(value, maximum))


def estimate_knowledge_tokens(value: str) -> int:
    """Return the same deterministic four-byte approximation used by FinOps."""
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
    normalized = " ".join(rendered.split())
    return normalized[:limit]


def _append_unique(values: list[str], candidate: str) -> None:
    normalized = candidate.strip()
    if normalized and normalized not in values:
        values.append(normalized)


@dataclass(frozen=True, slots=True)
class DeveloperKnowledgeQuery:
    query: str
    knowledge_types: tuple[str, ...]
    top_k: int


class DeveloperKnowledgeQueryBuilder:
    """Builds a project-scoped retrieval request from selected planning fields."""

    def __init__(self, *, top_k: int | None = None) -> None:
        self.top_k = top_k or _positive_environment_int(
            "DEVELOPER_KNOWLEDGE_TOP_K", 5, maximum=20
        )

    @staticmethod
    def _paths(state: Mapping[str, Any], tasks: list[Any]) -> list[str]:
        paths: list[str] = []
        for value in state.get("generated_files", []) or []:
            if isinstance(value, str):
                _append_unique(paths, value.replace("\\", "/"))
        proposal = state.get("project_implementation")
        if isinstance(proposal, Mapping):
            for item in proposal.get("files", []) or []:
                if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                    _append_unique(paths, str(item["path"]).replace("\\", "/"))
        for task in tasks:
            rendered = _compact(task, limit=1000)
            for match in PATH_PATTERN.findall(rendered.replace("\\", "/")):
                _append_unique(paths, match)
        return paths[:12]

    def build(self, state: ImplementationState) -> DeveloperKnowledgeQuery:
        analysis = state.get("requirement_analysis") or {}
        if not isinstance(analysis, Mapping):
            analysis = {}
        tasks = list(state.get("implementation_tasks", []) or [])
        acceptance = list(state.get("acceptance_criteria", []) or [])
        proposal = state.get("project_implementation")
        proposal_framework = (
            proposal.get("framework") if isinstance(proposal, Mapping) else None
        )
        framework = _compact(
            analysis.get("project_type")
            or state.get("detected_test_framework")
            or proposal_framework
        )
        constraints = [
            *(analysis.get("constraints") or []),
            *(analysis.get("non_functional_requirements") or []),
        ]
        parts = [f"Requirement: {_compact(state.get('original_user_message'), limit=1600)}"]
        objective = _compact(analysis.get("objective"), limit=800)
        if objective:
            parts.append(f"Objective: {objective}")
        if framework:
            parts.append(f"Framework: {framework}")
        if tasks:
            parts.append(f"Implementation tasks: {_compact(tasks, limit=1500)}")
        if acceptance:
            parts.append(f"Acceptance criteria: {_compact(acceptance, limit=1000)}")
        if constraints:
            parts.append(f"Architecture constraints: {_compact(constraints, limit=1000)}")
        paths = self._paths(state, tasks)
        if paths:
            parts.append(f"Relevant files: {', '.join(paths)}")
        parts.append(
            "Find project architecture, decisions, conventions, code patterns, similar solutions, "
            "workflow learnings, and documentation relevant to this implementation."
        )
        query = "\n".join(part for part in parts if not part.endswith(": "))[:6000]
        normalized_query = query.casefold()
        knowledge_types = list(DEFAULT_DEVELOPER_KNOWLEDGE_TYPES)
        if any(term in normalized_query for term in TEST_TERMS):
            knowledge_types.insert(4, "test_pattern")
        if any(term in normalized_query for term in INCIDENT_TERMS):
            solution_index = knowledge_types.index("solution") + 1
            knowledge_types.insert(solution_index, "incident")
        return DeveloperKnowledgeQuery(
            query=query,
            knowledge_types=tuple(knowledge_types),
            top_k=self.top_k,
        )


@dataclass(frozen=True, slots=True)
class DeveloperKnowledgeContext:
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


class DeveloperKnowledgeContextBuilder:
    """Renders untrusted retrieved content with bounded, durable provenance."""

    def __init__(self, *, max_tokens: int | None = None) -> None:
        self.max_tokens = max_tokens or _positive_environment_int(
            "DEVELOPER_KNOWLEDGE_MAX_TOKENS", 4000, maximum=32_000
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
    ) -> DeveloperKnowledgeContext:
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
            candidate = f"{opening}\n" + "\n\n".join([*blocks, block]) + f"\n{closing}"
            if estimate_knowledge_tokens(candidate) <= self.max_tokens:
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
                if estimate_knowledge_tokens(partial_candidate) <= self.max_tokens:
                    fitted = partial_block
                    low = middle + 1
                else:
                    high = middle - 1
            if fitted:
                blocks.append(fitted)
                sources.append(source)
            break

        if not blocks:
            return DeveloperKnowledgeContext("", (), 0)
        context = f"{opening}\n" + "\n\n".join(blocks) + f"\n{closing}"
        return DeveloperKnowledgeContext(
            context=context,
            sources=tuple(sources),
            token_count=estimate_knowledge_tokens(context),
        )
