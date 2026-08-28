"""The sweep is gated on liveness, never on ownership (build plan task t6).

``expires_at`` is written once at ``create()`` and — before this task — was
never extended again, while the CLI's throwaway session TTL is 300 seconds:
shorter than a slow page load. Any operation outstripping its TTL therefore
left a record that was ``active`` *past* its ``expires_at`` while its browser
was genuinely in use, and :meth:`FileSessionStore.clean` reaped exactly that
case — terminating the pid and removing the profile directory out from under
a running operation. Manual-only cleanup kept the race rare; task t10 makes
the sweep run on every session-creating invocation, which would turn it into
routine traffic. This module pins the fix:

1. **A held, unexpired lease is the liveness signal**, and ``clean()`` skips
   a record that has one no matter what its ``expires_at`` says (c28/h24).
2. **Using a session extends its life.** ``acquire_lease`` slides the
   record's expiry forward by its original lifetime, so a session in active
   use is not sitting one sweep away from being reaped.
3. **A long operation past its TTL survives a concurrent sweep** — tested
   with two store instances against one directory in two threads, the
   realistic shape (the store is deliberately stateless, so two *processes*
   see the same files the same way).
4. **Ownership does not gate the sweep** (c38, and deliberate): an expired
   record means its owner is finished or crashed, so it is reaped whoever
   owns it — that is what ``session clean`` already does store-wide, warning
   that it reaped other callers' sessions. What the sweep must never do is
   signal a pid that is not one of its own records' — the 187 "leaked
   chromium processes" in issue #14 turned out to be the reporter's desktop
   browser, and a sweep that went hunting for chromium by name would have
   killed them.
"""

from __future__ import annotations

import subprocess  # nosec B404 - fixed argv, no shell; a stand-in browser to kill by pid
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.helpers.session_seed import DEFAULT_TASK, seed_records, spawn_dead_pid
from webglass.adapters import session_store as store_module
from webglass.adapters.session_store import (
    FileSessionRecord,
    FileSessionStore,
    LaunchedBrowser,
)
from webglass.sessions import LeaseGrant, SessionStatus

CALLER = "cli"
HOLDER = "cli:cli"


def _sleeper() -> subprocess.Popen[bytes]:
    """A stand-in browser process: something real that a sweep could kill."""
    return subprocess.Popen(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c", "import time; time.sleep(120)"]
    )


def _fake_launcher(pid: int) -> store_module.SessionLauncher:
    def launch(session_id: str, user_data_dir: Path) -> LaunchedBrowser:
        return LaunchedBrowser(
            endpoint="ws://127.0.0.1:1/planted", pid=pid, user_data_dir=str(user_data_dir)
        )

    return launch


def _create(
    store: FileSessionStore, session_id: str = "sess-1", *, now: float, ttl: float
) -> FileSessionRecord:
    return store.create(
        session_id=session_id,
        owner=CALLER,
        caller=CALLER,
        task=DEFAULT_TASK,
        backend_id="test-backend",
        now=now,
        expires_at=now + ttl,
    )


# ---------------------------------------------------------------------------
# 1. A held, unexpired lease is never reaped
# ---------------------------------------------------------------------------


def test_clean_skips_a_record_whose_lease_is_held_and_unexpired(tmp_path: Path) -> None:
    """The core fix: past ``expires_at`` is not proof the browser is idle."""
    sleeper = _sleeper()
    try:
        store = FileSessionStore(tmp_path / "sessions", launcher=_fake_launcher(sleeper.pid))
        _create(store, now=1000.0, ttl=10.0)
        # The operation takes the lease at 1005 and is still running at 1020 —
        # past the record's original 1010 expiry, well inside the lease.
        grant = store.acquire_lease("sess-1", HOLDER, now=1005.0, ttl_seconds=60.0)
        assert isinstance(grant, LeaseGrant)

        assert store.clean(now=1020.0) == []

        record = store.get("sess-1")
        assert record is not None
        assert record.status is SessionStatus.ACTIVE
        assert record.lease is not None and record.lease.holder == HOLDER
        assert record.browser_reaped is False
        assert sleeper.poll() is None, "the sweep killed a browser that was in use"
        assert (tmp_path / "sessions" / "profiles" / "sess-1").is_dir()
    finally:
        sleeper.kill()
        sleeper.wait(timeout=30)


