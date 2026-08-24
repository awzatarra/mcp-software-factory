from __future__ import annotations

import hashlib
import json
from time import perf_counter
from typing import Any

from api.models import WorkflowSnapshotResponse
from api.services.planner_policy_proposal_store import PlannerPolicyProposalStore
from api.services.planner_policy_runtime_store import PlannerPolicyRuntimeStore
from api.services.planner_recommendation_review_store import PlannerRecommendationReviewStore
from api.services.workflow_metadata_store import WorkflowMetadataStore
from api.services.workflow_query_service import WorkflowQueryService
from graph.agent_recommendations import (
    AGENT_RECOMMENDATION_VERSION,
    DEFAULT_MIN_SAMPLE_SIZE as DEFAULT_AGENT_RECOMMENDATION_MIN_SAMPLE_SIZE,
    DEFAULT_SEGMENT_MIN_SAMPLE_SIZE as DEFAULT_AGENT_RECOMMENDATION_SEGMENT_MIN_SAMPLE_SIZE,
    analyze_agent_recommendations,
)
from graph.planner_calibration import (
    PlannerPolicySnapshot,
    analyze_planner_calibration,
)
from graph.planner_evaluation import PLANNING_EVALUATION_VERSION
from graph.planner_policy_proposals import (
    PlannerPolicyProposalError,
    build_policy_change_proposal,
    validate_proposal_against_snapshot,
)
from graph.planner_policy_portfolio import (
    evaluate_portfolio_conflicts,
)
from graph.planner_policy_registry import (
    POLICY_REGISTRY_VERSION,
    PlannerPolicyRegistryError,
    metadata_for,
    semantic_verify,
    validate_policy_value,
)
from graph.planner_policy_experiment import (
    DEFAULT_GUARDRAILS,
    POLICY_EXPERIMENT_ASSIGNMENT_ALGORITHM_VERSION,
    assign_experiment_variant,
    evaluate_experiment_result,
    evaluate_promotion_readiness,
    experiment_bucket,
    experiment_fingerprint,
    experiment_group_metrics,
    metric_direction,
    split_experiment_records,
    validate_allocation,
)
from graph.planner_policy_rollout import (
    DEFAULT_MINIMUM_BASELINE_SAMPLE_SIZE,
    DEFAULT_MINIMUM_STAGE_SAMPLE_SIZE,
    POLICY_ROLLOUT_ASSIGNMENT_ALGORITHM_VERSION,
    evaluate_rollout_health,
    is_workflow_in_rollout,
    parse_rollout_stages,
    rollout_fingerprint,
    rollout_metrics,
    split_rollout_records,
    stable_rollout_bucket,
)
from graph.planner_recommendation_governance import governance_recommendation
from streaming import EventStatus, WorkflowEventType, emit_workflow_event


class PlannerRecommendationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PlannerCalibrationService:
    def __init__(
        self,
        *,
        query: WorkflowQueryService,
        metadata_store: WorkflowMetadataStore | None = None,
        review_store: PlannerRecommendationReviewStore | None = None,
        proposal_store: PlannerPolicyProposalStore | None = None,
        policy_runtime_store: PlannerPolicyRuntimeStore | None = None,
    ) -> None:
        self.query = query
        self.metadata_store = metadata_store
        self.review_store = review_store
        self.proposal_store = proposal_store
        self.policy_runtime_store = policy_runtime_store

    async def _thread_ids(self, *, limit: int) -> list[str]:
        if self.metadata_store is None:
            return []
        listed = await self.metadata_store.list(
            status=None,
            search=None,
            limit=limit,
            offset=0,
            sort_by="updated_at",
            sort_order="desc",
        )
        return [item.thread_id for item in listed.items]

    @staticmethod
    def _record(snapshot: WorkflowSnapshotResponse) -> dict[str, Any] | None:
        planning = snapshot.planning or {}
        evaluation = planning.get("evaluation")
        if not isinstance(evaluation, dict):
            return None
        quality = planning.get("quality") if isinstance(planning.get("quality"), dict) else {}
        risk = planning.get("risk") if isinstance(planning.get("risk"), dict) else {}
        approval = planning.get("approval") if isinstance(planning.get("approval"), dict) else {}
        analysis = planning.get("analysis") if isinstance(planning.get("analysis"), dict) else {}
        framework = planning.get("framework") or analysis.get("framework")
        return {
            "workflow_id": snapshot.thread_id,
            "created_at": snapshot.created_at,
            "updated_at": snapshot.updated_at,
            "framework": framework,
            "risk_level": risk.get("level"),
            "quality_level": quality.get("level"),
            "approval_required": approval.get("required"),
            "planning_evaluation": evaluation,
            "planning_evaluation_version": evaluation.get("version"),
        }

    @staticmethod
    def _agent_record(snapshot: WorkflowSnapshotResponse) -> dict[str, Any] | None:
        agent_performance = snapshot.agent_performance or {}
        failure_attribution = snapshot.failure_attribution or {}
        if not agent_performance and not failure_attribution:
            return None
        planning = snapshot.planning or {}
        implementation = snapshot.implementation or {}
        testing = snapshot.testing or {}
        analysis = planning.get("analysis") if isinstance(planning.get("analysis"), dict) else {}
        return {
            "workflow_id": snapshot.thread_id,
            "created_at": snapshot.created_at,
            "updated_at": snapshot.updated_at,
            "framework": planning.get("framework") or implementation.get("framework") or analysis.get("framework"),
            "terminal_status": snapshot.terminal_status,
            "planning": planning,
            "implementation": implementation,
            "testing": testing,
            "agent_performance_evaluations": agent_performance,
            "failure_attribution": failure_attribution,
            "planning_evaluation": planning.get("evaluation") if isinstance(planning.get("evaluation"), dict) else {},
            "planning_judge_result": (planning.get("judge") or {}).get("result") if isinstance(planning.get("judge"), dict) else {},
            "planning_hybrid_evaluation": planning.get("hybrid_evaluation") if isinstance(planning.get("hybrid_evaluation"), dict) else {},
            "repair_attempts": testing.get("repair_attempts"),
        }

    async def records(
        self,
        *,
        limit: int = 200,
        version: str = PLANNING_EVALUATION_VERSION,
        framework: str | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for thread_id in await self._thread_ids(limit=limit):
            try:
                snapshot = await self.query.get_snapshot(thread_id)
            except Exception:
                continue
            record = self._record(snapshot)
            if record is None:
                continue
            if version and record.get("planning_evaluation_version") != version:
                continue
            if framework and str(record.get("framework") or "").casefold() != framework.casefold():
                continue
            records.append(record)
        return records

    async def agent_records(
        self,
        *,
        limit: int = 500,
        framework: str | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for thread_id in await self._thread_ids(limit=limit):
            try:
                snapshot = await self.query.get_snapshot(thread_id)
            except Exception:
                continue
            record = self._agent_record(snapshot)
            if record is None:
                continue
            if framework and str(record.get("framework") or "").casefold() != framework.casefold():
                continue
            records.append(record)
        return records

    async def analyze(
        self,
        *,
        limit: int = 200,
        version: str = PLANNING_EVALUATION_VERSION,
        framework: str | None = None,
        minimum_sample_size: int = 20,
        segment_min_sample_size: int = 10,
    ) -> dict[str, Any]:
        started = perf_counter()
        emit_workflow_event(
            WorkflowEventType.STAGE_STARTED,
            source="planner_calibration",
            stage="planner_calibration",
            status=EventStatus.RUNNING,
            data={
                "event": "planner_calibration_analysis_started",
                "limit": limit,
                "version": version,
                "framework": framework,
            },
        )
        records = await self.records(limit=limit, version=version, framework=framework)
        analytics = analyze_planner_calibration(
            records,
            PlannerPolicySnapshot(),
            compatible_version=version,
            minimum_sample_size=minimum_sample_size,
            segment_min_sample_size=segment_min_sample_size,
        )
        duration_ms = round((perf_counter() - started) * 1000, 3)
        await self._enrich_recommendations(analytics)
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="planner_calibration",
            stage="planner_calibration",
            status=EventStatus.COMPLETED,
            data={
                "event": "planner_calibration_analysis_completed",
                "sample_size": analytics["sample_size"],
                "recommendation_count": len(analytics["recommendations"]),
                "status": analytics["status"],
                "duration_ms": duration_ms,
                "version": analytics["version"],
            },
        )
        analytics["duration_ms"] = duration_ms
        return analytics

    async def _enrich_recommendations(self, analytics: dict[str, Any]) -> None:
        version = str(analytics.get("version") or "")
        enriched: list[dict[str, Any]] = []
        counters = {
            "recommendations_total": 0,
            "recommendations_unreviewed": 0,
            "recommendations_under_review": 0,
            "recommendations_accepted": 0,
            "recommendations_rejected": 0,
            "recommendations_deferred": 0,
        }
        for recommendation in analytics.get("recommendations") or []:
            item = governance_recommendation(recommendation, analytics_version=version)
            review = await self._review_for(item)
            item["review"] = review
            item["application_status"] = review.get("application_status") or (
                "not_applied" if review.get("status") == "accepted" else "not_applicable"
            )
            status = str(review.get("status") or "recommendation_only")
            item["review_status"] = status
            counters["recommendations_total"] += 1
            if status == "recommendation_only":
                counters["recommendations_unreviewed"] += 1
            elif status == "under_review":
                counters["recommendations_under_review"] += 1
            elif status == "accepted":
                counters["recommendations_accepted"] += 1
            elif status == "rejected":
                counters["recommendations_rejected"] += 1
            elif status == "deferred":
                counters["recommendations_deferred"] += 1
            enriched.append(item)
        total = counters["recommendations_total"]
        analytics["recommendations"] = enriched
        analytics["review_metrics"] = {
            **counters,
            "acceptance_rate": round(counters["recommendations_accepted"] / total, 4) if total else 0.0,
            "rejection_rate": round(counters["recommendations_rejected"] / total, 4) if total else 0.0,
        }

    async def analyze_agent_recommendations(
        self,
        *,
        limit: int = 500,
        agent: str | None = None,
        framework: str | None = None,
        minimum_sample_size: int = DEFAULT_AGENT_RECOMMENDATION_MIN_SAMPLE_SIZE,
        segment_min_sample_size: int = DEFAULT_AGENT_RECOMMENDATION_SEGMENT_MIN_SAMPLE_SIZE,
    ) -> dict[str, Any]:
        started = perf_counter()
        emit_workflow_event(
            WorkflowEventType.STAGE_STARTED,
            source="agent_recommendations",
            stage="agent_recommendations",
            status=EventStatus.RUNNING,
            data={
                "event": "agent_recommendation_analysis_started",
                "agent": agent,
                "framework": framework,
                "version": AGENT_RECOMMENDATION_VERSION,
            },
        )
        records = await self.agent_records(limit=limit, framework=framework)
        analytics = analyze_agent_recommendations(
            records,
            minimum_sample_size=minimum_sample_size,
            segment_min_sample_size=segment_min_sample_size,
            agent_filter=agent,
            framework=framework,
        )
        await self._enrich_recommendations(analytics)
        duration_ms = round((perf_counter() - started) * 1000, 3)
        for recommendation in analytics.get("recommendations") or []:
            emit_workflow_event(
                WorkflowEventType.STAGE_COMPLETED,
                source="agent_recommendations",
                stage="agent_recommendations",
                status=EventStatus.COMPLETED,
                data={
                    "event": "agent_recommendation_created",
                    "agent": recommendation.get("agent"),
                    "sample_size": ((recommendation.get("evidence") or {}).get("sample_size")),
                    "version": analytics["version"],
                },
            )
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="agent_recommendations",
            stage="agent_recommendations",
            status=EventStatus.COMPLETED,
            data={
                "event": "agent_recommendation_analysis_completed",
                "sample_size": analytics["sample_size"],
                "recommendation_count": len(analytics["recommendations"]),
                "status": analytics["status"],
                "duration_ms": duration_ms,
                "version": analytics["version"],
            },
        )
        analytics["duration_ms"] = duration_ms
        return analytics

    async def _review_for(self, recommendation: dict[str, Any]) -> dict[str, Any]:
        if self.review_store is None:
            return {"status": "recommendation_only"}
        row = await self.review_store.get(
            str(recommendation["recommendation_id"]),
            str(recommendation["recommendation_fingerprint"]),
        )
        if row is None:
            return {"status": "recommendation_only"}
        return {
            "status": row["review_status"],
            "reviewer": row.get("reviewer"),
            "review_notes": row.get("review_notes"),
            "reviewed_at": row.get("reviewed_at"),
            "decision_reason": row.get("decision_reason"),
            "deferred_until": row.get("deferred_until"),
            "review_version": row.get("review_version"),
            "application_status": row.get("application_status"),
        }

    async def agent_recommendations(
        self,
        *,
        agent: str | None = None,
        framework: str | None = None,
        status: str | None = None,
        severity: str | None = None,
        limit: int = 100,
        minimum_sample_size: int = DEFAULT_AGENT_RECOMMENDATION_MIN_SAMPLE_SIZE,
    ) -> dict[str, Any]:
        analytics = await self.analyze_agent_recommendations(
            limit=max(limit, 500),
            agent=agent,
            framework=framework,
            minimum_sample_size=minimum_sample_size,
        )
        items = list(analytics.get("recommendations") or [])
        if status:
            items = [item for item in items if (item.get("review") or {}).get("status") == status]
        if severity:
            items = [item for item in items if item.get("severity") == severity]
        return {
            "analysis": analytics,
            "items": items[:limit],
            "review_metrics": analytics.get("review_metrics") or {},
            "analytics_version": analytics.get("version"),
        }

    async def agent_recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        analytics = await self.analyze_agent_recommendations()
        for item in analytics.get("recommendations") or []:
            if item.get("recommendation_id") == recommendation_id:
                if self.review_store is not None:
                    item["history"] = await self.review_store.history(
                        recommendation_id,
                        str(item.get("recommendation_fingerprint") or ""),
                    )
                else:
                    item["history"] = []
                return item
        return None

    async def review_agent_recommendation(
        self,
        recommendation_id: str,
        *,
        action: str,
        reviewer: str,
        fingerprint: str,
        notes: str | None = None,
        reason: str | None = None,
        deferred_until: str | None = None,
    ) -> dict[str, Any]:
        if self.review_store is None:
            raise PlannerRecommendationError("planner_recommendation_review_store_unavailable")
        recommendation = await self.agent_recommendation(recommendation_id)
        if recommendation is None:
            raise PlannerRecommendationError("planner_recommendation_not_found")
        if fingerprint != recommendation.get("recommendation_fingerprint"):
            self._emit_agent_review_event("agent_recommendation_review_stale", recommendation, reviewer, "stale")
            raise PlannerRecommendationError("planner_recommendation_stale")
        target = {
            "start": "under_review",
            "accept": "accepted",
            "reject": "rejected",
            "defer": "deferred",
        }.get(action)
        if target is None:
            raise PlannerRecommendationError("planner_recommendation_review_transition_not_allowed")
        try:
            row, changed = await self.review_store.transition(
                recommendation,
                to_status=target,
                reviewer=reviewer,
                notes=notes,
                reason=reason,
                deferred_until=deferred_until,
            )
        except ValueError as exc:
            raise PlannerRecommendationError(str(exc)) from exc
        if changed:
            self._emit_agent_review_event(
                {
                    "under_review": "agent_recommendation_review_started",
                    "accepted": "agent_recommendation_accepted",
                    "rejected": "agent_recommendation_rejected",
                    "deferred": "agent_recommendation_deferred",
                }[target],
                recommendation,
                reviewer,
                target,
            )
        recommendation["review"] = await self._review_for(recommendation)
        recommendation["history"] = await self.review_store.history(recommendation_id, fingerprint)
        recommendation["application_status"] = recommendation["review"].get("application_status")
        return recommendation

    async def recommendations(
        self,
        *,
        status: str | None = None,
        policy: str | None = None,
        severity: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        analytics = await self.analyze(limit=max(limit, 200))
        items = list(analytics.get("recommendations") or [])
        if status:
            items = [item for item in items if (item.get("review") or {}).get("status") == status]
        if policy:
            items = [item for item in items if item.get("policy") == policy]
        if severity:
            items = [item for item in items if item.get("severity") == severity]
        items.extend(await self._experiment_winner_recommendations())
        return {
            "items": items[:limit],
            "review_metrics": analytics.get("review_metrics") or {},
            "analytics_version": analytics.get("version"),
        }

    async def recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        for item in await self._experiment_winner_recommendations():
            if item.get("recommendation_id") == recommendation_id:
                item["history"] = []
                return item
        analytics = await self.analyze()
        for item in analytics.get("recommendations") or []:
            if item.get("recommendation_id") == recommendation_id:
                if self.review_store is not None:
                    item["history"] = await self.review_store.history(
                        recommendation_id,
                        str(item.get("recommendation_fingerprint") or ""),
                    )
                else:
                    item["history"] = []
                return item
        return None

    async def _experiment_winner_recommendations(self) -> list[dict[str, Any]]:
        if self.policy_runtime_store is None:
            return []
        recommendations: list[dict[str, Any]] = []
        for experiment in await self.policy_runtime_store.list_experiments(status="completed", policy_key=None, limit=100):
            experiment = self._with_experiment_summaries(experiment)
            readiness = experiment.get("promotion_readiness")
            if not isinstance(readiness, dict) or readiness.get("status") != "ready" or not experiment.get("winner_variant_id"):
                continue
            variants = {str(item["variant_id"]): item for item in experiment.get("variants") or [] if isinstance(item, dict)}
            winner = variants.get(str(experiment["winner_variant_id"]))
            if winner is None:
                continue
            comparisons = ((experiment.get("metrics") or {}).get("comparison") or {}).get("comparisons") or {}
            comparison = comparisons.get(str(experiment["winner_variant_id"])) or {}
            statistical_summary = experiment.get("statistical_summary") if isinstance(experiment.get("statistical_summary"), dict) else {}
            payload = {
                "experiment_id": experiment["experiment_id"],
                "winner_variant_id": experiment["winner_variant_id"],
                "policy_key": experiment["policy_key"],
                "control_value": experiment["control_value"],
                "winner_value": winner.get("value"),
                "primary_metric": experiment["primary_metric"],
                "effect_size": readiness.get("effect_size") or comparison.get("improvement"),
                "decision_confidence": readiness.get("decision_confidence") or statistical_summary.get("decision_confidence"),
                "readiness_score": readiness.get("score"),
                "readiness_confidence": readiness.get("confidence"),
                "guardrail_summary": {
                    "status": comparison.get("guardrail_status"),
                    "violations": comparison.get("guardrail_violations") or [],
                    "margin": readiness.get("guardrail_margin"),
                },
                "sample_size": comparison.get("sample_size"),
                "sample_sizes": {
                    "control": ((experiment.get("metrics") or {}).get("control") or {}).get("attributable_sample_size"),
                    "winner": comparison.get("sample_size"),
                },
                "statistical_analysis_version": statistical_summary.get("analysis_version"),
                "promotion_readiness_version": readiness.get("version"),
                "readiness_status": readiness.get("status"),
                "readiness_reason_codes": readiness.get("reason_codes") or [],
                "experiment_fingerprint": experiment.get("experiment_fingerprint"),
            }
            fingerprint_payload = {
                "type": "policy_experiment_promotion",
                "experiment_fingerprint": experiment.get("experiment_fingerprint"),
                "winner_variant_id": experiment.get("winner_variant_id"),
                "promotion_readiness": readiness,
            }
            encoded = json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            fingerprint = hashlib.sha256(encoded).hexdigest()
            recommendation = {
                "recommendation_id": f"experiment-promotion-{experiment['experiment_id'][:16]}",
                "analytics_version": str(readiness.get("version") or "7.15-v1"),
                "recommendation_fingerprint": fingerprint,
                "fingerprint": fingerprint,
                "type": "policy_experiment_promotion",
                "policy": "policy_experiment_promotion",
                "segment": experiment.get("scope"),
                "direction": "decrease" if metric_direction(str(experiment["primary_metric"])) == "lower_is_better" else "increase",
                "severity": "medium" if int(readiness.get("score") or 0) < 90 else "low",
                "confidence": float(readiness.get("confidence") or 0),
                "reason_codes": list(readiness.get("reason_codes") or ["promotion_ready"]),
                "evidence": payload,
                "current_value": experiment["control_value"],
                "suggested_value": {"variant_id": experiment["winner_variant_id"], "value": winner.get("value"), "suggested_value": winner.get("value")},
                "status": "recommendation_only",
                "application_status": "not_applied",
                "review": {"status": "recommendation_only"},
            }
            review = await self._review_for(recommendation)
            recommendation["review"] = review
            recommendation["status"] = review.get("status") or "recommendation_only"
            recommendation["application_status"] = review.get("application_status") or ("not_applied" if recommendation["status"] == "accepted" else "not_applied")
            recommendations.append(
                recommendation
            )
        return recommendations

    async def review(
        self,
        recommendation_id: str,
        *,
        action: str,
        reviewer: str,
        fingerprint: str,
        notes: str | None = None,
        reason: str | None = None,
        deferred_until: str | None = None,
    ) -> dict[str, Any]:
        if self.review_store is None:
            raise PlannerRecommendationError("planner_recommendation_review_store_unavailable")
        recommendation = await self.recommendation(recommendation_id)
        if recommendation is None:
            raise PlannerRecommendationError("planner_recommendation_not_found")
        if fingerprint != recommendation.get("recommendation_fingerprint"):
            self._emit_review_event("planner_recommendation_review_stale", recommendation, reviewer, "stale")
            raise PlannerRecommendationError("planner_recommendation_stale")
        target = {
            "start": "under_review",
            "accept": "accepted",
            "reject": "rejected",
            "defer": "deferred",
        }.get(action)
        if target is None:
            raise PlannerRecommendationError("planner_recommendation_review_transition_not_allowed")
        try:
            row, changed = await self.review_store.transition(
                recommendation,
                to_status=target,
                reviewer=reviewer,
                notes=notes,
                reason=reason,
                deferred_until=deferred_until,
            )
        except ValueError as exc:
            raise PlannerRecommendationError(str(exc)) from exc
        event_name = {
            "under_review": "planner_recommendation_review_started",
            "accepted": "planner_recommendation_accepted",
            "rejected": "planner_recommendation_rejected",
            "deferred": "planner_recommendation_deferred",
        }[target]
        if changed:
            self._emit_review_event(event_name, recommendation, reviewer, target)
        recommendation["review"] = await self._review_for(recommendation)
        recommendation["history"] = await self.review_store.history(recommendation_id, fingerprint)
        recommendation["application_status"] = recommendation["review"].get("application_status")
        return recommendation

    @staticmethod
    def _emit_review_event(
        event_name: str,
        recommendation: dict[str, Any],
        reviewer: str,
        review_status: str,
    ) -> None:
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="planner_recommendation_review",
            stage="planner_recommendation_review",
            status=EventStatus.COMPLETED,
            data={
                "event": event_name,
                "recommendation_id": recommendation.get("recommendation_id"),
                "policy": recommendation.get("policy"),
                "severity": recommendation.get("severity"),
                "review_status": review_status,
                "reviewer": reviewer,
                "fingerprint": recommendation.get("recommendation_fingerprint"),
            },
        )

    @staticmethod
    def _emit_agent_review_event(
        event_name: str,
        recommendation: dict[str, Any],
        reviewer: str,
        review_status: str,
    ) -> None:
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="agent_recommendation_review",
            stage="agent_recommendation_review",
            status=EventStatus.COMPLETED,
            data={
                "event": event_name,
                "recommendation_id": recommendation.get("recommendation_id"),
                "agent": recommendation.get("agent"),
                "type": recommendation.get("type"),
                "severity": recommendation.get("severity"),
                "review_status": review_status,
                "reviewer": reviewer,
                "fingerprint": recommendation.get("recommendation_fingerprint"),
            },
        )

    async def create_policy_proposal(self, recommendation_id: str) -> dict[str, Any]:
        if self.proposal_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        recommendation = await self.recommendation(recommendation_id)
        if recommendation is None:
            raise PlannerPolicyProposalError("planner_recommendation_not_found")
        review = recommendation.get("review") or {}
        if review.get("status") != "accepted":
            raise PlannerPolicyProposalError("planner_policy_proposal_source_not_accepted")
        fingerprint = str(recommendation.get("recommendation_fingerprint") or "")
        if self.review_store is not None:
            accepted = await self.review_store.get(recommendation_id, fingerprint)
            if accepted is None or accepted.get("review_status") != "accepted":
                raise PlannerPolicyProposalError("planner_policy_proposal_source_stale")
            history = await self.review_store.history(recommendation_id, fingerprint)
            source_review_id = next(
                (item["review_id"] for item in reversed(history) if item.get("to_status") == "accepted"),
                None,
            )
        else:
            source_review_id = None
        records = await self.records(limit=1000)
        draft = build_policy_change_proposal(
            {**recommendation, "source_review_id": source_review_id},
            PlannerPolicySnapshot(),
            records=records,
        )
        draft["source_review_id"] = source_review_id
        proposal, created = await self.proposal_store.create(draft)
        if created:
            self._emit_proposal_event("planner_policy_proposal_created", proposal, None, "draft")
        return proposal

    async def policy_proposals(
        self,
        *,
        status: str | None = None,
        policy_key: str | None = None,
        risk_level: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if self.proposal_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        return {"items": await self.proposal_store.list(status=status, policy_key=policy_key, risk_level=risk_level, limit=limit)}

    async def policy_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        if self.proposal_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        proposal = await self.proposal_store.get(proposal_id)
        if proposal is None:
            return None
        proposal["history"] = await self.proposal_store.history(proposal_id)
        return proposal

    async def transition_policy_proposal(
        self,
        proposal_id: str,
        *,
        action: str,
        actor: str,
        fingerprint: str,
        notes: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if self.proposal_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        proposal = await self.proposal_store.get(proposal_id)
        if proposal is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_not_found")
        try:
            validate_proposal_against_snapshot(proposal, PlannerPolicySnapshot())
        except PlannerPolicyProposalError:
            self._emit_proposal_event("planner_policy_proposal_stale", proposal, actor, proposal.get("status"))
            raise
        target = {
            "ready": "ready_for_review",
            "approve": "approved",
            "reject": "rejected",
            "cancel": "cancelled",
        }.get(action)
        if target is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_transition_not_allowed")
        try:
            updated, changed = await self.proposal_store.transition(
                proposal_id,
                to_status=target,
                actor=actor,
                fingerprint=fingerprint,
                notes=notes,
                reason=reason,
            )
        except ValueError as exc:
            raise PlannerPolicyProposalError(str(exc)) from exc
        if changed:
            event_name = {
                "ready_for_review": "planner_policy_proposal_ready",
                "approved": "planner_policy_proposal_approved",
                "rejected": "planner_policy_proposal_rejected",
                "cancelled": "planner_policy_proposal_cancelled",
            }[target]
            self._emit_proposal_event(event_name, updated, actor, target)
        updated["history"] = await self.proposal_store.history(proposal_id)
        return updated

    @staticmethod
    def _emit_proposal_event(
        event_name: str,
        proposal: dict[str, Any],
        actor: str | None,
        status: Any,
    ) -> None:
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="planner_policy_proposal",
            stage="planner_policy_proposal",
            status=EventStatus.COMPLETED,
            data={
                "event": event_name,
                "proposal_id": proposal.get("proposal_id"),
                "policy_key": proposal.get("policy_key"),
                "change_type": proposal.get("change_type"),
                "risk_level": proposal.get("proposal_risk_level"),
                "source_recommendation_id": proposal.get("source_recommendation_id"),
                "actor": actor,
                "proposal_status": status,
            },
        )

    @staticmethod
    def _application_fingerprint(*, proposal: dict[str, Any], previous_value: Any) -> str:
        payload = {
            "proposal_fingerprint": proposal.get("proposal_fingerprint"),
            "policy_key": proposal.get("policy_key"),
            "scope": proposal.get("policy_scope"),
            "previous_value": previous_value,
            "proposed_value": proposal.get("proposed_value"),
            "registry_version": POLICY_REGISTRY_VERSION,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def prepare_policy_application(
        self,
        proposal_id: str,
        *,
        actor: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if self.proposal_store is None or self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        proposal = await self.proposal_store.get(proposal_id)
        if proposal is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_not_found")
        if proposal.get("status") != "approved":
            raise PlannerPolicyProposalError("planner_policy_application_not_approved")
        if proposal.get("application_status") != "not_applied":
            raise PlannerPolicyProposalError("planner_policy_application_conflict")
        if proposal.get("change_type") == "review_only" or proposal.get("proposed_value") is None:
            raise PlannerPolicyProposalError("planner_policy_application_not_applicable")
        source = await self.recommendation(str(proposal["source_recommendation_id"]))
        if (
            source is None
            or source.get("recommendation_fingerprint") != proposal.get("source_recommendation_fingerprint")
            or (source.get("review") or {}).get("status") != "accepted"
        ):
            raise PlannerPolicyProposalError("planner_policy_application_stale")
        try:
            metadata = metadata_for(str(proposal["policy_key"]))
            validate_policy_value(str(proposal["policy_key"]), proposal.get("proposed_value"))
            validate_proposal_against_snapshot(proposal, PlannerPolicySnapshot())
        except PlannerPolicyRegistryError as exc:
            raise PlannerPolicyProposalError(exc.code) from exc
        runtime = await self.policy_runtime_store.get_policy(str(proposal["policy_key"]), str(proposal["policy_scope"]))
        if runtime["value"] != proposal.get("current_value"):
            raise PlannerPolicyProposalError("planner_policy_application_stale")
        application_fingerprint = self._application_fingerprint(proposal=proposal, previous_value=runtime["value"])
        application, created = await self.policy_runtime_store.create_application(
            {
                "proposal_id": proposal["proposal_id"],
                "proposal_fingerprint": proposal["proposal_fingerprint"],
                "application_fingerprint": application_fingerprint,
                "policy_key": proposal["policy_key"],
                "scope": proposal["policy_scope"],
                "previous_value": runtime["value"],
                "proposed_value": proposal["proposed_value"],
                "baseline_revision": runtime["revision"],
                "application_risk_level": proposal["proposal_risk_level"],
                "actor": actor,
                "notes": notes,
            }
        )
        if created:
            self._emit_application_event("planner_policy_application_prepared", application, actor, "prepared")
        application["preview"] = {
            "risk": proposal["proposal_risk_level"],
            "affected_scope": proposal["affected_workflows_scope"],
            "simulation": proposal.get("simulation"),
            "rollback_available": metadata.rollback_supported,
            "verification_strategy": metadata.verification_strategy,
        }
        return application

    async def policy_applications(
        self,
        *,
        status: str | None = None,
        policy_key: str | None = None,
        proposal_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        return {
            "items": await self.policy_runtime_store.list_applications(
                status=status,
                policy_key=policy_key,
                proposal_id=proposal_id,
                limit=limit,
            )
        }

    async def policy_application(self, application_id: str) -> dict[str, Any] | None:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        item = await self.policy_runtime_store.get_application(application_id)
        if item is not None:
            item["history"] = await self.policy_runtime_store.history(application_id)
        return item

    async def apply_policy_application(
        self,
        application_id: str,
        *,
        actor: str,
        fingerprint: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if self.policy_runtime_store is None or self.proposal_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        application = await self.policy_runtime_store.get_application(application_id)
        if application is None:
            raise PlannerPolicyProposalError("planner_policy_application_not_found")
        if application["application_fingerprint"] != fingerprint:
            raise PlannerPolicyProposalError("planner_policy_application_stale")
        if application["status"] == "applied":
            return application
        if application["status"] != "prepared":
            raise PlannerPolicyProposalError("planner_policy_application_conflict")
        proposal = await self.proposal_store.get(str(application["proposal_id"]))
        if proposal is None or proposal.get("status") != "approved":
            raise PlannerPolicyProposalError("planner_policy_application_not_approved")
        if proposal.get("proposal_fingerprint") != application["proposal_fingerprint"]:
            raise PlannerPolicyProposalError("planner_policy_application_stale")
        validate_policy_value(str(application["policy_key"]), application["proposed_value"])
        current = await self.policy_runtime_store.get_policy(str(application["policy_key"]), str(application["scope"]))
        if current["value"] != application["previous_value"] or current["revision"] != application["baseline_revision"]:
            raise PlannerPolicyProposalError("planner_policy_application_stale")
        applying = await self.policy_runtime_store.update_application(
            application_id,
            {"status": "applying", "actor": actor, "notes": notes, "started_at": _utc_now()},
            "application_started",
        )
        self._emit_application_event("planner_policy_application_started", applying, actor, "applying")
        try:
            written = await self.policy_runtime_store.set_policy(
                str(application["policy_key"]),
                str(application["scope"]),
                application["proposed_value"],
                expected_value=application["previous_value"],
                expected_revision=int(application["baseline_revision"]),
            )
            read_back = await self.policy_runtime_store.get_policy(str(application["policy_key"]), str(application["scope"]))
            if read_back["value"] != application["proposed_value"] or not semantic_verify(str(application["policy_key"]), read_back["value"]):
                raise PlannerPolicyProposalError("planner_policy_verification_failed")
            completed = await self.policy_runtime_store.update_application(
                application_id,
                {"status": "applied", "completed_at": _utc_now(), "applied_revision": written["revision"]},
                "application_applied",
            )
            await self.proposal_store.set_application_status(str(application["proposal_id"]), "applied")
            self._emit_application_event("planner_policy_application_verified", completed, actor, "applied")
            self._emit_application_event("planner_policy_application_completed", completed, actor, "applied")
            return completed
        except (PlannerPolicyProposalError, PlannerPolicyRegistryError, ValueError) as exc:
            code = getattr(exc, "code", str(exc))
            return await self._rollback_after_failed_apply(application_id, application, actor, code)

    async def _rollback_after_failed_apply(
        self,
        application_id: str,
        application: dict[str, Any],
        actor: str,
        code: str,
    ) -> dict[str, Any]:
        if self.policy_runtime_store is None or self.proposal_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        current = await self.policy_runtime_store.get_policy(str(application["policy_key"]), str(application["scope"]))
        try:
            restored = await self.policy_runtime_store.set_policy(
                str(application["policy_key"]),
                str(application["scope"]),
                application["previous_value"],
                expected_value=current["value"],
                expected_revision=int(current["revision"]),
            )
            rolled = await self.policy_runtime_store.update_application(
                application_id,
                {
                    "status": "rolled_back",
                    "completed_at": _utc_now(),
                    "rolled_back_at": _utc_now(),
                    "rollback_revision": restored["revision"],
                    "error_code": code,
                    "error_message": code,
                },
                "rollback_completed",
            )
            await self.proposal_store.set_application_status(str(application["proposal_id"]), "rolled_back")
            self._emit_application_event("planner_policy_application_failed", rolled, actor, "rolled_back")
            self._emit_application_event("planner_policy_rollback_completed", rolled, actor, "rolled_back")
            return rolled
        except Exception as rollback_exc:
            failed = await self.policy_runtime_store.update_application(
                application_id,
                {
                    "status": "rollback_failed",
                    "completed_at": _utc_now(),
                    "error_code": "planner_policy_rollback_failed",
                    "error_message": str(rollback_exc),
                },
                "rollback_failed",
            )
            await self.proposal_store.set_application_status(str(application["proposal_id"]), "rollback_failed")
            self._emit_application_event("planner_policy_rollback_failed", failed, actor, "rollback_failed")
            return failed

    async def rollback_policy_application(
        self,
        application_id: str,
        *,
        actor: str,
        fingerprint: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if self.policy_runtime_store is None or self.proposal_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        application = await self.policy_runtime_store.get_application(application_id)
        if application is None:
            raise PlannerPolicyProposalError("planner_policy_application_not_found")
        if application["application_fingerprint"] != fingerprint:
            raise PlannerPolicyProposalError("planner_policy_rollback_stale")
        if application["status"] == "rolled_back":
            return application
        if application["status"] != "applied":
            raise PlannerPolicyProposalError("planner_policy_application_conflict")
        if not application.get("rollback_supported"):
            raise PlannerPolicyProposalError("planner_policy_application_not_applicable")
        current = await self.policy_runtime_store.get_policy(str(application["policy_key"]), str(application["scope"]))
        if current["value"] != application["proposed_value"] or current["revision"] != application["applied_revision"]:
            raise PlannerPolicyProposalError("planner_policy_rollback_stale")
        started = await self.policy_runtime_store.update_application(
            application_id,
            {"status": "rollback_requested", "actor": actor, "notes": notes},
            "rollback_started",
        )
        self._emit_application_event("planner_policy_rollback_started", started, actor, "rollback_requested")
        try:
            restored = await self.policy_runtime_store.set_policy(
                str(application["policy_key"]),
                str(application["scope"]),
                application["previous_value"],
                expected_value=application["proposed_value"],
                expected_revision=int(application["applied_revision"]),
            )
            rolled = await self.policy_runtime_store.update_application(
                application_id,
                {"status": "rolled_back", "rolled_back_at": _utc_now(), "rollback_revision": restored["revision"]},
                "rollback_completed",
            )
            await self.proposal_store.set_application_status(str(application["proposal_id"]), "rolled_back")
            self._emit_application_event("planner_policy_rollback_completed", rolled, actor, "rolled_back")
            return rolled
        except Exception as exc:
            failed = await self.policy_runtime_store.update_application(
                application_id,
                {"status": "rollback_failed", "error_code": "planner_policy_rollback_failed", "error_message": str(exc)},
                "rollback_failed",
            )
            await self.proposal_store.set_application_status(str(application["proposal_id"]), "rollback_failed")
            self._emit_application_event("planner_policy_rollback_failed", failed, actor, "rollback_failed")
            return failed

    async def prepare_policy_rollout(
        self,
        application_id: str,
        *,
        actor: str,
        notes: str | None = None,
        minimum_baseline_sample_size: int = DEFAULT_MINIMUM_BASELINE_SAMPLE_SIZE,
    ) -> dict[str, Any]:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        application = await self.policy_runtime_store.get_application(application_id)
        if application is None:
            raise PlannerPolicyProposalError("planner_policy_application_not_found")
        if application.get("status") != "applied":
            raise PlannerPolicyProposalError("planner_policy_rollout_not_applicable")
        runtime = await self.policy_runtime_store.get_policy(str(application["policy_key"]), str(application["scope"]))
        if runtime["value"] != application["proposed_value"] or runtime["revision"] != application["applied_revision"]:
            raise PlannerPolicyProposalError("planner_policy_rollout_stale")
        if await self.policy_runtime_store.active_experiment(str(application["policy_key"]), str(application["scope"])) is not None:
            raise PlannerPolicyProposalError("planner_policy_experiment_rollout_conflict")
        stages = parse_rollout_stages()
        baseline_records = await self.records(limit=1000)
        baseline_metrics = rollout_metrics(baseline_records)
        if int(baseline_metrics.get("policy_sample_size") or 0) < minimum_baseline_sample_size:
            baseline_status = "insufficient_baseline"
            health_status = "insufficient_baseline"
        else:
            baseline_status = "prepared"
            health_status = "not_started"
        fingerprint = rollout_fingerprint(
            application_fingerprint=str(application["application_fingerprint"]),
            policy_key=str(application["policy_key"]),
            scope=str(application["scope"]),
            previous_value=application["previous_value"],
            target_value=application["proposed_value"],
            stages=stages,
        )
        try:
            rollout, created = await self.policy_runtime_store.create_rollout(
                {
                    "application_id": application["application_id"],
                    "proposal_id": application["proposal_id"],
                    "policy_key": application["policy_key"],
                    "scope": application["scope"],
                    "baseline_revision": application["baseline_revision"],
                    "target_revision": application["applied_revision"],
                    "previous_value": application["previous_value"],
                    "target_value": application["proposed_value"],
                    "stages": stages,
                    "baseline_window": {"sample_source": "planner_calibration_records", "minimum_sample_size": minimum_baseline_sample_size},
                    "observation_window": {"minimum_stage_sample_size": DEFAULT_MINIMUM_STAGE_SAMPLE_SIZE},
                    "baseline_metrics": baseline_metrics,
                    "rollout_fingerprint": fingerprint,
                    "application_fingerprint": application["application_fingerprint"],
                    "application_risk_level": application.get("application_risk_level"),
                    "actor": actor,
                    "notes": notes,
                }
            )
        except ValueError as exc:
            raise PlannerPolicyProposalError(str(exc)) from exc
        if rollout["status"] == "prepared" and baseline_status == "insufficient_baseline":
            rollout = await self.policy_runtime_store.update_rollout(
                rollout["rollout_id"],
                {"status": "insufficient_baseline", "health_status": health_status, "error_code": "insufficient_baseline"},
                "rollout_insufficient_baseline",
                actor=actor,
            )
        if created:
            self._emit_rollout_event("planner_policy_rollout_prepared", rollout, actor)
        rollout["history"] = await self.policy_runtime_store.rollout_history(rollout["rollout_id"])
        return rollout

    async def policy_rollouts(
        self,
        *,
        status: str | None = None,
        policy_key: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        return {"items": await self.policy_runtime_store.list_rollouts(status=status, policy_key=policy_key, limit=limit)}

    async def policy_rollout(self, rollout_id: str) -> dict[str, Any] | None:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        rollout = await self.policy_runtime_store.get_rollout(rollout_id)
        if rollout is not None:
            rollout["history"] = await self.policy_runtime_store.rollout_history(rollout_id)
        return rollout

    async def start_policy_rollout(self, rollout_id: str, *, actor: str) -> dict[str, Any]:
        rollout = await self._rollout_for_action(rollout_id)
        if rollout["status"] in {"canary", "expanding", "completed", "rolled_back"}:
            return rollout
        if rollout["status"] not in {"prepared"}:
            raise PlannerPolicyProposalError("planner_policy_rollout_conflict")
        await self._validate_rollout_not_stale(rollout)
        if await self.policy_runtime_store.active_experiment(str(rollout["policy_key"]), str(rollout["scope"])) is not None:
            raise PlannerPolicyProposalError("planner_policy_experiment_rollout_conflict")
        candidate = {**rollout, "status": "canary"}
        await self._ensure_portfolio_allows(candidate, candidate_kind="rollout")
        first = int((rollout["stages"] or [10])[0])
        started = await self.policy_runtime_store.update_rollout(
            rollout_id,
            {"status": "canary", "current_percentage": first, "target_percentage": first, "started_at": _utc_now(), "health_status": "observing", "health_score": 50},
            "rollout_started",
            actor=actor,
            revision=int(rollout["target_revision"]),
        )
        self._emit_rollout_event("planner_policy_rollout_started", started, actor)
        return started

    async def evaluate_policy_rollout(
        self,
        rollout_id: str,
        *,
        actor: str | None = None,
        minimum_stage_sample_size: int = DEFAULT_MINIMUM_STAGE_SAMPLE_SIZE,
    ) -> dict[str, Any]:
        rollout = await self._rollout_for_action(rollout_id)
        records = await self.records(limit=1000)
        treatment_records, control_records = split_rollout_records(records, rollout_id)
        treatment_metrics = rollout_metrics(treatment_records)
        control_metrics = rollout_metrics(control_records)
        health = evaluate_rollout_health(
            baseline_metrics=rollout["baseline_metrics"],
            treatment_metrics=treatment_metrics,
            control_metrics=control_metrics,
            minimum_stage_sample_size=minimum_stage_sample_size,
        )
        status = rollout["status"]
        event = "stage_evaluated"
        if health["health_status"] == "warning":
            status = "paused"
            event = "rollout_paused"
        elif health["health_status"] == "degraded":
            status = "degraded"
            event = "rollout_degraded"
        elif health["health_status"] == "critical":
            application = await self.policy_runtime_store.get_application(str(rollout["application_id"]))
            if application and application.get("rollback_supported"):
                critical = await self.policy_runtime_store.update_rollout(
                    rollout_id,
                    {
                        "status": "degraded",
                        "treatment_metrics": treatment_metrics,
                        "control_metrics": control_metrics,
                        "delta_metrics": health["delta_metrics"],
                        "health_status": "critical",
                        "health_score": health["health_score"],
                    },
                    "rollout_degraded",
                    actor=actor,
                )
                self._emit_rollout_event("planner_policy_rollout_degraded", critical, actor)
                return await self.rollback_policy_rollout(rollout_id, actor=actor or "system")
            status = "rollback_required"
            event = "rollout_degraded"
        updated = await self.policy_runtime_store.update_rollout(
            rollout_id,
            {
                "status": status,
                "treatment_metrics": treatment_metrics,
                "control_metrics": control_metrics,
                "delta_metrics": health["delta_metrics"],
                "health_status": health["health_status"],
                "health_score": health["health_score"],
            },
            event,
            actor=actor,
        )
        self._emit_rollout_event("planner_policy_rollout_stage_evaluated", updated, actor)
        return updated

    async def advance_policy_rollout(self, rollout_id: str, *, actor: str) -> dict[str, Any]:
        rollout = await self._rollout_for_action(rollout_id)
        if rollout["status"] == "completed":
            return rollout
        if rollout["status"] not in {"canary", "expanding"}:
            raise PlannerPolicyProposalError("planner_policy_rollout_conflict")
        await self._validate_rollout_not_stale(rollout)
        evaluated = await self.evaluate_policy_rollout(rollout_id, actor=actor)
        if evaluated.get("health_status") != "healthy":
            raise PlannerPolicyProposalError("planner_policy_rollout_not_healthy")
        stages = [int(stage) for stage in evaluated["stages"]]
        current = int(evaluated["current_percentage"])
        try:
            next_stage = stages[stages.index(current) + 1]
        except (ValueError, IndexError):
            return await self._complete_rollout(evaluated, actor)
        advanced = await self.policy_runtime_store.update_rollout(
            rollout_id,
            {"status": "expanding", "current_percentage": next_stage, "target_percentage": next_stage, "health_status": "observing", "health_score": 50},
            "rollout_advanced",
            actor=actor,
            revision=int(evaluated["target_revision"]),
        )
        self._emit_rollout_event("planner_policy_rollout_advanced", advanced, actor)
        return advanced

    async def pause_policy_rollout(self, rollout_id: str, *, actor: str) -> dict[str, Any]:
        rollout = await self._rollout_for_action(rollout_id)
        if rollout["status"] == "paused":
            return rollout
        if rollout["status"] not in {"canary", "expanding", "observing"}:
            raise PlannerPolicyProposalError("planner_policy_rollout_conflict")
        paused = await self.policy_runtime_store.update_rollout(
            rollout_id,
            {"status": "paused", "paused_at": _utc_now()},
            "rollout_paused",
            actor=actor,
        )
        self._emit_rollout_event("planner_policy_rollout_paused", paused, actor)
        return paused

    async def resume_policy_rollout(self, rollout_id: str, *, actor: str) -> dict[str, Any]:
        rollout = await self._rollout_for_action(rollout_id)
        if rollout["status"] != "paused":
            raise PlannerPolicyProposalError("planner_policy_rollout_conflict")
        await self._validate_rollout_not_stale(rollout)
        if rollout.get("health_status") == "critical":
            raise PlannerPolicyProposalError("planner_policy_rollout_not_healthy")
        resumed = await self.policy_runtime_store.update_rollout(
            rollout_id,
            {"status": "canary" if int(rollout.get("current_percentage") or 0) == int((rollout["stages"] or [10])[0]) else "expanding"},
            "rollout_resumed",
            actor=actor,
        )
        self._emit_rollout_event("planner_policy_rollout_started", resumed, actor)
        return resumed

    async def rollback_policy_rollout(self, rollout_id: str, *, actor: str) -> dict[str, Any]:
        rollout = await self._rollout_for_action(rollout_id)
        if rollout["status"] == "rolled_back":
            return rollout
        application = await self.policy_runtime_store.get_application(str(rollout["application_id"]))
        if application is None or not application.get("rollback_supported"):
            raise PlannerPolicyProposalError("planner_policy_rollout_rollback_failed")
        current = await self.policy_runtime_store.get_policy(str(rollout["policy_key"]), str(rollout["scope"]))
        if current["value"] != rollout["target_value"] or current["revision"] != rollout["target_revision"]:
            raise PlannerPolicyProposalError("planner_policy_rollout_stale")
        started = await self.policy_runtime_store.update_rollout(rollout_id, {"status": "rollback_requested"}, "rollback_started", actor=actor)
        self._emit_rollout_event("planner_policy_rollout_rollback", started, actor)
        try:
            restored = await self.policy_runtime_store.set_policy(
                str(rollout["policy_key"]),
                str(rollout["scope"]),
                rollout["previous_value"],
                expected_value=rollout["target_value"],
                expected_revision=int(rollout["target_revision"]),
            )
            rolled = await self.policy_runtime_store.update_rollout(
                rollout_id,
                {"status": "rolled_back", "rolled_back_at": _utc_now(), "current_percentage": 0, "target_percentage": 0, "health_status": "rolled_back", "health_score": 0},
                "rollback_completed",
                actor=actor,
                revision=int(restored["revision"]),
            )
            self._emit_rollout_event("planner_policy_rollout_rollback", rolled, actor)
            return rolled
        except Exception as exc:
            failed = await self.policy_runtime_store.update_rollout(
                rollout_id,
                {"status": "failed", "error_code": "planner_policy_rollout_rollback_failed"},
                "rollout_failed",
                actor=actor,
            )
            self._emit_rollout_event("planner_policy_rollout_degraded", failed, actor)
            raise PlannerPolicyProposalError("planner_policy_rollout_rollback_failed") from exc

    async def create_policy_experiment(
        self,
        *,
        policy_key: str,
        scope: str,
        variants: list[dict[str, Any]],
        allocation: dict[str, int],
        primary_metric: str,
        secondary_metrics: list[str] | None = None,
        minimum_sample_size: int = 20,
        guardrails: dict[str, Any] | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        metadata_for(policy_key)
        metric_direction(primary_metric)
        normalized_variants: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, variant in enumerate(variants):
            variant_id = str(variant.get("variant_id") or f"variant_{index + 1}")
            if variant_id == "control" or variant_id in seen:
                raise PlannerPolicyProposalError("planner_policy_experiment_invalid_allocation")
            seen.add(variant_id)
            value = variant.get("value")
            try:
                validate_policy_value(policy_key, value)
            except PlannerPolicyRegistryError as exc:
                raise PlannerPolicyProposalError(exc.code) from exc
            if variant.get("reduces_safety") is True or variant.get("risk_level") == "critical":
                raise PlannerPolicyProposalError("planner_policy_experiment_safety_gate_required")
            normalized_variants.append(
                {
                    "variant_id": variant_id,
                    "name": variant.get("name") or variant_id,
                    "value": value,
                    "proposal_id": variant.get("proposal_id"),
                    "application_id": variant.get("application_id"),
                    "allocation_percentage": int(allocation.get(variant_id, 0)),
                    "risk_level": variant.get("risk_level") or "medium",
                    "status": "active",
                }
            )
        try:
            validate_allocation(allocation, normalized_variants)
        except ValueError as exc:
            raise PlannerPolicyProposalError(str(exc)) from exc
        active_rollout = await self.policy_runtime_store.active_rollout(policy_key, scope)
        if active_rollout is not None:
            raise PlannerPolicyProposalError("planner_policy_experiment_rollout_conflict")
        runtime = await self.policy_runtime_store.get_policy(policy_key, scope)
        fingerprint = experiment_fingerprint(
            policy_key=policy_key,
            scope=scope,
            baseline_revision=int(runtime["revision"]),
            control_value=runtime["value"],
            variants=normalized_variants,
            allocation=allocation,
            primary_metric=primary_metric,
            secondary_metrics=secondary_metrics or [],
            guardrails=guardrails or DEFAULT_GUARDRAILS,
        )
        try:
            experiment, created = await self.policy_runtime_store.create_experiment(
                {
                    "policy_key": policy_key,
                    "scope": scope,
                    "baseline_revision": runtime["revision"],
                    "control_value": runtime["value"],
                    "variants": normalized_variants,
                    "allocation": allocation,
                    "minimum_sample_size": minimum_sample_size,
                    "observation_window": {"minimum_sample_size_per_group": minimum_sample_size},
                    "primary_metric": primary_metric,
                    "secondary_metrics": secondary_metrics or [],
                    "guardrails": guardrails or DEFAULT_GUARDRAILS,
                    "experiment_fingerprint": fingerprint,
                    "actor": actor,
                }
            )
        except ValueError as exc:
            raise PlannerPolicyProposalError(str(exc)) from exc
        if created:
            self._emit_experiment_event("planner_policy_experiment_created", experiment, actor)
        return experiment

    async def policy_experiments(self, *, status: str | None = None, policy_key: str | None = None, limit: int = 100) -> dict[str, Any]:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        items = await self.policy_runtime_store.list_experiments(status=status, policy_key=policy_key, limit=limit)
        return {"items": [self._with_experiment_summaries(item) for item in items]}

    async def policy_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        experiment = await self.policy_runtime_store.get_experiment(experiment_id)
        if experiment is not None:
            experiment = self._with_experiment_summaries(experiment)
            experiment["history"] = await self.policy_runtime_store.experiment_history(experiment_id)
        return experiment

    async def policy_experiment_portfolio(self) -> dict[str, Any]:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        experiments = await self.policy_runtime_store.list_experiments(status=None, policy_key=None, limit=500)
        rollouts = await self.policy_runtime_store.list_rollouts(status=None, policy_key=None, limit=500)
        portfolio = evaluate_portfolio_conflicts(experiments=experiments, rollouts=rollouts)
        self._emit_portfolio_events(portfolio)
        return portfolio

    async def ready_policy_experiment(self, experiment_id: str, *, actor: str) -> dict[str, Any]:
        experiment = await self._experiment_for_action(experiment_id)
        if experiment["status"] == "ready":
            return experiment
        if experiment["status"] != "draft":
            raise PlannerPolicyProposalError("planner_policy_experiment_conflict")
        await self._validate_experiment_not_stale(experiment)
        ready = await self.policy_runtime_store.update_experiment(experiment_id, {"status": "ready"}, "experiment_ready", actor=actor)
        self._emit_experiment_event("planner_policy_experiment_ready", ready, actor)
        return ready

    async def start_policy_experiment(self, experiment_id: str, *, actor: str) -> dict[str, Any]:
        experiment = await self._experiment_for_action(experiment_id)
        if experiment["status"] == "running":
            return experiment
        if experiment["status"] != "ready":
            raise PlannerPolicyProposalError("planner_policy_experiment_conflict")
        await self._validate_experiment_not_stale(experiment)
        if await self.policy_runtime_store.active_rollout(str(experiment["policy_key"]), str(experiment["scope"])) is not None:
            raise PlannerPolicyProposalError("planner_policy_experiment_rollout_conflict")
        candidate = {**experiment, "status": "running"}
        await self._ensure_portfolio_allows(candidate, candidate_kind="experiment")
        started = await self.policy_runtime_store.update_experiment(experiment_id, {"status": "running", "started_at": _utc_now()}, "experiment_started", actor=actor)
        self._emit_experiment_event("planner_policy_experiment_started", started, actor)
        return started

    async def evaluate_policy_experiment(self, experiment_id: str, *, actor: str | None = None) -> dict[str, Any]:
        experiment = await self._experiment_for_action(experiment_id)
        records = await self.records(limit=1000)
        groups = split_experiment_records(records, experiment_id)
        control_metrics = experiment_group_metrics(groups.get("control", []))
        variant_metrics = {
            str(variant["variant_id"]): experiment_group_metrics(groups.get(str(variant["variant_id"]), []))
            for variant in experiment.get("variants") or []
        }
        self._emit_experiment_event("planner_policy_experiment_statistical_analysis_started", experiment, actor)
        comparison = evaluate_experiment_result(
            control_metrics=control_metrics,
            variant_metrics=variant_metrics,
            primary_metric=str(experiment["primary_metric"]),
            guardrails=experiment.get("guardrails") or {},
            minimum_sample_size=int(experiment["minimum_sample_size"]),
        )
        experiment_for_readiness = {**experiment, "metrics": {"control": control_metrics, "variants": variant_metrics, "comparison": comparison}, "result": comparison["result"], "winner_variant_id": comparison.get("winner_variant_id"), "statistical_summary": comparison.get("statistical_summary")}
        try:
            await self._validate_experiment_not_stale(experiment)
            policy_baseline_current = True
        except PlannerPolicyProposalError:
            policy_baseline_current = False
        self._emit_experiment_event("planner_policy_experiment_promotion_readiness_started", experiment_for_readiness, actor)
        promotion_readiness = evaluate_promotion_readiness(
            experiment=experiment_for_readiness,
            control_metrics=control_metrics,
            variant_metrics=variant_metrics,
            groups=groups,
            policy_baseline_current=policy_baseline_current,
        )
        status = "observing" if comparison["result"] == "insufficient_data" else experiment["status"]
        metrics = {"control": control_metrics, "variants": variant_metrics, "comparison": comparison, "promotion_readiness": promotion_readiness}
        updated = await self.policy_runtime_store.update_experiment(
            experiment_id,
            {"status": status, "metrics": metrics, "result": comparison["result"], "winner_variant_id": comparison.get("winner_variant_id")},
            "experiment_evaluated",
            actor=actor,
        )
        updated = self._with_experiment_summaries(updated)
        self._emit_experiment_event("planner_policy_experiment_promotion_readiness_completed", updated, actor)
        if promotion_readiness.get("status") == "ready":
            self._emit_experiment_event("planner_policy_experiment_promotion_recommendation_created", updated, actor)
        self._emit_experiment_event("planner_policy_experiment_statistical_analysis_completed", updated, actor)
        self._emit_experiment_event("planner_policy_experiment_evaluated", updated, actor)
        return updated

    async def complete_policy_experiment(self, experiment_id: str, *, actor: str) -> dict[str, Any]:
        experiment = await self._experiment_for_action(experiment_id)
        if experiment["status"] == "completed":
            return experiment
        evaluated = await self.evaluate_policy_experiment(experiment_id, actor=actor)
        if evaluated.get("result") == "insufficient_data":
            return evaluated
        completed = await self.policy_runtime_store.update_experiment(
            experiment_id,
            {"status": "completed", "completed_at": _utc_now()},
            "experiment_completed",
            actor=actor,
        )
        self._emit_experiment_event("planner_policy_experiment_completed", completed, actor)
        return completed

    async def pause_policy_experiment(self, experiment_id: str, *, actor: str) -> dict[str, Any]:
        experiment = await self._experiment_for_action(experiment_id)
        if experiment["status"] == "paused":
            return experiment
        if experiment["status"] not in {"running", "observing"}:
            raise PlannerPolicyProposalError("planner_policy_experiment_conflict")
        paused = await self.policy_runtime_store.update_experiment(experiment_id, {"status": "paused"}, "experiment_paused", actor=actor)
        self._emit_experiment_event("planner_policy_experiment_paused", paused, actor)
        return paused

    async def resume_policy_experiment(self, experiment_id: str, *, actor: str) -> dict[str, Any]:
        experiment = await self._experiment_for_action(experiment_id)
        if experiment["status"] != "paused":
            raise PlannerPolicyProposalError("planner_policy_experiment_conflict")
        await self._validate_experiment_not_stale(experiment)
        resumed = await self.policy_runtime_store.update_experiment(experiment_id, {"status": "running"}, "experiment_resumed", actor=actor)
        self._emit_experiment_event("planner_policy_experiment_resumed", resumed, actor)
        return resumed

    async def cancel_policy_experiment(self, experiment_id: str, *, actor: str) -> dict[str, Any]:
        experiment = await self._experiment_for_action(experiment_id)
        if experiment["status"] == "cancelled":
            return experiment
        cancelled = await self.policy_runtime_store.update_experiment(experiment_id, {"status": "cancelled", "completed_at": _utc_now()}, "experiment_cancelled", actor=actor)
        self._emit_experiment_event("planner_policy_experiment_cancelled", cancelled, actor)
        return cancelled

    async def resolve_policy_for_workflow(
        self,
        policy_key: str,
        workflow_id: str,
        scope: str = "global_planner",
    ) -> dict[str, Any]:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        experiment_pinned = await self.policy_runtime_store.get_workflow_experiment_snapshot(workflow_id, policy_key, scope)
        if experiment_pinned is not None:
            return experiment_pinned
        pinned = await self.policy_runtime_store.get_workflow_policy_snapshot(workflow_id, policy_key, scope)
        if pinned is not None:
            return pinned
        base = await self.policy_runtime_store.get_policy(policy_key, scope)
        experiment = await self.policy_runtime_store.active_experiment(policy_key, scope)
        if experiment and experiment["status"] == "running":
            ordered_allocation = {"control": int((experiment.get("allocation") or {}).get("control", 0))}
            for variant in experiment.get("variants") or []:
                if isinstance(variant, dict):
                    ordered_allocation[str(variant["variant_id"])] = int((experiment.get("allocation") or {}).get(str(variant["variant_id"]), 0))
            variant_id = assign_experiment_variant(
                experiment_id=str(experiment["experiment_id"]),
                workflow_id=workflow_id,
                policy_key=policy_key,
                allocation=ordered_allocation,
            )
            variants = {str(item["variant_id"]): item for item in experiment.get("variants") or [] if isinstance(item, dict)}
            effective_value = base["value"] if variant_id == "control" else variants[variant_id]["value"]
            revision = int(experiment["baseline_revision"])
            assignment = {
                "policy_key": policy_key,
                "revision": revision,
                "experiment_id": experiment["experiment_id"],
                "variant_id": variant_id,
                "effective_value": effective_value,
                "bucket": experiment_bucket(experiment_id=str(experiment["experiment_id"]), workflow_id=workflow_id, policy_key=policy_key),
                "assignment_algorithm_version": POLICY_EXPERIMENT_ASSIGNMENT_ALGORITHM_VERSION,
            }
            return await self.policy_runtime_store.create_workflow_experiment_snapshot(
                {
                    "workflow_id": workflow_id,
                    "policy_key": policy_key,
                    "scope": scope,
                    "revision": revision,
                    "experiment_id": experiment["experiment_id"],
                    "variant_id": variant_id,
                    "effective_value": effective_value,
                    "assignment": assignment,
                }
            )
        active = await self.policy_runtime_store.active_rollout(policy_key, scope)
        treatment = False
        rollout_id = None
        effective_value = base["value"]
        revision = int(base["revision"])
        if active and active["status"] in {"canary", "expanding"}:
            rollout_id = active["rollout_id"]
            treatment = is_workflow_in_rollout(
                workflow_id=workflow_id,
                policy_key=policy_key,
                rollout_id=rollout_id,
                percentage=int(active["current_percentage"]),
            )
            effective_value = active["target_value"] if treatment else active["previous_value"]
            revision = int(active["target_revision"] if treatment else active["baseline_revision"])
        assignment = {
            "policy_key": policy_key,
            "revision": revision,
            "rollout_id": rollout_id,
            "treatment": treatment,
            "effective_value": effective_value,
            "bucket": stable_rollout_bucket(workflow_id=workflow_id, policy_key=policy_key, rollout_id=rollout_id or "base"),
            "assignment_algorithm_version": POLICY_ROLLOUT_ASSIGNMENT_ALGORITHM_VERSION,
        }
        return await self.policy_runtime_store.create_workflow_policy_snapshot(
            {
                "workflow_id": workflow_id,
                "policy_key": policy_key,
                "scope": scope,
                "revision": revision,
                "rollout_id": rollout_id,
                "treatment": treatment,
                "effective_value": effective_value,
                "assignment": assignment,
            }
        )

    async def _experiment_for_action(self, experiment_id: str) -> dict[str, Any]:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        experiment = await self.policy_runtime_store.get_experiment(experiment_id)
        if experiment is None:
            raise PlannerPolicyProposalError("planner_policy_experiment_not_found")
        return experiment

    async def _validate_experiment_not_stale(self, experiment: dict[str, Any]) -> None:
        current = await self.policy_runtime_store.get_policy(str(experiment["policy_key"]), str(experiment["scope"]))
        if current["value"] != experiment["control_value"] or current["revision"] != experiment["baseline_revision"]:
            raise PlannerPolicyProposalError("planner_policy_experiment_stale")

    async def _rollout_for_action(self, rollout_id: str) -> dict[str, Any]:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        rollout = await self.policy_runtime_store.get_rollout(rollout_id)
        if rollout is None:
            raise PlannerPolicyProposalError("planner_policy_rollout_not_found")
        return rollout

    async def _validate_rollout_not_stale(self, rollout: dict[str, Any]) -> None:
        current = await self.policy_runtime_store.get_policy(str(rollout["policy_key"]), str(rollout["scope"]))
        if current["value"] != rollout["target_value"] or current["revision"] != rollout["target_revision"]:
            raise PlannerPolicyProposalError("planner_policy_rollout_stale")
        application = await self.policy_runtime_store.get_application(str(rollout["application_id"]))
        if application is None or application.get("application_fingerprint") != rollout.get("application_fingerprint"):
            raise PlannerPolicyProposalError("planner_policy_rollout_stale")

    async def _complete_rollout(self, rollout: dict[str, Any], actor: str) -> dict[str, Any]:
        current = await self.policy_runtime_store.get_policy(str(rollout["policy_key"]), str(rollout["scope"]))
        promoted = await self.policy_runtime_store.set_policy(
            str(rollout["policy_key"]),
            str(rollout["scope"]),
            rollout["target_value"],
            expected_value=current["value"],
            expected_revision=int(current["revision"]),
        )
        completed = await self.policy_runtime_store.update_rollout(
            rollout["rollout_id"],
            {"status": "completed", "completed_at": _utc_now(), "current_percentage": 100, "target_percentage": 100, "target_revision": promoted["revision"], "health_status": "healthy", "health_score": 100},
            "rollout_completed",
            actor=actor,
            revision=int(promoted["revision"]),
        )
        self._emit_rollout_event("planner_policy_rollout_completed", completed, actor)
        return completed

    @staticmethod
    def _emit_application_event(event_name: str, application: dict[str, Any], actor: str | None, status: str) -> None:
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="planner_policy_application",
            stage="planner_policy_application",
            status=EventStatus.COMPLETED,
            data={
                "event": event_name,
                "application_id": application.get("application_id"),
                "proposal_id": application.get("proposal_id"),
                "policy_key": application.get("policy_key"),
                "scope": application.get("scope"),
                "risk": application.get("application_risk_level"),
                "actor": actor,
                "application_status": status,
            },
        )

    @staticmethod
    def _emit_rollout_event(event_name: str, rollout: dict[str, Any], actor: str | None) -> None:
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="planner_policy_rollout",
            stage="planner_policy_rollout",
            status=EventStatus.COMPLETED,
            data={
                "event": event_name,
                "rollout_id": rollout.get("rollout_id"),
                "application_id": rollout.get("application_id"),
                "policy_key": rollout.get("policy_key"),
                "percentage": rollout.get("current_percentage"),
                "sample_size": ((rollout.get("treatment_metrics") or {}).get("policy_sample_size") if isinstance(rollout.get("treatment_metrics"), dict) else None),
                "health": rollout.get("health_status"),
                "health_score": rollout.get("health_score"),
                "revision": rollout.get("target_revision"),
                "actor": actor,
            },
        )

    @staticmethod
    def _emit_experiment_event(event_name: str, experiment: dict[str, Any], actor: str | None) -> None:
        statistical_summary = experiment.get("statistical_summary")
        confidence = statistical_summary.get("decision_confidence") if isinstance(statistical_summary, dict) else None
        confidence_label = statistical_summary.get("decision_confidence_label") if isinstance(statistical_summary, dict) else None
        promotion_readiness = experiment.get("promotion_readiness")
        readiness_status = promotion_readiness.get("status") if isinstance(promotion_readiness, dict) else None
        readiness_score = promotion_readiness.get("score") if isinstance(promotion_readiness, dict) else None
        readiness_confidence = promotion_readiness.get("confidence") if isinstance(promotion_readiness, dict) else None
        readiness_reason_count = len(promotion_readiness.get("reason_codes") or []) if isinstance(promotion_readiness, dict) else None
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="planner_policy_experiment",
            stage="planner_policy_experiment",
            status=EventStatus.COMPLETED,
            data={
                "event": event_name,
                "experiment_id": experiment.get("experiment_id"),
                "policy_key": experiment.get("policy_key"),
                "scope": experiment.get("scope"),
                "status": experiment.get("status"),
                "primary_metric": experiment.get("primary_metric"),
                "result": experiment.get("result"),
                "winner_variant_id": experiment.get("winner_variant_id"),
                "decision_confidence": confidence,
                "decision_confidence_label": confidence_label,
                "readiness_status": readiness_status,
                "readiness_score": readiness_score,
                "readiness_confidence": readiness_confidence,
                "reason_codes_count": readiness_reason_count,
                "actor": actor,
            },
        )

    async def _ensure_portfolio_allows(self, candidate: dict[str, Any], *, candidate_kind: str) -> None:
        if self.policy_runtime_store is None:
            raise PlannerPolicyProposalError("planner_policy_proposal_store_unavailable")
        experiments = await self.policy_runtime_store.list_experiments(status=None, policy_key=None, limit=500)
        rollouts = await self.policy_runtime_store.list_rollouts(status=None, policy_key=None, limit=500)
        portfolio = evaluate_portfolio_conflicts(
            experiments=experiments,
            rollouts=rollouts,
            candidate=candidate,
            candidate_kind=candidate_kind,
        )
        self._emit_portfolio_events(portfolio)
        if portfolio.get("blocking_conflict_count"):
            raise PlannerPolicyProposalError("planner_experiment_portfolio_conflict")

    @staticmethod
    def _emit_portfolio_events(portfolio: dict[str, Any]) -> None:
        emit_workflow_event(
            WorkflowEventType.STAGE_COMPLETED,
            source="planner_experiment_portfolio",
            stage="planner_experiment_portfolio",
            status=EventStatus.COMPLETED,
            data={
                "event": "planner_experiment_portfolio_evaluated",
                "active_experiments": len(portfolio.get("active_experiments") or []),
                "active_rollouts": len(portfolio.get("active_rollouts") or []),
                "blocking_conflicts": portfolio.get("blocking_conflict_count"),
                "warnings": portfolio.get("warning_count"),
                "isolation_status": portfolio.get("isolation_status"),
            },
        )
        for conflict in portfolio.get("conflicts") or []:
            emit_workflow_event(
                WorkflowEventType.STAGE_COMPLETED,
                source="planner_experiment_portfolio",
                stage="planner_experiment_portfolio",
                status=EventStatus.COMPLETED,
                data={
                    "event": "planner_experiment_conflict_detected",
                    "conflict_type": conflict.get("type"),
                    "severity": conflict.get("severity"),
                    "left": conflict.get("left"),
                    "right": conflict.get("right"),
                },
            )
        for warning in portfolio.get("warnings") or []:
            emit_workflow_event(
                WorkflowEventType.STAGE_COMPLETED,
                source="planner_experiment_portfolio",
                stage="planner_experiment_portfolio",
                status=EventStatus.COMPLETED,
                data={
                    "event": "planner_experiment_interference_detected",
                    "conflict_type": warning.get("type"),
                    "severity": warning.get("severity"),
                    "left": warning.get("left"),
                    "right": warning.get("right"),
                },
            )

    @staticmethod
    def _with_experiment_summaries(experiment: dict[str, Any]) -> dict[str, Any]:
        metrics = experiment.get("metrics")
        enriched = dict(experiment)
        if isinstance(metrics, dict):
            comparison = metrics.get("comparison")
            if isinstance(comparison, dict) and isinstance(comparison.get("statistical_summary"), dict):
                enriched["statistical_summary"] = comparison["statistical_summary"]
            if isinstance(metrics.get("promotion_readiness"), dict):
                enriched["promotion_readiness"] = metrics["promotion_readiness"]
        return enriched


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
