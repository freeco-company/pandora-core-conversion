"""HTTP tests for internal endpoints (HMAC auth, admin self-use, funnel).

ADR-008 — 5 stages, training endpoints removed, qualify renamed.
"""

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
            "event_type": "subscription.premium_active",
            "payload": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    assert resp.status_code == 401


async def test_internal_premium_event_creates_loyalist_transition(client) -> None:
    """ADR-008 §2.2 #1 — visitor → loyalist on premium subscription."""
    user_uuid = str(uuid4())
    resp = await client.post(
        "/api/v1/internal/events",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": user_uuid,
            "app_id": "doudou",
            "event_type": "subscription.premium_active",
            "payload": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["lifecycle_transition"] == "loyalist"


async def test_internal_first_order_promotes_applicant_to_self_use(client) -> None:
    """ADR-008 §2.2 #3 — applicant → franchisee_self_use on first_order ≥ 6600."""
    user_uuid = str(uuid4())
    headers = _internal_headers()

    # Step 1: become loyalist via premium.
    await client.post(
        "/api/v1/internal/events",
        headers=headers,
        json={
            "pandora_user_uuid": user_uuid,
            "app_id": "doudou",
            "event_type": "subscription.premium_active",
            "payload": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    # Step 2: cta_click → applicant
    await client.post(
        "/api/v1/internal/events",
        headers=headers,
        json={
            "pandora_user_uuid": user_uuid,
            "app_id": "doudou",
            "event_type": "franchise.cta_click",
            "payload": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    # Step 3: first_order → franchisee_self_use
    resp = await client.post(
        "/api/v1/internal/events",
        headers=headers,
        json={
            "pandora_user_uuid": user_uuid,
            "app_id": "pandora_js_store",
            "event_type": "mothership.first_order",
            "payload": {
                "order_id": "MO-9001",
                "amount": "6600",
                "sku_codes": ["FP-A-001"],
            },
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["lifecycle_transition"] == "franchisee_self_use"


# ── /internal/admin/users/{uuid}/qualify-franchisee-self-use ──────────


async def test_qualify_self_use_requires_secret(client) -> None:
    resp = await client.post(
        f"/api/v1/internal/admin/users/{uuid4()}/qualify-franchisee-self-use",
        json={"plan_chosen": "A_6600"},
    )
    assert resp.status_code == 401


async def test_qualify_self_use_force_transitions(client, make_jwt) -> None:
    user_uuid = str(uuid4())
    resp = await client.post(
        f"/api/v1/internal/admin/users/{user_uuid}/qualify-franchisee-self-use",
        headers=_internal_headers(),
        json={"plan_chosen": "A_6600", "note": "manual reconcile"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["to_status"] == "franchisee_self_use"

    # Confirm via lifecycle endpoint (user fetches own status)
    token = make_jwt(sub=user_uuid)
    resp2 = await client.get(
        f"/api/v1/users/{user_uuid}/lifecycle",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["current_status"] == "franchisee_self_use"


async def test_admin_lifecycle_override_requires_secret(client) -> None:
    resp = await client.post(
        f"/api/v1/internal/admin/users/{uuid4()}/lifecycle/transition",
        json={"to_status": "loyalist", "reason": "manual", "actor": "admin@x"},
    )
    assert resp.status_code == 401


async def test_admin_lifecycle_override_transitions_any_stage(client, make_jwt) -> None:
    user_uuid = str(uuid4())
    resp = await client.post(
        f"/api/v1/internal/admin/users/{user_uuid}/lifecycle/transition",
        headers=_internal_headers(),
        json={
            "to_status": "applicant",
            "reason": "user requested upgrade after offline call",
            "actor": "ops@freeco.cc",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["to_status"] == "applicant"

    token = make_jwt(sub=user_uuid)
    resp2 = await client.get(
        f"/api/v1/users/{user_uuid}/lifecycle",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.json()["current_status"] == "applicant"


async def test_admin_lifecycle_override_rejects_invalid_stage(client) -> None:
    resp = await client.post(
        f"/api/v1/internal/admin/users/{uuid4()}/lifecycle/transition",
        headers=_internal_headers(),
        json={"to_status": "not_a_stage", "reason": "x", "actor": "x"},
    )
    assert resp.status_code == 422


async def test_admin_lifecycle_override_requires_actor_and_reason(client) -> None:
    # Missing actor
    resp = await client.post(
        f"/api/v1/internal/admin/users/{uuid4()}/lifecycle/transition",
        headers=_internal_headers(),
        json={"to_status": "loyalist", "reason": "x"},
    )
    assert resp.status_code == 422
    # Missing reason
    resp = await client.post(
        f"/api/v1/internal/admin/users/{uuid4()}/lifecycle/transition",
        headers=_internal_headers(),
        json={"to_status": "loyalist", "actor": "x"},
    )
    assert resp.status_code == 422


async def test_old_qualify_franchisee_endpoint_is_gone(client) -> None:
    """ADR-008: old `qualify-franchisee` route renamed → 404."""
    resp = await client.post(
        f"/api/v1/internal/admin/users/{uuid4()}/qualify-franchisee",
        headers=_internal_headers(),
        json={"plan_chosen": "A_6600"},
    )
    assert resp.status_code == 404


# ── Training endpoints removed (ADR-008 §3.2) ──────────────────────────


async def test_training_endpoints_removed(client, make_jwt) -> None:
    """`academy.training_progress` is deprecated. Endpoints must 404."""
    user_uuid = str(uuid4())
    token = make_jwt(sub=user_uuid)
    headers = {"Authorization": f"Bearer {token}"}

    resp_get = await client.get(
        f"/api/v1/users/{user_uuid}/training", headers=headers
    )
    assert resp_get.status_code == 404

    resp_post = await client.post(
        f"/api/v1/users/{user_uuid}/training",
        headers=headers,
        json={"chapter_id": "intro", "completed": True, "quiz_score": 80},
    )
    assert resp_post.status_code == 404


# ── /funnel/metrics — 5 stages ─────────────────────────────────────────


async def test_funnel_metrics_requires_secret(client) -> None:
    resp = await client.get("/api/v1/funnel/metrics")
    assert resp.status_code == 401


async def test_funnel_metrics_returns_5_stages(client) -> None:
    """ADR-008 §2.2 — 5 stages, in order."""
    headers = _internal_headers()

    # User A: loyalist via premium event.
    user_a = str(uuid4())
    await client.post(
        "/api/v1/internal/events",
        headers=headers,
        json={
            "pandora_user_uuid": user_a,
            "app_id": "doudou",
            "event_type": "subscription.premium_active",
            "payload": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )

    # User B: force to franchisee_self_use via admin endpoint.
    user_b = str(uuid4())
    await client.post(
        f"/api/v1/internal/admin/users/{user_b}/qualify-franchisee-self-use",
        headers=headers,
        json={"plan_chosen": "B_9600"},
    )

    resp = await client.get("/api/v1/funnel/metrics", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    statuses = [s["status"] for s in body["stages"]]
    assert statuses == [
        "visitor",
        "loyalist",
        "applicant",
        "franchisee_self_use",
        "franchisee_active",
    ]

    by_status = {s["status"]: s["count"] for s in body["stages"]}
    assert by_status["loyalist"] >= 1
    assert by_status["franchisee_self_use"] >= 1
    # Old stages must NOT appear.
    assert "registered" not in by_status
    assert "engaged" not in by_status
    assert "franchisee" not in by_status

    assert body["total_users_with_lifecycle"] >= 2
