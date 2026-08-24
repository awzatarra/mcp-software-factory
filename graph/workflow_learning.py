from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any, Mapping

from api.services.observability_context import get_observability_context
from graph.state import SoftwareFactoryState
from streaming import get_workflow_event_context


SUPPORTED_LEARNING_TYPES = frozenset({
    "decision", "convention", "code_pattern", "test_pattern",
    "incident", "solution", "workflow_learning",
})


def _compact(value: Any, limit: int = 1200) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return " ".join(rendered.split())[:limit]


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def deterministic_candidate_id(
    workflow_id: str,
    branch_id: str,
    knowledge_type: str,
    content: str,
    evidence_refs: list[dict[str, Any]],
) -> str:
    evidence = json.dumps(evidence_refs, ensure_ascii=False, sort_keys=True, default=str)
    material = "|".join((workflow_id, branch_id, knowledge_type, _normalized(content), evidence))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class WorkflowLearningCandidate:
    candidate_id: str
    project_id: str
    workflow_id: str
    branch_id: str
    agent_name: str
    knowledge_type: str
    content: str
    source_reference: str
    evidence_refs: list[dict[str, Any]]
    knowledge_provenance_ids: list[str]
    confidence: float
    extraction_method: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkflowLearningExtractor:
    """Extracts bounded learnings from explicit, structured workflow evidence."""

    def extract(
        self,
        *,
        workflow_id: str,
        branch_id: str,
        project_id: str,
        evidence: Mapping[str, Any],
        created_at: str | None = None,
    ) -> list[WorkflowLearningCandidate]:
        terminal = str(evidence.get("terminal_status") or "")
        useful_failure = bool(evidence.get("failure_message") or evidence.get("test_failure_summary"))
        if terminal not in {"completed", "tests_failed", "implementation_failed"}:
            return []
        if terminal != "completed" and not useful_failure:
            return []
        timestamp = created_at or datetime.now(UTC).isoformat()
        provenance = sorted({
            str(item.get("knowledge_id"))
            for source_key in (
                "planner_knowledge_sources", "developer_knowledge_sources",
                "qa_knowledge_sources", "repair_knowledge_sources",
            )
            for item in (evidence.get(source_key) or [])
            if isinstance(item, Mapping) and item.get("knowledge_id")
        })
        candidates: list[WorkflowLearningCandidate] = []

        def add(kind: str, agent: str, content: str, confidence: float,
                refs: list[dict[str, Any]], method: str = "deterministic") -> None:
            compact = _compact(content, 2400)
            if not compact or kind not in SUPPORTED_LEARNING_TYPES:
                return
            candidate_id = deterministic_candidate_id(
                workflow_id, branch_id, kind, compact, refs
            )
            source_kind = kind if kind in {"incident", "solution"} else "learning"
            candidates.append(WorkflowLearningCandidate(
                candidate_id=candidate_id,
                project_id=project_id,
                workflow_id=workflow_id,
                branch_id=branch_id,
                agent_name=agent,
                knowledge_type=kind,
                content=compact,
                source_reference=f"workflow:{workflow_id}:{source_kind}:{candidate_id}",
                evidence_refs=refs,
                knowledge_provenance_ids=provenance,
                confidence=confidence,
                extraction_method=method,
                created_at=timestamp,
            ))

        repair_attempts = int(evidence.get("repair_attempts") or 0)
        failure = _compact(
            evidence.get("test_failure_summary") or evidence.get("failure_message"), 1400
        )
        if terminal == "implementation_failed" and failure:
            add(
                "incident", "Developer",
                f"Implementation incident observed in {project_id}: {failure}", .80,
                [{"kind": "workflow_stage", "value": "implementation"}], "heuristic",
            )
        failing_files = [str(item).replace("\\", "/") for item in evidence.get("failing_test_files") or []]
        if repair_attempts > 0 and failure:
            add(
                "incident", "Repair", f"Incident observed in {project_id}: {failure}", .85,
                [{"kind": "repair_attempt", "value": repair_attempts},
                 *({"kind": "file_path", "value": path} for path in failing_files)],
                "heuristic",
            )
        repaired = (
            repair_attempts > 0
            and evidence.get("repair_phase") == "completed"
            and evidence.get("tests_passed") is True
            and bool(evidence.get("repair_before"))
            and bool(evidence.get("repair_after"))
        )
        if repaired:
            updated = [str(item).replace("\\", "/") for item in evidence.get("files_updated_during_repair") or []]
            add(
                "solution", "Repair",
                f"Validated repair for {project_id}: {_compact(evidence.get('repair_decision'), 1000)} "
                f"Changed {_compact(evidence.get('repair_before'), 500)} to "
                f"{_compact(evidence.get('repair_after'), 500)}; the re-run passed.",
                .90,
                [{"kind": "repair_attempt", "value": repair_attempts},
                 {"kind": "test_result", "value": _compact(evidence.get("final_test_result_summary"), 500)},
                 *({"kind": "file_path", "value": path} for path in updated)],
            )

        tests_passed = evidence.get("tests_executed") is True and evidence.get("tests_passed") is True
        framework = _compact(evidence.get("detected_test_framework"), 100) or "project tests"
        if tests_passed:
            add(
                "test_pattern", "QA",
                f"Validated test pattern for {project_id}: {framework} completed successfully "
                f"with result {_compact(evidence.get('final_test_result_summary'), 500)}.",
                .95,
                [{"kind": "test_result", "value": _compact(evidence.get("final_test_result_summary"), 500)},
                 {"kind": "test_command", "value": evidence.get("actual_test_command") or evidence.get("expected_test_command")}],
            )

        generated = [str(item).replace("\\", "/") for item in evidence.get("generated_files") or []]
        if evidence.get("implementation_valid") is True and generated and tests_passed:
            add(
                "code_pattern", "Developer",
                f"Validated implementation pattern for {project_id}: the files "
                f"{', '.join(generated[:20])} passed {framework} checks.",
                .90,
                [{"kind": "file_path", "value": path} for path in generated[:20]]
                + [{"kind": "test_result", "value": _compact(evidence.get("final_test_result_summary"), 500)}],
            )

        analysis = evidence.get("requirement_analysis")
        constraints = analysis.get("constraints") if isinstance(analysis, Mapping) else None
        if constraints and evidence.get("planning_valid") is True and evidence.get("last_approved_tool"):
            add(
                "decision", "Planner",
                f"Approved project constraints for {project_id}: {_compact(constraints, 1200)}.",
                1.0,
                [{"kind": "approval", "value": evidence.get("last_approved_tool")}],
            )

        if evidence.get("environment_prepared") is True and tests_passed:
            add(
                "workflow_learning", "Workflow",
                f"For {project_id}, prepare the environment before executing {framework}; "
                f"the validated run completed with {_compact(evidence.get('final_test_result_summary'), 500)}.",
                .80,
                [{"kind": "workflow_stage", "value": "prepare_environment"},
                 {"kind": "test_result", "value": _compact(evidence.get("final_test_result_summary"), 500)}],
                "heuristic",
            )
        return candidates


