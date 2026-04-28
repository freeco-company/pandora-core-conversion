"""Lifecycle state machine. ADR-003 §2.2.

Implements the four event-driven transitions:

    visitor    -> registered    on first event for the uuid
    registered -> engaged       on subscription event OR ≥60 distinct event-days
    engaged    -> loyalist      on ≥3 distinct active months in last 90d
                                AND ≥2 母艦 repeat purchases (stubbed)
    loyalist   -> applicant     on franchise.cta_click

`applicant -> franchisee` is intentionally NOT auto-fired by rules — per
ADR-003 §7.1 婕樂纖團隊人工 / admin endpoint 處理。See
`force_transition` and routes.qualify_franchisee.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversion.models import ConversionEvent, LifecycleTransition
from app.conversion.mothership import get_mothership_client

# Allowed states (ADR-003 §2.2)
STATES = ["visitor", "registered", "engaged", "loyalist", "applicant", "franchisee"]

# Tunables (kept module-level for now — env-config can come later if needed).
ENGAGED_DAYS_THRESHOLD = 60
LOYALIST_LOOKBACK_DAYS = 90
LOYALIST_ACTIVE_MONTHS_REQUIRED = 3
LOYALIST_RECENT_ORDERS_REQUIRED = 2
SUBSCRIPTION_EVENT_TYPES = {"subscription.activated", "subscription.renewed"}


@dataclass
class TransitionContext:
    session: AsyncSession
    pandora_user_uuid: UUID
    event: ConversionEvent
    current_status: str | None


# A rule returns the target status (str) if it fires, else None.
TransitionRule = Callable[[TransitionContext], Awaitable[str | None]]


# ── visitor -> registered ──────────────────────────────────────────────


async def rule_first_app_opened(ctx: TransitionContext) -> str | None:
    """visitor -> registered on first app.opened event for this uuid."""
    if ctx.event.event_type != "app.opened":
        return None
    if ctx.current_status not in (None, "visitor"):
        return None
    # First event check: any prior event for this uuid?
    stmt = (
        select(ConversionEvent.id)
        .where(
            ConversionEvent.pandora_user_uuid == ctx.pandora_user_uuid,
            ConversionEvent.id != ctx.event.id,
        )
        .limit(1)
    )
    res = await ctx.session.execute(stmt)
    if res.scalar() is None:
        return "registered"
    return None


# ── registered -> engaged ──────────────────────────────────────────────


async def rule_engaged(ctx: TransitionContext) -> str | None:
    """registered -> engaged.

    Two paths:
      A) Any subscription.* event arrives (instant trigger).
      B) Distinct activity-day count for this uuid reaches ENGAGED_DAYS_THRESHOLD.

    Path B uses `DATE(occurred_at)` aggregation across all events. Cross-DB
    safe via `func.date(...)`. Cost: one COUNT(DISTINCT) per relevant event,
    but only after `current_status == 'registered'` so the hot path stays cheap.
    """
    if ctx.current_status != "registered":
        return None

    if ctx.event.event_type in SUBSCRIPTION_EVENT_TYPES:
        return "engaged"

    # Path B: distinct activity days
    stmt = select(
        func.count(distinct(func.date(ConversionEvent.occurred_at)))
    ).where(ConversionEvent.pandora_user_uuid == ctx.pandora_user_uuid)
    distinct_days = (await ctx.session.execute(stmt)).scalar() or 0
    if distinct_days >= ENGAGED_DAYS_THRESHOLD:
        return "engaged"
    return None


# ── engaged -> loyalist ────────────────────────────────────────────────


async def rule_loyalist(ctx: TransitionContext) -> str | None:
    """engaged -> loyalist.

    Conditions (both required, ADR-003 §2.2):
      1. ≥ LOYALIST_ACTIVE_MONTHS_REQUIRED distinct active months in the last
         LOYALIST_LOOKBACK_DAYS window. We approximate "month" by year+month
         of `occurred_at`.
      2. ≥ LOYALIST_RECENT_ORDERS_REQUIRED 母艦 repeat purchases — currently
         stubbed (returns 0). With the stub this branch never fires; v1 by
         design is conservative (see ADR-003 §6 risk mitigation).

    Note: we evaluate on every event while in `engaged` state — could be
    optimised later (e.g. only on engagement-significant events) but for now
    correctness > cost; aggregation is a single grouped COUNT.
    """
    if ctx.current_status != "engaged":
        return None

    cutoff = datetime.utcnow() - timedelta(days=LOYALIST_LOOKBACK_DAYS)
    # Cross-dialect month bucketing: pull distinct DATE(occurred_at), bucket
    # by (year, month) in Python. Cardinality is small (≤ LOOKBACK_DAYS rows).
    stmt = (
        select(distinct(func.date(ConversionEvent.occurred_at)))
        .where(
            ConversionEvent.pandora_user_uuid == ctx.pandora_user_uuid,
            ConversionEvent.occurred_at >= cutoff,
        )
    )
    rows = (await ctx.session.execute(stmt)).scalars().all()
    months: set[tuple[int, int]] = set()
    for row in rows:
        # row may come back as date, datetime, or str depending on dialect
        if isinstance(row, str):
            parsed = datetime.fromisoformat(row)
            months.add((parsed.year, parsed.month))
        else:
            months.add((row.year, row.month))
    if len(months) < LOYALIST_ACTIVE_MONTHS_REQUIRED:
        return None

    summary = await get_mothership_client().get_order_summary(ctx.pandora_user_uuid)
    if summary.recent_orders < LOYALIST_RECENT_ORDERS_REQUIRED:
        return None

    return "loyalist"


# ── loyalist -> applicant ──────────────────────────────────────────────


async def rule_applicant(ctx: TransitionContext) -> str | None:
    """loyalist -> applicant on franchise.cta_click event."""
    if ctx.event.event_type != "franchise.cta_click":
        return None
    if ctx.current_status != "loyalist":
        return None
    return "applicant"


# applicant -> franchisee is currently a manual / admin transition
# (ADR-003 §7.1 fairysalebox 暫人工處理).

DEFAULT_RULES: list[TransitionRule] = [
    rule_first_app_opened,
    rule_engaged,
    rule_loyalist,
    rule_applicant,
]


@dataclass
class TransitionResult:
    fired: bool
    from_status: str | None
    to_status: str | None
    metadata: dict[str, Any]


async def get_current_status(
    session: AsyncSession, pandora_user_uuid: UUID
) -> str | None:
    """Return latest lifecycle status, or None if no transitions yet."""
    stmt = (
        select(LifecycleTransition.to_status)
        .where(LifecycleTransition.pandora_user_uuid == pandora_user_uuid)
        .order_by(
            LifecycleTransition.transitioned_at.desc(), LifecycleTransition.id.desc()
        )
        .limit(1)
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def evaluate_event(
    session: AsyncSession,
    pandora_user_uuid: UUID,
    event: ConversionEvent,
    rules: list[TransitionRule] | None = None,
) -> TransitionResult:
    """Run all rules in order. First match wins. Persist transition if fired."""
    rules = rules or DEFAULT_RULES
    current = await get_current_status(session, pandora_user_uuid)
    ctx = TransitionContext(
        session=session,
        pandora_user_uuid=pandora_user_uuid,
        event=event,
        current_status=current,
    )

    for rule in rules:
        target = await rule(ctx)
        if target is None:
            continue
        if target == current:
            continue
        if target not in STATES:
            continue
        transition = LifecycleTransition(
            pandora_user_uuid=pandora_user_uuid,
            from_status=current,
            to_status=target,
            trigger_event_id=event.id,
            transitioned_at=datetime.utcnow(),
            extra_metadata={"rule": rule.__name__},
        )
        session.add(transition)
        await session.flush()
        return TransitionResult(
            fired=True,
            from_status=current,
            to_status=target,
            metadata={"rule": rule.__name__},
        )

    return TransitionResult(
        fired=False, from_status=current, to_status=None, metadata={}
    )


async def force_transition(
    session: AsyncSession,
    pandora_user_uuid: UUID,
    to_status: str,
    metadata: dict[str, Any] | None = None,
) -> LifecycleTransition:
    """Admin / internal-triggered transition (e.g. fairysalebox onboard, manual qualify)."""
    if to_status not in STATES:
        raise ValueError(f"invalid lifecycle status: {to_status}")
    current = await get_current_status(session, pandora_user_uuid)
    transition = LifecycleTransition(
        pandora_user_uuid=pandora_user_uuid,
        from_status=current,
        to_status=to_status,
        trigger_event_id=None,
        transitioned_at=datetime.utcnow(),
        extra_metadata=metadata or {"source": "manual"},
    )
    session.add(transition)
    await session.flush()
    return transition
