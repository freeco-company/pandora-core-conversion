"""SQLAlchemy models for conversion module. ADR-003 §3.1.

Note on partitioning: `conversion_events` is intended to be PARTITION BY RANGE
(occurred_at) on PostgreSQL for production. The Alembic migration creates the
partitioned parent + an initial monthly partition. The ORM treats it as a
normal table.
"""

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
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.db import Base


def _jsonb() -> JSON:
    """JSONB on PostgreSQL, JSON elsewhere (sqlite for tests)."""
    return JSON().with_variant(JSONB(), "postgresql")


class UUIDType(TypeDecorator):
    """Cross-dialect UUID: native PG UUID + CHAR(36) on sqlite (with bind/load conversion)."""

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return str(value) if not isinstance(value, str) else value

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)


def _uuid_col() -> UUIDType:
    """UUID column — native on PostgreSQL, CHAR(36) string on sqlite."""
    return UUIDType()


class ConversionEvent(Base):
    __tablename__ = "conversion_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pandora_user_uuid: Mapped[uuid.UUID] = mapped_column(_uuid_col(), nullable=False, index=True)
    customer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    app_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(
        _jsonb(), nullable=False, default=dict, server_default=text("'{}'")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LifecycleTransition(Base):
    __tablename__ = "lifecycle_transitions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pandora_user_uuid: Mapped[uuid.UUID] = mapped_column(_uuid_col(), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    trigger_event_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("conversion_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    transitioned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    extra_metadata: Mapped[dict] = mapped_column(
        "metadata", _jsonb(), nullable=False, default=dict, server_default=text("'{}'")
    )


class FranchiseTrainingProgress(Base):
    __tablename__ = "franchise_training_progress"

    pandora_user_uuid: Mapped[uuid.UUID] = mapped_column(
        _uuid_col(), primary_key=True, nullable=False
    )
    chapter_id: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quiz_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class FranchiseApplication(Base):
    __tablename__ = "franchise_applications"
    __table_args__ = (
        UniqueConstraint("pandora_user_uuid", name="uq_franchise_applications_uuid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pandora_user_uuid: Mapped[uuid.UUID] = mapped_column(_uuid_col(), nullable=False, index=True)
    source_app: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_content_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fairysalebox_pushed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    plan_chosen: Mapped[str | None] = mapped_column(String(16), nullable=True)
