"""initial schema for MariaDB (post Postgres migration, 2026-05-02)

Squashes prior migrations 0001-0006 (PostgreSQL) into one fresh
MariaDB-compatible initial migration. Uses Base.metadata.create_all so the
schema always tracks the SQLAlchemy models — partition tables and other
PG-specific features are dropped.

Revision ID: 0001
Revises:
Create Date: 2026-05-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Import models so Base.metadata is fully populated before create_all.
from app.conversion import models as _conv_models  # noqa: F401
from app.db import Base
from app.gamification import models as _gam_models  # noqa: F401

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
