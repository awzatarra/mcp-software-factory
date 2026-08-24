from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.agent_evaluation_models import BaselineCreate, EvaluationCompareRequest, EvaluationRunCreate, EvaluationValidityPatch, RubricCreate, RubricPatch, RubricVersionCreate
from api.ci_models import CIAnalyticsResponse
from api.dependencies import ApiServices, get_services
from api.services.planner_calibration_service import PlannerRecommendationError
from graph.agent_recommendations import DEFAULT_MIN_SAMPLE_SIZE as DEFAULT_AGENT_RECOMMENDATION_MIN_SAMPLE_SIZE
from graph.planner_policy_proposals import PlannerPolicyProposalError


router=APIRouter(prefix="/api/evaluations",tags=["agent-evaluations"])


def service(services: ApiServices):
    if services.agent_evaluations is None:raise HTTPException(503,"Agent evaluation service unavailable")
    return services.agent_evaluations


def planner_calibration_service(services: ApiServices):
    if services.planner_calibration is None:raise HTTPException(503,"Planner calibration service unavailable")
    return services.planner_calibration


def ci_analytics_service(services: ApiServices):
    if services.ci_analytics is None:raise HTTPException(503,"CI analytics service unavailable")
    return services.ci_analytics


class RecommendationReviewStart(BaseModel):
    reviewer: str
    notes: str | None = None
    fingerprint: str


class RecommendationAccept(BaseModel):
    reviewer: str
    notes: str | None = None
    decision_reason: str | None = None
    fingerprint: str


class RecommendationReject(BaseModel):
    reviewer: str
    reason: str
    notes: str | None = None
    fingerprint: str


class RecommendationDefer(BaseModel):
    reviewer: str
    reason: str
    deferred_until: str | None = None
    notes: str | None = None
    fingerprint: str


class PolicyProposalTransition(BaseModel):
    actor: str
    notes: str | None = None
    reason: str | None = None
    proposal_fingerprint: str


class PolicyApplicationPrepare(BaseModel):
    actor: str
    notes: str | None = None


class PolicyApplicationAction(BaseModel):
    actor: str
    notes: str | None = None
    application_fingerprint: str


class PolicyRolloutPrepare(BaseModel):
    actor: str
    notes: str | None = None


class PolicyRolloutAction(BaseModel):
    actor: str
    notes: str | None = None


class PolicyExperimentVariant(BaseModel):
    variant_id: str | None = None
    name: str | None = None
    value: object
    proposal_id: str | None = None
    application_id: str | None = None
    risk_level: str | None = None
    reduces_safety: bool | None = None


class PolicyExperimentCreate(BaseModel):
    policy_key: str
    scope: str = "global_planner"
    variants: list[PolicyExperimentVariant]
    allocation: dict[str, int]
    primary_metric: str
    secondary_metrics: list[str] = []
    minimum_sample_size: int = 20
    guardrails: dict[str, object] = {}
    actor: str | None = None


class PolicyExperimentAction(BaseModel):
    actor: str
    notes: str | None = None


def recommendation_error(exc: PlannerRecommendationError) -> HTTPException:
    status = 409 if exc.code in {"planner_recommendation_stale", "planner_recommendation_review_terminal", "planner_recommendation_review_transition_not_allowed"} else 404 if exc.code == "planner_recommendation_not_found" else 503
    return HTTPException(status, exc.code)


