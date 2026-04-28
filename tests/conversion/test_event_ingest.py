"""Event ingest endpoint smoke tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


async def test_ingest_requires_jwt(client) -> None:
    resp = await client.post(
        "/api/v1/events",
        json={
            "app_id": "doudou",
            "event_type": "app.opened",
            "payload": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    assert resp.status_code == 401


async def test_ingest_app_opened_no_transition(client, make_jwt) -> None:
    """ADR-008: app.opened no longer drives a transition (visitor stays visitor).

    Stays a valid event for analytics — the row persists, transition is null.
    """
    user_uuid = str(uuid4())
    token = make_jwt(sub=user_uuid, product_code="doudou", scopes=["events:write"])
    resp = await client.post(
        "/api/v1/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "app_id": "doudou",
            "event_type": "app.opened",
            "payload": {"first_open": True},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] > 0
    assert body["lifecycle_transition"] is None


async def test_ingest_premium_creates_loyalist_transition(client, make_jwt) -> None:
    """ADR-008 §2.2 #1 — subscription.premium_active → loyalist."""
    user_uuid = str(uuid4())
    token = make_jwt(sub=user_uuid, product_code="doudou", scopes=["events:write"])
    resp = await client.post(
        "/api/v1/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "app_id": "doudou",
            "event_type": "subscription.premium_active",
            "payload": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["lifecycle_transition"] == "loyalist"


async def test_ingest_rejects_disallowed_product(client, make_jwt) -> None:
    user_uuid = str(uuid4())
    token = make_jwt(sub=user_uuid, product_code="not_a_real_app")
    resp = await client.post(
        "/api/v1/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "app_id": "doudou",
            "event_type": "app.opened",
            "payload": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    assert resp.status_code == 401
    assert "not in whitelist" in resp.json()["detail"]
