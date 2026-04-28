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
    UserProgression,
    XpLedgerEntry,
)
from app.gamification.schemas import InternalEventIngestRequest

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
