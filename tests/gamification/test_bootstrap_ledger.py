"""Tests for the Phase B ledger bootstrap migration endpoint."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from app.config import get_settings
from app.gamification.models import (
    GamificationOutboxEvent,
    UserProgression,
    XpLedgerEntry,
)


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Secret": get_settings().internal_shared_secret}


# ── auth + validation ──────────────────────────────────────────────────


async def test_bootstrap_requires_internal_secret(client) -> None:
    resp = await client.post(
        "/api/v1/internal/gamification/migration/bootstrap-ledger",
        json={"entries": []},
    )
    assert resp.status_code == 401


async def test_bootstrap_validates_min_one_entry(client) -> None:
    resp = await client.post(
        "/api/v1/internal/gamification/migration/bootstrap-ledger",
        headers=_internal_headers(),
        json={"entries": []},
    )
    assert resp.status_code == 422


async def test_bootstrap_caps_batch_at_1000(client) -> None:
    entries = [
        {"pandora_user_uuid": str(uuid4()), "total_xp": 100, "source_app": "dodo"}
        for _ in range(1001)
    ]
    resp = await client.post(
        "/api/v1/internal/gamification/migration/bootstrap-ledger",
        headers=_internal_headers(),
        json={"entries": entries},
    )
    assert resp.status_code == 422


# ── happy path ─────────────────────────────────────────────────────────


async def test_bootstrap_writes_ledger_and_progression_for_new_user(client, db_session):
    user = uuid4()
    resp = await client.post(
        "/api/v1/internal/gamification/migration/bootstrap-ledger",
        headers=_internal_headers(),
        json={
            "entries": [
                {
                    "pandora_user_uuid": str(user),
                    "total_xp": 1000,
                    "source_app": "dodo",
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["new_bootstraps"] == 1
    assert body["skipped"] == 0
    assert body["total_in_request"] == 1
    assert len(body["results"]) == 1
    item = body["results"][0]
    assert item["bootstrapped"] is True
    assert item["total_xp"] == 1000
    assert item["group_level"] == 10  # 1000 XP = LV.10 anchor

    ledger = (
        await db_session.execute(
            select(XpLedgerEntry).where(XpLedgerEntry.pandora_user_uuid == user)
        )
    ).scalars().all()
    assert len(ledger) == 1
    assert ledger[0].event_kind == "migration.bootstrap"
    assert ledger[0].xp_delta == 1000
    assert ledger[0].idempotency_key == f"migration.dodo.bootstrap.{user}"

    progression = (
        await db_session.execute(
            select(UserProgression).where(UserProgression.pandora_user_uuid == user)
        )
    ).scalar_one()
    assert progression.total_xp == 1000
    assert progression.group_level == 10
    assert progression.level_name_zh == "穩定期"


async def test_bootstrap_is_idempotent(client) -> None:
    user = uuid4()
    payload = {
        "entries": [
            {
                "pandora_user_uuid": str(user),
                "total_xp": 250,
                "source_app": "dodo",
            }
        ]
    }
    r1 = await client.post(
        "/api/v1/internal/gamification/migration/bootstrap-ledger",
        headers=_internal_headers(),
        json=payload,
    )
    r2 = await client.post(
        "/api/v1/internal/gamification/migration/bootstrap-ledger",
        headers=_internal_headers(),
        json=payload,
    )
    assert r1.json()["new_bootstraps"] == 1
    assert r2.json()["new_bootstraps"] == 0
    assert r2.json()["skipped"] == 1
    # Still reports current state
    assert r2.json()["results"][0]["total_xp"] == 250


async def test_bootstrap_handles_zero_xp_user(client, db_session) -> None:
    user = uuid4()
    resp = await client.post(
        "/api/v1/internal/gamification/migration/bootstrap-ledger",
        headers=_internal_headers(),
        json={
            "entries": [
                {
                    "pandora_user_uuid": str(user),
                    "total_xp": 0,
                    "source_app": "dodo",
                }
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["bootstrapped"] is True
    assert body["results"][0]["total_xp"] == 0
    assert body["results"][0]["group_level"] == 1


async def test_bootstrap_does_not_enqueue_outbox_event(client, db_session, monkeypatch):
    """Migration is invisible to apps — no level_up webhook should fire."""
    monkeypatch.setenv("GAMIFICATION_CONSUMER_DODO_URL", "https://dodo.test/x")
    monkeypatch.setenv("GAMIFICATION_CONSUMER_DODO_SECRET", "x")
    monkeypatch.setattr(
        get_settings(), "gamification_consumers", "dodo", raising=False
    )
    user = uuid4()
    await client.post(
        "/api/v1/internal/gamification/migration/bootstrap-ledger",
        headers=_internal_headers(),
        json={
            "entries": [
                {
                    "pandora_user_uuid": str(user),
                    "total_xp": 5000,
                    "source_app": "dodo",
                }
            ]
        },
    )

    rows = (
        await db_session.execute(
            select(GamificationOutboxEvent).where(
                GamificationOutboxEvent.pandora_user_uuid == user
            )
        )
    ).scalars().all()
    assert rows == []


async def test_bootstrap_batch_with_mixed_users(client, db_session) -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    resp = await client.post(
        "/api/v1/internal/gamification/migration/bootstrap-ledger",
        headers=_internal_headers(),
        json={
            "entries": [
                {"pandora_user_uuid": str(a), "total_xp": 50, "source_app": "dodo"},
                {"pandora_user_uuid": str(b), "total_xp": 1500, "source_app": "dodo"},
                {"pandora_user_uuid": str(c), "total_xp": 0, "source_app": "dodo"},
            ]
        },
    )
    body = resp.json()
    assert body["new_bootstraps"] == 3
    by_uuid = {r["pandora_user_uuid"]: r for r in body["results"]}
    assert by_uuid[str(a)]["group_level"] == 2
    assert by_uuid[str(b)]["group_level"] >= 12
    assert by_uuid[str(c)]["group_level"] == 1


async def test_bootstrap_after_existing_events_does_not_overwrite_progression(
    client, db_session
):
    """If a user has already accumulated some XP via real events, bootstrap
    skips the progression overwrite (only inserts the ledger row tagged
    migration.bootstrap)."""
    user = uuid4()
    # First, a real event putting them at LV.2 (50 XP)
    await client.post(
        "/api/v1/internal/gamification/events",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": str(user),
            "source_app": "dodo",
            "event_kind": "dodo.streak_7",
            "idempotency_key": f"real-{user}",
            "occurred_at": "2026-04-29T00:00:00+00:00",
        },
    )
    # Then, a bootstrap claiming they had 5000 — should NOT clobber existing
    # progression.
    await client.post(
        "/api/v1/internal/gamification/migration/bootstrap-ledger",
        headers=_internal_headers(),
        json={
            "entries": [
                {"pandora_user_uuid": str(user), "total_xp": 5000, "source_app": "dodo"},
            ]
        },
    )

    progression = (
        await db_session.execute(
            select(UserProgression).where(UserProgression.pandora_user_uuid == user)
        )
    ).scalar_one()
    # Existing progression preserved (50 XP from the real event)
    assert progression.total_xp == 50
    assert progression.group_level == 2

    # But the ledger DID get the bootstrap row for audit
    ledger = (
        await db_session.execute(
            select(XpLedgerEntry).where(
                XpLedgerEntry.pandora_user_uuid == user,
                XpLedgerEntry.event_kind == "migration.bootstrap",
            )
        )
    ).scalars().all()
    assert len(ledger) == 1


async def test_bootstrap_supports_other_source_apps(client) -> None:
    """jerosse / calendar / ... apps can also bootstrap their legacy users."""
    user = uuid4()
    resp = await client.post(
        "/api/v1/internal/gamification/migration/bootstrap-ledger",
        headers=_internal_headers(),
        json={
            "entries": [
                {
                    "pandora_user_uuid": str(user),
                    "total_xp": 200,
                    "source_app": "jerosse",
                }
            ]
        },
    )
    body = resp.json()
    assert body["new_bootstraps"] == 1
    assert body["results"][0]["total_xp"] == 200
