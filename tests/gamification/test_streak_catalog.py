"""Daily-login streak event_kind contract test.

Mirrors the publisher in pandora-meal / pandora-calendar / pandora.js-store
(`App\\Services\\Gamification\\StreakPublisher`). These event_kinds are emitted
when a user's daily-login streak is extended or hits a milestone
(1 / 3 / 7 / 14 / 21 / 30 / 60 / 100). Renames here break idempotency_key
history and silently re-award XP for prior milestones — don't.
"""

from __future__ import annotations

import pytest

from app.gamification.catalog import EVENT_CATALOG, get_event_rule

EXPECTED_STREAK_EVENTS = {
    "meal.daily_login_streak_extended",
    "meal.streak_milestone_unlocked",
    "calendar.daily_login_streak_extended",
    "calendar.streak_milestone_unlocked",
    "jerosse.daily_login_streak_extended",
    "jerosse.streak_milestone_unlocked",
}


def test_all_streak_events_present_in_catalog() -> None:
    missing = EXPECTED_STREAK_EVENTS - set(EVENT_CATALOG.keys())
    assert not missing, f"missing event_kinds in catalog: {missing}"


@pytest.mark.parametrize("event_kind", sorted(EXPECTED_STREAK_EVENTS))
def test_streak_event_resolvable(event_kind: str) -> None:
    """get_event_rule must not raise — i.e. /internal/gamification/events
    will not 422 on these payloads."""
    rule = get_event_rule(event_kind)
    assert rule.xp > 0
    assert rule.source_app in {"meal", "calendar", "jerosse"}


@pytest.mark.parametrize(
    "event_kind",
    [
        "meal.daily_login_streak_extended",
        "calendar.daily_login_streak_extended",
        "jerosse.daily_login_streak_extended",
    ],
)
def test_extension_events_have_daily_cap(event_kind: str) -> None:
    """Daily extension events fire on every login — must be capped so a user
    re-opening the App many times in a day can't farm XP."""
    rule = get_event_rule(event_kind)
    assert rule.daily_cap_xp is not None
    assert rule.category == "micro"


@pytest.mark.parametrize(
    "event_kind",
    [
        "meal.streak_milestone_unlocked",
        "calendar.streak_milestone_unlocked",
        "jerosse.streak_milestone_unlocked",
    ],
)
def test_milestone_events_are_milestone_tier(event_kind: str) -> None:
    rule = get_event_rule(event_kind)
    assert rule.category == "milestone"