def policy_proposal_error(exc: PlannerPolicyProposalError) -> HTTPException:
    status = 404 if exc.code in {"planner_recommendation_not_found", "planner_policy_proposal_not_found", "planner_policy_application_not_found", "planner_policy_rollout_not_found", "planner_policy_experiment_not_found"} else 409 if exc.code in {
        "planner_policy_proposal_source_not_accepted",
        "planner_policy_proposal_source_stale",
        "planner_policy_proposal_stale",
        "planner_policy_proposal_transition_not_allowed",
        "planner_policy_application_not_approved",
        "planner_policy_application_conflict",
        "planner_policy_application_stale",
        "planner_policy_rollback_stale",
        "planner_policy_rollout_stale",
        "planner_policy_rollout_conflict",
        "planner_policy_rollout_not_healthy",
        "planner_policy_experiment_stale",
        "planner_policy_experiment_conflict",
        "planner_policy_experiment_rollout_conflict",
        "planner_experiment_portfolio_conflict",
    } else 422 if exc.code in {
        "planner_policy_key_unresolved",
        "planner_policy_proposal_invalid_source",
        "planner_policy_proposal_invalid_value",
        "planner_policy_application_not_applicable",
        "planner_policy_application_invalid_value",
        "planner_policy_rollout_not_applicable",
        "planner_policy_experiment_invalid_allocation",
        "planner_policy_experiment_invalid_metric",
        "planner_policy_experiment_safety_gate_required",
    } else 500 if exc.code in {
        "planner_policy_application_failed",
        "planner_policy_verification_failed",
        "planner_policy_rollback_failed",
        "planner_policy_rollout_rollback_failed",
    } else 503
    return HTTPException(status, exc.code)


@router.get("/planner/calibration")
async def planner_calibration(
    limit: int=Query(default=200,ge=1,le=1000),
    version: str="7.7-v1",
    framework: str | None=None,
    minimum_sample_size: int=Query(default=DEFAULT_AGENT_RECOMMENDATION_MIN_SAMPLE_SIZE,ge=1,le=1000),
    segment_min_sample_size: int=Query(default=10,ge=1,le=1000),
    services: ApiServices=Depends(get_services),
):
    return await planner_calibration_service(services).analyze(
        limit=limit,
        version=version,
        framework=framework,
        minimum_sample_size=minimum_sample_size,
        segment_min_sample_size=segment_min_sample_size,
    )


@router.get("/planner/recommendations")
async def planner_recommendations(
    status: str | None=None,
    policy: str | None=None,
    severity: str | None=None,
    limit: int=Query(default=100,ge=1,le=500),
    services: ApiServices=Depends(get_services),
):
    return await planner_calibration_service(services).recommendations(
        status=status,
        policy=policy,
        severity=severity,
        limit=limit,
    )


@router.get("/agents/recommendations")
async def agent_recommendations(
    agent: str | None=None,
    framework: str | None=None,
    status: str | None=None,
    severity: str | None=None,
    minimum_sample_size: int=Query(default=20,ge=1,le=1000),
    limit: int=Query(default=100,ge=1,le=500),
    services: ApiServices=Depends(get_services),
):
    return await planner_calibration_service(services).agent_recommendations(
        agent=agent,
        framework=framework,
        status=status,
        severity=severity,
        minimum_sample_size=minimum_sample_size,
        limit=limit,
    )


@router.get("/agents/recommendations/{recommendation_id}")
async def agent_recommendation(recommendation_id: str, services: ApiServices=Depends(get_services)):
    item=await planner_calibration_service(services).agent_recommendation(recommendation_id)
    if item is None:raise HTTPException(404,"planner_recommendation_not_found")
    return item


@router.post("/agents/recommendations/{recommendation_id}/review/start")
async def start_agent_recommendation_review(recommendation_id: str, body: RecommendationReviewStart, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).review_agent_recommendation(recommendation_id,action="start",reviewer=body.reviewer,notes=body.notes,fingerprint=body.fingerprint)
    except PlannerRecommendationError as exc:
        raise recommendation_error(exc)


@router.post("/agents/recommendations/{recommendation_id}/accept")
async def accept_agent_recommendation(recommendation_id: str, body: RecommendationAccept, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).review_agent_recommendation(recommendation_id,action="accept",reviewer=body.reviewer,notes=body.notes,reason=body.decision_reason,fingerprint=body.fingerprint)
    except PlannerRecommendationError as exc:
        raise recommendation_error(exc)


