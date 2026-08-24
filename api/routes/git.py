from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import ApiServices, get_services
from api.git_models import (
    GitBranchesResponse,
    GitCommitPrepareRequest,
    GitCommitPreview,
    GitPromotionPreview,
    GitPromotionStatus,
    GitDiffResponse,
    GitInitResponse,
    GitLogEntry,
    GitRepositoryInfo,
    GitStatusResponse,
    GitStageRequest,
    GitStageResponse,
    GitWorkflowBranchResponse,
)
from api.services.git_service import (
    GitCommandFailed,
    GitCommandTimeout,
    GitError,
    GitOperationNotAllowed,
    GitOutputLimitExceeded,
    GitPathViolation,
    GitRepositoryNotFound,
    GitApprovalRequired,
    GitApprovalStale,
    GitIdentityMissing,
    GitNothingStaged,
    GitProtectedBranchViolation,
    GitTestsNotPassed,
    GitWorkspaceDirtyConflict,
    GitPromotionConflict,
    GitPromotionStale,
    GitPromotionUnavailable,
    GitCICommitMismatch,
    GitCIPromotionBlocked,
    GitCIRequiredForPromotion,
)
from graph.persistence_service import WorkflowNotFoundError


router = APIRouter(prefix="/api/workflows", tags=["git"])


def _error(exc: GitError) -> HTTPException:
    if isinstance(exc, GitRepositoryNotFound):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, GitCommandTimeout):
        return HTTPException(status_code=504, detail=exc.code)
    if isinstance(exc, GitOutputLimitExceeded):
        return HTTPException(status_code=413, detail=exc.code)
    if isinstance(exc, (GitPathViolation, GitOperationNotAllowed)):
        return HTTPException(status_code=403, detail=exc.code)
    if isinstance(exc, (GitApprovalRequired, GitApprovalStale, GitIdentityMissing,
                        GitNothingStaged, GitProtectedBranchViolation,
                        GitPromotionConflict, GitPromotionStale,
                        GitPromotionUnavailable,
                        GitCIRequiredForPromotion, GitCIPromotionBlocked,
                        GitCICommitMismatch)):
        return HTTPException(status_code=409, detail=exc.code)
    if isinstance(exc, (GitTestsNotPassed, GitWorkspaceDirtyConflict)):
        return HTTPException(status_code=409, detail=exc.code)
    if isinstance(exc, GitCommandFailed):
        return HTTPException(status_code=422, detail=exc.code)
    return HTTPException(status_code=500, detail="git_operation_failed")


async def _project(thread_id: str, services: ApiServices) -> str:
    try:
        snapshot = await services.query.get_snapshot(thread_id)
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    if not snapshot.project_name:
        raise HTTPException(status_code=404, detail="Project not found")
    return snapshot.project_name


def _service(services: ApiServices):
    if services.git is None:
        raise HTTPException(status_code=503, detail="Git service unavailable")
    return services.git


@router.get("/{thread_id}/git", response_model=GitRepositoryInfo)
async def repository_info(
    thread_id: str,
    services: ApiServices = Depends(get_services),
) -> GitRepositoryInfo:
    project_id = await _project(thread_id, services)
    try:
        git_service = _service(services)
        info = await git_service.get_repository_info(
            project_id, workflow_id=thread_id, agent_name="API",
            allow_initial_bind=True,
        )
        if not info.is_repository:
            return info
        summary = await git_service.workflow_summary(project_id, thread_id)
        return info.model_copy(update={
            "base_branch": summary.base_branch,
            "base_commit": summary.base_commit,
            "workflow_branch": summary.workflow_branch,
            "effective_clean": getattr(summary, "effective_clean", None),
            "commits": summary.commits,
            "commit_preview": summary.commit_preview,
            "commit_status": summary.commit_status,
            "promotion": getattr(summary, "promotion", None),
            "ci_eligibility": getattr(summary, "ci_eligibility", None),
        })
    except GitError as exc:
        raise _error(exc) from exc


@router.get("/{thread_id}/git/status", response_model=GitStatusResponse)
async def repository_status(
    thread_id: str,
    services: ApiServices = Depends(get_services),
) -> GitStatusResponse:
    project_id = await _project(thread_id, services)
    try:
        return await _service(services).git_status(
            project_id, workflow_id=thread_id, agent_name="API"
        )
    except GitError as exc:
        raise _error(exc) from exc


