"""Unit tests for the XP / level curve (catalog §1)."""

from __future__ import annotations

import pytest

from app.gamification import catalog


def test_level_for_xp_anchors() -> None:
    # Anchors from catalog §1
    assert catalog.level_for_xp(0) == 1
    assert catalog.level_for_xp(50) == 2
    assert catalog.level_for_xp(120) == 3
    assert catalog.level_for_xp(1_000) == 10
    assert catalog.level_for_xp(2_000) == 15
    assert catalog.level_for_xp(3_500) == 20
    assert catalog.level_for_xp(12_000) == 50
    assert catalog.level_for_xp(30_000) == 100


def test_level_for_xp_just_below_threshold_stays_lower() -> None:
    assert catalog.level_for_xp(49) == 1
    assert catalog.level_for_xp(999) == 9
    assert catalog.level_for_xp(11_999) <= 49
    assert catalog.level_for_xp(29_999) <= 99


def test_level_for_xp_overshoot_caps_at_100() -> None:
    assert catalog.level_for_xp(99_999) == 100


def test_level_for_xp_negative_raises() -> None:
    with pytest.raises(ValueError):
        catalog.level_for_xp(-1)


def test_xp_to_next_level_decreases_with_xp() -> None:
    a = catalog.xp_to_next_level(0)
    b = catalog.xp_to_next_level(40)
    assert a > b > 0


def test_xp_to_next_level_zero_at_max() -> None:
    assert catalog.xp_to_next_level(30_000) == 0
    assert catalog.xp_to_next_level(50_000) == 0


def test_level_name_anchors() -> None:
    assert catalog.level_name(1) == ("種子期", "Seed")
    assert catalog.level_name(2) == ("萌芽期", "Sprout")
    assert catalog.level_name(10) == ("穩定期", "Steady")
    # Between anchors: takes the closest *lower or equal* anchor
    assert catalog.level_name(11) == ("穩定期", "Steady")
    assert catalog.level_name(15) == ("蛻變期", "Transform")
    assert catalog.level_name(99) == ("傳說期", "Legend")
    assert catalog.level_name(100) == ("永恆期", "Eternal")


def test_xp_for_level_matches_anchors() -> None:
    assert catalog.xp_for_level(1) == 0
    assert catalog.xp_for_level(10) == 1_000
    assert catalog.xp_for_level(20) == 3_500
    assert catalog.xp_for_level(100) == 30_000


def test_xp_for_level_out_of_range_raises() -> None:
    with pytest.raises(ValueError):
        catalog.xp_for_level(0)
    with pytest.raises(ValueError):
        catalog.xp_for_level(101)


def test_event_catalog_event_kind_prefix_matches_source_app() -> None:
    """Hygiene — every event_kind starts with `<source_app>.`."""
    for kind, rule in catalog.EVENT_CATALOG.items():
        assert kind.split(".", 1)[0] == rule.source_app, (
            f"event_kind {kind} prefix != source_app {rule.source_app}"
        )


def test_get_event_rule_unknown_raises() -> None:
    with pytest.raises(KeyError):
        catalog.get_event_rule("unknown.foo")
