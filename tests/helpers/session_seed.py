"""Seed a :class:`FileSessionStore` with records of a chosen shape (task t1).

Every later build-plan task that reports on, sweeps, or cleans up session
records (M2's session hygiene work, tracked in issue #14) needs a
reproducible "before state" to assert against — most concretely, the
reporting-host baseline this task's acceptance criteria names verbatim:
**134 records total, 115 ``closed``, 19 ``active`` with a dead pid.** Rather
than let every later test hand-roll
:class:`~webglass.adapters.session_store.FileSessionRecord` construction
(and inevitably drift from how the store itself builds one), this module is
the one place that does it.

Two building blocks:

* :func:`seed_records` writes ``count`` records directly into a store at a
  chosen status/expiry/owner, bypassing
  :meth:`~webglass.adapters.session_store.FileSessionStore.create` (which
  always creates an ``active`` record and optionally launches a browser —
  neither of which a "134 closed records already sitting on disk" scenario
  wants). Records are still written through the store's own
  ``_write_unlocked``/``_to_payload`` path, so a seeded record is
  byte-for-byte what the store itself would have produced for the same
  field values, not a hand-rolled JSON shape that happens to look similar.
* :func:`assert_seeded_totals` reads a store back and asserts its
  status/dead-pid counts match expectations, with a failure message that
  names which count was wrong — this is what makes the 134/115/19 baseline
  a *reproducible* assertion rather than a comment.

Forward-compatible, inert fields
---------------------------------

Two build-plan tasks that depend on this one add fields to
:class:`FileSessionRecord`: t7's owner token (now landed) and t8's
navigated-host set (still pending). :func:`seed_records` accepts
``owner_token`` and ``hosts`` keyword arguments so those tasks' tests can
call this helper without a rewrite, but it never invents a field itself: it
looks at :func:`dataclasses.fields` of the *current* :class:`FileSessionRecord`
and only sets an attribute that is actually declared there. ``owner_token``
is declared as of t7, so passing it now lands on the record and flows
through ``_to_payload`` for real; ``hosts`` has no field yet, so passing it
today is still accepted and silently inert (nothing is written, because
there is nowhere on the record to put it and no payload key to omit it
from) — once t8 adds the field, the very same call starts landing it too.
Passing ``None`` (the default for both) always means "omitted from the
written payload", on both sides of that boundary — never an explicit empty
value.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess  # nosec B404 - fixed argv, no shell; used only to mint a dead pid
import sys
from collections.abc import Iterable, Sequence

from webglass.adapters.session_store import FileSessionRecord, FileSessionStore
from webglass.sessions import SessionStatus

__all__ = [
    "DEFAULT_BACKEND_ID",
    "DEFAULT_CALLER",
    "DEFAULT_OWNER",
    "DEFAULT_TASK",
    "assert_seeded_totals",
    "count_active_with_dead_pids",
    "count_by_status",
    "seed_records",
    "spawn_dead_pid",
]

DEFAULT_OWNER = "seed-owner"
DEFAULT_CALLER = "seed-caller"
DEFAULT_TASK = "seed-task"
DEFAULT_BACKEND_ID = "seed-backend"

#: Field names actually declared on the *current* record shape. Recomputed
#: from the dataclass itself (never hand-copied) so this module notices the
#: moment t8 adds ``hosts`` too — no separate list to forget to update.
#: (t7's ``owner_token`` already showed up here the moment that field landed.)
_RECORD_FIELDS = {field.name for field in dataclasses.fields(FileSessionRecord)}


def spawn_dead_pid() -> int:
    """Return a pid that is guaranteed to no longer refer to a live process.

    Spawns the smallest possible real subprocess, waits for it to exit, and
    hands back its pid. Using a genuinely dead pid (rather than an
    arbitrarily large made-up integer) means a seeded "active session with a
    dead pid" record exercises exactly the ``kill(pid, 0)`` ->
    ``ProcessLookupError`` path that
    :func:`webglass.adapters.session_store.terminate_pid` and the store's own
    liveness check exercise in production — not a different, weaker
    approximation of it. The subprocess is reaped (``check=False``) before
    the pid is returned, so no zombie lingers to keep the pid answering
    ``kill(pid, 0)`` as if it were still alive.
    """
    process = subprocess.Popen([sys.executable, "-c", "pass"])  # nosec B603 - fixed argv, no shell
    process.wait()
    return process.pid


def _pid_is_alive(pid: int) -> bool:
    """Mirror of the store's own liveness check (kept independent on purpose).

    Deliberately re-implemented rather than imported from
    ``webglass.adapters.session_store``: this is a *test* assertion about
    what the production code should conclude, and importing the very
    function under test would let a bug in both places cancel out silently.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, but not ours to signal -- still alive for counting purposes.
        return True
    return True


