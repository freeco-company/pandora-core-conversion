"""Pydantic schemas for gamification API."""

from __future__ import annotations

from datetime import date, datetime
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


class AwardAchievementRequest(BaseModel):
    pandora_user_uuid: UUID
    code: str = Field(..., min_length=1, max_length=64)
    source_app: str = Field(..., min_length=1, max_length=32)
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    occurred_at: datetime
    metadata: dict = Field(default_factory=dict)


class AwardAchievementResponse(BaseModel):
    awarded: bool  # False = already had it (idempotent)
    code: str
    tier: str
    xp_delta: int
    total_xp: int
    group_level: int
    leveled_up_to: int | None = None


class SeedAchievementsResponse(BaseModel):
    inserted: int
    updated: int
    total: int


class OutfitItem(BaseModel):
    code: str
    name: str
    unlock_condition: str
    tier: str
    species_compat: list[str]


class OutfitCatalogResponse(BaseModel):
    outfits: list[OutfitItem]
    total: int


class SeedOutfitsResponse(BaseModel):
    inserted: int
    updated: int
    total: int


class UserOutfitItem(BaseModel):
    code: str
    awarded_at: datetime
    awarded_via: str


class UserOutfitsResponse(BaseModel):
    pandora_user_uuid: UUID
    outfits: list[UserOutfitItem]
    total: int


class GrantOutfitRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    awarded_via: str = Field(default="manual", min_length=1, max_length=32)


class GrantOutfitResponse(BaseModel):
    granted: bool
    code: str


class MascotManifestItem(BaseModel):
    species: str
    stage: int
    mood: str
    outfit_code: str
    sprite_url: str
    animation_url: str
    updated_at: datetime


class MascotManifestResponse(BaseModel):
    entries: list[MascotManifestItem]
    total: int


class SeedMascotManifestResponse(BaseModel):
    inserted: int
    total: int


class MascotManifestUpsertItem(BaseModel):
    species: str = Field(..., min_length=1, max_length=32)
    stage: int = Field(..., ge=1, le=5)
    mood: str = Field(..., min_length=1, max_length=32)
    outfit_code: str = Field(default="none", min_length=1, max_length=64)
    sprite_url: str = Field(default="", max_length=512)
    animation_url: str = Field(default="", max_length=512)


class MascotManifestUpsertRequest(BaseModel):
    entries: list[MascotManifestUpsertItem] = Field(..., min_length=1)


class MascotManifestUpsertResponse(BaseModel):
    inserted: int
    updated: int
    total_in_request: int


class BootstrapLedgerEntry(BaseModel):
    """One user's pre-existing total_xp to seed into the ledger.

    `source_app` lets the caller tag where the legacy XP came from (typically
    "dodo" for the Phase B initial migration of dodo's ~50 prod users).
    """

    pandora_user_uuid: UUID
    total_xp: int = Field(..., ge=0)
    source_app: str = Field(default="dodo", min_length=1, max_length=32)


class BootstrapLedgerRequest(BaseModel):
    """Batch request — bulk-bootstrap multiple users in one call."""

    entries: list[BootstrapLedgerEntry] = Field(..., min_length=1, max_length=1000)


class BootstrapLedgerResultItem(BaseModel):
    pandora_user_uuid: UUID
    bootstrapped: bool  # False = already had a bootstrap entry (idempotent skip)
    total_xp: int
    group_level: int


class BootstrapLedgerResponse(BaseModel):
    results: list[BootstrapLedgerResultItem]
    new_bootstraps: int
    skipped: int
    total_in_request: int


class UserAchievementItem(BaseModel):
    code: str
    tier: str
    awarded_at: datetime
    source_app: str


class GroupStreakResponse(BaseModel):
    """Master cross-App daily-login streak snapshot for one user.

    `today_in_streak` is the convenience flag App backends overlay on their
    own toast: True = the user has already logged in to *some* App today
    (Asia/Taipei). False = streak is "live but not yet bumped today" or empty.
    """

    user_uuid: UUID
    current_streak: int
    longest_streak: int
    last_login_date: date | None = None
    last_seen_app: str | None = None
    today_in_streak: bool


class UserSyncSnapshotResponse(BaseModel):
    """Full reconciliation snapshot for one user.

    Apps poll this on login or as a webhook-gap fallback to bring local mirror
    tables (achievements / outfits / progression) back in sync without needing
    to replay the outbox. Always returns a baseline progression even for users
    that don't yet have a row (synthesised LV.1).
    """

    pandora_user_uuid: UUID
    progression: ProgressionResponse
    achievements: list[UserAchievementItem]
    outfits: list[UserOutfitItem]
