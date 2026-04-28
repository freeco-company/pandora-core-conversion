"""Service layer (business logic) for conversion module."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversion import lifecycle
from app.conversion.models import (
    ConversionEvent,
    FranchiseTrainingProgress,
    LifecycleTransition,
)
from app.conversion.schemas import EventIngestRequest, InternalEventIngestRequest


async def ingest_event_internal(
    session: AsyncSession,
    payload: InternalEventIngestRequest,
) -> tuple[ConversionEvent, lifecycle.TransitionResult]:
    """Service-to-service event ingest (HMAC-authenticated).

    Same business path as `ingest_event` but the user uuid comes from the body
    rather than a JWT subject.
    """
    event = ConversionEvent(
        pandora_user_uuid=payload.pandora_user_uuid,
        customer_id=payload.customer_id,
        app_id=payload.app_id,
        event_type=payload.event_type,
        payload=payload.payload,
        occurred_at=payload.occurred_at,
    )
    session.add(event)
    await session.flush()
    transition = await lifecycle.evaluate_event(
        session, payload.pandora_user_uuid, event
    )
    return event, transition


async def funnel_metrics(session: AsyncSession) -> dict[str, int]:
    """Count users currently in each lifecycle status.

    "Current" = the latest transition per uuid. Users without any transition
    are considered `visitor` but are NOT counted here (we only count uuids
    that have at least one transition row).
    """
    # Subquery: latest transition_id per uuid via correlated MAX.
    inner = (
        select(
            LifecycleTransition.pandora_user_uuid,
            func.max(LifecycleTransition.id).label("max_id"),
        )
        .group_by(LifecycleTransition.pandora_user_uuid)
        .subquery()
    )
    stmt = (
        select(LifecycleTransition.to_status, func.count(LifecycleTransition.id))
        .join(inner, LifecycleTransition.id == inner.c.max_id)
        .group_by(LifecycleTransition.to_status)
    )
    rows = (await session.execute(stmt)).all()
    return {status: count for status, count in rows}


async def ingest_event(
    session: AsyncSession,
    pandora_user_uuid: UUID,
    payload: EventIngestRequest,
) -> tuple[ConversionEvent, lifecycle.TransitionResult]:
    """Persist an event, then evaluate lifecycle rules.

    Both writes (event + any transition) commit in the caller's transaction.
    """
    event = ConversionEvent(
        pandora_user_uuid=pandora_user_uuid,
        customer_id=payload.customer_id,
        app_id=payload.app_id,
        event_type=payload.event_type,
        payload=payload.payload,
        occurred_at=payload.occurred_at,
    )
    session.add(event)
    await session.flush()  # populate event.id for transition.trigger_event_id

    transition = await lifecycle.evaluate_event(session, pandora_user_uuid, event)
    return event, transition


async def get_lifecycle_history(
    session: AsyncSession, pandora_user_uuid: UUID
) -> list[LifecycleTransition]:
    stmt = (
        select(LifecycleTransition)
        .where(LifecycleTransition.pandora_user_uuid == pandora_user_uuid)
        .order_by(LifecycleTransition.transitioned_at.asc(), LifecycleTransition.id.asc())
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_training_progress(
    session: AsyncSession, pandora_user_uuid: UUID
) -> list[FranchiseTrainingProgress]:
    stmt = select(FranchiseTrainingProgress).where(
        FranchiseTrainingProgress.pandora_user_uuid == pandora_user_uuid
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def upsert_training_progress(
    session: AsyncSession,
    pandora_user_uuid: UUID,
    chapter_id: str,
    *,
    completed: bool,
    quiz_score: int | None,
) -> FranchiseTrainingProgress:
    from datetime import datetime

    stmt = select(FranchiseTrainingProgress).where(
        FranchiseTrainingProgress.pandora_user_uuid == pandora_user_uuid,
        FranchiseTrainingProgress.chapter_id == chapter_id,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is None:
        existing = FranchiseTrainingProgress(
            pandora_user_uuid=pandora_user_uuid,
            chapter_id=chapter_id,
            attempts=1,
            quiz_score=quiz_score,
            completed_at=datetime.utcnow() if completed else None,
        )
        session.add(existing)
    else:
        existing.attempts = (existing.attempts or 0) + 1
        if quiz_score is not None:
            existing.quiz_score = quiz_score
        if completed and existing.completed_at is None:
            existing.completed_at = datetime.utcnow()
    await session.flush()
    return existing
