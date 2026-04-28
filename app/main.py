"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.auth.jwt_verifier import get_jwt_verifier
from app.config import get_settings
from app.conversion.routes import router as conversion_router
from app.health.routes import router as health_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.setLevel(settings.log_level)
    # Best-effort warm public-key cache; failures are tolerated in dev.
    verifier = get_jwt_verifier()
    try:
        await verifier.refresh_public_key()
    except Exception as exc:  # pragma: no cover - dev convenience
        logger.warning("JWT public key warm-up failed: %s", exc)
    yield


app = FastAPI(
    title="Pandora Core Conversion Service",
    version="0.1.0",
    description="Conversion funnel — 5-stage lifecycle (ADR-008 supersedes ADR-003).",
    lifespan=lifespan,
)

app.include_router(health_router, tags=["health"])
app.include_router(conversion_router, prefix="/api/v1", tags=["conversion"])
