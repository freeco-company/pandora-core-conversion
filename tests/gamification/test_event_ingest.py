"""End-to-end tests for /internal/gamification/events ingest."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.config import get_settings


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Secret": get_settings().internal_shared_secret}


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


async def test_rejects_without_secret(client) -> None:
    resp = await client.post(
        "/api/v1/internal/gamification/events",
        json={
            "pandora_user_uuid": str(uuid4()),
            "source_app": "dodo",
            "event_kind": "dodo.meal_logged",
            "idempotency_key": "k1",
            "occurred_at": _now(),
        },
    )
    assert resp.status_code == 401


async def test_meal_logged_awards_xp_and_creates_progression(client) -> None:
    user = str(uuid4())
    resp = await client.post(
        "/api/v1/internal/gamification/events",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": user,
            "source_app": "dodo",
            "event_kind": "dodo.meal_logged",
            "idempotency_key": f"meal-1-{user}",
            "occurred_at": _now(),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["xp_delta"] == 5
    assert body["total_xp"] == 5
    assert body["group_level"] == 1  # below LV.2 threshold (50)
    assert body["leveled_up_to"] is None
    assert body["duplicate"] is False


async def test_idempotent_replay_returns_duplicate(client) -> None:
    user = str(uuid4())
    payload = {
        "pandora_user_uuid": user,
        "source_app": "dodo",
        "event_kind": "dodo.meal_logged",
        "idempotency_key": f"meal-dup-{user}",
        "occurred_at": _now(),
    }
    headers = _internal_headers()
    r1 = await client.post(
        "/api/v1/internal/gamification/events", headers=headers, json=payload
    )
    assert r1.status_code == 201
    assert r1.json()["xp_delta"] == 5

    r2 = await client.post(
        "/api/v1/internal/gamification/events", headers=headers, json=payload
    )
    assert r2.status_code == 201
    body = r2.json()
    assert body["duplicate"] is True
    # total_xp should still be 5 (not 10) — duplicate didn't double-credit
    assert body["total_xp"] == 5


async def test_first_order_lifetime_unique(client) -> None:
    user = str(uuid4())
    headers = _internal_headers()
    base = {
        "pandora_user_uuid": user,
        "source_app": "jerosse",
        "event_kind": "jerosse.first_order",
        "occurred_at": _now(),
    }
    r1 = await client.post(
        "/api/v1/internal/gamification/events",
        headers=headers,
        json={**base, "idempotency_key": "k1"},
    )
    assert r1.status_code == 201
    assert r1.json()["xp_delta"] == 100
    assert r1.json()["group_level"] == 2  # 100 XP between LV.2 (50) and LV.3 (120)

    # Different idempotency_key but same lifetime_unique event → 0 XP
    r2 = await client.post(
        "/api/v1/internal/gamification/events",
        headers=headers,
        json={**base, "idempotency_key": "k2"},
    )
    assert r2.status_code == 201
    assert r2.json()["xp_delta"] == 0
    assert r2.json()["total_xp"] == 100


async def test_daily_cap_caps_xp_within_window(client) -> None:
    user = str(uuid4())
    headers = _internal_headers()
    # dodo.app_opened: 1 XP, daily cap 5
    occurred_at = datetime.now(tz=UTC).isoformat()
    awarded = 0
    for i in range(8):
        resp = await client.post(
            "/api/v1/internal/gamification/events",
            headers=headers,
            json={
                "pandora_user_uuid": user,
                "source_app": "dodo",
                "event_kind": "dodo.app_opened",
                "idempotency_key": f"open-{user}-{i}",
                "occurred_at": occurred_at,
            },
        )
        assert resp.status_code == 201
        awarded += resp.json()["xp_delta"]
    assert awarded == 5  # daily cap


async def test_meal_logged_diminishing_returns(client) -> None:
    """First 3 meals 5 XP each, 4th onwards 2 XP each (catalog §3.1)."""
    user = str(uuid4())
    headers = _internal_headers()
    occurred_at = datetime.now(tz=UTC).isoformat()
    deltas: list[int] = []
    for i in range(6):
        resp = await client.post(
            "/api/v1/internal/gamification/events",
            headers=headers,
            json={
                "pandora_user_uuid": user,
                "source_app": "dodo",
                "event_kind": "dodo.meal_logged",
                "idempotency_key": f"meal-dim-{user}-{i}",
                "occurred_at": occurred_at,
            },
        )
        assert resp.status_code == 201
        deltas.append(resp.json()["xp_delta"])
    # First 3 meals = 5 XP each, then 2 XP each, but capped at daily 30 total
    assert deltas[:3] == [5, 5, 5]
    assert deltas[3:6] == [2, 2, 2]
    assert sum(deltas) == 21  # well under daily cap 30


async def test_unknown_event_kind_returns_422(client) -> None:
    user = str(uuid4())
    resp = await client.post(
        "/api/v1/internal/gamification/events",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": user,
            "source_app": "dodo",
            "event_kind": "dodo.never_heard_of_this",
            "idempotency_key": "x",
            "occurred_at": _now(),
        },
    )
    assert resp.status_code == 422


async def test_source_app_mismatch_returns_422(client) -> None:
    user = str(uuid4())
    resp = await client.post(
        "/api/v1/internal/gamification/events",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": user,
            "source_app": "jerosse",  # wrong: meal_logged is dodo
            "event_kind": "dodo.meal_logged",
            "idempotency_key": "x",
            "occurred_at": _now(),
        },
    )
    assert resp.status_code == 422


async def test_level_up_via_first_order(client) -> None:
    """jerosse.first_order = 100 XP → LV.4 (anchor 200 XP not yet reached → LV.3 anchor=120)."""
    user = str(uuid4())
    resp = await client.post(
        "/api/v1/internal/gamification/events",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": user,
            "source_app": "jerosse",
            "event_kind": "jerosse.first_order",
            "idempotency_key": f"fo-{user}",
            "occurred_at": _now(),
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    # 100 XP: passes LV.2 (50) and LV.3 (120 not reached) — so LV.2
    assert body["group_level"] == 2
    assert body["leveled_up_to"] == 2


async def test_progression_endpoint_returns_baseline_for_new_user(client) -> None:
    user = uuid4()
    resp = await client.get(
        f"/api/v1/internal/gamification/progression/{user}",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_xp"] == 0
    assert body["group_level"] == 1
    assert body["level_name_zh"] == "種子期"
    assert body["xp_to_next_level"] == 50  # LV.2 threshold


async def test_progression_endpoint_returns_user_state_after_events(client) -> None:
    user = str(uuid4())
    headers = _internal_headers()
    await client.post(
        "/api/v1/internal/gamification/events",
        headers=headers,
        json={
            "pandora_user_uuid": user,
            "source_app": "dodo",
            "event_kind": "dodo.streak_7",
            "idempotency_key": f"s7-{user}",
            "occurred_at": _now(),
        },
    )
    resp = await client.get(
        f"/api/v1/internal/gamification/progression/{user}",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_xp"] == 50  # streak_7 awards 50 xp
    assert body["group_level"] == 2  # crosses LV.2 anchor (50)


async def test_daily_cap_resets_next_day(client) -> None:
    """Exhaust daily cap on day 1, then earn again on day 2."""
    user = str(uuid4())
    headers = _internal_headers()
    day1 = datetime.now(tz=UTC) - timedelta(days=2)
    day2 = datetime.now(tz=UTC) - timedelta(days=1)

    # Burn cap on day 1: dodo.app_opened cap = 5
    for i in range(7):
        await client.post(
            "/api/v1/internal/gamification/events",
            headers=headers,
            json={
                "pandora_user_uuid": user,
                "source_app": "dodo",
                "event_kind": "dodo.app_opened",
                "idempotency_key": f"d1-{user}-{i}",
                "occurred_at": day1.isoformat(),
            },
        )
    # Now day 2 — should not be capped
    resp = await client.post(
        "/api/v1/internal/gamification/events",
        headers=headers,
        json={
            "pandora_user_uuid": user,
            "source_app": "dodo",
            "event_kind": "dodo.app_opened",
            "idempotency_key": f"d2-{user}-1",
            "occurred_at": day2.isoformat(),
        },
    )
    assert resp.status_code == 201
    assert resp.json()["xp_delta"] == 1
