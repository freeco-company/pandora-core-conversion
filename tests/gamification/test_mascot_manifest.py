"""Tests for mascot manifest list / seed / upsert."""

from __future__ import annotations

from app.config import get_settings
from app.gamification import catalog


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Secret": get_settings().internal_shared_secret}


def _expected_seed_count() -> int:
    return (
        len(catalog.MASCOT_SPECIES)
        * len(catalog.MASCOT_STAGES)
        * len(catalog.DEFAULT_MOODS)
    )


# ── list ───────────────────────────────────────────────────────────────


async def test_list_empty_when_unseeded(client) -> None:
    resp = await client.get(
        "/api/v1/internal/gamification/mascot-manifest",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"entries": [], "total": 0}


async def test_list_requires_internal_secret(client) -> None:
    resp = await client.get("/api/v1/internal/gamification/mascot-manifest")
    assert resp.status_code == 401


# ── seed ───────────────────────────────────────────────────────────────


async def test_seed_inserts_placeholder_for_every_combo(client) -> None:
    resp = await client.post(
        "/api/v1/internal/gamification/mascot-manifest/seed",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["inserted"] == _expected_seed_count()
    assert body["total"] == _expected_seed_count()


async def test_seed_is_idempotent(client) -> None:
    first = await client.post(
        "/api/v1/internal/gamification/mascot-manifest/seed",
        headers=_internal_headers(),
    )
    second = await client.post(
        "/api/v1/internal/gamification/mascot-manifest/seed",
        headers=_internal_headers(),
    )
    assert first.status_code == 200
    assert first.json()["inserted"] > 0
    assert second.json()["inserted"] == 0
    # Total unchanged
    assert second.json()["total"] == first.json()["total"]


async def test_seed_yields_blank_urls_marking_placeholders(client) -> None:
    await client.post(
        "/api/v1/internal/gamification/mascot-manifest/seed",
        headers=_internal_headers(),
    )
    resp = await client.get(
        "/api/v1/internal/gamification/mascot-manifest",
        headers=_internal_headers(),
    )
    assert resp.status_code == 200
    items = resp.json()["entries"]
    assert all(it["sprite_url"] == "" for it in items)
    assert all(it["animation_url"] == "" for it in items)
    assert all(it["outfit_code"] == "none" for it in items)


# ── filter by species ─────────────────────────────────────────────────


async def test_list_filtered_by_species(client) -> None:
    await client.post(
        "/api/v1/internal/gamification/mascot-manifest/seed",
        headers=_internal_headers(),
    )
    resp = await client.get(
        "/api/v1/internal/gamification/mascot-manifest?species=cat",
        headers=_internal_headers(),
    )
    items = resp.json()["entries"]
    assert all(it["species"] == "cat" for it in items)
    assert len(items) == len(catalog.MASCOT_STAGES) * len(catalog.DEFAULT_MOODS)


# ── upsert ────────────────────────────────────────────────────────────


async def test_upsert_inserts_new_combo_with_urls(client) -> None:
    resp = await client.post(
        "/api/v1/internal/gamification/mascot-manifest/upsert",
        headers=_internal_headers(),
        json={
            "entries": [
                {
                    "species": "cat",
                    "stage": 1,
                    "mood": "neutral",
                    "outfit_code": "scarf",
                    "sprite_url": "https://cdn.example/cat-1-neutral-scarf.png",
                    "animation_url": "https://cdn.example/cat-1-neutral-scarf.json",
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"inserted": 1, "updated": 0, "total_in_request": 1}

    listed = await client.get(
        "/api/v1/internal/gamification/mascot-manifest?species=cat",
        headers=_internal_headers(),
    )
    items = listed.json()["entries"]
    scarf_entry = next(
        (it for it in items if it["outfit_code"] == "scarf"), None
    )
    assert scarf_entry is not None
    assert scarf_entry["sprite_url"].endswith("-scarf.png")


async def test_upsert_updates_urls_in_place(client) -> None:
    payload_v1 = {
        "entries": [
            {
                "species": "penguin",
                "stage": 2,
                "mood": "cheerful",
                "outfit_code": "none",
                "sprite_url": "https://cdn.example/v1.png",
                "animation_url": "",
            }
        ]
    }
    r1 = await client.post(
        "/api/v1/internal/gamification/mascot-manifest/upsert",
        headers=_internal_headers(),
        json=payload_v1,
    )
    assert r1.json()["inserted"] == 1

    payload_v2 = {
        "entries": [
            {
                "species": "penguin",
                "stage": 2,
                "mood": "cheerful",
                "outfit_code": "none",
                "sprite_url": "https://cdn.example/v2.png",
                "animation_url": "https://cdn.example/v2.json",
            }
        ]
    }
    r2 = await client.post(
        "/api/v1/internal/gamification/mascot-manifest/upsert",
        headers=_internal_headers(),
        json=payload_v2,
    )
    assert r2.json() == {"inserted": 0, "updated": 1, "total_in_request": 1}


async def test_upsert_idempotent_when_urls_unchanged(client) -> None:
    payload = {
        "entries": [
            {
                "species": "bear",
                "stage": 3,
                "mood": "sleepy",
                "outfit_code": "none",
                "sprite_url": "https://cdn/x.png",
                "animation_url": "",
            }
        ]
    }
    await client.post(
        "/api/v1/internal/gamification/mascot-manifest/upsert",
        headers=_internal_headers(),
        json=payload,
    )
    resp = await client.post(
        "/api/v1/internal/gamification/mascot-manifest/upsert",
        headers=_internal_headers(),
        json=payload,
    )
    body = resp.json()
    assert body["inserted"] == 0
    assert body["updated"] == 0


async def test_upsert_validates_stage_range(client) -> None:
    resp = await client.post(
        "/api/v1/internal/gamification/mascot-manifest/upsert",
        headers=_internal_headers(),
        json={
            "entries": [
                {
                    "species": "cat",
                    "stage": 99,  # out of [1, 5]
                    "mood": "neutral",
                    "outfit_code": "none",
                    "sprite_url": "",
                    "animation_url": "",
                }
            ]
        },
    )
    assert resp.status_code == 422


async def test_upsert_requires_internal_secret(client) -> None:
    resp = await client.post(
        "/api/v1/internal/gamification/mascot-manifest/upsert",
        json={"entries": []},
    )
    assert resp.status_code == 401


async def test_seed_then_upsert_preserves_seed_count(client) -> None:
    await client.post(
        "/api/v1/internal/gamification/mascot-manifest/seed",
        headers=_internal_headers(),
    )
    # Upsert one of the seeded rows with real URLs
    upsert = await client.post(
        "/api/v1/internal/gamification/mascot-manifest/upsert",
        headers=_internal_headers(),
        json={
            "entries": [
                {
                    "species": "cat",
                    "stage": 1,
                    "mood": "neutral",
                    "outfit_code": "none",
                    "sprite_url": "https://cdn/cat-1-neutral.png",
                    "animation_url": "",
                }
            ]
        },
    )
    assert upsert.json() == {"inserted": 0, "updated": 1, "total_in_request": 1}

    listed = await client.get(
        "/api/v1/internal/gamification/mascot-manifest",
        headers=_internal_headers(),
    )
    assert listed.json()["total"] == _expected_seed_count()


def test_catalog_constants_are_non_empty():
    assert len(catalog.MASCOT_SPECIES) >= 4
    assert catalog.MASCOT_STAGES == (1, 2, 3, 4, 5)
    assert "neutral" in catalog.DEFAULT_MOODS
