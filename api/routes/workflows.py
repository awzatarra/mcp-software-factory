from __future__ import annotations

import logging
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from api.dependencies import ApiServices, get_services
from api.errors import ApprovalConflictError
from api.models import (
    ApprovalRequest,
    ApprovalResponse,
    CreateWorkflowRequest,
    CreateWorkflowResponse,
    WorkflowSnapshotResponse,
    WorkflowListResponse,
    WorkflowListStatus,
    WorkflowSortBy,
    WorkflowSortOrder,
    ProjectFileContentResponse,
    ProjectFileTreeResponse,
    WorkflowProjectSummary,
)
from api.git_models import GitPendingOperationRecoveryResponse
from api.services.project_explorer_service import ProjectExplorerError
from api.services.git_service import GitError, GitPostApprovalRecoveryUnavailable
from graph.git_workflow import git_commit_state_updates
from api.execution_models import WorkflowAgentExecutionResponse
from api.evaluation_models import WorkflowEvaluationResponse
from api.services.workflow_execution_service import ExecutionBranchNotFoundError
from graph.persistence_service import WorkflowNotFoundError
from graph.runtime import create_thread_id
from streaming import EventStatus, WorkflowEventFactory, WorkflowEventType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@router.get(
    "/{thread_id}/evaluation",
    response_model=WorkflowEvaluationResponse,
)
async def get_workflow_evaluation(
    thread_id: str,
    branch_id: str = Query(default="original", min_length=1, max_length=200),
    include_evidence: bool = Query(default=False),
    services: ApiServices = Depends(get_services),
) -> WorkflowEvaluationResponse:
    try:
        return await services.evaluation.get_evaluation(
            thread_id,
            branch_id=branch_id,
            include_evidence=include_evidence,
        )
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ExecutionBranchNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Branch not found") from exc


@router.get(
    "/{thread_id}/execution",
    response_model=WorkflowAgentExecutionResponse,
)
async def get_workflow_execution(
    thread_id: str,
    branch_id: str = Query(default="original", min_length=1, max_length=200),
    include_events: bool = Query(default=False),
    services: ApiServices = Depends(get_services),
) -> WorkflowAgentExecutionResponse:
    try:
        return await services.execution.get_execution(
            thread_id,
            branch_id=branch_id,
            include_events=include_events,
        )
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ExecutionBranchNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Branch not found") from exc


def _audit_path(path: str | None) -> str:
    if path is None:
        return "n/a"
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized[:3] or normalized.startswith("//"):
        return "<invalid>"
    return "".join(character if character.isprintable() else "?" for character in normalized)[:200]


def _project_error(
    exc: Exception,
    *,
    thread_id: str,
    operation: str,
    path: str | None = None,
) -> HTTPException:
    if isinstance(exc, WorkflowNotFoundError):
        return HTTPException(status_code=404, detail="Thread not found")
    if isinstance(exc, ProjectExplorerError):
        logger.info(
            "project explorer thread=%s operation=%s path=%s result=rejected status=%s",
            thread_id,
            operation,
            _audit_path(path),
            exc.status_code,
        )
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    return HTTPException(status_code=500, detail="Project explorer failed")


@router.get("/{thread_id}/project", response_model=WorkflowProjectSummary)
async def get_workflow_project(
    thread_id: str,
    services: ApiServices = Depends(get_services),
) -> WorkflowProjectSummary:
    try:
        return await services.project_explorer.summary(thread_id)
    except (WorkflowNotFoundError, ProjectExplorerError) as exc:
        raise _project_error(
            exc, thread_id=thread_id, operation="summary"
        ) from exc


@router.get(
    "/{thread_id}/project/files/content",
    response_model=ProjectFileContentResponse,
)
async def get_workflow_project_file_content(
    thread_id: str,
    path: str = Query(min_length=1, max_length=2_000),
    services: ApiServices = Depends(get_services),
) -> ProjectFileContentResponse:
    try:
        return await services.project_explorer.content(thread_id, path)
    except (WorkflowNotFoundError, ProjectExplorerError) as exc:
        raise _project_error(
            exc, thread_id=thread_id, operation="content", path=path
        ) from exc


@router.get("/{thread_id}/project/files/download")
async def download_workflow_project_file(
    thread_id: str,
    request: Request,
    path: str = Query(min_length=1, max_length=2_000),
    services: ApiServices = Depends(get_services),
):
    if request.headers.get("range"):
        raise HTTPException(status_code=416, detail="Range requests are not supported")
    try:
        target = await services.project_explorer.download_target(thread_id, path)
    except (WorkflowNotFoundError, ProjectExplorerError) as exc:
        raise _project_error(
            exc, thread_id=thread_id, operation="download", path=path
        ) from exc
    logger.info(
        "project explorer thread=%s operation=download path=%s result=ok size=%s",
        thread_id, path.replace("\\", "/"), target.stat().st_size,
    )
    return FileResponse(
        target,
        filename=target.name,
        media_type="application/octet-stream",
        headers={"Accept-Ranges": "none"},
    )


