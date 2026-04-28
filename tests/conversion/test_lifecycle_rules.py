"""Unit tests for individual lifecycle transition rules.

Each rule has at least one fire + one no-fire case. We exercise the rules at
the service layer (not HTTP) so we can pre-seed the DB cleanly and avoid
needing N JWT round-trips.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.conversion import lifecycle, mothership
from app.conversion.models import ConversionEvent, LifecycleTransition


@pytest_asyncio.fixture
async def session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)


# ── Helpers ────────────────────────────────────────────────────────────


async def _seed_event(
    session,
    uuid: UUID,
    *,
    event_type: str = "app.opened",
    occurred_at: datetime | None = None,
    app_id: str = "doudou",
) -> ConversionEvent:
    e = ConversionEvent(
        pandora_user_uuid=uuid,
        customer_id=None,
        app_id=app_id,
        event_type=event_type,
        payload={},
        occurred_at=occurred_at or datetime.now(tz=UTC),
    )
    session.add(e)
    await session.flush()
    return e


async def _seed_transition(
    session,
    uuid: UUID,
    *,
    to_status: str,
    from_status: str | None = None,
    transitioned_at: datetime | None = None,
) -> None:
    session.add(
        LifecycleTransition(
            pandora_user_uuid=uuid,
            from_status=from_status,
            to_status=to_status,
            trigger_event_id=None,
            transitioned_at=transitioned_at or datetime.now(tz=UTC),
            extra_metadata={"seed": True},
        )
    )
    await session.flush()


# ── rule_engaged ───────────────────────────────────────────────────────


async def test_engaged_fires_on_subscription_event(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="registered")
        event = await _seed_event(
            session, uuid, event_type="subscription.activated"
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        assert result.fired
        assert result.from_status == "registered"
        assert result.to_status == "engaged"


async def test_engaged_does_not_fire_when_not_registered(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="engaged")
        event = await _seed_event(
            session, uuid, event_type="subscription.activated"
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        # Already engaged; rule must not re-fire to engaged.
        assert not result.fired


async def test_engaged_fires_after_60_distinct_days(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="registered")
        # Seed 60 days of events
        base = datetime.now(tz=UTC) - timedelta(days=70)
        for i in range(60):
            await _seed_event(
                session,
                uuid,
                event_type="app.opened",
                occurred_at=base + timedelta(days=i),
            )
        # Trigger event (61st distinct day pushes count to 61)
        trigger = await _seed_event(
            session, uuid, event_type="app.opened", occurred_at=datetime.now(tz=UTC)
        )
        result = await lifecycle.evaluate_event(session, uuid, trigger)
        assert result.fired
        assert result.to_status == "engaged"


# ── rule_loyalist ──────────────────────────────────────────────────────


async def test_loyalist_does_not_fire_with_stub_mothership(session_factory) -> None:
    """Stub mothership returns 0 orders → loyalist branch can't satisfy
    repeat-purchase requirement → rule never fires (conservative v1)."""
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="engaged")
        # Spread events across 3 calendar months
        now = datetime.now(tz=UTC)
        for delta_days in (0, 35, 70):
            await _seed_event(
                session,
                uuid,
                event_type="engagement.deep",
                occurred_at=now - timedelta(days=delta_days),
            )
        trigger = await _seed_event(
            session, uuid, event_type="engagement.deep", occurred_at=now
        )
        result = await lifecycle.evaluate_event(session, uuid, trigger)
        assert not result.fired


async def test_loyalist_fires_with_fake_mothership_and_3_months(
    session_factory,
) -> None:
    """Inject a fake MothershipOrderClient that reports ≥2 recent orders."""

    class FakeClient:
        async def get_order_summary(self, _uuid: UUID):
            return mothership.MothershipOrderSummary(
                pandora_user_uuid=_uuid,
                recent_orders=3,
                lifetime_orders=5,
            )

    mothership.set_mothership_client_for_testing(FakeClient())
    try:
        uuid = uuid4()
        async with session_factory() as session:
            await _seed_transition(session, uuid, to_status="engaged")
            now = datetime.now(tz=UTC)
            # Three distinct calendar months in the lookback window
            for delta_days in (0, 35, 70):
                await _seed_event(
                    session,
                    uuid,
                    event_type="engagement.deep",
                    occurred_at=now - timedelta(days=delta_days),
                )
            trigger = await _seed_event(
                session, uuid, event_type="engagement.deep", occurred_at=now
            )
            result = await lifecycle.evaluate_event(session, uuid, trigger)
            assert result.fired
            assert result.to_status == "loyalist"
    finally:
        mothership.reset_mothership_client()


# ── rule_applicant ─────────────────────────────────────────────────────


async def test_applicant_fires_on_cta_click_when_loyalist(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="loyalist")
        event = await _seed_event(
            session, uuid, event_type="franchise.cta_click"
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        assert result.fired
        assert result.to_status == "applicant"


async def test_applicant_does_not_fire_when_not_loyalist(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="engaged")
        event = await _seed_event(
            session, uuid, event_type="franchise.cta_click"
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        # cta_click on engaged user must not skip levels
        assert not result.fired


# ── rule_first_app_opened (existing behaviour, sanity) ────────────────


async def test_first_event_visitor_to_registered(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        event = await _seed_event(session, uuid, event_type="app.opened")
        result = await lifecycle.evaluate_event(session, uuid, event)
        assert result.fired
        assert result.to_status == "registered"


# ── No transition above applicant (franchisee is admin-only) ──────────


async def test_franchisee_not_auto_fired(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="applicant")
        # Even an admin-ish event won't auto-promote to franchisee
        event = await _seed_event(session, uuid, event_type="franchise.cta_click")
        result = await lifecycle.evaluate_event(session, uuid, event)
        assert not result.fired


@pytest.mark.parametrize("invalid", ["unknown_state", "FRANCHISEE", ""])
async def test_force_transition_rejects_invalid(session_factory, invalid) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        with pytest.raises(ValueError):
            await lifecycle.force_transition(session, uuid, invalid)
