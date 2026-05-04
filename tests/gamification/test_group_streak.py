"""Group-level master daily-login streak tests.

Covers:
  - Any of meal / calendar / jerosse `*.daily_login_streak_extended` bumps the
    single shared streak.
  - Same Taipei day from a second App is a no-op (current_streak unchanged).
  - Yesterday → today bump increments by 1.
  - Gap > 1 day resets to 1.
  - longest_streak tracks max of current.
  - GET /internal/group-streak/{uuid} HMAC + schema + cache + invalidation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.gamification import group_streak_service
from app.gamification.models import GroupUserDailyStreak
from app.gamification.routes import _group_streak_cache

TZ_TAIPEI = timezone(timedelta(hours=8))


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Secret": get_settings().internal_shared_secret}


def _at_taipei_midday(d_offset: int = 0) -> datetime:
    """Return UTC datetime corresponding to ~midday Taipei `d_offset` days from
    today, so day-boundary edge cases are out of the test."""
    today_taipei = datetime.now(tz=TZ_TAIPEI).date() + timedelta(days=d_offset)
    local = datetime(
        today_taipei.year,
        today_taipei.month,
        today_taipei.day,
        12,
        0,
        0,
        tzinfo=TZ_TAIPEI,
    )
    return local.astimezone(UTC)


async def _publish_streak(
    client,
    user: str,
    source_app: str,
    occurred_at: datetime,
    key_suffix: str = "",
) -> dict:
    iso_date = occurred_at.date().isoformat()
    key = f"{source_app}-streak-{user}-{iso_date}-{key_suffix}"
    return (
        await client.post(
            "/api/v1/internal/gamification/events",
            headers=_internal_headers(),
            json={
                "pandora_user_uuid": user,
                "source_app": source_app,
                "event_kind": f"{source_app}.daily_login_streak_extended",
                "idempotency_key": key,
                "occurred_at": occurred_at.isoformat(),
            },
        )
    ).json()


@pytest.fixture(autouse=True)
def _clear_cache():
    _group_streak_cache.clear()
    yield
    _group_streak_cache.clear()


async def _read_streak_row(db_session, user_uuid_str: str) -> GroupUserDailyStreak | None:
    from uuid import UUID

    stmt = select(GroupUserDailyStreak).where(
        GroupUserDailyStreak.user_uuid == UUID(user_uuid_str)
    )
    return (await db_session.execute(stmt)).scalar_one_or_none()


# ── service-level unit tests (pure logic, no HTTP) ─────────────────────


async def test_first_bump_creates_streak_1(db_session) -> None:
    user = uuid4()
    out = await group_streak_service.bump(
        db_session, user_uuid=user, source_app="meal", occurred_at=_at_taipei_midday(0)
    )
    assert out.current_streak == 1
    assert out.longest_streak == 1
    assert out.bumped is True
    assert out.reset is False
    assert out.last_seen_app == "meal"


async def test_same_day_second_app_is_noop(db_session) -> None:
    user = uuid4()
    today = _at_taipei_midday(0)
    await group_streak_service.bump(
        db_session, user_uuid=user, source_app="meal", occurred_at=today
    )
    out2 = await group_streak_service.bump(
        db_session, user_uuid=user, source_app="calendar", occurred_at=today
    )
    assert out2.current_streak == 1  # unchanged
    assert out2.bumped is False
    # last_seen_app stays as the App that originally bumped today
    assert out2.last_seen_app == "meal"


async def test_consecutive_days_increment(db_session) -> None:
    user = uuid4()
    await group_streak_service.bump(
        db_session, user_uuid=user, source_app="meal", occurred_at=_at_taipei_midday(-2)
    )
    await group_streak_service.bump(
        db_session, user_uuid=user, source_app="calendar", occurred_at=_at_taipei_midday(-1)
    )
    out3 = await group_streak_service.bump(
        db_session, user_uuid=user, source_app="jerosse", occurred_at=_at_taipei_midday(0)
    )
    assert out3.current_streak == 3
    assert out3.longest_streak == 3
    assert out3.bumped is True
    assert out3.reset is False
    assert out3.last_seen_app == "jerosse"


async def test_gap_resets_to_1_but_longest_preserved(db_session) -> None:
    user = uuid4()
    # build up to 3
    await group_streak_service.bump(
        db_session, user_uuid=user, source_app="meal", occurred_at=_at_taipei_midday(-5)
    )
    await group_streak_service.bump(
        db_session, user_uuid=user, source_app="meal", occurred_at=_at_taipei_midday(-4)
    )
    await group_streak_service.bump(
        db_session, user_uuid=user, source_app="meal", occurred_at=_at_taipei_midday(-3)
    )
    # gap of 2 days (skip -2, skip -1), come back today
    out = await group_streak_service.bump(
        db_session, user_uuid=user, source_app="calendar", occurred_at=_at_taipei_midday(0)
    )
    assert out.current_streak == 1
    assert out.longest_streak == 3  # preserved
    assert out.reset is True


async def test_today_in_streak_helper(db_session) -> None:
    user = uuid4()
    await group_streak_service.bump(
        db_session, user_uuid=user, source_app="meal", occurred_at=_at_taipei_midday(0)
    )
    row = await group_streak_service.get(db_session, user)
    assert group_streak_service.today_in_streak(row) is True

    user2 = uuid4()
    await group_streak_service.bump(
        db_session, user_uuid=user2, source_app="meal", occurred_at=_at_taipei_midday(-1)
    )
    row2 = await group_streak_service.get(db_session, user2)
    assert group_streak_service.today_in_streak(row2) is False


# ── HTTP endpoint tests ─────────────────────────────────────────────────


async def test_streak_extended_via_meal_creates_master_row(client, db_engine) -> None:
    user = str(uuid4())
    body = await _publish_streak(client, user, "meal", _at_taipei_midday(0))
    assert "id" in body, body

    # read via GET endpoint
    resp = await client.get(
        f"/api/v1/internal/group-streak/{user}", headers=_internal_headers()
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_uuid"] == user
    assert body["current_streak"] == 1
    assert body["longest_streak"] == 1
    assert body["last_seen_app"] == "meal"
    assert body["today_in_streak"] is True


async def test_two_apps_same_day_dont_double_bump(client) -> None:
    user = str(uuid4())
    today = _at_taipei_midday(0)
    await _publish_streak(client, user, "meal", today)
    await _publish_streak(client, user, "calendar", today)
    await _publish_streak(client, user, "jerosse", today)

    resp = await client.get(
        f"/api/v1/internal/group-streak/{user}", headers=_internal_headers()
    )
    body = resp.json()
    # Three Apps published, but the master streak only counts one day.
    assert body["current_streak"] == 1
    # last_seen_app sticks with whichever App actually moved the streak (first).
    assert body["last_seen_app"] == "meal"


async def test_consecutive_days_via_http_increment(client) -> None:
    user = str(uuid4())
    await _publish_streak(client, user, "meal", _at_taipei_midday(-1))
    await _publish_streak(client, user, "calendar", _at_taipei_midday(0))

    resp = await client.get(
        f"/api/v1/internal/group-streak/{user}", headers=_internal_headers()
    )
    body = resp.json()
    assert body["current_streak"] == 2
    assert body["longest_streak"] == 2
    assert body["today_in_streak"] is True


async def test_get_endpoint_requires_internal_secret(client) -> None:
    user = str(uuid4())
    resp = await client.get(f"/api/v1/internal/group-streak/{user}")
    assert resp.status_code == 401


async def test_get_endpoint_synthesises_zero_for_unseen_user(client) -> None:
    user = str(uuid4())
    resp = await client.get(
        f"/api/v1/internal/group-streak/{user}", headers=_internal_headers()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_uuid"] == user
    assert body["current_streak"] == 0
    assert body["longest_streak"] == 0
    assert body["last_login_date"] is None
    assert body["last_seen_app"] is None
    assert body["today_in_streak"] is False


async def test_cache_invalidates_on_new_bump(client) -> None:
    """Read → bump → read should reflect the new streak in <30s.

    The 30s TTL would otherwise mask the bump; we explicitly invalidate on
    ingest so this never bites.
    """
    user = str(uuid4())
    # 1st read: 0 streak (synthesised)
    r1 = await client.get(
        f"/api/v1/internal/group-streak/{user}", headers=_internal_headers()
    )
    assert r1.json()["current_streak"] == 0
    # bump
    await _publish_streak(client, user, "meal", _at_taipei_midday(0))
    # 2nd read: should be 1, not the cached 0
    r2 = await client.get(
        f"/api/v1/internal/group-streak/{user}", headers=_internal_headers()
    )
    assert r2.json()["current_streak"] == 1


# ── structured logging ────────────────────────────────────────────────


async def test_bump_emits_structured_log_extended_then_same_day(
    db_session, caplog
) -> None:
    """First bump → `group_streak.bump.extended`; same-day repeat from another
    App → `group_streak.bump.same_day`. log records carry `extra` keys for
    JSON formatter / aggregator slicing."""
    import logging

    user = uuid4()
    today = _at_taipei_midday(0)

    with caplog.at_level(logging.INFO, logger="group_streak"):
        await group_streak_service.bump(
            db_session, user_uuid=user, source_app="meal", occurred_at=today
        )
        await group_streak_service.bump(
            db_session, user_uuid=user, source_app="calendar", occurred_at=today
        )

    events = [r.__dict__.get("event") for r in caplog.records if r.name == "group_streak"]
    assert "group_streak.bump.extended" in events
    assert "group_streak.bump.same_day" in events

    # extras carry slicing dimensions
    extended = next(r for r in caplog.records if r.__dict__.get("event") == "group_streak.bump.extended")
    assert extended.__dict__["source_app"] == "meal"
    assert extended.__dict__["new_streak"] == 1
    assert extended.__dict__["prev_streak"] == 0


async def test_fetch_emits_cache_miss_then_hit(client, db_session, caplog) -> None:
    """First GET is cache_miss; second within TTL is cache_hit."""
    import logging

    user = uuid4()
    await group_streak_service.bump(
        db_session,
        user_uuid=user,
        source_app="meal",
        occurred_at=_at_taipei_midday(0),
    )
    await db_session.commit()
    _group_streak_cache.clear()

    with caplog.at_level(logging.INFO, logger="group_streak"):
        await client.get(
            f"/api/v1/internal/group-streak/{user}", headers=_internal_headers()
        )
        await client.get(
            f"/api/v1/internal/group-streak/{user}", headers=_internal_headers()
        )

    events = [
        r.__dict__.get("event")
        for r in caplog.records
        if r.name == "group_streak" and r.__dict__.get("event", "").startswith("group_streak.fetch")
    ]
    assert events == ["group_streak.fetch.cache_miss", "group_streak.fetch.cache_hit"]
