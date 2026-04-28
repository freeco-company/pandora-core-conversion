"""SQLAlchemy models for gamification module. ADR-009 §2.2."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.conversion.models import UUIDType  # reuse the cross-dialect UUID
from app.db import Base


def _jsonb() -> JSON:
    return JSON().with_variant(JSONB(), "postgresql")


def _uuid_col() -> UUIDType:
    return UUIDType()


class XpLedgerEntry(Base):
    """Event-sourced XP ledger.

    Every accepted event becomes one row. `idempotency_key` is unique per
    `source_app` and lets producers safely retry. `xp_delta` may be 0 (event
    capped / dropped but kept for audit).
    """

    __tablename__ = "xp_ledger_entries"
    __table_args__ = (
        UniqueConstraint(
            "source_app",
            "idempotency_key",
            name="uq_xp_ledger_source_idempotency",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    pandora_user_uuid: Mapped[uuid.UUID] = mapped_column(
        _uuid_col(), nullable=False, index=True
    )
    source_app: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    xp_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    extra_metadata: Mapped[dict] = mapped_column(
        "metadata", _jsonb(), nullable=False, default=dict, server_default=text("'{}'")
    )


class UserProgression(Base):
    """Snapshot of a user's lifetime XP + current group_level. Hot read path."""

    __tablename__ = "user_progression"

    pandora_user_uuid: Mapped[uuid.UUID] = mapped_column(
        _uuid_col(), primary_key=True, nullable=False
    )
    total_xp: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    group_level: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    level_anchor_xp: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    level_name_zh: Mapped[str] = mapped_column(String(32), nullable=False, default="種子期")
    level_name_en: Mapped[str] = mapped_column(String(32), nullable=False, default="Seed")
    last_level_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Achievement(Base):
    """Cross-app achievement catalog. Source-controlled definitions."""

    __tablename__ = "gamification_achievements"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    source_app: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # tier: bronze / silver / gold / legendary
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    xp_reward: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra_metadata: Mapped[dict] = mapped_column(
        "metadata", _jsonb(), nullable=False, default=dict, server_default=text("'{}'")
    )


class UserAchievement(Base):
    """A user's unlocked achievements. Composite PK enforces idempotent grant."""

    __tablename__ = "user_achievements"

    pandora_user_uuid: Mapped[uuid.UUID] = mapped_column(
        _uuid_col(), primary_key=True, nullable=False
    )
    code: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("gamification_achievements.code", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    awarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    source_app: Mapped[str] = mapped_column(String(32), nullable=False)


class GamificationOutboxEvent(Base):
    """Webhook fan-out outbox. ADR-009 §2.2 / ADR-007 outbox pattern.

    One row per (event, consumer). The dispatcher picks up `pending` rows whose
    `next_retry_at` has passed, POSTs to the consumer URL with HMAC headers,
    then transitions to `sent` or schedules retry / dead-letter.
    """

    __tablename__ = "gamification_outbox_events"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "consumer",
            name="uq_gamification_outbox_event_consumer",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    # Logical event_id (UUID-ish string) — same value across all per-consumer
    # rows of one event so receivers can de-dup if they receive from multiple
    # producers eventually. Composed by enqueue() as f"{event_type}.{ledger_id}".
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pandora_user_uuid: Mapped[uuid.UUID] = mapped_column(
        _uuid_col(), nullable=False, index=True
    )
    consumer: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(
        _jsonb(), nullable=False, default=dict, server_default=text("'{}'")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending", index=True
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
