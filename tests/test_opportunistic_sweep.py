"""The time-bounded opportunistic sweep (build plan task t10, spec claim c14).

This is the root fix for issue #14. Before it, ``FileSessionStore.clean`` had
exactly one caller in the whole package — ``_op_session_clean``, the explicit
``webglass session clean`` verb — so a host only ever got a tidy session store
if a human remembered to ask for one. Nobody did, and the reporting host
accumulated 134 records and 206 MB of orphaned profile directories while a
perfectly working cleanup routine sat there unused.

The fix is deliberately narrow, and every one of those narrowings is pinned
here:

* it runs on **session-creating invocations only** — a read verb must never
  mutate the store its caller is trying to observe;
* it is bounded by **time**, not by a record count (spec v3), and stops the
  moment the budget is spent;
* it runs **outside the operation result path** and swallows its own errors,
  so a sweep failure can never turn a successful observation into a failure;
* it never sweeps a session **out from under the flow that is about to use
  it**;
* what it reaped stays **reachable**, because an unrequested destructive
  local-state effect that reports nothing is the observability gap issue #14
  was itself filed out of (spec s16).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.conftest import SeedSessionRecords
from webglass.adapters import FakeArtifactStore, FakeBrowserBackend, FixedClock, SequentialIds
from webglass.adapters.browser import FakeBrowserRoute
from webglass.adapters.session_store import (
    FileSessionRecord,
    FileSessionStore,
    LaunchedBrowser,
    default_sessions_dir,
)
from webglass.cli import _factory, main
from webglass.effects import OperationKind
from webglass.service import WebGlassService
from webglass.sessions import SessionStatus

FIXED_TIME = 1_700_000_000.0
FLOW_TOKEN = "sweep-flow-token"
HOST = "app.test"
PAGE_URL = f"http://{HOST}/one"

#: Comfortably past ``DEFAULT_RECORD_RETENTION_SECONDS`` (3 days), so a
#: non-active record seeded this far back is genuinely purgeable rather than
#: merely old.
LONG_AGO = FIXED_TIME - 30 * 24 * 60 * 60.0


def _store(tmp_path: Path) -> FileSessionStore:
    """A file store whose "launcher" starts nothing and reports no pid.

    Same shape as ``tests/test_session_reuse.py``'s: the sweep's browser
    reaping is covered against real pids by the liveness tests, and what
    these tests are about is *whether and when* the sweep runs at all.
    """

    def launch(session_id: str, user_data_dir: Path) -> LaunchedBrowser:
        return LaunchedBrowser(
            pid=None,
            endpoint=f"http://127.0.0.1:0/{session_id}",
            user_data_dir=str(user_data_dir),
            sandboxed=True,
        )

    return FileSessionStore(tmp_path / "sessions", launcher=launch)


def _service(store: FileSessionStore) -> WebGlassService:
    return WebGlassService(
        clock=FixedClock(FIXED_TIME),
        ids=SequentialIds(),
        browser=FakeBrowserBackend({PAGE_URL: FakeBrowserRoute(html="<html><p>hi</p></html>")}),
        sessions=store,
        artifacts=FakeArtifactStore(),
    )


def _seed(
    store: FileSessionStore,
    session_id: str,
    *,
    status: SessionStatus,
    expires_at: float,
    last_used_at: float = FIXED_TIME,
    owner_token: str = "someone-else",
    hosts: tuple[str, ...] | None = (HOST,),
) -> FileSessionRecord:
    record = FileSessionRecord(
        session_id=session_id,
        generation=0,
        owner="cli",
        caller="cli",
        task="cli",
        backend_id="fake",
        created_at=last_used_at,
        last_used_at=last_used_at,
        expires_at=expires_at,
        capability_profile_ref="built-in-default",
        status=status,
        lease=None,
        endpoint_ref=f"http://127.0.0.1:0/{session_id}",
        diagnostics=(),
        pid=None,
        owner_token=owner_token,
        hosts=hosts,
    )
    store._write_unlocked(record)  # noqa: SLF001 - the seeding path, as in tests/helpers
    return record


def _statuses(store: FileSessionStore) -> dict[str, SessionStatus]:
    return {record.session_id: record.status for record in store.list()}


# ---------------------------------------------------------------------------
# Criterion 4: an ordinary invocation shrinks a store of expired records
# ---------------------------------------------------------------------------


def test_an_ordinary_invocation_sweeps_the_store_with_no_session_clean_call(
    tmp_path: Path,
) -> None:
    """The whole point of the task: nobody had to ask for this.

    Two populations, matching the reporting host's: records long since
    closed (purged outright, so the record count actually falls) and records
    still marked ``active`` although they expired weeks ago (expired, their
    browser reaped). Nothing here calls ``session clean``.
    """
    store = _store(tmp_path)
    service = _service(store)
    for index in range(4):
        _seed(
            store,
            f"long-closed-{index}",
            status=SessionStatus.CLOSED,
            expires_at=LONG_AGO,
            last_used_at=LONG_AGO,
        )
    for index in range(3):
        _seed(
            store,
            f"stale-active-{index}",
            status=SessionStatus.ACTIVE,
            expires_at=FIXED_TIME - 60.0,
            last_used_at=FIXED_TIME - 60.0,
        )
    assert len(store.list()) == 7

    with _factory.ephemeral_session(service, None, hosts=(HOST,)) as provisioned:
        assert provisioned.session_id is not None

    remaining = _statuses(store)
    # The four purgeable records are gone: the store shrank.
    assert not [name for name in remaining if name.startswith("long-closed-")]
    # The three orphans are no longer claiming to be active.
    assert all(
        remaining[f"stale-active-{index}"] is SessionStatus.EXPIRED for index in range(3)
    ), remaining


def test_session_create_sweeps_the_store_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through the real CLI, with no browser and no ``session clean``.

    ``webglass session create`` does not go through
    :func:`_factory.ephemeral_session` — it is an operation like any other —
    but it is the most literally session-creating invocation there is, so the
    spec names it alongside ``ephemeral_session`` as a sweep site.
    """
    del tmp_path  # the autouse fixture already pins $WEBGLASS_STATE_DIR
    store = FileSessionStore(default_sessions_dir())
    now = time.time()
    _seed(
        store,
        "long-closed",
        status=SessionStatus.CLOSED,
        expires_at=now - 30 * 24 * 60 * 60.0,
        last_used_at=now - 30 * 24 * 60 * 60.0,
    )
    assert len(store.list()) == 1

    assert main(["session", "create", "--json"]) == 0
    capsys.readouterr()

    assert "long-closed" not in _statuses(store)


