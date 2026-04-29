"""Lifecycle state machine. ADR-008 §2.2 (supersedes ADR-003).

Implements four event-driven transitions across five stages:

    visitor              -> loyalist               on 14-day continuous engagement
                                                   OR Premium subscription active
    loyalist             -> applicant              on franchise.cta_click
                                                   OR mothership consultation form
    applicant            -> franchisee_self_use    on mothership first order ≥ NT$6,600
    franchisee_self_use  -> franchisee_active      on (a) 連續 3 個月月進貨 > NT$30K
                                                   OR (b) academy operator portal click

ADR-003 stages `registered` / `engaged` / `franchisee` 已作廢。仙女學院
training_progress 訊號 (`academy.training_progress`) 也作廢 — 段 1 不依賴
仙女學院。詳見 ADR-008 §3.2。

`force_transition` 仍保留作 admin / 對帳路徑（routes.qualify_franchisee_self_use）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversion import cache_invalidator

from app.conversion.models import ConversionEvent, LifecycleTransition
from app.conversion.mothership import get_mothership_client

# Allowed states (ADR-008 §2.2). Order matters for funnel reporting.
STATES = [
    "visitor",
    "loyalist",
    "applicant",
    "franchisee_self_use",
    "franchisee_active",
]

# Tunables (ADR-008 §2.2 — initial defaults, tune post-launch).
VISITOR_LOYALIST_CONTINUOUS_DAYS = 14
FRANCHISEE_ACTIVE_MONTHLY_THRESHOLD = Decimal("30000")
FRANCHISEE_ACTIVE_REQUIRED_MONTHS = 3
FRANCHISEE_SELF_USE_FIRST_ORDER_THRESHOLD = Decimal("6600")

# Event-type constants. Treat as the public contract — see schemas.EVENT_TYPES.
EVT_ENGAGEMENT_DEEP = "engagement.deep"
EVT_SUBSCRIPTION_PREMIUM_ACTIVE = "subscription.premium_active"
EVT_FRANCHISE_CTA_CLICK = "franchise.cta_click"
EVT_MOTHERSHIP_CONSULTATION_SUBMITTED = "mothership.consultation_submitted"
EVT_MOTHERSHIP_FIRST_ORDER = "mothership.first_order"
EVT_ACADEMY_OPERATOR_PORTAL_CLICK = "academy.operator_portal_click"


@dataclass
class TransitionContext:
    session: AsyncSession
    pandora_user_uuid: UUID
    event: ConversionEvent
    current_status: str | None


# A rule returns the target status (str) if it fires, else None.
TransitionRule = Callable[[TransitionContext], Awaitable[str | None]]


# ── visitor -> loyalist ────────────────────────────────────────────────


async def rule_visitor_to_loyalist(ctx: TransitionContext) -> str | None:
    """visitor -> loyalist (ADR-008 §2.2 transition #1).

    Two paths (OR):
      A) `subscription.premium_active` event arrives — immediate signal.
      B) ≥ VISITOR_LOYALIST_CONTINUOUS_DAYS distinct calendar days of
         `engagement.deep` events ending on the trigger event's day. We
         require *continuous* days (no gaps) — a one-day gap resets the
         streak.

    Only evaluated when current_status is None or "visitor". A user that's
    already past loyalist won't regress.
    """
    if ctx.current_status not in (None, "visitor"):
        return None

    if ctx.event.event_type == EVT_SUBSCRIPTION_PREMIUM_ACTIVE:
        return "loyalist"

    if ctx.event.event_type != EVT_ENGAGEMENT_DEEP:
        return None

    # Path B: continuous-day streak from engagement.deep events.
    # Pull distinct days within the lookback window (a bit more than the
    # required streak, to detect gap-resets at the boundary).
    lookback_days = VISITOR_LOYALIST_CONTINUOUS_DAYS + 7
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    stmt = (
        select(distinct(func.date(ConversionEvent.occurred_at)))
        .where(
            ConversionEvent.pandora_user_uuid == ctx.pandora_user_uuid,
            ConversionEvent.event_type == EVT_ENGAGEMENT_DEEP,
            ConversionEvent.occurred_at >= cutoff,
        )
    )
    rows = (await ctx.session.execute(stmt)).scalars().all()
    days: set[tuple[int, int, int]] = set()
    for row in rows:
        if isinstance(row, str):
            parsed = datetime.fromisoformat(row).date()
            days.add((parsed.year, parsed.month, parsed.day))
        elif isinstance(row, datetime):
            days.add((row.year, row.month, row.day))
        else:
            days.add((row.year, row.month, row.day))

    if not days:
        return None

    # Walk backwards from today: how many consecutive days ending today
    # (or the trigger-event's day) are present?
    today = ctx.event.occurred_at.date()
    streak = 0
    cursor = today
    while (cursor.year, cursor.month, cursor.day) in days:
        streak += 1
        cursor = cursor - timedelta(days=1)
        if streak >= VISITOR_LOYALIST_CONTINUOUS_DAYS:
            return "loyalist"
    return None


# ── loyalist -> applicant ──────────────────────────────────────────────


async def rule_loyalist_to_applicant(ctx: TransitionContext) -> str | None:
    """loyalist -> applicant (ADR-008 §2.2 transition #2).

    Triggers (OR):
      - `franchise.cta_click` (App-side CTA in 朵朵 / 肌膚 / 月曆 / 母艦)
      - `mothership.consultation_submitted` (婕樂纖後台收到諮詢表單)
    """
    if ctx.current_status != "loyalist":
        return None
    if ctx.event.event_type in (
        EVT_FRANCHISE_CTA_CLICK,
        EVT_MOTHERSHIP_CONSULTATION_SUBMITTED,
    ):
        return "applicant"
    return None


# ── applicant -> franchisee_self_use ───────────────────────────────────


async def rule_applicant_to_franchisee_self_use(
    ctx: TransitionContext,
) -> str | None:
    """applicant -> franchisee_self_use (ADR-008 §2.2 transition #3).

    Trigger: `mothership.first_order` event with payload.amount ≥ NT$6,600.
    Source: 婕樂纖後台首單成立 webhook → py-service. See ADR-008 §2.3.
    """
    if ctx.current_status != "applicant":
        return None
    if ctx.event.event_type != EVT_MOTHERSHIP_FIRST_ORDER:
        return None
    amount_raw = (ctx.event.payload or {}).get("amount")
    if amount_raw is None:
        return None
    try:
        amount = Decimal(str(amount_raw))
    except (ValueError, ArithmeticError):
        return None
    if amount < FRANCHISEE_SELF_USE_FIRST_ORDER_THRESHOLD:
        return None
    return "franchisee_self_use"


# ── franchisee_self_use -> franchisee_active ───────────────────────────


async def rule_franchisee_self_use_to_active(
    ctx: TransitionContext,
) -> str | None:
    """franchisee_self_use -> franchisee_active (ADR-008 §2.2 transition #4).

    Two paths (OR):
      (a) Last FRANCHISEE_ACTIVE_REQUIRED_MONTHS months from MothershipOrderClient
          all > FRANCHISEE_ACTIVE_MONTHLY_THRESHOLD.
      (b) `academy.operator_portal_click` event — user actively explored
          the operator track.

    Path (a) is evaluated on every event while in this state — it's a
    pull-based check against the mothership. Mothership endpoint may not
    exist yet (separate母艦 PR); the client falls back to zeros so this
    branch silently no-ops until母艦 ships it.
    """
    if ctx.current_status != "franchisee_self_use":
        return None

    # Path (b): cheap event-type check first.
    if ctx.event.event_type == EVT_ACADEMY_OPERATOR_PORTAL_CLICK:
        return "franchisee_active"

    # Path (a): fetch monthly purchases. Conservative — only run once we
    # have a state transition into franchisee_self_use; rule guard above
    # already ensures that.
    monthly = await get_mothership_client().get_monthly_purchases(
        ctx.pandora_user_uuid, months=FRANCHISEE_ACTIVE_REQUIRED_MONTHS
    )
    if len(monthly) < FRANCHISEE_ACTIVE_REQUIRED_MONTHS:
        return None
    if all(m > FRANCHISEE_ACTIVE_MONTHLY_THRESHOLD for m in monthly):
        return "franchisee_active"
    return None


DEFAULT_RULES: list[TransitionRule] = [
    rule_visitor_to_loyalist,
    rule_loyalist_to_applicant,
    rule_applicant_to_franchisee_self_use,
    rule_franchisee_self_use_to_active,
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
        cache_invalidator.schedule_invalidate(
            pandora_user_uuid=pandora_user_uuid,
            from_status=current,
            to_status=target,
        )
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
    """Admin / internal-triggered transition.

    Used by `qualify-franchisee-self-use` admin endpoint as a fallback /
    reconcile path when母艦 webhook is missed. Validates against STATES.
    """
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
    cache_invalidator.schedule_invalidate(
        pandora_user_uuid=pandora_user_uuid,
        from_status=current,
        to_status=to_status,
    )
    return transition
