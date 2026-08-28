"""Browser session store — one of WebGlass's four separate state kinds.

Per ``CLAUDE.md`` "Target architecture" section 2, a browser session is a
live/resumable context with tabs, cookies, storage, and leases. It is
volatile and sensitive by default, isolated per caller/task, and must
**never be emitted wholesale** in JSON, logs, evidence, or diagnostics. This
module owns *only* that state kind: it must not import ``exploration.py``,
``memory.py``, or ``context.py`` (see the cross-import AST assertions in
``tests/test_sessions.py``), and it must not import any of the sibling
task's concurrently-developed modules (``operations``/``results``/
``effects``, ``policy``, ``pages``/``extraction``/``references``, or the
service/adapter layer) — those integrate at task t9, not here.

Two later milestone tasks build directly on the contract defined here (see
the implementation spec, claims c29/c33 and honesty conditions h26/h30):

- **t11/t12 (M2)** implement the live-process CDP reattach mechanism and its
  endpoint-secrecy/lease/cleanup properties. This module defines the
  :class:`SessionStore` protocol and the lease machinery (:class:`LeaseGrant`
  / :class:`LeaseRefusal`) those tasks implement against, plus
  :class:`InMemorySessionStore` as a reference implementation that exercises
  the same contract for M1 characterization with fake backends.
- A ``FileSessionStore`` (an on-disk store providing real endpoint secrecy
  via file permissions) is explicitly **not** built here — that is task
  t12's job. This module only fixes the :class:`SessionStore` protocol shape
  it must satisfy.

The connect endpoint (:attr:`SessionRecord.endpoint_ref`) is
**secret-equivalent**: whatever transport it names (e.g. a CDP websocket
URL) grants full control of the underlying browser. It is therefore
excluded from the dataclass's ``repr()`` and omitted entirely from
:meth:`SessionRecord.to_public_dict`, the one method that is safe to feed
into JSON output, logs, or evidence records. Both redactions are
characterized in ``tests/test_sessions.py``.

Timestamps are never read from the wall clock inside this module — every
method that needs "now" takes it as an explicit ``float`` parameter (epoch
seconds), so tests stay fully deterministic and callers can inject whatever
clock they like (a real ``time.time`` at the call site, or a fixed value in
a test). Nothing here calls ``datetime.now()`` or ``time.time()``.
"""

from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

#: Default lease time-to-live, in seconds, used by :class:`InMemorySessionStore`
#: when a caller does not specify one explicitly. A crashed lease holder's
#: lease becomes available to a new holder once it passes its ``expires_at`` —
#: this is what keeps a crashed caller from starving a session forever.
DEFAULT_LEASE_TTL_SECONDS = 30.0

_REDACTED = "<redacted>"

#: Dataclass field-metadata key that keeps a field's *value* out of
#: :meth:`SessionRecord.__repr__`, which renders ``<name>=<redacted>``
#: instead. :attr:`SessionRecord.endpoint_ref` is redacted unconditionally
#: because it is secret-equivalent; a subclass field opts in through this
#: key when its value is sensitive for some other reason
#: (``FileSessionRecord.hosts`` is browsing history — build plan t8).
#:
#: It exists because the base ``__repr__`` enumerates
#: ``dataclasses.fields(self)`` on the *instance*, so a subclass field is
#: printed by the inherited redacting repr with no way to opt out short of
#: overriding that repr — and overriding it is precisely the mistake
#: ``FileSessionRecord``'s ``repr=False`` exists to prevent.
REPR_REDACTED = "webglass.repr_redacted"


def _repr_redacted(field: dataclasses.Field[Any]) -> bool:
    """Whether ``field``'s value must not appear in a ``repr()``.

    ``endpoint_ref`` is named explicitly rather than carrying the metadata
    itself so the redaction survives a subclass redeclaring the field.
    """
    return field.name == "endpoint_ref" or bool(field.metadata.get(REPR_REDACTED))


class SessionStatus(str, Enum):
    """Lifecycle status of a :class:`SessionRecord`."""

    ACTIVE = "active"
    EXPIRED = "expired"
    CLOSED = "closed"


@dataclass
class Lease:
    """A held lease on a session: one holder, valid until ``expires_at``."""

    holder: str
    acquired_at: float
    expires_at: float


@dataclass(frozen=True)
class LeaseGrant:
    """Structured success result from :meth:`SessionStore.acquire_lease`."""

    session_id: str
    holder: str
    acquired_at: float
    expires_at: float


@dataclass(frozen=True)
class LeaseRefusal:
    """Structured refusal result from :meth:`SessionStore.acquire_lease`.

    This is a typed, ordinary return value — **never** an exception. A
    second concurrent caller trying to acquire a lease already held by
    someone else is expected, normal-flow traffic (two processes racing to
    attach to the same reusable session), not an error condition.
    """

    session_id: str
    requested_by: str
    held_by: str
    reason: str = "lease_held"


