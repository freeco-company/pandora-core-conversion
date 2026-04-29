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
    AdminLifecycleOverrideRequest,
    EventIngestRequest,
    EventIngestResponse,
    FranchiseeQualifyRequest,
    FunnelMetricsResponse,
    FunnelStageMetric,
    InternalEventIngestRequest,
    LifecycleResponse,
    LifecycleTransitionItem,
    LifecycleTransitionRequest,
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
    "/internal/admin/users/{uuid}/qualify-franchisee-self-use",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal_secret)],
)
async def qualify_franchisee_self_use(
    uuid: UUID,
    payload: FranchiseeQualifyRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Admin endpoint — manually mark a user as `franchisee_self_use`.

    ADR-008 §2.3：first-order signal 由婕樂纖後台 webhook 自動推進；本端點作為
    fallback / 對帳路徑（webhook 漏掉、人工確認等情境）。
    """
    metadata: dict = {
        "source": "admin_qualify_franchisee_self_use",
        "qualified_at": datetime.utcnow().isoformat(),
    }
    if payload.plan_chosen:
        metadata["plan_chosen"] = payload.plan_chosen
    if payload.note:
        metadata["note"] = payload.note
    async with session.begin():
        transition = await lifecycle.force_transition(
            session, uuid, "franchisee_self_use", metadata=metadata
        )
    return {
        "id": transition.id,
        "from_status": transition.from_status,
        "to_status": transition.to_status,
    }


@router.post(
    "/internal/admin/users/{uuid}/lifecycle/transition",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal_secret)],
)
async def admin_lifecycle_override(
    uuid: UUID,
    payload: AdminLifecycleOverrideRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Generic admin lifecycle override (any stage → any stage).

    Used by 集團 admin tooling. Records `actor` + `reason` in transition
    metadata for audit. cache_invalidator (PG-93) auto-fires from
    `force_transition`, so consumer Apps see the new stage immediately.
    """
    metadata: dict = {
        "source": "admin_override",
        "actor": payload.actor,
        "reason": payload.reason,
        "overridden_at": datetime.utcnow().isoformat(),
    }
    try:
        async with session.begin():
            transition = await lifecycle.force_transition(
                session, uuid, payload.to_status, metadata=metadata
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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

    ADR-008 §2.2 — returns 5 stages (was 6 in ADR-003).
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