def structured_learning_evidence(state: SoftwareFactoryState) -> dict[str, Any]:
    keys = {
        "terminal_status", "requirement_analysis", "planning_valid", "last_approved_tool",
        "implementation_valid", "generated_files", "environment_prepared",
        "tests_executed", "tests_passed", "detected_test_framework", "actual_test_command",
        "expected_test_command", "final_test_result_summary", "failure_message",
        "test_failure_summary", "failing_test_files", "repair_phase", "repair_attempts",
        "repair_decision", "repair_before", "repair_after", "files_updated_during_repair",
        "planner_knowledge_sources", "developer_knowledge_sources", "qa_knowledge_sources",
        "repair_knowledge_sources",
    }
    return {key: state.get(key) for key in keys}


def learning_context(state: SoftwareFactoryState) -> tuple[str, str, str, str | None]:
    context = get_observability_context()
    event_thread_id, lineage = get_workflow_event_context()
    workflow_id = str(context.workflow_id or event_thread_id or state.get("workflow_id") or "unknown-workflow")
    branch_id = str(lineage.get("branch_id") or context.branch_id or "original")
    project_id = str(state.get("created_project_name") or state.get("project_name") or "")
    return workflow_id, branch_id, project_id, context.trace_id


async def _metric(dependencies: Any, name: str, value: int, **attributes: Any) -> None:
    observability = getattr(dependencies, "observability", None)
    store = getattr(observability, "store", None)
    if observability is None or store is None or not hasattr(store, "add_metric"):
        return
    context = get_observability_context()
    await observability._safe(store.add_metric, {
        "trace_id": context.trace_id,
        "workflow_id": context.workflow_id,
        "name": name,
        "value": value,
        "unit": "count",
        "timestamp": datetime.now(UTC).isoformat(),
        "attributes": {"branch_id": context.branch_id, **attributes},
    })


@asynccontextmanager
async def _span(dependencies: Any, name: str):
    observability = getattr(dependencies, "observability", None)
    if observability is None or not hasattr(observability, "span"):
        yield
        return
    async with observability.span(name, category="workflow_learning"):
        yield


async def extract_workflow_learnings_node(
    state: SoftwareFactoryState, dependencies: Any
) -> dict[str, Any]:
    async with _span(dependencies, "workflow.learning.extract"):
        return await _extract_workflow_learnings(state, dependencies)