@router.get("/{thread_id}/project/archive")
async def download_workflow_project_archive(
    thread_id: str,
    services: ApiServices = Depends(get_services),
):
    try:
        archive, filename, archive_size = await services.project_explorer.archive(thread_id)
    except (WorkflowNotFoundError, ProjectExplorerError) as exc:
        raise _project_error(
            exc, thread_id=thread_id, operation="archive"
        ) from exc

    def chunks() -> Iterator[bytes]:
        while data := archive.read(64 * 1024):
            yield data

    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(archive_size),
            "Accept-Ranges": "none",
        },
        background=BackgroundTask(archive.close),
    )


@router.get("/{thread_id}/project/files", response_model=ProjectFileTreeResponse)
async def get_workflow_project_files(
    thread_id: str,
    path: str = Query(default="", max_length=2_000),
    depth: int = Query(default=20, ge=0, le=50),
    include_hidden: bool = Query(default=False),
    services: ApiServices = Depends(get_services),
) -> ProjectFileTreeResponse:
    try:
        return await services.project_explorer.tree(
            thread_id,
            relative_path=path,
            depth=depth,
            include_hidden=include_hidden,
        )
    except (WorkflowNotFoundError, ProjectExplorerError) as exc:
        raise _project_error(
            exc, thread_id=thread_id, operation="tree", path=path
        ) from exc


