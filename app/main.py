"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.auth.jwt_verifier import get_jwt_verifier
from app.config import get_settings
from app.conversion.routes import router as conversion_router
from app.gamification.routes import router as gamification_router
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
    title="Pandora Core py-service",
    version="0.2.0",
    description=(
        "Pandora group services. Modules: conversion (ADR-008 lifecycle), "
        "gamification (ADR-009 cross-app XP / level / achievements)."
    ),
    lifespan=lifespan,
)

app.include_router(health_router, tags=["health"])
app.include_router(conversion_router, prefix="/api/v1", tags=["conversion"])
app.include_router(gamification_router, prefix="/api/v1", tags=["gamification"])