#: The result type returned by :meth:`SessionStore.acquire_lease`.
LeaseOutcome = LeaseGrant | LeaseRefusal


@dataclass
class SessionRecord:
    """A browser session's control-plane state.

    Deliberately excludes any live browser handle, tab list, cookie jar, or
    storage snapshot — those live in the real browser process this record
    merely references via :attr:`endpoint_ref`. ``capability_profile_ref``
    is typed loosely (``Any``) on purpose: this module must not import
    ``policy.py`` (a sibling task's module), so it only carries an opaque
    reference the caller can resolve elsewhere.
    """

    session_id: str
    generation: int
    owner: str
    caller: str
    task: str
    backend_id: str
    created_at: float
    last_used_at: float
    expires_at: float
    capability_profile_ref: Any = None
    status: SessionStatus = SessionStatus.ACTIVE
    lease: Lease | None = None
    endpoint_ref: str = ""

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        """Every field, except that redacted ones render as ``<redacted>``.

        ``endpoint_ref`` is always redacted; any other field (including one
        declared by a subclass) opts in with ``metadata={REPR_REDACTED: True}``.
        The redacted names are still *shown* — a repr that dropped them would
        hide the fact that the record carries them at all.
        """
        shown = ", ".join(
            f"{f.name}={getattr(self, f.name)!r}"
            for f in dataclasses.fields(self)
            if not _repr_redacted(f)
        )
        hidden = ", ".join(
            f"{f.name}={_REDACTED}" for f in dataclasses.fields(self) if _repr_redacted(f)
        )
        return f"{type(self).__name__}({shown}, {hidden})"

    def to_public_dict(self) -> dict[str, Any]:
        """A dict safe for JSON output, logs, or evidence: no ``endpoint_ref``.

        ``endpoint_ref`` is secret-equivalent (issue #1 implementation spec
        claim c33 / honesty h30: "no JSON result, log line, or evidence
        record contains it") — this is the one place that boundary is
        enforced structurally, in the record itself, rather than left to
        every caller remembering to filter it before serializing.
        """
        return {
            "session_id": self.session_id,
            "generation": self.generation,
            "owner": self.owner,
            "caller": self.caller,
            "task": self.task,
            "backend_id": self.backend_id,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "expires_at": self.expires_at,
            "capability_profile_ref": self.capability_profile_ref,
            "status": self.status.value,
            "lease": (
                None
                if self.lease is None
                else {
                    "holder": self.lease.holder,
                    "acquired_at": self.lease.acquired_at,
                    "expires_at": self.lease.expires_at,
                }
            ),
        }


