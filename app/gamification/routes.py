"""Gamification HTTP routes. ADR-009 §2."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.internal import require_internal_secret
from app.db import get_session
from app.gamification import catalog, outbox, service
from app.gamification.schemas import (
    AwardAchievementRequest,
    AwardAchievementResponse,
    EventIngestResponse,
    GrantOutfitRequest,
    GrantOutfitResponse,
    InternalEventIngestRequest,
    MascotManifestItem,
    MascotManifestResponse,
    MascotManifestUpsertRequest,
    MascotManifestUpsertResponse,
    OutfitCatalogResponse,
    OutfitItem,
    ProgressionResponse,
    SeedAchievementsResponse,
    SeedMascotManifestResponse,
    SeedOutfitsResponse,
    UserOutfitItem,
    UserOutfitsResponse,
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


@router.post(
    "/internal/gamification/achievements/award",
    response_model=AwardAchievementResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal_secret)],
)
async def award_achievement(
    payload: AwardAchievementRequest,
    session: AsyncSession = Depends(get_session),
) -> AwardAchievementResponse:
    """Grant an achievement (idempotent on (uuid, code)).

    The achievement must already exist in the catalog table — call
    `POST /internal/gamification/achievements/seed` first (or via deploy
    script) to populate from `catalog.ACHIEVEMENT_CATALOG`.
    """
    try:
        async with session.begin():
            outcome = await service.award_achievement(session, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return AwardAchievementResponse(
        awarded=outcome.awarded,
        code=outcome.achievement.code,
        tier=outcome.achievement.tier,
        xp_delta=outcome.xp_delta,
        total_xp=outcome.progression.total_xp,
        group_level=outcome.progression.group_level,
        leveled_up_to=outcome.leveled_up_to,
    )


@router.post(
    "/internal/gamification/achievements/seed",
    response_model=SeedAchievementsResponse,
    dependencies=[Depends(require_internal_secret)],
)
async def seed_achievements(
    session: AsyncSession = Depends(get_session),
) -> SeedAchievementsResponse:
    """Upsert the built-in achievement catalog into the DB. Idempotent — safe
    to run on every deploy.
    """
    async with session.begin():
        inserted, updated = await service.seed_achievement_catalog(session)
    return SeedAchievementsResponse(
        inserted=inserted,
        updated=updated,
        total=len(catalog.ACHIEVEMENT_CATALOG),
    )


@router.get(
    "/internal/gamification/outfits",
    response_model=OutfitCatalogResponse,
    dependencies=[Depends(require_internal_secret)],
)
async def list_outfits(
    session: AsyncSession = Depends(get_session),
) -> OutfitCatalogResponse:
    """List the seeded outfit catalog. Apps fetch this at startup to render
    UX hints ("unlock at LV.5") and the equip picker.
    """
    rows = await service.list_outfit_catalog(session)
    return OutfitCatalogResponse(
        outfits=[
            OutfitItem(
                code=r.code,
                name=r.name,
                unlock_condition=r.unlock_condition,
                tier=r.tier,
                species_compat=list(r.species_compat or []),
            )
            for r in rows
        ],
        total=len(rows),
    )


@router.post(
    "/internal/gamification/outfits/seed",
    response_model=SeedOutfitsResponse,
    dependencies=[Depends(require_internal_secret)],
)
async def seed_outfits(
    session: AsyncSession = Depends(get_session),
) -> SeedOutfitsResponse:
    """Upsert the built-in OUTFIT_CATALOG into DB. Idempotent."""
    async with session.begin():
        inserted, updated = await service.seed_outfit_catalog(session)
    return SeedOutfitsResponse(
        inserted=inserted,
        updated=updated,
        total=len(catalog.OUTFIT_CATALOG),
    )


@router.get(
    "/internal/gamification/users/{uuid}/outfits",
    response_model=UserOutfitsResponse,
    dependencies=[Depends(require_internal_secret)],
)
async def list_user_outfits(
    uuid: UUID,
    session: AsyncSession = Depends(get_session),
) -> UserOutfitsResponse:
    rows = await service.list_user_outfits(session, uuid)
    return UserOutfitsResponse(
        pandora_user_uuid=uuid,
        outfits=[
            UserOutfitItem(
                code=r.code,
                awarded_at=r.awarded_at,
                awarded_via=r.awarded_via,
            )
            for r in rows
        ],
        total=len(rows),
    )


@router.post(
    "/internal/gamification/users/{uuid}/outfits/grant",
    response_model=GrantOutfitResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal_secret)],
)
async def grant_user_outfit(
    uuid: UUID,
    payload: GrantOutfitRequest,
    session: AsyncSession = Depends(get_session),
) -> GrantOutfitResponse:
    """Manually grant an outfit. Idempotent on (uuid, code).

    For non-level tiers — streak, fp_lifetime, cross-app — Apps call this
    when their own detection fires.
    """
    try:
        async with session.begin():
            granted = await service.grant_outfit_manual(
                session, uuid, payload.code, awarded_via=payload.awarded_via
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return GrantOutfitResponse(granted=granted, code=payload.code)


@router.get(
    "/internal/gamification/mascot-manifest",
    response_model=MascotManifestResponse,
    dependencies=[Depends(require_internal_secret)],
)
async def get_mascot_manifest(
    species: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> MascotManifestResponse:
    """List all mascot asset URLs (optionally filtered by species).

    Apps cache this at startup and refresh on a long TTL. Empty `sprite_url` /
    `animation_url` means the asset isn't yet uploaded — Apps should fall back
    to a local default sprite for that combination.
    """
    rows = await service.list_mascot_manifest(session, species=species)
    return MascotManifestResponse(
        entries=[
            MascotManifestItem(
                species=r.species,
                stage=r.stage,
                mood=r.mood,
                outfit_code=r.outfit_code,
                sprite_url=r.sprite_url,
                animation_url=r.animation_url,
                updated_at=r.updated_at,
            )
            for r in rows
        ],
        total=len(rows),
    )


@router.post(
    "/internal/gamification/mascot-manifest/seed",
    response_model=SeedMascotManifestResponse,
    dependencies=[Depends(require_internal_secret)],
)
async def seed_mascot_manifest(
    session: AsyncSession = Depends(get_session),
) -> SeedMascotManifestResponse:
    """Bootstrap empty rows for every (species, stage, mood) with
    outfit_code='none'. URLs are blank — fill in via /upsert when assets are
    ready. Idempotent.
    """
    async with session.begin():
        inserted, total = await service.seed_mascot_manifest_placeholders(session)
    return SeedMascotManifestResponse(inserted=inserted, total=total)


@router.post(
    "/internal/gamification/mascot-manifest/upsert",
    response_model=MascotManifestUpsertResponse,
    dependencies=[Depends(require_internal_secret)],
)
async def upsert_mascot_manifest(
    payload: MascotManifestUpsertRequest,
    session: AsyncSession = Depends(get_session),
) -> MascotManifestUpsertResponse:
    """Upsert CDN URLs for one or more (species, stage, mood, outfit) combos.

    Used by the asset pipeline / ui-designer console after uploading new
    sprites. Idempotent — same input twice is a no-op.
    """
    async with session.begin():
        inserted, updated = await service.upsert_mascot_manifest_entries(
            session, payload.entries
        )
    return MascotManifestUpsertResponse(
        inserted=inserted,
        updated=updated,
        total_in_request=len(payload.entries),
    )


@router.post(
    "/internal/gamification/outbox/dispatch",
    dependencies=[Depends(require_internal_secret)],
)
async def dispatch_outbox(
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Manual / cron-driven dispatch of pending outbox rows.

    Runs synchronously over the request — fine for cron + small batches. A
    proper background worker (Phase A.2.1) can replace this with periodic
    `dispatch_pending` calls.
    """
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=422, detail="limit must be in 1..1000")
    async with session.begin():
        summary = await outbox.dispatch_pending(session, limit=limit)
    return summary
