from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.ci_models import (
    CIAuditTrail,
    CIPipelinePreview,
    CIPipelineRun,
    CIRepairability,
    CIRepairStatus,
    CIRunListResponse,
    CIRunRequest,
    CIWorkflowStatus,
)
from api.dependencies import ApiServices, get_services
from api.services.ci_service import (
    CICommandNotAllowed,
    CIEnvironmentUnavailable,
    CIError,
    CIPathViolation,
    CIPipelineStale,
    CIProjectUnavailable,
)
from graph.persistence_service import WorkflowNotFoundError


router = APIRouter(prefix="/api/workflows", tags=["ci"])


def _service(services: ApiServices):
    if services.ci is None:
        raise HTTPException(status_code=503, detail="CI service unavailable")
    return services.ci


def _analytics_service(services: ApiServices):
    if services.ci_analytics is None:
        raise HTTPException(status_code=503, detail="CI analytics service unavailable")
    return services.ci_analytics


def _ci_error(exc: CIError) -> HTTPException:
    if isinstance(exc, CIProjectUnavailable):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, CIPipelineStale):
        return HTTPException(status_code=409, detail=exc.code)
    if isinstance(exc, (CICommandNotAllowed, CIPathViolation)):
        return HTTPException(status_code=403, detail=exc.code)
    if isinstance(exc, CIEnvironmentUnavailable):
        return HTTPException(status_code=422, detail=exc.code)
    return HTTPException(status_code=500, detail="ci_operation_failed")


async def _project(thread_id: str, services: ApiServices) -> str:
    try:
        snapshot = await services.query.get_snapshot(thread_id)
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    if not snapshot.project_name:
        raise HTTPException(status_code=404, detail="Project not found")
    return snapshot.project_name


async def _git_source(thread_id: str, project_id: str, services: ApiServices) -> dict[str, str | None]:
    if getattr(services, "git", None) is None:
        return {}
    try:
        summary = await services.git.workflow_summary(project_id, thread_id)
    except Exception:
        return {}
    commit = None
    if summary.developer_commit:
        commit = summary.developer_commit.get("commit")
    if summary.repair_commit:
        commit = summary.repair_commit.get("commit")
    return {
        "source_commit": commit or summary.head_commit,
        "source_branch": summary.workflow_branch or summary.branch,
        "workflow_branch": summary.workflow_branch,
        "repository_root": project_id,
    }


async def _git_head_commit(
    thread_id: str,
    project_id: str,
    services: ApiServices,
) -> str | None:
    if getattr(services, "git", None) is None:
        return None
    try:
        summary = await services.git.workflow_summary(project_id, thread_id)
    except Exception:
        return None
    return summary.head_commit


@router.get("/{thread_id}/ci", response_model=CIWorkflowStatus)
async def workflow_ci_status(
    thread_id: str,
    services: ApiServices = Depends(get_services),
) -> CIWorkflowStatus:
    try:
        project_id = await _project(thread_id, services)
        source = await _git_source(thread_id, project_id, services)
        status = await _service(services).workflow_status(
            thread_id,
            project_id,
            target_commit=source.get("source_commit"),
        )
        try:
            snapshot = await services.query.get_snapshot(thread_id)
            values = snapshot.values if hasattr(snapshot, "values") else {}
        except Exception:
            values = {}
        if values:
            repairability = values.get("ci_repair_repairability")
            status = status.model_copy(update={
                "repair": CIRepairStatus(
                    state=values.get("ci_repair_state") or status.repair.state,
                    attempts=int(values.get("ci_repair_attempts") or status.repair.attempts),
                    max_attempts=int(values.get("ci_repair_max_attempts") or status.repair.max_attempts),
                    source_run_id=values.get("ci_repair_source_run_id") or status.repair.source_run_id,
                    source_commit=values.get("ci_repair_source_commit") or status.repair.source_commit,
                    target_commit=values.get("ci_repair_target_commit") or status.repair.target_commit,
                    category=(repairability or {}).get("category") or status.repair.category,
                    reason_codes=(repairability or {}).get("reason_codes") or status.repair.reason_codes,
                    repairability=(
                        CIRepairability.model_validate(repairability)
                        if isinstance(repairability, dict)
                        else status.repair.repairability
                    ),
                    lineage=list(values.get("ci_repair_lineage") or status.repair.lineage),
                )
            })
        return status
    except CIError as exc:
        raise _ci_error(exc) from exc