def test_clean_still_reaps_the_same_record_once_its_lease_lapses(tmp_path: Path) -> None:
    """The gate is *liveness*, not immunity: the lease running out re-arms it."""
    dead_pid = spawn_dead_pid()
    store = FileSessionStore(tmp_path / "sessions", launcher=_fake_launcher(dead_pid))
    _create(store, now=1000.0, ttl=10.0)
    store.acquire_lease("sess-1", HOLDER, now=1005.0, ttl_seconds=60.0)

    assert store.clean(now=1020.0) == []  # leased: skipped

    reaped = store.clean(now=2000.0)  # lease and the slid expiry both lapsed
    assert [record.session_id for record in reaped] == ["sess-1"]
    assert reaped[0].status is SessionStatus.EXPIRED
    assert reaped[0].lease is None


# ---------------------------------------------------------------------------
# 2. Using a session extends its life
# ---------------------------------------------------------------------------


def test_acquire_lease_extends_the_record_expiry_by_its_original_lifetime(
    tmp_path: Path,
) -> None:
    store = FileSessionStore(tmp_path / "sessions")
    _create(store, now=1000.0, ttl=300.0)

    store.acquire_lease("sess-1", HOLDER, now=1200.0, ttl_seconds=30.0)

    record = store.get("sess-1")
    assert record is not None
    # A fresh 300s from the moment of use, not 100s of leftover from create().
    assert record.expires_at == pytest.approx(1500.0)
    assert record.last_used_at == pytest.approx(1200.0)
    # The invariant that makes the slide idempotent: expires_at - last_used_at
    # is always the original TTL, however many times the session is used.
    assert record.expires_at - record.last_used_at == pytest.approx(300.0)


def test_repeated_use_slides_the_expiry_without_compounding_it(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path / "sessions")
    _create(store, now=1000.0, ttl=300.0)

    for moment in (1100.0, 1200.0, 1300.0):
        store.acquire_lease("sess-1", HOLDER, now=moment, ttl_seconds=30.0)

    record = store.get("sess-1")
    assert record is not None
    assert record.expires_at == pytest.approx(1600.0)


def test_lease_acquisition_never_shortens_an_expiry(tmp_path: Path) -> None:
    """A record explicitly given a long life keeps it: the slide only extends."""
    store = FileSessionStore(tmp_path / "sessions")
    _create(store, now=1000.0, ttl=10_000.0)

    store.acquire_lease("sess-1", HOLDER, now=10_500.0, ttl_seconds=30.0)

    record = store.get("sess-1")
    assert record is not None
    assert record.expires_at >= 11_000.0


# ---------------------------------------------------------------------------
# 3. The concurrent case this whole task exists for
# ---------------------------------------------------------------------------


def test_a_long_operation_past_its_ttl_survives_a_concurrent_sweep(tmp_path: Path) -> None:
    """Two stores, one directory, two threads — the shape of two CLI processes.

    The "operation" holds a lease and runs for far longer than the session's
    (deliberately tiny) TTL while a second store instance sweeps in a loop,
    exactly as task t10's opportunistic sweep will on every session-creating
    invocation. The browser must still be alive at the end, and still
    reachable through its record.
    """
    sleeper = _sleeper()
    stop = threading.Event()
    sweeps = 0
    try:
        sessions_dir = tmp_path / "sessions"
        operating = FileSessionStore(sessions_dir, launcher=_fake_launcher(sleeper.pid))
        sweeping = FileSessionStore(sessions_dir)  # a different invocation's store

        started = time.time()
        _create(operating, now=started, ttl=0.2)
        grant = operating.acquire_lease("sess-1", HOLDER, now=time.time(), ttl_seconds=30.0)
        assert isinstance(grant, LeaseGrant)

        def sweep() -> None:
            nonlocal sweeps
            while not stop.is_set():
                sweeping.clean(time.time())
                sweeps += 1
                time.sleep(0.01)

        sweeper = threading.Thread(target=sweep, daemon=True)
        sweeper.start()
        try:
            time.sleep(0.5)  # the "long operation": well past the 0.2s TTL
        finally:
            stop.set()
            sweeper.join(timeout=10)

        assert sweeps > 1, "the sweeper never ran; the test proves nothing"
        assert time.time() - started > 0.2, "the operation did not outlive the TTL"

        record = operating.get("sess-1")
        assert record is not None
        assert record.status is SessionStatus.ACTIVE
        assert record.endpoint_ref, "the sweep dropped the endpoint of a live session"
        assert sleeper.poll() is None, "a concurrent sweep killed a live browser"
        assert (sessions_dir / "profiles" / "sess-1").is_dir()
    finally:
        stop.set()
        sleeper.kill()
        sleeper.wait(timeout=30)