@router.post("/agents/recommendations/{recommendation_id}/reject")
async def reject_agent_recommendation(recommendation_id: str, body: RecommendationReject, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).review_agent_recommendation(recommendation_id,action="reject",reviewer=body.reviewer,notes=body.notes,reason=body.reason,fingerprint=body.fingerprint)
    except PlannerRecommendationError as exc:
        raise recommendation_error(exc)


@router.post("/agents/recommendations/{recommendation_id}/defer")
async def defer_agent_recommendation(recommendation_id: str, body: RecommendationDefer, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).review_agent_recommendation(recommendation_id,action="defer",reviewer=body.reviewer,notes=body.notes,reason=body.reason,deferred_until=body.deferred_until,fingerprint=body.fingerprint)
    except PlannerRecommendationError as exc:
        raise recommendation_error(exc)


@router.get("/planner/recommendations/{recommendation_id}")
async def planner_recommendation(recommendation_id: str, services: ApiServices=Depends(get_services)):
    item=await planner_calibration_service(services).recommendation(recommendation_id)
    if item is None:raise HTTPException(404,"planner_recommendation_not_found")
    return item


@router.post("/planner/recommendations/{recommendation_id}/review/start")
async def start_planner_recommendation_review(recommendation_id: str, body: RecommendationReviewStart, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).review(recommendation_id,action="start",reviewer=body.reviewer,notes=body.notes,fingerprint=body.fingerprint)
    except PlannerRecommendationError as exc:
        raise recommendation_error(exc)


@router.post("/planner/recommendations/{recommendation_id}/accept")
async def accept_planner_recommendation(recommendation_id: str, body: RecommendationAccept, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).review(recommendation_id,action="accept",reviewer=body.reviewer,notes=body.notes,reason=body.decision_reason,fingerprint=body.fingerprint)
    except PlannerRecommendationError as exc:
        raise recommendation_error(exc)


@router.post("/planner/recommendations/{recommendation_id}/reject")
async def reject_planner_recommendation(recommendation_id: str, body: RecommendationReject, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).review(recommendation_id,action="reject",reviewer=body.reviewer,notes=body.notes,reason=body.reason,fingerprint=body.fingerprint)
    except PlannerRecommendationError as exc:
        raise recommendation_error(exc)


@router.post("/planner/recommendations/{recommendation_id}/defer")
async def defer_planner_recommendation(recommendation_id: str, body: RecommendationDefer, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).review(recommendation_id,action="defer",reviewer=body.reviewer,notes=body.notes,reason=body.reason,deferred_until=body.deferred_until,fingerprint=body.fingerprint)
    except PlannerRecommendationError as exc:
        raise recommendation_error(exc)


@router.post("/planner/recommendations/{recommendation_id}/policy-proposal",status_code=201)
async def create_planner_policy_proposal(recommendation_id: str, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).create_policy_proposal(recommendation_id)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.get("/planner/policy-proposals")
async def planner_policy_proposals(
    status: str | None=None,
    policy_key: str | None=None,
    risk_level: str | None=None,
    limit: int=Query(default=100,ge=1,le=500),
    services: ApiServices=Depends(get_services),
):
    try:
        return await planner_calibration_service(services).policy_proposals(
            status=status,
            policy_key=policy_key,
            risk_level=risk_level,
            limit=limit,
        )
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.get("/planner/policy-proposals/{proposal_id}")
async def planner_policy_proposal(proposal_id: str, services: ApiServices=Depends(get_services)):
    try:
        item=await planner_calibration_service(services).policy_proposal(proposal_id)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)
    if item is None:raise HTTPException(404,"planner_policy_proposal_not_found")
    return item