async def _extract_workflow_learnings(
    state: SoftwareFactoryState, dependencies: Any
) -> dict[str, Any]:
    workflow_id, branch_id, project_id, _trace_id = learning_context(state)
    _event_thread_id, lineage = get_workflow_event_context()
    if str(lineage.get("lineage") or "").startswith("replay"):
        return {}
    existing = list(state.get("workflow_learning_candidates") or [])
    if existing and all(item.get("branch_id") == branch_id for item in existing):
        return {}
    if not project_id:
        return {"workflow_learning_state": "unavailable"}
    try:
        extractor = WorkflowLearningExtractor()
        candidates = [item.to_dict() for item in extractor.extract(
            workflow_id=workflow_id,
            branch_id=branch_id,
            project_id=project_id,
            evidence=structured_learning_evidence(state),
        )]
        store = getattr(dependencies, "workflow_learning_store", None)
        if store is not None and candidates:
            await store.save_candidates(candidates)
    except Exception:
        await _metric(dependencies, "learning_submission_failures", 1, stage="extract")
        return {"workflow_learning_state": "unavailable"}
    await _metric(dependencies, "learning_candidates_extracted", len(candidates))
    return {
        "workflow_learning_candidates": candidates,
        "workflow_learning_submission_results": [],
        "workflow_learning_state": "extracted",
    }


def _submission_status(payload: Mapping[str, Any]) -> str:
    if payload.get("duplicate") or payload.get("duplicate_of"):
        return "duplicate"
    status = str(payload.get("status") or "candidate")
    return status if status in {"candidate", "indexed", "rejected"} else "candidate"


async def submit_learning_candidate(
    candidate: Mapping[str, Any], dependencies: Any, state: SoftwareFactoryState
) -> dict[str, Any]:
    _workflow_id, _branch_id, _project_id, trace_id = learning_context(state)
    arguments = {
        "project_id": candidate["project_id"],
        "workflow_id": candidate["workflow_id"],
        "agent_name": candidate["agent_name"],
        "knowledge_type": candidate["knowledge_type"],
        "content": candidate["content"],
        "source_reference": candidate["source_reference"],
        "metadata": {
            "origin": "workflow_learning_extractor",
            "candidate_id": candidate["candidate_id"],
            "confidence": candidate["confidence"],
            "extraction_method": candidate["extraction_method"],
            "evidence_refs": candidate["evidence_refs"],
            "branch_id": candidate["branch_id"],
            "trace_id": trace_id,
            "knowledge_provenance_ids": candidate.get("knowledge_provenance_ids", []),
            "origin_checkpoint": state.get("fork_origin_checkpoint_id"),
        },
    }
    outcome = await dependencies.tool_executor.execute(
        "knowledge__submit_learning", arguments,
        state_context=state, approval_mode="already_approved",
    )
    if outcome.is_error or not isinstance(outcome.payload, Mapping):
        raise RuntimeError("Knowledge MCP submission failed")
    payload = dict(outcome.payload)
    return {
        "candidate_id": candidate["candidate_id"],
        "submission_status": _submission_status(payload),
        "knowledge_id": payload.get("knowledge_id"),
        "duplicate_of": payload.get("duplicate_of"),
        "retryable": False,
        "submitted_at": datetime.now(UTC).isoformat(),
    }


async def submit_workflow_learnings_node(
    state: SoftwareFactoryState, dependencies: Any
) -> dict[str, Any]:
    async with _span(dependencies, "workflow.learning.submit"):
        return await _submit_workflow_learnings(state, dependencies)


async def _submit_workflow_learnings(
    state: SoftwareFactoryState, dependencies: Any
) -> dict[str, Any]:
    candidates = list(state.get("workflow_learning_candidates") or [])
    previous = {
        item.get("candidate_id"): dict(item)
        for item in state.get("workflow_learning_submission_results") or []
        if isinstance(item, Mapping) and item.get("candidate_id")
    }
    results: list[dict[str, Any]] = []
    failures = 0
    store = getattr(dependencies, "workflow_learning_store", None)
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id")
        old = previous.get(candidate_id)
        if old and old.get("submission_status") in {"candidate", "indexed", "duplicate", "rejected"}:
            results.append(old)
            continue
        try:
            result = await submit_learning_candidate(candidate, dependencies, state)
        except Exception as exc:
            failures += 1
            result = {
                "candidate_id": candidate_id,
                "submission_status": "submission_failed",
                "knowledge_id": None,
                "retryable": True,
                "failure_type": type(exc).__name__,
                "failure_message": str(exc)[:500],
            }
        results.append(result)
        if store is not None:
            try:
                await store.record_submission(str(candidate_id), result["submission_status"], result)
            except Exception:
                failures += 1
                result = {**result, "submission_status": "submission_failed", "retryable": True,
                          "failure_type": "WorkflowLearningStoreError"}
                results[-1] = result
    submitted = sum(item.get("submission_status") in {"candidate", "indexed", "duplicate", "rejected"} for item in results)
    rejected = sum(item.get("submission_status") == "rejected" for item in results)
    duplicates = sum(item.get("submission_status") == "duplicate" for item in results)
    await _metric(dependencies, "learning_candidates_submitted", submitted)
    await _metric(dependencies, "learning_candidates_rejected", rejected)
    await _metric(dependencies, "learning_duplicates", duplicates)
    await _metric(dependencies, "learning_submission_failures", failures)
    learning_state = "unavailable" if candidates and failures == len(candidates) else "partial" if failures else "submitted"
    return {
        "workflow_learning_submission_results": results,
        "workflow_learning_state": learning_state,
    }