# ---------------------------------------------------------------------------
# Criterion 1: read verbs never sweep
# ---------------------------------------------------------------------------


def test_a_read_verb_leaves_the_record_count_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``session list`` must show the store, not silently rewrite it.

    Sweeping inside a read verb would mean the answer a caller gets back
    describes a store the act of asking just changed — and for a caller
    investigating a suspected leak (exactly issue #14's reporter) that is the
    worst possible moment to delete the evidence.
    """
    store = FileSessionStore(default_sessions_dir())
    now = time.time()
    for index in range(3):
        _seed(
            store,
            f"long-closed-{index}",
            status=SessionStatus.CLOSED,
            expires_at=now - 30 * 24 * 60 * 60.0,
            last_used_at=now - 30 * 24 * 60 * 60.0,
        )
    _seed(
        store,
        "stale-active",
        status=SessionStatus.ACTIVE,
        expires_at=now - 60.0,
        last_used_at=now - 60.0,
    )
    before = _statuses(store)

    for argv in (["session", "list", "--json"], ["session", "show", "stale-active", "--json"]):
        assert main(argv) == 0
        capsys.readouterr()

    assert _statuses(store) == before


# ---------------------------------------------------------------------------
# Criterion 2: bounded by time
# ---------------------------------------------------------------------------


def test_a_spent_budget_stops_the_sweep_before_it_reaps_anything(tmp_path: Path) -> None:
    """A zero budget is the degenerate case of the bound, and it must hold.

    Checked between records, so a budget already spent on arrival stops the
    pass at the first one — nothing is reaped, and crucially nothing raises:
    an invocation whose sweep did no work is a completely ordinary one.
    """
    store = _store(tmp_path)
    service = _service(store)
    _seed(
        store,
        "stale-active",
        status=SessionStatus.ACTIVE,
        expires_at=FIXED_TIME - 60.0,
        last_used_at=FIXED_TIME - 60.0,
    )

    swept = _factory.sweep_session_store(service, budget_seconds=0.0)

    assert swept == ()
    assert _statuses(store) == {"stale-active": SessionStatus.ACTIVE}


def test_the_sweep_stays_under_50ms_against_a_store_of_300_records(
    session_store: FileSessionStore,
    seed_session_records: SeedSessionRecords,
) -> None:
    """The spec's measurable target (c37): under 50ms added to ``page open``.

    Seeded as *recent closed* records: readable, lock-taking, and not
    reapable, which is the honest steady-state shape — the pass pays the
    per-record read cost for all 300 without the rmtree/SIGTERM cost skewing
    the measurement. The budget is checked between records, so the bound is
    the budget plus at most one record's work.
    """
    seed_session_records(
        count=300,
        status=SessionStatus.CLOSED,
        now=time.time(),
    )
    service = _service(session_store)

    started = time.monotonic()
    _factory.sweep_session_store(service)
    elapsed = time.monotonic() - started

    assert elapsed < 0.05, f"sweep took {elapsed * 1000:.1f}ms over 300 records"


# ---------------------------------------------------------------------------
# Criterion 3: outside the result path, errors swallowed
# ---------------------------------------------------------------------------


def test_a_sweep_that_blows_up_never_fails_the_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Housekeeping the caller did not ask for cannot cost them their answer."""
    store = _store(tmp_path)
    service = _service(store)

    def explode(*args: object, **kwargs: object) -> list[FileSessionRecord]:
        raise OSError("the session directory went away mid-sweep")

    monkeypatch.setattr(store, "clean", explode)

    with _factory.ephemeral_session(service, None, hosts=(HOST,)) as provisioned:
        assert provisioned.session_id is not None
        assert provisioned.ephemeral is True
        assert provisioned.swept == ()


# ---------------------------------------------------------------------------
# Ordering: the flow's own session survives its own sweep
# ---------------------------------------------------------------------------


def test_a_reused_session_is_never_swept_out_from_under_the_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse resolves first; the sweep runs against what is left.

    The flow's session is live and lease-held by the time the sweep starts,
    and a held lease is proof of life the sweep respects (t6). A stale
    sibling in the same store is reaped in the same pass, so this asserts
    effectiveness and safety together rather than just the absence of harm.
    """
    store = _store(tmp_path)
    service = _service(store)
    _seed(
        store,
        "keeper",
        status=SessionStatus.ACTIVE,
        expires_at=FIXED_TIME + 300.0,
        owner_token=FLOW_TOKEN,
    )
    _seed(
        store,
        "stale-active",
        status=SessionStatus.ACTIVE,
        expires_at=FIXED_TIME - 60.0,
        last_used_at=FIXED_TIME - 60.0,
        owner_token=FLOW_TOKEN,
    )
    monkeypatch.setenv(_factory.SESSION_OWNER_ENV, FLOW_TOKEN)

    with _factory.ephemeral_session(service, None, hosts=(HOST,)) as provisioned:
        assert provisioned.session_id == "keeper"
        assert provisioned.reused is True

    statuses = _statuses(store)
    assert statuses["keeper"] is SessionStatus.ACTIVE
    assert statuses["stale-active"] is SessionStatus.EXPIRED


# ---------------------------------------------------------------------------
# What it reaped stays reachable (the surface task t11 builds on)
# ---------------------------------------------------------------------------


def test_the_records_the_sweep_reaped_are_reachable_afterwards(tmp_path: Path) -> None:
    """An irreversible effect nobody can name afterwards is the s16 gap.

    t10 does not render this anywhere — that is t11's job — but it must not
    make rendering it impossible, so the provisioner carries the reaped
    records out with the session it provisioned.
    """
    store = _store(tmp_path)
    service = _service(store)
    _seed(
        store,
        "stale-active",
        status=SessionStatus.ACTIVE,
        expires_at=FIXED_TIME - 60.0,
        last_used_at=FIXED_TIME - 60.0,
    )

    with _factory.ephemeral_session(service, None, hosts=(HOST,)) as provisioned:
        swept = provisioned.swept

    assert [record.session_id for record in swept] == ["stale-active"]
    assert all(record.status is SessionStatus.EXPIRED for record in swept)


# ---------------------------------------------------------------------------
# Task t11: the sweep gets an observability surface
# ---------------------------------------------------------------------------


def test_a_sweep_that_reaped_something_says_so_in_the_json_envelope(
    tmp_path: Path,
) -> None:
    """Acceptance criterion 1, exercised at the service layer.

    ``swept`` rides into ``execute`` as a side channel (build plan t11) and
    must land, unconditionally, on the result — not only when a handler
    happens to look at it.
    """
    store = _store(tmp_path)
    service = _service(store)
    _seed(
        store,
        "stale-active",
        status=SessionStatus.ACTIVE,
        expires_at=FIXED_TIME - 60.0,
        last_used_at=FIXED_TIME - 60.0,
    )

    with _factory.ephemeral_session(service, None, hosts=(HOST,)) as provisioned:
        operation = _factory.build_operation(
            service,
            _factory.build_context(),
            OperationKind.SESSION_LIST,
            session_id=provisioned.session_id,
        )
        result = service.execute(operation, _factory.build_context(), swept=provisioned.swept)

    payload = result.to_dict()
    # The record the sweep reaped, rendered exactly as the store observed it
    # *after* reaping — status EXPIRED, not the ACTIVE it was seeded with.
    assert [entry["session_id"] for entry in payload["swept_sessions"]] == ["stale-active"]
    assert payload["swept_sessions"][0]["status"] == "expired"
    assert "sessions-swept" in payload["known_effects"]


def test_a_sweep_that_reaped_nothing_still_reports_an_empty_list(tmp_path: Path) -> None:
    """Criterion 2's flip side: absence must read as 'nothing swept', not
    'this build does not report sweeps' (the same ambiguity ``session_ephemeral``
    and ``session_reused`` were written to avoid).
    """
    store = _store(tmp_path)
    service = _service(store)

    with _factory.ephemeral_session(service, None, hosts=(HOST,)) as provisioned:
        assert provisioned.swept == ()
        operation = _factory.build_operation(
            service,
            _factory.build_context(),
            OperationKind.SESSION_LIST,
            session_id=provisioned.session_id,
        )
        result = service.execute(operation, _factory.build_context(), swept=provisioned.swept)

    payload = result.to_dict()
    assert payload["swept_sessions"] == []
    assert "sessions-swept" not in payload["known_effects"]


def test_the_swept_session_payload_carries_no_endpoint_ref(tmp_path: Path) -> None:
    """Trust-zone requirement: a reaped record's secret-equivalent connect
    endpoint must never reach the result, exactly as it never reaches
    ``session clean``'s ``reaped`` payload.
    """
    store = _store(tmp_path)
    service = _service(store)
    _seed(
        store,
        "stale-active",
        status=SessionStatus.ACTIVE,
        expires_at=FIXED_TIME - 60.0,
        last_used_at=FIXED_TIME - 60.0,
    )

    with _factory.ephemeral_session(service, None, hosts=(HOST,)) as provisioned:
        operation = _factory.build_operation(
            service,
            _factory.build_context(),
            OperationKind.SESSION_LIST,
            session_id=provisioned.session_id,
        )
        result = service.execute(operation, _factory.build_context(), swept=provisioned.swept)

    payload = result.to_dict()
    assert len(payload["swept_sessions"]) == 1
    assert "endpoint_ref" not in payload["swept_sessions"][0]


def test_a_sweep_reported_through_the_real_cli_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end, criterion 1 and 2 together: a caller reading only
    ``webglass session create --json`` (no earlier observation of the sweep)
    can name exactly what disappeared.
    """
    store = FileSessionStore(default_sessions_dir())
    now = time.time()
    # Only case-1 records (ACTIVE past expires_at, unleased) come back from
    # ``clean()`` as "reaped" — a purged CLOSED record does not (see
    # ``FileSessionStore.clean``'s docstring), so this has to be the same
    # shape the other opportunistic-sweep tests in this file use.
    _seed(
        store,
        "stale-active",
        status=SessionStatus.ACTIVE,
        expires_at=now - 60.0,
        last_used_at=now - 60.0,
    )

    assert main(["session", "create", "--json"]) == 0
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)

    assert payload["swept_sessions"], "the reaped record must be reachable from --json alone"
    assert payload["swept_sessions"][0]["session_id"] == "stale-active"
    assert "endpoint_ref" not in payload["swept_sessions"][0]
    assert "sessions-swept" in payload["known_effects"]