async def transition_policy_proposal(proposal_id: str, action: str, body: PolicyProposalTransition, services: ApiServices):
    try:
        return await planner_calibration_service(services).transition_policy_proposal(
            proposal_id,
            action=action,
            actor=body.actor,
            notes=body.notes,
            reason=body.reason,
            fingerprint=body.proposal_fingerprint,
        )
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-proposals/{proposal_id}/ready")
async def ready_planner_policy_proposal(proposal_id: str, body: PolicyProposalTransition, services: ApiServices=Depends(get_services)):
    return await transition_policy_proposal(proposal_id,"ready",body,services)


@router.post("/planner/policy-proposals/{proposal_id}/approve")
async def approve_planner_policy_proposal(proposal_id: str, body: PolicyProposalTransition, services: ApiServices=Depends(get_services)):
    return await transition_policy_proposal(proposal_id,"approve",body,services)


@router.post("/planner/policy-proposals/{proposal_id}/reject")
async def reject_planner_policy_proposal(proposal_id: str, body: PolicyProposalTransition, services: ApiServices=Depends(get_services)):
    return await transition_policy_proposal(proposal_id,"reject",body,services)


@router.post("/planner/policy-proposals/{proposal_id}/cancel")
async def cancel_planner_policy_proposal(proposal_id: str, body: PolicyProposalTransition, services: ApiServices=Depends(get_services)):
    return await transition_policy_proposal(proposal_id,"cancel",body,services)


@router.post("/planner/policy-proposals/{proposal_id}/application/prepare",status_code=201)
async def prepare_planner_policy_application(proposal_id: str, body: PolicyApplicationPrepare, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).prepare_policy_application(
            proposal_id,
            actor=body.actor,
            notes=body.notes,
        )
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.get("/planner/policy-applications")
async def planner_policy_applications(
    status: str | None=None,
    policy_key: str | None=None,
    proposal_id: str | None=None,
    limit: int=Query(default=100,ge=1,le=500),
    services: ApiServices=Depends(get_services),
):
    try:
        return await planner_calibration_service(services).policy_applications(
            status=status,
            policy_key=policy_key,
            proposal_id=proposal_id,
            limit=limit,
        )
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.get("/planner/policy-applications/{application_id}")
async def planner_policy_application(application_id: str, services: ApiServices=Depends(get_services)):
    try:
        item=await planner_calibration_service(services).policy_application(application_id)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)
    if item is None:raise HTTPException(404,"planner_policy_application_not_found")
    return item


@router.post("/planner/policy-applications/{application_id}/apply")
async def apply_planner_policy_application(application_id: str, body: PolicyApplicationAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).apply_policy_application(
            application_id,
            actor=body.actor,
            notes=body.notes,
            fingerprint=body.application_fingerprint,
        )
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-applications/{application_id}/rollback")
async def rollback_planner_policy_application(application_id: str, body: PolicyApplicationAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).rollback_policy_application(
            application_id,
            actor=body.actor,
            notes=body.notes,
            fingerprint=body.application_fingerprint,
        )
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-applications/{application_id}/rollout/prepare",status_code=201)
async def prepare_planner_policy_rollout(application_id: str, body: PolicyRolloutPrepare, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).prepare_policy_rollout(
            application_id,
            actor=body.actor,
            notes=body.notes,
        )
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.get("/planner/policy-rollouts")
async def planner_policy_rollouts(
    status: str | None=None,
    policy_key: str | None=None,
    limit: int=Query(default=100,ge=1,le=500),
    services: ApiServices=Depends(get_services),
):
    try:
        return await planner_calibration_service(services).policy_rollouts(status=status,policy_key=policy_key,limit=limit)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.get("/planner/policy-rollouts/{rollout_id}")
async def planner_policy_rollout(rollout_id: str, services: ApiServices=Depends(get_services)):
    try:
        item=await planner_calibration_service(services).policy_rollout(rollout_id)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)
    if item is None:raise HTTPException(404,"planner_policy_rollout_not_found")
    return item


