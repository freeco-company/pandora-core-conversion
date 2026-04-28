"""initial conversion tables (ADR-003 §3.1)

Creates conversion_events as a partitioned table (RANGE on occurred_at) on
PostgreSQL with one initial monthly partition. Operators add future partitions
manually or via a maintenance job; partition automation is out of scope for v1.

Revision ID: 0001
Revises:
Create Date: 2026-04-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── conversion_events (partitioned parent) ──────────────────────────
    op.execute(
        """
        CREATE TABLE conversion_events (
            id BIGSERIAL NOT NULL,
            pandora_user_uuid UUID NOT NULL,
            customer_id BIGINT NULL,
            app_id VARCHAR(32) NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            occurred_at TIMESTAMPTZ NOT NULL,
            ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (id, occurred_at)
        ) PARTITION BY RANGE (occurred_at);
        """
    )
    op.execute(
        "CREATE INDEX ix_conversion_events_uuid ON conversion_events (pandora_user_uuid);"
    )
    op.execute(
        "CREATE INDEX ix_conversion_events_app_event ON conversion_events (app_id, event_type);"
    )
    op.execute(
        "CREATE INDEX ix_conversion_events_occurred_at ON conversion_events (occurred_at);"
    )
    # initial partition: 2026-04 .. 2026-05 (operators add subsequent months)
    op.execute(
        """
        CREATE TABLE conversion_events_2026_04 PARTITION OF conversion_events
            FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
        """
    )
    op.execute(
        """
        CREATE TABLE conversion_events_2026_05 PARTITION OF conversion_events
            FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
        """
    )

    # ── lifecycle_transitions ───────────────────────────────────────────
    op.create_table(
        "lifecycle_transitions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("pandora_user_uuid", UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("trigger_event_id", sa.BigInteger, nullable=True),
        sa.Column(
            "transitioned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index(
        "ix_lifecycle_transitions_uuid",
        "lifecycle_transitions",
        ["pandora_user_uuid"],
    )
    op.create_index(
        "ix_lifecycle_transitions_to_status",
        "lifecycle_transitions",
        ["to_status"],
    )

    # ── franchise_training_progress ─────────────────────────────────────
    op.create_table(
        "franchise_training_progress",
        sa.Column("pandora_user_uuid", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("chapter_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quiz_score", sa.Integer, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    )

    # ── franchise_applications ──────────────────────────────────────────
    op.create_table(
        "franchise_applications",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("pandora_user_uuid", UUID(as_uuid=True), nullable=False),
        sa.Column("source_app", sa.String(32), nullable=True),
        sa.Column("source_content_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fairysalebox_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plan_chosen", sa.String(16), nullable=True),
        sa.UniqueConstraint("pandora_user_uuid", name="uq_franchise_applications_uuid"),
    )
    op.create_index(
        "ix_franchise_applications_status",
        "franchise_applications",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_franchise_applications_status", table_name="franchise_applications")
    op.drop_table("franchise_applications")
    op.drop_table("franchise_training_progress")
    op.drop_index("ix_lifecycle_transitions_to_status", table_name="lifecycle_transitions")
    op.drop_index("ix_lifecycle_transitions_uuid", table_name="lifecycle_transitions")
    op.drop_table("lifecycle_transitions")
    op.execute("DROP TABLE IF EXISTS conversion_events_2026_05;")
    op.execute("DROP TABLE IF EXISTS conversion_events_2026_04;")
    op.execute("DROP TABLE IF EXISTS conversion_events;")
