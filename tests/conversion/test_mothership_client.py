"""Tests for HttpMothershipOrderClient + settings-driven default selection.

ADR-003 §3.2. Coverage:

  - happy path: signed GET → parsed summary
  - mothership 5xx → falls back to zero (no exception bubbles)
  - timeout → falls back to zero
  - settings without env → default client is stub
  - settings with env → default client is HTTP
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from uuid import uuid4

import httpx

from app.config import Settings
from app.conversion.mothership import (
    HttpMothershipOrderClient,
    StubMothershipOrderClient,
    _build_default_client,
)

# pytest-asyncio mode is "auto" per pyproject; async tests just work.


# ── settings → default client selection ────────────────────────────────


def test_default_client_is_stub_when_env_missing() -> None:
    s = Settings(mothership_base_url="", mothership_internal_secret="")
    assert isinstance(_build_default_client(s), StubMothershipOrderClient)


def test_default_client_is_http_when_env_set() -> None:
    s = Settings(
        mothership_base_url="https://mothership.example.com",
        mothership_internal_secret="s3cret",
    )
    assert isinstance(_build_default_client(s), HttpMothershipOrderClient)


# ── happy path ─────────────────────────────────────────────────────────


SECRET = "test-conv-secret-abc"
BASE_URL = "https://mothership.test"


def _make_handler(
    *,
    status_code: int = 200,
    json_body: dict | None = None,
    captured: list[httpx.Request] | None = None,
) -> Iterator:
    """Build an httpx MockTransport handler. Optionally capture requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        body = json_body if json_body is not None else {}
        return httpx.Response(status_code, json=body)

    return handler


async def test_happy_path_returns_parsed_summary() -> None:
    uuid = uuid4()
    captured: list[httpx.Request] = []
    handler = _make_handler(
        status_code=200,
        json_body={
            "pandora_user_uuid": str(uuid),
            "recent_orders_90d": 3,
            "total_orders": 7,
            "last_order_at": "2026-04-01T12:00:00+00:00",
        },
        captured=captured,
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as injected:
        client = HttpMothershipOrderClient(
            base_url=BASE_URL,
            secret=SECRET,
            client=injected,
        )
        summary = await client.get_order_summary(uuid)

    assert summary.recent_orders == 3
    assert summary.lifetime_orders == 7
    assert summary.pandora_user_uuid == uuid

    # Sanity: signature header must match the documented signing scheme.
    req = captured[0]
    ts = req.headers["X-Pandora-Timestamp"]
    sig = req.headers["X-Pandora-Signature"]
    expected_path = f"/api/internal/conversion/customer-orders/{uuid}"
    base = f"{ts}.GET.{expected_path}"
    expected_sig = hmac.new(
        SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    assert sig == expected_sig
    assert req.url.path == expected_path


# ── 5xx fallback ───────────────────────────────────────────────────────


async def test_5xx_after_retry_falls_back_to_zero() -> None:
    uuid = uuid4()
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(503, json={"error": "down"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as injected:
        client = HttpMothershipOrderClient(
            base_url=BASE_URL,
            secret=SECRET,
            client=injected,
        )
        summary = await client.get_order_summary(uuid)

    # Retried once, then fell back. Did NOT raise.
    assert call_count["n"] == 2
    assert summary.recent_orders == 0
    assert summary.lifetime_orders == 0


# ── timeout fallback ───────────────────────────────────────────────────


async def test_timeout_falls_back_to_zero() -> None:
    uuid = uuid4()
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        # Simulate a timeout — httpx.MockTransport surfaces raised exceptions
        # from the handler as transport errors to the caller.
        raise httpx.ConnectTimeout("simulated timeout")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as injected:
        client = HttpMothershipOrderClient(
            base_url=BASE_URL,
            secret=SECRET,
            client=injected,
        )
        summary = await client.get_order_summary(uuid)

    # Retried once → 2 calls total. Then fell back.
    assert call_count["n"] == 2
    assert summary.recent_orders == 0


# ── 404 (uuid_not_mapped) ──────────────────────────────────────────────


async def test_404_uuid_not_mapped_returns_zero() -> None:
    uuid = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"error": "customer not found", "reason": "uuid_not_mapped"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as injected:
        client = HttpMothershipOrderClient(
            base_url=BASE_URL,
            secret=SECRET,
            client=injected,
        )
        summary = await client.get_order_summary(uuid)

    assert summary.recent_orders == 0
    assert summary.lifetime_orders == 0


