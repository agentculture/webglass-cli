"""Flow-scoped session reuse (build plan task t13, spec claims c23/c33).

A one-shot ``webglass`` invocation normally launches a Chromium, observes one
page, and tears the whole thing down again. When several invocations are one
*flow* — an agent taking three steps through the same site — that is three
browser launches for one continuation. This module pins the narrow, opt-in
mechanism that lets step two continue step one's session, and every guard
that keeps it from becoming session *sharing*:

* only the flow's **own** sessions are eligible (t7's ``owner_token``; a
  pre-upgrade record with no token is nobody's to claim);
* only an **ACTIVE, unexpired, lease-acquirable** record is offered — a
  closed record is a tombstone whose profile directory ``_reap_browser``
  already deleted, so there is nothing there to resurrect;
* only a record that actually **visited this host** (t8's ``hosts``, where
  ``None`` means "never tracked", not "matches anything");
* reuse **bumps the session generation**, so a reference minted before the
  reuse is refused rather than resolved against a different element;
* reuse is **observable** in the result and a caller can always **demand a
  fresh context**.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from webglass.adapters import FakeArtifactStore, FakeBrowserBackend, FixedClock, SequentialIds
from webglass.adapters.browser import FakeBrowserRoute
from webglass.adapters.session_store import FileSessionRecord, FileSessionStore, LaunchedBrowser
from webglass.cli import _factory
from webglass.cli._commands import page as page_cmd
from webglass.context import WebContext
from webglass.effects import OperationKind
from webglass.operations import OperationTarget
from webglass.results import LifecycleState
from webglass.service import ERROR_STALE_REFERENCE, WebGlassService
from webglass.sessions import SessionStatus

FIXED_TIME = 1_700_000_000.0
FLOW_TOKEN = "flow-token-1"
HOST = "app.test"
PAGE_URL = f"http://{HOST}/one"
OTHER_URL = f"http://{HOST}/two"

_PAGE_HTML = """
<html><body><main>
<h1>Reuse fixture</h1>
<p>A paragraph with a <a href="/next">link</a>.</p>
</main></body></html>
"""


def _store(tmp_path: Path, *, with_launcher: bool = True) -> FileSessionStore:
    """A file store whose "launcher" starts nothing and reports a live pid."""
    if not with_launcher:
        return FileSessionStore(tmp_path / "sessions")

    def launch(session_id: str, user_data_dir: Path) -> LaunchedBrowser:
        return LaunchedBrowser(
            pid=None,  # nothing is launched, so there is no process to reap
            endpoint=f"http://127.0.0.1:0/{session_id}",
            user_data_dir=str(user_data_dir),
            sandboxed=True,
        )

    return FileSessionStore(tmp_path / "sessions", launcher=launch)


def _service(store: FileSessionStore, browser: object | None = None) -> WebGlassService:
    routes = {
        PAGE_URL: FakeBrowserRoute(html=_PAGE_HTML),
        OTHER_URL: FakeBrowserRoute(html=_PAGE_HTML),
    }
    return WebGlassService(
        clock=FixedClock(FIXED_TIME),
        ids=SequentialIds(),
        browser=FakeBrowserBackend(routes) if browser is None else browser,  # type: ignore
        sessions=store,
        artifacts=FakeArtifactStore(),
    )


def _context() -> WebContext:
    return WebContext(
        caller="cli",
        task="cli",
        workspace="/fixture-workspace",
        policy_profile_ref="built-in-default",
        evidence_namespace="cli",
    )


def _seed(
    store: FileSessionStore,
    session_id: str,
    *,
    status: SessionStatus = SessionStatus.ACTIVE,
    owner_token: str = FLOW_TOKEN,
    hosts: tuple[str, ...] | None = (HOST,),
    expires_at: float = FIXED_TIME + 300.0,
    last_used_at: float = FIXED_TIME,
) -> FileSessionRecord:
    """One record of a chosen shape, written the way the store itself writes."""
    record = FileSessionRecord(
        session_id=session_id,
        generation=0,
        owner="cli",
        caller="cli",
        task="cli",
        backend_id="fake",
        created_at=FIXED_TIME - 10.0,
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


def _match(store: FileSessionStore, **kwargs: object) -> FileSessionRecord | None:
    defaults: dict[str, object] = {
        "owner_token": FLOW_TOKEN,
        "hosts": (HOST,),
        "now": FIXED_TIME,
    }
    defaults.update(kwargs)
    return _factory.find_reusable_session(store, **defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. Matching: the owner's recent sessions, by visited host
# ---------------------------------------------------------------------------


def test_an_active_session_that_visited_this_host_is_offered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, "keeper")
    matched = _match(store)
    assert matched is not None
    assert matched.session_id == "keeper"


def test_a_session_that_never_visited_this_host_is_not_offered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, "elsewhere", hosts=("other.test",))
    assert _match(store) is None


def test_a_record_that_never_tracked_hosts_is_not_a_wildcard(tmp_path: Path) -> None:
    """``hosts is None`` is *unknown*, not "matches anything" (t8)."""
    store = _store(tmp_path)
    _seed(store, "pre-upgrade-hosts", hosts=None)
    assert _match(store) is None


def test_a_record_owned_by_another_flow_is_not_offered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, "someone-elses", owner_token="flow-token-2")
    assert _match(store) is None


def test_a_record_with_no_owner_token_is_claimed_by_nobody(tmp_path: Path) -> None:
    """A pre-upgrade record (``owner_token == ""``) is never reused (h13)."""
    store = _store(tmp_path)
    _seed(store, "unowned", owner_token="")
    assert _match(store) is None
    # ...and an invocation with no flow token of its own cannot claim it by
    # matching emptiness against emptiness.
    assert _match(store, owner_token="") is None


def test_only_the_owners_most_recent_sessions_are_considered(tmp_path: Path) -> None:
    """Matching looks at the last few sessions, not the whole store (c23)."""
    store = _store(tmp_path)
    for index in range(_factory.REUSE_CANDIDATE_LIMIT):
        _seed(
            store,
            f"recent-{index}",
            hosts=("other.test",),
            last_used_at=FIXED_TIME - index,
        )
    _seed(store, "older-but-matching", last_used_at=FIXED_TIME - 100.0)
    assert _match(store) is None

    # The same record *is* offered once it is inside the recent window.
    _seed(store, "older-but-matching", last_used_at=FIXED_TIME)
    matched = _match(store)
    assert matched is not None
    assert matched.session_id == "older-but-matching"


# ---------------------------------------------------------------------------
# 2. Liveness: a closed or expired record is never offered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [SessionStatus.CLOSED, SessionStatus.EXPIRED])
def test_a_closed_or_expired_record_is_never_offered(tmp_path: Path, status: SessionStatus) -> None:
    """Its profile directory is already gone — there is nothing to reuse."""
    store = _store(tmp_path)
    _seed(store, f"dead-{status.value}", status=status)
    assert _match(store) is None


def test_a_record_past_its_expiry_is_never_offered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, "stale", expires_at=FIXED_TIME - 1.0)
    assert _match(store) is None


def test_a_session_leased_by_another_holder_is_not_stolen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, "busy")
    granted = store.acquire_lease("busy", "someone:else", FIXED_TIME, 60.0)
    assert getattr(granted, "holder", None) == "someone:else"
    assert _match(store) is None
    # Once the lease lapses it is eligible again.
    assert _match(store, now=FIXED_TIME + 120.0) is not None


# ---------------------------------------------------------------------------
# 3. Reuse bumps the generation
# ---------------------------------------------------------------------------


def test_reuse_bumps_the_generation_of_the_session_it_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    service = _service(store)
    _seed(store, "keeper")
    monkeypatch.setenv(_factory.SESSION_OWNER_ENV, FLOW_TOKEN)

    with _factory.ephemeral_session(service, None, hosts=(HOST,)) as provisioned:
        assert provisioned.session_id == "keeper"
        assert provisioned.reused is True
        assert provisioned.ephemeral is False

    record = store.get("keeper")
    assert record is not None
    assert record.generation == 1
    # A reused session belongs to the flow, not to this invocation: it must
    # still be there for the next step.
    assert record.status is SessionStatus.ACTIVE


def test_a_reference_minted_before_the_reuse_is_refused_as_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The correctness guarantee the generation bump exists for (h27).

    Snapshots are minted at the *session's* generation, so a reference that
    was in scope before the reuse names a generation the reused session has
    left behind — and resolving it against the new page is exactly the
    cross-generation confusion ``references.py`` exists to prevent.
    """
    store = _store(tmp_path)
    service = _service(store)
    context = _context()
    _seed(store, "keeper")
    monkeypatch.setenv(_factory.SESSION_OWNER_ENV, FLOW_TOKEN)

    before = service.execute(
        _factory.build_operation(
            service,
            context,
            OperationKind.PAGE_OPEN,
            target=OperationTarget(url=PAGE_URL),
            session_id="keeper",
        ),
        context,
    )
    assert before.lifecycle_state is LifecycleState.SUCCEEDED
    stale_generation = before.content.trusted["snapshot"]["generation"]

    with _factory.ephemeral_session(service, None, hosts=(HOST,)) as provisioned:
        after = service.execute(
            _factory.build_operation(
                service,
                context,
                OperationKind.PAGE_OPEN,
                target=OperationTarget(url=OTHER_URL),
                session_id=provisioned.session_id,
                session_reused=provisioned.reused,
            ),
            context,
        )
    assert after.lifecycle_state is LifecycleState.SUCCEEDED
    fresh_snapshot = after.content.trusted["snapshot"]
    assert fresh_snapshot["generation"] == stale_generation + 1

    # Same snapshot, the generation the caller last saw: refused, not resolved.
    stale_ref = f"{fresh_snapshot['snapshot_id']}@{stale_generation}/link:0"
    refused = service.execute(
        _factory.build_operation(
            service,
            context,
            OperationKind.ACTION_FOLLOW,
            target=OperationTarget(page_ref=fresh_snapshot["snapshot_id"], element_ref=stale_ref),
            session_id="keeper",
        ),
        context,
    )
    assert refused.error is not None
    assert refused.error.code == ERROR_STALE_REFERENCE


