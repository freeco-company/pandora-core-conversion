"""Pydantic schemas for conversion endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ADR-003 §2.3 minimum event set (other event_type strings allowed but logged for analytics).
EVENT_TYPES = {
    "app.opened",
    "engagement.deep",
    "franchise.cta_view",
    "franchise.cta_click",
    "academy.training_progress",
}

LIFECYCLE_STATUSES = {
    "visitor",
    "registered",
    "engaged",
    "loyalist",
    "applicant",
    "franchisee",
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


class TrainingProgressItem(BaseModel):
    chapter_id: str
    completed_at: datetime | None
    quiz_score: int | None
    attempts: int


class TrainingProgressResponse(BaseModel):
    pandora_user_uuid: UUID
    chapters: list[TrainingProgressItem]


class TrainingProgressUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=64)
    completed: bool = False
    quiz_score: int | None = Field(default=None, ge=0, le=100)


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


class FranchiseeQualifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_chosen: str | None = Field(default=None, max_length=16)
    note: str | None = Field(default=None, max_length=500)


class FunnelStageMetric(BaseModel):
    status: str
    count: int


class FunnelMetricsResponse(BaseModel):
    stages: list[FunnelStageMetric]
    total_users_with_lifecycle: int


