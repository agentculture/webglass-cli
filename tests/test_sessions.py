"""Characterization tests for ``webglass.sessions`` (build plan task t8).

Covers the browser-session state kind in isolation: :class:`SessionRecord`'s
endpoint-secrecy redactions, :class:`InMemorySessionStore`'s CRUD/clean
lifecycle, its lease-conflict semantics (issue #1 implementation spec claim
c29, honesty h26 — a session is reusable and exclusive across concurrent
attaches; claim c33, honesty h30 — the connect endpoint is secret-equivalent
and never serialized), and the four-store separation invariant from
``CLAUDE.md`` "Target architecture" section 2 (claim c3 / honesty h3): this
module must not import ``exploration.py``, ``memory.py``, or ``context.py``,
nor any of the sibling tasks' concurrently-developed modules.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

import webglass
from webglass.sessions import (
    DEFAULT_LEASE_TTL_SECONDS,
    InMemorySessionStore,
    Lease,
    LeaseGrant,
    LeaseRefusal,
    SessionRecord,
    SessionStatus,
    SessionStore,
)

_PACKAGE_ROOT = Path(webglass.__file__).resolve().parent

# The four state kinds that must never cross-import each other.
_STATE_MODULES = {"sessions", "context", "exploration", "memory"}

# Sibling tasks' modules under concurrent development at M1 — t8 must not
# depend on any of them; integration happens at t9.
_SIBLING_MODULES = {
    "operations",
    "results",
    "effects",
    "policy",
    "pages",
    "extraction",
    "references",
    "adapters",
    "service",
    "artifacts",
}


def _import_tokens(path: Path) -> set[str]:
    """Every module name referenced by a top-level import statement in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                tokens.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                tokens.add(node.module)
            elif node.level:  # `from . import x` / `from .. import x`
                for alias in node.names:
                    tokens.add(alias.name)
    return tokens


def _root_component(token: str) -> str:
    if token.startswith("webglass."):
        token = token[len("webglass.") :]
    return token.split(".")[0]


def test_sessions_module_has_no_cross_state_or_sibling_imports() -> None:
    path = _PACKAGE_ROOT / "sessions.py"
    tokens = {_root_component(token) for token in _import_tokens(path)}
    forbidden = (_STATE_MODULES - {"sessions"}) | _SIBLING_MODULES
    offenders = tokens & forbidden
    assert not offenders, f"sessions.py must not import: {sorted(offenders)}"


# --- SessionRecord: endpoint secrecy -----------------------------------


def _make_record(**overrides: object) -> SessionRecord:
    fields = dict(
        session_id="s1",
        generation=0,
        owner="colleague",
        caller="colleague-cli",
        task="task-1",
        backend_id="chromium",
        created_at=1000.0,
        last_used_at=1000.0,
        expires_at=2000.0,
        endpoint_ref="ws://127.0.0.1:9999/devtools/browser/SECRET-TOKEN",
    )
    fields.update(overrides)
    return SessionRecord(**fields)  # type: ignore[arg-type]


def test_session_record_repr_redacts_endpoint_ref() -> None:
    record = _make_record()
    rendered = repr(record)
    assert "<redacted>" in rendered
    assert "SECRET-TOKEN" not in rendered
    # Non-secret fields still show up, so the repr stays useful for debugging.
    assert "session_id='s1'" in rendered


def test_session_record_to_public_dict_omits_endpoint_ref() -> None:
    record = _make_record()
    public = record.to_public_dict()
    assert "endpoint_ref" not in public
    serialized = repr(public)
    assert "SECRET-TOKEN" not in serialized
    assert public["session_id"] == "s1"
    assert public["status"] == "active"
    assert public["lease"] is None


def test_session_record_to_public_dict_includes_lease_without_endpoint() -> None:
    record = _make_record(lease=Lease(holder="h1", acquired_at=1000.0, expires_at=1030.0))
    public = record.to_public_dict()
    assert public["lease"] == {"holder": "h1", "acquired_at": 1000.0, "expires_at": 1030.0}
    assert "endpoint_ref" not in public


# --- InMemorySessionStore: CRUD + clean ---------------------------------