@runtime_checkable
class SessionStore(Protocol):
    """The contract every session store must satisfy.

    :class:`InMemorySessionStore` below is the M1 reference implementation
    (used with fake backends per the build plan's t9/t10). Task t12 builds
    the on-disk ``FileSessionStore`` at M2 with real endpoint-secrecy (file
    permissions) and real process cleanup — it implements this same
    protocol rather than inventing a new one.
    """

    def create(
        self,
        *,
        session_id: str,
        owner: str,
        caller: str,
        task: str,
        backend_id: str,
        now: float,
        expires_at: float,
        capability_profile_ref: Any = None,
        endpoint_ref: str = "",
    ) -> SessionRecord:
        """Create and store a new active session; return its record."""

    def get(self, session_id: str) -> SessionRecord | None:
        """Return the record for ``session_id``, or ``None`` if unknown."""

    def list(self) -> list[SessionRecord]:
        """Return every stored session record (any status)."""

    def close(self, session_id: str) -> None:
        """Mark a session closed and release any held lease.

        Must touch nothing outside this store: no evidence record, no
        exploration edge, no memory entry. Those are separate state kinds
        (see the module docstring and ``tests/test_sessions.py``).
        """

    def clean(
        self,
        now: float,
        *,
        older_than_seconds: float | None = None,
        status: SessionStatus | None = None,
        site: str | None = None,
    ) -> list[SessionRecord]:
        """Expire sessions past their ``expires_at`` and return what was reaped.

        ``older_than_seconds``/``status``/``site`` (build plan t9, issue #14)
        narrow *which* records this sweep is allowed to touch, and compose as
        AND: a record must satisfy every filter that was passed to be
        eligible. ``None`` for a given filter means "no constraint from this
        one" — the historical no-argument ``clean(now)`` call stays exactly
        as permissive as before. Implementations must evaluate these under
        the same per-record lock the rest of ``clean`` uses, never via a
        caller reading the result and filtering afterwards: a read-then-act
        split would race a concurrent ``clean``/lease call between the two.
        """

    def acquire_lease(
        self,
        session_id: str,
        holder: str,
        now: float,
        ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> LeaseGrant | LeaseRefusal:
        """Attempt to take exclusive control of a session for ``holder``.

        Succeeds (returns :class:`LeaseGrant`) if no one else holds a live
        lease, or if ``holder`` already holds it (renewal), or if the
        current lease has passed its own ``expires_at`` (the previous
        holder is presumed gone). Otherwise returns a :class:`LeaseRefusal`
        — never raises for this case.
        """

    def release_lease(self, session_id: str, holder: str) -> bool:
        """Release ``holder``'s lease if held; return whether one was released."""


class InMemorySessionStore:
    """M1 reference :class:`SessionStore`: process-local, no persistence.

    Suitable for unit tests and for driving the operation service against
    fake backends (t9/t10). Not suitable for the M2 cross-invocation-session
    use case (issue #1 spec claim c29) — that needs state to survive across
    separate CLI subprocesses, which is exactly what t12's on-disk
    ``FileSessionStore`` is for.

    Guarded by an internal :class:`threading.RLock` so the lease-conflict
    guarantee ("one holder wins, the other gets a refusal") actually holds
    under genuine concurrent access from multiple threads, not just under
    sequential calls that merely look concurrent.
    """

    def __init__(self) -> None:
        self._records: dict[str, SessionRecord] = {}
        self._lock = threading.RLock()

    def create(
        self,
        *,
        session_id: str,
        owner: str,
        caller: str,
        task: str,
        backend_id: str,
        now: float,
        expires_at: float,
        capability_profile_ref: Any = None,
        endpoint_ref: str = "",
    ) -> SessionRecord:
        with self._lock:
            if session_id in self._records:
                raise ValueError(f"session_id already exists: {session_id}")
            record = SessionRecord(
                session_id=session_id,
                generation=0,
                owner=owner,
                caller=caller,
                task=task,
                backend_id=backend_id,
                created_at=now,
                last_used_at=now,
                expires_at=expires_at,
                capability_profile_ref=capability_profile_ref,
                status=SessionStatus.ACTIVE,
                lease=None,
                endpoint_ref=endpoint_ref,
            )
            self._records[session_id] = record
            return record

    def get(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            return self._records.get(session_id)

    def list(self) -> list[SessionRecord]:
        with self._lock:
            return list(self._records.values())

    def close(self, session_id: str) -> None:
        with self._lock:
            record = self._require(session_id)
            record.status = SessionStatus.CLOSED
            record.lease = None

    def clean(
        self,
        now: float,
        *,
        older_than_seconds: float | None = None,
        status: SessionStatus | None = None,
        site: str | None = None,
    ) -> list[SessionRecord]:
        with self._lock:
            reaped: list[SessionRecord] = []
            for record in self._records.values():
                if record.status is not SessionStatus.ACTIVE or record.expires_at > now:
                    continue
                if status is not None and record.status is not status:
                    continue
                if older_than_seconds is not None and (now - record.expires_at) < (
                    older_than_seconds
                ):
                    continue
                if site is not None:
                    # The base SessionRecord carries no navigation history at
                    # all (that is FileSessionRecord's t8 addition), so an
                    # in-memory record can never be known to have visited
                    # anywhere -- same "unknown is not a match" rule as the
                    # file store's ``hosts is None`` case.
                    hosts = getattr(record, "hosts", None)
                    if hosts is None or site not in hosts:
                        continue
                record.status = SessionStatus.EXPIRED
                record.lease = None
                reaped.append(record)
            return reaped

    def acquire_lease(
        self,
        session_id: str,
        holder: str,
        now: float,
        ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> LeaseGrant | LeaseRefusal:
        with self._lock:
            record = self._require(session_id)
            if record.status is not SessionStatus.ACTIVE:
                return LeaseRefusal(
                    session_id=session_id,
                    requested_by=holder,
                    held_by="",
                    reason=f"session_not_active:{record.status.value}",
                )
            current = record.lease
            held_by_someone_else = (
                current is not None and current.holder != holder and current.expires_at > now
            )
            if held_by_someone_else:
                assert current is not None  # narrows type for mypy-style readers
                return LeaseRefusal(
                    session_id=session_id,
                    requested_by=holder,
                    held_by=current.holder,
                    reason="lease_held",
                )
            record.lease = Lease(holder=holder, acquired_at=now, expires_at=now + ttl_seconds)
            record.last_used_at = now
            return LeaseGrant(
                session_id=session_id,
                holder=holder,
                acquired_at=now,
                expires_at=record.lease.expires_at,
            )

    def release_lease(self, session_id: str, holder: str) -> bool:
        with self._lock:
            record = self._require(session_id)
            if record.lease is not None and record.lease.holder == holder:
                record.lease = None
                return True
            return False

    def bump_generation(self, session_id: str) -> SessionRecord:
        """Increment ``generation`` (e.g. on a navigation-level change).

        No navigation logic lives in this module — this is a plain counter
        bump exposed so a later, page-owning module (t7/t9 territory) can
        call it without this store needing to know why.
        """
        with self._lock:
            record = self._require(session_id)
            record.generation += 1
            return record

    def _require(self, session_id: str) -> SessionRecord:
        record = self._records.get(session_id)
        if record is None:
            raise KeyError(f"unknown session_id: {session_id}")
        return record
