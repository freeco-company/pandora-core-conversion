"""Tests for the JIT user sync snapshot endpoint (webhook-gap fallback)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


def _internal_headers() -> dict[str, str]:
    from app.config import get_settings

    return {"X-Internal-Secret": get_settings().internal_shared_secret}


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


async def _seed_catalogs(client) -> None:
    await client.post(
        "/api/v1/internal/gamification/achievements/seed", headers=_internal_headers()
    )
    await client.post(
        "/api/v1/internal/gamification/outfits/seed", headers=_internal_headers()
    )


async def test_sync_unseen_user_returns_baseline(client) -> None:
    user = uuid4()
    resp = await client.get(
        f"/api/v1/internal/gamification/users/{user}/sync",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pandora_user_uuid"] == str(user)
    assert body["progression"]["group_level"] == 1
    assert body["progression"]["total_xp"] == 0
    assert body["achievements"] == []
    assert body["outfits"] == []


async def test_sync_returns_progression_achievements_and_outfits(client) -> None:
    await _seed_catalogs(client)
    user = uuid4()
    headers = _internal_headers()

    # Push enough events to cross LV.5 → auto-grant scarf
    for i in range(2):
        await client.post(
            "/api/v1/internal/gamification/events",
            headers=headers,
            json={
                "pandora_user_uuid": str(user),
                "source_app": "jerosse",
                "event_kind": "jerosse.referral_signed",
                "idempotency_key": f"sync-ref-{user}-{i}",
                "occurred_at": _now(),
            },
        )

    # Award an achievement
    await client.post(
        "/api/v1/internal/gamification/achievements/award",
        headers=headers,
        json={
            "pandora_user_uuid": str(user),
            "code": "meal.streak_7",
            "source_app": "meal",
            "idempotency_key": f"ach-{user}",
            "occurred_at": _now(),
        },
    )

    resp = await client.get(
        f"/api/v1/internal/gamification/users/{user}/sync",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["progression"]["group_level"] >= 5
    assert body["progression"]["total_xp"] >= 300

    ach_codes = {a["code"] for a in body["achievements"]}
    assert "meal.streak_7" in ach_codes
    # tier comes from the joined Achievement row
    streak_row = next(a for a in body["achievements"] if a["code"] == "meal.streak_7")
    assert streak_row["tier"] in {"bronze", "silver", "gold", "platinum"}
    assert streak_row["source_app"] == "meal"

    outfit_codes = {o["code"] for o in body["outfits"]}
    assert "scarf" in outfit_codes


async def test_sync_requires_internal_secret(client) -> None:
    user = uuid4()
    resp = await client.get(f"/api/v1/internal/gamification/users/{user}/sync")
    assert resp.status_code == 401


async def test_sync_progression_only_when_no_grants(client) -> None:
    """User with progression but no achievements/outfits returns shape correctly."""
    user = uuid4()
    # Push a small event to create progression but not unlock anything
    resp = await client.post(
        "/api/v1/internal/gamification/events",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": str(user),
            "source_app": "jerosse",
            "event_kind": "jerosse.first_order",  # 100 XP → LV.2
            "idempotency_key": f"sync-fo-{user}",
            "occurred_at": _now(),
        },
    )
    assert resp.status_code == 201

    sync = await client.get(
        f"/api/v1/internal/gamification/users/{user}/sync",
        headers=_internal_headers(),
    )
    assert sync.status_code == 200
    body = sync.json()
    assert body["progression"]["total_xp"] == 100
    assert body["progression"]["group_level"] == 2
    assert body["achievements"] == []
    assert body["outfits"] == []
