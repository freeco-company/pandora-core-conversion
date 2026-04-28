"""Gamification outbox enqueue + dispatcher. ADR-009 §2.2.

One outbox row per (event, configured consumer). Dispatcher picks up pending
rows whose `next_retry_at` has passed, POSTs to the consumer's URL with HMAC
headers, and transitions the row to `sent` / failed / dead_letter.

Failure semantics mirror the identity outbox in pandora-core-identity:
    - HTTP 5xx / network → exponential backoff (1m, 5m, 15m, 1h, 6h)
    - HTTP 4xx → dead_letter immediately (consumer rejected the payload)
    - 5+ retries → dead_letter

Receiver verifies via:
    - X-Pandora-Timestamp (ISO-8601 UTC)
    - X-Pandora-Nonce (random per send)
    - X-Pandora-Signature ("sha256="+ HMAC-SHA256(secret, timestamp+"."+nonce+"."+body))
And should reject events older than `gamification_max_clock_skew_seconds`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.gamification.models import GamificationOutboxEvent

# Backoff schedule (seconds) by retry_count. Last entry repeats until DLQ at 5+.
BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 900, 3600, 21600)
MAX_RETRIES: int = 5


def consumer_config(name: str) -> tuple[str, str] | None:
    """Read URL+secret for a named consumer from os.environ.

    Convention: GAMIFICATION_CONSUMER_<NAME>_URL / _SECRET (uppercased).
    Returns None when either is missing — keeps the outbox path safe in dev.
    """
    upper = name.upper()
    url = os.environ.get(f"GAMIFICATION_CONSUMER_{upper}_URL", "").strip()
    secret = os.environ.get(f"GAMIFICATION_CONSUMER_{upper}_SECRET", "").strip()
    if not url or not secret:
        return None
    return url, secret


async def enqueue_event(
    session: AsyncSession,
    *,
    event_type: str,
    pandora_user_uuid: UUID,
    payload: dict[str, Any],
    ledger_id: int | None = None,
) -> list[GamificationOutboxEvent]:
    """Write one outbox row per configured consumer.

    Returns the rows added (empty if no consumers configured).
    """
    settings = get_settings()
    consumers = settings.gamification_consumer_names
    if not consumers:
        return []

    event_id = (
        f"{event_type}.{ledger_id}" if ledger_id is not None else f"{event_type}.{uuid4()}"
    )
    rows: list[GamificationOutboxEvent] = []
    for consumer in consumers:
        row = GamificationOutboxEvent(
            event_id=event_id,
            event_type=event_type,
            pandora_user_uuid=pandora_user_uuid,
            consumer=consumer,
            payload=payload,
            status="pending",
            retry_count=0,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


def _sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    msg = timestamp.encode() + b"." + nonce.encode() + b"." + body
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _next_retry_at(retry_count: int) -> datetime:
    idx = min(retry_count, len(BACKOFF_SECONDS) - 1)
    return datetime.now(tz=UTC) + timedelta(seconds=BACKOFF_SECONDS[idx])


async def _dispatch_one(
    row: GamificationOutboxEvent, *, client: httpx.AsyncClient
) -> tuple[bool, str | None]:
    """POST a single outbox row. Returns (success, error_message)."""
    cfg = consumer_config(row.consumer)
    if cfg is None:
        # Consumer was de-configured between enqueue and dispatch — treat as
        # dead_letter rather than retrying forever.
        return False, "consumer_not_configured"
    url, secret = cfg
    body_dict = {
        "event_id": row.event_id,
        "event_type": row.event_type,
        "pandora_user_uuid": str(row.pandora_user_uuid),
        "payload": row.payload,
    }
    body = json.dumps(body_dict, separators=(",", ":"), sort_keys=True).encode()
    timestamp = datetime.now(tz=UTC).isoformat()
    nonce = secrets.token_hex(16)
    headers = {
        "Content-Type": "application/json",
        "X-Pandora-Timestamp": timestamp,
        "X-Pandora-Nonce": nonce,
        "X-Pandora-Signature": _sign(secret, timestamp, nonce, body),
    }
    try:
        resp = await client.post(url, content=body, headers=headers)
    except (httpx.RequestError, httpx.TimeoutException) as exc:
        return False, f"network: {exc.__class__.__name__}: {exc!s}"[:500]
    if 200 <= resp.status_code < 300:
        return True, None
    excerpt = resp.text[:200] if resp.text else ""
    return False, f"http {resp.status_code}: {excerpt}"


async def dispatch_pending(session: AsyncSession, *, limit: int = 100) -> dict[str, int]:
    """Dispatch up to `limit` pending rows whose `next_retry_at` has passed.

    Returns a small summary { sent, retried, dead_letter, skipped } — useful
    for the admin endpoint and tests.
    """
    settings = get_settings()
    now = datetime.now(tz=UTC)
    stmt = (
        select(GamificationOutboxEvent)
        .where(
            GamificationOutboxEvent.status == "pending",
            GamificationOutboxEvent.next_retry_at <= now,
        )
        .order_by(GamificationOutboxEvent.id.asc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    summary = {"sent": 0, "retried": 0, "dead_letter": 0, "skipped": 0}
    if not rows:
        return summary
    timeout = httpx.Timeout(settings.gamification_dispatch_timeout)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for row in rows:
            ok, err = await _dispatch_one(row, client=client)
            if ok:
                row.status = "sent"
                row.sent_at = datetime.now(tz=UTC)
                row.last_error = None
                summary["sent"] += 1
                continue
            row.last_error = err
            row.retry_count += 1
            # Configured-away consumer → dead-letter (no point retrying)
            if err == "consumer_not_configured":
                row.status = "dead_letter"
                summary["dead_letter"] += 1
                continue
            # 4xx → dead_letter immediately
            if err and err.startswith("http 4"):
                row.status = "dead_letter"
                summary["dead_letter"] += 1
                continue
            if row.retry_count >= MAX_RETRIES:
                row.status = "dead_letter"
                summary["dead_letter"] += 1
                continue
            row.next_retry_at = _next_retry_at(row.retry_count)
            summary["retried"] += 1
    await session.flush()
    return summary