def seed_records(
    store: FileSessionStore,
    *,
    count: int,
    status: SessionStatus,
    owner: str = DEFAULT_OWNER,
    caller: str | None = None,
    task: str = DEFAULT_TASK,
    backend_id: str = DEFAULT_BACKEND_ID,
    now: float = 1_700_000_000.0,
    expires_at: float | None = None,
    pid: int | None = None,
    session_id_prefix: str = "seed",
    hosts: Iterable[str] | None = None,
    owner_token: str | None = None,
) -> list[FileSessionRecord]:
    """Write ``count`` records of a chosen shape directly into ``store``.

    Every record shares the same status/expiry/owner/pid/host-set — callers
    that need a mixed population (e.g. the 115-closed-plus-19-active-with-
    dead-pid baseline) call this twice against the same ``store`` with
    different arguments. Session ids are ``<session_id_prefix>-<status>-<i>``
    so two calls against the same store with different statuses (or
    prefixes) never collide.

    ``expires_at`` defaults to five minutes after ``now`` when omitted --
    fine for a ``closed`` record (expiry is irrelevant once closed) and for
    an ``active`` one seeded as still-within-lease; pass an already-past
    value explicitly to seed an *expired* record instead.

    ``hosts``/``owner_token`` are forward-compatible seams for build-plan
    tasks t8/t7 -- see the module docstring. They are accepted here whether
    or not the underlying field exists yet, and never break this call
    either way.
    """
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    resolved_caller = owner if caller is None else caller
    resolved_expires_at = now + 300.0 if expires_at is None else expires_at

    records: list[FileSessionRecord] = []
    for index in range(count):
        session_id = f"{session_id_prefix}-{status.value}-{index}"
        record = FileSessionRecord(
            session_id=session_id,
            generation=0,
            owner=owner,
            caller=resolved_caller,
            task=task,
            backend_id=backend_id,
            created_at=now,
            last_used_at=now,
            expires_at=resolved_expires_at,
            capability_profile_ref=None,
            status=status,
            lease=None,
            endpoint_ref="",
            diagnostics=(),
            pid=pid,
        )
        _set_if_declared(record, "owner_token", owner_token)
        _set_if_declared(record, "hosts", None if hosts is None else tuple(hosts))
        store._write_unlocked(record)  # noqa: SLF001 - the one path a seed helper needs
        records.append(record)
    return records


def _set_if_declared(record: FileSessionRecord, field_name: str, value: object) -> None:
    """Set ``field_name`` on ``record`` only if both it is declared and ``value`` is given.

    ``value is None`` always means "leave this out" -- whether that is
    because the caller did not pass it, or because the field does not exist
    on this schema version yet. Both cases produce the same record: no
    attribute is set, so ``_to_payload`` (today's, or t7/t8's future one)
    writes exactly what it would have written without this helper involved.
    """
    if value is None:
        return
    if field_name not in _RECORD_FIELDS:
        return
    setattr(record, field_name, value)


def count_by_status(records: Sequence[FileSessionRecord], status: SessionStatus) -> int:
    """How many ``records`` are at ``status``."""
    return sum(1 for record in records if record.status is status)


def count_active_with_dead_pids(records: Sequence[FileSessionRecord]) -> int:
    """How many ``records`` are ``active`` but whose stored pid is no longer running.

    This is exactly the "before state" shape a session-hygiene sweep starts
    from: an active record left behind by a caller whose browser process
    (and, in the real M2 path, the CLI process that launched it) is long
    gone. A record with ``pid is None`` never counts here -- it was created
    without a browser attached at all, which is a different, unremarkable
    state.
    """
    return sum(
        1
        for record in records
        if record.status is SessionStatus.ACTIVE
        and record.pid is not None
        and not _pid_is_alive(record.pid)
    )


def assert_seeded_totals(
    store: FileSessionStore,
    *,
    total: int,
    closed: int,
    active_with_dead_pids: int,
) -> None:
    """Assert ``store`` holds exactly the record counts named.

    Written as one assertion per count (rather than a single combined
    boolean) so a mismatch's failure message names *which* count was wrong
    -- the whole point of making the 134/115/19 reporting-host baseline a
    reproducible check instead of a comment repeating a number.
    """
    records = store.list()
    actual_total = len(records)
    actual_closed = count_by_status(records, SessionStatus.CLOSED)
    actual_active_dead = count_active_with_dead_pids(records)

    assert (
        actual_total == total
    ), f"expected {total} total records, found {actual_total}"  # nosec B101 - test assertion helper
    assert (
        actual_closed == closed
    ), (  # nosec B101 - test assertion helper
        f"expected {closed} closed records, found {actual_closed}"
    )
    assert actual_active_dead == active_with_dead_pids, (  # nosec B101 - test assertion helper
        f"expected {active_with_dead_pids} active records with a dead pid, "
        f"found {actual_active_dead}"
    )
