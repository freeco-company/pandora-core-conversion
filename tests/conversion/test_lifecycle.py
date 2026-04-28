"""Lifecycle endpoints + state machine tests (HTTP layer).

ADR-008 — 5 stages; training endpoints removed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


async def test_lifecycle_history_starts_visitor(client, make_jwt) -> None:
    user_uuid = str(uuid4())
    token = make_jwt(sub=user_uuid)
    resp = await client.get(
        f"/api/v1/users/{user_uuid}/lifecycle",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_status"] == "visitor"
    assert body["history"] == []


async def test_lifecycle_path_after_premium(client, make_jwt) -> None:
    """ADR-008 §2.2 #1 — premium subscription event → loyalist."""
    user_uuid = str(uuid4())
    token = make_jwt(sub=user_uuid, scopes=["events:write"])
    headers = {"Authorization": f"Bearer {token}"}

    await client.post(
        "/api/v1/events",
        headers=headers,
        json={
            "app_id": "doudou",
            "event_type": "subscription.premium_active",
            "payload": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )

    resp = await client.get(f"/api/v1/users/{user_uuid}/lifecycle", headers=headers)
    body = resp.json()
    assert body["current_status"] == "loyalist"
    assert len(body["history"]) == 1
    assert body["history"][0]["from_status"] is None
    assert body["history"][0]["to_status"] == "loyalist"


async def test_lifecycle_cross_user_forbidden(client, make_jwt) -> None:
    user_a = str(uuid4())
    user_b = str(uuid4())
    token = make_jwt(sub=user_a)
    resp = await client.get(
        f"/api/v1/users/{user_b}/lifecycle",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_force_transition_requires_scope(client, make_jwt) -> None:
    user_uuid = str(uuid4())
    token = make_jwt(sub=user_uuid)  # no scope
    resp = await client.post(
        f"/api/v1/users/{user_uuid}/lifecycle/transition",
        headers={"Authorization": f"Bearer {token}"},
        json={"to_status": "applicant"},
    )
    assert resp.status_code == 403

    token2 = make_jwt(sub=user_uuid, scopes=["lifecycle:write"])
    resp2 = await client.post(
        f"/api/v1/users/{user_uuid}/lifecycle/transition",
        headers={"Authorization": f"Bearer {token2}"},
        json={"to_status": "applicant"},
    )
    assert resp2.status_code == 201
    assert resp2.json()["to_status"] == "applicant"


async def test_force_transition_invalid_status(client, make_jwt) -> None:
    user_uuid = str(uuid4())
    token = make_jwt(sub=user_uuid, scopes=["lifecycle:write"])
    resp = await client.post(
        f"/api/v1/users/{user_uuid}/lifecycle/transition",
        headers={"Authorization": f"Bearer {token}"},
        json={"to_status": "not_a_real_status"},
    )
    assert resp.status_code == 422


async def test_force_transition_rejects_old_adr003_stages(client, make_jwt) -> None:
    """ADR-008: stages registered/engaged/franchisee are gone."""
    user_uuid = str(uuid4())
    token = make_jwt(sub=user_uuid, scopes=["lifecycle:write"])
    for old in ("registered", "engaged", "franchisee"):
        resp = await client.post(
            f"/api/v1/users/{user_uuid}/lifecycle/transition",
            headers={"Authorization": f"Bearer {token}"},
            json={"to_status": old},
        )
        assert resp.status_code == 422, f"{old} should be rejected"