@router.post("/{thread_id}/ci/prepare", response_model=CIPipelinePreview)
async def prepare_workflow_ci(
    thread_id: str,
    services: ApiServices = Depends(get_services),
) -> CIPipelinePreview:
    project_id = await _project(thread_id, services)
    source = await _git_source(thread_id, project_id, services)
    try:
        return await _service(services).prepare(thread_id, project_id, **source)
    except CIError as exc:
        raise _ci_error(exc) from exc


@router.post("/{thread_id}/ci/run", response_model=CIPipelineRun)
async def run_workflow_ci(
    thread_id: str,
    body: CIRunRequest,
    services: ApiServices = Depends(get_services),
) -> CIPipelineRun:
    project_id = await _project(thread_id, services)
    source = await _git_source(thread_id, project_id, services)
    try:
        service = _service(services)
        run = await service.run(
            thread_id,
            project_id,
            expected_fingerprint=body.pipeline_fingerprint,
            ci_run_id=body.ci_run_id,
            **source,
        )
        persistence = getattr(services, "persistence", None)
        head_commit = await _git_head_commit(thread_id, project_id, services)
        if persistence is not None and head_commit:
            eligibility = await service.promotion_eligibility(thread_id, head_commit)
            graph_result = await persistence.reconcile_successful_ci_run(
                thread_id,
                run=run.model_dump(mode="json"),
                eligibility=eligibility.model_dump(mode="json"),
                head_commit=head_commit,
            )
            if graph_result is not None:
                runner = getattr(services, "runner", None)
                record_result = getattr(runner, "record_recovery_result", None)
                if record_result is not None:
                    await record_result(thread_id, graph_result)
                else:
                    await services.query.sync_snapshot(thread_id)
        return run
    except CIError as exc:
        raise _ci_error(exc) from exc


@router.get("/{thread_id}/ci/runs", response_model=CIRunListResponse)
async def list_workflow_ci_runs(
    thread_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None),
    services: ApiServices = Depends(get_services),
) -> CIRunListResponse:
    await _project(thread_id, services)
    return await _service(services).list_runs(thread_id, limit=limit, status=status)


@router.get("/{thread_id}/ci/runs/{run_id}", response_model=CIPipelineRun)
async def workflow_ci_run_detail(
    thread_id: str,
    run_id: str,
    services: ApiServices = Depends(get_services),
) -> CIPipelineRun:
    await _project(thread_id, services)
    result = await _service(services).get_run(thread_id, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="CI run not found")
    return result


@router.get("/{thread_id}/ci/audit", response_model=CIAuditTrail)
async def workflow_ci_audit(
    thread_id: str,
    services: ApiServices = Depends(get_services),
) -> CIAuditTrail:
    await _project(thread_id, services)
    try:
        snapshot = await services.query.get_snapshot(thread_id)
        values = snapshot.values if hasattr(snapshot, "values") else {}
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except Exception:
        values = {}
    repair_state = {
        "state": values.get("ci_repair_state"),
        "attempts": values.get("ci_repair_attempts"),
        "source_run_id": values.get("ci_repair_source_run_id"),
        "source_commit": values.get("ci_repair_source_commit"),
        "target_commit": values.get("ci_repair_target_commit"),
        "lineage": values.get("ci_repair_lineage") or [],
    }
    return await _analytics_service(services).audit_trail(
        thread_id,
        repair_state=repair_state,
        promotion_eligible=values.get("ci_promotion_eligible"),
    )
