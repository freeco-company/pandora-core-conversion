"""Lifecycle cache invalidation webhook publisher (PG-93).

When py-service persists a `LifecycleTransition` (e.g. mothership 首單 webhook
推進 applicant→franchisee_self_use), downstream consumer apps (pandora-meal /
future 月曆 / 肌膚 / 學院) hold a per-uuid lifecycle cache. Without an explicit
invalidation signal they keep serving stale stage for up to the cache TTL (1h
in pandora-meal). This module fires a fire-and-forget webhook to each
configured consumer so they can `forget()` the cache entry immediately.

Design: deliberately lighter than `gamification/outbox.py` — losing an
invalidation merely delays freshness by the existing TTL (no functional
regression), so we skip the DB outbox + retry machinery and accept best-effort
delivery. Promote to outbox-backed if SLA tightens.

HMAC scheme mirrors gamification/outbox to keep receiver code uniform:
    X-Pandora-Timestamp / X-Pandora-Nonce / X-Pandora-Signature
    signature = "sha256=" + HMAC-SHA256(secret, timestamp + "." + nonce + "." + body)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: float = 3.0


def consumer_config(name: str) -> tuple[str, str] | None:
    """Read URL+secret for a named lifecycle-invalidate consumer from env.

    Convention: LIFECYCLE_INVALIDATE_CONSUMER_<NAME>_URL / _SECRET (uppercased).
    Returns None when either is missing → consumer skipped silently.
    """
    upper = name.upper()
    url = os.environ.get(f"LIFECYCLE_INVALIDATE_CONSUMER_{upper}_URL", "").strip()
    secret = os.environ.get(f"LIFECYCLE_INVALIDATE_CONSUMER_{upper}_SECRET", "").strip()
    if not url or not secret:
        return None
    return url, secret


def _consumer_names() -> list[str]:
    raw = os.environ.get("LIFECYCLE_INVALIDATE_CONSUMERS", "").strip()
    return [n.strip() for n in raw.split(",") if n.strip()]


def _sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    msg = timestamp.encode() + b"." + nonce.encode() + b"." + body
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _build_request(
    *,
    pandora_user_uuid: UUID,
    from_status: str | None,
    to_status: str,
) -> tuple[bytes, dict[str, str]]:
    """Build canonical body + signed headers (secret-free; caller signs per-consumer)."""
    body_dict: dict[str, Any] = {
        "pandora_user_uuid": str(pandora_user_uuid),
        "from_status": from_status,
        "to_status": to_status,
    }
    body = json.dumps(body_dict, separators=(",", ":"), sort_keys=True).encode()
    timestamp = datetime.now(tz=UTC).isoformat()
    nonce = secrets.token_hex(16)
    headers = {
        "Content-Type": "application/json",
        "X-Pandora-Timestamp": timestamp,
        "X-Pandora-Nonce": nonce,
    }
    return body, headers


async def _post_one(
    consumer: str,
    body: bytes,
    base_headers: dict[str, str],
    *,
    client: httpx.AsyncClient,
) -> None:
    cfg = consumer_config(consumer)
    if cfg is None:
        logger.debug("lifecycle invalidate: consumer %s not configured, skip", consumer)
        return
    url, secret = cfg
    headers = dict(base_headers)
    headers["X-Pandora-Signature"] = _sign(
        secret, headers["X-Pandora-Timestamp"], headers["X-Pandora-Nonce"], body
    )
    try:
        resp = await client.post(url, content=body, headers=headers)
    except (httpx.RequestError, httpx.TimeoutException) as exc:
        logger.warning(
            "lifecycle invalidate %s: network error %s: %s",
            consumer,
            exc.__class__.__name__,
            exc,
        )
        return
    if 200 <= resp.status_code < 300:
        logger.debug("lifecycle invalidate %s: ok (%d)", consumer, resp.status_code)
        return
    excerpt = resp.text[:200] if resp.text else ""
    logger.warning(
        "lifecycle invalidate %s: http %d %s", consumer, resp.status_code, excerpt
    )


async def invalidate(
    *,
    pandora_user_uuid: UUID,
    from_status: str | None,
    to_status: str,
) -> None:
    """Fan out cache-invalidate webhook to all configured consumers.

    Best-effort. Each failure is logged at WARNING but never raised — losing an
    invalidate at most delays freshness by the consumer's TTL.
    """
    consumers = _consumer_names()
    if not consumers:
        return
    body, headers = _build_request(
        pandora_user_uuid=pandora_user_uuid,
        from_status=from_status,
        to_status=to_status,
    )
    settings = get_settings()
    timeout = httpx.Timeout(
        getattr(settings, "lifecycle_invalidate_timeout", DEFAULT_TIMEOUT_SECONDS)
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        await asyncio.gather(
            *(_post_one(c, body, headers, client=client) for c in consumers),
            return_exceptions=True,
        )


def schedule_invalidate(
    *,
    pandora_user_uuid: UUID,
    from_status: str | None,
    to_status: str,
) -> None:
    """Fire-and-forget wrapper: schedule `invalidate` on the running event loop.

    Safe to call from sync paths inside an async context (e.g. directly after a
    DB flush in `evaluate_event`). If there is no running loop (test sync path),
    we simply skip — tests should call `invalidate` directly.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        invalidate(
            pandora_user_uuid=pandora_user_uuid,
            from_status=from_status,
            to_status=to_status,
        )
    )