# ---------------------------------------------------------------------------
# 4. Ownership does not gate the sweep; the record's own pid does
# ---------------------------------------------------------------------------


def test_clean_reaps_an_expired_record_belonging_to_another_owner(
    session_store: FileSessionStore,
) -> None:
    """c38, deliberately: expired means its owner is finished or crashed."""
    seed_records(
        session_store,
        count=1,
        status=SessionStatus.ACTIVE,
        owner="somebody-else",
        caller="somebody-else",
        now=1000.0,
        expires_at=1010.0,
        pid=spawn_dead_pid(),
    )

    reaped = session_store.clean(now=2000.0)

    assert [record.owner for record in reaped] == ["somebody-else"]
    assert reaped[0].status is SessionStatus.EXPIRED


def test_the_sweep_only_ever_signals_pids_it_has_a_record_for(
    session_store: FileSessionStore,
) -> None:
    """Issue #14's 187 "leaked" chromium processes were the reporter's own
    desktop browser. A sweep that terminated anything it did not have a record
    for would have killed them, so this pins the boundary."""
    bystander = _sleeper()
    signalled: list[int] = []

    def spy(pid: int) -> bool:
        signalled.append(pid)
        return False

    try:
        store = FileSessionStore(session_store.directory, terminator=spy)
        recorded = [spawn_dead_pid(), spawn_dead_pid()]
        for index, pid in enumerate(recorded):
            seed_records(
                store,
                count=1,
                status=SessionStatus.ACTIVE,
                now=1000.0,
                expires_at=1010.0,
                pid=pid,
                session_id_prefix=f"reapable-{index}",
            )
        seed_records(
            store,
            count=1,
            status=SessionStatus.ACTIVE,
            now=1000.0,
            expires_at=1010.0,
            pid=None,
            session_id_prefix="pidless",
        )

        store.clean(now=2000.0)

        assert sorted(signalled) == sorted(recorded)
        assert bystander.pid not in signalled
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait(timeout=30)


def test_the_sweep_does_not_signal_a_pid_that_now_belongs_to_someone_else(
    session_store: FileSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``"foreign"`` pid is pid reuse after our browser exited, not our
    browser under another uid — signalling it would be reaching outside
    WebGlass's own processes. The record is still expired and its profile
    still removed; only the kill is withheld, and the record says so by
    reporting no reap attempt."""
    signalled: list[int] = []

    def spy(pid: int) -> bool:  # pragma: no cover - asserted not to be called
        signalled.append(pid)
        return False

    monkeypatch.setattr(store_module, "_pid_liveness", lambda pid: "foreign")
    store = FileSessionStore(session_store.directory, terminator=spy)
    seed_records(
        store,
        count=1,
        status=SessionStatus.ACTIVE,
        now=1000.0,
        expires_at=1010.0,
        pid=424242,
    )

    reaped = store.clean(now=2000.0)

    assert len(reaped) == 1
    assert signalled == []
    assert reaped[0].status is SessionStatus.EXPIRED
    assert reaped[0].browser_reaped is False
    assert reaped[0].browser_was_running is None
