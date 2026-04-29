"""Pydantic schemas for conversion endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ADR-008 §2.2 minimum event set. Other event_type strings are allowed
# (logged for analytics) but only these drive lifecycle transitions.
EVENT_TYPES = {
    "app.opened",
    "engagement.deep",
    "subscription.premium_active",
    "franchise.cta_view",
    "franchise.cta_click",
    "mothership.consultation_submitted",
    "mothership.first_order",
    "academy.operator_portal_click",
}

# ADR-008 §2.2 — 5 stages (was 6 in ADR-003).
LIFECYCLE_STATUSES = {
    "visitor",
    "loyalist",
    "applicant",
    "franchisee_self_use",
    "franchisee_active",
}


class EventIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1, max_length=32)
    event_type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
    customer_id: int | None = None  # 母艦 customers.id (optional, for legacy join)


class EventIngestResponse(BaseModel):
    id: int
    lifecycle_transition: str | None = None  # to_status if a transition fired


class LifecycleTransitionItem(BaseModel):
    from_status: str | None
    to_status: str
    transitioned_at: datetime
    trigger_event_id: int | None


class LifecycleResponse(BaseModel):
    pandora_user_uuid: UUID
    current_status: str
    history: list[LifecycleTransitionItem]


class LifecycleTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_status: str
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── Internal endpoints (service-to-service, not user-scoped) ───────────


class InternalEventIngestRequest(BaseModel):
    """Used by App backends to publish events on behalf of a user.

    Auth: X-Internal-Secret header. The body MUST include `pandora_user_uuid`
    because there's no JWT subject to derive it from.
    """

    model_config = ConfigDict(extra="forbid")

    pandora_user_uuid: UUID
    app_id: str = Field(min_length=1, max_length=32)
    event_type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
    customer_id: int | None = None


class AdminLifecycleOverrideRequest(BaseModel):
    """Admin-driven lifecycle stage override (any → any).

    Generalisation of `FranchiseeQualifyRequest`. Used by 集團 admin tooling
    (e.g. pandora-meal Filament FranchiseLeadResource) when an admin needs to
    correct lifecycle state outside the natural state machine — e.g. demoting
    a wrongly-promoted user, or skipping a stage on operator confirmation.

    Auth: X-Internal-Secret. `actor` and `reason` are required for audit.
    """

    model_config = ConfigDict(extra="forbid")

    to_status: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=500)
    actor: str = Field(min_length=1, max_length=128)


class FranchiseeQualifyRequest(BaseModel):
    """Admin-side override for `franchisee_self_use`.

    Used as fallback / 對帳 when母艦 first-order webhook is missed
    (ADR-008 §2.3 訊號源 = 婕樂纖後台).
    """

    model_config = ConfigDict(extra="forbid")

    plan_chosen: str | None = Field(default=None, max_length=16)
    note: str | None = Field(default=None, max_length=500)


class FunnelStageMetric(BaseModel):
    status: str
    count: int


class FunnelMetricsResponse(BaseModel):
    stages: list[FunnelStageMetric]
    total_users_with_lifecycle: int
