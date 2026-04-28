"""Lifecycle state machine. ADR-003 §2.2.

v1 implements only the `visitor -> registered` rule (first `app.opened` event).
Other transitions are stubbed with TODO markers; complex business rules
(60-day engagement window, 3-month + repeat purchase loyalist test, etc.)
land in subsequent PRs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversion.models import ConversionEvent, LifecycleTransition

# Allowed states (ADR-003 §2.2)
STATES = ["visitor", "registered", "engaged", "loyalist", "applicant", "franchisee"]


@dataclass
class TransitionContext:
    session: AsyncSession
    pandora_user_uuid: UUID
    event: ConversionEvent
    current_status: str | None


# A rule returns the target status (str) if it fires, else None.
TransitionRule = Callable[[TransitionContext], Awaitable[str | None]]


async def rule_first_app_opened(ctx: TransitionContext) -> str | None:
    """visitor -> registered on first app.opened event for this uuid."""
    if ctx.event.event_type != "app.opened":
        return None
    if ctx.current_status not in (None, "visitor"):
        return None
    # First event check: any prior event for this uuid?
    stmt = select(ConversionEvent.id).where(
        ConversionEvent.pandora_user_uuid == ctx.pandora_user_uuid,
        ConversionEvent.id != ctx.event.id,
    ).limit(1)
    res = await ctx.session.execute(stmt)
    if res.scalar() is None:
        return "registered"
    return None


async def rule_engaged(ctx: TransitionContext) -> str | None:
    """TODO: registered -> engaged when engagement window >= 60 days OR subscription event.

    Skipped in v1 skeleton; needs subscription event source + day-bucket aggregation.
    """
    return None


async def rule_loyalist(ctx: TransitionContext) -> str | None:
    """TODO: engaged -> loyalist when 3 consecutive months active + >=2 母艦 repeat purchases.

    Skipped in v1 skeleton; needs cross-app aggregation + 母艦 order data join.
    """
    return None


async def rule_applicant(ctx: TransitionContext) -> str | None:
    """TODO: loyalist -> applicant on franchise.cta_click event."""
    return None


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
        .order_by(LifecycleTransition.transitioned_at.desc(), LifecycleTransition.id.desc())
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

    return TransitionResult(fired=False, from_status=current, to_status=None, metadata={})


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
