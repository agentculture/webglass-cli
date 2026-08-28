"""``to_public_dict()`` reports observed liveness, never a stored status (task t3).

Build plan task t1 characterized the reporting-host baseline: 19 records sit
on disk as ``status=active, browser_reaped=false, browser_was_running=null``
with pids that expired weeks ago. ``session list``/``show`` render
``to_public_dict()`` verbatim (``webglass/adapters/session_store.py``) and,
before this task, never probed the pid at all — so a crashed caller's
session read as live indefinitely (spec claim c7). The fix adds an
``observed_liveness`` field computed fresh from a ``kill(pid, 0)`` probe on
every call, and is deliberately *not* a stored field: nothing here ever
writes ``status`` (or anything else) back to the record or the file on disk
(spec honesty h4).

Three outcomes are distinguished:

* a genuinely dead pid renders ``"dead"``;
* a pid nobody ever recorded (no browser attached) renders ``"unknown"``;
* a pid that still answers ``kill(pid, 0)`` but belongs to another user
  (``PermissionError`` — near-certain pid reuse after our browser already
  exited) also renders ``"unknown"``, never ``"running"``: it must not be
  claimed as our browser just because *something* is alive at that number.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import SeedSessionRecords
from tests.helpers.session_seed import spawn_dead_pid
from webglass.adapters.session_store import FileSessionStore
from webglass.sessions import SessionStatus


def test_active_record_with_dead_pid_renders_not_live(
    session_store: FileSessionStore, seed_session_records: SeedSessionRecords
) -> None:
    dead_pid = spawn_dead_pid()
    seed_session_records(count=1, status=SessionStatus.ACTIVE, pid=dead_pid)

    [record] = session_store.list()
    assert record.status is SessionStatus.ACTIVE  # unchanged: still on disk as active

    public = record.to_public_dict()
    assert public["observed_liveness"] == "dead"
    # The read must not have mutated the stored record.
    assert public["status"] == "active"


def test_reading_the_record_never_mutates_status_on_disk(
    session_store: FileSessionStore, seed_session_records: SeedSessionRecords
) -> None:
    dead_pid = spawn_dead_pid()
    seed_session_records(count=1, status=SessionStatus.ACTIVE, pid=dead_pid)

    # Render it twice, via list() and via get(), the way `session list`/`show`
    # do -- neither is allowed to write anything back.
    for record in session_store.list():
        record.to_public_dict()
    reread = session_store.get(session_store.list()[0].session_id)
    assert reread is not None
    assert reread.status is SessionStatus.ACTIVE
    assert reread.to_public_dict()["observed_liveness"] == "dead"


def test_record_with_no_pid_reports_unknown_not_dead(
    session_store: FileSessionStore, seed_session_records: SeedSessionRecords
) -> None:
    seed_session_records(count=1, status=SessionStatus.ACTIVE, pid=None)

    [record] = session_store.list()
    assert record.to_public_dict()["observed_liveness"] == "unknown"


def test_running_pid_reports_running(
    session_store: FileSessionStore, seed_session_records: SeedSessionRecords
) -> None:
    seed_session_records(count=1, status=SessionStatus.ACTIVE, pid=os.getpid())

    [record] = session_store.list()
    assert record.to_public_dict()["observed_liveness"] == "running"


def test_pid_alive_but_owned_by_another_user_is_not_claimed_as_ours(
    session_store: FileSessionStore,
    seed_session_records: SeedSessionRecords,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pid that answers ``kill(pid, 0)`` with ``PermissionError`` is alive,
    but the OS is telling us it isn't ours -- almost certainly pid reuse
    after our own browser exited. This must never render as ``"running"``.
    """
    foreign_pid = 999_999  # value is irrelevant; os.kill is monkeypatched below
    seed_session_records(count=1, status=SessionStatus.ACTIVE, pid=foreign_pid)

    real_kill = os.kill

    def _fake_kill(pid: int, sig: int) -> None:
        if pid == foreign_pid:
            raise PermissionError("owned by another user")
        real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", _fake_kill)

    [record] = session_store.list()
    liveness = record.to_public_dict()["observed_liveness"]
    assert liveness != "running"
    assert liveness == "unknown"