@router.post("/planner/policy-rollouts/{rollout_id}/start")
async def start_planner_policy_rollout(rollout_id: str, body: PolicyRolloutAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).start_policy_rollout(rollout_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-rollouts/{rollout_id}/evaluate")
async def evaluate_planner_policy_rollout(rollout_id: str, body: PolicyRolloutAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).evaluate_policy_rollout(rollout_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-rollouts/{rollout_id}/advance")
async def advance_planner_policy_rollout(rollout_id: str, body: PolicyRolloutAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).advance_policy_rollout(rollout_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-rollouts/{rollout_id}/pause")
async def pause_planner_policy_rollout(rollout_id: str, body: PolicyRolloutAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).pause_policy_rollout(rollout_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-rollouts/{rollout_id}/resume")
async def resume_planner_policy_rollout(rollout_id: str, body: PolicyRolloutAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).resume_policy_rollout(rollout_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-rollouts/{rollout_id}/rollback")
async def rollback_planner_policy_rollout(rollout_id: str, body: PolicyRolloutAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).rollback_policy_rollout(rollout_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-experiments",status_code=201)
async def create_planner_policy_experiment(body: PolicyExperimentCreate, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).create_policy_experiment(
            policy_key=body.policy_key,
            scope=body.scope,
            variants=[variant.model_dump() for variant in body.variants],
            allocation=body.allocation,
            primary_metric=body.primary_metric,
            secondary_metrics=body.secondary_metrics,
            minimum_sample_size=body.minimum_sample_size,
            guardrails=body.guardrails,
            actor=body.actor,
        )
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.get("/planner/policy-experiments")
async def planner_policy_experiments(
    status: str | None=None,
    policy_key: str | None=None,
    limit: int=Query(default=100,ge=1,le=500),
    services: ApiServices=Depends(get_services),
):
    try:
        return await planner_calibration_service(services).policy_experiments(status=status,policy_key=policy_key,limit=limit)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.get("/planner/policy-experiments/portfolio")
async def planner_policy_experiment_portfolio(services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).policy_experiment_portfolio()
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.get("/planner/policy-experiments/{experiment_id}")
async def planner_policy_experiment(experiment_id: str, services: ApiServices=Depends(get_services)):
    try:
        item=await planner_calibration_service(services).policy_experiment(experiment_id)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)
    if item is None:raise HTTPException(404,"planner_policy_experiment_not_found")
    return item


@router.post("/planner/policy-experiments/{experiment_id}/ready")
async def ready_planner_policy_experiment(experiment_id: str, body: PolicyExperimentAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).ready_policy_experiment(experiment_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-experiments/{experiment_id}/start")
async def start_planner_policy_experiment(experiment_id: str, body: PolicyExperimentAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).start_policy_experiment(experiment_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-experiments/{experiment_id}/evaluate")
async def evaluate_planner_policy_experiment(experiment_id: str, body: PolicyExperimentAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).evaluate_policy_experiment(experiment_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-experiments/{experiment_id}/pause")
async def pause_planner_policy_experiment(experiment_id: str, body: PolicyExperimentAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).pause_policy_experiment(experiment_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-experiments/{experiment_id}/resume")
async def resume_planner_policy_experiment(experiment_id: str, body: PolicyExperimentAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).resume_policy_experiment(experiment_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-experiments/{experiment_id}/complete")
async def complete_planner_policy_experiment(experiment_id: str, body: PolicyExperimentAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).complete_policy_experiment(experiment_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/planner/policy-experiments/{experiment_id}/cancel")
async def cancel_planner_policy_experiment(experiment_id: str, body: PolicyExperimentAction, services: ApiServices=Depends(get_services)):
    try:
        return await planner_calibration_service(services).cancel_policy_experiment(experiment_id,actor=body.actor)
    except PlannerPolicyProposalError as exc:
        raise policy_proposal_error(exc)


@router.post("/runs")
async def create_run(body: EvaluationRunCreate, services: ApiServices=Depends(get_services)):
    return await service(services).evaluate(body)


@router.get("/runs")
async def list_runs(
    workflow: str | None=None, agent: str | None=None, model: str | None=None,
    verdict: str | None=None, evaluation_type: str | None=None,
    date_from: datetime | None=None, date_to: datetime | None=None,
    min_score: float | None=Query(default=None,ge=0,le=1),
    max_score: float | None=Query(default=None,ge=0,le=1),
    validity_status: str | None=None,
    limit: int=Query(default=100,ge=1,le=500),offset: int=Query(default=0,ge=0),
    services: ApiServices=Depends(get_services),
):
    return await service(services).store.list_runs(limit=limit,offset=offset,workflow_id=workflow,agent=agent,model=model,verdict=verdict,evaluation_type=evaluation_type,validity_status=validity_status,date_from=date_from.isoformat() if date_from else None,date_to=date_to.isoformat() if date_to else None,min_score=min_score,max_score=max_score)


@router.get("/runs/{evaluation_run_id}")
async def run_detail(evaluation_run_id: str, services: ApiServices=Depends(get_services)):
    item=await service(services).store.detail(evaluation_run_id)
    if item is None:raise HTTPException(404,"Evaluation run not found")
    return item


@router.patch("/runs/{evaluation_run_id}/validity")
async def patch_validity(evaluation_run_id: str, body: EvaluationValidityPatch, services: ApiServices=Depends(get_services)):
    if body.validity_status=="invalidated" and not body.reason:raise HTTPException(422,"reason is required when invalidating a run")
    item=await service(services).store.set_validity(evaluation_run_id,validity_status=body.validity_status,superseded_by_run_id=body.superseded_by_run_id,reason=body.reason)
    if item is None:raise HTTPException(404,"Evaluation run not found")
    if item.get("error"):raise HTTPException(422,item["error"])
    return item


@router.get("/workflows/{thread_id}")
async def workflow_runs(thread_id: str, branch_id: str="original", services: ApiServices=Depends(get_services)):
    return await service(services).store.list_runs(workflow_id=thread_id,branch_id=branch_id)


@router.post("/workflows/{thread_id}/evaluate")
async def evaluate_workflow(thread_id: str, body: EvaluationRunCreate | None=None, force: bool=False, services: ApiServices=Depends(get_services)):
    request=body or EvaluationRunCreate(workflow_id=thread_id,force=force)
    request=request.model_copy(update={"workflow_id":thread_id,"force":force or request.force})
    return await service(services).evaluate(request)


@router.get("/agents")
async def agents(include_invalidated: bool=False, services: ApiServices=Depends(get_services)):
    validity="" if include_invalidated else "AND r.validity_status='valid'"
    items=await service(services).store.fetch_all(f"""SELECT x.agent_name AS agent,COUNT(*) AS evaluations_attempted,
    COUNT(x.score) AS evaluations_scored,SUM(CASE WHEN x.score IS NULL THEN 1 ELSE 0 END) AS not_evaluated_count,
    AVG(x.score) AS average_score,MIN(x.score) AS min_score,MAX(x.score) AS max_score
    FROM agent_evaluation_results x JOIN agent_evaluation_runs r ON r.evaluation_run_id=x.evaluation_run_id
    WHERE x.agent_name!='Workflow' {validity} GROUP BY x.agent_name ORDER BY average_score DESC""")
    return {"items":items}


@router.get("/metrics")
async def metrics(include_invalidated: bool=False, include_legacy: bool=False, services: ApiServices=Depends(get_services)):
    clauses=[]
    if not include_invalidated:clauses.append("r.validity_status='valid'")
    if not include_legacy:clauses.append("x.metric_name!='workflow_duration'")
    where="WHERE "+" AND ".join(clauses) if clauses else ""
    items=await service(services).store.fetch_all(f"""SELECT x.metric_name,x.metric_type,x.unit,COUNT(*) AS samples,
    AVG(CASE WHEN x.contributes_to_score=1 THEN x.metric_value END) AS average_value,
    AVG(CASE WHEN x.raw_value IS NOT NULL THEN x.raw_value WHEN x.metric_type='diagnostic' THEN x.metric_value END) AS average_raw_value,
    SUM(CASE WHEN x.contributes_to_score=1 THEN x.passed ELSE 0 END) AS passed_count,
    SUM(x.contributes_to_score) AS score_samples
    FROM agent_evaluation_metrics x JOIN agent_evaluation_runs r ON r.evaluation_run_id=x.evaluation_run_id {where}
    GROUP BY x.metric_name,x.metric_type,x.unit ORDER BY x.metric_type,x.metric_name""")
    duration_metrics={"agent_active_duration_total","workflow_wall_clock_duration","agent_duration","workflow_duration"}
    count_metrics={"handled_error_count","recovered_error_count","unhandled_error_count","retry_count","repair_count","agent_retry_count"}
    for item in items:
        legacy=item["metric_name"]=="workflow_duration"
        diagnostic=item["metric_type"]=="diagnostic" or int(item.get("score_samples") or 0)==0
        item["analytics_role"]="legacy" if legacy else "diagnostic" if diagnostic else "score"
        if diagnostic or legacy:
            item["average_value"]=None;item["passed_count"]=None
        item["raw_unit"]="ms" if item["metric_name"] in duration_metrics else "x" if item["metric_name"]=="parallelism_factor" else "count" if item["metric_name"] in count_metrics else "ratio" if item["metric_name"].endswith("_rate") or item["metric_name"].endswith("_ratio") else None
    return {"items":items}


@router.get("/ci/metrics", response_model=CIAnalyticsResponse)
async def ci_metrics(
    limit: int | None=Query(default=None,ge=1),
    framework: str | None=None,
    from_: datetime | None=Query(default=None,alias="from"),
    to: datetime | None=None,
    services: ApiServices=Depends(get_services),
):
    return await ci_analytics_service(services).metrics(
        limit=limit,
        framework=framework,
        from_timestamp=from_,
        to_timestamp=to,
    )


@router.get("/dashboard")
async def dashboard(include_invalidated: bool=False, services: ApiServices=Depends(get_services)):
    return await service(services).dashboard(include_invalidated=include_invalidated)


@router.get("/regressions")
async def regressions(include_invalidated: bool=False, services: ApiServices=Depends(get_services)):
    validity="" if include_invalidated else "AND r.validity_status='valid'"
    items=await service(services).store.fetch_all(f"""SELECT x.evidence_id,x.evaluation_run_id,x.reference_id,
    x.summary,x.metadata_json,x.created_at FROM agent_evaluation_evidence x JOIN agent_evaluation_runs r ON r.evaluation_run_id=x.evaluation_run_id
    WHERE x.evidence_type='regression' {validity} ORDER BY x.created_at DESC""")
    return {"items":items}


@router.get("/rubrics")
async def rubrics(services: ApiServices=Depends(get_services)):return {"items":await service(services).store.rubrics()}


def validate_rubric_weights(dimensions: dict[str,float], weights: dict[str,float]) -> None:
    if set(weights)!=set(dimensions) or abs(sum(weights.values())-1)>1e-6:
        raise HTTPException(422,"Rubric weights must match dimensions and sum to 1")


def next_minor_version(version: str) -> str:
    try:
        major,minor=version.split(".",1)
        return f"{int(major)}.{int(minor)+1}"
    except (ValueError,TypeError):
        return f"{version}.1"


def with_verdict_thresholds(item):
    if item is None:return None
    item["verdict_thresholds"]={"excellent":.9,"good":.8,"acceptable":.7,"needs_improvement":.5,"failed":0,**item.get("thresholds",{})}
    return item


@router.get("/rubrics/{rubric_id}")
async def rubric_detail(rubric_id: str, services: ApiServices=Depends(get_services)):
    item=await service(services).store.rubric_detail(rubric_id)
    if item is None:raise HTTPException(404,"Rubric not found")
    return with_verdict_thresholds(item)


@router.get("/rubrics/{rubric_id}/versions")
async def rubric_versions(rubric_id: str, services: ApiServices=Depends(get_services)):
    item=await service(services).store.rubric_detail(rubric_id)
    if item is None:raise HTTPException(404,"Rubric not found")
    return {"items":item["history"]}


@router.post("/rubrics",status_code=201)
async def create_rubric(body: RubricCreate, services: ApiServices=Depends(get_services)):
    validate_rubric_weights(body.dimensions,body.weights)
    return await service(services).store.create_rubric(body.model_dump())


@router.post("/rubrics/{rubric_id}/versions",status_code=201)
async def create_rubric_version(rubric_id: str, body: RubricVersionCreate, services: ApiServices=Depends(get_services)):
    validate_rubric_weights(body.dimensions,body.weights)
    item,_,error=await service(services).store.create_rubric_version(rubric_id,body.model_dump())
    if error=="not_found":raise HTTPException(404,"Rubric not found")
    if error=="version_exists":raise HTTPException(409,"Rubric version already exists with different content")
    return with_verdict_thresholds(await service(services).store.rubric_detail(item["rubric_id"]))


@router.post("/rubrics/{rubric_id}/disable")
async def disable_rubric(rubric_id: str, services: ApiServices=Depends(get_services)):
    store=service(services).store
    current=await store.rubric_detail(rubric_id)
    if current is None:raise HTTPException(404,"Rubric not found")
    if current["version_status"]!="current" or not current["enabled"]:
        raise HTTPException(409,"Only the current enabled rubric version can be disabled")
    item=await store.disable_rubric(rubric_id)
    if item is None:raise HTTPException(404,"Rubric not found")
    return with_verdict_thresholds(item)


@router.patch("/rubrics/{rubric_id}")
async def patch_rubric(rubric_id: str, body: RubricPatch, services: ApiServices=Depends(get_services)):
    store=service(services).store
    current=await store.fetch_one("SELECT * FROM agent_evaluation_rubrics WHERE rubric_id=?",(rubric_id,))
    if current is None:raise HTTPException(404,"Rubric not found")
    updates=body.model_dump(exclude_unset=True)
    if current["version_status"]!="current":
        raise HTTPException(409,"Historical rubric versions are immutable; create a new version instead")
    if updates=={"enabled":False}:return with_verdict_thresholds(await store.disable_rubric(rubric_id))
    if not any(key in updates for key in ("dimensions","weights","thresholds")):
        raise HTTPException(422,"Rubric updates must create a new version or disable the current version")
    proposed={**current,**{key:value for key,value in updates.items() if value is not None}}
    validate_rubric_weights(proposed["dimensions"],proposed["weights"])
    proposed["version"]=updates.get("version") or next_minor_version(current["version"])
    item,_,error=await store.create_rubric_version(rubric_id,proposed)
    if error=="version_exists":raise HTTPException(409,"Rubric version already exists with different content")
    return with_verdict_thresholds(await store.rubric_detail(item["rubric_id"]))


@router.post("/baselines",status_code=201)
async def create_baseline(body: BaselineCreate, services: ApiServices=Depends(get_services)):return await service(services).store.create_baseline(body.model_dump())


@router.get("/baselines")
async def baselines(services: ApiServices=Depends(get_services)):return {"items":await service(services).store.baselines()}


@router.post("/compare")
async def compare(body: EvaluationCompareRequest, services: ApiServices=Depends(get_services)):
    result=await service(services).compare(body.evaluation_run_id,body.baseline_id,body.regression_threshold)
    if result is None:raise HTTPException(404,"Evaluation run not found")
    return result
