"""Tests for user outfit ownership + level-driven auto-unlock."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.config import get_settings
from app.gamification import catalog
from app.gamification.models import UserOutfit


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Secret": get_settings().internal_shared_secret}


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


# ── catalog helpers ────────────────────────────────────────────────────


def test_parse_level_unlock_lv5():
    assert catalog.parse_level_unlock("LV.5") == 5
    assert catalog.parse_level_unlock("LV.20") == 20


def test_parse_level_unlock_non_level_returns_none():
    assert catalog.parse_level_unlock("streak 7 days") is None
    assert catalog.parse_level_unlock("fp_lifetime tier") is None
    assert catalog.parse_level_unlock("default") is None


def test_level_unlock_outfits_empty_below_lv5():
    out = catalog.level_unlock_outfits_up_to(4)
    assert out == []


def test_level_unlock_outfits_includes_scarf_at_lv5():
    out = catalog.level_unlock_outfits_up_to(5)
    codes = {d.code for d in out}
    assert "scarf" in codes
    # Higher-level outfits should not be in this set
    assert "angel_wings" not in codes


def test_level_unlock_outfits_at_lv100_includes_group_eternal():
    out = catalog.level_unlock_outfits_up_to(100)
    codes = {d.code for d in out}
    assert "scarf" in codes
    assert "angel_wings" in codes
    assert "group_eternal" in codes


# ── auto-unlock via level-up ───────────────────────────────────────────


async def test_level_up_grants_unlocked_level_tier_outfits(client, db_session):
    # Seed catalog so FK target exists
    await client.post(
        "/api/v1/internal/gamification/outfits/seed", headers=_internal_headers()
    )

    user = uuid4()
    headers = _internal_headers()

    # Trigger a level-up crossing LV.5 (300 XP). jerosse.referral_signed = 200 XP
    # no lifetime cap → 2x = 400 XP → LV.5.
    for i in range(2):
        resp = await client.post(
            "/api/v1/internal/gamification/events",
            headers=headers,
            json={
                "pandora_user_uuid": str(user),
                "source_app": "jerosse",
                "event_kind": "jerosse.referral_signed",
                "idempotency_key": f"ref-{user}-{i}",
                "occurred_at": _now(),
            },
        )
        assert resp.status_code == 201
    # Expect total_xp >= 300 (LV.5 anchor)
    final = resp.json()
    assert final["total_xp"] >= 300
    assert final["group_level"] >= 5

    # User should now own scarf (LV.5 unlock)
    outfits = (
        await db_session.execute(
            select(UserOutfit).where(UserOutfit.pandora_user_uuid == user)
        )
    ).scalars().all()
    codes = {o.code for o in outfits}
    assert "scarf" in codes
    # Higher-level outfits should NOT be there
    assert "angel_wings" not in codes
    # All grants are tagged level_up
    assert all(o.awarded_via == "level_up" for o in outfits)


async def test_level_up_skips_outfits_when_catalog_unseeded(client, db_session):
    """If outfit catalog isn't seeded, level-up logic silently skips (no 500)."""
    user = uuid4()
    resp = await client.post(
        "/api/v1/internal/gamification/events",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": str(user),
            "source_app": "jerosse",
            "event_kind": "jerosse.first_order",  # 100 XP → LV.2
            "idempotency_key": f"k-{user}",
            "occurred_at": _now(),
        },
    )
    assert resp.status_code == 201

    rows = (
        await db_session.execute(
            select(UserOutfit).where(UserOutfit.pandora_user_uuid == user)
        )
    ).scalars().all()
    assert rows == []  # nothing granted because catalog empty


async def test_no_duplicate_outfit_grants_across_repeated_level_ups(client, db_session):
    await client.post(
        "/api/v1/internal/gamification/outfits/seed", headers=_internal_headers()
    )
    user = uuid4()
    # Two referral events → cross LV.5
    for i in range(2):
        await client.post(
            "/api/v1/internal/gamification/events",
            headers=_internal_headers(),
            json={
                "pandora_user_uuid": str(user),
                "source_app": "jerosse",
                "event_kind": "jerosse.referral_signed",
                "idempotency_key": f"r-{user}-{i}",
                "occurred_at": _now(),
            },
        )
    # Another referral — already at LV.5; should not duplicate scarf
    await client.post(
        "/api/v1/internal/gamification/events",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": str(user),
            "source_app": "jerosse",
            "event_kind": "jerosse.referral_signed",
            "idempotency_key": f"r-{user}-extra",
            "occurred_at": _now(),
        },
    )
    rows = (
        await db_session.execute(
            select(UserOutfit).where(
                UserOutfit.pandora_user_uuid == user,
                UserOutfit.code == "scarf",
            )
        )
    ).scalars().all()
    assert len(rows) == 1


# ── manual grant ───────────────────────────────────────────────────────


async def test_grant_outfit_manual_inserts_row(client, db_session):
    await client.post(
        "/api/v1/internal/gamification/outfits/seed", headers=_internal_headers()
    )
    user = uuid4()
    resp = await client.post(
        f"/api/v1/internal/gamification/users/{user}/outfits/grant",
        headers=_internal_headers(),
        json={"code": "fp_crown", "awarded_via": "fp_lifetime"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body == {"granted": True, "code": "fp_crown"}

    rows = (
        await db_session.execute(
            select(UserOutfit).where(UserOutfit.pandora_user_uuid == user)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].code == "fp_crown"
    assert rows[0].awarded_via == "fp_lifetime"


async def test_grant_outfit_manual_idempotent(client) -> None:
    await client.post(
        "/api/v1/internal/gamification/outfits/seed", headers=_internal_headers()
    )
    user = uuid4()
    payload = {"code": "fp_chef", "awarded_via": "fp_lifetime"}
    r1 = await client.post(
        f"/api/v1/internal/gamification/users/{user}/outfits/grant",
        headers=_internal_headers(),
        json=payload,
    )
    r2 = await client.post(
        f"/api/v1/internal/gamification/users/{user}/outfits/grant",
        headers=_internal_headers(),
        json=payload,
    )
    assert r1.json()["granted"] is True
    assert r2.json()["granted"] is False


async def test_grant_outfit_manual_unknown_code_404(client) -> None:
    user = uuid4()
    resp = await client.post(
        f"/api/v1/internal/gamification/users/{user}/outfits/grant",
        headers=_internal_headers(),
        json={"code": "never_existed"},
    )
    assert resp.status_code == 404


async def test_list_user_outfits_returns_owned(client) -> None:
    await client.post(
        "/api/v1/internal/gamification/outfits/seed", headers=_internal_headers()
    )
    user = uuid4()
    await client.post(
        f"/api/v1/internal/gamification/users/{user}/outfits/grant",
        headers=_internal_headers(),
        json={"code": "scarf", "awarded_via": "manual"},
    )
    await client.post(
        f"/api/v1/internal/gamification/users/{user}/outfits/grant",
        headers=_internal_headers(),
        json={"code": "fp_crown", "awarded_via": "fp_lifetime"},
    )

    resp = await client.get(
        f"/api/v1/internal/gamification/users/{user}/outfits",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    codes = {o["code"] for o in body["outfits"]}
    assert codes == {"scarf", "fp_crown"}


async def test_list_user_outfits_returns_empty_for_unseen_user(client) -> None:
    user = uuid4()
    resp = await client.get(
        f"/api/v1/internal/gamification/users/{user}/outfits",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


async def test_list_user_outfits_requires_internal_secret(client) -> None:
    user = uuid4()
    resp = await client.get(f"/api/v1/internal/gamification/users/{user}/outfits")
    assert resp.status_code == 401
