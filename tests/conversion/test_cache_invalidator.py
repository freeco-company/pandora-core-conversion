"""Tests for lifecycle cache_invalidator (PG-93)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from app.conversion import cache_invalidator
from app.conversion.cache_invalidator import _sign, invalidate, schedule_invalidate


@pytest.fixture
def consumer_env(monkeypatch):
    monkeypatch.setenv("LIFECYCLE_INVALIDATE_CONSUMERS", "pandora_meal")
    monkeypatch.setenv(
        "LIFECYCLE_INVALIDATE_CONSUMER_PANDORA_MEAL_URL",
        "http://meal.local/internal/lifecycle/invalidate",
    )
    monkeypatch.setenv(
        "LIFECYCLE_INVALIDATE_CONSUMER_PANDORA_MEAL_SECRET", "test-secret"
    )


def _make_transport(captured: list[dict[str, Any]], status_code: int = 200):
    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": request.content,
            }
        )
        return httpx.Response(status_code, json={"ok": True})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_invalidate_signs_and_posts(consumer_env):
    captured: list[dict[str, Any]] = []
    transport = _make_transport(captured)

    _OrigClient = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _OrigClient(transport=transport, **kwargs)

    user = uuid4()
    with patch.object(cache_invalidator.httpx, "AsyncClient", _client_factory):
        await invalidate(
            pandora_user_uuid=user,
            from_status="applicant",
            to_status="franchisee_self_use",
        )

    assert len(captured) == 1
    req = captured[0]
    assert req["url"] == "http://meal.local/internal/lifecycle/invalidate"
    headers = req["headers"]
    assert "x-pandora-timestamp" in headers
    assert "x-pandora-nonce" in headers
    assert headers["x-pandora-signature"].startswith("sha256=")

    # Verify HMAC signature is correct over the actual body
    expected = _sign(
        "test-secret",
        headers["x-pandora-timestamp"],
        headers["x-pandora-nonce"],
        req["body"],
    )
    assert headers["x-pandora-signature"] == expected

    body = json.loads(req["body"])
    assert body == {
        "pandora_user_uuid": str(user),
        "from_status": "applicant",
        "to_status": "franchisee_self_use",
    }


@pytest.mark.asyncio
async def test_invalidate_no_consumers_is_noop(monkeypatch):
    monkeypatch.delenv("LIFECYCLE_INVALIDATE_CONSUMERS", raising=False)
    # Should not raise even with zero consumers
    await invalidate(
        pandora_user_uuid=uuid4(), from_status=None, to_status="loyalist"
    )


@pytest.mark.asyncio
async def test_invalidate_consumer_missing_url_skips(monkeypatch):
    monkeypatch.setenv("LIFECYCLE_INVALIDATE_CONSUMERS", "ghost")
    # No URL/SECRET set for ghost → silently skipped, no exception
    await invalidate(
        pandora_user_uuid=uuid4(), from_status="visitor", to_status="loyalist"
    )


@pytest.mark.asyncio
async def test_invalidate_soft_fails_on_5xx(consumer_env, caplog):
    captured: list[dict[str, Any]] = []
    transport = _make_transport(captured, status_code=503)

    _OrigClient = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _OrigClient(transport=transport, **kwargs)

    with patch.object(cache_invalidator.httpx, "AsyncClient", _client_factory):
        # Must not raise; logs warning instead
        await invalidate(
            pandora_user_uuid=uuid4(),
            from_status="loyalist",
            to_status="applicant",
        )

    assert len(captured) == 1
    assert any("http 503" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_invalidate_soft_fails_on_network_error(consumer_env, caplog):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(handler)

    _OrigClient = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _OrigClient(transport=transport, **kwargs)

    with patch.object(cache_invalidator.httpx, "AsyncClient", _client_factory):
        await invalidate(
            pandora_user_uuid=uuid4(),
            from_status=None,
            to_status="loyalist",
        )

    assert any("network error" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_invalidate_fans_out_to_multiple_consumers(monkeypatch):
    monkeypatch.setenv("LIFECYCLE_INVALIDATE_CONSUMERS", "meal,calendar")
    monkeypatch.setenv(
        "LIFECYCLE_INVALIDATE_CONSUMER_MEAL_URL", "http://a.local/x"
    )
    monkeypatch.setenv("LIFECYCLE_INVALIDATE_CONSUMER_MEAL_SECRET", "s1")
    monkeypatch.setenv(
        "LIFECYCLE_INVALIDATE_CONSUMER_CALENDAR_URL", "http://b.local/x"
    )
    monkeypatch.setenv("LIFECYCLE_INVALIDATE_CONSUMER_CALENDAR_SECRET", "s2")

    captured: list[dict[str, Any]] = []
    transport = _make_transport(captured)

    _OrigClient = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _OrigClient(transport=transport, **kwargs)

    with patch.object(cache_invalidator.httpx, "AsyncClient", _client_factory):
        await invalidate(
            pandora_user_uuid=uuid4(), from_status=None, to_status="loyalist"
        )

    urls = sorted(req["url"] for req in captured)
    assert urls == ["http://a.local/x", "http://b.local/x"]


@pytest.mark.asyncio
async def test_schedule_invalidate_inside_loop(consumer_env):
    captured: list[dict[str, Any]] = []
    transport = _make_transport(captured)

    _OrigClient = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _OrigClient(transport=transport, **kwargs)

    with patch.object(cache_invalidator.httpx, "AsyncClient", _client_factory):
        schedule_invalidate(
            pandora_user_uuid=uuid4(), from_status=None, to_status="loyalist"
        )
        # Yield to let the scheduled task run to completion
        await asyncio.sleep(0.05)

    assert len(captured) == 1


def test_schedule_invalidate_no_loop_is_noop(consumer_env):
    # Sync context, no running loop → must silently no-op (not raise)
    schedule_invalidate(
        pandora_user_uuid=uuid4(), from_status=None, to_status="loyalist"
    )
