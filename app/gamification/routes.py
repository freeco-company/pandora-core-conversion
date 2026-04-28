"""Gamification HTTP routes. ADR-009 §2."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.internal import require_internal_secret
from app.db import get_session
from app.gamification import catalog, service
from app.gamification.schemas import (
    EventIngestResponse,
    InternalEventIngestRequest,
    ProgressionResponse,
)

router = APIRouter()


def _progression_to_response(progression) -> ProgressionResponse:
    return ProgressionResponse(
        pandora_user_uuid=progression.pandora_user_uuid,
        total_xp=progression.total_xp,
        group_level=progression.group_level,
        level_name_zh=progression.level_name_zh,
        level_name_en=progression.level_name_en,
        level_anchor_xp=progression.level_anchor_xp,
        xp_to_next_level=catalog.xp_to_next_level(progression.total_xp),
        last_level_up_at=progression.last_level_up_at,
        updated_at=progression.updated_at,
    )


@router.post(
    "/internal/gamification/events",
    response_model=EventIngestResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal_secret)],
)
async def ingest_event_internal(
    payload: InternalEventIngestRequest,
    session: AsyncSession = Depends(get_session),
) -> EventIngestResponse:
    """Service-to-service event ingest (HMAC).

    App backends publish events here when a user performs a tracked action.
    Idempotent on (source_app, idempotency_key) — safe to retry.
    """
    try:
        async with session.begin():
            outcome = await service.ingest_event_internal(session, payload)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="unknown event_kind") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return EventIngestResponse(
        id=outcome.entry.id,
        xp_delta=outcome.entry.xp_delta,
        total_xp=outcome.progression.total_xp,
        group_level=outcome.progression.group_level,
        leveled_up_to=outcome.leveled_up_to,
        duplicate=outcome.duplicate,
    )


@router.get(
    "/internal/gamification/progression/{uuid}",
    response_model=ProgressionResponse,
    dependencies=[Depends(require_internal_secret)],
)
async def get_progression(
    uuid: UUID,
    session: AsyncSession = Depends(get_session),
) -> ProgressionResponse:
    """Internal: read snapshot. Used by App backends as JIT fallback (ADR-009 §2.2)."""
    progression = await service.get_progression(session, uuid)
    if progression is None:
        # not yet bootstrapped — synthesise a LV.1 baseline rather than 404
        return ProgressionResponse(
            pandora_user_uuid=uuid,
            total_xp=0,
            group_level=1,
            level_name_zh="種子期",
            level_name_en="Seed",
            level_anchor_xp=0,
            xp_to_next_level=catalog.xp_for_level(2),
        )
    return _progression_to_response(progression)