def test_create_returns_active_session_with_generation_zero() -> None:
    store = InMemorySessionStore()
    record = store.create(
        session_id="s1",
        owner="o",
        caller="c",
        task="t",
        backend_id="chromium",
        now=100.0,
        expires_at=200.0,
    )
    assert record.status is SessionStatus.ACTIVE
    assert record.generation == 0
    assert record.created_at == 100.0
    assert record.last_used_at == 100.0
    assert store.get("s1") is record
    assert store.list() == [record]


def test_create_duplicate_session_id_raises() -> None:
    store = InMemorySessionStore()
    store.create(
        session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=10
    )
    with pytest.raises(ValueError):
        store.create(
            session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=10
        )


def test_get_unknown_session_returns_none() -> None:
    store = InMemorySessionStore()
    assert store.get("missing") is None


def test_close_marks_closed_and_clears_lease_and_touches_nothing_else() -> None:
    store = InMemorySessionStore()
    store.create(
        session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=100
    )
    store.acquire_lease("s1", holder="h1", now=0)

    store.close("s1")

    record = store.get("s1")
    assert record is not None
    assert record.status is SessionStatus.CLOSED
    assert record.lease is None
    # "Touches nothing else" is structurally guaranteed: close()'s signature
    # takes only a session_id, and this module imports no evidence/exploration
    # store to call into (see test_sessions_module_has_no_cross_state_or_sibling_imports).


def test_close_unknown_session_raises_key_error() -> None:
    store = InMemorySessionStore()
    with pytest.raises(KeyError):
        store.close("missing")


def test_clean_reaps_only_active_sessions_past_expiry() -> None:
    store = InMemorySessionStore()
    store.create(
        session_id="expired", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=50
    )
    store.create(
        session_id="fresh", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=500
    )
    store.create(
        session_id="already-closed",
        owner="o",
        caller="c",
        task="t",
        backend_id="b",
        now=0,
        expires_at=10,
    )
    store.close("already-closed")

    reaped = store.clean(now=100.0)

    reaped_ids = {r.session_id for r in reaped}
    assert reaped_ids == {"expired"}
    assert store.get("expired").status is SessionStatus.EXPIRED  # type: ignore[union-attr]
    assert store.get("fresh").status is SessionStatus.ACTIVE  # type: ignore[union-attr]
    # clean() must not resurrect or otherwise touch an already-closed session.
    assert store.get("already-closed").status is SessionStatus.CLOSED  # type: ignore[union-attr]


def test_clean_releases_lease_on_reaped_session() -> None:
    store = InMemorySessionStore()
    store.create(
        session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=50
    )
    store.acquire_lease("s1", holder="h1", now=0)

    reaped = store.clean(now=100.0)

    assert reaped[0].lease is None


# --- Lease semantics -----------------------------------------------------


def test_acquire_lease_succeeds_for_first_holder() -> None:
    store = InMemorySessionStore()
    store.create(
        session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=1000
    )

    outcome = store.acquire_lease("s1", holder="h1", now=10.0)

    assert isinstance(outcome, LeaseGrant)
    assert outcome.session_id == "s1"
    assert outcome.holder == "h1"
    assert outcome.acquired_at == 10.0
    assert outcome.expires_at == 10.0 + DEFAULT_LEASE_TTL_SECONDS


def test_second_concurrent_acquire_returns_typed_refusal_not_an_exception() -> None:
    store = InMemorySessionStore()
    store.create(
        session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=1000
    )
    store.acquire_lease("s1", holder="h1", now=10.0)

    outcome = store.acquire_lease("s1", holder="h2", now=11.0)

    assert isinstance(outcome, LeaseRefusal)
    assert outcome.session_id == "s1"
    assert outcome.requested_by == "h2"
    assert outcome.held_by == "h1"
    assert outcome.reason == "lease_held"


