"""Tests for gamification webhook outbox (enqueue + dispatch + retry)."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.gamification import outbox, service
from app.gamification.models import GamificationOutboxEvent
from app.gamification.schemas import InternalEventIngestRequest


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Secret": get_settings().internal_shared_secret}


def _patch_httpx_with_handler(monkeypatch, handler) -> None:
    """Replace httpx.AsyncClient so the dispatcher routes via a MockTransport."""
    real_async_client = httpx.AsyncClient

    def factory(*_a, **kw):  # noqa: ANN001
        return real_async_client(
            transport=httpx.MockTransport(handler),
            timeout=kw.get("timeout"),
        )

    monkeypatch.setattr("httpx.AsyncClient", factory)


@pytest.fixture(autouse=True)
def _consumer_env(monkeypatch):
    """Default to a single configured consumer 'meal' for these tests."""
    monkeypatch.setenv("GAMIFICATION_CONSUMER_MEAL_URL", "https://meal.test/webhook")
    monkeypatch.setenv("GAMIFICATION_CONSUMER_MEAL_SECRET", "meal-secret")
    # Force settings to reload
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "gamification_consumers", "meal", raising=False)
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings_with_consumers(monkeypatch):
    """Helper for tests that need to override the configured consumer list."""
    def _set(consumers: str) -> None:
        monkeypatch.setattr(get_settings(), "gamification_consumers", consumers, raising=False)
    return _set


# ── enqueue ──────────────────────────────────────────────────────────────


async def test_enqueue_writes_one_row_per_configured_consumer(
    db_session, monkeypatch, settings_with_consumers
):
    settings_with_consumers("meal,jerosse")
    monkeypatch.setenv("GAMIFICATION_CONSUMER_JEROSSE_URL", "https://jerosse.test/webhook")
    monkeypatch.setenv("GAMIFICATION_CONSUMER_JEROSSE_SECRET", "j-secret")
    user = uuid4()

    rows = await outbox.enqueue_event(
        db_session,
        event_type="gamification.level_up",
        pandora_user_uuid=user,
        payload={"new_level": 5},
        ledger_id=42,
    )

    assert len(rows) == 2
    assert {r.consumer for r in rows} == {"meal", "jerosse"}
    assert all(r.event_id == "gamification.level_up.42" for r in rows)
    assert all(r.status == "pending" for r in rows)


async def test_enqueue_with_no_consumers_returns_empty(
    db_session, settings_with_consumers
):
    settings_with_consumers("")
    rows = await outbox.enqueue_event(
        db_session,
        event_type="gamification.level_up",
        pandora_user_uuid=uuid4(),
        payload={"x": 1},
    )
    assert rows == []


async def test_enqueue_falls_back_to_uuid_event_id_without_ledger_id(db_session):
    user = uuid4()
    rows = await outbox.enqueue_event(
        db_session,
        event_type="gamification.level_up",
        pandora_user_uuid=user,
        payload={"x": 1},
    )
    assert len(rows) == 1
    assert rows[0].event_id.startswith("gamification.level_up.")
    # Not the integer-suffix form
    assert "." in rows[0].event_id and not rows[0].event_id.endswith(".42")


# ── level-up auto-fan-out via ingest ────────────────────────────────────


async def test_level_up_ingest_enqueues_outbox_row(db_session):
    user = uuid4()
    payload = InternalEventIngestRequest(
        pandora_user_uuid=user,
        source_app="jerosse",
        event_kind="jerosse.first_order",  # 100 XP → LV.2
        idempotency_key=f"k-{user}",
        occurred_at=datetime.now(tz=UTC),
        metadata={},
    )
    async with db_session.begin():
        outcome = await service.ingest_event_internal(db_session, payload)
    assert outcome.leveled_up_to == 2

    stmt = select(GamificationOutboxEvent).where(
        GamificationOutboxEvent.pandora_user_uuid == user
    )
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "gamification.level_up"
    assert row.consumer == "meal"
    assert row.payload["new_level"] == 2
    assert row.payload["trigger_event_kind"] == "jerosse.first_order"


async def test_level_up_ingest_enqueues_outfit_unlocked_when_outfits_granted(
    client, db_session
):
    # Seed catalog so outfit FK target exists
    await client.post(
        "/api/v1/internal/gamification/outfits/seed", headers=_internal_headers()
    )
    user = uuid4()

    # Two referral events → 400 XP → LV.5; LV.5 unlocks "scarf"
    for i in range(2):
        resp = await client.post(
            "/api/v1/internal/gamification/events",
            headers=_internal_headers(),
            json={
                "pandora_user_uuid": str(user),
                "source_app": "jerosse",
                "event_kind": "jerosse.referral_signed",
                "idempotency_key": f"ref-{user}-{i}",
                "occurred_at": datetime.now(tz=UTC).isoformat(),
            },
        )
        assert resp.status_code == 201

    rows = (
        await db_session.execute(
            select(GamificationOutboxEvent).where(
                GamificationOutboxEvent.pandora_user_uuid == user,
                GamificationOutboxEvent.event_type == "gamification.outfit_unlocked",
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert "scarf" in payload["codes"]
    assert payload["awarded_via"] == "level_up"
    assert payload["trigger_level"] >= 5


async def test_level_up_with_no_outfit_gate_does_not_enqueue_outfit_event(
    client, db_session
):
    # Seed catalog
    await client.post(
        "/api/v1/internal/gamification/outfits/seed", headers=_internal_headers()
    )
    user = uuid4()
    # jerosse.first_order = 100 XP → LV.2 (no outfit unlocks until LV.5)
    resp = await client.post(
        "/api/v1/internal/gamification/events",
        headers=_internal_headers(),
        json={
            "pandora_user_uuid": str(user),
            "source_app": "jerosse",
            "event_kind": "jerosse.first_order",
            "idempotency_key": f"k-{user}",
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    assert resp.status_code == 201

    rows = (
        await db_session.execute(
            select(GamificationOutboxEvent).where(
                GamificationOutboxEvent.pandora_user_uuid == user,
                GamificationOutboxEvent.event_type == "gamification.outfit_unlocked",
            )
        )
    ).scalars().all()
    assert rows == []


async def test_non_level_up_ingest_does_not_enqueue(db_session):
    user = uuid4()
    payload = InternalEventIngestRequest(
        pandora_user_uuid=user,
        source_app="meal",
        event_kind="meal.meal_logged",  # 5 XP, no level-up from 0
        idempotency_key=f"k-{user}",
        occurred_at=datetime.now(tz=UTC),
        metadata={},
    )
    async with db_session.begin():
        await service.ingest_event_internal(db_session, payload)

    stmt = select(GamificationOutboxEvent).where(
        GamificationOutboxEvent.pandora_user_uuid == user
    )
    rows = (await db_session.execute(stmt)).scalars().all()
    assert rows == []


# ── dispatcher: HMAC + retry semantics ─────────────────────────────────


def _verify_signature(headers: httpx.Headers, body: bytes, secret: str) -> bool:
    timestamp = headers["X-Pandora-Timestamp"]
    nonce = headers["X-Pandora-Nonce"]
    signature = headers["X-Pandora-Signature"]
    msg = timestamp.encode() + b"." + nonce.encode() + b"." + body
    expected = "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


async def test_dispatch_sends_with_hmac_headers_on_success(
    db_session, monkeypatch
):
    user = uuid4()
    await outbox.enqueue_event(
        db_session,
        event_type="gamification.level_up",
        pandora_user_uuid=user,
        payload={"new_level": 3},
        ledger_id=7,
    )

    captured: dict = {}

    def transport_handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        captured["headers"] = request.headers
        return httpx.Response(200, json={"ok": True})

    _patch_httpx_with_handler(monkeypatch, transport_handler)

    summary = await outbox.dispatch_pending(db_session)

    assert summary["sent"] == 1
    assert summary["retried"] == 0
    assert summary["dead_letter"] == 0
    assert captured["url"] == "https://meal.test/webhook"
    assert _verify_signature(captured["headers"], captured["body"], "meal-secret")

    # Row transitioned to sent
    stmt = select(GamificationOutboxEvent).where(
        GamificationOutboxEvent.pandora_user_uuid == user
    )
    row = (await db_session.execute(stmt)).scalar_one()
    assert row.status == "sent"
    assert row.sent_at is not None
    assert row.last_error is None


async def test_dispatch_5xx_schedules_retry_with_backoff(db_session, monkeypatch):
    user = uuid4()
    await outbox.enqueue_event(
        db_session,
        event_type="gamification.level_up",
        pandora_user_uuid=user,
        payload={"new_level": 3},
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    _patch_httpx_with_handler(monkeypatch, handler)

    summary = await outbox.dispatch_pending(db_session)
    assert summary["retried"] == 1
    assert summary["sent"] == 0
    assert summary["dead_letter"] == 0

    row = (
        await db_session.execute(
            select(GamificationOutboxEvent).where(
                GamificationOutboxEvent.pandora_user_uuid == user
            )
        )
    ).scalar_one()
    assert row.status == "pending"
    assert row.retry_count == 1
    # sqlite drops tz; compare as naive
    now_naive = datetime.now(tz=UTC).replace(tzinfo=None)
    nra = row.next_retry_at
    nra_naive = nra.replace(tzinfo=None) if nra.tzinfo else nra
    assert nra_naive > now_naive
    assert "503" in (row.last_error or "")


async def test_dispatch_4xx_dead_letters_immediately(db_session, monkeypatch):
    user = uuid4()
    await outbox.enqueue_event(
        db_session,
        event_type="gamification.level_up",
        pandora_user_uuid=user,
        payload={},
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="malformed")

    _patch_httpx_with_handler(monkeypatch, handler)

    summary = await outbox.dispatch_pending(db_session)
    assert summary["dead_letter"] == 1

    row = (
        await db_session.execute(
            select(GamificationOutboxEvent).where(
                GamificationOutboxEvent.pandora_user_uuid == user
            )
        )
    ).scalar_one()
    assert row.status == "dead_letter"


async def test_dispatch_after_max_retries_dead_letters(db_session, monkeypatch):
    user = uuid4()
    rows = await outbox.enqueue_event(
        db_session,
        event_type="gamification.level_up",
        pandora_user_uuid=user,
        payload={},
    )
    # Pre-bump retry_count to last allowed value
    rows[0].retry_count = outbox.MAX_RETRIES - 1
    rows[0].next_retry_at = datetime.now(tz=UTC) - timedelta(minutes=1)
    await db_session.flush()

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    _patch_httpx_with_handler(monkeypatch, handler)

    summary = await outbox.dispatch_pending(db_session)
    assert summary["dead_letter"] == 1
    row = (
        await db_session.execute(
            select(GamificationOutboxEvent).where(
                GamificationOutboxEvent.pandora_user_uuid == user
            )
        )
    ).scalar_one()
    assert row.status == "dead_letter"
    assert row.retry_count == outbox.MAX_RETRIES


async def test_dispatch_skips_rows_with_future_next_retry(db_session, monkeypatch):
    user = uuid4()
    rows = await outbox.enqueue_event(
        db_session,
        event_type="gamification.level_up",
        pandora_user_uuid=user,
        payload={},
    )
    rows[0].next_retry_at = datetime.now(tz=UTC) + timedelta(hours=1)
    await db_session.flush()

    called = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200)

    _patch_httpx_with_handler(monkeypatch, handler)

    summary = await outbox.dispatch_pending(db_session)
    assert summary == {"sent": 0, "retried": 0, "dead_letter": 0, "skipped": 0}
    assert called["n"] == 0


async def test_consumer_de_configured_dead_letters(db_session, monkeypatch):
    user = uuid4()
    await outbox.enqueue_event(
        db_session,
        event_type="gamification.level_up",
        pandora_user_uuid=user,
        payload={},
    )
    # Drop env vars for the consumer between enqueue and dispatch
    monkeypatch.delenv("GAMIFICATION_CONSUMER_MEAL_URL", raising=False)
    monkeypatch.delenv("GAMIFICATION_CONSUMER_MEAL_SECRET", raising=False)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    _patch_httpx_with_handler(monkeypatch, handler)

    summary = await outbox.dispatch_pending(db_session)
    assert summary["dead_letter"] == 1


# ── HTTP admin endpoint ─────────────────────────────────────────────────


async def test_dispatch_endpoint_requires_internal_secret(client) -> None:
    resp = await client.post("/api/v1/internal/gamification/outbox/dispatch")
    assert resp.status_code == 401


async def test_dispatch_endpoint_returns_summary_on_empty_outbox(client) -> None:
    resp = await client.post(
        "/api/v1/internal/gamification/outbox/dispatch",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"sent": 0, "retried": 0, "dead_letter": 0, "skipped": 0}


async def test_dispatch_endpoint_validates_limit(client) -> None:
    resp = await client.post(
        "/api/v1/internal/gamification/outbox/dispatch?limit=0",
        headers=_internal_headers(),
    )
    assert resp.status_code == 422


async def test_consumer_config_requires_both_url_and_secret(monkeypatch):
    monkeypatch.setenv("GAMIFICATION_CONSUMER_FOO_URL", "https://x")
    monkeypatch.delenv("GAMIFICATION_CONSUMER_FOO_SECRET", raising=False)
    assert outbox.consumer_config("foo") is None
    monkeypatch.setenv("GAMIFICATION_CONSUMER_FOO_SECRET", "s")
    assert outbox.consumer_config("foo") == ("https://x", "s")


def test_signing_helper_is_deterministic_and_hex():
    sig = outbox._sign("secret", "2026-04-29T00:00:00+00:00", "abcd", b'{"a":1}')
    assert sig.startswith("sha256=")
    # Same inputs → same signature
    sig2 = outbox._sign("secret", "2026-04-29T00:00:00+00:00", "abcd", b'{"a":1}')
    assert sig == sig2
    # Different secret → different
    sig3 = outbox._sign("other", "2026-04-29T00:00:00+00:00", "abcd", b'{"a":1}')
    assert sig != sig3
