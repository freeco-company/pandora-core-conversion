"""Pydantic schemas for gamification API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class InternalEventIngestRequest(BaseModel):
    """Payload pushed by App backends to /internal/gamification/events."""

    pandora_user_uuid: UUID
    source_app: str = Field(..., min_length=1, max_length=32)
    event_kind: str = Field(..., min_length=1, max_length=64)
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    occurred_at: datetime
    metadata: dict = Field(default_factory=dict)


class EventIngestResponse(BaseModel):
    id: int
    xp_delta: int
    total_xp: int
    group_level: int
    leveled_up_to: int | None = None
    duplicate: bool = False


class ProgressionResponse(BaseModel):
    pandora_user_uuid: UUID
    total_xp: int
    group_level: int
    level_name_zh: str
    level_name_en: str
    level_anchor_xp: int
    xp_to_next_level: int
    last_level_up_at: datetime | None = None
    updated_at: datetime | None = None