def test_acquire_lease_succeeds_again_after_release() -> None:
    store = InMemorySessionStore()
    store.create(
        session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=1000
    )
    store.acquire_lease("s1", holder="h1", now=10.0)
    refusal = store.acquire_lease("s1", holder="h2", now=11.0)
    assert isinstance(refusal, LeaseRefusal)

    released = store.release_lease("s1", holder="h1")
    assert released is True

    outcome = store.acquire_lease("s1", holder="h2", now=12.0)
    assert isinstance(outcome, LeaseGrant)
    assert outcome.holder == "h2"


def test_release_lease_by_non_holder_is_a_noop() -> None:
    store = InMemorySessionStore()
    store.create(
        session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=1000
    )
    store.acquire_lease("s1", holder="h1", now=10.0)

    released = store.release_lease("s1", holder="someone-else")

    assert released is False
    # h1 still holds it: h2 gets refused.
    outcome = store.acquire_lease("s1", holder="h2", now=11.0)
    assert isinstance(outcome, LeaseRefusal)
    assert outcome.held_by == "h1"


def test_same_holder_can_renew_its_own_lease() -> None:
    store = InMemorySessionStore()
    store.create(
        session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=1000
    )
    store.acquire_lease("s1", holder="h1", now=10.0, ttl_seconds=5.0)

    outcome = store.acquire_lease("s1", holder="h1", now=12.0, ttl_seconds=5.0)

    assert isinstance(outcome, LeaseGrant)
    assert outcome.expires_at == 17.0


def test_lease_becomes_available_after_it_expires_without_release() -> None:
    store = InMemorySessionStore()
    store.create(
        session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=1000
    )
    store.acquire_lease("s1", holder="h1", now=10.0, ttl_seconds=5.0)  # expires at 15.0

    # h1 "crashed" and never released; h2 tries after the lease timed out.
    outcome = store.acquire_lease("s1", holder="h2", now=16.0)

    assert isinstance(outcome, LeaseGrant)
    assert outcome.holder == "h2"


def test_acquire_lease_on_unknown_session_raises_key_error() -> None:
    store = InMemorySessionStore()
    with pytest.raises(KeyError):
        store.acquire_lease("missing", holder="h1", now=0.0)


def test_acquire_lease_on_closed_session_returns_refusal_not_exception() -> None:
    store = InMemorySessionStore()
    store.create(
        session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=1000
    )
    store.close("s1")

    outcome = store.acquire_lease("s1", holder="h1", now=1.0)

    assert isinstance(outcome, LeaseRefusal)
    assert outcome.reason.startswith("session_not_active")


def test_bump_generation_increments_and_unknown_session_raises() -> None:
    store = InMemorySessionStore()
    store.create(
        session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=1000
    )

    record = store.bump_generation("s1")
    assert record.generation == 1
    record = store.bump_generation("s1")
    assert record.generation == 2

    with pytest.raises(KeyError):
        store.bump_generation("missing")


def test_real_concurrent_threads_racing_for_a_lease_yield_exactly_one_grant() -> None:
    """A genuine two-thread race, not just two sequential calls dressed up as one.

    Both threads block on a barrier so they call ``acquire_lease`` as close
    to simultaneously as the interpreter allows; the store's internal lock
    (see ``InMemorySessionStore``) must still guarantee exactly one grant.
    """
    store = InMemorySessionStore()
    store.create(
        session_id="s1", owner="o", caller="c", task="t", backend_id="b", now=0, expires_at=1000
    )

    barrier = threading.Barrier(2)
    outcomes: list[object] = [None, None]

    def attempt(index: int, holder: str) -> None:
        barrier.wait()
        outcomes[index] = store.acquire_lease("s1", holder=holder, now=0.0)

    t1 = threading.Thread(target=attempt, args=(0, "thread-a"))
    t2 = threading.Thread(target=attempt, args=(1, "thread-b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    grants = [o for o in outcomes if isinstance(o, LeaseGrant)]
    refusals = [o for o in outcomes if isinstance(o, LeaseRefusal)]
    assert len(grants) == 1
    assert len(refusals) == 1
    assert refusals[0].held_by == grants[0].holder


# --- Protocol conformance -------------------------------------------------


def test_in_memory_session_store_satisfies_session_store_protocol() -> None:
    store = InMemorySessionStore()
    assert isinstance(store, SessionStore)
