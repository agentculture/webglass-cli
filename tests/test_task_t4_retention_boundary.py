"""Record retention boundary test for task t4 (3-day retention window).

Tests that :const:`~webglass.adapters.session_store.DEFAULT_RECORD_RETENTION_SECONDS`
enforces the 3-day (not 7-day) retention window: a closed record is purgeable when
its age equals or exceeds 3 days, not before.
"""

from __future__ import annotations

from tests.helpers.session_seed import DEFAULT_OWNER, DEFAULT_TASK
from webglass.adapters.session_store import FileSessionStore
from webglass.sessions import SessionStatus


def test_record_retention_is_3_days_not_before_boundary(
    session_store: FileSessionStore,
    seed_session_records,
) -> None:
    """A closed record is purgeable at exactly 3 days, not before.

    Acceptance criterion from task t4: "a record is purgeable at the 3-day
    boundary, not before."
    """
    # Use a large base timestamp so subtracting days from it doesn't go
    # negative.
    base_now = 10_000_000.0
    day_seconds = 24 * 60 * 60

    # Seed a closed record just before the 3-day boundary (2.5 days old).
    # The seed_records function sets both last_used_at and created_at to the
    # `now` parameter; _purgeable measures age as now - max(last_used_at,
    # expires_at). By seeding with an old timestamp and then calling clean()
    # at base_now, we make the record appear old.
    seed_session_records(
        count=1,
        status=SessionStatus.CLOSED,
        owner=DEFAULT_OWNER,
        task=DEFAULT_TASK,
        now=base_now - (2.5 * day_seconds),
        expires_at=base_now - (2.5 * day_seconds),  # make sure expires_at matches
        session_id_prefix="within-window",
    )

    # Seed a closed record well past the 3-day boundary (3.5 days old).
    # This record's age (3.5 days) exceeds the 3-day retention, so it MUST
    # be purged when clean() is called at base_now.
    seed_session_records(
        count=1,
        status=SessionStatus.CLOSED,
        owner=DEFAULT_OWNER,
        task=DEFAULT_TASK,
        now=base_now - (3.5 * day_seconds),
        expires_at=base_now - (3.5 * day_seconds),
        session_id_prefix="past-window",
    )

    # Also test exactly at the boundary: a record whose age is exactly
    # 3 days should be purgeable (age == retention).
    seed_session_records(
        count=1,
        status=SessionStatus.CLOSED,
        owner=DEFAULT_OWNER,
        task=DEFAULT_TASK,
        now=base_now - (3 * day_seconds),
        expires_at=base_now - (3 * day_seconds),
        session_id_prefix="at-boundary",
    )

    # Run clean at base_now. The record from 3.5 days ago and the record
    # from exactly 3 days ago should be purged; the 2.5-day-old record
    # should survive.
    session_store.clean(now=base_now)

    remaining = {r.session_id for r in session_store.list()}

    # The only record that should remain is the one within the window.
    assert remaining == {
        "within-window-closed-0"
    }, f"Expected only the 2.5-day-old record to survive, but found: {remaining}"

    # Double-check that the boundary and past-boundary records are gone.
    assert "at-boundary-closed-0" not in remaining
    assert "past-window-closed-0" not in remaining
