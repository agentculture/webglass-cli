"""Cross-invocation sessions: secrecy, leases, and reaping (build plan t12).

Four properties, from the implementation spec's claim c33 / honesty h30 and
claim c29 / honesty h26, each tested against the thing it actually claims:

1. **The endpoint is a secret.** Its file is ``0600`` in a ``0700``
   directory, and a planted endpoint appears in *no* rendering — not in
   ``--json``, not in text output, not in a log record, not in a ``repr()``.
   The browser-backed test at the bottom plants nothing at all: it takes the
   endpoint of a *real* Chromium and asserts the same absence.
2. **Two callers cannot drive one browser.** One acquires, the other gets a
   structured refusal — across store instances, across threads, and across
   processes — and a holder that died is overtaken at lease expiry rather
   than blocking forever.
3. **Cleanup is a process story.** ``session clean`` expires the record
   *and* terminates the browser by its stored pid *and* removes the profile
   directory, reports all of it, and treats an already-dead pid as ordinary.
4. **A session outlives its CLI process.** Three separate OS processes:
   ``session create``, then ``page open`` + ``action press``, then another
   ``action press`` — and the page's in-memory JavaScript array still holds
   every key from all of them.

Browser tests are gated exactly like ``tests/test_playwright_adapter.py``:
``WEBGLASS_TEST_BROWSER=1`` to allow launching Chromium at all, plus
``WEBGLASS_TEST_ALLOW_NO_SANDBOX=1`` on a host whose kernel/AppArmor blocks
the Chromium sandbox (this class of Ubuntu host does). Without them the
browser tests skip with a reason and everything else here still runs::

    WEBGLASS_TEST_BROWSER=1 WEBGLASS_TEST_ALLOW_NO_SANDBOX=1 uv run pytest \\
        tests/test_session_persistence.py -v

Nothing in this file needs a network beyond 127.0.0.1, and nothing outside
the browser-gated tests starts a browser.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess  # nosec B404 - fixed argv, no shell; see each call site
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from webglass.adapters import session_store as store_module
from webglass.adapters.session_store import (
    ALLOW_UNSANDBOXED_ENV,
    RECORD_SCHEMA_VERSION,
    STATE_DIR_ENV,
    FileSessionRecord,
    FileSessionStore,
    LaunchedBrowser,
    SessionLaunchError,
    SessionRecordError,
    default_sessions_dir,
    default_state_root,
    make_playwright_launcher,
    terminate_pid,
)
from webglass.cli import _factory, main
from webglass.sessions import LeaseGrant, LeaseRefusal, SessionRecord, SessionStatus, SessionStore

BROWSER_ENV = "WEBGLASS_TEST_BROWSER"
NO_SANDBOX_ENV = "WEBGLASS_TEST_ALLOW_NO_SANDBOX"

_BROWSER_ENABLED = os.environ.get(BROWSER_ENV) == "1"
_NO_SANDBOX_ALLOWED = os.environ.get(NO_SANDBOX_ENV) == "1"

requires_browser = pytest.mark.skipif(
    not _BROWSER_ENABLED,
    reason=f"browser tests are opt-in: set {BROWSER_ENV}=1 (see this module's docstring)",
)

#: The planted secret. Distinctive enough that finding it anywhere is
#: unambiguous, and shaped like the CDP endpoint it stands in for.
# Deliberately fake and low-entropy so secret scanners (GitGuardian) do not
# flag it; distinctive enough that finding it anywhere is still a leak.
PLANTED_ENDPOINT = "http://127.0.0.1:59999/planted-test-endpoint-not-a-secret"

#: What the CLI factory uses for caller/task (``_factory``'s fixed identity).
#: Records the CLI must be able to see have to carry the same caller, because
#: the service scopes session visibility by it.
CLI_CALLER = _factory._DEFAULT_CALLER
CLI_TASK = _factory._DEFAULT_TASK

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path, **kwargs: object) -> FileSessionStore:
    return FileSessionStore(tmp_path / "sessions", **kwargs)  # type: ignore[arg-type]


def _create(
    store: FileSessionStore,
    session_id: str = "sess-1",
    *,
    now: float = 1000.0,
    ttl: float = 300.0,
    caller: str = CLI_CALLER,
    endpoint: str = PLANTED_ENDPOINT,
) -> FileSessionRecord:
    return store.create(
        session_id=session_id,
        owner=caller,
        caller=caller,
        task=CLI_TASK,
        backend_id="test-backend",
        now=now,
        expires_at=now + ttl,
        capability_profile_ref="built-in-default",
        endpoint_ref=endpoint,
    )


def _sleeper() -> subprocess.Popen[bytes]:
    """A stand-in browser process: something real to kill, by pid."""
    return subprocess.Popen(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c", "import time; time.sleep(120)"]
    )


def _fake_launcher(
    pid: int, *, endpoint: str = PLANTED_ENDPOINT, sandboxed: bool = True
) -> store_module.SessionLauncher:
    def launch(session_id: str, user_data_dir: Path) -> LaunchedBrowser:
        return LaunchedBrowser(
            endpoint=endpoint,
            pid=pid,
            user_data_dir=str(user_data_dir),
            sandboxed=sandboxed,
            diagnostics=() if sandboxed else ("sandbox-disabled: test harness opt-in",),
        )

    return launch


def _run_cli(argv: list[str], env_state_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run ``python -m webglass ...`` in a genuinely separate OS process."""
    env = dict(os.environ)
    env[STATE_DIR_ENV] = str(env_state_dir)
    return subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-m", "webglass", *argv],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(_REPO_ROOT),
        env=env,
    )


# ---------------------------------------------------------------------------
# 1. Where the store lives
# ---------------------------------------------------------------------------


