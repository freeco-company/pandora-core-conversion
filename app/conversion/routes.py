"""Conversion module HTTP routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt_verifier import VerifiedClaims
from app.auth.middleware import require_jwt
from app.conversion import lifecycle, service
from app.conversion.schemas import (
    EventIngestRequest,
    EventIngestResponse,
    LifecycleResponse,
    LifecycleTransitionItem,
    LifecycleTransitionRequest,
    TrainingProgressItem,
    TrainingProgressResponse,
    TrainingProgressUpdateRequest,
)
from app.db import get_session

router = APIRouter()


@router.post(
    "/events",
    response_model=EventIngestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_event(
    payload: EventIngestRequest,
    claims: VerifiedClaims = Depends(require_jwt),
    session: AsyncSession = Depends(get_session),
) -> EventIngestResponse:
    """Generic event ingest endpoint. Body event tied to JWT subject."""
    try:
        uuid_obj = UUID(claims.sub)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid sub uuid") from exc

    async with session.begin():
        event, transition = await service.ingest_event(session, uuid_obj, payload)
    return EventIngestResponse(
        id=event.id,
        lifecycle_transition=transition.to_status if transition.fired else None,
    )


@router.get("/users/{uuid}/lifecycle", response_model=LifecycleResponse)
async def get_lifecycle(
    uuid: UUID,
    claims: VerifiedClaims = Depends(require_jwt),
    session: AsyncSession = Depends(get_session),
) -> LifecycleResponse:
    if str(uuid) != claims.sub:
        raise HTTPException(status_code=403, detail="forbidden")
    history = await service.get_lifecycle_history(session, uuid)
    current = history[-1].to_status if history else "visitor"
    return LifecycleResponse(
        pandora_user_uuid=uuid,
        current_status=current,
        history=[
            LifecycleTransitionItem(
                from_status=t.from_status,
                to_status=t.to_status,
                transitioned_at=t.transitioned_at,
                trigger_event_id=t.trigger_event_id,
            )
            for t in history
        ],
    )


@router.post("/users/{uuid}/lifecycle/transition", status_code=status.HTTP_201_CREATED)
async def force_lifecycle_transition(
    uuid: UUID,
    payload: LifecycleTransitionRequest,
    claims: VerifiedClaims = Depends(require_jwt),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Admin / internal-triggered transition.

    v1 skeleton: requires `lifecycle:write` scope. In production this likely
    becomes a service-to-service call gated by an internal shared secret rather
    than a per-user JWT.
    """
    if "lifecycle:write" not in claims.scopes:
        raise HTTPException(status_code=403, detail="missing scope: lifecycle:write")
    try:
        transition = await lifecycle.force_transition(
            session, uuid, payload.to_status, metadata=payload.metadata
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    return {
        "id": transition.id,
        "from_status": transition.from_status,
        "to_status": transition.to_status,
    }


@router.get("/users/{uuid}/training", response_model=TrainingProgressResponse)
async def get_training(
    uuid: UUID,
    claims: VerifiedClaims = Depends(require_jwt),
    session: AsyncSession = Depends(get_session),
) -> TrainingProgressResponse:
    if str(uuid) != claims.sub:
        raise HTTPException(status_code=403, detail="forbidden")
    rows = await service.get_training_progress(session, uuid)
    return TrainingProgressResponse(
        pandora_user_uuid=uuid,
        chapters=[
            TrainingProgressItem(
                chapter_id=r.chapter_id,
                completed_at=r.completed_at,
                quiz_score=r.quiz_score,
                attempts=r.attempts,
            )
            for r in rows
        ],
    )


@router.post("/users/{uuid}/training", status_code=status.HTTP_200_OK)
async def update_training(
    uuid: UUID,
    payload: TrainingProgressUpdateRequest,
    claims: VerifiedClaims = Depends(require_jwt),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if str(uuid) != claims.sub:
        raise HTTPException(status_code=403, detail="forbidden")
    async with session.begin():
        row = await service.upsert_training_progress(
            session,
            uuid,
            payload.chapter_id,
            completed=payload.completed,
            quiz_score=payload.quiz_score,
        )
    return {
        "chapter_id": row.chapter_id,
        "completed_at": row.completed_at,
        "quiz_score": row.quiz_score,
        "attempts": row.attempts,
    }
