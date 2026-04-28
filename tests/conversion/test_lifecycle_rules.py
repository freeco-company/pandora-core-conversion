"""Unit tests for ADR-008 lifecycle transition rules.

Each rule has at least 2 cases (≥1 fire + ≥1 no-fire). Service-layer tests
(not HTTP) so we can pre-seed DB cleanly without JWT round-trips.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    event_type: str = "engagement.deep",
    payload: dict | None = None,
    occurred_at: datetime | None = None,
    app_id: str = "doudou",
) -> ConversionEvent:
    e = ConversionEvent(
        pandora_user_uuid=uuid,
        customer_id=None,
        app_id=app_id,
        event_type=event_type,
        payload=payload or {},
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


class _FakeMothership:
    """Configurable fake for both order summary and monthly purchases."""

    def __init__(
        self,
        *,
        recent_orders: int = 0,
        monthly: list[Decimal] | None = None,
    ) -> None:
        self._recent = recent_orders
        self._monthly = monthly if monthly is not None else [Decimal("0")] * 3

    async def get_order_summary(
        self, uuid: UUID
    ) -> mothership.MothershipOrderSummary:
        return mothership.MothershipOrderSummary(
            pandora_user_uuid=uuid,
            recent_orders=self._recent,
            lifetime_orders=self._recent,
        )

    async def get_monthly_purchases(
        self, uuid: UUID, months: int = 3
    ) -> list[Decimal]:
        out = list(self._monthly)
        if len(out) < months:
            out += [Decimal("0")] * (months - len(out))
        return out[:months]


# ── rule_visitor_to_loyalist ───────────────────────────────────────────


async def test_loyalist_fires_on_premium_subscription(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        # No prior transitions — current_status is None, treated as visitor.
        event = await _seed_event(
            session, uuid, event_type="subscription.premium_active"
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        assert result.fired
        assert result.to_status == "loyalist"


async def test_loyalist_fires_on_14_continuous_engagement_days(
    session_factory,
) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="visitor")
        # Seed 13 days of engagement.deep ending yesterday; trigger today
        # gives 14 consecutive days.
        today = datetime.now(tz=UTC)
        for i in range(1, 14):  # 1..13 days ago
            await _seed_event(
                session,
                uuid,
                event_type="engagement.deep",
                occurred_at=today - timedelta(days=i),
            )
        trigger = await _seed_event(
            session, uuid, event_type="engagement.deep", occurred_at=today
        )
        result = await lifecycle.evaluate_event(session, uuid, trigger)
        assert result.fired
        assert result.to_status == "loyalist"


async def test_loyalist_does_not_fire_with_gap_in_streak(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="visitor")
        today = datetime.now(tz=UTC)
        # Days 1..6 + 8..14 ago — there's a gap at day 7 → no 14-day streak.
        for i in [1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14]:
            await _seed_event(
                session,
                uuid,
                event_type="engagement.deep",
                occurred_at=today - timedelta(days=i),
            )
        trigger = await _seed_event(
            session, uuid, event_type="engagement.deep", occurred_at=today
        )
        result = await lifecycle.evaluate_event(session, uuid, trigger)
        assert not result.fired


async def test_loyalist_does_not_fire_when_already_past(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="applicant")
        event = await _seed_event(
            session, uuid, event_type="subscription.premium_active"
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        # Already past loyalist; rule must not regress.
        assert not result.fired


# ── rule_loyalist_to_applicant ─────────────────────────────────────────


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


async def test_applicant_fires_on_consultation_form(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="loyalist")
        event = await _seed_event(
            session,
            uuid,
            event_type="mothership.consultation_submitted",
            payload={"form_id": "fp_2026q2_a", "source": "fp_homepage"},
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        assert result.fired
        assert result.to_status == "applicant"


async def test_applicant_does_not_fire_when_visitor(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="visitor")
        event = await _seed_event(
            session, uuid, event_type="franchise.cta_click"
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        # cta_click on visitor must not skip levels (no auto-promote).
        assert not result.fired


# ── rule_applicant_to_franchisee_self_use ──────────────────────────────


async def test_self_use_fires_on_first_order_at_threshold(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="applicant")
        event = await _seed_event(
            session,
            uuid,
            event_type="mothership.first_order",
            payload={
                "order_id": "MO-12345",
                "amount": "6600",
                "sku_codes": ["FP-A-001"],
            },
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        assert result.fired
        assert result.to_status == "franchisee_self_use"


async def test_self_use_does_not_fire_below_threshold(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="applicant")
        event = await _seed_event(
            session,
            uuid,
            event_type="mothership.first_order",
            payload={"order_id": "MO-1", "amount": "5000", "sku_codes": []},
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        assert not result.fired


async def test_self_use_does_not_fire_when_not_applicant(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="loyalist")
        event = await _seed_event(
            session,
            uuid,
            event_type="mothership.first_order",
            payload={"order_id": "MO-1", "amount": "10000", "sku_codes": []},
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        # User must pass through applicant first.
        assert not result.fired


# ── rule_franchisee_self_use_to_active ─────────────────────────────────


async def test_active_fires_on_operator_portal_click(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="franchisee_self_use")
        event = await _seed_event(
            session,
            uuid,
            event_type="academy.operator_portal_click",
            payload={"source_page": "academy_home"},
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        assert result.fired
        assert result.to_status == "franchisee_active"


async def test_active_fires_on_3_months_above_30k(session_factory) -> None:
    """Mock母艦 client returns three months > NT$30K → path (a) fires."""
    mothership.set_mothership_client_for_testing(
        _FakeMothership(
            monthly=[Decimal("32000"), Decimal("35000"), Decimal("40000")]
        )
    )
    try:
        uuid = uuid4()
        async with session_factory() as session:
            await _seed_transition(
                session, uuid, to_status="franchisee_self_use"
            )
            # Any non-portal-click event triggers re-evaluation; engagement
            # events keep arriving naturally.
            event = await _seed_event(
                session, uuid, event_type="engagement.deep"
            )
            result = await lifecycle.evaluate_event(session, uuid, event)
            assert result.fired
            assert result.to_status == "franchisee_active"
    finally:
        mothership.reset_mothership_client()


async def test_active_does_not_fire_when_below_threshold(session_factory) -> None:
    mothership.set_mothership_client_for_testing(
        _FakeMothership(
            monthly=[Decimal("32000"), Decimal("10000"), Decimal("40000")]
        )
    )
    try:
        uuid = uuid4()
        async with session_factory() as session:
            await _seed_transition(
                session, uuid, to_status="franchisee_self_use"
            )
            event = await _seed_event(
                session, uuid, event_type="engagement.deep"
            )
            result = await lifecycle.evaluate_event(session, uuid, event)
            assert not result.fired
    finally:
        mothership.reset_mothership_client()


async def test_active_does_not_fire_when_not_self_use(session_factory) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        await _seed_transition(session, uuid, to_status="loyalist")
        event = await _seed_event(
            session, uuid, event_type="academy.operator_portal_click"
        )
        result = await lifecycle.evaluate_event(session, uuid, event)
        # Must reach franchisee_self_use first.
        assert not result.fired


# ── force_transition validation ────────────────────────────────────────


@pytest.mark.parametrize(
    "invalid",
    [
        "unknown_state",
        "FRANCHISEE_SELF_USE",
        "",
        # Old ADR-003 stages — must be rejected post-ADR-008.
        "registered",
        "engaged",
        "franchisee",
    ],
)
async def test_force_transition_rejects_invalid(session_factory, invalid) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        with pytest.raises(ValueError):
            await lifecycle.force_transition(session, uuid, invalid)


@pytest.mark.parametrize(
    "valid",
    [
        "visitor",
        "loyalist",
        "applicant",
        "franchisee_self_use",
        "franchisee_active",
    ],
)
async def test_force_transition_accepts_all_5_stages(session_factory, valid) -> None:
    uuid = uuid4()
    async with session_factory() as session:
        t = await lifecycle.force_transition(session, uuid, valid)
        assert t.to_status == valid
