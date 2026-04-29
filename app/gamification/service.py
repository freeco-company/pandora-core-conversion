"""Gamification ingest + progression service. ADR-009 §2.

Currently sync over the same Postgres ledger; future iterations may add a
webhook fan-out queue and a hot snapshot cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gamification import catalog, outbox
from app.gamification.models import (
    Achievement,
    MascotManifestEntry,
    OutfitCatalog,
    UserAchievement,
    UserOutfit,
    UserProgression,
    XpLedgerEntry,
)
from app.gamification.schemas import (
    AwardAchievementRequest,
    BootstrapLedgerEntry,
    InternalEventIngestRequest,
    MascotManifestUpsertItem,
)

# Day boundary used for daily-cap calculation. Catalog spec says "00:00 UTC+8".
TZ_UTC8 = timezone(timedelta(hours=8))


@dataclass
class IngestOutcome:
    entry: XpLedgerEntry
    progression: UserProgression
    leveled_up_to: int | None
    duplicate: bool


def _utc8_day_window(occurred_at: datetime) -> tuple[datetime, datetime]:
    """Return [start, end) UTC of the UTC+8 day containing occurred_at."""
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    local = occurred_at.astimezone(TZ_UTC8)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(UTC)
    end_utc = start_utc + timedelta(days=1)
    return start_utc, end_utc


async def _xp_already_earned_today(
    session: AsyncSession,
    *,
    user_uuid: UUID,
    event_kind: str,
    occurred_at: datetime,
) -> int:
    start, end = _utc8_day_window(occurred_at)
    stmt = select(func.coalesce(func.sum(XpLedgerEntry.xp_delta), 0)).where(
        XpLedgerEntry.pandora_user_uuid == user_uuid,
        XpLedgerEntry.event_kind == event_kind,
        XpLedgerEntry.occurred_at >= start,
        XpLedgerEntry.occurred_at < end,
    )
    res = await session.execute(stmt)
    return int(res.scalar_one() or 0)


async def _occurrence_count_today(
    session: AsyncSession,
    *,
    user_uuid: UUID,
    event_kind: str,
    occurred_at: datetime,
) -> int:
    start, end = _utc8_day_window(occurred_at)
    stmt = select(func.count(XpLedgerEntry.id)).where(
        XpLedgerEntry.pandora_user_uuid == user_uuid,
        XpLedgerEntry.event_kind == event_kind,
        XpLedgerEntry.occurred_at >= start,
        XpLedgerEntry.occurred_at < end,
    )
    res = await session.execute(stmt)
    return int(res.scalar_one() or 0)


async def _lifetime_seen(
    session: AsyncSession, *, user_uuid: UUID, event_kind: str
) -> bool:
    stmt = select(XpLedgerEntry.id).where(
        XpLedgerEntry.pandora_user_uuid == user_uuid,
        XpLedgerEntry.event_kind == event_kind,
        XpLedgerEntry.xp_delta > 0,  # ignore previously capped 0-xp rows
    ).limit(1)
    res = await session.execute(stmt)
    return res.scalar() is not None


async def _resolve_xp_delta(
    session: AsyncSession,
    *,
    user_uuid: UUID,
    rule: catalog.EventRule,
    event_kind: str,
    occurred_at: datetime,
) -> int:
    """Compute the XP this event should award after caps & diminishing."""
    base = rule.xp

    if rule.lifetime_unique and await _lifetime_seen(
        session, user_uuid=user_uuid, event_kind=event_kind
    ):
        return 0

    if rule.diminishing_after_n is not None:
        seen_today = await _occurrence_count_today(
            session, user_uuid=user_uuid, event_kind=event_kind, occurred_at=occurred_at
        )
        if seen_today >= rule.diminishing_after_n:
            base = rule.diminishing_xp if rule.diminishing_xp is not None else base

    if rule.daily_cap_xp is not None:
        already = await _xp_already_earned_today(
            session, user_uuid=user_uuid, event_kind=event_kind, occurred_at=occurred_at
        )
        remaining = max(0, rule.daily_cap_xp - already)
        base = min(base, remaining)

    return max(0, base)


async def _get_or_create_progression(
    session: AsyncSession, user_uuid: UUID
) -> UserProgression:
    stmt = select(UserProgression).where(UserProgression.pandora_user_uuid == user_uuid)
    res = await session.execute(stmt)
    row = res.scalar_one_or_none()
    if row is not None:
        return row
    row = UserProgression(
        pandora_user_uuid=user_uuid,
        total_xp=0,
        group_level=1,
        level_anchor_xp=0,
        level_name_zh="種子期",
        level_name_en="Seed",
    )
    session.add(row)
    await session.flush()
    return row


async def _apply_xp_to_progression(
    session: AsyncSession,
    progression: UserProgression,
    xp_delta: int,
    *,
    occurred_at: datetime,
) -> int | None:
    """Add xp to snapshot and return new level if a level-up happened."""
    if xp_delta <= 0:
        return None
    prev_level = progression.group_level
    progression.total_xp += xp_delta
    new_level = catalog.level_for_xp(progression.total_xp)
    if new_level > prev_level:
        progression.group_level = new_level
        progression.level_anchor_xp = catalog.xp_for_level(new_level)
        zh, en = catalog.level_name(new_level)
        progression.level_name_zh = zh
        progression.level_name_en = en
        progression.last_level_up_at = occurred_at
        await session.flush()
        return new_level
    await session.flush()
    return None


async def ingest_event_internal(
    session: AsyncSession, payload: InternalEventIngestRequest
) -> IngestOutcome:
    """Internal-secret path: trusted backends publish events on user behalf.

    Idempotent on (source_app, idempotency_key). Resolves XP via catalog rules
    (lifetime + daily cap + diminishing returns) before writing the ledger.
    """
    rule = catalog.get_event_rule(payload.event_kind)
    if rule.source_app != payload.source_app:
        raise ValueError(
            f"event_kind {payload.event_kind} belongs to source_app "
            f"{rule.source_app}, not {payload.source_app}"
        )

    # idempotency check
    dup_stmt = select(XpLedgerEntry).where(
        XpLedgerEntry.source_app == payload.source_app,
        XpLedgerEntry.idempotency_key == payload.idempotency_key,
    )
    dup = (await session.execute(dup_stmt)).scalar_one_or_none()
    if dup is not None:
        progression = await _get_or_create_progression(session, payload.pandora_user_uuid)
        return IngestOutcome(
            entry=dup,
            progression=progression,
            leveled_up_to=None,
            duplicate=True,
        )

    xp_delta = await _resolve_xp_delta(
        session,
        user_uuid=payload.pandora_user_uuid,
        rule=rule,
        event_kind=payload.event_kind,
        occurred_at=payload.occurred_at,
    )

    entry = XpLedgerEntry(
        pandora_user_uuid=payload.pandora_user_uuid,
        source_app=payload.source_app,
        event_kind=payload.event_kind,
        idempotency_key=payload.idempotency_key,
        xp_delta=xp_delta,
        occurred_at=payload.occurred_at,
        extra_metadata=payload.metadata,
    )
    session.add(entry)
    await session.flush()

    progression = await _get_or_create_progression(session, payload.pandora_user_uuid)
    leveled_up_to = await _apply_xp_to_progression(
        session, progression, xp_delta, occurred_at=payload.occurred_at
    )
    if leveled_up_to is not None:
        # ADR-009 §6 — auto-grant level-tier outfits the user just unlocked.
        # Idempotent on (uuid, code), so repeat level-ups within the same level
        # band are no-ops.
        granted_outfits = await _grant_level_unlocked_outfits(
            session, payload.pandora_user_uuid, leveled_up_to
        )

        # ADR-009 §2.2 — fan-out level-up via outbox so each App can mirror
        # group_level locally + drive its own celebration UX. We deliberately
        # don't fan-out every XP tick (would be N events per meal/card/etc);
        # level transitions are the user-perceptible milestone.
        await outbox.enqueue_event(
            session,
            event_type="gamification.level_up",
            pandora_user_uuid=payload.pandora_user_uuid,
            payload={
                "new_level": leveled_up_to,
                "total_xp": progression.total_xp,
                "level_name_zh": progression.level_name_zh,
                "level_name_en": progression.level_name_en,
                "trigger_source_app": payload.source_app,
                "trigger_event_kind": payload.event_kind,
                "trigger_ledger_id": entry.id,
                "occurred_at": payload.occurred_at.isoformat(),
            },
            ledger_id=entry.id,
        )

        # Fan out a single outfit_unlocked event per level-up batch (codes list)
        # so each App can mirror wardrobe state without polling. Skipped if no
        # new outfits crossed an unlock gate this level (e.g. LV.2 → LV.3 has
        # no outfit between LV.5 / LV.8 / LV.12 / LV.20, etc.).
        if granted_outfits:
            await outbox.enqueue_event(
                session,
                event_type="gamification.outfit_unlocked",
                pandora_user_uuid=payload.pandora_user_uuid,
                payload={
                    "codes": granted_outfits,
                    "awarded_via": "level_up",
                    "trigger_level": leveled_up_to,
                    "trigger_ledger_id": entry.id,
                    "occurred_at": payload.occurred_at.isoformat(),
                },
                ledger_id=entry.id,
            )
    return IngestOutcome(
        entry=entry,
        progression=progression,
        leveled_up_to=leveled_up_to,
        duplicate=False,
    )


async def get_progression(
    session: AsyncSession, user_uuid: UUID
) -> UserProgression | None:
    stmt = select(UserProgression).where(UserProgression.pandora_user_uuid == user_uuid)
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


@dataclass
class AwardOutcome:
    awarded: bool
    achievement: Achievement
    progression: UserProgression
    xp_delta: int
    leveled_up_to: int | None


async def award_achievement(
    session: AsyncSession, payload: AwardAchievementRequest
) -> AwardOutcome:
    """Grant an achievement and award its tier-based XP reward.

    Idempotent on (pandora_user_uuid, code) — granting the same achievement
    twice returns awarded=False with the existing progression snapshot.
    """
    # Look up the catalog row (must be seeded first via seed endpoint).
    ach_stmt = select(Achievement).where(Achievement.code == payload.code)
    achievement = (await session.execute(ach_stmt)).scalar_one_or_none()
    if achievement is None:
        raise KeyError(f"unknown achievement code: {payload.code}")

    # Idempotent grant: composite PK on user_achievements
    existing_stmt = select(UserAchievement).where(
        UserAchievement.pandora_user_uuid == payload.pandora_user_uuid,
        UserAchievement.code == payload.code,
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        progression = await _get_or_create_progression(session, payload.pandora_user_uuid)
        return AwardOutcome(
            awarded=False,
            achievement=achievement,
            progression=progression,
            xp_delta=0,
            leveled_up_to=None,
        )

    grant = UserAchievement(
        pandora_user_uuid=payload.pandora_user_uuid,
        code=payload.code,
        source_app=payload.source_app,
    )
    session.add(grant)
    await session.flush()

    # Award the tier XP via the ledger so progression and outbox stay coherent.
    xp_delta = int(achievement.xp_reward)
    progression = await _get_or_create_progression(session, payload.pandora_user_uuid)
    leveled_up_to: int | None = None
    if xp_delta > 0:
        entry = XpLedgerEntry(
            pandora_user_uuid=payload.pandora_user_uuid,
            source_app=payload.source_app,
            event_kind=f"achievement.{payload.code}",
            idempotency_key=payload.idempotency_key,
            xp_delta=xp_delta,
            occurred_at=payload.occurred_at,
            extra_metadata={"achievement_code": payload.code, "tier": achievement.tier},
        )
        session.add(entry)
        await session.flush()
        leveled_up_to = await _apply_xp_to_progression(
            session, progression, xp_delta, occurred_at=payload.occurred_at
        )
        if leveled_up_to is not None:
            granted_outfits = await _grant_level_unlocked_outfits(
                session, payload.pandora_user_uuid, leveled_up_to
            )
            await outbox.enqueue_event(
                session,
                event_type="gamification.level_up",
                pandora_user_uuid=payload.pandora_user_uuid,
                payload={
                    "new_level": leveled_up_to,
                    "total_xp": progression.total_xp,
                    "level_name_zh": progression.level_name_zh,
                    "level_name_en": progression.level_name_en,
                    "trigger_source_app": payload.source_app,
                    "trigger_event_kind": f"achievement.{payload.code}",
                    "trigger_ledger_id": entry.id,
                    "occurred_at": payload.occurred_at.isoformat(),
                },
                ledger_id=entry.id,
            )
            if granted_outfits:
                await outbox.enqueue_event(
                    session,
                    event_type="gamification.outfit_unlocked",
                    pandora_user_uuid=payload.pandora_user_uuid,
                    payload={
                        "codes": granted_outfits,
                        "awarded_via": "level_up",
                        "trigger_level": leveled_up_to,
                        "trigger_ledger_id": entry.id,
                        "occurred_at": payload.occurred_at.isoformat(),
                    },
                    ledger_id=entry.id,
                )

    # Always fan out the achievement event so apps can show the badge.
    await outbox.enqueue_event(
        session,
        event_type="gamification.achievement_awarded",
        pandora_user_uuid=payload.pandora_user_uuid,
        payload={
            "code": payload.code,
            "name": achievement.name,
            "description": achievement.description,
            "tier": achievement.tier,
            "source_app": achievement.source_app,
            "xp_reward": xp_delta,
            "occurred_at": payload.occurred_at.isoformat(),
        },
    )

    return AwardOutcome(
        awarded=True,
        achievement=achievement,
        progression=progression,
        xp_delta=xp_delta,
        leveled_up_to=leveled_up_to,
    )


async def _grant_level_unlocked_outfits(
    session: AsyncSession, user_uuid: UUID, new_level: int
) -> list[str]:
    """Grant any level-tier outfit the user newly qualifies for.

    Returns the codes that were *newly* granted (already-owned outfits skipped).
    Doesn't touch non-level tiers (those flow through manual grants).
    """
    candidates = catalog.level_unlock_outfits_up_to(new_level)
    if not candidates:
        return []
    # Pull owned codes for this user once; cheap.
    owned = (
        await session.execute(
            select(UserOutfit.code).where(UserOutfit.pandora_user_uuid == user_uuid)
        )
    ).scalars().all()
    owned_set = set(owned)
    granted: list[str] = []
    for d in candidates:
        if d.code in owned_set:
            continue
        # Catalog row may not yet exist in DB if seed wasn't run — skip silently
        # rather than 500. Apps can call /outfits/seed once at deploy.
        cat_row = (
            await session.execute(
                select(OutfitCatalog).where(OutfitCatalog.code == d.code)
            )
        ).scalar_one_or_none()
        if cat_row is None:
            continue
        session.add(
            UserOutfit(
                pandora_user_uuid=user_uuid,
                code=d.code,
                awarded_via="level_up",
            )
        )
        granted.append(d.code)
    if granted:
        await session.flush()
    return granted


async def list_user_outfits(
    session: AsyncSession, user_uuid: UUID
) -> list[UserOutfit]:
    stmt = (
        select(UserOutfit)
        .where(UserOutfit.pandora_user_uuid == user_uuid)
        .order_by(UserOutfit.awarded_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def grant_outfit_manual(
    session: AsyncSession, user_uuid: UUID, code: str, awarded_via: str = "manual"
) -> bool:
    """Grant an outfit not driven by level (streak / fp / cross_app tiers).

    Idempotent: returns True if newly granted, False if already owned.
    Raises KeyError if the outfit is not in the catalog table.
    """
    cat_row = (
        await session.execute(select(OutfitCatalog).where(OutfitCatalog.code == code))
    ).scalar_one_or_none()
    if cat_row is None:
        raise KeyError(f"unknown outfit code: {code}")
    existing = (
        await session.execute(
            select(UserOutfit).where(
                UserOutfit.pandora_user_uuid == user_uuid,
                UserOutfit.code == code,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    session.add(
        UserOutfit(pandora_user_uuid=user_uuid, code=code, awarded_via=awarded_via)
    )
    await session.flush()
    return True


@dataclass
class BootstrapOutcome:
    pandora_user_uuid: UUID
    bootstrapped: bool
    total_xp: int
    group_level: int


async def bootstrap_user_ledger(
    session: AsyncSession, entry: BootstrapLedgerEntry
) -> BootstrapOutcome:
    """Seed one user's pre-existing total_xp into the ledger as a single
    `migration.bootstrap` entry. Idempotent on the caller-side
    `source_app` + UNIQUE(idempotency_key) — second call is a no-op.

    No outbox event is enqueued here — the migration is invisible to apps
    by design (the user already had a level locally; this just makes the
    ledger agree with that state).
    """
    idempotency_key = f"migration.{entry.source_app}.bootstrap.{entry.pandora_user_uuid}"

    dup_stmt = select(XpLedgerEntry).where(
        XpLedgerEntry.source_app == entry.source_app,
        XpLedgerEntry.idempotency_key == idempotency_key,
    )
    dup = (await session.execute(dup_stmt)).scalar_one_or_none()
    if dup is not None:
        progression = await _get_or_create_progression(session, entry.pandora_user_uuid)
        return BootstrapOutcome(
            pandora_user_uuid=entry.pandora_user_uuid,
            bootstrapped=False,
            total_xp=progression.total_xp,
            group_level=progression.group_level,
        )

    occurred_at = datetime.now(tz=UTC)
    ledger_entry = XpLedgerEntry(
        pandora_user_uuid=entry.pandora_user_uuid,
        source_app=entry.source_app,
        event_kind="migration.bootstrap",
        idempotency_key=idempotency_key,
        xp_delta=entry.total_xp,
        occurred_at=occurred_at,
        extra_metadata={
            "reason": "phase_b_initial_migration",
            "source_app": entry.source_app,
        },
    )
    session.add(ledger_entry)
    await session.flush()

    progression = await _get_or_create_progression(session, entry.pandora_user_uuid)
    # Use the same _apply_xp_to_progression so level math + name are coherent
    # — but skip the outbox fan-out (apps already know the user's level).
    if entry.total_xp > 0 and progression.total_xp == 0:
        # Pristine progression: bootstrap directly to total_xp.
        progression.total_xp = entry.total_xp
        progression.group_level = catalog.level_for_xp(entry.total_xp)
        progression.level_anchor_xp = catalog.xp_for_level(progression.group_level)
        zh, en = catalog.level_name(progression.group_level)
        progression.level_name_zh = zh
        progression.level_name_en = en
        await session.flush()

    return BootstrapOutcome(
        pandora_user_uuid=entry.pandora_user_uuid,
        bootstrapped=True,
        total_xp=progression.total_xp,
        group_level=progression.group_level,
    )


async def list_mascot_manifest(
    session: AsyncSession,
    *,
    species: str | None = None,
) -> list[MascotManifestEntry]:
    stmt = select(MascotManifestEntry).order_by(
        MascotManifestEntry.species,
        MascotManifestEntry.stage,
        MascotManifestEntry.mood,
        MascotManifestEntry.outfit_code,
    )
    if species is not None:
        stmt = stmt.where(MascotManifestEntry.species == species)
    return list((await session.execute(stmt)).scalars().all())


async def seed_mascot_manifest_placeholders(session: AsyncSession) -> tuple[int, int]:
    """Seed empty placeholder rows for every (species, stage, mood, outfit) combo.

    URLs come up empty — Apps treat empty as "use local fallback sprite".
    Real CDN URLs land via /upsert when the asset pipeline ships.
    Returns (inserted_now, total_after).
    """
    inserted = 0
    for species in catalog.MASCOT_SPECIES:
        for stage in catalog.MASCOT_STAGES:
            for mood in catalog.DEFAULT_MOODS:
                # We seed with outfit_code="none"; per-outfit overrides come
                # via /upsert later. Keeping seed cheap means total ≈
                # species × stages × moods (4×5×5=100).
                existing = (
                    await session.execute(
                        select(MascotManifestEntry).where(
                            MascotManifestEntry.species == species,
                            MascotManifestEntry.stage == stage,
                            MascotManifestEntry.mood == mood,
                            MascotManifestEntry.outfit_code == "none",
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    continue
                session.add(
                    MascotManifestEntry(
                        species=species,
                        stage=stage,
                        mood=mood,
                        outfit_code="none",
                        sprite_url="",
                        animation_url="",
                    )
                )
                inserted += 1
    await session.flush()
    total = (
        await session.execute(select(func.count(MascotManifestEntry.id)))
    ).scalar_one()
    return inserted, int(total or 0)


async def upsert_mascot_manifest_entries(
    session: AsyncSession, entries: list[MascotManifestUpsertItem]
) -> tuple[int, int]:
    """Insert or update CDN URLs for one or more (species, stage, mood, outfit) combos.

    Returns (inserted, updated).
    """
    inserted = 0
    updated = 0
    for item in entries:
        existing = (
            await session.execute(
                select(MascotManifestEntry).where(
                    MascotManifestEntry.species == item.species,
                    MascotManifestEntry.stage == item.stage,
                    MascotManifestEntry.mood == item.mood,
                    MascotManifestEntry.outfit_code == item.outfit_code,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                MascotManifestEntry(
                    species=item.species,
                    stage=item.stage,
                    mood=item.mood,
                    outfit_code=item.outfit_code,
                    sprite_url=item.sprite_url,
                    animation_url=item.animation_url,
                )
            )
            inserted += 1
        else:
            changed = False
            if existing.sprite_url != item.sprite_url:
                existing.sprite_url = item.sprite_url
                changed = True
            if existing.animation_url != item.animation_url:
                existing.animation_url = item.animation_url
                changed = True
            if changed:
                updated += 1
    await session.flush()
    return inserted, updated


async def list_outfit_catalog(session: AsyncSession) -> list[OutfitCatalog]:
    stmt = select(OutfitCatalog).order_by(OutfitCatalog.tier, OutfitCatalog.code)
    return list((await session.execute(stmt)).scalars().all())


async def seed_outfit_catalog(session: AsyncSession) -> tuple[int, int]:
    """Upsert OUTFIT_CATALOG into the DB. Returns (inserted, updated)."""
    inserted = 0
    updated = 0
    for code, out_def in catalog.OUTFIT_CATALOG.items():
        existing = (
            await session.execute(select(OutfitCatalog).where(OutfitCatalog.code == code))
        ).scalar_one_or_none()
        species_compat_list = list(out_def.species_compat)
        if existing is None:
            session.add(
                OutfitCatalog(
                    code=out_def.code,
                    name=out_def.name,
                    unlock_condition=out_def.unlock_condition,
                    tier=out_def.tier,
                    species_compat=species_compat_list,
                )
            )
            inserted += 1
        else:
            changed = False
            if existing.name != out_def.name:
                existing.name = out_def.name
                changed = True
            if existing.unlock_condition != out_def.unlock_condition:
                existing.unlock_condition = out_def.unlock_condition
                changed = True
            if existing.tier != out_def.tier:
                existing.tier = out_def.tier
                changed = True
            if list(existing.species_compat or []) != species_compat_list:
                existing.species_compat = species_compat_list
                changed = True
            if changed:
                updated += 1
    await session.flush()
    return inserted, updated


async def seed_achievement_catalog(session: AsyncSession) -> tuple[int, int]:
    """Upsert the built-in catalog. Returns (inserted, updated)."""
    inserted = 0
    updated = 0
    for code, ach_def in catalog.ACHIEVEMENT_CATALOG.items():
        xp = catalog.xp_reward_for_tier(ach_def.tier)
        existing = (
            await session.execute(select(Achievement).where(Achievement.code == code))
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                Achievement(
                    code=ach_def.code,
                    name=ach_def.name,
                    description=ach_def.description,
                    source_app=ach_def.source_app,
                    tier=ach_def.tier,
                    xp_reward=xp,
                )
            )
            inserted += 1
        else:
            changed = False
            if existing.name != ach_def.name:
                existing.name = ach_def.name
                changed = True
            if existing.description != ach_def.description:
                existing.description = ach_def.description
                changed = True
            if existing.source_app != ach_def.source_app:
                existing.source_app = ach_def.source_app
                changed = True
            if existing.tier != ach_def.tier:
                existing.tier = ach_def.tier
                changed = True
            if existing.xp_reward != xp:
                existing.xp_reward = xp
                changed = True
            if changed:
                updated += 1
    await session.flush()
    return inserted, updated
