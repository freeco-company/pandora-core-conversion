"""Lifecycle endpoints + state machine tests."""

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


async def test_lifecycle_path_after_first_event(client, make_jwt) -> None:
    user_uuid = str(uuid4())
    token = make_jwt(sub=user_uuid, scopes=["events:write"])
    headers = {"Authorization": f"Bearer {token}"}

    # Fire first app.opened -> registered
    await client.post(
        "/api/v1/events",
        headers=headers,
        json={
            "app_id": "doudou",
            "event_type": "app.opened",
            "payload": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )

    resp = await client.get(f"/api/v1/users/{user_uuid}/lifecycle", headers=headers)
    body = resp.json()
    assert body["current_status"] == "registered"
    assert len(body["history"]) == 1
    assert body["history"][0]["from_status"] is None
    assert body["history"][0]["to_status"] == "registered"


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


async def test_training_progress_upsert(client, make_jwt) -> None:
    user_uuid = str(uuid4())
    token = make_jwt(sub=user_uuid)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        f"/api/v1/users/{user_uuid}/training",
        headers=headers,
        json={"chapter_id": "intro", "completed": True, "quiz_score": 88},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["chapter_id"] == "intro"
    assert body["quiz_score"] == 88
    assert body["completed_at"] is not None
    assert body["attempts"] == 1

    # Second submission -> attempts increments, completed_at unchanged
    resp2 = await client.post(
        f"/api/v1/users/{user_uuid}/training",
        headers=headers,
        json={"chapter_id": "intro", "completed": True, "quiz_score": 95},
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["attempts"] == 2
    assert body2["quiz_score"] == 95

    # Read back
    resp3 = await client.get(f"/api/v1/users/{user_uuid}/training", headers=headers)
    chapters = resp3.json()["chapters"]
    assert len(chapters) == 1
    assert chapters[0]["chapter_id"] == "intro"
