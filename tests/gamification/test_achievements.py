"""Tests for the achievement grant flow."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.config import get_settings
from app.gamification.models import (
    Achievement,
    GamificationOutboxEvent,
    UserAchievement,
    XpLedgerEntry,
)


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Secret": get_settings().internal_shared_secret}


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


# ── seed ────────────────────────────────────────────────────────────────


async def test_seed_inserts_full_catalog(client) -> None:
    resp = await client.post(
        "/api/v1/internal/gamification/achievements/seed",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["inserted"] >= 9  # built-in catalog size
    assert body["updated"] == 0
    assert body["total"] == body["inserted"]


async def test_seed_is_idempotent(client) -> None:
    first = await client.post(
        "/api/v1/internal/gamification/achievements/seed",
        headers=_internal_headers(),
    )
    second = await client.post(
        "/api/v1/internal/gamification/achievements/seed",
        headers=_internal_headers(),
    )
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["inserted"] == 0
    assert second.json()["updated"] == 0


async def test_seed_requires_internal_secret(client) -> None:
    resp = await client.post("/api/v1/internal/gamification/achievements/seed")
    assert resp.status_code == 401


# ── award ──────────────────────────────────────────────────────────────


async def test_award_inserts_user_achievement_and_credits_tier_xp(client, db_session):
    # Pre-seed the catalog so award has something to look up
    await client.post(
        "/api/v1/internal/gamification/achievements/seed",
        headers=_internal_headers(),
    )

    user = str(uuid4())
    resp = await client.post(
        "/api/v1/internal/gamification/achievements/award",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": user,
            "code": "dodo.streak_7",
            "source_app": "dodo",
            "idempotency_key": f"ach-{user}-streak_7",
            "occurred_at": _now(),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["awarded"] is True
    assert body["code"] == "dodo.streak_7"
    assert body["tier"] == "silver"
    assert body["xp_delta"] == 100  # silver tier
    assert body["total_xp"] == 100
    assert body["group_level"] == 2  # 100 XP crosses LV.2 (50 anchor)


async def test_award_is_idempotent_on_uuid_code(client) -> None:
    await client.post(
        "/api/v1/internal/gamification/achievements/seed",
        headers=_internal_headers(),
    )
    user = str(uuid4())
    payload = {
        "pandora_user_uuid": user,
        "code": "dodo.first_meal",
        "source_app": "dodo",
        "idempotency_key": f"ach-{user}-first_meal",
        "occurred_at": _now(),
    }
    r1 = await client.post(
        "/api/v1/internal/gamification/achievements/award",
        headers=_internal_headers(),
        json=payload,
    )
    assert r1.status_code == 201
    assert r1.json()["awarded"] is True

    r2 = await client.post(
        "/api/v1/internal/gamification/achievements/award",
        headers=_internal_headers(),
        json={**payload, "idempotency_key": f"ach-{user}-first_meal-retry"},
    )
    assert r2.status_code == 201
    body = r2.json()
    assert body["awarded"] is False
    assert body["xp_delta"] == 0
    assert body["total_xp"] == 30  # bronze, unchanged


async def test_award_unknown_code_returns_404(client) -> None:
    user = str(uuid4())
    resp = await client.post(
        "/api/v1/internal/gamification/achievements/award",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": user,
            "code": "dodo.never_existed",
            "source_app": "dodo",
            "idempotency_key": f"ach-{user}-x",
            "occurred_at": _now(),
        },
    )
    assert resp.status_code == 404


async def test_award_requires_internal_secret(client) -> None:
    resp = await client.post(
        "/api/v1/internal/gamification/achievements/award",
        json={
            "pandora_user_uuid": str(uuid4()),
            "code": "dodo.first_meal",
            "source_app": "dodo",
            "idempotency_key": "x",
            "occurred_at": _now(),
        },
    )
    assert resp.status_code == 401


async def test_award_writes_ledger_entry_and_progression(client, db_session) -> None:
    await client.post(
        "/api/v1/internal/gamification/achievements/seed",
        headers=_internal_headers(),
    )
    user_uuid = uuid4()  # for queries below

    # Use a known UUID we can query for
    resp = await client.post(
        "/api/v1/internal/gamification/achievements/award",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": str(user_uuid),
            "code": "dodo.streak_30",  # gold tier = 300 XP
            "source_app": "dodo",
            "idempotency_key": f"ach-{user_uuid}-streak_30",
            "occurred_at": _now(),
        },
    )
    assert resp.status_code == 201
    assert resp.json()["xp_delta"] == 300

    # Verify side effects via direct DB query
    grants = (
        await db_session.execute(
            select(UserAchievement).where(UserAchievement.pandora_user_uuid == user_uuid)
        )
    ).scalars().all()
    assert len(grants) == 1
    assert grants[0].code == "dodo.streak_30"

    ledger = (
        await db_session.execute(
            select(XpLedgerEntry).where(XpLedgerEntry.pandora_user_uuid == user_uuid)
        )
    ).scalars().all()
    assert len(ledger) == 1
    assert ledger[0].event_kind == "achievement.dodo.streak_30"
    assert ledger[0].xp_delta == 300


async def test_award_enqueues_achievement_awarded_outbox_event(
    client, db_session, monkeypatch
):
    monkeypatch.setenv("GAMIFICATION_CONSUMER_DODO_URL", "https://dodo.test/webhook")
    monkeypatch.setenv("GAMIFICATION_CONSUMER_DODO_SECRET", "x")
    monkeypatch.setattr(
        get_settings(), "gamification_consumers", "dodo", raising=False
    )
    await client.post(
        "/api/v1/internal/gamification/achievements/seed",
        headers=_internal_headers(),
    )
    user = uuid4()
    await client.post(
        "/api/v1/internal/gamification/achievements/award",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": str(user),
            "code": "dodo.first_meal",  # bronze 30 XP — no level-up at this XP
            "source_app": "dodo",
            "idempotency_key": f"ach-{user}-first_meal",
            "occurred_at": _now(),
        },
    )

    rows = (
        await db_session.execute(
            select(GamificationOutboxEvent).where(
                GamificationOutboxEvent.pandora_user_uuid == user
            )
        )
    ).scalars().all()
    achievement_events = [r for r in rows if r.event_type == "gamification.achievement_awarded"]
    assert len(achievement_events) == 1
    assert achievement_events[0].payload["code"] == "dodo.first_meal"
    assert achievement_events[0].payload["tier"] == "bronze"


async def test_award_with_level_up_also_enqueues_level_up_event(
    client, db_session, monkeypatch
):
    monkeypatch.setenv("GAMIFICATION_CONSUMER_DODO_URL", "https://dodo.test/webhook")
    monkeypatch.setenv("GAMIFICATION_CONSUMER_DODO_SECRET", "x")
    monkeypatch.setattr(
        get_settings(), "gamification_consumers", "dodo", raising=False
    )
    await client.post(
        "/api/v1/internal/gamification/achievements/seed",
        headers=_internal_headers(),
    )
    user = uuid4()
    await client.post(
        "/api/v1/internal/gamification/achievements/award",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": str(user),
            "code": "group.full_constellation",  # legendary 1000 XP → LV.10 area
            "source_app": "group",
            "idempotency_key": f"ach-{user}-constellation",
            "occurred_at": _now(),
        },
    )

    rows = (
        await db_session.execute(
            select(GamificationOutboxEvent).where(
                GamificationOutboxEvent.pandora_user_uuid == user
            )
        )
    ).scalars().all()
    types = {r.event_type for r in rows}
    assert "gamification.achievement_awarded" in types
    assert "gamification.level_up" in types


async def test_seed_updates_existing_when_catalog_changes(client, db_session, monkeypatch):
    await client.post(
        "/api/v1/internal/gamification/achievements/seed",
        headers=_internal_headers(),
    )
    # Mutate one catalog entry's tier in-process to simulate a catalog edit
    from app.gamification import catalog
    original = catalog.ACHIEVEMENT_CATALOG["dodo.first_meal"]
    monkeypatch.setitem(
        catalog.ACHIEVEMENT_CATALOG,
        "dodo.first_meal",
        catalog.AchievementDef(
            code=original.code,
            name="第一餐 (修訂)",
            description=original.description,
            source_app=original.source_app,
            tier="silver",  # bumped
        ),
    )
    resp = await client.post(
        "/api/v1/internal/gamification/achievements/seed",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] >= 1

    row = (
        await db_session.execute(
            select(Achievement).where(Achievement.code == "dodo.first_meal")
        )
    ).scalar_one()
    assert row.tier == "silver"
    assert row.xp_reward == 100  # silver tier
    assert row.name == "第一餐 (修訂)"