@router.get("/{thread_id}/git/diff", response_model=GitDiffResponse)
async def repository_diff(
    thread_id: str,
    staged: bool = Query(default=False),
    services: ApiServices = Depends(get_services),
) -> GitDiffResponse:
    project_id = await _project(thread_id, services)
    try:
        return await _service(services).git_diff(
            project_id, staged=staged, workflow_id=thread_id, agent_name="API"
        )
    except GitError as exc:
        raise _error(exc) from exc


@router.get("/{thread_id}/git/log", response_model=list[GitLogEntry])
async def repository_log(
    thread_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    services: ApiServices = Depends(get_services),
) -> list[GitLogEntry]:
    project_id = await _project(thread_id, services)
    try:
        return await _service(services).git_log(
            project_id, limit=limit, workflow_id=thread_id, agent_name="API"
        )
    except GitError as exc:
        raise _error(exc) from exc


@router.get("/{thread_id}/git/branches", response_model=GitBranchesResponse)
async def repository_branches(
    thread_id: str,
    services: ApiServices = Depends(get_services),
) -> GitBranchesResponse:
    project_id = await _project(thread_id, services)
    try:
        return await _service(services).git_branches(
            project_id, workflow_id=thread_id, agent_name="API"
        )
    except GitError as exc:
        raise _error(exc) from exc


@router.post("/{thread_id}/git/init", response_model=GitInitResponse)
async def initialize_repository(
    thread_id: str, services: ApiServices = Depends(get_services),
) -> GitInitResponse:
    project_id = await _project(thread_id, services)
    try:
        return await _service(services).git_init(
            project_id, workflow_id=thread_id, agent_name="Developer"
        )
    except GitError as exc:
        raise _error(exc) from exc


@router.post("/{thread_id}/git/branch", response_model=GitWorkflowBranchResponse)
async def create_workflow_branch(
    thread_id: str, services: ApiServices = Depends(get_services),
) -> GitWorkflowBranchResponse:
    project_id = await _project(thread_id, services)
    try:
        return await _service(services).git_create_workflow_branch(
            project_id, workflow_id=thread_id, agent_name="Developer"
        )
    except GitError as exc:
        raise _error(exc) from exc


@router.post("/{thread_id}/git/stage", response_model=GitStageResponse)
async def stage_workflow_files(
    thread_id: str, body: GitStageRequest,
    services: ApiServices = Depends(get_services),
) -> GitStageResponse:
    project_id = await _project(thread_id, services)
    try:
        return await _service(services).git_stage(
            project_id, body.paths, workflow_id=thread_id, agent_name=body.actor
        )
    except GitError as exc:
        raise _error(exc) from exc


@router.post("/{thread_id}/git/commit/prepare", response_model=GitCommitPreview)
async def prepare_workflow_commit(
    thread_id: str, body: GitCommitPrepareRequest,
    services: ApiServices = Depends(get_services),
) -> GitCommitPreview:
    project_id = await _project(thread_id, services)
    try:
        return await _service(services).prepare_git_commit(
            project_id, body.message, workflow_id=thread_id, agent_name=body.actor
        )
    except GitError as exc:
        raise _error(exc) from exc


@router.post("/{thread_id}/git/promotion/prepare", response_model=GitPromotionPreview)
async def prepare_workflow_promotion(
    thread_id: str, services: ApiServices = Depends(get_services),
) -> GitPromotionPreview:
    project_id = await _project(thread_id, services)
    try:
        return await _service(services).prepare_git_promotion(
            project_id, workflow_id=thread_id, actor="user"
        )
    except GitError as exc:
        raise _error(exc) from exc


@router.get("/{thread_id}/git/promotion", response_model=GitPromotionStatus)
async def workflow_promotion(
    thread_id: str, services: ApiServices = Depends(get_services),
) -> GitPromotionStatus:
    await _project(thread_id, services)
    try:
        return await _service(services).get_git_promotion(thread_id)
    except GitError as exc:
        raise _error(exc) from exc
