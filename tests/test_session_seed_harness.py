"""Characterizes the session-store seeding harness itself (build plan t1).

Every later session-lifecycle task's tests will build their "before state"
through :func:`tests.conftest.seed_session_records` /
:func:`tests.helpers.session_seed.seed_records`. If the harness itself is
wrong, every task built on it inherits the bug silently -- so this module
tests the harness in isolation, against the store it actually writes to,
independent of any CLI verb.
"""

from __future__ import annotations

import os

import pytest

from tests.helpers.session_seed import (
    assert_seeded_totals,
    count_active_with_dead_pids,
    count_by_status,
    seed_records,
    spawn_dead_pid,
)
from webglass.adapters.session_store import FileSessionStore
from webglass.sessions import SessionStatus

# --- spawn_dead_pid -----------------------------------------------------


def test_spawn_dead_pid_is_not_a_live_process() -> None:
    pid = spawn_dead_pid()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# --- seed_records: shape ------------------------------------------------


def test_seed_records_writes_the_requested_count(session_store: FileSessionStore) -> None:
    records = seed_records(session_store, count=5, status=SessionStatus.CLOSED)
    assert len(records) == 5
    assert len(session_store.list()) == 5


def test_seed_records_zero_count_writes_nothing(session_store: FileSessionStore) -> None:
    records = seed_records(session_store, count=0, status=SessionStatus.CLOSED)
    assert records == []
    assert session_store.list() == []


def test_seed_records_rejects_negative_count(session_store: FileSessionStore) -> None:
    with pytest.raises(ValueError, match="count must be >= 0"):
        seed_records(session_store, count=-1, status=SessionStatus.CLOSED)


def test_seed_records_applies_chosen_status(session_store: FileSessionStore) -> None:
    seed_records(session_store, count=3, status=SessionStatus.CLOSED)
    seed_records(session_store, count=2, status=SessionStatus.ACTIVE)
    records = session_store.list()
    assert count_by_status(records, SessionStatus.CLOSED) == 3
    assert count_by_status(records, SessionStatus.ACTIVE) == 2
    assert count_by_status(records, SessionStatus.EXPIRED) == 0


def test_seed_records_applies_chosen_owner(session_store: FileSessionStore) -> None:
    seed_records(session_store, count=2, status=SessionStatus.CLOSED, owner="alice")
    seed_records(session_store, count=1, status=SessionStatus.ACTIVE, owner="bob")
    owners = {record.owner for record in session_store.list()}
    assert owners == {"alice", "bob"}


def test_seed_records_defaults_caller_to_owner(session_store: FileSessionStore) -> None:
    (record,) = seed_records(session_store, count=1, status=SessionStatus.CLOSED, owner="alice")
    assert record.caller == "alice"


def test_seed_records_applies_explicit_caller(session_store: FileSessionStore) -> None:
    (record,) = seed_records(
        session_store, count=1, status=SessionStatus.CLOSED, owner="alice", caller="alice-cli"
    )
    assert record.caller == "alice-cli"


def test_seed_records_applies_chosen_expiry(session_store: FileSessionStore) -> None:
    (record,) = seed_records(
        session_store,
        count=1,
        status=SessionStatus.ACTIVE,
        now=1000.0,
        expires_at=500.0,
    )
    assert record.created_at == 1000.0
    assert record.expires_at == 500.0


def test_seed_records_defaults_expiry_five_minutes_after_now(
    session_store: FileSessionStore,
) -> None:
    (record,) = seed_records(session_store, count=1, status=SessionStatus.ACTIVE, now=1000.0)
    assert record.expires_at == 1300.0


def test_seed_records_applies_chosen_pid(session_store: FileSessionStore) -> None:
    dead_pid = spawn_dead_pid()
    (record,) = seed_records(session_store, count=1, status=SessionStatus.ACTIVE, pid=dead_pid)
    assert record.pid == dead_pid


def test_seed_records_two_calls_against_one_store_do_not_collide(
    session_store: FileSessionStore,
) -> None:
    """Two shapes seeded into the same store must not overwrite each other."""
    seed_records(session_store, count=3, status=SessionStatus.CLOSED, session_id_prefix="a")
    seed_records(session_store, count=3, status=SessionStatus.CLOSED, session_id_prefix="b")
    assert len(session_store.list()) == 6


def test_seed_records_round_trips_through_the_store(session_store: FileSessionStore) -> None:
    """A seeded record reads back identically through the store's own I/O path."""
    (written,) = seed_records(
        session_store,
        count=1,
        status=SessionStatus.CLOSED,
        owner="alice",
        task="research",
        backend_id="fake-backend",
    )
    read_back = session_store.get(written.session_id)
    assert read_back is not None
    assert read_back.owner == "alice"
    assert read_back.task == "research"
    assert read_back.backend_id == "fake-backend"
    assert read_back.status is SessionStatus.CLOSED


