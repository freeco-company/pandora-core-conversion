"""Conversion module HTTP routes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.internal import require_internal_secret
from app.auth.jwt_verifier import VerifiedClaims
from app.auth.middleware import require_jwt
from app.conversion import lifecycle, service
from app.conversion.schemas import (
    EventIngestRequest,
    EventIngestResponse,
    FranchiseeQualifyRequest,
    FunnelMetricsResponse,
    FunnelStageMetric,
    InternalEventIngestRequest,
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


@router.post(
    "/internal/events",
    response_model=EventIngestResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal_secret)],
)
async def ingest_event_internal(
    payload: InternalEventIngestRequest,
    session: AsyncSession = Depends(get_session),
) -> EventIngestResponse:
    """Service-to-service event ingest (HMAC).

    Used by App backends (e.g. dodo) to publish events on behalf of a user
    without round-tripping the user's platform JWT. The body MUST carry
    `pandora_user_uuid`.
    """
    async with session.begin():
        event, transition = await service.ingest_event_internal(session, payload)
    return EventIngestResponse(
        id=event.id,
        lifecycle_transition=transition.to_status if transition.fired else None,
    )


@router.post(
    "/internal/admin/users/{uuid}/qualify-franchisee",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal_secret)],
)
async def qualify_franchisee(
    uuid: UUID,
    payload: FranchiseeQualifyRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Admin endpoint — manually mark a user as `franchisee`.

    ADR-003 §7.1：fairysalebox 對接延後，加盟資格目前由婕樂纖團隊人工確認後
    呼叫本端點完成 lifecycle 升級。後續接 fairysalebox 自動化時，可保留本端點
    作 fallback / 對帳路徑。
    """
    metadata = {
        "source": "admin_qualify_franchisee",
        "qualified_at": datetime.utcnow().isoformat(),
    }
    if payload.plan_chosen:
        metadata["plan_chosen"] = payload.plan_chosen
    if payload.note:
        metadata["note"] = payload.note
    async with session.begin():
        transition = await lifecycle.force_transition(
            session, uuid, "franchisee", metadata=metadata
        )
    return {
        "id": transition.id,
        "from_status": transition.from_status,
        "to_status": transition.to_status,
    }


@router.get(
    "/funnel/metrics",
    response_model=FunnelMetricsResponse,
    dependencies=[Depends(require_internal_secret)],
)
async def funnel_metrics(
    session: AsyncSession = Depends(get_session),
) -> FunnelMetricsResponse:
    """Lifecycle stage counts (current status per uuid).

    v1: counts users that have at least one transition row. Visitors that
    never registered an event are not represented.
    """
    counts = await service.funnel_metrics(session)
    stages = [
        FunnelStageMetric(status=s, count=counts.get(s, 0))
        for s in lifecycle.STATES
    ]
    total = sum(counts.values())
    return FunnelMetricsResponse(
        stages=stages,
        total_users_with_lifecycle=total,
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
