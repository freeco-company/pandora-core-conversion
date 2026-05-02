"""Calendar event_kind contract test — must mirror the publisher in
github.com/freeco-company/pandora-calendar:
  backend/app/Services/Gamification/CalendarEventCatalog.php

If a calendar.* event is added to the publisher but not registered here, the
event ingest endpoint will 422 and the gamification loop is silently broken.
"""

from __future__ import annotations

from app.gamification.catalog import EVENT_CATALOG, get_event_rule

# Must include every public const of CalendarEventCatalog::ALL.
EXPECTED_CALENDAR_EVENTS: set[str] = {
    "calendar.first_cycle",
    "calendar.cycle_logged",
    "calendar.symptom_logged",
    "calendar.dodo_checkin",
    "calendar.track_7_days",
    "calendar.streak_30_days",
    "calendar.cycle_streak_3_months",
    "calendar.pms_pattern_detected",
    "calendar.pregnancy_logged",
}


def test_all_calendar_publisher_events_exist_in_catalog() -> None:
    missing = EXPECTED_CALENDAR_EVENTS - set(EVENT_CATALOG.keys())
    assert not missing, (
        f"Missing in py-service catalog (publisher will 422): {sorted(missing)}"
    )


def test_first_cycle_is_lifetime_unique() -> None:
    rule = get_event_rule("calendar.first_cycle")
    assert rule.lifetime_unique is True
    assert rule.xp == 30


def test_pregnancy_logged_is_lifetime_unique_and_major() -> None:
    rule = get_event_rule("calendar.pregnancy_logged")
    assert rule.lifetime_unique is True
    assert rule.category == "major"


def test_dodo_checkin_has_daily_cap() -> None:
    rule = get_event_rule("calendar.dodo_checkin")
    assert rule.daily_cap_xp == 3


def test_cycle_streak_3_months_is_major() -> None:
    rule = get_event_rule("calendar.cycle_streak_3_months")
    assert rule.category == "major"
    assert rule.xp == 200


def test_calendar_events_share_source_app() -> None:
    for ev in EXPECTED_CALENDAR_EVENTS:
        assert get_event_rule(ev).source_app == "calendar"