def test_seed_records_forward_compatible_kwargs_now_land_for_real(
    session_store: FileSessionStore,
) -> None:
    """Neither ``owner_token`` nor ``hosts`` is forward-compatible-only any more.

    Both fields have landed on :class:`FileSessionRecord` -- ``owner_token``
    with build plan t7, ``hosts`` with t8 -- so ``_set_if_declared`` now sets
    each of them for real and ``_to_payload`` carries them to disk. The full
    behavior of each lives with its own feature (``tests/test_owner_token.py``
    and ``tests/test_navigated_hosts.py``); what this test pins is the
    harness's half of the contract: the same call that was silently inert
    before the field existed started landing the value the moment it did,
    with no rewrite at the call site.
    """
    (record,) = seed_records(
        session_store,
        count=1,
        status=SessionStatus.CLOSED,
        owner_token="tok-123",
        hosts=("example.com", "example.org"),
    )
    read_back = session_store.get(record.session_id)
    assert read_back is not None
    assert read_back.owner_token == "tok-123"
    assert read_back.hosts == ("example.com", "example.org")


def test_seed_records_omitted_forward_compatible_kwargs_stay_omitted(
    session_store: FileSessionStore,
) -> None:
    """Omitting them writes the record shape a record would have anyway.

    For ``hosts`` that means ``None`` -- *unknown*, the pre-upgrade reading --
    rather than ``()``: a seeded record is written straight to disk, not
    created through :meth:`FileSessionStore.create`, so nothing has been
    tracking it and it must not claim otherwise. A test that wants a
    known-empty set passes ``hosts=()`` explicitly.
    """
    (record,) = seed_records(session_store, count=1, status=SessionStatus.CLOSED)
    assert record.owner_token == ""
    assert record.hosts is None
    read_back = session_store.get(record.session_id)
    assert read_back is not None
    assert read_back.hosts is None


# --- count_by_status / count_active_with_dead_pids -----------------------


def test_count_active_with_dead_pids_ignores_records_with_no_pid(
    session_store: FileSessionStore,
) -> None:
    seed_records(session_store, count=4, status=SessionStatus.ACTIVE)
    records = session_store.list()
    assert count_active_with_dead_pids(records) == 0


def test_count_active_with_dead_pids_ignores_closed_records_with_a_dead_pid(
    session_store: FileSessionStore,
) -> None:
    dead_pid = spawn_dead_pid()
    seed_records(session_store, count=3, status=SessionStatus.CLOSED, pid=dead_pid)
    records = session_store.list()
    assert count_active_with_dead_pids(records) == 0


def test_count_active_with_dead_pids_counts_active_records_with_a_dead_pid(
    session_store: FileSessionStore,
) -> None:
    dead_pid = spawn_dead_pid()
    seed_records(session_store, count=5, status=SessionStatus.ACTIVE, pid=dead_pid)
    records = session_store.list()
    assert count_active_with_dead_pids(records) == 5


def test_count_active_with_dead_pids_excludes_the_live_current_process(
    session_store: FileSessionStore,
) -> None:
    seed_records(session_store, count=2, status=SessionStatus.ACTIVE, pid=os.getpid())
    records = session_store.list()
    assert count_active_with_dead_pids(records) == 0


# --- the reporting-host baseline: 134 / 115 / 19 --------------------------


def test_reporting_host_baseline_is_reproducible_from_seeded_state(
    session_store: FileSessionStore,
) -> None:
    """The exact numbers named in this task's acceptance criteria."""
    dead_pid = spawn_dead_pid()
    seed_records(session_store, count=115, status=SessionStatus.CLOSED, session_id_prefix="c")
    seed_records(
        session_store,
        count=19,
        status=SessionStatus.ACTIVE,
        pid=dead_pid,
        session_id_prefix="a",
    )
    assert_seeded_totals(session_store, total=134, closed=115, active_with_dead_pids=19)


def test_assert_seeded_totals_fails_loudly_on_a_mismatch(session_store: FileSessionStore) -> None:
    seed_records(session_store, count=1, status=SessionStatus.CLOSED)
    with pytest.raises(AssertionError, match="expected 2 total records, found 1"):
        assert_seeded_totals(session_store, total=2, closed=1, active_with_dead_pids=0)


# --- the conftest fixtures themselves -------------------------------------


def test_session_store_fixture_is_isolated_per_test(session_store: FileSessionStore) -> None:
    assert session_store.list() == []


def test_seed_session_records_fixture_writes_into_the_session_store_fixture(
    session_store: FileSessionStore, seed_session_records
) -> None:
    seed_session_records(count=3, status=SessionStatus.CLOSED)
    assert len(session_store.list()) == 3


def test_seed_session_records_fixture_supports_multiple_shapes(
    session_store: FileSessionStore, seed_session_records
) -> None:
    dead_pid = spawn_dead_pid()
    seed_session_records(count=2, status=SessionStatus.CLOSED, session_id_prefix="closed")
    seed_session_records(
        count=3,
        status=SessionStatus.ACTIVE,
        pid=dead_pid,
        session_id_prefix="active",
    )
    assert_seeded_totals(session_store, total=5, closed=2, active_with_dead_pids=3)