# ---------------------------------------------------------------------------
# 4. Opt-in and observable
# ---------------------------------------------------------------------------


def test_reuse_never_happens_without_a_flow_owner_token(tmp_path: Path) -> None:
    """Unset ``$WEBGLASS_SESSION_OWNER`` means every invocation is anonymous."""
    store = _store(tmp_path)
    service = _service(store)
    _seed(store, "keeper")

    with _factory.ephemeral_session(service, None, hosts=(HOST,)) as provisioned:
        assert provisioned.session_id != "keeper"
        assert provisioned.reused is False
        assert provisioned.ephemeral is True
    keeper = store.get("keeper")
    assert keeper is not None
    assert keeper.generation == 0


def test_a_caller_can_always_demand_a_fresh_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    service = _service(store)
    _seed(store, "keeper")
    monkeypatch.setenv(_factory.SESSION_OWNER_ENV, FLOW_TOKEN)

    with _factory.ephemeral_session(service, None, hosts=(HOST,), reuse=False) as provisioned:
        assert provisioned.session_id != "keeper"
        assert provisioned.reused is False
        assert provisioned.ephemeral is True
    keeper = store.get("keeper")
    assert keeper is not None
    assert keeper.generation == 0


def test_the_first_step_of_a_flow_retains_its_session_for_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a flow token and nothing to match, the session created is kept.

    Otherwise the flow could never have a second step: an invocation that
    closed its own session on the way out would leave the next one nothing
    to continue.
    """
    store = _store(tmp_path)
    service = _service(store)
    monkeypatch.setenv(_factory.SESSION_OWNER_ENV, FLOW_TOKEN)

    with _factory.ephemeral_session(service, None, hosts=(HOST,)) as provisioned:
        session_id = provisioned.session_id
        assert session_id is not None
        assert provisioned.ephemeral is False
        assert provisioned.reused is False
    record = store.get(session_id)
    assert record is not None
    assert record.status is SessionStatus.ACTIVE
    assert record.owner_token == FLOW_TOKEN


def test_the_result_names_the_session_it_reused(tmp_path: Path) -> None:
    """Criterion 4 rides the same structured path as t2's ephemeral flag."""
    store = _store(tmp_path)
    service = _service(store)
    context = _context()
    _seed(store, "keeper")

    result = service.execute(
        _factory.build_operation(
            service,
            context,
            OperationKind.PAGE_OPEN,
            target=OperationTarget(url=PAGE_URL),
            session_id="keeper",
            session_reused=True,
        ),
        context,
    )
    assert result.lifecycle_state is LifecycleState.SUCCEEDED
    assert result.content.trusted["session"] == {
        "session_id": "keeper",
        "ephemeral": False,
        "reused": True,
    }


def test_a_page_verb_can_be_told_to_use_a_fresh_session(tmp_path: Path) -> None:
    """The CLI surface of criterion 4: ``--fresh-session`` opts out per call."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    page_cmd.register(sub)  # type: ignore[arg-type]
    args = parser.parse_args(["page", "open", PAGE_URL, "--fresh-session"])
    assert args.fresh_session is True
    assert parser.parse_args(["page", "open", PAGE_URL]).fresh_session is False
