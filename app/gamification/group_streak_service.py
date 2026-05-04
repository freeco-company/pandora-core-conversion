"""Group-level master daily-login streak service.

One streak per Pandora Core identity uuid that spans all Apps. Bumped by any
`*.daily_login_streak_extended` event from meal / calendar / jerosse. Day
boundary is Asia/Taipei (UTC+8) to match each App's local StreakPublisher.

Logic on bump:
  last_login_date == today (Taipei) → no-op (idempotent within day)
  last_login_date == yesterday      → current_streak += 1
  otherwise                         → reset to 1

`longest_streak` tracked as max(longest, current) after every bump.

Idempotent at the (user, day) granularity: same App publishing twice on the
same day is a no-op; different Apps on the same day each see "already bumped"
and short-circuit. Re-publishing yesterday's event today (clock skew) does NOT
double-count because last_login_date already moved forward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gamification.models import GroupUserDailyStreak

# Asia/Taipei = UTC+8, no DST. Match StreakPublisher day boundary across Apps.
TZ_TAIPEI = timezone(timedelta(hours=8))


def _to_taipei_date(occurred_at: datetime) -> date:
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    return occurred_at.astimezone(TZ_TAIPEI).date()


@dataclass
class GroupStreakOutcome:
    user_uuid: UUID
    current_streak: int
    longest_streak: int
    last_login_date: date | None
    last_seen_app: str | None
    bumped: bool  # False = same-day no-op
    reset: bool   # True if streak reset to 1 (gap > 1 day)


async def bump(
    session: AsyncSession,
    *,
    user_uuid: UUID,
    source_app: str,
    occurred_at: datetime,
) -> GroupStreakOutcome:
    """Bump the master streak for one user.

    Caller passes the original event `occurred_at` so the day boundary is
    consistent with the source App's StreakPublisher (we don't use server now).
    """
    today_taipei = _to_taipei_date(occurred_at)

    stmt = select(GroupUserDailyStreak).where(
        GroupUserDailyStreak.user_uuid == user_uuid
    )
    row = (await session.execute(stmt)).scalar_one_or_none()

    if row is None:
        row = GroupUserDailyStreak(
            user_uuid=user_uuid,
            current_streak=1,
            longest_streak=1,
            last_login_date=today_taipei,
            last_seen_app=source_app,
        )
        session.add(row)
        await session.flush()
        return GroupStreakOutcome(
            user_uuid=user_uuid,
            current_streak=1,
            longest_streak=1,
            last_login_date=today_taipei,
            last_seen_app=source_app,
            bumped=True,
            reset=False,
        )

    last = row.last_login_date
    reset = False
    bumped = True

    if last == today_taipei:
        # Same Taipei day — already bumped by some App today. last_seen_app is
        # observational; we deliberately don't update it on no-op so it stays
        # the App that actually triggered today's bump.
        bumped = False
    elif last is not None and last == today_taipei - timedelta(days=1):
        row.current_streak += 1
        row.last_login_date = today_taipei
        row.last_seen_app = source_app
    else:
        # Gap (> 1 day) or first-ever after migration with last=None somehow —
        # reset to 1.
        row.current_streak = 1
        row.last_login_date = today_taipei
        row.last_seen_app = source_app
        reset = True

    if row.current_streak > row.longest_streak:
        row.longest_streak = row.current_streak

    await session.flush()

    return GroupStreakOutcome(
        user_uuid=user_uuid,
        current_streak=row.current_streak,
        longest_streak=row.longest_streak,
        last_login_date=row.last_login_date,
        last_seen_app=row.last_seen_app,
        bumped=bumped,
        reset=reset,
    )


async def get(
    session: AsyncSession, user_uuid: UUID
) -> GroupUserDailyStreak | None:
    stmt = select(GroupUserDailyStreak).where(
        GroupUserDailyStreak.user_uuid == user_uuid
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def today_in_streak(row: GroupUserDailyStreak | None, now: datetime | None = None) -> bool:
    """True iff `last_login_date == today (Taipei)`. Used by API response."""
    if row is None or row.last_login_date is None:
        return False
    n = now or datetime.now(tz=UTC)
    return row.last_login_date == _to_taipei_date(n)
