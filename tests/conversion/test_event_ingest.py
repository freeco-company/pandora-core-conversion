"""Event ingest endpoint smoke tests."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


async def test_ingest_requires_jwt(client) -> None:
    resp = await client.post(
        "/api/v1/events",
        json={
            "app_id": "doudou",
            "event_type": "app.opened",
            "payload": {},
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
        },
    )
    assert resp.status_code == 401


async def test_ingest_first_app_opened_creates_registered_transition(client, make_jwt) -> None:
    user_uuid = str(uuid4())
    token = make_jwt(sub=user_uuid, product_code="doudou", scopes=["events:write"])
    resp = await client.post(
        "/api/v1/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "app_id": "doudou",
            "event_type": "app.opened",
            "payload": {"first_open": True},
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] > 0
    assert body["lifecycle_transition"] == "registered"


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
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
        },
    )
    assert resp.status_code == 401
    assert "not in whitelist" in resp.json()["detail"]