@router.get("", response_model=WorkflowListResponse)
async def list_workflows(
    status_filter: WorkflowListStatus | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort_by: WorkflowSortBy = Query(default="updated_at"),
    sort_order: WorkflowSortOrder = Query(default="desc"),
    services: ApiServices = Depends(get_services),
) -> WorkflowListResponse:
    return await services.query.list_workflows(
        status=status_filter,
        search=search,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.post(
    "",
    response_model=CreateWorkflowResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_workflow(
    body: CreateWorkflowRequest,
    services: ApiServices = Depends(get_services),
) -> CreateWorkflowResponse:
    thread_id = create_thread_id()
    register_workflow = getattr(services.query, "register_workflow", None)
    if register_workflow is not None:
        await register_workflow(thread_id, body.request)
    await services.runner.schedule_start(thread_id=thread_id, request=body.request)
    logger.info("workflow created thread=%s", thread_id)
    return CreateWorkflowResponse(
        thread_id=thread_id,
        status="running",
        workflow_url=f"/api/workflows/{thread_id}",
        events_url=f"/api/workflows/{thread_id}/events",
    )


@router.get("/{thread_id}", response_model=WorkflowSnapshotResponse)
async def get_workflow(
    thread_id: str,
    services: ApiServices = Depends(get_services),
) -> WorkflowSnapshotResponse:
    try:
        return await services.query.get_snapshot(thread_id)
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc


async def _publish_git_promotion_approval_event(
    services: ApiServices,
    thread_id: str,
    event_type: WorkflowEventType,
    *,
    status: EventStatus,
    data: dict,
) -> None:
    broker = getattr(services, "broker", None)
    if broker is None:
        return
    try:
        history = await broker.get_history(thread_id)
        event = WorkflowEventFactory().create(
            thread_id=thread_id,
            event_type=event_type,
            source="api.workflows",
            stage="git_merge",
            status=status,
            data={**data, "branch_id": "original", "lineage": "original"},
        )
        event.sequence = max((item.sequence for item in history), default=0) + 1
        await broker.publish(event)
    except Exception:
        logger.exception("could not publish git promotion approval event thread=%s", thread_id)


async def _publish_git_tool_event(
    services: ApiServices,
    thread_id: str,
    event_type: WorkflowEventType,
    *,
    tool: str,
    status: EventStatus,
) -> None:
    await _publish_git_promotion_approval_event(
        services,
        thread_id,
        event_type,
        status=status,
        data={
            "operation": "git_merge",
            "tool": tool,
            "tool_name": tool,
        },
    )


async def _publish_git_recovery_event(
    services: ApiServices,
    thread_id: str,
    event_type: WorkflowEventType,
    *,
    operation: str,
    tool_name: str,
    status: EventStatus,
    data: dict | None = None,
) -> None:
    broker = getattr(services, "broker", None)
    if broker is None:
        return
    try:
        history = await broker.get_history(thread_id)
        event = WorkflowEventFactory().create(
            thread_id=thread_id,
            event_type=event_type,
            source="api.workflows",
            stage=operation,
            status=status,
            data={
                "operation": operation,
                "tool": tool_name,
                "tool_name": tool_name,
                "branch_id": "original",
                "lineage": "original",
                **(data or {}),
            },
        )
        event.sequence = max((item.sequence for item in history), default=0) + 1
        await broker.publish(event)
    except Exception:
        logger.exception("could not publish Git recovery event thread=%s", thread_id)


@router.post(
    "/{thread_id}/retry-pending-operation",
    response_model=GitPendingOperationRecoveryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_pending_git_operation(
    thread_id: str,
    services: ApiServices = Depends(get_services),
) -> GitPendingOperationRecoveryResponse:
    git_service = getattr(services, "git", None)
    if git_service is None:
        raise HTTPException(status_code=503, detail="Git service unavailable")
    try:
        snapshot = await services.persistence.get_snapshot(thread_id)
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    if snapshot.interrupts:
        raise HTTPException(
            status_code=409, detail="git_post_approval_recovery_not_available"
        )
    pending_operation = snapshot.values.get("pending_operation")
    phase = str(snapshot.values.get("git_commit_phase") or "implementation")
    recovery = await git_service.detect_post_approval_recovery(
        thread_id, pending_operation
    )
    continuation_required = pending_operation == recovery.operation
    if recovery.status == "completed" and not continuation_required:
        return GitPendingOperationRecoveryResponse(
            thread_id=thread_id,
            accepted=True,
            operation=recovery.operation,  # type: ignore[arg-type]
            tool_name=(
                "git__commit" if recovery.operation == "git_commit"
                else "git__merge_workflow_branch"
            ),
            status="completed",
            existing=True,
            result_commit=recovery.result_commit,
        )
    recovery_allowed = recovery.recoverable or (
        recovery.status == "completed" and continuation_required
    )
    if recovery.operation is None or not recovery_allowed:
        raise HTTPException(
            status_code=409, detail="git_post_approval_recovery_not_available"
        )
    operation = recovery.operation
    tool_name = "git__commit" if operation == "git_commit" else "git__merge_workflow_branch"
    await _publish_git_recovery_event(
        services, thread_id, WorkflowEventType.WORKFLOW_RESUMED,
        operation=operation, tool_name=tool_name, status=EventStatus.RUNNING,
        data={"reason": "post_approval_recovery"},
    )
    await _publish_git_recovery_event(
        services, thread_id, WorkflowEventType.TOOL_STARTED,
        operation=operation, tool_name=tool_name, status=EventStatus.RUNNING,
        data={"recovery": True},
    )
    try:
        _detected, result = await git_service.recover_post_approval_operation(
            thread_id, pending_operation, phase=phase,
        )
        result_data = result.model_dump(mode="json")
        await _publish_git_recovery_event(
            services, thread_id, WorkflowEventType.TOOL_COMPLETED,
            operation=operation, tool_name=tool_name, status=EventStatus.COMPLETED,
            data={"recovery": True, "existing": bool(result_data.get("existing"))},
        )
        updates = (
            git_commit_state_updates(snapshot.values, result_data, recovery=True)
            if operation == "git_commit"
            else {
                "git_promotion_state": "completed",
                "git_promotion_result_commit": result_data.get("result_commit"),
                "git_promotion_strategy": result_data.get("strategy"),
                "git_head_commit": result_data.get("result_commit"),
                "pending_operation": None,
                "pending_tool_name": None,
                "pending_tool_arguments": None,
                "pending_approval_preview": None,
                "pending_approval_status": "none",
            }
        )
        graph_result = await services.persistence.continue_after_recovered_git_operation(
            thread_id,
            updates,
            as_node=(
                "execute_git_commit" if operation == "git_commit"
                else "execute_git_promotion"
            ),
        )
        record_result = getattr(services.runner, "record_recovery_result", None)
        if record_result is not None:
            await record_result(thread_id, graph_result)
    except GitPostApprovalRecoveryUnavailable as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except GitError as exc:
        await _publish_git_recovery_event(
            services, thread_id, WorkflowEventType.TOOL_FAILED,
            operation=operation, tool_name=tool_name, status=EventStatus.FAILED,
            data={"recovery": True, "error_code": exc.code},
        )
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return GitPendingOperationRecoveryResponse(
        thread_id=thread_id,
        accepted=True,
        operation=operation,  # type: ignore[arg-type]
        tool_name=tool_name,
        status="completed" if not graph_result.interrupted else "running",
        existing=bool(result_data.get("existing")),
        result_commit=result_data.get("commit") or result_data.get("result_commit"),
    )


async def _resolve(
    *,
    thread_id: str,
    body: ApprovalRequest,
    approved: bool,
    services: ApiServices,
) -> ApprovalResponse:
    graph_git_approval = False
    try:
        persistence = getattr(services.query, "persistence", None)
        if persistence is not None:
            raw_snapshot = await persistence.get_snapshot(thread_id)
            graph_git_approval = bool(
                raw_snapshot.interrupts
                and raw_snapshot.values.get("pending_operation") in {"git_commit", "git_merge"}
            )
        else:
            snapshot = await services.query.get_snapshot(thread_id)
            graph_git_approval = bool(
                snapshot.interrupted
                and snapshot.pending_operation in {"git_commit", "git_merge"}
            )
    except WorkflowNotFoundError:
        pass
    git_service = getattr(services, "git", None)
    if git_service is not None and not graph_git_approval:
        promotion = await git_service.get_git_promotion(thread_id)
        if promotion.state == "awaiting_approval" and promotion.approval_id:
            try:
                if approved:
                    await _publish_git_promotion_approval_event(
                        services,
                        thread_id,
                        WorkflowEventType.WORKFLOW_RESUMED,
                        status=EventStatus.RUNNING,
                        data={
                            "reason": "approval",
                            "operation": "git_merge",
                            "tool_name": "git__merge_workflow_branch",
                        },
                    )
                    await _publish_git_promotion_approval_event(
                        services,
                        thread_id,
                        WorkflowEventType.APPROVAL_GRANTED,
                        status=EventStatus.COMPLETED,
                        data={
                            "operation": "git_merge",
                            "tool_name": "git__merge_workflow_branch",
                        },
                    )
                    await _publish_git_tool_event(
                        services, thread_id, WorkflowEventType.TOOL_STARTED,
                        tool="approve_promotion", status=EventStatus.RUNNING,
                    )
                    await git_service.approve_git_promotion(thread_id, actor="user")
                    await _publish_git_tool_event(
                        services, thread_id, WorkflowEventType.TOOL_COMPLETED,
                        tool="approve_promotion", status=EventStatus.COMPLETED,
                    )
                    project_id = await services.query.get_snapshot(thread_id)
                    await _publish_git_tool_event(
                        services, thread_id, WorkflowEventType.TOOL_STARTED,
                        tool="merge_workflow_branch", status=EventStatus.RUNNING,
                    )
                    result = await git_service.merge_git_workflow_branch(
                        project_id.project_name, workflow_id=thread_id,
                        approval_id=promotion.approval_id, actor="user",
                    )
                    await _publish_git_tool_event(
                        services, thread_id, WorkflowEventType.TOOL_COMPLETED,
                        tool="merge_workflow_branch", status=EventStatus.COMPLETED,
                    )
                else:
                    await git_service.reject_git_promotion(
                        thread_id, reason=body.reason, actor="user"
                    )
                    result = None
            except GitError as exc:
                raise HTTPException(status_code=409, detail=exc.code) from exc
            return ApprovalResponse(
                thread_id=thread_id, accepted=True, operation="git_merge",
                tool_name="git__merge_workflow_branch",
                status="completed" if result is not None else "rejected",
            )
    if git_service is not None and not graph_git_approval:
        git_preview = await git_service.pending_commit_preview(thread_id)
        if git_preview is not None:
            try:
                result = await git_service.resolve_commit_approval(
                    thread_id, approved=approved, reason=body.reason, actor="user"
                )
            except GitError as exc:
                raise HTTPException(status_code=409, detail=exc.code) from exc
            return ApprovalResponse(
                thread_id=thread_id,
                accepted=True,
                operation="git_commit",
                tool_name="git__commit",
                status="completed" if result is not None else "rejected",
            )
    try:
        response = await services.approvals.resolve(
            thread_id=thread_id,
            approved=approved,
            reason=body.reason,
        )
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ApprovalConflictError as exc:
        logger.info("approval conflict thread=%s", thread_id)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "approval accepted thread=%s operation=%s approved=%s",
        thread_id,
        response.operation,
        approved,
    )
    return response


@router.post(
    "/{thread_id}/approve",
    response_model=ApprovalResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_workflow(
    thread_id: str,
    body: ApprovalRequest,
    services: ApiServices = Depends(get_services),
) -> ApprovalResponse:
    return await _resolve(
        thread_id=thread_id,
        body=body,
        approved=True,
        services=services,
    )


@router.post(
    "/{thread_id}/reject",
    response_model=ApprovalResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reject_workflow(
    thread_id: str,
    body: ApprovalRequest,
    services: ApiServices = Depends(get_services),
) -> ApprovalResponse:
    return await _resolve(
        thread_id=thread_id,
        body=body,
        approved=False,
        services=services,
    )
