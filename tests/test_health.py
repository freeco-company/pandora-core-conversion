"""Health endpoint sanity check."""

from __future__ import annotations


async def test_health_ok(client) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "pandora-core-conversion"
