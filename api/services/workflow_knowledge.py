from __future__ import annotations

from typing import Any, Mapping


KNOWLEDGE_SOURCE_FIELDS = (
    "knowledge_id",
    "chunk_id",
    "source_reference",
    "knowledge_type",
    "version",
    "retrieval_score",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _value(
    grouped: Mapping[str, Any],
    grouped_key: str,
    values: Mapping[str, Any],
    flat_key: str,
    default: Any,
) -> Any:
    if grouped_key in grouped:
        return grouped[grouped_key]
    if flat_key in values:
        return values[flat_key]
    return default


def _knowledge_summary(
    values: Mapping[str, Any],
    *,
    result_key: str,
    state_key: str,
    retrieval_id_key: str,
    sources_key: str,
    used_key: str,
    count_key: str,
    tokens_key: str,
    grouped_key: str = "knowledge",
) -> dict[str, Any]:
    grouped_result = _mapping(values.get(result_key))
    knowledge = _mapping(grouped_result.get(grouped_key))
    raw_sources = _value(
        knowledge,
        "sources",
        values,
        sources_key,
        [],
    )
    sources = [
        {key: item.get(key) for key in KNOWLEDGE_SOURCE_FIELDS}
        for item in raw_sources
        if isinstance(item, Mapping)
    ] if isinstance(raw_sources, list) else []
    return {
        "state": _value(
            knowledge,
            "state",
            values,
            state_key,
            "not_started",
        ),
        "retrieval_id": _value(
            knowledge,
            "retrieval_id",
            values,
            retrieval_id_key,
            None,
        ),
        "retrieval_used": bool(
            _value(
                knowledge,
                "used",
                values,
                used_key,
                False,
            )
        ),
        "retrieved_context_count": int(
            _value(
                knowledge,
                "retrieved_context_count",
                values,
                count_key,
                0,
            )
            or 0
        ),
        "context_tokens": int(
            _value(
                knowledge,
                "context_tokens",
                values,
                tokens_key,
                0,
            )
            or 0
        ),
        "sources": sources,
    }


def planner_knowledge_summary(values: Mapping[str, Any]) -> dict[str, Any]:
    return _knowledge_summary(
        values,
        result_key="planning_result",
        state_key="planner_knowledge_state",
        retrieval_id_key="planner_knowledge_retrieval_id",
        sources_key="planner_knowledge_sources",
        used_key="planner_knowledge_retrieval_used",
        count_key="planner_retrieved_context_count",
        tokens_key="planner_knowledge_context_tokens",
    )


def qa_knowledge_summary(values: Mapping[str, Any]) -> dict[str, Any]:
    return _knowledge_summary(
        values,
        result_key="testing_result",
        state_key="qa_knowledge_state",
        retrieval_id_key="qa_knowledge_retrieval_id",
        sources_key="qa_knowledge_sources",
        used_key="qa_knowledge_retrieval_used",
        count_key="qa_retrieved_context_count",
        tokens_key="qa_knowledge_context_tokens",
    )


def repair_knowledge_summary(values: Mapping[str, Any]) -> dict[str, Any]:
    return _knowledge_summary(
        values,
        result_key="testing_result",
        grouped_key="repair_knowledge",
        state_key="repair_knowledge_state",
        retrieval_id_key="repair_knowledge_retrieval_id",
        sources_key="repair_knowledge_sources",
        used_key="repair_knowledge_retrieval_used",
        count_key="repair_retrieved_context_count",
        tokens_key="repair_knowledge_context_tokens",
    )


def workflow_learning_summary(values: Mapping[str, Any]) -> dict[str, Any]:
    submissions = {
        str(item.get("candidate_id")): item
        for item in (values.get("workflow_learning_submission_results") or [])
        if isinstance(item, Mapping) and item.get("candidate_id")
    }
    candidates: list[dict[str, Any]] = []
    for item in values.get("workflow_learning_candidates") or []:
        if not isinstance(item, Mapping) or not item.get("candidate_id"):
            continue
        submission = submissions.get(str(item["candidate_id"]), {})
        candidates.append({
            "candidate_id": str(item["candidate_id"]),
            "knowledge_type": str(item.get("knowledge_type") or "workflow_learning"),
            "confidence": float(item.get("confidence") or 0),
            "submission_status": str(submission.get("submission_status") or "pending"),
            "knowledge_id": submission.get("knowledge_id"),
            "source_reference": str(item.get("source_reference") or ""),
            "created_at": item.get("created_at"),
        })
    statuses = {"candidate", "indexed", "duplicate", "rejected"}
    return {
        "state": str(values.get("workflow_learning_state") or "not_started"),
        "extracted_count": len(candidates),
        "submitted_count": sum(item["submission_status"] in statuses for item in candidates),
        "duplicate_count": sum(item["submission_status"] == "duplicate" for item in candidates),
        "rejected_count": sum(item["submission_status"] == "rejected" for item in candidates),
        "candidates": candidates,
    }