def test_state_root_prefers_the_override_then_xdg_then_the_home_default() -> None:
    assert default_state_root({STATE_DIR_ENV: "/srv/wg"}) == Path("/srv/wg")
    assert default_state_root({"XDG_STATE_HOME": "/xdg"}) == Path("/xdg/webglass")
    # An empty override is not an override: it must not resolve to Path("").
    assert default_state_root({STATE_DIR_ENV: "  ", "XDG_STATE_HOME": "/xdg"}) == Path(
        "/xdg/webglass"
    )
    assert default_state_root({}) == Path.home() / ".local" / "state" / "webglass"
    assert default_sessions_dir({STATE_DIR_ENV: "/srv/wg"}) == Path("/srv/wg/sessions")


def test_constructing_a_store_touches_no_filesystem(tmp_path: Path) -> None:
    """A store nobody uses must leave no directory behind.

    Every CLI invocation builds one, including ``webglass whoami``.
    """
    store = _store(tmp_path)
    assert not (tmp_path / "sessions").exists()
    assert store.list() == []
    assert store.get("nothing") is None
    assert not (tmp_path / "sessions").exists()


def test_the_store_satisfies_the_session_store_protocol(tmp_path: Path) -> None:
    assert isinstance(_store(tmp_path), SessionStore)


def test_records_are_session_records_so_every_existing_consumer_still_works(
    tmp_path: Path,
) -> None:
    record = _create(_store(tmp_path))
    assert isinstance(record, SessionRecord)
    assert record.status is SessionStatus.ACTIVE


# ---------------------------------------------------------------------------
# 2. The endpoint is secret-equivalent (spec claim c33 / honesty h30)
# ---------------------------------------------------------------------------


def test_the_record_file_is_0600_inside_a_0700_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    record_path = tmp_path / "sessions" / "sess-1.json"
    assert PLANTED_ENDPOINT in record_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "sessions").stat().st_mode) == 0o700
    # No group/other bit anywhere: "unreadable by other users" (honesty h30).
    assert not stat.S_IMODE(record_path.stat().st_mode) & 0o077


