"""HTTP tests for internal endpoints (HMAC auth, admin franchisee, funnel)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.config import get_settings


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Secret": get_settings().internal_shared_secret}


# ── /internal/events ───────────────────────────────────────────────────


async def test_internal_events_rejects_without_secret(client) -> None:
    resp = await client.post(
        "/api/v1/internal/events",
        json={
            "pandora_user_uuid": str(uuid4()),
            "app_id": "doudou",
            "event_type": "app.opened",
            "payload": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    assert resp.status_code == 401


async def test_internal_events_creates_registered_transition(client) -> None:
    user_uuid = str(uuid4())
    resp = await client.post(
        "/api/v1/internal/events",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": user_uuid,
            "app_id": "doudou",
            "event_type": "app.opened",
            "payload": {"first_open": True},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["lifecycle_transition"] == "registered"


# ── /internal/admin/users/{uuid}/qualify-franchisee ────────────────────


async def test_qualify_franchisee_requires_secret(client) -> None:
    resp = await client.post(
        f"/api/v1/internal/admin/users/{uuid4()}/qualify-franchisee",
        json={"plan_chosen": "A_6600"},
    )
    assert resp.status_code == 401


async def test_qualify_franchisee_force_transitions_to_franchisee(
    client, make_jwt
) -> None:
    user_uuid = str(uuid4())
    resp = await client.post(
        f"/api/v1/internal/admin/users/{user_uuid}/qualify-franchisee",
        headers=_internal_headers(),
        json={"plan_chosen": "A_6600", "note": "manual approval"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["to_status"] == "franchisee"

    # Confirm via lifecycle endpoint (user fetches own status)
    token = make_jwt(sub=user_uuid)
    resp2 = await client.get(
        f"/api/v1/users/{user_uuid}/lifecycle",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["current_status"] == "franchisee"


# ── /funnel/metrics ────────────────────────────────────────────────────


async def test_funnel_metrics_requires_secret(client) -> None:
    resp = await client.get("/api/v1/funnel/metrics")
    assert resp.status_code == 401


async def test_funnel_metrics_counts_lifecycle_stages(client) -> None:
    # Seed a couple of users into different stages via internal endpoints
    headers = _internal_headers()

    # User A: registered (one app.opened)
    user_a = str(uuid4())
    await client.post(
        "/api/v1/internal/events",
        headers=headers,
        json={
            "pandora_user_uuid": user_a,
            "app_id": "doudou",
            "event_type": "app.opened",
            "payload": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )

    # User B: force to franchisee via admin endpoint
    user_b = str(uuid4())
    await client.post(
        f"/api/v1/internal/admin/users/{user_b}/qualify-franchisee",
        headers=headers,
        json={"plan_chosen": "B_9600"},
    )

    resp = await client.get("/api/v1/funnel/metrics", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_status = {s["status"]: s["count"] for s in body["stages"]}
    assert by_status["registered"] >= 1
    assert by_status["franchisee"] >= 1
    assert body["total_users_with_lifecycle"] >= 2
