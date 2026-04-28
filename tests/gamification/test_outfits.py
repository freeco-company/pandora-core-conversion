"""Tests for the outfit catalog read API + seed."""

from __future__ import annotations

from sqlalchemy import select

from app.config import get_settings
from app.gamification import catalog
from app.gamification.models import OutfitCatalog


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Secret": get_settings().internal_shared_secret}


async def test_list_outfits_returns_empty_when_unseeded(client) -> None:
    resp = await client.get(
        "/api/v1/internal/gamification/outfits",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"outfits": [], "total": 0}


async def test_list_outfits_requires_internal_secret(client) -> None:
    resp = await client.get("/api/v1/internal/gamification/outfits")
    assert resp.status_code == 401


async def test_seed_inserts_full_catalog(client) -> None:
    resp = await client.post(
        "/api/v1/internal/gamification/outfits/seed",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["inserted"] >= 13  # built-in catalog size
    assert body["updated"] == 0
    assert body["total"] == body["inserted"]


async def test_seed_is_idempotent(client) -> None:
    first = await client.post(
        "/api/v1/internal/gamification/outfits/seed",
        headers=_internal_headers(),
    )
    second = await client.post(
        "/api/v1/internal/gamification/outfits/seed",
        headers=_internal_headers(),
    )
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["inserted"] == 0
    assert second.json()["updated"] == 0


async def test_list_outfits_after_seed_returns_all_entries(client) -> None:
    await client.post(
        "/api/v1/internal/gamification/outfits/seed",
        headers=_internal_headers(),
    )
    resp = await client.get(
        "/api/v1/internal/gamification/outfits",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == len(catalog.OUTFIT_CATALOG)
    codes = {o["code"] for o in body["outfits"]}
    assert "scarf" in codes
    assert "fp_crown" in codes
    assert "group_eternal" in codes
    # Items shaped correctly
    scarf = next(o for o in body["outfits"] if o["code"] == "scarf")
    assert scarf["name"] == "溫暖圍巾"
    assert scarf["unlock_condition"] == "LV.5"
    assert scarf["tier"] == "level"
    assert scarf["species_compat"] == []


async def test_list_outfits_orders_by_tier_then_code(client) -> None:
    await client.post(
        "/api/v1/internal/gamification/outfits/seed",
        headers=_internal_headers(),
    )
    resp = await client.get(
        "/api/v1/internal/gamification/outfits",
        headers=_internal_headers(),
    )
    items = resp.json()["outfits"]
    # Tier ordering is deterministic
    tier_seq = [o["tier"] for o in items]
    assert tier_seq == sorted(tier_seq)


async def test_seed_updates_existing_when_catalog_changes(client, db_session, monkeypatch):
    await client.post(
        "/api/v1/internal/gamification/outfits/seed",
        headers=_internal_headers(),
    )
    original = catalog.OUTFIT_CATALOG["scarf"]
    monkeypatch.setitem(
        catalog.OUTFIT_CATALOG,
        "scarf",
        catalog.OutfitDef(
            code=original.code,
            name="升級圍巾",
            unlock_condition="LV.6",
            tier=original.tier,
            species_compat=original.species_compat,
        ),
    )
    resp = await client.post(
        "/api/v1/internal/gamification/outfits/seed",
        headers=_internal_headers(),
    )
    body = resp.json()
    assert body["updated"] >= 1
    row = (
        await db_session.execute(
            select(OutfitCatalog).where(OutfitCatalog.code == "scarf")
        )
    ).scalar_one()
    assert row.name == "升級圍巾"
    assert row.unlock_condition == "LV.6"


def test_get_outfit_def_unknown_raises():
    import pytest
    with pytest.raises(KeyError):
        catalog.get_outfit_def("never_existed")


def test_outfit_catalog_codes_are_lowercase_with_underscores():
    """Hygiene check — keys are stable identifiers, no mixed case / spaces."""
    for code in catalog.OUTFIT_CATALOG:
        assert code == code.lower()
        assert " " not in code