def test_no_temporary_file_ever_exposes_the_endpoint(tmp_path: Path) -> None:
    """Atomic replacement must not open a world-readable window.

    A ``open(path, "w")`` + ``chmod`` implementation would pass the test
    above and still leave the endpoint readable for the instant in between.
    """
    store = _store(tmp_path)
    _create(store)
    store.acquire_lease("sess-1", "holder-a", now=1001.0)
    leftovers = [p for p in (tmp_path / "sessions").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    for path in (tmp_path / "sessions").iterdir():
        if path.is_file():
            assert not stat.S_IMODE(path.stat().st_mode) & 0o077, path


def test_the_planted_endpoint_is_absent_from_every_public_rendering(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _create(store)
    reread = store.get("sess-1")
    assert reread is not None

    renderings = [
        repr(record),
        repr(reread),
        str(record.to_public_dict()),
        json.dumps(reread.to_public_dict()),
        json.dumps([r.to_public_dict() for r in store.list()]),
    ]
    for rendering in renderings:
        assert PLANTED_ENDPOINT not in rendering
        assert "planted-cdp-secret" not in rendering
    assert "endpoint_ref" not in json.dumps(reread.to_public_dict())
    assert "<redacted>" in repr(record)
    # ...and it really did survive the round trip; the assertions above are
    # about *rendering*, not about the value having been dropped.
    assert reread.endpoint_ref == PLANTED_ENDPOINT


def test_the_extra_public_fields_carry_process_facts_and_no_secret(tmp_path: Path) -> None:
    store = _store(tmp_path, launcher=_fake_launcher(4242, sandboxed=False))
    store.create(
        session_id="sess-1",
        owner=CLI_CALLER,
        caller=CLI_CALLER,
        task=CLI_TASK,
        backend_id="test-backend",
        now=1000.0,
        expires_at=1300.0,
    )
    public = store.get("sess-1").to_public_dict()  # type: ignore[union-attr]
    assert public["pid"] == 4242
    assert public["sandboxed"] is False
    assert public["diagnostics"] == ["sandbox-disabled: test harness opt-in"]
    assert public["browser_reaped"] is False
    assert public["browser_was_running"] is None
    assert PLANTED_ENDPOINT not in json.dumps(public)


def test_endpoint_for_is_the_only_way_out_and_refuses_dead_sessions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    assert store.endpoint_for("sess-1") == PLANTED_ENDPOINT

    with pytest.raises(LookupError) as unknown:
        store.endpoint_for("no-such-session")
    assert PLANTED_ENDPOINT not in str(unknown.value)

    store.close("sess-1")
    with pytest.raises(LookupError) as closed:
        store.endpoint_for("sess-1")
    assert "closed" in str(closed.value)


def test_a_session_created_without_a_browser_has_no_endpoint_to_leak(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, endpoint="")
    with pytest.raises(LookupError) as excinfo:
        store.endpoint_for("sess-1")
    assert "no browser attached" in str(excinfo.value)


def test_planted_endpoint_never_reaches_cli_output_or_log_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The planted-secret test, end to end through the CLI.

    A record carrying a known endpoint is written to the state directory the
    CLI will read, then every session verb is run in both output modes with
    logging captured at ``DEBUG`` for every logger. The secret must appear in
    exactly one place: the ``0600`` file it was written to.
    """
    state_dir = tmp_path / "state"
    monkeypatch.setenv(STATE_DIR_ENV, str(state_dir))
    # Wall-clock timestamps: these verbs run against the real clock, and a
    # session that expired in 1970 would be reaped before `close` saw it.
    _create(FileSessionStore(state_dir / "sessions"), "sess-planted", now=time.time())

    outputs: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for argv in (
            ["session", "list", "--json"],
            ["session", "list"],
            ["session", "show", "sess-planted", "--json"],
            ["session", "show", "sess-planted"],
            ["session", "clean", "--json"],
            ["session", "close", "sess-planted", "--json"],
        ):
            assert main(argv) == 0, argv
            captured = capsys.readouterr()
            outputs.append(captured.out)
            outputs.append(captured.err)

    haystack = "\n".join(outputs + [record.getMessage() for record in caplog.records])
    assert PLANTED_ENDPOINT not in haystack
    assert "planted-cdp-secret" not in haystack
    assert "endpoint" not in haystack
    # The listing really did include the session -- otherwise this test would
    # pass by rendering nothing at all.
    assert "sess-planted" in haystack


def test_the_secret_lives_only_in_the_record_file(tmp_path: Path) -> None:
    """Nothing else the store writes may contain the endpoint."""
    store = _store(tmp_path, launcher=_fake_launcher(1, endpoint=PLANTED_ENDPOINT))
    store.create(
        session_id="sess-1",
        owner=CLI_CALLER,
        caller=CLI_CALLER,
        task=CLI_TASK,
        backend_id="test-backend",
        now=1000.0,
        expires_at=1300.0,
    )
    carriers = []
    for path in (tmp_path / "sessions").rglob("*"):
        if path.is_file() and PLANTED_ENDPOINT in path.read_bytes().decode("utf-8", "replace"):
            carriers.append(path.name)
    assert carriers == ["sess-1.json"]


# ---------------------------------------------------------------------------
# 3. Session ids become filenames, so they are validated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "session_id",
    ["../escape", "a/b", "", ".hidden", "with space", "x" * 129, "/absolute"],
)
def test_a_session_id_can_never_name_a_file_outside_the_store(
    tmp_path: Path, session_id: str
) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        _create(store, session_id)
    assert store.get(session_id) is None
    assert list(tmp_path.rglob("*escape*")) == []


def test_the_cli_refuses_a_traversing_session_id_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["session", "create", "--session-id", "../escape", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["lifecycle_state"] == "failed"
    assert "invalid session id" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# 4. Cross-invocation persistence (no browser needed for the record half)
# ---------------------------------------------------------------------------


def test_two_store_instances_are_one_store(tmp_path: Path) -> None:
    """Two instances stand in for two processes: nothing is cached in memory."""
    writer = _store(tmp_path)
    reader = _store(tmp_path)
    _create(writer)
    record = reader.get("sess-1")
    assert record is not None
    assert record.endpoint_ref == PLANTED_ENDPOINT
    writer.close("sess-1")
    assert reader.get("sess-1").status is SessionStatus.CLOSED  # type: ignore[union-attr]


def test_a_session_created_by_one_cli_process_is_usable_by_the_next(tmp_path: Path) -> None:
    """The record half of spec claim c29, with real subprocess boundaries.

    No browser is involved: this is the persistence property on its own, so
    it holds on every host including browser-less CI.
    """
    state_dir = tmp_path / "state"

    created = _run_cli(["session", "create", "--json"], state_dir)
    assert created.returncode == 0, created.stderr
    session_id = json.loads(created.stdout)["content"]["trusted"]["session"]["session_id"]

    shown = _run_cli(["session", "show", session_id, "--json"], state_dir)
    assert shown.returncode == 0, shown.stderr
    record = json.loads(shown.stdout)["content"]["trusted"]["session"]
    assert record["session_id"] == session_id
    assert record["status"] == "active"

    closed = _run_cli(["session", "close", session_id, "--json"], state_dir)
    assert closed.returncode == 0, closed.stderr

    reshown = _run_cli(["session", "show", session_id, "--json"], state_dir)
    assert reshown.returncode == 1
    assert json.loads(reshown.stdout)["error"]["code"] == "session_not_active"

    # Every one of those processes wrote through the same 0600 record.
    assert stat.S_IMODE((state_dir / "sessions" / f"{session_id}.json").stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# 5. Leases: one holder proceeds, the other is refused
# ---------------------------------------------------------------------------


def test_a_second_holder_gets_a_structured_refusal_not_an_exception(tmp_path: Path) -> None:
    first, second = _store(tmp_path), _store(tmp_path)
    _create(first)

    grant = first.acquire_lease("sess-1", "caller-a:task-a", now=1000.0, ttl_seconds=30.0)
    assert isinstance(grant, LeaseGrant)
    assert grant.expires_at == 1030.0

    refusal = second.acquire_lease("sess-1", "caller-b:task-b", now=1001.0, ttl_seconds=30.0)
    assert isinstance(refusal, LeaseRefusal)
    assert refusal.held_by == "caller-a:task-a"
    assert refusal.reason == "lease_held"


def test_the_same_holder_renews_rather_than_refusing_itself(tmp_path: Path) -> None:
    """Why a later CLI invocation can reuse its own session: the fixed
    ``cli:cli`` holder renews instead of colliding with the previous call."""
    store = _store(tmp_path)
    _create(store)
    store.acquire_lease("sess-1", "cli:cli", now=1000.0, ttl_seconds=30.0)
    renewal = _store(tmp_path).acquire_lease("sess-1", "cli:cli", now=1005.0, ttl_seconds=30.0)
    assert isinstance(renewal, LeaseGrant)
    assert renewal.expires_at == 1035.0


def test_a_crashed_holders_lease_frees_itself_at_expiry(tmp_path: Path) -> None:
    """Crash recovery is expiry-based: nothing has to notice the death."""
    store = _store(tmp_path)
    _create(store)
    store.acquire_lease("sess-1", "crashed:holder", now=1000.0, ttl_seconds=30.0)

    assert isinstance(store.acquire_lease("sess-1", "next:holder", now=1029.9), LeaseRefusal)
    taken = store.acquire_lease("sess-1", "next:holder", now=1030.0)
    assert isinstance(taken, LeaseGrant)


def test_a_closed_session_refuses_leases_with_its_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.close("sess-1")
    refusal = store.acquire_lease("sess-1", "someone", now=1001.0)
    assert isinstance(refusal, LeaseRefusal)
    assert refusal.reason == "session_not_active:closed"


def test_release_lease_only_releases_your_own(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.acquire_lease("sess-1", "holder-a", now=1000.0)
    assert _store(tmp_path).release_lease("sess-1", "holder-b") is False
    assert _store(tmp_path).release_lease("sess-1", "holder-a") is True
    assert store.get("sess-1").lease is None  # type: ignore[union-attr]


def test_concurrent_attaches_grant_exactly_one_holder(tmp_path: Path) -> None:
    """Eight threads, eight store instances, one session, one winner.

    The file lock is what makes this a *guarantee* rather than a race that
    usually works: without it, two read-modify-write cycles interleave and
    both callers believe they hold the lease.
    """
    _create(_store(tmp_path))
    barrier = threading.Barrier(8)
    outcomes: list[object] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        store = _store(tmp_path)
        barrier.wait()
        outcome = store.acquire_lease("sess-1", f"holder-{index}", now=1000.0, ttl_seconds=60.0)
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    grants = [o for o in outcomes if isinstance(o, LeaseGrant)]
    refusals = [o for o in outcomes if isinstance(o, LeaseRefusal)]
    assert len(grants) == 1, outcomes
    assert len(refusals) == 7
    assert {r.held_by for r in refusals} == {grants[0].holder}


def test_two_processes_attaching_to_one_session_yield_one_grant(tmp_path: Path) -> None:
    """The same guarantee across real process boundaries.

    Two OS processes, launched together, each try to lease the same session
    under a different holder. Exactly one may succeed — whichever one; the
    property is that they never both do.
    """
    state_dir = tmp_path / "state"
    _create(FileSessionStore(state_dir / "sessions"))
    script = tmp_path / "attach.py"
    script.write_text(
        textwrap.dedent("""
            import json, sys
            from webglass.adapters.session_store import FileSessionStore
            from webglass.sessions import LeaseGrant

            store = FileSessionStore(sys.argv[1])
            outcome = store.acquire_lease("sess-1", sys.argv[2], now=1000.0, ttl_seconds=600.0)
            print(json.dumps({"granted": isinstance(outcome, LeaseGrant),
                              "reason": getattr(outcome, "reason", "")}))
            """),
        encoding="utf-8",
    )
    processes = [
        subprocess.Popen(  # nosec B603 - fixed argv, no shell
            [sys.executable, str(script), str(state_dir / "sessions"), f"holder-{index}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        for index in range(2)
    ]
    results = []
    for process in processes:
        out, err = process.communicate(timeout=120)
        assert process.returncode == 0, err
        results.append(json.loads(out))

    assert [r["granted"] for r in results].count(True) == 1, results
    refused = next(r for r in results if not r["granted"])
    assert refused["reason"] == "lease_held"


def test_lease_operations_on_an_unknown_session_raise_like_the_in_memory_store(
    tmp_path: Path,
) -> None:
    """Protocol parity: an unknown id is a ``KeyError``, not a silent no-op.

    The service checks existence before it ever gets here, so this is the
    contract a *library* caller meets — and it must match
    :class:`~webglass.sessions.InMemorySessionStore`'s, or a store swap would
    change behavior.
    """
    store = _store(tmp_path)
    _create(store)
    for call in (
        lambda: store.close("no-such-session"),
        lambda: store.acquire_lease("no-such-session", "holder", now=1000.0),
        lambda: store.release_lease("no-such-session", "holder"),
        lambda: store.bump_generation("no-such-session"),
    ):
        with pytest.raises(KeyError):
            call()


def test_a_duplicate_session_id_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    with pytest.raises(ValueError, match="already exists"):
        _create(_store(tmp_path))


def test_bump_generation_persists(tmp_path: Path) -> None:
    """Element references are scoped to a session generation, so the counter
    has to survive the process that incremented it."""
    store = _store(tmp_path)
    _create(store)
    assert store.bump_generation("sess-1").generation == 1
    assert _store(tmp_path).bump_generation("sess-1").generation == 2
    assert store.get("sess-1").generation == 2  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"schema_version": 1}',
        '{"schema_version": 1, "session_id": "sess-1", "status": "not-a-status"}',
    ],
)
def test_a_structurally_wrong_record_is_refused_never_guessed_at(
    tmp_path: Path, payload: str
) -> None:
    store = _store(tmp_path)
    _create(store)
    (tmp_path / "sessions" / "sess-1.json").write_text(payload, encoding="utf-8")
    with pytest.raises(SessionRecordError):
        store.get("sess-1")


def test_session_launch_error_renders_as_structured_data() -> None:
    error = SessionLaunchError("browser_launch_failed", "nope", "try this")
    assert error.to_dict() == {
        "code": "browser_launch_failed",
        "message": "nope",
        "remediation": "try this",
    }
    assert "try this" in str(error)
    assert str(SessionLaunchError("c", "just a message")) == "just a message"


def test_the_cli_reports_a_lease_conflict_as_a_structured_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What a refused second attacher actually sees at the CLI."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv(STATE_DIR_ENV, str(state_dir))
    store = FileSessionStore(state_dir / "sessions")
    _create(store, "sess-leased", now=time.time())
    store.acquire_lease("sess-leased", "someone-else:task", now=time.time(), ttl_seconds=600.0)

    rc = main(["action", "press", "a", "--session-id", "sess-leased", "--apply", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["lifecycle_state"] == "denied"
    assert payload["error"]["code"] in {"session_lease_held", "remote_action_apply_unavailable"}


# ---------------------------------------------------------------------------
# 6. Cleanup: records, processes, and profile directories
# ---------------------------------------------------------------------------


def test_clean_expires_the_record_and_kills_the_browser(tmp_path: Path) -> None:
    sleeper = _sleeper()
    try:
        store = _store(tmp_path, launcher=_fake_launcher(sleeper.pid))
        store.create(
            session_id="sess-1",
            owner=CLI_CALLER,
            caller=CLI_CALLER,
            task=CLI_TASK,
            backend_id="test-backend",
            now=1000.0,
            expires_at=1300.0,
        )
        profile = tmp_path / "sessions" / "profiles" / "sess-1"
        assert profile.is_dir()

        assert store.clean(now=1299.0) == []  # not expired yet: nothing touched
        assert sleeper.poll() is None

        reaped = store.clean(now=1300.0)
        assert [r.session_id for r in reaped] == ["sess-1"]
        assert reaped[0].status is SessionStatus.EXPIRED
        assert reaped[0].browser_reaped is True
        assert reaped[0].browser_was_running is True
        assert reaped[0].endpoint_ref == ""
        assert sleeper.wait(timeout=30) is not None
        assert not profile.exists()
        # The record survives the reap (so `session show` can answer) and
        # says what happened to the browser.
        assert store.get("sess-1").browser_reaped is True  # type: ignore[union-attr]
    finally:
        sleeper.kill()
        sleeper.wait(timeout=30)


def test_clean_handles_an_already_dead_browser_gracefully(tmp_path: Path) -> None:
    """The crashed-caller case: the pid is stale, and that is not an error."""
    sleeper = _sleeper()
    sleeper.kill()
    sleeper.wait(timeout=30)

    store = _store(tmp_path, launcher=_fake_launcher(sleeper.pid))
    store.create(
        session_id="sess-1",
        owner=CLI_CALLER,
        caller=CLI_CALLER,
        task=CLI_TASK,
        backend_id="test-backend",
        now=1000.0,
        expires_at=1300.0,
    )
    reaped = store.clean(now=2000.0)
    assert len(reaped) == 1
    assert reaped[0].browser_reaped is True
    assert reaped[0].browser_was_running is False


def test_terminate_pid_reports_whether_anything_was_running() -> None:
    sleeper = _sleeper()
    assert terminate_pid(sleeper.pid, grace_seconds=30.0) is True
    assert terminate_pid(sleeper.pid, grace_seconds=1.0) is False
    assert terminate_pid(-1) is False
    assert terminate_pid(0) is False


def test_a_browser_that_ignores_sigterm_is_killed(tmp_path: Path) -> None:
    stubborn = subprocess.Popen(  # nosec B603 - fixed argv, no shell
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(120)",
        ]
    )
    try:
        # Give the child a moment to install its handler; otherwise SIGTERM
        # lands first and the escalation path is never exercised.
        time.sleep(0.5)
        assert terminate_pid(stubborn.pid, grace_seconds=1.0) is True
        assert stubborn.wait(timeout=30) is not None
    finally:
        stubborn.kill()


def test_close_terminates_the_browser_and_removes_its_profile(tmp_path: Path) -> None:
    sleeper = _sleeper()
    try:
        store = _store(tmp_path, launcher=_fake_launcher(sleeper.pid))
        store.create(
            session_id="sess-1",
            owner=CLI_CALLER,
            caller=CLI_CALLER,
            task=CLI_TASK,
            backend_id="test-backend",
            now=1000.0,
            expires_at=1300.0,
        )
        store.close("sess-1")
        record = store.get("sess-1")
        assert record is not None
        assert record.status is SessionStatus.CLOSED
        assert record.browser_reaped is True
        assert record.endpoint_ref == ""
        assert sleeper.wait(timeout=30) is not None
        assert not (tmp_path / "sessions" / "profiles" / "sess-1").exists()

        # Closing twice must not signal a pid that may have been recycled.
        store.close("sess-1")
        assert store.get("sess-1").browser_was_running is True  # type: ignore[union-attr]
    finally:
        sleeper.kill()
        sleeper.wait(timeout=30)


def test_clean_releases_an_expired_lease_on_a_still_live_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, now=1000.0, ttl=10_000.0)
    store.acquire_lease("sess-1", "crashed:holder", now=1000.0, ttl_seconds=30.0)

    assert store.clean(now=1020.0) == []
    assert store.get("sess-1").lease is not None  # type: ignore[union-attr]

    assert store.clean(now=1031.0) == []
    record = store.get("sess-1")
    assert record is not None
    assert record.lease is None
    assert record.status is SessionStatus.ACTIVE


def test_clean_purges_long_dead_records_but_keeps_recent_ones(tmp_path: Path) -> None:
    store = _store(tmp_path, record_retention_seconds=100.0)
    _create(store, "sess-old", now=1000.0, ttl=10.0)
    _create(store, "sess-new", now=1000.0, ttl=10.0)
    store.clean(now=1010.0)  # both expire

    store.clean(now=1050.0)
    assert {r.session_id for r in store.list()} == {"sess-old", "sess-new"}

    (tmp_path / "sessions" / "sess-new.json").write_text(
        json.dumps(
            {
                **json.loads((tmp_path / "sessions" / "sess-new.json").read_text(encoding="utf-8")),
                "last_used_at": 1200.0,
            }
        ),
        encoding="utf-8",
    )
    store.clean(now=1200.0)
    assert {r.session_id for r in store.list()} == {"sess-new"}


def test_a_corrupt_record_is_loud_and_clean_is_the_recovery_path(tmp_path: Path) -> None:
    """Never "no such session": that would hand out a fresh browser while an
    unreachable one kept running."""
    store = _store(tmp_path)
    _create(store)
    (tmp_path / "sessions" / "sess-1.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(SessionRecordError):
        store.get("sess-1")
    with pytest.raises(SessionRecordError):
        store.list()

    assert store.clean(now=2000.0) == []
    assert store.list() == []
    assert store.get("sess-1") is None


def test_a_record_from_another_schema_version_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    path = tmp_path / "sessions" / "sess-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == RECORD_SCHEMA_VERSION
    payload["schema_version"] = RECORD_SCHEMA_VERSION + 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionRecordError) as excinfo:
        store.get("sess-1")
    assert "schema_version" in str(excinfo.value)


def test_stray_files_in_the_sessions_directory_are_ignored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    (tmp_path / "sessions" / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "sessions" / ".hidden.json").write_text("{}", encoding="utf-8")
    assert [record.session_id for record in store.list()] == ["sess-1"]


def test_clean_reports_the_reap_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``session clean --json`` shows what it killed."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv(STATE_DIR_ENV, str(state_dir))
    sleeper = _sleeper()
    try:
        store = FileSessionStore(state_dir / "sessions", launcher=_fake_launcher(sleeper.pid))
        store.create(
            session_id="sess-expired",
            owner=CLI_CALLER,
            caller=CLI_CALLER,
            task=CLI_TASK,
            backend_id="test-backend",
            now=time.time() - 600.0,
            expires_at=time.time() - 300.0,
        )
        rc = main(["session", "clean", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        reaped = payload["content"]["trusted"]["reaped"]
        assert [r["session_id"] for r in reaped] == ["sess-expired"]
        assert reaped[0]["status"] == "expired"
        assert reaped[0]["browser_reaped"] is True
        assert reaped[0]["browser_was_running"] is True
        assert "endpoint" not in json.dumps(payload)
        assert sleeper.wait(timeout=30) is not None
    finally:
        sleeper.kill()
        sleeper.wait(timeout=30)


# ---------------------------------------------------------------------------
# 7. Launching: the sandbox posture is explicit, and failures are structured
# ---------------------------------------------------------------------------


def test_the_playwright_launcher_never_disables_the_sandbox_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No browser is started: only the argument the adapter would receive."""
    from webglass.adapters import playwright as adapter

    seen: list[bool] = []

    def fake_launch_detached(user_data_dir: object, **kwargs: object) -> adapter.DetachedBrowser:
        seen.append(bool(kwargs["allow_unsandboxed"]))
        return adapter.DetachedBrowser(
            endpoint=PLANTED_ENDPOINT,
            pid=1,
            user_data_dir=str(user_data_dir),
            sandboxed=not kwargs["allow_unsandboxed"],
            diagnostics=(
                (adapter.SANDBOX_DISABLED_WARNING,) if kwargs["allow_unsandboxed"] else ()
            ),
        )

    monkeypatch.setattr(adapter, "launch_detached", fake_launch_detached)

    monkeypatch.delenv(ALLOW_UNSANDBOXED_ENV, raising=False)
    launched = make_playwright_launcher()("sess-1", tmp_path)
    assert seen == [False]
    assert launched.sandboxed is True
    assert launched.diagnostics == ()

    monkeypatch.setenv(ALLOW_UNSANDBOXED_ENV, "1")
    loud = make_playwright_launcher()("sess-1", tmp_path)
    assert seen == [False, True]
    assert loud.sandboxed is False
    assert adapter.SANDBOX_DISABLED_WARNING in loud.diagnostics


def test_a_sandbox_refusal_reaches_the_caller_with_its_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webglass.adapters import playwright as adapter

    def refuse(user_data_dir: object, **kwargs: object) -> adapter.DetachedBrowser:
        raise adapter.BrowserLaunchError(
            "browser_sandbox_unavailable",
            "Chromium exited without a usable sandbox",
            adapter.SANDBOX_REMEDIATION,
        )

    monkeypatch.setattr(adapter, "launch_detached", refuse)
    store = _store(tmp_path, launcher=make_playwright_launcher())

    with pytest.raises(SessionLaunchError) as excinfo:
        store.create(
            session_id="sess-1",
            owner=CLI_CALLER,
            caller=CLI_CALLER,
            task=CLI_TASK,
            backend_id="playwright-chromium",
            now=1000.0,
            expires_at=1300.0,
        )
    error = excinfo.value
    assert error.code == "browser_sandbox_unavailable"
    assert "AppArmor" in error.remediation
    assert "AppArmor" in str(error)  # the service renders the message alone
    # A launch that failed leaves nothing to clean up later.
    assert store.get("sess-1") is None
    assert not (tmp_path / "sessions" / "profiles" / "sess-1").exists()


def test_a_launch_failure_surfaces_as_a_structured_cli_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No traceback, an actionable message, exit 1."""
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))

    def explode(session_id: str, user_data_dir: Path) -> LaunchedBrowser:
        raise SessionLaunchError("browser_launch_failed", "the browser died", "check the log")

    monkeypatch.setattr(
        _factory,
        "build_session_store",
        lambda *args, **kwargs: FileSessionStore(tmp_path / "state" / "sessions", launcher=explode),
    )
    rc = main(["session", "create", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["lifecycle_state"] == "failed"
    assert "the browser died" in payload["error"]["message"]
    assert "check the log" in payload["error"]["message"]


def test_the_sandbox_posture_is_visible_in_session_create_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The loud half of the explicit opt-in: the record says it ran unsandboxed."""
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(
        _factory,
        "build_session_store",
        lambda *args, **kwargs: FileSessionStore(
            tmp_path / "state" / "sessions",
            launcher=_fake_launcher(1, sandboxed=False),
        ),
    )
    rc = main(["session", "create", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    session = payload["content"]["trusted"]["session"]
    assert session["sandboxed"] is False
    assert session["diagnostics"] == ["sandbox-disabled: test harness opt-in"]
    assert PLANTED_ENDPOINT not in json.dumps(payload)


def test_a_non_json_capability_profile_ref_is_stringified_loudly(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create(
        session_id="sess-1",
        owner=CLI_CALLER,
        caller=CLI_CALLER,
        task=CLI_TASK,
        backend_id="test-backend",
        now=1000.0,
        expires_at=1300.0,
        capability_profile_ref=object(),
    )
    assert record.diagnostics
    assert "capability-profile-ref-stringified" in record.diagnostics[0]
    assert store.get("sess-1") is not None  # it round-trips through JSON


# ---------------------------------------------------------------------------
# 8. Factory wiring: the store and the endpoint-resolver seam
# ---------------------------------------------------------------------------


def test_the_default_cli_store_is_the_file_store_in_the_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
    store = _factory.build_session_store()
    assert isinstance(store, FileSessionStore)
    assert store.directory == tmp_path / "state" / "sessions"
    assert store.launcher is None  # no browser configured
    assert isinstance(_factory.build_service().sessions, FileSessionStore)


def test_the_browser_backend_is_on_by_default_and_explicitly_switchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build plan t13 flipped this default; t12 wired what it flips onto.

    Unset means ``playwright`` — the point of M2 is that ``page open`` opens a
    real page. ``none`` stays a first-class, documented posture rather than an
    accident of an unset variable, which is why the whole default test suite
    pins it (``tests/conftest.py``).
    """
    monkeypatch.delenv(_factory.BROWSER_BACKEND_ENV, raising=False)
    assert _factory.browser_backend_name() == "playwright"
    assert _factory.build_session_store().launcher is not None

    monkeypatch.setenv(_factory.BROWSER_BACKEND_ENV, "none")
    assert _factory.browser_backend_name() == "none"
    assert _factory.build_service().browser is None
    assert _factory.build_session_store().launcher is None


def test_an_unknown_browser_backend_is_an_environment_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(_factory.BROWSER_BACKEND_ENV, "firefox")
    rc = main(["session", "list", "--json"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "firefox" in err
    assert "playwright" in err


def test_the_browser_backend_resolves_endpoints_through_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam t13 inherits: a session id in, a stored endpoint out.

    Builds the real Playwright backend object — no browser is launched and no
    connection is opened, because ``PlaywrightBrowserBackend`` connects lazily
    on first use.
    """
    monkeypatch.setenv(_factory.BROWSER_BACKEND_ENV, "playwright")
    _factory.reset_browser_backends()
    store = _store(tmp_path)
    _create(store)
    backend = _factory.build_browser_backend(store)
    assert backend is not None
    assert backend.endpoint_for("sess-1") == PLANTED_ENDPOINT  # type: ignore[attr-defined]
    with pytest.raises(LookupError):
        backend.endpoint_for("unknown-session")  # type: ignore[attr-defined]
    # And the wiring layer quieted Playwright's interpreter-exit chatter, which
    # would otherwise reach stderr as a traceback (see the adapter docstring).
    assert logging.getLogger("asyncio").level == logging.CRITICAL


def test_the_browser_backend_is_one_per_process_not_one_per_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two web operations in one process must share one Playwright driver.

    Playwright's sync API refuses to start a second driver in a thread that
    already runs one, so a per-invocation backend would make the *second*
    operation of any process fail. Found the hard way: the three-process
    end-to-end test below failed on its ``action press`` until this held.
    """
    monkeypatch.setenv(_factory.BROWSER_BACKEND_ENV, "playwright")
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
    _factory.reset_browser_backends()

    first = _factory.build_service()
    second = _factory.build_service()
    assert first is not second
    assert first.browser is not None
    assert first.browser is second.browser

    _factory.reset_browser_backends()
    assert _factory.build_service().browser is not first.browser


# ---------------------------------------------------------------------------
# 9. The whole thing, with a real browser: three processes, one live page
# ---------------------------------------------------------------------------

_OPEN_AND_PRESS = """\
import sys
from webglass.cli import _factory, main
from webglass.effects import EffectClass, OperationKind
from webglass.policy import WebPolicyEvaluator, WebPolicyProfile

# The CLI has no --policy-profile flag and no test-profile effect resolver
# yet: both belong to the page/action verb surface (build plan t13). Until
# then the documented factory seam -- "a later task can rebind one entry at
# process start" -- is how a caller supplies an effective profile. Everything
# below this point is the ordinary CLI: main(), the file store, the real
# operation service.
_factory._DEFAULT_SERVICE_KWARGS["policy"] = WebPolicyEvaluator(
    WebPolicyProfile.default().with_declared_targets([{site!r}])
)
_factory._DEFAULT_SERVICE_KWARGS["effect_class_resolver"] = (
    lambda operation, profile: (
        EffectClass.OBSERVE
        if operation.kind is OperationKind.ACTION_PRESS
        else operation.effect_class
    )
)

for argv in {argv!r}:
    code = main(argv)
    if code != 0:
        sys.exit(code)
print("cli-ok")
"""

_READ_LIVE_DOM = """\
import json
from webglass.cli import _factory

# Reading a live page *without navigating* is not a CLI verb yet: page read /
# inspect project a retained snapshot, which is per-process, and navigating
# again would destroy the very in-memory state this proves. That verb is build
# plan t13. So this asks the same backend object the CLI call above just used
# -- built by the factory, resolving its endpoint out of the file store -- for
# the live DOM.
backend = _factory.build_browser_backend(_factory.build_session_store())
html = backend.current({session!r}).html
print(json.dumps({{"html": html}}))
"""


@pytest.fixture
def browser_state_dir(tmp_path: Path) -> Iterator[Path]:
    """A state directory whose sessions are always torn down.

    A leaked Chromium would outlive the test run — the whole point of a
    detached browser is that nothing supervises it — so cleanup goes through
    the store itself, by pid, exactly as ``session clean`` does.
    """
    state_dir = tmp_path / "state"
    try:
        yield state_dir
    finally:
        store = FileSessionStore(state_dir / "sessions")
        if store.directory.exists():
            for record in store.list():
                if record.pid is not None:
                    store_module.terminate_pid(record.pid, grace_seconds=10.0)


def _browser_env(state_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env[STATE_DIR_ENV] = str(state_dir)
    env[_factory.BROWSER_BACKEND_ENV] = "playwright"
    if _NO_SANDBOX_ALLOWED:
        env[ALLOW_UNSANDBOXED_ENV] = "1"
    return env


def _run_script(source: str, env: dict[str, str], tmp_path: Path, name: str) -> str:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    completed = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert completed.returncode == 0, f"{name} failed:\n{completed.stdout}\n{completed.stderr}"
    return completed.stdout


@requires_browser
def test_a_session_survives_three_separate_cli_processes(
    browser_state_dir: Path, tmp_path: Path, fixture_site: str
) -> None:
    """Spec claim c29 end to end: three processes, one browser, one JS array.

    * **process 1** — ``python -m webglass session create``: launches a
      detached Chromium and writes its endpoint into a ``0600`` record. The
      endpoint appears nowhere in that process's own JSON output.
    * **process 2** — ``page open`` the keydown fixture, then ``action press
      a b``. It reattaches through the stored endpoint; it never saw the
      browser start.
    * **process 3** — ``action press c``, then read the live DOM. The page's
      own ``window.__webglassKeyLog`` reads ``["a","b","c"]``: the array
      itself survived two process exits. A lost array would have started over
      at ``["c"]``.
    * **process 4** — ``session close``: the browser process is gone
      afterwards, which is what "callers never hold a daemon connection"
      has to mean.
    """
    env = _browser_env(browser_state_dir)
    session_id = "e2e-session"

    created = subprocess.run(  # nosec B603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "webglass",
            "session",
            "create",
            "--session-id",
            session_id,
            "--ttl-seconds",
            "600",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert created.returncode == 0, created.stderr
    payload = json.loads(created.stdout)
    public = payload["content"]["trusted"]["session"]
    assert public["session_id"] == session_id
    assert public["pid"] > 0
    assert public["sandboxed"] is not _NO_SANDBOX_ALLOWED

    store = FileSessionStore(browser_state_dir / "sessions")
    record = store.get(session_id)
    assert record is not None
    endpoint = record.endpoint_ref
    assert endpoint.startswith("http://127.0.0.1:")

    # The planted-secret test, with a real secret: a live CDP endpoint that
    # grants full control of this browser, absent from the process's own
    # output and present only in its 0600 record.
    assert endpoint not in created.stdout
    assert endpoint not in created.stderr
    assert "endpoint" not in created.stdout
    assert stat.S_IMODE((browser_state_dir / "sessions" / f"{session_id}.json").stat().st_mode) == (
        0o600
    )
    # No traceback reached stderr, browser wiring included (the CLI's error
    # contract, which Playwright's teardown chatter would otherwise break).
    assert "Traceback" not in created.stderr

    second = _run_script(
        _OPEN_AND_PRESS.format(
            site=fixture_site,
            argv=[
                ["page", "open", f"{fixture_site}/keydown", "--session-id", session_id, "--json"],
                ["action", "press", "a", "b", "--session-id", session_id, "--json"],
            ],
        ),
        env,
        tmp_path,
        "process2.py",
    )
    assert "cli-ok" in second
    assert endpoint not in second

    third = _run_script(
        _OPEN_AND_PRESS.format(
            site=fixture_site,
            argv=[["action", "press", "c", "--session-id", session_id, "--json"]],
        )
        + _READ_LIVE_DOM.format(session=session_id),
        env,
        tmp_path,
        "process3.py",
    )
    assert "cli-ok" in third
    html = json.loads(third.splitlines()[-1])["html"]
    # The fixture mirrors window.__webglassKeyLog into the DOM, so the page's
    # own in-memory array is what this asserts on.
    assert "&quot;a&quot;,&quot;b&quot;,&quot;c&quot;" in html
    assert ">a,b,c<" in html

    closed = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-m", "webglass", "session", "close", session_id, "--json"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert closed.returncode == 0, closed.stderr
    closed_public = json.loads(closed.stdout)["content"]["trusted"]["session"]
    assert closed_public["status"] == "closed"
    assert closed_public["browser_reaped"] is True
    assert closed_public["browser_was_running"] is True

    deadline = time.time() + 30
    while time.time() < deadline and store_module._is_running(record.pid or 0):
        time.sleep(0.1)
    assert not store_module._is_running(record.pid or 0), "the browser outlived session close"
    assert not (browser_state_dir / "sessions" / "profiles" / session_id).exists()


@requires_browser
def test_clean_reaps_a_real_expired_browser(browser_state_dir: Path, tmp_path: Path) -> None:
    """The crashed-caller path with a real browser: expiry, then reaping.

    Nobody closes this session — it simply runs out of time, exactly as a
    caller that died would leave it. ``session clean`` in a *later* process
    has to be what stops the browser.
    """
    env = _browser_env(browser_state_dir)
    created = subprocess.run(  # nosec B603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "webglass",
            "session",
            "create",
            "--session-id",
            "reap-me",
            "--ttl-seconds",
            "1",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert created.returncode == 0, created.stderr
    record = FileSessionStore(browser_state_dir / "sessions").get("reap-me")
    assert record is not None and record.pid is not None
    assert store_module._is_running(record.pid)

    time.sleep(1.2)
    cleaned = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-m", "webglass", "session", "clean", "--json"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert cleaned.returncode == 0, cleaned.stderr
    reaped = json.loads(cleaned.stdout)["content"]["trusted"]["reaped"]
    assert [r["session_id"] for r in reaped] == ["reap-me"]
    assert reaped[0]["browser_was_running"] is True
    assert record.endpoint_ref not in cleaned.stdout

    deadline = time.time() + 30
    while time.time() < deadline and store_module._is_running(record.pid):
        time.sleep(0.1)
    assert not store_module._is_running(record.pid), "an expired browser survived session clean"
