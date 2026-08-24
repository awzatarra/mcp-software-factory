from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import ApiServices, get_services
from api.knowledge_models import RejectLearningRequest
from api.services.knowledge_service import KnowledgeConflictError, KnowledgeNotFoundError, KnowledgeService


router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def service(services: ApiServices) -> KnowledgeService:
    if services.knowledge is None:
        raise HTTPException(503, "Knowledge service is unavailable")
    return services.knowledge


def translate_error(exc: Exception) -> None:
    if isinstance(exc, KnowledgeNotFoundError):
        raise HTTPException(404, "Knowledge record not found") from exc
    if isinstance(exc, KnowledgeConflictError):
        raise HTTPException(409, str(exc)) from exc
    raise exc


@router.get("")
async def list_knowledge(status: str | None = None, project_id: str | None = None,
                         knowledge_type: str | None = None, limit: int = Query(100, ge=1, le=500),
                         offset: int = Query(0, ge=0), services: ApiServices = Depends(get_services)):
    items = await service(services).store.list(status=status, project_id=project_id,
                                               knowledge_type=knowledge_type, limit=limit, offset=offset)
    return {"items": items, "count": len(items)}


@router.get("/candidates")
async def candidates(services: ApiServices = Depends(get_services)):
    items = await service(services).store.list(status="candidate", limit=500)
    return {"items": items, "count": len(items)}


@router.get("/sources")
async def sources(services: ApiServices = Depends(get_services)):
    items = await service(services).store.sources()
    return {"items": items, "count": len(items)}


@router.get("/retrievals")
async def retrievals(limit: int = Query(100, ge=1, le=500), services: ApiServices = Depends(get_services)):
    items = await service(services).store.retrievals(limit)
    return {"items": items, "count": len(items)}


@router.get("/retrievals/{retrieval_id}")
async def retrieval_detail(retrieval_id: str, services: ApiServices = Depends(get_services)):
    item = await service(services).store.retrieval(retrieval_id)
    if item is None:
        raise HTTPException(404, "Knowledge retrieval not found")
    return item


@router.get("/{knowledge_id}")
async def detail(knowledge_id: str, services: ApiServices = Depends(get_services)):
    item = await service(services).store.detail(knowledge_id)
    if item is None:
        raise HTTPException(404, "Knowledge record not found")
    return item


@router.get("/{knowledge_id}/chunks")
async def chunks(knowledge_id: str, services: ApiServices = Depends(get_services)):
    knowledge = service(services)
    if await knowledge.store.get(knowledge_id) is None:
        raise HTTPException(404, "Knowledge record not found")
    items = await knowledge.store.chunks(knowledge_id)
    return {"items": items, "count": len(items)}


@router.post("/{knowledge_id}/approve")
async def approve(knowledge_id: str, services: ApiServices = Depends(get_services)):
    try:
        return await service(services).approve(knowledge_id)
    except (KnowledgeNotFoundError, KnowledgeConflictError) as exc:
        translate_error(exc)


@router.post("/{knowledge_id}/reject")
async def reject(knowledge_id: str, payload: RejectLearningRequest,
                 services: ApiServices = Depends(get_services)):
    try:
        return await service(services).reject(knowledge_id, payload.reason)
    except (KnowledgeNotFoundError, KnowledgeConflictError) as exc:
        translate_error(exc)


@router.post("/{knowledge_id}/reindex")
async def reindex(knowledge_id: str, services: ApiServices = Depends(get_services)):
    try:
        return await service(services).reindex(knowledge_id)
    except (KnowledgeNotFoundError, KnowledgeConflictError) as exc:
        translate_error(exc)
